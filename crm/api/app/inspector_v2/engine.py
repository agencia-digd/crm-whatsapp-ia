"""Engine map/reduce integral sobre corpus previamente redigido."""

from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata
from datetime import datetime, timezone
from typing import Any

import structlog
from pydantic import ValidationError

from app.inspector_v2.chunking import MessageChunk, chunk_messages, unique_processed_count
from app.inspector_v2.corpus import parse_messages
from app.inspector_v2.models import (
    AnalysisPayload,
    AnalysisResult,
    DeterministicChecks,
    ObjectiveStatus,
    ProviderResponse,
    RedactedLead,
    Usage,
    VisitaStatus,
)
from app.inspector_v2.preparation import sensitive_matches
from app.inspector_v2.providers import ProviderError, ShadowProvider
from app.inspector_v2.storage import ShadowStore

log = structlog.get_logger()

SCHEMA_VERSION = "2.3.1"
PROMPT_VERSION = "acme-painel-orca-2026-08-25.3"

_SYSTEM = """Você é o Inspector V2 da Acme em modo sombra.
Analise SOMENTE as mensagens REDIGIDAS fornecidas. Não use ferramentas, memória externa ou pesquisa.
Cada linha começa com [#NNNNNN], o índice global da mensagem na jornada cronológica.
Mensagens [template] são automação, não fala humana da corretora; não atribua gap pessoal por elas.
Texto marcado como áudio transcrito é fala real. Áudio sem transcrição é conteúdo desconhecido.
Separe intenção máxima histórica de temperatura atual. Interesse antigo não prova prontidão atual.
Toda evidência e citação deve copiar um trecho literal curto e informar mensagem_id.
Não invente valor, visita, objeção, resposta ou intenção. Use sem_evidencia/lista vazia quando faltar base.
Gap da corretora só existe quando sustentado por comportamento observado; caso contrário use null.
Pergunta só fica sem resposta se nenhuma mensagem posterior responder.
A próxima ação é uma ANOTAÇÃO para revisão humana: ela pode recomendar enviar WhatsApp, mover no CRM ou outra ação futura.
Marque proxima_acao_requer_revisao_humana=true e acao_externa_executada=false. Nunca alegue que executou a recomendação.
A recomendação deve respeitar pausa/opt-out e nunca pedir/expor telefone, CPF/CNPJ, e-mail, CEP, endereço ou nascimento.
Não execute alteração em CRM, Kommo, WhatsApp, banco, briefing ou tarefa automática.
Retorne somente JSON válido no schema pedido."""


def _normalized(value: str) -> str:
    ascii_value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", ascii_value.lower())).strip()


def _add_usage(total: Usage, addition: Usage) -> Usage:
    def add_optional(left: int | None, right: int | None) -> int | None:
        if left is None and right is None:
            return None
        return (left or 0) + (right or 0)

    cost = None
    if total.cost_usd is not None or addition.cost_usd is not None:
        cost = (total.cost_usd or 0.0) + (addition.cost_usd or 0.0)
    return Usage(
        input_tokens=add_optional(total.input_tokens, addition.input_tokens),
        output_tokens=add_optional(total.output_tokens, addition.output_tokens),
        total_tokens=add_optional(total.total_tokens, addition.total_tokens),
        cost_usd=cost,
    )


def _parse_payload(response: ProviderResponse) -> AnalysisPayload:
    try:
        return AnalysisPayload.model_validate_json(response.content)
    except ValidationError as exc:
        raise ProviderError(f"JSON não respeita schema funcional: {exc}", transient=True) from exc


