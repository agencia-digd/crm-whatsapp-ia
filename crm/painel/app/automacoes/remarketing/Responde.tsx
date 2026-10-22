"use client";

// "QUANDO A PESSOA RESPONDE" — o que acontece com o card que cai em Respondeu.
//
// Pedido da Dona (05/10/2026): "quando cair na etapa respondeu vai ganhar tag
// RMKT, vai para etapa de entrada do funil de vendas, e vai ter roleta para
// remarketing". E, em seguida: "todo mundo que cair em Respondeu vai para a
// roleta", "vai com histórico", e o texto do aviso.
//
// Quatro perguntas, um bloco por pergunta (o desenho que ela aprovou no
// "Ajustar" do disparo): quem vai · pra onde vai · pra quem vai · o que a
// pessoa recebe. Tudo sai de `car.responde.config`, que é a MESMA leitura que
// o motor faz na hora de transferir — inclusive o `problema`: quando ele vem
// preenchido, ninguém está sendo transferido, e a tela diz por quê.
//
// O "salvar" manda tudo num pedido (`PUT …/remarketing/quando-responde`) e o
// servidor grava tudo ou nada. Erro volta por campo e aparece ao lado dele.
// Mudou quem recebe lead (roleta, quem vai, etapa)? Pergunta antes, com os
// nomes na frase.

import { useEffect, useMemo, useState } from "react";
import type { Mapa } from "../types";
import Previa, { canonizar } from "./previa";
import c from "./copy.module.css";
import s from "./disparo.module.css";

type Carrossel = Mapa["carrossel_corretor"];
type Campo =
  | "handoff_remarketing_quem_vai"
  | "handoff_remarketing_etapa"
  | "handoff_remarketing_roleta"
  | "handoff_remarketing_avisa_lead"
  | "handoff_remarketing_aviso_texto"
  | "handoff_remarketing_aviso_resumo"
  | "handoff_remarketing_respeita_humano"
  | "ia_resumo_card"
  | "ia_opina_indefinida"
  | "ia_opina_corte";

// Cortes de confiança oferecidos. O padrão vem do servidor (medido nas 87
// indefinidas reais de 05/10); um valor gravado fora da lista também aparece.
const CORTES = [0.7, 0.8, 0.85, 0.9, 0.95];
const pct = (v: number) => `${Math.round(v * 100)}%`;

const QUEM = [
  { valor: "todos", rotulo: "Todo mundo que responder" },
  { valor: "interesse", rotulo: "Só botão ou interesse" },
] as const;

const lista = (nomes: string[]) =>
  nomes.length <= 1 ? nomes.join("") : `${nomes.slice(0, -1).join(", ")} e ${nomes[nomes.length - 1]}`;

