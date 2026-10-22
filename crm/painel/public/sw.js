// Service worker do painel Acme — existe por um motivo só: receber o aviso
// de lead novo quando o app está fechado. Não faz cache offline de propósito
// (dado de CRM velho em tela é pior que tela vazia).

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));

self.addEventListener("push", (event) => {
  let d = {};
  try {
    d = event.data ? event.data.json() : {};
  } catch {
    d = { titulo: "Acme", corpo: event.data ? event.data.text() : "" };
  }

  const titulo = d.titulo || "Acme";
  const opcoes = {
    body: d.corpo || "",
    // O iPhone ignora ícone customizado na tela de bloqueio (usa o do app
    // instalado), mas o Android usa — então vale mandar.
    icon: "/icon-192.png",
    badge: "/icon-192.png",
    tag: d.tag || "acme",
    // Substitui a notificação de mesma tag sem tocar som de novo.
    renotify: false,
    data: { url: d.url || "/" },
    // Vibra: lead novo compete com o bolso da corretora.
    vibrate: [120, 60, 120],
  };

  event.waitUntil(self.registration.showNotification(titulo, opcoes));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const alvo = (event.notification.data && event.notification.data.url) || "/";

  // Se o painel já estiver aberto numa aba, foca ela em vez de abrir outra.
  event.waitUntil(
    self.clients
      .matchAll({ type: "window", includeUncontrolled: true })
      .then((janelas) => {
        for (const j of janelas) {
          if ("focus" in j) {
            j.navigate?.(alvo);
            return j.focus();
          }
        }
        return self.clients.openWindow(alvo);
      }),
  );
});
