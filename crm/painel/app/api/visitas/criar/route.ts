import { NextRequest, NextResponse } from "next/server";
import { cfg, resolveViewer } from "@/lib/config";

export const dynamic = "force-dynamic";

// 📅 Visitas — criação: proxy pro copiloto POST /painel/visitas.
// O copiloto delega pro MESMO caminho da Assistente (criar_visita.py): evento 🟠
// no Calendar "Acme Imóveis" via Gateway + INSERT na tabela `visitas` (dedup,
// re-visita 90d, resolução de lead_id). NÃO dispara NENHUMA mensagem ao lead.
// Escopo quem resolve é o copiloto via X-Painel-User: corretora agenda só pra
// ela (corretora_id divergente → 403); admin manda corretora_id (senão 400).
// Duplicada (mesma corretora + data/hora + nome) → 409.
export async function POST(req: NextRequest) {
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  const body = await req.json().catch(() => null);
  if (!body || !body.lead_nome || !body.data || !body.hora) {
    return NextResponse.json({ error: "body_invalido" }, { status: 400 });
  }
  try {
    const r = await fetch(`${cfg.copilotoUrl}/painel/visitas`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Painel-User": viewer.user },
      body: JSON.stringify({
        lead_nome: body.lead_nome,
        data: body.data,
        hora: body.hora,
        telefone: body.telefone ?? null,
        lead_id: body.lead_id ?? null,
        empreendimento: body.empreendimento ?? null,
        observacao: body.observacao ?? null,
        corretora_id: body.corretora_id ?? null,
      }),
      cache: "no-store",
    });
    const out = await r.json().catch(() => ({ error: "resposta_invalida" }));
    return NextResponse.json(out, { status: r.status });
  } catch {
    return NextResponse.json({ error: "copiloto_indisponivel" }, { status: 502 });
  }
}
