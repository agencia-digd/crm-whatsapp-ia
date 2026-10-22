/** @type {import('next').NextConfig} */
// distDir por env: o deploy builda em `.next-build` e só depois troca pelo
// `.next` em uso (ver scripts/deploy.sh).
//
// 🔴 APAGÃO 29/07: `next build` direto no diretório em produção APAGA o `.next`
// que o `next start` está servindo. O processo morre, o systemd reinicia, morre
// de novo ("Could not find a production build") — crash loop até o build acabar.
// O painel ficou fora do ar pras corretoras no meio do expediente. Nunca mais
// buildar em cima do diretório vivo.
const nextConfig = {
  distDir: process.env.NEXT_DIST_DIR || ".next",
};

export default nextConfig;
