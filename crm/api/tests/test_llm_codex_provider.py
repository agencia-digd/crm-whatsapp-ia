"""
Testes do CodexProvider e do FallbackProvider — subprocess mockado.

Cobre o que custa caro se quebrar em produção:
  * as flags obrigatórias do codex CLI (gotchas que já queimaram tempo);
  * stdin fechado (senão o CLI trava esperando input);
  * telemetria do CLI não vazar pro texto que a corretora lê;
  * falha permanente (auth/quota) não virar retry;
  * o fallback pro Claude manter a operação de pé.
"""
import json
import subprocess
import time
from unittest.mock import MagicMock, patch

import pytest

from app.llm.codex_provider import (
    CodexProvider,
    CodexProviderError,
    FallbackProvider,
    _classify_stderr,
    _strip_preamble,
)


def _proc(stdout: str = "", stderr: str = "", rc: int = 0) -> MagicMock:
    m = MagicMock()
    m.stdout = stdout
    m.stderr = stderr
    m.returncode = rc
    return m


# ── argv: os gotchas do codex CLI ─────────────────────────────────────────────

class TestComandoLocal:
    def test_flags_obrigatorias(self) -> None:
        """--skip-git-repo-check e -m <model> precisam estar no argv.

        Sem a primeira, o CLI aborta com 'Not inside a trusted directory'.
        """
        cmd = CodexProvider(model="gpt-5.6-terra")._cmd("oi")
        assert cmd[:3] == ["codex", "exec", "--skip-git-repo-check"]
        assert "-m" in cmd and cmd[cmd.index("-m") + 1] == "gpt-5.6-terra"
        assert cmd[-1] == "oi"  # prompt é posicional, não stdin

    def test_stdin_fechado(self) -> None:
        """stdin=DEVNULL: sem isso o codex trava em 'Reading additional input'."""
        with patch("subprocess.run", return_value=_proc("resposta")) as run:
            CodexProvider().complete_text("s", "u")
        assert run.call_args.kwargs["stdin"] is subprocess.DEVNULL

    def test_timeout_repassado(self) -> None:
        with patch("subprocess.run", return_value=_proc("ok")) as run:
            CodexProvider(timeout=42).complete_text("s", "u")
        assert run.call_args.kwargs["timeout"] == 42


class TestComandoRemoto:
    def test_ssh_usa_dash_n(self) -> None:
        """ssh -n fecha o stdin remoto — mesmo motivo do DEVNULL local."""
        cmd = CodexProvider(remote_host="contato@example.com")._cmd("oi")
        assert cmd[0] == "ssh"
        assert "-n" in cmd
        assert "contato@example.com" in cmd

    def test_prompt_com_aspas_e_acento_nao_estilhaca(self) -> None:
        """Prompt real tem aspas, acento e quebra de linha. O shell REMOTO tem
        que reconstruir exatamente o mesmo argumento — senão o prompt chega
        picado e o modelo responde outra coisa. Validado com shlex (mesmo
        parsing do sh), não por inspeção de string."""
        import shlex

        prompt = "diga 'oi' e \"tchau\"\nlead: João D'Ávila"
        cmd = CodexProvider(remote_host="h")._cmd(prompt)
        remoto = cmd[-1]
        assert remoto.startswith("cd /tmp && ")
        argv = shlex.split(remoto[len("cd /tmp && "):])
        assert argv[0] == "codex" and argv[1] == "exec"
        assert argv[-1] == prompt  # chegou inteiro, com aspas e acento


# ── saída ─────────────────────────────────────────────────────────────────────

class TestStripPreamble:
    def test_remove_telemetria(self) -> None:
        raw = "tokens used\n2.534\nOi, tudo bem?"
        assert "tokens used" not in _strip_preamble(raw)
        assert "Oi, tudo bem?" in _strip_preamble(raw)

    def test_preserva_texto_normal(self) -> None:
        assert _strip_preamble("Bom dia, Pessoa!") == "Bom dia, Pessoa!"

    def test_complete_text_nao_devolve_telemetria(self) -> None:
        """A corretora lê esse texto — telemetria do CLI não pode vazar."""
        with patch("subprocess.run", return_value=_proc("tokens used\n1.2\nOlá!")):
            texto, _ = CodexProvider().complete_text("s", "u")
        assert texto == "Olá!"


class TestComplete:
    def test_extrai_json_da_resposta(self) -> None:
        with patch("subprocess.run", return_value=_proc('tokens used\n{"temp": 8}')):
            raw, usage = CodexProvider().complete("s", "u", {"type": "object"})
        assert json.loads(raw)["temp"] == 8
        assert usage.total > 0

    def test_sem_json_tenta_duas_vezes_e_falha(self) -> None:
        with patch("subprocess.run", return_value=_proc("sem json aqui")) as run:
            with pytest.raises(RuntimeError, match="JSON não encontrado"):
                CodexProvider().complete("s", "u", {"type": "object"})
        assert run.call_count == 2


