// Página da automação de BOAS-VINDAS.
//
// Mesma porta de entrada das outras telas de gestão: SERVER component só pra
// checar quem é. Corretora não vê o número da equipe inteira, não mexe na
// roleta nem em copy — o menu já esconde, mas a URL direta abria uma tela
// quebrada (a API responde 403). Aqui nem entra: volta pro CRM dela.
import { redirect } from "next/navigation";
import { headers } from "next/headers";
import { resolveViewer } from "@/lib/config";
import ClienteBoasVindas from "./Cliente";

export default function Page() {
  const viewer = resolveViewer(headers().get("x-auth-user"));
  if (!viewer.isAdmin) redirect("/crm");
  return <ClienteBoasVindas />;
}
