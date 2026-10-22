import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from pydantic import model_validator
from pydantic_settings import BaseSettings

_log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from app.llm.modelstudio_provider import ModelStudioProvider
    from app.llm.provider import LLMProvider


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg://user:CHANGE_ME@localhost:5432/copiloto"

    anthropic_api_key: str = ""

    llm_provider: str = "cli"  # cli | codex | api | modelstudio

    # Classifier override: usa Qwen 2.5 7B no Mac (Ollama via Vpn).
    # Default vazio = usa llm_provider global (Claude CLI).
    # Quando "ollama", classifier roda local; analyzer continua no llm_provider global.
    classifier_provider: str = ""  # "" | ollama
    ollama_host: str = "http://100.64.0.1:11434"
    ollama_classifier_model: str = "qwen2.5:7b"

    # Kommo — fonte única: /root/.acme-kommo.env (nunca duplicar aqui)
    acme_kommo_env_file: str = "/root/.acme-kommo.env"
    kommo_pipeline_id_main: int = 10000004  # "Funil de Vendas" Acme
    kommo_subdomain: str = ""   # preenchido pelo model_validator abaixo
    kommo_token: str = ""       # preenchido pelo model_validator abaixo

    evolution_api_url: str = ""
    evolution_api_key: str = ""
    # Postgres INTERNO do Evolution API (schema evolution_api) — usado só como
    # fallback pra resolver @lid → telefone real (Message.key.remoteJidAlt) quando
    # o payload do webhook não trouxe o campo. Leitura apenas.
    evolution_db_url: str = "postgresql+psycopg://user:CHANGE_ME@localhost:5432/evolution_db"

    evolution_acme_ativa: bool = False  # Evolution da Acme aposentada em 22/10
    model_classifier: str = "claude-haiku-5-5"
    model_analyzer: str = "claude-sonnet-5-5"
    # Assistente (servico='api'): Haiku no próprio `claude -p` da assinatura — sem API paga e sem
    # Codex (morto por limite). Inspector e sombra continuam no model_analyzer.
    model_assistente: str = "claude-haiku-5-5"
    # Teto POR CHAMADA. O pior caso da tela = roteador x2 (retry) + resposta = 3 x 45 = 135 s,
    # e a tela (acme-painel/app/api/assistente/route.ts) espera 150 s.
    assistente_cli_timeout: int = 45
    # Sombra/Aprendiz (timer copiloto-shadow): `claude -p` da assinatura, SEM reserva Codex
    # (Codex fora por limite; a reserva só dobrava os erros). Juiz do placar usa Haiku.
    model_shadow: str = "claude-sonnet-5-5"
    model_juiz: str = "claude-haiku-5-5"
    shadow_cli_timeout: int = 120
    juiz_cli_timeout: int = 60

    # ── Codex/GPT (llm_provider="codex") ──────────────────────────────────────
    # Migração 28/07: tira o volume da Acme (~440 chamadas/dia) da assinatura
    # PESSOAL da Dona e joga na Codex, que é maior e estava ociosa. Terra ganhou
    # o bloco de voz PT-BR no benchmark F2 — é ele que escreve o que a corretora lê.
    model_analyzer_codex: str = "gpt-5.6-terra"
    model_classifier_codex: str = "gpt-5.6-terra"
    # Vazio = codex LOCAL (preferido). Com host, roda por SSH — só como ponte
    # enquanto o VPS não tiver login próprio: o Mac vira SPOF do sistema da cliente.
    codex_remote_host: str = ""
    codex_timeout: int = 180
    # Rede de proteção: se o codex falhar (login, cota, Mac fora), cai no Claude
    # em vez de derrubar a operação. Desligar só pra medir o Terra puro na sombra.
    codex_fallback_claude: bool = True
    # Rede na direção INVERSA (a que importa hoje, 02/08/2026): o Claude é o
    # primário e roda na assinatura PESSOAL da Dona. Quando a cota dela estoura,
    # o sistema da CLIENTE parava junto — aconteceu em 28/07. Com isto ligado,
    # a operação degrada pro codex sozinha e volta quando a cota renova.
    # Medido: a Acme é só 6-8% do consumo do Claude, então não é ela que
    # estoura a cota — é o trabalho da Dona no Claude Code. Por isso a resposta
    # certa foi a rede, não migrar tudo pro codex.
    cli_reserva_codex: bool = True
    # ÚLTIMA rede (05/09/2026): o Claude estourou a janela de 5h E o codex
    # estava sem cota até o dia 7 — os dois de ASSINATURA caíram juntos, a Pessoa
    # respondeu "probleminha técnico" 6x e engoliu lead quente. O Qwen (Model
    # Studio) é billing por token, não compete com assinatura nenhuma: entra
    # como último recurso quando Claude e codex falham. Só sobe se
    # MODELSTUDIO_API_KEY estiver no .env; sem chave, a rede fica em 2 níveis.
    cli_reserva_modelstudio: bool = True

    # ── Fila de retomada da Pessoa (05/09/2026) ────────────────────────────────
    # Quando NENHUM provider responde, o turno do lead vai pra `pessoa_retomadas`
    # e um worker em thread (dentro do copiloto-api) tenta de novo com backoff.
    # Desligar o worker (ex.: rodar só pelo timer/script): PESSOA_RETOMADA_WORKER=false.
    pessoa_retomada_worker: bool = True
    pessoa_retomada_intervalo_s: int = 120

    # ── Model Studio (Alibaba · Qwen) ─────────────────────────────────────────
    # Billing por TOKEN: tira o peso do copiloto (~230 chamadas/dia) da
    # assinatura pessoal da Dona, que hoje compete com o trabalho dela no
    # Claude Code. Endpoint OpenAI-compatible.
    #
    # ENDPOINT E MODELO NÃO TÊM DEFAULT EM CÓDIGO (05/09/2026): a conta da casa
    # é um workspace MaaS dedicado (ws-….maas.aliyuncs.com, modelo qwen3.8-max),
    # NÃO o DashScope público — contra o público a chave dá 401. O default
    # antigo (dashscope-intl + qwen-plus) escondia isso: o degrau "existia" e
    # não respondia. Fonte dos valores, nesta ordem:
    #   1. MODELSTUDIO_API_KEY / MODELSTUDIO_BASE_URL / MODELSTUDIO_MODEL_* no .env
    #   2. QWEN_API_KEY / QWEN_BASE_URL / QWEN_MODEL em `qwen_env_file`
    #      (/etc/agencia/qwen.env — lido no momento do uso, nunca copiado pro código)
    # A chave NUNCA fica em código nem em log.
    modelstudio_api_key: str = ""
    modelstudio_base_url: str = ""
    modelstudio_model_analyzer: str = ""
    modelstudio_model_classifier: str = ""
    # O workspace serve VÁRIOS fornecedores com a MESMA chave (medido 05/09:
    # qwen3.8-max, qwen3.8-flash, kimi-k3, kimi-k2.5, deepseek-v4-*, glm-5.*).
    # Reserva DENTRO do mesmo endpoint: se o modelo principal falhar (limite do
    # modelo, 5xx só dele), cai pro reserva sem trocar credencial. Ex.: kimi-k3.
    # Vazio = sem reserva. Fallback no arquivo: QWEN_MODEL_RESERVA.
    modelstudio_model_reserva: str = ""
    modelstudio_timeout: int = 120
    qwen_env_file: str = "/etc/agencia/qwen.env"
    # Verificação REAL do degrau antes de entrar na escada (uma chamada de ≤5
    # tokens, cacheada 1h). "Tem chave" não é prova de nada — foi o falso
    # conforto que deixou o 3º degrau morto. Desligar só em drill/teste.
    modelstudio_verificar: bool = True
    modelstudio_verificar_timeout: float = 10.0
    # qwen3 raciocina por padrão e cobra o raciocínio; a Pessoa só usa o content.
    modelstudio_enable_thinking: bool = False
    # Mesma rede de proteção do codex: se o Model Studio cair (chave, cota,
    # rede), degrada pro Claude CLI em vez de derrubar o sistema da CLIENTE.
    modelstudio_fallback_claude: bool = True

    # ── Sombra do LLM (passo 2 do PLANO-ELENCO-FINAL) ─────────────────────────
    # Roda o candidato (Terra) EM PARALELO nas conversas reais, só pra comparar.
    # A resposta que vai pra corretora continua sendo a do primário (Claude).
    # Liga/desliga sem restart: touch/rm no shadow_llm_flag.
    shadow_llm: bool = False
    shadow_llm_flag: str = "/opt/copiloto/.shadow_llm_on"
    shadow_llm_path: str = "/opt/copiloto/shadow_llm.jsonl"
    shadow_llm_model: str = "gpt-5.6-terra"
    # Quem é o candidato na sombra: "codex" (Terra) ou "modelstudio" (Qwen).
    # Trocar pra medir o Qwen: SHADOW_LLM_PROVIDER=modelstudio + SHADOW_LLM_MODEL=qwen-plus.
    shadow_llm_provider: str = "codex"  # codex | modelstudio
    # Teto diário: enquanto a assinatura Codex for $20, ~30 duelos/dia bastam
    # pra medir e não estouram a cota (o volume real é ~220 análises/dia).
    shadow_llm_cap_diario: int = 30

    telegram_bot_token: str = ""
    telegram_dona_chat_id: str = ""
    telegram_webhook_secret: str = ""

    # Onda 2 — Endpoints + Webhook Evolution
    copiloto_internal_token: str = ""
    evolution_webhook_secret: str = ""
    evolution_base_url: str = "http://127.0.0.1:8090"
    evolution_assistente_instance: str = "assistente"
    # Telefone da Assistente (dono da instância assistente) — usado pra rotear webhook por destinatário.
    # Mensagem só dispara o agente se foi endereçada a esse número.
    assistente_phone: str = "+5500000000000"

    # Gateway AI (Google Calendar via OAuth gerenciado) — Assistente
    gateway_api_key: str = ""
    gateway_connection_id: str = ""
    gateway_base_url: str = "https://gateway.gateway.ai/google-calendar/calendar/v3"
    gateway_calendar_id: str = "contato@example.com"
    acme_sheets_cred: str = ""

    gemini_api_key: str = ""
    gemini_transcricao_model: str = "gemini-2.5-flash-lite"

    # Whisper Mac (faster-whisper rodando no Mac mini via Vpn)
    whisper_mac_url: str = "http://100.64.0.1:11435"
    # imovel-fetcher Mac (Playwright/Chromium — busca GRÁTIS de imóveis de
    # terceiros na web, via Vpn). Motor da "Assistente busca na web".
    imovel_fetcher_url: str = "http://100.64.0.1:11441"
    # Provider de transcrição: "whisper_mac" (default, grátis, via Vpn)
    # ou "gemini" (fallback pago, mantido pra compat).
    transcricao_provider: str = "whisper_mac"

    # ── Banco de imóveis · Camada 1 (captação) — migration 0080 ────────────────
    # Onde ficam as fotos dos imóveis captados. Subdiretório do mesmo
    # /var/lib/copiloto-media que já guarda mídia de conversa — nada de bucket
    # novo nem serviço novo (restrição do plano).
    imoveis_media_dir: str = "/var/lib/copiloto-media/imoveis"
    # Logo da Acme usada na marca d'água da foto PRÓPRIA (pedido da Dona
    # 12/09). É diferente da marca d'água de portal que a gente recusou: aqui a
    # foto é da nossa base, veio do dono, e a marca é da própria Acme.
    # PNG com alpha. Vazio ou arquivo ausente = endpoint de logo responde 503
    # com mensagem clara, nunca gera logo improvisada.
    acme_logo_path: str = "app/assets/acme-logo.png"
    # Lado maior da foto guardada, em px. O front já reduz antes de subir; isto
    # é a rede de segurança pra quem manda direto na API.
    imoveis_foto_lado_max: int = 1600

    # ── Segunda opinião do corretor no opt-out (21/09/2026) ───────────────────────
    # A regra (`app/services/opt_out.py`) continua decidindo sozinha; o corretor só
    # pode DISCORDAR (veto) ou APONTAR (rede), e os dois viram linha de
    # conferência humana. Medição e cortes: RELATORIO-CORRETOR-OPTOUT.md.
    # Nasce DESLIGADO — com false nem a chave é lida, nem chamada sai.
    # Enunciado, modelo, endpoint e cortes NÃO estão aqui de propósito: são
    # propriedade da medição e moram em `app/services/opt_out_corretor.py`, onde
    # mudá-los aparece no diff. Ver a nota do enunciado congelado lá.
    opt_out_corretor_ativo: bool = False
    # Chave do OpenRouter. Vazia = lê `OPENROUTER_API_KEY` do arquivo abaixo no
    # MOMENTO DO USO (root-only, 0600). Nunca em código, nunca em log.
    openrouter_api_key: str = ""
    openrouter_env_file: str = "/etc/assistente/openrouter.env"
    # Latência medida: 0,6 s/chamada. 15 s é o fusível — estourou, falha aberta.
    opt_out_corretor_timeout: float = 15.0

    dry_run: bool = True
    log_level: str = "INFO"
    env: str = "development"  # ENV=production no deploy (ver docs/DEPLOY.md)

    # Observabilidade — GlitchTip (Sentry-compatible). DSN vazio = SDK desligado,
    # app sobe igual. Ver app/observability.py e .env.example.
    glitchtip_dsn: str = ""
    glitchtip_traces_sample_rate: float = 0.0

    # Dual-write EM SOMBRA da espinha canônica do lead (Opção A · leads/pipeline_stages/
    # lead_events). Quando True, todo lead que entra/atualiza também é gravado nas tabelas
    # canônicas nossas — best-effort, sem mudar a leitura (Kommo segue como fonte). Flag de
    # kill-switch: LEAD_SPINE_SHADOW=false desliga o shadow sem tocar código (reversível).
    lead_spine_shadow: bool = True

    # Recovery (Corretor · funil Remarketing) — instância Evolution + IDs Kommo
    evolution_recuperador_instance: str = "recuperador"
    evolution_recuperador_phone: str = ""
    kommo_pipeline_id_remarketing: int = 0
    kommo_stage_remarketing_entrada: int = 0
    kommo_stage_remarketing_api: int = 0
    kommo_stage_remarketing_em_atendimento_ia: int = 0
    kommo_stage_remarketing_respondida: int = 0
    kommo_stage_remarketing_perdida: int = 0

    # Dispatcher HSM (Meta Cloud API) — número Cloud API e template aprovado
    meta_cloud_phone_number_id: str = ""           # ex: "000000000000000"
    meta_cloud_access_token: str = ""              # token de System User com whatsapp_business_messaging
    meta_cloud_api_version: str = "v22.0"
    recovery_hsm_template_name: str = ""           # nome do template Meta aprovado
    recovery_hsm_language: str = "pt_BR"
    recovery_hsm_has_name_param: bool = True       # se True, manda {{1}} = primeiro nome do lead
    recovery_dispatch_default_limit: int = 50
    recovery_dispatch_delay_ms: int = 1500         # entre disparos pra evitar throttle Meta

    # Anti-ban do disparo de reengajamento (linha da Pessoa12, 2591 recém-recuperado).
    disparo_cap_diario: int = 50           # máx de envios reais por dia
    disparo_cooldown_dias: int = 14        # não redisparar pro mesmo tel nesse período
    disparo_max_tentativas: int = 2        # máx de disparos sem resposta por telefone
    disparo_min_intervalo_s: int = 60      # espaçamento mínimo entre envios reais

    # Onda 7 · Fase 3C — destinatário paralelo (Pessoa Pessoa9) via Evolution
    pessoa_whatsapp: str = ""           # ex: "+5500000000000" (vazio = desabilita)
    system_instance: str = "assistente"      # instância Evolution usada pra enviar pra Pessoa

    # Alexa (Voice Monkey) — anúncio de lead novo na Acme
    voicemonkey_token: str = ""        # access_token do voicemonkey.io
    voicemonkey_device: str = ""       # nome do monkey/device (ex: "cozinha")
    kommo_webhook_secret: str = ""     # segredo na URL do webhook Kommo

    # Voz da Assistente no Mac Mini — substitui a Alexa no aviso de lead (Dona 26/07).
    # Quando ligado, o aviso sai pela caixinha do Mac (SSH `say`) em vez do Alexa.
    mac_voz_enabled: bool = False              # kill-switch; liga com MAC_VOZ_ENABLED=true
    mac_voz_ssh: str = "contato@example.com"  # alvo SSH do Mac (Vpn)
    mac_voz_voice: str = "Luciana"             # voz PT-BR do macOS `say`
    mac_voz_chime: str = "Glass"               # toque antes da voz (som /System/Library/Sounds); vazio = sem toque

    # Bridge de notificação da Assistente (telegram-claude · POST /api/notify).
    # Usado pra avisar a Dona quando o briefing das 20h falha (fix E1).
    notify_api_url: str = "http://127.0.0.1:8099/api/notify"
    notify_api_token: str = ""                         # se vazio, lê de notify_bridge_env_file
    notify_bridge_env_file: str = "/opt/telegram-claude/.env"  # fonte do NOTIFY_API_TOKEN

    # ── Cadência híbrida (P2) — state machine de 8 toques + remarketing ──────────
    # Motor que decide, por lead na cadência, se há toque devido hoje e enfileira
    # (NUNCA envia direto). Mensagens vão pro disparo em DRY-RUN atrás de settings.dry_run;
    # tarefas de ligação geram registro de tarefa (cadencia_eventos tipo='tarefa_ligacao').
    cadencia_ativa: bool = True                        # kill-switch do motor de cadência
    # Dia útil (02/10/2026, motor portado da DEX): cadência, nutrição e mensagem
    # planejada não saem no sábado nem no domingo (dia de São Paulo). Nada se
    # perde: os três pegam o que está devido (`<= agora`), então o que cair no
    # fim de semana sai no primeiro tick de segunda. Chave de painel
    # `envio_dias_uteis` ganha desta; esta é o fallback (env ENVIO_DIAS_UTEIS).
    envio_dias_uteis: bool = True
    # Sugestão da IA no follow-up (29/07): a IA lê a conversa real e escreve a
    # variável {{2}} do template — mas só sai depois de um humano APROVAR no
    # painel. Default DESLIGADO; liga por env SUGESTAO_IA_CADENCIA=true.
    # É o kill-switch: com false a cadência nem olha a fila, usa o template de
    # sempre. Desligar não precisa de deploy.
    sugestao_ia_cadencia: bool = False
    # Matrícula automática: lead novo do webhook Kommo atribuído à Pessoa12 (linha Cloud
    # 2591) entra na cadência sozinho. Default DESLIGADO — deploy seguro; liga por
    # env CADENCIA_AUTO_ENROLL=true com OK explícito da Dona.
    cadencia_auto_enroll: bool = False
    cadencia_cap_diario: int = 50                      # máx mensagens enfileiradas/dia (anti-ban)
    # Cap por CORRETORA (id → máx/dia). Sobrepõe o cadencia_cap_diario pra quem
    # está listado; quem não está usa o valor global. Decisão da Dona 27/07:
    #   Pessoa12 (3) fica em 25 — chip novo, 2 bans em julho, regime de recuperação.
    #   Pessoa10 (1) e Pessoa11 (2) têm número PESSOAL com 2+ anos de uso orgânico: risco
    #   de ban baixo, e o cap ali é fusível contra BUG nosso (loop de envio), não
    #   freio de volume. 200 é folgado e ainda abaixo do teto que a própria Meta
    #   aplica (~250 conversas iniciadas/24h em número novo no Cloud).
    # Formato no .env: CADENCIA_CAP_POR_CORRETORA={"1": 200, "2": 200, "3": 25}
    cadencia_cap_por_corretora: dict[str, int] = {}
    # Linhas em RAMPA de aquecimento: corretora_id → slug da linha.
    # A rampa (app/services/rampa.py) limita o cap do dia abaixo do configurado
    # enquanto o número aquece, e só avança com qualidade GREEN na Meta.
    # Sai daqui quando a linha estabiliza no teto — aí vale o cap normal.
    # 4 = Corretor (0000-0000), número da casa, entrou em coexistência 30/07
    # (exibia 0000-0000 até 15/09/2026 — mesma WABA, mesmo phone_number_id).
    rampa_slugs: dict[str, str] = {"4": "corretor"}
    # ── Carrossel da Corretor (remarketing em massa pelo 0000-0000) ──
    # Fila → Disparado → volta em ~30 dias. Quanto sai por dia é a RAMPA.
    # Nasce DESLIGADO: nada roda até a Dona ligar (CARROSSEL_CORRETOR_ATIVO=true).
    carrossel_corretor_ativo: bool = False
    # Dias de silêncio exigidos depois que um HUMANO da casa falou com o número
    # em outra linha. Dona 31/07: ela conversa com fornecedor/parceiro que está
    # na base do 0000 e a Corretor mandava disparo por cima, no meio da conversa.
    # 7 dias é cirúrgico: hoje pega 2 pessoas das 5.173 na fila (39% da base já
    # falou com humano ALGUMA vez — por isso a janela, e não "já falou").
    carrossel_pausa_conversa_humana_dias: int = 7
    # Dona 30/07: a mensagem SE APRESENTA como Acme e pronto. Nada de "Acme,
    # agora Acme" — o número ainda mostra o nome antigo pro lead (coexistência,
    # só muda no aparelho), mas ela é categórica: essa base não vai lembrar da
    # marca antiga, e explicar transição só gasta linha e levanta pergunta.
    carrossel_corretor_texto: str = (
        "Aqui é da Acme Imóveis. Passando pra saber se você ainda está "
        "procurando imóvel em Cidade — apareceram opções novas essa semana. "
        "Quer que eu te mande?"
    )
    # Pessoa atende quem responde o carrossel da Corretor (texto livre, janela 24h).
    atendimento_corretor_ativo: bool = False
    # Handoff automático do remarketing (05/09): lead da linha da Corretor que mostra
    # interesse real vira card da corretora humana na hora, sem WhatsApp. O
    # destino é lido em runtime via config_operacional (painel > este default);
    # 3 = Pessoa3 hoje, mas a roleta muda — por isso não é constante no código.
    handoff_remarketing_ativo: bool = True
    handoff_remarketing_corretora: int = 3
    # Roleta do REMARKETING (18/09): o lead que sai da Corretor é dividido entre as
    # três — Pessoa10(1), Pessoa11(2) e Pessoa12(3). É uma roleta PRÓPRIA, separada da
    # `roleta_corretoras` (lead novo do Meta), porque a regra de 26/07 continua
    # valendo: lead novo NÃO vai pra Pessoa12. Lista vazia = usa o destino único de
    # `handoff_remarketing_corretora` (comportamento anterior).
    handoff_remarketing_roleta: list[int] = [1, 2, 3]
    cadencia_reserva_followup: int = 20                # vagas do cap GARANTIDAS pro follow-up (nutrição para antes)
    cadencia_cooldown_dias: int = 30                   # janela do remarketing (1 msg / 30 dias)
    cadencia_min_intervalo_s: int = 60                 # espaçamento mínimo entre enfileiramentos reais
    # (aposentado 10/07: cadencia_aviso_resposta — Dona proibiu aviso interno
    #  via WhatsApp; o aviso de "lead respondeu" é só in-app.)
    # Automação 24h (regra Dona 10/07): lead em "Em atendimento" MUDO há 24h+
    # (última msg trocada em mensagens_whatsapp é NOSSA — se o lead falou por
    # último, quem deve resposta é a corretora e o lead NÃO se move) → volta pra
    # Follow-up, que liga a cadência sozinho (auto-enroll). Só leads da Pessoa12.
    # Default DESLIGADO — mover pra Follow-up com auto-enroll dispara cadência
    # REAL; liga por env CADENCIA_AUTO_FOLLOWUP_24H=true com OK explícito da Dona.
    cadencia_auto_followup_24h: bool = False
    cadencia_auto_followup_horas: int = 24             # janela de mudez
    cadencia_auto_followup_cap: int = 30               # máx moves por tick (segurança)

    # ── CRM 2.0 — trava do piloto ────────────────────────────────────────────────
    # Corretoras com CRM ativo: só os leads delas aparecem no kanban
    # (/painel/crm/board). A sombra segue gravando TODAS na espinha — a trava é
    # só de exibição. Default [3] = Pessoa12 (piloto). Lista vazia = sem trava.
    # Env: CRM_CORRETORAS_ATIVAS='[3]' (JSON).
    # 01/10/2026 (decisão da Dona): entram Pessoa7 (7) e Corretor (8). As duas adotam
    # lead na janela de ligação, o lead nasce com elas como donas e ficava
    # invisível no kanban pra todo mundo (Fulano, lead 681; Fulano, 728).
    # Cada uma vê o próprio funil; a Pessoa12 vê os delas por delegação — ver
    # DELEGACAO_ATENDIMENTO em app/api/painel.py.
    crm_corretoras_ativas: list[int] = [1, 2, 3, 7, 8]
    # Roleta de LEAD NOVO do intake (round-robin). Regra da Dona 26/07: Pessoa10(1)
    # e Pessoa11(2); Pessoa12(3) fora (linha 2591 = remarketing). Ajustes do dia a dia
    # ("hoje só a Pessoa11") = editar ROLETA_CORRETORAS no .env + restart, sem código.
    roleta_corretoras: list[int] = [1, 2]  # V2 aberto pras 3 (22/07); era [3] no piloto só-Pessoa12

    # ── Lembrete de pendência da Assistente (registro incompleto) ──────────────────
    # Cobra a corretora UMA vez no dia seguinte quando a Assistente pediu info de
    # registro (visita/venda) e a corretora nunca respondeu (incidente Pessoa10/
    # Guilherme 22/07/2026: venda de R$ 423k ficou 2 dias fora do painel).
    # Default DESLIGADO — sem LEMBRETE_PENDENCIA_ATIVO=true o script só LOGA o
    # que enviaria; envio real também exige DRY_RUN=false. Ativar o timer
    # copiloto-lembrete-pendencia é decisão da Dona (P003).
    lembrete_pendencia_ativo: bool = False

    # ── Entrada do lead (Copiloto 2.0 · P0) — POST /intake/lead ─────────────────
    # Porta de entrada do lead novo (webhook Meta/Kommo): upsert na espinha
    # canônica etapa "Novo" + roleta (só Pessoa12 por ora) + matrícula na cadência +
    # lead_events(tipo='intake'). Default DESLIGADO — deploy seguro; liga por
    # env LEAD_INTAKE_ATIVO=true com OK explícito da Dona.
    lead_intake_ativo: bool = False
    # Token do header X-Intake-Token do POST /intake/lead. Fail-closed: vazio =
    # endpoint FECHADO (401) — mesmo padrão de cloud_webhook_secret.
    intake_token: str = ""

    # ── Receptor Meta leadgen em MODO SOMBRA (B6) — POST /intake/meta-leadgen/{secret} ──
    # Secret do path do receptor. Fail-closed: vazio = endpoint FECHADO (401).
    meta_leadgen_secret: str = ""
    # Fonte do META_ACCESS_TOKEN usado pra buscar o leadgen na Graph API — reusa
    # o .env do automacao-kommo (app 0000), que já busca leadgen hoje. Lido em
    # runtime pelo serviço (nunca logado).
    meta_leadgen_env_file: str = "/opt/acme-kommo/.env"

    # ── Receptor REAL do webhook de Lead Ads da Meta — GET+POST /intake/meta-webhook ──
    # É o callback DIRETO da Meta (substitui o Kommo como intake). GET faz a
    # verificação do webhook (hub.challenge); POST valida X-Hub-Signature-256 e
    # processa os eventos leadgen (Graph API → intake_lead). Atrás da MESMA flag
    # lead_intake_ativo — POST responde 200 "desligado" sem processar quando off
    # (nunca 500 pra Meta não desativar a assinatura). O GET de verificação
    # funciona mesmo com a flag off (senão não dá pra assinar o webhook).
    #
    # Verify token do painel da Meta (Webhooks → Verify Token). Fail-closed:
    # vazio = GET de verificação recusa (403).
    intake_meta_verify_token: str = ""
    # App Secret do app Meta — valida o HMAC-SHA256 do corpo (X-Hub-Signature-256).
    # Fail-closed: vazio = POST recusa toda entrega (403). NUNCA logar.
    intake_meta_app_secret: str = ""
    # Page Access Token com leads_retrieval — busca os dados do lead na Graph API
    # (GET /{leadgen_id}). Se vazio, cai no META_ACCESS_TOKEN do meta_leadgen_env_file
    # (reuso do token do automacao-kommo). Lido em runtime, NUNCA logado.
    intake_meta_page_token: str = ""
    # Página aceita no webhook. O callback do app "agencia api" é compartilhado
    # (Yashav repassa pro copiloto por page_id) — sem este filtro, um payload
    # misto ingeriria lead de OUTRA página na espinha da Acme. Vazio = RECUSA
    # tudo (fail-closed desde 26/09; antes aceitava qualquer página).
    intake_meta_page_id: str = ""

    # Canal Cloud da Assistente/Assistente (2955) — responsivo na janela de 24h.
    # A bridge (whatsapp-claude) roteia/responde; o copiloto faz ponte entre o
    # webhook Cloud (via api-disparos) e a bridge. (Missão 9 v2)
    assistente_bridge_cloud_url: str = "http://100.64.0.1:8095/cloud-webhook"
    # Segredo do endpoint /cloud/assistente/enviar (header X-Bridge-Secret).
    assistente_enviar_secret: str = ""

    # Segredo compartilhado dos webhooks /cloud/{echo,status,inbound} da linha da Pessoa12.
    # O caller (api-disparos) precisa mandar no header X-Cloud-Secret. Se vazio,
    # os endpoints ficam FECHADOS (fail-closed) — ninguém injeta payload forjado.
    cloud_webhook_secret: str = ""

    # ── Linha disparadora do remarketing (05/10) ─────────────────────────────
    # O remarketing sai pela linha que está marcada `papel='disparo'` no
    # cadastro de números do api-disparos (`phone_numbers.papel`). O copiloto
    # PERGUNTA pra ele a cada tick — ver app/services/linha_disparadora.py.
    # A chave é a do cliente (uma X-API-Key = um tenant lá) e é lida do arquivo
    # no momento do uso; aqui só fica ONDE ela mora, nunca o valor.
    api_disparos_url: str = "http://127.0.0.1:8021"
    api_disparos_env_file: str = "/etc/agencia/acme-disparos.env"
    api_disparos_key_var: str = "ACME_DISPAROS_API_KEY"
    # Opt-out gravado lá (`contacts.opted_out`, quem clicou "Parar mensagens"
    # numa linha que o webhook do api-disparos atendia). A nutrição lê SÓ
    # LEITURA, uma vez por volta — ver app/services/opt_out_api_disparos.py.
    # Aqui só fica ONDE mora a URL do banco, nunca o valor.
    api_disparos_db_env_file: str = "/etc/agencia/api-disparos.env"
    api_disparos_db_var: str = "DATABASE_URL"

    # ── Web Push (aviso de lead novo no celular da corretora) ────────────────
    # Chaves VAPID: identificam o nosso servidor pro push da Apple/Google. A
    # pública vai pro navegador; a privada assina o envio e NÃO pode vazar —
    # em produção vem do .env, nunca daqui.
    # No iPhone o push só funciona se o painel estiver INSTALADO na tela de
    # início (Safari puro não recebe) — a tela de ativação avisa isso.
    vapid_public_key: str = ""
    vapid_private_key: str = ""
    # Contato exigido pelo protocolo VAPID (a Apple recusa envio sem ele).
    vapid_subject: str = "mailto:contato@example.com"
    # Pra onde o toque na notificação leva.
    painel_base_url: str = "https://app.example.com"
    # Desligado = intake segue normal, só não envia push. Deploy seguro.
    push_lead_novo_ativo: bool = False

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}

    @model_validator(mode="after")
    def load_kommo_from_env_file(self) -> "Settings":
        if self.kommo_subdomain and self.kommo_token:
            return self  # já setados (ex: via env var direta em testes)

        env_path = Path(self.acme_kommo_env_file)
        if not env_path.exists():
            raise FileNotFoundError(
                f"Configure {self.acme_kommo_env_file} com KOMMO_SUBDOMAIN e KOMMO_TOKEN"
            )

        kommo_env: dict[str, str] = {}
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            kommo_env[k.strip()] = v.strip()

        self.kommo_subdomain = kommo_env.get("KOMMO_SUBDOMAIN", "")
        self.kommo_token = kommo_env.get("KOMMO_TOKEN", "")

        if not self.kommo_subdomain or not self.kommo_token:
            raise ValueError(
                f"KOMMO_SUBDOMAIN e KOMMO_TOKEN devem estar em {self.acme_kommo_env_file}"
            )
        return self


