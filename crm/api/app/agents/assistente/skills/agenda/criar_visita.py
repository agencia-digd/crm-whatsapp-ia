"""
Cria visita no Calendar Acme (Gateway, cor laranja) E grava em vendas.

Ordem da execução (CRÍTICA):
  1. POST Gateway → captura evento_calendar_id
  2. Se Gateway OK: INSERT em visitas com evento_calendar_id, status='agendada'
  3. Se Gateway FALHA: retorna erro, NÃO grava Postgres
  4. Se Gateway OK mas Postgres FALHA: warning loud com evento_calendar_id
     (não tenta deletar do Calendar — pior dos mundos)

Uso típico (subprocess via agent.py):
  python3 criar_visita.py --corretora Pessoa --cliente "João Silva" \\
    --data 2026-05-20 --hora 14:00 --telefone +5500000000000

Saída: JSON em stdout. Erros vão pra stderr com exit !=0.
"""
import argparse
import json
import os
import sys
from datetime import date, datetime, time, timedelta, timezone

import psycopg

from _helper import (calendar_request, cal_path, parse_corretora_arg,
                     to_brt_iso, COLOR_PLANNED)


CORR_ALIAS_DB = {
    "Pessoa11": "Pessoa1",
    "Pessoa10": "Pessoa5",
    "Pessoa": "Pessoa3",
}


def _err(msg: str, code: int = 1):
    sys.stderr.write(msg + "\n")
    sys.exit(code)


def _parse_data_hora(data_str: str, hora_str: str, allow_past: bool) -> datetime:
    try:
        d = date.fromisoformat(data_str)
    except ValueError:
        _err(f"--data inválida: {data_str} (esperado YYYY-MM-DD)")
    try:
        hh, mm = map(int, hora_str.split(":"))
        t = time(hh, mm)
    except (ValueError, IndexError):
        _err(f"--hora inválida: {hora_str} (esperado HH:MM)")
    brt = timezone(timedelta(hours=-3))
    dt = datetime.combine(d, t, tzinfo=brt)
    if not allow_past and d < date.today():
        _err(f"data no passado: {data_str} (use --allow-past pra forçar)")
    return dt


def _resolve_corretora(conn, prefixo: str) -> tuple[int, str]:
    nome_db = CORR_ALIAS_DB.get(prefixo)
    if not nome_db:
        _err(f"prefixo de corretora não mapeado: {prefixo}")
    with conn.cursor() as cur:
        cur.execute("SELECT id, nome, ativo FROM corretoras WHERE nome = %s", (nome_db,))
        row = cur.fetchone()
    if not row:
        _err(f"corretora '{nome_db}' não encontrada na tabela corretoras")
    cid, nome, ativo = row
    if not ativo:
        _err(f"corretora '{nome}' está inativa")
    return cid, nome


