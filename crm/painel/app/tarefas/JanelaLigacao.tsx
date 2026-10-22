"use client";

// 📞 JANELA DE LIGAÇÃO — aba da tela /tarefas.
//
// POR QUE AQUI E NÃO EM TELA PRÓPRIA: é o mesmo gesto mental da corretora
// nesta tela ("o que eu faço agora"), reusa as pills de corretora e o toast
// que já existem, e uma entrada nova de menu pra uma fila que se esvazia
// viraria item morto no dia em que a base acabar.
//
// O QUE ELA É: a fila de quem ligar vem do REMARKETING e só de lead SEM DONO.
// A ligação é o momento em que a corretora ADOTA o lead — a janela deixa de
// ser lista de tarefa e vira o mecanismo de distribuição da base parada.
//
// TRÊS BOTÕES, UM CLIQUE CADA (não atendeu · atendeu mas não quis · atendeu e
// quer continuar). Registrar não recarrega a lista nem faz perder a posição:
// o cartão vira "registrado ✓" no lugar. Concluir ligação é ação de rotina,
// dezenas de vezes por dia — formulário mata o registro e a gente perde o dado
// que já tem hoje.
//
// TODA tentativa vira nota na conversa, inclusive "não atendeu": é o que
// permite responder "quantas vezes já tentamos falar com essa pessoa?". O
// contador aparece no cartão ANTES de ligar ("3ª tentativa") — a corretora
// decide melhor sabendo que é a quinta vez.
//
// A referência 100 → 30 → 12 é HISTÓRICO, não meta: veio de um time de 5
// pessoas com hora fixa pra ligar. Está escrito na tela.

import { useCallback, useEffect, useRef, useState } from "react";
import Canvas, { Edge } from "@/app/automacoes/Canvas";
import { Espera, Node } from "@/app/automacoes/blocos";
import { useEstadoPersistido } from "@/app/lib/estado-persistido";
import s from "./ligacoes.module.css";
import {
  linkTel,
  formatarTel,
  paraDiscarChipVirtual,
  linkWhatsApp,
} from "@/lib/telefone";

export type EstadoLigacao =
  | "nao_atendeu"
  | "atendeu_sem_continuar"
  | "atendeu_quer_continuar";

type ItemFila = {
  lead_id: number;
  external_ref: string | null;
  nome: string;
  telefone: string | null;
  /** número do jid da conversa — o que o WhatsApp reconhece. null = sem histórico */
  telefone_whatsapp?: string | null;
  origem: string | null;
  etapa: string | null;
  dias_parado: number | null;
  resumo: string | null;
  ultima_msg: string | null;
  ultima_msg_at: string | null;
  tentativas: number;
  tentativa_atual: number;
  /** Quem está com o lead — só a visão de supervisão (admin) recebe de outra. */
  reservado_por: string | null;
  ultima_tentativa_em: string | null;
  /** ISO do retorno combinado ("ligar mais tarde"); null = sem hora marcada. */
  agendado_para: string | null;
  /** Quando o cliente entrou no sistema (data do Kommo). null = desconhecida. */
  entrou_em: string | null;
};

/** "entrou 17/06/25" — data de entrada do cliente, curta. */
function rotuloEntrada(iso: string): string {
  return new Date(iso).toLocaleDateString("pt-BR", {
    day: "2-digit",
    month: "2-digit",
    year: "2-digit",
  });
}

// "ligar mais tarde" ordena a fila: quem tem hora marcada vem primeiro, na
// ordem do relógio (o atrasado é o mais urgente, então fica no topo). O backend
// já devolve assim; refazemos no cliente pra o cartão subir NA HORA do clique,
// sem esperar o próximo refetch.
function ordenarFila(itens: ItemFila[]): ItemFila[] {
  return [...itens].sort((a, b) => {
    if (!a.agendado_para && !b.agendado_para) return 0;
    if (!a.agendado_para) return 1;
    if (!b.agendado_para) return -1;
    return Date.parse(a.agendado_para) - Date.parse(b.agendado_para);
  });
}

/** Hora marcada, curta: "hoje 14:30" / "qui 09:00" / "12/08 09:00". */
function rotuloAgendado(iso: string): string {
  const d = new Date(iso);
  const hora = d.toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" });
  const hoje = new Date();
  const mesmoDia =
    d.getDate() === hoje.getDate() &&
    d.getMonth() === hoje.getMonth() &&
    d.getFullYear() === hoje.getFullYear();
  if (mesmoDia) return `hoje ${hora}`;
  const amanha = new Date(hoje);
  amanha.setDate(hoje.getDate() + 1);
  if (
    d.getDate() === amanha.getDate() &&
    d.getMonth() === amanha.getMonth() &&
    d.getFullYear() === amanha.getFullYear()
  ) {
    return `amanhã ${hora}`;
  }
  return `${d.toLocaleDateString("pt-BR", { day: "2-digit", month: "2-digit" })} ${hora}`;
}

// Corretoras que atendem a fila (mesmos ids do copiloto). Admin (dona/pessoa/
// assistente) NÃO é corretora: quando é ele quem agenda, precisa dizer de quem é a
// ligação — senão a tarefa nasce sem dona e some da aba de tarefas assim que
// alguém seleciona uma pill de corretora.
const CORRETORAS_FILA: { id: number; nome: string }[] = [
  { id: 1, nome: "Pessoa10" },
  { id: 2, nome: "Pessoa11" },
  { id: 3, nome: "Pessoa12" },
];

/** `reservado_por` é o login do painel de quem pegou o lead — vira corretora. */
function corretoraDoLogin(login: string | null): number | null {
  const l = (login ?? "").trim().toLowerCase();
  if (l === "corretor") return 1;
  if (l === "corretor") return 2;
  if (l === "corretor") return 3;
  return null;
}

/** Data de hoje (YYYY-MM-DD) e hora +1h, pra abrir o modal já preenchido. */
function sugestaoRetorno(): { data: string; hora: string } {
  const d = new Date(Date.now() + 60 * 60 * 1000);
  const p = (n: number) => String(n).padStart(2, "0");
  return {
    data: `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`,
    hora: `${p(d.getHours())}:${p(d.getMinutes())}`,
  };
}

type ReferenciaHistorica = {
  periodo: string;
  ligacoes: number;
  taxa_atendimento: number;
  taxa_continuidade: number;
  resumo: string;
  nota: string;
};

/** Meta do dia e da semana da corretora (null pra admin: ele supervisiona). */
type Meta = {
  corretora_id: number;
  dia: string;
  fuso: string;
  meta_diaria: number;
  meta_semanal: number;
  blocos_extras: number;
  alvo_hoje: number;
  feitas_hoje: number;
  restante_hoje: number;
  concluiu_dia: boolean;
  feitas_semana: number;
  faltam_semana: number;
  concluiu_semana: boolean;
  confete_em: string | null;
};

type Fila = {
  total: number;
  itens: ItemFila[];
  /** Como o telefone DESTA corretora disca: "zero_ddd" (chip virtual) ou
   *  "padrao". Vem do backend porque quem sabe disso é o cadastro da linha,
   *  não a tela. */
  discagem?: "zero_ddd" | "padrao";
  /** Minutos que os leads desta lista ficam RESERVADOS pra quem a carregou. */
  reserva_minutos?: number;
  meta: Meta | null;
  referencia_historica: ReferenciaHistorica;
};

