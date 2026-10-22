"""Juiz do Aprendiz: o rascunho da IA faz a MESMA AÇÃO que a corretora fez?

O trigrama (comparador_sombra) mede se as palavras se parecem — e duas respostas
certas quase nunca se parecem. O juiz lê as duas e responde por intenção/ação
(perguntar, marcar visita, mandar material, encerrar...). Roda no `claude -p` da
assinatura (Haiku), sem API paga, com TETO de chamadas por rodada e medição
(chamadas, segundos, tokens estimados) impressa no fim.

Nunca envia nada: só lê a decisão e grava `juiz_*` em shadow_decisions.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

import structlog
from sqlalchemy import text

from app.llm.cli_provider import e_limite_de_cota

log = structlog.get_logger()

TETO_PADRAO = 15
VEREDITOS_JUIZ = ("bateu", "parcial", "nao_bateu")

_SYSTEM = (
    "Você avalia um assistente de WhatsApp de uma imobiliária. Recebe a última mensagem do "
    "lead, o RASCUNHO que a IA escreveria e a RESPOSTA REAL que a corretora mandou. "
    "Julgue pela INTENÇÃO e AÇÃO, nunca pelas palavras: as duas fazem a mesma coisa "
    "(perguntar o mesmo tipo de coisa, propor a mesma próxima ação, dar a mesma informação, "
    "encerrar ou não)?\n"
    "- bateu: mesma ação e nada que a corretora faria diferente de importante.\n"
    "- parcial: ação parecida, mas falta ou sobra algo relevante (ex.: IA pergunta, corretora "
    "pergunta e já propõe visita).\n"
    "- nao_bateu: ação diferente ou o rascunho atrapalharia (promete algo errado, encerra "
    "conversa viva, ignora o pedido)."
)

_SCHEMA = {
    "type": "object",
    "properties": {
        "veredito": {"type": "string", "enum": list(VEREDITOS_JUIZ)},
        "motivo": {"type": "string", "description": "uma frase, em português"},
    },
    "required": ["veredito", "motivo"],
}


@dataclass
class MedicaoJuiz:
    teto: int
    julgados: int = 0
    falhas: int = 0
    pulados_por_teto: int = 0
    parou_por_cota: bool = False
    segundos: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    vereditos: dict[str, int] = field(default_factory=lambda: {v: 0 for v in VEREDITOS_JUIZ})

    def linha(self) -> str:
        return (
            f"JUIZ: {self.julgados} julgados (teto {self.teto}), {self.falhas} falha(s), "
            f"{self.pulados_por_teto} ficaram pra próxima rodada, {self.segundos:.1f} s, "
            f"~{self.tokens_in} tokens in / ~{self.tokens_out} out (estimado chars/4), "
            f"vereditos {self.vereditos}" + (" — PAROU: cota do claude -p" if self.parou_por_cota else "")
        )


def _system_com_correcoes(corretora_id: int | None) -> str:
    """_SYSTEM + correções humanas recentes. Com a flag OFF devolve o próprio _SYSTEM."""
    from app.services.aprendiz_correcoes import bloco_para_prompt

    bloco = bloco_para_prompt(corretora_id)
    return _SYSTEM + bloco if bloco else _SYSTEM


def julgar(provider: Any, ultima_msg_lead: str | None, rascunho: str, resposta_real: str,
           corretora_id: int | None = None) -> tuple[dict, Any]:
    """Uma chamada ao juiz. Retorna ({veredito, motivo}, usage). Levanta se a saída for inválida."""
    user = (
        f"MENSAGEM DO LEAD:\n{ultima_msg_lead or '(não disponível)'}\n\n"
        f"RASCUNHO DA IA:\n{rascunho}\n\n"
        f"RESPOSTA REAL DA CORRETORA:\n{resposta_real}"
    )
    raw, usage = provider.complete(_system_com_correcoes(corretora_id), user, _SCHEMA)
    data = json.loads(raw)
    if not isinstance(data, dict) or data.get("veredito") not in VEREDITOS_JUIZ:
        raise ValueError(f"veredito inválido do juiz: {str(raw)[:120]}")
    return {"veredito": data["veredito"], "motivo": str(data.get("motivo") or "")[:400]}, usage


def julgar_pendentes(
    pares: list[dict[str, Any]],
    session_factory: Callable[[], Any],
    provider: Any,
    *,
    modelo: str,
    teto: int = TETO_PADRAO,
) -> MedicaoJuiz:
    """Julga os pares que têm rascunho + resposta real e ainda não têm veredito do juiz
    nem marcação humana. Mais recentes primeiro, no máximo `teto` por rodada."""
    med = MedicaoJuiz(teto=teto)
    fila = [
        p for p in pares
        if p.get("resposta_real") and p.get("rascunho")
        and p.get("juiz_veredito") is None and p.get("acerto") is None
        and p.get("decisao_id") is not None
    ]
    for p in fila:
        if med.julgados + med.falhas >= teto:
            med.pulados_por_teto += 1
            continue
        t0 = time.monotonic()
        try:
            res, usage = julgar(provider, p.get("ultima_msg_lead"), p["rascunho"], p["resposta_real"],
                                  p.get("corretora_id"))
        except Exception as exc:  # noqa: BLE001
            med.segundos += time.monotonic() - t0
            if e_limite_de_cota(str(exc)):
                med.parou_por_cota = True
                log.warning("juiz_parado_limite_cota", motivo=str(exc)[:200])
                break
            med.falhas += 1
            log.warning("juiz_falhou", decisao_id=p["decisao_id"], erro=str(exc)[:200])
            continue
        med.segundos += time.monotonic() - t0
        med.julgados += 1
        med.tokens_in += getattr(usage, "input_tokens", 0)
        med.tokens_out += getattr(usage, "output_tokens", 0)
        med.vereditos[res["veredito"]] += 1
        with session_factory() as s:
            s.execute(
                text(
                    "UPDATE shadow_decisions SET juiz_veredito = :v, juiz_motivo = :m, "
                    "juiz_modelo = :md, juiz_em = :em WHERE id = :i"
                ),
                {"v": res["veredito"], "m": res["motivo"], "md": modelo,
                 "em": datetime.now(timezone.utc), "i": p["decisao_id"]},
            )
            s.commit()
    # pares que sobraram na fila além do que foi processado ao parar por cota
    if med.parou_por_cota:
        med.pulados_por_teto = max(0, len(fila) - med.julgados - med.falhas)
    return med
