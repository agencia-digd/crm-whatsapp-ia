import { NextRequest, NextResponse } from "next/server";
import { cfg, resolveViewer } from "@/lib/config";

export const dynamic = "force-dynamic";

// Leads cuja ÚLTIMA mensagem foi DO LEAD — ele falou e ainda não foi respondido.
// É a fila inversa da /conversa/esperando (lá a corretora espera o lead).
//
// Stateless por natureza: o copiloto olha só qual foi a última mensagem da
// conversa. "Acende" quando o lead fala e apaga sozinho quando a corretora
// responde — não precisa marcar nada como lido.
//
// Devolve { total, telefones: { <8 últimos dígitos>: <ISO da última msg do lead> } }.
// O front casa esses 8 dígitos com o telefone do card (o CRM não tem o telefone
// normalizado, e 8 dígitos evita divergência de DDI/DDD/nono dígito).
// Privacidade por corretora é do copiloto (X-Painel-User).
export async function GET(req: NextRequest) {
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  const dias = req.nextUrl.searchParams.get("dias") ?? "14";
  try {
    const r = await fetch(
      `${cfg.copilotoUrl}/painel/conversa/aguardando?dias=${encodeURIComponent(dias)}`,
      { headers: { "X-Painel-User": viewer.user }, cache: "no-store" },
    );
    const out = await r.json().catch(() => ({ error: "resposta_invalida" }));
    return NextResponse.json(out, { status: r.status });
  } catch {
    return NextResponse.json({ error: "copiloto_indisponivel" }, { status: 502 });
  }
}
