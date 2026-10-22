"""coaching_observations: categorias do playbook de fechamento (Pessoa1)

Adiciona as 6 categorias de movimento (movimento_*) ao CHECK constraint de
coaching_observations.categoria, sem remover as genéricas do método Pessoa.

Fonte única das categorias: app/persona/playbook_corretora_b.py (MOVIMENTOS).

Revision ID: 0055
Revises: 0054
Create Date: 2026-07-24
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0055"
down_revision: Union[str, None] = "0054"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Genéricas (estado pós-0016) + movimentos do playbook Pessoa1.
CATEGORIAS_GENERICAS = (
    "falta_qualificacao",
    "foco_valor_cedo",
    "resposta_seca",
    "perdeu_deixa",
    "demora_resposta",
    "manual_followup_apos_robo",
    "outro",
)

CATEGORIAS_MOVIMENTO = (
    "movimento_sem_convite_visita",
    "movimento_visita_sem_horario",
    "movimento_sem_tira_friccao",
    "movimento_sem_decisor",
    "movimento_sem_qualificar_dinheiro",
    "movimento_sem_escassez",
    "movimento_followup_robotico",
)

CATEGORIAS_NOVAS = CATEGORIAS_GENERICAS + CATEGORIAS_MOVIMENTO


def _in_clause(cats: tuple[str, ...]) -> str:
    return "categoria IN ('" + "','".join(cats) + "')"


def upgrade() -> None:
    op.drop_constraint(
        "coaching_obs_categoria_chk",
        "coaching_observations",
        type_="check",
    )
    op.create_check_constraint(
        "coaching_obs_categoria_chk",
        "coaching_observations",
        _in_clause(CATEGORIAS_NOVAS),
    )


def downgrade() -> None:
    # Reverter pro estado pós-0016 (só genéricas).
    # Observações de movimento_* viram 'outro' pra não violar o CHECK antigo.
    bind = op.get_bind()
    from sqlalchemy import text as _sql

    bind.execute(
        _sql(
            "UPDATE coaching_observations SET categoria = 'outro' "
            "WHERE categoria IN ("
            + ",".join(f"'{c}'" for c in CATEGORIAS_MOVIMENTO)
            + ")"
        )
    )
    op.drop_constraint(
        "coaching_obs_categoria_chk",
        "coaching_observations",
        type_="check",
    )
    op.create_check_constraint(
        "coaching_obs_categoria_chk",
        "coaching_observations",
        _in_clause(CATEGORIAS_GENERICAS),
    )
