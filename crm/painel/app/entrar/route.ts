import { NextRequest, NextResponse } from "next/server";
import { readFile } from "node:fs/promises";
import { COOKIE_SESSAO, MAX_AGE_COOKIE, emitirSessao } from "@/lib/sessao";
import { publicOrigin } from "@/lib/public-origin";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

// Link mágico: /entrar?k=<token> → sessão pronta, sem digitar senha.
//
// Existe porque a corretora no celular tropeça no login: teclado sobe com
// maiúscula no usuário, o gerenciador de senhas atropela o campo, e o
// navegador de dentro do WhatsApp às vezes nem mostra a caixa. Um toque no
// link resolve.
//
// MIGRADO DO NGINX (11/08/2026): antes o token virava `auth_basic off` num
// `map`. Agora vira SESSÃO, igual ao login por senha — assim "Sair" também
// funciona pra quem entrou por link, e revogar é subir a geração da pessoa.
// Os tokens são OS MESMOS de antes: os links que já estão no WhatsApp delas
// continuam valendo.

const ARQUIVO_LINKS =
  process.env.CORRETOR_LINKS_FILE ?? "/etc/agencia/acme-painel-links.json";

async function usuarioDoToken(token: string): Promise<string | null> {
  if (!token) return null;
  try {
    const mapa = JSON.parse(await readFile(ARQUIVO_LINKS, "utf8")) as Record<
      string,
      string
    >;
    const user = mapa[token];
    return user ? user.toLowerCase().trim() : null;
  } catch {
    return null;
  }
}

export async function GET(req: NextRequest) {
  const origem = publicOrigin(req);
  const user = await usuarioDoToken(req.nextUrl.searchParams.get("k") ?? "");

  // Token desconhecido cai no login normal — nunca numa tela de erro que
  // confirme "esse link existe, só está errado".
  if (!user) return NextResponse.redirect(new URL("/login", origem));

  let token: string;
  try {
    token = await emitirSessao(user);
  } catch {
    return NextResponse.redirect(new URL("/login", origem));
  }

  const res = NextResponse.redirect(new URL("/crm", origem));
  res.cookies.set(COOKIE_SESSAO, token, {
    path: "/",
    maxAge: MAX_AGE_COOKIE,
    httpOnly: true,
    secure: true,
    sameSite: "lax",
  });
  return res;
}
