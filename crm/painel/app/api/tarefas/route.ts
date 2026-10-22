import { NextRequest, NextResponse } from "next/server";
import { cfg, resolveViewer } from "@/lib/config";

export const dynamic = "force-dynamic";

// GET /api/tarefas?escopo=hoje  →  GET /painel/tarefas?escopo=hoje
// Retorna { tarefas: [ { id, lead_id, lead_nome, corretora, data, hora, texto, status, atrasada } ] }
export async function GET(req: NextRequest) {
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  const escopo = req.nextUrl.searchParams.get("escopo") ?? "hoje";
  // ?corretora= só vale pra admin — o copiloto ignora quando é corretora.
  const corr = req.nextUrl.searchParams.get("corretora");
  const qsCorr = corr ? `&corretora=${encodeURIComponent(corr)}` : "";
  try {
    const r = await fetch(
      `${cfg.copilotoUrl}/painel/tarefas?escopo=${encodeURIComponent(escopo)}${qsCorr}`,
      {
        headers: { "X-Painel-User": viewer.user },
        cache: "no-store",
      },
    );
    const body = await r.json().catch(() => ({ error: "resposta_invalida" }));
    // viewer_admin: a tela usa pra SEMPRE mostrar de quem é a tarefa no modo
    // gestão (antes inferia por >1 corretora distinta — com tarefas de uma só,
    // o nome sumia; pedido da Dona 27/07).
    const payload = body && typeof body === "object" && !Array.isArray(body)
      ? { ...body, viewer_admin: viewer.isAdmin }
      : body;
    return NextResponse.json(payload, { status: r.status });
  } catch {
    return NextResponse.json({ error: "copiloto_indisponivel" }, { status: 502 });
  }
}

// POST /api/tarefas  { lead_id, data, texto, lead_nome?, hora? }  (hora "HH:MM" opcional)
//   →  POST /painel/lead/{id}/tarefa  (tabela tarefas_corretora, migration 0040)
// Criada pelo modal "criar tarefa" na thread do Chat; listada pelo GET acima
// quando data <= hoje. Owner: corretora do lead (copiloto resolve).
export async function POST(req: NextRequest) {
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  const body = await req.json().catch(() => null);
  if (!body || !body.lead_id || !body.data || !body.texto) {
    return NextResponse.json({ error: "body_invalido" }, { status: 400 });
  }
  try {
    const r = await fetch(
      `${cfg.copilotoUrl}/painel/lead/${encodeURIComponent(body.lead_id)}/tarefa`,
      {
        method: "POST",
        headers: {
          "X-Painel-User": viewer.user,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          data: body.data,
          hora: body.hora ?? null,
          texto: body.texto,
          lead_nome: body.lead_nome ?? null,
          anotacoes: body.anotacoes ?? null,
          mensagem_planejada: body.mensagem_planejada ?? null,
        }),
        cache: "no-store",
      },
    );
    const out = await r.json().catch(() => ({ error: "resposta_invalida" }));
    return NextResponse.json(out, { status: r.status });
  } catch {
    return NextResponse.json({ error: "copiloto_indisponivel" }, { status: 502 });
  }
}
