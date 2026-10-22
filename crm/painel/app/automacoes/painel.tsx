"use client";

// PEÇAS DO PAINEL DE ESTADO — as mesmas da tela do remarketing, para as outras.
//
// A tela do remarketing (05/10/2026, aprovada pela Dona) abre com um bloco que
// diz, em letra grande, se está ligada e se está agindo, e logo abaixo as
// respostas: por qual linha, pra quem, quando e quanto, o que aconteceu. As
// outras automações (boas-vindas, follow-up, nutrição) e a lista usam o MESMO
// bloco, com as MESMAS classes (`remarketing/disparo.module.css`): duas telas
// com desenhos diferentes pro mesmo estado viram duas linguagens na primeira
// mudança.
//
// Régua de leitura, a mesma de sempre:
//   - sem leitura não é zero nem verde: a bolinha fica tracejada e diz "sem leitura";
//   - o que o servidor ainda não manda aparece como "sem leitura", nunca inventado.

import { useEffect, useState } from "react";
import Interruptor from "./Interruptor";
import type { CorretoraBoasVindas, JanelaEnvio, LinhaCurta, NumeroDoCliente, SaudeDoNumero, SaudeEnvio } from "./types";
import s from "./remarketing/disparo.module.css";
import r from "./remarketing/remarketing.module.css";
import p from "./painel.module.css";

export type Cor = "verde" | "amarelo" | "vermelho" | "desconhecida";

export const NOME_COR: Record<Cor, string> = {
  verde: "verde",
  amarelo: "amarela",
  vermelho: "vermelha",
  desconhecida: "sem leitura",
};

export const mil = (n: number) => n.toLocaleString("pt-BR");

const SP = "America/Sao_Paulo";

export const horaCurta = (iso: string | null | undefined) =>
  iso ? new Date(iso).toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit", timeZone: SP }) : null;

export const diaDoInstante = (iso: string | null | undefined) =>
  iso ? new Date(iso).toLocaleDateString("pt-BR", { day: "2-digit", month: "2-digit", timeZone: SP }) : null;

export function horaDaLeitura(iso: string | null | undefined): string {
  if (!iso) return "nunca lida";
  return `lida às ${horaCurta(iso)} de ${diaDoInstante(iso)}`;
}

// Uma linha de texto pra saúde: a cor, e a hora quando a leitura é velha.
export function saudeEmTexto(saude: SaudeDoNumero): string {
  if (saude.cor === "desconhecida") return "saúde sem leitura";
  return `linha ${NOME_COR[saude.cor]}${saude.velha ? `, ${horaDaLeitura(saude.lido_em)}` : ""}`;
}

export function Bolinha({ cor, grande = false }: { cor: Cor; grande?: boolean }) {
  return <span className={`${s.bolinha} ${s[`cor_${cor}`]} ${grande ? s.bolinhaGrande : ""}`} aria-hidden />;
}

export function Barra({ valor, de }: { valor: number; de: number }) {
  const pct = de > 0 ? Math.min(100, Math.round((valor / de) * 100)) : 0;
  return (
    <div className={s.barra} aria-hidden>
      <div className={`${s.barraCheia} ${pct >= 100 ? s.barraFim : ""}`} style={{ width: `${pct}%` }} />
    </div>
  );
}

// ── a linha de cada corretora e a cor dela ──
//
// A saúde de cada número vem do servidor (`carrossel_corretor.linha_disparadora.
// numeros[].saude`, lida da Meta com cache de 5 min). O que o servidor ainda
// não manda é QUAL número é de qual corretora (PENDENCIAS-BACKEND, item 5):
//   1. se vier `linha` na corretora, vale ela;
//   2. senão, o número cujo slug é o nome curto da corretora sem acento
//      ("Pessoa7" → "pessoa_d"), que é como o registro de linhas do copiloto os
//      nomeia hoje. Sem esse número: "sem leitura", nunca verde.
const semAcento = (t: string) =>
  t.normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase().trim();

export type LinhaDaCorretora = {
  nome: string;
  telefone: string | null;
  saude: SaudeDoNumero;
  /** false = a corretora não tem linha Cloud: a automação não manda nada por ela. */
  temLinha: boolean;
  /** true = a automação está desligada pra esta corretora (interruptor dela). */
  desligada?: boolean;
  /** true = a linha está recusando por cobrança da Meta (template sai e volta failed). */
  cobranca?: boolean;
};

