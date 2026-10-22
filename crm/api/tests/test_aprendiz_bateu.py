"""Bateu ou não — rascunho da IA × resposta real da corretora.

Cobre:
  - heurística pura (normalização, similaridade, thresholds, rajada);
  - GET /aprendiz/bateu/placar (agregado + por corretora + escopo);
  - GET /aprendiz/bateu/pares (lado a lado, filtro por veredito, paginação);
  - privacidade: corretora só vê o dela; sem header → 403.

DB isolado: SQLite in-memory (mesmo padrão de test_endpoint_painel_cadencia.py)
injetado via monkeypatch de aprendiz.SessionLocal. Zero prod, zero envio.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import BigInteger, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.api.aprendiz as aprendiz
from app.db.models import Base, Corretora, MensagemWhatsapp, ShadowDecision
from app.main import app
from app.services.comparador_sombra import (
    extrair_resposta_real,
    montar_pares,
    normalizar,
    similaridade,
    veredito_do_score,
)


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(element, compiler, **kw):  # noqa: ANN001
    return "JSON"


@compiles(BigInteger, "sqlite")
def _compile_bigint_sqlite(element, compiler, **kw):  # noqa: ANN001
    return "INTEGER"


@compiles(__import__("sqlalchemy.dialects.postgresql", fromlist=["UUID"]).UUID, "sqlite")
def _compile_uuid_sqlite(element, compiler, **kw):  # noqa: ANN001
    return "CHAR(36)"


AGORA = datetime.now(timezone.utc)


# ── Heurística pura ───────────────────────────────────────────────────────────


def test_normalizar_remove_acento_emoji_pontuacao():
    assert normalizar("Oi, João! Tudo bem? 😊") == "oi joao tudo bem"
    assert normalizar(None) == ""
    assert normalizar("  MUITOS   espaços  ") == "muitos espacos"


def test_similaridade_textos_iguais_e_disjuntos():
    a = "Oi Pessoa! Podemos marcar a visita amanhã às 15h?"
    assert similaridade(a, a) == 1.0
    baixo = similaridade("banana amarela madura", "reunião jurídica contrato")
    assert baixo is not None and baixo < 0.30


def test_similaridade_parecido_da_bateu():
    a = "Oi Pessoa, podemos marcar a visita amanhã às 15h?"
    b = "Oi Pessoa! Vamos marcar a visita amanhã às 15h então?"
    s = similaridade(a, b)
    assert s is not None and s >= 0.55  # → bateu


def test_similaridade_sem_texto_comparavel():
    assert similaridade("", "olá") is None
    assert similaridade("olá tudo bem", None) is None


def test_veredito_thresholds():
    assert veredito_do_score(0.55) == "bateu"
    assert veredito_do_score(0.54) == "parcial"
    assert veredito_do_score(0.30) == "parcial"
    assert veredito_do_score(0.29) == "nao_bateu"
    assert veredito_do_score(None) == "sem_comparacao"


def test_extrair_resposta_real_rajada_e_janela():
    apos = AGORA - timedelta(hours=2)
    msgs = [
        # fora (antes da âncora)
        {"timestamp_evolution": apos - timedelta(minutes=5), "conteudo_texto": "antiga"},
        # rajada: 2 bolhas em 10 min
        {"timestamp_evolution": apos + timedelta(minutes=3), "conteudo_texto": "Oi!"},
        {"timestamp_evolution": apos + timedelta(minutes=10), "conteudo_texto": "Vamos marcar?"},
        # fora da rajada (2h depois da 1ª bolha)
        {"timestamp_evolution": apos + timedelta(hours=2), "conteudo_texto": "outro assunto"},
    ]
    r = extrair_resposta_real(msgs, apos)
    assert r is not None
    assert r["texto"] == "Oi!\nVamos marcar?"
    assert r["bolhas"] == 2

    # nada dentro da janela → None
    assert extrair_resposta_real(
        [{"timestamp_evolution": apos + timedelta(hours=100), "conteudo_texto": "tarde demais"}],
        apos,
    ) is None


def test_extrair_resposta_real_audio_sem_transcricao():
    apos = AGORA - timedelta(hours=1)
    r = extrair_resposta_real(
        [{"timestamp_evolution": apos + timedelta(minutes=2), "conteudo_texto": "[audio]"}], apos
    )
    assert r is not None and r["texto"] is None  # respondeu, mas sem texto → sem_comparacao


def test_montar_pares_usa_transcricao_e_sufixo_de_telefone():
    apos = AGORA - timedelta(hours=3)
    decisoes = [{
        "id": 1, "corretora_id": 3, "lead_id": "kommo_9", "lead_nome": "Corretor",
        "lead_telefone": "+5500000000000",  # DDI+DDD diferentes do contato, sufixo igual
        "bucket": "responderia_sozinha", "confianca": 90,
        "resposta_sugerida": "Podemos marcar a visita amanhã às 15h?",
        "ultima_msg_lead": "pode ser amanhã?", "ultima_msg_at": apos, "created_at": apos,
    }]
    msgs = [{
        "corretora_id": 3, "contato_telefone": "+5500000000000",
        "conteudo_texto": "[audio]",
        "transcricao": "Podemos marcar a visita amanhã às 15h sim!",
        "timestamp_evolution": apos + timedelta(minutes=8),
    }]
    pares = montar_pares(decisoes, msgs)
    assert len(pares) == 1
    assert pares[0]["veredito"] == "bateu"
    assert "visita amanhã" in pares[0]["resposta_real"]


# ── Endpoints ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def client(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        engine,
        tables=[Corretora.__table__, ShadowDecision.__table__, MensagemWhatsapp.__table__],
    )
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(aprendiz, "SessionLocal", factory)
    _seed(factory)
    return TestClient(app)


def _msg(cid: int, tel: str, texto: str | None, ts: datetime, transcricao: str | None = None):
    return MensagemWhatsapp(
        id=uuid.uuid4(), evolution_message_id=uuid.uuid4().hex, instance="corretor_cloud",
        corretora_id=cid, remote_jid=f"{tel}@s.whatsapp.net", contato_telefone=tel,
        from_corretora=True, tipo_mensagem="conversation",
        conteudo_texto=texto, transcricao=transcricao, timestamp_evolution=ts,
    )


def _seed(factory) -> None:
    with factory() as s:
        s.add_all([
            Corretora(id=1, nome="Pessoa5", telefone="+000000000000000"),
            Corretora(id=3, nome="Pessoa3", telefone="+000000000000000"),
        ])
        t = AGORA - timedelta(days=1)
        s.add_all([
            # Pessoa12 #1 — resposta real quase igual → BATEU
            ShadowDecision(
                id=101, corretora_id=3, lead_id="kommo_1", lead_nome="Corretor",
                lead_telefone="+5500000000000", bucket="responderia_sozinha", confianca=90,
                resposta_sugerida="Oi Corretor! Podemos marcar a visita amanhã às 15h?",
                ultima_msg_lead="pode ser amanhã?", ultima_msg_at=t, created_at=t,
            ),
            # Pessoa12 #2 — resposta real em outra direção → NAO_BATEU
            ShadowDecision(
                id=102, corretora_id=3, lead_id="kommo_2", lead_nome="Beto",
                lead_telefone="+5500000000000", bucket="rascunharia", confianca=60,
                resposta_sugerida="Te mando as fotos do apartamento de 2 quartos!",
                ultima_msg_lead="tem 2 quartos?", ultima_msg_at=t, created_at=t,
            ),
            # Pessoa12 #3 — corretora não respondeu → SEM_RESPOSTA_REAL
            ShadowDecision(
                id=103, corretora_id=3, lead_id="kommo_3", lead_nome="Carla",
                lead_telefone="+5500000000000", bucket="responderia_sozinha", confianca=80,
                resposta_sugerida="Oi Carla, ainda procura imóvel?",
                ultima_msg_lead="oi", ultima_msg_at=t, created_at=t,
            ),
            # Pessoa12 #4 — chamaria_voce (sem rascunho) → fora do bateu-ou-não
            ShadowDecision(
                id=104, corretora_id=3, lead_id="kommo_4", lead_nome="Davi",
                lead_telefone="+5500000000000", bucket="chamaria_voce", confianca=20,
                resposta_sugerida=None, ultima_msg_at=t, created_at=t,
            ),
            # Pessoa10 #1 — bateu (pra testar escopo por corretora)
            ShadowDecision(
                id=105, corretora_id=1, lead_id="kommo_5", lead_nome="Eva",
                lead_telefone="+5500000000000", bucket="responderia_sozinha", confianca=85,
                resposta_sugerida="Consigo sim! Te espero sábado às 10h no plantão.",
                ultima_msg_lead="sábado 10h?", ultima_msg_at=t, created_at=t,
            ),
        ])
        s.add_all([
            _msg(3, "+5500000000000", "Oi Corretor! Vamos marcar a visita amanhã às 15h então?",
                 t + timedelta(minutes=12)),
            _msg(3, "+5500000000000", "Esse já foi vendido, mas tenho um studio novinho no centro",
                 t + timedelta(minutes=30)),
            _msg(1, "+5500000000000", "Consigo sim, te espero sábado às 10h no plantão!",
                 t + timedelta(minutes=5)),
        ])
        s.commit()


def test_bateu_placar_sem_header_403(client):
    assert client.get("/aprendiz/bateu/placar").status_code == 403


def test_bateu_placar_admin_todas(client):
    r = client.get("/aprendiz/bateu/placar", headers={"X-Painel-User": "dona"})
    assert r.status_code == 200
    d = r.json()
    assert d["escopo"] == "todas"
    assert d["total_rascunhos"] == 4          # chamaria_voce fica de fora
    assert d["vereditos"]["bateu"] == 2       # Pessoa12 #1 + Pessoa10 #1
    assert d["vereditos"]["nao_bateu"] == 1
    assert d["vereditos"]["sem_resposta_real"] == 1
    assert d["comparaveis"] == 3
    assert d["bateu_pct"] == 67
    nomes = {c["corretora_id"]: c["corretora_nome"] for c in d["por_corretora"]}
    assert nomes == {1: "Pessoa5", 3: "Pessoa3"}
    corretor = next(c for c in d["por_corretora"] if c["corretora_id"] == 3)
    assert corretor["vereditos"]["bateu"] == 1 and corretor["comparaveis"] == 2
    assert corretor["bateu_pct"] == 50


def test_bateu_placar_corretora_ve_so_o_dela(client):
    # Pessoa12 pedindo corretora_id=1 é ignorado — sempre o dela (3)
    r = client.get("/aprendiz/bateu/placar?corretora_id=1", headers={"X-Painel-User": "corretor"})
    assert r.status_code == 200
    d = r.json()
    assert d["escopo"] == "corretora" and d["corretora_id"] == 3
    assert d["total_rascunhos"] == 3
    assert d["vereditos"]["bateu"] == 1


def test_bateu_pares_lado_a_lado_e_filtro(client):
    r = client.get("/aprendiz/bateu/pares", headers={"X-Painel-User": "dona"})
    assert r.status_code == 200
    d = r.json()
    assert d["total"] == 4
    por_id = {it["decisao_id"]: it for it in d["itens"]}
    p1 = por_id[101]
    assert p1["veredito"] == "bateu"
    assert p1["similaridade_pct"] is not None and p1["similaridade_pct"] >= 55
    assert "visita amanhã" in p1["resposta_real"]
    assert p1["rascunho"].startswith("Oi Corretor!")
    assert p1["corretora_nome"] == "Pessoa3"
    assert por_id[103]["resposta_real"] is None

    # filtro por veredito + paginação
    r2 = client.get("/aprendiz/bateu/pares?veredito=bateu&limit=1&offset=0",
                    headers={"X-Painel-User": "dona"})
    d2 = r2.json()
    assert d2["total"] == 2 and len(d2["itens"]) == 1

    # veredito inválido → 400
    assert client.get("/aprendiz/bateu/pares?veredito=xyz",
                      headers={"X-Painel-User": "dona"}).status_code == 400
