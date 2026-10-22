"""Tag "remarketing" no card — `leads.veio_remarketing_em` (Dona 15/09/2026).

O pedido: quando um lead do remarketing é transferido pra corretora, ele tem
que CHEGAR marcado — a Pessoa12 precisa saber de onde a pessoa veio sem abrir o
histórico. Contexto: no mesmo dia foram limpos 113 leads presos em "Respondeu",
metade tinha levantado a mão e ninguém foi atrás.

O que estes testes protegem:
  1. handoff automático (`handoff_remarketing.processar`) carimba a marca;
  2. promoção da Pessoa / 2ª mensagem (`atendimento_corretor.promover_para_vendas`)
     carimba a MESMA marca — os dois caminhos de transferência, uma tag só;
  3. quem NUNCA esteve no remarketing não é marcado (o botão do painel move
     lead de qualquer procedência — não pode inventar origem);
  4. a marca é escrita UMA vez: segunda passagem não recarimba a data;
  5. a marca CHEGA NA TELA: `GET /painel/crm/board` devolve `veio_remarketing`
     no card (é o que o chip 🔁 do painel V2 lê).

Bancos: 1-4 no Postgres de teste (conftest fail-closed, telefone aleatório por
teste). 5 em SQLite in-memory (mesmo padrão de test_crm_board_cadencia_toque).
Zero envio, zero produção.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import BigInteger, create_engine
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.api.painel as painel
from app.db.models import Base, Lead, LeadEvent, PipelineStage
from app.db.session import SessionLocal
from app.main import app
from app.services import atendimento_corretor as ab
from app.services import config_operacional
from app.services import handoff_remarketing as hr
from app.services.carrossel_corretor import CORRETORA_CORRETOR, STAGE_RESPONDEU
from app.services.lead_transferencia import transferir_lead

CORRETOR = 3
CORRETOR = 1


@pytest.fixture(autouse=True)
def _cenario(monkeypatch):
    """Etapas + Corretor no banco de teste; config no default (ligado, Pessoa12)."""
    with SessionLocal() as s:
        for sid, nome, funil, ordem in (
            (6, "Novo", "principal", 1),
            (ab.STAGE_ENTROU, "Em atendimento", "principal", 2),
            (STAGE_RESPONDEU, "Respondeu", "remarketing", 9),
        ):
            s.execute(sql_text(
                "INSERT INTO pipeline_stages (id, tenant_id, nome, funil, ordem) "
                "SELECT :id, 1, :n, :f, :o "
                "WHERE NOT EXISTS (SELECT 1 FROM pipeline_stages WHERE id = :id)"
            ), {"id": sid, "n": nome, "f": funil, "o": ordem})
        s.execute(sql_text(
            "INSERT INTO corretoras (id, nome, telefone, telefone_normalizado, ativo) "
            "VALUES (:id, 'Corretor', '+5500000000000', '+5500000000000', true) "
            "ON CONFLICT (id) DO UPDATE SET nome = 'Corretor', ativo = true"
        ), {"id": CORRETORA_CORRETOR})
        s.commit()
    monkeypatch.setattr(config_operacional, "_carregar", lambda: {})
    monkeypatch.setattr(config_operacional.settings, "handoff_remarketing_ativo", True)
    monkeypatch.setattr(config_operacional.settings, "handoff_remarketing_corretora", CORRETOR)
    yield


def _tel() -> str:
    return "5515" + "".join(random.choice("0123456789") for _ in range(9))


def _novo_lead(telefone: str, owner: int = CORRETORA_CORRETOR,
               stage: int = STAGE_RESPONDEU) -> int:
    with SessionLocal() as s:
        lead = Lead(tenant_id=1, nome="Teste tag", telefone=telefone,
                    owner_id=owner, stage_id=stage,
                    external_ref=f"tagrmk:{telefone}")
        s.add(lead)
        s.commit()
        return int(lead.id)


def _marca(lead_id: int) -> datetime | None:
    with SessionLocal() as s:
        return s.get(Lead, lead_id).veio_remarketing_em


# ── 1 e 2: os dois caminhos de transferência marcam ─────────────────────────

def test_handoff_automatico_marca_o_lead():
    tel = _tel()
    lid = _novo_lead(tel)

    res = hr.processar(tel, ["pode mandar pra mim, até 450 mil"], nome="Dona Pessoa")

    assert res["transferiu"] is True and res["leads"] == [lid]
    assert _marca(lid) is not None, "lead transferido tem que chegar marcado"


def test_promocao_da_pessoa_marca_o_lead():
    """`registrar_qualificacao` → `promover_para_vendas`: o handoff rico."""
    tel = _tel()
    lid = _novo_lead(tel)

    assert ab.promover_para_vendas(tel, "Dona Pessoa") is True

    with SessionLocal() as s:
        lead = s.get(Lead, lid)
        assert lead.owner_id == ab.CORRETORA_CORRETORA_A
        assert lead.stage_id == ab.STAGE_ENTROU
        assert lead.veio_remarketing_em is not None


# ── 3: procedência não se inventa ───────────────────────────────────────────

def test_lead_que_nunca_foi_do_remarketing_nao_e_marcado():
    """Botão do painel movendo lead do funil principal — sem tag."""
    tel = _tel()
    lid = _novo_lead(tel, owner=CORRETOR, stage=6)

    with SessionLocal() as s:
        lead = s.get(Lead, lid)
        transferir_lead(s, lead, CORRETOR, de_cid=CORRETOR, quem="dona", motivo="teste")
        s.commit()

    assert _marca(lid) is None


def test_promover_para_vendas_nao_marca_quem_ja_esta_no_funil_principal():
    tel = _tel()
    lid = _novo_lead(tel, owner=CORRETOR, stage=6)

    ab.promover_para_vendas(tel, "Fulano")

    assert _marca(lid) is None


# ── 4: escrita única ────────────────────────────────────────────────────────

def test_marca_nao_e_recarimbada():
    """A data que vale é a da PRIMEIRA saída do remarketing."""
    tel = _tel()
    lid = _novo_lead(tel)
    antiga = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    with SessionLocal() as s:
        lead = s.get(Lead, lid)
        lead.veio_remarketing_em = antiga
        lead.stage_id = STAGE_RESPONDEU  # volta pro remarketing
        lead.owner_id = CORRETORA_CORRETOR
        s.commit()

    ab.promover_para_vendas(tel, "Fulano")

    depois = _marca(lid)
    assert depois is not None
    assert abs(depois - antiga) < timedelta(seconds=1), "recarimbou a data"


# ── 5: a marca chega na tela ────────────────────────────────────────────────

@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(element, compiler, **kw):  # noqa: ANN001
    return "JSON"


@compiles(BigInteger, "sqlite")
def _compile_bigint_sqlite(element, compiler, **kw):  # noqa: ANN001
    return "INTEGER"


AGORA = datetime.now(timezone.utc)


@pytest.fixture()
def client_board(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(
        engine,
        tables=[PipelineStage.__table__, Lead.__table__, LeadEvent.__table__],
    )
    factory = sessionmaker(bind=engine)
    with factory() as s:
        st = PipelineStage(tenant_id=1, nome="Novo", ordem=1, tipo="aberta")
        s.add(st)
        s.flush()
        s.add_all([
            Lead(tenant_id=1, external_ref="k1", nome="Veio do Remarketing",
                 telefone="+5500000000000", owner_id=3, stage_id=st.id,
                 created_at=AGORA, updated_at=AGORA, veio_remarketing_em=AGORA),
            Lead(tenant_id=1, external_ref="k2", nome="Lead Novo",
                 telefone="+5500000000000", owner_id=3, stage_id=st.id,
                 created_at=AGORA, updated_at=AGORA),
        ])
        s.commit()
    monkeypatch.setattr(painel, "SessionLocal", factory)
    return TestClient(app)


def test_board_devolve_a_tag_no_card(client_board):
    r = client_board.get("/painel/crm/board", headers={"X-Painel-User": "dona"})
    assert r.status_code == 200
    cards = {c["nome"]: c for col in r.json()["colunas"] for c in col["cards"]}

    marcado = cards["Veio do Remarketing"]
    assert marcado["veio_remarketing"] is True
    assert marcado["veio_remarketing_em"] is not None

    assert cards["Lead Novo"]["veio_remarketing"] is False
    assert cards["Lead Novo"]["veio_remarketing_em"] is None
