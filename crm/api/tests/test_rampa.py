"""Rampa anti-ban da linha da Corretor (0000-0000, ex-0000-0000).

A linha é o telefone DA CASA: se ele leva ban, a Acme perde o número
principal, não só uma automação. E são 5.253 leads na fila esperando.

O que estes testes protegem: a rampa nunca pode LIBERAR volume por acidente.
Erro, indisponibilidade da Meta e qualidade ruim têm que puxar pra baixo.
"""
from __future__ import annotations

import pytest

from app.services import rampa


@pytest.fixture(autouse=True)
def _sem_cache():
    rampa._cache.clear()
    yield
    rampa._cache.clear()


class SessaoFake:
    """Devolve N dias ativos."""

    def __init__(self, dias):
        self.dias = dias

    def execute(self, *a, **k):
        class R:
            def __init__(self, v): self.v = v
            def scalar(self): return self.v
        return R(self.dias)


def _com_qualidade(monkeypatch, q):
    monkeypatch.setattr(rampa, "_qualidade_meta", lambda slug: q)


# ── 1. A subida ──────────────────────────────────────────────────────────────

# 80/dia desde 15/09/2026 (número novo, sem reputação). Estes testes ficaram
# sete semanas esperando 300 enquanto o código dizia 80 — suíte vermelha que
# ninguém olhava. Agora dizem o que o código faz, e o que precisa pra subir.
@pytest.mark.parametrize("dias", [0, 1, 10, 30])
def test_sem_pedido_o_teto_e_o_primeiro_degrau(monkeypatch, dias):
    """Linha GREEN, aquecida ou não: sem pedido no painel, 80. É isto que faz
    publicar o segundo degrau não mudar um disparo sequer."""
    _com_qualidade(monkeypatch, "GREEN")
    r = rampa.cap_de_hoje(SessaoFake(dias), "corretor", 4)
    assert r.cap == 80


def test_o_degrau_maximo_chega_a_300():
    assert rampa.DEGRAU_BASE == 80
    assert rampa.DEGRAU_MAXIMO == 300
    assert max(c for _, c in rampa.DEGRAUS) == 300
    assert rampa.DEGRAUS[0][1] == 80


@pytest.mark.parametrize("pedido,esperado", [(120, 120), (300, 300), (999, 300)])
def test_com_pedido_e_GREEN_sobe_ate_o_pedido(monkeypatch, pedido, esperado):
    """A capacidade de 300: pedido no painel + GREEN confirmado."""
    _com_qualidade(monkeypatch, "GREEN")
    r = rampa.cap_de_hoje(SessaoFake(30), "corretor", 4, pedido=pedido)
    assert r.cap == esperado
    assert r.degrau == 1


@pytest.mark.parametrize("q", ["UNKNOWN", "QUALQUER_COISA_NOVA"])
def test_pedido_sem_GREEN_NAO_sobe_e_diz_por_que(monkeypatch, q):
    """Número novo (UNKNOWN) ou valor que a Meta inventar: fica em 80, e o
    motivo cita o pedido — é o texto que a tela mostra pra ninguém digitar 300
    e achar que valeu. (Meta fora, None, é outro caso: não envia.)"""
    _com_qualidade(monkeypatch, q)
    r = rampa.cap_de_hoje(SessaoFake(30), "corretor", 4, pedido=300)
    assert r.cap == 80
    assert "300" in r.motivo and "GREEN" in r.motivo


def test_pedido_baixo_nao_mexe_na_rampa(monkeypatch):
    """Pedido de 40 não é assunto da rampa: ela devolve 80 e quem chama fica
    com o menor."""
    _com_qualidade(monkeypatch, "GREEN")
    r = rampa.cap_de_hoje(SessaoFake(30), "corretor", 4, pedido=40)
    assert r.cap == 80 and r.degrau == 0


def test_freios_continuam_valendo():
    """Os FREIOS são a proteção que sobra quando o teto sobe. Se alguém mexer
    neles, a linha da casa fica sem rede — YELLOW tem que recuar e RED parar."""
    assert rampa._CONGELA == {"YELLOW"}
    assert {"RED", "FLAGGED"} <= rampa._PARA


@pytest.mark.parametrize("q,esperado", [
    ("YELLOW", 50), ("RED", 0), ("FLAGGED", 0), ("RESTRICTED", 0),
])
def test_pedido_de_300_nao_fura_freio_de_qualidade(monkeypatch, q, esperado):
    """O teste que importa do teto novo: 300 pedido com a linha amarela ou
    vermelha continua 50 ou zero."""
    _com_qualidade(monkeypatch, q)
    assert rampa.cap_de_hoje(SessaoFake(30), "corretor", 4, pedido=300).cap == esperado


# ── 2. As travas — o que importa de verdade ─────────────────────────────────

