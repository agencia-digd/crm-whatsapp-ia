// Porta de entrada da tela — SERVER component só pra checar quem é.
//
// Estas telas são de GESTÃO (números da equipe inteira, configuração de
// automação). Corretora não precisa e não deve ver: o menu já escondia o link,
// mas a página abria por URL direta/favorito e a pessoa via uma tela quebrada
// (a API responde 403). Agora nem entra: volta pro CRM dela.
// Pedido da Dona 27/07.
import { redirect } from "next/navigation";
import { headers } from "next/headers";
import { resolveViewer } from "@/lib/config";
import Cliente from "./Cliente";

export default function Page() {
  const viewer = resolveViewer(headers().get("x-auth-user"));
  if (!viewer.isAdmin) redirect("/crm");
  return <Cliente />;
}
