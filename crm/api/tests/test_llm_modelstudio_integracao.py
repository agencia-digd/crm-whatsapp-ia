"""
Integração REAL do degrau Qwen — bate no endpoint do workspace dedicado.

Existe porque o teste de 05/09/2026 que faltava era exatamente este: a chave
existia no .env, o endpoint hardcoded era o errado, e ninguém tinha provado
que o degrau RESPONDIA. Marcado `integration`: fora do run default
(pyproject addopts = -m 'not integration'). Rodar à mão no VPS:

    TEST_DATABASE_URL=... .venv/bin/python -m pytest \\
        tests/test_llm_modelstudio_integracao.py -m integration -q -rs

Credenciais: lidas de /etc/agencia/qwen.env (QWEN_API_KEY/QWEN_BASE_URL/QWEN_MODEL)
NO MOMENTO DO TESTE. Sem o arquivo (CI, Mac), pula. Nunca imprime a chave.
"""
from __future__ import annotations

import json
import os

import pytest

from app.config import _ler_env_file
from app.llm.modelstudio_provider import ModelStudioProvider, limpar_cache_verificacao

pytestmark = pytest.mark.integration

QWEN_ENV = os.environ.get("QWEN_ENV_FILE", "/etc/agencia/qwen.env")


@pytest.fixture(scope="module")
def qwen() -> ModelStudioProvider:
    cfg = _ler_env_file(QWEN_ENV)
    faltando = [k for k in ("QWEN_API_KEY", "QWEN_BASE_URL", "QWEN_MODEL") if not cfg.get(k)]
    if faltando:
        pytest.skip(f"sem credencial Qwen em {QWEN_ENV} (faltando {faltando}) — pulado")
    limpar_cache_verificacao()
    return ModelStudioProvider(
        cfg["QWEN_API_KEY"], model=cfg["QWEN_MODEL"], base_url=cfg["QWEN_BASE_URL"], timeout=60
    )


def test_verificar_prova_que_o_degrau_responde(qwen: ModelStudioProvider) -> None:
    st = qwen.verificar(timeout=20, usar_cache=False)
    assert st.ok, f"degrau Qwen NÃO respondeu: {st.motivo}"
    assert st.duracao_ms > 0
    print(f"\n[integração] verificar(): ok modelo={qwen.model} {st.duracao_ms}ms motivo={st.motivo}")


def test_chave_da_casa_nao_vale_no_dashscope_publico(qwen: ModelStudioProvider) -> None:
    """Documenta o erro que matou o degrau: mesmo key, endpoint público = 401."""
    publico = ModelStudioProvider(
        qwen.api_key, model=qwen.model,
        base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1", timeout=30,
    )
    st = publico.verificar(timeout=20, usar_cache=False)
    assert st.ok is False and st.permanente is True, st
    assert "401" in st.motivo
    print(f"\n[integração] dashscope público: {st.motivo[:80]}")


def test_json_schema_como_a_pessoa_usa(qwen: ModelStudioProvider) -> None:
    """Saída estruturada (json_object + schema no prompt) com reasoning_content no meio."""
    schema = {
        "type": "object",
        "properties": {"text": {"type": "string"}, "tool_call": {"type": ["object", "null"]}},
        "required": ["text", "tool_call"],
    }
    json_str, usage = qwen.complete(
        "Você é a Pessoa, atendente de imobiliária. Responda curto, em PT-BR.",
        "Lead: oi, vi o anúncio do apartamento de 2 quartos, ainda tem?",
        json_schema=schema,
    )
    dados = json.loads(json_str)
    assert isinstance(dados.get("text"), str) and dados["text"].strip()
    assert "tool_call" in dados
    assert usage.input_tokens > 0 and usage.output_tokens > 0
    print(f"\n[integração] json_schema ok: text={dados['text'][:80]!r} tokens={usage.total}")


@pytest.mark.parametrize(
    "modelo", os.environ.get("QWEN_MODELOS_RESERVA_TESTE", "kimi-k3").split(",")
)
def test_modelo_de_reserva_responde_com_a_mesma_chave(qwen: ModelStudioProvider, modelo: str) -> None:
    """O workspace serve outros fornecedores (Kimi, DeepSeek, GLM) na mesma credencial."""
    reserva = ModelStudioProvider(qwen.api_key, model=modelo, base_url=qwen.base_url, timeout=60)
    st = reserva.verificar(timeout=30, usar_cache=False)
    assert st.ok, f"reserva {modelo} NÃO respondeu: {st.motivo}"
    json_str, _ = reserva.complete("Responda só JSON.", 'Devolva {"a": 1}', json_schema={"a": "int"})
    assert json.loads(json_str) == {"a": 1}
    print(f"\n[integração] reserva {modelo}: ok {st.duracao_ms}ms")


def test_reasoning_content_nao_contamina_o_content(qwen: ModelStudioProvider) -> None:
    """Com thinking ligado o modelo devolve reasoning_content; o content segue limpo."""
    pensante = ModelStudioProvider(
        qwen.api_key, model=qwen.model, base_url=qwen.base_url, timeout=60, enable_thinking=True
    )
    json_str, _ = pensante.complete("Responda só JSON.", 'Devolva {"a": 1}', json_schema={"a": "int"})
    assert json.loads(json_str) == {"a": 1}
