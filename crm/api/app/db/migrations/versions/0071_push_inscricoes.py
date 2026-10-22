"""push_inscricoes: aparelho da corretora que recebe aviso de lead novo

Cada corretora pode ter mais de um aparelho (celular + desktop), por isso a
chave é o `endpoint` da inscrição Web Push, não a corretora. O endpoint é a
URL única que o navegador dá pra cada instalação do app — repetir o mesmo
endpoint é o mesmo aparelho reinscrevendo, então UNIQUE nele e upsert.

`ativa` em vez de DELETE: quando o push volta 404/410 (pessoa desinstalou o
app ou limpou o site), a gente desliga e guarda o motivo. Apagar a linha
esconderia justamente o dado que explica por que a corretora parou de receber
aviso — e essa é a pergunta que aparece depois de uma semana sem lead
atendido.

Revision ID: 0071
Revises: 0070
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0071"
down_revision = "0070"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "push_inscricoes",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("tenant_id", sa.Integer, nullable=False, server_default=sa.text("1")),
        sa.Column("corretora_id", sa.Integer, nullable=True),
        sa.Column("endpoint", sa.Text, nullable=False),
        sa.Column("p256dh", sa.Text, nullable=False),
        sa.Column("auth", sa.Text, nullable=False),
        # Só pra diagnóstico ("ela instalou no iPhone ou no desktop?").
        sa.Column("user_agent", sa.Text, nullable=True),
        sa.Column("ativa", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("desativada_motivo", sa.Text, nullable=True),
        sa.Column(
            "criada_em",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("ultimo_envio_em", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("endpoint", name="uq_push_inscricoes_endpoint"),
    )
    op.create_index(
        "ix_push_inscricoes_corretora",
        "push_inscricoes",
        ["tenant_id", "corretora_id", "ativa"],
    )


def downgrade() -> None:
    op.drop_index("ix_push_inscricoes_corretora", table_name="push_inscricoes")
    op.drop_table("push_inscricoes")
