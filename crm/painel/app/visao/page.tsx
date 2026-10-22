"use client";

// Painel VISÃO (dashboard gerencial) — porte fiel do dash-imobiliaria (V1,
// view "visao" em app/page.tsx). Backend intocado: consome /api/visao →
// copiloto /painel/visao-geral. Escopo/privacidade quem resolve é o copiloto
// (X-Painel-User): admin vê tudo; corretora vê só o funil dela, sem Meta Ads/
// investimento/comparativo. Aqui, defesa em profundidade: só renderiza os
// blocos de investimento quando viewer.role !== "corretora".

import { ReactNode, useEffect, useState } from "react";
import "./visao.css";
import { Atividade, type AtividadeResp } from "./Atividade";

// ── helpers de formatação (idênticos ao V1) ────────────────────────────────
const brl = (v: number | null | undefined) =>
  v == null ? "—" : v.toLocaleString("pt-BR", { style: "currency", currency: "BRL", maximumFractionDigits: 0 });
const num = (v: number | null | undefined) => (v == null ? "—" : v.toLocaleString("pt-BR"));
const pct = (v: number | null | undefined) =>
  v == null ? "—" : `${v.toLocaleString("pt-BR", { maximumFractionDigits: 1 })}%`;

const BENCHMARK = { atendeu: 60, agendou: 33, visitou: 80, vendeu: 12 };

// ── Frescor (carimbo "atualizado em") ───────────────────────────────────────
// A CHAVE (`snapshot_kommo`) é o nome da fonte no backend; o RÓTULO é o que
// aparece na tela — e desde 31/07 não cita mais o Kommo, que saiu de operação.
const FRESCOR_LABELS: Record<string, string> = {
  analises: "análises",
  snapshot_kommo: "base de leads",
  conversas: "conversas",
  vendas_mensais: "vendas",
  atendidos_evolution: "WhatsApp",
};
function fmtHoraCurta(iso: string | null | undefined): string {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    const hoje = new Date();
    const mesmoDia = d.toDateString() === hoje.toDateString();
    if (mesmoDia) return d.toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" });
    return d.toLocaleDateString("pt-BR", { day: "2-digit", month: "2-digit" });
  } catch {
    return "—";
  }
}
function Frescor({ fontes, mostrar }: { fontes?: Record<string, string | null> | null; mostrar: string[] }) {
  if (!fontes) return null;
  const pecas = mostrar
    .filter((k) => k in fontes)
    .map((k) => {
      const iso = fontes[k];
      if (k === "conversas") return `${FRESCOR_LABELS[k]} ao vivo`;
      return `${FRESCOR_LABELS[k] ?? k} ${fmtHoraCurta(iso)}`;
    });
  if (pecas.length === 0) return null;
  const full = Object.keys(fontes)
    .map(
      (k) =>
        `${FRESCOR_LABELS[k] ?? k}: ${
          k === "conversas"
            ? "ao vivo · última " + fmtHoraCurta(fontes[k])
            : fontes[k]
            ? new Date(fontes[k] as string).toLocaleString("pt-BR")
            : "—"
        }`,
    )
    .join("\n");
  return (
    <div className="frescor" title={full}>
      🕐 {pecas.join(" · ")}
    </div>
  );
}

// ── KPI card ─────────────────────────────────────────────────────────────────
function KPI({ label, value, sub, variant, title }: { label: string; value: ReactNode; sub?: ReactNode; variant?: "dark" | "accent"; title?: string }) {
  return (
    <div className={`vg-kpi ${variant ?? ""}`} title={title}>
      <div className="vg-kpi-lbl">{label}</div>
      <div className="vg-kpi-val">{value}</div>
      {sub && <div className="vg-kpi-sub">{sub}</div>}
    </div>
  );
}

