import { NextRequest, NextResponse } from "next/server";
import { cfg, resolveViewer } from "@/lib/config";

export const dynamic = "force-dynamic";

// POST /api/lead/contato  { lead_id, telefone }  →  POST /painel/lead/{id}/contato
// Corrige o telefone do lead SÓ no nosso banco (nunca no Kommo). O copiloto
// normaliza/valida (BR E.164) e faz escopo via X-Painel-User: corretora só edita
// lead dela; admin qualquer. Telefone inválido → 422 (repassado ao cliente).
// Efeito: a thread do WhatsApp resolve por telefone — corrigir religa o lead à
// conversa real do número novo.
export async function POST(req: NextRequest) {
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  const body = await req.json().catch(() => null);
  if (!body || !body.lead_id || !body.telefone) {
    return NextResponse.json({ error: "body_invalido" }, { status: 400 });
  }
  try {
    const r = await fetch(
      `${cfg.copilotoUrl}/painel/lead/${encodeURIComponent(body.lead_id)}/contato`,
      {
        method: "POST",
        headers: {
          "X-Painel-User": viewer.user,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ telefone: body.telefone }),
        cache: "no-store",
      },
    );
    const out = await r.json().catch(() => ({ error: "resposta_invalida" }));
    return NextResponse.json(out, { status: r.status });
  } catch {
    return NextResponse.json({ error: "copiloto_indisponivel" }, { status: 502 });
  }
}
