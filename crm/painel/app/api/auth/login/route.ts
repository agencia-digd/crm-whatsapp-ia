import { NextRequest, NextResponse } from "next/server";
import { COOKIE_SESSAO, MAX_AGE_COOKIE, emitirSessao } from "@/lib/sessao";
import { conferirSenha } from "@/lib/senha";
import { publicOrigin } from "@/lib/public-origin";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

// Login: confere no htpasswd e emite a sessão.
//
// ACEITA FORM HTML (application/x-www-form-urlencoded), não só JSON — e isso é
// de propósito. Se a tela de login precisasse do JavaScript do Next, o bundle
// (/_next/*) teria que ser público pra ela carregar, e aí o código do painel
// ficaria aberto pra qualquer um. Com <form> nativo, o único endereço público
// é a própria página. Quem manda JSON (fetch) continua funcionando.
//
// Usuário em minúsculas: o htpasswd tem "Pessoa10", "CORRETOR", "Pessoa11", "CORRETOR"
// cadastrados só pra contornar a maiúscula automática do teclado do celular.
// Normalizando aqui, o resto do app (resolveViewer, PAINEL_ADMINS,
// USER_CORRETORA) recebe sempre a forma canônica — que é como ele já esperava.

/** Só caminho interno: "//evil.com" e "https://evil.com" viram null. */
function destinoSeguro(bruto: string | null): string {
  if (!bruto || !bruto.startsWith("/") || bruto.startsWith("//")) return "/crm";
  return bruto;
}

export async function POST(req: NextRequest) {
  const tipo = req.headers.get("content-type") ?? "";
  const querJson = tipo.includes("application/json");

  let user = "";
  let senha = "";
  let de = "/crm";

  if (querJson) {
    const body = await req.json().catch(() => null);
    user = String(body?.usuario ?? "").toLowerCase().trim();
    senha = String(body?.senha ?? "");
    de = destinoSeguro(String(body?.de ?? "") || null);
  } else {
    const form = await req.formData().catch(() => null);
    user = String(form?.get("usuario") ?? "").toLowerCase().trim();
    senha = String(form?.get("senha") ?? "");
    de = destinoSeguro(String(form?.get("de") ?? "") || null);
  }

  const origem = publicOrigin(req);

  const falhou = (status: number, msg: string) => {
    if (querJson) return NextResponse.json({ erro: msg }, { status });
    // Form: volta pro login com o aviso. 303 pra o navegador trocar POST por
    // GET — senão o "atualizar página" reenvia a senha.
    const url = new URL("/login", origem);
    url.searchParams.set("erro", "1");
    if (de !== "/crm") url.searchParams.set("de", de);
    return NextResponse.redirect(url, 303);
  };

  if (!user || !senha) return falhou(400, "faltou usuário ou senha");

  // Mensagem única pros dois casos: dizer "usuário não existe" entrega a lista
  // de logins pra quem estiver testando.
  if (!(await conferirSenha(user, senha))) {
    return falhou(401, "usuário ou senha inválidos");
  }

  let token: string;
  try {
    token = await emitirSessao(user);
  } catch {
    // Segredo ausente/curto: falhar visível é melhor que emitir cookie fraco.
    return falhou(500, "sessão indisponível");
  }

  const res = querJson
    ? NextResponse.json({ ok: true, usuario: user, de })
    : NextResponse.redirect(new URL(de, origem), 303);

  res.cookies.set(COOKIE_SESSAO, token, {
    path: "/",
    maxAge: MAX_AGE_COOKIE,
    httpOnly: true,
    secure: true,
    sameSite: "lax",
  });
  return res;
}
