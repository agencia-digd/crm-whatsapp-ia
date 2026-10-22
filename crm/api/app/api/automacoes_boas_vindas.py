"""/painel/automacoes/boas-vindas/metricas — o que a BOAS-VINDAS colhe.

Módulo separado de `app/api/automacoes.py` pelo mesmo motivo do de nutrição:
aquele arquivo já carrega o mapa inteiro + copies + métricas da cadência, e daqui
só se reusa o marco de régua e a amostra mínima.

O QUE A BOAS-VINDAS É (ver `app/services/lead_intake.py`): lead novo cai na
etapa "Novo" → a ROLETA escolhe a corretora dona → UMA mensagem sai pela linha
Cloud dela (template com botão "Saiba mais") → FIM, aguarda a corretora. Se ela
arrastar o lead pro Follow-up, aí começa a outra régua.

DEFINIÇÃO DAS MÉTRICAS:
  enviadas    = `lead_events(tipo='boas_vindas_enviada')` com payload.sent=true —
                só o que SAIU DE VERDADE. Envio em DRY-RUN é contado à parte
                (`simuladas`) e NUNCA entra na taxa: em simulação o lead não
                recebeu nada, então não teve chance de responder;
  responderam = o lead mandou QUALQUER mensagem depois da boas-vindas
                (`mensagens_whatsapp` com from_corretora=false). A janela abre no
                envio e não fecha: a automação é de uma mensagem só, então não
                existe "próximo toque" pra disputar o crédito;
  taxa        = responderam / enviadas, em PERCENTUAL (0-100). `None` quando
                enviadas = 0 — "sem dado" não é 0%.

DISTRIBUIÇÃO POR CORRETORA (a roleta está equilibrada?): a conta é do que
EFETIVAMENTE saiu por linha, e a comparação é com a fatia ideal (100 ÷ número
de corretoras na roleta). Não confundir com performance: a roleta é rodízio, o
desvio aqui é problema de distribuição, não de atendimento.

TESTE A/B (Dona 05/08): a variante que saiu está no
`payload.variante` do evento — é ela que conta, nunca o recálculo do balde (os
pesos podem ter mudado depois). Evento antigo, sem a chave, entra como
`(não registrada)` em vez de ser jogado no controle: chute vira número errado.

Admin-only, como o resto de /painel/automacoes.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog
from fastapi import APIRouter, Header, HTTPException

from app.db.session import SessionLocal

log = structlog.get_logger()

router = APIRouter(prefix="/painel", tags=["painel"])

METRICAS_DIAS_MAX = 365
DEFAULT_TENANT_ID_BV = 1
VARIANTE_NAO_REGISTRADA = "(não registrada)"

_SO_DIGITOS = re.compile(r"\D")


def _sufixo(telefone: str | None) -> str | None:
    """Últimos 8 dígitos — a mesma chave de identidade que o resto do sistema
    usa (as grafias '+55…' e '55…' convivem no banco desde o seed legado)."""
    if not telefone:
        return None
    digitos = _SO_DIGITOS.sub("", str(telefone))
    return digitos[-8:] if len(digitos) >= 8 else None


def _inbounds_por_telefone(
    session: Any, *, desde: datetime, ate: datetime, tenant_id: int
) -> dict[str, list[datetime]]:
    """Quando cada telefone escreveu pra gente na janela (sufixo → instantes).

    Best-effort: árvore sem `mensagens_whatsapp` devolve vazio e a métrica
    mostra "sem dado" na resposta, em vez de derrubar a página inteira.
    """
    from app.db.models import MensagemWhatsapp

    try:
        linhas = (
            session.query(
                MensagemWhatsapp.contato_telefone,
                MensagemWhatsapp.timestamp_evolution,
            )
            .filter(
                MensagemWhatsapp.tenant_id == tenant_id,
                MensagemWhatsapp.from_corretora.is_(False),
                MensagemWhatsapp.timestamp_evolution >= desde,
                MensagemWhatsapp.timestamp_evolution <= ate,
            )
            .all()
        )
    except Exception as e:  # noqa: BLE001 — métrica não cai por schema antigo
        log.warning("boas_vindas_inbound_indisponivel", erro=str(e)[:140])
        try:
            session.rollback()
        except Exception:  # noqa: BLE001
            pass
        return {}

    out: dict[str, list[datetime]] = {}
    for telefone, quando in linhas:
        suf = _sufixo(telefone)
        if not suf or quando is None:
            continue
        if quando.tzinfo is None:
            quando = quando.replace(tzinfo=timezone.utc)
        out.setdefault(suf, []).append(quando)
    return out


def _rotulo_fora_do_teste(vid: str) -> str:
    """Nome legível de uma copy que saiu mas não está no A/B."""
    try:
        from app.services import boas_vindas_continuidade as bvcont

        if vid == bvcont.COPY_ID:
            return "continuidade de ligação (fora do A/B)"
    except ImportError:
        pass
    return f"fora do A/B ({vid})"


def _bloco(enviadas: int, responderam: int, amostra_minima: int) -> dict[str, Any]:
    """Os quatro campos que toda linha da tela precisa, com as travas de sempre."""
    return {
        "enviadas": enviadas,
        "responderam": responderam,
        # sem envio não existe taxa: "sem dado" ≠ 0%.
        "taxa": round(responderam / enviadas * 100, 1) if enviadas else None,
        "sem_dado": enviadas == 0,
        "amostra_pequena": 0 < enviadas < amostra_minima,
    }


def calcular_metricas_boas_vindas(
    session: Any,
    *,
    dias: int = 30,
    agora: datetime | None = None,
    tenant_id: int = DEFAULT_TENANT_ID_BV,
) -> dict[str, Any]:
    """Boas-vindas enviadas, respostas, distribuição da roleta e placar do A/B.

    Função pura sobre a sessão (sem HTTP) pra ser testável direto. Nenhum id de
    corretora nem de variante é fixado aqui: corretora vem do cadastro, variante
    vem de `boas_vindas_copies.VARIANTES`.
    """
    from app.api.automacoes import AMOSTRA_MINIMA, _ultimo_marco
    from app.db.models import Lead, LeadEvent
    from app.services import boas_vindas_copies as bvc
    from app.services import config_operacional
    from app.services import lead_intake as li

    agora = agora or datetime.now(timezone.utc)
    desde = agora - timedelta(days=dias)

    # Mesmo marco de régua das outras automações: recomeçar a medição vale pra
    # todas (é o mesmo motor de envio e a mesma decisão de "conta a partir de").
    marco = _ultimo_marco(session)
    marco_em: datetime | None = getattr(marco, "criado_em", None)
    if marco_em is not None and marco_em.tzinfo is None:
        marco_em = marco_em.replace(tzinfo=timezone.utc)
    if marco_em is not None and marco_em > desde:
        desde = marco_em

    eventos = (
        session.query(LeadEvent)
        .filter(
            LeadEvent.tenant_id == tenant_id,
            LeadEvent.tipo == li.EVENT_BOAS_VINDAS,
            LeadEvent.at >= desde,
            LeadEvent.at <= agora,
        )
        .order_by(LeadEvent.at, LeadEvent.id)
        .all()
    )

    lead_ids = {ev.lead_id for ev in eventos if ev.lead_id is not None}
    donos: dict[int, int | None] = {}
    telefones: dict[int, str | None] = {}
    if lead_ids:
        for lid, owner, tel in (
            session.query(Lead.id, Lead.owner_id, Lead.telefone)
            .filter(Lead.id.in_(list(lead_ids)))
            .all()
        ):
            donos[int(lid)] = int(owner) if owner is not None else None
            telefones[int(lid)] = tel

    inbounds = _inbounds_por_telefone(
        session, desde=desde, ate=agora, tenant_id=tenant_id
    )

    simuladas = 0
    env_total = 0
    resp_total = 0
    por_corretora: dict[int | None, dict[str, int]] = {}
    por_variante: dict[str, dict[str, int]] = {}

    for ev in eventos:
        payload = ev.payload or {}
        if payload.get("sent") is not True:
            # DRY-RUN (ou envio que não confirmou): o lead não recebeu nada.
            simuladas += 1
            continue
        env_total += 1
        cid = payload.get("corretora_id")
        cid = int(cid) if isinstance(cid, int) else donos.get(int(ev.lead_id or 0))
        vid = payload.get("variante") or VARIANTE_NAO_REGISTRADA

        quando = ev.at
        if quando is not None and quando.tzinfo is None:
            quando = quando.replace(tzinfo=timezone.utc)
        suf = _sufixo(telefones.get(int(ev.lead_id or 0)))
        respondeu = bool(
            suf
            and quando is not None
            and any(t > quando for t in inbounds.get(suf, ()))
        )
        if respondeu:
            resp_total += 1

        alvo_c = por_corretora.setdefault(cid, {"enviadas": 0, "responderam": 0})
        alvo_c["enviadas"] += 1
        alvo_c["responderam"] += int(respondeu)

        alvo_v = por_variante.setdefault(str(vid), {"enviadas": 0, "responderam": 0})
        alvo_v["enviadas"] += 1
        alvo_v["responderam"] += int(respondeu)

    # ── distribuição por corretora ──
    roleta = config_operacional.roleta_corretoras()
    nomes: dict[int, str] = {}
    try:
        from app.api.painel import CORRETORA_LABEL
        from app.db.models import Corretora

        for cid, nome in session.query(Corretora.id, Corretora.nome).all():
            nomes[int(cid)] = CORRETORA_LABEL.get(int(cid), str(nome).split(" ")[0])
    except Exception as e:  # noqa: BLE001 — nome bonito é enfeite, número não
        log.warning("boas_vindas_corretoras_indisponivel", erro=str(e)[:140])

    ideal = round(100 / len(roleta), 1) if roleta else None
    corretoras: list[dict[str, Any]] = []
    vistas = [c for c in por_corretora if c is not None]
    for cid in list(dict.fromkeys(list(roleta) + sorted(vistas))):
        dados = por_corretora.get(cid, {"enviadas": 0, "responderam": 0})
        item = {
            "corretora_id": cid,
            "nome": nomes.get(cid, f"#{cid}"),
            "na_roleta": cid in roleta,
            "posicao_roleta": (roleta.index(cid) + 1) if cid in roleta else None,
            # fatia do que saiu. `None` sem envio nenhum — 0% aqui diria que a
            # roleta pulou essa corretora, e o que houve foi zero lead novo.
            "participacao": (
                round(dados["enviadas"] / env_total * 100, 1) if env_total else None
            ),
            **_bloco(dados["enviadas"], dados["responderam"], AMOSTRA_MINIMA),
        }
        corretoras.append(item)

    sem_dono = por_corretora.get(None)
    if sem_dono:
        corretoras.append({
            "corretora_id": None,
            "nome": "sem dona",
            "na_roleta": False,
            "posicao_roleta": None,
            "participacao": (
                round(sem_dono["enviadas"] / env_total * 100, 1) if env_total else None
            ),
            **_bloco(sem_dono["enviadas"], sem_dono["responderam"], AMOSTRA_MINIMA),
        })

    # ── placar do A/B ──
    variantes: list[dict[str, Any]] = []
    conhecidas = bvc.todas()
    for v in conhecidas:
        dados = por_variante.get(v.id, {"enviadas": 0, "responderam": 0})
        variantes.append({
            "id": v.id,
            "rotulo": v.rotulo,
            "peso": v.peso,
            "ativa": v.ativa,
            "estado": bvc.estado_da_variante(v),
            "aguardando": v.aguardando,
            "controle": v.id == bvc.CONTROLE_ID,
            **_bloco(dados["enviadas"], dados["responderam"], AMOSTRA_MINIMA),
        })
    # Copy que SAIU mas não é do A/B — hoje a de continuidade de ligação
    # (`boas_vindas_continuidade`), que fica fora do sorteio de propósito. Ela
    # PRECISA aparecer: sem esta linha o placar não fecha com o total lá em
    # cima, e tabela que não bate é a primeira coisa que faz alguém parar de
    # confiar no número. Derivado do que veio no evento — id novo aparece
    # sozinho, sem passar por aqui de novo.
    conhecidos = {v.id for v in conhecidas}
    for vid, dados in sorted(por_variante.items()):
        if vid in conhecidos or vid == VARIANTE_NAO_REGISTRADA:
            continue
        variantes.append({
            "id": vid,
            "rotulo": _rotulo_fora_do_teste(vid),
            "peso": 0,
            "ativa": False,
            "estado": "fora do A/B",
            "aguardando": False,
            "controle": False,
            **_bloco(dados["enviadas"], dados["responderam"], AMOSTRA_MINIMA),
        })

    # Envio de antes do A/B (evento sem `variante`): aparece à parte, com o
    # nome do que é. Somar no controle inflaria a variante A com histórico que
    # não passou pelo teste.
    resto = por_variante.get(VARIANTE_NAO_REGISTRADA)
    if resto:
        variantes.append({
            "id": VARIANTE_NAO_REGISTRADA,
            "rotulo": "antes do teste A/B",
            "peso": 0,
            "ativa": False,
            "estado": "histórico",
            "aguardando": False,
            "controle": False,
            **_bloco(resto["enviadas"], resto["responderam"], AMOSTRA_MINIMA),
        })

    return {
        "dias": dias,
        "desde": desde.isoformat(),
        "ate": agora.isoformat(),
        "medindo_desde": marco_em.isoformat() if marco_em else None,
        "motivo_do_marco": getattr(marco, "motivo", None),
        "marco_por": getattr(marco, "criado_por", None),
        "amostra_minima": AMOSTRA_MINIMA,
        "total": {
            "simuladas": simuladas,
            **_bloco(env_total, resp_total, AMOSTRA_MINIMA),
        },
        "por_corretora": corretoras,
        "variantes": variantes,
        "roleta": {
            "corretoras": len(roleta),
            "participacao_ideal": ideal,
            "sem_dado": env_total == 0,
        },
        "definicao": (
            "enviadas = boas-vindas que SAÍRAM de verdade (dry-run conta à "
            "parte e não entra na taxa); responderam = o lead escreveu qualquer "
            "coisa depois de receber; taxa = responderam ÷ enviadas, e abaixo "
            f"de {AMOSTRA_MINIMA} envios vem marcada como amostra pequena. "
            "'Participação' é a fatia de leads novos que cada linha recebeu — "
            "serve pra ver se a roleta está equilibrada, não pra comparar "
            "desempenho. A variante do A/B é a que foi GRAVADA no envio; "
            "envio anterior ao teste aparece à parte, nunca somado no controle. "
            "COMO LER O PLACAR: a variante A (controle) difere das outras em "
            "DUAS coisas, não uma — o enquadramento (confirma o cadastro × faz "
            "uma triagem) e o fecho (A termina em afirmação, as outras em "
            "pergunta). Então o que está sendo medido é 'pergunta que qualifica "
            "× aviso', não enquadramento puro: as duas variáveis andaram juntas, "
            "e o ganho não pode ser atribuído só a uma delas. "
            "A copy de CONTINUIDADE (quem acabou de falar por telefone com a "
            "corretora) entra no total mas fica FORA do A/B, em linha própria: "
            "o contexto dela é outro e as respostas contaminariam o placar."
        ),
    }


@router.get("/automacoes/boas-vindas/metricas")
def metricas_boas_vindas(
    dias: int = 30,
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Boas-vindas: envios, respostas, distribuição da roleta e A/B. Admin-only."""
    from app.api.painel import _identificar_usuario, _require_admin

    user = _identificar_usuario(x_painel_user)
    _require_admin(user)

    if dias < 1 or dias > METRICAS_DIAS_MAX:
        raise HTTPException(
            status_code=422, detail=f"dias fora da faixa (1 a {METRICAS_DIAS_MAX})"
        )

    with SessionLocal() as s:
        dados = calcular_metricas_boas_vindas(s, dias=dias)
        # Entregue e falha na mesma janela (06/10, pendência 8 do front).
        if isinstance(dados.get("total"), dict):
            from app.services import painel_automacoes as _pa

            dados["total"].update(_pa.entrega_da_janela(s, "boas-vindas", dados))
    return {"gerado_em": datetime.now(timezone.utc).isoformat(), **dados}


