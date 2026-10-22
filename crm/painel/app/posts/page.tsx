"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useEstadoPersistido } from "@/app/lib/estado-persistido";

// Posts v2 — fila de posts sociais da corretora (Instagram), servida pelo
// copiloto (posts_corretora.py → tarefas.social_posts no agencia_cerebro :5433).
//
// Fluxo (idêntico ao dashboard/agencia-central): a corretora manda um BRIEF
// ("Novo post": formato + link de referência + o que ela quer). O OD/Assistente gera
// copy + imagem. Corretora/admin aprova COPY e IMAGEM separados; com os dois
// ok o post vira "aprovado" e entra na fila de publicação. Quem publica no IG
// é o publisher do copiloto — esta tela NUNCA chama a Graph API.
//
// "Pedir ajuste" = recusar com motivo (o copiloto zera as aprovações e guarda
// recusa_motivo pro refazer); "reabrir" volta recusado → rascunho.
//
// Estilo herdado 1:1 do CRM/Tarefas (app/crm, app/tarefas): full-bleed,
// bg #f6f4ef, topbar branca, cards brancos com borda #e7e4dd. Sem design novo.
//
// Escopo/privacidade: o copiloto resolve por X-Painel-User (corretora vê só as
// dela; admin vê todas + filtro ?corretora=). O front só repassa via
// resolveViewer nas rotas proxy (/api/posts, /api/posts/acao).
//
// HIERARQUIA VISUAL (pedido da Dona: "o que já foi publicado pode deixar
// pequeno; só deixa grande o que está para aprovação, e aprovado para
// publicar"). A tela agrupa por PRIORIDADE DE AÇÃO, não por data:
//
//   1. "Precisa de você"    — pede decisão AGORA: tem copy e/ou imagem prontas
//                             e ainda falta aprovar. Card GRANDE (arte grande,
//                             legenda legível, botões à mão).
//   2. "Pronto pra publicar"— aprovado/publicando: já passou nas duas
//                             aprovações e está na fila. Card GRANDE (é o que
//                             vai ao ar; ela ainda pode pedir ajuste).
//   3. "Ajuste pedido"      — recusado: a bola está com a gente (refazer).
//                             Card MÉDIO — não pede ação dela, mas ela pode
//                             reabrir, então continua visível.
//   4. "Em produção"        — rascunho SEM copy e SEM imagem: a gente ainda
//                             está criando. Card MÉDIO: informa, não pede ação.
//   5. "Publicados"         — resolvido. Linha COMPACTA (miniatura 44px,
//                             hook/data + link do Instagram), COLAPSADA por
//                             padrão e paginada de 20 em 20.

type Post = {
  id: number;
  corretora: string; // slug: corretor | corretor | corretor
  data: string | null;
  hora: string | null;
  formato: string; // single | carrossel | reel
  modo: string | null;
  hook: string | null;
  caption: string;
  tem_caption: boolean;
  image_url: string | null;
  carousel_urls: string[] | null;
  status: string; // rascunho | proposta | aguardando_aprovacao | aprovado | publicando | publicado | recusado
  permalink: string | null;
  publicado_em: string | null;
  copy_aprovada: boolean;
  imagem_aprovada: boolean;
  url_referencia: string | null;
  observacao: string;
  recusa_motivo: string | null;
};

type Resposta = {
  posts: Post[];
  posts_por_semana: number;
  ig_conectado: boolean;
  is_admin: boolean;
  error?: string;
};

const REFRESH_MS = 60_000;

const CORRETORAS: { slug: string; label: string }[] = [
  { slug: "corretor", label: "Pessoa6" },
  { slug: "corretor", label: "Pessoa1" },
  { slug: "corretor", label: "Pessoa3" },
  { slug: "pessoa_d", label: "Pessoa7" },
];

const ST: Record<string, { l: string; c: string; b: string }> = {
  rascunho: { l: "Rascunho", c: "#6b6355", b: "#f2efe8" },
  proposta: { l: "Proposta", c: "#8a6d1f", b: "#f6edd6" },
  aguardando_aprovacao: { l: "Aguardando aprovação", c: "#8a6d1f", b: "#f6edd6" },
  aprovado: { l: "Aprovado · na fila", c: "#1a6b3a", b: "#eef7f0" },
  publicando: { l: "Publicando…", c: "#2563eb", b: "#eef3ff" },
  publicado: { l: "Publicado", c: "#2563eb", b: "#eef3ff" },
  recusado: { l: "Ajuste pedido", c: "#b3261e", b: "#fdecea" },
  // setado pelo social_publisher quando a Graph API falha (agencia-social).
  // Não é aprovação pendente — é falha técnica, a bola está com a gente.
  erro_publicacao: { l: "Falhou ao publicar", c: "#b3261e", b: "#fdecea" },
};

const FORMATO_LABEL: Record<string, string> = {
  single: "🖼 single",
  carrossel: "▤ carrossel",
  reel: "🎬 reel",
};

function labelCorretora(slug: string): string {
  return CORRETORAS.find((c) => c.slug === slug)?.label ?? slug;
}

// ── agrupamento por prioridade de ação ─────────────────────────────────
type Grupo = "acao" | "fila" | "ajuste" | "producao" | "publicado";

const PUBLICADOS_PAGINA = 20;

function imagensDe(p: Post): string[] {
  return p.carousel_urls && p.carousel_urls.length ? p.carousel_urls : p.image_url ? [p.image_url] : [];
}

/** Onde o post entra na tela. Não muda regra de negócio — só ordena a atenção. */
function grupoDe(p: Post): Grupo {
  if (p.status === "publicado") return "publicado";
  if (p.status === "aprovado" || p.status === "publicando") return "fila";
  // "erro_publicacao" (social_publisher) junto de "recusado": em ambos a bola
  // está com a gente, não com a corretora — mesma convenção do agencia-central.
  if (p.status === "recusado" || p.status === "erro_publicacao") return "ajuste";
  // rascunho | proposta | aguardando_aprovacao: só pede ação se já tem o que aprovar
  const temAlgo = p.tem_caption || imagensDe(p).length > 0;
  return temAlgo ? "acao" : "producao";
}

