"""Métricas da BOAS-VINDAS (GET /painel/automacoes/boas-vindas/metricas).

Cobre:
  - envio conta só o que SAIU de verdade (payload.sent != True vira `simuladas`);
  - resposta = inbound do lead DEPOIS da boas-vindas (antes não conta);
  - zero envio → taxa=None + sem_dado=True ("sem dado" ≠ 0%);
  - amostra pequena sinalizada abaixo de AMOSTRA_MINIMA;
  - distribuição por corretora + participação (a roleta está equilibrada?);
  - placar do A/B pela variante GRAVADA no envio, e envio antigo (sem variante)
    à parte — nunca somado no controle;
  - janela de dias respeitada;
  - admin-only (corretora → 403).

Nenhum id de variante ou de corretora é fixado: variante vem de
`boas_vindas_copies.VARIANTES`, corretora vem do que o teste cadastra.

DB isolado: SQLite in-memory (mesmo padrão de test_metricas_automacao.py).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import BigInteger, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.api.automacoes_boas_vindas as bv_api
from app.db.models import (
    Base,
    Corretora,
    Lead,
    LeadEvent,
    MensagemWhatsapp,
    ReguaMarco,
)
from app.main import app
from app.services import boas_vindas_copies as bvc
from app.services import lead_intake as li


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(element, compiler, **kw):  # noqa: ANN001
    return "JSON"


@compiles(BigInteger, "sqlite")
def _compile_bigint_sqlite(element, compiler, **kw):  # noqa: ANN001
    return "INTEGER"


AGORA = datetime.now(timezone.utc)
ADMIN = {"X-Painel-User": "dona"}
V_CONTROLE = bvc.CONTROLE_ID
V_OUTRA = next(v.id for v in bvc.VARIANTES if v.id != V_CONTROLE)


@pytest.fixture()
def factory(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        engine,
        tables=[
            Lead.__table__,
            LeadEvent.__table__,
            MensagemWhatsapp.__table__,
            Corretora.__table__,
            ReguaMarco.__table__,
        ],
    )
    f = sessionmaker(bind=engine)
    monkeypatch.setattr(bv_api, "SessionLocal", f)
    # roleta do teste: as duas corretoras que ele cadastra
    from app.services import config_operacional as cfg

    monkeypatch.setattr(cfg, "roleta_corretoras", lambda: [1, 2])
    return f


@pytest.fixture()
def client(factory):
    return TestClient(app)


def _corretora(session, cid: int, nome: str) -> None:
    session.add(Corretora(id=cid, nome=nome, telefone=f"+5500000000000{cid:03d}",
                          ativo=True, created_at=AGORA))
    session.flush()


def _lead(session, *, lead_id: int, telefone: str, owner: int | None) -> Lead:
    lead = Lead(id=lead_id, tenant_id=1, nome=f"Lead {lead_id}", telefone=telefone,
                owner_id=owner, created_at=AGORA, updated_at=AGORA)
    session.add(lead)
    session.flush()
    return lead


def _boas_vindas(session, lead, *, at, sent=True, variante=V_CONTROLE, owner=None):
    payload = {"texto": "oi", "sent": sent, "dry_run": not sent}
    if variante is not None:
        payload["variante"] = variante
    if owner is not None:
        payload["corretora_id"] = owner
    session.add(LeadEvent(tenant_id=1, lead_id=lead.id, tipo=li.EVENT_BOAS_VINDAS,
                          payload=payload, at=at))
    session.flush()


def _inbound(session, telefone: str, *, at):
    session.add(MensagemWhatsapp(
        id=uuid.uuid4(),  # SQLite não tem gen_random_uuid()
        tenant_id=1, evolution_message_id=f"m{telefone}{at.timestamp()}",
        instance="cloud_corretor", remote_jid=f"{telefone}@s.whatsapp.net",
        contato_telefone=telefone, from_corretora=False, tipo_mensagem="conversation",
        conteudo_texto="quero sim", timestamp_evolution=at, recebido_em=at,
    ))
    session.flush()


def _por_variante(dados) -> dict[str, dict]:
    return {v["id"]: v for v in dados["variantes"]}


def _por_corretora(dados) -> dict[int | None, dict]:
    return {c["corretora_id"]: c for c in dados["por_corretora"]}


# ── 1. envio e resposta ─────────────────────────────────────────────────────


def test_conta_envio_e_resposta(factory):
    with factory() as s:
        _corretora(s, 1, "Pessoa5")
        for i in range(4):
            tel = f"+5500000000000{i:04d}"
            lead = _lead(s, lead_id=100 + i, telefone=tel, owner=1)
            _boas_vindas(s, lead, at=AGORA - timedelta(days=3), owner=1)
            if i == 0:
                _inbound(s, tel, at=AGORA - timedelta(days=2))
        s.commit()
        dados = bv_api.calcular_metricas_boas_vindas(s, dias=30, agora=AGORA)

    assert dados["total"]["enviadas"] == 4
    assert dados["total"]["responderam"] == 1
    assert dados["total"]["taxa"] == pytest.approx(25)
    assert dados["total"]["sem_dado"] is False


def test_dry_run_nao_entra_na_taxa(factory):
    """Em simulação o lead não recebeu nada — não teve chance de responder."""
    with factory() as s:
        _corretora(s, 1, "Pessoa5")
        lead = _lead(s, lead_id=1, telefone="+5500000000000", owner=1)
        _boas_vindas(s, lead, at=AGORA - timedelta(days=1), sent=False, owner=1)
        s.commit()
        dados = bv_api.calcular_metricas_boas_vindas(s, dias=30, agora=AGORA)

    assert dados["total"]["simuladas"] == 1
    assert dados["total"]["enviadas"] == 0
    assert dados["total"]["taxa"] is None
    assert dados["total"]["sem_dado"] is True


def test_resposta_anterior_ao_envio_nao_conta(factory):
    with factory() as s:
        _corretora(s, 1, "Pessoa5")
        tel = "+5500000000000"
        lead = _lead(s, lead_id=2, telefone=tel, owner=1)
        _inbound(s, tel, at=AGORA - timedelta(days=5))
        _boas_vindas(s, lead, at=AGORA - timedelta(days=3), owner=1)
        s.commit()
        dados = bv_api.calcular_metricas_boas_vindas(s, dias=30, agora=AGORA)

    assert dados["total"]["enviadas"] == 1
    assert dados["total"]["responderam"] == 0
    assert dados["total"]["taxa"] == 0.0  # recebeu e ninguém respondeu ≠ sem dado
    assert dados["total"]["sem_dado"] is False


def test_sem_envio_nenhum_e_sem_dado_nao_zero(factory):
    with factory() as s:
        _corretora(s, 1, "Pessoa5")
        s.commit()
        dados = bv_api.calcular_metricas_boas_vindas(s, dias=30, agora=AGORA)

    assert dados["total"]["enviadas"] == 0
    assert dados["total"]["taxa"] is None
    assert dados["total"]["sem_dado"] is True
    assert dados["roleta"]["sem_dado"] is True
    for c in dados["por_corretora"]:
        assert c["participacao"] is None


def test_amostra_pequena_sinalizada(factory):
    with factory() as s:
        _corretora(s, 1, "Pessoa5")
        for i in range(3):
            tel = f"+5500000000000{i:04d}"
            lead = _lead(s, lead_id=300 + i, telefone=tel, owner=1)
            _boas_vindas(s, lead, at=AGORA - timedelta(days=2), owner=1)
        s.commit()
        dados = bv_api.calcular_metricas_boas_vindas(s, dias=30, agora=AGORA)

    assert dados["total"]["amostra_pequena"] is True
    assert dados["total"]["enviadas"] < dados["amostra_minima"]


def test_janela_de_dias_respeitada(factory):
    with factory() as s:
        _corretora(s, 1, "Pessoa5")
        lead = _lead(s, lead_id=9, telefone="+5500000000000", owner=1)
        _boas_vindas(s, lead, at=AGORA - timedelta(days=40), owner=1)
        s.commit()
        dados = bv_api.calcular_metricas_boas_vindas(s, dias=30, agora=AGORA)

    assert dados["total"]["enviadas"] == 0


def test_marco_de_regua_corta_o_historico(factory):
    with factory() as s:
        _corretora(s, 1, "Pessoa5")
        lead = _lead(s, lead_id=11, telefone="+5500000000000", owner=1)
        _boas_vindas(s, lead, at=AGORA - timedelta(days=10), owner=1)
        s.add(ReguaMarco(criado_em=AGORA - timedelta(days=5), motivo="copies novas",
                         criado_por="dona"))
        s.commit()
        dados = bv_api.calcular_metricas_boas_vindas(s, dias=30, agora=AGORA)

    assert dados["medindo_desde"] is not None
    assert dados["motivo_do_marco"] == "copies novas"
    assert dados["total"]["enviadas"] == 0  # o envio é anterior ao marco


# ── 2. distribuição da roleta ───────────────────────────────────────────────


def test_distribuicao_por_corretora(factory):
    with factory() as s:
        _corretora(s, 1, "Pessoa5")
        _corretora(s, 2, "Pessoa1")
        for i in range(3):
            lead = _lead(s, lead_id=400 + i, telefone=f"+5500000000000{i:04d}", owner=1)
            _boas_vindas(s, lead, at=AGORA - timedelta(days=1), owner=1)
        lead = _lead(s, lead_id=500, telefone="+5500000000000", owner=2)
        _boas_vindas(s, lead, at=AGORA - timedelta(days=1), owner=2)
        s.commit()
        dados = bv_api.calcular_metricas_boas_vindas(s, dias=30, agora=AGORA)

    porc = _por_corretora(dados)
    assert porc[1]["enviadas"] == 3
    assert porc[1]["participacao"] == pytest.approx(75)
    assert porc[2]["enviadas"] == 1
    assert porc[2]["participacao"] == pytest.approx(25)
    assert dados["roleta"]["corretoras"] == 2
    assert dados["roleta"]["participacao_ideal"] == pytest.approx(50)
    assert porc[1]["na_roleta"] is True
    assert porc[1]["posicao_roleta"] == 1


def test_corretora_da_roleta_sem_envio_aparece_com_sem_dado(factory):
    with factory() as s:
        _corretora(s, 1, "Pessoa5")
        _corretora(s, 2, "Pessoa1")
        lead = _lead(s, lead_id=600, telefone="+5500000000000", owner=1)
        _boas_vindas(s, lead, at=AGORA - timedelta(days=1), owner=1)
        s.commit()
        dados = bv_api.calcular_metricas_boas_vindas(s, dias=30, agora=AGORA)

    porc = _por_corretora(dados)
    assert 2 in porc
    assert porc[2]["enviadas"] == 0
    assert porc[2]["taxa"] is None
    assert porc[2]["sem_dado"] is True


# ── 3. placar do A/B ────────────────────────────────────────────────────────


def test_placar_por_variante_gravada(factory):
    with factory() as s:
        _corretora(s, 1, "Pessoa5")
        # 2 no controle (1 respondeu) e 1 na outra variante (sem resposta)
        for i, (vid, responde) in enumerate(
            ((V_CONTROLE, True), (V_CONTROLE, False), (V_OUTRA, False))
        ):
            tel = f"+5500000000000{i:04d}"
            lead = _lead(s, lead_id=700 + i, telefone=tel, owner=1)
            _boas_vindas(s, lead, at=AGORA - timedelta(days=2), variante=vid, owner=1)
            if responde:
                _inbound(s, tel, at=AGORA - timedelta(days=1))
        s.commit()
        dados = bv_api.calcular_metricas_boas_vindas(s, dias=30, agora=AGORA)

    porv = _por_variante(dados)
    assert porv[V_CONTROLE]["enviadas"] == 2
    assert porv[V_CONTROLE]["responderam"] == 1
    assert porv[V_CONTROLE]["taxa"] == pytest.approx(50)
    assert porv[V_OUTRA]["enviadas"] == 1
    assert porv[V_OUTRA]["taxa"] == 0.0
    # toda variante conhecida aparece, mesmo sem envio
    assert set(v.id for v in bvc.VARIANTES) <= set(porv)


def test_envio_antigo_sem_variante_fica_a_parte(factory):
    """Somar no controle inflaria a variante A com histórico de antes do teste."""
    with factory() as s:
        _corretora(s, 1, "Pessoa5")
        lead = _lead(s, lead_id=800, telefone="+5500000000000", owner=1)
        _boas_vindas(s, lead, at=AGORA - timedelta(days=2), variante=None, owner=1)
        s.commit()
        dados = bv_api.calcular_metricas_boas_vindas(s, dias=30, agora=AGORA)

    porv = _por_variante(dados)
    assert porv[V_CONTROLE]["enviadas"] == 0
    assert porv[bv_api.VARIANTE_NAO_REGISTRADA]["enviadas"] == 1


def test_copy_fora_do_ab_aparece_em_linha_propria(factory):
    """A copy de continuidade (quem veio de ligação) fica fora do sorteio, mas
    SAIU — se ela não aparecer, o placar não fecha com o total lá em cima."""
    from app.services import boas_vindas_continuidade as bvcont

    with factory() as s:
        _corretora(s, 1, "Pessoa5")
        lead = _lead(s, lead_id=900, telefone="+5500000000000", owner=1)
        _boas_vindas(s, lead, at=AGORA - timedelta(days=1),
                     variante=bvcont.COPY_ID, owner=1)
        s.commit()
        dados = bv_api.calcular_metricas_boas_vindas(s, dias=30, agora=AGORA)

    porv = _por_variante(dados)
    linha = porv[bvcont.COPY_ID]
    assert linha["enviadas"] == 1
    assert linha["estado"] == "fora do A/B"
    assert "continuidade" in linha["rotulo"]
    # e o placar fecha com o total
    somado = sum(v["enviadas"] for v in dados["variantes"])
    assert somado == dados["total"]["enviadas"] == 1


def test_variante_aguardando_nao_e_aposentada(factory):
    """C está cadastrada esperando a 2ª rodada: aparece no placar com o estado
    certo. 'Aguardando' e 'aposentada' são coisas diferentes."""
    from app.services import boas_vindas_copies as bvc

    with factory() as s:
        s.commit()
        dados = bv_api.calcular_metricas_boas_vindas(s, dias=30, agora=AGORA)

    porv = _por_variante(dados)
    for v in bvc.todas():
        assert porv[v.id]["estado"] == bvc.estado_da_variante(v)
        assert porv[v.id]["aguardando"] == v.aguardando


def test_placar_traz_peso_e_ativa_de_cada_variante(factory):
    with factory() as s:
        s.commit()
        dados = bv_api.calcular_metricas_boas_vindas(s, dias=30, agora=AGORA)

    porv = _por_variante(dados)
    for v in bvc.todas():
        assert porv[v.id]["peso"] == v.peso
        assert porv[v.id]["ativa"] == v.ativa
    assert porv[V_CONTROLE]["controle"] is True


# ── 4. acesso ───────────────────────────────────────────────────────────────


def test_endpoint_admin_ok(client):
    r = client.get("/painel/automacoes/boas-vindas/metricas?dias=30", headers=ADMIN)
    assert r.status_code == 200
    body = r.json()
    for chave in ("total", "por_corretora", "variantes", "roleta", "definicao",
                  "amostra_minima", "gerado_em"):
        assert chave in body, chave


def test_endpoint_corretora_403(client):
    r = client.get("/painel/automacoes/boas-vindas/metricas",
                   headers={"X-Painel-User": "corretor"})
    assert r.status_code == 403


def test_dias_fora_da_faixa_422(client):
    r = client.get("/painel/automacoes/boas-vindas/metricas?dias=9999", headers=ADMIN)
    assert r.status_code == 422
