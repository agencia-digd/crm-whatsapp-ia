"use client";

// FOLLOW-UP — a página de UMA automação, no molde da do remarketing (06/10/2026).
//
// A Dona aprovou a tela nova do remarketing e pediu as outras "igual". A página
// responde, nesta ordem e sem rolar:
//   1. está ligado? está agindo? se não, por quê (uma frase);
//   2. por qual linha sai: a da DONA do lead, com a cor de cada linha na Meta;
//   3. pra quem: quantos estão na régua agora;
//   4. quando e quanto: a régua, o horário do motor e o limite por linha;
//   5. o que aconteceu nas últimas 24 h e em 7 dias.
// Embaixo: "Ajustar" (o limite por dia de cada linha, com salvar que não liga
// nada), o desenho da régua ABERTO e os números por toque.
//
// Três travas de leitura honesta, as de sempre:
//   1. toque de LIGAÇÃO não tem taxa de resposta: diz "tarefa de ligação", nunca 0%;
//   2. abaixo de N envios (o backend manda o N) o número vem marcado como amostra pequena;
//   3. quem não recebeu ninguém mostra "sem dado", não 0%.
//
// Nada de régua fixada no código: a lista de toques vem do backend, que a deriva
// de `cadencia.TOQUES`. Quando entrou o D3 de áudio, a tela mudou sozinha.

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import { Balao, CopyEditor, Espera, Node } from "../blocos";
import Canvas, { Edge, corrente } from "../Canvas";
import { useConfigOperacional } from "../configOperacional";
import { capDaCorretora, estadoFollowUp, linhasDoCadastro } from "../estados";
import PorCorretora from "../PorCorretora";
import {
  BarraSalvar,
  Bolinha,
  EntregaTexto,
  EstadoAutomacao,
  Linhas,
  MarcoBarra,
  dataCurta,
  mil,
  pct,
  ultimoEnvio,
  useJanelas,
  useSalvo,
} from "../painel";
import { CopyItem, Gatilho, Mapa, MetricaToque, Metricas, Toque } from "../types";
import d from "../remarketing/disparo.module.css";
import p from "../painel.module.css";
import s from "../remarketing/remarketing.module.css";

const PERIODOS = [7, 30, 90];
const LARGURA_NO = 320;
// O mesmo teto que o servidor aceita no limite por linha.
const TETO = 250;

const dias = (n: number) => (n === 1 ? "espera 1 dia" : `espera ${n} dias`);

type Config = ReturnType<typeof useConfigOperacional>;

