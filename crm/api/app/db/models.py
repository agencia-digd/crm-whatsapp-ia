import uuid
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

from sqlalchemy import BigInteger, Boolean, Date, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, Time, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


_INTERNAL_CONTACT_CATEGORIAS = ("corretora", "coordenacao", "admin", "parceiro", "outros")


class Base(DeclarativeBase):
    pass


class Corretora(Base):
    __tablename__ = "corretoras"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    nome: Mapped[str] = mapped_column(Text, nullable=False)
    telefone: Mapped[str] = mapped_column(Text, nullable=False)
    kommo_user_id: Mapped[int | None] = mapped_column(Integer)
    evolution_instance: Mapped[str | None] = mapped_column(Text)
    evolution_connected_since: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Fonte de verdade da conversa: 'evolution' (lê pela instância Evolution) ou
    # 'cloud' (lê da tabela mensagens_whatsapp — Pessoa12 na Meta Cloud API). Migration 0056.
    canal: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'evolution'"))
    ativo: Mapped[bool] = mapped_column(Boolean, default=True)
    papel: Mapped[str] = mapped_column(Text, server_default="corretora_solo", nullable=False)
    telefone_normalizado: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Nome que o CLIENTE lê no texto das mensagens — separado do `nome`, que é a
    # identidade INTERNA (painel, ranking, Kommo). Só diverge quando a corretora
    # atende com outro nome: a Pessoa3 (linha 2591) se apresenta como "Pessoa",
    # e o verified_name do número na Meta é "Pessoa Pessoa9 Acme". NULL = usa o
    # primeiro nome de `nome`. Migration 0082 — nasceu do caso Kauane (18/09),
    # que recebeu "Aqui é a Pessoa3" numa linha que ela conhece como Pessoa.
    nome_publico: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Opt-out do PUSH de briefing seg/sex pela linha da Assistente (botão "Parar
    # mensagens" do template). NÃO desativa a corretora — só o push. Migration 0058.
    briefing_optout: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), default=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class LeadSnapshot(Base):
    __tablename__ = "leads_snapshot"

    lead_id: Mapped[str] = mapped_column(Text, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    nome: Mapped[str | None] = mapped_column(Text)
    # True = nome editado à mão no painel; o sync Kommo→local NÃO sobrescreve o
    # campo `nome` (proteção — a agência vai largar o Kommo).
    nome_editado_manual: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    telefone: Mapped[str | None] = mapped_column(Text)
    # True = telefone corrigido à mão no painel; o sync Kommo→local NÃO
    # sobrescreve o campo `telefone` (proteção — migration 0050).
    telefone_editado_manual: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    corretora_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("corretoras.id"))
    # True = lead TRANSFERIDO de corretora no painel (/crm/transferir); o sync
    # Kommo→local NÃO sobrescreve corretora_id (proteção — migration 0061).
    corretora_editado_manual: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    etapa_kommo: Mapped[str | None] = mapped_column(Text)
    origem: Mapped[str | None] = mapped_column(Text)
    criado_em_kommo: Mapped[datetime | None] = mapped_column(DateTime)
    ultima_sync: Mapped[datetime | None] = mapped_column(DateTime)


class LeadAnalysis(Base):
    __tablename__ = "lead_analyses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    lead_id: Mapped[str] = mapped_column(Text, ForeignKey("leads_snapshot.lead_id"))
    analyzed_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    raw_analysis: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    status: Mapped[str | None] = mapped_column(Text)
    temperatura: Mapped[str | None] = mapped_column(Text)
    has_gap: Mapped[bool | None] = mapped_column(Boolean)
    consumed_by_dona: Mapped[bool] = mapped_column(Boolean, default=False)
    consumed_by_pessoa: Mapped[bool] = mapped_column(Boolean, default=False)
    consumed_by_corretora: Mapped[bool] = mapped_column(Boolean, default=False)
    corretora_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("corretoras.id"), nullable=True)


class BriefingSent(Base):
    __tablename__ = "briefings_sent"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    destinatario: Mapped[str] = mapped_column(Text)
    canal: Mapped[str] = mapped_column(Text)
    conteudo: Mapped[str] = mapped_column(Text)
    sent_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    success: Mapped[bool | None] = mapped_column(Boolean)
    # True quando o registro veio de dry-run/teste/smoke (não é entrega real).
    # O anti-dobro (_already_delivered_today) ignora esses registros.
    is_test: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")


