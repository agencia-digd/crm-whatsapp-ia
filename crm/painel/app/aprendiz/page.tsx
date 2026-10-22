"use client";

// 🎓 APRENDIZ — modo sombra da IA.
// 1) Placar de autonomia (copiloto /aprendiz/placar): quanto a IA já
//    responderia sozinha / rascunharia / chapessoa a corretora.
// 2) "Bateu ou não" (copiloto /aprendiz/bateu/*): compara o rascunho da IA
//    com a resposta REAL que a corretora mandou na sequência — similaridade
//    por heurística local no backend (zero LLM, nada é enviado).
// 3) Placar honesto: "acertou" vem da corretora/Dona (botões em cada par) ou do juiz
//    (claude -p compara a AÇÃO do rascunho com a da corretora). Trigrama é só info.
// 4) "O que aprendeu / o que mudou" (copiloto /aprendiz/aprendizado).
// Escopo/privacidade quem resolve é o copiloto via X-Painel-User:
// corretora vê só o dela; admin vê todas.

import { useEffect, useState } from "react";
import "./aprendiz.css";

const num = (v: number | null | undefined) => (v == null ? "—" : v.toLocaleString("pt-BR"));
const pct = (v: number | null | undefined) => (v == null ? "—" : `${v}%`);

const SELO: Record<string, string> = {
  bateu: "✓ bateu",
  parcial: "≈ parcial",
  nao_bateu: "✕ não bateu",
  sem_resposta_real: "sem resposta real",
  sem_comparacao: "sem comparação",
  pendente: "sem avaliação",
};

const FILTROS_VEREDITO = [
  { v: "", label: "Todos" },
  { v: "pendente", label: "Falta avaliar" },
  { v: "bateu", label: "Acertou" },
  { v: "parcial", label: "Parcial" },
  { v: "nao_bateu", label: "Errou" },
  { v: "sem_resposta_real", label: "Sem resposta" },
];

const PAGE = 10;

function fmtData(iso: string | null | undefined): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString("pt-BR", {
      day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit",
    });
  } catch {
    return "—";
  }
}

// ── Placar de autonomia (dado já existente do aprendiz) ─────────────────────
function PlacarAutonomia({ p }: { p: any }) {
  const b = p?.buckets ?? {};
  const soz = b.responderia_sozinha?.n ?? 0;
  const ras = b.rascunharia?.n ?? 0;
  const cha = b.chamaria_voce?.n ?? 0;
  const total = p?.total ?? 0;
  const w = (n: number) => (total ? `${(100 * n) / total}%` : "0%");
  return (
    <div className="ap-secao">
      <div className="ap-secao-sub">modo sombra · últimos {p?.dias ?? 30} dias · {num(total)} leads avaliados</div>
      <h3>Autonomia da IA: {pct(p?.autonomia_pct)}</h3>
      <div className="ap-barra">
        <div className="ap-seg-sozinha" style={{ width: w(soz) }} />
        <div className="ap-seg-rascunho" style={{ width: w(ras) }} />
        <div className="ap-seg-chapessoa" style={{ width: w(cha) }} />
      </div>
      <div className="ap-legenda">
        <span><span className="dot" style={{ background: "#1a7f37" }} />Responderia sozinha <b>{num(soz)}</b></span>
        <span><span className="dot" style={{ background: "#d9a514" }} />Rascunharia <b>{num(ras)}</b></span>
        <span><span className="dot" style={{ background: "#c0392b" }} />Chapessoa você <b>{num(cha)}</b></span>
      </div>
    </div>
  );
}

