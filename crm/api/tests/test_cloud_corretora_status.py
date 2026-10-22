"""Status de entrega das linhas de CORRETORA (`POST /cloud/corretora/{slug}/status`).

O webhook gravava a chave `status`, que ninguém lê. Quem consome delivery — o
anexo do painel e o alerta de "linha com problema" (3+ `failed` no dia) — filtra
por `delivery_status`, a chave que a linha da Pessoa12 usa. Efeito prático: a Pessoa11 e a
Pessoa10, as duas que mais mandam mensagem, ficavam fora do alerta, e falha de
entrega em massa passaria despercebida.

Descoberto em 03/08/2026 ao investigar suspeita de restrição na linha da Pessoa11:
355 mensagens enviadas em dois dias, nenhuma com `delivery_status` — não porque
o status não chegava, mas porque chegava com outro nome.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.db.session import SessionLocal
from app.main import app
from app.services import cloud_lines
from sqlalchemy import text as sql_text

SECRET = "segredo-de-teste-status"
PHONE_ID_CORRETOR = "000000000000000"
INSTANCE_CORRETOR = "acme_corretor"
WAMID = "wamid.TESTE.STATUS.CORRETOR"
LEAD = "+5500000000000"


@pytest.fixture(autouse=True)
def _linha_corretor_registrada(monkeypatch):
    corretor = cloud_lines.CloudLine(
        phone_id=PHONE_ID_CORRETOR, slug="corretor", papel="corretora",
        env_file="/etc/agencia/review.env", token_var="META_REVIEW_TOKEN",
    )
    monkeypatch.setattr(cloud_lines, "_LINES", {PHONE_ID_CORRETOR: corretor})
    monkeypatch.setattr(cloud_lines, "_BY_SLUG", {"corretor": PHONE_ID_CORRETOR})
    monkeypatch.setattr(settings, "cloud_webhook_secret", SECRET)
    yield


@pytest.fixture(autouse=True)
def _msg_gravada():
    """Uma mensagem enviada pela linha da Pessoa11, à espera do status."""
    def _wipe() -> None:
        with SessionLocal() as s:
            s.execute(sql_text("DELETE FROM mensagens_whatsapp WHERE instance = :i"),
                      {"i": INSTANCE_CORRETOR})
            s.commit()

    _wipe()
    with SessionLocal() as s:
        s.execute(
            sql_text(
                """
                INSERT INTO mensagens_whatsapp
                    (id, evolution_message_id, instance, remote_jid,
                     contato_telefone, from_corretora, tipo_mensagem,
                     conteudo_texto, timestamp_evolution, recebido_em)
                VALUES (gen_random_uuid(), :w, :i, :jid, :tel, true, 'text',
                        'oi', now(), now())
                """
            ),
            {"w": WAMID, "i": INSTANCE_CORRETOR,
             "jid": f"{LEAD}@s.whatsapp.net", "tel": LEAD},
        )
        s.commit()
    yield
    _wipe()


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


def _meta(wamid: str = WAMID) -> dict | None:
    with SessionLocal() as s:
        return s.execute(
            sql_text(
                "SELECT conteudo_metadata FROM mensagens_whatsapp "
                "WHERE evolution_message_id = :w"
            ),
            {"w": wamid},
        ).scalar()


def _post(client: TestClient, statuses: list[dict], slug: str = "corretor"):
    return client.post(
        f"/cloud/corretora/{slug}/status",
        json={"statuses": statuses},
        headers={"X-Cloud-Secret": SECRET},
    )


def test_grava_na_chave_que_o_painel_le(client):
    """A regressão que motivou o teste: `status` não serve, tem que ser
    `delivery_status` — é por ela que painel e alerta filtram."""
    r = _post(client, [{"id": WAMID, "status": "delivered", "timestamp": "1785766638"}])
    assert r.status_code == 200, r.text

    meta = _meta() or {}
    assert meta.get("delivery_status") == "delivered"
    assert meta.get("delivery_ts"), "sem timestamp o painel não ordena a entrega"


def test_failed_guarda_o_motivo(client):
    """Sem `delivery_error` o alerta diz que falhou mas não diz por quê — e a
    diferença entre 'lead bloqueou' e 'linha restrita' é o motivo."""
    r = _post(client, [{
        "id": WAMID, "status": "failed", "timestamp": "1785766638",
        "errors": [{"code": 131049, "title": "Mensagem não entregue"}],
    }])
    assert r.status_code == 200

    meta = _meta() or {}
    assert meta.get("delivery_status") == "failed"
    assert meta.get("delivery_error") == "Mensagem não entregue"


def test_status_novo_sobrescreve_o_anterior(client):
    """sent → delivered → read na mesma mensagem: vale o último."""
    for st in ("sent", "delivered", "read"):
        assert _post(client, [{"id": WAMID, "status": st}]).status_code == 200
    assert (_meta() or {}).get("delivery_status") == "read"


def test_wamid_desconhecido_nao_quebra(client):
    """A Meta manda status de mensagem que o copiloto nunca gravou — ignora
    sem derrubar o webhook (senão perde os status válidos do mesmo lote)."""
    r = _post(client, [{"id": "wamid.NAO.EXISTE", "status": "delivered"}])
    assert r.status_code == 200
    assert _meta("wamid.NAO.EXISTE") is None


def test_linha_desconhecida_e_404(client):
    assert _post(client, [{"id": WAMID, "status": "delivered"}], slug="fulana").status_code == 404


def test_sem_secret_e_401(client):
    r = client.post(f"/cloud/corretora/corretor/status", json={"statuses": []})
    assert r.status_code == 401