// A linha como o servidor manda (`linha_curta`, 06/10): é ela que vale. A cor
// sem leitura continua "sem leitura", e linha não registrada = "sem linha".
export function linhaDoServidor(nome: string, l: LinhaCurta | null | undefined, desligada = false): LinhaDaCorretora {
  if (!l) return { nome, telefone: null, saude: { cor: "desconhecida" }, temLinha: false, desligada };
  const saude: SaudeDoNumero = l.saude?.cor ? l.saude : { ...(l.saude ?? {}), cor: l.cor ?? "desconhecida" };
  return {
    nome,
    telefone: l.telefone,
    saude,
    temLinha: l.registrada,
    desligada,
    cobranca: l.cobranca_bloqueada === true,
  };
}

export function linhaPorSlug(slug: string, numeros: NumeroDoCliente[] | undefined): NumeroDoCliente | null {
  return (numeros ?? []).find((n) => n.slug === slug) ?? null;
}

export function linhaDaCorretora(
  c: Pick<CorretoraBoasVindas, "label" | "tem_linha_cloud"> & { linha?: CorretoraBoasVindas["linha"] },
  numeros: NumeroDoCliente[] | undefined,
): LinhaDaCorretora {
  if (c.linha) {
    // O servidor manda a linha desde 06/10 (pendência 5): vale ela, não o nome.
    const l = c.linha as Partial<LinhaCurta> & { slug: string | null };
    if (l.registrada !== undefined) return linhaDoServidor(c.label, l as LinhaCurta);
    return { nome: c.label, telefone: c.linha.telefone ?? null, saude: c.linha.saude ?? { cor: "desconhecida" }, temLinha: true };
  }
  const n = linhaPorSlug(semAcento(c.label), numeros);
  return {
    nome: c.label,
    telefone: n?.telefone ?? null,
    saude: n?.saude ?? { cor: "desconhecida" },
    temLinha: c.tem_linha_cloud,
  };
}

// As linhas lado a lado, cada uma com a bolinha dela. Sem linha Cloud a
// corretora aparece riscada: o lead dela entra, mas nada sai pela automação.
export function Linhas({ linhas, testid }: { linhas: LinhaDaCorretora[]; testid?: string }) {
  if (linhas.length === 0) return <span className={s.respostaSub}>nenhuma linha</span>;
  return (
    <ul className={p.linhas} data-testid={testid}>
      {linhas.map((l) => (
        <li
          key={l.nome}
          className={`${p.linha} ${l.temLinha && !l.desligada ? "" : p.linhaSem} ${l.saude.cor === "vermelho" ? p.linhaRuim : ""}`}
          data-cor={l.temLinha ? l.saude.cor : "sem-linha"}
          data-desligada={l.desligada ? "sim" : undefined}
          title={
            l.temLinha
              ? `${l.telefone ?? "sem telefone"} · ${saudeEmTexto(l.saude)}${l.cobranca ? " · recusando por cobrança" : ""}`
              : "sem linha Cloud"
          }
        >
          <Bolinha cor={l.temLinha ? (l.saude.cor as Cor) : "desconhecida"} />
          <span>{l.nome}</span>
          {!l.temLinha && <span className={p.linhaNota}>sem linha</span>}
          {l.temLinha && l.desligada && <span className={p.linhaNota}>desligada</span>}
          {l.temLinha && l.cobranca && <span className={`${p.linhaNota} ${p.linhaRuim}`}>cobrança</span>}
        </li>
      ))}
    </ul>
  );
}

export function frasesDasLinhas(linhas: LinhaDaCorretora[]): string | null {
  const vermelhas = linhas.filter((l) => l.temLinha && !l.desligada && l.saude.cor === "vermelho").map((l) => l.nome);
  if (vermelhas.length === 0) return null;
  return vermelhas.length === 1
    ? `a linha da ${vermelhas[0]} está vermelha na Meta`
    : `as linhas da ${vermelhas.slice(0, -1).join(", ")} e da ${vermelhas.at(-1)} estão vermelhas na Meta`;
}

// ── o estado: ligada? agindo? se não, por quê ──
export type Tom = "saindo" | "ligado" | "parado" | "desligado" | "sem";
export type Situacao = { tom: Tom; titulo: string; frase: string };

/** Quando a automação mandou a última mensagem que chegou em alguém. */
export function ultimoEnvio(saude: SaudeEnvio | undefined): { texto: string; nasUltimas24h: boolean } {
  if (!saude || !saude.disponivel) return { texto: "último envio sem leitura", nasUltimas24h: false };
  if (saude.nunca_enviou) return { texto: "nunca enviou", nasUltimas24h: false };
  const em = saude.ultimo_envio_real;
  const d = saude.dias ?? 0;
  const quando = d === 0 ? `às ${horaCurta(em)}` : d === 1 ? "ontem" : `há ${d} dias`;
  return { texto: `último envio ${quando}`, nasUltimas24h: d === 0 };
}

