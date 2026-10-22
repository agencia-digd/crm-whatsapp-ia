import { NextRequest, NextResponse } from "next/server";
import { cfg, resolveViewer } from "@/lib/config";

export const dynamic = "force-dynamic";

// POST /api/tarefas/editar  { id, texto, data, hora? }
//   →  POST /painel/tarefa/{id}/editar
// Edita texto/data/hora de uma tarefa ABERTA. Escopo resolvido no copiloto:
// corretora dona ou admin; senão 403. Tarefa concluída não pode editar (422).
// O front só repassa o X-Painel-User via resolveViewer.
export async function POST(req: NextRequest) {
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  const body = await req.json().catch(() => null);
  const id = body?.id;
  if (id == null || Number.isNaN(Number(id))) {
    return NextResponse.json({ error: "id_invalido" }, { status: 400 });
  }
  if (!body?.texto || !body?.data) {
    return NextResponse.json({ error: "body_invalido" }, { status: 400 });
  }
  try {
    const r = await fetch(
      `${cfg.copilotoUrl}/painel/tarefa/${encodeURIComponent(String(id))}/editar`,
      {
        method: "POST",
        headers: {
          "X-Painel-User": viewer.user,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          texto: body.texto,
          data: body.data,
          hora: body.hora ?? null,
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
