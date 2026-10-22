"""visitas: lead_id + origem + índice único anti-duplicata

Higiene estrutural de VISITAS (REVISAO-08-AGENDA-PROSPEC gaps A3, A4, A6):
  - A3: `lead_id TEXT NULL` — referência LÓGICA a leads_snapshot (sem FK dura,
        pra não travar INSERT quando o lead ainda não está no snapshot).
  - A4: `origem TEXT NULL` ('whatsapp'|'crm'|'manual') — o painel.py
        listar_visitas já SELECTa `origem`, mas a coluna nunca existiu.
  - A6: índice ÚNICO parcial anti-visita-dupla:
        unique(corretora_id, data_hora, lower(lead_nome)) — só pra visitas
        NÃO canceladas (WHERE status <> 'cancelada'), via índice de expressão.
        O INSERT do criar_visita.py usa ON CONFLICT DO NOTHING contra esse índice.

Revision ID: 0036
Revises: 0035
Create Date: 2026-07-02
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0037"
down_revision: Union[str, None] = "0036"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Nome canônico do índice único parcial — o INSERT do criar_visita.py referencia
# este mesmo nome no ON CONFLICT.
UNIQUE_DEDUP_INDEX = "uq_visitas_corretora_datahora_leadnome"


def upgrade() -> None:
    # ADD COLUMN IF NOT EXISTS: `origem` já existia em produção (a REVISAO-08
    # errou ao dizer que faltava) e a renumeração 0036→0037 pede idempotência.
    op.execute("ALTER TABLE visitas ADD COLUMN IF NOT EXISTS lead_id TEXT")
    op.execute("ALTER TABLE visitas ADD COLUMN IF NOT EXISTS origem TEXT")

    # Cruzamento com o Inspector é por lead_id — index simples ajuda o join.
    op.execute("CREATE INDEX IF NOT EXISTS idx_visitas_lead_id ON visitas (lead_id)")

    # Índice ÚNICO PARCIAL de expressão: anti-duplicata pelos 2 canais
    # (WhatsApp + CRM) pro MESMO lead no MESMO horário/corretora.
    # Só vale pra visitas vivas (canceladas podem repetir).
    op.execute(
        f"""
        CREATE UNIQUE INDEX {UNIQUE_DEDUP_INDEX}
            ON visitas (corretora_id, data_hora, lower(lead_nome))
            WHERE status <> 'cancelada'
        """
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {UNIQUE_DEDUP_INDEX}")
    op.drop_index("idx_visitas_lead_id", table_name="visitas")
    op.drop_column("visitas", "origem")
    op.drop_column("visitas", "lead_id")
