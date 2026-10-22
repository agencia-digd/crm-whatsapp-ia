"""Canal WhatsApp Cloud API da linha da Assistente/Assistente (2955, coexistência).

A linha 2955 é RESPONSIVA: responde qualquer remetente dentro da janela de 24h
e NUNCA inicia conversa fora dela (sem template). Quem decide a resposta é a
BRIDGE (whatsapp-claude: Dona→Assistente, corretoras→Assistente, corretor→corretor, resto→corretor). O
copiloto aqui é só a PONTE entre o webhook Cloud (recebido pelo api-disparos) e
a bridge:

- `/inbound`: grava a mensagem em `mensagens_whatsapp` (instance='cloud_assistente',
  dedup por wamid) E repassa pra bridge — adaptando o payload Cloud pro shape de
  webhook que a bridge já entende (envelope Meta-Graph → endpoint /cloud-webhook).
- `/echo`: grava mensagens que a Assistente/Assistente enviou PELO CELULAR (coexistência)
  como outbound (from_corretora=True). NÃO repassa pra bridge — eco não é
  pergunta, não deve disparar resposta automática.
- `/status`: marca entrega (sent/delivered/read/failed) no thread.
- `/enviar`: a bridge chama aqui (com header-secret) quando quer que o copiloto
  ENVIE a resposta pela Cloud API da Assistente (texto livre, dentro da janela 24h).
- `/briefing/aviso`: (X-Internal-Token) o timer seg/sex chama aqui pra enviar o
  TEMPLATE de aviso de briefing pra UMA corretora (briefing_push).

Botões Quick Reply do template de briefing (chegam no /inbound como type
"button"): "Saiba mais" de uma CORRETORA → entrega o briefing completo pela
janela recém-aberta; "Parar mensagens" → opt-out do push (briefing_optout).
Botão de quem NÃO é corretora segue o fluxo normal (repassa pra bridge).

Token vem de /etc/agencia/assistente-cloudapi.env via o registry `cloud_lines` (ASSISTENTE_WA_TOKEN).
NUNCA loga o valor do token.
"""
from __future__ import annotations

import json as _json
import os
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
import structlog
from fastapi import BackgroundTasks, HTTPException, APIRouter, Header, Request
from pydantic import BaseModel, Field
from sqlalchemy import select, text as sql_text

from app.api import cloud_corretora_a as corretor
from app.config import settings
from app.db.models import Corretora
from app.db.session import SessionLocal
from app.services import cloud_lines

log = structlog.get_logger()

INSTANCE = "cloud_assistente"                # discrimina as mensagens da Assistente no thread
API_VER = "v21.0"
_ASSISTENTE_LINE = cloud_lines.line_by_slug("assistente")
PHONE_ID = _ASSISTENTE_LINE.phone_id if _ASSISTENTE_LINE else "000000000000000"  # +5500000000000
PHONE_ID_CORRETOR = "000000000000000"        # +5500000000000 (Pessoa12/Pessoa) — echo grava como acme_corretora_a

# Código de erro da Cloud API quando a janela de 24h está fechada (free-form
# bloqueado; só template resolveria — e o canal Assistente NUNCA manda template).
WINDOW_CLOSED_CODE = 131047

router = APIRouter(prefix="/cloud/assistente", tags=["cloud-assistente"])


def _so_digitos(tel: str) -> str:
    return "".join(c for c in (tel or "") if c.isdigit())


