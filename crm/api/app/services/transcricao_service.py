"""
Transcrição de áudios WhatsApp via Gemini.

Duas entradas públicas:
  transcrever_audio(audio_bytes, mime_type)  → dict com texto/modelo/erro
  transcrever_mensagem(mensagem_id)          → grava resultado no banco, retorna bool

Comportamento:
- Apenas mensagens tipo_mensagem='audio' com transcricao_status='pendente' são processadas
- Idempotente: segunda chamada na mesma mensagem retorna False (already done)
- Nunca levanta — erros são capturados e gravados como status='erro'
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Any

import structlog
from sqlalchemy import select, text as sql_text, update

from app.config import settings
from app.db.models import MensagemWhatsapp
from app.db.session import SessionLocal
from app.integrations.evolution_client import EvolutionClient

log = structlog.get_logger()

_PROMPT = (
    "Transcreva fielmente este áudio em português brasileiro. "
    "Mantenha pontuação natural. "
    "Não interprete, não resuma, não comente. "
    "Apenas transcreva o que foi dito."
)

_MAX_RETRIES = 2
_TIMEOUT = 60
_BACKOFF_BASE_S = 2  # sleep = base * 2**attempt (backoff exponencial nos retries)


# Marcador interno: o download falhou por THROTTLE, não por mídia perdida.
# Quem vê isso devolve o áudio pra fila (status 'pendente') em vez de gravar
# erro — senão um pico de rate limit apaga a chance de tentar de novo.
RATE_LIMIT = "__RATE_LIMIT__"


def transcrever_audio(audio_bytes: bytes, mime_type: str) -> dict[str, Any]:
    """
    Transcreve audio_bytes via provider configurado (settings.transcricao_provider).

    Default: whisper_mac (faster-whisper via Vpn, grátis).
    Fallback: gemini — usado quando o Mac está fora do ar (dorme, Vpn
    caído, serviço parado). Sem isso o áudio vira 'erro' e some: o Mac offline
    parava a transcrição inteira, e ninguém percebia porque a falha é silenciosa.

    Returns:
        {'texto': str | None, 'modelo': str, 'erro': str | None}
    """
    if settings.transcricao_provider == "whisper_mac":
        from app.services.transcricao_whisper_mac import transcrever_audio_whisper_mac
        resultado = transcrever_audio_whisper_mac(audio_bytes, mime_type)
        if not resultado.get("erro"):
            return resultado
        if not settings.gemini_api_key:
            return resultado
        log.warning(
            "transcricao_whisper_mac_indisponivel_fallback_gemini",
            erro=str(resultado.get("erro"))[:160],
        )

    from google import genai
    from google.genai import types

    modelo = settings.gemini_transcricao_model
    client = genai.Client(api_key=settings.gemini_api_key)

    for attempt in range(_MAX_RETRIES):
        t0 = time.monotonic()
        try:
            response = client.models.generate_content(
                model=modelo,
                contents=[
                    types.Part.from_bytes(data=audio_bytes, mime_type=mime_type),
                    _PROMPT,
                ],
            )
            elapsed = round(time.monotonic() - t0, 2)
            texto = (response.text or "").strip() or None
            log.info(
                "transcricao_gemini_ok",
                modelo=modelo,
                bytes=len(audio_bytes),
                elapsed_s=elapsed,
                texto_len=len(texto) if texto else 0,
            )
            return {"texto": texto, "modelo": modelo, "erro": None}
        except Exception as exc:
            elapsed = round(time.monotonic() - t0, 2)
            err_str = str(exc)
            log.warning(
                "transcricao_gemini_erro",
                attempt=attempt + 1,
                max=_MAX_RETRIES,
                elapsed_s=elapsed,
                erro=err_str[:200],
            )
            if attempt < _MAX_RETRIES - 1:
                time.sleep(_BACKOFF_BASE_S * (2 ** attempt))
            else:
                return {"texto": None, "modelo": modelo, "erro": err_str}

    return {"texto": None, "modelo": modelo, "erro": "max retries exceeded"}


def transcrever_pendentes(limit: int = 50, max_idade_horas: int = 48) -> dict[str, int]:
    """
    Pré-flush: processa transcrições pendentes em batch.
    Chamado pelo Inspector antes do pipeline rodar pra reduzir áudios sem texto.

    Limita janela em max_idade_horas pra não tentar mídia velha que já expirou
    no WhatsApp (404 garantido).

    Returns: {"processadas": int, "sucesso": int, "erro": int}.
    Nunca levanta.
    """
    from sqlalchemy import text as sql_text
    from datetime import timedelta

    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_idade_horas)
    try:
        with SessionLocal() as s:
            rows = s.execute(
                sql_text("""
                    SELECT id FROM mensagens_whatsapp
                    WHERE tipo_mensagem='audio'
                      AND transcricao_status='pendente'
                      AND timestamp_evolution >= :cutoff
                    ORDER BY timestamp_evolution DESC
                    LIMIT :limit
                """),
                {"cutoff": cutoff, "limit": limit},
            ).fetchall()
        ids = [r[0] for r in rows]
    except Exception:
        log.exception("transcrever_pendentes_query_failed")
        return {"processadas": 0, "sucesso": 0, "erro": 0}

    out = {"processadas": len(ids), "sucesso": 0, "erro": 0}
    for mid in ids:
        ok = transcrever_mensagem(mid)
        if ok:
            out["sucesso"] += 1
        else:
            out["erro"] += 1
    log.info("transcrever_pendentes_done", **out, limit=limit)
    return out


def transcrever_mensagem(mensagem_id: uuid.UUID) -> bool:
    """
    Lê mensagem do banco, baixa áudio do Evolution, transcreve e grava resultado.

    Returns True se transcrição foi realizada (nova), False caso contrário
    (não é áudio, status != pendente, erro de download, etc).
    Nunca levanta.
    """
    try:
        return _transcrever_mensagem(mensagem_id)
    except Exception as exc:
        log.error("transcrever_mensagem_falhou", mensagem_id=str(mensagem_id), erro=str(exc))
        return False


def _claim_transcricao(mensagem_id: uuid.UUID) -> str | None:
    """Claim atômico da transcrição.

    Faz `UPDATE ... SET transcricao_status='em_progresso'
    WHERE id=:id AND transcricao_status IN ('pendente','erro') RETURNING
    evolution_message_id` numa transação curta. Só UM worker recebe a linha —
    os concorrentes veem 0 linhas afetadas e retornam None (não seguem pro
    download/LLM). Elimina o race read-then-write que fazia dois workers
    baixarem/transcreverem o mesmo áudio (custo dobrado, last-write-wins).

    Retorna o evolution_message_id se o claim teve sucesso, senão None.
    """
    with SessionLocal() as s:
        row = s.execute(
            sql_text("""
                UPDATE mensagens_whatsapp
                SET transcricao_status = 'em_progresso'
                WHERE id = :id
                  AND tipo_mensagem = 'audio'
                  AND transcricao_status IN ('pendente', 'erro')
                RETURNING evolution_message_id
            """),
            {"id": mensagem_id},
        ).first()
        s.commit()
    return row[0] if row else None


def _transcrever_mensagem(mensagem_id: uuid.UUID) -> bool:
    # Pré-checagem barata: não-áudio vira nao_aplicavel; ausente loga e sai.
    # (O claim atômico abaixo já garante que só áudio pendente/erro segue —
    # esta etapa só evita transições espúrias e produz o log certo.)
    with SessionLocal() as s:
        msg = s.execute(
            select(
                MensagemWhatsapp.tipo_mensagem,
                MensagemWhatsapp.transcricao_status,
            ).where(MensagemWhatsapp.id == mensagem_id)
        ).first()

        if msg is None:
            log.warning("transcrever_mensagem_nao_encontrada", mensagem_id=str(mensagem_id))
            return False

        if msg.tipo_mensagem != "audio":
            _set_status(s, mensagem_id, "nao_aplicavel", None, None, None)
            return False

    # Claim atômico: só quem receber a linha (transição pendente/erro →
    # em_progresso) segue pro download/LLM. Concorrentes param aqui.
    evolution_message_id = _claim_transcricao(mensagem_id)
    if evolution_message_id is None:
        log.info("transcrever_mensagem_claim_perdido", mensagem_id=str(mensagem_id))
        return False

    # Baixa áudio fora da sessão do banco
    audio_bytes, mime_type, download_err = _baixar_audio(mensagem_id)
    if download_err:
        with SessionLocal() as s:
            if download_err == RATE_LIMIT:
                # Volta pra fila: o áudio continua lá, só fomos rápido demais.
                _set_status(s, mensagem_id, "pendente", None, None,
                            "rate limit da Graph — reenfileirado")
            else:
                _set_status(s, mensagem_id, "erro", None, None,
                            f"download: {download_err}")
        return False

    # Guarda o áudio em disco pra tocar no dashboard pra sempre (a URL do
    # WhatsApp expira; já temos os bytes aqui, então persistimos de graça).
    _salvar_audio_cache(evolution_message_id, audio_bytes, mime_type)

    resultado = transcrever_audio(audio_bytes, mime_type)

    with SessionLocal() as s:
        if resultado["erro"]:
            _set_status(s, mensagem_id, "erro", None, resultado["modelo"], resultado["erro"])
            return False

        _set_status(
            s,
            mensagem_id,
            "concluida",
            resultado["texto"],
            resultado["modelo"],
            None,
        )

    return True


def _set_status(
    session: Any,
    mensagem_id: uuid.UUID,
    status: str,
    texto: str | None,
    modelo: str | None,
    erro: str | None,
) -> None:
    now = datetime.now(timezone.utc)
    values: dict[str, Any] = {"transcricao_status": status}
    if texto is not None:
        values["transcricao"] = texto
    if modelo is not None:
        values["transcricao_modelo"] = modelo
    if erro is not None:
        values["transcricao_erro"] = erro
    if status in ("concluida", "erro"):
        values["transcricao_em"] = now

    session.execute(
        update(MensagemWhatsapp)
        .where(MensagemWhatsapp.id == mensagem_id)
        .values(**values)
    )
    session.commit()


MEDIA_DIR = "/var/lib/copiloto-media"


def _salvar_audio_cache(evolution_message_id: str | None, audio_bytes: bytes, mime_type: str) -> None:
    """Persiste o áudio no cache de mídia (best-effort, nunca levanta)."""
    import os
    import re

    if not evolution_message_id:
        return
    safe_id = re.sub(r"[^A-Za-z0-9]", "", evolution_message_id)[:80]
    if not safe_id:
        return
    try:
        os.makedirs(MEDIA_DIR, exist_ok=True)
        with open(os.path.join(MEDIA_DIR, safe_id), "wb") as f:
            f.write(audio_bytes)
        with open(os.path.join(MEDIA_DIR, safe_id + ".mime"), "w") as f:
            f.write(mime_type or "audio/ogg")
    except Exception as exc:  # noqa: BLE001
        log.warning("salvar_audio_cache_falhou", erro=str(exc)[:120])


def _cloud_media_id_e_mime(
    raw_payload: dict[str, Any] | None,
    meta: dict[str, Any] | None,
) -> tuple[str | None, str | None]:
    """Descobre o media_id/mime de uma mensagem da linha Cloud (Pessoa12).

    Fonte 1 (mensagens gravadas depois da missão G5): `conteudo_metadata`
    tem `cloud_media_id`/`mime_type`.
    Fonte 2 (mensagens legadas): o `raw_payload` da Cloud tem `{tipo}.id`
    e `{tipo}.mime_type` direto — extrai de lá pro backfill funcionar.
    """
    meta = meta or {}
    mid = meta.get("cloud_media_id")
    mime = meta.get("mime_type")
    if mid:
        return str(mid), mime

    raw = raw_payload or {}
    tipo = raw.get("type")
    if tipo:
        obj = raw.get(tipo)
        if isinstance(obj, dict) and obj.get("id"):
            return str(obj["id"]), obj.get("mime_type") or mime
    return None, mime


def _baixar_audio(mensagem_id: uuid.UUID) -> tuple[bytes, str, str | None]:
    """
    Busca o raw_payload da mensagem e baixa o binário.

    O roteamento segue o PAYLOAD, não a instance: Pessoa10, Pessoa11 e Corretor migraram pra
    Cloud API em coexistência mantendo a instance histórica `acme_<nome>`
    (ver `_DESTINO` em app/api/cloud_corretora.py), então casar por
    `instance == acme_corretora_a` deixava o áudio delas batendo no Evolution —
    400/404 em todo download, 2.6k áudios perdidos em 30 dias.

    - Payload da Cloud (tem `{tipo}.id` ou `cloud_media_id`): baixa pela Graph.
    - Payload do Evolution (data.key/data.message): getBase64FromMediaMessage.

    Instance com histórico misto (Evolution antes da migração, Cloud depois)
    funciona nos dois lados — cada mensagem é roteada pelo próprio payload.

    Retorna (bytes, mime_type, erro_str_ou_None).
    """
    import base64

    with SessionLocal() as s:
        row = s.execute(
            select(
                MensagemWhatsapp.instance,
                MensagemWhatsapp.raw_payload,
                MensagemWhatsapp.conteudo_metadata,
            ).where(MensagemWhatsapp.id == mensagem_id)
        ).first()

    if not row:
        return b"", "", "mensagem não encontrada"

    instance, raw_payload, meta = row

    # ── Payload da Cloud: baixa via Graph media_id, não via Evolution ──
    media_id, mime = _cloud_media_id_e_mime(raw_payload, meta)
    from app.api.cloud_corretora_a import INSTANCE as CLOUD_INSTANCE

    if media_id or instance == CLOUD_INSTANCE:
        from app.api import cloud_corretora_a

        mime_type = mime or "audio/ogg; codecs=opus"
        if not media_id:
            return b"", mime_type, "cloud: media_id ausente (raw_payload sem {tipo}.id)"
        try:
            audio_bytes = cloud_corretora_a.baixar_media_cloud(media_id)
        except cloud_corretora_a.CloudRateLimit:
            # Não é mídia perdida: é a Graph pedindo pra ir devagar. Devolve o
            # marcador RATE_LIMIT pra este áudio VOLTAR pra fila em vez de ser
            # gravado como erro definitivo (incidente 25/08/2026: 1.275 áudios
            # queimados assim num backfill com workers demais).
            return b"", mime_type, RATE_LIMIT
        if not audio_bytes:
            return b"", mime_type, "cloud: download falhou/mídia expirada"
        return audio_bytes, mime_type, None

    data = raw_payload.get("data") if raw_payload else raw_payload or {}
    if data is None:
        data = raw_payload or {}

    msg_key = data.get("key", {})
    message = data.get("message", {})
    mime_type: str = (meta or {}).get("mimetype", "audio/ogg; codecs=opus")

    client = EvolutionClient()
    try:
        resp = client._post(
            f"/chat/getBase64FromMediaMessage/{instance}",
            body={"message": {"key": msg_key, "message": message}},
        )
    except Exception as exc:
        return b"", mime_type, str(exc)

    if not isinstance(resp, dict) or "base64" not in resp:
        return b"", mime_type, f"resposta inesperada: {str(resp)[:200]}"

    try:
        audio_bytes = base64.b64decode(resp["base64"])
    except Exception as exc:
        return b"", mime_type, f"erro decodificando base64: {exc}"

    return audio_bytes, mime_type, None
