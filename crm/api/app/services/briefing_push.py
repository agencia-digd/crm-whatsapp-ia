"""Push do briefing 2x/semana pras corretoras — pela linha Cloud da ASSISTENTE (2955).

Decisão da Dona (25/07/2026, memória briefing-corretoras-cadencia):
  • SEGUNDA 08h  → template UTILITY curto avisando "seu briefing da semana chegou"
  • SEXTA   08h  → template UTILITY curto avisando "seu fechamento chegou"
  • O template NÃO carrega o briefing — só o aviso + botão Quick Reply
    "Saiba mais". O clique abre a janela de 24h e aí o briefing COMPLETO sai
    de graça (mensagem de serviço, enviar_cloud_assistente).
  • Botão "Parar mensagens" → opt-out do push (corretoras.briefing_optout).
    O opt-out do api-disparos NÃO cobre a linha da Assistente (webhooks.py retorna
    cedo pra ASSISTENTE_PHONE_ID), então ele vive aqui.

Templates (criados via api-disparos POST /templates/bulk — NUNCA POST cru):
esqueleto canônico de 3 variáveis ({{1}}=nome, {{2}}=aviso, {{3}}=verbo do CTA),
2 bases por edição × 2 cópias = pool de 4 com rotação por hash do telefone e
fallback pro próximo se um estiver PENDING/reclassificado (mesmo padrão do
enviar_template_corretor).

Guardas de envio (mesma ordem do enviar_cloud_assistente):
  1. PYTEST_CURRENT_TEST → nunca envia.
  2. CLOUD_DRY_RUN (linha assistente) → loga em vez de enviar.
  3. token ausente → retorno estruturado, sem envio.
"""
from __future__ import annotations

import os
import uuid
from typing import Any

import httpx
import structlog
from sqlalchemy import select, update

from app.db.models import Corretora
from app.db.session import SessionLocal
from app.services import cloud_lines

log = structlog.get_logger()

API_VER = "v21.0"

# ── Pools de templates de aviso (full_names gerados pelo /templates/bulk) ────
# base × quantidade 2 → sufixos _01/_02. Ver scripts/briefing_subir_templates.sh.
#
# Dona 28/07: MATOU cascas "Notificação para…" (seg_b / sex_d). Fica SÓ o
# esqueleto "Olá {nome}, tudo bem?" (seg_a / sex_c). Clique em Saiba mais abre
# a janela de 24h pra mandar o briefing completo de graça.
POOL_SEG: tuple[tuple[str, str], ...] = (
    ("acme_briefing_semana_seg_a_01", "pt_BR"),
    ("acme_briefing_semana_seg_a_02", "pt_BR"),
)
# Bases _a/_b de sexta foram RECATEGORIZADAS pra MARKETING pela Meta na
# aprovação (26/07) e deletadas da WABA — nunca voltar com elas no pool.
# sex_d ("Notificação para") morto 28/07 junto com seg_b.
POOL_SEX: tuple[tuple[str, str], ...] = (
    ("acme_briefing_fechamento_sex_c_01", "pt_BR"),
    ("acme_briefing_fechamento_sex_c_02", "pt_BR"),
)

# ── Conteúdo do aviso (vai nas VARIÁVEIS — o texto fixo do template é genérico).
# {{2}} sem \n (a Meta rejeita \n/\t/4+ espaços dentro de parâmetro).
AVISO: dict[str, dict[str, str]] = {
    "seg": {
        "hook": (
            "seu briefing da semana chegou: leads pra retomar, "
            "visitas agendadas e as prioridades de hoje"
        ),
        "cta": "receber o briefing completo",
    },
    "sex": {
        "hook": (
            "o fechamento da sua semana está pronto: ligações, visitas "
            "e o que ficou pendente pra retomar"
        ),
        "cta": "receber o fechamento completo",
    },
}

OPTOUT_CONFIRMACAO = (
    "Tudo certo — você não vai mais receber os avisos de briefing por aqui. "
    "Se mudar de ideia, é só me mandar \"me manda o briefing\" que eu te "
    "atualizo na hora. 😉"
)


