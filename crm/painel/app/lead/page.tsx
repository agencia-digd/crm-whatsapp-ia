"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { linkTel, linkWhats, formatarTel } from "@/lib/telefone";

// Tela que abre quando a corretora toca na notificação de lead novo.
//
// Por que não mandar direto pro /chat: o Chat resolve conversa pelo id do
// KOMMO, que um lead recém-caído ainda não tem (o sync vem depois) — e o
// Chat é uma tela de 2.600 linhas que carrega pesado no 4G. Aqui a corretora
// chega com o celular na mão e um só objetivo: falar com essa pessoa AGORA.
// Então a tela é nome, telefone e três botões grandes.
//
// Tudo vem da URL (?tel=&nome=), então ela abre instantânea, sem esperar API.
// O resto (origem, conversa) carrega depois, sem travar o que importa.

type Info = {
  origem?: string | null;
  corretora?: string | null;
  kommo_lead_id?: string | null;
  entrou?: string | null;
};

export default function LeadPage() {
  const [tel, setTel] = useState("");
  const [nome, setNome] = useState("");
  const [info, setInfo] = useState<Info | null>(null);

  useEffect(() => {
    const p = new URLSearchParams(window.location.search);
    const t = (p.get("tel") || "").trim();
    const n = (p.get("nome") || "").trim();
    setTel(t);
    setNome(n || "Lead novo");

    if (!t) return;
    // Best-effort: se o lead já estiver na base, enriquece a tela. Falhar aqui
    // não atrapalha — os botões de ligar e WhatsApp já funcionam sem isso.
    fetch(`/api/lead?tel=${encodeURIComponent(t)}`, { cache: "no-store" })
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => d && setInfo(d))
      .catch(() => {});
  }, []);

  if (!tel) {
    return (
      <div className="lead-tela">
        <p className="lead-vazio">
          Abre esta tela pela notificação de lead novo.
        </p>
        <Link href="/meu-dia" className="lead-btn lead-btn-fraco">
          Ir pro meu dia
        </Link>
      </div>
    );
  }

  return (
    <div className="lead-tela">
      <div className="lead-tag">🔔 Caiu um lead</div>
      <h1 className="lead-nome">{nome}</h1>
      <a className="lead-tel" href={linkTel(tel)}>
        {formatarTel(tel)}
      </a>

      {info?.origem && <div className="lead-meta">{info.origem}</div>}
      {info?.entrou && <div className="lead-meta">entrou {info.entrou}</div>}

      <div className="lead-acoes">
        {/* Ligar primeiro e maior: lead de imóvel atendido por voz nos
            primeiros minutos agenda muito mais que lead que recebe texto. */}
        <a className="lead-btn lead-btn-forte" href={linkTel(tel)}>
          📞 Ligar agora
        </a>
        <a
          className="lead-btn"
          href={linkWhats(tel)}
          target="_blank"
          rel="noopener"
        >
          💬 Chamar no WhatsApp
        </a>
        {info?.kommo_lead_id ? (
          <Link
            className="lead-btn lead-btn-fraco"
            href={`/chat?lead=${encodeURIComponent(info.kommo_lead_id)}&nome=${encodeURIComponent(nome)}`}
          >
            Ver a conversa
          </Link>
        ) : (
          <Link className="lead-btn lead-btn-fraco" href="/crm">
            Abrir o CRM
          </Link>
        )}
      </div>

      <p className="lead-dica">
        Ligar nos primeiros minutos é o que mais muda o resultado.
      </p>
    </div>
  );
}
