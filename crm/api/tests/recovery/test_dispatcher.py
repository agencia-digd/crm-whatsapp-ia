"""
Testes do dispatcher — sem rede, sem DB.

Mocka _fetch_leads_entrada + repo + Kommo + HTTP. Foco em:
  - leads sem telefone são puladas
  - leads já em recovery não duplicam
  - payload do HSM tem template correto + parâmetro de nome
  - DRY_RUN não chama httpx
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.recovery import dispatcher as disp


def _lead(lid: str, phone: str = "+5500000000000", name: str = "João Silva") -> dict:
    return {"lead_id": lid, "nome": name, "telefone": phone}


@pytest.fixture
def settings_ok(monkeypatch):
    """Settings mínimos pro dispatcher passar _check_config em dry-run."""
    monkeypatch.setattr(disp.settings, "kommo_pipeline_id_remarketing", 11305423)
    monkeypatch.setattr(disp.settings, "kommo_stage_remarketing_entrada", 86770147)
    monkeypatch.setattr(disp.settings, "kommo_stage_remarketing_api", 90464747)
    monkeypatch.setattr(disp.settings, "recovery_dispatch_delay_ms", 0)
    monkeypatch.setattr(disp.settings, "recovery_hsm_template_name", "reativacao_acme_v1")
    monkeypatch.setattr(disp.settings, "recovery_hsm_language", "pt_BR")
    monkeypatch.setattr(disp.settings, "recovery_hsm_has_name_param", True)
    monkeypatch.setattr(disp.settings, "meta_cloud_phone_number_id", "123")
    monkeypatch.setattr(disp.settings, "meta_cloud_access_token", "tok")
    monkeypatch.setattr(disp.settings, "meta_cloud_api_version", "v22.0")


def test_dispatch_dry_run_no_real_calls(settings_ok, monkeypatch):
    monkeypatch.setattr(disp, "_fetch_leads_entrada", lambda limit: [_lead("L1"), _lead("L2")])

    fake_session = MagicMock()
    fake_session.__enter__ = lambda s: fake_session
    fake_session.__exit__ = lambda *a: None
    monkeypatch.setattr(disp, "SessionLocal", lambda: fake_session)

    monkeypatch.setattr(disp.repo, "find_by_phone", lambda *a, **k: None)

    mock_row = MagicMock(hsm_sent_at=None)
    monkeypatch.setattr(disp.repo, "get_or_create", lambda *a, **k: mock_row)

    monkeypatch.setattr(disp.repo, "claim_new", lambda *a, **k: True)
    move_calls = []
    monkeypatch.setattr(disp, "_move_to_api_stage",
                        lambda lead_id, dry: move_calls.append((lead_id, dry)))

    # httpx.Client deve NUNCA ser chamado em dry-run
    with patch("httpx.Client") as mock_client:
        result = disp.dispatch_wave(limit=10, dry_run=True)
        mock_client.assert_not_called()

    assert result.total_candidates == 2
    assert result.sent == 2
    assert result.failed == 0
    assert len(move_calls) == 2


def test_skip_leads_without_phone(settings_ok, monkeypatch):
    monkeypatch.setattr(disp, "_fetch_leads_entrada",
                        lambda limit: [_lead("L1", phone=""), _lead("L2", phone="+5500000000000")])

    fake_session = MagicMock()
    fake_session.__enter__ = lambda s: fake_session
    fake_session.__exit__ = lambda *a: None
    monkeypatch.setattr(disp, "SessionLocal", lambda: fake_session)
    monkeypatch.setattr(disp.repo, "find_by_phone", lambda *a, **k: None)
    monkeypatch.setattr(disp.repo, "get_or_create", lambda *a, **k: MagicMock())
    monkeypatch.setattr(disp.repo, "claim_new", lambda *a, **k: True)
    monkeypatch.setattr(disp, "_move_to_api_stage", lambda *a, **k: None)

    result = disp.dispatch_wave(limit=10, dry_run=True)
    assert result.skipped_no_phone == 1
    assert result.sent == 1


def test_skip_leads_already_in_recovery(settings_ok, monkeypatch):
    monkeypatch.setattr(disp, "_fetch_leads_entrada", lambda limit: [_lead("L1")])

    fake_session = MagicMock()
    fake_session.__enter__ = lambda s: fake_session
    fake_session.__exit__ = lambda *a: None
    monkeypatch.setattr(disp, "SessionLocal", lambda: fake_session)

    existing = MagicMock(state="qualificando")
    monkeypatch.setattr(disp.repo, "find_by_phone", lambda *a, **k: existing)
    monkeypatch.setattr(disp, "_move_to_api_stage", lambda *a, **k: None)

    result = disp.dispatch_wave(limit=10, dry_run=True)
    assert result.already_in_recovery == 1
    assert result.sent == 0


def test_hsm_payload_includes_first_name(settings_ok, monkeypatch):
    """Quando has_name_param=True, payload deve ter components com {{1}}=primeiro nome."""
    captured = {}

    class FakeResp:
        status_code = 200
        text = ""

        def json(self):
            return {"messages": [{"id": "wamid.xxx"}]}

    class FakeClient:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass

        def post(self, url, headers=None, json=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["body"] = json
            return FakeResp()

    with patch("httpx.Client", FakeClient):
        ok = disp._send_hsm("+5500000000000", "João Silva", dry=False)

    assert ok is True
    assert captured["body"]["to"] == "+5500000000000"
    assert captured["body"]["template"]["name"] == "reativacao_acme_v1"
    assert captured["body"]["template"]["language"] == {"code": "pt_BR"}
    components = captured["body"]["template"]["components"]
    assert components[0]["parameters"][0]["text"] == "João"
    assert "Bearer tok" in captured["headers"]["Authorization"]


def test_hsm_no_name_param_when_disabled(settings_ok, monkeypatch):
    monkeypatch.setattr(disp.settings, "recovery_hsm_has_name_param", False)
    captured = {}

    class FakeResp:
        status_code = 200
        text = ""
        def json(self): return {}

    class FakeClient:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def post(self, url, headers=None, json=None):
            captured["body"] = json
            return FakeResp()

    with patch("httpx.Client", FakeClient):
        disp._send_hsm("+5500000000000", "Pessoa", dry=False)

    assert "components" not in captured["body"]["template"]


def test_hsm_fails_on_400(settings_ok):
    class FakeResp:
        status_code = 400
        text = '{"error":"invalid template"}'
        def json(self): return {}

    class FakeClient:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def post(self, *a, **k): return FakeResp()

    with patch("httpx.Client", FakeClient):
        ok = disp._send_hsm("+5500000000000", "Corretor", dry=False)
    assert ok is False


def test_first_name_helper():
    assert disp._first_name("João Silva") == "João"
    assert disp._first_name(" Pessoa  do Carmo ") == "Pessoa"
    assert disp._first_name(None) is None
    assert disp._first_name("") is None
    assert disp._first_name("   ") is None


def test_digits_only_strips_non_digits():
    assert disp._digits_only("+5500000000000") == "+5500000000000"
    assert disp._digits_only("abc") == ""


def test_move_to_api_stage_dry_nao_toca_kommo(monkeypatch):
    """Codex #4: em dry-run, _move_to_api_stage NÃO pode mover o lead no Kommo
    (antes o parâmetro dry era ignorado e o funil mudava numa simulação)."""
    fake_kommo = MagicMock()
    monkeypatch.setattr(disp, "KommoTool", lambda: fake_kommo)
    disp._move_to_api_stage("L1", dry=True)
    fake_kommo.move_lead_stage.assert_not_called()


def test_move_to_api_stage_live_move_kommo(settings_ok, monkeypatch):
    """Fora do dry-run, _move_to_api_stage move o lead de verdade."""
    fake_kommo = MagicMock()
    monkeypatch.setattr(disp, "KommoTool", lambda: fake_kommo)
    disp._move_to_api_stage("L1", dry=False)
    fake_kommo.move_lead_stage.assert_called_once()


def test_check_config_missing_kommo():
    with patch.object(disp.settings, "kommo_pipeline_id_remarketing", 0):
        with pytest.raises(RuntimeError, match="kommo_pipeline_id_remarketing"):
            disp._check_config(dry=True)


def test_check_config_dry_skips_meta_validation(monkeypatch):
    monkeypatch.setattr(disp.settings, "kommo_pipeline_id_remarketing", 1)
    monkeypatch.setattr(disp.settings, "kommo_stage_remarketing_entrada", 2)
    monkeypatch.setattr(disp.settings, "kommo_stage_remarketing_api", 3)
    monkeypatch.setattr(disp.settings, "meta_cloud_phone_number_id", "")
    monkeypatch.setattr(disp.settings, "recovery_hsm_template_name", "")
    # Em dry, Meta config pode estar vazia
    disp._check_config(dry=True)


def test_check_config_live_requires_meta(monkeypatch):
    monkeypatch.setattr(disp.settings, "kommo_pipeline_id_remarketing", 1)
    monkeypatch.setattr(disp.settings, "kommo_stage_remarketing_entrada", 2)
    monkeypatch.setattr(disp.settings, "kommo_stage_remarketing_api", 3)
    monkeypatch.setattr(disp.settings, "meta_cloud_phone_number_id", "")
    with pytest.raises(RuntimeError, match="meta_cloud_phone_number_id"):
        disp._check_config(dry=False)
