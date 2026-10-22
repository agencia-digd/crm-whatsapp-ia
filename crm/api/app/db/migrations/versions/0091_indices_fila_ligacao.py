"""índices da fila de ligação: lead_events, cadencia_eventos e lead_analyses

A aba de Ligações levava ~45 s pra abrir com 1.993 leads na fila (05/10/2026)
e o celular da Pessoa7 desistia antes (nginx 499). Os filtros por lead da fila
varriam três tabelas inteiras, uma vez por lead candidato:

    lead_events      (carrossel_disparo recente)   11,3 s
    cadencia_eventos (mensagem automática recente)  4,3 s
    lead_analyses    (LATERAL da última análise)   14,8 s

Os três índices foram criados em produção no mesmo dia, à mão e com OK da Dona
(CONCURRENTLY). Medido com o código de produção em transação com rollback:
lista da Pessoa7 31,3 s → 1,5 s; total da fila 13,8 s → 0,7 s.

Em produção esta migration é no-op (IF NOT EXISTS). CONCURRENTLY não roda
dentro de transação, por isso o autocommit_block.
"""

from alembic import op

revision = "0091"
down_revision = "0090"
branch_labels = None
depends_on = None


_INDICES = (
    ("ix_lead_events_lead_tipo_at", "lead_events (lead_id, tipo, at)"),
    ("ix_cadencia_eventos_tenant_tipo_at", "cadencia_eventos (tenant_id, tipo, at)"),
    ("ix_lead_analyses_lead_analyzed", "lead_analyses (lead_id, analyzed_at DESC)"),
)


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for nome, alvo in _INDICES:
            op.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {nome} ON {alvo}")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for nome, _ in _INDICES:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {nome}")