def _warn_if_cli_key_conflict(llm_provider: str, api_key: str) -> None:
    """Loga warning se ANTHROPIC_API_KEY não-vazia em modo cli.

    Claude CLI interpreta qualquer valor não-vazio como API key e ignora
    o OAuth (~/.claude/.credentials.json), causando falha de autenticação.
    """
    if llm_provider == "cli" and api_key:
        _log.warning(
            "ANTHROPIC_API_KEY está setada (não-vazia) mas llm_provider=cli. "
            "Isso faz o Claude CLI ignorar OAuth e usar API key. "
            "Limpe a variável no .env ou mude llm_provider pra 'api'."
        )


settings = Settings()
_warn_if_cli_key_conflict(settings.llm_provider, settings.anthropic_api_key)


def is_test_or_dryrun() -> bool:
    """True se o processo atual NÃO é uma entrega real de briefing.

    Fonte única de verdade pra marcar registros em `briefings_sent` como
    `is_test=True`. Cobre três cenários de não-entrega:

    - `settings.dry_run` — modo dev/DRY_RUN (loga em vez de enviar).
    - `PYTEST_CURRENT_TEST` — setado pelo pytest durante testes.
    - `COPILOTO_TEST_MODE` — env pra scripts de smoke/sanity fora do pytest
      marcarem explicitamente que a rodada não é entrega real (foi um script
      assim, sem essa marca, que causou o incidente do briefing das 20h não
      entregue: dry-runs gravavam success=True e o anti-dobro os contava como
      entrega real).

    Registros com is_test=True NUNCA contam pro anti-dobro
    (`_already_delivered_today`), então uma rodada de teste jamais bloqueia
    o envio real do dia.
    """
    return bool(
        settings.dry_run
        or os.environ.get("PYTEST_CURRENT_TEST")
        or os.environ.get("COPILOTO_TEST_MODE")
    )


