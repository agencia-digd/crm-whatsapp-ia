"""Consolidação diária de métricas — Onda P2."""
from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.config import settings
from app.db.session import SessionLocal
from app.main import app
from app.services.consolidacao_metricas import (
    MetricasCorretora,
    RETRY_BACKOFFS,
    consolidar_corretora,
    consolidar_todas,
    gravar_snapshot,
)


CORRETORA_A_ID = 3
CORRETORA_A_KOMMO_USER = 10000003


@pytest.fixture(autouse=True)
def _no_sleep():
    with patch("app.services.consolidacao_metricas.time.sleep"):
        yield


@pytest.fixture(autouse=True)
def _clean_dm():
    """Limpa daily_metrics_corretora pra datas de teste."""
    with SessionLocal() as s:
        s.execute(text(
            "DELETE FROM daily_metrics_corretora WHERE data IN ('1999-01-01','1999-01-02')"
        ))
        s.commit()
    yield
    with SessionLocal() as s:
        s.execute(text(
            "DELETE FROM daily_metrics_corretora WHERE data IN ('1999-01-01','1999-01-02')"
        ))
        s.commit()


# ── Coleta por corretora ─────────────────────────────────────────────────────


def test_consolidar_corretora_kommo_ok():
    fake_tasks = [{"id": i} for i in range(38)]
    fake_leads = iter([{"id": i} for i in range(120)])

    with patch("app.services.consolidacao_metricas._coletar_tarefas_atrasadas", return_value=38), \
         patch("app.services.consolidacao_metricas._coletar_total_leads_mes", return_value=120), \
         patch("app.services.consolidacao_metricas._coletar_atendidos_mes", return_value=42):
        m = consolidar_corretora(CORRETORA_A_ID)

    assert m.tarefas_atrasadas == 38
    assert m.total_leads_mes == 120
    assert m.atendidos_mes == 42
    assert "kommo_tasks" in m.fontes_completas
    assert "kommo_leads" in m.fontes_completas
    assert "lead_analyses" in m.fontes_completas
    assert m.fontes_falhas == []


def test_consolidar_corretora_kommo_falha():
    """Kommo falha 3x → campo None + fontes_falhas preenchido. Outras fontes seguem."""
    def _falha(*a, **k):
        raise RuntimeError("kommo down")

    with patch("app.services.consolidacao_metricas._coletar_tarefas_atrasadas", side_effect=_falha), \
         patch("app.services.consolidacao_metricas._coletar_total_leads_mes", side_effect=_falha), \
         patch("app.services.consolidacao_metricas._coletar_atendidos_mes", return_value=10):
        m = consolidar_corretora(CORRETORA_A_ID)

    assert m.tarefas_atrasadas is None
    assert m.total_leads_mes is None
    assert m.atendidos_mes == 10
    assert "kommo_tasks" in m.fontes_falhas
    assert "kommo_leads" in m.fontes_falhas
    assert "lead_analyses" in m.fontes_completas


# ── Privacidade — REGRA DE OURO ──────────────────────────────────────────────


def test_consolidar_corretora_filtra_por_kommo_user_id():
    """Pessoa3 → Kommo deve receber kommo_user_id da Pessoa3 (10000003)."""
    captured: dict[str, int] = {}

    def fake_tasks_collector(uid: int) -> int:
        captured["tasks_uid"] = uid
        return 5

    def fake_leads_collector(uid: int) -> int:
        captured["leads_uid"] = uid
        return 50

    with patch("app.services.consolidacao_metricas._coletar_tarefas_atrasadas",
               side_effect=fake_tasks_collector), \
         patch("app.services.consolidacao_metricas._coletar_total_leads_mes",
               side_effect=fake_leads_collector), \
         patch("app.services.consolidacao_metricas._coletar_atendidos_mes", return_value=0):
        consolidar_corretora(CORRETORA_A_ID)

    assert captured["tasks_uid"] == CORRETORA_A_KOMMO_USER
    assert captured["leads_uid"] == CORRETORA_A_KOMMO_USER


# ── UPSERT idempotência ──────────────────────────────────────────────────────