def _run_key(
    run_id: str,
    lead: RedactedLead,
    provider: ShadowProvider,
    cutoff_at: datetime | None,
    max_chunk_characters: int,
    overlap_messages: int,
) -> tuple[str, str]:
    input_json = json.dumps(lead.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    input_fingerprint = hashlib.sha256(input_json.encode("utf-8")).hexdigest()
    material = "\0".join(
        [
            run_id,
            provider.model,
            json.dumps(provider.runtime_configuration(), sort_keys=True, separators=(",", ":")),
            lead.lead_alias,
            cutoff_at.isoformat() if cutoff_at else "",
            SCHEMA_VERSION,
            PROMPT_VERSION,
            str(max_chunk_characters),
            str(overlap_messages),
            input_fingerprint,
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest(), input_fingerprint


def _citation_pairs(payload: AnalysisPayload) -> list[tuple[int, str]]:
    pairs = [(citation.mensagem_id, citation.trecho) for citation in payload.citacoes]
    for collection in (
        payload.evidencias_intencao,
        payload.financeiro,
        payload.objecoes,
        payload.perguntas_sem_resposta,
    ):
        pairs.extend((evidence.mensagem_id, evidence.citacao) for evidence in collection)
    return pairs


def _grounding_counts(payload: AnalysisPayload, messages: list[str]) -> tuple[int, int]:
    pairs = _citation_pairs(payload)
    valid = sum(
        1 <= message_id <= len(messages)
        and bool(_normalized(quote))
        and _normalized(quote) in _normalized(messages[message_id - 1])
        for message_id, quote in pairs
    )
    return valid, len(pairs)


def _grounding_failure_ids(
    payload: AnalysisPayload,
    messages: list[str],
    *,
    require_minimum: bool,
) -> tuple[list[int], bool]:
    pairs = _citation_pairs(payload)
    invalid = [
        message_id
        for message_id, quote in pairs
        if not (
            1 <= message_id <= len(messages)
            and bool(_normalized(quote))
            and _normalized(quote) in _normalized(messages[message_id - 1])
        )
    ]
    return invalid, require_minimum and len(pairs) < 3


def _objective_status(messages: list[Any], cutoff_at: datetime | None) -> ObjectiveStatus:
    timestamps = [message.timestamp for message in messages if message.timestamp is not None]
    last = messages[-1] if messages else None
    silence: int | None = None
    if timestamps:
        reference = cutoff_at or datetime.now().replace(tzinfo=None)
        silence = max((reference - max(timestamps)).days, 0)
    return ObjectiveStatus(
        ultima_mensagem_de=last.sender if last is not None else None,
        primeira_mensagem_at=min(timestamps) if timestamps else None,
        ultima_mensagem_at=max(timestamps) if timestamps else None,
        silencio_dias=silence,
    )


def _claims_executed_action(value: str) -> bool:
    normalized = _normalized(value)
    target = r"(?:crm|kommo|banco|whatsapp|briefing|tarefa automatica)"
    past_action = re.compile(
        r"\b(?:ja\s+)?(?:enviei|mandei|movi|atualizei|alterei|criei|escrevi|executei)\b"
        rf".{{0,50}}\b{target}\b"
    )
    passive_action = re.compile(
        rf"(?:\b{target}\b.{{0,50}}\b(?:enviado|movido|atualizado|alterado|criado|executado)\b|"
        rf"\b(?:enviado|movido|atualizado|alterado|criado|executado)\b.{{0,50}}\b{target}\b)"
    )
    explicit_completion = re.compile(
        r"\b(?:acao|tarefa|recomendacao)\b.{0,25}\b(?:executada|realizada|concluida)\b"
    )
    return bool(
        past_action.search(normalized)
        or passive_action.search(normalized)
        or explicit_completion.search(normalized)
    )


def _checks(
    payload: AnalysisPayload,
    messages: list[str],
    parsed: list[Any],
    status: ObjectiveStatus,
    cutoff_at: datetime | None,
) -> DeterministicChecks:
    valid_citations, total_citations = _grounding_counts(payload, messages)
    citations_ok = total_citations >= 3 and valid_citations == total_citations
    visit_citations = [citation for citation in payload.citacoes if "visita" in _normalized(citation.sustenta)]
    visit_ok = payload.visita in (VisitaStatus.SEM_EVIDENCIA, VisitaStatus.NAO_PROPOSTA) or bool(visit_citations)
    late_threshold = max(1, len(messages) - max(5, len(messages) // 10))
    current_citations = [
        citation for citation in payload.citacoes if "temperatura" in _normalized(citation.sustenta)
    ]
    last_message_ok = status.ultima_mensagem_de is None or any(
        citation.mensagem_id >= late_threshold for citation in current_citations
    )
    recommendation_safe = not sensitive_matches(payload.proxima_acao)
    no_operational_action = (
        payload.proxima_acao_requer_revisao_humana
        and not payload.acao_externa_executada
        and not _claims_executed_action(payload.proxima_acao)
    )
    lookahead = sum(
        message.timestamp is not None and cutoff_at is not None and message.timestamp > cutoff_at
        for message in parsed
    )
    critical = not citations_ok or not recommendation_safe or not no_operational_action or lookahead > 0
    return DeterministicChecks(
        json_valido=True,
        citacoes_existentes=citations_ok,
        citacoes_validas=valid_citations,
        citacoes_total=total_citations,
        coerencia_visita=visit_ok,
        coerencia_ultima_mensagem=last_message_ok,
        recomendacao_sem_dado_sensivel=recommendation_safe,
        nenhuma_acao_operacional=no_operational_action,
        lookahead_messages=lookahead,
        falha_critica=critical,
    )


class InspectorV2Engine:
    def __init__(
        self,
        provider: ShadowProvider,
        store: ShadowStore,
        *,
        max_chunk_characters: int = 32_000,
        max_reduce_characters: int = 48_000,
        overlap_messages: int = 2,
        retries: int = 1,
    ) -> None:
        if retries != 1:
            raise ValueError("piloto permite exatamente uma correção de formato")
        self.provider = provider
        self.store = store
        self.max_chunk_characters = max_chunk_characters
        self.max_reduce_characters = max_reduce_characters
        self.overlap_messages = overlap_messages
        self.retries = retries

    def analyze(
        self,
        lead: RedactedLead,
        run_id: str,
        *,
        cutoff_at: datetime | None = None,
        force: bool = False,
    ) -> AnalysisResult:
        run_key, input_fingerprint = _run_key(
            run_id,
            lead,
            self.provider,
            cutoff_at,
            self.max_chunk_characters,
            self.overlap_messages,
        )
        if not force:
            cached = self.store.load_cached_result(run_id, lead.lead_alias, run_key)
            if cached is not None:
                return cached.model_copy(update={"cache_hit": True})

        parsed = parse_messages(lead.mensagens)
        ordered_messages = [message.raw for message in parsed]
        chunks = chunk_messages(parsed, self.max_chunk_characters, self.overlap_messages)
        processed = unique_processed_count(chunks)
        if processed != len(parsed):
            raise ValueError("cobertura de chunking diferente de 100%")

        started = time.monotonic()
        usage = Usage()
        max_parse_attempts = 1
        partials: list[AnalysisPayload] = []
        for chunk in chunks:
            partial, partial_usage, parse_attempts = self._analyze_chunk(
                chunk, len(parsed), ordered_messages
            )
            partials.append(partial)
            usage = _add_usage(usage, partial_usage)
            max_parse_attempts = max(max_parse_attempts, parse_attempts)
        payload, reduce_usage, parse_attempts = self._reduce_all(
            partials, len(parsed), ordered_messages
        )
        usage = _add_usage(usage, reduce_usage)
        max_parse_attempts = max(max_parse_attempts, parse_attempts)

        status = _objective_status(parsed, cutoff_at)
        checks = _checks(payload, ordered_messages, parsed, status, cutoff_at)
        warnings: list[str] = []
        unparseable = sum(message.timestamp is None for message in parsed)
        if unparseable:
            warnings.append(f"mensagens_sem_timestamp:{unparseable}")
        automated = sum(message.automated for message in parsed)
        if automated:
            warnings.append(f"mensagens_automatizadas:{automated}")
        opaque_audio = sum(
            message.message_type == "audio" and "sem conteúdo disponível" in message.raw.lower()
            for message in parsed
        )
        if opaque_audio:
            warnings.append(f"audios_sem_conteudo:{opaque_audio}")
        if not checks.coerencia_visita:
            warnings.append("visita_sem_citacao_especifica")
        if not checks.coerencia_ultima_mensagem:
            warnings.append("temperatura_sem_evidencia_recente")
        if checks.falha_critica:
            warnings.append("falha_critica")

        result = AnalysisResult(
            schema_version=SCHEMA_VERSION,
            prompt_version=PROMPT_VERSION,
            run_id=run_id,
            run_key=run_key,
            provider=self.provider.name,
            model=self.provider.model,
            runtime=self.provider.runtime_configuration(),
            lead_alias=lead.lead_alias,
            corretora_alias=lead.corretora_alias,
            cutoff_at=cutoff_at,
            mensagens_entrada=len(parsed),
            mensagens_processadas=processed,
            chunks=[chunk.descriptor for chunk in chunks],
            cobertura=processed / len(parsed),
            status_objetivo=status,
            payload=payload,
            checks=checks,
            redactions_count=lead.redactions_count,
            warnings=warnings,
            parse_attempts=max_parse_attempts,
            usage=usage,
            latency_ms=int((time.monotonic() - started) * 1000),
            input_fingerprint=input_fingerprint,
            completed_at=datetime.now(timezone.utc),
        )
        self.store.save_result(run_id, result)
        log.info(
            "inspector_v2_shadow_completed",
            lead_alias=lead.lead_alias,
            provider=self.provider.name,
            messages=len(parsed),
            chunks=len(chunks),
            critical_failure=checks.falha_critica,
            latency_ms=result.latency_ms,
        )
        return result

    def _complete(
        self,
        user: str,
        *,
        grounding_messages: list[str] | None = None,
        require_minimum_grounding: bool = False,
    ) -> tuple[AnalysisPayload, Usage, int]:
        total_usage = Usage()
        last_error: Exception | None = None
        prompt = user
        response_schema = AnalysisPayload.model_json_schema()
        if require_minimum_grounding:
            properties = response_schema.get("properties")
            if isinstance(properties, dict):
                citations_schema = properties.get("citacoes")
                if isinstance(citations_schema, dict):
                    citations_schema["minItems"] = 3
        for attempt in range(1, 3):
            try:
                response = self.provider.complete(_SYSTEM, prompt, response_schema)
            except ProviderError as exc:
                last_error = exc
                if attempt == 2 or not exc.transient:
                    exc.attempts = attempt
                    raise
                # Transporte transitório repete exatamente a mesma entrada.
                prompt = user
                continue
            total_usage = _add_usage(total_usage, response.usage)
            try:
                payload = _parse_payload(response)
            except (ProviderError, ValidationError, json.JSONDecodeError) as exc:
                last_error = exc
                if attempt == 2:
                    if isinstance(exc, ProviderError):
                        exc.attempts = attempt
                    raise
                prompt = (
                    user
                    + "\n\nCORREÇÃO ÚNICA DE FORMATO: a resposta anterior não validou. "
                    "Gere novamente todo o objeto, sem markdown e estritamente no JSON Schema."
                )
                continue
            if grounding_messages is not None:
                invalid_ids, below_minimum = _grounding_failure_ids(
                    payload,
                    grounding_messages,
                    require_minimum=require_minimum_grounding,
                )
                if (invalid_ids or below_minimum) and attempt == 1:
                    ids = ",".join(str(value) for value in sorted(set(invalid_ids))) or "nenhum"
                    minimum_note = "; total abaixo de 3" if below_minimum else ""
                    prompt = (
                        user
                        + "\n\nCORREÇÃO ÚNICA DE GROUNDING: o validador literal rejeitou a resposta "
                        f"(mensagem_id inválido/não literal: {ids}{minimum_note}). "
                        "Regenere todo o objeto. Cada trecho/citação deve ser uma substring CONTÍGUA e literal "
                        "da mensagem indicada; não omita palavras internas, não parafraseie e não altere o "
                        "mensagem_id. Emita ao menos 3 citações/evidências no resultado final quando houver base."
                    )
                    continue
            return payload, total_usage, attempt
        assert last_error is not None
        raise last_error

    def _analyze_chunk(
        self,
        chunk: MessageChunk,
        total_messages: int,
        grounding_messages: list[str],
    ) -> tuple[AnalysisPayload, Usage, int]:
        user = (
            f"MAP parcial: chunk {chunk.descriptor.ordinal}, mensagens "
            f"{chunk.descriptor.first_message}..{chunk.descriptor.last_message} de {total_messages}; "
            f"overlap anterior={chunk.descriptor.overlap_before}.\n"
            "Extraia somente eventos presentes aqui. Preserve índices globais e datas. "
            "Ignore duplicatas do overlap no reduce. Como este mapa é parcial, não presuma fim da jornada.\n\n"
            + chunk.render()
        )
        return self._complete(user, grounding_messages=grounding_messages)

    def _reduce_all(
        self,
        partials: list[AnalysisPayload],
        total_messages: int,
        grounding_messages: list[str],
    ) -> tuple[AnalysisPayload, Usage, int]:
        if not partials:
            raise ValueError("não há mapas para consolidar")
        if len(partials) == 1:
            rendered = partials[0].model_dump_json()
            final_payload, usage, attempts = self._complete(
                f"REDUCE FINAL obrigatório de uma jornada com {total_messages} mensagens e um único mapa. "
                "Agora trate a jornada como encerrada no último evento, determine estado atual, intenção máxima, "
                "pendências, gap sustentável e próxima ação. Preserve todas as citações grounded.\n\nMAPA:\n"
                + rendered,
                grounding_messages=grounding_messages,
                require_minimum_grounding=True,
            )
            return final_payload, usage, attempts
        current = partials
        total_usage = Usage()
        max_attempts = 1
        level = 1
        while len(current) > 1:
            batches = self._batch_payloads(current)
            if len(batches) == len(current):
                batches = [current]
            next_level: list[AnalysisPayload] = []
            for batch_index, batch in enumerate(batches, start=1):
                rendered = json.dumps(
                    [payload.model_dump(mode="json") for payload in batch],
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                is_final = len(batches) == 1
                user = (
                    f"REDUCE nível {level}, lote {batch_index}/{len(batches)}; total da jornada={total_messages}. "
                    "Consolide em ordem, remova duplicatas do overlap e preserve citações/índices. "
                    "Intenção máxima usa o maior sinal histórico; temperatura atual usa o estado mais recente. "
                    "Pergunta só fica pendente se mapas posteriores não responderem. "
                    + ("Este é o reduce final." if is_final else "Este é um reduce intermediário.")
                    + "\n\nMAPAS:\n"
                    + rendered
                )
                reduced, usage, attempts = self._complete(
                    user,
                    grounding_messages=grounding_messages,
                    require_minimum_grounding=is_final,
                )
                next_level.append(reduced)
                total_usage = _add_usage(total_usage, usage)
                max_attempts = max(max_attempts, attempts)
            current = next_level
            level += 1
        return current[0], total_usage, max_attempts

    def _batch_payloads(self, payloads: list[AnalysisPayload]) -> list[list[AnalysisPayload]]:
        batches: list[list[AnalysisPayload]] = []
        current: list[AnalysisPayload] = []
        current_size = 2
        for payload in payloads:
            size = len(payload.model_dump_json()) + 1
            if current and current_size + size > self.max_reduce_characters:
                batches.append(current)
                current = []
                current_size = 2
            current.append(payload)
            current_size += size
        if current:
            batches.append(current)
        return batches
