import { NextRequest, NextResponse } from "next/server";
import { cfg, resolveViewer } from "@/lib/config";

export const dynamic = "force-dynamic";

// PUT    /api/automacoes/config/{chave}  { valor } → grava
// DELETE /api/automacoes/config/{chave}            → volta pro padrão do sistema
//
// A validação de verdade (teto do limite, roleta não-vazia, corretora que
// existe) mora no copiloto, não aqui: é ele que o motor lê, e regra de proteção
// duplicada em dois lugares vira duas regras diferentes na primeira mudança.
// Aqui só barra o que nem vale a viagem.

type Ctx = { params: { chave: string } };

const CHAVES = new Set([
  "cadencia_ativa",
  // Interruptor PRÓPRIO da nutrição (05/08). Faltava aqui: a página tinha o
  // botão e o proxy respondia `chave_desconhecida` antes do copiloto, então
  // desligar a nutrição pela tela nunca gravou nada (achado em 06/10/2026).
  "nutricao_ativa",
  "cadencia_cap_diario",
  "cadencia_cap_por_corretora",
  "roleta_corretoras",
  // Remarketing (carrossel da Corretor): interruptor da automação e os pesos do
  // teste A/B. As duas travas dos pesos (soma 100 e cota de MARKETING em 20%)
  // moram no copiloto de propósito — validação só no navegador é validação que
  // um curl fura.
  "carrossel_corretor_ativo",
  "carrossel_pesos",
  // Agenda do remarketing (05/10): janela de horário, dias da semana, limite
  // por dia e por semana. Sem estas quatro aqui a tela devolvia 400 antes de
  // chegar no copiloto — o formulário existia e não gravava nada.
  "remarketing_janela_horario",
  "remarketing_dias_semana",
  "remarketing_cap_diario",
  "remarketing_cap_semanal",
  // Alvo do remarketing (05/10): de qual etapa e de qual dono saem os leads.
  // A conferência de que a etapa é DESTE cliente mora no copiloto.
  "remarketing_etapa",
  "remarketing_dono",
  // Junção com o motor (05/10): pra onde vai quem pede pra parar (Venda
  // perdida, padrão, ou Não perturbe) e o aviso diário do conferente no
  // Telegram (nasce desligado). Quem confere se a etapa serve é o copiloto.
  "remarketing_etapa_parar",
  "remarketing_conferente_avisa_telegram",
  // Texto das variantes e "quando a pessoa responde" (05/10). A tela grava
  // pelos pedidos próprios (`/remarketing/copy` e `/remarketing/quando-responde`,
  // tudo ou nada); as chaves ficam aqui pro "voltar ao padrão" de cada uma.
  "carrossel_copies",
  "carrossel_marketing_espelha_direta",
  "handoff_remarketing_quem_vai",
  "handoff_remarketing_etapa",
  "handoff_remarketing_roleta",
  "handoff_remarketing_avisa_lead",
  "handoff_remarketing_aviso_texto",
  "handoff_remarketing_aviso_resumo",
  "handoff_remarketing_respeita_humano",
  // IA do lado (05/10): resumo por IA no card e opinião da IA no que a regra
  // chamou de indefinida. Validação (true/false, corte 0,5–1) no copiloto.
  "ia_resumo_card",
  "ia_opina_indefinida",
  "ia_opina_corte",
  // Boas-vindas: kill-switch (a mesma chave que o motor lê antes de processar
  // o webhook do lead novo), copies e pesos do teste A/B da primeira mensagem.
  // As travas (soma 100, placeholder suportado, variante aposentada) moram no
  // copiloto — validação só no navegador é validação que um curl fura.
  "lead_intake_ativo",
  "boas_vindas_copies",
  "boas_vindas_pesos",
  // Interruptor POR CORRETORA do follow-up e da nutrição (06/10): mapa
  // id → true/false, fora do mapa = ligada. A tela grava um clique por
  // corretora pelas rotas próprias (`/follow-up/corretoras/[id]`,
  // `/nutricao/corretoras/[id]`); aqui fica o "voltar ao padrão" (DELETE),
  // que é o conserto quando o mapa foi gravado torto por fora.
  "cadencia_por_corretora",
  "nutricao_por_corretora",
  // Nutrição pela linha da DONA do lead (07/10): true = sai pela linha dela
  // quando ela tem linha e modelo aprovado; false = toda pela Pessoa12.
  "nutricao_pela_linha_da_dona",
]);

async function repassar(
  req: NextRequest,
  chave: string,
  metodo: "PUT" | "DELETE",
  body?: unknown,
) {
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  if (!viewer.isAdmin) {
    return NextResponse.json({ error: "somente_gestao" }, { status: 403 });
  }
  if (!CHAVES.has(chave)) {
    return NextResponse.json({ error: "chave_desconhecida" }, { status: 400 });
  }
  try {
    const r = await fetch(`${cfg.copilotoUrl}/painel/automacoes/config/${chave}`, {
      method: metodo,
      headers: {
        "X-Painel-User": viewer.user,
        ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
      },
      body: body !== undefined ? JSON.stringify(body) : undefined,
      cache: "no-store",
    });
    const out = await r.json().catch(() => ({ error: "resposta_invalida" }));
    return NextResponse.json(out, { status: r.status });
  } catch {
    return NextResponse.json({ error: "copiloto_indisponivel" }, { status: 502 });
  }
}

export async function PUT(req: NextRequest, { params }: Ctx) {
  const body = await req.json().catch(() => null);
  if (body === null || !("valor" in body)) {
    return NextResponse.json({ error: "valor_obrigatorio" }, { status: 400 });
  }
  return repassar(req, params.chave, "PUT", { valor: body.valor });
}

export async function DELETE(req: NextRequest, { params }: Ctx) {
  return repassar(req, params.chave, "DELETE");
}
