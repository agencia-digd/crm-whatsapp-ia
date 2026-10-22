"""conversa_lida: "lido até" por pessoa e conversa (não lidas do Chat)

Chat do CRM igual ao WhatsApp, pacote A (Dona, 16/10/2026). Antes o "lido" do
Chat vivia só na tela aberta: recarregou, a bolinha verde voltava em tudo e
não havia número. Aqui fica guardado, por login do painel (`usuario`) e por
conversa (`p8` = últimos 8 dígitos do telefone, a mesma chave que o Chat usa
pra casar card e mensagens).

Por login e não por corretora_id: a Dona (admin) abrir a conversa da Pessoa12 não
pode zerar o contador da Pessoa12.

Aditiva: só cria a tabela. Desfazer = `alembic downgrade 0091` (derruba só ela;
nenhum outro dado depende dela).
"""

from alembic import op

revision = "0092"
down_revision = "0091"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS conversa_lida (
            usuario TEXT NOT NULL,
            p8 TEXT NOT NULL,
            lido_ate TIMESTAMPTZ NOT NULL,
            atualizado_em TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (usuario, p8)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS conversa_lida")
