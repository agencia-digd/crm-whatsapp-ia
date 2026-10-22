import { NextRequest, NextResponse } from "next/server";
import { cfg, resolveViewer } from "@/lib/config";

export const dynamic = "force-dynamic";

// A tela espera MAIS que o CLI: o copiloto gasta até 2 tentativas do roteador + 1 resposta,
// cada uma com teto de 45 s (ASSISTENTE_CLI_TIMEOUT) = 135 s no pior caso. Se a tela desistisse
// antes (eram 60 s contra 120 s do CLI), a corretora via erro com a resposta ainda saindo.
const TELA_TIMEOUT_MS = 150_000;

// Assistente Copilota (assistente interna da corretora) via copiloto /painel/assistente.
export async function POST(req: NextRequest) {
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  const body = await req.json().catch(() => ({}));
  try {
    const r = await fetch(`${cfg.copilotoUrl}/painel/assistente`, {
      method: "POST",
      headers: { "X-Painel-User": viewer.user, "Content-Type": "application/json" },
      body: JSON.stringify(body),
      cache: "no-store",
      signal: AbortSignal.timeout(TELA_TIMEOUT_MS),
    });
    const data = await r.json().catch(() => ({
      resposta: "A Assistente respondeu algo que eu não consegui ler 😿 tenta de novo? Se repetir, avisa a gestão.",
    }));
    return NextResponse.json(data, { status: r.status });
  } catch (e) {
    const timeout = e instanceof Error && (e.name === "TimeoutError" || e.name === "AbortError");
    const resposta = timeout
      ? "Essa pergunta demorou demais pra eu pensar 😿 Tenta de novo com uma pergunta mais curta (ex: 'como tá o João?')."
      : "A Assistente está fora do ar agora 😿 Espera 1 minuto e tenta de novo. Se continuar, avisa a gestão.";
    return NextResponse.json({ resposta, erro: timeout ? "timeout" : "indisponivel" }, { status: 200 });
  }
}
