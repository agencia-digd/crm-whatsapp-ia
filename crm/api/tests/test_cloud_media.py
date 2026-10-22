"""
Testes da mídia Cloud (linha da Pessoa12): download via media_id + transcrição de áudio.

Cobre (tudo com mocks — nenhum request real à Graph API):
1. extrair_media_info: extrai (media_id, mime) de mensagens/echoes de mídia
2. baixar_media_cloud: 2 GETs felizes → bytes; 404 → None; erro rede → None
3. _cloud_media_id_e_mime: fonte metadata (novo) e fonte raw_payload (legado)
4. _baixar_audio roteia a linha Cloud pra baixar_media_cloud (não Evolution)
"""
from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

from app.api import cloud_corretora_a as cg
from app.services import transcricao_service as ts

# Payload real anonimizado (telefone/media_id trocados) de um áudio Cloud da Pessoa12.
_AUDIO_MSG = {
    "id": "wamid.HBgNANONYMIZED==",
    "to": "+5500000000000",
    "from": "+5500000000000",
    "type": "audio",
    "audio": {
        "id": "000000000000000",
        "url": "https://lookaside.fbsbx.com/whatsapp_business/attachments/?mid=000000000000000&ext=1&hash=x",
        "voice": True,
        "sha256": "anon",
        "mime_type": "audio/ogg; codecs=opus",
    },
    "timestamp": "1782943471",
}

_IMAGE_ECHO = {
    "id": "wamid.ECHOANON==",
    "to": "+5500000000000",
    "type": "image",
    "image": {"id": "1234567890", "mime_type": "image/jpeg", "caption": "casa"},
}


# ── 1. extrair_media_info ───────────────────────────────────────────────────────

def test_extrair_media_info_audio() -> None:
    assert cg.extrair_media_info(_AUDIO_MSG) == ("000000000000000", "audio/ogg; codecs=opus")


def test_extrair_media_info_image_echo() -> None:
    assert cg.extrair_media_info(_IMAGE_ECHO) == ("1234567890", "image/jpeg")


def test_extrair_media_info_texto_none() -> None:
    assert cg.extrair_media_info({"type": "text", "text": {"body": "oi"}}) is None


def test_extrair_media_info_midia_sem_id_none() -> None:
    assert cg.extrair_media_info({"type": "audio", "audio": {"voice": True}}) is None


# ── 2. baixar_media_cloud ───────────────────────────────────────────────────────

def _mock_client(responses: list[MagicMock]) -> MagicMock:
    """Fabrica um httpx.Client mockado cujo .get() retorna as respostas em ordem."""
    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    client.get.side_effect = responses
    return client


def _resp(status: int, *, json_body: dict | None = None, content: bytes = b"") -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.content = content
    r.text = ""
    if json_body is not None:
        r.json.return_value = json_body
    return r


def test_baixar_media_cloud_feliz() -> None:
    r_meta = _resp(200, json_body={"url": "https://lookaside/x"})
    r_bin = _resp(200, content=b"OGGBYTES")
    with patch.object(cg, "_token", return_value="TOK"), \
         patch.object(cg.httpx, "Client", return_value=_mock_client([r_meta, r_bin])):
        out = cg.baixar_media_cloud("000000000000000")
    assert out == b"OGGBYTES"


def test_baixar_media_cloud_404_meta_none() -> None:
    with patch.object(cg, "_token", return_value="TOK"), \
         patch.object(cg.httpx, "Client", return_value=_mock_client([_resp(404)])):
        assert cg.baixar_media_cloud("expirada") is None


def test_baixar_media_cloud_410_url_none() -> None:
    r_meta = _resp(200, json_body={"url": "https://lookaside/x"})
    with patch.object(cg, "_token", return_value="TOK"), \
         patch.object(cg.httpx, "Client", return_value=_mock_client([r_meta, _resp(410)])):
        assert cg.baixar_media_cloud("expirada") is None


def test_baixar_media_cloud_erro_rede_none() -> None:
    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    client.get.side_effect = RuntimeError("connection reset")
    with patch.object(cg, "_token", return_value="TOK"), \
         patch.object(cg.httpx, "Client", return_value=client):
        assert cg.baixar_media_cloud("x") is None


def test_baixar_media_cloud_media_id_vazio_none() -> None:
    assert cg.baixar_media_cloud("") is None


def test_baixar_media_cloud_400_graphmethod_none() -> None:
    """400 GraphMethodException (echo SMB de coexistência não baixável) → None,
    sem retry (4xx não adianta re-bater), classificado como inacessível."""
    r = _resp(400, json_body={"error": {"code": 100, "message": "Object with ID"}})
    r.text = "Object with ID ... missing permissions"
    with patch.object(cg, "_token", return_value="TOK"), \
         patch.object(cg.httpx, "Client", return_value=_mock_client([r])):
        assert cg.baixar_media_cloud("echo_smb", tentativas=2) is None


