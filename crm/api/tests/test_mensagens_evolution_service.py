"""
Testes para mensagens_evolution_service e integração com evolution_webhook.

Cobre:
- Gravação de msg de texto de lead (fromMe=False)
- Gravação de msg de texto de corretora (fromMe=True)
- Gravação de msg de áudio com metadata
- Gravação de msg de imagem com caption
- Gravação de msg de imagem sem caption
- Idempotência (ON CONFLICT DO NOTHING)
- Instance não mapeada → corretora_id=null + warning logado
- Falha no DB não quebra o webhook (200 OK sempre)
"""
from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.config import settings
from app.db.session import SessionLocal
from app.main import app
from app.services.mensagens_evolution_service import gravar_mensagem_evolution


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _payload(
    remote_jid: str,
    msg: dict,
    msg_id: str | None = None,
    from_me: bool = False,
    push_name: str | None = "Contato Teste",
    timestamp: int = 1715299200,
    instance: str = "acme_corretora_a",
) -> dict:
    if msg_id is None:
        msg_id = f"TESTMSG-{uuid.uuid4().hex[:8]}"
    return {
        "event": "messages.upsert",
        "instance": instance,
        "data": {
            "key": {
                "remoteJid": remote_jid,
                "id": msg_id,
                "fromMe": from_me,
            },
            "pushName": push_name,
            "messageTimestamp": timestamp,
            "message": msg,
        },
    }


def _text_payload(**kw) -> dict:
    return _payload(msg={"conversation": "Olá, tudo bem?"}, **kw)


def _audio_payload(**kw) -> dict:
    return _payload(
        msg={"audioMessage": {"ptt": True, "seconds": 12, "mimetype": "audio/ogg; codecs=opus"}},
        **kw,
    )


def _image_payload(caption: str | None = "Foto do imóvel", **kw) -> dict:
    inner: dict = {"mimetype": "image/jpeg", "fileLength": "204800"}
    if caption:
        inner["caption"] = caption
    return _payload(msg={"imageMessage": inner}, **kw)


@pytest.fixture(autouse=True)
def _clean_evolution_msgs():
    """Remove linhas de teste antes e depois de cada teste."""
    _delete_test_rows()
    yield
    _delete_test_rows()


def _delete_test_rows() -> None:
    with SessionLocal() as s:
        s.execute(text("DELETE FROM mensagens_whatsapp WHERE evolution_message_id LIKE 'TESTMSG-%'"))
        s.commit()


def _fetch_by_msg_id(msg_id: str) -> dict | None:
    with SessionLocal() as s:
        row = s.execute(
            text("SELECT * FROM mensagens_whatsapp WHERE evolution_message_id = :id"),
            {"id": msg_id},
        ).mappings().first()
        return dict(row) if row else None


# ---------------------------------------------------------------------------
# 1. Texto de lead (fromMe=False)
# ---------------------------------------------------------------------------

def test_grava_msg_texto_lead() -> None:
    msg_id = "TESTMSG-text-lead"
    gravar_mensagem_evolution(
        instance=settings.evolution_assistente_instance,
        payload=_text_payload(
            remote_jid="+5500000000000@s.whatsapp.net",
            msg_id=msg_id,
            from_me=False,
        ),
    )
    row = _fetch_by_msg_id(msg_id)
    assert row is not None
    assert row["tipo_mensagem"] == "text"
    assert row["conteudo_texto"] == "Olá, tudo bem?"
    assert row["from_corretora"] is False
    assert row["contato_telefone"] == "+5500000000000"
    # fromMe=False → pushName é o nome do LEAD, então vira contato_nome.
    assert row["contato_nome"] == "Contato Teste"


# ---------------------------------------------------------------------------
# 2. Texto de corretora (fromMe=True)
# ---------------------------------------------------------------------------

def test_grava_msg_texto_corretora() -> None:
    msg_id = "TESTMSG-text-corretora"
    gravar_mensagem_evolution(
        instance=settings.evolution_assistente_instance,
        payload=_text_payload(
            remote_jid="+5500000000000@s.whatsapp.net",
            msg_id=msg_id,
            from_me=True,
        ),
    )
    row = _fetch_by_msg_id(msg_id)
    assert row is not None
    assert row["from_corretora"] is True
    assert row["tipo_mensagem"] == "text"
    # fromMe=True → pushName é o nome da CORRETORA (remetente), NÃO do lead.
    # Não deve virar contato_nome, senão todas as msgs enviadas pela corretora
    # se agrupam sob o nome dela (bug: 295 msgs sob "corretora_bazariias" 13/07).
    assert row["contato_nome"] is None


