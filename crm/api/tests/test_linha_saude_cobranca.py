"""Linha recusando por COBRANÇA: o painel avisa em vez de dizer "enviado".

18/09/2026: as WABAs da Pessoa10 e da Pessoa11 travaram em "Business eligibility
payment issue". A Pessoa10 mandou pra Sonia (…8631) às 18:46, o painel respondeu
"reengajamento enviado ✔️" — e a Meta recusou segundos depois, no webhook de
status. A corretora ficou achando que tinha falado.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from app.db.session import SessionLocal
from app.services import linha_saude

INSTANCE = "acme_corretora_c"


def _falha(s, *, erro: str, minutos: int, instance: str = INSTANCE) -> str:
    wamid = f"wamid.teste-{uuid.uuid4()}"
    s.execute(
        text(
            "INSERT INTO mensagens_whatsapp (evolution_message_id, instance, "
            "  remote_jid, contato_telefone, from_corretora, tipo_mensagem, "
            "  conteudo_texto, conteudo_metadata, timestamp_evolution) "
            "VALUES (:w, :inst, '+5500000000000@s.whatsapp.net', '+5500000000000', "
            "        TRUE, 'template', 'x', CAST(:meta AS jsonb), "
            "        now() - make_interval(mins => :m))"
        ),
        {"w": wamid, "inst": instance, "m": minutos,
         "meta": '{"delivery_status": "failed", "delivery_error": "%s"}' % erro},
    )
    return wamid


@pytest.fixture()
def limpo():
    wamids: list[str] = []
    yield wamids
    with SessionLocal() as s:
        if wamids:
            s.execute(
                text("DELETE FROM mensagens_whatsapp WHERE evolution_message_id = ANY(:w)"),
                {"w": wamids},
            )
            s.commit()


def _entrega(s, *, minutos: int, instance: str = INSTANCE, tipo: str = "template") -> str:
    """Template ENTREGUE — a prova de vida que solta o guard."""
    wamid = f"wamid.teste-{uuid.uuid4()}"
    s.execute(
        text(
            "INSERT INTO mensagens_whatsapp (evolution_message_id, instance, "
            "  remote_jid, contato_telefone, from_corretora, tipo_mensagem, "
            "  conteudo_texto, conteudo_metadata, timestamp_evolution) "
            "VALUES (:w, :inst, '+5500000000000@s.whatsapp.net', '+5500000000000', "
            "        TRUE, :tipo, 'x', CAST('{\"delivery_status\": \"delivered\"}' AS jsonb), "
            "        now() - make_interval(mins => :m))"
        ),
        {"w": wamid, "inst": instance, "tipo": tipo, "m": minutos},
    )
    return wamid


def test_recusa_de_cobranca_bloqueia(limpo):
    """Uma recusa basta: elas chegam esparsas (a Pessoa10 levou 3 em 2 horas)."""
    with SessionLocal() as s:
        limpo.append(_falha(s, erro="Business eligibility payment issue", minutos=18))
        s.commit()
        assert linha_saude.bloqueada_por_cobranca(s, INSTANCE) is True


def test_template_entregue_DEPOIS_da_recusa_solta_o_guard(limpo):
    """Cobrança resolvida: o primeiro template que entrega libera na hora."""
    with SessionLocal() as s:
        limpo.append(_falha(s, erro="Business eligibility payment issue", minutos=60))
        limpo.append(_entrega(s, minutos=5))
        s.commit()
        assert linha_saude.bloqueada_por_cobranca(s, INSTANCE) is False


def test_entrega_ANTES_da_recusa_nao_solta(limpo):
    """Ordem importa: entregou de manhã e travou à tarde = travada."""
    with SessionLocal() as s:
        limpo.append(_entrega(s, minutos=120))
        limpo.append(_falha(s, erro="Business eligibility payment issue", minutos=10))
        s.commit()
        assert linha_saude.bloqueada_por_cobranca(s, INSTANCE) is True


def test_texto_do_celular_nao_conta_como_prova_de_vida(limpo):
    """Em coexistência o celular entrega mesmo com a conta travada — se isso
    contasse, o guard desligaria justo quando ela usa o contorno."""
    with SessionLocal() as s:
        limpo.append(_falha(s, erro="Business eligibility payment issue", minutos=30))
        limpo.append(_entrega(s, minutos=5, tipo="text"))
        s.commit()
        assert linha_saude.bloqueada_por_cobranca(s, INSTANCE) is True


def test_recusa_antiga_demais_nao_bloqueia(limpo):
    with SessionLocal() as s:
        limpo.append(_falha(s, erro="Business eligibility payment issue", minutos=60 * 30))
        s.commit()
        assert linha_saude.bloqueada_por_cobranca(s, INSTANCE) is False
def test_falha_comum_de_entrega_nao_bloqueia(limpo):
    """Número errado / aparelho fora do ar vale tentar de novo — não é isto."""
    with SessionLocal() as s:
        for _ in range(3):
            limpo.append(_falha(s, erro="Message undeliverable", minutos=5))
        s.commit()
        assert linha_saude.bloqueada_por_cobranca(s, INSTANCE) is False


def test_nao_contamina_outra_linha(limpo):
    """A da Pessoa12 seguiu entregando enquanto Pessoa10 e Pessoa11 estavam travadas."""
    with SessionLocal() as s:
        for _ in range(3):
            limpo.append(_falha(s, erro="Business eligibility payment issue", minutos=5))
        s.commit()
        assert linha_saude.bloqueada_por_cobranca(s, "acme_corretora_a") is False


def test_sem_instance_nao_bloqueia():
    with SessionLocal() as s:
        assert linha_saude.bloqueada_por_cobranca(s, None) is False


# ── a ligação no endpoint: 422 e NENHUM envio ───────────────────────────────

def test_endpoint_reengajar_bloqueia_sem_chamar_a_meta(limpo, monkeypatch):
    """O painel dizia "enviado ✔️" e a Meta recusava depois. Agora barra antes —
    e o importante: sem chegar a chamar o envio."""
    from fastapi.testclient import TestClient

    from app.api import cloud_corretora_a as cg
    from app.api import painel
    from app.main import app

    REF = "9900009001"
    TEL = "+5500000000000"
    chamou: list[str] = []

    def _nao_deveria(*a, **k):
        chamou.append("enviou")
        raise AssertionError("envio chamado com a linha bloqueada por cobrança")

    monkeypatch.setattr(cg, "enviar_template_linha", _nao_deveria)
    monkeypatch.setattr(cg, "ler_thread_cloud", lambda *a, **k: [])
    monkeypatch.setattr(painel, "_pode_atender", lambda *a, **k: True)

    with SessionLocal() as s:
        limpo.append(_falha(s, erro="Business eligibility payment issue", minutos=10))
        s.execute(text("DELETE FROM leads_snapshot WHERE lead_id = :r"), {"r": REF})
        s.execute(
            text(
                "INSERT INTO leads_snapshot (lead_id, nome, telefone, corretora_id, "
                "                            etapa_kommo, ultima_sync) "
                "VALUES (:r, 'Lead Teste Cobranca', :t, 1, '10000005', NOW())"
            ),
            {"r": REF, "t": TEL},
        )
        s.commit()
    try:
        r = TestClient(app).post(
            "/painel/conversa/reengajar",
            json={"lead_id": REF, "texto": "oi, tudo bem?"},
            headers={"X-Painel-User": "corretor"},
        )
        assert r.status_code == 422, r.text
        assert r.json()["detail"] == "linha_bloqueada_cobranca"
        assert chamou == [], "não pode nem tentar enviar"
    finally:
        with SessionLocal() as s:
            s.execute(text("DELETE FROM leads_snapshot WHERE lead_id = :r"), {"r": REF})
            s.commit()
