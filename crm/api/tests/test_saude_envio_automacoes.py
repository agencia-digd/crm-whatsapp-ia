"""Saúde de envio por automação — "ligado" e "funcionando" são coisas diferentes.

A nutrição aparecia LIGADA no painel havia semanas sem nunca ter mandado uma
mensagem real (35 eventos, todos dry-run, todos de 22/07). Nenhum erro em log,
nenhum número errado na tela: só ausência se passando por normalidade.

Cobre:
  - só conta o que SAIU DE VERDADE (`payload.sent = true`) — dry-run não conta;
  - `nunca_enviou` é diferente de "faz 0 dias": automação que nasceu morta tem
    campo próprio e alerta sempre;
  - o limiar é POR AUTOMAÇÃO (a nutrição tem ritmo de 30 dias por lead e não
    pode dar alarme falso no mesmo prazo do follow-up, que é diário);
  - o toque do follow-up não é contado como nutrição e vice-versa;
  - tabela ausente vira "sem dado" naquela linha, não derruba o mapa.

DB isolado: SQLite in-memory.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import BigInteger, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.api.automacoes as automacoes
from app.db.models import Base, CadenciaEstado, CadenciaEvento, Lead, LeadEvent
from app.services import cadencia as cad


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(element, compiler, **kw):  # noqa: ANN001
    return "JSON"


@compiles(BigInteger, "sqlite")
def _compile_bigint_sqlite(element, compiler, **kw):  # noqa: ANN001
    return "INTEGER"


AGORA = datetime.now(timezone.utc)


@pytest.fixture()
def factory():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(
        engine,
        tables=[
            CadenciaEstado.__table__,
            CadenciaEvento.__table__,
            Lead.__table__,
            LeadEvent.__table__,
        ],
    )
    return sessionmaker(bind=engine)


def _estado(s) -> CadenciaEstado:
    est = CadenciaEstado(
        tenant_id=1, telefone="+5500000000000", corretora_id=3, toque_atual=0,
        status="ativa", proximo_toque_em=AGORA, atualizado_em=AGORA,
    )
    s.add(est)
    s.flush()
    return est


def _msg(s, est, *, at, toque=None, sent=True, nutricao=False):
    payload = {"sent": sent}
    if nutricao:
        payload.update({"nutricao": True, "marco": "30 dias"})
    s.add(CadenciaEvento(tenant_id=1, cadencia_id=est.id, toque=toque,
                         tipo=cad.TIPO_MENSAGEM, canal=cad.CANAL_WHATSAPP,
                         payload=payload, at=at))
    s.flush()


def _lead_event(s, tipo, *, at, sent=True):
    lead = Lead(tenant_id=1, nome="x", telefone="+5500000000000")
    s.add(lead)
    s.flush()
    s.add(LeadEvent(tenant_id=1, lead_id=lead.id, tipo=tipo,
                    payload={"sent": sent}, at=at))
    s.flush()


def test_nunca_enviou_nao_e_zero_dias(factory):
    with factory() as s:
        saude = automacoes._saude_envio(s)

    nut = saude["nutricao"]
    assert nut["nunca_enviou"] is True
    assert nut["ultimo_envio_real"] is None
    assert nut["dias"] is None
    assert nut["alerta"] is True


def test_dry_run_nao_conta_como_envio(factory):
    """Era exatamente o caso da nutrição: 35 eventos, zero mensagens reais."""
    with factory() as s:
        est = _estado(s)
        _msg(s, est, at=AGORA - timedelta(hours=2), sent=False, nutricao=True)
        s.commit()
        saude = automacoes._saude_envio(s)

    assert saude["nutricao"]["nunca_enviou"] is True


def test_envio_real_recente_nao_alerta(factory):
    with factory() as s:
        est = _estado(s)
        _msg(s, est, at=AGORA - timedelta(hours=3), sent=True, nutricao=True)
        s.commit()
        saude = automacoes._saude_envio(s)

    nut = saude["nutricao"]
    assert nut["nunca_enviou"] is False
    assert nut["dias"] == 0
    assert nut["alerta"] is False


def test_limiar_e_por_automacao(factory):
    """5 dias parada: alerta no follow-up (ritmo diário), não na nutrição
    (marco de 30 em 30 dias por lead)."""
    toque = [t.indice for t in cad.TOQUES if t.tipo == cad.TIPO_MENSAGEM][0]
    with factory() as s:
        est = _estado(s)
        _msg(s, est, at=AGORA - timedelta(days=5), toque=toque, sent=True)
        _msg(s, est, at=AGORA - timedelta(days=5, hours=1), sent=True, nutricao=True)
        s.commit()
        saude = automacoes._saude_envio(s)

    assert saude["follow-up"]["dias"] == 5
    assert saude["follow-up"]["alerta"] is True
    assert saude["nutricao"]["dias"] == 5
    assert saude["nutricao"]["alerta"] is False
    assert saude["nutricao"]["limiar_dias"] > saude["follow-up"]["limiar_dias"]


def test_nutricao_alerta_depois_do_limiar_dela(factory):
    with factory() as s:
        est = _estado(s)
        _msg(s, est, at=AGORA - timedelta(days=14), sent=True, nutricao=True)
        s.commit()
        saude = automacoes._saude_envio(s)

    assert saude["nutricao"]["dias"] == 14
    assert saude["nutricao"]["alerta"] is True


def test_followup_e_nutricao_nao_se_misturam(factory):
    """Toque da régua tem `toque` preenchido; marco de nutrição grava toque=None
    com payload.nutricao — cada um conta no seu."""
    toque = [t.indice for t in cad.TOQUES if t.tipo == cad.TIPO_MENSAGEM][0]
    with factory() as s:
        est = _estado(s)
        _msg(s, est, at=AGORA - timedelta(hours=1), toque=toque, sent=True)
        s.commit()
        saude = automacoes._saude_envio(s)

    assert saude["follow-up"]["nunca_enviou"] is False
    assert saude["nutricao"]["nunca_enviou"] is True


def test_boas_vindas_le_do_lead_events(factory):
    with factory() as s:
        _lead_event(s, "boas_vindas_enviada", at=AGORA - timedelta(hours=4))
        s.commit()
        saude = automacoes._saude_envio(s)

    assert saude["boas-vindas"]["nunca_enviou"] is False
    assert saude["boas-vindas"]["alerta"] is False


def test_todas_as_automacoes_aparecem(factory):
    with factory() as s:
        saude = automacoes._saude_envio(s)

    assert set(saude) == {"follow-up", "nutricao", "boas-vindas", "remarketing"}
    for chave, item in saude.items():
        assert item["limiar_dias"] >= 1, chave
        assert "fonte" in item
