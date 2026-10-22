"""D0 da cadência é SEMPRE o reencontro (lei Dona 10/07).

A boas-vindas de cadastro virou AUTOMAÇÃO SEPARADA (app/services/lead_intake.py):
o intake envia UMA mensagem quando o lead cai em "Novo" e AGUARDA a Pessoa12 — NÃO
matricula em cadência. A cadência só liga quando a Pessoa12 arrasta o lead pro
Follow-up, e aí o D0 é o reencontro padrão (`_TEMPLATES_MSG[0]`).

Cobre:
  - intake NÃO matricula em cadência (zero CadenciaEstado; tick não toca nada);
  - lead matriculado (iniciar_cadencia) → D0 = reencontro, mesmo existindo
    `lead_events(tipo='intake')` pro telefone (o D0-duplo morreu);
  - `iniciar_cadencia` não aceita mais o param `origem` (API antiga morta);
  - símbolos do D0-duplo (`_TEMPLATES_MSG_INTAKE`, `_veio_de_intake`) não
    existem mais em `app.services.cadencia`;
  - a copy da boas-vindas vive no intake (`BOAS_VINDAS_TEXTO`) e termina
    com "?" (regra de toda mensagem que pede próxima ação).

DB isolado: SQLite in-memory (mesmo padrão de test_lead_intake). Zero prod,
zero envio — senders fake, tick em DRY-RUN.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
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
    DisparoReengajamento,
    Lead,
    LeadEvent,
    PipelineStage,
)
from app.services import cadencia, lead_intake
from app.services.lead_intake import BOAS_VINDAS_TEXTO, intake_lead


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(element, compiler, **kw):  # noqa: ANN001
    return "JSON"


@compiles(BigInteger, "sqlite")
def _compile_bigint_sqlite(element, compiler, **kw):  # noqa: ANN001
    return "INTEGER"


CORRETOR_ID = 3

D0 = datetime(2026, 7, 10, 9, 0, 0, tzinfo=timezone.utc)


def _depois(minutos: int = 1) -> datetime:
    return datetime.now(timezone.utc) + timedelta(minutes=minutos)


@pytest.fixture()
def session_factory(monkeypatch):
    """Espinha completa (leads/lead_events/stages) + cadência.

    Recebe `monkeypatch` pra cravar a roleta na Pessoa12: este arquivo só semeia a
    Pessoa3, e a boas-vindas do intake (que os testes abaixo asseram) só sai
    pra lead da linha de automação (Pessoa12). O `.env` de produção muda a roleta no
    dia a dia — ROLETA_CORRETORAS=[2] já derrubou este arquivo inteiro. O
    conftest também pina a env em import-time; aqui o valor é o que ESTE
    arquivo precisa, não o default de projeto.
    """
    monkeypatch.setattr(lead_intake.settings, "roleta_corretoras", [CORRETOR_ID])

    engine = create_engine("sqlite://")
    Base.metadata.create_all(
        engine,
        tables=[
            Corretora.__table__,
            PipelineStage.__table__,
            # opt-out (07/10): o follow-up lê disparos_reengajamento a cada volta
            # e, sem a tabela, segura todo toque (fail-closed).
            DisparoReengajamento.__table__,
            Lead.__table__,
            LeadEvent.__table__,
            CadenciaEstado.__table__,
            CadenciaEvento.__table__,
        ],
    )
    factory = sessionmaker(bind=engine)
    with factory() as s:
        s.add(
            Corretora(
                id=CORRETOR_ID, nome="Pessoa3", telefone="+5500000000000",
                kommo_user_id=10000003, evolution_instance="acme_corretora_a",
                ativo=True, papel="corretora_atendedora_de_dona",
            )
        )
        s.commit()
    return factory


@pytest.fixture(autouse=True)
def _reset_guard():
    cadencia._LAST_ENQUEUE["at"] = 0.0
    yield


@pytest.fixture()
def flag_ligada():
    with patch.object(settings, "lead_intake_ativo", True):
        yield


class FakeSenderCadencia:
    """Contrato do sender do tick: (telefone, texto, *, com_botao)."""

    def __init__(self):
        self.chamadas: list[dict] = []

    def __call__(self, telefone, texto, *, com_botao):
        self.chamadas.append(
            {"telefone": telefone, "texto": texto, "com_botao": com_botao}
        )
        return {"ok": True, "dry_run": True, "sent": False}


class FakeSenderIntake:
    """Contrato do sender do intake: (telefone, texto)."""

    def __init__(self):
        self.chamadas: list[dict] = []

    def __call__(self, telefone, texto):
        self.chamadas.append({"telefone": telefone, "texto": texto})
        return {"ok": True, "dry_run": True, "sent": False}


def _tick(session_factory, sender, agora=D0):
    cadencia._LAST_ENQUEUE["at"] = 0.0
    with session_factory() as s:
        return cadencia.tick(agora=agora, session=s, sender=sender)


# ── 1. Intake NÃO matricula em cadência — tick não toca nada ─────────────────

def test_intake_nao_matricula_em_cadencia(session_factory, flag_ligada):
    sender_intake = FakeSenderIntake()
    res = intake_lead(
        nome="Lead Teste",
        telefone="+5500000000000",
        origem="meta_leadgen",
        session_factory=session_factory,
        sender=sender_intake,
    )
    assert res is not None
    assert res.boas_vindas_enviada is True
    # A copy sai do teste A/B (balde determinístico por lead): o texto tem que
    # ser o de UMA das variantes ativas, renderizado com o nome da linha — não
    # uma constante fixa, senão o teste quebra a cada variante nova.
    from app.services import boas_vindas_copies as bvc
    from app.services.lead_intake import nome_da_linha

    esperados = {v.render(nome_da_linha(res.corretora_id)) for v in bvc.ativas()}
    assert sender_intake.chamadas[0]["texto"] in esperados

    # ZERO cadência criada pelo intake (lei Dona 10/07)
    with session_factory() as s:
        assert s.query(CadenciaEstado).count() == 0

    # e o tick não tem nada pra tocar
    sender_tick = FakeSenderCadencia()
    tick_res = _tick(session_factory, sender_tick, agora=_depois())
    assert tick_res.mensagens_enfileiradas == 0
    assert sender_tick.chamadas == []


# ── 2. D0 = reencontro SEMPRE — mesmo com evento 'intake' na espinha ─────────

def test_d0_sempre_reencontro_mesmo_com_evento_intake(session_factory, flag_ligada):
    # lead entra pelo intake (grava lead_events tipo='intake')...
    intake_lead(
        nome="Lead Teste",
        telefone="+5500000000000",
        origem="meta_leadgen",
        session_factory=session_factory,
        sender=FakeSenderIntake(),
    )
    # ...e DEPOIS a Pessoa12 arrasta pro Follow-up → matrícula na cadência
    with session_factory() as s:
        cadencia.iniciar_cadencia(
            s, telefone="+5500000000000", corretora_id=CORRETOR_ID, agora=D0
        )
        s.commit()

    sender = FakeSenderCadencia()
    tick_res = _tick(session_factory, sender, agora=_depois())

    assert tick_res.mensagens_enfileiradas == 1
    # D0-duplo morreu: reencontro padrão, NUNCA boas-vindas de cadastro
    assert sender.chamadas[0]["texto"] == cadencia.aplicar_assinatura(
        cadencia._TEMPLATES_MSG[0], 3
    )   # 3 = Pessoa12 ("Pessoa"); a assinatura agora é a de quem manda
    assert "ficou parada" in sender.chamadas[0]["texto"]

    with session_factory() as s:
        ev = s.query(CadenciaEvento).filter(CadenciaEvento.toque == 0).one()
        assert ev.payload["texto"] == cadencia.aplicar_assinatura(
                cadencia._TEMPLATES_MSG[0], CORRETOR_ID
            )


# ── 3. API antiga morta: iniciar_cadencia sem param `origem` ─────────────────

def test_iniciar_cadencia_nao_aceita_origem(session_factory):
    with session_factory() as s:
        with pytest.raises(TypeError):
            cadencia.iniciar_cadencia(
                s, telefone="+5500000000000", origem="intake"  # type: ignore[call-arg]
            )


def test_simbolos_do_d0_duplo_nao_existem_mais():
    assert not hasattr(cadencia, "_TEMPLATES_MSG_INTAKE")
    assert not hasattr(cadencia, "_veio_de_intake")


# ── 4. Copy da boas-vindas vive no intake (casca Olá SEM botão, igual Pessoa12) ─

def test_boas_vindas_vive_no_intake_copy_dona():
    # Dona 28/07: cadastro + oportunidades no corpo; CTA/Saiba mais no template.
    assert "Acme" in BOAS_VINDAS_TEXTO
    assert "cadastrar" in BOAS_VINDAS_TEXTO.lower()
    assert "cidade" in BOAS_VINDAS_TEXTO.lower()
    assert "oportunidades" in BOAS_VINDAS_TEXTO.lower()
    # CTA NÃO no corpo (evita duplicar com o rodapé do template reeng_b)
    assert "saiba mais" not in BOAS_VINDAS_TEXTO.lower()
    assert "fico à disposição" not in BOAS_VINDAS_TEXTO.lower()
    # sem Oi/Olá no corpo (template já cumprimenta)
    assert not BOAS_VINDAS_TEXTO.lstrip().lower().startswith(("oi", "olá", "ola"))
    # e não está mais nos templates da cadência
    assert BOAS_VINDAS_TEXTO not in cadencia._TEMPLATES_MSG.values()
