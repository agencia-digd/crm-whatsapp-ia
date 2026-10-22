"use client";

// NUTRIÇÃO — a página de UMA automação, no molde da do remarketing (06/10/2026).
//
// Quem esgota a régua de follow-up sem responder cai na etapa Base do funil de
// remarketing e recebe UMA mensagem por marco (30/60/90/120/150 dias), pela
// linha da Pessoa12, até parar de vez. A página responde, sem rolar: está ligada?
// está agindo? por qual linha (e a cor dela)? quantos estão na nutrição agora
// (e quantos congelaram sem próxima data)? a cada quanto? o que saiu em 24 h e
// em 7 dias? Embaixo: "Ajustar", o desenho ABERTO e os números por marco.
//
// Cada marco mostra DUAS coisas que respondem perguntas diferentes:
//   · "quantos estão AQUI agora": a fila, estado do funil hoje;
//   · "quantos receberam / responderam": o histórico da janela escolhida.
// Os dois números NÃO fecham entre si de propósito.
//
// COPIES: somente leitura. A nutrição lê `nutricao.COPIES_MARCO` direto do
// código, sem tabela de override; botão de editar que não grava é pior que não
// ter botão. O LIMITE por dia é o mesmo do follow-up (o motor conta os dois
// juntos): aqui só aparece, e se muda na página do follow-up.

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import { Balao, Espera, Node } from "../blocos";
import Canvas, { Edge, corrente } from "../Canvas";
import { useConfigOperacional } from "../configOperacional";
import { estadoNutricao } from "../estados";
import { EntregaTexto, EstadoAutomacao, Linhas, MarcoBarra, dataCurta, mil, pct, ultimoEnvio, useJanelas } from "../painel";
import PorCorretora from "../PorCorretora";
import { Gatilho, Mapa, MetricaMarco, MetricasNutricao } from "../types";
import d from "../remarketing/disparo.module.css";
import n from "./nutricao.module.css";
import s from "../remarketing/remarketing.module.css";

const PERIODOS = [30, 90, 180];
const LARGURA_NO = 320;

const dias = (q: number) => (q === 1 ? "espera 1 dia" : `espera ${q} dias`);

// ── "quantos estão AQUI agora": a pergunta que a tela existe pra responder ──
function FilaNo({ m }: { m: MetricaMarco }) {
  if (m.em_fila === null) {
    return (
      <div className={n.fila}>
        <span className={n.filaSemDado}>fila indisponível</span>
        <span className={n.filaRotulo}>o funil ainda não existe no banco</span>
      </div>
    );
  }
  return (
    <>
      <div className={n.fila}>
        <span className={n.filaValor}>{m.em_fila}</span>
        <span className={n.filaRotulo}>
          {m.em_fila === 1 ? "lead parado aqui agora" : "leads parados aqui agora"}
        </span>
      </div>
      {/* CONGELADOS — o número que muda a leitura do de cima. "304 leads
          parados aqui" se lê como régua cheia girando; com 302 sem próxima
          data, a régua está entupida, não cheia. */}
      {!!m.sem_agenda && (
        <div className={n.congelado}>
          <strong>{m.sem_agenda}</strong> sem próxima data: congelados, não recebem mais nada
        </div>
      )}
    </>
  );
}

// ── métrica compacta, dentro do nó do canvas ──
function MetricaNo({ m, amostraMinima }: { m: MetricaMarco; amostraMinima: number }) {
  if (!m.tem_taxa) {
    return (
      <div className={s.metricaNo}>
        <div className={s.metricaLinha}>
          <span className={s.metricaDestaque}>etapa de entrada</span>
        </div>
        <div className={s.metricaDetalhe}>
          o lead cai aqui ao esgotar o follow-up e ainda não recebeu nada, então não há taxa.
        </div>
      </div>
    );
  }
  if (m.sem_dado) {
    return (
      <div className={s.metricaNo}>
        <div className={s.metricaLinha}>
          <span className={s.metricaSemDado}>sem dado</span>
        </div>
        <div className={s.metricaDetalhe}>
          nenhuma mensagem deste marco saiu no período.
        </div>
      </div>
    );
  }
  return (
    <div className={s.metricaNo}>
      <div className={s.metricaLinha}>
        <span className={s.metricaDestaque}>{pct(m.taxa)}</span>
        <span className={s.metricaRotulo}>engajamento</span>
        {m.amostra_pequena && (
          <span className={s.alerta} title={`menos de ${amostraMinima} envios`}>
            amostra pequena
          </span>
        )}
      </div>
      <div className={s.metricaDetalhe}>
        {m.enviados} receberam · {m.responderam} responderam
        {m.opt_outs ? ` · ${m.opt_outs} pediram saída` : ""}
      </div>
    </div>
  );
}

