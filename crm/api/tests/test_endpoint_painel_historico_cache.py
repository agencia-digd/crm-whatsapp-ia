"""GET /painel/historico — cache persistente (historico_cache, migration 0059).

Cobre:
  - 403 sem header / corretora (admin-only);
  - MISS: primeira chamada calcula todos os meses e persiste;
  - HIT: segunda chamada serve 100% do cache (zero recomputo) com payload igual;
  - force=1: apaga o cache do ano e recalcula tudo;
  - TTL do mês corrente: cache velho (> 15 min) recalcula SÓ o mês corrente,
    meses fechados calculados após o fechamento seguem imutáveis;
  - _historico_cache_valido: regras de validade puras.

DB isolado: SQLite in-memory (padrão de test_endpoint_painel_automacoes.py)
injetado via monkeypatch de painel.SessionLocal. O cálculo caro
(_historico_mes_payload → Kommo/Postgres/SQLite trafego) é substituído por
stub determinístico com contador. Zero prod, zero envio.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import BigInteger, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.api.painel as painel
from app.api.painel import _historico_cache_valido
from app.db.models import Base, HistoricoCache
from app.main import app


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(element, compiler, **kw):  # noqa: ANN001
    return "JSON"


@compiles(BigInteger, "sqlite")
def _compile_bigint_sqlite(element, compiler, **kw):  # noqa: ANN001
    return "INTEGER"


AGORA = datetime.now(timezone.utc)
HOJE = date.today()
ANO_PASSADO = HOJE.year - 1


def _payload_fake(ano: int, m: int) -> dict[str, Any]:
    """Shape mínimo compatível com o bloco de totais do endpoint."""
    return {
        "ano_mes": f"{ano:04d}-{m:02d}",
        "label": f"{m:02d}/{ano % 100:02d}",
        "meta_ads": {"spend": float(m), "leads_meta": m, "cpl": 1.0},
        "equipe": {
            "leads": m, "atendidos": m, "atendidos_evolution": 0,
            "agendados": m, "visitas": m, "vendas": 1, "vgv": 100.0 * m,
            "ticket_medio": None, "roi": None, "conv_atendeu": None,
            "conv_agendou": None, "conv_visitou": None, "conv_vendeu": None,
        },
        "por_corretora": [],
    }


@pytest.fixture()
def ambiente(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=[HistoricoCache.__table__])
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(painel, "SessionLocal", factory)

    chamadas: list[tuple[int, int]] = []

    def _stub(ano: int, m: int) -> dict[str, Any]:
        chamadas.append((ano, m))
        return _payload_fake(ano, m)

    monkeypatch.setattr(painel, "_historico_mes_payload", _stub)
    return TestClient(app), factory, chamadas


def _get(client: TestClient, **params: Any):
    return client.get(
        "/painel/historico", params=params, headers={"X-Painel-User": "dona"}
    )


# ── Auth ──────────────────────────────────────────────────────────────────────

def test_sem_header_403(ambiente):
    client, _, _ = ambiente
    assert client.get("/painel/historico").status_code == 403


def test_corretora_403(ambiente):
    client, _, _ = ambiente
    r = client.get("/painel/historico", headers={"X-Painel-User": "corretor"})
    assert r.status_code == 403
    assert r.json()["detail"] == "painel_admin_required"


# ── Miss / hit / force ────────────────────────────────────────────────────────

def test_miss_calcula_e_persiste(ambiente):
    client, factory, chamadas = ambiente
    r = _get(client, ano=ANO_PASSADO)
    assert r.status_code == 200
    body = r.json()
    assert len(body["meses"]) == 12
    assert chamadas == [(ANO_PASSADO, m) for m in range(1, 13)]
    with factory() as s:
        assert s.query(HistoricoCache).count() == 12
    # totais coerentes com os payloads fake (matemática intocada)
    assert body["totais"]["leads"] == sum(range(1, 13))
    assert body["totais"]["vgv"] == round(sum(100.0 * m for m in range(1, 13)), 2)


def test_hit_nao_recalcula_e_payload_igual(ambiente):
    client, _, chamadas = ambiente
    corpo1 = _get(client, ano=ANO_PASSADO).json()
    chamadas.clear()
    corpo2 = _get(client, ano=ANO_PASSADO).json()
    assert chamadas == []  # 100% cache — ano fechado, computed_at pós-fechamento
    assert corpo1 == corpo2


def test_force_invalida_e_recalcula_tudo(ambiente):
    client, _, chamadas = ambiente
    _get(client, ano=ANO_PASSADO)
    chamadas.clear()
    r = _get(client, ano=ANO_PASSADO, force=1)
    assert r.status_code == 200
    assert chamadas == [(ANO_PASSADO, m) for m in range(1, 13)]


# ── TTL do mês corrente ───────────────────────────────────────────────────────

def test_ttl_mes_corrente_expirado_recalcula_so_ele(ambiente):
    client, factory, chamadas = ambiente
    ano, mes = HOJE.year, HOJE.month
    velho = AGORA - timedelta(seconds=painel.HISTORICO_TTL_MES_ABERTO_SEC + 60)
    with factory() as s:
        for m in range(1, mes + 1):
            s.add(HistoricoCache(
                ano_mes=f"{ano:04d}-{m:02d}",
                payload=_payload_fake(ano, m),
                # meses fechados: computed_at agora (pós-fechamento) → imutável;
                # mês corrente: computed_at velho → TTL estourado
                computed_at=velho if m == mes else AGORA,
            ))
        s.commit()
    r = _get(client, ano=ano)
    assert r.status_code == 200
    assert chamadas == [(ano, mes)]  # só o mês corrente recalculado


def test_ttl_mes_corrente_fresco_serve_do_cache(ambiente):
    client, factory, chamadas = ambiente
    ano, mes = HOJE.year, HOJE.month
    with factory() as s:
        for m in range(1, mes + 1):
            s.add(HistoricoCache(
                ano_mes=f"{ano:04d}-{m:02d}",
                payload=_payload_fake(ano, m),
                computed_at=AGORA - timedelta(minutes=1),
            ))
        s.commit()
    r = _get(client, ano=ano)
    assert r.status_code == 200
    assert chamadas == []


# ── _historico_cache_valido (unidade) ─────────────────────────────────────────

def test_valido_mes_fechado_pos_fechamento_imortal():
    # calculado 2 anos depois do mês fechar → vale mesmo "velho"
    computed = datetime(2025, 3, 1, tzinfo=timezone.utc)
    now = computed + timedelta(days=700)
    assert _historico_cache_valido("2025-01", computed, now) is True


def test_valido_calculado_dentro_do_mes_respeita_ttl():
    computed = datetime(2025, 1, 15, 12, 0, tzinfo=timezone.utc)
    dentro_ttl = computed + timedelta(minutes=5)
    fora_ttl = computed + timedelta(minutes=20)
    assert _historico_cache_valido("2025-01", computed, dentro_ttl) is True
    # payload de mês calculado ainda DENTRO do mês expira via TTL — mesmo que
    # `now` já seja depois do fechamento (payload não cobre o mês inteiro)
    assert _historico_cache_valido("2025-01", computed, fora_ttl) is False


def test_valido_dezembro_vira_ano():
    computed = datetime(2026, 1, 2, tzinfo=timezone.utc)
    now = computed + timedelta(days=90)
    assert _historico_cache_valido("2025-12", computed, now) is True
