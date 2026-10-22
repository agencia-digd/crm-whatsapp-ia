"""Busca de imóvel na web assíncrona (job) — criar, consultar, falhar, dedupar.

O motor real é o imovel-fetcher no Mac mini (Playwright/Chromium via Vpn).
Aqui ele é SEMPRE mockado — nenhum teste toca o Mac de verdade.

Cobre:
  - POST cria o job e devolve o id NA HORA (status=rodando), sem esperar o Mac
  - o worker roda e o GET passa a devolver as opções (status=pronto)
  - Mac fora do ar → status=erro com mensagem humana, NUNCA fica rodando
  - mesma busca do mesmo usuário já rodando → reaproveita o job (não abre outra
    sessão de browser no Mac)
  - job de outro usuário não vaza (404)
  - job "rodando" com heartbeat velho (copiloto reiniciou) vira erro na leitura
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text as sa_text

from app.db.models import BuscaImovelJob
from app.db.session import SessionLocal
from app.main import app
from app.services import busca_imovel_jobs as jobs

USER = "dona"
OUTRO_USER = "pessoa"
HDR = {"X-Painel-User": USER}


_OPCOES_FAKE = [
    {
        "titulo": "Apartamento 3 quartos Parque Bairro A",
        "preco": "R$ 480.000",
        "bairro": "Parque Bairro A",
        "cidade": "Cidade",
        "m2": "92",
        "quartos": "3",
        "portal": "VivaReal",
        "url": "https://www.vivareal.com.br/imovel/exemplo-123/",
    },
    {  # sem URL real → tem que ser descartado pela normalização
        "titulo": "Anúncio sem link",
        "url": "",
    },
]


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def _limpa_jobs():
    with SessionLocal() as s:
        s.execute(sa_text("DELETE FROM busca_imovel_jobs"))
        s.commit()
    yield
    with SessionLocal() as s:
        s.execute(sa_text("DELETE FROM busca_imovel_jobs"))
        s.commit()


@pytest.fixture(autouse=True)
def _worker_sincrono(monkeypatch):
    """O worker roda inline no teste — sem thread, resultado determinístico.

    Em produção `disparar_worker` sobe uma thread daemon; aqui a gente troca por
    uma chamada direta pra não depender de sleep/corrida.
    """
    monkeypatch.setattr(jobs, "disparar_worker", lambda job_id: None)


def _mock_fetcher(monkeypatch, opcoes=None, erro=None):
    """Mock no nível do httpx — o Mac nunca é chamado de verdade.

    Mockar aqui (e não em `chamar_fetcher`) mantém a normalização do payload
    dentro do caminho testado: é ela que descarta anúncio sem link real.
    """
    chamadas: list[str] = []

    class _Resp:
        status_code = 200

        def json(self):
            return {"opcoes": opcoes or []}

    class _Cli:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, params=None):
            chamadas.append((params or {}).get("texto", ""))
            if erro is not None:
                raise httpx.ConnectError("mac off")
            return _Resp()

    monkeypatch.setattr(jobs.httpx, "Client", _Cli)
    return chamadas


# ── criação ──────────────────────────────────────────────────────────────────


def test_post_devolve_job_id_na_hora_sem_esperar_o_mac(client, monkeypatch):
    chamadas = _mock_fetcher(monkeypatch, opcoes=_OPCOES_FAKE)

    r = client.post(
        "/painel/busca-imovel",
        json={"texto": "apto 3 quartos Bairro A até 500 mil"},
        headers=HDR,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "rodando"
    assert isinstance(body["job_id"], int)
    assert body["reaproveitado"] is False
    # a tela liberou ANTES do Mac ser chamado
    assert chamadas == []

    consulta = client.get(f"/painel/busca-imovel/{body['job_id']}", headers=HDR)
    assert consulta.status_code == 200
    assert consulta.json()["status"] == "rodando"


def test_texto_curto_nao_cria_job(client):
    r = client.post("/painel/busca-imovel", json={"texto": "ap"}, headers=HDR)
    assert r.status_code == 200
    body = r.json()
    assert body["job_id"] is None
    assert body["status"] == "erro"
    assert "lead procura" in (body["erro"] or "")
    with SessionLocal() as s:
        assert s.query(BuscaImovelJob).count() == 0


# ── ciclo completo ───────────────────────────────────────────────────────────


def test_worker_conclui_e_get_devolve_opcoes(client, monkeypatch):
    _mock_fetcher(monkeypatch, opcoes=_OPCOES_FAKE)

    job_id = client.post(
        "/painel/busca-imovel", json={"texto": "casa 2 quartos Vila Haro"}, headers=HDR
    ).json()["job_id"]

    jobs.executar_job(job_id)  # o que a thread daemon faria em produção

    d = client.get(f"/painel/busca-imovel/{job_id}", headers=HDR).json()
    assert d["status"] == "pronto"
    assert d["erro"] is None
    assert len(d["opcoes"]) == 1  # o item sem URL foi descartado
    assert d["opcoes"][0]["url"].startswith("https://")
    assert d["opcoes"][0]["resumo"] == "92 m² · 3 quartos"
    assert d["concluido_em"] is not None


def test_listagem_devolve_o_job_ao_reabrir_a_tela(client, monkeypatch):
    _mock_fetcher(monkeypatch, opcoes=_OPCOES_FAKE)
    job_id = client.post(
        "/painel/busca-imovel", json={"texto": "sobrado Bairro X"}, headers=HDR
    ).json()["job_id"]
    jobs.executar_job(job_id)

    lista = client.get("/painel/busca-imovel", headers=HDR).json()["jobs"]
    assert [j["job_id"] for j in lista] == [job_id]
    assert lista[0]["status"] == "pronto"
    assert lista[0]["pedido"] == "sobrado Bairro X"


# ── falha ────────────────────────────────────────────────────────────────────


def test_mac_fora_do_ar_vira_erro_humano_e_nao_fica_rodando(client, monkeypatch):
    _mock_fetcher(monkeypatch, opcoes=[], erro="mac_off")  # httpx.ConnectError

    job_id = client.post(
        "/painel/busca-imovel", json={"texto": "cobertura Alto da Boa Vista"}, headers=HDR
    ).json()["job_id"]
    jobs.executar_job(job_id)

    d = client.get(f"/painel/busca-imovel/{job_id}", headers=HDR).json()
    assert d["status"] == "erro"
    assert "Mac indisponível" in d["erro"]
    assert d["opcoes"] == []
    assert d["concluido_em"] is not None


def test_excecao_no_worker_nao_deixa_job_pendurado(client, monkeypatch):
    def explode(texto: str):
        raise RuntimeError("boom")

    monkeypatch.setattr(jobs, "chamar_fetcher", explode)

    job_id = client.post(
        "/painel/busca-imovel", json={"texto": "terreno Bairro X"}, headers=HDR
    ).json()["job_id"]
    jobs.executar_job(job_id)

    d = client.get(f"/painel/busca-imovel/{job_id}", headers=HDR).json()
    assert d["status"] == "erro"
    assert d["erro"]


def test_job_com_heartbeat_velho_e_marcado_como_erro_na_leitura(client, monkeypatch):
    _mock_fetcher(monkeypatch, opcoes=_OPCOES_FAKE)
    job_id = client.post(
        "/painel/busca-imovel", json={"texto": "apto 1 quarto centro"}, headers=HDR
    ).json()["job_id"]

    velho = datetime.now(timezone.utc) - timedelta(seconds=jobs.HEARTBEAT_MORTO_S + 60)
    with SessionLocal() as s:
        s.execute(
            sa_text("UPDATE busca_imovel_jobs SET heartbeat_em = :h WHERE id = :i"),
            {"h": velho, "i": job_id},
        )
        s.commit()

    d = client.get(f"/painel/busca-imovel/{job_id}", headers=HDR).json()
    assert d["status"] == "erro"
    assert "interrompida" in d["erro"]


# ── dedupe ───────────────────────────────────────────────────────────────────


def test_mesma_busca_rodando_reaproveita_o_job(client, monkeypatch):
    _mock_fetcher(monkeypatch, opcoes=_OPCOES_FAKE)

    primeiro = client.post(
        "/painel/busca-imovel", json={"texto": "apto 3 quartos Bairro A até 500 mil"}, headers=HDR
    ).json()
    # mesma coisa, escrita diferente (caixa/pontuação/espaço) — mesmo fingerprint
    segundo = client.post(
        "/painel/busca-imovel",
        json={"texto": "  APTO 3 quartos, Bairro A  até 500 mil!! "},
        headers=HDR,
    ).json()

    assert segundo["job_id"] == primeiro["job_id"]
    assert segundo["reaproveitado"] is True
    with SessionLocal() as s:
        assert s.query(BuscaImovelJob).count() == 1


def test_busca_diferente_abre_job_novo(client, monkeypatch):
    _mock_fetcher(monkeypatch, opcoes=_OPCOES_FAKE)
    a = client.post("/painel/busca-imovel", json={"texto": "apto Bairro A"}, headers=HDR).json()
    b = client.post("/painel/busca-imovel", json={"texto": "casa Vila Haro"}, headers=HDR).json()
    assert a["job_id"] != b["job_id"]
    assert b["reaproveitado"] is False


def test_job_concluido_nao_e_reaproveitado(client, monkeypatch):
    _mock_fetcher(monkeypatch, opcoes=_OPCOES_FAKE)
    primeiro = client.post(
        "/painel/busca-imovel", json={"texto": "apto Bairro A 3q"}, headers=HDR
    ).json()
    jobs.executar_job(primeiro["job_id"])

    segundo = client.post(
        "/painel/busca-imovel", json={"texto": "apto Bairro A 3q"}, headers=HDR
    ).json()
    assert segundo["reaproveitado"] is False
    assert segundo["job_id"] != primeiro["job_id"]


def test_dedupe_e_por_usuario(client, monkeypatch):
    _mock_fetcher(monkeypatch, opcoes=_OPCOES_FAKE)
    a = client.post("/painel/busca-imovel", json={"texto": "apto Bairro A"}, headers=HDR).json()
    b = client.post(
        "/painel/busca-imovel",
        json={"texto": "apto Bairro A"},
        headers={"X-Painel-User": OUTRO_USER},
    ).json()
    assert a["job_id"] != b["job_id"]


# ── isolamento ───────────────────────────────────────────────────────────────


def test_job_de_outro_usuario_da_404(client, monkeypatch):
    _mock_fetcher(monkeypatch, opcoes=_OPCOES_FAKE)
    job_id = client.post(
        "/painel/busca-imovel", json={"texto": "apto Bairro A"}, headers=HDR
    ).json()["job_id"]

    r = client.get(
        f"/painel/busca-imovel/{job_id}", headers={"X-Painel-User": OUTRO_USER}
    )
    assert r.status_code == 404


def test_sem_header_de_usuario_bloqueia(client):
    r = client.post("/painel/busca-imovel", json={"texto": "apto Bairro A"})
    assert r.status_code == 403


# ── retenção ─────────────────────────────────────────────────────────────────


def test_limpar_antigos_apaga_job_alem_da_retencao(client, monkeypatch):
    _mock_fetcher(monkeypatch, opcoes=_OPCOES_FAKE)
    job_id = client.post(
        "/painel/busca-imovel", json={"texto": "apto Bairro A"}, headers=HDR
    ).json()["job_id"]

    antigo = datetime.now(timezone.utc) - timedelta(days=jobs.RETENCAO_DIAS + 1)
    with SessionLocal() as s:
        s.execute(
            sa_text("UPDATE busca_imovel_jobs SET criado_em = :c WHERE id = :i"),
            {"c": antigo, "i": job_id},
        )
        s.commit()

    assert jobs.limpar_antigos() == 1
    with SessionLocal() as s:
        assert s.query(BuscaImovelJob).count() == 0
