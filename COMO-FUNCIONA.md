# Como o CRM funciona hoje

CRM de imobiliária com WhatsApp e IA. As corretoras trabalham no painel; o resto (primeira mensagem,
follow-up, nutrição, remarketing, ligações) roda sozinho, atrás de interruptores que se ligam e
desligam pelo painel, sem deploy. Este texto descreve o que o código faz, com o nome do arquivo onde
cada regra mora, em `crm/api/app/services/`.

## Visão geral: o caminho do lead

```
Anúncio (Meta) ──► NOVO ──► primeira mensagem ──► corretora assume
                     │          (roleta escolhe a corretora)
                     │
                     └─► corretora arrasta pra FOLLOW-UP ──► cadência de 9 toques (10 dias)
                                                                  │ acabou sem resposta
                                                                  ▼
                                        NUTRIÇÃO: 1 mensagem a cada 30 dias (até 150 dias)
                                                                  │ base fria
                                                                  ▼
 REMARKETING em massa (número da casa) ──► lead responde ──► IA atende e qualifica
        ▲                                                         │ tem interesse real
        │ volta pra fila em ~30 dias                              ▼
        │                                  roleta do remarketing ──► NOVO com tag "remarketing"
        │                                                         │
 LIGAÇÕES: corretora liga pra base ───────────────────────────────┘ (atendeu e quer continuar)
```

Regras que valem em tudo: só template **UTILITY** aprovado (categoria Marketing custa cerca de 10x
e queima a lista), toda mensagem termina com pergunta, **opt-out** sempre vence, limite diário por
linha de WhatsApp, e horário comercial.

## 1. Primeira mensagem (boas-vindas)
`lead_intake.py`, `boas_vindas_copies.py`

- O lead entra pelo webhook do anúncio, vira um card na etapa **Novo** (um lead por telefone;
  repetir o webhook não duplica).
- **Roleta de lead novo**: escolhe a corretora dona do card em rodízio, entre as corretoras da lista
  `roleta_corretoras`. A lista se edita no painel e vale já para o próximo lead.
- Sai **uma** mensagem de boas-vindas pela linha da corretora dona, e o sistema espera. É um teste
  A/B com três variantes (A controle, B triagem por intenção, C triagem por prazo), sorteadas de
  forma fixa por lead; a variante que saiu fica gravada no evento.
- Nada mais automático acontece aí. A corretora assume o lead à mão.
- Interruptor da página de boas-vindas (`lead_intake_ativo`).

## 2. Follow-up (cadência de 9 toques)
`cadencia.py`

Começa quando a corretora arrasta o card para **Follow-up** (ou pela matrícula automática). Em 10 dias:

| Dia | Toque |
|---|---|
| D0 | mensagem de reencontro (WhatsApp) |
| D1 | tarefa de ligação para a corretora |
| D2 | mensagem |
| D3 | oferta de áudio, com botão "receber o áudio" (o clique abre a janela de 24h e o áudio sai sem template; clique não conta como resposta, o lead segue) |
| D4 | mensagem |
| D5 | tarefa de ligação |
| D7 | mensagem |
| D9 | última tarefa de ligação |
| D10 | toque "marketing": **não sai** (a linha só manda UTILITY), fica registrado como pulado e o lead segue para a nutrição |

A cadência só enfileira e passa por `enviar_template_corretor`,
atrás de DRY-RUN, opt-out, limite diário e espaçamento mínimo. Rodar o ciclo duas vezes não duplica
nada (um toque por lead por cadência). O ciclo roda a cada 3 minutos no horário comercial.
Se o lead responde, a cadência para (`processar_resposta_cadencia`).

Tem **interruptor geral** (`cadencia_ativa`) e **um interruptor por corretora** para follow-up e
nutrição (`config_operacional`); desligar uma corretora tira só os leads dela da cadência.

## 3. Nutrição (depois do follow-up)
`nutricao.py`

Quem esgota os toques sem resposta vai para o funil **Remarketing/Nutrição** do painel e recebe
uma mensagem a cada 30 dias pelo número da corretora:

Base → 30 dias → 60 → 90 → 120 → **150 dias** (para aqui; resposta e opt-out continuam funcionando).

Tem kill-switch próprio (`nutricao_ativa`), os mesmos limites da cadência e conta junto no limite
diário da linha.

