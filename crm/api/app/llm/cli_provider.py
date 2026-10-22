"""
ClaudeCliProvider — LLM via `claude -p` (Claude Code CLI).

Usa o plano Max da Dona (sem custo adicional de API no MVP).
Quando virar SaaS, trocar por AnthropicApiProvider.

Estimativa de tokens: chars / 4 (aproximação; CLI não retorna contagem real).
"""
import json
import subprocess
import time
from typing import Any

import structlog

from app.llm.provider import LLMProvider, TokenUsage

log = structlog.get_logger()

# Assinaturas de stderr que indicam falha PERMANENTE (auth/config/quota) —
# retry não resolve, só desperdiça. Qualquer outra falha com returncode != 0
# é tratada como transitória (rede, sobrecarga) e pode ser retentada.
_PERMANENT_ERROR_SIGNS = (
    "authentication",
    "not authenticated",
    "unauthorized",
    "invalid api key",
    "invalid_api_key",
    "no api key",
    "api key",
    "credit balance",
    "insufficient",
    "permission denied",
    "forbidden",
    "command not found",
    "not found: claude",
    "usage limit",
    "quota",
    # Cota da ASSINATURA (claude -p): "You've hit your session limit · resets 7pm"
    # e "...weekly limit...". Medido 19-20/10: sem isso virava "transitório", pagava
    # retry inútil e caía no Codex (que também estava no limite) — 3 linhas [error]
    # por conversa, e no timer do Aprendiz isso aparecia como "6 erros em 24 h".
    "session limit",
    "weekly limit",
    "hit your",
)


def e_limite_de_cota(mensagem: str | None) -> bool:
    """True se a mensagem de erro é cota/limite da assinatura (esperar resolve, retry não)."""
    low = (mensagem or "").lower()
    return any(s in low for s in ("session limit", "weekly limit", "usage limit", "hit your"))


class CliProviderError(RuntimeError):
    """Falha na execução do `claude` CLI (returncode != 0).

    `transient=True` quando a falha pode ser resolvida com retry (rede,
    sobrecarga); `False` para falhas permanentes (auth, config, quota).
    """

    def __init__(self, message: str, *, returncode: int, transient: bool) -> None:
        super().__init__(message)
        self.returncode = returncode
        self.transient = transient


def _classify_stderr(stderr: str, stdout: str = "") -> bool:
    """Retorna True se a saída indica falha transitória (retentável).

    Olha stderr E stdout: quando a cota do plano estoura, o `claude -p` sai
    com exit 1 e escreve o motivo ("Claude AI usage limit reached|<ts>") no
    STDOUT — o stderr só tem avisos de settings. Em 05/09/2026 isso fez cota
    estourada virar "transitório", ganhar retry inútil e cair no codex (que
    também estava sem cota) — e a Pessoa engoliu lead quente.
    """
    low = f"{stderr or ''}\n{stdout or ''}".lower()
    return not any(sign in low for sign in _PERMANENT_ERROR_SIGNS)


# Avisos que o CLI imprime no stderr em TODA chamada (regra de permissão mal
# escrita em .claude/settings.local.json do cwd). Não é erro — é ruído que
# enchia os 300 chars do log e escondia o stdout com a causa real.
_RUIDO_STDERR = ("permission deny rule",)


def _erro_relevante(stderr: str, stdout: str, limite: int = 300) -> str:
    """Resumo da falha pra log/exceção: stdout (onde o CLI põe o motivo) +
    linhas de stderr que não são ruído conhecido."""
    partes: list[str] = []
    saida = (stdout or "").strip()
    if saida:
        partes.append(f"stdout: {saida[:limite]}")
    linhas = [
        ln.strip() for ln in (stderr or "").splitlines()
        if ln.strip() and not any(r in ln.lower() for r in _RUIDO_STDERR)
    ]
    if linhas:
        partes.append("stderr: " + " | ".join(linhas)[:limite])
    return (" ".join(partes) or "(sem saída)")[: limite * 2]


