"""Contrato de envio da cadência (lote L1 da auditoria 07/2026).

Cobre o que a auditoria pegou de errado no caminho de envio:

  1. contrato do sender: sucesso REAL devolve sent=True/dry_run=False + wamid +
     template (antes devolvia o JSON cru da Graph, e todo evento gravava
     sent=false — 602/602 eventos errados em produção);
  2. resiliência: falha de envio NÃO derruba o tick, NÃO avança o toque,
     grava evento de falha com toque=NULL (pra não consumir o slot do UNIQUE)
     e o loop segue pros próximos leads;
  3. circuit breaker: N falhas SEGUIDAS abortam a varredura;
  4. gate de categoria: toque 'marketing' não é despachado pela linha UTILITY;
  5. cap diário: janela no dia LOCAL (America/Sao_Paulo), não no dia UTC;
  6. dedupe por telefone: '+55...' e '55...' são a MESMA cadência.

DB isolado: SQLite in-memory (mesmo padrão de tests/test_cadencia.py). O sender
é SEMPRE fake — nenhum teste aqui chega perto da Graph API.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import BigInteger, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from app.db.models import Base, CadenciaEstado, CadenciaEvento, DisparoReengajamento, Lead
from app.services import cadencia


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(element, compiler, **kw):  # noqa: ANN001
    return "JSON"


@compiles(BigInteger, "sqlite")
def _compile_bigint_sqlite(element, compiler, **kw):  # noqa: ANN001
    return "INTEGER"


@pytest.fixture()
def session_factory():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(
        engine, tables=[CadenciaEstado.__table__, CadenciaEvento.__table__,
                 # opt-out (07/10): o follow-up lê leads/disparos_reengajamento a cada
                 # volta e, sem as tabelas, segura todo toque (fail-closed).
                 Lead.__table__, DisparoReengajamento.__table__]
    )
    return sessionmaker(bind=engine)


@pytest.fixture(autouse=True)
def _reset_guard(monkeypatch):
    cadencia._LAST_ENQUEUE["at"] = 0.0
    # o cooldown anti-ban (60s entre envios) bloquearia o 2º lead de um mesmo
    # tick nos testes multi-lead — aqui o que se testa é outra coisa.
    from app.config import settings

    monkeypatch.setattr(settings, "cadencia_min_intervalo_s", 0)
    yield


D0 = datetime(2026, 7, 10, 9, 0, 0, tzinfo=timezone.utc)


def _iniciar(session_factory, telefone="+5500000000000", agora=D0):
    with session_factory() as s:
        est = cadencia.iniciar_cadencia(s, telefone=telefone, agora=agora)
        s.commit()
        return est.id


def _tick(session_factory, sender, agora):
    cadencia._LAST_ENQUEUE["at"] = 0.0
    with session_factory() as s:
        return cadencia.tick(agora=agora, session=s, sender=sender)


class SenderOk:
    """Simula o SUCESSO REAL (não dry-run) já no contrato normalizado."""

    def __init__(self):
        self.chamadas = 0

    def __call__(self, telefone, texto, *, com_botao):
        self.chamadas += 1
        return {
            "ok": True, "dry_run": False, "sent": True,
            "wamid": f"wamid.{self.chamadas}", "template": "acme_ola_a_01",
            "raw": {"messages": [{"id": f"wamid.{self.chamadas}"}]},
        }


class SenderQuebra:
    """Levanta sempre — simula WABA fora do ar / template não aprovado."""

    def __init__(self):
        self.chamadas = 0

    def __call__(self, telefone, texto, *, com_botao):
        self.chamadas += 1
        raise RuntimeError("reengajar sem template aprovado — 400: boom")


# ── 1. Contrato do sender no envio REAL ───────────────────────────────────────

def test_envio_real_grava_sent_true_com_wamid_e_template(session_factory):
    _iniciar(session_factory)
    res = _tick(session_factory, SenderOk(), D0)

    assert res.mensagens_enfileiradas == 1
    with session_factory() as s:
        ev = s.query(CadenciaEvento).filter(CadenciaEvento.toque == 0).one()
        assert ev.payload["sent"] is True
        assert ev.payload["dry_run"] is False
        assert ev.payload["wamid"] == "wamid.1"
        assert ev.payload["template"] == "acme_ola_a_01"


def _libera_rotacao_ola(monkeypatch) -> None:
    """Toda a rotação `ola` conta como UTILITY aprovado, sem rede pra Meta.

    Os três testes abaixo dizem "sem chamar rede" e substituem o httpx.Client do
    ENVIO — mas a pergunta "quais templates são UTILITY/MARKETING" saía por
    outro caminho (`cloud_lines`, httpx.get) e ia de verdade à Graph, com o
    token da linha da Pessoa12. Passavam ou falhavam conforme a Meta classificava os
    templates no dia. Desde 05/10/2026 o conftest corta essa consulta; aqui o
    cenário declara a resposta que precisa.
    """
    from app.api import cloud_corretora_a as cg
    from app.services import cloud_lines as _cl

    monkeypatch.setattr(
        _cl, "templates_utility",
        lambda slug, **kw: {t[0] for t in cg._OLA_ROTATION},
    )
    monkeypatch.setattr(_cl, "templates_marketing", lambda slug, **kw: set())


def test_contrato_real_do_cloud_corretora_a_normaliza(monkeypatch):
    """O ramo de SUCESSO real de enviar_template_corretor devolve o contrato — não o
    JSON cru da Graph. Sem chamar rede: httpx.Client é substituído."""
    from app.api import cloud_corretora_a as cg

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"messages": [{"id": "wamid.REAL"}]}

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **k):
            return _Resp()

    _libera_rotacao_ola(monkeypatch)
    monkeypatch.setattr(cg, "_dry_run_ativo", lambda: False)
    monkeypatch.setattr(cg, "_token", lambda: "tok")
    monkeypatch.setattr(cg.httpx, "Client", _Client)
    monkeypatch.setattr(cg, "gravar", lambda **kw: True)
    monkeypatch.setattr(cg, "_nome_para_template", lambda *a, **k: "João")

    out = cg.enviar_template_corretor("+5500000000000", "João", hook="oi")
    assert out["ok"] is True
    assert out["sent"] is True
    assert out["dry_run"] is False
    assert out["wamid"] == "wamid.REAL"
    assert out["template"].startswith("acme_ola_")
    assert out["raw"] == {"messages": [{"id": "wamid.REAL"}]}


def test_dry_run_continua_sent_false(monkeypatch):
    from app.api import cloud_corretora_a as cg

    monkeypatch.setattr(cg, "_dry_run_ativo", lambda: True)
    monkeypatch.setattr(cg, "gravar", lambda **kw: True)
    out = cg.enviar_template_corretor("+5500000000000", "João", hook="oi")
    assert out["sent"] is False
    assert out["dry_run"] is True
    assert out["wamid"].startswith("dry-")


# ── 2. Erro de LINHA corta a rotação na primeira ocorrência ───────────────────

def test_erro_de_linha_nao_queima_os_13_templates(monkeypatch):
    from app.api import cloud_corretora_a as cg

    tentativas = {"n": 0}

    class _Resp:
        status_code = 400

        @staticmethod
        def json():
            return {"error": {"code": 133010, "message": "número não registrado"}}

        text = '{"error":{"code":133010}}'

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **k):
            tentativas["n"] += 1
            return _Resp()

    _libera_rotacao_ola(monkeypatch)
    monkeypatch.setattr(cg, "_dry_run_ativo", lambda: False)
    monkeypatch.setattr(cg, "_token", lambda: "tok")
    monkeypatch.setattr(cg.httpx, "Client", _Client)
    monkeypatch.setattr(cg, "_nome_para_template", lambda *a, **k: "João")

    with pytest.raises(RuntimeError, match="erro de linha/conta"):
        cg.enviar_template_corretor("+5500000000000", "João", hook="oi")
    assert tentativas["n"] == 1, "abortou na primeira, sem rotacionar o pool"


def test_template_nao_aprovado_ainda_rotaciona(monkeypatch):
    from app.api import cloud_corretora_a as cg

    tentativas = {"n": 0}

    class _Resp:
        status_code = 400
        text = '{"error":{"code":132001}}'

        @staticmethod
        def json():
            return {"error": {"code": 132001, "message": "template não existe"}}

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **k):
            tentativas["n"] += 1
            return _Resp()

    monkeypatch.setattr(cg, "_dry_run_ativo", lambda: False)
    monkeypatch.setattr(cg, "_token", lambda: "tok")
    monkeypatch.setattr(cg.httpx, "Client", _Client)
    monkeypatch.setattr(cg, "_nome_para_template", lambda *a, **k: "João")
    _libera_rotacao_ola(monkeypatch)

    with pytest.raises(RuntimeError, match="sem template aprovado"):
        cg.enviar_template_corretor("+5500000000000", "João", hook="oi")
    # com_botao=False (default follow-up): só pool ola, NÃO reeng
    assert tentativas["n"] == len(cg._OLA_ROTATION)


# ── 3. Falha de envio não derruba o tick nem consome o toque ─────────────────

def test_falha_de_envio_nao_avanca_toque_e_grava_evento(session_factory):
    _iniciar(session_factory)
    sender = SenderQuebra()
    res = _tick(session_factory, sender, D0)

    assert res.mensagens_enfileiradas == 0
    assert res.bloqueados.get("envio_falhou") == 1
    with session_factory() as s:
        est = s.query(CadenciaEstado).one()
        assert est.toque_atual == 0, "toque NÃO avança em falha"
        ev = s.query(CadenciaEvento).one()
        assert ev.tipo == cadencia.TIPO_ENVIO_FALHOU
        assert ev.toque is None, "evento de falha não consome o slot do UNIQUE"
        assert ev.payload["toque_tentado"] == 0

    # retry no tick seguinte encontra o toque 0 livre e ENVIA
    ok = SenderOk()
    res2 = _tick(session_factory, ok, D0 + timedelta(minutes=5))
    assert res2.mensagens_enfileiradas == 1
    with session_factory() as s:
        assert s.query(CadenciaEstado).one().toque_atual == 1


def test_falha_em_um_lead_nao_impede_os_outros(session_factory):
    _iniciar(session_factory, telefone="+5500000000000")
    _iniciar(session_factory, telefone="+5500000000000")
    _iniciar(session_factory, telefone="+5500000000000")

    class SenderPrimeiroQuebra:
        def __init__(self):
            self.n = 0

        def __call__(self, telefone, texto, *, com_botao):
            self.n += 1
            if self.n == 1:
                raise RuntimeError("boom no primeiro")
            return {"ok": True, "dry_run": False, "sent": True,
                    "wamid": f"w{self.n}", "template": "acme_ola_a_01"}

    res = _tick(session_factory, SenderPrimeiroQuebra(), D0)
    assert res.mensagens_enfileiradas == 2
    assert res.bloqueados.get("envio_falhou") == 1


def test_circuit_breaker_aborta_apos_falhas_seguidas(session_factory):
    for i in range(6):
        _iniciar(session_factory, telefone=f"5500000000000{i:04d}")

    sender = SenderQuebra()
    res = _tick(session_factory, sender, D0)

    assert sender.chamadas == cadencia._MAX_FALHAS_SEGUIDAS
    assert res.bloqueados.get("circuit_breaker") == 1


def test_commit_por_lead_torna_envio_duravel(session_factory):
    """O envio do lead 1 tem que estar DURÁVEL (commitado) antes do lead 2 ser
    tocado. Antes o commit era só no fim do loop: um crash em lead posterior
    dava rollback da linha inteira e o lead 1 recebia de novo no tick seguinte.

    Provado de dentro: no 2º envio, uma sessão INDEPENDENTE já enxerga o evento
    do 1º (só possível com commit no meio do loop).
    """
    _iniciar(session_factory, telefone="+5500000000000")
    _iniciar(session_factory, telefone="+5500000000000")

    visto: dict[str, int] = {}

    class SenderEspia:
        def __init__(self):
            self.n = 0

        def __call__(self, telefone, texto, *, com_botao):
            self.n += 1
            if self.n == 2:
                with session_factory() as outra:
                    visto["eventos_commitados"] = (
                        outra.query(CadenciaEvento)
                        .filter(CadenciaEvento.tipo == cadencia.TIPO_MENSAGEM)
                        .count()
                    )
            return {"ok": True, "dry_run": False, "sent": True,
                    "wamid": f"w{self.n}", "template": "acme_ola_a_01"}

    res = _tick(session_factory, SenderEspia(), D0)
    assert res.mensagens_enfileiradas == 2
    assert visto["eventos_commitados"] == 1, "o envio do lead 1 já estava durável"


# ── 4. Gate de categoria (toque 7 marketing) ─────────────────────────────────

def test_toque_marketing_nao_despacha_mas_avanca(session_factory):
    _iniciar(session_factory)
    sender = SenderOk()
    for off in [t.offset_dias for t in cadencia.TOQUES]:
        _tick(session_factory, sender, D0 + timedelta(days=off))

    # Toques de mensagem menos o de marketing (que não despacha nesta linha).
    _msgs = [t for t in cadencia.TOQUES if t.tipo == cadencia.TIPO_MENSAGEM]
    _mkt = [t for t in _msgs if t.categoria == "marketing"]
    assert sender.chamadas == len(_msgs) - len(_mkt)
    with session_factory() as s:
        ev7 = s.query(CadenciaEvento).filter(CadenciaEvento.toque == cadencia.toque_encerramento()).one()
        assert ev7.payload["pulado"] == "categoria_marketing"
        assert ev7.payload["sent"] is False
        # e o lead SEGUIU (virou remarketing), não travou no toque 7
        assert s.query(CadenciaEstado).one().status == "remarketing"


def test_enfileirar_mensagem_recusa_toque_marketing(session_factory):
    """Rede de segurança: mesmo chamado direto, o envio de toque marketing
    levanta em vez de sair pela linha UTILITY."""
    _iniciar(session_factory)
    toque_mkt = cadencia.TOQUES[cadencia.toque_encerramento()]
    assert toque_mkt.categoria == "marketing"
    with session_factory() as s:
        est = s.query(CadenciaEstado).one()
        with pytest.raises(RuntimeError, match="marketing_bloqueado"):
            cadencia._enfileirar_mensagem(
                s, est, toque_mkt, "texto", False, D0, SenderOk()
            )


# ── 5. Cap diário no fuso de São Paulo ───────────────────────────────────────

def test_janela_do_cap_e_o_dia_local_de_sao_paulo():
    # 2026-07-10 02:00 UTC = 2026-07-09 23:00 em SP → dia local ainda é o 09
    agora = datetime(2026, 7, 10, 2, 0, tzinfo=timezone.utc)
    inicio, fim = cadencia._janela_dia_local(agora)
    assert inicio == datetime(2026, 7, 9, 3, 0, tzinfo=timezone.utc)   # 00:00 SP
    assert fim == datetime(2026, 7, 10, 3, 0, tzinfo=timezone.utc)     # 24:00 SP
    assert inicio <= agora < fim


def test_cap_conta_evento_de_23h_sp_no_mesmo_dia(session_factory, monkeypatch):
    """Evento das 23h de SP (= 02h UTC do dia seguinte) tem que contar no dia
    local corrente. Com a janela UTC antiga, ele caía em outro balde e o cap
    liberava cota nova à noite."""
    from app.config import settings

    monkeypatch.setattr(settings, "cadencia_cap_diario", 1)
    est_id = _iniciar(session_factory)
    # 23:30 SP do dia 09 → 02:30 UTC do dia 10
    at_23h_sp = datetime(2026, 7, 10, 2, 30, tzinfo=timezone.utc)
    with session_factory() as s:
        s.add(CadenciaEvento(
            tenant_id=1, cadencia_id=est_id, toque=None,
            tipo=cadencia.TIPO_MENSAGEM, canal="whatsapp",
            # `sent: true` = mensagem que SAIU de verdade. Desde 29/07 o cap só
            # conta o que foi despachado (evento pulado/dry-run não come cota).
            payload={"sent": True}, at=at_23h_sp,
        ))
        s.commit()

    with session_factory() as s:
        # consulta no mesmo dia local (22h SP = 01h UTC do dia 10)
        assert cadencia._cap_diario_atingido(
            s, 1, datetime(2026, 7, 10, 1, 0, tzinfo=timezone.utc)
        ) is True


# ── 6. Dedupe por telefone: '+55...' e '55...' são a mesma cadência ──────────

def test_iniciar_cadencia_deduplica_com_e_sem_mais(session_factory):
    with session_factory() as s:
        a = cadencia.iniciar_cadencia(s, telefone="+5500000000000", agora=D0)
        s.commit()
        a_id = a.id
    with session_factory() as s:
        b = cadencia.iniciar_cadencia(s, telefone="+5500000000000", agora=D0)
        s.commit()
        assert b.id == a_id
        assert s.query(CadenciaEstado).count() == 1


def test_telefone_gravado_na_forma_canonica(session_factory):
    with session_factory() as s:
        est = cadencia.iniciar_cadencia(s, telefone="+5500000000000", agora=D0)
        s.commit()
        assert est.telefone == "+5500000000000"


# ── 7. Guard de dono: cadência não toca lead de outra corretora ──────────────

def test_guard_de_dono_bloqueia_lead_transferido(session_factory, monkeypatch):
    """Lead cujo owner_id na espinha NÃO é a Pessoa12 (transferência manual ou
    sobrescrita do Kommo) não recebe toque — o robô da Pessoa12 estava mandando
    mensagem pro lead de outra corretora (2 casos vivos em produção)."""
    _iniciar(session_factory, telefone="+5500000000000")

    monkeypatch.setattr(cadencia, "_owner_ids_corretor", lambda s: {3, 12345})
    monkeypatch.setattr(
        cadencia, "_donos_por_suf8", lambda s, t: {"99990000": 99}  # outra corretora
    )

    sender = SenderOk()
    res = _tick(session_factory, sender, D0)
    assert sender.chamadas == 0
    assert res.bloqueados.get("dono_mudou") == 1
    with session_factory() as s:
        assert s.query(CadenciaEstado).one().toque_atual == 0


def test_guard_de_dono_deixa_passar_lead_da_corretor(session_factory, monkeypatch):
    _iniciar(session_factory, telefone="+5500000000000")
    monkeypatch.setattr(cadencia, "_owner_ids_corretor", lambda s: {3, 12345})
    monkeypatch.setattr(cadencia, "_donos_por_suf8", lambda s, t: {"99990000": 12345})

    sender = SenderOk()
    res = _tick(session_factory, sender, D0)
    assert res.mensagens_enfileiradas == 1
    assert sender.chamadas == 1


def test_guard_de_dono_nao_mata_lead_sem_espinha(session_factory, monkeypatch):
    """Lead ausente da espinha (ou com owner_id NULL) NÃO é bloqueado: cadência
    legítima da Pessoa12 sem lead canônico continua tocando."""
    _iniciar(session_factory, telefone="+5500000000000")
    monkeypatch.setattr(cadencia, "_owner_ids_corretor", lambda s: {3, 12345})
    monkeypatch.setattr(cadencia, "_donos_por_suf8", lambda s, t: {})  # sem lead

    res = _tick(session_factory, SenderOk(), D0)
    assert res.mensagens_enfileiradas == 1
    assert "dono_mudou" not in res.bloqueados


def test_guard_desligado_quando_corretor_nao_resolvida(session_factory, monkeypatch):
    """Sem Pessoa12 cadastrada (owners vazio) o guard fica DESLIGADO — não pode matar
    a cadência por falta de cadastro."""
    _iniciar(session_factory, telefone="+5500000000000")
    monkeypatch.setattr(cadencia, "_owner_ids_corretor", lambda s: set())
    monkeypatch.setattr(cadencia, "_donos_por_suf8", lambda s, t: {"99990000": 99})

    res = _tick(session_factory, SenderOk(), D0)
    assert res.mensagens_enfileiradas == 1


def test_resposta_para_todas_as_gemeas(session_factory):
    """Cadências gêmeas legadas (grafias diferentes já no banco): um 'PARA'
    tem que silenciar TODAS — antes parava só a primeira e a irmã seguia."""
    with session_factory() as s:
        s.add(CadenciaEstado(
            tenant_id=1, telefone="+5500000000000", toque_atual=0,
            proximo_toque_em=D0, status="ativa", criado_em=D0, atualizado_em=D0,
        ))
        s.add(CadenciaEstado(
            tenant_id=1, telefone="+5500000000000", toque_atual=2,
            proximo_toque_em=D0, status="ativa", criado_em=D0, atualizado_em=D0,
        ))
        s.commit()

    with session_factory() as s:
        out = cadencia.processar_resposta_cadencia(
            "+5500000000000", "pare de mandar mensagem", session=s
        )
        assert out == "opt_out"

    with session_factory() as s:
        estados = s.query(CadenciaEstado).all()
        assert [e.status for e in estados] == ["opt_out", "opt_out"]


# ── Cap conta só o que SAIU (achado do Grok, 29/07) ─────────────────────────

def test_cap_ignora_evento_que_nao_saiu(session_factory, monkeypatch):
    """Toque pulado por categoria marketing e dry-run NÃO comem cota anti-ban.

    Prova do impacto em produção: em 26/07 foram 52 eventos contra um cap de 25,
    com ZERO mensagem enviada — a cadência se auto-bloqueou o dia inteiro por
    eventos que nunca chegaram na Meta.
    """
    from app.config import settings

    monkeypatch.setattr(settings, "cadencia_cap_diario", 1)
    est_id = _iniciar(session_factory)
    agora = datetime(2026, 7, 10, 15, 0, tzinfo=timezone.utc)

    with session_factory() as s:
        s.add_all([
            # pulado por marketing — não saiu
            CadenciaEvento(tenant_id=1, cadencia_id=est_id, toque=7,
                           tipo=cadencia.TIPO_MENSAGEM, canal="whatsapp",
                           payload={"sent": False, "pulado": "categoria_marketing"},
                           at=agora),
            # dry-run — não saiu
            CadenciaEvento(tenant_id=1, cadencia_id=est_id, toque=None,
                           tipo=cadencia.TIPO_MENSAGEM, canal="whatsapp",
                           payload={"sent": False, "dry_run": True}, at=agora),
        ])
        s.commit()

    with session_factory() as s:
        assert cadencia._cap_diario_atingido(s, 1, agora) is False, (
            "evento que não saiu comeu cota — era o bug de 26/07"
        )

    # agora um que SAIU de verdade: aí sim bate o cap de 1
    with session_factory() as s:
        s.add(CadenciaEvento(tenant_id=1, cadencia_id=est_id, toque=0,
                             tipo=cadencia.TIPO_MENSAGEM, canal="whatsapp",
                             payload={"sent": True, "wamid": "wamid.X"}, at=agora))
        s.commit()
    with session_factory() as s:
        assert cadencia._cap_diario_atingido(s, 1, agora) is True
