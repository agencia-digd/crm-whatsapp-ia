"""Resposta do lead para a régua em QUALQUER linha (porte da DEX, 02/10/2026).

Até 02/10 só o inbound da linha da Pessoa12 chamava `processar_resposta_cadencia`.
Resposta na linha da Pessoa10/Pessoa11 não virava `respondida`: ficava só segurada pela
trava de 7 dias de conversa humana e depois a régua voltava a disparar (prova no
banco em 02/10: Pessoa10 1 'respondida' x 30 'remarketing'; Pessoa11 4 x 41; Pessoa12 159 x 681).

Pronto quando (documento reunioes/2026-10-02-motores-dex-para-acme.md, item 1):
uma resposta na linha da Pessoa10 muda `cadencia_estado` de 'ativa' para
'respondida' e cancela o próximo toque.

Banco da cadência: SQLite in-memory (mesmo padrão de test_cadencia_resposta).
Nada sai pra Meta: `gravar` e os envios são substituídos.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import BigInteger, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.db.session as db_session
from app.api import cloud_corretora
from app.api import cloud_corretora_a as corretor
from app.config import settings
from app.db.models import (
    Base,
    CadenciaEstado,
    CadenciaEvento,
    Corretora,
    DisparoReengajamento,
    Lead,
    LeadEvent,
    PipelineStage,
)
from app.main import app
from app.services import cadencia, cloud_lines


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(element, compiler, **kw):  # noqa: ANN001
    return "JSON"


@compiles(BigInteger, "sqlite")
def _bigint_sqlite(element, compiler, **kw):  # noqa: ANN001
    return "INTEGER"


SECRET = "segredo-teste-resposta-linha-corretora"
PHONE_ID_CORRETOR = "000000000000000"
PHONE_ID_CORRETOR = "000000000000000"
CORRETOR_ID = 1
LEAD = "+5500000000000"


@pytest.fixture()
def factory(monkeypatch):
    # StaticPool: a cadência abre a sessão dela em OUTRA thread (run_in_threadpool)
    # e `sqlite://` dá um banco vazio novo a cada conexão.
    engine = create_engine("sqlite://", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine, tables=[
        Corretora.__table__, CadenciaEstado.__table__, CadenciaEvento.__table__,
        Lead.__table__, LeadEvent.__table__, PipelineStage.__table__,
        # opt-out (07/10): o follow-up lê disparos_reengajamento a cada volta
        # e, sem a tabela, segura todo toque (fail-closed).
        DisparoReengajamento.__table__,
    ])
    f = sessionmaker(bind=engine)
    with f() as s:
        s.add(Corretora(id=CORRETOR_ID, nome="Pessoa5", telefone="+5500000000000",
                        telefone_normalizado="+5500000000000", ativo=True,
                        evolution_instance="acme_corretor", papel="corretora"))
        s.commit()
    # A cadência abre a própria sessão com `from app.db.session import
    # SessionLocal` (import tardio) — aponta pro SQLite deste teste.
    monkeypatch.setattr(db_session, "SessionLocal", f)
    return f


@pytest.fixture(autouse=True)
def _linhas_e_isolamento(monkeypatch):
    corretor = cloud_lines.CloudLine(phone_id=PHONE_ID_CORRETOR, slug="corretor", papel="corretora",
                                 env_file="/etc/agencia/review.env", token_var="META_REVIEW_TOKEN")
    corretor = cloud_lines.CloudLine(phone_id=PHONE_ID_CORRETOR, slug="corretor", papel="corretora",
                                env_file="/etc/agencia/review.env", token_var="META_REVIEW_TOKEN")
    monkeypatch.setattr(cloud_lines, "_LINES", {PHONE_ID_CORRETOR: corretor, PHONE_ID_CORRETOR: corretor})
    monkeypatch.setattr(cloud_lines, "_BY_SLUG", {"corretor": PHONE_ID_CORRETOR, "corretor": PHONE_ID_CORRETOR})
    monkeypatch.setattr(settings, "cloud_webhook_secret", SECRET)
    # O tick resolve a linha pela dona (corretoras.evolution_instance → slug).
    monkeypatch.setattr(cloud_lines, "linha_da_corretora",
                        lambda cid, session=None: corretor if cid == CORRETOR_ID else None)
    # Nada de banco de mensagens nem de efeitos laterais fora do que se testa.
    monkeypatch.setattr(corretor, "gravar", lambda **kw: True)
    monkeypatch.setattr(cloud_corretora.atendimento_corretor, "marcar_opt_out", lambda tel: None)
    monkeypatch.setattr(cloud_corretora.atendimento_corretor, "atender", lambda *a, **k: None)
    monkeypatch.setattr(cloud_corretora.cb, "processar_inbound", lambda *a, **k: None)
    from app.services import saida_remarketing

    monkeypatch.setattr(saida_remarketing, "processar_inbound", lambda *a, **k: None)
    cadencia._LAST_ENQUEUE["at"] = 0.0
    yield


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def _matricular_na_corretor(factory, *, agora: datetime) -> int:
    with factory() as s:
        est = cadencia.iniciar_cadencia(s, telefone=LEAD, lead_id="888",
                                        corretora_id=CORRETOR_ID, agora=agora)
        s.commit()
        return est.id


def _inbound(client, slug: str, texto: str, tipo: str = "text"):
    msg = {"from": LEAD, "id": f"wamid.teste.{slug}.{abs(hash(texto))}", "type": tipo,
           "timestamp": "1759400000"}
    if tipo == "button":
        msg["button"] = {"text": texto}
    else:
        msg["text"] = {"body": texto}
    return client.post(f"/cloud/corretora/{slug}/inbound",
                       json={"messages": [msg], "contacts": [
                           {"wa_id": LEAD, "profile": {"name": "Marta"}}]},
                       headers={"X-Cloud-Secret": SECRET})


def _status(factory) -> str:
    with factory() as s:
        return s.query(CadenciaEstado).one().status


def test_resposta_na_linha_da_corretor_encerra_a_cadencia_dela(factory, client):
    agora = datetime.now(timezone.utc)
    _matricular_na_corretor(factory, agora=agora - timedelta(days=2))
    assert _status(factory) == "ativa"

    r = _inbound(client, "corretor", "oi, ainda tenho interesse no apartamento")
    assert r.status_code == 200
    assert _status(factory) == "respondida"
    with factory() as s:
        tipos = [e.tipo for e in s.query(CadenciaEvento).all()]
    assert cadencia.TIPO_RESPOSTA in tipos

    # ...e o próximo toque NÃO sai: o tick de amanhã não manda nada pra ela.
    enviados: list = []

    def sender(tel, texto, **kw):
        enviados.append(tel)
        return {"ok": True, "dry_run": True, "sent": False}

    with factory() as s:
        cadencia.tick(agora=agora + timedelta(days=3), session=s, sender=sender)
    assert enviados == []


def test_sem_a_resposta_o_toque_sairia(factory):
    """Contraprova: o mesmo estado, sem a resposta, recebe o toque no tick."""
    agora = datetime.now(timezone.utc)
    _matricular_na_corretor(factory, agora=agora - timedelta(days=2))
    enviados: list = []

    def sender(tel, texto, **kw):
        enviados.append(tel)
        return {"ok": True, "dry_run": True, "sent": False}

    with factory() as s:
        cadencia.tick(agora=agora + timedelta(days=3), session=s, sender=sender)
    assert enviados == [LEAD]


def test_opt_out_na_linha_da_corretor_vira_opt_out_da_cadencia(factory, client):
    _matricular_na_corretor(factory, agora=datetime.now(timezone.utc) - timedelta(days=2))
    r = _inbound(client, "corretor", "Parar mensagens", tipo="button")
    assert r.status_code == 200
    assert _status(factory) == "opt_out"


def test_clique_em_botao_na_linha_da_corretor_nao_encerra(factory, client, monkeypatch):
    """Clique em "Saiba mais" não é resposta (regra Dona 05/08) — em qualquer linha."""
    monkeypatch.setattr(cadencia, "_enviar_audio", lambda est: False)
    _matricular_na_corretor(factory, agora=datetime.now(timezone.utc) - timedelta(days=2))
    r = _inbound(client, "corretor", "Saiba mais", tipo="button")
    assert r.status_code == 200
    assert _status(factory) == "ativa"


def test_linha_da_corretor_continua_com_o_carrossel(monkeypatch, client):
    """A linha da casa não passa pelo hook da cadência (quem trata é carrossel + Pessoa)."""
    chamados: list = []
    monkeypatch.setattr(corretor, "_tratar_resposta_cadencia_inbound",
                        lambda *a, **k: chamados.append(a))
    r = _inbound(client, "corretor", "oi")
    assert r.status_code == 200
    assert chamados == []


def test_linha_da_corretor_chama_o_hook(monkeypatch, client):
    chamados: list = []
    monkeypatch.setattr(corretor, "_tratar_resposta_cadencia_inbound",
                        lambda *a, **k: chamados.append(a))
    r = _inbound(client, "corretor", "oi")
    assert r.status_code == 200
    assert chamados == [(LEAD, "oi", "text")]


# ── o áudio do D3 sai pela linha da dona ──────────────────────────────────────

class _Est:
    def __init__(self, corretora_id):
        self.id = 1
        self.corretora_id = corretora_id
        self.telefone = LEAD


def _capturar_midia(monkeypatch, tmp_path):
    from app.services import audios_corretora

    arq = tmp_path / "a.oga"
    arq.write_bytes(b"OggS")
    monkeypatch.setattr(audios_corretora, "caminho_audio", lambda cid: arq)
    chamadas: list[dict] = []
    monkeypatch.setattr(corretor, "enviar_cloud_midia", lambda *a, **k: chamadas.append(k) or {})
    return chamadas


def test_audio_da_corretor_sai_pela_linha_da_corretor(monkeypatch, tmp_path):
    chamadas = _capturar_midia(monkeypatch, tmp_path)
    assert cadencia._enviar_audio(_Est(CORRETOR_ID)) is True
    assert chamadas[0]["linha"].slug == "corretor"
    assert chamadas[0]["corretora_id"] == CORRETOR_ID


def test_audio_da_corretor_segue_igual(monkeypatch, tmp_path):
    chamadas = _capturar_midia(monkeypatch, tmp_path)
    assert cadencia._enviar_audio(_Est(3)) is True
    assert "linha" not in chamadas[0]  # sem linha = 2591 da Pessoa12, como sempre


def test_audio_sem_linha_da_dona_nao_cai_na_da_corretor(monkeypatch, tmp_path):
    chamadas = _capturar_midia(monkeypatch, tmp_path)
    monkeypatch.setattr(cloud_lines, "_BY_SLUG", {})
    assert cadencia._enviar_audio(_Est(2)) is False  # Pessoa11 sem linha registrada
    assert chamadas == []
