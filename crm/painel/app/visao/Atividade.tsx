"use client";

// Atividade do time na Visão (06/10): mensagens e ligações por corretora.
// Consome /api/visao/atividade → copiloto /painel/visao/atividade, no MESMO
// período do seletor da tela. As contas moram no copiloto
// (app/services/visao_atividade.py); aqui é só apresentação.
// Molde do remarketing: um bloco por pergunta, caixa com rótulo + número + uma
// linha curta, tabela por corretora. Sem cor de alarme no texto.

type Msg = {
  escritas: number;
  pessoas: number;
  automaticas: number;
  pessoas_automaticas: number;
  recebidas: number;
  pessoas_que_falaram: number;
  automaticas_por_origem: { origem: string; rotulo: string; mensagens: number }[];
};
type Lig = {
  feitas: number;
  atendidas: number;
  nao_atendidas: number;
  quiseram_continuar: number;
  numero_incorreto_na_obs: number;
  pessoas: number;
  taxa_atendimento: number | null;
  venda_perdida: number;
  numero_errado: number;
};
export type AtividadeResp = {
  periodo?: { label?: string; inicio?: string; fim?: string };
  viewer?: { role?: string };
  total: { mensagens: Msg; ligacoes: Lig };
  por_corretora: { corretora_id: number; label: string; mensagens: Msg; ligacoes: Lig }[];
};

const n = (v: number | null | undefined) => (v == null ? "—" : v.toLocaleString("pt-BR"));
const pct = (v: number | null | undefined) =>
  v == null ? "—" : `${v.toLocaleString("pt-BR", { maximumFractionDigits: 1 })}%`;
const pessoas = (v: number) => (v === 1 ? "1 pessoa" : `${n(v)} pessoas`);

function Caixa({ rotulo, valor, linha }: { rotulo: string; valor: string; linha?: string }) {
  return (
    <div className="vg-at-caixa">
      <div className="vg-at-rotulo">{rotulo}</div>
      <div className="vg-at-valor">{valor}</div>
      {linha && <div className="vg-at-linha">{linha}</div>}
    </div>
  );
}

export function Atividade({ dados, erro }: { dados: AtividadeResp | null; erro: string | null }) {
  if (erro && !dados) {
    return (
      <div className="vg-at-bloco">
        <div className="vg-at-vazio">Não consegui ler mensagens e ligações agora ({erro}).</div>
      </div>
    );
  }
  if (!dados) return <div className="loading">Carregando mensagens e ligações…</div>;

  const m = dados.total.mensagens;
  const l = dados.total.ligacoes;
  const time = dados.por_corretora.length > 1;
  const comMsg = dados.por_corretora.filter((c) => c.mensagens.escritas || c.mensagens.automaticas);
  const comLig = dados.por_corretora.filter(
    (c) => c.ligacoes.feitas || c.ligacoes.venda_perdida || c.ligacoes.numero_errado,
  );
  const fora = [
    l.venda_perdida
      ? `${n(l.venda_perdida)} ${l.venda_perdida === 1 ? "venda perdida" : "vendas perdidas"}`
      : null,
    l.numero_errado
      ? `${n(l.numero_errado)} ${l.numero_errado === 1 ? "número errado" : "números errados"}`
      : null,
  ].filter(Boolean);

  return (
    <>
      <div className="vg-sec-titulo">Atividade do time</div>

      {/* ── Pergunta 1 ── */}
      <section className="vg-at-bloco" aria-labelledby="at-msg">
        <h3 id="at-msg" className="vg-at-pergunta">
          Quantas mensagens {time ? "elas escreveram" : "você escreveu"}, e pra quantas pessoas?
        </h3>
        <div className="vg-at-caixas">
          <Caixa rotulo="escritas por gente" valor={n(m.escritas)} linha={`pra ${pessoas(m.pessoas)}`} />
          <Caixa rotulo="automáticas" valor={n(m.automaticas)} linha={`pra ${pessoas(m.pessoas_automaticas)}`} />
          <Caixa rotulo="recebidas" valor={n(m.recebidas)} linha={`de ${pessoas(m.pessoas_que_falaram)}`} />
        </div>
        <div className="vg-at-nota">Escritas = sem template e sem robô, a mesma conta do Telegram.</div>
        {time && comMsg.length > 0 && (
          <div className="vg-at-tabwrap">
            <table className="vg-at-tab">
              <thead>
                <tr>
                  <th>corretora</th>
                  <th>escritas</th>
                  <th>pessoas</th>
                  <th>automáticas</th>
                </tr>
              </thead>
              <tbody>
                {comMsg.map((c) => (
                  <tr key={c.corretora_id}>
                    <td>{c.label}</td>
                    <td className="forte">{n(c.mensagens.escritas)}</td>
                    <td>{n(c.mensagens.pessoas)}</td>
                    <td>{n(c.mensagens.automaticas)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {/* ── Pergunta 2 ── */}
      {m.automaticas > 0 && (
        <section className="vg-at-bloco" aria-labelledby="at-auto">
          <h3 id="at-auto" className="vg-at-pergunta">De onde vieram as automáticas?</h3>
          <div className="vg-at-lista">
            {m.automaticas_por_origem.map((o) => (
              <div key={o.origem} className="vg-at-item">
                <span>{o.rotulo}</span>
                <b>{n(o.mensagens)}</b>
              </div>
            ))}
          </div>
        </section>
      )}

      {/* ── Pergunta 3 ── */}
      <section className="vg-at-bloco" aria-labelledby="at-lig">
        <h3 id="at-lig" className="vg-at-pergunta">
          Quantas ligações {time ? "cada uma fez" : "você fez"}, e quantas atenderam?
        </h3>
        <div className="vg-at-caixas">
          <Caixa rotulo="ligações feitas" valor={n(l.feitas)} linha={`pra ${pessoas(l.pessoas)}`} />
          <Caixa rotulo="atenderam" valor={n(l.atendidas)} linha={`${pct(l.taxa_atendimento)} das ligações`} />
          <Caixa rotulo="não atenderam" valor={n(l.nao_atendidas)} linha="voltam pra fila em 24 h" />
          <Caixa rotulo="quiseram continuar" valor={n(l.quiseram_continuar)} linha="das que atenderam" />
        </div>
        {fora.length > 0 && (
          <div className="vg-at-nota">Fora da conta: {fora.join(" · ")} (marcados direto na ligação).</div>
        )}
        {time && comLig.length > 0 && (
          <div className="vg-at-tabwrap">
            <table className="vg-at-tab">
              <thead>
                <tr>
                  <th>corretora</th>
                  <th>feitas</th>
                  <th>atend.</th>
                  <th>não atend.</th>
                  <th>pessoas</th>
                </tr>
              </thead>
              <tbody>
                {comLig.map((c) => (
                  <tr key={c.corretora_id}>
                    <td>{c.label}</td>
                    <td className="forte">{n(c.ligacoes.feitas)}</td>
                    <td>{n(c.ligacoes.atendidas)}</td>
                    <td>{n(c.ligacoes.nao_atendidas)}</td>
                    <td>{n(c.ligacoes.pessoas)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {l.feitas === 0 && <div className="vg-at-vazio">Nenhuma ligação registrada no período.</div>}
      </section>
    </>
  );
}