# ---------------------------------------------------------------------------
# 3. Áudio com metadata
# ---------------------------------------------------------------------------

def test_grava_msg_audio_com_metadata() -> None:
    msg_id = "TESTMSG-audio-meta"
    gravar_mensagem_evolution(
        instance=settings.evolution_assistente_instance,
        payload=_audio_payload(
            remote_jid="+5500000000000@s.whatsapp.net",
            msg_id=msg_id,
        ),
    )
    row = _fetch_by_msg_id(msg_id)
    assert row is not None
    assert row["tipo_mensagem"] == "audio"
    assert row["conteudo_texto"] is None
    meta = row["conteudo_metadata"]
    assert meta["ptt"] is True
    assert meta["duracao_segundos"] == 12
    assert "audio/ogg" in meta["mimetype"]


# ---------------------------------------------------------------------------
# 4. Imagem com caption
# ---------------------------------------------------------------------------

def test_grava_msg_imagem_com_caption() -> None:
    msg_id = "TESTMSG-img-caption"
    gravar_mensagem_evolution(
        instance=settings.evolution_assistente_instance,
        payload=_image_payload(
            caption="Foto do imóvel",
            remote_jid="+5500000000000@s.whatsapp.net",
            msg_id=msg_id,
        ),
    )
    row = _fetch_by_msg_id(msg_id)
    assert row is not None
    assert row["tipo_mensagem"] == "image"
    assert row["conteudo_texto"] == "Foto do imóvel"
    assert row["conteudo_metadata"]["mimetype"] == "image/jpeg"


# ---------------------------------------------------------------------------
# 5. Imagem sem caption
# ---------------------------------------------------------------------------

def test_grava_msg_imagem_sem_caption() -> None:
    msg_id = "TESTMSG-img-nocaption"
    gravar_mensagem_evolution(
        instance=settings.evolution_assistente_instance,
        payload=_image_payload(
            caption=None,
            remote_jid="+5500000000000@s.whatsapp.net",
            msg_id=msg_id,
        ),
    )
    row = _fetch_by_msg_id(msg_id)
    assert row is not None
    assert row["tipo_mensagem"] == "image"
    assert row["conteudo_texto"] is None


# ---------------------------------------------------------------------------
# 6. Idempotência — segundo INSERT não gera duplicata
# ---------------------------------------------------------------------------

def test_idempotencia() -> None:
    msg_id = "TESTMSG-idem"
    payload = _text_payload(
        remote_jid="+5500000000000@s.whatsapp.net",
        msg_id=msg_id,
    )
    gravar_mensagem_evolution(instance=settings.evolution_assistente_instance, payload=payload)
    gravar_mensagem_evolution(instance=settings.evolution_assistente_instance, payload=payload)

    with SessionLocal() as s:
        count = s.execute(
            text("SELECT COUNT(*) FROM mensagens_whatsapp WHERE evolution_message_id = :id"),
            {"id": msg_id},
        ).scalar()
    assert count == 1


# ---------------------------------------------------------------------------
# 7. Instance não mapeada → corretora_id=null + warning logado
# ---------------------------------------------------------------------------

def test_instance_nao_mapeada_grava_com_corretora_id_null() -> None:
    msg_id = "TESTMSG-unknown-instance"
    gravar_mensagem_evolution(
        instance="instancia_que_nao_existe_xyz",
        payload=_text_payload(
            remote_jid="+5500000000000@s.whatsapp.net",
            msg_id=msg_id,
        ),
    )
    with SessionLocal() as s:
        row = s.execute(
            text("SELECT corretora_id FROM mensagens_whatsapp WHERE evolution_message_id = :id"),
            {"id": msg_id},
        ).mappings().first()
    assert row is not None
    assert row["corretora_id"] is None


# ---------------------------------------------------------------------------
# 8. Falha no DB não quebra o webhook — responde 200 mesmo com erro
# ---------------------------------------------------------------------------

def test_falha_db_nao_quebra_webhook() -> None:
    client = TestClient(app)
    secret = settings.evolution_webhook_secret
    msg_id = "TESTMSG-db-fail"

    with patch(
        "app.services.mensagens_evolution_service.SessionLocal",
        side_effect=Exception("DB down"),
    ):
        r = client.post(
            f"/webhook/evolution/assistente/{secret}",
            json=_text_payload(
                remote_jid="+5500000000000@s.whatsapp.net",
                msg_id=msg_id,
                instance="acme_corretora_a",
            ),
        )
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# 9. Instance correta vem do payload, não do settings
# ---------------------------------------------------------------------------

