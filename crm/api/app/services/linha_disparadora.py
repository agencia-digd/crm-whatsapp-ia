"""Linha disparadora — POR QUAL NÚMERO o remarketing sai.

## Por que existe

Até 05/10/2026 a linha era constante no código (`SLUG_CORRETOR = "corretor"`). Ao mesmo
tempo o cadastro de números do api-disparos já tinha a marca de pra que serve
cada linha (`phone_numbers.papel`: `disparo` / `atendimento` / `pausado`) — e o
remarketing da Acme ignorava. Marcar outro número como disparador, ou pausar
a linha da Corretor, não mudava por onde a mensagem saía.

Agora as duas pontas estão ligadas: o tick PERGUNTA ao api-disparos quais
números do cliente estão marcados `disparo` e sai por esse. A marca é a
autorização.

## A regra dura: nunca por adivinhação

Sem linha resolvida o tick **não envia** e diz o motivo. Não existe queda pra
"a linha de sempre":

| motivo (`motivo_parada`)                | o que aconteceu |
|-----------------------------------------|-----------------|
| `sem_linha_disparadora`                 | nenhum número do cliente está marcado `disparo` |
| `linha_disparadora_ambigua`             | mais de um marcado — escolher um seria palpite |
| `linha_disparadora_indisponivel`        | não deu pra perguntar (api fora, chave ausente, resposta torta) |
| `linha_disparadora_fora_do_registro`    | o número marcado não é uma linha que o copiloto opera (sem token aqui) |
| `linha_disparadora_sem_resposta_tratada`| o copiloto opera a linha, mas a resposta do lead nela não tira ninguém da roda |

O último existe porque quem responde ao remarketing sai da roda e é atendido
pelo hook do inbound, e esse hook hoje só está ligado na linha da Corretor
(`api/cloud_corretora.py`). Disparar por outra linha deixaria quem respondeu em
"Disparado" pra receber de novo em 30 dias. Pra habilitar outra linha: ligar o
`carrossel_corretor.processar_inbound` no inbound dela e acrescentar o slug em
`LINHAS_COM_RESPOSTA_TRATADA` — nessa ordem.

## Sem cache

Uma consulta local por tick (a cada 3 min). Cache aqui seria o jeito de uma
linha pausada às 10:00 continuar disparando até o cache vencer.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import structlog

log = structlog.get_logger()

PAPEL_DISPARO = "disparo"

# Slugs de linha em que a resposta do lead é tratada pelo carrossel (sai da
# roda, opt-out, atendimento). Ver o docstring antes de acrescentar.
LINHAS_COM_RESPOSTA_TRATADA: frozenset[str] = frozenset({"corretor"})

TIMEOUT_S = 5.0


@dataclass
class Resolucao:
    """Resultado de `resolver`. `linha` None ⇒ não envia; `motivo` diz por quê."""

    linha: Any = None               # cloud_lines.CloudLine
    motivo: str | None = None
    detalhe: str | None = None
    # O que o cadastro respondeu (sem segredo): pra tela e pro log.
    numeros: list[dict[str, Any]] = field(default_factory=list)


class Indisponivel(RuntimeError):
    """Não deu pra perguntar ao api-disparos. A mensagem nunca leva a chave."""


def _chave() -> str:
    from app.config import settings
    from app.services import cloud_lines as cl

    env = cl._parse_env_file(settings.api_disparos_env_file)
    chave = (env.get(settings.api_disparos_key_var) or "").strip()
    if not chave:
        raise Indisponivel(
            f"chave do api-disparos ausente ({settings.api_disparos_key_var} em "
            f"{settings.api_disparos_env_file})"
        )
    return chave


def _buscar_numeros() -> list[dict[str, Any]]:
    """Números do cliente e o papel de cada um, direto do cadastro. Levanta
    `Indisponivel` em qualquer falha — quem chama decide (e decide segurar)."""
    import httpx

    from app.config import settings

    # Suíte de testes não sai da máquina de teste: quem testa este módulo troca
    # esta função inteira (mesma trava de `saude_linha._buscar`).
    if os.environ.get("COPILOTO_UNDER_PYTEST") == "1":
        raise Indisponivel("consulta ao api-disparos bloqueada na suíte de testes")

    chave = _chave()
    try:
        r = httpx.get(
            f"{settings.api_disparos_url.rstrip('/')}/numbers/papel",
            headers={"X-API-Key": chave},
            timeout=TIMEOUT_S,
        )
    except httpx.HTTPError as e:
        raise Indisponivel(f"api-disparos não respondeu: {type(e).__name__}") from None
    if r.status_code != 200:
        raise Indisponivel(f"api-disparos devolveu HTTP {r.status_code}")
    try:
        numeros = r.json()["numeros"]
        assert isinstance(numeros, list)
    except Exception:  # noqa: BLE001
        raise Indisponivel("resposta do api-disparos fora do formato") from None
    return numeros


def trocar_papel(phone_number_id: str, papel: str, autor: str,
                 pausar_os_outros: bool = False) -> dict[str, Any]:
    """Pede ao api-disparos pra trocar o papel de um número do cliente.

    Devolve `{"status": <http>, "corpo": <json>}` — a validação (papel aceito,
    número do cliente) é DELE, e o motivo da recusa volta pra tela como veio.
    Levanta `Indisponivel` se não deu pra chamar.
    """
    import httpx

    from app.config import settings

    if os.environ.get("COPILOTO_UNDER_PYTEST") == "1":
        raise Indisponivel("chamada ao api-disparos bloqueada na suíte de testes")

    chave = _chave()
    try:
        r = httpx.put(
            f"{settings.api_disparos_url.rstrip('/')}/numbers/"
            f"{phone_number_id}/papel",
            headers={"X-API-Key": chave},
            json={"papel": papel, "autor": autor,
                  "pausar_os_outros": bool(pausar_os_outros)},
            timeout=TIMEOUT_S,
        )
    except httpx.HTTPError as e:
        raise Indisponivel(f"api-disparos não respondeu: {type(e).__name__}") from None
    try:
        corpo = r.json()
    except Exception:  # noqa: BLE001
        corpo = {"detail": f"HTTP {r.status_code}"}
    return {"status": r.status_code, "corpo": corpo}


def historico_papel(limite: int = 20) -> list[dict[str, Any]]:
    """Quem trocou o papel de qual número (`phone_papel_historico`, no
    api-disparos). Levanta `Indisponivel` — a tela diz "não consegui ler"."""
    import httpx

    from app.config import settings

    if os.environ.get("COPILOTO_UNDER_PYTEST") == "1":
        raise Indisponivel("consulta ao api-disparos bloqueada na suíte de testes")
    chave = _chave()
    try:
        r = httpx.get(
            f"{settings.api_disparos_url.rstrip('/')}/numbers/papel/historico",
            headers={"X-API-Key": chave}, params={"limite": int(limite)},
            timeout=TIMEOUT_S,
        )
    except httpx.HTTPError as e:
        raise Indisponivel(f"api-disparos não respondeu: {type(e).__name__}") from None
    if r.status_code != 200:
        raise Indisponivel(f"api-disparos devolveu HTTP {r.status_code}")
    try:
        itens = r.json()["historico"]
        assert isinstance(itens, list)
    except Exception:  # noqa: BLE001
        raise Indisponivel("resposta do api-disparos fora do formato") from None
    return itens


def _publico(n: dict[str, Any]) -> dict[str, Any]:
    """Só o que pode ir pra tela e pro log."""
    from app.services import cloud_lines as cl

    pid = str(n.get("phone_number_id") or "")
    linha = cl.line_by_phone_id(pid)
    return {
        "phone_number_id": pid,
        "telefone": n.get("phone"),
        "nome": n.get("display_name"),
        "papel": n.get("papel"),
        "slug": linha.slug if linha else None,
        # O copiloto consegue disparar o remarketing por este número?
        "operavel": bool(linha and linha.slug in LINHAS_COM_RESPOSTA_TRATADA),
    }


def resolver() -> Resolucao:
    """A linha por onde o remarketing sai AGORA. Nunca levanta."""
    from app.services import cloud_lines as cl

    try:
        brutos = _buscar_numeros()
    except Indisponivel as e:
        return Resolucao(motivo="linha_disparadora_indisponivel", detalhe=str(e)[:160])
    except Exception as e:  # noqa: BLE001 — qualquer surpresa também segura
        return Resolucao(motivo="linha_disparadora_indisponivel",
                         detalhe=f"erro inesperado: {type(e).__name__}")

    numeros = [_publico(n) for n in brutos if isinstance(n, dict)]
    marcados = [n for n in numeros if n["papel"] == PAPEL_DISPARO]
    if not marcados:
        papeis = ", ".join(
            f"{n['slug'] or n['telefone'] or n['phone_number_id']}: {n['papel']}"
            for n in numeros
        ) or "nenhum número cadastrado"
        return Resolucao(motivo="sem_linha_disparadora", numeros=numeros,
                         detalhe=f"nenhum número marcado como disparo ({papeis})"[:200])
    if len(marcados) > 1:
        quais = ", ".join(str(n["slug"] or n["telefone"] or n["phone_number_id"])
                          for n in marcados)
        return Resolucao(motivo="linha_disparadora_ambigua", numeros=numeros,
                         detalhe=f"{len(marcados)} números marcados como disparo: {quais}"[:200])

    unico = marcados[0]
    linha = cl.line_by_phone_id(unico["phone_number_id"])
    if linha is None:
        return Resolucao(
            motivo="linha_disparadora_fora_do_registro", numeros=numeros,
            detalhe=(f"o número marcado ({unico['telefone'] or unico['phone_number_id']}) "
                     "não é uma linha que este sistema opera"),
        )
    if linha.slug not in LINHAS_COM_RESPOSTA_TRATADA:
        return Resolucao(
            motivo="linha_disparadora_sem_resposta_tratada", numeros=numeros,
            detalhe=(f"a linha '{linha.slug}' está marcada como disparo, mas a "
                     "resposta do lead nela não é tratada pelo remarketing"),
        )
    return Resolucao(linha=linha, numeros=numeros)


MOTIVO_EM_PORTUGUES: dict[str, str] = {
    "sem_linha_disparadora":
        "nenhum número está marcado como disparador",
    "linha_disparadora_ambigua":
        "há mais de um número marcado como disparador — deixe só um",
    "linha_disparadora_indisponivel":
        "não consegui consultar o cadastro de números — segurando",
    "linha_disparadora_fora_do_registro":
        "o número marcado como disparador não é operado por este sistema",
    "linha_disparadora_sem_resposta_tratada":
        "o número marcado como disparador não tem o tratamento de resposta do remarketing",
}


def resumo() -> dict[str, Any]:
    """O que a tela mostra: os números, o papel de cada um e se há linha."""
    r = resolver()
    return {
        "slug": r.linha.slug if r.linha else None,
        "phone_number_id": r.linha.phone_id if r.linha else None,
        "motivo": r.motivo,
        "motivo_texto": MOTIVO_EM_PORTUGUES.get(r.motivo) if r.motivo else None,
        "detalhe": r.detalhe,
        "numeros": r.numeros,
        # `numeros` vazio com motivo "indisponivel" = não consegui ler, não
        # "não há número". A tela mostra os dois de formas diferentes.
        "indisponivel": r.motivo == "linha_disparadora_indisponivel",
    }
