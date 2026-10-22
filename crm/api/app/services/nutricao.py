"""Nutrição (funil REMARKETING) — motor do segundo funil do CRM 2.0.

Spec Dona 10/07 (noite): lead que ESGOTOU a cadência de 8 toques (pós D10,
`cadencia_estado.status='remarketing'`) entra no funil de remarketing do board
/remarketing do acme-painel e recebe 1 mensagem a cada 30 dias PELO NÚMERO DA CORRETOR:

    etapa    marco    tag no card
    Base       —        —          (acabou de esgotar a cadência)
    30 dias   1º disparo  30D
    60 dias   2º disparo  60D
    90 dias   3º disparo  90D
    120 dias  4º disparo  120D
    150 dias  5º disparo  150D     ← PARA aqui. Lead fica na etapa 150 dias.

Como funciona (tudo no MESMO tick da cadência — scripts/cadencia_tick.py, timer
`copiloto-cadencia-tick` de hora em hora no horário comercial SP; SEM cron novo):

  1. ENTRADA (best-effort): todo `cadencia_estado` com status='remarketing' cujo
     lead canônico ainda NÃO está numa etapa do funil remarketing é movido pra
     etapa "Base" (+ lead_events tipo='nutricao_entrada').
  2. MARCOS: estados 'remarketing' com `proximo_toque_em <= agora` (o cooldown
     de 30d que `cadencia._virar_remarketing` já agenda) disparam a copy do
     PRÓXIMO marco via `enviar_template_corretor` (mesma trava DRY-RUN/`settings.dry_run`
     da Fase 7) e o lead avança de etapa (Base→30 dias→…→150 dias). O estado
     ganha `proximo_toque_em = agora + 30d`; no marco 150 vira None (para de vez,
     status segue 'remarketing' pra resposta/opt-out continuarem funcionando via
     `cadencia.processar_resposta_cadencia`).

Coexistência com o remarketing genérico do `cadencia.tick` (que manda
`_TEMPLATE_REMARKETING` a cada 30d pra sempre): este tick roda ANTES do
`cadencia.tick` no script e re-agenda `proximo_toque_em` — o lead processado
aqui deixa de estar "devido" lá (sem mensagem dupla). Lead SEM par canônico na
espinha não é processado aqui e continua no fluxo genérico antigo.

Travas (idênticas às da cadência — reusadas do módulo `cadencia`):
  - kill-switch PRÓPRIO `config_operacional.nutricao_ativa()` (05/08) — sem
    linha no banco herda `cadencia_ativa`, então nada mudou de estado na
    separação; a partir da primeira gravação as duas são independentes;
  - DRY-RUN global via `enviar_template_corretor` (`_dry_run_ativo`);
  - cap diário POR LINHA (`cadencia._cap_diario_atingido(corretora_id=)`, desde
    07/10): conta cadência + nutrição juntas que saíram pela mesma linha, com a
    reserva do follow-up dentro do balde;
  - espaçamento mínimo `settings.cadencia_min_intervalo_s` (mesmo guard
    in-process `cadencia._LAST_ENQUEUE`);
  - horário comercial = o próprio timer (12:15–21:15 UTC).

Linha de saída (07/10/2026): o marco sai pela linha Cloud da corretora DONA do
lead quando ela tem linha e modelo aprovado; senão, pela da Pessoa12. Ver
`saida_do_marco`. O teto do dia é o da linha por onde sai.

TODO (fora do escopo desta noite — specs amanhã com a Dona):
  - disparos de campanha pelo número da ACME;
  - Corretor atendendo a resposta do lead nutrido + handoff pra Pessoa12 (envio real).
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import structlog
from sqlalchemy.orm import Session

from app.config import settings
from app.services import config_operacional
from app.db.models import CadenciaEstado, Lead, PipelineStage
from app.services import cadencia
from app.services.trava_tick import NS_NUTRICAO, OCUPADO, soltar, travar
from app.services import opt_out
from app.services.opt_out import ETAPA_NAO_PERTURBE
from app.services.cadencia import (
    CANAL_WHATSAPP,
    reengajamento_guard,
    DEFAULT_TENANT_ID,
    TIPO_MENSAGEM,
    _now,
    _registrar_evento,
    _suf8,
)

log = structlog.get_logger()

FUNIL_REMARKETING = "remarketing"
INTERVALO_DIAS = 30

STAGE_BASE = "Base"
# ordem canônica das etapas do funil (mesma da migration 0046)
STAGES_NUTRICAO: tuple[str, ...] = (
    "Base", "30 dias", "60 dias", "90 dias", "120 dias", "150 dias"
)
STAGE_FINAL = "150 dias"

# Linha de RESERVA dos marcos: `sender(telefone, texto, com_botao=False)` sem
# `linha` → `cadencia._default_sender` → `enviar_template_corretor` → linha `corretor`. Até
# 07/10/2026 era a única (todo marco, de qualquer corretora). Agora é pra onde
# o marco cai quando a linha da dona não serve — ver `saida_do_marco`.
LINHA_DA_NUTRICAO = "corretor"

# ── Por qual linha o marco sai (07/10/2026, decisão da Dona) ─────────────────
#
# "Se tiver lead que entra na nutrição pelo nome da corretora, da Pessoa10 ou da
# Cláudia, pode fazer a nutrição pelo número delas." O lead não guarda por qual
# número entrou; a DONA (`cadencia_estado.corretora_id`) é o critério, o mesmo
# do follow-up. A regra, relida antes de cada marco:
#
#   chave `nutricao_pela_linha_da_dona` desligada (ou ilegível) → Pessoa12
#   dona sem linha Cloud registrada (ou cadastro ilegível)      → Pessoa12
#   linha da dona sem modelo sem botão UTILITY aprovado na WABA → Pessoa12
#   senão                                                       → linha da dona
#
# O motivo vai no evento do marco (`motivo_linha`) e a tela lê a mesma conta.
CHAVE_LINHA_DA_DONA = "nutricao_pela_linha_da_dona"
MOTIVO_DONA_COM_LINHA = "dona_com_linha"
MOTIVO_DONA_SEM_LINHA = "dona_sem_linha"
MOTIVO_TEMPLATE_NAO_APROVADO = "template_nao_aprovado_na_waba"
MOTIVO_CHAVE_DESLIGADA = "chave_desligada"
MOTIVO_LINHA_EM_PORTUGUES: dict[str, str] = {
    MOTIVO_DONA_COM_LINHA: "pela linha dela",
    MOTIVO_DONA_SEM_LINHA: "ela não tem linha registrada, sai pela da Pessoa12",
    MOTIVO_TEMPLATE_NAO_APROVADO: "a linha dela não tem modelo aprovado, sai pela da Pessoa12",
    MOTIVO_CHAVE_DESLIGADA: "nutrição pela linha da dona desligada, sai pela da Pessoa12",
}

# etapa atual → próxima etapa (marco a disparar). "150 dias" → None = PARA.
_PROXIMA_ETAPA: dict[str, str | None] = {
    "Base": "30 dias",
    "30 dias": "60 dias",
    "60 dias": "90 dias",
    "90 dias": "120 dias",
    "120 dias": "150 dias",
    "150 dias": None,
}

# tag do card no board /remarketing (campo `nutricao_tag`, análogo ao
# `cadencia_toque` do /crm). Base não tem tag — ainda não houve disparo.
TAG_POR_ETAPA: dict[str, str] = {
    "30 dias": "30D",
    "60 dias": "60D",
    "90 dias": "90D",
    "120 dias": "120D",
    "150 dias": "150D",
}

# ── Copies por marco ──────────────────────────────────────────────────────────
# Copies OFICIAIS do salesbot Kommo (aprovadas pela Dona 10/07), exceto onde
# marcado # RASCUNHO — nesses a copy real do Kommo chega depois via mapeamento.
# Linha única sempre. Saem pelo número da Pessoa12 via template WABA.
COPIES_MARCO: dict[str, str] = {
    # RASCUNHO — marco 30 dias (copy real do Kommo chega via mapeamento)
    "30 dias": "Se em algum momento voltar a pensar em comprar ou trocar de imóvel, pode contar comigo. Tô sempre por aqui, com novidades boas e orientação sincera, do jeito que tem que ser.",
    # APROVADO DONA 10/07 — marco 60 dias
    "60 dias": "Tem imóvel que entra e sai do mercado sem nem ir pra internet. Se quiser, posso te avisar quando surgir algo fora da curva com o seu perfil. Me dá um ok aqui",
    # APROVADO DONA 10/07 — marco 90 dias (indicação R$500)
    "90 dias": "Conhece alguém que esteja procurando imóvel? Se indicar e a pessoa fechar com a gente, você ainda ganha R$500. Simples assim. Me fala se lembrar de alguém!",
    # APROVADO DONA 10/07 — marco 120 dias
    # TODO: template Cloud API com IMAGEM do Cristo (mídia no header) — hoje vai texto
    "120 dias": "Olê... tô aqui que nem o Cristo Redentor... 👐 De braços abertos, te esperando pra conversar sobre o imóvel 😄",
    # RASCUNHO — marco 150 dias (último — copy real do Kommo 150D chega via mapeamento)
    "150 dias": "Recebi algumas opções novas que talvez façam sentido pra você, inclusive com condições melhores que o normal. Quer que eu te mande só o que realmente encaixa no seu perfil?",
}


@dataclass
class NutricaoResult:
    entradas: int = 0
    marcos_disparados: int = 0
    finalizados_150: int = 0
    sem_lead_canonico: int = 0
    # Telefone fora do padrão marcado nesta volta (06/10) e envio que falhou.
    # Os dois PULAM o lead e o laço segue — antes o primeiro erro derrubava a
    # volta inteira e nenhum marco saía enquanto ele estivesse na frente.
    telefone_invalido: int = 0
    falhas: int = 0
    bloqueados: dict[str, int] = field(default_factory=dict)
    # Por linha (07/10): marcos que saíram e marcos segurados pelo teto do dia.
    marcos_por_linha: dict[str, int] = field(default_factory=dict)
    teto_por_linha: dict[str, int] = field(default_factory=dict)
    # Opt-out (07/10): estados tirados da régua nesta volta, por caminho do
    # pedido. `bloqueados["opt_out_indisponivel"]` = marco devido segurado
    # porque a leitura de opt-out falhou.
    opt_out: int = 0
    opt_out_por_caminho: dict[str, int] = field(default_factory=dict)
    detalhes: list[dict[str, Any]] = field(default_factory=list)


# ── Telefone fora do padrão (06/10/2026) ─────────────────────────────────────
#
# O defeito: `cadencia_estado` 276 tinha 24 dígitos (dois números colados). A
# Meta recusava (131009), o sender levantava, o `except` do tick desfazia TUDO
# e a volta terminava com `marcos=0` — enquanto esse lead estivesse na frente
# da fila, nenhum marco de ninguém saía (13:36 → 15:18 UTC).
#
# Celular brasileiro no formato da Meta: 55 + DDD + 8 ou 9 dígitos = 12 ou 13.
# Fora disso não é palpite de número errado, é número que não existe: o estado
# vira `telefone_invalido` (sai da régua, nenhum dos dois ticks pega mais) e
# fica um evento com o motivo. Reversível: status de volta pra 'remarketing'.
STATUS_TELEFONE_INVALIDO = "telefone_invalido"
TIPO_TELEFONE_INVALIDO = "telefone_invalido"
_RE_TELEFONE_OK = re.compile(r"55\d{10,11}")

# ── Quem pediu pra parar, por QUALQUER caminho (07/10/2026) ─────────────────
#
# A nutrição chamava o emissor sem `origem`, e a trava de opt-out do emissor
# (`enviar_template_linha`) só confere envio de automação com `origem`. O marco
# passava por fora: quem pediu pra parar pela própria régua saía dela, quem
# pediu por outro caminho (etapa Não perturbe, disparo antigo, "parar" no
# remarketing novo, opt-out do api-disparos) seguia recebendo. Quem saiu, saiu.
#
# Agora, a cada volta:
#   1. UMA leitura de `opt_out.mapa_de_bloqueio` (todas as fontes) antes dos
#      marcos. Estado da régua cujo telefone está lá vira `opt_out`, com evento
#      dizendo por qual caminho a pessoa tinha pedido — devido ou não, com ou
#      sem lead canônico. Nunca mais é pego (o tick só lê 'remarketing').
#   2. FAIL-CLOSED: se a leitura falhar, nenhum marco sai naquela volta; cada
#      devido conta em `bloqueados["opt_out_indisponivel"]` e fica devido.
#   3. O envio leva `origem="nutricao"`: a trava do emissor confere de novo na
#      hora de sair. Se ela barrar (`erro="opt_out"`), o estado vira `opt_out`
#      do mesmo jeito, com caminho `emissor`.
# A leitura e a marcação moram em `opt_out` (`ler_mapa`, `tirar_da_regua`) —
# o follow-up de 8 toques usa as mesmas desde 07/10 à tarde.
ORIGEM_NUTRICAO = "nutricao"
STATUS_OPT_OUT = opt_out.STATUS_OPT_OUT
TIPO_OPT_OUT = opt_out.TIPO_OPT_OUT
BLOQUEIO_OPT_OUT_INDISPONIVEL = opt_out.BLOQUEIO_OPT_OUT_INDISPONIVEL
CAMINHO_EMISSOR = opt_out.CAMINHO_EMISSOR

# Envio que falhou não volta a bater na Meta a cada 3 minutos: o marco fica
# devido de novo amanhã. Um marco de 30 em 30 dias atrasar 1 dia não muda nada;
# o mesmo número errado sendo tentado 20 vezes por hora muda a qualidade da linha.
ADIA_APOS_FALHA = timedelta(days=1)


@dataclass(frozen=True)
class SaidaDoMarco:
    """Por onde um marco sai. `linha=None` = caminho histórico da Pessoa12
    (`enviar_template_corretor`, byte a byte como antes)."""

    slug: str
    motivo: str
    linha: Any = None
    # Balde do teto do dia: a corretora DONA da linha por onde sai.
    corretora_balde: int = 3
    # Leitura da chave quando ela não decidiu "ligada" (ilegível/indisponível).
    chave: str | None = None


def pela_linha_da_dona() -> tuple[bool, str | None]:
    """(ligada, problema). Relida do banco AGORA, sem cache. Sem linha = padrão
    (ligada). Ilegível ou banco fora = desligada: na dúvida, a Pessoa12, que é o
    caminho que sempre funcionou."""
    from app.services import automacao_por_corretora as apc
    from app.services.config_operacional import ConfigIndisponivel

    try:
        bruto = apc.ler_fresco(CHAVE_LINHA_DA_DONA)
    except ConfigIndisponivel as e:
        log.warning("nutricao_linha_da_dona_indisponivel", erro=str(e)[:140])
        return False, "indisponivel"
    if bruto is None:
        return True, None
    if isinstance(bruto, bool):
        return bruto, None
    log.warning("nutricao_linha_da_dona_ilegivel", valor=str(bruto)[:60])
    return False, "ilegivel"


def modelos_sem_botao_aprovados(slug: str) -> list[str]:
    """Modelos SEM botão (2 variáveis) que `enviar_template_linha` usaria nesta
    linha, já filtrados pela categoria REAL na Meta (UTILITY aprovado).

    Mesma conta do emissor, sem enviar nada: a rotação padrão (`acme_ola_*`)
    no que estiver UTILITY nesta WABA; se nenhum, a rotação própria da linha
    (nomes `_ola_` do prefixo dela). Os cascos da Corretor (`_ola_m_`/`_ola_w_`) são
    MARKETING por nome e ficam fora. Vazio = o envio por esta linha falharia
    com "sem template", então o marco vai pela Pessoa12.
    """
    from app.api.cloud_corretora_a import _OLA_ROTATION
    from app.services import cloud_lines

    utility = set(cloud_lines.templates_utility(slug))
    padrao = [n for n, _ in _OLA_ROTATION if n in utility]
    if padrao:
        candidatos = padrao
    else:
        pref = cloud_lines.prefixo_do_slug(slug)
        proprios = {n for n in utility if not pref or n.startswith(pref)}
        candidatos = sorted(n for n in proprios if "_ola_" in n)
    return [n for n in candidatos if "_ola_m_" not in n and "_ola_w_" not in n]


def saida_do_marco(corretora_id: int | None, *, session: Session | None = None) -> SaidaDoMarco:
    """Por qual linha sai o marco do lead desta dona. Nunca levanta: qualquer
    dúvida vira Pessoa12, com o motivo."""
    from app.services import automacao_por_corretora as apc
    from app.services import cloud_lines

    corretor_id = apc.CORRETORA_SEM_DONA

    def _corretor(motivo: str, chave: str | None = None) -> SaidaDoMarco:
        return SaidaDoMarco(slug=LINHA_DA_NUTRICAO, motivo=motivo,
                            corretora_balde=corretor_id, chave=chave)

    ligada, problema = pela_linha_da_dona()
    if not ligada:
        return _corretor(MOTIVO_CHAVE_DESLIGADA, problema)
    dona = apc.dona(corretora_id)
    try:
        linha = cloud_lines.linha_da_corretora(dona, session=session)
    except Exception as e:  # noqa: BLE001
        log.warning("nutricao_linha_da_dona_erro", corretora_id=dona, erro=str(e)[:140])
        linha = None
    if linha is None or linha is cloud_lines.INDETERMINADO:
        return _corretor(MOTIVO_DONA_SEM_LINHA)
    if linha.slug == LINHA_DA_NUTRICAO:
        # A dona É a Pessoa12: caminho histórico, sem `linha` no sender.
        return _corretor(MOTIVO_DONA_COM_LINHA)
    try:
        aprovados = modelos_sem_botao_aprovados(linha.slug)
    except Exception as e:  # noqa: BLE001 — não sei = não arrisco a linha dela
        log.warning("nutricao_modelos_da_linha_erro", slug=linha.slug, erro=str(e)[:140])
        aprovados = []
    if not aprovados:
        return _corretor(MOTIVO_TEMPLATE_NAO_APROVADO)
    return SaidaDoMarco(slug=linha.slug, motivo=MOTIVO_DONA_COM_LINHA, linha=linha,
                        corretora_balde=dona)


def telefone_no_padrao(telefone: str | None) -> bool:
    """12 ou 13 dígitos começando com 55."""
    digitos = re.sub(r"\D", "", telefone or "")
    return bool(_RE_TELEFONE_OK.fullmatch(digitos))


def _bloqueio(res: NutricaoResult, motivo: str) -> None:
    res.bloqueados[motivo] = res.bloqueados.get(motivo, 0) + 1


def _ensure_stages(session: Session, tenant_id: int) -> dict[str, PipelineStage] | None:
    """Garante as 5 etapas do funil remarketing e devolve {nome: stage}.

    A migration 0046 já semeia — isto é rede de segurança idempotente (DB de
    teste enxuto / tenant novo). SAVEPOINT: se a coluna `funil` ainda não existe
    (migration não aplicada), degrada pra None SEM poisonar a sessão do tick.
    """
    try:
        with session.begin_nested():
            existentes = {
                (st.nome or "").strip(): st
                for st in session.query(PipelineStage)
                .filter(
                    PipelineStage.tenant_id == tenant_id,
                    PipelineStage.funil == FUNIL_REMARKETING,
                )
                .all()
            }
            for ordem, nome in enumerate(STAGES_NUTRICAO, start=1):
                if nome not in existentes:
                    st = PipelineStage(
                        tenant_id=tenant_id, nome=nome, ordem=ordem, funil=FUNIL_REMARKETING
                    )
                    session.add(st)
                    session.flush()
                    existentes[nome] = st
            return existentes
    except Exception as e:  # noqa: BLE001 — coluna funil ausente = migration 0046 pendente
        log.warning("nutricao_stages_indisponiveis", erro=str(e)[:160])
        return None


def _leads_por_sufixo(session: Session, tenant_id: int) -> dict[str, Lead]:
    """Lead canônico indexado pelos últimos 8 dígitos do telefone (mesma
    heurística de match do `cadencia._mover_lead_cadencia`)."""
    out: dict[str, Lead] = {}
    for lead in (
        session.query(Lead)
        .filter(Lead.tenant_id == tenant_id, Lead.telefone.isnot(None))
        .all()
    ):
        suf = _suf8(lead.telefone)
        if suf:
            out.setdefault(suf, lead)
    return out


def tick(
    agora: datetime | None = None,
    *,
    session: Session | None = None,
    session_factory: Callable[[], Session] | None = None,
    sender: Callable[..., dict[str, Any]] | None = None,
    tenant_id: int = DEFAULT_TENANT_ID,
) -> NutricaoResult:
    """1 varredura da nutrição. Roda ANTES do `cadencia.tick` no script do timer.

    Fase 1 — ENTRADA: status='remarketing' sem lead no funil → etapa Base.
    Fase 2 — MARCOS: estados devidos (proximo_toque_em <= agora) disparam a copy
    do próximo marco (DRY-RUN atrás de settings.dry_run), avançam a etapa do
    lead e re-agendam +30d. No marco 150: proximo_toque_em=None (fica lá, para).

    Idempotente no dia: processado ganha proximo_toque_em no futuro (ou None).
    Best-effort: nunca levanta — erro degrada com log.
    """
    # Kill-switch PRÓPRIO desde 05/08 (antes era o da cadência, e desligar o
    # follow-up parava os marcos junto). Sem linha no banco, `nutricao_ativa`
    # herda `cadencia_ativa` — nada muda de estado até alguém tocar na chave.
    if not config_operacional.nutricao_ativa():
        log.info("nutricao_inativa_skip")
        return NutricaoResult()

    agora = agora or _now()
    # Dia útil (02/10/2026): sábado/domingo nenhum marco sai; segunda pega o
    # que ficou devido. Ver app/services/dia_util.py.
    from app.services import dia_util

    if not dia_util.pode_enviar(agora, motor="nutricao"):
        return NutricaoResult()
    sender = sender or cadencia._default_sender

    owns_session = session is None
    if owns_session:
        if session_factory is None:
            from app.db.session import SessionLocal

            session_factory = SessionLocal
        session = session_factory()

    res = NutricaoResult()

    # ── trava contra tick sobreposto (auditoria Grok+Sol, 29/07) ──
    # Mesma corrida da cadência, e aqui é PIOR: a nutrição roda ANTES no
    # `cadencia_tick.py` e só faz `session.commit()` no fim de tudo — uma
    # execução sobreposta lê estado desatualizado o loop inteiro, não só numa
    # janela. E manda WhatsApp real pelo mesmo motor.
    trava = travar(NS_NUTRICAO, tenant_id)
    if trava is OCUPADO:
        log.info("nutricao_tick_concorrente_pulou", tenant_id=tenant_id)
        if owns_session:
            session.close()
        return res
    try:
        stages = _ensure_stages(session, tenant_id)
        if stages is None:
            return res  # migration 0046 ainda não aplicada — nutrição dorme

        ids_funil = {st.id: nome for nome, st in stages.items()}
        leads_por_suf = _leads_por_sufixo(session, tenant_id)

        estados = (
            session.query(CadenciaEstado)
            .filter(
                CadenciaEstado.tenant_id == tenant_id,
                CadenciaEstado.status == "remarketing",
            )
            .all()
        )

        # Opt-out de TODAS as fontes, lido uma vez, ANTES da entrada (07/10).
        # None = leitura falhou → nenhum marco sai nesta volta.
        bloqueio = opt_out.ler_mapa(session, motor=ORIGEM_NUTRICAO)

        # ── Fase 1: ENTRADA no funil (etapa Base) ────────────────────────────
        for est in estados:
            lead = leads_por_suf.get(_suf8(est.telefone))
            if lead is None:
                res.sem_lead_canonico += 1
                continue
            if lead.stage_id in ids_funil:
                continue  # já está no funil remarketing
            # QUEM PEDIU PRA PARAR NÃO ENTRA (07/10). "Não perturbe" (26) é do
            # funil principal: sem isto a entrada puxava o card de lá pra Base.
            # A etapa vale mesmo com a leitura fora do ar; o resto do mapa
            # tira o estado da régua na Fase 2.
            if lead.stage_id == ETAPA_NAO_PERTURBE or (
                bloqueio is not None and _suf8(est.telefone) in bloqueio
            ):
                continue
            # PRECEDÊNCIA DA LIGAÇÃO (05/08): lead ADOTADO na janela de ligação
            # saiu daqui de propósito — foi pra "Novo" do funil principal com
            # uma corretora como dona, porque falou por telefone e quis
            # continuar. Sem esta linha, a Fase 1 puxava ele de volta pra "Base"
            # no tick seguinte: desfazia a adoção, tirava o lead do funil da
            # corretora e o recolocava na régua automática. Contato humano vale
            # mais que régua — quem foi adotado não volta.
            if getattr(lead, "adotado_ligacao_em", None) is not None:
                _bloqueio(res, "adotado_por_ligacao")
                continue
            lead.stage_id = stages[STAGE_BASE].id
            lead.updated_at = agora
            _registrar_lead_event_safe(
                session, tenant_id, lead.id, "nutricao_entrada",
                {"cadencia_id": est.id, "etapa": STAGE_BASE},
            )
            res.entradas += 1
            res.detalhes.append(
                {"telefone": est.telefone[-4:], "tipo": "entrada", "etapa": STAGE_BASE}
            )

        # Fase 1 vira durável antes de qualquer envio: a partir daqui o commit é
        # POR LEAD, e um erro lá na frente não pode desfazer quem entrou na Base.
        session.commit()

        # ── Fase 2: MARCOS devidos (1 msg / 30 dias) ─────────────────────────
        #
        # UM LEAD NÃO PARA O LAÇO (06/10/2026). Antes, qualquer exceção (a Meta
        # recusando um número de 24 dígitos) subia até o `except` lá de baixo:
        # rollback da volta inteira — inclusive do marco que já tinha SAÍDO, que
        # voltava a ficar devido e sairia de novo — e nenhum marco de ninguém
        # enquanto aquele lead estivesse na frente. Agora cada lead é uma
        # transação: o que deu certo fica gravado na hora, o que deu errado é
        # registrado com o motivo e o laço segue pro próximo.
        for est in estados:
            try:
                _marco_do_estado(
                    session, est, agora=agora, res=res, sender=sender,
                    tenant_id=tenant_id, stages=stages, ids_funil=ids_funil,
                    leads_por_suf=leads_por_suf, bloqueio=bloqueio,
                )
            except Exception as e:  # noqa: BLE001 — um lead não derruba a volta
                res.falhas += 1
                _bloqueio(res, "erro_no_lead")
                log.warning("nutricao_lead_erro", cadencia_id=getattr(est, "id", None),
                            erro=str(e)[:200])
                try:
                    session.rollback()
                except Exception:  # noqa: BLE001
                    pass

        session.commit()
    except Exception as e:  # noqa: BLE001 — nutrição nunca derruba o tick da cadência
        log.warning("nutricao_tick_erro", erro=str(e)[:200])
        try:
            session.rollback()
        except Exception:  # noqa: BLE001
            pass
    finally:
        # a trava sai POR ÚLTIMO: só depois do commit final outro tick entra.
        if owns_session:
            session.close()
        soltar(trava, NS_NUTRICAO, tenant_id)

    log.info(
        "nutricao_tick_done",
        entradas=res.entradas,
        marcos=res.marcos_disparados,
        finalizados_150=res.finalizados_150,
        sem_lead=res.sem_lead_canonico,
        telefone_invalido=res.telefone_invalido,
        falhas=res.falhas,
        bloqueados=res.bloqueados,
        marcos_por_linha=res.marcos_por_linha,
        teto_por_linha=res.teto_por_linha,
        opt_out=res.opt_out,
        opt_out_por_caminho=res.opt_out_por_caminho,
    )
    return res


def _marco_do_estado(
    session: Session,
    est: CadenciaEstado,
    *,
    agora: datetime,
    res: NutricaoResult,
    sender: Callable[..., dict[str, Any]],
    tenant_id: int,
    stages: dict[str, PipelineStage],
    ids_funil: dict[int, str],
    leads_por_suf: dict[str, Lead],
    bloqueio: dict[str, list[str]] | None,
) -> None:
    """Um lead da Fase 2. Termina em commit (o que fez fica gravado) ou em
    `continue` sem escrita; exceção inesperada sobe pro laço, que registra,
    desfaz SÓ este lead e segue."""
    from app.services import automacao_por_corretora as apc

    devido = est.proximo_toque_em
    if devido is not None and devido.tzinfo is None:
        devido = devido.replace(tzinfo=timezone.utc)  # SQLite de teste devolve naive
    esta_devido = devido is not None and devido <= agora

    # OPT-OUT, antes de tudo (07/10): vale pra devido ou não — quem pediu pra
    # parar sai da régua na primeira volta, não no dia do próximo marco.
    if bloqueio is None:
        if esta_devido:
            _bloqueio(res, BLOQUEIO_OPT_OUT_INDISPONIVEL)
        return
    caminhos = bloqueio.get(_suf8(est.telefone))
    if caminhos:
        _marcar_opt_out(session, est, agora, caminhos, res=res,
                        lead=leads_por_suf.get(_suf8(est.telefone)), tenant_id=tenant_id)
        session.commit()
        return

    if not esta_devido:
        return

    # TELEFONE FORA DO PADRÃO (06/10): antes de qualquer consulta por telefone
    # e antes de tentar a Meta. Não é "falhou hoje", é número que não existe.
    if not telefone_no_padrao(est.telefone):
        _marcar_telefone_invalido(session, est, agora)
        res.telefone_invalido += 1
        session.commit()
        return

    lead = leads_por_suf.get(_suf8(est.telefone))
    if lead is None:
        return  # sem par canônico → fica no remarketing genérico antigo

    etapa_atual = ids_funil.get(lead.stage_id)
    if etapa_atual is None:
        return  # entrada falhou acima — não avança marco sem funil
    proxima = _PROXIMA_ETAPA.get(etapa_atual)
    if proxima is None:
        # já está no 150 dias — PARA de vez (fica lá; status segue
        # 'remarketing' pra resposta/opt-out continuarem funcionando).
        est.proximo_toque_em = None
        est.atualizado_em = agora
        session.commit()
        res.finalizados_150 += 1
        return

    # TRAVA ANTI-COLISÃO (F1 do B9): lead em conversa VIVA com a Corretor
    # não recebe marco de nutrição — ADIA pro próximo tick (não avança
    # etapa nem re-agenda proximo_toque_em).
    # `agora=agora` — mesma razão da cadência: a janela de 7 dias precisa
    # ser medida do instante do tick, não do relógio real.
    if reengajamento_guard.corretor_conversa_ativa(
        est.telefone, agora=agora, session=session
    ):
        _bloqueio(res, "corretor_ativa")
        log.info(
            "reengajamento_guard_bloqueou",
            motor="nutricao_tick",
            telefone_last4=est.telefone[-4:],
        )
        return

    # TRAVA LIGAÇÃO × NUTRIÇÃO (05/08) — a colisão que a Dona achou.
    # A fila da janela de ligação é feita de lead DESTA régua (funil
    # remarketing, sem dono). Com os 548 leads descongelados em 05/08
    # voltando a receber marco, a mesma pessoa levaria ligação de manhã
    # e mensagem automática à tarde, de duas fontes que não se falam.
    # ADIA (não avança etapa nem re-agenda): o marco ATRASA 3 dias, não
    # se perde.
    if reengajamento_guard.ligacao_recente(
        est.telefone, agora=agora, session=session, tenant_id=tenant_id
    ):
        _bloqueio(res, "ligacao_recente")
        log.info(
            "reengajamento_guard_bloqueou",
            motor="nutricao_tick",
            trava="ligacao_recente",
            telefone_last4=est.telefone[-4:],
        )
        return

    # TRAVA CONVERSA HUMANA (06/08): mesma da cadência — quem está
    # conversando com a corretora não é lead frio e não recebe marco de
    # nutrição por cima da conversa.
    if reengajamento_guard.conversa_humana_ativa(
        est.telefone, agora=agora, session=session, tenant_id=tenant_id
    ):
        _bloqueio(res, "conversa_humana")
        log.info(
            "reengajamento_guard_bloqueou",
            motor="nutricao_tick",
            trava="conversa_humana",
            telefone_last4=est.telefone[-4:],
        )
        return

    # POR QUAL LINHA (07/10): resolvida AGORA, antes do teto — o teto é o da
    # linha por onde o marco vai sair, não um global.
    saida = saida_do_marco(est.corretora_id, session=session)

    # travas anti-ban (idênticas à cadência — mesmo contador/guard). TETO POR
    # LINHA: o marco que sai pela Pessoa10 conta no balde da Pessoa10, o que sai pela Pessoa12
    # no da Pessoa12 (inclusive o de lead da Pessoa10 que caiu pra Pessoa12). A reserva do
    # follow-up fica dentro do balde de cada uma. Antes de 07/10 era o teto
    # GLOBAL (`cadencia_cap_diario`), que somava todas as linhas.
    if cadencia._cap_diario_atingido(
        session, tenant_id, agora, reserva=settings.cadencia_reserva_followup,
        corretora_id=saida.corretora_balde,
    ):
        _bloqueio(res, "cap_diario")
        res.teto_por_linha[saida.slug] = res.teto_por_linha.get(saida.slug, 0) + 1
        return
    if (
        time.monotonic() - cadencia._LAST_ENQUEUE["at"]
        < settings.cadencia_min_intervalo_s
    ):
        _bloqueio(res, "muito_rapido")
        return

    # INTERRUPTOR DA CORRETORA (06/10): relido do banco AGORA, antes desta
    # mensagem. Desligada, ilegível ou sem leitura = não sai; o marco fica
    # devido (atrasa, não se perde).
    pode, motivo = apc.pode_enviar(apc.CHAVE_NUTRICAO, est.corretora_id)
    if not pode:
        _bloqueio(res, motivo or "corretora_desligada")
        return

    texto = COPIES_MARCO[proxima]
    rastro_linha = {"linha": saida.slug, "motivo_linha": saida.motivo,
                    **({"chave_linha_da_dona": saida.chave} if saida.chave else {})}
    try:
        # `origem` nos DOIS caminhos (07/10): liga a trava de opt-out do
        # emissor na hora de sair. Sender que não declara `origem` (os falsos
        # dos testes antigos) segue recebendo só o que sempre recebeu.
        if saida.linha is None:
            # Pessoa12: `enviar_template_corretor`, como sempre — agora com o carimbo.
            resultado = cadencia._chamar_sender(
                sender, est.telefone, texto, com_botao=False,
                origem=ORIGEM_NUTRICAO,
            )
        else:
            # Linha da dona: o mesmo envio do follow-up (`enviar_template_linha`
            # com o thread dela). Sender sem os kwargs novos segue funcionando.
            resultado = cadencia._chamar_sender(
                sender, est.telefone, texto, com_botao=False,
                linha=saida.linha, corretora_id=est.corretora_id,
                origem=ORIGEM_NUTRICAO,
            )
    except Exception as e:  # noqa: BLE001 — falha deste lead, não da volta
        # NÃO marca _LAST_ENQUEUE: nada saiu, o próximo lead pode tentar já.
        res.falhas += 1
        _bloqueio(res, "envio_falhou")
        log.warning("nutricao_envio_falhou", cadencia_id=est.id, marco=proxima,
                    telefone_last4=est.telefone[-4:], erro=str(e)[:200])
        _registrar_evento(
            session, est, toque=None, tipo=cadencia.TIPO_ENVIO_FALHOU,
            canal=CANAL_WHATSAPP,
            payload={"nutricao": True, "marco": proxima, "erro": str(e)[:300],
                     "adiado_ate": (agora + ADIA_APOS_FALHA).isoformat(),
                     "corretora_id": est.corretora_id, **rastro_linha},
            agora=agora,
        )
        est.proximo_toque_em = agora + ADIA_APOS_FALHA
        est.atualizado_em = agora
        session.commit()
        return
    if (resultado or {}).get("erro") == "opt_out":
        # A trava do emissor barrou na hora de sair (o pedido chegou depois da
        # leitura desta volta). Nada saiu: não avança etapa nem marca envio.
        _marcar_opt_out(session, est, agora, [CAMINHO_EMISSOR], res=res,
                        lead=lead, tenant_id=tenant_id)
        session.commit()
        return
    cadencia._LAST_ENQUEUE["at"] = time.monotonic()

    # evento no log da cadência (toque=None não colide com o UNIQUE)
    _registrar_evento(
        session, est, toque=None, tipo=TIPO_MENSAGEM, canal=CANAL_WHATSAPP,
        payload={
            "texto": texto,
            "com_botao": False,
            "categoria": "utility",
            "nutricao": True,
            "marco": proxima,
            "dry_run": bool(resultado.get("dry_run")),
            "sent": bool(resultado.get("sent")),
            # wamid/template: sem isso não dá pra cruzar o evento com o
            # webhook /cloud/status (entrega/leitura por mensagem).
            "wamid": resultado.get("wamid"),
            "template": resultado.get("template"),
            # Por qual linha saiu e por quê (07/10): slug REAL da linha, a dona
            # do lead e o motivo (`dona_com_linha`, `dona_sem_linha`,
            # `template_nao_aprovado_na_waba`, `chave_desligada`). O teto do
            # dia conta este evento no balde de `linha`.
            "corretora_id": est.corretora_id,
            **rastro_linha,
        },
        agora=agora,
    )

    # avança a etapa do lead + tag do card
    lead.stage_id = stages[proxima].id
    lead.updated_at = agora
    _registrar_lead_event_safe(
        session, tenant_id, lead.id, "nutricao_marco",
        {
            "marco": proxima,
            "tag": TAG_POR_ETAPA.get(proxima),
            "cadencia_id": est.id,
            "linha": saida.slug,
        },
    )

    if proxima == STAGE_FINAL:
        est.proximo_toque_em = None  # 5º e último disparo — para aqui
        res.finalizados_150 += 1
    else:
        est.proximo_toque_em = agora + timedelta(days=INTERVALO_DIAS)
    est.atualizado_em = agora
    # COMMIT POR LEAD: o marco que saiu fica gravado antes do próximo envio.
    session.commit()

    res.marcos_disparados += 1
    res.marcos_por_linha[saida.slug] = res.marcos_por_linha.get(saida.slug, 0) + 1
    res.detalhes.append(
        {
            "telefone": est.telefone[-4:],
            "tipo": "marco",
            "marco": proxima,
            "tag": TAG_POR_ETAPA.get(proxima),
            "linha": saida.slug,
            "motivo_linha": saida.motivo,
        }
    )


def _marcar_opt_out(session: Session, est: CadenciaEstado, agora: datetime,
                    caminhos: list[str], *, res: NutricaoResult,
                    lead: Lead | None, tenant_id: int) -> None:
    """`opt_out.tirar_da_regua` com o card canônico da nutrição."""
    opt_out.tirar_da_regua(session, est, agora, caminhos, motor=ORIGEM_NUTRICAO,
                           res=res, lead_id=lead.id if lead is not None else None)


def _marcar_telefone_invalido(session: Session, est: CadenciaEstado,
                              agora: datetime) -> None:
    """Tira o estado da régua com o motivo gravado. Não toca no lead (o
    telefone do card pode ser outro; quem corrige o cadastro é gente)."""
    digitos = re.sub(r"\D", "", est.telefone or "")
    status_antes = est.status
    est.status = STATUS_TELEFONE_INVALIDO
    est.atualizado_em = agora
    _registrar_evento(
        session, est, toque=None, tipo=TIPO_TELEFONE_INVALIDO, canal=CANAL_WHATSAPP,
        payload={"nutricao": True, "digitos": len(digitos),
                 "status_antes": status_antes,
                 "regra": "55 + 10 ou 11 dígitos",
                 "volta": "status de volta pra 'remarketing' depois de corrigir"},
        agora=agora,
    )
    log.warning("nutricao_telefone_invalido", cadencia_id=est.id,
                digitos=len(digitos), telefone_last4=(est.telefone or "")[-4:])


def _registrar_lead_event_safe(
    session: Session, tenant_id: int, lead_id: int, tipo: str, payload: dict[str, Any]
) -> None:
    """lead_events best-effort em SAVEPOINT (`begin_nested` best-effort) —
    espinha parcial em teste não derruba o tick."""
    try:
        from app.services.lead_spine import _registrar_evento as _lead_event

        with session.begin_nested():
            _lead_event(session, tenant_id, lead_id, tipo, payload)
    except Exception as e:  # noqa: BLE001
        log.debug("nutricao_lead_event_falhou", tipo=tipo, erro=str(e)[:120])
