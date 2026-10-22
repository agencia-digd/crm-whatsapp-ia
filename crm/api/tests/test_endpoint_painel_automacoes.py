"""GET /painel/automacoes — mapa vivo das automações.

Cobre:
  - 403 sem header / usuário desconhecido / corretora (admin-only);
  - 200 admin com os blocos (operacao / boas_vindas / cadencia / nutricao / gatilhos);
  - cronograma dos 8 toques com copies reais (D0 = reencontro, sempre);
  - bloco boas_vindas (automação separada da cadência — lei Dona 10/07);
  - nutrição com marcos 30/60/90/120/150 + copies;
  - gatilho auto_followup_24h refletindo a flag (default: desligado);
  - matriculados contados de cadencia_estado (por status).

DB isolado: SQLite in-memory (mesmo padrão de test_endpoint_painel_cadencia.py)
injetado via monkeypatch de automacoes.SessionLocal. Zero prod, zero envio.
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
from app.db.models import Base, CadenciaEstado
from app.main import app
from app.services import cadencia as _cad_mod


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(element, compiler, **kw):  # noqa: ANN001
    return "JSON"


@compiles(BigInteger, "sqlite")
def _compile_bigint_sqlite(element, compiler, **kw):  # noqa: ANN001
    return "INTEGER"


@compiles(__import__("sqlalchemy.dialects.postgresql", fromlist=["UUID"]).UUID, "sqlite")
def _compile_uuid_sqlite(element, compiler, **kw):  # noqa: ANN001
    return "CHAR(36)"


AGORA = datetime.now(timezone.utc)


@pytest.fixture()
def client(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=[CadenciaEstado.__table__])
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(automacoes, "SessionLocal", factory)
    with factory() as s:
        s.add_all([
            CadenciaEstado(tenant_id=1, lead_id="kommo_1", telefone="+5500000000000",
                           corretora_id=3, toque_atual=1, status="ativa",
                           proximo_toque_em=AGORA + timedelta(hours=2),
                           criado_em=AGORA - timedelta(days=1), atualizado_em=AGORA),
            CadenciaEstado(tenant_id=1, lead_id="kommo_2", telefone="+5500000000000",
                           corretora_id=3, toque_atual=8, status="remarketing",
                           proximo_toque_em=AGORA + timedelta(days=30),
                           criado_em=AGORA - timedelta(days=15), atualizado_em=AGORA),
            CadenciaEstado(tenant_id=1, lead_id="kommo_3", telefone="+5500000000000",
                           corretora_id=3, toque_atual=3, status="opt_out",
                           proximo_toque_em=None,
                           criado_em=AGORA - timedelta(days=5), atualizado_em=AGORA),
        ])
        s.commit()
    return TestClient(app)


def test_sem_header_403(client):
    assert client.get("/painel/automacoes").status_code == 403


def test_usuario_desconhecido_403(client):
    r = client.get("/painel/automacoes", headers={"X-Painel-User": "intruso"})
    assert r.status_code == 403


def test_corretora_403(client):
    r = client.get("/painel/automacoes", headers={"X-Painel-User": "corretor"})
    assert r.status_code == 403
    assert r.json()["detail"] == "painel_admin_required"


def test_admin_mapa_completo(client):
    r = client.get("/painel/automacoes", headers={"X-Painel-User": "dona"})
    assert r.status_code == 200
    body = r.json()

    # blocos presentes
    for chave in ("operacao", "boas_vindas", "cadencia", "nutricao", "gatilhos", "todo"):
        assert chave in body, chave

    # operação: flags vivas + matriculados do DB fake
    op = body["operacao"]
    # dry_run reflete o env REAL (é o ponto do mapa vivo) — só valida o tipo
    assert isinstance(op["dry_run"], bool)
    assert op["matriculados"]["por_status"] == {
        "ativa": 1, "remarketing": 1, "opt_out": 1,
    }
    assert op["matriculados"]["total"] == 3
    assert op["timer"]["unit"] == "copiloto-cadencia-tick.timer"

    # cadência: 8 toques na ordem D0..D10, copies presentes
    toques = body["cadencia"]["toques"]
    assert len(toques) == len(_cad_mod.TOQUES)
    # Os dias vêm da régua, não de uma lista fixa: ela muda (o D3 de áudio
    # entrou em 04/08). Fixar aqui só fazia o teste quebrar sem apontar bug.
    assert [t["dia"] for t in toques] == [f"D{t.offset_dias}" for t in _cad_mod.TOQUES]
    d0 = toques[0]
    # D0-duplo morreu (lei Dona 10/07): copy simples, sempre o reencontro
    assert "copies" not in d0
    assert d0["copy"]
    msgs = [t for t in toques if t["tipo"] == "mensagem"]
    ligacoes = [t for t in toques if t["tipo"] == "tarefa_ligacao"]
    _msgs_regua = [t for t in _cad_mod.TOQUES if t.tipo == _cad_mod.TIPO_MENSAGEM]
    _lig_regua = [t for t in _cad_mod.TOQUES if t.tipo == _cad_mod.TIPO_TAREFA_LIGACAO]
    assert len(msgs) == len(_msgs_regua) and len(ligacoes) == len(_lig_regua)
    for t in msgs:
        assert t["copy"], f"toque {t['indice']} sem copy"
    assert toques[-1]["categoria"] == "marketing"  # D10 encerramento
    assert body["cadencia"]["remarketing"]["copy"]

    # nutrição: 5 etapas, marcos com copy (Base sem — ainda não disparou)
    nut = body["nutricao"]
    assert [m["etapa"] for m in nut["marcos"]] == [
        "Base", "30 dias", "60 dias", "90 dias", "120 dias", "150 dias",
    ]
    assert nut["marcos"][0]["copy"] is None
    for m in nut["marcos"][1:]:
        assert m["copy"] and m["tag"]

    # gatilhos: auto_followup_24h desligado (decisão Dona), resposta sempre ligada
    por_id = {g["id"]: g for g in body["gatilhos"]}
    assert por_id["auto_followup_24h"]["estado"] == "desligado"
    assert por_id["resposta_lead"]["estado"] == "ligado"
    assert por_id["opt_out"]["estado"] == "ligado"
    assert por_id["auto_enroll_followup"]["flag"] == "CADENCIA_AUTO_ENROLL"

    # boas-vindas: automação separada da cadência (lei Dona 10/07).
    # Nesta árvore app.services.lead_intake EXISTE → disponível; flag off
    # (default) → desligado, mas a copy aparece no mapa.
    bv = body["boas_vindas"]
    for chave in (
        "descricao", "gatilho", "acao", "copy", "estado", "flag", "disponivel",
        "preview", "com_botao", "cta",
    ):
        assert chave in bv, chave
    assert bv["disponivel"] is True
    from app.services.lead_intake import BOAS_VINDAS_TEXTO
    assert bv["copy"] == BOAS_VINDAS_TEXTO
    assert bv["com_botao"] is True  # Dona: Saiba mais, SEM Fico à disposição
    assert bv["preview"] and "Olá" in bv["preview"]
    assert "Saiba mais" in (bv["preview"] or "")  # botão no preview
    assert "Fico à disposição" not in (bv["preview"] or "")
    assert "cidade" in (bv["copy"] or "").lower()
    # corpo sem CTA duplicado — o template já fecha com o botão
    assert "saiba mais" not in (bv["copy"] or "").lower()
    assert bv["estado"] == "desligado"  # LEAD_INTAKE_ATIVO default False
    assert bv["flag"] == "LEAD_INTAKE_ATIVO"


# ── ZERO ≠ AUSÊNCIA (falha de leitura não pode virar 0 na tela) ──────────────


def test_estoque_do_carrossel_nao_vira_zero_quando_a_leitura_falha(
    client, monkeypatch
):
    """Banco fora / migration faltando aparecia pra Dona como 'não tem ninguém
    na fila'. Agora vem None + `indisponivel` com o motivo."""
    def _explode(*a, **kw):
        raise RuntimeError("relation \"leads\" does not exist")

    monkeypatch.setattr(automacoes, "_estoque_carrossel", _explode)
    body = client.get(
        "/painel/automacoes", headers={"X-Painel-User": "dona"}
    ).json()
    est = body["carrossel_corretor"]["estoque"]
    assert est["fila"] is None and est["disparado"] is None
    assert est["indisponivel"] is True
    assert "does not exist" in (est["motivo_indisponivel"] or "")
    # e a frase do gatilho não inventa número
    assert "não consegui ler" in body["carrossel_corretor"]["gatilho"]


