"""Envio assistido da Assistente — tarefa 10 da `OS-ASSISTENTE-IA.md`, liberada em
parte pela `OS-ASSISTENTE-IA-TAREFA-9E10.md` (Dona, 21/09/2026 09:29 UTC).

**Nasce desligado.** `armado()` devolve `False` neste momento e continua
devolvendo `False` até três coisas acontecerem, nesta ordem, por decisão humana:
a chave no banco, a ausência do arquivo de pânico e um repositório de reservas
durável. Nenhuma delas é efeito de deploy. Ver §"Ligar e desligar" abaixo.

## O que esta autorização cobre — e o que não cobre

A autorização de 21/09 é estreita, e o código é estreito do mesmo jeito:

| Ação | Estado | Por quê |
|---|---|---|
| `CONTINUIDADE` — responder no direct quem escreveu, dentro de 24h | **liberada** | é conversa que a pessoa começou |
| `DM_INICIAL` — private reply de comentário | **travada** (`ACOES_LIBERADAS`) | abrir conversa com quem não escreveu no direct; a OS 9E10 não libera |
| `RESPOSTA_PUBLICA` — responder o comentário em público | **travada** | idem; as 20 de 21/09 foram feitas à mão |

As duas travadas têm código e teste — o que falta é autorização, não
implementação. Destravar é mexer em `ACOES_LIBERADAS`, com a OS na mão.

## O teto continua valendo

`TetosAssistente.system_send_limit = 0` não virou 1. O que a OS 9E10 fez foi abrir
**uma exceção nomeada**: resposta dentro da janela de 24h a quem escreveu
primeiro. Por isso a checagem de teto aqui é por ação, e `CONTINUIDADE` não
consome `max_initial_dm_per_person` — continuidade não é abordagem nova e não
reabre o teto de DM inicial (CONTRATOS §5). Uma pessoa, uma conversa.

## Reserva antes da rede

Nada vai para a rede sem uma reserva gravada antes. A chave é
`(proposta_id, versao, ação)`: duplo clique encontra a reserva e para; texto
editado muda a versão e vira outra chave, mas a revalidação recusa se a
proposta não voltou a ser aprovada.

**Timeout não autoriza repetir.** Resultado incerto vira `INCERTO` e fica lá:
não existe retentativa automática, não existe cron, não existe consumo de fila.
Reconciliar é ato humano — quem repete uma mensagem que talvez tenha saído é
quem manda a segunda mensagem para alguém que pediu uma.

## Ligar e desligar

* **Ligar** (ato da Dona, uma linha de SQL, sem restart):

      INSERT INTO config_operacional (chave, valor, atualizado_em, atualizado_por)
      VALUES ('assistente_envio_ativo', 'true'::jsonb, NOW(), '<quem>')
      ON CONFLICT (chave) DO UPDATE SET valor = 'true'::jsonb,
          atualizado_em = NOW(), atualizado_por = EXCLUDED.atualizado_por;

* **Botão de pânico** (um comando, efeito imediato, não derruba nada mais):

      sudo touch /etc/agencia/assistente-envio.off

  O arquivo vence a chave do banco, é lido a cada envio (sem cache) e não
  depende do Postgres estar de pé. Nenhum outro motor olha para ele: cadência,
  carrossel, nutrição, Assistente e WhatsApp da Corretor seguem como estavam.

* **Desligar pelo banco** (equivalente, precisa do banco vivo):

      UPDATE config_operacional SET valor = 'false'::jsonb,
             atualizado_em = NOW(), atualizado_por = '<quem>'
       WHERE chave = 'assistente_envio_ativo';

Este módulo **não** é registrado em `app/main.py`; o router da tarefa 10
(`app/api/instagram_acme_envio.py`) também não. Enquanto a integração não
acontecer, nem a rota existe.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

import structlog

from app.services.instagram_acme.contracts import (
    PAGINA_FACEBOOK,
    TETOS_PADRAO,
    AcaoProposta,
    ConsultaSupressao,
    ConsumoTeto,
    EnvioNaoPermitido,
    ErroAssistente,
    EstadoIdentidade,
    EstadoProposta,
    IdentidadeSocial,
    MotivoBloqueio,
    PropostaBase,
    TetosAssistente,
    exigir_utc,
    exigir_versao,
    janela_continuidade,
    janela_private_reply,
)

log = structlog.get_logger()


# ── Armamento ────────────────────────────────────────────────────────────────

#: Chave no `config_operacional`. NÃO está em `config_operacional.DEFAULTS`
#: de propósito: aquele mapa é o contrato do painel, e esta chave não tem botão
#: de painel — é ato manual com a OS na mão. Ausente = desligado.
CHAVE_ENVIO_ATIVO = "assistente_envio_ativo"

#: Botão de pânico. Existindo, nenhum envio sai — mesmo com a chave em `true`,
#: mesmo com o banco fora do ar.
ARQUIVO_PANICO = "/etc/agencia/assistente-envio.off"


class AcaoEnvio(str, Enum):
    """Ações de saída. `CONTINUIDADE` não existe em `AcaoProposta` porque não é
    abordagem: é resposta a quem escreveu."""

    RESPOSTA_PUBLICA = "resposta_publica"
    DM_INICIAL = "dm_inicial"
    CONTINUIDADE = "continuidade"


#: O que a autorização de 21/09/2026 cobre. Mexer aqui é decisão de produto com
#: OS, não ajuste de configuração — por isso é constante de código, não chave de
#: banco: a Dona não desbloqueia uma ação nova sem alguém reler a OS.
ACOES_LIBERADAS: frozenset[AcaoEnvio] = frozenset({AcaoEnvio.CONTINUIDADE})

_ACAO_DA_PROPOSTA: dict[AcaoProposta, AcaoEnvio] = {
    AcaoProposta.RESPOSTA_PUBLICA: AcaoEnvio.RESPOSTA_PUBLICA,
    AcaoProposta.DM_INICIAL: AcaoEnvio.DM_INICIAL,
}


class MotivoEnvio(str, Enum):
    """Motivos de recusa próprios do envio."""

    ENVIO_DESARMADO = "envio_desarmado"
    ACAO_NAO_LIBERADA = "acao_nao_liberada"
    PROPOSTA_NAO_APROVADA = "proposta_nao_aprovada"
    AUTORIZACAO_AUSENTE = "autorizacao_ausente"
    TEXTO_VAZIO = "texto_vazio"
    TEXTO_COM_PENDENCIA = "texto_com_pendencia"
    RESERVA_JA_EXISTE = "reserva_ja_existe"
    RESULTADO_INCERTO_ANTERIOR = "resultado_incerto_anterior"
    TRANSPORTE_AUSENTE = "transporte_ausente"
    REPOSITORIO_VOLATIL = "repositorio_volatil"
    ACAO_INCOMPATIVEL_COM_A_PROPOSTA = "acao_incompativel_com_a_proposta"


class EnvioBloqueado(ErroAssistente):
    """Revalidação recusou o envio. Nada foi para a rede."""

    def __init__(self, motivos: Any) -> None:
        self.motivos: tuple[str, ...] = tuple(
            m.value if isinstance(m, Enum) else str(m) for m in motivos
        )
        super().__init__("envio bloqueado: " + ", ".join(self.motivos))


def _arquivo_de_panico_existe(caminho: str | None = None) -> bool:
    try:
        return Path(caminho or ARQUIVO_PANICO).exists()
    except OSError:
        # Não consegui nem olhar o arquivo: fail-closed. Diz que o botão está
        # apertado, que é o lado seguro do erro.
        return True


def _chave_ativa_no_banco() -> bool:
    """Lê `config_operacional` DIRETO, sem cache. O envio é ato humano avulso —
    uma query a mais não pesa, e cache de 30s num botão de pânico é 30s de
    mensagem saindo depois de alguém mandar parar."""
    try:
        from sqlalchemy import text as sql_text

        from app.db.session import SessionLocal

        with SessionLocal() as s:
            valor = s.execute(
                sql_text("SELECT valor FROM config_operacional WHERE chave = :k"),
                {"k": CHAVE_ENVIO_ATIVO},
            ).scalar()
    except Exception as exc:  # noqa: BLE001
        log.warning("assistente_envio_config_indisponivel", erro=str(exc)[:140])
        return False
    if valor is None:
        return False
    if isinstance(valor, str):
        try:
            valor = json.loads(valor)
        except ValueError:
            return False
    return valor is True


def armado(*, arquivo_panico: str | None = None) -> bool:
    """`True` só quando TODAS as travas estão abertas ao mesmo tempo.

    A terceira trava é técnica, não política: enquanto o repositório de reservas
    for o de memória (que some no restart), a deduplicação não sobrevive a um
    `systemctl restart` — e reserva que some é reserva que deixa mandar duas
    vezes. A tarefa 3 é quem entrega o durável.
    """
    if _arquivo_de_panico_existe(arquivo_panico):
        return False
    if not _repositorio_e_duravel():
        return False
    return _chave_ativa_no_banco()


# ── Reserva ──────────────────────────────────────────────────────────────────


class EstadoReserva(str, Enum):
    EM_VOO = "em_voo"
    ENVIADO = "enviado"
    #: Meta recusou de forma definitiva (permissão, restrição, destinatário
    #: inválido). Não se tenta de novo sem alguém olhar.
    FALHOU_PERMANENTE = "falhou_permanente"
    #: Rate limit e afins: a Meta não processou. Libera nova tentativa MANUAL.
    FALHOU_TRANSITORIO = "falhou_transitorio"
    #: Timeout, conexão cortada, resposta ilegível. Pode ter saído. Nunca repete.
    INCERTO = "incerto"


@dataclass
class Reserva:
    chave: str
    estado: EstadoReserva
    tentativas: int = 1
    id_externo: str | None = None
    erro: str | None = None
    criado_em: datetime | None = None
    atualizado_em: datetime | None = None


class RepositorioEnvios(Protocol):
    """Contrato do armazém de reservas. A implementação durável é da tarefa 3."""

    def reservar(self, chave: str, *, agora: datetime) -> tuple[Reserva, bool]: ...

    def concluir(self, chave: str, *, id_externo: str | None, agora: datetime) -> None: ...

    def falhar(self, chave: str, *, erro: str, permanente: bool, agora: datetime) -> None: ...

    def marcar_incerto(self, chave: str, *, erro: str, agora: datetime) -> None: ...

    def por_chave(self, chave: str) -> Reserva | None: ...


@dataclass
class RegistroEnviosMemoria:
    """Implementação PROVISÓRIA, no molde do `RepositorioEventosMemoria` da
    tarefa 4A. Serve para testar; **some no restart**, e por isso `armado()`
    devolve False enquanto ela for a instalada."""

    _por_chave: dict[str, Reserva] = field(default_factory=dict)

    def reservar(self, chave: str, *, agora: datetime) -> tuple[Reserva, bool]:
        existente = self._por_chave.get(chave)
        if existente is not None:
            return existente, False
        reserva = Reserva(
            chave=chave, estado=EstadoReserva.EM_VOO, criado_em=agora, atualizado_em=agora
        )
        self._por_chave[chave] = reserva
        return reserva, True

    def concluir(self, chave: str, *, id_externo: str | None, agora: datetime) -> None:
        r = self._por_chave[chave]
        r.estado = EstadoReserva.ENVIADO
        r.id_externo = id_externo
        r.atualizado_em = agora

    def falhar(self, chave: str, *, erro: str, permanente: bool, agora: datetime) -> None:
        r = self._por_chave[chave]
        r.estado = (
            EstadoReserva.FALHOU_PERMANENTE if permanente else EstadoReserva.FALHOU_TRANSITORIO
        )
        r.erro = erro[:200]
        r.atualizado_em = agora

    def marcar_incerto(self, chave: str, *, erro: str, agora: datetime) -> None:
        r = self._por_chave[chave]
        r.estado = EstadoReserva.INCERTO
        r.erro = erro[:200]
        r.atualizado_em = agora

    def por_chave(self, chave: str) -> Reserva | None:
        return self._por_chave.get(chave)

    def limpar(self) -> None:
        self._por_chave.clear()


_repositorio: RepositorioEnvios = RegistroEnviosMemoria()
_repositorio_duravel = False


def definir_repositorio(repositorio: RepositorioEnvios, *, duravel: bool = False) -> None:
    """Instala o armazém de reservas. `duravel=True` é afirmação de quem instala:
    "isto sobrevive a restart". Sem ela, `armado()` nunca abre."""
    global _repositorio, _repositorio_duravel
    _repositorio = repositorio
    _repositorio_duravel = bool(duravel)


def repositorio_atual() -> RepositorioEnvios:
    return _repositorio


def _repositorio_e_duravel() -> bool:
    return _repositorio_duravel


def chave_de_reserva(proposta_id: str, versao: int, acao: AcaoEnvio) -> str:
    """Chave da reserva: proposta + versão + ação.

    A versão entra porque editar o texto invalida a aprovação (CONTRATOS §7.3).
    A ação entra porque resposta pública e DM são decisões separadas.
    """
    return f"{proposta_id}:v{versao}:{acao.value}"


# ── Transporte ───────────────────────────────────────────────────────────────


class ClasseFalha(str, Enum):
    PERMANENTE = "permanente"
    TRANSITORIA = "transitoria"
    INCERTA = "incerta"


@dataclass(frozen=True)
class RespostaTransporte:
    ok: bool
    id_externo: str | None = None
    erro: str | None = None
    classe: ClasseFalha | None = None


@dataclass(frozen=True)
class DestinoTransporte:
    """Para quem. Private reply vai pelo **ID do comentário**, nunca por
    username: username muda, e mandar para o @ errado é mandar para outra
    pessoa."""

    comentario_id: str | None = None
    destinatario_id: str | None = None


class Transporte(Protocol):
    def enviar(
        self, *, acao: AcaoEnvio, destino: DestinoTransporte, texto: str
    ) -> RespostaTransporte: ...


class TransporteBloqueado:
    """Transporte padrão: recusa alto. Enquanto ninguém instalar um transporte
    de verdade, chamar `enviar` é um erro no log, não uma DM na caixa de
    alguém."""

    def enviar(
        self, *, acao: AcaoEnvio, destino: DestinoTransporte, texto: str
    ) -> RespostaTransporte:
        raise EnvioNaoPermitido(
            f"transporte não instalado para {acao.value}: a Assistente não tem saída ligada"
        )


#: Códigos de erro da Graph que NÃO adianta repetir. 10/200/803: permissão,
#: escopo, objeto inacessível. 551/2018001: destinatário fora de alcance.
#: Qualquer coisa fora desta lista e fora do rate limit vira permanente também:
#: a dúvida, aqui, é para o lado de não insistir.
CODIGOS_PERMANENTES = frozenset({10, 100, 200, 230, 551, 803, 2018001})
#: Rate limit / uso excessivo. A Meta não processou — recuar e esperar, sem
#: criar sequência comercial.
CODIGOS_RATE_LIMIT = frozenset({4, 17, 32, 613})


@dataclass
class TransporteGraph:
    """Transporte real (Graph API, private reply e resposta em conversa).

    Escopos conferidos em 21/09/2026 no token da página
    (`ACESSOS-E-COBERTURA.md`, itens 5 e 6): `instagram_manage_comments`,
    `instagram_manage_messages`, `pages_messaging`.

    **Nunca é o transporte padrão.** Quem instala é o operador, no ato, e só
    com `armado()` verdadeiro. O token é lido no momento do uso e não entra em
    log, em exceção ou em retorno.
    """

    versao_graph: str = "v21.0"
    pagina: str = PAGINA_FACEBOOK
    timeout_s: float = 15.0

    def _token(self) -> str:
        from app.config import settings

        return (settings.intake_meta_page_token or "").strip()

    def enviar(
        self, *, acao: AcaoEnvio, destino: DestinoTransporte, texto: str
    ) -> RespostaTransporte:
        import httpx

        token = self._token()
        if not token:
            return RespostaTransporte(
                ok=False, erro="page_token_ausente", classe=ClasseFalha.PERMANENTE
            )

        if acao is AcaoEnvio.CONTINUIDADE:
            if not destino.destinatario_id:
                return RespostaTransporte(
                    ok=False, erro="destinatario_ausente", classe=ClasseFalha.PERMANENTE
                )
            recipient: dict[str, str] = {"id": destino.destinatario_id}
        elif acao is AcaoEnvio.DM_INICIAL:
            if not destino.comentario_id:
                return RespostaTransporte(
                    ok=False, erro="comentario_ausente", classe=ClasseFalha.PERMANENTE
                )
            recipient = {"comment_id": destino.comentario_id}
        else:
            # Resposta pública não é `/messages` — é `POST /{comment_id}/replies`.
            # Está travada em ACOES_LIBERADAS; implementar o caminho antes de a
            # ação existir seria código que nunca roda.
            return RespostaTransporte(
                ok=False, erro="acao_sem_transporte", classe=ClasseFalha.PERMANENTE
            )

        url = f"https://graph.facebook.com/{self.versao_graph}/{self.pagina}/messages"
        try:
            resposta = httpx.post(
                url,
                params={"access_token": token},
                json={"recipient": recipient, "message": {"text": texto}},
                timeout=self.timeout_s,
            )
        except Exception as exc:  # noqa: BLE001 — timeout, DNS, conexão cortada
            # NÃO sabemos se a Meta processou. Incerto, e incerto não repete.
            return RespostaTransporte(
                ok=False, erro=type(exc).__name__, classe=ClasseFalha.INCERTA
            )

        if resposta.status_code == 200:
            corpo = {}
            try:
                corpo = resposta.json()
            except ValueError:
                # 200 com corpo ilegível: quase certamente saiu, mas sem ID não
                # dá para reconciliar. Incerto.
                return RespostaTransporte(
                    ok=False, erro="resposta_ilegivel", classe=ClasseFalha.INCERTA
                )
            return RespostaTransporte(ok=True, id_externo=str(corpo.get("message_id") or ""))

        codigo, mensagem = _erro_da_graph(resposta)
        if codigo in CODIGOS_RATE_LIMIT or resposta.status_code == 429:
            classe = ClasseFalha.TRANSITORIA
        elif resposta.status_code >= 500:
            # 5xx: a Meta pode ter processado antes de errar. Incerto.
            classe = ClasseFalha.INCERTA
        else:
            classe = ClasseFalha.PERMANENTE
        return RespostaTransporte(ok=False, erro=f"{codigo}:{mensagem}"[:200], classe=classe)


def _erro_da_graph(resposta: Any) -> tuple[int, str]:
    """`(código, mensagem)` sem vazar token: a Graph devolve a URL chamada em
    alguns erros, e a URL leva o `access_token`."""
    try:
        erro = (resposta.json() or {}).get("error", {})
    except Exception:  # noqa: BLE001
        return (resposta.status_code, "corpo_ilegivel")
    codigo = int(erro.get("code") or resposta.status_code)
    mensagem = str(erro.get("message") or "")[:120]
    if "access_token" in mensagem:
        mensagem = "mensagem_redigida"
    return codigo, mensagem


# ── Pedido de envio ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AutorizacaoEnvio:
    """Ato humano, vinculado à proposta e à versão. Sem os três campos não há
    envio — e não existe caminho que gere isto sozinho."""

    autorizacao_dona_ref: str
    operador: str
    versao: int


@dataclass(frozen=True)
class AlvoEnvio:
    acao: AcaoEnvio
    identidade: IdentidadeSocial
    texto: str
    comentario_id: str | None = None
    comentario_criado_em: datetime | None = None
    thread_id: str | None = None
    #: Última mensagem que a PESSOA mandou. É o relógio da janela de 24h.
    ultima_mensagem_recebida_em: datetime | None = None


@dataclass(frozen=True)
class EstadoNoEnvio:
    """O que tem que ser consultado de novo AGORA. Nada disto vem da tela: a
    tela pode estar aberta desde ontem (CONTRATOS §6)."""

    supressao: ConsultaSupressao
    consumo: ConsumoTeto
    estado_identidade: EstadoIdentidade = EstadoIdentidade.NAO_CONCILIADA
    conversa_humana_em_andamento: bool = False


@dataclass(frozen=True)
class ResultadoEnvio:
    enviado: bool
    chave: str
    estado: EstadoReserva
    id_externo: str | None = None
    erro: str | None = None


def _agora() -> datetime:
    return datetime.now(timezone.utc)


def _revalidar(
    proposta: PropostaBase,
    alvo: AlvoEnvio,
    estado: EstadoNoEnvio,
    autorizacao: AutorizacaoEnvio,
    agora: datetime,
    tetos: TetosAssistente,
) -> list[str]:
    motivos: list[str] = []

    if alvo.acao not in ACOES_LIBERADAS:
        motivos.append(MotivoEnvio.ACAO_NAO_LIBERADA.value)

    # A ação pedida tem que ser a ação que a revisora aprovou. Sem isto, uma
    # proposta de resposta pública aprovada viraria DM sem ninguém decidir —
    # que é exatamente o que o contrato evita separando proposta por ação.
    #
    # LACUNA DE CONTRATO: `AcaoProposta` não tem `continuidade`. A proposta de
    # continuidade chega hoje marcada como `dm_inicial`, que é o canal certo e
    # a semântica errada. Pendência do dono da tarefa 1 — ver RELATORIO-9E10.
    esperada = {
        AcaoEnvio.RESPOSTA_PUBLICA: {AcaoProposta.RESPOSTA_PUBLICA},
        AcaoEnvio.DM_INICIAL: {AcaoProposta.DM_INICIAL},
        AcaoEnvio.CONTINUIDADE: {AcaoProposta.DM_INICIAL},
    }[alvo.acao]
    if proposta.acao not in esperada:
        motivos.append(MotivoEnvio.ACAO_INCOMPATIVEL_COM_A_PROPOSTA.value)

    if proposta.identidade.chave != alvo.identidade.chave:
        # Identidade da proposta ≠ identidade do alvo: mandaria para outra
        # pessoa. Não é borda teórica — é o bug que um `proposta_id` trocado na
        # URL produz.
        motivos.append(MotivoBloqueio.IDENTIDADE_NAO_CONCILIADA.value)

    if not autorizacao.autorizacao_dona_ref.strip() or not autorizacao.operador.strip():
        motivos.append(MotivoEnvio.AUTORIZACAO_AUSENTE.value)

    if proposta.estado is not EstadoProposta.APROVADO:
        motivos.append(MotivoEnvio.PROPOSTA_NAO_APROVADA.value)
    if proposta.bloqueios:
        motivos.extend(b.value for b in proposta.bloqueios)
    if proposta.pendencias:
        motivos.append(MotivoBloqueio.FATO_PENDENTE_NAO_RESOLVIDO.value)

    texto = (alvo.texto or "").strip()
    if not texto:
        motivos.append(MotivoEnvio.TEXTO_VAZIO.value)
    elif "{{PENDENTE" in texto:
        # `{{PENDENTE: ...}}` é recado para a revisora, não texto publicável
        # (ATENDIMENTO.md §4). Sair assim é a Acme mandando o próprio to-do.
        motivos.append(MotivoEnvio.TEXTO_COM_PENDENCIA.value)

    # Opt-out em qualquer vínculo. Indisponível bloqueia, por contrato.
    if estado.supressao.bloqueado or not estado.supressao.disponivel:
        motivos.extend(m.value for m in estado.supressao.motivos)

    if estado.conversa_humana_em_andamento:
        motivos.append(MotivoBloqueio.CONVERSA_HUMANA_EM_ANDAMENTO.value)

    if not estado.consumo.estado_conhecido:
        motivos.append(MotivoBloqueio.ESTADO_DE_ENVIO_DESCONHECIDO.value)

    if alvo.acao is AcaoEnvio.CONTINUIDADE:
        janela = janela_continuidade(alvo.ultima_mensagem_recebida_em, agora)
        if not janela.aberta:
            motivos.append(MotivoBloqueio.JANELA_CONTINUIDADE_VENCIDA.value)
        # Continuidade NÃO consome `max_initial_dm_per_person` (CONTRATOS §5):
        # é resposta, não abordagem. O que a protege de virar sequência é o
        # teto de follow-up automático — que é zero e não tem caminho aqui.
    elif alvo.acao is AcaoEnvio.DM_INICIAL:
        janela = janela_private_reply(alvo.comentario_criado_em or agora, agora)
        if not janela.aberta:
            motivos.append(MotivoBloqueio.JANELA_PRIVATE_REPLY_VENCIDA.value)
        if estado.estado_identidade is EstadoIdentidade.BLOQUEADA:
            motivos.append(MotivoBloqueio.OPT_OUT_SOCIAL.value)
        if not estado.consumo.dm_inicial_disponivel(tetos):
            motivos.append(MotivoBloqueio.TETO_DM_CONSUMIDO.value)
        if not alvo.comentario_id:
            motivos.append(MotivoEnvio.TEXTO_VAZIO.value)
    else:  # RESPOSTA_PUBLICA
        if not estado.consumo.resposta_publica_disponivel(tetos):
            motivos.append(MotivoBloqueio.TETO_RESPOSTA_PUBLICA_CONSUMIDO.value)

    vistos: list[str] = []
    for m in motivos:
        if m not in vistos:
            vistos.append(m)
    return vistos


def enviar(
    proposta: PropostaBase,
    alvo: AlvoEnvio,
    estado: EstadoNoEnvio,
    *,
    autorizacao: AutorizacaoEnvio,
    transporte: Transporte | None = None,
    repositorio: RepositorioEnvios | None = None,
    agora: datetime | None = None,
    tetos: TetosAssistente = TETOS_PADRAO,
    arquivo_panico: str | None = None,
) -> ResultadoEnvio:
    """Envia UMA mensagem, autorizada, para UMA pessoa. Ação separada da
    aprovação: aprovar não passa por aqui, e este módulo não é chamado por
    nenhum cron, fila ou `on_approve`.

    Levanta `EnvioBloqueado` sem tocar na rede quando qualquer revalidação
    falha, e `EnvioNaoPermitido` quando o envio está desarmado.
    """
    agora = exigir_utc(agora or _agora(), "agora")
    repo = repositorio or repositorio_atual()
    transporte = transporte or TransporteBloqueado()

    if not armado(arquivo_panico=arquivo_panico):
        log.warning(
            "assistente_envio_desarmado",
            proposta_id=proposta.proposta_id,
            acao=alvo.acao.value,
        )
        raise EnvioNaoPermitido(
            "envio da Assistente está desarmado "
            f"(chave {CHAVE_ENVIO_ATIVO}, {ARQUIVO_PANICO}, repositório durável)"
        )

    # Revisão otimista ANTES de qualquer coisa: se alguém editou, a decisão que
    # o operador leu não é mais a que está no banco.
    exigir_versao(proposta, autorizacao.versao)

    motivos = _revalidar(proposta, alvo, estado, autorizacao, agora, tetos)
    if motivos:
        log.warning(
            "assistente_envio_bloqueado",
            proposta_id=proposta.proposta_id,
            acao=alvo.acao.value,
            motivos=motivos,
        )
        raise EnvioBloqueado(motivos)

    chave = chave_de_reserva(proposta.proposta_id, autorizacao.versao, alvo.acao)

    # ── RESERVA ANTES DA REDE ────────────────────────────────────────────────
    reserva, nova = repo.reservar(chave, agora=agora)
    if not nova:
        if reserva.estado is EstadoReserva.FALHOU_TRANSITORIO:
            # Rate limit anterior: a Meta não processou. Nova tentativa MANUAL
            # é legítima — não é sequência comercial, é a mesma mensagem.
            reserva.estado = EstadoReserva.EM_VOO
            reserva.tentativas += 1
        else:
            motivo = (
                MotivoEnvio.RESULTADO_INCERTO_ANTERIOR
                if reserva.estado is EstadoReserva.INCERTO
                else MotivoEnvio.RESERVA_JA_EXISTE
            )
            log.warning(
                "assistente_envio_reserva_existente",
                proposta_id=proposta.proposta_id,
                estado=reserva.estado.value,
            )
            raise EnvioBloqueado((motivo,))

    # Cada ação leva SÓ o endereço que ela usa. Continuidade não carrega
    # comentário e private reply não carrega IGSID: campo a mais no destino é
    # campo que um dia alguém usa por engano.
    if alvo.acao is AcaoEnvio.CONTINUIDADE:
        destino = DestinoTransporte(destinatario_id=alvo.identidade.ator_id)
    else:
        destino = DestinoTransporte(comentario_id=alvo.comentario_id)

    try:
        resposta = transporte.enviar(acao=alvo.acao, destino=destino, texto=alvo.texto)
    except EnvioNaoPermitido:
        repo.falhar(chave, erro="transporte_bloqueado", permanente=True, agora=agora)
        raise
    except Exception as exc:  # noqa: BLE001
        # Estourou dentro do transporte: não dá para afirmar que não saiu.
        repo.marcar_incerto(chave, erro=type(exc).__name__, agora=agora)
        log.exception("assistente_envio_excecao_no_transporte", proposta_id=proposta.proposta_id)
        return ResultadoEnvio(
            enviado=False, chave=chave, estado=EstadoReserva.INCERTO, erro=type(exc).__name__
        )

    if resposta.ok:
        repo.concluir(chave, id_externo=resposta.id_externo, agora=agora)
        log.info(
            "assistente_envio_ok",
            proposta_id=proposta.proposta_id,
            acao=alvo.acao.value,
            id_externo=resposta.id_externo,
        )
        return ResultadoEnvio(
            enviado=True,
            chave=chave,
            estado=EstadoReserva.ENVIADO,
            id_externo=resposta.id_externo,
        )

    if resposta.classe is ClasseFalha.INCERTA:
        repo.marcar_incerto(chave, erro=resposta.erro or "incerto", agora=agora)
        estado_final = EstadoReserva.INCERTO
    else:
        permanente = resposta.classe is not ClasseFalha.TRANSITORIA
        repo.falhar(
            chave, erro=resposta.erro or "falhou", permanente=permanente, agora=agora
        )
        estado_final = (
            EstadoReserva.FALHOU_PERMANENTE if permanente else EstadoReserva.FALHOU_TRANSITORIO
        )

    log.warning(
        "assistente_envio_falhou",
        proposta_id=proposta.proposta_id,
        acao=alvo.acao.value,
        estado=estado_final.value,
        erro=(resposta.erro or "")[:120],
    )
    return ResultadoEnvio(
        enviado=False, chave=chave, estado=estado_final, erro=resposta.erro
    )


def acao_de_proposta(proposta: PropostaBase) -> AcaoEnvio | None:
    """Traduz a ação da proposta (contrato) para a ação de envio. `None` para
    `cadastro_lead`: cadastro não é envio, e é a tarefa 9."""
    return _ACAO_DA_PROPOSTA.get(proposta.acao)


__all__ = [
    "ACOES_LIBERADAS",
    "ARQUIVO_PANICO",
    "AcaoEnvio",
    "AlvoEnvio",
    "AutorizacaoEnvio",
    "CHAVE_ENVIO_ATIVO",
    "ClasseFalha",
    "DestinoTransporte",
    "EnvioBloqueado",
    "EstadoNoEnvio",
    "EstadoReserva",
    "MotivoEnvio",
    "RegistroEnviosMemoria",
    "Reserva",
    "RespostaTransporte",
    "ResultadoEnvio",
    "Transporte",
    "TransporteBloqueado",
    "TransporteGraph",
    "acao_de_proposta",
    "armado",
    "chave_de_reserva",
    "definir_repositorio",
    "enviar",
    "repositorio_atual",
]
