"""Agenda do remarketing — janela de horário, dias da semana e caps do painel.

A Dona passa a configurar pela tela QUANDO e QUANTO o carrossel da Corretor dispara.
O que estes testes seguram, em ordem de estrago se quebrar:

1. Fora da janela ou do dia, o tick NÃO envia — com fila cheia e tudo liberado.
2. O cap diário do painel só BAIXA: nunca passa da rampa.
3. O cap semanal trava a semana e corta o lote que estouraria o teto.
4. Sem nada gravado, o comportamento é o de antes (publicar não muda disparo).
5. Valor torto é recusado com 422 em português — e, se chegar no banco por
   fora da tela, o tick SEGURA em vez de adivinhar.
"""
from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text as sql_text

from app.db.session import SessionLocal
from app.main import app
from app.services import carrossel_corretor as cb
from app.services import config_operacional as cfg
from app.services import remarketing_agenda as ag

ADMIN = {"X-Painel-User": "dona"}

# 2026-10-05 é segunda (weekday 0); 10 = sábado; 11 = domingo.
SEG_10H = datetime(2026, 10, 5, 10, 0, tzinfo=ag.TZ_SP)
SEG_08H = datetime(2026, 10, 5, 8, 30, tzinfo=ag.TZ_SP)
SEG_18H = datetime(2026, 10, 5, 18, 0, tzinfo=ag.TZ_SP)
SAB_10H = datetime(2026, 10, 10, 10, 0, tzinfo=ag.TZ_SP)


class SenderFake:
    def __init__(self):
        self.chamadas = []

    def __call__(self, telefone, texto, *, com_botao, **kw):
        self.chamadas.append(telefone)
        return {"ok": True, "dry_run": True, "sent": False}


class _SessionFake:
    """Devolve a fila respeitando o `LIMIT :lim` — é o limite que o tick
    calculou, e é ele que os testes de cap conferem."""

    def __init__(self, fila):
        self._fila = fila
        self._lim = len(fila)

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


FILA = [{"id": i, "telefone": f"+5500000000000{i}"} for i in range(1, 4)]


@pytest.fixture()
def config(monkeypatch):
    """Config em memória: o que estaria gravado em `config_operacional`."""
    valores: dict = {}
    monkeypatch.setattr(cfg, "_carregar", lambda: valores)
    return valores


@pytest.fixture()
def tick_liberado(monkeypatch, config):
    """Tudo que deixaria o tick disparar: ligado, rampa GREEN, fila cheia.

    Só a agenda fica entre a fila e o envio — é o que cada teste mexe. O
    `enviados` conta o que chegou em `_disparar`, ou seja, o que teria saído.
    """
    from app.services import rampa

    config["carrossel_corretor_ativo"] = True
    enviados: list[int] = []
    monkeypatch.setattr(cb, "travar", lambda ns, t: None)
    monkeypatch.setattr(cb, "soltar", lambda *a, **k: None)
    monkeypatch.setattr(cb, "_voltar_pra_fila", lambda s, t: 0)
    monkeypatch.setattr(cb, "_voltar_respondeu_pra_fila", lambda s, t: 0)
    monkeypatch.setattr(cb, "_enviados_hoje", lambda s, t: 0)
    monkeypatch.setattr(cb, "_cota_ate_agora", lambda cap, agora=None, janela=None: cap)
    monkeypatch.setattr(cb, "_reservar", lambda s, i, t, *a: True)
    monkeypatch.setattr(cb, "_disparar",
                        lambda s, lead, *a, **k: enviados.append(lead["id"]))
    monkeypatch.setattr(rampa, "cap_de_hoje",
                        lambda s, slug, cid, **k: rampa.Rampa("corretor", 80, 0, 9, "GREEN", "ok"))
    return enviados


def _agora(monkeypatch, quando: datetime) -> None:
    monkeypatch.setattr(ag, "_agora_sp", lambda agora=None: quando)


def _tick(**kw):
    return cb.tick(session=_SessionFake(FILA), sender=SenderFake(), **kw)


# ── 1. Janela de horário ─────────────────────────────────────────────────────


def test_fora_da_janela_NAO_envia(monkeypatch, tick_liberado, config):
    """A prova pedida: ligado, GREEN, fila cheia — e 08:30 com janela 09-18."""
    config["remarketing_janela_horario"] = "09:00-18:00"
    _agora(monkeypatch, SEG_08H)

    res = _tick()

    assert tick_liberado == [], "saiu mensagem fora da janela"
    assert res.enviados == 0
    assert res.motivo_parada == "fora_da_janela"


