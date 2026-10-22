import { NextRequest, NextResponse } from "next/server";
import { publicOrigin } from "@/lib/public-origin";
import { COOKIE_SESSAO } from "@/lib/sessao";

export const dynamic = "force-dynamic";

// Sair de verdade — apaga a sessão e cai na tela de login.
//
// Antes da migração pra cookie (11/08/2026) isto não funcionava: quem entrava
// por usuário/senha era autenticado por HTTP Basic Auth no nginx, o navegador
// guardava a senha e reenviava sozinho, e o botão só recarregava a página.
// Basic Auth não tem logout. Agora quem manda é o cookie, então apagar basta —
// e como o nginx não pede mais Basic Auth, o navegador nem tem pra quem mandar
// a credencial velha de quem já tinha uma guardada.
//
// `corretor_as` (o "ver como" da Dona) e `painel_key` (cookie do link mágico
// antigo, lido por um map do nginx) também saem: resíduo desses não pode
// sobreviver a um logout e voltar mandando na identidade.
export async function GET(req: NextRequest) {
  const res = NextResponse.redirect(new URL("/login", publicOrigin(req)));

  for (const nome of [COOKIE_SESSAO, "corretor_as", "painel_key"]) {
    res.cookies.set(nome, "", {
      path: "/",
      maxAge: 0,
      httpOnly: true,
      secure: true,
      sameSite: "lax",
    });
  }

  return res;
}
