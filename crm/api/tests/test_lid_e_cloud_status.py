"""
Testes unitários (sem DB) para dois pontos da Revisão 01 - Ingestão:

- extração de telefone real de payload `@lid` (gap G1)
- montagem do patch de delivery status Cloud → conteudo_metadata (gap G3)

Payloads baseados em amostras reais anonimizadas do banco de produção.
"""
from __future__ import annotations

from app.api.cloud_corretora_a import build_status_patch
from app.services.mensagens_evolution_service import extrair_telefone_lid


# ---------------------------------------------------------------------------
# G1 — extração de telefone de payload @lid
# ---------------------------------------------------------------------------

def test_lid_resolve_por_remotejidalt() -> None:
    """key.remoteJidAlt (`@s.whatsapp.net`) é o caminho mais comum no Evolution v2."""
    data = {
        "key": {
            "id": "2AD2366A8C33E8DFCF73",
            "fromMe": True,
            "remoteJid": "000000000000000@lid",
            "remoteJidAlt": "+5500000000000@s.whatsapp.net",
            "addressingMode": "lid",
        },
        "message": {"conversation": "oi"},
    }
    assert extrair_telefone_lid(data) == "+5500000000000"


def test_lid_resolve_por_senderpn() -> None:
    data = {
        "key": {
            "remoteJid": "000000000000000@lid",
            "senderPn": "+5500000000000@s.whatsapp.net",
        }
    }
    assert extrair_telefone_lid(data) == "+5500000000000"


def test_lid_resolve_por_participantalt() -> None:
    data = {"key": {"remoteJid": "000000000000000@lid", "participantAlt": "+5500000000000"}}
    assert extrair_telefone_lid(data) == "+5500000000000"


def test_lid_resolve_por_remotejidalt_toplevel() -> None:
    data = {
        "key": {"remoteJid": "000000000000000@lid"},
        "remoteJidAlt": "+5500000000000@s.whatsapp.net",
    }
    assert extrair_telefone_lid(data) == "+5500000000000"


def test_lid_sem_telefone_retorna_none() -> None:
    """Payload real @lid sem nenhum campo alternativo → None (mantém comportamento antigo)."""
    data = {
        "key": {
            "id": "3EB05D5240F18D030B78",
            "fromMe": True,
            "remoteJid": "000000000000000@lid",
            "participant": "",
            "addressingMode": "lid",
        },
        "message": {"conversation": "viu o que te enviei?"},
    }
    assert extrair_telefone_lid(data) is None


def test_lid_ignora_alt_que_tambem_e_lid() -> None:
    """Um remoteJidAlt que também é @lid não é telefone real → cai pro próximo/None."""
    data = {"key": {"remoteJid": "111@lid", "remoteJidAlt": "222@lid"}}
    assert extrair_telefone_lid(data) is None


def test_lid_prioridade_remotejidalt_sobre_senderpn() -> None:
    data = {
        "key": {
            "remoteJid": "999@lid",
            "remoteJidAlt": "+000000000000000@s.whatsapp.net",
            "senderPn": "+000000000000000@s.whatsapp.net",
        }
    }
    assert extrair_telefone_lid(data) == "+000000000000000"


# ---------------------------------------------------------------------------
# G3 — patch de delivery status Cloud
# ---------------------------------------------------------------------------

def test_status_patch_delivered() -> None:
    patch = build_status_patch("delivered", timestamp="1782926093")
    assert patch["delivery_status"] == "delivered"
    assert patch["delivery_ts"].startswith("2026-")
    assert "delivery_error" not in patch


def test_status_patch_failed_com_erro() -> None:
    patch = build_status_patch(
        "failed",
        timestamp=1782926093,
        errors=[{"code": 131026, "title": "Message undeliverable"}],
    )
    assert patch["delivery_status"] == "failed"
    assert patch["delivery_error"] == "Message undeliverable"


def test_status_patch_failed_usa_message_se_sem_title() -> None:
    patch = build_status_patch(
        "failed", errors=[{"code": 131047, "message": "Re-engagement message"}]
    )
    assert patch["delivery_error"] == "Re-engagement message"


def test_status_patch_sem_timestamp() -> None:
    patch = build_status_patch("read")
    assert patch["delivery_status"] == "read"
    assert patch["delivery_ts"] is None


def test_status_patch_timestamp_invalido_nao_quebra() -> None:
    patch = build_status_patch("sent", timestamp="nao-e-numero")
    assert patch["delivery_status"] == "sent"
    assert patch["delivery_ts"] is None
