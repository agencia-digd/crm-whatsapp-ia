"""Canal WhatsApp Cloud API para a linha da Pessoa3 (2591, coexistência).

Substitui o Baileys/Evolution NESSA linha (que apanhava de ban por ser Web
não-oficial em IP de datacenter). Aqui tudo passa pelo canal OFICIAL da Meta:

- inbound: o api-disparos recebe o webhook da Meta e repassa pra cá (POST
  /cloud/inbound). Gravamos em `mensagens_whatsapp` (mesmo schema do Evolution),
  então o painel exibe sem saber a diferença.
- envio: graph.facebook.com /{phone_id}/messages (texto livre, dentro da
  janela de 24h da coexistência).
- leitura: ler_thread_cloud() monta a thread a partir de `mensagens_whatsapp`.

Token da WABA Acme vem de /etc/agencia/review.env (não duplicar no .env).
"""
from __future__ import annotations

import hmac
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from app.services.emission_gate import guarded

import httpx
import structlog
from fastapi import APIRouter, Header, HTTPException, Request
from sqlalchemy import text as sql_text

from app.config import settings
from app.db.session import SessionLocal
from app.services import cloud_lines

log = structlog.get_logger()

# --- Reengajamento: resposta do lead a um disparo (linha da Pessoa12) ---------------
# Funil de Vendas Acme: "Follow-up" -> "Em atendimento" quando o lead responde
# a um disparo de reengajamento. IDs canônicos (0036_create_stage_map).
KOMMO_PIPELINE_FUNIL = 10000004
KOMMO_STAGE_FOLLOWUP = 10000001        # origem: só move DAQUI
KOMMO_STAGE_EM_ATENDIMENTO = 10000005  # destino

# Opt-out: implementação canônica em app/services/opt_out.py (unificada na
# auditoria 26/07 — antes eram duas regras divergentes). O regex antigo daqui
# marcava opt-out em "quero cancelar a visita" e em "não quero esse bairro";
# a canônica exige frase inequívoca OU palavra-comando em mensagem curta.
from app.services.opt_out import is_opt_out as _is_opt_out  # noqa: E402


# ── Dedupe de saudação (rede de segurança da mensagem planejada) ──────────────
# O template da Pessoa12 JÁ cumprimenta ("Olá {nome}, tudo bem?" / "Oi, tudo bem?") no
# {{1}}. A mensagem planejada da corretora entra no {{2}} (corpo). O UI já trava
# a saudação (a corretora escreve só o corpo), mas se ela colar uma saudação
# mesmo assim, isto remove UMA saudação SÓ no início — pra não dobrar
# ("Olá João, tudo bem?\n\nOi João, tudo bem? ...").
#
# CONSERVADORA de propósito: casa só no início (^), uma vez, saudação curta
# ("oi/olá/ola/e aí/opa/bom dia..." + nome opcional + "tudo bem?/como vai?"
# opcional + pontuação). Nunca corta no meio do texto. Se não casar, devolve o
# texto intacto — melhor deixar uma saudação passar do que comer conteúdo real.
# 🔴 Bug de 31/07 (lead Wesley Cruz, tarefa 374 — a PRIMEIRA mensagem planejada
# a sair de verdade): a versão antiga listava `ola` e `bom dia` como alternativas
# IRMÃS e tinha um "nome opcional" logo depois. Em "Ola bom dia tudo bem?" ela
# casava `Ola`, tomava "bom" como o nome, e parava — sobrava "dia tudo bem?".
# O lead recebeu:
#     Olá Wesley, tudo bem?
#
#     dia tudo bem?
#
# Saudação é COMPOSTA: "oi" e "bom dia" podem vir juntos, nessa ordem. Agora são
# dois blocos opcionais em sequência, e o lookahead exige que o texto REALMENTE
# comece com saudação — sem ele, os blocos opcionais casariam vazio e o antigo
# "nome opcional" comeria a primeira palavra de qualquer mensagem ("Segue o
# contrato" viraria "o contrato").
#
# O "nome opcional" saiu de propósito: comer uma palavra solta é o que produz
# frase quebrada. Mensagem que é SÓ saudação continua caindo no fallback do
# `_limpar_saudacao` (devolve o original) — saudação repetida é feia, mas frase
# truncada parece defeito do sistema.
_SAUDACAO_INICIO_RE = re.compile(
    r"^\s*"
    r"(?=(?:oi|ol[áa]|ola|opa|e\s+a[íi]|bom\s+dia|boa\s+tarde|boa\s+noite)\b)"
    r"(?:(?:oi|ol[áa]|ola|opa|e\s+a[íi])\b[\s,!.…-]*)?"      # cumprimento curto
    r"(?:(?:bom\s+dia|boa\s+tarde|boa\s+noite)\b[\s,!.…-]*)?"  # período do dia
    r"(?:(?:tudo\s+bem|tudo\s+bom|como\s+vai|como\s+voc[êe]\s+est[áa])\b\s*[?!.…]*)?"
    r"[\s,!.…-]*",
    re.IGNORECASE,
)


def _limpar_saudacao(texto: str | None) -> str:
    """Remove UMA saudação só do INÍCIO do texto (rede de segurança do dedupe).

    Conservadora: se o corte deixaria o texto vazio (a corretora mandou SÓ a
    saudação), devolve o texto original — não some com a mensagem inteira.
    """
    if not texto:
        return texto or ""
    t = texto.strip()
    novo = _SAUDACAO_INICIO_RE.sub("", t, count=1).strip()
    if not novo:  # era só saudação → não zera; devolve o original
        return t
    # Sobrou UMA palavra depois de cortar? Quase sempre é o vocativo ("Bom dia,
    # Wesley!" → "Wesley!"), e mandar isso solto no corpo parece defeito. Mesma
    # regra do caso vazio: devolve o original. Saudação repetida é feia; frase
    # truncada é bug aos olhos do cliente.
    if len(novo.split()) < 2:
        return t
    return novo


# ── Indicação R$500 (aprovado Dona): lead que já comprou → pivô p/ indicação ──
# Regex CONSERVADORA: só casos CLAROS de "já resolvi noutro lugar / já comprei".
# Evita falso-positivo (ex: "ainda não comprei", "quero comprar") — por isso
# casa "já <verbo>" e "em outro/outra". NÃO envia nada; só sugere nota interna.
_JA_COMPROU_RE = re.compile(
    r"\bj[áa]\s+(comprei|encontrei|fechei|achei|consegui|resolvi|comprou)\b"
    r"|\b(comprei|achei|fechei|encontrei)\s+(em\s+)?(outr[oa]|noutr[oa])\b"
    r"|\bcomprei\s+em\s+outro\b",
    re.IGNORECASE,
)

# Rótulo da nota auto de indicação — usado TAMBÉM pra idempotência (LIKE).
_NOTA_INDICACAO_ROTULO = (
    "💡 SUGESTÃO DE RESPOSTA (auto) — lead sinalizou que já comprou "
    "→ pivô pra indicação (R$500):"
)


def _is_ja_comprou(texto: str | None) -> bool:
    """True se o lead sinaliza CLARAMENTE que já comprou/encontrou (outro lugar)."""
    if not texto:
        return False
    return bool(_JA_COMPROU_RE.search(texto))


def _montar_nota_indicacao(nome: str | None) -> str:
    """Monta o texto da nota interna de sugestão de indicação (R$500).
    Voz da Pessoa, 1ª pessoa; usa o 1º nome do lead se houver."""
    voc = f" {nome}" if _nome_valido(nome) else ""
    msg = (
        f"Que notícia boa,{voc if voc else ' que legal'}! Fico muito feliz que "
        "você já encontrou 🙌 Deixa eu te perguntar então: você conhece alguém "
        "aí em Cidade que tá procurando imóvel? A gente tá com condições bem "
        "boas esse mês. E lembrando: se a sua indicação fechar um imóvel com a "
        "gente, você ganha R$500 de presente 🎁 Se pensar em alguém, é só me "
        "mandar o contato que eu cuido de tudo com carinho 😊"
    )
    return f"{_NOTA_INDICACAO_ROTULO} «{msg}»"


def tratar_intencao_indicacao(telefone: str, texto: str | None) -> str | None:
    """Se o lead responder o follow-up sinalizando que JÁ COMPROU, cria uma NOTA
    INTERNA (observacao_corretora) no card dele com uma sugestão de resposta de
    INDICAÇÃO (brinde R$500) pra a corretora revisar e enviar. NUNCA manda nada
    pro lead. Idempotente (não duplica a nota). Best-effort: nunca derruba o inbound.

    Retorna: "criada" | "duplicada" | "sem_intencao" | "sem_lead" | None(erro).
    """
    try:
        if not _is_ja_comprou(texto):
            return "sem_intencao"
        suf = _so_digitos(telefone)[-8:]
        if len(suf) != 8:
            return "sem_lead"
        with SessionLocal() as s:
            row = s.execute(
                sql_text(
                    """
                    SELECT lead_id, nome FROM leads_snapshot
                    WHERE right(regexp_replace(coalesce(telefone,''),'\\D','','g'),8) = :suf
                    LIMIT 2
                    """
                ),
                {"suf": suf},
            ).mappings().all()
            if len(row) != 1:  # 0 = sem card; >1 = ambíguo → não cria às cegas
                return "sem_lead"
            lead_id = str(row[0]["lead_id"])
            nome = (row[0]["nome"] or "").strip().split()[0] if row[0]["nome"] else None

            # Idempotência: já existe nota de indicação pra esse lead? não duplica.
            ja = s.execute(
                sql_text(
                    "SELECT 1 FROM observacao_corretora "
                    "WHERE lead_id = :lid AND texto LIKE :pat LIMIT 1"
                ),
                {"lid": lead_id, "pat": _NOTA_INDICACAO_ROTULO[:40] + "%"},
            ).first()
            if ja:
                return "duplicada"

            s.execute(
                sql_text(
                    "INSERT INTO observacao_corretora "
                    "(lead_id, corretora_id, autor, texto) "
                    "VALUES (:lid, :cid, :autor, :texto)"
                ),
                {
                    "lid": lead_id, "cid": CORRETORA_ID, "autor": "sistema",
                    "texto": _montar_nota_indicacao(nome),
                },
            )
            s.commit()
        log.info("nota_indicacao_auto_criada", lead_id=lead_id, telefone_suf=suf)
        return "criada"
    except Exception as e:  # noqa: BLE001 — nunca derruba o inbound
        log.warning("nota_indicacao_erro", erro=str(e)[:160])
        return None

# Linha da Pessoa12 vem do registry (fonte única de linhas Cloud). As constantes de
# módulo são derivadas dele — mesmo comportamento de antes, sem hardcode duplicado.
_CORRETOR_LINE = cloud_lines.line_by_slug("corretor")
PHONE_ID = _CORRETOR_LINE.phone_id if _CORRETOR_LINE else "000000000000000"  # +5500000000000
INSTANCE = "acme_corretora_a"          # mesma string do cadastro corretoras.evolution_instance
CORRETORA_ID = 3                      # Pessoa3
# Linhas DA CASA (robô), não de corretora: a conversa que sai delas pertence ao
# lead, não a uma pessoa. Mantido pra quem ainda importa a constante — desde
# 02/09 a thread não filtra mais por corretora nenhuma (ver ler_thread_cloud).
_CORRETORAS_DA_CASA: tuple[int, ...] = (4,)

# Instâncias que NÃO são conversa com lead da operação: a bridge da Assistente com a
# Dona fala pelo mesmo banco, e um telefone que coincida traria conversa interna
# pro chat da corretora. Lista de exclusão (e não whitelist) porque instância
# nova de atendimento é o caso comum — nascer visível é o certo.
_INSTANCES_FORA_DA_THREAD: tuple[str, ...] = ("cloud_assistente",)

# Tenant único da Acme hoje (mesmo valor de cadencia.DEFAULT_TENANT_ID —
# constante local pra não criar import circular api → services → api).
DEFAULT_TENANT_ID = 1
API_VER = "v21.0"
_TOKEN_FILE = _CORRETOR_LINE.env_file if _CORRETOR_LINE else "/etc/agencia/review.env"

router = APIRouter(prefix="/cloud", tags=["cloud"])


def _token() -> str:
    tok = cloud_lines.token_for(_CORRETOR_LINE or "corretor")
    if not tok:
        raise RuntimeError("META_REVIEW_TOKEN ausente em review.env")
    return tok


