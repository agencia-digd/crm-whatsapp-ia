"""Testes do parser de echo da linha Cloud da Pessoa12 (Revisão 01 - gap G2).

Echo = mensagem que a Pessoa12 manda PELO CELULAR na coexistência; a Meta ecoa via
webhook com `from` = número do negócio e destinatário em `to`/`recipient_id`.
Aqui testamos só as funções puras de parsing (sem DB): destino, tipo/texto.
Payloads baseados na estrutura documentada de message_echoes.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.api import cloud_corretora_a as cg
from app.api.cloud_corretora_a import _echo_destino, _echo_texto_tipo


def _echo_texto(destino: str = "+5500000000000", body: str = "oi, ainda tem interesse?") -> dict:
    return {
        "from": "+5500000000000",          # número do negócio (Pessoa12)
        "to": destino,                     # lead
        "id": "wamid.ECHO123",
        "timestamp": "1782926093",
        "type": "text",
        "text": {"body": body},
    }


# --- destino ---------------------------------------------------------------

def test_echo_destino_por_to() -> None:
    assert _echo_destino(_echo_texto()) == "+5500000000000"


def test_echo_destino_por_recipient_id() -> None:
    echo = {"from": "+5500000000000", "recipient_id": "+5500000000000",
            "type": "text", "text": {"body": "x"}}
    assert _echo_destino(echo) == "+5500000000000"


def test_echo_destino_normaliza_digitos() -> None:
    echo = {"to": "+5500000000000", "type": "text", "text": {"body": "x"}}
    assert _echo_destino(echo) == "+5500000000000"


def test_echo_destino_ausente_retorna_none() -> None:
    echo = {"from": "+5500000000000", "type": "text", "text": {"body": "x"}}
    assert _echo_destino(echo) is None


# --- tipo/texto ------------------------------------------------------------

def test_echo_texto_simples() -> None:
    tipo, texto = _echo_texto_tipo(_echo_texto(body="marquei sua visita"))
    assert tipo == "text"
    assert texto == "marquei sua visita"


def test_echo_midia_com_caption() -> None:
    echo = {"to": "+5500000000000", "type": "image",
            "image": {"id": "media123", "caption": "planta do apto"}}
    tipo, texto = _echo_texto_tipo(echo)
    assert tipo == "image"
    assert texto == "planta do apto"


def test_echo_midia_sem_caption_vira_placeholder() -> None:
    echo = {"to": "+5500000000000", "type": "audio", "audio": {"id": "media123"}}
    tipo, texto = _echo_texto_tipo(echo)
    assert tipo == "audio"
    assert texto == "[audio]"


def test_echo_tipo_default_text() -> None:
    echo = {"to": "+5500000000000", "text": {"body": "sem type explicito"}}
    tipo, texto = _echo_texto_tipo(echo)
    assert tipo == "text"
    assert texto == "sem type explicito"


# --- dedup: mídia NÃO usa dedup por texto placeholder ("[audio]") -----------
# Regressão do bug em que 2 áudios seguidos pro mesmo lead colidiam em
# conteudo_texto="[audio]" e o 2º era descartado como dup_conteudo.

def _fake_session() -> MagicMock:
    s = MagicMock()
    s.__enter__.return_value = s
    s.__exit__.return_value = False
    return s


def test_echo_audio_ignora_dedup_por_conteudo() -> None:
    """Áudio deve deduplicar por media_id, não por texto. Mesmo que o dedup de
    conteúdo dissesse 'já existe [audio]', o áudio precisa ser gravado."""
    echo = {"to": "+5500000000000", "id": "wamid.A1", "type": "audio",
            "audio": {"id": "MEDIA_A1", "mime_type": "audio/ogg; codecs=opus"}}
    with patch.object(cg, "SessionLocal", return_value=_fake_session()), \
         patch.object(cg, "_echo_ja_registrado_por_conteudo", return_value=True) as m_cont, \
         patch.object(cg, "_echo_ja_registrado_por_media", return_value=False) as m_media, \
         patch.object(cg, "gravar", return_value=True) as m_grava:
        assert cg.registrar_echo_corretor(echo) == "gravado"
    m_cont.assert_not_called()          # texto placeholder NÃO deduplica mídia
    m_media.assert_called_once()
    # metadata com media_id foi passado pra gravar (pro download/transcrição)
    assert m_grava.call_args.kwargs["metadata"] == {
        "cloud_media_id": "MEDIA_A1", "mime_type": "audio/ogg; codecs=opus"}
    assert m_grava.call_args.kwargs["tipo"] == "audio"


def test_echo_audio_dedup_por_media_id() -> None:
    """Re-entrega da MESMA mídia (mesmo media_id em 300s) é deduplicada."""
    echo = {"to": "+5500000000000", "id": "wamid.A2", "type": "audio",
            "audio": {"id": "MEDIA_A1"}}
    with patch.object(cg, "SessionLocal", return_value=_fake_session()), \
         patch.object(cg, "_echo_ja_registrado_por_media", return_value=True), \
         patch.object(cg, "gravar", return_value=True) as m_grava:
        assert cg.registrar_echo_corretor(echo) == "dup_conteudo"
    m_grava.assert_not_called()


def test_echo_texto_ainda_dedup_por_conteudo() -> None:
    """Texto mantém o dedup por conteúdo (cobre sync via Evolution-Mac)."""
    echo = {"to": "+5500000000000", "id": "wamid.T1", "type": "text",
            "text": {"body": "oi"}}
    with patch.object(cg, "SessionLocal", return_value=_fake_session()), \
         patch.object(cg, "_echo_ja_registrado_por_conteudo", return_value=True), \
         patch.object(cg, "gravar", return_value=True) as m_grava:
        assert cg.registrar_echo_corretor(echo) == "dup_conteudo"
    m_grava.assert_not_called()
