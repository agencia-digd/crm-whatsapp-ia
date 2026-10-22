"""create mensagens_automacao + revisar categorias coaching — Onda 3a.2

Revision ID: 0016
Revises: 0015
Create Date: 2026-05-09
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: Union[str, None] = "0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


CATEGORIAS_NOVAS = (
    "falta_qualificacao",
    "foco_valor_cedo",
    "resposta_seca",
    "perdeu_deixa",
    "demora_resposta",
    "manual_followup_apos_robo",  # renomeado de ausencia_followup
    "outro",
)


def upgrade() -> None:
    # ── 1. Tabela de auditoria de automações detectadas ──────────────────────
    op.create_table(
        "mensagens_automacao",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("corretora_id", sa.BigInteger(),
                  sa.ForeignKey("corretoras.id"), nullable=False),
        sa.Column("texto_normalizado", sa.Text(), nullable=False),
        sa.Column("texto_exemplo", sa.Text(), nullable=False),
        sa.Column("ocorrencias", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("leads_distintos", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("primeira_vez", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ultima_vez", sa.DateTime(timezone=True), nullable=False),
        sa.Column("detectada_em", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.Column("ativa", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("observacao", sa.Text(), nullable=True),
        sa.UniqueConstraint("corretora_id", "texto_normalizado",
                            name="uq_mensagens_automacao_corretora_texto"),
    )
    op.execute(
        "CREATE INDEX idx_mensagens_automacao_ativas "
        "ON mensagens_automacao(corretora_id, ativa) WHERE ativa = true"
    )

    # ── 2. Migrar/limpar coaching_observations.ausencia_followup ─────────────
    # Se houver < 10 → DELETE (vai regenerar). Se ≥ 10 → UPDATE.
    bind = op.get_bind()
    n_old = bind.execute(sa.text(
        "SELECT COUNT(*) FROM coaching_observations WHERE categoria = 'ausencia_followup'"
    )).scalar() or 0
    if n_old < 10:
        bind.execute(sa.text(
            "DELETE FROM coaching_observations WHERE categoria = 'ausencia_followup'"
        ))
    else:
        bind.execute(sa.text(
            "UPDATE coaching_observations SET categoria = 'manual_followup_apos_robo' "
            "WHERE categoria = 'ausencia_followup'"
        ))

    # ── 3. Recriar CHECK constraint com categorias novas ─────────────────────
    op.drop_constraint(
        "coaching_obs_categoria_chk",
        "coaching_observations",
        type_="check",
    )
    op.create_check_constraint(
        "coaching_obs_categoria_chk",
        "coaching_observations",
        "categoria IN ('" + "','".join(CATEGORIAS_NOVAS) + "')",
    )


def downgrade() -> None:
    # Reverter CHECK pro estado antigo
    op.drop_constraint(
        "coaching_obs_categoria_chk",
        "coaching_observations",
        type_="check",
    )
    op.create_check_constraint(
        "coaching_obs_categoria_chk",
        "coaching_observations",
        "categoria IN ('falta_qualificacao','foco_valor_cedo','resposta_seca',"
        "'perdeu_deixa','demora_resposta','ausencia_followup','outro')",
    )
    op.execute(
        "UPDATE coaching_observations SET categoria='ausencia_followup' "
        "WHERE categoria='manual_followup_apos_robo'"
    )

    op.execute("DROP INDEX IF EXISTS idx_mensagens_automacao_ativas")
    op.drop_table("mensagens_automacao")
