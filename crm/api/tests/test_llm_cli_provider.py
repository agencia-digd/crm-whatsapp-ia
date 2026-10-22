"""
Testes do ClaudeCliProvider — mock de subprocess para unit tests,
CLI real para integration tests.
"""
import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from app.llm.cli_provider import (
    ClaudeCliProvider,
    CliProviderError,
    _classify_stderr,
    _estimate_tokens,
    _extract_json,
)


# ── _extract_json ─────────────────────────────────────────────────────────────

class TestExtractJson:
    def test_json_puro(self) -> None:
        raw = '{"status": "ok", "valor": 42}'
        assert _extract_json(raw) == raw

    def test_json_com_texto_antes_e_depois(self) -> None:
        raw = 'Aqui está a resposta:\n{"x": 1}\nFim.'
        result = _extract_json(raw)
        assert result is not None
        assert json.loads(result) == {"x": 1}

    def test_json_em_bloco_markdown(self) -> None:
        raw = "```json\n{\"a\": true}\n```"
        result = _extract_json(raw)
        assert result is not None
        assert json.loads(result) == {"a": True}

    def test_json_aninhado(self) -> None:
        raw = '{"outer": {"inner": 1}}'
        result = _extract_json(raw)
        assert result is not None
        assert json.loads(result)["outer"]["inner"] == 1

    def test_sem_json_retorna_none(self) -> None:
        assert _extract_json("sem json aqui") is None
        assert _extract_json("") is None

    def test_json_malformado_retorna_none(self) -> None:
        assert _extract_json('{"chave": valor_invalido}') is None


# ── _estimate_tokens ──────────────────────────────────────────────────────────

class TestEstimateTokens:
    def test_basico(self) -> None:
        usage = _estimate_tokens("a" * 400, "b" * 200)
        assert usage.input_tokens == 100
        assert usage.output_tokens == 50
        assert usage.total == 150

    def test_minimo_1_token(self) -> None:
        usage = _estimate_tokens("a", "b")
        assert usage.input_tokens >= 1
        assert usage.output_tokens >= 1


# ── ClaudeCliProvider (mock subprocess) ──────────────────────────────────────

def _make_subprocess_result(stdout: str, returncode: int = 0, stderr: str = "") -> MagicMock:
    r = MagicMock()
    r.stdout = stdout
    r.stderr = stderr
    r.returncode = returncode
    return r


class TestClaudeCliProvider:
    def _provider(self) -> ClaudeCliProvider:
        return ClaudeCliProvider(timeout=30)

    def test_extracts_json_from_cli_output(self) -> None:
        payload = {"status": "atendido", "temperatura": "quente"}
        with patch("subprocess.run", return_value=_make_subprocess_result(json.dumps(payload))):
            raw, usage = self._provider().complete("system", "user")
        assert json.loads(raw) == payload
        assert usage.total > 0

    def test_json_com_texto_volta_limpo(self) -> None:
        payload = {"ok": True}
        stdout = f"Aqui está:\n{json.dumps(payload)}\nespero que ajude."
        with patch("subprocess.run", return_value=_make_subprocess_result(stdout)):
            raw, usage = self._provider().complete("sys", "user")
        assert json.loads(raw) == payload

    def test_retries_on_malformed_json(self) -> None:
        """Primeira chamada retorna texto sem JSON; segunda retorna JSON válido."""
        good = json.dumps({"result": 1})
        calls = [
            _make_subprocess_result("Não tenho JSON pra você."),
            _make_subprocess_result(good),
        ]
        with patch("subprocess.run", side_effect=calls):
            raw, _ = self._provider().complete("sys", "user")
        assert json.loads(raw) == {"result": 1}

    def test_two_failures_raises_runtime_error(self) -> None:
        with patch("subprocess.run", return_value=_make_subprocess_result("sem json")):
            with pytest.raises(RuntimeError, match="JSON não encontrado"):
                self._provider().complete("sys", "user")

    def test_timeout_raises_runtime_error(self) -> None:
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("claude", 30)):
            with pytest.raises(RuntimeError, match="timeout"):
                self._provider().complete("sys", "user")

    def test_schema_incluido_no_prompt(self) -> None:
        schema = {"campo": "descrição"}
        captured: list[str] = []

        def fake_run(args: list[str], **kwargs: object) -> MagicMock:
            captured.append(args[2])  # args = ["claude", "-p", prompt]
            return _make_subprocess_result(json.dumps({"campo": "valor"}))

        with patch("subprocess.run", side_effect=fake_run):
            self._provider().complete("sys", "user", json_schema=schema)

        assert "Responda APENAS com JSON válido" in captured[0]
        assert "campo" in captured[0]


