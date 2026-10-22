"""
Testes para transcricao_service.

Cobre:
1. transcrever_audio com áudio real (smoke test Gemini)
2. transcrever_audio com bytes inválidos → erro capturado
3. transcrever_mensagem com tipo != audio → nao_aplicavel
4. Idempotência: segunda chamada em mensagem já concluída retorna False
"""
from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import text

from app.config import settings
from app.db.session import SessionLocal
from app.services.transcricao_service import transcrever_audio, transcrever_mensagem

# ── Helpers ───────────────────────────────────────────────────────────────────

_INSTANCE = settings.evolution_assistente_instance

_MSG_PREFIX = "TTEST-"


def _fetch_msg(msg_id_str: str) -> dict | None:
    with SessionLocal() as s:
        row = s.execute(
            text("SELECT * FROM mensagens_whatsapp WHERE evolution_message_id = :id"),
            {"id": msg_id_str},
        ).mappings().first()
        return dict(row) if row else None


def _fetch_by_uuid(uid: uuid.UUID) -> dict | None:
    with SessionLocal() as s:
        row = s.execute(
            text("SELECT * FROM mensagens_whatsapp WHERE id = :id"),
            {"id": uid},
        ).mappings().first()
        return dict(row) if row else None


@pytest.fixture(autouse=True)
def _clean():
    _delete()
    yield
    _delete()


def _delete() -> None:
    with SessionLocal() as s:
        s.execute(text(f"DELETE FROM mensagens_whatsapp WHERE evolution_message_id LIKE '{_MSG_PREFIX}%'"))
        s.commit()


def _insert_msg(
    evolution_message_id: str,
    tipo: str = "text",
    transcricao_status: str | None = None,
    instance: str = "acme_corretor",
    raw_payload: dict | None = None,
    conteudo_metadata: dict | None = None,
) -> uuid.UUID:
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from app.db.models import MensagemWhatsapp
    from datetime import datetime, timezone

    status = transcricao_status or ("pendente" if tipo == "audio" else "nao_aplicavel")
    with SessionLocal() as s:
        stmt = pg_insert(MensagemWhatsapp).values(
            evolution_message_id=evolution_message_id,
            instance=instance,
            remote_jid="+5500000000000@s.whatsapp.net",
            from_corretora=False,
            tipo_mensagem=tipo,
            conteudo_texto=None,
            conteudo_metadata=(
                conteudo_metadata
                if conteudo_metadata is not None
                else {"ptt": True, "duracao_segundos": 5}
            ),
            raw_payload=raw_payload,
            timestamp_evolution=datetime.now(timezone.utc),
            transcricao_status=status,
        ).returning(MensagemWhatsapp.id)
        result = s.execute(stmt)
        s.commit()
        return result.scalar_one()


# ── 1. Smoke test Gemini com áudio sintético mínimo ──────────────────────────

@pytest.mark.integration
def test_transcrever_audio_real_gemini() -> None:
    """Transcreve áudio PTT real do banco (Pessoa11, ~30s). Requer GEMINI_API_KEY."""
    if not settings.gemini_api_key:
        pytest.skip("GEMINI_API_KEY não configurada")

    import base64
    import httpx

    with SessionLocal() as s:
        row = s.execute(
            text("""
                SELECT id, instance, raw_payload, conteudo_metadata
                FROM mensagens_whatsapp
                WHERE tipo_mensagem = 'audio'
                  AND (conteudo_metadata->>'duracao_segundos')::int BETWEEN 20 AND 45
                  AND instance = 'acme_corretor'
                ORDER BY timestamp_evolution DESC LIMIT 1
            """)
        ).mappings().first()

    assert row is not None, "nenhum áudio de 20-45s encontrado no banco"

    raw = row["raw_payload"]
    data = raw.get("data") if raw else raw or {}
    if data is None:
        data = raw or {}
    msg_key = data.get("key", {})
    message = data.get("message", {})
    meta = row["conteudo_metadata"] or {}
    mime_type = meta.get("mimetype", "audio/ogg; codecs=opus")

    EVOLUTION_URL = settings.evolution_api_url or settings.evolution_base_url
    EVOLUTION_KEY = settings.evolution_api_key

    resp = httpx.post(
        f"{EVOLUTION_URL}/chat/getBase64FromMediaMessage/{row['instance']}",
        headers={"apikey": EVOLUTION_KEY},
        json={"message": {"key": msg_key, "message": message}},
        timeout=30,
    )
    assert resp.status_code in (200, 201), f"Evolution retornou {resp.status_code}: {resp.text[:200]}"
    b64 = resp.json().get("base64")
    assert b64, "Evolution não retornou base64"
    audio_bytes = base64.b64decode(b64)

    resultado = transcrever_audio(audio_bytes, mime_type)

    assert resultado["erro"] is None, f"Erro na transcrição: {resultado['erro']}"
    assert resultado["texto"], "transcrição vazia"
    assert len(resultado["texto"]) > 10, "transcrição muito curta"

    print(f"\n=== SMOKE TEST TRANSCRIÇÃO ===")
    print(f"Duração: {meta.get('duracao_segundos')}s | Bytes: {len(audio_bytes)}")
    print(f"Modelo: {resultado['modelo']}")
    print(f"Texto:\n{resultado['texto']}")