type LinhaMetrica = {
  corretora_id: number | null;
  corretora: string | null;
  total_na_fila: number;
  ligacoes: number;
  sem_registro: number;
  atenderam: number;
  nao_atenderam: number;
  quiseram_continuar: number;
  nao_quiseram_continuar: number;
  taxa_atendimento: number | null;
  taxa_continuidade: number | null;
  sem_dado: boolean;
  amostra_pequena: boolean;
  amostra_pequena_continuidade: boolean;
};

type FaixaHorario = {
  chave: number | null;
  rotulo: string;
  ligacoes: number;
  atenderam: number;
  taxa_atendimento: number | null;
  sem_dado: boolean;
  amostra_pequena: boolean;
};

/** Em que horário a pessoa atende — agrupado em America/Sao_Paulo, nunca UTC. */
type QuandoAtendem = {
  fuso: string;
  amostra: number;
  amostra_minima: number;
  amostra_minima_faixa: number;
  /** false = ainda não dá pra concluir nada; a tela diz "juntando dado". */
  confiavel: boolean;
  por_faixa: FaixaHorario[];
  fora_da_janela: FaixaHorario;
  por_dia_semana: FaixaHorario[];
  melhor_faixa: { rotulo: string; taxa: number | null; ligacoes: number } | null;
  definicao: string;
};

type Metricas = {
  dias: number;
  amostra_minima: number;
  por_corretora: LinhaMetrica[];
  total: LinhaMetrica;
  tentativas_ate_atender: {
    leads_que_atenderam: number;
    media: number | null;
    maximo: number | null;
    sem_dado: boolean;
  };
  referencia_historica: ReferenciaHistorica;
  quando_atendem: QuandoAtendem;
  definicao: string;
};

type Registro = {
  estado: EstadoLigacao | null;
  adotou: boolean;
  moveuEtapa: boolean;
  jaTemDono: boolean;
  /** marcado como "Venda perdida" — saiu da fila e foi pro funil principal */
  perdida?: boolean;
  /** número errado: telefone marcado como inválido, lead fora da fila pra sempre */
  telefoneErrado?: boolean;
};

const LIMITE = 50;

// De quanto em quanto tempo a aba aberta renova a reserva da lista. Bem abaixo
// dos 30 min da reserva: celular que dorme no meio perde uma ou duas voltas e
// ainda chega antes de vencer.
const RENOVAR_A_CADA_MS = 5 * 60 * 1000;

// "sem dado" ≠ 0%: taxa null nunca vira zero na tela.
function pct(taxa: number | null): string {
  if (taxa === null) return "sem dado";
  return `${taxa.toFixed(1).replace(".", ",")}%`;
}

const ROTULO_ESTADO: Record<EstadoLigacao, string> = {
  nao_atendeu: "não atendeu",
  atendeu_sem_continuar: "atendeu, não quis seguir agora",
  atendeu_quer_continuar: "atendeu e quer continuar",
};


// ── confete (sem lib) ───────────────────────────────────────────────────────
//
// A Dona pediu a explosão de confete ao fechar a tarefa do dia — e é enfeite de
// verdade: cai DEPOIS do registro já gravado, e se quebrar aqui a ligação
// continua registrada (o componente é isolado e não participa do POST).
const CORES = ["#1a6b3a", "#EA2871", "#FFD413", "#064794", "#f08c2e"];

function Confete({ aoFim }: { aoFim: () => void }) {
  useEffect(() => {
    const t = setTimeout(aoFim, 4200);
    return () => clearTimeout(t);
  }, [aoFim]);
  const pecas = Array.from({ length: 70 }, (_, i) => ({
    left: (i * 37) % 100,
    delay: (i % 12) * 0.12,
    dur: 2.4 + ((i * 7) % 18) / 10,
    cor: CORES[i % CORES.length],
  }));
  return (
    <div className={s.confete} aria-hidden>
      {pecas.map((p, i) => (
        <span
          key={i}
          className={s.confeteItem}
          style={{
            left: `${p.left}%`,
            background: p.cor,
            animationDelay: `${p.delay}s`,
            animationDuration: `${p.dur}s`,
          }}
        />
      ))}
    </div>
  );
}

// ── meta do dia + progresso da semana ───────────────────────────────────────
function MetaDoDia({
  meta,
  adiantando,
  onAdiantar,
}: {
  meta: Meta;
  adiantando: boolean;
  onAdiantar: () => void;
}) {
  const pctDia = Math.min((meta.feitas_hoje / Math.max(meta.alvo_hoje, 1)) * 100, 100);
  const pctSemana = Math.min(
    (meta.feitas_semana / Math.max(meta.meta_semanal, 1)) * 100,
    100,
  );
  return (
    <div className={`${s.meta} ${meta.concluiu_dia ? s.metaFeito : ""}`}>
      <div className={s.metaTopo}>
        <span className={s.rotulo}>hoje</span>
        <span className={s.metaNumero}>
          {meta.feitas_hoje}/{meta.alvo_hoje}
        </span>
        {meta.concluiu_dia ? (
          <span className={s.statSub}>
            🎉 tarefa de hoje concluída
            {meta.blocos_extras > 0
              ? ` (+${meta.blocos_extras} bloco${meta.blocos_extras > 1 ? "s" : ""} adiantado${meta.blocos_extras > 1 ? "s" : ""})`
              : ""}
          </span>
        ) : (
          <span className={s.statSub}>
            faltam {meta.restante_hoje} pra fechar o dia
          </span>
        )}
      </div>
      <div className={s.metaBarra}>
        <div className={s.metaPreenchida} style={{ width: `${pctDia}%` }} />
      </div>

      <div className={s.metaTopo} style={{ marginTop: 12 }}>
        <span className={s.rotulo}>semana</span>
        <span className={s.metaNumero}>
          {meta.feitas_semana}/{meta.meta_semanal}
        </span>
        <span className={s.statSub}>
          {meta.concluiu_semana
            ? "semana fechada 👏"
            : `faltam ${meta.faltam_semana} ligações pra fechar as ${meta.meta_semanal} da semana`}
        </span>
      </div>
      <div className={s.metaBarra}>
        <div className={s.metaPreenchida} style={{ width: `${pctSemana}%` }} />
      </div>

      {meta.concluiu_dia && !meta.concluiu_semana && (
        <button className={s.botaoAdiantar} disabled={adiantando} onClick={onAdiantar}>
          {adiantando ? "liberando…" : `adiantar a tarefa de amanhã (+${meta.meta_diaria})`}
        </button>
      )}
    </div>
  );
}

