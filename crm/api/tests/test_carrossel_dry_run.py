"""DRY-RUN não é envio — o lead não pode virar "Disparado" sem ter recebido.

Medido em 15/09/2026 no banco de produção: 3 leads (6272, 1271, 1272) foram
pra Disparado às 14:35 com `lead_events.payload->>'template' = null` e a
mensagem correspondente gravada com `evolution_message_id` começando em
`dry-` — ou seja, um tick rodou com DRY_RUN=true contra o banco de produção.
Ninguém recebeu nada, mas os três ficariam DESCANSO_DIAS (30) fora da fila,
e o placar de copy contaria os três como envio.

O contrato do sender é explícito (`cloud_corretora_a.enviar_template_linha`):
saiu → {"sent": True, "dry_run": False}; dry-run → {"sent": False,
"dry_run": True}. O carrossel passou a ler esse contrato.
"""
from __future__ import annotations

import pytest

from app.services import carrossel_corretor as cb


class _LinhaFake:
    slug = cb.SLUG_CORRETOR


class _SessionSpy:
    def __init__(self) -> None:
        self.execucoes = 0

    def execute(self, *a, **k):
        self.execucoes += 1
        return self


def _preparar(monkeypatch, retorno):
    """Deixa `_disparar` chegar até o sender, sem banco nem rede."""
    from app.services import cloud_lines as cl

    monkeypatch.setattr(cl, "line_by_slug", lambda s: _LinhaFake())
    monkeypatch.setattr(
        "app.services.cadencia._chamar_sender", lambda *a, **k: retorno
    )


def test_dry_run_nao_grava_evento_de_disparo(monkeypatch):
    """Nada saiu → `_NadaSaiu` → o tick devolve o lead pra Fila."""
    _preparar(monkeypatch, {"ok": True, "dry_run": True, "sent": False,
                            "wamid": "dry-x", "template": None})
    ses = _SessionSpy()
    with pytest.raises(cb._NadaSaiu):
        cb._disparar(ses, {"id": 1}, "+5500000000000", object(), 1, linha=_LinhaFake())
    assert ses.execucoes == 0, "gravou lead_events de um envio que não aconteceu"


def test_sent_false_tambem_e_nada_saiu(monkeypatch):
    """Sender que declara `sent: False` sem flag de dry-run também não enviou."""
    _preparar(monkeypatch, {"ok": True, "sent": False})
    with pytest.raises(cb._NadaSaiu):
        cb._disparar(_SessionSpy(), {"id": 1}, "+5500000000000", object(), 1,
                     linha=_LinhaFake())


def test_envio_real_continua_gravando(monkeypatch):
    """A trava não pode calar o caminho bom: sent=True grava o evento."""
    _preparar(monkeypatch, {"ok": True, "dry_run": False, "sent": True,
                            "template": "acme_corretor_ola_a_01",
                            "categoria": "UTILITY"})
    ses = _SessionSpy()
    cb._disparar(ses, {"id": 1}, "+5500000000000", object(), 1, linha=_LinhaFake())
    # duas escritas desde 05/10: sai a reserva do claim, entra o disparo
    assert ses.execucoes == 2


@pytest.mark.parametrize("retorno", [{"ok": True}, {}, {"ok": False}, None,
                                     {"sent": "true"}, {"sent": 1}])
def test_retorno_fora_do_contrato_NAO_vira_disparo(monkeypatch, retorno):
    """Só `sent is True` é envio. Até 05/10/2026 `{}`, `None` e `{"ok": false}`
    passavam e viravam `carrossel_disparo` com wamid nulo — lead em Disparado,
    contador subindo, mensagem que talvez nunca tenha existido. Agora é
    `_Incerto`: nada é gravado como disparo e a reserva segura o teto."""
    _preparar(monkeypatch, retorno)
    ses = _SessionSpy()
    with pytest.raises(cb._Incerto):
        cb._disparar(ses, {"id": 1}, "+5500000000000", object(), 1, linha=_LinhaFake())
    assert ses.execucoes == 0
