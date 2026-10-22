"""internos_acme: aceita a casa inteira, não só a dona

A tabela se chama `internos_acme` mas o CHECK só admitia papel='dona' — na
prática ela guardava uma linha (Pessoa Pessoa9). Todo o resto da casa ficava
fora, e quem consulta "esse telefone é da equipe?" recebia não pra corretora.

Custou caro em 17/08: o detector de negociação (sinal_negocio) apontou a Pessoa10 e
a Pessoa1 como "negociação avançada" todo dia. Elas conversam entre si pelas
linhas de trabalho, e conversa de corretora é cheia de "documentação",
"aprovação", "cartório" — o vocabulário exato que o detector procura.

E a causa de fundo é outra descoberta: as duas têm SEGUNDO APARELHO. O número
em `corretoras` é um; o que elas usam nas conversas é outro (a Pessoa10 tem 696
mensagens na própria linha com o número não cadastrado). Sem lugar pra
registrar isso, todo sistema que pergunta "é da casa?" erra.

Revision ID: 0075
Revises: 0074
"""
from __future__ import annotations

from alembic import op

revision = "0075"
down_revision = "0074"
branch_labels = None
depends_on = None

_PAPEIS = ("dona", "corretora", "gerente", "assistente", "socia", "outro")


def upgrade() -> None:
    op.execute("ALTER TABLE internos_acme DROP CONSTRAINT IF EXISTS ck_internos_acme_papel")
    lista = ", ".join(f"'{p}'" for p in _PAPEIS)
    op.execute(
        f"ALTER TABLE internos_acme ADD CONSTRAINT ck_internos_acme_papel "
        f"CHECK (papel IN ({lista}))"
    )


def downgrade() -> None:
    # Volta ao CHECK antigo. Só passa se ninguém além da dona estiver cadastrado
    # — e é bom que falhe nesse caso, em vez de apagar gente da casa em silêncio.
    op.execute("ALTER TABLE internos_acme DROP CONSTRAINT IF EXISTS ck_internos_acme_papel")
    op.execute(
        "ALTER TABLE internos_acme ADD CONSTRAINT ck_internos_acme_papel "
        "CHECK (papel = 'dona')"
    )
