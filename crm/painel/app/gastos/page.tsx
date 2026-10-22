// 💸 Gastos — quanto a operação da Acme custa, e de onde sai cada número.
//
// Tela de GESTÃO (admin), como /automacoes: corretora que abre por URL volta
// pro CRM. Sem dado é "sem registro" com o motivo, nunca "0" (regra do DEX).
import { redirect } from "next/navigation";
import { headers } from "next/headers";
import { resolveViewer } from "@/lib/config";
import { gastos, type Cotacao, type Mes } from "@/lib/gastos";
import s from "./gastos.module.css";

export const dynamic = "force-dynamic";

const usd = (n: number) => {
  const d = n > 0 && n < 0.01 ? 4 : 2;
  return `US$ ${n.toLocaleString("pt-BR", { minimumFractionDigits: d, maximumFractionDigits: d })}`;
};
const brl = (n: number) => n.toLocaleString("pt-BR", { style: "currency", currency: "BRL" });
const num = (n: number) => n.toLocaleString("pt-BR");
const tok = (n: number) => (n >= 1_000_000 ? `${(n / 1_000_000).toLocaleString("pt-BR", { maximumFractionDigits: 1 })} mi`
  : n >= 1000 ? `${(n / 1000).toLocaleString("pt-BR", { maximumFractionDigits: 1 })} mil` : num(n));

function Dinheiro({ u, c }: { u: number; c: Cotacao }) {
  return (
    <span className={s.dinheiro}>
      <b>{c ? brl(u * c.valor) : usd(u)}</b>
      {c && <span className={s.usd}>{usd(u)}</span>}
    </span>
  );
}

function Valor({ u, motivo, c }: { u: number | null; motivo: string | null; c: Cotacao }) {
  return u === null
    ? <span className={s.semRegistro}><b>Sem registro.</b> {motivo}</span>
    : <Dinheiro u={u} c={c} />;
}

// Comparação com o mês anterior. O corrente é parcial: a conta diz isso.
function Comparacao({ atual, anterior }: { atual: Mes; anterior: Mes }) {
  if (atual.totalUsd === null || anterior.totalUsd === null) {
    return <span className={s.nota}>Sem comparação: um dos meses não tem nenhum valor registrado.</span>;
  }
  const dif = atual.totalUsd - anterior.totalUsd;
  const pct = anterior.totalUsd > 0 ? (dif / anterior.totalUsd) * 100 : null;
  return (
    <span className={s.nota}>
      {dif === 0 ? "Igual ao" : dif > 0 ? `${usd(dif)} a mais que o` : `${usd(-dif)} a menos que o`} mês anterior inteiro
      {pct !== null && dif !== 0 ? ` (${pct > 0 ? "+" : ""}${pct.toLocaleString("pt-BR", { maximumFractionDigits: 0 })}%)` : ""}.
      {" "}O mês corrente ainda está em andamento.
    </span>
  );
}

function CartaoMes({ m, c, destaque }: { m: Mes; c: Cotacao; destaque?: boolean }) {
  return (
    <div className={destaque ? s.cardDestaque : s.card}>
      <div className={s.cardRot}>{m.rotulo}</div>
      <Valor u={m.totalUsd} motivo={m.totalMotivo} c={c} />
      {m.totalUsd !== null && m.totalMotivo && <div className={s.cardNota}>{m.totalMotivo}</div>}
      <dl className={s.partes}>
        <div><dt>Templates</dt><dd><Valor u={m.templatesUsd} motivo={m.templatesMotivo} c={c} /></dd></div>
        <div><dt>IA do copiloto</dt><dd><Valor u={m.iaUsd} motivo={m.iaMotivo} c={c} /></dd></div>
      </dl>
    </div>
  );
}

function BlocoTemplates({ m, c }: { m: Mes; c: Cotacao }) {
  return (
    <div className={s.mes}>
      <h3 className={s.mesTitulo}>{m.rotulo}</h3>
      {m.templates.length === 0 ? (
        <div className={s.semRegistro}><b>Sem registro.</b> Nenhum template enviado neste mês.</div>
      ) : (
        <div className={s.tabelaWrap}>
          <table className={s.tabela}>
            <thead><tr><th>Template · categoria</th><th className={s.n}>Enviados</th><th className={s.n}>Custo</th></tr></thead>
            <tbody>
              {m.templates.map((t) => (
                <tr key={`${t.template}${t.categoria}`}>
                  <td className={s.tpl}>{t.template}<br /><span className={t.categoria === "MARKETING" ? s.tagMkt : s.tag}>{t.categoria}</span></td>
                  <td className={s.n}>{num(t.enviados)}</td>
                  <td className={s.n}>{t.custoUsd === null ? <span className={s.semValor}>sem registro: {t.motivo}</span> : <Dinheiro u={t.custoUsd} c={c} />}</td>
                </tr>
              ))}
            </tbody>
            <tfoot>
              <tr><td>Total do mês</td><td className={s.n}>{num(m.enviados)}</td>
                <td className={s.n}><Valor u={m.templatesUsd} motivo={m.templatesMotivo} c={c} /></td></tr>
            </tfoot>
          </table>
        </div>
      )}
      {m.naoSaiu > 0 && (
        <p className={s.fonte}>{num(m.naoSaiu)} tentativas de envio não saíram (segurados pela trava, erro ou teste). A Meta não cobra o que não saiu.</p>
      )}
    </div>
  );
}