@pytest.mark.parametrize("q", ["RED", "FLAGGED", "RESTRICTED"])
def test_qualidade_ruim_para_a_linha(monkeypatch, q):
    """Não é degrau — é freio. Zero até alguém olhar."""
    _com_qualidade(monkeypatch, q)
    r = rampa.cap_de_hoje(SessaoFake(30), "corretor", 4)
    assert r.cap == 0, f"qualidade {q} deveria PARAR a linha, veio cap={r.cap}"


def test_amarelo_congela_um_degrau_abaixo(monkeypatch):
    """YELLOW não sobe e ainda recua — dar volume com qualidade caindo é como
    se perde o número."""
    _com_qualidade(monkeypatch, "GREEN")
    verde = rampa.cap_de_hoje(SessaoFake(6), "corretor", 4).cap
    rampa._cache.clear()
    _com_qualidade(monkeypatch, "YELLOW")
    amarelo = rampa.cap_de_hoje(SessaoFake(6), "corretor", 4).cap
    assert amarelo < verde, f"amarelo ({amarelo}) devia ser menor que verde ({verde})"
    # O recuo é pro piso de segurança — sem isso o freio seria enfeite e a
    # linha seguiria no teto com a qualidade caindo.
    assert amarelo == rampa.CAP_AMARELO_MINIMO


@pytest.mark.parametrize("pedido", [None, 40, 300])
def test_sem_leitura_de_qualidade_NAO_envia(monkeypatch, pedido):
    """FALHA FECHADA (05/10/2026): Meta fora do ar / token ruim → teto ZERO.

    Antes valia o primeiro degrau (80). O furo: linha em RED, a Graph fora por
    um minuto, e o teto saltava de 0 pra 80 — o freio soltava justamente quando
    não dava pra ver a qualidade."""
    _com_qualidade(monkeypatch, None)
    r = rampa.cap_de_hoje(SessaoFake(30), "corretor", 4, pedido=pedido)   # já aquecida
    assert r.cap == 0
    assert "não consegui ler" in r.motivo


def test_RED_e_depois_sem_leitura_continua_zero(monkeypatch):
    """A sequência que reabria volume: RED numa volta, leitura falha na outra."""
    _com_qualidade(monkeypatch, "RED")
    assert rampa.cap_de_hoje(SessaoFake(30), "corretor", 4).cap == 0
    _com_qualidade(monkeypatch, None)
    assert rampa.cap_de_hoje(SessaoFake(30), "corretor", 4).cap == 0


def test_UNKNOWN_da_meta_e_leitura_boa_e_segue_no_primeiro_degrau(monkeypatch):
    """`UNKNOWN` é o que a Meta responde pra número novo. Travar nele
    impediria o número de sair pra sempre (a reputação só nasce enviando)."""
    _com_qualidade(monkeypatch, "UNKNOWN")
    assert rampa.cap_de_hoje(SessaoFake(30), "corretor", 4).cap == rampa.DEGRAU_BASE


def test_erro_ao_contar_dias_nao_libera(monkeypatch):
    """Banco ilegível → 0 dias → degrau mínimo. Nunca o contrário."""
    _com_qualidade(monkeypatch, "GREEN")

    class SessaoQuebrada:
        def execute(self, *a, **k):
            raise RuntimeError("banco fora")

    r = rampa.cap_de_hoje(SessaoQuebrada(), "corretor", 4)
    assert r.cap == rampa.DEGRAUS[0][1]


def test_qualidade_da_meta_nunca_levanta(monkeypatch):
    """Falha de rede na consulta vira None (que já cai no mínimo), não exceção."""
    import app.services.cloud_lines as cl

    monkeypatch.setattr(cl, "line_by_slug", lambda s: (_ for _ in ()).throw(RuntimeError("x")))
    # (o conftest troca `_qualidade_meta` por padrão; a de verdade fica em `.real`)
    real = getattr(rampa._qualidade_meta, "real", rampa._qualidade_meta)
    assert real("corretor") is None


# ── 3. Integração com o cap da cadência ─────────────────────────────────────

def test_config_liga_a_rampa_so_na_corretor():
    """Pessoa12/Pessoa10/Pessoa11 não estão em rampa — o cap delas não muda."""
    from app.config import Settings

    s = Settings()
    assert s.rampa_slugs.get("4") == "corretor"
    for outra in ("1", "2", "3"):
        assert outra not in s.rampa_slugs


def test_rampa_so_reduz_nunca_aumenta(monkeypatch):
    """A rampa é teto, não piso: se o cap configurado for MENOR, vale o menor."""
    _com_qualidade(monkeypatch, "GREEN")
    r = rampa.cap_de_hoje(SessaoFake(30), "corretor", 4)
    assert r.cap == 80
    # no cadencia.py o uso é `if r.cap < cap: cap = r.cap` — nunca sobe.