def _resolve_telefone(conn, telefone: str | None, nome: str) -> str | None:
    """Descobre o telefone do lead quando quem chamou não passou.

    POR QUE ISSO EXISTE (13/08): 74% das visitas de agosto e 100% de julho
    nasceram SEM telefone. E visita sem telefone não se liga a lead nenhum — o
    funil inteiro fica cego. Foi assim que a Lucilene compareceu, entregou
    documento e ficou invisível: a visita dela não apontava pra pessoa.

    O script sempre soube gravar o telefone; o problema é que dependia de quem
    chama passar `--telefone`, e a Assistente não passava. Resolver aqui conserta
    todos os chamadores de uma vez — inclusive os que ainda não existem.

    Busca por NOME EXATO em `leads`, e só aceita match único: telefone errado
    numa visita é pior que telefone nenhum, porque aí a visita se liga à pessoa
    ERRADA e ninguém desconfia.
    """
    if telefone and "".join(c for c in telefone if c.isdigit()):
        return telefone                      # veio de quem chamou: respeita
    nome_norm = (nome or "").strip()
    if not nome_norm:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT telefone FROM leads
                 WHERE lower(btrim(nome)) = lower(btrim(%s))
                   AND telefone IS NOT NULL AND btrim(telefone) <> ''
                 LIMIT 2
                """,
                (nome_norm,),
            )
            rows = cur.fetchall()
        if len(rows) == 1:
            sys.stderr.write(f"INFO: telefone resolvido pelo nome ({nome_norm})\n")
            return rows[0][0]
        if len(rows) > 1:
            sys.stderr.write(
                f"CONFIRMAR: nome ambíguo pra telefone ({nome_norm}, {len(rows)} leads) — "
                "a corretora precisa dizer qual é\n"
            )
    except Exception as e:  # noqa: BLE001 — nunca impede a visita de ser criada
        sys.stderr.write(f"WARN: resolver telefone falhou: {str(e)[:100]}\n")
    sys.stderr.write(
        f"CONFIRMAR: visita de '{nome_norm}' vai nascer SEM telefone — "
        "vai aparecer no painel pedindo confirmação\n"
    )
    return None


def _resolve_lead_id(conn, telefone: str | None, nome: str) -> str | None:
    """
    Resolve lead_id (kommo_id) em leads_snapshot pra cruzar com o Inspector.

    Estratégia (para no primeiro match não-ambíguo):
      1. Telefone → últimos 8 dígitos (right-8, padrão da casa) → leads_snapshot.
         Exatamente 1 match: usa. >1: ambíguo → NULL. 0: cai pro nome.
      2. Nome exato (case-insensitive) → leads_snapshot.
         Só resolve se exatamente 1 match. 0 ou >1: NULL.

    Ambíguo ou não encontrado → NULL (+ log em stderr, sem quebrar a criação).
    """
    suf = "".join(c for c in (telefone or "") if c.isdigit())[-8:]
    with conn.cursor() as cur:
        if len(suf) == 8:
            cur.execute(
                """
                SELECT lead_id FROM leads_snapshot
                WHERE right(regexp_replace(coalesce(telefone, ''), '\\D', '', 'g'), 8) = %s
                LIMIT 2
                """,
                (suf,),
            )
            rows = cur.fetchall()
            if len(rows) == 1:
                return rows[0][0]
            if len(rows) > 1:
                sys.stderr.write(
                    f"INFO: lead_id ambíguo por telefone (suf={suf}, 2+ matches) — deixando NULL\n"
                )
                return None

        nome_norm = (nome or "").strip()
        if nome_norm:
            cur.execute(
                "SELECT lead_id FROM leads_snapshot WHERE lower(nome) = lower(%s) LIMIT 2",
                (nome_norm,),
            )
            rows = cur.fetchall()
            if len(rows) == 1:
                return rows[0][0]
            if len(rows) > 1:
                sys.stderr.write(
                    f"INFO: lead_id ambíguo por nome ('{nome_norm}', 2+ matches) — deixando NULL\n"
                )
                return None

    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corretora", required=True)
    ap.add_argument("--cliente", required=True)
    ap.add_argument("--data", required=True, help="YYYY-MM-DD")
    ap.add_argument("--hora", required=True, help="HH:MM (BRT)")
    ap.add_argument("--telefone", default=None)
    ap.add_argument("--bairro", default=None)
    ap.add_argument("--obs", default=None,
                    help="observação livre — vai só na description do evento do Calendar")
    ap.add_argument("--duration", type=int, default=60, help="minutos (default 60)")
    ap.add_argument("--allow-past", action="store_true")
    ap.add_argument("--registrado-por-telefone", default=None)
    ap.add_argument("--lead-id", default=None,
                    help="kommo_id do lead (leads_snapshot); resolvido automático se omitido")
    ap.add_argument("--origem", default=None,
                    choices=("whatsapp", "crm", "manual"),
                    help="canal de origem da visita: whatsapp | crm | manual")
    ap.add_argument("--is-revisita", action="store_true",
                    help="marca como re-visita (não conta nas primeiras visitas)")
    ap.add_argument("--check-revisita-only", action="store_true",
                    help="apenas verifica se é re-visita (não cria nada)")
    args = ap.parse_args()

    cliente = args.cliente.strip()
    if not cliente:
        _err("cliente não pode ser vazio")

    prefixo = parse_corretora_arg(args.corretora)
    start_dt = _parse_data_hora(args.data, args.hora, args.allow_past)
    end_dt = start_dt + timedelta(minutes=args.duration)

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        _err("DATABASE_URL não setado no ambiente (.env)")
    if dsn.startswith("postgresql+psycopg://"):
        dsn = dsn.replace("postgresql+psycopg://", "postgresql://", 1)

    # Resolve corretora ANTES de mexer no Gateway (falha barata primeiro).
    with psycopg.connect(dsn) as conn:
        cid, nome_db = _resolve_corretora(conn, prefixo)

    # Detecção de re-visita: mesma corretora + (nome similar OU telefone) nos
    # últimos 90 dias, ignorando canceladas.
    anterior = None
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, lead_nome, data_hora, status
                FROM visitas
                WHERE corretora_id = %s
                  AND data_hora >= NOW() - INTERVAL '90 days'
                  AND data_hora < %s
                  AND status <> 'cancelada'
                  AND (
                    lower(lead_nome) = lower(%s::text)
                    OR (telefone_lead IS NOT NULL AND %s::text IS NOT NULL
                        AND regexp_replace(telefone_lead, '\\D', '', 'g')
                          = regexp_replace(%s::text, '\\D', '', 'g'))
                  )
                ORDER BY data_hora DESC
                LIMIT 1
                """,
                (cid, start_dt, cliente, args.telefone, args.telefone or ""),
            )
            row = cur.fetchone()
            if row:
                anterior = {
                    "visita_id": row[0],
                    "lead_nome": row[1],
                    "data_hora": row[2].isoformat(),
                    "status": row[3],
                }

    if args.check_revisita_only:
        print(json.dumps({
            "status": "ok",
            "is_revisita_detectada": anterior is not None,
            "anterior": anterior,
            "corretora_id": cid,
            "corretora_nome": nome_db,
        }, ensure_ascii=False))
        return

    is_revisita = args.is_revisita or (anterior is not None)

    # Resolve lead_id (cruzamento com Inspector). Explícito via --lead-id ganha;
    # senão tenta right-8 do telefone / nome exato único (ambíguo → NULL).
    # O telefone é resolvido SEMPRE, fora do `if not lead_id` — quem chama com
    # --lead-id (o painel, quando a corretora escolhe o lead na busca) também
    # precisa dele gravado. Enquanto estava dentro do if, esse caminho estourava
    # UnboundLocalError no INSERT: evento nascia no Calendar e nada ia pro
    # Postgres (Pessoa11, 17/08 — 5 tentativas, 5 eventos órfãos).
    lead_id = args.lead_id
    with psycopg.connect(dsn) as conn:
        # Telefone primeiro: ele é o que liga a visita ao lead, e sem ele o
        # funil não fecha (ver _resolve_telefone).
        tel_final = _resolve_telefone(conn, args.telefone, cliente)
        if not lead_id:
            lead_id = _resolve_lead_id(conn, tel_final, cliente)

    summary = f"[{prefixo}] {cliente}"
    description_parts = []
    if args.telefone:
        description_parts.append(f"telefone: {args.telefone}")
    if args.bairro:
        description_parts.append(f"bairro: {args.bairro}")
    if args.obs:
        description_parts.append(f"obs: {args.obs.strip()}")
    body = {
        "summary": summary,
        "start": {"dateTime": start_dt.isoformat(), "timeZone": "America/Sao_Paulo"},
        "end":   {"dateTime": end_dt.isoformat(),   "timeZone": "America/Sao_Paulo"},
        "colorId": COLOR_PLANNED,
    }
    if description_parts:
        body["description"] = "\n".join(description_parts)

    # 1) Cria no Gateway
    try:
        resp = calendar_request("POST", cal_path(), body=body)
    except Exception as e:
        _err(f"falha ao criar evento no Calendar (Gateway): {e}")

    event_id = resp.get("id")
    if not event_id:
        _err(f"Gateway não retornou evento_id: {resp}")

    # 2) INSERT Postgres (Gateway já confirmou)
    #    ON CONFLICT DO NOTHING contra o índice único parcial anti-duplicata
    #    (uq_visitas_corretora_datahora_leadnome — mesma corretora+data_hora+nome,
    #    ignorando canceladas). Se colidir, RETURNING vem vazio → dedup.
    dedup = False
    try:
        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO visitas
                      (corretora_id, evento_calendar_id, lead_nome, data_hora,
                       bairro, telefone_lead, status, is_revisita, lead_id, origem,
                       criado_por_telefone, criado_em, atualizado_em)
                    VALUES (%s, %s, %s, %s, %s, %s, 'agendada', %s, %s, %s, %s, NOW(), NOW())
                    ON CONFLICT (corretora_id, data_hora, lower(lead_nome))
                      WHERE status <> 'cancelada'
                      DO NOTHING
                    RETURNING id, criado_em
                    """,
                    (cid, event_id, cliente, start_dt, args.bairro, tel_final,
                     is_revisita, lead_id, args.origem, args.registrado_por_telefone),
                )
                row = cur.fetchone()
            conn.commit()
        if row is None:
            dedup = True
            visita_id, criado_em = None, None
        else:
            visita_id, criado_em = row
    except Exception as e:
        sys.stderr.write(
            f"WARN: evento criado no Calendar mas falhou ao gravar Postgres. "
            f"evento_calendar_id={event_id}. Avise admin. erro={e}\n"
        )
        out = {
            "status": "warning",
            "warning": "calendar_ok_postgres_failed",
            "evento_calendar_id": event_id,
            "html_link": resp.get("htmlLink"),
            "error": str(e),
        }
        print(json.dumps(out, ensure_ascii=False))
        sys.exit(2)

    if dedup:
        # Já existe visita viva pra essa corretora+data_hora+nome. Não gravou de
        # novo (ON CONFLICT DO NOTHING). O evento do Calendar acabou de ser criado
        # e vira órfão — reportado pra revisão manual (não deletamos aqui).
        sys.stderr.write(
            f"INFO: visita duplicada (corretora={nome_db}, {cliente}, "
            f"{start_dt.isoformat()}) — não gravou. evento_calendar_id órfão={event_id}\n"
        )
        out = {
            "status": "duplicada",
            "dedup": True,
            "evento_calendar_id": event_id,
            "html_link": resp.get("htmlLink"),
            "corretora_id": cid,
            "corretora_nome": nome_db,
            "valores": {
                "lead_nome": cliente,
                "data_hora": start_dt.isoformat(),
                "origem": args.origem,
            },
        }
        print(json.dumps(out, ensure_ascii=False))
        return

    out = {
        "status": "ok",
        "dedup": False,
        "visita_id": visita_id,
        "evento_calendar_id": event_id,
        "html_link": resp.get("htmlLink"),
        "corretora_id": cid,
        "corretora_nome": nome_db,
        "lead_id": lead_id,
        "origem": args.origem,
        "valores_gravados": {
            "lead_nome": cliente,
            "data_hora": start_dt.isoformat(),
            "telefone_lead": args.telefone,
            "bairro": args.bairro,
            "status": "agendada",
            "is_revisita": is_revisita,
            "lead_id": lead_id,
            "origem": args.origem,
        },
        "is_revisita": is_revisita,
        "anterior": anterior,
        "criado_em": criado_em.isoformat(),
    }
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