# ── classificação de erro ─────────────────────────────────────────────────────

class TestClassifyStderr:
    @pytest.mark.parametrize("stderr", [
        "401 Unauthorized",
        "Not logged in",
        "usage limit reached",
        "model is not supported when using Codex with a ChatGPT account",
    ])
    def test_permanente(self, stderr: str) -> None:
        assert _classify_stderr(stderr) is False

    @pytest.mark.parametrize("stderr", ["connection reset", "timeout", "502 bad gateway"])
    def test_transitorio(self, stderr: str) -> None:
        assert _classify_stderr(stderr) is True

    def test_erro_permanente_nao_retenta(self) -> None:
        """Auth/quota não melhora com retry — propaga na hora (e o log mostra a
        causa real, em vez de esconder atrás de 'JSON não encontrado')."""
        with patch("subprocess.run", return_value=_proc(stderr="401 Unauthorized", rc=1)) as run:
            with pytest.raises(CodexProviderError):
                CodexProvider().complete("s", "u", {"type": "object"})
        assert run.call_count == 1

    def test_erro_transitorio_retenta_uma_vez(self) -> None:
        falha = _proc(stderr="connection reset", rc=1)
        ok = _proc('{"x": 1}')
        with patch("subprocess.run", side_effect=[falha, ok]) as run:
            raw, _ = CodexProvider().complete("s", "u", {"type": "object"})
        assert json.loads(raw)["x"] == 1
        assert run.call_count == 2

    def test_timeout_vira_runtime_error(self) -> None:
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("codex", 5)):
            with pytest.raises(RuntimeError, match="timeout"):
                CodexProvider(timeout=5).complete_text("s", "u")


# ── fallback: a rede de proteção da operação ──────────────────────────────────

class _Fake:
    """Provider de mentira: responde fixo ou explode."""

    def __init__(self, resposta: str | None = None, erro: Exception | None = None) -> None:
        self.resposta = resposta
        self.erro = erro
        self.chamadas = 0

    def complete(self, system, user, json_schema=None):
        self.chamadas += 1
        if self.erro:
            raise self.erro
        return self.resposta, MagicMock()

    def complete_text(self, system, user):
        self.chamadas += 1
        if self.erro:
            raise self.erro
        return self.resposta, MagicMock()


class TestFallback:
    def test_primario_ok_nao_chama_reserva(self) -> None:
        prim, back = _Fake('{"a":1}'), _Fake('{"b":2}')
        raw, _ = FallbackProvider(prim, back).complete("s", "u")
        assert raw == '{"a":1}'
        assert back.chamadas == 0

    def test_primario_falha_cai_no_reserva(self) -> None:
        """Codex fora (login/cota/Mac) NÃO pode derrubar o sistema da cliente."""
        prim = _Fake(erro=CodexProviderError("401", returncode=1, transient=False))
        back = _Fake('{"b":2}')
        raw, _ = FallbackProvider(prim, back).complete("s", "u")
        assert raw == '{"b":2}'
        assert back.chamadas == 1

    def test_fallback_vale_pro_texto_livre(self) -> None:
        prim = _Fake(erro=RuntimeError("timeout"))
        back = _Fake("Bom dia!")
        texto, _ = FallbackProvider(prim, back).complete_text("s", "u")
        assert texto == "Bom dia!"

    def test_reserva_tambem_falha_propaga(self) -> None:
        """Se os dois caem, o erro tem que aparecer — não engolir em silêncio."""
        prim = _Fake(erro=RuntimeError("codex fora"))
        back = _Fake(erro=RuntimeError("claude fora"))
        with pytest.raises(RuntimeError, match="claude fora"):
            FallbackProvider(prim, back).complete_text("s", "u")


# ── fiação do config ──────────────────────────────────────────────────────────

