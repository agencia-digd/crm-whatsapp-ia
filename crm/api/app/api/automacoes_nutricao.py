"""/painel/automacoes/nutricao/metricas — o que a NUTRIÇÃO colhe, por marco.

Módulo separado de `app/api/automacoes.py` de propósito: aquele arquivo já
carrega o mapa inteiro + copies + métricas da cadência, e a nutrição não
precisa de nada dele além de duas peças reusadas (o marco de régua e a
amostra mínima) — importar é mais barato que engordar 1.400 linhas.

O QUE A NUTRIÇÃO É (ver `app/services/nutricao.py`): lead que ESGOTOU o
follow-up cai na etapa Base do funil de remarketing e recebe 1 mensagem a cada
30 dias, avançando de etapa a cada disparo (Base → 30 → 60 → 90 → 120 → 150),
até parar de vez no marco final.

DEFINIÇÃO DAS MÉTRICAS (mesma régua da cadência, adaptada ao marco):
  enviados    = eventos tipo='mensagem' com payload.nutricao=true e
                payload.sent=true, agrupados por payload.marco (o motor grava o
                nome da etapa, não índice — nada aqui é posição fixa);
  responderam = respostas que caíram DEPOIS daquele marco e ANTES do próximo
                envio de mensagem da mesma cadência (atribuição last-touch,
                igual à do follow-up). Uma mensagem de FOLLOW-UP no meio fecha a
                janela: crédito de resposta é do último texto que saiu;
  opt_outs    = mesma janela, com payload.opt_out=true. Conta separado e NÃO
                entra na taxa — opt-out é engajamento negativo;
  taxa        = responderam / enviados, em PERCENTUAL (0-100). `None` quando
                enviados = 0: "sem dado" não é 0%.

FILA POR MARCO ("tem gente nessa régua ou ela está vazia?"): contagem de leads
por etapa do funil `remarketing` AGORA — estado, não histórico, e por isso fora
da janela de dias. Best-effort: árvore sem a coluna `funil` (migration 0046 não
aplicada) devolve `disponivel: False` em vez de derrubar a métrica.

Admin-only, como o resto de /painel/automacoes.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import structlog
from fastapi import APIRouter, Header, HTTPException

from app.db.session import SessionLocal

log = structlog.get_logger()

router = APIRouter(prefix="/painel", tags=["painel"])

METRICAS_DIAS_MAX = 365
DEFAULT_TENANT_ID_NUTRICAO = 1


def _fila_por_etapa(session: Any, *, tenant_id: int) -> dict[str, Any]:
    """Quantos leads estão AGORA em cada etapa da NUTRIÇÃO.

    CUIDADO (medido em 05/08): o funil `remarketing` NÃO é só da nutrição — o
    carrossel da Corretor usa as etapas 'Fila' (4952 leads), 'Disparado' e
    'Respondeu' no MESMO funil. Contar o funil inteiro fazia a tela dizer 6.159
    leads em nutrição onde há 913. Só entram as etapas de `STAGES_NUTRICAO`.
    """
    from sqlalchemy import func as sa_func

    from app.db.models import Lead, PipelineStage
    from app.services import nutricao as nut

    vazio: dict[str, Any] = {
        "disponivel": False,
        "total": None,
        "por_etapa": {},
        "sem_agenda_por_etapa": None,
        "sem_agenda_total": None,
    }
    try:
        stages = (
            session.query(PipelineStage)
            .filter(
                PipelineStage.tenant_id == tenant_id,
                PipelineStage.funil == nut.FUNIL_REMARKETING,
            )
            .all()
        )
        por_id = {
            st.id: (st.nome or "").strip()
            for st in stages
            if (st.nome or "").strip() in nut.STAGES_NUTRICAO
        }
        if not por_id:
            # funil existe no código mas ainda não no banco: dizer "0 em todas"
            # seria afirmar que a régua está vazia — e não se sabe.
            return vazio
        linhas = (
            session.query(Lead.stage_id, sa_func.count(Lead.id))
            .filter(Lead.tenant_id == tenant_id, Lead.stage_id.in_(list(por_id)))
            .group_by(Lead.stage_id)
            .all()
        )
    except Exception as e:  # noqa: BLE001 — métrica não cai por schema antigo
        log.warning("nutricao_fila_indisponivel", erro=str(e)[:140])
        try:
            session.rollback()
        except Exception:  # noqa: BLE001
            pass
        return vazio

    por_etapa = {nome: 0 for nome in nut.STAGES_NUTRICAO}
    for stage_id, n in linhas:
        nome = por_id.get(stage_id)
        if nome:
            por_etapa[nome] = por_etapa.get(nome, 0) + int(n or 0)

    sem_agenda = _sem_agenda_por_etapa(session, por_id, tenant_id=tenant_id)
    return {
        "disponivel": True,
        "total": sum(por_etapa.values()),
        "por_etapa": por_etapa,
        # CONGELADOS: leads na régua cujo estado está com `proximo_toque_em`
        # NULL fora do marco final — o tick só processa datas vencidas, então
        # NULL é "nunca mais recebe nada", em silêncio. 548 assim em 05/08.
        "sem_agenda_por_etapa": sem_agenda,
        "sem_agenda_total": (
            sum(sem_agenda.values()) if sem_agenda is not None else None
        ),
    }


def _sem_agenda_por_etapa(
    session: Any, por_id: dict[int, str], *, tenant_id: int
) -> dict[str, int] | None:
    """Quantos leads de cada etapa estão CONGELADOS (sem próxima data).

    O casamento é feito em Python pelos últimos 8 dígitos — a MESMA heurística
    do `nutricao.tick` (`cadencia._suf8`). Em SQL isso exigiria `regexp_replace`
    do Postgres e a função deixaria de rodar no SQLite dos testes.

    A etapa FINAL fica de fora: ali `proximo_toque_em` NULL é o motor dizendo
    "acabou de vez", não um congelamento. Contar aquilo como problema faria a
    tela pedir conserto pra quem já terminou a régua direito.
    """
    from app.db.models import CadenciaEstado, Lead
    from app.services import nutricao as nut
    from app.services.cadencia import _suf8

    try:
        congelados = {
            _suf8(tel)
            for tel, prox in session.query(
                CadenciaEstado.telefone, CadenciaEstado.proximo_toque_em
            ).filter(
                CadenciaEstado.tenant_id == tenant_id,
                CadenciaEstado.status == "remarketing",
                CadenciaEstado.proximo_toque_em.is_(None),
            )
            if _suf8(tel)
        }
        if not congelados:
            return {nome: 0 for nome in nut.STAGES_NUTRICAO}
        out = {nome: 0 for nome in nut.STAGES_NUTRICAO}
        for tel, stage_id in session.query(Lead.telefone, Lead.stage_id).filter(
            Lead.tenant_id == tenant_id, Lead.stage_id.in_(list(por_id))
        ):
            nome = por_id.get(stage_id)
            if not nome or nome == nut.STAGE_FINAL:
                continue
            if _suf8(tel or "") in congelados:
                out[nome] = out.get(nome, 0) + 1
        return out
    except Exception as e:  # noqa: BLE001 — número a mais não derruba a tela
        log.warning("nutricao_sem_agenda_indisponivel", erro=str(e)[:140])
        try:
            session.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None


def calcular_metricas_nutricao(
    session: Any,
    *,
    dias: int = 30,
    agora: datetime | None = None,
    tenant_id: int = DEFAULT_TENANT_ID_NUTRICAO,
) -> dict[str, Any]:
    """Engajamento por MARCO da nutrição na janela de `dias`.

    Função pura sobre a sessão (sem HTTP) pra ser testável direto. A lista de
    marcos vem de `nutricao.STAGES_NUTRICAO` — nenhum nome de etapa é escrito
    aqui, então marco novo no motor aparece sozinho na tela.
    """
    from app.api.automacoes import AMOSTRA_MINIMA, _ultimo_marco
    from app.db.models import CadenciaEvento
    from app.services import cadencia as cad
    from app.services import nutricao as nut

    agora = agora or datetime.now(timezone.utc)
    desde = agora - timedelta(days=dias)

    # Mesmo marco de régua da cadência: recomeçar a medição vale pras duas
    # automações (é o mesmo motor de envio e o mesmo log de eventos).
    marco = _ultimo_marco(session)
    marco_em: datetime | None = getattr(marco, "criado_em", None)
    if marco_em is not None and marco_em.tzinfo is None:
        marco_em = marco_em.replace(tzinfo=timezone.utc)
    if marco_em is not None and marco_em > desde:
        desde = marco_em

    eventos = (
        session.query(CadenciaEvento)
        .filter(
            CadenciaEvento.tenant_id == tenant_id,
            CadenciaEvento.at >= desde,
            CadenciaEvento.at <= agora,
            CadenciaEvento.tipo.in_((cad.TIPO_MENSAGEM, cad.TIPO_RESPOSTA)),
        )
        .order_by(CadenciaEvento.cadencia_id, CadenciaEvento.at, CadenciaEvento.id)
        .all()
    )

    enviados: dict[str, int] = {}
    respostas: dict[str, int] = {}
    opt_outs: dict[str, int] = {}
    por_cadencia: dict[int, list[Any]] = {}

    for ev in eventos:
        por_cadencia.setdefault(ev.cadencia_id, []).append(ev)
        payload = ev.payload or {}
        if (
            ev.tipo == cad.TIPO_MENSAGEM
            and payload.get("nutricao") is True
            and payload.get("sent") is True
        ):
            etapa = str(payload.get("marco") or "")
            if etapa:
                enviados[etapa] = enviados.get(etapa, 0) + 1

    for linha in por_cadencia.values():
        atual: str | None = None
        for ev in linha:
            payload = ev.payload or {}
            if ev.tipo == cad.TIPO_MENSAGEM:
                # Qualquer mensagem fecha a janela anterior — inclusive um toque
                # de follow-up, que não é marco de nutrição.
                if payload.get("nutricao") is True and payload.get("sent") is True:
                    atual = str(payload.get("marco") or "") or None
                else:
                    atual = None
            elif ev.tipo == cad.TIPO_RESPOSTA and atual is not None:
                if payload.get("opt_out") is True:
                    opt_outs[atual] = opt_outs.get(atual, 0) + 1
                else:
                    respostas[atual] = respostas.get(atual, 0) + 1
                atual = None  # nunca credita a mesma resposta duas vezes

    fila = _fila_por_etapa(session, tenant_id=tenant_id)

    marcos: list[dict[str, Any]] = []
    for ordem, etapa in enumerate(nut.STAGES_NUTRICAO):
        copy = nut.COPIES_MARCO.get(etapa)
        item: dict[str, Any] = {
            "etapa": etapa,
            "ordem": ordem,
            "tag": nut.TAG_POR_ETAPA.get(etapa),
            "copy": copy,
            # Base é etapa de ENTRADA: o lead cai lá ao esgotar o follow-up e
            # ainda não recebeu nada. Sem mensagem não há taxa pra medir.
            "tem_taxa": copy is not None,
            "final": etapa == nut.STAGE_FINAL,
            "em_fila": fila["por_etapa"].get(etapa) if fila["disponivel"] else None,
            # dos que estão na etapa, quantos NÃO têm próxima data (congelados).
            "sem_agenda": (
                (fila.get("sem_agenda_por_etapa") or {}).get(etapa)
                if fila["disponivel"] and fila.get("sem_agenda_por_etapa") is not None
                else None
            ),
        }
        if copy is None:
            item.update({
                "enviados": None,
                "responderam": None,
                "opt_outs": None,
                "taxa": None,
                "sem_dado": True,
                "amostra_pequena": False,
            })
        else:
            n = enviados.get(etapa, 0)
            r = respostas.get(etapa, 0)
            item.update({
                "enviados": n,
                "responderam": r,
                "opt_outs": opt_outs.get(etapa, 0),
                "taxa": round(r / n * 100, 1) if n else None,
                "sem_dado": n == 0,
                "amostra_pequena": 0 < n < AMOSTRA_MINIMA,
            })
        marcos.append(item)

    total_enviados = sum(int(m["enviados"] or 0) for m in marcos if m["tem_taxa"])
    total_respostas = sum(int(m["responderam"] or 0) for m in marcos if m["tem_taxa"])
    total_opt_outs = sum(int(m["opt_outs"] or 0) for m in marcos if m["tem_taxa"])

    return {
        "dias": dias,
        "desde": desde.isoformat(),
        "ate": agora.isoformat(),
        "medindo_desde": marco_em.isoformat() if marco_em else None,
        "motivo_do_marco": getattr(marco, "motivo", None),
        "marco_por": getattr(marco, "criado_por", None),
        "amostra_minima": AMOSTRA_MINIMA,
        "intervalo_dias": nut.INTERVALO_DIAS,
        "etapa_entrada": nut.STAGE_BASE,
        "etapa_final": nut.STAGE_FINAL,
        "marcos": marcos,
        "fila": fila,
        # A copy de cada marco vive em `nutricao.COPIES_MARCO` (código) e NÃO
        # tem tabela de override como a cadência (`cadencia_copies`) — a tela
        # mostra somente leitura e diz isso, em vez de oferecer um botão de
        # editar que não gravaria em lugar nenhum.
        "copies_editaveis": False,
        "total": {
            "enviados": total_enviados,
            "responderam": total_respostas,
            "opt_outs": total_opt_outs,
            "taxa": (
                round(total_respostas / total_enviados * 100, 1)
                if total_enviados
                else None
            ),
            "sem_dado": total_enviados == 0,
            "amostra_pequena": 0 < total_enviados < AMOSTRA_MINIMA,
        },
        "definicao": (
            "enviados = mensagens do marco que SAÍRAM de verdade; responderam = "
            "leads que responderam depois dele e antes da próxima mensagem "
            "(qualquer uma, inclusive de follow-up); opt-out conta separado e "
            f"não entra na taxa; abaixo de {AMOSTRA_MINIMA} envios a taxa é "
            "ruído e vem marcada. 'Na fila' é o estado de AGORA (quantos leads "
            "estão parados em cada etapa), não o histórico da janela — por isso "
            "não fecha com 'receberam'."
        ),
    }


@router.get("/automacoes/nutricao/metricas")
def metricas_nutricao(
    dias: int = 30,
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Engajamento por marco da nutrição + fila atual por etapa. Admin-only."""
    from app.api.painel import _identificar_usuario, _require_admin

    user = _identificar_usuario(x_painel_user)
    _require_admin(user)

    if dias < 1 or dias > METRICAS_DIAS_MAX:
        raise HTTPException(
            status_code=422, detail=f"dias fora da faixa (1 a {METRICAS_DIAS_MAX})"
        )

    with SessionLocal() as s:
        dados = calcular_metricas_nutricao(s, dias=dias)
        # Entregue e falha na mesma janela (06/10, pendência 8 do front).
        if isinstance(dados.get("total"), dict):
            from app.services import painel_automacoes as _pa

            dados["total"].update(_pa.entrega_da_janela(s, "nutricao", dados))
    return {"gerado_em": datetime.now(timezone.utc).isoformat(), **dados}


@router.put("/automacoes/nutricao/corretoras/{corretora_id}")
def gravar_nutricao_da_corretora(
    corretora_id: int,
    patch: dict[str, Any],
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Liga/desliga a nutrição dos leads de UMA corretora (a dona do lead; a
    linha de saída é a da Pessoa12 pra todas). Não envia nada; o geral
    (`nutricao_ativa`) desligado continua vencendo."""
    from app.api.automacoes import gravar_interruptor_da_corretora
    from app.api.painel import _identificar_usuario, _require_admin
    from app.db.session import SessionLocal as _SL
    from app.services import automacao_por_corretora as apc
    from app.services import painel_automacoes as pa

    user = _identificar_usuario(x_painel_user)
    _require_admin(user)
    extras = set(patch) - {"ligada"}
    if extras or "ligada" not in patch:
        raise HTTPException(422, "esperado só {\"ligada\": true|false}")
    r = gravar_interruptor_da_corretora(apc.CHAVE_NUTRICAO, corretora_id,
                                        patch["ligada"], user)
    with _SL() as s:
        r["nutricao"] = pa.bloco_nutricao(s)
    return r
