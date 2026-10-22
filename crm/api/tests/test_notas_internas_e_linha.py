"""Leva 2 do Chat — notas internas na thread + tag de linha por mensagem.

Notas internas: POST /painel/lead/{id}/nota (INSERT puro em
observacao_corretora, sem IA/Kommo) + GET /painel/lead/{id}/notas.
Privacidade: corretora só no lead dela; admin em qualquer.

Tag de linha: helpers _mapa_instance_linha / _linha_da_instance /
_linhas_por_msg_ids (mensagens_whatsapp.instance → label da corretora).

Precisa de DB real (TEST_DATABASE_URL — ver conftest.py).
"""
from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text as sql_text

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="precisa de TEST_DATABASE_URL (Postgres de teste)",
)


@pytest.fixture
def client():
    from app.main import app
    return TestClient(app)


def _hdr(user):
    return {"X-Painel-User": user}


@pytest.fixture
def lead_da_corretor():
    """Seed: lead no snapshot pertencendo à Pessoa5 (corretora_id=1)."""
    from app.db.session import SessionLocal
    lid = f"t-nota-{uuid.uuid4().hex[:10]}"
    with SessionLocal() as s:
        s.execute(
            sql_text(
                "INSERT INTO leads_snapshot (lead_id, nome, telefone, corretora_id) "
                "VALUES (:lid, 'Lead Teste Nota', '+5500000000000', 1)"
            ),
            {"lid": lid},
        )
        s.commit()
    yield lid
    with SessionLocal() as s:
        s.execute(sql_text("DELETE FROM observacao_corretora WHERE lead_id = :lid"), {"lid": lid})
        s.execute(sql_text("DELETE FROM leads_snapshot WHERE lead_id = :lid"), {"lid": lid})
        s.commit()


# ── notas internas ────────────────────────────────────────────────────────────

def test_nota_cria_e_lista_sem_ia(client, lead_da_corretor):
    r = client.post(
        f"/painel/lead/{lead_da_corretor}/nota",
        json={"texto": "liguei, não atendeu"},
        headers=_hdr("corretor"),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["nota"]["autor"] == "corretor"
    assert body["nota"]["via_ia"] is False

    r2 = client.get(f"/painel/lead/{lead_da_corretor}/notas", headers=_hdr("corretor"))
    assert r2.status_code == 200
    notas = r2.json()["notas"]
    assert len(notas) == 1
    assert notas[0]["texto"] == "liguei, não atendeu"
    assert notas[0]["corretora"] == "Pessoa10"
    assert notas[0]["via_ia"] is False
    assert notas[0]["at"]  # timestamp presente pra intercalar na thread

    # nota interna NÃO passou pela IA nem virou nota externa
    from app.db.session import SessionLocal
    with SessionLocal() as s:
        row = s.execute(
            sql_text(
                "SELECT ia_nota_kommo, kommo_note_ok FROM observacao_corretora "
                "WHERE lead_id = :lid"
            ),
            {"lid": lead_da_corretor},
        ).mappings().one()
    assert row["ia_nota_kommo"] is None
    assert row["kommo_note_ok"] is False


def test_nota_privacidade_corretora_alheia(client, lead_da_corretor):
    # corretor (corretora 3) não anota nem lê lead da Pessoa10 (corretora 1)
    r = client.post(
        f"/painel/lead/{lead_da_corretor}/nota",
        json={"texto": "tentativa indevida"},
        headers=_hdr("corretor"),
    )
    assert r.status_code == 403
    r2 = client.get(f"/painel/lead/{lead_da_corretor}/notas", headers=_hdr("corretor"))
    assert r2.status_code == 403


def test_nota_admin_anota_lead_de_corretora(client, lead_da_corretor):
    r = client.post(
        f"/painel/lead/{lead_da_corretor}/nota",
        json={"texto": "nota da gestão"},
        headers=_hdr("dona"),
    )
    assert r.status_code == 200
    # admin não tem corretora própria → nota herda a dona do lead (Pessoa10)
    assert r.json()["nota"]["corretora"] == "Pessoa10"


def test_nota_vazia_422(client, lead_da_corretor):
    r = client.post(
        f"/painel/lead/{lead_da_corretor}/nota",
        json={"texto": "  "},
        headers=_hdr("corretor"),
    )
    assert r.status_code == 422


# ── tag de linha ──────────────────────────────────────────────────────────────

def test_mapa_instance_linha_cadastro_e_fixas():
    import app.api.painel as painel
    painel._LINHA_MAPA_CACHE.update(at=0.0, mapa={})  # zera cache entre testes
    mapa = painel._mapa_instance_linha()
    # do cadastro (seed do conftest)
    assert mapa["acme_corretora_c"] == "Pessoa10"
    assert mapa["acme_corretor"] == "Pessoa11"
    assert mapa["acme_corretora_a"] == "Pessoa12"
    # fixas (instâncias históricas/sistema fora do cadastro)
    assert mapa["acme_corretor"] == "Pessoa10"
    assert mapa["recuperador"] == "Recuperador"
    assert mapa["assistente"] == "Corretor"
    assert mapa["cloud_assistente"] == "Corretor"


def test_linha_da_instance_desconhecida_volta_cru():
    import app.api.painel as painel
    assert painel._linha_da_instance("instancia_zumbi") == "instancia_zumbi"
    assert painel._linha_da_instance(None) is None


def test_linhas_por_msg_ids(lead_da_corretor):
    import app.api.painel as painel
    from app.db.session import SessionLocal

    mid_corretor = f"WAMID-{uuid.uuid4().hex[:12]}"
    mid_corretor = f"WAMID-{uuid.uuid4().hex[:12]}"
    with SessionLocal() as s:
        for mid, inst, cid in ((mid_corretor, "acme_corretora_a", 3), (mid_corretor, "acme_corretora_c", 1)):
            s.execute(
                sql_text(
                    "INSERT INTO mensagens_whatsapp "
                    "(id, evolution_message_id, instance, corretora_id, remote_jid, "
                    " contato_telefone, from_corretora, tipo_mensagem, timestamp_evolution) "
                    "VALUES (gen_random_uuid(), :mid, :inst, :cid, '+5500000000000@s.whatsapp.net', "
                    "        '+5500000000000', false, 'text', now())"
                ),
                {"mid": mid, "inst": inst, "cid": cid},
            )
        s.commit()
    try:
        linhas = painel._linhas_por_msg_ids([mid_corretor, mid_corretor, "inexistente"])
        assert linhas[mid_corretor] == "Pessoa12"
        assert linhas[mid_corretor] == "Pessoa10"
        assert "inexistente" not in linhas
    finally:
        with SessionLocal() as s:
            s.execute(
                sql_text("DELETE FROM mensagens_whatsapp WHERE evolution_message_id = ANY(:ids)"),
                {"ids": [mid_corretor, mid_corretor]},
            )
            s.commit()
