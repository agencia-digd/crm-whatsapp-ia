"""Testes da ENTRADA DO LEAD (Copiloto 2.0 · P0) — serviço + endpoint.

Cobre (lei Dona 10/07 — boas-vindas SEPARADA da cadência):
  - flag `lead_intake_ativo` desligada (default) → não faz NADA;
  - lead novo → espinha canônica etapa "Novo" + roleta (Pessoa12) + boas-vindas UMA
    vez + evento 'intake' — e NENHUMA matrícula em cadência (a cadência só
    liga quando a Pessoa12 arrastar o lead pro Follow-up);
  - idempotência por telefone normalizado: 2 intakes → 1 lead, 1 boas-vindas;
  - envio falhou → evento não gravado (próximo webhook re-tenta);
  - telefone legado do Kommo com '0' na frente do DDD → normaliza;
  - telefone inválido → ValueError (endpoint traduz pra 422);
  - lead existente NÃO regride de etapa nem troca de dona (e não recebe
    boas-vindas — não "caiu em Novo" agora);
  - round-robin da roleta pronto pra N corretoras (patch da lista elegível);
  - endpoint POST /intake/lead: auth fail-closed (X-Intake-Token) + flag 503.

DB isolado: SQLite in-memory (mesmo padrão de test_cadencia_auto_enroll).
Zero prod, zero envio — sender fake injetado; o default fica atrás de
settings.dry_run.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from sqlalchemy import BigInteger, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.db.models import (
    Base,
    CadenciaEstado,
    CadenciaEvento,
    Corretora,
    Lead,
    LeadEvent,
    PipelineStage,
)
from app.services import lead_intake
from app.services.lead_intake import (
    BOAS_VINDAS_TEXTO,
    EVENT_BOAS_VINDAS,
    STAGE_ENTRADA,
    IntakeResult,
    intake_lead,
)
from app.services.phone import normalize_phone_kommo


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(element, compiler, **kw):  # noqa: ANN001
    return "JSON"


@compiles(BigInteger, "sqlite")
def _compile_bigint_sqlite(element, compiler, **kw):  # noqa: ANN001
    return "INTEGER"


# Mesmos valores canônicos do seed de tests/conftest.py / painel.py
CORRETOR_ID = 1
CORRETOR_ID = 2
CORRETOR_ID = 3


@pytest.fixture(autouse=True)
def _roleta_padrao(monkeypatch):
    """O .env real muda a roleta no dia a dia (ex: 'hoje só a Pessoa11').
    Os testes cravam o padrão de projeto (Pessoa10+Pessoa11) pra não depender do env."""
    monkeypatch.setattr(lead_intake.settings, "roleta_corretoras", [CORRETOR_ID, CORRETOR_ID])



@pytest.fixture()
def session_factory():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(
        engine,
        tables=[
            Corretora.__table__,
            PipelineStage.__table__,
            Lead.__table__,
            LeadEvent.__table__,
            CadenciaEstado.__table__,
            CadenciaEvento.__table__,
        ],
    )
    factory = sessionmaker(bind=engine)
    with factory() as s:
        s.add_all(
            [
                Corretora(
                    id=CORRETOR_ID, nome="Pessoa5", telefone="+5500000000000",
                    kommo_user_id=10000006, evolution_instance="acme_corretora_c",
                    ativo=True,
                ),
                # A Pessoa1 FALTAVA neste seed. `corretoras_roleta_validas`
                # filtra a roleta pelas corretoras que EXISTEM e estão ativas —
                # sem a Pessoa11 no banco, a roleta [1,2] colapsava em [1] e
                # test_roleta_corretor_corretor_corretor_fora esperava [1,2,1,2] mas recebia
                # [1,1,1,1]. Passava despercebido porque o `.env` de produção
                # tinha ROLETA_CORRETORAS=[2] e o teste nunca rodava a regra
                # que documenta.
                Corretora(
                    id=CORRETOR_ID, nome="Pessoa1", telefone="+5500000000000",
                    kommo_user_id=10000007, evolution_instance="acme_corretor",
                    ativo=True,
                ),
                Corretora(
                    id=CORRETOR_ID, nome="Pessoa3", telefone="+5500000000000",
                    kommo_user_id=10000003, evolution_instance="acme_corretora_a",
                    ativo=True, papel="corretora_atendedora_de_dona",
                ),
            ]
        )
        s.commit()
    return factory


@pytest.fixture()
def flag_ligada():
    with patch.object(settings, "lead_intake_ativo", True):
        yield


class FakeSender:
    """Sender fake do contrato lead_intake.SenderFn — zero envio real."""

    def __init__(self, resposta=None, exc=None):
        self.chamadas: list[dict] = []
        self.resposta = resposta or {"ok": True, "dry_run": True, "sent": False}
        self.exc = exc

    def __call__(self, telefone, texto):
        self.chamadas.append({"telefone": telefone, "texto": texto})
        if self.exc is not None:
            raise self.exc
        return self.resposta


def _intake(session_factory, telefone="+5500000000000", sender=None, **over):
    kwargs = dict(
        nome="Lead Teste",
        telefone=telefone,
        origem="meta_leadgen",
        external_ref="lg_123",
        session_factory=session_factory,
        sender=sender or FakeSender(),
    )
    kwargs.update(over)
    return intake_lead(**kwargs)


def _all(session_factory, model):
    with session_factory() as s:
        return s.query(model).order_by(model.id).all()


# ── 1. Flag desligada (default) = não faz nada ────────────────────────────────

def test_flag_desligada_nao_faz_nada(session_factory):
    # Patch explícito em vez de assertar o ambiente: o `.env` de produção define
    # LEAD_INTAKE_ATIVO e já deixou este teste vermelho quando a Dona ligou a
    # flag (mesmo padrão de test_endpoint_fail_closed_sem_token_configurado
    # logo abaixo, que aprendeu a lição pro token e não pra flag).
    # O conftest também pina a env em import-time — este patch é a garantia
    # local de que o teste afirma o COMPORTAMENTO com a flag off, não o estado
    # do servidor.
    with patch.object(settings, "lead_intake_ativo", False):
        assert settings.lead_intake_ativo is False
        assert _intake(session_factory) is None
    assert _all(session_factory, Lead) == []
    assert _all(session_factory, CadenciaEstado) == []
    assert _all(session_factory, LeadEvent) == []


# ── 2. Lead novo: espinha + roleta + boas-vindas 1x + evento ─────────────────

def test_intake_lead_novo(session_factory, flag_ligada):
    # REGRA DA DONA 26/07: lead novo → Pessoa10/Pessoa11 (roleta), SEM boas-vindas
    # automática (a 2591 é linha de remarketing; Pessoa10/Pessoa11 contatam manual).
    sender = FakeSender()
    res = _intake(session_factory, sender=sender)
    assert isinstance(res, IntakeResult)
    assert res.ja_existia is False
    assert res.corretora_id == CORRETOR_ID        # 1ª da roleta (Pessoa10, Pessoa11)
    assert res.stage == STAGE_ENTRADA
    assert res.boas_vindas_enviada is False

    leads = _all(session_factory, Lead)
    assert len(leads) == 1
    lead = leads[0]
    assert lead.telefone == "+5500000000000"  # canônico da espinha (com '+', padrão Kommo)
    assert lead.nome == "Lead Teste"
    assert lead.origem == "meta_leadgen"
    assert lead.external_ref == "lg_123"
    assert lead.owner_id == CORRETOR_ID
    assert lead.tenant_id == 1

    # etapa "Novo" resolvida por nome no catálogo canônico (tenant 1)
    with session_factory() as s:
        stage = s.get(PipelineStage, lead.stage_id)
        assert stage is not None
        assert stage.nome == STAGE_ENTRADA
        assert stage.tenant_id == 1

    # NÃO matricula em cadência (lei Dona 10/07): boas-vindas é separada;
    # a cadência só liga quando a Pessoa12 arrastar o lead pro Follow-up.
    assert _all(session_factory, CadenciaEstado) == []

    # boas-vindas NÃO sai pra lead de Pessoa10/Pessoa11 (linha 2591 = só remarketing)
    assert sender.chamadas == []

    # evento de auditoria: só o intake
    eventos = _all(session_factory, LeadEvent)
    assert [e.tipo for e in eventos] == ["intake"]
    ev = eventos[0]
    assert ev.lead_id == lead.id
    assert ev.payload["corretora_id"] == CORRETOR_ID
    assert ev.payload["boas_vindas_enviada"] is False
    assert ev.payload["ja_existia"] is False


# ── 3. Idempotência por telefone normalizado ──────────────────────────────────

def test_idempotente_mesmo_telefone(session_factory, flag_ligada, monkeypatch):
    # idempotência da boas-vindas exige lead na linha de automação (Pessoa12)
    monkeypatch.setattr(lead_intake.settings, "roleta_corretoras", [CORRETOR_ID])
    sender = FakeSender()
    res1 = _intake(session_factory, sender=sender)
    # 2º webhook: mesmo telefone em OUTRO formato, outro external_ref
    res2 = _intake(
        session_factory, telefone="+5500000000000", external_ref="lg_999", sender=sender
    )

    assert res1.ja_existia is False
    assert res2.ja_existia is True
    assert res2.lead_id == res1.lead_id
    # boas-vindas UMA vez só: 1º intake envia, 2º NÃO reenvia
    assert res1.boas_vindas_enviada is True
    assert res2.boas_vindas_enviada is False
    assert len(sender.chamadas) == 1

    assert len(_all(session_factory, Lead)) == 1
    assert _all(session_factory, CadenciaEstado) == []
    # auditoria: 1 boas_vindas + 1 intake por webhook (append-only)
    eventos = _all(session_factory, LeadEvent)
    assert sorted(e.tipo for e in eventos) == [EVENT_BOAS_VINDAS, "intake", "intake"]

    # external_ref original preservado (não sobrescreve o que já existia)
    assert _all(session_factory, Lead)[0].external_ref == "lg_123"


def test_envio_falhou_nao_grava_evento_e_retenta(session_factory, flag_ligada, monkeypatch):
    # mecânica da boas-vindas (só na linha de automação) — força roleta na Pessoa12
    monkeypatch.setattr(lead_intake.settings, "roleta_corretoras", [CORRETOR_ID])
    # 1º webhook: sender explode → lead entra na espinha, MAS sem evento de
    # boas-vindas (o próximo webhook re-tenta o envio).
    quebrado = FakeSender(exc=RuntimeError("cloud fora"))
    res1 = _intake(session_factory, sender=quebrado)
    assert res1.boas_vindas_enviada is False
    assert len(_all(session_factory, Lead)) == 1
    # Sem o evento de idempotência; a falha fica gravada com a corretora
    # (06/10/2026 — a tela conta o que falhou, não só o log).
    tipos = [e.tipo for e in _all(session_factory, LeadEvent)]
    assert sorted(tipos) == ["boas_vindas_falhou", "intake"]
    assert lead_intake.EVENT_BOAS_VINDAS not in tipos

    # 2º webhook: sender saudável → agora envia e grava
    ok = FakeSender()
    res2 = _intake(session_factory, sender=ok)
    assert res2.ja_existia is True
    assert res2.boas_vindas_enviada is True
    assert len(ok.chamadas) == 1


def test_telefone_legado_com_zero_normaliza(session_factory, flag_ligada):
    res = _intake(session_factory, telefone="+5500000000000")
    assert res.telefone == "+5500000000000"
    assert _all(session_factory, Lead)[0].telefone == "+5500000000000"


def test_telefone_invalido_levanta(session_factory, flag_ligada):
    with pytest.raises(ValueError):
        _intake(session_factory, telefone="999")
    assert _all(session_factory, Lead) == []
    assert _all(session_factory, CadenciaEstado) == []


# ── 4. Lead existente não regride de etapa nem troca de dona ─────────────────

def test_lead_existente_nao_regride_stage_nem_reroleta(session_factory, flag_ligada):
    with session_factory() as s:
        stage_atendimento = PipelineStage(tenant_id=1, nome="Em atendimento")
        s.add(stage_atendimento)
        s.flush()
        s.add(
            Lead(
                tenant_id=1, external_ref="kommo_1", nome="Já Cliente",
                telefone="+5500000000000", owner_id=CORRETOR_ID,
                stage_id=stage_atendimento.id,
            )
        )
        s.commit()
        stage_id_antes = stage_atendimento.id

    sender = FakeSender()
    res = _intake(session_factory, sender=sender)
    assert res.ja_existia is True
    assert res.corretora_id == CORRETOR_ID        # dona mantida — sem re-roleta
    # já avançou de etapa → NÃO "caiu em Novo" agora → sem boas-vindas
    assert res.boas_vindas_enviada is False
    assert sender.chamadas == []

    lead = _all(session_factory, Lead)[0]
    assert lead.stage_id == stage_id_antes    # NÃO voltou pra "Novo"
    assert lead.owner_id == CORRETOR_ID
    assert lead.nome == "Lead Teste"          # dados que vieram são atualizados


# ── 5. Roleta: round-robin pronto pra N corretoras ────────────────────────────

def test_roleta_corretor_corretor_corretor_fora(session_factory, flag_ligada):
    # REGRA DA DONA 26/07: round-robin Pessoa10↔Pessoa11; Pessoa12 NUNCA recebe lead novo.
    atribs = [
        _intake(session_factory, telefone=f"+5500000000000{i}", external_ref=None).corretora_id
        for i in range(4)
    ]
    assert atribs == [CORRETOR_ID, CORRETOR_ID, CORRETOR_ID, CORRETOR_ID]
    assert CORRETOR_ID not in atribs


def test_roleta_round_robin_com_duas(session_factory, flag_ligada):
    with patch.object(lead_intake.settings, "roleta_corretoras", [CORRETOR_ID, CORRETOR_ID]):
        atribuicoes = [
            _intake(session_factory, telefone=f"+5500000000000{i}", external_ref=None).corretora_id
            for i in range(4)
        ]
    # 1º lead sem histórico → primeira da lista; depois alterna circular
    assert atribuicoes == [CORRETOR_ID, CORRETOR_ID, CORRETOR_ID, CORRETOR_ID]


# ── 6. normalize_phone_kommo (helper compartilhado) ───────────────────────────

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("+5500000000000", "+5500000000000"),
        ("5500000000000", "+5500000000000"),
        ("+5500000000000", "+5500000000000"),   # legado Kommo: 0 + DDD
        ("0+5500000000000", "+5500000000000"),
        ("", None),
        (None, None),
        ("abc", None),
        ("999", None),
    ],
)
def test_normalize_phone_kommo(raw, expected):
    assert normalize_phone_kommo(raw) == expected


# ── 7. Endpoint POST /intake/lead — auth fail-closed + flag ──────────────────

@pytest.fixture()
def client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.intake import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


_PAYLOAD = {"telefone": "+5500000000000", "nome": "Lead Teste", "origem": "meta_leadgen"}


def test_endpoint_fail_closed_sem_token_configurado(client):
    # token vazio explícito (default do settings) → endpoint FECHADO mesmo com
    # header. Patch em vez de assert do ambiente: o .env de dev/prod define
    # INTAKE_TOKEN e o teste não pode depender disso.
    with patch.object(settings, "intake_token", ""):
        resp = client.post("/intake/lead", json=_PAYLOAD, headers={"X-Intake-Token": "qualquer"})
    assert resp.status_code == 401


def test_endpoint_token_errado_401(client):
    with patch.object(settings, "intake_token", "s3cr3t"):
        assert client.post("/intake/lead", json=_PAYLOAD).status_code == 401
        resp = client.post("/intake/lead", json=_PAYLOAD, headers={"X-Intake-Token": "errado"})
        assert resp.status_code == 401


def test_endpoint_flag_desligada_503(client):
    # flag patchada explicitamente (não herdada do .env) — ver nota em
    # test_flag_desligada_nao_faz_nada.
    with patch.object(settings, "intake_token", "s3cr3t"), \
         patch.object(settings, "lead_intake_ativo", False):
        resp = client.post("/intake/lead", json=_PAYLOAD, headers={"X-Intake-Token": "s3cr3t"})
    assert resp.status_code == 503


def test_endpoint_ok_delega_pro_servico(client, flag_ligada):
    fake = IntakeResult(
        lead_id=7, telefone="+5500000000000", corretora_id=CORRETOR_ID,
        boas_vindas_enviada=True, stage=STAGE_ENTRADA, ja_existia=False,
    )
    with patch.object(settings, "intake_token", "s3cr3t"), \
         patch("app.api.intake.intake_lead", return_value=fake) as mock_svc:
        resp = client.post("/intake/lead", json=_PAYLOAD, headers={"X-Intake-Token": "s3cr3t"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["lead_id"] == 7
    assert body["boas_vindas_enviada"] is True
    assert body["corretora_id"] == CORRETOR_ID
    mock_svc.assert_called_once_with(
        nome="Lead Teste", telefone="+5500000000000",
        origem="meta_leadgen", external_ref=None,
    )


def test_endpoint_telefone_invalido_422(client, flag_ligada):
    with patch.object(settings, "intake_token", "s3cr3t"), \
         patch("app.api.intake.intake_lead", side_effect=ValueError("telefone inválido")):
        resp = client.post("/intake/lead", json=_PAYLOAD, headers={"X-Intake-Token": "s3cr3t"})
    assert resp.status_code == 422
