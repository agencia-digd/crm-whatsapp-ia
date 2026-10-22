"""create cadencia_estado / cadencia_eventos — motor de cadência híbrida (P2)

Motor de cadência automática (state machine de 8 toques híbridos + remarketing).
Tenant-aware desde o dia 1 (compartilha o contrato multi-tenant da Opção A).

    cadencia_estado(id, tenant_id, lead_id, telefone, corretora_id,
                    toque_atual, proximo_toque_em, status, criado_em, atualizado_em)
    cadencia_eventos(id, tenant_id, cadencia_id, toque, tipo, canal, payload, at)

`cadencia_estado` é 1 linha por lead em cadência. `cadencia_eventos` é o log
append-only do que o tick decidiu/enfileirou (mensagem enfileirada, tarefa de
ligação gerada, opt-out, etc.) — é a fonte de idempotência (não re-enfileira o
mesmo toque duas vezes).

Índices: (tenant_id, status, proximo_toque_em) pro tick varrer o que é devido;
UNIQUE(tenant_id, cadencia_id, toque) pra garantir 1 evento por toque (idempotência).

Revision ID: 0044
Revises: 0043
Create Date: 2026-07-10
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "0044"
down_revision: Union[str, None] = "0043"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "cadencia_estado",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("lead_id", sa.Text(), nullable=True),
        sa.Column("telefone", sa.Text(), nullable=False),
        sa.Column("corretora_id", sa.Integer(), nullable=True),
        sa.Column("toque_atual", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("proximo_toque_em", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'ativa'")),
        sa.Column(
            "criado_em",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "atualizado_em",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "tenant_id", "telefone", name="uq_cadencia_estado_tenant_telefone"
        ),
    )
    op.create_index(
        "ix_cadencia_estado_devido",
        "cadencia_estado",
        ["tenant_id", "status", "proximo_toque_em"],
    )

    op.create_table(
        "cadencia_eventos",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column(
            "cadencia_id",
            sa.BigInteger(),
            sa.ForeignKey("cadencia_estado.id"),
            nullable=False,
        ),
        sa.Column("toque", sa.Integer(), nullable=True),
        sa.Column("tipo", sa.Text(), nullable=True),
        sa.Column("canal", sa.Text(), nullable=True),
        sa.Column("payload", JSONB(), nullable=True),
        sa.Column(
            "at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "tenant_id", "cadencia_id", "toque", name="uq_cadencia_eventos_toque"
        ),
    )


def downgrade() -> None:
    op.drop_table("cadencia_eventos")
    op.drop_index("ix_cadencia_estado_devido", table_name="cadencia_estado")
    op.drop_table("cadencia_estado")
