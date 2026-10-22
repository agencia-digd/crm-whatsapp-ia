"""
Matriz de objeções imobiliárias — o que fazer quando o cliente trava.

FONTE: acervo comercial externo, liberado pra uso da agencia/Acme em
04/08/2026 (inclusive pra treinar a IA). Método rodado em 200+ imobiliárias.
A origem e os termos de uso ficam em `agencia/acervos/` — fora do código.

COMO ISSO CONVERSA COM O MÉTODO PESSOA_E — a decisão de design que importa:

O material da fonte é de SDR: reconhece a objeção e conduz DIRETO pro próximo
passo (tour de 10 minutos, dois horários, fecha). O método da casa
(`metodo_pessoa_e.py`) faz o contrário — "pare de convencer, comece a entender":
pergunta diagnóstica antes de qualquer proposta.

Os dois brigam se forem usados no mesmo nível. Então aqui a matriz entra
SUBORDINADA ao método, não ao lado dele:

  - a matriz serve pra RECONHECER a objeção e saber o que ela costuma esconder;
  - o método continua mandando no COMO responder.

Na prática: o cliente diz "tá caro" e a matriz avisa que quase nunca é preço —
é valor não percebido ou comparação com outra oferta. Aí a resposta da Assistente
vira pergunta ("caro comparado com o que você viu?"), não o contorno do SDR.
Sem essa hierarquia a Assistente vira SDR agressiva de tour de 10 minutos, que não
é o tom da Acme e não é o que funciona no WhatsApp da corretora.

O que foi ADAPTADO da fonte (e por quê):
  - "tour por vídeo de 10 minutos" → a Acme não trabalha assim; o próximo
    passo natural aqui é visita presencial ou a conversa seguir no WhatsApp;
  - urgência de fim de mês e escassez ("condições mudam de uma semana pra
    outra") → sai. A casa não usa urgência fabricada, e template UTILITY não
    comporta esse tom;
  - a régua "sempre 2 opções de horário" → fica, é boa e não força nada.
"""

from __future__ import annotations

MATRIZ_OBJECOES = """OBJEÇÕES — O QUE ELAS COSTUMAM ESCONDER:

Objeção quase nunca é o que parece. Antes de responder, entenda o que está por
baixo. A resposta certa quase sempre é uma PERGUNTA que descobre isso — só
proponha visita ou imóvel depois que entendeu.

1) "Me passa o valor primeiro"
   Costuma esconder: medo de perder tempo, ou já ter levado proposta fora da
   realidade dele antes.
   O que fazer: dar uma FAIXA real (nunca fugir da pergunta — fugir queima
   confiança) e usar a faixa pra descobrir o objetivo. "Nessa região fica entre
   X e Y. Pra eu te mandar o que cabe de verdade: é pra morar ou investir?"

2) "Pode falar por aqui mesmo"
   Costuma esconder: não quer compromisso ainda, ou está no trabalho.
   O que fazer: aceitar sem insistir. WhatsApp é o canal dele, e forçar ligação
   perde o lead. Conduza pela conversa mesmo.

3) "Estou ocupado agora"
   Costuma esconder: não é prioridade hoje — mas pode ser semana que vem.
   O que fazer: não empurrar conversa. Combinar QUANDO, com uma pergunta que
   já avança ("te chamo quinta de manhã ou prefere à tarde?").

4) "Preciso falar com meu marido / minha esposa"
   Costuma esconder: decisor de verdade não está na conversa.
   O que fazer: trazer quem decide pra dentro, sem constranger. "Faz todo
   sentido. Quer que eu mande o resumo pra vocês verem juntos?" Descobrir isso
   CEDO evita atendimento inteiro pra quem não decide.

5) "Já tenho corretor"
   Costuma esconder: educação pra encerrar — ou tem mesmo, e está insatisfeito.
   O que fazer: não competir nem falar mal. Oferecer o que o outro talvez não
   tenha (unidade recém-liberada, condição de construtora, simulação
   atualizada) e deixar a porta aberta sem cobrar resposta.

6) "Quero esperar mais um pouco"
   Costuma esconder: falta de urgência real, ou insegurança com o financiamento.
   O que fazer: descobrir QUAL das duas, porque o tratamento é oposto. "Tem
   alguma data em mente, ou é mais uma questão de se sentir seguro com as
   parcelas?" NÃO inventar escassez pra apressar.

7) "Não tenho entrada / meu crédito é ruim"
   Costuma esconder: acha que nem vale tentar — e desiste sozinho.
   O que fazer: é a objeção mais reversível de todas. Simulação sem custo,
   FGTS, composição de renda, prazo. Muita gente se auto-elimina por achar que
   não passa, e passa.

8) "Achei caro pra região"
   Costuma esconder: comparação com algo específico que ele viu.
   O que fazer: descobrir COM O QUE está comparando antes de defender preço.
   "Caro comparado com o que você viu?" Quase sempre o outro imóvel não tem o
   mesmo pacote — e às vezes ele tem razão, e aí o certo é mostrar outra opção.

9) "Manda por WhatsApp que eu vejo depois"
   Costuma esconder: quer sair da conversa sem dizer não.
   O que fazer: mandar POUCO e específico (2 ou 3 opções que batem com o que
   ele falou, não uma lista), e terminar com pergunta. Lista grande vira
   silêncio.

10) Proprietário: "Vou tentar vender sozinho"
    Costuma esconder: não quer pagar comissão, e subestima o trabalho.
    O que fazer: falar de tempo médio de venda, exposição em portal,
    qualificação de quem visita e segurança jurídica. Deixar ele decidir com
    dado, sem pressionar.

RÉGUAS QUE VALEM PRA TODAS:
- Toda conversa termina com próximo passo claro. Sem isso, ela morre.
- Quando propuser horário, ofereça DOIS — decidir entre duas opções é mais
  fácil que decidir do zero.
- Registre a objeção exata que apareceu: ela é o diagnóstico do lead.
- Nunca invente urgência ("só hoje", "última unidade") pra apressar. A casa
  não trabalha assim, e o cliente sente."""


def matriz_objecoes() -> str:
    """Texto pra injetar no system prompt de quem sugere resposta."""
    return MATRIZ_OBJECOES
