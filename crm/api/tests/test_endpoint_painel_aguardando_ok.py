"""GET /painel/conversa/aguardando — o card que JÁ EXISTE, com a limpeza do "OK".

Este é o card que acende no CRM/Chat quando o lead falou por último. Até
08/2026 ele contava QUALQUER última mensagem do lead: "ok", "obrigado" e "👍"
viravam pendência, e a corretora mandava joinha só pra limpar a tela.

Aqui a gente trava o conserto: só entra quem PEDE resposta — e o endpoint
expõe `total_bruto`/`encerramentos_filtrados` pra dar pra conferir o que sumiu.
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

TEL_PERGUNTA = "+5500000000000"
TEL_OK = "+5500000000000"
TEL_OBRIGADO = "+5500000000000"
TEL_EMOJI = "+5500000000000"
TEL_AUDIO = "+5500000000000"      # mídia → mantém (não dá pra ler)
TEL_DUVIDA = "+5500000000000"     # "sim" → na dúvida MANTÉM
TEL_ELA_FALOU = "+5500000000000"  # corretora falou por último → fora


def _hdr(user: str) -> dict[str, str]:
    return {"X-Painel-User": user}


def _ins(s, *, tel: str, from_corretora: bool, texto: str,
         tipo: str = "text", at: datetime | None = None) -> str:
    mid = f"WAMID-AGO-{uuid.uuid4().hex[:12]}"
    s.execute(
        sql_text(
            "INSERT INTO mensagens_whatsapp "
            "(id, evolution_message_id, instance, corretora_id, remote_jid, "
            " contato_telefone, contato_nome, from_corretora, tipo_mensagem, "
            " conteudo_texto, timestamp_evolution) "
            "VALUES (gen_random_uuid(), :mid, 'acme_corretora_c', 1, :jid, :tel, "
            "        :nome, :fromc, :tipo, :texto, :at)"
        ),
        {"mid": mid, "jid": f"{tel}@s.whatsapp.net", "tel": tel,
         "nome": f"C {tel[-4:]}", "fromc": from_corretora, "tipo": tipo,
         "texto": texto, "at": at or (AGORA - timedelta(hours=2))},
    )
    return mid


@pytest.fixture(scope="module")
def seed():
    from app.db.session import SessionLocal

    mids: list[str] = []
    with SessionLocal() as s:
        mids.append(_ins(s, tel=TEL_PERGUNTA, from_corretora=False,
                         texto="qual a metragem do apartamento?"))
        mids.append(_ins(s, tel=TEL_OK, from_corretora=False, texto="ok"))
        mids.append(_ins(s, tel=TEL_OBRIGADO, from_corretora=False,
                         texto="Muito obrigada!"))
        mids.append(_ins(s, tel=TEL_EMOJI, from_corretora=False, texto="👍"))
        mids.append(_ins(s, tel=TEL_AUDIO, from_corretora=False, texto="",
                         tipo="audio"))
        mids.append(_ins(s, tel=TEL_DUVIDA, from_corretora=False, texto="sim"))
        mids.append(_ins(s, tel=TEL_ELA_FALOU, from_corretora=True,
                         texto="te mando agora"))
        s.commit()
    yield
    with SessionLocal() as s:
        s.execute(sql_text("DELETE FROM mensagens_whatsapp "
                           "WHERE evolution_message_id = ANY(:ids)"), {"ids": mids})
        s.commit()


@pytest.fixture
def client(seed):
    from app.main import app
    return TestClient(app)


def _p8(tel: str) -> str:
    return tel[-8:]


def test_ok_obrigado_e_emoji_saem_da_lista(client):
    body = client.get("/painel/conversa/aguardando?dias=7", headers=_hdr("corretor")).json()
    tels = body["telefones"]
    assert _p8(TEL_PERGUNTA) in tels
    assert _p8(TEL_OK) not in tels
    assert _p8(TEL_OBRIGADO) not in tels
    assert _p8(TEL_EMOJI) not in tels


def test_midia_e_duvida_continuam_na_lista(client):
    """A trava: erro pra menos custa um clique; erro pra mais custa um lead."""
    tels = client.get("/painel/conversa/aguardando?dias=7",
                      headers=_hdr("corretor")).json()["telefones"]
    assert _p8(TEL_AUDIO) in tels, "áudio sumiu da lista — não dá pra ler o conteúdo"
    assert _p8(TEL_DUVIDA) in tels, '"sim" sumiu da lista — a trava foi violada'


def test_corretora_falou_por_ultimo_nunca_entrou(client):
    tels = client.get("/painel/conversa/aguardando?dias=7",
                      headers=_hdr("corretor")).json()["telefones"]
    assert _p8(TEL_ELA_FALOU) not in tels


def test_expoe_quantos_foram_filtrados(client):
    body = client.get("/painel/conversa/aguardando?dias=7", headers=_hdr("corretor")).json()
    assert body["total"] == len(body["telefones"])
    assert body["total_bruto"] >= body["total"]
    assert body["encerramentos_filtrados"] >= 3
    assert body["total_bruto"] - body["total"] == body["encerramentos_filtrados"]


def test_escopo_por_corretora(client):
    # corretor não vê conversa da corretor
    tels = client.get("/painel/conversa/aguardando?dias=7",
                      headers=_hdr("corretor")).json()["telefones"]
    assert _p8(TEL_PERGUNTA) not in tels


def test_sem_header_403(client):
    assert client.get("/painel/conversa/aguardando").status_code == 403
