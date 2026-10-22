"""Contratos da Assistente IA — formatos, estados e interfaces consumidos pelas
tarefas 3 a 7 da `projetos/acme/OS-ASSISTENTE-IA.md`.

Este módulo é **só contrato**: tipos, enums, invariantes, máquina de estados e
`Protocol`s de repositório. Não abre conexão, não fala com a Meta, não escreve
em lugar nenhum. É o arquivo que permite 3–7 trabalharem em paralelo sem editar
os mesmos arquivos.

O que este contrato garante, e por que cada garantia existe:

* **Não existe saída.** Nenhuma rota, função ou campo aqui envia mensagem ou
  cria lead. `ROTAS_PROIBIDAS_V1` deixa isso explícito e o teste de contrato
  varre o módulo atrás de qualquer coisa que pareça um caminho de envio. Aprovar
  na tela grava uma decisão — quem executa é uma pessoa, no app, com autorização
  da Dona por escrito (`RegistroExecucaoManual`).
* **Teto de produto, não de gosto.** `TetosAssistente` aceita 1 resposta pública por
  comentário e 1 DM inicial por pessoa (na operação inteira, sem reset por post,
  campanha ou dia). Aceita reduzir para zero; **rejeita** aumentar, e rejeita
  qualquer valor positivo em follow-up automático ou envio pelo sistema. Mudar
  configuração não pode habilitar envio.
* **Janela é do evento, não da fila.** Private reply vence 7 dias depois da
  *criação do comentário* (`>=` no instante exato: no segundo 7×24h já está
  fechada). Continuidade exige mensagem recebida da pessoa há menos de 24h.
  Aprovar não renova prazo — por isso toda função de janela recebe `agora`
  injetado e é rechecada na hora da decisão, nunca só na montagem do rascunho.
* **Identidade é escopada.** A chave de pessoa é
  `(tenant, plataforma, conta, ator_id)`. Username muda, `fone8` colide, nome
  se repete: nenhum dos três é identidade. Telefone só entra vinculado com
  evidência que a própria pessoa deu (`VinculoTelefone`).
* **Bloqueio ganha de classificação.** `MotivoBloqueio` não é sobreposto por
  aprovação. Falha ao consultar supressão bloqueia (`ConsultaSupressao` com
  `disponivel=False` → bloqueado), porque "não consegui verificar" nunca pode
  virar "pode mandar".

Identificadores medidos (SPEC + tarefa 0, ver `ACESSOS-E-COBERTURA.md`):
conta Instagram `000000000000000` (@acme_imoveis), página
`000000000000000`, conta de anúncios `act_000000000000`, tenant 1.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable, Iterable, Protocol, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ── Identificadores fixos desta operação ─────────────────────────────────────

TENANT_ID = 1
CONTA_INSTAGRAM = "000000000000000"
PAGINA_FACEBOOK = "000000000000000"
CONTA_ANUNCIOS = "act_000000000000"

#: Valor de `leads.origem` proposto para o cadastro (tag que o painel mostra no
#: card). Nomenclatura pendente de validação da Dona — ver CONTRATOS.md §7.
ORIGEM_LEAD = "Instagram / rede social"

# ── Janelas (UTC, relógio injetável) ─────────────────────────────────────────

#: Private reply: até 7 dias da CRIAÇÃO do comentário. Fechamos no instante
#: exato (>=), não em 7d+1s — margem a favor da conta do cliente.
JANELA_PRIVATE_REPLY = timedelta(days=7)
#: Continuidade: só depois de mensagem da pessoa, até 24h da última recebida.
JANELA_CONTINUIDADE = timedelta(hours=24)

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    """Relógio padrão. Todo cálculo de janela recebe um `Clock` — em teste se
    injeta um relógio fixo em vez de dormir ou de tolerar segundos de folga."""
    return datetime.now(timezone.utc)


def exigir_utc(valor: datetime, campo: str = "timestamp") -> datetime:
    """Datetime sem timezone é bug de janela esperando acontecer: 7 dias em
    horário de Cidade não são 7 dias em UTC. Contrato só aceita aware."""
    if valor.tzinfo is None:
        raise ContratoInvalido(f"{campo} precisa ser datetime aware em UTC")
    return valor.astimezone(timezone.utc)


# ── Erros do contrato ────────────────────────────────────────────────────────


class ErroAssistente(Exception):
    """Base de tudo que este módulo levanta."""


class ContratoInvalido(ErroAssistente):
    """Payload/estrutura fora do contrato."""


class TransicaoInvalida(ErroAssistente):
    """Mudança de estado que a máquina não permite."""


class VersaoDesatualizada(ErroAssistente):
    """Revisão otimista: alguém editou a proposta entre a leitura e a decisão."""


class TetoInvalido(ErroAssistente):
    """Configuração tentou afrouxar o teto de produto ou habilitar envio."""


class SupressaoIndisponivel(ErroAssistente):
    """Consulta de opt-out falhou. Quem não consegue verificar, não aborda."""


class EnvioNaoPermitido(ErroAssistente):
    """Alguém chamou um caminho de saída. Na V1 não existe caminho de saída."""


# ── Enums ────────────────────────────────────────────────────────────────────


class Plataforma(str, Enum):
    INSTAGRAM = "instagram"
    #: Facebook entra IDENTIFICADO, em fila própria, e não atravessa o fluxo
    #: Instagram. Anúncio na mesma conta não implica mesmo produto nem mesma API.
    FACEBOOK = "facebook"


class TipoEvento(str, Enum):
    COMENTARIO = "comentario"
    RESPOSTA_COMENTARIO = "resposta_comentario"
    MENSAGEM_RECEBIDA = "mensagem_recebida"
    #: Mensagem que a própria conta enviou, devolvida pelo webhook. Nunca vira
    #: proposta; serve para saber que a conversa já tem gente humana dentro.
    ECO_MENSAGEM = "eco_mensagem"
    #: Comentário da própria conta (inclusive resposta já publicada). Consome
    #: o teto de resposta pública daquele comentário — ver `ConsumoTeto`.
    ECO_COMENTARIO = "eco_comentario"


class OrigemTrafego(str, Enum):
    PAGO = "pago"
    ORGANICO = "organico"
    #: Anúncio dinâmico pode não retornar ad ID. Ausência de ad ID **não** é
    #: prova de orgânico — é isto aqui.
    NAO_DETERMINADO = "nao_determinado"


class GrupoClasse(str, Enum):
    INTERESSE = "interesse"
    PERGUNTA_FACTUAL = "pergunta_factual"
    CRITICA = "critica"
    SPAM_APARENTE = "spam_aparente"
    AMBIGUIDADE = "ambiguidade"
    #: Atravessa qualquer grupo e ganha dele.
    TRANSVERSAL = "transversal"


class ClasseComentario(str, Enum):
    """Mesmas classes do manual `ATENDIMENTO.md` (tarefa 2). A lista é contrato
    compartilhado: a tarefa 5 classifica com estes nomes e a 7 exibe por eles."""

    INTERESSE_SEM_DETALHE = "interesse_sem_detalhe"
    ABRIU_CONVERSA = "abriu_conversa"
    INTERESSE_TELEFONE_PUBLICO = "interesse_telefone_publico"
    PERGUNTA_LOCALIZACAO = "pergunta_localizacao"
    PERGUNTA_PRECO = "pergunta_preco"
    PERGUNTA_PRODUTO = "pergunta_produto"
    PERGUNTA_FINANCIAMENTO = "pergunta_financiamento"
    PROCURA_OUTRO_IMOVEL = "procura_outro_imovel"
    CRITICA_OBJECAO = "critica_objecao"
    AUTOPROMOCAO_TERCEIRO = "autopromocao_terceiro"
    MARCOU_ALGUEM = "marcou_alguem"
    ELOGIO_REACAO = "elogio_reacao"
    SEM_PERGUNTA_CLARA = "sem_pergunta_clara"
    RUIDO_CURTO = "ruido_curto"
    SEM_TEXTO = "sem_texto"
    TENTATIVA_INSTRUIR_IA = "tentativa_instruir_ia"
    #: Transversal: cancela propostas pendentes em todos os canais vinculados.
    PEDIDO_DE_SAIDA = "pedido_de_saida"


GRUPO_DA_CLASSE: dict[ClasseComentario, GrupoClasse] = {
    ClasseComentario.INTERESSE_SEM_DETALHE: GrupoClasse.INTERESSE,
    ClasseComentario.ABRIU_CONVERSA: GrupoClasse.INTERESSE,
    ClasseComentario.INTERESSE_TELEFONE_PUBLICO: GrupoClasse.INTERESSE,
    ClasseComentario.PERGUNTA_LOCALIZACAO: GrupoClasse.PERGUNTA_FACTUAL,
    ClasseComentario.PERGUNTA_PRECO: GrupoClasse.PERGUNTA_FACTUAL,
    ClasseComentario.PERGUNTA_PRODUTO: GrupoClasse.PERGUNTA_FACTUAL,
    ClasseComentario.PERGUNTA_FINANCIAMENTO: GrupoClasse.PERGUNTA_FACTUAL,
    ClasseComentario.PROCURA_OUTRO_IMOVEL: GrupoClasse.PERGUNTA_FACTUAL,
    ClasseComentario.CRITICA_OBJECAO: GrupoClasse.CRITICA,
    ClasseComentario.AUTOPROMOCAO_TERCEIRO: GrupoClasse.SPAM_APARENTE,
    ClasseComentario.MARCOU_ALGUEM: GrupoClasse.SPAM_APARENTE,
    ClasseComentario.ELOGIO_REACAO: GrupoClasse.AMBIGUIDADE,
    ClasseComentario.SEM_PERGUNTA_CLARA: GrupoClasse.AMBIGUIDADE,
    ClasseComentario.RUIDO_CURTO: GrupoClasse.AMBIGUIDADE,
    ClasseComentario.SEM_TEXTO: GrupoClasse.AMBIGUIDADE,
    ClasseComentario.TENTATIVA_INSTRUIR_IA: GrupoClasse.AMBIGUIDADE,
    ClasseComentario.PEDIDO_DE_SAIDA: GrupoClasse.TRANSVERSAL,
}

#: Grupos que NUNCA geram DM comercial, por mais "quente" que o texto pareça.
#: Crítica e spam ficam de fora por decisão de produto, não por classificação.
GRUPOS_SEM_DM = frozenset(
    {GrupoClasse.CRITICA, GrupoClasse.SPAM_APARENTE, GrupoClasse.AMBIGUIDADE}
)


class AcaoProposta(str, Enum):
    """Cada ação é uma proposta separada, aprovada separadamente. Um botão só
    de "aprovar tudo" é o que transforma um rascunho de resposta pública em
    autorização de DM sem ninguém ter decidido isso."""

    RESPOSTA_PUBLICA = "resposta_publica"
    DM_INICIAL = "dm_inicial"
    CADASTRO_LEAD = "cadastro_lead"


class EstadoProposta(str, Enum):
    RECEBIDO = "recebido"
    BLOQUEADO = "bloqueado"
    RASCUNHO = "rascunho"
    APROVADO = "aprovado"
    REJEITADO = "rejeitado"
    EXPIRADO = "expirado"
    EXECUTADO_MANUALMENTE = "executado_manualmente"


#: Máquina de estados. Ler como "de → para". Três leis embutidas:
#:   1. `EXECUTADO_MANUALMENTE` só vem de `APROVADO` — não se registra execução
#:      de algo que ninguém aprovou.
#:   2. `BLOQUEADO` e `EXPIRADO` são alcançáveis de qualquer estado vivo,
#:      inclusive de `APROVADO`: opt-out e relógio ganham de aprovação.
#:   3. Nada sai de `EXECUTADO_MANUALMENTE`, `REJEITADO` ou `EXPIRADO`.
TRANSICOES: dict[EstadoProposta, frozenset[EstadoProposta]] = {
    EstadoProposta.RECEBIDO: frozenset(
        {EstadoProposta.RASCUNHO, EstadoProposta.BLOQUEADO, EstadoProposta.EXPIRADO}
    ),
    EstadoProposta.RASCUNHO: frozenset(
        {
            EstadoProposta.RASCUNHO,  # edição gera nova versão, segue rascunho
            EstadoProposta.APROVADO,
            EstadoProposta.REJEITADO,
            EstadoProposta.BLOQUEADO,
            EstadoProposta.EXPIRADO,
        }
    ),
    EstadoProposta.APROVADO: frozenset(
        {
            EstadoProposta.RASCUNHO,  # editar invalida a aprovação
            EstadoProposta.EXECUTADO_MANUALMENTE,
            EstadoProposta.REJEITADO,
            EstadoProposta.BLOQUEADO,
            EstadoProposta.EXPIRADO,
        }
    ),
    EstadoProposta.BLOQUEADO: frozenset({EstadoProposta.REJEITADO, EstadoProposta.EXPIRADO}),
    EstadoProposta.REJEITADO: frozenset(),
    EstadoProposta.EXPIRADO: frozenset(),
    EstadoProposta.EXECUTADO_MANUALMENTE: frozenset(),
}


class MotivoBloqueio(str, Enum):
    OPT_OUT_SOCIAL = "opt_out_social"
    OPT_OUT_TELEFONE = "opt_out_telefone"
    #: Perfil sem vínculo confiável com telefone: não dá para afirmar que aquela
    #: pessoa não pediu para sair. Bloqueia abordagem proativa (DM), não a
    #: resposta pública.
    IDENTIDADE_NAO_CONCILIADA = "identidade_nao_conciliada"
    CONSULTA_SUPRESSAO_INDISPONIVEL = "consulta_supressao_indisponivel"
    JANELA_PRIVATE_REPLY_VENCIDA = "janela_private_reply_vencida"
    JANELA_CONTINUIDADE_VENCIDA = "janela_continuidade_vencida"
    TETO_RESPOSTA_PUBLICA_CONSUMIDO = "teto_resposta_publica_consumido"
    TETO_DM_CONSUMIDO = "teto_dm_consumido"
    CLASSE_SEM_DM = "classe_sem_dm"
    CONVERSA_HUMANA_EM_ANDAMENTO = "conversa_humana_em_andamento"
    ESTADO_DE_ENVIO_DESCONHECIDO = "estado_de_envio_desconhecido"
    PLATAFORMA_FORA_DO_FLUXO = "plataforma_fora_do_fluxo"
    CONTA_NAO_PERMITIDA = "conta_nao_permitida"
    SEM_CONSENTIMENTO = "sem_consentimento"
    TELEFONE_AMBIGUO = "telefone_ambiguo"
    SEM_OWNER_ESCOLHIDO = "sem_owner_escolhido"
    FATO_PENDENTE_NAO_RESOLVIDO = "fato_pendente_nao_resolvido"


#: Bloqueios que impedem DM mas deixam a resposta pública de pé. Resposta em
#: comentário não é abordagem proativa — é atendimento de quem falou em público.
BLOQUEIOS_SO_DE_DM = frozenset(
    {
        MotivoBloqueio.IDENTIDADE_NAO_CONCILIADA,
        MotivoBloqueio.JANELA_PRIVATE_REPLY_VENCIDA,
        MotivoBloqueio.JANELA_CONTINUIDADE_VENCIDA,
        MotivoBloqueio.TETO_DM_CONSUMIDO,
        MotivoBloqueio.CLASSE_SEM_DM,
    }
)


class EstadoIdentidade(str, Enum):
    #: Telefone vinculado com evidência da própria pessoa, opt-out consultável.
    CONCILIADA = "conciliada"
    #: Só IGSID. É o caso de 100% da base coletada na tarefa 0.
    NAO_CONCILIADA = "nao_conciliada"
    BLOQUEADA = "bloqueada"


class FonteEvidenciaVinculo(str, Enum):
    """Como o telefone chegou até nós. Só as duas primeiras valem para vincular."""

    INFORMADO_EM_DM = "informado_em_dm"
    INFORMADO_EM_FORMULARIO = "informado_em_formulario"
    #: Telefone que a pessoa publicou em comentário. NÃO vincula e NÃO cadastra
    #: — 5 casos assim na coleta da tarefa 0, todos mascarados.
    PUBLICADO_EM_COMENTARIO = "publicado_em_comentario"
    #: Alguém achou parecido. Nunca vincula.
    INFERIDO = "inferido"


FONTES_QUE_VINCULAM = frozenset(
    {FonteEvidenciaVinculo.INFORMADO_EM_DM, FonteEvidenciaVinculo.INFORMADO_EM_FORMULARIO}
)


# ── Base dos modelos ─────────────────────────────────────────────────────────


class _Contrato(BaseModel):
    """Imutável e fechado: campo a mais é erro, não "extra ignorado". Um typo em
    `bloqueios` que virasse `bloqueio` passaria silencioso e a proposta chegaria
    na fila sem motivo nenhum."""

    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=False)


# ── Identidade ───────────────────────────────────────────────────────────────


class IdentidadeSocial(_Contrato):
    """Chave de pessoa nesta operação: `(tenant, plataforma, conta, ator_id)`.

    `ator_id` é o ID escopado que a Meta devolve (IGSID/PSID) — escopado por app
    e por conta, então **não** é global e não pode ser comparado com ID de outra
    conta. `username` é dado de exibição, nunca chave: muda quando a pessoa
    quiser, e duas contas diferentes podem ter tido o mesmo @ em épocas
    diferentes.
    """

    tenant_id: int = TENANT_ID
    plataforma: Plataforma
    conta: str = Field(min_length=1)
    ator_id: str = Field(min_length=1)
    username: str | None = None

    @property
    def chave(self) -> tuple[int, str, str, str]:
        return (self.tenant_id, self.plataforma.value, self.conta, self.ator_id)


class VinculoTelefone(_Contrato):
    """Ligação identidade social ↔ telefone E.164, sempre com evidência.

    Nunca se vincula por nome, username ou sufixo de telefone. O sufixo-8 é a
    chave que a consulta canônica de opt-out usa para BLOQUEAR (errar para o
    lado de bloquear é barato) — mas como identidade ele junta gente diferente,
    e é por isso que ele não aparece aqui.
    """

    identidade: IdentidadeSocial
    telefone_e164: str = Field(pattern=r"^55\d{10,11}$")
    fonte: FonteEvidenciaVinculo
    evidencia_id: str = Field(min_length=1, description="ID da mensagem que trouxe o telefone")
    informado_em: datetime

    @field_validator("informado_em")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        return exigir_utc(v, "informado_em")

    @model_validator(mode="after")
    def _fonte_vincula(self) -> "VinculoTelefone":
        if self.fonte not in FONTES_QUE_VINCULAM:
            raise ContratoInvalido(
                f"fonte {self.fonte.value} não vincula telefone a identidade "
                "(telefone público ou inferido não é consentimento)"
            )
        return self


class ConsultaSupressao(_Contrato):
    """Resposta da consulta de opt-out. Três respostas possíveis, não duas.

    `disponivel=False` é a terceira: a consulta falhou. Nesse caso
    `bloqueado` é forçado para True — quem não consegue verificar, não aborda.
    """

    disponivel: bool
    bloqueado: bool
    motivos: tuple[MotivoBloqueio, ...] = ()
    consultado_em: datetime
    fonte: str = Field(default="opt_out.esta_bloqueado", min_length=1)

    @field_validator("consultado_em")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        return exigir_utc(v, "consultado_em")

    @model_validator(mode="after")
    def _falha_bloqueia(self) -> "ConsultaSupressao":
        if not self.disponivel:
            if not self.bloqueado:
                raise ContratoInvalido(
                    "consulta indisponível precisa bloquear (fail-closed)"
                )
            if MotivoBloqueio.CONSULTA_SUPRESSAO_INDISPONIVEL not in self.motivos:
                raise ContratoInvalido(
                    "consulta indisponível precisa registrar o motivo "
                    "consulta_supressao_indisponivel"
                )
        if self.bloqueado and not self.motivos:
            raise ContratoInvalido("bloqueio sem motivo não é auditável")
        return self


class Supressao(_Contrato):
    """Pedido de saída registrado, por identidade social e/ou por telefone.

    Escopo social cobre quem pediu para sair antes de dar telefone — que é o
    caso normal no Instagram. Quando existe vínculo com telefone, a escrita
    canônica é a do `app/services/opt_out.py`; este registro é o espelho local
    que a fila consulta, não uma segunda fonte da verdade.
    """

    identidade: IdentidadeSocial | None = None
    telefone_e164: str | None = Field(default=None, pattern=r"^55\d{10,11}$")
    motivo: str = Field(min_length=1)
    evidencia_id: str | None = None
    registrado_em: datetime
    #: True quando o pedido também foi gravado na fonte canônica por telefone.
    propagado_para_canonico: bool = False

    @field_validator("registrado_em")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        return exigir_utc(v, "registrado_em")

    @model_validator(mode="after")
    def _tem_alvo(self) -> "Supressao":
        if self.identidade is None and self.telefone_e164 is None:
            raise ContratoInvalido("supressão precisa de identidade social ou telefone")
        return self


# ── Evento normalizado ───────────────────────────────────────────────────────


class ContextoAnuncio(_Contrato):
    """O que dá para afirmar sobre a mídia onde a pessoa falou.

    `no_grid=False` é o caso REAL da Acme: as 19 mídias com comentário são
    dark ads, interseção zero com as 153 do feed (medido na tarefa 0 e conferido
    de novo em 21/09). Ferramenta de fluxo que só enxerga post do grid não cobre
    nada disso.
    """

    media_id: str | None = None
    permalink: str | None = None
    ad_id: str | None = None
    campanha: str | None = None
    empreendimento: str | None = None
    no_grid: bool | None = None
    origem: OrigemTrafego = OrigemTrafego.NAO_DETERMINADO

    @model_validator(mode="after")
    def _sem_ad_id_nao_e_organico(self) -> "ContextoAnuncio":
        if self.origem is OrigemTrafego.ORGANICO and self.ad_id:
            raise ContratoInvalido("evento com ad_id não pode ser marcado como orgânico")
        return self


class EventoNormalizado(_Contrato):
    """Um evento recebido (webhook ou leitura), já normalizado e deduplicável.

    `evento_id` é a chave de idempotência: o mesmo comentário entregue três
    vezes (a Meta reentrega quando o 200 demora) tem que virar UM evento.
    """

    tenant_id: int = TENANT_ID
    plataforma: Plataforma
    conta: str = Field(min_length=1)
    evento_id: str = Field(min_length=1)
    tipo: TipoEvento
    autor: IdentidadeSocial
    texto: str | None = None
    criado_em: datetime
    recebido_em: datetime
    contexto: ContextoAnuncio = ContextoAnuncio()
    #: ID do comentário-pai quando o evento é resposta dentro de uma thread.
    parent_id: str | None = None
    #: ID da conversa/thread, quando o evento é mensagem.
    thread_id: str | None = None
    #: True quando a própria conta já respondeu este comentário em público.
    #: Medido na coleta: 8 de 40 comentários amostrados já tinham resposta da
    #: casa. Sem este campo a Assistente sugere responder de novo quem já foi
    #: respondido, e o teto "uma resposta por comentário" cai no mundo real.
    resposta_publica_existente: bool = False
    #: Corpo cru guardado para auditoria/reprocessamento. Nunca contém segredo:
    #: token não entra em evento, e o ingestor redige antes de persistir.
    payload_bruto: dict[str, Any] | None = None

    @field_validator("criado_em", "recebido_em")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        return exigir_utc(v)

    @model_validator(mode="after")
    def _coerencia(self) -> "EventoNormalizado":
        if self.autor.plataforma is not self.plataforma:
            raise ContratoInvalido("autor de outra plataforma no mesmo evento")
        if self.autor.conta != self.conta:
            raise ContratoInvalido("autor escopado em outra conta")
        if self.tipo in (TipoEvento.COMENTARIO, TipoEvento.RESPOSTA_COMENTARIO):
            if not self.contexto.media_id:
                raise ContratoInvalido("comentário sem media_id não é rastreável")
        return self

    @property
    def e_eco(self) -> bool:
        return self.tipo in (TipoEvento.ECO_MENSAGEM, TipoEvento.ECO_COMENTARIO)


# ── Janelas ──────────────────────────────────────────────────────────────────


class ResultadoJanela(_Contrato):
    aberta: bool
    vence_em: datetime
    motivo: MotivoBloqueio | None = None

    @field_validator("vence_em")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        return exigir_utc(v, "vence_em")


def janela_private_reply(criado_em: datetime, agora: datetime) -> ResultadoJanela:
    """7 dias da criação do comentário. Fecha NO instante exato.

    Aprovação não renova: quem chama isto na hora de aprovar passa o `criado_em`
    do comentário original, nunca a data em que o rascunho entrou na fila.
    """
    criado_em = exigir_utc(criado_em, "criado_em")
    agora = exigir_utc(agora, "agora")
    vence = criado_em + JANELA_PRIVATE_REPLY
    aberta = agora < vence
    return ResultadoJanela(
        aberta=aberta,
        vence_em=vence,
        motivo=None if aberta else MotivoBloqueio.JANELA_PRIVATE_REPLY_VENCIDA,
    )


def janela_continuidade(
    ultima_mensagem_recebida_em: datetime | None, agora: datetime
) -> ResultadoJanela:
    """24h desde a última mensagem que a PESSOA mandou.

    `None` = a pessoa nunca respondeu: não há continuidade a ter. Mandar a
    private reply não abre janela nenhuma por si só.
    """
    agora = exigir_utc(agora, "agora")
    if ultima_mensagem_recebida_em is None:
        return ResultadoJanela(
            aberta=False,
            vence_em=agora,
            motivo=MotivoBloqueio.JANELA_CONTINUIDADE_VENCIDA,
        )
    base = exigir_utc(ultima_mensagem_recebida_em, "ultima_mensagem_recebida_em")
    vence = base + JANELA_CONTINUIDADE
    aberta = agora < vence
    return ResultadoJanela(
        aberta=aberta,
        vence_em=vence,
        motivo=None if aberta else MotivoBloqueio.JANELA_CONTINUIDADE_VENCIDA,
    )


# ── Tetos ────────────────────────────────────────────────────────────────────


class TetosAssistente(_Contrato):
    """Teto de produto. "Não fica enviando muita mensagem" virou número.

    Os dois primeiros só podem ser 1 (padrão) ou 0 (desligar). Os dois últimos
    só podem ser 0. Não existe configuração que habilite envio — mexer aqui
    nunca liga um transporte, porque transporte não existe na V1.
    """

    max_public_replies_per_comment: int = 1
    #: Por PESSOA, na operação inteira. Sem reinício por post, campanha ou dia.
    max_initial_dm_per_person: int = 1
    max_automatic_followups: int = 0
    system_send_limit: int = 0

    @model_validator(mode="after")
    def _travas(self) -> "TetosAssistente":
        if self.max_public_replies_per_comment not in (0, 1):
            raise TetoInvalido(
                "max_public_replies_per_comment só aceita 1 (padrão) ou 0 (desligar)"
            )
        if self.max_initial_dm_per_person not in (0, 1):
            raise TetoInvalido(
                "max_initial_dm_per_person só aceita 1 (padrão) ou 0 (desligar)"
            )
        if self.max_automatic_followups != 0:
            raise TetoInvalido("follow-up automático não existe na V1")
        if self.system_send_limit != 0:
            raise TetoInvalido("o sistema não envia; system_send_limit tem que ser 0")
        return self


TETOS_PADRAO = TetosAssistente()


class ConsumoTeto(_Contrato):
    """Quanto do teto já foi gasto — inclusive por gente, fora do sistema.

    `respostas_publicas_registradas` conta o que a casa já respondeu no app, de
    qualquer época, não só o que passou pela Assistente. Estado incerto
    (`estado_conhecido=False`) bloqueia nova abordagem até reconciliar: propor
    de novo "porque não temos registro" é como o mundo real ganha DM repetida.
    """

    identidade: IdentidadeSocial
    comentario_id: str | None = None
    respostas_publicas_registradas: int = 0
    dms_iniciais_registradas: int = 0
    estado_conhecido: bool = True

    def resposta_publica_disponivel(self, tetos: TetosAssistente = TETOS_PADRAO) -> bool:
        if not self.estado_conhecido:
            return False
        return self.respostas_publicas_registradas < tetos.max_public_replies_per_comment

    def dm_inicial_disponivel(self, tetos: TetosAssistente = TETOS_PADRAO) -> bool:
        if not self.estado_conhecido:
            return False
        return self.dms_iniciais_registradas < tetos.max_initial_dm_per_person


# ── Propostas ────────────────────────────────────────────────────────────────


class FatoCitado(_Contrato):
    """Rastro de cada afirmação comercial que entrou no rascunho.

    Sem isto não dá para responder "de onde saiu esse preço?" — e um rascunho
    que ninguém consegue auditar não pode ser aprovado.
    """

    fato_id: str = Field(min_length=1)
    fonte: str = Field(min_length=1)
    validado_em: str | None = None
    valido_ate: str | None = None


class PropostaBase(_Contrato):
    """Campos comuns a toda proposta. Uma proposta = uma ação.

    `versao` é a revisão otimista: quem aprova manda a versão que leu. Se outra
    pessoa editou no meio, a decisão é recusada em vez de sobrescrever.
    """

    proposta_id: str = Field(min_length=1)
    tenant_id: int = TENANT_ID
    acao: AcaoProposta
    estado: EstadoProposta
    versao: int = Field(default=1, ge=1)
    identidade: IdentidadeSocial
    evento_id: str = Field(min_length=1)
    #: Todos os eventos da mesma pessoa que esta proposta cobre. Quem comentou
    #: cinco vezes recebe UMA abordagem, não cinco.
    eventos_agrupados: tuple[str, ...] = ()
    classe: ClasseComentario | None = None
    contexto: ContextoAnuncio = ContextoAnuncio()
    texto_sugerido: str | None = None
    texto_revisado: str | None = None
    fatos: tuple[FatoCitado, ...] = ()
    #: Marcadores {{PENDENTE: ...}} que a revisora precisa resolver antes de
    #: aprovar. Lista vazia não significa "conferido": significa "sem lacuna".
    pendencias: tuple[str, ...] = ()
    bloqueios: tuple[MotivoBloqueio, ...] = ()
    vence_em: datetime | None = None
    criado_em: datetime
    atualizado_em: datetime
    revisora: str | None = None
    decidido_em: datetime | None = None

    @field_validator("criado_em", "atualizado_em")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        return exigir_utc(v)

    @field_validator("vence_em", "decidido_em")
    @classmethod
    def _utc_opt(cls, v: datetime | None) -> datetime | None:
        return None if v is None else exigir_utc(v)

    @model_validator(mode="after")
    def _coerencia(self) -> "PropostaBase":
        if self.estado is EstadoProposta.BLOQUEADO and not self.bloqueios:
            raise ContratoInvalido("proposta bloqueada sem motivo não é auditável")
        if self.estado is EstadoProposta.APROVADO:
            if self.bloqueios:
                raise ContratoInvalido(
                    "bloqueio não pode ser sobreposto por aprovação"
                )
            if not self.revisora:
                raise ContratoInvalido("aprovação sem revisora não é auditável")
            if self.pendencias:
                raise ContratoInvalido(
                    "rascunho com {{PENDENTE}} não pode ser aprovado"
                )
        return self

    @property
    def texto_vigente(self) -> str | None:
        return self.texto_revisado if self.texto_revisado is not None else self.texto_sugerido


class PropostaRespostaPublica(PropostaBase):
    acao: AcaoProposta = AcaoProposta.RESPOSTA_PUBLICA
    comentario_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def _texto(self) -> "PropostaRespostaPublica":
        if self.estado in (EstadoProposta.RASCUNHO, EstadoProposta.APROVADO):
            if not (self.texto_vigente or "").strip():
                raise ContratoInvalido("rascunho de resposta pública sem texto")
        return self


class PropostaDmInicial(PropostaBase):
    """DM inicial — uma por pessoa, na operação inteira.

    Só existe atrelada a um comentário elegível: é private reply, não DM avulsa.
    `comentario_id` é o destinatário da operação futura, não um username.
    """

    acao: AcaoProposta = AcaoProposta.DM_INICIAL
    comentario_id: str = Field(min_length=1)
    comentario_criado_em: datetime

    @field_validator("comentario_criado_em")
    @classmethod
    def _utc_c(cls, v: datetime) -> datetime:
        return exigir_utc(v, "comentario_criado_em")

    @model_validator(mode="after")
    def _classe_permite(self) -> "PropostaDmInicial":
        if self.classe is not None:
            grupo = GRUPO_DA_CLASSE[self.classe]
            if grupo in GRUPOS_SEM_DM and self.estado not in (
                EstadoProposta.BLOQUEADO,
                EstadoProposta.REJEITADO,
                EstadoProposta.EXPIRADO,
            ):
                raise ContratoInvalido(
                    f"classe {self.classe.value} ({grupo.value}) não gera DM comercial"
                )
            if self.classe is ClasseComentario.PEDIDO_DE_SAIDA and self.estado not in (
                EstadoProposta.BLOQUEADO,
                EstadoProposta.REJEITADO,
                EstadoProposta.EXPIRADO,
            ):
                raise ContratoInvalido("pedido de saída nunca vira DM")
        return self


class ConsentimentoCadastro(_Contrato):
    """Aceite explícito, ligado à finalidade e ao mesmo atendimento.

    "Sim" solto não serve: tem que ser possível apontar a mensagem em que a
    pessoa concordou com encaminhamento/simulação.
    """

    evidencia_id: str = Field(min_length=1)
    texto_evidencia: str = Field(min_length=1)
    finalidade: str = Field(min_length=1)
    aceito_em: datetime
    thread_id: str | None = None

    @field_validator("aceito_em")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        return exigir_utc(v, "aceito_em")


class PropostaCadastroLead(PropostaBase):
    """Proposta de cadastro. Aprovar grava a decisão — não escreve em `leads`.

    A gravação é a tarefa 9, liberada em separado pela Dona. Por isso não há
    `lead_id` de saída aqui, nem campo `fone8` (coluna gerada), nem nada que
    chame intake, roleta ou boas-vindas.
    """

    acao: AcaoProposta = AcaoProposta.CADASTRO_LEAD
    consentimento: ConsentimentoCadastro | None = None
    vinculo: VinculoTelefone | None = None
    owner_id: int | None = Field(default=None, description="corretora escolhida por humano")
    stage_id: int | None = Field(default=None, description="etapa Novo, validada no schema")
    origem: str = ORIGEM_LEAD
    origem_trafego: OrigemTrafego = OrigemTrafego.NAO_DETERMINADO
    #: Lead que já existe para este telefone. Duplicata NÃO recria lead: a
    #: proposta vira "anexar interação", preservando owner, etapa e origem.
    lead_existente_id: int | None = None
    anexar_a_lead_existente: bool = False

    @model_validator(mode="after")
    def _pronta_so_com_tudo(self) -> "PropostaCadastroLead":
        pronta = self.estado in (EstadoProposta.RASCUNHO, EstadoProposta.APROVADO)
        if pronta:
            if self.consentimento is None:
                raise ContratoInvalido("cadastro sem aceite explícito não fica pronto")
            if self.vinculo is None:
                raise ContratoInvalido("cadastro sem telefone vinculado com evidência")
            if self.owner_id is None and not self.anexar_a_lead_existente:
                raise ContratoInvalido(
                    "sem corretora escolhida por humano a proposta fica pendente"
                )
        if self.anexar_a_lead_existente and self.lead_existente_id is None:
            raise ContratoInvalido("anexar a lead existente exige lead_existente_id")
        if self.origem != ORIGEM_LEAD:
            raise ContratoInvalido(f"origem fora do contrato: {self.origem!r}")
        return self


Proposta = PropostaRespostaPublica | PropostaDmInicial | PropostaCadastroLead


# ── Transições, edição e decisão ─────────────────────────────────────────────


def validar_transicao(de: EstadoProposta, para: EstadoProposta) -> None:
    """Levanta `TransicaoInvalida` quando o caminho não existe na máquina."""
    if para not in TRANSICOES[de]:
        raise TransicaoInvalida(f"{de.value} → {para.value} não é permitido")


def exigir_versao(proposta: PropostaBase, versao_cliente: int) -> None:
    """Revisão otimista. A tela manda a versão que leu; se não bate, recusa.

    Sem isto, duas revisoras abrindo a mesma proposta gravam uma por cima da
    outra e a auditoria fica com um texto que ninguém aprovou.
    """
    if versao_cliente != proposta.versao:
        raise VersaoDesatualizada(
            f"proposta está na versão {proposta.versao}, decisão veio da {versao_cliente}"
        )


def rechecagem_obrigatoria(acao: AcaoProposta) -> tuple[str, ...]:
    """O que TEM que ser consultado de novo no instante da decisão.

    Não basta o botão estar desabilitado na tela: a tela pode estar aberta
    desde ontem. A checagem é no servidor, com o relógio do servidor.
    """
    comum = ("supressao", "conversa_humana", "teto")
    if acao is AcaoProposta.DM_INICIAL:
        return comum + ("janela_private_reply", "identidade_conciliada")
    if acao is AcaoProposta.CADASTRO_LEAD:
        return comum + ("consentimento", "duplicidade_telefone", "owner_ativo")
    return comum


# ── Execução manual (registro, não envio) ────────────────────────────────────


class RegistroExecucaoManual(_Contrato):
    """Prova de que uma PESSOA executou a ação, no app, com autorização.

    Consome teto. Não chama transporte nenhum — e não pode ser gerado pelo
    sistema: sem referência de autorização da Dona e sem evidência do canal,
    o registro não existe.
    """

    proposta_id: str = Field(min_length=1)
    acao: AcaoProposta
    autorizacao_dona_ref: str = Field(min_length=1)
    responsavel: str = Field(min_length=1)
    executado_em: datetime
    canal: str = Field(min_length=1, description="ex.: app Instagram, caixa de entrada")
    evidencia: str = Field(min_length=1, description="link ou print da ação feita")
    registrado_em: datetime

    @field_validator("executado_em", "registrado_em")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        return exigir_utc(v)

    @model_validator(mode="after")
    def _cadastro_nao_se_registra_aqui(self) -> "RegistroExecucaoManual":
        if self.acao is AcaoProposta.CADASTRO_LEAD:
            raise ContratoInvalido(
                "cadastro não tem execução manual na V1 — cadastro efetivo é a tarefa 9"
            )
        return self


# ── Auditoria ────────────────────────────────────────────────────────────────


class EventoAuditoria(_Contrato):
    """Uma linha por decisão. Guarda o texto que valia na hora, não o de agora.

    Sem `texto_no_momento` a auditoria vira ponteiro para a versão atual — e
    "o que foi aprovado" deixa de ser respondível depois da próxima edição.
    """

    proposta_id: str = Field(min_length=1)
    tenant_id: int = TENANT_ID
    acao: AcaoProposta
    de_estado: EstadoProposta | None
    para_estado: EstadoProposta
    versao: int = Field(ge=1)
    autor: str = Field(min_length=1)
    motivo: str | None = None
    texto_no_momento: str | None = None
    em: datetime

    @field_validator("em")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        return exigir_utc(v, "em")


# ── Paginação ────────────────────────────────────────────────────────────────


class Pagina(BaseModel):
    """Página por cursor opaco. Offset numa fila que muda embaixo do pé pula e
    repete item; cursor não."""

    model_config = ConfigDict(extra="forbid")

    itens: Sequence[Any]
    proximo_cursor: str | None = None
    total_aproximado: int | None = None


# ── Interfaces de repositório e política (tarefas 3, 5, 6, 7) ────────────────


class RepositorioEventos(Protocol):
    """Tarefa 3. Persistência idempotente de evento.

    `registrar` devolve `(evento, novo)`: `novo=False` quando o evento já
    existia. Reentrega da Meta não pode gerar duas propostas — e não pode
    confirmar entrega antes de persistir.
    """

    def registrar(self, evento: EventoNormalizado) -> tuple[EventoNormalizado, bool]: ...

    def por_id(
        self, tenant_id: int, plataforma: Plataforma, conta: str, evento_id: str
    ) -> EventoNormalizado | None: ...

    def listar_por_identidade(
        self, identidade: IdentidadeSocial, *, limite: int = 50, cursor: str | None = None
    ) -> Pagina: ...


class RepositorioIdentidades(Protocol):
    """Tarefa 3. Identidades e vínculos, sempre escopados."""

    def garantir(self, identidade: IdentidadeSocial) -> IdentidadeSocial: ...

    def vincular_telefone(self, vinculo: VinculoTelefone) -> None: ...

    def estado(self, identidade: IdentidadeSocial) -> EstadoIdentidade: ...

    def telefones_vinculados(self, identidade: IdentidadeSocial) -> tuple[str, ...]: ...


class RepositorioSupressao(Protocol):
    """Tarefas 3 e 5. Consulta e registro de opt-out.

    `consultar` nunca levanta por falha de infraestrutura: devolve
    `ConsultaSupressao(disponivel=False, bloqueado=True, ...)`. Quem quiser
    tratar a falha como erro chama `consultar_estrito`.
    """

    def consultar(
        self, identidade: IdentidadeSocial, telefone_e164: str | None = None
    ) -> ConsultaSupressao: ...

    def consultar_estrito(
        self, identidade: IdentidadeSocial, telefone_e164: str | None = None
    ) -> ConsultaSupressao: ...

    def registrar(self, supressao: Supressao) -> None: ...


class RepositorioPropostas(Protocol):
    """Tarefas 3, 6 e 7. Propostas com revisão otimista.

    `decidir` recebe a versão que a tela leu e levanta `VersaoDesatualizada`
    quando alguém editou no meio. `editar` sempre devolve versão+1 em
    `RASCUNHO`, invalidando aprovação anterior.
    """

    def criar(self, proposta: PropostaBase) -> PropostaBase: ...

    def por_id(self, tenant_id: int, proposta_id: str) -> PropostaBase | None: ...

    def fila(
        self,
        tenant_id: int,
        *,
        estados: Iterable[EstadoProposta] = (),
        acoes: Iterable[AcaoProposta] = (),
        limite: int = 50,
        cursor: str | None = None,
    ) -> Pagina: ...

    def editar(
        self, tenant_id: int, proposta_id: str, *, texto: str, versao: int, autor: str
    ) -> PropostaBase: ...

    def decidir(
        self,
        tenant_id: int,
        proposta_id: str,
        *,
        para: EstadoProposta,
        versao: int,
        autor: str,
        motivo: str | None = None,
    ) -> PropostaBase: ...

    def registrar_execucao_manual(self, registro: RegistroExecucaoManual) -> PropostaBase: ...

    def cancelar_pendentes_da_identidade(
        self, identidade: IdentidadeSocial, *, motivo: MotivoBloqueio, autor: str
    ) -> int: ...


class RepositorioAuditoria(Protocol):
    """Tarefa 3. Só append. Auditoria que se edita não é auditoria."""

    def registrar(self, evento: EventoAuditoria) -> None: ...

    def por_proposta(self, tenant_id: int, proposta_id: str) -> Sequence[EventoAuditoria]: ...


class PoliticaElegibilidade(Protocol):
    """Tarefa 5. Decide o que pode, com os bloqueios ganhando de tudo."""

    def avaliar_resposta_publica(
        self, evento: EventoNormalizado, consumo: ConsumoTeto, agora: datetime
    ) -> tuple[bool, tuple[MotivoBloqueio, ...]]: ...

    def avaliar_dm_inicial(
        self,
        evento: EventoNormalizado,
        consumo: ConsumoTeto,
        supressao: ConsultaSupressao,
        estado_identidade: EstadoIdentidade,
        agora: datetime,
    ) -> tuple[bool, tuple[MotivoBloqueio, ...]]: ...


# ── Contratos HTTP ───────────────────────────────────────────────────────────

#: Webhook (tarefa 4). Autenticação: HMAC-SHA256 do CORPO CRU em
#: `X-Hub-Signature-256`, validado com o app secret — mesmo padrão de
#: `app/api/meta_webhook.py`, inclusive fail-closed com segredo vazio. O GET faz
#: o handshake `hub.challenge`.
ROTA_WEBHOOK = "/webhooks/instagram-acme"

#: Endpoints internos (tarefa 7). Autenticação: header `X-Painel-User` posto
#: pelo nginx (Basic Auth), validado no backend por `_identificar_usuario` —
#: nunca confiar em tenant/autor vindos do navegador.
ROTA_PAINEL = "/painel/instagram-acme"

ENDPOINTS_PAINEL: dict[str, str] = {
    "fila": f"{ROTA_PAINEL}/fila",
    "detalhe": f"{ROTA_PAINEL}/propostas/{{proposta_id}}",
    "editar": f"{ROTA_PAINEL}/propostas/{{proposta_id}}/editar",
    "decidir": f"{ROTA_PAINEL}/propostas/{{proposta_id}}/decidir",
    "execucao_manual": f"{ROTA_PAINEL}/propostas/{{proposta_id}}/execucao-manual",
    "auditoria": f"{ROTA_PAINEL}/propostas/{{proposta_id}}/auditoria",
}

#: O que NÃO existe na V1. Está aqui escrito para o teste de contrato poder
#: afirmar a ausência, e para que criar qualquer um deles seja uma decisão
#: consciente com revisão da OS — não um `@router.post` a mais num arquivo.
ROTAS_PROIBIDAS_V1: tuple[str, ...] = (
    f"{ROTA_PAINEL}/enviar",
    f"{ROTA_PAINEL}/propostas/{{proposta_id}}/enviar",
    f"{ROTA_PAINEL}/cadastrar",
    f"{ROTA_PAINEL}/propostas/{{proposta_id}}/cadastrar",
    "/painel/instagram-acme/leads",
)

#: Marcas obrigatórias na tela (tarefa 7). Quem olha a fila precisa saber, sem
#: perguntar, que nada saiu daqui.
MARCA_RASCUNHO = "Rascunho — não enviado"
MARCA_CADASTRO = "Proposta — não cadastrada"


class FiltroFila(BaseModel):
    """Query da fila. `tenant_id` NÃO entra: vem do servidor."""

    model_config = ConfigDict(extra="forbid")

    estados: tuple[EstadoProposta, ...] = ()
    acoes: tuple[AcaoProposta, ...] = ()
    somente_bloqueadas: bool = False
    limite: int = Field(default=50, ge=1, le=200)
    cursor: str | None = None


class PedidoEdicao(BaseModel):
    model_config = ConfigDict(extra="forbid")

    texto: str = Field(min_length=1)
    versao: int = Field(ge=1)


class PedidoDecisao(BaseModel):
    """Decisão da revisora. Só aprovar/rejeitar — não há `enviar`."""

    model_config = ConfigDict(extra="forbid")

    decisao: EstadoProposta
    versao: int = Field(ge=1)
    motivo: str | None = None

    @model_validator(mode="after")
    def _so_decisoes_humanas(self) -> "PedidoDecisao":
        if self.decisao not in (EstadoProposta.APROVADO, EstadoProposta.REJEITADO):
            raise ContratoInvalido(
                "decisão só pode ser aprovado ou rejeitado; bloqueio e expiração "
                "são calculados pelo servidor"
            )
        if self.decisao is EstadoProposta.REJEITADO and not (self.motivo or "").strip():
            raise ContratoInvalido("rejeição sem motivo não é auditável")
        return self


class PedidoExecucaoManual(BaseModel):
    """Registro do que uma pessoa fez no app. Não aciona transporte."""

    model_config = ConfigDict(extra="forbid")

    autorizacao_dona_ref: str = Field(min_length=1)
    canal: str = Field(min_length=1)
    evidencia: str = Field(min_length=1)
    executado_em: datetime
    versao: int = Field(ge=1)


class ItemFila(BaseModel):
    """O que a tela recebe por proposta. Telefone vai MASCARADO na lista; o
    número completo só na tela de detalhe, para quem atende."""

    model_config = ConfigDict(extra="forbid")

    proposta_id: str
    acao: AcaoProposta
    estado: EstadoProposta
    versao: int
    classe: ClasseComentario | None = None
    username: str | None = None
    texto_original: str | None = None
    texto_sugerido: str | None = None
    pendencias: tuple[str, ...] = ()
    bloqueios: tuple[MotivoBloqueio, ...] = ()
    vence_em: datetime | None = None
    telefone_mascarado: str | None = None
    marca: str = MARCA_RASCUNHO
    permalink: str | None = None


def mascarar_telefone(telefone_e164: str | None) -> str | None:
    """`+5500000000000` → `+55 15 ****-2211`. Lista mostra isto; detalhe mostra
    o número, e quem abriu o detalhe fica na auditoria."""
    if not telefone_e164:
        return None
    digitos = "".join(c for c in telefone_e164 if c.isdigit())
    if len(digitos) < 6:
        return "****"
    return f"+{digitos[:2]} {digitos[2:4]} ****-{digitos[-4:]}"


# ── Guarda de saída ──────────────────────────────────────────────────────────


def recusar_envio(o_que: str) -> None:
    """Chamada obrigatória em qualquer ponto que um dia seja tentado como saída.

    Existe para falhar alto: é melhor um `EnvioNaoPermitido` no log do que uma
    DM na caixa de entrada de alguém que não pediu.
    """
    raise EnvioNaoPermitido(
        f"{o_que}: a Assistente V1 não envia mensagem nem cadastra lead. "
        "Execução é manual, por pessoa autorizada, registrada em "
        "RegistroExecucaoManual."
    )


__all__ = [
    "AcaoProposta",
    "BLOQUEIOS_SO_DE_DM",
    "CONTA_ANUNCIOS",
    "CONTA_INSTAGRAM",
    "Clock",
    "ClasseComentario",
    "ConsentimentoCadastro",
    "ConsultaSupressao",
    "ConsumoTeto",
    "ContextoAnuncio",
    "ContratoInvalido",
    "ENDPOINTS_PAINEL",
    "EnvioNaoPermitido",
    "ErroAssistente",
    "EstadoIdentidade",
    "EstadoProposta",
    "EventoAuditoria",
    "EventoNormalizado",
    "FatoCitado",
    "FONTES_QUE_VINCULAM",
    "FiltroFila",
    "FonteEvidenciaVinculo",
    "GRUPOS_SEM_DM",
    "GRUPO_DA_CLASSE",
    "GrupoClasse",
    "IdentidadeSocial",
    "ItemFila",
    "JANELA_CONTINUIDADE",
    "JANELA_PRIVATE_REPLY",
    "MARCA_CADASTRO",
    "MARCA_RASCUNHO",
    "MotivoBloqueio",
    "ORIGEM_LEAD",
    "OrigemTrafego",
    "PAGINA_FACEBOOK",
    "Pagina",
    "PedidoDecisao",
    "PedidoEdicao",
    "PedidoExecucaoManual",
    "Plataforma",
    "PoliticaElegibilidade",
    "Proposta",
    "PropostaBase",
    "PropostaCadastroLead",
    "PropostaDmInicial",
    "PropostaRespostaPublica",
    "ROTAS_PROIBIDAS_V1",
    "ROTA_PAINEL",
    "ROTA_WEBHOOK",
    "RegistroExecucaoManual",
    "RepositorioAuditoria",
    "RepositorioEventos",
    "RepositorioIdentidades",
    "RepositorioPropostas",
    "RepositorioSupressao",
    "ResultadoJanela",
    "Supressao",
    "SupressaoIndisponivel",
    "TENANT_ID",
    "TETOS_PADRAO",
    "TRANSICOES",
    "TetoInvalido",
    "TetosAssistente",
    "TipoEvento",
    "TransicaoInvalida",
    "VersaoDesatualizada",
    "VinculoTelefone",
    "exigir_utc",
    "exigir_versao",
    "janela_continuidade",
    "janela_private_reply",
    "mascarar_telefone",
    "rechecagem_obrigatoria",
    "recusar_envio",
    "utc_now",
    "validar_transicao",
]
