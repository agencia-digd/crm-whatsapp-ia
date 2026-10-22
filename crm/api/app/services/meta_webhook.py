"""Receptor REAL do webhook de Lead Ads da Meta — serviço (intake próprio).

Este é o cano DIRETO Meta → copiloto (intake próprio, pra aposentar o Kommo).
Diferente do `meta_leadgen.py` (modo SOMBRA, recebe o body repassado pelo
automacao-kommo e autentica por secret no path), aqui a Meta chama o copiloto
direto: verificação por hub.challenge (GET) e assinatura HMAC-SHA256
(X-Hub-Signature-256, POST). Ver `app/api/meta_webhook.py`.

Por evento leadgen:
  1. busca `GET /{leadgen_id}` na Graph API (campos ricos: field_data +
     campaign_name/adset_name/ad_name/form_id) com o Page Access Token;
  2. extrai nome/telefone/email do field_data (reusa `parse_field_data` do
     meta_leadgen — tolerante a variações pt/en);
  3. chama `intake_lead(origem='meta_leadgen', external_ref=leadgen_id, ...)`
     com a origem de campanha/anúncio/form — o intake DEDUPLICA por
     (tenant, telefone) e por external_ref (dedup extra abaixo), então o mesmo
     leadgen_id 2x = 1 lead.

Tudo atrás da flag de intake (`config_operacional.lead_intake_ativo()`,
painel > .env) — o endpoint decide (200
"desligado" sem processar quando off). O token da Graph NUNCA é logado/exposto.
"""
from __future__ import annotations

import re
from typing import Any, Callable

import httpx
import structlog

from app.config import settings
from app.services import config_operacional
from app.services.meta_leadgen import parse_field_data

log = structlog.get_logger()

DEFAULT_TENANT_ID = 1

_GRAPH = "https://graph.facebook.com/v21.0"
# Campos ricos do nó de leadgen — campaign/adset/ad_name + form vêm direto.
LEADGEN_FIELDS = (
    "field_data,created_time,ad_id,adset_id,campaign_id,"
    "ad_name,adset_name,campaign_name,form_id"
)

# Cobre user tokens e System User tokens (padrão do meta_client do automacao-kommo).
_TOKEN_RE = re.compile(r"EAA[A-Za-z0-9_-]{20,}")

# Fetcher injetável (teste passa fake; default = Graph API real).
FetcherFn = Callable[[str], dict[str, Any]]
# intake_lead injetável (teste passa fake).
IntakeFn = Callable[..., Any]


def _redact(texto: str | None) -> str:
    """Remove qualquer token-like de uma string antes de logar/propagar."""
    if not texto:
        return ""
    return _TOKEN_RE.sub("EAA***REDACTED***", texto)


def _load_page_token() -> str | None:
    """Page Access Token com leads_retrieval.

    Ordem: `settings.intake_meta_page_token` (setting próprio) → fallback pro
    META_ACCESS_TOKEN do .env do automacao-kommo (reuso do token que já lê
    leadgen hoje). NUNCA loga o valor.
    """
    if settings.intake_meta_page_token:
        return settings.intake_meta_page_token
    # Fallback: reusa o loader do meta_leadgen (parse manual do .env, sem source).
    from app.services.meta_leadgen import _load_meta_token

    return _load_meta_token()


def fetch_leadgen_rico(leadgen_id: str) -> dict[str, Any]:
    """Busca os dados completos do leadgen na Graph API (campos ricos).

    Token via header Authorization (NUNCA query string). Erros sobem como
    RuntimeError com corpo sanitizado (sem token).
    """
    token = _load_page_token()
    if not token:
        raise RuntimeError("Page Access Token não configurado (intake_meta_page_token)")
    resp = httpx.get(
        f"{_GRAPH}/{leadgen_id}",
        params={"fields": LEADGEN_FIELDS},
        headers={"Authorization": f"Bearer {token}"},
        timeout=15.0,
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"Graph API {resp.status_code} em leadgen {leadgen_id}: "
            f"{_redact(resp.text)[:300]}"
        )
    return resp.json()


def _extrair_email(field_data: list[dict[str, Any]]) -> str | None:
    """Email do field_data (tolerante a variações pt/en)."""
    for field in field_data:
        key = str(field.get("name", "")).lower()
        if key in {"email", "e-mail", "e_mail"}:
            values = field.get("values") or [""]
            valor = (values[0] or "").strip()
            if valor:
                return valor
    return None