def _dry_run_ativo() -> bool:
    """True se envio real deve ser suprimido (loga em vez de bater na Graph API).

    A linha da Pessoa12 não tem `dry_run_var` próprio no registry (cloud_lines) — por
    isso a rede de segurança aqui é a flag GLOBAL `settings.dry_run` (default True
    em dev) MAIS o guard de pytest (nunca envia de verdade sob teste, mesmo com
    mock esquecido — mesmo padrão de cloud_assistente.enviar_cloud_assistente).
    """
    return bool(settings.dry_run or os.environ.get("PYTEST_CURRENT_TEST"))


def _auth_cloud_webhook(secret_header: str | None) -> None:
    """Valida o secret compartilhado dos webhooks /cloud/{echo,status,inbound}.

    Quem chama esses endpoints é o `api-disparos` (projeto irmão), que DEVE mandar
    o header `X-Cloud-Secret` com o mesmo valor de settings.cloud_webhook_secret.
    Comparação em tempo constante (hmac.compare_digest) contra timing attack.

    Fail-closed: se o secret não estiver configurado em settings, o endpoint fica
    FECHADO (401) — ninguém injeta payload forjado (mensagem falsa no thread da Pessoa12,
    status delivered/failed forjado, eco fantasma).
    """
    secret = settings.cloud_webhook_secret
    if not secret or not secret_header or not hmac.compare_digest(secret_header, secret):
        log.warning("cloud_webhook_auth_falha")
        raise HTTPException(401, "unauthorized")


def _so_digitos(tel: str) -> str:
    return "".join(c for c in (tel or "") if c.isdigit())


# Vocativo neutro quando não temos o nome real do lead. Fica DEPOIS de "Olá" no
# template ("Olá tudo bem, tudo bem?") — por isso, sem nome, preferimos NÃO
# renderizar o vocativo (ver _saudacao_ola_sem_nome). Mantido como sentinela.
_NOME_FALLBACK = "tudo bem"

# Tokens que NÃO são nome real (não viram vocativo). Evita "Olá 0, tudo bem?".
_NAO_NOME = {"", "0", "-", "lead", "cliente", "contato", "null", "none", "nan"}


def _nome_valido(token: str | None) -> bool:
    """True se `token` parece um primeiro nome real (não vazio, não dígito,
    não placeholder). Conservador: só aceita o que dá pra pôr no "Olá ___"."""
    if not token:
        return False
    t = token.strip()
    if not t or t.lower() in _NAO_NOME:
        return False
    # Nome não é só dígitos/pontuação (ex: "0", "123", "+55").
    if not any(c.isalpha() for c in t):
        return False
    # Uma letra só não é vocativo: "Olá F, tudo bem?" é pior que sem nome.
    if len(t) < 2:
        return False
    # Emoji e símbolo no vocativo entregam que é robô montando o texto. Vem de
    # nome de perfil do WhatsApp ("WILSONTWOLAUS 👊🏽❤️🤘🏽"), nunca do CRM.
    if any(ord(c) > 0x2100 for c in t):
        return False
    return True


def _resolver_primeiro_nome(to_phone: str) -> str | None:
    """Resolve o PRIMEIRO nome do lead pelo TELEFONE, pros templates da Pessoa12
    (quando a cadência chama com first_name=None). Fonte, em ordem:
      1. `leads` — a tabela VIVA do CRM;
      2. `leads_snapshot` — cache do Kommo, legado;
      3. `mensagens_whatsapp.contato_nome` — perfil do WhatsApp, último recurso.

    A ORDEM É O CONSERTO (07/08/2026). Antes o snapshot vinha primeiro e `leads`
    nem era consultada — e o snapshot parou de ser alimentado no cutover do
    Kommo (27/07). Resultado: 82% dos leads não estavam lá e caíam direto no
    nome do perfil do WhatsApp.

    O caso que revelou: o lead Fulano recebeu "Olá Fulano" — nome do
    perfil do aparelho dele. Medindo depois, 155 leads receberiam nome trocado,
    incluindo um "João da Silva" que viraria "Fulano".

    Perfil de WhatsApp é apelido, não cadastro. Só serve quando o CRM não sabe
    o nome — nunca por cima dele.

    Casa pelos últimos 8 dígitos (padrão da casa). Só retorna se achar EXATAMENTE
    um nome real e não-ambíguo — senão None (o caller decide o fallback).
    """
    suf = _so_digitos(to_phone)[-8:]
    if len(suf) != 8:
        return None
    try:
        with SessionLocal() as s:
            # 1) `leads` — a tabela viva. LIMIT 2 de propósito: dois cadastros
            # com o mesmo telefone é ambiguidade, e chutar qual é o certo é
            # como o bug do "Fulano" começou.
            row = s.execute(
                sql_text(
                    """
                    SELECT nome FROM leads
                    WHERE right(regexp_replace(coalesce(telefone,''),'\\D','','g'),8) = :suf
                      AND nome IS NOT NULL AND btrim(nome) <> ''
                    LIMIT 2
                    """
                ),
                {"suf": suf},
            ).all()
            if len(row) == 1:
                tok = (row[0][0] or "").strip().split()[0] if row[0][0] else None
                if _nome_valido(tok):
                    return tok

            # 2) `leads_snapshot` — legado do Kommo, congelado desde 27/07.
            row = s.execute(
                sql_text(
                    """
                    SELECT nome FROM leads_snapshot
                    WHERE right(regexp_replace(coalesce(telefone,''),'\\D','','g'),8) = :suf
                      AND nome IS NOT NULL AND btrim(nome) <> ''
                    LIMIT 2
                    """
                ),
                {"suf": suf},
            ).all()
            if len(row) == 1:
                tok = (row[0][0] or "").strip().split()[0] if row[0][0] else None
                if _nome_valido(tok):
                    return tok
            # 3) Último recurso: nome de perfil do WhatsApp. É apelido, não
            # cadastro — só entra porque "Olá, tudo bem?" sem nome nenhum
            # converte pior, e aqui já sabemos que o CRM não tem o nome.
            row2 = s.execute(
                sql_text(
                    """
                    SELECT contato_nome FROM mensagens_whatsapp
                    WHERE right(regexp_replace(coalesce(contato_telefone,''),'\\D','','g'),8) = :suf
                      AND contato_nome IS NOT NULL AND btrim(contato_nome) <> ''
                    ORDER BY timestamp_evolution DESC NULLS LAST
                    LIMIT 1
                    """
                ),
                {"suf": suf},
            ).first()
            if row2 and row2[0]:
                tok = row2[0].strip().split()[0]
                if _nome_valido(tok):
                    return tok
    except Exception as e:  # noqa: BLE001 — resolver nome nunca derruba o envio
        log.warning("resolver_nome_falha", telefone_suf=suf, erro=str(e)[:120])
    return None


# Tipos de mensagem Cloud que carregam mídia binária (têm {tipo}.id = media_id).
_MEDIA_TYPES = ("audio", "image", "video", "document", "sticker", "voice")


def extrair_media_info(msg: dict[str, Any]) -> tuple[str, str] | None:
    """Extrai (media_id, mime_type) de uma mensagem Cloud de mídia.

    A Meta manda `{tipo: {"id": <media_id>, "mime_type": ..., ...}}`. Cobre
    também o eco (mesmo formato). Retorna None se não for mídia ou não tiver id.
    """
    tipo = msg.get("type")
    if tipo not in _MEDIA_TYPES:
        return None
    obj = msg.get(tipo)
    if not isinstance(obj, dict):
        return None
    media_id = obj.get("id")
    if not media_id:
        return None
    return str(media_id), obj.get("mime_type") or ""


# GraphMethodException (subcode padrão de "Object with ID ... does not exist,
# cannot be loaded due to missing permissions, or does not support this
# operation"). Na coexistência SMB, o media_id do ECHO (mídia que a corretora
# mandou PELO CELULAR) frequentemente NÃO é baixável via Graph /{media_id} —
# o binário não é exposto pra download, mesmo com o token/scope corretos
# (whatsapp_business_messaging presente). A Meta responde 400 com esse code.
# Isso é ESPERADO e NÃO é erro de token — logamos como info, não warning, pra
# não poluir. Erro REAL de token/permissão é 401/403 (OAuthException).
_GRAPH_METHOD_ERR = 100  # error.code do GraphMethodException


# Códigos de throttle da Graph: 4 = app request limit, 17 = user request limit,
# 32 = page rate limit, 613 = custom rate limit. Todos passam com o tempo.
_GRAPH_RATE_LIMIT_CODES = {4, 17, 32, 613}


def _classificar_erro_media(resp: httpx.Response) -> tuple[str, str]:
    """Classifica a falha de download de mídia a partir da resposta da Graph.

    Retorna (motivo, nivel_log): motivo legível + nível ("info"|"warning") pra
    o caller logar com o peso certo. 400/GraphMethodException e 404/410 são
    esperados (mídia inacessível/expirada) → info. 401/403 = token/permissão de
    verdade → warning (exige ação). Demais → warning (transitório/desconhecido).
    """
    st = resp.status_code
    try:
        err = (resp.json() or {}).get("error") or {}
    except Exception:  # noqa: BLE001 — corpo não-JSON
        err = {}
    code = err.get("code")
    if st in (404, 410):
        return "midia_expirada", "info"
    # RATE LIMIT vem como 403 igual a token morto, mas é TEMPORÁRIO — a Meta
    # marca `is_transient: true`. Confundir os dois custou caro em 25/08/2026:
    # um backfill com 12 workers estourou o limite e 1.275 áudios foram gravados
    # como "mídia expirada" (definitivo) quando só precisavam esperar. Erro
    # temporário classificado como definitivo apaga a chance de tentar de novo.
    if code in _GRAPH_RATE_LIMIT_CODES or err.get("is_transient") is True:
        return "rate_limit", "warning"
    if st in (401, 403):
        return "token_ou_permissao", "warning"  # ação: rotacionar/revisar token
    if st == 400 and code == _GRAPH_METHOD_ERR:
        # típico de echo SMB de coexistência: binário não exposto pra download.
        return "midia_inacessivel_coexistencia", "info"
    return "falha_desconhecida", "warning"


class SemTemplateDeRotacao(RuntimeError):
    """A linha não tem nenhum template ELEGÍVEL pra rotação deste envio.

    Não é falha de entrega e NÃO adianta tentar de novo: ou a WABA não tem
    UTILITY aprovado, ou os aprovados não casam com a convenção de nome que a
    rotação usa (`_ola_` 2 vars sem botão · `_reeng_`/`_prot_` 3 vars com
    botão). Foi o caso da Pessoa7 em 10/09: 4 templates UTILITY na linha, todos
    de pós-ligação, nenhum com o casco de mensagem livre — a corretora só via
    "não reengajou" e apertava "tentar de novo" em looping.

    Tipo próprio pro caller distinguir isso de erro de rede/Meta e dizer o que
    houve. Continua RuntimeError: quem já capturava genérico não muda.
    """


class CloudRateLimit(RuntimeError):
    """A Graph pediu pra ir mais devagar. NÃO é mídia perdida — é para tentar
    de novo mais tarde. Quem chama deve deixar o áudio como pendente."""

    def __init__(self, media_id: str = "") -> None:
        super().__init__(f"rate limit da Graph (media_id={media_id})")
        self.media_id = media_id


