"use client";

// 📅 Visitas — agenda portada do V1 (dash-imobiliaria, view "agenda" em
// app/page.tsx), adaptada ao estilo do V2 (bg #f6f4ef, cards brancos com
// borda #e7e4dd, laranja #E8720C de acento). Backend intocado: consome
// /api/visitas → copiloto /painel/agenda.
//
// Diferenças vs V1: lista AGRUPADA POR DIA (V1 era lista corrida) e, pra
// admin, chips de filtro por corretora (padrão dos outros módulos).
// Escopo/privacidade quem resolve é o copiloto (X-Painel-User): corretora
// só recebe as visitas dela — os chips de corretora nem aparecem.
//
// AÇÕES DE STATUS: cards agendada/pendente_confirmacao têm botões
// "✓ Compareceu" / "✗ Não veio" / "Cancelar" → POST /api/visitas/status →
// copiloto /painel/visita/{id}/status (corretora só muda visita dela;
// admin muda qualquer). Atualização otimista + refetch; toast em erro.
//
// AGENDAR: botão "+ Agendar visita" abre modal (lead via busca /api/buscar,
// data/hora, empreendimento/endereço, observação; admin escolhe a corretora)
// → POST /api/visitas/criar → copiloto /painel/visitas → MESMO caminho da
// Assistente (criar_visita.py: evento 🟠 no Calendar + INSERT `visitas`).
// NÃO dispara NENHUMA mensagem ao lead — aviso fixo no modal.

import { useEffect, useMemo, useRef, useState } from "react";
import "./visitas.css";

type Visita = {
  id: number;
  corretora_id: number;
  corretora_label: string;
  lead_nome: string | null;
  data_hora_sp: string;
  data: string; // ISO YYYY-MM-DD (já em SP)
  hora: string; // "HH:MM"
  bairro: string | null;
  status: string;
  origem: string | null;
  is_revisita: boolean;
};

type Payload = {
  viewer?: { role: string };
  periodo?: { label: string; inicio: string; fim: string };
  pendentes_vencidas?: Array<{
    id: number;
    lead_nome: string | null;
    corretora_id: number | null;
    bairro: string | null;
    data_hora: string;
    dias_atras: number;
  }>;
  sem_telefone?: Array<{
    id: number;
    lead_nome: string | null;
    corretora_id: number | null;
    bairro: string | null;
    status: string;
    data_hora: string;
  }>;
  marcadas_longe?: Array<{
    id: number;
    lead_nome: string | null;
    corretora_id: number | null;
    bairro: string | null;
    data_hora: string;
    antecedencia_dias: number;
    falta_dias: number;
  }>;
  total: number;
  por_status: Record<string, number>;
  taxa_comparecimento: number | null;
  items: Visita[];
  error?: string;
};

const num = (v: number | null | undefined) => (v == null ? "—" : v.toLocaleString("pt-BR"));
const pct = (v: number | null | undefined) =>
  v == null ? "—" : `${v.toLocaleString("pt-BR", { maximumFractionDigits: 1 })}%`;

// Cores por status — mesmas do V1, dentro da paleta do V2.
const ST: Record<string, { label: string; emo: string; c: string; b: string }> = {
  agendada: { label: "Agendada", emo: "🟠", c: "#c85e08", b: "#fff3e8" },
  compareceu: { label: "Compareceu", emo: "🟢", c: "#1a6b3a", b: "#eef7f0" },
  no_show: { label: "No-show", emo: "🔴", c: "#c0392b", b: "#fdeef0" },
  cancelada: { label: "Cancelada", emo: "⚪", c: "#888", b: "#f2f2f2" },
  pendente_confirmacao: { label: "Pendente", emo: "🟡", c: "#9a7000", b: "#fff8e8" },
};

// Status que aceitam ação de marcação no card
const ACIONAVEIS = new Set(["agendada", "pendente_confirmacao"]);

function hojeISO(): string {
  return new Date().toLocaleDateString("en-CA", { timeZone: "America/Sao_Paulo" });
}

// Registro retroativo: visita que já aconteceu e não estava na agenda entra
// até 30 dias depois (mesma janela do copiloto — _VISITA_RETROATIVO_MAX_DIAS
// em painel.py; se mudar lá, muda aqui). Ela nasce `agendada` com data no
// passado, então cai direto no bloco "vencidas em aberto" lá de cima, com os
// botões ✓/✗ ao lado — registrar e fechar são dois cliques seguidos.
const RETROATIVO_MAX_DIAS = 30;

