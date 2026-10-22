"""Visão: comparecimento sobre visitas encerradas e ROI só de mídia.

Dois números da Visão enganavam em set/2026 (a Dona aprovou corrigir em 24/09):

1. COMPARECIMENTO 92,3% — 12 visitas ÷ 13 "agendados", com 26 visitas na
   agenda do mês: 12 compareceu, 11 cancelada, 1 no-show, 2 ainda por vir. O
   degrau "agendados" excluía cancelada e a taxa ignorava cancelada. Certo:
   compareceu ÷ encerradas (compareceu + no-show + cancelada) = 12/24 = 50%,
   e o degrau "agendados" bate com a agenda (26).

2. ROI 299,7x — VGV ÷ gasto Meta com a venda de INDICAÇÃO da Pessoa Clara
   (R$ 210.000) somada como se fosse retorno do anúncio. Certo: só
   `vendas.origem = 'midia'` entra no ROI e no custo por venda.

Os números abaixo são os de produção em 24/09/2026 (tabela `visitas`, tenant 1;
tabela `vendas`; gasto Meta de 01 a 24/09). DB de teste (conftest), zero prod.
"""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text as sql_text

import app.api.painel as painel
from app.db.session import SessionLocal

SP = ZoneInfo("America/Sao_Paulo")
CORRETOR, CORRETOR, CORRETOR = 1, 2, 3

GASTO_META_SET = 2584.88  # insights_diario, acme, 01–24/09/2026

# (corretora, dia, hora, lead, status) — as 26 visitas de set/2026, tenant 1
VISITAS_SET = [
    (CORRETOR, 2, "13:00", "Fulano", "compareceu"),
    (CORRETOR, 3, "09:30", "Fulano", "cancelada"),
    (CORRETOR, 3, "15:00", "Pessoa Clara e Josefa", "compareceu"),
    (CORRETOR, 5, "11:00", "Jose lauding", "cancelada"),
    (CORRETOR, 8, "16:30", "Dionne Almeida", "compareceu"),
    (CORRETOR, 9, "15:00", "renato", "compareceu"),
    (CORRETOR, 9, "15:00", "Cleo Farah", "cancelada"),
    (CORRETOR, 11, "10:00", "Fulano", "cancelada"),
    (CORRETOR, 12, "09:00", "Fulano", "compareceu"),
    (CORRETOR, 12, "10:00", "Fulano", "cancelada"),
    (CORRETOR, 12, "10:00", "Vitor Gabriel", "cancelada"),
    (CORRETOR, 12, "14:00", "Fulano", "cancelada"),
    (CORRETOR, 13, "14:33", "Isadora ferro", "compareceu"),
    (CORRETOR, 14, "10:00", "Pessoa1", "compareceu"),
    (CORRETOR, 14, "17:00", "Japinha", "cancelada"),
    (CORRETOR, 15, "10:00", "Gabrielli sturaro", "compareceu"),
    (CORRETOR, 17, "16:30", "Fulano", "cancelada"),
    (CORRETOR, 18, "15:00", "Val vieira", "no_show"),
    (CORRETOR, 18, "15:30", "Camila", "cancelada"),
    (CORRETOR, 19, "14:30", "Stefany", "compareceu"),
    (CORRETOR, 19, "15:00", "Fabiana", "cancelada"),
    (CORRETOR, 21, "15:00", "Stefany", "compareceu"),
    (CORRETOR, 22, "09:30", "Nil Vieira corrales", "compareceu"),
    (CORRETOR, 22, "15:30", "Christiane Pedroso Fulini", "compareceu"),
    (CORRETOR, 26, "13:00", "Fulano", "agendada"),
    (CORRETOR, 27, "10:00", "Fabiana", "agendada"),
]

# As 3 vendas de set/2026 e a origem que ficou em cada uma
VENDAS_SET = [
    (CORRETOR, "Pessoa Clara", 210000.00, date(2026, 9, 12), "indicacao"),
    (CORRETOR, "Pessoa8 Félix e Rodrigo Roquetti", 282754.20, date(2026, 9, 14), "midia"),
    (CORRETOR, "Lucilene", 282000.00, date(2026, 9, 24), "midia"),
]

# vendas_mensais de set/2026 como está em produção (o trigger soma `vendas`)
VENDAS_MENSAIS_SET = [(CORRETOR, 97, 0, 0.0), (CORRETOR, 65, 3, 774754.20), (CORRETOR, 5, 0, 0.0)]

INI_SET, FIM_SET = datetime(2026, 9, 1, tzinfo=SP), datetime(2026, 10, 1, tzinfo=SP)


