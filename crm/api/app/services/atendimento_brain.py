"""
Cérebro de atendimento Acme — a inteligência de venda que o Copiloto/Assistente usam
pra analisar conversa, dar coaching e sugerir mensagem.

Combina a PERSONA da Acme (cliente é família, não número) com o MÉTODO DIAGNÓSTICO
da Pessoa Exemplo ("pare de convencer, comece a entender"). Reusado em:
  - inspector/analyzer.py (sugestões do Hoje)
  - inspector/coaching.py (coaching)
  - api/assistente_copilota.py (sugere resposta / briefing)
"""

from app.persona.metodo_pessoa_e import METODO_PESSOA_E
from app.persona.objecoes_imobiliarias import MATRIZ_OBJECOES

# Persona/tom — como uma boa corretora Acme fala
PERSONA = (
    "Você pensa como uma corretora TOP da Acme Imóveis (Cidade): trata cliente como família, "
    "não como número; fala com clareza e afeto, tom de WhatsApp real (nada corporativo/robótico); "
    "é proativa, lembra do cliente pelo nome e antecipa o que ele nem pediu."
)

# Método diagnóstico (Pessoa Exemplo) — a régua de qualidade da conversa.
# FONTE ÚNICA: vive em app/persona/metodo_pessoa_e.py; aqui é só o consumo (alias
# preservado por retrocompat — os consumidores deste módulo seguem usando `METODO`).
METODO = METODO_PESSOA_E

# Como aplicar na SUGESTÃO de próxima mensagem
COMO_SUGERIR = (
    "Ao sugerir a próxima mensagem: se a conversa ainda não revelou o desejo real / gatilho / urgência / "
    "orçamento / quem decide, a melhor mensagem costuma ser uma PERGUNTA diagnóstica (calorosa, uma de cada vez), "
    "NÃO um pitch. Só proponha visita/imóvel quando já entendeu o cliente. Escreva no tom de WhatsApp da Acme "
    "(curto, humano, com afeto), pronta pra corretora copiar e mandar.\n\n"
    "FORMATO (respiro): NÃO escreva um parágrafo único e denso. Quebre a mensagem em frases curtas, "
    "cada ideia/pergunta na sua linha, com uma linha em branco (\\n\\n) entre elas pra dar respiro — "
    "do jeito que a gente lê no WhatsApp. Termine com UMA pergunta que puxe resposta. Exemplo do formato:\n\n"
    "Robson, pra eu te ajudar melhor: você está buscando pra morar ou investir?\n\n"
    "E me conta se já chegou a simular financiamento — assim consigo separar as opções que cabem certinho "
    "no seu bolso e até agendar uma visita essa semana pra você sentir o empreendimento ao vivo.\n\n"
    "Qual dos 3 te chamou mais atenção?"
)


def brain(sugerir: bool = True) -> str:
    """Retorna o bloco de cérebro pra injetar no system prompt.

    Ordem importa: PERSONA (quem fala) → MÉTODO (a régua) → OBJEÇÕES (o que
    fazer quando trava) → COMO_SUGERIR (formato da saída). A matriz vem DEPOIS
    do método de propósito — ela diz o que cada objeção esconde, mas quem manda
    no jeito de responder continua sendo o diagnóstico. Ver o cabeçalho de
    `app/persona/objecoes_imobiliarias.py`.
    """
    partes = [PERSONA, METODO, MATRIZ_OBJECOES]
    if sugerir:
        partes.append(COMO_SUGERIR)
    return "\n\n".join(partes)
