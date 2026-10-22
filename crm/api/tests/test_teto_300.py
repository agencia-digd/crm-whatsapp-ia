"""Teto até 300 por dia — e o número da tela parou de mentir.

Antes de 05/10/2026:
  - o campo "disparos por dia" recusava acima de 250;
  - a rampa tinha um degrau só (80), então NADA passava de 80;
  - quem digitava 200 gravava 200 e o motor mandava 80, calado.

O que estes testes seguram:

1. 300 é aceito no campo; 301 não;
2. sem nada gravado o teto continua 80 — publicar não muda o volume, nem com a
   linha GREEN;
3. com 300 gravado E a linha GREEN, o tick trabalha com 300;
4. com 300 gravado e a linha SEM GREEN, o tick fica em 80 — e o resumo da tela
   diz os dois números e o motivo;
5. o pedido alto nunca fura o freio: YELLOW segue 50, RED segue zero.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text as sql_text

from app.db.session import SessionLocal
from app.main import app
from app.services import carrossel_corretor as cb
from app.services import config_operacional as cfg
from app.services import rampa
from app.services import remarketing_agenda as ag

ADMIN = {"X-Painel-User": "dona"}
# A curva de verdade, guardada antes de qualquer fixture trocar por dublê.
_COTA_ORIGINAL = cb._cota_ate_agora


class _SessionFake:
    def __init__(self, n=400):
        self._fila = [{"id": i, "telefone": f"5500000000000{i:08d}"} for i in range(1, n + 1)]
        self._lim = n

    def execute(self, sql, params=None, *a, **k):
        if params and "lim" in params:
            self._lim = params["lim"]
        return self

    def mappings(self): return self
    def all(self): return self._fila[: self._lim]
    def scalar(self): return 0
    def commit(self): pass
    def rollback(self): pass
    def close(self): pass


@pytest.fixture(autouse=True)
def _sem_cache_da_rampa():
    rampa._cache.clear()
    yield
    rampa._cache.clear()


@pytest.fixture()
def config(monkeypatch):
    valores: dict = {"carrossel_corretor_ativo": True}
    monkeypatch.setattr(cfg, "_carregar", lambda: valores)
    return valores


@pytest.fixture()
def tick_com_rampa_real(monkeypatch, config):
    """Tudo liberado, e a RAMPA É A DE VERDADE — só a leitura da Meta é
    trocada. É a rampa que está sendo provada aqui, não um dublê dela."""
    enviados: list[int] = []
    monkeypatch.setattr(cb, "travar", lambda ns, t: None)
    monkeypatch.setattr(cb, "soltar", lambda *a, **k: None)
    monkeypatch.setattr(cb, "_voltar_pra_fila", lambda s, t: 0)
    monkeypatch.setattr(cb, "_voltar_respondeu_pra_fila", lambda s, t: 0)
    monkeypatch.setattr(cb, "_enviados_hoje", lambda s, t: 0)
    monkeypatch.setattr(cb, "_cota_ate_agora", lambda cap, agora=None, janela=None: cap)
    monkeypatch.setattr(cb, "_reservar", lambda s, i, t, *a: True)
    monkeypatch.setattr(cb, "_disparar", lambda s, lead, *a, **k: enviados.append(lead["id"]))
    monkeypatch.setattr(rampa, "_dias_ativos", lambda s, cid: 30)
    return enviados


def _qualidade(monkeypatch, q):
    monkeypatch.setattr(rampa, "_qualidade_meta", lambda slug: q)


def _tick():
    # max_por_tick alto: aqui o que se mede é o TETO DO DIA, não o cinto do tick.
    return cb.tick(session=_SessionFake(), sender=object(), max_por_tick=1000)


# ── 1. O campo aceita 300 ────────────────────────────────────────────────────


def test_300_e_aceito_no_campo():
    assert ag.CAP_DIARIO_MAXIMO == 300
    assert ag.validar_cap_diario(300) == 300
    assert ag.validar_cap_semanal(2100) == 2100


def test_acima_de_300_e_recusado():
    with pytest.raises(ValueError, match="0 a 300"):
        ag.validar_cap_diario(301)


def test_o_campo_e_a_rampa_tem_o_mesmo_maximo():
    """Se um subir sem o outro a tela volta a aceitar o que o motor não manda."""
    assert rampa.DEGRAU_MAXIMO == ag.CAP_DIARIO_MAXIMO


# ── 2. Sem pedido, 80 — publicar não muda o volume ───────────────────────────


@pytest.mark.parametrize("q", ["GREEN", "UNKNOWN"])
def test_sem_nada_gravado_o_teto_continua_80(monkeypatch, tick_com_rampa_real, q):
    _qualidade(monkeypatch, q)
    res = _tick()
    assert res.cap_do_dia == 80
    assert len(tick_com_rampa_real) == 80


# ── 3. A capacidade: 300 pedido + GREEN ──────────────────────────────────────


def test_300_gravado_e_linha_GREEN_dispara_300(monkeypatch, tick_com_rampa_real, config):
    config["remarketing_cap_diario"] = 300
    _qualidade(monkeypatch, "GREEN")
    res = _tick()
    assert res.cap_do_dia == 300
    assert len(tick_com_rampa_real) == 300


def test_200_gravado_e_linha_GREEN_dispara_200(monkeypatch, tick_com_rampa_real, config):
    """O caso do laudo: 200 na tela mandava 80. Agora manda 200 (com GREEN)."""
    config["remarketing_cap_diario"] = 200
    _qualidade(monkeypatch, "GREEN")
    assert _tick().cap_do_dia == 200
    assert len(tick_com_rampa_real) == 200


# ── 4. Pedido sem GREEN: fica em 80 e DIZ ────────────────────────────────────


def test_sem_leitura_de_qualidade_nao_sai_nada(monkeypatch, tick_com_rampa_real, config):
    """Meta fora do ar: teto zero, com ou sem pedido na tela (05/10/2026)."""
    config["remarketing_cap_diario"] = 300
    _qualidade(monkeypatch, None)
    res = _tick()
    assert res.cap_do_dia == 0
    assert tick_com_rampa_real == []
    assert "não consegui ler" in (res.motivo_parada or "")


@pytest.mark.parametrize("q", ["UNKNOWN"])
def test_300_gravado_sem_GREEN_fica_em_80(monkeypatch, tick_com_rampa_real, config, q):
    """UNKNOWN é o estado do número novo (2166). Sem reputação não sobe."""
    config["remarketing_cap_diario"] = 300
    _qualidade(monkeypatch, q)
    res = _tick()
    assert res.cap_do_dia == 80
    assert len(tick_com_rampa_real) == 80


def test_teto_efetivo_diz_os_dois_numeros_e_o_motivo(monkeypatch):
    """É isto que a tela mostra. 200 pedido, 80 valendo — e por quê."""
    _qualidade(monkeypatch, "UNKNOWN")
    monkeypatch.setattr(rampa, "_dias_ativos", lambda s, cid: 30)
    r = rampa.cap_de_hoje(object(), "corretor", 4, pedido=200)

    teto = ag.teto_efetivo(200, r.cap, r.motivo)

    assert teto["pedido"] == 200
    assert teto["efetivo"] == 80
    assert teto["diverge"] is True
    assert "200" in teto["explicacao"] and "80" in teto["explicacao"]
    assert "GREEN" in teto["explicacao"]


def test_teto_efetivo_sem_divergencia_nao_inventa_aviso():
    assert ag.teto_efetivo(40, 80, "degrau 1/2")["diverge"] is False
    assert ag.teto_efetivo(40, 80, "degrau 1/2")["efetivo"] == 40
    assert ag.teto_efetivo(None, 80, "degrau 1/2") == {
        "pedido": None, "rampa": 80, "efetivo": 80, "diverge": False,
        "explicacao": "sem limite próprio — vale o teto da rampa (80)",
        "cabe_no_horario": None,
    }


def test_teto_efetivo_sem_leitura_da_rampa_nao_e_numero():
    """Rampa ilegível: `efetivo` é None. Nunca o pedido, nunca 80 inventado."""
    teto = ag.teto_efetivo(300, None, None)
    assert teto["efetivo"] is None and teto["diverge"] is False


def test_mapa_da_tela_mostra_pedido_efetivo_e_motivo(monkeypatch, config):
    """Ponta a ponta do que a Dona vê: 200 gravado, linha UNKNOWN."""
    from app.api.automacoes import _mapa_carrossel_corretor

    config["remarketing_cap_diario"] = 200
    _qualidade(monkeypatch, "UNKNOWN")
    monkeypatch.setattr(rampa, "_dias_ativos", lambda s, cid: 30)

    ritmo = _mapa_carrossel_corretor()["ritmo"]

    assert ritmo["por_dia"] == 80
    assert ritmo["teto"]["pedido"] == 200
    assert ritmo["teto"]["efetivo"] == 80
    assert ritmo["teto"]["diverge"] is True
    assert "GREEN" in ritmo["teto"]["explicacao"]
    assert ritmo["cap_maximo_aceito"] == 300


def test_mapa_da_tela_com_GREEN_mostra_o_pedido_valendo(monkeypatch, config):
    from app.api.automacoes import _mapa_carrossel_corretor

    config["remarketing_cap_diario"] = 300
    _qualidade(monkeypatch, "GREEN")
    monkeypatch.setattr(rampa, "_dias_ativos", lambda s, cid: 30)

    ritmo = _mapa_carrossel_corretor()["ritmo"]

    assert ritmo["por_dia"] == 300
    assert ritmo["teto"]["diverge"] is False


# ── 4b. O horário também é teto ──────────────────────────────────────────────


def _hm(h, m=0):
    return h * 60 + m


@pytest.mark.parametrize("cap", [80, 200, 300])
def test_no_horario_padrao_o_teto_cabe(cap):
    """09:00–19:00, 3 a cada 3 min: 300 cabe com folga. Sem isto a capacidade
    de 300 seria só no papel."""
    assert cb.maximo_que_cabe_no_dia(cap) == cap


def test_janela_curta_nao_cabe_300():
    """Duas horas = 40 voltas de 3 = 120 no papel; com a curva (que começa
    devagar) saem 107. Gravar 300 com essa janela mandava 107 e a tela dizia 300."""
    assert cb.maximo_que_cabe_no_dia(300, (_hm(10), _hm(12))) == 107


def test_janela_curta_segura_ate_teto_pequeno():
    """Achado da simulação: até 80 numa janela de 2h não fecha — a curva só
    libera os 100% no fim da janela, quando o robô já não roda, e o que
    acumulou não sai todo (3 por volta). Saem 71."""
    assert cb.maximo_que_cabe_no_dia(80, (_hm(10), _hm(12))) == 71


def test_janela_gravada_ate_18h_fecha_um_abaixo():
    """Outro achado: com janela GRAVADA 09:00-18:00 a última volta é 17:57,
    quando a curva ainda não liberou tudo — o dia fecha em 79 de 80. Sem
    janela gravada o robô roda até 18:57 e fecha os 80."""
    assert cb.maximo_que_cabe_no_dia(80, (_hm(9), _hm(18))) == 79
    assert cb.maximo_que_cabe_no_dia(80) == 80


def test_cap_zero_nao_cabe_nada():
    assert cb.maximo_que_cabe_no_dia(0) == 0


def test_teto_efetivo_avisa_quando_o_horario_segura():
    teto = ag.teto_efetivo(300, 300, "degrau 2/2", cabe_no_horario=107)
    assert teto["efetivo"] == 107 and teto["diverge"] is True
    assert "107" in teto["explicacao"] and "horário" in teto["explicacao"]


def test_mapa_com_janela_curta_mostra_o_que_cabe(monkeypatch, config):
    from app.api.automacoes import _mapa_carrossel_corretor

    config["remarketing_cap_diario"] = 300
    config["remarketing_janela_horario"] = "10:00-12:00"
    _qualidade(monkeypatch, "GREEN")
    monkeypatch.setattr(rampa, "_dias_ativos", lambda s, cid: 30)

    ritmo = _mapa_carrossel_corretor()["ritmo"]

    assert ritmo["por_dia"] == 107
    assert ritmo["teto"]["diverge"] is True
    assert ritmo["teto"]["cabe_no_horario"] == 107


def test_simulacao_bate_com_o_tick_de_verdade(monkeypatch, tick_com_rampa_real, config):
    """A conta da tela e o motor não podem ser duas regras: roda o tick REAL
    em cada volta do dia, com janela de 2h, e compara com a simulação."""
    from datetime import datetime

    config["remarketing_cap_diario"] = 300
    config["remarketing_janela_horario"] = "10:00-12:00"
    _qualidade(monkeypatch, "GREEN")
    relogio = {"agora": None}
    monkeypatch.setattr(ag, "_agora_sp", lambda agora=None: relogio["agora"])
    monkeypatch.setattr(cb, "_enviados_hoje", lambda s, t: len(tick_com_rampa_real))
    # a curva de verdade (a fixture tinha trocado por "libera tudo"), no relógio do teste
    monkeypatch.setattr(
        cb, "_cota_ate_agora",
        lambda cap, agora=None, janela=None: _COTA_ORIGINAL(cap, relogio["agora"], janela=janela))

    for minuto in range(9 * 60, 19 * 60, 3):
        relogio["agora"] = datetime(2026, 10, 5, minuto // 60, minuto % 60, tzinfo=ag.TZ_SP)
        cb.tick(session=_SessionFake(), sender=object())

    assert len(tick_com_rampa_real) == 107      # o mesmo número da simulação


# ── 5. O pedido não fura freio ───────────────────────────────────────────────


@pytest.mark.parametrize("q,esperado,motivo", [
    ("YELLOW", 50, None),
    ("RED", 0, "qualidade RED — linha parada"),
    ("FLAGGED", 0, "qualidade FLAGGED — linha parada"),
])
def test_300_gravado_nao_fura_freio_de_qualidade(
        monkeypatch, tick_com_rampa_real, config, q, esperado, motivo):
    config["remarketing_cap_diario"] = 300
    _qualidade(monkeypatch, q)
    res = _tick()
    assert res.cap_do_dia == esperado
    assert len(tick_com_rampa_real) == esperado
    if motivo:
        assert res.motivo_parada == motivo


def test_300_gravado_ainda_respeita_o_teto_semanal(monkeypatch, tick_com_rampa_real, config):
    config["remarketing_cap_diario"] = 300
    config["remarketing_cap_semanal"] = 120
    _qualidade(monkeypatch, "GREEN")
    monkeypatch.setattr(ag, "enviados_na_semana", lambda s, t, ev: 100)
    res = _tick()
    assert len(tick_com_rampa_real) == 20


def test_lote_manual_com_override_nao_ganha_300_de_brinde(monkeypatch, tick_com_rampa_real, config):
    """`cap_override` (lote autorizado) segue valendo o número passado."""
    config["remarketing_cap_diario"] = 300
    _qualidade(monkeypatch, "GREEN")
    res = cb.tick(session=_SessionFake(), sender=object(), max_por_tick=1000,
                  cap_override=10, ignorar_ritmo=True)
    assert res.cap_do_dia == 10 and len(tick_com_rampa_real) == 10


# ── Endpoint: o caminho da tela ──────────────────────────────────────────────


@pytest.fixture()
def banco_limpo():
    def _wipe():
        with SessionLocal() as s:
            s.execute(sql_text("DELETE FROM config_operacional"))
            s.commit()
        cfg.invalidar_cache()

    _wipe()
    yield
    _wipe()


def test_endpoint_grava_300(banco_limpo):
    client = TestClient(app)
    r = client.put("/painel/automacoes/config/remarketing_cap_diario",
                   json={"valor": 300}, headers=ADMIN)
    assert r.status_code == 200, r.text
    assert ag.cap_diario() == 300


def test_endpoint_recusa_301_em_portugues(banco_limpo):
    client = TestClient(app)
    r = client.put("/painel/automacoes/config/remarketing_cap_diario",
                   json={"valor": 301}, headers=ADMIN)
    assert r.status_code == 422
    assert "0 a 300" in r.json()["detail"]
    with SessionLocal() as s:
        assert s.execute(sql_text("SELECT count(*) FROM config_operacional")).scalar() == 0


def test_o_padrao_nao_mudou(banco_limpo):
    """Trava do 'não mude o valor em produção': sem linha no banco não há
    pedido, e sem pedido a rampa é 80."""
    assert ag.cap_diario() is None
    assert cfg.DEFAULTS["remarketing_cap_diario"]() is None
    assert rampa.DEGRAU_BASE == 80
