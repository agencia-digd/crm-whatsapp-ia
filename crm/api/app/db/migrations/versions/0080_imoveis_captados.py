"""imoveis_captados — Camada 1 do banco de imóveis (captação da corretora)

O ativo próprio da Acme: imóvel de DONO, que chega por WhatsApp todo dia e
hoje morre na conversa (ver projetos/acme/PLANO-BANCO-DE-IMOVEIS.md §2).
Medido em 12/09: ~3 avaliações por semana só pelo caminho da Pessoa.

Três tabelas, todas NOVAS. Migration 100% ADITIVA — nenhum ALTER em tabela
existente, nada é apagado nem renomeado.

  imoveis_captados  o imóvel captado. `corretora_id` é NOT NULL de propósito:
                    UMA corretora é dona da captação, e comissão nasce daqui
                    (ver memória `acme-comissoes-caixa-vs-crm`). Sem dono
                    obrigatório vira a briga de roleta de novo.

  imovel_foto       foto do imóvel. O CHECK de `origem` só aceita 'dono' e
                    'corretora' — **não existe o valor 'portal'**. A recusa de
                    processar foto de portal de terceiro (§3 do plano) vira
                    impossibilidade no schema, não recomendação no README.
                    Foto tratada (logo da Acme) nasce como LINHA NOVA com
                    `tratada_de_id` apontando pra original; a original nunca é
                    sobrescrita nem apagada. Retirar foto de circulação é
                    `arquivada=true`, não DELETE.

  imovel_evento     append-only. Transferência de captação entre corretoras é
                    EVENTO ASSINADO, nunca UPDATE silencioso — o dado que
                    define comissão não pode mudar sem rastro.

Sem UNIQUE anti-duplicata de propósito: dono com dois apartamentos idênticos no
mesmo prédio existe, e uma constraint dura viraria 500 no meio da conversa com
o dono. A checagem de parecido roda no serviço e devolve AVISO junto do 201 —
trava social, não erro HTTP. Nada pode fazer o cadastro falhar.

Revision ID: 0080
Revises: 0079
Create Date: 2026-09-12
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0080"
down_revision: Union[str, None] = "0079"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "imoveis_captados",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False, server_default=sa.text("1")),
        # ── dona da captação ────────────────────────────────────────────────
        sa.Column(
            "corretora_id",
            sa.Integer(),
            sa.ForeignKey("corretoras.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        # login de quem digitou (admin pode cadastrar NO LUGAR da dona)
        sa.Column("captado_por", sa.Text(), nullable=False),
        sa.Column(
            "captado_em", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        # ── imóvel ──────────────────────────────────────────────────────────
        sa.Column("tipo", sa.Text(), nullable=False),  # apartamento|casa|terreno|outro
        sa.Column("quartos", sa.Integer(), nullable=True),
        sa.Column("vagas", sa.Integer(), nullable=True),
        sa.Column("suites", sa.Integer(), nullable=True),
        sa.Column("area_m2", sa.Numeric(10, 2), nullable=True),
        sa.Column("bairro", sa.Text(), nullable=False),
        sa.Column("cidade", sa.Text(), nullable=False, server_default=sa.text("'Cidade'")),
        sa.Column("endereco", sa.Text(), nullable=True),
        sa.Column("cep", sa.Text(), nullable=True),
        sa.Column("referencia", sa.Text(), nullable=True),
        sa.Column("preco_pedido", sa.Numeric(14, 2), nullable=True),
        # NULL = não perguntei. Diferente de false (= perguntei e é não).
        sa.Column("aceita_financiamento", sa.Boolean(), nullable=True),
        sa.Column("aceita_permuta", sa.Boolean(), nullable=True),
        # ── dono ────────────────────────────────────────────────────────────
        sa.Column("dono_nome", sa.Text(), nullable=True),
        sa.Column("dono_telefone", sa.Text(), nullable=False),
        sa.Column("dono_telefone_normalizado", sa.Text(), nullable=True),
        sa.Column("dono_relacao", sa.Text(), nullable=True),
        # ── situação comercial ──────────────────────────────────────────────
        sa.Column(
            "situacao", sa.Text(), nullable=False, server_default=sa.text("'disponivel'")
        ),
        sa.Column(
            "exclusividade", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("exclusividade_ate", sa.Date(), nullable=True),
        # O ativo de verdade é a autorização assinada, não a linha no banco
        # (revisão de 12/09 no plano). Campo nasce false e é cobrado na ficha.
        sa.Column(
            "autorizacao_assinada", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("fonte", sa.Text(), nullable=True),
        # ── amarras com o que já existe ─────────────────────────────────────
        sa.Column("lead_id", sa.Text(), nullable=True),
        sa.Column("observacoes", sa.Text(), nullable=True),
        sa.Column("completude", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "duplicata_de",
            sa.BigInteger(),
            sa.ForeignKey("imoveis_captados.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # Gancho da Camada 3 (cruzamento com anúncio de portal). Nasce vazio e
        # continua vazio enquanto a Camada 2 estiver arquivada.
        sa.Column("imovel_mercado_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "atualizado_em",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "tipo IN ('apartamento','casa','terreno','comercial','chacara','outro')",
            name="ck_imoveis_captados_tipo",
        ),
        sa.CheckConstraint(
            "situacao IN ('disponivel','vendido','desistiu','exclusividade_outra','nao_valida')",
            name="ck_imoveis_captados_situacao",
        ),
        sa.CheckConstraint(
            "dono_relacao IS NULL OR dono_relacao IN "
            "('proprietario','procurador','familiar','inventario','outro')",
            name="ck_imoveis_captados_dono_relacao",
        ),
        sa.CheckConstraint(
            "fonte IS NULL OR fonte IN "
            "('whatsapp','indicacao','placa','porta','portal','pessoa','outro')",
            name="ck_imoveis_captados_fonte",
        ),
    )
    op.create_index(
        "ix_imoveis_captados_corretora",
        "imoveis_captados",
        ["tenant_id", "corretora_id", "situacao"],
    )
    op.create_index(
        "ix_imoveis_captados_captado_em", "imoveis_captados", ["tenant_id", "captado_em"]
    )
    # Blocking key da checagem de parecido (§1 do desenho): cidade+bairro+tipo+
    # quartos é o que o serviço consulta antes de avisar "a Pessoa10 já captou".
    op.create_index(
        "ix_imoveis_captados_bloco",
        "imoveis_captados",
        ["tenant_id", "cidade", "bairro", "tipo", "quartos"],
    )
    op.create_index(
        "ix_imoveis_captados_dono_tel", "imoveis_captados", ["dono_telefone_normalizado"]
    )

    op.create_table(
        "imovel_foto",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "imovel_id",
            sa.BigInteger(),
            sa.ForeignKey("imoveis_captados.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ordem", sa.Integer(), nullable=False, server_default=sa.text("0")),
        # caminho RELATIVO dentro do diretório de mídia — nunca path absoluto no
        # banco, senão mover o disco vira UPDATE em massa.
        sa.Column("arquivo", sa.Text(), nullable=False),
        sa.Column("mime", sa.Text(), nullable=False, server_default=sa.text("'image/jpeg'")),
        sa.Column("bytes", sa.Integer(), nullable=True),
        sa.Column("largura", sa.Integer(), nullable=True),
        sa.Column("altura", sa.Integer(), nullable=True),
        sa.Column("hash_sha256", sa.String(64), nullable=True),
        # 🔴 A TRAVA: 'portal' NÃO está na lista e não deve entrar nunca.
        # Foto de anúncio de terceiro não é nossa nem do VivaReal — é do
        # fotógrafo/imobiliária que anunciou. Ver §3 do plano.
        sa.Column("origem", sa.Text(), nullable=False, server_default=sa.text("'dono'")),
        sa.Column(
            "autorizacao_dono", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("tratada", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        # A versão com a logo aponta pra original. Original NUNCA é sobrescrita.
        sa.Column(
            "tratada_de_id",
            sa.BigInteger(),
            sa.ForeignKey("imovel_foto.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("tratamento", sa.Text(), nullable=True),  # ex: 'logo_acme'
        # Retirar de circulação é isto, não DELETE (nada se apaga).
        sa.Column("arquivada", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("enviado_por", sa.Text(), nullable=True),
        sa.Column(
            "criado_em", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "origem IN ('dono','corretora')", name="ck_imovel_foto_origem_sem_portal"
        ),
    )
    op.create_index("ix_imovel_foto_imovel", "imovel_foto", ["imovel_id", "arquivada", "ordem"])
    # Dedupe barato: a mesma foto enviada duas vezes no mesmo imóvel.
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_imovel_foto_hash "
        "ON imovel_foto (imovel_id, hash_sha256) "
        "WHERE hash_sha256 IS NOT NULL AND tratada = false"
    )

    op.create_table(
        "imovel_evento",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "imovel_id",
            sa.BigInteger(),
            sa.ForeignKey("imoveis_captados.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tipo", sa.Text(), nullable=False),
        sa.Column("de", JSONB(), nullable=True),
        sa.Column("para", JSONB(), nullable=True),
        sa.Column("por", sa.Text(), nullable=False),
        sa.Column("nota", sa.Text(), nullable=True),
        sa.Column(
            "em", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_imovel_evento_imovel", "imovel_evento", ["imovel_id", "em"])


def downgrade() -> None:
    op.drop_index("ix_imovel_evento_imovel", table_name="imovel_evento")
    op.drop_table("imovel_evento")
    op.execute("DROP INDEX IF EXISTS uq_imovel_foto_hash")
    op.drop_index("ix_imovel_foto_imovel", table_name="imovel_foto")
    op.drop_table("imovel_foto")
    op.drop_index("ix_imoveis_captados_dono_tel", table_name="imoveis_captados")
    op.drop_index("ix_imoveis_captados_bloco", table_name="imoveis_captados")
    op.drop_index("ix_imoveis_captados_captado_em", table_name="imoveis_captados")
    op.drop_index("ix_imoveis_captados_corretora", table_name="imoveis_captados")
    op.drop_table("imoveis_captados")