def test_dentro_da_janela_envia(monkeypatch, tick_liberado, config):
    """O controle do teste de cima: mesma montagem, hora dentro → sai.
    Sem isto, 'não enviou' poderia ser o tick quebrado, não a janela."""
    config["remarketing_janela_horario"] = "09:00-18:00"
    _agora(monkeypatch, SEG_10H)

    res = _tick()

    assert len(tick_liberado) == cb.MAX_POR_TICK
    assert res.motivo_parada is None


def test_o_fim_da_janela_e_exclusivo(monkeypatch, tick_liberado, config):
    """18:00 em ponto com janela até 18:00 já é fora."""
    config["remarketing_janela_horario"] = "09:00-18:00"
    _agora(monkeypatch, SEG_18H)
    assert _tick().motivo_parada == "fora_da_janela"
    assert tick_liberado == []


def test_fora_da_janela_ainda_devolve_quem_descansou(monkeypatch, tick_liberado, config):
    """Arrumar a fila não é envio: continua fora da janela, senão a segunda
    abre com a fila do tamanho de sexta."""
    config["remarketing_janela_horario"] = "09:00-18:00"
    _agora(monkeypatch, SEG_08H)
    monkeypatch.setattr(cb, "_voltar_pra_fila", lambda s, t: 7)
    res = _tick()
    assert res.voltaram_pra_fila == 7
    assert tick_liberado == []


def test_fora_da_janela_nem_consulta_a_rampa(monkeypatch, tick_liberado, config):
    from app.services import rampa

    config["remarketing_janela_horario"] = "09:00-18:00"
    _agora(monkeypatch, SEG_08H)
    monkeypatch.setattr(rampa, "cap_de_hoje",
                        lambda *a, **k: pytest.fail("consultou a rampa fora da janela"))
    _tick()


# ── 2. Dias da semana ────────────────────────────────────────────────────────


def test_dia_nao_escolhido_NAO_envia(monkeypatch, tick_liberado, config):
    config["remarketing_dias_semana"] = [0, 1, 2, 3, 4]
    _agora(monkeypatch, SAB_10H)
    res = _tick()
    assert tick_liberado == []
    assert res.motivo_parada == "fora_dos_dias"


def test_dia_escolhido_envia(monkeypatch, tick_liberado, config):
    config["remarketing_dias_semana"] = [5]   # só sábado
    _agora(monkeypatch, SAB_10H)
    _tick()
    assert len(tick_liberado) == cb.MAX_POR_TICK


def test_lista_vazia_mantem_o_comportamento_de_antes(monkeypatch, tick_liberado, config):
    """Vazio = todo dia. O carrossel nunca leu `envio_dias_uteis` — sábado
    sempre disparou, e publicar esta mudança não pode alterar isso sozinho."""
    _agora(monkeypatch, SAB_10H)
    config["envio_dias_uteis"] = True   # é dos outros motores; aqui não pesa
    _tick()
    assert len(tick_liberado) == cb.MAX_POR_TICK


def test_sem_nada_gravado_nao_ha_bloqueio():
    """Nenhuma das quatro chaves → agenda não segura em hora nenhuma."""
    cfg.invalidar_cache()
    for quando in (SEG_08H, SEG_10H, SAB_10H):
        # banco de teste sem linha em config_operacional pras chaves novas
        with SessionLocal() as s:
            s.execute(sql_text(
                "DELETE FROM config_operacional WHERE chave LIKE 'remarketing_%'"))
            s.commit()
        cfg.invalidar_cache()
        assert ag.bloqueio_de_agenda(quando) is None


# ── 3. Cap diário do painel: nunca passa da rampa ────────────────────────────────────────


def test_cap_do_painel_baixa_o_limite(monkeypatch, tick_liberado, config):
    config["remarketing_cap_diario"] = 2      # rampa diz 80
    res = _tick()
    assert res.cap_do_dia == 2
    assert len(tick_liberado) == 2


def test_cap_do_painel_NAO_passa_da_rampa(monkeypatch, tick_liberado, config):
    """Número digitado na tela não fura o freio de qualidade."""
    config["remarketing_cap_diario"] = 250    # rampa diz 80
    res = _tick()
    assert res.cap_do_dia == 80


