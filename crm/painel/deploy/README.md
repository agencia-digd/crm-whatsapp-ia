# deploy/ — o que roda fora do diretório do painel

| Arquivo | Destino | O que faz |
|---|---|---|
| `systemd/restart.conf` | `/etc/systemd/system/acme-painel.service.d/` | Faz o painel voltar sozinho se cair |

```bash
install -D -m644 deploy/systemd/restart.conf /etc/systemd/system/acme-painel.service.d/restart.conf
systemctl daemon-reload
```

## Por que existe

Em 26/08/2026 o painel saiu sozinho às 16h10 com **status=0/SUCCESS** — saída
limpa, não falha. O unit tinha `Restart=on-failure`, que por definição **não
reergue quem saiu com sucesso**, então ficou morto sem alarme nenhum.

Quem descobriu foi a Dona, tentando salvar uma anotação na página de auditoria:
o HTML é estático e carregava normal, mas o `/api/` que grava depende do Next.
Abria bonito e só falhava na hora de salvar.

`Restart=always` cobre os dois casos. Servidor web que "termina com sucesso" é
sempre anomalia — ninguém pede pro Next encerrar. Testado derrubando o processo
de propósito: voltou em 5s.

## nginx

`deploy/nginx/acme-painel.conf` é cópia versionada de `/etc/nginx/sites-enabled/acme-painel`.
Editar o arquivo em `/etc` e copiar pra cá — não o contrário (nada aplica isto sozinho).

**A regra que menos parece importante e mais quebrou:** o que é público precisa
estar na lista de exceções ANTES do `location /` com `auth_request`. A tela de
login é pública, mas o CSS dela não estava na lista — então o navegador pedia o
estilo e levava 302 pro próprio login, e a página abria em HTML cru. Parecia
"o painel não abre no celular" e era só estilo faltando.
