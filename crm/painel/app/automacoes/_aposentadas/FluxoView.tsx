"use client";

// APOSENTADO em 05/08/2026 — o desenho GERAL do fluxo saiu da home de
// /automacoes: cada automação passou a desenhar o seu (mesmo Canvas.tsx,
// com mais detalhe e com as métricas de cada toque). Mantido aqui porque
// nada se apaga; `Canvas.tsx`, `blocos.tsx` e `fluxo.module.css` seguem
// VIVOS e usados pelas 4 páginas + /tarefas.

// 🗺 Vista FLUXO — desenho da máquina estilo editor de salesbot do Kommo:
// caixas brancas numeradas conectadas por linhas (SVG próprio, zero lib),
// balões azuis com a copy REAL do payload, ramificações de condição,
// zoom + scroll (canvas maior que a tela). Somente leitura, como o resto.
//
// Como as linhas são desenhadas: cada nó se registra num Map por id via
// ref callback; depois do layout a gente mede as caixas (getBoundingClientRect
// ÷ zoom pra voltar ao espaço de layout, já que o canvas usa transform:scale)
// e gera paths bezier num <svg> absoluto dentro do próprio canvas.

import { useMemo } from "react";
import s from "../fluxo.module.css";
import { Mapa, Toque, Gatilho, STATUS_LABEL } from "../types";
import { Badge, Balao, Espera, Node } from "../blocos";
import Canvas, { Edge, Tone, corrente } from "../Canvas";

/** Preview completo = o que o lead vê (casca + corpo), não só o {{2}}. */
function ToqueBody({ t, dryRun }: { t: Toque; dryRun: boolean }) {
  if (t.tipo === "tarefa_ligacao") {
    return (
      <>
        <div className={s.acaoTexto}>
          {t.acao || "🔔 vira tarefa pra corretora ligar — nenhuma mensagem é enviada."}
        </div>
        <div className={s.tags}>
          <span className={s.tag}>tarefa no CRM</span>
        </div>
      </>
    );
  }
  const texto = t.preview || t.copy;
  return (
    <>
      {texto ? <Balao>{texto}</Balao> : null}
      <div className={s.tags}>
        <span className={s.tag}>{t.categoria}</span>
        <span className={s.tag}>{t.com_botao ? "com botão" : "sem botão"}</span>
        {t.template && <span className={s.tag}>{t.template}</span>}
        {dryRun && <Badge estado="dry-run" />}
      </div>
    </>
  );
}

const dias = (n: number) => (n === 1 ? "espera 1 dia" : `espera ${n} dias`);

