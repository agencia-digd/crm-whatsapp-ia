"""A regra do próximo passo no prompt do Aprendiz.

Medido em 05/08 sobre 182 rascunhos reais: 63% não pediam nada e 21% se
DESPEDIAM de conversa ainda viva. "Fico à disposição" é o jeito mais educado de
fazer um lead sumir — e conversa que morre ali não volta.

Estes testes travam a regra no prompt. Não medem a saída do modelo (isso é o
placar de forma, em test_comparador_sombra), medem que a instrução não sumiu
numa edição futura.
"""
from __future__ import annotations

from app.inspector.shadow import _SYSTEM


def test_prompt_exige_um_proximo_passo():
    assert "PRÓXIMO PASSO" in _SYSTEM.upper()
    assert "termina com UM próximo passo" in _SYSTEM


def test_prompt_proibe_despedida_em_conversa_viva():
    baixo = _SYSTEM.lower()
    assert "nunca termine com despedida" in baixo
    for frase in ("fico à disposição", "qualquer coisa me chama", "boa sorte na busca"):
        assert frase in baixo, f"o prompt precisa citar o vício: {frase}"


def test_prompt_exige_prazo_no_compromisso():
    """Compromisso sem prazo não é compromisso — é outra forma de não fazer nada."""
    assert "Sem prazo não é compromisso" in _SYSTEM


def test_prompt_proibe_empilhar_perguntas():
    """Quem recebe duas perguntas responde uma, ou nenhuma."""
    assert "Não empilhe duas perguntas" in _SYSTEM
