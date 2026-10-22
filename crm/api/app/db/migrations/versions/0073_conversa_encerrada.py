"""conversa_encerrada: "feito" do Meu Dia — quem encerra é o LEAD, às vezes

O bloco "responderam e esperam você" só apaga quando a corretora manda alguma
coisa. Só que às vezes a última mensagem do lead É o fim da conversa ("fechado,
obrigada!") e não há o que responder — a corretora mandava um joinha só pra
limpar a tela. O filtro determinístico (`pede_resposta`) pega os casos óbvios,
mas na dúvida ele MANTÉM, de propósito; este botão é a saída manual pro resto.

`encerrado_ate` guarda o INSTANTE da última mensagem no momento do clique, e
não um booleano. É o que faz a conversa VOLTAR sozinha se a pessoa escrever
de novo depois: a mensagem nova é mais recente que o encerramento. Com um
booleano, a corretora marcaria "feito" uma vez e o lead sumiria pra sempre.

A chave é o telefone (8 dígitos), não o lead: é a mesma chave que o bloco usa,
e conversa existe até pra contato sem lead no funil.

Revision ID: 0073
Revises: 0072
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0073"
down_revision = "0072"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "conversa_encerrada",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("tenant_id", sa.Integer, nullable=False, server_default=sa.text("1")),
        # últimos 8 dígitos do telefone — mesma chave do bloco do Meu Dia
        sa.Column("p8", sa.Text, nullable=False),
        # instante da última mensagem quando marcaram "feito"
        sa.Column("encerrado_ate", sa.DateTime(timezone=True), nullable=False),
        sa.Column("corretora_id", sa.Integer, nullable=True),
        sa.Column("autor", sa.Text, nullable=True),
        sa.Column(
            "criado_em", sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"), nullable=False,
        ),
    )
    # um encerramento por conversa: marcar de novo EMPURRA a marca pra frente
    op.create_unique_constraint(
        "uq_conversa_encerrada_p8", "conversa_encerrada", ["tenant_id", "p8"]
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_conversa_encerrada_p8", "conversa_encerrada", type_="unique"
    )
    op.drop_table("conversa_encerrada")
