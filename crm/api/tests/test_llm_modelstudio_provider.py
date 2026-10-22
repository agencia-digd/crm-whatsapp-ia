"""
Testes do ModelStudioProvider (Qwen via Model Studio) — httpx mockado.

Cobre o que custa caro se quebrar em produção:
  * a chamada bate no endpoint CONFIGURADO, com o modelo pedido — não há
    endpoint default em código (a conta da casa é workspace dedicado);
  * a chave vai no header e NUNCA aparece em log/mensagem de erro;
  * sem chave/base_url o provider falha claro, e falha PERMANENTE;
  * 429/5xx é transitório (retry), 401/400 é permanente (propaga na hora);
  * `reasoning_content` (qwen3) é ignorado; só `content` vale;
  * JSON embrulhado em markdown ainda é extraído;
  * a contagem real de tokens da API é usada;
  * `verificar()` prova que o degrau responde (com cache) e classifica a falha;
  * a escada só sobe o degrau Qwen quando ele RESPONDE — "tem chave" não basta.
"""
import json
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.llm import modelstudio_provider as msp
from app.llm.modelstudio_provider import (
    ModelStudioError,
    ModelStudioProvider,
    StatusDegrau,
    limpar_cache_verificacao,
)

BASE = "https://ws-teste.us-east-1.maas.example/compatible-mode/v1"


def _resp(
    conteudo: str = "ok",
    *,
    status: int = 200,
    usage: dict | None = None,
    texto: str | None = None,
    reasoning: str | None = None,
) -> MagicMock:
    m = MagicMock()
    m.status_code = status
    msg: dict = {"role": "assistant", "content": conteudo}
    if reasoning is not None:
        msg["reasoning_content"] = reasoning
    corpo = {
        "choices": [{"message": msg}],
        "usage": usage if usage is not None else {"prompt_tokens": 120, "completion_tokens": 30},
    }
    m.json.return_value = corpo
    m.text = texto if texto is not None else json.dumps(corpo)
    return m


def _provider(**kw) -> ModelStudioProvider:
    kw.setdefault("model", "qwen3.8-max")
    kw.setdefault("base_url", BASE)
    return ModelStudioProvider("chave-secreta-123", **kw)


@pytest.fixture(autouse=True)
def _cache_limpo():
    limpar_cache_verificacao()
    yield
    limpar_cache_verificacao()


# ── a chamada HTTP ────────────────────────────────────────────────────────────

class TestChamada:
    def test_endpoint_e_modelo_vem_da_configuracao(self) -> None:
        with patch("httpx.post", return_value=_resp()) as post:
            _provider().complete_text("sistema", "usuario")
        assert post.call_args.args[0] == f"{BASE}/chat/completions"
        payload = post.call_args.kwargs["json"]
        assert payload["model"] == "qwen3.8-max"
        assert payload["messages"] == [
            {"role": "system", "content": "sistema"},
            {"role": "user", "content": "usuario"},
        ]

    def test_nao_existe_endpoint_default_em_codigo(self) -> None:
        """O DashScope público dava 401 pra chave da casa; default nenhum."""
        assert not hasattr(msp, "BASE_URL_INTL")
        with pytest.raises(TypeError):
            ModelStudioProvider("k")  # type: ignore[call-arg]

    def test_base_url_com_barra_final_normaliza(self) -> None:
        p = ModelStudioProvider("k", model="m", base_url="https://exemplo.test/v1/")
        with patch("httpx.post", return_value=_resp()) as post:
            p.complete_text("s", "u")
        assert post.call_args.args[0] == "https://exemplo.test/v1/chat/completions"

    def test_chave_vai_no_header_bearer(self) -> None:
        with patch("httpx.post", return_value=_resp()) as post:
            _provider().complete_text("s", "u")
        assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer chave-secreta-123"

    def test_timeout_repassado(self) -> None:
        with patch("httpx.post", return_value=_resp()) as post:
            _provider(timeout=42).complete_text("s", "u")
        assert post.call_args.kwargs["timeout"] == 42

    def test_thinking_desligado_por_padrao(self) -> None:
        """qwen3 raciocina e cobra por isso; a Pessoa só usa o content."""
        with patch("httpx.post", return_value=_resp()) as post:
            _provider().complete_text("s", "u")
        assert post.call_args.kwargs["json"]["enable_thinking"] is False
        with patch("httpx.post", return_value=_resp()) as post:
            _provider(enable_thinking=True).complete_text("s", "u")
        assert post.call_args.kwargs["json"]["enable_thinking"] is True

    def test_json_mode_so_quando_tem_schema(self) -> None:
        with patch("httpx.post", return_value=_resp()) as post:
            _provider().complete_text("s", "u")
        assert "response_format" not in post.call_args.kwargs["json"]

        with patch("httpx.post", return_value=_resp('{"a": 1}')) as post:
            _provider().complete("s", "u", {"type": "object"})
        assert post.call_args.kwargs["json"]["response_format"] == {"type": "json_object"}

    def test_schema_entra_no_prompt(self) -> None:
        with patch("httpx.post", return_value=_resp('{"score": 9}')) as post:
            _provider().complete("s", "avalie", {"score": "int"})
        user = post.call_args.kwargs["json"]["messages"][1]["content"]
        assert "avalie" in user and "score" in user


