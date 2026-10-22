// 💸 Gastos da Acme — de onde sai cada real. Porta da regra de crm-dex/lib/gastos.ts.
//
// Regra da tela (a mesma do DEX): nenhum número vira "0" por falta de dado.
// Sem dado é "sem registro", com o motivo. Zero só quando o zero é o fato.
//
// SÓ LEITURA. Sem escrita no banco, sem Telegram.
//
// ── De onde vem cada coisa ──
//
// Template enviado: `cadencia_eventos` do copiloto (banco `copiloto`, tenant_id 1 =
// Acme lá dentro). Cada envio de template grava `payload.template`,
// `payload.categoria`, `payload.sent` e `payload.dry_run`. A spec original pedia
// `messages` do api_disparos (tenant 7), mas lá a Acme tem 0 linhas: o copiloto
// manda template direto pela Cloud API, sem passar pelo api-disparos.
//
// Preço: a Meta cobra por MENSAGEM de template desde jul/2025 (o DEX ainda conta
// por conversa de 24h, regra antiga). Aqui cada envio `sent=true` custa o preço
// da categoria na data. A tabela de preços é cópia de `crm_dex.preco_conversa`
// (BR); reajuste da Meta é uma linha nova aqui.
//
// IA do copiloto: `llm_chamadas` (copiloto). Tem tokens por serviço. A coluna
// `custo_usd` ainda não existe (vem na obra 6b): enquanto não existir, o custo é
// "sem registro". Se a coluna aparecer, a tela passa a somá-la sozinha.
//
// Banco: o `psql` do servidor, em sessão read-only e com timeout. A URL é lida
// do ambiente (GASTOS_DATABASE_URL) ou do .env do copiloto no momento do uso,
// vira variável PG* do processo filho (não aparece em argv) e nunca vai pra log.

import { execFile } from "node:child_process";
import { readFile } from "node:fs/promises";
import path from "node:path";

const TZ = "America/Sao_Paulo";
const TENANT_COPILOTO = 1; // tenant_id da Acme no banco do copiloto
const MS_STATEMENT = 8000;

// ── Preços (US$ por mensagem de template, BR) ───────────────────────────────

type Preco = { categoria: string; usd: number; desde: string };
export const PRECOS: Preco[] = [
  { categoria: "UTILITY", usd: 0.008, desde: "2026-09-01" },
  { categoria: "MARKETING", usd: 0.0625, desde: "2026-09-01" },
  { categoria: "UTILITY", usd: 0.0068, desde: "2026-10-01" },
  { categoria: "MARKETING", usd: 0.0625, desde: "2026-10-01" },
];

/** Preço vigente da categoria no dia (YYYY-MM-DD). null = sem preço cadastrado pra essa data. */
export function precoEm(categoria: string, dia: string): number | null {
  const p = PRECOS.filter((x) => x.categoria === categoria && x.desde <= dia)
    .sort((a, b) => (a.desde < b.desde ? 1 : -1))[0];
  return p ? p.usd : null;
}

// ── psql read-only ──────────────────────────────────────────────────────────

