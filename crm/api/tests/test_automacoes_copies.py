"""Testes da edição de copies da cadência (overrides em `cadencia_copies`).

Cobre:
  - GET /painel/automacoes/copies: 403 sem header/corretora; lista os 6 itens
    editáveis (toques 0/2/3/5/7 + remarketing -1) com default = efetiva;
  - PUT: salva override (efetiva muda, personalizada=True) + validações
    (vazia, sem "?", placeholder novo, toque de ligação → 404);
  - DELETE: restaura o padrão (idempotente);
  - MOTOR: `cadencia.tick` enfileira a copy do OVERRIDE quando existe
    (toque e remarketing) e o default quando não — comportamento de envio
    (guards, cap, dry-run) intocado;
  - GET /painel/automacoes mostra a copy EFETIVA no mapa.

DB isolado: SQLite in-memory (mesmo padrão de test_cadencia.py). Zero prod,
zero envio — sender é fake.
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

import app.api.automacoes as automacoes
from app.db.models import (
    Base, CadenciaCopy, CadenciaEstado, CadenciaEvento, DisparoReengajamento, Lead,
)
from app.main import app
from app.services import cadencia


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(element, compiler, **kw):  # noqa: ANN001
    return "JSON"


@compiles(BigInteger, "sqlite")
def _compile_bigint_sqlite(element, compiler, **kw):  # noqa: ANN001
    return "INTEGER"


AGORA = datetime.now(timezone.utc)
ADMIN = {"X-Painel-User": "dona"}


@pytest.fixture()
def factory(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        engine,
        tables=[CadenciaEstado.__table__, CadenciaEvento.__table__, CadenciaCopy.__table__,
                # opt-out (07/10): o follow-up lê leads/disparos_reengajamento a cada
                # volta e, sem as tabelas, segura todo toque (fail-closed).
                Lead.__table__, DisparoReengajamento.__table__],
    )
    f = sessionmaker(bind=engine)
    monkeypatch.setattr(automacoes, "SessionLocal", f)
    cadencia._LAST_ENQUEUE["at"] = 0.0
    return f


@pytest.fixture()
def client(factory):
    return TestClient(app)


class FakeSender:
    def __init__(self):
        self.chamadas: list[dict] = []

    def __call__(self, telefone, texto, *, com_botao):
        self.chamadas.append({"telefone": telefone, "texto": texto, "com_botao": com_botao})
        return {"ok": True, "dry_run": True, "sent": False}


# ── GET /painel/automacoes/copies ─────────────────────────────────────────────

def test_copies_exige_auth(client):
    assert client.get("/painel/automacoes/copies").status_code == 403


def test_copies_corretora_403(client):
    r = client.get("/painel/automacoes/copies", headers={"X-Painel-User": "corretor"})
    assert r.status_code == 403


def test_copies_lista_defaults(client):
    r = client.get("/painel/automacoes/copies", headers=ADMIN)
    assert r.status_code == 200
    copies = r.json()["copies"]
    # Toques de MENSAGEM (ligação fica fora) + remarketing (-1). Derivado da
    # régua, não fixo: ela muda (o D3 de áudio entrou em 04/08).
    from app.services import cadencia as _cad
    esperado = [t.indice for t in _cad.TOQUES if t.tipo == _cad.TIPO_MENSAGEM] + [-1]
    assert [c["toque"] for c in copies] == esperado
    for c in copies:
        assert c["personalizada"] is False
        assert c["override"] is None
        assert c["efetiva"] == c["default"]
    assert copies[0]["default"] == cadencia._TEMPLATES_MSG[0]
    assert copies[-1]["default"] == cadencia._TEMPLATE_REMARKETING


# ── PUT (salvar override) + validações ────────────────────────────────────────

NOVA_COPY = "Sua busca continua de pé? Me conta que eu te ajudo por aqui mesmo?"


def test_put_salva_override(client):
    r = client.put(
        "/painel/automacoes/copies/0", headers=ADMIN, json={"texto": NOVA_COPY}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["personalizada"] is True
    assert body["efetiva"] == NOVA_COPY

    lista = client.get("/painel/automacoes/copies", headers=ADMIN).json()["copies"]
    t0 = next(c for c in lista if c["toque"] == 0)
    assert t0["efetiva"] == NOVA_COPY
    assert t0["override"] == NOVA_COPY
    assert t0["personalizada"] is True
    assert t0["default"] == cadencia._TEMPLATES_MSG[0]  # default preservado
    assert t0["atualizado_por"] == "dona"


def test_put_valida_vazia(client):
    r = client.put("/painel/automacoes/copies/0", headers=ADMIN, json={"texto": "   "})
    assert r.status_code == 400
    assert "copy_vazia" in r.json()["detail"]


def test_put_valida_pergunta(client):
    r = client.put(
        "/painel/automacoes/copies/0", headers=ADMIN, json={"texto": "Oi, tudo bem."}
    )
    assert r.status_code == 400
    assert "copy_sem_pergunta" in r.json()["detail"]


def test_put_pergunta_com_emoji_no_fim_ok(client):
    r = client.put(
        "/painel/automacoes/copies/0",
        headers=ADMIN,
        json={"texto": "Ainda tá procurando imóvel? 🏠"},
    )
    assert r.status_code == 200


def test_put_valida_placeholder(client):
    r = client.put(
        "/painel/automacoes/copies/0",
        headers=ADMIN,
        json={"texto": "Oi {nome}, ainda procura imóvel?"},
    )
    assert r.status_code == 400
    assert "placeholder_nao_suportado" in r.json()["detail"]


def test_put_toque_ligacao_404(client):
    # toques 1/4/6 são tarefa de ligação — não têm copy editável
    r = client.put(
        "/painel/automacoes/copies/1", headers=ADMIN, json={"texto": "Alguma copy?"}
    )
    assert r.status_code == 404


def test_put_corretora_403(client):
    r = client.put(
        "/painel/automacoes/copies/0",
        headers={"X-Painel-User": "corretor"},
        json={"texto": NOVA_COPY},
    )
    assert r.status_code == 403


# ── DELETE (restaurar padrão) ─────────────────────────────────────────────────

def test_delete_restaura_padrao(client):
    client.put("/painel/automacoes/copies/2", headers=ADMIN, json={"texto": NOVA_COPY})
    r = client.delete("/painel/automacoes/copies/2", headers=ADMIN)
    assert r.status_code == 200
    assert r.json()["efetiva"] == cadencia._TEMPLATES_MSG[2]
    lista = client.get("/painel/automacoes/copies", headers=ADMIN).json()["copies"]
    t2 = next(c for c in lista if c["toque"] == 2)
    assert t2["personalizada"] is False and t2["override"] is None
    # idempotente: apagar de novo não erra
    assert client.delete("/painel/automacoes/copies/2", headers=ADMIN).status_code == 200


# ── MOTOR: tick usa a copy efetiva (override quando existe) ──────────────────

def test_tick_usa_override_no_toque(factory):
    with factory() as s:
        s.add(CadenciaCopy(tenant_id=1, toque=0, texto=NOVA_COPY))
        cadencia.iniciar_cadencia(s, telefone="+5500000000000", agora=AGORA)
        s.commit()

    sender = FakeSender()
    with factory() as s:
        res = cadencia.tick(agora=AGORA, session=s, sender=sender)
    assert res.mensagens_enfileiradas == 1
    assert sender.chamadas[0]["texto"] == NOVA_COPY

    # payload do evento registra o texto realmente enfileirado
    with factory() as s:
        ev = s.query(CadenciaEvento).filter(CadenciaEvento.toque == 0).one()
        assert ev.payload["texto"] == NOVA_COPY


def test_tick_sem_override_usa_default(factory):
    with factory() as s:
        cadencia.iniciar_cadencia(s, telefone="+5500000000000", agora=AGORA)
        s.commit()

    sender = FakeSender()
    with factory() as s:
        cadencia.tick(agora=AGORA, session=s, sender=sender)
    assert sender.chamadas[0]["texto"] == cadencia.aplicar_assinatura(
        cadencia._TEMPLATES_MSG[0], 3
    )   # 3 = Pessoa12 ("Pessoa"); a assinatura agora é a de quem manda


def test_tick_remarketing_usa_override(factory):
    rmk = "Apareceram imóveis novos com a sua cara. Quer que eu te mande?"
    with factory() as s:
        s.add(CadenciaCopy(tenant_id=1, toque=cadencia.TOQUE_REMARKETING, texto=rmk))
        s.add(CadenciaEstado(
            tenant_id=1, telefone="+5500000000000", toque_atual=8,
            status="remarketing", proximo_toque_em=AGORA - timedelta(hours=1),
            criado_em=AGORA - timedelta(days=40), atualizado_em=AGORA,
        ))
        s.commit()

    sender = FakeSender()
    with factory() as s:
        res = cadencia.tick(agora=AGORA, session=s, sender=sender)
    assert res.remarketing == 1
    assert sender.chamadas[0]["texto"] == rmk


def test_copy_efetiva_sem_tabela_degrada_pro_default():
    # árvore sem a migration (tabela ausente): devolve default, não levanta
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[CadenciaEstado.__table__])
    f = sessionmaker(bind=engine)
    with f() as s:
        assert cadencia.copy_efetiva(s, 0) == cadencia._TEMPLATES_MSG[0]


# ── Mapa /painel/automacoes reflete a copy efetiva ────────────────────────────

def test_mapa_automacoes_mostra_efetiva(client, factory):
    from app.services import cadencia as _cad

    with factory() as s:
        s.add(CadenciaCopy(tenant_id=1, toque=_cad.toque_encerramento(),
                           texto=NOVA_COPY))
        s.commit()
    r = client.get("/painel/automacoes", headers=ADMIN)
    assert r.status_code == 200
    toques = r.json()["cadencia"]["toques"]
    _ult = _cad.toque_encerramento()
    t7 = next(t for t in toques if t["indice"] == _ult)
    assert t7["copy"] == NOVA_COPY
    assert t7["copy_padrao"] == cadencia._TEMPLATES_MSG[_ult]
    assert t7["personalizada"] is True
