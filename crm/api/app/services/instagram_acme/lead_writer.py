"""Cadastro efetivo do lead da Assistente — tarefa 9 da `OS-ASSISTENTE-IA.md`,
liberada por `OS-ASSISTENTE-IA-TAREFA-9E10.md` (Dona, 21/09/2026 09:29 UTC).

Este é o ÚNICO lugar do projeto Assistente que escreve em `public.leads`. Ele grava
o lead e para: **não chama intake, não roda roleta, não manda boas-vindas, não
preenche `fone8`** (que é coluna gerada e nem aceita escrita). O teste
`tests/test_instagram_lead_writer.py` varre esta fonte atrás dos nomes desses
caminhos e falha se algum aparecer.

## Por que inserção direta, e não `POST /intake/lead`

`lead_intake.registrar_lead` faz três coisas além de gravar: roleta de
corretora, boas-vindas por template WABA e matrícula do fluxo de entrada. A
pessoa da Assistente **já está conversando** — receber "você acabou de se cadastrar,
certo?" em cima da conversa é o erro que queima o atendimento. A OS 9E10 é
explícita: inserção direta em `public.leads`, tenant 1.

## O que a auditoria de consumidores achou (21/09/2026)

Detalhe completo em `projetos/acme/assistente-ia/AUDITORIA-CADASTRO.md`. Resumo
do que virou código aqui:

* **Nenhum trigger em `public.leads`** (o único `trg_*_leads_touch` do cluster é
  do schema `yashav`, outro produto). Nenhuma rule, nenhum LISTEN/NOTIFY.
* **Nenhum motor varre `leads` procurando linha nova.** Cadência, carrossel,
  nutrição e disparo são movidos por `cadencia_estado`,
  `disparos_reengajamento` e `mensagens_whatsapp` — não pela chegada de um lead.
* **Mas três caminhos mudam de comportamento por causa da linha nova**, e dois
  viraram BLOQUEIO aqui, porque nenhum deles é desejado:
  1. `nutricao.tick` fase 1 casa `cadencia_estado(status='remarketing')` com
     lead **por sufixo-8** e, achando, move o lead para a etapa `Base` do funil
     de remarketing — de onde a fase 2 dispara template a cada 30 dias.
     → `_nutricao_capturaria()` bloqueia o cadastro antes de gravar.
  2. `owner_id` na linha da Corretor (remarketing/automação) põe a pessoa no alcance
     do carrossel e do aviso de remarketing.
     → `_validar_owner()` recusa corretora cuja linha é a de automação.
  3. `cadencia._donos_por_suf8` usa o dono do lead como guard por sufixo-8: um
     lead novo com outro dono **pára** uma cadência em andamento de terceiro
     que compartilhe os 8 dígitos finais ("dono_mudou"). Erra para o lado de
     não enviar, então não vira bloqueio — vira registro no relatório.

## Concorrência

`public.leads` **não tem unique por (tenant_id, telefone)** — só por
`external_ref`, que aqui é nulo. Duas gravações simultâneas do mesmo telefone
criariam dois leads. A trava é um `pg_advisory_xact_lock` derivado do telefone
canônico: serializa só quem disputa o mesmo número, morre junto com a transação
e não exige migration (schema é da tarefa 3, não desta).

## Idempotência

Marcada em `lead_events`, que é tabela real e durável: uma linha
`assistente_instagram_cadastro` (ou `..._interacao`, na duplicata) com o
`proposta_id` no payload. Rodar de novo a mesma proposta devolve o mesmo
`lead_id` com `repetido=True` e não escreve nada.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Protocol, Sequence

import structlog
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from app.db.models import Corretora, Lead, LeadEvent
from app.services.instagram_acme.contracts import (
    ORIGEM_LEAD,
    TENANT_ID,
    AcaoProposta,
    ConsultaSupressao,
    ErroAssistente,
    EstadoProposta,
    IdentidadeSocial,
    MotivoBloqueio,
    PropostaCadastroLead,
    exigir_utc,
)
from app.services.phone import canonical_phone_info

log = structlog.get_logger()

# ── Nomes que o cadastro escreve ─────────────────────────────────────────────

#: Lead novo criado pela Assistente.
EVENTO_CADASTRO = "assistente_instagram_cadastro"
#: Telefone já existia no CRM: a proposta vira interação anexada ao lead atual,
#: preservando owner, etapa e origem (a OS proíbe recriar).
EVENTO_INTERACAO = "assistente_instagram_interacao"

#: Etapa canônica de entrada. Nome, não número: `stage_id=6` é referência da
#: SPEC, não garantia — quem resolve é o schema (`lead_spine._resolve_stage_id`).
STAGE_ENTRADA = "Novo"

#: Linha de automação/remarketing (a "Corretor"). Espelha `carrossel_corretor.INSTANCE_CORRETOR`
#: sem importar aquele módulo — o teste afirma que os dois valores batem.
#: Lead da Assistente com dono nesta linha entraria no carrossel de disparo.
INSTANCE_LINHA_REMARKETING = "acme_corretor"

#: Namespace do advisory lock. Número arbitrário e fixo: só precisa não colidir
#: com outro uso de advisory lock no mesmo banco.
_LOCK_NAMESPACE = 0x6265_7469  # "beti"


# ── Motivos de bloqueio próprios desta etapa ─────────────────────────────────


class MotivoCadastro(str, Enum):
    """Motivos que não existem no enum do contrato (tarefa 1) porque só fazem
    sentido na hora de gravar. Ficam aqui em vez de virarem campo novo num
    contrato `frozen` de outro executor."""

    PROPOSTA_NAO_APROVADA = "proposta_nao_aprovada"
    ACAO_ERRADA = "acao_errada"
    TENANT_ERRADO = "tenant_errado"
    AUTORIZACAO_AUSENTE = "autorizacao_ausente"
    OWNER_INEXISTENTE = "owner_inexistente"
    OWNER_INATIVO = "owner_inativo"
    OWNER_DE_AUTOMACAO = "owner_de_automacao"
    STAGE_NAO_RESOLVIDA = "stage_nao_resolvida"
    NUTRICAO_CAPTURARIA_O_LEAD = "nutricao_capturaria_o_lead"
    LEAD_EXISTENTE_DIVERGENTE = "lead_existente_divergente"
    IDEMPOTENCIA_INDISPONIVEL = "idempotencia_indisponivel"


class CadastroBloqueado(ErroAssistente):
    """Revalidação recusou a gravação. Carrega TODOS os motivos, não o primeiro:
    quem lê o log precisa saber tudo que está errado, não descobrir um por vez."""

    def __init__(self, motivos: Sequence[MotivoBloqueio | MotivoCadastro | str]) -> None:
        self.motivos: tuple[str, ...] = tuple(
            m.value if isinstance(m, Enum) else str(m) for m in motivos
        )
        super().__init__("cadastro bloqueado: " + ", ".join(self.motivos))


# ── Entrada e saída ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AutorizacaoCadastro:
    """Quem mandou gravar. Sem isto a operação não existe — cadastro efetivo não
    é efeito colateral de aprovar na tela."""

    autorizacao_dona_ref: str
    responsavel: str

    def validar(self) -> tuple[MotivoCadastro, ...]:
        if not self.autorizacao_dona_ref.strip() or not self.responsavel.strip():
            return (MotivoCadastro.AUTORIZACAO_AUSENTE,)
        return ()


@dataclass(frozen=True)
class ResultadoCadastro:
    lead_id: int
    criado: bool
    anexado: bool
    repetido: bool
    telefone_canonico: str
    owner_id: int | None
    stage_id: int | None
    origem: str


@dataclass
class ContextoCadastro:
    """O que a hora da gravação precisa saber e não está na proposta.

    `telefones_vinculados` são TODOS os telefones com vínculo evidenciado da
    mesma identidade: opt-out em qualquer um deles bloqueia (a regra é "bloqueio
    em qualquer identidade vinculada", não "no telefone desta proposta").
    """

    telefones_vinculados: tuple[str, ...] = ()
    #: Consulta de supressão social (tarefa 5). Ausente = só a canônica por
    #: telefone roda; presente e indisponível = bloqueia.
    supressao_social: ConsultaSupressao | None = None
    resumo_atendimento: str | None = None
    extra_auditoria: dict[str, Any] = field(default_factory=dict)


class ConsultaOptOut(Protocol):
    """Assinatura de `app.services.opt_out.esta_bloqueado`, injetável no teste."""

    def __call__(self, session: Session, telefone: str | None) -> bool: ...


# ── Helpers ──────────────────────────────────────────────────────────────────


def _agora() -> datetime:
    return datetime.now(timezone.utc)


def _chave_lock(telefone_canonico: str) -> int:
    """Segundo argumento do `pg_advisory_xact_lock`, derivado do telefone.

    SHA-256 truncado em 31 bits: cabe em `int4` com sinal positivo e colisão
    aqui só custa serializar dois telefones diferentes por um instante.
    """
    digest = hashlib.sha256(telefone_canonico.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") & 0x7FFF_FFFF


def _suf8(telefone: str | None) -> str:
    return "".join(c for c in (telefone or "") if c.isdigit())[-8:]


def _nutricao_capturaria(session: Session, telefone_canonico: str) -> bool:
    """True se existe estado de remarketing com o mesmo sufixo-8.

    `nutricao.tick` fase 1 indexa os leads por sufixo-8 e, para cada
    `cadencia_estado(status='remarketing')`, move o lead achado para a etapa
    `Base`. Gravar um lead nessa condição é entregar a pessoa para a régua de
    30/60/90/120/150 dias — que manda template de verdade, pela linha da casa.

    Falha de consulta devolve True: não conseguir verificar é motivo de
    bloqueio, igual à consulta de opt-out.
    """
    suf = _suf8(telefone_canonico)
    if len(suf) < 8:
        return True
    try:
        # SAVEPOINT: consulta que falha (tabela ausente, tipo estranho) aborta a
        # transação inteira no Postgres. Isolando, a falha vira "não sei" sem
        # levar junto o resto da revalidação.
        with session.begin_nested():
            achou = session.execute(
                sql_text(
                    r"""SELECT 1 FROM cadencia_estado
                         WHERE status = 'remarketing'
                           AND right(regexp_replace(telefone, '\D', '', 'g'), 8) = :suf
                         LIMIT 1"""
                ),
                {"suf": suf},
            ).scalar()
        return bool(achou)
    except Exception as exc:  # noqa: BLE001
        log.warning("assistente_cadastro_nutricao_indisponivel", erro=str(exc)[:140])
        return True


def _ja_gravado(
    session: Session, tenant_id: int, proposta_id: str
) -> ResultadoCadastro | None:
    """Idempotência durável: esta proposta já virou linha em `lead_events`?

    Falha de consulta **bloqueia** (`CadastroBloqueado`). Fail-open aqui seria
    "não consegui ver se já gravei, então gravo de novo" — que é exatamente a
    duplicata que esta função existe para evitar.
    """
    try:
        with session.begin_nested():
            linha = session.execute(
                sql_text(
                    """SELECT le.lead_id, le.tipo, l.telefone, l.owner_id,
                              l.stage_id, l.origem
                         FROM lead_events le
                         JOIN leads l ON l.id = le.lead_id
                        WHERE le.tenant_id = :t
                          AND le.tipo IN (:c, :i)
                          AND le.payload->>'proposta_id' = :p
                        ORDER BY le.id ASC
                        LIMIT 1"""
                ),
                {"t": tenant_id, "c": EVENTO_CADASTRO, "i": EVENTO_INTERACAO, "p": proposta_id},
            ).first()
    except Exception as exc:  # noqa: BLE001
        log.warning("assistente_cadastro_idempotencia_indisponivel", erro=str(exc)[:140])
        raise CadastroBloqueado((MotivoCadastro.IDEMPOTENCIA_INDISPONIVEL,)) from exc
    if linha is None:
        return None
    lead_id, tipo, telefone, owner_id, stage_id, origem = linha
    return ResultadoCadastro(
        lead_id=int(lead_id),
        criado=False,
        anexado=(tipo == EVENTO_INTERACAO),
        repetido=True,
        telefone_canonico=str(telefone or ""),
        owner_id=owner_id,
        stage_id=stage_id,
        origem=str(origem or ""),
    )


def _validar_owner(session: Session, owner_id: int | None) -> tuple[MotivoCadastro, ...]:
    """Corretora tem que existir, estar ativa e NÃO ser a linha de automação."""
    if owner_id is None:
        return (MotivoCadastro.OWNER_INEXISTENTE,)
    corretora = session.get(Corretora, owner_id)
    if corretora is None:
        return (MotivoCadastro.OWNER_INEXISTENTE,)
    if not corretora.ativo:
        return (MotivoCadastro.OWNER_INATIVO,)
    if (corretora.evolution_instance or "") == INSTANCE_LINHA_REMARKETING:
        return (MotivoCadastro.OWNER_DE_AUTOMACAO,)
    return ()


def _consultar_supressao(
    session: Session,
    telefones: Sequence[str],
    consulta: ConsultaOptOut | None,
) -> tuple[MotivoBloqueio, ...]:
    """Opt-out canônico em QUALQUER telefone vinculado. Falha bloqueia."""
    if consulta is None:
        from app.services import opt_out

        consulta = opt_out.esta_bloqueado
    for telefone in telefones:
        try:
            with session.begin_nested():
                bloqueado = consulta(session, telefone)
            if bloqueado:
                return (MotivoBloqueio.OPT_OUT_TELEFONE,)
        except Exception as exc:  # noqa: BLE001
            log.warning("assistente_cadastro_opt_out_indisponivel", erro=str(exc)[:140])
            return (MotivoBloqueio.CONSULTA_SUPRESSAO_INDISPONIVEL,)
    return ()


def _revalidar(
    proposta: PropostaCadastroLead,
    autorizacao: AutorizacaoCadastro,
    contexto: ContextoCadastro,
    session: Session,
    consulta_opt_out: ConsultaOptOut | None,
) -> tuple[list[str], str, int | None]:
    """Tudo que tem que ser verdade AGORA, não na hora em que alguém aprovou.

    Devolve `(motivos, telefone_canonico, stage_id)`.
    """
    motivos: list[str] = [m.value for m in autorizacao.validar()]

    if proposta.acao is not AcaoProposta.CADASTRO_LEAD:
        motivos.append(MotivoCadastro.ACAO_ERRADA.value)
    if proposta.estado is not EstadoProposta.APROVADO:
        motivos.append(MotivoCadastro.PROPOSTA_NAO_APROVADA.value)
    if proposta.tenant_id != TENANT_ID:
        motivos.append(MotivoCadastro.TENANT_ERRADO.value)
    if proposta.bloqueios:
        motivos.extend(b.value for b in proposta.bloqueios)
    if proposta.pendencias:
        motivos.append(MotivoBloqueio.FATO_PENDENTE_NAO_RESOLVIDO.value)
    if proposta.consentimento is None:
        motivos.append(MotivoBloqueio.SEM_CONSENTIMENTO.value)
    if proposta.origem != ORIGEM_LEAD:
        motivos.append(MotivoCadastro.TENANT_ERRADO.value)

    # Telefone: canônico completo, sem adivinhar DDD. Ambíguo (12 dígitos
    # começando em 6/7/8) NÃO vira cadastro — vai para triagem humana.
    canonico = ""
    if proposta.vinculo is None:
        motivos.append(MotivoBloqueio.TELEFONE_AMBIGUO.value)
    else:
        canon, ambiguo = canonical_phone_info(proposta.vinculo.telefone_e164)
        if canon is None or ambiguo:
            motivos.append(MotivoBloqueio.TELEFONE_AMBIGUO.value)
        else:
            canonico = canon

    # Supressão social (tarefa 5) — indisponível bloqueia, por contrato.
    social = contexto.supressao_social
    if social is not None and (social.bloqueado or not social.disponivel):
        motivos.extend(m.value for m in social.motivos)

    telefones = [t for t in (canonico, *contexto.telefones_vinculados) if t]
    motivos.extend(m.value for m in _consultar_supressao(session, telefones, consulta_opt_out))

    # Owner humano ativo — só quando a proposta cria lead. Anexar a lead
    # existente preserva o dono que já está lá e não escolhe ninguém.
    if not proposta.anexar_a_lead_existente:
        motivos.extend(m.value for m in _validar_owner(session, proposta.owner_id))

    # Etapa validada no schema (o 6 da SPEC é referência, não garantia).
    from app.services.lead_spine import _resolve_stage_id

    stage_id = _resolve_stage_id(session, proposta.tenant_id, None, STAGE_ENTRADA)
    if stage_id is None:
        motivos.append(MotivoCadastro.STAGE_NAO_RESOLVIDA.value)

    # Régua de nutrição: ver docstring do módulo.
    if canonico and _nutricao_capturaria(session, canonico):
        motivos.append(MotivoCadastro.NUTRICAO_CAPTURARIA_O_LEAD.value)

    # Ordem estável e sem repetição — motivo duplicado em log vira ruído.
    vistos: list[str] = []
    for m in motivos:
        if m not in vistos:
            vistos.append(m)
    return vistos, canonico, stage_id


def _payload_auditoria(
    proposta: PropostaCadastroLead,
    autorizacao: AutorizacaoCadastro,
    contexto: ContextoCadastro,
    agora: datetime,
) -> dict[str, Any]:
    """Payload do `lead_events`. Sem token, sem telefone completo, sem o texto
    da pessoa: o que fica aqui é rastro de decisão, não cópia da conversa."""
    consentimento = proposta.consentimento
    identidade: IdentidadeSocial = proposta.identidade
    payload: dict[str, Any] = {
        "proposta_id": proposta.proposta_id,
        "versao": proposta.versao,
        "acao": proposta.acao.value,
        "origem": proposta.origem,
        "origem_trafego": proposta.origem_trafego.value,
        "plataforma": identidade.plataforma.value,
        "conta": identidade.conta,
        "ator_id": identidade.ator_id,
        "evento_id": proposta.evento_id,
        "eventos_agrupados": list(proposta.eventos_agrupados),
        "autorizacao_dona_ref": autorizacao.autorizacao_dona_ref,
        "responsavel": autorizacao.responsavel,
        "revisora": proposta.revisora,
        "gravado_em": agora.isoformat(),
    }
    if consentimento is not None:
        payload["consentimento"] = {
            "evidencia_id": consentimento.evidencia_id,
            "finalidade": consentimento.finalidade,
            "aceito_em": consentimento.aceito_em.isoformat(),
        }
    if proposta.vinculo is not None:
        payload["vinculo_fonte"] = proposta.vinculo.fonte.value
        payload["vinculo_evidencia_id"] = proposta.vinculo.evidencia_id
    if contexto.resumo_atendimento:
        payload["resumo_atendimento"] = contexto.resumo_atendimento
    if contexto.extra_auditoria:
        payload["extra"] = contexto.extra_auditoria
    return payload


# ── Operação ─────────────────────────────────────────────────────────────────


def gravar_cadastro(
    proposta: PropostaCadastroLead,
    *,
    autorizacao: AutorizacaoCadastro,
    session: Session,
    contexto: ContextoCadastro | None = None,
    agora: datetime | None = None,
    consulta_opt_out: ConsultaOptOut | None = None,
) -> ResultadoCadastro:
    """Grava (ou anexa) o lead. Operação separada da aprovação, autorizada,
    idempotente e transacional.

    Commita ao final: o `pg_advisory_xact_lock` só solta no fim da transação, e
    segurar o lock esperando o caller commitar seria serializar a fila inteira
    pelo tempo de vida de uma request.

    Levanta `CadastroBloqueado` sem gravar nada quando qualquer revalidação
    falha. Não levanta `EnvioNaoPermitido` porque não existe envio aqui: este
    módulo não importa transporte nenhum, e o teste afirma isso varrendo a fonte.
    """
    try:
        return _gravar(
            proposta,
            autorizacao=autorizacao,
            session=session,
            contexto=contexto or ContextoCadastro(),
            agora=exigir_utc(agora or _agora(), "agora"),
            consulta_opt_out=consulta_opt_out,
        )
    except CadastroBloqueado:
        # Nada meio-gravado: `_resolve_stage_id` pode ter criado uma etapa no
        # catálogo durante a revalidação, e proposta bloqueada não deixa rastro.
        session.rollback()
        raise


def _gravar(
    proposta: PropostaCadastroLead,
    *,
    autorizacao: AutorizacaoCadastro,
    session: Session,
    contexto: ContextoCadastro,
    agora: datetime,
    consulta_opt_out: ConsultaOptOut | None,
) -> ResultadoCadastro:
    motivos, canonico, stage_id = _revalidar(
        proposta, autorizacao, contexto, session, consulta_opt_out
    )
    if motivos:
        log.warning(
            "assistente_cadastro_bloqueado",
            proposta_id=proposta.proposta_id,
            motivos=motivos,
        )
        raise CadastroBloqueado(motivos)

    # Idempotência antes do lock: o caso comum (repetiu o clique) nem disputa.
    repetido = _ja_gravado(session, proposta.tenant_id, proposta.proposta_id)
    if repetido is not None:
        log.info(
            "assistente_cadastro_repetido",
            proposta_id=proposta.proposta_id,
            lead_id=repetido.lead_id,
        )
        return repetido

    # Serializa quem disputa o MESMO telefone. Sem isto, duas propostas
    # concorrentes criam dois leads: `leads` não tem unique por telefone.
    session.execute(
        sql_text("SELECT pg_advisory_xact_lock(:ns, :chave)"),
        {"ns": _LOCK_NAMESPACE, "chave": _chave_lock(canonico)},
    )

    # Reconfere a idempotência JÁ com o lock: o concorrente pode ter gravado
    # entre a checagem de cima e a aquisição.
    repetido = _ja_gravado(session, proposta.tenant_id, proposta.proposta_id)
    if repetido is not None:
        session.commit()
        return repetido

    from app.services.lead_spine import encontrar_lead

    existente = encontrar_lead(session, proposta.tenant_id, telefone=canonico)

    if proposta.lead_existente_id is not None and existente is not None:
        if int(existente.id) != int(proposta.lead_existente_id):
            raise CadastroBloqueado((MotivoCadastro.LEAD_EXISTENTE_DIVERGENTE,))
    if proposta.anexar_a_lead_existente and existente is None:
        raise CadastroBloqueado((MotivoCadastro.LEAD_EXISTENTE_DIVERGENTE,))

    payload = _payload_auditoria(proposta, autorizacao, contexto, agora)

    if existente is not None:
        # DUPLICATA: preserva owner, etapa e origem do lead que já existe.
        # Nenhum campo do lead é tocado — nem `updated_at`, que outros motores
        # leem como marco ("quando este lead mexeu pela última vez").
        payload["anexado_a_lead_existente"] = True
        session.add(
            LeadEvent(
                tenant_id=proposta.tenant_id,
                lead_id=existente.id,
                tipo=EVENTO_INTERACAO,
                payload=payload,
                at=agora,
            )
        )
        session.commit()
        log.info(
            "assistente_cadastro_anexado",
            proposta_id=proposta.proposta_id,
            lead_id=int(existente.id),
        )
        return ResultadoCadastro(
            lead_id=int(existente.id),
            criado=False,
            anexado=True,
            repetido=False,
            telefone_canonico=canonico,
            owner_id=existente.owner_id,
            stage_id=existente.stage_id,
            origem=str(existente.origem or ""),
        )

    lead = Lead(
        tenant_id=proposta.tenant_id,
        external_ref=None,
        nome=None,
        telefone=canonico,
        owner_id=proposta.owner_id,
        stage_id=stage_id,
        origem=ORIGEM_LEAD,
        criado_em_origem=agora,
        created_at=agora,
        updated_at=agora,
    )
    session.add(lead)
    session.flush()  # popula lead.id
    session.add(
        LeadEvent(
            tenant_id=proposta.tenant_id,
            lead_id=lead.id,
            tipo=EVENTO_CADASTRO,
            payload=payload,
            at=agora,
        )
    )
    session.commit()
    log.info(
        "assistente_cadastro_criado",
        proposta_id=proposta.proposta_id,
        lead_id=int(lead.id),
        owner_id=proposta.owner_id,
    )
    return ResultadoCadastro(
        lead_id=int(lead.id),
        criado=True,
        anexado=False,
        repetido=False,
        telefone_canonico=canonico,
        owner_id=proposta.owner_id,
        stage_id=stage_id,
        origem=ORIGEM_LEAD,
    )


__all__ = [
    "AutorizacaoCadastro",
    "CadastroBloqueado",
    "ContextoCadastro",
    "EVENTO_CADASTRO",
    "EVENTO_INTERACAO",
    "INSTANCE_LINHA_REMARKETING",
    "MotivoCadastro",
    "ResultadoCadastro",
    "STAGE_ENTRADA",
    "gravar_cadastro",
]
