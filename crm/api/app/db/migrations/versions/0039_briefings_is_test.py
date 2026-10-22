"""adiciona coluna is_test em briefings_sent (blindagem do anti-dobro)

Rodadas de teste/dry-run/smoke gravavam em briefings_sent com success=True
mesmo sem enviar nada de verdade. O anti-dobro (_already_delivered_today)
contava qualquer success=True do dia como entrega real e bloqueava o envio
real (incidente do briefing das 20h não entregue à Dona).

is_test=True marca registros de não-entrega; o anti-dobro passa a ignorá-los.

Revision ID: 0039
Revises: 0038
"""
from typing import Union

from alembic import op
import sqlalchemy as sa

revision: str = "0039"
down_revision: Union[str, None] = "0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "briefings_sent",
        sa.Column(
            "is_test",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("briefings_sent", "is_test")
