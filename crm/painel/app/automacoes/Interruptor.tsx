"use client";

// Interruptor de uma chave booleana da operação. O MESMO controle aparece no
// bloco "Controle da operação" e no cabeçalho da página de uma automação —
// dois desenhos diferentes pro mesmo botão fariam a Dona achar que são duas
// chaves. Só apresentação: quem grava é o hook `useConfigOperacional`.

import s from "./controle.module.css";

export default function Interruptor({
  ativa,
  ocupado,
  rotulo,
  confirmarDesligar,
  confirmarLigar,
  grande = false,
  onMudar,
}: {
  ativa: boolean;
  ocupado: boolean;
  rotulo: string;
  /** Texto do confirm ao DESLIGAR. */
  confirmarDesligar?: string;
  /** Texto do confirm ao LIGAR. Sem ele, ligar não pergunta. O remarketing
   *  pergunta: ligar ali solta mensagem pra base inteira, com o número na frase. */
  confirmarLigar?: string;
  /** Versão de 44 px de altura, pro interruptor que manda na página. */
  grande?: boolean;
  onMudar: (novo: boolean) => void;
}) {
  return (
    <span className={s.linhaAcao}>
      <button
        type="button"
        role="switch"
        aria-checked={ativa}
        aria-label={rotulo}
        className={`${s.chave} ${grande ? s.chaveGrande : ""} ${ativa ? s.chaveOn : s.chaveOff}`}
        disabled={ocupado}
        onClick={() => {
          if (ativa && confirmarDesligar && !confirm(confirmarDesligar)) return;
          if (!ativa && confirmarLigar && !confirm(confirmarLigar)) return;
          onMudar(!ativa);
        }}
      >
        <span className={s.chaveBola} />
      </button>
      {!grande && (
        <strong className={ativa ? s.estadoOn : s.estadoOff}>
          {ativa ? "ligado" : "desligado"}
        </strong>
      )}
    </span>
  );
}