# ── 2. Bytes inválidos → nunca levanta, sempre retorna dict ──────────────────

def test_transcrever_audio_invalido_nao_levanta() -> None:
    # Gemini pode aceitar bytes inválidos e retornar texto (ou erro) —
    # a garantia é que a função NUNCA levanta e sempre retorna o dict correto.
    with patch.object(settings, "transcricao_provider", "gemini"), \
         patch("google.genai.Client") as mock_client_cls:
        mock_client_cls.return_value.models.generate_content.side_effect = Exception("API error")
        resultado = transcrever_audio(b"isto nao e audio", "audio/ogg; codecs=opus")
    assert resultado["erro"] is not None
    assert resultado["texto"] is None


# ── 3. Mensagem não-áudio → nao_aplicavel ────────────────────────────────────

def test_transcrever_mensagem_nao_audio() -> None:
    uid = _insert_msg(f"{_MSG_PREFIX}text-001", tipo="text")
    ok = transcrever_mensagem(uid)
    assert ok is False
    row = _fetch_by_uuid(uid)
    assert row["transcricao_status"] == "nao_aplicavel"


# ── 4. Idempotência: concluída → segunda chamada retorna False ────────────────

def test_transcrever_mensagem_idempotente() -> None:
    uid = _insert_msg(f"{_MSG_PREFIX}idem-001", tipo="audio", transcricao_status="concluida")

    with patch("app.services.transcricao_service._baixar_audio") as mock_dl, \
         patch("app.services.transcricao_service.transcrever_audio") as mock_tr:
        ok = transcrever_mensagem(uid)

    assert ok is False
    mock_dl.assert_not_called()
    mock_tr.assert_not_called()


# ── 5. Claim atômico: só quem vence o claim baixa/transcreve ─────────────────

def test_claim_perdido_nao_baixa_nem_transcreve() -> None:
    """Se _claim_transcricao devolve None (outro worker já pegou), o fluxo
    para ANTES do download/LLM — sem custo dobrado."""
    uid = _insert_msg(f"{_MSG_PREFIX}claim-001", tipo="audio", transcricao_status="pendente")

    with patch("app.services.transcricao_service._claim_transcricao", return_value=None) as mock_claim, \
         patch("app.services.transcricao_service._baixar_audio") as mock_dl, \
         patch("app.services.transcricao_service.transcrever_audio") as mock_tr:
        ok = transcrever_mensagem(uid)

    assert ok is False
    mock_claim.assert_called_once()
    mock_dl.assert_not_called()
    mock_tr.assert_not_called()


def test_dois_workers_concorrentes_so_um_baixa() -> None:
    """Dois workers na MESMA mensagem: o claim atômico deixa só o primeiro
    seguir; o segundo vê 0 linhas (None) e para. Só 1 download/transcrição."""
    from app.services import transcricao_service as svc

    uid = _insert_msg(f"{_MSG_PREFIX}claim-race", tipo="audio", transcricao_status="pendente")

    # 1º claim vence (retorna evolution_message_id), 2º perde (None) — simula
    # o UPDATE ... RETURNING atômico sob concorrência.
    claim_results = iter([f"{_MSG_PREFIX}claim-race", None])

    def fake_claim(_mid: uuid.UUID) -> str | None:
        return next(claim_results)

    with patch.object(svc, "_claim_transcricao", side_effect=fake_claim), \
         patch.object(svc, "_baixar_audio", return_value=(b"fakeaudio", "audio/ogg", None)) as mock_dl, \
         patch.object(svc, "_salvar_audio_cache"), \
         patch.object(svc, "transcrever_audio", return_value={"texto": "oi", "modelo": "m", "erro": None}) as mock_tr:
        ok1 = transcrever_mensagem(uid)
        ok2 = transcrever_mensagem(uid)

    assert ok1 is True
    assert ok2 is False
    assert mock_dl.call_count == 1
    assert mock_tr.call_count == 1


