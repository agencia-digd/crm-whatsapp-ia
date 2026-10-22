#!/usr/bin/env node
// Derruba TODAS as sessões de uma pessoa — celular perdido, corretora que saiu,
// suspeita de link vazado.
//
//   node scripts/revogar-sessao.mjs corretor
//
// Como funciona: cada usuário tem uma "geração" e o cookie carrega a dele.
// Subir a geração invalida todo cookie antigo daquela pessoa na hora, sem
// mexer em ninguém mais e sem reiniciar o serviço (o arquivo é lido a cada
// verificação). Pra derrubar TODO MUNDO, troque CORRETOR_SESSION_SECRET em
// /etc/agencia/acme-painel-auth.env e reinicie o acme-painel.
//
// O link mágico da pessoa continua valendo depois de revogar — ele emite uma
// sessão nova, já na geração atual. Se o problema FOR o link, tire o token de
// /etc/agencia/acme-painel-links.json antes.

import { readFileSync, writeFileSync, chmodSync } from "node:fs";

const ARQUIVO = process.env.CORRETOR_GERACOES_FILE ?? "/etc/agencia/acme-painel-geracoes.json";
const user = (process.argv[2] ?? "").toLowerCase().trim();

if (!user) {
  console.error("uso: node scripts/revogar-sessao.mjs <usuário>");
  process.exit(1);
}

let mapa = {};
try {
  mapa = JSON.parse(readFileSync(ARQUIVO, "utf8"));
} catch {
  // arquivo ausente na primeira revogação: começa vazio
}

const antes = Number(mapa[user] ?? 0) || 0;
mapa[user] = antes + 1;

writeFileSync(ARQUIVO, `${JSON.stringify(mapa, null, 2)}\n`);
chmodSync(ARQUIVO, 0o600);

console.log(`sessões de "${user}" revogadas (geração ${antes} → ${antes + 1})`);
console.log("ela precisa entrar de novo — pelo login ou pelo link.");
