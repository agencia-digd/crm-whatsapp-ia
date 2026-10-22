"""Chat igual ao WhatsApp, pacote A (16/10/2026): não lidas + prévia na lista.

Cobre:
  - `/painel/kanban` devolve `nao_lidas`, `preview` e `preview_de` por card
    (funil e "Fora do funil");
  - não lidas = mensagens do lead depois do mais recente entre "lido até" do
    login e a última saída da linha (sem registro: só o que está sem resposta);
  - POST `/painel/conversa/lida` zera só pra QUEM leu (admin não zera o da
    corretora), nunca volta no tempo, não aceita futuro, recusa telefone curto
    e quem não está logado;
  - `_preview_msg`: mídia vira rótulo, marcador "[document]" some, corta longo.

Precisa de DB real (TEST_DATABASE_URL — ver conftest.py).
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text as sql_text

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="precisa de TEST_DATABASE_URL (Postgres de teste)",
)

AGORA = datetime.now(timezone.utc)
KOMMO_CORRETOR = 10000006
STATUS_NOVO = 990001

TEL_A = "+5500000000000"  # lead mandou 2 depois da resposta da Pessoa10 → 2 não lidas
TEL_B = "+5500000000000"  # Pessoa10 falou por último → 0
TEL_FORA = "+5500000000000"  # sem lead no funil, lead mandou 1 → coluna "Fora do funil"
LID_A = f"t-nl-a-{uuid.uuid4().hex[:8]}"
LID_B = f"t-nl-b-{uuid.uuid4().hex[:8]}"
P8S = [TEL_A[-8:], TEL_B[-8:], TEL_FORA[-8:]]


def _hdr(user: str) -> dict[str, str]:
    return {"X-Painel-User": user}


def _ins_msg(s, *, tel: str, from_corretora: bool, at: datetime,
             tipo: str = "text", texto: str | None = "oi") -> str:
    mid = f"WAMID-NL-{uuid.uuid4().hex[:12]}"
    s.execute(
        sql_text(
            "INSERT INTO mensagens_whatsapp "
            "(id, evolution_message_id, instance, corretora_id, remote_jid, "
            " contato_telefone, contato_nome, from_corretora, tipo_mensagem, "
            " conteudo_texto, timestamp_evolution) "
            "VALUES (gen_random_uuid(), :mid, 'acme_corretora_c', 1, :jid, :tel, "
            "        :nome, :fromc, :tipo, :texto, :at)"
        ),
        {"mid": mid, "jid": f"{tel}@s.whatsapp.net", "tel": tel,
         "nome": f"Contato {tel[-4:]}", "fromc": from_corretora,
         "tipo": tipo, "texto": texto, "at": at},
    )
    return mid


@pytest.fixture(scope="module")
def seed():
    from app.db.session import SessionLocal

    mids: list[str] = []
    with SessionLocal() as s:
        s.execute(sql_text("DELETE FROM conversa_lida WHERE p8 = ANY(:p)"), {"p": P8S})
        mids.append(_ins_msg(s, tel=TEL_A, from_corretora=False, at=AGORA - timedelta(hours=3)))
        mids.append(_ins_msg(s, tel=TEL_A, from_corretora=True, at=AGORA - timedelta(hours=2),
                             texto="te mando as fotos"))
        mids.append(_ins_msg(s, tel=TEL_A, from_corretora=False, at=AGORA - timedelta(hours=1),
                             texto="manda sim"))
        mids.append(_ins_msg(s, tel=TEL_A, from_corretora=False, at=AGORA - timedelta(minutes=30),
                             tipo="audio", texto=None))
        mids.append(_ins_msg(s, tel=TEL_B, from_corretora=False, at=AGORA - timedelta(hours=5)))
        mids.append(_ins_msg(s, tel=TEL_B, from_corretora=True, at=AGORA - timedelta(hours=4),
                             texto="posso te ligar amanhã?"))
        mids.append(_ins_msg(s, tel=TEL_FORA, from_corretora=False, at=AGORA - timedelta(minutes=10),
                             texto="quero saber do apartamento"))
        for lid, tel, nome in ((LID_A, TEL_A, "Marina Teste"), (LID_B, TEL_B, "Bruno Teste")):
            s.execute(
                sql_text("INSERT INTO leads_snapshot (lead_id, nome, telefone, corretora_id) "
                         "VALUES (:lid, :nome, :tel, 1)"),
                {"lid": lid, "nome": nome, "tel": f"+{tel}"},
            )
        s.commit()
    yield mids
    with SessionLocal() as s:
        s.execute(sql_text("DELETE FROM mensagens_whatsapp WHERE evolution_message_id = ANY(:ids)"),
                  {"ids": mids})
        s.execute(sql_text("DELETE FROM leads_snapshot WHERE lead_id = ANY(:l)"),
                  {"l": [LID_A, LID_B]})
        s.execute(sql_text("DELETE FROM conversa_lida WHERE p8 = ANY(:p)"), {"p": P8S})
        s.commit()


@pytest.fixture
def client(seed, monkeypatch):
    from app.api import painel
    from app.main import app

    snap = {
        "statuses": [{"id": STATUS_NOVO, "name": "Novo", "sort": 1}],
        "leads": [
            {"id": LID_A, "name": "Marina Teste", "status_id": STATUS_NOVO,
             "responsible_user_id": KOMMO_CORRETOR},
            {"id": LID_B, "name": "Bruno Teste", "status_id": STATUS_NOVO,
             "responsible_user_id": KOMMO_CORRETOR},
        ],
        "users": {},
    }
    monkeypatch.setattr(painel, "_kommo_funil_snapshot", lambda: snap)
    return TestClient(app)


def _cards(client, user: str) -> dict[str, dict]:
    r = client.get("/painel/kanban?ordem=recentes&incluir_fora_funil=1", headers=_hdr(user))
    assert r.status_code == 200, r.text
    return {c["telefone"].lstrip("+"): c for col in r.json()["colunas"] for c in col["cards"]}


def _lida(client, user: str, tel: str, ate: datetime | None = None):
    body: dict = {"telefone": f"+{tel}"}
    if ate is not None:
        body["ate"] = ate.isoformat()
    return client.post("/painel/conversa/lida", json=body, headers=_hdr(user))


# ── a lista ───────────────────────────────────────────────────────────────────

def test_kanban_traz_nao_lidas_e_previa(client):
    c = _cards(client, "corretor")
    a, b = c[TEL_A], c[TEL_B]
    assert a["nao_lidas"] == 2  # as duas depois da resposta dela
    assert a["preview"] == "🎙️ Áudio"
    assert a["preview_de"] == "lead"
    assert b["nao_lidas"] == 0
    assert b["preview"] == "posso te ligar amanhã?"
    assert b["preview_de"] == "corretora"


def test_fora_do_funil_tambem_tem_previa_e_contador(client):
    fora = _cards(client, "corretor")[TEL_FORA]
    assert fora["stage"] == "fora_funil"
    assert fora["nao_lidas"] == 1
    assert fora["preview"] == "quero saber do apartamento"
    assert fora["preview_de"] == "lead"


# ── marcar como lida ──────────────────────────────────────────────────────────

def test_lida_zera_so_pra_quem_leu_e_persiste(client):
    r = _lida(client, "dona", TEL_A)
    assert r.status_code == 200, r.text
    assert r.json()["p8"] == TEL_A[-8:]
    # a Dona leu: o contador DELA zera; o da Pessoa10 continua (recarregar não muda)
    assert _cards(client, "dona")[TEL_A]["nao_lidas"] == 0
    assert _cards(client, "corretor")[TEL_A]["nao_lidas"] == 2
    assert _lida(client, "corretor", TEL_A).status_code == 200
    assert _cards(client, "corretor")[TEL_A]["nao_lidas"] == 0


def test_lida_nunca_volta_no_tempo(client):
    from app.db.session import SessionLocal

    assert _lida(client, "corretor", TEL_A).status_code == 200
    r = _lida(client, "corretor", TEL_A, ate=AGORA - timedelta(days=3))
    assert r.status_code == 200
    assert datetime.fromisoformat(r.json()["lido_ate"]) > AGORA - timedelta(minutes=5)
    assert _cards(client, "corretor")[TEL_A]["nao_lidas"] == 0
    # e mensagem nova do lead depois da leitura volta a contar
    with SessionLocal() as s:
        mid = _ins_msg(s, tel=TEL_A, from_corretora=False,
                       at=datetime.now(timezone.utc) + timedelta(seconds=2), texto="?")
        s.commit()
    try:
        assert _cards(client, "corretor")[TEL_A]["nao_lidas"] == 1
    finally:
        with SessionLocal() as s:
            s.execute(sql_text("DELETE FROM mensagens_whatsapp WHERE evolution_message_id = :m"),
                      {"m": mid})
            s.commit()


def test_lida_no_futuro_vira_agora(client):
    r = _lida(client, "corretor", TEL_B, ate=AGORA + timedelta(days=30))
    assert r.status_code == 200
    assert datetime.fromisoformat(r.json()["lido_ate"]) <= datetime.now(timezone.utc)


def test_lida_recusa_telefone_curto_e_sem_login(client):
    assert client.post("/painel/conversa/lida", json={"telefone": "123"},
                       headers=_hdr("corretor")).status_code == 422
    assert client.post("/painel/conversa/lida", json={"telefone": "+5500000000000"}
                       ).status_code == 403
    assert client.post("/painel/conversa/lida", json={"telefone": "+5500000000000"},
                       headers=_hdr("hacker")).status_code == 403


# ── prévia ────────────────────────────────────────────────────────────────────

def test_preview_msg_formata_como_whatsapp():
    from app.api.painel import _preview_msg

    assert _preview_msg("audio", None) == "🎙️ Áudio"
    assert _preview_msg("document", "contrato.pdf") == "📎 Documento · contrato.pdf"
    assert _preview_msg("document", "[document]") == "📎 Documento"
    assert _preview_msg("text", "  oi\n\ntudo bem?  ") == "oi tudo bem?"
    assert _preview_msg("text", "") == "📩 Mensagem"
    longo = _preview_msg("text", "a" * 200)
    assert len(longo) == 90 and longo.endswith("…")


# ── duração do áudio na conversa ──────────────────────────────────────────────

REF_AUDIO = "9900000177"
TEL_AUDIO = "+5500000000000"


@pytest.fixture()
def lead_corretor_com_audio():
    from app.db.session import SessionLocal

    def _limpa(s) -> None:
        s.execute(sql_text("DELETE FROM mensagens_whatsapp WHERE contato_telefone = :t"),
                  {"t": TEL_AUDIO})
        s.execute(sql_text("DELETE FROM leads_snapshot WHERE lead_id = :r"), {"r": REF_AUDIO})
        s.execute(sql_text("DELETE FROM leads WHERE external_ref = :r AND tenant_id = 1"),
                  {"r": REF_AUDIO})

    with SessionLocal() as s:
        _limpa(s)
        s.execute(
            sql_text("INSERT INTO leads_snapshot (lead_id, nome, telefone, corretora_id, ultima_sync) "
                     "VALUES (:r, 'Lead do Audio', :tel, 3, NOW())"),
            {"r": REF_AUDIO, "tel": TEL_AUDIO},
        )
        s.execute(
            sql_text("INSERT INTO leads (tenant_id, external_ref, nome, owner_id, origem, "
                     "created_at, updated_at) VALUES (1, :r, 'Lead do Audio', 3, 'teste', NOW(), NOW())"),
            {"r": REF_AUDIO},
        )
        for mid, meta, minutos in (("nl-audio-com", '{"duracao_segundos": 42}', 20),
                                   ("nl-audio-sem", "{}", 10)):
            s.execute(
                sql_text(
                    "INSERT INTO mensagens_whatsapp (evolution_message_id, instance, corretora_id, "
                    " remote_jid, contato_telefone, from_corretora, tipo_mensagem, conteudo_texto, "
                    " conteudo_metadata, timestamp_evolution, tenant_id) "
                    "VALUES (:mid, 'acme_corretora_a', 3, '90000177@s.whatsapp.net', :tel, FALSE, "
                    " 'audio', NULL, CAST(:meta AS JSONB), :ts, 1)"
                ),
                {"mid": mid, "tel": TEL_AUDIO, "meta": meta,
                 "ts": datetime.now(timezone.utc) - timedelta(minutes=minutos)},
            )
        s.commit()
    try:
        yield
    finally:
        with SessionLocal() as s:
            _limpa(s)
            s.commit()


def test_conversa_traz_duracao_do_audio_quando_a_linha_manda(lead_corretor_com_audio):
    from app.main import app

    r = TestClient(app).get(f"/painel/conversa?lead_id={REF_AUDIO}", headers=_hdr("corretor"))
    assert r.status_code == 200, r.text
    por_id = {m.get("id"): m for m in r.json()["mensagens"]}
    assert por_id["nl-audio-com"]["duracao"] == 42
    # sem o dado (linha Cloud não manda): o campo não vem, o player lê do arquivo
    assert "duracao" not in por_id["nl-audio-sem"]


# ── duração medida no servidor (linha Cloud não manda) ────────────────────────

def _wav(segundos: float) -> bytes:
    import io
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x00" * int(8000 * segundos))
    return buf.getvalue()


def test_duracao_audio_mede_o_arquivo():
    import shutil

    from app.api.painel import _duracao_audio

    if not shutil.which("ffprobe"):
        pytest.skip("sem ffprobe nesta máquina")
    assert _duracao_audio(_wav(3.0)) == 3.0
    assert _duracao_audio(b"isso nao e audio") is None
    assert _duracao_audio(b"") is None


def test_rota_duracao_passa_pela_trava_da_midia(monkeypatch):
    import shutil

    from fastapi import HTTPException, Response

    from app.api import painel
    from app.main import app

    if not shutil.which("ffprobe"):
        pytest.skip("sem ffprobe nesta máquina")
    chamadas: list[str] = []

    def _midia(*, lead_id, msg_id, x_painel_user):
        chamadas.append(x_painel_user)
        if x_painel_user == "corretor":
            raise HTTPException(403, "lead_de_outra_corretora")
        return Response(content=_wav(2.0), media_type="audio/wav")

    monkeypatch.setattr(painel, "painel_conversa_midia", _midia)
    monkeypatch.setattr(painel, "_DURACAO_CACHE", {})
    c = TestClient(app)
    url = "/painel/conversa/midia/duracao?lead_id=1&msg_id=dur-teste-1"
    r = c.get(url, headers=_hdr("corretor"))
    assert r.status_code == 200, r.text
    assert r.json()["duracao"] == 2.0
    # já medido, mas quem não pode ver a mídia continua barrado
    assert c.get(url, headers=_hdr("corretor")).status_code == 403
    assert chamadas == ["corretor", "corretor"]
