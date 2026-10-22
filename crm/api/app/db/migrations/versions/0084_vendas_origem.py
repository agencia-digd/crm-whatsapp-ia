"""vendas.origem: de onde veio o cliente de cada venda

O ROI da Visão era VGV ÷ gasto Meta com TODA venda no numerador. Em set/2026
isso deu 299x: dos R$ 774.754 do mês, R$ 210.000 eram da Pessoa Clara, que chegou
por indicação direto no WhatsApp da Pessoa11 e nunca foi lead de anúncio. Venda de
indicação ou parceiro não é retorno da mídia, e somar as duas faz o anúncio
parecer melhor do que é justamente no mês em que a indicação foi boa.

Valores:
  midia          lead que veio de anúncio (form Meta, UTM ig/fb no Kommo)
  indicacao      alguém mandou o cliente pra gente
  parceiro       veio de imobiliária/corretor parceiro
  nao_informada  ninguém sabe ainda — é o DEFAULT, de propósito

O default é `nao_informada`, não `midia`: venda que entra sem ninguém dizer a
origem fica FORA do ROI até alguém confirmar. Errar pra baixo no retorno da
mídia é recuperável; inflar de novo é o que esta coluna existe pra impedir.

Só estrutura. O preenchimento das vendas de 2026 é dado, não schema, e vive em
`scripts/sql/vendas_origem_2026.sql` com a evidência de cada linha.

O trigger `trg_vendas_reconcilia` dispara em UPDATE e recalcula
`vendas_mensais` do mês — a conta dele não olha `origem`, então o VGV e o
número de vendas do mês saem iguais.

Revision ID: 0084
Revises: 0083
Create Date: 2026-09-24
"""
from __future__ import annotations

from alembic import op

revision = "0084"
down_revision = "0083"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE vendas ADD COLUMN IF NOT EXISTS origem TEXT "
        "NOT NULL DEFAULT 'nao_informada'"
    )
    op.execute("ALTER TABLE vendas DROP CONSTRAINT IF EXISTS ck_vendas_origem")
    op.execute(
        "ALTER TABLE vendas ADD CONSTRAINT ck_vendas_origem CHECK "
        "(origem IN ('midia', 'indicacao', 'parceiro', 'nao_informada'))"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE vendas DROP CONSTRAINT IF EXISTS ck_vendas_origem")
    op.execute("ALTER TABLE vendas DROP COLUMN IF EXISTS origem")
