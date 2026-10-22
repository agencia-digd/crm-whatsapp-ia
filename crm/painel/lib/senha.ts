// Confere usuário/senha contra o MESMO arquivo que o Basic Auth usava
// (/etc/nginx/.htpasswd-imob) — ninguém precisa trocar de senha na migração.
//
// Por que via binário `htpasswd` e não uma lib: o arquivo tem hash bcrypt
// ($2y$) e apr1 (MD5 do Apache, o da pessoa). Uma lib de bcrypt não lê apr1, e
// implementar apr1 na mão é escrever cripto — erro silencioso ali vira falha
// de autenticação. O htpasswd do apache2-utils já sabe os dois.
//
// A senha vai por STDIN (-i), nunca em argv: argumento de processo aparece pra
// qualquer um que rode `ps`.

import { execFile } from "node:child_process";

const ARQUIVO_SENHAS =
  process.env.CORRETOR_HTPASSWD_FILE ?? "/etc/nginx/.htpasswd-imob";

// Usuário é interpolado num comando: limita ao formato real dos logins.
const USUARIO_OK = /^[a-zA-Z0-9._-]{1,32}$/;

export function conferirSenha(user: string, senha: string): Promise<boolean> {
  if (!USUARIO_OK.test(user) || !senha) return Promise.resolve(false);

  return new Promise((resolve) => {
    const p = execFile(
      "htpasswd",
      ["-vi", ARQUIVO_SENHAS, user],
      { timeout: 5000 },
      (erro) => resolve(!erro), // exit 0 = senha confere
    );
    p.stdin?.end(senha);
  });
}