# ── configuração faltando ────────────────────────────────────────────────────

class TestConfigFaltando:
    def test_sem_chave_falha_claro_e_permanente(self) -> None:
        p = ModelStudioProvider("", model="m", base_url=BASE)
        with patch("httpx.post") as post:
            with pytest.raises(ModelStudioError) as exc:
                p.complete_text("s", "u")
        post.assert_not_called()
        assert exc.value.transient is False
        assert "MODELSTUDIO_API_KEY" in str(exc.value)

    def test_sem_base_url_falha_claro_e_permanente(self) -> None:
        p = ModelStudioProvider("k", model="m", base_url="")
        with patch("httpx.post") as post:
            with pytest.raises(ModelStudioError) as exc:
                p.complete_text("s", "u")
        post.assert_not_called()
        assert exc.value.transient is False
        assert "MODELSTUDIO_BASE_URL" in str(exc.value)

    def test_sem_modelo_falha_claro(self) -> None:
        p = ModelStudioProvider("k", model="", base_url=BASE)
        with pytest.raises(ModelStudioError, match="modelo vazio"):
            p.complete_text("s", "u")

    def test_chave_nao_vaza_na_mensagem_de_erro(self, caplog) -> None:
        r = _resp(status=401, texto='{"error": "invalid api key"}')
        with patch("httpx.post", return_value=r):
            with pytest.raises(ModelStudioError) as exc:
                _provider().complete_text("s", "u")
        assert "chave-secreta-123" not in str(exc.value)
        assert "chave-secreta-123" not in caplog.text


# ── falhas: o que vira retry e o que não vira ─────────────────────────────────

class TestFalhas:
    def test_401_e_permanente_nao_retenta(self) -> None:
        with patch("httpx.post", return_value=_resp(status=401)) as post:
            with pytest.raises(ModelStudioError) as exc:
                _provider().complete("s", "u", {"x": 1})
        assert exc.value.transient is False
        assert post.call_count == 1

    def test_429_e_transitorio_e_retenta_uma_vez(self) -> None:
        ok = _resp('{"ok": true}')
        with patch("httpx.post", side_effect=[_resp(status=429), ok]) as post:
            saida, _ = _provider().complete("s", "u", {"x": 1})
        assert post.call_count == 2
        assert json.loads(saida) == {"ok": True}

    def test_5xx_persistente_propaga(self) -> None:
        with patch("httpx.post", return_value=_resp(status=503)) as post:
            with pytest.raises(ModelStudioError):
                _provider().complete("s", "u", {"x": 1})
        assert post.call_count == 2

    def test_timeout_vira_erro_transitorio(self) -> None:
        with patch("httpx.post", side_effect=httpx.TimeoutException("estourou")):
            with pytest.raises(ModelStudioError) as exc:
                _provider(timeout=7).complete_text("s", "u")
        assert exc.value.transient is True
        assert "7s" in str(exc.value)

    def test_corpo_inesperado_nao_estoura_keyerror(self) -> None:
        r = MagicMock(status_code=200, text="<html>gateway</html>")
        r.json.return_value = {"sem": "choices"}
        with patch("httpx.post", return_value=r):
            with pytest.raises(ModelStudioError):
                _provider().complete_text("s", "u")


# ── parâmetro rejeitado pelo modelo (workspace multi-fornecedor) ─────────────