# ─── persistência (mensagens_whatsapp, instance='cloud_assistente') ───────────────
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
) -> bool:
    """Insere uma mensagem da linha Assistente em mensagens_whatsapp. Idempotente por
    wamid. corretora_id fica NULL (a Assistente não é corretora — é o canal
    dono/atendimento). Espelha o shape do gravar() da Pessoa12 pro painel exibir igual.
    """
    tel = _so_digitos(contato_telefone)
    remote_jid = f"{tel}@s.whatsapp.net"
    when = ts or datetime.now(timezone.utc)
    try:
        with SessionLocal() as s:
            ja = s.execute(
                sql_text("SELECT 1 FROM mensagens_whatsapp "
                         "WHERE evolution_message_id = :w AND instance = :inst LIMIT 1"),
                {"w": wamid, "inst": INSTANCE},
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
                        (:id, :wamid, :instance, NULL, :jid, :tel, :nome, :fromc,
                         :tipo, :texto, CAST(:meta AS jsonb), :ts, now(),
                         CAST(:raw AS jsonb))
                    """
                ),
                {
                    "id": str(uuid.uuid4()), "wamid": wamid, "instance": INSTANCE,
                    "jid": remote_jid, "tel": tel, "nome": contato_nome,
                    "fromc": from_corretora, "tipo": tipo, "texto": texto, "ts": when,
                    "meta": _json.dumps(metadata) if metadata else None,
                    "raw": _json.dumps(raw or {}),
                },
            )
            s.commit()
            return True
    except Exception as e:  # noqa: BLE001 — best-effort, não derruba o webhook
        log.warning("cloud_assistente_gravar_falha", wamid=wamid, erro=str(e)[:160])
        return False


def aplicar_status(
    *,
    wamid: str,
    status: str,
    timestamp: str | int | None = None,
    errors: list[dict[str, Any]] | None = None,
    instance: str = INSTANCE,
) -> bool:
    """Mescla status de entrega no conteudo_metadata da mensagem, localizada por
    (evolution_message_id, instance). Generalizado por `instance` — a lógica de
    patch é a pura `cloud_corretora_a.build_status_patch` (reuso, sem duplicar).

    Retorna True se atualizou uma linha; False se o wamid não foi achado (normal:
    a Meta manda status até de mensagens que o copiloto nunca gravou).
    """
    patch = corretor.build_status_patch(status, timestamp, errors)
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
                {"patch": _json.dumps(patch), "wamid": wamid, "instance": instance},
            )
            s.commit()
            return (res.rowcount or 0) > 0
    except Exception as e:  # noqa: BLE001 — best-effort, não derruba o webhook
        log.warning("cloud_assistente_status_falha", wamid=wamid, erro=str(e)[:160])
        return False


# ─── adaptador Cloud → shape que a bridge entende ────────────────────────────
def adaptar_para_bridge(msg: dict[str, Any], nome: str | None) -> dict[str, Any]:
    """Monta um envelope de webhook Meta-Graph MÍNIMO a partir de UMA mensagem
    Cloud inbound, no shape que a bridge consome em POST /cloud-webhook
    (cloudapi.parse_inbound espera entry[].changes[].value com messages+contacts).

    A bridge lê: value.messages[].{from,type,text.body,button.text,interactive,id}
    e value.contacts[].{wa_id, profile.name}. Reconstruímos exatamente isso,
    preservando o wamid (id) pra dedup na bridge (_ja_visto).
    """
    frm = msg.get("from")
    contacts = [{"wa_id": frm, "profile": {"name": nome or "?"}}] if frm else []
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": PHONE_ID,
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"phone_number_id": PHONE_ID},
                            "contacts": contacts,
                            "messages": [msg],
                        },
                    }
                ],
            }
        ],
    }


def repassar_para_bridge(msg: dict[str, Any], nome: str | None) -> bool:
    """POST best-effort do envelope adaptado pra bridge. Nunca levanta —
    o inbound do copiloto não pode falhar por causa da bridge estar fora."""
    envelope = adaptar_para_bridge(msg, nome)
    try:
        httpx.post(settings.assistente_bridge_cloud_url, json=envelope, timeout=8)
        return True
    except Exception as e:  # noqa: BLE001 — best-effort
        log.warning("cloud_assistente_bridge_forward_falhou", erro=str(e)[:160])
        return False


# ─── envio de RESPOSTA (texto livre, janela 24h) ─────────────────────────────
def enviar_cloud_assistente(to: str, texto: str) -> dict[str, Any]:
    """Envia texto livre pela Cloud API da Assistente (2955), dentro da janela de 24h.

    Guardas (nesta ordem, pós-incidentes):
      1. PYTEST_CURRENT_TEST → NUNCA envia de verdade (mock esquecido não vaza).
      2. dry_run_for(assistente) → loga em vez de enviar (CLOUD_DRY_RUN=true).
      3. token ausente → não envia, retorna estruturado.
    Erro 131047 (janela fechada) → log `cloud_assistente_fora_de_janela` + retorno
    estruturado, SEM retry e SEM template (o canal é responsivo, não inicia).

    Grava o outbound no thread (instance='cloud_assistente', from_corretora=True) quando
    o envio (ou dry-run) é aceito, pra resposta aparecer no painel.
    """
    to_num = _so_digitos(to)

    # GUARDA ANTI-TESTE (padrão obrigatório pós-incidentes — ver
    # tools/telegram.py send_to_pessoa_telegram). pytest seta PYTEST_CURRENT_TEST;
    # sob teste NUNCA envia de verdade, independente de mock esquecido.
    if os.environ.get("PYTEST_CURRENT_TEST"):
        log.info("cloud_assistente_envio_bloqueado_pytest", to=to_num, preview=texto[:60])
        return {"ok": True, "pytest": True, "sent": False}

    line = _ASSISTENTE_LINE or "assistente"
    if cloud_lines.dry_run_for(line):
        log.info("cloud_assistente_dry_run", to=to_num, preview=texto[:120])
        gravar(wamid=f"dry-{uuid.uuid4()}", contato_telefone=to_num,
               contato_nome=None, from_corretora=True, texto=texto)
        return {"ok": True, "dry_run": True, "sent": False}

    tok = cloud_lines.token_for(line)
    if not tok:
        log.error("cloud_assistente_sem_token")
        return {"ok": False, "error": "no_token", "sent": False}

    url = f"https://graph.facebook.com/{API_VER}/{PHONE_ID}/messages"
    body = {"messaging_product": "whatsapp", "to": to_num,
            "type": "text", "text": {"body": texto, "preview_url": False}}
    try:
        with httpx.Client(timeout=20) as c:
            resp = c.post(url, headers={"Authorization": f"Bearer {tok}",
                                        "Content-Type": "application/json"}, json=body)
    except Exception as e:  # noqa: BLE001 — rede
        log.warning("cloud_assistente_envio_erro_rede", to=to_num, erro=str(e)[:160])
        return {"ok": False, "error": "network", "detail": str(e)[:160], "sent": False}

    if resp.status_code >= 400:
        code = _extrair_error_code(resp)
        if code == WINDOW_CLOSED_CODE:
            # Janela de 24h fechada: NÃO retenta, NÃO manda template. O canal Assistente
            # é responsivo — se a janela fechou, a resposta simplesmente não sai.
            log.info("cloud_assistente_fora_de_janela", to=to_num, code=code)
            return {"ok": False, "error": "fora_de_janela",
                    "code": WINDOW_CLOSED_CODE, "sent": False}
        log.error("cloud_assistente_envio_falha", status=resp.status_code,
                  body=resp.text[:200])
        return {"ok": False, "error": "graph_error",
                "status": resp.status_code, "sent": False}

    data = resp.json()
    wamid = (data.get("messages") or [{}])[0].get("id") or str(uuid.uuid4())
    gravar(wamid=wamid, contato_telefone=to_num, contato_nome=None,
           from_corretora=True, texto=texto, raw=data)
    return {"ok": True, "sent": True, "wamid": wamid}


def _extrair_error_code(resp: httpx.Response) -> int | None:
    """Extrai o error.code do corpo de erro da Graph API (tolerante a parsing)."""
    try:
        err = (resp.json() or {}).get("error") or {}
        code = err.get("code")
        return int(code) if code is not None else None
    except Exception:  # noqa: BLE001
        return None


# ─── botões Quick Reply do template de briefing (corretoras) ─────────────────
def _corretora_por_telefone(tel: str) -> Corretora | None:
    """Corretora ATIVA cujo telefone_normalizado casa com o wa_id (tolerante ao
    9º dígito — a Meta manda ora com, ora sem)."""
    digits = _so_digitos(tel)
    if not digits:
        return None
    candidatos = {digits}
    if len(digits) == 12 and digits.startswith("55"):
        candidatos.add(digits[:4] + "9" + digits[4:])
    elif len(digits) == 13 and digits.startswith("55") and digits[4] == "9":
        candidatos.add(digits[:4] + digits[5:])
    try:
        with SessionLocal() as s:
            return s.execute(
                select(Corretora)
                .where(Corretora.telefone_normalizado.in_(candidatos))
                .where(Corretora.ativo.is_(True))
            ).scalars().first()
    except Exception as e:  # noqa: BLE001 — best-effort, não derruba o webhook
        log.warning("cloud_assistente_lookup_corretora_falhou", erro=str(e)[:160])
        return None


def _processar_briefing_botao(corretora_id: int, destino: str) -> None:
    """Clique em "Saiba mais" → o que estiver esperando, pela janela aberta.

    RECADO PENDENTE TEM PRIORIDADE. Se a Dona mandou um recado pontual ("ficou
    gente sem resposta hoje"), é ELE que a corretora precisa ler agora — o
    briefing do dia ela já recebe no ritual de segunda e sexta. Mandar os dois
    junto enterra o recado no meio de um bloco longo.

    Sem recado, o comportamento é o de sempre: briefing completo.
    Roda em BackgroundTasks (coleta é lenta: DB + Calendar + Sheets)."""
    from app.services import briefing_push, recado_corretora  # import tardio

    try:
        if recado_corretora.entregar(corretora_id, destino) is not None:
            return
    except Exception as e:  # noqa: BLE001 — recado nunca derruba o briefing
        log.warning("recado_falhou_cai_no_briefing", corretora_id=corretora_id,
                    erro=str(e)[:160])

    briefing_push.entregar_briefing_completo(corretora_id, destino)


def _processar_optout_botao(corretora_id: int, destino: str) -> None:
    """Clique em "Parar mensagens" → marca opt-out do push + confirma."""
    from app.services import briefing_push  # import tardio (evita ciclo)

    briefing_push.registrar_optout(corretora_id)
    enviar_cloud_assistente(destino, briefing_push.OPTOUT_CONFIRMACAO)


def _tratar_botao_corretora(
    m: dict[str, Any], background: BackgroundTasks | None
) -> bool:
    """Decide o que fazer com um clique de botão (type='button') na linha Assistente.

    Retorna True se o clique foi TRATADO aqui (corretora + botão conhecido) —
    nesse caso NÃO repassa pra bridge. False → segue o fluxo normal (bridge).
    A decisão (lookup + texto do botão) é síncrona e barata; o trabalho pesado
    (gerar/enviar briefing) vai pra BackgroundTasks.
    """
    frm = m.get("from") or ""
    texto = ((m.get("button") or {}).get("text") or "").strip()
    if not frm or not texto:
        return False
    low = texto.lower()
    eh_optout = "parar" in low or "bloquear" in low
    eh_saiba = not eh_optout and "saiba" in low
    if not (eh_optout or eh_saiba):
        return False
    corretora = _corretora_por_telefone(frm)
    if corretora is None:
        return False  # não é corretora → a bridge decide (fluxo atual)
    task = _processar_optout_botao if eh_optout else _processar_briefing_botao
    if background is not None:
        background.add_task(task, corretora.id, frm)
    else:  # chamada direta (testes chamam o handler sem BackgroundTasks)
        task(corretora.id, frm)
    log.info("cloud_assistente_botao_corretora", corretora_id=corretora.id,
             botao=texto[:40], optout=eh_optout)
    return True


# ─── endpoints ───────────────────────────────────────────────────────────────
@router.post("/inbound")
async def assistente_inbound(
    req: Request, background_tasks: BackgroundTasks = None  # type: ignore[assignment]
) -> dict[str, Any]:
    """Mensagens que CHEGARAM pra Assistente (via api-disparos). Grava no thread E
    repassa pra bridge (que decide e responde). Dedup por wamid.

    Exceção: clique de botão do template de briefing vindo de uma CORRETORA é
    tratado AQUI (briefing completo / opt-out) e NÃO vai pra bridge.

    Mensagem marcada `X-Cloud-Velho: 1` (reentrega atrasada da Meta) só é
    gravada: nem o botão nem a bridge respondem pergunta de horas atrás."""
    velho = corretor.mensagem_velha(req)
    payload = await req.json()
    msgs = payload.get("messages") or []
    contatos = {c.get("wa_id"): (c.get("profile") or {}).get("name")
                for c in (payload.get("contacts") or [])}
    gravadas = 0
    repassadas = 0
    botoes_tratados = 0
    for m in msgs:
        frm = m.get("from")
        if not frm:
            continue
        tipo = m.get("type", "text")
        if tipo == "text":
            texto = (m.get("text") or {}).get("body", "")
        elif tipo == "button":
            texto = (m.get("button") or {}).get("text") or "[button]"
        else:
            texto = corretor.texto_de_midia(m, tipo)
        ts = None
        if m.get("timestamp"):
            try:
                ts = datetime.fromtimestamp(int(m["timestamp"]), tz=timezone.utc)
            except (ValueError, TypeError, OSError):
                ts = None
        meta = None
        info = corretor.extrair_media_info(m)
        if info:
            meta = {"cloud_media_id": info[0], "mime_type": info[1]}
        novo = gravar(
            wamid=m.get("id") or str(uuid.uuid4()), contato_telefone=frm,
            contato_nome=contatos.get(frm), from_corretora=False,
            texto=texto, tipo=tipo, ts=ts, raw=m, metadata=meta,
        )
        if novo:
            gravadas += 1
        if velho:
            log.warning("cloud_assistente_inbound_velho_so_gravado", tel=frm[-4:])
            continue
        # Botão de corretora (briefing/opt-out) é tratado aqui — NÃO vai pra
        # bridge. Só pra mensagem NOVA (dedup por wamid vale pro botão também).
        if novo and tipo == "button" and _tratar_botao_corretora(m, background_tasks):
            botoes_tratados += 1
            continue
        # Repassa pra bridge só o que é NOVO (evita reprocessar duplicata).
        if novo and repassar_para_bridge(m, contatos.get(frm)):
            repassadas += 1
    log.info("cloud_assistente_inbound", gravadas=gravadas, repassadas=repassadas,
             botoes_tratados=botoes_tratados)
    return {"ok": True, "gravadas": gravadas, "repassadas": repassadas,
            "botoes_tratados": botoes_tratados}


@router.post("/echo")
async def assistente_echo(req: Request) -> dict[str, Any]:
    """Ecos: mensagens que a Assistente/Assistente mandou PELO CELULAR (coexistência).
    Grava como outbound (from_corretora=True). NÃO repassa pra bridge — eco não
    é pergunta, não deve disparar resposta automática."""
    payload = await req.json()
    echoes = payload.get("echoes") or []
    # Roteamento por linha: echo do 2591 (Pessoa12) grava como a corretora Pessoa3
    # (instance acme_corretora_a); qualquer outro (2955) grava como a Assistente (cloud_assistente).
    phone_number_id = str(payload.get("phone_number_id") or "")
    _grava = corretor.gravar if phone_number_id == PHONE_ID_CORRETOR else gravar
    gravados = 0
    dups = 0
    sem_destino = 0
    for echo in echoes:
        if not isinstance(echo, dict):
            continue
        destino = corretor._echo_destino(echo)
        if not destino:
            sem_destino += 1
            continue
        tipo, texto = corretor._echo_texto_tipo(echo)
        wamid = echo.get("id") or str(uuid.uuid4())
        ts = None
        if echo.get("timestamp"):
            try:
                ts = datetime.fromtimestamp(int(echo["timestamp"]), tz=timezone.utc)
            except (ValueError, TypeError, OSError):
                ts = None
        meta = None
        info = corretor.extrair_media_info(echo)
        if info:
            meta = {"cloud_media_id": info[0], "mime_type": info[1]}
        if _grava(wamid=wamid, contato_telefone=destino, contato_nome=None,
                  from_corretora=True, texto=texto, tipo=tipo, ts=ts,
                  raw=echo, metadata=meta):
            gravados += 1
        else:
            dups += 1
    log.info("cloud_assistente_echo", gravados=gravados, dups=dups, sem_destino=sem_destino)
    return {"ok": True, "gravados": gravados, "dups": dups, "sem_destino": sem_destino}


@router.post("/status")
async def assistente_status(req: Request) -> dict[str, Any]:
    """Statuses de entrega (sent/delivered/read/failed) da linha Assistente."""
    payload = await req.json()
    statuses = payload.get("statuses") or []
    aplicados = 0
    nao_encontrados = 0
    for st in statuses:
        wamid = st.get("id")
        status = st.get("status")
        if not wamid or not status:
            continue
        if aplicar_status(wamid=wamid, status=status,
                          timestamp=st.get("timestamp"), errors=st.get("errors")):
            aplicados += 1
        else:
            nao_encontrados += 1
    log.info("cloud_assistente_status", aplicados=aplicados, nao_encontrados=nao_encontrados)
    return {"ok": True, "aplicados": aplicados, "nao_encontrados": nao_encontrados}


@router.post("/enviar")
async def assistente_enviar(
    req: Request,
    x_bridge_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    """A bridge chama aqui pra ENVIAR a resposta pela Cloud API da Assistente.

    Auth por secret simples (header X-Bridge-Secret == settings.assistente_enviar_secret).
    Se o secret não estiver configurado, o endpoint fica FECHADO (fail-closed) —
    evita que qualquer um dispare envio.
    """
    secret = settings.assistente_enviar_secret
    if not secret or x_bridge_secret != secret:
        log.warning("cloud_assistente_enviar_auth_falha")
        raise HTTPException(401, "unauthorized")
    payload = await req.json()
    to = (payload.get("to") or "").strip()
    texto = (payload.get("texto") or payload.get("text") or "").strip()
    if not to or not texto:
        return {"ok": False, "error": "to_e_texto_obrigatorios", "sent": False}
    return enviar_cloud_assistente(to, texto)


class BriefingAvisoIn(BaseModel):
    corretora_id: int = Field(...)
    edicao: str = Field(..., pattern="^(seg|sex)$")


@router.post("/briefing/aviso")
def assistente_briefing_aviso(
    payload: BriefingAvisoIn,
    x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
) -> dict[str, Any]:
    """Envia o TEMPLATE de aviso de briefing (seg/sex) pra UMA corretora pela
    linha da Assistente. Chamado pelo script dos timers briefing-corretoras-{seg,sex}
    (loop + anti-ban ficam no script, igual briefing_8h.sh).

    Auth: X-Internal-Token == settings.copiloto_internal_token (fail-closed).
    Respeita opt-out (briefing_optout) e corretora inativa/sem telefone → 404.
    Endpoint `def` (sync) → FastAPI roda no threadpool (não trava o loop).
    """
    expected = settings.copiloto_internal_token
    if not expected or x_internal_token != expected:
        raise HTTPException(401, "unauthorized")
    with SessionLocal() as s:
        corretora = s.execute(
            select(Corretora)
            .where(Corretora.id == payload.corretora_id)
            .where(Corretora.ativo.is_(True))
        ).scalar_one_or_none()
        if corretora is None or not corretora.telefone_normalizado:
            raise HTTPException(404, "corretora_not_found_or_sem_telefone")
        if corretora.briefing_optout:
            log.info("briefing_aviso_pulado_optout", corretora_id=corretora.id)
            return {"ok": True, "skipped": "optout", "sent": False}
        nome, telefone = corretora.nome, corretora.telefone_normalizado
    from app.services import briefing_push  # import tardio (evita ciclo)

    return briefing_push.enviar_aviso_briefing(
        corretora_id=payload.corretora_id, nome=nome,
        telefone=telefone, edicao=payload.edicao,
    )
