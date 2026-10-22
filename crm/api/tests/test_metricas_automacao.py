"""Métricas de engajamento por toque da cadência (GET /painel/automacoes/metricas).

Cobre:
  - taxa calculada certo (responderam / enviados) e atribuição da resposta ao
    ÚLTIMO toque de mensagem enviado antes dela;
  - toque de LIGAÇÃO não tem taxa (tem_taxa=False) — conta tarefas, não %;
  - amostra pequena sinalizada (< AMOSTRA_MINIMA envios);
  - zero envios NÃO dá divisão por zero: taxa=None + sem_dado=True
    ("sem dado" e "0%" são coisas diferentes);
  - opt-out conta separado e NÃO entra na taxa;
  - só o que SAIU de verdade conta (payload.sent != True é ignorado);
  - janela de dias respeitada;
  - admin-only (corretora → 403, sem header → 403).

Índice de toque NUNCA é fixado no teste: tudo derivado de `cadencia.TOQUES`
(a régua mudou em 04/08 com a entrada do D3 e quebrou 9 testes que fixavam 7/8).

DB isolado: SQLite in-memory (mesmo padrão de test_automacoes_copies.py).
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
from app.db.models import Base, CadenciaCopy, CadenciaEstado, CadenciaEvento, ReguaMarco
from app.main import app
from app.services import cadencia as cad


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(element, compiler, **kw):  # noqa: ANN001
    return "JSON"


@compiles(BigInteger, "sqlite")
def _compile_bigint_sqlite(element, compiler, **kw):  # noqa: ANN001
    return "INTEGER"


AGORA = datetime.now(timezone.utc)
ADMIN = {"X-Painel-User": "dona"}


# ── régua derivada (nunca fixada) ────────────────────────────────────────────
TOQUES_MSG = [t.indice for t in cad.TOQUES if t.tipo == cad.TIPO_MENSAGEM]
TOQUES_LIG = [t.indice for t in cad.TOQUES if t.tipo == cad.TIPO_TAREFA_LIGACAO]
T_MSG_0 = TOQUES_MSG[0]
T_MSG_1 = TOQUES_MSG[1]
T_LIG_0 = TOQUES_LIG[0]


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
            CadenciaCopy.__table__,
            ReguaMarco.__table__,
        ],
    )
    f = sessionmaker(bind=engine)
    monkeypatch.setattr(automacoes, "SessionLocal", f)
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
        status="ativa",
        proximo_toque_em=AGORA,
        atualizado_em=AGORA,
    )
    session.add(est)
    session.flush()
    return est


def _evento(session, est, *, toque, tipo, at, payload=None, canal="whatsapp"):
    session.add(
        CadenciaEvento(
            tenant_id=1,
            cadencia_id=est.id,
            toque=toque,
            tipo=tipo,
            canal=canal,
            payload=payload or {},
            at=at,
        )
    )
    session.flush()


def _enviou(session, est, toque, *, at, sent=True):
    _evento(session, est, toque=toque, tipo=cad.TIPO_MENSAGEM, at=at,
            payload={"sent": sent})


def _respondeu(session, est, *, at, opt_out=False):
    _evento(session, est, toque=None, tipo=cad.TIPO_RESPOSTA, at=at,
            payload={"opt_out": opt_out, "texto": "oi"})


def _por_indice(dados) -> dict[int, dict]:
    return {t["indice"]: t for t in dados["toques"]}


# ── taxa ─────────────────────────────────────────────────────────────────────


def test_taxa_calculada_certo(factory):
    """4 receberam o 1º toque de mensagem, 1 respondeu → 25%."""
    with factory() as s:
        for i in range(4):
            est = _cadencia(s, f"5500000000000{i:03d}")
            _enviou(s, est, T_MSG_0, at=AGORA - timedelta(days=3))
            if i == 0:
                _respondeu(s, est, at=AGORA - timedelta(days=2))
        s.commit()

        dados = automacoes.calcular_metricas(s, dias=30, agora=AGORA)

    t = _por_indice(dados)[T_MSG_0]
    assert t["enviados"] == 4
    assert t["responderam"] == 1
    assert t["taxa"] == pytest.approx(25)
    assert t["sem_dado"] is False
    assert dados["total"]["taxa"] == pytest.approx(25)


def test_resposta_vai_pro_ultimo_toque_enviado(factory):
    """Recebeu toque A, não respondeu; recebeu toque B e respondeu → crédito do B."""
    with factory() as s:
        est = _cadencia(s, "+5500000000000")
        _enviou(s, est, T_MSG_0, at=AGORA - timedelta(days=5))
        _enviou(s, est, T_MSG_1, at=AGORA - timedelta(days=3))
        _respondeu(s, est, at=AGORA - timedelta(days=2))
        s.commit()
        dados = automacoes.calcular_metricas(s, dias=30, agora=AGORA)

    por = _por_indice(dados)
    assert por[T_MSG_0]["responderam"] == 0
    assert por[T_MSG_0]["taxa"] == 0.0
    assert por[T_MSG_1]["responderam"] == 1
    assert por[T_MSG_1]["taxa"] == pytest.approx(100)


def test_so_conta_o_que_saiu_de_verdade(factory):
    """payload.sent falso (dry-run / toque pulado) não vira envio."""
    with factory() as s:
        est = _cadencia(s, "+5500000000000")
        _enviou(s, est, T_MSG_0, at=AGORA - timedelta(days=2), sent=False)
        s.commit()
        dados = automacoes.calcular_metricas(s, dias=30, agora=AGORA)

    t = _por_indice(dados)[T_MSG_0]
    assert t["enviados"] == 0
    assert t["taxa"] is None
    assert t["sem_dado"] is True


# ── ligação ──────────────────────────────────────────────────────────────────


def test_toque_de_ligacao_nao_tem_taxa(factory):
    with factory() as s:
        est = _cadencia(s, "+5500000000000")
        _evento(s, est, toque=T_LIG_0, tipo=cad.TIPO_TAREFA_LIGACAO,
                at=AGORA - timedelta(days=2), canal=cad.CANAL_LIGACAO,
                payload={"acao": "ligar"})
        s.commit()
        dados = automacoes.calcular_metricas(s, dias=30, agora=AGORA)

    t = _por_indice(dados)[T_LIG_0]
    assert t["tem_taxa"] is False
    assert t["taxa"] is None
    assert t["enviados"] is None
    assert t["tarefas"] == 1


def test_resposta_atravessa_a_ligacao(factory):
    """Ligação não abre janela nova: a resposta segue creditada à mensagem."""
    with factory() as s:
        est = _cadencia(s, "+5500000000000")
        _enviou(s, est, T_MSG_0, at=AGORA - timedelta(days=4))
        _evento(s, est, toque=T_LIG_0, tipo=cad.TIPO_TAREFA_LIGACAO,
                at=AGORA - timedelta(days=3), canal=cad.CANAL_LIGACAO,
                payload={"acao": "ligar"})
        _respondeu(s, est, at=AGORA - timedelta(days=2))
        s.commit()
        dados = automacoes.calcular_metricas(s, dias=30, agora=AGORA)

    assert _por_indice(dados)[T_MSG_0]["responderam"] == 1


# ── amostra pequena / zero ───────────────────────────────────────────────────


def test_amostra_pequena_sinalizada(factory):
    with factory() as s:
        for i in range(3):
            est = _cadencia(s, f"5500000000000{i:03d}")
            _enviou(s, est, T_MSG_0, at=AGORA - timedelta(days=2))
            if i == 0:
                _respondeu(s, est, at=AGORA - timedelta(days=1))
        s.commit()
        dados = automacoes.calcular_metricas(s, dias=30, agora=AGORA)

    t = _por_indice(dados)[T_MSG_0]
    assert t["enviados"] == 3
    assert t["amostra_pequena"] is True
    assert t["taxa"] == pytest.approx(100 / 3, abs=0.05)  # percentual, não fração
    assert dados["amostra_minima"] == automacoes.AMOSTRA_MINIMA


def test_amostra_grande_nao_marcada(factory):
    n = automacoes.AMOSTRA_MINIMA
    with factory() as s:
        for i in range(n):
            est = _cadencia(s, f"5500000000000{i:06d}")
            _enviou(s, est, T_MSG_0, at=AGORA - timedelta(days=2))
        s.commit()
        dados = automacoes.calcular_metricas(s, dias=30, agora=AGORA)

    t = _por_indice(dados)[T_MSG_0]
    assert t["enviados"] == n
    assert t["amostra_pequena"] is False
    assert t["taxa"] == 0.0  # enviou e ninguém respondeu: 0% é DADO, não ausência


def test_zero_envios_nao_divide_por_zero(factory):
    with factory() as s:
        dados = automacoes.calcular_metricas(s, dias=30, agora=AGORA)

    for t in dados["toques"]:
        assert t["taxa"] is None
        assert t["sem_dado"] is True
        assert t["amostra_pequena"] is False
    assert dados["total"]["taxa"] is None
    assert dados["total"]["sem_dado"] is True
    # a régua inteira aparece, mesmo sem dado nenhum
    assert len(dados["toques"]) == len(cad.TOQUES)


# ── opt-out ──────────────────────────────────────────────────────────────────


def test_opt_out_conta_separado_e_fora_da_taxa(factory):
    with factory() as s:
        for i in range(2):
            est = _cadencia(s, f"5500000000000{i:03d}")
            _enviou(s, est, T_MSG_0, at=AGORA - timedelta(days=3))
            _respondeu(s, est, at=AGORA - timedelta(days=2), opt_out=(i == 0))
        s.commit()
        dados = automacoes.calcular_metricas(s, dias=30, agora=AGORA)

    t = _por_indice(dados)[T_MSG_0]
    assert t["enviados"] == 2
    assert t["opt_outs"] == 1
    assert t["responderam"] == 1
    assert t["taxa"] == pytest.approx(50)


def test_total_fecha_com_a_tabela(factory):
    """Evento com índice que a régua de HOJE não tem (régua antiga) não pode
    inflar o total: tabela que não fecha com o total mata a confiança no número."""
    fantasma = max(t.indice for t in cad.TOQUES) + 5
    with factory() as s:
        est = _cadencia(s, "+5500000000000")
        _enviou(s, est, T_MSG_0, at=AGORA - timedelta(days=3))
        _enviou(s, est, fantasma, at=AGORA - timedelta(days=2))
        s.commit()
        dados = automacoes.calcular_metricas(s, dias=30, agora=AGORA)

    soma = sum(t["enviados"] or 0 for t in dados["toques"] if t["tem_taxa"])
    assert dados["total"]["enviados"] == soma == 1


# ── janela de período ────────────────────────────────────────────────────────


def test_janela_de_dias_respeitada(factory):
    with factory() as s:
        velho = _cadencia(s, "+5500000000000")
        _enviou(s, velho, T_MSG_0, at=AGORA - timedelta(days=45))
        novo = _cadencia(s, "+5500000000000")
        _enviou(s, novo, T_MSG_0, at=AGORA - timedelta(days=2))
        s.commit()

        d30 = automacoes.calcular_metricas(s, dias=30, agora=AGORA)
        d90 = automacoes.calcular_metricas(s, dias=90, agora=AGORA)

    assert _por_indice(d30)[T_MSG_0]["enviados"] == 1
    assert _por_indice(d90)[T_MSG_0]["enviados"] == 2


# ── marco de régua (o "recomeçar a medição") ─────────────────────────────────


def _marco(session, *, em, motivo="régua mudou", por="dona"):
    m = ReguaMarco(criado_em=em, motivo=motivo, criado_por=por)
    session.add(m)
    session.flush()
    return m


def test_sem_marco_conta_tudo(factory):
    with factory() as s:
        est = _cadencia(s, "+5500000000000")
        _enviou(s, est, T_MSG_0, at=AGORA - timedelta(days=20))
        s.commit()
        dados = automacoes.calcular_metricas(s, dias=30, agora=AGORA)

    assert dados["medindo_desde"] is None
    assert dados["motivo_do_marco"] is None
    assert _por_indice(dados)[T_MSG_0]["enviados"] == 1


def test_marco_corta_o_que_veio_antes(factory):
    """Envio anterior ao marco sai da conta; o posterior fica. Nada é apagado."""
    with factory() as s:
        antigo = _cadencia(s, "+5500000000000")
        _enviou(s, antigo, T_MSG_0, at=AGORA - timedelta(days=20))
        novo = _cadencia(s, "+5500000000000")
        _enviou(s, novo, T_MSG_0, at=AGORA - timedelta(days=2))
        _respondeu(s, novo, at=AGORA - timedelta(days=1))
        _marco(s, em=AGORA - timedelta(days=10),
               motivo="entrada do toque D3 de áudio")
        s.commit()

        dados = automacoes.calcular_metricas(s, dias=30, agora=AGORA)
        eventos_na_tabela = s.query(CadenciaEvento).count()

    t = _por_indice(dados)[T_MSG_0]
    assert t["enviados"] == 1  # só o de depois do marco
    assert t["responderam"] == 1
    assert dados["medindo_desde"] is not None
    assert dados["motivo_do_marco"] == "entrada do toque D3 de áudio"
    # o marco é corte de LEITURA: a auditoria continua inteira
    assert eventos_na_tabela == 3


def test_marco_mais_antigo_que_a_janela_nao_alarga_o_periodo(factory):
    """Marco velho não faz 7 dias virarem 90: quem manda é o mais recente dos dois."""
    with factory() as s:
        est = _cadencia(s, "+5500000000000")
        _enviou(s, est, T_MSG_0, at=AGORA - timedelta(days=20))
        _marco(s, em=AGORA - timedelta(days=60), motivo="marco velho")
        s.commit()
        dados = automacoes.calcular_metricas(s, dias=7, agora=AGORA)

    assert _por_indice(dados)[T_MSG_0]["enviados"] == 0


def test_vale_o_marco_mais_recente(factory):
    with factory() as s:
        est = _cadencia(s, "+5500000000000")
        _enviou(s, est, T_MSG_0, at=AGORA - timedelta(days=5))
        _marco(s, em=AGORA - timedelta(days=10), motivo="primeiro")
        _marco(s, em=AGORA - timedelta(days=2), motivo="segundo")
        s.commit()
        dados = automacoes.calcular_metricas(s, dias=30, agora=AGORA)

    assert dados["motivo_do_marco"] == "segundo"
    assert _por_indice(dados)[T_MSG_0]["enviados"] == 0


# ── endpoint HTTP ────────────────────────────────────────────────────────────


def test_endpoint_exige_auth(client):
    assert client.get("/painel/automacoes/metricas").status_code == 403


def test_endpoint_corretora_403(client):
    r = client.get("/painel/automacoes/metricas", headers={"X-Painel-User": "corretor"})
    assert r.status_code == 403


def test_endpoint_admin_ok(client, factory):
    with factory() as s:
        est = _cadencia(s, "+5500000000000")
        _enviou(s, est, T_MSG_0, at=AGORA - timedelta(days=2))
        _respondeu(s, est, at=AGORA - timedelta(days=1))
        s.commit()

    r = client.get("/painel/automacoes/metricas?dias=30", headers=ADMIN)
    assert r.status_code == 200
    body = r.json()
    assert body["dias"] == 30
    assert "gerado_em" in body and "definicao" in body
    assert _por_indice(body)[T_MSG_0]["taxa"] == pytest.approx(100)


def test_endpoint_dias_fora_da_faixa(client):
    assert client.get("/painel/automacoes/metricas?dias=0", headers=ADMIN).status_code == 422
    assert client.get(
        f"/painel/automacoes/metricas?dias={automacoes.METRICAS_DIAS_MAX + 1}",
        headers=ADMIN,
    ).status_code == 422


# ── endpoint do marco ────────────────────────────────────────────────────────


def test_marco_exige_auth(client):
    r = client.post("/painel/automacoes/metricas/marco", json={"motivo": "x?"})
    assert r.status_code == 403


def test_marco_corretora_403(client, factory):
    r = client.post(
        "/painel/automacoes/metricas/marco",
        json={"motivo": "tentando zerar"},
        headers={"X-Painel-User": "corretor"},
    )
    assert r.status_code == 403
    with factory() as s:
        assert s.query(ReguaMarco).count() == 0


def test_marco_motivo_obrigatorio(client):
    r = client.post(
        "/painel/automacoes/metricas/marco", json={"motivo": "   "}, headers=ADMIN
    )
    assert r.status_code == 400


def test_marco_pelo_endpoint_nao_apaga_evento_nenhum(client, factory):
    """O 'zerar' da tela é corte de leitura: a auditoria fica intacta."""
    with factory() as s:
        est = _cadencia(s, "+5500000000000")
        _enviou(s, est, T_MSG_0, at=AGORA - timedelta(days=3))
        _respondeu(s, est, at=AGORA - timedelta(days=2))
        s.commit()
        antes = s.query(CadenciaEvento).count()

    r = client.post(
        "/painel/automacoes/metricas/marco",
        json={"motivo": "entrada do toque D3 de áudio + copies novas"},
        headers=ADMIN,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["motivo_do_marco"] == "entrada do toque D3 de áudio + copies novas"

    with factory() as s:
        depois = s.query(CadenciaEvento).count()
        assert s.query(ReguaMarco).count() == 1
    assert depois == antes  # NADA foi apagado

    # e a leitura recomeça: o que era do passado sai da conta
    novo = client.get("/painel/automacoes/metricas?dias=30", headers=ADMIN).json()
    assert novo["medindo_desde"] is not None
    assert novo["total"]["enviados"] == 0
    assert novo["total"]["sem_dado"] is True
