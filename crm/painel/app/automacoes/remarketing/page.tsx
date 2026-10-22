// Página da automação de REMARKETING (carrossel da Corretor).
//
// Mesma porta de entrada das outras telas de gestão: SERVER component só pra
// checar quem é. Corretora não vê número da operação inteira nem mexe no peso
// do teste A/B — o menu já esconde, mas a URL direta abria uma tela quebrada
// (a API responde 403). Aqui nem entra: volta pro CRM dela.
import { redirect } from "next/navigation";
import { headers } from "next/headers";
import { resolveViewer } from "@/lib/config";
import ClienteRemarketing from "./Cliente";

export default function Page() {
  const viewer = resolveViewer(headers().get("x-auth-user"));
  if (!viewer.isAdmin) redirect("/crm");
  return <ClienteRemarketing />;
}
