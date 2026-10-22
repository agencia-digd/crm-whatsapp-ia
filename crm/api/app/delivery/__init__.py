"""
Camada de ENTREGA — montagem do texto dos briefings (funções puras).

Extraído dos entry-points (Onda 2 · squad Acme):
  - Briefing Dona (Telegram)   ← `app/agents/agente_assistente.py`
  - Briefing corretora (WA)    ← `app/services/briefing_corretora.py`

Os entry-points seguem sendo finos: coletam dados, chamam a formatação daqui,
enviam. Aqui só se monta texto — sem DB, sem envio (exceto o CTA da corretora,
que consulta métrica de adoção com degradação silenciosa, igual ao original).
"""
from app.delivery.corretora_formatter import montar_texto as format_briefing_corretora
from app.delivery.dona_formatter import (
    format_briefing_mini,
    format_briefing_rich,
    format_msg1_metrics,
)


def format_briefing_dona(context: dict, *, dia_fraco: bool = False) -> str:
    """Fachada: Mensagem 1 (métricas) do briefing da Dona.

    Atalho conveniente pra Msg 1. Pra o fluxo completo (mini/rich/cenários) o
    agente usa format_msg1_metrics / format_briefing_mini / format_briefing_rich
    diretamente.
    """
    return format_msg1_metrics(context, dia_fraco=dia_fraco)


__all__ = [
    "format_briefing_dona",
    "format_briefing_corretora",
    "format_msg1_metrics",
    "format_briefing_mini",
    "format_briefing_rich",
]
