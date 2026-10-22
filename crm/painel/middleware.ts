import { NextRequest, NextResponse } from "next/server";

// O cookie `corretor_as` deixa a Dona trocar de conta sem limpar cookie na mão
// (/como/dona, /sair). A identidade REAL vem do nginx no header X-Auth-User:
// basic auth (htpasswd) ou o link mágico da corretora.
//
// 🔴 ESCALADA DE PRIVILÉGIO corrigida em 29/07 (auditoria Sol).
// Antes, qualquer valor da lista virava identidade — inclusive "dona", que é
// ADMIN. Cookie é 100% controlado pelo navegador: uma corretora com o link
// dela fazia `document.cookie="corretor_as=dona"` e passava a ver o dado das outras
// (regra dura do CLAUDE.md), abrir /automacoes e a fila /sugestoes — que
// aprova mensagem que vai pro cliente. Reproduzido: header `corretor` + cookie
// `dona` devolvia 200.
//
// O fail-closed do `resolveViewer` não pegava isso: o header chegava já
// preenchido com "dona", então a checagem nem era exercitada.
//
// REGRA AGORA: o cookie só REBAIXA, nunca eleva. Quem já é admin pelo header
// pode se ver como corretora (o caso de uso real da Dona); quem não é admin
// não vira admin por cookie nenhum.
const ADMINS = new Set(["admin"]);
const CORRETORAS = new Set(["corretor", "corretor", "corretor"]);
const ALLOWED = new Set(["admin", "corretor", "corretor", "corretor"]);

export function middleware(req: NextRequest) {
  const as = (req.cookies.get("corretor_as")?.value || "").toLowerCase().trim();
  const real = (req.headers.get("x-auth-user") || "").toLowerCase().trim();
  const realEhAdmin = ADMINS.has(real);
  let efetivo = real;

  // Só admin troca de visão pelo cookie. Até 02/10/2026 o único caso barrado era
  // não-admin virar admin — uma corretora trocava `corretor_as` para o nome de outra
  // e via os leads dela. Não-admin fica sempre na própria identidade.
  if (as && ALLOWED.has(as) && realEhAdmin) efetivo = as;

  if (efetivo === real) return NextResponse.next();

  const headers = new Headers(req.headers);
  headers.set("x-auth-user", efetivo);
  return NextResponse.next({ request: { headers } });
}

export const config = {
  // tudo menos assets estáticos do Next
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
