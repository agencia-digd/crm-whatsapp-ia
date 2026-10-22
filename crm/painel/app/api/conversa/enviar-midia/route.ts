import { NextRequest, NextResponse } from "next/server";
import { cfg, resolveViewer } from "@/lib/config";

export const dynamic = "force-dynamic";

// Envia mídia (foto/PDF/documento/áudio) pro lead PELA LINHA da corretora dona
// do card — mesmo caminho do /conversa/enviar, só que multipart. O copiloto
// (POST /painel/conversa/enviar-midia) roteia Cloud (Pessoa12) x Evolution, trava
// privacidade (lead de outra corretora → 403) e respeita DRY_RUN. Limite de
// 16MB é do copiloto; o front barra antes pra dar erro amigável.
// Body (FormData): lead_id, tipo ("image"|"document"|"audio"), arquivo, legenda.
export async function POST(req: NextRequest) {
  const form = await req.formData().catch(() => null);
  if (!form || !form.get("lead_id") || !form.get("arquivo") || !form.get("tipo")) {
    return NextResponse.json({ error: "campos_obrigatorios" }, { status: 400 });
  }
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  try {
    const r = await fetch(`${cfg.copilotoUrl}/painel/conversa/enviar-midia`, {
      method: "POST",
      // sem Content-Type manual: o fetch monta o boundary do multipart sozinho
      headers: { "X-Painel-User": viewer.user },
      body: form,
      cache: "no-store",
    });
    const out = await r.json().catch(() => ({ error: "resposta_invalida" }));
    return NextResponse.json(out, { status: r.status });
  } catch {
    return NextResponse.json({ error: "copiloto_indisponivel" }, { status: 502 });
  }
}
