"""Atividade do time na Visão: mensagens e ligações por corretora.

POR QUE EXISTE (06/10/2026): a Dona pediu na Visão "aquele card de quantas
mensagens as meninas fizeram, aquele que manda no copiloto do Telegram: quantas
mensagens enviaram pra quantas pessoas. Numa visão tipo 7 dias, um mês, quanto
ao todo. A mesma coisa de ligação: quantas ligações cada uma fez, quantas
atenderam." A Visão só tinha o funil.

MENSAGENS — a MESMA conta do `/painel/conversas/por-corretora`, que é o que a
Assistente responde no Telegram (tool `acme_conversas`). Há teste que compara as
duas. Definições:

  escritas por gente  saída da linha (`from_corretora`) que não é template e não
                      tem `conteudo_metadata.origem` (carimbo de robô). Áudio,
                      foto, texto do celular e do painel entram; reação, edição
                      e mensagem apagada também (são eventos da pessoa).
  automáticas         saída que é template OU tem origem: disparo do carrossel,
                      régua, botão Reengajar (é template), Pessoa, pós-ligação,
                      aviso de transferência. Sai pela linha dela, mas não é ela.
  pessoas             contatos distintos pelo sufixo-8 do telefone. No total do
                      time a pessoa conta UMA vez, mesmo que duas falem com ela.

  Fica fora: mensagem sem corretora (instância `cloud_assistente`) e contato sem
  telefone — igual ao Telegram.

  O resumo diário da Assistente (Telegram das 20h, `snapshots_corretora`) usa a
  mesma regra de "escrita por gente" desde 09/10/2026 (inspector v3), e o robô
  vai numa linha 🤖 à parte. Os números ainda não batem um a um com estes: lá
  só conta conversa em que a corretora escreveu no dia, tira internos_acme e
  corta na hora em que o Inspector roda (~20h50), não à meia-noite.

LIGAÇÕES — de `ligacao_resultado`, uma linha por ligação registrada com
resultado (o UNIQUE é por tentativa: ligar de novo pra quem não atendeu é outra
linha). Conta pelo `criado_em` no fuso de SP.

  feitas         registros no período
  atendidas      `atendeu`; `quiseram_continuar` é a parte que quis seguir
  não atendidas  `not atendeu`. "Não atendeu" volta pra fila em 24 h, então a
                 mesma pessoa pode somar várias; `pessoas` mostra quantas foram.
                 Até 01/10 não havia botão de número errado e a corretora usava
                 "não atendeu" com a observação "número incorreto" — esses estão
                 aqui dentro e vêm contados à parte (`numero_incorreto_na_obs`);
                 nem todo "incorreto" é morto (a Wagna atendeu a Corretor depois).
  À parte, sem entrar em "feitas": "Venda perdida" e "Número errado" da tela de
  ligações não gravam `ligacao_resultado` (decisão de 01/10, pra não afundar a
  taxa de atendimento com cadastro furado); ficam em `lead_events`.
"""
from __future__ import annotations

import threading
import time
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

SP_TZ = ZoneInfo("America/Sao_Paulo")
TENANT_ID = 1

# De onde veio a mensagem automática (`conteudo_metadata.origem`). Template sem
# origem é disparo/régua/Reengajar — o banco não diz qual dos três.
ROTULO_ORIGEM = {
    "template": "Templates (disparo, régua, Reengajar)",
    "pessoa": "Pessoa (IA)",
    "pos_ligacao": "Depois da ligação",
    "handoff_remarketing": "Aviso de transferência",
    "reenvio_pagamento": "Reenvio de template",
    "anti_ghosting": "Anti-sumiço",
    "carrossel": "Carrossel",
    "cadencia": "Régua",
    "teste_billing": "Teste de cobrança",
}

SQL_MENSAGENS = """
WITH msgs AS (
    SELECT m.corretora_id,
           m.from_corretora,
           right(regexp_replace(coalesce(m.contato_telefone,''), '\\D', '', 'g'), 8) AS p8,
           (m.from_corretora
            AND coalesce(m.tipo_mensagem,'') <> 'template'
            AND coalesce(m.conteudo_metadata->>'origem','') = '') AS humana
      FROM mensagens_whatsapp m
     WHERE m.tenant_id = :t
       AND m.contato_telefone IS NOT NULL
       AND m.corretora_id IS NOT NULL
       AND m.timestamp_evolution >= :ini AND m.timestamp_evolution < :fim
)
SELECT corretora_id,
       GROUPING(corretora_id)                                   AS eh_total,
       count(*) FILTER (WHERE humana)                           AS escritas,
       count(DISTINCT p8) FILTER (WHERE humana)                 AS pessoas,
       count(*) FILTER (WHERE from_corretora AND NOT humana)    AS automaticas,
       count(DISTINCT p8) FILTER (WHERE from_corretora AND NOT humana) AS pessoas_automaticas,
       count(*) FILTER (WHERE NOT from_corretora)               AS recebidas,
       count(DISTINCT p8) FILTER (WHERE NOT from_corretora)     AS pessoas_que_falaram
  FROM msgs
 GROUP BY GROUPING SETS ((corretora_id), ())
"""

