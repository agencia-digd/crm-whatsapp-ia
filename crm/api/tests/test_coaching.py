"""Testes do módulo de coaching — Onda 3a.1."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.db.session import SessionLocal
from app.inspector.coaching import (
    CATEGORIAS_VALIDAS,
    analisar_coaching,
)
from app.services.briefing_corretora import coletar_coaching


CORRETORA_A_ID = 3
CORRETORA_C_ID = 1

# Sentinela deste arquivo. Até 26/07/2026 a limpeza era
# `DELETE FROM coaching_observations WHERE corretora_id IN (1, 2, 3)` — uma
# varredura que apagaria TODO o coaching real da Pessoa5, Pessoa1 e Pessoa3
# se a suíte pegasse o banco errado. Agora cada linha criada aqui nasce com
# `tenant_id = TENANT_TESTE` e a limpeza só remove o que este arquivo escreveu.
# `coletar_coaching` filtra por corretora_id/janela e IGNORA tenant_id, então as
# asserções continuam valendo com a sentinela.
TENANT_TESTE = 7731


@pytest.fixture(autouse=True)
def _clean_coaching():
    _apagar_do_teste()
    yield
    _apagar_do_teste()


def _apagar_do_teste() -> None:
    """Remove SÓ as observações semeadas por este arquivo (nunca dado real)."""
    with SessionLocal() as s:
        s.execute(
            text("DELETE FROM coaching_observations WHERE tenant_id = :t"),
            {"t": TENANT_TESTE},
        )
        s.commit()


def _insert_obs(corretora_id: int, lead_nome: str, observacao: str,
                analyzed_at: datetime | None = None,
                categoria: str = "resposta_seca",
                origem: str = "lead_quente") -> None:
    with SessionLocal() as s:
        params = {
            "cid": corretora_id, "ln": lead_nome, "obs": observacao,
            "cat": categoria, "ori": origem, "tenant": TENANT_TESTE,
        }
        if analyzed_at is not None:
            params["at"] = analyzed_at
            s.execute(text(
                "INSERT INTO coaching_observations "
                "(tenant_id, corretora_id, lead_nome, observacao, categoria, origem, analyzed_at) "
                "VALUES (:tenant, :cid, :ln, :obs, :cat, :ori, :at)"
            ), params)
        else:
            s.execute(text(
                "INSERT INTO coaching_observations "
                "(tenant_id, corretora_id, lead_nome, observacao, categoria, origem) "
                "VALUES (:tenant, :cid, :ln, :obs, :cat, :ori)"
            ), params)
        s.commit()


# ── Schema ───────────────────────────────────────────────────────────────────


def test_coaching_schema_categoria_invalida_rejeitada():
    with pytest.raises(IntegrityError):
        with SessionLocal() as s:
            s.execute(text(
                "INSERT INTO coaching_observations "
                "(corretora_id, observacao, categoria, origem) "
                "VALUES (3, 'teste', 'categoria_inexistente', 'lead_quente')"
            ))
            s.commit()


def test_coaching_schema_origem_invalida_rejeitada():
    with pytest.raises(IntegrityError):
        with SessionLocal() as s:
            s.execute(text(
                "INSERT INTO coaching_observations "
                "(corretora_id, observacao, categoria, origem) "
                "VALUES (3, 'teste', 'outro', 'origem_qualquer')"
            ))
            s.commit()


def test_coaching_schema_indices_existem():
    with SessionLocal() as s:
        rows = s.execute(text(
            "SELECT indexname FROM pg_indexes "
            "WHERE tablename = 'coaching_observations'"
        )).all()
    nomes = {r[0] for r in rows}
    assert "idx_coaching_corretora_data" in nomes
    assert "idx_coaching_categoria" in nomes


# ── Coletor pra briefing ─────────────────────────────────────────────────────


def test_coletar_coaching_filtra_corretora():
    """REGRA DE OURO: corretora 1 NUNCA recebe observação da corretora 3."""
    _insert_obs(CORRETORA_A_ID, "Lead Pessoa12", "obs da Pessoa12")
    _insert_obs(CORRETORA_C_ID, "Lead Pessoa10", "obs da Pessoa10")
    out = coletar_coaching(CORRETORA_C_ID)
    nomes = [c.lead_nome for c in out]
    assert "Lead Pessoa10" in nomes
    assert "Lead Pessoa12" not in nomes
    assert all(c.observacao != "obs da Pessoa12" for c in out)


def test_coletar_coaching_janela_24h():
    agora = datetime.now(timezone.utc)
    _insert_obs(CORRETORA_A_ID, "Velho", "obs antiga",
                analyzed_at=agora - timedelta(hours=30))
    _insert_obs(CORRETORA_A_ID, "Recente", "obs nova",
                analyzed_at=agora - timedelta(hours=12))
    out = coletar_coaching(CORRETORA_A_ID)
    nomes = [c.lead_nome for c in out]
    assert "Recente" in nomes
    assert "Velho" not in nomes


def test_coletar_coaching_limite():
    for i in range(8):
        _insert_obs(CORRETORA_A_ID, f"Lead{i}", f"obs {i}")
    out = coletar_coaching(CORRETORA_A_ID, limite=5)
    assert len(out) == 5


# ── analisar_coaching ────────────────────────────────────────────────────────


def test_analisar_coaching_json_ok():
    fake = MagicMock()
    fake.complete.return_value = (
        '{"categoria": "falta_qualificacao", '
        '"trecho_corretora": "te mando opções por X mil", '
        '"observacao": "Talvez vale entender o que ele busca antes.", '
        '"sugestao": "O que é prioridade pra você?"}',
        MagicMock(),
    )
    obs = analisar_coaching(
        corretora_nome="Pessoa3", corretora_id=3,
        lead_nome="Pedro", lead_id=12345,
        conversa=[
            {"from": "lead", "text": "oi", "at": "2026-05-09T10:00:00"},
            {"from": "corretora", "text": "te mando opções por X mil",
             "at": "2026-05-09T10:01:00"},
        ],
        origem="lead_quente",
        provider=fake,
    )
    assert obs is not None
    assert obs.categoria == "falta_qualificacao"
    assert obs.lead_nome == "Pedro"
    assert obs.lead_id == 12345
    assert obs.corretora_id == 3
    assert obs.origem == "lead_quente"
    assert "Talvez vale entender" in obs.observacao
    assert obs.sugestao == "O que é prioridade pra você?"


def test_analisar_coaching_categoria_null():
    fake = MagicMock()
    fake.complete.return_value = ('{"categoria": null}', MagicMock())
    obs = analisar_coaching(
        corretora_nome="Pessoa3", corretora_id=3,
        lead_nome="X", lead_id=1, conversa=[], origem="lead_quente",
        provider=fake,
    )
    assert obs is None


def test_analisar_coaching_categoria_invalida_retorna_none():
    fake = MagicMock()
    fake.complete.return_value = (
        '{"categoria": "inventada", "observacao": "x"}', MagicMock(),
    )
    obs = analisar_coaching(
        corretora_nome="X", corretora_id=3, lead_nome="X", lead_id=1,
        conversa=[], origem="lead_quente", provider=fake,
    )
    assert obs is None


def test_analisar_coaching_json_invalido():
    fake = MagicMock()
    fake.complete.return_value = ("lixo sem json", MagicMock())
    obs = analisar_coaching(
        corretora_nome="X", corretora_id=3, lead_nome="X", lead_id=1,
        conversa=[], origem="lead_quente", provider=fake,
    )
    assert obs is None


def test_analisar_coaching_llm_excecao_retorna_none():
    fake = MagicMock()
    fake.complete.side_effect = RuntimeError("timeout CLI")
    obs = analisar_coaching(
        corretora_nome="X", corretora_id=3, lead_nome="X", lead_id=1,
        conversa=[], origem="lead_quente", provider=fake,
    )
    assert obs is None


def test_analisar_coaching_origem_invalida_lanca():
    with pytest.raises(ValueError):
        analisar_coaching(
            corretora_nome="X", corretora_id=3, lead_nome="X", lead_id=1,
            conversa=[], origem="invalido",  # type: ignore[arg-type]
        )


def test_coaching_ignora_automacao(monkeypatch):
    """Mensagens de automação são filtradas antes do LLM."""
    from app.services import deteccao_automacao
    deteccao_automacao.invalidar_cache()
    deteccao_automacao._cache[3] = {"ola boa tarde tudo bem"}

    captured: dict = {}
    fake_provider = MagicMock()

    def _capture(system, user, schema):
        captured["user"] = user
        return ('{"categoria": "resposta_seca", '
                '"trecho_corretora": "blz", '
                '"observacao": "obs.", "sugestao": "x"}', MagicMock())
    fake_provider.complete.side_effect = _capture

    conversa = [
        {"from": "lead", "text": "oi", "at": "2026-05-09T10:00:00"},
        {"from": "corretora", "text": "Olá boa tarde tudo bem?", "at": "2026-05-09T10:01:00"},
        {"from": "corretora", "text": "Olá boa tarde tudo bem?", "at": "2026-05-09T11:01:00"},
        {"from": "lead", "text": "queria ver um apto", "at": "2026-05-09T12:00:00"},
        {"from": "corretora", "text": "blz, te mando", "at": "2026-05-09T12:05:00"},
    ]
    obs = analisar_coaching(
        corretora_nome="Pessoa3", corretora_id=3, lead_nome="X", lead_id=1,
        conversa=conversa, origem="lead_quente", provider=fake_provider,
    )
    assert obs is not None
    # Automações não devem aparecer no prompt; mensagem real "blz, te mando" sim
    assert "Olá boa tarde tudo bem" not in captured["user"]
    assert "blz, te mando" in captured["user"]
    deteccao_automacao.invalidar_cache()


def test_coaching_pula_se_so_automacao(monkeypatch):
    """Se TODA fala da corretora é automação: retorna None sem chamar LLM."""
    from app.services import deteccao_automacao
    deteccao_automacao.invalidar_cache()
    deteccao_automacao._cache[3] = {"ola boa tarde tudo bem"}

    fake_provider = MagicMock()
    conversa = [
        {"from": "lead", "text": "oi", "at": "2026-05-09T10:00:00"},
        {"from": "corretora", "text": "Olá boa tarde tudo bem?", "at": "2026-05-09T10:01:00"},
        {"from": "corretora", "text": "olá BOA tarde TUDO bem!", "at": "2026-05-09T11:01:00"},
        {"from": "corretora", "text": "Olá boa tarde tudo bem??", "at": "2026-05-09T12:00:00"},
    ]
    obs = analisar_coaching(
        corretora_nome="Pessoa3", corretora_id=3, lead_nome="X", lead_id=1,
        conversa=conversa, origem="lead_quente", provider=fake_provider,
    )
    assert obs is None
    fake_provider.complete.assert_not_called()
    deteccao_automacao.invalidar_cache()


def test_categorias_validas_match_migration():
    genericas = {
        "falta_qualificacao", "foco_valor_cedo", "resposta_seca",
        "perdeu_deixa", "demora_resposta", "manual_followup_apos_robo", "outro",
    }
    movimentos = {
        "movimento_sem_convite_visita", "movimento_visita_sem_horario",
        "movimento_sem_tira_friccao", "movimento_sem_decisor",
        "movimento_sem_qualificar_dinheiro", "movimento_sem_escassez",
        "movimento_followup_robotico",
    }
    assert CATEGORIAS_VALIDAS == genericas | movimentos


def test_categorias_movimento_batem_com_playbook():
    """As 6 categorias de fechamento vêm da fonte única (playbook_corretora_b)."""
    from app.persona.playbook_corretora_b import CATEGORIAS_MOVIMENTO, MOVIMENTOS

    assert len(MOVIMENTOS) == 7  # 6 movimentos + horário concreto derivado do #1
    assert CATEGORIAS_MOVIMENTO <= CATEGORIAS_VALIDAS
    # todo movimento_* é uma categoria válida
    assert all(c.startswith("movimento_") for c in CATEGORIAS_MOVIMENTO)


def test_analisar_coaching_aceita_categoria_movimento():
    """LLM pode devolver uma categoria de fechamento; passa validação e persiste no schema."""
    fake = MagicMock()
    fake.complete.return_value = (
        '{"categoria": "movimento_sem_convite_visita", '
        '"trecho_corretora": "te mando os valores por WhatsApp", '
        '"observacao": "Lead tava quente — talvez valha amarrar a visita com horário.", '
        '"sugestao": "consigo terça 10h ou quarta 16h pra conhecer, qual fica melhor?"}',
        MagicMock(),
    )
    obs = analisar_coaching(
        corretora_nome="Pessoa3", corretora_id=3,
        lead_nome="Pedro", lead_id=999,
        conversa=[
            {"from": "lead", "text": "gostei do de 2 quartos, quanto fica?",
             "at": "2026-07-24T10:00:00"},
            {"from": "corretora", "text": "te mando os valores por WhatsApp",
             "at": "2026-07-24T10:01:00"},
        ],
        origem="lead_quente",
        provider=fake,
    )
    assert obs is not None
    assert obs.categoria == "movimento_sem_convite_visita"
    assert "terça 10h" in obs.sugestao


def test_playbook_injetado_no_system_prompt():
    """O bloco do playbook Pessoa1 entra no system prompt de coaching."""
    from app.inspector import coaching

    assert "PLAYBOOK DE FECHAMENTO" in coaching._SYSTEM
    assert "movimento_sem_convite_visita" in coaching._SYSTEM
