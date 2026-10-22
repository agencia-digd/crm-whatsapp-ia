"""
ContabilProvider — envelopa QUALQUER provider e grava o que cada chamada custou.

Por que existe (09/09/2026): em 06/09 o copiloto inteiro saiu do Claude e foi
pro Alibaba. O painel de consumo da casa lê Alibaba pelo LOG DO CLI do Qwen —
e o copiloto chama a API direto, sem CLI. Da noite pro dia o custo do único
provider que sustenta produção sumiu do painel.

A lição não é "ler o Alibaba também". É que **medir o custo pelo canal do
provider quebra toda vez que a gente troca de provider**. Então a medição passa
a sair de dentro: o envelope fica em `get_llm_provider()`, que é o funil por
onde todo provider passa (cli, codex, modelstudio, api, ollama). Trocar de
provider de novo não cega o painel outra vez.

Contrato:
  * a resposta é SEMPRE a do provider envelopado, intacta;
  * falha ao gravar é engolida — contabilidade nunca derruba atendimento;
  * grava também a chamada que deu ERRO, com a exceção em `erro`. Chamada que
    falhou também queimou token de entrada, e some do custo se a gente só
    contar sucesso.
"""
from __future__ import annotations

import os
import sys

import structlog

from app.llm.provider import LLMProvider, TokenUsage

log = structlog.get_logger()


def _cava_modelo(p, fundo: int = 4) -> str | None:
    """Desce a cadeia de envelopes ate achar quem sabe o modelo.

    Nao basta olhar o provider de fora: a cadeia real e' ShadowProvider por cima
    do FallbackProvider (que e' quem tem `ultimo_modelo`), e as vezes um Fallback
    dentro do outro. Lendo so o primeiro nivel, o modelo gravava "?" — peguei
    assim em 10/09/2026, ao mover o copiloto de volta pro Claude.
    """
    if p is None or fundo < 0:
        return None
    m = getattr(p, "ultimo_modelo", None) or getattr(p, "model", None)
    if m:
        return m
    for dentro in ("primary", "_interno", "principal", "backup"):
        m = _cava_modelo(getattr(p, dentro, None), fundo - 1)
        if m:
            return m
    return None


def _servico() -> str:
    """Quem está chamando. A conta de 'quanto custa operar' é por serviço.

    Preferência: COPILOTO_SERVICO no systemd (explícito e estável). Sem ele,
    cai no nome do script — o que já separa inspector de api sem configurar
    nada, e é melhor que jogar tudo num balaio 'desconhecido'.
    """
    env = (os.getenv("COPILOTO_SERVICO") or "").strip()
    if env:
        return env[:60]
    base = os.path.basename(sys.argv[0] or "").removesuffix(".py")
    return (base or "desconhecido")[:60]


class ContabilProvider:
    """Envelopa um provider e registra cada chamada em `llm_chamadas`."""

    def __init__(self, interno: LLMProvider, modelo: str | None = None) -> None:
        self._interno = interno
        self._modelo_fixo = modelo
        self._provider = type(interno).__name__

    def _modelo_agora(self) -> str:
        """Resolvido A CADA chamada, nunca na construção.

        Quando o primário é um FallbackProvider, o modelo que respondeu só se
        sabe DEPOIS: pode ter sido o reserva (kimi no lugar do qwen, Claude no
        lugar do codex). Lido na construção, o custo do reserva era lançado no
        nome do primário — mentindo justo quando o primário caiu.
        """
        if self._modelo_fixo:
            return self._modelo_fixo
        return _cava_modelo(self._interno) or "?"

    def complete(
        self,
        system: str,
        user: str,
        json_schema: dict | None = None,
    ) -> tuple[str, TokenUsage]:
        import time

        t0 = time.monotonic()
        try:
            saida, usage = self._interno.complete(system, user, json_schema)
        except Exception as exc:
            self._registra(None, int((time.monotonic() - t0) * 1000), repr(exc)[:400])
            raise
        self._registra(usage, int((time.monotonic() - t0) * 1000), None)
        return saida, usage

    def _registra(self, usage: TokenUsage | None, duracao_ms: int, erro: str | None) -> None:
        try:
            from sqlalchemy import text

            from app.db.session import SessionLocal
            from app.llm.precos import custo_usd

            modelo = str(self._modelo_agora())
            it = usage.input_tokens if usage else 0
            ot = usage.output_tokens if usage else 0
            params = {
                "sv": _servico(),
                "pv": self._provider,
                "md": modelo,
                "it": it,
                "ot": ot,
                "ms": duracao_ms,
                "er": erro,
                "cu": custo_usd(modelo, it, ot),
            }
            base = (
                "INSERT INTO llm_chamadas "
                "(servico, provider, modelo, input_tokens, output_tokens, duracao_ms, erro{col}) "
                "VALUES (:sv, :pv, :md, :it, :ot, :ms, :er{val})"
            )
            with SessionLocal() as s:
                try:
                    s.execute(text(base.format(col=", custo_usd", val=", :cu")), params)
                except Exception:
                    # Código novo antes da migration 0093: sem a coluna, grava como antes.
                    # Perder o custo é aceitável; perder a chamada inteira não.
                    s.rollback()
                    s.execute(text(base.format(col="", val="")), params)
                s.commit()
        except Exception as exc:  # nunca derruba o atendimento
            log.warning("contabil_falhou", err=str(exc)[:200])

    def __getattr__(self, nome: str):
        # Providers têm extras além do complete() (ex. `model`, health checks).
        # Sem isso o envelope esconderia o que o resto do app já usa.
        return getattr(self._interno, nome)