def _candidato_sombra() -> "LLMProvider | None":
    """Monta o candidato que vai duelar na sombra, conforme shadow_llm_provider.

    Retorna None quando não há candidato utilizável — ex.: pediram Model Studio
    mas a chave não está no .env. Nesse caso a sombra simplesmente não sobe:
    melhor não medir do que gastar chamada pra colecionar erro de auth.
    """
    if settings.shadow_llm_provider == "modelstudio":
        cfg = _qwen_config()
        if not cfg.api_key or not cfg.base_url:
            _log.warning(
                "SHADOW_LLM_PROVIDER=modelstudio mas falta MODELSTUDIO_API_KEY/BASE_URL "
                "(.env do copiloto-api ou %s). Sombra do Qwen NÃO vai subir.",
                settings.qwen_env_file,
            )
            return None
        return _modelstudio_provider(settings.shadow_llm_model or cfg.model)
    from app.llm.codex_provider import CodexProvider

    return CodexProvider(model=settings.shadow_llm_model, timeout=settings.codex_timeout)


def _envelopar_sombra(provider: "LLMProvider") -> "LLMProvider":
    """Se a sombra estiver ligada, duela o candidato contra o provider de produção.

    Só faz sentido quando o primário NÃO é o próprio candidato (senão duelaria
    consigo mesmo). A resposta de produção nunca muda.
    """
    if not settings.shadow_llm or settings.llm_provider == settings.shadow_llm_provider:
        return provider
    candidato = _candidato_sombra()
    if candidato is None:
        return provider
    from app.llm.shadow_provider import ShadowProvider

    return ShadowProvider(
        provider,
        candidato,
        jsonl_path=settings.shadow_llm_path,
        flag_path=settings.shadow_llm_flag,
        cap_diario=settings.shadow_llm_cap_diario,
        rotulo=settings.shadow_llm_model,
    )