_KIMI_400 = ('{"error":{"code":"invalid_parameter_error","param":null,"message":'
             '"Parameter \'temperature\'=0.2 is not supported for kimi-k3 model.",'
             '"type":"invalid_request_error"}}')


class TestParamRejeitado:
    """Caso real de 06/09: kimi-k3 devolve 400 pra `temperature`. Não é o degrau
    morto — é o modelo dizendo "não uso isso". Tira e repete, e lembra."""

    def test_repete_sem_o_parametro_e_lembra(self) -> None:
        p = _provider(model="kimi-k3")
        with patch("httpx.post", side_effect=[_resp(status=400, texto=_KIMI_400), _resp("ok")]) as post:
            texto, _ = p.complete_text("s", "u")
        assert texto == "ok"
        assert post.call_count == 2
        assert "temperature" in post.call_args_list[0].kwargs["json"]
        assert "temperature" not in post.call_args_list[1].kwargs["json"]
        # próxima chamada já sai sem o parâmetro, sem pagar o 400 de novo
        with patch("httpx.post", return_value=_resp("ok")) as post:
            p.complete_text("s", "u")
        assert post.call_count == 1 and "temperature" not in post.call_args.kwargs["json"]

    def test_verificar_tambem_se_adapta(self) -> None:
        p = _provider(model="kimi-k3")
        with patch("httpx.post", side_effect=[_resp(status=400, texto=_KIMI_400), _resp("ok")]):
            st = p.verificar()
        assert st.ok

    def test_400_de_parametro_obrigatorio_continua_permanente(self) -> None:
        corpo = '{"error":{"code":"invalid_parameter_error","message":"Parameter \'messages\' is invalid"}}'
        with patch("httpx.post", return_value=_resp(status=400, texto=corpo)) as post:
            with pytest.raises(ModelStudioError) as exc:
                _provider().complete_text("s", "u")
        assert post.call_count == 1 and exc.value.transient is False

    def test_400_sem_nome_de_parametro_continua_permanente(self) -> None:
        with patch("httpx.post", return_value=_resp(status=400, texto='{"error":"bad"}')) as post:
            with pytest.raises(ModelStudioError):
                _provider().complete_text("s", "u")
        assert post.call_count == 1

    def test_extrator(self) -> None:
        assert msp._param_rejeitado(_KIMI_400) == "temperature"
        assert msp._param_rejeitado('{"error":"invalid_parameter_error: Parameter \'enable_thinking\' x"}') == "enable_thinking"
        assert msp._param_rejeitado("Incorrect API key provided") is None


# ── reasoning_content (qwen3) ────────────────────────────────────────────────

class TestReasoning:
    def test_reasoning_e_ignorado_e_content_vale(self) -> None:
        r = _resp('{"text": "Oi!", "tool_call": null}',
                  reasoning='O usuário quer {"text": "ERRADO"}')
        with patch("httpx.post", return_value=r):
            saida, _ = _provider().complete("s", "u", {"type": "object"})
        assert json.loads(saida) == {"text": "Oi!", "tool_call": None}

    def test_so_reasoning_sem_content_nao_parseia_o_raciocinio(self) -> None:
        """Content vazio + raciocínio: retenta com prompt reforçado, não inventa."""
        so_reasoning = _resp("", reasoning='pensando... {"text": "não é resposta"}')
        ok = _resp('{"text": "agora sim"}')
        with patch("httpx.post", side_effect=[so_reasoning, ok]) as post:
            saida, _ = _provider().complete("s", "u", {"type": "object"})
        assert post.call_count == 2
        assert json.loads(saida) == {"text": "agora sim"}

    def test_content_null_vira_vazio(self) -> None:
        r = _resp(None, reasoning="x")  # type: ignore[arg-type]
        with patch("httpx.post", return_value=r):
            texto, _ = _provider().complete_text("s", "u")
        assert texto == ""

    def test_content_multipart(self) -> None:
        r = MagicMock(status_code=200)
        r.json.return_value = {
            "choices": [{"message": {"content": [{"type": "text", "text": "a"},
                                                  {"type": "text", "text": "b"}]}}],
            "usage": {},
        }
        r.text = "{}"
        with patch("httpx.post", return_value=r):
            texto, _ = _provider().complete_text("s", "u")
        assert texto == "ab"


# ── JSON ──────────────────────────────────────────────────────────────────────

