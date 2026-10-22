"""
calcular_metricas.py — Inspector: cálculo diário de métricas WhatsApp.

Movido de fetch_whatsapp_metrics() em agente_assistente.py para cá (Onda 7.5).
Responsabilidade: ler mensagens_whatsapp, calcular métricas por corretora
e fazer UPSERT idempotente em snapshots_diarios + snapshots_corretora.

Timezone: America/Sao_Paulo para cálculo de data. Timestamps em UTC no banco.
Idempotente via ON CONFLICT DO UPDATE em ambas as tabelas.

Dois recortes por corretora (pedido Dona 29/07/2026):
  - total: qualquer 1:1 na linha (exceto grupo e internos_acme)
  - crm: só contatos cujo telefone bate em leads_snapshot
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import text

from app.db.session import SessionLocal

log = structlog.get_logger()

SP_TZ = ZoneInfo("America/Sao_Paulo")

INSPECTOR_VERSAO = "v3-envio-de-gente"

# Mapeamento instance → nome (cache local — fonte de verdade é a tabela corretoras)
_INSTANCE_NOME_CACHE: dict[str, str] = {}


def _get_instance_nome_map() -> dict[str, str]:
    """Lê mapeamento instance→nome da tabela corretoras. Cacheia em memória."""
    if _INSTANCE_NOME_CACHE:
        return _INSTANCE_NOME_CACHE
    with SessionLocal() as session:
        rows = session.execute(
            text(
                "SELECT evolution_instance, nome FROM corretoras "
                "WHERE ativo = true AND evolution_instance IS NOT NULL"
            )
        ).fetchall()
    for r in rows:
        _INSTANCE_NOME_CACHE[r[0]] = r[1]
    return _INSTANCE_NOME_CACHE


def _empty_instance_metrics() -> dict[str, Any]:
    return {
        "envios": 0,
        "recebidos": 0,
        "contatos": 0,
        "responderam": 0,
        "taxa_resposta": 0.0,
        "envios_crm": 0,
        "recebidos_crm": 0,
        "contatos_crm": 0,
        "responderam_crm": 0,
        "taxa_resposta_crm": 0.0,
    }


def calcular_metricas_dia(
    data_alvo: date,
    conn=None,
) -> dict[str, Any]:
    """
    Calcula métricas do dia para snapshots_diarios + snapshots_corretora.

    Usa CTE única para eficiência. Filtra internos_acme e grupos.
    Separa recorte CRM (telefone em leads_snapshot) do total.
    Idempotente via UPSERT (ON CONFLICT DO UPDATE).

    Args:
        data_alvo: data no fuso SP para calcular métricas.
        conn: conexão SQLAlchemy (não usada — mantida por compatibilidade de assinatura).
              Internamente abre SessionLocal() própria.

    Returns:
        dict com keys:
          - 'diario': dict com totais agregados (total + crm)
          - 'por_corretora': list[dict] com métricas por instance
          - 'linhas_diario': int (1 se gravou, 0 se falhou)
          - 'linhas_corretora': int (nº de corretoras gravadas)
    """
    # Janela UTC para o dia SP
    dia_inicio_sp = datetime(
        data_alvo.year, data_alvo.month, data_alvo.day,
        0, 0, 0, tzinfo=SP_TZ
    )
    dia_fim_sp = dia_inicio_sp + timedelta(days=1)
    midnight_utc = dia_inicio_sp.astimezone(timezone.utc)
    tomorrow_utc = dia_fim_sp.astimezone(timezone.utc)

    instance_nome = _get_instance_nome_map()
    instances = list(instance_nome.keys())

    if not instances:
        log.warning("calcular_metricas_no_instances", data=str(data_alvo))
        return {"diario": {}, "por_corretora": [], "linhas_diario": 0, "linhas_corretora": 0}

    log.info(
        "calcular_metricas_start",
        data=str(data_alvo),
        instances=instances,
        window_utc_start=midnight_utc.isoformat(),
        window_utc_end=tomorrow_utc.isoformat(),
    )

    # ── CTE principal ──────────────────────────────────────────────────────────
    # tel11: dígitos do contato (contato_telefone se houver, senão JID).
    # no_crm: telefone bate em leads_snapshot (11 ou últimos 10 dígitos).
    query = text("""
        WITH msgs_today AS (
            SELECT
                instance,
                remote_jid,
                from_corretora,
                timestamp_evolution,
                -- Escrita por gente: saída da linha que não é template e não tem
                -- carimbo de robô (`origem`). Mesma regra da Visão
                -- (app/services/visao_atividade.py). Carrossel, régua, nutrição,
                -- Pessoa e pós-ligação saem pela linha dela, mas não são ela.
                (from_corretora
                 AND COALESCE(tipo_mensagem, '') <> 'template'
                 AND COALESCE(conteudo_metadata->>'origem', '') = '') AS humana,
                right(
                    regexp_replace(
                        COALESCE(NULLIF(contato_telefone, ''), split_part(remote_jid, '@', 1)),
                        '\\D', '', 'g'
                    ),
                    11
                ) AS tel11
            FROM mensagens_whatsapp
            WHERE timestamp_evolution >= :midnight_utc
              AND timestamp_evolution < :tomorrow_utc
              AND remote_jid NOT LIKE '%@g.us'
              AND (contato_telefone IS NULL
                   OR contato_telefone NOT IN (
                       SELECT telefone_normalizado
                       FROM internos_acme
                       WHERE ativa = true
                   ))
              AND instance = ANY(:instances)
        ),
        crm_tels AS (
            SELECT DISTINCT right(regexp_replace(telefone, '\\D', '', 'g'), 11) AS tel11
            FROM leads_snapshot
            WHERE telefone IS NOT NULL
              AND telefone <> ''
              AND length(regexp_replace(telefone, '\\D', '', 'g')) >= 10
        ),
        -- Chats em que a corretora ESCREVEU >= 1 msg hoje (disparo automático
        -- não conta como abordagem dela — vai no placar do robô do briefing)
        abordados AS (
            SELECT
                instance,
                remote_jid,
                MIN(timestamp_evolution) AS first_cor_at,
                MAX(tel11) AS tel11
            FROM msgs_today
            WHERE humana
            GROUP BY instance, remote_jid
        ),
        abordados_flag AS (
            SELECT
                a.*,
                EXISTS (
                    SELECT 1 FROM crm_tels c
                    WHERE c.tel11 = a.tel11
                       OR (
                           length(a.tel11) >= 10
                           AND length(c.tel11) >= 10
                           AND right(c.tel11, 10) = right(a.tel11, 10)
                       )
                ) AS no_crm
            FROM abordados a
        ),
        -- Todas as msgs de chats abordados (enviadas + recebidas)
        msgs_abordados AS (
            SELECT
                m.instance,
                m.remote_jid,
                m.from_corretora,
                m.humana,
                m.timestamp_evolution,
                a.no_crm
            FROM msgs_today m
            INNER JOIN abordados_flag a
                ON m.instance = a.instance AND m.remote_jid = a.remote_jid
        ),
        -- Lead respondeu APÓS a primeira msg da corretora
        lead_respondeu AS (
            SELECT DISTINCT m.instance, m.remote_jid, a.no_crm
            FROM msgs_abordados m
            JOIN abordados_flag a
                ON m.instance = a.instance AND m.remote_jid = a.remote_jid
            WHERE m.from_corretora = false
              AND m.timestamp_evolution > a.first_cor_at
        )
        SELECT
            a.instance,
            COUNT(DISTINCT a.remote_jid) AS contatos_unicos,
            COUNT(*) FILTER (WHERE m.humana) AS msgs_enviadas,
            COUNT(*) FILTER (WHERE m.from_corretora = false) AS msgs_recebidas,
            COUNT(DISTINCT lr.remote_jid) AS leads_responderam,
            COUNT(DISTINCT a.remote_jid) FILTER (WHERE a.no_crm) AS contatos_crm,
            COUNT(*) FILTER (WHERE m.humana AND a.no_crm) AS msgs_enviadas_crm,
            COUNT(*) FILTER (WHERE m.from_corretora = false AND a.no_crm) AS msgs_recebidas_crm,
            COUNT(DISTINCT lr.remote_jid) FILTER (WHERE lr.no_crm) AS leads_responderam_crm
        FROM abordados_flag a
        JOIN msgs_abordados m
            ON m.instance = a.instance AND m.remote_jid = a.remote_jid
        LEFT JOIN lead_respondeu lr
            ON lr.instance = a.instance AND lr.remote_jid = a.remote_jid
        GROUP BY a.instance
    """)

    por_corretora: list[dict[str, Any]] = []
    total_envios = 0
    total_recebidos = 0
    total_contatos = 0
    total_envios_crm = 0
    total_recebidos_crm = 0
    total_contatos_crm = 0

    with SessionLocal() as session:
        rows = session.execute(
            query,
            {
                "midnight_utc": midnight_utc,
                "tomorrow_utc": tomorrow_utc,
                "instances": instances,
            },
        ).fetchall()

        # Monta dict por instance com dados
        data_por_instance: dict[str, dict[str, Any]] = {}
        for r in rows:
            contatos = int(r.contatos_unicos or 0)
            enviadas = int(r.msgs_enviadas or 0)
            recebidas = int(r.msgs_recebidas or 0)
            responderam = int(r.leads_responderam or 0)
            taxa = round(responderam / contatos * 100, 2) if contatos > 0 else 0.0

            contatos_crm = int(r.contatos_crm or 0)
            enviadas_crm = int(r.msgs_enviadas_crm or 0)
            recebidas_crm = int(r.msgs_recebidas_crm or 0)
            responderam_crm = int(r.leads_responderam_crm or 0)
            taxa_crm = (
                round(responderam_crm / contatos_crm * 100, 2) if contatos_crm > 0 else 0.0
            )

            data_por_instance[r.instance] = {
                "envios": enviadas,
                "recebidos": recebidas,
                "contatos": contatos,
                "responderam": responderam,
                "taxa_resposta": taxa,
                "envios_crm": enviadas_crm,
                "recebidos_crm": recebidas_crm,
                "contatos_crm": contatos_crm,
                "responderam_crm": responderam_crm,
                "taxa_resposta_crm": taxa_crm,
            }
            total_envios += enviadas
            total_recebidos += recebidas
            total_contatos += contatos
            total_envios_crm += enviadas_crm
            total_recebidos_crm += recebidas_crm
            total_contatos_crm += contatos_crm

        # Garante que instâncias sem msgs do dia aparecem com zeros
        for inst in instances:
            if inst not in data_por_instance:
                data_por_instance[inst] = _empty_instance_metrics()

        # ── UPSERT snapshots_diarios ───────────────────────────────────────────
        session.execute(
            text("""
                INSERT INTO snapshots_diarios
                  (data, total_envios, total_recebidos, total_contatos, total_msgs,
                   total_envios_crm, total_recebidos_crm, total_contatos_crm,
                   gerado_em, inspector_versao)
                VALUES
                  (:data, :total_envios, :total_recebidos, :total_contatos,
                   :total_msgs, :total_envios_crm, :total_recebidos_crm,
                   :total_contatos_crm, NOW(), :versao)
                ON CONFLICT (data) DO UPDATE SET
                  total_envios         = EXCLUDED.total_envios,
                  total_recebidos      = EXCLUDED.total_recebidos,
                  total_contatos       = EXCLUDED.total_contatos,
                  total_msgs           = EXCLUDED.total_msgs,
                  total_envios_crm     = EXCLUDED.total_envios_crm,
                  total_recebidos_crm  = EXCLUDED.total_recebidos_crm,
                  total_contatos_crm   = EXCLUDED.total_contatos_crm,
                  gerado_em            = NOW(),
                  inspector_versao     = EXCLUDED.inspector_versao
            """),
            {
                "data": data_alvo,
                "total_envios": total_envios,
                "total_recebidos": total_recebidos,
                "total_contatos": total_contatos,
                "total_msgs": total_envios + total_recebidos,
                "total_envios_crm": total_envios_crm,
                "total_recebidos_crm": total_recebidos_crm,
                "total_contatos_crm": total_contatos_crm,
                "versao": INSPECTOR_VERSAO,
            },
        )

        # ── UPSERT snapshots_corretora (uma linha por instance) ────────────────
        linhas_corretora = 0
        for inst, d in data_por_instance.items():
            nome = instance_nome.get(inst, inst)
            session.execute(
                text("""
                    INSERT INTO snapshots_corretora
                      (data, instance, corretora_nome, envios, recebidos,
                       contatos, responderam, taxa_resposta,
                       envios_crm, recebidos_crm, contatos_crm,
                       responderam_crm, taxa_resposta_crm, status)
                    VALUES
                      (:data, :instance, :nome, :envios, :recebidos,
                       :contatos, :responderam, :taxa,
                       :envios_crm, :recebidos_crm, :contatos_crm,
                       :responderam_crm, :taxa_crm, :status)
                    ON CONFLICT (data, instance) DO UPDATE SET
                      corretora_nome     = EXCLUDED.corretora_nome,
                      envios             = EXCLUDED.envios,
                      recebidos          = EXCLUDED.recebidos,
                      contatos           = EXCLUDED.contatos,
                      responderam        = EXCLUDED.responderam,
                      taxa_resposta      = EXCLUDED.taxa_resposta,
                      envios_crm         = EXCLUDED.envios_crm,
                      recebidos_crm      = EXCLUDED.recebidos_crm,
                      contatos_crm       = EXCLUDED.contatos_crm,
                      responderam_crm    = EXCLUDED.responderam_crm,
                      taxa_resposta_crm  = EXCLUDED.taxa_resposta_crm,
                      status             = EXCLUDED.status
                """),
                {
                    "data": data_alvo,
                    "instance": inst,
                    "nome": nome,
                    "envios": d["envios"],
                    "recebidos": d["recebidos"],
                    "contatos": d["contatos"],
                    "responderam": d["responderam"],
                    "taxa": d["taxa_resposta"],
                    "envios_crm": d["envios_crm"],
                    "recebidos_crm": d["recebidos_crm"],
                    "contatos_crm": d["contatos_crm"],
                    "responderam_crm": d["responderam_crm"],
                    "taxa_crm": d["taxa_resposta_crm"],
                    "status": "ok",
                },
            )
            linhas_corretora += 1
            por_corretora.append({
                "instance": inst,
                "corretora_nome": nome,
                **d,
            })

        session.commit()

    log.info(
        "calcular_metricas_done",
        data=str(data_alvo),
        total_contatos=total_contatos,
        total_envios=total_envios,
        total_envios_crm=total_envios_crm,
        total_recebidos=total_recebidos,
        linhas_corretora=linhas_corretora,
    )

    return {
        "diario": {
            "data": str(data_alvo),
            "total_envios": total_envios,
            "total_recebidos": total_recebidos,
            "total_contatos": total_contatos,
            "total_msgs": total_envios + total_recebidos,
            "total_envios_crm": total_envios_crm,
            "total_recebidos_crm": total_recebidos_crm,
            "total_contatos_crm": total_contatos_crm,
        },
        "por_corretora": por_corretora,
        "linhas_diario": 1,
        "linhas_corretora": linhas_corretora,
    }