def _contabilizar(provider: "LLMProvider") -> "LLMProvider":
    """Envelopa pra gravar o custo de cada chamada em `llm_chamadas`.

    Fica aqui, no funil, e não dentro de um provider específico: em 06/09/2026 o
    copiloto trocou de Claude pro Alibaba e o custo sumiu do painel da casa, que
    media pelo log do CLI do Qwen. Medir pelo canal do provider quebra toda vez
    que a gente troca de provider — medindo daqui, não quebra mais.
    """
    from app.llm.contabil_provider import ContabilProvider

    return ContabilProvider(provider)


def get_llm_provider() -> "LLMProvider":
    """Factory: retorna o provider LLM configurado em settings.llm_provider.

    Modo CLI pina settings.model_analyzer (Sonnet) — sem isso o subprocess
    `claude -p` herda o modelo default da conta da Dona (~/.claude/settings.json),
    que muda quando ela troca de modelo no Claude Code e já vazou Opus/Fable
    pra dentro do Inspector e da Assistente sem ninguém decidir.
    """
    return _contabilizar(_get_llm_provider_cru())


def get_llm_provider_assistente() -> "LLMProvider":
    """Provider SÓ da Assistente copilota: `claude -p` com Haiku, sem reserva Codex/Qwen.

    Reserva em cadeia pagava timeout em fila e escondia a falha atrás de um 😿 mudo; aqui
    falhou, falha rápido (<= assistente_cli_timeout por chamada) e o endpoint responde com
    texto claro. Continua registrando custo/latência em `llm_chamadas` (ContabilProvider).
    """
    from app.llm.cli_provider import ClaudeCliProvider

    return _contabilizar(
        _envelopar_sombra(  # sombra segue como está (hoje desligada: SHADOW_LLM não setada)
            ClaudeCliProvider(
                model=settings.model_assistente,
                timeout=settings.assistente_cli_timeout,
                setting_sources="project",
            )
        )
    )


