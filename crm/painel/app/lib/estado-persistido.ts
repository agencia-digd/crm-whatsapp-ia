"use client";

import { useEffect, useState } from "react";

/**
 * useState que sobrevive à navegação, guardando no localStorage do navegador.
 *
 * Nasceu do filtro de corretora (pedido Dona 29/07): ela escolhia a corretora
 * no CRM, clicava no card de um lead e, ao voltar, o filtro tinha zerado —
 * tinha que selecionar de novo a cada ida e volta. Cada módulo guarda sob a
 * própria chave, então CRM e Tarefas podem estar em corretoras diferentes.
 *
 * O valor salvo é lido JÁ no primeiro render (inicializador lazy do useState),
 * não num efeito. Restaurar um render depois criava uma corrida: a tela
 * disparava o fetch com o filtro ainda vazio e o board vinha sem filtro — a
 * pill da corretora aparecia marcada, mas a lista era de todas.
 *
 * No servidor não existe `window`, então lá o valor é o default. Isso só é
 * seguro porque quem usa o hook renderiza o filtro depois dos dados chegarem
 * (board/lista nulos no primeiro render, no servidor e no cliente) — o HTML
 * hidratado é idêntico dos dois lados.
 */
function ler<T>(chave: string, inicial: T): T {
  if (typeof window === "undefined") return inicial;
  try {
    const cru = localStorage.getItem(chave);
    return cru === null ? inicial : (JSON.parse(cru) as T);
  } catch {
    return inicial; // storage bloqueado ou JSON corrompido: segue com o default
  }
}

export function useEstadoPersistido<T>(chave: string, inicial: T) {
  const [valor, setValor] = useState<T>(() => ler(chave, inicial));

  useEffect(() => {
    try {
      localStorage.setItem(chave, JSON.stringify(valor));
    } catch {
      /* modo privado / cota cheia: perder a preferência é aceitável */
    }
  }, [chave, valor]);

  return [valor, setValor] as const;
}
