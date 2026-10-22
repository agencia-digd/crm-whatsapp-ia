import type { Metadata, Viewport } from "next";
import localFont from "next/font/local";
import Link from "next/link";
import { headers } from "next/headers";
import { resolveViewer } from "@/lib/config";
import "./globals.css";
import RespondeuBadge from "./ui/RespondeuBadge";
import AssistenteFab from "./ui/AssistenteFab";
import AtivarAviso from "./ui/AtivarAviso";
import MenuMobile from "./ui/MenuMobile";

const sans = localFont({
  src: "./fonts/GeistVF.woff",
  variable: "--font-sans",
  weight: "100 900",
});
const mono = localFont({
  src: "./fonts/GeistMonoVF.woff",
  variable: "--font-mono",
  weight: "100 900",
});

export const metadata: Metadata = {
  title: "Acme V2",
  description: "Painel Acme — versão 2, módulo a módulo.",
  manifest: "/manifest.webmanifest",
  // O iPhone usa apple-touch-icon pro ícone da tela de início — ele não lê
  // os ícones do manifest. Sem isto, sai um print da tela como ícone.
  // Nome versionado (-v2) de propósito: o iOS guarda o ícone pela URL, então
  // trocar só o conteúdo do arquivo não adianta — quem já instalou continuaria
  // vendo o antigo pra sempre.
  icons: {
    icon: "/icon-192-v2.png",
    apple: "/icon-192-v2.png",
  },
  // Sem isto o iPhone não oferece "Adicionar à Tela de Início" como app — e
  // sem instalar na tela de início, o Safari não entrega push nenhum.
  appleWebApp: {
    capable: true,
    title: "Acme",
    statusBarStyle: "default",
  },
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  // Combina com o fundo do painel: no app instalado a barra de status some
  // dentro da tela em vez de virar uma faixa verde solta.
  themeColor: "#f7f6f3",
  // Respeita a área da barrinha/notch do iPhone.
  viewportFit: "cover",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  // ⚙️ Automações é visão de gestão (mapa vivo de gatilhos/copies) — o item
  // só aparece pra admin. A página e a API revalidam de qualquer jeito
  // (defesa em profundidade); aqui é só pra corretora não ver porta fechada.
  const viewer = resolveViewer(headers().get("x-auth-user"));

  // Telas PÚBLICAS (/login, /sair/confirmar) — as únicas que o nginx deixa
  // passar sem sessão, então são as únicas que chegam aqui sem X-Auth-User.
  // Sem esta saída elas renderizariam a sidebar inteira vazia: menu de módulos
  // que a pessoa ainda não pode abrir e o chip "conta" em branco.
  if (!viewer.user) {
    return (
      <html lang="pt-BR">
        <body className={`${sans.variable} ${mono.variable}`}>{children}</body>
      </html>
    );
  }

  return (
    <html lang="pt-BR">
      <body className={`${sans.variable} ${mono.variable}`}>
        <div className="shell">
          {/* No celular a sidebar vira gaveta; este é o botão que abre. */}
          <MenuMobile />
          <aside className="sidebar" id="sidebar">
            <Link href="/" className="brand">
              <span className="brand-name">Acme</span>
              <span className="brand-tag">v2</span>
            </Link>
            <nav className="nav">
              <div className="nav-label">Módulos</div>
              {/* 👋 Meu dia: PRIMEIRO item de propósito — é a tela que responde
                  "o que eu faço agora" e a única que muda a rotina da
                  corretora. Os 5 blocos vêm ordenados por CUSTO DE DEMORA do
                  copiloto (/painel/meu-dia). Aparece pra todos; o escopo por
                  corretora é do copiloto via X-Painel-User. */}
              <Link href="/meu-dia" className="nav-item">
                <span className="nav-dot" />
                👋 Meu dia
              </Link>
              <Link href="/crm" className="nav-item">
                <span className="nav-dot" />
                CRM
                {/* nº de leads que responderam na cadência e seguem sem
                    atendimento — aviso in-app (lei da Dona: zero WhatsApp) */}
                <RespondeuBadge />
              </Link>
              <Link href="/chat" className="nav-item">
                <span className="nav-dot" />
                💬 Chat
              </Link>
              <Link href="/tarefas" className="nav-item">
                <span className="nav-dot" />
                ☑️ Tarefas
              </Link>
              {/* 📅 Visitas (agenda portada do V1): lista da semana agrupada
                  por dia, status colorido. Aparece pra todos — o copiloto
                  escopa por corretora via X-Painel-User. */}
              <Link href="/visitas" className="nav-item">
                <span className="nav-dot" />
                📅 Visitas
              </Link>
              {/* 📊 Visão (dashboard gerencial): funil da equipe, KPIs, Meta Ads,
                  tendência. Aparece pra todos — o copiloto filtra o escopo por
                  corretora (sem investimento/comparativo pra corretora comum). */}
              <Link href="/visao" className="nav-item">
                <span className="nav-dot" />
                📊 Visão
              </Link>
              {/* 🏠 Captação: o imóvel de DONO que chega por WhatsApp e antes
                  morria na conversa ("elas não têm onde cadastrar", Dona
                  12/09). Aparece pra todos — o copiloto escopa por corretora,
                  e aqui o escopo é de PROPRIEDADE, não de atendimento: a
                  comissão nasce do corretora_id, então ninguém edita captação
                  de outra, nem a Pessoa12 cobrindo a linha da Pessoa7. */}
              <Link href="/imoveis" className="nav-item">
                <span className="nav-dot" />
                🏠 Captação
              </Link>
              {/* 🏗️ Catálogo da planilha CONTROLE EMPREENDIMENTOS (só leitura,
                  fonte viva no Sheets via Mac). Aparece pra todos — corretora
                  usa pra consultar carteira e pegar material do Drive. */}
              <Link href="/empreendimentos" className="nav-item">
                <span className="nav-dot" />
                🏗️ Empreendimentos
              </Link>
              {/* 📸 Posts: fila de posts sociais da corretora (brief → copy →
                  imagem → aprovação dupla → publica IG). Aparece pra todos —
                  o copiloto escopa por corretora; admin vê filtro geral. */}
              <Link href="/posts" className="nav-item">
                <span className="nav-dot" />
                📸 Posts
              </Link>
              {/* 🎓 Aprendiz: modo sombra da IA — placar de autonomia + "bateu
                  ou não" (rascunho da IA × resposta real). O copiloto escopa
                  por corretora via X-Painel-User. */}
              <Link href="/aprendiz" className="nav-item">
                <span className="nav-dot" />
                🎓 Aprendiz
              </Link>
              {/* ✍️ Sugestões: REMOVIDA em 05/08 (tela + rotas). Era a fila de
                  aprovação humana do follow-up com IA.

                  Motivo, medido antes de tirar: 51 itens, todos de um único dia
                  (29/07), e das 3 aprovadas ZERO chegou no lead — a peça pedia
                  aprovação pra mensagem com hora marcada pra sair, e quando
                  alguém aprovava o toque já tinha passado.

                  Se voltar, tem que ser com outro desenho: aprovar o MOLDE
                  antes, não cada mensagem. Os dados (shadow_cadencia) e o
                  gerador manual continuam no copiloto. */}
              {viewer.isAdmin && (
                <Link href="/automacoes" className="nav-item">
                  <span className="nav-dot" />
                  ⚙️ Automações
                </Link>
              )}
              {viewer.isAdmin && (
                <Link href="/templates" className="nav-item">
                  <span className="nav-dot" />
                  📄 Templates
                </Link>
              )}
              {viewer.isAdmin && (
                <Link href="/gastos" className="nav-item">
                  <span className="nav-dot" />
                  💸 Gastos
                </Link>
              )}
              {viewer.isAdmin && (
                <Link href="/ranking" className="nav-item">
                  <span className="nav-dot" />
                  🏆 Ranking
                </Link>
              )}
              {viewer.isAdmin && (
                <Link href="/historico" className="nav-item">
                  <span className="nav-dot" />
                  📈 Histórico
                </Link>
              )}
            </nav>
            {/* Só aparece pra corretora e só quando dá pra ativar — o
                componente se esconde sozinho no resto dos casos. */}
            <AtivarAviso />
            <div className="sidebar-foot">
              <div className="viewer-chip" title="conta ativa neste navegador">
                <span className="viewer-label">conta</span>
                <span className="viewer-name">
                  {viewer.user}
                  {viewer.isAdmin ? " · admin" : ""}
                </span>
              </div>
              <a href="/sair" className="btn-sair">
                Sair / trocar conta
              </a>
              <div className="construcao">
                Novos módulos em construção.
                <br />
                Um de cada vez, com a Dona.
              </div>
            </div>
          </aside>
          <main className="main">{children}</main>
          {/* Assistente em qualquer módulo — FAB global; no /chat ela já tem botão próprio */}
          <AssistenteFab />
        </div>
      </body>
    </html>
  );
}