def _so_digitos(tel: str) -> str:
    return "".join(c for c in (tel or "") if c.isdigit())


def pool_da_edicao(edicao: str) -> tuple[tuple[str, str], ...]:
    if edicao == "seg":
        return POOL_SEG
    if edicao == "sex":
        return POOL_SEX
    raise ValueError(f"edicao inválida: {edicao!r} (use 'seg' ou 'sex')")


# ── Envio do TEMPLATE de aviso (fora da janela — é pra isso que ele existe) ──
def enviar_aviso_briefing(
    *, corretora_id: int, nome: str, telefone: str, edicao: str,
    hook: str | None = None, cta: str | None = None,
) -> dict[str, Any]:
    """Envia o template UTILITY de aviso pra UMA corretora pela linha da Assistente.

    Rotação por hash do telefone + fallback pro próximo do pool se a Meta
    recusar (PENDING/reclassificado) — mesmo padrão do enviar_template_corretor.

    `hook`/`cta` sobrescrevem o texto do aviso quando o que espera do outro
    lado NÃO é o briefing do ritual e sim um recado pontual ("tenho um parecer
    pra te dar"). O ESQUELETO do template continua o mesmo — só as variáveis
    mudam, então nada de reaprovação na Meta. Sem eles, o aviso é o de sempre.
    """
    from app.api import cloud_assistente  # import tardio (evita ciclo)
    from app.api.cloud_corretora_a import normalizar_param_template

    pool = pool_da_edicao(edicao)
    to = _so_digitos(telefone)
    primeiro_nome = (nome or "").strip().split()[0] or "corretora"
    hook = normalizar_param_template(hook or AVISO[edicao]["hook"])
    cta = cta or AVISO[edicao]["cta"]

    # GUARDA ANTI-TESTE — nunca envia sob pytest, independente de mock.
    if os.environ.get("PYTEST_CURRENT_TEST"):
        log.info("briefing_aviso_bloqueado_pytest", corretora_id=corretora_id,
                 edicao=edicao, to_last4=to[-4:])
        return {"ok": True, "pytest": True, "sent": False}

    if cloud_lines.dry_run_for("assistente"):
        log.info("briefing_aviso_dry_run", corretora_id=corretora_id,
                 edicao=edicao, to_last4=to[-4:], hook=hook[:80])
        cloud_assistente.gravar(
            wamid=f"dry-{uuid.uuid4()}", contato_telefone=to, contato_nome=nome,
            from_corretora=True, texto=f"[aviso briefing {edicao}: {hook}]",
            tipo="template",
        )
        return {"ok": True, "dry_run": True, "sent": False}

    tok = cloud_lines.token_for("assistente")
    if not tok:
        log.error("briefing_aviso_sem_token", corretora_id=corretora_id)
        return {"ok": False, "error": "no_token", "sent": False}

    url = f"https://graph.facebook.com/{API_VER}/{cloud_assistente.PHONE_ID}/messages"
    params = [
        {"type": "text", "text": primeiro_nome},
        {"type": "text", "text": hook},
        {"type": "text", "text": cta},
    ]
    seed = sum(ord(c) for c in to) if to else 0
    start = seed % len(pool)
    ultimo_erro = ""
    for k in range(len(pool)):
        tmpl, lang = pool[(start + k) % len(pool)]
        body = {
            "messaging_product": "whatsapp", "to": to, "type": "template",
            "template": {
                "name": tmpl, "language": {"code": lang},
                "components": [{"type": "body", "parameters": params}],
            },
        }
        try:
            with httpx.Client(timeout=20) as c:
                resp = c.post(url, headers={"Authorization": f"Bearer {tok}",
                                            "Content-Type": "application/json"},
                              json=body)
        except Exception as e:  # noqa: BLE001 — rede
            ultimo_erro = f"rede: {str(e)[:120]}"
            log.warning("briefing_aviso_erro_rede", template=tmpl, erro=ultimo_erro)
            continue
        if resp.status_code < 400:
            data = resp.json()
            wamid = (data.get("messages") or [{}])[0].get("id") or str(uuid.uuid4())
            cloud_assistente.gravar(
                wamid=wamid, contato_telefone=to, contato_nome=nome,
                from_corretora=True, texto=f"[aviso briefing {edicao}: {hook}]",
                tipo="template", raw=data,
            )
            log.info("briefing_aviso_enviado", corretora_id=corretora_id,
                     edicao=edicao, template=tmpl, wamid=wamid)
            return {"ok": True, "sent": True, "wamid": wamid, "template": tmpl}
        ultimo_erro = f"{resp.status_code}: {resp.text[:160]}"
        log.warning("briefing_aviso_template_pulou", template=tmpl, erro=ultimo_erro)

    log.error("briefing_aviso_falha_total", corretora_id=corretora_id,
              edicao=edicao, erro=ultimo_erro)
    return {"ok": False, "error": "sem_template_aprovado",
            "detail": ultimo_erro, "sent": False}


