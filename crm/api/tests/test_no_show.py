"""No-show — as travas, em ordem de dano se quebrarem.

A pior de todas está no primeiro teste: a automação NÃO pode disparar em visita
vencida sem registro. "Vencida e ainda agendada" quer dizer que ninguém fechou
o card — não que a pessoa faltou. Mandar "você não veio ontem" pra quem
compareceu queima o lead e a corretora junto.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.services import no_show as ns

SP = ZoneInfo("America/Sao_Paulo")


def test_so_dispara_em_falta_marcada_explicitamente():
    """A trava que corrige o desenho original. Se alguém trocar o filtro de
    status por 'agendada', este teste cai."""
    assert "v.status = 'no_show'" in ns._SQL_FALTAS
    assert "agendada" not in ns._SQL_FALTAS
    assert "pendente_confirmacao" not in ns._SQL_FALTAS


def test_cota_de_tres_do_acervo():
    """Depois do 3º, para. Só existem 2 copies: o 3º não recebe nada — vira
    conversa de gente, não mais automação."""
    assert ns.COTA_MAX == 3
    assert len(ns.COPIES) == 2


def test_tom_sobe_uma_vez_e_para():
    """A 2ª é mais direta que a 1ª, mas nenhuma culpa a pessoa."""
    primeira, segunda = ns.COPIES
    assert "aconteceu alguma coisa" in primeira.texto.lower()
    assert "ainda faz sentido" in segunda.texto.lower()
    for c in ns.COPIES:
        baixo = c.texto.lower()
        for palavra in ("você faltou", "não apareceu", "descaso", "furou"):
            assert palavra not in baixo, f"tom de bronca: {palavra}"


def test_janela_de_disparo_e_do_dia_seguinte():
    """Piso de 14h evita mandar de madrugada pra visita do fim da tarde; teto de
    38h evita cobrar falta de três dias como se fosse ontem."""
    assert ns.MIN_H >= 12.0
    assert ns.MAX_H <= 48.0
    assert ns.MIN_H < ns.MAX_H


@pytest.mark.parametrize(
    "quando,esperado",
    [
        (datetime(2026, 8, 5, 10, 0, tzinfo=SP), True),
        (datetime(2026, 8, 5, 22, 0, tzinfo=SP), False),   # cobrança às 22h é briga
        (datetime(2026, 8, 5, 6, 0, tzinfo=SP), False),
        (datetime(2026, 8, 9, 11, 0, tzinfo=SP), False),   # domingo
    ],
)
def test_horario_civilizado(quando, esperado):
    assert ns.dentro_do_horario(quando) is esperado


def test_quem_remarcou_nao_e_cobrado():
    """Se a pessoa já reagendou depois da falta, não há o que cobrar."""
    assert "_SQL_REMARCOU" in dir(ns)
    assert "data_hora > :depois" in ns._SQL_REMARCOU


def test_opt_out_bloqueia():
    assert "opt_out" in ns._SQL_BLOQUEADO
    assert "ja_encontrou" in ns._SQL_BLOQUEADO


def test_cota_conta_por_telefone_e_nao_por_card():
    """O mesmo comprador pode ter mais de um card no CRM. A cota é da pessoa."""
    assert "telefone_lead" in ns._SQL_FALTAS
    assert "regexp_replace" in ns._SQL_FALTAS


def test_dedup_e_por_visita():
    """Uma cobrança por falta, pra sempre. Cobrar duas vezes vira bloqueio."""
    assert "visita_id = :vid" in ns._SQL_JA_MANDOU
    assert "uq_no_show_visita" in ns._SQL_GRAVA


# ── o tick ───────────────────────────────────────────────────────────────────


def test_desligado_nao_faz_nada(monkeypatch):
    monkeypatch.setattr(ns.cfg, "no_show_ativo", lambda: False)
    r = ns.rodar()
    assert r["enviados"] == 0 and "desligado" in r["pulados"]


def test_fora_do_horario_nao_varre(monkeypatch):
    monkeypatch.setattr(ns.cfg, "no_show_ativo", lambda: True)
    monkeypatch.setattr(ns, "dentro_do_horario", lambda *a: False)

    def _boom(**kw):
        raise AssertionError("varreu fora do horário")

    monkeypatch.setattr(ns, "selecionar", _boom)
    r = ns.rodar()
    assert "fora_do_horario" in r["pulados"]


def test_primeiro_nome():
    assert ns._primeiro_nome("Pessoa Aparecida Souza") == "Pessoa"
    assert ns._primeiro_nome(None) == "tudo bem"   # nunca manda "Olá , tudo bem?"
    assert ns._primeiro_nome("   ") == "tudo bem"
