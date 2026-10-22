"""Testes da detecção dinâmica de mensagens de automação — Onda 3a.2."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import text

from app.db.session import SessionLocal
from app.services.deteccao_automacao import (
    detectar_automacoes,
    invalidar_cache,
    is_automacao,
    normalizar_texto,
    upsert_automacoes,
)


CORRETORA_A_ID = 3
CORRETORA_C_ID = 1

# Textos-sentinela deste arquivo. Até 26/07/2026 a limpeza era
# `DELETE FROM mensagens_automacao WHERE corretora_id IN (1,2,3)` — varredura
# que apagaria TODAS as automações detectadas das 3 corretoras reais, incluindo
# as que a Dona marcou `ativa=false` à mão (decisão humana, não reproduzível).
#
# `upsert_automacoes` é código de aplicação e grava sempre com tenant_id=1, então
# a sentinela aqui NÃO pode ser o tenant (como em test_coaching.py) — é a lista
# fechada de `texto_normalizado` que este arquivo semeia. Todo texto novo
# inserido nos testes abaixo precisa entrar nesta lista, senão vaza entre runs.
_TEXTOS_DO_TESTE = (
    "ola boa tarde",
    "cumprimento manual",
    "ola tudo bem",
    "saudacao da corretor",
    "so manual",
)

# Prefixo dos `leads_snapshot.lead_id` semeados por `_seed_lead` (999001,
# 999100, 999201...). Esses leads NUNCA eram apagados: ficavam no banco entre
# runs referenciando `corretoras.id=3`, e na sessão SEGUINTE o seed de
# tests/conftest.py quebrava com
#   ForeignKeyViolation: ... "leads_snapshot_corretora_id_fkey" ...
#   DETAIL: Key (id)=(3) is still referenced from table "leads_snapshot"
# derrubando a suíte INTEIRA em erro de setup, de forma alternada (verde numa
# run, 95 erros na próxima). Limpar aqui fecha o vazamento na origem.
_PREFIXO_LEAD_TESTE = "999"


@pytest.fixture(autouse=True)
def _clean_automacao():
    _apagar_do_teste()
    yield
    _apagar_do_teste()


def _apagar_do_teste() -> None:
    """Remove SÓ o que este arquivo semeia (nunca dado real)."""
    invalidar_cache()
    with SessionLocal() as s:
        s.execute(
            text("DELETE FROM mensagens_automacao WHERE texto_normalizado = ANY(:txts)"),
            {"txts": list(_TEXTOS_DO_TESTE)},
        )
        s.execute(
            text("DELETE FROM leads_snapshot WHERE lead_id LIKE :pref"),
            {"pref": f"{_PREFIXO_LEAD_TESTE}%"},
        )
        s.commit()


# ── Normalização ─────────────────────────────────────────────────────────────


def test_normalizar_lowercase_e_pontuacao():
    assert normalizar_texto("Olá boa tarde tudo bem?") == "ola boa tarde tudo bem"


def test_normalizar_remove_emoji():
    assert normalizar_texto("Olá! Boa Tarde 😊  tudo bem???") == "ola! boa tarde tudo bem"
    # Note: o ! interno permanece — só pontuação FINAL é tirada.


def test_normalizar_colapsa_espacos():
    assert normalizar_texto("  oi  oi  ") == "oi oi"


def test_normalizar_string_vazia():
    assert normalizar_texto("") == ""
    assert normalizar_texto(None) == ""  # type: ignore[arg-type]


def test_normalizar_so_emoji_vira_vazio():
    assert normalizar_texto("😊😊😊") == ""


# ── Detecção ─────────────────────────────────────────────────────────────────


def _seed_lead(lead_id: str, telefone: str, corretora_id: int = CORRETORA_A_ID) -> None:
    with SessionLocal() as s:
        s.execute(text(
            "INSERT INTO leads_snapshot (lead_id, nome, telefone, corretora_id, ultima_sync) "
            "VALUES (:l, :n, :t, :c, NOW()) "
            "ON CONFLICT (lead_id) DO UPDATE SET telefone=:t, corretora_id=:c, ultima_sync=NOW()"
        ), {"l": lead_id, "n": "L", "t": telefone, "c": corretora_id})
        s.commit()


@pytest.fixture
def _set_corretora_instance():
    """Garante que a corretora do teste tem `evolution_instance` (o detector
    pula quem não tem). O seed de tests/conftest.py já preenche as 3 — o UPDATE
    é rede de segurança e NUNCA sobrescreve valor existente (COALESCE), pra não
    reescrever a instância real de ninguém. Escopo restrito à corretora usada
    aqui, em vez do antigo `WHERE id IN (1,2,3)`.
    """
    with SessionLocal() as s:
        s.execute(
            text(
                "UPDATE corretoras "
                "SET evolution_instance = COALESCE(evolution_instance, 'fake_inst') "
                "WHERE id = :cid AND evolution_instance IS NULL"
            ),
            {"cid": CORRETORA_A_ID},
        )
        s.commit()
    yield


def test_detectar_3_leads_distintos_eh_automacao(_set_corretora_instance):
    """Cada lead seedado retorna a saudação; demais leads do snapshot retornam []."""
    fones = {"+5500000000000", "+5500000000000", "+5500000000000"}
    _seed_lead("999001", "+5500000000000")
    _seed_lead("999002", "+5500000000000")
    _seed_lead("999003", "+5500000000000")

    fake_evo = MagicMock()
    saud = "Olá boa tarde tudo bem?"

    def _conv(inst, tel, hours_back=720):
        if tel in fones:
            return [{"from": "corretora", "text": saud,
                     "at": "2026-05-09T10:00:00+00:00"}]
        return []
    fake_evo.get_conversation.side_effect = _conv

    cands = detectar_automacoes(
        CORRETORA_A_ID, janela_dias=30, min_leads_distintos=3,
        evolution_tool=fake_evo,
    )
    cand_saud = [c for c in cands if c["texto_normalizado"] == "ola boa tarde tudo bem"]
    assert len(cand_saud) == 1
    c = cand_saud[0]
    assert c["leads_distintos"] == 3
    assert c["ocorrencias"] == 3


def test_detectar_msg_repetida_mesmo_lead_nao_eh_automacao(_set_corretora_instance):
    """5x 'ola unico-9991' no MESMO lead — não é automação (1 lead distinto)."""
    _seed_lead("999100", "+5500000000000")
    texto_unico = "frase muito específica desse lead apenas xyzzy 9991"

    def _conv(inst, tel, hours_back=720):
        if tel == "+5500000000000":
            return [
                {"from": "corretora", "text": texto_unico,
                 "at": f"2026-05-09T1{i}:00:00+00:00"} for i in range(5)
            ]
        return []
    fake_evo = MagicMock()
    fake_evo.get_conversation.side_effect = _conv
    cands = detectar_automacoes(
        CORRETORA_A_ID, janela_dias=30, min_leads_distintos=3,
        evolution_tool=fake_evo,
    )
    # Texto único do mesmo lead não pode aparecer como candidato
    assert all(texto_unico.lower() not in c["texto_normalizado"]
               for c in cands)


def test_detectar_ignora_msgs_do_lead(_set_corretora_instance):
    """Mensagens do lead nunca viram automação da corretora."""
    fones = {"+5500000000000", "+5500000000000", "+5500000000000"}
    _seed_lead("999201", "+5500000000000")
    _seed_lead("999202", "+5500000000000")
    _seed_lead("999203", "+5500000000000")
    texto = "frase só do lead nunca da corretora abc999"

    def _conv(inst, tel, hours_back=720):
        if tel in fones:
            return [{"from": "lead", "text": texto,
                     "at": "2026-05-09T10:00:00+00:00"}]
        return []
    fake_evo = MagicMock()
    fake_evo.get_conversation.side_effect = _conv
    cands = detectar_automacoes(
        CORRETORA_A_ID, janela_dias=30, min_leads_distintos=3,
        evolution_tool=fake_evo,
    )
    assert all(texto.lower() not in c["texto_normalizado"] for c in cands)


# ── Upsert ───────────────────────────────────────────────────────────────────


def test_upsert_insere_novo():
    cand = {
        "texto_normalizado": "ola boa tarde",
        "texto_exemplo": "Olá boa tarde 🌻",
        "ocorrencias": 5, "leads_distintos": 4,
        "primeira_vez": "2026-05-01T10:00:00+00:00",
        "ultima_vez": "2026-05-09T10:00:00+00:00",
    }
    n = upsert_automacoes(CORRETORA_A_ID, [cand])
    assert n == 1
    with SessionLocal() as s:
        row = s.execute(text(
            "SELECT ativa, ocorrencias, leads_distintos FROM mensagens_automacao "
            "WHERE corretora_id = :c AND texto_normalizado = :t"
        ), {"c": CORRETORA_A_ID, "t": "ola boa tarde"}).first()
    assert row is not None
    assert row[0] is True and row[1] == 5 and row[2] == 4


def test_upsert_nao_reativa_desativada():
    """Dona marcou ativa=false; upsert atualiza counts mas NÃO reativa."""
    with SessionLocal() as s:
        s.execute(text(
            "INSERT INTO mensagens_automacao "
            "(corretora_id, texto_normalizado, texto_exemplo, ocorrencias, "
            " leads_distintos, primeira_vez, ultima_vez, ativa, observacao) "
            "VALUES (:c, :t, 'ex', 3, 3, NOW(), NOW(), false, 'manual')"
        ), {"c": CORRETORA_A_ID, "t": "cumprimento manual"})
        s.commit()
    cand = {
        "texto_normalizado": "cumprimento manual",
        "texto_exemplo": "Cumprimento Manual",
        "ocorrencias": 9, "leads_distintos": 7,
        "primeira_vez": "2026-04-01T10:00:00+00:00",
        "ultima_vez": "2026-05-09T10:00:00+00:00",
    }
    upsert_automacoes(CORRETORA_A_ID, [cand])
    with SessionLocal() as s:
        row = s.execute(text(
            "SELECT ativa, ocorrencias FROM mensagens_automacao "
            "WHERE corretora_id = :c AND texto_normalizado = :t"
        ), {"c": CORRETORA_A_ID, "t": "cumprimento manual"}).first()
    assert row[0] is False  # NÃO reativa
    assert row[1] == 9      # mas atualiza counts


# ── is_automacao lookup ──────────────────────────────────────────────────────


def test_is_automacao_match_exato():
    cand = {
        "texto_normalizado": "ola tudo bem",
        "texto_exemplo": "Olá tudo bem?",
        "ocorrencias": 3, "leads_distintos": 3,
        "primeira_vez": "2026-05-01T10:00:00+00:00",
        "ultima_vez": "2026-05-09T10:00:00+00:00",
    }
    upsert_automacoes(CORRETORA_A_ID, [cand])
    invalidar_cache(CORRETORA_A_ID)
    assert is_automacao(CORRETORA_A_ID, "ola tudo bem") is True


def test_is_automacao_normaliza_input():
    cand = {
        "texto_normalizado": "ola tudo bem",
        "texto_exemplo": "Olá tudo bem?",
        "ocorrencias": 3, "leads_distintos": 3,
        "primeira_vez": "2026-05-01T10:00:00+00:00",
        "ultima_vez": "2026-05-09T10:00:00+00:00",
    }
    upsert_automacoes(CORRETORA_A_ID, [cand])
    invalidar_cache(CORRETORA_A_ID)
    # variação com emoji + caps + pontuação extra
    assert is_automacao(CORRETORA_A_ID, "Olá tudo bem??? 😊") is True


def test_is_automacao_texto_diferente_false():
    cand = {
        "texto_normalizado": "ola tudo bem",
        "texto_exemplo": "Olá tudo bem?",
        "ocorrencias": 3, "leads_distintos": 3,
        "primeira_vez": "2026-05-01T10:00:00+00:00",
        "ultima_vez": "2026-05-09T10:00:00+00:00",
    }
    upsert_automacoes(CORRETORA_A_ID, [cand])
    invalidar_cache(CORRETORA_A_ID)
    assert is_automacao(CORRETORA_A_ID, "vi seu interesse no apartamento") is False


def test_is_automacao_corretora_isolada():
    """Privacidade: automação da corretora 1 não vaza pra 3."""
    cand = {
        "texto_normalizado": "saudacao da corretor",
        "texto_exemplo": "Saudação da Pessoa10",
        "ocorrencias": 3, "leads_distintos": 3,
        "primeira_vez": "2026-05-01T10:00:00+00:00",
        "ultima_vez": "2026-05-09T10:00:00+00:00",
    }
    upsert_automacoes(CORRETORA_C_ID, [cand])
    invalidar_cache()
    assert is_automacao(CORRETORA_C_ID, "saudacao da corretor") is True
    assert is_automacao(CORRETORA_A_ID, "saudacao da corretor") is False


def test_is_automacao_ignora_desativada():
    with SessionLocal() as s:
        s.execute(text(
            "INSERT INTO mensagens_automacao "
            "(corretora_id, texto_normalizado, texto_exemplo, ocorrencias, "
            " leads_distintos, primeira_vez, ultima_vez, ativa) "
            "VALUES (:c, 'so manual', 'ex', 5, 5, NOW(), NOW(), false)"
        ), {"c": CORRETORA_A_ID})
        s.commit()
    invalidar_cache(CORRETORA_A_ID)
    assert is_automacao(CORRETORA_A_ID, "so manual") is False