def baixar_media_cloud(media_id: str, *, tentativas: int = 2) -> bytes | None:
    """Baixa o binário de uma mídia da Cloud API pelo media_id (2 GETs).

    1. GET graph.facebook.com/{ver}/{media_id}  → { "url": ... } (URL efêmera ~5min)
    2. GET nessa url (mesmo Bearer)             → binário

    BEST-EFFORT: retorna os bytes ou None. NUNCA levanta — a mensagem já foi
    gravada no ingest (echo/inbound gravam com placeholder + media_id sem baixar);
    este download só ENRIQUECE (cache/transcrição) e pode falhar sem perder nada.

    Classifica a causa da falha (`_classificar_erro_media`) e loga com o nível
    certo: mídia expirada / inacessível em coexistência = info (esperado);
    token/permissão (401/403) = warning (exige ação). Faz retry com backoff só
    pra falha transitória (timeout / 5xx), nunca pra 4xx (não adianta re-bater).
    """
    if not media_id:
        return None
    try:
        tok = _token()
    except Exception as e:  # noqa: BLE001
        log.warning("cloud_media_token_falha", media_id=media_id, erro=str(e)[:120])
        return None

    headers = {"Authorization": f"Bearer {tok}"}
    meta_url = f"https://graph.facebook.com/{API_VER}/{media_id}"
    import time as _time

    for tentativa in range(1, max(1, tentativas) + 1):
        try:
            with httpx.Client(timeout=30) as c:
                r1 = c.get(meta_url, headers=headers)
                if r1.status_code >= 400:
                    motivo, nivel = _classificar_erro_media(r1)
                    getattr(log, nivel)("cloud_media_meta_falha", media_id=media_id,
                                        status=r1.status_code, motivo=motivo,
                                        body=r1.text[:160])
                    if motivo == "rate_limit":
                        # Sinaliza ao caller (transcrição) que NÃO é definitivo:
                        # o áudio continua lá, só fomos rápido demais.
                        raise CloudRateLimit(media_id)
                    if r1.status_code >= 500 and tentativa < tentativas:
                        _time.sleep(0.5 * tentativa)  # 5xx transitório → retry
                        continue
                    return None  # 4xx = não adianta insistir
                media_url = r1.json().get("url")
                if not media_url:
                    log.warning("cloud_media_sem_url", media_id=media_id)
                    return None
                # o download binário usa o MESMO Bearer (lookaside exige auth)
                r2 = c.get(media_url, headers=headers)
                if r2.status_code >= 400:
                    motivo, nivel = _classificar_erro_media(r2)
                    getattr(log, nivel)("cloud_media_download_falha", media_id=media_id,
                                        status=r2.status_code, motivo=motivo,
                                        body=r2.text[:160])
                    if motivo == "rate_limit":
                        raise CloudRateLimit(media_id)
                    if r2.status_code >= 500 and tentativa < tentativas:
                        _time.sleep(0.5 * tentativa)
                        continue
                    return None
                return r2.content
        except (httpx.TimeoutException, httpx.TransportError) as e:
            log.warning("cloud_media_transitorio", media_id=media_id,
                        tentativa=tentativa, erro=str(e)[:120])
            if tentativa < tentativas:
                _time.sleep(0.5 * tentativa)
                continue
            return None
        except CloudRateLimit:
            # Precisa passar direto: o `except Exception` abaixo a engolia e a
            # devolvia como None, que o caller lê como "mídia perdida". Era por
            # isso que o throttle continuava virando erro definitivo mesmo
            # depois de classificado (25/08/2026).
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("cloud_media_erro", media_id=media_id, erro=str(e)[:160])
            return None
    return None


def gravar(
    *,
    wamid: str,
    contato_telefone: str,
    contato_nome: str | None,
    from_corretora: bool,
    texto: str | None,
    tipo: str = "text",
    ts: datetime | None = None,
    raw: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    instance: str | None = None,
    corretora_id: int | None = None,
) -> bool:
    """Insere uma mensagem em mensagens_whatsapp. Idempotente por wamid.

    `metadata` vai pro `conteudo_metadata` (JSONB) — usado pra rastrear o
    `cloud_media_id`/`mime_type` de mídias (pro download posterior via
    baixar_media_cloud, sem baixar sincronamente no webhook).
    """
    tel = _so_digitos(contato_telefone)
    remote_jid = f"{tel}@s.whatsapp.net"
    when = ts or datetime.now(timezone.utc)
    import json as _json
    try:
        with SessionLocal() as s:
            ja = s.execute(
                sql_text("SELECT 1 FROM mensagens_whatsapp "
                         "WHERE evolution_message_id = :w LIMIT 1"),
                {"w": wamid},
            ).first()
            if ja:
                return False  # idempotência por wamid (sem unique constraint na tabela)
            s.execute(
                sql_text(
                    """
                    INSERT INTO mensagens_whatsapp
                        (id, evolution_message_id, instance, corretora_id,
                         remote_jid, contato_telefone, contato_nome, from_corretora,
                         tipo_mensagem, conteudo_texto, conteudo_metadata,
                         timestamp_evolution, recebido_em, raw_payload)
                    VALUES
                        (:id, :wamid, :instance, :cid, :jid, :tel, :nome, :fromc,
                         :tipo, :texto, CAST(:meta AS jsonb), :ts, now(),
                         CAST(:raw AS jsonb))
                    """
                ),
                {
                    "id": str(uuid.uuid4()), "wamid": wamid,
                    "instance": instance or INSTANCE,
                    "cid": corretora_id if corretora_id is not None else CORRETORA_ID, "jid": remote_jid, "tel": tel,
                    "nome": contato_nome, "fromc": from_corretora, "tipo": tipo,
                    "texto": texto, "ts": when,
                    "meta": _json.dumps(metadata) if metadata else None,
                    "raw": _json.dumps(raw or {}),
                },
            )
            s.commit()
            return True
    except Exception as e:  # noqa: BLE001
        log.warning("cloud_gravar_falha", wamid=wamid, erro=str(e)[:160])
        return False


@guarded
def enviar_cloud(
    to_phone: str,
    texto: str,
    *,
    linha: "cloud_lines.CloudLine | None" = None,
    instance: str | None = None,
    corretora_id: int | None = None,
    origem: str | None = None,
) -> dict[str, Any]:
    """Envia texto livre pela Cloud API. Grava o outbound na thread.

    Sem `linha`, usa a da Pessoa12 (2591) — comportamento histórico, chamadores antigos
    seguem iguais. Com `linha`, sai pelo phone_id e token DAQUELA linha: é o que
    permite a Pessoa5 (e depois a Pessoa11) responder pelo painel pela linha DELA, em
    vez de a mensagem sair pelo número da Pessoa12 (lead responderia pra outra pessoa).

    Texto livre só vale DENTRO da janela de 24h — fora dela a Meta exige template
    (ver `enviar_template_linha`).
    """
    to = _so_digitos(to_phone)
    phone_id = linha.phone_id if linha else PHONE_ID
    if _dry_run_ativo():
        log.info("cloud_dry_run", func="enviar_cloud", to=to,
                 slug=linha.slug if linha else "corretor", preview=(texto or "")[:120])
        wamid = f"dry-{uuid.uuid4()}"
        gravar(wamid=wamid, contato_telefone=to, contato_nome=None,
               from_corretora=True, texto=texto,
               metadata={"origem": origem} if origem else None,
               instance=instance, corretora_id=corretora_id)
        return {"ok": True, "dry_run": True, "sent": False,
                "messages": [{"id": wamid}]}
    url = f"https://graph.facebook.com/{API_VER}/{phone_id}/messages"
    body = {"messaging_product": "whatsapp", "to": to,
            "type": "text", "text": {"body": texto}}
    with httpx.Client(timeout=20) as c:
        resp = c.post(url, headers={"Authorization": f"Bearer {_token()}",
                                    "Content-Type": "application/json"}, json=body)
    if resp.status_code >= 400:
        log.error("cloud_envio_falha", status=resp.status_code, body=resp.text[:200])
        raise RuntimeError(f"cloud {resp.status_code}: {resp.text[:160]}")
    data = resp.json()
    wamid = (data.get("messages") or [{}])[0].get("id") or str(uuid.uuid4())
    gravar(wamid=wamid, contato_telefone=to, contato_nome=None,
           from_corretora=True, texto=texto, raw=data,
           # `origem` marca o que o ROBÔ mandou (Pessoa). Saída sem essa marca é
           # gente — é assim que o sistema sabe que um humano assumiu a conversa
           # (Dona 31/07: "assim que eu enviar mensagem, a Corretor para").
           metadata={"origem": origem} if origem else None,
           instance=instance, corretora_id=corretora_id)
    return data


# Pool de templates de reengajamento (fallback do acme_ola).
# Dona 28/07: MATOU as variantes a/c/d/e ("AVISO IMPORTANTE", "Atualização
# importante", "Notificação para", "Prezado(a)") — soavam robô/spam. Fica SÓ
# a variante b: "Olá {nome}, tudo bem?" (+ botão). Preferido continua sendo
# acme_ola (sem botão, mesmo cumprimento).
# Esqueleto: {{1}}=nome | {{2}}=mensagem | {{3}}=verbo do CTA.
_REENG_ROTATION: list[tuple[str, str]] = [
    ("acme_reeng_b_01", "pt_BR"), ("acme_reeng_b_02", "pt_BR"),
]

_REENG_HOOK_DEFAULT = (
    "Passando pra saber se você ainda está buscando imóvel — apareceram opções "
    "novas que podem combinar com o que você procurava."
)
_REENG_CTA_DEFAULT = "ver as novas opções"

# Corpos dos templates (espelho do que foi aprovado na Meta — fonte:
# projetos/api/scripts/subir_acme_reeng_canonico.py). Usado pra PREVIEW
# exato na aba Disparo: a Dona aprova o texto INTEIRO que o lead recebe.
# Só a variante b sobrevive (decisão Dona 28/07).
_REENG_BODIES: dict[str, str] = {
    "acme_reeng_b": "Olá {{1}}, tudo bem?\n\n{{2}}\n\nPara {{3}}, é só tocar no botão abaixo.",
}

# Pool SEM botão — esqueleto 2 vars: {{1}}=nome, {{2}}=mensagem.
# Preferido no follow-up (Dona 28/07: SEM botão).
# `*_ola_fu_*` = casca sem "Fico à disposição" (subidos 28/07, rodapé neutro).
# `*_ola_a/b/c_*` = legados (alguns ainda com "Fico à disposição") — fallback.
# ORDEM IMPORTA. A cadência passa o índice do toque como `variacao`, então esta
# lista é percorrida em sequência ao longo dos toques do MESMO lead. Ordenada
# pra os primeiros passos caírem em cascas DIFERENTES (Dona 29/07: "usa cada
# hora um template pra não terminar sempre igual").
#
# ⚠️ São 5 templates mas só 3 rodapés distintos — fu_01, fu_02 e b_01 terminam
# igual ("Qualquer coisa é só me chamar por aqui"). Intercalar ajuda, mas
# variedade de verdade pede um template novo. O b_01 pelo menos abre diferente
# ("Oi {nome}! 😊" em vez de "Olá {nome}, tudo bem?").
#
# ⛔ 04/10/2026 — `acme_ola_fu_01` e `acme_ola_fu_02` SAÍRAM da rotação. Conferido direto na
# Graph API: os dois estão **MARKETING/APPROVED** na WABA, e a regra da casa é disparo só UTILITY
# (Marketing custa ~10x e queima a lista). O `fu_01` era o PRIMEIRO da lista — o toque 1 de todo
# lead novo da Pessoa12 sairia como Marketing. Não explodiu porque a cadência está parada: zero envio
# por esses dois em 30 dias.
# Pra devolver à rotação: reclassificar na Meta pra UTILITY e conferir com
# `monitor_templates_marketing.py` antes de descomentar. Não basta a Meta aprovar — tem que
# aprovar como UTILITY.
_OLA_ROTATION: list[tuple[str, str]] = [
    ("acme_ola_c_01", "pt_BR"),   # "Tô por aqui se precisar 🙂"
    ("acme_ola_b_01", "pt_BR"),   # abre "Oi {nome}! 😊" (saudação diferente)
    ("acme_ola_a_01", "pt_BR"),   # legado com "Fico à disposição"
    # ("acme_ola_fu_01", "pt_BR"),  # MARKETING — fora até reclassificar
    # ("acme_ola_fu_02", "pt_BR"),  # MARKETING — fora até reclassificar
]
_OLA_BODY = "Olá {{1}}, tudo bem?\n\n{{2}}\n\nQualquer coisa é só me chamar por aqui."

# Corpo REAL de cada template do pool, como está aprovado na WABA (conferido no
# banco da api-disparos em 29/07). O rodapé muda entre eles — e é justamente o
# ponto: a Dona não quer que toda mensagem termine igual. Sem este mapa o
# preview mostrava sempre o rodapé do `_OLA_BODY` e mentia quando a rotação
# caía noutro template.
_OLA_BODIES: dict[str, str] = {
    "acme_ola_fu_01": "Olá {{1}}, tudo bem?\n\n{{2}}\n\nQualquer coisa é só me chamar por aqui.",
    "acme_ola_fu_02": "Olá {{1}}, tudo bem?\n\n{{2}}\n\nQualquer coisa é só me chamar por aqui.",
    "acme_ola_b_01": "Oi {{1}}! 😊\n\n{{2}}\n\nQualquer coisa é só me chamar por aqui.",
    "acme_ola_c_01": "Olá {{1}}, tudo bem?\n\n{{2}}\n\nTô por aqui se precisar 🙂",
    "acme_ola_a_01": "Olá {{1}}, tudo bem?\n\n{{2}}\n\nFico à disposição por aqui 😊",
    # Linha da Corretor (0000) — conferidos na WABA 000000000000000 em 30/07.
    # ⚠️ Os TRÊS são idênticos, rodapé incluso: rotacionar entre eles não varia
    # nada. Pra variar o fecho nesta linha (pedido da Dona em 29/07) é preciso
    # subir template novo com outro rodapé, não mexer na rotação.
    "acme_corretor_ola_a_01": "Olá {{1}}, tudo bem?\n\n{{2}}\n\nTô por aqui se precisar 🙂",
    "acme_corretor_ola_a_02": "Olá {{1}}, tudo bem?\n\n{{2}}\n\nTô por aqui se precisar 🙂",
    "acme_corretor_ola_a_03": "Olá {{1}}, tudo bem?\n\n{{2}}\n\nTô por aqui se precisar 🙂",
}

