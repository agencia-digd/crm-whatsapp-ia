"use client";

// BOAS-VINDAS — a página de UMA automação, no molde da do remarketing (06/10/2026).
//
// O que ela faz, do jeito que o motor faz (lead_intake.py): lead novo cai em
// "Novo" → a ROLETA escolhe a corretora dona → UMA mensagem sai pela linha
// Cloud DELA, com botão "Saiba mais" → fim, aguarda a corretora.
//
// A página responde, sem rolar: está ligada? está agindo? por qual linha (as da
// roleta, cada uma com a cor dela na Meta)? pra quem? quando? o que saiu em
// 24 h e em 7 dias? Embaixo:
//   - "Ajustar": a roleta (quem recebe e em que ordem) e as fatias do teste A/B,
//     num rascunho só, com uma barra de salvar que não liga nada. Antes cada
//     clique na roleta gravava na hora;
//   - "Como funciona hoje": o desenho, aberto;
//   - "Resultado": os números e o teste A/B, com o texto de cada versão.
//
// Leitura honesta, a de sempre: "sem dado" ≠ 0%; abaixo de N envios a taxa vem
// marcada; envio simulado conta à parte e não entra na taxa; a versão que conta
// é a GRAVADA no envio.

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import { Node, WaPreview } from "../blocos";
import Canvas, { Edge, corrente } from "../Canvas";
import { useConfigOperacional } from "../configOperacional";
import { estadoBoasVindas } from "../estados";
import {
  BarraSalvar,
  Bolinha,
  EntregaTexto,
  EstadoAutomacao,
  Linhas,
  MarcoBarra,
  dataCurta,
  horaCurta,
  linhaDaCorretora,
  linhaDoServidor,
  mil,
  pct,
  ultimoEnvio,
  useJanelas,
  useSalvo,
} from "../painel";
import { BoasVindas, CorretoraBoasVindas, Mapa, MetricasBoasVindas, VarianteBoasVindas } from "../types";
import b from "./boasvindas.module.css";
import d from "../remarketing/disparo.module.css";
import p from "../painel.module.css";
import s from "../remarketing/remarketing.module.css";

const PERIODOS = [7, 30, 90];
const LARGURA_NO = 320;
const COPY_MAX = 900;

type CfgOp = ReturnType<typeof useConfigOperacional>;

// ── números de uma variante (as mesmas travas do resto do painel) ──
function Numeros({
  bloco,
  amostraMinima,
}: {
  bloco: { enviadas: number; responderam: number; taxa: number | null; sem_dado: boolean; amostra_pequena: boolean } | undefined;
  amostraMinima: number;
}) {
  if (!bloco) return <div className={s.metricaDetalhe}>carregando os números…</div>;
  if (bloco.sem_dado) {
    return (
      <div className={s.metricaNo}>
        <div className={s.metricaLinha}>
          <span className={s.metricaSemDado}>sem dado</span>
        </div>
        <div className={s.metricaDetalhe}>
          nenhuma boas-vindas desta versão saiu no período.
        </div>
      </div>
    );
  }
  return (
    <div className={s.metricaNo}>
      <div className={s.metricaLinha}>
        <span className={s.metricaDestaque}>{pct(bloco.taxa)}</span>
        <span className={s.metricaRotulo}>responderam</span>
        {bloco.amostra_pequena && (
          <span className={s.alerta} title={`menos de ${amostraMinima} envios`}>
            amostra pequena
          </span>
        )}
      </div>
      <div className={s.metricaDetalhe}>
        {bloco.enviadas} receberam · {bloco.responderam} responderam
      </div>
    </div>
  );
}

