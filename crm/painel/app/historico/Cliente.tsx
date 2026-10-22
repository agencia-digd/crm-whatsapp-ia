"use client";

// 📈 HISTÓRICO anual (visão gestão) — porte do V1 (dash-imobiliaria, view
// "historico" em app/page.tsx) pro V2, com dois upgrades: gráfico de VGV
// mês-a-mês em barras CSS e breakdown por corretora ao clicar num mês
// (o /painel/historico já devolve por_corretora — o V1 jogava fora).
// Consome /api/historico → copiloto /painel/historico?ano=YYYY.
// Admin-only: proxy devolve 403 pra corretora e o copiloto revalida.
// O endpoint recalcula o ano inteiro — pode levar 1-2 min; loading avisa.

import { Fragment, useEffect, useState } from "react";
import "./historico.css";

const brl = (v: number | null | undefined) =>
  v == null ? "—" : v.toLocaleString("pt-BR", { style: "currency", currency: "BRL", maximumFractionDigits: 0 });
const num = (v: number | null | undefined) => (v == null ? "—" : v.toLocaleString("pt-BR"));
const pct = (v: number | null | undefined) =>
  v == null ? "—" : `${v.toLocaleString("pt-BR", { maximumFractionDigits: 1 })}%`;

// Gráfico simples de VGV por mês — barras verticais CSS (sem lib).
function HtGraficoVgv({ meses }: { meses: any[] }) {
  const dados = meses.map((m) => ({ label: m.label, vgv: m.equipe?.vgv || 0 }));
  const max = Math.max(1, ...dados.map((d) => d.vgv));
  if (dados.every((d) => d.vgv === 0)) return null;
  return (
    <div className="ht-graf">
      <div className="ht-graf-head">
        <h3>VGV mês a mês</h3>
      </div>
      <div className="ht-graf-bars">
        {dados.map((d, i) => {
          const h = Math.max(2, (d.vgv / max) * 100);
          return (
            <div key={i} className={`ht-graf-col ${d.vgv ? "" : "zero"}`}>
              <div className="ht-graf-bar" style={{ height: `${h}%` }}>
                {d.vgv > 0 && <span>{(d.vgv / 1_000_000).toLocaleString("pt-BR", { maximumFractionDigits: 1 })}M</span>}
              </div>
              <div className="ht-graf-mes">{d.label}</div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

export default function HistoricoPageCliente() {
  const anoAtual = new Date().getFullYear();
  const [hist, setHist] = useState<any | null>(null);
  const [loading, setLoading] = useState(false);
  const [ano, setAno] = useState(anoAtual);
  const [erro, setErro] = useState<string | null>(null);
  const [aberto, setAberto] = useState<string | null>(null); // ano_mes expandido

  function carregar(a: number) {
    setLoading(true);
    setErro(null);
    setAberto(null);
    fetch(`/api/historico?ano=${a}`, { cache: "no-store" })
      .then(async (r) => {
        const body = await r.json().catch(() => ({ error: "resposta_invalida" }));
        if (!r.ok || body?.error) throw new Error(body?.error || body?.detail || `HTTP ${r.status}`);
        setHist(body);
      })
      .catch((e) => {
        setErro(String(e.message || e));
        setHist(null);
      })
      .finally(() => setLoading(false));
  }
  useEffect(() => {
    carregar(ano);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (erro === "somente_gestao" || erro === "painel_admin_required") {
    return (
      <div className="ht">
        <div className="ht-vazio">🔒 O histórico anual é visão de gestão — fala com a Dona se precisar desses números.</div>
      </div>
    );
  }

  const totais = hist?.totais;

  return (
    <div className="ht">
      <div className="ht-head">
        <div>
          <div className="ht-kick">Histórico anual · Acme</div>
          <h2 className="ht-titulo">Mês a mês · {hist?.ano ?? ano}</h2>
          <p className="ht-sub">Investimento, funil e resultado de cada mês. Clica num mês pra abrir por corretora.</p>
        </div>
        <div className="ht-acoes">
          <select
            className="ht-ano"
            value={ano}
            onChange={(e) => {
              const a = Number(e.target.value);
              setAno(a);
              carregar(a);
            }}
          >
            {[anoAtual, anoAtual - 1].map((a) => (
              <option key={a} value={a}>{a}</option>
            ))}
          </select>
          <button className="ht-reload" onClick={() => carregar(ano)} title="atualizar" disabled={loading}>↻</button>
        </div>
      </div>

      {loading && !hist && (
        <div className="ht-loading">Recalculando o ano inteiro no copiloto… isso pode levar até 2 minutos. ☕</div>
      )}
      {erro && !hist && !loading && (
        <div className="ht-vazio">Sem dados agora ({erro}). Tenta recarregar em instantes.</div>
      )}

      {hist && (
        <>
          <div className="ht-kpis">
            <div className="ht-kpi dark">
              <div className="ht-kpi-lbl">Investimento no ano</div>
              <div className="ht-kpi-val">{brl(totais?.spend)}</div>
              <div className="ht-kpi-sub">{num(totais?.leads_meta)} leads Meta · CPL {totais?.cpl != null ? brl(totais.cpl) : "—"}</div>
            </div>
            <div className="ht-kpi accent">
              <div className="ht-kpi-lbl">VGV no ano</div>
              <div className="ht-kpi-val">{brl(totais?.vgv)}</div>
              <div className="ht-kpi-sub">{num(totais?.vendas)} venda(s) · ticket {totais?.ticket_medio != null ? brl(totais.ticket_medio) : "—"}</div>
            </div>
            <div className="ht-kpi">
              <div className="ht-kpi-lbl">ROI</div>
              <div className="ht-kpi-val">{totais?.roi != null ? `${num(Math.round(totais.roi))}×` : "—"}</div>
              <div className="ht-kpi-sub">VGV / investimento</div>
            </div>
            <div className="ht-kpi">
              <div className="ht-kpi-lbl">Funil no ano</div>
              <div className="ht-kpi-val">{num(totais?.leads)}</div>
              <div className="ht-kpi-sub">{num(totais?.atendidos)} atend. · {num(totais?.agendados)} agend. · {num(totais?.visitas)} visitas</div>
            </div>
          </div>

          <HtGraficoVgv meses={hist.meses || []} />

          <div className="ht-tabela-wrap">
            <table className="ht-tabela">
              <thead>
                <tr>
                  <th>Mês</th><th>Invest.</th><th>Leads Meta</th><th>CPL</th><th>Leads</th>
                  <th>Atend.</th><th>Agend.</th><th>Visitas</th><th>Vendas</th><th>VGV</th><th>ROI</th>
                </tr>
              </thead>
              <tbody>
                {(hist.meses || []).map((mm: any) => {
                  const e = mm.equipe || {};
                  const ma = mm.meta_ads || {};
                  const on = aberto === mm.ano_mes;
                  return (
                    <Fragment key={mm.ano_mes}>
                      <tr
                        className={`ht-mes ${on ? "on" : ""}`}
                        onClick={() => setAberto(on ? null : mm.ano_mes)}
                        title="clica pra ver por corretora"
                      >
                        <td className="ht-corr">{on ? "▾" : "▸"} {mm.label}</td>
                        <td>{brl(ma.spend)}</td>
                        <td>{num(ma.leads_meta)}</td>
                        <td>{ma.cpl != null ? brl(ma.cpl) : "—"}</td>
                        <td>{num(e.leads)}</td>
                        <td>{num(e.atendidos_evolution || e.atendidos)}</td>
                        <td>{num(e.agendados)}</td>
                        <td>{num(e.visitas)}</td>
                        <td>{num(e.vendas)}</td>
                        <td>{brl(e.vgv)}</td>
                        <td>{e.roi != null ? `${num(Math.round(e.roi))}×` : "—"}</td>
                      </tr>
                      {on && (mm.por_corretora || []).length > 0 && (
                        <tr className="ht-detalhe">
                          <td colSpan={11}>
                            <table className="ht-subtabela">
                              <thead>
                                <tr>
                                  <th>Corretora</th><th>Leads</th><th>Atend.</th><th>Agend.</th><th>Visitas</th>
                                  <th>Vendas</th><th>VGV</th><th>Atendeu %</th><th>Agendou %</th><th>Visitou %</th>
                                </tr>
                              </thead>
                              <tbody>
                                {mm.por_corretora.map((c: any) => (
                                  <tr key={c.corretora_id}>
                                    <td className="ht-corr">{c.label}</td>
                                    <td>{num(c.leads)}</td>
                                    <td>{num(c.atendidos_evolution || c.atendidos)}</td>
                                    <td>{num(c.agendados)}</td>
                                    <td>{num(c.visitas)}</td>
                                    <td>{num(c.vendas)}</td>
                                    <td>{brl(c.vgv)}</td>
                                    <td>{pct(c.conv_atendeu)}</td>
                                    <td>{pct(c.conv_agendou)}</td>
                                    <td>{pct(c.conv_visitou)}</td>
                                  </tr>
                                ))}
                              </tbody>
                            </table>
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  );
                })}
                {totais && (
                  <tr className="ht-total">
                    <td className="ht-corr">Total {hist.ano}</td>
                    <td>{brl(totais.spend)}</td>
                    <td>{num(totais.leads_meta)}</td>
                    <td>{totais.cpl != null ? brl(totais.cpl) : "—"}</td>
                    <td>{num(totais.leads)}</td>
                    <td>{num(totais.atendidos)}</td>
                    <td>{num(totais.agendados)}</td>
                    <td>{num(totais.visitas)}</td>
                    <td>{num(totais.vendas)}</td>
                    <td>{brl(totais.vgv)}</td>
                    <td>{totais.roi != null ? `${num(Math.round(totais.roi))}×` : "—"}</td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
}
