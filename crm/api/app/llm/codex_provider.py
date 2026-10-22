"""
CodexProvider — LLM via `codex exec` (Codex CLI / assinatura ChatGPT).

Motivo (28/07/2026): o copiloto rodava em `claude -p`, ou seja, na assinatura
PESSOAL da Dona — ~220 análises/dia × 2 chamadas. Quando a cota dela estourava,
o sistema da CLIENTE parava junto. Este provider move esse volume pra assinatura
Codex/GPT, que é a maior e estava ociosa.

Modelo padrão: gpt-5.6-terra ("Terra"), que ganhou o bloco de voz PT-BR no
benchmark F2 — é ele que escreve o que a corretora lê.

Gotchas do codex CLI (custaram tempo antes — não remover):
  * `--skip-git-repo-check` é OBRIGATÓRIO fora de repo git, senão o CLI aborta
    com "Not inside a trusted directory".
  * stdin PRECISA ser fechado (DEVNULL / `</dev/null`), senão trava em
    "Reading additional input from stdin..." até o timeout.
  * Via SSH (execução remota no Mac), usar `ssh -n` pelo mesmo motivo.

Estimativa de tokens: chars / 4, igual ao cli_provider (o CLI não devolve
contagem real de forma estável).
"""
import json
import os
import re
import subprocess
import time

import structlog

from app.llm.cli_provider import _estimate_tokens, _extract_json
from app.llm.provider import LLMProvider, TokenUsage

log = structlog.get_logger()

# Mesma lógica do cli_provider: falha permanente não merece retry.
_PERMANENT_ERROR_SIGNS = (
    "unauthorized",
    "not logged in",
    "no auth",
    "authentication",
    "invalid api key",
    "credit",
    "insufficient",
    "quota",
    "usage limit",
    "forbidden",
    "permission denied",
    "command not found",
    "not found: codex",
    "not supported when using codex with a chatgpt account",
)


class CodexProviderError(RuntimeError):
    """Falha na execução do `codex` CLI (returncode != 0 ou saída vazia)."""

    def __init__(self, message: str, *, returncode: int, transient: bool) -> None:
        super().__init__(message)
        self.returncode = returncode
        self.transient = transient


def _classify_stderr(stderr: str) -> bool:
    """True = transitório (vale retry); False = permanente (auth/quota/config)."""
    low = (stderr or "").lower()
    return not any(sign in low for sign in _PERMANENT_ERROR_SIGNS)


# Ruído que o codex escreve no stderr ANTES do erro real: o "Reading additional
# input from stdin..." (sai MESMO com stdin em DEVNULL — é só o CLI anunciando
# que vai olhar o stdin, não é trava), o aviso do bubblewrap, o preâmbulo de
# telemetria e o ECO DO PROMPT INTEIRO (19k chars) sob a linha "user".
# Em 05/09/2026 esse ruído ocupou os 300 chars do log e escondeu a linha que
# importava, no FIM do stderr: "ERROR: You've hit your usage limit ... try again
# at Sep 7th". O diagnóstico saiu errado por horas ("stdin não fechado").
_RUIDO_STDERR = (
    "reading additional input from stdin",
    "could not find bubblewrap",
)


def _erro_relevante(stderr: str, limite: int = 300) -> str:
    """Só a parte do stderr que explica a falha.

    Prioridade: linhas `ERROR:` (o codex repete a mesma 2x — dedupa). Sem elas,
    a ÚLTIMA linha que não é ruído nem preâmbulo — o erro vem no fim, depois do
    eco do prompt; pegar mais linhas traz texto da conversa pro log e pra
    classificação ("quota"/"credit" ditos pelo lead não são falha permanente).
    """
    linhas = [ln.strip() for ln in (stderr or "").splitlines() if ln.strip()]
    erros = [ln for ln in linhas if ln.lower().startswith("error")]
    if erros:
        return " | ".join(dict.fromkeys(erros))[:limite]
    uteis = [
        ln for ln in linhas
        if not any(m in ln.lower() for m in _PREAMBLE_MARKERS + _RUIDO_STDERR)
        and ln not in ("--------", "user")
    ]
    return (uteis[-1] if uteis else "")[:limite]


