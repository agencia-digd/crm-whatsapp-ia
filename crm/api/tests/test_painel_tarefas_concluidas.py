"""GET /painel/tarefas/concluidas — histórico paginado da tela ☑️ Tarefas V2.

Também cobre o rastro `concluido_por` gravado pelo POST /tarefa/{id}/concluir
(migration 0062).

DB isolado: SQLite in-memory injetado via monkeypatch de painel.SessionLocal
(mesmo padrão de test_endpoint_painel_automacoes.py). Zero prod, zero envio.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import BigInteger, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.api.painel as painel
from app.db.models import Base, TarefaCorretora
from app.main import app


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(element, compiler, **kw):  # noqa: ANN001
    return "JSON"


@compiles(BigInteger, "sqlite")
def _compile_bigint_sqlite(element, compiler, **kw):  # noqa: ANN001
    return "INTEGER"


AGORA = datetime.now(timezone.utc).replace(tzinfo=None)


def _tarefa(i: int, cid: int, status: str = "concluida",
            concluido_em: datetime | None = None,
            concluido_por: str | None = None) -> TarefaCorretora:
    return TarefaCorretora(
        id=i, tenant_id=1, lead_id=f"kommo_{i}", lead_nome=f"Lead {i}",
        corretora_id=cid, data_tarefa=date(2026, 7, 20), texto=f"tarefa {i}",
        status=status, autor="corretor", criado_em=AGORA - timedelta(days=5),
        concluido_em=concluido_em, concluido_por=concluido_por,
    )


@pytest.fixture()
def client(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=[TarefaCorretora.__table__])
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(painel, "SessionLocal", factory)
    with factory() as s:
        s.add_all([
            # 3 concluídas da Pessoa12 (cid=3), em momentos diferentes
            _tarefa(1, 3, concluido_em=AGORA - timedelta(days=3), concluido_por="corretor"),
            _tarefa(2, 3, concluido_em=AGORA - timedelta(days=1), concluido_por="corretor"),
            _tarefa(3, 3, concluido_em=AGORA - timedelta(hours=2), concluido_por="dona"),
            # 1 concluída da Pessoa10 (cid=1) — antiga, sem concluido_por (pré-0059)
            _tarefa(4, 1, concluido_em=AGORA - timedelta(days=10), concluido_por=None),
            # 1 ABERTA da Pessoa12 — não pode aparecer no histórico
            _tarefa(5, 3, status="aberta"),
        ])
        s.commit()
    return TestClient(app)


def _hdr(user):
    return {"X-Painel-User": user}


# ── escopo ───────────────────────────────────────────────────────────────────

def test_403_sem_header(client):
    r = client.get("/painel/tarefas/concluidas")
    assert r.status_code == 403


def test_corretora_ve_so_as_dela(client):
    r = client.get("/painel/tarefas/concluidas", headers=_hdr("corretor"))
    assert r.status_code == 200
    d = r.json()
    assert d["total"] == 3
    assert all(t["corretora"] == "Pessoa12" for t in d["tarefas"])
    # aberta (id=5) não aparece
    assert 5 not in [t["id"] for t in d["tarefas"]]


def test_admin_ve_todas(client):
    r = client.get("/painel/tarefas/concluidas", headers=_hdr("dona"))
    assert r.status_code == 200
    d = r.json()
    assert d["total"] == 4
    assert {t["corretora"] for t in d["tarefas"]} == {"Pessoa12", "Pessoa10"}


# ── ordenação + campos ───────────────────────────────────────────────────────

def test_mais_recente_primeiro_com_quem_e_quando(client):
    r = client.get("/painel/tarefas/concluidas", headers=_hdr("dona"))
    ids = [t["id"] for t in r.json()["tarefas"]]
    assert ids == [3, 2, 1, 4]  # concluido_em DESC
    t3 = r.json()["tarefas"][0]
    assert t3["concluido_por"] == "dona"
    assert t3["concluido_em"] is not None
    # linha pré-0059: concluido_por NULL, endpoint não quebra
    t4 = r.json()["tarefas"][-1]
    assert t4["concluido_por"] is None


# ── paginação ────────────────────────────────────────────────────────────────

def test_paginacao(client):
    r1 = client.get("/painel/tarefas/concluidas?page=1&por_pagina=2", headers=_hdr("dona"))
    d1 = r1.json()
    assert [t["id"] for t in d1["tarefas"]] == [3, 2]
    assert d1["tem_mais"] is True
    r2 = client.get("/painel/tarefas/concluidas?page=2&por_pagina=2", headers=_hdr("dona"))
    d2 = r2.json()
    assert [t["id"] for t in d2["tarefas"]] == [1, 4]
    assert d2["tem_mais"] is False
    r3 = client.get("/painel/tarefas/concluidas?page=3&por_pagina=2", headers=_hdr("dona"))
    assert r3.json()["tarefas"] == []


# ── concluir grava concluido_por ─────────────────────────────────────────────

def test_concluir_grava_concluido_por(client):
    r = client.post("/painel/tarefa/5/concluir", headers=_hdr("corretor"))
    assert r.status_code == 200 and r.json()["ok"] is True
    r2 = client.get("/painel/tarefas/concluidas", headers=_hdr("corretor"))
    t5 = next(t for t in r2.json()["tarefas"] if t["id"] == 5)
    assert t5["concluido_por"] == "corretor"
    assert t5["concluido_em"] is not None
