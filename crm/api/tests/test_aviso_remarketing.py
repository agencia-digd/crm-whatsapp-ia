"""Roleta do remarketing + aviso por ESTADO (Dona 18/09/2026).

O que protege:
  1. Quem sai da Corretor é dividido entre Pessoa10, Pessoa11 e Pessoa12 em round-robin — e a
     roleta do INTAKE continua sem a Pessoa12 (lead novo do Meta não vai pra ela).
  2. Corretora inativa sai da roda; roleta vazia cai no destino único; sem
     ninguém válido, o lead fica na Corretor (fail-closed).
  3. O aviso dispara por ESTADO (tag de remarketing + coluna "Novo"), não pelo
     inbound — é o que alcança transferência manual e lote retroativo.
  4. Cap por linha, dedup por telefone e veto de quem já teve o handoff rico
     da Pessoa.
  5. Desarmado ou fora de horário = não sai nada.

Banco: Postgres de TESTE (conftest fail-closed). Nenhum teste manda WhatsApp —
o envio é sempre monkeypatchado.
"""
from __future__ import annotations

import random

import pytest
from sqlalchemy import text as sql_text

from app.db.models import Lead
from app.db.session import SessionLocal
from app.services import aviso_remarketing as ar
from app.services import config_operacional
from app.services import handoff_remarketing as hr
from app.services.atendimento_corretor import STAGE_NOVO
from app.services.carrossel_corretor import CORRETORA_CORRETOR

CORRETOR, CORRETOR, CORRETOR = 1, 2, 3


@pytest.fixture(autouse=True)
def _cenario(monkeypatch):
    """Etapas + as 4 corretoras; config nos defaults do código."""
    with SessionLocal() as s:
        s.execute(sql_text(
            "INSERT INTO pipeline_stages (id, tenant_id, nome, funil, ordem) "
            "SELECT :id, 1, 'Novo', 'principal', 1 "
            "WHERE NOT EXISTS (SELECT 1 FROM pipeline_stages WHERE id = :id)"
        ), {"id": STAGE_NOVO})
        s.execute(sql_text(
            "INSERT INTO corretoras (id, nome, telefone, telefone_normalizado, ativo) "
            "VALUES (:id, 'Corretor', '+5500000000000', '+5500000000000', true) "
            "ON CONFLICT (id) DO UPDATE SET ativo = true"
        ), {"id": CORRETORA_CORRETOR})
        for cid in (CORRETOR, CORRETOR, CORRETOR):
            s.execute(sql_text("UPDATE corretoras SET ativo = true WHERE id = :id"),
                      {"id": cid})
        # O banco de teste PERSISTE entre runs, e tanto o tick quanto a roleta
        # leem o universo inteiro de leads marcados. Sem zerar a tag, um teste
        # herda a fila dos anteriores e o cap corta antes de chegar no lead do
        # caso. Zerar (em vez de apagar) não mexe em lead_events nem em FK.
        s.execute(sql_text(
            "UPDATE leads SET veio_remarketing_em = NULL "
            " WHERE veio_remarketing_em IS NOT NULL"))
        s.commit()
    monkeypatch.setattr(config_operacional, "_carregar", lambda: {})
    monkeypatch.setattr(config_operacional.settings, "handoff_remarketing_roleta",
                        [CORRETOR, CORRETOR, CORRETOR])
    monkeypatch.setattr(config_operacional.settings, "handoff_remarketing_corretora", CORRETOR)
    yield


def _tel() -> str:
    return "5515" + "".join(random.choice("0123456789") for _ in range(9))


def _lead_marcado(owner: int, *, telefone: str | None = None,
                  stage: int = STAGE_NOVO, tag: bool = True) -> int:
    """Lead como o handoff deixa: dono é a corretora, etapa Novo, tag carimbada."""
    tel = telefone or _tel()
    with SessionLocal() as s:
        lead = Lead(tenant_id=1, nome="Teste aviso", telefone=tel, owner_id=owner,
                    stage_id=stage, external_ref=f"test-aviso:{tel}:{random.random()}")
        s.add(lead)
        s.flush()
        if tag:
            s.execute(sql_text(
                "UPDATE leads SET veio_remarketing_em = now() WHERE id = :i"
            ), {"i": lead.id})
        s.commit()
        return int(lead.id)


# ── 1. Roleta ───────────────────────────────────────────────────────────────

def test_roleta_alterna_entre_as_tres():
    """Cada transferência vai pra próxima da lista, circular."""
    vistos = []
    with SessionLocal() as s:
        for _ in range(4):
            cid, label = hr.destino_valido(s)
            vistos.append(cid)
            # a rotação lê a última atribuição real: simula o lead recebido
            _lead_marcado(cid)
    # começa em alguém da lista e não repete antes de fechar a volta
    assert set(vistos) == {CORRETOR, CORRETOR, CORRETOR}
    assert vistos[0] == vistos[3]
    assert vistos[1] != vistos[0] and vistos[2] not in (vistos[0], vistos[1])


def test_roleta_pula_corretora_inativa(monkeypatch):
    with SessionLocal() as s:
        s.execute(sql_text("UPDATE corretoras SET ativo = false WHERE id = :id"),
                  {"id": CORRETOR})
        s.commit()
    try:
        with SessionLocal() as s:
            for _ in range(4):
                cid, _ = hr.destino_valido(s)
                assert cid in (CORRETOR, CORRETOR)
                _lead_marcado(cid)
    finally:
        with SessionLocal() as s:
            s.execute(sql_text("UPDATE corretoras SET ativo = true WHERE id = :id"),
                      {"id": CORRETOR})
            s.commit()


