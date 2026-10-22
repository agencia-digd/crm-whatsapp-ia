"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useEstadoPersistido } from "@/app/lib/estado-persistido";
import { useRouter } from "next/navigation";
import JanelaLigacao, { EstadoLigacao } from "./JanelaLigacao";

// Tarefas v2 — a corretora cria tarefa na thread do Chat (botão "☑️ Tarefa" →
// POST /api/tarefas → tabela tarefas_corretora). Antes NÃO havia tela pra
// LISTAR; o módulo "Hoje"/agenda foi removido 11/07. Esta tela preenche o
// buraco: lista as tarefas ABERTAS agrupadas em Atrasadas · Hoje · Próximas.
//
// Estilo herdado 1:1 do CRM (app/crm): full-bleed, bg #f6f4ef, topbar branca,
// cards brancos com borda #e7e4dd e :hover via <style>. Sem design novo.
//
// Escopo/privacidade: o copiloto resolve por X-Painel-User (corretora vê as
// dela, admin vê todas). O front só repassa via resolveViewer nas rotas.
//
// Deep-link: clicar no card abre a conversa no Chat. O Chat identifica pelo id
// do KOMMO — e o /painel/tarefas devolve `lead_id` = id do Kommo (external_ref,
// ex "23573806"), NÃO o id local da tarefa. Então /chat?lead=<t.lead_id>.

// A aba unifica DOIS sistemas: tarefas MANUAIS (tipo="tarefa", tabela
// tarefas_corretora — editáveis/concluíveis) + tarefas de LIGAÇÃO da CADÊNCIA
// (tipo="ligacao", geradas pelo motor D1/D5/D9 — não editáveis, só "✓ liguei").
// `id` das de ligação é NEGATIVO (=-cadencia_id) pra não colidir com as manuais.
type Tarefa = {
  id: number;
  lead_id: string; // id do KOMMO (external_ref) — chave da conversa no Chat
  lead_nome: string | null;
  corretora: string | null;
  data: string; // ISO date YYYY-MM-DD
  hora: string | null; // "HH:MM" ou null (opcional)
  texto: string;
  status: string;
  atrasada: boolean;
  anotacoes: string | null;
  mensagem_planejada: string | null; // só o CORPO (a saudação vai automática)
  tem_msg_planejada: boolean;
  msg_planejada_enviada_em: string | null; // ISO ou null (ainda não disparou)
  tipo?: "tarefa" | "ligacao"; // default "tarefa" (compat)
  cadencia_id?: number; // só nas de ligação
  toque?: number; // só nas de ligação (índice do toque D1/D5/D9)
};

// Os TRÊS resultados de uma ligação — um clique cada, na ordem em que a
// corretora encontra na vida real. "Não atendeu" é o caso comum e vem primeiro.
// Não virar formulário é requisito: se ficar pesado, ela para de registrar e a
// gente perde o dado que já tem hoje.
const BOTOES_LIGACAO: Array<{
  estado: EstadoLigacao;
  rotulo: string;
  titulo: string;
  cor: string;
  fundo: string;
  borda: string;
}> = [
  {
    estado: "nao_atendeu",
    rotulo: "🔕 não atendeu",
    titulo: "Ligou e ninguém atendeu — registra a tentativa na conversa do lead",
    cor: "#6b6355",
    fundo: "#f7f4ec",
    borda: "#e0dccf",
  },
  {
    estado: "atendeu_sem_continuar",
    rotulo: "🟡 não quis seguir",
    titulo: "Atendeu, mas não quis seguir o atendimento agora",
    cor: "#8a6d1f",
    fundo: "#faf3ea",
    borda: "#e3d6c6",
  },
  {
    estado: "atendeu_quer_continuar",
    rotulo: "⭐ quer continuar",
    titulo: "Atendeu e quer continuar — o lead passa a ser seu",
    cor: "#fff",
    fundo: "#1a6b3a",
    borda: "#1a6b3a",
  },
];

const REFRESH_MS = 60_000;
// corretoras: nome do DB (vai no ?corretora=) → rótulo curto do botão.
// Mesma lista/ordem do Chat pra não divergir de tela pra tela.
//
// ⚠️ Lista FIXA: corretora nova fica invisível aqui até alguém editar o código,
// mesmo já aparecendo nas métricas do backend (que lê do banco). Foi o que
// aconteceu com a Pessoa7 em 09/09 — ela tinha 16 ligações registradas e nenhum
// botão pra ver isso. Quando entrar a terceira nova, vale trocar por uma lista
// vinda da API em vez de continuar somando linha aqui.
const CORRETORAS: Array<{ nome: string; label: string }> = [
  { nome: "Pessoa5", label: "Pessoa10" },
  { nome: "Pessoa1", label: "Pessoa11" },
  { nome: "Pessoa3", label: "Pessoa12" },
  { nome: "Pessoa7", label: "Pessoa7" },
  { nome: "Corretor", label: "Corretor" },
];

// data local de São Paulo em ISO YYYY-MM-DD (mesma referência do copiloto)
function hojeISO(): string {
  return new Date().toLocaleDateString("en-CA", { timeZone: "America/Sao_Paulo" });
}

// Saudação TRAVADA da mensagem planejada — espelha o greeting do template UTILITY
// da Pessoa12 (com primeiro nome → "Olá {nome}, tudo bem?"; sem nome → "Oi, tudo bem?").
const _NAO_NOME = new Set(["", "0", "-", "lead", "cliente", "contato", "null", "none", "nan"]);
function saudacaoTravada(leadNome: string | null | undefined): string {
  const tok = (leadNome ?? "").trim().split(/\s+/)[0] ?? "";
  const valido = tok !== "" && !_NAO_NOME.has(tok.toLowerCase()) && /[a-zA-ZÀ-ÿ]/.test(tok);
  return valido ? `Olá ${tok}, tudo bem?` : "Oi, tudo bem?";
}