class PatternWeekly(Base):
    __tablename__ = "patterns_weekly"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    corretora_id: Mapped[int] = mapped_column(Integer, ForeignKey("corretoras.id"))
    week_start: Mapped[datetime] = mapped_column(Date)
    pattern_type: Mapped[str] = mapped_column(Text)
    occurrences: Mapped[int] = mapped_column(Integer)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class InternalContact(Base):
    """DEPRECATED — use InternoAcme (internos_acme table) instead. Drop in Onda 9."""

    __tablename__ = "internal_contacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telefone: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    nome: Mapped[str] = mapped_column(Text, nullable=False)
    categoria: Mapped[str] = mapped_column(Text, nullable=False)
    observacao: Mapped[str | None] = mapped_column(Text)
    ativo: Mapped[bool] = mapped_column(Boolean, default=True)
    criado_em: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    phone: Mapped[str] = mapped_column(Text, nullable=False)
    sender_kind: Mapped[str] = mapped_column(Text, nullable=False)
    sender_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    message_role: Mapped[str] = mapped_column(Text, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    message_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class InternoAcme(Base):
    __tablename__ = "internos_acme"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    nome: Mapped[str] = mapped_column(Text, nullable=False)
    telefone_normalizado: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    papel: Mapped[str] = mapped_column(Text, nullable=False)
    ativa: Mapped[bool] = mapped_column(Boolean, server_default="true", nullable=False)
    modo_teste: Mapped[bool] = mapped_column(Boolean, server_default="false", nullable=False)
    observacao: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class CoachingObservation(Base):
    __tablename__ = "coaching_observations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    corretora_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("corretoras.id"), nullable=False)
    lead_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    lead_nome: Mapped[str | None] = mapped_column(Text, nullable=True)
    trecho_corretora: Mapped[str | None] = mapped_column(Text, nullable=True)
    observacao: Mapped[str] = mapped_column(Text, nullable=False)
    sugestao: Mapped[str | None] = mapped_column(Text, nullable=True)
    categoria: Mapped[str] = mapped_column(Text, nullable=False)
    origem: Mapped[str] = mapped_column(Text, nullable=False)
    analyzed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ShadowDecision(Base):
    """Modo sombra: o que a IA responderia (sem enviar). Alimenta o placar Aprendiz."""

    __tablename__ = "shadow_decisions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    corretora_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("corretoras.id"), nullable=True)
    lead_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    lead_nome: Mapped[str | None] = mapped_column(Text, nullable=True)
    lead_telefone: Mapped[str | None] = mapped_column(Text, nullable=True)
    bucket: Mapped[str] = mapped_column(Text, nullable=False)
    confianca: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    resposta_sugerida: Mapped[str | None] = mapped_column(Text, nullable=True)
    motivo: Mapped[str | None] = mapped_column(Text, nullable=True)
    ultima_msg_lead: Mapped[str | None] = mapped_column(Text, nullable=True)
    ultima_msg_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    modelo: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    # fase aprendiz (preenchido quando a corretora responde de verdade):
    resposta_real: Mapped[str | None] = mapped_column(Text, nullable=True)
    acerto: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    avaliado_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    avaliado_por: Mapped[str | None] = mapped_column(Text, nullable=True)
    # juiz IA (claude -p): bateu | parcial | nao_bateu — migration 0092
    juiz_veredito: Mapped[str | None] = mapped_column(Text, nullable=True)
    juiz_motivo: Mapped[str | None] = mapped_column(Text, nullable=True)
    juiz_modelo: Mapped[str | None] = mapped_column(Text, nullable=True)
    juiz_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ShadowCadencia(Base):
    """Sombra da CADÊNCIA: o que a IA mandaria no lugar do template fixo.

    Irmão proativo do ShadowDecision (que é reativo). Nada aqui é enviado —
    guarda o par (template atual × sugestão da IA) pra Dona julgar qual ganha.
    """

    __tablename__ = "shadow_cadencia"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    corretora_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("corretoras.id"), nullable=True)
    lead_telefone: Mapped[str | None] = mapped_column(Text, nullable=True)
    lead_nome: Mapped[str | None] = mapped_column(Text, nullable=True)
    toque: Mapped[int] = mapped_column(Integer, nullable=False)
    template_atual: Mapped[str] = mapped_column(Text, nullable=False)
    sugestao_ia: Mapped[str | None] = mapped_column(Text, nullable=True)
    usou_contexto: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    contexto_usado: Mapped[str | None] = mapped_column(Text, nullable=True)
    motivo: Mapped[str | None] = mapped_column(Text, nullable=True)
    # veredito humano: None = não julgado · 'ia' | 'template' | 'empate'
    veredito: Mapped[str | None] = mapped_column(Text, nullable=True)
    modelo: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    # ── ciclo de vida (0066): sombra → fila de aprovação → uso ──
    # sombra: só pra comparar, nunca sai · pendente: esperando revisão humana
    # aprovada: cadência usa `texto_final` · rejeitada: cadência usa o template
    # usada: já saiu · expirada: toque passou sem aprovação
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="sombra")
    # o que de fato vai pro lead — pode ser a sugestão EDITADA por quem aprovou.
    # separado de `sugestao_ia` pra dar pra medir se a IA melhora ou se é a
    # humana consertando.
    texto_final: Mapped[str | None] = mapped_column(Text, nullable=True)
    aprovado_por: Mapped[str | None] = mapped_column(Text, nullable=True)
    aprovado_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    usada_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MensagemAutomacao(Base):
    __tablename__ = "mensagens_automacao"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    corretora_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("corretoras.id"), nullable=False)
    texto_normalizado: Mapped[str] = mapped_column(Text, nullable=False)
    texto_exemplo: Mapped[str] = mapped_column(Text, nullable=False)
    ocorrencias: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    leads_distintos: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    primeira_vez: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ultima_vez: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    detectada_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    ativa: Mapped[bool] = mapped_column(Boolean, server_default="true", nullable=False)
    observacao: Mapped[str | None] = mapped_column(Text, nullable=True)


class InspectorRun(Base):
    """Rastreia cada execução do Inspector por corretora, com janela temporal."""

    __tablename__ = "inspector_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    corretora_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("corretoras.id"), nullable=True
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="running")
    mensagens_processadas: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    leads_analisados: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    erro: Mapped[str | None] = mapped_column(Text, nullable=True)
    window_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OportunidadeCuidada(Base):
    __tablename__ = "oportunidade_cuidada"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    corretora_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("corretoras.id", ondelete="CASCADE"), nullable=False
    )
    lead_id: Mapped[str] = mapped_column(Text, nullable=False)
    marcado_em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Lembrete(Base):
    __tablename__ = "lembretes"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    corretora_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("corretoras.id", ondelete="SET NULL"), nullable=True
    )
    telefone_destino: Mapped[str] = mapped_column(Text, nullable=False)
    texto: Mapped[str] = mapped_column(Text, nullable=False)
    lembrar_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    criado_em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    criado_via_msg_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    enviado_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'pendente'"))
    erro: Mapped[str | None] = mapped_column(Text, nullable=True)


class MensagemWhatsapp(Base):
    __tablename__ = "mensagens_whatsapp"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    evolution_message_id: Mapped[str] = mapped_column(Text, nullable=False)
    instance: Mapped[str] = mapped_column(Text, nullable=False)
    corretora_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("corretoras.id", ondelete="SET NULL"), nullable=True
    )
    remote_jid: Mapped[str] = mapped_column(Text, nullable=False)
    contato_telefone: Mapped[str | None] = mapped_column(Text, nullable=True)
    contato_nome: Mapped[str | None] = mapped_column(Text, nullable=True)
    from_corretora: Mapped[bool] = mapped_column(Boolean, nullable=False)
    tipo_mensagem: Mapped[str] = mapped_column(Text, nullable=False)
    conteudo_texto: Mapped[str | None] = mapped_column(Text, nullable=True)
    conteudo_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    timestamp_evolution: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    recebido_em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    raw_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    transcricao: Mapped[str | None] = mapped_column(Text, nullable=True)
    transcricao_modelo: Mapped[str | None] = mapped_column(String(50), nullable=True)
    transcricao_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    transcricao_status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="pendente")
    transcricao_erro: Mapped[str | None] = mapped_column(Text, nullable=True)


