"""Testes do receptor de histórico da coexistência (`POST /cloud/corretora/{slug}/history`).

Quando a corretora entra na coexistência e aceita compartilhar histórico, a Meta
reentrega até 6 MESES de conversa no field `history` (em fases/chunks). Sem este
receptor, o histórico é oferecido e cai no chão.

O que se garante aqui:
  • direção correta nas DUAS pontas (echo do negócio = from_corretora=True,
    mensagem do contato = False) — errar isso inverte a conversa na tela;
  • `ts` = timestamp ORIGINAL da mensagem (nunca now()) — o painel ordena por ele;
  • marcador `historico_coexistencia` no metadata;
  • reentrega do MESMO payload não duplica (idempotência por wamid);
  • linha desconhecida = 404, auth errada = 401 (fail-closed);
  • mídia sem binário baixável grava marcador e não quebra;
  • histórico NÃO aciona automação (nenhum envio/cadência/IA).

Formato do payload conforme doc da Cloud API de coexistência: cada bloco de
`history[]` tem `metadata` (phase/chunk_order/progress) e `threads[]`; cada thread
tem `id` (telefone do contato) e `messages[]`; `to` só aparece quando é echo do
negócio.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api import cloud_corretora as cc
from app.api import cloud_corretora_a as corretor
from app.config import settings
from app.db.session import SessionLocal
from app.main import app
from app.services import cloud_lines
from sqlalchemy import text as sql_text

SECRET = "segredo-de-teste-history"
PHONE_ID_CORRETOR = "000000000000000"
LINHA_CORRETOR = "+5500000000000"          # display_phone_number da linha da Pessoa10
LEAD = "+5500000000000"
INSTANCE_CORRETOR = "acme_corretora_c"
CORRETORA_CORRETOR = 1

# timestamps ANTIGOS de propósito: histórico é conversa velha, e é isso que
# mantém o registro fora das varreduras por recência (transcrição/Inspector).
TS_LEAD = 1739230955        # 2025-02-10 22:22:35 UTC
TS_CORRETORA = 1739231100   # 2025-02-10 22:25:00 UTC


@pytest.fixture(autouse=True)
def _linha_corretor_registrada(monkeypatch):
    """Registra a linha da Pessoa10 no registry (em produção vem de CLOUD_LINHA_CORRETOR)
    e fixa o secret do webhook — sem ele o endpoint é fail-closed (401)."""
    corretor = cloud_lines.CloudLine(
        phone_id=PHONE_ID_CORRETOR, slug="corretor", papel="corretora",
        env_file="/etc/agencia/review.env", token_var="META_REVIEW_TOKEN",
    )
    monkeypatch.setattr(cloud_lines, "_LINES", {PHONE_ID_CORRETOR: corretor})
    monkeypatch.setattr(cloud_lines, "_BY_SLUG", {"corretor": PHONE_ID_CORRETOR})
    monkeypatch.setattr(settings, "cloud_webhook_secret", SECRET)
    yield


@pytest.fixture(autouse=True)
def _limpa_thread():
    """Zera as mensagens da instância da Pessoa10 antes e depois de cada teste."""
    def _wipe() -> None:
        with SessionLocal() as s:
            s.execute(sql_text("DELETE FROM mensagens_whatsapp WHERE instance = :i"),
                      {"i": INSTANCE_CORRETOR})
            s.commit()
    _wipe()
    yield
    _wipe()


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


def _payload(messages: list[dict[str, Any]] | None = None, *,
             progress: int = 55, phase: int = 1) -> dict[str, Any]:
    """Payload de history como o api-disparos repassa (metadata + history[])."""
    msgs = messages if messages is not None else [_msg_do_lead(), _msg_da_corretora()]
    return {
        "metadata": {
            "display_phone_number": LINHA_CORRETOR,
            "phone_number_id": PHONE_ID_CORRETOR,
        },
        "history": [{
            "metadata": {"phase": phase, "chunk_order": 1, "progress": progress},
            "threads": [{"id": LEAD, "messages": msgs}],
        }],
    }


def _msg_do_lead(wamid: str = "wamid.HIST.LEAD1") -> dict[str, Any]:
    """Mensagem DO CONTATO: `from` = lead e SEM `to` (não é echo)."""
    return {
        "from": LEAD,
        "id": wamid,
        "timestamp": str(TS_LEAD),
        "type": "text",
        "text": {"body": "tem apto de 2 quartos na Bairro X?"},
        "history_context": {"status": "READ"},
    }


def _msg_da_corretora(wamid: str = "wamid.HIST.CORR1") -> dict[str, Any]:
    """Echo do NEGÓCIO: `to` presente (marca de SMB echo) + `from` = a linha."""
    return {
        "from": LINHA_CORRETOR,
        "to": LEAD,
        "id": wamid,
        "timestamp": str(TS_CORRETORA),
        "type": "text",
        "text": {"body": "tenho sim! te mando as fotos"},
        "history_context": {"status": "READ"},
    }


def _post(client: TestClient, payload: dict[str, Any], *,
          slug: str = "corretor", secret: str | None = SECRET):
    headers = {"X-Cloud-Secret": secret} if secret is not None else {}
    return client.post(f"/cloud/corretora/{slug}/history", json=payload,
                       headers=headers)


def _rows() -> list[dict[str, Any]]:
    with SessionLocal() as s:
        rows = s.execute(sql_text(
            """
            SELECT evolution_message_id, from_corretora, conteudo_texto,
                   tipo_mensagem, timestamp_evolution, conteudo_metadata,
                   instance, corretora_id, contato_telefone
              FROM mensagens_whatsapp
             WHERE instance = :i
             ORDER BY timestamp_evolution ASC
            """), {"i": INSTANCE_CORRETOR}).mappings().all()
    return [dict(r) for r in rows]


# ── direção + timestamp original ────────────────────────────────────────────

def test_grava_as_duas_direcoes(client: TestClient) -> None:
    """Contato → from_corretora=False; echo do negócio → True."""
    r = _post(client, _payload())
    assert r.status_code == 200, r.text
    assert r.json()["recebidas"] == 2

    rows = _rows()
    assert len(rows) == 2
    por_wamid = {x["evolution_message_id"]: x for x in rows}

    lead = por_wamid["wamid.HIST.LEAD1"]
    assert lead["from_corretora"] is False
    assert lead["conteudo_texto"] == "tem apto de 2 quartos na Bairro X?"

    corr = por_wamid["wamid.HIST.CORR1"]
    assert corr["from_corretora"] is True
    assert corr["conteudo_texto"] == "tenho sim! te mando as fotos"


def test_direcao_por_from_quando_nao_tem_to(client: TestClient) -> None:
    """Cinto e suspensório: mesmo SEM `to`, `from` == a linha ⇒ é da corretora.
    (algumas entregas de history omitem o `to` do echo)"""
    msg = _msg_da_corretora()
    msg.pop("to")
    _post(client, _payload([msg]))
    rows = _rows()
    assert len(rows) == 1
    assert rows[0]["from_corretora"] is True


def test_usa_timestamp_original_nunca_now(client: TestClient) -> None:
    """O painel ordena por timestamp_evolution — histórico com now() jogaria
    conversa de 6 meses atrás pro topo da tela."""
    _post(client, _payload())
    rows = _rows()
    esperado = {
        "wamid.HIST.LEAD1": datetime.fromtimestamp(TS_LEAD, tz=timezone.utc),
        "wamid.HIST.CORR1": datetime.fromtimestamp(TS_CORRETORA, tz=timezone.utc),
    }
    for row in rows:
        ts = row["timestamp_evolution"]
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        assert ts == esperado[row["evolution_message_id"]]
        # e nada gravado "agora"
        assert (datetime.now(timezone.utc) - ts).days > 30


def test_marcador_de_historico_no_metadata(client: TestClient) -> None:
    """Precisa dar pra distinguir depois o que veio do sync."""
    _post(client, _payload())
    for row in _rows():
        assert row["conteudo_metadata"]["historico_coexistencia"] is True


def test_grava_na_instance_e_corretora_da_linha(client: TestClient) -> None:
    _post(client, _payload())
    for row in _rows():
        assert row["instance"] == INSTANCE_CORRETOR
        assert row["corretora_id"] == CORRETORA_CORRETOR
        assert row["contato_telefone"] == LEAD


def test_contato_vem_do_thread_id(client: TestClient) -> None:
    """O telefone da conversa é o `threads[].id`, não o `from` (que alterna)."""
    _post(client, _payload())
    assert {r["contato_telefone"] for r in _rows()} == {LEAD}


# ── idempotência (o sync REENTREGA chunks) ──────────────────────────────────

def test_reentrega_do_mesmo_payload_nao_duplica(client: TestClient) -> None:
    """A Meta reentrega chunk em timeout — reprocessar é o caso NORMAL."""
    payload = _payload()
    r1 = _post(client, payload)
    r2 = _post(client, payload)
    assert r1.status_code == 200 and r2.status_code == 200
    # o ACK conta o que RECEBEU (2 nas duas vezes), mas o banco tem 2 no total
    assert r2.json()["recebidas"] == 2
    assert len(_rows()) == 2


def test_chunk_novo_soma_sem_apagar_o_anterior(client: TestClient) -> None:
    _post(client, _payload())
    _post(client, _payload([_msg_do_lead("wamid.HIST.LEAD2")]))
    wamids = {r["evolution_message_id"] for r in _rows()}
    assert wamids == {"wamid.HIST.LEAD1", "wamid.HIST.CORR1", "wamid.HIST.LEAD2"}


def test_wamid_repetido_no_mesmo_payload_grava_uma_vez(client: TestClient) -> None:
    """A Meta às vezes repete o mesmo wamid dentro do chunk — o dedup
    intra-payload evita violar a idempotência dentro de um único INSERT."""
    m = _msg_do_lead()
    r = _post(client, _payload([m, dict(m), dict(m)]))
    assert r.status_code == 200
    assert len(_rows()) == 1


def test_reentrega_com_lote_grande_nao_perde_nem_duplica(client: TestClient) -> None:
    """Regressão da idempotência em VOLUME (o cenário real do sync).

    Reentrega um chunk maior que `_LOTE` já gravado, MAIS mensagens novas: as
    antigas não podem duplicar e as novas não podem ser perdidas. Cobre o caso em
    que 1 duplicata abortaria o lote inteiro de 500 mensagens boas.
    """
    n = cc._LOTE + 120
    antigas = [
        {"from": LEAD, "id": f"wamid.MIX{i}", "timestamp": str(TS_LEAD + i),
         "type": "text", "text": {"body": f"m{i}"}}
        for i in range(n)
    ]
    assert _post(client, _payload(antigas)).status_code == 200
    assert len(_rows()) == n

    novas = [
        {"from": LEAD, "id": f"wamid.MIX_NOVA{i}", "timestamp": str(TS_LEAD + 9000 + i),
         "type": "text", "text": {"body": f"nova{i}"}}
        for i in range(50)
    ]
    # payload com TODAS as antigas de novo + as novas, embaralhando a ordem
    assert _post(client, _payload(antigas + novas)).status_code == 200

    rows = _rows()
    assert len(rows) == n + 50
    wamids = {r["evolution_message_id"] for r in rows}
    assert len(wamids) == n + 50          # nenhuma duplicada
    assert "wamid.MIX_NOVA49" in wamids   # nenhuma nova perdida


def test_volume_acima_do_lote(client: TestClient) -> None:
    """Chunk maior que `_LOTE` (500) tem que gravar tudo, em várias fatias."""
    n = cc._LOTE * 2 + 37
    msgs = [
        {"from": LEAD, "id": f"wamid.BULK{i}", "timestamp": str(TS_LEAD + i),
         "type": "text", "text": {"body": f"m{i}"}}
        for i in range(n)
    ]
    r = _post(client, _payload(msgs))
    assert r.status_code == 200
    assert r.json()["recebidas"] == n
    assert len(_rows()) == n


def test_falha_de_banco_nao_estoura_500(client: TestClient, monkeypatch) -> None:
    """Erro de gravação NUNCA vira 5xx — a Meta reenfileiraria pra sempre."""
    def _boom(*a: Any, **k: Any):
        raise RuntimeError("banco caiu")

    monkeypatch.setattr(cc, "SessionLocal", _boom)
    r = _post(client, _payload())
    assert r.status_code == 200


# ── segurança / fail-closed ────────────────────────────────────────────────

def test_linha_desconhecida_404(client: TestClient) -> None:
    assert _post(client, _payload(), slug="fulana").status_code == 404


def test_linha_sem_env_cadastrada_404(client: TestClient, monkeypatch) -> None:
    """Slug conhecido no _DESTINO mas sem linha no registry (env ausente)."""
    monkeypatch.setattr(cloud_lines, "_BY_SLUG", {})
    assert _post(client, _payload(), slug="corretor").status_code == 404


def test_auth_errada_401(client: TestClient) -> None:
    r = _post(client, _payload(), secret="secret-errado")
    assert r.status_code == 401
    assert _rows() == []


def test_sem_header_401(client: TestClient) -> None:
    assert _post(client, _payload(), secret=None).status_code == 401


# ── mídia e payloads defeituosos ───────────────────────────────────────────

def test_midia_sem_binario_grava_marcador(client: TestClient) -> None:
    """Mídia de histórico raramente é baixável (media_id antigo): grava
    placeholder + media_id no metadata, sem tentar download e sem quebrar."""
    msg = {
        "from": LEAD,
        "id": "wamid.HIST.AUDIO",
        "timestamp": str(TS_LEAD),
        "type": "audio",
        "audio": {"id": "MEDIA_ANTIGO_1", "mime_type": "audio/ogg; codecs=opus"},
    }
    r = _post(client, _payload([msg]))
    assert r.status_code == 200
    row = _rows()[0]
    assert row["tipo_mensagem"] == "audio"
    assert row["conteudo_texto"] == "[audio]"
    assert row["conteudo_metadata"]["historico_coexistencia"] is True
    assert row["conteudo_metadata"]["cloud_media_id"] == "MEDIA_ANTIGO_1"


def test_mensagem_sem_wamid_e_ignorada(client: TestClient) -> None:
    msg = {"from": LEAD, "timestamp": str(TS_LEAD), "type": "text",
           "text": {"body": "sem id"}}
    r = _post(client, _payload([msg]))
    assert r.status_code == 200
    assert r.json()["recebidas"] == 0
    assert _rows() == []


def test_payload_vazio_nao_quebra(client: TestClient) -> None:
    """Nunca estourar 5xx pra Meta — ela reenfileira pra sempre."""
    for corpo in ({}, {"history": []}, {"history": [{"threads": []}]},
                  {"history": [{"threads": [{"id": LEAD}]}]}):
        r = _post(client, corpo)
        assert r.status_code == 200, (corpo, r.text)
        assert r.json()["recebidas"] == 0


def test_thread_sem_id_e_ignorado(client: TestClient) -> None:
    payload = _payload()
    payload["history"][0]["threads"][0].pop("id")
    r = _post(client, payload)
    assert r.status_code == 200
    assert _rows() == []


def test_progresso_volta_no_ack(client: TestClient) -> None:
    """progress=100 é o sinal de sync concluído — precisa ser observável."""
    r = _post(client, _payload(progress=100))
    assert r.json()["progresso"] == 100


# ── 🔒 histórico NÃO aciona automação ──────────────────────────────────────

def test_history_nao_dispara_automacao(client: TestClient, monkeypatch) -> None:
    """Requisito de segurança: gravar conversa velha não pode mandar mensagem,
    matricular em cadência nem chamar IA. Espiona os caminhos que enviam."""
    chamados: list[str] = []

    for mod, nome in (
        (corretor, "enviar_cloud"),
        (corretor, "enviar_template_linha"),
        (corretor, "tratar_resposta_disparo"),
        (corretor, "tratar_intencao_indicacao"),
    ):
        if hasattr(mod, nome):
            monkeypatch.setattr(
                mod, nome,
                lambda *a, _n=nome, **k: chamados.append(_n),
            )

    from app.services import cadencia
    for nome in ("matricular", "enfileirar_toque", "processar_tick"):
        if hasattr(cadencia, nome):
            monkeypatch.setattr(
                cadencia, nome,
                lambda *a, _n=nome, **k: chamados.append(f"cadencia.{_n}"),
            )

    r = _post(client, _payload())
    assert r.status_code == 200
    assert len(_rows()) == 2       # gravou
    assert chamados == []          # e não acionou NADA


def test_history_nao_usa_o_caminho_de_inbound(client: TestClient, monkeypatch) -> None:
    """Blindagem estrutural: o endpoint de history não delega pro inbound (que é
    quem carrega os gatilhos de automação)."""
    monkeypatch.setattr(
        cc, "corretora_inbound",
        lambda *a, **k: pytest.fail("history não deve chamar o inbound"),
    )
    assert _post(client, _payload()).status_code == 200