def test_gravar_snapshot_upsert_atualiza():
    d = date(1999, 1, 1)
    metricas_a = {CORRETORA_A_ID: MetricasCorretora(
        corretora_id=CORRETORA_A_ID, total_leads_mes=10, atendidos_mes=5,
        tarefas_atrasadas=10, fontes_completas=["kommo_tasks"],
    )}
    gravar_snapshot(metricas_a, data=d)

    metricas_b = {CORRETORA_A_ID: MetricasCorretora(
        corretora_id=CORRETORA_A_ID, total_leads_mes=12, atendidos_mes=6,
        tarefas_atrasadas=15, fontes_completas=["kommo_tasks"],
    )}
    gravar_snapshot(metricas_b, data=d)

    with SessionLocal() as s:
        rows = s.execute(text(
            "SELECT tarefas_atrasadas, total_leads_mes FROM daily_metrics_corretora "
            "WHERE corretora_id = :c AND data = :d"
        ), {"c": CORRETORA_A_ID, "d": d}).all()
    assert len(rows) == 1, "UPSERT deveria atualizar, não duplicar"
    assert rows[0].tarefas_atrasadas == 15
    assert rows[0].total_leads_mes == 12


def test_gravar_snapshot_dia_diferente_empilha():
    d1 = date(1999, 1, 1)
    d2 = date(1999, 1, 2)
    m = {CORRETORA_A_ID: MetricasCorretora(corretora_id=CORRETORA_A_ID, tarefas_atrasadas=5)}
    gravar_snapshot(m, data=d1)
    gravar_snapshot(m, data=d2)

    with SessionLocal() as s:
        n = s.execute(text(
            "SELECT COUNT(*) FROM daily_metrics_corretora "
            "WHERE corretora_id = :c AND data IN (:d1, :d2)"
        ), {"c": CORRETORA_A_ID, "d1": d1, "d2": d2}).scalar()
    assert n == 2


# ── consolidar_todas resiliente ──────────────────────────────────────────────


def test_consolidar_todas_falha_uma_nao_para_outras():
    """Corretora 2 explode em consolidar_corretora; 1 e 3 seguem."""
    real = consolidar_corretora

    def fake(cid: int) -> MetricasCorretora:
        if cid == 2:
            raise RuntimeError("kommo unavailable for corretor")
        return MetricasCorretora(
            corretora_id=cid, tarefas_atrasadas=cid * 10,
            fontes_completas=["kommo_tasks"],
        )

    with patch("app.services.consolidacao_metricas.consolidar_corretora",
               side_effect=fake):
        out = consolidar_todas()

    # ">=" e não "==": o banco de teste é compartilhado pela suíte, e outros
    # arquivos deixam corretoras ativas além das 3 do seed (Corretor=4, Corretor, Pessoa7 —
    # como em produção). O que este teste guarda é que a 2 explodir não tira a
    # 1 nem a 3 do resultado, não quantas corretoras existem.
    assert set(out.keys()) >= {1, 2, 3}
    assert out[1].tarefas_atrasadas == 10
    assert out[3].tarefas_atrasadas == 30
    # Falha encapsulada
    assert "consolidar_corretora_exception" in out[2].fontes_falhas
    assert out[2].tarefas_atrasadas is None


# ── Endpoint admin ───────────────────────────────────────────────────────────


@pytest.fixture
def client():
    return TestClient(app)


def _hdrs():
    return {"X-Internal-Token": settings.copiloto_internal_token}


def test_endpoint_admin_consolidar_sem_token_401(client):
    r = client.post("/admin/consolidar", json={})
    assert r.status_code == 401


def test_endpoint_admin_consolidar_com_token_ok(client):
    fake = {
        i: MetricasCorretora(corretora_id=i, tarefas_atrasadas=i,
                             total_leads_mes=i * 10, atendidos_mes=i * 2,
                             fontes_completas=["kommo_tasks", "kommo_leads", "lead_analyses"])
        for i in (1, 2, 3)
    }
    with patch("app.api.admin.consolidar_todas", return_value=fake), \
         patch("app.api.admin.gravar_snapshot", return_value=3):
        r = client.post("/admin/consolidar", headers=_hdrs(), json={})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["corretoras_processadas"] == 3
    assert "kommo_tasks" in body["fontes_completas_global"]
    assert body["fontes_falhas_global"] == []
    assert len(body["detalhes_por_corretora"]) == 3
