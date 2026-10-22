import { NextRequest, NextResponse } from "next/server";
import { cfg, resolveViewer } from "@/lib/config";

export const dynamic = "force-dynamic";

// Fila "⏳ Esperando resposta" do Chat — conversas cuja ÚLTIMA mensagem é DA
// CORRETORA (o lead ainda não respondeu). Proxy READ-ONLY do
// /painel/conversa/esperando_resposta do copiloto. É a fila INVERSA da
// "🟢 Esperando resposta" (B4, lead esperando a corretora): aqui a corretora
// cobra retorno. Ordenada pela espera mais longa primeiro; >30 dias sem
// movimento = arquivado (fora); última msg de template automático (cadência/
// reengajamento) fica fora por default. Escopo por corretora é do copiloto
// (X-Painel-User); admin vê todas e filtra ?corretora=<nome>.
export async function GET(req: NextRequest) {
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  const corretora = req.nextUrl.searchParams.get("corretora");
  const qs = corretora ? `?corretora=${encodeURIComponent(corretora)}` : "";
  try {
    const r = await fetch(
      `${cfg.copilotoUrl}/painel/conversa/esperando_resposta${qs}`,
      { headers: { "X-Painel-User": viewer.user }, cache: "no-store" },
    );
    const body = await r.json().catch(() => ({ error: "resposta_invalida" }));
    return NextResponse.json(body, { status: r.status });
  } catch {
    return NextResponse.json({ error: "copiloto_indisponivel" }, { status: 502 });
  }
}
