"""Métricas por MARCO da nutrição (GET /painel/automacoes/nutricao/metricas).

Cobre:
  - taxa por marco (responderam / enviados) com atribuição last-touch;
  - mensagem de FOLLOW-UP no meio fecha a janela do marco (o crédito é de quem
    falou por último);
  - etapa de ENTRADA (Base) não tem taxa — ninguém recebeu nada lá;
  - "sem dado" ≠ 0%: zero envios devolve taxa=None e sem_dado=True;
  - amostra pequena sinalizada abaixo de AMOSTRA_MINIMA envios;
  - opt-out conta separado e fora da taxa;
  - só o que SAIU de verdade conta (payload.sent != True é ignorado);
  - marco de régua e janela de dias respeitados;
  - fila por etapa = estado de AGORA (leads parados em cada etapa);
  - fila indisponível (sem stages do funil) não derruba a métrica;
  - admin-only (corretora → 403, sem header → 403).

Nenhum nome de etapa é fixado no teste: tudo derivado de
`nutricao.STAGES_NUTRICAO` / `COPIES_MARCO`.

DB isolado: SQLite in-memory (mesmo padrão de test_metricas_automacao.py).
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

import app.api.automacoes_nutricao as api_nut
from app.api.automacoes import AMOSTRA_MINIMA
from app.db.models import (
    Base,
    CadenciaEstado,
    CadenciaEvento,
    Lead,
    PipelineStage,
    ReguaMarco,
)
from app.main import app
from app.services import cadencia as cad
from app.services import nutricao as nut


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(element, compiler, **kw):  # noqa: ANN001
    return "JSON"


@compiles(BigInteger, "sqlite")
def _compile_bigint_sqlite(element, compiler, **kw):  # noqa: ANN001
    return "INTEGER"


AGORA = datetime.now(timezone.utc)
ADMIN = {"X-Painel-User": "dona"}

# ── marcos derivados (nunca fixados) ─────────────────────────────────────────
MARCOS_COM_COPY = [e for e in nut.STAGES_NUTRICAO if nut.COPIES_MARCO.get(e)]
M0 = MARCOS_COM_COPY[0]
M1 = MARCOS_COM_COPY[1]
ENTRADA = nut.STAGE_BASE


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
            CadenciaEstado.__table__,
            CadenciaEvento.__table__,
            ReguaMarco.__table__,
            PipelineStage.__table__,
            Lead.__table__,
        ],
    )
    f = sessionmaker(bind=engine)
    monkeypatch.setattr(api_nut, "SessionLocal", f)
    return f


@pytest.fixture()
def client(factory):
    return TestClient(app)


def _cadencia(session, telefone: str) -> CadenciaEstado:
    est = CadenciaEstado(
        tenant_id=1,
        lead_id=telefone,
        telefone=telefone,
        corretora_id=3,
        toque_atual=0,
        status="remarketing",
        proximo_toque_em=AGORA,
        atualizado_em=AGORA,
    )
    session.add(est)
    session.flush()
    return est


def _evento(session, est, *, tipo, at, payload=None, toque=None):
    session.add(
        CadenciaEvento(
            tenant_id=1,
            cadencia_id=est.id,
            toque=toque,
            tipo=tipo,
            canal=cad.CANAL_WHATSAPP,
            payload=payload or {},
            at=at,
        )
    )
    session.flush()


def _marcou(session, est, marco: str, *, at, sent=True):
    """1 disparo de marco de nutrição (como `nutricao.tick` grava)."""
    _evento(
        session,
        est,
        tipo=cad.TIPO_MENSAGEM,
        at=at,
        payload={"nutricao": True, "marco": marco, "sent": sent, "com_botao": False},
    )


def _toque_followup(session, est, toque: int, *, at):
    _evento(session, est, tipo=cad.TIPO_MENSAGEM, at=at, toque=toque,
            payload={"sent": True})


def _respondeu(session, est, *, at, opt_out=False):
    _evento(session, est, tipo=cad.TIPO_RESPOSTA, at=at,
            payload={"opt_out": opt_out, "texto": "oi"})


def _por_etapa(dados) -> dict[str, dict]:
    return {m["etapa"]: m for m in dados["marcos"]}


# ── taxa ─────────────────────────────────────────────────────────────────────


def test_taxa_por_marco(factory):
    """4 receberam o 1º marco, 1 respondeu → 25%."""
    with factory() as s:
        for i in range(4):
            est = _cadencia(s, f"5500000000000{i:03d}")
            _marcou(s, est, M0, at=AGORA - timedelta(days=3))
            if i == 0:
                _respondeu(s, est, at=AGORA - timedelta(days=2))
        s.commit()
        dados = api_nut.calcular_metricas_nutricao(s, dias=30, agora=AGORA)

    m = _por_etapa(dados)[M0]
    assert m["enviados"] == 4
    assert m["responderam"] == 1
    assert m["taxa"] == pytest.approx(25)
    assert m["sem_dado"] is False
    assert dados["total"]["taxa"] == pytest.approx(25)


def test_resposta_vai_pro_ultimo_marco(factory):
    with factory() as s:
        est = _cadencia(s, "+5500000000000")
        _marcou(s, est, M0, at=AGORA - timedelta(days=40))
        _marcou(s, est, M1, at=AGORA - timedelta(days=3))
        _respondeu(s, est, at=AGORA - timedelta(days=2))
        s.commit()
        dados = api_nut.calcular_metricas_nutricao(s, dias=90, agora=AGORA)

    por = _por_etapa(dados)
    assert por[M0]["responderam"] == 0
    assert por[M0]["taxa"] == 0.0
    assert por[M1]["responderam"] == 1
    assert por[M1]["taxa"] == pytest.approx(100)


def test_mensagem_de_followup_fecha_a_janela_do_marco(factory):
    """Se um toque de follow-up saiu depois do marco, o crédito não é da nutrição."""
    toque = [t.indice for t in cad.TOQUES if t.tipo == cad.TIPO_MENSAGEM][0]
    with factory() as s:
        est = _cadencia(s, "+5500000000000")
        _marcou(s, est, M0, at=AGORA - timedelta(days=5))
        _toque_followup(s, est, toque, at=AGORA - timedelta(days=3))
        _respondeu(s, est, at=AGORA - timedelta(days=2))
        s.commit()
        dados = api_nut.calcular_metricas_nutricao(s, dias=30, agora=AGORA)

    m = _por_etapa(dados)[M0]
    assert m["enviados"] == 1
    assert m["responderam"] == 0


def test_so_conta_o_que_saiu_de_verdade(factory):
    with factory() as s:
        est = _cadencia(s, "+5500000000000")
        _marcou(s, est, M0, at=AGORA - timedelta(days=2), sent=False)
        s.commit()
        dados = api_nut.calcular_metricas_nutricao(s, dias=30, agora=AGORA)

    m = _por_etapa(dados)[M0]
    assert m["enviados"] == 0
    assert m["taxa"] is None
    assert m["sem_dado"] is True


# ── etapa de entrada / sem dado ──────────────────────────────────────────────


def test_etapa_de_entrada_nao_tem_taxa(factory):
    with factory() as s:
        dados = api_nut.calcular_metricas_nutricao(s, dias=30, agora=AGORA)

    entrada = _por_etapa(dados)[ENTRADA]
    assert entrada["tem_taxa"] is False
    assert entrada["copy"] is None
    assert entrada["taxa"] is None
    assert entrada["enviados"] is None


def test_zero_envios_nao_divide_por_zero(factory):
    with factory() as s:
        dados = api_nut.calcular_metricas_nutricao(s, dias=30, agora=AGORA)

    for m in dados["marcos"]:
        assert m["taxa"] is None
        assert m["sem_dado"] is True
    assert dados["total"]["taxa"] is None
    assert dados["total"]["sem_dado"] is True
    # o funil inteiro aparece, mesmo vazio
    assert len(dados["marcos"]) == len(nut.STAGES_NUTRICAO)
    assert dados["marcos"][-1]["final"] is True
    assert dados["etapa_final"] == nut.STAGE_FINAL
    assert dados["copies_editaveis"] is False


def test_amostra_pequena_sinalizada(factory):
    with factory() as s:
        for i in range(3):
            est = _cadencia(s, f"5500000000000{i:03d}")
            _marcou(s, est, M0, at=AGORA - timedelta(days=2))
            if i == 0:
                _respondeu(s, est, at=AGORA - timedelta(days=1))
        s.commit()
        dados = api_nut.calcular_metricas_nutricao(s, dias=30, agora=AGORA)

    m = _por_etapa(dados)[M0]
    assert m["enviados"] == 3
    assert m["amostra_pequena"] is True
    assert m["taxa"] == pytest.approx(100 / 3, abs=0.05)
    assert dados["amostra_minima"] == AMOSTRA_MINIMA


def test_amostra_grande_nao_marcada(factory):
    with factory() as s:
        for i in range(AMOSTRA_MINIMA):
            est = _cadencia(s, f"5500000000000{i:06d}")
            _marcou(s, est, M0, at=AGORA - timedelta(days=2))
        s.commit()
        dados = api_nut.calcular_metricas_nutricao(s, dias=30, agora=AGORA)

    m = _por_etapa(dados)[M0]
    assert m["enviados"] == AMOSTRA_MINIMA
    assert m["amostra_pequena"] is False
    assert m["taxa"] == 0.0  # enviou e ninguém respondeu: 0% é DADO


def test_opt_out_conta_separado_e_fora_da_taxa(factory):
    with factory() as s:
        for i in range(2):
            est = _cadencia(s, f"5500000000000{i:03d}")
            _marcou(s, est, M0, at=AGORA - timedelta(days=3))
            _respondeu(s, est, at=AGORA - timedelta(days=2), opt_out=(i == 0))
        s.commit()
        dados = api_nut.calcular_metricas_nutricao(s, dias=30, agora=AGORA)

    m = _por_etapa(dados)[M0]
    assert m["opt_outs"] == 1
    assert m["responderam"] == 1
    assert m["taxa"] == pytest.approx(50)


def test_marco_desconhecido_nao_infla_o_total(factory):
    """Etapa que não existe mais no motor fica fora da tabela E do total."""
    with factory() as s:
        est = _cadencia(s, "+5500000000000")
        _marcou(s, est, M0, at=AGORA - timedelta(days=3))
        _marcou(s, est, "999 dias", at=AGORA - timedelta(days=2))
        s.commit()
        dados = api_nut.calcular_metricas_nutricao(s, dias=30, agora=AGORA)

    soma = sum(m["enviados"] or 0 for m in dados["marcos"] if m["tem_taxa"])
    assert dados["total"]["enviados"] == soma == 1


# ── janela / marco de régua ──────────────────────────────────────────────────


def test_janela_de_dias_respeitada(factory):
    with factory() as s:
        velho = _cadencia(s, "+5500000000000")
        _marcou(s, velho, M0, at=AGORA - timedelta(days=45))
        novo = _cadencia(s, "+5500000000000")
        _marcou(s, novo, M0, at=AGORA - timedelta(days=2))
        s.commit()
        d30 = api_nut.calcular_metricas_nutricao(s, dias=30, agora=AGORA)
        d90 = api_nut.calcular_metricas_nutricao(s, dias=90, agora=AGORA)

    assert _por_etapa(d30)[M0]["enviados"] == 1
    assert _por_etapa(d90)[M0]["enviados"] == 2


def test_marco_de_regua_corta_o_que_veio_antes(factory):
    with factory() as s:
        antigo = _cadencia(s, "+5500000000000")
        _marcou(s, antigo, M0, at=AGORA - timedelta(days=20))
        novo = _cadencia(s, "+5500000000000")
        _marcou(s, novo, M0, at=AGORA - timedelta(days=2))
        s.add(ReguaMarco(criado_em=AGORA - timedelta(days=10),
                         motivo="copies novas", criado_por="dona"))
        s.commit()
        dados = api_nut.calcular_metricas_nutricao(s, dias=30, agora=AGORA)
        eventos = s.query(CadenciaEvento).count()

    assert _por_etapa(dados)[M0]["enviados"] == 1
    assert dados["motivo_do_marco"] == "copies novas"
    assert eventos == 2  # corte é de LEITURA — nada apagado


# ── fila por etapa (estado de AGORA) ─────────────────────────────────────────


def _stages(session) -> dict[str, PipelineStage]:
    out: dict[str, PipelineStage] = {}
    for ordem, nome in enumerate(nut.STAGES_NUTRICAO, start=1):
        st = PipelineStage(tenant_id=1, nome=nome, ordem=ordem,
                           funil=nut.FUNIL_REMARKETING)
        session.add(st)
        session.flush()
        out[nome] = st
    return out


def test_fila_conta_leads_parados_em_cada_etapa(factory):
    with factory() as s:
        stages = _stages(s)
        for i in range(2):
            s.add(Lead(tenant_id=1, nome=f"lead {i}", telefone=f"5500000000000{i}",
                       stage_id=stages[ENTRADA].id))
        s.add(Lead(tenant_id=1, nome="lead m0", telefone="5500000000000",
                   stage_id=stages[M0].id))
        s.commit()
        dados = api_nut.calcular_metricas_nutricao(s, dias=30, agora=AGORA)

    assert dados["fila"]["disponivel"] is True
    assert dados["fila"]["total"] == 3
    por = _por_etapa(dados)
    assert por[ENTRADA]["em_fila"] == 2
    assert por[M0]["em_fila"] == 1
    assert por[M1]["em_fila"] == 0


def test_fila_ignora_etapas_de_outra_automacao_no_mesmo_funil(factory):
    """O funil 'remarketing' também abriga o carrossel da Corretor ('Fila',
    'Disparado', 'Respondeu' — 5.246 leads medidos em 05/08). Contar o funil
    inteiro faria a tela dizer que a nutrição tem 7x o que tem."""
    with factory() as s:
        stages = _stages(s)
        alheia = PipelineStage(tenant_id=1, nome="Fila", ordem=99,
                               funil=nut.FUNIL_REMARKETING)
        s.add(alheia)
        s.flush()
        s.add(Lead(tenant_id=1, nome="da nutrição", telefone="5500000000000",
                   stage_id=stages[M0].id))
        for i in range(5):
            s.add(Lead(tenant_id=1, nome=f"do carrossel {i}",
                       telefone=f"5500000000000{i}", stage_id=alheia.id))
        s.commit()
        dados = api_nut.calcular_metricas_nutricao(s, dias=30, agora=AGORA)

    assert dados["fila"]["total"] == 1
    assert "Fila" not in dados["fila"]["por_etapa"]


def test_sem_agenda_conta_os_congelados_por_etapa(factory):
    """Lead na régua cujo estado está com `proximo_toque_em` NULL nunca mais
    recebe marco — o tick só processa data vencida. 548 assim em 05/08."""
    with factory() as s:
        stages = _stages(s)
        # congelado: estado sem próxima data, lead na etapa M0
        est = _cadencia(s, "+5500000000000")
        est.proximo_toque_em = None
        s.add(Lead(tenant_id=1, nome="congelado", telefone="+5500000000000",
                   stage_id=stages[M0].id))
        # saudável: tem data futura
        est2 = _cadencia(s, "+5500000000000")
        est2.proximo_toque_em = AGORA + timedelta(days=10)
        s.add(Lead(tenant_id=1, nome="ok", telefone="+5500000000000",
                   stage_id=stages[M0].id))
        s.commit()
        dados = api_nut.calcular_metricas_nutricao(s, dias=30, agora=AGORA)

    por = _por_etapa(dados)
    assert por[M0]["em_fila"] == 2
    assert por[M0]["sem_agenda"] == 1
    assert dados["fila"]["sem_agenda_total"] == 1


def test_sem_agenda_ignora_a_etapa_final(factory):
    """No marco final o NULL é o motor dizendo 'acabou' — não é congelamento."""
    final = nut.STAGE_FINAL
    with factory() as s:
        stages = _stages(s)
        est = _cadencia(s, "+5500000000000")
        est.proximo_toque_em = None
        s.add(Lead(tenant_id=1, nome="terminou", telefone="+5500000000000",
                   stage_id=stages[final].id))
        s.commit()
        dados = api_nut.calcular_metricas_nutricao(s, dias=30, agora=AGORA)

    por = _por_etapa(dados)
    assert por[final]["em_fila"] == 1
    assert por[final]["sem_agenda"] == 0
    assert dados["fila"]["sem_agenda_total"] == 0


def test_fila_indisponivel_nao_vira_zero(factory):
    """Sem as etapas do funil no banco, a tela diz 'sem dado' — não 'vazia'."""
    with factory() as s:
        dados = api_nut.calcular_metricas_nutricao(s, dias=30, agora=AGORA)

    assert dados["fila"]["disponivel"] is False
    assert dados["fila"]["total"] is None
    for m in dados["marcos"]:
        assert m["em_fila"] is None


# ── endpoint HTTP ────────────────────────────────────────────────────────────


def test_endpoint_exige_auth(client):
    assert client.get("/painel/automacoes/nutricao/metricas").status_code == 403


def test_endpoint_corretora_403(client):
    r = client.get("/painel/automacoes/nutricao/metricas",
                   headers={"X-Painel-User": "corretor"})
    assert r.status_code == 403


def test_endpoint_admin_ok(client, factory):
    with factory() as s:
        est = _cadencia(s, "+5500000000000")
        _marcou(s, est, M0, at=AGORA - timedelta(days=2))
        s.commit()
    r = client.get("/painel/automacoes/nutricao/metricas?dias=30", headers=ADMIN)
    assert r.status_code == 200
    body = r.json()
    assert body["intervalo_dias"] == nut.INTERVALO_DIAS
    assert _por_etapa(body)[M0]["enviados"] == 1


def test_endpoint_dias_fora_da_faixa(client):
    r = client.get("/painel/automacoes/nutricao/metricas?dias=0", headers=ADMIN)
    assert r.status_code == 422
