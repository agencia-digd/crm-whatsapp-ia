// 👋 Meu dia — a tela que responde "o que eu faço AGORA" pra corretora.
//
// É a ÚNICA tela do painel que muda a rotina dela (o resto é gestão), e por
// isso é montagem do que já existe — cadência, conversas, leads do dia,
// tarefas e fila de ligação — num endpoint só (/painel/meu-dia).
//
// SERVER component fino, igual às outras: só resolve quem está olhando e
// entrega pro cliente. O escopo real (corretora vê só o dela) é do copiloto,
// via X-Painel-User; aqui o viewer serve só pro cabeçalho ("MEU DIA — Pessoa10")
// e pra chave do confete no localStorage.
//
// Aparece pra TODOS: admin também abre e enxerga o dia da equipe inteira.
import { headers } from "next/headers";
import { resolveViewer } from "@/lib/config";
import ClienteMeuDia from "./Cliente";

export default function Page() {
  const viewer = resolveViewer(headers().get("x-auth-user"));
  return (
    <ClienteMeuDia
      user={viewer.user}
      nome={viewer.corretora ?? (viewer.isAdmin ? "equipe" : viewer.user)}
    />
  );
}
