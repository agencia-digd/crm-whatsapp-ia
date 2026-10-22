import { NextResponse } from "next/server";

export const dynamic = "force-dynamic";
export const revalidate = 0;

// Empreendimentos PRÓPRIOS da Acme (catálogo da carteira, ~114 imóveis).
// Planilha VIVA lida pelo Mac mini (navegador logado no Google, via gviz) e
// servida na Vpn. O painel lê, transforma linhas em objetos E normaliza os
// campos sujos (preço, bairro, quartos, entrega) pro painel filtrar/casar com o lead.
// Normalização portada 1:1 do V1 (dash-imobiliaria/app/api/empreendimentos/route.ts).
const FONTE = process.env.EMPREENDIMENTOS_URL ?? "http://100.64.0.1:11440/empreendimentos";

// Último payload bom montado por este processo. Se a fonte (Mac) cair, servimos
// isso com stale:true em vez de engolir o erro — dado pode ficar velho, nunca sem etiqueta.
let ultimoBom: { atualizado_em: string | null; total: number; itens: any[] } | null = null;

function idxDe(header: string[]): Record<string, number> {
  const norm = header.map((h) => (h || "").toLowerCase().trim());
  const acha = (...t: string[]) => norm.findIndex((h) => t.some((x) => h.includes(x)));
  return {
    empreendimento: acha("empreendimento", "nome"),
    construtora: acha("construtora", "incorpora"),
    tipo: acha("tipo"),
    quartos: acha("quarto", "dorm"),
    preco: acha("a partir", "r$", "valor", "preço", "preco"),
    entrega: acha("entrega"),
    cidade: acha("localiza", "cidade", "região", "regiao", "zona"),
    bairro: acha("bairro"),
    decorado: acha("decorado", "endereço", "endereco"),
    drive: acha("link", "drive"),
  };
}

const titleCase = (s: string) =>
  s.toLowerCase().replace(/\b([a-zà-ú])/g, (m) => m.toUpperCase()).trim();

function parsePreco(raw: string): { num: number | null; fmt: string } {
  const digits = (raw || "").replace(/[^\d]/g, "");
  if (!digits) return { num: null, fmt: "sob consulta" };
  const n = parseInt(digits, 10);
  if (!n) return { num: null, fmt: "sob consulta" };
  const fmt = n >= 1_000_000
    ? `R$ ${(n / 1_000_000).toLocaleString("pt-BR", { maximumFractionDigits: 2 })} mi`
    : `R$ ${Math.round(n / 1000)} mil`;
  return { num: n, fmt };
}

function parseQuartos(raw: string): number | null {
  const m = (raw || "").match(/\d/);
  return m ? parseInt(m[0], 10) : null;
}

function parseEntrega(raw: string): { bucket: "pronto" | "lancamento" | "data" | null; fmt: string } {
  const s = (raw || "").trim();
  if (!s) return { bucket: null, fmt: "" };
  const low = s.toLowerCase();
  if (low.includes("pronto")) return { bucket: "pronto", fmt: "Pronto" };
  if (low.includes("lanç") || low.includes("lanc")) return { bucket: "lancamento", fmt: "Lançamento" };
  // tem número/data (ex: 01/27, jul/28, dez/2028) → bucket data
  if (/\d/.test(s)) return { bucket: "data", fmt: s.replace(/\s+/g, " ") };
  return { bucket: "data", fmt: s };
}

export async function GET() {
  try {
    const r = await fetch(FONTE, { cache: "no-store", signal: AbortSignal.timeout(12000) });
    if (!r.ok) throw new Error(`fonte ${r.status}`);
    const data = await r.json();
    const rows: string[][] = data?.rows ?? [];
    if (rows.length < 2) {
      return NextResponse.json({ configurado: true, atualizado_em: data?.at ?? null, stale: Boolean(data?.stale), total: 0, itens: [] });
    }
    const ix = idxDe(rows[0]);
    const cel = (row: string[], k: keyof ReturnType<typeof idxDe>) => {
      const i = ix[k];
      return i >= 0 ? (row[i] ?? "").trim() : "";
    };
    const itens = rows.slice(1)
      .map((row) => {
        const preco = parsePreco(cel(row, "preco"));
        const entrega = parseEntrega(cel(row, "entrega"));
        const drive = cel(row, "drive");
        return {
          empreendimento: cel(row, "empreendimento"),
          construtora: cel(row, "construtora"),
          tipo: cel(row, "tipo").toUpperCase(),
          quartos: cel(row, "quartos"),
          quartos_norm: parseQuartos(cel(row, "quartos")),
          preco_bruto: cel(row, "preco"),
          preco_num: preco.num,
          preco_fmt: preco.fmt,
          entrega_bucket: entrega.bucket,
          entrega_fmt: entrega.fmt,
          zona: titleCase(cel(row, "cidade")),
          bairro: titleCase(cel(row, "bairro")),
          decorado: cel(row, "decorado"),
          drive,
          tem_material: /^https?:\/\//i.test(drive),
        };
      })
      .filter((e) => e.empreendimento);
    ultimoBom = { atualizado_em: data?.at ?? null, total: itens.length, itens };
    return NextResponse.json({ configurado: true, atualizado_em: data?.at ?? null, stale: Boolean(data?.stale), total: itens.length, itens });
  } catch (e: any) {
    // Fonte fora do ar (Mac desligado/rede). Se já tivemos dado bom, serve ele etiquetado.
    if (ultimoBom) {
      return NextResponse.json({ configurado: true, ...ultimoBom, stale: true, erro: String(e?.message ?? e) }, { status: 200 });
    }
    return NextResponse.json({ configurado: false, stale: true, itens: [], erro: String(e?.message ?? e) }, { status: 200 });
  }
}
