"""Meta leadgen em MODO SOMBRA (B6 · blueprint V2) — serviço do receptor.

Caminho do lead HOJE (mapeado 24/07/2026): Meta Lead Ads (app 0000, callback
global) → automacao-kommo `POST /api/webhook/meta-leads` (app.example.com,
:8766) → Graph API (field_data + campaign_name) → cria lead no Kommo → o
copiloto só ESPELHA via lead_spine/kommo_webhook.

A SOMBRA: o automacao-kommo repassa o body original do webhook pro receptor
próprio do copiloto (`POST /intake/meta-leadgen/{secret}`), que:

  1. SEMPRE grava em `meta_leads_sombra` (idempotente por leadgen_id) — é a
     auditoria de que o cano direto funciona;
  2. SE a flag de intake estiver ligada (`config_operacional.lead_intake_ativo()`
     — painel > LEAD_INTAKE_ATIVO): repassa pro fluxo interno
     `intake_lead()` (chamada de função, não HTTP). Com a flag off, só loga
     `meta_leadgen_sombra` com o que FARIA.

Nenhum lead real muda de fluxo enquanto a flag estiver off.

Token da Graph: reusa o META_ACCESS_TOKEN do .env do automacao-kommo
(`settings.meta_leadgen_env_file`) — validado 24/07 que ele lê o nó do
leadgen com `campaign_name`/`ad_name` direto. NUNCA logar/expor o token.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable

import httpx
import structlog
from sqlalchemy.orm import Session

from app.config import settings
from app.services import config_operacional
from app.db.models import MetaLeadSombra

log = structlog.get_logger()

DEFAULT_TENANT_ID = 1

_GRAPH = "https://graph.facebook.com/v21.0"
# Campos do nó de leadgen — campaign_name/ad_name vêm direto, sem hop por ad_id
LEADGEN_FIELDS = "field_data,created_time,ad_id,form_id,campaign_name,ad_name"

# Cobre user tokens e System User tokens (padrão do meta_client do automacao-kommo)
_TOKEN_RE = re.compile(r"EAA[A-Za-z0-9_-]{20,}")
_RE_NON_DIGIT = re.compile(r"\D")
_RE_PHONE_ALLOWED = re.compile(r"^[\d\s\+\-\(\)\.]+$")

# Fetcher injetável (teste passa fake; default = Graph API real)
FetcherFn = Callable[[str], dict[str, Any]]


def _redact(texto: str | None) -> str:
    """Remove qualquer token-like de uma string antes de logar/propagar."""
    if not texto:
        return ""
    return _TOKEN_RE.sub("EAA***REDACTED***", texto)


def _load_meta_token() -> str | None:
    """Lê o META_ACCESS_TOKEN do .env do automacao-kommo (nunca loga o valor).

    Parse manual linha a linha — NUNCA `source` em .env (gotcha da casa §4).
    """
    path = Path(settings.meta_leadgen_env_file)
    if not path.exists():
        return None
    for linha in path.read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if linha.startswith("META_ACCESS_TOKEN="):
            valor = linha.split("=", 1)[1].strip().strip('"').strip("'")
            return valor or None
    return None


def fetch_leadgen(leadgen_id: str) -> dict[str, Any]:
    """Busca os dados completos do leadgen na Graph API.

    Token via header Authorization (NUNCA query string). Erros sobem como
    RuntimeError com corpo sanitizado (sem token).
    """
    token = _load_meta_token()
    if not token:
        raise RuntimeError(
            f"META_ACCESS_TOKEN não encontrado em {settings.meta_leadgen_env_file}"
        )
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


def _is_phone_valido(p: str | None) -> bool:
    """Só strings com >=10 dígitos e majoritariamente numéricas (bug Maio/2026:
    texto livre no campo customizado). Mesma regra do automacao-kommo."""
    if not p:
        return False
    if len(_RE_NON_DIGIT.sub("", p)) < 10:
        return False
    return bool(_RE_PHONE_ALLOWED.match(p.strip()))


def parse_field_data(field_data: list[dict[str, Any]]) -> dict[str, str]:
    """Extrai nome e telefone do formulário Meta.

    Adaptado 1:1 do `_parse_field_data` do automacao-kommo (fonte de verdade do
    fluxo atual): prefere `qual_seu_whatsapp?` (campo customizado Acme) se
    válido; senão cai no `phone_number` autopreenchido pelo Facebook (casos
    reais Renan/Miguel/Paula 05/2026).
    """
    result: dict[str, str] = {}
    first_name = ""
    last_name = ""
    phone_custom = ""
    phone_meta = ""

    for field in field_data:
        key = str(field.get("name", "")).lower()
        values = field.get("values") or [""]
        value = values[0]
        if not value:
            continue
        if key in {"full_name", "nome", "name"}:
            result["nome"] = value
        elif key == "first_name":
            first_name = value
        elif key == "last_name":
            last_name = value
        elif key == "qual_seu_whatsapp?":
            phone_custom = value
        elif key == "phone_number":
            phone_meta = value

    if phone_custom and _is_phone_valido(phone_custom):
        result["telefone"] = phone_custom
    elif phone_meta and _is_phone_valido(phone_meta):
        result["telefone"] = phone_meta

    if "nome" not in result and (first_name or last_name):
        result["nome"] = f"{first_name} {last_name}".strip()

    return result


def _default_session_factory() -> Session:
    # Import tardio: mantém o módulo importável em testes sem engine de prod.
    from app.db.session import SessionLocal

    return SessionLocal()


def _default_intake(**kwargs: Any) -> Any:
    # Import tardio pra evitar ciclo e facilitar patch em teste.
    from app.services.lead_intake import intake_lead

    return intake_lead(**kwargs)


def processar_leadgen_value(
    value: dict[str, Any],
    *,
    tenant_id: int = DEFAULT_TENANT_ID,
    fetcher: FetcherFn | None = None,
    session_factory: Callable[[], Session] | None = None,
    intake_fn: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Processa UM change value de leadgen (webhook Meta) em modo sombra.

    SEMPRE grava `meta_leads_sombra` (dedup por leadgen_id). Com a flag
    `lead_intake_ativo` ligada, repassa pro `intake_lead()`; desligada, só
    loga o que FARIA. Best-effort de ponta a ponta: falha na Graph NÃO impede
    a gravação da sombra (raw fica só com o value do webhook).
    """
    leadgen_id = str(value.get("leadgen_id") or "").strip()
    if not leadgen_id:
        log.warning("meta_leadgen_sem_id", value_keys=sorted(value.keys()))
        return {"leadgen_id": None, "status": "sem_leadgen_id"}

    # Mesma trava de página do meta_webhook (26/09, fase 0 do CRM único): este
    # caminho não filtrava nada — gravava a sombra (nome e telefone) e, com a
    # flag ligada, chamava o intake pra lead de QUALQUER página que chegasse
    # aqui. Página não configurada ou de outro cliente: para antes do banco.
    pagina_aceita = str(settings.intake_meta_page_id or "").strip()
    if not pagina_aceita:
        log.error("meta_leadgen_pagina_nao_configurada", leadgen_id=leadgen_id)
        return {"leadgen_id": leadgen_id, "status": "pagina_nao_configurada"}
    if str(value.get("page_id") or "") != pagina_aceita:
        log.info("meta_leadgen_outra_pagina", leadgen_id=leadgen_id,
                 page_id=str(value.get("page_id") or ""))
        return {"leadgen_id": leadgen_id, "status": "outra_pagina"}

    make_session = session_factory or _default_session_factory
    session = make_session()
    try:
        existente = (
            session.query(MetaLeadSombra.id)
            .filter(MetaLeadSombra.leadgen_id == leadgen_id)
            .first()
        )
        if existente is not None:
            log.info("meta_leadgen_duplicado", leadgen_id=leadgen_id)
            return {"leadgen_id": leadgen_id, "status": "duplicado"}

        detalhe: dict[str, Any] = {}
        erro_graph: str | None = None
        try:
            detalhe = (fetcher or fetch_leadgen)(leadgen_id)
        except Exception as exc:
            erro_graph = _redact(str(exc))[:300]
            log.warning("meta_leadgen_graph_falhou", leadgen_id=leadgen_id, erro=erro_graph)

        campos = parse_field_data(detalhe.get("field_data", []))
        nome = campos.get("nome")
        telefone = campos.get("telefone")
        campanha = detalhe.get("campaign_name") or None
        ad_id = str(value.get("ad_id") or detalhe.get("ad_id") or "") or None
        form_id = str(value.get("form_id") or detalhe.get("form_id") or "") or None

        raw: dict[str, Any] = {"webhook": value, "graph": detalhe}
        if erro_graph:
            raw["erro_graph"] = erro_graph

        linha = MetaLeadSombra(
            tenant_id=tenant_id,
            leadgen_id=leadgen_id,
            nome=nome,
            telefone=telefone,
            campanha=campanha,
            ad_id=ad_id,
            form_id=form_id,
            raw=raw,
            processado=False,
        )
        session.add(linha)
        session.flush()

        resultado: dict[str, Any] = {"leadgen_id": leadgen_id, "status": "gravado"}

        if config_operacional.lead_intake_ativo() and telefone:
            try:
                intake = (intake_fn or _default_intake)(
                    nome=nome,
                    telefone=telefone,
                    origem="meta_leadgen",
                    external_ref=leadgen_id,
                    tenant_id=tenant_id,
                )
            except Exception as exc:
                # Sombra gravada mesmo assim; intake re-tenta em reenvio futuro? Não —
                # dedup por leadgen_id barra. Fica visível no log pra correção manual.
                log.exception(
                    "meta_leadgen_intake_falhou", leadgen_id=leadgen_id, erro=str(exc)[:200]
                )
                resultado["intake"] = "erro"
            else:
                if intake is not None:
                    linha.processado = True
                    resultado["intake"] = "ok"
                else:  # flag caiu entre o check e a chamada
                    resultado["intake"] = "flag_desligada"
        else:
            # MODO SOMBRA — loga o que FARIA, sem tocar no fluxo real.
            log.info(
                "meta_leadgen_sombra",
                leadgen_id=leadgen_id,
                lead_intake_ativo=config_operacional.lead_intake_ativo(),
                faria="intake_lead(nome=%s, telefone=****%s, origem=meta_leadgen)"
                % (nome, (telefone or "")[-4:]),
                campanha=campanha,
                tem_telefone=bool(telefone),
            )
            resultado["intake"] = "sombra"

        session.commit()
        resultado["processado"] = linha.processado
        return resultado
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
