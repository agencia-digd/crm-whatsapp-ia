"""Lead diz "já encontrei": despedida com indicação, e encerra.

Decisão da Dona (05/08/2026), a partir das respostas reais de 30 dias: "tava
procurando, mas já desisti" e "no momento já resolvemos" NÃO são opt-out — a
pessoa não está brava, terminou a busca. Só que continuava na régua recebendo
toque, que é o caminho mais curto pra virar denúncia de spam.

O risco desta feature é despachar quem AINDA está procurando. "Ainda não
encontrei" e "já encontrei" diferem por uma palavra e significam o oposto — e o
custo do erro é perder um lead vivo com uma mensagem de despedida.
"""
from __future__ import annotations

import pytest

from app.services import cadencia as c


@pytest.mark.parametrize("texto", [
    "Tava procurando, mas já desisti",
    "No momento ja resolvemos. Obrigado",
    "já encontrei",
    "Já achei um, obrigada",
    "já comprei outro",
    "não quero mais",
    "desisti de comprar",
])
def test_reconhece_quem_terminou_a_busca(texto):
    assert c._ja_encontrou(texto) is True


@pytest.mark.parametrize("texto", [
    "ainda não encontrei",          # CONTINUA procurando — o oposto
    "não encontrei nada ainda",
    "Boa tarde, ainda não encontrei",
    "quero ver mais opções",
    "pode enviar",
    "Bom dia",
    "tem algo de 2 quartos?",
    "", None,
])
def test_nao_despacha_quem_ainda_procura(texto):
    """O erro caro: mandar despedida pra quem está procurando."""
    assert c._ja_encontrou(texto) is False


def test_despedida_tem_as_tres_partes():
    """A Dona desenhou: fecha bem, oferece a condição do mês, e apresenta a
    indicação com o valor e QUANDO paga."""
    t = c.DESPEDIDA_JA_ENCONTROU
    assert "que bom que você já encontrou" in t
    assert "condição exclusiva" in t
    assert "R$ 500" in t
    assert "concluída" in t, "tem que dizer quando o prêmio é pago"
    assert t.rstrip().endswith("?"), "termina com pergunta — é o que puxa a indicação"


def test_encerra_sem_passar_pelo_atendimento():
    """Quem tem conclusão não é assunto de corretora. Se voltar a falar, o
    próximo inbound cai no fluxo normal e aí sim vira atendimento."""
    import inspect

    fonte = inspect.getsource(c.processar_resposta) if hasattr(c, "processar_resposta") else ""
    if not fonte:
        import app.services.cadencia as mod
        fonte = inspect.getsource(mod)
    assert 'return "ja_encontrou"' in fonte


def test_status_e_etapa_de_destino():
    assert c.STATUS_JA_ENCONTROU == "ja_encontrou"
    assert c.STAGE_ID_VENDA_PERDIDA == 13


def test_opt_out_tem_precedencia():
    """"não quero mais" cai NAS DUAS regras, e opt-out ganha.

    A diferença importa: opt-out é pedido explícito de parar, com peso legal e
    trava de reenvio. Mandar despedida com oferta e programa de indicação pra
    quem pediu pra sair é exatamente o oposto do que a pessoa pediu — e é o
    tipo de coisa que vira denúncia."""
    from app.services.opt_out import is_opt_out

    assert is_opt_out("não quero mais") and c._ja_encontrou("não quero mais")

    import inspect
    fonte = inspect.getsource(c._despedir_quem_ja_encontrou)
    assert "is_opt_out(texto) or not _ja_encontrou" in fonte, (
        "a despedida tem que sair fora quando é opt-out"
    )
