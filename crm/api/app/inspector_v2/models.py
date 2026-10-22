"""Schemas funcionais e envelope técnico do Inspector V2 sombra."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Intencao(StrEnum):
    SEM_EVIDENCIA = "sem_evidencia"
    BAIXA = "baixa"
    MEDIA = "media"
    ALTA = "alta"


class Temperatura(StrEnum):
    SEM_EVIDENCIA = "sem_evidencia"
    FRIO = "frio"
    MORNO = "morno"
    QUENTE = "quente"


class Confianca(StrEnum):
    BAIXA = "baixa"
    MEDIA = "media"
    ALTA = "alta"


class VisitaStatus(StrEnum):
    SEM_EVIDENCIA = "sem_evidencia"
    NAO_PROPOSTA = "nao_proposta"
    PROPOSTA = "proposta"
    AGENDADA = "agendada"
    REALIZADA = "realizada"


class Evidence(StrictModel):
    mensagem_id: int = Field(ge=1)
    data: str | None = None
    citacao: str = Field(min_length=1)
    leitura: str = Field(min_length=1)


class Citation(StrictModel):
    mensagem_id: int = Field(ge=1)
    trecho: str = Field(min_length=1)
    sustenta: str = Field(min_length=1)


class AnalysisPayload(StrictModel):
    """Schema funcional compatível com os campos da auditoria consolidada."""

    temperatura_atual: Temperatura
    intencao_maxima: Intencao
    resumo_jornada: str = Field(min_length=1)
    evidencias_intencao: list[Evidence] = Field(default_factory=list)
    financeiro: list[Evidence] = Field(default_factory=list)
    objecoes: list[Evidence] = Field(default_factory=list)
    perguntas_sem_resposta: list[Evidence] = Field(default_factory=list)
    visita: VisitaStatus
    gap_corretora: str | None = None
    proxima_acao: str = Field(min_length=1)
    proxima_acao_requer_revisao_humana: bool
    acao_externa_executada: bool
    citacoes: list[Citation] = Field(default_factory=list)
    confianca: Confianca


class Usage(StrictModel):
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    cost_usd: float | None = Field(default=None, ge=0)


class ProviderResponse(StrictModel):
    content: str
    usage: Usage = Field(default_factory=Usage)
    raw_metadata: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


class ChunkDescriptor(StrictModel):
    ordinal: int = Field(ge=1)
    first_message: int = Field(ge=1)
    last_message: int = Field(ge=1)
    message_count: int = Field(ge=1)
    overlap_before: int = Field(default=0, ge=0)
    character_count: int = Field(ge=0)


class ObjectiveStatus(StrictModel):
    ultima_mensagem_de: str | None
    primeira_mensagem_at: datetime | None
    ultima_mensagem_at: datetime | None
    silencio_dias: int | None = Field(default=None, ge=0)


class DeterministicChecks(StrictModel):
    json_valido: bool
    citacoes_existentes: bool
    citacoes_validas: int = Field(ge=0)
    citacoes_total: int = Field(ge=0)
    coerencia_visita: bool
    coerencia_ultima_mensagem: bool
    recomendacao_sem_dado_sensivel: bool
    nenhuma_acao_operacional: bool
    lookahead_messages: int = Field(ge=0)
    falha_critica: bool


class AnalysisResult(StrictModel):
    schema_version: str
    prompt_version: str
    run_id: str
    run_key: str
    provider: str
    model: str
    runtime: dict[str, str | int | bool | None] = Field(default_factory=dict)
    lead_alias: str
    corretora_alias: str
    cutoff_at: datetime | None
    mensagens_entrada: int = Field(ge=1)
    mensagens_processadas: int = Field(ge=1)
    chunks: list[ChunkDescriptor]
    cobertura: float = Field(ge=0, le=1)
    status_objetivo: ObjectiveStatus
    payload: AnalysisPayload
    checks: DeterministicChecks
    redactions_count: int = Field(ge=0)
    warnings: list[str] = Field(default_factory=list)
    parse_attempts: int = Field(ge=1, le=2)
    error: str | None = None
    usage: Usage
    latency_ms: int = Field(ge=0)
    input_fingerprint: str
    completed_at: datetime
    cache_hit: bool = False


class FailureRecord(StrictModel):
    run_id: str
    run_key: str
    provider: str
    model: str
    runtime: dict[str, str | int | bool | None] = Field(default_factory=dict)
    lead_alias: str
    cutoff_at: datetime | None
    error_type: str
    error_message: str
    parse_attempts: int = Field(ge=1, le=2)
    failed_at: datetime


class SkipRecord(StrictModel):
    lead_alias: str
    reason: str


class PrivateCorpusLead(StrictModel):
    """Usado somente pelo preparador local; nunca segue para providers."""

    lead_id: str = Field(min_length=1)
    nome: str = ""
    telefone: str = ""
    corretora: str = ""
    mensagens: list[str]


class RedactedLead(StrictModel):
    lead_alias: str = Field(pattern=r"^LEAD-[A-Z2-7]{10}$")
    corretora_alias: str = Field(pattern=r"^CORRETORA-[A-Z2-7]{10}$")
    mensagens: list[str]
    mensagens_total: int = Field(ge=1)
    faixa_jornada: str
    temperatura_referencia: str
    redactions_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_message_count(self) -> RedactedLead:
        if self.mensagens_total != len(self.mensagens):
            raise ValueError("mensagens_total diverge da lista")
        if not any(message.strip() for message in self.mensagens):
            raise ValueError("conversa sem conteúdo utilizável")
        return self


class ParsedMessage(StrictModel):
    index: int = Field(ge=1)
    original_index: int = Field(ge=1)
    raw: str
    timestamp: datetime | None = None
    sender: str | None = None
    message_type: str | None = None
    automated: bool = False


class SelectionItem(StrictModel):
    lead_alias: str
    corretora_alias: str
    faixa_jornada: str
    temperatura_referencia: str
    mensagens_total: int = Field(ge=1)
    redactions_count: int = Field(ge=0)


class SelectionManifest(StrictModel):
    schema_version: str = "2"
    seed: str
    corpus_fingerprint: str
    target_size: int = Field(ge=1)
    items: list[SelectionItem]


class RunManifest(StrictModel):
    run_id: str
    corpus_fingerprint: str
    provider: str
    model: str
    runtime: dict[str, str | int | bool | None] = Field(default_factory=dict)
    selection_fingerprint: str
    cutoff_at: datetime | None = None
    started_at: datetime
    completed_at: datetime | None = None
    selected: int = Field(ge=0)
    completed: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)
    skipped: int = Field(default=0, ge=0)
    cache_hits: int = Field(default=0, ge=0)
    critical_failures: int = Field(default=0, ge=0)
    not_run: int = Field(default=0, ge=0)
    stopped_early: bool = False
    stop_reason: str | None = None