class TestJson:
    def test_extrai_json_embrulhado_em_markdown(self) -> None:
        bruto = 'Claro!\n```json\n{"interesse": "alto"}\n```\nEspero ter ajudado.'
        with patch("httpx.post", return_value=_resp(bruto)):
            saida, _ = _provider().complete("s", "u", {"interesse": "str"})
        assert json.loads(saida) == {"interesse": "alto"}

    def test_sem_json_retenta_com_prompt_reforcado_e_desiste(self) -> None:
        with patch("httpx.post", return_value=_resp("desculpa, não consigo")) as post:
            with pytest.raises(RuntimeError, match="JSON não encontrado"):
                _provider().complete("s", "u", {"x": 1})
        assert post.call_count == 2
        segundo = post.call_args.kwargs["json"]["messages"][1]["content"]
        assert "SOMENTE" in segundo

    def test_texto_livre_nao_exige_json(self) -> None:
        with patch("httpx.post", return_value=_resp("  Oi, tudo bem?  ")):
            texto, _ = _provider().complete_text("s", "u")
        assert texto == "Oi, tudo bem?"


# ── tokens (é o que vira a conta no fim do mês) ───────────────────────────────

class TestTokens:
    def test_usa_contagem_real_da_api(self) -> None:
        r = _resp("ok", usage={"prompt_tokens": 1234, "completion_tokens": 56})
        with patch("httpx.post", return_value=r):
            _, usage = _provider().complete_text("s", "u")
        assert (usage.input_tokens, usage.output_tokens) == (1234, 56)
        assert usage.total == 1290

    def test_sem_usage_cai_na_estimativa(self) -> None:
        with patch("httpx.post", return_value=_resp("resposta", usage={})):
            _, usage = _provider().complete_text("s", "u" * 400)
        assert usage.input_tokens == 100
        assert usage.output_tokens >= 1


# ── verificar(): prova real do degrau ────────────────────────────────────────

class TestVerificar:
    def test_ok_faz_uma_chamada_minima(self) -> None:
        with patch("httpx.post", return_value=_resp("ok")) as post:
            st = _provider().verificar(timeout=3)
        assert st == StatusDegrau(ok=True, motivo="respondeu 'ok'", duracao_ms=st.duracao_ms)
        payload = post.call_args.kwargs["json"]
        assert payload["max_tokens"] == 5
        assert payload["enable_thinking"] is False
        assert post.call_args.kwargs["timeout"] == 3

    def test_401_e_permanente(self) -> None:
        with patch("httpx.post", return_value=_resp(status=401, texto="invalid_api_key")):
            st = _provider().verificar()
        assert (st.ok, st.permanente) == (False, True)
        assert "401" in st.motivo

    def test_404_modelo_errado_e_permanente(self) -> None:
        with patch("httpx.post", return_value=_resp(status=404, texto="model not found")):
            st = _provider().verificar()
        assert (st.ok, st.permanente) == (False, True)

    def test_rede_e_transitoria(self) -> None:
        with patch("httpx.post", side_effect=httpx.ConnectError("down")):
            st = _provider().verificar()
        assert (st.ok, st.permanente) == (False, False)

    def test_config_vazia_e_permanente_sem_rede(self) -> None:
        with patch("httpx.post") as post:
            st = ModelStudioProvider("", model="m", base_url="").verificar()
        post.assert_not_called()
        assert st.permanente and "MODELSTUDIO_API_KEY" in st.motivo
        assert "MODELSTUDIO_BASE_URL" in st.motivo

    def test_nunca_levanta(self) -> None:
        with patch("httpx.post", side_effect=ValueError("bizarro")):
            st = _provider().verificar()
        assert st.ok is False and st.permanente is False

    def test_cache_por_chave_endpoint_modelo(self) -> None:
        with patch("httpx.post", return_value=_resp("ok")) as post:
            _provider().verificar()
            _provider().verificar()
            assert post.call_count == 1  # segunda vem do cache
            _provider(model="outro").verificar()
            assert post.call_count == 2  # modelo diferente = revalida
            ModelStudioProvider("outra-chave", model="qwen3.8-max", base_url=BASE).verificar()
            assert post.call_count == 3  # chave diferente = revalida

    def test_cache_de_falha_expira_antes_do_de_sucesso(self, monkeypatch) -> None:
        relogio = [1000.0]
        monkeypatch.setattr(msp.time, "monotonic", lambda: relogio[0])
        with patch("httpx.post", return_value=_resp(status=503)) as post:
            _provider().verificar()
            relogio[0] += msp.VERIFICACAO_TTL_FALHA_S - 1
            _provider().verificar()
            assert post.call_count == 1
            relogio[0] += 2
            _provider().verificar()
            assert post.call_count == 2

    def test_usar_cache_false_forca(self) -> None:
        with patch("httpx.post", return_value=_resp("ok")) as post:
            _provider().verificar()
            _provider().verificar(usar_cache=False)
        assert post.call_count == 2

    def test_chave_nao_aparece_no_motivo(self) -> None:
        with patch("httpx.post", return_value=_resp(status=401, texto="nope")):
            st = _provider().verificar()
        assert "chave-secreta-123" not in st.motivo