def get_llm_provider_shadow(model: str | None = None, timeout: int | None = None) -> "LLMProvider":
    """Provider do Aprendiz (sombra e juiz): `claude -p` direto, sem reserva Codex/Qwen.

    Limite de cota da assinatura sobe como erro permanente (cli_provider) e o
    shadow_run para a rodada; nada de retry nem queda em provider também sem cota.
    Cada chamada continua indo para `llm_chamadas` (ContabilProvider).
    """
    from app.llm.cli_provider import ClaudeCliProvider

    return _contabilizar(
        ClaudeCliProvider(
            model=model or settings.model_shadow,
            timeout=timeout or settings.shadow_cli_timeout,
            setting_sources="project",
        )
    )


def _get_llm_provider_cru() -> "LLMProvider":
    if settings.llm_provider == "cli":
        return _envelopar_sombra(
            _claude_com_reserva(settings.model_analyzer, settings.model_analyzer_codex)
        )
    if settings.llm_provider == "codex":
        return _codex_com_fallback(settings.model_analyzer_codex, settings.model_analyzer)
    if settings.llm_provider == "modelstudio":
        return _envelopar_sombra(
            _modelstudio_com_fallback(settings.modelstudio_model_analyzer)
        )
    if settings.llm_provider == "api":
        # Mesmo modelo do CLI (Sonnet), sem o pedagio de abrir sessao do
        # Claude Code a cada analise: 94% do custo medido em 29/08/2026.
        from app.llm.api_provider import AnthropicApiProvider
        return _envelopar_sombra(
            AnthropicApiProvider(
                api_key=settings.anthropic_api_key,
                model=settings.model_analyzer,
            )
        )
    raise ValueError(f"llm_provider desconhecido: {settings.llm_provider!r}")