def _limpa(s) -> None:
    s.rollback()
    s.execute(sql_text("DELETE FROM visitas WHERE data_hora >= :i AND data_hora < :f"),
              {"i": INI_SET, "f": FIM_SET})
    s.execute(sql_text("DELETE FROM vendas WHERE data_venda BETWEEN '2026-09-01' AND '2026-09-30'"))
    s.execute(sql_text("DELETE FROM vendas_mensais WHERE ano_mes = '2026-09'"))
    s.commit()


@pytest.fixture()
def setembro(monkeypatch):
    # Os dois só alimentam "atendidos"; consultam eventos do Kommo e não entram
    # em nenhuma das contas testadas aqui.
    monkeypatch.setattr(painel, "calcular_atendidos_breakdown",
                        lambda di, df: {"por_corretora": {}})
    monkeypatch.setattr(painel, "calcular_recuperados_por_corretora", lambda di, df: {})
    with SessionLocal() as s:
        _limpa(s)
        for cid, nome in ((CORRETOR, "Pessoa5"), (CORRETOR, "Pessoa1"), (CORRETOR, "Pessoa3")):
            s.execute(sql_text(
                "INSERT INTO corretoras (id, nome, telefone, ativo) "
                "VALUES (:i, :n, :t, TRUE) ON CONFLICT (id) DO UPDATE SET ativo = TRUE"),
                {"i": cid, "n": nome, "t": f"+5500000000000{cid:03d}"})
        for cid, dia, hora, lead, status in VISITAS_SET:
            h, m = (int(x) for x in hora.split(":"))
            s.execute(sql_text(
                "INSERT INTO visitas (tenant_id, corretora_id, lead_nome, data_hora, status) "
                "VALUES (1, :c, :n, :d, :st)"),
                {"c": cid, "n": lead, "d": datetime(2026, 9, dia, h, m, tzinfo=SP), "st": status})
        for cid, cliente, valor, quando, origem in VENDAS_SET:
            s.execute(sql_text(
                "INSERT INTO vendas (tenant_id, corretora_id, cliente_nome, valor, data_venda, origem) "
                "VALUES (1, :c, :n, :v, :d, :o)"),
                {"c": cid, "n": cliente, "v": valor, "d": quando, "o": origem})
        for cid, leads, vendas, vgv in VENDAS_MENSAIS_SET:
            s.execute(sql_text(
                "INSERT INTO vendas_mensais (corretora_id, ano_mes, qtde_vendas, vgv, qtde_leads) "
                "VALUES (:c, '2026-09', :v, :g, :l)"),
                {"c": cid, "v": vendas, "g": vgv, "l": leads})
        s.commit()
        yield s
        _limpa(s)


# ── 1. comparecimento ────────────────────────────────────────────────────────


def test_comparecimento_de_setembro_e_50_por_cento_das_encerradas(setembro):
    visitas = painel.listar_visitas(date(2026, 9, 1), date(2026, 9, 30))
    r = painel.resumo_comparecimento(visitas["por_status"])

    assert r["na_agenda"] == 26
    assert r["compareceu"] == 12
    assert r["canceladas"] == 11
    assert r["no_show"] == 1
    assert r["a_realizar"] == 2
    assert r["encerradas"] == 24, "denominador = compareceu + no-show + cancelada"
    assert r["taxa_comparecimento"] == 50.0


def test_formula_antiga_dava_92_e_ignorava_as_canceladas(setembro):
    """O número que estava no ar, pra registro: 12 ÷ (12 + 1)."""
    visitas = painel.listar_visitas(date(2026, 9, 1), date(2026, 9, 24))
    assert visitas["taxa_comparecimento"] == 92.3


def test_degrau_agendados_bate_com_a_agenda_do_mes(setembro):
    """Visão em "mês atual" (01–24/09) com a agenda até 30/09."""
    funil = painel.calcular_funil(date(2026, 9, 1), date(2026, 9, 24),
                                  agenda_completa=True, visitas_ate=date(2026, 9, 30))
    eq = funil["equipe"]
    assert eq["agendados"] == 26
    assert eq["visitas"] == 12
    assert eq["conv_visitou"] == 50.0, "mesma base do card de comparecimento"
    por = {c["corretora_id"]: c for c in funil["corretoras"]}
    assert (por[CORRETOR]["agendados"], por[CORRETOR]["agendados"], por[CORRETOR]["agendados"]) == (10, 13, 3)
    # Pessoa10: 6 compareceu de 9 encerradas (2 canceladas + 1 no-show); 1 a realizar
    assert por[CORRETOR]["conv_visitou"] == 66.7


def test_funil_sem_agenda_completa_segue_como_antes(setembro):
    """Ranking e comparativo ainda chamam sem a flag — o número deles não muda."""
    eq = painel.calcular_funil(date(2026, 9, 1), date(2026, 9, 24))["equipe"]
    assert (eq["agendados"], eq["visitas"], eq["conv_visitou"]) == (13, 12, 92.3)


