"""adiciona campos de Instagram na corretora (pra publicar/medir rede social dela)

Cada corretora Acme pode ter o próprio IG conectado ao Business Manager Acme.
Quando ig_user_id estiver preenchido, o social_publisher publica os posts dela no
IG dela (resolvendo o cliente=slug -> ig_user_id). Sem isso, posts ficam
aprovados mas o publisher marca 'sem ig_user_id' (publicação manual).

Revision ID: 0034
Revises: 0033
"""
from typing import Union

from alembic import op
import sqlalchemy as sa

revision: str = "0034"
down_revision: Union[str, None] = "0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("corretoras", sa.Column("ig_user_id", sa.Text(), nullable=True))
    op.add_column("corretoras", sa.Column("ig_handle", sa.Text(), nullable=True))
    op.add_column("corretoras", sa.Column("ig_connected_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("corretoras", "ig_connected_at")
    op.drop_column("corretoras", "ig_handle")
    op.drop_column("corretoras", "ig_user_id")
