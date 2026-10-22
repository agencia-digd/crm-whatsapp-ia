"use client";

import { useEffect } from "react";

/**
 * Trava a rolagem GERAL da página enquanto a tela estiver montada.
 *
 * Pra que existe (pedido Dona 07/08): CRM e Chat já ocupam a tela inteira e
 * rolam por dentro (as colunas do kanban, a thread da conversa). Só que eles
 * continuavam dentro da rolagem do documento — então a barra da direita ainda
 * andava, empurrava a tela inteira pra cima e revelava uma faixa BRANCA
 * embaixo, sem conteúdo nenhum. A pessoa rolava e "saía" do sistema.
 *
 * Aqui a rolagem externa some nessas telas: quem rola é o miolo. Os outros
 * módulos (Meu dia, Tarefas, Visitas…) seguem com a rolagem normal da página,
 * porque neles o conteúdo cresce de verdade pra baixo.
 *
 * Restaura o valor anterior ao desmontar — nunca deixa a página travada
 * quando a corretora navega pra outro módulo.
 */
// Abaixo disso o layout vira coluna única (mesmo 820px do globals.css) e a
// tela PRECISA rolar: travar no celular prenderia a corretora numa tela que
// não cabe. A trava é só do desktop, onde o layout é de altura fixa.
const LARGURA_MINIMA = 821;

export function useTravarRolagemDaPagina(): void {
  useEffect(() => {
    const anteriorBody = document.body.style.overflow;
    const anteriorHtml = document.documentElement.style.overflow;

    const aplicar = () => {
      const trava = window.innerWidth >= LARGURA_MINIMA;
      document.body.style.overflow = trava ? "hidden" : anteriorBody;
      document.documentElement.style.overflow = trava ? "hidden" : anteriorHtml;
    };
    aplicar();
    // girar o tablet ou redimensionar a janela reavalia — senão a trava fica
    // presa no estado de quando a tela abriu.
    window.addEventListener("resize", aplicar);

    return () => {
      window.removeEventListener("resize", aplicar);
      document.body.style.overflow = anteriorBody;
      document.documentElement.style.overflow = anteriorHtml;
    };
  }, []);
}
