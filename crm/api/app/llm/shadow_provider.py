"""
ShadowProvider — roda o cérebro candidato EM PARALELO, só pra comparar.

Passo 2 do PLANO-ELENCO-FINAL: antes de virar a chave da Acme pro Terra,
medir a qualidade dele nas conversas REAIS das corretoras — sem que uma única
corretora receba resposta do candidato.

Contrato inegociável:
  * a resposta de produção é SEMPRE a do primário (Claude). O sombra nunca
    influencia o retorno;
  * o sombra roda em thread separada → não adiciona latência ao briefing;
  * qualquer falha do sombra é engolida (ele é experimento, não operação);
  * amostragem com teto diário — enquanto a assinatura Codex for $20, medir
    ~20-30 análises/dia basta e não estoura cota (pedido da Dona, 28/07).

Saída: JSONL em `shadow_llm_path`, um registro por duelo, com os dois lados
lado a lado. Mesmo padrão da sombra do Telegram (`shadow_grok.jsonl`), que já
provou ser fácil de auditar depois.

Liga/desliga sem restart: arquivo-flag (`shadow_llm_flag`). `touch` liga,
`rm` desliga — igual à sombra do Telegram.
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import structlog

from app.llm.provider import LLMProvider, TokenUsage

log = structlog.get_logger()

_lock = threading.Lock()


class ShadowProvider:
    """Envelopa o provider de produção e duela contra um candidato."""

    def __init__(
        self,
        primary: LLMProvider,
        shadow: LLMProvider,
        *,
        jsonl_path: str,
        flag_path: str = "",
        cap_diario: int = 30,
        rotulo: str = "codex",
    ) -> None:
        self.primary = primary
        self.shadow = shadow
        self.jsonl_path = jsonl_path
        self.flag_path = flag_path
        self.cap_diario = cap_diario
        self.rotulo = rotulo

    # ── API do LLMProvider (delega tudo pro primário) ─────────────────────────

    def complete(self, system: str, user: str, json_schema: dict | None = None):
        resultado = self.primary.complete(system, user, json_schema)
        self._duelar("complete", system, user, json_schema, resultado)
        return resultado

    def complete_text(self, system: str, user: str):
        resultado = self.primary.complete_text(system, user)
        self._duelar("complete_text", system, user, None, resultado)
        return resultado

    # ── sombra ────────────────────────────────────────────────────────────────

    def _ligada(self) -> bool:
        """Flag-file liga/desliga sem restart. Sem flag configurada = ligada."""
        if not self.flag_path:
            return True
        return Path(self.flag_path).exists()

    def _n_hoje(self) -> int:
        """Quantos duelos já rodaram hoje (lê o próprio JSONL — sem estado extra)."""
        hoje = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        try:
            with open(self.jsonl_path, encoding="utf-8") as fh:
                return sum(1 for ln in fh if f'"ts": "{hoje}' in ln or f'"ts":"{hoje}' in ln)
        except FileNotFoundError:
            return 0
        except Exception:  # noqa: BLE001 — contagem quebrada não pode travar produção
            return 0

    def _duelar(self, metodo, system, user, schema, resultado_primario) -> None:
        """Dispara o candidato em background. NUNCA levanta, NUNCA atrasa."""
        try:
            if not self._ligada():
                return
            with _lock:
                if self._n_hoje() >= self.cap_diario:
                    return
            saida_primario = resultado_primario[0] if resultado_primario else ""
            t = threading.Thread(
                target=self._rodar_sombra,
                args=(metodo, system, user, schema, saida_primario),
                daemon=True,  # não segura o processo do Inspector no fim do run
            )
            t.start()
        except Exception as exc:  # noqa: BLE001
            log.warning("shadow_llm_dispatch_falhou", erro=str(exc)[:120])

    def _rodar_sombra(self, metodo, system, user, schema, saida_primario) -> None:
        inicio = time.monotonic()
        saida, erro = "", ""
        tokens_in, tokens_out = 0, 0
        try:
            if metodo == "complete":
                saida, usage = self.shadow.complete(system, user, schema)
            else:
                saida, usage = self.shadow.complete_text(system, user)
            tokens_in = getattr(usage, "input_tokens", 0) or 0
            tokens_out = getattr(usage, "output_tokens", 0) or 0
        except Exception as exc:  # noqa: BLE001 — sombra falhar é dado, não incidente
            erro = str(exc)[:300]

        registro = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "rotulo": self.rotulo,
            "metodo": metodo,
            # prompt truncado: o JSONL é pra comparar SAÍDA; guardar conversa
            # inteira incharia o arquivo e duplicaria dado de lead sem necessidade.
            "prompt_preview": (user or "")[:400],
            "primario": saida_primario,
            "sombra": saida,
            "sombra_ok": not erro,
            "sombra_erro": erro,
            "sombra_ms": int((time.monotonic() - inicio) * 1000),
            "sombra_tokens_in": tokens_in,
            "sombra_tokens_out": tokens_out,
        }
        try:
            with _lock:
                os.makedirs(os.path.dirname(self.jsonl_path) or ".", exist_ok=True)
                with open(self.jsonl_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(registro, ensure_ascii=False) + "\n")
            log.info(
                "shadow_llm_duelo",
                rotulo=self.rotulo, metodo=metodo,
                ok=not erro, ms=registro["sombra_ms"],
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("shadow_llm_write_falhou", erro=str(exc)[:120])
