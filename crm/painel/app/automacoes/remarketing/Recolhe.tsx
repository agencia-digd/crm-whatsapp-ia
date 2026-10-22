"use client";

// Bloco secundário que fecha sozinho no celular (≤ 820 px) e fica aberto no
// computador. É <details> de verdade: abre por teclado (Enter/Espaço no resumo),
// e sem JavaScript o HTML nasce aberto — nada some. O conteúdo continua montado
// quando fechado, então o que a pessoa digitou não se perde ao recolher.
import { useEffect, useState, type ReactNode } from "react";
import s from "./recolhe.module.css";

export default function Recolhe({
  titulo,
  dica,
  children,
}: {
  titulo: string;
  dica?: string;
  children: ReactNode;
}) {
  const [aberto, setAberto] = useState(true);

  useEffect(() => {
    if (window.matchMedia("(max-width: 820px)").matches) setAberto(false);
  }, []);

  return (
    <details
      className={s.recolhe}
      open={aberto}
      onToggle={(e) => setAberto((e.currentTarget as HTMLDetailsElement).open)}
    >
      <summary className={s.resumo}>
        <span>{titulo}</span>
        {dica && <span className={s.dica}>{dica}</span>}
      </summary>
      {children}
    </details>
  );
}
