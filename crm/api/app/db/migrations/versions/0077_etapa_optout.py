"""etapa "Não perturbe": opt-out não é venda perdida

Perdido é quem não quer comprar. Opt-out é quem talvez quisesse e pediu pra
parar de receber mensagem. Sem etapa própria, os dois viravam a mesma coisa —
e a conta de "quantos perdemos" passava a incluir gente que a gente cansou.

O caso que abriu isso (01/09/2026): a Eduarda Ribeiro conversava com a Pessoa
sobre restrição no nome em 21/08, levou quatro templates automáticos entre
25/08 e 01/09 e clicou "Parar mensagens". O sistema moveu pra Venda perdida no
mesmo minuto. Ela nunca recusou o imóvel.

Hoje 13 leads em Venda perdida têm opt-out registrado — 1,1% dos 1.151. É pouco
no total e enorme no diagnóstico: enquanto estiverem misturados, ninguém
consegue medir quanto a insistência está custando em lead queimado.

A etapa fica com `tipo='perdido'` (mesma família de Venda perdida, então quem
soma "saiu do funil" continua somando certo) mas com nome e ordem próprios, no
fim da fila. É destino final: quem está aqui não recebe automação — as travas de opt-out já existentes
(`cadencia_estado.status='opt_out'` e `disparos_reengajamento.opt_out`) seguem
sendo a fonte que barra o envio. Esta etapa é para a LEITURA: separar no board e
no relatório o que é recusa comercial do que é pedido de silêncio.

Revision ID: 0077
Revises: 0076
"""
from __future__ import annotations

from alembic import op

revision = "0077"
down_revision = "0076"
branch_labels = None
depends_on = None

STAGE_ID = 26
NOME = "Não perturbe"


def upgrade() -> None:
    op.execute(
        f"""
        INSERT INTO pipeline_stages (id, tenant_id, nome, funil, ordem, tipo)
        VALUES ({STAGE_ID}, 1, '{NOME}', 'principal', 99, 'perdido')
        ON CONFLICT (id) DO NOTHING
        """
    )
    # A sequence do id precisa passar do valor cravado, senão o próximo INSERT
    # automático colide com esta linha.
    op.execute(
        """
        SELECT setval(pg_get_serial_sequence('pipeline_stages','id'),
                      GREATEST((SELECT max(id) FROM pipeline_stages), 26))
        """
    )


def downgrade() -> None:
    # Devolve quem estiver aqui pra Venda perdida antes de apagar a etapa —
    # senão os leads ficam apontando pra uma etapa que não existe mais.
    op.execute(f"UPDATE leads SET stage_id = 13 WHERE stage_id = {STAGE_ID}")
    op.execute(f"DELETE FROM pipeline_stages WHERE id = {STAGE_ID}")
