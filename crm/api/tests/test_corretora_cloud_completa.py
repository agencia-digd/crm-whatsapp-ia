"""Linha em coexistência tem que estar cadastrada em TODOS os mapas.

Este teste existe porque o MESMO buraco já aconteceu três vezes:

  • Corretor (30/07): env setada, slug fora da tupla de roteamento — inbound no vazio;
  • Pessoa7 (09/09): idêntico. O comentário em webhooks.py registra: "Passamos a
    tarde procurando na Meta (WABA inscrita, linha CONNECTED, qualidade GREEN —
    tudo certo lá) porque o buraco estava aqui";
  • Corretor (22/09): no banco e no `cloud_lines`, mas sem destino de inbound no
    copiloto e sem rótulo na tela. Mandou 4 mensagens, não recebeu nenhuma, e
    nem o status de entrega voltou.

O sintoma é sempre SILENCIOSO: a linha ENVIA normal e nada do que ela recebe
aparece — sem erro em lugar nenhum. Por isso a checagem é estrutural e não
depende do banco: roda sempre, em qualquer máquina.

Fonte da verdade aqui é `cloud_lines._CORRETORAS_ENV` — a lista de quem é linha
de coexistência. A Pessoa12 fica fora porque tem módulo próprio (`cloud_corretora_a`,
endpoints /cloud/* legados), não o genérico /cloud/corretora/<slug>/*.
"""
from __future__ import annotations

from app.api.cloud_corretora import _DESTINO
from app.api.painel import CID_TO_FRONT
from app.services import cloud_lines

# `corretor` é a linha DA CASA (remarketing em massa), não uma corretora do painel:
# ela não tem card com nome na tela, então fica fora do teste de rótulo.
_LINHA_DA_CASA = ("corretor",)


def _slugs() -> list[str]:
    return sorted(cloud_lines._CORRETORAS_ENV)


def test_todo_slug_tem_instancia():
    """Sem instância, `linha_da_corretora` devolve None e o envio do painel cai
    no Evolution — desconectado em coexistência. A mensagem some."""
    faltando = [s for s in _slugs() if not cloud_lines.instance_do_slug(s)]
    assert not faltando, (
        f"slug sem instância em cloud_lines._INSTANCE_POR_SLUG: {faltando}"
    )


def test_todo_slug_tem_destino_de_inbound():
    """Sem destino, o webhook responde 404 `linha_desconhecida` e a mensagem do
    cliente é descartada (caso Corretor, 22/09)."""
    faltando = [s for s in _slugs() if s not in _DESTINO]
    assert not faltando, (
        f"slug sem rota em app/api/cloud_corretora.py::_DESTINO: {faltando}"
    )


def test_destino_e_instancia_concordam():
    """Os dois mapas precisam apontar pra MESMA instância: divergir grava a
    resposta num thread e o envio em outro (é o que o comentário do
    `_INSTANCE_POR_SLUG` avisa)."""
    erradas = [
        f"{s}: _DESTINO={_DESTINO[s][0]!r} vs cloud_lines={cloud_lines.instance_do_slug(s)!r}"
        for s in _slugs()
        if s in _DESTINO and _DESTINO[s][0] != cloud_lines.instance_do_slug(s)
    ]
    assert not erradas, erradas


def test_corretora_tem_rotulo_na_tela():
    """Sem rótulo, o card dela aparece sem nome no chat."""
    faltando = [
        f"{s} (corretora_id {_DESTINO[s][1]})"
        for s in _slugs()
        if s in _DESTINO and s not in _LINHA_DA_CASA
        and _DESTINO[s][1] not in CID_TO_FRONT
    ]
    assert not faltando, f"corretora sem nome em painel.CID_TO_FRONT: {faltando}"


def test_a_corretor_esta_completa():
    """Caso concreto que originou o teste — trava a regressão pelo nome."""
    assert cloud_lines.instance_do_slug("corretor") == "acme_corretor"
    assert _DESTINO["corretor"] == ("acme_corretor", 8)
    assert CID_TO_FRONT[8] == "Corretor"