def test_claim_atomico_deixa_so_um_worker_no_banco() -> None:
    """Contra o Postgres real: N chamadas concorrentes de _claim_transcricao
    na mesma linha — só UMA recebe o evolution_message_id, resto vê None."""
    from app.services.transcricao_service import _claim_transcricao

    eid = f"{_MSG_PREFIX}claim-db"
    uid = _insert_msg(eid, tipo="audio", transcricao_status="pendente")

    r1 = _claim_transcricao(uid)
    r2 = _claim_transcricao(uid)
    r3 = _claim_transcricao(uid)

    # Primeiro claim vence; os seguintes veem status='em_progresso' → None.
    assert r1 == eid
    assert r2 is None
    assert r3 is None
    row = _fetch_by_uuid(uid)
    assert row["transcricao_status"] == "em_progresso"


# ── 6. Roteamento do download: Cloud API vs Evolution ────────────────────────
#
# Pessoa10, Pessoa11 e Corretor migraram pra Cloud API (coexistência) mantendo a instance
# histórica `acme_<nome>`. O roteamento não pode olhar só a instance da Pessoa12:
# tem que seguir o PAYLOAD. Payload da Cloud traz `{tipo}.id` (media_id) —
# baixar pelo Evolution nesse caso dá 400/404 e o áudio morre sem transcrição.

def _payload_cloud(media_id: str = "000000000000000") -> dict:
    return {
        "id": "wamid.TESTE",
        "type": "audio",
        "audio": {"id": media_id, "mime_type": "audio/ogg; codecs=opus"},
        "timestamp": "1756080000",
    }


def _payload_evolution() -> dict:
    return {
        "data": {
            "key": {"id": "EVO1", "remoteJid": "+5500000000000@s.whatsapp.net"},
            "message": {"audioMessage": {"mimetype": "audio/ogg; codecs=opus"}},
        }
    }


def test_baixar_audio_cloud_por_payload_em_instancia_de_corretora() -> None:
    """Instance de corretora (acme_corretora_c) + payload Cloud → baixa pela Graph,
    NUNCA pelo Evolution."""
    from app.services import transcricao_service as svc

    uid = _insert_msg(
        f"{_MSG_PREFIX}cloud-corretor",
        tipo="audio",
        transcricao_status="pendente",
        instance="acme_corretora_c",
        raw_payload=_payload_cloud(),
        conteudo_metadata={"delivery_status": "played"},
    )

    with patch("app.api.cloud_corretora_a.baixar_media_cloud", return_value=b"AUDIOCLOUD") as mock_cloud, \
         patch.object(svc, "EvolutionClient") as mock_evo:
        audio_bytes, mime_type, erro = svc._baixar_audio(uid)

    assert erro is None
    assert audio_bytes == b"AUDIOCLOUD"
    assert mime_type == "audio/ogg; codecs=opus"
    mock_cloud.assert_called_once_with("000000000000000")
    mock_evo.assert_not_called()


def test_baixar_audio_evolution_quando_payload_nao_e_cloud() -> None:
    """Payload no formato Evolution (sem media_id) continua indo pro Evolution."""
    from app.services import transcricao_service as svc

    uid = _insert_msg(
        f"{_MSG_PREFIX}evo-corretor",
        tipo="audio",
        transcricao_status="pendente",
        instance="acme_corretor",
        raw_payload=_payload_evolution(),
    )

    import base64

    with patch("app.api.cloud_corretora_a.baixar_media_cloud") as mock_cloud, \
         patch.object(svc, "EvolutionClient") as mock_evo:
        mock_evo.return_value._post.return_value = {"base64": base64.b64encode(b"AUDIOEVO").decode()}
        audio_bytes, _mime, erro = svc._baixar_audio(uid)

    assert erro is None
    assert audio_bytes == b"AUDIOEVO"
    mock_cloud.assert_not_called()
    mock_evo.return_value._post.assert_called_once()


