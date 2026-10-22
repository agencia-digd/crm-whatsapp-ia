import { NextRequest, NextResponse } from "next/server";
import { cfg, resolveViewer } from "@/lib/config";

export const dynamic = "force-dynamic";

// PUT /api/automacoes/remarketing/agenda  { <chave>: valor | null, ... }
//
// O "salvar" da agenda do remarketing INTEIRO, numa transação no copiloto: ou
// entram todos os campos, ou nenhum. Campo a campo, o robô podia ler o horário
// novo com o teto antigo no meio do salvamento, e a falha de um campo deixava
// a agenda pela metade.
//
// Campo ausente não muda; `null` volta ao padrão. A validação é do copiloto, e
// a recusa (422) volta com `erros` por campo, que a tela mostra ao lado de
// cada um. Aqui só barra o que nem vale a viagem.
//
// Não envia nada e não liga o disparo: quem liga é o interruptor da tela.

const CAMPOS = [
  "remarketing_janela_horario",
  "remarketing_dias_semana",
  "remarketing_cap_diario",
  "remarketing_cap_semanal",
  "remarketing_etapa",
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
    const r = await fetch(`${cfg.copilotoUrl}/painel/automacoes/remarketing/agenda`, {
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
