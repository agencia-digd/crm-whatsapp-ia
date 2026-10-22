"""conversations: aceita 'lead' e 'pessoa' em sender_kind

A tabela nasceu pra Assistente ↔ corretoras/donas, então o CHECK só admitia
('corretora','dona','assistant'). A partir de 30/07 ela guarda também a conversa
do LEAD com a Pessoa na linha da Corretor (0000-0000) — é o histórico que a Pessoa lê
pra não repetir pergunta entre uma mensagem e outra.

Descoberto em produção, ponta a ponta: o inbound gravava a mensagem na thread
normalmente, mas o atendimento estourava CheckViolation ao tentar montar o
histórico — e a Pessoa ficava muda. Sem este CHECK aberto, o atendimento não
funciona.

Revision ID: 0068
Revises: 0067
"""
from __future__ import annotations

from alembic import op

revision = "0068"
down_revision = "0067"
branch_labels = None
depends_on = None

_ANTIGO = ("corretora", "dona", "assistant")
_NOVO = ("corretora", "dona", "assistant", "lead", "pessoa")


def _recriar(valores: tuple[str, ...]) -> None:
    lista = ", ".join(f"'{v}'" for v in valores)
    op.execute("ALTER TABLE conversations DROP CONSTRAINT IF EXISTS ck_conversations_sender_kind")
    op.execute(
        "ALTER TABLE conversations ADD CONSTRAINT ck_conversations_sender_kind "
        f"CHECK (sender_kind = ANY (ARRAY[{lista}]::text[]))"
    )


def upgrade() -> None:
    _recriar(_NOVO)


def downgrade() -> None:
    # Volta o CHECK antigo. As linhas de lead/pessoa que existirem passam a
    # violá-lo, então some com elas primeiro — são histórico de conversa,
    # não dado de negócio (o thread real vive em mensagens_whatsapp).
    op.execute("DELETE FROM conversations WHERE sender_kind IN ('lead', 'pessoa')")
    _recriar(_ANTIGO)
