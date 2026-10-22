"use client";

import { useEffect, useState } from "react";
import { usePathname } from "next/navigation";

// No desktop a sidebar é fixa e este componente não aparece. No celular ela
// vira gaveta: 224px fixos numa tela de 390px comiam mais da metade do espaço.
//
// Fecha sozinha ao trocar de página — senão a corretora toca no item, a página
// muda por baixo e a gaveta continua aberta na frente.
export default function MenuMobile() {
  const [aberto, setAberto] = useState(false);
  const caminho = usePathname();

  useEffect(() => setAberto(false), [caminho]);

  useEffect(() => {
    document.body.classList.toggle("menu-aberto", aberto);
    return () => document.body.classList.remove("menu-aberto");
  }, [aberto]);

  return (
    <>
      <header className="topo-mobile">
        <button
          className="menu-btn"
          onClick={() => setAberto((v) => !v)}
          aria-label={aberto ? "Fechar menu" : "Abrir menu"}
          aria-expanded={aberto}
        >
          {aberto ? "✕" : "☰"}
        </button>
        <span className="topo-marca">
          Acme <em>v2</em>
        </span>
      </header>
      {aberto && (
        <button
          className="menu-fundo"
          aria-label="Fechar menu"
          onClick={() => setAberto(false)}
        />
      )}
    </>
  );
}
