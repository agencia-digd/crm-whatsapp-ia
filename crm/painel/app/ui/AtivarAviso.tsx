"use client";

import { useCallback, useEffect, useState } from "react";

// Botão "quero ser avisada quando cair lead".
//
// A trava que manda no desenho desta tela: no iPhone o push SÓ funciona com o
// painel adicionado à tela de início. Aberto no Safari, a permissão nem é
// oferecida pelo sistema. Como as corretoras da Acme são todas iPhone, a
// tela precisa detectar isso e ENSINAR o passo — senão a corretora aperta o
// botão, não acontece nada, e ela conclui que "não funciona".

type Estado =
  | "carregando"
  | "indisponivel"      // navegador sem suporte
  | "precisa_instalar"  // iPhone no Safari, fora da tela de início
  | "desligado"
  | "ligado"
  | "negado";           // ela recusou a permissão no sistema

function base64ParaUint8(base64: string) {
  const pad = "=".repeat((4 - (base64.length % 4)) % 4);
  const b64 = (base64 + pad).replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(b64);
  const bytes = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
  return bytes;
}

function ehIOS() {
  return /iPad|iPhone|iPod/.test(navigator.userAgent);
}

function estaInstalado() {
  return (
    window.matchMedia("(display-mode: standalone)").matches ||
    // Safari usa a propriedade própria, fora do padrão.
    (window.navigator as unknown as { standalone?: boolean }).standalone === true
  );
}

export default function AtivarAviso() {
  const [estado, setEstado] = useState<Estado>("carregando");
  const [chave, setChave] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [ocupado, setOcupado] = useState(false);

  useEffect(() => {
    (async () => {
      if (!("serviceWorker" in navigator) || !("PushManager" in window)) {
        // No iPhone fora da tela de início, PushManager nem existe — então
        // esse caso precisa ser separado de "navegador velho".
        setEstado(ehIOS() && !estaInstalado() ? "precisa_instalar" : "indisponivel");
        return;
      }
      if (ehIOS() && !estaInstalado()) {
        setEstado("precisa_instalar");
        return;
      }
      if (Notification.permission === "denied") {
        setEstado("negado");
        return;
      }

      try {
        const r = await fetch("/api/push", { cache: "no-store" });
        const d = await r.json();
        if (!d?.chave_publica) {
          setEstado("indisponivel");
          return;
        }
        setChave(d.chave_publica);

        const reg = await navigator.serviceWorker.register("/sw.js");
        const sub = await reg.pushManager.getSubscription();
        setEstado(sub ? "ligado" : "desligado");
      } catch {
        setEstado("indisponivel");
      }
    })();
  }, []);

  const ligar = useCallback(async () => {
    if (!chave) return;
    setOcupado(true);
    setMsg(null);
    try {
      const permissao = await Notification.requestPermission();
      if (permissao !== "granted") {
        setEstado("negado");
        return;
      }
      const reg = await navigator.serviceWorker.register("/sw.js");
      const sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: base64ParaUint8(chave),
      });
      const r = await fetch("/api/push?acao=inscrever", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(sub.toJSON()),
      });
      if (!r.ok) throw new Error("falhou");
      setEstado("ligado");
      setMsg("Pronto! Vou te avisar aqui quando cair lead.");
    } catch {
      setMsg("Não consegui ligar o aviso. Tenta de novo?");
    } finally {
      setOcupado(false);
    }
  }, [chave]);

  const desligar = useCallback(async () => {
    setOcupado(true);
    setMsg(null);
    try {
      const reg = await navigator.serviceWorker.getRegistration();
      const sub = await reg?.pushManager.getSubscription();
      if (sub) {
        await fetch("/api/push?acao=desinscrever", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ endpoint: sub.endpoint }),
        });
        await sub.unsubscribe();
      }
      setEstado("desligado");
    } catch {
      setMsg("Não consegui desligar agora.");
    } finally {
      setOcupado(false);
    }
  }, []);

  const testar = useCallback(async () => {
    setOcupado(true);
    setMsg(null);
    try {
      const r = await fetch("/api/push?acao=testar", { method: "POST" });
      const d = await r.json();
      setMsg(
        d?.enviados > 0
          ? "Mandei! Deve chegar em instantes."
          : "Não achei aparelho ligado — liga o aviso primeiro.",
      );
    } catch {
      setMsg("Não consegui enviar o teste.");
    } finally {
      setOcupado(false);
    }
  }, []);

  if (estado === "carregando" || estado === "indisponivel") return null;

  return (
    <div className="aviso-card">
      {estado === "precisa_instalar" && (
        <>
          <b>Quer ser avisada quando cair lead?</b>
          <p>
            No iPhone, primeiro instala o painel na tela de início — aí eu
            consigo te avisar mesmo com o app fechado:
          </p>
          <ol>
            <li>
              Toca no botão <b>Compartilhar</b> aqui embaixo do Safari
              (o quadradinho com a setinha pra cima)
            </li>
            <li>
              Escolhe <b>Adicionar à Tela de Início</b>
            </li>
            <li>Abre o Acme pelo ícone novo e volta aqui</li>
          </ol>
        </>
      )}

      {estado === "negado" && (
        <>
          <b>Aviso bloqueado</b>
          <p>
            Você recusou as notificações antes. Pra liberar: Ajustes → Acme →
            Notificações → permitir.
          </p>
        </>
      )}

      {estado === "desligado" && (
        <>
          <b>Quer ser avisada quando cair lead?</b>
          <p>Chega o nome e o telefone na hora, mesmo com o app fechado.</p>
          <button onClick={ligar} disabled={ocupado} className="aviso-btn">
            {ocupado ? "Ligando…" : "Quero ser avisada"}
          </button>
        </>
      )}

      {estado === "ligado" && (
        <>
          <b>✓ Aviso ligado neste aparelho</b>
          <p>Quando cair lead seu, chega aqui na hora.</p>
          <div className="aviso-acoes">
            <button onClick={testar} disabled={ocupado} className="aviso-btn">
              Mandar teste
            </button>
            <button onClick={desligar} disabled={ocupado} className="aviso-link">
              desligar
            </button>
          </div>
        </>
      )}

      {msg && <p className="aviso-msg">{msg}</p>}
    </div>
  );
}