class RecoveryConversation(Base):
    """Estado da conversa de reativação Corretor ↔ lead (funil Remarketing)."""
    __tablename__ = "recovery_conversations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    lead_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True, index=True)
    lead_phone: Mapped[str] = mapped_column(Text, nullable=False)
    lead_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default="aguardando")
    turns: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    reengage_done: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    end_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    qualified_data: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    hsm_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_lead_msg_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_corretor_msg_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class RecoveryMessage(Base):
    """Histórico cronológico de mensagens da conversa de reativação."""
    __tablename__ = "recovery_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    lead_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    role: Mapped[str] = mapped_column(Text, nullable=False)  # 'lead' | 'corretor'
    text_content: Mapped[str] = mapped_column("text", Text, nullable=False)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class HunterProspect(Base):
    """Prospect do Prospector / outras prospecções agencia.

    Source-of-truth dos leads pra quem a Assistente-agente disparou e/ou está
    conversando. Backfilled do site-hunter dashboard/state.json + novos
    leads via scripts/hunter_dispatch.py.
    """
    __tablename__ = "hunter_prospects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    phone: Mapped[str] = mapped_column(Text, nullable=False, unique=True, index=True)
    nome: Mapped[str | None] = mapped_column(Text, nullable=True)
    segmento: Mapped[str | None] = mapped_column(Text, nullable=True)
    cidade: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(Text, nullable=False, server_default="site-hunter")
    slug: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    preview_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    # estados: pending, sent, responded, qualificando, qualificado, perdido, opted_out
    turns: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    end_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    qualified_data: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    hsm_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_lead_msg_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_assistente_msg_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class HunterMessage(Base):
    """Histórico cronológico Assistente ↔ prospect Hunter."""
    __tablename__ = "hunter_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    phone: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    role: Mapped[str] = mapped_column(Text, nullable=False)  # 'lead' | 'assistente'
    text_content: Mapped[str] = mapped_column("text", Text, nullable=False)
    message_id: Mapped[str | None] = mapped_column(Text, nullable=True, unique=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class LeadHandoffNote(Base):
    """Nota curada de passagem de lead pra Assistente conversacional.

    Quando um lead é qualificado/resgatado (ex: pela Corretor, motor de recuperação)
    e passado pra uma corretora, a narrativa do handoff vive aqui — pra Assistente
    ter contexto quando a corretora responder, em vez de só ver mensagens/Kommo.
    """
    __tablename__ = "lead_handoff_notes"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    telefone: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    lead_nome: Mapped[str | None] = mapped_column(Text, nullable=True)
    corretora_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("corretoras.id", ondelete="SET NULL"), nullable=True
    )
    contexto: Mapped[str] = mapped_column(Text, nullable=False)
    origem: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'ativo'")
    )
    criado_em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    atualizado_em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TarefaCorretora(Base):
    """Tarefa criada pela corretora (ou admin) num lead, agendada pra um dia.

    A corretora escreve uma tarefa (dia + texto) num lead; ela aparece no "Hoje"
    da corretora dona quando `data_tarefa <= hoje` e `status='aberta'`. Ao concluir,
    vira `status='concluida'` e some da lista do Hoje.
    """
    __tablename__ = "tarefas_corretora"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    lead_id: Mapped[str] = mapped_column(Text, nullable=False)
    lead_nome: Mapped[str | None] = mapped_column(Text, nullable=True)
    corretora_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("corretoras.id", ondelete="SET NULL"), nullable=True
    )
    data_tarefa: Mapped[date] = mapped_column(Date, nullable=False)
    texto: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'aberta'")
    )
    autor: Mapped[str | None] = mapped_column(Text, nullable=True)
    criado_em: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    concluido_em: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # quem concluiu (login do painel, migration 0062) — histórico "Concluídas" V2
    concluido_por: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── mensagem planejada + anotações (migrations 0048 / 0056) ──
    # ⚠️ Estas 4 colunas existiam no BANCO desde a 0048 mas NUNCA foram
    # declaradas aqui. Como o conftest monta o banco de teste a partir dos
    # models (`create_all`), a tabela de teste nascia SEM elas — e por isso era
    # IMPOSSÍVEL escrever teste pra mensagem planejada.
    #
    # Foi o que deixou passar, em 29/07, um NameError que matou o envio em
    # produção por um dia inteiro com 1.326 testes verdes. O bug foi achado por
    # auditoria externa, não pelo CI. Declarar aqui é o que destrava a cobertura.
    hora_tarefa: Mapped[time | None] = mapped_column(Time, nullable=True)
    mensagem_planejada: Mapped[str | None] = mapped_column(Text, nullable=True)
    msg_planejada_enviada_em: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    anotacoes: Mapped[str | None] = mapped_column(Text, nullable=True)


class AssistenteConversa(Base):
    """Histórico de interações da Assistente Copilota (assistente interna da corretora).

    Um registro best-effort por request do /painel/assistente e /painel/assistente/acao.
    Nunca quebra o fluxo (falha vira log warning). Serve pra medir adoção, intents
    mais usados, taxa de sucesso das ações e latência. Gap 🔴 B1 da REVISAO-06.
    """
    __tablename__ = "assistente_conversas"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    corretora_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("corretoras.id", ondelete="SET NULL"), nullable=True
    )
    usuario: Mapped[str] = mapped_column(Text, nullable=False)
    intent: Mapped[str | None] = mapped_column(Text, nullable=True)
    pergunta: Mapped[str | None] = mapped_column(Text, nullable=True)
    resposta_resumo: Mapped[str | None] = mapped_column(String(300), nullable=True)
    acao_tipo: Mapped[str | None] = mapped_column(Text, nullable=True)
    acao_executada: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    latencia_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    criado_em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class DisparoReengajamento(Base):
    """Registro de cada disparo real de reengajamento (template WABA) na linha da Pessoa12.

    Fonte de verdade pro anti-ban do /disparo/enviar: cooldown de 14 dias por
    telefone, máx 2 tentativas sem resposta, cap diário, espaçamento mínimo e
    opt-out. Um registro por envio real bem-sucedido.
    """
    __tablename__ = "disparos_reengajamento"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    lead_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    corretora_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    telefone: Mapped[str] = mapped_column(Text, nullable=False)
    template: Mapped[str | None] = mapped_column(Text, nullable=True)
    variacao: Mapped[int | None] = mapped_column(Integer, nullable=True)
    texto_enviado: Mapped[str | None] = mapped_column(Text, nullable=True)
    enviado_por: Mapped[str | None] = mapped_column(Text, nullable=True)
    enviado_em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    respondeu_em: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    opt_out: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    opt_out_em: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Id da mensagem na Meta (0087). É a chave que casa o disparo com o status
    # de entrega em `mensagens_whatsapp`. NULL nas linhas anteriores a 04/10/2026.
    wamid: Mapped[str | None] = mapped_column(Text, nullable=True)


# ── Espinha canônica do lead (Opção A · multi-tenant) ─────────────────────────
#
# Contrato compartilhado com a plataforma agencia. Estas 3 tabelas são o DONO da
# verdade do lead no nosso Postgres. Hoje alimentadas por dual-write EM SOMBRA
# (shadow) a partir do Kommo — Kommo segue como fonte de leitura enquanto o
# shadow estiver ligado (settings.lead_spine_shadow). Ver 0043 + lead_spine.py.


