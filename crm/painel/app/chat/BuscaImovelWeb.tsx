"use client";

// Assistente busca imóvel de TERCEIROS na web (fora da carteira Acme).
//
// A busca é ASSÍNCRONA: a corretora dispara, o servidor abre um job e devolve
// o id na hora. A tela LIBERA — dá pra fechar o modal, atender um lead e voltar
// depois. Ao reabrir, o último job é recarregado do servidor (o estado vive no
// banco, não no navegador) e, se ainda estiver rodando, o polling recomeça.
//
// Motor: Playwright/Chromium no Mac mini (GRÁTIS, lento por natureza).
// SUGESTÃO pra corretora — nunca vai pro lead.

import { useCallback, useEffect, useRef, useState } from "react";
import s from "./chat.module.css";

type Opcao = {
  titulo: string | null;
  preco: string | null;
  bairro: string | null;
  cidade: string | null;
  m2: string | null;
  quartos: string | null;
  portal: string | null;
  url: string;
  resumo: string | null;
};

type Job = {
  job_id: number;
  status: "rodando" | "pronto" | "erro";
  pedido: string;
  opcoes: Opcao[];
  erro: string | null;
  criado_em: string | null;
  concluido_em: string | null;
};

const POLL_MS = 3000;

function decorridoDesde(iso: string | null): string {
  if (!iso) return "";
  const seg = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
  if (seg < 60) return `${seg}s`;
  return `${Math.floor(seg / 60)}min${String(seg % 60).padStart(2, "0")}`;
}