# ── fiação: config → provider ─────────────────────────────────────────────────

@pytest.fixture
def cfg(monkeypatch, tmp_path):
    """Settings isolado do .env real: sem chave, sem arquivo de fallback."""
    from app.config import settings

    monkeypatch.setattr(settings, "modelstudio_api_key", "")
    monkeypatch.setattr(settings, "modelstudio_base_url", "")
    monkeypatch.setattr(settings, "modelstudio_model_analyzer", "")
    monkeypatch.setattr(settings, "modelstudio_model_classifier", "")
    monkeypatch.setattr(settings, "modelstudio_model_reserva", "")
    monkeypatch.setattr(settings, "qwen_env_file", str(tmp_path / "nao-existe.env"))
    monkeypatch.setattr(settings, "modelstudio_verificar", True)
    monkeypatch.setattr(settings, "modelstudio_fallback_claude", True)
    monkeypatch.setattr(settings, "shadow_llm_provider", "codex")
    monkeypatch.setattr(settings, "classifier_provider", "")
    monkeypatch.setattr(settings, "cli_reserva_modelstudio", True)
    monkeypatch.setattr(settings, "cli_reserva_codex", True)
    monkeypatch.setattr(settings, "llm_provider", "cli")
    monkeypatch.setattr(settings, "shadow_llm", False)
    return settings


def _env_file(tmp_path, **kv) -> str:
    p = tmp_path / "qwen.env"
    p.write_text("# comentário\n" + "".join(f"{k}={v}\n" for k, v in kv.items()))
    return str(p)


class TestQwenConfig:
    def test_defaults_vazios_sem_endpoint_em_codigo(self) -> None:
        from app.config import Settings
        campos = Settings.model_fields
        assert campos["modelstudio_base_url"].default == ""
        assert campos["modelstudio_model_analyzer"].default == ""
        assert campos["modelstudio_api_key"].default == ""
        assert campos["qwen_env_file"].default == "/etc/agencia/qwen.env"
        assert campos["llm_provider"].default == "cli"

    def test_env_tem_prioridade(self, cfg) -> None:
        from app.config import _qwen_config
        cfg.modelstudio_api_key = "k-env"
        cfg.modelstudio_base_url = BASE
        cfg.modelstudio_model_analyzer = "m-env"
        c = _qwen_config()
        assert (c.api_key, c.base_url, c.model, c.origem) == ("k-env", BASE, "m-env", "env")

    def test_cai_no_arquivo_quando_env_vazio(self, cfg, tmp_path) -> None:
        from app.config import _qwen_config
        cfg.qwen_env_file = _env_file(
            tmp_path, QWEN_API_KEY='"k-arq"', QWEN_BASE_URL=BASE, QWEN_MODEL="qwen3.8-max"
        )
        c = _qwen_config()
        assert (c.api_key, c.base_url, c.model, c.origem) == ("k-arq", BASE, "qwen3.8-max", "arquivo")

    def test_misto_campo_a_campo(self, cfg, tmp_path) -> None:
        from app.config import _qwen_config
        cfg.modelstudio_api_key = "k-env"
        cfg.qwen_env_file = _env_file(tmp_path, QWEN_BASE_URL=BASE, QWEN_MODEL="qwen3.8-max")
        c = _qwen_config()
        assert (c.api_key, c.base_url, c.model, c.origem) == ("k-env", BASE, "qwen3.8-max", "misto")

    def test_modelo_explicito_ganha(self, cfg, tmp_path) -> None:
        from app.config import _qwen_config
        cfg.qwen_env_file = _env_file(tmp_path, QWEN_MODEL="qwen3.8-max")
        assert _qwen_config("qwen-plus").model == "qwen-plus"

    def test_arquivo_ausente_nao_quebra(self, cfg) -> None:
        from app.config import _qwen_config
        c = _qwen_config()
        assert (c.api_key, c.base_url, c.model) == ("", "", "")


