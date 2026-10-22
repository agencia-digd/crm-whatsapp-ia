import { NextRequest, NextResponse } from "next/server";
import { publicOrigin } from "@/lib/public-origin";

export const dynamic = "force-dynamic";

// Link de 1 toque pra Dona virar admin mesmo com cookie de corretora
// grudado no navegador (link mágico da Pessoa10/Pessoa11/Pessoa12).
// URL: /como/dona?k=<segredo>
const SECRET = process.env.DONA_SWITCH_KEY || "";

export async function GET(req: NextRequest) {
  const k = req.nextUrl.searchParams.get("k") || "";
  if (!SECRET || k !== SECRET) {
    return new NextResponse("link inválido", { status: 403 });
  }

  const dest = new URL("/chat", publicOrigin(req));
  const res = NextResponse.redirect(dest);
  // App-level override (lido no middleware → X-Auth-User=dona)
  res.cookies.set("corretor_as", "dona", {
    path: "/",
    maxAge: 60 * 60 * 24 * 365,
    httpOnly: true,
    secure: true,
    sameSite: "lax",
  });
  // Tenta limpar o cookie do nginx também (mesmo path/domínio)
  res.cookies.set("painel_key", "", {
    path: "/",
    maxAge: 0,
    httpOnly: true,
    secure: true,
    sameSite: "lax",
  });
  return res;
}
