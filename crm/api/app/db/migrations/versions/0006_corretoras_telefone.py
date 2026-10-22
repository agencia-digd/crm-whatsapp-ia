"""add telefone_normalizado to corretoras + populate (Onda 2)

Revision ID: 0006
Revises: 0005
Create Date: 2026-05-09

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


CORRETORA_PHONES = {
    "Pessoa3": "+5500000000000",
    "Pessoa5": "+5500000000000",
    "Pessoa1": "+5500000000000",
}


def upgrade() -> None:
    op.add_column(
        "corretoras",
        sa.Column("telefone_normalizado", sa.Text(), nullable=True),
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_corretoras_tel "
        "ON corretoras(telefone_normalizado) "
        "WHERE telefone_normalizado IS NOT NULL"
    )

    # Sanity check + populate
    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT id, nome FROM corretoras WHERE ativo = true")
    ).fetchall()
    db_names = {r.nome for r in rows}
    # DB novo/vazio (ambiente de teste ou tenant novo): não há corretoras pra
    # popular ainda — elas são semeadas depois. Nada a validar/atualizar aqui.
    # (Antes isto dava raise e travava alembic upgrade head em qualquer DB limpo.)
    if not db_names:
        return
    expected = set(CORRETORA_PHONES.keys())
    missing = expected - db_names
    if missing:
        raise RuntimeError(
            f"Migration 0006: corretoras esperadas não encontradas: {missing}. "
            f"DB tem: {db_names}"
        )

    for nome, telefone in CORRETORA_PHONES.items():
        bind.execute(
            sa.text(
                "UPDATE corretoras SET telefone_normalizado = :tel WHERE nome = :nome"
            ),
            {"tel": telefone, "nome": nome},
        )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_corretoras_tel")
    op.drop_column("corretoras", "telefone_normalizado")