class TestEscada:
    """Claude → codex → Qwen: o 3º degrau só entra se RESPONDER."""

    @staticmethod
    def _escada():
        from app.config import _claude_com_reserva
        return _claude_com_reserva("claude-sonnet-4-6", "gpt-5.6-terra")

    @staticmethod
    def _preencher(cfg) -> None:
        cfg.modelstudio_api_key = "k"
        cfg.modelstudio_base_url = BASE
        cfg.modelstudio_model_analyzer = "qwen3.8-max"

    def test_sem_config_fica_em_dois_niveis(self, cfg, caplog) -> None:
        from app.llm.codex_provider import CodexProvider, FallbackProvider
        with patch("httpx.post") as post:
            prov = self._escada()
        post.assert_not_called()
        assert isinstance(prov, FallbackProvider) and isinstance(prov.backup, CodexProvider)
        assert "llm_degrau_qwen_sem_config" in caplog.text

    def test_chave_existir_nao_basta_401_exclui_o_degrau(self, cfg, caplog) -> None:
        """O falso conforto de 05/09: chave no .env, endpoint errado, degrau morto."""
        from app.llm.codex_provider import CodexProvider, FallbackProvider
        self._preencher(cfg)
        with patch("httpx.post", return_value=_resp(status=401, texto="invalid_api_key")):
            prov = self._escada()
        assert isinstance(prov, FallbackProvider) and isinstance(prov.backup, CodexProvider)
        assert "llm_degrau_qwen_indisponivel" in caplog.text
        assert "EXCLUÍDO" in caplog.text
        assert "Bearer" not in caplog.text

    def test_responde_entra_como_terceiro_degrau(self, cfg, caplog) -> None:
        import logging
        from app.llm.codex_provider import FallbackProvider
        caplog.set_level(logging.INFO, logger="app.config")
        self._preencher(cfg)
        with patch("httpx.post", return_value=_resp("ok")):
            prov = self._escada()
        assert isinstance(prov, FallbackProvider)
        assert isinstance(prov.backup, ModelStudioProvider)
        assert prov.backup.model == "qwen3.8-max" and prov.backup.base_url == BASE
        assert isinstance(prov.primary, FallbackProvider)  # Claude → codex por dentro
        assert "llm_degrau_qwen_ok" in caplog.text

    def test_falha_transitoria_mantem_o_degrau(self, cfg, caplog) -> None:
        from app.llm.codex_provider import FallbackProvider
        self._preencher(cfg)
        with patch("httpx.post", side_effect=httpx.ConnectError("rede")):
            prov = self._escada()
        assert isinstance(prov, FallbackProvider)
        assert isinstance(prov.backup, ModelStudioProvider)
        assert "llm_degrau_qwen_instavel" in caplog.text

    def test_verificacao_cacheada_nao_bate_por_turno(self, cfg) -> None:
        """get_llm_provider() roda por turno da Pessoa — a prova é 1x por hora."""
        self._preencher(cfg)
        with patch("httpx.post", return_value=_resp("ok")) as post:
            for _ in range(5):
                self._escada()
        assert post.call_count == 1

    def test_verificacao_desligavel_pra_drill(self, cfg) -> None:
        self._preencher(cfg)
        cfg.modelstudio_verificar = False
        with patch("httpx.post") as post:
            prov = self._escada()
        post.assert_not_called()
        assert isinstance(prov.backup, ModelStudioProvider)

    def test_config_do_arquivo_sobe_o_degrau(self, cfg, tmp_path) -> None:
        cfg.qwen_env_file = _env_file(
            tmp_path, QWEN_API_KEY="k-arq", QWEN_BASE_URL=BASE, QWEN_MODEL="qwen3.8-max"
        )
        with patch("httpx.post", return_value=_resp("ok")) as post:
            prov = self._escada()
        assert isinstance(prov.backup, ModelStudioProvider)
        assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer k-arq"
        assert post.call_args.args[0] == f"{BASE}/chat/completions"

    # ── reserva no MESMO endpoint (workspace serve Qwen, Kimi, DeepSeek, GLM) ──

    def test_reserva_no_mesmo_endpoint_vira_fallback_dentro_do_degrau(self, cfg) -> None:
        from app.llm.codex_provider import FallbackProvider
        self._preencher(cfg)
        cfg.modelstudio_model_reserva = "kimi-k3"
        with patch("httpx.post", return_value=_resp("ok")) as post:
            prov = self._escada()
        degrau = prov.backup
        assert isinstance(degrau, FallbackProvider)
        assert isinstance(degrau.primary, ModelStudioProvider) and degrau.primary.model == "qwen3.8-max"
        assert isinstance(degrau.backup, ModelStudioProvider) and degrau.backup.model == "kimi-k3"
        # mesma chave, mesmo endpoint, só o nome do modelo muda
        assert degrau.primary.api_key == degrau.backup.api_key == "k"
        assert degrau.primary.base_url == degrau.backup.base_url == BASE
        assert post.call_count == 2  # verificou os dois

    def test_principal_morto_sobe_so_com_a_reserva(self, cfg, caplog) -> None:
        self._preencher(cfg)
        cfg.modelstudio_model_reserva = "kimi-k3"

        def _por_modelo(url, json, **k):
            return _resp(status=404, texto="model not found") if json["model"] == "qwen3.8-max" else _resp("ok")

        with patch("httpx.post", side_effect=_por_modelo):
            prov = self._escada()
        assert isinstance(prov.backup, ModelStudioProvider) and prov.backup.model == "kimi-k3"
        assert "papel=principal modelo=qwen3.8-max" in caplog.text and "EXCLUÍDO" in caplog.text

    def test_reserva_morta_sobe_so_com_o_principal(self, cfg) -> None:
        self._preencher(cfg)
        cfg.modelstudio_model_reserva = "modelo-que-nao-existe"

        def _por_modelo(url, json, **k):
            return _resp("ok") if json["model"] == "qwen3.8-max" else _resp(status=404, texto="nope")

        with patch("httpx.post", side_effect=_por_modelo):
            prov = self._escada()
        assert isinstance(prov.backup, ModelStudioProvider) and prov.backup.model == "qwen3.8-max"

    def test_os_dois_mortos_escada_em_dois_niveis(self, cfg, caplog) -> None:
        from app.llm.codex_provider import CodexProvider
        self._preencher(cfg)
        cfg.modelstudio_model_reserva = "kimi-k3"
        with patch("httpx.post", return_value=_resp(status=401, texto="invalid_api_key")):
            prov = self._escada()
        assert isinstance(prov.backup, CodexProvider)
        assert "llm_degrau_qwen_fora" in caplog.text

    def test_reserva_igual_ao_principal_e_ignorada(self, cfg) -> None:
        self._preencher(cfg)
        cfg.modelstudio_model_reserva = "qwen3.8-max"
        with patch("httpx.post", return_value=_resp("ok")) as post:
            prov = self._escada()
        assert isinstance(prov.backup, ModelStudioProvider)
        assert post.call_count == 1

    def test_reserva_do_arquivo(self, cfg, tmp_path) -> None:
        from app.config import _qwen_config
        cfg.qwen_env_file = _env_file(
            tmp_path, QWEN_API_KEY="k", QWEN_BASE_URL=BASE, QWEN_MODEL="qwen3.8-max",
            QWEN_MODEL_RESERVA="kimi-k3",
        )
        assert _qwen_config().model_reserva == "kimi-k3"

    def test_principal_qwen_falha_na_hora_e_a_reserva_responde(self, cfg) -> None:
        """Em runtime: limite do modelo principal → mesma chave, outro modelo."""
        self._preencher(cfg)
        cfg.modelstudio_model_reserva = "kimi-k3"
        resposta = json.dumps({"text": "Oi! Pra morar ou investir?", "tool_call": None})
        with patch("httpx.post", return_value=_resp("ok")):
            prov = self._escada()
        vistos: list[str] = []

        def _por_modelo(url, json, **k):
            vistos.append(json["model"])
            if json["model"] == "qwen3.8-max":
                return _resp(status=429, texto="model rate limit")
            return _resp(resposta)

        def _cai(*a, **k):
            raise RuntimeError("usage limit")

        with patch.object(prov.primary.primary, "complete", _cai), \
             patch.object(prov.primary.backup, "complete", _cai), \
             patch("httpx.post", side_effect=_por_modelo):
            json_str, _ = prov.complete("s", "u", {"type": "object"})
        assert json.loads(json_str)["text"].startswith("Oi!")
        assert vistos[-1] == "kimi-k3" and "qwen3.8-max" in vistos

    def test_escada_entrega_pelo_qwen_quando_os_dois_de_assinatura_caem(self, cfg) -> None:
        self._preencher(cfg)
        resposta = json.dumps({"text": "Oi! Pra morar ou investir?", "tool_call": None})
        with patch("httpx.post", return_value=_resp("ok")):
            prov = self._escada()

        def _cai(*a, **k):
            raise RuntimeError("usage limit")

        with patch.object(prov.primary.primary, "complete", _cai), \
             patch.object(prov.primary.backup, "complete", _cai), \
             patch("httpx.post", return_value=_resp(resposta)):
            json_str, _ = prov.complete("s", "u", {"type": "object"})
        assert json.loads(json_str)["text"].startswith("Oi!")


