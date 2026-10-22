"""Helper compartilhado pra contagem de tarefas atrasadas no Kommo.

Duas semânticas distintas — escolha qual usar:

  • ``contar_atrasadas_todos_pipelines(uid)``
    Usado pelo briefing matinal (``briefing_corretora``) e pelo painel
    (``api/painel``). Conta TODAS tarefas atrasadas da corretora em
    qualquer pipeline — inclui Nutrição (11244771), Funil Acme
    (10000004), etc. Decisão tomada em 2026-05-13 após observar que
    corretoras têm tarefas em múltiplos pipelines e o número operacional
    relevante é o total.

  • ``contar_atrasadas_pipeline_principal(uid)``
    Usado pela consolidação noturna (``consolidacao_metricas``). Conta
    SÓ no pipeline Acme principal, excluindo leads em status final
    (Venda ganha / perdida). Usado pra histórico/snapshot, onde o número
    inflado por leads finalizados distorce as métricas longitudinais.

Antes da consolidação havia 3 implementações divergentes do mesmo
``_coletar_tarefas_atrasadas`` espalhadas pelo repo — uma em cada lugar.
"""
from __future__ import annotations

import structlog

log = structlog.get_logger()


def contar_atrasadas_todos_pipelines(kommo_user_id: int | None) -> int:
    """TODAS as tarefas atrasadas da corretora — qualquer pipeline.

    Privacy: o filtro Kommo por ``responsible_user_id`` garante que só
    vêm tarefas DESSA corretora.

    Retorna 0 silenciosamente em erro (não levanta).
    """
    if not kommo_user_id:
        return 0
    try:
        from app.tools.kommo import KommoTool
        tool = KommoTool()
        tasks = tool._client.get_tasks(int(kommo_user_id), filter_overdue=True)
        return len(tasks)
    except Exception as e:
        log.error("tarefas_kommo_todos_pipelines_error", error=str(e))
        return 0


def contar_atrasadas_pipeline_principal(kommo_user_id: int) -> int:
    """Tarefas atrasadas no pipeline Acme principal, excluindo finais.

    Semântica usada pela consolidação noturna em ``daily_metrics_corretora``
    pra evitar inflar contagem com leads de Venda ganha/perdida.
    """
    from app.config import settings
    from app.integrations.kommo_client import KommoClient

    client = KommoClient(subdomain=settings.kommo_subdomain, token=settings.kommo_token)
    tasks = client.get_overdue_tasks_in_pipeline(
        user_id=kommo_user_id,
        pipeline_id=settings.kommo_pipeline_id_main,
        exclude_final_statuses=True,
    )
    return len(tasks)
