"use client";

// 🧱 Blocos compartilhados das telas de automação.
//
// Nasceram duplicados em Cliente.tsx (vista Cards) e FluxoView.tsx (canvas).
// Quando a tela virou "lista de automações → página de cada automação", a
// página do Follow-up precisou dos MESMOS blocos — e três cópias do balão do
// WhatsApp seria três balões diferentes na primeira mudança de copy.
//
// Nada aqui tem estado de rede além do CopyEditor (que fala com
// /api/automacoes/copies, o mesmo endpoint de sempre).

import { useState } from "react";
import a from "./automacoes.module.css";
import s from "./fluxo.module.css";
import { CopyItem } from "./types";

// `indisponivel` = o servidor não conseguiu ler se a automação está ligada.
// Aparece como "sem leitura", apagado: escrever "desligado" seria afirmar uma
// leitura que não houve.
export function Badge({ estado }: { estado: "ligado" | "desligado" | "dry-run" | "indisponivel" }) {
  const cls = estado === "ligado" ? a.badgeOn : estado === "dry-run" ? a.badgeDry : a.badgeOff;
  return <span className={`${a.badge} ${cls}`}>{estado === "indisponivel" ? "sem leitura" : estado}</span>;
}

// ── nó genérico (caixa branca com título numerado, como no Kommo) ──
// `reg` é opcional: no canvas do Fluxo ele registra a caixa pra medir os fios;
// em layouts sem SVG (página de uma automação) não existe fio pra medir.
export function Node({
  reg,
  id,
  num,
  icon,
  titulo,
  badge,
  muted,
  cond,
  wide,
  largura,
  children,
}: {
  reg?: (id: string) => (el: HTMLDivElement | null) => void;
  id: string;
  num?: number;
  icon?: string;
  titulo: string;
  badge?: React.ReactNode;
  muted?: boolean;
  cond?: boolean;
  wide?: boolean;
  /** Largura em px. Sem isso vale a do CSS (252px) — o canvas geral não muda. */
  largura?: number;
  children?: React.ReactNode;
}) {
  return (
    <div
      ref={reg ? reg(id) : undefined}
      style={largura ? { width: largura } : undefined}
      className={`${s.node} ${muted ? s.muted : ""} ${cond ? s.cond : ""} ${wide ? s.wide : ""}`}
    >
      <div className={s.nodeHead}>
        {num != null && <span className={s.nodeNum}>{num}</span>}
        {icon && <span className={s.nodeIcon}>{icon}</span>}
        <span className={s.nodeTitulo}>{titulo}</span>
        {badge && <span className={s.nodeBadge}>{badge}</span>}
      </div>
      <div className={s.nodeBody}>{children}</div>
    </div>
  );
}

// ── pausa (pílula "espera N dias") ──
export function Espera({
  reg,
  id,
  texto,
  muted,
}: {
  reg?: (id: string) => (el: HTMLDivElement | null) => void;
  id: string;
  texto: string;
  muted?: boolean;
}) {
  return (
    <div ref={reg ? reg(id) : undefined} className={`${s.wait} ${muted ? s.muted : ""}`}>
      ⏸ {texto}
    </div>
  );
}

export function Balao({ children }: { children: React.ReactNode }) {
  return <div className={s.balao}>{children}</div>;
}

/** Preview visual = exatamente o que o lead vê no WhatsApp. */
export function WaPreview({
  preview,
  comBotao,
  botoes,
  template,
}: {
  preview?: string | null;
  comBotao?: boolean;
  botoes?: string[];
  template?: string;
}) {
  if (!preview) return null;
  const btns =
    botoes && botoes.length > 0 ? botoes : comBotao ? ["Saiba mais", "Parar mensagens"] : [];
  return (
    <div className={a.waFrame}>
      <div className={a.waBubble}>{preview}</div>
      {btns.length > 0 && (
        <div className={a.waBtns}>
          {btns.map((b) => (
            <span key={b} className={a.waBtn}>
              {b}
            </span>
          ))}
        </div>
      )}
      <div className={a.waMeta}>
        <span className={a.tag}>{comBotao ? "com botão" : "sem botão"}</span>
        {template && <span className={a.tag}>{template}</span>}
      </div>
    </div>
  );
}

// ── Editor de copy de um toque (personalizado vs padrão) ──
export function CopyEditor({ item, onMudou }: { item: CopyItem; onMudou: () => void }) {
  const [editando, setEditando] = useState(false);
  const [texto, setTexto] = useState(item.efetiva);
  const [salvando, setSalvando] = useState(false);
  const [erro, setErro] = useState<string | null>(null);

  const abrir = () => {
    setTexto(item.efetiva);
    setErro(null);
    setEditando(true);
  };

  const chamar = async (metodo: "PUT" | "DELETE", body?: unknown) => {
    setSalvando(true);
    setErro(null);
    try {
      const r = await fetch(`/api/automacoes/copies/${item.toque}`, {
        method: metodo,
        headers: body ? { "Content-Type": "application/json" } : undefined,
        body: body ? JSON.stringify(body) : undefined,
      });
      const out = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(out?.detail || out?.error || `HTTP ${r.status}`);
      setEditando(false);
      onMudou();
    } catch (e) {
      setErro(String((e as Error).message || e));
    } finally {
      setSalvando(false);
    }
  };

  if (!editando) {
    return (
      <>
        <div className={a.copy}>{item.efetiva}</div>
        <div className={a.copyBar}>
          <span className={`${a.badge} ${item.personalizada ? a.badgeCustom : a.badgeOff}`}>
            {item.personalizada ? "personalizada" : "padrão"}
          </span>
          {item.personalizada && item.atualizado_em && (
            <span className={a.copyMeta}>
              editada em {new Date(item.atualizado_em).toLocaleString("pt-BR")}
              {item.atualizado_por ? ` por ${item.atualizado_por}` : ""}
            </span>
          )}
          <span style={{ flex: 1 }} />
          <button className={a.btnMini} onClick={abrir} disabled={salvando}>
            ✏️ editar
          </button>
          {item.personalizada && (
            <button
              className={a.btnMini}
              onClick={() => chamar("DELETE")}
              disabled={salvando}
              title="Volta pro texto padrão do sistema"
            >
              ↩ restaurar padrão
            </button>
          )}
        </div>
        {erro && <div className={a.copyErro}>{erro}</div>}
      </>
    );
  }

  return (
    <div className={a.editor}>
      <textarea
        className={a.editorArea}
        value={texto}
        onChange={(e) => setTexto(e.target.value)}
        rows={4}
        maxLength={900}
        disabled={salvando}
      />
      <div className={a.copyBar}>
        <span className={a.copyMeta}>
          {texto.length}/900 · precisa terminar com pergunta (?)
        </span>
        <span style={{ flex: 1 }} />
        <button
          className={`${a.btnMini} ${a.btnSalvar}`}
          onClick={() => chamar("PUT", { texto })}
          disabled={salvando || !texto.trim()}
        >
          {salvando ? "salvando…" : "💾 salvar"}
        </button>
        <button className={a.btnMini} onClick={() => setEditando(false)} disabled={salvando}>
          cancelar
        </button>
      </div>
      {item.personalizada && (
        <div className={a.copyPadraoRef}>
          <span className={a.copyMeta}>padrão do sistema:</span> {item.default}
        </div>
      )}
      {erro && <div className={a.copyErro}>{erro}</div>}
    </div>
  );
}
