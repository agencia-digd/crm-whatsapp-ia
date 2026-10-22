import { NextRequest, NextResponse } from "next/server";
import { cfg, resolveViewer } from "@/lib/config";

export const dynamic = "force-dynamic";

// PUT /api/automacoes/remarketing/quando-responde  { <chave>: valor | null, ... }
//
// O "salvar" do bloco "Quando a pessoa responde" INTEIRO, numa transação no
// copiloto: quem vai pra corretora, a etapa de entrada, a roleta do
// remarketing e o aviso. Ou entram todos os campos, ou nenhum.
//
// Campo ausente não muda; `null` volta ao padrão (na roleta, lista vazia `[]`
// é valor de verdade: ninguém recebe). A validação é do copiloto, e a recusa
// (422) volta com `erros` por campo.
//
// Não envia mensagem, não mexe na roleta do Meta e não transfere quem já está
// em Respondeu.

const CAMPOS = [
  "handoff_remarketing_quem_vai",
  "handoff_remarketing_etapa",
  "handoff_remarketing_roleta",
  "handoff_remarketing_avisa_lead",
  "handoff_remarketing_aviso_texto",
  "handoff_remarketing_aviso_resumo",
  "handoff_remarketing_respeita_humano",
  // IA do lado (05/10): resumo no card e opinião no que a regra não entendeu.
  "ia_resumo_card",
  "ia_opina_indefinida",
  "ia_opina_corte",
] as const;

export async function PUT(req: NextRequest) {
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  if (!viewer.isAdmin) {
    return NextResponse.json({ error: "somente_gestao" }, { status: 403 });
  }
  const body = await req.json().catch(() => null);
  if (body === null || typeof body !== "object" || Array.isArray(body)) {
    return NextResponse.json({ error: "corpo_invalido" }, { status: 400 });
  }
  const estranhos = Object.keys(body).filter((k) => !(CAMPOS as readonly string[]).includes(k));
  if (estranhos.length > 0) {
    return NextResponse.json({ error: "campo_desconhecido", campos: estranhos }, { status: 400 });
  }
  const envio: Record<string, unknown> = {};
  for (const campo of CAMPOS) if (campo in body) envio[campo] = body[campo];
  if (Object.keys(envio).length === 0) {
    return NextResponse.json({ error: "nenhum_campo" }, { status: 400 });
  }
  try {
    const r = await fetch(`${cfg.copilotoUrl}/painel/automacoes/remarketing/quando-responde`, {
      method: "PUT",
      headers: { "X-Painel-User": viewer.user, "Content-Type": "application/json" },
      body: JSON.stringify(envio),
      cache: "no-store",
    });
    const out = await r.json().catch(() => ({ error: "resposta_invalida" }));
    return NextResponse.json(out, { status: r.status });
  } catch {
    return NextResponse.json({ error: "copiloto_indisponivel" }, { status: 502 });
  }
}
