"""Motor de jobs da busca de imóvel na web (assíncrona).

Por que existe: o imovel-fetcher no Mac mini abre Chromium de verdade e raspa
VivaReal + Chaves na Mão. Isso leva dezenas de segundos. O endpoint antigo era
síncrono e a corretora ficava parada na tela. Aqui a busca vira um job:

    POST  → cria linha `busca_imovel_jobs` (status=rodando) + dispara worker em
            thread daemon → devolve {job_id, status} NA HORA
    GET   → lê a linha; enquanto rodando devolve parciais (o que já achou),
            quando termina devolve as opções finais

Garantias:
  - **Não trava rodando pra sempre.** O worker tem timeout duro
    (`_TIMEOUT_WORKER_S`) e bate `heartbeat_em` enquanto trabalha. Se o processo
    do copiloto morrer no meio, a LEITURA considera job com heartbeat velho
    (> `_HEARTBEAT_MORTO_S`) como erro e o marca — não fica pendurado.
  - **Não duplica trabalho.** Índice único parcial (usuario, fingerprint) WHERE
    status='rodando' + checagem prévia: mesma corretora pedindo a mesma coisa
    reaproveita o job em vez de abrir outra sessão de browser no Mac.
  - **Mac fora do ar = erro humano.** Vpn caído vira mensagem em
    português, não stacktrace.

REGRA DE OURO herdada: isto é SUGESTÃO pra corretora. NUNCA envia nada pro lead.
"""
from __future__ import annotations

import hashlib
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import structlog
from sqlalchemy import select, text as sa_text

from app.config import settings
from app.db.models import BuscaImovelJob
from app.db.session import SessionLocal

log = structlog.get_logger()

# Playwright no Mac é lento (Chromium + 2 portais). Timeout da chamada HTTP.
TIMEOUT_FETCHER_S = 90.0
# Teto duro do job inteiro (chamada + normalização). Acima disso vira erro.
TIMEOUT_JOB_S = 120.0
# Job "rodando" cujo heartbeat parou há mais que isso = worker morto.
HEARTBEAT_MORTO_S = 180
# Job de menos de 10 min é reaproveitável no dedupe de "já rodando".
# Retenção: jobs com mais de 7 dias são apagados (limpeza oportunista).
RETENCAO_DIAS = 7

_CAMPOS = ("titulo", "preco", "bairro", "cidade", "m2", "quartos", "portal", "url", "resumo")

STATUS_RODANDO = "rodando"
STATUS_PRONTO = "pronto"
STATUS_ERRO = "erro"


# ── critérios ────────────────────────────────────────────────────────────────


def fingerprint(texto: str) -> str:
    """Assinatura estável dos critérios — base do dedupe.

    Normaliza caixa, acentuação de espaço e pontuação boba pra que
    "Apto 3 QUARTOS  Bairro A!" e "apto 3 quartos bairro" sejam o mesmo job.
    """
    norm = re.sub(r"[^0-9a-zà-ú]+", " ", (texto or "").strip().lower())
    norm = re.sub(r"\s+", " ", norm).strip()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def normalizar_opcoes(itens: list) -> list[dict[str, Any]]:
    """Garante o shape esperado pelo front; descarta item sem URL http real."""
    out: list[dict[str, Any]] = []
    for it in itens:
        if not isinstance(it, dict):
            continue
        url = str(it.get("url") or "").strip()
        if not url.startswith("http"):
            continue  # sem link real não serve — link é o ponto
        card: dict[str, Any] = {}
        for k in _CAMPOS:
            v = it.get(k)
            card[k] = str(v).strip() if v not in (None, "", "null") else None
        if not card.get("resumo"):
            partes = []
            if card.get("m2"):
                partes.append(f"{card['m2']} m²")
            if card.get("quartos"):
                partes.append(f"{card['quartos']} quartos")
            card["resumo"] = " · ".join(partes) or None
        card["url"] = url
        out.append(card)
    return out[:12]


# ── chamada ao Mac ───────────────────────────────────────────────────────────


