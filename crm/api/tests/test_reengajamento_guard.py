"""Testes da trava anti-colisão Corretor × cadência (F1 do B9).

`app/services/reengajamento_guard.py` — duas consultas puras por telefone
(match p8 = últimos 8 dígitos):

  - `corretor_conversa_ativa`: recovery_conversations com state ativo
    (aguardando/qualificando/reengajado) e atividade (hsm_sent_at OU
    last_lead_msg_at) na janela de 7 dias;
  - `cadencia_engajada`: cadencia_estado status='ativa' (remarketing NÃO
    bloqueia — sem campo de resposta, resposta vira status='respondida');
  - fail-open: erro na query NUNCA bloqueia (retorna False) e não poisona a
    sessão do caller (SAVEPOINT);
  - integração: `cadencia.tick` ADIA (sem avançar toque) telefone com conversa
    Corretor viva; quando a conversa morre, o toque sai normalmente.

DB isolado: SQLite in-memory (mesmo padrão de test_cadencia_resposta.py).
Zero prod, zero envio (sender fake).
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
    RecoveryConversation,
)
from app.services import cadencia
from app.services.reengajamento_guard import corretor_conversa_ativa, cadencia_engajada


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(element, compiler, **kw):  # noqa: ANN001
    return "JSON"


@compiles(BigInteger, "sqlite")
def _compile_bigint_sqlite(element, compiler, **kw):  # noqa: ANN001
    return "INTEGER"


AGORA = datetime(2026, 7, 24, 12, 0, 0, tzinfo=timezone.utc)
FONE = "+5500000000000"           # formato digits-only (recovery/_digits_only)
FONE_KOMMO = "+5500000000000"  # mesmo telefone, formato Kommo


@pytest.fixture()
def session_factory():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(
        engine,
        tables=[
            CadenciaEstado.__table__,
            CadenciaEvento.__table__,
            RecoveryConversation.__table__,
            # opt-out (07/10): o follow-up lê leads/disparos_reengajamento a cada
            # volta e, sem as tabelas, segura todo toque (fail-closed).
            Lead.__table__,
            DisparoReengajamento.__table__,
        ],
    )
    return sessionmaker(bind=engine)


@pytest.fixture(autouse=True)
def _reset_guard():
    # zera o guard in-process de espaçamento (senão o cooldown de 60s bloqueia
    # os testes que rodam em milissegundos). Mesmo padrão de test_cadencia.py.
    cadencia._LAST_ENQUEUE["at"] = 0.0
    yield


def _seed_recovery(
    factory,
    *,
    state: str = "aguardando",
    hsm_sent_at: datetime | None = None,
    last_lead_msg_at: datetime | None = None,
    phone: str = FONE,
    lead_id: str = "999",
) -> None:
    with factory() as s:
        s.add(
            RecoveryConversation(
                lead_id=lead_id,
                lead_phone=phone,
                lead_name="Lead Teste",
                state=state,
                hsm_sent_at=hsm_sent_at,
                last_lead_msg_at=last_lead_msg_at,
            )
        )
        s.commit()


def _seed_cadencia(factory, *, status: str = "ativa", telefone: str = FONE) -> None:
    with factory() as s:
        est = cadencia.iniciar_cadencia(
            s, telefone=telefone, lead_id="777", corretora_id=3, agora=AGORA
        )
        if status != "ativa":
            est.status = status
        s.commit()


# ── corretor_conversa_ativa ────────────────────────────────────────────────────────

def test_corretor_ativa_hsm_recente(session_factory):
    _seed_recovery(session_factory, hsm_sent_at=AGORA - timedelta(days=1))
    assert corretor_conversa_ativa(FONE, agora=AGORA, session_factory=session_factory) is True


def test_corretor_match_sufixo_entre_formatos(session_factory):
    # recovery guarda digits-only; a cadência consulta com formato Kommo ('+55 (15)...')
    _seed_recovery(session_factory, hsm_sent_at=AGORA - timedelta(days=1))
    assert corretor_conversa_ativa(FONE_KOMMO, agora=AGORA, session_factory=session_factory) is True


def test_corretor_estado_final_nao_bloqueia(session_factory):
    for i, state in enumerate(("qualificado", "perdido", "largado")):
        _seed_recovery(
            session_factory, state=state, lead_id=f"final_{i}",
            hsm_sent_at=AGORA - timedelta(days=1),
        )
    assert corretor_conversa_ativa(FONE, agora=AGORA, session_factory=session_factory) is False


def test_corretor_janela_7d_expirada(session_factory):
    _seed_recovery(session_factory, hsm_sent_at=AGORA - timedelta(days=8))
    assert corretor_conversa_ativa(FONE, agora=AGORA, session_factory=session_factory) is False


def test_corretor_lead_msg_recente_mantem_ativa_mesmo_com_hsm_velho(session_factory):
    # lead ainda respondendo à Corretor depois do 7º dia do HSM = conversa viva
    _seed_recovery(
        session_factory, state="qualificando",
        hsm_sent_at=AGORA - timedelta(days=10),
        last_lead_msg_at=AGORA - timedelta(hours=6),
    )
    assert corretor_conversa_ativa(FONE, agora=AGORA, session_factory=session_factory) is True


def test_corretor_sem_timestamps_nao_bloqueia(session_factory):
    _seed_recovery(session_factory)  # aguardando mas sem hsm/lead_msg — sem atividade
    assert corretor_conversa_ativa(FONE, agora=AGORA, session_factory=session_factory) is False


def test_corretor_telefone_diferente_nao_bloqueia(session_factory):
    _seed_recovery(session_factory, hsm_sent_at=AGORA - timedelta(days=1))
    assert corretor_conversa_ativa("+5500000000000", agora=AGORA, session_factory=session_factory) is False


def test_corretor_telefone_vazio(session_factory):
    assert corretor_conversa_ativa("", session_factory=session_factory) is False
    assert corretor_conversa_ativa(None, session_factory=session_factory) is False


# ── cadencia_engajada ─────────────────────────────────────────────────────────

def test_cadencia_engajada_ativa(session_factory):
    _seed_cadencia(session_factory, status="ativa")
    assert cadencia_engajada(FONE, session_factory=session_factory) is True
    # dispatcher consulta com digits-only; cadência pode guardar '+55...'
    assert cadencia_engajada(FONE_KOMMO, session_factory=session_factory) is True


@pytest.mark.parametrize("status", ["remarketing", "respondida", "opt_out"])
def test_cadencia_nao_ativa_nao_bloqueia(session_factory, status):
    _seed_cadencia(session_factory, status=status)
    assert cadencia_engajada(FONE, session_factory=session_factory) is False


# ── fail-open (erro na trava NUNCA bloqueia nem poisona a sessão) ─────────────

def test_fail_open_sem_tabelas():
    # engine SEM tabela nenhuma → query estoura → trava responde False (não
    # bloqueia o fluxo normal) em vez de levantar.
    engine = create_engine("sqlite://")
    factory = sessionmaker(bind=engine)
    assert corretor_conversa_ativa(FONE, session_factory=factory) is False
    assert cadencia_engajada(FONE, session_factory=factory) is False


def test_fail_open_nao_poisona_sessao_do_caller(session_factory):
    # engine só com cadencia_estado (sem recovery_conversations): a consulta da
    # Corretor falha DENTRO da sessão do caller — SAVEPOINT garante que a sessão
    # continua utilizável depois.
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[CadenciaEstado.__table__])
    factory = sessionmaker(bind=engine)
    with factory() as s:
        assert corretor_conversa_ativa(FONE, session=s) is False
        # sessão segue viva: query normal funciona
        assert s.query(CadenciaEstado).count() == 0


# ── integração: tick da cadência ADIA telefone com Corretor ativa ──────────────────

class FakeSender:
    def __init__(self):
        self.chamadas: list[dict] = []

    def __call__(self, telefone, texto, *, com_botao):
        self.chamadas.append({"telefone": telefone, "texto": texto, "com_botao": com_botao})
        return {"ok": True, "dry_run": True, "sent": False}


def test_tick_pula_telefone_com_corretor_ativa_sem_avancar_toque(session_factory):
    _seed_cadencia(session_factory)  # D0 devido em AGORA
    _seed_recovery(session_factory, hsm_sent_at=AGORA - timedelta(days=1))

    sender = FakeSender()
    with session_factory() as s:
        res = cadencia.tick(agora=AGORA, session=s, sender=sender)

    assert sender.chamadas == []                      # nada enfileirado
    assert res.mensagens_enfileiradas == 0
    assert res.bloqueados.get("corretor_ativa") == 1
    with session_factory() as s:
        est = s.query(CadenciaEstado).one()
        assert est.toque_atual == 0                   # NÃO avançou
        devido = est.proximo_toque_em
        if devido.tzinfo is None:
            devido = devido.replace(tzinfo=timezone.utc)
        assert devido <= AGORA                        # segue devido (adia, não pula)
        assert s.query(CadenciaEvento).count() == 0   # nenhum evento gravado


def test_tick_retoma_quando_conversa_corretor_morre(session_factory):
    _seed_cadencia(session_factory)
    _seed_recovery(session_factory, hsm_sent_at=AGORA - timedelta(days=1))

    sender = FakeSender()
    with session_factory() as s:
        cadencia.tick(agora=AGORA, session=s, sender=sender)
    assert sender.chamadas == []

    # conversa da Corretor termina (state final) → próximo tick manda o D0 normal
    with session_factory() as s:
        conv = s.query(RecoveryConversation).one()
        conv.state = "perdido"
        s.commit()

    cadencia._LAST_ENQUEUE["at"] = 0.0
    with session_factory() as s:
        res = cadencia.tick(agora=AGORA + timedelta(hours=1), session=s, sender=sender)

    assert res.mensagens_enfileiradas == 1
    assert len(sender.chamadas) == 1
    with session_factory() as s:
        est = s.query(CadenciaEstado).one()
        assert est.toque_atual == 1                   # avançou pro D1
