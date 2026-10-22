"""Anti-ghosting — as travas que protegem o lead e a linha.

O que estes testes garantem, em ordem de dano se quebrar:

  1. NÃO manda texto livre fora da janela de 24h (erro na Meta, mancha a linha);
  2. NÃO manda pra quem pediu pra sair / já encontrou / número morto;
  3. NÃO manda pra quem a corretora já respondeu (não há ghosting nenhum);
  4. NÃO manda duas vezes pela mesma parada (a diferença entre reabrir uma
     conversa e perseguir alguém);
  5. NÃO manda de madrugada nem no domingo.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.services import anti_ghosting as ag

SP = ZoneInfo("America/Sao_Paulo")


# ── horário civilizado ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "quando,esperado",
    [
        (datetime(2026, 8, 5, 10, 0, tzinfo=SP), True),    # terça 10h
        (datetime(2026, 8, 5, 18, 59, tzinfo=SP), True),   # terça 18h59
        (datetime(2026, 8, 5, 19, 0, tzinfo=SP), False),   # 19h já é tarde
        (datetime(2026, 8, 5, 8, 59, tzinfo=SP), False),   # antes das 9h
        (datetime(2026, 8, 5, 23, 0, tzinfo=SP), False),   # o "👀" das 23h
        (datetime(2026, 8, 5, 3, 0, tzinfo=SP), False),    # madrugada
        (datetime(2026, 8, 8, 11, 0, tzinfo=SP), True),    # sábado vale
        (datetime(2026, 8, 9, 11, 0, tzinfo=SP), False),   # domingo não
    ],
)
def test_horario_civilizado(quando, esperado):
    assert ag.dentro_do_horario(quando) is esperado


def test_horario_usa_fuso_de_sao_paulo_e_nao_utc():
    """23h em SP é 2h da manhã do dia seguinte em UTC. Se a checagem rodar em
    UTC, o "👀" sai na madrugada de quem está dormindo."""
    onze_da_noite_sp = datetime(2026, 8, 5, 23, 30, tzinfo=SP)
    assert ag.dentro_do_horario(onze_da_noite_sp) is False
    # o mesmo instante, expresso em UTC, tem que dar a MESMA resposta
    assert ag.dentro_do_horario(onze_da_noite_sp.astimezone(timezone.utc)) is False


# ── a janela de 24h ──────────────────────────────────────────────────────────


def test_margem_da_janela_deixa_folga_real():
    """A janela da Meta é 24h. Parar em 20h dá 4h de folga pra tick atrasado.
    Se alguém encostar isso em 24h, o teste cai — e é pra cair."""
    assert ag.JANELA_MAX_H <= 20.0, "sem folga, texto livre vira erro na Meta"


def test_toques_cabem_dentro_da_janela():
    """Nenhum toque pode qualificar depois do teto da janela."""
    for t in ag.TOQUES:
        assert t.max_h <= ag.JANELA_MAX_H, f"toque {t.numero} passa da janela"
        assert t.min_h < t.max_h


def test_sao_cinco_toques_e_nenhum_depende_de_template():
    """Eram dois; a Dona pediu cinco em 15/09/2026 (commit 5637abf2). A regra
    que este teste guarda não mudou: nenhum toque pode qualificar a partir das
    20h, porque aí a janela fechou e o que sai é template pago — outra
    automação, outro custo, outra aprovação. Um sexto toque (ou um quinto que
    passe da margem) cai aqui antes de virar erro na Meta."""
    assert len(ag.TOQUES) == 5
    assert [t.numero for t in ag.TOQUES] == [1, 2, 3, 4, 5]
    assert ag.TOQUES[0].texto == "👀"
    assert all(t.min_h < ag.JANELA_MAX_H for t in ag.TOQUES)
    # Faixas encostadas, sem buraco nem sobreposição: uma parada cai em UM toque.
    for anterior, seguinte in zip(ag.TOQUES, ag.TOQUES[1:]):
        assert anterior.max_h == seguinte.min_h


# ── seleção: quem entra e quem não entra ─────────────────────────────────────


class _FakeSession:
    """Sessão mínima: devolve as linhas de conversa parada e responde às
    consultas de dedup e bloqueio conforme o cenário."""

    def __init__(self, paradas, ja_mandou=False, bloqueio=None, e_lead=True):
        self.paradas = paradas
        self.ja_mandou = ja_mandou
        self.bloqueio = bloqueio  # None | "opt_out" | "morto" | "interno"
        self.e_lead = e_lead

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        txt = str(sql)
        paradas, ja, bloq, e_lead = (
            self.paradas, self.ja_mandou, self.bloqueio, self.e_lead,
        )

        class R:
            def mappings(self_inner):
                return self_inner

            def all(self_inner):
                return paradas

            def first(self_inner):
                if "anti_ghosting_toques" in txt:
                    return (1,) if ja else None
                if "internos_acme" in txt:
                    return (1,) if bloq == "interno" else None
                if "telefone_invalido" in txt:
                    return (1,) if bloq == "morto" else None
                if "FROM leads" in txt:                    # é lead?
                    return (1,) if e_lead else None
                if "cadencia_estado" in txt:
                    return (1,) if bloq == "opt_out" else None
                return None

        return R()

    def commit(self):
        pass


def _parada(horas_atras: float, tel="+5500000000000"):
    return {
        "instance": "acme_corretora_c",
        "telefone": tel,
        "nome": "Corretor",
        "corretora_id": 2,
        "ultima_em": datetime.now(timezone.utc) - timedelta(hours=horas_atras),
    }


def _selecionar(monkeypatch, paradas, **kw):
    monkeypatch.setattr(ag, "SessionLocal", lambda: _FakeSession(paradas, **kw))
    return ag.selecionar()


def test_conversa_parada_1h45_recebe_o_primeiro_toque(monkeypatch):
    got = _selecionar(monkeypatch, [_parada(1.75)])
    assert len(got) == 1
    assert got[0].toque.numero == 1
    assert got[0].toque.texto == "👀"


def test_parada_recente_demais_nao_entra(monkeypatch):
    """1h de silêncio é conversa normal, não ghosting."""
    assert _selecionar(monkeypatch, [_parada(1.0)]) == []


def test_parada_de_4h_pula_direto_pro_segundo_toque(monkeypatch):
    """Tick atrasado não deve mandar o "👀" fora de hora e o segundo em
    seguida — entra no maior toque que couber."""
    got = _selecionar(monkeypatch, [_parada(4.0)])
    assert len(got) == 1
    assert got[0].toque.numero == 2


def test_parada_de_8h_recebe_o_terceiro_toque_e_nao_o_olhinho(monkeypatch):
    """8h parado: o "👀" já ficou estranho — continua não saindo. Com dois
    toques isso significava "ninguém recebe"; com cinco (15/09/2026) a parada
    de 8h cai na faixa do 3º toque, que retoma o assunto sem cobrar."""
    cands = _selecionar(monkeypatch, [_parada(8.0)])
    assert len(cands) == 1
    assert cands[0].toque.numero == 3
    assert cands[0].toque.texto != "👀"


def test_fora_da_janela_de_24h_nunca_entra(monkeypatch):
    """A trava mais cara da lista: texto livre fora da janela é erro na Meta."""
    for horas in (20.0, 23.0, 26.0, 40.0):
        assert _selecionar(monkeypatch, [_parada(horas)]) == [], f"{horas}h passou"


def test_nao_repete_o_mesmo_toque_na_mesma_parada(monkeypatch):
    assert _selecionar(monkeypatch, [_parada(1.75)], ja_mandou=True) == []


def test_quem_pediu_pra_sair_nao_recebe(monkeypatch):
    assert _selecionar(monkeypatch, [_parada(1.75)], bloqueio="opt_out") == []


def test_telefone_marcado_como_morto_nao_recebe(monkeypatch):
    assert _selecionar(monkeypatch, [_parada(1.75)], bloqueio="morto") == []


def test_contato_pessoal_da_corretora_nunca_recebe(monkeypatch):
    """A trava mais constrangedora se faltar — e faltou na primeira versão.

    A linha da corretora é o WhatsApp de trabalho, mas tem a vida dela junto: na
    primeira medição em produção, 3 dos 5 candidatos eram contato pessoal
    ("Marcelinho", "|_Jotapê_|"). Quem não está no CRM não é lead e não recebe.
    """
    assert _selecionar(monkeypatch, [_parada(1.75)], e_lead=False) == []


def test_equipe_da_casa_nao_recebe(monkeypatch):
    """Corretora, dona e gerente conversam entre si pelas mesmas linhas. "👀"
    automático pra colega é constrangimento, não automação."""
    assert _selecionar(monkeypatch, [_parada(1.75)], bloqueio="interno") == []


def test_checagem_de_bloqueio_falha_fechado(monkeypatch):
    """Se a consulta de opt-out estourar, NÃO manda. Perder um toque é barato;
    mandar pra quem pediu pra sair, não."""

    class _Explode(_FakeSession):
        def execute(self, sql, params=None):
            if "cadencia_estado" in str(sql):
                raise RuntimeError("banco fora")
            return super().execute(sql, params)

    monkeypatch.setattr(ag, "SessionLocal", lambda: _Explode([_parada(1.75)]))
    assert ag.selecionar() == []


# ── o tick ───────────────────────────────────────────────────────────────────


def test_desligado_nao_faz_nada(monkeypatch):
    """Nasce desligado. Ligar é ato deliberado no painel."""
    monkeypatch.setattr(ag.cfg, "anti_ghosting_ativo", lambda: False)
    r = ag.rodar()
    assert r["enviados"] == 0
    assert "desligado" in r["pulados"]


def test_fora_do_horario_nao_varre_o_banco(monkeypatch):
    monkeypatch.setattr(ag.cfg, "anti_ghosting_ativo", lambda: True)
    monkeypatch.setattr(ag, "dentro_do_horario", lambda *a: False)

    def _nao_deveria():
        raise AssertionError("varreu o banco fora do horário")

    monkeypatch.setattr(ag, "selecionar", lambda **kw: _nao_deveria())
    r = ag.rodar()
    assert "fora_do_horario" in r["pulados"]
    assert r["enviados"] == 0
