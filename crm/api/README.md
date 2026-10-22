# 🏠 Copiloto Imobiliário

Sistema multi-agente que analisa conversas reais de WhatsApp das corretoras, cruza com dados do Kommo e entrega coaching automático.

> **Cliente piloto:** Acme Imóveis · **Destino:** SaaS multi-tenant · **Agência:** [agencia](https://agencia)

---

## 🎯 O que faz

**De noite (20h)** manda pra Dona no Telegram um resumo do dia:
- Quantos leads entraram, quem atendeu, quem ficou parado
- Leads quentes que precisam de atenção
- Performance do time
- Padrões observados

**De manhã (8h)** manda pra cada corretora individualmente no WhatsApp:
- Briefing do que ela tem pra cuidar hoje
- Coaching baseado nas conversas de ontem ("você falou X, sugestão de Y")
- Tarefas atrasadas no Kommo
- Padrões da semana ("notei que você costuma fazer X — vamos trabalhar")

---

## 🧠 Como funciona

```
Kommo (leads + tarefas) ─┐
                         ├─→ Lead Inspector ─→ Análises ─→ Agente Dona  ─→ Telegram
Evolution (WhatsApp) ────┘                              └─→ Agente Corretora ─→ WhatsApp
```

**Lead Inspector** roda 1x ao dia (19h45) e gera o "raio-X" estruturado de cada lead com atividade. Os 2 agentes leem esses raios-X e formatam pra suas audiências.

Detalhes em [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

---

## 🛠️ Stack

- **Agno** (v2.5+) — framework de agentes com AgentOS embutido
- **Python 3.11** + **FastAPI** (vem do AgentOS)
- **Claude Haiku 4.5** (classificação rápida) + **Claude Sonnet 4.6** (análise profunda)
- **PostgreSQL 16** (Agno managed + tabelas próprias)
- **Evolution API** (WhatsApp das corretoras)
- **Kommo** (CRM, via skill `kommo-bridge`)
- **Docker Compose** (dev e prod)

---

## 🚀 Setup

### Pré-requisitos
- Docker + Docker Compose
- Python 3.11
- Poetry (`pip install poetry`)
- Bot do Telegram criado ([instruções](https://core.telegram.org/bots#botfather))
- Acesso à API do Kommo (já configurado em `kommo-bridge`)
- Servidor com Evolution API rodando (ver [`docs/EVOLUTION_SETUP.md`](docs/EVOLUTION_SETUP.md))

### Instalação

```bash
# 1. Clone o repo
git clone <url> copiloto-imobiliario
cd copiloto-imobiliario

# 2. Configure variáveis
cp .env.example .env
# edite .env com suas credenciais

# 3. Suba o Postgres
docker compose up -d postgres

# 4. Instale deps
poetry install

# 5. Rode migrations
poetry run alembic upgrade head

# 6. Popule corretoras iniciais
poetry run python scripts/seed_corretoras.py

# 7. Conecte WhatsApp das corretoras (uma vez por corretora)
poetry run python scripts/connect_whatsapp.py --corretora corretor

# 8. Suba o AgentOS
poetry run fastapi dev app/main.py
```

API estará em `http://localhost:8000`.
Control plane em [os.agno.com](https://os.agno.com) → "Connect Local".

---

## 📁 Estrutura

```
app/
├── inspector/    ← engine de análise (não é agente)
├── agents/       ← Agente Dona + Agente Corretora
├── tools/        ← Kommo, Evolution, Telegram, WhatsApp
├── workflows/    ← crons (Inspector 19h45, Dona 20h, Corretoras 8h)
├── delivery/     ← formatação de mensagens
└── db/           ← models + migrations
```

Detalhes completos em [`CLAUDE.md`](CLAUDE.md).

---

## 🧪 Modo dry-run

Durante desenvolvimento, **não envie mensagem real**. Use:

```bash
DRY_RUN=true poetry run python -m app.workflows.daily_dona
```

Vai logar a mensagem que **seria** enviada em vez de mandar.

---

## 🗺️ Roadmap

- [x] **Fase 1** — Setup do repo + estrutura base
- [ ] **Fase 2** — Tools (Kommo, Evolution, Telegram, WhatsApp)
- [ ] **Fase 3** — Lead Inspector (engine de análise)
- [ ] **Fase 4** — Agente Dona (Telegram 20h)
- [ ] **Fase 5** — Agente Corretora (WhatsApp 8h)
- [ ] **Fase 6** — Hardening + deploy VPS
- [ ] **Fase 7** — Multi-tenancy (rumo SaaS)

Roadmap detalhado em [`CLAUDE.md`](CLAUDE.md#-roadmap-por-fase).

---

## 📖 Docs

- [`CLAUDE.md`](CLAUDE.md) — briefing técnico completo (leitura obrigatória pra quem vai codar)
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — arquitetura detalhada
- [`docs/AGENTS.md`](docs/AGENTS.md) — prompts e personas dos agentes
- [`docs/EVOLUTION_SETUP.md`](docs/EVOLUTION_SETUP.md) — como instalar e configurar Evolution API
- [`docs/DEPLOY.md`](docs/DEPLOY.md) — deploy na VPS

---

## 🔒 Privacidade (regra de ouro)

**Corretora NUNCA vê dado de outra corretora.**
Todo filtro no banco é por `corretora_id`. Todo briefing é gerado por corretora individualmente. Sem exceção.

---

## 📞 Contato

agencia — [agencia](https://agencia)
