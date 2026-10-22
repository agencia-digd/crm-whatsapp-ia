import { NextRequest, NextResponse } from "next/server";
import { cfg, resolveViewer } from "@/lib/config";

export const dynamic = "force-dynamic";

// PUT /api/automacoes/remarketing/linhas/{phone_number_id}/papel
//     { papel, pausar_os_outros? }
//
// Troca pra que serve um número já cadastrado: "disparo" (é por ele que o
// remarketing sai), "atendimento" (número de gente, nunca dispara em massa) ou
// "pausado" (número de disparo que está parado).
//
// O dado mora no cadastro de números (api-disparos); o copiloto é a ponte com
// o login do painel. A validação é de lá — papel aceito, número que é deste
// cliente — e a recusa volta como veio. Aqui só barra o que nem vale a viagem.
//
// `pausar_os_outros: true` (só com papel "disparo") troca de disparador num
// pedido só: o cadastro pausa quem estava marcado e marca o novo na MESMA
// transação. Em dois pedidos, a falha do segundo deixava a casa sem disparador
// e a ordem inversa deixava dois (o motor segura tudo como "ambíguo").
//
// Não envia nada e não liga o disparo: quem liga é o interruptor da tela.

type Ctx = { params: { id: string } };

export async function PUT(req: NextRequest, { params }: Ctx) {
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  if (!viewer.isAdmin) {
    return NextResponse.json({ error: "somente_gestao" }, { status: 403 });
  }
  if (!/^\d{5,40}$/.test(params.id)) {
    return NextResponse.json({ error: "numero_invalido" }, { status: 400 });
  }
  const body = await req.json().catch(() => null);
  if (body === null || typeof body.papel !== "string") {
    return NextResponse.json({ error: "papel_obrigatorio" }, { status: 400 });
  }
  try {
    const r = await fetch(
      `${cfg.copilotoUrl}/painel/automacoes/remarketing/linhas/${params.id}/papel`,
      {
        method: "PUT",
        headers: { "X-Painel-User": viewer.user, "Content-Type": "application/json" },
        body: JSON.stringify({
          papel: body.papel,
          // Só `true` de verdade liga a pausa dos outros; qualquer outra coisa é não.
          ...(body.pausar_os_outros === true ? { pausar_os_outros: true } : {}),
        }),
        cache: "no-store",
      },
    );
    const out = await r.json().catch(() => ({ error: "resposta_invalida" }));
    return NextResponse.json(out, { status: r.status });
  } catch {
    return NextResponse.json({ error: "copiloto_indisponivel" }, { status: 502 });
  }
}
