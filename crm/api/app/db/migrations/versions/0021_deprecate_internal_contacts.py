"""deprecate internal_contacts (superseded by internos_acme)

Revision ID: 0021
Revises: 0020
Create Date: 2026-05-10

internal_contacts is empty and superseded by internos_acme.
Kept for schema compatibility — do NOT use in new code.
Drop in Onda 9 (SaaS multi-tenant refactor).
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0021"
down_revision: Union[str, None] = "0020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "COMMENT ON TABLE internal_contacts IS "
        "'DEPRECATED — use internos_acme instead. Drop in Onda 9 (SaaS refactor).'"
    )


def downgrade() -> None:
    op.execute("COMMENT ON TABLE internal_contacts IS NULL")