# Pool NO-NAME (sem botão, 1 var) — resolve o caso "lead SEM nome". UTILITY.
# Body aprovado na WABA: "Oi, tudo bem?\n\n{{1}}\n\nFico à disposição por aqui 😊".
# UMA variável só: {{1}} = a MENSAGEM (o gancho/hook). NÃO tem variável de nome →
# nunca dobra saudação ("Oi, tudo bem?" fixo, sem vocativo). Usado SÓ pra lead
# sem nome resolvido (com nome, o acme_ola personaliza o "Olá {nome}"). Nasce
# PENDING na Meta; enquanto não APPROVED o POST falha e a rotação cai no
# acme_ola/acme_reeng (mesmo loop de fallback por tentativa) — fallback intacto.
_OI_ROTATION: list[tuple[str, str]] = [
    ("acme_oi_01", "pt_BR"),
]
_OI_BODY = "Oi, tudo bem?\n\n{{1}}\n\nFico à disposição por aqui 😊"

# Mensagem PADRÃO do disparo de reengajamento (o {{2}}): convite de plantão de
# fim de semana nos decorados. 3 variações que rotacionam round-robin por lead
# (índice determinístico por telefone), pra não repetir o mesmo texto na lista
# nem no envio real. Fonte: briefing Assistente 2026-07 (disparo reengajamento Pessoa12).
DISPARO_VARIACOES: tuple[str, ...] = (
    "Passando pra te avisar que neste fim de semana a gente vai estar de plantão "
    "nos apartamentos decorados 🏡 Se você quiser aproveitar pra conhecer de "
    "pertinho, consigo já reservar um horário pra você. Quer que eu agende?",
    "Neste fim de semana vamos estar de plantão nos decorados 🏡 É uma boa chance "
    "de conhecer os apartamentos de pertinho e sentir o espaço. Quer que eu "
    "reserve um horário pra você dar uma passada?",
    "Esse fim de semana a gente tá de plantão nos apartamentos decorados 🏡 Se "
    "quiser aproveitar pra visitar sem compromisso, consigo encaixar um horário. "
    "Posso agendar?",
)


def variacao_disparo(to_phone: str) -> tuple[int, str]:
    """Escolhe a variação padrão do disparo pro telefone (round-robin determinístico
    por hash). Retorna (índice 0-based, texto). Mesma fórmula usada no preview
    (candidatos) e no envio real, pra o que a corretora aprova bater com o enviado.
    """
    to = _so_digitos(to_phone)
    idx = (sum(ord(c) for c in to) if to else 0) % len(DISPARO_VARIACOES)
    return idx, DISPARO_VARIACOES[idx]


def normalizar_param_template(texto: str) -> str:
    """Normaliza um parâmetro de template pra Cloud API. A Meta REJEITA parâmetro
    com \\n, \\t ou 4+ espaços consecutivos (erro #132000 / #131009). Troca quebras
    de linha e tabs por espaço e colapsa runs de espaço. As variações do disparo
    são de uma linha só, mas isso blinda contra texto editado à mão que traga \\n.
    """
    import re as _re
    # \n vira U+000B (vertical tab): a Meta ACEITA esse char e o WhatsApp o
    # renderiza como quebra de linha (mobile). Assim dá pra escrever a mensagem
    # com parágrafos naturais (\n\n) que chegam quebrados no celular. No WhatsApp
    # Web fica corrido (limitação do client) — ver api/TEMPLATES-prospeccao-agencia.md.
    t = (texto or "").replace("\r\n", "\n").replace("\r", "\n")
    t = t.replace("\n", "\v").replace("\t", " ")
    t = _re.sub(r" {2,}", " ", t)          # colapsa espaços (Meta rejeita 4+); nao toca no \v
    t = _re.sub("\v{3,}", "\v\v", t)     # no maximo 2 quebras seguidas
    return t.strip()


def _nome_para_template(to_phone: str, first_name: str | None) -> str:
    """Resolve o {{1}} (vocativo) dos templates da Pessoa12, SEM dobrar saudação.

    Ordem: (1) first_name explícito, se for nome real; (2) nome resolvido pelo
    TELEFONE (leads_snapshot → mensagens_whatsapp); (3) fallback neutro
    `_NOME_FALLBACK` ("tudo bem"). O caso (3) ainda gera "Olá tudo bem, tudo
    bem?" no vocativo — mas é RESIDUAL (só quando não há nome em lugar nenhum) e
    a saudação dobrada do CORPO ({{2}}) já foi removida (ver cadencia._TEMPLATES_MSG).
    Solução 100% limpa pro no-name exigiria template Meta novo (ver PROPOSTA no
    relatório): BODY="{{1}}\\n\\n{{2}}\\n\\n<rodapé>" com a saudação inteira no {{1}}.
    """
    tok = (first_name or "").strip().split()[0] if first_name else None
    if _nome_valido(tok):
        return tok  # type: ignore[return-value]
    resolvido = _resolver_primeiro_nome(to_phone)
    if resolvido:
        return resolvido
    return _NOME_FALLBACK


def preview_template_corretor(
    to_phone: str, first_name: str | None, variacao: int = 0
) -> dict[str, str]:
    """Prevê QUAL template a rotação escolheria e devolve o esqueleto renderizado
    ({{1}} preenchido, {{2}} fica como marcador pro front substituir ao vivo pelo
    texto editável). Preferimos o pool acme_ola (SEM botão, esqueleto de 2
    vars) — é o que o lead recebe.

    MESMA fórmula de hash do `enviar_template_linha`, `variacao` inclusa: sem
    isso o preview mostraria um rodapé e o envio usaria outro.
    """
    to = _so_digitos(to_phone)
    seed = (sum(ord(c) for c in to) if to else 0) + int(variacao or 0)
    start = seed % len(_OLA_ROTATION)
    tmpl, _lang = _OLA_ROTATION[start]
    nome = _nome_para_template(to_phone, first_name)
    # O rodapé é do TEMPLATE escolhido, não do _OLA_BODY fixo — senão o preview
    # mente quando a rotação cai num template com rodapé diferente.
    skeleton = _OLA_BODIES.get(tmpl, _OLA_BODY).replace("{{1}}", nome)
    return {"template": tmpl, "skeleton": skeleton}


# Códigos de erro da Graph que NÃO são "esse template não serve", e sim
# "essa LINHA/CONTA não pode mandar agora". Rotacionar template contra isso só
# queima os 13 do pool contra a mesma parede — corta na primeira ocorrência.
#   #133010 → número não registrado na Cloud API
#   #131000 → erro genérico da conta/plataforma
#   #131031 → conta restrita/bloqueada
#   #368    → temporariamente bloqueada por violação de política
#   #130497 / #131048 → limite de qualidade/spam da LINHA
_ERRO_LINHA_CODES = {133010, 131000, 131031, 368, 130497, 131048, 190}


def _erro_de_linha(resp: Any) -> bool:
    """True quando o erro da Graph é de LINHA/conta (não de template).

    401/403 (token morto/sem permissão) sempre contam. Fora isso, olha o
    `error.code` do corpo JSON. Corpo ilegível → False (segue rotacionando,
    comportamento antigo).
    """
    try:
        if int(getattr(resp, "status_code", 0)) in (401, 403):
            return True
        err = (resp.json() or {}).get("error") or {}
        return int(err.get("code") or 0) in _ERRO_LINHA_CODES
    except Exception:  # noqa: BLE001 — corpo não-JSON não decide nada
        return False