class PipelineStage(Base):
    __tablename__ = "pipeline_stages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(Integer, nullable=False)
    nome: Mapped[str | None] = mapped_column(Text, nullable=True)
    ordem: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tipo: Mapped[str | None] = mapped_column(Text, nullable=True)
    # A qual funil a etapa pertence: 'principal' (8 canônicos) ou 'remarketing'
    # (nutrição pós-cadência: Base → 30/60/90/120/150 dias). Ver migration 0046 +
    # app/services/nutricao.py. Default 'principal' = zero backfill.
    funil: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'principal'")
    )


class Lead(Base):
    __tablename__ = "leads"
    __table_args__ = (
        UniqueConstraint("tenant_id", "external_ref", name="uq_leads_tenant_external_ref"),
        Index("ix_leads_tenant_stage", "tenant_id", "stage_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    external_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    nome: Mapped[str | None] = mapped_column(Text, nullable=True)
    # True = nome editado à mão no painel; o shadow do Kommo (lead_spine) NÃO
    # sobrescreve `nome`. Proteção do sync — ver migration 0049.
    nome_editado_manual: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    telefone: Mapped[str | None] = mapped_column(Text, nullable=True)
    # True = telefone corrigido à mão no painel; o shadow do Kommo (lead_spine)
    # NÃO sobrescreve `telefone`. Proteção do sync — ver migration 0050.
    telefone_editado_manual: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    # True = telefone entrou numa forma AMBÍGUA (12 dígitos começando com 7/8:
    # pode ser fixo antigo ou celular que perdeu o 9). O lead entra na espinha
    # do mesmo jeito — é lead vivo, perder é pior que revisar — mas fica
    # marcado pra triagem humana e NÃO casa com ninguém no dedupe.
    # Ver app/services/phone.canonical_phone_info e migration 0063.
    telefone_invalido: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    owner_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # True = dono trocado à mão no painel (transferência de lead entre
    # corretoras — /painel/crm/transferir); o shadow do Kommo (lead_spine) NÃO
    # sobrescreve `owner_id`. Proteção do sync — ver migration 0061.
    owner_editado_manual: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    # TAG "veio do remarketing por LIGAÇÃO" — quando (e se) uma corretora
    # adotou este lead marcando "atendeu e quer continuar" na janela de ligação.
    # É campo DATADO em vez de flag/string de tag de propósito: responde
    # "veio de ligação?" (IS NOT NULL) e ainda "há quanto tempo?" — que é o que
    # a boas-vindas precisa saber pra não mandar "você acabou de se cadastrar"
    # pra quem acabou de falar por telefone. Ver
    # `app/services/boas_vindas_continuidade.py` (fluxo completo) e
    # `ligacao_resultado.adotar_lead`. DDL direta —
    # `scripts/criar_ligacao_resultado.py`.
    adotado_ligacao_em: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # TAG "veio do remarketing" — quando (e se) este lead saiu do funil de
    # remarketing (a linha da Corretor) pra mão de uma corretora humana. Pedido da
    # Dona 15/09/2026: a corretora precisa saber de onde a pessoa veio SEM
    # abrir o histórico.
    # Mesma escolha do `adotado_ligacao_em` acima: campo DATADO em vez de
    # flag/string de tag solta. Responde "veio do remarketing?" (IS NOT NULL) e
    # "há quanto tempo?" — e não colide com `origem`, que o shadow do Kommo
    # sobrescreve a cada sync. Escrito UMA vez (coalesce): quem foi transferido
    # em 15/09 continua marcado com 15/09 mesmo que passe por outro handoff.
    # Escrito por: `lead_transferencia.transferir_lead(marcar_remarketing=True)`
    # (handoff automático) e `atendimento_corretor.promover_para_vendas` (promoção da
    # Pessoa/Corretor). Lido por: card do `/painel/crm/board` (`veio_remarketing`).
    # Ver migration 0081.
    veio_remarketing_em: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    stage_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("pipeline_stages.id"), nullable=True
    )
    # Quando o cliente entrou no sistema de ORIGEM (hoje só Kommo). Diferente
    # de `created_at`, que é quando a linha nasceu AQUI: os leads de nutrição
    # foram importados em lote e têm todos o mesmo `created_at` (22/07/2026
    # 17:33). NULL = não sabemos — a tela mostra "—" em vez de chutar uma data.
    # Ver migration 0074.
    criado_em_origem: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    origem: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Origem Meta Lead Ads (migration 0060) — preenchidas pelo receptor REAL do
    # webhook (/intake/meta-webhook) a partir da Graph API. campaign_name /
    # ad_name / form_id. NULL pra leads de outras origens (Kommo, manual).
    origem_campanha: Mapped[str | None] = mapped_column(Text, nullable=True)
    origem_anuncio: Mapped[str | None] = mapped_column(Text, nullable=True)
    origem_form: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Valor de negócio do lead (R$) — alimenta KPIs de R$ do CRM. Vem do `price`
    # do lead Kommo no shadow. NULL quando o Kommo não traz valor.
    valor: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=True
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class LeadEvent(Base):
    __tablename__ = "lead_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(Integer, nullable=False)
    lead_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("leads.id"), nullable=True
    )
    tipo: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=True
    )


# ── Prospecção/SDR agencia (F0 · staging antes do CRM) ─────────────────────────
#
# O copiloto NÃO usa estas tabelas — elas vivem aqui porque o banco `copiloto`
# é o dono do schema canônico (tenant 2 = agencia) e o alembic daqui é a fonte de
# migrations. Escrita/leitura: API prospect (projetos/api) + runner de
# prospecção. Ver migration 0051 + projetos/agencia-central/docs/
# SPEC-PROSPECT-STAGING.md.


class ProspectSearch(Base):
    __tablename__ = "prospect_searches"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("2"))
    descricao_natural: Mapped[str] = mapped_column(Text, nullable=False)
    params_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    # pendente → processando → concluida | erro
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pendente'"))
    # créditos Xpoz REAIS medidos pelo runner (diff do getCreditsUsageHistory)
    custo_creditos: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, server_default=text("0")
    )
    erro: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ProspectLead(Base):
    __tablename__ = "prospect_leads"
    __table_args__ = (
        UniqueConstraint("tenant_id", "username", name="uq_prospect_leads_tenant_username"),
        Index("ix_prospect_leads_search", "search_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    search_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("prospect_searches.id", ondelete="CASCADE"), nullable=False
    )
    tenant_id: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("2"))
    username: Mapped[str] = mapped_column(Text, nullable=False)
    nome: Mapped[str | None] = mapped_column(Text, nullable=True)
    bio: Mapped[str | None] = mapped_column(Text, nullable=True)
    link: Mapped[str | None] = mapped_column(Text, nullable=True)
    telefone_whatsapp: Mapped[str | None] = mapped_column(Text, nullable=True)
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    qualificacao_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    # staging | promovido | descartado — NUNCA regride (regra do upsert do runner)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'staging'"))
    crm_lead_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# ── Cadência híbrida (P2 · multi-tenant) ──────────────────────────────────────
