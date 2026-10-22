"""Testes do módulo de persona Playbook Pessoa1 (6 movimentos de fechamento)."""

from app.persona.playbook_corretora_b import (
    CATEGORIAS_MOVIMENTO,
    MOVIMENTOS,
    PLAYBOOK_CORRETORA_B,
    aplicar_playbook,
)


def test_tem_os_6_movimentos_do_playbook() -> None:
    ids = {m["id"] for m in MOVIMENTOS}
    # 6 movimentos + horario_concreto (desdobramento do #1 pra coaching fino)
    assert {
        "propor_visita", "horario_concreto", "tira_friccao", "traz_decisor",
        "qualifica_dinheiro", "escassez", "followup_humano",
    } <= ids


def test_cada_movimento_tem_categoria_gap_e_sugestao() -> None:
    for m in MOVIMENTOS:
        assert m["categoria"].startswith("movimento_")
        assert m["quando"].strip()
        assert m["gap"].strip()
        assert m["sugestao_modelo"].strip()


def test_categorias_sao_unicas() -> None:
    cats = [m["categoria"] for m in MOVIMENTOS]
    assert len(cats) == len(set(cats))
    assert set(cats) == set(CATEGORIAS_MOVIMENTO)


def test_playbook_render_contem_ancoras() -> None:
    texto = PLAYBOOK_CORRETORA_B.lower()
    assert "atendido" in texto and "visita" in texto
    assert "amarr" in texto  # amarrar a agenda
    assert "termina em pergunta" in texto
    # prioridade explícita pro movimento nº1
    assert "movimento_sem_convite_visita" in PLAYBOOK_CORRETORA_B


def test_aplicar_playbook_retorna_string_nao_vazia() -> None:
    out = aplicar_playbook()
    assert isinstance(out, str)
    assert out.strip()
    assert out == PLAYBOOK_CORRETORA_B
