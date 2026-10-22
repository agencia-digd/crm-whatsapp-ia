"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useEstadoPersistido } from "@/app/lib/estado-persistido";
import { useTravarRolagemDaPagina } from "@/app/lib/tela-cheia";
import { useRouter } from "next/navigation";

// CRM v2 — visual replicado 1:1 do CRM do painel antigo (dash-imobiliaria/app/crm),
// a pedido da Dona: mesmo bg #f6f4ef, topbar branca, chips, cores e disposição
// das datas no card. Mantém os 5 campos definidos por ela: nome, telefone, tag,
// dias parado (>7d laranja / >30d vermelho) e data de entrada.
//
// 11/07 (pedido Dona, ditado): o Remarketing deixou de ser módulo separado e
// virou SELETOR DE FUNIL aqui no cabeçalho — como o painel antigo fazia com o
// "Funil de atendimento". "Funil de vendas" = kanban canônico (drag&drop,
// clique→Chat); "Remarketing" = kanban de nutrição Base→30→…→150 dias
// (somente leitura, tags de nutrição, clique→Chat). E a BUSCA do cabeçalho
// antigo voltou: nome OU telefone em TODOS os leads (proxy /api/buscar →
// copiloto /painel/atendimento/busca), resultado abre a conversa no Chat.
// Lógica/fetch por funil inalterados (proxies /api/crm e /api/remarketing).

type Funil = "vendas" | "remarketing";

type Card = {
  lead_id: number;
  external_ref: string | null; // id do Kommo — chave da conversa no Chat (/chat?lead=)
  nome: string;
  telefone: string | null;
  valor: number | null;
  origem: string | null;
  tag: string | null;
  campanha?: string | null; // campanha publicitária (Meta lead ads) — chip 📣
  tem_tarefa?: boolean; // lead tem tarefa aberta — badge 📋
  proxima_tarefa?: string | null; // ISO da próxima tarefa (title do badge)
  corretora_id: number | null;
  corretora_label: string | null;
  created_at: string | null;
  updated_at: string | null;
  dias_parado: number | null;
  cadencia_toque?: string | null; // funil de vendas: "CD1".."CD8" quando a cadência está ativa
  respondeu_em?: string | null; // funil de vendas: lead respondeu na cadência — badge verde
  nutricao_tag?: string | null; // remarketing: "30D".."150D" — marco de nutrição (Base = sem tag)
  veio_remarketing?: boolean; // card chegou pela linha da Corretor (handoff/promoção) — chip RMKT
  veio_remarketing_em?: string | null; // ISO de quando chegou (title do chip)
  tag_remarketing?: string | null; // nome da tag no card ("RMKT"); backend antigo não manda
  handoff_segurado?: { motivo: string | null; em: string | null } | null; // ficou em Respondeu: alguém já conversa pelo celular da linha da Corretor
  resumo_ia?: ResumoIa | null; // resumo por IA do que a pessoa escreveu na linha da Corretor (05/10)
};

// Resumo por IA no card (05/10/2026). Só cards que passaram pelo handoff têm.
// "pendente" = está saindo; "sem_resumo" = a IA falhou ou demorou, e o card
// chegou do mesmo jeito.
type ResumoIa = {
  status: "ok" | "pendente" | "sem_resumo";
  resumo?: string | null;
  fatos?: { fato: string | null; trecho: string | null }[];
  em?: string | null;
};

function ResumoIaNoCard({ r }: { r: ResumoIa }) {
  const base = { fontSize: 11.5, lineHeight: 1.35, marginTop: 7, borderRadius: 6, padding: "5px 7px" } as const;
  if (r.status !== "ok") {
    return (
      <div
        data-testid="resumo-ia-card"
        style={{ ...base, color: "#8a857c", background: "#f4f2ed" }}
        title={r.status === "pendente"
          ? "A IA está lendo a conversa. O card já é seu; o resumo aparece quando ficar pronto."
          : "A IA não conseguiu resumir a tempo. Abra a conversa: está tudo lá."}
      >
        {r.status === "pendente" ? "resumo saindo…" : "sem resumo"}
      </div>
    );
  }
  const fatos = (r.fatos ?? []).filter((f) => f.fato);
  const texto = r.resumo || fatos.map((f) => f.fato).join(" · ");
  return (
    <div
      data-testid="resumo-ia-card"
      style={{ ...base, color: "#3b372f", background: "#f1efe8" }}
      title={`Resumo por IA, só do que a pessoa escreveu:\n${fatos.map((f) => `• ${f.fato} — “${f.trecho}”`).join("\n")}`}
    >
      {/* o corte em 3 linhas mora aqui dentro: no de fora, o padding deixava a 4ª aparecer pela metade */}
      <div style={{ display: "-webkit-box", WebkitLineClamp: 3, WebkitBoxOrient: "vertical", overflow: "hidden" }}>
        <span style={{ fontWeight: 700, color: "#6b665c" }}>Resumo IA · </span>
        {texto}
      </div>
    </div>
  );
}

type Coluna = {
  stage_id: number | null;
  nome: string;
  total: number;
  cards: Card[];
};

type Board = {
  viewer?: { user: string; role: string };
  total: number;
  vazio: boolean;
  colunas: Coluna[];
  corretoras?: { id: number; label: string }[];
  error?: string;
};

// resultado da busca do cabeçalho (shape do copiloto /painel/atendimento/busca)
type BuscaHit = {
  lead_id: string; // id do Kommo (leads_snapshot) — mesmo que o /chat?lead= espera
  nome: string;
  telefone: string | null;
  etapa: string | null;
  corretora_label: string | null;
};

const REFRESH_MS = 60_000;
const BUSCA_DEBOUNCE_MS = 400;

