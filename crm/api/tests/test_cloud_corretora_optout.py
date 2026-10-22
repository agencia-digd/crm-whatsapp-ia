"""Opt-out nas linhas de corretora que NÃO são a da Corretor.

Até 23/08/2026 só o slug da Corretor tratava "pare de me mandar mensagem" aqui — o
carrossel dela chama `carrossel_corretor.processar_inbound`. Nas linhas da Pessoa10 e da
Pessoa11 o clique em "Parar mensagens" era gravado pelo api-disparos no tenant do
HUNTER, ou seja, num lugar que a Acme não consulta: o lead seguia elegível
pra cadência depois de ter pedido pra sair.

Achado investigando o lead que, pela linha da Pessoa10, recebeu a auto-resposta do
Hunter (o reel do Gustavo) ao clicar nos botões de um template da Acme.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api import cloud_corretora
from app.config import settings
from app.main import app
from app.services import cloud_lines

SECRET = "segredo-de-teste-optout"
PHONE_ID_CORRETOR = "000000000000000"
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


@pytest.fixture()
def marcados(monkeypatch):
    """Captura quem foi marcado como opt-out (sem tocar no banco)."""
    chamados: list[str] = []
    monkeypatch.setattr(
        cloud_corretora.atendimento_corretor, "marcar_opt_out", chamados.append
    )
    return chamados


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def _inbound(client, texto: str, tipo: str = "button"):
    msg = {"from": LEAD, "id": f"wamid.optout.{abs(hash(texto))}", "type": tipo,
           "timestamp": "1756000000"}
    if tipo == "button":
        msg["button"] = {"text": texto}
    else:
        msg["text"] = {"body": texto}
    return client.post(
        "/cloud/corretora/corretor/inbound",
        json={"messages": [msg], "contacts": [
            {"wa_id": LEAD, "profile": {"name": "Isaac"}}]},
        headers={"X-Cloud-Secret": SECRET},
    )


def test_botao_parar_mensagens_na_linha_da_corretor_vira_optout(client, marcados):
    r = _inbound(client, "Parar mensagens")
    assert r.status_code == 200
    assert marcados == [LEAD]


def test_pedido_escrito_na_linha_da_corretor_vira_optout(client, marcados):
    r = _inbound(client, "não quero mais receber mensagens", tipo="text")
    assert r.status_code == 200
    assert marcados == [LEAD]


def test_conversa_normal_nao_vira_optout(client, marcados):
    """Falso positivo aqui mata um lead vivo — "para" é preposição."""
    r = _inbound(client, "quero um apê para minha mãe", tipo="text")
    assert r.status_code == 200
    assert marcados == []
