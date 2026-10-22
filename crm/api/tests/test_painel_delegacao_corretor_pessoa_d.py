"""Delegação de atendimento: a Pessoa12 (3) conversa pela linha da Pessoa7 (7).

A Pessoa7 liga/resgata pelo chip virtual; quando o lead responde no WhatsApp
quem conversa é a Pessoa12, pelo painel, e a mensagem sai pelo NÚMERO DA PESSOA_D — o
lead vê uma linha só. Prova aqui que:

1. o mapa `DELEGACAO_ATENDIMENTO` é uma via de mão única (Pessoa12→Pessoa7, nunca o
   contrário) e não vaza pras outras corretoras;
2. o escopo das listas do chat (`_cids_atendimento`) inclui a linha delegada;
3. no envio, a Pessoa12 passa pelo gate do lead da Pessoa7 e a saída é a instância
   DELA (`acme_pessoa_d`), não a da Pessoa12 — e a Pessoa10 continua levando 403.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.api import painel
from app.db.session import SessionLocal
from app.main import app

PESSOA_D_ID = 7
REF_DA_PESSOA_D = "9900007001"
TEL_LEAD = "+5500000000000"


# ── 1. Mapa e helpers ────────────────────────────────────────────────────────

def test_corretor_pode_atender_pessoa_d():
    assert painel._pode_atender(3, PESSOA_D_ID) is True


def test_delegacao_nao_e_de_mao_dupla():
    """A Pessoa7 NÃO herda a linha da Pessoa12 — ela só empresta a dela."""
    assert painel._pode_atender(PESSOA_D_ID, 3) is False


def test_delegacao_nao_vaza_pras_outras():
    assert painel._pode_atender(1, PESSOA_D_ID) is False
    assert painel._pode_atender(2, PESSOA_D_ID) is False


def test_corretora_continua_atendendo_a_propria():
    assert painel._pode_atender(1, 1) is True
    assert painel._pode_atender(PESSOA_D_ID, PESSOA_D_ID) is True


def test_admin_atende_qualquer_uma():
    assert painel._pode_atender(None, PESSOA_D_ID) is True


def test_escopo_das_listas_inclui_a_delegada():
    # 01/10/2026: a Corretor (8) entrou na delegação da Pessoa12, junto da Pessoa7
    assert painel._cids_atendimento(3) == [3, PESSOA_D_ID, 8]
    assert painel._cids_atendimento(1) == [1]
    assert painel._cids_atendimento(PESSOA_D_ID) == [PESSOA_D_ID]
    # admin = sem filtro (as consultas tratam NULL como "todas")
    assert painel._cids_atendimento(None) is None


# ── 2. Gate do chat, no endpoint ─────────────────────────────────────────────

@pytest.fixture()
def lead_da_pessoa_d():
    """Pessoa7 (id 7, linha Cloud) + um lead dela. Limpa no fim."""
    with SessionLocal() as s:
        s.execute(
            text("DELETE FROM leads_snapshot WHERE lead_id = :ref"),
            {"ref": REF_DA_PESSOA_D},
        )
        s.execute(text("DELETE FROM corretoras WHERE id = :id"), {"id": PESSOA_D_ID})
        s.execute(
            text(
                "INSERT INTO corretoras (id, nome, telefone, telefone_normalizado, "
                "                        kommo_user_id, evolution_instance, ativo) "
                "VALUES (:id, 'Pessoa7', '+5500000000000', '+5500000000000', "
                "        NULL, 'acme_pessoa_d', TRUE)"
            ),
            {"id": PESSOA_D_ID},
        )
        s.execute(
            text(
                "INSERT INTO leads_snapshot "
                "(lead_id, nome, telefone, corretora_id, etapa_kommo, ultima_sync) "
                "VALUES (:ref, 'Lead da Pessoa7', :tel, :cid, '10000005', NOW())"
            ),
            {"ref": REF_DA_PESSOA_D, "tel": TEL_LEAD, "cid": PESSOA_D_ID},
        )
        s.commit()
    try:
        yield
    finally:
        with SessionLocal() as s:
            s.execute(
                text("DELETE FROM leads_snapshot WHERE lead_id = :ref"),
                {"ref": REF_DA_PESSOA_D},
            )
            s.execute(text("DELETE FROM corretoras WHERE id = :id"), {"id": PESSOA_D_ID})
            s.commit()


@pytest.fixture()
def client():
    return TestClient(app)


def test_corretor_envia_pelo_numero_da_pessoa_d(client, lead_da_pessoa_d):
    """Passa o gate E a saída é a instância da Pessoa7 (DRY_RUN na suíte)."""
    r = client.post(
        "/painel/conversa/enviar",
        json={"lead_id": REF_DA_PESSOA_D, "texto": "Oi! Sou a Pessoa12, vi que você ligou."},
        headers={"X-Painel-User": "corretor"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["enviado_por_instance"] == "acme_pessoa_d"


def test_corretor_continua_barrada_no_lead_da_pessoa_d(client, lead_da_pessoa_d):
    r = client.post(
        "/painel/conversa/enviar",
        json={"lead_id": REF_DA_PESSOA_D, "texto": "oi"},
        headers={"X-Painel-User": "corretor"},
    )
    assert r.status_code == 403
    assert r.json()["detail"] == "lead_de_outra_corretora"


# ── 3. Corretor (8): mesma delegação, desde 01/10/2026 ────────────────────────────
#
# Decisão da Dona: o lead que a Corretor adota na ligação fica no funil DELA, e a Pessoa12
# trabalha por lá. Aqui é o caminho inteiro no Postgres: conversa pelo número da
# Corretor e nota no card — os dois gates (`_pode_atender` e `_checar_escopo_lead`).

CORRETOR_ID = 8
REF_DA_CORRETOR = "9900008001"
TEL_LEAD_CORRETOR = "+5500000000000"


def _limpa_corretor(s) -> None:
    s.execute(
        text("DELETE FROM observacao_corretora WHERE lead_id = :ref"),
        {"ref": REF_DA_CORRETOR},
    )
    s.execute(
        text("DELETE FROM leads_snapshot WHERE lead_id = :ref"), {"ref": REF_DA_CORRETOR}
    )
    s.execute(
        text("DELETE FROM leads WHERE external_ref = :ref AND tenant_id = 1"),
        {"ref": REF_DA_CORRETOR},
    )
    s.execute(text("DELETE FROM corretoras WHERE id = :id"), {"id": CORRETOR_ID})


@pytest.fixture()
def lead_da_corretor():
    """Corretor (id 8, linha Cloud) + um lead dela no snapshot e na espinha."""
    with SessionLocal() as s:
        _limpa_corretor(s)
        s.execute(
            text(
                "INSERT INTO corretoras (id, nome, telefone, telefone_normalizado, "
                "                        kommo_user_id, evolution_instance, ativo) "
                "VALUES (:id, 'Corretor', '+5500000000000', '+5500000000000', "
                "        NULL, 'acme_corretor', TRUE)"
            ),
            {"id": CORRETOR_ID},
        )
        s.execute(
            text(
                "INSERT INTO leads_snapshot "
                "(lead_id, nome, telefone, corretora_id, etapa_kommo, ultima_sync) "
                "VALUES (:ref, 'Lead da Corretor', :tel, :cid, '10000005', NOW())"
            ),
            {"ref": REF_DA_CORRETOR, "tel": TEL_LEAD_CORRETOR, "cid": CORRETOR_ID},
        )
        s.execute(
            text(
                "INSERT INTO leads (tenant_id, external_ref, nome, telefone, owner_id, "
                "                   origem, created_at, updated_at) "
                "VALUES (1, :ref, 'Lead da Corretor', :tel, :cid, 'teste', NOW(), NOW())"
            ),
            {"ref": REF_DA_CORRETOR, "tel": TEL_LEAD_CORRETOR, "cid": CORRETOR_ID},
        )
        s.commit()
    try:
        yield
    finally:
        with SessionLocal() as s:
            _limpa_corretor(s)
            s.commit()


def test_corretor_pode_atender_corretor_e_e_mao_unica():
    assert painel._pode_atender(3, CORRETOR_ID) is True
    assert painel._pode_atender(CORRETOR_ID, 3) is False
    assert painel._pode_atender(CORRETOR_ID, PESSOA_D_ID) is False
    assert painel._pode_atender(PESSOA_D_ID, CORRETOR_ID) is False
    assert painel._pode_atender(1, CORRETOR_ID) is False
    assert painel._pode_atender(2, CORRETOR_ID) is False
    assert painel._cids_atendimento(CORRETOR_ID) == [CORRETOR_ID]


def test_corretor_envia_pelo_numero_da_corretor(client, lead_da_corretor):
    """Passa o gate E a saída é a instância da Corretor (DRY_RUN na suíte)."""
    r = client.post(
        "/painel/conversa/enviar",
        json={"lead_id": REF_DA_CORRETOR, "texto": "Oi! Sou a Pessoa12, a Corretor falou com você."},
        headers={"X-Painel-User": "corretor"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["enviado_por_instance"] == "acme_corretor"


def test_corretor_continua_barrada_no_lead_da_corretor(client, lead_da_corretor):
    r = client.post(
        "/painel/conversa/enviar",
        json={"lead_id": REF_DA_CORRETOR, "texto": "oi"},
        headers={"X-Painel-User": "corretor"},
    )
    assert r.status_code == 403
    assert r.json()["detail"] == "lead_de_outra_corretora"


def test_corretor_anota_no_card_da_corretor(client, lead_da_corretor):
    r = client.post(
        f"/painel/lead/{REF_DA_CORRETOR}/nota",
        json={"texto": "Liguei de volta, quer visitar sábado."},
        headers={"X-Painel-User": "corretor"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["nota"]["autor"] == "corretor"

    # a dona lê a nota que a Pessoa12 deixou no card dela
    r = client.get(
        f"/painel/lead/{REF_DA_CORRETOR}/notas", headers={"X-Painel-User": "corretor"}
    )
    assert r.status_code == 200, r.text
    assert [n["texto"] for n in r.json()["notas"]] == [
        "Liguei de volta, quer visitar sábado."
    ]


@pytest.mark.parametrize("user", ["corretor", "corretor", "pessoa_d"])
def test_nota_no_card_da_corretor_barrada_pra_quem_nao_tem_delegacao(
    client, lead_da_corretor, user
):
    r = client.post(
        f"/painel/lead/{REF_DA_CORRETOR}/nota",
        json={"texto": "não devia entrar"},
        headers={"X-Painel-User": user},
    )
    assert r.status_code == 403
    assert r.json()["detail"] == "lead_nao_pertence_corretora"
    r = client.get(
        f"/painel/lead/{REF_DA_CORRETOR}/notas", headers={"X-Painel-User": user}
    )
    assert r.status_code == 403
