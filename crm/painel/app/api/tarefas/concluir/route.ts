import { NextRequest, NextResponse } from "next/server";
import { cfg, resolveViewer } from "@/lib/config";

export const dynamic = "force-dynamic";

// POST /api/tarefas/concluir  { id, resultado?, observacao? }
//   →  POST /painel/tarefa/{id}/concluir
// Marca a tarefa como concluída (status='concluida'). Idempotente no copiloto
// (UPDATE ... WHERE status='aberta'). Escopo resolvido lá: corretora dona ou
// admin; senão 403. O front só repassa o X-Painel-User via resolveViewer.
//
// `resultado` (tarefa de LIGAÇÃO) é um dos três estados — nao_atendeu ·
// atendeu_sem_continuar · atendeu_quer_continuar. Com ele o copiloto grava a
// nota na conversa do lead + o dado estruturado da métrica. Sem ele conclui
// como sempre (e a tarefa vira "sem registro", nunca "não atendeu").
export async function POST(req: NextRequest) {
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  const body = await req.json().catch(() => null);
  const id = body?.id;
  if (id == null || Number.isNaN(Number(id))) {
    return NextResponse.json({ error: "id_invalido" }, { status: 400 });
  }
  try {
    const r = await fetch(
      `${cfg.copilotoUrl}/painel/tarefa/${encodeURIComponent(String(id))}/concluir`,
      {
        method: "POST",
        headers: {
          "X-Painel-User": viewer.user,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          resultado: body?.resultado ?? null,
          observacao: body?.observacao ?? null,
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