def chamar_fetcher(texto: str) -> tuple[list[dict[str, Any]], str | None]:
    """Chama o imovel-fetcher no Mac (Vpn). Retorna (opcoes, erro)."""
    url = f"{settings.imovel_fetcher_url.rstrip('/')}/buscar"
    start = time.monotonic()
    try:
        with httpx.Client(timeout=TIMEOUT_FETCHER_S) as cli:
            resp = cli.get(url, params={"texto": texto})
    except httpx.TimeoutException:
        log.warning("busca_imovel_timeout", timeout_s=TIMEOUT_FETCHER_S)
        return [], "a busca demorou demais — tenta de novo em instantes"
    except httpx.HTTPError as e:  # Mac off / Vpn caiu / porta fechada
        log.warning("busca_imovel_mac_off", erro=str(e)[:160])
        return [], "não consegui buscar agora (motor de busca no Mac indisponível) — tenta de novo"

    duration_ms = int((time.monotonic() - start) * 1000)
    if resp.status_code != 200:
        log.warning("busca_imovel_status", status=resp.status_code, body=resp.text[:200])
        return [], "não consegui buscar agora, tenta de novo"
    try:
        data = resp.json()
    except ValueError:
        log.warning("busca_imovel_json_invalido", preview=resp.text[:200])
        return [], "não entendi o resultado da busca — tenta de novo"

    opcoes = normalizar_opcoes(data.get("opcoes") or [])
    log.info(
        "busca_imovel_call",
        duration_ms=duration_ms,
        filtros=data.get("filtros"),
        portais=data.get("portais"),
        qtd=len(opcoes),
    )
    return opcoes, None


# ── ciclo do job ─────────────────────────────────────────────────────────────


def _agora() -> datetime:
    return datetime.now(timezone.utc)


