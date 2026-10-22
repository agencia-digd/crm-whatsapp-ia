# CRM com IA no WhatsApp

Código de um CRM de atendimento por WhatsApp com IA do lado do vendedor, em produção numa operação
imobiliária. **Sem dados de lead, sem credenciais, sem nomes reais**: nomes de pessoas, marca do
cliente, telefones, e-mails e IDs foram trocados por marcadores (`Acme`, `PessoaN`, `5500000000000`).

```
crm/api      API do copiloto (FastAPI + PostgreSQL + Alembic): régua de mensagens, opt-out,
             templates, IA em modo sombra, vigias, ~90 migrations, ~220 arquivos de teste
crm/painel   Painel (Next.js): meu dia, funil, chat, tarefas, visitas, ligações, automações,
             templates, remarketing, gastos, aprendiz
COMO-FUNCIONA.md  o passo a passo da operação (primeira mensagem, follow-up, nutrição, remarketing, ligações)
BLUEPRINT.md visão geral, regras da régua, lições
schema.sql   modelo de dados mínimo para começar do zero
.env.example nomes das variáveis de ambiente (sem valores)
```

## Rodando
- API: `cd crm/api && python -m venv .venv && .venv/bin/pip install -e . && cp ../../.env.example .env` e
  preencha `DATABASE_URL` e as chaves que for usar; `alembic upgrade heads`; `uvicorn app.main:app`.
- Painel: `cd crm/painel && npm install && npm run build && npm start`.
- Testes: `cd crm/api && pytest tests/ -q` (precisa de um PostgreSQL de teste).

## Avisos honestos
- Veio de um sistema vivo: tem peças específicas do negócio original (Kommo, Evolution, calendário,
  Instagram). Troque ou apague o que não usar; comece por `app/services` (régua) e `app/api`.
- Há duas migrations com o mesmo id `0092` (use `alembic upgrade heads`) — vale renumerar.
- Os nomes trocados quebram textos de prompt/persona que citavam gente de verdade; reescreva-os.
- Os arquivos `deploy/*` são do servidor original (systemd, nginx); use só de referência.
