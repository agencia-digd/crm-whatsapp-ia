"""
Reanálise sob demanda de UM lead pelo Inspector (botão 🔁 no raio-X do chat V2).

Promove a receita validada em 24/07 (script one-off do lead 10000016) a código
de produção: reusa exatamente as peças do pipeline diário — ler_thread_cloud
(linha Pessoa12) / EvolutionTool (demais linhas), classify_lead, analyze_lead e os
guardrails/_save_analysis de pipeline.py — só que escopado num lead, na hora.

NÃO duplica lógica de análise: classifier e analyzer são os mesmos do cron
diário. A única diferença é a origem do disparo (entrou_por="reanalise_manual")
e a ausência de cruzamento Kommo (tarefas/etapa ficam neutras — o cron das
19h45 continua sendo a fonte disso).
"""
from datetime import datetime, timezone
from typing import Any

import structlog

from app.inspector import pipeline as P
from app.inspector.analyzer import analyze_lead, dias_sem_msg_lead
from app.inspector.classifier import classify_lead

log = structlog.get_logger()

# Janela de conversa: 180 dias — mesmo teto do painel_conversa (hours_back<=4320).
REANALISE_HOURS_BACK = 4320


def _resolver_contexto(lead_id: str) -> dict[str, Any]:
    """Resolve telefone + corretora + instância via painel (fonte única).

    Import local: painel.py importa este módulo no endpoint — evita ciclo.
    Propaga as HTTPException claras do painel (404 lead_nao_encontrado,
    422 lead_sem_telefone/lead_sem_corretora/corretora_sem_instancia_evolution).
    """
    from app.api.painel import _lead_conversa_contexto

    return _lead_conversa_contexto(lead_id)


def _buscar_conversa(ctx: dict[str, Any]) -> list[dict[str, Any]]:
    """Thread do lead pela linha da corretora dona — igual ao painel_conversa.

    Linha da Pessoa12 (acme_corretora_a) roda via Cloud API → lê do Postgres.
    Demais corretoras seguem no Evolution (pull).
    """
    if ctx["instance"] == "acme_corretora_a":
        from app.api.cloud_corretora_a import ler_thread_cloud

        return ler_thread_cloud(ctx["telefone"], hours_back=REANALISE_HOURS_BACK)

    from app.tools.evolution import EvolutionTool

    return EvolutionTool().get_conversation(
        instance=ctx["instance"],
        lead_phone=ctx["telefone"],
        hours_back=REANALISE_HOURS_BACK,
    )