def _claude_com_reserva(model_claude: str, model_codex: str) -> "LLMProvider":
    """Claude CLI como principal, codex como reserva — o inverso de
    `_codex_com_fallback`, e a direção que importa enquanto o primário é o
    Claude na assinatura pessoal da Dona.

    Nada muda no dia a dia: 100% das chamadas seguem no Claude, mesma
    qualidade. A reserva só entra quando o Claude FALHA (cota estourada,
    login), e o disjuntor do FallbackProvider evita que cada chamada pague o
    timeout do primário durante o apagão.

    Desligar com `cli_reserva_codex=false` (ex.: pra provar que uma falha de
    cota realmente derruba, num drill).
    """
    from app.llm.cli_provider import ClaudeCliProvider

    principal = ClaudeCliProvider(model=model_claude)
    if not settings.cli_reserva_codex:
        return principal
    from app.llm.codex_provider import CodexProvider, FallbackProvider

    reserva = CodexProvider(
        model=model_codex,
        remote_host=settings.codex_remote_host or None,
        timeout=settings.codex_timeout,
    )
    provider: "LLMProvider" = FallbackProvider(principal, reserva)
    # Terceiro nível (05/09/2026): Claude e codex são assinatura e podem cair
    # JUNTOS (janela de 5h + cota mensal). O Qwen é por token — quando os dois
    # falham, ele responde em vez de o lead ficar no vácuo. Só entra se
    # RESPONDER de verdade (ver _degrau_qwen) — "tem chave" não basta.
    if settings.cli_reserva_modelstudio:
        ultimo = _degrau_qwen()
        if ultimo is not None:
            provider = FallbackProvider(provider, ultimo)
    return provider


