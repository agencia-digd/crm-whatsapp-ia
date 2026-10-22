"""
Testes do AnthropicApiProvider (Claude via API oficial) — httpx mockado.

Cobre o que custa caro se quebrar em produção:
  * bate no endpoint /messages certo, com modelo e versão da API;
  * a chave vai no header `x-api-key` e NUNCA aparece em log/erro;
  * sem chave falha claro e PERMANENTE (não vira retry infinito);
  * 429/5xx é transitório (retry), 401/400 é permanente (propaga na hora);
  * JSON embrulhado em markdown ainda é extraído;
  * a contagem de tokens soma cache_read e cache_creation — token cacheado
    é mais barato, mas foi pago; ignorar inflaria a economia na comparação
    entre providers, que é justamente pra que este provider existe;
  * cache do system só é pedido quando o system passa do piso da Anthropic.
"""
import json
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.llm.api_provider import (
    API_VERSION,
    BASE_URL,
    AnthropicApiError,
    AnthropicApiProvider,
)


def _resp(
    texto_saida: str = "ok",
    *,
    status: int = 200,
    usage: dict | None = None,
    texto: str | None = None,
) -> MagicMock:
    m = MagicMock()
    m.status_code = status
    corpo = {
        "content": [{"type": "text", "text": texto_saida}],
        "usage": usage if usage is not None else {"input_tokens": 120, "output_tokens": 30},
    }
    m.json.return_value = corpo
    m.text = texto if texto is not None else json.dumps(corpo)
    return m


def _provider(**kw) -> AnthropicApiProvider:
    kw.setdefault("api_key", "sk-teste-NAO-REAL")
    return AnthropicApiProvider(**kw)


# ── endpoint, modelo e cabeçalhos ────────────────────────────────────────────

def test_bate_no_endpoint_messages_com_modelo_e_versao():
    with patch("app.llm.api_provider.httpx.post", return_value=_resp()) as post:
        _provider(model="claude-sonnet-4-6").complete_text("sys", "user")
    url = post.call_args[0][0]
    assert url == f"{BASE_URL}/messages"
    assert post.call_args.kwargs["json"]["model"] == "claude-sonnet-4-6"
    assert post.call_args.kwargs["headers"]["anthropic-version"] == API_VERSION


def test_chave_vai_no_header_x_api_key():
    with patch("app.llm.api_provider.httpx.post", return_value=_resp()) as post:
        _provider(api_key="sk-segredo").complete_text("sys", "user")
    assert post.call_args.kwargs["headers"]["x-api-key"] == "sk-segredo"


def test_chave_nunca_vaza_na_mensagem_de_erro():
    resp = _resp(status=401, texto="unauthorized")
    with patch("app.llm.api_provider.httpx.post", return_value=resp):
        with pytest.raises(AnthropicApiError) as exc:
            _provider(api_key="sk-segredo-que-nao-pode-vazar").complete_text("s", "u")
    assert "sk-segredo" not in str(exc.value)


# ── ausência de chave ────────────────────────────────────────────────────────

def test_sem_chave_falha_permanente_e_nao_chama_a_rede():
    with patch("app.llm.api_provider.httpx.post") as post:
        with pytest.raises(AnthropicApiError) as exc:
            _provider(api_key="").complete_text("s", "u")
    assert exc.value.transient is False
    assert "ANTHROPIC_API_KEY" in str(exc.value)
    post.assert_not_called()


def test_construir_sem_chave_nao_levanta():
    AnthropicApiProvider(api_key="")  # não pode derrubar o factory da produção


# ── transitório x permanente ─────────────────────────────────────────────────

@pytest.mark.parametrize("status", [429, 500, 503])
def test_429_e_5xx_sao_transitorios(status):
    with patch("app.llm.api_provider.httpx.post", return_value=_resp(status=status)):
        with pytest.raises(AnthropicApiError) as exc:
            _provider()._chamar("s", "u", tentativa=1)
    assert exc.value.transient is True


@pytest.mark.parametrize("status", [400, 401, 403])
def test_4xx_de_chave_ou_payload_e_permanente(status):
    with patch("app.llm.api_provider.httpx.post", return_value=_resp(status=status)):
        with pytest.raises(AnthropicApiError) as exc:
            _provider()._chamar("s", "u", tentativa=1)
    assert exc.value.transient is False