// ── em que horário a pessoa atende ──────────────────────────────────────────
//
// Barra por faixa de 1h, no fuso de SP. TRÊS travas de leitura, porque este é
// o gráfico com mais risco de conclusão errada da tela inteira:
//   1. faixa sem ligação nenhuma NÃO é barra de 0% — é hachura cinza (vazia);
//   2. faixa com pouca ligação vem esmaecida e com o número de ligações à
//      mostra, pra ninguém ler "100%" sem ver que são 2 ligações;
//   3. enquanto o período não junta `amostra_minima` ligações, não existe
//      "melhor horário": o bloco diz que está juntando dado.
function QuandoAtendem({ q }: { q: QuandoAtendem }) {
  const maior = Math.max(
    ...q.por_faixa.map((f) => f.taxa_atendimento ?? 0),
    1,
  );
  return (
    <div className={s.horario}>
      <div className={s.horarioTopo}>
        <span className={s.rotulo}>quando atendem</span>
        {q.confiavel && q.melhor_faixa ? (
          <>
            <strong style={{ fontSize: 15, color: "#15110d" }}>
              {q.melhor_faixa.rotulo}
            </strong>
            <span className={s.statSub}>
              melhor faixa — {pct(q.melhor_faixa.taxa)} de{" "}
              {q.melhor_faixa.ligacoes} ligações
            </span>
          </>
        ) : (
          <span className={s.statSub}>
            ainda juntando dado ({q.amostra} de {q.amostra_minima} ligações) — com
            pouca amostra, &ldquo;melhor horário&rdquo; é sorte, não padrão
          </span>
        )}
      </div>

      <div className={s.horarioBarras}>
        {q.por_faixa.map((f) => (
          <div key={f.rotulo} className={s.horarioCol} title={
            f.sem_dado
              ? `${f.rotulo}: nenhuma ligação`
              : `${f.rotulo}: ${f.atenderam} de ${f.ligacoes} atenderam`
          }>
            {f.sem_dado ? (
              <div className={s.horarioVazia} />
            ) : (
              <>
                <span className={s.horarioValor}>{pct(f.taxa_atendimento)}</span>
                <div
                  className={`${s.horarioBarra} ${f.amostra_pequena ? s.horarioPouca : ""}`}
                  style={{
                    height: `${Math.max(((f.taxa_atendimento ?? 0) / maior) * 88, 2)}%`,
                  }}
                />
              </>
            )}
            <span className={s.horarioRotulo}>
              {f.rotulo.slice(0, 2)}
            </span>
          </div>
        ))}
      </div>

      <div className={s.horarioDias}>
        {q.por_dia_semana
          .filter((d) => d.ligacoes > 0)
          .map((d) => (
            <span key={d.rotulo} className={s.horarioDia}>
              {d.rotulo} <strong>{pct(d.taxa_atendimento)}</strong>{" "}
              <span className={s.statSub}>({d.ligacoes})</span>
            </span>
          ))}
        {q.fora_da_janela.ligacoes > 0 && (
          <span className={s.horarioDia}>
            fora do horário comercial: {q.fora_da_janela.ligacoes}
          </span>
        )}
      </div>
      <p className={s.referenciaNota}>{q.definicao}</p>
    </div>
  );
}

// ── métrica (só admin) ───────────────────────────────────────────────────────
function Metrica({ met }: { met: Metricas }) {
  const t = met.total;
  return (
    <>
      <div className={s.stats}>
        <div className={s.stat}>
          <div className={s.statRotulo}>ligações feitas</div>
          <div className={s.statValor}>{t.ligacoes}</div>
          <div className={s.statSub}>
            com resultado registrado, nos últimos {met.dias} dias
          </div>
          {t.sem_registro > 0 && (
            <div className={s.statSub}>
              + {t.sem_registro} concluídas <strong>sem registro</strong> — não
              entram em nenhuma taxa e não são &ldquo;não atendeu&rdquo;
            </div>
          )}
        </div>
        <div className={s.stat}>
          <div className={s.statRotulo}>atenderam</div>
          <div className={s.statValor}>
            {t.sem_dado ? <span className={s.semDado}>sem dado</span> : t.atenderam}
          </div>
          <div className={s.statSub}>taxa de atendimento {pct(t.taxa_atendimento)}</div>
          {t.amostra_pequena && (
            <div className={s.marca}>amostra &lt; {met.amostra_minima}</div>
          )}
        </div>
        <div className={s.stat}>
          <div className={s.statRotulo}>quiseram continuar</div>
          <div className={s.statValor}>
            {t.atenderam === 0 ? (
              <span className={s.semDado}>sem dado</span>
            ) : (
              t.quiseram_continuar
            )}
          </div>
          <div className={s.statSub}>
            {pct(t.taxa_continuidade)} de quem atendeu
          </div>
          {t.amostra_pequena_continuidade && (
            <div className={s.marca}>amostra &lt; {met.amostra_minima}</div>
          )}
        </div>
        <div className={s.stat}>
          <div className={s.statRotulo}>tentativas até atender</div>
          <div className={s.statValor}>
            {met.tentativas_ate_atender.media === null ? (
              <span className={s.semDado}>sem dado</span>
            ) : (
              met.tentativas_ate_atender.media.toString().replace(".", ",")
            )}
          </div>
          <div className={s.statSub}>
            média, contando só quem atendeu na janela
            {met.tentativas_ate_atender.maximo
              ? ` · máximo ${met.tentativas_ate_atender.maximo}`
              : ""}
          </div>
        </div>
      </div>

      <table className={s.tabela}>
        <thead>
          <tr>
            <th>corretora</th>
            <th>ligações</th>
            <th>atenderam</th>
            <th>atendimento</th>
            <th>quiseram continuar</th>
            <th>continuidade</th>
            <th>sem registro</th>
          </tr>
        </thead>
        <tbody>
          {met.por_corretora.map((l) => (
            <tr key={l.corretora_id ?? "sem"}>
              <td>{l.corretora ?? "—"}</td>
              <td>{l.ligacoes}</td>
              <td>{l.sem_dado ? <span className={s.semDado}>—</span> : l.atenderam}</td>
              <td>
                {l.taxa_atendimento === null ? (
                  <span className={s.semDado}>sem dado</span>
                ) : (
                  <>
                    {pct(l.taxa_atendimento)}
                    {l.amostra_pequena && <span className={s.marca}>amostra baixa</span>}
                  </>
                )}
              </td>
              <td>
                {l.atenderam === 0 ? (
                  <span className={s.semDado}>—</span>
                ) : (
                  l.quiseram_continuar
                )}
              </td>
              <td>
                {l.taxa_continuidade === null ? (
                  <span className={s.semDado}>sem dado</span>
                ) : (
                  pct(l.taxa_continuidade)
                )}
              </td>
              <td>{l.sem_registro}</td>
            </tr>
          ))}
        </tbody>
      </table>

      {met.quando_atendem && <QuandoAtendem q={met.quando_atendem} />}
    </>
  );
}

// ── o desenho do fluxo (mesmo canvas das outras automações) ──────────────────
const EDGES: Edge[] = [
  { from: "lig-fila", to: "lig-w", kind: "h" },
  { from: "lig-w", to: "lig-call", kind: "h" },
  { from: "lig-call", to: "lig-nao", kind: "branch", tone: "muted", dashed: true },
  { from: "lig-call", to: "lig-sem", kind: "branch", tone: "warn" },
  { from: "lig-call", to: "lig-quer", kind: "branch" },
  { from: "lig-quer", to: "lig-adota", kind: "h" },
  { from: "lig-adota", to: "lig-msg", kind: "h" },
];