## 4. Remarketing em massa (o "carrossel")
`carrossel_corretor.py`, `remarketing_alvo.py`, `remarketing_agenda.py`, `rampa.py`, `saida_remarketing.py`

Funciona como uma **roda**: **Fila → Disparado → volta pra Fila**.

- Sai um template UTILITY pelo número da casa (a linha marcada como `disparo`) para cada lead da
  etapa de origem configurada (`remarketing_etapa`) e do dono configurado (`remarketing_dono`).
- Quanto sai por dia é a **rampa**: começa em 20/dia, sobe até 80 e só passa disso (até 300) com
  qualidade GREEN na Meta; YELLOW limita a 50 e RED zera. O limite digitado na tela só baixa.
- A agenda é configurável: janela de horário, dias da semana, limite diário e limite semanal.
  Ler a configuração que falha **segura** o disparo (falha fechada); o interruptor nasce desligado.
- Quem não respondeu descansa cerca de 30 dias e volta para o fim da fila. Quem responde sai da roda.

### O que acontece quando o lead responde
`atendimento_corretor.py`, `handoff_remarketing.py`, `aviso_remarketing.py`

1. A **IA atende** pelo número da casa e qualifica (renda, interesse). Não manda texto livre fora da
   janela de 24h.
2. Se há **interesse real** (quer comprar ou vender, pergunta preço, bairro, visita, ou responde
   "sim" ao disparo), o lead é transferido na hora para uma corretora pela **roleta do
   remarketing** (`handoff_remarketing_roleta`), que é **separada** da roleta do Meta.
   Quem pediu para parar ou recusou nunca é transferido.
3. O card chega na etapa **Novo** com a **tag "remarketing"** (`veio_remarketing_em`).
4. Card em Novo com essa tag dispara a automação de **aviso**: não é a boas-vindas padrão, é uma
   mensagem pela linha da corretora que pegou o lead, dizendo que a conversa continua por outro número.
   Vale para os quatro caminhos de entrada (inbound automático, botão de transferir, lote, roleta),
   com limite próprio de 10/dia por linha e horário 9h–19h, sem domingo.
5. Se a pessoa já vinha sendo atendida por uma corretora, a régua de reengajamento não dispara por
   cima (trava de conversa humana de 7 dias, e `saida_remarketing` tira o lead da fila quando ele volta a falar).

## 5. Ligações
`ligacao_resultado.py`, `pos_ligacao.py`, `boas_vindas_continuidade.py`

- Cada corretora tem uma **lista de ligação** (aba "Ligar" em Tarefas), alimentada pela base de
  remarketing, mais as tarefas de ligação da cadência.
- Ao concluir a ligação, um clique registra o resultado:
  `não atendeu` · `atendeu e não quer continuar` · `atendeu e quer continuar`.
- Em **"atendeu e quer continuar"** o lead é **adotado**: vai para **Novo**, ganha a corretora como
  dona e recebe a tag `adotado_ligacao_em`.
