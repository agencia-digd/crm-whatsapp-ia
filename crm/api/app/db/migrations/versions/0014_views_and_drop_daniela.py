"""create views vw_metricas_mes_corretora + vw_semana_atual_corretora;
remove Dona de internos_acme (modo_teste caducou) — Onda P1

Revision ID: 0014
Revises: 0013
Create Date: 2026-05-09
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


VW_METRICAS_MES = """
CREATE OR REPLACE VIEW vw_metricas_mes_corretora AS
SELECT
  c.id AS corretora_id,
  c.nome AS corretora_nome,
  to_char(d::date, 'YYYY-MM') AS mes,
  d::date AS mes_data,
  COALESCE((SELECT COUNT(*) FROM vendas v
            WHERE v.corretora_id = c.id
              AND to_char(v.data_venda, 'YYYY-MM') = to_char(d::date, 'YYYY-MM')
           ), 0) AS vendas_mes,
  COALESCE((SELECT SUM(valor) FROM vendas v
            WHERE v.corretora_id = c.id
              AND to_char(v.data_venda, 'YYYY-MM') = to_char(d::date, 'YYYY-MM')
           ), 0) AS vgv_mes
FROM corretoras c
CROSS JOIN generate_series(
  date_trunc('month', CURRENT_DATE - INTERVAL '6 months'),
  date_trunc('month', CURRENT_DATE),
  '1 month'
) AS d
WHERE c.ativo = true
"""

VW_SEMANA_ATUAL = """
CREATE OR REPLACE VIEW vw_semana_atual_corretora AS
SELECT
  c.id AS corretora_id,
  c.nome AS corretora_nome,
  to_char(CURRENT_DATE, 'IYYY-"W"IW') AS ano_semana_iso,
  COALESCE(l.ligacoes, 0) AS ligacoes,
  COALESCE(l.atendeu, 0) AS ligacoes_atendeu,
  COALESCE(l.prosseguiu, 0) AS ligacoes_prosseguiu,
  COALESCE(a.agendados, 0) AS agendados,
  COALESCE(a.realizados, 0) AS realizados,
  l.atualizado_em AS ligacoes_atualizado_em,
  a.atualizado_em AS agendamentos_atualizado_em
FROM corretoras c
LEFT JOIN ligacoes_semanais l
  ON l.corretora_id = c.id
  AND l.ano_semana_iso = to_char(CURRENT_DATE, 'IYYY-"W"IW')
LEFT JOIN agendamentos_semanais a
  ON a.corretora_id = c.id
  AND a.ano_semana_iso = to_char(CURRENT_DATE, 'IYYY-"W"IW')
WHERE c.ativo = true
"""


def upgrade() -> None:
    op.execute(VW_METRICAS_MES)
    op.execute(VW_SEMANA_ATUAL)
    # Dona sai de internos_acme (modo_teste caducou)
    op.execute(
        "DELETE FROM internos_acme WHERE telefone_normalizado = '+5500000000000'"
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS vw_semana_atual_corretora")
    op.execute("DROP VIEW IF EXISTS vw_metricas_mes_corretora")
    # Não restauramos Dona no downgrade (decisão de produto)