def test_baixar_media_cloud_5xx_retry_e_recupera() -> None:
    """5xx no 1º GET é transitório → retry; 2ª tentativa feliz devolve os bytes."""
    r_500 = _resp(500, json_body={"error": {"code": 1, "message": "internal"}})
    r_500.text = "internal"
    r_meta = _resp(200, json_body={"url": "https://lookaside/x"})
    r_bin = _resp(200, content=b"OK")
    # 1ª tentativa: só o r_500 (client novo por tentativa); 2ª: meta+bin.
    with patch.object(cg, "_token", return_value="TOK"), \
         patch.object(cg.httpx, "Client",
                      side_effect=[_mock_client([r_500]), _mock_client([r_meta, r_bin])]):
        assert cg.baixar_media_cloud("x", tentativas=2) == b"OK"


def test_classificar_erro_media_niveis() -> None:
    """A classificação separa esperado (info) de acionável (warning)."""
    assert cg._classificar_erro_media(_resp(404))[1] == "info"          # expirada
    assert cg._classificar_erro_media(_resp(401))[1] == "warning"       # token
    r400 = _resp(400, json_body={"error": {"code": 100}})
    assert cg._classificar_erro_media(r400) == ("midia_inacessivel_coexistencia", "info")


def test_registrar_echo_midia_grava_sem_download() -> None:
    """GARANTIA: echo de mídia vira linha em mensagens_whatsapp SEM depender do
    download do binário. baixar_media_cloud NEM é chamado no ingest do echo —
    mídia é best-effort/assíncrona, a mensagem nunca se perde."""
    with patch.object(cg, "_echo_ja_registrado_por_media", return_value=False), \
         patch.object(cg, "SessionLocal") as _sess, \
         patch.object(cg, "gravar", return_value=True) as mock_gravar, \
         patch.object(cg, "baixar_media_cloud") as mock_dl:
        _sess.return_value.__enter__.return_value = MagicMock()
        assert cg.registrar_echo_corretor(_IMAGE_ECHO) == "gravado"
    mock_dl.assert_not_called()                     # ingest NÃO baixa binário
    kw = mock_gravar.call_args.kwargs
    assert kw["tipo"] == "image"                    # tipo preservado
    assert kw["from_corretora"] is True
    assert kw["metadata"]["cloud_media_id"] == "1234567890"  # media_id no metadata p/ backfill


# ── 3. _cloud_media_id_e_mime ───────────────────────────────────────────────────

def test_cloud_media_id_prefere_metadata() -> None:
    meta = {"cloud_media_id": "META_ID", "mime_type": "audio/ogg"}
    mid, mime = ts._cloud_media_id_e_mime(_AUDIO_MSG, meta)
    assert mid == "META_ID"
    assert mime == "audio/ogg"


def test_cloud_media_id_fallback_raw_payload() -> None:
    # Mensagem legada: sem metadata novo, extrai do raw_payload da Cloud.
    mid, mime = ts._cloud_media_id_e_mime(_AUDIO_MSG, None)
    assert mid == "000000000000000"
    assert mime == "audio/ogg; codecs=opus"


def test_cloud_media_id_ausente() -> None:
    mid, _ = ts._cloud_media_id_e_mime({"type": "text"}, {})
    assert mid is None


# ── 4. _baixar_audio roteia a linha Cloud pro download da Graph ─────────────────

def test_baixar_audio_linha_cloud_usa_graph() -> None:
    fake_row = (cg.INSTANCE, _AUDIO_MSG, None)
    session = MagicMock()
    session.__enter__.return_value = session
    session.__exit__.return_value = False
    session.execute.return_value.first.return_value = fake_row

    with patch.object(ts, "SessionLocal", return_value=session), \
         patch("app.api.cloud_corretora_a.baixar_media_cloud", return_value=b"OGG") as mock_dl:
        audio, mime, err = ts._baixar_audio(uuid.uuid4())

    mock_dl.assert_called_once_with("000000000000000")
    assert audio == b"OGG"
    assert err is None
    assert "ogg" in mime


def test_baixar_audio_linha_cloud_expirada_erro() -> None:
    fake_row = (cg.INSTANCE, _AUDIO_MSG, None)
    session = MagicMock()
    session.__enter__.return_value = session
    session.__exit__.return_value = False
    session.execute.return_value.first.return_value = fake_row

    with patch.object(ts, "SessionLocal", return_value=session), \
         patch("app.api.cloud_corretora_a.baixar_media_cloud", return_value=None):
        audio, _, err = ts._baixar_audio(uuid.uuid4())

    assert audio == b""
    assert err is not None and "cloud" in err.lower()


def test_baixar_audio_linha_evolution_nao_toca_cloud() -> None:
    """Numa linha Evolution, o path Cloud não é usado (usa getBase64)."""
    fake_row = ("acme_corretor", {"data": {"key": {}, "message": {}}}, {"mimetype": "audio/ogg"})
    session = MagicMock()
    session.__enter__.return_value = session
    session.__exit__.return_value = False
    session.execute.return_value.first.return_value = fake_row

    fake_client = MagicMock()
    fake_client._post.return_value = {"base64": "QUJD"}  # base64("ABC")

    with patch.object(ts, "SessionLocal", return_value=session), \
         patch.object(ts, "EvolutionClient", return_value=fake_client), \
         patch("app.api.cloud_corretora_a.baixar_media_cloud") as mock_cloud:
        audio, _, err = ts._baixar_audio(uuid.uuid4())

    mock_cloud.assert_not_called()
    assert audio == b"ABC"
    assert err is None
