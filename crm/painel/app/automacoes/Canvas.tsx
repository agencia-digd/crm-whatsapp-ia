"use client";

// 🗺 CANVAS — o desenho de fluxo estilo editor de salesbot do Kommo: caixas
// brancas ligadas por fios em SVG, sobre grade de pontos, com zoom e scroll.
//
// Nasceu dentro do FluxoView (mapa geral). Quando a página de UMA automação
// precisou do MESMO desenho, o caminho fácil seria copiar — e dois canvas
// viram dois desenhos diferentes no primeiro ajuste. Então a maquinaria
// (registro dos nós, medição, fios, zoom, viewport) mora aqui e os dois
// consumidores só declaram nós e arestas.
//
// Como as linhas são desenhadas: cada nó se registra num Map por id via ref
// callback; depois do layout a gente mede as caixas (getBoundingClientRect ÷
// zoom pra voltar ao espaço de layout, já que o canvas usa transform:scale) e
// gera paths bezier num <svg> absoluto dentro do próprio canvas.
//
// Um nó que cresce (abrir o editor de copy dentro dele) muda a altura da caixa
// e deslocaria os fios: por isso o ResizeObserver re-mede sozinho.

import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import s from "./fluxo.module.css";

export type Tone = "base" | "warn" | "muted";
export type Edge = {
  from: string;
  to: string;
  kind: "h" | "branch";
  tone?: Tone;
  dashed?: boolean;
};
type Wire = { d: string; tone: Tone; dashed: boolean };

export const STROKE: Record<Tone, string> = {
  base: "#b9b2a3",
  warn: "#c78a1a",
  muted: "#d8d3c8",
};

/** Encadeia ids em arestas horizontais (a → b → c). */
export function corrente(ids: string[], tone: Tone = "base"): Edge[] {
  const es: Edge[] = [];
  for (let i = 0; i < ids.length - 1; i++) {
    es.push({ from: ids[i], to: ids[i + 1], kind: "h", tone, dashed: tone === "muted" });
  }
  return es;
}

export type RegNo = (id: string) => (el: HTMLDivElement | null) => void;

export default function Canvas({
  edges,
  zoomInicial = 0.8,
  altura,
  children,
}: {
  edges: Edge[];
  zoomInicial?: number;
  /** CSS height do viewport. Padrão: o do mapa geral. */
  altura?: string;
  children: (reg: RegNo) => React.ReactNode;
}) {
  const nodesRef = useRef(new Map<string, HTMLDivElement>());
  const canvasRef = useRef<HTMLDivElement>(null);
  const [zoom, setZoom] = useState(zoomInicial);
  const [wires, setWires] = useState<Wire[]>([]);
  const [dims, setDims] = useState({ w: 1200, h: 800 });

  const reg = useCallback<RegNo>(
    (id: string) => (el: HTMLDivElement | null) => {
      if (el) nodesRef.current.set(id, el);
      else nodesRef.current.delete(id);
    },
    [],
  );

  const measure = useCallback(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const cr = canvas.getBoundingClientRect();
    const z = zoom || 1;
    const get = (id: string) => {
      const el = nodesRef.current.get(id);
      if (!el) return null;
      const r = el.getBoundingClientRect();
      return {
        x: (r.left - cr.left) / z,
        y: (r.top - cr.top) / z,
        w: r.width / z,
        h: r.height / z,
      };
    };
    const out: Wire[] = [];
    for (const e of edges) {
      const p = get(e.from);
      const q = get(e.to);
      if (!p || !q) continue;
      let d: string;
      if (e.kind === "branch") {
        const sx = p.x + Math.min(70, p.w / 2);
        const sy = p.y + p.h;
        const ex = q.x + Math.min(70, q.w / 2);
        const ey = q.y;
        d = `M ${sx} ${sy} C ${sx} ${sy + 70}, ${ex} ${ey - 70}, ${ex} ${ey}`;
      } else {
        const sx = p.x + p.w;
        const sy = p.y + Math.min(24, p.h / 2);
        const ex = q.x;
        const ey = q.y + Math.min(24, q.h / 2);
        const dx = Math.max(28, (ex - sx) / 2);
        d = `M ${sx} ${sy} C ${sx + dx} ${sy}, ${ex - dx} ${ey}, ${ex - 6} ${ey}`;
      }
      out.push({ d, tone: e.tone ?? "base", dashed: !!e.dashed });
    }
    setWires(out);
    setDims({ w: canvas.scrollWidth, h: canvas.scrollHeight });
  }, [edges, zoom]);

  useLayoutEffect(() => {
    measure();
  }, [measure, children]);

  useEffect(() => {
    const onResize = () => measure();
    window.addEventListener("resize", onResize);
    // fontes carregando mudam a altura dos balões → re-mede
    if (typeof document !== "undefined" && (document as any).fonts?.ready) {
      (document as any).fonts.ready.then(() => measure());
    }
    // nó que expande (editor de copy aberto) muda a altura da caixa
    let ro: ResizeObserver | null = null;
    if (typeof ResizeObserver !== "undefined" && canvasRef.current) {
      ro = new ResizeObserver(() => measure());
      ro.observe(canvasRef.current);
      nodesRef.current.forEach((el) => ro?.observe(el));
    }
    const t = setTimeout(measure, 300);
    return () => {
      window.removeEventListener("resize", onResize);
      ro?.disconnect();
      clearTimeout(t);
    };
  }, [measure]);

  const zoomTo = (dz: number) =>
    setZoom((z) => Math.min(1.25, Math.max(0.4, Math.round((z + dz) * 100) / 100)));

  return (
    <div className={s.stage}>
      <div className={s.zoomCtl}>
        <button className={s.zoomBtn} onClick={() => zoomTo(-0.1)} aria-label="menos zoom">
          −
        </button>
        <span className={s.zoomPct}>{Math.round(zoom * 100)}%</span>
        <button className={s.zoomBtn} onClick={() => zoomTo(0.1)} aria-label="mais zoom">
          +
        </button>
      </div>

      <div className={s.viewport} style={altura ? { height: altura } : undefined}>
        <div style={{ width: dims.w * zoom, height: dims.h * zoom }}>
          <div ref={canvasRef} className={s.canvas} style={{ transform: `scale(${zoom})` }}>
            <svg className={s.wires} width={dims.w} height={dims.h}>
              <defs>
                {(Object.keys(STROKE) as Tone[]).map((t) => (
                  <marker
                    key={t}
                    id={`arr-${t}`}
                    viewBox="0 0 10 10"
                    refX="8"
                    refY="5"
                    markerWidth="7"
                    markerHeight="7"
                    orient="auto-start-reverse"
                  >
                    <path d="M 0 0 L 10 5 L 0 10 z" fill={STROKE[t]} />
                  </marker>
                ))}
              </defs>
              {wires.map((w, i) => (
                <path
                  key={i}
                  d={w.d}
                  fill="none"
                  stroke={STROKE[w.tone]}
                  strokeWidth={1.7}
                  strokeDasharray={w.dashed ? "6 5" : undefined}
                  markerEnd={`url(#arr-${w.tone})`}
                />
              ))}
            </svg>

            {children(reg)}
          </div>
        </div>
      </div>
    </div>
  );
}
