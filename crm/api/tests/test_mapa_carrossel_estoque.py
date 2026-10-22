"""Estoque do carrossel da Corretor — MESMO recorte do motor.

A contagem por etapa (Fila / Disparado / Respondeu) que o painel mostra não
filtrava `tenant_id` nem a dona. O motor (`carrossel_corretor`) filtra os dois em
toda query — então a tela podia contar lead de OUTRO cliente (ou de outra
corretora que caísse nesses stages) como estoque da Corretor.

Precisa de DB real (TEST_DATABASE_URL — ver conftest.py).
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import text as sql_text

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="precisa de TEST_DATABASE_URL (Postgres de teste)",
)


def test_estoque_conta_so_o_tenant_e_a_dona_certos():
    from app.api.automacoes import _estoque_carrossel
    from app.db.models import Lead
    from app.db.session import SessionLocal
    from app.services import carrossel_corretor as cb

    agora = datetime.now(timezone.utc)
    marca = f"t-est-{uuid.uuid4().hex[:8]}"
    with SessionLocal() as s:
        # o stage 23 (Fila do remarketing) é dado de produção — no banco de
        # teste ele pode não existir, e a FK de leads exige a linha.
        s.execute(
            sql_text(
                "INSERT INTO pipeline_stages (id, tenant_id, nome, ordem, funil) "
                "VALUES (:i, 1, 'Fila', 1, 'remarketing') "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {"i": cb.STAGE_FILA_PADRAO},
        )
        s.commit()
        antes = _estoque_carrossel(s)
        s.add_all([
            # o único que é estoque da Corretor
            Lead(tenant_id=1, external_ref=f"{marca}-ok", nome="Da Corretor",
                 telefone="+5500000000000", owner_id=cb.CORRETORA_CORRETOR,
                 stage_id=cb.STAGE_FILA_PADRAO, created_at=agora, updated_at=agora),
            # mesma etapa, outra dona
            Lead(tenant_id=1, external_ref=f"{marca}-outra", nome="De outra",
                 telefone="+5500000000000", owner_id=1,
                 stage_id=cb.STAGE_FILA_PADRAO, created_at=agora, updated_at=agora),
            # mesma etapa e mesma dona, OUTRO cliente
            Lead(tenant_id=2, external_ref=f"{marca}-t2", nome="Outro tenant",
                 telefone="+5500000000000", owner_id=cb.CORRETORA_CORRETOR,
                 stage_id=cb.STAGE_FILA_PADRAO, created_at=agora, updated_at=agora),
        ])
        s.commit()
        try:
            depois = _estoque_carrossel(s)
            assert depois["fila"] - antes["fila"] == 1
        finally:
            s.execute(
                sql_text("DELETE FROM leads WHERE external_ref LIKE :m"),
                {"m": f"{marca}%"},
            )
            s.commit()
