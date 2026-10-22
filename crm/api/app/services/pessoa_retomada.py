"""Fila de retomada da Pessoa (linha da Corretor) — turno sem LLM não morre.

Caso de 05/09/2026: Claude (janela de 5h) e codex (cota até dia 7) caíram
JUNTOS. A Pessoa respondeu "Tive um probleminha técnico" 6x e nunca voltou —
um lead disse "Quero", outra pediu "apartamento 2 quartos acima de 70m²", e
ninguém respondeu. Isto é a última linha de defesa, depois do retry no agente
e do fallback de provider em app.config:

    atender() → _rodar_pessoa() → provider_falhou → enfileirar()
                                                        │
    worker (thread no copiloto-api) ou timer ──► tick() ┘
        │ humano assumiu / handoff → cancelada
        │ provider ainda fora     → adia (backoff) … MAX_TENTATIVAS → desistiu + avisa Dona
        └ respondeu               → _responder() pela linha da Corretor → feita

Travas:
- `enfileirar` NUNCA levanta (é chamado de dentro do except do atendimento).
- `_responder` respeita DRY_RUN (settings.dry_run) e o bloqueio sob pytest.
- Aviso à Dona vai pelo caminho que já existe (app.tools.notify → bridge
  Telegram), com trava DRY_RUN/pytest aqui.
- 1 pendente por telefone (índice parcial): mensagens seguidas se juntam.
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog
from pydantic import BaseModel
from sqlalchemy import select

from app.config import settings
from app.db.models import PessoaRetomada
from app.db.session import SessionLocal

log = structlog.get_logger()

STATUS_PENDENTE = "pendente"
STATUS_FEITA = "feita"
STATUS_CANCELADA = "cancelada"
STATUS_DESISTIU = "desistiu"

MAX_TENTATIVAS = 8
# minutos até a próxima tentativa, indexado por `tentativas` já feitas
BACKOFF_MIN: tuple[int, ...] = (3, 5, 10, 15, 30, 60, 60, 60)


class TickResultado(BaseModel):
    considerados: int = 0
    respondidas: int = 0
    adiadas: int = 0
    canceladas: int = 0
    desistidas: int = 0
    falhas: int = 0


def _agora(agora: datetime | None) -> datetime:
    return agora or datetime.now(timezone.utc)


def _backoff(tentativas: int) -> timedelta:
    idx = max(0, min(tentativas, len(BACKOFF_MIN) - 1))
    return timedelta(minutes=BACKOFF_MIN[idx])


# ── entrada ──────────────────────────────────────────────────────────────────

def enfileirar(telefone: str, texto: str, nome: str | None, erro: str | None,
               agora: datetime | None = None) -> int | None:
    """Guarda o turno do lead pra retomada. Best-effort: nunca levanta.

    Se já há um pendente pro telefone, a mensagem nova é anexada (linha nova)
    — o lead que escreveu 3x enquanto o LLM estava fora recebe UMA resposta
    que considera as 3, não 3 respostas desencontradas.
    """
    agora = _agora(agora)
    texto = (texto or "").strip()
    if not texto:
        return None
    try:
        with SessionLocal() as s:
            row = s.execute(
                select(PessoaRetomada)
                .where(PessoaRetomada.telefone == telefone)
                .where(PessoaRetomada.status == STATUS_PENDENTE)
                .with_for_update()
            ).scalar_one_or_none()
            nova = row is None
            if row is None:
                row = PessoaRetomada(
                    telefone=telefone, nome=nome, texto=texto, erro=erro,
                    proxima_em=agora + _backoff(0), atualizado_em=agora,
                )
                s.add(row)
            else:
                if texto not in row.texto.splitlines():
                    row.texto = f"{row.texto}\n{texto}"
                row.erro = erro
                row.nome = row.nome or nome
                row.atualizado_em = agora
            s.commit()
            rid = row.id
        log.error(
            "pessoa_retomada_enfileirada",
            id=rid, tel=telefone[-4:], nova=nova, erro=(erro or "")[:120],
        )
        if nova:
            _alertar_dona(telefone, erro, rid, agora)
        return rid
    except Exception as e:  # noqa: BLE001 — a fila é rede, não pode furar
        log.exception("pessoa_retomada_enfileirar_falhou", tel=telefone[-4:], erro=str(e)[:160])
        return None


def _alertar_dona(telefone: str, erro: str | None, rid: int | None,
                  agora: datetime, desistiu: bool = False) -> bool:
    """Aviso pelo caminho que já existe (bridge Telegram da Assistente).

    Trava: sob pytest ou DRY_RUN só loga — aviso é canal externo como qualquer
    outro (CLAUDE.md regra #9).
    """
    suf = telefone[-4:]
    dia = agora.strftime("%Y%m%d")
    if desistiu:
        texto = (
            f"🚨 Pessoa (linha Corretor) DESISTIU do lead final {suf} depois de "
            f"{MAX_TENTATIVAS} tentativas — LLM segue fora. Responder na mão. "
            f"Fila #{rid}. Último erro: {(erro or '?')[:160]}"
        )
        task_id = f"copiloto-pessoa-desistiu-{suf}-{dia}"
    else:
        texto = (
            f"⚠️ Pessoa (linha Corretor) sem LLM: lead final {suf} ficou sem resposta. "
            f"Turno na fila de retomada #{rid}; tento de novo com backoff. "
            f"Erro: {(erro or '?')[:160]}"
        )
        task_id = f"copiloto-pessoa-retomada-{suf}-{dia}"
    if os.environ.get("PYTEST_CURRENT_TEST") or settings.dry_run:
        log.info("[DRY_RUN] alerta_dona_pessoa_retomada", task_id=task_id, preview=texto[:120])
        return True
    try:
        from app.tools.notify import notify_dona

        return notify_dona(task_id=task_id, text=texto,
                           descricao="Pessoa sem LLM — fila de retomada (Copiloto Acme)")
    except Exception as e:  # noqa: BLE001
        log.warning("pessoa_retomada_alerta_falhou", erro=str(e)[:120])
        return False


# ── saída ────────────────────────────────────────────────────────────────────

def _historico_sem_o_turno(historico: list[dict[str, Any]], texto: str) -> list[dict[str, Any]]:
    """Tira do fim do histórico as mensagens do lead que JÁ são este turno —
    elas foram gravadas na primeira tentativa, e o agente recebe o texto de
    novo como `message`. Sem isso o modelo lê a mesma fala duas vezes."""
    from app.agents.pessoa.agent import FRASE_PROBLEMINHA

    linhas = {ln.strip() for ln in texto.splitlines()}
    saida: list[dict[str, Any]] = []
    na_cauda = True
    for h in reversed(historico):
        t = (h.get("text") or "").strip()
        if na_cauda:
            if h.get("role") == "assistant" and t == FRASE_PROBLEMINHA:
                saida.append(h)      # fica: a Pessoa "pediu um instante", agora volta
                continue
            if h.get("role") == "user" and t in linhas:
                continue             # é o turno que vai como `message`
            na_cauda = False
        saida.append(h)
    saida.reverse()
    return saida


def _pendentes_vencidos(agora: datetime, limite: int) -> list[int]:
    with SessionLocal() as s:
        ids = s.execute(
            select(PessoaRetomada.id)
            .where(PessoaRetomada.status == STATUS_PENDENTE)
            .where(PessoaRetomada.proxima_em <= agora)
            .order_by(PessoaRetomada.proxima_em)
            .limit(limite)
        ).scalars().all()
    return list(ids)


def _reservar(rid: int, agora: datetime) -> PessoaRetomada | None:
    """Marca a tentativa ANTES de rodar (crash no meio não vira loop infinito)
    e devolve uma cópia desanexada da linha."""
    with SessionLocal() as s:
        row = s.execute(
            select(PessoaRetomada).where(PessoaRetomada.id == rid).with_for_update(skip_locked=True)
        ).scalar_one_or_none()
        if row is None or row.status != STATUS_PENDENTE or row.proxima_em > agora:
            return None
        row.tentativas += 1
        row.proxima_em = agora + _backoff(row.tentativas)
        row.atualizado_em = agora
        s.commit()
        s.refresh(row)
        s.expunge(row)
        return row


def _fechar(rid: int, status: str, agora: datetime, resultado: str | None = None,
            erro: str | None = None) -> None:
    with SessionLocal() as s:
        row = s.get(PessoaRetomada, rid)
        if row is None:
            return
        row.status = status
        row.atualizado_em = agora
        if resultado is not None:
            row.resultado = resultado[:500]
        if erro is not None:
            row.erro = erro[:300]
        s.commit()


def _adiar(rid: int, agora: datetime, erro: str | None) -> None:
    with SessionLocal() as s:
        row = s.get(PessoaRetomada, rid)
        if row is None:
            return
        row.erro = (erro or "")[:300] or row.erro
        row.atualizado_em = agora
        s.commit()


def _retomar_um(row: PessoaRetomada, agora: datetime, res: TickResultado) -> None:
    from app.services import atendimento_corretor as ab
    from app.services.conversations import append_message, load_history

    tel, texto = row.telefone, row.texto
    # Mesmas travas do inbound: humano assumiu ou já passou pra Pessoa12 → a Pessoa
    # cala e o item fecha. Responder por cima da Pessoa12 é pior que não responder.
    if ab._handoff_feito(tel) or ab._humano_falando(tel, desde=ab._inicio_da_conversa(tel)):
        _fechar(row.id, STATUS_CANCELADA, agora, erro="humano/handoff assumiu")
        res.canceladas += 1
        log.info("pessoa_retomada_cancelada", id=row.id, tel=tel[-4:])
        return

    historico = _historico_sem_o_turno(
        load_history(tel, max_messages=20, max_age_hours=48), texto
    )
    out = ab._executar_pessoa(tel, texto, row.nome, historico)

    if out["provider_falhou"]:
        if row.tentativas >= MAX_TENTATIVAS:
            _fechar(row.id, STATUS_DESISTIU, agora, erro=out["erro"])
            _alertar_dona(tel, out["erro"], row.id, agora, desistiu=True)
            res.desistidas += 1
            log.error("pessoa_retomada_desistiu", id=row.id, tel=tel[-4:], tentativas=row.tentativas)
        else:
            _adiar(row.id, agora, out["erro"])
            res.adiadas += 1
            log.warning("pessoa_retomada_adiada", id=row.id, tel=tel[-4:],
                        tentativas=row.tentativas, proxima_em=row.proxima_em.isoformat())
        return

    resposta = out["resposta"]
    if not resposta:
        _fechar(row.id, STATUS_FEITA, agora, resultado="(pessoa sem resposta)")
        res.respondidas += 1
        return

    ab._responder(tel, resposta)
    append_message(tel, "pessoa", None, "assistant", resposta)
    _fechar(row.id, STATUS_FEITA, agora, resultado=resposta)
    res.respondidas += 1
    log.info("pessoa_retomada_respondida", id=row.id, tel=tel[-4:], tentativas=row.tentativas)
    try:
        from app.services.aviso_resposta_disparo import avisar_em_background

        avisar_em_background(tel)
    except Exception as e:  # noqa: BLE001 — aviso não trava a retomada
        log.warning("aviso_resposta_pos_retomada_falhou", erro=str(e)[:120])


def tick(agora: datetime | None = None, limite: int = 20) -> TickResultado:
    """Uma varredura: retoma os pendentes vencidos. Idempotente e segura pra
    rodar em paralelo (worker + timer): `_reservar` usa SKIP LOCKED e só pega
    quem ainda está pendente e vencido."""
    agora = _agora(agora)
    res = TickResultado()
    for rid in _pendentes_vencidos(agora, limite):
        row = _reservar(rid, agora)
        if row is None:
            continue
        res.considerados += 1
        try:
            _retomar_um(row, agora, res)
        except Exception as e:  # noqa: BLE001 — um item ruim não trava a fila
            res.falhas += 1
            log.exception("pessoa_retomada_item_falhou", id=rid, erro=str(e)[:160])
            _adiar(rid, agora, f"{type(e).__name__}: {e}")
    if res.considerados:
        log.info("pessoa_retomada_tick", **res.model_dump())
    return res


# ── worker em thread (dentro do copiloto-api) ────────────────────────────────

_worker_iniciado = threading.Event()


def _loop(intervalo_s: int, parar: threading.Event) -> None:
    while not parar.wait(intervalo_s):
        try:
            tick()
        except Exception as e:  # noqa: BLE001 — o loop não morre
            log.exception("pessoa_retomada_worker_erro", erro=str(e)[:160])
        try:
            from app.services import aviso_resposta_disparo as _aviso

            _aviso.avisar_pendentes()
        except Exception as e:  # noqa: BLE001 — o aviso não mata a fila
            log.warning("aviso_resposta_tick_falhou", erro=str(e)[:160])


def iniciar_worker(intervalo_s: int | None = None) -> threading.Event | None:
    """Sobe o worker em thread daemon. Devolve o Event que o para, ou None se
    não subiu (desligado por config, sob pytest, ou já rodando)."""
    if os.environ.get("COPILOTO_UNDER_PYTEST") or not settings.pessoa_retomada_worker:
        return None
    if _worker_iniciado.is_set():
        return None
    _worker_iniciado.set()
    parar = threading.Event()
    threading.Thread(
        target=_loop,
        args=(intervalo_s or settings.pessoa_retomada_intervalo_s, parar),
        daemon=True,
        name="pessoa-retomada",
    ).start()
    log.info("pessoa_retomada_worker_iniciado", intervalo_s=intervalo_s or settings.pessoa_retomada_intervalo_s)
    return parar
