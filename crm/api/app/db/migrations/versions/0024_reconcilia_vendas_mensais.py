"""reconcilia vendas → vendas_mensais via trigger (Onda 10.1.b)

Cria fn_reconcilia_vendas_mensais(corretora_id, ano_mes) que agrega
COUNT/SUM da tabela `vendas` e faz UPSERT em vendas_mensais com
fonte='assistente_aggregate'. Precedência: não sobrescreve linhas
fonte='funil_manual' (histórico Sheets sagrado).

Trigger trg_vendas_reconcilia em INSERT/UPDATE/DELETE em `vendas`
dispara reconciliação real-time. UPDATE que muda corretora_id ou
data_venda recalcula ambas as chaves (antiga + nova).

Revision ID: 0024
Revises: 0023
Create Date: 2026-05-13
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0024"
down_revision: Union[str, None] = "0023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


FN_RECONCILIA = """
CREATE OR REPLACE FUNCTION fn_reconcilia_vendas_mensais(
    p_corretora_id INT,
    p_ano_mes CHAR(7)
) RETURNS VOID AS $$
DECLARE
    v_qtde INT;
    v_vgv NUMERIC(14,2);
BEGIN
    SELECT COUNT(*), COALESCE(SUM(valor), 0)
      INTO v_qtde, v_vgv
      FROM vendas
     WHERE corretora_id = p_corretora_id
       AND to_char(data_venda, 'YYYY-MM') = p_ano_mes;

    INSERT INTO vendas_mensais (
        corretora_id, ano_mes, qtde_vendas, vgv,
        fonte, criado_em, atualizado_em
    ) VALUES (
        p_corretora_id, p_ano_mes, v_qtde, v_vgv,
        'assistente_aggregate', NOW(), NOW()
    )
    ON CONFLICT (corretora_id, ano_mes) DO UPDATE
       SET qtde_vendas = EXCLUDED.qtde_vendas,
           vgv = EXCLUDED.vgv,
           fonte = 'assistente_aggregate',
           atualizado_em = NOW()
     WHERE vendas_mensais.fonte IS DISTINCT FROM 'funil_manual';
END;
$$ LANGUAGE plpgsql;
"""

FN_TRIGGER = """
CREATE OR REPLACE FUNCTION fn_trigger_vendas_reconcilia() RETURNS TRIGGER AS $$
BEGIN
    IF (TG_OP = 'DELETE') THEN
        PERFORM fn_reconcilia_vendas_mensais(
            OLD.corretora_id, to_char(OLD.data_venda, 'YYYY-MM')
        );
        RETURN OLD;
    ELSIF (TG_OP = 'UPDATE') THEN
        PERFORM fn_reconcilia_vendas_mensais(
            OLD.corretora_id, to_char(OLD.data_venda, 'YYYY-MM')
        );
        IF OLD.corretora_id IS DISTINCT FROM NEW.corretora_id
           OR OLD.data_venda IS DISTINCT FROM NEW.data_venda THEN
            PERFORM fn_reconcilia_vendas_mensais(
                NEW.corretora_id, to_char(NEW.data_venda, 'YYYY-MM')
            );
        END IF;
        RETURN NEW;
    ELSE
        PERFORM fn_reconcilia_vendas_mensais(
            NEW.corretora_id, to_char(NEW.data_venda, 'YYYY-MM')
        );
        RETURN NEW;
    END IF;
END;
$$ LANGUAGE plpgsql;
"""

CREATE_TRIGGER = """
DROP TRIGGER IF EXISTS trg_vendas_reconcilia ON vendas;
CREATE TRIGGER trg_vendas_reconcilia
AFTER INSERT OR UPDATE OR DELETE ON vendas
FOR EACH ROW EXECUTE FUNCTION fn_trigger_vendas_reconcilia();
"""


def upgrade() -> None:
    op.execute(FN_RECONCILIA)
    op.execute(FN_TRIGGER)
    op.execute(CREATE_TRIGGER)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_vendas_reconcilia ON vendas;")
    op.execute("DROP FUNCTION IF EXISTS fn_trigger_vendas_reconcilia();")
    op.execute("DROP FUNCTION IF EXISTS fn_reconcilia_vendas_mensais(INT, CHAR);")
