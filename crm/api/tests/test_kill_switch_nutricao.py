"""Kill-switch PRÓPRIO da nutrição (`nutricao_ativa`) — separado da cadência 05/08.

Até aqui a nutrição lia `cadencia_ativa`: desligar o follow-up parava os marcos
de 30/60/90/120/150 dias junto, em silêncio. São riscos diferentes — o follow-up
fala com lead quente, a nutrição com base fria de meses.

Cobre exatamente as três garantias que a separação tem que dar:
  1. DEFAULT HERDADO: sem linha no banco, `nutricao_ativa` devolve o mesmo que
     `cadencia_ativa` — inclusive quando a cadência foi desligada PELO PAINEL
     (herda o valor efetivo, não o `.env`). Nada muda de estado na migração;
  2. desligar o FOLLOW-UP não para mais a NUTRIÇÃO (com a chave nova gravada);
  3. desligar a NUTRIÇÃO não para o FOLLOW-UP.

Os dois motores rodam de verdade aqui (nada de mock do tick): é o único jeito de
provar independência — asserção sobre a função de config sozinha passaria mesmo
se alguém esquecesse de trocar o `if` dentro do `nutricao.tick`.

DB isolado: SQLite in-memory. Sender fake — nunca chama o motor real.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import BigInteger, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from app.db.models import (
    Base,
    CadenciaEstado,
    CadenciaEvento,
    DisparoReengajamento,
    Lead,
    LeadEvent,
    PipelineStage,
)
from app.services import cadencia, config_operacional, nutricao


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(element, compiler, **kw):  # noqa: ANN001
    return "JSON"


@compiles(BigInteger, "sqlite")
def _compile_bigint_sqlite(element, compiler, **kw):  # noqa: ANN001
    return "INTEGER"


T0 = datetime(2026, 8, 5, 15, 0, tzinfo=timezone.utc)
FONE = "+5500000000000"


@pytest.fixture()
def session_factory():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(
        engine,
        tables=[
            CadenciaEstado.__table__,
            CadenciaEvento.__table__,
            Lead.__table__,
            LeadEvent.__table__,
            PipelineStage.__table__,
            # opt-out (07/10): a nutrição lê disparos_reengajamento a cada
            # volta e, sem a tabela, segura todo marco (fail-closed).
            DisparoReengajamento.__table__,
        ],
    )
    return sessionmaker(bind=engine)


@pytest.fixture(autouse=True)
def _reset_guard():
    cadencia._LAST_ENQUEUE["at"] = 0.0
    yield
    cadencia._LAST_ENQUEUE["at"] = 0.0


@pytest.fixture()
def config(monkeypatch):
    """Simula as linhas de `config_operacional` sem banco.

    Devolve um setter: `config({"cadencia_ativa": False})` = como se a Dona
    tivesse desligado no painel. Chave ausente = sem linha no banco (default).
    """

    def _set(linhas: dict) -> None:
        monkeypatch.setattr(config_operacional, "_cache", dict(linhas))
        monkeypatch.setattr(config_operacional, "_cache_em", 9e18)  # cache eterno

    _set({})
    return _set


class FakeSender:
    def __init__(self):
        self.chamadas: list[dict] = []

    def __call__(self, telefone, texto, *, com_botao):
        self.chamadas.append({"telefone": telefone, "texto": texto, "com_botao": com_botao})
        return {"ok": True, "dry_run": True, "sent": False}


def _seed_marco_devido(s):
    """Lead que esgotou a cadência, já na etapa Base, com marco vencido."""
    stage_base = PipelineStage(tenant_id=1, nome=nutricao.STAGE_BASE, ordem=1,
                               funil=nutricao.FUNIL_REMARKETING)
    s.add(stage_base)
    s.flush()
    est = CadenciaEstado(
        tenant_id=1,
        telefone=FONE,
        toque_atual=9,
        status="remarketing",
        proximo_toque_em=T0 - timedelta(minutes=5),
        criado_em=T0 - timedelta(days=40),
        atualizado_em=T0,
    )
    lead = Lead(tenant_id=1, nome="Lead Nutrição", telefone=f"+{FONE}",
                stage_id=stage_base.id)
    s.add_all([est, lead])
    s.commit()
    return est, lead


# ── 1. default herdado ───────────────────────────────────────────────────────


def test_default_herda_cadencia_ligada(config):
    config({})  # nenhuma linha no banco
    assert config_operacional.cadencia_ativa() is True
    assert config_operacional.nutricao_ativa() is True


def test_default_herda_cadencia_desligada_pelo_painel(config):
    """Herda o valor EFETIVO, não o `.env`: se a cadência está desligada no
    painel e ninguém gravou a chave nova, a nutrição segue desligada — que é o
    comportamento de antes da separação."""
    config({"cadencia_ativa": False})
    assert config_operacional.cadencia_ativa() is False
    assert config_operacional.nutricao_ativa() is False


def test_chave_gravada_ganha_do_herdado(config):
    config({"cadencia_ativa": False, "nutricao_ativa": True})
    assert config_operacional.cadencia_ativa() is False
    assert config_operacional.nutricao_ativa() is True


def test_estado_atual_expoe_a_chave_nova(config):
    config({"nutricao_ativa": False})
    estado = config_operacional.estado_atual()
    assert estado["nutricao_ativa"]["valor"] is False
    assert estado["nutricao_ativa"]["personalizado"] is True
    assert estado["cadencia_ativa"]["personalizado"] is False


# ── 2. desligar o follow-up NÃO para mais a nutrição ─────────────────────────


def test_followup_desligado_nutricao_segue_disparando(session_factory, config):
    config({"cadencia_ativa": False, "nutricao_ativa": True})
    sender = FakeSender()
    with session_factory() as s:
        _seed_marco_devido(s)
        res = nutricao.tick(agora=T0, session=s, sender=sender)

    assert res.marcos_disparados == 1
    assert len(sender.chamadas) == 1


def test_nutricao_desligada_nao_dispara_marco(session_factory, config):
    config({"cadencia_ativa": True, "nutricao_ativa": False})
    sender = FakeSender()
    with session_factory() as s:
        _seed_marco_devido(s)
        res = nutricao.tick(agora=T0, session=s, sender=sender)

    assert res.marcos_disparados == 0
    assert res.entradas == 0
    assert sender.chamadas == []


# ── 3. desligar a nutrição NÃO para o follow-up ──────────────────────────────


def test_nutricao_desligada_followup_segue_enviando(session_factory, config):
    config({"cadencia_ativa": True, "nutricao_ativa": False})
    sender = FakeSender()
    with session_factory() as s:
        cadencia.iniciar_cadencia(
            s, telefone=f"+{FONE}", lead_id="kommo_1", corretora_id=3, agora=T0
        )
        s.commit()
        cadencia._LAST_ENQUEUE["at"] = 0.0
        res = cadencia.tick(agora=T0, session=s, sender=sender)

    assert res.mensagens_enfileiradas >= 1
    assert len(sender.chamadas) >= 1


def test_as_duas_desligadas_ninguem_dispara(session_factory, config):
    config({"cadencia_ativa": False, "nutricao_ativa": False})
    sender = FakeSender()
    with session_factory() as s:
        _seed_marco_devido(s)
        res_nut = nutricao.tick(agora=T0, session=s, sender=sender)
        cadencia.iniciar_cadencia(
            s, telefone="+5500000000000", lead_id="kommo_2", corretora_id=3, agora=T0
        )
        s.commit()
        res_cad = cadencia.tick(agora=T0, session=s, sender=sender)

    assert res_nut.marcos_disparados == 0
    assert res_cad.mensagens_enfileiradas == 0
    assert sender.chamadas == []