function Desenho({ descanso = 24 }: { descanso?: number }) {
  return (
    <>
      <div className={s.nota}>
        O desenho abaixo é o fluxo que roda de verdade. A fila vem do
        remarketing (lead <strong>sem dono</strong>), a corretora liga e marca
        um dos três resultados. Só o terceiro muda o lead de lugar.
      </div>
      <Canvas edges={EDGES} zoomInicial={0.85} altura="620px">
        {(reg) => (
          <div className={s.flow}>
            <div className={s.flowLabel}>LIGAÇÃO — FILA DO REMARKETING</div>
            <div className={s.row}>
              <Node reg={reg} id="lig-fila" num={1} icon="📇" titulo="Fila" largura={300}>
                <div>
                  Leads do funil de remarketing <strong>sem dono</strong>, com
                  telefone válido. Quem já atendeu alguma vez sai da fila.
                </div>
              </Node>
              <Espera reg={reg} id="lig-w" texto={`descanso de ${descanso}h entre tentativas`} />
              <Node reg={reg} id="lig-call" num={2} icon="📞" titulo="A corretora liga" cond largura={300}>
                <div>
                  Um clique por resultado. Toda tentativa vira nota na conversa
                  do lead — inclusive quando ninguém atende.
                </div>
              </Node>
            </div>

            <div className={s.faixaRotulo}>OS TRÊS RESULTADOS</div>
            <div className={s.ramos}>
              <div className={s.ramo}>
                <Node reg={reg} id="lig-nao" num={3} icon="🔕" titulo="Não atendeu" muted largura={280}>
                  <div>
                    Nota &ldquo;tentativa de ligação — não atendeu&rdquo; e o
                    contador sobe. O lead volta pra fila depois do descanso.
                  </div>
                </Node>
              </div>
              <div className={s.ramo}>
                <Node reg={reg} id="lig-sem" num={4} icon="🟡" titulo="Atendeu, não quis seguir" largura={280}>
                  <div>
                    Nota na conversa e sai da fila. Insistir em quem já disse
                    não é atropelo.
                  </div>
                </Node>
              </div>
              <div className={s.ramo}>
                <Node reg={reg} id="lig-quer" num={5} icon="⭐" titulo="Atendeu e quer continuar" largura={280}>
                  <div>Nota na conversa + adoção do lead.</div>
                </Node>
                <Node reg={reg} id="lig-adota" num={6} icon="🤝" titulo="Adoção" largura={280}>
                  <div>
                    Vai pra etapa de entrada do funil, ganha a corretora como
                    dona e recebe a tag de remarketing. Não rebaixa etapa mais
                    avançada e não tira o dono de quem já tem.
                  </div>
                </Node>
                <Node reg={reg} id="lig-msg" num={7} icon="💬" titulo="Mensagem de continuidade" largura={280}>
                  <div>
                    A tag faz a boas-vindas trocar a copy: em vez de
                    &ldquo;você acabou de se cadastrar, certo?&rdquo;, sai
                    &ldquo;acabamos de falar por telefone&rdquo;. Fica fora do
                    teste A/B de propósito.
                  </div>
                </Node>
              </div>
            </div>
          </div>
        )}
      </Canvas>
    </>
  );
}

// ── cartão de um lead na fila ───────────────────────────────────────────────
// ── 📄 Roteiro da ligação ────────────────────────────────────────────────────
//
// Vive AQUI, não num documento: roteiro que mora em arquivo ninguém abre na
// hora de discar. Fonte: projetos/acme/gestao/SCRIPT-LIGACAO.md (acervo
// comercial externo adaptado à Acme) — se mudar lá, muda aqui.
//
// Fica aberto na primeira visita e a escolha PERSISTE: quem já decorou fecha
// uma vez e não vê mais; quem está começando não precisa procurar.
//
// O objetivo do roteiro é UM: sair da ligação com permissão pra mandar no
// WhatsApp. Não é marcar visita — pedido grande em quem sumiu há meses vira
// "depois eu vejo". É o mesmo desfecho do botão "atendeu e quer continuar".

const PASSOS: Array<{ etapa: string; fala: string; nota?: string }> = [
  {
    etapa: "abre",
    fala:
      "Oi [NOME], tudo bem? Aqui é a [SEU NOME], da Acme Imóveis, de Cidade. " +
      "Você chegou pra gente [em março / quando procurava na REGIÃO] — tem uns dois minutinhos?",
    nota: "Nome da empresa logo, ancora em quando ela chegou, e pede o tempo. Quem concede o tempo fica.",
  },
  {
    etapa: "pergunta",
    fala: "Deixa eu te perguntar rapidinho: você chegou a resolver essa questão do imóvel?",
    nota: "E escuta. Se ela disser que desistiu, pergunta “o que travou?” — quase sempre é crédito, entrada ou parcela, e pra esses três a gente tem resposta.",
  },
  {
    etapa: "tria",
    fala: "E é pra você morar ou está olhando mais como investimento?",
    nota: "Morar → parcela, entrada, tempo pra mudar. Investir → aluguel da região, valorização. Trocar os dois mata a conversa.",
  },
  {
    etapa: "gancho",
    fala: "Uma frase só, ligada ao que ela acabou de falar.",
    nota: "Sem número que não esteja na planilha. Estimativa no telefone vira promessa na cabeça dela.",
  },
  {
    etapa: "pede",
    fala:
      "Posso te mandar no WhatsApp duas ou três opções que encaixam no que você falou? " +
      "Você olha com calma e me diz o que achou.",
    nota: "É o objetivo da ligação. Se topar: confirma o número e MANDA no mesmo dia.",
  },
];

const OBJECOES: Array<{ diz: string; responde: string }> = [
  {
    diz: "“não lembro de vocês”",
    responde:
      "Na época a gente se chamava Acme — mudou o nome, mesma equipe, aqui em Cidade.",
  },
  {
    diz: "“onde pegaram meu número?”",
    responde:
      "Foi você que deixou com a gente quando procurou imóvel em [MÊS]. Se preferir, tiro seu contato agora mesmo. — E se ela pedir, TIRA.",
  },
  {
    diz: "“me manda os valores primeiro”",
    responde:
      "Mando sim. Só me diz pra eu não mandar o que não serve: é pra morar ou investir?",
  },
  {
    diz: "“estou ocupada agora”",
    responde: "Prefere hoje às 17h ou amanhã de manhã? — dois horários, nunca “quando puder”.",
  },
  {
    diz: "“não tenho entrada / meu crédito não passa”",
    responde:
      "É o que mais escuto, e é o que mais mudou. Hoje tem entrada parcelada e uso de FGTS. Posso refazer sua conta?",
  },
  {
    diz: "“vou esperar melhorar”",
    responde:
      "Deixa eu te avisar quando aparecer algo na sua faixa? Você não perde nada e nem precisa responder.",
  },
  {
    diz: "“já estou com outro corretor”",
    responde:
      "Show, espero que ache uma boa. Se quiser uma segunda opinião numa proposta, me chama — faço de graça.",
  },
  {
    diz: "“não quero mais, me tira daí”",
    responde: "Tiro agora, desculpa o incômodo. — Opt-out na hora, sem tentar mais nada.",
  },
];