#
# Motor de cadência automática: state machine de 8 toques híbridos + remarketing.
# `cadencia_estado` = 1 linha por lead em cadência. `cadencia_eventos` = log
# append-only do que o tick decidiu/enfileirou (fonte de idempotência). Ver
# migration 0044 + app/services/cadencia.py.


class CadenciaEstado(Base):
    __tablename__ = "cadencia_estado"
    __table_args__ = (
        UniqueConstraint("tenant_id", "telefone", name="uq_cadencia_estado_tenant_telefone"),
        Index("ix_cadencia_estado_devido", "tenant_id", "status", "proximo_toque_em"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    lead_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    telefone: Mapped[str] = mapped_column(Text, nullable=False)
    corretora_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    toque_atual: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    proximo_toque_em: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'ativa'"))
    criado_em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    atualizado_em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CadenciaEvento(Base):
    __tablename__ = "cadencia_eventos"
    __table_args__ = (
        UniqueConstraint("tenant_id", "cadencia_id", "toque", name="uq_cadencia_eventos_toque"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    cadencia_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("cadencia_estado.id"), nullable=False
    )
    toque: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tipo: Mapped[str | None] = mapped_column(Text, nullable=True)
    canal: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CadenciaCopy(Base):
    """Override de copy dos toques da cadência (migration 0057).

    A copy DEFAULT continua no código (`cadencia._TEMPLATES_MSG` /
    `_TEMPLATE_REMARKETING`) — esta tabela guarda só o texto PERSONALIZADO
    pela tela /automacoes do painel. `toque` usa o índice do toque de mensagem
    (0, 2, 3, 5, 7); `toque = -1` é a mensagem de remarketing (30 em 30 dias).
    Sem linha aqui = usa o default. Apagar a linha = "restaurar padrão".
    """

    __tablename__ = "cadencia_copies"
    __table_args__ = (
        UniqueConstraint("tenant_id", "toque", name="uq_cadencia_copies_tenant_toque"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    toque: Mapped[int] = mapped_column(Integer, nullable=False)
    texto: Mapped[str] = mapped_column(Text, nullable=False)
    atualizado_por: Mapped[str | None] = mapped_column(Text, nullable=True)
    atualizado_em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ReguaMarco(Base):
    """Marco de régua — o "recomeçar a medição" da tela de automações.

    Quando a régua muda (entrou o toque D3 de áudio em 04/08) ou as copies
    viram outra coisa, a métrica anterior está medindo OUTRA máquina: comparar
    D0 de antes com D0 de agora é comparar duas mensagens diferentes.

    O caminho óbvio seria apagar `cadencia_eventos` — e é justamente o que NÃO
    se faz: aquilo é auditoria, é como a gente prova o que saiu pra quem, e já
    salvou investigação. Então em vez de destruir dado, marca-se uma data: a
    métrica passa a contar só o que veio DEPOIS do último marco. Reversível
    (apagar a linha devolve o histórico inteiro) e sem perder um evento.

    DDL aplicada direto no banco (schema aqui não passa por alembic) —
    `scripts/criar_regua_marcos.py`.
    """

    __tablename__ = "regua_marcos"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    criado_em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    motivo: Mapped[str] = mapped_column(Text, nullable=False)
    criado_por: Mapped[str | None] = mapped_column(Text, nullable=True)


class LigacaoResultado(Base):
    """O que aconteceu numa LIGAÇÃO que a corretora marcou como feita.

    Três dados, na ordem de importância que a Dona já media na mão quando o time
    tinha uma hora fixa só pra ligar: atendeu? · quis continuar o atendimento? ·
    observação livre.

    POR QUE TABELA PRÓPRIA (e não colunas em `tarefas_corretora`):
      1. a fila de ligação da régua NÃO vive em `tarefas_corretora` — ela é
         `cadencia_eventos(tipo='tarefa_ligacao')`, gerada pelo motor (537 nos
         últimos 30 dias). Colunas em `tarefas_corretora` deixariam de fora
         justamente a origem que produz quase todas as ligações;
      2. as duas origens (cadência e tarefa manual) precisam cair no MESMO
         número — a tabela é o ponto de encontro, com `origem`/`origem_id`;
      3. `tarefas_corretora` não ganharia 3 colunas NULL em ~99% das linhas.

    AUSÊNCIA ≠ ZERO: não existe linha aqui pra ligação concluída SEM registro —
    ela conta como "sem registro" na métrica, nunca como "não atendeu".
    `quis_continuar` é NULL quando não atendeu (a pergunta nem é feita).

    `toque` = índice do toque da cadência; 0 nas tarefas MANUAIS (não existe
    toque 0 de ligação — a régua usa 1/4/6). Fica NOT NULL pra o UNIQUE de
    idempotência funcionar (em Postgres, NULL não colide com NULL).

    DDL aplicada direto no banco (schema aqui não passa por alembic) —
    `scripts/criar_ligacao_resultado.py`.
    """

    __tablename__ = "ligacao_resultado"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "origem", "origem_id", "toque",
            name="uq_ligacao_resultado_origem",
        ),
        Index("ix_ligacao_resultado_criado_em", "criado_em"),
        Index("ix_ligacao_resultado_corretora", "corretora_id", "criado_em"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    # 'cadencia' (origem_id = cadencia_estado.id) | 'tarefa' (tarefas_corretora.id)
    origem: Mapped[str] = mapped_column(Text, nullable=False)
    origem_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    toque: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    lead_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    corretora_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("corretoras.id", ondelete="SET NULL"), nullable=True
    )
    atendeu: Mapped[bool] = mapped_column(Boolean, nullable=False)
    quis_continuar: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    observacao: Mapped[str | None] = mapped_column(Text, nullable=True)
    autor: Mapped[str | None] = mapped_column(Text, nullable=True)
    criado_em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class LigacaoReserva(Base):
    """Quem PEGOU cada lead da janela de ligação, e até quando.

    A fila de ligação era global: duas corretoras abrindo a aba na mesma manhã
    viam o mesmo lead no topo e ligavam pra mesma pessoa. Quem pede a fila
    reserva os leads que levou por `ligacao_resultado.RESERVA_MINUTOS`.

    POR QUE TABELA (e não coluna em `leads`): é estado EFÊMERO de tela — nasce
    e morre em minutos, não descreve o lead. `leads` é espelhado do Kommo e
    lido pelo funil inteiro; sujar ela com estado de UI custa caro depois.

    EXPIRA SOZINHA (`expira_em`): reserva sem prazo trancaria o lead pra sempre
    se a corretora fechar a aba. UNIQUE por (tenant, lead) — a linha é
    reaproveitada por quem pegar depois, não acumula histórico.

    DDL aplicada direto no banco (schema aqui não passa por alembic) —
    `scripts/criar_ligacao_resultado.py`.
    """

    __tablename__ = "ligacao_reserva"
    __table_args__ = (
        UniqueConstraint("tenant_id", "lead_id", name="uq_ligacao_reserva_lead"),
        Index("ix_ligacao_reserva_expira", "expira_em"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    # leads.id (espinha) — a mesma chave que a janela de remarketing usa
    lead_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # usuário do painel (não corretora_id: admin também pega lead pra ligar)
    dono: Mapped[str] = mapped_column(Text, nullable=False)
    corretora_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("corretoras.id", ondelete="SET NULL"), nullable=True
    )
    reservado_em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expira_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class LigacaoMetaDia(Base):
    """O dia de ligação de UMA corretora: bloco extra liberado e confete dado.

    Só o que NÃO dá pra derivar de `ligacao_resultado` mora aqui — a contagem de
    ligações do dia e da semana é sempre contada dos registros, nunca de um
    contador que pode dessincronizar.

    `blocos_extras`: quantas vezes ela clicou "adiantar a tarefa de amanhã"
    depois de fechar a meta. É por DIA CIVIL de propósito — adiantar aumenta a
    fila de HOJE e o progresso da SEMANA (que é o ponto: fechar antes da sexta),
    e NÃO desconta a meta de amanhã. Amanhã ela abre com 25 de novo, nunca com
    50 nem com 0.

    `confete_em`: carimbo da comemoração. É o que faz o confete cair UMA vez no
    dia em que a meta fechou, e não a cada F5.

    DDL aplicada direto no banco — `scripts/criar_ligacao_resultado.py`.
    """

    __tablename__ = "ligacao_meta_dia"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "corretora_id", "dia", name="uq_ligacao_meta_dia"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    corretora_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("corretoras.id", ondelete="CASCADE"), nullable=False
    )
    # dia CIVIL em America/Sao_Paulo (não UTC — 21h daqui já é o dia seguinte lá)
    dia: Mapped[date] = mapped_column(Date, nullable=False)
    blocos_extras: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    confete_em: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class AssistenteLembreteEnviado(Base):
    """Dedup do lembrete de pendência de registro da Assistente (migration 0052).

    Uma pendência = (corretora_id, conversa_ultima_msg_at). UNIQUE garante que
    a mesma pendência nunca é cobrada 2x. Só gravado em envio REAL (dry-run loga).
    """

    __tablename__ = "assistente_lembretes_enviados"
    __table_args__ = (
        UniqueConstraint(
            "corretora_id", "conversa_ultima_msg_at",
            name="uq_assistente_lembrete_pendencia",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    corretora_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("corretoras.id"), nullable=False
    )
    conversa_ultima_msg_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    enviado_em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class MetaLeadSombra(Base):
    """Auditoria do receptor Meta leadgen em MODO SOMBRA (B6 · migration 0053).

    Toda entrega de leadgen no POST /intake/meta-leadgen/{secret} vira UMA linha
    aqui, SEMPRE — mesmo com LEAD_INTAKE_ATIVO=false. Idempotência por
    leadgen_id UNIQUE. `processado=true` só quando o lead entrou de verdade no
    fluxo /intake/lead (flag ligada).
    """

    __tablename__ = "meta_leads_sombra"
    __table_args__ = (
        UniqueConstraint("leadgen_id", name="uq_meta_leads_sombra_leadgen_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    leadgen_id: Mapped[str] = mapped_column(Text, nullable=False)
    nome: Mapped[str | None] = mapped_column(Text, nullable=True)
    telefone: Mapped[str | None] = mapped_column(Text, nullable=True)
    # campaign_name da Graph API — a futura tag rica 📣 do card do CRM
    campanha: Mapped[str | None] = mapped_column(Text, nullable=True)
    ad_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    form_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    recebido_em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    processado: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )


class HistoricoCache(Base):
    """Cache persistente do /painel/historico (migration 0059).

    Uma linha por mês (`ano_mes` = 'YYYY-MM') com o payload pronto do mês
    (meta_ads + equipe + por_corretora). Semântica de validade (decidida na
    LEITURA, não na escrita):
      - mês FECHADO calculado APÓS o fechamento → imutável (serve pra sempre);
      - mês corrente (ou fechado mas calculado ainda DENTRO do mês) → TTL
        curto (HISTORICO_TTL_MES_ABERTO_SEC em painel.py).
    `?force=1` no endpoint apaga as linhas do ano e recalcula tudo — é o
    remédio pra registro retroativo em mês fechado (venda/lead atrasado).
    """

    __tablename__ = "historico_cache"

    ano_mes: Mapped[str] = mapped_column(String(7), primary_key=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class BuscaImovelJob(Base):
    """Job assíncrono de busca de imóvel de TERCEIROS na web (migration 0064).

    A busca roda no imovel-fetcher do Mac mini (Playwright/Chromium via
    Vpn) e é LENTA por natureza — abrir Chromium e raspar 2 portais leva
    dezenas de segundos. Antes, o endpoint era síncrono e a corretora ficava
    presa na tela esperando. Agora o POST cria uma linha aqui, devolve o `id`
    na hora e um worker em background preenche `resultado`/`status`.

    Ciclo: rodando → pronto | erro. Nunca fica "rodando" pra sempre: o worker
    tem timeout e a leitura considera job estagnado (heartbeat velho) como erro.

    Dedupe: (usuario, fingerprint, status='rodando') tem índice único parcial —
    a mesma corretora pedindo a MESMA busca que já está rodando reaproveita o
    job em vez de abrir outra sessão de browser no Mac.

    Isto é SUGESTÃO pra corretora. NUNCA envia nada pro lead.
    """

    __tablename__ = "busca_imovel_jobs"
    __table_args__ = (
        Index("ix_busca_imovel_jobs_usuario_criado", "usuario", "criado_em"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    usuario: Mapped[str] = mapped_column(Text, nullable=False)
    corretora_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    texto: Mapped[str] = mapped_column(Text, nullable=False)
    # sha256 dos critérios normalizados — chave do dedupe
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    # rodando | pronto | erro
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'rodando'"))
    resultado: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    erro: Mapped[str | None] = mapped_column(Text, nullable=True)
    criado_em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # batido pelo worker enquanto trabalha; job "rodando" com heartbeat velho
    # (worker morto / restart do processo) é tratado como erro na leitura.
    heartbeat_em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    concluido_em: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class PushInscricao(Base):
    """Aparelho da corretora inscrito pra receber aviso de lead novo.

    Uma corretora pode ter vários (celular + desktop) — a chave é o `endpoint`
    do navegador, não ela. Ver migration 0071.
    """

    __tablename__ = "push_inscricoes"
    __table_args__ = (
        UniqueConstraint("endpoint", name="uq_push_inscricoes_endpoint"),
        Index("ix_push_inscricoes_corretora", "tenant_id", "corretora_id", "ativa"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    # NULL = admin: recebe TODO lead novo, não só o de uma corretora.
    corretora_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # login de quem inscreveu o aparelho (só faz diferença pra admin, que não
    # tem corretora_id pra identificar).
    usuario: Mapped[str | None] = mapped_column(Text, nullable=True)
    endpoint: Mapped[str] = mapped_column(Text, nullable=False)
    p256dh: Mapped[str] = mapped_column(Text, nullable=False)
    auth: Mapped[str] = mapped_column(Text, nullable=False)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    ativa: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    desativada_motivo: Mapped[str | None] = mapped_column(Text, nullable=True)
    criada_em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    ultimo_envio_em: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ConversaEncerrada(Base):
    """"Feito" do Meu Dia: a conversa acabou com a mensagem do LEAD.

    Quando a última mensagem do lead É o fim do assunto ("fechado, obrigada!"),
    não há o que responder — mas o bloco "responderam e esperam você" só apaga
    quando a corretora manda alguma coisa. Sem esta saída ela mandava um joinha
    só pra limpar a tela, que é trabalho falso.

    `encerrado_ate` é o INSTANTE da última mensagem no clique, não um booleano:
    é o que faz a conversa voltar sozinha se a pessoa escrever de novo (a
    mensagem nova é mais recente que a marca). Ver migration 0073.
    """

    __tablename__ = "conversa_encerrada"
    __table_args__ = (
        UniqueConstraint("tenant_id", "p8", name="uq_conversa_encerrada_p8"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    # últimos 8 dígitos do telefone — mesma chave que o bloco do Meu Dia usa
    p8: Mapped[str] = mapped_column(Text, nullable=False)
    encerrado_ate: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    corretora_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    autor: Mapped[str | None] = mapped_column(Text, nullable=True)
    criado_em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class PessoaRetomada(Base):
    """Turno da Pessoa (linha da Corretor) que ficou sem resposta porque NENHUM
    provider de LLM respondeu — vai pra cá em vez de morrer (migration 0078).

    Caso de 05/09/2026: Claude (janela de 5h) e codex (cota até dia 7) caíram
    juntos; a Pessoa respondeu "probleminha técnico" 6x e nunca voltou. Um lead
    tinha dito "Quero", outra pediu "apartamento 2 quartos acima de 70m²".
    A mensagem do lead fica aqui, com backoff, até um provider voltar.

    Ciclo: pendente → feita | cancelada (humano assumiu / handoff) | desistiu
    (estourou MAX_TENTATIVAS — e a Dona é avisada). 1 pendente por telefone:
    mensagens seguidas do mesmo lead se juntam no mesmo item (índice parcial).
    """

    __tablename__ = "pessoa_retomadas"
    __table_args__ = (
        Index("ix_pessoa_retomadas_status_proxima", "status", "proxima_em"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    telefone: Mapped[str] = mapped_column(Text, nullable=False)
    nome: Mapped[str | None] = mapped_column(Text, nullable=True)
    # o que o lead escreveu — NUNCA se perde; linhas novas = mensagens seguidas
    texto: Mapped[str] = mapped_column(Text, nullable=False)
    erro: Mapped[str | None] = mapped_column(Text, nullable=True)
    # pendente | feita | cancelada | desistiu
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pendente'"))
    tentativas: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    proxima_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resultado: Mapped[str | None] = mapped_column(Text, nullable=True)
    criado_em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    atualizado_em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ImovelCaptado(Base):
    """Imóvel de DONO captado pela corretora — Camada 1 (migration 0080).

    O ativo próprio da Acme. Imóvel de dono chega por WhatsApp toda semana
    ("ah, eu tenho esse imóvel pra vender") e hoje morre na conversa, porque as
    corretoras não têm onde cadastrar. Nenhum portal tem esses; nenhum
    concorrente tem esses.

    🔴 `corretora_id` é NOT NULL: UMA corretora é dona da captação. Comissão
    nasce daqui (`acme-comissoes-caixa-vs-crm`), então trocar de dona é
    EVENTO ASSINADO em `imovel_evento`, nunca UPDATE silencioso.

    `captado_por` é o login de quem digitou — pode ser um admin cadastrando no
    lugar da corretora. Os dois campos existem porque respondem perguntas
    diferentes: quem ganha a comissão × quem apertou o botão.

    Nada aqui pode fazer o cadastro falhar. Só 4 campos são obrigatórios
    (tipo, bairro, dono_telefone e o preço quando houver); o resto é cobrado
    depois pela `completude`. A tela é preenchida no meio da conversa com o
    dono, do celular — um 422 ali custa o imóvel.
    """

    __tablename__ = "imoveis_captados"
    __table_args__ = (
        Index("ix_imoveis_captados_corretora", "tenant_id", "corretora_id", "situacao"),
        Index("ix_imoveis_captados_captado_em", "tenant_id", "captado_em"),
        Index("ix_imoveis_captados_bloco", "tenant_id", "cidade", "bairro", "tipo", "quartos"),
        Index("ix_imoveis_captados_dono_tel", "dono_telefone_normalizado"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    corretora_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("corretoras.id", ondelete="RESTRICT"), nullable=False
    )
    captado_por: Mapped[str] = mapped_column(Text, nullable=False)
    captado_em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # apartamento | casa | terreno | comercial | chacara | outro
    tipo: Mapped[str] = mapped_column(Text, nullable=False)
    quartos: Mapped[int | None] = mapped_column(Integer, nullable=True)
    vagas: Mapped[int | None] = mapped_column(Integer, nullable=True)
    suites: Mapped[int | None] = mapped_column(Integer, nullable=True)
    area_m2: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    bairro: Mapped[str] = mapped_column(Text, nullable=False)
    cidade: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'Cidade'"))
    endereco: Mapped[str | None] = mapped_column(Text, nullable=True)
    cep: Mapped[str | None] = mapped_column(Text, nullable=True)
    referencia: Mapped[str | None] = mapped_column(Text, nullable=True)
    preco_pedido: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    # NULL = não perguntei; false = perguntei e é não. A diferença importa na
    # ficha ("falta perguntar" × "não aceita").
    aceita_financiamento: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    aceita_permuta: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    dono_nome: Mapped[str | None] = mapped_column(Text, nullable=True)
    dono_telefone: Mapped[str] = mapped_column(Text, nullable=False)
    dono_telefone_normalizado: Mapped[str | None] = mapped_column(Text, nullable=True)
    dono_relacao: Mapped[str | None] = mapped_column(Text, nullable=True)

    situacao: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'disponivel'")
    )
    exclusividade: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    exclusividade_ate: Mapped[date | None] = mapped_column(Date, nullable=True)
    # O ativo é a autorização assinada, não a linha no banco (revisão 12/09).
    autorizacao_assinada: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    fonte: Mapped[str | None] = mapped_column(Text, nullable=True)

    lead_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    observacoes: Mapped[str | None] = mapped_column(Text, nullable=True)
    completude: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    duplicata_de: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("imoveis_captados.id", ondelete="SET NULL"), nullable=True
    )
    # Camada 3 (cruzamento com anúncio de portal). Arquivada — nasce e fica NULL.
    imovel_mercado_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    atualizado_em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ImovelFoto(Base):
    """Foto do imóvel captado (migration 0080).

    🔴 `origem` só aceita 'dono' e 'corretora'. **Não existe 'portal'**, e o
    CHECK no banco garante isso. Foto de anúncio de terceiro não é do VivaReal
    nem nossa — é do fotógrafo ou da imobiliária que anunciou; tirar marca
    d'água e republicar gera notificação com o nome da Acme e o da agencia
    (§3 do plano). A recusa virou schema pra não depender de ninguém lembrar.

    Tratamento (hoje: logo da Acme) NUNCA sobrescreve a original — nasce como
    linha nova com `tratada=true` e `tratada_de_id` apontando pra ela. Retirar
    uma foto de circulação é `arquivada=true`, não DELETE.
    """

    __tablename__ = "imovel_foto"
    __table_args__ = (
        Index("ix_imovel_foto_imovel", "imovel_id", "arquivada", "ordem"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    imovel_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("imoveis_captados.id", ondelete="CASCADE"), nullable=False
    )
    ordem: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    # caminho RELATIVO ao diretório de mídia (settings.imoveis_media_dir)
    arquivo: Mapped[str] = mapped_column(Text, nullable=False)
    mime: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'image/jpeg'"))
    bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    largura: Mapped[int | None] = mapped_column(Integer, nullable=True)
    altura: Mapped[int | None] = mapped_column(Integer, nullable=True)
    hash_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    origem: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'dono'"))
    autorizacao_dono: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    tratada: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    tratada_de_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("imovel_foto.id", ondelete="SET NULL"), nullable=True
    )
    tratamento: Mapped[str | None] = mapped_column(Text, nullable=True)
    arquivada: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    enviado_por: Mapped[str | None] = mapped_column(Text, nullable=True)
    criado_em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ImovelEvento(Base):
    """Histórico append-only do imóvel captado (migration 0080).

    Existe por um motivo só: `corretora_id` define comissão. Transferência de
    captação, mudança de situação e edição de preço precisam deixar rastro com
    autor e horário — UPDATE silencioso num campo desses vira briga interna e
    ninguém consegue reconstruir o que aconteceu.
    """

    __tablename__ = "imovel_evento"
    __table_args__ = (Index("ix_imovel_evento_imovel", "imovel_id", "em"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    imovel_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("imoveis_captados.id", ondelete="CASCADE"), nullable=False
    )
    # criado | editado | captacao_transferida | situacao_mudou | preco_mudou
    # | foto_adicionada | foto_tratada | foto_arquivada
    tipo: Mapped[str] = mapped_column(Text, nullable=False)
    de: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    para: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    por: Mapped[str] = mapped_column(Text, nullable=False)
    nota: Mapped[str | None] = mapped_column(Text, nullable=True)
    em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class OptOutConferencia(Base):
    """Linha de conferência humana do opt-out (migration 0083).

    A regra (`app/services/opt_out.py`) decide e grava sozinha. Esta tabela é o
    outro lado: o que a SEGUNDA OPINIÃO (`app/services/opt_out_corretor.py`) tem a
    dizer sobre a decisão dela, pra alguém olhar. Nada aqui muda estado de
    ninguém — nenhuma linha desta tabela descadastra, reativa ou move card.

    Dois tipos, os dois medidos em 21/09/2026 (RELATORIO-CORRETOR-OPTOUT.md):

      veto — a regra marcou opt-out e o classificador discorda (nota < 0,50).
             O opt-out CONTINUA valendo; a linha só pede uma conferida. Foram 7
             em 14 meses, e os 7 eram falso positivo da regra (lead vivo que
             sumia do funil).
      rede — a regra não marcou e o classificador pontuou alto (nota ≥ 0,85).
             São 5 pedidos legítimos em 14 meses que hoje seguem recebendo
             disparo. Viram conferência, nunca `registrar()` automático.

    Por que guardar `texto` e `enunciado` na linha: quem confere precisa ler a
    fala pra julgar, e precisa saber COM QUE PERGUNTA aquela nota foi tirada —
    a nota é propriedade do enunciado (mudar a frase move até 0,72), então uma
    linha antiga sem o enunciado dela é um número sem unidade.
    """

    __tablename__ = "opt_out_conferencia"
    __table_args__ = (
        # A fila é lida por "o que ainda não foi conferido, mais novo primeiro".
        Index("ix_opt_out_conferencia_pendente", "conferido_em", "criado_em"),
        # Uma conferência por (mensagem, tipo): varredura que roda de novo não
        # vira duas linhas iguais na fila. Parcial porque `mensagem_id` pode ser
        # nula (fala avulsa, fora da tabela de mensagens) e NULL não colide com
        # NULL — sem o WHERE o índice não travaria nada ali. Espelha a migration
        # 0083; os dois precisam bater, senão a suíte (que monta o schema por
        # `create_all`) testa um banco diferente do de produção.
        Index("uq_opt_out_conferencia_msg", "mensagem_id", "tipo", unique=True,
              postgresql_where=text("mensagem_id IS NOT NULL")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    # 'veto' | 'rede'
    tipo: Mapped[str] = mapped_column(Text, nullable=False)
    telefone: Mapped[str | None] = mapped_column(Text, nullable=True)
    # `mensagens_whatsapp.id`. Sem FK de propósito: a linha de conferência é um
    # registro de auditoria e não pode sumir junto se a mensagem for expurgada.
    mensagem_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    texto: Mapped[str | None] = mapped_column(Text, nullable=True)
    nota: Mapped[Decimal | None] = mapped_column(Numeric(4, 3), nullable=True)
    #: O que a REGRA decidiu — o que vale. Guardado pra a conferência saber o
    #: que está em discussão sem ter que reprocessar a fala.
    regra_marcou: Mapped[bool] = mapped_column(Boolean, nullable=False)
    modelo: Mapped[str | None] = mapped_column(Text, nullable=True)
    enunciado: Mapped[str | None] = mapped_column(Text, nullable=True)
    criado_em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    conferido_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    conferido_por: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: null = ninguém olhou ainda; 'procede' | 'nao_procede'. É registro do que
    #: a humana achou — quem age depois é ela, na mão, pelo caminho de sempre.
    veredito: Mapped[str | None] = mapped_column(Text, nullable=True)
