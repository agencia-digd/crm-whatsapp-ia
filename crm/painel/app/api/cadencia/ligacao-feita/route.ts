import { NextRequest, NextResponse } from "next/server";
import { cfg, resolveViewer } from "@/lib/config";

export const dynamic = "force-dynamic";

// POST /api/cadencia/ligacao-feita  { cadencia_id, toque? }
//   →  POST /painel/cadencia/ligacao-feita
// Marca o toque de LIGAÇÃO atual da cadência como FEITO ("a Pessoa12 ligou"): registra
// o evento tarefa_ligacao e AVANÇA o estado (tira da fila de devidas). Diferente
// de concluir tarefa MANUAL — aqui a tarefa é gerada pelo motor de cadência.
// Escopo/ownership resolvido no copiloto (corretora dona ou admin; senão 403).
export async function POST(req: NextRequest) {
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  const body = await req.json().catch(() => null);
  const cadenciaId = body?.cadencia_id;
  if (cadenciaId == null || Number.isNaN(Number(cadenciaId))) {
    return NextResponse.json({ error: "cadencia_id_invalido" }, { status: 400 });
  }
  try {
    const r = await fetch(`${cfg.copilotoUrl}/painel/cadencia/ligacao-feita`, {
      method: "POST",
      headers: {
        "X-Painel-User": viewer.user,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        cadencia_id: Number(cadenciaId),
        toque: body?.toque ?? null,
        // resultado da ligação (3 estados) + observação livre — com eles o
        // copiloto grava a nota na conversa do lead e o dado da métrica.
        resultado: body?.resultado ?? null,
        observacao: body?.observacao ?? null,
      }),
      cache: "no-store",
    });
    const out = await r.json().catch(() => ({ error: "resposta_invalida" }));
    return NextResponse.json(out, { status: r.status });
  } catch {
    return NextResponse.json({ error: "copiloto_indisponivel" }, { status: 502 });
  }
}