@guarded
def enviar_template_linha(
    linha: cloud_lines.CloudLine,
    to_phone: str,
    first_name: str | None,
    hook: str | None = None,
    cta: str | None = None,
    variantes: tuple[str, ...] | None = None,
    *,
    instance: str | None = None,
    corretora_id: int | None = None,
    com_botao: bool = False,
    variacao: int = 0,
    permitir_marketing: bool = False,
    preferir_marketing: bool = False,
    cap_diario_da_linha: int = 0,
    origem: str | None = None,
    template_fixo: tuple[str, str] | None = None,
) -> dict[str, Any]:
    """Reengaja lead frio (fora das 24h) por UMA LINHA Cloud qualquer, via template
    WABA aprovado (esqueleto de 2 ou 3 variáveis). {{1}}=nome, {{2}}=mensagem,
    {{3}}=verbo do CTA. Rotaciona por hash do telefone (anti-ban). Grava no thread
    com a `instance`/`corretora_id` da linha (multi-linha).

    Comportamento IDÊNTICO ao caminho histórico da Pessoa12 (que agora é um wrapper
    fino disto): mesma ordem de pools (acme_ola → acme_reeng), mesmo hash,
    mesmo corte de rotação em erro de LINHA (`_erro_de_linha`) e mesmo contrato
    de retorno {"ok","dry_run","sent","wamid","template"}.

    `com_botao=True` (boas-vindas / quando a copy manda "toque no Saiba mais"):
    pula o pool ola (sem botão) e usa só reeng. Senão o lead leria "toque no
    botão" e não teria botão nenhum.

    ⚠️ Os templates são por WABA: cada linha tem a SUA cópia aprovada com os
    MESMOS nomes (`acme_ola_a_01`...). Template ainda PENDING na WABA da linha
    faz o POST falhar e a rotação cair pro próximo — comportamento preservado.

    Args:
        linha: CloudLine de onde a mensagem sai (phone_id + token).
        instance/corretora_id: identidade do thread na gravação. Default = a da Pessoa12
            (compatibilidade com o caminho histórico).
        com_botao: se True, só tenta templates reeng (COM botão Saiba mais).
        origem: carimbo da AUTOMAÇÃO que pediu o envio, gravado em
            `conteudo_metadata->>'origem'`. Sem ele a mensagem conta como
            "mensagem humana" na trava de conversa da cadência e no relatório do
            painel (os dois contam humano pela AUSÊNCIA de origem). Envio de
            gente (painel) continua passando None.
    """
    to = _so_digitos(to_phone)

    # ── TRAVA DE OPT-OUT (16/09/2026) ────────────────────────────────────────
    # Aqui, e não na fila de cada motor, porque TODO template automático passa
    # por esta função. A varredura de 16/09 achou 33 pessoas que pediram pra
    # parar e SETE que receberam mensagem depois — quatro pelo robô. A causa não
    # foi a detecção: foi cada motor ter (ou não ter) a sua própria trava.
    # `nutricao`, `pessoa_retomada` e `lead_intake` não consultavam opt-out de
    # forma nenhuma, e NENHUM conhecia a etapa "Não perturbe".
    #
    # Só barra envio de AUTOMAÇÃO (`origem` preenchida). Envio humano do painel
    # passa `origem=None` e continua livre: opt-out barra disparo, não conversa —
    # se a pessoa escrever de novo, a corretora responde.
    if origem:
        try:
            from app.db.session import SessionLocal as _Sess
            from app.services import opt_out as _oo

            with _Sess() as _s:
                if _oo.esta_bloqueado(_s, to_phone):
                    log.warning("envio_bloqueado_opt_out", func="enviar_template_linha",
                                linha=linha.slug, to=to[-4:], origem=origem)
                    return {"ok": False, "dry_run": False, "sent": False,
                            "wamid": None, "template": None,
                            "erro": "opt_out", "messages": []}
        except Exception:  # noqa: BLE001
            # Falha ABERTA de propósito: banco fora não pode parar a operação
            # inteira. O risco aceito é uma mensagem a mais pra quem optou por
            # sair; o risco recusado é a régua parar calada por erro de conexão.
            log.exception("opt_out_check_falhou", to=to[-4:], origem=origem)

    # O hash é do TELEFONE: espalha os templates entre leads diferentes. Mas sem
    # o `variacao` o MESMO lead recebia sempre o mesmo template — e portanto o
    # mesmo rodapé nos 4 toques da cadência. A Dona (29/07): "usa cada hora um
    # template pra não terminar sempre igual". A cadência passa o índice do
    # toque aqui, então o rodapé varia ao longo da sequência do lead.
    hash_seed = (sum(ord(c) for c in to) if to else 0) + int(variacao or 0)
    nome = _nome_para_template(to_phone, first_name)
    # Normaliza o {{2}} pra Cloud API (Meta rejeita \n/\t/4+ espaços no parâmetro).
    gancho = normalizar_param_template(hook or _REENG_HOOK_DEFAULT)
    verbo = (cta or _REENG_CTA_DEFAULT).strip()
    if _dry_run_ativo():
        log.info("cloud_dry_run", func="enviar_template_linha", linha=linha.slug,
                 to=to, preview=gancho[:120], com_botao=com_botao)
        wamid = f"dry-{uuid.uuid4()}"
        gravar(wamid=wamid, contato_telefone=to, contato_nome=None,
               from_corretora=True, texto=f"[reengajamento: {gancho}]", tipo="template",
               metadata={"origem": origem} if origem else None,
               instance=instance, corretora_id=corretora_id)
        return {"ok": True, "dry_run": True, "sent": False,
                "wamid": wamid, "template": None,
                "messages": [{"id": wamid}]}
    url = f"https://graph.facebook.com/{API_VER}/{linha.phone_id}/messages"
    tok = cloud_lines.token_for(linha)
    if not tok:
        raise RuntimeError(f"token ausente pra linha {linha.slug} ({linha.token_var})")

    # Filtra por variantes calorosas se pedido (ex: ("b",) = "Olá {nome}, tudo bem?").
    reeng_rot = _REENG_ROTATION
    if variantes:
        filt = [t for t in _REENG_ROTATION if t[0].split("_")[2] in variantes]
        if filt:
            reeng_rot = filt

    # Pools (todos UTILITY, REGRA DA DONA: disparo NUNCA em MARKETING):
    #   acme_ola  (SEM botão, 2 vars {{1}}=nome/{{2}}=gancho) — default preferido.
    #   acme_reeng (COM botão, 3 vars) — fallback; ÚNICO se com_botao=True.
    # acme_oi DESATIVADO (Meta reclassificou pra MARKETING).
    ola_params = [{"type": "text", "text": nome}, {"type": "text", "text": gancho}]
    reeng_params = [
        {"type": "text", "text": nome},
        {"type": "text", "text": gancho},
        {"type": "text", "text": verbo},
    ]
    tentativas: list[tuple[str, str, list[dict[str, str]]]] = []
    # Dona 28/07: com_botao=False → SÓ pool ola (SEM botão). NÃO cai no reeng
    # (que tem Saiba mais). com_botao=True → SÓ reeng.
    if com_botao:
        reeng_start = hash_seed % len(reeng_rot)
        for k in range(len(reeng_rot)):
            tmpl, lang = reeng_rot[(reeng_start + k) % len(reeng_rot)]
            tentativas.append((tmpl, lang, reeng_params))
    else:
        ola_start = hash_seed % len(_OLA_ROTATION)
        for k in range(len(_OLA_ROTATION)):
            tmpl, lang = _OLA_ROTATION[(ola_start + k) % len(_OLA_ROTATION)]
            tentativas.append((tmpl, lang, ola_params))

    # TRAVA DE CATEGORIA (regra dura da Dona: disparo SÓ UTILITY).
    # A Meta classifica a MESMA copy diferente por conta — os templates da Pessoa12 são
    # UTILITY e os idênticos criados na WABA da Pessoa10 (27/07) voltaram TODOS
    # MARKETING. Filtra pela categoria REAL na Meta antes de tentar; assim a
    # rotação nunca escolhe um marketing e a linha sem UTILITY não gasta 13
    # requests pra falhar (era isso que estourava o circuit breaker global e
    # calava a linha da Pessoa12 no mesmo tick).
    utility = cloud_lines.templates_utility(linha.slug)
    permitidos = set(utility)
    # COTA DE MARKETING (Dona, 30/07): só entra se o caller PEDIR e se a cota do
    # dia dessa linha ainda tiver espaço. UTILITY segue com preferência absoluta
    # na rotação — a cota é teto, não meta a ser gasta.
    marketing: set[str] = set()
    if permitir_marketing:
        from app.services import cota_marketing

        try:
            from app.db.session import SessionLocal

            with SessionLocal() as _s:
                liberado = cota_marketing.pode_marketing(
                    linha.slug, cap_diario=cap_diario_da_linha, session=_s
                )
        except Exception as e:  # noqa: BLE001 — não sei ⇒ não libero
            log.warning("cota_marketing_indisponivel", slug=linha.slug,
                        erro=str(e)[:120])
            liberado = False
        if liberado:
            marketing = cloud_lines.templates_marketing(linha.slug)
            permitidos |= marketing
            log.info("cota_marketing_pool_liberado", slug=linha.slug,
                     n_marketing=len(marketing))

    if not permitidos:
        log.warning("linha_sem_template_utility", slug=linha.slug,
                    to=to_phone[-4:] if to_phone else None)
        raise SemTemplateDeRotacao(
            f"linha {linha.slug} não tem template UTILITY aprovado — nada enviado"
        )
    antes = len(tentativas)
    tentativas = [t for t in tentativas if t[0] in permitidos]
    # UTILITY primeiro, MARKETING no fim: a rotação só chega no MARKETING se
    # nenhum UTILITY entregou. Sem isso a cota seria consumida à toa, gastando
    # 10x onde o barato funcionaria.
    #
    # ⚠️ EXCEÇÃO (Dona 30/07, teste A/B de categoria): quando o caller PEDE
    # marketing, ele quer marketing — não um fallback que nunca acontece. Com a
    # ordem normal o braço "marketing" do teste sairia 100% em UTILITY e o
    # experimento compararia duas copies UTILITY achando que compara categoria.
    # A cota (`cota_marketing`) continua sendo o teto real; isto só decide a
    # ORDEM de tentativa dentro do que a cota já liberou.
    if len(tentativas) != antes:
        log.info("templates_fora_de_utility_ignorados", slug=linha.slug,
                 ignorados=antes - len(tentativas), restantes=len(tentativas))

    # Se a rotação padrão (nomes da linha da Pessoa12) não deu nenhum template válido
    # nesta linha, monta a rotação a partir do que a LINHA realmente tem em
    # UTILITY. Convenção: "_ola_" = 2 vars (sem botão), "_reeng_" = 3 (com botão).
    # Preferir nomes com `_ola_fu` (rodapé neutro, sem "Fico à disposição").
    #
    # ⚠️ A rotação é por CONVENÇÃO DE NOME: template de 3 variáveis com nome fora
    # da lista fica INVISÍVEL aqui — aprovado na Meta, inútil pro código. Foi o
    # que quase aconteceu com `acme_cl_prot_a_*` (29/07): o nome `reeng_c`
    # estava em cooldown de 4 semanas, subi como `prot_a` e ele não casava com
    # nada. Ao subir base nova, ou usa um destes prefixos ou acrescenta aqui.
    _PREFIXOS_COM_BOTAO = ("_reeng_", "_prot_")
    # 🔴 WABA COM PASSADO: a do 0000 herdou 44 templates do recuperador antigo,
    # e dois deles (`acme_reeng_1_*`, `acme_reeng_2_*`) casam com `_reeng_`
    # mas têm 2 variáveis — o pool com botão manda 3. Sairia erro da Meta, ou
    # copy de outro sistema assinada como Acme. Quando a linha declara um
    # prefixo próprio, SÓ ele entra na rotação.
    _pref = cloud_lines.prefixo_do_slug(linha.slug)
    if _pref:
        _antes_pref = len(permitidos)
        permitidos = {n for n in permitidos if n.startswith(_pref)}
        marketing = {n for n in marketing if n.startswith(_pref)}
        if len(permitidos) != _antes_pref:
            log.info("templates_de_outro_sistema_ignorados", slug=linha.slug,
                     prefixo=_pref, ignorados=_antes_pref - len(permitidos),
                     restantes=len(permitidos))
    if not tentativas:
        if com_botao:
            proprios = sorted(
                n for n in permitidos
                if any(p in n for p in _PREFIXOS_COM_BOTAO)
            )
            params = reeng_params
        else:
            # fu primeiro, depois resto dos ola
            fu = sorted(n for n in permitidos if "_ola_fu" in n or "_ola_v" in n)
            outros = sorted(
                n for n in permitidos
                if "_ola_" in n and n not in fu
            )
            proprios = fu + outros
            params = ola_params
        if proprios:
            ini = hash_seed % len(proprios)
            for k in range(len(proprios)):
                tentativas.append((proprios[(ini + k) % len(proprios)],
                                   "pt_BR", params))
            log.info("rotacao_propria_da_linha", slug=linha.slug,
                     n=len(proprios), com_botao=com_botao)

    if not tentativas:
        raise SemTemplateDeRotacao(
            f"linha {linha.slug}: nenhum template da rotação está em UTILITY"
        )

    # Dona 06/09: copy direta/variação NÃO pode cair em casco MARKETING.
    # Trava por nome (os cascos da Corretor são `_ola_m_` / `_ola_w_`) + pelo set
    # que a Meta devolveu. Sem isso, cache velho de UTILITY manda um template
    # que a Meta já reclassificou.
    if not permitir_marketing:
        _CASCO_MKT = ("_ola_m_", "_ola_w_")
        conhecidos_mkt = cloud_lines.templates_marketing(linha.slug)
        antes_mkt = len(tentativas)
        tentativas = [
            t for t in tentativas
            if t[0] not in conhecidos_mkt
            and not any(p in t[0] for p in _CASCO_MKT)
        ]
        if len(tentativas) != antes_mkt:
            log.info(
                "templates_marketing_fora_da_rotacao_utility",
                slug=linha.slug,
                ignorados=antes_mkt - len(tentativas),
                restantes=len(tentativas),
            )
        if not tentativas:
            raise RuntimeError(
                f"linha {linha.slug}: rotação ficou só com MARKETING — "
                "troca o modelo, nada enviado"
            )

    # ORDEM ENTRE CATEGORIAS — depois de a rotação estar montada (a da linha
    # própria só é construída acima, então ordenar antes não teria efeito).
    #
    # Padrão: UTILITY primeiro, MARKETING no fim — a rotação só chega no
    # MARKETING se nenhum UTILITY entregar. Sem isso a cota seria consumida à
    # toa, gastando 10x onde o barato funcionaria.
    #
    # ⚠️ EXCEÇÃO (Dona 30/07, teste A/B de categoria): quando o caller PEDE
    # marketing, ele quer marketing — não um fallback que nunca acontece. Com a
    # ordem padrão o braço "marketing" do teste sairia 100% em UTILITY e o
    # experimento compararia duas copies UTILITY achando que compara categoria.
    # A cota (`cota_marketing`) continua sendo o teto real; isto decide só a
    # ORDEM de tentativa dentro do que a cota já liberou.
    if marketing:
        tentativas.sort(key=lambda t: (t[0] in marketing) is not preferir_marketing)
        log.info("rotacao_ordem_categoria", slug=linha.slug,
                 prefere_marketing=preferir_marketing,
                 primeiro=tentativas[0][0] if tentativas else None)

    # Tenta o template do hash; se não estiver aprovado (ou falhar), rotaciona
    # pro próximo até um entregar. Assim funciona mesmo com parte ainda PENDING.
    # `template_fixo=(nome, idioma)`: manda ESTE template, sem rotação e sem
    # variáveis. Existe pra reaproveitar template UTILITY já aprovado cujo
    # corpo é fixo — a régua do proprietário usa o `continuar_atendimento`,
    # que tem os botões certos ("Prosseguir"/"Encerrar") e nenhum {{1}}.
    #
    # Vale a pena passar por aqui em vez de montar o POST no chamador: assim a
    # trava de opt-out, o DRY-RUN, a gravação no thread e o contrato de retorno
    # continuam sendo os mesmos do resto do sistema.
    if template_fixo:
        tentativas = [(template_fixo[0], template_fixo[1] or "pt_BR", [])]

    ultimo_erro = ""
    for tmpl, lang, params in tentativas:
        # Categoria REAL deste template (não a intenção): é o que vai pro
        # metadata e alimenta a contagem da cota.
        categoria_usada = "MARKETING" if tmpl in marketing else "UTILITY"
        payload: dict[str, Any] = {"name": tmpl, "language": {"code": lang}}
        # Template sem variável NÃO aceita `components` com body vazio — a Meta
        # devolve 132000 ("number of parameters does not match"). Só manda o
        # bloco quando há parâmetro de verdade.
        if params:
            payload["components"] = [{"type": "body", "parameters": params}]
        body = {"messaging_product": "whatsapp", "to": to,
                "type": "template", "template": payload}
        with httpx.Client(timeout=20) as c:
            resp = c.post(url, headers={"Authorization": f"Bearer {tok}",
                                        "Content-Type": "application/json"}, json=body)
        if resp.status_code < 400:
            data = resp.json()
            wamid_meta = (data.get("messages") or [{}])[0].get("id")
            # A Meta aceitou e não devolveu id: o thread precisa de uma chave
            # pra gravar, então fabricamos uma — e AVISAMOS quem chamou
            # (`wamid_fabricado`). Nenhum status de entrega chega nesse id;
            # quem guarda o wamid pra conferir entrega não pode confiar nele.
            wamid = wamid_meta or str(uuid.uuid4())
            # `template`/`categoria` no metadata: é o que permite CONTAR a cota
            # de MARKETING do dia (cota_marketing.contagem_hoje) e auditar depois
            # qual template saiu pra cada lead. Antes só ficava o delivery_status,
            # e sem isso não havia como saber a categoria do que já foi enviado.
            gravar(wamid=wamid, contato_telefone=to, contato_nome=None,
                   from_corretora=True, texto=f"[reengajamento: {gancho}]",
                   tipo="template", raw=data,
                   metadata={"template": tmpl, "categoria": categoria_usada,
                             **({"origem": origem} if origem else {})},
                   instance=instance, corretora_id=corretora_id)
            # Contrato normalizado (o chamador NÃO deve inferir `sent` de
            # messages[0].id: o dry-run também devolve esse campo, com prefixo
            # "dry-", e isso inverteria o flag de segurança).
            return {
                "ok": True, "dry_run": False, "sent": True,
                "wamid": wamid, "template": tmpl, "categoria": categoria_usada,
                "wamid_fabricado": not wamid_meta,
                "raw": data,
                "messages": data.get("messages") or [{"id": wamid}],
            }
        ultimo_erro = f"{resp.status_code}: {resp.text[:160]}"
        log.warning("corretor_reengajar_template_pulou", linha=linha.slug,
                    template=tmpl, erro=ultimo_erro)
        if _erro_de_linha(resp):
            # Erro de LINHA/conta (número bloqueado, WABA restrita, token morto):
            # rotacionar queima os 13 templates contra a mesma parede. Aborta já.
            log.error("corretor_reengajar_abort_linha", linha=linha.slug,
                      template=tmpl, erro=ultimo_erro)
            raise RuntimeError(f"reengajar abortado (erro de linha/conta) — {ultimo_erro}")

    log.error("corretor_reengajar_falha_total", linha=linha.slug, erro=ultimo_erro)
    raise RuntimeError(f"reengajar sem template aprovado — {ultimo_erro}")


