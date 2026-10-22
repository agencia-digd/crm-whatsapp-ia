"""Recado avulso: o que sai quando a corretora toca em "Saiba mais".

A regra que importa: recado pendente GANHA do briefing. Se a Dona mandou um
recado do dia, é ele que a corretora lê — o briefing ela recebe no ritual.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from app.services import recado_corretora


@pytest.fixture()
def _limpa():
    """Cada teste começa sem recado pendente — o índice parcial é por
    corretora, então sobra de um teste quebraria o seguinte."""
    from sqlalchemy import text as _t

    from app.db.session import SessionLocal

    with SessionLocal() as s:
        s.execute(_t("DELETE FROM recado_corretora"))
        s.commit()
    yield


def test_criar_e_ler_pendente(_limpa):
    recado_corretora.criar(corretora_id=1, texto="quatro pessoas sem resposta",
                           autor="assistente:teste")
    r = recado_corretora.pendente(1)
    assert r is not None
    assert "quatro pessoas" in r["texto"]
    assert r["criado_por"] == "assistente:teste"


def test_recado_novo_cancela_o_anterior(_limpa):
    """Dois recados no mesmo dia: vale o último. Recado velho entregue depois
    faz a corretora agir sobre um dia que já passou."""
    recado_corretora.criar(corretora_id=1, texto="primeiro", autor="assistente")
    recado_corretora.criar(corretora_id=1, texto="segundo", autor="assistente")
    r = recado_corretora.pendente(1)
    assert r["texto"] == "segundo"


def test_entregar_marca_enviado_e_nao_repete(_limpa):
    recado_corretora.criar(corretora_id=1, texto="oi", autor="assistente")
    with patch("app.api.cloud_assistente.enviar_cloud_assistente",
               return_value={"ok": True, "sent": True}) as env:
        res = recado_corretora.entregar(1, "+5500000000000")
    assert res["ok"] and env.call_count == 1
    assert recado_corretora.pendente(1) is None      # consumido
    with patch("app.api.cloud_assistente.enviar_cloud_assistente") as env2:
        assert recado_corretora.entregar(1, "+5500000000000") is None
        env2.assert_not_called()                      # 2º clique não repete


def test_falha_de_envio_nao_come_o_recado(_limpa):
    """Se a Graph API falhar, o recado CONTINUA pendente — senão a corretora
    toca o botão, não recebe nada, e o aviso foi gasto à toa."""
    recado_corretora.criar(corretora_id=1, texto="importante", autor="assistente")
    with patch("app.api.cloud_assistente.enviar_cloud_assistente",
               return_value={"ok": False, "error": "timeout"}):
        recado_corretora.entregar(1, "+5500000000000")
    assert recado_corretora.pendente(1) is not None


def test_sem_recado_devolve_none(_limpa):
    assert recado_corretora.entregar(2, "+5500000000000") is None


def test_recado_vazio_recusado(_limpa):
    with pytest.raises(ValueError):
        recado_corretora.criar(corretora_id=1, texto="   ", autor="assistente")


def test_botao_prioriza_recado_sobre_briefing(_limpa):
    """O gancho do botão: com recado, o briefing NÃO é gerado."""
    from app.api.cloud_assistente import _processar_briefing_botao

    recado_corretora.criar(corretora_id=1, texto="recado do dia", autor="assistente")
    with patch("app.api.cloud_assistente.enviar_cloud_assistente",
               return_value={"ok": True, "sent": True}), \
         patch("app.services.briefing_push.entregar_briefing_completo") as brief:
        _processar_briefing_botao(1, "+5500000000000")
    brief.assert_not_called()


def test_botao_cai_no_briefing_quando_nao_ha_recado(_limpa):
    from app.api.cloud_assistente import _processar_briefing_botao

    with patch("app.services.briefing_push.entregar_briefing_completo") as brief:
        _processar_briefing_botao(2, "+5500000000000")
    brief.assert_called_once()
