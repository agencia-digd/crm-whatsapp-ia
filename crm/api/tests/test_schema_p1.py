"""Schema P1 — tabelas/views novas + constraints."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.db.session import SessionLocal


EXPECTED_TABLES = {
    "daily_metrics_corretora",
    "vendas",
    "ligacoes_semanais",
    "agendamentos_semanais",
    "visitas",
    "notas",
}
EXPECTED_VIEWS = {
    "vw_metricas_mes_corretora",
    "vw_semana_atual_corretora",
}


def test_tabelas_existem():
    with SessionLocal() as s:
        rows = s.execute(text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='public' AND table_type='BASE TABLE'"
        )).scalars().all()
    have = set(rows)
    missing = EXPECTED_TABLES - have
    assert not missing, f"faltando tabelas: {missing}"


def test_views_existem():
    with SessionLocal() as s:
        rows = s.execute(text(
            "SELECT table_name FROM information_schema.views "
            "WHERE table_schema='public'"
        )).scalars().all()
    have = set(rows)
    missing = EXPECTED_VIEWS - have
    assert not missing, f"faltando views: {missing}"


# ── Constraints ──────────────────────────────────────────────────────────────


def _cleanup_p1(s):
    s.execute(text("DELETE FROM ligacoes_semanais WHERE ano_semana_iso='9999-W99'"))
    s.execute(text("DELETE FROM agendamentos_semanais WHERE ano_semana_iso='9999-W99'"))
    s.commit()


def test_constraints_ligacoes_atendeu_le_ligacoes():
    with SessionLocal() as s:
        _cleanup_p1(s)
        with pytest.raises(IntegrityError):
            s.execute(text(
                "INSERT INTO ligacoes_semanais "
                "(corretora_id, ano_semana_iso, ligacoes, atendeu, prosseguiu) "
                "VALUES (3, '9999-W99', 5, 10, 0)"
            ))
            s.commit()
        s.rollback()


def test_constraints_ligacoes_prosseguiu_le_atendeu():
    with SessionLocal() as s:
        _cleanup_p1(s)
        with pytest.raises(IntegrityError):
            s.execute(text(
                "INSERT INTO ligacoes_semanais "
                "(corretora_id, ano_semana_iso, ligacoes, atendeu, prosseguiu) "
                "VALUES (3, '9999-W99', 50, 10, 20)"
            ))
            s.commit()
        s.rollback()


def test_constraints_agendamentos_realizados_le_agendados():
    with SessionLocal() as s:
        _cleanup_p1(s)
        with pytest.raises(IntegrityError):
            s.execute(text(
                "INSERT INTO agendamentos_semanais "
                "(corretora_id, ano_semana_iso, agendados, realizados) "
                "VALUES (3, '9999-W99', 5, 10)"
            ))
            s.commit()
        s.rollback()


def test_unique_ligacoes_corretora_semana():
    with SessionLocal() as s:
        _cleanup_p1(s)
        s.execute(text(
            "INSERT INTO ligacoes_semanais "
            "(corretora_id, ano_semana_iso, ligacoes, atendeu, prosseguiu) "
            "VALUES (3, '9999-W99', 10, 5, 1)"
        ))
        s.commit()
        with pytest.raises(IntegrityError):
            s.execute(text(
                "INSERT INTO ligacoes_semanais "
                "(corretora_id, ano_semana_iso, ligacoes, atendeu, prosseguiu) "
                "VALUES (3, '9999-W99', 20, 8, 2)"
            ))
            s.commit()
        s.rollback()
        _cleanup_p1(s)


def test_unique_agendamentos_corretora_semana():
    with SessionLocal() as s:
        _cleanup_p1(s)
        s.execute(text(
            "INSERT INTO agendamentos_semanais "
            "(corretora_id, ano_semana_iso, agendados, realizados) "
            "VALUES (3, '9999-W99', 5, 2)"
        ))
        s.commit()
        with pytest.raises(IntegrityError):
            s.execute(text(
                "INSERT INTO agendamentos_semanais "
                "(corretora_id, ano_semana_iso, agendados, realizados) "
                "VALUES (3, '9999-W99', 8, 3)"
            ))
            s.commit()
        s.rollback()
        _cleanup_p1(s)


# ── Views: COALESCE retorna 0, não NULL ──────────────────────────────────────


def test_vw_metricas_mes_vazia_retorna_zero():
    """Sem vendas no mês corrente, view retorna 0/0 (não NULL)."""
    with SessionLocal() as s:
        s.execute(text("DELETE FROM vendas WHERE cliente_nome LIKE '%FAKE_VW_TEST%'"))
        s.commit()
        rows = s.execute(text(
            "SELECT vendas_mes, vgv_mes FROM vw_metricas_mes_corretora "
            "WHERE corretora_id = :cid AND mes = to_char(CURRENT_DATE, 'YYYY-MM')"
        ), {"cid": 3}).first()
    assert rows is not None
    assert rows.vendas_mes == 0
    assert float(rows.vgv_mes) == 0.0


def test_vw_semana_corretora_vazia_retorna_zero():
    """Sem registros semanais, view retorna ligacoes=0/agendados=0 (não NULL)."""
    sem = _semana_iso()
    with SessionLocal() as s:
        s.execute(text(
            "DELETE FROM ligacoes_semanais WHERE corretora_id=:c AND ano_semana_iso=:w"
        ), {"c": 1, "w": sem})
        s.execute(text(
            "DELETE FROM agendamentos_semanais WHERE corretora_id=:c AND ano_semana_iso=:w"
        ), {"c": 1, "w": sem})
        s.commit()
        row = s.execute(text(
            "SELECT ligacoes, ligacoes_atendeu, agendados, realizados "
            "FROM vw_semana_atual_corretora WHERE corretora_id = :c"
        ), {"c": 1}).first()
    assert row is not None
    assert row.ligacoes == 0
    assert row.ligacoes_atendeu == 0
    assert row.agendados == 0
    assert row.realizados == 0


def _semana_iso() -> str:
    """ISO week current, no formato 'IYYY-Www'. Match Postgres."""
    from sqlalchemy import text as _t
    with SessionLocal() as s:
        return s.execute(_t("SELECT to_char(CURRENT_DATE, 'IYYY-\"W\"IW')")).scalar()
