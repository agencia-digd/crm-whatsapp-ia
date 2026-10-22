import { NextRequest, NextResponse } from "next/server";
import { cfg, resolveViewer } from "@/lib/config";

export const dynamic = "force-dynamic";

// Áudio pra Assistente: repassa o arquivo gravado no navegador pro copiloto
// (/painel/assistente/audio), que transcreve e responde. Multipart passa direto —
// nada é escrito em disco aqui.
export async function POST(req: NextRequest) {
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  try {
    const form = await req.formData();
    const r = await fetch(`${cfg.copilotoUrl}/painel/assistente/audio`, {
      method: "POST",
      headers: { "X-Painel-User": viewer.user },
      body: form,
      cache: "no-store",
      // transcrição (Whisper no Mac) + resposta da Assistente: janela mais larga
      signal: AbortSignal.timeout(120000),
    });
    const data = await r
      .json()
      .catch(() => ({ transcricao: null, resposta: "Deu um erro aqui 😿" }));
    return NextResponse.json(data, { status: r.status });
  } catch {
    return NextResponse.json(
      { transcricao: null, resposta: "Não consegui te ouvir agora 😿 escreve pra mim?" },
      { status: 200 },
    );
  }
}
