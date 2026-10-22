"""Mensagem velha não ganha resposta da cadência em nenhuma linha (14/10/2026).

O api-disparos marca com `X-Cloud-Velho: 1` a mensagem que a Meta reentregou
com mais de 30 min de atraso. Até 14/10 só a linha da Corretor respeitava a marca:
nas linhas Pessoa10/Pessoa11/Pessoa12/Pessoa7/Corretor o hook da cadência rodava sem saber disso
e, com régua viva, mandava o áudio da corretora (clique ou "sim, manda") ou a
despedida do "já encontrei" pra uma mensagem de horas atrás. Achado no
reprocesso da Pessoa11.

Regra: com `velho`, status, eventos e card andam igual — só NADA sai pro lead.

Banco: SQLite in-memory (mesmo padrão de test_cadencia_resposta_linha_corretora).
Nada sai pra Meta: `gravar` e todos os envios são substituídos por captura.
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


SECRET = "segredo-teste-resposta-velha"
PHONE_ID_CORRETOR = "000000000000000"
CORRETOR_ID = 1
LEAD = "+5500000000000"


@pytest.fixture()
def factory(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine, tables=[
        Corretora.__table__, CadenciaEstado.__table__, CadenciaEvento.__table__,
        Lead.__table__, LeadEvent.__table__, PipelineStage.__table__,
        DisparoReengajamento.__table__,
    ])
    f = sessionmaker(bind=engine)
    with f() as s:
        s.add(Corretora(id=CORRETOR_ID, nome="Pessoa5", telefone="+5500000000000",
                        telefone_normalizado="+5500000000000", ativo=True,
                        evolution_instance="acme_corretor", papel="corretora"))
        s.commit()
    monkeypatch.setattr(db_session, "SessionLocal", f)
    return f


@pytest.fixture()
def envios(monkeypatch, tmp_path):
    """Captura TODO envio pro lead (texto e mídia) e isola o resto do inbound."""
    corretor = cloud_lines.CloudLine(phone_id=PHONE_ID_CORRETOR, slug="corretor", papel="corretora",
                                 env_file="/etc/agencia/review.env", token_var="META_REVIEW_TOKEN")
    monkeypatch.setattr(cloud_lines, "_LINES", {PHONE_ID_CORRETOR: corretor})
    monkeypatch.setattr(cloud_lines, "_BY_SLUG", {"corretor": PHONE_ID_CORRETOR})
    monkeypatch.setattr(settings, "cloud_webhook_secret", SECRET)
    monkeypatch.setattr(corretor, "gravar", lambda **kw: True)
    monkeypatch.setattr(corretor, "tratar_resposta_disparo", lambda *a, **k: None)
    monkeypatch.setattr(corretor, "_tratar_saida_remarketing", lambda *a, **k: None)
    monkeypatch.setattr(corretor, "tratar_intencao_indicacao", lambda *a, **k: None)
    monkeypatch.setattr(corretor, "_tratar_resposta_regua_proprietario", lambda *a, **k: None)
    monkeypatch.setattr(cloud_corretora.atendimento_corretor, "marcar_opt_out", lambda tel: None)
    monkeypatch.setattr(cloud_corretora.atendimento_corretor, "atender", lambda *a, **k: None)
    monkeypatch.setattr(cloud_corretora.cb, "processar_inbound", lambda *a, **k: None)
    from app.services import audios_corretora, saida_remarketing

    monkeypatch.setattr(saida_remarketing, "processar_inbound", lambda *a, **k: None)
    arq = tmp_path / "corretor.oga"
    arq.write_bytes(b"OggS")
    monkeypatch.setattr(audios_corretora, "caminho_audio", lambda cid: arq)

    saiu: list[tuple[str, str]] = []
    monkeypatch.setattr(corretor, "enviar_cloud",
                        lambda tel, texto, **k: saiu.append(("texto", tel)) or {"ok": True})
    monkeypatch.setattr(corretor, "enviar_cloud_midia",
                        lambda tel, dados, **k: saiu.append(("midia", tel)) or {"ok": True})
    cadencia._LAST_ENQUEUE["at"] = 0.0
    return saiu


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def _matricular(factory, *, no_toque_do_botao: bool = False) -> int:
    with factory() as s:
        est = cadencia.iniciar_cadencia(
            s, telefone=LEAD, lead_id="889", corretora_id=CORRETOR_ID,
            agora=datetime.now(timezone.utc) - timedelta(days=2))
        if no_toque_do_botao:
            est.toque_atual = cadencia.toque_com_botao().indice + 1
        s.commit()
        return est.id


_N = iter(range(10_000))


def _inbound(client, slug: str, texto: str, *, tipo: str = "text", velho: bool):
    msg = {"from": LEAD, "id": f"wamid.velho.{next(_N)}", "type": tipo,
           "timestamp": "1759400000"}
    if tipo == "button":
        msg["button"] = {"text": texto}
    else:
        msg["text"] = {"body": texto}
    headers = {"X-Cloud-Secret": SECRET}
    if velho:
        headers["X-Cloud-Velho"] = "1"
    url = "/cloud/inbound" if slug == "corretor" else f"/cloud/corretora/{slug}/inbound"
    r = client.post(url, json={"messages": [msg], "contacts": [
        {"wa_id": LEAD, "profile": {"name": "Marta"}}]}, headers=headers)
    assert r.status_code == 200, r.text
    return r


def _estado(factory) -> CadenciaEstado:
    with factory() as s:
        est = s.query(CadenciaEstado).one()
        s.expunge(est)
        return est


def _eventos(factory) -> list[CadenciaEvento]:
    with factory() as s:
        evs = s.query(CadenciaEvento).order_by(CadenciaEvento.id).all()
        for e in evs:
            s.expunge(e)
        return evs


# ── "já encontrei" ───────────────────────────────────────────────────────────


def test_ja_encontrei_velho_encerra_sem_despedida(factory, envios, client):
    _matricular(factory)
    _inbound(client, "corretor", "obrigada, mas já encontrei um imóvel", velho=True)
    assert envios == [], "mensagem velha não ganha despedida"
    assert _estado(factory).status == cadencia.STATUS_JA_ENCONTROU


def test_ja_encontrei_novo_continua_com_despedida(factory, envios, client):
    """Contraprova: a mesma mensagem, fresca, recebe a despedida."""
    _matricular(factory)
    _inbound(client, "corretor", "obrigada, mas já encontrei um imóvel", velho=False)
    assert envios == [("texto", LEAD)]
    assert _estado(factory).status == cadencia.STATUS_JA_ENCONTROU


# ── clique no botão do áudio ─────────────────────────────────────────────────


def test_clique_velho_registra_sem_mandar_audio(factory, envios, client):
    _matricular(factory, no_toque_do_botao=True)
    _inbound(client, "corretor", "Saiba mais", tipo="button", velho=True)
    assert envios == [], "clique velho não recebe áudio"
    assert _estado(factory).status == "ativa"
    evs = [e for e in _eventos(factory) if e.tipo == cadencia.TIPO_CLIQUE_BOTAO]
    assert len(evs) == 1
    assert evs[0].payload["audio_enviado"] is False
    assert evs[0].payload["velho"] is True


def test_clique_novo_continua_mandando_audio(factory, envios, client):
    _matricular(factory, no_toque_do_botao=True)
    _inbound(client, "corretor", "Saiba mais", tipo="button", velho=False)
    assert envios == [("midia", LEAD)]


# ── "sim, manda" em texto ────────────────────────────────────────────────────


def test_pedido_de_audio_velho_vira_resposta_sem_audio(factory, envios, client):
    _matricular(factory, no_toque_do_botao=True)
    _inbound(client, "corretor", "sim, manda", velho=True)
    assert envios == []
    # a régua para e o card anda igual — só a fala é que não sai
    assert _estado(factory).status == cadencia.STATUS_RESPONDIDA
    assert [e.tipo for e in _eventos(factory)] == [cadencia.TIPO_RESPOSTA]


def test_pedido_de_audio_novo_continua_mandando(factory, envios, client):
    _matricular(factory, no_toque_do_botao=True)
    _inbound(client, "corretor", "sim, manda", velho=False)
    assert envios == [("midia", LEAD)]
    assert _estado(factory).status == cadencia.STATUS_RESPONDIDA


# ── linha da Pessoa12 (cloud_corretora_a.cloud_inbound) ───────────────────────────────


def test_linha_da_corretor_repassa_o_velho_pro_hook(monkeypatch, envios, client):
    chamados: list = []
    monkeypatch.setattr(corretor, "_tratar_resposta_cadencia_inbound",
                        lambda *a, **k: chamados.append((a, k)))
    _inbound(client, "corretor", "já encontrei", velho=True)
    _inbound(client, "corretor", "já encontrei", velho=False)
    assert chamados == [((LEAD, "já encontrei", "text"), {"velho": True}),
                        ((LEAD, "já encontrei", "text"), {"velho": False})]


def test_linha_da_corretora_repassa_o_velho_pro_hook(monkeypatch, envios, client):
    chamados: list = []
    monkeypatch.setattr(corretor, "_tratar_resposta_cadencia_inbound",
                        lambda *a, **k: chamados.append((a, k)))
    _inbound(client, "corretor", "oi", velho=True)
    assert chamados == [((LEAD, "oi", "text"), {"velho": True})]