// ── card de UMA variante do A/B: texto, peso, hipótese, preview e resultado ──
function CardVariante({
  v,
  corretora,
  met,
  amostraMinima,
  overrides,
  cfgOp,
  onSalvou,
}: {
  v: VarianteBoasVindas;
  corretora: CorretoraBoasVindas | null;
  met: MetricasBoasVindas | null;
  amostraMinima: number;
  overrides: Record<string, string>;
  cfgOp: CfgOp;
  onSalvou: () => void;
}) {
  const [editando, setEditando] = useState(false);
  const [texto, setTexto] = useState(v.texto);
  const [erro, setErro] = useState<string | null>(null);
  const ocupada = cfgOp.ocupado === "boas_vindas_copies";
  const numeros = met?.variantes.find((m) => m.id === v.id);

  const salvar = async (novoTexto: string | null) => {
    setErro(null);
    // Texto vazio = tirar o override → o backend volta pro texto do código.
    const mapa = { ...overrides, [v.id]: novoTexto ?? "" };
    await cfgOp.gravar("boas_vindas_copies", mapa);
    setEditando(false);
    onSalvou();
  };

  return (
    <div className={`${b.variante} ${v.ativa ? "" : b.varianteOff}`}>
      <div className={b.varianteTopo}>
        <span className={b.varianteNome}>{v.rotulo}</span>
        {v.controle && <span className={b.selo}>controle</span>}
        {/* "aguardando a próxima rodada" ≠ "aposentada". A primeira ainda não
            competiu; a segunda perdeu. Chamar as duas do mesmo jeito faria a
            Dona achar que uma copy nova já tinha sido testada e reprovada. */}
        <span className={v.aguardando ? `${b.selo} ${b.seloEspera}` : b.selo}>
          {v.ativa ? `${v.peso}% da fila` : v.estado}
        </span>
        {/* categoria é a mesma nas três de propósito: o teste é de TEXTO. Se uma
            saísse em marketing, o resultado não diria se ganhou pela copy ou
            pela categoria (foi o que aconteceu no carrossel). */}
        <span className={b.selo}>utility</span>
        <span className={v.personalizada ? `${b.selo} ${b.seloEditado}` : b.selo}>
          {v.personalizada ? "editada no painel" : "padrão do sistema"}
        </span>
      </div>

      <div className={b.hipotese}>{v.hipotese}</div>

      <Numeros bloco={numeros} amostraMinima={amostraMinima} />

      {corretora ? (
        <WaPreview
          preview={corretora.previews[v.id]}
          comBotao
          botoes={["Saiba mais", "Parar mensagens"]}
          template="reeng_b · UTILITY"
        />
      ) : (
        <div className={s.metricaDetalhe}>
          escolha uma linha acima pra ver como a mensagem chega.
        </div>
      )}

      {editando ? (
        <>
          <textarea
            className={b.editorArea}
            value={texto}
            rows={4}
            maxLength={COPY_MAX}
            disabled={ocupada}
            onChange={(e) => setTexto(e.target.value)}
          />
          <div className={b.editorBarra}>
            <span className={b.editorMeta}>
              {texto.length}/{COPY_MAX} · {"{nome}"} vira o nome da corretora da linha
            </span>
            <span style={{ flex: 1 }} />
            <button
              type="button"
              className={b.btnMini}
              disabled={ocupada || !texto.trim()}
              onClick={() => void salvar(texto)}
            >
              {ocupada ? "salvando…" : "salvar o texto"}
            </button>
            <button
              type="button"
              className={b.btnMini}
              disabled={ocupada}
              onClick={() => {
                setTexto(v.texto);
                setEditando(false);
              }}
            >
              cancelar
            </button>
          </div>
          {v.personalizada && (
            <div className={b.editorMeta} style={{ marginTop: 6 }}>
              padrão do sistema: {v.padrao}
            </div>
          )}
        </>
      ) : (
        <div className={b.editorBarra}>
          <button
            type="button"
            className={b.btnMini}
            onClick={() => {
              setTexto(v.texto);
              setEditando(true);
            }}
          >
            editar a mensagem
          </button>
          {v.personalizada && (
            <button
              type="button"
              className={b.btnMini}
              disabled={ocupada}
              title="Volta pro texto do sistema"
              onClick={() => void salvar(null)}
            >
              voltar ao texto padrão
            </button>
          )}
        </div>
      )}
      {cfgOp.erro && editando && <div className={s.erro}>{cfgOp.erro}</div>}
      {erro && <div className={s.erro}>{erro}</div>}
    </div>
  );
}