def enviar_template_corretor(
    to_phone: str,
    first_name: str | None,
    hook: str | None = None,
    cta: str | None = None,
    variantes: tuple[str, ...] | None = None,
    *,
    com_botao: bool = False,
    variacao: int = 0,
    origem: str | None = None,
) -> dict[str, Any]:
    """Wrapper fino: reengajamento pela LINHA DA CORRETOR (2591). Comportamento e
    contrato de retorno inalterados — a lógica vive em `enviar_template_linha`.

    Mantido como função própria porque é o ponto de entrada histórico usado pelo
    painel, msg_planejada e nutrição (grep antes de renomear).

    `variacao`: desloca a rotação de template (a cadência passa o índice do
    toque, pra o rodapé não repetir ao longo da sequência do mesmo lead).
    """
    linha = _CORRETOR_LINE or cloud_lines.line_by_slug("corretor")
    if linha is None:  # registry sem a Pessoa12 = configuração quebrada, não silenciar
        raise RuntimeError("linha 'corretor' ausente do registry cloud_lines")
    return enviar_template_linha(
        linha, to_phone, first_name, hook=hook, cta=cta, variantes=variantes,
        instance=INSTANCE, corretora_id=CORRETORA_ID, com_botao=com_botao,
        variacao=variacao, origem=origem,
    )


def registrar_saida_corretor(to_phone: str, texto: str, tipo: str = "text") -> None:
    """Grava no thread (mensagens_whatsapp) uma mensagem que a Pessoa12 ENVIOU pelo
    Evolution do Mac. Dedup por conteúdo+telefone nos últimos 120s (evita duplicar
    caso a coexistência Cloud também ecoe o envio).
    """
    to = _so_digitos(to_phone)
    suf = to[-8:]
    with SessionLocal() as s:
        ja = s.execute(
            sql_text(
                """
                SELECT 1 FROM mensagens_whatsapp
                WHERE corretora_id = :cid AND from_corretora = TRUE
                  AND right(regexp_replace(contato_telefone, '\\D', '', 'g'), 8) = :suf
                  AND coalesce(conteudo_texto,'') = :txt
                  AND timestamp_evolution >= now() - interval '120 seconds'
                LIMIT 1
                """
            ),
            {"cid": CORRETORA_ID, "suf": suf, "txt": texto or f"[{tipo}]"},
        ).first()
    if ja:
        return
    gravar(wamid=f"evo-mac-{uuid.uuid4()}", contato_telefone=to, contato_nome=None,
           from_corretora=True, texto=texto or f"[{tipo}]", tipo=tipo)


@guarded
def enviar_cloud_midia(
    to_phone: str,
    media_bytes: bytes,
    *,
    mimetype: str,
    filename: str,
    tipo: str,            # "image" | "document" | "audio"
    caption: str = "",
    linha: "cloud_lines.CloudLine | None" = None,
    instance: str | None = None,
    corretora_id: int | None = None,
) -> dict[str, Any]:
    """Envia mídia pela Cloud API: upload → /media, depois /messages.

    Sem `linha`, sai pelo 2591 da Pessoa12 (comportamento histórico). Com `linha`, sai
    pelo phone_id e token DAQUELA linha — mesmo contrato do `enviar_cloud`. Faltava
    isto: a Pessoa7 (Cloud, sem instância Evolution) tentava mandar foto e caía no
    Evolution → 404 em /message/sendMedia/acme_pessoa_d (24/09).

    Grava o outbound na thread (mensagens_whatsapp). Dentro da janela 24h.
    """
    to = _so_digitos(to_phone)
    if _dry_run_ativo():
        log.info("cloud_dry_run", func="enviar_cloud_midia", to=to,
                 slug=linha.slug if linha else "corretor",
                 tipo=tipo, preview=(caption or "")[:120])
        wamid = f"dry-{uuid.uuid4()}"
        gravar(wamid=wamid, contato_telefone=to, contato_nome=None,
               from_corretora=True, texto=caption or f"[{tipo}]", tipo=tipo,
               instance=instance, corretora_id=corretora_id)
        return {"ok": True, "dry_run": True, "sent": False,
                "messages": [{"id": wamid}]}
    if linha is not None:
        tok = cloud_lines.token_for(linha)
        if not tok:
            raise RuntimeError(f"token ausente pra linha {linha.slug} ({linha.token_var})")
    else:
        tok = _token()
    phone_id = linha.phone_id if linha else PHONE_ID
    base = f"https://graph.facebook.com/{API_VER}/{phone_id}"
    with httpx.Client(timeout=40) as c:
        up = c.post(
            f"{base}/media",
            headers={"Authorization": f"Bearer {tok}"},
            data={"messaging_product": "whatsapp", "type": mimetype},
            files={"file": (filename, media_bytes, mimetype)},
        )
    if up.status_code >= 400:
        log.error("cloud_upload_falha", status=up.status_code, body=up.text[:200])
        raise RuntimeError(f"cloud upload {up.status_code}: {up.text[:160]}")
    media_id = up.json().get("id")
    if not media_id:
        raise RuntimeError("cloud upload sem media id")
    obj: dict[str, Any] = {"id": media_id}
    if tipo == "document":
        obj["filename"] = filename
    if caption and tipo in ("image", "document", "video"):
        obj["caption"] = caption
    body = {"messaging_product": "whatsapp", "to": to, "type": tipo, tipo: obj}
    with httpx.Client(timeout=30) as c:
        resp = c.post(f"{base}/messages",
                      headers={"Authorization": f"Bearer {tok}",
                               "Content-Type": "application/json"}, json=body)
    if resp.status_code >= 400:
        log.error("cloud_envio_midia_falha", status=resp.status_code, body=resp.text[:200])
        raise RuntimeError(f"cloud {resp.status_code}: {resp.text[:160]}")
    data = resp.json()
    wamid = (data.get("messages") or [{}])[0].get("id") or str(uuid.uuid4())
    gravar(wamid=wamid, contato_telefone=to, contato_nome=None,
           from_corretora=True, texto=caption or f"[{tipo}]", tipo=tipo, raw=data,
           instance=instance, corretora_id=corretora_id)
    return data


def ler_thread_cloud(
    telefone: str,
    hours_back: int = 720,
    *,
    corretora_id: int | None = None,
) -> list[dict[str, Any]]:
    """Monta a thread (formato do painel) a partir de mensagens_whatsapp.

    A thread é do LEAD, resolvida por TELEFONE — não da linha que transportou
    cada mensagem. `corretora_id` é aceito por compatibilidade (chamadores
    antigos) e NÃO filtra mais nada: quem protege escopo é o gate de DONO do
    lead em `/painel/conversa` (`_lead_conversa_contexto` + o 403 de
    `lead_de_outra_corretora`), que já rodou antes daqui. Cada mensagem sai
    carimbada com a linha de origem (`_linhas_por_msg_ids`, no painel), então a
    corretora vê por onde foi cada trecho.

    Multi-linha (28/07): Pessoa10/Pessoa11 em coexistência gravam template Cloud em
    mensagens_whatsapp, mas o chat lia só Evolution e a msg "sumia" (aparecia
    no celular, não no painel — caso BR-Jeison).

    🔴 Achado 01/08 (lead Lúcio, o 1º do carrossel a responder): o carrossel
    fala pela linha DA CASA (0000/Corretor) e o card é da corretora que vai atender —
    filtrando pela dona, o chat abria VAZIO com 36 mensagens no banco.

    🔴 Achado 02/09 (lead Rebeca, dona Pessoa5): a nutrição/remarketing dispara
    TODO toque pela linha da CORRETOR (nutricao.py, spec 10/07), então as respostas
    voltam gravadas com corretora_id=3. Com o filtro por corretora, o lead
    respondia "pode mandar as oportunidades" e NINGUÉM via: a Pessoa10 porque o
    filtro cortava, a Pessoa12 porque o card não é dela (403). Mesma classe do
    BR-Jeison, agora resolvida na raiz: some o filtro de linha.

    Escopo que permanece: só conversa da operação Acme do tenant. Linha sem
    corretora que não é da operação (bridge `cloud_assistente`, Assistente↔Dona) fica de
    fora — ver `_INSTANCES_FORA_DA_THREAD`.
    """
    suf = _so_digitos(telefone)[-8:]
    with SessionLocal() as s:
        rows = s.execute(
            sql_text(
                """
                SELECT evolution_message_id, from_corretora, tipo_mensagem,
                       conteudo_texto, transcricao, timestamp_evolution,
                       raw_payload, conteudo_metadata
                FROM mensagens_whatsapp
                WHERE right(regexp_replace(contato_telefone, '\\D', '', 'g'), 8) = :suf
                  AND tenant_id = :tenant
                  AND coalesce(instance, '') <> ALL(:fora)
                  AND timestamp_evolution >= now() - (:hb || ' hours')::interval
                ORDER BY timestamp_evolution ASC
                """
            ),
            {
                "suf": suf,
                "tenant": DEFAULT_TENANT_ID,
                "fora": list(_INSTANCES_FORA_DA_THREAD),
                "hb": hours_back,
            },
        ).mappings().all()
    out: list[dict[str, Any]] = []
    for r in rows:
        texto = r["conteudo_texto"] or ""
        # Templates gravam "[reengajamento: {corpo}]" — no chat mostra o corpo
        # limpo pra corretora reconhecer o que mandou.
        if (r["tipo_mensagem"] or "") == "template" and texto.startswith("[reengajamento:"):
            texto = texto[len("[reengajamento:"):].rstrip("]").strip()
        msg: dict[str, Any] = {
            "from": "corretora" if r["from_corretora"] else "lead",
            "text": texto,
            "at": (r["timestamp_evolution"].isoformat() if r["timestamp_evolution"] else None),
            "type": r["tipo_mensagem"] or "text",
            "id": r["evolution_message_id"],
            "transcricao": r["transcricao"],
        }
        # Documento/vídeo: nome original e mimetype pro chat mostrar "📎
        # contrato-assinado.pdf" em vez de um "documento" morto, e decidir
        # entre player de vídeo e link de download.
        if msg["type"] in {"document", "video", "image"}:
            from app.api.painel import _nome_arquivo_midia

            nome = _nome_arquivo_midia(r["raw_payload"])
            if nome:
                msg["filename"] = nome
            meta = r["conteudo_metadata"] or {}
            mime = meta.get("mime_type") or meta.get("mimetype")
            if mime:
                msg["mime"] = mime
        out.append(msg)
    return out