function fmtData(iso: string): string {
  const [y, m, d] = iso.split("-");
  return `${d}/${m}/${y.slice(2)}`;
}

type Grupo = { chave: "atrasadas" | "hoje" | "proximas"; titulo: string; emoji: string; cor: string; bg: string; itens: Tarefa[] };

// Item do histórico de concluídas (GET /api/tarefas/concluidas → copiloto
// /painel/tarefas/concluidas, paginado, mais recente primeiro).
type TarefaConcluida = {
  id: number;
  lead_id: string;
  lead_nome: string | null;
  corretora: string | null;
  data: string; // data planejada da tarefa
  texto: string;
  autor: string | null;
  concluido_em: string | null; // ISO
  concluido_por: string | null; // login do painel; null = pré-migration 0062
};

function fmtDataHora(iso: string | null): string {
  if (!iso) return "—";
  try {
    return new Date(iso.endsWith("Z") || iso.includes("+") ? iso : `${iso}Z`).toLocaleString("pt-BR", {
      day: "2-digit",
      month: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      timeZone: "America/Sao_Paulo",
    });
  } catch {
    return iso;
  }
}

export default function TarefasPage() {
  const [tarefas, setTarefas] = useState<Tarefa[] | null>(null);
  const [erro, setErro] = useState<string | null>(null);
  const [isAdmin, setIsAdmin] = useState(false);
  const [atualizadoEm, setAtualizadoEm] = useState<Date | null>(null);
  const [concluindo, setConcluindo] = useState<Set<number>>(new Set());
  const [toast, setToast] = useState<{ msg: string; erro?: boolean } | null>(null);
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const router = useRouter();

  // edição de tarefa aberta — modal pré-preenchido (herda o modal do Chat)
  const [editando, setEditando] = useState<Tarefa | null>(null);
  const [edTexto, setEdTexto] = useState("");
  const [edData, setEdData] = useState("");
  const [edHora, setEdHora] = useState(""); // opcional "HH:MM"; vazio → sem hora
  const [edAnot, setEdAnot] = useState("");
  const [edMsgOn, setEdMsgOn] = useState(false);
  const [edMsg, setEdMsg] = useState(""); // corpo (sem a saudação travada)
  const [salvandoEd, setSalvandoEd] = useState(false);
  const [edErro, setEdErro] = useState<string | null>(null);

  // aba Abertas | Concluídas | Ligar
  // "Ligar" é a JANELA DE LIGAÇÃO (fila do remarketing sem dono). Fica aqui, e
  // não em tela própria, porque é o mesmo gesto mental da corretora nesta tela
  // ("o que eu faço agora") e reusa as pills/toast que já existem.
  const [vista, setVista] = useState<"abertas" | "concluidas" | "ligar">("abertas");
  const [filtro, setFiltro] = useState(""); // busca por nome do lead / texto / corretora
  // Pills de corretora (só admin) — MESMO padrão da lista do Chat: nome do DB
  // no fetch, rótulo curto no botão. Filtro é do SERVIDOR (a aba Concluídas é
  // paginada; filtrar no front peneiraria só a página atual).
  const [corrFiltro, setCorrFiltro] = useEstadoPersistido<string | null>("tarefas_corretora_v1", null);
  const [concluidas, setConcluidas] = useState<TarefaConcluida[] | null>(null);
  const [concPage, setConcPage] = useState(1);
  const [concTotal, setConcTotal] = useState(0);
  const [concTemMais, setConcTemMais] = useState(false);
  const [concErro, setConcErro] = useState<string | null>(null);
  const [concCarregando, setConcCarregando] = useState(false);
  const POR_PAGINA = 20;

  // `?aba=ligar` abre direto na janela de ligação — é o atalho do
  // "📞 ligações [ começar ]" da tela 👋 Meu dia. Vai num efeito (e não no
  // inicializador do useState) porque a página é SSR: ler window.location no
  // primeiro render daria hydration mismatch.
  useEffect(() => {
    const aba = new URLSearchParams(window.location.search).get("aba");
    if (aba === "ligar" || aba === "concluidas") setVista(aba);
  }, []);

  const carregarConcluidas = useCallback(async (page: number, corr: string | null = corrFiltro) => {
    setConcCarregando(true);
    setConcErro(null);
    try {
      const qs = corr ? `&corretora=${encodeURIComponent(corr)}` : "";
      const r = await fetch(`/api/tarefas/concluidas?page=${page}&por_pagina=${POR_PAGINA}${qs}`, { cache: "no-store" });
      const data = await r.json();
      if (!r.ok) throw new Error(data.error ?? `http_${r.status}`);
      setConcluidas(Array.isArray(data.tarefas) ? data.tarefas : []);
      setConcTotal(Number(data.total ?? 0));
      setConcTemMais(Boolean(data.tem_mais));
      setConcPage(page);
    } catch (e) {
      setConcErro(e instanceof Error ? e.message : "erro_desconhecido");
    } finally {
      setConcCarregando(false);
    }
  }, [corrFiltro]);

  const mostrarToast = useCallback((msg: string, erroT = false) => {
    if (toastTimer.current) clearTimeout(toastTimer.current);
    setToast({ msg, erro: erroT });
    toastTimer.current = setTimeout(() => setToast(null), 4000);
  }, []);

  const carregar = useCallback(async (corr: string | null = corrFiltro) => {
    try {
      // escopo=todas: hoje + atrasadas + futuras; o agrupamento é no front.
      const qs = corr ? `&corretora=${encodeURIComponent(corr)}` : "";
      const r = await fetch(`/api/tarefas?escopo=todas${qs}`, { cache: "no-store" });
      const data = await r.json();
      if (!r.ok) throw new Error(data.error ?? `http_${r.status}`);
      const lista: Tarefa[] = Array.isArray(data.tarefas) ? data.tarefas : [];
      // Modo gestão (admin) SEMPRE mostra de quem é a tarefa — flag vem do
      // proxy (viewer_admin). Fallback antigo (>1 corretora distinta) mantido
      // pra compat se o proxy ainda não mandar a flag.
      // viewer_admin vem do proxy. NÃO derivar de "mais de uma corretora na
      // lista": com uma pill ativa só existe uma, e as pills desapareceriam.
      const corrs = new Set(lista.map((t) => t.corretora).filter(Boolean));
      setIsAdmin((prev) => Boolean(data.viewer_admin) || corrs.size > 1 || prev);
      setTarefas(lista);
      setErro(null);
      setAtualizadoEm(new Date());
    } catch (e) {
      setErro(e instanceof Error ? e.message : "erro_desconhecido");
    }
  }, [corrFiltro]);

  useEffect(() => {
    carregar();
    const t = setInterval(() => {
      if (document.visibilityState === "visible") carregar();
    }, REFRESH_MS);
    return () => clearInterval(t);
  }, [carregar]);

  const abrirNoChat = useCallback(
    (t: Tarefa) => {
      if (!t.lead_id) {
        mostrarToast("essa tarefa não tem lead vinculado no Chat");
        return;
      }
      router.push(
        `/chat?lead=${encodeURIComponent(t.lead_id)}&nome=${encodeURIComponent(t.lead_nome ?? "Lead")}`,
      );
    },
    [router, mostrarToast],
  );

  const concluir = useCallback(
    async (t: Tarefa, resultado?: EstadoLigacao) => {
      setConcluindo((s) => new Set(s).add(t.id));
      // remoção otimista (com rollback): a tarefa some da lista na hora.
      const snapshot = tarefas;
      setTarefas((prev) => (prev ? prev.filter((x) => x.id !== t.id) : prev));
      try {
        const r = await fetch("/api/tarefas/concluir", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ id: t.id, resultado: resultado ?? null }),
        });
        const data = await r.json().catch(() => ({}));
        if (!r.ok || data.ok !== true) {
          throw new Error(
            typeof data.detail === "string" ? data.detail : data.error ?? `http_${r.status}`,
          );
        }
        mostrarToast("tarefa concluída ✓");
      } catch (e) {
        setTarefas(snapshot); // rollback
        mostrarToast(`não consegui concluir (${e instanceof Error ? e.message : "erro"})`, true);
      } finally {
        setConcluindo((s) => {
          const n = new Set(s);
          n.delete(t.id);
          return n;
        });
      }
    },
    [tarefas, mostrarToast],
  );

  // Registro da LIGAÇÃO da cadência (tipo="ligacao"): marca o toque como feito
  // no motor (registra evento + avança o estado) E grava o resultado — nota na
  // conversa do lead + dado da métrica. Três estados, um clique cada; sem
  // resultado a ligação ficaria "sem registro" (nunca "não atendeu").
  // Remoção otimista com rollback.
  const ligarFeito = useCallback(
    async (t: Tarefa, resultado?: EstadoLigacao) => {
      if (t.cadencia_id == null) return;
      setConcluindo((s) => new Set(s).add(t.id));
      const snapshot = tarefas;
      setTarefas((prev) => (prev ? prev.filter((x) => x.id !== t.id) : prev));
      try {
        const r = await fetch("/api/cadencia/ligacao-feita", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            cadencia_id: t.cadencia_id,
            toque: t.toque ?? null,
            resultado: resultado ?? null,
          }),
        });
        const data = await r.json().catch(() => ({}));
        if (!r.ok || data.ok !== true) {
          throw new Error(
            typeof data.motivo === "string" ? data.motivo : data.error ?? `http_${r.status}`,
          );
        }
        mostrarToast("ligação registrada ✓");
      } catch (e) {
        setTarefas(snapshot); // rollback
        mostrarToast(`não consegui registrar (${e instanceof Error ? e.message : "erro"})`, true);
      } finally {
        setConcluindo((s) => {
          const n = new Set(s);
          n.delete(t.id);
          return n;
        });
      }
    },
    [tarefas, mostrarToast],
  );

  const abrirEditar = useCallback((t: Tarefa) => {
    setEditando(t);
    setEdTexto(t.texto);
    setEdData(t.data);
    setEdHora(t.hora ?? "");
    setEdAnot(t.anotacoes ?? "");
    setEdMsgOn(!!t.mensagem_planejada);
    setEdMsg(t.mensagem_planejada ?? ""); // guardamos só o corpo
    setEdErro(null);
  }, []);

  const salvarEdicao = useCallback(async () => {
    if (!editando || salvandoEd) return;
    const texto = edTexto.trim();
    if (texto.length < 2 || !edData) return;
    setSalvandoEd(true);
    setEdErro(null);
    const id = editando.id;
    const snapshot = tarefas;
    const msgPlan = edMsgOn && edMsg.trim() ? edMsg.trim() : null;
    const anot = edAnot.trim() || null;
    const patch = {
      texto,
      data: edData,
      hora: edHora || null,
      anotacoes: anot,
      mensagem_planejada: msgPlan,
      tem_msg_planejada: !!msgPlan,
    };
    // atualização otimista (com rollback): reflete na lista na hora.
    setTarefas((prev) =>
      prev ? prev.map((x) => (x.id === id ? { ...x, ...patch, atrasada: patch.data < hojeISO() } : x)) : prev,
    );
    try {
      const r = await fetch("/api/tarefas/editar", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id, texto, data: edData, hora: edHora || null, anotacoes: anot, mensagem_planejada: msgPlan }),
      });
      const data = await r.json().catch(() => ({}));
      if (!r.ok || data.ok !== true) {
        throw new Error(
          typeof data.detail === "string" ? data.detail : data.error ?? `http_${r.status}`,
        );
      }
      setEditando(null);
      mostrarToast("tarefa atualizada ✓");
    } catch (e) {
      setTarefas(snapshot); // rollback
      setEdErro(e instanceof Error ? e.message : "erro");
    } finally {
      setSalvandoEd(false);
    }
  }, [editando, edTexto, edData, edHora, edAnot, edMsgOn, edMsg, salvandoEd, tarefas, mostrarToast]);

  // Pill no padrão visual do Chat (bege claro / escuro ativo).
  const pillStyle = (ativo: boolean): React.CSSProperties => ({
    font: "inherit",
    fontSize: 12.5,
    fontWeight: 700,
    padding: "5px 12px",
    borderRadius: 999,
    border: `1px solid ${ativo ? "#15110d" : "#e7e4dd"}`,
    background: ativo ? "#15110d" : "#fff",
    color: ativo ? "#fff" : "#6b6355",
    cursor: "pointer",
    whiteSpace: "nowrap",
  });

  // Troca a corretora e recarrega a vista atual. A aba Concluídas volta pra
  // página 1 (a paginação é do servidor e o total muda com o filtro).
  const trocarCorretora = useCallback((nome: string | null) => {
    setCorrFiltro(nome);
    setTarefas(null);
    carregar(nome);
    if (vista === "concluidas") {
      setConcluidas(null);
      carregarConcluidas(1, nome);
    }
  }, [carregar, carregarConcluidas, vista]);

  const hoje = hojeISO();
  const norm = (x: string | null | undefined) =>
    (x ?? "").toLowerCase().normalize("NFD").replace(/[\u0300-\u036f]/g, "");
  const fq = norm(filtro.trim());
  const listaTudo = tarefas ?? [];
  const lista = fq
    ? listaTudo.filter(
        (t) => norm(t.lead_nome).includes(fq) || norm(t.texto).includes(fq) || norm(t.corretora).includes(fq),
      )
    : listaTudo;
  // ordena por data, depois hora (sem hora vai por último no dia), depois id
  const ord = (arr: Tarefa[]) =>
    [...arr].sort(
      (a, b) =>
        a.data.localeCompare(b.data) ||
        (a.hora ?? "99:99").localeCompare(b.hora ?? "99:99") ||
        a.id - b.id,
    );
  const grupos: Grupo[] = [
    { chave: "atrasadas", titulo: "Atrasadas", emoji: "⏰", cor: "#b3261e", bg: "#fdecea", itens: ord(lista.filter((t) => t.atrasada || t.data < hoje)) },
    { chave: "hoje", titulo: "Hoje", emoji: "📌", cor: "#15110d", bg: "#f2efe8", itens: ord(lista.filter((t) => !t.atrasada && t.data === hoje)) },
    { chave: "proximas", titulo: "Próximas", emoji: "📅", cor: "#1a6b3a", bg: "#eef7f0", itens: ord(lista.filter((t) => !t.atrasada && t.data > hoje)) },
  ];
  const vazio = tarefas != null && listaTudo.length === 0;
  const filtroSemMatch = tarefas != null && listaTudo.length > 0 && lista.length === 0;

  return (
    // full-bleed: escapa o padding do .main do shell (36px 40px 48px), igual CRM
    <div
      style={{
        margin: "-36px -40px -48px",
        minHeight: "100vh",
        background: "#f6f4ef",
        fontFamily: "Inter, system-ui, sans-serif",
        fontSize: 15,
        lineHeight: 1.5,
      }}
    >
      <style>{`.tarefaCard:hover { border-color: #cfc9bc !important; box-shadow: 0 2px 8px rgba(0,0,0,.07); }
.concluirBtn:hover { background: #1a6b3a !important; color: #fff !important; border-color: #1a6b3a !important; }
.editarBtn:hover { background: #efe9dc !important; border-color: #cfc9bc !important; }
.edSalvarBtn:disabled { opacity: 0.45; cursor: default; }`}</style>

      {/* topbar — mesmo padrão do CRM */}
      {/* flexWrap é o que impede o deslize lateral no iPhone: sem ele, os
          filtros de corretora empurram a barra pra fora da tela e a página
          inteira passa a rolar de lado. */}
      <div style={{ display: "flex", alignItems: "center", flexWrap: "wrap", gap: 8, padding: "12px 14px", background: "#fff", borderBottom: "1px solid #e7e4dd" }}>
        <a href="/" style={{ textDecoration: "none", color: "#888", fontSize: 13 }}>← Painel</a>
        <span style={{ color: "#ccc" }}>·</span>
        <strong style={{ color: "#15110d", fontSize: 16 }}>☑️ Tarefas</strong>
        {/* toggle Abertas | Concluídas */}
        <div style={{ display: "flex", background: "#f2efe8", borderRadius: 8, padding: 2, gap: 2 }}>
          {(["abertas", "concluidas", "ligar"] as const).map((v) => (
            <button
              key={v}
              onClick={() => {
                setVista(v);
                if (v === "concluidas") carregarConcluidas(1);
              }}
              style={{
                font: "inherit",
                fontSize: 12,
                fontWeight: 700,
                padding: "4px 12px",
                borderRadius: 6,
                border: "none",
                cursor: "pointer",
                background: vista === v ? "#fff" : "transparent",
                color: vista === v ? "#15110d" : "#8a857c",
                boxShadow: vista === v ? "0 1px 3px rgba(0,0,0,.08)" : "none",
              }}
            >
              {v === "abertas" ? "Abertas" : v === "concluidas" ? "✓ Concluídas" : "📞 Ligar"}
            </button>
          ))}
        </div>
        {vista === "abertas" && (
          <span style={{ fontSize: 12, color: "#8a857c", background: "#f2efe8", borderRadius: 6, padding: "2px 8px" }}>
            suas tarefas abertas · clica no card pra abrir a conversa no Chat
          </span>
        )}
        {isAdmin && (
          <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
            <button
              onClick={() => trocarCorretora(null)}
              style={pillStyle(corrFiltro === null)}
              title="Tarefas de todas as corretoras"
            >
              Todas
            </button>
            {CORRETORAS.map((c) => (
              <button
                key={c.nome}
                onClick={() => trocarCorretora(c.nome)}
                style={pillStyle(corrFiltro === c.nome)}
                title={`Só as tarefas de ${c.nome}`}
              >
                {c.label}
              </button>
            ))}
          </div>
        )}
        {vista === "abertas" && (
          <input
            value={filtro}
            onChange={(e) => setFiltro(e.target.value)}
            placeholder="🔎 filtrar por nome…"
            style={{
              fontSize: 13, padding: "6px 10px", borderRadius: 8,
              border: "1px solid #e7e4dd", background: "#fff", color: "#15110d",
              width: 190, outline: "none",
            }}
          />
        )}
        <div style={{ flex: 1 }} />
        <span style={{ fontSize: 11.5, color: "#a9a396" }}>
          {atualizadoEm
            ? `atualizado ${atualizadoEm.toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" })} · refresh 60s`
            : "carregando…"}
        </span>
      </div>

      <div style={{ padding: "18px 18px 60px", maxWidth: vista === "ligar" ? 960 : 720 }}>
        {/* ── vista LIGAR: a janela de ligação (fila do remarketing) ── */}
        {vista === "ligar" && <JanelaLigacao isAdmin={isAdmin} onToast={mostrarToast} />}

        {/* ── vista CONCLUÍDAS: histórico paginado (quem concluiu + quando) ── */}
        {vista === "concluidas" && (
          <>
            {concErro && (
              <div style={{ color: "#a0316a", background: "#fdeef4", border: "1px solid #f3c9dd", borderRadius: 12, padding: "14px 18px", marginBottom: 16 }}>
                Não consegui carregar o histórico ({concErro}).
              </div>
            )}
            {concCarregando && concluidas == null && (
              <div style={{ color: "#8a857c", padding: 30, textAlign: "center" }}>carregando…</div>
            )}
            {concluidas != null && concluidas.length === 0 && !concErro && (
              <div style={{ maxWidth: 560, margin: "60px auto", textAlign: "center", background: "#fff", border: "1px solid #e7e4dd", borderRadius: 16, padding: "44px 32px" }}>
                <div style={{ fontSize: 44, marginBottom: 12 }}>🗂️</div>
                <div style={{ fontSize: 18, fontWeight: 700, color: "#15110d", marginBottom: 8 }}>Nenhuma tarefa concluída ainda</div>
                <div style={{ fontSize: 13.5, color: "#8a857c", lineHeight: 1.6 }}>
                  Quando você concluir tarefas na aba Abertas, o histórico aparece aqui.
                </div>
              </div>
            )}
            {concluidas != null && concluidas.length > 0 && (
              <>
                <div style={{ fontSize: 12, color: "#8a857c", marginBottom: 10 }}>
                  {concTotal} concluída{concTotal === 1 ? "" : "s"} · página {concPage}
                </div>
                <div style={{ display: "flex", flexDirection: "column", gap: 8, opacity: concCarregando ? 0.6 : 1 }}>
                  {concluidas.map((t) => (
                    <div
                      key={t.id}
                      className="tarefaCard"
                      onClick={() => t.lead_id && router.push(`/chat?lead=${encodeURIComponent(t.lead_id)}&nome=${encodeURIComponent(t.lead_nome ?? "Lead")}`)}
                      title="Clica pra abrir a conversa no Chat"
                      style={{ background: "#fff", border: "1px solid #e7e4dd", borderRadius: 12, padding: "12px 14px", cursor: "pointer" }}
                    >
                      <div style={{ display: "flex", alignItems: "baseline", gap: 8, flexWrap: "wrap" }}>
                        <span style={{ fontSize: 14, fontWeight: 700, color: "#15110d" }}>{t.lead_nome || "Lead"}</span>
                        <span style={{ fontSize: 11, fontWeight: 700, color: "#1a6b3a", background: "#eef7f0", borderRadius: 6, padding: "2px 7px" }}>
                          ✓ concluída {fmtDataHora(t.concluido_em)}
                        </span>
                        <span style={{ fontSize: 11, color: "#8a857c", background: "#f2efe8", borderRadius: 6, padding: "2px 7px" }}>
                          por {t.concluido_por || "—"}
                        </span>
                        {isAdmin && t.corretora && (
                          <span style={{ fontSize: 11, color: "#8a857c", background: "#f2efe8", borderRadius: 6, padding: "2px 7px" }}>{t.corretora}</span>
                        )}
                        <span style={{ fontSize: 11, color: "#a9a396" }}>prazo {fmtData(t.data)}</span>
                      </div>
                      <div style={{ fontSize: 13, color: "#444", marginTop: 6, whiteSpace: "pre-wrap", wordBreak: "break-word" }}>{t.texto}</div>
                    </div>
                  ))}
                </div>
                {/* paginação */}
                <div style={{ display: "flex", justifyContent: "center", gap: 10, marginTop: 16, alignItems: "center" }}>
                  <button
                    onClick={() => carregarConcluidas(concPage - 1)}
                    disabled={concPage <= 1 || concCarregando}
                    style={{ font: "inherit", fontSize: 12.5, fontWeight: 700, padding: "6px 14px", borderRadius: 8, border: "1px solid #e7e4dd", background: "#fff", color: concPage <= 1 ? "#c9c4b8" : "#6b6355", cursor: concPage <= 1 ? "default" : "pointer" }}
                  >
                    ‹ anteriores
                  </button>
                  <span style={{ fontSize: 12, color: "#8a857c" }}>{concPage}</span>
                  <button
                    onClick={() => carregarConcluidas(concPage + 1)}
                    disabled={!concTemMais || concCarregando}
                    style={{ font: "inherit", fontSize: 12.5, fontWeight: 700, padding: "6px 14px", borderRadius: 8, border: "1px solid #e7e4dd", background: "#fff", color: !concTemMais ? "#c9c4b8" : "#6b6355", cursor: !concTemMais ? "default" : "pointer" }}
                  >
                    mais antigas ›
                  </button>
                </div>
              </>
            )}
          </>
        )}

        {vista === "abertas" && erro && (
          <div style={{ color: "#a0316a", background: "#fdeef4", border: "1px solid #f3c9dd", borderRadius: 12, padding: "14px 18px", marginBottom: 16 }}>
            Não consegui carregar as tarefas ({erro}).
          </div>
        )}

        {vista === "abertas" && filtroSemMatch && (
          <div style={{ color: "#8a857c", padding: 30, textAlign: "center" }}>
            Nenhuma tarefa combina com &ldquo;{filtro.trim()}&rdquo; — limpa o filtro pra ver todas.
          </div>
        )}

        {vista === "abertas" && vazio && (
          <div style={{ maxWidth: 560, margin: "60px auto", textAlign: "center", background: "#fff", border: "1px solid #e7e4dd", borderRadius: 16, padding: "44px 32px" }}>
            <div style={{ fontSize: 44, marginBottom: 12 }}>☑️</div>
            <div style={{ fontSize: 18, fontWeight: 700, color: "#15110d", marginBottom: 8 }}>Nenhuma tarefa aberta</div>
            <div style={{ fontSize: 13.5, color: "#8a857c", lineHeight: 1.6 }}>
              Crie tarefas na conversa de um lead pelo botão ☑️ Tarefa no Chat. Elas aparecem aqui organizadas por prazo.
            </div>
          </div>
        )}

        {vista === "abertas" &&
          !vazio &&
          grupos.map((g) =>
            g.itens.length === 0 ? null : (
              <div key={g.chave} style={{ marginBottom: 26 }}>
                {/* cabeçalho GRUDA no topo enquanto o grupo rola (pedido Dona
                    29/07): a lista é longa e, sem isso, some a referência de
                    onde acabam as atrasadas e começam as de hoje. A faixa
                    ocupa a largura toda e tem fundo opaco pra os cards
                    passarem POR BAIXO, sem texto sobreposto. */}
                <div
                  style={{
                    position: "sticky", top: 0, zIndex: 5,
                    display: "flex", alignItems: "center", gap: 8,
                    padding: "10px 0 9px",
                    background: "var(--bg, #f7f6f3)",  // MESMO fundo da página: card passa por baixo sem transparecer
                    borderBottom: `2px solid ${g.cor}`,
                    marginBottom: 12,
                  }}
                >
                  <span style={{ fontSize: 13, fontWeight: 700, color: g.cor, background: g.bg, borderRadius: 7, padding: "3px 10px" }}>
                    {g.emoji} {g.titulo}
                  </span>
                  <span style={{ fontSize: 12, color: "#8a857c" }}>
                    {g.itens.length} {g.itens.length === 1 ? "tarefa" : "tarefas"}
                  </span>
                </div>
                <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                  {g.itens.map((t) => {
                    const busy = concluindo.has(t.id);
                    const isLigacao = t.tipo === "ligacao";
                    return (
                      <div
                        key={t.id}
                        className="tarefaCard"
                        style={{ background: isLigacao ? "#fbf9f4" : "#fff", border: isLigacao ? "1px solid #e3ddc9" : "1px solid #e7e4dd", borderRadius: 12, padding: "12px 14px", display: "flex", gap: 12, alignItems: "flex-start", borderLeft: isLigacao ? "3px solid #c79a3a" : undefined }}
                      >
                        {/* corpo do card: clica pra abrir no Chat */}
                        <div
                          onClick={() => abrirNoChat(t)}
                          title="Clica pra abrir a conversa no Chat"
                          style={{ minWidth: 0, flex: 1, cursor: "pointer" }}
                        >
                          <div style={{ display: "flex", alignItems: "baseline", gap: 8, flexWrap: "wrap" }}>
                            <span style={{ fontSize: 14, fontWeight: 700, color: "#15110d" }}>{t.lead_nome || "Lead"}</span>
                            {isLigacao ? (
                              <span title="Tarefa de ligação gerada pela cadência (follow-up automático)" style={{ fontSize: 11, fontWeight: 700, color: "#8a6d1f", background: "#f6edd6", borderRadius: 6, padding: "2px 7px" }}>🔔 ligação automática</span>
                            ) : (
                              <span title="Tarefa criada por você" style={{ fontSize: 11, fontWeight: 600, color: "#6b6355", background: "#f2efe8", borderRadius: 6, padding: "2px 7px" }}>📋 tarefa</span>
                            )}
                            {/* a tag de data é colorida pelo grupo: vermelha em atrasada,
                                verde em "hoje" — bate o olho e já sabe o que é do dia. */}
                            <span style={{ fontSize: 11.5, color: g.chave === "atrasadas" ? "#b3261e" : g.chave === "hoje" ? "#1a6b3a" : "#8a857c", fontWeight: g.chave === "proximas" ? 500 : 700, background: g.chave === "atrasadas" ? "#fdecea" : g.chave === "hoje" ? "#e6f4ea" : "#f2efe8", borderRadius: 6, padding: "2px 7px" }}>
                              {g.chave === "hoje" ? (t.hora ? `hoje · ${t.hora}` : "hoje") : t.hora ? `${fmtData(t.data)} · ${t.hora}` : fmtData(t.data)}
                            </span>
                            {isAdmin && t.corretora && (
                              <span style={{ fontSize: 11, color: "#8a857c", background: "#f2efe8", borderRadius: 6, padding: "2px 7px" }}>{t.corretora}</span>
                            )}
                            {t.tem_msg_planejada && (
                              <span
                                title={
                                  t.msg_planejada_enviada_em
                                    ? `mensagem enviada em ${new Date(t.msg_planejada_enviada_em).toLocaleString("pt-BR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })}`
                                    : `mensagem planejada — dispara ${t.hora ? `${fmtData(t.data)} · ${t.hora}` : `${fmtData(t.data)} · 09:00`}`
                                }
                                style={{ fontSize: 11, fontWeight: 700, color: t.msg_planejada_enviada_em ? "#1a6b3a" : "#7a5cff", background: t.msg_planejada_enviada_em ? "#eef7f0" : "#f0ecff", borderRadius: 6, padding: "2px 7px" }}
                              >
                                {t.msg_planejada_enviada_em
                                  ? "📤 enviada"
                                  : `📤 ${t.hora ?? "09:00"}`}
                              </span>
                            )}
                          </div>
                          <div style={{ fontSize: 13, color: "#444", marginTop: 6, whiteSpace: "pre-wrap", wordBreak: "break-word" }}>{t.texto}</div>
                        </div>
                        {/* ações — ligação: os TRÊS resultados, um clique cada
                            (não é formulário: concluir ligação é rotina, e
                            formulário faz a corretora parar de registrar).
                            manual: editar + concluir. */}
                        <div style={{ flex: "0 0 auto", display: "flex", gap: 6, alignItems: "flex-start" }}>
                          {isLigacao ? (
                            <>
                              {BOTOES_LIGACAO.map((b) => (
                                <button
                                  key={b.estado}
                                  onClick={() => ligarFeito(t, b.estado)}
                                  disabled={busy}
                                  title={b.titulo}
                                  style={{
                                    cursor: busy ? "wait" : "pointer",
                                    border: `1.5px solid ${b.borda}`,
                                    background: b.fundo,
                                    color: b.cor,
                                    borderRadius: 8,
                                    padding: "6px 10px",
                                    fontSize: 12.5,
                                    fontWeight: 700,
                                    fontFamily: "inherit",
                                    opacity: busy ? 0.6 : 1,
                                    whiteSpace: "nowrap",
                                  }}
                                >
                                  {busy ? "…" : b.rotulo}
                                </button>
                              ))}
                            </>
                          ) : (
                            <>
                              <button
                                className="editarBtn"
                                onClick={() => abrirEditar(t)}
                                disabled={busy}
                                title="Editar tarefa"
                                style={{ cursor: busy ? "wait" : "pointer", border: "1.5px solid #e0dccf", background: "#f7f4ec", color: "#6b6355", borderRadius: 8, padding: "6px 10px", fontSize: 12.5, fontWeight: 700, fontFamily: "inherit", opacity: busy ? 0.6 : 1, whiteSpace: "nowrap" }}
                              >
                                ✏️ editar
                              </button>
                              {/* concluir — idempotente no copiloto; remoção otimista */}
                              <button
                                className="concluirBtn"
                                onClick={() => concluir(t)}
                                disabled={busy}
                                title="Marcar como concluída"
                                style={{ cursor: busy ? "wait" : "pointer", border: "1.5px solid #cfe0d4", background: "#eef7f0", color: "#1a6b3a", borderRadius: 8, padding: "6px 12px", fontSize: 12.5, fontWeight: 700, fontFamily: "inherit", opacity: busy ? 0.6 : 1, whiteSpace: "nowrap" }}
                              >
                                {busy ? "…" : "✓ concluir"}
                              </button>
                            </>
                          )}
                        </div>
                      </div>
                    );
                  })}
                </div>
              </div>
            ),
          )}
      </div>

      {/* modal editar tarefa — herda a estrutura do modal "criar tarefa" do Chat */}
      {editando && (
        <div
          onClick={() => !salvandoEd && setEditando(null)}
          style={{ position: "fixed", inset: 0, zIndex: 90, background: "rgba(15,23,42,.45)", display: "flex", alignItems: "center", justifyContent: "center" }}
        >
          <div
            onClick={(e) => e.stopPropagation()}
            style={{ width: "min(440px, calc(100vw - 32px))", background: "#fff", border: "1px solid #e7e4dd", borderRadius: 14, padding: 16, display: "flex", flexDirection: "column", gap: 10, boxShadow: "0 12px 40px rgba(0,0,0,.18)" }}
          >
            <div style={{ fontSize: 14, fontWeight: 700, color: "#15110d" }}>
              ✏️ Editar tarefa · {editando.lead_nome || "Lead"}
            </div>
            <textarea
              value={edTexto}
              onChange={(e) => setEdTexto(e.target.value)}
              placeholder="O que precisa ser feito?"
              rows={3}
              autoFocus
              style={{ font: "inherit", fontSize: 13, padding: "9px 11px", borderRadius: 8, border: "1px solid #e7e4dd", background: "#f6f4ef", color: "#15110d", resize: "vertical" }}
            />
            <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
              <label style={{ fontSize: 11, color: "#8a857c" }}>quando</label>
              <input
                type="date"
                value={edData}
                onChange={(e) => setEdData(e.target.value)}
                style={{ font: "inherit", fontSize: 13, padding: "6px 10px", borderRadius: 8, border: "1px solid #e7e4dd", background: "#f6f4ef", color: "#15110d" }}
              />
              <input
                type="time"
                value={edHora}
                onChange={(e) => setEdHora(e.target.value)}
                title="horário (opcional)"
                style={{ font: "inherit", fontSize: 13, padding: "6px 10px", borderRadius: 8, border: "1px solid #e7e4dd", background: "#f6f4ef", color: "#15110d" }}
              />
              <span style={{ fontSize: 11, color: "#a9a396" }}>hora opcional</span>
            </div>

            {/* Anotações — campo próprio da tarefa (não é nota do lead) */}
            <textarea
              value={edAnot}
              onChange={(e) => setEdAnot(e.target.value)}
              placeholder="Anotações (opcional) — contexto pra você, não vai pro lead"
              rows={2}
              style={{ font: "inherit", fontSize: 13, padding: "9px 11px", borderRadius: 8, border: "1px solid #e7e4dd", background: "#f6f4ef", color: "#15110d", resize: "vertical" }}
            />

            {/* Mensagem planejada — checkbox; marcado abre a caixa com saudação travada */}
            <label style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 12.5, color: "#444", cursor: "pointer" }}>
              <input type="checkbox" checked={edMsgOn} onChange={(e) => setEdMsgOn(e.target.checked)} />
              📤 Mensagem planejada — dispara sozinha na data/hora
            </label>
            {edMsgOn && (
              <div>
                <div style={{ display: "flex", alignItems: "stretch", border: "1px solid #e7e4dd", borderRadius: 8, overflow: "hidden", background: "#f6f4ef" }}>
                  <span style={{ padding: "9px 10px", background: "#efe9dc", color: "#8a857c", fontSize: 12.5, whiteSpace: "nowrap", borderRight: "1px solid #e7e4dd", alignSelf: "center" }}>
                    {saudacaoTravada(editando.lead_nome)}
                  </span>
                  <textarea
                    value={edMsg}
                    onChange={(e) => setEdMsg(e.target.value)}
                    placeholder="a saudação já vai automática — escreve daqui pra frente"
                    rows={3}
                    style={{ flex: 1, minWidth: 0, border: "none", outline: "none", background: "transparent", padding: "9px 11px", fontSize: 13, color: "#15110d", resize: "vertical", font: "inherit" }}
                  />
                </div>
                <div style={{ fontSize: 11, color: "#a9a396", marginTop: 4 }}>
                  enviada em UTILITY pela SUA linha — a saudação vai automática.
                  {editando.msg_planejada_enviada_em ? " (já disparada; reescrever re-agenda)" : ""}
                </div>
              </div>
            )}
            {edErro && <div style={{ fontSize: 11, color: "#b3261e" }}>não salvou ({edErro})</div>}
            <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
              <button
                onClick={() => setEditando(null)}
                disabled={salvandoEd}
                style={{ font: "inherit", fontSize: 12.5, fontWeight: 600, padding: "6px 14px", borderRadius: 8, border: "1px solid #e7e4dd", background: "#fff", color: "#6b6355", cursor: salvandoEd ? "default" : "pointer" }}
              >
                cancelar
              </button>
              <button
                className="edSalvarBtn"
                onClick={salvarEdicao}
                disabled={salvandoEd || edTexto.trim().length < 2 || !edData}
                style={{ font: "inherit", fontSize: 12.5, fontWeight: 700, padding: "6px 14px", borderRadius: 8, border: "1px solid #1a6b3a", background: "#1a6b3a", color: "#fff", cursor: "pointer" }}
              >
                {salvandoEd ? "salvando…" : "Salvar"}
              </button>
            </div>
          </div>
        </div>
      )}

      {toast && (
        <div style={{ position: "fixed", bottom: 24, left: "50%", transform: "translateX(-50%)", background: toast.erro ? "#b3261e" : "#15110d", color: "#fff", padding: "10px 18px", borderRadius: 10, fontSize: 13, fontWeight: 600, boxShadow: "0 6px 20px rgba(0,0,0,.2)", zIndex: 80 }}>
          {toast.msg}
        </div>
      )}
    </div>
  );
}