def test_baixar_audio_evolution_em_instance_da_corretor_vai_pro_evolution() -> None:
    """Desde 20/10 a Pessoa12 tem sessão Evolution gravando em `acme_corretora_a`: payload
    no formato Evolution (sem media_id) não pode cair no ramo Cloud."""
    from app.services import transcricao_service as svc

    uid = _insert_msg(
        f"{_MSG_PREFIX}evo-corretor",
        tipo="audio",
        transcricao_status="pendente",
        instance="acme_corretora_a",
        raw_payload=_payload_evolution(),
    )

    import base64

    with patch("app.api.cloud_corretora_a.baixar_media_cloud") as mock_cloud, \
         patch.object(svc, "EvolutionClient") as mock_evo:
        mock_evo.return_value._post.return_value = {"base64": base64.b64encode(b"AUDIOGI").decode()}
        audio_bytes, _mime, erro = svc._baixar_audio(uid)

    assert erro is None
    assert audio_bytes == b"AUDIOGI"
    mock_cloud.assert_not_called()


def test_baixar_audio_cloud_expirado_reporta_erro() -> None:
    """Mídia da Cloud que já expirou: erro claro, sem cair no Evolution."""
    from app.services import transcricao_service as svc

    uid = _insert_msg(
        f"{_MSG_PREFIX}cloud-exp",
        tipo="audio",
        transcricao_status="pendente",
        instance="acme_corretor",
        raw_payload=_payload_cloud("999"),
    )

    with patch("app.api.cloud_corretora_a.baixar_media_cloud", return_value=None), \
         patch.object(svc, "EvolutionClient") as mock_evo:
        audio_bytes, _mime, erro = svc._baixar_audio(uid)

    assert audio_bytes == b""
    assert erro is not None and "cloud" in erro
    mock_evo.assert_not_called()


# ── 7. Fallback do provider: Mac fora do ar não pode matar a transcrição ─────

def test_whisper_mac_fora_do_ar_cai_pro_gemini() -> None:
    """Mac offline (Vpn/serviço) → transcreve pelo Gemini em vez de perder
    o áudio. O default segue sendo o Mac (grátis)."""
    from app.services import transcricao_service as svc

    if not settings.gemini_api_key:
        pytest.skip("GEMINI_API_KEY não configurada")

    falha = {"texto": None, "modelo": "whisper-mac-large-v3", "erro": "ConnectTimeout"}
    with patch.object(settings, "transcricao_provider", "whisper_mac"), \
         patch("app.services.transcricao_whisper_mac.transcrever_audio_whisper_mac", return_value=falha), \
         patch("google.genai.Client") as mock_genai:
        mock_genai.return_value.models.generate_content.return_value = MagicMock(text="oi, tudo bem?")
        resultado = svc.transcrever_audio(b"audio", "audio/ogg; codecs=opus")

    assert resultado["erro"] is None
    assert resultado["texto"] == "oi, tudo bem?"
    assert "gemini" in resultado["modelo"]


def test_whisper_mac_ok_nao_chama_gemini() -> None:
    """Mac respondendo: não gasta Gemini à toa."""
    from app.services import transcricao_service as svc

    ok = {"texto": "transcrito no mac", "modelo": "whisper-mac-large-v3", "erro": None}
    with patch.object(settings, "transcricao_provider", "whisper_mac"), \
         patch("app.services.transcricao_whisper_mac.transcrever_audio_whisper_mac", return_value=ok), \
         patch("google.genai.Client") as mock_genai:
        resultado = svc.transcrever_audio(b"audio", "audio/ogg; codecs=opus")

    assert resultado["texto"] == "transcrito no mac"
    mock_genai.assert_not_called()


def test_probe_evita_pendurar_quando_mac_nao_responde() -> None:
    """Vpn aceita o TCP mesmo com o Mac dormindo — sem o probe, cada áudio
    ficava 300s pendurado no read. Mac fora = erro imediato, sem POST."""
    from app.services import transcricao_whisper_mac as wm

    wm._probe_cache = None
    with patch.object(wm.httpx, "Client") as mock_client:
        mock_client.return_value.__enter__.return_value.get.side_effect = Exception("timeout")
        resultado = wm.transcrever_audio_whisper_mac(b"audio", "audio/ogg")

    wm._probe_cache = None
    assert resultado["texto"] is None
    assert "fora do ar" in resultado["erro"]
    mock_client.return_value.__enter__.return_value.post.assert_not_called()
