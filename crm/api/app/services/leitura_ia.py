"""Acha negociação avançada LENDO A CONVERSA com IA (Haiku 5.5).

Substitui a lista de palavras do `sinal_negocio.procurar` nos avisos: a IA lê as
últimas mensagens de cada conversa ativa e diz se o cliente está FECHANDO
(documentação entregue, crédito em análise/aprovado, cartório, sinal, reserva,
assinatura, data marcada com banco) ou só interessado (preço, pedido de visita,
simulação). Na amostra de 22/10 o vocabulário acusou 5 conversas que eram só
simulação; a IA derrubou as 5.

Só lê e aponta: não move card, não manda mensagem ao cliente. Cada chamada vai
para `llm_chamadas` (custo/latência) pelo provider da Assistente.
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import structlog
from sqlalchemy import text as sql_text

from app.db.session import SessionLocal
from app.services import sinal_negocio as sn

log = structlog.get_logger()

MAX_MSGS = 40          # últimas mensagens de cada conversa
JANELA_LEITURA = 14    # dias de histórico considerados na leitura
PARALELO = 4

_SQL_ATIVAS = """
SELECT right(regexp_replace(coalesce(contato_telefone,''), '\\D','','g'), 8) AS suf,
       max(contato_telefone) AS telefone,
       max(contato_nome) AS nome_wpp,
       max(corretora_id) AS corretora_id,
       max(timestamp_evolution) AS ultima
  FROM mensagens_whatsapp
 WHERE tenant_id = 1
   AND timestamp_evolution > now() - make_interval(hours => :horas)
   AND contato_telefone IS NOT NULL
   AND coalesce(tipo_mensagem,'') <> 'template'
   AND instance <> 'pessoa_evolution'
 GROUP BY 1
HAVING length(right(regexp_replace(coalesce(max(contato_telefone),''), '\\D','','g'), 8)) = 8
"""
_SQL_MSGS = """
SELECT from_corretora, coalesce(conteudo_texto, transcricao, '') AS t
  FROM mensagens_whatsapp
 WHERE tenant_id = 1
   AND right(regexp_replace(coalesce(contato_telefone,''), '\\D','','g'), 8) = :suf
   AND timestamp_evolution > now() - make_interval(days => :dias)
   AND coalesce(tipo_mensagem,'') <> 'template'
   AND instance <> 'pessoa_evolution'
 ORDER BY timestamp_evolution DESC LIMIT :n
"""
_SQL_CARD = """
SELECT l.nome, ps.nome AS etapa
  FROM leads l LEFT JOIN pipeline_stages ps ON ps.id = l.stage_id
 WHERE right(regexp_replace(coalesce(l.telefone,''), '\\D','','g'), 8) = :suf
 LIMIT 1
"""

SISTEMA = (
    "Você lê uma conversa de WhatsApp entre uma corretora e um cliente de imobiliária. "
    "Diga se o cliente está FECHANDO negócio de imóvel agora (documentação entregue, crédito em análise "
    "ou aprovado, cartório, sinal, reserva, assinatura, data marcada com banco), diferente de só ter "
    "interesse (perguntar preço, pedir visita, pedir simulação, dizer que vai pensar). "
    "Para dono que quer vender: combinar preço de anúncio ou mandar fotos ainda não é fechamento; "
    "assinar a autorização/contrato de venda ou receber proposta de comprador é. "
    "Se a conversa não é sobre compra, venda ou locação de imóvel (equipe, fornecedor, banco pessoal, "
    "assunto de outro serviço), fechando é false. Responda só JSON."
)
SCHEMA = {
    "type": "object",
    "properties": {
        "fechando": {"type": "boolean"},
        "confianca": {"type": "string", "enum": ["alta", "media", "baixa"]},
        "motivo": {"type": "string"},
        "trecho": {"type": "string"},
    },
    "required": ["fechando", "confianca", "motivo", "trecho"],
}


def _provider():
    from app.config import get_llm_provider_assistente

    return get_llm_provider_assistente()


def ler_conversa(provider: Any, mensagens: list[tuple[bool, str]]) -> dict | None:
    """`mensagens` em ordem cronológica: (from_corretora, texto). None se a IA falhar."""
    conv = "\n".join(
        f"{'CORRETORA' if corretora else 'CLIENTE'}: {t[:300]}" for corretora, t in mensagens if t.strip()
    )
    if not conv:
        return None
    try:
        txt, _ = provider.complete(SISTEMA, conv, SCHEMA)
        r = json.loads(txt) if isinstance(txt, str) else txt
        return r if isinstance(r, dict) else None
    except Exception as exc:  # noqa: BLE001 — uma conversa que falha não derruba as outras
        log.warning("leitura_ia_falhou", erro=str(exc)[:120])
        return None


def _uma(provider: Any, a: dict) -> sn.Sinal | None:
    suf = a["suf"]
    with SessionLocal() as s:
        msgs = s.execute(sql_text(_SQL_MSGS), {"suf": suf, "dias": JANELA_LEITURA, "n": MAX_MSGS}).all()
        card = s.execute(sql_text(_SQL_CARD), {"suf": suf}).first()
    etapa = card[1] if card else None
    # Sem card não há o que subir (e é onde caem os assuntos de fora: CPF, assinador, banco pessoal).
    if card is None or etapa in sn.ETAPAS_OK or len(msgs) < 3:
        return None
    r = ler_conversa(provider, [(bool(m[0]), m[1] or "") for m in reversed(msgs)])
    if not r or not r.get("fechando") or r.get("confianca") == "baixa":
        return None
    return sn.Sinal(
        telefone=a["telefone"],
        nome=(card[0] if card and card[0] else a["nome_wpp"]),
        corretora_id=a["corretora_id"],
        etapa=etapa,
        # confiança alta vira "forte" (vai pro aviso); média fica "medio" (só no painel)
        forca="forte" if r.get("confianca") == "alta" else "medio",
        trechos=[(r.get("trecho") or r.get("motivo") or "")[:110]],
        ultima_msg_em=a["ultima"],
    )


def procurar(*, horas: int = 24, provider: Any = None) -> list[sn.Sinal]:
    """Lê as conversas com mensagem nas últimas `horas` e devolve as que estão fechando."""
    prov = provider or _provider()
    with SessionLocal() as s:
        ativas = [dict(r) for r in s.execute(sql_text(_SQL_ATIVAS), {"horas": horas}).mappings().all()]
    ativas = [a for a in ativas if not sn._e_da_casa(a["suf"])]
    with ThreadPoolExecutor(max_workers=PARALELO) as ex:
        lidas = list(ex.map(lambda a: _uma(prov, a), ativas))
    achados = [x for x in lidas if x]
    achados.sort(key=lambda a: (a.forca != "forte", -(a.ultima_msg_em.timestamp() if a.ultima_msg_em else 0)))
    log.info("leitura_ia_procurou", conversas=len(ativas), achados=len(achados),
             fortes=sum(1 for a in achados if a.forca == "forte"))
    return achados
