"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { useEstadoPersistido } from "@/app/lib/estado-persistido";
import s from "./meu-dia.module.css";

// 👋 Meu dia — cliente.
//
// REGRA QUE NÃO SE MEXE: a ordem dos blocos é por CUSTO DE DEMORA e vem PRONTA
// do backend (/painel/meu-dia). Este componente renderiza `blocos` na ordem em
// que chegaram — nada de sort aqui.
//
// "Sem dado" ≠ zero: bloco com `status: "erro"` NUNCA vira "0" nem conta pro
// confete; ele diz que falhou e oferece recarregar.

type Desde = { iso: string | null; horas: number | null; rotulo: string | null };

type Item = {
  lead_id: string | null;
  nome: string;
  telefone: string | null;
  corretora: string | null;
  preview: string | null;
  contexto: string | null;
  desde: Desde;
  motivo?: string;
  duvida?: boolean;
};

type Meta = {
  meta_diaria: number;
  feitas_hoje: number;
  restante_hoje: number;
  meta_semanal?: number;
  feitas_semana?: number;
} | null;

type Bloco = {
  chave:
    | "responderam"
    | "perguntas"
    | "primeiro_contato"
    | "tarefas"
    // 🔁 conversa do mês que esfriou e não morreu (17/08). Vem ANTES das
    // ligações: aqui é gente que falou com a corretora esta semana, a ligação
    // é base fria de remarketing. Lead morno de 4 dias volta mais fácil que
    // lead de 6 meses.
    | "retomar"
    | "ligacoes";
  icone: string;
  titulo: string;
  status: "ok" | "erro";
  erro: string | null;
  total: number | null;
  itens: Item[];
  href?: string;
  meta?: Meta;
  sem_meta?: boolean;
  encerramentos_filtrados?: number;
  /** quantas saíram do bloco por "feito" (o lead encerrou a conversa) */
  encerradas_manualmente?: number;
};

type MeuDia = {
  viewer: { user: string; corretora_label: string | null };
  pode_filtrar: boolean;
  corretoras: Array<{ id: number; label: string; nome: string | null }>;
  filtro: string | null;
  dono: string | null;
  data: string;
  gerado_em: string;
  blocos: Bloco[];
  tudo_ok: boolean;
  tudo_limpo: boolean;
  pode_comemorar: boolean;
  pode_agir: boolean;
};

const REFRESH_MS = 90_000;
const CORES = ["#0a5c46", "#EA2871", "#FFD413", "#064794", "#f08c2e"];

// Confete uma vez por dia por usuário — reencontrar a tela limpa cinco vezes
// na mesma tarde não é conquista cinco vezes.
function chaveConfete(user: string, data: string) {
  return `meu-dia:confete:${user || "anon"}:${data}`;
}