/**
 * A mesma escada do remarketing, em ordem: sem leitura → desligado (e o que
 * aconteceria se ligasse) → ligado com trava → ligado e mandou nas últimas 24 h
 * → ligado sem mandar há mais tempo do que o ritmo dela (o limiar vem do
 * servidor) → ligado, esperando.
 */
export function situacaoDe(o: {
  ligado: boolean | null;
  trava: string | null;
  saude: SaudeEnvio | undefined;
  /** Gente esperando a vez agora. null = não sei. */
  naFila: number | null;
  /** O que acontece ao ligar sem trava ("o próximo lead novo recebe…"). */
  aoLigar: string;
  /** Ligado, sem trava e sem envio recente: o que ela está esperando. */
  esperando: string;
  /** Alerta que não para o envio (linha vermelha), na frase. */
  alerta?: string | null;
  /** "o follow-up" é masculino; "a boas-vindas" e "a nutrição", feminino. */
  masculino?: boolean;
}): Situacao {
  const a = o.masculino ? "o" : "a";
  const ult = ultimoEnvio(o.saude);
  const extra = o.alerta ? ` Atenção: ${o.alerta}.` : "";
  if (o.ligado === null) {
    return { tom: "sem", titulo: "Sem leitura", frase: `Não consegui ler se está ligad${a}. Enquanto isso o motor não envia.` };
  }
  if (!o.ligado) {
    return {
      tom: "desligado",
      titulo: `Desligad${a}`,
      frase: o.trava
        ? `Nada sai sozinho. Se ligar agora, ainda não sai: ${o.trava}.`
        : `Nada sai sozinho. Se ligar agora, ${o.aoLigar}.`,
    };
  }
  if (o.trava) return { tom: "parado", titulo: `Ligad${a}, mas parad${a}`, frase: `Não sai: ${o.trava}.${extra}` };
  if (ult.nasUltimas24h) {
    return { tom: "saindo", titulo: "Agindo", frase: `Ligad${a} e mandando: ${ult.texto}.${extra}` };
  }
  if (o.saude?.disponivel && o.saude.alerta && (o.naFila ?? 0) > 0) {
    const quanto = o.saude.nunca_enviou ? "nunca mandou nada" : `não manda nada ${ult.texto.replace("último envio ", "desde ")}`;
    return {
      tom: "parado",
      titulo: `Ligad${a}, mas parad${a}`,
      frase: `Tem gente esperando e ${quanto} (o normal é até ${o.saude.limiar_dias} dias).${extra}`,
    };
  }
  return { tom: "ligado", titulo: `Ligad${a}`, frase: `${o.esperando}${ult.texto !== "nunca enviou" ? ` ${ult.texto[0].toUpperCase()}${ult.texto.slice(1)}.` : ""}${extra}` };
}

// ── o bloco do topo ──
export type Resposta = {
  rotulo: string;
  valor: React.ReactNode;
  sub?: React.ReactNode;
  ruim?: boolean;
  testid: string;
};

export type Chave = {
  /** null = não consegui ler (≠ desligado). */
  ligado: boolean | null;
  carregando: boolean;
  ocupado: boolean;
  rotulo: string;
  confirmarLigar: string;
  confirmarDesligar: string;
  onMudar: (novo: boolean) => void;
};