def test_timeout_e_transitorio():
    with patch("app.llm.api_provider.httpx.post", side_effect=httpx.TimeoutException("t")):
        with pytest.raises(AnthropicApiError) as exc:
            _provider()._chamar("s", "u", tentativa=1)
    assert exc.value.transient is True


def test_transitorio_vira_retry_e_a_segunda_passa():
    respostas = [_resp(status=500), _resp("depois deu certo")]
    with patch("app.llm.api_provider.httpx.post", side_effect=respostas) as post:
        saida, _ = _provider().complete("s", "u")
    assert saida == "depois deu certo"
    assert post.call_count == 2


# ── extração de JSON ─────────────────────────────────────────────────────────

def test_json_embrulhado_em_markdown_e_extraido():
    bruto = '```json\n{"temperatura": "quente"}\n```'
    with patch("app.llm.api_provider.httpx.post", return_value=_resp(bruto)):
        saida, _ = _provider().complete("s", "u", {"type": "object"})
    assert json.loads(saida)["temperatura"] == "quente"


def test_schema_entra_no_prompt_do_usuario():
    with patch("app.llm.api_provider.httpx.post", return_value=_resp('{"a":1}')) as post:
        _provider().complete("s", "conversa", {"type": "object", "properties": {"a": {}}})
    enviado = post.call_args.kwargs["json"]["messages"][0]["content"]
    assert "conversa" in enviado and "JSON" in enviado


def test_sem_json_apos_duas_tentativas_levanta():
    with patch("app.llm.api_provider.httpx.post", return_value=_resp("sem json aqui")):
        with pytest.raises(RuntimeError, match="JSON não encontrado"):
            _provider().complete("s", "u", {"type": "object"})


# ── contagem de tokens ───────────────────────────────────────────────────────

def test_usage_soma_cache_read_e_cache_creation_no_input():
    usage = {
        "input_tokens": 100,
        "cache_read_input_tokens": 900,
        "cache_creation_input_tokens": 50,
        "output_tokens": 30,
    }
    with patch("app.llm.api_provider.httpx.post", return_value=_resp(usage=usage)):
        _, u = _provider().complete_text("s", "u")
    assert u.input_tokens == 1050  # token cacheado foi pago, entra na conta
    assert u.output_tokens == 30
    assert u.total == 1080


def test_usage_ausente_nao_quebra():
    with patch("app.llm.api_provider.httpx.post", return_value=_resp(usage={})):
        _, u = _provider().complete_text("s", "u")
    assert u.input_tokens == 0 and u.output_tokens == 0


# ── cache do system ──────────────────────────────────────────────────────────

def test_system_curto_vai_como_string_sem_pedir_cache():
    # o _SYSTEM do analyzer tem ~490 tokens: abaixo do piso, pedir cache é ruído
    with patch("app.llm.api_provider.httpx.post", return_value=_resp()) as post:
        _provider(cache_system=True).complete_text("system curtinho", "u")
    assert post.call_args.kwargs["json"]["system"] == "system curtinho"


def test_system_longo_com_cache_ligado_pede_ephemeral():
    longo = "x" * 8000  # ~2.100 tokens, acima do piso
    with patch("app.llm.api_provider.httpx.post", return_value=_resp()) as post:
        _provider(cache_system=True).complete_text(longo, "u")
    bloco = post.call_args.kwargs["json"]["system"]
    assert bloco[0]["cache_control"] == {"type": "ephemeral"}


def test_cache_desligado_e_o_padrao():
    longo = "x" * 8000
    with patch("app.llm.api_provider.httpx.post", return_value=_resp()) as post:
        _provider().complete_text(longo, "u")
    assert isinstance(post.call_args.kwargs["json"]["system"], str)


# ── resposta vazia ───────────────────────────────────────────────────────────

def test_resposta_sem_texto_e_transitoria():
    m = MagicMock()
    m.status_code = 200
    m.json.return_value = {"content": [], "usage": {}}
    with patch("app.llm.api_provider.httpx.post", return_value=m):
        with pytest.raises(AnthropicApiError) as exc:
            _provider()._chamar("s", "u", tentativa=1)
    assert exc.value.transient is True
