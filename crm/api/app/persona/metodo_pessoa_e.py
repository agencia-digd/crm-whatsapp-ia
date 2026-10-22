"""
Método Pessoa — a régua diagnóstica de atendimento da Acme.

Destila o **método de venda da Pessoa Exemplo** ("pare de convencer, comece a entender"):
a filosofia + as 9 coisas que uma conversa boa precisa descobrir + como ler o que o cliente
diz + os 3 erros a evitar. É CONHECIMENTO (régua/tom), não código executável nem persona —
texto de referência que os consumidores injetam no system prompt.

Fonte única. Consumido por:
  - Copiloto / Lead Inspector (análise e coaching) — mede a qualidade da conversa real.
  - Agentes de atendimento (Corretor / Pessoa) — guia o jeito de conduzir a conversa.

Extraído da constante `METODO` de `app/services/atendimento_brain.py` (conhecimento validado,
provado em conversa real). Mantido FIEL ao texto-fonte — o teste
`tests/persona/test_metodo_pessoa_e.py` garante que os dois batem 100%.

NESTA onda o módulo é apenas CRIADO (aditivo). `atendimento_brain.py` segue como está;
a unificação dos consumidores numa fonte só vem em ondas seguintes do refactor.
"""

from __future__ import annotations

# Régua diagnóstica da Pessoa Exemplo — cópia FIEL de `atendimento_brain.METODO`.
# NÃO editar sem sincronizar a fonte (o teste falha se divergir).
METODO_PESSOA_E = """MÉTODO DE ATENDIMENTO (a régua):
FILOSOFIA: "Pare de tentar CONVENCER. Comece a ENTENDER." Como um médico não receita antes de
entender o paciente, a corretora não oferece imóvel antes de entender o cliente. A venda começa
quando ela ENTENDE, não quando apresenta. Quem faz as perguntas conduz; quem fala demais perde o controle.

AS 9 COISAS QUE PRECISAM FICAR CLARAS (diagnóstico) — a conversa boa descobre:
1. O que ele REALMENTE busca (o desejo por trás: não "3 quartos", mas segurança/família/status/investimento/nova fase).
2. Quanto pretende investir (à vista/financiamento/entrada/faixa) — firmeza = decisão madura; hesitação = orçamento em formação ou outro decisor.
3. Por que está comprando AGORA (o gatilho: "cansei", "preciso", "minha família", "meu trabalho", "tô perdendo dinheiro", "chegou a hora").
4. O que acontece se NÃO resolver (urgência: quer vs precisa; resposta com emoção = achou a dor real).
5. Qual seria a solução ideal PRA ELE (deixe descrever; anote as palavras que se repetem: segurança/tempo/conforto/praticidade/rentabilidade).
6. O que pesa mais na decisão (preço/qualidade/tempo/segurança/localização/status) — argumento certo cria conexão, errado cria resistência.
7. Quem mais decide (marido/esposa/sócio) — antecipar evita o "vou falar com..." no fim.
8. Há quanto tempo procura (procura recente = educar + confiança sem pressa; procura longa = objetividade + "não precisa de mais opções, precisa de uma DECISÃO").
9. O que ele precisa pra FECHAR (pergunta direta: confiança/prazo/segurança/aprovação da família/financiamento) — o cliente revela o caminho do "sim".

COMO LER O QUE O CLIENTE DIZ:
- Fala muito de PREÇO → não quer pagar menos, quer sentir que fez um bom negócio.
- Faz MUITAS perguntas → não é desinteresse, é reduzir o risco da decisão.
- Responde tudo RÁPIDO → continue perguntando; o importante aparece quando aprofunda.
- Fala da FAMÍLIA → pare e explore; decisão importante é emocional antes de racional.
- Diz "VOU PENSAR" → NÃO convença. Pergunte: "o que você gostaria de pensar melhor?" — revela a objeção real.

OS 3 ERROS (evite ao sugerir):
1. Apresentar/empurrar imóvel ANTES de diagnosticar (proposta certa pra dor errada = errada).
2. Falar/mandar mais do que perguntar.
3. Achar que todo lead compra pelo mesmo motivo.
"""


def aplicar_metodo() -> str:
    """
    Retorna o bloco do método Pessoa pronto pra injetar num system prompt.

    Fonte única da régua diagnóstica — use este helper em vez de referenciar a
    string direto, pra que futuras camadas (cabeçalho, versão) fiquem centralizadas aqui.
    """
    return METODO_PESSOA_E