def test_rampa_zerada_continua_zerada(monkeypatch, tick_liberado, config):
    """Linha RED: o painel pedindo 50 não ressuscita o disparo."""
    from app.services import rampa

    config["remarketing_cap_diario"] = 50
    monkeypatch.setattr(
        rampa, "cap_de_hoje",
        lambda s, slug, cid, **k: rampa.Rampa(slug, 0, 0, 0, "RED", "qualidade RED — linha parada"))
    res = _tick()
    assert tick_liberado == []
    assert res.motivo_parada == "qualidade RED — linha parada"


def test_zero_no_painel_para_e_diz_que_foi_o_painel(monkeypatch, tick_liberado, config):
    config["remarketing_cap_diario"] = 0
    res = _tick()
    assert tick_liberado == []
    assert res.motivo_parada == "cap_do_dia_zerado_no_painel"


# ── 4. Cap semanal ───────────────────────────────────────────────────────────


def test_cap_semanal_atingido_NAO_envia(monkeypatch, tick_liberado, config):
    config["remarketing_cap_semanal"] = 100
    monkeypatch.setattr(ag, "enviados_na_semana", lambda s, t, ev: 100)
    res = _tick()
    assert tick_liberado == []
    assert res.motivo_parada == "cap_semanal_atingido"
    assert res.ja_enviados_semana == 100


def test_saldo_da_semana_corta_o_lote(monkeypatch, tick_liberado, config):
    """Faltando 1 pro teto, sai 1 — não os 3 do MAX_POR_TICK."""
    config["remarketing_cap_semanal"] = 100
    monkeypatch.setattr(ag, "enviados_na_semana", lambda s, t, ev: 99)
    _tick()
    assert len(tick_liberado) == 1


def test_contagem_da_semana_que_falha_assume_cheio(monkeypatch, tick_liberado, config):
    """Erro nunca libera volume."""
    class _Quebra:
        def execute(self, *a, **k): raise RuntimeError("banco fora")

    assert ag.enviados_na_semana(_Quebra(), 1, "x") >= 10**6


def test_diario_e_semanal_valem_juntos(monkeypatch, tick_liberado, config):
    """O que estourar primeiro trava: semana com saldo, dia já cheio."""
    config["remarketing_cap_semanal"] = 500
    config["remarketing_cap_diario"] = 10
    monkeypatch.setattr(ag, "enviados_na_semana", lambda s, t, ev: 40)
    monkeypatch.setattr(cb, "_enviados_hoje", lambda s, t: 10)
    res = _tick()
    assert tick_liberado == []
    assert res.motivo_parada == "cap_do_dia_atingido"


def test_semana_conta_de_segunda_a_domingo_no_fuso_de_sp():
    """Conta só o que saiu desde segunda 00:00 de São Paulo, e só do tenant."""
    with SessionLocal() as s:
        s.execute(sql_text("DELETE FROM lead_events WHERE tipo = 'teste_semana'"))
        s.execute(sql_text(
            """
            INSERT INTO lead_events (tenant_id, lead_id, tipo, at, payload)
            SELECT t, NULL, 'teste_semana', quando, '{}'::jsonb FROM (VALUES
              (1, now()),
              (1, (date_trunc('week', now() AT TIME ZONE 'America/Sao_Paulo')
                   + interval '1 minute') AT TIME ZONE 'America/Sao_Paulo'),
              (1, (date_trunc('week', now() AT TIME ZONE 'America/Sao_Paulo')
                   - interval '1 minute') AT TIME ZONE 'America/Sao_Paulo'),
              (2, now())
            ) AS v(t, quando)
            """))
        s.commit()
        try:
            assert ag.enviados_na_semana(s, 1, "teste_semana") == 2
        finally:
            s.execute(sql_text("DELETE FROM lead_events WHERE tipo = 'teste_semana'"))
            s.commit()


# ── 5. Lote manual autorizado ────────────────────────────────────────────────


def test_lote_manual_OBEDECE_a_agenda(monkeypatch, tick_liberado, config):
    """`carrossel_burst.py` (ignorar_ritmo) só ignora a CURVA do dia.

    Até 05/10/2026 o lote manual pulava a agenda — uma segunda autoridade, por
    terminal, passando por cima da tela. Ordem da Dona: o painel manda. Sábado
    com a tela dizendo "só segunda": não sai, nem pelo lote."""
    config["remarketing_janela_horario"] = "09:00-18:00"
    config["remarketing_dias_semana"] = [0]
    _agora(monkeypatch, SAB_10H)
    res = _tick(ignorar_ritmo=True)
    assert tick_liberado == []
    assert res.motivo_parada == "fora_dos_dias"