def test_sem_roleta_nao_transfere(monkeypatch):
    """Até 05/10/2026 a roleta vazia caía num destino único de reserva (a Pessoa12).
    Agora vazio é vazio: ninguém recebe, o card fica em Respondeu."""
    monkeypatch.setattr(config_operacional.settings, "handoff_remarketing_roleta", [])
    monkeypatch.setattr(config_operacional.settings, "handoff_remarketing_corretora", CORRETOR)
    with SessionLocal() as s:
        assert hr.destino_valido(s) is None


def test_roleta_so_com_a_corretor_nao_transfere(monkeypatch):
    """Fail-closed: ninguém válido = None, o lead fica visível na Corretor."""
    monkeypatch.setattr(config_operacional.settings, "handoff_remarketing_roleta",
                        [CORRETORA_CORRETOR])
    monkeypatch.setattr(config_operacional.settings, "handoff_remarketing_corretora",
                        CORRETORA_CORRETOR)
    with SessionLocal() as s:
        assert hr.destino_valido(s) is None


def test_roleta_do_intake_nao_muda():
    """A regra de 26/07 segue de pé: lead novo do Meta é só Pessoa10 e Pessoa11."""
    assert CORRETOR not in config_operacional.roleta_corretoras()


# ── 2. O tick ───────────────────────────────────────────────────────────────

@pytest.fixture
def _armado(monkeypatch):
    """Interruptor ligado, horário liberado, envio fake. Devolve os enviados."""
    enviados: list[dict] = []

    def _fake(session, *, telefone, nome, corretora_id, corretora_label):
        enviados.append({"telefone": telefone, "corretora_id": corretora_id})
        return {"ok": True, "template": "fake"}

    monkeypatch.setitem(config_operacional.DEFAULTS,
                        "handoff_remarketing_avisa_lead", lambda: True)
    monkeypatch.setattr(ar, "dentro_do_horario", lambda *a, **k: True)
    monkeypatch.setattr(hr, "avisar_lead", _fake)
    return enviados


def test_tick_avisa_quem_tem_tag_e_esta_em_novo(_armado):
    lead_id = _lead_marcado(CORRETOR)
    r = ar.rodar()
    assert r["enviados"] >= 1
    assert any(e["corretora_id"] == CORRETOR for e in _armado)
    with SessionLocal() as s:
        assert s.get(Lead, lead_id).stage_id == STAGE_NOVO


def test_tick_ignora_lead_sem_tag(_armado):
    tel = _tel()
    _lead_marcado(CORRETOR, telefone=tel, tag=False)
    ar.rodar()
    assert all(e["telefone"] != tel for e in _armado)


def test_tick_ignora_lead_da_corretor(_armado):
    tel = _tel()
    _lead_marcado(CORRETORA_CORRETOR, telefone=tel)
    ar.rodar()
    assert all(e["telefone"] != tel for e in _armado)


def test_tick_desarmado_nao_manda(monkeypatch):
    monkeypatch.setitem(config_operacional.DEFAULTS,
                        "handoff_remarketing_avisa_lead", lambda: False)
    _lead_marcado(CORRETOR)
    r = ar.rodar()
    assert r["enviados"] == 0 and "desarmado" in r["pulados"]


def test_tick_fora_do_horario_nao_manda(monkeypatch, _armado):
    monkeypatch.setattr(ar, "dentro_do_horario", lambda *a, **k: False)
    _lead_marcado(CORRETOR)
    r = ar.rodar()
    assert r["enviados"] == 0 and "fora_do_horario" in r["pulados"]


def test_cap_por_linha(monkeypatch, _armado):
    monkeypatch.setitem(config_operacional.DEFAULTS,
                        "aviso_remarketing_cap_linha", lambda: 2)
    for _ in range(4):
        _lead_marcado(CORRETOR)
    r = ar.rodar()
    da_corretor = [e for e in _armado if e["corretora_id"] == CORRETOR]
    assert len(da_corretor) == 2
    assert r["pulados"].get("cap_da_linha", 0) >= 2


def test_mesmo_telefone_em_dois_cards_recebe_uma_vez(_armado):
    tel = _tel()
    _lead_marcado(CORRETOR, telefone=tel)
    _lead_marcado(CORRETOR, telefone=tel)
    ar.rodar()
    assert len([e for e in _armado if e["telefone"] == tel]) == 1


def test_dry_run_nao_chama_envio(monkeypatch, _armado):
    _lead_marcado(CORRETOR)
    r = ar.rodar(dry_run=True)
    assert r["dry_run"] is True and r["enviados"] >= 1
    assert _armado == []


# ── 3. Veto do handoff rico ─────────────────────────────────────────────────

def test_quem_teve_handoff_rico_da_pessoa_nao_recebe_o_generico():
    """Já ouviu 'te resumo: você tá procurando…' pela linha da Pessoa12 — o genérico
    depois disso seria a mesma coisa, pior."""
    tel = _tel()
    with SessionLocal() as s:
        s.execute(sql_text(
            "INSERT INTO lead_handoff_notes (telefone, lead_nome, corretora_id, "
            "contexto, origem, status, tenant_id) "
            "VALUES (:t, 'Teste', :c, 'raio-x', 'pessoa', 'ativo', 1)"
        ), {"t": tel, "c": CORRETOR})
        s.commit()
        assert hr._teve_handoff_rico(s, tel) is True


def test_quem_nao_teve_handoff_rico_passa():
    with SessionLocal() as s:
        assert hr._teve_handoff_rico(s, _tel()) is False