def test_corretora_so_com_visita_nao_some_do_degrau(setembro):
    """Sem linha em vendas_mensais nem lead no período, a visita ainda conta."""
    setembro.execute(sql_text("DELETE FROM vendas_mensais WHERE ano_mes = '2026-09' AND corretora_id = :g"),
                     {"g": CORRETOR})
    setembro.commit()
    eq = painel.calcular_funil(date(2026, 9, 1), date(2026, 9, 24),
                               agenda_completa=True, visitas_ate=date(2026, 9, 30))["equipe"]
    assert eq["agendados"] == 26


# ── 2. retorno ───────────────────────────────────────────────────────────────


def test_vendas_de_setembro_por_origem(setembro):
    por = painel.vendas_por_origem(date(2026, 9, 1), date(2026, 9, 24))
    assert por["midia"]["vendas"] == 2
    assert por["midia"]["vgv"] == pytest.approx(564754.20)
    assert por["indicacao"]["vendas"] == 1
    assert por["indicacao"]["vgv"] == pytest.approx(210000.00)
    assert por["parceiro"]["vendas"] == 0
    assert por["nao_informada"]["vendas"] == 0


def test_roi_e_custo_por_venda_de_setembro_contam_so_midia(setembro):
    vgv_total = painel.calcular_funil(date(2026, 9, 1), date(2026, 9, 24))["equipe"]["vgv"]
    r = painel.montar_retorno(GASTO_META_SET, vgv_total,
                              painel.vendas_por_origem(date(2026, 9, 1), date(2026, 9, 24)))

    assert r["vgv_total"] == pytest.approx(774754.20), "VGV do card não muda"
    assert r["vgv_midia"] == pytest.approx(564754.20)
    assert r["vendas_midia"] == 2
    assert r["vgv_fora_midia"] == pytest.approx(210000.00)
    assert r["fora_midia"]["indicacao"] == {"vendas": 1, "vgv": 210000.00}
    assert r["fora_midia"]["nao_informada"]["vgv"] == 0
    assert r["roi"] == pytest.approx(218.48, abs=0.01)       # era 299,73
    assert r["custo_venda"] == pytest.approx(1292.44, abs=0.01)  # era 861,63
    assert r["vgv_midia"] + r["vgv_fora_midia"] == pytest.approx(r["vgv_total"])


def test_venda_sem_origem_fica_fora_do_roi(setembro):
    """Default da coluna é nao_informada: venda nova não infla o ROI sozinha."""
    setembro.execute(sql_text(
        "INSERT INTO vendas (tenant_id, corretora_id, cliente_nome, valor, data_venda) "
        "VALUES (1, :c, 'TST-sem origem', 300000, '2026-09-20') RETURNING origem"),
        {"c": CORRETOR})
    setembro.commit()
    por = painel.vendas_por_origem(date(2026, 9, 1), date(2026, 9, 24))
    assert por["nao_informada"] == {"vendas": 1, "vgv": 300000.0}
    r = painel.montar_retorno(GASTO_META_SET, 774754.20 + 300000, por)
    assert r["vgv_midia"] == pytest.approx(564754.20)
    assert r["fora_midia"]["nao_informada"]["vgv"] == pytest.approx(300000)


def test_origem_fora_da_lista_e_recusada_pelo_banco(setembro):
    with pytest.raises(Exception):
        setembro.execute(sql_text(
            "INSERT INTO vendas (tenant_id, corretora_id, cliente_nome, valor, data_venda, origem) "
            "VALUES (1, :c, 'TST-x', 1, '2026-09-20', 'instagram')"), {"c": CORRETOR})
    setembro.rollback()


def test_semana_nao_divide_venda_do_mes_por_gasto_de_7_dias(setembro):
    """Período que não começa no dia 1 conta só a venda de DENTRO dele, igual ao
    VGV do funil (06/10; antes zerava). Semana 21–27/09: só a Lucilene (24/09);
    as de 12 e 14/09 ficam fora — nunca a venda do mês sobre o gasto de 7 dias."""
    por = painel.vendas_por_origem(date(2026, 9, 21), date(2026, 9, 27))
    assert por["midia"] == {"vendas": 1, "vgv": pytest.approx(282000.00)}
    assert por["indicacao"]["vendas"] == 0
    assert painel.montar_retorno(700.0, 282000.0, por)["roi"] == pytest.approx(282000.0 / 700.0, abs=0.01)


def test_sem_venda_de_midia_custo_por_venda_fica_vazio():
    por = {o: {"vendas": 0, "vgv": 0.0} for o in painel.ORIGENS_VENDA}
    por["indicacao"] = {"vendas": 1, "vgv": 210000.0}
    r = painel.montar_retorno(GASTO_META_SET, 210000.0, por)
    assert r["custo_venda"] is None
    assert r["roi"] == 0.0
    assert r["vgv_fora_midia"] == 210000.0
