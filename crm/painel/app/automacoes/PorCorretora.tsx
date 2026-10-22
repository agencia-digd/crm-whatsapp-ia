"use client";

// LIGADA POR CORRETORA — o follow-up e a nutrição de cada corretora (06/10/2026).
//
// Um clique = uma corretora, com confirmação que diz quantos leads dela param ou
// voltam. Grava pela rota própria (`PUT …/follow-up/corretoras/{id}` ou
// `…/nutricao/corretoras/{id}`), que mexe só no item dela e devolve o que ficou
// no banco RELIDO: se não for o pedido, a tela diz, em vez de "gravado". O motor
// relê a mesma chave antes de cada mensagem.
//
// Regras do servidor que a tela só mostra:
//   - fora do mapa = ligada (nasce vazio: publicar não desliga ninguém);
//   - o geral desligado vence: "ligada" aqui não manda nada sem o geral;
//   - mapa gravado torto (ilegível) segura TODO mundo; o conserto é voltar ao
//     padrão, e mexer numa corretora devolve 409 até lá;
//   - na nutrição o interruptor é da DONA do lead. A linha de saída (07/10) é
//     a da dona quando ela tem linha e modelo aprovado, senão a da Pessoa12 — o
//     servidor diz por corretora (`sai_por`, `motivo_linha_texto`) e a chave
//     `nutricao_pela_linha_da_dona` desligada manda tudo pela Pessoa12.

import { useState } from "react";
import Interruptor from "./Interruptor";
import type { Chave, Resultado } from "./configOperacional";
import { Bolinha, linhaDoServidor, mil, saudeEmTexto } from "./painel";
import type { BlocoFollowUp, BlocoNutricao, CorretoraFollowUp, CorretoraNutricao, JanelaEnvio } from "./types";
import d from "./remarketing/disparo.module.css";
import p from "./painel.module.css";

type Qual = "follow-up" | "nutricao";
type Linha = CorretoraFollowUp | CorretoraNutricao;

const CHAVE: Record<Qual, Chave> = { "follow-up": "cadencia_por_corretora", nutricao: "nutricao_por_corretora" };
const NOME: Record<Qual, string> = { "follow-up": "o follow-up", nutricao: "a nutrição" };

const devidas = (c: Linha) => ("devidas_agora" in c ? c.devidas_agora : c.devidos_agora);

function janela(j: JanelaEnvio | null): string {
  if (!j) return "sem leitura";
  if (j.saiu === 0 && j.falhou === 0) return "nada saiu";
  const partes = [`${mil(j.saiu)} saíram`];
  if (j.saiu - j.sem_rastro > 0) partes.push(`${mil(j.entregue)} entregues`);
  if (j.falhou > 0) partes.push(`${mil(j.falhou)} falharam`);
  if (j.sem_rastro > 0) partes.push(`${mil(j.sem_rastro)} sem rastro`);
  return partes.join(", ");
}