function dataMinimaISO(): string {
  const d = new Date(`${hojeISO()}T12:00:00-03:00`);
  d.setDate(d.getDate() - RETROATIVO_MAX_DIAS);
  return d.toLocaleDateString("en-CA", { timeZone: "America/Sao_Paulo" });
}

// Opções de corretora pro admin — mesma convenção fixa do copiloto
// (painel.py: CORRETORA_LABEL = {1: "Pessoa10", 2: "Pessoa11", 3: "Pessoa12"}).
const CORRETORAS_OPCOES = [
  { id: 1, label: "Pessoa10" },
  { id: 2, label: "Pessoa11" },
  { id: 3, label: "Pessoa12" },
];

// Lead devolvido pela busca /api/buscar (copiloto /painel/atendimento/busca)
type LeadBusca = {
  lead_id: string;
  nome: string;
  telefone: string | null;
  corretora_id: number | null;
  corretora_label: string | null;
};

// "sex, 18/07" — cabeçalho do grupo do dia (Hoje/Amanhã quando for o caso)
function diaLabel(iso: string): string {
  const hoje = hojeISO();
  if (iso === hoje) return "Hoje";
  try {
    const d = new Date(`${iso}T12:00:00-03:00`);
    const amanha = new Date(`${hoje}T12:00:00-03:00`);
    amanha.setDate(amanha.getDate() + 1);
    if (iso === amanha.toLocaleDateString("en-CA", { timeZone: "America/Sao_Paulo" })) return "Amanhã";
    return d.toLocaleDateString("pt-BR", { weekday: "short", day: "2-digit", month: "2-digit" });
  } catch {
    return iso;
  }
}

