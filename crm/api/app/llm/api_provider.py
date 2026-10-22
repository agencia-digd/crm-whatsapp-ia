"""
AnthropicApiProvider — Claude via API oficial, billing por token.

POR QUE EXISTE (29/08/2026, medido):
Hoje `llm_provider="cli"` faz cada análise abrir uma sessão nova do
`claude -p`. Toda sessão do Claude Code carrega o contexto base antes de
olhar a conversa — system prompt do harness, CLAUDE.md, MEMORY.md e o
cardápio das 132 skills. Medição:

    a análise em si (tokens_analyzer no banco) ....  3.693 tokens
    o que o transcript registra por chamada ......  60.160 tokens
    pedágio do CLI ...............................  56.467 tokens (94%)

Ou seja: 94% do custo do copiloto é carregar contexto que a análise nunca
usa. Não é cache mal configurado — não há o que cachear, o `_SYSTEM` do
analyzer tem 490 tokens. O conserto é sair do CLI.

Este provider fala direto com a API, então o prompt é exatamente
system + conversa. Mesma qualidade de hoje (mesmo modelo Sonnet), sem o
pedágio, e o consumo passa a ser da OPERAÇÃO em vez da cota pessoal da
Dona — que hoje compete com o trabalho dela.

Segue o mesmo padrão do ModelStudioProvider: httpx puro, sem SDK novo,
chave só de settings, erro transitório separado de permanente.

Chave: `ANTHROPIC_API_KEY` no .env do serviço. Sem chave, levanta na
CHAMADA e nunca na construção — construir não pode derrubar o factory que
serve a produção.
"""
from __future__ import annotations

import json
import time
from typing import Any

import httpx
import structlog

from app.llm.cli_provider import _extract_json
from app.llm.provider import TokenUsage

log = structlog.get_logger()

BASE_URL = "https://api.anthropic.com/v1"
API_VERSION = "2023-06-01"

# Abaixo disso a Anthropic não cacheia prefixo, então pedir cache seria
# ruído: o _SYSTEM do analyzer tem ~490 tokens. Se um dia o system crescer
# além deste piso, `cache_system=True` passa a valer a pena.
MIN_TOKENS_PRA_CACHE = 1024


class AnthropicApiError(RuntimeError):
    """Erro da API. `transient=True` quando repetir pode resolver."""

    def __init__(self, msg: str, *, transient: bool) -> None:
        super().__init__(msg)
        self.transient = transient