function MetricaNo({ m, amostraMinima }: { m: MetricaToque; amostraMinima: number }) {
  if (!m.tem_taxa) {
    return (
      <div className={s.metricaNo}>
        <div className={s.metricaLinha}>
          <span className={s.metricaDestaque}>tarefa de ligação</span>
        </div>
        <div className={s.metricaDetalhe}>
          {m.tarefas ?? 0} tarefas geradas. Não sai mensagem, então não há taxa.
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
          nenhuma mensagem deste toque saiu no período
          {m.categoria === "marketing"
            ? " (toque de marketing, e a linha só manda utility)"
            : ""}
          .
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

// ── nó de um toque: métrica + mensagem + editor ao expandir ──
function NoToque({
  reg,
  t,
  num,
  metrica,
  copia,
  amostraMinima,
  onCopiaMudou,
}: {
  reg: (id: string) => (el: HTMLDivElement | null) => void;
  t: Toque;
  num: number;
  metrica?: MetricaToque;
  copia?: CopyItem;
  amostraMinima: number;
  onCopiaMudou: () => void;
}) {
  const [aberto, setAberto] = useState(false);
  const ligacao = t.tipo === "tarefa_ligacao";

  return (
    <Node
      reg={reg}
      id={`fu-t${t.indice}`}
      num={num}
      icon={ligacao ? "📞" : "💬"}
      titulo={ligacao ? `Tarefa de ligação · ${t.dia}` : `Mensagem · ${t.dia}`}
      largura={LARGURA_NO}
    >
      {metrica ? (
        <MetricaNo m={metrica} amostraMinima={amostraMinima} />
      ) : (
        <div className={s.metricaNo}>
          <div className={s.metricaDetalhe}>carregando os números…</div>
        </div>
      )}

      <div className={s.tagsNo}>
        <span className={s.tagSaida}>sai da régua se responder</span>
        {t.com_botao && (
          <span className={s.tagFica}>clique não tira</span>
        )}
      </div>

      {ligacao ? (
        <div className={s.acaoTexto}>
          {t.acao || "vira tarefa pra corretora ligar, nenhuma mensagem sai"}
        </div>
      ) : (
        <>
          <Balao>{t.preview || t.copy}</Balao>
          <div className={s.tagsNo}>
            <span className={s.tagCat}>{t.categoria}</span>
            <span className={s.tagCat}>{t.com_botao ? "com botão" : "sem botão"}</span>
          </div>
          {copia && (
            <>
              <button
                type="button"
                className={s.btnEditar}
                onClick={() => setAberto((v) => !v)}
              >
                {aberto ? "fechar a edição" : "editar a mensagem"}
              </button>
              {aberto && (
                <div className={s.editorNo}>
                  <CopyEditor item={copia} onMudou={onCopiaMudou} />
                </div>
              )}
            </>
          )}
        </>
      )}
    </Node>
  );
}

// ── tabela comparativa (rola dentro do container, nunca na página) ──
function TabelaToques({ met }: { met: Metricas }) {
  return (
    <div className={s.tabelaWrap}>
      <table className={s.tabela}>
        <thead>
          <tr>
            <th>Toque</th>
            <th>Receberam</th>
            <th>Responderam</th>
            <th>Pediram saída</th>
            <th>Engajamento</th>
          </tr>
        </thead>
        <tbody>
          {met.toques.map((m) => (
            <tr key={m.indice}>
              <td>
                {m.dia} · {m.tipo === "mensagem" ? "mensagem" : "ligação"}
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
                  <td className={s.celulaVazia}>{m.tarefas ?? 0} tarefas</td>
                  <td className={s.celulaVazia}>—</td>
                  <td className={s.celulaVazia}>—</td>
                  <td className={s.celulaVazia}>tarefa de ligação</td>
                </>
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ── AJUSTAR: quanto por dia, por linha ──
//
// Morava na tela geral ("Controle da operação"). Veio pra cá porque é o limite
// da régua do follow-up (`cadencia_cap_diario` + `cadencia_cap_por_corretora`),
// e a nutrição conta no MESMO limite: a página da nutrição mostra o número e
// aponta pra cá. Um lugar só pra mexer, senão são dois botões pro mesmo valor.
//
// Nada sai em teclada solta: muda o rascunho, aparece a barra, e salvar pede
// confirmação com os números quando algum limite SOBE. Salvar não liga nada.
function Quanto({ mapa, config, antes }: { mapa: Mapa; config: Config; antes?: React.ReactNode }) {
  const { dados, gravar, restaurar, ocupado } = config;
  const corretoras = dados?.corretoras ?? [];
  const linhas = linhasDoCadastro(mapa);
  const padraoGravado = dados ? Number(dados.config.cadencia_cap_diario?.valor ?? mapa.operacao.cap_diario) : null;
  const proprioGravado = (dados?.config.cadencia_cap_por_corretora?.valor ?? {}) as Record<string, number>;
  const assinatura = JSON.stringify([padraoGravado, proprioGravado]);

  const [padrao, setPadrao] = useState("");
  const [proprio, setProprio] = useState<Record<string, string>>({});
  const [erros, setErros] = useState<Record<string, string>>({});
  const [salvando, setSalvando] = useState(false);
  const [salvo, setSalvo] = useSalvo();

  function voltarAoGravado() {
    const [pd, pr] = JSON.parse(assinatura) as [number | null, Record<string, number>];
    setPadrao(pd === null ? "" : String(pd));
    setProprio(Object.fromEntries(Object.entries(pr).map(([k, v]) => [k, String(v)])));
    setErros({});
  }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(voltarAoGravado, [assinatura]);

  if (!dados) return null;

  const valido = (t: string) => /^\d+$/.test(t.trim()) && Number(t) <= TETO;
  const efetivo = (id: number) => {
    const t = proprio[String(id)];
    return t !== undefined && t.trim() !== "" ? t : padrao;
  };
  const mudouPadrao = padrao.trim() !== String(padraoGravado);
  const proprioNovo: Record<string, number> = {};
  for (const c of corretoras) {
    const t = proprio[String(c.id)];
    if (t !== undefined && t.trim() !== "" && valido(t)) proprioNovo[String(c.id)] = Number(t);
  }
  const mudouProprio = JSON.stringify(proprioNovo) !== JSON.stringify(
    Object.fromEntries(Object.entries(proprioGravado).filter(([k]) => corretoras.some((c) => String(c.id) === k))),
  );
  const invalidos = [padrao, ...Object.values(proprio).filter((t) => t.trim() !== "")].filter((t) => !valido(t));
  const quantas = Number(mudouPadrao) + Number(mudouProprio);

  async function salvar() {
    // Limite que SOBE pede confirmação com o número antes e depois.
    const subidas: string[] = [];
    if (mudouPadrao && Number(padrao) > (padraoGravado ?? 0)) subidas.push(`padrão: ${padraoGravado} → ${padrao}`);
    for (const c of corretoras) {
      const antes = proprioGravado[String(c.id)] ?? padraoGravado ?? 0;
      const depois = Number(efetivo(c.id));
      if (depois > antes) subidas.push(`${c.label}: ${antes} → ${depois}`);
    }
    if (
      subidas.length > 0 &&
      !window.confirm(
        `Subir o limite de mensagens por dia?\n\n${subidas.join("\n")}\n\n` +
          "Volume alto demais derruba a qualidade da linha na Meta. Salvar não liga o follow-up.",
      )
    ) {
      return;
    }
    setSalvando(true);
    const novos: Record<string, string> = {};
    if (mudouPadrao) {
      const r = await gravar("cadencia_cap_diario", Number(padrao), true);
      if (!r.ok) novos.padrao = r.erro ?? "não consegui gravar";
    }
    if (mudouProprio) {
      const r = await gravar("cadencia_cap_por_corretora", proprioNovo, true);
      if (!r.ok) novos.proprio = r.erro ?? "não consegui gravar";
    }
    setErros(novos);
    setSalvando(false);
    if (Object.keys(novos).length === 0) setSalvo(true);
  }

  return (
    <section id="ajustar" className={`${d.ajustes} ${quantas > 0 ? d.ajustesComMudanca : ""}`} aria-label="Ajustar o follow-up">
      <h2 className={d.ajustesTitulo}>Ajustar</h2>
      <div className={d.grade}>
        {antes}
        <div className={`${d.bloco} ${d.blocoLargo}`} data-testid="bloco-quanto">
          <h3 className={d.blocoTitulo}>Quanto, por dia, em cada linha</h3>
          <div className={p.pares}>
            <label className={p.par}>
              <span className={p.parNome}>
                Padrão <small>vale pra quem não tem limite próprio</small>
              </span>
              <input
                type="number"
                min={0}
                max={TETO}
                inputMode="numeric"
                className={`${d.campo} ${valido(padrao) ? "" : p.campoErro}`}
                value={padrao}
                onChange={(e) => setPadrao(e.target.value)}
                aria-label="limite padrão por dia"
              />
            </label>
            {corretoras.map((c) => {
              const l = linhas.find((x) => x.nome === c.label);
              const t = proprio[String(c.id)] ?? "";
              return (
                <label key={c.id} className={p.par} data-testid={`limite-${c.label}`}>
                  <span className={p.parNome}>
                    <Bolinha cor={l && l.temLinha ? l.saude.cor : "desconhecida"} />
                    {c.label}
                    {l && !l.temLinha && <small>sem linha</small>}
                    {t.trim() === "" && <small>usa o padrão</small>}
                  </span>
                  <input
                    type="number"
                    min={0}
                    max={TETO}
                    inputMode="numeric"
                    className={`${d.campo} ${t.trim() === "" || valido(t) ? "" : p.campoErro}`}
                    value={t}
                    placeholder={padrao}
                    onChange={(e) => setProprio((x) => ({ ...x, [String(c.id)]: e.target.value }))}
                    aria-label={`limite por dia da ${c.label}`}
                  />
                </label>
              );
            })}
          </div>
          {invalidos.length > 0 && <p className={d.erroTexto}>Use um número inteiro de 0 a {TETO}.</p>}
          {erros.padrao && <p className={d.erroTexto}>{erros.padrao}</p>}
          {erros.proprio && <p className={d.erroTexto}>{erros.proprio}</p>}
          <p className={d.dica}>Vale pro follow-up e pra nutrição: as duas contam no mesmo limite. Vazio = usa o padrão.</p>
          {dados.config.cadencia_cap_por_corretora?.personalizado && (
            <button
              type="button"
              className={`${d.link} ${d.linkEsquerda}`}
              disabled={ocupado === "cadencia_cap_por_corretora"}
              onClick={() => {
                if (!window.confirm(`Apagar os limites próprios? Todas as linhas passam a usar o padrão (${padraoGravado} por dia).`)) return;
                void restaurar("cadencia_cap_por_corretora");
              }}
            >
              tirar os limites próprios (todas usam o padrão)
            </button>
          )}
        </div>
      </div>
      <BarraSalvar
        quantas={quantas}
        salvando={salvando}
        salvo={salvo}
        nota="salvar não liga o follow-up"
        bloqueio={invalidos.length > 0 ? "tem número fora de 0 a 250" : null}
        onDescartar={voltarAoGravado}
        onSalvar={() => void salvar()}
      />
    </section>
  );
}

export default function ClienteFollowUp() {
  const [mapa, setMapa] = useState<Mapa | null>(null);
  const [copies, setCopies] = useState<Record<number, CopyItem>>({});
  const [met, setMet] = useState<Metricas | null>(null);
  const [metErro, setMetErro] = useState<string | null>(null);
  const [erro, setErro] = useState<string | null>(null);
  const [periodo, setPeriodo] = useState(30);
  const [recarga, setRecarga] = useState(0);
  const config = useConfigOperacional();
  const janelas = useJanelas<Metricas>("/api/automacoes/metricas", recarga);

  const carregarCopies = useCallback(() => {
    fetch("/api/automacoes/copies", { cache: "no-store" })
      .then(async (r) => {
        if (!r.ok) return;
        const body = await r.json();
        const porToque: Record<number, CopyItem> = {};
        for (const item of body?.copies ?? []) porToque[item.toque] = item;
        setCopies(porToque);
      })
      .catch(() => {});
  }, []);

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
    carregarCopies();
  }, [carregarMapa, carregarCopies]);

  useEffect(() => {
    setMet(null);
    setMetErro(null);
    fetch(`/api/automacoes/metricas?dias=${periodo}`, { cache: "no-store" })
      .then(async (r) => {
        const body = await r.json();
        if (!r.ok) throw new Error(body?.error || body?.detail || `HTTP ${r.status}`);
        setMet(body);
      })
      .catch((e) => setMetErro(String((e as Error).message || e)));
  }, [periodo, recarga]);

  const toques: Toque[] = useMemo(() => mapa?.cadencia.toques ?? [], [mapa]);

  // O toque que sai COM botão (hoje a oferta de áudio). Vem do backend, que
  // deriva de `cadencia.TOQUES`: nada de índice escrito aqui.
  const toqueBotao = useMemo(
    () => toques.find((t) => t.tipo === "mensagem" && t.com_botao) ?? null,
    [toques],
  );

  // ids do canvas: gatilho → (espera → toque)* → fim, mais a ramificação do
  // clique quando existe toque com botão.
  const edges: Edge[] = useMemo(() => {
    const ids: string[] = ["fu-g"];
    let anterior = 0;
    for (const t of toques) {
      const off = t.offset_dias ?? 0;
      if (off - anterior > 0) ids.push(`fu-w${t.indice}`);
      ids.push(`fu-t${t.indice}`);
      anterior = off;
    }
    ids.push("fu-fim");
    const es = corrente(ids);

    // ── SAÍDA COMUM ──
    // A régua é interrompível em QUALQUER ponto: em todos os toques, se o lead
    // reagir, ele sai. Desenhar o desvio só no toque do botão dava a impressão
    // (falsa) de que a pessoa percorre os 9 em linha reta. Um fio de cada toque
    // converge no MESMO nó — leque, não espaguete: todos terminam no mesmo
    // ponto, e é isso que comunica "vale em qualquer um".
    for (const t of toques) {
      es.push({
        from: `fu-t${t.indice}`,
        to: "fu-saida",
        kind: "branch",
        tone: "warn",
        dashed: true,
      });
    }
    for (const destino of ["fu-atendimento", "fu-ja", "fu-opt", "fu-pausa"]) {
      es.push({ from: "fu-saida", to: destino, kind: "branch", tone: "warn" });
    }

    if (toqueBotao) {
      // clique → áudio → duas saídas (calou segue a régua / falou vai pro
      // atendimento). Tom âmbar e tracejado = condição, igual ao mapa geral.
      es.push({
        from: `fu-t${toqueBotao.indice}`,
        to: "fu-clique",
        kind: "branch",
        tone: "warn",
        dashed: true,
      });
      es.push({ from: "fu-clique", to: "fu-audio", kind: "h", tone: "warn" });
      es.push({ from: "fu-audio", to: "fu-calou", kind: "h", tone: "warn" });
      es.push({ from: "fu-audio", to: "fu-falou", kind: "branch", tone: "warn", dashed: true });
    }
    return es;
  }, [toques, toqueBotao]);


  if (erro) {
    return (
      <div className="estado">
        <h2>Follow-up indisponível</h2>
        <p>Não consegui falar com o copiloto. Tenta de novo em instantes.</p>
        <div className="estado-meta">{erro}</div>
      </div>
    );
  }
  if (!mapa) {
    return (
      <div className="estado">
        <h2>Carregando o follow-up…</h2>
      </div>
    );
  }

  const gatilhoTexto =
    mapa.gatilhos.find((g) => g.id === "auto_enroll_followup")?.quando ??
    "o lead entra na etapa Follow-up";
  // As saídas vêm do MAPA (que as lê do motor), não de texto digitado aqui.
  const saidas: Record<string, Gatilho | undefined> = {};
  for (const g of mapa.gatilhos) saidas[g.id] = g;
  const porToque: Record<number, MetricaToque> = {};
  for (const m of met?.toques ?? []) porToque[m.indice] = m;
  const amostraMinima = met?.amostra_minima ?? 20;
  let num = 0;

  // ── o topo ──
  const est = estadoFollowUp(mapa, config.dados);
  const comLinha = est.linhas.filter((l) => l.temLinha);
  const estadoChave = config.dados?.config.cadencia_ativa;
  const ultimoDia = toques.length ? toques[toques.length - 1].offset_dias ?? 0 : 0;
  const capPadrao = config.dados ? capDaCorretora(config.dados, mapa, -1) : mapa.operacao.cap_diario;
  const caps = (mapa.boas_vindas.por_corretora ?? [])
    .filter((c) => c.tem_linha_cloud)
    .map((c) => capDaCorretora(config.dados, mapa, c.corretora_id));
  const capTexto =
    caps.length > 0 && Math.min(...caps) !== Math.max(...caps)
      ? `de ${Math.min(...caps)} a ${Math.max(...caps)} por linha/dia`
      : `até ${caps[0] ?? capPadrao} por linha/dia`;
  const j1 = janelas.d1?.total ?? null;
  const j7 = janelas.d7?.total ?? null;
  const ult = ultimoEnvio(mapa.saude_envio?.["follow-up"]);
  const semLeitura = <span>{janelas.erro ? "não consegui ler" : "carregando…"}</span>;
  const vezes = (n: number, um: string, varios: string) => `${mil(n)} ${n === 1 ? um : varios}`;
  return (
    <div>
      <div className={s.wrap}>
        <Link href="/automacoes" className={s.voltar}>
          ← todas as automações
        </Link>

        <header className={s.cabecalho}>
          <h1 className={s.nome}>Follow-up</h1>
          <p className={s.gatilho}>Quando {gatilhoTexto}, começa a régua do D0 ao encerramento.</p>
        </header>

        <EstadoAutomacao
          rotulo="Estado do follow-up"
          masculino
          sit={est.sit}
          chave={{
            ligado: config.dados ? est.ligado : null,
            carregando: !config.dados && !config.erro,
            ocupado: config.ocupado === "cadencia_ativa",
            rotulo: est.ligado ? "Desligar o follow-up" : "Ligar o follow-up",
            confirmarLigar:
              `Ligar o follow-up?\n\n${mil(est.naRegua)} leads na régua voltam a receber, ${capTexto}, ` +
              `pelas linhas ${comLinha.map((l) => `da ${l.nome}`).join(", ")}.` +
              (est.trava ? `\n\nAgora ainda não sai: ${est.trava}.` : ""),
            confirmarDesligar:
              `Desligar o follow-up?\n\nOs ${mil(est.naRegua)} leads da régua param de receber e retomam de onde ` +
              "pararam quando religar.",
            onMudar: (novo) => void config.gravar("cadencia_ativa", novo).then(carregarMapa),
          }}
          respostas={[
            {
              rotulo: "por qual linha",
              testid: "resposta-linha",
              valor: `${comLinha.length} ${comLinha.length === 1 ? "linha" : "linhas"}`,
              sub: <Linhas linhas={est.linhas} testid="linhas-das-corretoras" />,
              ruim: est.linhas.some((l) => l.temLinha && l.saude.cor === "vermelho"),
            },
            {
              rotulo: "pra quem",
              testid: "resposta-quem",
              valor: mil(est.naRegua),
              sub: (() => {
                const b = est.bloco;
                const desl = b ? b.corretoras.filter((c) => c.ligada === false) : [];
                const devidas = b ? b.corretoras.reduce((t, c) => t + (c.devidas_agora ?? 0), 0) : 0;
                const partes = ["na régua agora"];
                if (devidas > 0) partes.push(`${mil(devidas)} devidos agora`);
                if (desl.length > 0) partes.push(`desligado pra ${desl.map((c) => c.label).join(", ")}`);
                return partes.join(" · ");
              })(),
            },
            {
              rotulo: "quando e quanto",
              testid: "resposta-quando",
              valor: `${toques.length} toques em ${ultimoDia} dias`,
              sub: `${mapa.operacao.timer.janela.replace(" (São Paulo)", "")} · ${capTexto}`,
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
                    <span>{vezes(j7.opt_outs, "pediu pra sair", "pediram pra sair")} em 7 dias</span>
                  </>
                ) : (
                  semLeitura
                ),
            },
            {
              rotulo: "tarefas de ligação",
              testid: "faixa-tarefas",
              conteudo:
                j1 && j7 ? (
                  <>
                    <span>
                      <strong>{mil(j1.tarefas)}</strong> em 24 h
                    </span>
                    <span>
                      <strong>{mil(j7.tarefas)}</strong> em 7 dias
                    </span>
                  </>
                ) : (
                  semLeitura
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
          rodape={
            config.erro || estadoChave?.personalizado ? (
              <>
                {config.erro && <span className={d.erroTexto}>{config.erro}</span>}
                {estadoChave?.personalizado && (
                  <button
                    type="button"
                    className={d.link}
                    disabled={config.ocupado === "cadencia_ativa"}
                    onClick={() => {
                      const padraoLigado = Boolean(estadoChave.padrao);
                      if (
                        padraoLigado !== est.ligado &&
                        !window.confirm(
                          padraoLigado
                            ? `O padrão do sistema é LIGADO. Voltar ao padrão liga o follow-up pra ${mil(est.naRegua)} leads. Confirmar?`
                            : "O padrão do sistema é DESLIGADO. Voltar ao padrão desliga o follow-up. Confirmar?",
                        )
                      )
                        return;
                      void config.restaurar("cadencia_ativa").then(carregarMapa);
                    }}
                  >
                    voltar o interruptor ao padrão ({estadoChave.padrao ? "ligado" : "desligado"})
                  </button>
                )}
              </>
            ) : undefined
          }
        />

        <Quanto
          mapa={mapa}
          config={config}
          antes={
            est.bloco ? (
              <PorCorretora
                qual="follow-up"
                bloco={est.bloco}
                restaurar={config.restaurar}
                ocupadoRestaurar={config.ocupado === "cadencia_por_corretora"}
                onGravou={carregarMapa}
              />
            ) : mapa.follow_up?.indisponivel ? (
              <div className={`${d.bloco} ${d.blocoLargo}`}>
                <h3 className={d.blocoTitulo}>Por corretora</h3>
                <p className={d.erroTexto}>Não consegui ler o follow-up por corretora: {mapa.follow_up.motivo}</p>
              </div>
            ) : null
          }
        />

        <h2 className={s.secaoTitulo} id="como-funciona-hoje">Como funciona hoje</h2>
        <p className={s.fluxoLegenda}>
          A régua que roda de verdade. O texto de cada toque se edita na caixa dele.
          <span className={s.fluxoArraste}> Arraste pro lado pra ver tudo.</span>
        </p>
      </div>

      {/* O desenho fica sempre aberto (pedido da Dona pro remarketing, 05/10).
          É o MESMO canvas do mapa geral (Canvas.tsx). */}
      <div data-testid="desenho-do-fluxo">
      <Canvas edges={edges} zoomInicial={0.8} altura="auto">
        {(reg) => (
          <div className={s.flow}>
            <div className={s.flowLabel}>RÉGUA DE {toques.length} TOQUES</div>
            <div className={s.row}>
              <Node
                reg={reg}
                id="fu-g"
                num={++num}
                icon="⚡"
                titulo="Gatilho"
                largura={LARGURA_NO}
              >
                <div>{gatilhoTexto}</div>
              </Node>

              {toques.map((t, i) => {
                const anterior = i > 0 ? toques[i - 1].offset_dias ?? 0 : 0;
                const gap = (t.offset_dias ?? 0) - anterior;
                return (
                  <div key={t.indice} className={s.seq}>
                    {gap > 0 && <Espera reg={reg} id={`fu-w${t.indice}`} texto={dias(gap)} />}
                    <NoToque
                      reg={reg}
                      t={t}
                      num={++num}
                      metrica={porToque[t.indice]}
                      copia={copies[t.indice]}
                      amostraMinima={amostraMinima}
                      onCopiaMudou={carregarCopies}
                    />
                  </div>
                );
              })}

              <Node
                reg={reg}
                id="fu-fim"
                num={++num}
                icon="🔁"
                titulo="Esgotou sem responder"
                largura={LARGURA_NO}
              >
                <div>{mapa.cadencia.remarketing.regra}</div>
              </Node>
            </div>

            {/* ── SAÍDA COMUM — vale a partir de QUALQUER toque ──
                Todo toque tem saída: em qualquer um dos {toques.length}, se o
                lead reagir, ele sai da régua. Um nó só, com um fio vindo de
                cada toque, e daí os destinos que o motor realmente tem. */}
            <div className={s.faixaRotulo}>
              SAÍDA: VALE EM QUALQUER TOQUE
            </div>
            <div className={s.ramoCentro}>
              <Node
                reg={reg}
                id="fu-saida"
                icon="⏹"
                titulo="Se o lead reagir (em qualquer ponto)"
                cond
                largura={LARGURA_NO}
              >
                <div>
                  A régua não é uma esteira: ela para na hora em que a pessoa dá
                  sinal. Os {toques.length} toques só acontecem inteiros pra quem
                  ficou calado até o fim.
                </div>
              </Node>
            </div>

            <div className={s.ramo}>
              <Node
                reg={reg}
                id="fu-atendimento"
                icon="🟢"
                titulo="Respondeu qualquer coisa"
                largura={LARGURA_NO}
              >
                <div>{saidas.resposta_lead?.entao ?? "cadência para e o lead vai pro atendimento"}</div>
                <div className={s.tagsNo}>
                  <span className={s.tagCat}>respondida</span>
                  <span className={s.tagCat}>→ Em atendimento</span>
                </div>
              </Node>

              <Node
                reg={reg}
                id="fu-ja"
                icon="🏁"
                titulo="“Já encontrei / já resolvi”"
                largura={LARGURA_NO}
              >
                <div>
                  {saidas.ja_encontrou?.entao ??
                    "despedida com pedido de indicação e sai como perdido"}
                </div>
                <div className={s.tagsNo}>
                  <span className={s.tagCat}>sem passar pelo atendimento</span>
                </div>
              </Node>

              <Node
                reg={reg}
                id="fu-opt"
                icon="🚫"
                titulo="Pediu pra parar (opt-out)"
                largura={LARGURA_NO}
              >
                <div>{saidas.opt_out?.entao ?? "cadência para e o card vai pra Venda perdida"}</div>
                <div className={s.notaRamo}>
                  Vale escrito (“para”) ou clicado (“Parar mensagens”), e tem
                  precedência sobre o “já encontrei” — quem pede pra sair não
                  recebe despedida com oferta.
                </div>
              </Node>

              <Node
                reg={reg}
                id="fu-pausa"
                icon="⏸"
                titulo="Escolheu “2” no encerramento"
                largura={LARGURA_NO}
              >
                <div>
                  {saidas.pausa_30d?.entao ??
                    "fica pausada por 30 dias e depois volta pro remarketing"}
                </div>
                <div className={s.tagsNo}>
                  <span className={s.tagCat}>só no fim da régua</span>
                </div>
              </Node>
            </div>

            {/* ── ramificação do BOTÃO (só existe se a régua tiver um) ──
                Clicar não é responder: o áudio sai e o lead CONTINUA na régua.
                É a diferença que a Dona pediu — antes o "sim" em texto parava
                a cadência e a pessoa saía sem nunca ter conversado. */}
            {toqueBotao && (
              <div className={s.faixaRotulo}>
                O QUE NÃO TIRA O LEAD DA RÉGUA
              </div>
            )}
            {toqueBotao && (
              <div className={s.ramo}>
                <Node
                  reg={reg}
                  id="fu-clique"
                  icon="👆"
                  titulo={`Se clicar no botão · ${toqueBotao.dia}`}
                  cond
                  largura={LARGURA_NO}
                >
                  <div>
                    O lead toca em “Saiba mais” pra {toqueBotao.cta || "receber o áudio"}.
                  </div>
                  <div className={s.tagsNo}>
                    <span className={s.tagCat}>clique ≠ resposta</span>
                  </div>
                </Node>

                <Node
                  reg={reg}
                  id="fu-audio"
                  icon="🎙"
                  titulo="Manda o áudio da corretora"
                  largura={LARGURA_NO}
                >
                  <div>
                    Sai a voz da corretora dona do lead, como mensagem de voz — a
                    janela de 24h está aberta pelo clique, então não gasta template.
                  </div>
                </Node>

                <Node
                  reg={reg}
                  id="fu-calou"
                  icon="▶️"
                  titulo="Ouviu e não falou nada"
                  largura={LARGURA_NO}
                >
                  <div>
                    Continua na automação: o próximo toque chega na data prevista,
                    como se nada tivesse acontecido.
                  </div>
                </Node>

                <Node
                  reg={reg}
                  id="fu-falou"
                  icon="🟢"
                  titulo="Falou alguma coisa depois"
                  cond
                  largura={LARGURA_NO}
                >
                  <div>
                    Aí sim a cadência para e o lead vai pro atendimento da
                    corretora — quem escreve tem assunto.
                  </div>
                  <div className={s.tagsNo}>
                    <span className={s.tagCat}>exceção: “já encontrei”</span>
                  </div>
                  <div className={s.notaRamo}>
                    Se a mensagem for “já achei / já resolvi”, a Assistente se despede
                    com o pedido de indicação e encerra — sem passar pelo
                    atendimento. E “Parar mensagens”, mesmo clicado, é opt-out.
                  </div>
                </Node>

                <Node
                  reg={reg}
                  id="fu-autoreply"
                  icon="🤖"
                  titulo="Resposta automática do outro lado"
                  largura={LARGURA_NO}
                >
                  <div>
                    {saidas.auto_reply?.entao ??
                      "ignora — não marca respondida, não move o card, régua segue"}
                  </div>
                  <div className={s.notaRamo}>
                    {saidas.auto_reply?.quando ??
                      "inbound colado no nosso envio, ou com cara de secretária eletrônica"}
                  </div>
                </Node>
              </div>
            )}
          </div>
        )}
      </Canvas>
      </div>

      <div className={s.wrap}>
        <h2 className={s.secaoTitulo}>Resultado</h2>
        <div className={s.periodo}>
          <span className={s.periodoRotulo}>período</span>
          {PERIODOS.map((d) => (
            <button
              key={d}
              type="button"
              className={`${s.pill} ${periodo === d ? s.pillOn : ""}`}
              onClick={() => setPeriodo(d)}
            >
              {d} dias
            </button>
          ))}
        </div>

        {metErro && <div className={s.erro}>Não consegui ler os números: {metErro}</div>}

        {met && (
          <>
            <MarcoBarra met={met} onRecomecou={() => setRecarga((n) => n + 1)} />

            <div className={s.resumo}>
              <div className={s.stat}>
                <div className={s.statLabel}>mensagens enviadas</div>
                <div className={s.statValor}>{met.total.enviados}</div>
                <div className={s.statSub}>
                  {met.medindo_desde
                    ? `desde ${dataCurta(met.medindo_desde)}`
                    : `nos últimos ${met.dias} dias`}
                </div>
              </div>
              <div className={s.stat}>
                <div className={s.statLabel}>responderam</div>
                <div className={s.statValor}>{met.total.responderam}</div>
                <div className={s.statSub}>{met.total.opt_outs} pediram pra sair</div>
              </div>
              <div className={s.stat}>
                <div className={s.statLabel}>engajamento</div>
                <div className={met.total.sem_dado ? s.semDado : s.statValor}>
                  {pct(met.total.taxa)}
                </div>
                <div className={s.statSub}>
                  {met.total.sem_dado
                    ? "nenhuma mensagem saiu no período"
                    : met.total.amostra_pequena
                      ? `amostra pequena (< ${amostraMinima} envios)`
                      : "respostas ÷ mensagens enviadas"}
                </div>
              </div>
              <div className={s.stat}>
                <div className={s.statLabel}>tarefas de ligação</div>
                <div className={s.statValor}>{met.total.tarefas}</div>
                <div className={s.statSub}>geradas pra corretora</div>
              </div>
            </div>

            {/* ── VISITA: o que a resposta virou ──
                "marcadas" e "compareceram" ficam em caixas separadas de
                propósito. Somadas, a tela diria que a automação entrega o
                dobro do que entrega — e visita marcada que ninguém confirmou
                não é visita feita. */}
            {met.visitas?.disponivel && (
              <div className={s.resumo}>
                <div className={s.stat}>
                  <div className={s.statLabel}>visitas marcadas</div>
                  <div className={s.statValor}>{met.visitas.marcadas}</div>
                  <div className={s.statSub}>agenda criada no período</div>
                </div>
                <div className={s.stat}>
                  <div className={s.statLabel}>compareceram</div>
                  <div className={s.statValor}>{met.visitas.compareceram}</div>
                  <div className={s.statSub}>confirmadas por alguém</div>
                </div>
                <div className={s.stat}>
                  <div className={s.statLabel}>comparecimento</div>
                  <div className={met.visitas.sem_dado ? s.semDado : s.statValor}>
                    {pct(met.visitas.taxa_comparecimento)}
                  </div>
                  <div className={s.statSub}>
                    {met.visitas.sem_dado
                      ? "nenhuma visita marcada no período"
                      : met.visitas.amostra_pequena
                        ? `amostra pequena (< ${amostraMinima} visitas)`
                        : "compareceram ÷ marcadas"}
                  </div>
                </div>
                {!!met.visitas.vencidas_sem_confirmacao && (
                  <div className={`${s.stat} ${s.statAlerta}`}>
                    <div className={s.statLabel}>vencidas sem confirmação</div>
                    <div className={s.statValor}>
                      {met.visitas.vencidas_sem_confirmacao}
                    </div>
                    <div className={s.statSub}>
                      a data passou e ninguém fechou o registro (fora da conta)
                    </div>
                  </div>
                )}
              </div>
            )}

            <TabelaToques met={met} />
          </>
        )}

        <details className={s.comoFunciona}>
          <summary>Mais sobre o funcionamento</summary>
          <div className={s.rodape}>
            {met?.definicao}
            <br />
            Régua lida do motor que roda de verdade: se um toque entrar ou sair do código, ele entra ou sai
            desta página sozinho. Recomeçar a medição não apaga nenhum registro de envio.
          </div>
        </details>
      </div>
    </div>
  );
}
