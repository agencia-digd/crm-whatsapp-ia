"""Lead Intake — ENTRADA DO LEAD do Copiloto 2.0 (P0 · brief REDESIGN-2026-07).

Porta de entrada do lead novo (webhook Meta/Kommo → POST /intake/lead). Por lead:

  1. **Upsert na espinha canônica** (`leads`/`pipeline_stages`/`lead_events`,
     mesmo contrato do lead_spine) na etapa "Novo". Idempotência por
     (tenant_id, telefone normalizado) — 2 webhooks do mesmo telefone convergem
     pro MESMO lead, sem duplicar.
  2. **Roleta de distribuição** — hoje SÓ a Pessoa12 (corretora_id=3, linha Cloud 2591).
     O round-robin já está pronto pra N corretoras (ver ROLETA_CORRETORAS_ELEGIVEIS).
  3. **Boas-vindas (lei Dona 10/07)** — automação SEPARADA da cadência: envia
     UMA mensagem de boas-vindas (copy do A/B em `boas_vindas_copies`, balde
     determinístico por lead — a variante que saiu fica no payload do evento)
     pela linha da corretora dona
     (reuso de `enviar_template_corretor`, DRY-RUN atrás de `settings.dry_run`) e
     AGUARDA. Nada mais automático: a Pessoa12 assume o lead manualmente; se ELA
     arrastar pro Follow-up, aí sim a cadência liga — e o D0 da cadência é
     SEMPRE o reencontro (ver `app/services/cadencia.py`). O intake NÃO
     matricula em cadência nenhuma.
     Idempotência do envio: `lead_events(tipo='boas_vindas_enviada')` por lead
     (lead é único por telefone) — repetir o webhook NÃO reenvia.
     ⚠️ EXCEÇÃO: lead ADOTADO POR LIGAÇÃO (tag `leads.adotado_ligacao_em`)
     recebe a copy de CONTINUIDADE e fica FORA do A/B — ele acabou de falar
     por telefone com a corretora, e "você acabou de se cadastrar, certo?"
     seria constrangedor. Ver `app/services/boas_vindas_continuidade.py`.
  4. **Auditoria**: grava `lead_events(tipo='intake')` a cada entrada.

Atrás da flag de intake (default False — deploy seguro). Estado EFETIVO em
`config_operacional.lead_intake_ativo()`: o interruptor da página de boas-vindas
grava no banco e vence o `.env`; sem linha no banco vale LEAD_INTAKE_ATIVO.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

import structlog
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import settings
from app.services import config_operacional
from app.db.models import Corretora, Lead, LeadEvent
from app.services import cloud_lines
from app.services.lead_spine import (  # padrão canônico de stage + match único
    _resolve_stage_id,
    encontrar_lead,
)
from app.services.phone import canonical_phone_info, normalize_phone_kommo

log = structlog.get_logger()

DEFAULT_TENANT_ID = 1

# Etapa canônica de entrada do lead (pipeline_stages tenant 1, chave por nome —
# mesma identidade de stage que o lead_spine usa).
STAGE_ENTRADA = "Novo"

# ── Boas-vindas (lei Dona 10/07 — automação separada da cadência) ────────────
# UMA mensagem quando o lead cai em "Novo", depois AGUARDA a corretora.
#
# Sai via template COM BOTÃO (reeng_b) — SEM "Fico à disposição" (isso é do
# pool ola). Casca:
#   Olá {lead}, tudo bem?
#   {corpo abaixo}
#   Para {CTA}, é só tocar no botão abaixo.
#   [Saiba mais] [Parar mensagens]
#
# Dona 28/07: copy com cadastro + oportunidades + "Toque em Saiba mais…".
# Nome: Pessoa12 = "Pessoa"; Pessoa10/Pessoa11 = nome real.
_BOAS_VINDAS_NOME: dict[int, str] = {
    1: "Pessoa5",
    2: "Pessoa1",
    3: "Pessoa",     # linha da Pessoa12 (2591) — persona da casa, NÃO trocar
}
_BOAS_VINDAS_NOME_PADRAO = "Pessoa"

# {{3}} do template reeng_b → "Para {CTA}, é só tocar no botão abaixo."
BOAS_VINDAS_CTA = "continuarmos com o atendimento"


def nome_da_linha(corretora_id: int | None = None) -> str:
    """Nome que assina a boas-vindas naquela linha (Pessoa12 = 'Pessoa', persona da casa)."""
    return _BOAS_VINDAS_NOME.get(corretora_id or 0, _BOAS_VINDAS_NOME_PADRAO)


def boas_vindas_texto_padrao(corretora_id: int | None = None) -> str:
    """Texto do CÓDIGO da variante de CONTROLE (sem override e sem A/B)."""
    from app.services import boas_vindas_copies as bvc

    padrao = bvc.padrao_de(bvc.CONTROLE_ID) or ""
    return padrao.replace(bvc.PLACEHOLDER_NOME, nome_da_linha(corretora_id))


def boas_vindas_texto(
    corretora_id: int | None = None,
    *,
    lead_id: int | str | None = None,
    variante: Any = None,
) -> str:
    """Corpo ({{2}}) EFETIVO da boas-vindas — o template já diz 'Olá {lead},
    tudo bem?'.

    A copy vem do teste A/B (`boas_vindas_copies`): o balde é do LEAD (hash
    determinístico), então o mesmo lead cai sempre na mesma variante. Sem
    `lead_id` — preview de tela, chamada legada — vale o CONTROLE, nunca um
    sorteio. Override do painel vence o texto do código; com a config fora do ar
    o texto do código segue valendo (a copy da linha não some porque uma tabela
    não respondeu).

    NÃO repetir "Toque em Saiba mais…" aqui: o template reeng_b já fecha com
    "Para {CTA}, é só tocar no botão abaixo." + botão [Saiba mais].
    """
    from app.services import boas_vindas_copies as bvc

    v = variante if variante is not None else bvc.escolher(lead_id)
    return v.render(nome_da_linha(corretora_id))


# Mantido pra compat (testes e chamadores antigos): é a versão da linha da Pessoa12.
BOAS_VINDAS_TEXTO = (
    "Sou a Pessoa, da Acme. Você acabou de se cadastrar com interesse "
    "em imóveis aqui na cidade de Cidade, certo? Estamos com algumas "
    "oportunidades nessa semana."
)
EVENT_BOAS_VINDAS = "boas_vindas_enviada"

# ── Roleta de distribuição ────────────────────────────────────────────────────
# REGRA DA DONA (26/07/2026): lead NOVO vai só pra Pessoa5 (1) e Pessoa1 (2),
# em round-robin. A Pessoa12 (3) fica FORA da roleta — a linha dela (2591 Cloud) vira
# operação de REMARKETING (cadência/follow-up/reengajamento), porque "as meninas
# não tão fazendo remarketing direito". Não é roteamento por performance (Dona
# vetou) — é divisão de papel: novas = Pessoa10+Pessoa11, requentar base = Pessoa12.
# (a lista vive em settings.roleta_corretoras — ver comentário no config.py)

# Boas-vindas automática: sai pela LINHA CLOUD DA DONA do lead. Multi-linha
# (27/07): deixou de ser lista fixa `(3,)` — vale pra TODA corretora com linha
# Cloud registrada (hoje Pessoa12 + Pessoa10). Corretora sem linha (Pessoa11, ainda no
# Evolution) NÃO recebe automação: o contato dela segue manual, pelo telefone
# dela, e mandar template de outra linha confundiria o lead sobre onde responder.
def boas_vindas_habilitada(session: Session, corretora_id: int | None) -> bool:
    """True se a corretora dona do lead tem linha Cloud (→ boas-vindas automática).

    Fail-closed e silencioso: sem linha, sem cadastro ou sem DB devolve False —
    ninguém recebe nada por engano. Diferente da cadência (que degrada pra Pessoa12
    quando o cadastro está ilegível, pra não matar cadência já em curso), aqui a
    dúvida BLOQUEIA: é um envio NOVO, e mandar pela linha errada é pior que não
    mandar.
    """
    if corretora_id is None:
        return False
    linha = cloud_lines.linha_da_corretora(corretora_id, session=session)
    return linha is not None and linha is not cloud_lines.INDETERMINADO


# Sender injetável (teste passa um fake; default = template WABA da linha da dona).
# Assinatura histórica `(telefone, texto)`; multi-linha passa `linha`/`corretora_id`
# como kwargs OPCIONAIS só pra quem aceita (ver _chamar_sender).
SenderFn = Callable[..., dict[str, Any]]


class IntakeResult(BaseModel):
    """Resultado da entrada do lead — contrato de saída do intake."""

    lead_id: int
    telefone: str
    corretora_id: int | None
    boas_vindas_enviada: bool
    stage: str
    ja_existia: bool


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _default_session_factory() -> Session:
    # Import tardio: mantém o módulo importável em testes sem engine de prod.
    from app.db.session import SessionLocal

    return SessionLocal()


def _default_sender(
    telefone: str,
    texto: str,
    *,
    linha: Any = None,
    corretora_id: int | None = None,
    first_name: str | None = None,
) -> dict[str, Any]:
    """Envia a boas-vindas pela LINHA DA DONA do lead (template WABA COM botão).

    Multi-linha (27/07): com `linha` resolvida, sai pelo phone_id/token dela e
    grava no thread da corretora certa. Sem `linha` cai no caminho histórico da
    Pessoa12. Já está atrás da trava DRY-RUN global (`settings.dry_run`) — em dev NADA
    sai de verdade. Import tardio pra evitar ciclo app.api ↔ app.services (mesmo
    padrão do sender da cadência).

    Dona 28/07: COM botão Saiba mais (reeng_b). Pool ola tem "Fico à disposição"
    — Dona pediu pra tirar e fechar com "Toque em Saiba mais…".
    """
    from app.api.cloud_corretora_a import enviar_template_corretor, enviar_template_linha

    kwargs: dict[str, Any] = {
        "hook": texto,
        "cta": BOAS_VINDAS_CTA,
        "variantes": ("b",),
        "com_botao": True,  # reeng_b — SEM "Fico à disposição"
    }
    if linha is None:
        resp = enviar_template_corretor(telefone, first_name, **kwargs)
    else:
        resp = enviar_template_linha(
            linha, telefone, first_name,
            instance=cloud_lines.instance_da_linha(linha),
            corretora_id=corretora_id,
            **kwargs,
        )
    # Sucesso REAL da Graph vem cru (messages[].id = wamid), sem chave "ok" —
    # só o caminho DRY_RUN devolve {"ok": True, ...}. Normaliza aqui, senão o
    # envio real era tratado como falha e o evento de idempotência não era
    # gravado (bug pego no teste E2E de 26/07 — reenvio em webhook repetido).
    if isinstance(resp, dict) and "ok" not in resp:
        wamid = ((resp.get("messages") or [{}])[0] or {}).get("id")
        return {"ok": bool(wamid), "dry_run": False, "sent": bool(wamid), "wamid": wamid}
    return resp


def _chamar_sender(
    sender: SenderFn,
    telefone: str,
    texto: str,
    *,
    linha: Any = None,
    corretora_id: int | None = None,
    first_name: str | None = None,
) -> dict[str, Any]:
    """Chama o sender passando kwargs extras SÓ se ele aceitar.

    O contrato histórico do sender de boas-vindas é `(telefone, texto)` — é o que
    os fakes dos testes implementam. Passar os kwargs novos às cegas estouraria
    TypeError neles, então a assinatura decide.
    """
    extras: dict[str, Any] = {}
    try:
        import inspect

        params = inspect.signature(sender).parameters
        aceita_tudo = any(
            p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
        )
        if aceita_tudo or "linha" in params:
            extras["linha"] = linha
        if aceita_tudo or "corretora_id" in params:
            extras["corretora_id"] = corretora_id
        if aceita_tudo or "first_name" in params:
            extras["first_name"] = first_name
    except (TypeError, ValueError):  # sem assinatura introspectável
        extras = {}
    return sender(telefone, texto, **extras)


def roleta_configurada() -> tuple[int, ...]:
    """A roleta do Meta como está gravada, lida do banco AGORA (06/10/2026).

    A tela edita a roleta e o lead seguinte já tem que cair nela — o cache de
    30 s do `config_operacional` deixava o lead das próximas dezenas de
    segundos ir pra quem a Dona acabou de tirar. Sem linha no banco vale o
    padrão (`.env`); banco fora do ar cai no valor em cache, como antes; valor
    gravado torto (não é lista de ids) = ninguém, e o lead nasce sem dona,
    visível no board.
    """
    from app.services import automacao_por_corretora as apc

    try:
        bruto = apc.ler_fresco("roleta_corretoras")
    except config_operacional.ConfigIndisponivel:
        return tuple(config_operacional.roleta_corretoras())
    if bruto is None:
        bruto = config_operacional.DEFAULTS["roleta_corretoras"]()
    try:
        return tuple(int(x) for x in bruto)
    except (TypeError, ValueError):
        log.error("roleta_corretoras_ilegivel", gravado=str(bruto)[:80])
        return ()


def vez_da_roleta(session: Session, tenant_id: int,
                  elegiveis: tuple[int, ...]) -> int | None:
    """De quem é a vez, SEM gravar nada: a corretora seguinte à dona do lead
    mais recente entre as elegíveis. Mesma conta que o intake usa pra
    distribuir — a tela mostra exatamente quem vai receber o próximo."""
    if not elegiveis:
        return None
    if len(elegiveis) == 1:
        return elegiveis[0]
    ultimo = (
        session.query(Lead)
        .filter(Lead.tenant_id == tenant_id, Lead.owner_id.in_(elegiveis))
        .order_by(Lead.id.desc())
        .first()
    )
    if ultimo is None or ultimo.owner_id not in elegiveis:
        return elegiveis[0]
    idx = (elegiveis.index(ultimo.owner_id) + 1) % len(elegiveis)
    return elegiveis[idx]


def corretoras_roleta_validas(session: Session) -> tuple[int, ...]:
    """Corretoras de `settings.roleta_corretoras` que EXISTEM e estão ativas,
    na ordem da config (a ordem é o estado da rotação).

    Público de propósito: o startup chama pra falhar rápido em typo de config,
    em vez de descobrir com lead fantasma no board.
    """
    configuradas = roleta_configurada()
    if not configuradas:
        return ()
    existentes = {
        row[0]
        for row in session.query(Corretora.id)
        .filter(Corretora.id.in_(configuradas), Corretora.ativo.is_(True))
        .all()
    }
    return tuple(cid for cid in configuradas if cid in existentes)


def _proxima_corretora_roleta(session: Session, tenant_id: int) -> int | None:
    """Round-robin sobre as corretoras elegíveis.

    Estado da rotação = última atribuição real (o `leads.owner_id` do lead mais
    recente entre as elegíveis). Com 1 elegível (hoje: só a Pessoa12) devolve sempre
    ela; com N, avança circular a partir da última.
    """
    elegiveis = corretoras_roleta_validas(session)
    if not elegiveis:
        # Nenhuma corretora da config existe/está ativa. Lead NASCE SEM DONO
        # (visível pro admin no board) em vez de nascer com owner fantasma —
        # leads.owner_id não tem foreign key, então um typo em
        # ROLETA_CORRETORAS gravaria um id inexistente e o lead sumiria do CRM
        # de todo mundo, silenciosamente.
        log.error(
            "roleta_corretora_inexistente",
            configuradas=list(roleta_configurada()),
        )
        return None
    return vez_da_roleta(session, tenant_id, elegiveis)


def _dono_pela_roleta(session: Session, tenant_id: int,
                      lead: Lead | None = None) -> int | None:
    """Quem recebe o lead que está sem dono — a ÚNICA porta pra roleta do Meta.

    Card com a tag RMKT (`leads.veio_remarketing_em`) NÃO passa por ela (Dona
    05/10/2026: "tem tag RMKT, então passa por uma outra roleta ao invés da
    roleta normal do Meta"): vai pra próxima da roleta do REMARKETING, ou fica
    sem dono se ela estiver vazia/ilegível. Lead novo (sem card ainda) e card
    sem a tag seguem na roleta do Meta, como sempre.
    """
    if lead is not None and lead.veio_remarketing_em is not None:
        from app.services import quando_responde

        cid = quando_responde.dono_para_lead_marcado(session, tenant_id)
        log.info("intake_lead_rmkt_fora_da_roleta_do_meta", lead_id=lead.id,
                 para=cid)
        return cid
    return _proxima_corretora_roleta(session, tenant_id)


def _boas_vindas_ja_enviada(session: Session, tenant_id: int, lead_id: int) -> bool:
    """True se o lead JÁ recebeu a boas-vindas (idempotência por telefone —
    lead é único por (tenant, telefone), então o evento por lead cobre)."""
    return (
        session.query(LeadEvent.id)
        .filter(
            LeadEvent.tenant_id == tenant_id,
            LeadEvent.lead_id == lead_id,
            LeadEvent.tipo == EVENT_BOAS_VINDAS,
        )
        .first()
        is not None
    )


EVENT_BOAS_VINDAS_FALHOU = "boas_vindas_falhou"


def _registrar_falha_boas_vindas(session: Session, tenant_id: int, lead_id: int,
                                 corretora_id: int | None, erro: str) -> None:
    """A falha da boas-vindas fica no banco, não só no log (06/10/2026): a tela
    da automação conta o que falhou por corretora. NÃO é o evento de
    idempotência — o próximo webhook do mesmo lead continua tentando.
    SAVEPOINT: gravar a falha não pode derrubar a entrada do lead."""
    try:
        with session.begin_nested():
            session.add(LeadEvent(
                tenant_id=tenant_id, lead_id=lead_id,
                tipo=EVENT_BOAS_VINDAS_FALHOU,
                payload={"corretora_id": corretora_id, "erro": erro[:300]},
                at=_now(),
            ))
    except Exception as e:  # noqa: BLE001
        log.warning("lead_intake_falha_nao_gravada", lead_id=lead_id,
                    erro=str(e)[:120])


def intake_lead(
    *,
    nome: str | None,
    telefone: str,
    origem: str | None = None,
    external_ref: str | None = None,
    origem_campanha: str | None = None,
    origem_anuncio: str | None = None,
    origem_form: str | None = None,
    tenant_id: int = DEFAULT_TENANT_ID,
    session_factory: Callable[[], Session] | None = None,
    sender: SenderFn | None = None,
) -> IntakeResult | None:
    """Entrada do lead: upsert na espinha + roleta + boas-vindas 1x + evento 'intake'.

    Idempotente por (tenant_id, telefone normalizado): repetir a chamada com o
    mesmo telefone NÃO duplica lead nem reenvia boas-vindas — devolve o
    existente com `ja_existia=True`. Lead existente NUNCA regride de etapa
    (não volta pra "Novo") nem troca de dona — só completa dados que vieram
    (padrão lead_spine).

    NÃO matricula em cadência (lei Dona 10/07): boas-vindas é automação
    separada; a cadência só liga quando a Pessoa12 arrastar o lead pro Follow-up.

    Args:
        nome/telefone/origem: dados do lead (telefone obrigatório).
        external_ref: id externo (ex: kommo_id / leadgen_id Meta), opcional.
        tenant_id: tenant dono (1 = Acme).
        session_factory: injeção pra teste. Default = SessionLocal.
        sender: injeção pra teste. Default = template WABA da Pessoa12
            (`enviar_template_corretor`, atrás de settings.dry_run).

    Returns:
        IntakeResult, ou None se a flag `lead_intake_ativo` está desligada.

    Raises:
        ValueError: telefone inválido/inormalizável. O caller (endpoint) decide
        o status HTTP; aqui a exceção sobe pra ficar visível em teste.
    """
    # Estado EFETIVO (painel > .env): o interruptor da página de boas-vindas
    # grava em `config_operacional`, e sem ler daqui ele seria decorativo.
    if not config_operacional.lead_intake_ativo():
        log.info("lead_intake_skip", motivo="flag_desligada")
        return None

    tel = normalize_phone_kommo(telefone)
    if tel is None:
        raise ValueError(f"telefone inválido pro intake: {telefone!r}")

    # Forma canônica (L3): identidade única do lead. `ambiguo` = tem cara de
    # telefone mas não dá pra decidir se falta o 9 (12 díg começando com 7/8).
    # Nesse caso o lead ENTRA do mesmo jeito (é lead vivo da Meta, perder é
    # pior), só que marcado pra revisão humana e sem casar com ninguém.
    tel_canonico, tel_ambiguo = canonical_phone_info(tel)
    if tel_ambiguo:
        log.warning("lead_intake_telefone_ambiguo", telefone=tel[-4:])
    tel = tel_canonico or tel

    nome = (nome or "").strip() or None
    origem = (origem or "").strip() or None
    external_ref = (str(external_ref).strip() or None) if external_ref is not None else None
    origem_campanha = (origem_campanha or "").strip() or None
    origem_anuncio = (origem_anuncio or "").strip() or None
    origem_form = (origem_form or "").strip() or None

    make_session = session_factory or _default_session_factory
    session = make_session()
    try:
        stage_id = _resolve_stage_id(session, tenant_id, None, STAGE_ENTRADA)

        # Dedupe pelo match ÚNICO da espinha (L3 · encontrar_lead): external_ref
        # exato OU telefone canônico COMPLETO com DDD igual. O fallback frouxo
        # `candidatos[0]` foi REMOVIDO — ele fundia lead novo no lead errado
        # quando o sufixo-8 batia por acaso entre DDDs diferentes. Sem match
        # exato agora é lead NOVO (duplicata é reversível; fusão errada não).
        # `excluir_remarketing=True` (achado do Grok, 29/07): o shadow do Kommo
        # já passava isso, o intake não. Sem ele, um lead da base antiga que está
        # em REMARKETING e preenche um anúncio NOVO casava por telefone com o
        # registro velho — ficava com a dona antiga, mantinha a etapa de
        # remarketing e sumia da roleta. Quem se recadastra é lead NOVO: entra na
        # fila e vai pra próxima corretora.
        lead = encontrar_lead(
            session, tenant_id, external_ref=external_ref, telefone=tel,
            excluir_remarketing=True,
        )
        ja_existia = lead is not None

        if lead is None:
            corretora_id = _dono_pela_roleta(session, tenant_id)
            lead = Lead(
                tenant_id=tenant_id,
                external_ref=external_ref,
                nome=nome,
                telefone=tel if tel.startswith("+") else f"+{tel}",
                owner_id=corretora_id,
                stage_id=stage_id,
                origem=origem,
                origem_campanha=origem_campanha,
                origem_anuncio=origem_anuncio,
                origem_form=origem_form,
                telefone_invalido=tel_ambiguo,
                created_at=_now(),
                updated_at=_now(),
            )
            session.add(lead)
            session.flush()  # popula lead.id
        else:
            # Lead já na espinha: NÃO regride stage, NÃO re-roleta. Se ainda não
            # tinha dona (ex: criado pelo shadow do Kommo sem owner), atribui agora.
            corretora_id = lead.owner_id
            if corretora_id is None:
                corretora_id = _dono_pela_roleta(session, tenant_id, lead)
                lead.owner_id = corretora_id
            if nome is not None:
                lead.nome = nome
            if origem is not None:
                lead.origem = origem
            if external_ref is not None and lead.external_ref is None:
                lead.external_ref = external_ref
            # Origem Meta: só completa o que ainda está vazio (não sobrescreve o
            # que já foi atribuído — o primeiro webhook manda).
            if origem_campanha is not None and lead.origem_campanha is None:
                lead.origem_campanha = origem_campanha
            if origem_anuncio is not None and lead.origem_anuncio is None:
                lead.origem_anuncio = origem_anuncio
            if origem_form is not None and lead.origem_form is None:
                lead.origem_form = origem_form
            lead.updated_at = _now()

        # Boas-vindas (automação separada — lei Dona 10/07): UMA mensagem e
        # AGUARDA. Só quando o lead está na etapa de entrada ("Novo") — lead
        # existente que já avançou de etapa não "caiu em Novo" agora e não
        # recebe. Idempotente: lead_events(tipo='boas_vindas_enviada').
        boas_vindas_enviada = False
        variante_bv = None  # variante do A/B que saiu (None = não saiu nada)
        continuidade = False  # True = copy de continuidade (lead veio de ligação)
        # Multi-linha: sai pela linha Cloud da DONA. Corretora sem linha não
        # dispara nada (fail-closed em boas_vindas_habilitada).
        linha_dona = (
            cloud_lines.linha_da_corretora(corretora_id, session=session)
            if boas_vindas_habilitada(session, corretora_id)
            else None
        )
        if (
            linha_dona is not None
            and lead.stage_id == stage_id
            and not _boas_vindas_ja_enviada(session, tenant_id, lead.id)
        ):
            # ⚠️ Lead ADOTADO POR LIGAÇÃO não recebe a copy padrão nem entra no
            # A/B. Ele acabou de falar por TELEFONE com a corretora — mandar
            # "você acabou de se cadastrar, certo?" é constrangedor e destrói a
            # credibilidade da conversa que acabou de acontecer. Sai a copy de
            # CONTINUIDADE, e fora do teste de propósito (o contexto é outro e
            # contaminaria o resultado das três variantes). Fluxo completo em
            # `app/services/boas_vindas_continuidade.py`.
            from app.services import boas_vindas_copies as bvc
            from app.services import boas_vindas_continuidade as bvcont
            from app.services.ligacao_resultado import veio_de_ligacao

            continuidade = veio_de_ligacao(session, lead.id)
            if continuidade:
                variante_bv = None
                texto_bv = bvcont.render(nome_da_linha(corretora_id))
            else:
                # Balde do A/B pelo id do LEAD (determinístico — o mesmo lead
                # cai sempre na mesma variante, mesmo se o webhook repetir).
                variante_bv = bvc.escolher(lead.id)
                texto_bv = boas_vindas_texto(corretora_id, variante=variante_bv)
            # Primeiro nome do lead pro "Olá {nome}, tudo bem?" do template.
            lead_primeiro = (lead.nome or "").strip().split()[0] or None
            try:
                envio = _chamar_sender(
                    sender or _default_sender, tel, texto_bv,
                    linha=linha_dona, corretora_id=corretora_id,
                    first_name=lead_primeiro,
                )
            except Exception as e:
                # Envio falhou → NÃO grava o evento de idempotência (o próximo
                # webhook re-tenta). O lead entra na espinha do mesmo jeito.
                log.exception(
                    "lead_intake_boas_vindas_falhou",
                    lead_id=lead.id,
                    telefone=tel[-4:],
                )
                _registrar_falha_boas_vindas(session, tenant_id, lead.id,
                                             corretora_id, str(e))
            else:
                if envio.get("ok"):
                    boas_vindas_enviada = True
                    session.add(
                        LeadEvent(
                            tenant_id=tenant_id,
                            lead_id=lead.id,
                            tipo=EVENT_BOAS_VINDAS,
                            payload={
                                "texto": texto_bv,
                                # O que CONTA no relatório é o que SAIU: a
                                # métrica lê esta chave, nunca o recálculo do
                                # balde (os pesos podem mudar depois).
                                "variante": (
                                    variante_bv.id if variante_bv else bvcont.COPY_ID
                                ),
                                # marca o envio que ficou FORA do A/B — a
                                # métrica das 3 variantes tem que poder excluir.
                                "continuidade": continuidade,
                                "corretora_id": corretora_id,
                                "dry_run": bool(envio.get("dry_run")),
                                "sent": bool(envio.get("sent")),
                                # Pra cruzar com o status da Meta (entregue /
                                # falhou) na tela da automação (06/10).
                                "wamid": envio.get("wamid"),
                            },
                            at=_now(),
                        )
                    )
                else:
                    log.warning(
                        "lead_intake_boas_vindas_nao_ok",
                        lead_id=lead.id,
                        telefone=tel[-4:],
                        resposta=str(envio)[:200],
                    )
                    _registrar_falha_boas_vindas(session, tenant_id, lead.id,
                                                 corretora_id, str(envio))

        session.add(
            LeadEvent(
                tenant_id=tenant_id,
                lead_id=lead.id,
                tipo="intake",
                payload={
                    "origem": origem,
                    "corretora_id": corretora_id,
                    "boas_vindas_enviada": boas_vindas_enviada,
                    "external_ref": external_ref,
                    "ja_existia": ja_existia,
                    "origem_campanha": origem_campanha,
                    "origem_anuncio": origem_anuncio,
                    "origem_form": origem_form,
                },
                at=_now(),
            )
        )
        session.commit()

        log.info(
            "lead_intake_ok",
            lead_id=lead.id,
            telefone=tel[-4:],
            corretora_id=corretora_id,
            boas_vindas_enviada=boas_vindas_enviada,
            variante_bv=(variante_bv.id if variante_bv else None),
            continuidade=continuidade,
            ja_existia=ja_existia,
            origem=origem,
        )

        # Aviso no celular da dona — só pra lead REALMENTE novo. Lead que já
        # existia e voltou não vira push: a corretora já conhece, e repetir
        # aviso de conhecido é o caminho mais curto pra ela desligar a
        # notificação — aí perde também os leads que importam.
        # Falha aqui nunca derruba o intake: o lead já está commitado.
        if not ja_existia and corretora_id is not None:
            try:
                from app.services.push import avisar_lead_novo

                avisar_lead_novo(
                    corretora_id=corretora_id,
                    nome=nome,
                    telefone=tel,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("push_lead_novo_falhou", erro=str(exc)[:200])

        return IntakeResult(
            lead_id=lead.id,
            telefone=tel,
            corretora_id=corretora_id,
            boas_vindas_enviada=boas_vindas_enviada,
            stage=STAGE_ENTRADA,
            ja_existia=ja_existia,
        )
    finally:
        session.close()