class CodexProvider:
    """Implementa LLMProvider via subprocess `codex exec`.

    `remote_host` != None roda no Mac por SSH (fallback enquanto o VPS não tem
    login próprio). Preferir SEMPRE local: SSH adiciona latência e transforma o
    Mac em ponto único de falha do sistema da cliente.
    """

    def __init__(
        self,
        timeout: int = 180,
        model: str | None = None,
        remote_host: str | None = None,
        workdir: str = "/tmp",
    ) -> None:
        self.timeout = timeout
        self.model = model or "gpt-5.6-terra"
        self.remote_host = remote_host
        self.workdir = workdir

    def complete(
        self,
        system: str,
        user: str,
        json_schema: dict | None = None,
    ) -> tuple[str, TokenUsage]:
        prompt = self._build_prompt(system, user, json_schema)

        for attempt in range(2):
            effective_prompt = prompt if attempt == 0 else self._reinforce(prompt)
            try:
                output = self._run_cli(effective_prompt, attempt=attempt + 1)
            except CodexProviderError as exc:
                # Falha permanente (auth/quota) propaga na hora: esconder isso
                # atrás de "JSON não encontrado" foi o que dificultou o debug
                # do cli_provider no passado.
                if exc.transient and attempt == 0:
                    log.warning("codex_provider_retry_transitorio", returncode=exc.returncode)
                    continue
                raise

            json_str = _extract_json(output)
            if json_str is not None:
                return json_str, _estimate_tokens(effective_prompt, output)

            log.warning(
                "codex_provider_no_json", attempt=attempt + 1, output_preview=output[:200]
            )

        raise RuntimeError("CodexProvider: JSON não encontrado após 2 tentativas")

    def complete_text(self, system: str, user: str) -> tuple[str, TokenUsage]:
        prompt = self._build_prompt(system, user, json_schema=None)
        output = self._run_cli(prompt)
        return output.strip(), _estimate_tokens(prompt, output)

    # ── internals ────────────────────────────────────────────────────────────

    def _build_prompt(self, system: str, user: str, json_schema: dict | None) -> str:
        parts = [system, user]
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

    def _cmd(self, prompt: str) -> list[str]:
        """Monta o argv. O prompt vai como argumento posicional (não por stdin,
        que precisa ficar fechado)."""
        base = [
            "codex",
            "exec",
            "--skip-git-repo-check",  # obrigatório fora de repo git
            "-m",
            self.model,
            prompt,
        ]
        if self.remote_host:
            # -n fecha o stdin do ssh; sem isso o codex remoto trava esperando input.
            return ["ssh", "-n", "-o", "BatchMode=yes", self.remote_host,
                    f"cd {self.workdir} && " + " ".join(_shq(a) for a in base)]
        return base

    def _run_cli(self, prompt: str, attempt: int = 1) -> str:
        start = time.monotonic()
        cmd = self._cmd(prompt)
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,  # sem isso: "Reading additional input from stdin"
                timeout=self.timeout,
                cwd=None if self.remote_host else self.workdir,
                env={**os.environ, "RUST_LOG": "error"},  # menos ruído no stderr
            )
            duration_ms = int((time.monotonic() - start) * 1000)
            stderr = result.stderr or ""
            log.info(
                "codex_provider_call",
                provider="codex",
                model=self.model,
                remote=bool(self.remote_host),
                attempt=attempt,
                input_chars=len(prompt),
                output_chars=len(result.stdout),
                duration_ms=duration_ms,
                returncode=result.returncode,
            )
            if result.returncode != 0:
                # Classifica e loga SÓ a parte relevante: o stderr traz o eco do
                # prompt inteiro, e uma palavra da conversa ("quota", "credit")
                # não pode decidir se a falha é permanente.
                relevante = _erro_relevante(stderr, limite=2000)
                transient = _classify_stderr(relevante)
                stderr_trunc = relevante[:300]
                log.error(
                    "codex_provider_returncode_erro",
                    returncode=result.returncode,
                    transient=transient,
                    stderr=stderr_trunc,
                )
                raise CodexProviderError(
                    f"CodexProvider: exit {result.returncode} "
                    f"({'transitório' if transient else 'permanente'}): {stderr_trunc}",
                    returncode=result.returncode,
                    transient=transient,
                )
            return _strip_preamble(result.stdout)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"CodexProvider: timeout após {self.timeout}s") from exc


