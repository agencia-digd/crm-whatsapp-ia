"""
Testes da EvolutionTool e EvolutionClient.

Mocks: cenários de normalização sem chamar API real.
Integration: marcado com @pytest.mark.integration — chama acme_corretora_a real.
"""
import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.integrations.evolution_client import EvolutionClient
from app.tools.evolution import (
    EvolutionTool,
    _VALID_INDIVIDUAL_SUFFIXES,
    _normalize_message,
    _phone_from_jid,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_raw_msg(
    from_me: bool = False,
    remote_jid: str = "+5500000000000@s.whatsapp.net",
    msg_type: str = "conversation",
    text: str = "Olá",
    ts: int = 1700000000,
    msg_id: str = "FAKEID",
) -> dict:
    message: dict = {}
    if msg_type == "conversation":
        message = {"conversation": text}
    elif msg_type == "extendedTextMessage":
        message = {"extendedTextMessage": {"text": text}}
    elif msg_type == "imageMessage":
        message = {"imageMessage": {"caption": text}}
    elif msg_type == "audioMessage":
        message = {"audioMessage": {}}
    elif msg_type == "documentMessage":
        message = {"documentMessage": {"caption": text}}
    elif msg_type == "stickerMessage":
        message = {"stickerMessage": {}}
    elif msg_type == "reactionMessage":
        message = {"reactionMessage": {}}

    return {
        "key": {"remoteJid": remote_jid, "fromMe": from_me, "id": msg_id},
        "messageType": msg_type,
        "messageTimestamp": ts,
        "message": message,
        "pushName": "Contato Teste",
        "MessageUpdate": [],
    }


# ── Testes de normalização (_normalize_message) ───────────────────────────────

class TestNormalizeMessage:
    def test_from_me_true_vira_corretora(self):
        raw = _make_raw_msg(from_me=True)
        result = _normalize_message(raw)
        assert result is not None
        assert result["from"] == "corretora"

    def test_from_me_false_vira_lead(self):
        raw = _make_raw_msg(from_me=False)
        result = _normalize_message(raw)
        assert result is not None
        assert result["from"] == "lead"

    def test_grupo_filtrado(self):
        raw = _make_raw_msg(remote_jid="contato@example.com")
        result = _normalize_message(raw)
        assert result is None

    def test_sticker_filtrado(self):
        raw = _make_raw_msg(msg_type="stickerMessage")
        result = _normalize_message(raw)
        assert result is None

    def test_reaction_filtrado(self):
        raw = _make_raw_msg(msg_type="reactionMessage")
        result = _normalize_message(raw)
        assert result is None

    def test_conversation_tipo_text(self):
        raw = _make_raw_msg(msg_type="conversation", text="Oi")
        result = _normalize_message(raw)
        assert result is not None
        assert result["type"] == "text"
        assert result["text"] == "Oi"

    def test_audio_tipo_correto(self):
        raw = _make_raw_msg(msg_type="audioMessage")
        result = _normalize_message(raw)
        assert result is not None
        assert result["type"] == "audio"

    def test_imagem_tipo_correto(self):
        raw = _make_raw_msg(msg_type="imageMessage", text="legenda aqui")
        result = _normalize_message(raw)
        assert result is not None
        assert result["type"] == "image"
        assert result["text"] == "legenda aqui"

    def test_documento_tipo_correto(self):
        raw = _make_raw_msg(msg_type="documentMessage", text="")
        result = _normalize_message(raw)
        assert result is not None
        assert result["type"] == "document"

    def test_mensagem_deletada_sem_payload(self):
        raw = _make_raw_msg()
        raw["message"] = {}
        result = _normalize_message(raw)
        assert result is not None
        assert result["type"] == "deleted"
        assert "[mensagem deletada]" in result["text"]

    def test_timestamp_iso_utc(self):
        raw = _make_raw_msg(ts=1700000000)
        result = _normalize_message(raw)
        assert result is not None
        assert "2023" in result["at"]
        assert result["at"].endswith("+00:00")

    def test_extended_text_extraido(self):
        raw = _make_raw_msg(msg_type="extendedTextMessage", text="texto longo")
        result = _normalize_message(raw)
        assert result is not None
        assert result["text"] == "texto longo"


class TestPhoneFromJid:
    def test_extrai_numero(self):
        assert _phone_from_jid("+5500000000000@s.whatsapp.net") == "+5500000000000"

    def test_jid_sem_sufixo(self):
        assert _phone_from_jid("5500000000000") == "5500000000000"


# ── Testes do EvolutionClient (mock httpx) ────────────────────────────────────

class TestEvolutionClientMocked:
    def test_find_messages_retorna_records(self):
        fake_response = {
            "messages": {
                "total": 2,
                "pages": 1,
                "records": [
                    _make_raw_msg(from_me=True),
                    _make_raw_msg(from_me=False),
                ],
            }
        }
        client = EvolutionClient(base_url="http://fake", api_key="test")
        with patch.object(client, "_post", return_value=fake_response):
            msgs = client.find_messages("test_instance", "+5500000000000@s.whatsapp.net")
        assert len(msgs) == 2

    def test_get_instance_status_retorna_estado(self):
        client = EvolutionClient(base_url="http://fake", api_key="test")
        fake = {"instance": {"instanceName": "test", "state": "open"}}
        with patch.object(client, "_get", return_value=fake):
            status = client.get_instance_status("test")
        assert status == "open"

    def test_find_chats_filtra_por_since(self):
        client = EvolutionClient(base_url="http://fake", api_key="test")
        chats = [
            {"remoteJid": "111@s.whatsapp.net", "updatedAt": "2020-01-01T00:00:00.000Z",
             "unreadCount": 0, "pushName": "A"},
            {"remoteJid": "222@s.whatsapp.net", "updatedAt": "2099-12-31T00:00:00.000Z",
             "unreadCount": 1, "pushName": "B"},
        ]
        with patch.object(client, "_post", return_value=chats):
            since = datetime(2026, 1, 1, tzinfo=timezone.utc)
            result = client.find_chats("test", since=since)
        assert len(result) == 1
        assert result[0]["remoteJid"] == "222@s.whatsapp.net"


# ── Testes da EvolutionTool (mock do client) ──────────────────────────────────

class TestEvolutionToolMocked:
    def _tool_with_mock(self, method: str, return_value: object) -> EvolutionTool:
        tool = EvolutionTool()
        setattr(tool._client, method, MagicMock(return_value=return_value))
        return tool

    def test_check_instance_status(self):
        tool = self._tool_with_mock("get_instance_status", "open")
        assert tool.check_instance_status("acme_corretora_a") == "open"

    def test_list_recent_chats_normaliza(self):
        raw_chats = [
            {
                "remoteJid": "+5500000000000@s.whatsapp.net",
                "pushName": "João",
                "updatedAt": "2099-12-31T00:00:00.000Z",
                "unreadCount": 3,
                "lastMessage": {"messageTimestamp": 1700000000, "key": {"fromMe": False}},
            },
            # Grupo deve ser filtrado
            {
                "remoteJid": "contato@example.com",
                "pushName": "Grupo Acme",
                "updatedAt": "2099-12-31T00:00:00.000Z",
                "unreadCount": 0,
                "lastMessage": None,
            },
        ]
        tool = EvolutionTool()
        with patch.object(tool._client, "find_chats", return_value=raw_chats):
            result = tool.list_recent_chats("acme_corretora_a", since_hours=24)
        assert len(result) == 1
        assert result[0]["phone"] == "+5500000000000"
        assert result[0]["unread_count"] == 3

    def test_get_conversation_ordena_cronologicamente(self):
        raw_msgs = [
            _make_raw_msg(from_me=False, ts=1700000100, msg_id="m1"),
            _make_raw_msg(from_me=True,  ts=1700000050, msg_id="m2"),
            _make_raw_msg(from_me=False, ts=1700000200, msg_id="m3"),
        ]
        tool = EvolutionTool()
        # _find_jid_by_phone → None: sem split @lid, exercita só ordenação
        with patch.object(tool._client, "find_messages", return_value=raw_msgs), \
             patch.object(tool, "_find_jid_by_phone", return_value=None):
            result = tool.get_conversation("acme_corretora_a", "+5500000000000", hours_back=99999)
        assert len(result) == 3
        assert result[0]["at"] < result[1]["at"] < result[2]["at"]

    def test_get_conversation_filtra_grupos(self):
        raw_msgs = [
            _make_raw_msg(remote_jid="contato@example.com", from_me=False, ts=1700000100, msg_id="g1"),
            _make_raw_msg(from_me=True, ts=1700000050, msg_id="m1"),
        ]
        tool = EvolutionTool()
        with patch.object(tool._client, "find_messages", return_value=raw_msgs), \
             patch.object(tool, "_find_jid_by_phone", return_value=None):
            result = tool.get_conversation("acme_corretora_a", "+5500000000000", hours_back=99999)
        assert len(result) == 1
        assert result[0]["from"] == "corretora"

    def test_get_conversation_normaliza_jid(self):
        """Telefone sem @s.whatsapp.net deve ser normalizado antes da chamada."""
        tool = EvolutionTool()
        captured_jid: list[str] = []

        def capture_jid(instance: str, jid: str, **kwargs: object) -> list:
            captured_jid.append(jid)
            return []

        # find_chats mockado para evitar chamada real na busca @lid fallback
        with patch.object(tool._client, "find_messages", side_effect=capture_jid), \
             patch.object(tool._client, "find_chats", return_value=[]):
            tool.get_conversation("acme_corretora_a", "+5500000000000", hours_back=1)

        assert captured_jid[0] == "+5500000000000@s.whatsapp.net"

    def test_list_recent_chats_includes_lid(self):
        """Chats @lid devem ser retornados junto com @s.whatsapp.net."""
        raw_chats = [
            {
                "remoteJid": "+5500000000000@s.whatsapp.net",
                "pushName": "Clássico",
                "updatedAt": "2099-12-31T00:00:00.000Z",
                "unreadCount": 1,
                "lastMessage": {"messageTimestamp": 1700000000, "key": {"fromMe": False}},
            },
            {
                "remoteJid": "000000000000000@lid",
                "pushName": "Regina Nunes",
                "updatedAt": "2099-12-31T00:00:00.000Z",
                "unreadCount": 0,
                "lastMessage": {
                    "messageTimestamp": 1700000100,
                    "key": {
                        "fromMe": True,
                        "remoteJid": "000000000000000@lid",
                        "remoteJidAlt": "+5500000000000@s.whatsapp.net",
                    },
                },
            },
            {
                "remoteJid": "contato@example.com",
                "pushName": "Grupo Acme",
                "updatedAt": "2099-12-31T00:00:00.000Z",
                "unreadCount": 0,
                "lastMessage": None,
            },
        ]
        tool = EvolutionTool()
        with patch.object(tool._client, "find_chats", return_value=raw_chats):
            result = tool.list_recent_chats("acme_corretora_a", since_hours=24)

        assert len(result) == 2  # clássico + lid; grupo excluído
        jids = {r["remote_jid"] for r in result}
        assert "+5500000000000@s.whatsapp.net" in jids
        assert "000000000000000@lid" in jids

    def test_list_recent_chats_excludes_groups(self):
        """Grupos @g.us nunca entram no resultado, mesmo com @lid presentes."""
        raw_chats = [
            {
                "remoteJid": "contato@example.com",
                "pushName": "Grupo",
                "updatedAt": "2099-12-31T00:00:00.000Z",
                "unreadCount": 0,
                "lastMessage": None,
            },
            {
                "remoteJid": "999888777@lid",
                "pushName": "Individual",
                "updatedAt": "2099-12-31T00:00:00.000Z",
                "unreadCount": 0,
                "lastMessage": {"messageTimestamp": 1700000000, "key": {}},
            },
        ]
        tool = EvolutionTool()
        with patch.object(tool._client, "find_chats", return_value=raw_chats):
            result = tool.list_recent_chats("acme_corretora_a", since_hours=24)
        assert len(result) == 1
        assert result[0]["remote_jid"] == "999888777@lid"

    def test_list_recent_chats_phone_from_remote_jid_alt(self):
        """Para @lid, o campo 'phone' deve ser extraído de remoteJidAlt."""
        raw_chats = [
            {
                "remoteJid": "000000000000000@lid",
                "pushName": "Regina",
                "updatedAt": "2099-12-31T00:00:00.000Z",
                "unreadCount": 0,
                "lastMessage": {
                    "messageTimestamp": 1700000000,
                    "key": {
                        "fromMe": True,
                        "remoteJidAlt": "+5500000000000@s.whatsapp.net",
                    },
                },
            },
        ]
        tool = EvolutionTool()
        with patch.object(tool._client, "find_chats", return_value=raw_chats):
            result = tool.list_recent_chats("acme_corretora_a", since_hours=24)
        assert len(result) == 1
        assert result[0]["phone"] == "+5500000000000"

    def test_get_conversation_accepts_lid_jid_directly(self):
        """JID @lid passado diretamente não deve ser reescrito para @s.whatsapp.net."""
        tool = EvolutionTool()
        captured_jid: list[str] = []

        def capture(instance: str, jid: str, **kwargs: object) -> list:
            captured_jid.append(jid)
            return [_make_raw_msg(from_me=False, ts=9999999999)]

        with patch.object(tool._client, "find_messages", side_effect=capture):
            tool.get_conversation("acme_corretora_a", "000000000000000@lid", hours_back=99999)

        assert captured_jid[0] == "000000000000000@lid"

    def test_get_conversation_falls_back_to_lid(self):
        """Quando @s.whatsapp.net retorna vazio, deve tentar @lid via _find_jid_by_phone."""
        lid_jid = "000000000000000@lid"
        msgs_by_jid = {
            "+5500000000000@s.whatsapp.net": [],
            lid_jid: [_make_raw_msg(from_me=False, ts=9999999999)],
        }

        tool = EvolutionTool()

        def find_msgs(instance: str, jid: str, **kwargs: object) -> list:
            return msgs_by_jid.get(jid, [])

        with patch.object(tool._client, "find_messages", side_effect=find_msgs), \
             patch.object(tool, "_find_jid_by_phone", return_value=lid_jid):
            result = tool.get_conversation("acme_corretora_a", "+5500000000000", hours_back=99999)

        assert len(result) == 1
        assert result[0]["from"] == "lead"

    def test_get_conversation_merges_classic_and_lid(self):
        """Conversa PARTIDA entre @s.whatsapp.net e @lid: a leitura junta os dois.

        No Multi-Device o mesmo lead costuma ter parte das mensagens num JID e
        parte no outro — tipicamente as respostas da corretora (fromMe) caem no
        @lid. Ler só o JID clássico faz essas mensagens sumirem da thread
        (reclamação da corretora "não carrega o que EU mandei"). O fix lê ambos.
        """
        lid_jid = "000000000000000@lid"
        msgs_by_jid = {
            "+5500000000000@s.whatsapp.net": [
                _make_raw_msg(from_me=False, ts=1700000100, msg_id="lead-1"),
            ],
            lid_jid: [
                _make_raw_msg(from_me=True, ts=1700000200, remote_jid=lid_jid, msg_id="cor-1"),
                _make_raw_msg(from_me=True, ts=1700000300, remote_jid=lid_jid, msg_id="cor-2"),
            ],
        }
        tool = EvolutionTool()

        def find_msgs(instance: str, jid: str, **kwargs: object) -> list:
            return msgs_by_jid.get(jid, [])

        with patch.object(tool._client, "find_messages", side_effect=find_msgs), \
             patch.object(tool, "_find_jid_by_phone", return_value=lid_jid):
            result = tool.get_conversation("acme_corretora_a", "+5500000000000", hours_back=99999)

        # 1 do lead (@s) + 2 da corretora (@lid), sem perder nenhuma
        assert len(result) == 3
        assert sum(1 for m in result if m["from"] == "corretora") == 2
        assert sum(1 for m in result if m["from"] == "lead") == 1

    def test_get_conversation_dedup_por_id(self):
        """Mesma mensagem devolvida sob os dois JIDs não é contada em dobro."""
        lid_jid = "000000000000000@lid"
        dup = _make_raw_msg(from_me=True, ts=1700000200, msg_id="dup-1")
        msgs_by_jid = {
            "+5500000000000@s.whatsapp.net": [dup],
            lid_jid: [dict(dup)],  # mesma id sob o outro JID
        }
        tool = EvolutionTool()

        def find_msgs(instance: str, jid: str, **kwargs: object) -> list:
            return msgs_by_jid.get(jid, [])

        with patch.object(tool._client, "find_messages", side_effect=find_msgs), \
             patch.object(tool, "_find_jid_by_phone", return_value=lid_jid):
            result = tool.get_conversation("acme_corretora_a", "+5500000000000", hours_back=99999)

        assert len(result) == 1

    def test_list_recent_chats_dedup_por_jid(self):
        """Mesmo remote_jid retornado duas vezes → mantém apenas o entry mais recente."""
        raw_chats = [
            {
                "remoteJid": "+5500000000000@s.whatsapp.net",
                "pushName": "João v1",
                "updatedAt": "2099-12-31T00:00:00.000Z",
                "unreadCount": 1,
                # timestamp 1700000100 < 1700000200
                "lastMessage": {"messageTimestamp": 1700000100, "key": {}},
            },
            {
                "remoteJid": "+5500000000000@s.whatsapp.net",  # mesmo JID
                "pushName": "João v2",
                "updatedAt": "2099-12-31T01:00:00.000Z",
                "unreadCount": 2,
                "lastMessage": {"messageTimestamp": 1700000200, "key": {}},  # mais recente
            },
        ]
        tool = EvolutionTool()
        with patch.object(tool._client, "find_chats", return_value=raw_chats):
            result = tool.list_recent_chats("acme_test", since_hours=24)

        assert len(result) == 1  # deduplicado
        # Mantém o entry com last_msg_at mais recente (timestamp 1700000200)
        from datetime import datetime, timezone
        expected_at = datetime.fromtimestamp(1700000200, tz=timezone.utc).isoformat()
        assert result[0]["last_msg_at"] == expected_at


# ── Testes de _find_jid_by_phone ─────────────────────────────────────────────

class TestFindJidByPhone:
    """Testa a resolução de JID por telefone (clássico e @lid)."""

    def _make_lid_chat(self, lid_id: str, phone_digits: str) -> dict:
        return {
            "remoteJid": f"{lid_id}@lid",
            "pushName": "Contato",
            "updatedAt": "2026-05-04T12:00:00.000Z",
            "lastMessage": {
                "messageTimestamp": 1746360000,
                "key": {
                    "fromMe": True,
                    "remoteJid": f"{lid_id}@lid",
                    "remoteJidAlt": f"{phone_digits}@s.whatsapp.net",
                },
            },
        }

    def _make_classic_chat(self, phone_digits: str) -> dict:
        return {
            "remoteJid": f"{phone_digits}@s.whatsapp.net",
            "pushName": "Contato Clássico",
            "updatedAt": "2026-05-04T12:00:00.000Z",
            "lastMessage": {"messageTimestamp": 1746360000, "key": {"fromMe": False}},
        }

    def test_returns_classic_when_present(self):
        chats = [self._make_classic_chat("+5500000000000")]
        tool = EvolutionTool()
        with patch.object(tool._client, "find_chats", return_value=chats):
            result = tool._find_jid_by_phone("acme_corretora_a", "+5500000000000")
        assert result == "+5500000000000@s.whatsapp.net"

    def test_returns_lid_when_classic_missing(self):
        chats = [self._make_lid_chat("000000000000000", "+5500000000000")]
        tool = EvolutionTool()
        with patch.object(tool._client, "find_chats", return_value=chats):
            result = tool._find_jid_by_phone("acme_corretora_a", "+5500000000000")
        assert result == "000000000000000@lid"

    def test_returns_none_when_not_found(self):
        chats = [self._make_lid_chat("999000111", "+000000000000000")]
        tool = EvolutionTool()
        with patch.object(tool._client, "find_chats", return_value=chats):
            result = tool._find_jid_by_phone("acme_corretora_a", "+5500000000000")
        assert result is None

    def test_returns_none_on_api_error(self):
        tool = EvolutionTool()
        with patch.object(tool._client, "find_chats", side_effect=Exception("timeout")):
            result = tool._find_jid_by_phone("acme_corretora_a", "+5500000000000")
        assert result is None

    def test_phone_normalization_handles_plus_prefix(self):
        """Verifica que o caller normalizou corretamente — dígitos sem + ou espaços."""
        chats = [self._make_classic_chat("+5500000000000")]
        tool = EvolutionTool()
        with patch.object(tool._client, "find_chats", return_value=chats):
            # digits já normalizados pelo caller (get_conversation)
            result = tool._find_jid_by_phone("acme_corretora_a", "+5500000000000")
        assert result == "+5500000000000@s.whatsapp.net"


# ── Testes da constante _VALID_INDIVIDUAL_SUFFIXES ────────────────────────────

class TestValidIndividualSuffixes:
    def test_contains_classic_and_lid(self):
        assert "@s.whatsapp.net" in _VALID_INDIVIDUAL_SUFFIXES
        assert "@lid" in _VALID_INDIVIDUAL_SUFFIXES

    def test_does_not_contain_groups(self):
        assert not any(s == "@g.us" for s in _VALID_INDIVIDUAL_SUFFIXES)


# ── Integration tests ─────────────────────────────────────────────────────────

@pytest.mark.integration
class TestEvolutionIntegration:
    """Chama a instância acme_corretora_a real — requer Evolution API rodando."""

    def test_instance_open(self):
        from app.config import settings
        client = EvolutionClient(
            base_url=settings.evolution_api_url,
            api_key=settings.evolution_api_key,
        )
        status = client.get_instance_status("acme_corretora_a")
        assert status == "open", f"Esperado 'open', got '{status}'"

    def test_fetch_instances_nao_vazio(self):
        from app.config import settings
        client = EvolutionClient(
            base_url=settings.evolution_api_url,
            api_key=settings.evolution_api_key,
        )
        instances = client.fetch_instances()
        assert isinstance(instances, list)
        assert len(instances) > 0
        names = [i.get("name") or i.get("instanceName") for i in instances]
        assert "acme_corretora_a" in names
