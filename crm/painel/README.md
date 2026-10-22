This is a [Next.js](https://nextjs.org) project bootstrapped with [`create-next-app`](https://nextjs.org/docs/app/api-reference/cli/create-next-app).

## Getting Started

First, run the development server:

```bash
npm run dev
# or
yarn dev
# or
pnpm dev
# or
bun dev
```

Open [http://localhost:3000](http://localhost:3000) with your browser to see the result.

You can start editing the page by modifying `app/page.tsx`. The page auto-updates as you edit the file.

This project uses [`next/font`](https://nextjs.org/docs/app/building-your-application/optimizing/fonts) to automatically optimize and load [Geist](https://vercel.com/font), a new font family for Vercel.

## Learn More

To learn more about Next.js, take a look at the following resources:

- [Next.js Documentation](https://nextjs.org/docs) - learn about Next.js features and API.
- [Learn Next.js](https://nextjs.org/learn) - an interactive Next.js tutorial.

You can check out [the Next.js GitHub repository](https://github.com/vercel/next.js) - your feedback and contributions are welcome!

## Deploy on Vercel

The easiest way to deploy your Next.js app is to use the [Vercel Platform](https://vercel.com/new?utm_medium=default-template&filter=next.js&utm_source=create-next-app&utm_campaign=create-next-app-readme) from the creators of Next.js.

Check out our [Next.js deployment documentation](https://nextjs.org/docs/app/building-your-application/deploying) for more details.

## PWA · app instalado no celular da corretora (06/08/2026)

O painel virou app de tela de início. Sem loja, sem build: deploy do Next =
atualização no celular.

**Gotcha que custou tempo — arquivos do PWA precisam ficar FORA do Basic Auth.**
O Safari busca ícone e manifest **sem mandar o cookie da sessão**. Com tudo
atrás de `auth_basic`, ele levava 401, desistia, e o iOS desenhava o fallback
dele: quadrado cinza com a inicial do site. Parece cache de ícone e não é —
é permissão. Renomear o arquivo não resolve.

Fix em `/etc/nginx/sites-enabled/acme-painel`: `location ~ ^/(icon-*.png|
apple-touch-icon*.png|manifest.webmanifest|favicon.ico|sw.js)$` com
`auth_basic off`. Nada aí é dado da imobiliária. O resto segue protegido
(conferir: `/crm` sem senha tem que dar 401).

Outros pontos:
- **iPhone só entrega push com o app na tela de início.** No Safari a permissão
  nem é oferecida — por isso `AtivarAviso.tsx` detecta e ensina o passo.
- **apple-touch-icon é obrigatório**: o iOS não lê os ícones do manifest.
  Também tem cópia em `/apple-touch-icon.png` (raiz), onde ele procura por
  convenção.
- **Ícone versionado (`-v2`)**: o iOS guarda o ícone pela URL.
- A logo tem margem branca própria — recortar o conteúdo real antes de compor,
  senão a casinha sai minúscula. Ver o script no histórico do projeto.
- Entrada por link (`/entrar?k=`) em vez de senha: Basic Auth em PWA no iPhone
  fica pedindo login e atrapalha.

## Login · sessão em cookie (11/08/2026)

O painel saiu do **HTTP Basic Auth** e passou a usar **sessão em cookie
assinado**. O gatilho foi o "Sair / trocar conta" não funcionar: Basic Auth não
tem logout — o navegador guarda a senha e reenvia sozinho, então o botão só
recarregava a página (no access log: 6 cliques em `/sair`, todos com
`$remote_user=pessoa`).

**O nginx continua sendo o porteiro**, via `auth_request` → `/api/auth/verificar`.
A decisão NÃO foi movida pro middleware do Next de propósito: erro de `matcher`
ali deixaria o painel aberto, e aqui tem dado da imobiliária. Com `auth_request`,
app fora do ar = ninguém entra (fail-closed).

Peças:
- `lib/sessao.ts` — cookie `corretor_sess` = `payload.hmac_sha256`, HttpOnly/Secure/Lax.
- `lib/senha.ts` — confere no **mesmo** `/etc/nginx/.htpasswd-imob` de antes
  (ninguém trocou de senha). Via binário `htpasswd -vi` porque o arquivo mistura
  bcrypt e apr1, e senha por stdin não aparece no `ps`.
- `app/login/` — `<form>` HTML **sem JavaScript**. Assim `/_next/*` continua
  fechado: se a tela dependesse do bundle, o código do painel viraria público.
- `app/entrar/` — link mágico, agora emitindo sessão. **Mesmos tokens de antes**:
  os links que já estão no WhatsApp das corretoras seguem valendo. Ficam em
  `/etc/agencia/acme-painel-links.json` (era um `map` do nginx).
- `app/sair/` — apaga o cookie. Fim.

**Não expira** (decisão da Dona): a corretora vive no celular e relogar toda
semana foi o atrito que criou o link mágico. Em troca existe **revogação**:

    node scripts/revogar-sessao.mjs corretor     # derruba só ela, na hora

Cada usuário tem uma "geração" em `/etc/agencia/acme-painel-geracoes.json` e o cookie
carrega a dele. Pra derrubar **todo mundo**, troque `CORRETOR_SESSION_SECRET` em
`/etc/agencia/acme-painel-auth.env` e reinicie o serviço.

Segredo injetado por `/etc/systemd/system/acme-painel.service.d/auth.conf`
(EnvironmentFile) — fora do repo, que é sincronizado com o desktop.

Login normaliza o usuário pra minúsculo. O htpasswd tem `Pessoa10`/`CORRETOR`/`Pessoa11`/
`CORRETOR` só pra contornar a maiúscula automática do teclado; a duplicata deixou de
ser necessária (mantida por enquanto, sem uso).

Conferir depois de mexer (sem sessão tem que dar 302 pro /login):

    /crm /chat /tarefas /api/tarefas /_next/static/chunks/*.js  → 302
    /login /manifest.webmanifest                                → 200
    /_auth                                                      → 404 (interno)

Ataques testados e barrados: `X-Auth-User` injetado pelo cliente (o nginx
sobrescreve), `corretor_as=dona` de corretora (o middleware só REBAIXA, ver auditoria
de 29/07), payload do cookie adulterado com assinatura antiga, cookie sem
assinatura, e o `painel_key` do sistema antigo.

### Histórico: o workaround do Basic Auth (removido)

Antes da migração, `/sair` tentava fazer o navegador esquecer a senha batendo
num endpoint com credencial inválida de propósito. Funcionava no Chrome e no
Firefox, não no Safari. Foi removido junto com o Basic Auth — sem desafio 401,
o navegador não tem mais pra quem mandar credencial.

