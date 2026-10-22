import { NextRequest, NextResponse } from "next/server";
import { resolveViewer } from "@/lib/config";
import { gastos } from "@/lib/gastos";

export const dynamic = "force-dynamic";

// Gastos da operação (template + IA do copiloto). Só admin; só leitura.
export async function GET(req: NextRequest) {
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  if (!viewer.isAdmin) return NextResponse.json({ error: "forbidden" }, { status: 403 });
  try {
    return NextResponse.json(await gastos());
  } catch (e) {
    return NextResponse.json(
      { error: "banco_indisponivel", detalhe: e instanceof Error ? e.message : "erro" },
      { status: 502 });
  }
}
