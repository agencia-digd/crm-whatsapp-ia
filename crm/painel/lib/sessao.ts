// Sessão em cookie assinado — substitui o HTTP Basic Auth do nginx (11/08/2026).
//
// POR QUE MIGROU: Basic Auth não tem logout. O browser guarda a senha e
// reenvia sozinha; "Sair/trocar conta" recarregava sem desconectar (ver
// README). Com sessão em cookie, sair é apagar o cookie.
//
// O NGINX CONTINUA SENDO O PORTEIRO. A validação roda aqui, mas quem barra é
// o nginx via `auth_request` → /api/auth/verificar. Se este app cair, o nginx
// nega tudo (fail-closed). Mover a decisão pro middleware do Next deixaria o
// painel ABERTO num erro de matcher — dado da imobiliária não corre esse risco.
//
// Formato: base64url(payload).base64url(hmac_sha256(payload))
// payload = {"u": usuário, "g": geração, "t": emitido_em}
//
// NÃO EXPIRA por decisão da Dona (11/08): a corretora vive no celular e
// relogar toda semana foi o atrito que criou o link mágico. Em troca existe
// REVOGAÇÃO: cada usuário tem uma "geração" em /etc/agencia/acme-painel-geracoes.json
// e o token carrega a dele. Subir a geração derruba as sessões daquela pessoa
// (celular perdido, corretora que saiu) sem mexer em ninguém mais:
//
//     node scripts/revogar-sessao.mjs corretor
//
// Trocar CORRETOR_SESSION_SECRET derruba TODO MUNDO de uma vez.

import { createHmac, timingSafeEqual } from "node:crypto";
import { readFile } from "node:fs/promises";

export const COOKIE_SESSAO = "corretor_sess";

const ARQUIVO_GERACOES =
  process.env.CORRETOR_GERACOES_FILE ?? "/etc/agencia/acme-painel-geracoes.json";

// 1 ano. Não é validade da sessão — é só pra o cookie sobreviver ao fechar o
// navegador (cookie sem Max-Age morre na hora, o oposto do que foi pedido).
export const MAX_AGE_COOKIE = 365 * 24 * 60 * 60;

type Payload = { u: string; g: number; t: number };

function segredo(): string {
  const s = process.env.CORRETOR_SESSION_SECRET;
  // Sem segredo NÃO se emite nem se aceita sessão. Cair aqui é melhor que
  // assinar com string vazia — que faria qualquer um forjar um cookie de admin.
  if (!s || s.length < 32) {
    throw new Error("CORRETOR_SESSION_SECRET ausente ou curto demais");
  }
  return s;
}

const b64url = (b: Buffer) => b.toString("base64url");

function assinar(corpo: string): string {
  return b64url(createHmac("sha256", segredo()).update(corpo).digest());
}

/** Geração atual do usuário (0 quando nunca foi revogado). */
export async function geracaoAtual(user: string): Promise<number> {
  try {
    const bruto = await readFile(ARQUIVO_GERACOES, "utf8");
    const mapa = JSON.parse(bruto) as Record<string, number>;
    return Number(mapa[user] ?? 0) || 0;
  } catch {
    // Arquivo ausente/ilegível = ninguém foi revogado. Não pode barrar login:
    // um erro de permissão trancaria a equipe inteira fora do painel.
    return 0;
  }
}

export async function emitirSessao(user: string): Promise<string> {
  const payload: Payload = {
    u: user,
    g: await geracaoAtual(user),
    t: Math.floor(Date.now() / 1000),
  };
  const corpo = b64url(Buffer.from(JSON.stringify(payload)));
  return `${corpo}.${assinar(corpo)}`;
}

/** Usuário do cookie, ou null se inválido/forjado/revogado. */
export async function usuarioDaSessao(
  token: string | undefined | null,
): Promise<string | null> {
  if (!token) return null;
  const [corpo, mac] = token.split(".");
  if (!corpo || !mac) return null;

  let esperado: string;
  try {
    esperado = assinar(corpo);
  } catch {
    return null; // sem segredo configurado: nega
  }

  // timingSafeEqual exige mesmo tamanho — compara antes pra não lançar.
  const a = Buffer.from(mac);
  const b = Buffer.from(esperado);
  if (a.length !== b.length || !timingSafeEqual(a, b)) return null;

  let payload: Payload;
  try {
    payload = JSON.parse(Buffer.from(corpo, "base64url").toString("utf8"));
  } catch {
    return null;
  }

  const user = (payload?.u || "").toLowerCase().trim();
  if (!user) return null;

  // Revogação: token de geração antiga não vale mais.
  if (Number(payload.g ?? 0) !== (await geracaoAtual(user))) return null;

  return user;
}
