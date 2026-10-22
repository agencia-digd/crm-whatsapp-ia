"""A fila de ligações do painel tem que seguir a RÉGUA — não uma cópia dela.

Bug de 10/08/2026 (relato da Pessoa11): "clico no botão e dá 'não consegui
registrar'". Causa: `_TOQUE_LIGACAO_LABEL` no painel era um hardcode da régua
de 17/07 (ligação nos índices 1, 4, 6). Em 04-05/08 a régua V2 inseriu o D3
(oferta de áudio) e empurrou tudo — ligação virou 1, 5, 7. Resultado:

  - a fila listava toques de MENSAGEM como "🔔 Ligar";
  - o clique caía em `marcar_ligacao_feita` → `toque_atual_nao_e_ligacao`;
  - e as ligações de verdade (toques 5 e 7) sumiam da fila da corretora.

Este teste é a trava: qualquer mudança futura na régua quebra aqui, não na mão
da corretora.
"""
from __future__ import annotations

from app.api.painel import _INDICES_LIGACAO, _TOQUE_LIGACAO_LABEL
from app.services.cadencia import TIPO_TAREFA_LIGACAO, TOQUES


def test_indices_de_ligacao_batem_com_a_regua() -> None:
    da_regua = {t.indice for t in TOQUES if t.tipo == TIPO_TAREFA_LIGACAO}
    assert set(_INDICES_LIGACAO) == da_regua


def test_rotulo_do_toque_usa_o_dia_da_regua() -> None:
    """O rótulo "D5 (dia 5)" tem que vir do offset real do toque."""
    por_indice = {t.indice: t for t in TOQUES}
    for indice, (rotulo, dia) in _TOQUE_LIGACAO_LABEL.items():
        toque = por_indice[indice]
        assert toque.tipo == TIPO_TAREFA_LIGACAO
        assert dia == toque.offset_dias
        assert rotulo == f"D{toque.offset_dias}"
