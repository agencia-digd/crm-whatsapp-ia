import structlog
from agno.agent import Agent
from agno.db.postgres import PostgresDb
from agno.models.anthropic import Claude
from agno.os import AgentOS

from app.config import settings
from app.observability import init_observability
from app.tools.telegram import TelegramTool

log = structlog.get_logger()

# Antes de montar o app: erro de qualquer rota/worker já nasce instrumentado.
# Sem GLITCHTIP_DSN é no-op — o app sobe igual.
glitchtip_ativo = init_observability()


def build_app() -> AgentOS:
    db = PostgresDb(db_url=settings.database_url)

    agente_assistente_v0 = Agent(
        name="agente-assistente-v0",
        id="agente-assistente-v0",
        model=Claude(id="claude-haiku-4-5"),
        db=db,
        tools=[TelegramTool()],
        description="Quando chamado, mande uma mensagem amigável pra Dona via TelegramTool.",
        instructions=[
            "Você é o Copiloto Imobiliário.",
            "Quando receber uma solicitação, use a ferramenta send_to_dona para enviar uma mensagem amigável à Dona.",
        ],
        add_history_to_context=True,
        markdown=True,
    )

    agent_os = AgentOS(agents=[agente_assistente_v0])
    return agent_os


agent_os = build_app()
app = agent_os.get_app()

# Onda 2 — endpoints custom (Assistente + webhook Evolution)
from app.api.assistente import router as assistente_router  # noqa: E402
from app.api.evolution_webhook import router as evolution_webhook_router  # noqa: E402
from app.api.admin import router as admin_router  # noqa: E402
from app.api.painel import router as painel_router  # noqa: E402
from app.api.telegram_webhook import router as telegram_router  # noqa: E402
from app.api.kommo_webhook import router as kommo_webhook_router  # noqa: E402
from app.api.recovery_dash import router as recovery_dash_router  # noqa: E402
from app.api.hunter_api import router as hunter_router  # noqa: E402
from app.api.posts_corretora import router as posts_corretora_router  # noqa: E402
from app.api.cloud_corretora_a import router as cloud_router  # noqa: E402
from app.api.cloud_corretora import router as cloud_corretora_router  # noqa: E402
from app.api.cloud_assistente import router as cloud_assistente_router  # noqa: E402
from app.api.aprendiz import router as aprendiz_router  # noqa: E402
from app.api.assistente_copilota import router as assistente_copilota_router  # noqa: E402
from app.api.assistente_busca_web import router as assistente_busca_web_router  # noqa: E402
from app.api.busca_imovel import router as busca_imovel_router  # noqa: E402
from app.api.intake import router as intake_router  # noqa: E402
from app.api.meta_leadgen import router as meta_leadgen_router  # noqa: E402
from app.api.meta_webhook import router as meta_webhook_router  # noqa: E402
from app.api.automacoes import router as automacoes_router  # noqa: E402
from app.api.automacoes_nutricao import router as automacoes_nutricao_router  # noqa: E402
from app.api.automacoes_boas_vindas import router as automacoes_bv_router  # noqa: E402
from app.api.imoveis_captados import router as imoveis_captados_router  # noqa: E402
from app.api.visao_atividade import router as visao_atividade_router  # noqa: E402
from app.api.lembrete_visita_d1 import router as lembrete_visita_d1_router  # noqa: E402
from app.api.instagram_acme_webhook import router as instagram_acme_router  # noqa: E402

app.include_router(cloud_router)
app.include_router(cloud_assistente_router)
app.include_router(cloud_corretora_router)
app.include_router(aprendiz_router)
app.include_router(assistente_copilota_router)
app.include_router(assistente_busca_web_router)
app.include_router(busca_imovel_router)
app.include_router(assistente_router)
app.include_router(evolution_webhook_router)
app.include_router(admin_router)
app.include_router(painel_router)
app.include_router(telegram_router)
app.include_router(kommo_webhook_router)
app.include_router(recovery_dash_router)
app.include_router(hunter_router)
app.include_router(posts_corretora_router)
app.include_router(intake_router)
app.include_router(meta_leadgen_router)
app.include_router(meta_webhook_router)
app.include_router(automacoes_router)
app.include_router(automacoes_nutricao_router)
app.include_router(automacoes_bv_router)
# Camada 1 do banco de imóveis (captação da corretora). Prefixo /painel, mas
# router próprio: painel.py já tem 10k linhas e captação é domínio novo.
app.include_router(imoveis_captados_router)
# Lembrete de véspera (D-1) da visita — leitura das métricas e das pendências.
# Router próprio: é automação nova e o painel.py já passa de 10k linhas.
app.include_router(lembrete_visita_d1_router)
# Visão: mensagens e ligações por corretora (06/10). Router próprio, só leitura.
app.include_router(visao_atividade_router)

# Porta de entrada do Instagram da Acme (tarefa 4A). Recebe o REPASSE do
# painel-corretor — a Meta aceita um callback por objeto/app, e o objeto `instagram`
# do app 0000 aponta pra lá. MODO SOMBRA: grava e normaliza, não responde
# comentário, não manda DM, não cadastra lead. Os routers de cadastro e envio
# continuam FORA daqui, e há teste que afirma isso.
#
# Desligar não é mexer neste arquivo: é apagar a linha IG_ACME de
# /etc/agencia/corretor-rotas.env e reiniciar o corretor-painel. Sem ela o repasse não sai, e
# o evento da Acme volta a ficar só gravado em sombra no banco do CORRETOR.
app.include_router(instagram_acme_router)


# Fila de retomada da Pessoa (05/09/2026): turno sem LLM é retentado daqui, em
# thread daemon — não bloqueia o event loop. No import (e não em on_event):
# o app vem do AgentOS com lifespan próprio, e handlers de startup adicionados
# depois não rodam. Sob pytest e com PESSOA_RETOMADA_WORKER=false não sobe.
from app.services.pessoa_retomada import iniciar_worker as _iniciar_worker_pessoa  # noqa: E402

_iniciar_worker_pessoa()


log.info(
    "copiloto_imobiliario_started",
    agents=["agente-assistente-v0"],
    glitchtip=glitchtip_ativo,
    routers=[
        "assistente",
        "evolution_webhook",
        "admin",
        "painel",
        "telegram",
        "kommo_webhook",
        # Porta de entrada do Instagram da Acme, em sombra (21/09/2026). Está
        # aqui porque um log que lista routers e omite um monta um retrato falso
        # de produção — e foi exatamente olhando log que a casa levou 30 dias pra
        # ver que o comentário da Acme chegava e morria.
        "instagram_acme_webhook",
    ],
)
