"use client";

// Assistente — copilota conversacional da corretora, portada 1:1 do painel V1
// (dash-imobiliaria/app/page.tsx, view "assistente"). Modal na tela do Chat:
// histórico + input, respostas em markdown-lite, fluxo de ARTE (📎 foto →
// /arte/dados → confirma → /arte → PNG pra baixar) e AÇÕES propostas que só
// executam com clique de confirmação. NADA vai pro lead sozinho.
//
// Backend (reuso zero-mudança): /api/assistente, /api/assistente/acao,
// /api/assistente/arte, /api/assistente/arte/dados (proxies → copiloto :8002).
//
// Áudio (🎤): grava com MediaRecorder, manda pro /api/assistente/audio, que
// transcreve no Whisper do Mac e responde. A transcrição aparece como a
// mensagem enviada — se entendeu errado, dá pra ver na hora.

import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import s from "./chat.module.css";

// ── markdown-lite (portado do V1) ─────────────────────────────────────
// Backend manda **negrito**, *itálico*, `code`, listas com "- " e quebras.
// Sem lib nova; escapa via nós React (nunca dangerouslySetInnerHTML).
function mdInline(texto: string, keyBase: string): ReactNode[] {
  const out: ReactNode[] = [];
  const re = /\*\*([^*]+)\*\*|\*([^*]+)\*|`([^`]+)`/g;
  let last = 0;
  let m: RegExpExecArray | null;
  let i = 0;
  while ((m = re.exec(texto)) !== null) {
    if (m.index > last) out.push(texto.slice(last, m.index));
    if (m[1] !== undefined) out.push(<strong key={`${keyBase}-b${i}`}>{m[1]}</strong>);
    else if (m[2] !== undefined) out.push(<em key={`${keyBase}-i${i}`}>{m[2]}</em>);
    else if (m[3] !== undefined) out.push(<code key={`${keyBase}-c${i}`} className={s.beMdCode}>{m[3]}</code>);
    last = m.index + m[0].length;
    i++;
  }
  if (last < texto.length) out.push(texto.slice(last));
  return out;
}

function MarkdownLite({ texto }: { texto: string }) {
  if (!texto) return null;
  const linhas = texto.split("\n");
  const blocos: ReactNode[] = [];
  let lista: string[] = [];
  let k = 0;
  const flushLista = () => {
    if (lista.length === 0) return;
    const itens = lista;
    blocos.push(
      <ul key={`ul-${k}`} className={s.beMdUl}>
        {itens.map((li, j) => <li key={j}>{mdInline(li, `ul-${k}-${j}`)}</li>)}
      </ul>,
    );
    lista = [];
    k++;
  };
  linhas.forEach((raw, idx) => {
    const linha = raw.replace(/\s+$/, "");
    const mItem = linha.match(/^\s*[-*•]\s+(.*)$/);
    if (mItem) { lista.push(mItem[1]); return; }
    flushLista();
    if (linha.trim() === "") { blocos.push(<div key={`sp-${idx}`} className={s.beMdGap} />); return; }
    blocos.push(<p key={`p-${idx}`} className={s.beMdP}>{mdInline(linha, `p-${idx}`)}</p>);
  });
  flushLista();
  return <div className={s.beMdLite}>{blocos}</div>;
}

// Lead citado na resposta → vira botão "💬 nome" que abre /chat?lead=<id>
type BeLeadBotao = { lead_id: string; nome: string };

type BeMsg = {
  de: "eu" | "assistente";
  txt: string;
  img?: string;
  confirm?: { foto: string; dados: Record<string, unknown> };
  acao?: Record<string, unknown> | null;
  botoes?: BeLeadBotao[];
  // bolha de áudio: marca pra trocar o texto pela transcrição quando ela chegar
  audioId?: string;
};

// Contexto do lead aberto no chat (opcional): passa nome/telefone junto da
// pergunta pra Assistente ter contexto. Backend ignora campos extras sem quebrar.
export type LeadCtx = { nome?: string | null; telefone?: string | null } | null;

// Teto de gravação: 2 minutos. Bate no limite → para sozinho e envia o que tem.
const GRAV_MAX_S = 120;

export default function Assistente({ onClose, leadCtx }: { onClose: () => void; leadCtx?: LeadCtx }) {
  const [msgs, setMsgs] = useState<BeMsg[]>([]);
  const [input, setInput] = useState("");
  const [foto, setFoto] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [lento, setLento] = useState(false);
  const [copiado, setCopiado] = useState<number | null>(null);
  const [gravando, setGravando] = useState(false);
  const [segundos, setSegundos] = useState(0);
  const [micErro, setMicErro] = useState<string | null>(null);
  const bodyRef = useRef<HTMLDivElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const taRef = useRef<HTMLTextAreaElement>(null);
  const recRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<BlobPart[]>([]);
  // cancelar = descarta o áudio no onstop em vez de enviar
  const canceladoRef = useRef(false);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    taRef.current?.focus();
    const onEsc = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onEsc);
    return () => window.removeEventListener("keydown", onEsc);
  }, [onClose]);

  const scroll = useCallback(() => {
    setTimeout(() => bodyRef.current?.scrollTo(0, bodyRef.current.scrollHeight), 50);
  }, []);

  function anexarFoto(e: React.ChangeEvent<HTMLInputElement>) {
    const f = e.target.files?.[0];
    if (!f) return;
    const rd = new FileReader();
    rd.onload = () => setFoto(rd.result as string);
    rd.readAsDataURL(f);
    e.target.value = "";
  }

  const enviar = useCallback(async () => {
    const q = input.trim();
    const ft = foto;
    if ((!q && !ft) || loading) return;
    setInput(""); setFoto(null);
    setMsgs((m) => [...m, { de: "eu", txt: q || (ft ? "Cria a arte com essa foto" : ""), img: ft || undefined }]);
    setLoading(true); setLento(false); scroll();
    const avisoLento = setTimeout(() => setLento(true), 15000);
    try {
      if (ft) {
        // passo 1: lê os dados e pede confirmação (não gera ainda)
        const r = await fetch("/api/assistente/arte/dados", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ texto: q }),
        });
        const d = await r.json();
        const dados = d?.dados || {};
        setMsgs((m) => [...m, { de: "assistente", txt: "Confere os dados que vou botar na arte 👇 tá certo?", confirm: { foto: ft, dados } }]);
      } else {
        const historico = msgs.slice(-6).map((m) => ({ role: m.de === "eu" ? "user" : "assistant", text: m.txt }));
        const body: Record<string, unknown> = { pergunta: q, historico };
        // contexto do lead aberto (best-effort; backend ignora extras)
        if (leadCtx?.nome || leadCtx?.telefone) {
          body.lead = { nome: leadCtx.nome ?? null, telefone: leadCtx.telefone ?? null };
        }
        const r = await fetch("/api/assistente", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        const d = await r.json();
        setMsgs((m) => [...m, { de: "assistente", txt: d?.resposta || "…", acao: d?.acao, botoes: d?.leads_botoes }]);
      }
    } catch {
      setMsgs((m) => [...m, { de: "assistente", txt: "Perdi a conexão com a Assistente 😿 confere sua internet e tenta de novo?" }]);
    } finally {
      clearTimeout(avisoLento);
      setLoading(false); setLento(false); scroll();
    }
  }, [input, foto, loading, msgs, leadCtx, scroll]);

  // ── áudio ────────────────────────────────────────────────────────────
  const pararTimer = useCallback(() => {
    if (timerRef.current) { clearInterval(timerRef.current); timerRef.current = null; }
  }, []);

  const enviarAudio = useCallback(async (blob: Blob) => {
    setLoading(true);
    // marca única na bolha: acha ela depois pelo id, não por índice (o estado
    // pode ter mudado no meio da transcrição)
    const audioId = `aud-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
    setMsgs((m) => [...m, { de: "eu", txt: "🎤 áudio — transcrevendo…", audioId }]);
    scroll();
    const trocarBolha = (txt: string) =>
      setMsgs((m) => m.map((x) => (x.audioId === audioId ? { ...x, txt } : x)));
    try {
      const fd = new FormData();
      fd.append("audio", blob, "gravacao.webm");
      const r = await fetch("/api/assistente/audio", { method: "POST", body: fd });
      const d = await r.json();
      const transcricao = (d?.transcricao || "").trim();
      // a bolha do "eu" vira a transcrição (o que a Assistente entendeu)
      trocarBolha(transcricao || "🎤 áudio");
      setMsgs((m) => [...m, { de: "assistente", txt: d?.resposta || "…", acao: d?.acao, botoes: d?.leads_botoes }]);
    } catch {
      trocarBolha("🎤 áudio");
      setMsgs((m) => [...m, { de: "assistente", txt: "Não consegui te ouvir agora 😿 escreve pra mim?" }]);
    } finally {
      setLoading(false); scroll();
    }
  }, [scroll]);

  const pararGravacao = useCallback((cancelar: boolean) => {
    canceladoRef.current = cancelar;
    pararTimer();
    try { recRef.current?.stop(); } catch { /* já parado */ }
  }, [pararTimer]);

  const gravar = useCallback(async () => {
    if (gravando) { pararGravacao(false); return; }
    setMicErro(null);
    if (typeof MediaRecorder === "undefined" || !navigator.mediaDevices?.getUserMedia) {
      setMicErro("Esse navegador não grava áudio. Tenta pelo Chrome ou escreve pra mim 🙂");
      return;
    }
    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch {
      setMicErro("Preciso da permissão do microfone pra te ouvir. Libera no cadeadinho do navegador 🎤");
      return;
    }
    // webm/opus é o que o Chrome dá e o Whisper aceita; se não suportar, deixa o
    // navegador escolher o default dele.
    const mime = MediaRecorder.isTypeSupported("audio/webm;codecs=opus")
      ? "audio/webm;codecs=opus"
      : (MediaRecorder.isTypeSupported("audio/webm") ? "audio/webm" : "");
    const rec = mime ? new MediaRecorder(stream, { mimeType: mime }) : new MediaRecorder(stream);
    recRef.current = rec;
    chunksRef.current = [];
    canceladoRef.current = false;

    rec.ondataavailable = (e) => { if (e.data.size > 0) chunksRef.current.push(e.data); };
    rec.onstop = () => {
      stream.getTracks().forEach((t) => t.stop()); // solta o microfone
      setGravando(false);
      setSegundos(0);
      const partes = chunksRef.current;
      chunksRef.current = [];
      recRef.current = null;
      if (canceladoRef.current) return;
      const blob = new Blob(partes, { type: rec.mimeType || "audio/webm" });
      if (blob.size < 1024) {
        setMicErro("Áudio muito curtinho — segura enquanto fala 🙂");
        return;
      }
      void enviarAudio(blob);
    };

    rec.start();
    setGravando(true);
    setSegundos(0);
    pararTimer();
    timerRef.current = setInterval(() => {
      setSegundos((s) => {
        const n = s + 1;
        if (n >= GRAV_MAX_S) { pararGravacao(false); return GRAV_MAX_S; }
        return n;
      });
    }, 1000);
  }, [gravando, pararGravacao, pararTimer, enviarAudio]);

  // desmontou o modal no meio da gravação → solta o microfone
  useEffect(() => () => {
    if (timerRef.current) clearInterval(timerRef.current);
    canceladoRef.current = true;
    try { recRef.current?.stop(); } catch { /* nada gravando */ }
  }, []);

  const montarArte = useCallback(async (ft: string, dados: Record<string, unknown>, msgIdx: number) => {
    setMsgs((m) => m.map((x, i) => i === msgIdx ? { ...x, txt: "Montando a arte… 🎨", confirm: undefined } : x));
    setLoading(true); scroll();
    try {
      const r = await fetch("/api/assistente/arte", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ imagem_b64: ft, dados }),
      });
      const d = await r.json();
      if (d?.png_b64) setMsgs((m) => [...m, { de: "assistente", txt: "Prontinho! 🎨 Toca na imagem pra baixar e mandar pro lead.", img: d.png_b64 }]);
      else setMsgs((m) => [...m, { de: "assistente", txt: d?.erro || "Não consegui montar 😿" }]);
    } catch {
      setMsgs((m) => [...m, { de: "assistente", txt: "Não consegui montar agora 😿" }]);
    } finally { setLoading(false); scroll(); }
  }, [scroll]);

  const copiarTexto = useCallback(async (txt: string, msgIdx: number) => {
    try {
      await navigator.clipboard.writeText(txt);
      setCopiado(msgIdx);
      setTimeout(() => setCopiado((c) => (c === msgIdx ? null : c)), 1800);
    } catch {
      try {
        const ta = document.createElement("textarea");
        ta.value = txt; ta.style.position = "fixed"; ta.style.opacity = "0";
        document.body.appendChild(ta); ta.select(); document.execCommand("copy");
        document.body.removeChild(ta);
        setCopiado(msgIdx);
        setTimeout(() => setCopiado((c) => (c === msgIdx ? null : c)), 1800);
      } catch { /* sem clipboard */ }
    }
  }, []);

  const executarAcao = useCallback(async (acao: Record<string, unknown>, msgIdx: number) => {
    setMsgs((m) => m.map((x, i) => i === msgIdx ? { ...x, acao: undefined, txt: x.txt + "\n\n⏳ registrando…" } : x));
    setLoading(true); scroll();
    try {
      const r = await fetch("/api/assistente/acao", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(acao),
      });
      const d = await r.json();
      setMsgs((m) => [...m, { de: "assistente", txt: d?.msg || (d?.ok ? "Feito! ✅" : "Não consegui 😿") }]);
    } catch {
      setMsgs((m) => [...m, { de: "assistente", txt: "Não consegui registrar agora 😿" }]);
    } finally { setLoading(false); scroll(); }
  }, [scroll]);

  const perguntaRapida = (str: string) => { setInput(str); setTimeout(enviar, 10); };

  const CATS: { cat: string; qs: string[] }[] = [
    { cat: "🧑 Meus leads", qs: ["Como tá o lead da Elaine?", "Quais leads meus tão esfriando?", "Quem tá esperando minha resposta?", "Resume minha última conversa com o João"] },
    { cat: "🏢 Imóveis", qs: ["Tem apto 2 quartos até 300k?", "Me lista casas em Bairro X", "Detalhes do empreendimento Bairro X", "Empreendimento pronto pra morar até 250k"] },
    { cat: "📊 Meu dia e números", qs: ["O que eu preciso fazer hoje?", "Minhas visitas dessa semana", "Como tão meus números do mês?"] },
    { cat: "⚡ Ações rápidas", qs: ["Move o João pro Follow-up", "Rascunha uma resposta pra Elaine", "Liguei pra Pessoa7, não atendeu"] },
    { cat: "💡 Coaching", qs: ["Como abordar quem acha caro?", "Me dá um script pra 'vou pensar'", "O que eu podia ter feito melhor com o lead X?"] },
  ];

  return (
    <div className={s.buscaWebOverlay} onClick={onClose}>
      <div className={s.beModal} onClick={(e) => e.stopPropagation()}>
        <div className={s.beTop}>
          <span className={s.beAv}>🐱</span>
          <div className={s.beTopTxt}>
            <div className={s.beNome}>Assistente</div>
            <div className={s.beSub}>
              sua assistente — pergunta o que quiser sobre seus leads, seu dia, os imóveis…
              {leadCtx?.nome ? ` · lead aberto: ${leadCtx.nome}` : ""}
            </div>
          </div>
          <button className={s.buscaWebClose} onClick={onClose} aria-label="Fechar">✕</button>
        </div>

        <div className={s.beBody} ref={bodyRef}>
          {msgs.length === 0 && (
            <div className={s.beEmpty}>
              <div className={s.beEmptyIc}>🐱</div>
              <div className={s.beEmptyTxt}>Oi! Sou a Assistente, sua assistente. Começa pelo seu briefing — ou me pergunta qualquer coisa:</div>
              <button className={s.beBriefing} onClick={() => perguntaRapida("Me dá o meu briefing da manhã")}>☀️ Meu briefing da manhã</button>
              <div className={s.beCats}>
                {CATS.map((c) => (
                  <div key={c.cat} className={s.beCat}>
                    <div className={s.beCatTit}>{c.cat}</div>
                    <div className={s.beSugs}>
                      {c.qs.map((q) => <button key={q} className={s.beSug} onClick={() => perguntaRapida(q)}>{q}</button>)}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {msgs.map((m, i) => (
            <div key={i} className={`${s.beMsg} ${m.de === "eu" ? s.beMsgEu : s.beMsgBet}`}>
              {m.de === "assistente" && <span className={s.beMsgAv}>🐱</span>}
              <div className={s.beBolha}>
                {m.txt && (m.de === "assistente" ? <MarkdownLite texto={m.txt} /> : <div className={s.beTxtUser}>{m.txt}</div>)}
                {m.de === "assistente" && m.txt && !m.confirm && !m.acao && (
                  <button className={s.beCopy} title="Copiar o texto" onClick={() => copiarTexto(m.txt, i)}>
                    {copiado === i ? "✅ copiado" : "📋 copiar"}
                  </button>
                )}
                {m.confirm && (() => {
                  const dd = (m.confirm.dados || {}) as Record<string, unknown>;
                  const linhas: [string, unknown][] = [
                    ["Título", dd.nome], ["Tipo", dd.tipo], ["Bairro", dd.bairro],
                    ["Preço", dd.preco_fmt], ["Quartos", dd.quartos],
                    ["Metros", dd.metros ? `${dd.metros}m²` : null], ["Entrega", dd.entrega_fmt],
                  ];
                  return (
                    <div className={s.beConfirm}>
                      <img src={m.confirm.foto} alt="" className={s.beFotoThumb} style={{ marginBottom: 8 }} />
                      {linhas.filter(([, v]) => v).map(([k, v]) => (
                        <div key={k} className={s.beConfirmL}><span>{k}</span><b>{String(v)}</b></div>
                      ))}
                      <button className={s.beConfirmBtn} onClick={() => montarArte(m.confirm!.foto, dd, i)}>✅ Tá certo, monta a arte</button>
                      <div className={s.beConfirmNota}>Algum dado errado? É só me escrever de novo (com a foto) que eu corrijo.</div>
                    </div>
                  );
                })()}
                {m.acao && (() => {
                  const a = m.acao as Record<string, unknown>;
                  const tipo = a.tipo as string | undefined;
                  const st = a.status as string | undefined;
                  return (
                    <div className={s.beConfirm}>
                      {tipo === "agendar" ? (
                        <>
                          <div className={s.beAcaoNota}>📅 {String(a.resumo ?? "")}</div>
                          <button className={s.beConfirmBtn} onClick={() => executarAcao(a, i)}>✅ Confirmar e agendar</button>
                        </>
                      ) : tipo === "ligacoes" ? (
                        <>
                          <div className={s.beAcaoNota}>{String(a.resumo ?? `📞 ${a.ligacoes ?? 0} ligações`)}</div>
                          <button className={s.beConfirmBtn} onClick={() => executarAcao(a, i)}>📞 Registrar</button>
                        </>
                      ) : tipo === "mover" ? (
                        <>
                          <div className={s.beAcaoNota}>🎯 {String(a.resumo ?? "")}</div>
                          <button className={s.beConfirmBtn} onClick={() => executarAcao(a, i)}>
                            {a.destrutivo ? "⚠️ Sim, confirmo a mudança" : "✅ Confirmar e mover"}
                          </button>
                        </>
                      ) : tipo === "ligacao" ? (
                        <>
                          <div className={s.beAcaoNota}>{String(a.resumo ?? "📞 ligação")}</div>
                          <button className={s.beConfirmBtn} onClick={() => executarAcao(a, i)}>📞 Registrar ligação</button>
                        </>
                      ) : tipo === "visita_status" ? (
                        <>
                          <div className={s.beAcaoNota}>{String(a.resumo ?? `${a.lead_nome ?? "visita"} — ${st ?? ""}`)}</div>
                          <button className={s.beConfirmBtn} onClick={() => executarAcao(a, i)}>
                            {st === "no_show" || st === "no-show" || st === "faltou" ? "❌ Confirmar no-show" : "✅ Confirmar compareceu"}
                          </button>
                        </>
                      ) : (
                        <>
                          <div className={s.beAcaoNota}>“{String(a.nota ?? "")}”</div>
                          <button className={s.beConfirmBtn} onClick={() => executarAcao(a, i)}>✅ Pode registrar no {String(a.lead_nome ?? "lead")}</button>
                        </>
                      )}
                      <div className={s.beConfirmNota}>Só faço depois que você confirmar.</div>
                    </div>
                  );
                })()}
                {m.de === "assistente" && m.botoes && m.botoes.length > 0 && (
                  <div className={s.beLeadBtns}>
                    {m.botoes.map((b) => (
                      <a
                        key={b.lead_id}
                        className={s.beLeadBtn}
                        href={"/chat?lead=" + encodeURIComponent(b.lead_id) + "&nome=" + encodeURIComponent(b.nome)}
                      >
                        💬 {b.nome}
                      </a>
                    ))}
                  </div>
                )}
                {m.img && (m.de === "assistente" ? (
                  <a href={m.img} download="arte-acme.png" className={s.beArte}>
                    <img src={m.img} alt="arte" /><span className={s.beArteDl}>⬇ baixar</span>
                  </a>
                ) : (
                  <img src={m.img} alt="foto" className={s.beFotoThumb} />
                ))}
              </div>
            </div>
          ))}
          {loading && <div className={`${s.beMsg} ${s.beMsgBet}`}><span className={s.beMsgAv}>🐱</span><div className={`${s.beBolha} ${s.beTyping}`}>{lento ? "pensando… tá demorando mais que o normal, segura que já vem 🐱" : "pensando…"}</div></div>}
        </div>

        <div className={s.beInputWrap}>
          {gravando && (
            <div className={s.beGrav}>
              <i className={s.beGravDot} />
              <b className={s.beGravTempo}>
                {String(Math.floor(segundos / 60)).padStart(2, "0")}:{String(segundos % 60).padStart(2, "0")}
              </b>
              <span>Gravando… fala que eu te escuto (máx 2 min)</span>
              <button className={s.beGravBtn} onClick={() => pararGravacao(true)}>✕ cancelar</button>
              <button className={`${s.beGravBtn} ${s.beGravOk}`} onClick={() => pararGravacao(false)}>➤ enviar</button>
            </div>
          )}
          {micErro && <div className={s.beGravErro}>{micErro}</div>}
          {foto && (
            <div className={s.beAnexo}>
              <img src={foto} alt="" className={s.beAnexoThumb} />
              <span>Foto anexada — escreve o empreendimento e manda que eu monto a arte 🎨</span>
              <button onClick={() => setFoto(null)}>✕</button>
            </div>
          )}
          <div className={s.beInput}>
            <input ref={fileRef} type="file" accept="image/*" style={{ display: "none" }} onChange={anexarFoto} />
            <button className={s.beAttach} onClick={() => fileRef.current?.click()} title="Anexar foto pra criar arte do empreendimento">📎</button>
            <button
              className={`${s.beMic} ${gravando ? s.beMicOn : ""}`}
              onClick={gravar}
              disabled={loading && !gravando}
              title={gravando ? "Parar e enviar o áudio" : "Falar com a Assistente"}
              aria-label={gravando ? "Parar e enviar o áudio" : "Gravar áudio"}
            >
              {gravando ? "⏹" : "🎤"}
            </button>
            <textarea
              ref={taRef}
              value={input}
              onChange={(e) => { setInput(e.target.value); e.target.style.height = "auto"; e.target.style.height = e.target.scrollHeight + "px"; }}
              onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); enviar(); } }}
              placeholder={foto ? "Qual empreendimento? (ex: Smart Reale)" : "Pergunta pra Assistente…"}
              rows={1}
              style={{ maxHeight: "9em", overflowY: "auto" }}
            />
            <button className={s.beSend} disabled={(!input.trim() && !foto) || loading} onClick={enviar}>➤</button>
          </div>
        </div>
      </div>
    </div>
  );
}