// ── nó de um marco: fila + métrica + a mensagem que sai ──
function NoMarco({
  reg,
  m,
  num,
  amostraMinima,
  intervaloDias,
  editavel,
}: {
  reg: (id: string) => (el: HTMLDivElement | null) => void;
  m: MetricaMarco;
  num: number;
  amostraMinima: number;
  intervaloDias: number;
  editavel: boolean;
}) {
  const entrada = !m.tem_taxa;
  return (
    <Node
      reg={reg}
      id={`nu-m${m.ordem}`}
      num={num}
      icon={entrada ? "🌱" : m.final ? "🏁" : "💬"}
      titulo={entrada ? `Entrada · ${m.etapa}` : `Mensagem · ${m.etapa}`}
      badge={m.tag ?? undefined}
      largura={LARGURA_NO}
    >
      <FilaNo m={m} />
      <MetricaNo m={m} amostraMinima={amostraMinima} />

      {entrada ? (
        <div className={s.acaoTexto}>
          Aqui o lead só espera: o primeiro marco chega {intervaloDias} dias depois
          de ele esgotar o follow-up.
        </div>
      ) : (
        <>
          <Balao>{m.copy}</Balao>
          <div className={s.tagsNo}>
            <span className={s.tagCat}>utility</span>
            <span className={s.tagCat}>sem botão</span>
            {!editavel && (
              <span className={n.somenteLeitura} title="a copy vive no código do motor">
                somente leitura
              </span>
            )}
          </div>
          <div className={s.tagsNo}>
            <span className={s.tagSaida}>sai da régua se responder</span>
          </div>
          {m.final && (
            <div className={s.notaRamo}>
              Último marco: depois dele a nutrição <strong>para de vez</strong> e o
              lead fica nesta etapa sem receber mais nada sozinho.
            </div>
          )}
        </>
      )}
    </Node>
  );
}

