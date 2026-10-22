"""Detector de auto-resposta de WhatsApp Business.

Quando a Corretor dispara o HSM, alguns leads têm autoreply ligado ("retornamos
em breve", "horário comercial", etc) — essas mensagens chegam no webhook como
se fossem resposta do lead e queimam a conversa. Filtramos antes de gravar.

Heurística:
- Janela curta (< 60s do HSM): qualquer 1 padrão bate → autoreply
- Janela larga: precisa de 2+ padrões pra reduzir falso positivo

Padrões aprendidos de exemplos reais (2026-05-21, ondas Acme).
"""
from __future__ import annotations

import re

_PATTERNS = [
    r"assim que poss[íi]vel",
    r"em breve",
    r"retornaremos|retornamos",
    r"hor[áa]rio (comercial|de atendimento|de funcionamento)",
    r"fora do (nosso )?hor[áa]rio",
    r"agradec[eo](mos|me)? (seu|o seu) contato",
    r"como pod(emos|e) (te )?ajudar",
    r"obrigad[oa] (pelo|por) contato",
    r"mensagens? (ser[ãa]o|s[ãa]o) respondidas?",
    r"para d[úu]vidas? ou or[çc]amento",
    r"informe qual (seria )?o procedimento",
    r"foto de refer[êe]ncia",
    r"encaminhar para o or[çc]amento",
    r"resposta autom[áa]tica|mensagem autom[áa]tica",
    r"^\s*ol[áa][,.! ]+(tudo bem|como (vai|posso))",
]

_COMPILED = [re.compile(p, re.IGNORECASE) for p in _PATTERNS]

QUICK_WINDOW_SECONDS = 60


def is_autoreply(text: str, seconds_since_hsm: float | None) -> tuple[bool, list[str]]:
    """Retorna (é_autoreply, padrões_que_bateram)."""
    if not text:
        return False, []
    hits = [p.pattern for p in _COMPILED if p.search(text)]
    if not hits:
        return False, []
    if seconds_since_hsm is not None and seconds_since_hsm < QUICK_WINDOW_SECONDS:
        return True, hits
    return (len(hits) >= 2), hits