def test_grava_msg_com_instance_correta_do_payload() -> None:
    msg_id = "TESTMSG-instance-payload"
    gravar_mensagem_evolution(
        instance="acme_corretor",
        payload=_text_payload(
            remote_jid="+5500000000000@s.whatsapp.net",
            msg_id=msg_id,
            instance="acme_corretor",
        ),
    )
    row = _fetch_by_msg_id(msg_id)
    assert row is not None
    assert row["instance"] == "acme_corretor"
    assert row["instance"] != settings.evolution_assistente_instance


# ---------------------------------------------------------------------------
# 10. Webhook com payload sem "instance" → não grava, retorna 200
# ---------------------------------------------------------------------------

def test_payload_sem_instance_nao_grava() -> None:
    client = TestClient(app)
    secret = settings.evolution_webhook_secret
    msg_id = "TESTMSG-no-instance"

    payload_sem_instance = {
        "event": "messages.upsert",
        # sem campo "instance"
        "data": {
            "key": {
                "remoteJid": "+5500000000000@s.whatsapp.net",
                "id": msg_id,
                "fromMe": False,
            },
            "messageTimestamp": 1715299200,
            "message": {"conversation": "sem instance"},
        },
    }
    r = client.post(f"/webhook/evolution/assistente/{secret}", json=payload_sem_instance)
    assert r.status_code == 200
    row = _fetch_by_msg_id(msg_id)
    assert row is None


# ---------------------------------------------------------------------------
# 11. Multi-instance: duas mensagens de instâncias diferentes são gravadas separadamente
# ---------------------------------------------------------------------------

def test_multi_instance_no_mesmo_servico() -> None:
    msg_id_corretora_a = "TESTMSG-multi-corretora_a"
    msg_id_corretor = "TESTMSG-multi-corretor"

    gravar_mensagem_evolution(
        instance="acme_corretora_a",
        payload=_text_payload(
            remote_jid="+5500000000000@s.whatsapp.net",
            msg_id=msg_id_corretora_a,
            instance="acme_corretora_a",
        ),
    )
    gravar_mensagem_evolution(
        instance="acme_corretor",
        payload=_text_payload(
            remote_jid="+5500000000000@s.whatsapp.net",
            msg_id=msg_id_corretor,
            instance="acme_corretor",
        ),
    )

    row_g = _fetch_by_msg_id(msg_id_corretora_a)
    row_c = _fetch_by_msg_id(msg_id_corretor)

    assert row_g is not None and row_g["instance"] == "acme_corretora_a"
    assert row_c is not None and row_c["instance"] == "acme_corretor"


# ---------------------------------------------------------------------------
# 12. templateMessage simples → tipo=text, conteudo_texto preenchido
# ---------------------------------------------------------------------------

def test_grava_template_simples_como_text() -> None:
    msg_id = "TESTMSG-tmpl-simple"
    gravar_mensagem_evolution(
        instance=settings.evolution_assistente_instance,
        payload=_payload(
            remote_jid="+5500000000000@s.whatsapp.net",
            msg_id=msg_id,
            msg={
                "templateMessage": {
                    "hydratedFourRowTemplate": {
                        "templateId": "000000000000000",
                        "hydratedTitleText": "",
                        "hydratedContentText": "Oi, aqui é a Acme Imóveis. Como posso ajudar?",
                        "hydratedButtons": [],
                    }
                }
            },
        ),
    )
    row = _fetch_by_msg_id(msg_id)
    assert row is not None
    assert row["tipo_mensagem"] == "text"
    assert row["conteudo_texto"] == "Oi, aqui é a Acme Imóveis. Como posso ajudar?"
    assert row["conteudo_metadata"]["tem_botoes"] is False
    assert row["conteudo_metadata"]["quantos_botoes"] == 0
    assert row["conteudo_metadata"]["template_id"] == "000000000000000"


# ---------------------------------------------------------------------------
# 13. templateMessage com botões → metadata.tem_botoes=True
# ---------------------------------------------------------------------------

def test_grava_template_com_botoes() -> None:
    msg_id = "TESTMSG-tmpl-buttons"
    gravar_mensagem_evolution(
        instance=settings.evolution_assistente_instance,
        payload=_payload(
            remote_jid="+5500000000000@s.whatsapp.net",
            msg_id=msg_id,
            msg={
                "templateMessage": {
                    "hydratedFourRowTemplate": {
                        "templateId": "tmpl-abc",
                        "hydratedTitleText": "",
                        "hydratedContentText": "Escolha uma opção:",
                        "hydratedButtons": [
                            {"index": 0, "quickReplyButton": {"id": "sim", "displayText": "Sim"}},
                            {"index": 1, "quickReplyButton": {"id": "nao", "displayText": "Não"}},
                        ],
                    }
                }
            },
        ),
    )
    row = _fetch_by_msg_id(msg_id)
    assert row is not None
    assert row["tipo_mensagem"] == "text"
    assert row["conteudo_texto"] == "Escolha uma opção:"
    meta = row["conteudo_metadata"]
    assert meta["tem_botoes"] is True
    assert meta["quantos_botoes"] == 2


