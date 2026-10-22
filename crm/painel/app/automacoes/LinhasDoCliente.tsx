"use client";

// AS LINHAS DO CLIENTE — saúde de todos os números numa leitura só (06/10/2026).
//
// Vem de `GET /painel/automacoes/linhas`: dona, número, papel, cor na Meta e se a
// linha está recusando por cobrança. É a resposta pra "posso confiar nesta
// linha?" sem abrir automação por automação. Somente leitura: o papel de cada
// número se troca na página do remarketing.
//
// Régua de sempre: sem leitura fica tracejada e diz "sem leitura", nunca verde;
// número que o copiloto não tem registrado aparece "não registrada" (nada sai).

import { useEffect, useState } from "react";
import { Bolinha, horaDaLeitura, saudeEmTexto, type Cor } from "./painel";
import type { LinhasDoCliente as Payload } from "./types";
import p from "./painel.module.css";

const PAPEL: Record<string, string> = {
  disparo: "disparo",
  atendimento: "atendimento",
  pausado: "pausado",
};

export default function LinhasDoCliente() {
  const [dados, setDados] = useState<Payload | null>(null);
  const [erro, setErro] = useState<string | null>(null);

  useEffect(() => {
    fetch("/api/automacoes/linhas", { cache: "no-store" })
      .then(async (r) => {
        const body = await r.json();
        if (!r.ok) throw new Error(body?.error || body?.detail || `HTTP ${r.status}`);
        setDados(body);
      })
      .catch((e) => setErro(String((e as Error).message || e)));
  }, []);

  return (
    <section aria-label="Linhas do cliente" data-testid="linhas-todas">
      <h2 className={p.titulo}>Linhas</h2>
      {erro ? (
        <p className={p.itemSub}>Não consegui ler as linhas: {erro}</p>
      ) : !dados ? (
        <p className={p.itemSub}>carregando…</p>
      ) : (
        <>
          <ul className={p.todas}>
            {dados.linhas.map((l) => {
              const cor = (l.registrada ? l.saude?.cor ?? "desconhecida" : "desconhecida") as Cor;
              const ruim = l.registrada && (cor === "vermelho" || l.cobranca_bloqueada === true);
              return (
                <li
                  key={l.slug}
                  className={`${p.todasItem} ${ruim ? p.todasRuim : ""}`}
                  data-slug={l.slug}
                  data-cor={l.registrada ? cor : "nao-registrada"}
                >
                  <span className={p.todasNome}>
                    <Bolinha cor={cor} />
                    {l.corretora ?? l.slug}
                  </span>
                  <span className={p.todasNumero}>{l.telefone ?? "sem número"}</span>
                  <span className={p.todasPapel}>{l.papel ? PAPEL[l.papel] ?? l.papel : "papel sem leitura"}</span>
                  <span className={ruim ? p.linhaRuim : p.todasSaude}>
                    {!l.registrada
                      ? "não registrada: nada sai por ela"
                      : l.cobranca_bloqueada
                        ? `${saudeEmTexto(l.saude ?? { cor: "desconhecida" })}, recusando por cobrança`
                        : saudeEmTexto(l.saude ?? { cor: "desconhecida" })}
                  </span>
                </li>
              );
            })}
          </ul>
          <p className={p.itemSub} style={{ margin: "8px 0 32px" }}>
            Cor da Meta {horaDaLeitura(dados.lido_em)}.
            {Object.keys(dados.erros ?? {}).length > 0 &&
              ` Sem leitura de: ${Object.keys(dados.erros).join(", ").replaceAll("_", " ")}.`}
          </p>
        </>
      )}
    </section>
  );
}