// ── tabela comparativa (rola dentro do container, nunca na página) ──
function TabelaMarcos({ met }: { met: MetricasNutricao }) {
  return (
    <div className={s.tabelaWrap}>
      <table className={s.tabela}>
        <thead>
          <tr>
            <th>Marco</th>
            <th>Na fila agora</th>
            <th>Receberam</th>
            <th>Responderam</th>
            <th>Pediram saída</th>
            <th>Engajamento</th>
          </tr>
        </thead>
        <tbody>
          {met.marcos.map((m) => (
            <tr key={m.etapa}>
              <td>
                {m.etapa}
                {m.tag ? ` · ${m.tag}` : ""}
              </td>
              <td>
                {m.em_fila === null ? (
                  <span className={s.celulaVazia}>sem dado</span>
                ) : (
                  <>
                    {m.em_fila}
                    {!!m.sem_agenda && (
                      <span className={s.alerta}> {m.sem_agenda} congelados</span>
                    )}
                  </>
                )}
              </td>
              {m.tem_taxa ? (
                <>
                  <td>
                    {m.sem_dado ? <span className={s.celulaVazia}>sem dado</span> : m.enviados}
                  </td>
                  <td>{m.sem_dado ? <span className={s.celulaVazia}>—</span> : m.responderam}</td>
                  <td>{m.sem_dado ? <span className={s.celulaVazia}>—</span> : m.opt_outs}</td>
                  <td className={s.taxaForte}>
                    {m.sem_dado ? (
                      <span className={s.celulaVazia}>sem dado</span>
                    ) : (
                      <>
                        {pct(m.taxa)}
                        {m.amostra_pequena && <span className={s.alerta}> amostra pequena</span>}
                      </>
                    )}
                  </td>
                </>
              ) : (
                <>
                  <td className={s.celulaVazia}>—</td>
                  <td className={s.celulaVazia}>—</td>
                  <td className={s.celulaVazia}>—</td>
                  <td className={s.celulaVazia}>etapa de entrada</td>
                </>
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}


export default function ClienteNutricao() {
  const [mapa, setMapa] = useState<Mapa | null>(null);
  const [met, setMet] = useState<MetricasNutricao | null>(null);
  const [metErro, setMetErro] = useState<string | null>(null);
  const [erro, setErro] = useState<string | null>(null);
  const [periodo, setPeriodo] = useState(90);
  const [recarga, setRecarga] = useState(0);
  const config = useConfigOperacional();
  const janelas = useJanelas<MetricasNutricao>("/api/automacoes/nutricao/metricas", recarga);

  const carregarMapa = useCallback(() => {
    fetch("/api/automacoes", { cache: "no-store" })
      .then(async (r) => {
        const body = await r.json();
        if (!r.ok) throw new Error(body?.error || body?.detail || `HTTP ${r.status}`);
        setMapa(body);
      })
      .catch((e) => setErro(String((e as Error).message || e)));
  }, []);

  useEffect(() => {
    carregarMapa();
  }, [carregarMapa]);

  useEffect(() => {
    setMet(null);
    setMetErro(null);
    fetch(`/api/automacoes/nutricao/metricas?dias=${periodo}`, { cache: "no-store" })
      .then(async (r) => {
        const body = await r.json();
        if (!r.ok) throw new Error(body?.error || body?.detail || `HTTP ${r.status}`);
        setMet(body);
      })
      .catch((e) => setMetErro(String((e as Error).message || e)));
  }, [periodo, recarga]);

  const marcos: MetricaMarco[] = useMemo(() => met?.marcos ?? [], [met]);
  const intervalo = met?.intervalo_dias ?? mapa?.nutricao.intervalo_dias ?? 30;

  const edges: Edge[] = useMemo(() => {
    const ids: string[] = ["nu-g"];
    for (const m of marcos) {
      if (m.tem_taxa) ids.push(`nu-w${m.ordem}`);
      ids.push(`nu-m${m.ordem}`);
    }
    ids.push("nu-fim");
    const es = corrente(ids);

    // ── SAÍDA COMUM ──
    // Vale em QUALQUER marco: o lead que reage sai da régua. Um fio de cada
    // marco converge no MESMO nó (leque, não espaguete) — desenhar a saída só
    // no fim daria a impressão falsa de que a pessoa percorre os 5 em linha
    // reta obrigatoriamente.
    for (const m of marcos) {
      if (!m.tem_taxa) continue;
      es.push({
        from: `nu-m${m.ordem}`,
        to: "nu-saida",
        kind: "branch",
        tone: "warn",
        dashed: true,
      });
    }
    for (const destino of ["nu-atendimento", "nu-opt"]) {
      es.push({ from: "nu-saida", to: destino, kind: "branch", tone: "warn" });
    }
    return es;
  }, [marcos]);

  if (erro) {
    return (
      <div className="estado">
        <h2>Nutrição indisponível</h2>
        <p>Não consegui falar com o copiloto. Tenta de novo em instantes.</p>
        <div className="estado-meta">{erro}</div>
      </div>
    );
  }
  if (!mapa) {
    return (
      <div className="estado">
        <h2>Carregando a nutrição…</h2>
      </div>
    );
  }

  const gatilhoEsgotou = mapa.gatilhos.find((g) => g.id === "esgotou_cadencia");
  const gatilhoTexto =
    gatilhoEsgotou?.quando ?? "o lead completa o follow-up inteiro sem responder";
  const saidas: Record<string, Gatilho | undefined> = {};
  for (const g of mapa.gatilhos) saidas[g.id] = g;
  const amostraMinima = met?.amostra_minima ?? 20;
  const etapaFinal = met?.etapa_final ?? mapa.nutricao.etapa_final;
  const editavel = met?.copies_editaveis ?? false;
  let num = 0;

  // ── o topo ──
  // A fila é foto de agora: vale a de qualquer janela (a de 24 h chega primeiro).
  const fila = janelas.d1?.fila ?? met?.fila ?? null;
  const naFila = fila?.disponivel ? fila.total : null;
  const congelados = fila?.disponivel ? fila.sem_agenda_total ?? 0 : 0;
  const est = estadoNutricao(mapa, config.dados, naFila);
  const nMarcos = (mapa.nutricao.marcos ?? []).filter((m) => m.copy).length;
  const chave = config.dados?.config.nutricao_ativa;
  const cadenciaLigada = config.dados ? Boolean(config.dados.config.cadencia_ativa?.valor) : mapa.operacao.cadencia_ativa;
  const j1 = janelas.d1?.total ?? null;
  const j7 = janelas.d7?.total ?? null;
  const ult = ultimoEnvio(mapa.saude_envio?.nutricao);
  const capTexto = est.cap === null ? "limite sem leitura" : `até ${est.cap} por dia`;
  const tel = est.bloco?.telefone_invalido ?? null;

  return (
    <div>
      <div className={s.wrap}>
        <Link href="/automacoes" className={s.voltar}>
          ← todas as automações
        </Link>

        <header className={s.cabecalho}>
          <h1 className={s.nome}>Nutrição</h1>
          <p className={s.gatilho}>
            Quando {gatilhoTexto}, entra aqui: 1 mensagem a cada {intervalo} dias até o marco {etapaFinal}.
          </p>
        </header>

        <EstadoAutomacao
          rotulo="Estado da nutrição"
          sit={est.sit}
          chave={{
            ligado: config.dados ? est.ligado : null,
            carregando: !config.dados && !config.erro,
            ocupado: config.ocupado === "nutricao_ativa",
            rotulo: est.ligado ? "Desligar a nutrição" : "Ligar a nutrição",
            confirmarLigar:
              `Ligar a nutrição?\n\n${naFila === null ? "Os" : mil(naFila)} leads na nutrição voltam a receber no marco ` +
              `de cada um, ${est.todaPelaGi ? `pela linha da ${est.linha.nome}` : "pela linha da corretora dona (ou da Pessoa12, quando a dela não serve)"}, ${capTexto}.` +
              (est.trava ? `\n\nAgora ainda não sai: ${est.trava}.` : ""),
            confirmarDesligar:
              `Desligar a nutrição?\n\nOs ${naFila === null ? "" : `${mil(naFila)} `}leads ficam parados na etapa em que ` +
              "estão e retomam quando religar. O follow-up não muda.",
            onMudar: (novo) => void config.gravar("nutricao_ativa", novo).then(carregarMapa),
          }}
          respostas={[
            {
              rotulo: "por qual linha",
              testid: "resposta-linha",
              valor: est.todaPelaGi ? `linha da ${est.linha.nome}` : "linha da dona do lead",
              sub: (
                <>
                  <Linhas linhas={est.linhasDeSaida} testid="linhas-das-corretoras" />
                  {est.bloco &&
                    (est.todaPelaGi ? (
                      <span>sai toda por ela, inclusive o lead das outras corretoras</span>
                    ) : (
                      <span>cada marco pela linha da corretora dona; sem linha ou sem modelo aprovado, pela da Pessoa12</span>
                    ))}
                </>
              ),
              ruim: est.linhasDeSaida.some((l) => l.saude.cor === "vermelho"),
            },
            {
              rotulo: "pra quem",
              testid: "resposta-quem",
              valor: naFila === null ? "—" : mil(naFila),
              sub:
                naFila === null
                  ? "não consegui ler a fila"
                  : congelados > 0
                    ? `na nutrição agora · ${mil(congelados)} sem próxima data`
                    : "na nutrição agora",
              ruim: congelados > 0,
            },
            {
              rotulo: "quando e quanto",
              testid: "resposta-quando",
              valor: `1 a cada ${intervalo} dias`,
              sub: `${nMarcos} marcos até ${etapaFinal} · ${capTexto}`,
            },
            {
              rotulo: "últimas 24 h",
              testid: "resposta-hoje",
              valor: j1 ? (
                <>
                  {mil(j1.enviados)}
                  <span className={d.de}> saíram</span>
                </>
              ) : (
                "—"
              ),
              sub: j7 ? `${mil(j7.enviados)} em 7 dias` : "7 dias sem leitura",
            },
          ]}
          faixa={[
            {
              rotulo: "respostas",
              testid: "faixa-respostas",
              conteudo:
                j1 && j7 ? (
                  <>
                    <span>
                      <strong>{mil(j1.responderam)}</strong> em 24 h
                    </span>
                    <span>
                      <strong>{mil(j7.responderam)}</strong> em 7 dias
                    </span>
                    <span>{mil(j7.opt_outs)} pediram pra sair em 7 dias</span>
                  </>
                ) : (
                  <span>{janelas.erro ? "não consegui ler" : "carregando…"}</span>
                ),
            },
            {
              rotulo: "congelados e telefone inválido",
              testid: "faixa-congelados",
              conteudo: (
                <>
                  {congelados > 0 ? (
                    <span className={d.faixaRuim}>{mil(congelados)} sem próxima data, não recebem mais nada</span>
                  ) : (
                    <span>{fila?.disponivel ? "nenhum congelado" : "congelados sem leitura"}</span>
                  )}
                  {/* Telefone fora de 55 + 10/11 dígitos: o motor marca e pula
                      (antes um só travava a volta inteira). "Ainda na régua" =
                      vão ser marcados na próxima volta. */}
                  <span data-testid="telefone-invalido">
                    {tel === null ? (
                      "telefone inválido: sem leitura"
                    ) : (
                      <>
                        telefone inválido: <strong>{mil(tel.marcados)}</strong> marcados
                        {tel.ainda_na_regua > 0 ? (
                          <>
                            , <strong className={d.faixaRuim}>{mil(tel.ainda_na_regua)} ainda na régua</strong>
                          </>
                        ) : (
                          ", nenhum na régua"
                        )}
                      </>
                    )}
                  </span>
                </>
              ),
            },
            {
              rotulo: "entrega na Meta",
              testid: "faixa-entrega",
              conteudo: est.bloco ? (
                <>
                  <EntregaTexto j={est.bloco.total_hoje} rotulo="hoje" />
                  <EntregaTexto j={est.bloco.total_semana} rotulo="7 dias" />
                  <span>{ult.texto}</span>
                </>
              ) : (
                <>
                  <span>{ult.texto}</span>
                  <span>entregue e falha: sem leitura</span>
                </>
              ),
            },
          ]}
          rodape={config.erro ? <span className={d.erroTexto}>{config.erro}</span> : undefined}
        />

        {/* ── AJUSTAR: um bloco por pergunta. Aqui não há rascunho: o que se
            grava é o interruptor (com confirmação), e o resto se ajusta em
            outra página ou mora no código. ── */}
        <section className={d.ajustes} aria-label="Ajustar a nutrição">
          <h2 className={d.ajustesTitulo}>Ajustar</h2>
          <div className={d.grade}>
            <div className={d.bloco} data-testid="bloco-chave">
              <h3 className={d.blocoTitulo}>Interruptor</h3>
              <p className={d.contagem}>
                {chave?.personalizado ? (
                  <>Chave própria, gravada no painel.</>
                ) : (
                  <>
                    Segue o follow-up, que está <strong>{cadenciaLigada ? "ligado" : "desligado"}</strong>.
                  </>
                )}
              </p>
              {chave?.personalizado && (
                <button
                  type="button"
                  className={`${d.link} ${d.linkEsquerda}`}
                  disabled={config.ocupado === "nutricao_ativa"}
                  onClick={() => {
                    if (
                      !window.confirm(
                        `Voltar a seguir o follow-up?\n\nHoje ele está ${cadenciaLigada ? "LIGADO: a nutrição fica ligada" : "DESLIGADO: a nutrição desliga"}` +
                          `${naFila === null ? "." : ` para ${mil(naFila)} leads.`}`,
                      )
                    )
                      return;
                    void config.restaurar("nutricao_ativa").then(carregarMapa);
                  }}
                >
                  voltar a seguir o follow-up
                </button>
              )}
              <p className={d.dica}>Desligar aqui não para o follow-up.</p>
            </div>

            <div className={d.bloco} data-testid="bloco-quanto">
              <h3 className={d.blocoTitulo}>Quanto</h3>
              <p className={d.contagem}>
                <strong>{est.cap === null ? "—" : mil(est.cap)}</strong> por dia na linha da {est.linha.nome}
              </p>
              <Link href="/automacoes/follow-up#ajustar" className={`${d.link} ${d.linkEsquerda}`}>
                mudar na página do follow-up
              </Link>
              <p className={d.dica}>
                {est.todaPelaGi
                  ? "A nutrição e o follow-up contam no mesmo limite."
                  : "A nutrição e o follow-up contam no mesmo limite, o de cada linha: o marco que sai pela Pessoa10 conta no limite da Pessoa10."}
              </p>
            </div>

            {est.bloco ? (
              <PorCorretora
                qual="nutricao"
                bloco={est.bloco}
                restaurar={config.restaurar}
                ocupadoRestaurar={config.ocupado === "nutricao_por_corretora"}
                onGravou={carregarMapa}
                gravar={config.gravar}
                ocupadoLinhaDaDona={config.ocupado === "nutricao_pela_linha_da_dona"}
              />
            ) : mapa.nutricao.por_corretora?.indisponivel ? (
              <div className={`${d.bloco} ${d.blocoLargo}`}>
                <h3 className={d.blocoTitulo}>Por corretora (dona do lead)</h3>
                <p className={d.erroTexto}>Não consegui ler a nutrição por corretora: {mapa.nutricao.por_corretora.motivo}</p>
              </div>
            ) : null}

            <div className={d.bloco} data-testid="bloco-mensagens">
              <h3 className={d.blocoTitulo}>Mensagens dos marcos</h3>
              <p className={d.contagem}>{editavel ? "Editáveis na caixa de cada marco." : "Somente leitura."}</p>
              <p className={d.dica}>{editavel ? "Vale a partir do próximo marco." : "O texto mora no código do motor."}</p>
            </div>
          </div>
        </section>

        <h2 className={s.secaoTitulo} id="como-funciona-hoje">Como funciona hoje</h2>
        <p className={s.fluxoLegenda}>
          A régua que roda de verdade, com quantos leads estão em cada marco agora.
          <span className={s.fluxoArraste}> Arraste pro lado pra ver tudo.</span>
        </p>
      </div>

      <div data-testid="desenho-do-fluxo">
      <Canvas edges={edges} zoomInicial={0.8} altura="auto">
        {(reg) => (
          <div className={s.flow}>
            <div className={s.flowLabel}>
              {marcos.filter((m) => m.tem_taxa).length} MARCOS, 1 A CADA {intervalo} DIAS
            </div>
            <div className={s.row}>
              <Node
                reg={reg}
                id="nu-g"
                num={++num}
                icon="⚡"
                titulo="Gatilho"
                largura={LARGURA_NO}
              >
                <div>{gatilhoTexto}</div>
                <div className={s.notaRamo}>
                  {gatilhoEsgotou?.entao ??
                    "vira 'remarketing' e entra no funil de nutrição"}
                </div>
              </Node>

              {marcos.map((m) => (
                <div key={m.etapa} className={s.seq}>
                  {m.tem_taxa && (
                    <Espera reg={reg} id={`nu-w${m.ordem}`} texto={dias(intervalo)} />
                  )}
                  <NoMarco
                    reg={reg}
                    m={m}
                    num={++num}
                    amostraMinima={amostraMinima}
                    intervaloDias={intervalo}
                    editavel={editavel}
                  />
                </div>
              ))}

              <Node
                reg={reg}
                id="nu-fim"
                num={++num}
                icon="⏹"
                titulo="Fim de linha"
                largura={LARGURA_NO}
              >
                <div>
                  Depois do marco {etapaFinal} a automação para de vez: o lead fica
                  parado nessa etapa e não recebe mais nenhuma mensagem sozinho.
                </div>
                <div className={s.notaRamo}>
                  O cadastro continua vivo: se ele responder um dia, a resposta
                  cai no atendimento normalmente. O que acaba é o disparo
                  automático, não o lead.
                </div>
              </Node>
            </div>

            {/* ── SAÍDA COMUM — vale a partir de QUALQUER marco ── */}
            <div className={s.faixaRotulo}>SAÍDA: VALE EM QUALQUER MARCO</div>
            <div className={s.ramoCentro}>
              <Node
                reg={reg}
                id="nu-saida"
                icon="⏹"
                titulo="Se o lead reagir (em qualquer ponto)"
                cond
                largura={LARGURA_NO}
              >
                <div>
                  A régua para na hora em que a pessoa dá sinal. Os marcos só
                  acontecem inteiros pra quem ficou calado por{" "}
                  {intervalo * Math.max(1, marcos.filter((m) => m.tem_taxa).length)}{" "}
                  dias seguidos.
                </div>
              </Node>
            </div>

            <div className={s.ramo}>
              <Node
                reg={reg}
                id="nu-atendimento"
                icon="🟢"
                titulo="Respondeu qualquer coisa"
                largura={LARGURA_NO}
              >
                <div>
                  {saidas.resposta_lead?.entao ??
                    "os disparos param e o lead vai pro atendimento da corretora"}
                </div>
                <div className={s.tagsNo}>
                  <span className={s.tagCat}>→ Em atendimento</span>
                </div>
              </Node>

              <Node
                reg={reg}
                id="nu-opt"
                icon="🚫"
                titulo="Pediu pra parar (opt-out)"
                largura={LARGURA_NO}
              >
                <div>
                  {saidas.opt_out?.entao ??
                    "os disparos param e o card vai pra Venda perdida"}
                </div>
                <div className={s.notaRamo}>
                  {saidas.opt_out?.quando ??
                    "vale escrito (“para”) ou clicado (“Parar mensagens”)"}
                </div>
              </Node>
            </div>
          </div>
        )}
      </Canvas>
      </div>

      <div className={s.wrap}>
        <h2 className={s.secaoTitulo}>Resultado</h2>
        <div className={s.periodo}>
          <span className={s.periodoRotulo}>período</span>
          {PERIODOS.map((q) => (
            <button
              key={q}
              type="button"
              className={`${s.pill} ${periodo === q ? s.pillOn : ""}`}
              onClick={() => setPeriodo(q)}
            >
              {q} dias
            </button>
          ))}
        </div>

        {metErro && <div className={s.erro}>Não consegui ler os números: {metErro}</div>}

        {met && (
          <>
            <MarcoBarra met={met} onRecomecou={() => setRecarga((q) => q + 1)} />

            <div className={s.resumo}>
              <div className={s.stat}>
                <div className={s.statLabel}>mensagens enviadas</div>
                <div className={s.statValor}>{met.total.enviados}</div>
                <div className={s.statSub}>
                  {met.medindo_desde ? `desde ${dataCurta(met.medindo_desde)}` : `nos últimos ${met.dias} dias`}
                </div>
              </div>
              <div className={s.stat}>
                <div className={s.statLabel}>responderam</div>
                <div className={s.statValor}>{met.total.responderam}</div>
                <div className={s.statSub}>{met.total.opt_outs} pediram pra sair</div>
              </div>
              <div className={s.stat}>
                <div className={s.statLabel}>engajamento</div>
                <div className={met.total.sem_dado ? s.semDado : s.statValor}>{pct(met.total.taxa)}</div>
                <div className={s.statSub}>
                  {met.total.sem_dado
                    ? "nenhuma mensagem saiu no período"
                    : met.total.amostra_pequena
                      ? `amostra pequena (< ${amostraMinima} envios)`
                      : "respostas ÷ mensagens enviadas"}
                </div>
              </div>
            </div>

            <TabelaMarcos met={met} />
          </>
        )}

        <details className={s.comoFunciona}>
          <summary>Mais sobre o funcionamento</summary>
          <div className={s.rodape}>
            {met?.definicao}
            <br />
            Marco a cada {intervalo} dias: janela curta demais mostra régua vazia. Régua lida do motor que
            roda de verdade: se um marco entrar ou sair do código, ele entra ou sai desta página sozinho.
          </div>
        </details>
      </div>
    </div>
  );
}