function BlocoIa({ m }: { m: Mes }) {
  return (
    <div className={s.mes}>
      <h3 className={s.mesTitulo}>{m.rotulo}</h3>
      {m.servicos.length === 0 ? (
        <div className={s.semRegistro}><b>Sem registro.</b> Nenhuma chamada de IA neste mês.</div>
      ) : (
        <div className={s.tabelaWrap}>
          <table className={s.tabela}>
            <thead><tr><th>Serviço</th><th className={s.n}>Chamadas</th><th className={s.n}>Entrada</th><th className={s.n}>Saída</th><th className={s.n}>Erros</th><th className={s.n}>Custo</th></tr></thead>
            <tbody>
              {m.servicos.map((x) => (
                <tr key={x.servico}>
                  <td className={s.tpl}>{x.servico}</td>
                  <td className={s.n}>{num(x.chamadas)}</td>
                  <td className={s.n}>{tok(x.entrada)}</td>
                  <td className={s.n}>{tok(x.saida)}</td>
                  <td className={s.n}>{num(x.erros)}</td>
                  <td className={s.n}>{x.custoUsd === null ? <span className={s.semValor}>sem registro</span> : usd(x.custoUsd)}</td>
                </tr>
              ))}
            </tbody>
            <tfoot>
              <tr><td>Total</td><td className={s.n}>{num(m.chamadas)}</td><td className={s.n}>{tok(m.entrada)}</td>
                <td className={s.n}>{tok(m.saida)}</td><td className={s.n}></td><td className={s.n}></td></tr>
            </tfoot>
          </table>
        </div>
      )}
    </div>
  );
}

export default async function Gastos() {
  const viewer = resolveViewer(headers().get("x-auth-user"));
  if (!viewer.isAdmin) redirect("/crm");

  let g: Awaited<ReturnType<typeof gastos>> | null = null;
  let erro = "";
  try { g = await gastos(); } catch (e) { erro = e instanceof Error ? e.message : "erro"; }
  if (!g) {
    return (
      <div className={s.wrap}>
        <h1 className={s.titulo}>Gastos</h1>
        <div className={s.semRegistro}><b>Sem registro.</b> Não consegui ler o banco do copiloto agora ({erro}). Nada foi gravado nem estimado.</div>
      </div>
    );
  }
  const c = g.cotacao;
  return (
    <div className={s.wrap}>
      <header className={s.topo}>
        <h1 className={s.titulo}>Gastos</h1>
        <div className={s.sub}>Mês atual e anterior · lido em {g.agora}</div>
      </header>

      <div className={s.cotacao}>
        {c
          ? <>Valores em R$ a <b>US$ 1 = {c.valor.toLocaleString("pt-BR", { minimumFractionDigits: 4 })}</b> ({c.fonte}). O dólar, que é como a Meta cobra, fica embaixo.</>
          : <>Sem cotação do dólar agora (o Banco Central não respondeu): valores só em US$.</>}
      </div>

      <section className={s.resumo} aria-label="Total do mês">
        <CartaoMes m={g.atual} c={c} destaque />
        <CartaoMes m={g.anterior} c={c} />
      </section>
      <p className={s.fonte}><Comparacao atual={g.atual} anterior={g.anterior} /></p>

      <section className={s.bloco}>
        <h2 className={s.blocoTitulo}>1 · Custo por template enviado</h2>
        <p className={s.fonte}>
          A Meta cobra por mensagem de template, pela categoria. Conta: envios com <code>sent = true</code> (sem teste) × preço da categoria na data do envio.
          Fonte: <code>cadencia_eventos</code> do copiloto. Mensagem dentro da janela de 24h, texto livre, não é template e não é cobrada.
        </p>
        <BlocoTemplates m={g.atual} c={c} />
        <BlocoTemplates m={g.anterior} c={c} />
        <p className={s.fonte}>
          Preços em uso (BR, por mensagem):{" "}
          {g.precos.map((p) => `${p.categoria} US$ ${p.usd.toLocaleString("pt-BR", { minimumFractionDigits: 4 })} desde ${p.desde.split("-").reverse().join("/")}`).join(" · ")}.
          Conferir na tabela da Meta antes de tratar como definitivo.
        </p>
      </section>

      <section className={s.bloco}>
        <h2 className={s.blocoTitulo}>2 · IA do copiloto, por serviço</h2>
        <p className={s.fonte}>
          Fonte: <code>llm_chamadas</code> (uma linha por chamada).
          {g.temCustoIa
            ? " O custo é a soma de custo_usd."
            : " A tabela ainda não tem custo (coluna custo_usd): hoje o copiloto roda na assinatura do Claude e não gera cobrança por token. O que dá pra medir é o volume em tokens."}
        </p>
        <BlocoIa m={g.atual} />
        <BlocoIa m={g.anterior} />
      </section>
    </div>
  );
}