// ── AJUSTAR: a roleta e as fatias, num rascunho só ──
//
// A roleta decide de QUEM é o lead novo, e é a linha DELA que manda a mensagem.
// Mexer na ordem ou tirar alguém muda pra onde vai o próximo lead: salvar pede
// confirmação com a fila inteira na frase. As fatias têm que somar 100 (o
// servidor recusa; aqui só se antecipa). Salvar não liga a boas-vindas.
//
// Desde 06/10 a roleta grava pela rota própria (`PUT …/boas-vindas/roleta`):
// o servidor só aceita corretora ativa, sem repetir, e vazia só com
// `confirmar_vazia` (sem ela, 409). A resposta traz o que ficou no banco, relido;
// se não for o que a tela pediu, a tela diz, em vez de "gravado".
function Ajustar({
  bv,
  mapa,
  met,
  cfgOp,
  onSalvou,
}: {
  bv: Mapa["boas_vindas"];
  mapa: Mapa;
  met: MetricasBoasVindas | null;
  cfgOp: CfgOp;
  onSalvou: () => void;
}) {
  const { dados, gravar } = cfgOp;
  const numeros = mapa.carrossel_corretor?.linha_disparadora?.numeros;
  const porCorretora = bv.por_corretora ?? [];
  const rol = bv.roleta ?? null;
  const variantes = (bv.variantes ?? []).filter((v) => !v.aguardando);
  const roletaGravada = (rol?.gravada ?? (dados?.config.roleta_corretoras?.valor as number[] | undefined) ?? []) as number[];
  const pesosGravados = Object.fromEntries(variantes.map((v) => [v.id, v.peso]));
  const assinatura = JSON.stringify([roletaGravada, pesosGravados]);

  const [roleta, setRoleta] = useState<number[]>([]);
  const [pesos, setPesos] = useState<Record<string, string>>({});
  const [erros, setErros] = useState<Record<string, string>>({});
  const [salvando, setSalvando] = useState(false);
  const [salvo, setSalvo] = useSalvo();

  function voltarAoGravado() {
    const [r, pw] = JSON.parse(assinatura) as [number[], Record<string, number>];
    setRoleta(r);
    setPesos(Object.fromEntries(Object.entries(pw).map(([k, v]) => [k, String(v)])));
    setErros({});
  }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(voltarAoGravado, [assinatura]);

  if (!dados) return null;

  // As corretoras que podem entrar: as ativas do cadastro, como o servidor da
  // roleta as lê (sem ele, as da configuração).
  const corretoras = rol ? rol.corretoras.map((c) => ({ id: c.corretora_id, label: c.label })) : dados.corretoras;
  const nome = (id: number) => corretoras.find((c) => c.id === id)?.label ?? `#${id}`;
  const daRoleta = (id: number) => rol?.corretoras.find((c) => c.corretora_id === id) ?? null;
  const linhaDe = (id: number) => {
    const r = daRoleta(id);
    if (r) return linhaDoServidor(r.label, r.linha);
    const c = porCorretora.find((x) => x.corretora_id === id);
    return c ? linhaDaCorretora(c, numeros) : null;
  };
  const ultima = rol?.historico?.[0] ?? null;
  const medido = new Map(met?.por_corretora.map((m) => [m.corretora_id, m]) ?? []);

  const mudouRoleta = roleta.join() !== roletaGravada.join();
  const mudouPesos = variantes.some((v) => String(v.peso) !== (pesos[v.id] ?? ""));
  const soma = variantes.reduce((t, v) => t + (Number(pesos[v.id]) || 0), 0);
  const pesosValidos = variantes.every((v) => /^\d+$/.test((pesos[v.id] ?? "").trim()) && Number(pesos[v.id]) <= 100);
  const quantas = Number(mudouRoleta) + Number(mudouPesos);
  // Roleta vazia é permitida, mas só com a confirmação que diz o que acontece
  // (o servidor exige `confirmar_vazia`). Sem servidor novo, segue travada.
  const bloqueio =
    roleta.length === 0 && !rol
      ? "a roleta precisa de pelo menos uma corretora"
      : mudouPesos && (!pesosValidos || soma !== 100)
        ? `as fatias somam ${soma}%, precisam dar 100%`
        : null;

  const mover = (i: number, delta: number) => {
    const nova = [...roleta];
    [nova[i], nova[i + delta]] = [nova[i + delta], nova[i]];
    setRoleta(nova);
  };

  async function salvar() {
    const vazia = mudouRoleta && roleta.length === 0;
    if (mudouRoleta) {
      const semLinha = roleta.filter((id) => linhaDe(id)?.temLinha === false).map(nome);
      const fila = roleta.map((id, i) => `${i + 1}º ${nome(id)}`).join(", ");
      const pergunta = vazia
        ? "Deixar a roleta VAZIA?\n\nLead novo do Meta passa a entrar SEM dona, e ninguém recebe a boas-vindas " +
          "até alguém pôr uma corretora de volta. Quem já tem dona continua com ela.\n\nSalvar não liga a boas-vindas."
        : `Gravar a roleta?\n\nO próximo lead novo vai, em rodízio, pra: ${fila}.` +
          (semLinha.length ? `\n\n${semLinha.join(", ")} não tem linha Cloud: o lead dela entra sem mensagem.` : "") +
          "\n\nSalvar não liga a boas-vindas.";
      if (!window.confirm(pergunta)) return;
    }
    setSalvando(true);
    const novos: Record<string, string> = {};
    if (mudouRoleta) {
      if (rol) {
        try {
          const r = await fetch("/api/automacoes/boas-vindas/roleta", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ corretoras: roleta, confirmar_vazia: vazia }),
          });
          const out = await r.json().catch(() => ({}));
          if (!r.ok) throw new Error(out?.detail || out?.error || `HTTP ${r.status}`);
          // O banco, relido, tem que ter o que a tela pediu.
          if (!out.ok || JSON.stringify(out.gravado) !== JSON.stringify(roleta)) {
            novos.roleta = `o servidor gravou ${JSON.stringify(out.gravado)}, não o que foi pedido. Recarregue antes de mexer de novo.`;
          }
        } catch (e) {
          novos.roleta = String((e as Error).message || e);
        }
      } else {
        const r = await gravar("roleta_corretoras", roleta, true);
        if (!r.ok) novos.roleta = r.erro ?? "não consegui gravar";
      }
    }
    if (mudouPesos) {
      const r = await gravar(
        "boas_vindas_pesos",
        Object.fromEntries((bv.variantes ?? []).map((v) => [v.id, v.aguardando ? v.peso : Number(pesos[v.id])])),
        true,
      );
      if (!r.ok) novos.pesos = r.erro ?? "não consegui gravar";
    }
    setErros(novos);
    setSalvando(false);
    if (Object.keys(novos).length === 0) setSalvo(true);
    onSalvou();
  }

  return (
    <section className={`${d.ajustes} ${quantas > 0 ? d.ajustesComMudanca : ""}`} aria-label="Ajustar a boas-vindas">
      <h2 className={d.ajustesTitulo}>Ajustar</h2>
      <div className={d.grade}>
        <div className={`${d.bloco} ${d.blocoLargo}`} data-testid="bloco-roleta">
          <h3 className={d.blocoTitulo}>Quem recebe lead novo</h3>
          <ol className={p.fila} data-testid="roleta">
            {roleta.map((id, i) => {
              const l = linhaDe(id);
              const m = medido.get(id);
              const r = daRoleta(id);
              // "A vez" só vale pra roleta gravada: no rascunho a ordem mudou.
              const aVez = !mudouRoleta && r?.e_a_vez;
              return (
                <li key={id} className={p.filaItem} data-corretora={nome(id)} data-vez={aVez ? "sim" : undefined}>
                  <span className={p.filaPos}>{i + 1}º</span>
                  <span className={p.filaNome}>
                    <Bolinha cor={l && l.temLinha ? l.saude.cor : "desconhecida"} />
                    {nome(id)}
                    {aVez && <span className={b.selo}>a vez é dela</span>}
                    {l && !l.temLinha && <span className={d.dica}>sem linha, entra sem mensagem</span>}
                    {r && r.recebeu_7d !== null ? (
                      <span className={d.dica}>
                        recebeu {r.recebeu_7d} em 7 dias, {r.recebeu_30d} em 30
                      </span>
                    ) : (
                      m &&
                      m.participacao !== null && (
                        <span className={d.dica}>
                          {pct(m.participacao)} dos leads · {m.enviadas} no período
                        </span>
                      )
                    )}
                  </span>
                  <span className={p.filaBotoes}>
                    <button type="button" className={p.mini} disabled={i === 0} aria-label={`Subir ${nome(id)}`} onClick={() => mover(i, -1)}>
                      ↑
                    </button>
                    <button
                      type="button"
                      className={p.mini}
                      disabled={i === roleta.length - 1}
                      aria-label={`Descer ${nome(id)}`}
                      onClick={() => mover(i, 1)}
                    >
                      ↓
                    </button>
                    <button
                      type="button"
                      className={p.mini}
                      disabled={roleta.length <= 1 && !rol}
                      aria-label={`Tirar ${nome(id)} da roleta`}
                      onClick={() => setRoleta(roleta.filter((x) => x !== id))}
                    >
                      tirar
                    </button>
                  </span>
                </li>
              );
            })}
          </ol>
          {corretoras.some((c) => !roleta.includes(c.id)) && (
            <div className={p.fora}>
              <span className={d.rotulo}>fora da roleta</span>
              {corretoras
                .filter((c) => !roleta.includes(c.id))
                .map((c) => (
                  <button key={c.id} type="button" className={p.foraBtn} onClick={() => setRoleta([...roleta, c.id])}>
                    + {c.label}
                  </button>
                ))}
            </div>
          )}
          {roleta.length === 0 && (
            <p className={d.erroTexto} data-testid="roleta-vazia">
              Roleta vazia: lead novo do Meta entra sem dona e ninguém recebe a boas-vindas.
            </p>
          )}
          {!mudouRoleta && rol?.aviso && !rol.vazia && <p className={d.erroTexto}>{rol.aviso}</p>}
          {erros.roleta && <p className={d.erroTexto} data-testid="roleta-erro">{erros.roleta}</p>}
          <p className={d.dica}>
            Rodízio na ordem da lista. Vale a partir do próximo lead; quem já tem dona fica com ela. Card com RMKT não passa
            por aqui.
          </p>
          {ultima && (
            <p className={d.dica} data-testid="roleta-ultima-mudanca">
              Última mudança: {ultima.em ? `${dataCurta(ultima.em)} às ${horaCurta(ultima.em)}` : "sem data"}
              {ultima.autor ? `, por ${ultima.autor}` : ""}.
            </p>
          )}
        </div>

        {variantes.length > 1 && (
          <div className={d.bloco} data-testid="bloco-fatias">
            <h3 className={d.blocoTitulo}>Fatia de cada versão</h3>
            <div className={p.pares}>
              {variantes.map((v) => (
                <label key={v.id} className={p.par}>
                  <span className={p.parNome}>{v.rotulo.split("·")[0].trim()}</span>
                  <input
                    type="number"
                    min={0}
                    max={100}
                    inputMode="numeric"
                    className={`${d.campo} ${pesosValidos ? "" : p.campoErro}`}
                    value={pesos[v.id] ?? ""}
                    onChange={(e) => setPesos((x) => ({ ...x, [v.id]: e.target.value }))}
                    aria-label={`fatia da versão ${v.rotulo} em %`}
                  />
                </label>
              ))}
            </div>
            <p className={soma === 100 ? d.contagem : d.erroTexto} data-testid="soma-fatias">
              soma <strong>{soma}%</strong>
              {soma === 100 ? "" : ", precisa dar 100%"}
            </p>
            {erros.pesos && <p className={d.erroTexto}>{erros.pesos}</p>}
            <p className={d.dica}>Cada lead fica sempre na mesma versão.</p>
          </div>
        )}
      </div>
      <BarraSalvar
        quantas={quantas}
        salvando={salvando}
        salvo={salvo}
        nota="salvar não liga a boas-vindas"
        bloqueio={bloqueio}
        onDescartar={voltarAoGravado}
        onSalvar={() => void salvar()}
      />
    </section>
  );
}