# ── _classify_stderr ──────────────────────────────────────────────────────────

class TestClassifyStderr:
    def test_auth_e_permanente(self) -> None:
        assert _classify_stderr("Invalid API key provided") is False
        assert _classify_stderr("Error: not authenticated") is False
        assert _classify_stderr("Credit balance is too low") is False
        assert _classify_stderr("claude: command not found") is False

    def test_rede_e_transitorio(self) -> None:
        assert _classify_stderr("connection reset by peer") is True
        assert _classify_stderr("temporary failure in name resolution") is True
        assert _classify_stderr("") is True


# ── returncode != 0 → erro classificado ANTES de extrair JSON ────────────────

class TestReturncodeErro:
    def _provider(self) -> ClaudeCliProvider:
        return ClaudeCliProvider(timeout=30)

    def test_returncode_permanente_levanta_sem_retry(self) -> None:
        """Falha de auth (returncode != 0, stderr de auth): levanta
        CliProviderError transient=False na 1ª tentativa, SEM retry."""
        result = _make_subprocess_result("", returncode=1, stderr="Invalid API key")
        with patch("subprocess.run", return_value=result) as mock_run:
            with pytest.raises(CliProviderError) as ei:
                self._provider().complete("sys", "user")
        assert ei.value.transient is False
        assert ei.value.returncode == 1
        assert "Invalid API key" in str(ei.value)
        mock_run.assert_called_once()  # não retentou

    def test_returncode_transitorio_retenta_uma_vez(self) -> None:
        """Falha transitória (returncode != 0, stderr de rede) na 1ª tentativa,
        JSON válido na 2ª: retenta e retorna."""
        good = json.dumps({"result": 1})
        calls = [
            _make_subprocess_result("", returncode=1, stderr="connection reset by peer"),
            _make_subprocess_result(good, returncode=0),
        ]
        with patch("subprocess.run", side_effect=calls) as mock_run:
            raw, _ = self._provider().complete("sys", "user")
        assert json.loads(raw) == {"result": 1}
        assert mock_run.call_count == 2

    def test_returncode_transitorio_nas_duas_propaga(self) -> None:
        """Transitório nas 2 tentativas: propaga CliProviderError (não vira
        'JSON não encontrado' genérico)."""
        result = _make_subprocess_result("", returncode=1, stderr="connection reset")
        with patch("subprocess.run", return_value=result):
            with pytest.raises(CliProviderError) as ei:
                self._provider().complete("sys", "user")
        assert ei.value.transient is True

    def test_returncode_nao_extrai_json_de_stdout_de_falha(self) -> None:
        """Mesmo com JSON no stdout, se returncode != 0 e stderr é permanente,
        levanta o erro classificado em vez de retornar o JSON — a falha não
        fica mascarada."""
        stdout_com_json = json.dumps({"fantasma": True})
        result = _make_subprocess_result(stdout_com_json, returncode=2, stderr="unauthorized")
        with patch("subprocess.run", return_value=result):
            with pytest.raises(CliProviderError) as ei:
                self._provider().complete("sys", "user")
        assert ei.value.transient is False


# ── Integration (chama CLI real) ──────────────────────────────────────────────

