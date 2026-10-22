// 🔑 Login do painel — substitui a caixa de usuário/senha do navegador.
//
// SEM JAVASCRIPT, de propósito: é um <form> HTML puro que posta em
// /api/auth/login. Se dependesse do bundle do Next, /_next/* teria que ser
// público pra esta página carregar — e aí o código do painel ficaria aberto.
// Assim o único endereço sem sessão é esta tela.
//
// Cuidados de celular (o motivo do link mágico ter nascido): usuário com
// `autoCapitalize="none"` (o teclado do iPhone maiusculiza a primeira letra e
// o login vira "Pessoa10"), `autoCorrect` desligado, e input de 16px — abaixo
// disso o iOS dá zoom ao focar e a tela "pula".

import s from "./login.module.css";

export const dynamic = "force-dynamic";

export default function Login({
  searchParams,
}: {
  searchParams: { erro?: string; de?: string };
}) {
  const erro = searchParams?.erro === "1";
  // só caminho interno vira destino pós-login (evita virar redirecionador
  // aberto pra site de phishing)
  const de =
    searchParams?.de?.startsWith("/") && !searchParams.de.startsWith("//")
      ? searchParams.de
      : "";

  return (
    <div className={s.wrap}>
      <form className={s.card} method="post" action="/api/auth/login">
        <p className={s.marca}>Acme Imóveis</p>
        <h1 className={s.titulo}>Entrar no painel</h1>

        {erro && <div className={s.erro}>Usuário ou senha inválidos.</div>}

        <label className={s.campo}>
          <span className={s.rotulo}>Usuário</span>
          <input
            className={s.input}
            name="usuario"
            autoCapitalize="none"
            autoCorrect="off"
            autoComplete="username"
            spellCheck={false}
            required
          />
        </label>

        <label className={s.campo}>
          <span className={s.rotulo}>Senha</span>
          <input
            className={s.input}
            type="password"
            name="senha"
            autoComplete="current-password"
            required
          />
        </label>

        {de && <input type="hidden" name="de" value={de} />}

        <button className={s.btn} type="submit">
          Entrar
        </button>

        <p className={s.nota}>
          Você fica conectada neste aparelho até clicar em “Sair / trocar
          conta”.
        </p>
      </form>
    </div>
  );
}
