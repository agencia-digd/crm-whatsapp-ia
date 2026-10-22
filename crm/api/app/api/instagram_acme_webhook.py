"""Receptor de Instagram da Acme — GET+POST /webhooks/instagram-acme.

Tarefas 4 e 4A da `projetos/acme/OS-ASSISTENTE-IA.md`. MODO SOMBRA: recebe,
valida, normaliza, grava. **Não responde comentário, não manda DM, não cadastra
lead.** Não existe neste arquivo nenhum cliente HTTP de saída — o teste
`tests/test_instagram_ingest.py` varre a fonte para afirmar a ausência.

## De onde vem o evento (medido em 21/09/2026)

A Meta aceita **um callback por objeto, por app**, e o objeto `instagram` do app
`agencia api` (0000) já aponta para `https://app.example.com/webhook/meta`. Os
eventos da Acme já chegam lá — 24 comentários entre 15/08 e 17/09. Então esta
rota não é assinada na Meta: ela recebe o **repasse** do `painel-corretor`, que manda
o corpo cru e a assinatura originais.

Consequência prática: o contrato de autenticação é exatamente o da Meta
(HMAC-SHA256 do corpo cru com o app secret do 0000), e continua valendo se um
dia o callback vier direto para cá. O que muda é quem chama, não o que se
valida. Ver `projetos/acme/assistente-ia/PORTA-DE-ENTRADA.md`.

## Segredos

O app secret e o verify token são lidos **do disco, no momento do uso**, como o
`painel-corretor` já faz — nunca ficam em `settings`, em variável de ambiente, em
argumento de função ou em log. Arquivo ausente ou chave vazia = fail-closed
(403), nunca "passa porque não consegui conferir".

O arquivo do verify token (`/etc/agencia/assistente-webhook.env`) ainda **não existe**:
enquanto não existir, o GET devolve 403, que é o comportamento certo para uma
rota que hoje não é assinada na Meta.

## Registro

Este router É montado em `app/main.py` desde 21/09/2026 (OS-ASSISTENTE-SUBIR-LIGADA,
autorização da Dona). Os routers de **cadastro** e **envio** continuam fora de
lá, e `test_router_de_cadastro_nao_esta_registrado_em_main` afirma isso.

Desligar não é mexer aqui: é apagar a linha `IG_ACME` de
`/etc/agencia/corretor-rotas.env` e reiniciar o `corretor-painel`. Sem ela o repasse não sai
e esta rota fica de pé sem receber nada. Uma trava só, no lugar onde quem for
desligar às pressas vai procurar.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

import structlog
from fastapi import APIRouter, Header, HTTPException, Query, Request, Response

from app.services.instagram_acme import ingest
from app.services.instagram_acme.contracts import ROTA_WEBHOOK

log = structlog.get_logger()

router = APIRouter(tags=["instagram-acme"])

#: App secret do app Meta 0000 — o MESMO que assina os eventos que o `painel-corretor`
#: recebe e repassa. Lido na hora do uso; o valor nunca sai desta função.
ARQUIVO_APP_SECRET = "/etc/agencia/review-app.env"
CHAVE_APP_SECRET = "META_APP_SECRET"

#: Verify token do handshake. Arquivo próprio da Assistente, ainda não criado.
ARQUIVO_VERIFY_TOKEN = "/etc/agencia/assistente-webhook.env"
CHAVE_VERIFY_TOKEN = "ASSISTENTE_VERIFY_TOKEN"


def _segredo(arquivo: str, chave: str) -> str:
    """Lê `chave=valor` do arquivo. Devolve "" quando não dá — e "" fecha a porta.

    Mesmo padrão de `painel-corretor/webhook.py::_cfg`: abre, lê, usa, esquece. Sem
    cache em variável de módulo, para que rotacionar o segredo não exija
    restart, e sem devolver o valor para log ou resposta.
    """
    try:
        for linha in Path(arquivo).read_text().splitlines():
            if linha.startswith(chave + "="):
                return linha.split("=", 1)[1].strip().strip('"').strip("'")
    except (OSError, PermissionError):
        return ""
    return ""


def _assinatura_valida(corpo: bytes, cabecalho: str | None) -> bool:
    """HMAC-SHA256 do CORPO CRU, comparação em tempo constante, fail-closed.

    Sobre os bytes exatos: o repasse preserva o corpo original justamente porque
    reserializar o JSON muda os bytes e invalida a assinatura.
    """
    segredo = _segredo(ARQUIVO_APP_SECRET, CHAVE_APP_SECRET)
    if not segredo or not cabecalho or not cabecalho.startswith("sha256="):
        return False
    enviado = cabecalho.split("=", 1)[1]
    esperado = hmac.new(segredo.encode("utf-8"), corpo, hashlib.sha256).hexdigest()
    return hmac.compare_digest(enviado, esperado)


@router.get(ROTA_WEBHOOK)
def verificar_webhook(
    hub_mode: str | None = Query(default=None, alias="hub.mode"),
    hub_verify_token: str | None = Query(default=None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(default=None, alias="hub.challenge"),
) -> Response:
    """Handshake da Meta. Fail-closed: verify token vazio/ausente = 403."""
    esperado = _segredo(ARQUIVO_VERIFY_TOKEN, CHAVE_VERIFY_TOKEN)
    if (
        not esperado
        or hub_mode != "subscribe"
        or not hub_verify_token
        or not hmac.compare_digest(hub_verify_token, esperado)
    ):
        log.warning("assistente_webhook_handshake_recusado", mode=hub_mode)
        raise HTTPException(403, "forbidden")
    log.info("assistente_webhook_handshake_ok")
    return Response(content=hub_challenge or "", media_type="text/plain")


@router.post(ROTA_WEBHOOK)
async def receber_webhook(
    request: Request,
    x_hub_signature_256: str | None = Header(default=None, alias="X-Hub-Signature-256"),
) -> dict[str, Any]:
    """Entrega de eventos (hoje: repasse do `painel-corretor`).

    `async` só para ler o corpo cru — a assinatura tem que bater sobre os bytes
    exatos, não sobre o JSON reserializado.

    Nunca devolve 5xx: quem está do outro lado é um repassador que registra
    falha e reentrega, e antes dele a Meta, que desativa assinatura quando o
    endpoint erra. Erro nosso vira 200 com motivo no corpo e no log.
    """
    corpo = await request.body()

    if not _assinatura_valida(corpo, x_hub_signature_256):
        log.warning("assistente_webhook_assinatura_invalida")
        raise HTTPException(403, "invalid signature")

    try:
        payload = json.loads(corpo or b"{}")
    except json.JSONDecodeError:
        log.warning("assistente_webhook_corpo_nao_json")
        return {"ok": True, "modo": "sombra", "status": "corpo_invalido", "recebidos": 0}

    if not isinstance(payload, dict):
        log.warning("assistente_webhook_corpo_nao_objeto")
        return {"ok": True, "modo": "sombra", "status": "corpo_invalido", "recebidos": 0}

    try:
        resumo = ingest.processar_lote(payload)
    except Exception as exc:  # noqa: BLE001
        # Persistir falhou. NÃO confirmamos processamento: devolvemos 200 (para
        # não derrubar a assinatura) mas com status explícito, e o repassador
        # tem o evento guardado em corretor.repasses para reentregar.
        log.exception("assistente_webhook_ingestao_falhou", erro=str(exc)[:200])
        return {"ok": False, "modo": "sombra", "status": "ingestao_falhou", "recebidos": 0}

    log.info(
        "assistente_webhook_recebido",
        recebidos=resumo.recebidos,
        novos=resumo.novos,
        repetidos=resumo.repetidos,
        ignorados=len(resumo.ignorados),
    )
    return resumo.como_resposta()