// Funil canônico de 8 (mesmo STAGES_CANONICOS do backend) — só essas colunas
// aceitam drop; "Sem etapa"/derivadas são somente leitura.
const STAGES_CANONICOS = new Set([
  "Novo",
  "Em atendimento",
  "Follow-up",
  "Visita marcada",
  "Compareceu",
  "Proposta",
  "Venda ganha",
  "Venda perdida",
]);

function fmtValor(v: number | null): string | null {
  if (v == null || v === 0) return null;
  return v.toLocaleString("pt-BR", {
    style: "currency",
    currency: "BRL",
    maximumFractionDigits: 0,
  });
}

// formato do antigo: +5500000000000
const CHAVE_VISTO = "crm_chat_visto_v1";

// O "!" aparece quando o lead falou por último E a corretora ainda não abriu o
// chat DEPOIS dessa mensagem. Abrir o chat basta — não precisa responder.
function temMsgNova(
  aguardando: Record<string, string>,
  vistoEm: Record<string, string>,
  c: { lead_id: number; telefone: string | null },
): string | null {
  const msgEm = aguardando[p8(c.telefone)];
  if (!msgEm) return null;
  const visto = vistoEm[String(c.lead_id)];
  // visto >= msg → já olhou essa mensagem. Msg mais nova que a visita → volta.
  if (visto && visto >= msgEm) return null;
  return msgEm;
}

// 8 últimos dígitos — é como o copiloto devolve a fila de "aguardando". Casar por
// 8 evita divergência de DDI/DDD e do nono dígito entre Kommo e WhatsApp.
function p8(t: string | null): string {
  return (t || "").replace(/\D/g, "").slice(-8);
}

function fmtTel(t: string | null): string {
  const d = (t || "").replace(/\D/g, "");
  if (d.length === 13) return `+${d.slice(0, 2)} (${d.slice(2, 4)}) ${d.slice(4, 9)}-${d.slice(9)}`;
  if (d.length === 12) return `+${d.slice(0, 2)} (${d.slice(2, 4)}) ${d.slice(4, 8)}-${d.slice(8)}`;
  if (d.length === 11) return `(${d.slice(0, 2)}) ${d.slice(2, 7)}-${d.slice(7)}`;
  return t || "—";
}

function fmtEntrada(iso: string | null): string | null {
  if (!iso) return null;
  return new Date(iso).toLocaleDateString("pt-BR", {
    day: "2-digit",
    month: "2-digit",
    year: "2-digit",
  });
}

// só dígitos — usado no filtro do board pra casar telefone ignorando formatação
function soDigitos(s: string): string {
  return s.replace(/\D/g, "");
}

// card casa o filtro do board? nome (case-insensitive, substring) OU telefone
// (só dígitos, substring). Filtro vazio = casa tudo.
function cardCasaFiltro(c: Card, termo: string): boolean {
  const t = termo.trim().toLowerCase();
  if (!t) return true;
  if ((c.nome || "").toLowerCase().includes(t)) return true;
  const td = soDigitos(termo);
  if (td && soDigitos(c.telefone || "").includes(td)) return true;
  return false;
}

function diasLabel(d: number | null): string | null {
  if (d == null) return null;
  if (d === 0) return "hoje";
  return `${d}d`;
}

// dias parado: >7d laranja, >30d vermelho (cores mantidas do v2, chip no formato do antigo)
function diasChipStyle(d: number | null): React.CSSProperties {
  const base: React.CSSProperties = { fontSize: 11, fontWeight: 600, borderRadius: 6, padding: "2px 7px" };
  if (d != null && d > 30) return { ...base, color: "#b3261e", background: "#fdecea" };
  if (d != null && d > 7) return { ...base, color: "#9a5b00", background: "#fbf1e0" };
  return { ...base, fontWeight: 500, color: "#8a857c", background: "#f2efe8" };
}

