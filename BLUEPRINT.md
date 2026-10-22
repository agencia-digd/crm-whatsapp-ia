# Blueprint do CRM

## 1. Ideia central
Um CRM onde **o lead chega de anúncio, cai numa fila, é distribuído a um vendedor (roleta) e o
atendimento acontece no WhatsApp**. Em volta disso, três camadas:

1. **Operação humana**: funil, caixa de entrada, tarefas, visitas/reuniões, ligações, vendas.
2. **Automação de mensagens (régua)**: primeira mensagem, follow-up, remarketing, lembretes, tudo
   com travas de horário, opt-out e template aprovado.
3. **IA do lado do vendedor** (copiloto): sugere resposta, resume conversa, avalia o atendimento.
   Começa em **modo sombra**: a IA propõe, o humano marca "acertou/errou", só depois ganha poder.

## 2. Stack usada (troque à vontade)
- Painel: Next.js (App Router) + CSS próprio, login por cookie de sessão, perfis (admin / vendedor).
- API do copiloto: FastAPI (Python) + PostgreSQL + Alembic (migrations).
- WhatsApp: API oficial da Meta (Cloud API) para envio com template; uma ponte não-oficial (Evolution)
  só para **ler** a conversa do vendedor. Webhook próprio por número.
- Agendador: systemd timers (um por rotina) — simples, auditável, sem fila externa.
- IA: modelos de linguagem por chamada, com registro de tokens e custo por serviço (tabela
  `llm_chamadas`).

## 3. Telas (menu)
| Tela | Para quê |
|---|---|
| Meu dia | O que o vendedor faz agora: tarefas atrasadas, leads novos, retornos. |
| Funil / Visão | Quadro por etapa, com contagem e tempo parado. |
| Chat | Caixa de entrada WhatsApp unificada, não lidas por usuário, prévia "quem falou", áudio com player. |
| Lead | Ficha: histórico, tarefas, observações, origem, próxima ação. |
| Tarefas | Lista por vendedor, com atraso destacado. |
| Visitas | Agenda e lembrete automático de véspera. |
| Ligações | Fila de ligação do dia (reserva de 30 min por lead, resultado: atendeu / quis continuar). |
| Automações | Mapa das réguas: ligada/desligada, última execução, erro, texto, template, trava. |
| Templates | Status real de cada template na Meta (aprovado/em análise/reprovado), por número. |
| Remarketing | Quem está na régua, quem saiu e por quê. |
| Ranking / Histórico | Placar por vendedor, mês a mês. |
| Gastos | Custo de mensagens (por template enviado) e da IA, por mês. |
| Aprendiz | Placar de acerto da IA em modo sombra (humano > juiz IA > pendente) e o que mudou. |

## 4. Régua de mensagens (o motor)
Estados em tabela (`cadencia_estado`): lead, toque atual, próximo toque, status. Eventos em outra
(`cadencia_eventos`) para auditoria. Um timer varre de minuto em minuto e dispara o que venceu.

**Regras que valem para toda mensagem automática:**
1. Só dentro do **horário permitido** (ex.: 8h–20h, não domingo).
2. **Opt-out checado antes de cada envio**, não só no cadastro.
3. Fora da janela de 24h do WhatsApp, **só template aprovado**. Dentro da janela, texto livre.
4. Teto diário e cota por número (para não queimar a qualidade do número).
5. **Estado de envio mora no banco**, nunca em memória: reiniciar o serviço não pode reenviar.
6. Resposta do lead **cancela** o próximo toque pendente.
7. Todo envio grava o texto exato enviado (auditoria).

**Opt-out**: pedido explícito ("parar", "sair", "me tira da lista") sempre vale. Desinteresse claro
vale **só se for a mensagem inteira** ("não tenho interesse"). Frases ambíguas ("não agora",
"não é esse", "depois eu vejo") **não** viram opt-out — testadas com mensagens reais, falso positivo = 0.

## 5. Templates do WhatsApp
- Categorias que importam: **UTILITY** (barata, aprovada fácil se for aviso de algo que o lead pediu)
  e **MARKETING** (mais cara). Meta pode **reclassificar** UTILITY para MARKETING sozinha.
- Regras de aprovação que derrubaram templates: proporção palavras/variáveis (erro 2388293);
  não começar com variável (2388299); nome apagado fica em cooldown de 4 semanas (2388025).
- Rotina horária sincroniza status com a Meta e guarda um snapshot; a tela de templates lê o snapshot.
- Rotina diária mantém um **piso por número** (ex.: ≥5 UTILITY e ≥5 MARKETING aprovados) e propõe
  o que subir. Só relatório: quem sobe é humano.
- Modelo de texto que passou na Meta: `Olá {{1}}` / `{{2}}` / frase fixa de 15+ palavras / botão
  opcional ("Saiba mais") — a frase fixa longa é o que evita a recusa por proporção.

## 6. IA do lado do vendedor
- **Modo sombra**: para cada lead parado, a IA escreve a resposta que daria, grava com motivo e
  confiança, **não envia**.
- O vendedor marca acertou/errou na tela. Um **juiz de IA** avalia por intenção, com teto de custo.
  Placar honesto: humano vale mais que juiz, juiz mais que pendente.
- As últimas correções humanas (máx. 5, sem telefone/nome/link) podem voltar como exemplos no prompt,
  **atrás de uma flag desligada por padrão**, com medição antes/depois. Prove que, desligada, o
  prompt é idêntico byte a byte.
- Todo uso de modelo registra serviço, modelo, tokens e custo. Modelo sem preço cadastrado = custo
  vazio, nunca zero.

## 7. Qualidade e operação
- **Vigias**: rotina que avisa quando uma automação está dormindo (ligada mas sem executar) e quando
  o número está caindo de qualidade.
- **Deploy com rollback**: build em pasta separada, troca atômica, health-check, volta sozinho se
  falhar. Nunca `build` por cima do que está servindo.
- **Testes**: a lógica de regra (opt-out, horário, régua) tem teste com mensagens reais anonimizadas.
- **Config operacional em tabela** (`config_operacional` + histórico): quem mudou o quê e quando.
- **Isolamento por cliente** (`tenant_id` em toda tabela) desde o primeiro dia — retrofit é caro.

## 8. Lições caras
1. Dois números de WhatsApp com a mesma função confundem a régua: um número, uma função.
2. Mensagem "enviada" ≠ entregue: guarde o status do webhook e trate status órfão.
3. Cast SQL de campo texto para número dentro de `WHERE` estoura em dado sujo: use `CASE`.
4. Id de migration duplicado quebra o Alembic ("multiple heads"): confira antes de mesclar.
5. Meta reclassifica template sozinha: monitore a categoria, não só o status.
6. Medir antes de portar: o que parece "motor quebrado" muitas vezes é motor diferente.
7. Nada de IA mandando mensagem sem passar por template aprovado, horário, opt-out e teto.

## 9. O que não está aqui de propósito
Código-fonte, dados de leads, nomes de clientes e vendedores, credenciais, IDs de número.
