"use client";

import { useCallback, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { useEstadoPersistido } from "@/app/lib/estado-persistido";
import { useTravarRolagemDaPagina } from "@/app/lib/tela-cheia";
import s from "./chat.module.css";
import BuscaImovelWeb from "./BuscaImovelWeb";
import Empreendimentos from "./Empreendimentos";
import Assistente from "./Assistente";
import { linkTel } from "@/lib/telefone";

// 💬 Chat V2 — leva 1: migração do núcleo do painel antigo (dash-imobiliaria,
// view "chat") — leitura + coaching (read-only).
// Leva 2 (2026-07-10, requisitos da Dona):
//  · TAG DE LINHA — cada mensagem marcada por qual TELEFONE/LINHA rolou
//    (campo `linha` do copiloto, best-effort via mensagens_evolution.instance).
//    Motivo: lead transferido de corretora leva o histórico junto; quem recebe
//    sabe por onde foi cada trecho e dispara pela PRÓPRIA linha. UI: chip da
//    linha atual no topo + divisor discreto quando a linha muda na thread.
//  · CRIAR TAREFA — modal (texto + quando) → tarefas_corretora (0040) →
//    tarefa exposta via /api/tarefas.
//  · ANOTAÇÕES INTERNAS — notas privadas da equipe ("liguei, não atendeu")
//    como cards amarelos intercalados na thread + caixa no rodapé. Reusa
//    observacao_corretora (0033) SEM IA/CRM — NUNCA vai pro lead.
// Envio de mensagem PRO LEAD segue fora (painel antigo/imob ou WhatsApp).
// Leva 3 (2026-07-26): TRANSFERIR LEAD entre corretoras — botão ↔️ no topo da
// conversa → escolhe destino + motivo (opcional) → POST /api/crm/transferir
// (muda ownership + escopo/linha de disparo; histórico fica com as tags de
// linha). Admin transfere qualquer lead; corretora só lead PRÓPRIO (backend
// devolve 403 pros demais). ZERO WhatsApp — aviso é in-app: card chega pra
// corretora destino com selo ↔️ "transferido" por 7 dias.

type Card = {
  kommoLeadId: string;
  nome: string;
  telefone: string;
  corretora: string;
  tags: string[];
  stage: string;
  ultima_at: string | null;
  dias: number | null;
  acende: boolean;
  transferido?: boolean; // selo: trocou de corretora nos últimos 7 dias
  rmkt?: string | null; // tag do card ("RMKT"): veio do remarketing. Vem do GET /api/conversa
  // Resumo por IA do que a pessoa escreveu na linha da Corretor (05/10). Vem do GET /api/conversa.
  resumoIa?: {
    status: "ok" | "pendente" | "sem_resumo";
    resumo?: string | null;
    fatos?: { fato: string | null; trecho: string | null }[];
  } | null;
  preview?: string | null; // prévia da última msg (todas as filas — pacote A, 16/10)
  preview_de?: "lead" | "corretora" | null; // quem mandou a última msg
  nao_lidas?: number; // msgs do lead não lidas por QUEM está logado (persiste no banco)
};
type Coluna = { stage: string; label: string; total: number; cards: Card[] };
// item da fila "⏳ Esperando resposta" (/api/conversa/esperando — última msg
// é DA corretora, lead mudo). O front mapeia pra Card pra reusar abrir().
type EsperaItem = {
  kommoLeadId: string;
  nome: string;
  telefone: string;
  corretora: string;
  etapa: string | null;
  preview: string;
  preview_de?: "lead" | "corretora";
  ultima_at: string | null;
};
type Viewer = { user: string; isAdmin: boolean; corretora: string | null };
type Msg = {
  from: "lead" | "corretora";
  text: string;
  at: string;
  type: string;
  id?: string;
  transcricao?: string;
  duracao?: number; // segundos do áudio, quando a linha manda (Evolution)
  delivery_status?: "sent" | "delivered" | "read" | "failed";
  delivery_error?: string | null; // motivo quando failed (se o backend mandar)
  linha?: string; // por qual linha/telefone o trecho rolou (tag best-effort)
  filename?: string; // documento/vídeo: nome original ("contrato-assinado.pdf")
  mime?: string; // mimetype do anexo — escolhe o ícone do documento
};
type Coaching = { categoria?: string; trecho?: string; observacao?: string; sugestao?: string } | null;
// Tarefa de LIGAÇÃO da cadência devida pra ESTE lead (sistema novo — D1/D5/D9).
// Origem certa do "Ligar — dia N do follow-up"; a Pessoa12 vê aqui e marca "✓ liguei".
type TarefaLigacao = {
  cadencia_id: number;
  toque: number;
  texto: string;
  data: string;
  atrasada: boolean;
};
// Tarefa MANUAL da corretora pra ESTE lead (tarefas_corretora, aberta). Criada
// na aba /tarefas OU no modal do chat; editável/concluível aqui dentro do chat.
type TarefaLead = {
  id: number;
  texto: string;
  data: string;
  hora?: string | null;
  atrasada: boolean;
  anotacoes?: string | null;
  mensagem_planejada?: string | null;
  tem_msg_planejada?: boolean;
};
type Raiox = {
  temperatura?: string;
  status?: string;
  resumo?: string;
  gap?: string;
  abordagem?: string;
  tarefas_atrasadas?: string[];
  etapa_desatualizada?: boolean;
  analyzed_at?: string;
} | null;
// arquivo escolhido no 📎 aguardando o "Enviar". `url` é um object URL só pra
// miniatura — precisa de revoke ao tirar da fila, senão o blob fica preso.
type Anexo = { id: number; file: File; url: string; tipo: "image" | "document" };

type Nota = {
  id: number;
  autor: string | null;
  texto: string;
  at: string | null;
  corretora: string | null;
  via_ia: boolean; // true = observação que passou pela IA (painel atendimento)
};

const CORRETORAS = ["Pessoa6", "Pessoa1", "Pessoa3", "Pessoa7", "Corretor"];
const ABREV: Record<string, string> = { Pessoa6: "Pessoa10", Pessoa1: "Pessoa11", Pessoa3: "Pessoa12", "Pessoa7": "Pessoa7", Corretor: "Corretor" };
const abrev = (c: string) => ABREV[c] ?? (c || "").split(" ")[0];
// ids do copiloto (corretoras.id) pro transferir — mesma ordem de CORRETORAS
const CORRETORA_ID: Record<string, number> = { Pessoa6: 1, Pessoa1: 2, Pessoa3: 3, "Pessoa7": 7, Corretor: 8 };
// nome (do card OU do DB, com/sem acento) → id da corretora; null se não deu
function corretoraIdDeNome(nome: string | null | undefined): number | null {
  const n = (nome ?? "").toLowerCase();
  if (n.startsWith("corretor")) return 1;
  if (n.startsWith("corretor")) return 2;
  if (n.startsWith("gio") || n === "corretor") return 3;
  // "pessoa_d"/"pessoa_d" INTEIRO, nunca só "mar": a conta da Pessoa12 no Kommo se chama
  // "Pessoa Pessoa9" e cairia aqui como se fosse a Pessoa7.
  if (n.startsWith("pessoa_d") || n.startsWith("pessoa_d")) return 7;
  if (n.startsWith("corretor")) return 8;
  return null;
}
const MEDIA_LABEL: Record<string, string> = {
  audio: "🎙️ áudio",
  image: "🖼️ imagem",
  video: "🎬 vídeo",
  document: "📎 documento",
  sticker: "💟 figurinha",
  other: "📩 mídia",
};

// erros "conhecidos" do /api/conversa quando o lead ainda não tem thread no
// WhatsApp (deep-link do CRM) — vira estado vazio amigável, não erro cru
const SEM_CONVERSA = new Set(["lead_nao_encontrado", "lead_sem_telefone", "lead_sem_corretora"]);

// Copy humana pros erros conhecidos do /api/conversa. Sem entrada aqui → texto
// genérico "Não abriu (<erro>)". Enquanto houver threadErro o composer some
// inteiro (fail-closed): não oferecemos texto livre em conversa que a corretora
// não pode nem ler.
function copiaThreadErro(erro: string): string {
  if (SEM_CONVERSA.has(erro)) {
    return "Sem conversa com esse lead ainda — quando rolar mensagem no WhatsApp, a thread aparece aqui.";
  }
  if (erro === "lead_de_outra_corretora") {
    return "Esse lead é de outra corretora — você não vê a conversa nem responde por aqui.";
  }
  return `Não abriu (${erro}).`;
}

// Saudação TRAVADA da mensagem planejada — espelha o greeting do template UTILITY
// da Pessoa12 no backend (cloud_corretora_a): COM primeiro nome real → "Olá {nome}, tudo
// bem?"; SEM nome → "Oi, tudo bem?". A corretora escreve só o corpo depois; assim
// nunca dobra a saudação (o backend ainda dedupa como rede de segurança).
const _NAO_NOME = new Set(["", "0", "-", "lead", "cliente", "contato", "null", "none", "nan"]);
function saudacaoTravada(leadNome: string | null | undefined): string {
  const tok = (leadNome ?? "").trim().split(/\s+/)[0] ?? "";
  const valido = tok !== "" && !_NAO_NOME.has(tok.toLowerCase()) && /[a-zA-ZÀ-ÿ]/.test(tok);
  return valido ? `Olá ${tok}, tudo bem?` : "Oi, tudo bem?";
}

function fmtHora(iso: string): string {
  if (!iso) return "";
  return new Date(iso).toLocaleString("pt-BR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
}

// "há quanto tempo" humano a partir de um ISO (pro aviso da janela fechada):
// < 48h → "30h"; senão → "2 dias". Fallback "um tempo" se não parsear.
function haQuanto(iso: string | null): string {
  if (!iso) return "um tempo";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "um tempo";
  const h = (Date.now() - t) / 3_600_000;
  if (h < 1) return `${Math.max(1, Math.round(h * 60))}min`;
  if (h < 48) return `${Math.round(h)}h`;
  return `${Math.round(h / 24)} dias`;
}

// wa.me com só dígitos (o app/Web da corretora não tem trava de 24h).
function waLink(telefone: string | null | undefined): string {
  const num = (telefone ?? "").replace(/\D/g, "");
  return `https://wa.me/${num}`;
}

const primeiroNome = (nome: string | null | undefined): string =>
  (nome ?? "").trim().split(/\s+/)[0] || "o lead";

// grupo de WhatsApp não é conversa 1:1 — fica fora da lista (herdado do antigo)
function ehGrupo(tel: string | null | undefined): boolean {
  if (!tel) return false;
  const t = String(tel);
  if (t.includes("@g.us")) return true;
  return /\d{6,}-\d{6,}/.test(t);
}

// re-render só quando a thread mudou de verdade (herdado do antigo — evita piscar)
function msgsMudaram(atuais: Msg[], novas: Msg[]): boolean {
  if (atuais.length !== novas.length) return true;
  if (novas.length === 0) return false;
  const a = atuais[atuais.length - 1];
  const b = novas[novas.length - 1];
  if ((a.id ?? "") !== (b.id ?? "") || a.at !== b.at || a.from !== b.from) return true;
  for (let i = 0; i < atuais.length; i++) {
    if ((atuais[i].delivery_status ?? "") !== (novas[i].delivery_status ?? "")) return true;
  }
  return false;
}

// Ticks de entrega estilo WhatsApp (pedido Dona 11/07): ✓ enviada · ✓✓ entregue
// · ✓✓ azul lida · ⚠ vermelho falhou (motivo no tooltip quando o backend anexa
// delivery_error). Msg sem status — Evolution/Baileys não reporta ACK aqui, ou
// msg antiga — fica sem tick nenhum: degrade limpo, sem placeholder.
type Falha =
  | { tipo: "texto"; leadId: string; texto: string; motivo: string }
  | { tipo: "template"; leadId: string; texto: string; motivo: string }
  | {
      tipo: "midia"; leadId: string; blob: Blob;
      midia: "image" | "document" | "audio"; filename: string; legenda: string; motivo: string;
    };

// Rótulo humano do que ficou pendente, pra barra "não enviou".
function rotuloFalha(f: Falha): string {
  if (f.tipo === "template") return "o reengajamento";
  if (f.tipo === "midia") {
    return f.midia === "audio" ? "o áudio" : f.midia === "image" ? "a foto" : "o arquivo";
  }
  return "a mensagem";
}

function DeliveryBadge({ status, error }: { status?: string; error?: string | null }) {
  if (!status) return null;
  if (status === "failed") {
    return (
      <span className={s.delivFailed} title={error ? `não entregue — ${error}` : "não entregue"}>
        ⚠
      </span>
    );
  }
  if (status === "read") return <span className={s.delivRead} title="lida">✓✓</span>;
  if (status === "delivered") return <span className={s.deliv} title="entregue">✓✓</span>;
  if (status === "sent") return <span className={s.deliv} title="enviada">✓</span>;
  return null;
}

const _fotoCache: Record<string, string | null> = {};
// Foto de perfil de QUALQUER corretora (03/10/2026). Nasceu travada em
// `enabled={corretora === "Pessoa3"}`, mas o backend abriu pra todas em
// 07/08 (`/painel/conversa/foto` consulta pelo telefone, não pela linha, e
// já devolve 403 em lead de outra corretora). O gate do front era o motivo
// de Corretor/Pessoa7/Pessoa10/Pessoa11 ficarem com avatar cinza — a request nem saía.
//
// Só o avatar da conversa ABERTA busca (08/10/2026). Antes cada card da lista
// buscava a sua ao montar: a tela abria ~68 requests juntas, todas consultavam
// o WhatsApp pela linha da Dona (a leitora, linha crítica dos disparos) e o
// teto de 30/h do backend era furado — 68 consultas em 9 segundos. O card da
// lista (`buscar={false}`) mostra a foto que a sessão já tem e, sem ela, a
// inicial. Resposta sem foto por teto/linha fora do ar não entra no cache: a
// próxima abertura da conversa tenta de novo.
function Avatar({ leadId, nome, buscar = true }: { leadId: string; nome: string; buscar?: boolean }) {
  const ini = (nome || "?").trim().charAt(0).toUpperCase();
  const [url, setUrl] = useState<string | null>(_fotoCache[leadId] ?? null);
  const [quebrou, setQuebrou] = useState(false);
  useEffect(() => {
    setQuebrou(false);
    if (!buscar) return;
    if (leadId in _fotoCache) { setUrl(_fotoCache[leadId]); return; }
    let alive = true;
    fetch(`/api/conversa/foto?lead_id=${encodeURIComponent(leadId)}`)
      .then((r) => r.json())
      .then((d) => {
        const u = d?.url ?? null;
        if (u || (!d?.motivo && !d?.error) || d.motivo === "sem_telefone") _fotoCache[leadId] = u;
        if (alive) setUrl(u);
      })
      .catch(() => { if (alive) setUrl(null); });
    return () => { alive = false; };
  }, [leadId, buscar]);
  const foto = buscar ? url : (_fotoCache[leadId] ?? null);
  if (foto && !quebrou) {
    return <img src={foto} alt={ini} onError={() => { _fotoCache[leadId] = null; setUrl(null); setQuebrou(true); }} />;
  }
  return <>{ini}</>;
}

function DiasBadge({ dias }: { dias: number | null }) {
  if (dias === null) return <span className={s.diasOk} title="Sem mensagem registrada">—</span>;
  const label = dias === 0 ? "hoje" : `${dias}d`;
  const cls = dias > 30 ? s.diasCrit : dias > 7 ? s.diasWarn : s.diasOk;
  return <span className={cls} title={`Última mensagem há ${dias} dia(s)`}>{label}</span>;
}

// Badge da fila "🟢 Esperando resposta" (B4) — há quanto tempo o LEAD aguarda
// retorno, derivado de ultima_at (a última msg é dele — acende=true). Régua de
// dívida: >48h crítico (vermelho), >4h atenção (laranja), fresquinho neutro.
function EsperaBadge({ ultimaAt, quem = "lead" }: { ultimaAt: string | null; quem?: "lead" | "corretora" }) {
  const h = ultimaAt ? (Date.now() - Date.parse(ultimaAt)) / 3_600_000 : null;
  const cls = h == null ? s.diasOk : h > 48 ? s.diasCrit : h > 4 ? s.diasWarn : s.diasOk;
  const title = quem === "lead"
    ? "Tempo desde a última mensagem do lead sem resposta da corretora"
    : "Tempo desde a última mensagem da corretora sem resposta do lead";
  return (
    <span className={cls} title={title}>
      esperando há {haQuanto(ultimaAt)}
    </span>
  );
}

// Player de áudio no jeito do WhatsApp (pacote A, 16/10/2026): ▶ grande,
// barra pra pular, DURAÇÃO antes de tocar e velocidade 1x / 1,5x / 2x.
// Antes era o <audio controls> do navegador, que no iPhone mostrava
// "0:00 / 0:00" até tocar (o Safari não lê o arquivo antes do play).
// Duração, nesta ordem: a que veio com a mensagem (linha Evolution) → a
// medida pelo servidor quando o balão aparece na tela → a do próprio arquivo.
const VELOCIDADES = [1, 1.5, 2] as const;
const VEL_KEY = "chat_audio_vel_v1";

function fmtSeg(seg: number | null): string {
  if (seg == null || !Number.isFinite(seg) || seg < 0) return "–:––";
  const t = Math.round(seg);
  return `${Math.floor(t / 60)}:${String(t % 60).padStart(2, "0")}`;
}
const rotuloVel = (v: number) => `${String(v).replace(".", ",")}x`;

function AudioMsg({
  leadId, msgId, transcricao, duracao,
}: { leadId: string; msgId: string; transcricao?: string; duracao?: number }) {
  const src = `/api/conversa/midia?lead_id=${encodeURIComponent(leadId)}&msg_id=${encodeURIComponent(msgId)}`;
  const audioRef = useRef<HTMLAudioElement>(null);
  const boxRef = useRef<HTMLDivElement>(null);
  const [erro, setErro] = useState(false);
  const [tocando, setTocando] = useState(false);
  const [carregando, setCarregando] = useState(false);
  const [dur, setDur] = useState<number | null>(duracao && duracao > 0 ? duracao : null);
  const [pos, setPos] = useState(0);
  const [vel, setVel] = useState<number>(1);

  // velocidade é preferência da pessoa, vale pra todos os áudios (igual o app)
  useEffect(() => {
    const v = Number(window.localStorage.getItem(VEL_KEY));
    if ((VELOCIDADES as readonly number[]).includes(v)) setVel(v);
  }, []);
  useEffect(() => {
    if (audioRef.current) audioRef.current.playbackRate = vel;
  }, [vel]);

  // sem duração: pede ao servidor quando o balão chega perto da tela
  useEffect(() => {
    if (dur != null || erro) return;
    const el = boxRef.current;
    if (!el) return;
    let vivo = true;
    const pedir = () => {
      fetch(`/api/conversa/midia/duracao?lead_id=${encodeURIComponent(leadId)}&msg_id=${encodeURIComponent(msgId)}`)
        .then((r) => (r.ok ? r.json() : null))
        .then((d) => { if (vivo && d && typeof d.duracao === "number" && d.duracao > 0) setDur(d.duracao); })
        .catch(() => { /* fica "–:––" até tocar */ });
    };
    if (typeof IntersectionObserver === "undefined") { pedir(); return () => { vivo = false; }; }
    const io = new IntersectionObserver((es) => {
      if (es.some((e) => e.isIntersecting)) { io.disconnect(); pedir(); }
    }, { rootMargin: "300px" });
    io.observe(el);
    return () => { vivo = false; io.disconnect(); };
  }, [leadId, msgId, dur, erro]);

  const lerDuracao = (a: HTMLAudioElement) => {
    if (Number.isFinite(a.duration) && a.duration > 0) setDur(a.duration);
  };

  const alternar = () => {
    const a = audioRef.current;
    if (!a) return;
    if (!a.paused) { a.pause(); return; }
    // um áudio por vez, como no WhatsApp
    document.querySelectorAll("audio").forEach((o) => { if (o !== a) o.pause(); });
    a.playbackRate = vel;
    setCarregando(true);
    a.play().catch(() => setCarregando(false));
  };

  const trocarVel = () => {
    const i = (VELOCIDADES as readonly number[]).indexOf(vel);
    const nova = VELOCIDADES[(i + 1) % VELOCIDADES.length];
    setVel(nova);
    try { window.localStorage.setItem(VEL_KEY, String(nova)); } catch { /* modo privado */ }
  };

  if (erro) {
    return (
      <div className={s.audioBox}>
        <span className={s.bMedia}>🎙️ áudio (mídia expirou)</span>
        {transcricao && <div className={s.transcr}>🎤 {transcricao}</div>}
      </div>
    );
  }

  const max = dur ?? 0;
  const mostra = tocando || pos > 0 ? pos : dur;
  return (
    <div className={s.audioBox} ref={boxRef}>
      <div className={s.player} data-testid="player-audio">
        <button
          type="button"
          className={s.playerPlay}
          onClick={alternar}
          aria-label={tocando ? "Pausar áudio" : "Tocar áudio"}
        >
          {carregando && !tocando ? "…" : tocando ? "❚❚" : "▶"}
        </button>
        <input
          type="range"
          className={s.playerBarra}
          min={0}
          max={max || 1}
          step={0.1}
          value={Math.min(pos, max || 1)}
          disabled={!max}
          aria-label="Posição do áudio"
          onChange={(e) => {
            const a = audioRef.current;
            const v = Number(e.target.value);
            setPos(v);
            if (a) a.currentTime = v;
          }}
        />
        <span className={s.playerTempo} data-testid="player-tempo">{fmtSeg(mostra)}</span>
        <button
          type="button"
          className={vel === 1 ? s.playerVel : s.playerVelOn}
          onClick={trocarVel}
          aria-label={`Velocidade ${rotuloVel(vel)} — toque pra trocar`}
          title="Velocidade: 1x · 1,5x · 2x"
        >
          {rotuloVel(vel)}
        </button>
        <audio
          ref={audioRef}
          preload="none"
          src={src}
          onLoadedMetadata={(e) => lerDuracao(e.currentTarget)}
          onDurationChange={(e) => lerDuracao(e.currentTarget)}
          onTimeUpdate={(e) => setPos(e.currentTarget.currentTime)}
          onPlaying={() => { setTocando(true); setCarregando(false); }}
          onPause={() => setTocando(false)}
          onEnded={() => { setTocando(false); setPos(0); }}
          onError={() => { setErro(true); setCarregando(false); }}
        />
      </div>
      {transcricao && <div className={s.transcr}>🎤 {transcricao}</div>}
    </div>
  );
}

// Imagem no chat — espelha o AudioMsg. Carrega o binário via proxy
// /api/conversa/midia → copiloto /painel/conversa/midia (baixa da Cloud API).
// Thumbnail clicável (abre em nova aba). Mídia da Meta expira: no onError
// cai num placeholder discreto, igual o AudioMsg faz com áudio expirado.
function ImagemMsg({ leadId, msgId, caption }: { leadId: string; msgId: string; caption?: string }) {
  const [erro, setErro] = useState(false);
  const src = `/api/conversa/midia?lead_id=${encodeURIComponent(leadId)}&msg_id=${encodeURIComponent(msgId)}`;
  return (
    <div className={s.imgBox}>
      {erro ? (
        <span className={s.bMedia}>🖼️ imagem (não disponível — expirou)</span>
      ) : (
        <a href={src} target="_blank" rel="noopener noreferrer" title="Abrir imagem">
          <img
            className={s.imgThumb}
            src={src}
            alt="imagem"
            loading="lazy"
            onError={() => setErro(true)}
          />
        </a>
      )}
      {caption && <div className={s.imgCaption}>{caption}</div>}
    </div>
  );
}

// Mídia sem legenda real grava um marcador de tipo no conteudo_texto
// ("[document]", "[video]"…). Isso é rótulo interno, não legenda do lead —
// mostrar viraria um "[document]" pendurado embaixo do arquivo.
const _MARCADOR_MIDIA = /^\[(document|video|image|audio|sticker|midia)\]$/i;
function legendaMidia(t?: string): string | undefined {
  const v = (t ?? "").trim();
  if (!v || _MARCADOR_MIDIA.test(v)) return undefined;
  return v;
}

// Ícone por tipo de arquivo — só pelo que dá pra afirmar olhando a extensão/mime.
function iconeArquivo(nome?: string, mime?: string): string {
  const ext = (nome ?? "").split(".").pop()?.toLowerCase() ?? "";
  const m = mime ?? "";
  if (ext === "pdf" || m.includes("pdf")) return "📕";
  if (["doc", "docx", "odt", "rtf"].includes(ext) || m.includes("word")) return "📘";
  if (["xls", "xlsx", "csv", "ods"].includes(ext) || m.includes("sheet") || m.includes("excel")) return "📗";
  if (["ppt", "pptx"].includes(ext) || m.includes("presentation")) return "📙";
  if (["zip", "rar", "7z"].includes(ext)) return "🗜️";
  if (m.startsWith("image/") || ["heic", "jpg", "jpeg", "png", "webp"].includes(ext)) return "🖼️";
  return "📎";
}

// Documento no chat (PDF do contrato, extrato, HEIC mandado como arquivo).
// Antes isso era um rótulo morto "📎 documento" — dava pra ver que existia e
// não dava pra abrir. Agora é link pro mesmo proxy da imagem/áudio, com o nome
// original; o backend responde inline, então PDF abre na aba e o resto baixa.
function DocumentoMsg({
  leadId, msgId, filename, mime, caption,
}: { leadId: string; msgId: string; filename?: string; mime?: string; caption?: string }) {
  const src = `/api/conversa/midia?lead_id=${encodeURIComponent(leadId)}&msg_id=${encodeURIComponent(msgId)}`;
  const nome = filename || "documento";
  return (
    <div className={s.docBox}>
      <a href={src} target="_blank" rel="noopener noreferrer" className={s.docLink} title={`Abrir ${nome}`}>
        <span className={s.docIcone}>{iconeArquivo(filename, mime)}</span>
        <span className={s.docNome}>{nome}</span>
      </a>
      {caption && <div className={s.imgCaption}>{caption}</div>}
    </div>
  );
}

// Vídeo no chat — player inline. Vídeo do WhatsApp não tem nome de arquivo,
// então no erro (mídia expirada / formato que o browser não toca) cai num link
// pra abrir fora, em vez de sumir.
function VideoMsg({ leadId, msgId, caption }: { leadId: string; msgId: string; caption?: string }) {
  const [erro, setErro] = useState(false);
  const src = `/api/conversa/midia?lead_id=${encodeURIComponent(leadId)}&msg_id=${encodeURIComponent(msgId)}`;
  return (
    <div className={s.imgBox}>
      {erro ? (
        <a href={src} target="_blank" rel="noopener noreferrer" className={s.docLink} title="Abrir vídeo">
          <span className={s.docIcone}>🎬</span>
          <span className={s.docNome}>abrir vídeo</span>
        </a>
      ) : (
        <video className={s.videoPlayer} controls preload="none" src={src} onError={() => setErro(true)} />
      )}
      {caption && <div className={s.imgCaption}>{caption}</div>}
    </div>
  );
}

function TempPill({ t }: { t?: string }) {
  const v = (t || "").toLowerCase();
  if (!v) return null;
  const cls = v === "quente" ? s.tempQuente : v === "morno" ? s.tempMorno : s.tempFrio;
  const emo = v === "quente" ? "🔥" : v === "morno" ? "🌤️" : "❄️";
  return <span className={cls}>{emo} {t}</span>;
}

const REFRESH_LISTA_MS = 60_000;
const REFRESH_THREAD_MS = 10_000;

// Etapas que o /painel/crm/mover aceita (mesmo STAGES_CANONICOS do backend).
// O seletor de etapa da conversa só lista essas — colunas derivadas ("Sem
// etapa", "Etapa {id}") do board são read-only e o mover recusa (400).
const STAGES_CANONICOS = [
  "Novo",
  "Em atendimento",
  "Follow-up",
  "Visita marcada",
  "Compareceu",
  "Proposta",
  "Venda ganha",
  "Venda perdida",
];

// Etapas do funil REMARKETING/NUTRIÇÃO (copiloto nutricao.STAGES_NUTRICAO).
// Mover o lead pra cá troca de funil: só move a etapa (não dispara nutrição —
// o motor de nutrição vive em CadenciaEstado.status='remarketing', não na etapa).
const STAGES_REMARKETING = [
  "Base",
  "30 dias",
  "60 dias",
  "90 dias",
  "120 dias",
  "150 dias",
];

const FUNIL_LABEL: Record<string, string> = {
  principal: "Vendas",
  remarketing: "Remarketing",
};

// value do <select> de etapa codifica funil + etapa ("<funil>::<etapa>") porque
// nomes podem colidir entre funis; parseFunilStage desfaz.
function funilStageValue(funil: string, etapa: string): string {
  return `${funil}::${etapa}`;
}
function parseFunilStage(v: string): { funil: string; etapa: string } {
  const i = v.indexOf("::");
  if (i < 0) return { funil: "principal", etapa: v };
  return { funil: v.slice(0, i), etapa: v.slice(i + 2) };
}

export default function ChatPage() {
  // tela cheia: quem rola é a thread, não a página
  useTravarRolagemDaPagina();
  const [colunas, setColunas] = useState<Coluna[]>([]);
  const [viewer, setViewer] = useState<Viewer | null>(null);
  const [erro, setErro] = useState<string | null>(null);
  const [filtro, setFiltro] = useEstadoPersistido<string | null>("chat_corretora_v1", null);
  const [busca, setBusca] = useState("");
  // busca REMOTA (/api/buscar → copiloto /painel/atendimento/busca): varre TODOS
  // os leads do snapshot, não só os ≤100 cards por coluna que o board entrega.
  // MESCLA com o filtro local (não substitui): o remoto não conhece a coluna
  // "Fora do funil" nem traz ultima_at/acende/preview.
  const [buscaRemota, setBuscaRemota] = useState<Card[]>([]);
  const [buscando, setBuscando] = useState(false);
  const [etapaFiltro, setEtapaFiltro] = useState<string | null>(null);
  // fila "🟢 Esperando resposta" (B4, aprovado Dona 24/07): só conversas com
  // acende=true (última msg foi DO LEAD), da espera mais ANTIGA pra mais
  // recente — é fila de dívida, não feed.
  const [esperandoOn, setEsperandoOn] = useState(false);
  // fila inversa "⏳ Esperando resposta": última msg é DA CORRETORA e o lead
  // não respondeu. Vem pronta do copiloto (/api/conversa/esperando): espera
  // mais longa em cima, >30 dias fora (arquivado), toque automático de
  // template (cadência/reengajamento) fora.
  const [aguardandoLeadOn, setAguardandoLeadOn] = useState(false);
  const [aguardandoLead, setAguardandoLead] = useState<EsperaItem[] | null>(null);
  const [atualizadoEm, setAtualizadoEm] = useState<Date | null>(null);

  // conversa aberta
  const [sel, setSel] = useState<Card | null>(null);
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [loadingThread, setLoadingThread] = useState(false);
  const [threadErro, setThreadErro] = useState<string | null>(null);
  const [viaCorretora, setViaCorretora] = useState("");
  const [infoLead, setInfoLead] = useState<{ origem?: string | null; criadoEm?: string | null; coaching?: Coaching; raiox?: Raiox; tarefasLigacao?: TarefaLigacao[]; tarefasLead?: TarefaLead[] }>({});
  const [ligando, setLigando] = useState(false);
  // Desktop: painel do Copiloto aberto (cabe do lado). Celular: fechado —
  // aberto por padrão, ele espremia a conversa até virar uma coluna de duas
  // palavras. Lá ele abre por cima, sob demanda.
  const [painelOn, setPainelOn] = useState(true);
  useEffect(() => {
    if (typeof window !== "undefined" && window.innerWidth <= 820) setPainelOn(false);
  }, []);
  // reanálise sob demanda do Inspector (botão 🔁 no raio-X)
  const [reanalisando, setReanalisando] = useState(false);
  const [reanaliseErro, setReanaliseErro] = useState<string | null>(null);
  const [lidos, setLidos] = useState<Map<string, number>>(new Map());

  // leva 2: notas internas + criar tarefa + tag de linha
  const [notas, setNotas] = useState<Nota[]>([]);
  const [notaTexto, setNotaTexto] = useState("");
  const [salvandoNota, setSalvandoNota] = useState(false);
  const [notaOpen, setNotaOpen] = useState(false);
  // editar nome do lead (só no nosso banco — nunca no Kommo)
  const [nomeOpen, setNomeOpen] = useState(false);
  const [nomeTexto, setNomeTexto] = useState("");
  const [salvandoNome, setSalvandoNome] = useState(false);
  // corrige o telefone do lead SÓ no nosso banco — religa a thread do WhatsApp
  const [telefoneTexto, setTelefoneTexto] = useState("");
  const [linhaAtual, setLinhaAtual] = useState("");
  // leva 3: transferir lead pra outra corretora (modal ↔️)
  const [transfOpen, setTransfOpen] = useState(false);
  const [transfDestino, setTransfDestino] = useState<number | null>(null);
  const [transfMotivo, setTransfMotivo] = useState("");
  const [transferindo, setTransferindo] = useState(false);

  // Janela de 24h (WhatsApp): fora dela, texto livre NÃO entrega — só template.
  // Vem do GET /api/conversa (janela_aberta + ultima_entrada_at). O composer usa
  // pra decidir entre barra de texto livre (aberta) e reengajamento (fechada).
  const [janelaAberta, setJanelaAberta] = useState<boolean>(true);
  const [ultimaEntrada, setUltimaEntrada] = useState<string | null>(null);
  const [msgTexto, setMsgTexto] = useState(""); // texto livre (janela aberta)
  const [copiado, setCopiado] = useState(false); // feedback do "copiar" (janela fechada)
  const [enviando, setEnviando] = useState(false);
  // anexo (📎 foto/PDF/doc) e áudio (🎤) — mesma janela de 24h do texto livre
  const [enviandoMidia, setEnviandoMidia] = useState(false);
  const [gravando, setGravando] = useState(false);
  // Anexos ESCOLHIDOS mas ainda NÃO enviados (pedido Dona 29/07): o 📎 antes
  // disparava o upload na hora, um arquivo por vez. Agora aceita vários, eles
  // ficam numa faixa de pré-visualização acima do campo e só vão no "Enviar" —
  // dá tempo de conferir, tirar um que entrou errado e escrever a legenda.
  const [pendentes, setPendentes] = useState<Anexo[]>([]);
  const seqAnexo = useRef(0);
  const [reengOn, setReengOn] = useState(false); // toggle "mandar reengajamento"
  const [reengTexto, setReengTexto] = useState(""); // corpo do reengajamento
  const [reengando, setReengando] = useState(false);
  const [toast, setToast] = useState<{ tipo: "ok" | "erro"; msg: string } | null>(null);

  // ÚLTIMO envio que NÃO foi (texto, template de reengajamento ou mídia solta).
  // Guarda o conteúdo pra corretora reenviar num clique, sem redigitar nem
  // regravar o áudio. Vive só na conversa onde falhou (leadId) e some no
  // primeiro envio que dá certo, no ✕ ou ao trocar de lead.
  const [falha, setFalha] = useState<Falha | null>(null);

  const [tarefaOpen, setTarefaOpen] = useState(false);
  const [tarefaTexto, setTarefaTexto] = useState("");
  const [tarefaData, setTarefaData] = useState("");
  const [tarefaHora, setTarefaHora] = useState(""); // opcional "HH:MM"; vazio → sem hora
  const [tarefaAnot, setTarefaAnot] = useState(""); // anotações (campo próprio)
  const [tarefaMsgOn, setTarefaMsgOn] = useState(false); // checkbox mensagem planejada
  const [tarefaMsg, setTarefaMsg] = useState(""); // corpo (SÓ o que vem depois da saudação travada)
  const [salvandoTarefa, setSalvandoTarefa] = useState(false);
  const [tarefaErro, setTarefaErro] = useState<string | null>(null);
  // quando != null, o modal de tarefa está EDITANDO a tarefa manual desse id
  // (salva via /api/tarefas/editar); quando null, cria nova (/api/tarefas).
  const [tarefaEditId, setTarefaEditId] = useState<number | null>(null);
  const [concluindoTarefa, setConcluindoTarefa] = useState<number | null>(null);

  // mudar a etapa direto da conversa (reusa /api/crm/mover). O card do Chat só
  // tem o id do Kommo (kommoLeadId); o mover exige o id LOCAL (leads.id) +
  // nome CANÔNICO da etapa. Ao abrir a conversa resolvemos DIRETO na espinha
  // via /api/crm/lead-por-ref (antes varria os boards e o corte de cards por
  // coluna escondia lead real — bug 24/07). vinculado=false = lead do Kommo
  // ainda SEM card na espinha → escolher etapa ADOTA o lead (/api/crm/adotar).
  const [crmLead, setCrmLead] = useState<{
    vinculado: boolean; // false → lead só no snapshot; seletor abre em "— sem etapa —"
    leadId: number | null; // id local (leads.id); null enquanto não vinculado
    funil: string; // "principal" | "remarketing" — funil atual do lead
    etapa: string; // nome da etapa atual dentro do funil ("" = sem etapa)
    colunas: string[]; // (legado) etapas do principal — o seletor usa os dois funis
  } | null>(null);
  const [mudandoEtapa, setMudandoEtapa] = useState(false);

  // 🔎 Assistente busca imóvel de terceiros na web (fora da carteira). Modal.
  const [buscaWebOn, setBuscaWebOn] = useState(false);
  const [empreendOn, setEmpreendOn] = useState(false);
  const [assistenteOn, setAssistenteOn] = useState(false);

  const arqRef = useRef<HTMLInputElement>(null); // input file escondido do 📎
  const mediaRecRef = useRef<MediaRecorder | null>(null);
  const audioChunksRef = useRef<Blob[]>([]);
  const threadRef = useRef<HTMLDivElement>(null);
  const atBottomRef = useRef(true);
  const selIdRef = useRef<string | null>(null);
  // celular: posição da lista guardada ao abrir a conversa, devolvida ao voltar
  const listaRef = useRef<HTMLDivElement>(null);
  const listaScrollRef = useRef(0);
  // última leitura gravada no banco (p8 do telefone + instante) — não repete POST
  const lidaEnviadaRef = useRef<string>("");

  // Não lidas (pacote A, 16/10): grava no banco "li até aqui" desta conversa,
  // só pra quem está logado. Sem isso o número voltava a cada recarregar.
  // Acessório: falhou, a lista só segue mostrando o número até a próxima.
  const marcarLida = useCallback((telefone: string | null | undefined, mensagens: Msg[]) => {
    const p8 = (telefone ?? "").replace(/\D/g, "").slice(-8);
    if (p8.length < 8) return;
    const ate = mensagens.reduce((mx, m) => (m.at && m.at > mx ? m.at : mx), "");
    const chave = `${p8}|${ate}`;
    if (lidaEnviadaRef.current === chave) return;
    lidaEnviadaRef.current = chave;
    fetch("/api/conversa/lida", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(ate ? { telefone, ate } : { telefone }),
    }).catch(() => { lidaEnviadaRef.current = ""; });
  }, []);

  // Voltar pra lista (celular). Se a conversa entrou no histórico do navegador
  // (abrir() empilha), volta por ele: o "voltar" do Android e o gesto do
  // iPhone fazem a mesma coisa que o ‹ — e não saem do Chat.
  const fecharConversa = useCallback(() => {
    setSel(null);
    selIdRef.current = null;
  }, []);
  function voltarPraLista() {
    if (typeof window !== "undefined" && window.history.state?.chatLead) {
      window.history.back();
    } else {
      fecharConversa();
    }
  }
  useEffect(() => {
    const onPop = () => {
      if (!window.history.state?.chatLead && selIdRef.current) fecharConversa();
    };
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, [fecharConversa]);
  // lista de volta na tela (celular): devolve a rolagem de onde ela estava
  useLayoutEffect(() => {
    if (!sel && listaRef.current) listaRef.current.scrollTop = listaScrollRef.current;
  }, [sel]);

  // deep-link (pedido Dona 11/07): /chat?lead=<kommoLeadId>[&nome=<nome>] —
  // o card do CRM navega pra cá e a conversa abre sozinha ao montar.
  // Lead sem thread ainda cai no estado vazio amigável (SEM_CONVERSA).
  const deepLinkFeito = useRef(false);
  useEffect(() => {
    if (deepLinkFeito.current) return;
    deepLinkFeito.current = true;
    const p = new URLSearchParams(window.location.search);
    const lead = (p.get("lead") || "").trim();
    if (!lead) return;
    abrir({
      kommoLeadId: lead,
      nome: (p.get("nome") || "").trim() || "Lead",
      telefone: "",
      corretora: "",
      tags: [],
      stage: "",
      ultima_at: null,
      dias: null,
      acende: false,
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const carregar = useCallback(async (f: string | null) => {
    try {
      const qs = f ? `?corretora=${encodeURIComponent(f)}` : "";
      const r = await fetch(`/api/conversas${qs}`, { cache: "no-store" });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error ?? `http_${r.status}`);
      setColunas(d.colunas ?? []);
      setViewer(d.viewer ?? null);
      setErro(null);
      setAtualizadoEm(new Date());
    } catch (e) {
      setErro(e instanceof Error ? e.message : "erro_desconhecido");
    }
  }, []);

  // fila "⏳ Esperando resposta" — acessória: falha aqui não derruba a lista.
  const carregarEspera = useCallback(async (f: string | null) => {
    try {
      const qs = f ? `?corretora=${encodeURIComponent(f)}` : "";
      const r = await fetch(`/api/conversa/esperando${qs}`, { cache: "no-store" });
      const d = await r.json();
      if (r.ok) setAguardandoLead(d.conversas ?? []);
    } catch { /* badge segue com a última contagem */ }
  }, []);

  // notas internas do lead aberto (acessório — thread abre mesmo se falhar)
  const carregarNotas = useCallback(async (leadId: string) => {
    try {
      const r = await fetch(`/api/conversa/notas?lead_id=${encodeURIComponent(leadId)}`, { cache: "no-store" });
      if (!r.ok) return;
      const d = await r.json();
      if (selIdRef.current === leadId) setNotas(d.notas ?? []);
    } catch { /* sem notas nesse refresh */ }
  }, []);

  // resolve o lead pra habilitar a troca de etapa/funil pela conversa: DIRETO
  // na espinha canônica via /api/crm/lead-por-ref (external_ref == kommoLeadId).
  // Antes varria a resposta dos boards e o corte de cards por coluna escondia
  // lead real → seletor sumia (bug 24/07). vinculado=true → seletor normal no
  // funil/etapa atuais (inclusive remarketing). vinculado=false → lead ainda
  // SEM card na espinha: seletor abre em "— sem etapa —" com as 8 canônicas e
  // escolher uma ADOTA o lead pro funil (POST /api/crm/adotar). 403 (lead de
  // outra corretora) → sem seletor (chip read-only). Acessório: falha aqui
  // NÃO derruba a thread.
  const resolverCrmLead = useCallback(async (kommoLeadId: string) => {
    try {
      const r = await fetch(
        `/api/crm/lead-por-ref?external_ref=${encodeURIComponent(kommoLeadId)}`,
        { cache: "no-store" },
      );
      if (!r.ok) return; // 403 = lead de outra corretora → chip read-only
      const d = await r.json();
      if (selIdRef.current !== kommoLeadId) return;
      if (d.vinculado) {
        const funil = d.funil === "remarketing" ? "remarketing" : "principal";
        setCrmLead({
          vinculado: true,
          leadId: d.lead_id,
          funil,
          etapa: d.etapa ?? "",
          colunas: funil === "remarketing" ? STAGES_REMARKETING : STAGES_CANONICOS,
        });
      } else {
        setCrmLead({
          vinculado: false,
          leadId: null,
          funil: "principal",
          etapa: "",
          colunas: STAGES_CANONICOS,
        });
      }
    } catch { /* sem seletor nesse lead — cai no chip read-only */ }
  }, []);

  useEffect(() => {
    carregar(filtro);
    carregarEspera(filtro);
    const t = setInterval(() => {
      if (document.visibilityState === "visible") {
        carregar(filtro);
        carregarEspera(filtro);
      }
    }, REFRESH_LISTA_MS);
    return () => clearInterval(t);
  }, [carregar, carregarEspera, filtro]);

  // auto-scroll só quando o usuário já estava no fundo
  useEffect(() => {
    if (atBottomRef.current && threadRef.current) {
      threadRef.current.scrollTop = threadRef.current.scrollHeight;
    }
  }, [msgs, notas]);

  // Janela reabriu (lead respondeu) com rascunho pendente do 🏢 Empreendimentos:
  // avisa, senão a corretora manda catálogo montado horas antes sem perceber.
  const janelaAntRef = useRef(janelaAberta);
  useEffect(() => {
    const antes = janelaAntRef.current;
    janelaAntRef.current = janelaAberta;
    if (!antes && janelaAberta && msgTexto.trim()) {
      setToast({ tipo: "ok", msg: "janela reabriu — confere o rascunho antes de enviar" });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [janelaAberta]);

  // toast some sozinho depois de alguns segundos
  useEffect(() => {
    if (!toast) return;
    const id = setTimeout(() => setToast(null), 4200);
    return () => clearTimeout(id);
  }, [toast]);

  // Busca remota debounced (350ms). Só a partir de 2 caracteres — igual o
  // backend exige. AbortController + token: resposta de um termo antigo não
  // pode pousar sobre o termo novo.
  useEffect(() => {
    const q = busca.trim();
    if (q.length < 2) { setBuscaRemota([]); setBuscando(false); return; }
    const ctrl = new AbortController();
    let vivo = true;
    setBuscando(true);
    const t = setTimeout(() => {
      fetch(`/api/buscar?q=${encodeURIComponent(q)}`, { cache: "no-store", signal: ctrl.signal })
        .then((r) => r.json())
        .then((d) => {
          if (!vivo) return;
          const leads = Array.isArray(d?.leads) ? d.leads : [];
          setBuscaRemota(
            leads.map((l: Record<string, unknown>): Card => ({
              kommoLeadId: String(l.lead_id ?? ""),
              nome: String(l.nome ?? "Sem nome"),
              telefone: String(l.telefone ?? ""),
              corretora: String(l.corretora_label ?? ""),
              tags: [],
              stage: String(l.etapa_id ?? ""),
              ultima_at: (l.ultima_msg_at as string | null) ?? null,
              dias: null,
              acende: false,
            })),
          );
        })
        .catch(() => { if (vivo) setBuscaRemota([]); })
        .finally(() => { if (vivo) setBuscando(false); });
    }, 350);
    return () => { vivo = false; ctrl.abort(); clearTimeout(t); };
  }, [busca]);

  // thread viva: re-fetch da conversa aberta a cada 10s, merge sem piscar
  useEffect(() => {
    const tick = async () => {
      const lid = selIdRef.current;
      if (!lid || document.hidden) return;
      try {
        const r = await fetch(`/api/conversa?lead_id=${encodeURIComponent(lid)}`);
        if (!r.ok) return;
        const d = await r.json();
        if (selIdRef.current !== lid) return;
        const novas: Msg[] = d.mensagens ?? [];
        setMsgs((atuais) => (msgsMudaram(atuais, novas) ? novas : atuais));
        // conversa aberta e na tela = o que chegou já foi visto
        if (document.visibilityState === "visible") {
          marcarLida(d.telefone, novas);
          setLidos((m) => new Map(m).set(lid, Date.now()));
        }
        // Janela pode virar sozinha (lead respondeu → abre; passou 24h → fecha).
        setJanelaAberta(d.janela_aberta ?? false);
        setUltimaEntrada(d.ultima_entrada_at ?? null);
      } catch { /* próximo tick tenta de novo */ }
    };
    const id = setInterval(tick, REFRESH_THREAD_MS);
    return () => clearInterval(id);
  }, []);

  // Abre a conversa de um card. TODO commit pós-await é guardado por
  // `selIdRef.current === lid` (lid = lead que ESTA chamada abriu): a corretora
  // troca de lead antes do fetch voltar e a resposta antiga não pode pousar na
  // tela do lead novo. Sem o guard, `setSel(prev => ...)` fazia o pior dano —
  // espalhava telefone/nome do lead A dentro do card do lead B (e daí pro modal
  // de edição).
  async function abrir(card: Card) {
    const lid = card.kommoLeadId;
    // TRAVA: anexo escolhido pra uma conversa NUNCA acompanha a troca de lead —
    // senão a foto de um cliente sairia na conversa do outro.
    if (lid !== selIdRef.current) limparPendentes();
    setLidos((m) => new Map(m).set(lid, Date.now()));
    // celular: a conversa vira uma "página" no histórico, pro voltar do
    // aparelho cair na lista. Desktop mostra as duas colunas, não precisa.
    if (typeof window !== "undefined" && window.matchMedia?.("(max-width: 820px)").matches) {
      if (window.history.state?.chatLead) window.history.replaceState({ chatLead: lid }, "");
      else window.history.pushState({ chatLead: lid }, "");
    }
    selIdRef.current = lid;
    atBottomRef.current = true;
    setSel(card);
    setMsgs([]);
    setNotas([]);
    setCrmLead(null);
    setLinhaAtual("");
    setThreadErro(null);
    setLoadingThread(true);
    setInfoLead({});
    setReanaliseErro(null);
    // reanálise de OUTRO lead não pode deixar o botão deste travado
    setReanalisando(false);
    setMsgTexto("");
    setReengOn(false);
    setReengTexto("");
    setFalha(null); // pendência de envio pertence à conversa onde falhou
    // fail-closed: só libera texto livre depois que o backend confirmar janela
    setJanelaAberta(false);
    setUltimaEntrada(null);
    carregarNotas(lid);
    resolverCrmLead(lid);
    try {
      const r = await fetch(`/api/conversa?lead_id=${encodeURIComponent(lid)}`);
      const d = await r.json();
      if (selIdRef.current !== lid) return; // já trocou de lead — descarta
      if (r.ok) {
        setMsgs(d.mensagens ?? []);
        marcarLida(d.telefone || card.telefone, d.mensagens ?? []);
        setViaCorretora(d.corretora_nome ?? card.corretora);
        setInfoLead({ origem: d.origem, criadoEm: d.criado_em_kommo, coaching: d.coaching, raiox: d.raiox, tarefasLigacao: d.tarefas_ligacao ?? [], tarefasLead: d.tarefas_lead ?? [] });
        setLinhaAtual(d.linha_atual ?? "");
        setJanelaAberta(d.janela_aberta ?? false);
        setUltimaEntrada(d.ultima_entrada_at ?? null);
        setSel((prev) =>
          prev && prev.kommoLeadId === lid
            ? { ...prev, telefone: d.telefone || prev.telefone, nome: d.lead_nome || prev.nome, rmkt: d.tag_remarketing ?? null, resumoIa: d.resumo_ia ?? null }
            : prev,
        );
      } else {
        setThreadErro(String(d.detail ?? d.error ?? r.status));
      }
    } catch {
      if (selIdRef.current === lid) setThreadErro("falha_de_rede");
    } finally {
      // sem `return` antes daqui: se desse return, o spinner do lead novo
      // ficaria girando pra sempre. Só apaga o spinner se ainda é este lead.
      if (selIdRef.current === lid) setLoadingThread(false);
    }
  }

  // Botão 🔁 do raio-X: pede pro Inspector refazer a análise deste lead AGORA
  // (com a conversa mais recente). Demora ~1 min (LLM). Sucesso → atualiza o
  // raio-X local com o retorno + refetch do /api/conversa pra sincronizar tudo.
  async function reanalisarLead() {
    if (!sel || reanalisando) return;
    const lid = sel.kommoLeadId;
    setReanalisando(true);
    setReanaliseErro(null);
    try {
      const r = await fetch("/api/reanalisar", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ lead_id: lid }),
      });
      const d = await r.json().catch(() => ({ erro: "resposta_invalida" }));
      // guard: a reanálise leva ~1 min (LLM). Se a corretora trocou de lead
      // nesse meio-tempo, o raio-X de A NÃO pode ser mesclado no de B — o
      // updater funcional espalha prev.raiox, então seria um Frankenstein
      // (resumo/gap de A sobre campos de B) que o polling nunca corrige.
      if (selIdRef.current !== lid) return;
      if (r.ok && !d.erro) {
        setInfoLead((prev) => ({
          ...prev,
          raiox: {
            ...(prev.raiox ?? {}),
            temperatura: d.temperatura ?? prev.raiox?.temperatura,
            status: d.status ?? prev.raiox?.status,
            resumo: d.resumo ?? undefined,
            gap: d.gap ?? undefined,
            abordagem: d.sugestao ?? undefined,
            analyzed_at: d.analyzed_at ?? prev.raiox?.analyzed_at,
          },
        }));
        // refetch pra sincronizar coaching/tarefas com a análise nova
        try {
          const r2 = await fetch(`/api/conversa?lead_id=${encodeURIComponent(lid)}`);
          if (r2.ok && selIdRef.current === lid) {
            const d2 = await r2.json();
            setInfoLead({ origem: d2.origem, criadoEm: d2.criado_em_kommo, coaching: d2.coaching, raiox: d2.raiox, tarefasLigacao: d2.tarefas_ligacao ?? [], tarefasLead: d2.tarefas_lead ?? [] });
          }
        } catch { /* raio-X já atualizado localmente */ }
      } else {
        setReanaliseErro(
          d.erro === "sem_conversa" ? "Sem conversa pra analisar ainda."
          : d.erro === "lead_sem_cadastro" ? "Contato sem cadastro no funil — sem análise."
          : "Não deu pra reanalisar agora. Tenta de novo em instantes.",
        );
      }
    } catch {
      if (selIdRef.current === lid) setReanaliseErro("Não deu pra reanalisar agora. Tenta de novo em instantes.");
    } finally {
      setReanalisando(false);
    }
  }

  async function salvarNota() {
    if (!sel || salvandoNota) return;
    const texto = notaTexto.trim();
    if (texto.length < 2) return;
    setSalvandoNota(true);
    try {
      const r = await fetch("/api/conversa/notas", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ lead_id: sel.kommoLeadId, texto }),
      });
      const d = await r.json();
      if (r.ok && d.nota) {
        setNotas((ns) => [...ns, d.nota]);
        setNotaTexto("");
        setNotaOpen(false);
      }
    } catch { /* mantém o texto digitado pra tentar de novo */ } finally {
      setSalvandoNota(false);
    }
  }

  // Edita o nome do lead SÓ no nosso banco (nunca no Kommo). Otimista com
  // rollback se o backend recusar (403 = não é seu lead).
  async function salvarNome() {
    if (!sel || salvandoNome) return;
    const novo = nomeTexto.trim();
    if (novo.length < 2 || novo === sel.nome) { setNomeOpen(false); return; }
    const anterior = sel.nome;
    setSalvandoNome(true);
    setSel((prev) => (prev ? { ...prev, nome: novo } : prev)); // otimista
    try {
      const r = await fetch("/api/lead/nome", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ lead_id: sel.kommoLeadId, nome: novo }),
      });
      const d = await r.json().catch(() => ({}));
      if (r.ok && d.nome) {
        setSel((prev) => (prev ? { ...prev, nome: d.nome } : prev));
        setNomeOpen(false);
        setToast({ tipo: "ok", msg: "nome atualizado ✔️" });
      } else {
        setSel((prev) => (prev ? { ...prev, nome: anterior } : prev)); // rollback
        const err = String(d.detail ?? d.error ?? r.status);
        const amigavel = r.status === 403 ? "não é seu lead" : `não renomeei (${err})`;
        setToast({ tipo: "erro", msg: amigavel });
      }
    } catch {
      setSel((prev) => (prev ? { ...prev, nome: anterior } : prev)); // rollback
      setToast({ tipo: "erro", msg: "falha de rede" });
    } finally {
      setSalvandoNome(false);
    }
  }

  // Corrige o TELEFONE do lead SÓ no nosso banco. Normalização/validação (BR
  // E.164) e escopo são do copiloto. Efeito: a thread do WhatsApp resolve por
  // telefone — corrigir religa o lead à conversa real do número novo.
  // Retorna true se salvou (ou não precisava), false se o backend recusou.
  async function salvarTelefone(): Promise<boolean> {
    if (!sel) return true;
    const novo = telefoneTexto.trim();
    if (novo === (sel.telefone || "")) return true; // nada a fazer
    const anterior = sel.telefone;
    setSel((prev) => (prev ? { ...prev, telefone: novo } : prev)); // otimista
    try {
      const r = await fetch("/api/lead/contato", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ lead_id: sel.kommoLeadId, telefone: novo }),
      });
      const d = await r.json().catch(() => ({}));
      if (r.ok && d.telefone) {
        setSel((prev) => (prev ? { ...prev, telefone: d.telefone } : prev));
        return true;
      }
      setSel((prev) => (prev ? { ...prev, telefone: anterior } : prev)); // rollback
      const err = String(d.detail ?? d.error ?? r.status);
      const amigavel =
        r.status === 403 ? "não é seu lead" :
        (r.status === 422 || err === "telefone_invalido") ? "telefone inválido (use +55 DDD 9XXXX-XXXX)" :
        `não atualizei o telefone (${err})`;
      setToast({ tipo: "erro", msg: amigavel });
      return false;
    } catch {
      setSel((prev) => (prev ? { ...prev, telefone: anterior } : prev)); // rollback
      setToast({ tipo: "erro", msg: "falha de rede" });
      return false;
    }
  }

  // Salva nome + telefone (o que mudou) num clique só e fecha o modal se tudo
  // deu certo. Nome via salvarNome() (que já fecha o modal no fim); telefone via
  // salvarTelefone(). Se o telefone recusar, mantém o modal aberto p/ correção.
  async function salvarLead() {
    if (!sel || salvandoNome) return;
    setSalvandoNome(true);
    let okTel = true;
    try {
      okTel = await salvarTelefone();
    } finally {
      setSalvandoNome(false);
    }
    if (!okTel) return; // telefone inválido/recusado — modal fica aberto
    // salvarNome() gerencia seu próprio salvandoNome, fecha o modal e mostra o
    // toast (inclusive quando o nome não mudou: só fecha).
    await salvarNome();
  }

  // Abre o MESMO modal pré-preenchido pra EDITAR uma tarefa manual do lead.
  function abrirEditarTarefa(t: TarefaLead) {
    setTarefaEditId(t.id);
    setTarefaTexto(t.texto);
    setTarefaData(t.data);
    setTarefaHora(t.hora ?? "");
    setTarefaAnot(t.anotacoes ?? "");
    setTarefaMsgOn(!!t.mensagem_planejada);
    setTarefaMsg(t.mensagem_planejada ?? "");
    setTarefaErro(null);
    setTarefaOpen(true);
  }

  // Cria (POST /api/tarefas) OU edita (POST /api/tarefas/editar, quando
  // tarefaEditId != null) a tarefa manual. Reflete otimista no bloco
  // "📋 Tarefas deste cliente" e refaz o fetch da conversa pra sincronizar.
  async function criarTarefa() {
    if (!sel || salvandoTarefa) return;
    const texto = tarefaTexto.trim();
    if (texto.length < 2 || !tarefaData) return;
    setSalvandoTarefa(true);
    setTarefaErro(null);
    // mensagem planejada só quando o check está marcado E há corpo; envia SÓ o
    // corpo (a saudação vai automática no template — não mandamos o prefixo).
    const msgPlan = tarefaMsgOn && tarefaMsg.trim() ? tarefaMsg.trim() : null;
    const editando = tarefaEditId != null;
    try {
      const r = await fetch(editando ? "/api/tarefas/editar" : "/api/tarefas", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(
          editando
            ? {
                id: tarefaEditId,
                data: tarefaData,
                hora: tarefaHora || null,
                texto,
                anotacoes: tarefaAnot.trim() || null,
                mensagem_planejada: msgPlan,
              }
            : {
                lead_id: sel.kommoLeadId,
                data: tarefaData,
                hora: tarefaHora || null,
                texto,
                lead_nome: sel.nome,
                anotacoes: tarefaAnot.trim() || null,
                mensagem_planejada: msgPlan,
              },
        ),
      });
      const d = await r.json();
      if (!r.ok) {
        setTarefaErro(String(d.detail ?? d.error ?? r.status));
        return;
      }
      const hoje = new Date().toISOString().slice(0, 10);
      const novoId = editando ? (tarefaEditId as number) : Number(d.tarefa_id);
      const item: TarefaLead = {
        id: novoId,
        texto,
        data: tarefaData,
        hora: tarefaHora || null,
        atrasada: tarefaData < hoje,
        anotacoes: tarefaAnot.trim() || null,
        mensagem_planejada: msgPlan,
        tem_msg_planejada: !!msgPlan,
      };
      // otimista: substitui (edição) ou adiciona (criação) no bloco do lead
      setInfoLead((prev) => {
        const atuais = prev.tarefasLead ?? [];
        const proximas = editando
          ? atuais.map((x) => (x.id === novoId ? item : x))
          : [...atuais, item];
        proximas.sort((a, b) => (a.data < b.data ? -1 : a.data > b.data ? 1 : a.id - b.id));
        return { ...prev, tarefasLead: proximas };
      });
      setTarefaOpen(false);
      setTarefaEditId(null);
      setToast({ tipo: "ok", msg: editando ? "tarefa atualizada ✔️" : "tarefa criada ✔️" });
    } catch {
      setTarefaErro("falha_de_rede");
    } finally {
      setSalvandoTarefa(false);
    }
  }

  // Conclui uma tarefa manual do lead (POST /api/tarefas/concluir). Otimista:
  // some da lista na hora; rollback + toast se a API falhar.
  async function concluirTarefaLead(t: TarefaLead) {
    if (concluindoTarefa != null) return;
    setConcluindoTarefa(t.id);
    const snapshot = infoLead.tarefasLead;
    setInfoLead((prev) => ({
      ...prev,
      tarefasLead: (prev.tarefasLead ?? []).filter((x) => x.id !== t.id),
    }));
    try {
      const r = await fetch("/api/tarefas/concluir", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: t.id }),
      });
      const d = await r.json().catch(() => ({}));
      if (!r.ok || d.ok === false) throw new Error(String(d.detail ?? d.error ?? r.status));
      setToast({ tipo: "ok", msg: "tarefa concluída ✔️" });
    } catch (e) {
      setInfoLead((prev) => ({ ...prev, tarefasLead: snapshot })); // rollback
      setToast({ tipo: "erro", msg: `não concluí (${e instanceof Error ? e.message : "erro"})` });
    } finally {
      setConcluindoTarefa(null);
    }
  }

  // Muda a ETAPA e/ou o FUNIL do lead direto da conversa (reusa POST
  // /api/crm/mover → copiloto /painel/crm/mover). id LOCAL (crmLead.leadId) +
  // funil destino + etapa daquele funil. Otimista com rollback: atualiza
  // crmLead na hora; se a API falhar, restaura e avisa. O backend valida
  // dono/admin (403 → "não é seu lead"), liga a cadência quando destino=
  // Follow-up (principal) e SÓ move a etapa quando o destino é remarketing
  // (não dispara nutrição — ver copiloto painel_crm_mover).
  async function mudarEtapa(funilDestino: string, destino: string) {
    if (!crmLead || mudandoEtapa || !destino) return;
    // lead ainda sem card na espinha → escolher etapa ADOTA o lead (cria o
    // vínculo + seta o stage no backend) em vez de mover.
    if (!crmLead.vinculado) {
      await adotarLead(destino);
      return;
    }
    if (funilDestino === crmLead.funil && destino === crmLead.etapa) return;
    const antEtapa = crmLead.etapa;
    const antFunil = crmLead.funil;
    setMudandoEtapa(true);
    setCrmLead((cl) =>
      cl
        ? {
            ...cl,
            funil: funilDestino,
            etapa: destino,
            colunas: funilDestino === "remarketing" ? STAGES_REMARKETING : STAGES_CANONICOS,
          }
        : cl,
    ); // otimista
    try {
      const r = await fetch("/api/crm/mover", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          lead_id: crmLead.leadId,
          funil: funilDestino,
          stage_destino: destino,
        }),
      });
      const d = await r.json().catch(() => ({}));
      if (!r.ok || d.ok !== true) {
        const err = String(d.detail ?? d.error ?? r.status);
        throw new Error(err);
      }
      const trocouFunil = funilDestino !== antFunil;
      const alvo = trocouFunil ? `${FUNIL_LABEL[funilDestino] ?? funilDestino} · ${destino}` : destino;
      setToast({
        tipo: "ok",
        msg: d.cadencia_ligada ? `etapa → ${alvo} · cadência ligada ✔️` : `etapa → ${alvo} ✔️`,
      });
    } catch (e) {
      setCrmLead((cl) =>
        cl
          ? {
              ...cl,
              funil: antFunil,
              etapa: antEtapa,
              colunas: antFunil === "remarketing" ? STAGES_REMARKETING : STAGES_CANONICOS,
            }
          : cl,
      ); // rollback
      const err = e instanceof Error ? e.message : "erro";
      const amigavel = err === "lead_de_outra_corretora"
        ? "não é seu lead — só a dona ou admin muda a etapa"
        : `não mudei a etapa (${err})`;
      setToast({ tipo: "erro", msg: amigavel });
    } finally {
      setMudandoEtapa(false);
    }
  }

  // Transfere o lead pra outra corretora (POST /api/crm/transferir). Precisa
  // do id LOCAL da espinha (crmLead.leadId — o botão só aparece vinculado).
  // Backend valida escopo (admin qualquer; corretora só lead DELA → 403).
  // Sucesso: corretora perde o lead da lista (fecha a conversa e recarrega);
  // admin segue vendo — só atualiza a lista e o contexto da conversa.
  async function transferirLead() {
    if (!sel || !crmLead?.leadId || transferindo || transfDestino == null) return;
    setTransferindo(true);
    try {
      const r = await fetch("/api/crm/transferir", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          lead_id: crmLead.leadId,
          corretora_destino: transfDestino,
          motivo: transfMotivo.trim() || null,
        }),
      });
      const d = await r.json().catch(() => ({}));
      if (r.ok && d.ok === true) {
        setTransfOpen(false);
        setTransfMotivo("");
        setTransfDestino(null);
        const para = d.para_label ?? "a outra corretora";
        if (viewer?.isAdmin) {
          setToast({ tipo: "ok", msg: `lead transferido pra ${para} ✔️` });
          // admin continua vendo o lead — reabre pra atualizar linha/contexto
          carregar(filtro);
          abrir({ ...sel });
        } else {
          setToast({ tipo: "ok", msg: `lead transferido pra ${para} — saiu da sua lista ✔️` });
          // corretora perde o acesso: fecha a conversa e recarrega a lista
          selIdRef.current = null;
          setSel(null);
          setMsgs([]);
          setCrmLead(null);
          carregar(filtro);
        }
      } else {
        const err = String(d.detail ?? d.error ?? r.status);
        const amigavel =
          err === "lead_de_outra_corretora" ? "não é seu lead — só a dona ou admin transfere"
          : err === "corretora_invalida" ? "corretora de destino inválida"
          : `não transferi (${err})`;
        setToast({ tipo: "erro", msg: amigavel });
      }
    } catch {
      setToast({ tipo: "erro", msg: "falha de rede" });
    } finally {
      setTransferindo(false);
    }
  }

  // Lead sem card na espinha (crmLead.vinculado=false): escolher uma etapa no
  // seletor ADOTA o lead pro funil principal — POST /api/crm/adotar cria o
  // vínculo (leads + lead_events stage_manual_v2; cadência se Follow-up) e o
  // seletor vira o normal. Otimista com rollback, mesmo feedback do mudarEtapa.
  async function adotarLead(destino: string) {
    if (!sel || !crmLead || mudandoEtapa) return;
    const anterior = crmLead;
    setMudandoEtapa(true);
    setCrmLead({ ...crmLead, etapa: destino }); // otimista
    try {
      const r = await fetch("/api/crm/adotar", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ external_ref: sel.kommoLeadId, etapa: destino }),
      });
      const d = await r.json().catch(() => ({}));
      if (!r.ok || d.ok !== true) {
        throw new Error(String(d.detail ?? d.error ?? r.status));
      }
      setCrmLead({
        vinculado: true,
        leadId: d.lead_id,
        funil: "principal",
        etapa: destino,
        colunas: STAGES_CANONICOS,
      });
      setToast({
        tipo: "ok",
        msg: d.cadencia_ligada ? `etapa → ${destino} · cadência ligada ✔️` : `etapa → ${destino} ✔️`,
      });
    } catch (e) {
      setCrmLead(anterior); // rollback — segue sem vínculo
      const err = e instanceof Error ? e.message : "erro";
      const amigavel = err === "lead_de_outra_corretora"
        ? "não é seu lead — só a dona ou admin muda a etapa"
        : `não mudei a etapa (${err})`;
      setToast({ tipo: "erro", msg: amigavel });
    } finally {
      setMudandoEtapa(false);
    }
  }

  // Envia texto livre PRO LEAD (só quando a janela de 24h está ABERTA).
  // `textoRetry` vem do botão "tentar de novo" (barra de falha ou bolha ⚠) —
  // reenvia o mesmo conteúdo sem depender do que está digitado no campo.
  async function enviarMsg(textoRetry?: string) {
    if (!sel || enviando) return;
    const lid = sel.kommoLeadId;
    const texto = (textoRetry ?? msgTexto).trim();
    if (texto.length < 1) return;
    setEnviando(true);
    try {
      const r = await fetch("/api/conversa/enviar", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ lead_id: lid, texto }),
      });
      const d = await r.json().catch(() => ({}));
      if (r.ok) {
        if (!textoRetry) setMsgTexto("");
        setFalha(null);
        setToast({ tipo: "ok", msg: d.dry_run ? "registrado (dry-run) ✔️" : "enviada ✔️" });
        // atualiza a thread na hora (o backend já gravou o outbound) — só se a
        // conversa aberta ainda for a mesma (guard contra escrever no lead errado)
        try {
          const rc = await fetch(`/api/conversa?lead_id=${encodeURIComponent(lid)}`);
          if (rc.ok && selIdRef.current === lid) {
            const dc = await rc.json();
            setMsgs(dc.mensagens ?? []);
            setJanelaAberta(dc.janela_aberta ?? false);
            setUltimaEntrada(dc.ultima_entrada_at ?? null);
          }
        } catch { /* próximo tick pega */ }
      } else {
        const motivo = String(d.detail ?? d.error ?? r.status);
        // o texto sai do campo e passa a viver na barra de falha — dono único,
        // sem risco de mandar duas vezes (uma pelo campo, outra pelo retry)
        if (!textoRetry) setMsgTexto("");
        // Linha travada por cobrança na Meta: tentar de novo dá no mesmo até
        // alguém acertar o pagamento. Guarda o texto pra ela copiar, mas não
        // oferece o retry — o caminho é o celular dela.
        if (motivo === "linha_bloqueada_cobranca") {
          setToast({
            tipo: "erro",
            msg: "o WhatsApp está recusando os envios desse número por problema de pagamento na conta — fala com essa pessoa pelo seu celular e avisa a Dona",
          });
          setMsgTexto(texto);
        } else {
          setFalha({ tipo: "texto", leadId: lid, texto, motivo });
          setToast({ tipo: "erro", msg: `não enviou (${motivo})` });
        }
      }
    } catch {
      if (!textoRetry) setMsgTexto("");
      setFalha({ tipo: "texto", leadId: lid, texto, motivo: "falha de rede" });
      setToast({ tipo: "erro", msg: "falha de rede" });
    } finally {
      setEnviando(false);
    }
  }

  // Envia MÍDIA pro lead (foto/PDF/documento/áudio) pela linha da corretora.
  // Mesma regra do texto livre: só dentro da janela de 24h (o composer só existe
  // com janelaAberta). Privacidade e DRY_RUN são do copiloto.
  // refetch da thread depois de mandar mídia (o backend já gravou o outbound).
  // Guard contra escrever no lead errado se a corretora trocou de conversa.
  async function recarregarThread() {
    const lid = selIdRef.current;
    if (!lid) return;
    try {
      const rc = await fetch(`/api/conversa?lead_id=${encodeURIComponent(lid)}`);
      if (rc.ok && selIdRef.current === lid) {
        const dc = await rc.json();
        setMsgs(dc.mensagens ?? []);
        setJanelaAberta(dc.janela_aberta ?? false);
        setUltimaEntrada(dc.ultima_entrada_at ?? null);
      }
    } catch { /* próximo tick pega */ }
  }

  // Devolve true se foi. `quieto` cala o toast e o refetch por arquivo — quem
  // manda uma fila inteira dá UM toast e UM refetch no fim, não um por item.
  async function enviarMidia(
    blob: Blob,
    tipo: "image" | "document" | "audio",
    filename: string,
    legenda = "",
    quieto = false,
  ): Promise<boolean> {
    if (!sel || enviandoMidia) return false;
    const lid = sel.kommoLeadId;
    setEnviandoMidia(true);
    try {
      const fd = new FormData();
      fd.append("lead_id", lid);
      fd.append("tipo", tipo);
      fd.append("legenda", legenda);
      fd.append("arquivo", blob, filename);
      const r = await fetch("/api/conversa/enviar-midia", { method: "POST", body: fd });
      const d = await r.json().catch(() => ({}));
      if (r.ok && d.ok) {
        setFalha(null);
        if (!quieto) {
          const rotulo = tipo === "audio" ? "áudio" : tipo === "image" ? "foto" : "arquivo";
          setToast({ tipo: "ok", msg: d.dry_run ? `${rotulo} registrado (dry-run) ✔️` : `${rotulo} enviado ✔️` });
          await recarregarThread();
        }
        return true;
      }
      const motivo = String(d.detail ?? d.error ?? r.status);
      // anexo da FILA não vira barra de falha: ele continua na pré-visualização
      // e o próprio "Enviar" tenta de novo. Só o envio solto (áudio gravado,
      // retry) precisa de um lugar pra ser reenviado.
      if (!quieto) setFalha({ tipo: "midia", leadId: lid, blob, midia: tipo, filename, legenda, motivo });
      setToast({ tipo: "erro", msg: `não enviou (${motivo})` });
      return false;
    } catch {
      if (!quieto) setFalha({ tipo: "midia", leadId: lid, blob, midia: tipo, filename, legenda, motivo: "falha de rede" });
      setToast({ tipo: "erro", msg: "falha de rede" });
      return false;
    } finally {
      setEnviandoMidia(false);
    }
  }

  // 📎 → escolheu arquivo(s). NÃO envia: só põe na fila de pendentes pra
  // aparecer na pré-visualização. Imagem vira "image" (preview no WhatsApp),
  // o resto "document". Barra 16MB aqui pra avisar antes de subir nada.
  function onPickFile(e: React.ChangeEvent<HTMLInputElement>) {
    const escolhidos = Array.from(e.target.files ?? []);
    e.target.value = ""; // permite escolher o MESMO arquivo de novo depois
    if (!escolhidos.length) return;

    const grandes = escolhidos.filter((f) => f.size > 16 * 1024 * 1024);
    const ok = escolhidos.filter((f) => f.size <= 16 * 1024 * 1024);
    if (grandes.length) {
      setToast({ tipo: "erro", msg: `${grandes.length} arquivo(s) acima de 16MB ficaram de fora` });
    }
    if (!ok.length) return;

    setPendentes((prev) => [
      ...prev,
      ...ok.map((f) => ({
        id: ++seqAnexo.current,
        file: f,
        url: URL.createObjectURL(f),
        tipo: (f.type.startsWith("image/") ? "image" : "document") as Anexo["tipo"],
      })),
    ]);
  }

  function tirarPendente(id: number) {
    setPendentes((prev) => {
      const alvo = prev.find((a) => a.id === id);
      if (alvo) URL.revokeObjectURL(alvo.url);
      return prev.filter((a) => a.id !== id);
    });
  }

  function limparPendentes() {
    setPendentes((prev) => {
      prev.forEach((a) => URL.revokeObjectURL(a.url));
      return [];
    });
  }

  // "Enviar" com anexos na fila: manda um a um, NA ORDEM em que aparecem.
  // O texto do campo vira legenda do PRIMEIRO (igual ao WhatsApp) — se não
  // houver anexo nenhum, o Enviar segue no caminho normal de texto.
  async function enviarPendentes() {
    if (!sel || !pendentes.length || enviandoMidia) return;
    const fila = pendentes;
    const legenda = msgTexto.trim();
    let enviados = 0;
    for (let i = 0; i < fila.length; i++) {
      const a = fila[i];
      const okEnvio = await enviarMidia(a.file, a.tipo, a.file.name || "arquivo", i === 0 ? legenda : "", true);
      if (!okEnvio) break; // parou no erro: o que sobrou continua na fila pra tentar de novo
      enviados++;
      tirarPendente(a.id);
    }
    if (enviados) {
      setMsgTexto("");
      setToast({
        tipo: enviados === fila.length ? "ok" : "erro",
        msg: enviados === fila.length
          ? `${enviados} ${enviados === 1 ? "arquivo enviado" : "arquivos enviados"} ✔️`
          : `enviei ${enviados} de ${fila.length} — o resto continua aí pra tentar de novo`,
      });
      await recarregarThread();
    }
  }

  // 🎤 → grava e, ao parar, envia. Toggle: 1º clique grava, 2º para e manda.
  async function toggleGravar() {
    if (gravando) {
      mediaRecRef.current?.stop();
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const mr = new MediaRecorder(stream);
      audioChunksRef.current = [];
      mr.ondataavailable = (ev) => { if (ev.data.size) audioChunksRef.current.push(ev.data); };
      mr.onstop = () => {
        stream.getTracks().forEach((t) => t.stop()); // solta o mic (tira o ponto vermelho)
        const blob = new Blob(audioChunksRef.current, { type: mr.mimeType || "audio/webm" });
        setGravando(false);
        if (blob.size > 0) enviarMidia(blob, "audio", "audio.ogg");
      };
      mediaRecRef.current = mr;
      mr.start();
      setGravando(true);
    } catch {
      setToast({ tipo: "erro", msg: "sem acesso ao microfone — libera no cadeado do navegador" });
    }
  }

  // Reengajamento FORA da janela: template UTILITY (saudação automática). Envia
  // SÓ o corpo (o prefixo travado não vai — a saudação está no template).
  // `corpoRetry` vem do botão "tentar de novo" da barra de falha.
  async function reengajar(corpoRetry?: string) {
    if (!sel || reengando) return;
    const lid = sel.kommoLeadId;
    const corpo = (corpoRetry ?? reengTexto).trim();
    if (corpo.length < 1) return;
    setReengando(true);
    try {
      const r = await fetch("/api/conversa/reengajar", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ lead_id: lid, texto: corpo }),
      });
      const d = await r.json().catch(() => ({}));
      if (r.ok) {
        setReengOn(false);
        setFalha(null);
        setToast({ tipo: "ok", msg: d.dry_run ? "reengajamento registrado (dry-run) ✔️" : "reengajamento enviado ✔️" });
        try {
          const rc = await fetch(`/api/conversa?lead_id=${encodeURIComponent(lid)}`);
          if (rc.ok && selIdRef.current === lid) {
            const dc = await rc.json();
            setMsgs(dc.mensagens ?? []);
            setJanelaAberta(dc.janela_aberta ?? false);
            setUltimaEntrada(dc.ultima_entrada_at ?? null);
          }
        } catch { /* próximo tick pega */ }
      } else {
        const err = String(d.detail ?? d.error ?? r.status);
        // Erros que TENTAR DE NOVO não resolve — cada um com o motivo em
        // português. Fora daqui, mantém o retry (falha de entrega/rede).
        const semRetry: Record<string, string> = {
          lead_opt_out: "esse lead pediu pra não receber mais mensagens",
          // linha sem casco de mensagem livre aprovado na Meta (caso da
          // Pessoa7, 10/09): repetir dá no mesmo até a Meta aprovar.
          linha_sem_template_de_reengajamento:
            "essa conversa está fora da janela de 24h e o número ainda não tem template de reengajamento aprovado — avisa a Dona",
          linha_sem_template_utility_aprovado:
            "essa conversa está fora da janela de 24h e o número ainda não tem template de reengajamento aprovado — avisa a Dona",
          reengajamento_indisponivel_nesta_linha:
            "esse número ainda não manda reengajamento pelo painel",
          // A Meta está recusando tudo dessa linha por forma de pagamento.
          // Repetir não adianta — o caminho é o celular da corretora, que em
          // coexistência é WhatsApp normal (sem API, sem cobrança).
          linha_bloqueada_cobranca:
            "o WhatsApp está recusando os envios desse número por problema de pagamento na conta — fala com essa pessoa pelo seu celular e avisa a Dona",
        };
        const amigavel = semRetry[err] ?? `não reengajou (${err})`;
        if (!(err in semRetry)) setFalha({ tipo: "template", leadId: lid, texto: corpo, motivo: err });
        setToast({ tipo: "erro", msg: amigavel });
      }
    } catch {
      setFalha({ tipo: "template", leadId: lid, texto: corpo, motivo: "falha de rede" });
      setToast({ tipo: "erro", msg: "falha de rede" });
    } finally {
      setReengando(false);
    }
  }

  // Botão "tentar de novo" da barra de falha: reenvia exatamente o que não foi,
  // pelo mesmo caminho de antes (texto livre, template ou mídia).
  async function tentarDeNovo() {
    const f = falha;
    if (!f || !sel || f.leadId !== sel.kommoLeadId) return;
    if (f.tipo === "texto") { await enviarMsg(f.texto); return; }
    if (f.tipo === "template") { await reengajar(f.texto); return; }
    await enviarMidia(f.blob, f.midia, f.filename, f.legenda);
  }

  // ✕ na barra: desiste do reenvio. Texto volta pro campo (dá pra editar e
  // mandar de novo); mídia é descartada mesmo.
  function descartarFalha() {
    const f = falha;
    setFalha(null);
    if (f && (f.tipo === "texto" || f.tipo === "template")) {
      if (f.tipo === "texto") setMsgTexto(f.texto);
      else { setReengOn(true); setReengTexto(f.texto); }
    }
  }

  // "✓ liguei" no painel do lead — registra o toque de ligação da cadência como
  // feito (avança o follow-up) e remove o card da lista otimisticamente.
  async function marcarLiguei(t: TarefaLigacao) {
    if (ligando) return;
    setLigando(true);
    const snapshot = infoLead.tarefasLigacao;
    setInfoLead((prev) => ({ ...prev, tarefasLigacao: (prev.tarefasLigacao ?? []).filter((x) => x.cadencia_id !== t.cadencia_id) }));
    try {
      const r = await fetch("/api/cadencia/ligacao-feita", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ cadencia_id: t.cadencia_id, toque: t.toque }),
      });
      const d = await r.json().catch(() => ({}));
      if (!r.ok || d.ok !== true) throw new Error(String(d.motivo ?? d.error ?? r.status));
      setToast({ tipo: "ok", msg: "ligação registrada ✔️" });
    } catch (e) {
      setInfoLead((prev) => ({ ...prev, tarefasLigacao: snapshot })); // rollback
      setToast({ tipo: "erro", msg: `não registrei (${e instanceof Error ? e.message : "erro"})` });
    } finally {
      setLigando(false);
    }
  }

  const ultimaAtMs = (c: Card) => (c.ultima_at ? Date.parse(c.ultima_at) : 0);
  // Não lidas por conversa: o número vem do banco (por login). Abriu aqui
  // agora → zera na hora, sem esperar o próximo refresh da lista.
  const naoLidas = (c: Card): number => {
    const n = c.nao_lidas ?? 0;
    if (n <= 0) return 0;
    if (sel?.kommoLeadId === c.kommoLeadId) return 0;
    const lido = lidos.get(c.kommoLeadId);
    if (lido && ultimaAtMs(c) <= lido) return 0;
    return n;
  };

  const etapaLabel = (stage: string) => colunas.find((c) => c.stage === stage)?.label || null;
  // base da lista (sem etapa/busca) — todos os cards visíveis do viewer
  // (escopo por corretora já resolvido no backend via X-Painel-User).
  // ATENÇÃO: o /painel/kanban corta cada coluna em 100 cards; `col.total` é o
  // número real. Por isso a lista pode ser menor do que o total da etapa — o
  // corte é rotulado explicitamente na UI (avisoCorte).
  const baseConversas = colunas
    .flatMap((col) => col.cards)
    .filter((c) => !ehGrupo(c.telefone));
  // Contador da fila "🟢 Esperando resposta": conta a MESMA base que a lista
  // renderiza (cards recebidos, inclusive "Fora do funil"). Antes o label usava
  // uma definição e a lista outra — botão e lista divergiam. O `esperando` do
  // payload NÃO serve aqui: ele ignora "Fora do funil" (o maior bucket).
  const esperandoCount = baseConversas.filter((c) => c.acende).length;
  // Total de conversas com não lidas → título da aba "(3) Chat" e número no
  // ícone do app instalado (onde o aparelho deixa). Conta conversa, não
  // mensagem, igual o WhatsApp Web.
  const totalNaoLidas = new Set(
    baseConversas.filter((c) => naoLidas(c) > 0).map((c) => c.kommoLeadId),
  ).size;
  // quanto ficou de fora do board por causa do corte de 100/coluna
  const totalReal = colunas.reduce((n, col) => n + (col.total ?? col.cards.length), 0);
  const totalRecebido = colunas.reduce((n, col) => n + col.cards.length, 0);
  const truncado = totalReal > totalRecebido;

  // fila "⏳ Esperando resposta": já vem escopada/filtrada/ordenada do backend
  // (espera mais longa em cima) — mapeia pra Card só pra reusar abrir().
  const aguardandoCards: Card[] = (aguardandoLead ?? []).map((e) => ({
    kommoLeadId: e.kommoLeadId,
    nome: e.nome,
    telefone: e.telefone,
    corretora: e.corretora,
    tags: [],
    stage: e.etapa || "",
    ultima_at: e.ultima_at,
    dias: null,
    acende: false,
    preview: e.preview,
    preview_de: e.preview_de ?? "corretora", // nessa fila quem falou por último foi ela
  }));

  // Filtros de ETAPA e BUSCA valem pras DUAS filas. Antes a precedência do
  // ternário deixava os dois no ramo `false` — a fila "⏳ Sem resposta do lead"
  // ignorava o seletor de etapa. Hoistados pra fora.
  const q = busca.trim().toLowerCase();
  const casaBusca = (c: Card) =>
    !q || (c.nome || "").toLowerCase().includes(q) || (c.telefone || "").includes(q);
  // stage "" = card sem lead no snapshot: sempre visível (não some no filtro)
  const casaEtapa = (c: Card) => !etapaFiltro || !c.stage || c.stage === etapaFiltro;

  const conversas = (
    aguardandoLeadOn
      ? aguardandoCards
      : baseConversas
          .filter((c) => !esperandoOn || c.acende)
          // fila ligada: espera mais ANTIGA em cima (asc); normal: mais recente (desc)
          .sort((a, b) => (esperandoOn ? ultimaAtMs(a) - ultimaAtMs(b) : ultimaAtMs(b) - ultimaAtMs(a)))
  )
    .filter(casaEtapa)
    .filter(casaBusca);

  // Hits REMOTOS da busca que o board não entregou (o corte de 100/coluna
  // esconde funil vivo, não só cemitério). Dedupe pelo sufixo de 8 dígitos do
  // telefone — o mesmo critério que o backend usa pra casar conversa.
  const p8 = (tel: string | null | undefined) => (tel ?? "").replace(/\D/g, "").slice(-8);
  const locaisP8 = new Set(conversas.map((c) => p8(c.telefone)).filter(Boolean));
  const locaisIds = new Set(conversas.map((c) => c.kommoLeadId));
  const extrasBusca = q.length >= 2 && !aguardandoLeadOn && !esperandoOn
    ? buscaRemota.filter(
        (c) =>
          c.kommoLeadId &&
          !locaisIds.has(c.kommoLeadId) &&
          (!p8(c.telefone) || !locaisP8.has(p8(c.telefone))) &&
          casaEtapa(c),
      )
    : [];

  // Card da lista. `remoto` = veio da busca no snapshot (fora do board): sem
  // última atividade confiável, então não mostra badge de espera/dias.
  // "Você:" / "Marina:" antes da prévia, como no WhatsApp. Admin não é quem
  // falou: pra ela aparece a corretora ("Pessoa12:").
  const quemFalou = (c: Card): string => {
    if (c.preview_de === "corretora") {
      return viewer?.isAdmin ? `${abrev(c.corretora) || "Corretora"}: ` : "Você: ";
    }
    if (c.preview_de === "lead") {
      const p = (c.nome || "").trim().split(/\s+/)[0] ?? "";
      return /[a-zA-ZÀ-ÿ]/.test(p) ? `${p}: ` : "Lead: ";
    }
    return "";
  };

  // Card da lista. `remoto` = veio da busca no snapshot (fora do board): sem
  // última atividade confiável, então não mostra badge de espera/dias.
  const renderConv = (c: Card, remoto = false) => {
    const n = remoto ? 0 : naoLidas(c);
    const etapa = etapaLabel(c.stage);
    return (
      <div
        key={`${remoto ? "r-" : ""}${c.kommoLeadId}`}
        className={sel?.kommoLeadId === c.kommoLeadId ? s.convOn : s.conv}
        onClick={() => {
          listaScrollRef.current = listaRef.current?.scrollTop ?? 0;
          abrir(c);
        }}
        data-testid="conversa-item"
      >
        <div className={s.av}><Avatar leadId={c.kommoLeadId} nome={c.nome} buscar={false} /></div>
        <div className={s.convTxt}>
          <div className={s.convTop}>
            <span className={n > 0 ? s.convNomeNovo : s.convNome}>
              {c.nome}
              {c.transferido && (
                <span title="Lead transferido de corretora recentemente" style={{ marginLeft: 4, fontSize: 11 }}>↔️</span>
              )}
            </span>
            {/* na fila, o que importa é HÁ QUANTO TEMPO se espera */}
            {remoto ? null : esperandoOn || aguardandoLeadOn
              ? <EsperaBadge ultimaAt={c.ultima_at} quem={aguardandoLeadOn ? "corretora" : "lead"} />
              : <DiasBadge dias={c.dias} />}
          </div>
          {/* prévia da última mensagem em TODAS as filas (pacote A). Etapa:
              NUNCA o id cru do Kommo (8 dígitos leem como telefone). */}
          <div className={s.convPrevLinha}>
            <span className={n > 0 ? s.convPrevNovo : s.convPrev} data-testid="conversa-previa">
              {remoto
                ? `${etapa ? `${etapa} · ` : ""}fora da lista carregada`
                : c.preview
                  ? <>{aguardandoLeadOn && etapa ? `${etapa} · ` : ""}<b className={s.convQuem}>{quemFalou(c)}</b>{c.preview}</>
                  : (etapa ?? "")}
            </span>
            {n > 0 && (
              <span
                className={s.naoLidas}
                data-testid="nao-lidas"
                title={`${n} mensage${n > 1 ? "ns" : "m"} não lida${n > 1 ? "s" : ""}`}
              >
                {n > 99 ? "99+" : n}
              </span>
            )}
          </div>
          {viewer?.isAdmin && c.corretora && <div className={s.convSub}>{abrev(c.corretora)}</div>}
        </div>
      </div>
    );
  };

  // título da aba + ícone do app com o total de conversas não lidas
  useEffect(() => {
    if (colunas.length === 0) return; // ainda carregando: não zera o ícone à toa
    document.title = totalNaoLidas > 0 ? `(${totalNaoLidas}) Chat · Acme` : "Chat · Acme";
    const nav = navigator as Navigator & {
      setAppBadge?: (n?: number) => Promise<void>;
      clearAppBadge?: () => Promise<void>;
    };
    try {
      if (totalNaoLidas > 0) nav.setAppBadge?.(totalNaoLidas)?.catch(() => {});
      else nav.clearAppBadge?.()?.catch(() => {});
    } catch { /* aparelho sem suporte a número no ícone */ }
  }, [totalNaoLidas, colunas.length]);
  useEffect(() => () => { document.title = "Acme V2"; }, []);

  const raiox = infoLead.raiox;
  const tarefasLigacao = infoLead.tarefasLigacao ?? [];
  const tarefasLead = infoLead.tarefasLead ?? [];
  const coaching = infoLead.coaching;

  return (
    <div className={s.wrap}>
      <div className={s.topbar}>
        <span className={s.topTitulo}>💬 Chat</span>
        <span className={s.topNota}>responder + reengajar + coaching</span>
        <button
          className={s.pill}
          onClick={() => setBuscaWebOn(true)}
          title="Busca imóvel de terceiros na web (VivaReal, Chaves na Mão). Só pra você abrir e avaliar — não vai pro lead."
        >
          🔎 Buscar imóvel na web
        </button>
        <button
          className={s.pill}
          onClick={() => setEmpreendOn(true)}
          title="Catálogo PRÓPRIO da Acme (imóveis da carteira). Filtra, casa com o lead e insere a msg pronta no campo pra você revisar — não vai pro lead sozinho."
        >
          🏢 Empreendimentos
        </button>
        <button
          className={s.pill}
          onClick={() => setAssistenteOn(true)}
          title="Assistente — sua copilota. Pergunta sobre leads, seu dia e imóveis, gera arte do empreendimento e registra ações (com confirmação). Nada vai pro lead sozinho."
        >
          🧠 Assistente
        </button>
        <span className={s.topMeta}>
          {erro
            ? `erro: ${erro}`
            : atualizadoEm
              ? `atualizado ${atualizadoEm.toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" })} · refresh 60s`
              : "carregando…"}
        </span>
      </div>

      {buscaWebOn && <BuscaImovelWeb onClose={() => setBuscaWebOn(false)} />}

      {assistenteOn && (
        <Assistente
          onClose={() => setAssistenteOn(false)}
          leadCtx={sel ? { nome: sel.nome, telefone: sel.telefone } : null}
        />
      )}

      {empreendOn && (
        <Empreendimentos
          onClose={() => setEmpreendOn(false)}
          onInserir={(msg) => {
            // Insere no CAMPO DE TEXTO do chat (composer) — corretora revisa e envia.
            // NUNCA envia automático. Anexa se já houver texto digitado.
            setMsgTexto((atual) => (atual.trim() ? `${atual.trimEnd()}\n\n${msg}` : msg));
            setEmpreendOn(false);
            if (!sel) {
              setToast({ tipo: "erro", msg: "abra uma conversa pra usar o rascunho" });
            } else if (!janelaAberta) {
              // fora da janela de 24h o texto livre não entrega — o rascunho
              // aparece read-only no rodapé pra copiar/colar no WhatsApp.
              setToast({ tipo: "ok", msg: "janela de 24h fechada — copia o rascunho e manda pelo WhatsApp" });
            }
          }}
        />
      )}

        <div className={`${s.body} ${sel ? s.comSel : ""}`}>
          {/* coluna esquerda: lista de conversas */}
          <div className={s.lista}>
            <div className={s.listaBusca}>
              <input value={busca} onChange={(e) => setBusca(e.target.value)} placeholder="🔍 Buscar conversa…" />
            </div>
            {viewer?.isAdmin && (
              <div className={s.filtros}>
                <button className={!filtro ? s.pillOn : s.pill} onClick={() => setFiltro(null)}>Todas</button>
                {CORRETORAS.map((c) => (
                  <button key={c} className={filtro === c ? s.pillOn : s.pill} onClick={() => setFiltro(c)}>{abrev(c)}</button>
                ))}
              </div>
            )}
            <div className={s.etapaSel}>
              <select value={etapaFiltro ?? ""} onChange={(e) => setEtapaFiltro(e.target.value || null)}>
                <option value="">Todas as etapas</option>
                {colunas.map((col) => (
                  <option key={col.stage} value={col.stage}>
                    {col.label} ({col.cards.length < (col.total ?? 0)
                      ? `${col.cards.length} de ${col.total}`
                      : col.total})
                  </option>
                ))}
              </select>
              {/* fila de dívida (B4): só quem tem o lead esperando, mais antiga em cima */}
              <button
                className={esperandoOn ? s.pillOn : s.pill}
                onClick={() => { setEsperandoOn((v) => !v); setAguardandoLeadOn(false); }}
                title="Só conversas em que o LEAD falou por último — quem espera há mais tempo aparece em cima"
              >
                🟢 Esperando resposta ({esperandoCount})
              </button>
              {/* fila inversa: VOCÊ falou por último e o lead não respondeu */}
              <button
                className={aguardandoLeadOn ? s.pillOn : s.pill}
                onClick={() => { setAguardandoLeadOn((v) => !v); setEsperandoOn(false); }}
                title="Só conversas em que a CORRETORA falou por último e o lead ainda não respondeu — espera mais longa em cima; +30 dias sem movimento sai da fila; toque automático da cadência fica de fora"
              >
                ⏳ Sem resposta do lead ({aguardandoLead ? aguardandoLead.length : "…"})
              </button>
            </div>
            <div className={s.listaBody} ref={listaRef}>
              {colunas.length === 0 && !erro && <div className={s.listaVazia}>Carregando conversas…</div>}
              {colunas.length > 0 && conversas.length === 0 && extrasBusca.length === 0 && (
                <div className={s.listaVazia}>
                  {buscando ? "Buscando…" : "Nenhuma conversa encontrada."}
                </div>
              )}
              {conversas.map((c) => renderConv(c))}
              {/* Hits da busca que NÃO estão no board (corte de 100 por coluna).
                  Grupo separado: vêm do snapshot, sem última atividade/preview. */}
              {extrasBusca.length > 0 && (
                <>
                  <div className={s.listaVazia} style={{ fontSize: 11, padding: "8px 12px 4px" }}>
                    🔎 {extrasBusca.length} fora da lista carregada{buscando ? " · buscando…" : ""}
                  </div>
                  {extrasBusca.map((c) => renderConv(c, true))}
                </>
              )}
              {/* o board entrega no máximo 100 cards por etapa — deixa explícito */}
              {truncado && !aguardandoLeadOn && (
                <div className={s.listaVazia} style={{ fontSize: 11 }}>
                  mostrando {totalRecebido} de {totalReal} conversas (100 por etapa) — use a busca pra achar o resto
                </div>
              )}
            </div>
          </div>

          {/* centro: thread */}
          <div className={s.main}>
            {sel ? (
              <>
                <div className={s.chatHead}>
                  {/* volta pra lista no celular — no desktop o CSS esconde,
                      porque lá as duas colunas aparecem juntas. */}
                  <button
                    className={s.voltarLista}
                    onClick={voltarPraLista}
                    aria-label="Voltar para a lista de conversas"
                  >
                    ‹
                  </button>
                  <div className={s.av}><Avatar leadId={sel.kommoLeadId} nome={sel.nome} /></div>
                  <div className={s.chatInfo}>
                    <div className={s.chatNome}>
                      <span className={s.chatNomeTxt}>{sel.nome}</span>
                      <button
                        onClick={() => { setNomeTexto(sel.nome); setTelefoneTexto(sel.telefone || ""); setNomeOpen(true); }}
                        title="Editar nome e telefone do lead (só no nosso sistema)"
                        style={{
                          background: "none", border: "none", cursor: "pointer",
                          padding: 0, fontSize: 12, opacity: 0.55, lineHeight: 1,
                        }}
                      >✏️</button>
                    </div>
                    <div className={s.chatSub} data-testid="chat-sub">
                      {(viaCorretora || sel.corretora) ? `via WhatsApp da ${viaCorretora || sel.corretora}` : "conversa"}
                      {sel.telefone ? ` · ${sel.telefone}` : ""}
                    </div>
                  </div>
                  <div className={s.chatTools}>
                    {/* 📞 Ligar — no celular o `tel:` abre o discador com o
                        número já digitado, então a corretora sai da
                        notificação e cai na ligação em dois toques. No
                        desktop o link não faz nada útil, por isso o botão só
                        aparece em tela pequena (CSS .toolLigar). */}
                    {sel.telefone && (
                      <a
                        className={`${s.tool} toolLigar`}
                        href={linkTel(sel.telefone)}
                        title={`Ligar para ${sel.telefone}`}
                      >📞 Ligar</a>
                    )}
                    <button
                      className={s.tool}
                      onClick={() => {
                        setTarefaEditId(null);
                        setTarefaTexto("");
                        setTarefaData(new Date().toLocaleDateString("en-CA"));
                        setTarefaHora("");
                        setTarefaAnot("");
                        setTarefaMsgOn(false);
                        setTarefaMsg("");
                        setTarefaErro(null);
                        setTarefaOpen(true);
                      }}
                      title="Criar tarefa pra este lead"
                    >☑️ Tarefa</button>
                    <button
                      className={s.tool}
                      onClick={() => setNotaOpen(true)}
                      title="Anotação interna — só a equipe vê"
                    >📝 Anotação</button>
                    {crmLead?.vinculado && crmLead.leadId != null && (
                      <button
                        className={s.tool}
                        onClick={() => { setTransfDestino(null); setTransfMotivo(""); setTransfOpen(true); }}
                        title="Transferir este lead pra outra corretora — o histórico fica, marcado por linha"
                      >↔️ Transferir</button>
                    )}
                    <button
                      className={painelOn ? s.toolOn : s.tool}
                      onClick={() => setPainelOn((v) => !v)}
                      title="Raio-X do Copiloto: temperatura, gap, sugestão"
                    >🤖 Copiloto</button>
                  </div>
                </div>

                <div className={s.chips}>
                  {linhaAtual && (
                    <span className={s.chipLinha} title="Linha (telefone) por onde essa conversa roda hoje — respostas saem por aqui">
                      📱 linha da {linhaAtual}
                    </span>
                  )}
                  {sel.rmkt && (
                    <span
                      className={s.chip}
                      data-testid="chip-rmkt"
                      title="Veio do remarketing: respondeu a um disparo da base. A conversa da linha da Corretor está aqui em cima, marcada pela linha."
                    >
                      {sel.rmkt}
                    </span>
                  )}
                  {sel.transferido && (
                    <span className={s.chip} title="Este lead trocou de corretora nos últimos dias — o histórico anterior fica marcado pela linha antiga">
                      ↔️ transferido
                    </span>
                  )}
                  {crmLead && (
                    <span
                      className={s.chip}
                      title="Funil onde o lead está agora"
                    >
                      {crmLead.funil === "remarketing" ? "🌱 Remarketing" : "🎯 Vendas"}
                    </span>
                  )}
                  {crmLead ? (
                    // etapa/funil EDITÁVEIS: <select> agrupado (optgroup Vendas +
                    // Remarketing). value = "<funil>::<etapa>". Trocar de grupo
                    // move o lead pro outro funil (reusa /api/crm/mover). Backend
                    // barra quem não é dono/admin (403 → toast "não é seu lead").
                    // Move pro remarketing só troca a etapa (não dispara nutrição).
                    <select
                      className={s.chipSelect}
                      value={funilStageValue(crmLead.funil, crmLead.etapa)}
                      disabled={mudandoEtapa}
                      onChange={(e) => {
                        const { funil, etapa } = parseFunilStage(e.target.value);
                        mudarEtapa(funil, etapa);
                      }}
                      title="Mudar a etapa ou o funil do lead"
                    >
                      {/* sem etapa (lead não vinculado ou stage NULL) → placeholder;
                          etapa atual fora das listas conhecidas (órfã) → option extra */}
                      {!crmLead.etapa ? (
                        <option value={funilStageValue(crmLead.funil, "")}>— sem etapa —</option>
                      ) : (
                        !STAGES_CANONICOS.includes(crmLead.etapa) &&
                        !STAGES_REMARKETING.includes(crmLead.etapa) && (
                          <option value={funilStageValue(crmLead.funil, crmLead.etapa)}>
                            {crmLead.etapa}
                          </option>
                        )
                      )}
                      <optgroup label="Vendas">
                        {STAGES_CANONICOS.map((nome) => (
                          <option key={`p-${nome}`} value={funilStageValue("principal", nome)}>
                            {nome}
                          </option>
                        ))}
                      </optgroup>
                      {/* adoção (não vinculado) só entra no principal — as 8 canônicas */}
                      {crmLead.vinculado && (
                        <optgroup label="Remarketing">
                          {STAGES_REMARKETING.map((nome) => (
                            <option key={`r-${nome}`} value={funilStageValue("remarketing", nome)}>
                              {nome}
                            </option>
                          ))}
                        </optgroup>
                      )}
                    </select>
                  ) : (
                    etapaLabel(sel.stage) && <span className={s.chip}>{etapaLabel(sel.stage)}</span>
                  )}
                  {sel.dias != null && sel.dias > 0 && <span className={s.chipWarn}>{sel.dias}d parado</span>}
                  {infoLead.origem && <span className={s.chip} title="origem do lead">📍 {infoLead.origem}</span>}
                  {infoLead.criadoEm && <span className={s.chip} title="entrou no funil">entrou {new Date(infoLead.criadoEm).toLocaleDateString("pt-BR")}</span>}
                  {sel.tags.map((t) => <span key={t} className={s.chip}>{t}</span>)}
                </div>

                {/* Resumo por IA (05/10): só o que a pessoa escreveu na linha
                    da Corretor, cada fato com o trecho de onde saiu. */}
                {sel.resumoIa && (
                  <div className={s.resumoIa} data-testid="resumo-ia-aberto">
                    <span className={s.resumoIaRotulo}>Resumo IA</span>
                    {sel.resumoIa.status === "ok" ? (
                      <>
                        {sel.resumoIa.resumo && <span>{sel.resumoIa.resumo}</span>}
                        {(sel.resumoIa.fatos ?? []).filter((f) => f.fato).length > 0 && (
                          <ul className={s.resumoIaFatos}>
                            {(sel.resumoIa.fatos ?? []).filter((f) => f.fato).map((f, i) => (
                              <li key={i}>
                                {f.fato} <span className={s.resumoIaTrecho}>“{f.trecho}”</span>
                              </li>
                            ))}
                          </ul>
                        )}
                      </>
                    ) : (
                      <span className={s.resumoIaVazio}>
                        {sel.resumoIa.status === "pendente"
                          ? "saindo… o card já é seu, o resumo aparece quando ficar pronto"
                          : "sem resumo: a IA não conseguiu a tempo. A conversa está aqui embaixo."}
                      </span>
                    )}
                  </div>
                )}

                <div
                  className={s.thread}
                  ref={threadRef}
                  onScroll={(e) => {
                    const el = e.currentTarget;
                    atBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
                  }}
                >
                  {loadingThread && <div className={s.loading}>Carregando conversa…</div>}
                  {threadErro && <div className={s.loading}>{copiaThreadErro(threadErro)}</div>}
                  {!loadingThread && !threadErro && msgs.length === 0 && <div className={s.loading}>Sem mensagens nessa janela.</div>}
                  {(() => {
                    // timeline = mensagens + notas internas intercaladas por hora
                    // + divisor discreto quando a LINHA (telefone) muda no meio.
                    const itens: { at: number; el: ReactNode }[] = [];
                    for (const n of notas) {
                      itens.push({
                        at: n.at ? Date.parse(n.at) : 0,
                        el: (
                          <div key={`n-${n.id}`} className={s.notaCard}>
                            <div className={s.notaTxt}>📝 {n.texto}</div>
                            <div className={s.notaMeta}>
                              {n.autor ?? "equipe"}
                              {n.via_ia ? " · via observação" : ""} · {n.at ? fmtHora(n.at) : ""} · interna — o lead não vê
                            </div>
                          </div>
                        ),
                      });
                    }
                    let linhaAnt: string | undefined;
                    msgs.forEach((m, i) => {
                      const at = m.at ? Date.parse(m.at) : 0;
                      if (m.linha && linhaAnt && m.linha !== linhaAnt) {
                        const txt = m.linha === linhaAtual
                          ? `— agora pela linha da ${m.linha} —`
                          : `— conversa pela linha da ${m.linha} —`;
                        itens.push({ at: at - 1, el: <div key={`ld-${i}`} className={s.linhaDiv}><span>{txt}</span></div> });
                      }
                      if (m.linha) linhaAnt = m.linha;
                      const temTexto = !!m.text && (m.type === "text" || m.type === "template" || m.type === "deleted");
                      const audio = m.type === "audio" && m.id && sel;
                      const imagem = m.type === "image" && m.id && sel;
                      const documento = m.type === "document" && m.id && sel;
                      const video = m.type === "video" && m.id && sel;
                      itens.push({
                        at,
                        el: (
                          <div key={i} className={m.from === "lead" ? s.bLead : s.bCorretora}>
                            {audio ? (
                              <AudioMsg leadId={sel.kommoLeadId} msgId={m.id!} transcricao={m.transcricao} duracao={m.duracao} />
                            ) : imagem ? (
                              <ImagemMsg leadId={sel.kommoLeadId} msgId={m.id!} caption={legendaMidia(m.text)} />
                            ) : documento ? (
                              <DocumentoMsg
                                leadId={sel.kommoLeadId}
                                msgId={m.id!}
                                filename={m.filename}
                                mime={m.mime}
                                caption={legendaMidia(m.text)}
                              />
                            ) : video ? (
                              <VideoMsg leadId={sel.kommoLeadId} msgId={m.id!} caption={legendaMidia(m.text)} />
                            ) : temTexto ? (
                              m.text
                            ) : (
                              <span className={s.bMedia}>{MEDIA_LABEL[m.type] ?? "📩 mídia"}</span>
                            )}
                            <span className={s.at}>
                              {fmtHora(m.at)}
                              {m.from === "corretora" && <DeliveryBadge status={m.delivery_status} error={m.delivery_error} />}
                              {/* saiu daqui mas o WhatsApp não entregou (⚠):
                                  reenvia o MESMO texto pela linha da corretora.
                                  Só com a janela de 24h aberta — fora dela texto
                                  livre não passa, o caminho é o template. */}
                              {m.from === "corretora" && m.delivery_status === "failed" && temTexto && janelaAberta && (
                                <button
                                  onClick={() => enviarMsg(m.text!)}
                                  disabled={enviando}
                                  title="não foi entregue — tentar de novo"
                                  style={{
                                    marginLeft: 6, padding: "0 5px", borderRadius: 5,
                                    border: "1px solid #e7c9c4", background: "#fff",
                                    color: "#b3261e", fontSize: 12, lineHeight: "16px",
                                    cursor: "pointer", fontFamily: "inherit",
                                  }}
                                >
                                  ↻
                                </button>
                              )}
                            </span>
                          </div>
                        ),
                      });
                    });
                    itens.sort((a, b) => a.at - b.at);
                    return itens.map((x) => x.el);
                  })()}
                </div>

                {/* Barra "não enviou" — guarda o conteúdo do último envio que
                    falhou e devolve num clique. Só aparece na conversa onde a
                    falha aconteceu; some no primeiro envio que dá certo. */}
                {falha && falha.leadId === sel.kommoLeadId && (
                  <div
                    style={{
                      display: "flex", alignItems: "center", gap: 10,
                      padding: "8px 12px", borderTop: "1px solid #f0d6d2",
                      background: "#fdf1ef", color: "#8c2f26", fontSize: 13,
                    }}
                  >
                    <span style={{ flex: 1, minWidth: 0 }}>
                      ⚠ Não consegui enviar {rotuloFalha(falha)} ({falha.motivo})
                      {(falha.tipo === "texto" || falha.tipo === "template") && (
                        <span style={{ display: "block", color: "#6b6355", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                          “{falha.texto}”
                        </span>
                      )}
                    </span>
                    <button
                      onClick={tentarDeNovo}
                      disabled={enviando || enviandoMidia || reengando}
                      style={{
                        padding: "6px 12px", borderRadius: 6, border: "1px solid #b3261e",
                        background: "#b3261e", color: "#fff", fontSize: 13, cursor: "pointer",
                        fontFamily: "inherit", whiteSpace: "nowrap",
                      }}
                    >
                      {enviando || enviandoMidia || reengando ? "enviando…" : "↻ Tentar de novo"}
                    </button>
                    <button
                      onClick={descartarFalha}
                      title={falha.tipo === "midia" ? "Descartar" : "Descartar (o texto volta pro campo)"}
                      style={{
                        padding: "6px 9px", borderRadius: 6, border: "1px solid #e7c9c4",
                        background: "#fff", color: "#8c2f26", fontSize: 13, cursor: "pointer",
                        fontFamily: "inherit",
                      }}
                    >
                      ✕
                    </button>
                  </div>
                )}

                {/* Composer — depende da janela de 24h do WhatsApp. FAIL-CLOSED:
                    com threadErro (lead de outra corretora, sem thread, rede)
                    não mostra composer nenhum; janelaAberta só é true depois do
                    backend confirmar. */}
                {threadErro ? null : janelaAberta ? (
                  // JANELA ABERTA → texto livre normal + opção de template UTILITY
                  // sempre disponível (pedido Dona 23/07 — "coloca de volta").
                  <>
                  {/* fila de anexos escolhidos, ainda NÃO enviados. Some quando
                      a fila esvazia (no envio, no ✕ ou ao trocar de conversa). */}
                  {pendentes.length > 0 && (
                    <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "flex-end", padding: "10px 12px", borderTop: "1px solid #eee7db", background: "#fbf9f4" }}>
                      {pendentes.map((a) => (
                        <div key={a.id} style={{ position: "relative", width: 76 }}>
                          {a.tipo === "image" ? (
                            /* eslint-disable-next-line @next/next/no-img-element -- blob local, não passa pelo otimizador */
                            <img src={a.url} alt={a.file.name} style={{ width: 76, height: 76, objectFit: "cover", borderRadius: 8, border: "1px solid #e7e4dd", display: "block" }} />
                          ) : (
                            <div title={a.file.name} style={{ width: 76, height: 76, borderRadius: 8, border: "1px solid #e7e4dd", background: "#fff", display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", gap: 4, fontSize: 11, color: "#6b6355", padding: 4, textAlign: "center", overflow: "hidden" }}>
                              <span style={{ fontSize: 20 }}>📄</span>
                              <span style={{ lineHeight: 1.15, wordBreak: "break-all" }}>{a.file.name.slice(0, 18)}</span>
                            </div>
                          )}
                          <button
                            onClick={() => tirarPendente(a.id)}
                            title="Tirar da fila"
                            disabled={enviandoMidia}
                            style={{ position: "absolute", top: -7, right: -7, width: 22, height: 22, borderRadius: "50%", border: "1px solid #e7e4dd", background: "#fff", color: "#b3261e", fontSize: 13, lineHeight: 1, cursor: "pointer", fontFamily: "inherit", boxShadow: "0 1px 3px rgba(0,0,0,.12)" }}
                          >
                            ✕
                          </button>
                        </div>
                      ))}
                      <span style={{ fontSize: 12, color: "#8a857c", paddingBottom: 4 }}>
                        {pendentes.length} pra enviar · o texto abaixo vai como legenda {pendentes.length > 1 ? "do primeiro" : ""}
                      </span>
                    </div>
                  )}
                  <div className={s.notaBox}>
                    {/* 📎 anexo e 🎤 áudio — mesma linha do texto, mesma janela de 24h */}
                    <input
                      ref={arqRef}
                      type="file"
                      multiple
                      accept="image/*,application/pdf,.doc,.docx,.xls,.xlsx"
                      style={{ display: "none" }}
                      onChange={onPickFile}
                    />
                    <button
                      className={s.anexoBtn}
                      title="Escolher fotos, PDF ou documentos (até 16MB cada) — dá pra selecionar vários"
                      disabled={enviando || enviandoMidia || gravando}
                      onClick={() => arqRef.current?.click()}
                    >
                      {enviandoMidia && !gravando ? "⏳" : "📎"}
                    </button>
                    <button
                      className={`${s.anexoBtn} ${gravando ? s.anexoRec : ""}`}
                      title={gravando ? "Parar e enviar o áudio" : "Gravar áudio"}
                      disabled={enviando || enviandoMidia}
                      onClick={toggleGravar}
                    >
                      {gravando ? "⏹️" : "🎤"}
                    </button>
                    <textarea
                      value={msgTexto}
                      onChange={(e) => setMsgTexto(e.target.value)}
                      onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); pendentes.length ? enviarPendentes() : enviarMsg(); } }}
                      placeholder={gravando
                        ? "🔴 Gravando… toque em ⏹️ pra enviar"
                        : pendentes.length
                          ? "Legenda (opcional) — toque em Enviar pra mandar os anexos"
                          : "💬 Responder o lead… (vai pro WhatsApp dele) · Shift+Enter quebra linha"}
                      maxLength={4096}
                      rows={msgTexto.includes("\n") ? Math.min(8, msgTexto.split("\n").length + 1) : 1}
                      disabled={enviando || gravando}
                    />
                    {/* com fila, o Enviar manda os ANEXOS (texto vira legenda);
                        sem fila, segue o envio de texto de sempre. */}
                    <button
                      onClick={() => (pendentes.length ? enviarPendentes() : enviarMsg())}
                      disabled={enviando || gravando || enviandoMidia || (!pendentes.length && msgTexto.trim().length < 1)}
                    >
                      {enviandoMidia ? "enviando…" : enviando ? "enviando…" : pendentes.length ? `Enviar ${pendentes.length}` : "Enviar"}
                    </button>
                  </div>
                  <div style={{ padding: "6px 12px 10px", borderTop: "1px dashed #eee7db", background: "#fbf9f4" }}>
                    <label style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 12, color: "var(--ink-3, #8a857c)", cursor: "pointer" }}>
                      <input
                        type="checkbox"
                        checked={reengOn}
                        onChange={(e) => {
                          setReengOn(e.target.checked);
                          if (e.target.checked && !reengTexto.trim()) {
                            setReengTexto("Podemos retomar nosso atendimento por aqui? 😊");
                          }
                        }}
                      />
                      📨 Mandar template utility (reengajamento pela sua linha)
                    </label>
                    {reengOn && (
                      <div style={{ marginTop: 8 }}>
                        <div style={{ display: "flex", alignItems: "stretch", border: "1px solid #e7e4dd", borderRadius: 8, overflow: "hidden", background: "#f6f4ef" }}>
                          <span style={{ padding: "9px 10px", background: "#efe9dc", color: "#8a857c", fontSize: 12.5, whiteSpace: "nowrap", borderRight: "1px solid #e7e4dd", alignSelf: "center" }}>
                            {saudacaoTravada(sel.nome)}
                          </span>
                          <textarea
                            value={reengTexto}
                            onChange={(e) => setReengTexto(e.target.value)}
                            placeholder="a saudação já vai automática — escreve daqui pra frente"
                            rows={3}
                            maxLength={900}
                            style={{ flex: 1, minWidth: 0, border: "none", outline: "none", background: "transparent", padding: "9px 11px", fontSize: 13, color: "#15110d", resize: "vertical", font: "inherit" }}
                          />
                        </div>
                        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginTop: 6 }}>
                          <span style={{ fontSize: 11, color: "var(--ink-3, #a9a396)" }}>
                            enviada em UTILITY pela sua linha — a saudação vai automática.
                          </span>
                          <button
                            className={s.modalCriar}
                            onClick={() => reengajar()}
                            disabled={reengando || reengTexto.trim().length < 1}
                          >
                            {reengando ? "enviando…" : "Enviar template"}
                          </button>
                        </div>
                      </div>
                    )}
                  </div>
                  </>
                ) : (
                  // JANELA FECHADA → texto livre BLOQUEADO; só reengajamento (template
                  // UTILITY) ou abrir no WhatsApp do celular (sem trava de 24h).
                  <div style={{ padding: "10px 12px", borderTop: "1px solid #eee7db", background: "#fbf9f4" }}>
                    <div style={{ fontSize: 12.5, color: "#8a6d3b", marginBottom: 8, lineHeight: 1.4 }}>
                      ⏰ {primeiroNome(sel.nome)} não responde há {haQuanto(ultimaEntrada)} — janela de 24h fechada. Texto livre não entrega.
                    </div>

                    {/* 🅰️ Reengajamento (template UTILITY) */}
                    <label style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 12.5, color: "var(--ink-2, #444)", cursor: "pointer" }}>
                      <input
                        type="checkbox"
                        checked={reengOn}
                        onChange={(e) => {
                          setReengOn(e.target.checked);
                          if (e.target.checked && !reengTexto.trim()) {
                            setReengTexto("Podemos retomar nosso atendimento por aqui? 😊");
                          }
                        }}
                      />
                      🔄 Mandar reengajamento (template UTILITY pela sua linha)
                    </label>
                    {reengOn && (
                      <div style={{ marginTop: 8 }}>
                        <div style={{ display: "flex", alignItems: "stretch", border: "1px solid #e7e4dd", borderRadius: 8, overflow: "hidden", background: "#f6f4ef" }}>
                          {/* saudação TRAVADA (não editável) — bate com o template */}
                          <span style={{ padding: "9px 10px", background: "#efe9dc", color: "#8a857c", fontSize: 12.5, whiteSpace: "nowrap", borderRight: "1px solid #e7e4dd", alignSelf: "center" }}>
                            {saudacaoTravada(sel.nome)}
                          </span>
                          <textarea
                            value={reengTexto}
                            onChange={(e) => setReengTexto(e.target.value)}
                            placeholder="a saudação já vai automática — escreve daqui pra frente"
                            rows={3}
                            maxLength={900}
                            style={{ flex: 1, minWidth: 0, border: "none", outline: "none", background: "transparent", padding: "9px 11px", fontSize: 13, color: "#15110d", resize: "vertical", font: "inherit" }}
                          />
                        </div>
                        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginTop: 6 }}>
                          <span style={{ fontSize: 11, color: "var(--ink-3, #a9a396)" }}>
                            enviada em UTILITY pela sua linha — a saudação vai automática.
                          </span>
                          <button
                            className={s.modalCriar}
                            onClick={() => reengajar()}
                            disabled={reengando || reengTexto.trim().length < 1}
                          >
                            {reengando ? "enviando…" : "Enviar reengajamento"}
                          </button>
                        </div>
                      </div>
                    )}

                    {/* 🅲 Rascunho do 🏢 Empreendimentos com a janela FECHADA.
                        Texto livre não entrega e o template UTILITY não aceita
                        bloco multilinha com preço/link (vira falha dura + copy
                        promocional em UTILITY) — então o bloco fica read-only
                        pra copiar e colar no WhatsApp do celular. */}
                    {msgTexto.trim() && (
                      <div style={{ marginTop: 10, border: "1px solid #e7e4dd", borderRadius: 8, background: "#f6f4ef", padding: "8px 10px" }}>
                        <div style={{ fontSize: 11, color: "var(--ink-3, #a9a396)", marginBottom: 6 }}>
                          rascunho montado aqui (não dá pra enviar fora da janela) — copia e cola no WhatsApp
                        </div>
                        <pre style={{ margin: 0, whiteSpace: "pre-wrap", wordBreak: "break-word", fontSize: 12.5, color: "#15110d", font: "inherit", maxHeight: 180, overflowY: "auto" }}>
                          {msgTexto}
                        </pre>
                        <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
                          <button
                            className={s.tool}
                            onClick={async () => {
                              try {
                                await navigator.clipboard.writeText(msgTexto);
                                setCopiado(true);
                                setTimeout(() => setCopiado(false), 1600);
                              } catch {
                                setToast({ tipo: "erro", msg: "não copiei — seleciona o texto à mão" });
                              }
                            }}
                          >
                            {copiado ? "✓ copiado" : "📋 Copiar"}
                          </button>
                          <button className={s.tool} onClick={() => setMsgTexto("")}>🗑️ Limpar</button>
                        </div>
                      </div>
                    )}

                  </div>
                )}

                {tarefaOpen && (
                  <div className={s.modalOverlay} onClick={() => setTarefaOpen(false)}>
                    <div className={s.modalBox} onClick={(e) => e.stopPropagation()}>
                      <div className={s.modalTitulo}>{tarefaEditId != null ? "✏️ Editar tarefa" : "☑️ Criar tarefa"} · {sel.nome}</div>
                      <textarea
                        className={s.modalTexto}
                        value={tarefaTexto}
                        onChange={(e) => setTarefaTexto(e.target.value)}
                        placeholder="O que precisa ser feito? (ex: ligar de novo amanhã de manhã)"
                        rows={3}
                        autoFocus
                      />
                      <div className={s.modalRow}>
                        <label>quando</label>
                        <input type="date" value={tarefaData} onChange={(e) => setTarefaData(e.target.value)} />
                        <input type="time" value={tarefaHora} onChange={(e) => setTarefaHora(e.target.value)} title="horário (opcional)" />
                        <span style={{ fontSize: 11, color: "var(--ink-3)" }}>hora opcional</span>
                      </div>

                      {/* Anotações — campo próprio da tarefa (não é nota do lead) */}
                      <textarea
                        className={s.modalTexto}
                        value={tarefaAnot}
                        onChange={(e) => setTarefaAnot(e.target.value)}
                        placeholder="Anotações (opcional) — contexto pra você, não vai pro lead"
                        rows={2}
                        style={{ marginTop: 8 }}
                      />

                      {/* Mensagem planejada — checkbox; só quando marcado abre a caixa */}
                      <label style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 10, fontSize: 12.5, color: "var(--ink-2, #444)", cursor: "pointer" }}>
                        <input
                          type="checkbox"
                          checked={tarefaMsgOn}
                          onChange={(e) => setTarefaMsgOn(e.target.checked)}
                        />
                        📤 Mensagem planejada — dispara sozinha na data/hora
                      </label>
                      {tarefaMsgOn && (
                        <div style={{ marginTop: 8 }}>
                          <div style={{ display: "flex", alignItems: "stretch", border: "1px solid #e7e4dd", borderRadius: 8, overflow: "hidden", background: "#f6f4ef" }}>
                            {/* saudação TRAVADA (não editável) — bate com o template */}
                            <span style={{ padding: "9px 10px", background: "#efe9dc", color: "#8a857c", fontSize: 12.5, whiteSpace: "nowrap", borderRight: "1px solid #e7e4dd", alignSelf: "center" }}>
                              {saudacaoTravada(sel.nome)}
                            </span>
                            <textarea
                              value={tarefaMsg}
                              onChange={(e) => setTarefaMsg(e.target.value)}
                              placeholder="a saudação já vai automática — escreve daqui pra frente"
                              rows={3}
                              style={{ flex: 1, minWidth: 0, border: "none", outline: "none", background: "transparent", padding: "9px 11px", fontSize: 13, color: "#15110d", resize: "vertical", font: "inherit" }}
                            />
                          </div>
                          <div style={{ fontSize: 11, color: "var(--ink-3, #a9a396)", marginTop: 4 }}>
                            enviada em UTILITY pela sua linha, fora da janela de 24h — a saudação vai automática.
                          </div>
                        </div>
                      )}

                      {tarefaErro && <div className={s.modalErro}>não criou ({tarefaErro})</div>}
                      <div className={s.modalAcoes}>
                        <button className={s.tool} onClick={() => setTarefaOpen(false)}>cancelar</button>
                        <button
                          className={s.modalCriar}
                          onClick={criarTarefa}
                          disabled={salvandoTarefa || tarefaTexto.trim().length < 2 || !tarefaData}
                        >
                          {salvandoTarefa ? (tarefaEditId != null ? "salvando…" : "criando…") : (tarefaEditId != null ? "Salvar" : "Criar tarefa")}
                        </button>
                      </div>
                    </div>
                  </div>
                )}

                {notaOpen && (
                  <div className={s.modalOverlay} onClick={() => setNotaOpen(false)}>
                    <div className={s.modalBox} onClick={(e) => e.stopPropagation()}>
                      <div className={s.modalTitulo}>📝 Anotação interna · {sel.nome}</div>
                      <textarea
                        className={s.modalTexto}
                        value={notaTexto}
                        onChange={(e) => setNotaTexto(e.target.value)}
                        onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) salvarNota(); }}
                        placeholder="Anotação interna — só a equipe vê, não vai pro lead… (ex: liguei, não atendeu)"
                        rows={4}
                        maxLength={2000}
                        autoFocus
                      />
                      <div className={s.modalAcoes}>
                        <button className={s.tool} onClick={() => setNotaOpen(false)}>cancelar</button>
                        <button
                          className={s.modalCriar}
                          onClick={salvarNota}
                          disabled={salvandoNota || notaTexto.trim().length < 2}
                        >
                          {salvandoNota ? "salvando…" : "Anotar"}
                        </button>
                      </div>
                    </div>
                  </div>
                )}

                {nomeOpen && (
                  <div className={s.modalOverlay} onClick={() => setNomeOpen(false)}>
                    <div className={s.modalBox} onClick={(e) => e.stopPropagation()}>
                      <div className={s.modalTitulo}>✏️ Editar lead</div>
                      <label style={{ fontSize: 12, opacity: 0.7, marginBottom: 4 }}>Nome</label>
                      <input
                        className={s.modalTexto}
                        value={nomeTexto}
                        onChange={(e) => setNomeTexto(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") salvarLead();
                          if (e.key === "Escape") setNomeOpen(false);
                        }}
                        placeholder="Nome real do lead (ex: João Silva)"
                        maxLength={120}
                        autoFocus
                        style={{ minHeight: 0, height: 40 }}
                      />
                      <label style={{ fontSize: 12, opacity: 0.7, margin: "10px 0 4px" }}>Telefone</label>
                      <input
                        className={s.modalTexto}
                        value={telefoneTexto}
                        onChange={(e) => setTelefoneTexto(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") salvarLead();
                          if (e.key === "Escape") setNomeOpen(false);
                        }}
                        placeholder="+5500000000000"
                        maxLength={40}
                        style={{ minHeight: 0, height: 40 }}
                      />
                      <div style={{ fontSize: 11, opacity: 0.6, marginTop: 6, lineHeight: 1.35 }}>
                        ⚠️ Corrigir o telefone reconecta a conversa certa do WhatsApp.
                        Se o número novo ainda não tem histórico, a conversa vem vazia
                        até a 1ª mensagem.
                      </div>
                      <div className={s.modalAcoes}>
                        <button className={s.tool} onClick={() => setNomeOpen(false)}>cancelar</button>
                        <button
                          className={s.modalCriar}
                          onClick={salvarLead}
                          disabled={salvandoNome || nomeTexto.trim().length < 2}
                        >
                          {salvandoNome ? "salvando…" : "Salvar"}
                        </button>
                      </div>
                    </div>
                  </div>
                )}
                {transfOpen && (() => {
                  // dona atual (nome do card ou do backend) → não aparece como destino
                  const donoCid = corretoraIdDeNome(viaCorretora || sel.corretora);
                  const destinos = CORRETORAS.filter((c) => CORRETORA_ID[c] !== donoCid);
                  return (
                    <div className={s.modalOverlay} onClick={() => setTransfOpen(false)}>
                      <div className={s.modalBox} onClick={(e) => e.stopPropagation()}>
                        <div className={s.modalTitulo}>↔️ Transferir lead · {sel.nome}</div>
                        <div style={{ fontSize: 12.5, color: "var(--ink-2, #444)", marginBottom: 10, lineHeight: 1.45 }}>
                          O lead muda de dona: sai da lista da corretora atual e aparece pra
                          quem recebe (com selo ↔️). O histórico da conversa fica, marcado
                          pela linha por onde cada trecho rolou. Nenhuma mensagem é enviada.
                        </div>
                        <label style={{ fontSize: 12, opacity: 0.7, marginBottom: 4, display: "block" }}>Transferir para</label>
                        <select
                          className={s.chipSelect}
                          value={transfDestino ?? ""}
                          onChange={(e) => setTransfDestino(e.target.value ? Number(e.target.value) : null)}
                          style={{ width: "100%", marginBottom: 10 }}
                          autoFocus
                        >
                          <option value="">— escolha a corretora —</option>
                          {destinos.map((c) => (
                            <option key={c} value={CORRETORA_ID[c]}>{c} ({abrev(c)})</option>
                          ))}
                        </select>
                        <textarea
                          className={s.modalTexto}
                          value={transfMotivo}
                          onChange={(e) => setTransfMotivo(e.target.value)}
                          placeholder="Motivo (opcional) — ex: cliente pediu, férias, redistribuição…"
                          rows={2}
                          maxLength={500}
                        />
                        {!viewer?.isAdmin && (
                          <div style={{ fontSize: 11.5, color: "#8a6d3b", marginTop: 8, lineHeight: 1.4 }}>
                            ⚠️ Depois de confirmar, este lead sai da SUA lista — só a nova
                            dona (ou a Dona) consegue devolver.
                          </div>
                        )}
                        <div className={s.modalAcoes}>
                          <button className={s.tool} onClick={() => setTransfOpen(false)}>cancelar</button>
                          <button
                            className={s.modalCriar}
                            onClick={transferirLead}
                            disabled={transferindo || transfDestino == null}
                          >
                            {transferindo ? "transferindo…" : "Confirmar transferência"}
                          </button>
                        </div>
                      </div>
                    </div>
                  );
                })()}
              </>
            ) : (
              <div className={s.vazio}>
                <div className={s.vazioIc}>💬</div>
                <div>Selecione uma conversa na lista<br />pra ler o atendimento e o coaching</div>
              </div>
            )}
          </div>

          {/* direita: painel Copiloto */}
          {sel && painelOn && (
            <div className={s.side}>
              <div className={s.sideHead}>
                🤖 Copiloto <span>raio-X do lead pela IA</span>
                {/* fecha o painel no celular, onde ele cobre a conversa */}
                <button
                  className={s.sideFechar}
                  onClick={() => setPainelOn(false)}
                  aria-label="Fechar o painel do Copiloto"
                >✕</button>
              </div>
              <div className={s.sideBody}>
                {/* Tarefa de LIGAÇÃO da cadência devida pra este lead (sistema
                    novo — D1/D5/D9). Fica no topo: é a ação concreta do momento.
                    A Pessoa12 vê o que é e marca "✓ liguei" (avança o follow-up). */}
                {tarefasLigacao.length > 0 && (
                  <div style={{ marginBottom: 12, padding: "10px 12px", background: "#fbf6e8", border: "1px solid #ecdcae", borderLeft: "3px solid #c79a3a", borderRadius: 8 }}>
                    <div style={{ fontSize: 11, fontWeight: 700, color: "#8a6d1f", textTransform: "uppercase", letterSpacing: 0.3, marginBottom: 6 }}>🔔 Tarefa de ligação (follow-up)</div>
                    {tarefasLigacao.map((t) => (
                      <div key={t.cadencia_id} style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8, marginTop: 4 }}>
                        <span style={{ fontSize: 13, color: "#15110d", fontWeight: 600 }}>
                          {t.texto}{t.atrasada ? " · atrasada" : ""}
                        </span>
                        <button
                          onClick={() => marcarLiguei(t)}
                          disabled={ligando}
                          title="Registrar que você ligou — avança o follow-up e tira da fila"
                          style={{ cursor: ligando ? "wait" : "pointer", border: "1.5px solid #d8c48a", background: "#f6edd6", color: "#8a6d1f", borderRadius: 8, padding: "5px 11px", fontSize: 12.5, fontWeight: 700, fontFamily: "inherit", whiteSpace: "nowrap", opacity: ligando ? 0.6 : 1 }}
                        >
                          {ligando ? "…" : "✓ liguei"}
                        </button>
                      </div>
                    ))}
                  </div>
                )}
                {/* Tarefas MANUAIS abertas deste cliente (tarefas_corretora).
                    Distinto do 🔔 acima (cadência/ligação): aqui é o que a
                    corretora criou à mão — editável (✏️) e concluível (✓) sem
                    sair do chat. Some da lista ao concluir. */}
                {tarefasLead.length > 0 && (
                  <div style={{ marginBottom: 12, padding: "10px 12px", background: "#f2f6fb", border: "1px solid #cfe0f0", borderLeft: "3px solid #3a7bc7", borderRadius: 8 }}>
                    <div style={{ fontSize: 11, fontWeight: 700, color: "#2c5a8f", textTransform: "uppercase", letterSpacing: 0.3, marginBottom: 6 }}>📋 Tarefas deste cliente</div>
                    {tarefasLead.map((t) => (
                      <div key={t.id} style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8, marginTop: 6 }}>
                        <span style={{ fontSize: 13, color: "#15110d", fontWeight: 600, minWidth: 0 }}>
                          {t.texto}
                          <span style={{ display: "block", fontSize: 11, fontWeight: 500, color: t.atrasada ? "#b23b3b" : "#6b7785", marginTop: 1 }}>
                            {new Date(`${t.data}T00:00:00`).toLocaleDateString("pt-BR", { day: "2-digit", month: "2-digit" })}
                            {t.hora ? ` · ${t.hora}` : ""}
                            {t.atrasada ? " · atrasada" : ""}
                            {t.tem_msg_planejada ? " · 📤" : ""}
                          </span>
                        </span>
                        <span style={{ display: "flex", gap: 6, whiteSpace: "nowrap" }}>
                          <button
                            onClick={() => abrirEditarTarefa(t)}
                            title="Editar esta tarefa"
                            style={{ cursor: "pointer", border: "1.5px solid #c3d6ea", background: "#e8f0f9", color: "#2c5a8f", borderRadius: 8, padding: "5px 9px", fontSize: 12.5, fontWeight: 700, fontFamily: "inherit" }}
                          >✏️</button>
                          <button
                            onClick={() => concluirTarefaLead(t)}
                            disabled={concluindoTarefa != null}
                            title="Concluir — tira da lista"
                            style={{ cursor: concluindoTarefa != null ? "wait" : "pointer", border: "1.5px solid #a9cfa9", background: "#e7f3e7", color: "#2f7a2f", borderRadius: 8, padding: "5px 9px", fontSize: 12.5, fontWeight: 700, fontFamily: "inherit", opacity: concluindoTarefa === t.id ? 0.6 : 1 }}
                          >{concluindoTarefa === t.id ? "…" : "✓"}</button>
                        </span>
                      </div>
                    ))}
                  </div>
                )}
                {!raiox && !coaching && tarefasLigacao.length === 0 && tarefasLead.length === 0 && (
                  <div className={s.sideTxt} style={{ color: "var(--ink-3)" }}>
                    O Copiloto ainda não analisou esse lead. A análise roda no fim do dia e aparece aqui.
                  </div>
                )}
                <button
                  onClick={reanalisarLead}
                  disabled={reanalisando}
                  title="Refaz a análise do Copiloto agora, com a conversa mais recente (~1 min)"
                  style={{ cursor: reanalisando ? "wait" : "pointer", border: "1.5px solid #c3d6ea", background: "#e8f0f9", color: "#2c5a8f", borderRadius: 8, padding: "5px 10px", fontSize: 12.5, fontWeight: 700, fontFamily: "inherit", margin: "6px 0 8px", opacity: reanalisando ? 0.6 : 1 }}
                >{reanalisando ? "Reanalisando… (~1 min)" : raiox ? "🔁 Reanalisar" : "🔁 Analisar agora"}</button>
                {reanaliseErro && !reanalisando && (
                  <div className={s.sideTxt} style={{ color: "#a3352c", marginBottom: 6 }}>{reanaliseErro}</div>
                )}
                {raiox && (
                  <>
                    {raiox.temperatura && <TempPill t={raiox.temperatura} />}
                    {raiox.resumo && (<><div className={s.sideSec}>Resumo da conversa</div><div className={s.sideTxt}>{raiox.resumo}</div></>)}
                    {raiox.gap && (<><div className={s.sideSec}>O que falta</div><div className={s.sideTxt}>{raiox.gap}</div></>)}
                    {raiox.abordagem && (<><div className={s.sideSec}>Abordagem sugerida</div><div className={s.sideSug}>{raiox.abordagem}</div></>)}
                    {(raiox.tarefas_atrasadas?.length ?? 0) > 0 && (
                      <>
                        <div className={s.sideSec} title="Tarefas herdadas da base antiga, importadas na migração. As tarefas de LIGAÇÃO do follow-up ficam no bloco 🔔 acima.">Tarefas atrasadas (base antiga)</div>
                        <div className={s.sideTxt}>{raiox.tarefas_atrasadas!.map((t, i) => <div key={i}>⏰ {t}</div>)}</div>
                      </>
                    )}
                    {raiox.analyzed_at && (
                      <div className={s.sideSec}>análise de {new Date(raiox.analyzed_at).toLocaleString("pt-BR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })}</div>
                    )}
                  </>
                )}
                {coaching && (coaching.observacao || coaching.sugestao) && (
                  <>
                    <div className={s.sideSec}>Coaching{coaching.categoria ? ` · ${coaching.categoria}` : ""}</div>
                    {coaching.trecho && <div className={s.sideTxt} style={{ fontStyle: "italic" }}>“{coaching.trecho}”</div>}
                    {coaching.observacao && <div className={s.sideTxt} style={{ marginTop: 6 }}>{coaching.observacao}</div>}
                    {coaching.sugestao && <div className={s.sideSug} style={{ marginTop: 8 }}>{coaching.sugestao}</div>}
                  </>
                )}
              </div>
            </div>
          )}
        </div>

      {toast && (
        <div
          role="status"
          style={{
            position: "fixed", bottom: 20, left: "50%", transform: "translateX(-50%)",
            zIndex: 60, padding: "10px 16px", borderRadius: 10, fontSize: 13, fontWeight: 600,
            boxShadow: "0 6px 20px rgba(0,0,0,.18)",
            background: toast.tipo === "ok" ? "#1f7a4d" : "#a3352c", color: "#fff",
          }}
        >
          {toast.msg}
        </div>
      )}
    </div>
  );
}
