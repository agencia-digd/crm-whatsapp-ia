"""Obra 6b: a Assistente roda SÓ no `claude -p` (Haiku), sem Codex/Qwen na reserva, com teto por chamada."""
from app.config import get_llm_provider_assistente, settings
from app.llm.cli_provider import ClaudeCliProvider
from app.llm.contabil_provider import ContabilProvider
from app.llm.codex_provider import FallbackProvider


def test_assistente_usa_claude_cli_haiku_sem_reserva():
    prov = get_llm_provider_assistente()
    assert isinstance(prov, ContabilProvider)  # custo/latência continuam em llm_chamadas
    interno = prov._interno
    assert isinstance(interno, ClaudeCliProvider)
    assert not isinstance(interno, FallbackProvider)  # nada de Codex na cadeia
    assert interno.model == settings.model_assistente == "claude-haiku-4-5-20251001"


def test_timeout_da_cli_cabe_na_tela():
    # pior caso: roteador 2x (retry) + resposta = 3 chamadas; a tela espera 150 s
    # (acme-painel/app/api/assistente/route.ts, TELA_TIMEOUT_MS).
    assert 3 * settings.assistente_cli_timeout < 150
    assert get_llm_provider_assistente()._interno.timeout == settings.assistente_cli_timeout


def test_inspector_e_sombra_continuam_no_provider_global():
    from app.config import get_llm_provider

    interno = get_llm_provider()._interno
    assert not (isinstance(interno, ClaudeCliProvider) and interno.model == settings.model_assistente)
