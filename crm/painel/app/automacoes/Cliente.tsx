"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import s from "./automacoes.module.css";
import p from "./painel.module.css";
import { useConfigOperacional } from "./configOperacional";
import { Mapa, Metricas, MetricasBoasVindas, MetricasNutricao, STATUS_LABEL } from "./types";
import { Badge } from "./blocos";
import ListaAutomacoes, { type Janelas24h } from "./ListaAutomacoes";
import LinhasDoCliente from "./LinhasDoCliente";

// AUTOMAÇÕES — a porta de entrada, como painel de uma olhada (06/10/2026).
//
// Em cima, uma linha por automação: estado, linhas com a cor de cada uma, o
// número do dia e o link. Embaixo, o que vale pra TODAS: o motor (envio real,
// relógio, ritmo, quantos estão na máquina) e as travas que não são de uma
// automação só. Tudo o que é de UMA automação mora na página dela, inclusive
// o limite por dia de cada linha, que foi pra página do follow-up (é o limite
// da régua; a nutrição conta no mesmo). Dois lugares pro mesmo controle era
// alguém mexendo achando que mexia em outra coisa.

// Gatilhos que NÃO aparecem em nenhuma das 4 páginas: se sumissem daqui,
// sumiam do painel.
const GATILHOS_SO_AQUI = ["auto_followup_24h", "trava_so_corretor"];

function ler<T>(url: string): Promise<T | null> {
  return fetch(url, { cache: "no-store" })
    .then((r) => (r.ok ? (r.json() as Promise<T>) : null))
    .catch(() => null);
}

export default function AutomacoesPageCliente() {
  const [mapa, setMapa] = useState<Mapa | null>(null);
  const [erro, setErro] = useState<string | null>(null);
  const [janelas, setJanelas] = useState<Janelas24h>({ boasVindas: null, followUp: null, nutricao: null });
  const config = useConfigOperacional();

  const carregar = useCallback(() => {
    fetch("/api/automacoes", { cache: "no-store" })
      .then(async (r) => {
        const body = await r.json();
        if (!r.ok) throw new Error(body?.error || body?.detail || `HTTP ${r.status}`);
        setMapa(body);
      })
      .catch((e) => setErro(String(e.message || e)));
    // Número que não veio fica "—": a lista não inventa zero.
    void Promise.all([
      ler<MetricasBoasVindas>("/api/automacoes/boas-vindas/metricas?dias=1"),
      ler<Metricas>("/api/automacoes/metricas?dias=1"),
      ler<MetricasNutricao>("/api/automacoes/nutricao/metricas?dias=1"),
    ]).then(([boasVindas, followUp, nutricao]) => setJanelas({ boasVindas, followUp, nutricao }));
  }, []);

  useEffect(() => {
    carregar();
  }, [carregar]);

  if (erro) {
    return (
      <div className="estado">
        <h2>Automações indisponíveis</h2>
        <p>Não consegui falar com o copiloto. Tenta de novo em instantes.</p>
        <div className="estado-meta">{erro}</div>
      </div>
    );
  }
  if (!mapa) {
    return (
      <div className="estado">
        <h2>Carregando as automações…</h2>
      </div>
    );
  }

  const op = mapa.operacao;
  const mat = op.matriculados;
  const gerais = mapa.gatilhos.filter((g) => GATILHOS_SO_AQUI.includes(g.id));

  return (
    <div className={s.wrap}>
      <h1 className={p.pagina}>Automações</h1>

      <ListaAutomacoes mapa={mapa} cfg={config.dados} janelas={janelas} />

      <LinhasDoCliente />

      <h2 className={p.titulo}>Motor</h2>
      <dl className={p.motor} data-testid="motor">
        <div>
          <dt>envio real</dt>
          <dd className={`${p.motorValor} ${op.dry_run ? p.motorRuim : ""}`}>{op.dry_run ? "simulação" : "ligado"}</dd>
          <dd className={p.motorSub}>{op.dry_run ? "nada chega no WhatsApp" : "as mensagens saem de verdade"}</dd>
        </div>
        <div>
          <dt>relógio</dt>
          <dd className={`${p.motorValor} ${op.timer.enabled === false ? p.motorRuim : ""}`}>
            {op.timer.enabled === false ? "desligado" : op.timer.enabled === null ? "sem leitura" : "ligado"}
          </dd>
          <dd className={p.motorSub}>{op.timer.janela.replace(" (São Paulo)", "")}</dd>
        </div>
        <div>
          <dt>ritmo</dt>
          <dd className={p.motorValor}>{op.min_intervalo_s} s</dd>
          <dd className={p.motorSub}>entre dois envios da mesma linha</dd>
        </div>
        <div>
          <dt>na máquina</dt>
          <dd className={p.motorValor}>{mat.total.toLocaleString("pt-BR")}</dd>
          <dd className={p.motorSub}>
            {Object.entries(mat.por_status)
              .map(([k, v]) => `${v} ${STATUS_LABEL[k] ?? k}`)
              .join(" · ") || "ninguém"}
          </dd>
        </div>
      </dl>
      <p className={p.itemSub} style={{ margin: "-20px 0 32px" }}>
        O limite por dia de cada linha está no <Link href="/automacoes/follow-up#ajustar">follow-up</Link>.
      </p>

      {gerais.length > 0 && (
        <section className={s.secao}>
          <h2 className={p.titulo}>Travas gerais</h2>
          {gerais.map((g) => (
            <div key={g.id} className={s.gatilho}>
              <div className={s.gatilhoTopo}>
                <div>
                  <div className={s.gatilhoQuando}>
                    <strong>quando</strong>
                    {g.quando}
                  </div>
                  <div className={s.gatilhoEntao}>
                    <strong>então</strong>
                    {g.entao}
                  </div>
                </div>
                <Badge estado={g.estado} />
              </div>
            </div>
          ))}
        </section>
      )}
    </div>
  );
}