class TestFiacaoSombra:
    def test_sombra_de_codex_continua_o_default(self) -> None:
        from app.config import Settings
        assert Settings.model_fields["shadow_llm_provider"].default == "codex"

    def test_candidato_modelstudio_quando_pedido(self, cfg) -> None:
        from app.config import _candidato_sombra
        cfg.shadow_llm_provider = "modelstudio"
        cfg.modelstudio_api_key = "k"
        cfg.modelstudio_base_url = BASE
        cfg.shadow_llm_model = "qwen-max"
        c = _candidato_sombra()
        assert isinstance(c, ModelStudioProvider)
        assert c.model == "qwen-max" and c.base_url == BASE

    def test_sem_chave_a_sombra_nao_sobe(self, cfg) -> None:
        from app.config import _envelopar_sombra
        from app.llm.cli_provider import ClaudeCliProvider
        from app.llm.shadow_provider import ShadowProvider
        cfg.shadow_llm = True
        cfg.shadow_llm_provider = "modelstudio"
        p = _envelopar_sombra(ClaudeCliProvider(model="x"))
        assert not isinstance(p, ShadowProvider)

    def test_com_chave_envelopa_e_producao_continua_no_primario(self, cfg) -> None:
        from app.config import _envelopar_sombra
        from app.llm.cli_provider import ClaudeCliProvider
        from app.llm.shadow_provider import ShadowProvider
        cfg.shadow_llm = True
        cfg.shadow_llm_provider = "modelstudio"
        cfg.modelstudio_api_key = "k"
        cfg.modelstudio_base_url = BASE
        primario = ClaudeCliProvider(model="x")
        p = _envelopar_sombra(primario)
        assert isinstance(p, ShadowProvider)
        assert p.primary is primario
        assert isinstance(p.shadow, ModelStudioProvider)


