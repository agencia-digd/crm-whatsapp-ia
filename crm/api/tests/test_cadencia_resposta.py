"""Testes do hook de RESPOSTA da cadência (P0 + correções Dona 10/07).

Quando o lead responde no inbound da linha Pessoa12 (`cloud_inbound` →
`processar_resposta_cadencia`):

  - resposta normal → cadencia_estado status='respondida' + cadencia_eventos
    tipo='resposta' + lead canônico move pra "Em atendimento" +
    lead_events(tipo='respondeu');
  - opt-out → status='opt_out' + lead canônico move pra "Venda perdida"
    (regra Dona 10/07) + lead_events(tipo='opt_out');
  - timer 5s anti-secretária-eletrônica: inbound < 5s depois do nosso último
    envio (cadencia_eventos tipo='mensagem') é auto-reply → IGNORA (não marca
    respondida, não move, cadência segue); > 5s conta normal;
  - remarketing também responde: o hook age em status 'ativa' E 'remarketing';
  - sem cadência viva → no-op ("sem_cadencia");
  - não rebaixa etapa mais avançada (ex: "Visita marcada"); opt-out não
    rebaixa "Venda ganha";
  - best-effort: espinha ausente não impede o 'respondida'.

Aviso à corretora via WhatsApp foi APOSENTADO (Dona 10/07: aviso interno é
só in-app) — não existe mais sender de aviso aqui.

DB isolado: SQLite in-memory (mesmo padrão de test_cadencia_enroll_followup).
Zero prod, zero envio.
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
    Corretora,
    Lead,
    LeadEvent,
    PipelineStage,
)
from app.services.cadencia import (
    STAGE_EM_ATENDIMENTO,
    STAGE_VENDA_PERDIDA,
    STATUS_RESPONDIDA,
    TIPO_MENSAGEM,
    TIPO_RESPOSTA,
    iniciar_cadencia,
    processar_resposta_cadencia,
)


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(element, compiler, **kw):  # noqa: ANN001
    return "JSON"


@compiles(BigInteger, "sqlite")
def _compile_bigint_sqlite(element, compiler, **kw):  # noqa: ANN001
    return "INTEGER"


CORRETOR_ID = 3
LEAD_FONE = "+5500000000000"          # formato cadência (normalizado)
LEAD_FONE_KOMMO = "+5500000000000"   # formato leads canônico (Kommo, com '+')
CORRETOR_FONE = "+5500000000000"


@pytest.fixture()
def session_factory():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(
        engine,
        tables=[
            Corretora.__table__,
            CadenciaEstado.__table__,
            CadenciaEvento.__table__,
            Lead.__table__,
            LeadEvent.__table__,
            PipelineStage.__table__,
        ],
    )
    factory = sessionmaker(bind=engine)
    with factory() as s:
        s.add(
            Corretora(
                id=CORRETOR_ID, nome="Pessoa3", telefone="+5500000000000",
                telefone_normalizado=CORRETOR_FONE, kommo_user_id=10000003,
                evolution_instance="acme_corretora_a", ativo=True,
                papel="corretora_atendedora_de_dona",
            )
        )
        s.commit()
    return factory


def _seed_cadencia_e_lead(
    factory,
    *,
    stage_nome: str = "Follow-up",
    com_lead: bool = True,
    status: str = "ativa",
) -> int:
    """Matricula LEAD_FONE na cadência (status configurável) e (opcional) cria
    o lead canônico na etapa `stage_nome`. Retorna o stage_id inicial (ou 0)."""
    with factory() as s:
        est = iniciar_cadencia(
            s, telefone=LEAD_FONE, lead_id="777", corretora_id=CORRETOR_ID
        )
        if status != "ativa":
            est.status = status
        stage_id = 0
        if com_lead:
            stage = PipelineStage(tenant_id=1, nome=stage_nome, ordem=3)
            s.add(stage)
            s.flush()
            stage_id = stage.id
            s.add(
                Lead(
                    tenant_id=1, external_ref="777", nome="João Silva",
                    telefone=LEAD_FONE_KOMMO, owner_id=CORRETOR_ID, stage_id=stage_id,
                )
            )
        s.commit()
    return stage_id


def _registrar_envio(factory, *, segundos_atras: float) -> None:
    """Grava um cadencia_eventos tipo='mensagem' (como o tick grava no
    enfileiramento — toque OU remarketing) há `segundos_atras` segundos."""
    with factory() as s:
        est = s.query(CadenciaEstado).one()
        s.add(
            CadenciaEvento(
                tenant_id=est.tenant_id, cadencia_id=est.id, toque=0,
                tipo=TIPO_MENSAGEM, canal="whatsapp",
                payload={"texto": "oi?", "dry_run": True, "sent": False},
                at=datetime.now(timezone.utc) - timedelta(seconds=segundos_atras),
            )
        )
        s.commit()


# ── resposta normal ───────────────────────────────────────────────────────────

def test_resposta_marca_respondida_move_lead_e_grava_eventos(session_factory):
    stage_followup = _seed_cadencia_e_lead(session_factory)

    with session_factory() as s:
        res = processar_resposta_cadencia(
            LEAD_FONE, "Oi! Ainda tô procurando sim", session=s
        )
    assert res == STATUS_RESPONDIDA

    with session_factory() as s:
        est = s.query(CadenciaEstado).one()
        assert est.status == STATUS_RESPONDIDA

        ev = s.query(CadenciaEvento).filter(CadenciaEvento.tipo == TIPO_RESPOSTA).one()
        assert ev.cadencia_id == est.id
        assert ev.payload["opt_out"] is False
        assert "procurando" in ev.payload["texto"]

        lead = s.query(Lead).one()
        novo_stage = s.get(PipelineStage, lead.stage_id)
        assert novo_stage.nome == STAGE_EM_ATENDIMENTO
        assert lead.stage_id != stage_followup

        lev = s.query(LeadEvent).filter(LeadEvent.tipo == "respondeu").one()
        assert lev.lead_id == lead.id
        assert lev.payload["origem"] == "cadencia"
        assert lev.payload["movido"] is True


def test_resposta_cruza_formatos_de_telefone(session_factory):
    """Inbound manda wa_id sem '+'; cadência/leads podem ter outros formatos —
    o match é por sufixo de 8 dígitos."""
    _seed_cadencia_e_lead(session_factory)
    with session_factory() as s:
        res = processar_resposta_cadencia(
            "+5500000000000", "tenho interesse", session=s
        )
    assert res == STATUS_RESPONDIDA


# ── opt-out → Venda perdida (correção a — Dona 10/07) ─────────────────────────

def test_opt_out_marca_opt_out_e_move_pra_venda_perdida(session_factory):
    stage_followup = _seed_cadencia_e_lead(session_factory)

    with session_factory() as s:
        res = processar_resposta_cadencia(LEAD_FONE, "não quero mais, para", session=s)
    assert res == "opt_out"

    with session_factory() as s:
        est = s.query(CadenciaEstado).one()
        assert est.status == "opt_out"

        ev = s.query(CadenciaEvento).filter(CadenciaEvento.tipo == TIPO_RESPOSTA).one()
        assert ev.payload["opt_out"] is True

        lead = s.query(Lead).one()
        assert lead.stage_id != stage_followup
        novo_stage = s.get(PipelineStage, lead.stage_id)
        assert novo_stage.nome == STAGE_VENDA_PERDIDA

        lev = s.query(LeadEvent).filter(LeadEvent.tipo == "opt_out").one()
        assert lev.lead_id == lead.id
        assert lev.payload["origem"] == "cadencia"
        assert lev.payload["movido"] is True
        # sem evento 'respondeu' — opt-out não é resposta comercial
        assert s.query(LeadEvent).filter(LeadEvent.tipo == "respondeu").count() == 0


def test_opt_out_nao_rebaixa_venda_ganha(session_factory):
    stage_ganha = _seed_cadencia_e_lead(session_factory, stage_nome="Venda ganha")

    with session_factory() as s:
        res = processar_resposta_cadencia(LEAD_FONE, "parar", session=s)
    assert res == "opt_out"

    with session_factory() as s:
        lead = s.query(Lead).one()
        assert lead.stage_id == stage_ganha  # venda ganha fica ganha
        lev = s.query(LeadEvent).filter(LeadEvent.tipo == "opt_out").one()
        assert lev.payload["movido"] is False


# ── timer 5s anti-secretária-eletrônica (correção d — Dona 10/07) ─────────────

def test_resposta_em_menos_de_5s_do_envio_e_ignorada(session_factory):
    stage_followup = _seed_cadencia_e_lead(session_factory)
    _registrar_envio(session_factory, segundos_atras=2)

    with session_factory() as s:
        res = processar_resposta_cadencia(LEAD_FONE, "mensagem automática", session=s)
    assert res == "auto_reply"

    with session_factory() as s:
        est = s.query(CadenciaEstado).one()
        assert est.status == "ativa"  # cadência SEGUE
        # nenhum evento de resposta gravado, lead não moveu
        assert (
            s.query(CadenciaEvento).filter(CadenciaEvento.tipo == TIPO_RESPOSTA).count()
            == 0
        )
        assert s.query(Lead).one().stage_id == stage_followup
        assert s.query(LeadEvent).count() == 0


def test_opt_out_em_menos_de_5s_tambem_e_ignorado(session_factory):
    """Auto-reply com palavra de opt-out ('pare de me ligar' gravado em
    secretária) não pode descadastrar o lead sozinho."""
    _seed_cadencia_e_lead(session_factory)
    _registrar_envio(session_factory, segundos_atras=1)

    with session_factory() as s:
        res = processar_resposta_cadencia(LEAD_FONE, "parar", session=s)
    assert res == "auto_reply"
    with session_factory() as s:
        assert s.query(CadenciaEstado).one().status == "ativa"


def test_resposta_depois_da_janela_conta_normal(session_factory):
    _seed_cadencia_e_lead(session_factory)
    _registrar_envio(session_factory, segundos_atras=20)  # > janela 15s

    with session_factory() as s:
        res = processar_resposta_cadencia(LEAD_FONE, "oi, pode falar", session=s)
    assert res == STATUS_RESPONDIDA
    with session_factory() as s:
        assert s.query(CadenciaEstado).one().status == STATUS_RESPONDIDA


def test_resposta_auto_reply_por_conteudo_e_ignorada(session_factory):
    """FORA da janela de tempo, mas conteúdo de auto-reply de empresa (intro
    comercial tipo 'sou o X da Y Equipamentos') → ignora, cadência segue."""
    _seed_cadencia_e_lead(session_factory)
    _registrar_envio(session_factory, segundos_atras=60)  # bem fora da janela 15s
    with session_factory() as s:
        res = processar_resposta_cadencia(
            LEAD_FONE, "Ol\u00e1, sou o Leonardo Britto da LEFIT Equipamentos", session=s)
    assert res == "auto_reply"
    with session_factory() as s:
        assert s.query(CadenciaEstado).one().status == "ativa"


def test_sem_envio_anterior_resposta_conta(session_factory):
    """Cadência recém-matriculada (nenhuma mensagem enviada ainda) — resposta
    espontânea do lead conta normal."""
    _seed_cadencia_e_lead(session_factory)
    with session_factory() as s:
        res = processar_resposta_cadencia(LEAD_FONE, "oi", session=s)
    assert res == STATUS_RESPONDIDA


# ── remarketing também responde (correção d — hook age em ativa E remarketing) ─

def test_remarketing_responde_e_vira_respondida(session_factory):
    _seed_cadencia_e_lead(session_factory, status="remarketing")

    with session_factory() as s:
        res = processar_resposta_cadencia(LEAD_FONE, "apareceu algo novo?", session=s)
    assert res == STATUS_RESPONDIDA

    with session_factory() as s:
        est = s.query(CadenciaEstado).one()
        assert est.status == STATUS_RESPONDIDA
        lead = s.query(Lead).one()
        assert s.get(PipelineStage, lead.stage_id).nome == STAGE_EM_ATENDIMENTO


def test_remarketing_auto_reply_em_5s_e_ignorado(session_factory):
    _seed_cadencia_e_lead(session_factory, status="remarketing")
    _registrar_envio(session_factory, segundos_atras=3)

    with session_factory() as s:
        res = processar_resposta_cadencia(LEAD_FONE, "recado automático", session=s)
    assert res == "auto_reply"
    with session_factory() as s:
        assert s.query(CadenciaEstado).one().status == "remarketing"


def test_remarketing_opt_out_move_pra_venda_perdida(session_factory):
    _seed_cadencia_e_lead(session_factory, status="remarketing")

    with session_factory() as s:
        res = processar_resposta_cadencia(LEAD_FONE, "descadastrar", session=s)
    assert res == "opt_out"
    with session_factory() as s:
        lead = s.query(Lead).one()
        assert s.get(PipelineStage, lead.stage_id).nome == STAGE_VENDA_PERDIDA


# ── no-ops e guards ───────────────────────────────────────────────────────────

def test_sem_cadencia_e_noop(session_factory):
    with session_factory() as s:
        res = processar_resposta_cadencia("+5500000000000", "oi", session=s)
    assert res == "sem_cadencia"
    with session_factory() as s:
        assert s.query(CadenciaEvento).count() == 0
        assert s.query(LeadEvent).count() == 0


def test_cadencia_ja_parada_e_noop(session_factory):
    """Cadência já respondida/opt-out não processa de novo."""
    _seed_cadencia_e_lead(session_factory)
    for parado in ("opt_out", STATUS_RESPONDIDA):
        with session_factory() as s:
            s.query(CadenciaEstado).update({"status": parado})
            s.commit()
        with session_factory() as s:
            res = processar_resposta_cadencia(LEAD_FONE, "oi de novo", session=s)
        assert res == "sem_cadencia"


def test_nao_rebaixa_etapa_avancada(session_factory):
    """Lead já em 'Visita marcada' responde: cadência para, evento registrado,
    mas a etapa NÃO volta pra 'Em atendimento'."""
    stage_visita = _seed_cadencia_e_lead(session_factory, stage_nome="Visita marcada")

    with session_factory() as s:
        res = processar_resposta_cadencia(LEAD_FONE, "confirmo a visita", session=s)
    assert res == STATUS_RESPONDIDA

    with session_factory() as s:
        lead = s.query(Lead).one()
        assert lead.stage_id == stage_visita  # não rebaixou
        lev = s.query(LeadEvent).filter(LeadEvent.tipo == "respondeu").one()
        assert lev.payload["movido"] is False


def test_sem_lead_canonico_ainda_marca_respondida(session_factory):
    """Espinha sem o lead (ou indisponível) não impede a cadência de parar."""
    _seed_cadencia_e_lead(session_factory, com_lead=False)

    with session_factory() as s:
        res = processar_resposta_cadencia(LEAD_FONE, "oi", session=s)
    assert res == STATUS_RESPONDIDA

    with session_factory() as s:
        est = s.query(CadenciaEstado).one()
        assert est.status == STATUS_RESPONDIDA
        assert s.query(LeadEvent).count() == 0


def test_best_effort_nunca_levanta():
    """Sessão explodindo → retorna None, não propaga (inbound não pode cair)."""

    class BoomSession:
        def query(self, *a, **k):  # noqa: ANN001
            raise RuntimeError("db fora")

        def close(self):
            pass

    res = processar_resposta_cadencia(
        LEAD_FONE, "oi", session=BoomSession()  # type: ignore[arg-type]
    )
    assert res is None


# ── desinteresse claro → opt-out + frase na auditoria (obra 10, 22/10) ────────

def test_desinteresse_claro_tira_da_regua_e_grava_a_frase(session_factory):
    _seed_cadencia_e_lead(session_factory)

    with session_factory() as s:
        res = processar_resposta_cadencia(LEAD_FONE, "Olá, não tenho mais interesse, obrigada", session=s)
    assert res == "opt_out"

    with session_factory() as s:
        est = s.query(CadenciaEstado).one()
        assert est.status == "opt_out"  # régua para: nenhum toque pendente sai
        ev = s.query(CadenciaEvento).filter(CadenciaEvento.tipo == TIPO_RESPOSTA).one()
        assert ev.payload["opt_out"] is True
        assert ev.payload["opt_out_motivo"] == "desinteresse"
        assert ev.payload["opt_out_frase"] == "nao tenho mais interesse"


@pytest.mark.parametrize("texto", ["agora não", "não tenho interesse agora",
                                   "depois eu vejo", "não é esse"])
def test_negativa_ambigua_continua_respondida(session_factory, texto):
    _seed_cadencia_e_lead(session_factory)

    with session_factory() as s:
        res = processar_resposta_cadencia(LEAD_FONE, texto, session=s)
    assert res == STATUS_RESPONDIDA

    with session_factory() as s:
        ev = s.query(CadenciaEvento).filter(CadenciaEvento.tipo == TIPO_RESPOSTA).one()
        assert ev.payload["opt_out"] is False
        assert ev.payload["opt_out_motivo"] is None