function Roteiro() {
  const [aberto, setAberto] = useEstadoPersistido<boolean>("ligacao_roteiro_v1", true);
  return (
    <div className={s.roteiro}>
      <button
        className={s.roteiroTopo}
        onClick={() => setAberto(!aberto)}
        aria-expanded={aberto}
      >
        <span className={s.rotulo}>roteiro</span>
        <span className={s.roteiroResumo}>
          abre · pergunta · tria · gancho · <strong>pede pra mandar no zap</strong>
        </span>
        <span className={`${s.roteiroSeta} ${aberto ? s.roteiroSetaAberta : ""}`} aria-hidden />
      </button>

      {aberto && (
        <div className={s.roteiroCorpo}>
          <p className={s.roteiroObjetivo}>
            O objetivo não é marcar visita — é sair com um{" "}
            <strong>“pode me mandar no WhatsApp”</strong>. Pedido pequeno é o que
            converte em quem sumiu faz tempo.
          </p>

          <ol className={s.roteiroPassos}>
            {PASSOS.map((p) => (
              <li key={p.etapa} className={s.roteiroPasso}>
                <span className={s.roteiroEtapa}>{p.etapa}</span>
                <span className={s.roteiroFala}>{p.fala}</span>
                {p.nota && <span className={s.roteiroNota}>{p.nota}</span>}
              </li>
            ))}
          </ol>

          {/* A trava mais importante da base da Acme: 84% cadastrou na era
              Acme. "Não lembro de vocês" não é lead ruim, é rebrand — e sem
              essa frase a ligação morre em "deve ser golpe". */}
          <div className={s.roteiroTrava}>
            <strong>Se ela disser “não lembro de vocês”:</strong> boa parte dessa
            base se cadastrou quando a imobiliária ainda se chamava{" "}
            <strong>Acme</strong>. Não é ela que está confusa — foi a gente que
            mudou de nome. Diz isso e a conversa volta.
          </div>

          <details className={s.roteiroObjecoes}>
            <summary>quando ela disser não — 8 respostas prontas</summary>
            <ul>
              {OBJECOES.map((o) => (
                <li key={o.diz}>
                  <span className={s.objDiz}>{o.diz}</span>
                  <span className={s.objResp}>{o.responde}</span>
                </li>
              ))}
            </ul>
          </details>

          <p className={s.roteiroRodape}>
            Nunca: ligar 3x no mesmo dia · falar número que não está na planilha ·
            insistir depois de um não claro · prometer e não mandar.
          </p>
        </div>
      )}
    </div>
  );
}

