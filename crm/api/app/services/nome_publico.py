"""O nome da corretora que o CLIENTE lê — fonte única.

Existe porque cada corretora pode ter DOIS nomes, e confundi-los fala errado
com o lead:

  • `corretoras.nome` é a identidade INTERNA — painel, ranking, briefing, o
    cruzamento com o Kommo. Pra Pessoa3, "Pessoa3".
  • `corretoras.nome_publico` é o que vai no TEXTO da mensagem. Pra ela,
    "Pessoa" — é como ela atende, e o verified_name do número dela na Meta é
    "Pessoa Pessoa9 Acme".

Caso que criou isto (18/09/2026): a Kauane recebeu a primeira mensagem pela
linha 2591 dizendo "Aqui é a Pessoa3, da Acme Imóveis". A cliente nunca vai
ouvir esse nome de novo — pra ela, quem atende é a Pessoa.

REGRA: todo texto que o LEAD lê pede `publico()`. Tela, log e relatório
interno continuam no `corretoras.nome` — lá "Pessoa3" é o certo, e trocar
confundiria a própria equipe.
"""
from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy import text as sql_text

log = structlog.get_logger()


def primeiro_nome(nome: str | None) -> str | None:
    """Primeiro nome de um nome completo, ou None se não der pra extrair."""
    partes = (nome or "").strip().split()
    return partes[0] if partes else None


def publico_de(nome: str | None, nome_publico: str | None) -> str | None:
    """Resolve o nome público a partir dos dois campos já lidos.

    Versão sem banco — pra quem já tem a linha da corretora em mãos. Preferir
    esta nos caminhos quentes, em vez de abrir sessão só pra isto.
    """
    return primeiro_nome(nome_publico) or primeiro_nome(nome)


def publico(session: Any, corretora_id: int | None) -> str | None:
    """Nome público da corretora, ou None se não der pra saber.

    None NÃO é erro: quem chama decide o fallback (hoje os textos caem em "a
    equipe"). Devolver um nome errado é pior que não dizer nome — foi
    exatamente o estrago do caso Kauane.
    """
    if corretora_id is None:
        return None
    try:
        row = session.execute(
            sql_text("SELECT nome, nome_publico FROM corretoras WHERE id = :cid"),
            {"cid": int(corretora_id)},
        ).mappings().first()
    except Exception:  # noqa: BLE001 — cadastro ilegível não derruba o envio
        log.warning("nome_publico_indisponivel", corretora_id=corretora_id,
                    exc_info=True)
        return None
    if row is None:
        return None
    return publico_de(row["nome"], row["nome_publico"])
