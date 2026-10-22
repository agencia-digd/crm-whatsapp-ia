"""
Testes para EvolutionTool.send_to_self e EvolutionClient.send_text.

Unit tests: mockam client, DB e settings — sem chamadas reais.
Integration: @pytest.mark.integration — envia de verdade pra Pessoa3 (rodar manual).
"""
from unittest.mock import MagicMock, patch

import pytest

from app.integrations.evolution_client import EvolutionClient
from app.tools.evolution import EvolutionTool

# Dados de referência (Pessoa3)
_INSTANCE = "acme_corretora_a"
_PHONE = "+5500000000000"
_CORRETORA_ID = 3
_CORRETORA_INFO = {"id": _CORRETORA_ID, "phone": _PHONE}
_TEXT = "Teste do Copiloto. Pode ignorar."


# ── EvolutionClient.send_text ─────────────────────────────────────────────────

class TestEvolutionClientSendText:
    def test_send_text_posts_correct_payload(self) -> None:
        # A trava _blocked_under_pytest (COPILOTO_UNDER_PYTEST=1, setada no
        # conftest) intercepta send_text ANTES do _post — correto em qualquer
        # outro teste, mas aqui o alvo É o payload do _post. Desativa a trava
        # SÓ neste escopo; _post segue mockado, então nada sai pra rede.
        client = EvolutionClient(base_url="http://fake", api_key="test")
        fake_response = {"key": {"id": "FAKEMSGID", "fromMe": True}, "status": "PENDING"}
        with (
            patch("app.integrations.evolution_client._blocked_under_pytest",
                  return_value=False),
            patch.object(client, "_post", return_value=fake_response) as mock_post,
        ):
            result = client.send_text(_INSTANCE, _PHONE, _TEXT)
        mock_post.assert_called_once_with(
            f"/message/sendText/{_INSTANCE}",
            {"number": _PHONE, "text": _TEXT},
        )
        assert result["key"]["id"] == "FAKEMSGID"

    def test_send_text_bloqueado_sob_pytest_nao_chama_post(self) -> None:
        """Sob pytest (COPILOTO_UNDER_PYTEST=1 do conftest), send_text NUNCA
        chega no _post — devolve o sentinel BLOCKED_UNDER_PYTEST. É a trava
        que impede envio real de WhatsApp na suíte (incidentes 04/05 e 11/07)."""
        client = EvolutionClient(base_url="http://fake", api_key="test")
        with patch.object(client, "_post") as mock_post:
            result = client.send_text(_INSTANCE, _PHONE, _TEXT)
        mock_post.assert_not_called()
        assert result == {"status": "BLOCKED_UNDER_PYTEST", "key": None}

    def test_send_text_never_logs_content(self, caplog: pytest.LogCaptureFixture) -> None:
        """Garante que o conteúdo de 'text' não aparece em nenhum log."""
        client = EvolutionClient(base_url="http://fake", api_key="test")
        secret_text = "SEGREDO_NAO_DEVE_APARECER_NO_LOG"
        with patch.object(client, "_post", return_value={}):
            client.send_text(_INSTANCE, _PHONE, secret_text)
        for record in caplog.records:
            assert secret_text not in record.getMessage()


# ── EvolutionTool.send_to_self ────────────────────────────────────────────────

