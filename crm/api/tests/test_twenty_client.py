"""Cliente Twenty (app/integrations/twenty_client.py).

Sem rede real: credencial lida de env-file temporário, httpx mockado. Cobre:
  • credencial lazy/tolerante (arquivo ausente → configured False, sem raise)
  • guard PYTEST_CURRENT_TEST na escrita (update_stage NÃO chama rede em teste)
  • dry_run em settings (loga, devolve simulado)
  • retry em 429/5xx no _request
  • get_opportunity_by_kommo_id (filtro + match)
  • _unwrap_opportunity (3 envelopes)
"""
from __future__ import annotations

import httpx
import pytest

from app.integrations import twenty_client as tc
from app.integrations.twenty_client import TwentyClient, TwentyConfigError, _unwrap_opportunity


def _client_com_creds(tmp_path):
    env = tmp_path / "twenty.env"
    env.write_text('TWENTY_API_URL="http://localhost:3333"\nTWENTY_API_KEY="tok-xyz"\n')
    return TwentyClient(env_file=str(env))


# ── credencial lazy/tolerante ────────────────────────────────────────────────

def test_configured_false_sem_arquivo():
    c = TwentyClient(env_file="/caminho/que/nao/existe.env")
    assert c.configured is False


def test_configured_true_com_creds(tmp_path):
    c = _client_com_creds(tmp_path)
    assert c.configured is True


def test_headers_sem_cred_levanta():
    c = TwentyClient(env_file="/nao/existe.env")
    with pytest.raises(TwentyConfigError):
        c._headers()


def test_nao_le_cred_na_init(tmp_path, monkeypatch):
    # a init NÃO deve ler o arquivo; só _ensure_creds (lazy).
    env = tmp_path / "twenty.env"
    env.write_text('TWENTY_API_URL="http://x"\nTWENTY_API_KEY="k"\n')
    c = TwentyClient(env_file=str(env))
    assert c._loaded is False
    _ = c.configured
    assert c._loaded is True


# ── guard pytest na escrita ──────────────────────────────────────────────────

def test_update_stage_guard_pytest(tmp_path):
    # PYTEST_CURRENT_TEST está setado (rodando sob pytest) → NÃO chama rede.
    c = _client_com_creds(tmp_path)
    called = {"req": False}

    def fake_request(*a, **k):
        called["req"] = True
        return {}

    c._request = fake_request  # type: ignore[method-assign]
    out = c.update_stage("opp-1", "VISITA_REAL")
    assert out["dry_run"] is True
    assert out["reason"] == "pytest"
    assert called["req"] is False


def test_update_stage_dry_run_settings(tmp_path, monkeypatch):
    # remove o guard pytest e liga settings.dry_run → simula sem rede.
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(tc.settings, "dry_run", True)
    c = _client_com_creds(tmp_path)
    called = {"req": False}
    c._request = lambda *a, **k: called.__setitem__("req", True)  # type: ignore[method-assign]
    out = c.update_stage("opp-1", "PROPOSAL")
    assert out["dry_run"] is True
    assert out["reason"] == "settings.dry_run"
    assert called["req"] is False


def test_update_stage_real_quando_liberado(tmp_path, monkeypatch):
    # sem guard pytest, dry_run False → chama _request com o shape certo.
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(tc.settings, "dry_run", False)
    c = _client_com_creds(tmp_path)
    captured = {}

    def fake_request(method, path, body=None):
        captured.update(method=method, path=path, body=body)
        return {"data": {"updateOpportunity": {"id": "opp-1", "stage": "PROPOSAL"}}}

    c._request = fake_request  # type: ignore[method-assign]
    out = c.update_stage("opp-1", "PROPOSAL")
    assert captured["method"] == "PATCH"
    assert captured["path"] == "/rest/opportunities/opp-1"
    assert captured["body"] == {"stage": "PROPOSAL"}
    assert out["stage"] == "PROPOSAL"


# ── retry 429/5xx ────────────────────────────────────────────────────────────

def test_request_retry_em_429(tmp_path, monkeypatch):
    c = _client_com_creds(tmp_path)
    monkeypatch.setattr(tc.time, "sleep", lambda *_: None)  # não dorme no teste

    seq = [429, 429, 200]
    calls = {"n": 0}

    class FakeResp:
        def __init__(self, code):
            self.status_code = code
            self.content = b'{"ok": true}' if code == 200 else b""

        def raise_for_status(self):
            if self.status_code >= 400:
                raise httpx.HTTPStatusError("err", request=None, response=self)  # type: ignore[arg-type]

        def json(self):
            return {"ok": True}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def request(self, *a, **k):
            code = seq[calls["n"]]
            calls["n"] += 1
            return FakeResp(code)

    monkeypatch.setattr(tc.httpx, "Client", FakeClient)
    out = c._request("GET", "/rest/opportunities/x")
    assert out == {"ok": True}
    assert calls["n"] == 3  # 2 falhas + 1 sucesso


def test_request_500_esgota_retries_e_levanta(tmp_path, monkeypatch):
    c = _client_com_creds(tmp_path)
    monkeypatch.setattr(tc.time, "sleep", lambda *_: None)

    class FakeResp:
        status_code = 500
        content = b""

        def raise_for_status(self):
            raise httpx.HTTPStatusError("500", request=None, response=self)  # type: ignore[arg-type]

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def request(self, *a, **k):
            return FakeResp()

    monkeypatch.setattr(tc.httpx, "Client", FakeClient)
    with pytest.raises(httpx.HTTPStatusError):
        c._request("PATCH", "/rest/opportunities/x", {"stage": "X"})


# ── get_opportunity_by_kommo_id ──────────────────────────────────────────────

def test_get_opportunity_by_kommo_id_match(tmp_path):
    c = _client_com_creds(tmp_path)
    c._request = lambda *a, **k: {  # type: ignore[method-assign]
        "data": {"opportunities": [{"id": "opp-9", "kommoLeadId": "12345", "stage": "NEW"}]}
    }
    opp = c.get_opportunity_by_kommo_id("12345")
    assert opp is not None
    assert opp["id"] == "opp-9"


def test_get_opportunity_by_kommo_id_sem_match(tmp_path):
    c = _client_com_creds(tmp_path)
    c._request = lambda *a, **k: {"data": {"opportunities": []}}  # type: ignore[method-assign]
    assert c.get_opportunity_by_kommo_id("99999") is None


# ── _unwrap_opportunity ──────────────────────────────────────────────────────

def test_unwrap_envelopes():
    assert _unwrap_opportunity({"data": {"opportunity": {"id": "a"}}})["id"] == "a"
    assert _unwrap_opportunity({"data": {"updateOpportunity": {"id": "b"}}})["id"] == "b"
    assert _unwrap_opportunity({"id": "c"})["id"] == "c"
    assert _unwrap_opportunity({"data": {}}) is None
    assert _unwrap_opportunity(None) is None
