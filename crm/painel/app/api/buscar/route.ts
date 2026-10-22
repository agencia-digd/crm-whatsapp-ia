import { NextRequest, NextResponse } from "next/server";
import { cfg, resolveViewer } from "@/lib/config";

export const dynamic = "force-dynamic";

// Busca lead por NOME ou TELEFONE em TODOS os leads (não só os visíveis no
// board) — padrão da busca do painel antigo, agora no cabeçalho do CRM.
// Proxy pro copiloto /painel/atendimento/busca: 4+ dígitos → sufixo do
// telefone (robusto a +55/9º dígito), senão ILIKE no nome. Escopo/privacidade
// é o copiloto quem resolve via X-Painel-User (corretora só acha leads dela).
export async function GET(req: NextRequest) {
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  const q = (req.nextUrl.searchParams.get("q") || "").trim();
  if (q.length < 2) return NextResponse.json({ leads: [] });
  try {
    const r = await fetch(
      `${cfg.copilotoUrl}/painel/atendimento/busca?q=${encodeURIComponent(q)}`,
      { headers: { "X-Painel-User": viewer.user }, cache: "no-store" },
    );
    const body = await r.json().catch(() => ({ error: "resposta_invalida" }));
    return NextResponse.json(body, { status: r.status });
  } catch {
    return NextResponse.json({ leads: [], error: "copiloto_indisponivel" }, { status: 502 });
  }
}
