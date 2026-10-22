"""Testes do módulo de persona compartilhada DNA da Pessoa11."""

from app.persona.dna_corretor import DNA_CORRETOR, PLAYBOOK_RESGATE, aplicar_dna


def test_dna_contem_ancoras_chave() -> None:
    """DNA_CORRETOR deve carregar os princípios-âncora do jeito Pessoa11."""
    texto = DNA_CORRETOR.lower()
    assert "termina em pergunta" in texto
    assert "não quero incomodar" in texto
    assert "subsídio" in texto or "presente" in texto


def test_playbook_tem_os_5_passos() -> None:
    """PLAYBOOK_RESGATE deve conter os 5 passos da conversa de resgate."""
    texto = PLAYBOOK_RESGATE.lower()
    assert "abertura" in texto
    assert "conexão" in texto or "escuta" in texto
    assert "âncora" in texto
    assert "simulação" in texto
    assert "visita" in texto


def test_aplicar_dna_retorna_string_nao_vazia() -> None:
    """aplicar_dna() sempre retorna string útil pra injetar num prompt."""
    resultado = aplicar_dna()
    assert isinstance(resultado, str)
    assert resultado.strip()


def test_aplicar_dna_modo_resgate_inclui_playbook() -> None:
    """No modo resgate (default), o bloco carrega princípios + playbook."""
    resultado = aplicar_dna("resgate")
    assert "termina em pergunta" in resultado.lower()
    assert PLAYBOOK_RESGATE in resultado


def test_aplicar_dna_outro_modo_so_principios() -> None:
    """Fora do modo resgate, retorna só o DNA, sem o playbook de 5 passos."""
    resultado = aplicar_dna("tom")
    assert resultado == DNA_CORRETOR
    assert PLAYBOOK_RESGATE not in resultado