export function EstadoAutomacao({
  sit,
  chave,
  respostas,
  faixa,
  rodape,
  rotulo,
  masculino = false,
}: {
  masculino?: boolean;
  sit: Situacao;
  chave: Chave;
  respostas: Resposta[];
  faixa?: { rotulo: string; conteudo: React.ReactNode; testid: string }[];
  rodape?: React.ReactNode;
  rotulo: string;
}) {
  return (
    <section className={`${s.estado} ${s[`tom_${sit.tom}`] ?? ""} ${p[`tom_${sit.tom}`] ?? ""}`} aria-label={rotulo}>
      <div className={s.estadoTopo}>
        <div className={s.estadoTexto}>
          <h2 className={s.estadoTitulo} data-testid="estado-titulo" data-tom={sit.tom}>
            <span className={s.estadoPonto} aria-hidden />
            {sit.titulo}
          </h2>
          <p className={s.estadoFrase} data-testid="estado-frase">
            {sit.frase}
          </p>
        </div>
        <div className={s.estadoChave}>
          {chave.ligado === null ? (
            <span className={s.dica}>{chave.carregando ? "carregando…" : "sem leitura"}</span>
          ) : (
            <>
              <Interruptor
                grande
                ativa={chave.ligado}
                ocupado={chave.ocupado}
                rotulo={chave.rotulo}
                confirmarLigar={chave.confirmarLigar}
                confirmarDesligar={chave.confirmarDesligar}
                onMudar={chave.onMudar}
              />
              <span className={s.chaveRotulo}>{chave.ligado ? "ligad" : "desligad"}{masculino ? "o" : "a"}</span>
            </>
          )}
        </div>
      </div>

      <dl className={s.respostas}>
        {respostas.map((r) => (
          <div key={r.testid} className={`${s.resposta} ${r.ruim ? s.respostaRuim : ""}`} data-testid={r.testid}>
            <dt>{r.rotulo}</dt>
            <dd className={s.respostaValor}>{r.valor}</dd>
            {r.sub !== undefined && <dd className={s.respostaSub}>{r.sub}</dd>}
          </div>
        ))}
      </dl>

      {faixa && faixa.length > 0 && (
        <div className={`${s.faixaHoje} ${p.faixa}`} data-testid="faixa-hoje">
          {faixa.map((f) => (
            <div key={f.testid} className={s.faixaGrupo} data-testid={f.testid}>
              <span className={s.faixaRotulo}>{f.rotulo}</span>
              <span className={s.faixaNumeros}>{f.conteudo}</span>
            </div>
          ))}
        </div>
      )}

      {rodape && <div className={s.estadoRodape}>{rodape}</div>}
    </section>
  );
}

// ── o interruptor de uma chave, lido do hook de configuração ──
export function lerChave(estado: { valor: unknown; indisponivel?: boolean } | undefined): boolean | null {
  if (!estado || estado.indisponivel) return null;
  return Boolean(estado.valor);
}

// ── barra de salvar: só ganha corpo com mudança, e diz que salvar não liga ──
export function BarraSalvar({
  quantas,
  salvando,
  salvo,
  nota,
  onDescartar,
  onSalvar,
  bloqueio,
}: {
  quantas: number;
  salvando: boolean;
  salvo: boolean;
  nota: string;
  onDescartar: () => void;
  onSalvar: () => void;
  /** Motivo pelo qual não dá pra salvar ainda (a soma das fatias, por exemplo). */
  bloqueio?: string | null;
}) {
  return (
    <div className={`${s.salvarBarra} ${quantas > 0 ? s.salvarBarraAtiva : ""}`} data-testid="barra-salvar">
      <span className={s.salvarTexto}>
        {salvando
          ? "salvando…"
          : quantas > 0
            ? `${quantas} ${quantas === 1 ? "mudança" : "mudanças"} sem salvar`
            : salvo
              ? "gravado"
              : "nada a salvar"}
        <span className={s.salvarNota}>{quantas > 0 && bloqueio ? bloqueio : nota}</span>
      </span>
      {quantas > 0 && (
        <button type="button" className={s.link} disabled={salvando} onClick={onDescartar}>
          descartar
        </button>
      )}
      <button
        type="button"
        className={s.btnPrimario}
        disabled={quantas === 0 || salvando || Boolean(bloqueio)}
        onClick={onSalvar}
      >
        {salvando ? "salvando…" : "salvar"}
      </button>
    </div>
  );
}

// "gravado" some sozinho depois de 4 s.
export function useSalvo(): [boolean, (v: boolean) => void] {
  const [salvo, setSalvo] = useState(false);
  useEffect(() => {
    if (!salvo) return;
    const t = setTimeout(() => setSalvo(false), 4000);
    return () => clearTimeout(t);
  }, [salvo]);
  return [salvo, setSalvo];
}

// ── números curtos: últimas 24 h e 7 dias, do MESMO endpoint de métricas ──
//
// O servidor não tem "hoje" pra estas automações: a janela é rolante
// (`dias=1` = últimas 24 h). A tela diz "24 h", não "hoje".
export function useJanelas<T>(url: string, recarga = 0): { d1: T | null; d7: T | null; erro: string | null } {
  const [d1, setD1] = useState<T | null>(null);
  const [d7, setD7] = useState<T | null>(null);
  const [erro, setErro] = useState<string | null>(null);
  useEffect(() => {
    const ler = (dias: number) =>
      fetch(`${url}?dias=${dias}`, { cache: "no-store" }).then(async (r) => {
        const body = await r.json();
        if (!r.ok) throw new Error(body?.error || body?.detail || `HTTP ${r.status}`);
        return body as T;
      });
    Promise.all([ler(1), ler(7)])
      .then(([a, b]) => {
        setD1(a);
        setD7(b);
        setErro(null);
      })
      .catch((e) => setErro(String((e as Error).message || e)));
  }, [url, recarga]);
  return { d1, d7, erro };
}