# ── Entrega do briefing COMPLETO (janela de 24h aberta pelo clique) ──────────
def entregar_briefing_completo(corretora_id: int, destino_phone: str) -> dict[str, Any]:
    """Gera o briefing (mesmo gerador do ritual das 8h) e envia como TEXTO
    LIVRE pela linha da Assistente (grátis — janela aberta pelo clique no botão).

    Reusa coletar_dados + montar_texto de briefing_corretora; o transporte é
    enviar_cloud_assistente (respeita CLOUD_DRY_RUN + guarda pytest), NÃO o Evolution.
    """
    from app.api.cloud_assistente import enviar_cloud_assistente  # import tardio (ciclo)
    from app.delivery.corretora_formatter import montar_texto
    from app.services.briefing_corretora import coletar_dados
    from app.services.conversations import append_message

    try:
        dados = coletar_dados(corretora_id)
    except ValueError as e:
        log.error("briefing_completo_coleta_falhou", corretora_id=corretora_id,
                  erro=str(e)[:160])
        return {"ok": False, "error": str(e), "sent": False}

    texto = montar_texto(dados)
    res = enviar_cloud_assistente(destino_phone, texto)
    if res.get("ok"):
        try:
            append_message(
                phone=destino_phone, sender_kind="assistant", sender_id=None,
                message_role="assistant", text=texto,
            )
        except Exception as e:  # noqa: BLE001 — best-effort
            log.warning("briefing_completo_persist_falhou", erro=str(e)[:160])
    log.info("briefing_completo_entregue", corretora_id=corretora_id,
             ok=res.get("ok"), sent=res.get("sent"),
             dry_run=res.get("dry_run", False))
    return {**res, "message_length": len(texto)}


# ── Opt-out (botão "Parar mensagens") ────────────────────────────────────────
def registrar_optout(corretora_id: int) -> bool:
    """Marca corretoras.briefing_optout=true. Só o push — corretora segue ativa."""
    try:
        with SessionLocal() as s:
            res = s.execute(
                update(Corretora)
                .where(Corretora.id == corretora_id)
                .values(briefing_optout=True)
            )
            s.commit()
            ok = (res.rowcount or 0) > 0
        log.info("briefing_optout_registrado", corretora_id=corretora_id, ok=ok)
        return ok
    except Exception as e:  # noqa: BLE001
        log.error("briefing_optout_falhou", corretora_id=corretora_id,
                  erro=str(e)[:160])
        return False


def corretoras_para_push() -> list[dict[str, Any]]:
    """Corretoras que recebem o push: ativas, com telefone e SEM opt-out."""
    with SessionLocal() as s:
        rows = s.execute(
            select(Corretora.id, Corretora.nome, Corretora.telefone_normalizado)
            .where(
                Corretora.ativo.is_(True),
                Corretora.briefing_optout.is_(False),
                Corretora.telefone_normalizado.is_not(None),
            )
            .order_by(Corretora.id)
        ).all()
    return [{"id": r[0], "nome": r[1], "telefone": r[2]} for r in rows]
