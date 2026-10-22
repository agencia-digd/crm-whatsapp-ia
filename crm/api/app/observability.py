"""Observabilidade — erro de produção vai pro GlitchTip em vez de sumir no log.

GlitchTip é wire-compatible com o Sentry, então usamos o `sentry-sdk` normal e
só trocamos o DSN (`GLITCHTIP_DSN`).

REGRA: observabilidade NUNCA derruba o serviço que ela observa. Sem
`GLITCHTIP_DSN` setado o SDK simplesmente não sobe, e qualquer falha no init é
logada como warning — nunca propaga.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import structlog

from app.config import settings

log = structlog.get_logger()

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _derive_release() -> str | None:
    """SHA curto do commit atual, pra correlacionar erro com deploy.

    Barato (um `git rev-parse` com timeout) e best-effort: em container sem git
    ou sem .git, devolve None e o evento vai sem release.
    """
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=_PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    sha = out.stdout.strip()
    return f"copiloto-acme@{sha}" if sha else None


def init_observability() -> bool:
    """Liga o SDK do Sentry apontado pro GlitchTip. Devolve True se subiu.

    No-op silencioso quando `GLITCHTIP_DSN` está vazio (dev, testes, CI).
    """
    dsn = settings.glitchtip_dsn.strip()
    if not dsn:
        log.debug("glitchtip_desligado", motivo="GLITCHTIP_DSN vazio")
        return False

    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.starlette import StarletteIntegration

        sentry_sdk.init(
            dsn=dsn,
            environment=settings.env,
            release=_derive_release(),
            integrations=[StarletteIntegration(), FastApiIntegration()],
            traces_sample_rate=settings.glitchtip_traces_sample_rate,
            # Conversa de lead e telefone de corretora não vão pro painel.
            send_default_pii=False,
        )
    except Exception as exc:  # noqa: BLE001 — observabilidade não derruba o app
        log.warning("glitchtip_init_falhou", erro=str(exc))
        return False

    log.info("glitchtip_ativo", environment=settings.env)
    return True