export default function FluxoView({ mapa }: { mapa: Mapa }) {
  const op = mapa.operacao;
  const dryRun = op.dry_run;
  const bv = mapa.boas_vindas;
  const bvLigada = bv.estado === "ligado";

  const g = useMemo(() => {
    const by: Record<string, Gatilho | undefined> = {};
    for (const x of mapa.gatilhos) by[x.id] = x;
    return by;
  }, [mapa]);

  const enroll = g["auto_enroll_followup"];
  const resposta = g["resposta_lead"];
  const optOut = g["opt_out"];
  const esgotou = g["esgotou_cadencia"];
  // fluxos com estado "desligado" viram um fluxo esmaecido próprio
  const desligados = useMemo(
    () => mapa.gatilhos.filter((x) => x.estado === "desligado"),
    [mapa]
  );

  // ── ids da corrente da cadência (nó/pausa/nó/pausa…) ──
  const toques = mapa.cadencia.toques;
  const cadChain = useMemo(() => {
    const ids: string[] = ["cad-g"];
    let prev = 0;
    for (const t of toques) {
      const off = t.offset_dias ?? 0;
      if (off - prev > 0) ids.push(`cad-w${t.indice}`);
      ids.push(`cad-t${t.indice}`);
      prev = off;
    }
    ids.push("cad-fim");
    return ids;
  }, [toques]);

  const marcos = mapa.nutricao.marcos;
  const nutChain = useMemo(() => {
    const ids: string[] = ["nut-g"];
    marcos.forEach((m, i) => {
      if (i > 0) ids.push(`nut-w${i}`);
      ids.push(`nut-m${i}`);
    });
    ids.push("nut-fim");
    return ids;
  }, [marcos]);

  const edges = useMemo(() => {
    const es: Edge[] = [];
    const chain = (ids: string[], tone: Tone = "base") => {
      es.push(...corrente(ids, tone));
    };
    // boas-vindas: fluxo curto e independente (gatilho → 1 msg → fim)
    chain(["bv-g", "bv-msg", "bv-fim"], bvLigada ? "base" : "muted");
    // saída que NÃO entra no A/B: quem foi adotado por ligação recebe a copy
    // de continuidade. Sem este ramo a tela dizia que todo envio é do teste.
    if (bv.continuidade) {
      es.push({ from: "bv-g", to: "bv-cont", kind: "branch", tone: "warn", dashed: true });
      es.push({ from: "bv-cont", to: "bv-fim", kind: "h", tone: "warn" });
    }
    chain(cadChain);
    chain(nutChain);
    // ramificações da cadência (condições valem a qualquer momento)
    const anchorResp = toques.length > 0 ? `cad-t${toques[0].indice}` : "cad-g";
    const anchorOpt = toques.length > 2 ? `cad-t${toques[2].indice}` : anchorResp;
    es.push({ from: anchorResp, to: "cad-cond-resp", kind: "branch", tone: "warn", dashed: true });
    es.push({ from: "cad-cond-resp", to: "cad-res-resp", kind: "h", tone: "warn" });
    es.push({ from: anchorOpt, to: "cad-cond-opt", kind: "branch", tone: "warn", dashed: true });
    es.push({ from: "cad-cond-opt", to: "cad-res-opt", kind: "h", tone: "warn" });
    // fluxos desligados
    desligados.forEach((_, i) => {
      chain([`off${i}-g`, `off${i}-a`, `off${i}-b`], "muted");
    });
    return es;
  }, [cadChain, nutChain, toques, desligados, bvLigada, bv.continuidade]);

  const mat = op.matriculados;
  let nCad = 0;
  let nNut = 0;

  return (
    <div>
      {/* ── estado vivo do motor (badges) ── */}
      <div className={s.toolbar}>
        <span className={s.chip}>
          <span className={s.chipLabel}>envio</span>
          {dryRun ? <Badge estado="dry-run" /> : <Badge estado="ligado" />}
        </span>
        {/* rótulos em português de gente: "motor"/"relógio" não diziam o que
            eram. Os dois precisam estar ligados — relógio desligado com motor
            ligado não manda nada, e não avisa. */}
        <span className={s.chip}>
          <span className={s.chipLabel}>automações</span>
          <Badge estado={op.cadencia_ativa ? "ligado" : "desligado"} />
        </span>
        <span className={s.chip} title="Quem acorda a máquina. Desligado aqui, nada sai mesmo com as automações ligadas.">
          <span className={s.chipLabel}>horário</span>
          <Badge estado={op.timer.enabled === false ? "desligado" : "ligado"} />
          <span className={s.chipInfo}>{op.timer.janela}</span>
        </span>
        <span className={s.chip}>
          <span className={s.chipLabel}>cap</span>
          <span className={s.chipInfo}>
            {op.cap_diario} msgs/dia · mín. {op.min_intervalo_s}s
          </span>
        </span>
        <span className={s.chip}>
          <span className={s.chipLabel}>na máquina</span>
          <span className={s.chipInfo}>
            {mat.total}
            {" — "}
            {Object.entries(mat.por_status)
              .map(([k, v]) => `${v} ${STATUS_LABEL[k] ?? k}`)
              .join(" · ") || "ninguém ainda"}
          </span>
        </span>
      </div>

      <Canvas edges={edges}>
        {(reg) => (
          <>
              {/* ═══ FLUXO 0 · BOAS-VINDAS (independente da cadência) ═══ */}
              <div className={s.flow}>
                <div className={s.flowLabel}>
                  0 · BOAS-VINDAS — CADASTRO NOVO (FLUXO INDEPENDENTE)
                </div>
                <div className={s.row}>
                  <Node
                    reg={reg}
                    id="bv-g"
                    num={1}
                    icon="⚡"
                    titulo="Gatilho"
                    badge={<Badge estado={bvLigada ? "ligado" : "desligado"} />}
                    muted={!bvLigada}
                  >
                    <div>{bv.gatilho}</div>
                    {/* o motor também pega o lead JÁ EXISTENTE que está em
                        'Novo' e ainda não recebeu — não é só cadastro novo */}
                    {bv.caminhos && (
                      <ul className={s.condNota}>
                        {bv.caminhos.map((c) => (
                          <li key={c.id}>
                            <strong>{c.titulo}:</strong> {c.detalhe}
                            {!c.no_ab && " · fora do teste A/B"}
                          </li>
                        ))}
                      </ul>
                    )}
                    <div className={s.tags}>
                      <span className={s.tag}>{bv.flag}</span>
                      {!bv.disponivel && <span className={s.tag}>não deployada</span>}
                    </div>
                  </Node>
                  <Node
                    reg={reg}
                    id="bv-msg"
                    num={2}
                    icon="💬"
                    titulo="Mensagem de boas-vindas"
                    muted={!bvLigada}
                  >
                    {bv.preview || bv.copy ? (
                      <Balao>{bv.preview || bv.copy}</Balao>
                    ) : (
                      <div className={s.acaoTexto}>{bv.detalhe}</div>
                    )}
                    <div className={s.tags}>
                      <span className={s.tag}>1x por telefone</span>
                      <span className={s.tag}>linha da dona · WABA</span>
                      <span className={s.tag}>{bv.com_botao ? "com botão" : "sem botão"}</span>
                      {bv.template && <span className={s.tag}>{bv.template}</span>}
                      {dryRun && <Badge estado="dry-run" />}
                    </div>
                  </Node>
                  {bv.continuidade && (
                    <Node
                      reg={reg}
                      id="bv-cont"
                      num={3}
                      icon="📞"
                      titulo="Veio de ligação — copy de continuidade"
                      cond
                      muted={!bvLigada}
                    >
                      <Balao>
                        {bv.continuidade.preview || bv.continuidade.texto}
                      </Balao>
                      <div className={s.condNota}>
                        {bv.continuidade.quando}. <strong>Fora do teste A/B</strong>{" "}
                        — {bv.continuidade.por_que}.
                      </div>
                    </Node>
                  )}
                  <Node
                    reg={reg}
                    id="bv-fim"
                    num={bv.continuidade ? 4 : 3}
                    icon="🏁"
                    titulo="Fim — aguarda a corretora"
                    muted={!bvLigada}
                  >
                    <div>{bv.fim}</div>
                    <div className={s.condNota}>
                      sem cadência atrás. Se a corretora arrastar o lead pro Follow-up, aí sim
                      começa o fluxo 1 (D0 = reencontro) — são DUAS automações separadas.
                    </div>
                  </Node>
                </div>
              </div>

              {/* ═══ FLUXO 1 · CADÊNCIA ═══ */}
              <div className={s.flow}>
                <div className={s.flowLabel}>
                  1 · CADÊNCIA — FOLLOW-UP, 8 TOQUES EM 10 DIAS
                </div>
                <div className={s.row}>
                  <Node
                    reg={reg}
                    id="cad-g"
                    num={++nCad}
                    icon="⚡"
                    titulo="Gatilho"
                    badge={<Badge estado={enroll?.estado ?? "ligado"} />}
                  >
                    <div>{enroll?.quando ?? "lead entra na etapa Follow-up"}</div>
                    <div className={s.tags}>
                      {enroll?.flag && <span className={s.tag}>{enroll.flag}</span>}
                    </div>
                  </Node>

                  {toques.map((t, i) => {
                    const prev = i > 0 ? toques[i - 1].offset_dias ?? 0 : 0;
                    const gap = (t.offset_dias ?? 0) - prev;
                    return (
                      <div key={t.indice} className={s.seq}>
                        {gap > 0 && (
                          <Espera reg={reg} id={`cad-w${t.indice}`} texto={dias(gap)} />
                        )}
                        <Node
                          reg={reg}
                          id={`cad-t${t.indice}`}
                          num={++nCad}
                          icon={t.tipo === "mensagem" ? "💬" : "📞"}
                          titulo={
                            t.tipo === "mensagem"
                              ? `Mensagem · ${t.dia}`
                              : `Tarefa de ligação · ${t.dia}`
                          }
                        >
                          <ToqueBody t={t} dryRun={dryRun} />
                        </Node>
                      </div>
                    );
                  })}

                  <Node reg={reg} id="cad-fim" num={++nCad} icon="🔁" titulo="Esgotou sem resposta">
                    <div>{mapa.cadencia.remarketing.regra}</div>
                    <div className={s.tags}>
                      <span className={s.tag}>
                        1 msg a cada {mapa.cadencia.remarketing.intervalo_dias} dias
                      </span>
                      <span className={s.tag}>→ fluxo 2 · nutrição</span>
                    </div>
                  </Node>
                </div>

                {/* ramificações — valem a qualquer momento da cadência */}
                <div className={s.branchRow}>
                  <Node
                    reg={reg}
                    id="cad-cond-resp"
                    icon="❓"
                    titulo="Se o lead responder"
                    badge={<Badge estado={resposta?.estado ?? "ligado"} />}
                    cond
                  >
                    <div>{resposta?.quando ?? "lead matriculado responde no WhatsApp"}</div>
                    <div className={s.condNota}>
                      timer de 5s anti-secretária-eletrônica: auto-reply colado no nosso envio
                      não conta como resposta
                    </div>
                  </Node>
                  <Node reg={reg} id="cad-res-resp" icon="🟢" titulo="Cadência PARA">
                    <div>{resposta?.entao ?? "para + card move pra Em atendimento + badge 🟢"}</div>
                    <div className={s.tags}>
                      <span className={s.tag}>→ Em atendimento</span>
                      <span className={s.tag}>badge 🟢 in-app</span>
                    </div>
                  </Node>
                  <Node
                    reg={reg}
                    id="cad-cond-opt"
                    icon="✋"
                    titulo="Se pedir pra sair (opt-out)"
                    badge={<Badge estado={optOut?.estado ?? "ligado"} />}
                    cond
                  >
                    <div>{optOut?.quando ?? "lead responde com pedido de saída"}</div>
                  </Node>
                  <Node reg={reg} id="cad-res-opt" icon="🚫" titulo="Cadência PARA">
                    <div>{optOut?.entao ?? "para + card move pra Venda perdida"}</div>
                    <div className={s.tags}>
                      <span className={s.tag}>→ Venda perdida</span>
                      <span className={s.tag}>nunca recebe mais nada</span>
                    </div>
                  </Node>
                </div>
              </div>

              {/* ═══ FLUXO 2 · NUTRIÇÃO ═══ */}
              <div className={s.flow}>
                <div className={s.flowLabel}>
                  2 · NUTRIÇÃO — MARCOS 30/60/90/120/150 DIAS
                </div>
                <div className={s.row}>
                  <Node
                    reg={reg}
                    id="nut-g"
                    num={++nNut}
                    icon="⚡"
                    titulo="Gatilho"
                    badge={<Badge estado={esgotou?.estado ?? "ligado"} />}
                  >
                    <div>{mapa.nutricao.regra_entrada}</div>
                    <div className={s.tags}>
                      {esgotou?.flag && <span className={s.tag}>{esgotou.flag}</span>}
                    </div>
                  </Node>

                  {marcos.map((m, i) => (
                    <div key={m.etapa} className={s.seq}>
                      {i > 0 && (
                        <Espera
                          reg={reg}
                          id={`nut-w${i}`}
                          texto={dias(mapa.nutricao.intervalo_dias)}
                        />
                      )}
                      <Node
                        reg={reg}
                        id={`nut-m${i}`}
                        num={++nNut}
                        icon={m.copy ? "💬" : "📥"}
                        titulo={m.copy ? `Mensagem · ${m.etapa}` : `Etapa · ${m.etapa}`}
                      >
                        {m.copy ? (
                          <Balao>{m.copy}</Balao>
                        ) : (
                          <div className={s.acaoTexto}>
                            entrada do funil — nenhum disparo ainda.
                          </div>
                        )}
                        <div className={s.tags}>
                          {m.tag && <span className={s.tag}>{m.tag}</span>}
                          <span className={s.tag}>board /remarketing</span>
                        </div>
                      </Node>
                    </div>
                  ))}

                  <Node reg={reg} id="nut-fim" num={++nNut} icon="🏁" titulo="Fim">
                    <div>
                      para de vez no marco {mapa.nutricao.etapa_final} — não recebe mais nada.
                    </div>
                  </Node>
                </div>
                <div className={s.flowNota}>copies: {mapa.nutricao.copies_status}</div>
              </div>

              {/* ═══ 3 · REMARKETING DA BASE (CARROSSEL DA CORRETOR) ═══
                  A máquina de maior volume da operação — 4.9k leads na fila.
                  Estava fora do canvas: a tela desenhava follow-up e nutrição,
                  e o remarketing, que dispara todo dia, não aparecia. */}
              {mapa.carrossel_corretor && (
                <div className={s.flow}>
                  <div className={s.flowLabel}>
                    3 · {mapa.carrossel_corretor.titulo.toUpperCase()} —{" "}
                    {mapa.carrossel_corretor.linha}
                  </div>
                  <div className={s.row}>
                    <Node
                      reg={reg}
                      id="car-g"
                      num={1}
                      icon="⚡"
                      titulo="Gatilho"
                      badge={<Badge estado={mapa.carrossel_corretor.estado} />}
                    >
                      <div>{mapa.carrossel_corretor.gatilho}</div>
                      <div className={s.tags}>
                        <span className={s.tag}>
                          {mapa.carrossel_corretor.ritmo.por_dia}/dia
                        </span>
                        <span className={s.tag}>
                          limite: {mapa.carrossel_corretor.ritmo.fonte_do_limite}
                        </span>
                      </div>
                    </Node>

                    <Espera
                      reg={reg}
                      id="car-e1"
                      texto={
                        mapa.carrossel_corretor.ritmo.disparos_hoje === null ||
                        mapa.carrossel_corretor.ritmo.disparos_hoje === undefined
                          ? "hoje: não consegui ler os disparos"
                          : `hoje: ${mapa.carrossel_corretor.ritmo.disparos_hoje} disparos`
                      }
                    />

                    <Node reg={reg} id="car-msg" num={2} icon="💬" titulo="O que sai">
                      {mapa.carrossel_corretor.variantes
                        .filter((v) => v.ativa)
                        .map((v) => (
                          <div key={v.id} className={s.tags}>
                            <span className={s.tag}>
                              {v.peso}% · {v.categoria}
                            </span>
                            <Balao>{v.texto}</Balao>
                          </div>
                        ))}
                    </Node>

                    <Espera reg={reg} id="car-e2" texto="lead responde" />

                    <Node reg={reg} id="car-pessoa" num={3} icon="🤖" titulo="Pessoa atende">
                      <div>{mapa.carrossel_corretor.quando_responde[0]?.o_que}</div>
                      <div className={s.tags}>
                        <span className={s.tag}>na própria linha 0000</span>
                      </div>
                    </Node>

                    <Espera reg={reg} id="car-e3" texto="qualificou" />

                    <Node reg={reg} id="car-corretor" num={4} icon="📤" titulo="Passa pra Pessoa12">
                      <div>{mapa.carrossel_corretor.quando_responde[2]?.o_que}</div>
                      <div className={s.tags}>
                        <span className={s.tag}>template UTILITY</span>
                        <span className={s.tag}>sem botão</span>
                        <span className={s.tag}>+ nota no painel</span>
                      </div>
                    </Node>

                    <Node reg={reg} id="car-fim" num={5} icon="🔇" titulo="Fim de linha">
                      <div>{mapa.carrossel_corretor.quando_responde[3]?.o_que}</div>
                      <div>
                        Quem não respondeu volta pra fila em{" "}
                        {mapa.carrossel_corretor.ritmo.descanso_dias} dias — o ciclo é
                        circular.
                      </div>
                    </Node>
                  </div>
                  <div className={s.flowNota}>
                    {/* falha de leitura NÃO pode virar zero na tela */}
                    {mapa.carrossel_corretor.estoque.indisponivel ? (
                      <>onde estão: não consegui ler o estoque agora · </>
                    ) : (
                      <>
                        onde estão: {mapa.carrossel_corretor.estoque.fila} na fila ·{" "}
                        {mapa.carrossel_corretor.estoque.disparado} disparados ·{" "}
                        {mapa.carrossel_corretor.estoque.respondeu} responderam ·{" "}
                      </>
                    )}
                    nunca dispara pra: {mapa.carrossel_corretor.travas.join(" · ")}
                  </div>
                </div>
              )}

              {/* ═══ FLUXOS DESLIGADOS ═══ */}
              {desligados.map((d, i) => (
                <div key={d.id} className={s.flow}>
                  <div className={`${s.flowLabel} ${s.flowLabelOff}`}>
                    {4 + i} · {d.id.replace(/_/g, " ").toUpperCase()} — DESLIGADO
                  </div>
                  <div className={s.row}>
                    <Node
                      reg={reg}
                      id={`off${i}-g`}
                      num={1}
                      icon="⚡"
                      titulo="Gatilho"
                      badge={<Badge estado="desligado" />}
                      muted
                    >
                      <div>{d.quando}</div>
                      <div className={s.tags}>
                        {d.flag && <span className={s.tag}>{d.flag}</span>}
                      </div>
                    </Node>
                    <Node reg={reg} id={`off${i}-a`} num={2} icon="↩️" titulo="Ação" muted>
                      <div>{d.entao}</div>
                    </Node>
                    <Node reg={reg} id={`off${i}-b`} num={3} icon="🔒" titulo="Por que está desligado" muted>
                      <div>{d.detalhe}</div>
                    </Node>
                  </div>
                </div>
              ))}
          </>
        )}
      </Canvas>
    </div>
  );
}
