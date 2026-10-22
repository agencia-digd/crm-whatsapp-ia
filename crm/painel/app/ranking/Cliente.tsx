"use client";

// 🏆 RANKING de corretoras (visão gestão) — consome /api/ranking →
// copiloto /painel/ranking. O V1 não tinha tela própria (endpoint já
// existia no backend); aqui vira painel: 5 categorias (atendimento%,
// agendamento%, comparecimento%, vendas, VGV) com barras CSS e medalhas.
// Admin-only: a rota proxy devolve 403 (somente_gestao) pra corretora e o
// copiloto revalida via X-Painel-User (defesa em profundidade).

import { useEffect, useState } from "react";
import "./ranking.css";

const brl = (v: number | null | undefined) =>
  v == null ? "—" : v.toLocaleString("pt-BR", { style: "currency", currency: "BRL", maximumFractionDigits: 0 });
const num = (v: number | null | undefined) => (v == null ? "—" : v.toLocaleString("pt-BR"));
const pct = (v: number | null | undefined) =>
  v == null ? "—" : `${v.toLocaleString("pt-BR", { maximumFractionDigits: 1 })}%`;

type Item = { label: string; valor: number | null };

const MEDALHAS = ["🥇", "🥈", "🥉"];

// Uma categoria do ranking: lista ordenada (o copiloto já ordena desc)
// com barra proporcional ao líder e medalha pros 3 primeiros.
function RkCategoria({
  titulo,
  sub,
  itens,
  fmt,
}: {
  titulo: string;
  sub: string;
  itens: Item[];
  fmt: (v: number | null | undefined) => string;
}) {
  const max = Math.max(1, ...itens.map((i) => i.valor || 0));
  return (
    <div className="rk-cat">
      <div className="rk-cat-head">
        <h3>{titulo}</h3>
        <span className="rk-mono">{sub}</span>
      </div>
      {itens.length === 0 && <div className="rk-vazio-cat">Sem dados no período.</div>}
      <div className="rk-lista">
        {itens.map((it, i) => {
          const w = Math.max(4, ((it.valor || 0) / max) * 100);
          return (
            <div key={it.label} className={`rk-row ${i === 0 ? "lider" : ""}`}>
              <span className="rk-pos">{MEDALHAS[i] ?? `${i + 1}º`}</span>
              <span className="rk-nome">{it.label}</span>
              <div className="rk-track">
                <div className={`rk-bar ${i === 0 ? "ouro" : ""}`} style={{ width: `${w}%` }} />
              </div>
              <span className="rk-valor">{fmt(it.valor)}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

export default function RankingPageCliente() {
  const [dados, setDados] = useState<any | null>(null);
  const [loading, setLoading] = useState(false);
  const [periodo, setPeriodo] = useState("mes_atual");
  const [erro, setErro] = useState<string | null>(null);

  function carregar(p: string) {
    setLoading(true);
    setErro(null);
    fetch(`/api/ranking?periodo=${encodeURIComponent(p)}`, { cache: "no-store" })
      .then(async (r) => {
        const body = await r.json().catch(() => ({ error: "resposta_invalida" }));
        if (!r.ok || body?.error) throw new Error(body?.error || body?.detail || `HTTP ${r.status}`);
        setDados(body);
      })
      .catch((e) => {
        setErro(String(e.message || e));
        setDados(null);
      })
      .finally(() => setLoading(false));
  }
  useEffect(() => {
    carregar(periodo);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (erro === "somente_gestao" || erro === "painel_admin_required") {
    return (
      <div className="rk">
        <div className="rk-vazio">🔒 O ranking da equipe é visão de gestão — fala com a Dona se precisar desses números.</div>
      </div>
    );
  }

  return (
    <div className="rk">
      <div className="rk-head">
        <div>
          <div className="rk-kick">Ranking · Acme</div>
          <h2 className="rk-titulo" style={{ textTransform: "capitalize" }}>
            {dados?.periodo?.label ?? "Corretoras"}
          </h2>
          <p className="rk-sub">
            {dados?.periodo?.inicio ?? ""}
            {dados?.periodo?.fim ? ` → ${dados.periodo.fim}` : ""}
          </p>
        </div>
        <select
          className="rk-periodo"
          value={periodo}
          onChange={(e) => {
            setPeriodo(e.target.value);
            carregar(e.target.value);
          }}
        >
          <option value="hoje">Hoje</option>
          <option value="semana">Semana</option>
          <option value="mes_atual">Mês atual</option>
          <option value="mes_passado">Mês passado</option>
        </select>
      </div>

      {loading && !dados && <div className="rk-loading">Carregando ranking…</div>}
      {erro && !dados && !loading && (
        <div className="rk-vazio">Sem dados agora ({erro}). Tenta recarregar em instantes.</div>
      )}

      {dados && (
        <div className="rk-grid">
          <RkCategoria
            titulo="🏆 VGV"
            sub="valor geral de vendas"
            itens={dados.vgv || []}
            fmt={brl}
          />
          <RkCategoria
            titulo="🤝 Vendas"
            sub="quantidade no período"
            itens={dados.vendas_qtde || []}
            fmt={num}
          />
          <RkCategoria
            titulo="📞 Atendimento"
            sub="% dos leads atendidos"
            itens={dados.atendimento || []}
            fmt={pct}
          />
          <RkCategoria
            titulo="📅 Agendamento"
            sub="% dos atendidos que agendaram"
            itens={dados.agendamento || []}
            fmt={pct}
          />
          <RkCategoria
            titulo="🏠 Comparecimento"
            sub="% dos agendados que visitaram"
            itens={dados.comparecimento || []}
            fmt={pct}
          />
        </div>
      )}
    </div>
  );
}