class TestSendToSelf:
    def _make_tool(self) -> EvolutionTool:
        tool = EvolutionTool()
        tool._client.send_text = MagicMock(return_value={"key": {"id": "FAKEID"}, "status": "PENDING"})
        return tool

    def test_dry_run_does_not_call_api(self) -> None:
        tool = self._make_tool()
        with (
            patch.object(tool, "_get_corretora_info", return_value=_CORRETORA_INFO),
            patch.object(tool, "_get_instance_owner_phone", return_value=_PHONE),
            patch.object(tool, "_record_briefing"),
            patch("app.tools.evolution.settings") as mock_s,
        ):
            mock_s.dry_run = True
            result = tool.send_to_self(_INSTANCE, _TEXT)

        assert result is True
        tool._client.send_text.assert_not_called()

    def test_dry_run_records_in_briefings_sent(self) -> None:
        tool = self._make_tool()
        with (
            patch.object(tool, "_get_corretora_info", return_value=_CORRETORA_INFO),
            patch.object(tool, "_get_instance_owner_phone", return_value=_PHONE),
            patch.object(tool, "_record_briefing") as mock_record,
            patch("app.tools.evolution.settings") as mock_s,
        ):
            mock_s.dry_run = True
            tool.send_to_self(_INSTANCE, _TEXT)

        mock_record.assert_called_once_with(_CORRETORA_ID, _TEXT, success=True)

    def test_blocks_if_instance_not_found_in_db(self) -> None:
        tool = self._make_tool()
        with patch.object(tool, "_get_corretora_info", return_value=None):
            result = tool.send_to_self("instancia_inexistente", _TEXT)

        assert result is False
        tool._client.send_text.assert_not_called()

    def test_blocks_if_phone_mismatch(self) -> None:
        """Corretora no banco tem número diferente do ownerJid da instância."""
        tool = self._make_tool()
        wrong_owner = "+5500000000000"
        with (
            patch.object(tool, "_get_corretora_info", return_value=_CORRETORA_INFO),
            patch.object(tool, "_get_instance_owner_phone", return_value=wrong_owner),
            patch.object(tool, "_record_briefing") as mock_record,
        ):
            result = tool.send_to_self(_INSTANCE, _TEXT)

        assert result is False
        tool._client.send_text.assert_not_called()
        mock_record.assert_called_once_with(_CORRETORA_ID, _TEXT, success=False)

    def test_blocks_if_owner_phone_not_found(self) -> None:
        """fetchInstances não retorna ownerJid para a instância."""
        tool = self._make_tool()
        with (
            patch.object(tool, "_get_corretora_info", return_value=_CORRETORA_INFO),
            patch.object(tool, "_get_instance_owner_phone", return_value=None),
            patch.object(tool, "_record_briefing") as mock_record,
        ):
            result = tool.send_to_self(_INSTANCE, _TEXT)

        assert result is False
        tool._client.send_text.assert_not_called()
        mock_record.assert_called_once_with(_CORRETORA_ID, _TEXT, success=False)

    def test_real_call_correct_payload(self) -> None:
        """Com dry_run=False e números batendo, chama send_text com args corretos."""
        tool = self._make_tool()
        with (
            patch.object(tool, "_get_corretora_info", return_value=_CORRETORA_INFO),
            patch.object(tool, "_get_instance_owner_phone", return_value=_PHONE),
            patch.object(tool, "_record_briefing"),
            patch("app.tools.evolution.settings") as mock_s,
        ):
            mock_s.dry_run = False
            result = tool.send_to_self(_INSTANCE, _TEXT)

        assert result is True
        tool._client.send_text.assert_called_once_with(_INSTANCE, _PHONE, _TEXT)

    def test_real_call_records_success(self) -> None:
        tool = self._make_tool()
        with (
            patch.object(tool, "_get_corretora_info", return_value=_CORRETORA_INFO),
            patch.object(tool, "_get_instance_owner_phone", return_value=_PHONE),
            patch.object(tool, "_record_briefing") as mock_record,
            patch("app.tools.evolution.settings") as mock_s,
        ):
            mock_s.dry_run = False
            tool.send_to_self(_INSTANCE, _TEXT)

        mock_record.assert_called_once_with(_CORRETORA_ID, _TEXT, success=True)

    def test_real_call_records_failure_on_api_error(self) -> None:
        """Quando a API lança exceção, registra success=False e retorna False."""
        tool = self._make_tool()
        tool._client.send_text = MagicMock(side_effect=Exception("API down"))
        with (
            patch.object(tool, "_get_corretora_info", return_value=_CORRETORA_INFO),
            patch.object(tool, "_get_instance_owner_phone", return_value=_PHONE),
            patch.object(tool, "_record_briefing") as mock_record,
            patch("app.tools.evolution.settings") as mock_s,
        ):
            mock_s.dry_run = False
            result = tool.send_to_self(_INSTANCE, _TEXT)

        assert result is False
        mock_record.assert_called_once_with(_CORRETORA_ID, _TEXT, success=False)


# ── Integration tests ─────────────────────────────────────────────────────────

@pytest.mark.integration
class TestEvolutionSendIntegration:
    """Envia mensagem real pra Pessoa3 — rodar apenas manual: pytest -m integration"""

    def test_send_to_self_dry_run(self) -> None:
        """Verifica o fluxo completo em dry-run com DB e Evolution reais."""
        from app.config import settings
        assert settings.dry_run is True, "Este teste requer DRY_RUN=true no .env"

        tool = EvolutionTool()
        result = tool.send_to_self(_INSTANCE, "[Copiloto] Teste dry-run integração.")
        assert result is True

    def test_send_to_self_real_message(self) -> None:
        """Envia mensagem real pra Pessoa3. Confirmar manualmente no WhatsApp.

        Pulado por padrão. Rodar explicitamente:
            pytest tests/test_evolution_send.py::TestEvolutionSendIntegration::test_send_to_self_real_message --run-real-sends
        """
        pytest.skip("envia mensagem real — rodar só com --run-real-sends")