function CartaoLead({
  item,
  registro,
  ocupado,
  isAdmin,
  onRegistrar,
  onAgendar,
  onPerder,
  onTelefoneErrado,
  zeroDdd,
  perdido,
}: {
  item: ItemFila;
  zeroDdd: boolean;
  /** outra corretora pegou este lead depois que a lista ficou parada */
  perdido: boolean;
  registro: Registro | undefined;
  ocupado: boolean;
  isAdmin: boolean;
  onRegistrar: (estado: EstadoLigacao, observacao: string) => void;
  onAgendar: (
    data: string,
    hora: string,
    observacao: string,
    corretoraId: number | null,
  ) => Promise<boolean>;
  onPerder: (observacao: string, corretoraId: number | null) => void;
  onTelefoneErrado: (observacao: string, corretoraId: number | null) => void;
}) {
  const [obs, setObs] = useState("");
  // modal do "ligar mais tarde" — o texto da tarefa é fixo ("Ligar novamente"),
  // a corretora só diz a hora. Ela está no telefone: menos campo, mais registro.
  const [agendaOpen, setAgendaOpen] = useState(false);
  const [agData, setAgData] = useState("");
  const [agHora, setAgHora] = useState("");
  const [agErro, setAgErro] = useState<string | null>(null);
  const [agSalvando, setAgSalvando] = useState(false);

  // só o admin escolhe; corretora agenda pra si (o backend resolve pelo login)
  const [agCorr, setAgCorr] = useState<number | null>(null);

  const abrirAgenda = () => {
    const sug = sugestaoRetorno();
    setAgData(sug.data);
    setAgHora(sug.hora);
    // chute bom pro admin: quem está com o lead reservado é quem vai ligar
    setAgCorr(corretoraDoLogin(item.reservado_por));
    setAgErro(null);
    setAgendaOpen(true);
  };

  const confirmarAgenda = async () => {
    if (isAdmin && agCorr == null) {
      setAgErro("escolhe de quem é a ligação");
      return;
    }
    setAgSalvando(true);
    setAgErro(null);
    const ok = await onAgendar(agData, agHora, obs, agCorr);
    setAgSalvando(false);
    if (ok) setAgendaOpen(false);
    else setAgErro("não consegui agendar");
  };

  const atrasado =
    !!item.agendado_para && Date.parse(item.agendado_para) < Date.now();

  if (registro) {
    return (
      <div className={`${s.card} ${s.cardFeito}`}>
        <div className={s.cardTopo}>
          <span className={s.nome}>{item.nome}</span>
          <span className={s.tag}>{item.telefone}</span>
        </div>
        <div className={s.feito}>
          {registro.telefoneErrado
            ? "✓ número errado — não volta mais pra fila"
            : registro.perdida
              ? "✓ movido pra Venda perdida"
              : `✓ registrado — ${registro.estado ? ROTULO_ESTADO[registro.estado] : ""}`}
        </div>
        {registro.adotou && (
          <div className={s.feito}>
            🤝 lead adotado{registro.moveuEtapa ? " e movido pra entrada do funil" : ""}
          </div>
        )}
        {registro.jaTemDono && (
          <div className={s.aviso}>
            Esse lead já tem dona — mantivemos quem estava. Fala com a gestão
            antes de assumir.
          </div>
        )}
      </div>
    );
  }

  return (
    <div className={`${s.card} ${item.agendado_para ? s.cardAgendado : ""}`}>
      <div className={s.cardTopo}>
        <span className={s.nome}>{item.nome}</span>
        {item.telefone && perdido && (
          <span className={s.tag}>{formatarTel(item.telefone)}</span>
        )}
        {item.telefone && !perdido && (
          <a className={s.telefone} href={linkTel(item.telefone, zeroDdd)}>
            {formatarTel(item.telefone)}
          </a>
        )}
        {/* `telefone_whatsapp` é o número EXATO da conversa (jid). Quando ele
            existe e difere do cadastro, é ele que abre — foi o caso do
            Reinaldo em 20/09: cadastro com DDD 15, WhatsApp real com DDD 55, e
            o link do cadastro dava "não tem WhatsApp". */}
        {!perdido && (item.telefone_whatsapp || item.telefone) &&
          linkWhatsApp(item.telefone_whatsapp || item.telefone) && (
          <a
            className={s.zap}
            href={linkWhatsApp(item.telefone_whatsapp || item.telefone)}
            target="_blank"
            rel="noreferrer"
            title={
              item.telefone_whatsapp
                ? "Abre a conversa no WhatsApp — a chamada é o ícone de telefone lá dentro"
                : "Sem conversa registrada: este número pode não ter WhatsApp"
            }
          >
            💬 WhatsApp
            {/* Sem histórico, o número é PALPITE do cadastro — pode não ter
                WhatsApp (503 leads do remarketing estão nessa situação). O "?"
                avisa antes do clique, em vez de ela descobrir na tela do
                WhatsApp dizendo que o número não existe. */}
            {!item.telefone_whatsapp && <span className={s.zapIncerto}>?</span>}
          </a>
        )}
        {zeroDdd && paraDiscarChipVirtual(item.telefone) && (
          <span
            className={s.chipVirtual}
            title="Chip virtual (Salvy) não completa com +55 — disque este"
          >
            chip virtual: {paraDiscarChipVirtual(item.telefone)}
          </span>
        )}
        {item.agendado_para && (
          <span
            className={atrasado ? s.tagAgendadoAtrasado : s.tagAgendado}
            title={
              atrasado
                ? `Retorno combinado para ${rotuloAgendado(item.agendado_para)} — já passou da hora`
                : `Retorno combinado para ${rotuloAgendado(item.agendado_para)}`
            }
          >
            🕒 ligar mais tarde · {rotuloAgendado(item.agendado_para)}
          </span>
        )}
        {item.tentativa_atual > 1 && (
          <span className={s.tagTentativa}>{item.tentativa_atual}ª tentativa</span>
        )}
        {item.entrou_em && (
          <span
            className={s.tag}
            title={`Cliente entrou no sistema em ${new Date(item.entrou_em).toLocaleDateString("pt-BR")} (data do Kommo)`}
          >
            entrou {rotuloEntrada(item.entrou_em)}
          </span>
        )}
        {item.etapa && <span className={s.tag}>{item.etapa}</span>}
        {item.dias_parado != null && (
          <span className={s.tag}>parado há {item.dias_parado} dias</span>
        )}
        {item.origem && <span className={s.tag}>{item.origem}</span>}
      </div>

      {/* A reserva desta lista venceu e outra corretora pegou o lead (ou já
          registrou a ligação). O telefone some de propósito: ligar daqui é
          ligar de novo pra quem acabou de ser ligado. Os botões de registro
          ficam — se ela JÁ ligou antes, a baixa ainda é dela (caso Corretor, 22/09). */}
      {perdido && (
        <div className={s.aviso}>
          Outra corretora pegou este lead enquanto sua lista estava parada.
          <strong> Não ligue de novo.</strong> Se você já tinha ligado, registre
          o resultado aqui embaixo.
        </div>
      )}

      {item.resumo && (
        <div className={s.contexto}>
          <span className={s.contextoRotulo}>procurava</span>
          {item.resumo}
        </div>
      )}
      {item.ultima_msg && (
        <div className={s.contexto}>
          <span className={s.contextoRotulo}>última conversa</span>
          {item.ultima_msg}
        </div>
      )}

      <div className={s.acoes}>
        <button
          className={s.botao}
          disabled={ocupado}
          onClick={() => onRegistrar("nao_atendeu", obs)}
        >
          🔕 não atendeu
        </button>
        <button
          className={`${s.botao} ${s.botaoSemContinuar}`}
          disabled={ocupado}
          onClick={() => onRegistrar("atendeu_sem_continuar", obs)}
        >
          🟡 atendeu, não quis seguir
        </button>
        <button
          className={`${s.botao} ${s.botaoQuerContinuar}`}
          disabled={ocupado}
          onClick={() => onRegistrar("atendeu_quer_continuar", obs)}
        >
          ⭐ atendeu e quer continuar
        </button>
        {/* "me liga mais tarde": NÃO registra resultado — o lead continua na
            fila (agora no topo, na hora marcada). Registrar "atendeu" aqui o
            tiraria da fila, que é o contrário do que a pessoa pediu. */}
        {/* Agendar re-reserva o lead SEM checar de quem ele está agora
            (`agendar_retorno`): no cartão perdido isso topessoa o lead de outra. */}
        {!perdido && (
          <button
            className={`${s.botao} ${s.botaoMaisTarde}`}
            disabled={ocupado}
            onClick={abrirAgenda}
            title="A pessoa atendeu e pediu pra ligar em outro horário"
          >
            🕒 ligar mais tarde
          </button>
        )}
        {/* Número errado: marca o telefone como inválido e tira o lead da fila
            pra sempre. Existe porque sem ele o único botão possível era "não
            atendeu" — e aí o lead voltava em 24h, pra sempre, com o mesmo
            número quebrado. Confirma antes: é o único botão daqui que some com
            o lead sem ninguém ter falado com a pessoa. */}
        <button
          className={`${s.botao} ${s.botaoNumeroErrado}`}
          disabled={ocupado}
          onClick={() => {
            if (
              window.confirm(
                `Número errado de ${item.nome}?\n\n${formatarTel(item.telefone)}\n\n` +
                  "O telefone fica marcado como inválido e o lead NÃO volta mais " +
                  "pra fila de ligação. A gestão pode desfazer.",
              )
            ) {
              onTelefoneErrado(obs, corretoraDoLogin(item.reservado_por));
            }
          }}
          title="O número não existe, está incompleto ou é de outra pessoa"
        >
          📵 número errado
        </button>
        {/* Venda perdida: sai da fila e vai pro funil principal, com dona.
            Sem dona o card sumiria do board — o backend recusa nesse caso. */}
        <button
          className={`${s.botao} ${s.botaoPerdida}`}
          disabled={ocupado}
          onClick={() => onPerder(obs, corretoraDoLogin(item.reservado_por))}
          title="Manda o lead pra etapa Venda perdida do funil"
        >
          ❌ venda perdida
        </button>
        <input
          className={s.obs}
          value={obs}
          onChange={(e) => setObs(e.target.value)}
          placeholder="anotação (opcional) — o que a pessoa falou"
        />
      </div>

      {agendaOpen && (
        <div className={s.modalOverlay} onClick={() => setAgendaOpen(false)}>
          <div className={s.modalBox} onClick={(e) => e.stopPropagation()}>
            <div className={s.modalTitulo}>🕒 Ligar mais tarde · {item.nome}</div>
            {/* texto travado: é sempre a mesma tarefa, e é o que a fila procura */}
            <div className={s.modalTarefa}>
              <span className={s.modalTarefaRotulo}>tarefa</span>
              Ligar novamente
            </div>
            <div className={s.modalRow}>
              <label>quando</label>
              <input
                type="date"
                value={agData}
                onChange={(e) => setAgData(e.target.value)}
              />
              <input
                type="time"
                value={agHora}
                onChange={(e) => setAgHora(e.target.value)}
                autoFocus
              />
            </div>
            {/* Admin não é corretora: sem escolher, a tarefa nasceria sem dona
                e sumiria da aba de tarefas sob qualquer pill. */}
            {isAdmin && (
              <div className={s.modalRow}>
                <label>de quem</label>
                <select
                  value={agCorr ?? ""}
                  onChange={(e) =>
                    setAgCorr(e.target.value ? Number(e.target.value) : null)
                  }
                >
                  <option value="">escolher…</option>
                  {CORRETORAS_FILA.map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.nome}
                    </option>
                  ))}
                </select>
              </div>
            )}
            <div className={s.modalNota}>
              O lead continua nesta fila e sobe pro topo na hora marcada.
            </div>
            {agErro && <div className={s.modalErro}>{agErro}</div>}
            <div className={s.modalAcoes}>
              <button className={s.botao} onClick={() => setAgendaOpen(false)}>
                cancelar
              </button>
              <button
                className={`${s.botao} ${s.botaoQuerContinuar}`}
                onClick={confirmarAgenda}
                disabled={agSalvando || !agData || !agHora}
              >
                {agSalvando ? "agendando…" : "agendar retorno"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

export default function JanelaLigacao({
  isAdmin,
  onToast,
}: {
  isAdmin: boolean;
  onToast: (msg: string, erro?: boolean) => void;
}) {
  const [fila, setFila] = useState<Fila | null>(null);
  const [met, setMet] = useState<Metricas | null>(null);
  const [erro, setErro] = useState<string | null>(null);
  // Registrar NÃO recarrega a lista nem perde a posição: o resultado fica aqui
  // e o cartão troca de estado no lugar.
  const [registros, setRegistros] = useState<Record<number, Registro>>({});
  const [ocupado, setOcupado] = useState<Set<number>>(new Set());
  // meta vive à parte da fila: o registro atualiza ela sem recarregar a lista
  // (recarregar faria a corretora perder a posição em que estava)
  const [meta, setMeta] = useState<Meta | null>(null);
  const [confete, setConfete] = useState(false);
  const [adiantando, setAdiantando] = useState(false);
  // Leads da tela que não são mais desta corretora: a reserva venceu com a
  // lista parada e outra pessoa pegou (ou registrou). Ver `renovar`.
  const [perdidos, setPerdidos] = useState<Set<number>>(new Set());

  const carregar = useCallback(async () => {
    try {
      const r = await fetch(`/api/ligacoes/fila?limite=${LIMITE}`, { cache: "no-store" });
      const data = await r.json();
      if (!r.ok) throw new Error(data.error ?? `http_${r.status}`);
      setFila(data);
      setMeta(data.meta ?? null);
      setPerdidos(new Set()); // lista nova: tudo nela acabou de ser reservado
      setErro(null);
    } catch (e) {
      setErro(e instanceof Error ? e.message : "erro_desconhecido");
    }
  }, []);

  const carregarMetricas = useCallback(async () => {
    if (!isAdmin) return;
    try {
      const r = await fetch("/api/tarefas/metricas-ligacao?dias=30", { cache: "no-store" });
      if (!r.ok) return; // métrica é extra: sem ela a fila continua utilizável
      setMet(await r.json());
    } catch {
      /* silencioso de propósito — a fila é o que a corretora precisa */
    }
  }, [isAdmin]);

  useEffect(() => {
    carregar();
    carregarMetricas();
  }, [carregar, carregarMetricas]);

  // ── a lista aberta continua sendo dela ──────────────────────────────────
  // A fila é carregada UMA vez (recarregar perderia a posição), mas a reserva
  // vence em 30 min. Sem renovar, a lista parada na tela deixava de ser dela
  // sem aviso: em 22/09 a Corretor voltou pra dar baixa numa lista da véspera, e a
  // fila da Pessoa7 já tinha pegado e ligado pros mesmos leads. Aqui a aba
  // renova a reserva enquanto está visível e, quando a corretora volta pra
  // ela, descobre na hora o que saiu — em vez de descobrir recarregando.
  const idsNaTela = useRef<number[]>([]);
  idsNaTela.current = (fila?.itens ?? [])
    .map((i) => i.lead_id)
    // registrado/perdido já largou a reserva de propósito: não é "perdido"
    .filter((id) => !registros[id]);

  const renovar = useCallback(async () => {
    const ids = idsNaTela.current;
    if (ids.length === 0) return;
    try {
      const r = await fetch("/api/ligacoes/renovar", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ lead_ids: ids }),
      });
      if (!r.ok) return; // renovar é proteção extra: falhou, a lista segue
      const data = await r.json().catch(() => null);
      if (!data || !Array.isArray(data.perdidos)) return;
      setPerdidos((prev) => {
        const n = new Set(prev);
        for (const id of data.perdidos as number[]) n.add(id);
        return n.size === prev.size ? prev : n;
      });
    } catch {
      /* silencioso: sem rede agora, tenta de novo na próxima volta */
    }
  }, []);

  useEffect(() => {
    const t = setInterval(() => {
      if (document.visibilityState === "visible") renovar();
    }, RENOVAR_A_CADA_MS);
    const aoVoltar = () => {
      if (document.visibilityState === "visible") renovar();
    };
    document.addEventListener("visibilitychange", aoVoltar);
    return () => {
      clearInterval(t);
      document.removeEventListener("visibilitychange", aoVoltar);
    };
  }, [renovar]);

  const registrar = useCallback(
    async (item: ItemFila, estado: EstadoLigacao, observacao: string) => {
      setOcupado((s0) => new Set(s0).add(item.lead_id));
      try {
        const r = await fetch("/api/ligacoes/registrar", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            lead_id: item.lead_id,
            resultado: estado,
            observacao: observacao.trim() || null,
          }),
        });
        const data = await r.json().catch(() => ({}));
        if (!r.ok || data.ok !== true) {
          throw new Error(
            typeof data.detail === "string" ? data.detail : data.error ?? `http_${r.status}`,
          );
        }
        // ENFEITE, nunca bloqueio: a ligação já foi gravada quando isto roda.
        if (data.meta) setMeta(data.meta);
        if (data.confete === true) setConfete(true);
        const ad = data.adocao ?? null;
        setRegistros((prev) => ({
          ...prev,
          [item.lead_id]: {
            estado,
            adotou: Boolean(ad?.ok && ad?.definiu_dono),
            moveuEtapa: Boolean(ad?.moveu_etapa),
            jaTemDono: Boolean(ad?.ja_tem_dono),
          },
        }));
        onToast(`${item.nome}: ${ROTULO_ESTADO[estado]} ✓`);
      } catch (e) {
        onToast(
          `não consegui registrar (${e instanceof Error ? e.message : "erro"})`,
          true,
        );
      } finally {
        setOcupado((s0) => {
          const n = new Set(s0);
          n.delete(item.lead_id);
          return n;
        });
      }
    },
    [onToast],
  );

  // "ligar mais tarde": agenda o retorno e MANTÉM o cartão na fila. O item é
  // atualizado no lugar e a lista reordena na hora — sem refetch, que faria a
  // corretora perder a posição no meio de uma sessão de ligações.
  const agendar = useCallback(
    async (
      item: ItemFila,
      data: string,
      hora: string,
      observacao: string,
      corretoraId: number | null,
    ) => {
      setOcupado((s0) => new Set(s0).add(item.lead_id));
      try {
        const r = await fetch("/api/ligacoes/agendar", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            lead_id: item.lead_id,
            data,
            hora,
            observacao: observacao.trim() || null,
            corretora_id: corretoraId,
          }),
        });
        const resp = await r.json().catch(() => ({}));
        if (!r.ok || resp.ok !== true) {
          throw new Error(
            typeof resp.detail === "string" ? resp.detail : resp.error ?? `http_${r.status}`,
          );
        }
        setFila((f) =>
          f
            ? {
                ...f,
                itens: ordenarFila(
                  f.itens.map((i) =>
                    i.lead_id === item.lead_id
                      ? { ...i, agendado_para: resp.agendado_para }
                      : i,
                  ),
                ),
              }
            : f,
        );
        onToast(`${item.nome}: retorno marcado pra ${rotuloAgendado(resp.agendado_para)} ✓`);
        return true;
      } catch (e) {
        onToast(
          `não consegui agendar (${e instanceof Error ? e.message : "erro"})`,
          true,
        );
        return false;
      } finally {
        setOcupado((s0) => {
          const n = new Set(s0);
          n.delete(item.lead_id);
          return n;
        });
      }
    },
    [onToast],
  );

  // "venda perdida": move o lead pro funil principal e tira ele da fila. O
  // cartão vira "movido pra Venda perdida" no lugar, sem recarregar a lista —
  // mesma escolha dos três botões de registro (recarregar faria a corretora
  // perder a posição no meio de uma sessão de ligações).
  const perder = useCallback(
    async (item: ItemFila, observacao: string, corretoraId: number | null) => {
      setOcupado((s0) => new Set(s0).add(item.lead_id));
      try {
        const r = await fetch("/api/ligacoes/perder", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            lead_id: item.lead_id,
            motivo: observacao.trim() || null,
            corretora_id: corretoraId,
          }),
        });
        const resp = await r.json().catch(() => ({}));
        if (!r.ok || resp.ok !== true) {
          throw new Error(
            typeof resp.detail === "string" ? resp.detail : resp.error ?? `http_${r.status}`,
          );
        }
        setRegistros((prev) => ({
          ...prev,
          [item.lead_id]: {
            estado: null,
            adotou: false,
            moveuEtapa: true,
            jaTemDono: false,
            perdida: true,
          },
        }));
        onToast(`${item.nome}: venda perdida ✓`);
      } catch (e) {
        const msg = e instanceof Error ? e.message : "erro";
        onToast(
          msg === "sem_corretora_alvo"
            ? "esse lead não tem dona — entra com o login da corretora pra marcar"
            : `não consegui marcar (${msg})`,
          true,
        );
      } finally {
        setOcupado((s0) => {
          const n = new Set(s0);
          n.delete(item.lead_id);
          return n;
        });
      }
    },
    [onToast],
  );

  // "número errado": marca o telefone como inválido no cadastro e tira o lead
  // da fila pra sempre. NÃO registra resultado de ligação de propósito — o
  // backend também não grava `ligacao_resultado` (ver `marcar_telefone_errado`):
  // número que não completa a chamada não é "não atendeu", e contar como tal
  // afundava a taxa de atendimento da corretora com um furo de cadastro.
  const telefoneErrado = useCallback(
    async (item: ItemFila, observacao: string, corretoraId: number | null) => {
      setOcupado((s0) => new Set(s0).add(item.lead_id));
      try {
        const r = await fetch("/api/ligacoes/telefone-errado", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            lead_id: item.lead_id,
            observacao: observacao.trim() || null,
            corretora_id: corretoraId,
          }),
        });
        const resp = await r.json().catch(() => ({}));
        if (!r.ok || resp.ok !== true) {
          throw new Error(
            typeof resp.detail === "string" ? resp.detail : resp.error ?? `http_${r.status}`,
          );
        }
        setRegistros((prev) => ({
          ...prev,
          [item.lead_id]: {
            estado: null,
            adotou: false,
            moveuEtapa: false,
            jaTemDono: false,
            telefoneErrado: true,
          },
        }));
        onToast(`${item.nome}: número errado ✓ — não volta mais`);
      } catch (e) {
        const msg = e instanceof Error ? e.message : "erro";
        onToast(`não consegui marcar (${msg})`, true);
      } finally {
        setOcupado((s0) => {
          const n = new Set(s0);
          n.delete(item.lead_id);
          return n;
        });
      }
    },
    [onToast],
  );

  const adiantar = useCallback(async () => {
    setAdiantando(true);
    try {
      const r = await fetch("/api/ligacoes/adiantar", { method: "POST" });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(data.detail ?? data.error ?? `http_${r.status}`);
      setMeta(data.progresso ?? null);
      await carregar(); // o bloco novo traz leads novos reservados pra ela
      onToast("bloco de amanhã liberado — bora 💪");
    } catch (e) {
      onToast(
        `não consegui liberar (${e instanceof Error ? e.message : "erro"})`,
        true,
      );
    } finally {
      setAdiantando(false);
    }
  }, [carregar, onToast]);

  const ref = fila?.referencia_historica ?? met?.referencia_historica ?? null;

  return (
    <div className={s.wrap}>
      <div className={s.cabecalho}>
        <h2 className={s.titulo}>📞 Janela de ligação</h2>
        <p className={s.sub}>
          <span className={s.rotulo}>fila</span>
          Leads do remarketing que ainda <strong>não têm dono</strong>. Você
          liga, marca o resultado num clique — e quando a pessoa quer continuar,
          o lead passa a ser seu.
          {fila ? ` ${fila.total} na base.` : ""}
          {fila?.reserva_minutos
            ? ` Esta lista é sua enquanto a aba estiver aberta. Parada por mais de ${fila.reserva_minutos} min, outra corretora pode pegar os leads — e o cartão avisa.`
            : ""}
        </p>

        {ref && (
          <div className={s.referencia}>
            <div className={s.referenciaTopo}>
              <span className={s.rotulo}>referência histórica</span>
              <span className={s.referenciaNumero}>{ref.resumo}</span>
              <span className={s.statSub}>
                ({ref.ligacoes} ligações em {ref.periodo})
              </span>
            </div>
            <p className={s.referenciaNota}>{ref.nota}</p>
          </div>
        )}

        <Roteiro />
      </div>

      {confete && (
        <>
          <Confete aoFim={() => setConfete(false)} />
          <div className={s.parabens} role="status">
            <div className={s.parabensTitulo}>🎉 Sua tarefa de hoje foi concluída!</div>
            <div className={s.parabensSub}>
              {meta && !meta.concluiu_semana
                ? `Faltam ${meta.faltam_semana} ligações pra fechar as ${meta.meta_semanal} da semana.`
                : "E a semana já está fechada. 👏"}
            </div>
          </div>
        </>
      )}

      {meta && (
        <MetaDoDia meta={meta} adiantando={adiantando} onAdiantar={adiantar} />
      )}

      {isAdmin && met && <Metrica met={met} />}

      {erro && <div className={s.erro}>Não consegui carregar a fila ({erro}).</div>}

      {fila && fila.itens.length === 0 && !erro && (
        <div className={s.vazio}>
          <div style={{ fontSize: 40, marginBottom: 10 }}>📞</div>
          <strong style={{ color: "#15110d", fontSize: 17 }}>
            {meta?.concluiu_dia
              ? "Bloco de hoje concluído"
              : "Fila de ligação vazia"}
          </strong>
          <div style={{ marginTop: 8 }}>
            {meta?.concluiu_dia
              ? "Você fez as ligações de hoje. Se quiser adiantar, é só usar o botão acima."
              : "Ou a base do remarketing já foi toda trabalhada, ou quem sobrou levou uma tentativa nas últimas horas e está descansando."}
          </div>
        </div>
      )}

      {perdidos.size > 0 && (
        <div className={s.aviso} role="status">
          {perdidos.size === 1
            ? "1 lead desta lista foi pego por outra corretora"
            : `${perdidos.size} leads desta lista foram pegos por outra corretora`}{" "}
          enquanto ela estava parada — estão marcados abaixo. Registre só o que
          você já ligou.{" "}
          <button type="button" onClick={() => carregar()}>
            carregar lista nova
          </button>
        </div>
      )}

      {fila && fila.itens.length > 0 && (
        <div className={s.lista}>
          {fila.itens.map((item) => (
            <CartaoLead
              key={item.lead_id}
              item={item}
              registro={registros[item.lead_id]}
              ocupado={ocupado.has(item.lead_id)}
              onRegistrar={(estado, obs) => registrar(item, estado, obs)}
              isAdmin={isAdmin}
              onAgendar={(data, hora, obs, cid) =>
                agendar(item, data, hora, obs, cid)
              }
              onPerder={(obs, cid) => perder(item, obs, cid)}
              onTelefoneErrado={(obs, cid) => telefoneErrado(item, obs, cid)}
              zeroDdd={fila.discagem === "zero_ddd"}
              perdido={perdidos.has(item.lead_id)}
            />
          ))}
        </div>
      )}

      <Desenho />
    </div>
  );
}
