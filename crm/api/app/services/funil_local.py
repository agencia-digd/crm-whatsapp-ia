"""Agregação do funil a partir do Postgres LOCAL (painel V2), por janela de datas.

Por que existe: em 27/07/2026 o cutover desligou a criação de lead no Kommo e o
painel V2 virou o dono do lead. O `scripts/sync_vendas_mensais_local.py` já
tinha reescrito o fechamento MENSAL em cima da tabela `leads` — mas o painel,
quando o período é parcial (o caso mais comum: "mês atual" até hoje), descartava
o `vendas_mensais` e recalculava `leads`/`atendidos` a partir de
`leads_snapshot.criado_em_kommo`, que é cache do Kommo e congelou em 29/07.
Efeito: /visao mostrava o funil de agosto inteiro ZERADO — e, como leads=0 com
spend>0, ainda acendia o alerta falso "investiu R$X sem nenhum lead".

Este módulo é a fonte única: o sync mensal e o painel chamam a mesma agregação,
só mudando a janela. Se as definições mudarem, mudam nos dois de uma vez.

Definições (as mesmas do sync mensal):

  leads      — leads criados na janela, por corretora responsável (owner_id)
  atendidos  — leads que RESPONDERAM (conversa nos dois sentidos). Etapa não
               serve de proxy: no V2 ninguém move o card na mão.
  agendados  — chegou em "Visita marcada" ou além (etapas 9..12)
  visitas    — chegou em "Compareceu" ou além (etapas 10..12)
  vendas/vgv — tabela `vendas` pela `data_venda` na janela (registro humano
                do negócio). NÃO é a etapa 12 do card: a venda leva 60-90
                dias pra fechar e cairia no mês errado, `leads.valor` vive
                NULL, e um negócio pode ocupar 2 cards (irmãos, casal).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

# Só conta lead de CAPTAÇÃO. A tabela `leads` também guarda base importada
# (kommo_remarketing_4990 tem 5.248 linhas de um backfill de 26/07, nutricao_kommo
# tem 558) — contar isso como lead do período infla o funil em duas ordens de
# grandeza. `ig`/`fb` são a nomenclatura antiga do Kommo, `meta_leadgen` a do V2.
ORIGENS_CAPTACAO = (
    "meta_leadgen", "ig", "fb", "Meta Ads · Instagram",
    "site", "site_4990", "prospeccao",
)

STAGE_AGENDADO = (9, 10, 11, 12)   # Visita marcada em diante
STAGE_VISITOU = (10, 11, 12)       # Compareceu em diante
STAGE_GANHA = 12                   # Venda ganha

# Agrega tudo num round-trip só.
# O casamento lead <-> conversa é pelos 8 últimos dígitos do telefone, mesma
# heurística que o painel usa (DDI/DDD/nono dígito variam entre as fontes).
SQL_AGREGA = """
WITH janela AS (
    SELECT CAST(:ini AS timestamptz) AS ini, CAST(:fim AS timestamptz) AS fim
),
lead_mes AS (
    SELECT l.id, l.owner_id, l.stage_id, l.valor, l.created_at,
           right(regexp_replace(coalesce(l.telefone, ''), '\\D', '', 'g'), 8) AS tel8
    FROM leads l, janela j
    WHERE l.tenant_id = 1
      AND l.owner_id IS NOT NULL
      AND l.created_at >= j.ini
      AND l.created_at <  j.fim
      AND (NOT :so_captacao OR l.origem = ANY(:origens))
),
-- Materializa as respostas de entrada UMA vez por janela (varre cada tabela de
-- mensagem só uma vez) em vez de um EXISTS correlacionado por lead: o
-- regexp_replace no telefone não é indexável, então o correlacionado vira
-- seq scan por lead e a agregação leva minutos.
entrada AS MATERIALIZED (
    SELECT right(regexp_replace(m.contato_telefone, '\\D', '', 'g'), 8) AS tel8,
           min(m.timestamp_evolution) AS primeira
    FROM mensagens_whatsapp m, janela j
    WHERE NOT m.from_corretora AND m.timestamp_evolution >= j.ini
    GROUP BY 1
    UNION ALL
    SELECT right(regexp_replace(c.phone, '\\D', '', 'g'), 8),
           min(c.created_at)
    FROM conversations c, janela j
    WHERE c.message_role = 'user' AND c.created_at >= j.ini
    GROUP BY 1
),
respondeu AS (
    SELECT DISTINCT lm.id
    FROM lead_mes lm
    JOIN entrada e ON e.tel8 = lm.tel8 AND e.primeira >= lm.created_at
    WHERE length(lm.tel8) = 8
),
funil AS (
    SELECT lm.owner_id,
           count(*)                                              AS qtde_leads,
           count(*) FILTER (WHERE r.id IS NOT NULL)              AS qtde_atendidos,
           count(*) FILTER (WHERE lm.stage_id = ANY(:st_agend))  AS qtde_agendados,
           count(*) FILTER (WHERE lm.stage_id = ANY(:st_visita)) AS qtde_visitas
    FROM lead_mes lm
    LEFT JOIN respondeu r ON r.id = lm.id
    GROUP BY lm.owner_id
),
-- Venda NÃO sai de `lead_mes`. O ciclo lead->venda da Acme é de 60-90 dias,
-- então a venda quase nunca cai no mês em que o lead nasceu: contar aqui dentro
-- zerava agosto/2026 inteiro (venda real de leads criados em março). Some-se
-- que `leads.valor` fica NULL na maioria dos cards ganhos e que o filtro de
-- captação descartaria lead de indicação/origem vazia — o dinheiro sumia por
-- três caminhos diferentes.
--
-- `vendas` é o registro humano do negócio: tem data, corretora e valor, e uma
-- linha por VENDA (não por card — João e Sarah são 2 cards e 1 negócio só).
-- A janela chega em timestamptz e `data_venda` é DATE: converte pro fuso do
-- negócio antes de comparar, senão 31/08 escapa pra setembro.
vendas_janela AS (
    SELECT v.corretora_id                    AS owner_id,
           count(*)                          AS qtde_vendas,
           coalesce(sum(v.valor), 0)         AS vgv
    FROM vendas v, janela j
    WHERE v.tenant_id = 1
      AND v.corretora_id IS NOT NULL
      AND v.data_venda >= (j.ini AT TIME ZONE 'America/Sao_Paulo')::date
      AND v.data_venda <  (j.fim AT TIME ZONE 'America/Sao_Paulo')::date
    GROUP BY v.corretora_id
)
-- FULL OUTER: quem vendeu sem lead novo no mês tem que aparecer (e quem só teve
-- lead, sem venda, também) — com INNER a linha inteira sumia do resultado.
SELECT coalesce(f.owner_id, vj.owner_id)  AS owner_id,
       coalesce(f.qtde_leads, 0)          AS qtde_leads,
       coalesce(f.qtde_atendidos, 0)      AS qtde_atendidos,
       coalesce(f.qtde_agendados, 0)      AS qtde_agendados,
       coalesce(f.qtde_visitas, 0)        AS qtde_visitas,
       coalesce(vj.qtde_vendas, 0)        AS qtde_vendas,
       coalesce(vj.vgv, 0)                AS vgv
FROM funil f
FULL OUTER JOIN vendas_janela vj ON vj.owner_id = f.owner_id
"""


def agregar_janela(s: Session, ini: datetime, fim: datetime,
                   so_captacao: bool = True) -> list[dict[str, Any]]:
    """Funil por corretora na janela [ini, fim). Uma linha por `owner_id`.

    `fim` é EXCLUSIVO — pra incluir o dia de hoje, passe amanhã 00:00.
    """
    return [
        dict(r)
        for r in s.execute(sql_text(SQL_AGREGA), {
            "ini": ini, "fim": fim,
            "st_agend": list(STAGE_AGENDADO),
            "st_visita": list(STAGE_VISITOU),
            "st_ganha": STAGE_GANHA,
            "so_captacao": so_captacao,
            "origens": list(ORIGENS_CAPTACAO),
        }).mappings().all()
    ]
