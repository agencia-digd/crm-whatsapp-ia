"""Acesso de LEITURA pra quem disparou o remarketing — só se o lead responder.

POR QUE EXISTE (decisão Dona, 03/09/2026 — caso Rebeca):
a nutrição dispara TODO toque pela linha da Pessoa12 (`nutricao.py`, spec 10/07). Se
o lead responde, a conversa fica viva no aparelho da Pessoa12 — mas o card é da dona
(Pessoa5), e o gate de escopo dava 403 na Pessoa12. Resultado: quem tinha a conversa
não tinha o card, quem tinha o card não tinha a conversa, e a resposta do lead
("quando tiver alguma oportunidade boa pode mandar") ficou um dia parada.

REGRA (literal, como a Dona pediu): quem disparou vê o lead **se, e só se, o
cliente responder na linha dela**. Silêncio não abre nada — disparo sozinho não
dá acesso a lead de outra corretora.

E o acesso é de LEITURA. Atender pelo painel (enviar, reengajar, mover,
transferir, adotar) continua sendo só da dona: a linha que disparou responde
pelo próprio WhatsApp, onde a conversa já está.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db.session import SessionLocal
from app.main import app

TENANT = 1
REF = "9900000101"                 # lead da CORRETOR (dona), fora da faixa real
TEL = "+5500000000000"
SUF = "90000101"
CORRETOR, CORRETOR, CORRETOR = 3, 1, 2


def _msg(s, cid: int, texto: str, minutos: int, *, from_corretora: bool,
         instance: str, mid: str) -> None:
    s.execute(
        text(
            """
            INSERT INTO mensagens_whatsapp
                (evolution_message_id, instance, corretora_id, remote_jid,
                 contato_telefone, from_corretora, tipo_mensagem,
                 conteudo_texto, timestamp_evolution, tenant_id)
            VALUES (:mid, :inst, :cid, :jid, :tel, :fc, 'text', :txt, :ts, :t)
            """
        ),
        {
            "mid": mid, "inst": instance, "cid": cid,
            "jid": f"{SUF}@s.whatsapp.net", "tel": TEL, "fc": from_corretora,
            "txt": texto,
            "ts": datetime.now(timezone.utc) - timedelta(minutes=minutos),
            "t": TENANT,
        },
    )


def _limpa(s) -> None:
    s.execute(text("DELETE FROM mensagens_whatsapp WHERE contato_telefone = :t"),
              {"t": TEL})
    s.execute(text("DELETE FROM leads_snapshot WHERE lead_id = :r"), {"r": REF})
    s.execute(text("DELETE FROM leads WHERE external_ref = :r AND tenant_id = :t"),
              {"r": REF, "t": TENANT})


@pytest.fixture()
def lead_da_corretor():
    """Lead cuja dona é a Pessoa10, com um toque de remarketing saído pela linha da Pessoa12."""
    with SessionLocal() as s:
        _limpa(s)
        s.execute(
            text(
                "INSERT INTO leads_snapshot "
                "(lead_id, nome, telefone, corretora_id, etapa_kommo, ultima_sync) "
                "VALUES (:r, 'Lead da Pessoa10', :tel, :cid, '10000005', NOW())"
            ),
            {"r": REF, "tel": TEL, "cid": CORRETOR},
        )
        s.execute(
            text(
                "INSERT INTO leads (tenant_id, external_ref, nome, owner_id, "
                "                   origem, created_at, updated_at) "
                "VALUES (:t, :r, 'Lead da Pessoa10', :owner, 'teste', NOW(), NOW())"
            ),
            {"t": TENANT, "r": REF, "owner": CORRETOR},
        )
        _msg(s, CORRETOR, "oi, tudo bem?", 5000, from_corretora=True,
             instance="acme_corretora_c", mid="acc-corretor-1")
        _msg(s, CORRETOR, "tem imóvel que entra e sai do mercado", 3000,
             from_corretora=True, instance="acme_corretora_a", mid="acc-corretor-toque")
        s.commit()
    try:
        yield
    finally:
        with SessionLocal() as s:
            _limpa(s)
            s.commit()


@pytest.fixture()
def client():
    return TestClient(app)


def _h(user: str) -> dict[str, str]:
    return {"X-Painel-User": user}


def _responde(minutos: int = 60) -> None:
    with SessionLocal() as s:
        _msg(s, CORRETOR, "Quando tiver oportunidade boa pode mandar", minutos,
             from_corretora=False, instance="acme_corretora_a", mid="acc-corretor-resp")
        s.commit()


# ── Silêncio não abre nada ────────────────────────────────────────────────────

def test_disparo_sem_resposta_nao_da_acesso(lead_da_corretor, client):
    """Só o toque saiu. O lead não falou → a Pessoa12 segue barrada."""
    r = client.get(f"/painel/conversa?lead_id={REF}", headers=_h("corretor"))
    assert r.status_code == 403
    assert r.json()["detail"] == "lead_de_outra_corretora"


def test_resposta_antiga_nao_da_acesso(lead_da_corretor, client):
    """Conversa morta (resposta há mais de 30 dias) fecha de novo."""
    _responde(minutos=60 * 24 * 45)
    r = client.get(f"/painel/conversa?lead_id={REF}", headers=_h("corretor"))
    assert r.status_code == 403


def test_corretora_sem_conversa_nenhuma_continua_barrada(lead_da_corretor, client):
    """A Pessoa11 nunca falou com esse lead: responder pra Pessoa12 não abre pra ela."""
    _responde()
    r = client.get(f"/painel/conversa?lead_id={REF}", headers=_h("corretor"))
    assert r.status_code == 403


# ── Resposta abre a leitura ───────────────────────────────────────────────────

def test_resposta_na_linha_abre_leitura(lead_da_corretor, client):
    """O lead respondeu na linha da Pessoa12 → ela lê a thread inteira."""
    _responde()
    r = client.get(f"/painel/conversa?lead_id={REF}&hours_back=4320", headers=_h("corretor"))
    assert r.status_code == 200
    body = r.json()
    textos = [m["text"] for m in body["mensagens"]]
    assert "Quando tiver oportunidade boa pode mandar" in textos
    # histórico da dona vem junto — é o contexto que ela precisa pra responder
    assert "oi, tudo bem?" in textos


def test_leitura_emprestada_vem_sinalizada(lead_da_corretor, client):
    """O front precisa saber que o lead é de outra: não é card dela."""
    _responde()
    body = client.get(f"/painel/conversa?lead_id={REF}", headers=_h("corretor")).json()
    assert body["acesso"] == "leitura_por_resposta"
    assert body["dono_outra_corretora"] == "Pessoa5"


def test_dona_e_admin_nao_sao_afetados(lead_da_corretor, client):
    """Quem já podia continua podendo, e sem o carimbo de acesso emprestado."""
    _responde()
    for user in ("corretor", "dona"):
        body = client.get(f"/painel/conversa?lead_id={REF}", headers=_h(user)).json()
        assert body.get("acesso") is None


# ── Leitura ≠ atendimento ─────────────────────────────────────────────────────

def test_acesso_por_resposta_nao_deixa_enviar(lead_da_corretor, client):
    """Ler sim, atender pelo painel não: envio continua sendo da dona."""
    _responde()
    r = client.post(
        "/painel/conversa/enviar",
        json={"lead_id": REF, "texto": "oi"},
        headers=_h("corretor"),
    )
    assert r.status_code == 403
    assert r.json()["detail"] == "lead_de_outra_corretora"


def test_acesso_por_resposta_nao_deixa_transferir(lead_da_corretor, client):
    """Nem se apossar do card sozinha."""
    _responde()
    with SessionLocal() as s:
        lead_pk = s.execute(
            text("SELECT id FROM leads WHERE external_ref = :r AND tenant_id = :t"),
            {"r": REF, "t": TENANT},
        ).scalar_one()
    r = client.post(
        "/painel/crm/transferir",
        json={"lead_id": lead_pk, "corretora_destino": CORRETOR},
        headers=_h("corretor"),
    )
    assert r.status_code == 403
