"""Tabela de preços por modelo (US$ por 1 milhão de tokens) — alimenta `llm_chamadas.custo_usd`.

É o preço de TABELA da API, em dólar. Chamada feita pelo `claude -p` da assinatura
não cobra por token: lá o número é o custo EQUIVALENTE na API (serve pra comparar
serviços e modelos, não pra conciliar fatura).

Modelo que não está aqui (gpt-5.6-terra, qwen, gemini...) devolve None e a coluna
fica NULL: "sem registro" é melhor que um custo inventado. Preço novo ou reajuste é
uma linha nova; o casamento é por prefixo do nome do modelo (vence o mais longo).
"""
from __future__ import annotations

# (prefixo do modelo, US$/1M tokens de entrada, US$/1M tokens de saída)
PRECOS_POR_MILHAO: tuple[tuple[str, float, float], ...] = (
    ("claude-haiku-4-5", 1.0, 5.0),
    ("claude-sonnet-4", 3.0, 15.0),
    # Tabela da Anthropic conferida em 22/10/2026 (platform.claude.com/docs/en/about-claude/pricing).
    # Haiku 5.5 acima de 100 mil tokens de prompt custa US$ 0,50 / 2,50; aqui vale a faixa até 100 mil
    # (as conversas lidas ficam muito abaixo disso).
    ("claude-haiku-5-5", 0.10, 0.50),
    ("claude-sonnet-5-5", 2.0, 10.0),
    ("claude-opus-5-5", 4.0, 20.0),
    ("claude-fable-5-1", 10.0, 50.0),
)


def custo_usd(modelo: str | None, input_tokens: int, output_tokens: int) -> float | None:
    """Custo da chamada em US$, ou None se o modelo não tem preço cadastrado."""
    nome = (modelo or "").strip().lower()
    achados = [p for p in PRECOS_POR_MILHAO if nome.startswith(p[0])]
    if not achados:
        return None
    _, p_in, p_out = max(achados, key=lambda p: len(p[0]))
    return round((input_tokens * p_in + output_tokens * p_out) / 1_000_000, 6)
