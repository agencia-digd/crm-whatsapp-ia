"use client";

// Assistente em todo o painel — botão flutuante global (pedido Dona 24/07).
// Reusa o modal do Chat (app/chat/Assistente.tsx) sem mudança: mesmo backend
// (/api/assistente*), mesmas ações com confirmação, nada vai pro lead sozinho.
// No /chat o FAB se esconde — lá a Assistente já tem botão próprio no header
// (e abre com o contexto do lead selecionado, que o FAB não tem).

import { useState } from "react";
import { usePathname } from "next/navigation";
import Assistente from "../chat/Assistente";

export default function AssistenteFab() {
  const [on, setOn] = useState(false);
  const pathname = usePathname();
  if (pathname?.startsWith("/chat")) return null;
  return (
    <>
      <button
        onClick={() => setOn(true)}
        title="Assistente — sua copilota. Pergunta sobre leads, seu dia e imóveis, gera arte do empreendimento e registra ações (com confirmação)."
        aria-label="Abrir Assistente"
        style={{
          position: "fixed", right: 22, bottom: 22, zIndex: 70,
          width: 54, height: 54, borderRadius: "50%", border: "none",
          background: "#15110d", color: "#fff", fontSize: 24, cursor: "pointer",
          boxShadow: "0 6px 20px rgba(0,0,0,.25)",
        }}
      >
        🧠
      </button>
      {on && <Assistente onClose={() => setOn(false)} />}
    </>
  );
}
