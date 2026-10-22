"""POST /painel/visitas — agendar visita pela tela 📅 Visitas do V2.

O endpoint delega pro MESMO caminho da Assistente (subprocess de
skills/agenda/criar_visita.py: Calendar Gateway → INSERT `visitas`). Aqui o
subprocess é MOCKADO — zero Calendar real, zero Postgres, zero envio.

Casos: validação (422 nome/data/hora, data velha demais), retroativa (até 30
dias atrás, com --allow-past), escopo (403 corretora agendando pra outra, 400
admin sem corretora_id), happy (corretora usa a própria; admin escolhe), 409
duplicada, 502 falha do script.
"""
from __future__ import annotations

import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

import app.api.painel as painel
from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


class _FakeRun:
    """Dublê do subprocess.run — captura o cmd e devolve saída configurável."""

    returncode = 0
    stdout = json.dumps({
        "status": "ok", "dedup": False, "visita_id": 77,
        "evento_calendar_id": "ev123", "is_revisita": False,
    })
    stderr = ""
    chamadas: list[list[str]] = []

    def __call__(self, cmd, **kw):
        type(self).chamadas.append(list(cmd))
        r = type("R", (), {})()
        r.returncode = type(self).returncode
        r.stdout = type(self).stdout
        r.stderr = type(self).stderr
        return r


@pytest.fixture(autouse=True)
def _mock_subprocess(monkeypatch):
    _FakeRun.returncode = 0
    _FakeRun.stdout = json.dumps({
        "status": "ok", "dedup": False, "visita_id": 77,
        "evento_calendar_id": "ev123", "is_revisita": False,
    })
    _FakeRun.stderr = ""
    _FakeRun.chamadas = []
    import subprocess
    monkeypatch.setattr(subprocess, "run", _FakeRun())


def _hdr(user):
    return {"X-Painel-User": user}


def _body(**extra):
    base = {"lead_nome": "João Silva", "data": "2030-01-15", "hora": "14:00"}
    base.update(extra)
    return base


# ── validação ────────────────────────────────────────────────────────────────

def test_422_sem_nome(client):
    r = client.post("/painel/visitas", json=_body(lead_nome=""), headers=_hdr("corretor"))
    assert r.status_code == 422
    assert "lead_nome_obrigatorio" in r.text


def test_422_data_invalida(client):
    r = client.post("/painel/visitas", json=_body(data="15/01/2030"), headers=_hdr("corretor"))
    assert r.status_code == 422
    assert "data_invalida" in r.text


def test_422_hora_invalida(client):
    r = client.post("/painel/visitas", json=_body(hora="25h"), headers=_hdr("corretor"))
    assert r.status_code == 422
    assert "hora_invalida" in r.text


def test_422_data_muito_antiga(client):
    r = client.post("/painel/visitas", json=_body(data="2020-01-01"), headers=_hdr("corretor"))
    assert r.status_code == 422
    assert "data_muito_antiga" in r.text


# ── retroativa (janela de 30 dias) ───────────────────────────────────────────

def test_retroativa_dentro_da_janela_passa_allow_past(client):
    """Visita de 3 dias atrás entra, e o script recebe --allow-past."""
    ontem3 = (painel._hoje_sp() - timedelta(days=3)).isoformat()
    r = client.post("/painel/visitas", json=_body(data=ontem3), headers=_hdr("corretor"))
    assert r.status_code == 200, r.text
    assert r.json()["retroativa"] is True
    assert "--allow-past" in _FakeRun.chamadas[-1]


def test_borda_30_dias_passa_e_31_nao(client):
    hoje = painel._hoje_sp()
    r = client.post("/painel/visitas",
                    json=_body(data=(hoje - timedelta(days=30)).isoformat()),
                    headers=_hdr("corretor"))
    assert r.status_code == 200, r.text

    r = client.post("/painel/visitas",
                    json=_body(data=(hoje - timedelta(days=31)).isoformat()),
                    headers=_hdr("corretor"))
    assert r.status_code == 422
    assert "data_muito_antiga" in r.text