def build_status_patch(
    status: str,
    timestamp: str | int | None = None,
    errors: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Monta o patch de delivery pro conteudo_metadata a partir de um status da Meta.

    `delivery_error` só é preenchido em status `failed` (título ou mensagem do 1º erro).
    `delivery_error_code` vai junto quando a Meta manda o código: é ele que diz de
    quem é a culpa da falha (lista, conta ou template — ver
    `remarketing_placar.culpa_de`). Só com o título, um 131026 e um erro novo
    qualquer ficam indistinguíveis.
    Função pura (sem DB) — testável isoladamente.
    """
    ts_iso: str | None = None
    if timestamp is not None:
        try:
            ts_iso = datetime.fromtimestamp(
                int(timestamp), tz=timezone.utc
            ).isoformat()
        except (ValueError, TypeError, OSError):
            ts_iso = None

    patch: dict[str, Any] = {"delivery_status": status, "delivery_ts": ts_iso}
    if status == "failed" and errors:
        err = errors[0] or {}
        patch["delivery_error"] = err.get("title") or err.get("message")
        if err.get("code") is not None:
            patch["delivery_error_code"] = str(err["code"])
    return patch


def aplicar_status(
    *,
    wamid: str,
    status: str,
    timestamp: str | int | None = None,
    errors: list[dict[str, Any]] | None = None,
) -> bool:
    """Mescla o status de entrega (sent/delivered/read/failed) no conteudo_metadata
    da mensagem da Pessoa12. Localiza por evolution_message_id (wamid) + instance.

    Retorna True se atualizou uma linha, False se o wamid não foi achado (não é erro:
    a Meta manda status até de mensagens que o copiloto nunca gravou).
    """
    patch = build_status_patch(status, timestamp, errors)

    import json as _json
    try:
        with SessionLocal() as s:
            res = s.execute(
                sql_text(
                    """
                    UPDATE mensagens_whatsapp
                    SET conteudo_metadata =
                        coalesce(conteudo_metadata, '{}'::jsonb) || CAST(:patch AS jsonb)
                    WHERE evolution_message_id = :wamid AND instance = :instance
                    """
                ),
                {"patch": _json.dumps(patch), "wamid": wamid, "instance": INSTANCE},
            )
            s.commit()
            ok = (res.rowcount or 0) > 0

        # Falha de entrega em número que NUNCA deu sinal de vida = número que
        # não existe no WhatsApp. Marca pra o carrossel parar de discar pra ele
        # a cada 30 dias — era o que empurrava a taxa de falha do 0000 pra 15%.
        if ok and status == "failed":
            try:
                from app.services.telefone_invalido import marcar_se_morto

                with SessionLocal() as s2:
                    tel = s2.execute(
                        sql_text(
                            "SELECT contato_telefone FROM mensagens_whatsapp "
                            "WHERE evolution_message_id = :w LIMIT 1"
                        ),
                        {"w": wamid},
                    ).scalar()
                if tel:
                    marcar_se_morto(tel)
            except Exception as e2:  # noqa: BLE001 — nunca derruba o webhook
                log.warning("marcar_invalido_pos_status", erro=str(e2)[:140])

            # E a falha que NÃO é do número: conta sem pagamento derruba a linha
            # inteira e só se resolve no Business Manager. Ver alerta_entrega.
            from app.services import alerta_entrega

            alerta_entrega.avisar_falha_de_conta(
                linha="corretor", erro=patch.get("delivery_error")
            )
        return ok
    except Exception as e:  # noqa: BLE001 — best-effort, não derruba o webhook
        log.warning("cloud_status_falha", wamid=wamid, erro=str(e)[:160])
        return False


def _echo_destino(echo: dict[str, Any]) -> str | None:
    """Extrai o número do DESTINATÁRIO de um echo (mensagem que a Pessoa12 mandou pelo
    celular). Na coexistência o echo tem `from` = número do negócio; o lead vem
    em `to` (ou `recipient_id` em algumas versões)."""
    to = echo.get("to") or echo.get("recipient_id")
    return _so_digitos(to) if to else None


def _echo_texto_tipo(echo: dict[str, Any]) -> tuple[str, str]:
    """Retorna (tipo, texto) de um echo, cobrindo text e mídia com caption."""
    tipo = echo.get("type", "text")
    if tipo == "text":
        texto = (echo.get("text") or {}).get("body", "")
    else:
        texto = texto_de_midia(echo, tipo)
    return tipo, texto


def _echo_ja_registrado_por_conteudo(
    s: Any, *, destino: str, texto: str, tipo: str
) -> bool:
    """Dedup camada (b): um envio feito PELO PAINEL/Evolution-Mac que a Meta
    também ecoa. `enviar_cloud`/`enviar_cloud_midia` já gravam o wamid real do
    Graph → dedup por wamid cobre esses. Mas `registrar_saida_corretor` (Evolution-Mac)
    grava wamid sintético `evo-mac-*`; nesse caso casamos por conteúdo+telefone
    numa janela curta pra não duplicar."""
    suf = destino[-8:]
    ja = s.execute(
        sql_text(
            """
            SELECT 1 FROM mensagens_whatsapp
            WHERE corretora_id = :cid AND from_corretora = TRUE
              AND right(regexp_replace(contato_telefone, '\\D', '', 'g'), 8) = :suf
              AND coalesce(conteudo_texto,'') = :txt
              AND timestamp_evolution >= now() - interval '300 seconds'
            LIMIT 1
            """
        ),
        {"cid": CORRETORA_ID, "suf": suf, "txt": texto or f"[{tipo}]"},
    ).first()
    return ja is not None


def _echo_ja_registrado_por_media(s: Any, *, media_id: str) -> bool:
    """Dedup de MÍDIA por cloud_media_id (janela 300s).

    Áudios/imagens gravam `conteudo_texto` como placeholder ("[audio]"/"[image]"),
    então dedup por conteúdo (`_echo_ja_registrado_por_conteudo`) dá FALSO-POSITIVO
    entre mídias DIFERENTES pro mesmo lead (ex: 2 áudios seguidos → o 2º sumia).
    O media_id é único por mídia, então casa só re-entregas do MESMO echo
    (disfarçado + field dedicado, ou retry da Meta). Dedup por wamid em gravar()
    já cobre o mesmo wamid; este cobre o caso de wamids distintos p/ a mesma mídia."""
    ja = s.execute(
        sql_text(
            """
            SELECT 1 FROM mensagens_whatsapp
            WHERE corretora_id = :cid AND from_corretora = TRUE
              AND conteudo_metadata->>'cloud_media_id' = :mid
              AND timestamp_evolution >= now() - interval '300 seconds'
            LIMIT 1
            """
        ),
        {"cid": CORRETORA_ID, "mid": media_id},
    ).first()
    return ja is not None


def registrar_echo_corretor(echo: dict[str, Any]) -> str:
    """Grava um echo (mensagem que a Pessoa12 mandou pelo CELULAR) no thread como
    outbound (from_corretora=True). Destinatário = `to`/`recipient_id`.

    Dedup em 2 camadas:
      (a) por wamid (SELECT em gravar()) — cobre re-entrega do mesmo echo E
          envios via API do painel (que já gravam o wamid real do Graph);
      (b) por conteúdo+telefone (janela 300s) — cobre envios via Evolution-Mac
          gravados com wamid sintético.

    Retorna: "gravado" | "dup_wamid" | "dup_conteudo" | "sem_destino".
    """
    destino = _echo_destino(echo)
    if not destino:
        log.warning("cloud_echo_sem_destino", wamid=echo.get("id"))
        return "sem_destino"
    tipo, texto = _echo_texto_tipo(echo)
    wamid = echo.get("id") or str(uuid.uuid4())
    ts = None
    if echo.get("timestamp"):
        try:
            ts = datetime.fromtimestamp(int(echo["timestamp"]), tz=timezone.utc)
        except (ValueError, TypeError, OSError):
            ts = None

    # Extrai a info de mídia primeiro — ela define a ESTRATÉGIA de dedup.
    info = extrair_media_info(echo)
    meta = {"cloud_media_id": info[0], "mime_type": info[1]} if info else None

    with SessionLocal() as s:
        if info:
            # MÍDIA: dedup por media_id, nunca pelo texto placeholder ("[audio]"),
            # que colide entre mídias diferentes pro mesmo lead (dropava áudios).
            if _echo_ja_registrado_por_media(s, media_id=info[0]):
                return "dup_conteudo"
        else:
            # TEXTO: dedup por conteúdo (cobre sync via Evolution-Mac wamid sintético).
            if _echo_ja_registrado_por_conteudo(s, destino=destino, texto=texto, tipo=tipo):
                return "dup_conteudo"

    gravou = gravar(
        wamid=wamid, contato_telefone=destino, contato_nome=None,
        from_corretora=True, texto=texto, tipo=tipo, ts=ts, raw=echo, metadata=meta,
    )
    return "gravado" if gravou else "dup_wamid"


@router.post("/echo")
async def cloud_echo(
    req: Request,
    x_cloud_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    """Recebe echoes (mensagens que a Pessoa12 enviou PELO CELULAR na coexistência),
    repassados pelo api-disparos já filtrados p/ o 2591. Grava cada um no thread
    como outbound, pra resposta do celular entrar na conversa do painel.

    Auth: o caller (api-disparos) DEVE mandar o header X-Cloud-Secret."""
    _auth_cloud_webhook(x_cloud_secret)
    payload = await req.json()
    echoes = payload.get("echoes") or []
    gravados = 0
    dups = 0
    sem_destino = 0
    # Distribuição por tipo:resultado — diagnóstico de quais tipos de echo a Meta
    # entrega de fato (ex: se "audio:*" aparece = echo de áudio está chegando).
    from collections import Counter
    tipos: Counter[str] = Counter()
    for echo in echoes:
        if not isinstance(echo, dict):
            continue
        tipo = echo.get("type", "text")
        r = registrar_echo_corretor(echo)
        tipos[f"{tipo}:{r}"] += 1
        if r == "gravado":
            gravados += 1
        elif r == "sem_destino":
            sem_destino += 1
        else:
            dups += 1
    log.info("cloud_echo", gravados=gravados, dups=dups,
             sem_destino=sem_destino, tipos=dict(tipos))
    return {"ok": True, "gravados": gravados, "dups": dups, "sem_destino": sem_destino}


@router.post("/status")
async def cloud_status(
    req: Request,
    x_cloud_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    """Recebe statuses (sent/delivered/read/failed) repassados pelo api-disparos
    (já filtrados p/ o 2591). Marca o estado de entrega no thread da Pessoa12.

    Auth: o caller (api-disparos) DEVE mandar o header X-Cloud-Secret."""
    _auth_cloud_webhook(x_cloud_secret)
    payload = await req.json()
    statuses = payload.get("statuses") or []
    aplicados = 0
    nao_encontrados = 0
    for st in statuses:
        wamid = st.get("id")
        status = st.get("status")
        if not wamid or not status:
            continue
        if aplicar_status(
            wamid=wamid,
            status=status,
            timestamp=st.get("timestamp"),
            errors=st.get("errors"),
        ):
            aplicados += 1
        else:
            nao_encontrados += 1
            log.info("cloud_status_wamid_nao_encontrado", wamid=wamid, status=status)
    log.info("cloud_status", aplicados=aplicados, nao_encontrados=nao_encontrados)
    return {"ok": True, "aplicados": aplicados, "nao_encontrados": nao_encontrados}


def _mover_kommo_followup_para_atendimento(lead_id: str) -> str:
    """Move o lead de Follow-up -> Em atendimento no Funil de Vendas, SÓ se ele
    ainda está em Follow-up (não sobrescreve etapa mais avançada). Best-effort:
    qualquer falha do Kommo é logada e engolida (não derruba o inbound).

    Retorna: "movido" | "ja_avancou" | "kommo_falha".
    """
    from app.tools.kommo import KommoTool
    try:
        kommo = KommoTool()
        lead = kommo._client.get_lead(lead_id)
        status_atual = lead.get("status_id")
        if status_atual != KOMMO_STAGE_FOLLOWUP:
            log.info("disparo_resp_lead_ja_avancou", lead_id=lead_id,
                     status_atual=status_atual)
            return "ja_avancou"
        kommo.move_lead_stage(
            lead_id, KOMMO_STAGE_EM_ATENDIMENTO, pipeline_id=KOMMO_PIPELINE_FUNIL,
        )
        # Marcador na timeline do Kommo pro front/Pessoa12 destacar (DRY_RUN-safe).
        try:
            kommo.add_lead_note(
                lead_id,
                "respondeu_disparo — lead respondeu ao reengajamento da Pessoa12; "
                "movido de Follow-up para Em atendimento.",
            )
        except Exception as ne:  # noqa: BLE001
            log.warning("disparo_resp_nota_falha", lead_id=lead_id, erro=str(ne)[:120])
        return "movido"
    except Exception as e:  # noqa: BLE001 — Kommo fora não pode derrubar o inbound
        log.error("disparo_resp_kommo_falha", lead_id=lead_id, erro=str(e)[:160])
        return "kommo_falha"


def tratar_resposta_disparo(telefone: str, texto: str | None) -> str | None:
    """Se `telefone` tem um disparo de reengajamento recente (<=14 dias) ainda
    sem resposta, trata a mensagem do lead como resposta ao disparo.

    - opt-out  -> marca opt_out no registro, NÃO move etapa.
    - normal   -> marca respondeu_em e move o lead Follow-up -> Em atendimento.

    À prova de falha: qualquer exceção é logada e engolida (não afeta a gravação
    da mensagem no inbound). Retorna o resultado ("opt_out"|"respondido"|
    "sem_disparo"|None-em-erro) só pra observabilidade/teste.
    """
    try:
        suf = _so_digitos(telefone)[-8:]
        if not suf:
            return "sem_disparo"
        with SessionLocal() as s:
            row = s.execute(
                sql_text(
                    """
                    SELECT id, lead_id
                    FROM disparos_reengajamento
                    WHERE right(regexp_replace(telefone, '\\D', '', 'g'), 8) = :suf
                      AND enviado_em >= now() - interval '14 days'
                      AND respondeu_em IS NULL
                      AND opt_out IS NOT TRUE
                    ORDER BY enviado_em DESC
                    LIMIT 1
                    """
                ),
                {"suf": suf},
            ).mappings().first()
            if not row:
                return "sem_disparo"

            disparo_id = row["id"]
            lead_id = row["lead_id"]

            if _is_opt_out(texto):
                s.execute(
                    sql_text(
                        "UPDATE disparos_reengajamento "
                        "SET opt_out = TRUE, opt_out_em = now() WHERE id = :id"
                    ),
                    {"id": disparo_id},
                )
                s.commit()
                log.info("disparo_opt_out", disparo_id=disparo_id, lead_id=lead_id,
                         telefone_suf=suf)
                return "opt_out"

            # Resposta normal: marca respondeu_em e move a etapa no Kommo.
            s.execute(
                sql_text(
                    "UPDATE disparos_reengajamento "
                    "SET respondeu_em = now() WHERE id = :id"
                ),
                {"id": disparo_id},
            )
            s.commit()

        move_res = _mover_kommo_followup_para_atendimento(str(lead_id)) if lead_id else "sem_lead_id"
        log.info("disparo_respondido", disparo_id=disparo_id, lead_id=lead_id,
                 telefone_suf=suf, kommo=move_res)
        return "respondido"
    except Exception as e:  # noqa: BLE001 — nunca derruba o inbound
        log.warning("disparo_resposta_erro", erro=str(e)[:160])
        return None


def _tratar_resposta_cadencia_inbound(
    telefone: str, texto: str | None, tipo_mensagem: str | None = None,
    *, velho: bool = False,
) -> str | None:
    """Ponte fina pro hook da cadência (P2): lead respondeu → cadência para
    (respondida/opt_out) + lead canônico move ("Em atendimento" ou, no opt-out,
    "Venda perdida"). Resposta <5s após o nosso envio é auto-reply e é ignorada.
    Aviso à corretora é só in-app (aviso WhatsApp aposentado — regra Dona 10/07).
    Import tardio: `cadencia` importa `enviar_template_corretor` daqui no sender
    default (evita ciclo). Best-effort: processar_resposta_cadencia nunca levanta."""
    from app.services.cadencia import processar_resposta_cadencia

    # `tipo_mensagem` importa: clique em botão de template NÃO é resposta (não
    # para a cadência) — quem trata é a própria cadência.
    # `velho` (X-Cloud-Velho): régua para e card anda, mas nada sai pro lead.
    return processar_resposta_cadencia(telefone, texto, tipo_mensagem=tipo_mensagem,
                                       velho=velho)


def _tratar_saida_remarketing(
    telefone: str, texto: str | None, tipo_mensagem: str | None = None
) -> str | None:
    """Ponte fina pra porta de saída do remarketing (19/09/2026).

    Import tardio pelo mesmo motivo do hook da cadência: `saida_remarketing`
    importa `cadencia`, que importa `enviar_template_corretor` daqui. Best-effort —
    `processar_inbound` nunca levanta e devolve None quando está desarmado."""
    from app.services.saida_remarketing import processar_inbound

    return processar_inbound(telefone, texto, tipo_mensagem=tipo_mensagem)


def _tratar_resposta_regua_proprietario(telefone: str, texto: str) -> None:
    """Wrapper best-effort da régua do proprietário pro webhook.

    Import tardio de propósito: o módulo da régua importa `cloud_corretora_a` de
    volta (pra enviar o template), e no topo isso seria import circular.
    """
    try:
        from app.services.regua_proprietario import tratar_resposta_inbound

        tratar_resposta_inbound(telefone, texto)
    except Exception:  # noqa: BLE001
        log.exception("regua_proprietario_webhook_falhou", to=telefone[-4:])


def texto_de_midia(m: dict[str, Any], tipo: str) -> str:
    """Texto de uma mensagem que não é `text`: a legenda, ou um marcador.

    Cartão de contato (`contacts`) vem como LISTA, não como objeto. Até
    05/10/2026 o `.get("caption")` em cima dela estourava e a porta devolvia
    500: a mensagem não era gravada e o evento ficava preso no api-disparos
    (8 dos 34 presos de 28/09 a 05/10 eram cartões de contato). O cartão entra
    com nome e telefone, que é o que a corretora precisa ler."""
    obj = m.get(tipo)
    if isinstance(obj, dict):
        return obj.get("caption") or f"[{tipo}]"
    if tipo == "contacts" and isinstance(obj, list):
        cartoes = []
        for c in obj:
            if not isinstance(c, dict):
                continue
            nome = ((c.get("name") or {}).get("formatted_name") or "").strip()
            fones = [str(f.get("phone") or f.get("wa_id") or "").strip()
                     for f in (c.get("phones") or []) if isinstance(f, dict)]
            cartoes.append(" ".join(x for x in (nome, next((f for f in fones if f), "")) if x))
        cartoes = [c for c in cartoes if c]
        if cartoes:
            return "[contato] " + "; ".join(cartoes)
    return f"[{tipo}]"


def mensagem_velha(req: Request) -> bool:
    """O api-disparos marca com `X-Cloud-Velho: 1` a mensagem que a Meta
    reentregou depois do corte de idade (30 min). Quem recebe grava e registra,
    mas não fala com ninguém. Sem o header vale o comportamento de sempre."""
    headers = getattr(req, "headers", None) or {}
    return (headers.get("x-cloud-velho") or "").strip() == "1"


# ⚠️ O DECORADOR FICA COLADO NESTA função. Em 16/09 (bde8ade4) um helper novo
# foi inserido ENTRE o `@router.post("/inbound")` e este handler: o decorador
# passou a registrar o helper, que tem assinatura `(telefone, texto)`, então o
# FastAPI virou a rota em query-params e TODO inbound da linha da Pessoa12 respondeu
# 422 — mensagem de cliente sumindo sem erro visível por 2 dias.
@router.post("/inbound")
async def cloud_inbound(
    req: Request,
    x_cloud_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    """Recebe mensagens repassadas pelo api-disparos (já filtradas p/ o 2591).

    Auth: o caller (api-disparos) DEVE mandar o header X-Cloud-Secret."""
    _auth_cloud_webhook(x_cloud_secret)
    from fastapi.concurrency import run_in_threadpool

    velho = mensagem_velha(req)
    payload = await req.json()
    msgs = payload.get("messages") or []
    contatos = {c.get("wa_id"): (c.get("profile") or {}).get("name")
                for c in (payload.get("contacts") or [])}
    n = 0
    for m in msgs:
        frm = m.get("from")
        if not frm:
            continue
        tipo = m.get("type", "text")
        if tipo == "text":
            texto = (m.get("text") or {}).get("body", "")
        elif tipo == "button":
            # Clique em botão de template: o rótulo vem em button.text (ex.:
            # "Saiba mais" / "Parar mensagens"). Antes caía no `caption`, que
            # não existe aqui, e o texto virava o literal "[button]" — com isso
            # nem o opt-out por botão era reconhecido.
            texto = (m.get("button") or {}).get("text") or "[botão]"
        elif tipo == "interactive":
            inter = m.get("interactive") or {}
            br = inter.get("button_reply") or inter.get("list_reply") or {}
            texto = br.get("title") or "[interativo]"
        else:
            texto = texto_de_midia(m, tipo)
        ts = None
        if m.get("timestamp"):
            ts = datetime.fromtimestamp(int(m["timestamp"]), tz=timezone.utc)
        # Rastreia o media_id da mídia pro download posterior (transcrição etc);
        # NÃO baixa aqui pra não segurar o webhook.
        meta = None
        info = extrair_media_info(m)
        if info:
            meta = {"cloud_media_id": info[0], "mime_type": info[1]}
        if gravar(wamid=m.get("id") or str(uuid.uuid4()), contato_telefone=frm,
                  contato_nome=contatos.get(frm), from_corretora=False,
                  texto=texto, tipo=tipo, ts=ts, raw=m, metadata=meta):
            n += 1
            # Resposta a disparo de reengajamento? (move etapa / opt-out).
            # Off-thread p/ não bloquear o event loop com DB + Kommo síncronos.
            # Best-effort: falhas ficam contidas em tratar_resposta_disparo.
            await run_in_threadpool(tratar_resposta_disparo, frm, texto)
            # Resposta à CADÊNCIA híbrida (P2)? Para a cadência, move o lead
            # canônico e (atrás de flag) avisa a corretora. Best-effort.
            await run_in_threadpool(
                _tratar_resposta_cadencia_inbound, frm, texto, tipo, velho=velho
            )
            # PORTA DE SAÍDA DO REMARKETING (19/09): o hook da cadência acima
            # PARA a régua, mas não tira o card do funil de remarketing — as
            # etapas "Base"/"30 dias"… não estão em `_STAGES_MOVIVEIS`, então o
            # lead segue na coluna de fila (391 leads em "30 dias" com a Pessoa12 como
            # dona em 19/09). Isto completa a saída: card de volta pro funil
            # principal quando há sinal, "Venda perdida" no opt-out.
            # DESARMADO por padrão, não manda mensagem, nunca levanta.
            await run_in_threadpool(
                _tratar_saida_remarketing, frm, texto, tipo
            )
            # Lead sinalizou "já comprei"? Cria nota interna de SUGESTÃO de
            # indicação (R$500) pra corretora revisar. NUNCA envia. Idempotente.
            await run_in_threadpool(tratar_intencao_indicacao, frm, texto)
            # Dono respondeu à régua do proprietário ("Continua à venda" /
            # "Já vendi" / "Desisti de vender")? Atualiza a situação do imóvel
            # e tira da roda. Best-effort: sem toque recente pra este telefone
            # devolve None e não mexe em nada — mudar situação por palpite é
            # como sumir com o imóvel da carteira.
            #
            # Mensagem velha (reentrega atrasada da Meta) não entra aqui: este
            # é o único gancho desta porta que RESPONDE ao contato. Os de cima
            # só registram, param régua e movem card.
            if velho:
                log.warning("cloud_inbound_velho_sem_resposta", tel=frm[-4:])
            else:
                await run_in_threadpool(_tratar_resposta_regua_proprietario, frm, texto)
    log.info("cloud_inbound", gravadas=n)
    return {"ok": True, "gravadas": n}