class AnthropicApiProvider:
    """Implementa LLMProvider contra a Messages API da Anthropic."""

    def __init__(
        self,
        api_key: str,
        *,
        model: str = "claude-sonnet-5-5",
        base_url: str = BASE_URL,
        timeout: int = 120,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        cache_system: bool = False,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.cache_system = cache_system

    # ── API do LLMProvider ───────────────────────────────────────────────────

    def complete(
        self,
        system: str,
        user: str,
        json_schema: dict | None = None,
    ) -> tuple[str, TokenUsage]:
        user_msg = self._com_schema(user, json_schema)

        for tentativa in range(2):
            conteudo = user_msg if tentativa == 0 else self._reforcar(user_msg)
            try:
                saida, usage = self._chamar(system, conteudo, tentativa=tentativa + 1)
            except AnthropicApiError as exc:
                if exc.transient and tentativa == 0:
                    log.warning("anthropic_retry_transitorio", erro=str(exc)[:160])
                    continue
                raise

            if not json_schema:
                return saida, usage
            json_str = _extract_json(saida)
            if json_str is not None:
                return json_str, usage
            log.warning("anthropic_sem_json", tentativa=tentativa + 1, preview=saida[:200])

        raise RuntimeError("AnthropicApiProvider: JSON não encontrado após 2 tentativas")

    def complete_text(self, system: str, user: str) -> tuple[str, TokenUsage]:
        saida, usage = self._chamar(system, user, tentativa=1)
        return saida.strip(), usage

    # ── internals ────────────────────────────────────────────────────────────

    def _com_schema(self, user: str, json_schema: dict | None) -> str:
        if not json_schema:
            return user
        schema_str = json.dumps(json_schema, ensure_ascii=False, indent=2)
        return (
            f"{user}\n\n"
            "Responda APENAS com JSON válido conforme o schema abaixo.\n"
            "Não inclua texto antes ou depois do JSON.\n\n"
            f"{schema_str}"
        )

    def _reforcar(self, user: str) -> str:
        return (
            user
            + "\n\nATENÇÃO: Responda SOMENTE com o objeto JSON, "
            "sem explicação, sem markdown, sem texto adicional."
        )

    def _bloco_system(self, system: str) -> Any:
        """System como string simples, ou em bloco com cache quando valer a pena."""
        if not (self.cache_system and len(system) / 3.7 >= MIN_TOKENS_PRA_CACHE):
            return system
        return [{
            "type": "text",
            "text": system,
            "cache_control": {"type": "ephemeral"},
        }]

    def _chamar(self, system: str, user: str, *, tentativa: int) -> tuple[str, TokenUsage]:
        if not self.api_key:
            # Permanente de propósito: sem chave, nenhum retry ajuda.
            raise AnthropicApiError(
                "AnthropicApiProvider: ANTHROPIC_API_KEY vazia — "
                "configure no .env do copiloto-api antes de usar",
                transient=False,
            )

        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "system": self._bloco_system(system),
            "messages": [{"role": "user", "content": user}],
        }

        inicio = time.monotonic()
        try:
            r = httpx.post(
                f"{self.base_url}/messages",
                json=payload,
                # x-api-key nunca é logado — só o status volta pro log.
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": API_VERSION,
                    "content-type": "application/json",
                },
                timeout=self.timeout,
            )
        except httpx.TimeoutException as exc:
            raise AnthropicApiError(
                f"AnthropicApiProvider: timeout após {self.timeout}s", transient=True
            ) from exc
        except httpx.HTTPError as exc:
            raise AnthropicApiError(
                f"AnthropicApiProvider: erro de rede: {type(exc).__name__}", transient=True
            ) from exc

        dur = time.monotonic() - inicio

        # 429 e 5xx passam; 4xx de chave/payload não adianta repetir.
        if r.status_code == 429 or r.status_code >= 500:
            raise AnthropicApiError(
                f"AnthropicApiProvider: HTTP {r.status_code}", transient=True
            )
        if r.status_code >= 400:
            raise AnthropicApiError(
                f"AnthropicApiProvider: HTTP {r.status_code} — {r.text[:200]}",
                transient=False,
            )

        try:
            body = r.json()
            partes = [b.get("text", "") for b in body.get("content", []) if b.get("type") == "text"]
            saida = "".join(partes)
        except (ValueError, AttributeError, TypeError) as exc:
            raise AnthropicApiError(
                f"AnthropicApiProvider: resposta ilegível: {type(exc).__name__}",
                transient=False,
            ) from exc

        if not saida:
            raise AnthropicApiError(
                "AnthropicApiProvider: resposta sem texto", transient=True
            )

        u = body.get("usage") or {}
        # cache_read entra como input: é token que a gente pagou (mais barato,
        # mas pagou). Ignorar inflaria a economia na hora de comparar provider.
        entrada = (
            (u.get("input_tokens") or 0)
            + (u.get("cache_read_input_tokens") or 0)
            + (u.get("cache_creation_input_tokens") or 0)
        )
        usage = TokenUsage(input_tokens=entrada, output_tokens=u.get("output_tokens") or 0)

        log.info(
            "anthropic_ok",
            modelo=self.model,
            tentativa=tentativa,
            dur_s=round(dur, 2),
            tokens_in=usage.input_tokens,
            tokens_out=usage.output_tokens,
        )
        return saida, usage
