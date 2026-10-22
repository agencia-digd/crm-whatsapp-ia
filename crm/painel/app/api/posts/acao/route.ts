import { NextRequest, NextResponse } from "next/server";
import { cfg, resolveViewer } from "@/lib/config";

export const dynamic = "force-dynamic";

// Ações sobre um post — proxy pro copiloto (posts_corretora.py, :8002).
// POST /api/posts/acao { id, action, value?, motivo? }
//   action: "aprovar-copy" | "aprovar-imagem"  → body { value: bool } (toggle)
//           "recusar"                          → body { motivo } (pedir ajuste/recusar)
//           "reabrir"                          → body {} (volta recusado → rascunho)
// NÃO existe publicação por aqui: quem publica no IG é o publisher do copiloto
// quando o post está com copy E imagem aprovadas (status "aprovado" na fila).
const ACOES = new Set(["aprovar-copy", "aprovar-imagem", "recusar", "reabrir"]);

export async function POST(req: NextRequest) {
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  const { id, action, value, motivo } = await req.json().catch(() => ({}));
  if (!id || !ACOES.has(action)) {
    return NextResponse.json({ error: "parametros_invalidos" }, { status: 400 });
  }
  const payload =
    action === "recusar" ? { motivo: motivo ?? "" } : action === "reabrir" ? {} : { value: value !== false };
  try {
    const r = await fetch(
      `${cfg.copilotoUrl}/painel/posts/${encodeURIComponent(id)}/${action}`,
      {
        method: "POST",
        headers: { "X-Painel-User": viewer.user, "Content-Type": "application/json" },
        body: JSON.stringify(payload),
        cache: "no-store",
      },
    );
    const out = await r.json().catch(() => ({}));
    return NextResponse.json(out, { status: r.status });
  } catch {
    return NextResponse.json({ error: "copiloto_indisponivel" }, { status: 502 });
  }
}