SQL_AUTOMATICAS_POR_ORIGEM = """
SELECT m.corretora_id,
       CASE WHEN coalesce(m.conteudo_metadata->>'origem','') <> ''
            THEN m.conteudo_metadata->>'origem' ELSE 'template' END AS origem,
       count(*) AS n
  FROM mensagens_whatsapp m
 WHERE m.tenant_id = :t
   AND m.contato_telefone IS NOT NULL
   AND m.corretora_id IS NOT NULL
   AND m.from_corretora
   AND (coalesce(m.tipo_mensagem,'') = 'template'
        OR coalesce(m.conteudo_metadata->>'origem','') <> '')
   AND m.timestamp_evolution >= :ini AND m.timestamp_evolution < :fim
 GROUP BY 1, 2
"""

SQL_LIGACOES = """
SELECT corretora_id,
       GROUPING(corretora_id)                              AS eh_total,
       count(*)                                            AS feitas,
       count(*) FILTER (WHERE atendeu)                     AS atendidas,
       count(*) FILTER (WHERE NOT atendeu)                 AS nao_atendidas,
       count(*) FILTER (WHERE quis_continuar IS TRUE)      AS quiseram_continuar,
       count(*) FILTER (WHERE NOT atendeu
                          AND observacao ~* 'incorret|errad') AS numero_incorreto_na_obs,
       count(DISTINCT lead_id)                             AS pessoas
  FROM ligacao_resultado
 WHERE tenant_id = :t
   AND criado_em >= :ini AND criado_em < :fim
 GROUP BY GROUPING SETS ((corretora_id), ())
"""

SQL_LIGACAO_FORA = """
SELECT (payload->>'corretora_id')::int AS corretora_id, tipo, count(*) AS n
  FROM lead_events
 WHERE tenant_id = :t
   AND tipo IN ('ligacao_venda_perdida', 'ligacao_telefone_errado')
   AND at >= :ini AND at < :fim
 GROUP BY 1, 2
"""

SQL_CORRETORAS = "SELECT id FROM corretoras WHERE ativo ORDER BY id"


def janela(di: date, df: date) -> tuple[datetime, datetime]:
    """[di 00:00 SP, df+1 00:00 SP) — o dia inteiro de df entra."""
    ini = datetime.combine(di, datetime.min.time(), tzinfo=SP_TZ)
    fim = datetime.combine(df + timedelta(days=1), datetime.min.time(), tzinfo=SP_TZ)
    return ini, fim


def _taxa(num: int, den: int) -> float | None:
    """Percentual com uma casa. Sem base é None (nunca 0%)."""
    return round(num / den * 100, 1) if den else None


def _linha_msg(r: Any | None) -> dict[str, int]:
    campos = ("escritas", "pessoas", "automaticas", "pessoas_automaticas",
              "recebidas", "pessoas_que_falaram")
    return {c: int((r[c] if r is not None else 0) or 0) for c in campos}


def _linha_lig(r: Any | None) -> dict[str, Any]:
    campos = ("feitas", "atendidas", "nao_atendidas", "quiseram_continuar",
              "numero_incorreto_na_obs", "pessoas")
    d: dict[str, Any] = {c: int((r[c] if r is not None else 0) or 0) for c in campos}
    d["taxa_atendimento"] = _taxa(d["atendidas"], d["feitas"])
    d["venda_perdida"] = 0
    d["numero_errado"] = 0
    return d