function Confete({ aoFim }: { aoFim: () => void }) {
  useEffect(() => {
    const t = setTimeout(aoFim, 4200);
    return () => clearTimeout(t);
  }, [aoFim]);
  const pecas = Array.from({ length: 40 }, (_, i) => ({
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

function dataLonga(iso: string): string {
  // "2026-08-05" → "terça, 5 de agosto" (sem depender do fuso do browser:
  // monta a data em UTC a partir das partes)
  const [a, m, d] = iso.split("-").map(Number);
  const dt = new Date(Date.UTC(a, (m || 1) - 1, d || 1));
  const semana = dt.toLocaleDateString("pt-BR", { weekday: "long", timeZone: "UTC" });
  const resto = dt.toLocaleDateString("pt-BR", { day: "numeric", month: "long", timeZone: "UTC" });
  return `${semana.replace("-feira", "")}, ${resto}`;
}

export default function ClienteMeuDia({ user, nome }: { user: string; nome: string }) {
  const router = useRouter();
  const [dados, setDados] = useState<MeuDia | null>(null);
  const [erro, setErro] = useState<string | null>(null);
  const [carregando, setCarregando] = useState(true);
  const [aberto, setAberto] = useState<string | null>(null);
  const [confete, setConfete] = useState(false);
  // Filtro de pessoa (admin) — MESMO padrão da tela de Tarefas: guarda o NOME
  // do DB ("Pessoa1"), sobrevive à navegação e vai pro SERVIDOR no
  // ?corretora=. Peneirar no front não serviria: os totais e a meta de ligação
  // são calculados lá.
  const [corrFiltro, setCorrFiltro] = useEstadoPersistido<string | null>(
    "meu_dia_corretora_v1",
    null,
  );
  // Quem pode filtrar + a lista de pessoas fica FORA de `dados` de propósito:
  // ao trocar de pessoa `dados` vira null enquanto carrega, e as pills não
  // podem sumir debaixo do dedo de quem acabou de clicar.
  const [podeFiltrar, setPodeFiltrar] = useState(false);
  const [corretoras, setCorretoras] = useState<MeuDia["corretoras"]>([]);

  const carregar = useCallback(async (corr: string | null = corrFiltro) => {
    setCarregando(true);
    try {
      const qs = corr ? `&corretora=${encodeURIComponent(corr)}` : "";
      const r = await fetch(`/api/meu-dia?limite=25${qs}`, { cache: "no-store" });
      const body = await r.json();
      if (!r.ok) throw new Error(body?.detail || body?.error || `HTTP ${r.status}`);
      const md = body as MeuDia;
      setDados(md);
      setPodeFiltrar(Boolean(md.pode_filtrar));
      setCorretoras(md.corretoras ?? []);
      setErro(null);
    } catch (e) {
      // Falha de rede/copiloto NÃO vira tela vazia: some com os números e diz
      // o que houve. Mostrar "0 pendências" porque a API caiu seria mentira.
      setDados(null);
      setErro(String((e as Error).message || e));
    } finally {
      setCarregando(false);
    }
  }, [corrFiltro]);

  useEffect(() => {
    carregar();
    const t = setInterval(() => {
      if (document.visibilityState === "visible") carregar();
    }, REFRESH_MS);
    return () => clearInterval(t);
  }, [carregar]);

  // Troca de pessoa: zera a tela e recarrega do servidor. Some com os números
  // antigos primeiro — número da Pessoa10 embaixo do título "Pessoa11" é pior que
  // "Carregando…".
  const trocarCorretora = useCallback(
    (nome: string | null) => {
      setCorrFiltro(nome);
      setDados(null);
      setAberto(null);
      carregar(nome);
    },
    [carregar, setCorrFiltro],
  );

  // Confete: só com tudo VERIFICADO e zerado (tudo_limpo já exige tudo_ok no
  // backend), uma vez por dia, e SÓ pra quem trabalha a fila. Admin vê o estado
  // limpo — a comemoração é de quem limpou (`pode_comemorar` vem do copiloto).
  useEffect(() => {
    if (!dados?.tudo_limpo || !dados.pode_comemorar) return;
    const chave = chaveConfete(user, dados.data);
    try {
      if (localStorage.getItem(chave)) return;
      localStorage.setItem(chave, "1");
    } catch {
      /* modo privado: mostra o confete assim mesmo, é enfeite */
    }
    setConfete(true);
  }, [dados, user]);

  const irPara = (item: Item) => {
    if (!item.lead_id) return;
    router.push(`/chat?lead=${encodeURIComponent(item.lead_id)}`);
  };

  // "feito": às vezes quem encerra a conversa é o LEAD ("fechado, obrigada!") e
  // não há o que responder. Some da lista sem exigir que a corretora mande um
  // joinha só pra limpar a tela. Some NA HORA (otimista) e não volta no refresh
  // — o backend guarda o instante da última mensagem; se a pessoa escrever de
  // novo, aí sim a conversa reaparece sozinha.
  const [encerrando, setEncerrando] = useState<Set<string>>(new Set());
  const marcarFeito = useCallback(
    async (item: Item) => {
      const tel = item.telefone;
      if (!tel) return;
      setEncerrando((s0) => new Set(s0).add(tel));
      // guarda pra restaurar se o servidor recusar
      const antes = dados;
      setDados((d) =>
        d
          ? {
              ...d,
              blocos: d.blocos.map((b) =>
                b.chave === "responderam" ||
                b.chave === "perguntas" ||
                b.chave === "primeiro_contato"
                  ? {
                      ...b,
                      itens: b.itens.filter((i) => i.telefone !== tel),
                      total: Math.max(0, (b.total ?? 1) - 1),
                    }
                  : b,
              ),
            }
          : d,
      );
      try {
        const r = await fetch("/api/conversa/encerrar", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ telefone: tel }),
        });
        const body = await r.json().catch(() => ({}));
        if (!r.ok || body?.ok !== true) {
          throw new Error(body?.detail || body?.error || `HTTP ${r.status}`);
        }
      } catch {
        setDados(antes); // não marcou: devolve o item pra lista
      } finally {
        setEncerrando((s0) => {
          const n = new Set(s0);
          n.delete(tel);
          return n;
        });
      }
    },
    [dados],
  );

  const pill = (ativo: boolean) => `${s.pill} ${ativo ? s.pillAtiva : ""}`;

  // Blocos com fila pendente, na ordem que veio do backend. O PRIMEIRO ganha o
  // selo "comece por aqui" e o número grande — a página tem um herói só, e ele
  // é escolhido por custo de demora (a ordem), nunca pelo número maior.
  const pendentes = (dados?.blocos ?? []).filter(
    (b) => b.status === "ok" && (b.total ?? 0) > 0,
  );
  const heroi = pendentes[0]?.chave ?? null;
  // Progresso do dia: quantas das filas já estão limpas. É a única medida
  // honesta aqui — "37 de quantos?" não tem denominador estável. Bloco com erro
  // fica FORA das duas contas: não está limpo nem sujo, está desconhecido.
  const verificados = (dados?.blocos ?? []).filter((b) => b.status === "ok");
  const limpas = verificados.filter((b) => (b.total ?? 0) === 0).length;

  return (
    <div className={s.wrap}>
      <div className={s.cabeca}>
        {/* De quem é a tela. `dono` vem do copiloto: o rótulo da corretora
            filtrada, "equipe" pro admin sem filtro, ou a própria corretora.
            Não pode haver dúvida de quem é o dia que está na tela. */}
        <h1 className={s.titulo}>Meu dia</h1>
        <span className={s.data}>
          {dados?.dono ?? nome}
          {dados ? ` · ${dataLonga(dados.data)}` : ""}
        </span>
      </div>

      {/* Pills de pessoa — SÓ admin (`pode_filtrar` do copiloto). Corretora
          nunca vê, e mesmo que batesse na API com ?corretora= o copiloto
          ignora: o escopo é dela. */}
      {podeFiltrar && (
        <div className={s.pills}>
          <button
            onClick={() => trocarCorretora(null)}
            className={pill(corrFiltro === null)}
            title="O dia da equipe inteira, somado"
          >
            Todas
          </button>
          {corretoras.map((c) => (
            <button
              key={c.id}
              onClick={() => trocarCorretora(c.nome ?? c.label)}
              className={pill(corrFiltro === (c.nome ?? c.label))}
              title={`Ver o dia de ${c.label} como ela vê`}
            >
              {c.label}
            </button>
          ))}
          {corrFiltro && dados?.dono && (
            <span className={s.pillNota}>
              você está vendo o dia de {dados.dono} — supervisão, não é a sua fila
            </span>
          )}
        </div>
      )}

      {erro && (
        <div className={s.avisoErro}>
          Não consegui carregar seu dia. Isso <strong>não</strong> quer dizer que
          está tudo em dia — quer dizer que não deu pra saber.{" "}
          <button className={s.btnRecarregar} onClick={() => carregar()}>
            tentar de novo
          </button>
          {/* A mensagem crua da API é pro dev, não pra corretora — fica a um
              clique, sem ocupar a tela da manhã dela. */}
          <details className={s.detalheTecnico}>
            <summary>detalhe técnico</summary>
            {erro}
          </details>
        </div>
      )}

      {!dados && !erro && carregando && <p className={s.sub}>Carregando…</p>}

      {dados && (
        <>
          {/* Progresso some quando tudo zerou: o cartão de dia limpo já conta
              essa história, e "5 de 5" embaixo dele é a mesma frase duas vezes. */}
          {!dados.tudo_limpo && verificados.length > 0 && (
            <div className={s.progresso}>
              <div className={s.progressoCabeca}>
                <span>SEU DIA</span>
                <span>
                  {limpas} de {verificados.length}{" "}
                  {limpas === 1 ? "fila limpa" : "filas limpas"}
                </span>
              </div>
              <div className={s.progressoTrilho}>
                <div
                  className={s.progressoValor}
                  style={{ width: `${(limpas / verificados.length) * 100}%` }}
                />
              </div>
            </div>
          )}

          {/* Com tudo zerado a lista inteira sai de cena: cinco ✓ seguidos de um
              cartão "seu dia está limpo" contam a mesma coisa seis vezes. */}
          {!dados.tudo_limpo && (
          <div className={s.lista}>
            {dados.blocos.map((b) => {
              const falhou = b.status === "erro";
              const zerado = !falhou && (b.total ?? 0) === 0;
              const expansivel = !falhou && b.itens.length > 0;
              const estaAberto = aberto === b.chave;
              return (
                <div
                  key={b.chave}
                  className={[
                    s.bloco,
                    falhou ? s.falhou : "",
                    zerado ? s.zerado : "",
                    heroi === b.chave ? s.prioritario : "",
                  ]
                    .filter(Boolean)
                    .join(" ")}
                >
                  {heroi === b.chave && <span className={s.selo}>comece por aqui</span>}
                  <button
                    className={s.linha}
                    disabled={!expansivel && !b.href}
                    onClick={() => {
                      if (expansivel) setAberto(estaAberto ? null : b.chave);
                      else if (b.href) router.push(b.href);
                    }}
                  >
                    {falhou ? (
                      <span className={s.rotuloErro}>
                        {b.titulo} — não consegui consultar agora
                      </span>
                    ) : (
                      <>
                        <span className={s.numero}>{b.total}</span>
                        <span className={s.rotulo}>
                          {b.titulo}
                          {/* "a Pessoa11 já fez quantas das 25?" — o progresso só
                              aparece quando existe meta (corretora, ou admin
                              olhando uma pessoa específica). */}
                          {b.chave === "ligacoes" && b.meta && (
                            <span className={s.rotuloExtra}>
                              {b.meta.feitas_hoje} de {b.meta.meta_diaria} feitas hoje
                            </span>
                          )}
                        </span>
                      </>
                    )}
                    {/* O atalho é de quem LIGA. Admin supervisionando não tem
                        fila própria — mandá-lo pra /tarefas?aba=ligar abriria
                        uma janela vazia. */}
                    {!falhou && dados.pode_agir && b.chave === "ligacoes" && (b.total ?? 0) > 0 && (
                      <span
                        className={s.acao}
                        onClick={(e) => {
                          e.stopPropagation();
                          router.push(b.href || "/tarefas?aba=ligar");
                        }}
                      >
                        começar
                      </span>
                    )}
                    {expansivel && (
                      <span
                        className={`${s.chevron} ${estaAberto ? s.chevronAberto : ""}`}
                        aria-hidden
                      />
                    )}
                  </button>

                  {estaAberto && expansivel && (
                    <div className={s.detalhe}>
                      {b.itens.map((it, i) => (
                        <div
                          key={`${it.lead_id ?? it.telefone ?? i}`}
                          className={s.item}
                          onClick={() => irPara(it)}
                          role={it.lead_id ? "link" : undefined}
                          style={{ cursor: it.lead_id ? "pointer" : "default" }}
                        >
                          <span className={s.itemNome}>{it.nome}</span>
                          <span className={s.itemCtx}>
                            {it.preview || it.contexto || ""}
                          </span>
                          {it.corretora && (
                            <span className={s.itemCorretora}>{it.corretora}</span>
                          )}
                          <span className={s.itemTempo}>{it.desde.rotulo ?? ""}</span>
                          {/* "feito" só aqui: é o bloco que só apaga quando a
                              corretora responde. stopPropagation pra o clique
                              não abrir o Chat junto. */}
                          {(b.chave === "responderam" ||
                            b.chave === "perguntas" ||
                            b.chave === "primeiro_contato") && it.telefone && (
                            <button
                              className={s.itemFeito}
                              disabled={encerrando.has(it.telefone)}
                              title={
                                b.chave === "primeiro_contato"
                                  ? "Já falei com essa pessoa (por ligação, por exemplo) — tirar da lista"
                                  : "A conversa acabou com a mensagem do cliente — não precisa responder"
                              }
                              onClick={(e) => {
                                e.stopPropagation();
                                marcarFeito(it);
                              }}
                            >
                              ✓ feito
                            </button>
                          )}
                        </div>
                      ))}
                      {(b.total ?? 0) > b.itens.length && (
                        <div className={s.maisItens}>
                          + {(b.total ?? 0) - b.itens.length} sem caber aqui — abra o Chat.
                        </div>
                      )}
                      {(b.chave === "responderam" ||
                        b.chave === "perguntas" ||
                        b.chave === "primeiro_contato") &&
                        (b.encerradas_manualmente ?? 0) > 0 && (
                        <div className={s.nota}>
                          {b.encerradas_manualmente} conversa
                          {(b.encerradas_manualmente ?? 0) > 1 ? "s" : ""} marcada
                          {(b.encerradas_manualmente ?? 0) > 1 ? "s" : ""} como feito —
                          quem encerrou foi o cliente. Se ele escrever de novo, volta pra cá.
                        </div>
                      )}
                      {b.chave === "perguntas" && (b.encerramentos_filtrados ?? 0) > 0 && (
                        <div className={s.nota}>
                          {b.encerramentos_filtrados} conversa
                          {(b.encerramentos_filtrados ?? 0) > 1 ? "s" : ""} ficaram de fora:
                          a última mensagem era só um “ok/obrigado”. Nada a responder ali.
                        </div>
                      )}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
          )}

          {/* Estado limpo aparece pra todo mundo (o admin PRECISA ver que o dia
              da Pessoa11 está limpo). A comemoração — confete + "Seu dia" — é só de
              quem limpou. */}
          {dados.tudo_limpo && (
            <div className={s.limpo}>
              <div className={s.limpoGlifo}>✓</div>
              <div className={s.limpoTitulo}>
                {dados.pode_comemorar
                  ? "Seu dia está limpo"
                  : `O dia de ${dados.dono ?? "quem está na tela"} está limpo`}
              </div>
              <div className={s.limpoSub}>Ninguém esperando, nada parado.</div>
            </div>
          )}

          {/* Lembrete do CRM — o Meu dia responde "o que eu faço agora", mas
              quem mantém o funil verdadeiro é a etapa do card.
              Aparece pra TODO MUNDO (pedido Dona 06/08). Antes era só pra quem
              tem fila (`pode_agir`), com a ideia de que admin supervisiona e
              não atende — mas quem cobra o funil em dia também precisa ver o
              combinado que a equipe está lendo.
              Quando o dia zera vira o PRÓXIMO PASSO, em destaque; antes disso
              fica discreto, pra não competir com as pendências. */}
          {(
            <div
              className={`${s.lembreteCrm} ${dados.tudo_limpo ? s.lembreteCrmForte : ""}`}
            >
              <span className={s.lembreteIcone} aria-hidden>
                🗂️
              </span>
              <div className={s.lembreteTexto}>
                <strong>
                  {dados.tudo_limpo
                    ? "Falta um passo: atualizar o CRM"
                    : "Ao terminar por aqui, atualiza o CRM"}
                </strong>
                <span>
                  Mova cada cliente que você atendeu para a etapa certa do funil.
                </span>
              </div>
              <button
                className={s.lembreteBotao}
                onClick={() => router.push("/crm")}
              >
                abrir o CRM
              </button>
            </div>
          )}

          {!dados.tudo_ok && (
            <div className={s.avisoErro}>
              Um ou mais blocos não puderam ser consultados — o que aparece aqui
              está <strong>incompleto</strong>.{" "}
              <button className={s.btnRecarregar} onClick={() => carregar()}>
                tentar de novo
              </button>
            </div>
          )}

          <div className={s.rodape}>
            <span>
              atualizado{" "}
              {new Date(dados.gerado_em).toLocaleTimeString("pt-BR", {
                hour: "2-digit",
                minute: "2-digit",
              })}
            </span>
            <button
              className={s.btnRecarregar}
              onClick={() => carregar()}
              disabled={carregando}
            >
              {carregando ? "atualizando…" : "atualizar"}
            </button>
          </div>
        </>
      )}

      {confete && <Confete aoFim={() => setConfete(false)} />}
    </div>
  );
}
