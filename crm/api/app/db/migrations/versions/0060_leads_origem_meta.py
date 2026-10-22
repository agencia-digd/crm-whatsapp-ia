"""leads: colunas de origem Meta (campanha/anúncio/form)

O receptor REAL do webhook de Lead Ads da Meta (POST /intake/meta-webhook)
busca na Graph API, além de nome/telefone, a ORIGEM do lead: campaign_name,
ad_name e form_id. Sem lugar na espinha (`leads`), esses dados se perdiam no
raw da sombra. Estas colunas aditivas (nullable) guardam a origem no card
canônico do CRM — futura tag rica 📣 por campanha/anúncio.

Aditiva e reversível: 3 colunas text nullable, sem backfill, sem índice.

Revision ID: 0060
Revises: 0059
Create Date: 2026-07-26
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0060"
down_revision: Union[str, None] = "0059"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("leads", sa.Column("origem_campanha", sa.Text(), nullable=True))
    op.add_column("leads", sa.Column("origem_anuncio", sa.Text(), nullable=True))
    op.add_column("leads", sa.Column("origem_form", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("leads", "origem_form")
    op.drop_column("leads", "origem_anuncio")
    op.drop_column("leads", "origem_campanha")
