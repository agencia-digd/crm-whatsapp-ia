import { NextRequest, NextResponse } from "next/server";
import { cfg, resolveViewer } from "@/lib/config";

export const dynamic = "force-dynamic";

// Lista de conversas do Chat — proxy do /painel/kanban do copiloto (snapshot
// Kommo + última atividade WhatsApp por lead). O front achata as colunas e
// ordena por recência. Privacidade é do copiloto (X-Painel-User): corretora
// só recebe os leads dela; admin vê tudo e pode filtrar ?corretora=<nome>.
export async function GET(req: NextRequest) {
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  const corretora = req.nextUrl.searchParams.get("corretora");
  // incluir_fora_funil: chat mostra TODA conversa do celular, com ou sem lead
  // no funil (pedido Dona 24/07) — o copiloto anexa a coluna "Fora do funil".
  const qs = new URLSearchParams({ ordem: "recentes", incluir_fora_funil: "1" });
  if (corretora) qs.set("corretora", corretora);
  try {
    const r = await fetch(`${cfg.copilotoUrl}/painel/kanban?${qs.toString()}`, {
      headers: { "X-Painel-User": viewer.user },
      cache: "no-store",
    });
    const body = await r.json().catch(() => ({ error: "resposta_invalida" }));
    return NextResponse.json(body, { status: r.status });
  } catch {
    return NextResponse.json({ error: "copiloto_indisponivel" }, { status: 502 });
  }
}