class FallbackProvider:
    """Tenta o primário; se ele falhar, cai no reserva e SEGUE FUNCIONANDO.

    Existe por causa do risco registrado no PLANO-ELENCO-FINAL §3.3: o codex
    pode estar fora (Mac desligado, login expirado, cota). Sem isso, o sistema
    da CLIENTE pararia — que é justamente o problema que a migração veio curar.
    Degradar pro Claude é sempre melhor que parar.

    DISJUNTOR (achado no ensaio de rollback, 28/07): sem ele, cada análise paga
    a falha do primário ANTES de cair no reserva — medido 74s vs 14s normais.
    Com 220 análises/dia, o Inspector atrasaria o briefing das 8h. Depois de
    `limite_falhas` falhas seguidas, o primário é pulado por `pausa_s` e as
    chamadas vão DIRETO pro reserva; passada a pausa, uma chamada testa se
    voltou. Sucesso zera o contador.
    """

    def __init__(
        self,
        primary: LLMProvider,
        backup: LLMProvider,
        *,
        limite_falhas: int = 3,
        pausa_s: float = 300.0,
    ) -> None:
        self.primary = primary
        self.backup = backup
        self.limite_falhas = limite_falhas
        self.pausa_s = pausa_s
        self._falhas = 0
        self._aberto_ate = 0.0
        self._ultimo_modelo: str | None = None

    # ── disjuntor ────────────────────────────────────────────────────────────

    def _pular_primario(self) -> bool:
        """True enquanto o disjuntor estiver aberto (primário em quarentena)."""
        if self._aberto_ate and time.monotonic() < self._aberto_ate:
            return True
        if self._aberto_ate:  # pausa venceu — deixa uma chamada testar
            self._aberto_ate = 0.0
            self._falhas = 0
            log.info("llm_disjuntor_testando_primario")
        return False

    def _registrar_falha(self) -> None:
        self._falhas += 1
        if self._falhas >= self.limite_falhas and not self._aberto_ate:
            self._aberto_ate = time.monotonic() + self.pausa_s
            log.error(
                "llm_disjuntor_abriu",
                falhas=self._falhas,
                pausa_s=self.pausa_s,
                efeito="chamadas vão direto pro reserva até a pausa vencer",
            )

    def _registrar_sucesso(self) -> None:
        if self._falhas:
            log.info("llm_disjuntor_reset", falhas_antes=self._falhas)
        self._falhas = 0
        self._aberto_ate = 0.0

    # ── API ──────────────────────────────────────────────────────────────────

    @property
    def ultimo_modelo(self) -> str | None:
        """Modelo de quem respondeu a ÚLTIMA chamada — primário ou reserva.

        Existe pra contabilidade (ContabilProvider). Sem isso o custo era
        lançado no modelo do primário mesmo quando o reserva atendeu, ou seja,
        mentia exatamente na hora em que a gente mais precisa saber que o
        primário caiu.
        """
        return self._ultimo_modelo

    def _chamar(self, metodo: str, *args):
        if self._pular_primario():
            return self._via(self.backup, metodo, *args)
        try:
            resultado = self._via(self.primary, metodo, *args)
            self._registrar_sucesso()
            return resultado
        except Exception as exc:  # noqa: BLE001 — qualquer falha justifica degradar
            self._registrar_falha()
            log.error("llm_fallback_ativado", metodo=metodo, erro=str(exc)[:200])
            return self._via(self.backup, metodo, *args)

    def _via(self, alvo, metodo: str, *args):
        self._ultimo_modelo = getattr(alvo, "ultimo_modelo", None) or getattr(
            alvo, "model", None
        )
        return getattr(alvo, metodo)(*args)

    def complete(self, system: str, user: str, json_schema: dict | None = None):
        return self._chamar("complete", system, user, json_schema)

    def complete_text(self, system: str, user: str):
        return self._chamar("complete_text", system, user)


# ── helpers ──────────────────────────────────────────────────────────────────

def _shq(arg: str) -> str:
    """Quote pra linha de comando remota (o prompt tem aspas, quebra de linha e
    acento — sem isso o SSH estilhaça o argumento)."""
    return "'" + arg.replace("'", "'\\''") + "'"


# Linhas de telemetria que o codex imprime junto da resposta.
_PREAMBLE_MARKERS = ("tokens used", "[codex]", "workdir:", "model:", "provider:")
# "tokens used" vem seguido do NÚMERO em outra linha ("tokens used\n2.534") —
# essa segunda linha também é telemetria. Só descartamos um número solto quando
# ele vem logo depois do marcador, pra não comer um número da resposta real.
_SO_NUMERO = re.compile(r"^[\d.,]+$")


def _strip_preamble(text: str) -> str:
    """Remove as linhas de telemetria do `codex exec`, preservando o conteúdo.

    O CLI mistura metadados ("tokens used", "workdir:") com a resposta. Pro
    complete() isso é inofensivo (o _extract_json acha o bloco), mas o
    complete_text() devolveria lixo junto do texto que a corretora lê.
    """
    saida: list[str] = []
    anterior_era_marcador = False
    for ln in (text or "").splitlines():
        nu = ln.strip().lower()
        if any(m in nu for m in _PREAMBLE_MARKERS):
            anterior_era_marcador = True
            continue
        if anterior_era_marcador and _SO_NUMERO.match(ln.strip()):
            anterior_era_marcador = False  # consome só a linha do contador
            continue
        anterior_era_marcador = False
        saida.append(ln)
    return "\n".join(saida).strip()