def test_hoje_e_futuro_nao_mandam_allow_past(client):
    """--allow-past só na retroativa — o gate do script segue valendo."""
    r = client.post("/painel/visitas",
                    json=_body(data=painel._hoje_sp().isoformat()),
                    headers=_hdr("corretor"))
    assert r.status_code == 200, r.text
    assert r.json()["retroativa"] is False
    assert "--allow-past" not in _FakeRun.chamadas[-1]

    client.post("/painel/visitas", json=_body(), headers=_hdr("corretor"))
    assert "--allow-past" not in _FakeRun.chamadas[-1]


# ── escopo ───────────────────────────────────────────────────────────────────

def test_403_corretora_agendando_pra_outra(client):
    # corretor = corretora 3; tentando agendar pra Pessoa10 (1) → 403
    r = client.post("/painel/visitas", json=_body(corretora_id=1), headers=_hdr("corretor"))
    assert r.status_code == 403
    assert "corretora_so_agenda_pra_ela" in r.text
    assert _FakeRun.chamadas == []  # nem chegou no script


def test_400_admin_sem_corretora(client):
    r = client.post("/painel/visitas", json=_body(), headers=_hdr("dona"))
    assert r.status_code == 400
    assert "corretora_id_obrigatorio" in r.text


def test_400_admin_corretora_invalida(client):
    r = client.post("/painel/visitas", json=_body(corretora_id=99), headers=_hdr("dona"))
    assert r.status_code == 400
    assert "corretora_id_invalido" in r.text


def test_403_sem_header(client):
    r = client.post("/painel/visitas", json=_body())
    assert r.status_code == 403


# ── happy path ───────────────────────────────────────────────────────────────

def test_corretora_agenda_pra_ela(client):
    r = client.post("/painel/visitas", json=_body(), headers=_hdr("corretor"))
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert d["visita_id"] == 77
    assert d["corretora_id"] == 3
    assert d["corretora_label"] == "Pessoa12"
    # subprocess chamado com o prefixo do Calendar da Pessoa3 ("Pessoa")
    cmd = _FakeRun.chamadas[0]
    assert "criar_visita.py" in cmd[1]
    assert cmd[cmd.index("--corretora") + 1] == "Pessoa"
    assert cmd[cmd.index("--cliente") + 1] == "João Silva"
    assert cmd[cmd.index("--origem") + 1] == "crm"
    assert cmd[cmd.index("--registrado-por-telefone") + 1] == "painel:corretor"


def test_admin_agenda_com_opcionais(client):
    r = client.post(
        "/painel/visitas",
        json=_body(corretora_id=1, telefone="+5500000000000",
                   lead_id="kommo_555", empreendimento="Res. Bairro X",
                   observacao="levar chave reserva"),
        headers=_hdr("dona"),
    )
    assert r.status_code == 200
    assert r.json()["corretora_label"] == "Pessoa10"
    cmd = _FakeRun.chamadas[0]
    assert cmd[cmd.index("--corretora") + 1] == "Pessoa10"
    assert cmd[cmd.index("--telefone") + 1] == "+5500000000000"
    assert cmd[cmd.index("--bairro") + 1] == "Res. Bairro X"
    assert cmd[cmd.index("--obs") + 1] == "levar chave reserva"
    assert cmd[cmd.index("--lead-id") + 1] == "kommo_555"


# ── saídas do script ─────────────────────────────────────────────────────────

def test_409_duplicada(client):
    _FakeRun.stdout = json.dumps({"status": "duplicada", "dedup": True})
    r = client.post("/painel/visitas", json=_body(), headers=_hdr("corretor"))
    assert r.status_code == 409
    assert "visita_duplicada" in r.text


def test_502_script_falhou(client):
    _FakeRun.returncode = 1
    _FakeRun.stderr = "falha ao criar evento no Calendar (Gateway): boom"
    r = client.post("/painel/visitas", json=_body(), headers=_hdr("corretor"))
    assert r.status_code == 502
    assert "criar_visita_falhou" in r.text