- A mensagem que sai é a de **continuidade** ("estávamos falando por telefone, vamos continuar por
  aqui"), de propósito diferente da boas-vindas padrão e fora do teste A/B. Também é template UTILITY,
  pois ligação não abre a janela de 24h. `pos_ligacao_ativo` nasce desligado.
- Há métrica de ligações (taxa de atendimento, de continuidade), sempre com amostra mínima: "sem
  dado" nunca vira 0%.

## Roletas e interruptores

Duas roletas independentes (o follow-up não tem roleta: usa a dona do card e um interruptor por corretora):

| Roleta | Quem entra | Onde se muda |
|---|---|---|
| Lead novo do Meta | lead de anúncio (primeira mensagem) | `roleta_corretoras`, tela de boas-vindas |
| Remarketing | lead que respondeu ao disparo em massa (e leva a tag) | `handoff_remarketing_roleta` |

Interruptores (`config_operacional`, com histórico de quem mudou e quando): `lead_intake_ativo`
(primeira mensagem), `cadencia_ativa`, `nutricao_ativa`, o do remarketing em massa, `pos_ligacao_ativo`
e o do aviso. Sem linha gravada no banco vale o `.env`; as chaves do disparo em massa são exceção e
**falham fechadas** (sem linha, desligado).

## Mensagens, áudios e a leitura do copiloto
`mensagens_evolution_service.py`, `transcricao_service.py`, `transcricao_whisper_mac.py`, `leitura_ia.py`, `sinal_negocio.py`, `anti_ghosting.py`, `vigia_silencio.py`

**Onde as mensagens ficam.** Toda mensagem que chega ou sai do WhatsApp é gravada no Postgres, na
tabela `mensagens_whatsapp` (texto, tipo, quem mandou, horário, telefone com sufixo-8 para casar com
o card, e o payload original). Antes disso, o webhook da Meta passa por um **ledger** (um registro por
evento recebido): se o repasse ao CRM demora e o resultado é incerto, o evento vai para uma
**quarentena** em vez de ser refeito, para não duplicar mensagem. Um vigia avisa quando há algo preso.

**Áudios.** O áudio chega como mídia e fica com `transcricao_status = pendente`. Um timer
(`transcricao-backfill`) baixa o arquivo (Graph da Meta ou Evolution, escolhido pelo **formato do payload** da própria mensagem, e não pelo nome da instância) e
transcreve com **Whisper large-v3** rodando num Mac mini (sem chave e sem limite), com **Gemini** como
reserva. O texto vai para a coluna `transcricao` da mesma mensagem; assim o áudio entra na leitura
como se fosse texto. Se a Graph pede para ir mais devagar, o áudio volta para a fila em vez de virar
erro. Outro timer (`transcricao-watch`) alerta se a fila trava.

**O que o copiloto lê sozinho, de tempos em tempos** (nenhuma dessas rotinas move card):
- **Leitura de conversa por IA** (`leitura_ia`): duas vezes por dia (aviso) e no resumo da noite, um
  modelo pequeno (Haiku 5.5) lê as últimas 40 mensagens de cada conversa ativa nas últimas 24h e diz
  se o cliente está *fechando* (documentação entregue, crédito em análise ou aprovado, cartório, sinal,
  reserva, assinatura, data marcada com banco) ou só interessado (preço, visita, simulação). Só entram
  conversas que têm card (não há o que subir sem card), de fora da equipe, com confiança alta no aviso;
  confiança média fica só para o painel. Dono que combina preço de anúncio ainda não é fechamento.
  Não move card nem fala com o cliente: avisa no Telegram ("o card precisa subir") e abre o resumo da
  noite com a lista. Se a IA falha, o resumo sai sem esse bloco. Cada chamada grava custo e latência
  (`llm_chamadas`, com tabela de preços em `llm/precos.py`).
- **Sinal de negócio por vocabulário** (`sinal_negocio`): a versão anterior, uma lista de palavras.
  Continua no código (a auditoria do funil ainda a usa), mas deixou de mandar aviso: na amostra de um
  dia ela acusou 5 conversas que eram só simulação, e a IA derrubou as 5.
- **Anti-ghosting** (`anti_ghosting`): quem estava conversando e sumiu com a janela de 24h ainda
  aberta recebe até 5 toques de texto livre (1h30, 3h, 6h, 10h e 15h), de graça e sem template. Para no
  quinto, que é a saída digna. Nasce **desligado** (`anti_ghosting_ativo`) e assim deve ficar: as corretoras não gostaram de mensagem automática em conversa que elas já tocavam.
- **Vigia do silêncio** (`vigia_silencio`): lead de anúncio com mais de 30 min e nenhuma mensagem de
  saída gera alerta. Cobre qualquer causa (intake desligado, linha sem template, roleta vazia).
- **Visita**: lembrete no dia anterior, detecção de no-show e de pendência da corretora.

## Opt-out
`opt_out.py`: só com intenção clara. Frase inequívoca ("não quero receber", "pare de mandar") em
qualquer lugar, ou palavra-comando em mensagem curta. "Para" no meio de frase e "cancelar visita" não
contam. É checado antes de qualquer envio, em todos os motores.

## O que revisar antes de instalar
- Várias peças são específicas do negócio original (Kommo, Evolution, calendário, Instagram). Troque
  ou apague o que não usar.
- Os nomes de pessoas foram trocados por marcadores; os textos dos prompts e das mensagens precisam
  ser reescritos para a sua operação.
- Os templates de WhatsApp precisam ser cadastrados e aprovados na sua conta da Meta.
