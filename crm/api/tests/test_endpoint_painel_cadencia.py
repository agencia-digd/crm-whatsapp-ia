"""GET /painel/cadencia — visão Cadência Pessoa12 no painel.

Cobre:
  - 403 sem header / usuário desconhecido / corretora (admin-only);
  - 400 status inválido;
  - resumo (ativas / concluidas_remarketing / opt_out / enviadas_hoje / tarefas hoje);
  - lista com toque atual, próximo toque (tipo/dia), telefone COMPLETO e últimos eventos;
  - paginação e filtro por status.

DB isolado: SQLite in-memory (mesmo padrão de test_cadencia.py) injetado via
monkeypatch de painel.SessionLocal. Zero prod, zero envio.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import BigInteger, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import uuid

import app.api.painel as painel
from app.db.models import Base, CadenciaEstado, CadenciaEvento, MensagemWhatsapp
from app.main import app


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

# O endpoint conta "hoje" pelo dia LOCAL de São Paulo (UTC-3), não pelo dia UTC.
# Ancorar evento em `AGORA - 1h` fazia este teste quebrar sozinho todo dia entre
# 00h e 01h de SP: uma hora antes da meia-noite ainda é ONTEM lá, então caía
# fora da janela e `enviadas_hoje` vinha 0. Achado em 29/07/2026, 00h09 de SP.
#
# HOJE_SP fixa o MEIO-DIA de São Paulo do dia corrente: está dentro da janela a
# qualquer hora que a suíte rode, e continua sendo "hoje" de verdade.
_SP_TZ = timezone(timedelta(hours=-3))
HOJE_SP = (
    datetime.now(_SP_TZ)
    .replace(hour=12, minute=0, second=0, microsecond=0)
    .astimezone(timezone.utc)
)


@pytest.fixture()
def client(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        engine,
        tables=[CadenciaEstado.__table__, CadenciaEvento.__table__,
                MensagemWhatsapp.__table__],
    )
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(painel, "SessionLocal", factory)
    _seed(factory)
    return TestClient(app)


def _seed(factory) -> None:
    with factory() as s:
        # 2 ativas + 1 remarketing (concluiu os 8) + 1 opt-out
        c1 = CadenciaEstado(tenant_id=1, lead_id="kommo_1", telefone="+5500000000000",
                            corretora_id=3, toque_atual=1, status="ativa",
                            proximo_toque_em=AGORA + timedelta(hours=2),
                            criado_em=AGORA - timedelta(days=1), atualizado_em=AGORA)
        c2 = CadenciaEstado(tenant_id=1, lead_id="kommo_2", telefone="+5500000000000",
                            corretora_id=3, toque_atual=2, status="ativa",
                            proximo_toque_em=AGORA + timedelta(days=1),
                            criado_em=AGORA - timedelta(days=2), atualizado_em=AGORA)
        c3 = CadenciaEstado(tenant_id=1, lead_id="kommo_3", telefone="+5500000000000",
                            corretora_id=3, toque_atual=8, status="remarketing",
                            proximo_toque_em=AGORA + timedelta(days=30),
                            criado_em=AGORA - timedelta(days=15), atualizado_em=AGORA)
        c4 = CadenciaEstado(tenant_id=1, lead_id="kommo_4", telefone="+5500000000000",
                            corretora_id=3, toque_atual=3, status="opt_out",
                            proximo_toque_em=None,
                            criado_em=AGORA - timedelta(days=5), atualizado_em=AGORA)
        s.add_all([c1, c2, c3, c4])
        s.flush()
        s.add_all([
            # c1: mensagem D0 hoje (enfileirada em DRY-RUN)
            CadenciaEvento(tenant_id=1, cadencia_id=c1.id, toque=0, tipo="mensagem",
                           canal="whatsapp",
                           payload={"texto": "Aqui é a Acme Imóveis 🏠 ...?",
                                    "com_botao": False, "dry_run": True, "sent": False},
                           at=HOJE_SP),
            # c2: mensagem ontem + tarefa de ligação hoje
            CadenciaEvento(tenant_id=1, cadencia_id=c2.id, toque=1, tipo="mensagem",
                           canal="whatsapp",
                           payload={"texto": "Bom te falar de novo! ...?",
                                    "com_botao": True, "dry_run": True, "sent": False},
                           at=AGORA - timedelta(days=1)),
            CadenciaEvento(tenant_id=1, cadencia_id=c2.id, toque=2, tipo="tarefa_ligacao",
                           canal="ligacao", payload={"acao": "ligar"},
                           at=HOJE_SP),
            # c3: remarketing (toque=None)
            CadenciaEvento(tenant_id=1, cadencia_id=c3.id, toque=None, tipo="mensagem",
                           canal="whatsapp",
                           payload={"texto": "Oi! Passando pra saber ...?",
                                    "remarketing": True, "dry_run": True, "sent": False},
                           at=HOJE_SP),
        ])
        # mensagens_whatsapp → respostas_hoje: 1 inbound HOJE de lead matriculado
        # (c1), 1 inbound de telefone fora da cadência (não conta) e 1 outbound
        # da corretora (não conta).
        def _msg(tel: str, inbound: bool, quando) -> MensagemWhatsapp:
            return MensagemWhatsapp(
                id=uuid.uuid4(), tenant_id=1,
                evolution_message_id=f"ev-{uuid.uuid4().hex[:8]}", instance="assistente",
                remote_jid=f"{tel.lstrip('+')}@s.whatsapp.net",
                contato_telefone=tel, from_corretora=not inbound,
                tipo_mensagem="conversation", conteudo_texto="oi",
                timestamp_evolution=quando,
            )
        s.add_all([
            _msg("+5500000000000", inbound=True,  quando=HOJE_SP),
            _msg("+000000000000000", inbound=True,  quando=HOJE_SP),
            _msg("+5500000000000", inbound=False, quando=HOJE_SP),
        ])
        s.commit()


def _hdr(user: str) -> dict[str, str]:
    return {"X-Painel-User": user}


# ── auth ─────────────────────────────────────────────────────────────────────

def test_sem_header_403(client):
    assert client.get("/painel/cadencia").status_code == 403


def test_user_desconhecido_403(client):
    assert client.get("/painel/cadencia", headers=_hdr("intruso")).status_code == 403


def test_corretora_403_admin_only(client):
    assert client.get("/painel/cadencia", headers=_hdr("corretor")).status_code == 403


def test_status_invalido_400(client):
    r = client.get("/painel/cadencia?status=zumbi", headers=_hdr("dona"))
    assert r.status_code == 400


# ── resumo ───────────────────────────────────────────────────────────────────

def test_resumo(client):
    r = client.get("/painel/cadencia", headers=_hdr("dona"))
    assert r.status_code == 200
    resumo = r.json()["resumo"]
    assert resumo["total"] == 4
    assert resumo["ativas"] == 2
    assert resumo["concluidas_remarketing"] == 1
    assert resumo["opt_out"] == 1
    # hoje: msg da c1 + remarketing da c3 = 2 mensagens; 1 tarefa de ligação
    assert resumo["enviadas_hoje"] == 2
    assert resumo["tarefas_ligacao_hoje"] == 1
    # inbound hoje: só o +...0001 está matriculado (outbound e nº de fora não contam)
    assert resumo["respostas_hoje"] == 1
    assert resumo["concluidas"] == 0  # state machine atual não usa 'concluida' ainda
    assert isinstance(resumo["dry_run"], bool)
    assert isinstance(resumo["cadencia_ativa"], bool)


# ── lista ────────────────────────────────────────────────────────────────────

def test_lista_item_completo(client):
    r = client.get("/painel/cadencia", headers=_hdr("dona"))
    body = r.json()
    assert body["total_filtrado"] == 4
    itens = {i["lead_id"]: i for i in body["itens"]}
    c1 = itens["kommo_1"]
    # telefone COMPLETO (painel interno) — sem máscara
    assert c1["telefone"] == "+5500000000000"
    assert c1["corretora_label"] == "Pessoa12"
    assert c1["toque_atual"] == 1 and c1["total_toques"] == len(__import__("app.services.cadencia", fromlist=["TOQUES"]).TOQUES)
    # próximo toque da ativa em toque_atual=1 → D1 tarefa-ligação
    assert c1["proximo_toque"] == {
        "indice": 1, "dia": "D1", "tipo": "tarefa_ligacao",
        "canal": "ligacao", "categoria": "utility",
    }
    assert c1["proximo_toque_em"] is not None
    assert len(c1["ultimos_eventos"]) == 1
    ev = c1["ultimos_eventos"][0]
    assert ev["tipo"] == "mensagem" and ev["dry_run"] is True and ev["sent"] is False
    assert ev["texto"].startswith("Aqui é a Acme")

    # remarketing: próximo toque vira D+30
    c3 = itens["kommo_3"]
    assert c3["proximo_toque"]["dia"] == "D+30"
    assert c3["ultimos_eventos"][0]["remarketing"] is True

    # opt-out: sem próximo toque
    c4 = itens["kommo_4"]
    assert c4["status"] == "opt_out" and c4["proximo_toque"] is None


def test_eventos_ordenados_desc(client):
    r = client.get("/painel/cadencia", headers=_hdr("dona"))
    itens = {i["lead_id"]: i for i in r.json()["itens"]}
    evs = itens["kommo_2"]["ultimos_eventos"]
    assert [e["tipo"] for e in evs] == ["tarefa_ligacao", "mensagem"]  # mais recente 1º


def test_filtro_status(client):
    r = client.get("/painel/cadencia?status=ativa", headers=_hdr("dona"))
    body = r.json()
    assert body["total_filtrado"] == 2
    assert all(i["status"] == "ativa" for i in body["itens"])
    # resumo continua global (não muda com o filtro)
    assert body["resumo"]["total"] == 4


def test_paginacao(client):
    r1 = client.get("/painel/cadencia?page=1&page_size=2", headers=_hdr("dona"))
    r2 = client.get("/painel/cadencia?page=2&page_size=2", headers=_hdr("dona"))
    b1, b2 = r1.json(), r2.json()
    assert len(b1["itens"]) == 2 and len(b2["itens"]) == 2
    assert b1["total_filtrado"] == 4
    ids1 = {i["cadencia_id"] for i in b1["itens"]}
    ids2 = {i["cadencia_id"] for i in b2["itens"]}
    assert ids1.isdisjoint(ids2)
    # ordena por proximo_toque_em asc, nulls last → opt-out (None) por último
    assert b2["itens"][-1]["status"] == "opt_out"