// ── Placar bateu ou não ──────────────────────────────────────────────────────
function PlacarBateu({ p }: { p: any }) {
  const v = p?.vereditos ?? {};
  const comp = p?.comparaveis ?? 0;
  const w = (n: number) => (comp ? `${(100 * (n ?? 0)) / comp}%` : "0%");
  return (
    <>
      <div className="ap-kpis">
        {/* A métrica que MANDA. "Bateu por texto" media se a IA escreveu as
            mesmas palavras — e dá ~0%, porque duas respostas certas quase nunca
            têm o mesmo texto. A FORMA mede se ela faria a mesma COISA
            (perguntar, prometer, encerrar), que é o que decide se ela pode ser
            solta. Por isso ela é a primeira, e a de texto virou secundária. */}
        {/* Placar honesto: conta só o que alguém (corretora/Dona) ou o juiz avaliou
            por intenção. Sem avaliação não inventa percentual. */}
        <div className="ap-kpi accent">
          <div className="ap-kpi-lbl">Acertou</div>
          <div className="ap-kpi-val">{pct(p?.acertou_pct)}</div>
          <div className="ap-kpi-sub">
            {num(p?.avaliados)} avaliados ({num(p?.fonte_avaliacao?.humano)} pela equipe,{" "}
            {num(p?.fonte_avaliacao?.ia)} pelo juiz) · {num(p?.pendentes_avaliacao)} esperando
          </div>
        </div>
        <div className="ap-kpi">
          <div className="ap-kpi-lbl">Faria a mesma coisa</div>
          <div className="ap-kpi-val">{pct(p?.mesma_forma_pct)}</div>
          <div className="ap-kpi-sub">
            {num(p?.com_forma)} pares comparados por intenção
          </div>
        </div>
        {/* O vício que a Dona apontou: encerrar conversa viva. A corretora faz
            isso ~3% das vezes; a IA fazia 20%. É o número que a regra do
            próximo passo tem que derrubar. */}
        <div className="ap-kpi">
          <div className="ap-kpi-lbl">Encerra conversa</div>
          <div className="ap-kpi-val">{pct(p?.despedidas_ia_pct)}</div>
          <div className="ap-kpi-sub">
            quanto dos rascunhos se despede — quanto menor, melhor
          </div>
        </div>
        <div className="ap-kpi">
          <div className="ap-kpi-lbl">Bateu no texto</div>
          <div className="ap-kpi-val">{pct(p?.bateu_pct)}</div>
          <div className="ap-kpi-sub">
            escreveu igual · {num(v.bateu)} de {num(comp)} — quase sempre baixo, e tudo bem
          </div>
        </div>
        <div className="ap-kpi">
          <div className="ap-kpi-lbl">Rascunhos</div>
          <div className="ap-kpi-val">{num(p?.total_rascunhos)}</div>
          <div className="ap-kpi-sub">{num(v.sem_resposta_real)} sem resposta real ainda</div>
        </div>
      </div>

      {/* O retrato lado a lado é onde o problema fica óbvio: a IA se despede
          muito mais e promete muito mais que a corretora. */}
      {p?.forma_ia && p?.forma_corretora && (
        <div className="ap-formas">
          <div className="ap-formas-tit">O que cada uma faz no fim da mensagem</div>
          <table className="ap-formas-tab">
            <thead>
              <tr><th /><th>a IA</th><th>a corretora</th></tr>
            </thead>
            <tbody>
              {[
                ["pergunta", "pergunta", "devolve a bola pro lead"],
                ["promessa", "promete", "assume uma tarefa"],
                ["despedida", "se despede", "encerra a conversa"],
                ["afirmacao", "só informa", "não pede nada"],
              ].map(([k, rot, exp]) => (
                <tr key={k}>
                  <td>
                    <b>{rot}</b>
                    <span className="ap-formas-exp">{exp}</span>
                  </td>
                  <td>{num(p.forma_ia?.[k])}</td>
                  <td>{num(p.forma_corretora?.[k])}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div style={{ display: "none" }}>
      </div>
      {comp > 0 && (
        <div className="ap-secao">
          <div className="ap-secao-sub">rascunho da IA × resposta real · {num(comp)} pares comparáveis</div>
          <div className="ap-barra">
            <div className="ap-seg-bateu" style={{ width: w(v.bateu) }} />
            <div className="ap-seg-parcial" style={{ width: w(v.parcial) }} />
            <div className="ap-seg-nao" style={{ width: w(v.nao_bateu) }} />
          </div>
          <div className="ap-legenda">
            <span><span className="dot" style={{ background: "#1a7f37" }} />Bateu <b>{num(v.bateu)}</b></span>
            <span><span className="dot" style={{ background: "#d9a514" }} />Parcial <b>{num(v.parcial)}</b></span>
            <span><span className="dot" style={{ background: "#c0392b" }} />Não bateu <b>{num(v.nao_bateu)}</b></span>
            <span><span className="dot" style={{ background: "#cfcabf" }} />Sem comparação <b>{num(v.sem_comparacao)}</b></span>
          </div>
        </div>
      )}
    </>
  );
}

// ── Par lado a lado ──────────────────────────────────────────────────────────
function Par({ p, onMarcar }: { p: any; onMarcar: (id: number, acerto: boolean | null) => void }) {
  const final = p.veredito_final ?? "pendente";
  const fonte = p.fonte === "humano" ? "marcado por " + (p.avaliado_por ?? "equipe") : p.fonte === "ia" ? "juiz" : null;
  return (
    <div className="ap-par">
      <div className="ap-par-head">
        <span className="ap-par-lead">{p.lead_nome || p.lead_telefone || "Lead"}</span>
        <span className="ap-par-meta">
          {p.corretora_nome ?? ""} · rascunho {fmtData(p.rascunho_at)}
          {p.resposta_real_at ? ` · real ${fmtData(p.resposta_real_at)}` : ""}
        </span>
        <span className={`ap-selo ${final === "pendente" ? "sem_comparacao" : final}`}>
          {SELO[final] ?? final}
          {fonte ? ` · ${fonte}` : ""}
        </span>
      </div>
      {p.ultima_msg_lead && <div className="ap-par-msg-lead">Lead: “{p.ultima_msg_lead}”</div>}
      <div className="ap-par-cols">
        <div className="ap-lado ia">
          <div className="ap-lado-lbl">Rascunho da IA · {p.bucket === "responderia_sozinha" ? "responderia sozinha" : "rascunharia"} ({p.confianca}%)</div>
          <div className="ap-lado-txt">{p.rascunho}</div>
        </div>
        <div className="ap-lado">
          <div className="ap-lado-lbl">Resposta real da corretora</div>
          {p.resposta_real ? (
            <div className="ap-lado-txt">{p.resposta_real}</div>
          ) : (
            <div className="ap-lado-txt vazio">
              {p.veredito === "sem_comparacao"
                ? "respondeu sem texto comparável (áudio/mídia)"
                : "ainda não respondeu na janela de comparação"}
            </div>
          )}
        </div>
      </div>
      {p.juiz_motivo && p.fonte !== "humano" && (
        <div className="ap-juiz">Juiz: {p.juiz_motivo}</div>
      )}
      <div className="ap-marcar">
        <span className="ap-marcar-lbl">O rascunho serviria?</span>
        <button
          className={`ap-btn ok ${p.acerto === true ? "on" : ""}`}
          onClick={() => onMarcar(p.decisao_id, p.acerto === true ? null : true)}
        >
          ✓ acertou
        </button>
        <button
          className={`ap-btn no ${p.acerto === false ? "on" : ""}`}
          onClick={() => onMarcar(p.decisao_id, p.acerto === false ? null : false)}
        >
          ✕ errou
        </button>
      </div>
    </div>
  );
}

// ── O que já aprendeu / o que mudou ──────────────────────────────────────────
function Aprendizado({ a }: { a: any }) {
  const evo: any[] = a?.evolucao ?? [];
  const mud: any[] = a?.mudancas ?? [];
  const cor: any[] = a?.correcoes ?? [];
  return (
    <div className="ap-secao">
      <div className="ap-secao-sub">últimas {a?.semanas ?? 8} semanas</div>
      <h3>O que o Aprendiz já aprendeu e o que mudou</h3>

      <div className="ap-sub-tit">O que mudou nele</div>
      {mud.map((m) => (
        <div className="ap-mud" key={m.data + m.titulo}>
          <div className="ap-mud-data">{m.data.split("-").reverse().join("/")}</div>
          <div>
            <b>{m.titulo}</b>
            <div className="ap-mud-txt">{m.descricao}</div>
            {m.efeito && (
              <div className="ap-mud-efeito">
                {m.efeito.metrica}: {pct(m.efeito.antes_pct)} antes ({num(m.efeito.antes_n)} rascunhos) →{" "}
                {pct(m.efeito.depois_pct)} depois ({num(m.efeito.depois_n)})
              </div>
            )}
          </div>
        </div>
      ))}

      <div className="ap-sub-tit">Semana a semana</div>
      <div className="ap-tab-wrap">
        <table className="ap-formas-tab ap-evo">
          <thead>
            <tr><th>semana de</th><th>decisões</th><th>sozinha</th><th>acertou</th><th>despede</th></tr>
          </thead>
          <tbody>
            {evo.map((w) => (
              <tr key={w.semana}>
                <td>{w.semana.split("-").reverse().slice(0, 2).join("/")}</td>
                <td>{num(w.decisoes)}</td>
                <td>{pct(w.autonomia_pct)}</td>
                <td>{w.avaliados ? `${pct(w.acertou_pct)} (${num(w.avaliados)})` : "—"}</td>
                <td>{pct(w.despedida_pct)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="ap-sub-tit">Últimas correções</div>
      {cor.length === 0 && (
        <div className="ap-mud-txt">Nenhum rascunho marcado como errado ainda. Os botões “errou” em cada par alimentam esta lista.</div>
      )}
      {cor.map((c) => (
        <div className="ap-cor" key={c.decisao_id}>
          <b>{c.lead_nome || "Lead"}</b> · {c.fonte === "humano" ? "marcado pela equipe" : "juiz"}
          <div className="ap-mud-txt">“{c.rascunho}”</div>
          {c.motivo && <div className="ap-mud-efeito">{c.motivo}</div>}
        </div>
      ))}
      {a?.correcoes_alimentam_prompt === false && (
        <div className="ap-aviso">
          Ainda não: as correções acima aparecem aqui, mas não voltam sozinhas para o prompt da IA.
          Hoje o aprendizado vem das regras do prompt, ajustadas à mão.
        </div>
      )}
    </div>
  );
}

export default function AprendizPage() {
  const [dias, setDias] = useState(30);
  const [autonomia, setAutonomia] = useState<any>(null);
  const [bateu, setBateu] = useState<any>(null);
  const [aprendizado, setAprendizado] = useState<any>(null);
  const [erro, setErro] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const [veredito, setVeredito] = useState("");
  const [pares, setPares] = useState<any[]>([]);
  const [totalPares, setTotalPares] = useState(0);
  const [loadingPares, setLoadingPares] = useState(false);

  function carregarPlacares(d: number) {
    setLoading(true);
    setErro(null);
    Promise.all([
      fetch(`/api/aprendiz/placar?dias=${d}`, { cache: "no-store" }).then((r) => r.json()),
      fetch(`/api/aprendiz/bateu/placar?dias=${d}`, { cache: "no-store" }).then((r) => r.json()),
      fetch(`/api/aprendiz/aprendizado`, { cache: "no-store" }).then((r) => r.json()).catch(() => null),
    ])
      .then(([a, b, ap]) => {
        setAprendizado(ap?.error ? null : ap);
        if (a?.error || b?.error) setErro(a?.error || b?.error);
        setAutonomia(a?.error ? null : a);
        setBateu(b?.error ? null : b);
      })
      .catch(() => setErro("copiloto_indisponivel"))
      .finally(() => setLoading(false));
  }

  function carregarPares(d: number, v: string, offset: number, substituir: boolean) {
    setLoadingPares(true);
    const qs = new URLSearchParams({ dias: String(d), limit: String(PAGE), offset: String(offset) });
    if (v) qs.set("final", v);
    fetch(`/api/aprendiz/bateu/pares?${qs}`, { cache: "no-store" })
      .then((r) => r.json())
      .then((data) => {
        if (data?.error) return;
        setTotalPares(data.total ?? 0);
        setPares((prev) => (substituir ? data.itens ?? [] : [...prev, ...(data.itens ?? [])]));
      })
      .catch(() => {})
      .finally(() => setLoadingPares(false));
  }

  useEffect(() => {
    carregarPlacares(dias);
    carregarPares(dias, veredito, 0, true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Marca acertou/errou e atualiza a tela na hora (otimista); em erro, recarrega.
  function marcar(id: number, acerto: boolean | null) {
    setPares((prev) =>
      prev.map((x) =>
        x.decisao_id === id
          ? { ...x, acerto, fonte: acerto === null ? (x.juiz_veredito ? "ia" : null) : "humano",
              veredito_final: acerto === null ? (x.juiz_veredito ?? "pendente") : acerto ? "bateu" : "nao_bateu" }
          : x,
      ),
    );
    fetch("/api/aprendiz/acerto", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ decisao_id: id, acerto }),
    })
      .then((r) => {
        if (!r.ok) throw new Error("falhou");
        carregarPlacares(dias);
      })
      .catch(() => carregarPares(dias, veredito, 0, true));
  }

  function trocarDias(d: number) {
    setDias(d);
    carregarPlacares(d);
    carregarPares(d, veredito, 0, true);
  }

  function trocarVeredito(v: string) {
    setVeredito(v);
    carregarPares(dias, v, 0, true);
  }

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>🎓 Aprendiz</h1>
          <div className="page-meta">
            a IA treina em silêncio: decide o que responderia — sem enviar nada — e compara com o que a corretora respondeu de verdade
          </div>
        </div>
        <select className="ap-select" value={dias} onChange={(e) => trocarDias(Number(e.target.value))}>
          <option value={7}>Últimos 7 dias</option>
          <option value={30}>Últimos 30 dias</option>
          <option value={90}>Últimos 90 dias</option>
        </select>
      </div>

      {loading && !autonomia && !bateu && <div style={{ padding: 40, color: "var(--ink-3)" }}>Carregando…</div>}

      {erro && !autonomia && !bateu && (
        <div className="estado">
          <div className="estado-glifo">🎓</div>
          <h2>Sem dados agora</h2>
          <p>Não consegui falar com o copiloto ({erro}). Tenta recarregar em instantes.</p>
        </div>
      )}

      {autonomia && <PlacarAutonomia p={autonomia} />}
      {bateu && <PlacarBateu p={bateu} />}
      {aprendizado && <Aprendizado a={aprendizado} />}

      {bateu && (
        <>
          <div className="page-head" style={{ marginTop: 8, marginBottom: 14 }}>
            <div>
              <h1 style={{ fontSize: 16 }}>Rascunho × resposta real</h1>
              <div className="page-meta">{num(totalPares)} par(es) no recorte</div>
            </div>
          </div>
          <div className="filtros">
            {FILTROS_VEREDITO.map((f) => (
              <button
                key={f.v}
                className={`filtro-pill ${veredito === f.v ? "on" : ""}`}
                onClick={() => trocarVeredito(f.v)}
              >
                {f.label}
              </button>
            ))}
          </div>

          {pares.map((p) => (
            <Par key={p.decisao_id} p={p} onMarcar={marcar} />
          ))}
          {pares.length === 0 && !loadingPares && (
            <div style={{ color: "var(--ink-3)", padding: "18px 4px" }}>Nenhum par nesse recorte.</div>
          )}
          {pares.length < totalPares && (
            <button className="ap-mais" disabled={loadingPares} onClick={() => carregarPares(dias, veredito, pares.length, false)}>
              {loadingPares ? "Carregando…" : `Carregar mais (${num(totalPares - pares.length)})`}
            </button>
          )}
        </>
      )}
    </div>
  );
}