@pytest.mark.integration
def test_cli_real_retorna_json() -> None:
    """Chama `claude -p` de verdade e verifica que JSON é extraído."""
    provider = ClaudeCliProvider(timeout=60)
    raw, usage = provider.complete(
        system="Responda apenas com JSON válido, sem texto adicional.",
        user='Retorne exatamente: {"ok": true, "numero": 42}',
        json_schema={"ok": "boolean", "numero": "integer"},
    )
    data = json.loads(raw)
    assert "ok" in data or "numero" in data  # Claude pode formatar ligeiramente diferente
    assert usage.total > 0


# ── modo magro (29/08/2026) ──────────────────────────────────────────────────
# O pedágio do `claude -p` era 94% do custo do copiloto: cada análise abria uma
# sessão nova carregando skills, MCP e memória que a análise nunca usa. Estes
# testes travam as duas correções — as flags, e o system saindo do prompt pra
# virar prefixo estável (que é o que faz o cache casar entre chamadas).

class TestModoMagro:
    def _cmd(self, mock_run):
        return mock_run.call_args[0][0]

    def test_magro_e_o_padrao(self):
        assert ClaudeCliProvider().magro is True

    def test_flags_magras_entram_no_comando(self):
        p = ClaudeCliProvider(model="claude-sonnet-4-6")
        with patch("app.llm.cli_provider.subprocess.run") as run:
            run.return_value = MagicMock(returncode=0, stdout='{"ok":1}', stderr="")
            p.complete("SISTEMA", "conversa", {"type": "object"})
        cmd = self._cmd(run)
        assert "--disable-slash-commands" in cmd
        assert "--strict-mcp-config" in cmd
        assert "--allowedTools" in cmd

    def test_system_vai_por_flag_e_sai_do_prompt(self):
        """O ponto todo: system como prefixo estável = cache reusado."""
        p = ClaudeCliProvider()
        with patch("app.llm.cli_provider.subprocess.run") as run:
            run.return_value = MagicMock(returncode=0, stdout='{"ok":1}', stderr="")
            p.complete("INSTRUCAO_DE_SISTEMA", "a conversa do lead", {"type": "object"})
        cmd = self._cmd(run)
        assert "--system-prompt" in cmd
        assert cmd[cmd.index("--system-prompt") + 1] == "INSTRUCAO_DE_SISTEMA"
        prompt = cmd[cmd.index("-p") + 1]
        assert "INSTRUCAO_DE_SISTEMA" not in prompt   # não pode estar nos dois
        assert "a conversa do lead" in prompt

    def test_prompt_nao_comeca_com_linha_em_branco(self):
        p = ClaudeCliProvider()
        with patch("app.llm.cli_provider.subprocess.run") as run:
            run.return_value = MagicMock(returncode=0, stdout='{"ok":1}', stderr="")
            p.complete("SIS", "conversa", None)
        prompt = self._cmd(run)[self._cmd(run).index("-p") + 1]
        assert not prompt.startswith("\n")

    def test_magro_desligado_volta_ao_comportamento_antigo(self):
        p = ClaudeCliProvider(magro=False)
        with patch("app.llm.cli_provider.subprocess.run") as run:
            run.return_value = MagicMock(returncode=0, stdout='{"ok":1}', stderr="")
            p.complete("SISTEMA", "conversa", None)
        cmd = self._cmd(run)
        assert "--disable-slash-commands" not in cmd
        assert "--system-prompt" not in cmd
        assert "SISTEMA" in cmd[cmd.index("-p") + 1]   # volta pra dentro do prompt

    def test_complete_text_tambem_usa_o_modo_magro(self):
        p = ClaudeCliProvider()
        with patch("app.llm.cli_provider.subprocess.run") as run:
            run.return_value = MagicMock(returncode=0, stdout="texto", stderr="")
            p.complete_text("SIS_TEXTO", "user")
        cmd = self._cmd(run)
        assert "--system-prompt" in cmd
        assert "SIS_TEXTO" not in cmd[cmd.index("-p") + 1]
