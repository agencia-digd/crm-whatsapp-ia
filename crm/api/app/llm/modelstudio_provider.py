"""
ModelStudioProvider — Qwen via Alibaba Model Studio (endpoint OpenAI-compatible).

Por que existe (27/08/2026): hoje cada mensagem de lead abre uma sessão nova do
`claude -p` (~230/dia) e come a cota da assinatura PESSOAL da Dona — a mesma que
ela usa pra trabalhar. Model Studio é billing por TOKEN: o custo passa a ser da
operação, não da assinatura, e para de competir com o trabalho dela.

Endpoint OpenAI-compatible (não precisa do SDK da OpenAI — httpx basta, mesmo
padrão do OllamaProvider):

    POST {base_url}/chat/completions

ENDPOINT E MODELO SÃO CONFIGURAÇÃO, NÃO CÓDIGO (05/09/2026). A conta da casa
NÃO é do DashScope público (`dashscope-intl.aliyuncs.com`): é um workspace
MaaS dedicado (`ws-….maas.aliyuncs.com`). Contra o DashScope público a chave
devolve 401 `invalid_api_key` — foi o que deixou o 3º degrau da escada da
Pessoa (Claude → codex → Qwen) morto sem ninguém perceber: só checavam se a
chave EXISTIA no .env, nunca se ela RESPONDIA. Por isso:

  * `base_url` e `model` vêm de `app.config` (MODELSTUDIO_BASE_URL /
    MODELSTUDIO_MODEL_*; com fallback pra /etc/agencia/qwen.env). Sem base_url o
    provider falha claro na CHAMADA, nunca na construção.
  * `verificar()` faz uma chamada mínima real (com cache) e diz se o degrau
    está vivo — é isso, e não "tem chave", que decide se ele entra na escada.

Formato de resposta: modelos da família qwen3 (ex.: `qwen3.8-max`) devolvem
`reasoning_content` ao lado de `content`. O que vale é `content`; o raciocínio
é ignorado (e desligado por padrão com `enable_thinking=false`, que o endpoint
aceita — medido: sem ele, ~40 tokens de raciocínio pra responder "ok").

Chave: NUNCA em código nem em log. Vem de `MODELSTUDIO_API_KEY` no .env do
serviço (`EnvironmentFile` do copiloto-api.service) ou de /etc/agencia/qwen.env.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass
from typing import Any

import httpx
import structlog

# Reusa o extrator do CLI em vez de uma 3ª cópia: ele já é módulo-level e
# documentado como "testável isoladamente", e o problema é o mesmo (modelo
# devolve JSON embrulhado em markdown/texto).
from app.llm.cli_provider import _extract_json
from app.llm.provider import LLMProvider, TokenUsage

log = structlog.get_logger()

# Assinaturas de falha PERMANENTE: retry não resolve, só queima chamada.
# Mesmo espírito do _PERMANENT_ERROR_SIGNS do cli_provider.
_STATUS_PERMANENTES = frozenset({400, 401, 403, 404, 422})

# Cache da verificação: sucesso vale 1h (não gastar chamada a cada turno da
# Pessoa — get_llm_provider() é chamado por turno); falha vale 5 min (dá tempo
# de uma rede/cota voltar sem ficar martelando um 401).
VERIFICACAO_TTL_OK_S = 3600.0
VERIFICACAO_TTL_FALHA_S = 300.0

# Parâmetros que são "ajuste fino", não contrato: se o modelo rejeitar
# (400 invalid_parameter_error), a chamada repete sem eles. `model`,
# `messages`, `response_format` NÃO estão aqui — rejeição desses é falha real.
_PARAMS_OPCIONAIS = frozenset({"temperature", "enable_thinking", "max_tokens"})
_RE_PARAM_REJEITADO = re.compile(r"[Pp]arameter\s+'([A-Za-z_]+)'")


class ModelStudioError(RuntimeError):
    """Falha na chamada ao Model Studio.

    `transient=True` quando retry pode resolver (429, 5xx, timeout, rede).
    """

    def __init__(self, message: str, *, transient: bool) -> None:
        super().__init__(message)
        self.transient = transient


@dataclass(frozen=True)
class StatusDegrau:
    """Resultado de `ModelStudioProvider.verificar()`.

    `permanente=True` = credencial/endpoint/modelo errados: o degrau NÃO deve
    entrar na escada. `permanente=False` com `ok=False` = rede/cota/5xx: pode
    voltar sozinho, o degrau fica e o FallbackProvider tenta na hora.
    """

    ok: bool
    motivo: str = ""
    permanente: bool = False
    duracao_ms: int = 0


_cache_verificacao: dict[str, tuple[float, StatusDegrau]] = {}
_cache_lock = threading.Lock()


def limpar_cache_verificacao() -> None:
    """Zera o cache (testes e drills)."""
    with _cache_lock:
        _cache_verificacao.clear()


class ModelStudioProvider:
    """Implementa LLMProvider contra um endpoint OpenAI-compatible do Model Studio."""

    def __init__(
        self,
        api_key: str,
        *,
        model: str,
        base_url: str,
        timeout: int = 120,
        temperature: float = 0.2,
        enable_thinking: bool = False,
    ) -> None:
        self.api_key = api_key or ""
        self.model = model or ""
        self.base_url = (base_url or "").rstrip("/")
        self.timeout = timeout
        self.temperature = temperature
        self.enable_thinking = enable_thinking
        self._params_rejeitados: set[str] = set()

    def _post(self, url: str, payload: dict[str, Any], timeout: float | None) -> httpx.Response:
        try:
            return httpx.post(
                url,
                json=payload,
                # Authorization nunca é logado — só o status volta pro log.
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=timeout if timeout is not None else self.timeout,
            )
        except httpx.TimeoutException as exc:
            raise ModelStudioError(
                f"ModelStudioProvider: timeout após {timeout or self.timeout}s",
                transient=True,
            ) from exc
        except httpx.HTTPError as exc:
            raise ModelStudioError(
                f"ModelStudioProvider: erro de rede: {type(exc).__name__}", transient=True
            ) from exc

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
                saida, usage = self._chamar(
                    system, conteudo, json_mode=bool(json_schema), tentativa=tentativa + 1
                )
            except ModelStudioError as exc:
                if exc.transient and tentativa == 0:
                    log.warning("modelstudio_retry_transitorio", erro=str(exc)[:160])
                    continue
                raise

            if not json_schema:
                return saida, usage
            json_str = _extract_json(saida)
            if json_str is not None:
                return json_str, usage
            log.warning(
                "modelstudio_sem_json", tentativa=tentativa + 1, preview=saida[:200]
            )

        raise RuntimeError("ModelStudioProvider: JSON não encontrado após 2 tentativas")

    def complete_text(self, system: str, user: str) -> tuple[str, TokenUsage]:
        saida, usage = self._chamar(system, user, json_mode=False, tentativa=1)
        return saida.strip(), usage

    # ── verificação real do degrau ───────────────────────────────────────────

    def _chave_cache(self) -> str:
        # Impressão digital da chave (nunca a chave): muda de chave = revalida.
        digest = hashlib.sha256(self.api_key.encode()).hexdigest()[:12]
        return f"{self.base_url}|{self.model}|{digest}"

    def verificar(self, *, timeout: float = 10.0, usar_cache: bool = True) -> StatusDegrau:
        """Prova que o degrau RESPONDE: uma chamada mínima real (≤ 5 tokens).

        Nunca levanta. Cacheado por (base_url, modelo, chave) — ver TTLs no
        topo. É o critério pra habilitar o degrau na escada, no lugar do
        antigo "a chave existe".
        """
        chave = self._chave_cache()
        agora = time.monotonic()
        if usar_cache:
            with _cache_lock:
                hit = _cache_verificacao.get(chave)
            if hit is not None:
                validade, status = hit
                if agora < validade:
                    return status

        status = self._verificar_sem_cache(timeout)
        ttl = VERIFICACAO_TTL_OK_S if status.ok else VERIFICACAO_TTL_FALHA_S
        with _cache_lock:
            _cache_verificacao[chave] = (agora + ttl, status)
        return status

    def _verificar_sem_cache(self, timeout: float) -> StatusDegrau:
        faltando = [
            nome
            for nome, valor in (
                ("MODELSTUDIO_API_KEY", self.api_key),
                ("MODELSTUDIO_BASE_URL", self.base_url),
                ("MODELSTUDIO_MODEL", self.model),
            )
            if not valor
        ]
        if faltando:
            return StatusDegrau(
                ok=False, motivo=f"configuração vazia: {', '.join(faltando)}", permanente=True
            )
        inicio = time.monotonic()
        try:
            saida, _ = self._chamar(
                "Responda apenas: ok", "ok", json_mode=False, tentativa=1,
                timeout=timeout, max_tokens=5,
            )
        except ModelStudioError as exc:
            return StatusDegrau(
                ok=False,
                motivo=str(exc)[:200],
                permanente=not exc.transient,
                duracao_ms=int((time.monotonic() - inicio) * 1000),
            )
        except Exception as exc:  # noqa: BLE001 — verificar() nunca levanta
            return StatusDegrau(
                ok=False,
                motivo=f"{type(exc).__name__}: {str(exc)[:160]}",
                permanente=False,
                duracao_ms=int((time.monotonic() - inicio) * 1000),
            )
        return StatusDegrau(
            ok=True,
            motivo=f"respondeu {saida.strip()[:20]!r}",
            duracao_ms=int((time.monotonic() - inicio) * 1000),
        )

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

    def _chamar(
        self,
        system: str,
        user: str,
        *,
        json_mode: bool,
        tentativa: int,
        timeout: float | None = None,
        max_tokens: int | None = None,
    ) -> tuple[str, TokenUsage]:
        if not self.api_key:
            # Permanente de propósito: sem chave, nenhum retry ajuda.
            raise ModelStudioError(
                "ModelStudioProvider: MODELSTUDIO_API_KEY vazia — "
                "configure no .env do copiloto-api (ou /etc/agencia/qwen.env) antes de usar",
                transient=False,
            )
        if not self.base_url:
            raise ModelStudioError(
                "ModelStudioProvider: MODELSTUDIO_BASE_URL vazia — a conta da casa é "
                "um workspace dedicado, o endpoint vem da configuração, não do código",
                transient=False,
            )
        if not self.model:
            raise ModelStudioError(
                "ModelStudioProvider: modelo vazio — configure MODELSTUDIO_MODEL_ANALYZER",
                transient=False,
            )

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.temperature,
            # qwen3 raciocina por padrão e cobra os tokens do raciocínio; a
            # Pessoa só precisa do `content`. O endpoint aceita o campo no
            # topo do payload (medido 05/09/2026).
            "enable_thinking": self.enable_thinking,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        # Cada fornecedor do workspace aceita um subconjunto dos parâmetros
        # (medido 06/09: kimi-k3 rejeita `temperature`). O que o modelo já
        # rejeitou não vai mais nas próximas chamadas desta instância.
        for nome in self._params_rejeitados:
            payload.pop(nome, None)

        url = f"{self.base_url}/chat/completions"
        inicio = time.monotonic()
        r = self._post(url, payload, timeout)
        duracao_ms = int((time.monotonic() - inicio) * 1000)

        if r.status_code == 400:
            rejeitado = _param_rejeitado(r.text or "")
            if rejeitado and rejeitado in payload and rejeitado in _PARAMS_OPCIONAIS:
                # Não é falha do degrau: é o modelo dizendo "não uso esse
                # parâmetro". Tira e repete UMA vez, sem contar como retry.
                log.warning(
                    "modelstudio_param_rejeitado", model=self.model, param=rejeitado,
                    efeito="removido das próximas chamadas",
                )
                self._params_rejeitados.add(rejeitado)
                payload = {k: v for k, v in payload.items() if k != rejeitado}
                inicio = time.monotonic()
                r = self._post(url, payload, timeout)
                duracao_ms = int((time.monotonic() - inicio) * 1000)

        if r.status_code >= 400:
            transient = r.status_code not in _STATUS_PERMANENTES
            corpo = (r.text or "")[:300]
            log.error(
                "modelstudio_http_erro",
                status=r.status_code,
                transient=transient,
                model=self.model,
                corpo=corpo,
            )
            raise ModelStudioError(
                f"ModelStudioProvider: HTTP {r.status_code} "
                f"({'transitório' if transient else 'permanente'}): {corpo}",
                transient=transient,
            )

        try:
            data = r.json()
            mensagem = data["choices"][0]["message"]
            saida = _conteudo_da_mensagem(mensagem)
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise ModelStudioError(
                f"ModelStudioProvider: resposta inesperada: {(r.text or '')[:200]}",
                transient=True,
            ) from exc

        usage = _usage(data.get("usage"), user, saida)
        log.info(
            "modelstudio_provider_call",
            provider="modelstudio",
            model=self.model,
            tentativa=tentativa,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            duration_ms=duracao_ms,
        )
        return saida, usage


def _param_rejeitado(corpo: str) -> str | None:
    """Nome do parâmetro que o modelo rejeitou num 400 invalid_parameter_error.

    Ex. real (kimi-k3, 06/09/2026):
      {"error":{"code":"invalid_parameter_error","message":
        "Parameter 'temperature'=0.2 is not supported for kimi-k3 model."}}
    """
    if "invalid_parameter" not in corpo and "not supported" not in corpo:
        return None
    m = _RE_PARAM_REJEITADO.search(corpo)
    return m.group(1) if m else None


def _conteudo_da_mensagem(mensagem: Any) -> str:
    """Pega o `content`; ignora `reasoning_content` (qwen3 devolve os dois).

    Se só veio raciocínio (ex.: `max_tokens` curto demais e o modelo gastou
    tudo pensando), devolve vazio e avisa — quem chama trata como "sem JSON"
    e retenta com prompt reforçado, em vez de tentar parsear o raciocínio.
    """
    if not isinstance(mensagem, dict):
        raise TypeError("message não é objeto")
    conteudo = mensagem.get("content")
    if isinstance(conteudo, list):  # variante multipart do formato OpenAI
        conteudo = "".join(
            p.get("text", "") for p in conteudo if isinstance(p, dict)
        )
    conteudo = conteudo or ""
    if not conteudo and mensagem.get("reasoning_content"):
        log.warning(
            "modelstudio_so_reasoning_sem_content",
            reasoning_chars=len(str(mensagem.get("reasoning_content"))),
        )
    return conteudo


def _usage(bruto: object, entrada: str, saida: str) -> TokenUsage:
    """Contagem real da API; se vier faltando, estima em ~4 chars/token."""
    if isinstance(bruto, dict):
        entrada_tok = bruto.get("prompt_tokens")
        saida_tok = bruto.get("completion_tokens")
        if isinstance(entrada_tok, int) and isinstance(saida_tok, int):
            return TokenUsage(input_tokens=entrada_tok, output_tokens=saida_tok)
    return TokenUsage(
        input_tokens=max(1, len(entrada) // 4),
        output_tokens=max(1, len(saida) // 4),
    )
