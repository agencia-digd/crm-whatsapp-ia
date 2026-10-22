# deploy/ — o que roda fora do diretório do projeto

Versionado aqui porque senão só existe no servidor.

| Arquivo | Destino | O que faz |
|---|---|---|
| `transcricao-watch` | `/usr/local/bin/` | Avisa a Dona se áudio de lead voltar a ficar mudo |
| `systemd/copiloto-transcricao-backfill.{service,timer}` | `/etc/systemd/system/` | Transcreve em gotas, a cada 15 min |
| `systemd/copiloto-transcricao-watch.{service,timer}` | idem | Check a cada 20 min |

O script do tick (`scripts/transcricao_backfill_tick.sh`) já vive no projeto.

```bash
install -m755 deploy/transcricao-watch /usr/local/bin/
install -m644 deploy/systemd/* /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now copiloto-transcricao-backfill.timer copiloto-transcricao-watch.timer
```

## Por que os dois existem

Áudio de lead ficava mudo e ninguém via. Até 25/08/2026 a transcrição só rodava
dentro do Inspector, 1x por dia, teto de 50 — com ~130 áudios/dia chegando. A
fila crescia 80 por dia até somar 8.308, metade da base.

- O **backfill em gotas** faz o trabalho: lote pequeno, 2 workers, mais recentes
  primeiro (mídia da Cloud expira). Correr estoura o rate limit da Meta — foi o
  que aconteceu no dia em que isso foi escrito.
- O **watch** garante que a gente saiba: olha o RESULTADO (áudio recente segue
  mudo?), não o processo. Timer verde não prova transcrição acontecendo.
