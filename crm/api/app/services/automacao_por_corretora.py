"""Interruptor POR CORRETORA do follow-up e da nutrição (06/10/2026).

## Por que existe

A Dona pediu, pra cada automação, "se tá ativo o da Pessoa7, o da Pessoa11, o da
Pessoa12". Até aqui o follow-up (`cadencia_ativa`) e a nutrição (`nutricao_ativa`)
só tinham o interruptor geral: desligar uma corretora era desligar todas.

## A regra

Duas chaves, um mapa cada, corretora → ligada:

    cadencia_por_corretora   {"1": true, "2": false}   follow-up (cadência)
    nutricao_por_corretora   {"3": false}              marcos de 30 em 30 dias

- **Corretora fora do mapa = ligada.** O padrão é `{}`: publicar não muda o
  envio de ninguém, e corretora nova não nasce desligada por esquecimento.
- **O geral desligado vence.** O tick nem começa com `cadencia_ativa` /
  `nutricao_ativa` desligada; a chave daqui só desliga, nunca liga.
- **De quem é a mensagem:** a corretora do estado da régua
  (`cadencia_estado.corretora_id`). Estado sem dona gravada saiu sempre pela
  linha da Pessoa12, então conta como da Pessoa12 (`CORRETORA_SEM_DONA`) — mesma conta do
  cap por linha da cadência.
- **Falha FECHADA.** O motor relê o banco na hora, antes de CADA envio
  (`pode_enviar`), sem o cache de 30 s. Não consegui ler, ou o valor gravado
  não é um mapa de true/false → aquela mensagem não sai, e o motivo vai pro
  contador do tick (`config_indisponivel`, `chave_ilegivel`). Desligar pela tela
  não pode deixar de valer por causa de um cache ou de uma leitura que falhou.

Toda gravação passa por `config_operacional` (histórico com autor).
"""
from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy import text as sql_text

log = structlog.get_logger()

CHAVE_FOLLOWUP = "cadencia_por_corretora"
CHAVE_NUTRICAO = "nutricao_por_corretora"
CHAVES = (CHAVE_FOLLOWUP, CHAVE_NUTRICAO)

# Estado de régua sem `corretora_id` sai pela linha da Pessoa12 (default histórico do
# sender da cadência) — `cadencia._CORRETORA_CORRETOR`.
CORRETORA_SEM_DONA = 3

MOTIVO_EM_PORTUGUES: dict[str, str] = {
    "corretora_desligada": "desligada pra esta corretora na tela",
    "config_indisponivel": "não consegui ler a configuração — segurando",
    "chave_ilegivel": "o valor gravado não é ligado/desligado — segurando",
}


class Ilegivel(ValueError):
    """O que está gravado não serve. Quem lê segura o envio."""


def dona(corretora_id: int | None) -> int:
    """A corretora a quem a mensagem pertence pra efeito do interruptor."""
    return int(corretora_id) if corretora_id is not None else CORRETORA_SEM_DONA


def validar(valor: Any, corretoras_ativas: set[int]) -> dict[str, bool]:
    """Mapa corretora → true/false. Só corretora ativa; só booleano de verdade
    ("sim", 1, "true" são recusados: valor torto não pode virar "ligado")."""
    if not isinstance(valor, dict):
        raise ValueError("esperado um mapa corretora → ligada (true/false)")
    out: dict[str, bool] = {}
    for k, v in valor.items():
        try:
            cid = int(k)
        except (TypeError, ValueError):
            raise ValueError(f"corretora inválida: {k!r}") from None
        if cid not in corretoras_ativas:
            raise ValueError(f"corretora inexistente ou inativa: {cid}")
        if not isinstance(v, bool):
            raise ValueError(f"corretora {cid}: precisa ser true ou false")
        out[str(cid)] = v
    return out


def interpretar(valor: Any) -> dict[str, bool]:
    """O mapa gravado, conferido. `None` (sem linha) = `{}`. Levanta `Ilegivel`."""
    if valor is None:
        return {}
    if not isinstance(valor, dict):
        raise Ilegivel("não é um mapa")
    out: dict[str, bool] = {}
    for k, v in valor.items():
        if not isinstance(v, bool):
            raise Ilegivel(f"corretora {k}: {v!r} não é true/false")
        out[str(k)] = v
    return out


def ler_fresco(chave: str) -> Any:
    """A linha da chave, lida AGORA do banco (sem cache). `None` = sem linha.
    Levanta `ConfigIndisponivel` se a leitura falhar."""
    from app.db.session import SessionLocal
    from app.services.config_operacional import ConfigIndisponivel

    try:
        with SessionLocal() as s:
            return s.execute(
                sql_text("SELECT valor FROM config_operacional WHERE chave = :k"),
                {"k": chave},
            ).scalar()
    except Exception as e:  # noqa: BLE001
        if _tabela_nao_existe(e):
            # Árvore sem a tabela (SQLite de teste, banco antes da migration):
            # ninguém pode ter desligado nada aqui — é "sem linha", não "não
            # consegui ler". Banco fora do ar continua segurando.
            return None
        raise ConfigIndisponivel(f"{chave}: {str(e)[:160]}") from None


def _tabela_nao_existe(e: Exception) -> bool:
    orig = getattr(e, "orig", None)
    if getattr(orig, "sqlstate", None) == "42P01" or getattr(orig, "pgcode", None) == "42P01":
        return True
    return "no such table: config_operacional" in str(e)


def pode_enviar(chave: str, corretora_id: int | None) -> tuple[bool, str | None]:
    """(pode, motivo). Relê o banco na hora. Nunca levanta."""
    from app.services.config_operacional import ConfigIndisponivel

    try:
        mapa = interpretar(ler_fresco(chave))
    except ConfigIndisponivel as e:
        log.warning("automacao_por_corretora_indisponivel", chave=chave, erro=str(e)[:140])
        return False, "config_indisponivel"
    except Ilegivel as e:
        log.warning("automacao_por_corretora_ilegivel", chave=chave, erro=str(e)[:140])
        return False, "chave_ilegivel"
    if mapa.get(str(dona(corretora_id)), True):
        return True, None
    return False, "corretora_desligada"


def estado(chave: str) -> dict[str, Any]:
    """O que a tela mostra: o mapa como está gravado e se dá pra ler.

    `mapa: None` + `indisponivel` = não consegui ler (≠ todas ligadas);
    `ilegivel` = gravado torto, e o motor está segurando todo mundo.
    """
    from app.services.config_operacional import ConfigIndisponivel

    try:
        bruto = ler_fresco(chave)
    except ConfigIndisponivel as e:
        return {"mapa": None, "indisponivel": True, "ilegivel": False,
                "motivo": str(e)[:160], "personalizado": None}
    try:
        mapa = interpretar(bruto)
    except Ilegivel as e:
        return {"mapa": None, "indisponivel": False, "ilegivel": True,
                "motivo": str(e)[:160], "personalizado": True, "gravado": bruto}
    return {"mapa": mapa, "indisponivel": False, "ilegivel": False,
            "motivo": None, "personalizado": bruto is not None}


def ligada_na_tela(est: dict[str, Any], corretora_id: int) -> bool | None:
    """Ligada (True), desligada (False) ou "não sei" (None) pra uma corretora,
    a partir do `estado()` — sem reler o banco a cada linha da tela."""
    if est.get("mapa") is None:
        return None
    return bool(est["mapa"].get(str(int(corretora_id)), True))