// ── Funil da equipe (coração do painel) ──────────────────────────────────────
// Interativo (2026-07-26): hover destaca a etapa (tooltip com o detalhe) e o
// CLIQUE expande um painel com conversão vs mercado + quebra por corretora
// (quando o viewer é admin — porCorretora chega vazio pra corretora). Só
// apresentação: nenhum dado novo, tudo já vinha no payload do /api/visao.
function VgFunilEquipe({ eq, captadosMeta, porCorretora }: { eq: any; captadosMeta: number; porCorretora: any[] }) {
  const [etapaSel, setEtapaSel] = useState<number | null>(null);
  const showCaptados = captadosMeta > 0;
  const atend = eq.atendidos_evolution && eq.atendidos_evolution > 0 ? eq.atendidos_evolution : eq.atendidos;
  const convAtend = eq.conv_atendeu_evolution ?? eq.conv_atendeu;
  // `campo(c)` extrai o valor da MESMA etapa no funil de cada corretora — é o
  // que alimenta a quebra do painel expandido. null = etapa sem quebra (Meta).
  const etapas = [
    showCaptados
      ? { label: "Captados Meta", valor: captadosMeta, conv: null as number | null, bench: undefined as number | undefined, cor: "ouro", campo: null as ((c: any) => number) | null }
      : { label: "Lead", valor: eq.leads, conv: null as number | null, bench: undefined as number | undefined, cor: "grafite", campo: (c: any) => c.leads },
    {
      label: showCaptados ? "Atendido" : "Atendeu",
      valor: atend,
      conv: convAtend,
      bench: BENCHMARK.atendeu,
      cor: "laranja",
      campo: (c: any) => c.atendidos_evolution ?? c.atendidos,
      titulo:
        eq.atendidos_evolution && eq.atendidos_evolution > 0
          ? "Atendidos (WhatsApp): leads com conversa 2-via real no período"
          : "Atendidos (base de leads): leads que saíram da etapa Entrada",
    },
    {
      label: "Agendou",
      valor: eq.agendados,
      conv: eq.conv_agendou,
      bench: BENCHMARK.agendou,
      cor: "laranja",
      campo: (c: any) => c.agendados,
      titulo: "Toda visita da agenda no período, inclusive as canceladas e as que ainda vão acontecer",
    },
    {
      label: "Visitou",
      valor: eq.visitas,
      conv: eq.conv_visitou,
      bench: BENCHMARK.visitou,
      cor: "laranja",
      campo: (c: any) => c.visitas,
      titulo: "Compareceram. Conversão = compareceu ÷ visitas encerradas (compareceu + no-show + cancelada)",
    },
    { label: "Vendeu", valor: eq.vendas, conv: eq.conv_vendeu, bench: BENCHMARK.vendeu, cor: "verde", campo: (c: any) => c.vendas },
  ];
  const maxVal = Math.max(1, ...etapas.map((e) => e.valor || 0));
  return (
    <div className="vg-funilbox">
      <div className="vg-funilbox-head">
        <div>
          <h3>Funil da equipe</h3>
          <span className="vg-mono">{showCaptados ? "Tráfego → Captação → Conversão → Resultado" : "Captação → Conversão → Resultado"}</span>
        </div>
        <div style={{ textAlign: "right" }}>
          <div className="vg-mono">Conversão geral</div>
          <div className="vg-funil-geral">{pct(eq.conv_vendeu)}</div>
        </div>
      </div>
      <div className="vg-funilbars">
        {etapas.map((e, i) => {
          const w = Math.max(8, ((e.valor || 0) / maxVal) * 100);
          const acima = e.bench != null && e.conv != null ? e.conv >= e.bench : null;
          const tip = [
            `${e.label}: ${num(e.valor)}`,
            e.conv != null ? `conversão ${pct(e.conv)}` : null,
            e.bench != null && e.conv != null
              ? `mercado ${e.bench}% (${e.conv >= e.bench ? "acima ▲" : "abaixo ▼"})`
              : null,
            (e as any).titulo ?? null,
            "clique pra expandir o detalhe",
          ].filter(Boolean).join("\n");
          const aberta = etapaSel === i;
          return (
            <div key={i}>
              <div
                className={`vg-funilrow clicavel ${aberta ? "aberta" : ""}`}
                title={tip}
                onClick={() => setEtapaSel(aberta ? null : i)}
              >
                <div className="vg-funilrow-lbl">
                  {e.label}
                  {(e as any).titulo ? <span className="vg-info"> ⓘ</span> : null}
                </div>
                <div className="vg-funilrow-track">
                  <div className={`vg-funilrow-bar anim ${e.cor}`} style={{ width: `${w}%`, animationDelay: `${i * 70}ms` }}>
                    <span>{num(e.valor)}</span>
                  </div>
                </div>
                <div className="vg-funilrow-conv">
                  {e.conv != null ? (
                    <>
                      <div>
                        <b>{pct(e.conv)}</b> <span className="vg-mono">conv.</span>
                      </div>
                      {e.bench != null && <div className={acima ? "vg-up" : "vg-down"}>{acima ? "▲" : "▼"} mercado {e.bench}%</div>}
                    </>
                  ) : (
                    <span className="vg-mono" style={{ color: "#bbb" }}>
                      —
                    </span>
                  )}
                </div>
              </div>
              {aberta && (
                <div className="vg-funil-detalhe">
                  <div className="vg-funil-detalhe-head">
                    <b>{e.label}</b> · {num(e.valor)} no período
                    {e.conv != null && e.bench != null && (
                      <span className={e.conv >= e.bench ? "vg-up" : "vg-down"} style={{ marginLeft: 8 }}>
                        {pct(e.conv)} vs mercado {e.bench}% ({e.conv >= e.bench ? "+" : ""}{(e.conv - e.bench).toLocaleString("pt-BR", { maximumFractionDigits: 1 })} p.p.)
                      </span>
                    )}
                  </div>
                  {e.campo && porCorretora.length > 0 ? (
                    <div className="vg-funil-detalhe-grid">
                      {porCorretora.map((c: any) => {
                        const v = e.campo!(c) || 0;
                        const share = e.valor ? (v / e.valor) * 100 : 0;
                        return (
                          <div key={c.corretora_id} className="vg-funil-detalhe-item" title={`${c.label}: ${num(v)} (${pct(share)} da etapa)`}>
                            <span className="vg-funil-detalhe-nome">{c.label}</span>
                            <div className="vg-funil-detalhe-track">
                              <div className="vg-funil-detalhe-bar" style={{ width: `${Math.max(2, share)}%` }} />
                            </div>
                            <b>{num(v)}</b>
                          </div>
                        );
                      })}
                    </div>
                  ) : (
                    <div className="vg-mono" style={{ marginTop: 6 }}>
                      {e.campo ? "sem quebra por corretora nesse recorte" : "etapa de tráfego — sem quebra por corretora"}
                    </div>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ── Card por corretora ───────────────────────────────────────────────────────
function VgCorretora({ c }: { c: any }) {
  const atend = c.atendidos_evolution ?? c.atendidos;
  const max = Math.max(1, c.leads, atend, c.agendados, c.visitas, c.vendas);
  const mini = [
    { l: "Lead", v: c.leads, conv: null as number | null, bench: undefined as number | undefined },
    { l: "Atend.", v: atend, conv: c.conv_atendeu_evolution ?? c.conv_atendeu, bench: BENCHMARK.atendeu },
    { l: "Agend.", v: c.agendados, conv: c.conv_agendou, bench: BENCHMARK.agendou },
    { l: "Visit.", v: c.visitas, conv: c.conv_visitou, bench: BENCHMARK.visitou },
    { l: "Venda", v: c.vendas, conv: c.conv_vendeu, bench: BENCHMARK.vendeu },
  ];
  return (
    <div className="vg-cc">
      <div className="vg-cc-top">
        <div className="vg-cc-av">{(c.label || c.nome || "?")[0]}</div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="vg-cc-nome">{c.label}</div>
          <div className="vg-cc-sub">{c.nome}</div>
        </div>
        <div style={{ textAlign: "right" }}>
          <div className="vg-mono">VGV</div>
          <div className="vg-cc-vgv">{brl(c.vgv)}</div>
        </div>
      </div>
      {c.recuperados > 0 && (
        <div className="vg-cc-recup">
          <span className="vg-mono">Recuperados</span>
          <b>{num(c.recuperados)}</b>
        </div>
      )}
      <div className="vg-cc-mini">
        {mini.map((e, i) => {
          const w = Math.max(2, ((e.v || 0) / max) * 100);
          const acima = e.bench != null && e.conv != null ? e.conv >= e.bench : null;
          const tip = [
            `${e.l}: ${num(e.v)}`,
            e.conv != null ? `conversão ${pct(e.conv)}` : null,
            e.bench != null && e.conv != null
              ? `mercado ${e.bench}% (${e.conv >= e.bench ? "acima ▲" : "abaixo ▼"})`
              : null,
          ].filter(Boolean).join(" · ");
          return (
            <div key={i} className="vg-cc-row" title={tip}>
              <span className="vg-cc-row-l">{e.l}</span>
              <div className="vg-cc-track">
                <div className="vg-cc-bar anim" style={{ width: `${w}%`, animationDelay: `${i * 60}ms` }} />
              </div>
              <span className="vg-cc-v">{num(e.v)}</span>
              <span className="vg-cc-dot" style={{ background: acima == null ? "transparent" : acima ? "#3f7b5f" : "#c0392b" }} />
            </div>
          );
        })}
      </div>
      <div className="vg-cc-foot">
        <span className="vg-mono">Atendeu / Lead</span>
        <b>{pct(c.conv_atendeu_evolution ?? c.conv_atendeu)}</b>
      </div>
    </div>
  );
}

// ── Tendência 14 dias (barrinhas verticais) — novo no V2 ─────────────────────
// Interativa (2026-07-26): hover destaca a coluna (tooltip com data completa e
// % do total) e o CLIQUE fixa um painel de detalhe do dia (qtde, participação
// e comparação com a média diária). Só apresentação — dados idênticos.
function VgTendencia({ tend }: { tend: any }) {
  const [diaSel, setDiaSel] = useState<number | null>(null);
  const dados: { data: string; qtde: number }[] = tend?.meta_leads ?? [];
  if (!Array.isArray(dados) || dados.length === 0) return null;
  const maxV = Math.max(1, ...dados.map((d) => d.qtde || 0));
  const total = dados.reduce((s, d) => s + (d.qtde || 0), 0);
  const media = total / dados.length;
  const fmtData = (iso: string) => {
    try {
      return new Date(`${iso}T00:00:00`).toLocaleDateString("pt-BR", { weekday: "long", day: "2-digit", month: "2-digit" });
    } catch {
      return iso;
    }
  };
  const sel = diaSel != null && diaSel < dados.length ? dados[diaSel] : null;
  return (
    <div className="vg-tend">
      <div className="vg-tend-head">
        <h3>Leads Meta · últimos 14 dias</h3>
        <span className="vg-mono">{total} no total · média {media.toLocaleString("pt-BR", { maximumFractionDigits: 1 })}/dia</span>
      </div>
      <div className="vg-tend-bars">
        {dados.map((d, i) => {
          const h = Math.max(2, ((d.qtde || 0) / maxV) * 100);
          const dia = d.data?.slice(8, 10) ?? "";
          const share = total > 0 ? ((d.qtde || 0) / total) * 100 : 0;
          const aberta = diaSel === i;
          return (
            <div
              key={i}
              className={`vg-tend-col clicavel ${d.qtde ? "" : "zero"} ${aberta ? "aberta" : ""}`}
              title={`${fmtData(d.data)}\n${num(d.qtde)} lead(s) · ${pct(share)} do período\nclique pra fixar o detalhe`}
              onClick={() => setDiaSel(aberta ? null : i)}
            >
              <div className="vg-tend-bar anim" style={{ height: `${h}%`, animationDelay: `${i * 40}ms` }}>
                {d.qtde > 0 && <span>{d.qtde}</span>}
              </div>
              <div className="vg-tend-dia">{dia}</div>
            </div>
          );
        })}
      </div>
      {sel && (
        <div className="vg-tend-detalhe">
          <b style={{ textTransform: "capitalize" }}>{fmtData(sel.data)}</b>
          <span>{num(sel.qtde)} lead(s)</span>
          <span>{pct(total > 0 ? ((sel.qtde || 0) / total) * 100 : 0)} do período</span>
          <span className={sel.qtde >= media ? "vg-up" : "vg-down"}>
            {sel.qtde >= media ? "▲ acima" : "▼ abaixo"} da média ({media.toLocaleString("pt-BR", { maximumFractionDigits: 1 })}/dia)
          </span>
        </div>
      )}
    </div>
  );
}

type Vg = any;

export default function VisaoPage() {
  const [vg, setVg] = useState<Vg | null>(null);
  const [loading, setLoading] = useState(false);
  const [periodo, setPeriodo] = useState("mes_atual");
  const [erro, setErro] = useState<string | null>(null);
  const [ativ, setAtiv] = useState<AtividadeResp | null>(null);
  const [ativErro, setAtivErro] = useState<string | null>(null);

  function carregar(p: string) {
    setLoading(true);
    setErro(null);
    // Some com o período anterior: sem isso o funil velho (e o título velho)
    // ficava na tela enquanto a atividade já mostrava o período novo.
    setVg(null);
    // Mensagens e ligações vêm à parte (rápido) — não esperam o funil.
    setAtiv(null);
    setAtivErro(null);
    fetch(`/api/visao/atividade?periodo=${encodeURIComponent(p)}`, { cache: "no-store" })
      .then((r) => r.json())
      .then((d) => {
        if (d?.error || d?.detail) setAtivErro(String(d.error ?? d.detail));
        else setAtiv(d);
      })
      .catch(() => setAtivErro("copiloto_indisponivel"));
    fetch(`/api/visao?periodo=${encodeURIComponent(p)}`, { cache: "no-store" })
      .then((r) => r.json())
      .then((d) => {
        if (d?.error) {
          setErro(d.error);
          setVg(null);
        } else {
          setVg(d);
        }
      })
      .catch(() => setErro("copiloto_indisponivel"))
      .finally(() => setLoading(false));
  }
  useEffect(() => {
    carregar(periodo);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div className="vg">
      <div className="hoje-head">
        <div>
          <div className="hoje-kick">Visão geral · Acme</div>
          <h2 className="hoje-titulo" style={{ textTransform: "capitalize" }}>
            {vg?.periodo?.label ?? "—"}
          </h2>
          <p className="hoje-sub">
            {vg?.periodo?.inicio ?? ""}
            {vg?.periodo?.fim ? ` → ${vg.periodo.fim}` : ""}
          </p>
        </div>
        <select
          className="vg-periodo"
          value={periodo}
          onChange={(e) => {
            setPeriodo(e.target.value);
            carregar(e.target.value);
          }}
        >
          <option value="hoje">Hoje</option>
          <option value="semana">Semana</option>
          <option value="7d">Últimos 7 dias</option>
          <option value="mes_atual">Mês atual</option>
          <option value="30d">Últimos 30 dias</option>
          <option value="mes_passado">Mês passado</option>
          <option value="tudo">Tudo (desde 01/08)</option>
        </select>
      </div>
      <Frescor fontes={vg?.fontes} mostrar={["vendas_mensais", "atendidos_evolution", "analises", "conversas"]} />

      {loading && !vg && <div className="loading" style={{ padding: 40 }}>Carregando…</div>}
      {erro && !vg && (
        <div className="hoje-vazio" style={{ marginTop: 14 }}>
          Sem dados agora ({erro}). Tenta recarregar em instantes.
        </div>
      )}

      {vg &&
        (() => {
          const m = vg.meta_ads || {};
          const eq = vg.funil_equipe || {};
          const dv = vg.derivados || {};
          const vis = vg.visitas_resumo || {};
          const ret = dv.retorno || null;
          const isAdmin = vg.viewer?.role !== "corretora";
          // O que ficou fora da mídia, só as origens que têm venda.
          const foraPartes: string[] = ret
            ? [
                ["indicação", ret.fora_midia?.indicacao],
                ["parceiro", ret.fora_midia?.parceiro],
                ["sem origem", ret.fora_midia?.nao_informada],
              ]
                .filter(([, o]: any) => o && o.vgv > 0)
                .map(([nome, o]: any) => `${nome} ${brl(o.vgv)}`)
            : [];
          return (
            <>
              <div className="vg-kpis">
                {isAdmin && (
                  <KPI
                    label="Investimento Meta Ads"
                    value={brl(m.spend)}
                    sub={
                      <>
                        {num(m.impressions)} impressões · {num(m.clicks)} cliques
                      </>
                    }
                    variant="dark"
                  />
                )}
                {isAdmin && (
                  <KPI
                    label="Leads captados (Meta)"
                    value={num(m.leads_meta)}
                    sub={
                      <>
                        CPL {m.cpl != null ? brl(m.cpl) : "—"} · CPC {m.cpc != null ? brl(m.cpc) : "—"}
                      </>
                    }
                  />
                )}
                <KPI
                  label="Leads no funil"
                  value={num(eq.leads)}
                  sub={
                    <>
                      {num(eq.atendidos_evolution ?? eq.atendidos)} atendidos · {num(eq.agendados)} agendados
                    </>
                  }
                />
                <KPI
                  label="VGV no período"
                  value={brl(eq.vgv)}
                  sub={
                    <>
                      {num(eq.vendas)} venda(s) · ticket {eq.ticket_medio != null ? brl(eq.ticket_medio) : "—"}
                    </>
                  }
                  variant="accent"
                />
                {isAdmin && (
                  <KPI
                    label="ROI da mídia"
                    value={dv.roi != null ? `${num(Math.round(dv.roi))}×` : "—"}
                    sub={ret ? <>VGV de anúncio {brl(ret.vgv_midia)} ÷ investimento</> : "VGV de anúncio ÷ investimento"}
                    title="Só conta venda cuja origem é mídia (lead de anúncio). Indicação, parceiro e venda sem origem ficam fora."
                  />
                )}
                {isAdmin && (
                  <KPI
                    label="Custo por venda (mídia)"
                    value={dv.custo_venda != null ? brl(dv.custo_venda) : "—"}
                    sub={
                      <>
                        {ret ? (ret.vendas_midia > 0 ? `${num(ret.vendas_midia)} venda(s) de anúncio` : "nenhuma venda de anúncio") : ""}
                        {dv.custo_visita != null ? ` · visita ${brl(dv.custo_visita)}` : ""}
                      </>
                    }
                  />
                )}
                {isAdmin && ret && (
                  <KPI
                    label="VGV fora da mídia"
                    value={brl(ret.vgv_fora_midia)}
                    sub={foraPartes.length ? foraPartes.join(" · ") : "todo o VGV veio de anúncio"}
                    title="Parte do VGV do período que não entra no ROI: indicação, parceiro e venda sem origem informada."
                  />
                )}
                <KPI
                  label="Comparecimento"
                  value={pct(vis.taxa_comparecimento)}
                  sub={
                    vis.encerradas != null ? (
                      <>
                        {num(vis.compareceu)} de {num(vis.encerradas)} encerradas · {num(vis.canceladas)} cancelada(s) · {num(vis.no_show)} no-show
                        {vis.a_realizar > 0 ? ` · ${num(vis.a_realizar)} a realizar` : ""}
                      </>
                    ) : (
                      <>{num(vis.total)} visita(s) no período</>
                    )
                  }
                  title="Compareceu ÷ visitas encerradas (compareceu + no-show + cancelada). Visita que ainda vai acontecer fica fora da conta."
                />
              </div>

              {isAdmin &&
                vg.vazamento_captacao &&
                vg.vazamento_captacao.captados_meta > 0 &&
                (() => {
                  const z = vg.vazamento_captacao;
                  return (
                    <div className="vg-vaz">
                      <div className="vg-vaz-lbl">
                        Vazamento de captação <span className="vg-mono">· {z.captados_meta} captados Meta</span>
                      </div>
                      <div className="vg-vaz-cels">
                        <span className="vg-vaz-cel ok">
                          <b>{num(z.novos_no_funil)}</b> novos no funil
                        </span>
                        <span className="vg-vaz-cel warn">
                          <b>{num(z.reengajados)}</b> reengajados
                        </span>
                        <span className="vg-vaz-cel bad">
                          <b>{num(z.realmente_perdidos)}</b> perdidos
                        </span>
                        {z.taxa_vazamento_real_pct != null && (
                          <span className="vg-vaz-cel">
                            <b>{pct(z.taxa_vazamento_real_pct)}</b> vazamento real
                          </span>
                        )}
                      </div>
                    </div>
                  );
                })()}

              <VgFunilEquipe
                eq={eq}
                captadosMeta={isAdmin ? vg.vazamento_captacao?.captados_meta ?? 0 : 0}
                porCorretora={isAdmin ? vg.funil_por_corretora ?? [] : []}
              />

              <VgTendencia tend={vg.tendencia_14d} />

              <Atividade dados={ativ} erro={ativErro} />

              {isAdmin && (vg.funil_por_corretora || []).length > 0 && (
                <>
                  <div className="vg-sec-titulo">
                    Time <span className="vg-mono" style={{ fontWeight: 400 }}>· {vg.funil_por_corretora.length} corretoras ativas</span>
                  </div>
                  <div className="vg-cc-grid">
                    {vg.funil_por_corretora.map((c: any) => (
                      <VgCorretora key={c.corretora_id} c={c} />
                    ))}
                  </div>
                </>
              )}

              {/* Blocos secundários (recuperação Corretor + remarketing Pessoa12 + atenção
                  hoje) — não vazam investimento; ok pra corretora ver o dela.
                  remarketing_corretor só chega pra admin e pra própria Pessoa12 (o backend
                  manda null pras outras corretoras). */}
              {(vg.recovery_corretor || vg.remarketing_corretor || vg.atencao_hoje) && (
                <>
                  <div className="vg-sec-titulo">Recuperação & atenção</div>
                  <div className="vg-mini-grid">
                    {vg.remarketing_corretor && (
                      <div
                        className="vg-mini vg-remkt"
                        title={
                          "Operação de remarketing da Pessoa12 (reengajamento da base via cadência).\n" +
                          "Régua: respostas → agendamentos.\n" +
                          "Meta: respostas virarem visita agendada."
                        }
                      >
                        <h4>🔄 Remarketing (Pessoa12)</h4>
                        <div className="vg-mini-row" title="Templates enviados pela instância acme_corretora_a no período">
                          <span>Toques enviados</span>
                          <b>{num(vg.remarketing_corretor.toques_enviados)}</b>
                        </div>
                        <div className="vg-mini-row" title="Leads distintos que responderam na instância no período">
                          <span>Leads responderam</span>
                          <b>{num(vg.remarketing_corretor.leads_responderam)}</b>
                        </div>
                        <div className="vg-mini-row" title="Cadências marcadas como respondidas no período">
                          <span>Respostas (cadência)</span>
                          <b>{num(vg.remarketing_corretor.respostas_cadencia)}</b>
                        </div>
                        <div className="vg-mini-row" title="Visitas agendadas da Pessoa12 no período (mesma fonte do funil)">
                          <span>Agendamentos</span>
                          <b>{num(vg.remarketing_corretor.agendamentos)}</b>
                        </div>
                        <div
                          className="vg-remkt-taxa"
                          title="agendamentos ÷ leads que responderam — meta: respostas virarem visita agendada"
                        >
                          <span className="vg-mono">Resposta → Agendamento</span>
                          <b>{pct(vg.remarketing_corretor.taxa_resposta_para_agendamento)}</b>
                        </div>
                      </div>
                    )}
                    {vg.recovery_corretor && (
                      <div className="vg-mini">
                        <h4>Recuperação (Corretor)</h4>
                        <div className="vg-mini-row"><span>Disparados</span><b>{num(vg.recovery_corretor.disparados)}</b></div>
                        <div className="vg-mini-row"><span>Responderam</span><b>{num(vg.recovery_corretor.responderam)}</b></div>
                        <div className="vg-mini-row"><span>Aguardando</span><b>{num(vg.recovery_corretor.aguardando)}</b></div>
                        <div className="vg-mini-row"><span>Ainda na fila</span><b>{num(vg.recovery_corretor.na_fila)}</b></div>
                      </div>
                    )}
                    {vg.atencao_hoje && (
                      <div className="vg-mini">
                        <h4>Atenção hoje</h4>
                        <div className="vg-mini-row"><span>Leads quentes</span><b>{num(vg.atencao_hoje.leads_quentes?.total)}</b></div>
                        <div className="vg-mini-row"><span>Mornos com gap</span><b>{num(vg.atencao_hoje.leads_mornos_com_gap?.total)}</b></div>
                        <div className="vg-mini-row"><span>Visitas hoje</span><b>{num((vg.atencao_hoje.visitas_hoje || []).length)}</b></div>
                        <div className="vg-mini-row"><span>Tarefas atrasadas</span><b>{num(vg.atencao_hoje.tarefas_atrasadas?.total)}</b></div>
                      </div>
                    )}
                  </div>
                </>
              )}

              {Array.isArray(vg.alertas) && vg.alertas.length > 0 && (
                <div className="vg-alertas" style={{ marginTop: 22 }}>
                  <div className="vg-sec-titulo">Alertas</div>
                  {vg.alertas.map((a: any, i: number) => (
                    <div key={i} className={`vg-alerta ${a.nivel === "alto" ? "alto" : ""}`}>
                      {a.nivel === "alto" ? "🔴" : "🟡"} {a.msg}
                    </div>
                  ))}
                </div>
              )}
            </>
          );
        })()}
    </div>
  );
}
