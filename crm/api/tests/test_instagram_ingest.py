"""Porta de entrada da Assistente (tarefas 4 e 4A) — receptor em sombra.

Rede externa BLOQUEADA por fixture autouse: qualquer tentativa de abrir socket
dentro destes testes levanta. É assim que "zero saída" deixa de ser promessa de
docstring e vira asserção — se alguém puser um `httpx.post` no caminho de
ingestão, o teste quebra aqui e não em produção.

O app secret dos testes é um valor descartável escrito num arquivo temporário;
o segredo real de `/etc/agencia/review-app.env` nunca é lido, nem aqui nem no log.

Como rodar:

    TEST_DATABASE_URL=postgresql+psycopg://<user>:<senha>@127.0.0.1:5433/<banco_descartavel> \\
        .venv/bin/python -m pytest tests/test_instagram_ingest.py -q
"""
from __future__ import annotations

import hashlib
import hmac
import inspect
import json
import socket
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import instagram_acme_webhook as webhook
from app.services.instagram_acme import contracts as c
from app.services.instagram_acme import ingest

FIXTURES = json.loads((Path(__file__).parent / "fixtures" / "instagram_events.json").read_text())

SEGREDO_DE_TESTE = "app-secret-descartavel-do-teste"
VERIFY_DE_TESTE = "verify-token-descartavel-do-teste"
AGORA = datetime(2026, 9, 21, 12, 0, 0, tzinfo=timezone.utc)


# ── Ambiente: sem rede, sem segredo real ─────────────────────────────────────


