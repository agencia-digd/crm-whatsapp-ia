import { NextRequest, NextResponse } from "next/server";
import { COOKIE_SESSAO, usuarioDaSessao } from "@/lib/sessao";

export const dynamic = "force-dynamic";
export const runtime = "nodejs"; // usa node:crypto e lê /etc — não pode ser edge

// Porteiro do painel. Quem chama é o NGINX, a cada request, via `auth_request`
// (ver /etc/nginx/sites-enabled/acme-painel). Não é rota de navegação.
//
// 204 = pode entrar; o nginx copia o X-Auth-User da resposta e repassa pro app.
// 401 = manda pro /login.
//
// Fail-closed de propósito: qualquer erro aqui vira 401. Se este endpoint
// quebrar, o painel FECHA — nunca abre.
export async function GET(req: NextRequest) {
  try {
    const user = await usuarioDaSessao(req.cookies.get(COOKIE_SESSAO)?.value);
    if (!user) return new NextResponse(null, { status: 401 });
    return new NextResponse(null, {
      status: 204,
      headers: { "X-Auth-User": user },
    });
  } catch {
    return new NextResponse(null, { status: 401 });
  }
}
