import { redirect } from "next/navigation";

// REMARKETING deixou de ser módulo separado (pedido Dona 11/07): virou
// seletor de funil dentro do /crm ("Funil de vendas" · "Remarketing").
// A rota fica como redirect pra não quebrar link/bookmark antigo.
// O proxy /api/remarketing continua vivo — é o CRM quem consome agora.
export default function RemarketingRedirect() {
  redirect("/crm");
}