def criar_job(*, usuario: str, corretora_id: int | None, texto: str) -> tuple[int, bool]:
    """Cria (ou reaproveita) o job. Retorna (job_id, reaproveitado)."""
    fp = fingerprint(texto)
    with SessionLocal() as s:
        existente = s.execute(
            select(BuscaImovelJob)
            .where(
                BuscaImovelJob.usuario == usuario,
                BuscaImovelJob.fingerprint == fp,
                BuscaImovelJob.status == STATUS_RODANDO,
            )
            .order_by(BuscaImovelJob.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        if existente is not None and not _esta_morto(existente):
            log.info("busca_imovel_job_dedupe", job_id=existente.id, usuario=usuario)
            return existente.id, True

        job = BuscaImovelJob(
            usuario=usuario,
            corretora_id=corretora_id,
            texto=texto[:2000],
            fingerprint=fp,
            status=STATUS_RODANDO,
        )
        s.add(job)
        s.commit()
        job_id = job.id
    log.info("busca_imovel_job_criado", job_id=job_id, usuario=usuario)
    return job_id, False


def _esta_morto(job: BuscaImovelJob) -> bool:
    """Job rodando cujo worker parou de dar sinal de vida."""
    if job.status != STATUS_RODANDO:
        return False
    hb = job.heartbeat_em or job.criado_em
    if hb is None:
        return True
    if hb.tzinfo is None:
        hb = hb.replace(tzinfo=timezone.utc)
    return (_agora() - hb).total_seconds() > HEARTBEAT_MORTO_S


def _finalizar(job_id: int, *, status: str, resultado: dict | None, erro: str | None) -> None:
    with SessionLocal() as s:
        job = s.get(BuscaImovelJob, job_id)
        if job is None or job.status != STATUS_RODANDO:
            return  # já finalizado por outro caminho (ex: varredura de morto)
        job.status = status
        job.resultado = resultado
        job.erro = erro
        job.concluido_em = _agora()
        job.heartbeat_em = _agora()
        s.commit()


def _bater_heartbeat(job_id: int, parar: threading.Event) -> None:
    """Bate heartbeat a cada 20s enquanto o worker trabalha."""
    while not parar.wait(20):
        try:
            with SessionLocal() as s:
                s.execute(
                    sa_text(
                        "UPDATE busca_imovel_jobs SET heartbeat_em = now() "
                        "WHERE id = :i AND status = 'rodando'"
                    ),
                    {"i": job_id},
                )
                s.commit()
        except Exception as e:  # noqa: BLE001
            log.warning("busca_imovel_heartbeat_falha", job_id=job_id, erro=str(e)[:160])
            return


def executar_job(job_id: int) -> None:
    """Roda a busca e grava o desfecho. Nunca levanta — job sempre termina."""
    t0 = time.monotonic()
    parar = threading.Event()
    hb = threading.Thread(
        target=_bater_heartbeat, args=(job_id, parar), daemon=True, name=f"bij-hb-{job_id}"
    )
    hb.start()
    try:
        with SessionLocal() as s:
            job = s.get(BuscaImovelJob, job_id)
            texto = job.texto if job else None
        if not texto:
            _finalizar(job_id, status=STATUS_ERRO, resultado=None, erro="busca não encontrada")
            return

        opcoes, erro = chamar_fetcher(texto)
        if time.monotonic() - t0 > TIMEOUT_JOB_S:
            erro = erro or "a busca demorou demais — tenta de novo em instantes"
            opcoes = []
        if erro and not opcoes:
            _finalizar(job_id, status=STATUS_ERRO, resultado={"opcoes": []}, erro=erro)
        else:
            _finalizar(
                job_id,
                status=STATUS_PRONTO,
                resultado={"opcoes": opcoes, "latencia_ms": int((time.monotonic() - t0) * 1000)},
                erro=None,
            )
    except Exception as e:  # noqa: BLE001 — worker nunca pode deixar job pendurado
        log.warning("busca_imovel_job_falhou", job_id=job_id, erro=str(e)[:200])
        _finalizar(
            job_id,
            status=STATUS_ERRO,
            resultado={"opcoes": []},
            erro="não consegui buscar agora — tenta de novo",
        )
    finally:
        parar.set()


def disparar_worker(job_id: int) -> None:
    """Sobe o worker em thread daemon (não bloqueia o request)."""
    threading.Thread(
        target=executar_job, args=(job_id,), daemon=True, name=f"busca-imovel-{job_id}"
    ).start()


def ler_job(job_id: int, *, usuario: str) -> dict[str, Any] | None:
    """Lê o estado do job pro polling da tela. None = não é dele / não existe."""
    with SessionLocal() as s:
        job = s.get(BuscaImovelJob, job_id)
        if job is None or job.usuario != usuario:
            return None
        if _esta_morto(job):
            job.status = STATUS_ERRO
            job.erro = "a busca foi interrompida (o copiloto reiniciou) — tenta de novo"
            job.concluido_em = _agora()
            s.commit()
        return _serializar(job)


def listar_jobs(*, usuario: str, limite: int = 5) -> list[dict[str, Any]]:
    """Últimos jobs do usuário — é o que faz a tela reabrir já com resultado."""
    with SessionLocal() as s:
        linhas = (
            s.execute(
                select(BuscaImovelJob)
                .where(BuscaImovelJob.usuario == usuario)
                .order_by(BuscaImovelJob.id.desc())
                .limit(max(1, min(limite, 20)))
            )
            .scalars()
            .all()
        )
        saida = []
        mudou = False
        for job in linhas:
            if _esta_morto(job):
                job.status = STATUS_ERRO
                job.erro = "a busca foi interrompida (o copiloto reiniciou) — tenta de novo"
                job.concluido_em = _agora()
                mudou = True
            saida.append(_serializar(job))
        if mudou:
            s.commit()
        return saida


def _serializar(job: BuscaImovelJob) -> dict[str, Any]:
    resultado = job.resultado or {}
    return {
        "job_id": job.id,
        "status": job.status,
        "pedido": job.texto,
        "opcoes": resultado.get("opcoes") or [],
        "erro": job.erro,
        "criado_em": job.criado_em.isoformat() if job.criado_em else None,
        "concluido_em": job.concluido_em.isoformat() if job.concluido_em else None,
    }


def limpar_antigos(dias: int = RETENCAO_DIAS) -> int:
    """Apaga jobs mais velhos que `dias`. Chamado de forma oportunista no POST."""
    corte = _agora() - timedelta(days=dias)
    with SessionLocal() as s:
        res = s.execute(
            sa_text("DELETE FROM busca_imovel_jobs WHERE criado_em < :c"), {"c": corte}
        )
        s.commit()
        return res.rowcount or 0
