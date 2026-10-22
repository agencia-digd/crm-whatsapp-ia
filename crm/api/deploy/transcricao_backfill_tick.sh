#!/bin/bash
# Backfill de transcrição em GOTAS, não em corrida.
#
# Por que gotejar: em 25/08/2026 rodei o backfill com 12 workers, estourei o
# limite de requisições da app na Graph ("(#4) Application request limit
# reached") e 1.275 áudios foram gravados como "mídia expirada" — quando só
# precisavam esperar. O limite é por janela de tempo: quem corre, para.
#
# Lote pequeno + poucos workers + intervalo do timer = a cota se recompõe entre
# um tick e outro. Se ainda assim bater no teto, o áudio volta pra fila como
# `pendente` (transcricao_service.RATE_LIMIT) e é tentado no tick seguinte —
# a fila se auto-cura, ninguém precisa reprocessar na mão.
#
# Ordem DESC de propósito: o mês corrente primeiro. Mídia da Cloud API expira,
# então áudio recente é o que ainda dá pra salvar — e é o que a Dona usa pra
# decidir alguma coisa. Áudio de março já era.
set -u

PROJ="/opt/crm"
cd "$PROJ" || exit 1

LOTE="${TRANSCRICAO_LOTE:-90}"
WORKERS="${TRANSCRICAO_WORKERS:-2}"
export BACKFILL_SEM_MEMORIA=1   # sem gravar memória por lote (ruído caro)

# BACKOFF: se o lote anterior não transcreveu NADA, a cota da Graph ainda está
# fechada — insistir no mesmo ritmo só mantém o limite saturado e não recupera
# nada. Cada ciclo seco dobra a espera (1, 2, 4… até 8 ciclos = 4h), e um lote
# com qualquer sucesso zera o contador.
ESTADO_BACKOFF="/var/lib/assistente/transcricao-backoff"
mkdir -p "$(dirname "$ESTADO_BACKOFF")"
PULAR=$(cat "$ESTADO_BACKOFF" 2>/dev/null || echo 0)
if [[ "$PULAR" -gt 0 ]]; then
  echo "[$(date -Iseconds)] backoff ativo — pulando este ciclo (faltam $PULAR)"
  echo $((PULAR - 1)) > "$ESTADO_BACKOFF"
  exit 0
fi

# Nada a fazer? Sai quieto — não polui o journal a cada 15 min.
#
# A janela aqui tem que ser A MESMA do backfill (TRANSCRICAO_JANELA_DIAS). Ela
# já foi "mês corrente e anterior", e o resultado era um tick que anunciava
# "fila: 2702" e logo depois "Nada a processar" — contava áudio que o Python
# nem consulta. Foi esse número inflado que fez o alerta gritar "transcrição
# parou" em 28/08/2026 com 100% dos áudios recentes transcritos.
JANELA_DIAS="${TRANSCRICAO_JANELA_DIAS:-10}"
PENDENTES=$(PGPASSWORD=CHANGE_ME psql -h localhost -U copiloto -d copiloto -At -c \
  "SELECT count(*) FROM mensagens_whatsapp
    WHERE tipo_mensagem='audio' AND transcricao_status IN ('pendente','erro')
      AND timestamp_evolution >= now() - interval '$JANELA_DIAS days';" 2>/dev/null)

if [[ -z "$PENDENTES" || "$PENDENTES" -eq 0 ]]; then
  echo "[$(date -Iseconds)] fila vazia (últimos $JANELA_DIAS dias) — nada a fazer."
  exit 0
fi

echo "[$(date -Iseconds)] fila: $PENDENTES · processando lote de $LOTE com $WORKERS worker(s)"

rm -f /tmp/backfill_transcricoes.pid
# O summary é a ÚNICA fonte pro backoff. Se o Python sair sem escrever (ex.:
# "Nada a processar"), sem apagar antes a gente lê o resultado do lote passado e
# o journal passa a mentir — ficou 8h repetindo "8 transcritos" sem transcrever
# nada em 28/08/2026. Apagar transforma "não escreveu" em ausência detectável.
rm -f /tmp/backfill_summary_latest.json
timeout 840 .venv/bin/python scripts/backfill_transcricoes.py \
  --include-errors --limit "$LOTE" --workers "$WORKERS" --order DESC 2>&1 | tail -25

# Sem summary = o Python não processou lote nenhum. Não é "lote seco" (que é
# sintoma de cota fechada e merece backoff) — é simplesmente não ter trabalho na
# janela. Sai sem mexer no backoff.
if [[ ! -f /tmp/backfill_summary_latest.json ]]; then
  echo "[$(date -Iseconds)] nenhum lote processado (nada na janela de $JANELA_DIAS dias) — backoff intocado"
  exit 0
fi

# Cota do app fechada é outra categoria de espera. "Lote seco" pode ser azar do
# lote; "(#4) Application request limit reached" é a Meta dizendo que o app
# gastou a janela dele, e essa janela é de HORAS, não de minutos. Com o backoff
# curto (8 ciclos = 40 min) a gente voltava a bater cedo demais e ressaturava o
# limite — a fila ficou parada em 2.700 áudios o dia 25/08/2026 inteiro assim.
# Aqui a espera vai direto pra 3h, sem dobrar de 5 em 5 minutos.
CICLOS_RATE_LIMIT=36   # 36 × 5 min = 3h
if python3 -c "import json,sys;sys.exit(0 if json.load(open('/tmp/backfill_summary_latest.json')).get('abortou_por_rate_limit') else 1)" 2>/dev/null; then
  echo "$CICLOS_RATE_LIMIT" > "$ESTADO_BACKOFF"; echo "$CICLOS_RATE_LIMIT" > "${ESTADO_BACKOFF}.n"
  echo "[$(date -Iseconds)] cota do app da Meta fechada — esperando 3h antes de tentar de novo"
  exit 0
fi

# Lote seco (nada concluído) → dobra a espera. Qualquer sucesso zera.
OK=$(python3 -c "import json;print(json.load(open('/tmp/backfill_summary_latest.json'))['n_concluida'])" 2>/dev/null || echo 0)
CICLOS=$(cat "${ESTADO_BACKOFF}.n" 2>/dev/null || echo 0)
if [[ "$OK" -eq 0 ]]; then
  CICLOS=$(( CICLOS == 0 ? 1 : CICLOS * 2 ))
  [[ "$CICLOS" -gt 8 ]] && CICLOS=8
  echo "$CICLOS" > "$ESTADO_BACKOFF"; echo "$CICLOS" > "${ESTADO_BACKOFF}.n"
  echo "[$(date -Iseconds)] lote seco — esperando $CICLOS ciclo(s) antes de tentar de novo"
else
  echo 0 > "$ESTADO_BACKOFF"; echo 0 > "${ESTADO_BACKOFF}.n"
  echo "[$(date -Iseconds)] $OK transcritos — backoff zerado"
fi