class TestFiacaoProducao:
    def test_provider_modelstudio_tem_rede_pro_claude(self, cfg) -> None:
        from app.config import _modelstudio_com_fallback
        from app.llm.codex_provider import FallbackProvider
        cfg.modelstudio_api_key = "k"
        cfg.modelstudio_base_url = BASE
        p = _modelstudio_com_fallback("qwen3.8-max")
        assert isinstance(p, FallbackProvider)
        assert isinstance(p.primary, ModelStudioProvider)
        assert p.primary.model == "qwen3.8-max"

    def test_fallback_desligavel_pra_medir_qwen_puro(self, cfg) -> None:
        from app.config import _modelstudio_com_fallback
        cfg.modelstudio_api_key = "k"
        cfg.modelstudio_fallback_claude = False
        assert isinstance(_modelstudio_com_fallback("qwen3.8-max"), ModelStudioProvider)

    def test_classifier_segue_o_provider_global(self, cfg) -> None:
        from app.config import get_classifier_provider
        from app.llm.codex_provider import FallbackProvider
        cfg.llm_provider = "modelstudio"
        cfg.classifier_provider = ""
        cfg.modelstudio_api_key = "k"
        cfg.modelstudio_model_classifier = "qwen-plus"
        p = get_classifier_provider()
        assert isinstance(p, FallbackProvider)
        assert p.primary.model == "qwen-plus"