def test_tick_do_timer_nao_ignora_ritmo():
    """O timer chama `tick()` puro — a agenda vale no dia a dia."""
    import pathlib

    fonte = pathlib.Path("scripts/cadencia_tick.py").read_text()
    assert "carrossel_corretor.tick()" in fonte


# ── 6. Valor ilegível no banco: segura ───────────────────────────────────────


@pytest.mark.parametrize("chave,lixo", [
    ("remarketing_janela_horario", "das nove às seis"),
    ("remarketing_dias_semana", [9]),
])
def test_agenda_ilegivel_segura(monkeypatch, tick_liberado, config, chave, lixo):
    config[chave] = lixo
    res = _tick()
    assert tick_liberado == []
    assert res.motivo_parada == "agenda_ilegivel"


def test_cap_ilegivel_segura(monkeypatch, tick_liberado, config):
    config["remarketing_cap_semanal"] = -5
    res = _tick()
    assert tick_liberado == []
    assert res.motivo_parada == "agenda_ilegivel"


# ── 7. Curva de cota dentro da janela ────────────────────────────────────────


def _sp(h, m=0):
    return datetime(2026, 7, 30, h, m, tzinfo=cb.TZ_SP)


def test_janela_padrao_nao_muda_a_curva():
    for h in range(7, 20):
        assert cb._cota_ate_agora(100, _sp(h), janela=(540, 1080)) \
            == cb._cota_ate_agora(100, _sp(h))


def test_janela_curta_libera_o_cap_inteiro_ate_o_fim():
    """10:00-16:00: às 16h o cap do dia inteiro já pôde sair. Sem esticar a
    curva, fecharia em ~70% e a Dona veria 56 de 80 sem explicação."""
    j = (10 * 60, 16 * 60)
    assert cb._cota_ate_agora(80, _sp(10), janela=j) == 0
    assert cb._cota_ate_agora(80, _sp(16), janela=j) == 80
    meio = [cb._cota_ate_agora(80, _sp(h), janela=j) for h in range(10, 17)]
    assert meio == sorted(meio)


# ── 8. Validação: o que a tela grava ─────────────────────────────────────────


@pytest.mark.parametrize("valor,esperado", [
    ("09:00-18:00", "09:00-18:00"),
    ("9:30 - 12:00", "09:30-12:00"),
    (None, None),
    ("", None),
])
def test_janela_valida(valor, esperado):
    assert ag.validar_janela(valor) == esperado


@pytest.mark.parametrize("valor", [
    "18:00-09:00", "09:00-09:00", "25:00-26:00", "09:61-10:00", "9-18",
    "manhã", 900, ["09:00", "18:00"],
])
def test_janela_invalida(valor):
    with pytest.raises(ValueError):
        ag.validar_janela(valor)


@pytest.mark.parametrize("valor", ["07:00-12:00", "14:00-22:00"])
def test_janela_fora_do_timer_e_recusada(valor):
    """O timer só acorda das 09 às 19 (SP): aceitar seria gravar um horário
    em que nada nunca dispara."""
    with pytest.raises(ValueError, match="robô"):
        ag.validar_janela(valor)


@pytest.mark.parametrize("valor,esperado", [
    ([4, 0, 2], [0, 2, 4]), ([], []), (None, []), ([6], [6]),
])
def test_dias_validos(valor, esperado):
    assert ag.validar_dias(valor) == esperado


@pytest.mark.parametrize("valor", [[7], [-1], [0, 0], ["seg"], [True], "0,1", [1.5]])
def test_dias_invalidos(valor):
    with pytest.raises(ValueError):
        ag.validar_dias(valor)


@pytest.mark.parametrize("valor", [-1, 301, "10", 1.5, True])
def test_cap_diario_invalido(valor):
    with pytest.raises(ValueError):
        ag.validar_cap_diario(valor)


@pytest.mark.parametrize("valor", [-1, 2101, "100", False])
def test_cap_semanal_invalido(valor):
    with pytest.raises(ValueError):
        ag.validar_cap_semanal(valor)


def test_caps_validos():
    assert ag.validar_cap_diario(0) == 0
    assert ag.validar_cap_diario(None) is None
    assert ag.validar_cap_semanal(1750) == 1750
    # 300/dia é a capacidade pedida em 05/10 (era 250)
    assert ag.validar_cap_diario(300) == 300
    assert ag.validar_cap_semanal(2100) == 2100


# ── 9. Endpoint: mesmo caminho do interruptor, 422 em PT-BR, autor gravado ───


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


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


