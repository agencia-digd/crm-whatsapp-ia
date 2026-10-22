"""GET /painel/conversa/esperando_resposta — fila "Esperando resposta" do Chat V2.

Conversas cuja ÚLTIMA mensagem é DA CORRETORA (lead ainda não respondeu).

Cobre:
  - 403 sem header / usuário desconhecido;
  - entra só quem tem a última msg from_corretora=TRUE;
  - conversa onde o LEAD falou por último fica fora;
  - arquivado: última msg > `dias` (default 30) fica fora;
  - escopo: corretora vê só as conversas DELA; admin vê todas e filtra
    com ?corretora=<nome front>;
  - ordenação: espera mais LONGA primeiro (msg mais antiga em cima);
  - última msg tipo 'template' (cadência/reengajamento) fica fora por
    default; incluir_automaticas=1 traz de volta;
  - enriquecimento: nome/etapa do leads_snapshot quando o telefone tem lead.

Precisa de DB real (TEST_DATABASE_URL — ver conftest.py): a query usa
DISTINCT ON + regexp_replace + interval (Postgres).
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text as sql_text

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="precisa de TEST_DATABASE_URL (Postgres de teste)",
)

AGORA = datetime.now(timezone.utc)

# Telefones únicos do teste (sufixo-8 improvável de colidir com outros testes)
TEL_CORRETOR_ESPERA_LONGA = "+5500000000000"   # Pessoa10 falou por último há 10d
TEL_CORRETOR_ESPERA_CURTA = "+5500000000000"   # Pessoa10 falou por último há 2h
TEL_CORRETOR_LEAD_FALOU = "+5500000000000"     # lead falou por último → fora
TEL_CORRETOR_ARQUIVADA = "+5500000000000"      # Pessoa10 falou por último há 40d → fora
TEL_CORRETOR_TEMPLATE = "+5500000000000"         # última msg da Pessoa12 é template → fora por default
TEL_CORRETOR_ESPERA = "+5500000000000"         # Pessoa11 falou por último há 1d

LEAD_SNAP_ID = f"t-esp-{uuid.uuid4().hex[:10]}"


def _hdr(user: str) -> dict[str, str]:
    return {"X-Painel-User": user}


def _ins_msg(s, *, tel: str, cid: int, instance: str, from_corretora: bool,
             at: datetime, tipo: str = "text", texto: str = "oi") -> str:
    mid = f"WAMID-ESP-{uuid.uuid4().hex[:12]}"
    s.execute(
        sql_text(
            "INSERT INTO mensagens_whatsapp "
            "(id, evolution_message_id, instance, corretora_id, remote_jid, "
            " contato_telefone, contato_nome, from_corretora, tipo_mensagem, "
            " conteudo_texto, timestamp_evolution) "
            "VALUES (gen_random_uuid(), :mid, :inst, :cid, :jid, :tel, :nome, "
            "        :fromc, :tipo, :texto, :at)"
        ),
        {
            "mid": mid, "inst": instance, "cid": cid,
            "jid": f"{tel}@s.whatsapp.net", "tel": tel,
            "nome": f"Contato {tel[-4:]}", "fromc": from_corretora,
            "tipo": tipo, "texto": texto, "at": at,
        },
    )
    return mid


@pytest.fixture(scope="module")
def seed():
    """Semeia as 6 conversas do cenário + 1 lead no snapshot. Limpa no fim."""
    from app.db.session import SessionLocal

    mids: list[str] = []
    with SessionLocal() as s:
        # Pessoa10 espera há 10 dias (lead falou antes, ela respondeu — sem retorno)
        mids.append(_ins_msg(s, tel=TEL_CORRETOR_ESPERA_LONGA, cid=1,
                             instance="acme_corretora_c", from_corretora=False,
                             at=AGORA - timedelta(days=11)))
        mids.append(_ins_msg(s, tel=TEL_CORRETOR_ESPERA_LONGA, cid=1,
                             instance="acme_corretora_c", from_corretora=True,
                             at=AGORA - timedelta(days=10),
                             texto="te mando as fotos amanhã"))
        # Pessoa10 espera há 2 horas
        mids.append(_ins_msg(s, tel=TEL_CORRETOR_ESPERA_CURTA, cid=1,
                             instance="acme_corretora_c", from_corretora=True,
                             at=AGORA - timedelta(hours=2)))
        # lead falou por último → NÃO entra
        mids.append(_ins_msg(s, tel=TEL_CORRETOR_LEAD_FALOU, cid=1,
                             instance="acme_corretora_c", from_corretora=True,
                             at=AGORA - timedelta(hours=5)))
        mids.append(_ins_msg(s, tel=TEL_CORRETOR_LEAD_FALOU, cid=1,
                             instance="acme_corretora_c", from_corretora=False,
                             at=AGORA - timedelta(hours=4)))
        # arquivada (40 dias) → NÃO entra com dias=30
        mids.append(_ins_msg(s, tel=TEL_CORRETOR_ARQUIVADA, cid=1,
                             instance="acme_corretora_c", from_corretora=True,
                             at=AGORA - timedelta(days=40)))
        # Pessoa12: última msg é TEMPLATE (cadência/reengajamento) → fora por default
        mids.append(_ins_msg(s, tel=TEL_CORRETOR_TEMPLATE, cid=3,
                             instance="acme_corretora_a", from_corretora=True,
                             at=AGORA - timedelta(days=1), tipo="template",
                             texto="[reengajamento: oi]"))
        # Pessoa11 espera há 1 dia
        mids.append(_ins_msg(s, tel=TEL_CORRETOR_ESPERA, cid=2,
                             instance="acme_corretor", from_corretora=True,
                             at=AGORA - timedelta(days=1, hours=1)))
        # lead no snapshot pro telefone da espera longa (nome + etapa do board)
        s.execute(
            sql_text(
                "INSERT INTO leads_snapshot (lead_id, nome, telefone, "
                " corretora_id, etapa_kommo) "
                "VALUES (:lid, 'Lead Snapshot Esp', :tel, 1, 'Em atendimento')"
            ),
            {"lid": LEAD_SNAP_ID, "tel": f"+{TEL_CORRETOR_ESPERA_LONGA}"},
        )
        s.commit()
    yield
    with SessionLocal() as s:
        s.execute(
            sql_text("DELETE FROM mensagens_whatsapp "
                     "WHERE evolution_message_id = ANY(:ids)"),
            {"ids": mids},
        )
        s.execute(
            sql_text("DELETE FROM leads_snapshot WHERE lead_id = :lid"),
            {"lid": LEAD_SNAP_ID},
        )
        s.commit()


@pytest.fixture
def client(seed):
    from app.main import app
    return TestClient(app)


def _tels(body: dict) -> list[str]:
    return [c["telefone"] for c in body["conversas"]]


# ── auth ──────────────────────────────────────────────────────────────────────

def test_sem_header_403(client):
    assert client.get("/painel/conversa/esperando_resposta").status_code == 403


def test_usuario_desconhecido_403(client):
    r = client.get("/painel/conversa/esperando_resposta",
                   headers=_hdr("hacker"))
    assert r.status_code == 403


# ── escopo corretora ──────────────────────────────────────────────────────────

def test_corretora_ve_so_as_dela_e_regras_de_entrada(client):
    r = client.get("/painel/conversa/esperando_resposta", headers=_hdr("corretor"))
    assert r.status_code == 200, r.text
    body = r.json()
    tels = _tels(body)
    # entram: espera longa + espera curta da Pessoa10
    assert f"+{TEL_CORRETOR_ESPERA_LONGA}" in tels
    assert f"+{TEL_CORRETOR_ESPERA_CURTA}" in tels
    # fora: lead falou por último, arquivada (>30d), conversas da Pessoa11/Pessoa12
    assert f"+{TEL_CORRETOR_LEAD_FALOU}" not in tels
    assert f"+{TEL_CORRETOR_ARQUIVADA}" not in tels
    assert f"+{TEL_CORRETOR_ESPERA}" not in tels
    assert f"+{TEL_CORRETOR_TEMPLATE}" not in tels
    assert body["viewer"]["isAdmin"] is False


def test_ordenacao_espera_mais_longa_primeiro(client):
    r = client.get("/painel/conversa/esperando_resposta", headers=_hdr("corretor"))
    tels = _tels(r.json())
    assert tels.index(f"+{TEL_CORRETOR_ESPERA_LONGA}") < tels.index(f"+{TEL_CORRETOR_ESPERA_CURTA}")


def test_enriquecimento_snapshot_nome_etapa_e_preview(client):
    r = client.get("/painel/conversa/esperando_resposta", headers=_hdr("corretor"))
    por_tel = {c["telefone"]: c for c in r.json()["conversas"]}
    longa = por_tel[f"+{TEL_CORRETOR_ESPERA_LONGA}"]
    assert longa["kommoLeadId"] == LEAD_SNAP_ID
    assert longa["nome"] == "Lead Snapshot Esp"
    assert longa["etapa"] == "Em atendimento"
    assert longa["preview"] == "te mando as fotos amanhã"
    assert longa["espera_horas"] and longa["espera_horas"] > 200  # ~10 dias
    # sem lead no board → kommoLeadId "tel:<dígitos>"
    curta = por_tel[f"+{TEL_CORRETOR_ESPERA_CURTA}"]
    assert curta["kommoLeadId"] == f"tel:{TEL_CORRETOR_ESPERA_CURTA}"
    assert curta["etapa"] is None


# ── admin ─────────────────────────────────────────────────────────────────────

def test_admin_ve_todas(client):
    r = client.get("/painel/conversa/esperando_resposta", headers=_hdr("dona"))
    assert r.status_code == 200
    body = r.json()
    tels = _tels(body)
    assert f"+{TEL_CORRETOR_ESPERA_LONGA}" in tels
    assert f"+{TEL_CORRETOR_ESPERA}" in tels
    assert body["viewer"]["isAdmin"] is True


def test_admin_filtra_por_corretora(client):
    r = client.get("/painel/conversa/esperando_resposta?corretora=Pessoa1",
                   headers=_hdr("dona"))
    tels = _tels(r.json())
    assert f"+{TEL_CORRETOR_ESPERA}" in tels
    assert f"+{TEL_CORRETOR_ESPERA_LONGA}" not in tels
    assert r.json()["filtro"] == "Pessoa1"


# ── automáticas (template) ────────────────────────────────────────────────────

def test_template_fora_por_default_e_dentro_com_flag(client):
    r = client.get("/painel/conversa/esperando_resposta", headers=_hdr("corretor"))
    assert f"+{TEL_CORRETOR_TEMPLATE}" not in _tels(r.json())

    r2 = client.get(
        "/painel/conversa/esperando_resposta?incluir_automaticas=1",
        headers=_hdr("corretor"),
    )
    por_tel = {c["telefone"]: c for c in r2.json()["conversas"]}
    assert f"+{TEL_CORRETOR_TEMPLATE}" in por_tel
    assert por_tel[f"+{TEL_CORRETOR_TEMPLATE}"]["automatica"] is True


# ── janela `dias` ─────────────────────────────────────────────────────────────

def test_dias_maior_traz_arquivada_de_volta(client):
    r = client.get("/painel/conversa/esperando_resposta?dias=60",
                   headers=_hdr("corretor"))
    assert f"+{TEL_CORRETOR_ARQUIVADA}" in _tels(r.json())
