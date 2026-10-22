"""Web Push — aviso de lead novo no celular da corretora.

Por que Web Push e não app de loja: o painel V2 é web. Instalado na tela de
início (PWA), ele recebe notificação com o app fechado, sem loja, sem revisão
da Apple, sem build. Deploy do painel = atualização no celular.

Trava do iPhone: o Safari só entrega push se o site tiver sido ADICIONADO À
TELA DE INÍCIO. Aberto no navegador, a permissão nem é oferecida. Como as
corretoras da Acme são todas iPhone, o onboarding ("instala aqui, ó") é
parte do trabalho — não é detalhe.

Falha nunca derruba o intake: lead entrando é o que importa; aviso é
conveniência. Todo erro daqui é logado e engolido.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import structlog

from sqlalchemy import or_ as sa_or

from app.config import settings

log = structlog.get_logger(__name__)

# Códigos que significam "esse aparelho não existe mais" (desinstalou, limpou
# dados, trocou de celular). Não adianta retentar — desliga a inscrição.
_MORTOS = {404, 410}


def _agora() -> datetime:
    return datetime.now(timezone.utc)


def enviar_push(
    *,
    corretora_id: int | None,
    titulo: str,
    corpo: str,
    url: str | None = None,
    tag: str | None = None,
    session_factory=None,
) -> dict[str, Any]:
    """Manda uma notificação pros aparelhos ativos de uma corretora.

    Args:
        corretora_id: dona do aviso. None = ninguém recebe (lead sem dona não
            vira push pra todo mundo — isso viraria spam na equipe inteira).
        titulo / corpo: o que aparece na tela de bloqueio.
        url: pra onde o toque leva (relativa ao painel ou absoluta).
        tag: agrupa avisos — mesma tag substitui a anterior em vez de empilhar.

    Returns dict com enviados / falhas / desativadas.
    """
    if not settings.vapid_private_key or not settings.vapid_public_key:
        log.warning("push_sem_chaves_vapid")
        return {"status": "sem_chaves", "enviados": 0}

    from pywebpush import WebPushException, webpush

    from app.db import models as _models

    if session_factory is None:
        from app.db.session import SessionLocal as session_factory  # type: ignore

    destino = url or "/meu-dia"
    if destino.startswith("/"):
        destino = settings.painel_base_url.rstrip("/") + destino

    payload = json.dumps({
        "titulo": titulo,
        "corpo": corpo,
        "url": destino,
        "tag": tag or "acme",
    })

    enviados = falhas = desativadas = 0

    with session_factory() as session:
        # Vai pra dona do lead E pras admins. Admin = inscrição com
        # corretora_id NULL: acompanha a operação inteira, então recebe todo
        # lead novo. Sem isso a Dona não recebia nada — nem conseguia testar.
        alvo = _models.PushInscricao.corretora_id.is_(None)
        if corretora_id is not None:
            alvo = sa_or(_models.PushInscricao.corretora_id == corretora_id, alvo)

        inscricoes = (
            session.query(_models.PushInscricao)
            .filter(alvo, _models.PushInscricao.ativa.is_(True))
            .all()
        )
        if not inscricoes:
            log.info("push_sem_aparelho", corretora_id=corretora_id)
            return {"status": "sem_aparelho", "enviados": 0}

        for ins in inscricoes:
            try:
                webpush(
                    subscription_info={
                        "endpoint": ins.endpoint,
                        "keys": {"p256dh": ins.p256dh, "auth": ins.auth},
                    },
                    data=payload,
                    vapid_private_key=settings.vapid_private_key,
                    vapid_claims={"sub": settings.vapid_subject},
                    ttl=3600,          # lead velho não interessa mais
                )
                ins.ultimo_envio_em = _agora()
                enviados += 1
            except WebPushException as exc:
                codigo = getattr(exc.response, "status_code", None)
                if codigo in _MORTOS:
                    ins.ativa = False
                    ins.desativada_motivo = f"http {codigo} — aparelho não existe mais"
                    desativadas += 1
                    log.info("push_inscricao_desativada",
                             corretora_id=corretora_id, http=codigo)
                else:
                    falhas += 1
                    log.warning("push_falhou", corretora_id=corretora_id,
                                http=codigo, erro=str(exc)[:200])
            except Exception as exc:  # noqa: BLE001 — push nunca derruba o chamador
                falhas += 1
                log.warning("push_erro_inesperado", erro=str(exc)[:200])

        session.commit()

    log.info("push_enviado", corretora_id=corretora_id,
             enviados=enviados, falhas=falhas, desativadas=desativadas)
    return {
        "status": "ok",
        "enviados": enviados,
        "falhas": falhas,
        "desativadas": desativadas,
    }


def avisar_lead_novo(
    *,
    corretora_id: int | None,
    nome: str | None,
    telefone: str | None,
    session_factory=None,
) -> dict[str, Any]:
    """O aviso que a corretora vê: nome e número, nada mais.

    Curto de propósito — a notificação é para ela decidir se atende AGORA, e
    isso se decide em dois segundos na tela de bloqueio.
    """
    if not settings.push_lead_novo_ativo:
        return {"status": "desligado", "enviados": 0}

    nome_show = (nome or "").strip() or "Lead sem nome"
    tel_show = (telefone or "").strip() or "sem telefone"

    from urllib.parse import urlencode

    # Leva pra tela do lead, não pro CRM: a corretora está com o celular na
    # mão e um objetivo só — falar com essa pessoa agora. Lá tem o botão de
    # ligar. Nome e telefone vão na URL porque o lead recém-criado ainda não
    # tem id do Kommo (o sync vem depois) e a tela precisa abrir instantânea.
    query = urlencode({"tel": tel_show, "nome": nome_show})

    return enviar_push(
        corretora_id=corretora_id,
        titulo="Caiu um lead 🔔",
        corpo=f"{nome_show} · {tel_show}",
        url=f"/lead?{query}",
        # tag por telefone: se o mesmo lead gerar dois eventos, a segunda
        # notificação SUBSTITUI a primeira em vez de empilhar duas iguais.
        tag=f"lead-{tel_show}",
        session_factory=session_factory,
    )
