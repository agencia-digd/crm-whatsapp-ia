import { NextRequest, NextResponse } from "next/server";
import { cfg, resolveViewer } from "@/lib/config";

export const dynamic = "force-dynamic";

// Assistente busca imóvel de TERCEIROS na web (fora da carteira Acme).
// Proxy pro copiloto POST /painel/assistente/buscar-imovel, que por sua vez chama
// o motor GRÁTIS de busca (Playwright/Chromium no Mac mini via Vpn).
// SUGESTÃO pra corretora — nunca envia nada pro lead.
//
// Busca real é lenta (abre Chromium, raspa 2 portais): timeout generoso.
export async function POST(req: NextRequest) {
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  let texto = "";
  try {
    const body = await req.json();
    texto = (body?.texto || "").toString().trim();
  } catch {
    return NextResponse.json({ opcoes: [], erro: "pedido_invalido" }, { status: 400 });
  }
  if (texto.length < 4) {
    return NextResponse.json({
      opcoes: [],
      erro: "me diz o que o lead procura — ex: 'apto 3 quartos no Bairro A até 500 mil'",
      pedido: texto,
    });
  }

  const ac = new AbortController();
  const t = setTimeout(() => ac.abort(), 100_000);
  try {
    const r = await fetch(`${cfg.copilotoUrl}/painel/assistente/buscar-imovel`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Painel-User": viewer.user,
      },
      body: JSON.stringify({ texto }),
      cache: "no-store",
      signal: ac.signal,
    });
    const body = await r.json().catch(() => ({ opcoes: [], erro: "resposta_invalida" }));
    return NextResponse.json(body, { status: r.status });
  } catch (e) {
    const aborted = e instanceof Error && e.name === "AbortError";
    return NextResponse.json(
      {
        opcoes: [],
        erro: aborted
          ? "a busca demorou demais — tenta de novo"
          : "copiloto indisponível — tenta de novo",
      },
      { status: 502 },
    );
  } finally {
    clearTimeout(t);
  }
}
