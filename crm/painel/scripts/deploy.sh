#!/bin/bash
# Deploy do painel Acme V2 SEM derrubar quem está usando.
#
# Uso:  ./scripts/deploy.sh
#
# 🔴 Por que este script existe (apagão 29/07, meio do expediente):
# `next build` no diretório de produção APAGA o `.next` que o `next start` está
# servindo. O processo morre na hora, o systemd reinicia, morre de novo
# ("Could not find a production build in the '.next' directory") — crash loop
# até o build terminar. As corretoras tomaram 502 no painel inteiro.
#
# Aqui o build vai pra `.next-build` (distDir por env, ver next.config.mjs), o
# painel VELHO continua servindo o tempo todo, e a troca é um `mv` (instantâneo)
# seguido de um restart de ~1s. Build quebrado = nada é trocado, painel intacto.
set -euo pipefail

cd "$(dirname "$0")/.."
RAIZ="$PWD"
NOVO="$RAIZ/.next-build"
VIVO="$RAIZ/.next"
ANTIGO="$RAIZ/.next-anterior"

echo "── build em .next-build (o painel no ar não é tocado)…"
rm -rf "$NOVO"
NEXT_DIST_DIR=.next-build npm run build

# Sanidade: build sem BUILD_ID não serve — aborta ANTES de mexer no que está no ar.
if [[ ! -f "$NOVO/BUILD_ID" ]]; then
  echo "ERRO: build não gerou BUILD_ID. Nada trocado — painel segue no ar." >&2
  exit 1
fi

echo "── troca atômica + restart…"
rm -rf "$ANTIGO"
[[ -d "$VIVO" ]] && mv "$VIVO" "$ANTIGO"
mv "$NOVO" "$VIVO"
systemctl restart acme-painel

# Espera o painel responder de verdade antes de cantar vitória.
for _ in $(seq 1 20); do
  if [[ "$(curl -s -o /dev/null -m 3 -w '%{http_code}' http://127.0.0.1:3009/crm)" == "200" ]]; then
    echo "✅ no ar — build $(cat "$VIVO/BUILD_ID")"
    exit 0
  fi
  sleep 1
done

echo "⚠️  painel não respondeu 200 em 20s. Rollback:" >&2
echo "    rm -rf $VIVO && mv $ANTIGO $VIVO && systemctl restart acme-painel" >&2
exit 1
