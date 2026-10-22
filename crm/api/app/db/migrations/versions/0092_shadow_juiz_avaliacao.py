"""shadow_decisions: colunas do juiz (claude -p) e de quem marcou acertou/errou

O placar do Aprendiz media só similaridade de trigramas (nunca passava de 55%) e o
campo `acerto` nunca foi preenchido. Agora o veredito final vem, nesta ordem, de:
marcação humana (acerto) > juiz IA (juiz_*) > pendente.

Só colunas NOVAS e nulas, `IF NOT EXISTS`: publicar não muda nenhuma linha.
"""

from alembic import op

revision = "0092"
down_revision = "0091"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for col, tipo in (
        ("juiz_veredito", "TEXT"),
        ("juiz_motivo", "TEXT"),
        ("juiz_modelo", "TEXT"),
        ("juiz_em", "TIMESTAMPTZ"),
        ("avaliado_por", "TEXT"),
    ):
        op.execute(f"ALTER TABLE shadow_decisions ADD COLUMN IF NOT EXISTS {col} {tipo}")


def downgrade() -> None:
    for col in ("avaliado_por", "juiz_em", "juiz_modelo", "juiz_motivo", "juiz_veredito"):
        op.execute(f"ALTER TABLE shadow_decisions DROP COLUMN IF EXISTS {col}")