export default function QuandoResponde({ car, onSalvou }: { car: Carrossel; onSalvou: () => void }) {
  const cfg = car.responde?.config ?? null;
  const etapaCfg = cfg?.etapa ?? null;
  const roletaCfg = cfg?.roleta ?? null;
  const avisoCfg = cfg?.aviso ?? null;

  const [quem, setQuem] = useState("todos");
  const [etapa, setEtapa] = useState("");
  const [roleta, setRoleta] = useState<number[]>([]);
  const [avisa, setAvisa] = useState(false);
  const [texto, setTexto] = useState("");
  const [resumo, setResumo] = useState(false);
  const [humano, setHumano] = useState(true);
  const [iaResumo, setIaResumo] = useState(true);
  const [iaOpina, setIaOpina] = useState(true);
  const [iaCorte, setIaCorte] = useState("");
  const [erros, setErros] = useState<Partial<Record<Campo, string>>>({});
  const [salvando, setSalvando] = useState(false);
  const [salvo, setSalvo] = useState(false);

  // O rascunho nasce do que está gravado e volta pra lá depois de salvar.
  // Depende dos VALORES: o mapa recarrega a cada ação e um objeto novo com os
  // mesmos valores não pode apagar o que está sendo digitado.
  const assinatura = JSON.stringify([
    cfg?.quem_vai ?? null,
    etapaCfg?.gravada ?? etapaCfg?.padrao ?? null,
    roletaCfg?.ids ?? [],
    avisoCfg?.ligado ?? false,
    avisoCfg?.texto ?? avisoCfg?.padrao ?? "",
    avisoCfg?.com_resumo ?? false,
    cfg?.respeita_humano ?? true,
    cfg?.ia?.resumo_ligado ?? false,
    cfg?.ia?.opina_ligada ?? false,
    cfg?.ia?.corte ?? null,
  ]);
  const gravado = useMemo(() => {
    const [q, e, r, a, t, re, h, ir, io, ic] = JSON.parse(assinatura) as [
      string | null,
      number | null,
      number[],
      boolean,
      string,
      boolean,
      boolean,
      boolean,
      boolean,
      number | null,
    ];
    return {
      quem: q, etapa: e, roleta: r, avisa: a, texto: t, resumo: re, humano: h,
      iaResumo: ir, iaOpina: io, iaCorte: ic,
    };
  }, [assinatura]);

  function voltarAoGravado() {
    setQuem(gravado.quem ?? "todos");
    setEtapa(gravado.etapa === null ? "" : String(gravado.etapa));
    setRoleta(gravado.roleta);
    setAvisa(gravado.avisa);
    setTexto(gravado.texto);
    setResumo(gravado.resumo);
    setHumano(gravado.humano);
    setIaResumo(gravado.iaResumo);
    setIaOpina(gravado.iaOpina);
    setIaCorte(gravado.iaCorte === null ? "" : String(gravado.iaCorte));
    setErros({});
  }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(voltarAoGravado, [gravado]);

  useEffect(() => {
    if (!salvo) return;
    const t = setTimeout(() => setSalvo(false), 4000);
    return () => clearTimeout(t);
  }, [salvo]);

  if (!car.responde) return null; // servidor antigo: não inventa bloco
  if (!cfg || !etapaCfg || !roletaCfg || !avisoCfg) {
    return (
      <div className={s.responde} data-testid="quando-responde">
        <h3 className={s.respondeTitulo}>Quando a pessoa responde</h3>
        <p className={s.aviso}>
          Não consegui ler o que acontece com quem responde
          {car.responde.motivo_indisponivel || cfg?.detalhe
            ? `: ${car.responde.motivo_indisponivel ?? cfg?.detalhe}`
            : ""}
          . Enquanto isso ninguém é transferido.
        </p>
      </div>
    );
  }

  const nomeDe = (id: number) => roletaCfg.corretoras.find((x) => x.id === id)?.nome ?? `#${id}`;
  // A ordem da roleta é a do cadastro (a mesma em que a tela lista).
  const roletaOrdenada = roletaCfg.corretoras.filter((x) => roleta.includes(x.id)).map((x) => x.id);
  const textoLimpo = canonizar(texto);
  const estourou = textoLimpo.length > avisoCfg.max;

  const mudou: Record<Campo, boolean> = {
    handoff_remarketing_quem_vai: quem !== (gravado.quem ?? "todos"),
    handoff_remarketing_etapa: etapa !== "" && etapa !== (gravado.etapa === null ? "" : String(gravado.etapa)),
    handoff_remarketing_roleta: roletaOrdenada.join() !== gravado.roleta.join(),
    handoff_remarketing_avisa_lead: avisa !== gravado.avisa,
    handoff_remarketing_aviso_texto: textoLimpo !== gravado.texto,
    handoff_remarketing_aviso_resumo: resumo !== gravado.resumo,
    handoff_remarketing_respeita_humano: cfg.respeita_humano !== undefined && humano !== gravado.humano,
    ia_resumo_card: cfg.ia !== undefined && iaResumo !== gravado.iaResumo,
    ia_opina_indefinida: cfg.ia !== undefined && iaOpina !== gravado.iaOpina,
    ia_opina_corte:
      cfg.ia !== undefined && iaCorte !== "" && iaCorte !== (gravado.iaCorte === null ? "" : String(gravado.iaCorte)),
  };
  const quantas = Object.values(mudou).filter(Boolean).length;

  async function salvar() {
    // Mudou QUEM RECEBE LEAD: pergunta com os nomes na frase.
    const frases: string[] = [];
    if (mudou.handoff_remarketing_quem_vai) {
      frases.push(
        quem === "todos"
          ? "Todo mundo que cair em Respondeu passa a ir pra corretora (inclui “bom dia”, áudio e figurinha)."
          : "Só quem clicar no botão ou mostrar interesse passa a ir pra corretora.",
      );
    }
    if (mudou.handoff_remarketing_roleta) {
      frases.push(
        roletaOrdenada.length === 0
          ? "A roleta do remarketing fica VAZIA: ninguém recebe, quem responder fica em Respondeu."
          : `A roleta do remarketing passa a ser: ${lista(roletaOrdenada.map(nomeDe))}.`,
      );
    }
    if (mudou.handoff_remarketing_etapa) {
      const nome = etapaCfg!.opcoes.find((o) => String(o.id) === etapa)?.nome ?? etapa;
      frases.push(`O card passa a entrar na etapa “${nome}”.`);
    }
    if (mudou.handoff_remarketing_respeita_humano) {
      frases.push(
        humano
          ? "Quem já está conversando pelo celular da linha da Corretor deixa de ser transferido sozinho."
          : "Quem já está conversando pelo celular da linha da Corretor passa a ser transferido também.",
      );
    }
    if (mudou.ia_opina_indefinida && iaOpina) {
      frases.push(
        `A IA passa a opinar no que a regra não entendeu: recusa com ${pct(Number(iaCorte || cfg!.ia!.corte_padrao))} de certeza ou mais vai pra Venda perdida.`,
      );
    }
    if (mudou.ia_opina_corte && iaOpina && !mudou.ia_opina_indefinida) {
      frases.push(`A recusa lida pela IA passa a precisar de ${pct(Number(iaCorte))} de certeza pra ir pra Venda perdida.`);
    }
    if (mudou.handoff_remarketing_avisa_lead && avisa) {
      frases.push("O lead passa a receber o aviso pela linha da corretora.");
    }
    if (
      frases.length > 0 &&
      !window.confirm(
        `${frases.join("\n")}\n\nVale a partir da próxima resposta. Quem já está em Respondeu não é transferido agora.`,
      )
    ) {
      return;
    }

    const corpo: Partial<Record<Campo, unknown>> = {};
    if (mudou.handoff_remarketing_quem_vai) corpo.handoff_remarketing_quem_vai = quem;
    if (mudou.handoff_remarketing_etapa) corpo.handoff_remarketing_etapa = Number(etapa);
    if (mudou.handoff_remarketing_roleta) corpo.handoff_remarketing_roleta = roletaOrdenada;
    if (mudou.handoff_remarketing_avisa_lead) corpo.handoff_remarketing_avisa_lead = avisa;
    if (mudou.handoff_remarketing_aviso_texto) {
      // Texto igual ao padrão (ou vazio) apaga a chave: volta a valer o padrão.
      corpo.handoff_remarketing_aviso_texto =
        textoLimpo === "" || textoLimpo === avisoCfg!.padrao ? null : texto;
    }
    if (mudou.handoff_remarketing_aviso_resumo) corpo.handoff_remarketing_aviso_resumo = resumo;
    if (mudou.handoff_remarketing_respeita_humano) corpo.handoff_remarketing_respeita_humano = humano;
    if (mudou.ia_resumo_card) corpo.ia_resumo_card = iaResumo;
    if (mudou.ia_opina_indefinida) corpo.ia_opina_indefinida = iaOpina;
    if (mudou.ia_opina_corte) corpo.ia_opina_corte = Number(iaCorte);

    setSalvando(true);
    setSalvo(false);
    const novos: Partial<Record<Campo, string>> = {};
    try {
      const r = await fetch("/api/automacoes/remarketing/quando-responde", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(corpo),
      });
      const out = await r.json().catch(() => ({}));
      if (!r.ok) {
        const porCampo = (out?.erros ?? null) as Partial<Record<Campo, string>> | null;
        if (porCampo && Object.keys(porCampo).length > 0) Object.assign(novos, porCampo);
        else {
          const msg = String(out?.detail || out?.error || `HTTP ${r.status}`);
          for (const k of Object.keys(corpo) as Campo[]) novos[k] = msg;
        }
      }
    } catch (e) {
      const msg = String((e as Error).message || e);
      for (const k of Object.keys(corpo) as Campo[]) novos[k] = msg;
    }
    setErros(novos);
    setSalvando(false);
    if (Object.keys(novos).length === 0) setSalvo(true);
    onSalvou();
  }

  const daVez = roletaCfg.da_vez_nome;
  // A prévia troca {corretora} pelo nome público de quem está na vez (é o
  // nome que a pessoa vai ler). Sem ninguém na roleta, mostra o marcador.
  const nomePublico =
    roletaCfg.corretoras.find((x) => x.id === roletaCfg.da_vez)?.nome_publico ??
    roletaCfg.corretoras.find((x) => roleta.includes(x.id))?.nome_publico ??
    null;
  const textoDaPrevia = nomePublico ? texto.split(avisoCfg.placeholder).join(nomePublico) : texto;

  return (
    <div className={s.responde} data-testid="quando-responde">
      <h3 className={s.respondeTitulo}>Quando a pessoa responde</h3>

      {cfg.problema && (
        <p className={s.aviso} data-testid="responde-problema">
          Ninguém está sendo transferido agora: {cfg.problema}.
          {typeof cfg.em_respondeu === "number" && cfg.em_respondeu > 0
            ? ` ${cfg.em_respondeu} em Respondeu, com a Corretor.`
            : ""}
        </p>
      )}

      <div className={s.grade}>
        <div className={s.bloco} data-testid="bloco-quem-vai">
          <h4 className={s.blocoTitulo} id="rsp-quem-titulo">Quem vai pra corretora</h4>
          <div className={s.opcoes} role="radiogroup" aria-labelledby="rsp-quem-titulo">
            {QUEM.map((o) => (
              <button
                key={o.valor}
                type="button"
                role="radio"
                aria-checked={quem === o.valor}
                tabIndex={quem === o.valor ? 0 : -1}
                data-testid={`quem-${o.valor}`}
                className={`${s.opcao} ${quem === o.valor ? s.opcaoOn : ""}`}
                onClick={() => setQuem(o.valor)}
                onKeyDown={(e) => {
                  // grupo de rádio: as setas trocam a opção e levam o foco junto
                  if (!["ArrowDown", "ArrowRight", "ArrowUp", "ArrowLeft"].includes(e.key)) return;
                  e.preventDefault();
                  const outra = QUEM.find((x) => x.valor !== o.valor)!.valor;
                  setQuem(outra);
                  (e.currentTarget.parentElement?.querySelector(
                    `[data-testid="quem-${outra}"]`,
                  ) as HTMLElement | null)?.focus();
                }}
              >
                {o.rotulo}
                {o.valor === (cfg.quem_vai_padrao ?? "todos") ? " · padrão" : ""}
              </button>
            ))}
          </div>
          {erros.handoff_remarketing_quem_vai && (
            <p className={s.erroTexto} role="alert">{erros.handoff_remarketing_quem_vai}</p>
          )}
          <p className={s.dica}>Recusa, parar e resposta automática nunca vão.</p>
          {cfg.respeita_humano !== undefined && (
            <>
              <label className={s.marcar}>
                <input
                  type="checkbox"
                  checked={humano}
                  data-testid="respeita-humano"
                  onChange={(e) => setHumano(e.target.checked)}
                />
                <span>Não transferir quem já está em conversa pelo celular da linha</span>
              </label>
              {erros.handoff_remarketing_respeita_humano && (
                <p className={s.erroTexto} role="alert">{erros.handoff_remarketing_respeita_humano}</p>
              )}
              {typeof cfg.segurados_humano === "number" && cfg.segurados_humano > 0 && (
                <p className={s.contagem} data-testid="segurados-humano">
                  {cfg.segurados_humano === 1
                    ? "1 em Respondeu com a Corretor por isso. Dá pra transferir na mão pelo card."
                    : `${cfg.segurados_humano} em Respondeu com a Corretor por isso. Dá pra transferir na mão pelo card.`}
                </p>
              )}
            </>
          )}
        </div>

        <div className={s.bloco} data-testid="bloco-pra-onde">
          <h4 className={s.blocoTitulo}>
            <label htmlFor="rsp-etapa">Pra onde vai</label>
          </h4>
          <select
            id="rsp-etapa"
            className={s.campo}
            value={etapa}
            onChange={(e) => setEtapa(e.target.value)}
          >
            {etapa === "" && <option value="">escolha uma etapa</option>}
            {etapaCfg.opcoes.map((o) => (
              <option key={o.id} value={o.id} disabled={!o.permitida}>
                {o.nome}
                {o.padrao ? " · padrão" : ""}
                {!o.permitida ? " · não pode" : ""}
              </option>
            ))}
          </select>
          <p className={s.contagem} data-testid="tag-do-card">
            O card ganha a tag <span className={s.tagRmkt}>{cfg.tag}</span>
          </p>
          {erros.handoff_remarketing_etapa && (
            <p className={s.erroTexto} role="alert" data-testid="erro-etapa">{erros.handoff_remarketing_etapa}</p>
          )}
          <p className={s.dica}>Etapa do funil de vendas. Leva a conversa inteira junto.</p>
        </div>

        <div className={s.bloco} data-testid="bloco-roleta">
          <h4 className={s.blocoTitulo} id="rsp-roleta-titulo">Roleta do remarketing</h4>
          <div className={s.roleta} role="group" aria-labelledby="rsp-roleta-titulo">
            {roletaCfg.corretoras.map((x) => {
              const on = roleta.includes(x.id);
              return (
                <label key={x.id} className={`${s.roletaItem} ${on ? s.roletaItemOn : ""}`}>
                  <input
                    type="checkbox"
                    checked={on}
                    data-testid={`roleta-${x.id}`}
                    onChange={() => setRoleta(on ? roleta.filter((i) => i !== x.id) : [...roleta, x.id])}
                  />
                  <span className={s.roletaNome}>{x.nome}</span>
                  <span className={s.roletaConta}>
                    {x.recebeu} em {roletaCfg.periodo_dias} dias
                  </span>
                </label>
              );
            })}
          </div>
          {roletaOrdenada.length === 0 ? (
            <p className={s.aviso} data-testid="aviso-roleta-vazia">
              Roleta vazia: quem responder fica em Respondeu.
            </p>
          ) : (
            daVez &&
            !mudou.handoff_remarketing_roleta && (
              <p className={s.contagem} data-testid="roleta-da-vez">
                A vez é da <strong>{daVez}</strong>
              </p>
            )
          )}
          {erros.handoff_remarketing_roleta && (
            <p className={s.erroTexto} role="alert" data-testid="erro-roleta">{erros.handoff_remarketing_roleta}</p>
          )}
          <p className={s.dica} data-testid="roleta-do-meta">
            Roleta do Meta (lead novo): {lista(cfg.roleta_do_meta?.nomes ?? []) || "ninguém"}. Não muda aqui.
          </p>
        </div>

        <div className={`${s.bloco} ${s.blocoLargo}`} data-testid="bloco-aviso-lead">
          <h4 className={s.blocoTitulo}>Aviso ao lead</h4>
          <label className={s.marcar}>
            <input
              type="checkbox"
              checked={avisa}
              data-testid="aviso-ligado"
              onChange={(e) => setAvisa(e.target.checked)}
            />
            <span>Avisar a pessoa pela linha de quem recebeu</span>
          </label>
          {erros.handoff_remarketing_avisa_lead && (
            <p className={s.erroTexto} role="alert">{erros.handoff_remarketing_avisa_lead}</p>
          )}
          <div className={s.avisoGrade}>
            <div className={s.avisoCampo}>
              <label className={s.rotulo} htmlFor="rsp-aviso-texto">
                texto do aviso
              </label>
              <textarea
                id="rsp-aviso-texto"
                className={`${c.texto} ${erros.handoff_remarketing_aviso_texto || estourou ? c.textoComErro : ""}`}
                value={texto}
                disabled={salvando}
                aria-invalid={Boolean(erros.handoff_remarketing_aviso_texto) || estourou}
                onChange={(e) => {
                  setTexto(e.target.value);
                  setErros((x) => ({ ...x, handoff_remarketing_aviso_texto: undefined }));
                }}
              />
              <div className={c.linhaDoCampo}>
                <span>{avisoCfg.placeholder} vira o nome de quem recebeu o lead.</span>
                <span className={`${c.contador} ${estourou ? c.contadorEstourou : ""}`}>
                  {textoLimpo.length} de {avisoCfg.max}
                </span>
              </div>
              {(erros.handoff_remarketing_aviso_texto || avisoCfg.problema) && (
                <p className={s.erroTexto} role="alert" data-testid="erro-aviso-texto">
                  {erros.handoff_remarketing_aviso_texto ?? avisoCfg.problema}
                </p>
              )}
              {avisoCfg.personalizado && (
                <button type="button" className={`${s.link} ${s.linkEsquerda}`} onClick={() => setTexto(avisoCfg.padrao)}>
                  usar o texto padrão
                </button>
              )}
              <label className={s.marcar}>
                <input
                  type="checkbox"
                  checked={resumo}
                  data-testid="aviso-resumo"
                  onChange={(e) => setResumo(e.target.checked)}
                />
                <span>Lembrar o que a pessoa disse, quando der pra ler</span>
              </label>
            </div>
            <Previa texto={textoDaPrevia} rotulo={avisa ? "como chega no celular" : "como chegaria (aviso desligado)"} />
          </div>
          <p className={s.dica}>Vai dentro de um template já aprovado: editar não muda a categoria.</p>
        </div>
        {cfg.ia && (
          <div className={`${s.bloco} ${s.blocoLargo}`} data-testid="bloco-ia">
            <h4 className={s.blocoTitulo}>IA do lado</h4>
            <p className={s.dica}>A IA não fala com a pessoa e não segura o card: ele anda na hora, pela regra.</p>
            <label className={s.marcar}>
              <input
                type="checkbox"
                checked={iaResumo}
                data-testid="ia-resumo"
                onChange={(e) => setIaResumo(e.target.checked)}
              />
              <span>Resumo por IA no card</span>
            </label>
            <p className={s.dica} data-testid="ia-resumo-falha">
              O que a pessoa escreveu, em uma frase, pra corretora que recebe. {cfg.ia.falha_resumo}
            </p>
            {cfg.ia.resumo_ligado === null && (
              <p className={s.erroTexto} role="alert">O valor gravado está ilegível: vale desligado.</p>
            )}
            {erros.ia_resumo_card && <p className={s.erroTexto} role="alert">{erros.ia_resumo_card}</p>}

            <label className={s.marcar}>
              <input
                type="checkbox"
                checked={iaOpina}
                data-testid="ia-opina"
                onChange={(e) => setIaOpina(e.target.checked)}
              />
              <span>IA opina no que a regra não entendeu</span>
            </label>
            <p className={s.dica} data-testid="ia-opina-para-que">{cfg.ia.para_que_serve_opiniao}</p>
            <p className={s.dica} data-testid="ia-opina-falha">{cfg.ia.falha_opiniao}</p>
            {cfg.ia.opina_ligada === null && (
              <p className={s.erroTexto} role="alert">O valor gravado está ilegível: vale desligado.</p>
            )}
            {erros.ia_opina_indefinida && <p className={s.erroTexto} role="alert">{erros.ia_opina_indefinida}</p>}
            <label className={s.rotulo} htmlFor="ia-corte">
              certeza mínima pra mandar a recusa pra Venda perdida
            </label>
            <select
              id="ia-corte"
              className={s.campo}
              value={iaCorte}
              disabled={!iaOpina}
              data-testid="ia-corte"
              onChange={(e) => setIaCorte(e.target.value)}
            >
              {iaCorte === "" && <option value="">ilegível: escolha</option>}
              {Array.from(new Set([...CORTES, ...(gravado.iaCorte !== null ? [gravado.iaCorte] : [])]))
                .sort((a, b) => a - b)
                .map((v) => (
                  <option key={v} value={String(v)}>
                    {pct(v)}
                    {v === cfg.ia!.corte_padrao ? " · padrão (medido nas 87)" : ""}
                  </option>
                ))}
            </select>
            {(erros.ia_opina_corte || cfg.ia.corte_problema) && (
              <p className={s.erroTexto} role="alert">{erros.ia_opina_corte ?? cfg.ia.corte_problema}</p>
            )}
            {cfg.ia.ultimos_30_dias && (
              <p className={s.contagem} data-testid="ia-contagem">
                Últimos 30 dias: {cfg.ia.ultimos_30_dias.resumos_ok} resumos no card
                {cfg.ia.ultimos_30_dias.resumos_falhou > 0 ? ` (${cfg.ia.ultimos_30_dias.resumos_falhou} sem resumo)` : ""}
                {" · "}
                {cfg.ia.ultimos_30_dias.opinioes} opiniões, {cfg.ia.ultimos_30_dias.pesou} pesaram
                {cfg.ia.ultimos_30_dias.recusas_ia > 0 ? ` (${cfg.ia.ultimos_30_dias.recusas_ia} pra Venda perdida)` : ""}
              </p>
            )}
          </div>
        )}
      </div>

      <div className={s.respondeSalvar} data-testid="responde-salvar">
        <span className={s.salvarTexto} role="status">
          {salvando
            ? "salvando…"
            : quantas > 0
              ? `${quantas} ${quantas === 1 ? "mudança" : "mudanças"} sem salvar`
              : salvo
                ? "gravado"
                : "nada a salvar"}
        </span>
        {quantas > 0 && (
          <button type="button" className={s.link} disabled={salvando} onClick={voltarAoGravado}>
            descartar
          </button>
        )}
        <button
          type="button"
          className={s.btnPrimario}
          disabled={quantas === 0 || salvando || estourou}
          data-testid="responde-btn-salvar"
          onClick={() => void salvar()}
        >
          {salvando ? "salvando…" : "salvar quem responde"}
        </button>
      </div>
    </div>
  );
}
