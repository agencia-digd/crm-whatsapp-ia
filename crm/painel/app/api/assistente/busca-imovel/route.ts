import { NextRequest, NextResponse } from "next/server";
import { cfg, resolveViewer } from "@/lib/config";

export const dynamic = "force-dynamic";

// Busca de imóvel de TERCEIROS na web — versão ASSÍNCRONA (job).
//
// POST → abre o job no copiloto e devolve {job_id} na hora. A tela não fica
// presa esperando o Mac (Playwright é lento). O resultado chega por polling em
// GET /api/assistente/busca-imovel?job_id=N.
// GET sem job_id → últimos jobs do usuário (é o que faz a tela reabrir já com
// o resultado de quem saiu e voltou).
//
// SUGESTÃO pra corretora — nunca envia nada pro lead.

const TIMEOUT_MS = 15_000; // abrir/consultar job é rápido: só fala com o copiloto

async function chamarCopiloto(
  req: NextRequest,
  caminho: string,
  init: RequestInit,
): Promise<NextResponse> {
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  const ac = new AbortController();
  const t = setTimeout(() => ac.abort(), TIMEOUT_MS);
  try {
    const r = await fetch(`${cfg.copilotoUrl}${caminho}`, {
      ...init,
      headers: {
        "Content-Type": "application/json",
        "X-Painel-User": viewer.user,
        ...(init.headers || {}),
      },
      cache: "no-store",
      signal: ac.signal,
    });
    const body = await r.json().catch(() => ({ erro: "resposta_invalida" }));
    return NextResponse.json(body, { status: r.status });
  } catch {
    return NextResponse.json(
      { erro: "copiloto indisponível — tenta de novo" },
      { status: 502 },
    );
  } finally {
    clearTimeout(t);
  }
}

export async function POST(req: NextRequest) {
  let texto = "";
  try {
    const body = await req.json();
    texto = (body?.texto || "").toString().trim();
  } catch {
    return NextResponse.json({ erro: "pedido_invalido" }, { status: 400 });
  }
  if (texto.length < 4) {
    return NextResponse.json({
      job_id: null,
      status: "erro",
      pedido: texto,
      erro: "me diz o que o lead procura — ex: 'apto 3 quartos no Bairro A até 500 mil'",
    });
  }
  return chamarCopiloto(req, "/painel/busca-imovel", {
    method: "POST",
    body: JSON.stringify({ texto }),
  });
}

export async function GET(req: NextRequest) {
  const jobId = req.nextUrl.searchParams.get("job_id");
  const caminho = jobId
    ? `/painel/busca-imovel/${encodeURIComponent(jobId)}`
    : "/painel/busca-imovel?limite=5";
  return chamarCopiloto(req, caminho, { method: "GET" });
}
