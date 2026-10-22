import { NextRequest, NextResponse } from "next/server";
import { cfg, resolveViewer } from "@/lib/config";

export const dynamic = "force-dynamic";

// Posts sociais da corretora — proxy pro copiloto (posts_corretora.py, :8002).
// GET  /api/posts[?corretora=corretor]  →  GET /painel/posts/me[?corretora=]
//   Retorna { posts: [...], posts_por_semana, ig_conectado, is_admin }.
//   ?corretora= só tem efeito pra admin (o copiloto ignora pra corretora comum).
// POST /api/posts { formato, url_referencia?, observacao, corretora? }
//   →  POST /painel/posts — cria um BRIEF (pedido de post), não post pronto.
//   observacao é obrigatória (o copiloto devolve 422 sem ela).
// DELETE /api/posts?id=123  →  DELETE /painel/posts/{id}
//   (copiloto recusa com 409 se já publicado/publicando)
export async function GET(req: NextRequest) {
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  const corretora = req.nextUrl.searchParams.get("corretora");
  const qs = corretora ? `?corretora=${encodeURIComponent(corretora)}` : "";
  try {
    const r = await fetch(`${cfg.copilotoUrl}/painel/posts/me${qs}`, {
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
  if (!body || !body.observacao || !String(body.observacao).trim()) {
    return NextResponse.json({ error: "observacao_obrigatoria" }, { status: 422 });
  }
  try {
    const r = await fetch(`${cfg.copilotoUrl}/painel/posts`, {
      method: "POST",
      headers: { "X-Painel-User": viewer.user, "Content-Type": "application/json" },
      body: JSON.stringify({
        formato: body.formato ?? "single",
        url_referencia: body.url_referencia ?? null,
        observacao: body.observacao,
        corretora: body.corretora ?? null, // só admin — copiloto valida
      }),
      cache: "no-store",
    });
    const out = await r.json().catch(() => ({ error: "resposta_invalida" }));
    return NextResponse.json(out, { status: r.status });
  } catch {
    return NextResponse.json({ error: "copiloto_indisponivel" }, { status: 502 });
  }
}

export async function DELETE(req: NextRequest) {
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  const id = req.nextUrl.searchParams.get("id");
  if (!id) return NextResponse.json({ error: "id_obrigatorio" }, { status: 400 });
  try {
    const r = await fetch(`${cfg.copilotoUrl}/painel/posts/${encodeURIComponent(id)}`, {
      method: "DELETE",
      headers: { "X-Painel-User": viewer.user },
      cache: "no-store",
    });
    const out = await r.json().catch(() => ({}));
    return NextResponse.json(out, { status: r.status });
  } catch {
    return NextResponse.json({ error: "copiloto_indisponivel" }, { status: 502 });
  }
}
