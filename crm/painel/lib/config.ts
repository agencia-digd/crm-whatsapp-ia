// Config server-side. Segredos só aqui — nunca expostos ao cliente.
// Mesmo padrão de auth do dash-imobiliaria (basic auth no nginx →
// X-Auth-User injetado → resolveViewer → X-Painel-User pro copiloto).
export const cfg = {
  copilotoUrl: process.env.COPILOTO_API_URL ?? "http://127.0.0.1:8002",
  // Fallback de usuário quando o nginx não injeta header.
  //
  // ⚠️ FAIL-CLOSED em produção (auditoria Sol, 29/07). Antes o default era
  // "dona" — que é ADMIN. Qualquer requisição sem `X-Auth-User` (nginx
  // reconfigurado, alguém batendo direto no :3009, um proxy novo esquecendo o
  // header) entrava com acesso administrativo. Hoje o nginx sempre preenche o
  // header, então não havia porta aberta; mas o padrão era fail-open e a tela
  // /sugestoes aprova mensagem que vai pro cliente — não pode depender de uma
  // linha de config estar certa.
  //
  // Em dev (`NODE_ENV !== production`) o fallback continua, senão não dá pra
  // rodar local sem nginx.
  // Em produção NÃO existe fallback — nem via PAINEL_USER. O Sol reparou que
  // o `??` vinha ANTES do teste de NODE_ENV, então um PAINEL_USER no env do
  // :3009 ressuscitava o comportamento antigo; e subir o serviço com
  // `next dev` (NODE_ENV != production) também trazia o "dona" de volta.
  fallbackUser:
    process.env.NODE_ENV === "production"
      ? ""
      : (process.env.PAINEL_USER ?? "dona"),
};

// Admin vê todas as corretoras; corretora vê só a dela.
//
// Vem de env pra cada instância ter os seus: a da Acme mantém dona/assistente/pessoa,
// a da demonstração tem o login do parceiro. Antes era literal aqui — e como as
// duas instâncias rodam o MESMO código, dar acesso a alguém na demo significaria
// escrever o nome dele na lista que a Acme também lê.
// Sem a env, cai no default histórico: nada muda pra quem já roda.
const ADMINS = new Set(
  (process.env.PAINEL_ADMINS ?? "admin")
    .split(",")
    .map((u) => u.trim().toLowerCase())
    .filter(Boolean),
);
const USER_CORRETORA: Record<string, string> = {
  corretor: "Pessoa6",
  corretor: "Pessoa1",
  corretor: "Pessoa3",
  pessoa_d: "Pessoa8",
  // 20/09: mesma função da Pessoa7 (ligação + primeira mensagem). O valor é o
  // `corretoras.nome` no banco do copiloto — sem acento, como os outros.
  corretor: "Corretor",
};

export type Viewer = {
  user: string;
  isAdmin: boolean;
  corretora: string | null; // null = admin/todas
};

export function resolveViewer(rawUser: string | null | undefined): Viewer {
  const user = (rawUser || cfg.fallbackUser).toLowerCase().trim();
  // Sem usuário identificado: NÃO é admin e NÃO é corretora nenhuma. As telas
  // devolvem 403 e o copiloto revalida do lado dele de qualquer forma.
  if (!user) return { user: "", isAdmin: false, corretora: null };
  if (ADMINS.has(user)) return { user, isAdmin: true, corretora: null };
  const corretora = USER_CORRETORA[user] ?? null;
  return { user, isAdmin: false, corretora };
}