export default function ClienteBoasVindas() {
  const cfgOp = useConfigOperacional();
  const [mapa, setMapa] = useState<Mapa | null>(null);
  const [met, setMet] = useState<MetricasBoasVindas | null>(null);
  const [metErro, setMetErro] = useState<string | null>(null);
  const [erro, setErro] = useState<string | null>(null);
  const [periodo, setPeriodo] = useState(30);
  const [recarga, setRecarga] = useState(0);
  const [corretoraSel, setCorretoraSel] = useState<number | null>(null);
  const janelas = useJanelas<MetricasBoasVindas>("/api/automacoes/boas-vindas/metricas", recarga);

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
    fetch(`/api/automacoes/boas-vindas/metricas?dias=${periodo}`, { cache: "no-store" })
      .then(async (r) => {
        const body = await r.json();
        if (!r.ok) throw new Error(body?.error || body?.detail || `HTTP ${r.status}`);
        setMet(body);
      })
      .catch((e) => setMetErro(String((e as Error).message || e)));
  }, [periodo, recarga]);

  const bv = mapa?.boas_vindas ?? null;
  const porCorretora = useMemo(() => bv?.por_corretora ?? [], [bv]);

  // Linha escolhida pra ler a mensagem: a primeira COM linha Cloud (é a única
  // pra quem a automação roda de verdade). Nada de índice fixo.
  useEffect(() => {
    if (corretoraSel !== null || porCorretora.length === 0) return;
    const comLinha = porCorretora.find((x) => x.tem_linha_cloud) ?? porCorretora[0];
    setCorretoraSel(comLinha.corretora_id);
  }, [porCorretora, corretoraSel]);

  const corretora =
    porCorretora.find((x) => x.corretora_id === corretoraSel) ?? porCorretora[0] ?? null;

  const edges: Edge[] = useMemo(() => {
    const es = corrente(["bv-g", "bv-roleta", "bv-msg", "bv-fim"]);
    // saídas: quem não tem linha Cloud, quem responde, e o pulo pro follow-up.
    es.push({ from: "bv-roleta", to: "bv-semlinha", kind: "branch", tone: "warn", dashed: true });
    es.push({ from: "bv-msg", to: "bv-resposta", kind: "branch", tone: "warn", dashed: true });
    es.push({ from: "bv-fim", to: "bv-followup", kind: "branch", tone: "warn", dashed: true });
    return es;
  }, []);

  if (erro) {
    return (
      <div className="estado">
        <h2>Boas-vindas indisponível</h2>
        <p>Não consegui falar com o copiloto. Tenta de novo em instantes.</p>
        <div className="estado-meta">{erro}</div>
      </div>
    );
  }
  if (!mapa || !bv) {
    return (
      <div className="estado">
        <h2>Carregando as boas-vindas…</h2>
      </div>
    );
  }

  const amostraMinima = met?.amostra_minima ?? 20;
  const semLinha = porCorretora.filter((x) => !x.tem_linha_cloud);
  let num = 0;

  // ── o topo ──
  const est = estadoBoasVindas(mapa, cfgOp.dados);
  const chave = cfgOp.dados?.config.lead_intake_ativo;
  const j1 = janelas.d1?.total ?? null;
  const j7 = janelas.d7?.total ?? null;
  const ult = ultimoEnvio(mapa.saude_envio?.["boas-vindas"]);
  const comLinha = est.linhas.filter((l) => l.temLinha);
  const fila = est.naRoleta.map((c, i) => `${i + 1}º ${c.label}`).join(", ");
  const aguardando = bv.aguardando;
  // Quem está em Novo sem a mensagem, por dona (06/10). Pessoa7 e Corretor não estão
  // na roleta e recebem lead por ligação: aparecem aqui, não são defeito.
  const emNovo = bv.envios?.em_novo ?? null;
  const nomeDe = (id: string) =>
    bv.roleta?.corretoras.find((c) => String(c.corretora_id) === id)?.label ??
    porCorretora.find((c) => String(c.corretora_id) === id)?.label ??
    `#${id}`;
  const emNovoPorDona = emNovo
    ? Object.entries(emNovo.por_corretora)
        .filter(([, v]) => v.sem_boas_vindas > 0)
        .sort((a, b) => b[1].sem_boas_vindas - a[1].sem_boas_vindas)
        .map(([id, v]) => `${nomeDe(id)} ${mil(v.sem_boas_vindas)}`)
    : [];
  const envios = bv.envios ?? null;
  const semLeitura = <span>{janelas.erro ? "não consegui ler" : "carregando…"}</span>;

  return (
    <div>
      <div className={s.wrap}>
        <Link href="/automacoes" className={s.voltar}>
          ← todas as automações
        </Link>

        <header className={s.cabecalho}>
          <h1 className={s.nome}>Boas-vindas</h1>
          <p className={s.gatilho}>
            Quando o lead cai em Novo, a roleta escolhe a dona e a linha dela manda uma mensagem. Depois, aguarda a corretora.
          </p>
        </header>

        <EstadoAutomacao
          rotulo="Estado da boas-vindas"
          sit={est.sit}
          chave={{
            ligado: cfgOp.dados ? est.ligado : null,
            carregando: !cfgOp.dados && !cfgOp.erro,
            ocupado: cfgOp.ocupado === "lead_intake_ativo",
            rotulo: est.ligado ? "Desligar a boas-vindas" : "Ligar a boas-vindas",
            confirmarLigar:
              `Ligar a boas-vindas?\n\nCada lead novo que cair em Novo recebe 1 mensagem pela linha da dona. ` +
              `Roleta: ${fila || "ninguém"} (${comLinha.length} ${comLinha.length === 1 ? "linha" : "linhas"}).` +
              (j7 ? ` Nos últimos 7 dias saíram ${mil(j7.enviadas)}.` : "") +
              (est.trava ? `\n\nAgora ainda não sai: ${est.trava}.` : ""),
            confirmarDesligar:
              "Desligar a boas-vindas?\n\nLead novo continua entrando no CRM e ganhando dona pela roleta, " +
              "mas ninguém recebe a primeira mensagem.",
            onMudar: (novo) => void cfgOp.gravar("lead_intake_ativo", novo).then(carregarMapa),
          }}
          respostas={[
            {
              rotulo: "por qual linha",
              testid: "resposta-linha",
              valor: "linha da dona",
              sub: <Linhas linhas={est.linhas} testid="linhas-das-corretoras" />,
              ruim: est.linhas.some((l) => l.temLinha && l.saude.cor === "vermelho"),
            },
            {
              rotulo: "pra quem",
              testid: "resposta-quem",
              valor: aguardando === undefined || aguardando === null ? "lead novo" : mil(aguardando),
              sub:
                aguardando === undefined || aguardando === null
                  ? "1 vez por lead, ao cair em Novo"
                  : emNovoPorDona.length > 0
                    ? `em Novo sem a mensagem: ${emNovoPorDona.join(", ")}${emNovo && emNovo.sem_dona > 0 ? `, sem dona ${mil(emNovo.sem_dona)}` : ""}`
                    : "em Novo esperando a mensagem",
            },
            {
              rotulo: "quando e quanto",
              testid: "resposta-quando",
              valor: "na hora",
              sub: `1 mensagem por lead · roleta com ${est.naRoleta.length}`,
            },
            {
              rotulo: "últimas 24 h",
              testid: "resposta-hoje",
              valor: j1 ? (
                <>
                  {mil(j1.enviadas)}
                  <span className={d.de}> saíram</span>
                </>
              ) : (
                "—"
              ),
              sub: j7 ? `${mil(j7.enviadas)} em 7 dias` : "7 dias sem leitura",
            },
          ]}
          faixa={[
            {
              rotulo: "responderam",
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
                    <span>{pct(j7.taxa)} em 7 dias</span>
                  </>
                ) : (
                  semLeitura
                ),
            },
            {
              rotulo: "simuladas",
              testid: "faixa-simuladas",
              conteudo: j7 ? (
                <span>
                  {j7.simuladas > 0 ? `${mil(j7.simuladas)} em 7 dias, fora da conta` : "nenhuma em 7 dias"}
                </span>
              ) : (
                semLeitura
              ),
            },
            {
              rotulo: "entrega na Meta",
              testid: "faixa-entrega",
              conteudo: envios ? (
                <>
                  <EntregaTexto j={envios.total_hoje} rotulo="hoje" />
                  <EntregaTexto j={envios.total_semana} rotulo="7 dias" />
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
            cfgOp.erro || chave?.personalizado ? (
              <>
                {cfgOp.erro && <span className={d.erroTexto}>{cfgOp.erro}</span>}
                {chave?.personalizado && (
                  <button
                    type="button"
                    className={d.link}
                    disabled={cfgOp.ocupado === "lead_intake_ativo"}
                    onClick={() => {
                      const padraoLigado = Boolean(chave.padrao);
                      if (
                        padraoLigado !== est.ligado &&
                        !window.confirm(
                          padraoLigado
                            ? "O padrão do sistema é LIGADO. Voltar ao padrão liga a boas-vindas. Confirmar?"
                            : "O padrão do sistema é DESLIGADO. Voltar ao padrão desliga a boas-vindas. Confirmar?",
                        )
                      )
                        return;
                      void cfgOp.restaurar("lead_intake_ativo").then(carregarMapa);
                    }}
                  >
                    voltar o interruptor ao padrão ({chave.padrao ? "ligado" : "desligado"})
                  </button>
                )}
              </>
            ) : undefined
          }
        />

        <Ajustar
          bv={bv}
          mapa={mapa}
          met={met}
          cfgOp={cfgOp}
          onSalvou={() => {
            carregarMapa();
            setRecarga((q) => q + 1);
          }}
        />

        <h2 className={s.secaoTitulo} id="como-funciona-hoje">Como funciona hoje</h2>
        <p className={s.fluxoLegenda}>
          Uma mensagem só, e a automação para: quem continua é a corretora.
          <span className={s.fluxoArraste}> Arraste pro lado pra ver tudo.</span>
        </p>
      </div>

      <div data-testid="desenho-do-fluxo">
      <Canvas edges={edges} zoomInicial={0.8} altura="auto">
        {(reg) => (
          <div className={s.flow}>
            <div className={s.flowLabel}>UMA MENSAGEM, E AGUARDA A CORRETORA</div>
            <div className={s.row}>
              <Node reg={reg} id="bv-g" num={++num} icon="⚡" titulo="Gatilho" largura={LARGURA_NO}>
                <div>{bv.gatilho}</div>
              </Node>

              <Node
                reg={reg}
                id="bv-roleta"
                num={++num}
                icon="🎡"
                titulo="Roleta escolhe a dona"
                largura={LARGURA_NO}
              >
                <div>
                  O lead vai pra próxima corretora da fila, em rodízio, e é a
                  linha DELA que fala com ele.
                </div>
                <div className={s.tagsNo}>
                  {porCorretora
                    .filter((p) => p.na_roleta)
                    .map((p) => (
                      <span key={p.corretora_id} className={s.tagCat}>
                        {p.posicao_roleta}º {p.label}
                      </span>
                    ))}
                </div>
              </Node>

              <Node
                reg={reg}
                id="bv-msg"
                num={++num}
                icon="💬"
                titulo="Mensagem de boas-vindas"
                largura={LARGURA_NO}
              >
                <div>{bv.acao}</div>
                {corretora && (
                  <WaPreview
                    preview={corretora.previews[bv.controle ?? ""] ?? null}
                    comBotao
                    botoes={bv.botoes}
                    template={bv.template}
                  />
                )}
                <div className={s.tagsNo}>
                  <span className={s.tagCat}>1x por lead</span>
                  <span className={s.tagCat}>utility</span>
                  {(bv.variantes ?? []).filter((v) => v.ativa).length > 1 && (
                    <span className={s.tagCat}>
                      {(bv.variantes ?? []).filter((v) => v.ativa).length} versões em teste
                    </span>
                  )}
                </div>
              </Node>

              <Node
                reg={reg}
                id="bv-fim"
                num={++num}
                icon="🏁"
                titulo="Fim: aguarda a corretora"
                largura={LARGURA_NO}
              >
                <div>{bv.fim}. Nada mais sai sozinho a partir daqui.</div>
              </Node>
            </div>

            <div className={s.faixaRotulo}>O QUE ACONTECE DEPOIS</div>
            <div className={s.ramo}>
              <Node
                reg={reg}
                id="bv-semlinha"
                icon="🚫"
                titulo="Corretora sem linha Cloud"
                cond
                largura={LARGURA_NO}
              >
                <div>
                  O lead entra no CRM com dona, mas NENHUMA mensagem sai: o
                  contato dela é manual, pelo telefone dela. Mandar por outra
                  linha confundiria o lead sobre onde responder.
                </div>
                <div className={s.tagsNo}>
                  {semLinha.length === 0 ? (
                    <span className={s.tagCat}>hoje todas têm linha</span>
                  ) : (
                    semLinha.map((p) => (
                      <span key={p.corretora_id} className={s.tagCat}>
                        {p.label}
                      </span>
                    ))
                  )}
                </div>
              </Node>

              <Node
                reg={reg}
                id="bv-resposta"
                icon="🟢"
                titulo="Se o lead responder"
                cond
                largura={LARGURA_NO}
              >
                <div>
                  A conversa cai no WhatsApp da corretora dona e vira atendimento
                  humano. A automação não manda mais nada, e é essa resposta que
                  a taxa mede.
                </div>
              </Node>

              <Node
                reg={reg}
                id="bv-followup"
                icon="🔁"
                titulo="Se a corretora arrastar pro Follow-up"
                cond
                largura={LARGURA_NO}
              >
                <div>
                  Aí começa a OUTRA automação: a régua de toques do follow-up,
                  do D0 até o encerramento. É decisão dela, não do sistema.
                </div>
                <div className={s.tagsNo}>
                  <Link href="/automacoes/follow-up" className={s.tagCat}>
                    ver o follow-up
                  </Link>
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
                <div className={s.statLabel}>enviadas</div>
                <div className={s.statValor}>{met.total.enviadas}</div>
                <div className={s.statSub}>
                  {met.medindo_desde ? `desde ${dataCurta(met.medindo_desde)}` : `nos últimos ${met.dias} dias`}
                </div>
              </div>
              <div className={s.stat}>
                <div className={s.statLabel}>responderam</div>
                <div className={s.statValor}>{met.total.responderam}</div>
                <div className={s.statSub}>escreveram depois de receber</div>
              </div>
              <div className={s.stat}>
                <div className={s.statLabel}>taxa de resposta</div>
                <div className={met.total.sem_dado ? s.semDado : s.statValor}>{pct(met.total.taxa)}</div>
                <div className={s.statSub}>
                  {met.total.sem_dado
                    ? "nenhuma mensagem saiu no período"
                    : met.total.amostra_pequena
                      ? `amostra pequena (< ${amostraMinima} envios)`
                      : "responderam ÷ enviadas"}
                </div>
              </div>
              {met.total.simuladas > 0 && (
                <div className={`${s.stat} ${s.statAlerta}`}>
                  <div className={s.statLabel}>simuladas</div>
                  <div className={s.statValor}>{met.total.simuladas}</div>
                  <div className={s.statSub}>não saíram de verdade, fora da conta</div>
                </div>
              )}
            </div>
          </>
        )}

        <TesteAB bv={bv} met={met} amostraMinima={amostraMinima} cfgOp={cfgOp}
          corretora={corretora} porCorretora={porCorretora} onEscolher={setCorretoraSel}
          onSalvou={() => {
            carregarMapa();
            setRecarga((q) => q + 1);
          }}
        />

        <details className={s.comoFunciona}>
          <summary>Mais sobre o teste e o funcionamento</summary>
          <div className={s.nota}>
            Cada lead cai sempre na mesma versão (o sorteio é pelo id dele). Todas viajam na variável do mesmo
            template UTILITY aprovado: nenhuma precisa de template novo na Meta. A A (controle) é a mensagem que
            já rodava: sem ela, daria pra saber qual nova é melhor entre as novas, mas não se alguma bate o que
            existe. A A difere das outras em duas coisas (confirma o cadastro × pergunta de triagem, e termina em
            afirmação × pergunta): o que se mede é pergunta que qualifica × aviso.
          </div>
          <div className={s.rodape}>
            {met?.definicao}
            <br />
            Fluxo e textos lidos do motor que roda de verdade (`lead_intake`). Recomeçar a medição não apaga
            nenhum registro de envio.
          </div>
        </details>
      </div>
    </div>
  );
}

// ── o teste A/B: as versões lado a lado, com o texto que sai em cada linha ──
function TesteAB({
  bv,
  met,
  amostraMinima,
  cfgOp,
  corretora,
  porCorretora,
  onEscolher,
  onSalvou,
}: {
  bv: Mapa["boas_vindas"];
  met: MetricasBoasVindas | null;
  amostraMinima: number;
  cfgOp: CfgOp;
  corretora: CorretoraBoasVindas | null;
  porCorretora: CorretoraBoasVindas[];
  onEscolher: (id: number) => void;
  onSalvou: () => void;
}) {
  const variantes = useMemo(() => bv.variantes ?? [], [bv.variantes]);
  const overrides = useMemo(() => {
    const out: Record<string, string> = {};
    for (const v of variantes) if (v.personalizada) out[v.id] = v.texto;
    return out;
  }, [variantes]);
  if (variantes.length === 0) return null;
  return (
    <div className={s.abBloco} data-testid="teste-ab">
      <h3 className={s.abTitulo}>Teste A/B da primeira mensagem</h3>
      {porCorretora.length > 0 && (
        <div className={b.tabs} role="group" aria-label="ver a mensagem como chega na linha de">
          <span className={d.rotulo}>como chega na linha da</span>
          {porCorretora.map((x) => (
            <button
              key={x.corretora_id}
              type="button"
              aria-pressed={x.corretora_id === corretora?.corretora_id}
              className={`${b.tab} ${x.corretora_id === corretora?.corretora_id ? b.tabOn : ""} ${x.tem_linha_cloud ? "" : b.tabSemLinha}`}
              onClick={() => onEscolher(x.corretora_id)}
            >
              {x.label}
              {x.tem_linha_cloud ? "" : " (sem linha)"}
            </button>
          ))}
        </div>
      )}
      {variantes.map((v) => (
        <CardVariante
          key={v.id}
          v={v}
          corretora={corretora}
          met={met}
          amostraMinima={amostraMinima}
          overrides={overrides}
          cfgOp={cfgOp}
          onSalvou={onSalvou}
        />
      ))}
    </div>
  );
}
