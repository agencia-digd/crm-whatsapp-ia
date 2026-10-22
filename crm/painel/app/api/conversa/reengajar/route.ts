import { NextRequest, NextResponse } from "next/server";
import { cfg, resolveViewer } from "@/lib/config";

export const dynamic = "force-dynamic";

// Reengajamento a partir do Chat — quando o lead está FORA da janela de 24h,
// texto livre não entrega (Cloud API recusa "Re-engagement message"). Aqui
// mandamos um template UTILITY (nunca MARKETING) pela linha da Pessoa12: a saudação
// vai automática no template e o `texto` da corretora é o corpo. Backend
// (POST /painel/conversa/reengajar) resolve telefone, checa opt-out, dedupa a
// saudação e chama enviar_template_corretor. Privacidade + DRY_RUN são do copiloto.

// POST /api/conversa/reengajar  { lead_id, texto }  →  POST /painel/conversa/reengajar
export async function POST(req: NextRequest) {
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  const body = await req.json().catch(() => null);
  if (!body || !body.lead_id || !body.texto) {
    return NextResponse.json({ error: "body_invalido" }, { status: 400 });
  }
  try {
    const r = await fetch(`${cfg.copilotoUrl}/painel/conversa/reengajar`, {
      method: "POST",
      headers: {
        "X-Painel-User": viewer.user,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ lead_id: body.lead_id, texto: body.texto }),
      cache: "no-store",
    });
    const out = await r.json().catch(() => ({ error: "resposta_invalida" }));
    return NextResponse.json(out, { status: r.status });
  } catch {
    return NextResponse.json({ error: "copiloto_indisponivel" }, { status: 502 });
  }
}
