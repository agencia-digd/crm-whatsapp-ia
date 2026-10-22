// Aviso in-app "lead respondeu" (blueprint B4 · lei da Dona 10/07: ZERO
// WhatsApp/template pra aviso interno — o aviso vive SÓ na plataforma).
//
// Deriva tudo do board do CRM (/api/crm → copiloto /painel/crm/board), que já
// carrega `respondeu_em` por card (timestamp do último cadencia_eventos
// tipo='resposta' da cadência do telefone). Critério: card AINDA na coluna
// "Em atendimento" com resposta nas últimas 48h = lead que a cadência
// recuperou e a corretora ainda não deu conta.
//
// Fix 17/07 (reclamação Pessoa12): o backend já ZERA `respondeu_em` quando a
// corretora responde de volta o lead (outbound > respondeu_em). Então
// atender = a tag some do card E o contador aqui baixa junto — sem botão
// manual. A janela de 48h continua como teto de segurança.

export type RespondidoCard = {
  lead_id: number;
  external_ref?: string | null;
  nome: string;
  telefone: string | null;
  corretora_label?: string | null;
  respondeu_em: string | null;
};

type BoardLike = {
  colunas?: { nome?: string | null; cards?: RespondidoCard[] }[];
};

export const JANELA_RESPOSTA_MS = 48 * 60 * 60 * 1000; // 48h

export function respondidosNaoAtendidos(
  board: unknown,
  agora: Date = new Date(),
): RespondidoCard[] {
  const colunas = (board as BoardLike | null)?.colunas;
  if (!Array.isArray(colunas)) return [];
  const col = colunas.find(
    (c) => (c?.nome || "").trim().toLowerCase() === "em atendimento",
  );
  if (!col || !Array.isArray(col.cards)) return [];
  return col.cards
    .filter((c) => {
      if (!c?.respondeu_em) return false;
      const t = new Date(c.respondeu_em).getTime();
      return Number.isFinite(t) && agora.getTime() - t <= JANELA_RESPOSTA_MS;
    })
    .sort((a, b) => (b.respondeu_em || "").localeCompare(a.respondeu_em || ""));
}

// `kommoUrl` removida em 31/07: montava link pro Kommo (example.kommo.com),
// que saiu de operação. Não tinha nenhum chamador — era só uma porta aberta pro
// sistema antigo esperando alguém usar.