@pytest.mark.parametrize("chave,valor,trecho", [
    ("remarketing_janela_horario", "18:00-09:00", "começar antes"),
    ("remarketing_janela_horario", "abc", "horário inválido"),
    ("remarketing_dias_semana", [7], "dia da semana inválido"),
    ("remarketing_cap_semanal", -1, "não pode ser negativo"),
    ("remarketing_cap_diario", -3, "não pode ser negativo"),
    ("remarketing_cap_diario", 9999, "fora da faixa"),
])
def test_endpoint_recusa_com_422_em_portugues(client, banco_limpo, chave, valor, trecho):
    r = client.put(f"/painel/automacoes/config/{chave}", json={"valor": valor},
                   headers=ADMIN)
    assert r.status_code == 422, r.text
    assert trecho in r.json()["detail"]
    with SessionLocal() as s:
        n = s.execute(sql_text(
            "SELECT count(*) FROM config_operacional WHERE chave = :k"),
            {"k": chave}).scalar()
    assert n == 0, "valor recusado não pode ter sido gravado"


def test_endpoint_grava_as_quatro_com_autor(client, banco_limpo):
    gravar = {
        "remarketing_janela_horario": ("9:00-17:30", "09:00-17:30"),
        "remarketing_dias_semana": ([4, 1], [1, 4]),
        "remarketing_cap_diario": (40, 40),
        "remarketing_cap_semanal": (200, 200),
    }
    for chave, (mandado, gravado) in gravar.items():
        r = client.put(f"/painel/automacoes/config/{chave}",
                       json={"valor": mandado}, headers=ADMIN)
        assert r.status_code == 200, r.text
        est = r.json()["config"][chave]
        assert est["valor"] == gravado
        assert est["personalizado"] is True
    with SessionLocal() as s:
        autores = {r[0] for r in s.execute(sql_text(
            "SELECT atualizado_por FROM config_operacional "
            "WHERE chave LIKE 'remarketing_%'")).all()}
    assert autores == {"dona"}
    assert ag.janela() == (540, 1050)
    assert ag.dias_semana() == [1, 4]


def test_endpoint_volta_ao_padrao(client, banco_limpo):
    client.put("/painel/automacoes/config/remarketing_cap_semanal",
               json={"valor": 10}, headers=ADMIN)
    r = client.delete("/painel/automacoes/config/remarketing_cap_semanal",
                      headers=ADMIN)
    assert r.status_code == 200
    assert ag.cap_semanal() is None


def test_gravar_a_agenda_nao_liga_o_carrossel(client, banco_limpo):
    """Mexer em horário não é ligar. O interruptor é outra chave, outro clique."""
    antes = cfg.carrossel_corretor_ativo()
    client.put("/painel/automacoes/config/remarketing_janela_horario",
               json={"valor": "09:00-18:00"}, headers=ADMIN)
    assert cfg.carrossel_corretor_ativo() is antes is False


# ── 10. Resumo pra tela: teto E saldo ────────────────────────────────────────


def test_resumo_traz_o_gasto_e_o_saldo(monkeypatch, config):
    config["remarketing_cap_semanal"] = 200
    monkeypatch.setattr(ag, "enviados_na_semana", lambda s, t, ev: 150)
    r = ag.resumo(object(), 1, "x", agora=SEG_10H)
    assert (r["cap_semanal"], r["enviados_semana"], r["saldo_semana"]) == (200, 150, 50)
    assert r["bloqueio_agora"] is None


def test_resumo_sem_contagem_nao_vira_zero(monkeypatch, config):
    config["remarketing_cap_semanal"] = 200
    monkeypatch.setattr(ag, "enviados_na_semana", lambda s, t, ev: 10**6)
    r = ag.resumo(object(), 1, "x", agora=SEG_10H)
    assert r["enviados_semana"] is None
    assert r["enviados_semana_indisponivel"] is True
    assert r["saldo_semana"] is None
    assert r["bloqueio_agora"] == "cap_semanal_atingido"


def test_resumo_diz_por_que_nao_dispara_agora(monkeypatch, config):
    config["remarketing_dias_semana"] = [0, 1, 2, 3, 4]
    monkeypatch.setattr(ag, "enviados_na_semana", lambda s, t, ev: 0)
    r = ag.resumo(object(), 1, "x", agora=SAB_10H)
    assert r["bloqueio_agora"] == "fora_dos_dias"
    assert r["bloqueio_agora_texto"] == "hoje não é dia de disparo"
