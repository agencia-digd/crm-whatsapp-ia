"""O vocativo dos templates — o bug do "Fulano" (07/08/2026).

O lead Fulano recebeu "Olá Fulano": o nome do perfil do WhatsApp do
aparelho dele. A causa não foi o fallback existir, foi a ORDEM: o resolver
consultava `leads_snapshot` (cache do Kommo, congelado no cutover de 27/07) e
caía direto no WhatsApp quando não achava — o que valia pra 82% dos leads.

Medido depois: 155 leads receberiam nome trocado, incluindo um "João da Silva"
que viraria "Fulano".
"""
from __future__ import annotations

import inspect

from app.api.cloud_corretora_a import _nome_valido, _resolver_primeiro_nome


def test_leads_vem_antes_do_snapshot_e_do_whatsapp():
    """A ordem das fontes É o conserto. Se alguém reordenar, o bug volta."""
    fonte = inspect.getsource(_resolver_primeiro_nome)
    pos_leads = fonte.index("FROM leads\n")
    pos_snap = fonte.index("FROM leads_snapshot")
    pos_wpp = fonte.index("contato_nome FROM mensagens_whatsapp")
    assert pos_leads < pos_snap < pos_wpp, "ordem das fontes invertida — bug do Fulano volta"


def test_telefone_com_dois_cadastros_nao_chuta():
    """LIMIT 2 + exigir exatamente 1: dois leads no mesmo telefone é ambiguidade,
    e chutar qual é o certo é como o "Fulano" começou."""
    fonte = inspect.getsource(_resolver_primeiro_nome)
    assert fonte.count("LIMIT 2") >= 2
    assert fonte.count("len(row) == 1") >= 2


def test_uma_letra_nao_e_vocativo():
    """"Olá F, tudo bem?" é pior que não ter nome."""
    assert _nome_valido("F") is False
    assert _nome_valido("A") is False
    assert _nome_valido("Corretor") is True


def test_emoji_no_nome_e_recusado():
    """Emoji no vocativo entrega que é robô montando o texto. Vem sempre de
    perfil de WhatsApp ("WILSONTWOLAUS 👊🏽❤️"), nunca do CRM."""
    for apelido in ("WILSONTWOLAUS 👊🏽", "Ale 🪄", "Mara 🦋", "Aparecida💗"):
        assert _nome_valido(apelido) is False, f"passou: {apelido}"


def test_nome_normal_continua_passando():
    for nome in ("Roseval", "Pessoa", "João", "Alessandra", "J. Roberto"):
        assert _nome_valido(nome) is True, f"recusou nome bom: {nome}"


def test_placeholders_recusados():
    for lixo in ("", "0", "-", "lead", "cliente", "null", "+55", "123"):
        assert _nome_valido(lixo) is False
