"""Visão: mensagens e ligações por corretora (06/10/2026) + funil em janela curta.

Fixture REAL anonimizada (`fixtures/visao_atividade_real_anon.json`): 136
mensagens de 01/10/2026 09h–12h SP e as 228 ligações + 3 "venda perdida" de
30/09 e 01/10, extraídas de produção só leitura, com telefone e lead_id trocados
(mesma pessoa = mesmo falso) e sem texto. Os números esperados foram medidos em
produção na mesma janela. Aqui as datas andam +5 anos pra não cruzar com dado de
outro teste. DB de teste (conftest), zero prod, zero envio.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException
from sqlalchemy import text as sql_text

import app.api.painel as painel
from app.api.visao_atividade import painel_visao_atividade
from app.db.session import SessionLocal
from app.services import visao_atividade as svc

SP = ZoneInfo("America/Sao_Paulo")
FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "visao_atividade_real_anon.json").read_text())
ANOS = 5  # desloca a fixture: 2026 → 2031
DIA_MSG = date(2031, 10, 1)
LIG_DI, LIG_DF = date(2031, 9, 30), date(2031, 10, 1)
CORRETORAS = {1: "Pessoa5", 2: "Pessoa1", 3: "Pessoa3", 4: "Corretor", 7: "Pessoa7", 8: "Corretor"}
LABELS = {1: "Pessoa10", 2: "Pessoa11", 3: "Pessoa12", 4: "Corretor", 7: "Pessoa7", 8: "Corretor"}


def _desloca(iso: str) -> datetime:
    dt = datetime.fromisoformat(iso)
    return dt.replace(year=dt.year + ANOS)


def _limpa(s) -> None:
    s.rollback()
    s.execute(sql_text("DELETE FROM mensagens_whatsapp WHERE evolution_message_id LIKE 'TSTV-%'"))
    s.execute(sql_text("DELETE FROM ligacao_resultado WHERE lead_id LIKE 'TSTV-%'"))
    s.execute(sql_text("DELETE FROM lead_events WHERE payload->>'teste' = 'visao_atividade'"))
    s.execute(sql_text("DELETE FROM vendas WHERE cliente_nome LIKE 'TSTV-%'"))
    s.execute(sql_text("DELETE FROM vendas_mensais WHERE ano_mes IN ('2031-09', '2031-10')"))
    s.execute(sql_text("DELETE FROM atendimentos_evolution_mensais WHERE ano_mes IN ('2031-09', '2031-10')"))
    s.execute(sql_text("DELETE FROM leads WHERE nome LIKE 'TSTV-%'"))
    s.commit()
    svc.limpar_cache()


@pytest.fixture()
def db():
    with SessionLocal() as s:
        _limpa(s)
        for cid, nome in CORRETORAS.items():
            s.execute(sql_text(
                "INSERT INTO corretoras (id, nome, telefone, ativo) "
                "VALUES (:i, :n, :t, TRUE) ON CONFLICT (id) DO NOTHING"),
                {"i": cid, "n": nome, "t": f"+5500000000000{cid:03d}"})
        s.commit()
        yield s
        _limpa(s)


_N = [0]


def _msg(s, *, cid: int | None, tel: str | None, saiu: bool, tipo: str = "text",
         origem: str | None = None, quando: datetime, instance: str = "tstv") -> None:
    _N[0] += 1
    s.execute(sql_text(
        """INSERT INTO mensagens_whatsapp
             (evolution_message_id, instance, corretora_id, remote_jid, contato_telefone,
              from_corretora, tipo_mensagem, conteudo_metadata, timestamp_evolution)
           VALUES (:w, :inst, :c, :jid, :tel, :saiu, :tipo, CAST(:meta AS jsonb), :ts)"""),
        {"w": f"TSTV-{_N[0]}", "inst": instance, "c": cid,
         "jid": f"{tel or 'x'}@s.whatsapp.net", "tel": tel, "saiu": saiu, "tipo": tipo,
         "meta": json.dumps({"origem": origem} if origem else {}), "ts": quando})


def _lig(s, *, cid: int, lead: str, atendeu: bool, quis: bool | None = None,
         obs: str | None = None, quando: datetime) -> None:
    _N[0] += 1
    s.execute(sql_text(
        """INSERT INTO ligacao_resultado (tenant_id, origem, origem_id, toque, lead_id,
               corretora_id, atendeu, quis_continuar, observacao, criado_em)
           VALUES (1, 'remarketing', :oid, 1, :lead, :c, :a, :q, :obs, :ts)"""),
        {"oid": 900000 + _N[0], "lead": lead, "c": cid, "a": atendeu, "q": quis,
         "obs": obs, "ts": quando})


def _evento(s, *, tipo: str, cid: int, quando: datetime) -> None:
    s.execute(sql_text(
        "INSERT INTO lead_events (tenant_id, lead_id, tipo, payload, at) "
        "VALUES (1, NULL, :t, CAST(:p AS jsonb), :ts)"),
        {"t": tipo, "p": json.dumps({"corretora_id": cid, "teste": "visao_atividade"}),
         "ts": quando})


def _carrega_fixture(s) -> None:
    for m in FIXTURE["mensagens"]:
        _msg(s, cid=m["corretora_id"], tel=m["contato_telefone"], saiu=m["from_corretora"],
             tipo=m["tipo_mensagem"], origem=m["origem"], quando=_desloca(m["ts"]),
             instance=m["instance"])
    for i, l in enumerate(FIXTURE["ligacoes"]):
        _lig(s, cid=l["corretora_id"], lead=l["lead_id"], atendeu=l["atendeu"],
             quis=l["quis_continuar"], obs="numero incorreto" if l["obs_incorreto"] else None,
             quando=_desloca(l["ts"]))
    for e in FIXTURE["eventos"]:
        _evento(s, tipo=e["tipo"], cid=e["corretora_id"], quando=_desloca(e["ts"]))
    s.commit()


def _por(res: dict) -> dict[int, dict]:
    return {c["corretora_id"]: c for c in res["por_corretora"]}


# ── 1. a manhã real ──────────────────────────────────────────────────────────


CAMPOS_MSG = ("escritas", "pessoas", "automaticas", "pessoas_automaticas",
              "recebidas", "pessoas_que_falaram")


def test_manha_real_bate_com_o_medido_em_producao(db):
    _carrega_fixture(db)
    res = svc.calcular_atividade(db, DIA_MSG, DIA_MSG, corretora_label=LABELS)
    esperado = FIXTURE["esperado_mensagens"]
    assert {k: res["total"]["mensagens"][k] for k in CAMPOS_MSG} == esperado["total"]
    por = _por(res)
    for cid, esp in esperado.items():
        if cid == "total":
            continue
        assert {k: por[int(cid)]["mensagens"][k] for k in CAMPOS_MSG} == esp, cid


def test_manha_real_bate_com_uma_conta_feita_a_mao_em_python(db):
    """Implementação independente da regra, sobre as mesmas linhas."""
    _carrega_fixture(db)
    ref: dict = defaultdict(lambda: defaultdict(set))
    cont: dict = defaultdict(lambda: defaultdict(int))
    for m in FIXTURE["mensagens"]:
        if m["corretora_id"] is None or m["contato_telefone"] is None:
            continue
        p8 = re.sub(r"\D", "", m["contato_telefone"])[-8:]
        humana = (m["from_corretora"] and m["tipo_mensagem"] != "template"
                  and not m["origem"])
        tipo = ("escritas" if humana else "automaticas") if m["from_corretora"] else "recebidas"
        for chave in (m["corretora_id"], "total"):
            cont[chave][tipo] += 1
            ref[chave][tipo].add(p8)
    res = svc.calcular_atividade(db, DIA_MSG, DIA_MSG)
    linhas = {**{c["corretora_id"]: c["mensagens"] for c in res["por_corretora"]},
              "total": res["total"]["mensagens"]}
    for chave, c in cont.items():
        assert linhas[chave]["escritas"] == c["escritas"]
        assert linhas[chave]["automaticas"] == c["automaticas"]
        assert linhas[chave]["recebidas"] == c["recebidas"]
        assert linhas[chave]["pessoas"] == len(ref[chave]["escritas"])
        assert linhas[chave]["pessoas_automaticas"] == len(ref[chave]["automaticas"])


def test_ligacoes_reais_de_30_09_e_01_10(db):
    _carrega_fixture(db)
    res = svc.calcular_atividade(db, LIG_DI, LIG_DF, corretora_label=LABELS)
    esperado = FIXTURE["esperado_ligacoes"]
    campos = ("feitas", "atendidas", "nao_atendidas", "quiseram_continuar", "pessoas")
    assert {k: res["total"]["ligacoes"][k] for k in campos} == {k: esperado["total"][k] for k in campos}
    por = _por(res)
    assert por[7]["ligacoes"]["feitas"] == esperado["7"]["feitas"] == 205
    assert por[8]["ligacoes"]["feitas"] == esperado["8"]["feitas"] == 23
    # as 3 "venda perdida" da Pessoa7 ficam à parte, fora de "feitas"
    assert por[7]["ligacoes"]["venda_perdida"] == 3
    assert res["total"]["ligacoes"]["venda_perdida"] == 3
    assert res["total"]["ligacoes"]["taxa_atendimento"] == round(15 / 228 * 100, 1)


# ── 2. as regras, uma a uma ──────────────────────────────────────────────────


def test_regras_de_mensagem(db):
    base = datetime(2031, 10, 1, 10, 0, tzinfo=SP)
    a, b, c = "+5500000000000", "+5500000000000", "+5500000000000"
    _msg(db, cid=1, tel=a, saiu=True, tipo="text", quando=base)              # gente
    _msg(db, cid=1, tel=a, saiu=True, tipo="audio", quando=base)             # gente
    _msg(db, cid=1, tel=b, saiu=True, tipo="template", quando=base)          # disparo
    _msg(db, cid=1, tel=b, saiu=True, tipo="text", origem="pessoa", quando=base)  # robô
    _msg(db, cid=1, tel=b, saiu=False, tipo="text", quando=base)             # recebida
    _msg(db, cid=2, tel=a, saiu=True, tipo="text", quando=base)              # mesma pessoa, outra corretora
    _msg(db, cid=None, tel=c, saiu=True, tipo="text", quando=base, instance="cloud_assistente")
    _msg(db, cid=1, tel=None, saiu=True, tipo="text", quando=base)           # sem telefone: fora
    db.commit()
    res = svc.calcular_atividade(db, DIA_MSG, DIA_MSG, corretora_label=LABELS)
    corretor = _por(res)[1]["mensagens"]
    assert (corretor["escritas"], corretor["pessoas"]) == (2, 1)
    assert (corretor["automaticas"], corretor["pessoas_automaticas"]) == (2, 1)
    assert corretor["recebidas"] == 1
    assert {o["origem"]: o["mensagens"] for o in corretor["automaticas_por_origem"]} == {
        "template": 1, "pessoa": 1}
    tot = res["total"]["mensagens"]
    assert tot["escritas"] == 3
    assert tot["pessoas"] == 1, "a pessoa A falou com Pessoa10 e Pessoa11: no total conta uma vez"


def test_dia_e_o_de_sao_paulo(db):
    """02:59 UTC de 02/10 ainda é 01/10 em SP; 03:00 UTC já é 02/10."""
    from datetime import timezone as tz
    _msg(db, cid=1, tel="+5500000000000", saiu=True, quando=datetime(2031, 10, 2, 2, 59, tzinfo=tz.utc))
    _msg(db, cid=1, tel="+5500000000000", saiu=True, quando=datetime(2031, 10, 2, 3, 0, tzinfo=tz.utc))
    db.commit()
    assert svc.calcular_atividade(db, DIA_MSG, DIA_MSG)["total"]["mensagens"]["escritas"] == 1
    assert svc.calcular_atividade(db, date(2031, 10, 2), date(2031, 10, 2))["total"]["mensagens"]["escritas"] == 1


def test_regras_de_ligacao(db):
    base = datetime(2031, 10, 1, 10, 0, tzinfo=SP)
    for h in range(3):  # não atendeu volta em 24 h: 3 tentativas, 1 pessoa
        _lig(db, cid=7, lead="TSTV-x", atendeu=False, quando=base + timedelta(hours=h))
    _lig(db, cid=7, lead="TSTV-y", atendeu=False, obs="Número incorreto", quando=base)
    _lig(db, cid=7, lead="TSTV-z", atendeu=True, quis=True, quando=base)
    _lig(db, cid=8, lead="TSTV-w", atendeu=True, quis=False, quando=base)
    _evento(db, tipo="ligacao_venda_perdida", cid=7, quando=base)
    _evento(db, tipo="ligacao_telefone_errado", cid=8, quando=base)
    db.commit()
    res = svc.calcular_atividade(db, DIA_MSG, DIA_MSG, corretora_label=LABELS)
    m = _por(res)[7]["ligacoes"]
    assert (m["feitas"], m["atendidas"], m["nao_atendidas"], m["pessoas"]) == (5, 1, 4, 3)
    assert m["quiseram_continuar"] == 1
    assert m["numero_incorreto_na_obs"] == 1
    assert m["venda_perdida"] == 1 and m["numero_errado"] == 0
    assert m["taxa_atendimento"] == 20.0
    corretor = _por(res)[8]["ligacoes"]
    assert (corretor["feitas"], corretor["numero_errado"]) == (1, 1), "número errado não vira ligação feita"
    assert _por(res)[1]["ligacoes"]["taxa_atendimento"] is None, "sem ligação: sem taxa, nunca 0%"
    assert res["total"]["ligacoes"]["feitas"] == 6


def test_corretora_ve_so_a_linha_dela(db):
    _carrega_fixture(db)
    res = svc.calcular_atividade(db, LIG_DI, LIG_DF, so_corretora=8)
    assert [c["corretora_id"] for c in res["por_corretora"]] == [8]
    assert res["total"]["ligacoes"]["feitas"] == 23, "o total é o dela, não o do time"


# ── 3. a mesma conta do Telegram ─────────────────────────────────────────────


def test_bate_com_o_que_a_assistente_responde_no_telegram(db, monkeypatch):
    _carrega_fixture(db)
    monkeypatch.setattr(painel, "_hoje_sp", lambda: DIA_MSG)
    tg = painel.painel_conversas_por_corretora(periodo="hoje", x_painel_user="dona")
    res = svc.calcular_atividade(db, DIA_MSG, DIA_MSG)
    nosso = {c["corretora_id"]: (c["mensagens"]["escritas"], c["mensagens"]["pessoas"],
                                 c["mensagens"]["automaticas"], c["mensagens"]["recebidas"])
             for c in res["por_corretora"]
             if c["mensagens"]["escritas"] or c["mensagens"]["automaticas"] or c["mensagens"]["recebidas"]}
    dela = {c["corretora_id"]: (c["enviadas"], c["pessoas"], c["automaticas"], c["recebidas"])
            for c in tg["por_corretora"]}
    assert nosso == dela
    assert res["total"]["mensagens"]["escritas"] == tg["total"]["enviadas"]


# ── 4. endpoint, período e cache ─────────────────────────────────────────────


def test_endpoint_admin_e_corretora(db, monkeypatch):
    _carrega_fixture(db)
    monkeypatch.setattr(painel, "_hoje_sp", lambda: LIG_DF)
    adm = painel_visao_atividade(periodo="7d", inicio=None, fim=None, x_painel_user="dona")
    assert adm["periodo"]["inicio"] == "2031-09-25" and adm["periodo"]["fim"] == "2031-10-01"
    assert adm["total"]["ligacoes"]["feitas"] == 228
    assert len(adm["por_corretora"]) >= 6
    corretor = painel_visao_atividade(periodo="7d", inicio=None, fim=None, x_painel_user="corretor")
    assert [c["corretora_id"] for c in corretor["por_corretora"]] == [8]
    assert corretor["total"]["ligacoes"]["feitas"] == 23
    with pytest.raises(HTTPException):
        painel_visao_atividade(periodo="7d", inicio=None, fim=None, x_painel_user=None)


def test_periodos_novos(monkeypatch):
    monkeypatch.setattr(painel, "_hoje_sp", lambda: date(2026, 10, 6))
    assert painel.parse_periodo("7d")[:2] == (date(2026, 9, 30), date(2026, 10, 6))
    assert painel.parse_periodo("30d")[:2] == (date(2026, 9, 7), date(2026, 10, 6))
    assert painel.parse_periodo("tudo")[:2] == (date(2026, 8, 1), date(2026, 10, 6))
    assert painel.parse_periodo("tudo")[2] == "desde 01/08/2026"


def test_cache_curto():
    svc.limpar_cache()
    chamadas = []

    def calc():
        chamadas.append(1)
        return {"n": len(chamadas)}

    assert svc.com_cache(("k",), calc, agora=100.0) == {"n": 1}
    assert svc.com_cache(("k",), calc, agora=159.0) == {"n": 1}
    assert svc.com_cache(("k",), calc, agora=161.0) == {"n": 2}
    svc.limpar_cache()


# ── 5. funil da Visão em janela curta ────────────────────────────────────────


def _lead(s, *, nome: str, owner: int, tel: str, criado: datetime) -> None:
    s.execute(sql_text(
        "INSERT INTO pipeline_stages (id, tenant_id, nome) VALUES (4, 1, 'Novo') "
        "ON CONFLICT (id) DO NOTHING"))
    s.execute(sql_text(
        """INSERT INTO leads (tenant_id, nome, telefone, owner_id, stage_id, origem, created_at)
           VALUES (1, :n, :tel, :o, 4, 'meta_leadgen', :c)"""),
        {"n": nome, "tel": tel, "o": owner, "c": criado})


@pytest.fixture()
def funil(db, monkeypatch):
    # Só alimentam "atendidos" pelo Kommo (fora de operação); não entram aqui.
    monkeypatch.setattr(painel, "calcular_atendidos_breakdown", lambda di, df: {"por_corretora": {}})
    monkeypatch.setattr(painel, "calcular_recuperados_por_corretora", lambda di, df: {})
    # setembro/31 como setembro/26: 4 vendas da Pessoa11 (12, 14, 24 e 29) e o mensal
    for nome, valor, dia, origem in (("TSTV-a", 210000.0, 12, "indicacao"),
                                     ("TSTV-b", 282754.20, 14, "midia"),
                                     ("TSTV-c", 282000.0, 24, "midia"),
                                     ("TSTV-d", 248000.0, 29, "midia")):
        db.execute(sql_text(
            "INSERT INTO vendas (tenant_id, corretora_id, cliente_nome, valor, data_venda, origem) "
            "VALUES (1, 2, :n, :v, :d, :o)"), {"n": nome, "v": valor, "d": date(2031, 9, dia), "o": origem})
    db.execute(sql_text(
        "INSERT INTO vendas_mensais (corretora_id, ano_mes, qtde_vendas, vgv, qtde_leads) "
        "VALUES (2, '2031-09', 4, 10000015.20, 77) ON CONFLICT DO NOTHING"))
    # o mês de outubro teve 33 atendidos; a semana teve 1
    db.execute(sql_text(
        "INSERT INTO atendimentos_evolution_mensais (corretora_id, ano_mes, atendidos_2via, reengajados_2via) "
        "VALUES (2, '2031-10', 33, 0)"))
    _lead(db, nome="TSTV-L1", owner=2, tel="+5500000000000", criado=datetime(2031, 10, 6, 9, 0, tzinfo=SP))
    _lead(db, nome="TSTV-L2", owner=2, tel="+5500000000000", criado=datetime(2031, 10, 6, 9, 0, tzinfo=SP))
    _msg(db, cid=2, tel="+5500000000000", saiu=False, quando=datetime(2031, 10, 6, 10, 0, tzinfo=SP))
    db.commit()
    return db


def test_30_dias_mostra_as_vendas_de_dentro_da_janela(funil):
    """Antes: 0 venda em "30 dias" com 4 vendas de setembro dentro da janela."""
    eq = painel.calcular_funil(date(2031, 9, 7), date(2031, 10, 6), agenda_completa=True)["equipe"]
    assert (eq["vendas"], eq["vgv"]) == (4, pytest.approx(10000015.20))
    # 7 dias (30/09 a 06/10): nenhuma das 4 cai dentro
    eq7 = painel.calcular_funil(date(2031, 9, 30), date(2031, 10, 6))["equipe"]
    assert eq7["vendas"] == 0
    # 20 a 26/09: só a do dia 24
    eqs = painel.calcular_funil(date(2031, 9, 20), date(2031, 9, 26))["equipe"]
    assert (eqs["vendas"], eqs["vgv"]) == (1, pytest.approx(282000.0))


def test_roi_da_janela_usa_a_venda_da_janela(funil):
    por = painel.vendas_por_origem(date(2031, 9, 7), date(2031, 10, 6))
    assert por["midia"] == {"vendas": 3, "vgv": pytest.approx(812754.20)}
    assert por["indicacao"]["vendas"] == 1
    assert painel.vendas_por_origem(date(2031, 9, 30), date(2031, 10, 6))["midia"]["vendas"] == 0


def test_mes_inteiro_continua_vindo_do_mensal(funil):
    eq = painel.calcular_funil(date(2031, 9, 1), date(2031, 9, 30))["equipe"]
    assert (eq["vendas"], eq["vgv"]) == (4, pytest.approx(10000015.20))


def test_semana_nao_mostra_os_atendidos_do_mes(funil):
    """Antes: "semana" com 2 leads mostrava 33 atendidos (o mês de outubro)."""
    eq = painel.calcular_funil(date(2031, 10, 6), date(2031, 10, 12), agenda_completa=True)["equipe"]
    assert eq["leads"] == 2
    assert eq["atendidos_evolution"] == 1
    assert eq["conv_atendeu_evolution"] == 50.0
    # mês corrente (começa no dia 1) segue com o mensal
    eqm = painel.calcular_funil(date(2031, 10, 1), date(2031, 10, 6))["equipe"]
    assert eqm["atendidos_evolution"] == 33