# ── Roleta do Meta pela tela (06/10/2026) ────────────────────────────────────
#
# A Dona: "a automação boas-vindas, quando clica lá, precisa também saber a
# roleta". A roleta (`roleta_corretoras`) já era configuração; faltava a tela
# ler e mexer nela com as travas de produto:
#   - só corretora ATIVA do cadastro, sem repetir, na ordem da rotação;
#   - vazia é permitida (= ninguém recebe; o lead nasce sem dona e aparece
#     pro admin), mas só com `confirmar_vazia: true` — o servidor confirma,
#     não só o botão da tela;
#   - grava com autor no histórico e devolve o que ficou no banco, RELIDO;
#   - o intake relê a roleta do banco a cada lead (`roleta_configurada`);
#   - card com RMKT continua fora desta roleta (`_dono_pela_roleta`).


@router.get("/automacoes/boas-vindas/roleta")
def ler_roleta_boas_vindas(
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    from app.api.painel import _identificar_usuario, _require_admin
    from app.services import painel_automacoes as pa

    user = _identificar_usuario(x_painel_user)
    _require_admin(user)
    with SessionLocal() as s:
        return {"gerado_em": datetime.now(timezone.utc).isoformat(),
                **pa.roleta(s)}


@router.put("/automacoes/boas-vindas/roleta")
def gravar_roleta_boas_vindas(
    patch: dict[str, Any],
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Grava quem está na roleta do Meta e em que ordem. Não envia nada e não
    redistribui lead que já tem dona: vale a partir do próximo lead novo."""
    from app.api.painel import _identificar_usuario, _require_admin
    from app.services import automacao_por_corretora as apc
    from app.services import config_operacional
    from app.services import painel_automacoes as pa

    user = _identificar_usuario(x_painel_user)
    _require_admin(user)
    extras = set(patch) - {"corretoras", "confirmar_vazia"}
    if extras:
        raise HTTPException(422, f"campo desconhecido: {sorted(extras)}")
    if "corretoras" not in patch:
        raise HTTPException(422, "faltou a lista de corretoras")
    confirmar_vazia = patch.get("confirmar_vazia", False)
    if not isinstance(confirmar_vazia, bool):
        raise HTTPException(422, "confirmar_vazia precisa ser true ou false")
    with SessionLocal() as s:
        try:
            ids = pa.validar_roleta(patch["corretoras"], s)
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        antes = pa.roleta(s)["gravada"]
    if not ids and not confirmar_vazia:
        raise HTTPException(409, (
            "roleta vazia: nenhum lead novo do Meta terá dona e ninguém recebe a "
            "boas-vindas. Pra gravar assim mesmo, mande confirmar_vazia=true."))
    config_operacional.definir("roleta_corretoras", ids, autor=user)
    gravada = apc.ler_fresco("roleta_corretoras")
    with SessionLocal() as s:
        roleta = pa.roleta(s)
    log.info("roleta_meta_gravada", autor=user, antes=antes, depois=ids,
             confirmado=gravada == ids)
    return {
        "ok": gravada == ids,
        "pedido": ids,
        # O que o banco tem AGORA (relido). Diferente do pedido = não confie.
        "gravado": gravada,
        "antes": antes,
        "roleta": roleta,
        "ultima_mudanca": (roleta.get("historico") or [None])[0],
    }