export default function PorCorretora({
  qual,
  bloco,
  restaurar,
  ocupadoRestaurar,
  onGravou,
  gravar,
  ocupadoLinhaDaDona = false,
}: {
  qual: Qual;
  bloco: BlocoFollowUp | BlocoNutricao;
  restaurar: (chave: Chave, erroLocal?: boolean) => Promise<Resultado>;
  ocupadoRestaurar: boolean;
  onGravou: () => void;
  /** Só na nutrição: grava `nutricao_pela_linha_da_dona`. */
  gravar?: (chave: Chave, valor: unknown, erroLocal?: boolean) => Promise<Resultado>;
  ocupadoLinhaDaDona?: boolean;
}) {
  const [ocupada, setOcupada] = useState<number | null>(null);
  const [erros, setErros] = useState<Record<number, string>>({});
  const [erroGeral, setErroGeral] = useState<string | null>(null);
  const it = bloco.interruptor;
  const geral = bloco.geral.ligado;
  const nome = NOME[qual];
  const naNutricao = qual === "nutricao";
  const nut = naNutricao ? (bloco as BlocoNutricao) : null;
  // Servidor anterior a 07/10 não manda `toda_pela_corretor`: lá tudo saía pela Pessoa12.
  const todaPelaGi = nut ? (nut.toda_pela_corretor ?? true) : false;
  const pelaDona = nut?.pela_linha_da_dona ?? null;
  const [erroDona, setErroDona] = useState<string | null>(null);
  // Nome de quem é a linha de um slug ("corretor" → "Pessoa10"), pelo cadastro do bloco.
  const nomeDaLinha = (slug: string | null | undefined) =>
    (slug && bloco.corretoras.find((c) => "linha_da_corretora" in c && c.linha_da_corretora === slug)?.label) ||
    (slug ? slug[0].toUpperCase() + slug.slice(1) : "Pessoa12");
  const saidaDe = (c: Linha) => ("linha_da_corretora" in c ? c.sai_por : null);

  async function mudar(c: Linha, ligada: boolean) {
    setOcupada(c.corretora_id);
    setErros((e) => ({ ...e, [c.corretora_id]: "" }));
    try {
      const r = await fetch(`/api/automacoes/${qual}/corretoras/${c.corretora_id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ligada }),
      });
      const out = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(out?.detail || out?.error || `HTTP ${r.status}`);
      if (!out.ok || out.gravado !== ligada) {
        throw new Error(`o banco ficou com ${String(out.gravado)}, não com o que foi pedido. Recarregue.`);
      }
    } catch (e) {
      setErros((x) => ({ ...x, [c.corretora_id]: String((e as Error).message || e) }));
    } finally {
      setOcupada(null);
      onGravou();
    }
  }

  const titulo = naNutricao ? "Por corretora (dona do lead)" : "Por corretora";

  return (
    <div className={`${d.bloco} ${d.blocoLargo}`} data-testid={`por-corretora-${qual}`}>
      <h3 className={d.blocoTitulo}>{titulo}</h3>
      {naNutricao && (
        <p className={d.contagem} data-testid="nutricao-por-qual-linha">
          {todaPelaGi ? (
            <>
              Sai toda pela linha da <strong>Pessoa12</strong>, inclusive o lead das outras corretoras
              {pelaDona?.ligada ? " (nenhuma outra tem linha com modelo aprovado agora)" : ""}. Desligar aqui para os
              marcos dos leads DELA; a linha de saída não muda.
            </>
          ) : (
            <>
              Cada marco sai pela <strong>linha da corretora dona do lead</strong> quando ela tem linha e modelo
              aprovado; senão, pela da Pessoa12. Desligar aqui para os marcos dos leads DELA.
            </>
          )}
        </p>
      )}
      {naNutricao && pelaDona && gravar && (
        <div data-testid="nutricao-linha-da-dona">
          <span className={d.contagem}>Sair pela linha da dona do lead </span>
          <Interruptor
            ativa={pelaDona.ligada}
            ocupado={ocupadoLinhaDaDona}
            rotulo={pelaDona.ligada ? "Mandar toda a nutrição pela linha da Pessoa12" : "Mandar a nutrição pela linha da dona do lead"}
            confirmarDesligar={
              "Mandar toda a nutrição pela linha da Pessoa12?\n\nOs marcos dos leads da Pessoa10, da Pessoa11 e das outras passam a sair " +
              "pelo número da Pessoa12, e a resposta chega no WhatsApp da Pessoa12. Conta no limite por dia da Pessoa12."
            }
            confirmarLigar={
              "Mandar cada marco pela linha da dona do lead?\n\nQuem tem linha e modelo aprovado recebe pelo número da " +
              "corretora dona, e a resposta chega no WhatsApp dela. Quem não tem continua pela Pessoa12."
            }
            onMudar={(novo) => {
              setErroDona(null);
              void gravar("nutricao_pela_linha_da_dona", novo, true).then((r) => {
                if (!r.ok) setErroDona(r.erro ?? "não consegui gravar");
                onGravou();
              });
            }}
          />
          {pelaDona.problema && (
            <p className={d.erroTexto}>
              {pelaDona.problema === "ilegivel"
                ? "O valor gravado não é ligado/desligado: o motor está mandando tudo pela Pessoa12."
                : "Não consegui ler esta chave: o motor está mandando tudo pela Pessoa12."}
            </p>
          )}
          {erroDona && <p className={d.erroTexto}>{erroDona}</p>}
        </div>
      )}
      {it.indisponivel ? (
        <p className={d.erroTexto}>Não consegui ler quem está ligado por corretora. Enquanto isso o motor segura.</p>
      ) : it.ilegivel ? (
        <>
          <p className={d.erroTexto} data-testid="interruptor-ilegivel">
            O valor gravado está ilegível ({it.motivo}). O motor está segurando todas as corretoras.
          </p>
          <button
            type="button"
            className={`${d.link} ${d.linkEsquerda}`}
            disabled={ocupadoRestaurar}
            onClick={() => {
              if (!window.confirm(`Voltar ao padrão? ${nome[0].toUpperCase()}${nome.slice(1)} fica ligad${qual === "follow-up" ? "o" : "a"} pra todas as corretoras.`)) return;
              void restaurar(CHAVE[qual], true).then((r) => {
                if (!r.ok) setErroGeral(r.erro ?? "não consegui voltar ao padrão");
                onGravou();
              });
            }}
          >
            voltar ao padrão (todas ligadas)
          </button>
        </>
      ) : null}
      {geral === false && (
        <p className={d.erroTexto} data-testid="geral-desligado">
          O interruptor geral está desligado: nada sai pra ninguém, mesmo pra quem está ligada aqui.
        </p>
      )}
      <ul className={p.fila}>
        {bloco.corretoras.map((c) => {
          const l = linhaDoServidor(c.label, c.linha);
          const fora = "telefone_fora_do_padrao" in c ? c.telefone_fora_do_padrao : null;
          const n = devidas(c);
          const regua = c.na_regua === null ? "régua sem leitura" : `${mil(c.na_regua)} na régua${n ? `, ${mil(n)} devidos agora` : ""}`;
          return (
            <li
              key={c.corretora_id}
              className={`${p.filaItem} ${p.corrItem}`}
              data-corretora={c.label}
              data-ligada={c.ligada === null ? "sem-leitura" : c.ligada ? "sim" : "nao"}
            >
              <span className={p.filaNome}>
                {naNutricao ? null : <Bolinha cor={l.temLinha ? l.saude.cor : "desconhecida"} />}
                <span>
                  {c.label}
                  <span className={p.corrSub}>
                    {naNutricao
                      ? `${regua} · sai pela linha da ${nomeDaLinha(saidaDe(c))}` +
                        ("motivo_linha" in c && c.motivo_linha && c.motivo_linha !== "dona_com_linha" && c.motivo_linha_texto
                          ? ` (${c.motivo_linha_texto.replace(/, sai pela da Pessoa12$/, "")})`
                          : "")
                      : l.temLinha
                        ? `${regua} · ${saudeEmTexto(l.saude)}${l.cobranca ? ", recusando por cobrança" : ""}`
                        : `${regua} · sem linha, nada sai pra ela`}
                  </span>
                  <span className={p.corrSub}>
                    hoje: {janela(c.hoje)} · 7 dias: {janela(c.semana)}
                    {fora ? ` · ${mil(fora)} com telefone fora do padrão` : ""}
                  </span>
                  {c.ligada && c.ligada_efetiva === false && <span className={p.corrSub}>ligada, mas o geral está desligado</span>}
                  {erros[c.corretora_id] && <span className={d.erroTexto}>{erros[c.corretora_id]}</span>}
                </span>
              </span>
              {c.ligada === null ? (
                <span className={d.dica}>sem leitura</span>
              ) : (
                <Interruptor
                  ativa={c.ligada}
                  ocupado={ocupada !== null || it.ilegivel}
                  rotulo={`${c.ligada ? "Desligar" : "Ligar"} ${nome} da ${c.label}`}
                  confirmarDesligar={
                    `Desligar ${nome} da ${c.label}?\n\n` +
                    (c.na_regua !== null ? `Os ${mil(c.na_regua)} leads dela ${naNutricao ? "na nutrição param de receber os marcos" : "na régua param de receber"}` : "Os leads dela param de receber") +
                    " e retomam de onde pararam quando religar. As outras corretoras não mudam."
                  }
                  confirmarLigar={
                    `Religar ${nome} da ${c.label}?\n\n` +
                    (c.na_regua !== null ? `Os ${mil(c.na_regua)} leads dela voltam a receber` : "Os leads dela voltam a receber") +
                    (naNutricao ? ` no marco de cada um, pela linha da ${nomeDaLinha(saidaDe(c))}.` : " no próximo horário do motor, pela linha dela.") +
                    (geral === false ? "\n\nO geral está desligado: nada sai até ligar o geral." : "")
                  }
                  onMudar={(novo) => void mudar(c, novo)}
                />
              )}
            </li>
          );
        })}
      </ul>
      {erroGeral && <p className={d.erroTexto}>{erroGeral}</p>}
      {!it.ilegivel && it.personalizado && bloco.corretoras.some((c) => c.ligada === false) && (
        <button
          type="button"
          className={`${d.link} ${d.linkEsquerda}`}
          disabled={ocupadoRestaurar}
          onClick={() => {
            const desl = bloco.corretoras.filter((c) => c.ligada === false).map((c) => c.label);
            if (!window.confirm(`Religar ${nome} pra todas? Volta pra ${desl.join(", ")}.`)) return;
            void restaurar(CHAVE[qual], true).then((r) => {
              if (!r.ok) setErroGeral(r.erro ?? "não consegui voltar ao padrão");
              onGravou();
            });
          }}
        >
          religar todas (voltar ao padrão)
        </button>
      )}
      <p className={d.dica}>Vale a partir da próxima mensagem. Quem não tem linha Cloud não recebe nada de qualquer jeito.</p>
    </div>
  );
}