@pytest.fixture(autouse=True)
def _sem_rede(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nenhum socket sai daqui. O TestClient fala ASGI em processo, não rede."""

    def proibido(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("teste de sombra tentou abrir conexão de rede")

    monkeypatch.setattr(socket, "create_connection", proibido)
    monkeypatch.setattr(socket.socket, "connect", proibido)


@pytest.fixture
def segredos(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    arquivo = tmp_path / "review-app.env"
    arquivo.write_text(f'{webhook.CHAVE_APP_SECRET}="{SEGREDO_DE_TESTE}"\n')
    monkeypatch.setattr(webhook, "ARQUIVO_APP_SECRET", str(arquivo))
    return arquivo


@pytest.fixture
def repo() -> ingest.RepositorioEventosMemoria:
    repositorio = ingest.RepositorioEventosMemoria()
    anterior = ingest.repositorio_atual()
    ingest.definir_repositorio(repositorio)
    yield repositorio
    ingest.definir_repositorio(anterior)


@pytest.fixture
def cliente(segredos: Path, repo) -> TestClient:
    app = FastAPI()
    app.include_router(webhook.router)
    return TestClient(app)


def assinar(corpo: bytes, segredo: str = SEGREDO_DE_TESTE) -> str:
    return "sha256=" + hmac.new(segredo.encode(), corpo, hashlib.sha256).hexdigest()


def entregar(cliente: TestClient, payload: dict, *, segredo: str = SEGREDO_DE_TESTE):
    """Repassa o lote como o `painel-corretor` repassa: corpo cru + assinatura."""
    corpo = json.dumps(payload).encode()
    return cliente.post(
        c.ROTA_WEBHOOK, content=corpo, headers={"X-Hub-Signature-256": assinar(corpo, segredo)}
    )


# ── 1. Autenticação ──────────────────────────────────────────────────────────


def test_assinatura_invalida_e_recusada_e_nada_e_gravado(cliente: TestClient, repo) -> None:
    corpo = json.dumps(FIXTURES["comentario_dark_ad"]).encode()
    r = cliente.post(
        c.ROTA_WEBHOOK, content=corpo, headers={"X-Hub-Signature-256": assinar(corpo, "outro-segredo")}
    )
    assert r.status_code == 403
    assert repo._por_chave == {}


def test_sem_cabecalho_de_assinatura_e_recusado(cliente: TestClient) -> None:
    r = cliente.post(c.ROTA_WEBHOOK, content=b'{"object":"instagram","entry":[]}')
    assert r.status_code == 403


def test_app_secret_ausente_fecha_a_porta(
    cliente: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail-closed: sem arquivo de segredo, assinatura NENHUMA passa — nem uma
    calculada com segredo vazio, que é o jeito clássico de fail-open."""
    monkeypatch.setattr(webhook, "ARQUIVO_APP_SECRET", str(tmp_path / "nao-existe.env"))
    corpo = b'{"object":"instagram","entry":[]}'
    r = cliente.post(c.ROTA_WEBHOOK, content=corpo, headers={"X-Hub-Signature-256": assinar(corpo, "")})
    assert r.status_code == 403


def test_assinatura_e_sobre_os_bytes_crus_nao_sobre_o_json(cliente: TestClient) -> None:
    """Reserializar muda os bytes e tem que invalidar — é por isso que o repasse
    do painel-corretor manda o corpo original."""
    payload = FIXTURES["comentario_dark_ad"]
    original = json.dumps(payload).encode()
    reserializado = json.dumps(payload, indent=2).encode()
    r = cliente.post(
        c.ROTA_WEBHOOK, content=reserializado, headers={"X-Hub-Signature-256": assinar(original)}
    )
    assert r.status_code == 403


def test_handshake_devolve_challenge_e_falha_sem_token(
    cliente: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    query = {"hub.mode": "subscribe", "hub.verify_token": VERIFY_DE_TESTE, "hub.challenge": "1234"}

    # Arquivo ainda não existe em produção: 403, não "passa porque não confiro".
    monkeypatch.setattr(webhook, "ARQUIVO_VERIFY_TOKEN", str(tmp_path / "ausente.env"))
    assert cliente.get(c.ROTA_WEBHOOK, params=query).status_code == 403

    arquivo = tmp_path / "assistente-webhook.env"
    arquivo.write_text(f"{webhook.CHAVE_VERIFY_TOKEN}={VERIFY_DE_TESTE}\n")
    monkeypatch.setattr(webhook, "ARQUIVO_VERIFY_TOKEN", str(arquivo))
    ok = cliente.get(c.ROTA_WEBHOOK, params=query)
    assert ok.status_code == 200 and ok.text == "1234"

    errado = cliente.get(c.ROTA_WEBHOOK, params={**query, "hub.verify_token": "chute"})
    assert errado.status_code == 403


def test_segredo_nao_aparece_na_resposta_nem_no_corpo_do_erro(cliente: TestClient) -> None:
    r = entregar(cliente, FIXTURES["comentario_dark_ad"])
    assert SEGREDO_DE_TESTE not in r.text
    ruim = cliente.post(c.ROTA_WEBHOOK, content=b"{}")
    assert SEGREDO_DE_TESTE not in ruim.text


# ── 2. Quem entra e quem não entra ───────────────────────────────────────────


def test_comentario_da_acme_vira_evento_normalizado(cliente: TestClient, repo) -> None:
    r = entregar(cliente, FIXTURES["comentario_dark_ad"])
    assert r.status_code == 200
    assert r.json()["novos"] == 1

    evento = repo.por_id(c.TENANT_ID, c.Plataforma.INSTAGRAM, c.CONTA_INSTAGRAM, "CID-EXEMPLO-0001")
    assert evento is not None
    assert evento.tipo is c.TipoEvento.COMENTARIO
    assert evento.autor.ator_id == "IGSID-EXEMPLO-1"
    assert evento.contexto.media_id == "MEDIA-EXEMPLO-A"
    assert evento.criado_em.tzinfo is not None


def test_conta_desconhecida_e_ignorada_com_motivo(cliente: TestClient, repo) -> None:
    r = entregar(cliente, FIXTURES["conta_desconhecida"])
    assert r.status_code == 200
    corpo = r.json()
    assert corpo["recebidos"] == 0
    assert corpo["ignorados"] == [
        {"motivo": ingest.MotivoIgnorado.CONTA_NAO_PERMITIDA.value, "conta": "000000000000000"}
    ]
    assert repo._por_chave == {}


def test_lote_com_duas_contas_so_ingere_a_permitida(cliente: TestClient, repo) -> None:
    """O fan-out do repassador entrega o lote inteiro; a porta de entrada é que
    separa. Quem resolve a conta é o servidor, não quem chamou."""
    r = entregar(cliente, FIXTURES["duas_contas_no_mesmo_lote"])
    assert r.json()["novos"] == 1
    assert [e.evento_id for e in repo._por_chave.values()] == ["CID-EXEMPLO-0007"]


def test_facebook_nao_vira_instagram(cliente: TestClient, repo) -> None:
    r = entregar(cliente, FIXTURES["facebook_page"])
    assert r.status_code == 200
    corpo = r.json()
    assert corpo["recebidos"] == 0
    assert corpo["ignorados"][0]["motivo"] == ingest.MotivoIgnorado.OBJETO_NAO_INSTAGRAM.value
    assert repo._por_chave == {}


def test_campo_nao_tratado_entra_como_ignorado_e_nao_como_comentario(cliente: TestClient) -> None:
    corpo = entregar(cliente, FIXTURES["campo_nao_tratado"]).json()
    assert corpo["recebidos"] == 0
    assert corpo["ignorados"][0]["motivo"] == ingest.MotivoIgnorado.CAMPO_NAO_TRATADO.value


def test_comentario_sem_media_nao_derruba_o_lote(cliente: TestClient) -> None:
    """Contrato exige media_id em comentário. Item fora do contrato vira
    ignorado com motivo — nunca 500, que faria a Meta desativar a assinatura."""
    r = entregar(cliente, FIXTURES["comentario_sem_media"])
    assert r.status_code == 200
    assert r.json()["ignorados"][0]["motivo"] == ingest.MotivoIgnorado.COMENTARIO_SEM_MEDIA.value


def test_corpo_invalido_com_assinatura_boa_responde_200_sem_pedir_retry(cliente: TestClient) -> None:
    corpo = b"isto nao e json"
    r = cliente.post(c.ROTA_WEBHOOK, content=corpo, headers={"X-Hub-Signature-256": assinar(corpo)})
    assert r.status_code == 200
    assert r.json()["status"] == "corpo_invalido"


# ── 3. Idempotência ──────────────────────────────────────────────────────────


def test_reentrega_do_mesmo_evento_gera_um_evento_so(cliente: TestClient, repo) -> None:
    """A Meta reentrega quando o 200 demora — e o repassador também reentrega.
    Duas fontes de repetição, uma chave de idempotência."""
    primeiro = entregar(cliente, FIXTURES["comentario_dark_ad"]).json()
    segundo = entregar(cliente, FIXTURES["comentario_dark_ad"]).json()
    terceiro = entregar(cliente, FIXTURES["comentario_dark_ad"]).json()

    assert (primeiro["novos"], segundo["novos"], terceiro["novos"]) == (1, 0, 0)
    assert (segundo["repetidos"], terceiro["repetidos"]) == (1, 1)
    assert len(repo._por_chave) == 1


def test_eventos_diferentes_da_mesma_pessoa_nao_se_deduplicam(cliente: TestClient, repo) -> None:
    entregar(cliente, FIXTURES["comentario_dark_ad"])
    entregar(cliente, FIXTURES["resposta_em_thread"])
    assert len(repo._por_chave) == 2


def test_falha_ao_persistir_nao_confirma_processamento(
    cliente: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    class RepoQueQuebra:
        def registrar(self, evento):  # noqa: ANN001
            raise RuntimeError("banco fora do ar")

    monkeypatch.setattr(ingest, "_repositorio", RepoQueQuebra())
    r = entregar(cliente, FIXTURES["comentario_dark_ad"])
    assert r.status_code == 200  # não derruba a assinatura da Meta…
    assert r.json() == {"ok": False, "modo": "sombra", "status": "ingestao_falhou", "recebidos": 0}


# ── 4. Ecos e origem de tráfego ──────────────────────────────────────────────


def test_eco_de_comentario_da_casa_e_marcado_como_eco(cliente: TestClient, repo) -> None:
    entregar(cliente, FIXTURES["eco_comentario_da_casa"])
    evento = repo.por_id(c.TENANT_ID, c.Plataforma.INSTAGRAM, c.CONTA_INSTAGRAM, "CID-EXEMPLO-0004")
    assert evento.tipo is c.TipoEvento.ECO_COMENTARIO
    assert evento.e_eco is True


def test_eco_de_mensagem_e_marcado_e_aponta_pra_outra_ponta(cliente: TestClient, repo) -> None:
    entregar(cliente, FIXTURES["eco_mensagem_da_casa"])
    evento = repo.por_id(c.TENANT_ID, c.Plataforma.INSTAGRAM, c.CONTA_INSTAGRAM, "MID-EXEMPLO-0003")
    assert evento.tipo is c.TipoEvento.ECO_MENSAGEM
    assert evento.thread_id == "IGSID-EXEMPLO-4"


def test_mensagem_recebida_guarda_a_thread_da_pessoa(cliente: TestClient, repo) -> None:
    entregar(cliente, FIXTURES["mensagem_recebida"])
    evento = repo.por_id(c.TENANT_ID, c.Plataforma.INSTAGRAM, c.CONTA_INSTAGRAM, "MID-EXEMPLO-0001")
    assert evento.tipo is c.TipoEvento.MENSAGEM_RECEBIDA
    assert evento.thread_id == "IGSID-EXEMPLO-4"
    assert evento.criado_em == datetime.fromtimestamp(1789934650076 / 1000, tz=timezone.utc)


def test_mensagem_sem_texto_entra_igual(cliente: TestClient, repo) -> None:
    """Resposta a story sem texto existe e tem que ser registrada — o contrato
    aceita `texto=None` justamente porque o mundo real manda assim."""
    entregar(cliente, FIXTURES["mensagem_resposta_a_story"])
    evento = repo.por_id(c.TENANT_ID, c.Plataforma.INSTAGRAM, c.CONTA_INSTAGRAM, "MID-EXEMPLO-0002")
    assert evento is not None and evento.texto is None


def test_reacao_nao_vira_mensagem(cliente: TestClient) -> None:
    corpo = entregar(cliente, FIXTURES["reacao_nao_e_mensagem"]).json()
    assert corpo["recebidos"] == 0
    assert corpo["ignorados"][0]["motivo"] == ingest.MotivoIgnorado.CAMPO_NAO_TRATADO.value


def test_sem_ad_id_nao_vira_organico(cliente: TestClient, repo) -> None:
    """100% das mídias com comentário da Acme são dark ad. Se ausência de
    ad_id virasse `organico`, a operação inteira ficaria classificada errado."""
    entregar(cliente, FIXTURES["comentario_dark_ad"])
    evento = repo.por_id(c.TENANT_ID, c.Plataforma.INSTAGRAM, c.CONTA_INSTAGRAM, "CID-EXEMPLO-0001")
    assert evento.contexto.origem is c.OrigemTrafego.NAO_DETERMINADO
    assert evento.contexto.ad_id is None


def test_com_ad_id_vira_pago(cliente: TestClient, repo) -> None:
    entregar(cliente, FIXTURES["comentario_com_ad_id"])
    evento = repo.por_id(c.TENANT_ID, c.Plataforma.INSTAGRAM, c.CONTA_INSTAGRAM, "CID-EXEMPLO-0002")
    assert evento.contexto.origem is c.OrigemTrafego.PAGO
    assert evento.contexto.ad_id == "AD-EXEMPLO-77"


# ── 5. O relógio do comentário ───────────────────────────────────────────────


def test_comentario_sem_timestamp_cai_pro_entry_time_e_registra_a_fonte(cliente: TestClient, repo) -> None:
    """O payload real de `comments` chega SEM timestamp. A janela de private
    reply conta da criação do comentário, então o evento precisa dizer de onde
    veio o relógio — senão a tarefa 5 decide em cima de um valor aproximado sem
    saber que é aproximado."""
    entregar(cliente, FIXTURES["comentario_dark_ad"])
    evento = repo.por_id(c.TENANT_ID, c.Plataforma.INSTAGRAM, c.CONTA_INSTAGRAM, "CID-EXEMPLO-0001")
    assert evento.payload_bruto["criado_em_fonte"] == "entry.time"
    assert evento.criado_em == datetime.fromtimestamp(1789934000, tz=timezone.utc)


def test_epoch_em_segundos_e_em_milissegundos(cliente: TestClient) -> None:
    assert ingest._epoch(1789934000) == datetime.fromtimestamp(1789934000, tz=timezone.utc)
    assert ingest._epoch(1789934650076) == datetime.fromtimestamp(1789934650.076, tz=timezone.utc)
    assert ingest._epoch(None) is None and ingest._epoch("ontem") is None


# ── 6. Zero saída ────────────────────────────────────────────────────────────


def test_modulos_nao_importam_cliente_http_nem_transporte() -> None:
    for modulo in (webhook, ingest):
        fonte = inspect.getsource(modulo)
        for proibido in (
            "import httpx",
            "import requests",
            "urllib.request",
            "graph.facebook.com",
            "EvolutionClient",
            "lead_intake",
            "send_text",
        ):
            assert proibido not in fonte, f"{modulo.__name__} não pode conter {proibido!r}"


def test_nenhuma_rota_de_envio_ou_cadastro_declarada() -> None:
    rotas = {r.path for r in webhook.router.routes}
    assert rotas == {c.ROTA_WEBHOOK}
    assert not rotas & set(c.ROTAS_PROIBIDAS_V1)


def test_ingestao_nao_produz_proposta_nem_lead(cliente: TestClient, repo) -> None:
    """A porta de entrada grava evento e para. Proposta é tarefa 5; lead é 6/9."""
    for nome in ("comentario_dark_ad", "mensagem_recebida", "eco_comentario_da_casa"):
        entregar(cliente, FIXTURES[nome])
    assert all(isinstance(e, c.EventoNormalizado) for e in repo._por_chave.values())
    corpo_visto = json.dumps(entregar(cliente, FIXTURES["comentario_dark_ad"]).json())
    for palavra in ("proposta", "lead", "enviado", "cadastrad"):
        assert palavra not in corpo_visto


# ── 7. Normalização direta (sem HTTP) ────────────────────────────────────────


def test_normalizar_lote_e_puro_e_nao_grava(repo) -> None:
    eventos, ignorados = ingest.normalizar_lote(FIXTURES["comentario_dark_ad"], recebido_em=AGORA)
    assert len(eventos) == 1 and ignorados == []
    assert eventos[0].recebido_em == AGORA
    assert repo._por_chave == {}


def test_payload_bruto_redige_chave_que_pareca_segredo() -> None:
    payload = {
        "object": "instagram",
        "entry": [
            {
                "id": c.CONTA_INSTAGRAM,
                "time": 1789934000,
                "changes": [
                    {
                        "field": "comments",
                        "value": {
                            "id": "CID-REDIGIR",
                            "from": {"id": "IGSID-EXEMPLO-1"},
                            "text": "oi",
                            "media": {"id": "MEDIA-EXEMPLO-A"},
                            "access_token": "EAA-nunca-deveria-estar-aqui",
                        },
                    }
                ],
            }
        ],
    }
    eventos, _ = ingest.normalizar_lote(payload, recebido_em=AGORA)
    guardado = json.dumps(eventos[0].payload_bruto)
    assert "EAA-nunca-deveria-estar-aqui" not in guardado
    assert "***REDIGIDO***" in guardado
