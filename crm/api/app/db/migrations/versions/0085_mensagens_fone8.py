"""mensagens_whatsapp.fone8: coluna gerada com os 8 últimos dígitos do telefone

A coluna e o índice `ix_msg_fone8_ts` já existiam em produção, criados à mão,
sem migration — por isso o banco de teste não tinha e qualquer consulta que
usasse `fone8` passava em produção e quebrava na suíte.

Por que importa: o bloco "retomar" do Meu dia casava lead com mensagem pela
expressão `right(regexp_replace(coalesce(contato_telefone,''),'\\D','','g'),8)`
escrita por extenso. O Postgres não troca a expressão pela coluna gerada, então
não usava o índice e relia as 129 mil mensagens por lead do mês — 37s dos 40s
da tela (25/09). Com `m.fone8 = b.suf`: 0,3s, mesmo resultado.

IF NOT EXISTS em tudo: em produção vira no-op.

Revision ID: 0085
Revises: 0084
Create Date: 2026-09-25
"""
from __future__ import annotations

from alembic import op

revision = "0085"
down_revision = "0084"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
        ALTER TABLE mensagens_whatsapp
          ADD COLUMN IF NOT EXISTS fone8 text GENERATED ALWAYS AS
          (right(regexp_replace(coalesce(contato_telefone, ''), '\D', '', 'g'), 8)) STORED
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_msg_fone8_ts "
        "ON mensagens_whatsapp (fone8, timestamp_evolution DESC)"
    )


def downgrade() -> None:
    # Não derruba: a coluna existia antes desta migration em produção.
    pass
