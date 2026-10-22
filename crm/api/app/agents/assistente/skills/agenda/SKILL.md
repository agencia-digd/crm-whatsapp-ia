---
name: acme-agenda
description: Gerencia visitas no calendário "Acme Imóveis" (contato@example.com). Cria visitas 🟠 quando uma corretora pede via WhatsApp; atualiza pra 🟢 (compareceu) ou 🔴 (no-show); aos domingos 20h confirma com cada corretora as visitas pendentes da semana passada.
---

# acme-agenda

Skill da **Assistente** pra gerenciar a agenda da Acme Imóveis.

## Mapeamento corretora ↔ telefone

| Corretora | Prefixo | Números WhatsApp |
|---|---|---|
| Pessoa1 | `[Pessoa11]` | +5500000000000 |
| Pessoa6    | `[Pessoa10]` | +5500000000000 |
| Pessoa3   | `[Pessoa]` | +5500000000000 / +5500000000000 / +5500000000000 |

**Importante:** o prefixo `[Pessoa]` no título do evento é a Pessoa Pessoa9 atuando como corretora pela conta da Pessoa3. Não é a "Pessoa" do funil interno.

## Regras de cor (Google Calendar)

| Cor | colorId | Quando usar |
|---|---|---|
| 🟠 Laranja | 6 | Visita planejada/agendada (estado inicial) |
| 🟢 Verde | 10 | Cliente compareceu |
| 🔴 Vermelho | 11 | No-show |

## Formato do evento

- **Título:** `[Corretora] Nome do Cliente`
- **Parênteses no nome:** `( )` indica algo a confirmar
- **Calendar:** `contato@example.com`
- **Duração padrão:** 1 hora (sobrescrever com `--duration`)

## Comandos

### Criar visita 🟠
```bash
python3 ~/skills/acme-agenda/criar_visita.py \
  --corretora Pessoa11 \
  --cliente "Poliana Silva" \
  --datetime "2026-04-29T14:00"
```

Cria evento laranja no calendário da Acme. Retorna `event_id` no stdout.

### Marcar status (🟢 ou 🔴)
```bash
python3 ~/skills/acme-agenda/marcar_visita.py \
  --event-id ABC123 --status compareceu

python3 ~/skills/acme-agenda/marcar_visita.py \
  --event-id ABC123 --status noshow
```

### Listar visitas pendentes (🟠 da semana passada)
```bash
python3 ~/skills/acme-agenda/listar_pendentes.py
# Retorna JSON com event_id, summary, start, attendees
```

### Confirmar visitas com as corretoras (cron domingo)
```bash
python3 ~/skills/acme-agenda/confirmar_domingo.py
# Pra cada visita 🟠 da semana passada, manda WhatsApp pra corretora
# perguntando se aconteceu. Aguarda resposta no canal Assistente.
```

## Como funciona o WhatsApp

Usa CLI nativa do OpenClaw:
```bash
openclaw message send --channel whatsapp --target +55XXXX --message "Texto"
```

A Assistente já está autenticada (creds em `/data/.openclaw/credentials/whatsapp/default/`).

## Fluxo completo

1. **Corretora manda zap pra Assistente**: "Beti, agenda visita Poliana terça 14h"
2. **Assistente delega pra Assistente** (que é IA): Assistente parseia o texto livre e chama `criar_visita.py` com os args extraídos. Se faltar dado, ela pergunta no zap antes.
3. **Assistente confirma no zap**: "✅ Marquei [Pessoa11] Poliana terça 29/04 14h"
4. **Domingo 20h** (cron): `confirmar_domingo.py` roda, pega visitas 🟠 da semana passada, manda zap pra cada corretora pedindo confirmação.
5. **Quando corretora responde**: Assistente interpreta ("apareceu" / "deu bolo") e chama `marcar_visita.py` com o status correto.

## Limites

- **Não cria visita sem cliente, data e hora** (campos obrigatórios)
- **Não duplica** visitas — se já existe evento mesmo cliente/dia, pergunta antes
- **Não confirma sozinha** o status de visita — só atualiza após resposta da corretora
- **Logs**: tudo em `/var/log/acme-agenda.log` no host (via cron)