def calcular_atividade(
    s: Session,
    di: date,
    df: date,
    *,
    corretora_label: dict[int, str] | None = None,
    so_corretora: int | None = None,
) -> dict[str, Any]:
    """Mensagens e ligações por corretora e no total, na janela [di, df].

    `so_corretora`: viewer corretora — devolve só a linha dela, e o "total" é o
    dela (nunca o do time).
    """
    labels = corretora_label or {}
    ini, fim = janela(di, df)
    p = {"t": TENANT_ID, "ini": ini, "fim": fim}

    msg_rows = s.execute(sql_text(SQL_MENSAGENS), p).mappings().all()
    lig_rows = s.execute(sql_text(SQL_LIGACOES), p).mappings().all()
    orig_rows = s.execute(sql_text(SQL_AUTOMATICAS_POR_ORIGEM), p).mappings().all()
    fora_rows = s.execute(sql_text(SQL_LIGACAO_FORA), p).mappings().all()
    ativas = [int(r[0]) for r in s.execute(sql_text(SQL_CORRETORAS)).all()]

    msg_por = {int(r["corretora_id"]): r for r in msg_rows if not r["eh_total"]}
    msg_total = next((r for r in msg_rows if r["eh_total"]), None)
    lig_por = {r["corretora_id"]: r for r in lig_rows if not r["eh_total"]}
    lig_total = next((r for r in lig_rows if r["eh_total"]), None)

    # Toda corretora ativa aparece (zero também é resposta), mais quem teve
    # atividade e já saiu do cadastro.
    ids = sorted(set(ativas) | set(msg_por) | {c for c in lig_por if c is not None})

    origens_por: dict[int, dict[str, int]] = {}
    for r in orig_rows:
        origens_por.setdefault(int(r["corretora_id"]), {})[str(r["origem"])] = int(r["n"])
    fora_por: dict[int | None, dict[str, int]] = {}
    for r in fora_rows:
        fora_por.setdefault(r["corretora_id"], {})[str(r["tipo"])] = int(r["n"])

    def _origens(cnt: dict[str, int]) -> list[dict[str, Any]]:
        return [
            {"origem": o, "rotulo": ROTULO_ORIGEM.get(o, o), "mensagens": n}
            for o, n in sorted(cnt.items(), key=lambda x: (-x[1], x[0]))
        ]

    def _aplica_fora(lig: dict[str, Any], cnt: dict[str, int]) -> None:
        lig["venda_perdida"] += cnt.get("ligacao_venda_perdida", 0)
        lig["numero_errado"] += cnt.get("ligacao_telefone_errado", 0)

    por_corretora: list[dict[str, Any]] = []
    for cid in ids:
        if so_corretora is not None and cid != so_corretora:
            continue
        lig = _linha_lig(lig_por.get(cid))
        _aplica_fora(lig, fora_por.get(cid, {}))
        por_corretora.append({
            "corretora_id": cid,
            "label": labels.get(cid, f"C{cid}"),
            "mensagens": {**_linha_msg(msg_por.get(cid)),
                          "automaticas_por_origem": _origens(origens_por.get(cid, {}))},
            "ligacoes": lig,
        })

    if so_corretora is not None:
        mine = por_corretora[0] if por_corretora else None
        total_msg = dict(mine["mensagens"]) if mine else {
            **_linha_msg(None), "automaticas_por_origem": []}
        total_lig = dict(mine["ligacoes"]) if mine else _linha_lig(None)
    else:
        soma: dict[str, int] = {}
        for cnt in origens_por.values():
            for o, n in cnt.items():
                soma[o] = soma.get(o, 0) + n
        total_msg = {**_linha_msg(msg_total), "automaticas_por_origem": _origens(soma)}
        total_lig = _linha_lig(lig_total)
        for cnt in fora_por.values():
            _aplica_fora(total_lig, cnt)

    return {
        "inicio": di.isoformat(),
        "fim": df.isoformat(),
        "total": {"mensagens": total_msg, "ligacoes": total_lig},
        "por_corretora": por_corretora,
    }


# ── Cache curto ───────────────────────────────────────────────────────────────
# A Visão recarrega a cada troca de período; 60 s segura clique repetido sem
# deixar número velho na tela por muito tempo.
CACHE_TTL_S = 60.0
_cache: dict[tuple[Any, ...], tuple[float, dict[str, Any]]] = {}
_cache_lock = threading.Lock()


def com_cache(chave: tuple[Any, ...], calcular: Any,
              agora: float | None = None) -> dict[str, Any]:
    t = time.monotonic() if agora is None else agora
    with _cache_lock:
        hit = _cache.get(chave)
        if hit and t - hit[0] < CACHE_TTL_S:
            return hit[1]
    valor = calcular()
    with _cache_lock:
        _cache[chave] = (t, valor)
    return valor


def limpar_cache() -> None:
    with _cache_lock:
        _cache.clear()
