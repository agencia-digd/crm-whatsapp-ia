import { NextRequest, NextResponse } from "next/server";
import { cfg, resolveViewer } from "@/lib/config";

export const dynamic = "force-dynamic";

// PUT /api/automacoes/remarketing/copy  { textos?: {variante: texto|null}, espelho?: boolean }
//
// O "salvar" do texto das variantes do teste A/B. `null` (ou vazio) numa
// variante = voltar ao texto do código. `espelho` liga/desliga a variante de
// marketing seguindo o texto da `direta`.
//
// A validação é do copiloto (tamanho, caractere que a Meta recusa, chaves no
// texto) e a recusa (422) volta com `erros` POR VARIANTE, que a tela mostra ao
// lado do campo. Texto recusado nunca é gravado. Aqui só barra o que nem vale
// a viagem.
//
// Não reaprova template nem muda categoria: o texto vai na variável de um
// template UTILITY já aprovado. Não envia nada e não liga o disparo.

export async function PUT(req: NextRequest) {
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  if (!viewer.isAdmin) {
    return NextResponse.json({ error: "somente_gestao" }, { status: 403 });
  }
  const body = await req.json().catch(() => null);
  if (body === null || typeof body !== "object" || Array.isArray(body)) {
    return NextResponse.json({ error: "corpo_invalido" }, { status: 400 });
  }
  const estranhos = Object.keys(body).filter((k) => k !== "textos" && k !== "espelho");
  if (estranhos.length > 0) {
    return NextResponse.json({ error: "campo_desconhecido", campos: estranhos }, { status: 400 });
  }
  const envio: Record<string, unknown> = {};
  if ("textos" in body) envio.textos = body.textos;
  if ("espelho" in body) envio.espelho = body.espelho;
  if (Object.keys(envio).length === 0) {
    return NextResponse.json({ error: "nenhum_campo" }, { status: 400 });
  }
  try {
    const r = await fetch(`${cfg.copilotoUrl}/painel/automacoes/remarketing/copy`, {
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
