// Página da automação de NUTRIÇÃO.
//
// Mesma porta de entrada da página de Follow-up: SERVER component só pra checar
// quem é. Corretora não vê número da equipe inteira — o menu já esconde, mas a
// URL direta abria uma tela quebrada (a API responde 403). Aqui nem entra:
// volta pro CRM dela.
import { redirect } from "next/navigation";
import { headers } from "next/headers";
import { resolveViewer } from "@/lib/config";
import ClienteNutricao from "./Cliente";

export default function Page() {
  const viewer = resolveViewer(headers().get("x-auth-user"));
  if (!viewer.isAdmin) redirect("/crm");
  return <ClienteNutricao />;
}