const GRUPOS: { id: Grupo; titulo: string; sub: string }[] = [
  { id: "acao", titulo: "Precisa de você", sub: "aprova a legenda e a arte pra entrar na fila" },
  { id: "fila", titulo: "Pronto pra publicar", sub: "aprovado — vai ao ar automaticamente" },
  { id: "ajuste", titulo: "Precisa de ajuste", sub: "com a gente — refazendo ou republicando" },
  { id: "producao", titulo: "Em produção", sub: "criando arte e legenda" },
  { id: "publicado", titulo: "Publicados", sub: "já foram ao ar" },
];

export default function PostsPage() {
  const [dados, setDados] = useState<Resposta | null>(null);
  const [erro, setErro] = useState<string | null>(null);
  const [atualizadoEm, setAtualizadoEm] = useState<Date | null>(null);
  const [filtro, setFiltro] = useEstadoPersistido<string | null>("posts_corretora_v1", null); // admin: slug da corretora
  const [busy, setBusy] = useState<Set<number>>(new Set());
  const [toast, setToast] = useState<{ msg: string; erro?: boolean } | null>(null);
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // modal de detalhe (imagem grande + copy + ações)
  const [aberto, setAberto] = useState<Post | null>(null);
  const [slide, setSlide] = useState(0);
  const [copiado, setCopiado] = useState(false);

  // modal "pedir ajuste" (recusar com motivo)
  const [ajustando, setAjustando] = useState<Post | null>(null);
  const [motivo, setMotivo] = useState("");
  const [enviandoAjuste, setEnviandoAjuste] = useState(false);

  // "Publicados" começa colapsado (é histórico, não ação) e pagina de 20 em 20
  const [publicadosAbertos, setPublicadosAbertos] = useState(false);
  const [publicadosLimite, setPublicadosLimite] = useState(PUBLICADOS_PAGINA);

  // modal "novo post" (brief)
  const [criando, setCriando] = useState(false);
  const [nvFormato, setNvFormato] = useState("single");
  const [nvRef, setNvRef] = useState("");
  const [nvObs, setNvObs] = useState("");
  const [nvCorretora, setNvCorretora] = useState("corretor"); // só admin
  const [salvandoNovo, setSalvandoNovo] = useState(false);
  const [nvErro, setNvErro] = useState<string | null>(null);

  const mostrarToast = useCallback((msg: string, erroT = false) => {
    if (toastTimer.current) clearTimeout(toastTimer.current);
    setToast({ msg, erro: erroT });
    toastTimer.current = setTimeout(() => setToast(null), 4000);
  }, []);

  const carregar = useCallback(
    async (corretora?: string | null) => {
      const alvo = corretora === undefined ? filtro : corretora;
      try {
        const qs = alvo ? `?corretora=${encodeURIComponent(alvo)}` : "";
        const r = await fetch(`/api/posts${qs}`, { cache: "no-store" });
        const data = await r.json();
        if (!r.ok) throw new Error(data.error ?? `http_${r.status}`);
        setDados(data);
        setErro(null);
        setAtualizadoEm(new Date());
      } catch (e) {
        setErro(e instanceof Error ? e.message : "erro_desconhecido");
      }
    },
    [filtro],
  );

  useEffect(() => {
    carregar();
    const t = setInterval(() => {
      if (document.visibilityState === "visible") carregar();
    }, REFRESH_MS);
    return () => clearInterval(t);
  }, [carregar]);

  // ação com atualização otimista local + reconciliação com a resposta
  const acao = useCallback(
    async (p: Post, action: string, extra: Record<string, unknown> = {}) => {
      setBusy((s) => new Set(s).add(p.id));
      try {
        const r = await fetch("/api/posts/acao", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ id: p.id, action, ...extra }),
        });
        const data = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(data.error ?? data.detail ?? `http_${r.status}`);
        const patch: Partial<Post> = { status: data.status ?? p.status };
        if ("copy_aprovada" in data) patch.copy_aprovada = data.copy_aprovada;
        if ("imagem_aprovada" in data) patch.imagem_aprovada = data.imagem_aprovada;
        if (action === "recusar") {
          patch.copy_aprovada = false;
          patch.imagem_aprovada = false;
          patch.recusa_motivo = (extra.motivo as string) || null;
        }
        if (action === "reabrir") patch.recusa_motivo = null;
        setDados((prev) =>
          prev ? { ...prev, posts: prev.posts.map((x) => (x.id === p.id ? { ...x, ...patch } : x)) } : prev,
        );
        setAberto((prev) => (prev && prev.id === p.id ? { ...prev, ...patch } : prev));
        return true;
      } catch (e) {
        mostrarToast(`não consegui (${e instanceof Error ? e.message : "erro"})`, true);
        return false;
      } finally {
        setBusy((s) => {
          const n = new Set(s);
          n.delete(p.id);
          return n;
        });
      }
    },
    [mostrarToast],
  );

  const enviarAjuste = useCallback(async () => {
    if (!ajustando || enviandoAjuste) return;
    setEnviandoAjuste(true);
    const ok = await acao(ajustando, "recusar", { motivo: motivo.trim() });
    setEnviandoAjuste(false);
    if (ok) {
      setAjustando(null);
      setMotivo("");
      mostrarToast("ajuste pedido — a gente refaz ✓");
    }
  }, [ajustando, enviandoAjuste, motivo, acao, mostrarToast]);

  const criarBrief = useCallback(async () => {
    if (salvandoNovo || !nvObs.trim()) return;
    setSalvandoNovo(true);
    setNvErro(null);
    try {
      const r = await fetch("/api/posts", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          formato: nvFormato,
          url_referencia: nvRef.trim() || null,
          observacao: nvObs.trim(),
          corretora: dados?.is_admin ? nvCorretora : null,
        }),
      });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(data.error ?? data.detail ?? `http_${r.status}`);
      setCriando(false);
      setNvRef("");
      setNvObs("");
      mostrarToast("pedido enviado — a gente cria arte e legenda ✓");
      carregar();
    } catch (e) {
      setNvErro(e instanceof Error ? e.message : "erro");
    } finally {
      setSalvandoNovo(false);
    }
  }, [salvandoNovo, nvFormato, nvRef, nvObs, nvCorretora, dados?.is_admin, mostrarToast, carregar]);

  const isAdmin = dados?.is_admin ?? false;
  const lista = dados?.posts ?? [];
  const vazio = dados != null && lista.length === 0;

  const imgsDe = imagensDe;

  // reparte a lista nos 5 grupos preservando a ordem que o copiloto mandou
  const porGrupo: Record<Grupo, Post[]> = { acao: [], fila: [], ajuste: [], producao: [], publicado: [] };
  for (const p of lista) porGrupo[grupoDe(p)].push(p);

  const abrirDetalhe = (p: Post) => { setAberto(p); setSlide(0); setCopiado(false); };

  // ── botões de aprovação (mesmas ações de sempre, só reaproveitados) ──
  const botoesAprovacao = (p: Post, grande: boolean) => {
    const imgs = imgsDe(p);
    const ocupado = busy.has(p.id);
    const fs = grande ? 12.5 : 12;
    const pad = grande ? "7px 13px" : "5px 10px";
    return (
      <div style={{ display: "flex", gap: grande ? 8 : 6, flexWrap: "wrap" }} onClick={(e) => e.stopPropagation()}>
        <button
          className="aprovBtn"
          onClick={() => acao(p, "aprovar-copy", { value: !p.copy_aprovada })}
          disabled={ocupado || !p.tem_caption}
          title={!p.tem_caption ? "a legenda ainda não ficou pronta" : p.copy_aprovada ? "clica pra desfazer" : "aprovar a legenda"}
          style={{ font: "inherit", fontSize: fs, fontWeight: 700, padding: pad, borderRadius: 8, border: p.copy_aprovada ? "1.5px solid #1a6b3a" : "1.5px solid #cfe0d4", background: p.copy_aprovada ? "#1a6b3a" : "#eef7f0", color: p.copy_aprovada ? "#fff" : "#1a6b3a", cursor: ocupado ? "wait" : "pointer", opacity: !p.tem_caption ? 0.45 : 1 }}
        >
          {p.copy_aprovada ? "✓ copy" : "aprovar copy"}
        </button>
        <button
          className="aprovBtn"
          onClick={() => acao(p, "aprovar-imagem", { value: !p.imagem_aprovada })}
          disabled={ocupado || imgs.length === 0}
          title={imgs.length === 0 ? "a arte ainda não ficou pronta" : p.imagem_aprovada ? "clica pra desfazer" : "aprovar a imagem"}
          style={{ font: "inherit", fontSize: fs, fontWeight: 700, padding: pad, borderRadius: 8, border: p.imagem_aprovada ? "1.5px solid #1a6b3a" : "1.5px solid #cfe0d4", background: p.imagem_aprovada ? "#1a6b3a" : "#eef7f0", color: p.imagem_aprovada ? "#fff" : "#1a6b3a", cursor: ocupado ? "wait" : "pointer", opacity: imgs.length === 0 ? 0.45 : 1 }}
        >
          {p.imagem_aprovada ? "✓ imagem" : "aprovar imagem"}
        </button>
        <button
          className="ajusteBtn"
          onClick={() => { setAjustando(p); setMotivo(""); }}
          disabled={ocupado}
          title="Pedir ajuste — conta o que mudar e a gente refaz"
          style={{ font: "inherit", fontSize: fs, fontWeight: 700, padding: pad, borderRadius: 8, border: "1.5px solid #f0d5d2", background: "#fff", color: "#b3261e", cursor: ocupado ? "wait" : "pointer" }}
        >
          ✎ {grande ? "pedir ajuste" : "ajuste"}
        </button>
      </div>
    );
  };

  const metaLinha = (p: Post) => {
    const s = ST[p.status] ?? { l: p.status, c: "#6b6355", b: "#f2efe8" };
    return (
      <div style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" }}>
        <span style={{ fontSize: 11, fontWeight: 700, color: s.c, background: s.b, borderRadius: 6, padding: "2px 8px" }}>{s.l}</span>
        <span style={{ fontSize: 11, color: "#8a857c" }}>{FORMATO_LABEL[p.formato] ?? p.formato}</span>
        {isAdmin && (
          <span style={{ fontSize: 11, color: "#8a857c", background: "#f2efe8", borderRadius: 6, padding: "2px 7px" }}>{labelCorretora(p.corretora)}</span>
        )}
      </div>
    );
  };

  // ── CARD GRANDE: arte grande + legenda legível + ações (grupos 1 e 2) ──
  const cardGrande = (p: Post) => {
    const imgs = imgsDe(p);
    const naFila = p.status === "aprovado" || p.status === "publicando";
    return (
      <div
        key={p.id}
        className="postCard"
        onClick={() => abrirDetalhe(p)}
        title="clica pra ver maior"
        style={{ background: "#fff", border: naFila ? "1.5px solid #cfe0d4" : "1.5px solid #ecdfb8", borderRadius: 14, overflow: "hidden", cursor: "pointer", display: "flex", gap: 0, alignItems: "stretch" }}
      >
        <div style={{ flex: "0 0 clamp(160px, 32%, 260px)", position: "relative", background: "#f2efe8", minHeight: 190 }}>
          {imgs.length > 0 ? (
            <>
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img src={imgs[0]} alt="" loading="lazy" style={{ position: "absolute", inset: 0, width: "100%", height: "100%", objectFit: "cover", display: "block" }} />
              {imgs.length > 1 && (
                <span style={{ position: "absolute", top: 8, right: 8, fontSize: 11, fontWeight: 700, color: "#fff", background: "rgba(21,17,13,.72)", borderRadius: 6, padding: "2px 8px" }}>▤ {imgs.length}</span>
              )}
            </>
          ) : (
            <div style={{ position: "absolute", inset: 0, display: "flex", alignItems: "center", justifyContent: "center", flexDirection: "column", gap: 6, color: "#a9a396" }}>
              <span style={{ fontSize: 30 }}>🎨</span>
              <span style={{ fontSize: 12 }}>arte em produção…</span>
            </div>
          )}
        </div>
        <div style={{ flex: 1, minWidth: 0, padding: "14px 16px 16px", display: "flex", flexDirection: "column", gap: 8 }}>
          {metaLinha(p)}
          {p.hook && <div style={{ fontSize: 15, fontWeight: 700, color: "#15110d", lineHeight: 1.35 }}>{p.hook}</div>}
          {p.tem_caption ? (
            <div style={{ fontSize: 13, color: "#444", lineHeight: 1.5, whiteSpace: "pre-wrap", maxHeight: 132, overflow: "hidden" }}>
              {p.caption.slice(0, 420)}{p.caption.length > 420 ? "…" : ""}
            </div>
          ) : (
            p.observacao && (
              <div style={{ fontSize: 12.5, color: "#8a857c", fontStyle: "italic", lineHeight: 1.5 }}>
                pedido: {p.observacao.slice(0, 200)}{p.observacao.length > 200 ? "…" : ""}
              </div>
            )
          )}
          <div style={{ flex: 1 }} />
          {naFila && (
            <div style={{ fontSize: 11.5, color: "#1a6b3a" }}>
              copy e imagem aprovadas — entra na fila de publicação. Ainda dá pra pedir ajuste.
            </div>
          )}
          {botoesAprovacao(p, true)}
        </div>
      </div>
    );
  };

  // ── CARD MÉDIO: ajuste pedido / em produção (informa, não grita) ──
  const cardMedio = (p: Post) => {
    const imgs = imgsDe(p);
    const ocupado = busy.has(p.id);
    return (
      <div
        key={p.id}
        className="postCard"
        onClick={() => abrirDetalhe(p)}
        title="clica pra ver maior"
        style={{ background: "#fff", border: "1px solid #e7e4dd", borderRadius: 12, overflow: "hidden", cursor: "pointer", display: "flex", gap: 12, alignItems: "stretch", padding: 10 }}
      >
        <div style={{ flex: "0 0 84px", height: 84, borderRadius: 8, overflow: "hidden", background: "#f2efe8", display: "flex", alignItems: "center", justifyContent: "center", color: "#a9a396" }}>
          {imgs.length > 0 ? (
            // eslint-disable-next-line @next/next/no-img-element
            <img src={imgs[0]} alt="" loading="lazy" style={{ width: "100%", height: "100%", objectFit: "cover", display: "block" }} />
          ) : (
            <span style={{ fontSize: 22 }}>🎨</span>
          )}
        </div>
        <div style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column", gap: 6 }}>
          {metaLinha(p)}
          {p.hook ? (
            <div style={{ fontSize: 13, fontWeight: 700, color: "#15110d", lineHeight: 1.35 }}>{p.hook}</div>
          ) : (
            p.observacao && (
              <div style={{ fontSize: 12.5, color: "#8a857c", fontStyle: "italic", lineHeight: 1.45 }}>
                pedido: {p.observacao.slice(0, 130)}{p.observacao.length > 130 ? "…" : ""}
              </div>
            )
          )}
          {p.recusa_motivo && (
            <div style={{ fontSize: 11.5, color: "#b3261e", background: "#fdecea", borderRadius: 6, padding: "4px 8px" }}>
              {p.status === "erro_publicacao" ? "falha: " : "ajuste: "}{p.recusa_motivo.slice(0, 110)}
            </div>
          )}
          <div style={{ flex: 1 }} />
          {/* "reabrir" é a única ação que o copiloto aceita e só vale pra recusado */}
          {p.status === "recusado" && (
            <button
              onClick={(e) => { e.stopPropagation(); acao(p, "reabrir"); }}
              disabled={ocupado}
              style={{ font: "inherit", fontSize: 12, fontWeight: 700, padding: "5px 10px", borderRadius: 8, border: "1px solid #e0dccf", background: "#f7f4ec", color: "#6b6355", cursor: ocupado ? "wait" : "pointer", alignSelf: "flex-start" }}
            >
              ↩︎ reabrir
            </button>
          )}
        </div>
      </div>
    );
  };

  // ── LINHA COMPACTA: publicado (miniatura + título/data + link IG) ──
  const linhaPublicado = (p: Post) => {
    const imgs = imgsDe(p);
    const quando = p.publicado_em ? new Date(p.publicado_em).toLocaleDateString("pt-BR", { day: "2-digit", month: "short" }) : p.data ?? "";
    return (
      <div
        key={p.id}
        className="postLinha"
        onClick={() => abrirDetalhe(p)}
        title="clica pra ver maior"
        style={{ display: "flex", alignItems: "center", gap: 10, padding: "7px 10px", background: "#fff", border: "1px solid #eeebe4", borderRadius: 9, cursor: "pointer" }}
      >
        <div style={{ flex: "0 0 44px", height: 44, borderRadius: 6, overflow: "hidden", background: "#f2efe8", display: "flex", alignItems: "center", justifyContent: "center", color: "#a9a396" }}>
          {imgs.length > 0 ? (
            // eslint-disable-next-line @next/next/no-img-element
            <img src={imgs[0]} alt="" loading="lazy" style={{ width: "100%", height: "100%", objectFit: "cover", display: "block" }} />
          ) : (
            <span style={{ fontSize: 15 }}>🖼</span>
          )}
        </div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: 12.5, fontWeight: 600, color: "#15110d", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
            {p.hook || (p.caption ? p.caption.slice(0, 90) : "post publicado")}
          </div>
          <div style={{ fontSize: 11, color: "#a9a396" }}>
            {quando}{quando ? " · " : ""}{FORMATO_LABEL[p.formato] ?? p.formato}
            {isAdmin ? ` · ${labelCorretora(p.corretora)}` : ""}
          </div>
        </div>
        {p.permalink && (
          <a
            href={p.permalink}
            target="_blank"
            rel="noreferrer"
            onClick={(e) => e.stopPropagation()}
            style={{ fontSize: 12, fontWeight: 700, color: "#2563eb", textDecoration: "none", whiteSpace: "nowrap" }}
          >
            ver no Instagram ↗
          </a>
        )}
      </div>
    );
  };

  return (
    // full-bleed: escapa o padding do .main do shell (36px 40px 48px), igual CRM/Tarefas
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
      <style>{`.postCard:hover { border-color: #cfc9bc !important; box-shadow: 0 2px 8px rgba(0,0,0,.07); }
.postLinha:hover { border-color: #cfc9bc !important; background: #fcfbf8 !important; }
.aprovBtn:hover { filter: brightness(.96); }
.ajusteBtn:hover { background: #fdecea !important; border-color: #e7b3ae !important; }
.novoBtn:hover { filter: brightness(1.08); }
.filtroPill:hover { border-color: #cfc9bc !important; }
.modalBtn:disabled { opacity: 0.45; cursor: default; }`}</style>

      {/* topbar — mesmo padrão do CRM/Tarefas */}
      <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "12px 18px", background: "#fff", borderBottom: "1px solid #e7e4dd", flexWrap: "wrap" }}>
        <a href="/" style={{ textDecoration: "none", color: "#888", fontSize: 13 }}>← Painel</a>
        <span style={{ color: "#ccc" }}>·</span>
        <strong style={{ color: "#15110d", fontSize: 16 }}>📸 Posts</strong>
        <span style={{ fontSize: 12, color: "#8a857c", background: "#f2efe8", borderRadius: 6, padding: "2px 8px" }}>
          manda a ideia · a gente cria arte e legenda · você aprova copy e imagem
        </span>
        <div style={{ flex: 1 }} />
        <span style={{ fontSize: 11.5, color: "#a9a396" }}>
          {atualizadoEm
            ? `atualizado ${atualizadoEm.toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" })} · refresh 60s`
            : "carregando…"}
        </span>
        <button
          className="novoBtn"
          onClick={() => { setCriando(true); setNvErro(null); }}
          style={{ font: "inherit", fontSize: 12.5, fontWeight: 700, padding: "7px 14px", borderRadius: 8, border: "1px solid #15110d", background: "#15110d", color: "#fff", cursor: "pointer" }}
        >
          + Novo post
        </button>
      </div>

      <div style={{ padding: "18px 18px 60px", maxWidth: 1080 }}>
        {/* filtro por corretora — só admin */}
        {isAdmin && (
          <div style={{ display: "flex", gap: 6, marginBottom: 14, flexWrap: "wrap" }}>
            {[null, ...CORRETORAS.map((c) => c.slug)].map((slug) => {
              const on = filtro === slug;
              return (
                <button
                  key={slug ?? "todas"}
                  className="filtroPill"
                  onClick={() => { setFiltro(slug); carregar(slug); }}
                  style={{ font: "inherit", fontSize: 12.5, fontWeight: on ? 700 : 500, padding: "5px 12px", borderRadius: 99, border: on ? "1.5px solid #15110d" : "1px solid #e7e4dd", background: on ? "#15110d" : "#fff", color: on ? "#fff" : "#6b6355", cursor: "pointer" }}
                >
                  {slug ? labelCorretora(slug) : "Todas"}
                </button>
              );
            })}
          </div>
        )}

        {erro && (
          <div style={{ color: "#a0316a", background: "#fdeef4", border: "1px solid #f3c9dd", borderRadius: 12, padding: "14px 18px", marginBottom: 16 }}>
            Não consegui carregar os posts ({erro}).
          </div>
        )}

        {vazio && (
          <div style={{ maxWidth: 560, margin: "60px auto", textAlign: "center", background: "#fff", border: "1px solid #e7e4dd", borderRadius: 16, padding: "44px 32px" }}>
            <div style={{ fontSize: 44, marginBottom: 12 }}>📸</div>
            <div style={{ fontSize: 18, fontWeight: 700, color: "#15110d", marginBottom: 8 }}>Nenhum post ainda</div>
            <div style={{ fontSize: 13.5, color: "#8a857c", lineHeight: 1.6 }}>
              Manda uma ideia pelo botão <b>+ Novo post</b> (meta {dados?.posts_por_semana ?? 2}/semana).
              A gente cria a arte e a legenda — você aprova <b>copy</b> e <b>imagem</b> e o post entra na fila.
            </div>
          </div>
        )}

        {/* seções por prioridade de ação (ver comentário no topo do arquivo) */}
        {GRUPOS.map((g) => {
          const itens = porGrupo[g.id];
          if (itens.length === 0) return null;
          const ehPublicados = g.id === "publicado";
          const aberto = !ehPublicados || publicadosAbertos;
          const visiveis = ehPublicados ? itens.slice(0, publicadosLimite) : itens;
          return (
            <section key={g.id} style={{ marginBottom: 22 }}>
              <div
                onClick={ehPublicados ? () => setPublicadosAbertos((v) => !v) : undefined}
                style={{ display: "flex", alignItems: "baseline", gap: 8, marginBottom: 10, cursor: ehPublicados ? "pointer" : "default", userSelect: "none" }}
              >
                {ehPublicados && (
                  <span style={{ fontSize: 11, color: "#a9a396", width: 10 }}>{publicadosAbertos ? "▾" : "▸"}</span>
                )}
                <h2 style={{ fontSize: 13.5, fontWeight: 700, color: "#15110d", letterSpacing: "-0.01em" }}>{g.titulo}</h2>
                <span style={{ fontSize: 11.5, fontWeight: 700, color: "#6b6355", background: "#f2efe8", borderRadius: 99, padding: "1px 8px" }}>{itens.length}</span>
                <span style={{ fontSize: 11.5, color: "#a9a396" }}>{g.sub}</span>
              </div>
              {aberto && (
                <div
                  style={
                    g.id === "acao" || g.id === "fila"
                      ? { display: "flex", flexDirection: "column", gap: 12 }
                      : ehPublicados
                        ? { display: "flex", flexDirection: "column", gap: 6 }
                        : { display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(320px, 1fr))", gap: 10 }
                  }
                >
                  {visiveis.map((p) =>
                    g.id === "acao" || g.id === "fila" ? cardGrande(p) : ehPublicados ? linhaPublicado(p) : cardMedio(p),
                  )}
                </div>
              )}
              {aberto && ehPublicados && itens.length > visiveis.length && (
                <button
                  onClick={() => setPublicadosLimite((n) => n + PUBLICADOS_PAGINA)}
                  style={{ font: "inherit", marginTop: 8, fontSize: 12, fontWeight: 600, padding: "6px 12px", borderRadius: 8, border: "1px solid #e7e4dd", background: "#fff", color: "#6b6355", cursor: "pointer" }}
                >
                  mostrar mais {Math.min(PUBLICADOS_PAGINA, itens.length - visiveis.length)} de {itens.length - visiveis.length}
                </button>
              )}
            </section>
          );
        })}
      </div>

      {/* modal detalhe — imagem grande + carrossel + copy + ações */}
      {aberto && (() => {
        const p = aberto;
        const s = ST[p.status] ?? { l: p.status, c: "#6b6355", b: "#f2efe8" };
        const imgs = imgsDe(p);
        const sl = Math.min(slide, Math.max(0, imgs.length - 1));
        const ocupado = busy.has(p.id);
        // mesma regra do agrupamento da lista: erro_publicacao é falha técnica,
        // não pendência de aprovação (senão o modal contradiz o card).
        const emAprovacao =
          p.status !== "publicado" && p.status !== "publicando" &&
          p.status !== "recusado" && p.status !== "erro_publicacao";
        return (
          <div
            onClick={() => setAberto(null)}
            style={{ position: "fixed", inset: 0, zIndex: 90, background: "rgba(15,23,42,.55)", display: "flex", alignItems: "center", justifyContent: "center", padding: 16 }}
          >
            <div
              onClick={(e) => e.stopPropagation()}
              style={{ width: "min(860px, 100%)", maxHeight: "calc(100vh - 48px)", background: "#fff", border: "1px solid #e7e4dd", borderRadius: 16, display: "flex", overflow: "hidden", boxShadow: "0 12px 40px rgba(0,0,0,.22)" }}
            >
              {/* foto */}
              <div style={{ flex: "1 1 55%", minWidth: 0, background: "#15110d", position: "relative", display: "flex", alignItems: "center", justifyContent: "center", minHeight: 320 }}>
                {imgs.length > 0 ? (
                  <>
                    {/* eslint-disable-next-line @next/next/no-img-element */}
                    <img src={imgs[sl]} alt="" style={{ maxWidth: "100%", maxHeight: "calc(100vh - 48px)", objectFit: "contain", display: "block" }} />
                    {imgs.length > 1 && (
                      <>
                        <button onClick={() => setSlide((sl - 1 + imgs.length) % imgs.length)} style={{ position: "absolute", left: 10, top: "50%", transform: "translateY(-50%)", width: 34, height: 34, borderRadius: 99, border: "none", background: "rgba(255,255,255,.85)", color: "#15110d", fontSize: 18, cursor: "pointer" }}>‹</button>
                        <button onClick={() => setSlide((sl + 1) % imgs.length)} style={{ position: "absolute", right: 10, top: "50%", transform: "translateY(-50%)", width: 34, height: 34, borderRadius: 99, border: "none", background: "rgba(255,255,255,.85)", color: "#15110d", fontSize: 18, cursor: "pointer" }}>›</button>
                        <div style={{ position: "absolute", bottom: 12, left: 0, right: 0, display: "flex", gap: 6, justifyContent: "center" }}>
                          {imgs.map((_, i) => (
                            <span key={i} onClick={() => setSlide(i)} style={{ width: 8, height: 8, borderRadius: 99, background: i === sl ? "#fff" : "rgba(255,255,255,.45)", cursor: "pointer" }} />
                          ))}
                        </div>
                      </>
                    )}
                  </>
                ) : (
                  <div style={{ color: "#a9a396", fontSize: 13, textAlign: "center", padding: 30 }}>
                    <div style={{ fontSize: 34, marginBottom: 8 }}>🎨</div>
                    arte em produção…
                  </div>
                )}
              </div>
              {/* lado direito */}
              <div style={{ flex: "1 1 45%", minWidth: 260, padding: 18, display: "flex", flexDirection: "column", gap: 10, overflowY: "auto" }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                  <span style={{ fontSize: 11.5, fontWeight: 700, color: s.c, background: s.b, borderRadius: 6, padding: "2px 9px" }}>{s.l}</span>
                  <span style={{ fontSize: 12, color: "#8a857c" }}>{FORMATO_LABEL[p.formato] ?? p.formato}{p.modo ? ` · ${p.modo}` : ""}</span>
                  {isAdmin && <span style={{ fontSize: 11, color: "#8a857c", background: "#f2efe8", borderRadius: 6, padding: "2px 7px" }}>{labelCorretora(p.corretora)}</span>}
                  <div style={{ flex: 1 }} />
                  <button onClick={() => setAberto(null)} title="fechar" style={{ border: "none", background: "transparent", fontSize: 16, color: "#8a857c", cursor: "pointer" }}>✕</button>
                </div>
                {p.hook && <div style={{ fontSize: 15, fontWeight: 700, color: "#15110d", lineHeight: 1.35 }}>{p.hook}</div>}
                <div style={{ fontSize: 13, color: "#444", whiteSpace: "pre-wrap", lineHeight: 1.55, background: "#f6f4ef", border: "1px solid #e7e4dd", borderRadius: 10, padding: "10px 12px" }}>
                  {p.caption || <span style={{ color: "#a9a396" }}>legenda ainda em produção…</span>}
                </div>
                {p.caption && (
                  <button
                    onClick={async () => {
                      try {
                        await navigator.clipboard.writeText(p.caption);
                        setCopiado(true);
                        setTimeout(() => setCopiado(false), 2000);
                      } catch { /* clipboard indisponível */ }
                    }}
                    style={{ font: "inherit", fontSize: 12.5, fontWeight: 600, padding: "6px 12px", borderRadius: 8, border: "1px solid #e7e4dd", background: "#fff", color: "#6b6355", cursor: "pointer", alignSelf: "flex-start" }}
                  >
                    {copiado ? "✓ copiado!" : "⧉ copiar legenda"}
                  </button>
                )}
                {p.observacao && (
                  <div style={{ fontSize: 12, color: "#8a857c", lineHeight: 1.5 }}>
                    <b>pedido original:</b> {p.observacao}
                    {p.url_referencia && (
                      <>
                        {" · "}
                        <a href={p.url_referencia} target="_blank" rel="noreferrer" style={{ color: "#2563eb" }}>referência ↗</a>
                      </>
                    )}
                  </div>
                )}
                {p.recusa_motivo && (p.status === "recusado" || p.status === "erro_publicacao") && (
                  <div style={{ fontSize: 12.5, color: "#b3261e", background: "#fdecea", borderRadius: 8, padding: "8px 10px" }}>
                    <b>{p.status === "erro_publicacao" ? "falhou ao publicar:" : "ajuste pedido:"}</b> {p.recusa_motivo}
                  </div>
                )}
                <div style={{ flex: 1 }} />
                {p.status === "publicado" && p.permalink ? (
                  <a href={p.permalink} target="_blank" rel="noreferrer" style={{ fontSize: 13, fontWeight: 700, color: "#2563eb", textDecoration: "none" }}>
                    ver no Instagram ↗
                  </a>
                ) : p.status === "recusado" ? (
                  <button
                    onClick={() => acao(p, "reabrir")}
                    disabled={ocupado}
                    style={{ font: "inherit", fontSize: 12.5, fontWeight: 700, padding: "7px 14px", borderRadius: 8, border: "1px solid #e0dccf", background: "#f7f4ec", color: "#6b6355", cursor: ocupado ? "wait" : "pointer", alignSelf: "flex-start" }}
                  >
                    ↩︎ reabrir pra refazer
                  </button>
                ) : emAprovacao ? (
                  <>
                    <div style={{ fontSize: 11.5, color: "#a9a396" }}>
                      copy e imagem aprovadas → o post entra na fila de publicação automaticamente.
                    </div>
                    <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                      <button
                        className="aprovBtn"
                        onClick={() => acao(p, "aprovar-copy", { value: !p.copy_aprovada })}
                        disabled={ocupado || !p.tem_caption}
                        style={{ font: "inherit", fontSize: 12.5, fontWeight: 700, padding: "7px 13px", borderRadius: 8, border: p.copy_aprovada ? "1.5px solid #1a6b3a" : "1.5px solid #cfe0d4", background: p.copy_aprovada ? "#1a6b3a" : "#eef7f0", color: p.copy_aprovada ? "#fff" : "#1a6b3a", cursor: ocupado ? "wait" : "pointer", opacity: !p.tem_caption ? 0.45 : 1 }}
                      >
                        {p.copy_aprovada ? "✓ copy aprovada" : "aprovar copy"}
                      </button>
                      <button
                        className="aprovBtn"
                        onClick={() => acao(p, "aprovar-imagem", { value: !p.imagem_aprovada })}
                        disabled={ocupado || imgs.length === 0}
                        style={{ font: "inherit", fontSize: 12.5, fontWeight: 700, padding: "7px 13px", borderRadius: 8, border: p.imagem_aprovada ? "1.5px solid #1a6b3a" : "1.5px solid #cfe0d4", background: p.imagem_aprovada ? "#1a6b3a" : "#eef7f0", color: p.imagem_aprovada ? "#fff" : "#1a6b3a", cursor: ocupado ? "wait" : "pointer", opacity: imgs.length === 0 ? 0.45 : 1 }}
                      >
                        {p.imagem_aprovada ? "✓ imagem aprovada" : "aprovar imagem"}
                      </button>
                      <button
                        className="ajusteBtn"
                        onClick={() => { setAjustando(p); setMotivo(""); }}
                        disabled={ocupado}
                        style={{ font: "inherit", fontSize: 12.5, fontWeight: 700, padding: "7px 13px", borderRadius: 8, border: "1.5px solid #f0d5d2", background: "#fff", color: "#b3261e", cursor: ocupado ? "wait" : "pointer" }}
                      >
                        ✎ pedir ajuste
                      </button>
                    </div>
                  </>
                ) : null}
              </div>
            </div>
          </div>
        );
      })()}

      {/* modal pedir ajuste (recusar com motivo) */}
      {ajustando && (
        <div
          onClick={() => !enviandoAjuste && setAjustando(null)}
          style={{ position: "fixed", inset: 0, zIndex: 95, background: "rgba(15,23,42,.45)", display: "flex", alignItems: "center", justifyContent: "center" }}
        >
          <div
            onClick={(e) => e.stopPropagation()}
            style={{ width: "min(440px, calc(100vw - 32px))", background: "#fff", border: "1px solid #e7e4dd", borderRadius: 14, padding: 16, display: "flex", flexDirection: "column", gap: 10, boxShadow: "0 12px 40px rgba(0,0,0,.18)" }}
          >
            <div style={{ fontSize: 14, fontWeight: 700, color: "#15110d" }}>✎ Pedir ajuste no post</div>
            <div style={{ fontSize: 12.5, color: "#8a857c", lineHeight: 1.5 }}>
              Conta o que mudar (na copy, na imagem ou nos dois) — ajuda a gente a refazer melhor. As aprovações são zeradas.
            </div>
            <textarea
              value={motivo}
              onChange={(e) => setMotivo(e.target.value)}
              placeholder="Ex.: troca a foto por uma da fachada e deixa a legenda mais curta"
              rows={3}
              autoFocus
              style={{ font: "inherit", fontSize: 13, padding: "9px 11px", borderRadius: 8, border: "1px solid #e7e4dd", background: "#f6f4ef", color: "#15110d", resize: "vertical" }}
            />
            <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
              <button
                onClick={() => setAjustando(null)}
                disabled={enviandoAjuste}
                style={{ font: "inherit", fontSize: 12.5, fontWeight: 600, padding: "6px 14px", borderRadius: 8, border: "1px solid #e7e4dd", background: "#fff", color: "#6b6355", cursor: enviandoAjuste ? "default" : "pointer" }}
              >
                cancelar
              </button>
              <button
                className="modalBtn"
                onClick={enviarAjuste}
                disabled={enviandoAjuste || motivo.trim().length < 3}
                style={{ font: "inherit", fontSize: 12.5, fontWeight: 700, padding: "6px 14px", borderRadius: 8, border: "1px solid #b3261e", background: "#b3261e", color: "#fff", cursor: "pointer" }}
              >
                {enviandoAjuste ? "enviando…" : "Pedir ajuste"}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* modal novo post (brief) */}
      {criando && (
        <div
          onClick={() => !salvandoNovo && setCriando(false)}
          style={{ position: "fixed", inset: 0, zIndex: 95, background: "rgba(15,23,42,.45)", display: "flex", alignItems: "center", justifyContent: "center" }}
        >
          <div
            onClick={(e) => e.stopPropagation()}
            style={{ width: "min(460px, calc(100vw - 32px))", background: "#fff", border: "1px solid #e7e4dd", borderRadius: 14, padding: 16, display: "flex", flexDirection: "column", gap: 10, boxShadow: "0 12px 40px rgba(0,0,0,.18)" }}
          >
            <div style={{ fontSize: 14, fontWeight: 700, color: "#15110d" }}>📸 Novo post — manda a ideia</div>
            <div style={{ fontSize: 12.5, color: "#8a857c", lineHeight: 1.5 }}>
              Você manda o pedido; a gente cria a arte e a legenda e te devolve aqui pra aprovar. Meta: {dados?.posts_por_semana ?? 2}/semana.
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
              <label style={{ fontSize: 11, color: "#8a857c" }}>formato</label>
              <select
                value={nvFormato}
                onChange={(e) => setNvFormato(e.target.value)}
                style={{ font: "inherit", fontSize: 13, padding: "6px 10px", borderRadius: 8, border: "1px solid #e7e4dd", background: "#f6f4ef", color: "#15110d" }}
              >
                <option value="single">single (1 imagem)</option>
                <option value="carrossel">carrossel</option>
                <option value="reel">reel</option>
              </select>
              {isAdmin && (
                <>
                  <label style={{ fontSize: 11, color: "#8a857c" }}>corretora</label>
                  <select
                    value={nvCorretora}
                    onChange={(e) => setNvCorretora(e.target.value)}
                    style={{ font: "inherit", fontSize: 13, padding: "6px 10px", borderRadius: 8, border: "1px solid #e7e4dd", background: "#f6f4ef", color: "#15110d" }}
                  >
                    {CORRETORAS.map((c) => (
                      <option key={c.slug} value={c.slug}>{c.label}</option>
                    ))}
                  </select>
                </>
              )}
            </div>
            <input
              value={nvRef}
              onChange={(e) => setNvRef(e.target.value)}
              placeholder="Link de um post-modelo que você curtiu (opcional)"
              style={{ font: "inherit", fontSize: 13, padding: "9px 11px", borderRadius: 8, border: "1px solid #e7e4dd", background: "#f6f4ef", color: "#15110d" }}
            />
            <textarea
              value={nvObs}
              onChange={(e) => setNvObs(e.target.value)}
              placeholder="O que você quer? Ex.: post sobre o lançamento X, copia a pegada desse modelo, destaca a área de lazer"
              rows={3}
              style={{ font: "inherit", fontSize: 13, padding: "9px 11px", borderRadius: 8, border: "1px solid #e7e4dd", background: "#f6f4ef", color: "#15110d", resize: "vertical" }}
            />
            {nvErro && <div style={{ fontSize: 11, color: "#b3261e" }}>não enviou ({nvErro})</div>}
            <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
              <button
                onClick={() => setCriando(false)}
                disabled={salvandoNovo}
                style={{ font: "inherit", fontSize: 12.5, fontWeight: 600, padding: "6px 14px", borderRadius: 8, border: "1px solid #e7e4dd", background: "#fff", color: "#6b6355", cursor: salvandoNovo ? "default" : "pointer" }}
              >
                cancelar
              </button>
              <button
                className="modalBtn"
                onClick={criarBrief}
                disabled={salvandoNovo || nvObs.trim().length < 3}
                style={{ font: "inherit", fontSize: 12.5, fontWeight: 700, padding: "6px 14px", borderRadius: 8, border: "1px solid #1a6b3a", background: "#1a6b3a", color: "#fff", cursor: "pointer" }}
              >
                {salvandoNovo ? "enviando…" : "Enviar pedido"}
              </button>
            </div>
          </div>
        </div>
      )}

      {toast && (
        <div style={{ position: "fixed", bottom: 24, left: "50%", transform: "translateX(-50%)", background: toast.erro ? "#b3261e" : "#15110d", color: "#fff", padding: "10px 18px", borderRadius: 10, fontSize: 13, fontWeight: 600, boxShadow: "0 6px 20px rgba(0,0,0,.2)", zIndex: 100 }}>
          {toast.msg}
        </div>
      )}
    </div>
  );
}
