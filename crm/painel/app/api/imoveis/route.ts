import { NextRequest, NextResponse } from "next/server";
import { cfg, resolveViewer } from "@/lib/config";

export const dynamic = "force-dynamic";

// Captação de imóveis — proxy pro copiloto (imoveis_captados.py, :8002).
//
// GET  /api/imoveis[?situacao=&q=&corretora_id=]  →  GET /painel/imoveis
//   { imoveis: [...], total, is_admin, corretoras, situacoes, tipos }
//   corretora_id só tem efeito pra admin; o copiloto ignora pra corretora comum.
// POST /api/imoveis { tipo, bairro, dono_telefone, preco_pedido, ... }
//   →  POST /painel/imoveis, 201 { ok, id, imovel, avisos }
//
// ⚠️ Este proxy NÃO valida campo nenhum de propósito. A regra que manda no
// módulo inteiro é "nada pode fazer o cadastro falhar": a tela é preenchida em
// 60 segundos, do celular, no meio da conversa com o dono, e um 422 no meio da
// conversa custa o imóvel. Quem decide o que é obrigatório é o copiloto (quatro
// campos), e duplicata lá AVISA sem barrar. Espelhar validação aqui só criaria
// uma segunda regra que diverge na primeira mudança.
export async function GET(req: NextRequest) {
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  const qs = req.nextUrl.search || "";
  try {
    const r = await fetch(`${cfg.copilotoUrl}/painel/imoveis${qs}`, {
      headers: { "X-Painel-User": viewer.user },
      cache: "no-store",
    });
    const body = await r.json().catch(() => ({ error: "resposta_invalida" }));
    return NextResponse.json(body, { status: r.status });
  } catch {
    return NextResponse.json({ error: "copiloto_indisponivel" }, { status: 502 });
  }
}

export async function POST(req: NextRequest) {
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  const body = await req.json().catch(() => null);
  if (!body) {
    return NextResponse.json({ error: "corpo_invalido" }, { status: 400 });
  }
  try {
    const r = await fetch(`${cfg.copilotoUrl}/painel/imoveis`, {
      method: "POST",
      headers: {
        "X-Painel-User": viewer.user,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(body),
    });
    const out = await r.json().catch(() => ({ error: "resposta_invalida" }));
    return NextResponse.json(out, { status: r.status });
  } catch {
    return NextResponse.json({ error: "copiloto_indisponivel" }, { status: 502 });
  }
}