export default function CrmPage() {
  // tela cheia: quem rola é o kanban, não a página (some a faixa branca)
  useTravarRolagemDaPagina();
  const [funil, setFunil] = useState<Funil>("vendas");
  // filtro do BOARD (client-side): esconde os cards que não casam com nome ou
  // telefone; NÃO abre chat, NÃO faz fetch. Cards que casam continuam na coluna
  // deles, arrastáveis. É diferente da BUSCA do cabeçalho (que abre o Chat).
  const [filtroBoard, setFiltroBoard] = useState("");
  // toggle "🟢 Só esperando resposta" (B4): esconde os cards SEM o badge verde
  // de resposta (respondeu_em — lead respondeu e ainda não foi retornado).
  // Client-side, combina com o filtro do board. Só faz sentido no funil de
  // vendas (remarketing não tem o dado).
  const [soEsperando, setSoEsperando] = useState(false);
  // toggle "📋 Sem tarefa": o espelho do badge 📋 — mostra só quem NÃO tem
  // tarefa aberta, ou seja, o lead que ninguém agendou o próximo passo.
  // Client-side, combina com os outros dois filtros. Vendas apenas: o board de
  // remarketing (/crm/remarketing/board) não devolve `tem_tarefa`, e um toggle
  // que ali escondesse tudo seria pior que não existir.
  const [semTarefa, setSemTarefa] = useState(false);
  const [board, setBoard] = useState<Board | null>(null);
  // leads cuja ÚLTIMA mensagem foi DELES (falaram e ainda não foram respondidos).
  // Chave = 8 últimos dígitos do telefone; valor = ISO da mensagem.
  // Some sozinho quando a corretora responde — o copiloto é stateless nisso.
  const [aguardando, setAguardando] = useState<Record<string, string>>({});
  // quando a corretora ABRIU o chat de cada lead (ISO). Guardado no navegador
  // dela — some o "!" sem exigir resposta, porque às vezes a mensagem do cliente
  // encerra a conversa ("obrigada!") e não há o que responder (pedido Dona 29/07).
  // Comparado com o timestamp da última msg: mensagem NOVA depois da visita faz
  // o "!" voltar sozinho.
  const [vistoEm, setVistoEm] = useState<Record<string, string>>({});
  const [erro, setErro] = useState<string | null>(null);
  const [filtro, setFiltro] = useEstadoPersistido<number | null>("crm_corretora_v1", null);
  const [atualizadoEm, setAtualizadoEm] = useState<Date | null>(null);
  // drag & drop (B7, só no funil de vendas): coluna sob o card + toast
  const [overCol, setOverCol] = useState<string | null>(null);
  const [toast, setToast] = useState<{ msg: string; erro?: boolean } | null>(null);
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  // busca do cabeçalho — debounce 400ms, min 2 chars, dropdown sob o campo
  // Altura do kanban medida em tempo real (pedido Dona 30/07): antes as colunas
  // tinham maxHeight fixo `calc(100vh - 250px)`, um chute do quanto havia acima.
  // Quando as pills de corretora quebravam linha, ou com zoom diferente, o board
  // terminava antes do fim da tela — a barra de rolagem horizontal ficava no meio
  // da página com uma faixa branca embaixo. Agora ele mede o próprio topo e ocupa
  // o resto da janela, então a barra encosta no rodapé em qualquer situação.
  const kanbanRef = useRef<HTMLDivElement>(null);
  const [alturaKanban, setAlturaKanban] = useState<number | null>(null);

  const [busca, setBusca] = useState("");
  const [hits, setHits] = useState<BuscaHit[] | null>(null);
  const [buscando, setBuscando] = useState(false);
  const buscaTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const buscaBox = useRef<HTMLDivElement>(null);
  // clique no card abre a conversa no Chat (pedido Dona 11/07) — mas o card
  // também é draggable (vendas): a flag marca o arrasto e o onClick ignora o
  // clique fantasma que alguns browsers disparam depois do drop.
  const router = useRouter();
  const arrastou = useRef(false);

  const mostrarToast = useCallback((msg: string, erroT = false) => {
    if (toastTimer.current) clearTimeout(toastTimer.current);
    setToast({ msg, erro: erroT });
    toastTimer.current = setTimeout(() => setToast(null), 4000);
  }, []);

  useEffect(() => {
    try {
      const cru = localStorage.getItem(CHAVE_VISTO);
      if (cru) setVistoEm(JSON.parse(cru));
    } catch { /* storage bloqueado ou json podre: segue sem histórico de visto */ }
  }, []);

  const carregar = useCallback(async (f: number | null, fn: Funil) => {
    try {
      const qs = f != null ? `?corretora=${f}` : "";
      const endpoint = fn === "vendas" ? "/api/crm" : "/api/remarketing";
      const r = await fetch(`${endpoint}${qs}`, { cache: "no-store" });
      const data: Board = await r.json();
      if (!r.ok) throw new Error(data.error ?? `http_${r.status}`);
      setBoard(data);
      setErro(null);
      setAtualizadoEm(new Date());
      // fila de quem falou por último — best-effort: se falhar, o board segue
      // normal, só sem o destaque (não é motivo pra derrubar a tela).
      try {
        const ra = await fetch("/api/conversa/aguardando?dias=14", { cache: "no-store" });
        if (ra.ok) {
          const da = await ra.json();
          setAguardando(da?.telefones ?? {});
        }
      } catch { /* segue sem o destaque */ }
    } catch (e) {
      setErro(e instanceof Error ? e.message : "erro_desconhecido");
    }
  }, []);

  useEffect(() => {
    carregar(filtro, funil);
    const t = setInterval(() => {
      if (document.visibilityState === "visible") carregar(filtro, funil);
    }, REFRESH_MS);
    return () => clearInterval(t);
  }, [carregar, filtro, funil]);

  // busca: debounce + fetch /api/buscar (nome OU telefone, todos os leads)
  useEffect(() => {
    if (buscaTimer.current) clearTimeout(buscaTimer.current);
    const q = busca.trim();
    if (q.length < 2) {
      setHits(null);
      setBuscando(false);
      return;
    }
    setBuscando(true);
    buscaTimer.current = setTimeout(async () => {
      try {
        const r = await fetch(`/api/buscar?q=${encodeURIComponent(q)}`, { cache: "no-store" });
        const d = await r.json();
        setHits(Array.isArray(d.leads) ? d.leads : []);
      } catch {
        setHits([]);
      } finally {
        setBuscando(false);
      }
    }, BUSCA_DEBOUNCE_MS);
    return () => {
      if (buscaTimer.current) clearTimeout(buscaTimer.current);
    };
  }, [busca]);

  // fecha o dropdown da busca em clique fora
  useEffect(() => {
    const fechar = (e: MouseEvent) => {
      if (buscaBox.current && !buscaBox.current.contains(e.target as Node)) {
        setHits(null);
        setBusca("");
      }
    };
    document.addEventListener("mousedown", fechar);
    return () => document.removeEventListener("mousedown", fechar);
  }, []);

  const trocarFunil = useCallback((fn: Funil) => {
    setFunil(fn);
    setBoard(null); // evita flash do board antigo enquanto o outro carrega
    setErro(null);
    setOverCol(null);
  }, []);

  // move otimista com rollback (SÓ no funil de vendas): atualiza o board na
  // hora, POST /api/crm/mover; falhou → restaura o snapshot e avisa. Backend
  // valida escopo e liga a cadência quando destino=Follow-up.
  const moverCard = useCallback(
    async (leadId: number, destino: Coluna) => {
      setOverCol(null);
      if (!board || funil !== "vendas") return;
      if (!STAGES_CANONICOS.has(destino.nome)) {
        mostrarToast("essa coluna não aceita cards", true);
        return;
      }
      let card: Card | undefined;
      let origemNome: string | null = null;
      for (const col of board.colunas) {
        const c = col.cards.find((x) => x.lead_id === leadId);
        if (c) {
          card = c;
          origemNome = col.nome;
          break;
        }
      }
      if (!card || origemNome === destino.nome) return;
      const snapshot = board;
      setBoard({
        ...board,
        colunas: board.colunas.map((col) => {
          if (col.nome === origemNome)
            return { ...col, total: col.total - 1, cards: col.cards.filter((c) => c.lead_id !== leadId) };
          if (col.nome === destino.nome)
            return { ...col, total: col.total + 1, cards: [card!, ...col.cards] };
          return col;
        }),
      });
      try {
        const r = await fetch("/api/crm/mover", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ lead_id: leadId, stage_destino: destino.nome }),
        });
        const data = await r.json().catch(() => ({}));
        if (!r.ok || data.ok !== true) {
          throw new Error(
            typeof data.detail === "string" ? data.detail : data.error ?? `http_${r.status}`,
          );
        }
        mostrarToast(
          data.cadencia_ligada
            ? `movido pra ${destino.nome} — cadência ligada ✓`
            : `movido pra ${destino.nome} ✓`,
        );
      } catch (e) {
        setBoard(snapshot); // rollback
        mostrarToast(
          `não consegui mover (${e instanceof Error ? e.message : "erro"})`,
          true,
        );
      }
    },
    [board, funil, mostrarToast],
  );

  // navega pro Chat com a conversa do lead aberta (deep-link /chat?lead=).
  // O Chat identifica a conversa pelo id do KOMMO (external_ref) — mas lead
  // criado à mão no painel / nativo do intake NÃO tem vínculo Kommo, e o card
  // ficava intocável: o clique só dizia "esse lead ainda não tem conversa no
  // Chat" e não abria nada (caso Edison, 21/09). O backend já resolve os dois
  // ids (`_lead_conversa_contexto` tenta o snapshot e cai no lead nativo),
  // então cai no id da espinha quando não há Kommo.
  const abrirNoChat = useCallback(
    (c: Card) => {
      if (arrastou.current) return; // foi drag, não clique
      const idChat = c.external_ref || (c.lead_id != null ? String(c.lead_id) : "");
      if (!idChat) {
        mostrarToast("esse lead ainda não tem conversa no Chat");
        return;
      }
      // abrir o chat JÁ conta como visto — apaga o "!" e a borda sem exigir
      // resposta. Grava o AGORA: se o lead mandar outra msg depois, o timestamp
      // dela fica maior que este e o destaque volta sozinho.
      setVistoEm((atual) => {
        const novo = { ...atual, [String(c.lead_id)]: new Date().toISOString() };
        try { localStorage.setItem(CHAVE_VISTO, JSON.stringify(novo)); } catch { /* storage cheio/bloqueado */ }
        return novo;
      });
      router.push(`/chat?lead=${encodeURIComponent(idChat)}&nome=${encodeURIComponent(c.nome)}`);
    },
    [router, mostrarToast],
  );

  const abrirHitNoChat = useCallback(
    (h: BuscaHit) => {
      setHits(null);
      setBusca("");
      router.push(`/chat?lead=${encodeURIComponent(h.lead_id)}&nome=${encodeURIComponent(h.nome)}`);
    },
    [router],
  );

  const ehVendas = funil === "vendas";
  useEffect(() => {
    const medir = () => {
      const el = kanbanRef.current;
      if (!el) return;
      // 8px de respiro pra sombra do card não ficar colada no rodapé
      setAlturaKanban(Math.max(240, window.innerHeight - el.getBoundingClientRect().top - 8));
    };
    medir();
    window.addEventListener("resize", medir);
    // o que está ACIMA do board muda de altura (pills quebram linha, aviso
    // aparece) — sem observar isso o board voltaria a desencontrar do rodapé
    const obs = new ResizeObserver(medir);
    if (kanbanRef.current?.parentElement) obs.observe(kanbanRef.current.parentElement);
    return () => { window.removeEventListener("resize", medir); obs.disconnect(); };
  }, [board, funil, filtro]);   // remede quando o board troca de forma

  const isAdmin = board?.viewer?.role === "admin";
  const vazio =
    board != null && (board.total === 0 || board.colunas.length === 0) && filtro == null;

  return (
    // full-bleed: escapa o padding do .main do shell (36px 40px 48px) pra
    // reproduzir a página inteira do antigo, com o próprio fundo e topbar.
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
      {/* hover do card (estilos da página são inline; só o :hover precisa de CSS) */}
      <style>{`.crmCard:hover { border-color: #cfc9bc !important; box-shadow: 0 2px 8px rgba(0,0,0,.07); }
.buscaHit:hover { background: #f6f4ef !important; }`}</style>

      {/* topbar — igual ao antigo, agora com seletor de funil + busca */}
      <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "12px 18px", background: "#fff", borderBottom: "1px solid #e7e4dd" }}>
        <a href="/" style={{ textDecoration: "none", color: "#888", fontSize: 13 }}>← Painel</a>
        <span style={{ color: "#ccc" }}>·</span>
        <strong style={{ color: "#15110d", fontSize: 16 }}>🗂️ CRM</strong>
        {/* seletor de funil (pedido Dona 11/07 — como o "Funil de atendimento" do antigo) */}
        <select
          value={funil}
          onChange={(e) => trocarFunil(e.target.value as Funil)}
          title="Escolhe o funil exibido no board"
          style={{ border: "1.5px solid #ddd", background: "#fff", color: "#15110d", borderRadius: 8, padding: "5px 8px", fontSize: 13, fontWeight: 600, fontFamily: "inherit", cursor: "pointer", outline: "none" }}
        >
          <option value="vendas">Funil de vendas</option>
          <option value="remarketing">Remarketing</option>
        </select>
        <span style={{ fontSize: 12, color: "#8a857c", background: "#f2efe8", borderRadius: 6, padding: "2px 8px" }}>
          {ehVendas
            ? "espinha canônica · arrasta pra mudar de etapa · clica pra abrir no Chat"
            : "nutrição pós-cadência · 1 msg a cada 30 dias · somente leitura · clica pra abrir no Chat"}
        </span>
        {/* busca do cabeçalho (padrão do antigo): nome OU telefone, todos os leads */}
        <div ref={buscaBox} style={{ position: "relative", marginLeft: 6 }}>
          <input
            type="search"
            value={busca}
            onChange={(e) => setBusca(e.target.value)}
            placeholder="🔍 Buscar por nome ou telefone…"
            style={{ padding: "6px 12px", borderRadius: 8, border: "1px solid #ddd", fontSize: 13, width: 260, outline: "none", fontFamily: "inherit", background: "#fff", color: "#15110d" }}
          />
          {(hits !== null || buscando) && busca.trim().length >= 2 && (
            <div style={{ position: "absolute", top: "calc(100% + 6px)", left: 0, width: 380, maxHeight: 340, overflowY: "auto", background: "#fff", border: "1px solid #e7e4dd", borderRadius: 10, boxShadow: "0 8px 28px rgba(0,0,0,.12)", zIndex: 60 }}>
              {buscando && (
                <div style={{ padding: "12px 14px", fontSize: 12.5, color: "#8a857c" }}>Buscando…</div>
              )}
              {!buscando && hits !== null && hits.length === 0 && (
                <div style={{ padding: "12px 14px", fontSize: 12.5, color: "#8a857c" }}>
                  Nenhum lead encontrado por nome ou telefone.
                </div>
              )}
              {!buscando && (hits ?? []).map((h) => (
                <div
                  key={h.lead_id}
                  className="buscaHit"
                  onClick={() => abrirHitNoChat(h)}
                  title="Clica pra abrir a conversa no Chat"
                  style={{ display: "flex", alignItems: "center", gap: 10, padding: "9px 12px", borderBottom: "1px solid #f2efe8", cursor: "pointer" }}
                >
                  <div style={{ minWidth: 0, flex: 1 }}>
                    <div style={{ fontSize: 13, fontWeight: 700, color: "#15110d", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{h.nome}</div>
                    <div style={{ fontSize: 12, color: "#666", fontVariantNumeric: "tabular-nums" }}>{fmtTel(h.telefone)}</div>
                  </div>
                  {h.etapa && (
                    <span style={{ flex: "0 0 auto", fontSize: 10.5, color: "#8a857c", background: "#f2efe8", borderRadius: 6, padding: "2px 7px", textTransform: "uppercase", letterSpacing: 0.4, whiteSpace: "nowrap" }}>
                      {h.etapa}
                    </span>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
        {/* FILTRO DO BOARD (client-side) — esconde os cards que não casam por
            nome/telefone; os que casam ficam na coluna deles, arrastáveis.
            Não abre chat, não faz fetch. "x" limpa. */}
        <div style={{ position: "relative", marginLeft: 6 }}>
          <input
            type="search"
            value={filtroBoard}
            onChange={(e) => setFiltroBoard(e.target.value)}
            placeholder="⧩ Filtrar cards do board…"
            title="Filtra os cards já carregados por nome ou telefone — sem abrir o Chat"
            style={{ padding: "6px 28px 6px 12px", borderRadius: 8, border: `1px solid ${filtroBoard.trim() ? "#15110d" : "#ddd"}`, fontSize: 13, width: 230, outline: "none", fontFamily: "inherit", background: "#fff", color: "#15110d" }}
          />
          {filtroBoard.trim() && (
            <button
              onClick={() => setFiltroBoard("")}
              title="Limpar filtro"
              style={{ position: "absolute", right: 6, top: "50%", transform: "translateY(-50%)", border: "none", background: "transparent", cursor: "pointer", color: "#8a857c", fontSize: 15, lineHeight: 1, padding: "2px 4px" }}
            >
              ×
            </button>
          )}
        </div>
        {/* toggle da fila (B4): só cards com o badge 🟢 respondeu — vendas apenas */}
        {ehVendas && (
          <button
            onClick={() => setSoEsperando((v) => !v)}
            title="Mostra só os cards com o badge 🟢 respondeu (lead respondeu e ainda não foi retornado)"
            style={{ cursor: "pointer", border: `1.5px solid ${soEsperando ? "#15110d" : "#ddd"}`, background: soEsperando ? "#15110d" : "#fff", color: soEsperando ? "#fff" : "#555", borderRadius: 8, padding: "6px 12px", fontSize: 13, fontWeight: 600, fontFamily: "inherit", marginLeft: 6, whiteSpace: "nowrap" }}
          >
            🟢 Só esperando resposta
          </button>
        )}
        {/* toggle "sem tarefa": cards SEM o badge 📋 — lead sem próximo passo agendado */}
        {ehVendas && (
          <button
            onClick={() => setSemTarefa((v) => !v)}
            title="Mostra só os cards SEM o badge 📋 — leads que não têm nenhuma tarefa aberta cadastrada"
            style={{ cursor: "pointer", border: `1.5px solid ${semTarefa ? "#15110d" : "#ddd"}`, background: semTarefa ? "#15110d" : "#fff", color: semTarefa ? "#fff" : "#555", borderRadius: 8, padding: "6px 12px", fontSize: 13, fontWeight: 600, fontFamily: "inherit", marginLeft: 6, whiteSpace: "nowrap" }}
          >
            📋 Sem tarefa
          </button>
        )}
        <div style={{ flex: 1 }} />
        <span style={{ fontSize: 11.5, color: "#a9a396" }}>
          {atualizadoEm
            ? `atualizado ${atualizadoEm.toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" })} · refresh 60s`
            : "carregando…"}
        </span>
      </div>

      {/* rodapé era 60px: sobrava uma faixa branca enorme embaixo da barra de
          rolagem horizontal do kanban (Dona, 30/07). O board agora mede a
          própria altura, então esse respiro não precisa mais existir. */}
      <div style={{ padding: "18px 18px 8px" }}>
        {erro && (
          <div style={{ color: "#a0316a", background: "#fdeef4", border: "1px solid #f3c9dd", borderRadius: 12, padding: "14px 18px", marginBottom: 16 }}>
            Não consegui carregar o {ehVendas ? "CRM" : "Remarketing"} ({erro}).
          </div>
        )}

        {!board && !erro && <div style={{ color: "#999", padding: 40, textAlign: "center" }}>Carregando…</div>}

        {board && (
          <>
            {/* filtro por corretora (admin) — botões do antigo, vale nos 2 funis */}
            {isAdmin && (board.corretoras?.length ?? 0) > 0 && (
              <div style={{ display: "flex", gap: 8, marginBottom: 14, flexWrap: "wrap", alignItems: "center" }}>
                <button onClick={() => setFiltro(null)}
                  style={{ cursor: "pointer", border: `1.5px solid ${filtro === null ? "#15110d" : "#ddd"}`, background: filtro === null ? "#15110d" : "#fff", color: filtro === null ? "#fff" : "#555", borderRadius: 8, padding: "6px 14px", fontSize: 13, fontWeight: 600, fontFamily: "inherit" }}>
                  Todas
                </button>
                {board.corretoras!.map((c) => {
                  const on = filtro === c.id;
                  return (
                    <button key={c.id} onClick={() => setFiltro(on ? null : c.id)}
                      style={{ cursor: "pointer", border: `1.5px solid ${on ? "#15110d" : "#ddd"}`, background: on ? "#15110d" : "#fff", color: on ? "#fff" : "#555", borderRadius: 8, padding: "6px 14px", fontSize: 13, fontWeight: 600, fontFamily: "inherit" }}>
                      {c.label}
                    </button>
                  );
                })}
                <span style={{ fontSize: 12.5, color: "#8a857c", marginLeft: 6 }}>{board.total} lead(s)</span>
              </div>
            )}

            {/* estado vazio — cartão central do antigo, texto por funil */}
            {vazio && (
              <div style={{ maxWidth: 560, margin: "60px auto", textAlign: "center", background: "#fff", border: "1px solid #e7e4dd", borderRadius: 16, padding: "44px 32px" }}>
                <div style={{ fontSize: 44, marginBottom: 12 }}>{ehVendas ? "🗂️" : "🔁"}</div>
                <div style={{ fontSize: 18, fontWeight: 700, color: "#15110d", marginBottom: 8 }}>
                  {ehVendas ? "CRM aguardando os primeiros leads" : "Nenhum lead em nutrição ainda"}
                </div>
                <div style={{ fontSize: 13.5, color: "#8a857c", lineHeight: 1.6 }}>
                  {ehVendas
                    ? "O kanban lê a base canônica de leads do copiloto e se monta sozinho assim que as etapas e os primeiros leads chegarem — sem precisar fazer nada."
                    : "Quem esgota a cadência de 8 toques sem responder entra aqui na etapa Base e recebe 1 mensagem a cada 30 dias — até o marco de 150 dias, onde o funil para."}
                </div>
              </div>
            )}

            {/* kanban — colunas e cards do antigo; drag&drop só no funil de vendas */}
            {!vazio && (
              <div
                ref={kanbanRef}
                style={{
                  display: "flex", gap: 12, overflowX: "auto",
                  alignItems: "flex-start", paddingBottom: 4,
                  // medido: a barra horizontal encosta no rodapé da janela
                  height: alturaKanban ? `${alturaKanban}px` : undefined,
                }}
              >
                {board.colunas.map((col) => {
                  // filtrando = busca do board E/OU toggle "só esperando resposta"
                  const filtrando =
                    filtroBoard.trim() !== "" ||
                    (ehVendas && (soEsperando || semTarefa));
                  const base = filtrando
                    ? col.cards.filter(
                        (c) =>
                          cardCasaFiltro(c, filtroBoard) &&
                          (!(ehVendas && soEsperando) || !!c.respondeu_em) &&
                          (!(ehVendas && semTarefa) || !c.tem_tarefa),
                      )
                    : col.cards;
                  // Ordem em 3 faixas (pedido Dona 29/07) — pra não perder de
                  // vista um lead que acabou de falar:
                  //   1. com "!" — falou e ainda não foi aberto
                  //   2. já abri, mas ele ainda é o último a falar (segue em aberto)
                  //   3. o resto, na ordem que veio do backend
                  // Dentro das faixas 1 e 2, o mais recente primeiro. A faixa 2
                  // esvazia sozinha: quando a corretora responde, o lead sai do
                  // `aguardando` e o card volta pro fluxo normal.
                  const faixa = (c: Card) => {
                    if (temMsgNova(aguardando, vistoEm, c)) return 0;
                    return aguardando[p8(c.telefone)] ? 1 : 2;
                  };
                  const cardsVisiveis = [...base].sort((a, b) => {
                    const fa = faixa(a), fb = faixa(b);
                    if (fa !== fb) return fa - fb;
                    if (fa === 2) return 0;                 // sort estável: não embaralha
                    const ta = aguardando[p8(a.telefone)] ?? "";
                    const tb = aguardando[p8(b.telefone)] ?? "";
                    return tb.localeCompare(ta);            // mais recente primeiro
                  });
                  return (
                  <div
                    key={`${col.stage_id ?? "null"}-${col.nome}`}
                    onDragOver={ehVendas ? (e) => {
                      if (!STAGES_CANONICOS.has(col.nome)) return;
                      e.preventDefault();
                      e.dataTransfer.dropEffect = "move";
                      setOverCol(col.nome);
                    } : undefined}
                    onDragLeave={ehVendas ? (e) => {
                      if (e.currentTarget.contains(e.relatedTarget as Node)) return;
                      setOverCol((atual) => (atual === col.nome ? null : atual));
                    } : undefined}
                    onDrop={ehVendas ? (e) => {
                      e.preventDefault();
                      const id = Number(e.dataTransfer.getData("text/plain"));
                      if (id) moverCard(id, col);
                    } : undefined}
                    // alignSelf stretch: a coluna ocupa a altura INTEIRA do quadro mesmo com
                    // poucos cards. Sem isso a área que aceita o drop era só a altura
                    // dos cards — Compareceu (0-1 card) aceitava ~100px de 697, e as
                    // corretoras achavam que a coluna estava bloqueada (25/09).
                    style={{ minWidth: 250, maxWidth: 250, flex: "0 0 auto", alignSelf: "stretch", display: "flex", flexDirection: "column", maxHeight: "100%", background: overCol === col.nome ? "#e4e0d4" : "#efece5", borderRadius: 14, padding: 10, outline: overCol === col.nome ? "2px dashed #b0aa9e" : "none", transition: "background 120ms" }}
                  >
                    <div style={{ display: "flex", alignItems: "baseline", gap: 8, padding: "4px 6px 10px" }}>
                      <strong style={{ fontSize: 13, color: "#15110d" }}>{col.nome}</strong>
                      <span style={{ fontSize: 11.5, color: "#8a857c" }}>
                        {filtrando ? `${cardsVisiveis.length}/${col.total}` : col.total}
                      </span>
                    </div>
                    <div style={{ display: "flex", flexDirection: "column", gap: 8, overflowY: "auto", minHeight: 0, flex: "1 1 auto" }}>
                      {cardsVisiveis.length === 0 && (
                        <div style={{ fontSize: 12, color: "#b0aa9e", textAlign: "center", padding: "14px 0" }}>
                          {filtrando ? "sem resultado" : "vazio"}
                        </div>
                      )}
                      {cardsVisiveis.map((c) => (
                        <div
                          key={c.lead_id}
                          className="crmCard"
                          draggable={ehVendas}
                          onDragStart={ehVendas ? (e) => {
                            arrastou.current = true;
                            e.dataTransfer.setData("text/plain", String(c.lead_id));
                            e.dataTransfer.effectAllowed = "move";
                          } : undefined}
                          onDragEnd={ehVendas ? () => {
                            setOverCol(null);
                            // solta a flag DEPOIS do possível click fantasma do drop
                            setTimeout(() => { arrastou.current = false; }, 0);
                          } : undefined}
                          onClick={() => abrirNoChat(c)}
                          title={ehVendas
                            ? "Clica pra abrir a conversa no Chat · arrasta pra mudar de etapa"
                            : "Clica pra abrir a conversa no Chat"}
                          style={{
                            background: "#fff",
                            // borda destacada quando o lead falou e ainda não foi
                            // respondido — o "!" sozinho some no meio dos chips
                            border: temMsgNova(aguardando, vistoEm, c) ? "1.5px solid #e0a800" : "1px solid #e7e4dd",
                            borderRadius: 10, padding: "10px 12px", display: "flex", gap: 10,
                            justifyContent: "space-between", cursor: "pointer",
                            position: "relative",
                          }}
                        >
                          {/* "!" no canto superior direito: o lead mandou mensagem e
                              ainda não foi respondido. Apaga sozinho quando a
                              corretora responde (pedido Dona 29/07). */}
                          {temMsgNova(aguardando, vistoEm, c) && (
                            <span
                              aria-label="mensagem nova do lead, sem resposta"
                              title={`Mensagem nova, ainda não aberta — ${new Date(temMsgNova(aguardando, vistoEm, c)!).toLocaleString("pt-BR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })}. Abrir o chat já apaga.`}
                              style={{
                                position: "absolute", top: -7, right: -7,
                                width: 20, height: 20, borderRadius: "50%",
                                background: "#e0a800", color: "#fff",
                                fontSize: 13, fontWeight: 800, lineHeight: "20px",
                                textAlign: "center", boxShadow: "0 1px 4px rgba(0,0,0,.2)",
                                pointerEvents: "none",
                              }}
                            >
                              !
                            </span>
                          )}
                          {/* coluna esquerda: nome · telefone · chips (valor/tag) embaixo */}
                          <div style={{ minWidth: 0, flex: 1 }}>
                            <div style={{ fontSize: 13.5, fontWeight: 700, color: "#15110d", minWidth: 0 }}>{c.nome}</div>
                            <div style={{ fontSize: 12, color: "#666", marginTop: 3, fontVariantNumeric: "tabular-nums" }}>{fmtTel(c.telefone)}</div>
                            {(fmtValor(c.valor) || c.origem || c.campanha || c.tem_tarefa || c.cadencia_toque || c.respondeu_em || c.nutricao_tag || c.veio_remarketing || c.handoff_segurado) && (
                              <div style={{ display: "flex", gap: 6, marginTop: 7, flexWrap: "wrap", alignItems: "center", minWidth: 0 }}>
                                {/* procedência: este lead veio do remarketing (linha da Corretor).
                                    Primeiro chip da fila de propósito — é o que a corretora
                                    precisa saber antes de abrir a conversa (Dona 15/09/2026). */}
                                {c.veio_remarketing && (
                                  <span
                                    title={c.veio_remarketing_em
                                      ? `RMKT: veio do remarketing — chegou pra você em ${new Date(c.veio_remarketing_em).toLocaleString("pt-BR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })}`
                                      : "RMKT: veio do remarketing — respondeu a um disparo da base, não é lead novo"}
                                    style={{ fontSize: 10.5, fontWeight: 700, letterSpacing: 0.3, color: "#8a4a6b", background: "#f7eef3", borderRadius: 6, padding: "2px 7px" }}
                                  >
                                    {c.tag_remarketing ?? "RMKT"}
                                  </span>
                                )}
                                {c.handoff_segurado && (
                                  <span
                                    data-testid="chip-segurado"
                                    title={`Não foi pra roleta: alguém da casa já está conversando com essa pessoa pelo celular da linha da Corretor${c.handoff_segurado.em ? ` (desde ${new Date(c.handoff_segurado.em).toLocaleString("pt-BR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })})` : ""}. Pra mandar pra uma corretora, transfira na mão.`}
                                    style={{ fontSize: 10.5, fontWeight: 700, color: "#6b5a00", background: "#f6f0d8", borderRadius: 6, padding: "2px 7px" }}
                                  >
                                    em conversa no celular
                                  </span>
                                )}
                                {c.tem_tarefa && (
                                  <span
                                    title={c.proxima_tarefa
                                      ? `Tem tarefa aberta · próxima ${new Date(c.proxima_tarefa).toLocaleString("pt-BR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })}`
                                      : "Tem tarefa aberta"}
                                    style={{ fontSize: 11, fontWeight: 700, color: "#8a5a00", background: "#fbf1e0", borderRadius: 6, padding: "2px 7px" }}
                                  >
                                    📋 tarefa
                                  </span>
                                )}
                                {c.origem && (
                                  <span
                                    title={`Origem: ${c.origem}`}
                                    style={{ fontSize: 10.5, color: "#5a6b8a", background: "#eef1f7", borderRadius: 6, padding: "2px 7px" }}
                                  >
                                    📍 {c.origem}
                                  </span>
                                )}
                                {c.campanha && (
                                  <span
                                    title={`Campanha: ${c.campanha}`}
                                    style={{ fontSize: 10.5, color: "#6b4a8a", background: "#f2eef7", borderRadius: 6, padding: "2px 7px", maxWidth: 150, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
                                  >
                                    📣 {c.campanha}
                                  </span>
                                )}
                                {fmtValor(c.valor) && (
                                  <span style={{ fontSize: 11.5, fontWeight: 700, color: "#1a6b3a", background: "#eef7f0", borderRadius: 6, padding: "2px 7px" }}>
                                    {fmtValor(c.valor)}
                                  </span>
                                )}
                                {c.cadencia_toque && (
                                  <span
                                    title={`Cadência automática — toque ${c.cadencia_toque.replace("CD", "")} de 8`}
                                    style={{ fontSize: 10.5, fontWeight: 700, letterSpacing: 0.3, color: "#4a5f8a", background: "#eef1f7", borderRadius: 6, padding: "2px 7px" }}
                                  >
                                    {c.cadencia_toque}
                                  </span>
                                )}
                                {c.respondeu_em && (
                                  <span
                                    title={`Respondeu e ainda não foi retornado — respondeu em ${new Date(c.respondeu_em).toLocaleString("pt-BR")}. Some quando você responde o lead.`}
                                    style={{ fontSize: 10.5, fontWeight: 700, letterSpacing: 0.3, color: "#1a6b3a", background: "#eef7f0", borderRadius: 6, padding: "2px 7px" }}
                                  >
                                    🟢 respondeu
                                  </span>
                                )}
                                {c.nutricao_tag && (
                                  <span
                                    title={`Nutrição — marco de ${c.nutricao_tag.replace("D", "")} dias disparado`}
                                    style={{ fontSize: 10.5, fontWeight: 700, letterSpacing: 0.3, color: "#6b4a8a", background: "#f2eef7", borderRadius: 6, padding: "2px 7px" }}
                                  >
                                    {c.nutricao_tag}
                                  </span>
                                )}
                              </div>
                            )}
                            {c.resumo_ia && <ResumoIaNoCard r={c.resumo_ia} />}
                          </div>
                          {/* coluna direita: data em cima · dias parado no meio · corretora (discreta) embaixo */}
                          <div style={{ flex: "0 0 auto", display: "flex", flexDirection: "column", alignItems: "flex-end", justifyContent: "space-between", gap: 5 }}>
                            {fmtEntrada(c.created_at) && (
                              <div style={{ fontSize: 10.5, color: "#a9a396", whiteSpace: "nowrap", fontVariantNumeric: "tabular-nums" }}>
                                {fmtEntrada(c.created_at)}
                              </div>
                            )}
                            {diasLabel(c.dias_parado) && (
                              <span style={diasChipStyle(c.dias_parado)}>{diasLabel(c.dias_parado)}</span>
                            )}
                            {c.corretora_label && (
                              <span style={{ fontSize: 11, color: "#8a857c", whiteSpace: "nowrap" }}>{c.corretora_label}</span>
                            )}
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>
                  );
                })}
              </div>
            )}
          </>
        )}
      </div>

      {/* toast de confirmação do move (some sozinho em 4s) */}
      {toast && (
        <div
          style={{
            position: "fixed",
            bottom: 24,
            left: "50%",
            transform: "translateX(-50%)",
            zIndex: 50,
            background: toast.erro ? "#fdecea" : "#15110d",
            color: toast.erro ? "#b3261e" : "#fff",
            border: toast.erro ? "1px solid #f3c0bb" : "1px solid #15110d",
            borderRadius: 10,
            padding: "10px 18px",
            fontSize: 13.5,
            fontWeight: 600,
            boxShadow: "0 6px 24px rgba(0,0,0,.18)",
          }}
        >
          {toast.msg}
        </div>
      )}
    </div>
  );
}
