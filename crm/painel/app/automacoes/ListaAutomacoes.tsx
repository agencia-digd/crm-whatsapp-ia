"use client";

// A LISTA de automações como painel de uma olhada (06/10/2026).
//
// Uma linha por automação, sem texto corrido: o estado (agindo, ligada,
// parada e por quê, desligada), as linhas por onde sai com a cor de cada uma,
// o número do dia e o link pra página dela. O estado sai das MESMAS funções
// que a página de cada automação usa (`estados.ts` e, no remarketing, a de
// `Disparo.tsx`): a lista dizer uma coisa e a página outra seria o pior erro.
//
// "Do dia" e "por qual linha" vêm do `resumo[]` do servidor (06/10): o número é
// o do dia de São Paulo, com o rótulo que ele manda ("enviadas hoje", "marcos
// hoje"), e as linhas são as que ele considera (no follow-up, só as das
// corretoras ligadas). Sem `resumo` (servidor antigo), a janela rolante de 24 h.

import Link from "next/link";
import type { PayloadConfig } from "./configOperacional";
import { estadoBoasVindas, estadoFollowUp, estadoNutricao, nomeDaLinha, resumoDe } from "./estados";
import { Linhas, lerChave, linhaDoServidor, linhaPorSlug, mil, type LinhaDaCorretora, type Situacao } from "./painel";
import { situacao as situacaoRemarketing } from "./remarketing/Disparo";
import type { Mapa, Metricas, MetricasBoasVindas, MetricasNutricao } from "./types";
import p from "./painel.module.css";

export type Janelas24h = {
  boasVindas: MetricasBoasVindas | null;
  followUp: Metricas | null;
  nutricao: MetricasNutricao | null;
};

type Item = {
  id: string;
  nome: string;
  href: string;
  sit: Situacao;
  linhas: LinhaDaCorretora[];
  linhasTexto?: string;
  numero: string;
  numeroSub: string;
};

const CLASSE_DO_TOM: Record<string, string> = { saindo: p.itemSaindo, parado: p.itemParado };

export default function ListaAutomacoes({
  mapa,
  cfg,
  janelas,
}: {
  mapa: Mapa;
  cfg: PayloadConfig | null;
  janelas: Janelas24h;
}) {
  const bv = estadoBoasVindas(mapa, cfg);
  const fu = estadoFollowUp(mapa, cfg);
  const fila = janelas.nutricao?.fila;
  const nu = estadoNutricao(mapa, cfg, fila?.disponivel ? fila.total : null);
  const semDado = "—";

  // O que o servidor decidiu pra cada uma: linhas com a cor e o número do dia.
  const doServidor = (id: "boas-vindas" | "follow-up" | "remarketing" | "nutricao") => {
    const r = resumoDe(mapa, id);
    if (!r) return null;
    return {
      linhas: r.linhas.map((l) => linhaDoServidor(nomeDaLinha(mapa, l.slug), l)),
      numero: r.numero_do_dia.valor === null ? semDado : mil(r.numero_do_dia.valor),
      numeroSub: r.numero_do_dia.rotulo,
    };
  };
  const rBv = doServidor("boas-vindas");
  const rFu = doServidor("follow-up");
  const rRm = doServidor("remarketing");
  const rNu = doServidor("nutricao");

  const itens: Item[] = [
    {
      id: "boas-vindas",
      nome: "Boas-vindas",
      href: "/automacoes/boas-vindas",
      sit: bv.sit,
      linhas: rBv?.linhas ?? bv.linhas,
      linhasTexto: "linha da dona, pela roleta",
      numero: rBv?.numero ?? (janelas.boasVindas ? mil(janelas.boasVindas.total.enviadas) : semDado),
      numeroSub: rBv?.numeroSub ?? "saíram em 24 h",
    },
    {
      id: "follow-up",
      nome: "Follow-up",
      href: "/automacoes/follow-up",
      sit: fu.sit,
      linhas: rFu?.linhas ?? fu.linhas,
      linhasTexto: `${mil(fu.naRegua)} na régua · linha da dona`,
      numero: rFu?.numero ?? (janelas.followUp ? mil(janelas.followUp.total.enviados) : semDado),
      numeroSub: rFu?.numeroSub ?? "saíram em 24 h",
    },
  ];

  const car = mapa.carrossel_corretor;
  if (car) {
    const chave = cfg?.config.carrossel_corretor_ativo;
    const ligado = car.estado === "indisponivel" ? null : cfg ? lerChave(chave) : car.estado === "ligado";
    const sit = situacaoRemarketing(car, ligado);
    // Do remarketing só o número do DISPARO interessa (as outras são de atendimento).
    const disp = car.linha_disparadora?.numeros.find((n) => n.papel === "disparo") ?? null;
    const casa = disp ?? (car.saude_linha?.slug ? linhaPorSlug(car.saude_linha.slug, car.linha_disparadora?.numeros) : null);
    itens.push({
      id: "remarketing",
      nome: "Remarketing da base",
      href: "/automacoes/remarketing",
      // "Desligado/Ligado" do remarketing já vem no masculino (o disparo).
      sit: sit as Situacao,
      linhas: casa
        ? [{ nome: casa.nome ?? casa.slug ?? "linha da casa", telefone: casa.telefone, saude: casa.saude ?? { cor: "desconhecida" }, temLinha: true }]
        : [],
      linhasTexto: disp ? "disparador" : "nenhum número disparador",
      numero: rRm?.numero ?? (car.ritmo.disparos_hoje === null ? semDado : mil(car.ritmo.disparos_hoje)),
      numeroSub: car.ritmo.por_dia > 0 ? `de ${car.ritmo.por_dia} hoje` : "hoje · limite zero",
    });
  }

  itens.push({
    id: "nutricao",
    nome: "Nutrição",
    href: "/automacoes/nutricao",
    sit: nu.sit,
    linhas: rNu?.linhas ?? nu.linhasDeSaida,
    linhasTexto: fila?.disponivel
      ? `${mil(fila.total ?? 0)} na nutrição · ${nu.todaPelaGi ? `sai toda pela ${nu.linha.nome}` : "linha da dona"}`
      : "fila sem leitura",
    numero: rNu?.numero ?? (janelas.nutricao ? mil(janelas.nutricao.total.enviados) : semDado),
    numeroSub: rNu?.numeroSub ?? "saíram em 24 h",
  });

  return (
    <ul className={p.lista} data-testid="lista-automacoes">
      {itens.map((it) => (
        <li key={it.id}>
          <Link href={it.href} className={`${p.item} ${CLASSE_DO_TOM[it.sit.tom] ?? ""}`} data-testid={`item-${it.id}`} data-tom={it.sit.tom}>
            <div>
              <div className={p.itemNome}>{it.nome}</div>
              <div className={`${p.itemEstado} ${p[`cor_${it.sit.tom}`]}`}>
                <span className={`${p.itemPonto} ${p[`ponto_${it.sit.tom}`] ?? ""}`} aria-hidden />
                {it.sit.titulo}
              </div>
              <div className={p.itemMotivo}>{it.sit.frase}</div>
            </div>
            <div>
              <div className={p.itemRotulo}>por qual linha</div>
              <Linhas linhas={it.linhas} />
              {it.linhasTexto && <div className={p.itemSub}>{it.linhasTexto}</div>}
            </div>
            <div>
              <div className={p.itemRotulo}>do dia</div>
              <div className={p.itemNumero}>{it.numero}</div>
              <div className={p.itemSub}>{it.numeroSub}</div>
            </div>
            <span className={p.itemSeta}>abrir →</span>
          </Link>
        </li>
      ))}
    </ul>
  );
}
