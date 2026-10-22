"""Lead Spine — dual-write EM SOMBRA da espinha canônica do lead (Opção A).

O nosso Postgres é o DONO da verdade do lead. Este serviço grava/atualiza o lead
nas tabelas canônicas MULTI-TENANT (`leads`, `pipeline_stages`, `lead_events`) a
partir dos dados que já lemos do Kommo.

É SHADOW: NÃO muda o comportamento de leitura atual. O Kommo segue como fonte de
leitura enquanto `settings.lead_spine_shadow` estiver ligado. Serve pra validar a
migração antes de virar a chave. Reversível: `LEAD_SPINE_SHADOW=false` desliga.

Idempotência: a chave de negócio é (tenant_id, external_ref=kommo_id). Rodar
`upsert_lead_shadow` N vezes com os mesmos dados converge pro mesmo estado — 1 lead,
sem eventos duplicados. Só gera `lead_events(tipo='stage_change')` quando o stage
REALMENTE muda de um valor pra outro.

Uso (best-effort, nunca derruba o fluxo principal):

    from app.services.lead_spine import upsert_lead_shadow
    try:
        upsert_lead_shadow(dados_kommo, tenant_id=1)
    except Exception:
        log.exception("lead_spine_shadow_failed")
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

import structlog
from sqlalchemy import func as sa_func
from sqlalchemy import or_ as sa_or
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import Corretora, Lead, LeadEvent, PipelineStage
from app.services.phone import (
    canonical_phone,
    normalize_phone_kommo,
    same_ddd,
    same_phone,
)

# Nome do funil de remarketing/nutrição em `pipeline_stages.funil` (migration
# 0046). Duplicado aqui de propósito: importar app.services.nutricao criaria
# ciclo (nutricao → lead_spine).
FUNIL_REMARKETING = "remarketing"

log = structlog.get_logger()

# Tenant default = Acme (id 1). Multi-tenant: a plataforma agencia passa o seu.
DEFAULT_TENANT_ID = 1

# Nomes que identificam a etapa Follow-up na espinha: o inspector passa o nome
# real do Kommo ("Follow-up"); o webhook fino só tem o status_id (10000001,
# canônico em 0036_create_stage_map / cloud_corretora_a.KOMMO_STAGE_FOLLOWUP).
_FOLLOWUP_STAGE_NOMES = {"follow-up", "followup", "10000001"}

# Janela de proteção do move MANUAL feito no kanban V2 (B7): dentro dela, o
# espelho do Kommo (Inspector/webhook) NÃO sobrescreve leads.stage_id — senão
# o robô da manhã desfaz o arrasto da Dona/corretora.
_STAGE_MANUAL_TTL_HORAS = 72


def _default_session_factory() -> Session:
    # Import tardio: mantém o módulo importável em testes sem engine de prod.
    from app.db.session import SessionLocal

    return SessionLocal()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_str(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _as_decimal(v: Any) -> Decimal | None:
    """Converte o `price` do Kommo (int/float/str) pra Decimal. Tolerante:
    None/vazio/inválido → None. Não gera evento — é só o valor de negócio.
    """
    if v is None:
        return None
    try:
        s = str(v).strip()
        return Decimal(s) if s else None
    except (InvalidOperation, ValueError, TypeError):
        return None


# ── Funil canônico de 8 (ditado pela Dona 10/07) ──────────────────────────────
# A sombra NUNCA cria coluna fora dessas 8. Etapa que chega do Kommo é traduzida
# pelo alias abaixo; nome desconhecido → stage None (lead cai em "Sem etapa" no
# board) + log — melhor que poluir o kanban com coluna órfã.
STAGES_CANONICOS: tuple[str, ...] = (
    "Novo", "Em atendimento", "Follow-up", "Visita marcada",
    "Compareceu", "Proposta", "Venda ganha", "Venda perdida",
)

# Kommo (nomes reais do pipeline Acme + órfãs já vistas em produção) → canônico.
# Chaves em lowercase — o lookup normaliza com .strip().lower().
_STAGE_ALIASES: dict[str, str] = {
    # entrada
    "leads de entrada": "Novo",
    "entrada": "Novo",
    "entrou": "Novo",
    "primeiro contato": "Novo",       # órfã vista em produção (item 3 blueprint)
    "provisoria": "Novo",
    "provisória": "Novo",
    # atendimento
    "em atendimento": "Em atendimento",
    "recuperados": "Em atendimento",
    # follow-up
    "follow-up": "Follow-up",
    "followup": "Follow-up",
    "10000001": "Follow-up",          # etapa_id cru do Follow-up Kommo (órfã, item 3)
    # visita
    "visita agendada": "Visita marcada",
    "visita marcada": "Visita marcada",
    "visita realizada": "Compareceu",
    "compareceu": "Compareceu",
    # negociação
    "negociacao": "Proposta",
    "negociação": "Proposta",
    "proposta": "Proposta",
    "documentacao": "Proposta",
    "documentação": "Proposta",
    # fechamento
    "venda": "Venda ganha",
    "venda ganha": "Venda ganha",
    "perdida": "Venda perdida",
    "venda perdida": "Venda perdida",
    # canônicos que não colidem acima
    "novo": "Novo",
}


def stage_canonico(nome: str | None) -> str | None:
    """Nome canônico (um dos 8) pra uma etapa vinda do Kommo/sombra, ou None
    se desconhecido. Público: o board (/painel/crm/board) usa pra dobrar
    colunas órfãs já existentes na coluna canônica."""
    if not nome:
        return None
    return _STAGE_ALIASES.get(str(nome).strip().lower())


def _resolve_stage_id(
    session: Session,
    tenant_id: int,
    etapa_id: Any,
    etapa_nome: Any,
) -> int | None:
    """Upsert-shadow do stage no catálogo canônico. Retorna pipeline_stages.id.

    Chave de negócio do stage no shadow: (tenant_id, nome CANÔNICO). O nome que
    chega do Kommo (ou o etapa_id como texto, sem nome) é traduzido pro funil
    de 8 via `stage_canonico`. Nome fora do funil → None (não cria coluna nova)
    + log. Sem nome e sem id → None.
    """
    bruto = _as_str(etapa_nome) or _as_str(etapa_id)
    if bruto is None:
        return None

    nome = stage_canonico(bruto)
    if nome is None:
        log.warning("lead_spine_stage_fora_do_funil", etapa=bruto[:60])
        return None

    stage = (
        session.query(PipelineStage)
        .filter(PipelineStage.tenant_id == tenant_id, PipelineStage.nome == nome)
        .first()
    )
    if stage is None:
        stage = PipelineStage(tenant_id=tenant_id, nome=nome)
        session.add(stage)
        session.flush()  # popula stage.id
    return stage.id


def resolve_stage_id_por_funil(
    session: Session,
    tenant_id: int,
    funil: str,
    stage_nome: str,
) -> int | None:
    """Resolve pipeline_stages.id de uma etapa EXATA dentro de um funil.

    Diferente de `_resolve_stage_id` (que só conhece o funil principal de 8 e
    canonicaliza via aliases): aqui o nome é LITERAL e o filtro inclui `funil`,
    porque nomes podem colidir entre funis (não hoje, mas o move manual precisa
    escrever no funil certo). Não cria coluna nova — retorna None se a etapa não
    existir naquele funil (o caller já validou o nome contra a lista do funil).
    """
    nome = (stage_nome or "").strip()
    if not nome:
        return None
    stage = (
        session.query(PipelineStage)
        .filter(
            PipelineStage.tenant_id == tenant_id,
            PipelineStage.funil == funil,
            PipelineStage.nome == nome,
        )
        .order_by(PipelineStage.id.asc())
        .first()
    )
    if stage is None:
        log.warning(
            "lead_spine_stage_funil_inexistente", funil=funil, etapa=nome[:60]
        )
        return None
    return stage.id


# ── Identidade do lead (L3) — match ÚNICO compartilhado pelos writers ────────
#
# Antes: o intake dedupava por telefone (sufixo-8) e o shadow do Kommo SÓ por
# external_ref. Os dois não se enxergavam, então a mesma pessoa entrava 2x —
# uma pelo external_ref da Meta (leadgen_id) e outra pelo do Kommo (lead_id).
# Prova em produção (26/07): leads 6474/6476 "Fulano" e 1219/6475
# "Edson Edson", mesmo telefone, 2 refs diferentes, 2 cards no board da Pessoa11.
#
# Agora os DOIS writers chamam `encontrar_lead`, nesta ordem:
#   1. external_ref exato (chave de negócio do shadow, tem UNIQUE)
#   2. telefone canônico COMPLETO + mesmo DDD
# Sem match → lead NOVO. Não existe mais fallback frouxo `candidatos[0]`.


def encontrar_lead(
    session: Session,
    tenant_id: int,
    *,
    external_ref: str | None = None,
    telefone: str | None = None,
    excluir_remarketing: bool = False,
) -> Lead | None:
    """Lead canônico já existente pra esta identidade, ou None.

    Args:
        external_ref: id externo (leadgen_id Meta / lead_id Kommo). Match exato.
        telefone: qualquer formato; comparado na forma canônica completa.
        excluir_remarketing: quando True, leads em etapa do funil `remarketing`
            NÃO casam por telefone. Usado pelo shadow do Kommo: sem isso o sync
            "encontraria" linhas da base 0000 e arrastaria lead de remarketing
            pro funil principal — dano maior que a duplicata que ele conserta.

    O match por telefone exige DDD IGUAL e string canônica COMPLETA. Telefone
    ambíguo (12 díg começando com 7/8) nunca casa — vira lead novo pra triagem.
    """
    if external_ref is not None:
        lead = (
            session.query(Lead)
            .filter(Lead.tenant_id == tenant_id, Lead.external_ref == external_ref)
            .order_by(Lead.id.asc())
            .first()
        )
        if lead is not None:
            return lead

    canon = canonical_phone(telefone)
    if canon is None:
        return None

    # Pré-filtro barato pelo sufixo-8 (usa índice/LIKE), decisão fina em Python
    # com a forma canônica — sufixo-8 sozinho colide entre DDDs.
    q = session.query(Lead).filter(
        Lead.tenant_id == tenant_id, Lead.telefone.like(f"%{canon[-8:]}")
    )
    if excluir_remarketing:
        ids_remarketing = [
            row[0]
            for row in session.query(PipelineStage.id)
            .filter(
                PipelineStage.tenant_id == tenant_id,
                PipelineStage.funil == FUNIL_REMARKETING,
            )
            .all()
        ]
        if ids_remarketing:
            q = q.filter(
                sa_or(Lead.stage_id.is_(None), Lead.stage_id.notin_(ids_remarketing))
            )

    # order_by determinístico: sem ele a escolha era a ordem de heap do Postgres.
    for cand in q.order_by(Lead.id.asc()).all():
        if same_phone(cand.telefone, canon) and same_ddd(cand.telefone, canon):
            return cand
    return None


def upsert_lead_shadow(
    dados_kommo: dict[str, Any],
    tenant_id: int = DEFAULT_TENANT_ID,
    session_factory: Callable[[], Session] | None = None,
) -> int | None:
    """Grava/atualiza o lead na espinha canônica (shadow). Idempotente.

    Args:
        dados_kommo: dict como o pipeline/inspector já monta a partir do Kommo.
            Campos usados (todos tolerantes a ausência):
              - lead_id / id           → external_ref (obrigatório; sem ele, no-op)
              - nome                    → leads.nome
              - telefone                → leads.telefone
              - corretora_id / owner_id → leads.owner_id
              - etapa_nome / etapa_id   → resolve stage_id
              - origem                  → leads.origem
        tenant_id: tenant dono do lead (1 = Acme).
        session_factory: fábrica de Session (injeção pra teste). Default = SessionLocal.

    Returns:
        leads.id (canônico) ou None se sem external_ref / shadow desligado.

    NÃO faz try/except interno — o caller decide como tratar (best-effort no
    webhook/inspector). Aqui a exceção sobe pra ficar visível em teste.
    """
    if not settings.lead_spine_shadow:
        return None

    external_ref = _as_str(dados_kommo.get("lead_id") or dados_kommo.get("id"))
    if external_ref is None:
        log.info("lead_spine_shadow_skip", motivo="sem_external_ref")
        return None

    make_session = session_factory or _default_session_factory
    session = make_session()
    try:
        nome = _as_str(dados_kommo.get("nome"))
        telefone = _as_str(dados_kommo.get("telefone"))
        owner_id = dados_kommo.get("corretora_id")
        if owner_id is None:
            owner_id = dados_kommo.get("owner_id")
        try:
            owner_id = int(owner_id) if owner_id is not None else None
        except (TypeError, ValueError):
            owner_id = None
        origem = _as_str(dados_kommo.get("origem"))
        # Valor de negócio: campo `price` do lead Kommo (fallback `valor`).
        valor = _as_decimal(dados_kommo.get("price"))
        if valor is None:
            valor = _as_decimal(dados_kommo.get("valor"))

        stage_nome = _as_str(dados_kommo.get("etapa_nome")) or _as_str(
            dados_kommo.get("etapa_id")
        )
        stage_id = _resolve_stage_id(
            session, tenant_id,
            dados_kommo.get("etapa_id"), dados_kommo.get("etapa_nome"),
        )

        # Match único (L3): external_ref primeiro; se não achar, telefone
        # canônico. Leads em funil remarketing ficam FORA do match por telefone.
        lead = encontrar_lead(
            session, tenant_id,
            external_ref=external_ref, telefone=telefone,
            excluir_remarketing=True,
        )
        # Casou por TELEFONE, não por ref: é o lead que o intake da Meta já
        # criou. O shadow do Kommo aqui é o writer SECUNDÁRIO — ele completa
        # buracos, mas NÃO manda em etapa nem em dono. Sem isso, o inspector
        # (2x/dia) reverteria o stage que a corretora moveu no painel e ainda
        # dispararia stage_change espúrio, que alimenta o auto-enroll da
        # cadência. Ver briefing L3 item 3.
        casou_por_telefone = lead is not None and lead.external_ref != external_ref

        if lead is None:
            lead = Lead(
                tenant_id=tenant_id,
                external_ref=external_ref,
                nome=nome,
                telefone=telefone,
                owner_id=owner_id,
                stage_id=stage_id,
                origem=origem,
                valor=valor,
                created_at=_now(),
                updated_at=_now(),
            )
            session.add(lead)
            session.flush()  # popula lead.id
            if stage_id is not None:
                _registrar_evento(
                    session, tenant_id, lead.id, "lead_created",
                    {"stage_id": stage_id, "external_ref": external_ref},
                )
            session.commit()
            log.info("lead_spine_shadow_created", external_ref=external_ref, lead_id=lead.id)
            return lead.id

        # Update: só mexe no que veio; registra stage_change se o stage mudou.
        stage_anterior = lead.stage_id
        stage_mudou = stage_id is not None and stage_id != stage_anterior

        if casou_por_telefone:
            # Writer secundário: só preenche VAZIO, nunca sobrescreve. Etapa e
            # dono são do writer primário (intake/painel). O ref do Kommo fica
            # em lead_events pra rastreabilidade — external_ref tem UNIQUE por
            # (tenant, ref) e já pertence à identidade da Meta.
            if lead.nome is None and nome is not None:
                lead.nome = nome
            if lead.telefone is None and telefone is not None:
                lead.telefone = telefone
            if lead.origem is None and origem is not None:
                lead.origem = origem
            if lead.valor is None and valor is not None:
                lead.valor = valor
            if lead.owner_id is None and owner_id is not None:
                lead.owner_id = owner_id
            lead.updated_at = _now()
            if not _ref_kommo_ja_vinculado(session, tenant_id, lead.id, external_ref):
                _registrar_evento(
                    session, tenant_id, lead.id, "lead_ref_vinculado",
                    {
                        "external_ref_kommo": external_ref,
                        "external_ref_lead": lead.external_ref,
                        "stage_kommo_ignorado": stage_id,
                        "owner_kommo_ignorado": owner_id,
                    },
                )
            session.commit()
            log.info(
                "lead_spine_shadow_match_telefone",
                external_ref=external_ref, lead_id=lead.id,
                external_ref_lead=lead.external_ref,
                stage_preservado=stage_anterior,
            )
            return lead.id

        # ── Anticonflito (B7): move manual no V2 vence a sombra do Kommo ─────
        # Existe lead_events tipo='stage_manual_v2' mais recente que a janela
        # de proteção (72h)? Então o stage manual PREVALECE — o dado do Kommo é
        # mais velho que a intenção de quem arrastou o card. NUNCA escrevemos o
        # stage de volta no Kommo (histórico de incidente de loop de PATCH).
        if stage_mudou and _manual_v2_recente(session, tenant_id, lead.id):
            log.info(
                "lead_spine_stage_manual_preservado",
                external_ref=external_ref, lead_id=lead.id,
                stage_mantido=stage_anterior, stage_kommo_ignorado=stage_id,
            )
            stage_mudou = False
            stage_id = None  # não aplica o stage vindo do Kommo

        # Proteção do sync (migration 0049): nome editado à mão no painel vence
        # o shadow do Kommo — não sobrescrevemos `nome`. O resto do sync segue.
        if nome is not None and not lead.nome_editado_manual:
            lead.nome = nome
        # Proteção do sync (migration 0050): telefone corrigido à mão no painel
        # vence o shadow do Kommo — não sobrescrevemos `telefone`.
        if telefone is not None and not lead.telefone_editado_manual:
            lead.telefone = telefone
        # Proteção do sync (migration 0061): dono trocado à mão no painel
        # (transferência de lead entre corretoras) vence o shadow do Kommo —
        # não sobrescrevemos `owner_id`.
        if owner_id is not None and not lead.owner_editado_manual:
            lead.owner_id = owner_id
        if origem is not None:
            lead.origem = origem
        if valor is not None:
            lead.valor = valor
        if stage_id is not None:
            lead.stage_id = stage_id
        lead.updated_at = _now()

        if stage_mudou:
            _registrar_evento(
                session, tenant_id, lead.id, "stage_change",
                {"de": stage_anterior, "para": stage_id, "external_ref": external_ref},
            )
            log.info(
                "lead_spine_shadow_stage_change",
                external_ref=external_ref, lead_id=lead.id,
                de=stage_anterior, para=stage_id,
            )

        session.commit()

        # ── Enroll-on-followup (cadência invisível) — best-effort ────────────
        # Lead MOVIDO pra Follow-up → matricula na cadência (só Pessoa12, atrás de
        # flag). Depois do commit do stage_change: falha aqui não desfaz nada.
        if stage_mudou and _is_followup_stage(stage_nome):
            try:
                _enroll_cadencia_followup(session, lead, tenant_id)
                session.commit()
            except Exception:
                session.rollback()
                log.exception(
                    "cadencia_enroll_followup_failed",
                    origem="lead_spine", lead_id=lead.id,
                )

        return lead.id
    finally:
        session.close()


def _registrar_evento(
    session: Session,
    tenant_id: int,
    lead_id: int,
    tipo: str,
    payload: dict[str, Any],
) -> None:
    session.add(
        LeadEvent(tenant_id=tenant_id, lead_id=lead_id, tipo=tipo, payload=payload, at=_now())
    )


def _ref_kommo_ja_vinculado(
    session: Session, tenant_id: int, lead_id: int, external_ref: str
) -> bool:
    """True se este ref do Kommo já foi registrado neste lead (idempotência do
    evento — o inspector passa 2x/dia e não pode empilhar evento igual)."""
    for (payload,) in (
        session.query(LeadEvent.payload)
        .filter(
            LeadEvent.tenant_id == tenant_id,
            LeadEvent.lead_id == lead_id,
            LeadEvent.tipo == "lead_ref_vinculado",
        )
        .all()
    ):
        if isinstance(payload, dict) and payload.get("external_ref_kommo") == external_ref:
            return True
    return False


def _manual_v2_recente(session: Session, tenant_id: int, lead_id: int) -> bool:
    """True se o lead tem move MANUAL do kanban V2 (lead_events
    tipo='stage_manual_v2') dentro da janela de proteção (72h). Nesse caso o
    upsert da sombra preserva o stage manual em vez de aplicar o do Kommo."""
    at_max = (
        session.query(sa_func.max(LeadEvent.at))
        .filter(
            LeadEvent.tenant_id == tenant_id,
            LeadEvent.lead_id == lead_id,
            LeadEvent.tipo == "stage_manual_v2",
        )
        .scalar()
    )
    if at_max is None:
        return False
    if at_max.tzinfo is None:  # SQLite (testes) devolve datetime naive
        at_max = at_max.replace(tzinfo=timezone.utc)
    return at_max >= _now() - timedelta(hours=_STAGE_MANUAL_TTL_HORAS)


def _is_followup_stage(stage_nome: str | None) -> bool:
    return bool(stage_nome) and stage_nome.strip().lower() in _FOLLOWUP_STAGE_NOMES


def _enroll_cadencia_followup(session: Session, lead: Lead, tenant_id: int) -> int | None:
    """Enroll-on-followup: lead que ENTROU na etapa Follow-up → matricula na
    cadência, PELA LINHA DA DONA. Cadência invisível — sem módulo próprio: mover
    o card pro Follow-up liga a máquina; o painel mostra só a tag CD{n}.

    Multi-linha (27/07): deixou de ser "só a Pessoa12". Vale pra QUALQUER corretora com
    linha Cloud registrada (hoje Pessoa12 + Pessoa10). Corretora sem linha (Pessoa11, ainda no
    Evolution) NÃO é matriculada — fail-closed e silencioso, sem exception.

    Atrás da flag `settings.cadencia_auto_enroll` (mesma do auto-enroll do
    webhook). A corretora é identificada por `corretoras.evolution_instance` via
    o registry de linhas (a conta da Pessoa12 no Kommo aparece como "Pessoa Pessoa9" —
    NUNCA casar por nome); `owner_id` do lead na espinha pode ser o id interno
    (inspector) OU o kommo_user_id (webhook fino) — `linha_da_corretora` aceita
    os dois. Idempotente via `iniciar_cadencia`.

    Levanta em erro — o caller (upsert_lead_shadow) trata como best-effort.
    """
    if not settings.cadencia_auto_enroll:
        return None

    telefone = normalize_phone_kommo(lead.telefone)
    if telefone is None:
        log.info(
            "cadencia_enroll_followup_skip", motivo="sem_telefone_valido", lead_id=lead.id
        )
        return None

    if lead.owner_id is None:
        log.info(
            "cadencia_enroll_followup_skip", motivo="sem_dono", lead_id=lead.id
        )
        return None

    from app.services import cloud_lines
    from app.services.cadencia import iniciar_cadencia

    # A dona tem linha Cloud? Sem linha = fora do regime de automação. Cadastro
    # ilegível (INDETERMINADO) também NÃO matricula: é decisão nova, e matricular
    # às cegas ligaria a máquina pela linha errada.
    linha = cloud_lines.linha_da_corretora(lead.owner_id, session=session)
    if linha is None or linha is cloud_lines.INDETERMINADO:
        log.info(
            "cadencia_enroll_followup_skip", motivo="dona_sem_linha_cloud",
            lead_id=lead.id, owner_id=lead.owner_id,
        )
        return None

    # `corretora_id` do estado: SEMPRE o id INTERNO da corretora (o owner_id do
    # lead pode ser o kommo_user_id). É por ele que o tick resolve a linha e
    # contabiliza o cap por linha depois.
    instance = cloud_lines.instance_da_linha(linha)
    dona = (
        session.query(Corretora)
        .filter(Corretora.evolution_instance == instance, Corretora.ativo.is_(True))
        .first()
    )
    if dona is None:
        log.info(
            "cadencia_enroll_followup_skip", motivo="dona_nao_cadastrada",
            lead_id=lead.id, linha=linha.slug,
        )
        return None

    estado = iniciar_cadencia(
        session,
        telefone=telefone,
        lead_id=lead.external_ref,
        corretora_id=dona.id,
        tenant_id=tenant_id,
    )
    log.info(
        "cadencia_enroll_followup_ok",
        cadencia_id=estado.id, lead_id=lead.id, telefone=telefone[-4:],
        linha=linha.slug, corretora_id=dona.id,
    )
    return estado.id
