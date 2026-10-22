"""Vasculhada semanal do funil — as 4 camadas e o que cada uma protege.

Nasceu do pedido da Dona em 17/08, depois de uma hora de leitura de conversas
achar: uma lead fechando negócio marcada como no-show, uma falta que era
cancelamento avisado, uma visita realizada e nunca registrada, 6 negociações
não reportadas e 74% das visitas sem telefone.

"isso não pode acontecer — uma vez na semana você deveria fazer essa vasculhada."
"""
from __future__ import annotations

import inspect

from app.services import auditoria_funil as af


def test_as_quatro_camadas_na_ordem_pedida():
    """A ordem não é estética: vai do caso mais caro e raro (quem faltou) pro
    mais amplo (quem conversou). Se alguém trocar, o aviso começa pelo volume e
    o caso caro afunda no meio."""
    fonte = inspect.getsource(af.rodar)
    for i, camada in enumerate(["no_show", "compareceu", "entraram", "conversaram"]):
        assert f'"{camada}"' in fonte
    pos = [fonte.index(f'"{c}"') for c in ["no_show", "compareceu", "entraram", "conversaram"]]
    assert pos == sorted(pos), "as camadas saíram de ordem"


def test_reconhece_aviso_de_ausencia():
    """Caso Silvana Mello: avisou na véspera que ia viajar e foi marcada como
    falta. Quem avisa não faltou — é cancelamento, e a diferença muda a métrica
    de comparecimento da casa inteira."""
    assert any("amanhã não" in t for t in af._AVISOU_QUE_NAO_IA)
    assert any("viaj" in t for t in af._AVISOU_QUE_NAO_IA)


def test_reconhece_comparecimento_de_fato():
    """Caso Lucilene: faltou no dia 11, remarcou, e no dia 13 escreveu "Cheguei".
    A visita do dia 13 não existia no sistema."""
    assert "cheguei" in af._COMPARECEU_DE_FATO


def test_le_audio_transcrito():
    """Metade da conversa da Acme é áudio. Auditar só `conteudo_texto` é
    auditar meia conversa — e as corretoras mandam justamente as coisas
    importantes em áudio."""
    assert "transcricao" in inspect.getsource(af._falas)


def test_nao_muta_nada():
    """A vasculhada LÊ e aponta. Corrigir dado de cliente é decisão de gente —
    eu já mandei um lead pra Venda perdida lendo "já comprei" sem ler a frase
    seguinte, onde ele oferecia permuta."""
    fonte = inspect.getsource(af)
    for proibido in ("UPDATE ", "INSERT ", "DELETE "):
        assert proibido not in fonte.upper(), f"a auditoria não pode escrever ({proibido})"


def test_camada_que_falha_nao_derruba_as_outras():
    """Quatro camadas, quatro consultas pesadas. Se a de conversas estourar, as
    três anteriores já valem o aviso."""
    assert "auditoria_camada_falhou" in inspect.getsource(af.rodar)


def test_achado_sempre_tem_evidencia_e_acao():
    """Aviso sem a frase que prova é opinião, e ninguém age em cima. Aviso sem
    ação vira lista de problema."""
    campos = af.Achado.__dataclass_fields__
    assert "evidencia" in campos and "acao" in campos


def test_texto_vazio_desconfia_de_si_mesmo():
    """Quando não acha nada, o aviso diz pra desconfiar. Auditoria que se declara
    perfeita é a mais perigosa que existe."""
    t = af.como_texto(af.Auditoria())
    assert "eu também erro" in t.lower()
