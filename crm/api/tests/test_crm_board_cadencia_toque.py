"""GET /painel/crm/board — campo `cadencia_toque` no card (cadência invisível).

Regra (Dona, 10/07): a cadência não tem módulo próprio — no card do CRM aparece
só a tag "CD1", "CD2"... conforme o toque atual. Cobre:

  - cadência ATIVA → cadencia_toque = "CD{toque_atual+1}" (toque 0 → CD1);
  - RESPONDIDA → tag CD FICA (congela no toque que recuperou) e o
    respondeu_em vem preenchido — badge 🟢 aparece JUNTO da tag no front;
  - concluída / opt-out / remarketing → cadencia_toque = None;
  - lead sem cadência / sem telefone → None;
  - join por telefone NORMALIZADO (lead "+5515..." casa com estado "5515...").

DB isolado: SQLite in-memory via monkeypatch de painel.SessionLocal
(mesmo padrão de test_endpoint_painel_crm_board.py). Zero prod, zero envio.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import BigInteger, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.api.painel as painel
from app.db.models import Base, CadenciaEstado, CadenciaEvento, Lead, LeadEvent, PipelineStage
from app.main import app


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(element, compiler, **kw):  # noqa: ANN001
    return "JSON"


@compiles(BigInteger, "sqlite")
def _compile_bigint_sqlite(element, compiler, **kw):  # noqa: ANN001
    return "INTEGER"


AGORA = datetime.now(timezone.utc)


def _estado(telefone: str, toque: int, status: str) -> CadenciaEstado:
    return CadenciaEstado(
        tenant_id=1, telefone=telefone, toque_atual=toque, status=status,
        proximo_toque_em=AGORA, criado_em=AGORA, atualizado_em=AGORA,
    )


def _seed(factory) -> None:
    with factory() as s:
        st = PipelineStage(tenant_id=1, nome="Follow-up", ordem=1, tipo="aberta")
        s.add(st)
        s.flush()

        def _lead(ref: str, nome: str, telefone: str | None) -> Lead:
            return Lead(
                tenant_id=1, external_ref=ref, nome=nome, telefone=telefone,
                owner_id=3, stage_id=st.id, created_at=AGORA, updated_at=AGORA,
            )

        s.add_all([
            _lead("kommo_1", "Toque Zero", "+5500000000000"),
            _lead("kommo_2", "Toque Tres", "+5500000000000"),
            _lead("kommo_3", "Concluida", "+5500000000000"),
            _lead("kommo_4", "OptOut", "+5500000000000"),
            _lead("kommo_5", "Remarketing", "+5500000000000"),
            _lead("kommo_6", "Sem Cadencia", "+5500000000000"),
            _lead("kommo_7", "Sem Telefone", None),
            _lead("kommo_8", "Respondida", "+5500000000000"),
        ])
        est_respondida = _estado("+5500000000000", 4, "respondida")  # → CD5 + 🟢
        s.add_all([
            _estado("+5500000000000", 0, "ativa"),        # → CD1
            _estado("+5500000000000", 3, "ativa"),        # → CD4
            _estado("+5500000000000", 8, "concluida"),    # → None
            _estado("+5500000000000", 2, "opt_out"),      # → None
            _estado("+5500000000000", 8, "remarketing"),  # → None
            est_respondida,
        ])
        s.flush()
        s.add(
            CadenciaEvento(
                tenant_id=1, cadencia_id=est_respondida.id, toque=None,
                tipo="resposta", canal="whatsapp",
                payload={"texto": "oi", "opt_out": False}, at=AGORA,
            )
        )
        s.commit()


@pytest.fixture()
def client(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        engine,
        tables=[
            PipelineStage.__table__, Lead.__table__, LeadEvent.__table__,
            CadenciaEstado.__table__, CadenciaEvento.__table__,
        ],
    )
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(painel, "SessionLocal", factory)
    _seed(factory)
    return TestClient(app)


def _cards_por_nome(client) -> dict[str, dict]:
    r = client.get("/painel/crm/board", headers={"X-Painel-User": "dona"})
    assert r.status_code == 200
    return {
        c["nome"]: c
        for col in r.json()["colunas"]
        for c in col["cards"]
    }


def test_cadencia_toque_no_card(client):
    cards = _cards_por_nome(client)

    # ativa → CD{toque_atual+1}; join normaliza o '+' do telefone do lead
    assert cards["Toque Zero"]["cadencia_toque"] == "CD1"
    assert cards["Toque Tres"]["cadencia_toque"] == "CD4"

    # concluída / opt-out / remarketing → sem tag
    assert cards["Concluida"]["cadencia_toque"] is None
    assert cards["OptOut"]["cadencia_toque"] is None
    assert cards["Remarketing"]["cadencia_toque"] is None

    # sem cadência / sem telefone → sem tag (e não explode)
    assert cards["Sem Cadencia"]["cadencia_toque"] is None
    assert cards["Sem Telefone"]["cadencia_toque"] is None


def test_respondida_mantem_tag_cd_e_traz_respondeu_em(client):
    """Correção b (Dona 10/07): a tag CD NÃO some quando o lead responde —
    congela no toque que recuperou; o badge 🟢 (respondeu_em) vem junto."""
    cards = _cards_por_nome(client)
    card = cards["Respondida"]
    assert card["cadencia_toque"] == "CD5"      # tag congelada
    assert card["respondeu_em"] is not None     # badge 🟢 junto, não no lugar