export default function VisitasPage() {
  const [ag, setAg] = useState<Payload | null>(null);
  const [loading, setLoading] = useState(false);
  const [erro, setErro] = useState<string | null>(null);
  const [periodo, setPeriodo] = useState("semana");
  const [fStatus, setFStatus] = useState("");
  const [fCorretora, setFCorretora] = useState("");
  const [mutando, setMutando] = useState<number | null>(null);
  const [toast, setToast] = useState<{ msg: string; erro?: boolean } | null>(null);
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // modal "+ Agendar visita"
  const [agendando, setAgendando] = useState(false);
  const [agNome, setAgNome] = useState("");
  const [agLead, setAgLead] = useState<LeadBusca | null>(null); // lead vinculado (opcional)
  const [agBusca, setAgBusca] = useState<LeadBusca[]>([]);
  const [agBuscando, setAgBuscando] = useState(false);
  const [agData, setAgData] = useState(hojeISO());
  const [agHora, setAgHora] = useState("");
  const [agEmpreend, setAgEmpreend] = useState("");
  const [agObs, setAgObs] = useState("");
  const [agCorretora, setAgCorretora] = useState(""); // id em string — só admin
  const [agSalvando, setAgSalvando] = useState(false);
  const [agErro, setAgErro] = useState<string | null>(null);
  const buscaTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  // data anterior a hoje = registro de visita que JÁ aconteceu. Muda o texto
  // do modal inteiro: não é "agendar", é "lançar o que já rolou".
  const agRetroativa = agData !== "" && agData < hojeISO();

  // busca de lead por nome/telefone (debounce 350ms) — escopo da corretora é
  // o copiloto quem resolve (corretora só acha leads dela).
  function buscarLead(q: string) {
    setAgNome(q);
    setAgLead(null);
    if (buscaTimer.current) clearTimeout(buscaTimer.current);
    if (q.trim().length < 2) {
      setAgBusca([]);
      return;
    }
    buscaTimer.current = setTimeout(async () => {
      setAgBuscando(true);
      try {
        const r = await fetch(`/api/buscar?q=${encodeURIComponent(q.trim())}`, { cache: "no-store" });
        const d = await r.json().catch(() => ({}));
        setAgBusca(Array.isArray(d?.leads) ? d.leads.slice(0, 8) : []);
      } catch {
        setAgBusca([]);
      } finally {
        setAgBuscando(false);
      }
    }, 350);
  }

  function abrirAgendar() {
    setAgNome("");
    setAgLead(null);
    setAgBusca([]);
    setAgData(hojeISO());
    setAgHora("");
    setAgEmpreend("");
    setAgObs("");
    setAgCorretora("");
    setAgErro(null);
    setAgendando(true);
  }

  async function salvarAgendamento() {
    if (agSalvando) return;
    const nome = agNome.trim();
    if (nome.length < 2 || !agData || !agHora) return;
    if (isAdmin && !agCorretora && !agLead?.corretora_id) {
      setAgErro("escolhe a corretora da visita");
      return;
    }
    setAgSalvando(true);
    setAgErro(null);
    try {
      const r = await fetch("/api/visitas/criar", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          lead_nome: nome,
          data: agData,
          hora: agHora,
          telefone: agLead?.telefone ?? null,
          lead_id: agLead?.lead_id ?? null,
          empreendimento: agEmpreend.trim() || null,
          observacao: agObs.trim() || null,
          // corretora: só admin manda (corretora agenda pra ela — backend resolve)
          corretora_id: isAdmin ? Number(agCorretora || agLead?.corretora_id) : null,
        }),
      });
      const d = await r.json().catch(() => ({}));
      if (r.status === 409) throw new Error("já existe visita desse lead nessa data/hora");
      if (d?.detail === "data_muito_antiga") {
        throw new Error(`visita de mais de ${RETROATIVO_MAX_DIAS} dias atrás não entra — confere o ano e o mês`);
      }
      if (!r.ok || d?.error || d?.ok !== true) {
        throw new Error(typeof d?.detail === "string" ? d.detail : d?.error || `http_${r.status}`);
      }
      setAgendando(false);
      mostrarToast(
        d.retroativa
          ? "visita registrada ✓ — agora marca lá em cima se a pessoa foi"
          : d.is_revisita
            ? "visita agendada ✓ (re-visita)"
            : "visita agendada ✓",
      );
      carregar(periodo);
    } catch (e) {
      setAgErro(e instanceof Error ? e.message : "erro");
    } finally {
      setAgSalvando(false);
    }
  }

  function mostrarToast(msg: string, erro = false) {
    if (toastTimer.current) clearTimeout(toastTimer.current);
    setToast({ msg, erro });
    toastTimer.current = setTimeout(() => setToast(null), 4000);
  }

  // Ação de status: otimista (status do item + contadores) → POST → refetch.
  // Em erro: rollback pro payload anterior + toast.
  async function mudarStatus(v: Visita, status: "compareceu" | "no_show" | "cancelada") {
    if (mutando != null) return;
    if (status === "cancelada" && !window.confirm(`Cancelar a visita de ${v.lead_nome || "esse lead"}?`)) return;
    const anterior = ag;
    setMutando(v.id);
    setAg((cur) => {
      if (!cur) return cur;
      const ps = { ...cur.por_status };
      ps[v.status] = Math.max(0, (ps[v.status] ?? 1) - 1);
      ps[status] = (ps[status] ?? 0) + 1;
      return { ...cur, por_status: ps, items: cur.items.map((i) => (i.id === v.id ? { ...i, status } : i)) };
    });
    try {
      const r = await fetch("/api/visitas/status", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: v.id, status }),
      });
      const d = await r.json().catch(() => ({}));
      if (!r.ok || d?.error) throw new Error(String(d?.detail || d?.error || r.status));
      carregar(periodo); // números oficiais (taxa de comparecimento etc.)
    } catch {
      setAg(anterior); // rollback
      mostrarToast("Não consegui atualizar a visita. Tenta de novo.", true);
    } finally {
      setMutando(null);
    }
  }

  function carregar(p: string) {
    setLoading(true);
    setErro(null);
    fetch(`/api/visitas?periodo=${encodeURIComponent(p)}`, { cache: "no-store" })
      .then((r) => r.json())
      .then((d) => {
        if (d?.error) {
          setErro(d.error);
          setAg(null);
        } else {
          setAg(d);
        }
      })
      .catch(() => setErro("copiloto_indisponivel"))
      .finally(() => setLoading(false));
  }
  useEffect(() => {
    carregar(periodo);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const isAdmin = ag?.viewer?.role !== "corretora";
  const items = ag?.items ?? [];

  // Chips de corretora (só admin — corretora recebe do copiloto só as dela)
  const corretoras = useMemo(
    () => Array.from(new Set(items.map((v) => v.corretora_label).filter(Boolean))).sort(),
    [items],
  );

  // id → nome, pro bloco de vencidas (que vem do copiloto só com corretora_id).
  const nomeCorretora = (id: number | null) =>
    items.find((v) => v.corretora_id === id)?.corretora_label ?? "";

  // Filtros aplicados + agrupamento por dia (ordem cronológica)
  const grupos = useMemo(() => {
    const filtrados = items.filter(
      (v) => (!fStatus || v.status === fStatus) && (!fCorretora || v.corretora_label === fCorretora),
    );
    const porDia = new Map<string, Visita[]>();
    for (const v of filtrados) {
      const arr = porDia.get(v.data) ?? [];
      arr.push(v);
      porDia.set(v.data, arr);
    }
    return Array.from(porDia.entries())
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([dia, vs]) => ({
        dia,
        visitas: vs.slice().sort((a, b) => a.hora.localeCompare(b.hora)),
      }));
  }, [items, fStatus, fCorretora]);

  const totalFiltrado = grupos.reduce((s, g) => s + g.visitas.length, 0);
  const ps = ag?.por_status || {};

  return (
    <div className="ags">
      <div className="hoje-head">
        <div>
          <div className="hoje-kick">Agenda · Acme</div>
          <h2 className="hoje-titulo">📅 Visitas</h2>
          <p className="hoje-sub" style={{ textTransform: "capitalize" }}>
            {ag?.periodo?.label ?? "—"}
            {ag?.periodo?.inicio ? ` · ${ag.periodo.inicio} → ${ag.periodo.fim}` : ""}
          </p>
        </div>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <button
            onClick={abrirAgendar}
            style={{
              font: "inherit",
              fontSize: 13,
              fontWeight: 700,
              padding: "8px 14px",
              borderRadius: 9,
              border: "1px solid #E8720C",
              background: "#E8720C",
              color: "#fff",
              cursor: "pointer",
              whiteSpace: "nowrap",
            }}
          >
            + Agendar visita
          </button>
          <select
            className="ags-periodo"
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
      </div>

      {loading && !ag && <div className="loading" style={{ padding: 40 }}>Carregando…</div>}
      {erro && !ag && (
        <div className="hoje-vazio" style={{ marginTop: 14 }}>
          Sem dados agora ({erro}). Tenta recarregar em instantes.
        </div>
      )}

      {ag && (
        <>
          {/* topo: destaque + grade de status (porte do V1) */}
          <div className="ags-topo">
            <div className="ags-destaque">
              <div className="ags-destaque-lbl">Visitas no período</div>
              <div className="ags-big">{num(ag.total)}</div>
              {ag.taxa_comparecimento != null && (
                <div className="ags-taxa">
                  comparecimento <b>{pct(ag.taxa_comparecimento)}</b>
                </div>
              )}
            </div>
            <div className="ags-status-grid">
              {Object.keys(ST).map((k) => (
                <div key={k} className="ags-status-cel" style={{ background: ST[k].b, color: ST[k].c }}>
                  <div className="ags-status-n">{num(ps[k] ?? 0)}</div>
                  <div className="ags-status-l">{ST[k].emo} {ST[k].label}</div>
                </div>
              ))}
            </div>
          </div>

          {/* chips de status */}
          <div className="ags-chips">
            <button className={`ags-chip ${!fStatus ? "on" : ""}`} onClick={() => setFStatus("")}>
              Todas ({num(ag.total)})
            </button>
            {Object.keys(ST)
              .filter((k) => (ps[k] ?? 0) > 0)
              .map((k) => (
                <button key={k} className={`ags-chip ${fStatus === k ? "on" : ""}`} onClick={() => setFStatus(k)}>
                  {ST[k].emo} {ST[k].label} ({ps[k]})
                </button>
              ))}
          </div>

          {/* chips de corretora — só admin (corretora já vem escopada do copiloto) */}
          {isAdmin && corretoras.length > 1 && (
            <div className="ags-chips">
              <button className={`ags-chip ${!fCorretora ? "on" : ""}`} onClick={() => setFCorretora("")}>
                Time todo
              </button>
              {corretoras.map((c) => (
                <button key={c} className={`ags-chip ${fCorretora === c ? "on" : ""}`} onClick={() => setFCorretora(c)}>
                  {c}
                </button>
              ))}
            </div>
          )}

          {totalFiltrado === 0 && <div className="hoje-vazio" style={{ marginTop: 14 }}>Nenhuma visita nesse filtro.</div>}

          {/* lista agrupada por dia */}
          {/* ☎️ SEM TELEFONE — a visita não se liga a lead nenhum.
              74% das visitas de agosto nasceram assim, e sem telefone a visita
              é um registro solto: não entra no funil, não cruza com a conversa.
              Foi assim que uma lead compareceu, entregou documentação e ficou
              invisível pra casa inteira.
              A raiz já foi consertada (o sistema resolve pelo nome), mas quando
              o nome é ambíguo só a corretora sabe — e chutar é pior que vazio. */}
          {(ag?.sem_telefone?.length ?? 0) > 0 && (
            <div className="ags-semtel">
              <div className="ags-semtel-topo">
                <strong>
                  {ag!.sem_telefone!.length} visita
                  {ag!.sem_telefone!.length > 1 ? "s" : ""} sem o telefone do cliente
                </strong>
                <span>
                  sem o número a visita não se liga ao cliente e não conta no
                  funil. Me diz qual é que eu completo.
                </span>
              </div>
              <div className="ags-lista">
                {ag!.sem_telefone!.slice(0, 8).map((v) => (
                  <div key={`st-${v.id}`} className="ags-visita">
                    <div className="ags-hora">
                      {new Date(v.data_hora).toLocaleDateString("pt-BR", {
                        day: "2-digit",
                        month: "2-digit",
                      })}
                    </div>
                    <div className="ags-meio">
                      <div className="ags-nome">
                        {v.lead_nome || "—"}
                        <span className="ags-semtel-tag">{v.status}</span>
                      </div>
                      <div className="ags-sub">
                        {isAdmin && v.corretora_id ? nomeCorretora(v.corretora_id) : ""}
                        {v.bairro ? `${isAdmin && v.corretora_id ? " · " : ""}${v.bairro}` : ""}
                      </div>
                    </div>
                  </div>
                ))}
                {ag!.sem_telefone!.length > 8 && (
                  <div className="ags-semtel-mais">
                    + {ag!.sem_telefone!.length - 8} mais antigas
                  </div>
                )}
              </div>
            </div>
          )}

          {/* 📉 MARCADA LONGE — o risco medido em 13/08.
              Antecedência × comparecimento, 90 dias de dado real:
                mesmo dia 100% · amanhã 50% · 2-3 dias 64% · 4-7 dias 20%
              Não é falta de persuasão, é DISTÂNCIA: o lead esfria. Depois de
              marcada só resta confirmar na véspera — e é isso que o aviso pede.
              NÃO bloqueia: visita distante às vezes é a única opção da pessoa. */}
          {(ag?.marcadas_longe?.length ?? 0) > 0 && (
            <div className="ags-risco">
              <div className="ags-risco-topo">
                <strong>
                  {ag!.marcadas_longe!.length} visita
                  {ag!.marcadas_longe!.length > 1 ? "s" : ""} marcada
                  {ag!.marcadas_longe!.length > 1 ? "s" : ""} com folga — confirme na véspera
                </strong>
                <span>
                  visita marcada pro mesmo dia tem 100% de comparecimento; pra 4+
                  dias, 20%. Uma mensagem no dia anterior é o que dá pra fazer.
                </span>
              </div>
              <div className="ags-lista">
                {ag!.marcadas_longe!.map((v) => (
                  <div key={`r-${v.id}`} className="ags-visita">
                    <div className="ags-hora">
                      {new Date(v.data_hora).toLocaleDateString("pt-BR", {
                        day: "2-digit",
                        month: "2-digit",
                      })}
                    </div>
                    <div className="ags-meio">
                      <div className="ags-nome">
                        {v.lead_nome || "—"}
                        <span className="ags-risco-tag">
                          {v.falta_dias === 0
                            ? "é hoje"
                            : v.falta_dias === 1
                              ? "é amanhã"
                              : `em ${v.falta_dias} dias`}
                        </span>
                      </div>
                      <div className="ags-sub">
                        marcada com {v.antecedencia_dias} dias de antecedência
                        {isAdmin && v.corretora_id
                          ? ` · ${nomeCorretora(v.corretora_id)}`
                          : ""}
                        {v.bairro ? ` · ${v.bairro}` : ""}
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* ⚠️ VENCIDAS EM ABERTO — fora do filtro de propósito.
              Visita passada sem fechamento é SEMPRE de dias atrás, então o
              filtro de período escondia exatamente o que precisa de ação. Foi
              o que travou a Pessoa10 em 11/08: a tela abria em "semana", as visitas
              dela eram da semana anterior, e a lista vinha vazia — parecia que
              o botão não funcionava.
              Fica no topo, com os mesmos botões, e some sozinho quando zera. */}
          {(ag?.pendentes_vencidas?.length ?? 0) > 0 && (
            <div className="ags-vencidas">
              <div className="ags-vencidas-topo">
                <strong>
                  {ag!.pendentes_vencidas!.length} visita
                  {ag!.pendentes_vencidas!.length > 1 ? "s" : ""} já {ag!.pendentes_vencidas!.length > 1 ? "passaram" : "passou"} e {ag!.pendentes_vencidas!.length > 1 ? "esperam" : "espera"} seu registro
                </strong>
                <span>aparecem aqui em qualquer período — a pessoa foi ou não foi?</span>
              </div>
              <div className="ags-lista">
                {ag!.pendentes_vencidas!.map((v) => (
                  <div key={`p-${v.id}`} className="ags-visita">
                    <div className="ags-hora">
                      {new Date(v.data_hora).toLocaleDateString("pt-BR", {
                        day: "2-digit",
                        month: "2-digit",
                      })}
                    </div>
                    <div className="ags-meio">
                      <div className="ags-nome">
                        {v.lead_nome || "—"}
                        <span className="ags-atraso">
                          {v.dias_atras === 0 ? "ontem" : `há ${v.dias_atras} dias`}
                        </span>
                      </div>
                      <div className="ags-sub">
                        {isAdmin && v.corretora_id ? nomeCorretora(v.corretora_id) : ""}
                        {v.bairro ? `${isAdmin && v.corretora_id ? " · " : ""}${v.bairro}` : ""}
                      </div>
                      <div className="ags-acoes">
                        <button
                          className="ags-acao ok"
                          disabled={mutando === v.id}
                          onClick={() => mudarStatus({ id: v.id } as Visita, "compareceu")}
                        >
                          ✓ Compareceu
                        </button>
                        <button
                          className="ags-acao nf"
                          disabled={mutando === v.id}
                          onClick={() => mudarStatus({ id: v.id } as Visita, "no_show")}
                        >
                          ✗ Não veio
                        </button>
                        <button
                          className="ags-acao cx"
                          disabled={mutando === v.id}
                          onClick={() => mudarStatus({ id: v.id } as Visita, "cancelada")}
                        >
                          Cancelar
                        </button>
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {grupos.map((g) => (
            <div key={g.dia} className="ags-dia">
              <div className={`ags-dia-head ${g.dia === hojeISO() ? "hoje" : ""}`}>
                <span className="ags-dia-lbl">{diaLabel(g.dia)}</span>
                <span className="ags-dia-n">{g.visitas.length} visita{g.visitas.length > 1 ? "s" : ""}</span>
              </div>
              <div className="ags-lista">
                {g.visitas.map((v) => {
                  const s = ST[v.status] || { label: v.status, emo: "•", c: "#666", b: "#f2f2f2" };
                  return (
                    <div key={v.id} className="ags-visita">
                      <div className="ags-hora">{v.hora}</div>
                      <div className="ags-meio">
                        <div className="ags-nome">
                          {v.lead_nome || "—"}
                          {v.is_revisita && <span className="ags-revisita">revisita</span>}
                        </div>
                        <div className="ags-sub">
                          {isAdmin && v.corretora_label ? v.corretora_label : ""}
                          {v.bairro ? `${isAdmin && v.corretora_label ? " · " : ""}${v.bairro}` : ""}
                        </div>
                        {ACIONAVEIS.has(v.status) && (
                          <div className="ags-acoes">
                            <button
                              className="ags-acao ok"
                              disabled={mutando === v.id}
                              onClick={() => mudarStatus(v, "compareceu")}
                            >
                              ✓ Compareceu
                            </button>
                            <button
                              className="ags-acao nf"
                              disabled={mutando === v.id}
                              onClick={() => mudarStatus(v, "no_show")}
                            >
                              ✗ Não veio
                            </button>
                            <button
                              className="ags-acao cx"
                              disabled={mutando === v.id}
                              onClick={() => mudarStatus(v, "cancelada")}
                            >
                              Cancelar
                            </button>
                          </div>
                        )}
                      </div>
                      <span className="ags-badge" style={{ background: s.b, color: s.c }}>
                        {s.emo} {s.label}
                      </span>
                    </div>
                  );
                })}
              </div>
            </div>
          ))}
        </>
      )}

      {/* modal "+ Agendar visita" — mesmo caminho da Assistente (Calendar+banco);
          NÃO envia mensagem nenhuma ao lead (aviso fixo abaixo). */}
      {agendando && (
        <div
          onClick={() => !agSalvando && setAgendando(false)}
          style={{ position: "fixed", inset: 0, zIndex: 90, background: "rgba(15,23,42,.45)", display: "flex", alignItems: "center", justifyContent: "center" }}
        >
          <div
            onClick={(e) => e.stopPropagation()}
            style={{ width: "min(460px, calc(100vw - 32px))", maxHeight: "calc(100vh - 48px)", overflowY: "auto", background: "#fff", border: "1px solid #e7e4dd", borderRadius: 14, padding: 16, display: "flex", flexDirection: "column", gap: 10, boxShadow: "0 12px 40px rgba(0,0,0,.18)" }}
          >
            <div style={{ fontSize: 14, fontWeight: 700, color: "#15110d" }}>
              {agRetroativa ? "📅 Registrar visita que já aconteceu" : "📅 Agendar visita"}
            </div>

            {/* lead: busca por nome (escopo da corretora resolvido no copiloto) */}
            <div style={{ position: "relative" }}>
              <input
                value={agNome}
                onChange={(e) => buscarLead(e.target.value)}
                placeholder="Nome do lead — digita pra buscar nos seus leads"
                autoFocus
                style={{ width: "100%", boxSizing: "border-box", font: "inherit", fontSize: 13, padding: "9px 11px", borderRadius: 8, border: "1px solid #e7e4dd", background: "#f6f4ef", color: "#15110d" }}
              />
              {agBusca.length > 0 && !agLead && (
                <div style={{ position: "absolute", top: "100%", left: 0, right: 0, zIndex: 5, background: "#fff", border: "1px solid #e7e4dd", borderRadius: 10, marginTop: 4, boxShadow: "0 8px 24px rgba(0,0,0,.12)", overflow: "hidden" }}>
                  {agBusca.map((l) => (
                    <div
                      key={l.lead_id}
                      onClick={() => {
                        setAgLead(l);
                        setAgNome(l.nome);
                        setAgBusca([]);
                        if (l.corretora_id) setAgCorretora(String(l.corretora_id));
                      }}
                      style={{ padding: "8px 12px", cursor: "pointer", borderBottom: "1px solid #f2efe8", fontSize: 13 }}
                    >
                      <b style={{ color: "#15110d" }}>{l.nome}</b>
                      <span style={{ color: "#8a857c", marginLeft: 8, fontSize: 12 }}>
                        {l.telefone || "sem telefone"}
                        {l.corretora_label ? ` · ${l.corretora_label}` : ""}
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </div>
            <div style={{ fontSize: 11, color: agLead ? "#1a6b3a" : "#a9a396", marginTop: -4 }}>
              {agBuscando
                ? "buscando…"
                : agLead
                  ? `✓ vinculado ao lead ${agLead.nome}${agLead.telefone ? ` (${agLead.telefone})` : ""}`
                  : "sem match? segue com o nome digitado — a visita é criada mesmo assim"}
            </div>

            {/* corretora — só admin (corretora agenda sempre pra ela) */}
            {isAdmin && (
              <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <label style={{ fontSize: 11, color: "#8a857c" }}>corretora</label>
                <select
                  value={agCorretora}
                  onChange={(e) => setAgCorretora(e.target.value)}
                  style={{ font: "inherit", fontSize: 13, padding: "6px 10px", borderRadius: 8, border: "1px solid #e7e4dd", background: "#f6f4ef", color: "#15110d" }}
                >
                  <option value="">escolhe…</option>
                  {CORRETORAS_OPCOES.map((c) => (
                    <option key={c.id} value={c.id}>{c.label}</option>
                  ))}
                </select>
              </div>
            )}

            <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
              <label style={{ fontSize: 11, color: "#8a857c" }}>quando</label>
              <input
                type="date"
                value={agData}
                min={dataMinimaISO()}
                title={`pode ser data passada — até ${RETROATIVO_MAX_DIAS} dias atrás`}
                onChange={(e) => setAgData(e.target.value)}
                style={{ font: "inherit", fontSize: 13, padding: "6px 10px", borderRadius: 8, border: "1px solid #e7e4dd", background: "#f6f4ef", color: "#15110d" }}
              />
              <input
                type="time"
                value={agHora}
                onChange={(e) => setAgHora(e.target.value)}
                style={{ font: "inherit", fontSize: 13, padding: "6px 10px", borderRadius: 8, border: "1px solid #e7e4dd", background: "#f6f4ef", color: "#15110d" }}
              />
            </div>

            <input
              value={agEmpreend}
              onChange={(e) => setAgEmpreend(e.target.value)}
              placeholder="Empreendimento / endereço (opcional)"
              style={{ font: "inherit", fontSize: 13, padding: "9px 11px", borderRadius: 8, border: "1px solid #e7e4dd", background: "#f6f4ef", color: "#15110d" }}
            />
            <textarea
              value={agObs}
              onChange={(e) => setAgObs(e.target.value)}
              placeholder="Observação (opcional) — vai só pro evento da agenda"
              rows={2}
              style={{ font: "inherit", fontSize: 13, padding: "9px 11px", borderRadius: 8, border: "1px solid #e7e4dd", background: "#f6f4ef", color: "#15110d", resize: "vertical" }}
            />

            {/* aviso fixo: agendar NÃO manda mensagem (lei da Dona). Na
                retroativa o aviso é outro: nada a combinar, a visita já foi —
                o que importa é que ela vai pedir a marcação ✓/✗. */}
            {agRetroativa ? (
              <div style={{ fontSize: 11.5, color: "#1a6b3a", background: "#eef7f0", border: "1px solid #cfe6d7", borderRadius: 8, padding: "7px 10px" }}>
                ⏪ Data no passado: vai entrar como <b>visita a marcar</b>, no topo da
                tela — aí você diz se a pessoa <b>compareceu</b> ou <b>não veio</b>.
                Nenhuma mensagem é enviada.
              </div>
            ) : (
              <div style={{ fontSize: 11.5, color: "#8a6d1f", background: "#f6edd6", border: "1px solid #e9dcb4", borderRadius: 8, padding: "7px 10px" }}>
                ℹ️ Agendar aqui <b>não envia nenhuma mensagem</b> ao lead — só registra
                na agenda (Calendar) e no painel. Combinar a visita é com você.
              </div>
            )}

            {agErro && (
              <div style={{ fontSize: 11.5, color: "#b3261e" }}>
                {agRetroativa ? "não registrou" : "não agendou"} ({agErro})
              </div>
            )}
            <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
              <button
                onClick={() => setAgendando(false)}
                disabled={agSalvando}
                style={{ font: "inherit", fontSize: 12.5, fontWeight: 600, padding: "6px 14px", borderRadius: 8, border: "1px solid #e7e4dd", background: "#fff", color: "#6b6355", cursor: agSalvando ? "default" : "pointer" }}
              >
                cancelar
              </button>
              <button
                onClick={salvarAgendamento}
                disabled={agSalvando || agNome.trim().length < 2 || !agData || !agHora || (isAdmin && !agCorretora)}
                style={{ font: "inherit", fontSize: 12.5, fontWeight: 700, padding: "6px 14px", borderRadius: 8, border: "1px solid #E8720C", background: "#E8720C", color: "#fff", cursor: "pointer", opacity: agSalvando || agNome.trim().length < 2 || !agData || !agHora || (isAdmin && !agCorretora) ? 0.5 : 1 }}
              >
                {agSalvando
                  ? agRetroativa ? "registrando…" : "agendando…"
                  : agRetroativa ? "Registrar" : "Agendar"}
              </button>
            </div>
          </div>
        </div>
      )}

      {toast && (
        <div
          style={{
            position: "fixed",
            bottom: 24,
            left: "50%",
            transform: "translateX(-50%)",
            background: toast.erro ? "#b3261e" : "#15110d",
            color: "#fff",
            padding: "10px 18px",
            borderRadius: 10,
            fontSize: 13,
            fontWeight: 600,
            boxShadow: "0 6px 20px rgba(0,0,0,.2)",
            zIndex: 80,
          }}
        >
          {toast.msg}
        </div>
      )}
    </div>
  );
}
