"use client";

// Catálogo PRÓPRIO da Acme (empreendimentos da carteira, ~114 imóveis).
// Match-maker: a corretora filtra por tipo/preço/quartos/entrega/material e
// busca por texto; o painel mostra selo de match (✓ casa / ≈ parcial) contra os
// filtros. "⚡ Inserir na conversa" monta a msg WhatsApp formatada e joga no
// CAMPO DE TEXTO do chat pra corretora revisar e enviar (NUNCA envia automático).
// Porte 1:1 do V1 (dash-imobiliaria/app/page.tsx, painel "empreend").
// Diferente do "🔎 Buscar imóvel na web" — aquele é busca EXTERNA; este é a carteira.

import { useCallback, useEffect, useMemo, useState } from "react";
import s from "./chat.module.css";

type Emp = {
  empreendimento: string;
  construtora: string;
  tipo: string;
  quartos: string;
  quartos_norm: number | null;
  preco_bruto: string;
  preco_num: number | null;
  preco_fmt: string;
  entrega_bucket: "pronto" | "lancamento" | "data" | null;
  entrega_fmt: string;
  zona: string;
  bairro: string;
  decorado: string;
  drive: string;
  tem_material: boolean;
};

// Monta a mensagem WhatsApp multi-linha do empreendimento, pulando campos vazios.
export function montarMsgEmpreendimento(e: Emp): string {
  const linhas: string[] = [];
  linhas.push(`🏢 *${e.empreendimento}*`);
  const local = [e.bairro, e.zona].filter(Boolean).join(", ");
  if (local) linhas.push(`📍 ${local}`);
  if (e.preco_num) linhas.push(`💰 A partir de ${e.preco_fmt}`);
  if (e.quartos_norm) linhas.push(`🛏️ ${e.quartos} quartos`);
  if (e.entrega_fmt) linhas.push(`🔑 ${e.entrega_fmt}`);
  if (e.construtora) linhas.push(`🏗️ ${e.construtora}`);
  if (e.tem_material) linhas.push(`📎 ${e.drive}`);
  return linhas.join("\n");
}

