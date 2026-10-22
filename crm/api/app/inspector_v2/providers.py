"""Adapters equivalentes para Codex, Grok CLI e Qwen 3.8 Max Token Plan."""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, cast

from app.inspector_v2.models import ProviderResponse, Usage


class ProviderError(RuntimeError):
    def __init__(self, message: str, *, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient
        self.attempts = 1


class ShadowProvider(ABC):
    name: str
    model: str

    @abstractmethod
    def complete(self, system: str, user: str, schema: dict[str, Any]) -> ProviderResponse:
        raise NotImplementedError

    @abstractmethod
    def preflight(self) -> dict[str, str | bool | None]:
        raise NotImplementedError

    def runtime_configuration(self) -> dict[str, str | int | bool | None]:
        return {"adapter": type(self).__name__, "model": self.model}


def _command_version(executable: str) -> str | None:
    try:
        completed = subprocess.run(
            [executable, "--version"], text=True, capture_output=True, timeout=10, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = (completed.stdout or completed.stderr).strip()
    return value[:160] or None


def strict_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Normaliza o mesmo contrato para transports que exigem strict schema."""
    normalized = cast(dict[str, Any], json.loads(json.dumps(schema)))

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            properties = node.get("properties")
            if isinstance(properties, dict):
                node["required"] = list(properties)
                node["additionalProperties"] = False
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(normalized)
    return normalized


def build_prompt(system: str, user: str, schema: dict[str, Any]) -> str:
    schema = strict_json_schema(schema)
    return (
        f"{system}\n\nTAREFA\n{user}\n\n"
        "Retorne somente um objeto JSON válido que respeite este JSON Schema:\n"
        f"{json.dumps(schema, ensure_ascii=False, separators=(',', ':'))}"
    )


def extract_json(text: str) -> str:
    decoder = json.JSONDecoder()
    for match in re.finditer(r"[\[{]", text):
        try:
            value, _ = decoder.raw_decode(text[match.start():])
        except json.JSONDecodeError:
            continue
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False)
    raise ProviderError("resposta não contém JSON válido")


def _token_usage_from_text(text: str) -> Usage:
    match = re.search(r"tokens used\s*[\r\n]+\s*([\d.,]+)", text, re.IGNORECASE)
    if not match:
        return Usage()
    total = int(re.sub(r"\D", "", match.group(1)))
    return Usage(total_tokens=total)


class CodexCliProvider(ShadowProvider):
    name = "codex"

    def __init__(self, model: str, timeout_seconds: int = 240) -> None:
        self.model = model
        self.timeout_seconds = timeout_seconds
        self._cli_version: str | None = None

    def preflight(self) -> dict[str, str | bool | None]:
        executable = shutil.which("codex")
        self._cli_version = _command_version(executable) if executable else None
        return {
            "available": executable is not None,
            "executable": executable,
            "model": self.model,
            "version": self._cli_version,
        }

    def runtime_configuration(self) -> dict[str, str | int | bool | None]:
        return {
            "adapter": type(self).__name__,
            "model": self.model,
            "timeout_seconds": self.timeout_seconds,
            "sandbox": "read-only",
            "cli_version": self._cli_version or _command_version("codex"),
        }

    def complete(self, system: str, user: str, schema: dict[str, Any]) -> ProviderResponse:
        if not shutil.which("codex"):
            raise ProviderError("codex CLI não encontrado")
        schema = strict_json_schema(schema)
        prompt = build_prompt(system, user, schema)
        with tempfile.TemporaryDirectory(prefix="acme-painel-codex-", dir="/tmp") as temp_dir:
            schema_path = Path(temp_dir) / "schema.json"
            output_path = Path(temp_dir) / "response.json"
            schema_path.write_text(json.dumps(schema, ensure_ascii=False), encoding="utf-8")
            cmd = [
                "codex", "exec", "--skip-git-repo-check", "--ephemeral", "--ignore-rules",
                "--sandbox", "read-only", "--color", "never", "-C", temp_dir,
                "--output-schema", str(schema_path),
                "--output-last-message", str(output_path), "-",
            ]
            if self.model != "default":
                cmd[cmd.index("--output-schema"):cmd.index("--output-schema")] = ["-m", self.model]
            started = time.monotonic()
            try:
                completed = subprocess.run(
                    cmd,
                    input=prompt,
                    text=True,
                    capture_output=True,
                    timeout=self.timeout_seconds,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise ProviderError("codex timeout", transient=True) from exc
            if completed.returncode != 0:
                error = completed.stderr.strip()[-500:]
                raise ProviderError(f"codex falhou ({completed.returncode}): {error}")
            content = output_path.read_text(encoding="utf-8") if output_path.exists() else completed.stdout
            return ProviderResponse(
                content=extract_json(content),
                usage=_token_usage_from_text(f"{completed.stdout}\n{completed.stderr}"),
                raw_metadata={"latency_ms": int((time.monotonic() - started) * 1000)},
            )


class GrokCliProvider(ShadowProvider):
    name = "grok"

    def __init__(self, model: str, timeout_seconds: int = 240, max_turns: int = 2) -> None:
        if max_turns != 2:
            raise ValueError("piloto fixa Grok em exatamente dois turnos de transporte")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_turns = max_turns
        self._cli_version: str | None = None

    def preflight(self) -> dict[str, str | bool | None]:
        executable = shutil.which("grok")
        self._cli_version = _command_version(executable) if executable else None
        return {
            "available": executable is not None,
            "executable": executable,
            "model": self.model,
            "version": self._cli_version,
            "max_turns": str(self.max_turns),
        }

    def runtime_configuration(self) -> dict[str, str | int | bool | None]:
        return {
            "adapter": type(self).__name__,
            "model": self.model,
            "timeout_seconds": self.timeout_seconds,
            "max_turns": self.max_turns,
            "web_search": False,
            "subagents": False,
            "memory": False,
            "cli_version": self._cli_version or _command_version("grok"),
        }

    def complete(self, system: str, user: str, schema: dict[str, Any]) -> ProviderResponse:
        if not shutil.which("grok"):
            raise ProviderError("grok CLI não encontrado")
        schema = strict_json_schema(schema)
        prompt = build_prompt(system, user, schema)
        with tempfile.TemporaryDirectory(prefix="acme-painel-grok-", dir="/tmp") as temp_dir:
            prompt_path = Path(temp_dir) / "prompt.txt"
            prompt_path.write_text(prompt, encoding="utf-8")
            cmd = [
                "grok", "--prompt-file", str(prompt_path), "--output-format", "json",
                "--json-schema", json.dumps(schema, ensure_ascii=False, separators=(",", ":")),
                "--disable-web-search", "--no-subagents", "--no-memory", "--no-plan",
                "--permission-mode", "plan", "--max-turns", str(self.max_turns), "--cwd", temp_dir,
            ]
            if self.model and self.model != "default":
                cmd.extend(["--model", self.model])
            started = time.monotonic()
            try:
                completed = subprocess.run(
                    cmd,
                    text=True,
                    capture_output=True,
                    timeout=self.timeout_seconds,
                    check=False,
                    stdin=subprocess.DEVNULL,
                )
            except subprocess.TimeoutExpired as exc:
                raise ProviderError("grok timeout", transient=True) from exc
            if completed.returncode != 0:
                error = completed.stderr.strip()[-500:]
                transient = "max turns reached" in error.lower()
                raise ProviderError(f"grok falhou ({completed.returncode}): {error}", transient=transient)
            raw = extract_json(completed.stdout)
            decoded = json.loads(raw)
            content_value = _unwrap_structured(decoded)
            return ProviderResponse(
                content=json.dumps(content_value, ensure_ascii=False),
                usage=_extract_usage(decoded),
                raw_metadata={"latency_ms": int((time.monotonic() - started) * 1000)},
            )


def _extract_usage(decoded: Any) -> Usage:
    if not isinstance(decoded, dict):
        return Usage()
    usage = decoded.get("usage")
    if not isinstance(usage, dict):
        return Usage()

    def integer(*keys: str) -> int | None:
        for key in keys:
            value = usage.get(key)
            if isinstance(value, int) and value >= 0:
                return value
        return None

    input_tokens = integer("input_tokens", "prompt_tokens")
    output_tokens = integer("output_tokens", "completion_tokens")
    total_tokens = integer("total_tokens")
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens
    return Usage(input_tokens=input_tokens, output_tokens=output_tokens, total_tokens=total_tokens)


def _unwrap_structured(decoded: Any) -> Any:
    """Encontra o payload funcional em envelopes JSON de CLIs distintos."""
    if isinstance(decoded, str):
        try:
            nested = json.loads(extract_json(decoded))
        except (ProviderError, json.JSONDecodeError):
            return decoded
        return _unwrap_structured(nested)
    if isinstance(decoded, dict):
        if "temperatura_atual" in decoded and "intencao_maxima" in decoded:
            return decoded
        for key in ("result", "response", "content", "output", "message", "text", "delta"):
            if key not in decoded:
                continue
            candidate = decoded[key]
            if isinstance(candidate, str):
                try:
                    candidate = json.loads(extract_json(candidate))
                except (ProviderError, json.JSONDecodeError):
                    continue
            found = _unwrap_structured(candidate)
            if isinstance(found, dict) and "temperatura_atual" in found:
                return found
        for value in reversed(list(decoded.values())):
            if isinstance(value, (dict, list, str)):
                found = _unwrap_structured(value)
                if isinstance(found, dict) and "temperatura_atual" in found:
                    return found
    elif isinstance(decoded, list):
        for item in reversed(decoded):
            found = _unwrap_structured(item)
            if isinstance(found, dict) and "temperatura_atual" in found:
                return found
    return decoded


def _shape_signature(value: Any, depth: int = 0) -> str:
    """Descreve envelopes sem registrar conteúdo potencialmente sensível."""
    if depth >= 4:
        return type(value).__name__
    if isinstance(value, dict):
        parts = [f"{key}:{_shape_signature(item, depth + 1)}" for key, item in value.items()]
        return "dict{" + ",".join(parts[:12]) + "}"
    if isinstance(value, list):
        samples = value[:2] + value[-1:] if len(value) > 2 else value
        return f"list[{len(value)}](" + ",".join(_shape_signature(item, depth + 1) for item in samples) + ")"
    if isinstance(value, str):
        try:
            nested = json.loads(extract_json(value))
        except (ProviderError, json.JSONDecodeError):
            return f"str[{len(value)}]"
        return f"str-json[{len(value)}]:" + _shape_signature(nested, depth + 1)
    return type(value).__name__


def _event_signature(value: Any) -> str:
    """Resumo sem conteúdo dos eventos de CLIs headless."""
    if not isinstance(value, list):
        return _shape_signature(value)
    events: list[str] = []
    for item in value:
        if not isinstance(item, dict):
            events.append(type(item).__name__)
            continue
        event_type = item.get("type")
        subtype = item.get("subtype")
        message = item.get("message")
        content_shapes: list[str] = []
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        content_shapes.append(type(block).__name__)
                        continue
                    block_type = str(block.get("type", "dict"))
                    text_value = block.get("text")
                    if isinstance(text_value, str):
                        markers = "keys" if "temperatura_atual" in text_value else "no-keys"
                        braces = "braces" if "{" in text_value else "no-braces"
                        lowered = text_value.lower()
                        classes = [
                            name
                            for name, words in (
                                ("asks-input", ("provide", "paste", "forneca", "envie o conteudo")),
                                ("refusal", ("cannot", "can't", "unable", "nao posso")),
                                ("mentions-stdin", ("stdin",)),
                                ("mentions-json", ("json",)),
                            )
                            if any(word in lowered for word in words)
                        ]
                        suffix = ";" + "+".join(classes) if classes else ""
                        block_type += f"[{len(text_value)};{markers};{braces}{suffix}]"
                    content_shapes.append(block_type)
            elif content is not None:
                content_shapes = [type(content).__name__]
        label = f"type={event_type!s}"
        if subtype is not None:
            label += f"/subtype={subtype!s}"
        if content_shapes:
            label += "/content=" + "+".join(content_shapes)
        events.append(label)
    return f"events[{len(value)}](" + ",".join(events) + ")"


class QwenTokenPlanProvider(ShadowProvider):
    """Qwen 3.8 Max oficial via wrapper Alibaba Token Plan na VPS."""

    name = "qwen"

    def __init__(
        self,
        model: str = "qwen3.8-max",
        ssh_host: str = "vps",
        remote_command: str = "/usr/local/bin/qwen-token-plan",
        timeout_seconds: int = 300,
    ) -> None:
        self.model = model
        self.ssh_host = ssh_host
        self.remote_command = remote_command
        self.timeout_seconds = timeout_seconds
        self._cli_version: str | None = None

    def preflight(self) -> dict[str, str | bool | None]:
        version_cmd = [
            "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
            self.ssh_host, f"{shlex.quote(self.remote_command)} --version",
        ]
        version = subprocess.run(version_cmd, text=True, capture_output=True, timeout=15, check=False)
        self._cli_version = version.stdout.strip()[:80] or None
        authenticated = False
        reason: str | None = None
        if version.returncode == 0:
            remote = shlex.join(
                [
                    self.remote_command,
                    "--safe-mode",
                    "-m",
                    self.model,
                    "-o",
                    "json",
                    "-p",
                    "Return only the exact word OK.",
                ]
            )
            probe = subprocess.run(
                ["ssh", "-T", "-o", "BatchMode=yes", self.ssh_host, remote],
                text=True,
                capture_output=True,
                timeout=30,
                check=False,
                stdin=subprocess.DEVNULL,
            )
            authenticated = probe.returncode == 0 and not _contains_api_error(probe.stdout)
            if not authenticated:
                reason = "authentication_probe_failed"
        else:
            reason = "wrapper_unavailable"
        return {
            "available": version.returncode == 0 and authenticated,
            "ssh_host": self.ssh_host,
            "remote_command": self.remote_command,
            "model": self.model,
            "version": self._cli_version,
            "authenticated": authenticated,
            "reason": reason,
        }

    def runtime_configuration(self) -> dict[str, str | int | bool | None]:
        return {
            "adapter": type(self).__name__,
            "model": self.model,
            "timeout_seconds": self.timeout_seconds,
            "ssh_host": self.ssh_host,
            "remote_command": self.remote_command,
            "safe_mode": True,
            "prompt_transport": "remote_argv_p",
            "cli_version": self._cli_version,
        }

    def complete(self, system: str, user: str, schema: dict[str, Any]) -> ProviderResponse:
        schema = strict_json_schema(schema)
        prompt = build_prompt(system, user, schema)
        remote = shlex.join(
            [
                self.remote_command,
                "--safe-mode",
                "-m",
                self.model,
                "-o",
                "json",
                "-p",
                prompt,
            ]
        )
        cmd = ["ssh", "-T", "-o", "BatchMode=yes", self.ssh_host, remote]
        started = time.monotonic()
        try:
            completed = subprocess.run(
                cmd,
                text=True,
                capture_output=True,
                timeout=self.timeout_seconds,
                check=False,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired as exc:
            raise ProviderError("qwen token plan timeout", transient=True) from exc
        if completed.returncode != 0:
            error = completed.stderr.strip()[-500:]
            raise ProviderError(f"qwen token plan falhou ({completed.returncode}): {error}")
        outer_json = extract_json(completed.stdout)
        decoded = json.loads(outer_json)
        if _contains_api_error(decoded):
            raise ProviderError("qwen token plan authentication failure (API 401)")
        content_value = _unwrap_structured(decoded)
        if not (
            isinstance(content_value, dict)
            and "temperatura_atual" in content_value
            and "intencao_maxima" in content_value
        ):
            raise ProviderError(
                "qwen token plan retornou envelope sem payload funcional; shape="
                + _event_signature(decoded),
                transient=True,
            )
        return ProviderResponse(
            content=json.dumps(content_value, ensure_ascii=False),
            usage=_extract_usage(decoded),
            raw_metadata={"latency_ms": int((time.monotonic() - started) * 1000)},
        )


def _contains_api_error(value: Any) -> bool:
    if isinstance(value, str):
        lowered = value.lower()
        return (
            "api error" in lowered
            or "invalid api-key" in lowered
            or "invalid api key" in lowered
            or "authentication failed" in lowered
            or "unauthorized" in lowered
        )
    if isinstance(value, dict):
        return any(_contains_api_error(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_api_error(item) for item in value)
    return False