class TestFactory:
    def test_provider_codex_vem_com_fallback(self) -> None:
        from app.config import _codex_com_fallback
        p = _codex_com_fallback("gpt-5.6-terra", "claude-sonnet-4-6")
        assert isinstance(p, FallbackProvider)
        assert isinstance(p.primary, CodexProvider)

    def test_modo_cli_tem_o_codex_de_reserva(self) -> None:
        """A rede na direção que importa: o Claude é o primário e roda na
        assinatura pessoal da Dona. Se a cota dela estoura, a operação da
        CLIENTE não pode parar junto (aconteceu em 28/07)."""
        from app.llm.cli_provider import ClaudeCliProvider

        from app.config import _claude_com_reserva
        p = _claude_com_reserva("claude-sonnet-4-6", "gpt-5.6-terra")
        assert isinstance(p, FallbackProvider)
        # Com MODELSTUDIO_API_KEY no .env há um 3º nível (Qwen) por fora —
        # 05/09/2026. O par Claude→codex continua sendo o miolo.
        base = p.primary if isinstance(p.primary, FallbackProvider) else p
        assert isinstance(base.primary, ClaudeCliProvider)
        assert isinstance(base.backup, CodexProvider)

    def test_reserva_do_cli_desligavel(self) -> None:
        """Pra provar num drill que a falha de cota realmente derruba."""
        from app.llm.cli_provider import ClaudeCliProvider

        from app.config import _claude_com_reserva, settings
        original = settings.cli_reserva_codex
        try:
            settings.cli_reserva_codex = False
            p = _claude_com_reserva("claude-sonnet-4-6", "gpt-5.6-terra")
            assert isinstance(p, ClaudeCliProvider)
        finally:
            settings.cli_reserva_codex = original

    def test_factory_cli_entrega_a_rede(self) -> None:
        """O caminho real de produção (llm_provider=cli), não só o helper.
        A sombra pode envelopar por fora — o que importa é que a rede exista."""
        from app.config import get_llm_provider, settings
        original = settings.llm_provider
        try:
            settings.llm_provider = "cli"
            p = get_llm_provider()
            # Desde 06/09/2026 a fábrica devolve tudo dentro do ContabilProvider
            # (grava o custo de cada chamada em llm_chamadas). Ele é a casca de
            # fora; a rede fica logo abaixo, ou abaixo da sombra se ela estiver
            # ligada.
            alvo = getattr(p, "_interno", p)
            if not isinstance(alvo, FallbackProvider):
                alvo = getattr(alvo, "primary", alvo)
            assert isinstance(alvo, FallbackProvider), f"sem rede: {type(p)}"
        finally:
            settings.llm_provider = original

    def test_fallback_desligavel(self) -> None:
        """Sem rede de proteção pra medir o Terra puro na sombra."""
        from app.config import _codex_com_fallback, settings
        original = settings.codex_fallback_claude
        try:
            settings.codex_fallback_claude = False
            p = _codex_com_fallback("gpt-5.6-terra", "claude-sonnet-4-6")
            assert isinstance(p, CodexProvider)
        finally:
            settings.codex_fallback_claude = original


# ── disjuntor (achado no ensaio de rollback, 28/07) ───────────────────────────
# Sem ele, cada análise paga a falha do primário antes de cair no reserva —
# medido 74s vs 14s. Com 220 análises/dia, o briefing das 8h atrasaria.

class TestDisjuntor:
    def test_abre_apos_n_falhas_e_pula_o_primario(self) -> None:
        prim = _Fake(erro=RuntimeError("codex fora"))
        back = _Fake("reserva")
        fp = FallbackProvider(prim, back, limite_falhas=3, pausa_s=300)

        for _ in range(3):
            fp.complete_text("s", "u")
        assert prim.chamadas == 3  # tentou 3x...

        for _ in range(5):
            texto, _ = fp.complete_text("s", "u")
            assert texto == "reserva"
        assert prim.chamadas == 3, "disjuntor aberto não pode chamar o primário"
        assert back.chamadas == 8  # operação nunca parou

    def test_sucesso_zera_o_contador(self) -> None:
        """Falha isolada não pode abrir o disjuntor."""
        class _Intermitente:
            def __init__(self) -> None:
                self.n = 0

            def complete_text(self, system, user):
                self.n += 1
                if self.n % 2:  # falha ímpar, sucesso par
                    raise RuntimeError("blip")
                return "primario", MagicMock()

        prim, back = _Intermitente(), _Fake("reserva")
        fp = FallbackProvider(prim, back, limite_falhas=3, pausa_s=300)
        for _ in range(10):
            fp.complete_text("s", "u")
        assert fp._aberto_ate == 0.0, "blip alternado não devia abrir o disjuntor"

    def test_passada_a_pausa_testa_o_primario_de_novo(self) -> None:
        """Codex voltou? O sistema tem que voltar sozinho, sem restart."""
        class _Recupera:
            def __init__(self) -> None:
                self.n = 0

            def complete_text(self, system, user):
                self.n += 1
                if self.n <= 3:
                    raise RuntimeError("fora")
                return "primario-voltou", MagicMock()

        prim, back = _Recupera(), _Fake("reserva")
        fp = FallbackProvider(prim, back, limite_falhas=3, pausa_s=300)
        for _ in range(3):
            fp.complete_text("s", "u")
        assert fp._aberto_ate > 0  # abriu

        fp._aberto_ate = time.monotonic() - 1  # simula pausa vencida
        texto, _ = fp.complete_text("s", "u")
        assert texto == "primario-voltou"
        assert fp._falhas == 0

    def test_disjuntor_nao_engole_falha_do_reserva(self) -> None:
        """Os dois fora = erro tem que aparecer, não silêncio."""
        fp = FallbackProvider(
            _Fake(erro=RuntimeError("codex fora")),
            _Fake(erro=RuntimeError("claude fora")),
            limite_falhas=2, pausa_s=300,
        )
        with pytest.raises(RuntimeError, match="claude fora"):
            fp.complete_text("s", "u")
