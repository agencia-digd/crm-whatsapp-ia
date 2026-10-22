import { NextRequest, NextResponse } from "next/server";
import { cfg, resolveViewer } from "@/lib/config";

export const dynamic = "force-dynamic";

// POST /api/conversa/lida { telefone, ate? } → POST /painel/conversa/lida
// Guarda "lido até" da conversa SÓ pra quem está logado (pacote A do Chat,
// 16/10): o número de não lidas sobrevive ao recarregar. Não toca em mensagem
// nem em lead; a Dona abrir a conversa da Pessoa12 não zera o contador da Pessoa12.
export async function POST(req: NextRequest) {
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  const body = await req.json().catch(() => null);
  if (!body || typeof body.telefone !== "string" || !body.telefone.trim()) {
    return NextResponse.json({ error: "body_invalido" }, { status: 400 });
  }
  const payload: { telefone: string; ate?: string } = { telefone: body.telefone };
  if (typeof body.ate === "string" && body.ate) payload.ate = body.ate;
  try {
    const r = await fetch(`${cfg.copilotoUrl}/painel/conversa/lida`, {
      method: "POST",
      headers: { "X-Painel-User": viewer.user, "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      cache: "no-store",
    });
    const out = await r.json().catch(() => ({ error: "resposta_invalida" }));
    return NextResponse.json(out, { status: r.status });
  } catch {
    return NextResponse.json({ error: "copiloto_indisponivel" }, { status: 502 });
  }
}
