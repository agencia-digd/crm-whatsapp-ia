"use client";

import { useCallback, useEffect, useRef, useState } from "react";

// Galeria da janela "Editar imóvel": vê as fotos, exclui (×), adiciona (+) e
// arrasta pra mudar a ordem. A 1ª é a capa do card — a regra mora no copiloto
// (`_capa`), aqui só se mostra o selo.
//
// Arrastar é feito na mão com pointer events (sem biblioteca) porque precisa
// funcionar no celular, que é onde a corretora edita. No toque o arrasto só
// começa depois de segurar ~250ms: antes disso o dedo rola a janela normal.

type Foto = { id: number; ordem: number; tratada: boolean };

const SEGURAR_MS = 250;
const LIMIAR_MOUSE = 5;
const LIMIAR_TOQUE = 8;

export default function GaleriaFotos({
  imovelId,
  onMudou,
}: {
  imovelId: number;
  onMudou: () => void;
}) {
  const [fotos, setFotos] = useState<Foto[] | null>(null);
  const [erro, setErro] = useState<string | null>(null);
  const [enviando, setEnviando] = useState(0);
  const [arrastando, setArrastando] = useState<number | null>(null);
  const input = useRef<HTMLInputElement>(null);
  const fotosRef = useRef(fotos);
  fotosRef.current = fotos;
  const gesto = useRef<{
    id: number;
    x: number;
    y: number;
    toque: boolean;
    ativo: boolean;
    timer: ReturnType<typeof setTimeout> | null;
    ordemAntes: number[];
    el: HTMLElement;
    pointerId: number;
  } | null>(null);

  const originais = (lista: Foto[]) => lista.filter((f) => !f.tratada);

  const carregar = useCallback(async () => {
    try {
      const r = await fetch(`/api/imoveis/${imovelId}`, { cache: "no-store" });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail ?? d.error ?? `http_${r.status}`);
      setFotos(originais(d.fotos ?? []));
    } catch (e) {
      setErro(e instanceof Error ? e.message : "nao_consegui_carregar_as_fotos");
      setFotos((f) => f ?? []);
    }
  }, [imovelId]);

  useEffect(() => {
    carregar();
  }, [carregar]);

  // ── adicionar ──────────────────────────────────────────────────────────────
  async function adicionar(files: FileList | null) {
    if (!files?.length) return;
    const lista = Array.from(files);
    setErro(null);
    setEnviando(lista.length);
    let falhas = 0;
    for (const arq of lista) {
      const fd = new FormData();
      fd.append("arquivo", arq);
      const r = await fetch(`/api/imoveis/${imovelId}/fotos`, { method: "POST", body: fd })
        .catch(() => null);
      const d = r ? await r.json().catch(() => null) : null;
      if (r?.ok && d?.fotos) {
        setFotos(originais(d.fotos));
        onMudou();
      } else {
        falhas += 1;
      }
      setEnviando((n) => n - 1);
    }
    if (falhas) setErro(`${falhas} foto(s) não subiram — tenta de novo`);
    if (input.current) input.current.value = "";
  }

  // ── excluir ────────────────────────────────────────────────────────────────
  async function excluir(id: number) {
    if (!window.confirm("Excluir esta foto?")) return;
    setErro(null);
    setFotos((f) => (f ? f.filter((x) => x.id !== id) : f));
    const r = await fetch(`/api/imoveis/fotos/${id}/arquivar`, { method: "POST" }).catch(() => null);
    if (!r?.ok) {
      setErro("não consegui excluir a foto");
      await carregar();
      return;
    }
    onMudou();
  }

  // ── reordenar ──────────────────────────────────────────────────────────────
  async function gravarOrdem(ordem: number[], antes: number[]) {
    if (ordem.join() === antes.join()) return;
    setErro(null);
    const r = await fetch(`/api/imoveis/${imovelId}/fotos/ordem`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ordem }),
    }).catch(() => null);
    if (!r?.ok) {
      setErro(
        r?.status === 409
          ? "as fotos mudaram enquanto você arrastava — recarreguei, tenta de novo"
          : "não consegui salvar a nova ordem",
      );
      await carregar();
      return;
    }
    onMudou();
  }

  function mover(id: number, para: number) {
    setFotos((f) => {
      if (!f) return f;
      const de = f.findIndex((x) => x.id === id);
      if (de < 0 || de === para) return f;
      const nova = [...f];
      const [item] = nova.splice(de, 1);
      nova.splice(para, 0, item);
      return nova;
    });
  }

  const bloquearRolagem = useCallback((e: TouchEvent) => e.preventDefault(), []);

  function iniciarArrasto() {
    const g = gesto.current;
    if (!g) return;
    g.ativo = true;
    setArrastando(g.id);
    try {
      g.el.setPointerCapture(g.pointerId);
    } catch {
      /* ponteiro já foi solto */
    }
    document.addEventListener("touchmove", bloquearRolagem, { passive: false });
    navigator.vibrate?.(15);
  }

  function encerrar(gravar: boolean) {
    const g = gesto.current;
    gesto.current = null;
    if (!g) return;
    if (g.timer) clearTimeout(g.timer);
    document.removeEventListener("touchmove", bloquearRolagem);
    if (!g.ativo) return;
    setArrastando(null);
    if (gravar && fotosRef.current) gravarOrdem(fotosRef.current.map((x) => x.id), g.ordemAntes);
  }

  function onPointerDown(e: React.PointerEvent<HTMLDivElement>, id: number) {
    if ((e.target as HTMLElement).closest("button")) return;
    if (e.pointerType === "mouse" && e.button !== 0) return;
    const toque = e.pointerType !== "mouse";
    gesto.current = {
      id,
      x: e.clientX,
      y: e.clientY,
      toque,
      ativo: false,
      timer: null,
      ordemAntes: (fotos ?? []).map((x) => x.id),
      el: e.currentTarget,
      pointerId: e.pointerId,
    };
    if (toque) gesto.current.timer = setTimeout(iniciarArrasto, SEGURAR_MS);
  }

  function onPointerMove(e: React.PointerEvent<HTMLDivElement>) {
    const g = gesto.current;
    if (!g) return;
    const dist = Math.hypot(e.clientX - g.x, e.clientY - g.y);
    if (!g.ativo) {
      if (g.toque) {
        if (dist > LIMIAR_TOQUE) encerrar(false); // é rolagem, não arrasto
      } else if (dist > LIMIAR_MOUSE) {
        iniciarArrasto();
      }
      return;
    }
    const alvo = document
      .elementFromPoint(e.clientX, e.clientY)
      ?.closest<HTMLElement>("[data-foto-pos]");
    if (alvo) mover(g.id, Number(alvo.dataset.fotoPos));
  }

  function onKeyDown(e: React.KeyboardEvent, id: number, pos: number) {
    if (!fotos) return;
    const para = e.key === "ArrowLeft" ? pos - 1 : e.key === "ArrowRight" ? pos + 1 : null;
    if (para === null || para < 0 || para >= fotos.length) return;
    e.preventDefault();
    const antes = fotos.map((x) => x.id);
    const nova = [...fotos];
    const [item] = nova.splice(pos, 1);
    nova.splice(para, 0, item);
    setFotos(nova);
    gravarOrdem(nova.map((x) => x.id), antes);
    requestAnimationFrame(() =>
      document.querySelector<HTMLElement>(`[data-foto-id="${id}"]`)?.focus(),
    );
  }

  useEffect(() => () => document.removeEventListener("touchmove", bloquearRolagem), [bloquearRolagem]);

  return (
    <section className="cap-galeria" aria-label="Fotos do imóvel">
      <div className="cap-galeria-topo">
        <span className="cap-galeria-titulo">Fotos {fotos ? `(${fotos.length})` : ""}</span>
        {fotos && fotos.length > 1 && (
          <span className="cap-galeria-dica">segure e arraste pra mudar a ordem · a 1ª é a capa</span>
        )}
      </div>

      {erro && <div className="cap-erro cap-galeria-erro" role="alert">{erro}</div>}

      <div className="cap-galeria-grid">
        {fotos === null && <div className="cap-foto cap-foto-carregando">carregando…</div>}
        {fotos?.map((f, pos) => (
          <div
            key={f.id}
            data-foto-id={f.id}
            data-foto-pos={pos}
            className={`cap-foto${arrastando === f.id ? " cap-foto-arrastando" : ""}`}
            onPointerDown={(e) => onPointerDown(e, f.id)}
            onPointerMove={onPointerMove}
            onPointerUp={() => encerrar(true)}
            onPointerCancel={() => encerrar(false)}
            onContextMenu={(e) => e.preventDefault()}
            onKeyDown={(e) => onKeyDown(e, f.id, pos)}
            tabIndex={0}
            aria-label={`Foto ${pos + 1}${pos === 0 ? ", capa" : ""}. Setas pra mover.`}
          >
            <img src={`/api/imoveis/fotos/${f.id}`} alt="" draggable={false} loading="lazy" />
            {pos === 0 && <span className="cap-foto-capa">Capa</span>}
            <button
              type="button"
              className="cap-foto-x"
              onClick={() => excluir(f.id)}
              aria-label={`Excluir foto ${pos + 1}`}
              title="Excluir foto"
            >
              ×
            </button>
          </div>
        ))}
        {Array.from({ length: enviando }).map((_, i) => (
          <div key={`env-${i}`} className="cap-foto cap-foto-carregando">enviando…</div>
        ))}
        <button
          type="button"
          className="cap-foto cap-foto-mais"
          onClick={() => input.current?.click()}
          aria-label="Adicionar fotos"
          title="Adicionar fotos"
        >
          <span aria-hidden="true">+</span>
        </button>
        <input
          ref={input}
          type="file"
          accept="image/*"
          multiple
          hidden
          onChange={(e) => adicionar(e.target.files)}
        />
      </div>
    </section>
  );
}
