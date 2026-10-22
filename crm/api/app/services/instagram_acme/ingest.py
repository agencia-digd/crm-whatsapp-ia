"""Ingestão em SOMBRA dos eventos de Instagram da Acme — normaliza e grava.

Tarefa 4A da `projetos/acme/OS-ASSISTENTE-IA-TAREFA-4A.md` (porta de entrada
multi-conta). Este módulo transforma o payload que a Meta manda — e que hoje
chega **repassado** pelo `painel-corretor` — em `EventoNormalizado` do contrato da
tarefa 1, deduplicado por `evento_id`.

**Zero saída.** Não existe aqui nenhum cliente HTTP, nenhuma chamada à Graph,
nenhuma resposta a comentário e nenhuma DM. O teste
`tests/test_instagram_ingest.py` varre o arquivo atrás disso.

O que este módulo NÃO faz, e de quem é:

* ler a Graph para completar `resposta_publica_existente`, `ad_id` e o timestamp
  exato do comentário → `meta_reader.py`, tarefa 4;
* classificar, montar rascunho ou avaliar elegibilidade → tarefa 5;
* persistir em Postgres → tarefa 3 (`repository.py` + migration). Enquanto ela
  não existe, o repositório padrão é o de MEMÓRIA declarado no fim do arquivo:
  prova idempotência e some no restart. Quando a 3 entregar, basta chamar
  `definir_repositorio(...)` no boot — nenhum arquivo desta tarefa muda.

## O relógio do comentário — leitura do payload real (21/09/2026)

Os comentários gravados em `corretor.eventos` chegam assim, **sem timestamp**:

    {"field": "comments", "value": {"id": "...", "from": {...},
     "text": "...", "media": {"id": "...", "media_product_type": "REELS"}}}

Então `criado_em` cai para `entry[].time`, que é o instante em que a Meta montou
a entrega — próximo da criação, mas não igual a ela. Isso importa porque a
janela de private reply é contada da **criação do comentário** (7 dias), e quem
decidir a partir de um `criado_em` aproximado pode achar aberta uma janela já
fechada. Por isso cada evento guarda de onde veio o relógio
(`payload_bruto["criado_em_fonte"]`) e a tarefa 4 deve confirmar o timestamp
pela Graph antes de qualquer decisão de DM.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

import structlog

from app.services.instagram_acme.contracts import (
    CONTA_INSTAGRAM,
    TENANT_ID,
    ContextoAnuncio,
    ContratoInvalido,
    EventoNormalizado,
    IdentidadeSocial,
    OrigemTrafego,
    Plataforma,
    RepositorioEventos,
    TipoEvento,
    utc_now,
)

log = structlog.get_logger()

#: Campos de `changes[]` que viram comentário. O resto (mentions, story_insights,
#: o que a Meta adicionar amanhã) é ignorado com motivo, nunca "tratado mais ou
#: menos".
CAMPOS_COMENTARIO = ("comments", "live_comments")

#: Chaves que nunca podem sobreviver ao `payload_bruto`. Hoje a Meta não manda
#: segredo nenhum no corpo — a trava existe para o dia em que mandar.
CHAVES_REDIGIDAS = ("token", "secret", "password", "senha", "app_secret")


class MotivoIgnorado(str, Enum):
    """Por que um item do lote não virou evento. Sempre explícito: silêncio é
    como a Acme ficou 30 dias sem ninguém ver que o evento chegava e morria."""

    OBJETO_NAO_INSTAGRAM = "objeto_nao_instagram"
    CONTA_NAO_PERMITIDA = "conta_nao_permitida"
    CAMPO_NAO_TRATADO = "campo_nao_tratado"
    EVENTO_SEM_ID = "evento_sem_id"
    EVENTO_SEM_AUTOR = "evento_sem_autor"
    COMENTARIO_SEM_MEDIA = "comentario_sem_media"
    ITEM_FORA_DO_CONTRATO = "item_fora_do_contrato"


@dataclass(frozen=True)
class Ignorado:
    motivo: MotivoIgnorado
    conta: str | None = None
    detalhe: str | None = None


@dataclass(frozen=True)
class ResumoIngestao:
    """O que o lote produziu. `novos` é o que a tarefa 5 tem para trabalhar."""

    recebidos: int = 0
    novos: int = 0
    repetidos: int = 0
    eventos: tuple[EventoNormalizado, ...] = ()
    ignorados: tuple[Ignorado, ...] = ()

    def como_resposta(self) -> dict[str, Any]:
        """Corpo do 200 do webhook. Contagens, nunca texto de pessoa."""
        return {
            "ok": True,
            "modo": "sombra",
            "recebidos": self.recebidos,
            "novos": self.novos,
            "repetidos": self.repetidos,
            "ignorados": [
                {"motivo": i.motivo.value, "conta": i.conta} for i in self.ignorados
            ],
        }


# ── Normalização ─────────────────────────────────────────────────────────────


def _redigir(valor: Any) -> Any:
    if isinstance(valor, dict):
        return {
            k: ("***REDIGIDO***" if any(p in k.lower() for p in CHAVES_REDIGIDAS) else _redigir(v))
            for k, v in valor.items()
        }
    if isinstance(valor, list):
        return [_redigir(v) for v in valor]
    return valor


def _epoch(valor: Any) -> datetime | None:
    """A Meta manda epoch em segundos (entry.time) ou em milissegundos
    (messaging[].timestamp), sem avisar qual. 10^11 separa os dois até o ano
    5138 — e um erro aqui vira 1970 ou 55 mil, não um deslize de horas."""
    if isinstance(valor, bool) or not isinstance(valor, (int, float)):
        return None
    segundos = valor / 1000 if valor > 10**11 else float(valor)
    try:
        return datetime.fromtimestamp(segundos, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _iso(valor: Any) -> datetime | None:
    if not isinstance(valor, str) or not valor:
        return None
    try:
        d = datetime.fromisoformat(valor.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _contexto_do_comentario(value: dict[str, Any]) -> ContextoAnuncio:
    """`ad_id` ausente NÃO é prova de orgânico.

    100% das mídias com comentário da Acme são dark ads (interseção zero com
    as 153 do feed, medido na tarefa 0). Se a ausência de ad_id virasse
    `organico`, a operação inteira seria classificada errado.
    """
    media = value.get("media") if isinstance(value.get("media"), dict) else {}
    ad_id = value.get("ad_id") or (media or {}).get("ad_id")
    return ContextoAnuncio(
        media_id=(media or {}).get("id"),
        permalink=value.get("permalink") or (media or {}).get("permalink"),
        ad_id=str(ad_id) if ad_id else None,
        origem=OrigemTrafego.PAGO if ad_id else OrigemTrafego.NAO_DETERMINADO,
    )


def _normalizar_comentario(
    change: dict[str, Any], conta: str, entry_time: datetime | None, recebido_em: datetime
) -> tuple[EventoNormalizado | None, Ignorado | None]:
    value = change.get("value") or {}
    evento_id = value.get("id")
    if not evento_id:
        return None, Ignorado(MotivoIgnorado.EVENTO_SEM_ID, conta, change.get("field"))

    autor_id = (value.get("from") or {}).get("id")
    if not autor_id:
        return None, Ignorado(MotivoIgnorado.EVENTO_SEM_AUTOR, conta, str(evento_id))

    criado_em = _iso(value.get("timestamp")) or _epoch(value.get("timestamp"))
    fonte_relogio = "value.timestamp"
    if criado_em is None:
        criado_em, fonte_relogio = entry_time, "entry.time"
    if criado_em is None:
        criado_em, fonte_relogio = recebido_em, "recebido_em"

    # Comentário da própria conta (inclusive resposta já publicada à mão no app)
    # é ECO: não vira proposta e consome o teto daquele comentário.
    proprio = str(autor_id) == conta
    if proprio:
        tipo = TipoEvento.ECO_COMENTARIO
    elif value.get("parent_id"):
        tipo = TipoEvento.RESPOSTA_COMENTARIO
    else:
        tipo = TipoEvento.COMENTARIO

    try:
        evento = EventoNormalizado(
            tenant_id=TENANT_ID,
            plataforma=Plataforma.INSTAGRAM,
            conta=conta,
            evento_id=str(evento_id),
            tipo=tipo,
            autor=IdentidadeSocial(
                plataforma=Plataforma.INSTAGRAM,
                conta=conta,
                ator_id=str(autor_id),
                username=(value.get("from") or {}).get("username"),
            ),
            texto=value.get("text"),
            criado_em=criado_em,
            recebido_em=recebido_em,
            contexto=_contexto_do_comentario(value),
            parent_id=str(value["parent_id"]) if value.get("parent_id") else None,
            payload_bruto={
                "item": _redigir(change),
                "criado_em_fonte": fonte_relogio,
                "entry_time": entry_time.isoformat() if entry_time else None,
            },
        )
    except (ContratoInvalido, ValueError) as exc:
        motivo = (
            MotivoIgnorado.COMENTARIO_SEM_MEDIA
            if "media_id" in str(exc)
            else MotivoIgnorado.ITEM_FORA_DO_CONTRATO
        )
        return None, Ignorado(motivo, conta, str(evento_id))
    return evento, None


def _normalizar_mensagem(
    item: dict[str, Any], conta: str, entry_time: datetime | None, recebido_em: datetime
) -> tuple[EventoNormalizado | None, Ignorado | None]:
    mensagem = item.get("message")
    if not isinstance(mensagem, dict):
        # reaction, read, delivery, postback, referral… nada disso é mensagem
        # recebida. Entra como ignorado COM motivo, para aparecer na contagem.
        campo = next((k for k in item if k not in ("sender", "recipient", "timestamp")), None)
        return None, Ignorado(MotivoIgnorado.CAMPO_NAO_TRATADO, conta, campo)

    evento_id = mensagem.get("mid")
    if not evento_id:
        return None, Ignorado(MotivoIgnorado.EVENTO_SEM_ID, conta, "messaging")

    remetente = (item.get("sender") or {}).get("id")
    if not remetente:
        return None, Ignorado(MotivoIgnorado.EVENTO_SEM_AUTOR, conta, str(evento_id))

    eco = bool(mensagem.get("is_echo"))
    criado_em = _epoch(item.get("timestamp")) or entry_time or recebido_em
    destinatario = (item.get("recipient") or {}).get("id")
    outra_ponta = str(destinatario) if eco and destinatario else str(remetente)

    try:
        evento = EventoNormalizado(
            tenant_id=TENANT_ID,
            plataforma=Plataforma.INSTAGRAM,
            conta=conta,
            evento_id=str(evento_id),
            tipo=TipoEvento.ECO_MENSAGEM if eco else TipoEvento.MENSAGEM_RECEBIDA,
            autor=IdentidadeSocial(
                plataforma=Plataforma.INSTAGRAM, conta=conta, ator_id=str(remetente)
            ),
            texto=mensagem.get("text"),
            criado_em=criado_em,
            recebido_em=recebido_em,
            # A conversa é identificada pela OUTRA ponta, nunca pela conta: no
            # eco quem manda somos nós, então a pessoa é o `recipient`.
            thread_id=outra_ponta,
            payload_bruto={
                "item": _redigir(item),
                "criado_em_fonte": "messaging.timestamp" if item.get("timestamp") else "entry.time",
                "entry_time": entry_time.isoformat() if entry_time else None,
            },
        )
    except (ContratoInvalido, ValueError) as exc:
        return None, Ignorado(MotivoIgnorado.ITEM_FORA_DO_CONTRATO, conta, str(exc)[:120])
    return evento, None


def normalizar_lote(
    payload: dict[str, Any], *, recebido_em: datetime | None = None
) -> tuple[list[EventoNormalizado], list[Ignorado]]:
    """Payload da Meta (repassado ou direto) → eventos da conta permitida.

    Duas portas fechadas, nesta ordem:

    1. `object` diferente de `instagram` não atravessa o fluxo. O objeto `page`
       (Facebook) é IDENTIFICADO e devolvido como ignorado — são 158 comentários
       já mapeados, com tratamento humano próprio, e a API é outra.
    2. `entry[].id` diferente de `CONTA_INSTAGRAM` é ignorado. O repasse do
       `painel-corretor` é por conta, mas a conta permitida é resolvida **aqui, no
       servidor**: quem chama não decide de quem é o evento.
    """
    agora = recebido_em or utc_now()
    eventos: list[EventoNormalizado] = []
    ignorados: list[Ignorado] = []

    objeto = str(payload.get("object") or "")
    if objeto and objeto != "instagram":
        return [], [Ignorado(MotivoIgnorado.OBJETO_NAO_INSTAGRAM, None, objeto)]

    for entry in payload.get("entry", []) or []:
        if not isinstance(entry, dict):
            ignorados.append(Ignorado(MotivoIgnorado.ITEM_FORA_DO_CONTRATO, None, "entry"))
            continue
        conta = str(entry.get("id") or "")
        if conta != CONTA_INSTAGRAM:
            ignorados.append(Ignorado(MotivoIgnorado.CONTA_NAO_PERMITIDA, conta or None))
            continue

        entry_time = _epoch(entry.get("time"))

        for change in entry.get("changes", []) or []:
            if not isinstance(change, dict):
                ignorados.append(Ignorado(MotivoIgnorado.ITEM_FORA_DO_CONTRATO, conta, "change"))
                continue
            if change.get("field") not in CAMPOS_COMENTARIO:
                ignorados.append(
                    Ignorado(MotivoIgnorado.CAMPO_NAO_TRATADO, conta, str(change.get("field")))
                )
                continue
            evento, ignorado = _normalizar_comentario(change, conta, entry_time, agora)
            if evento is not None:
                eventos.append(evento)
            else:
                ignorados.append(ignorado)

        for item in entry.get("messaging", []) or []:
            if not isinstance(item, dict):
                ignorados.append(Ignorado(MotivoIgnorado.ITEM_FORA_DO_CONTRATO, conta, "messaging"))
                continue
            evento, ignorado = _normalizar_mensagem(item, conta, entry_time, agora)
            if evento is not None:
                eventos.append(evento)
            else:
                ignorados.append(ignorado)

    return eventos, ignorados


# ── Repositório ──────────────────────────────────────────────────────────────


@dataclass
class RepositorioEventosMemoria:
    """Implementação PROVISÓRIA de `RepositorioEventos`, até a tarefa 3.

    Existe para a porta de entrada poder ser ligada e testada sem inventar
    schema que pertence a outro executor. Guarda por
    `(tenant, plataforma, conta, evento_id)` — a mesma chave que a tabela da
    tarefa 3 vai usar — e some no restart. Não serve para produção: um restart
    faz a reentrega da Meta parecer evento novo.
    """

    _por_chave: dict[tuple[int, str, str, str], EventoNormalizado] = field(default_factory=dict)

    def _chave(self, evento: EventoNormalizado) -> tuple[int, str, str, str]:
        return (evento.tenant_id, evento.plataforma.value, evento.conta, evento.evento_id)

    def registrar(self, evento: EventoNormalizado) -> tuple[EventoNormalizado, bool]:
        chave = self._chave(evento)
        existente = self._por_chave.get(chave)
        if existente is not None:
            return existente, False
        self._por_chave[chave] = evento
        return evento, True

    def por_id(
        self, tenant_id: int, plataforma: Plataforma, conta: str, evento_id: str
    ) -> EventoNormalizado | None:
        return self._por_chave.get((tenant_id, plataforma.value, conta, evento_id))

    def listar_por_identidade(self, identidade, *, limite: int = 50, cursor: str | None = None):
        from app.services.instagram_acme.contracts import Pagina

        itens = [e for e in self._por_chave.values() if e.autor.chave == identidade.chave]
        return Pagina(itens=itens[:limite], proximo_cursor=None, total_aproximado=len(itens))

    def limpar(self) -> None:
        self._por_chave.clear()


_repositorio: RepositorioEventos = RepositorioEventosMemoria()


def definir_repositorio(repositorio: RepositorioEventos) -> None:
    """Ponto de troca para a tarefa 3 ligar o repositório de Postgres no boot."""
    global _repositorio
    _repositorio = repositorio


def repositorio_atual() -> RepositorioEventos:
    return _repositorio


# ── Entrada pública ──────────────────────────────────────────────────────────


def processar_lote(
    payload: dict[str, Any],
    *,
    recebido_em: datetime | None = None,
    repositorio: RepositorioEventos | None = None,
) -> ResumoIngestao:
    """Normaliza e grava. **Persistir vem antes de confirmar** — quem chama só
    devolve 200 depois que esta função voltou, então evento perdido não vira
    entrega confirmada."""
    repo = repositorio or repositorio_atual()
    eventos, ignorados = normalizar_lote(payload, recebido_em=recebido_em)

    gravados: list[EventoNormalizado] = []
    novos = 0
    for evento in eventos:
        persistido, e_novo = repo.registrar(evento)
        gravados.append(persistido)
        novos += 1 if e_novo else 0

    resumo = ResumoIngestao(
        recebidos=len(eventos),
        novos=novos,
        repetidos=len(eventos) - novos,
        eventos=tuple(gravados),
        ignorados=tuple(ignorados),
    )
    # Contagens e motivos. Nunca texto de pessoa, nunca IGSID, nunca segredo.
    log.info(
        "assistente_ingest_lote",
        recebidos=resumo.recebidos,
        novos=resumo.novos,
        repetidos=resumo.repetidos,
        ignorados=[i.motivo.value for i in resumo.ignorados],
    )
    return resumo
