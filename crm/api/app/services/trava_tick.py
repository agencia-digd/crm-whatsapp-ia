"""
Trava de execução pros motores que MANDAM mensagem.

Todo emissor (cadência, nutrição, mensagem planejada) segue o mesmo padrão
perigoso: *verifica no banco → envia na Meta → só então grava*. Dois ticks
sobrepostos — timer atrasado, chamada manual, cron duplicado — leem o MESMO
estado antes de qualquer um gravar, e o lead recebe duas vezes.

O UNIQUE do banco protege o BANCO, não o WhatsApp: quando ele estoura, a
mensagem já saiu.

Advisory lock de sessão do Postgres, mesmo padrão que o Inspector usa
(app/inspector/pipeline.py). Não-bloqueante: se outro tick do MESMO motor está
rodando, este não roda — o próximo minuto pega o que sobrou.

Este módulo nasceu da auditoria de 29/07: a trava tinha sido posta só na
cadência, e o Grok e o Sol apontaram que `nutricao` e `msg_planejada` seguiam
expostos. Centralizar aqui evita a terceira cópia divergindo — que é o problema
de fundo que os dois diagnosticaram (cada emissor com a sua versão da trava).

Uso:
    from app.services.trava_tick import travar, soltar, OCUPADO, NS_NUTRICAO

    trava = travar(NS_NUTRICAO, tenant_id)
    if trava is OCUPADO:
        return  # outro tick já está rodando
    try:
        ...
    finally:
        soltar(trava, NS_NUTRICAO, tenant_id)
"""
from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy import text as sql_text

log = structlog.get_logger()

# Namespaces por MOTOR. Cada um trava só contra si mesmo: dois ticks de nutrição
# não se atropelam, mas nutrição e cadência ainda podem rodar em paralelo (elas
# operam conjuntos de lead diferentes e já têm o skip `nutricao_owned`).
# Valores distintos do namespace do Inspector (0x42455454, "BETT").
NS_CADENCIA = 0x43414431      # "CAD1"
NS_NUTRICAO = 0x4E555431      # "NUT1"
NS_MSG_PLANEJADA = 0x4D534731  # "MSG1"
NS_CARROSSEL = 0x43525231     # "CRR1" — carrossel da Corretor (remarketing em massa)

# Sentinela: outro tick do mesmo motor já segura a trava.
OCUPADO = object()

# Sentinela: a trava DEU ERRO (conexão, permissão). Diferente de None, que é o
# caso benigno de banco não-Postgres (SQLite dos testes).
#
# A distinção existe desde 02/08/2026: antes, os dois casos viravam None e todo
# motor seguia disparando sem trava — inclusive o carrossel, que manda em massa.
# Quem só precisa não calar (cadência, nutrição) continua tratando os dois
# igual; quem manda em massa trata INDISPONIVEL como "não roda" (ver o tick do
# carrossel). Falhar fechado num motor de 5 mil leads é mais barato que dobrar.
INDISPONIVEL = object()


def travar(namespace: int, tenant_id: int) -> Any:
    """Tenta travar (não bloqueia).

    Devolve:
      - a conexão que segura a trava (sucesso);
      - `OCUPADO`, se outro tick do mesmo motor está rodando;
      - `None`, quando o banco não é Postgres (SQLite dos testes) — benigno;
      - `INDISPONIVEL`, quando a trava falhou de verdade.

    Cabe a CADA motor decidir o que fazer com `INDISPONIVEL`: seguir sem trava
    (histórico, pra não calar follow-up) ou parar (emissor em massa).
    """
    try:
        from app.db.session import engine

        if engine.dialect.name != "postgresql":
            return None
        conn = engine.connect()
        try:
            pegou = conn.execute(
                sql_text("SELECT pg_try_advisory_lock(:ns, :t)"),
                {"ns": int(namespace), "t": int(tenant_id)},
            ).scalar()
        except Exception:
            conn.close()
            raise
        if pegou:
            return conn
        conn.close()
        return OCUPADO
    except Exception as e:  # noqa: BLE001
        log.warning("trava_tick_indisponivel", ns=hex(namespace), erro=str(e)[:140])
        return INDISPONIVEL


def soltar(conn: Any, namespace: int, tenant_id: int) -> None:
    """Libera a trava e devolve a conexão. Best-effort — nunca levanta."""
    if conn is None or conn is OCUPADO or conn is INDISPONIVEL:
        return
    try:
        conn.execute(
            sql_text("SELECT pg_advisory_unlock(:ns, :t)"),
            {"ns": int(namespace), "t": int(tenant_id)},
        )
    except Exception:  # noqa: BLE001
        log.warning("trava_tick_unlock_falhou", ns=hex(namespace), tenant_id=tenant_id)
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