# ---------------------------------------------------------------------------
# 14. templateMessage sem texto extraível → conteudo_texto=null, tipo=text
# ---------------------------------------------------------------------------

def test_template_sem_texto_extraivel() -> None:
    msg_id = "TESTMSG-tmpl-notext"
    gravar_mensagem_evolution(
        instance=settings.evolution_assistente_instance,
        payload=_payload(
            remote_jid="+5500000000000@s.whatsapp.net",
            msg_id=msg_id,
            msg={
                "templateMessage": {
                    "hydratedFourRowTemplate": {
                        "hydratedTitleText": "",
                        "hydratedContentText": "",
                        "hydratedButtons": [],
                    }
                }
            },
        ),
    )
    row = _fetch_by_msg_id(msg_id)
    assert row is not None
    assert row["tipo_mensagem"] == "text"
    assert row["conteudo_texto"] is None


# ---------------------------------------------------------------------------
# 14b. templateMessage do HSM Cloud (hydratedTemplate) → texto extraído (I3)
# ---------------------------------------------------------------------------
# Regressão da Revisão 03 · I3: a linha Cloud/Mac da Pessoa12 grava o corpo do HSM em
# `templateMessage.hydratedTemplate` (não `hydratedFourRowTemplate`). Antes do fix
# isso virava conteudo_texto=NULL → conversa "cega" → combustível do I1.

def test_template_hsm_cloud_hydrated_template() -> None:
    from app.services.mensagens_evolution_service import _extract_template

    texto, meta = _extract_template({
        "templateId": "000000000000000",
        "hydratedTemplate": {
            "templateId": "000000000000000",
            "hydratedTitleText": "",
            "hydratedContentText": "Oi, tudo bem? Aqui é Pessoa, da Acme Imóveis.",
            "hydratedButtons": [
                {"index": 0, "quickReplyButton": {"id": "Saiba Mais", "displayText": "Saiba Mais"}},
                {"index": 1, "quickReplyButton": {"id": "Parar", "displayText": "Parar de Receber"}},
            ],
        },
    })
    assert texto == "Oi, tudo bem? Aqui é Pessoa, da Acme Imóveis."
    assert meta["tem_botoes"] is True
    assert meta["quantos_botoes"] == 2
    assert meta["template_id"] == "000000000000000"


def test_template_hsm_cloud_grava_no_banco() -> None:
    msg_id = "TESTMSG-tmpl-hydrated-cloud"
    gravar_mensagem_evolution(
        instance=settings.evolution_assistente_instance,
        payload=_payload(
            remote_jid="+5500000000000@s.whatsapp.net",
            msg_id=msg_id,
            msg={
                "templateMessage": {
                    "hydratedTemplate": {
                        "templateId": "tmpl-cloud",
                        "hydratedTitleText": "",
                        "hydratedContentText": "Conseguiu ver minha mensagem sobre os apartamentos?",
                        "hydratedButtons": [],
                    }
                }
            },
        ),
    )
    row = _fetch_by_msg_id(msg_id)
    assert row is not None
    assert row["tipo_mensagem"] == "text"
    assert row["conteudo_texto"] == "Conseguiu ver minha mensagem sobre os apartamentos?"


# ---------------------------------------------------------------------------
# 15. Tipos não mapeados continuam como unknown
# ---------------------------------------------------------------------------

def test_tipos_nao_mapeados_ficam_unknown() -> None:
    unknown_types = [
        ("protocolMessage", {"protocolMessage": {"type": 0}}),
        ("contactMessage", {"contactMessage": {"displayName": "Fulano"}}),
        ("templateButtonReplyMessage", {"templateButtonReplyMessage": {"selectedId": "x"}}),
        ("albumMessage", {"albumMessage": {"expectedImageCount": 3}}),
    ]
    for i, (label, msg) in enumerate(unknown_types):
        msg_id = f"TESTMSG-unknown-{i}"
        gravar_mensagem_evolution(
            instance=settings.evolution_assistente_instance,
            payload=_payload(
                remote_jid=f"5500000000000{9020 + i}@s.whatsapp.net",
                msg_id=msg_id,
                msg=msg,
            ),
        )
        row = _fetch_by_msg_id(msg_id)
        assert row is not None, f"{label} não foi gravado"
        assert row["tipo_mensagem"] == "unknown", f"{label} deveria ser unknown, got {row['tipo_mensagem']}"