async function urlDoBanco(): Promise<string> {
  const env = (process.env.GASTOS_DATABASE_URL ?? "").trim();
  if (env) return env;
  const arq = process.env.GASTOS_ENV_FILE
    ?? path.resolve(process.cwd(), "../acme/copiloto-acme/.env");
  const txt = await readFile(arq, "utf8");
  const m = txt.match(/^DATABASE_URL=(.+)$/m);
  if (!m) throw new Error("DATABASE_URL do copiloto não encontrada");
  return m[1].trim().replace(/^["']|["']$/g, "");
}

function envPg(url: string): NodeJS.ProcessEnv {
  const u = new URL(url.replace(/^postgres(ql)?\+[a-z0-9]+:/i, "postgresql:"));
  return {
    NODE_ENV: process.env.NODE_ENV,
    PATH: process.env.PATH,
    PGHOST: u.hostname,
    PGPORT: u.port || "5432",
    PGUSER: decodeURIComponent(u.username),
    PGPASSWORD: decodeURIComponent(u.password),
    PGDATABASE: u.pathname.replace(/^\//, ""),
    PGCONNECT_TIMEOUT: "5",
    PGOPTIONS: `-c default_transaction_read_only=on -c statement_timeout=${MS_STATEMENT}`,
  };
}

async function json<T>(sql: string): Promise<T> {
  const url = await urlDoBanco();
  const saida = await new Promise<string>((ok, erro) => {
    execFile("psql", ["-X", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-c", sql],
      { env: envPg(url), timeout: MS_STATEMENT + 4000, maxBuffer: 4 * 1024 * 1024 },
      // a mensagem do erro do psql não leva a URL (ela vai por variável de ambiente)
      (e, out, err) => (e ? erro(new Error(String(err || e.message).trim().split("\n")[0])) : ok(out)));
  });
  return JSON.parse(saida.trim() || "null") as T;
}

// ── Consultas (cada uma é a "consulta que gera o número" da prova) ──────────

// Janela: mês anterior inteiro + mês corrente até agora, no fuso de Brasília.
const INI = `((date_trunc('month', now() AT TIME ZONE '${TZ}') - interval '1 month') AT TIME ZONE '${TZ}')`;
const MES = (col: string) => `to_char(${col} AT TIME ZONE '${TZ}', 'YYYY-MM')`;

export const SQL_TEMPLATES = `
SELECT coalesce(json_agg(r), '[]'::json) FROM (
  SELECT ${MES("at")} AS mes,
         payload->>'template'            AS template,
         upper(payload->>'categoria')    AS categoria,
         to_char(at AT TIME ZONE '${TZ}', 'YYYY-MM-DD') AS dia,
         count(*)::int                   AS enviados
    FROM cadencia_eventos
   WHERE tenant_id = ${TENANT_COPILOTO} AND tipo = 'mensagem'
     AND (payload->>'sent') = 'true'
     AND coalesce(payload->>'dry_run', 'false') <> 'true'
     AND payload->>'template' IS NOT NULL
     AND at >= ${INI}
   GROUP BY 1, 2, 3, 4
) r`;

export const SQL_NAO_SAIU = `
SELECT coalesce(json_agg(r), '[]'::json) FROM (
  SELECT ${MES("at")} AS mes, count(*)::int AS tentativas
    FROM cadencia_eventos
   WHERE tenant_id = ${TENANT_COPILOTO} AND tipo = 'mensagem'
     AND coalesce(payload->>'sent', 'false') <> 'true'
     AND at >= ${INI}
   GROUP BY 1
) r`;

export const SQL_TEM_CUSTO = `
SELECT json_build_object('tem', EXISTS (
  SELECT 1 FROM information_schema.columns
   WHERE table_name = 'llm_chamadas' AND column_name = 'custo_usd'))`;

export function sqlLlm(temCusto: boolean): string {
  return `
SELECT coalesce(json_agg(r), '[]'::json) FROM (
  SELECT ${MES("criado_em")} AS mes, servico,
         count(*)::int AS chamadas,
         coalesce(sum(input_tokens), 0)::bigint  AS entrada,
         coalesce(sum(output_tokens), 0)::bigint AS saida,
         count(erro)::int AS erros,
         ${temCusto ? "sum(custo_usd)::float" : "NULL::float"} AS custo_usd,
         ${temCusto ? "count(custo_usd)::int" : "0"} AS com_custo
    FROM llm_chamadas
   WHERE criado_em >= ${INI}
   GROUP BY 1, 2
) r`;
}

export const SQL_MESES = `
SELECT json_build_object(
  'atual',    to_char(now() AT TIME ZONE '${TZ}', 'YYYY-MM'),
  'anterior', to_char((now() AT TIME ZONE '${TZ}') - interval '1 month', 'YYYY-MM'),
  'hoje',     to_char(now() AT TIME ZONE '${TZ}', 'DD/MM HH24:MI'))`;

// ── Montagem ────────────────────────────────────────────────────────────────

type LinhaTpl = { mes: string; template: string; categoria: string | null; dia: string; enviados: number };
type LinhaLlm = {
  mes: string; servico: string; chamadas: number; entrada: number; saida: number;
  erros: number; custo_usd: number | null; com_custo: number;
};

export type TemplateMes = {
  template: string; categoria: string; enviados: number;
  /** null = sem registro (categoria ou preço desconhecidos) */
  custoUsd: number | null; motivo: string | null;
};
export type ServicoMes = {
  servico: string; chamadas: number; entrada: number; saida: number; erros: number;
  custoUsd: number | null; motivo: string | null;
};
export type Mes = {
  chave: string; rotulo: string;
  templates: TemplateMes[]; enviados: number; templatesUsd: number | null; templatesMotivo: string | null;
  naoSaiu: number;
  servicos: ServicoMes[]; chamadas: number; entrada: number; saida: number;
  iaUsd: number | null; iaMotivo: string | null;
  totalUsd: number | null; totalMotivo: string | null;
};
export type Cotacao = { valor: number; fonte: string } | null;
export type Gastos = {
  agora: string; cotacao: Cotacao; atual: Mes; anterior: Mes;
  precos: { categoria: string; usd: number; desde: string }[];
  temCustoIa: boolean;
};

const NOMES_MES = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho",
  "agosto", "setembro", "outubro", "novembro", "dezembro"];
const rotuloMes = (k: string) => `${NOMES_MES[Number(k.slice(5)) - 1]} de ${k.slice(0, 4)}`;

function mes(chave: string, tpl: LinhaTpl[], naoSaiu: { mes: string; tentativas: number }[],
  llm: LinhaLlm[], temCusto: boolean): Mes {
  const porTpl = new Map<string, TemplateMes>();
  let templatesUsd: number | null = 0;
  let templatesMotivo: string | null = null;
  for (const l of tpl.filter((x) => x.mes === chave)) {
    const cat = l.categoria ?? "";
    const k = `${l.template}|${cat}`;
    const a = porTpl.get(k) ?? {
      template: l.template, categoria: cat || "sem categoria", enviados: 0, custoUsd: 0, motivo: null,
    };
    a.enviados += l.enviados;
    const p = cat ? precoEm(cat, l.dia) : null;
    if (p === null) {
      a.custoUsd = null;
      a.motivo = cat ? `sem preço de ${cat} cadastrado em ${l.dia}` : "envio sem categoria gravada";
    } else if (a.custoUsd !== null) {
      a.custoUsd += p * l.enviados;
    }
    porTpl.set(k, a);
  }
  const templates = Array.from(porTpl.values())
    .sort((a, b) => (b.custoUsd ?? -1) - (a.custoUsd ?? -1) || b.enviados - a.enviados);
  for (const t of templates) {
    if (t.custoUsd === null) { templatesUsd = null; templatesMotivo = t.motivo; }
    else if (templatesUsd !== null) templatesUsd += t.custoUsd;
  }
  const enviados = templates.reduce((s, t) => s + t.enviados, 0);
  if (enviados === 0) { templatesUsd = null; templatesMotivo = "nenhum template enviado no mês"; }

  const servicos: ServicoMes[] = llm.filter((x) => x.mes === chave).map((x) => ({
    servico: x.servico, chamadas: x.chamadas, entrada: Number(x.entrada), saida: Number(x.saida),
    erros: x.erros,
    custoUsd: temCusto && x.com_custo > 0 ? x.custo_usd : null,
    motivo: !temCusto
      ? "a tabela não guarda custo (coluna custo_usd não existe); roda na assinatura do Claude, não é cobrança por token"
      : x.com_custo === 0 ? "chamadas sem custo gravado" : null,
  })).sort((a, b) => b.chamadas - a.chamadas);
  const chamadas = servicos.reduce((s, x) => s + x.chamadas, 0);
  let iaUsd: number | null = null;
  let iaMotivo: string | null = chamadas === 0 ? "nenhuma chamada no mês" : servicos[0]?.motivo ?? null;
  if (chamadas > 0 && servicos.every((x) => x.custoUsd !== null)) {
    iaUsd = servicos.reduce((s, x) => s + (x.custoUsd ?? 0), 0);
    iaMotivo = null;
  }

  // O total só é número quando as duas partes são. Parte sem registro não vira 0:
  // o total mostra o que dá pra somar e diz o que ficou de fora.
  const partes = [templatesUsd, iaUsd].filter((x): x is number => x !== null);
  const totalUsd = partes.length ? partes.reduce((s, x) => s + x, 0) : null;
  const fora: string[] = [];
  if (iaUsd === null) fora.push("IA do copiloto (sem custo registrado)");
  if (templatesUsd === null) fora.push("templates (sem registro)");
  return {
    chave, rotulo: rotuloMes(chave), templates, enviados, templatesUsd, templatesMotivo,
    naoSaiu: naoSaiu.find((x) => x.mes === chave)?.tentativas ?? 0,
    servicos, chamadas,
    entrada: servicos.reduce((s, x) => s + x.entrada, 0),
    saida: servicos.reduce((s, x) => s + x.saida, 0),
    iaUsd, iaMotivo, totalUsd,
    totalMotivo: totalUsd === null ? "sem nenhuma parte registrada" : fora.length ? `não inclui ${fora.join(" e ")}` : null,
  };
}

// PTAX de venda do Banco Central, guardada por 6h. Sem resposta, a tela mostra só US$ e diz por quê.
let cacheCotacao: { em: number; c: Cotacao } | null = null;
async function cotacao(): Promise<Cotacao> {
  if (cacheCotacao && Date.now() - cacheCotacao.em < 6 * 3600_000) return cacheCotacao.c;
  let c: Cotacao = null;
  try {
    const d = (dt: Date) => `${String(dt.getMonth() + 1).padStart(2, "0")}-${String(dt.getDate()).padStart(2, "0")}-${dt.getFullYear()}`;
    const fim = new Date();
    const ini = new Date(fim.getTime() - 10 * 86_400_000);
    const url = "https://olinda.bcb.gov.br/olinda/servico/PTAX/versao/v1/odata/" +
      "CotacaoDolarPeriodo(dataInicial=@dataInicial,dataFinalCotacao=@dataFinalCotacao)" +
      `?@dataInicial='${d(ini)}'&@dataFinalCotacao='${d(fim)}'&$format=json&$select=cotacaoVenda,dataHoraCotacao`;
    const r = await fetch(url, { signal: AbortSignal.timeout(4000), cache: "no-store" });
    const j: any = await r.json();
    const ult = j?.value?.[j.value.length - 1];
    if (ult?.cotacaoVenda) {
      const [a, m, dd] = String(ult.dataHoraCotacao).slice(0, 10).split("-");
      c = { valor: Number(ult.cotacaoVenda), fonte: `PTAX venda do Banco Central de ${dd}/${m}/${a}` };
    }
  } catch { /* sem cotação: só US$ */ }
  cacheCotacao = { em: Date.now(), c };
  return c;
}

export async function gastos(): Promise<Gastos> {
  const [meses, tpl, naoSaiu, tem, cot] = await Promise.all([
    json<{ atual: string; anterior: string; hoje: string }>(SQL_MESES),
    json<LinhaTpl[]>(SQL_TEMPLATES),
    json<{ mes: string; tentativas: number }[]>(SQL_NAO_SAIU),
    json<{ tem: boolean }>(SQL_TEM_CUSTO),
    cotacao(),
  ]);
  const llm = await json<LinhaLlm[]>(sqlLlm(tem.tem));
  return {
    agora: meses.hoje, cotacao: cot,
    atual: mes(meses.atual, tpl, naoSaiu, llm, tem.tem),
    anterior: mes(meses.anterior, tpl, naoSaiu, llm, tem.tem),
    precos: PRECOS,
    temCustoIa: tem.tem,
  };
}
