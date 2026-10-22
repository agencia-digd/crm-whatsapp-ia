"use client";

import { useEffect, useState } from "react";
import { respondidosNaoAtendidos } from "@/lib/respondidos";

// Contador verde no item CRM do menu lateral: nº de leads que RESPONDERAM na
// cadência (<48h) e seguem em "Em atendimento" sem a corretora dar conta.
// Poll do board a cada 60s (mesmo ritmo do CRM); falha = badge some,
// nunca quebra a navegação.
const REFRESH_MS = 60_000;

export default function RespondeuBadge() {
  const [n, setN] = useState(0);

  useEffect(() => {
    let vivo = true;
    const carregar = async () => {
      try {
        const r = await fetch("/api/crm", { cache: "no-store" });
        const board = await r.json();
        if (vivo && r.ok) setN(respondidosNaoAtendidos(board).length);
      } catch {
        /* silencioso — aviso é acessório */
      }
    };
    carregar();
    const t = setInterval(() => {
      if (document.visibilityState === "visible") carregar();
    }, REFRESH_MS);
    return () => {
      vivo = false;
      clearInterval(t);
    };
  }, []);

  if (n === 0) return null;
  return (
    <span
      className="nav-badge-verde"
      title={`${n} lead${n > 1 ? "s" : ""} respondeu${n > 1 ? "ram" : ""} na cadência — corre lá`}
    >
      {n}
    </span>
  );
}