export default function BuscaImovelWeb({ onClose }: { onClose: () => void }) {
  const [texto, setTexto] = useState("");
  const [job, setJob] = useState<Job | null>(null);
  const [erroLocal, setErroLocal] = useState<string | null>(null);
  const [abrindo, setAbrindo] = useState(false);
  const [tick, setTick] = useState(0); // só pra o "há Xs" andar na tela
  const inputRef = useRef<HTMLInputElement>(null);
  const jobIdRef = useRef<number | null>(null);

  const rodando = job?.status === "rodando";

  // Retoma o último job ao abrir o modal (a pessoa saiu da tela e voltou).
  useEffect(() => {
    let vivo = true;
    (async () => {
      try {
        const r = await fetch("/api/assistente/busca-imovel", { cache: "no-store" });
        const d = await r.json();
        const ultimo: Job | undefined = d?.jobs?.[0];
        if (vivo && ultimo) {
          setJob(ultimo);
          jobIdRef.current = ultimo.job_id;
          setTexto(ultimo.pedido || "");
        }
      } catch {
        /* sem job anterior é o caso normal — silencioso */
      }
    })();
    inputRef.current?.focus();
    const onEsc = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onEsc);
    return () => {
      vivo = false;
      window.removeEventListener("keydown", onEsc);
    };
  }, [onClose]);

  // Polling enquanto o job estiver rodando.
  useEffect(() => {
    if (!rodando) return;
    let vivo = true;
    const id = setInterval(async () => {
      setTick((t) => t + 1);
      const jid = jobIdRef.current;
      if (!jid) return;
      try {
        const r = await fetch(`/api/assistente/busca-imovel?job_id=${jid}`, { cache: "no-store" });
        if (!r.ok) return; // 502 momentâneo: tenta de novo no próximo tick
        const d: Job = await r.json();
        if (vivo && d?.job_id === jid) setJob(d);
      } catch {
        /* rede instável: o próximo tick tenta de novo */
      }
    }, POLL_MS);
    return () => {
      vivo = false;
      clearInterval(id);
    };
  }, [rodando]);

  const buscar = useCallback(async () => {
    const q = texto.trim();
    if (q.length < 4) {
      setErroLocal("me diz o que o lead procura — ex: 'apto 3 quartos no Bairro A até 500 mil'");
      return;
    }
    setErroLocal(null);
    setAbrindo(true);
    try {
      const r = await fetch("/api/assistente/busca-imovel", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ texto: q }),
      });
      const d = await r.json();
      if (!d?.job_id) {
        setErroLocal(d?.erro || "não consegui abrir a busca — tenta de novo");
        return;
      }
      jobIdRef.current = d.job_id;
      setJob({
        job_id: d.job_id,
        status: "rodando",
        pedido: q,
        opcoes: [],
        erro: null,
        criado_em: new Date().toISOString(),
        concluido_em: null,
      });
    } catch {
      setErroLocal("não consegui abrir a busca — tenta de novo");
    } finally {
      setAbrindo(false);
    }
  }, [texto]);

  const opcoes = job?.opcoes ?? [];
  const erro = erroLocal || (job?.status === "erro" ? job.erro : null);
  const vazio = job?.status === "pronto" && opcoes.length === 0;

  return (
    <div className={s.buscaWebOverlay} onClick={onClose}>
      <div className={s.buscaWebModal} onClick={(e) => e.stopPropagation()}>
        <div className={s.buscaWebHead}>
          <span className={s.buscaWebTitulo}>🔎 Buscar imóvel na web</span>
          <span className={s.buscaWebNota}>
            roda em segundo plano · pode fechar e voltar depois — não vai pro lead
          </span>
          <button className={s.buscaWebClose} onClick={onClose} aria-label="Fechar">
            ✕
          </button>
        </div>

        <div className={s.buscaWebBar}>
          <input
            ref={inputRef}
            value={texto}
            onChange={(e) => setTexto(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !abrindo && !rodando) buscar();
            }}
            placeholder="ex: apartamento 3 quartos Parque Bairro A Cidade até 500 mil"
            disabled={abrindo}
          />
          <button className={s.buscaWebBtn} onClick={buscar} disabled={abrindo || rodando}>
            {rodando ? "buscando…" : abrindo ? "abrindo…" : "Buscar"}
          </button>
        </div>

        <div className={s.buscaWebBody}>
          {rodando && (
            <div className={s.buscaWebLoading} key={`rodando-${tick}`}>
              Buscando em segundo plano há {decorridoDesde(job?.criado_em ?? null)} — abrindo
              os portais no Mac (VivaReal + Chaves na Mão).
              <br />
              Pode fechar esta janela e continuar trabalhando: quando voltar, o resultado
              estará aqui.
            </div>
          )}
          {!rodando && erro && <div className={s.buscaWebErro}>{erro}</div>}
          {!rodando && !erro && vazio && (
            <div className={s.buscaWebVazio}>
              Não achei nada real que batesse. Tenta afrouxar (menos filtros, outro bairro ou
              preço maior).
            </div>
          )}
          {job?.status === "pronto" && opcoes.length > 0 && (
            <div className={s.buscaWebVazio} style={{ paddingBottom: 8 }}>
              ✅ Busca concluída — {opcoes.length} opções pra “{job.pedido}”.
            </div>
          )}
          {opcoes.length > 0 && (
            <div className={s.buscaWebGrid}>
              {opcoes.map((o, i) => (
                <a
                  key={o.url + i}
                  className={s.imovelCard}
                  href={o.url}
                  target="_blank"
                  rel="noopener noreferrer"
                  title="Abrir o anúncio em nova aba"
                >
                  <div className={s.imovelTop}>
                    <span className={s.imovelPreco}>{o.preco ?? "Preço sob consulta"}</span>
                    <span className={s.imovelPortal}>{o.portal}</span>
                  </div>
                  <div className={s.imovelTitulo}>{o.titulo ?? "Imóvel"}</div>
                  <div className={s.imovelMeta}>
                    {[
                      o.quartos ? `${o.quartos} quartos` : null,
                      o.m2 ? `${o.m2} m²` : null,
                      o.bairro ?? null,
                      o.cidade ?? null,
                    ]
                      .filter(Boolean)
                      .join(" · ")}
                  </div>
                  <span className={s.imovelAbrir}>abrir anúncio ↗</span>
                </a>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