def reanalisar_lead(
    lead_id: str,
    classifier_provider: Any | None = None,
    analyzer_provider: Any | None = None,
) -> dict[str, Any]:
    """Refaz a análise do Inspector pra UM lead, agora, com a conversa atual.

    Retorna o raio-X novo:
        {temperatura, status, dias_sem_msg_lead, resumo, gap, sugestao, analyzed_at}
    ou erro amigável:
        {"erro": "lead_sem_cadastro"}  — lead_id "tel:..." (fora do funil; sem
                                          linha em leads_snapshot → não dá pra
                                          gravar em lead_analyses)
        {"erro": "sem_conversa"}       — nenhuma mensagem na janela
        {"erro": "analise_falhou"}     — falha de LLM/infra (detalhe só no log)

    providers opcionais são injeção pra teste (mesmo padrão de classify_lead/
    analyze_lead); em produção ficam None e cada camada resolve o seu.
    """
    if lead_id.startswith("tel:"):
        log.info("reanalise_lead_sem_cadastro", lead_id=lead_id)
        return {"erro": "lead_sem_cadastro"}

    ctx = _resolver_contexto(lead_id)  # HTTPException 404/422 propaga pro caller
    log.info(
        "reanalise_inicio",
        lead_id=lead_id,
        corretora=ctx.get("corretora_nome"),
        instance=ctx.get("instance"),
    )

    try:
        messages = _buscar_conversa(ctx)
    except Exception as e:  # noqa: BLE001 — Evolution/DB fora não vaza stack
        log.warning(
            "reanalise_conversa_falha",
            lead_id=lead_id,
            instance=ctx.get("instance"),
            erro=str(e)[:160],
        )
        return {"erro": "analise_falhou"}

    if not messages:
        log.info("reanalise_sem_conversa", lead_id=lead_id)
        return {"erro": "sem_conversa"}

    lead = {
        "lead_id": lead_id,
        "nome": ctx.get("lead_nome") or "",
        "name": ctx.get("lead_nome") or "",
        "etapa_nome": ctx.get("etapa_kommo") or "?",
    }

    try:
        classification = classify_lead(lead, messages, provider=classifier_provider)
        log.info(
            "reanalise_classifier",
            lead_id=lead_id,
            status=classification.status,
            temperatura=classification.temperatura,
            deep=classification.deserves_deep_analysis,
        )

        raw_at, raw_de = P._last_message_meta(messages)
        ultima_at, ultima_de, _ = P._validar_ultima_msg_meta(raw_at, raw_de, messages)
        dias_silencio = dias_sem_msg_lead(messages)

        # analyze_lead já retorna None sozinho se não há msg do lead.
        deep = None
        if classification.deserves_deep_analysis:
            deep = analyze_lead(
                lead,
                messages,
                ctx.get("corretora_nome") or "",
                provider=analyzer_provider,
                dias_silencio_lead=dias_silencio,
            )

        temperatura, capped = P._cap_temperatura_staleness(
            classification.temperatura, dias_silencio
        )
        suspeita = P._detectar_analise_suspeita(
            classification.reason, messages, deep
        )

        analyzed_at = datetime.now(timezone.utc)
        output = P.LeadAnalysisOutput(
            lead_id=lead_id,
            lead_name=ctx.get("lead_nome") or "",
            corretora=ctx.get("corretora_nome") or "",
            corretora_id=int(ctx["corretora_id"]),
            entrou_por="reanalise_manual",
            analysis_mode="conversa_whatsapp",
            status=classification.status,
            temperatura=temperatura,
            ultima_msg_at=ultima_at,
            ultima_msg_de=ultima_de,
            conversa_resumo=deep.conversa_resumo if deep else None,
            o_que_corretora_disse=deep.o_que_corretora_disse if deep else None,
            gap_identificado=deep.gap_identificado if deep else None,
            abordagem_sugerida=deep.abordagem_sugerida if deep else None,
            tarefas_kommo_atrasadas=[],
            etapa_kommo_desatualizada=False,
            skipped_reason=None,
            classifier_reason=classification.reason,
            tokens_classifier=classification.tokens_used,
            tokens_analyzer=deep.tokens_used if deep else 0,
            dias_sem_msg_lead=dias_silencio,
            temperatura_capped_staleness=capped,
            citacao_verificada=deep.citacao_verificada if deep else True,
            respiro_valido=deep.respiro_valido if deep else True,
            analise_suspeita=suspeita,
        )
        P._save_analysis(output)
    except Exception as e:  # noqa: BLE001 — erro de LLM/save vira erro amigável
        log.error("reanalise_falhou", lead_id=lead_id, erro=str(e)[:200])
        return {"erro": "analise_falhou"}

    log.info(
        "reanalise_ok",
        lead_id=lead_id,
        temperatura=temperatura,
        status=classification.status,
        deep=bool(deep),
        capped=capped,
        suspeita=suspeita,
    )
    return {
        "temperatura": temperatura,
        "status": classification.status,
        "dias_sem_msg_lead": dias_silencio,
        "resumo": deep.conversa_resumo if deep else None,
        "gap": deep.gap_identificado if deep else None,
        "sugestao": deep.abordagem_sugerida if deep else None,
        "analyzed_at": analyzed_at.isoformat(),
    }
