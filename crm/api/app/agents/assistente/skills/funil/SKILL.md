---
name: acme-funil
description: Acessa a planilha "Acme Imóveis · planejamento" — leitura, resumo do funil semanal, registro de ligações na aba Remarketing e adição de notas/observações. Use quando a Dona ou Assistente pedir status do funil, registrar ligações de uma corretora, ou pra adicionar observações pontuais.
---

# acme-funil

Conjunto de scripts pra a **Assistente** acessar a planilha de planejamento da Acme.

## O que dá pra fazer

```bash
# Registra UMA venda na aba "Funil" manual — soma +1 venda e o valor no VGV
# do mês corrente, atualiza o Total geral também.
python3 ~/skills/acme-funil/registrar_venda.py \
  --corretora Pessoa11 --cliente "Lurdes" --valor 300000 --preview   # só calcula
python3 ~/skills/acme-funil/registrar_venda.py \
  --corretora Pessoa11 --cliente "Lurdes" --valor 300000             # aplica

# Registra ligações de uma corretora numa semana (aba Remarketing)
python3 ~/skills/acme-funil/registrar_ligacoes.py \
  --corretora Pessoa11 --ligacoes 87 --atenderam 14 --prosseguiram 8
# Default: semana corrente do mês corrente. Override: --week 3 --month 2026-04
# Recalcula a linha "Total" automaticamente.

# Resumo do funil semanal: vê os totais do mês corrente e por corretora
python3 ~/skills/acme-funil/summary.py

# Snapshot de um mês específico
python3 ~/skills/acme-funil/summary.py --month 2026-04

# Adiciona uma observação numa célula (ex: comentário sobre vendas de abril)
python3 ~/skills/acme-funil/add_note.py --row 11 --col 2 --text "Maio: cobrar atualização de price na Kommo"

# Lista todas as abas e suas dimensões
python3 ~/skills/acme-funil/inspect.py
```

## O que NÃO fazer aqui

- **Não rodar** atualização completa Kommo→Sheets aqui — isso é trabalho do robô em `/opt/acme-dash-api/update.py` (toda segunda 8h, no host). Se precisar disparar manualmente, pedir pra Assistente.
- **Não sobrescrever** linhas que a Dona ou as corretoras preencheram à mão sem avisar.
- **Não tocar** na aba "Funil" original (manual histórica) — só na aba "Funil Semanal" (gerada pelo robô).

## Credenciais

Service account em `~/.acme-sheets.json` (workspace).
Sheet ID: `SPREADSHEET_ID`

## IDs e nomes importantes

```
Pipeline "Funil de Vendas" Kommo: 10000004

Corretoras (Kommo):
  Pessoa6    10000006
  Pessoa1 10000007
  Pessoa3   10000003  (= "Pessoa" na aba Funil manual)
```