// ── entrega na Meta: o que saiu, entregou, foi lido e falhou numa janela ──
//
// Vem do servidor desde 06/10 (status do webhook pelo wamid). `sem_rastro` =
// saiu sem o wamid gravado (boas-vindas antigas): fica fora de entregue E de
// falhou, e a tela diz isso em vez de "0 entregues". null = não consegui ler.
export function EntregaTexto({ j, rotulo }: { j: JanelaEnvio | null | undefined; rotulo: string }) {
  if (!j) return <span>{rotulo}: sem leitura</span>;
  if (j.saiu === 0 && j.falhou === 0) return <span>{rotulo}: nada saiu</span>;
  const rastreadas = j.saiu - j.sem_rastro;
  return (
    <span>
      {rotulo}: <strong>{mil(j.saiu)}</strong> saíram
      {rastreadas > 0 && <>, {mil(j.entregue)} entregues, {mil(j.lido)} lidas</>}
      {j.falhou > 0 ? <>, <strong className={s.faixaRuim}>{mil(j.falhou)} falharam</strong></> : rastreadas > 0 ? ", 0 falharam" : ""}
      {j.sem_rastro > 0 && <>, {mil(j.sem_rastro)} sem rastro de entrega</>}
    </span>
  );
}

// ── régua de leitura dos números ──
export function pct(taxa: number | null): string {
  if (taxa === null) return "sem dado";
  // O backend já manda PERCENTUAL (0-100), igual ao resto do painel.
  return `${taxa.toFixed(1).replace(".", ",")}%`;
}

export const dataCurta = (iso: string) =>
  new Date(iso).toLocaleDateString("pt-BR", { day: "2-digit", month: "2-digit" });

// ── faixa do MARCO: desde quando a medição conta, e como recomeçar ──
//
// O marco é UM só pra boas-vindas, follow-up e nutrição (mesmo motor, mesmo
// log): recomeçar numa recomeça nas três, e a pergunta diz isso. Não apaga
// nada: só muda o ponto de partida da conta.
export function MarcoBarra({
  met,
  onRecomecou,
}: {
  met: { medindo_desde: string | null; motivo_do_marco: string | null; marco_por: string | null };
  onRecomecou: () => void;
}) {
  const [salvando, setSalvando] = useState(false);
  const [erro, setErro] = useState<string | null>(null);

  const recomecar = async () => {
    const motivo = prompt(
      "Recomeçar a medição a partir de agora?\n\n" +
        "Vale pra boas-vindas, follow-up e nutrição juntas (é a mesma medição). " +
        "Não apaga nada: só a conta dos números passa a valer daqui pra frente.\n\n" +
        "Por que está recomeçando? (ex: copies novas)",
    );
    if (motivo === null) return;
    if (!motivo.trim()) {
      setErro("Precisa do motivo: número sem explicação vira mistério depois.");
      return;
    }
    setSalvando(true);
    setErro(null);
    try {
      const r = await fetch("/api/automacoes/metricas/marco", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ motivo: motivo.trim() }),
      });
      const out = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(out?.detail || out?.error || `HTTP ${r.status}`);
      onRecomecou();
    } catch (e) {
      setErro(String((e as Error).message || e));
    } finally {
      setSalvando(false);
    }
  };

  return (
    <div className={r.marcoBarra}>
      <div className={r.marcoTexto}>
        {met.medindo_desde ? (
          <>
            <strong>medindo desde {dataCurta(met.medindo_desde)}</strong>
            {met.motivo_do_marco ? `: ${met.motivo_do_marco}` : ""}
            {met.marco_por ? ` (${met.marco_por})` : ""}
          </>
        ) : (
          <strong>medindo todo o histórico</strong>
        )}
      </div>
      <button
        type="button"
        className={r.btnRecomecar}
        onClick={() => void recomecar()}
        disabled={salvando}
        title="Passa a contar a partir de agora. Não apaga histórico."
      >
        {salvando ? "recomeçando…" : "recomeçar a medição"}
      </button>
      {erro && <div className={r.erro}>{erro}</div>}
    </div>
  );
}
