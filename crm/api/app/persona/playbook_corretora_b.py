"""Playbook Pessoa1 — os 6 movimentos de FECHAMENTO que convertem atendimento→visita.

Enquanto ``dna_corretor`` guarda o *tom* (jeito de conversar) e ``metodo_pessoa_e`` guarda
a *régua diagnóstica* (o que uma boa conversa descobre), este módulo guarda o
**playbook de fechamento** da Pessoa1 (corretora_id=2 na Acme): os 6 movimentos
estruturados que ela usa pra transformar atendimento em visita agendada — e que
sustentam os ~39% dela contra os ~12% das outras corretoras.

Fonte curada: ``projetos/acme/copiloto-acme/PLAYBOOK-CORRETORA_B.md`` (engenharia
reversa de mensagens reais dela, 17/06/2026). Nenhum dado pessoal de lead é copiado —
só o **padrão** de fechamento.

Consumido por:
  - Copiloto / Lead Inspector (coaching) — detecta quais movimentos faltaram na
    conversa da corretora e cobra o movimento certo, priorizando o momento de
    propor visita (gargalo nº1: atendido→agendado).

Cada movimento tem um ``id`` estável que casa 1:1 com uma categoria de coaching
em ``app/inspector/coaching.py`` (prefixo ``movimento_``).
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "MOVIMENTOS",
    "PLAYBOOK_CORRETORA_B",
    "CATEGORIAS_MOVIMENTO",
    "aplicar_playbook",
]


# ── Os 6 movimentos (fonte única — id estável ↔ categoria de coaching) ────────
# id                          → categoria de coaching disparada quando o movimento
#                               é o gap mais relevante da conversa.
MOVIMENTOS: Final[tuple[dict[str, str], ...]] = (
    {
        "id": "propor_visita",
        "categoria": "movimento_sem_convite_visita",
        "titulo": "Propor a visita amarrando a AGENDA",
        "quando": (
            "lead demonstrou interesse real (perguntou de imóvel, valor, região) "
            "e a corretora NÃO convidou pra visitar OU convidou de forma vaga "
            "('quer visitar?') sem amarrar com agenda."
        ),
        "gap": (
            "faltou pedir a visita amarrando com dois horários concretos — a Pessoa11 "
            "não pergunta 'quer visitar?', pergunta 'quando'."
        ),
        "sugestao_modelo": (
            "consigo te levar terça 10h ou quarta 16h pra conhecer o decorado — "
            "qual fica melhor pra você?"
        ),
    },
    {
        "id": "horario_concreto",
        "categoria": "movimento_visita_sem_horario",
        "titulo": "Dar 2 horários concretos (não 'sim' vago)",
        "quando": (
            "a corretora convidou pra visita mas deixou aberto ('me avisa quando "
            "puder', 'qual dia é melhor?') sem oferecer dois horários fechados."
        ),
        "gap": (
            "o convite ficou sem compromisso de tempo — oferecer dois horários "
            "concretos pro lead só escolher costuma fechar a agenda."
        ),
        "sugestao_modelo": (
            "tenho quinta 9h ou sexta 15h livres — qual desses encaixa melhor?"
        ),
    },
    {
        "id": "tira_friccao",
        "categoria": "movimento_sem_tira_friccao",
        "titulo": "Tirar a fricção (visita rápida/leve)",
        "quando": (
            "lead hesitou na visita por tempo/compromisso ('tô sem tempo', 'depois "
            "vejo') e a corretora não aliviou o peso do convite."
        ),
        "gap": (
            "vale prometer que é rápido e leve — 'visita breve, só pra tirar as "
            "dúvidas' derruba a objeção de tempo."
        ),
        "sugestao_modelo": (
            "prometo que é rapidinho, uns 20min só pra você sentir o apê — sem "
            "compromisso nenhum. topa?"
        ),
    },
    {
        "id": "traz_decisor",
        "categoria": "movimento_sem_decisor",
        "titulo": "Trazer o decisor junto (cônjuge/sócio)",
        "quando": (
            "a conversa revelou outro decisor (marido/esposa/sócio/'vou ver com...') "
            "e a corretora não convidou esse decisor pra visita junto."
        ),
        "gap": (
            "chamar o decisor pra visita junto mata o 'vou falar com...' depois — "
            "todos tiram as dúvidas de uma vez."
        ),
        "sugestao_modelo": (
            "que tal irmos junto com seu marido? assim vocês dois já tiram as "
            "dúvidas de uma vez. qual dia fica bom pros dois?"
        ),
    },
    {
        "id": "qualifica_dinheiro",
        "categoria": "movimento_sem_qualificar_dinheiro",
        "titulo": "Qualificar o dinheiro (empolgando, não travando)",
        "quando": (
            "o lead sinalizou dúvida/objeção financeira (entrada, financiamento, "
            "'não sei se cabe') e a corretora não qualificou o poder de compra "
            "como um GANHO (subsídio/Caixa/entrada que abate)."
        ),
        "gap": (
            "dá pra transformar dinheiro em presente — mostrar o subsídio/poder de "
            "compra como algo que o lead GANHA derruba a objeção antes da visita."
        ),
        "sugestao_modelo": (
            "quer que eu veja seu poder de compra? a Caixa costuma liberar um "
            "subsídio que abate bem da entrada — te mando o número certinho."
        ),
    },
    {
        "id": "escassez",
        "categoria": "movimento_sem_escassez",
        "titulo": "Usar escassez/exclusividade REAL",
        "quando": (
            "havia gatilho real de escassez (lançamento, unidade que sai rápido, "
            "imóvel fora da curva) e a corretora não usou pra acelerar a decisão."
        ),
        "gap": (
            "escassez real (lançamento novo, unidade que some rápido) acelera — só "
            "vale quando é verdade, nunca inventada."
        ),
        "sugestao_modelo": (
            "esse é lançamento, saiu sexta e as melhores unidades somem rápido — "
            "consigo te garantir uma visita ainda essa semana, bora?"
        ),
    },
    {
        "id": "followup_humano",
        "categoria": "movimento_followup_robotico",
        "titulo": "Follow-up HUMANO (reabre sem cobrar)",
        "quando": (
            "a corretora fez follow-up robótico/cobrança ('e aí?', 'oi, tudo bem?', "
            "'ainda tem interesse?') tendo pista pessoal do lead (filho, viagem, "
            "trabalho) que daria pra usar pra reabrir com calor."
        ),
        "gap": (
            "follow-up que lembra algo pessoal do lead ('como foi a viagem?', 'como "
            "está sua filha?') reabre sem soar cobrança."
        ),
        "sugestao_modelo": (
            "oi! passando só pra saber como foi a mudança de trabalho — e se ainda "
            "posso te ajudar naquela busca do apê?"
        ),
    },
)


# id de categoria de cada movimento (usado pra registrar CATEGORIAS_VALIDAS).
CATEGORIAS_MOVIMENTO: Final[frozenset[str]] = frozenset(
    m["categoria"] for m in MOVIMENTOS
)


def _render_playbook() -> str:
    linhas = [
        "# PLAYBOOK DE FECHAMENTO (Pessoa1 — 39% atendido→visita vs ~12% do time)",
        "",
        "O gargalo nº1 do time é 'atendido → agendado'. A Pessoa11 resolve isso PEDINDO a",
        "visita de forma estruturada — as outras esperam o lead pedir. Ao coachar,",
        "verifique quais destes 6 movimentos FALTARAM na conversa e cobre o mais",
        "relevante. PRIORIDADE MÁXIMA: se o lead está quente e a corretora NÃO",
        "propôs a visita amarrando agenda, esse é quase sempre o gap a apontar.",
        "",
    ]
    for i, m in enumerate(MOVIMENTOS, start=1):
        linhas.append(f"{i}. {m['titulo']}  →  categoria: {m['categoria']}")
        linhas.append(f"   Cobre quando: {m['quando']}")
        linhas.append(f"   Sugestão no tom da Pessoa11 (adapte ao lead): \"{m['sugestao_modelo']}\"")
        linhas.append("")
    linhas.append(
        "Regra de ouro da Pessoa11: toda mensagem termina em PERGUNTA "
        "('o que acha?', 'qual fica melhor?', 'me dá um ok aqui?') — mantém a bola com o lead."
    )
    return "\n".join(linhas)


PLAYBOOK_CORRETORA_B: Final[str] = _render_playbook()


def aplicar_playbook() -> str:
    """Retorna o bloco do playbook Pessoa1 pronto pra injetar num system prompt.

    Fonte única dos 6 movimentos de fechamento — use este helper em vez de
    referenciar a string direto, pra centralizar versão/cabeçalho aqui.
    """
    return PLAYBOOK_CORRETORA_B
