"""
Contrato de ingestão ÚNICO da conversa do lead para o Inspector.

Antes desta função, classifier (Haiku) e analyzer (Sonnet) montavam o texto da
conversa por caminhos DIVERGENTES (achados I1/I2 da REVISAO-03-INSPECTOR):
  - classifier: últimas 15 msgs, SEM timestamps, max_chars=300
  - analyzer:   TODAS as msgs, com timestamps BRT, max_chars=500

Resultado: as duas camadas viam inputs diferentes (áudio transcrito visível pra uma
e "expirado" pra outra), degradando a qualidade e permitindo contradição entre
`classifier_reason` e o resumo do analyzer no mesmo JSON.

`render_conversa()` produz UMA representação canônica, usada por AMBOS:
  - mensagens em ORDEM cronológica (como vêm do conversation_reader);
  - transcrição de áudio HIDRATADA (texto real entre aspas, não placeholder);
  - HSM/template coalescido (texto hidratado quando presente);
  - timestamps em BRT (America/Sao_Paulo);
  - marcação de quem falou (LEAD / CORRETORA).

Formato de cada linha: `[YYYY-MM-DD HH:MM] ROLE: conteúdo`.
Cabeçalho fixo `(horários em Brasília / BRT)` na primeira linha.

A representação é uma STRING estável e testável isolada. `render_conversa()` é a
fonte da verdade do texto — o classifier e o analyzer NÃO devem remontar a conversa.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

# Timestamps do Evolution são UTC. Exibir SEMPRE em Brasília pro LLM não rotular
# horário errado (bug 07/07/2026: análise narrava UTC como se fosse local).
_SP_TZ = ZoneInfo("America/Sao_Paulo")

_HEADER = "(horários em Brasília / BRT)"
_EMPTY = "[sem mensagens]"


def _at_brt(at_iso: str) -> str:
    """ISO do Evolution (UTC/tz-aware) → 'YYYY-MM-DD HH:MM' em horário de Brasília.

    Fallback pro comportamento antigo se o parse falhar, pra nunca quebrar o render.
    """
    if not at_iso:
        return "??"
    try:
        dt = datetime.fromisoformat(at_iso)
    except ValueError:
        return at_iso[:16].replace("T", " ")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_SP_TZ).strftime("%Y-%m-%d %H:%M")


def render_message(m: dict[str, Any], max_chars: int = 500) -> str:
    """Formata UMA mensagem para o contexto do LLM.

    Hidrata:
      - áudio → transcrição real entre aspas (placeholders só quando não há texto);
      - imagem/vídeo → legenda quando existe, senão marcador;
      - documento → legenda quando existe;
      - HSM/template e texto → o texto coalescido (`conteudo_texto` já hidratado
        pela ingestão); vazio vira `[sem texto]`.
    """
    msg_type = m.get("type", "text")
    raw_text = m.get("text") or ""

    if msg_type == "audio":
        transcricao = m.get("transcricao") or ""
        status = m.get("transcricao_status") or ""
        if status == "concluida" and transcricao:
            return f'[áudio]: "{transcricao[:max_chars - 10]}"'
        if status in ("pendente", "em_progresso"):
            return "[áudio em processamento]"
        return "[áudio não disponível]"

    if msg_type in ("image", "video"):
        label = "imagem" if msg_type == "image" else "vídeo"
        if raw_text:
            return f'[{label}]: "{raw_text[:max_chars - 12]}"'
        return f"[{label} sem legenda]"

    if msg_type == "document":
        return "[documento]" + (f': "{raw_text[:max_chars - 14]}"' if raw_text else "")

    if msg_type == "other":
        return "[outro]"

    return raw_text[:max_chars] or "[sem texto]"


def render_conversa(
    mensagens: list[dict[str, Any]],
    max_chars: int = 500,
) -> str:
    """Representação canônica da conversa do lead — fonte única pra classifier e analyzer.

    Args:
        mensagens: lista normalizada do conversation_reader, em ordem cronológica.
            Cada item: {"from": "lead"|"corretora", "text", "at", "type",
                        "transcricao"?, "transcricao_status"?}.
        max_chars: truncamento por mensagem (default 500, igual ao analyzer legado).

    Returns:
        String com cabeçalho BRT + uma linha por mensagem:
        `[YYYY-MM-DD HH:MM] ROLE: conteúdo`. `[sem mensagens]` se a lista for vazia.
    """
    if not mensagens:
        return _EMPTY
    lines = [_HEADER]
    for m in mensagens:
        role = "CORRETORA" if m.get("from") == "corretora" else "LEAD"
        at = _at_brt(m.get("at") or "")
        lines.append(f"[{at}] {role}: {render_message(m, max_chars=max_chars)}")
    return "\n".join(lines)
