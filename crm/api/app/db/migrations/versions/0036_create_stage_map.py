"""create stage_map — 1 fonte pro mapeamento de etapas (Kommo ↔ Twenty ↔ label)

Hoje o mapeamento etapa vive HARDCODED em dois lugares (scripts/sync_kommo_twenty.py
STAGE e a config.ts do dash), só com IDs, sem labels. Renomeação/nova etapa no Kommo
passa despercebida (REVISAO-02-FUNIL gaps F4/F7 · plano P1 item 4).

Esta tabela vira a fonte única: o sync lê o mapeamento daqui (fallback pro dict
hardcoded), o helper `app/services/stage_map.py` serve o dash e o endpoint de escrita
de etapa, e um status_id desconhecido fica visível (não está na tabela ativa).

Colunas:
  - kommo_status_id  TEXT NULL   — id do status no Kommo (NULL = stage só do Twenty,
                                   ex: RECUPERADOS/PROVISORIA que não têm par 1:1)
  - twenty_stage     TEXT        — value da coluna no Twenty (ENTRADA, NEW, ...)
  - label_pt         TEXT        — rótulo humano pro painel/kanban
  - ordem            INT         — ordem de exibição no kanban
  - ativo            BOOL        — soft-disable sem apagar histórico

Seed: os 12 stages do pipeline 10000004 (IDs do sync_kommo_twenty.py STAGE + labels PT).

Revision ID: 0036
Revises: 0035
Create Date: 2026-07-02
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0036"
down_revision: Union[str, None] = "0035"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (kommo_status_id, twenty_stage, label_pt, ordem)
# IDs: scripts/sync_kommo_twenty.py STAGE · pipeline 10000004 (REVISAO-02-FUNIL §1).
# RECUPERADOS/PROVISORIA têm status_id no Kommo (10000010/10000011); os demais idem.
# Ordem segue o fluxo do funil (entrada → ganho), com PERDIDA por último.
_SEED = [
    ("10000008", "ENTRADA", "Entrada", 1),
    ("10000009", "NEW", "Entrou", 2),
    ("10000005", "SCREENING", "Em atendimento", 3),
    ("10000001", "FOLLOWUP", "Follow-up", 4),
    ("10000010", "RECUPERADOS", "Recuperados", 5),
    ("10000011", "PROVISORIA", "Provisória", 6),
    ("10000002", "MEETING", "Visita agendada", 7),
    ("10000012", "VISITA_REAL", "Visita realizada", 8),
    ("10000013", "PROPOSAL", "Proposta", 9),
    ("10000014", "DOCUMENTACAO", "Documentação", 10),
    ("142", "CUSTOMER", "Venda", 11),
    ("143", "PERDIDA", "Perdida", 12),
]


def upgrade() -> None:
    op.create_table(
        "stage_map",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("kommo_status_id", sa.Text(), nullable=True),
        sa.Column("twenty_stage", sa.Text(), nullable=False),
        sa.Column("label_pt", sa.Text(), nullable=False),
        sa.Column("ordem", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("ativo", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.UniqueConstraint("twenty_stage", name="uq_stage_map_twenty_stage"),
    )
    # kommo_status_id é único quando presente (NULL não conflita no Postgres).
    op.create_index(
        "uq_stage_map_kommo_status_id",
        "stage_map",
        ["kommo_status_id"],
        unique=True,
        postgresql_where=sa.text("kommo_status_id IS NOT NULL"),
    )

    stage_map = sa.table(
        "stage_map",
        sa.column("kommo_status_id", sa.Text),
        sa.column("twenty_stage", sa.Text),
        sa.column("label_pt", sa.Text),
        sa.column("ordem", sa.Integer),
        sa.column("ativo", sa.Boolean),
    )
    op.bulk_insert(
        stage_map,
        [
            {
                "kommo_status_id": kid,
                "twenty_stage": tw,
                "label_pt": label,
                "ordem": ordem,
                "ativo": True,
            }
            for (kid, tw, label, ordem) in _SEED
        ],
    )


def downgrade() -> None:
    op.drop_index("uq_stage_map_kommo_status_id", table_name="stage_map")
    op.drop_table("stage_map")
