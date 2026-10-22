"""
OllamaProvider — LLM local via Ollama HTTP (Mac mini, Vpn).

Default model: qwen2.5:7b (rodando em http://100.64.0.1:11434).
Custo: zero. Latencia: ~200ms primeira chamada (com KEEP_ALIVE 30m),
~80ms subsequentes pra classificacao curta.

Uso pretendido: classificacao rapida no Inspector (substitui Haiku).
Sonnet continua na analise profunda via ClaudeCliProvider.
"""
import json
import time
from typing import Any

import httpx
import structlog

from app.llm.provider import LLMProvider, TokenUsage

log = structlog.get_logger()


class OllamaProvider:
    def __init__(
        self,
        host: str = "http://100.64.0.1:11434",
        model: str = "qwen2.5:7b",
        timeout: int = 60,
        temperature: float = 0.1,
    ) -> None:
        self.host = host.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.temperature = temperature

    def complete(
        self,
        system: str,
        user: str,
        json_schema: dict | None = None,
    ) -> tuple[str, TokenUsage]:
        prompt = self._build_prompt(system, user, json_schema)
        payload: dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": self.temperature},
        }
        if json_schema:
            payload["format"] = "json"

        output, usage = self._call(payload, attempt=1)
        if json_schema and not _is_valid_json(output):
            log.warning("ollama_invalid_json_retry", preview=output[:200])
            payload["prompt"] = self._reinforce(prompt)
            output, usage = self._call(payload, attempt=2)
            if not _is_valid_json(output):
                raise RuntimeError("OllamaProvider: JSON invalido apos 2 tentativas")
        return output, usage

    def complete_text(self, system: str, user: str) -> tuple[str, TokenUsage]:
        prompt = self._build_prompt(system, user, json_schema=None)
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": self.temperature},
        }
        output, usage = self._call(payload, attempt=1)
        return output.strip(), usage

    def _build_prompt(self, system: str, user: str, json_schema: dict | None) -> str:
        parts = [system, user]
        if json_schema:
            schema_str = json.dumps(json_schema, ensure_ascii=False, indent=2)
            parts.append(
                "Responda APENAS com JSON valido conforme o schema:\n" + schema_str
            )
        return "\n\n".join(parts)

    def _reinforce(self, prompt: str) -> str:
        return prompt + "\n\nIMPORTANTE: responda APENAS com o objeto JSON. Sem texto antes ou depois."

    def _call(self, payload: dict, attempt: int) -> tuple[str, TokenUsage]:
        url = f"{self.host}/api/generate"
        start = time.monotonic()
        try:
            r = httpx.post(url, json=payload, timeout=self.timeout)
            r.raise_for_status()
            data = r.json()
            duration_ms = int((time.monotonic() - start) * 1000)
            output = data.get("response", "")
            usage = TokenUsage(
                input_tokens=data.get("prompt_eval_count", 0) or max(1, len(payload["prompt"]) // 4),
                output_tokens=data.get("eval_count", 0) or max(1, len(output) // 4),
            )
            log.info(
                "ollama_provider_call",
                provider="ollama",
                model=self.model,
                attempt=attempt,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                duration_ms=duration_ms,
            )
            return output, usage
        except httpx.TimeoutException as exc:
            raise RuntimeError(f"OllamaProvider: timeout apos {self.timeout}s") from exc
        except httpx.HTTPError as exc:
            raise RuntimeError(f"OllamaProvider: HTTP {exc}") from exc


def _is_valid_json(text: str) -> bool:
    try:
        json.loads(text)
        return True
    except (json.JSONDecodeError, ValueError):
        return False