export default function Empreendimentos({
  onClose,
  onInserir,
}: {
  onClose: () => void;
  onInserir: (msg: string) => void;
}) {
  const [itens, setItens] = useState<Emp[] | null>(null);
  const [info, setInfo] = useState<{ configurado?: boolean; erro?: string }>({});
  const [at, setAt] = useState<string | null>(null);
  const [stale, setStale] = useState(false);

  const [busca, setBusca] = useState("");
  const [fTipo, setFTipo] = useState<Set<string>>(new Set());
  const [fPreco, setFPreco] = useState<number | null>(null); // teto em reais
  const [fQuartos, setFQuartos] = useState<Set<number>>(new Set());
  const [fEntrega, setFEntrega] = useState<Set<string>>(new Set());
  const [fZona, setFZona] = useState<Set<string>>(new Set());
  const [fMaterial, setFMaterial] = useState(false);
  const [aberto, setAberto] = useState<number | null>(null);
  const [inserido, setInserido] = useState<number | null>(null);

  useEffect(() => {
    const onEsc = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onEsc);
    return () => window.removeEventListener("keydown", onEsc);
  }, [onClose]);

  useEffect(() => {
    fetch("/api/empreendimentos")
      .then((r) => r.json())
      .then((d) => {
        setItens(d?.itens ?? []);
        setInfo({ configurado: d?.configurado, erro: d?.erro });
        setAt(d?.atualizado_em ?? null);
        setStale(Boolean(d?.stale));
      })
      .catch(() => { setItens([]); setInfo({ configurado: false }); });
  }, []);

  const toggle = useCallback(<T,>(set: Set<T>, val: T, setter: (s: Set<T>) => void) => {
    const n = new Set(set);
    n.has(val) ? n.delete(val) : n.add(val);
    setter(n);
  }, []);

  const lista = itens || [];
  const temFiltro =
    fTipo.size > 0 || fPreco != null || fQuartos.size > 0 || fEntrega.size > 0 ||
    fZona.size > 0 || fMaterial || busca.trim() !== "";

  const { filtrados, casam } = useMemo(() => {
    function passa(e: Emp): { ok: boolean; parcial: boolean } {
      let parcial = false;
      const q = busca.trim().toLowerCase();
      if (q && ![e.empreendimento, e.bairro, e.zona, e.construtora].join(" ").toLowerCase().includes(q)) return { ok: false, parcial };
      if (fTipo.size && !fTipo.has((e.tipo || "").split(/[ /]/)[0])) return { ok: false, parcial };
      if (fZona.size) { const z = (e.zona || "").toLowerCase(); if (!Array.from(fZona).some((k) => z.includes(k))) return { ok: false, parcial }; }
      if (fPreco != null) { if (e.preco_num == null) parcial = true; else if (e.preco_num > fPreco) return { ok: false, parcial }; }
      if (fQuartos.size) { if (e.quartos_norm == null) parcial = true; else if (!Array.from(fQuartos).some((n) => e.quartos_norm === n || (n === 3 && (e.quartos_norm ?? 0) >= 3))) return { ok: false, parcial }; }
      if (fEntrega.size) { if (!e.entrega_bucket) parcial = true; else if (!fEntrega.has(e.entrega_bucket)) return { ok: false, parcial }; }
      if (fMaterial && !e.tem_material) return { ok: false, parcial };
      return { ok: true, parcial };
    }
    const f = lista.map((e) => ({ e, m: passa(e) })).filter((x) => x.m.ok)
      .sort((a, b) => Number(a.m.parcial) - Number(b.m.parcial) || ((a.e.preco_num ?? 9e9) - (b.e.preco_num ?? 9e9)));
    return { filtrados: f, casam: f.filter((x) => !x.m.parcial).length };
  }, [lista, busca, fTipo, fPreco, fQuartos, fEntrega, fZona, fMaterial]);

  const limpar = () => { setBusca(""); setFTipo(new Set()); setFPreco(null); setFQuartos(new Set()); setFEntrega(new Set()); setFZona(new Set()); setFMaterial(false); };

  const inserir = (e: Emp, i: number, soLink: boolean) => {
    const msg = soLink ? e.drive : montarMsgEmpreendimento(e);
    onInserir(msg);
    setInserido(i);
    setTimeout(() => setInserido((cur) => (cur === i ? null : cur)), 1600);
  };

  const TIPOS: [string, string][] = [["APTO", "🏢 Apto"], ["CASA", "🏡 Casa"], ["TERRENO", "🟩 Terreno"]];
  const PRECOS: [number, string][] = [[250000, "até 250k"], [350000, "até 350k"], [500000, "até 500k"]];
  const ENTREGAS: [string, string][] = [["pronto", "✅ Pronto"], ["lancamento", "🏗️ Lançam."], ["data", "📅 Com data"]];
  const ZONAS: [string, string][] = [["norte", "Norte"], ["sul", "Sul"], ["leste", "Leste"], ["oeste", "Oeste"], ["central", "Central"], ["votorantim", "Bairro X"]];

  const idade = (() => {
    if (!at) return null;
    const h = Math.max(0, (Date.now() - new Date(at).getTime()) / 3600000);
    const txt = h < 1 ? "há menos de 1h" : h < 48 ? `há ${Math.floor(h)}h` : `há ${Math.floor(h / 24)} dias`;
    const cls = h < 6 ? s.empFrescorOk : h < 24 ? s.empFrescorMed : s.empFrescorVelho;
    return { txt, cls, velho: h >= 24 };
  })();

  return (
    <div className={s.buscaWebOverlay} onClick={onClose}>
      <div className={s.buscaWebModal} onClick={(e) => e.stopPropagation()}>
        <div className={s.buscaWebHead}>
          <span className={s.buscaWebTitulo}>🏢 Empreendimentos</span>
          <span className={s.buscaWebNota}>
            catálogo PRÓPRIO da Acme ({lista.length} imóveis) · monta a msg pra você revisar — não vai pro lead sozinho
          </span>
          <button className={s.buscaWebClose} onClick={onClose} aria-label="Fechar">✕</button>
        </div>

        <div className={s.empFiltros}>
          <input
            className={s.empBusca}
            value={busca}
            onChange={(e) => setBusca(e.target.value)}
            placeholder="🔍 nome, bairro, construtora…"
            autoFocus
          />
          <div className={s.empPills}>
            {TIPOS.map(([v, l]) => <button key={v} className={fTipo.has(v) ? s.empPillOn : s.empPill} onClick={() => toggle(fTipo, v, setFTipo)}>{l}</button>)}
            {PRECOS.map(([v, l]) => <button key={v} className={fPreco === v ? s.empPillOn : s.empPill} onClick={() => setFPreco(fPreco === v ? null : v)}>{l}</button>)}
          </div>
          <div className={s.empPills}>
            {[1, 2, 3].map((n) => <button key={n} className={fQuartos.has(n) ? s.empPillOn : s.empPill} onClick={() => toggle(fQuartos, n, setFQuartos)}>{n === 3 ? "3+" : n} 🛏️</button>)}
            {ENTREGAS.map(([v, l]) => <button key={v} className={fEntrega.has(v) ? s.empPillOn : s.empPill} onClick={() => toggle(fEntrega, v, setFEntrega)}>{l}</button>)}
            <button className={fMaterial ? s.empPillOn : s.empPill} onClick={() => setFMaterial(!fMaterial)}>📎 com material</button>
          </div>
          <div className={s.empPills}>
            {ZONAS.map(([k, l]) => <button key={k} className={fZona.has(k) ? s.empPillOn : s.empPill} onClick={() => toggle(fZona, k, setFZona)}>📍 {l}</button>)}
            {temFiltro && <button className={s.empLimpar} onClick={limpar}>✕ limpar</button>}
          </div>
          <div className={s.empContador}>
            {info.configurado === false
              ? "planilha não conectada"
              : `${filtrados.length} de ${lista.length}${temFiltro ? ` · ${casam} casam` : ""}`}
            {idade && <span className={idade.cls}>catálogo atualizado {idade.txt}{stale ? " · snapshot" : ""}{idade.velho ? " · Mac pode estar deslogado" : ""}</span>}
          </div>
        </div>

        <div className={s.buscaWebBody}>
          {itens === null && <div className={s.buscaWebLoading}>carregando catálogo…</div>}
          {itens !== null && info.configurado === false && (
            <div className={s.buscaWebErro}>Planilha não conectada — o Mac (fetcher) pode estar fora. {info.erro || ""}</div>
          )}
          {itens !== null && info.configurado !== false && filtrados.length === 0 && (
            <div className={s.buscaWebVazio}>Nenhum empreendimento com esses filtros. Afrouxa algum.</div>
          )}
          {filtrados.length > 0 && (
            <div className={s.buscaWebGrid}>
              {filtrados.map(({ e, m }, i) => {
                const tb = (e.tipo || "").startsWith("CASA") ? "🏡" : (e.tipo || "").startsWith("TERR") ? "🟩" : "🏢";
                const eb = e.entrega_bucket === "pronto" ? { l: "✅ Pronto", cls: s.empBadgeOk }
                  : e.entrega_bucket === "lancamento" ? { l: "🏗️ Lançamento", cls: s.empBadgeAzul }
                  : e.entrega_bucket ? { l: `📅 ${e.entrega_fmt}`, cls: s.empBadgeAmar } : null;
                return (
                  <div key={i} className={s.imovelCard}>
                    {temFiltro && <div className={m.parcial ? s.empMatchParc : s.empMatchOk}>{m.parcial ? "≈ casa parcial" : "✓ casa com o lead"}</div>}
                    <div className={s.imovelTop}>
                      <span className={s.imovelTitulo}>{tb} {e.empreendimento}</span>
                      <span className={s.imovelPreco}>{e.preco_num ? e.preco_fmt : "sob consulta"}</span>
                    </div>
                    <div className={s.imovelMeta}>
                      📍 {[e.bairro, e.zona].filter(Boolean).join(" · ") || "—"}
                      {e.quartos_norm ? ` · ${e.quartos} quartos` : ""}
                      {e.construtora ? ` · ${e.construtora}` : ""}
                    </div>
                    <div className={s.empBadges}>
                      {eb && <span className={eb.cls}>{eb.l}</span>}
                      <span className={e.tem_material ? s.empBadgeOk : s.empBadgeCinza}>{e.tem_material ? "📎 material" : "sem material"}</span>
                    </div>
                    <div className={s.empAcoes}>
                      <button className={s.empInserir} onClick={() => inserir(e, i, false)}>
                        {inserido === i ? "✓ no campo" : "⚡ Inserir na conversa"}
                      </button>
                      {e.tem_material && (
                        <button className={s.empSoLink} onClick={() => inserir(e, i, true)} title="Insere só o link do Drive no campo">📎 só link</button>
                      )}
                      <button className={s.empDetalhes} onClick={() => setAberto(aberto === i ? null : i)}>
                        {aberto === i ? "▲" : "🔎"}
                      </button>
                    </div>
                    {aberto === i && (
                      <div className={s.empDet}>
                        <div className={s.empDetLinha}><span>Construtora</span><b>{e.construtora || "—"}</b></div>
                        <div className={s.empDetLinha}><span>Tipo</span><b>{e.tipo || "—"}{e.quartos_norm ? ` · ${e.quartos} quartos` : ""}</b></div>
                        <div className={s.empDetLinha}><span>Preço</span><b>{e.preco_num ? `${e.preco_fmt} (${e.preco_bruto})` : "sob consulta"}</b></div>
                        <div className={s.empDetLinha}><span>Entrega</span><b>{e.entrega_fmt || "—"}</b></div>
                        <div className={s.empDetLinha}><span>Local</span><b>{[e.bairro, e.zona].filter(Boolean).join(" · ") || "—"}</b></div>
                        {e.decorado && <div className={s.empDetLinha}><span>Decorado</span><b>{e.decorado}</b></div>}
                        {e.tem_material
                          ? <a className={s.empMaterial} href={e.drive} target="_blank" rel="noreferrer">📂 Abrir material no Drive (só você vê)</a>
                          : <div className={s.empSemMat}>sem material no Drive</div>}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