def test_disparos_hoje_indisponivel_nao_e_zero(client):
    """Neste ambiente o SQLite não entende `AT TIME ZONE` — a contagem falha e
    tem que vir como indisponível, nunca como 0 disparos."""
    ritmo = client.get(
        "/painel/automacoes", headers={"X-Painel-User": "dona"}
    ).json()["carrossel_corretor"]["ritmo"]
    assert ritmo["disparos_hoje"] is None
    assert ritmo["disparos_hoje_indisponivel"] is True


def test_mapa_boas_vindas_mostra_os_tres_caminhos(client):
    """O desenho dizia que todo envio participa do A/B. O motor tem 3 caminhos
    — e o da ligação fica FORA do teste."""
    bv = client.get(
        "/painel/automacoes", headers={"X-Painel-User": "dona"}
    ).json()["boas_vindas"]
    caminhos = {c["id"]: c for c in bv["caminhos"]}
    assert set(caminhos) == {"cadastro_novo", "lead_existente", "continuidade_ligacao"}
    assert caminhos["lead_existente"]["no_ab"] is True
    assert caminhos["continuidade_ligacao"]["no_ab"] is False
    assert bv["continuidade"]["id"] == "continuidade_ligacao"
    assert "telefone" in bv["continuidade"]["texto"].lower()
    assert "JÁ EXISTENTE" in bv["gatilho"]
