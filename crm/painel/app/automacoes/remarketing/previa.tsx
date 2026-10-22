"use client";

// A mensagem como chega no celular: o casco do template (fixo, aprovado na
// Meta) + o texto que vai na variável. Usada na edição da copy do disparo e
// no aviso que o lead recebe quando vai pra corretora.
//
// `canonizar` é a MESMA limpeza que o servidor faz antes de gravar e que o
// envio faz antes de mandar (tab vira espaço, espaços repetidos viram um, no
// máximo uma linha em branco entre parágrafos). A prévia mostra o texto já
// limpo — é o que sai. Quem manda é o servidor; isto só antecipa.

import s from "./copy.module.css";

export function canonizar(texto: string): string {
  return texto
    .replace(/\r\n?/g, "\n")
    .replace(/\t/g, " ")
    .replace(/ {2,}/g, " ")
    .replace(/ *\n */g, "\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

export const ABERTURA_DO_CASCO = "Olá João, tudo bem?";
export const FECHO_DO_CASCO = "Tô por aqui se precisar 🙂";

export default function Previa({ texto, rotulo }: { texto: string; rotulo?: string }) {
  const limpo = canonizar(texto);
  return (
    <figure className={s.previa} data-testid="previa-no-celular">
      <figcaption className={s.previaRotulo}>{rotulo ?? "como chega no celular"}</figcaption>
      <div className={s.balao}>
        <span className={s.balaoFixo}>{ABERTURA_DO_CASCO}</span>
        {"\n\n"}
        {limpo || <span className={s.balaoVazio}>(o texto aparece aqui)</span>}
        {"\n\n"}
        <span className={s.balaoFixo}>{FECHO_DO_CASCO}</span>
      </div>
    </figure>
  );
}