class _QwenConfig(NamedTuple):
    api_key: str
    base_url: str
    model: str
    origem: str  # "env" | "arquivo" | "misto" — pro log; nunca a chave
    model_reserva: str = ""  # 2º modelo no MESMO endpoint (mesma chave); "" = sem


def _ler_env_file(path: str) -> dict[str, str]:
    """Lê KEY=VALUE de um .env simples. Arquivo ausente = dict vazio."""
    valores: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as f:
            for linha in f:
                linha = linha.strip()
                if not linha or linha.startswith("#") or "=" not in linha:
                    continue
                k, v = linha.split("=", 1)
                valores[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        return {}
    return valores


def _qwen_config(model: str | None = None) -> _QwenConfig:
    """Chave, endpoint e modelo do Qwen — do .env, com fallback pro qwen_env_file.

    Lido NO MOMENTO DO USO (não no import): trocar a chave em /etc/agencia/qwen.env
    vale na próxima construção de provider. Nada disso vai pra log.
    """
    arquivo: dict[str, str] | None = None
    do_env = 0
    do_arq = 0

    def _valor(env_valor: str, nome_arquivo: str) -> str:
        nonlocal arquivo, do_env, do_arq
        if env_valor:
            do_env += 1
            return env_valor
        if arquivo is None:
            arquivo = _ler_env_file(settings.qwen_env_file) if settings.qwen_env_file else {}
        v = arquivo.get(nome_arquivo, "")
        if v:
            do_arq += 1
        return v

    api_key = _valor(settings.modelstudio_api_key, "QWEN_API_KEY")
    base_url = _valor(settings.modelstudio_base_url, "QWEN_BASE_URL")
    modelo = model or _valor(settings.modelstudio_model_analyzer, "QWEN_MODEL")
    reserva = _valor(settings.modelstudio_model_reserva, "QWEN_MODEL_RESERVA")
    if reserva == modelo:
        reserva = ""  # reserva igual ao principal não é reserva
    origem = "misto" if (do_env and do_arq) else ("arquivo" if do_arq else "env")
    return _QwenConfig(api_key, base_url, modelo, origem, reserva)


def _modelstudio_provider(model: str | None = None) -> "ModelStudioProvider":
    from app.llm.modelstudio_provider import ModelStudioProvider

    cfg = _qwen_config(model)
    return ModelStudioProvider(
        cfg.api_key,
        model=cfg.model,
        base_url=cfg.base_url,
        timeout=settings.modelstudio_timeout,
        enable_thinking=settings.modelstudio_enable_thinking,
    )


def _modelo_responde(provider: "ModelStudioProvider", host: str, papel: str) -> "bool | None":
    """Verifica UM modelo do workspace e loga o veredito.

    True = respondeu; None = falha transitória (fica, pode voltar); False =
    falha permanente (chave/endpoint/modelo errados — fora).
    """
    status = provider.verificar(timeout=settings.modelstudio_verificar_timeout)
    if status.ok:
        _log.info(
            "llm_degrau_qwen_ok papel=%s modelo=%s host=%s (%dms)",
            papel, provider.model, host, status.duracao_ms,
        )
        return True
    if status.permanente:
        _log.error(
            "llm_degrau_qwen_indisponivel papel=%s modelo=%s host=%s motivo=%s — "
            "modelo EXCLUÍDO do degrau (chave/endpoint/modelo errados; corrija a config).",
            papel, provider.model, host, status.motivo,
        )
        return False
    _log.warning(
        "llm_degrau_qwen_instavel papel=%s modelo=%s host=%s motivo=%s — falha transitória; "
        "modelo MANTIDO no degrau (pode voltar sozinho).",
        papel, provider.model, host, status.motivo,
    )
    return None


def _degrau_qwen() -> "LLMProvider | None":
    """Monta o 3º degrau da escada SE ele responder de verdade.

    Critério (05/09/2026): uma chamada mínima real por modelo, cacheada 1h.
    Sem config → fora, com aviso. Falha PERMANENTE (401 chave errada, 404
    modelo/endpoint errado) → o modelo sai, com erro no log dizendo o quê.
    Falha TRANSITÓRIA (rede, 5xx, timeout) → o modelo FICA: pode voltar
    sozinho e o FallbackProvider lida com a falha na hora. A verificação
    nunca levanta e não trava o boot além de `modelstudio_verificar_timeout`
    por modelo (uma vez por hora, no máximo).

    RESERVA NO MESMO ENDPOINT: o workspace serve vários fornecedores com a
    mesma chave. Com `modelstudio_model_reserva` (ex.: kimi-k3), o degrau vira
    FallbackProvider(principal, reserva) — mesmo disjuntor da escada de cima.
    Se o principal estiver morto (permanente) e a reserva viva, o degrau sobe
    só com a reserva. O que sobra, sobe; nada = escada em 2 níveis.
    """
    cfg = _qwen_config()
    if not cfg.api_key or not cfg.base_url or not cfg.model:
        faltando = [
            n for n, v in (("chave", cfg.api_key), ("base_url", cfg.base_url), ("modelo", cfg.model))
            if not v
        ]
        _log.warning(
            "llm_degrau_qwen_sem_config faltando=%s — escada fica em 2 níveis (Claude → codex). "
            "Preencha MODELSTUDIO_* no .env do copiloto-api ou %s.",
            ",".join(faltando), settings.qwen_env_file,
        )
        return None
    host = cfg.base_url.split("/")[2] if "//" in cfg.base_url else cfg.base_url
    candidatos = [("principal", _modelstudio_provider(cfg.model))]
    if cfg.model_reserva:
        candidatos.append(("reserva", _modelstudio_provider(cfg.model_reserva)))

    if settings.modelstudio_verificar:
        vivos = [p for papel, p in candidatos if _modelo_responde(p, host, papel) is not False]
    else:
        _log.info(
            "llm_degrau_qwen_sem_verificacao modelos=%s (MODELSTUDIO_VERIFICAR=false)",
            [p.model for _, p in candidatos],
        )
        vivos = [p for _, p in candidatos]

    if not vivos:
        _log.error(
            "llm_degrau_qwen_fora host=%s origem=%s — nenhum modelo do workspace responde; "
            "escada fica em 2 níveis (Claude → codex).", host, cfg.origem,
        )
        return None
    if len(vivos) == 1:
        _log.info(
            "llm_degrau_qwen_montado host=%s origem=%s modelos=[%s] — escada: Claude → codex → %s",
            host, cfg.origem, vivos[0].model, vivos[0].model,
        )
        return vivos[0]
    from app.llm.codex_provider import FallbackProvider

    _log.info(
        "llm_degrau_qwen_montado host=%s origem=%s modelos=[%s → %s] — "
        "escada: Claude → codex → %s → %s (mesma chave)",
        host, cfg.origem, vivos[0].model, vivos[1].model, vivos[0].model, vivos[1].model,
    )
    return FallbackProvider(vivos[0], vivos[1])


def _codex_com_fallback(model_codex: str, model_claude: str) -> "LLMProvider":
    """Codex (Terra) como principal, Claude CLI como rede de proteção.

    Sem o fallback, uma falha do codex (login expirado, cota, Mac fora quando
    remoto) derrubaria o sistema da CLIENTE — exatamente o problema que a
    migração veio curar. Degradar pro Claude é sempre melhor que parar.
    Desligar com codex_fallback_claude=false (ex.: pra medir o Terra puro na
    sombra, sem o Claude mascarar falha).
    """
    from app.llm.cli_provider import ClaudeCliProvider
    from app.llm.codex_provider import CodexProvider, FallbackProvider

    primario = CodexProvider(
        model=model_codex,
        remote_host=settings.codex_remote_host or None,
        timeout=settings.codex_timeout,
    )
    if not settings.codex_fallback_claude:
        return primario
    return FallbackProvider(primario, ClaudeCliProvider(model=model_claude))


def _modelstudio_com_fallback(model_qwen: str) -> "LLMProvider":
    """Qwen (Model Studio) como principal, Claude CLI como rede de proteção.

    Mesmo desenho do `_codex_com_fallback`, mesmo motivo: falha do provider
    externo (chave, cota, rede) não pode derrubar o atendimento da Assistente —
    degrada pro Claude e volta sozinho quando o disjuntor testa de novo.
    Desligar com MODELSTUDIO_FALLBACK_CLAUDE=false (ex.: pra medir o Qwen puro).
    """
    primario = _modelstudio_provider(model_qwen or None)
    if not settings.modelstudio_fallback_claude:
        return primario
    from app.llm.cli_provider import ClaudeCliProvider
    from app.llm.codex_provider import FallbackProvider

    return FallbackProvider(primario, ClaudeCliProvider(model=settings.model_analyzer))


def get_classifier_provider() -> "LLMProvider":
    """Factory pro classifier (rapido, override opcional).

    - settings.classifier_provider == "ollama" -> Qwen no Mac (Vpn)
    - modo cli -> pina settings.model_classifier (Haiku — classificação não
      precisa de modelo grande; ver CLAUDE.md "Custo otimizado")
    - senão -> cai no provider global (get_llm_provider)
    """
    if settings.classifier_provider == "ollama":
        from app.llm.ollama_provider import OllamaProvider
        return OllamaProvider(
            host=settings.ollama_host,
            model=settings.ollama_classifier_model,
        )
    if settings.llm_provider == "cli":
        return _claude_com_reserva(
            settings.model_classifier, settings.model_classifier_codex
        )
    if settings.llm_provider == "codex":
        # classificação é binária//curta: modelo menor, mesmo fallback
        return _codex_com_fallback(settings.model_classifier_codex, settings.model_classifier)
    if settings.llm_provider == "modelstudio":
        return _modelstudio_com_fallback(settings.modelstudio_model_classifier)
    if settings.llm_provider == "api":
        # classificacao e binaria/curta: modelo menor, mesma API
        from app.llm.api_provider import AnthropicApiProvider
        return AnthropicApiProvider(
            api_key=settings.anthropic_api_key,
            model=settings.model_classifier,
        )
    return get_llm_provider()