def processar_evento_leadgen(
    value: dict[str, Any],
    *,
    tenant_id: int = DEFAULT_TENANT_ID,
    fetcher: FetcherFn | None = None,
    intake_fn: IntakeFn | None = None,
) -> dict[str, Any]:
    """Processa UM change value de leadgen do webhook REAL da Meta.

    Busca a Graph, extrai os dados e chama `intake_lead` com a origem rica.
    Dedup por leadgen_id: o intake é idempotente por telefone/external_ref, e
    aqui não guardamos estado próprio (a espinha já dedup). Best-effort: falha
    na Graph ou sem telefone → status descritivo, sem estourar (o endpoint
    nunca deve dar 500 pra Meta).

    Returns dict com {leadgen_id, status, ...} — usado só em log/resposta
    (NUNCA ecoa nome/telefone).
    """
    leadgen_id = str(value.get("leadgen_id") or "").strip()
    if not leadgen_id:
        log.warning("meta_webhook_sem_id", value_keys=sorted(value.keys()))
        return {"leadgen_id": None, "status": "sem_leadgen_id"}

    # Filtro por página: o receptor é compartilhado, e lead de outra página NÃO
    # entra na espinha da Acme. Estrito de propósito.
    #
    # Nota de 27/07: por um tempo achei que este filtro estava derrubando leads
    # reais (6 leads da Acme não apareceram no painel durante 14h). Não era —
    # a causa era a migration 0063 não aplicada (`leads.telefone_invalido does
    # not exist`), que derrubava o intake DEPOIS do filtro. Lição registrada: ler
    # o erro antes de relaxar uma trava de segurança.
    #
    # 26/09 (fase 0 do CRM único): página NÃO configurada agora FECHA. Antes,
    # vazio aceitava qualquer página — um .env sem INTAKE_META_PAGE_ID fazia o
    # receptor compartilhado botar lead de outro cliente na espinha e mandar a
    # boas-vindas da Acme pra ele.
    pagina_aceita = str(settings.intake_meta_page_id or "").strip()
    if not pagina_aceita:
        log.error("meta_webhook_pagina_nao_configurada", leadgen_id=leadgen_id)
        return {"leadgen_id": leadgen_id, "status": "pagina_nao_configurada"}
    if str(value.get("page_id") or "") != pagina_aceita:
        log.info("meta_webhook_outra_pagina", leadgen_id=leadgen_id,
                 page_id=str(value.get("page_id") or ""))
        return {"leadgen_id": leadgen_id, "status": "outra_pagina"}

    try:
        detalhe = (fetcher or fetch_leadgen_rico)(leadgen_id)
    except Exception as exc:
        erro = _redact(str(exc))[:300]
        log.warning("meta_webhook_graph_falhou", leadgen_id=leadgen_id, erro=erro)
        return {"leadgen_id": leadgen_id, "status": "erro_graph"}

    field_data = detalhe.get("field_data", []) or []
    campos = parse_field_data(field_data)
    nome = campos.get("nome")
    telefone = campos.get("telefone")
    email = _extrair_email(field_data)

    origem_campanha = detalhe.get("campaign_name") or None
    origem_anuncio = detalhe.get("ad_name") or None
    adset_name = detalhe.get("adset_name") or None
    origem_form = (
        str(detalhe.get("form_id") or value.get("form_id") or "") or None
    )

    if not telefone:
        log.warning(
            "meta_webhook_sem_telefone",
            leadgen_id=leadgen_id,
            campanha=origem_campanha,
        )
        return {"leadgen_id": leadgen_id, "status": "sem_telefone"}

    if not config_operacional.lead_intake_ativo():
        # Guard extra (o endpoint já barra com a flag off, mas o serviço é
        # chamável direto — não toca no fluxo real sem a flag).
        log.info("meta_webhook_flag_off", leadgen_id=leadgen_id)
        return {"leadgen_id": leadgen_id, "status": "desligado"}

    try:
        resultado = (intake_fn or _default_intake)(
            nome=nome,
            telefone=telefone,
            origem="meta_leadgen",
            external_ref=leadgen_id,
            origem_campanha=origem_campanha,
            # adset entra no anúncio quando não há ad_name (raro), pra não perder;
            # senão prioriza ad_name.
            origem_anuncio=origem_anuncio or adset_name,
            origem_form=origem_form,
            tenant_id=tenant_id,
        )
    except ValueError as exc:  # telefone inválido/inormalizável
        log.warning("meta_webhook_intake_telefone_invalido", leadgen_id=leadgen_id,
                    erro=str(exc)[:200])
        return {"leadgen_id": leadgen_id, "status": "telefone_invalido"}
    except Exception as exc:
        log.exception("meta_webhook_intake_falhou", leadgen_id=leadgen_id,
                      erro=str(exc)[:200])
        return {"leadgen_id": leadgen_id, "status": "erro_intake"}

    if resultado is None:  # flag caiu entre o check e a chamada
        return {"leadgen_id": leadgen_id, "status": "desligado"}

    log.info(
        "meta_webhook_intake_ok",
        leadgen_id=leadgen_id,
        lead_id=getattr(resultado, "lead_id", None),
        ja_existia=getattr(resultado, "ja_existia", None),
        campanha=origem_campanha,
        tem_email=bool(email),
    )
    return {
        "leadgen_id": leadgen_id,
        "status": "ok",
        "lead_id": getattr(resultado, "lead_id", None),
        "ja_existia": getattr(resultado, "ja_existia", None),
    }


def _default_intake(**kwargs: Any) -> Any:
    # Import tardio pra evitar ciclo e facilitar patch em teste.
    from app.services.lead_intake import intake_lead

    return intake_lead(**kwargs)