class ClaudeCliProvider:
    """
    Implementa LLMProvider via subprocess `claude -p`.

    Combina system + user + schema num único prompt texto.
    Extrai o primeiro bloco JSON bem-formado da resposta.
    Retry 1x com prompt reforçado se JSON não encontrado.
    """

    def __init__(
        self,
        timeout: int = 120,
        model: str | None = None,
        magro: bool = True,
        setting_sources: str | None = None,
    ) -> None:
        self.timeout = timeout
        # Ex.: "project" pula settings/hooks do USUÁRIO (hook de prompt, plugins) — medido
        # 22/10 em chamada de "ok" com Haiku: 7,5 s -> ~5 s. None = comportamento antigo.
        self.setting_sources = setting_sources
        # Sem model explícito o CLI usa o default da CONTA (~/.claude/settings.json),
        # que muda quando a Dona troca de modelo no Claude Code — Inspector/Assistente
        # não podem herdar isso. Sempre pinar via get_*_provider().
        self.model = model
        # MODO MAGRO (29/08/2026, medido chamada a chamada com --output-format json):
        #
        #   claude -p "responda ok"                          47.446 tokens
        #   + --disable-slash-commands                       34.241
        #   + --strict-mcp-config + --allowedTools ""        27.891
        #   com system fixo e conversa variando              ~29.400 (13k escrita
        #                                                     + 15k LEITURA de cache)
        #
        # Duas coisas, e a segunda importa mais que a primeira:
        #
        # 1. As flags cortam o que a análise nunca usa — cardápio de 132 skills,
        #    servidores MCP, schema das ferramentas. O analyzer só transforma
        #    texto em JSON: não chama tool, não lê arquivo, não usa MCP.
        #
        # 2. O `system` sai do prompt e vai pra `--system-prompt`. Enfiado no -p
        #    junto da conversa, ele virava prefixo VARIÁVEL e o cache nunca casava
        #    — toda chamada reescrevia tudo. Como flag, vira prefixo ESTÁVEL entre
        #    chamadas: a 2ª em diante lê barato (0,30/mi) em vez de escrever
        #    (3,75/mi). Foi isso que a medição mostrou: cache_r 15k por chamada,
        #    que antes não existia.
        #
        # Desligar (magro=False) volta ao comportamento antigo, prompt único.
        self.magro = magro

    def complete(
        self,
        system: str,
        user: str,
        json_schema: dict | None = None,
    ) -> tuple[str, TokenUsage]:
        # No modo magro o system vai por flag (prefixo estável = cache barato);
        # senão entra no prompt, como sempre foi.
        sistema = system if self.magro else None
        prompt = self._build_prompt(
            "" if self.magro else system, user, json_schema
        )

        for attempt in range(2):
            effective_prompt = prompt if attempt == 0 else self._reinforce(prompt)
            # _run_cli levanta CliProviderError classificado se returncode != 0.
            # Falha permanente (auth/config) NÃO vira retry: propaga direto pra
            # causa raiz não ficar escondida atrás de "JSON não encontrado".
            # Só falha transitória (e apenas na 1ª tentativa) é retentada.
            try:
                output = self._run_cli(
                    effective_prompt, attempt=attempt + 1, sistema=sistema
                )
            except CliProviderError as exc:
                if exc.transient and attempt == 0:
                    log.warning("cli_provider_retry_transitorio", returncode=exc.returncode)
                    continue
                raise

            json_str = _extract_json(output)
            if json_str is not None:
                usage = _estimate_tokens(effective_prompt, output)
                return json_str, usage

            log.warning("cli_provider_no_json", attempt=attempt + 1, output_preview=output[:200])

        raise RuntimeError("ClaudeCliProvider: JSON não encontrado após 2 tentativas")

    def complete_text(
        self,
        system: str,
        user: str,
    ) -> tuple[str, TokenUsage]:
        """Chama CLI e retorna texto livre (sem extração de JSON)."""
        sistema = system if self.magro else None
        prompt = self._build_prompt(
            "" if self.magro else system, user, json_schema=None
        )
        output = self._run_cli(prompt, sistema=sistema)
        usage = _estimate_tokens(prompt, output)
        return output.strip(), usage

    # ── internals ────────────────────────────────────────────────────────────

    def _build_prompt(self, system: str, user: str, json_schema: dict | None) -> str:
        # No modo magro o system chega vazio (vai por flag) — sem o filtro,
        # o prompt começaria com duas quebras de linha à toa.
        parts = [p for p in (system, user) if p]
        if json_schema:
            schema_str = json.dumps(json_schema, ensure_ascii=False, indent=2)
            parts.append(
                f"Responda APENAS com JSON válido conforme o schema abaixo.\n"
                f"Não inclua texto antes ou depois do JSON.\n\n"
                f"{schema_str}"
            )
        return "\n\n".join(parts)

    def _reinforce(self, prompt: str) -> str:
        return (
            prompt
            + "\n\nATENÇÃO: Responda SOMENTE com o objeto JSON, "
            "sem explicação, sem markdown, sem texto adicional."
        )

    def _run_cli(
        self, prompt: str, attempt: int = 1, sistema: str | None = None
    ) -> str:
        start = time.monotonic()
        cmd = ["claude", "-p", prompt]
        if self.model:
            cmd += ["--model", self.model]
        if self.magro:
            cmd += [
                "--disable-slash-commands",      # tira o cardápio de skills
                "--strict-mcp-config",
                "--mcp-config", "{\"mcpServers\":{}}",
                "--allowedTools", "",            # o analyzer não chama tool
            ]
            if sistema:
                cmd += ["--system-prompt", sistema]
        if self.setting_sources:
            cmd += ["--setting-sources", self.setting_sources]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=self.timeout,
            )
            duration_ms = int((time.monotonic() - start) * 1000)
            stderr = result.stderr or ""
            log.info(
                "cli_provider_call",
                provider="cli",
                model=self.model or "(default CLI)",
                attempt=attempt,
                input_chars=len(prompt),
                output_chars=len(result.stdout),
                duration_ms=duration_ms,
                returncode=result.returncode,
            )
            # returncode != 0: a execução falhou. Levantar ANTES de tentar
            # extrair JSON — senão auth/config vira stdout vazio → "JSON não
            # encontrado" genérico, escondendo a causa real.
            if result.returncode != 0:
                transient = _classify_stderr(stderr, result.stdout or "")
                stderr_trunc = _erro_relevante(stderr, result.stdout or "")
                # Cota estourada é estado esperado (volta quando a janela reseta), não bug:
                # warning, pra não poluir o painel de erros do timer.
                emitir = (
                    log.warning
                    if e_limite_de_cota(f"{stderr} {result.stdout or ''}")
                    else log.error
                )
                emitir(
                    "cli_provider_returncode_erro",
                    returncode=result.returncode,
                    transient=transient,
                    stderr=stderr_trunc,
                )
                raise CliProviderError(
                    f"ClaudeCliProvider: exit {result.returncode} "
                    f"({'transitório' if transient else 'permanente'}): {stderr_trunc}",
                    returncode=result.returncode,
                    transient=transient,
                )
            return result.stdout
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"ClaudeCliProvider: timeout após {self.timeout}s"
            ) from exc


# ── module-level helpers (testáveis isoladamente) ────────────────────────────

def _extract_json(text: str) -> str | None:
    """Extrai o primeiro objeto JSON bem-formado de um texto livre."""
    depth = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                candidate = text[start : i + 1]
                try:
                    json.loads(candidate)
                    return candidate
                except json.JSONDecodeError:
                    start = -1
                    depth = 0
    return None


def _estimate_tokens(input_text: str, output_text: str) -> TokenUsage:
    """Estimativa grosseira: ~4 chars por token (sem tiktoken no MVP)."""
    return TokenUsage(
        input_tokens=max(1, len(input_text) // 4),
        output_tokens=max(1, len(output_text) // 4),
    )
