"""/painel/automacoes — mapa VIVO das automações (lei Dona 10/07) + edição de copies.

Visão "editor de salesbot" da NOSSA máquina: gatilhos, ações, copies e estado
real (ligado/desligado/dry-run) — transparência total pra Dona confiar no que
roda. NADA aqui é hardcode de front: tudo é lido do código/config que executa
de verdade (`app.services.cadencia`, `app.services.nutricao`, `settings`,
unit do timer systemd, contagem em `cadencia_estado`).

EDIÇÃO (só de TEXTO — nunca de comportamento):
  - GET    /painel/automacoes/copies          → toques de mensagem com copy
    efetiva + default + flag "personalizada";
  - PUT    /painel/automacoes/copies/{toque}  → salva override em
    `cadencia_copies` (toque -1 = remarketing);
  - DELETE /painel/automacoes/copies/{toque}  → restaura o padrão (apaga override).

Gatilhos/flags seguem SOMENTE leitura: são env/kill-switches de comportamento
de envio (cap, dry-run, auto-enroll...) — mudança neles é decisão da Dona via
.env, não da tela. Admin-only (mesma classe de /painel/cadencia).
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog
from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import func as sa_func
from sqlalchemy import text as sa_text

from app.config import settings
from app.services import config_operacional
from app.services.opt_out import EXEMPLOS_HUMANOS as EXEMPLOS_OPT_OUT
from app.db.session import SessionLocal

log = structlog.get_logger()

router = APIRouter(prefix="/painel", tags=["painel"])

# Tenant das leituras deste módulo. Fica aqui em cima porque o mapa (topo do
# arquivo) e as métricas (fim) usam o MESMO recorte — dois defaults diferentes
# fariam a tela mostrar estoque de um tenant e placar de outro.
DEFAULT_TENANT_ID_METRICAS = 1

# ── Timer do tick (cadência + nutrição rodam no MESMO tick) ──────────────────
_TIMER_UNIT = "copiloto-cadencia-tick.timer"
_TIMER_PATH = f"/etc/systemd/system/{_TIMER_UNIT}"
_TIMER_ENABLED_LINK = f"/etc/systemd/system/timers.target.wants/{_TIMER_UNIT}"
# 12:15–21:15 UTC = 9:15–18:15 America/Sao_Paulo (rótulo humano; o valor cru
# vem do unit file — se alguém mudar o OnCalendar, o campo `on_calendar` conta).
_TIMER_JANELA_HUMANA = "de hora em hora, 9:15–18:15 (São Paulo)"


def _timer_info() -> dict[str, Any]:
    """Estado vivo do timer: OnCalendar cru do unit + enabled (symlink wants).

    Best-effort: em dev/teste (sem o unit no disco) devolve on_calendar/enabled
    None — o front mostra "desconhecido" em vez de mentir.
    """
    info: dict[str, Any] = {
        "unit": _TIMER_UNIT,
        "on_calendar": None,
        "enabled": None,
        "janela": _TIMER_JANELA_HUMANA,
    }
    try:
        with open(_TIMER_PATH, encoding="utf-8") as f:
            for line in f:
                if line.strip().startswith("OnCalendar="):
                    info["on_calendar"] = line.strip().split("=", 1)[1]
        info["enabled"] = os.path.lexists(_TIMER_ENABLED_LINK)
    except OSError:
        pass
    return info


# ── Blocos do mapa (funções puras — testáveis sem DB) ────────────────────────


def _preview_whatsapp(
    corpo: str,
    *,
    com_botao: bool = False,
    cta: str | None = None,
    nome_lead: str = "{nome_do_lead}",
) -> str:
    """Monta o texto EXATO que o lead vê no WhatsApp (casca do template + corpo).

    com_botao=True → reeng_b (Saiba mais). False → ola SEM botão.
    Follow-up: rodapé neutro (não 'Fico à disposição').
    """
    corpo = (corpo or "").strip()
    if com_botao:
        verbo = (cta or "continuarmos com o atendimento").strip()
        return (
            f"Olá {nome_lead}, tudo bem?\n\n"
            f"{corpo}\n\n"
            f"Para {verbo}, é só tocar no botão abaixo.\n\n"
            "[ Saiba mais ]  [ Parar mensagens ]"
        )
    return (
        f"Olá {nome_lead}, tudo bem?\n\n"
        f"{corpo}\n\n"
        "Qualquer coisa é só me chamar por aqui."
    )


def _variantes_boas_vindas() -> list[dict[str, Any]]:
    """As variantes do A/B da boas-vindas, com peso e texto EFETIVOS.

    Texto com o `{nome}` cru (é o que se edita): quem renderiza por corretora é
    `_corretoras_boas_vindas`. Uma cópia por corretora aqui faria o teste virar
    seis copies soltas na primeira edição.
    """
    from app.services import boas_vindas_copies as bvc

    out: list[dict[str, Any]] = []
    for v in bvc.todas():
        padrao = bvc.padrao_de(v.id) or v.texto
        out.append({
            "id": v.id,
            "rotulo": v.rotulo,
            "peso": v.peso,
            "ativa": v.ativa,
            # "aguardando" ≠ "aposentada": uma ainda não competiu, a outra
            # perdeu. A tela precisa das duas palavras.
            "estado": bvc.estado_da_variante(v),
            "aguardando": v.aguardando,
            "controle": v.id == bvc.CONTROLE_ID,
            "hipotese": v.hipotese,
            "texto": v.texto,
            "padrao": padrao,
            "personalizada": v.texto.strip() != padrao.strip(),
        })
    return out


def _corretoras_boas_vindas(session: Any) -> list[dict[str, Any]]:
    """Corretoras ativas com a boas-vindas de cada uma, variante a variante.

    Lista derivada do BANCO (não de um mapa de ids no código): quem entra na
    tela é quem existe no cadastro. Pra cada uma vai o nome que assina a copy,
    o texto de CADA variante já renderizado, se ela tem linha Cloud (sem linha a
    automação NÃO roda pra ela) e a posição na roleta.
    """
    from app.api.painel import CORRETORA_LABEL
    from app.services import boas_vindas_copies as bvc
    from app.services import cloud_lines
    from app.services import lead_intake as li

    cta = getattr(li, "BOAS_VINDAS_CTA", "continuarmos com o atendimento")
    roleta = config_operacional.roleta_corretoras()
    variantes = bvc.todas()

    linhas = session.execute(
        sa_text(
            "SELECT id, nome FROM corretoras WHERE ativo = TRUE "
            "AND coalesce(papel,'corretora') LIKE 'corretora%' ORDER BY id"
        )
    ).all()

    out: list[dict[str, Any]] = []
    for r in linhas:
        cid = int(r[0])
        nome = str(r[1])
        nome_copy = li.nome_da_linha(cid)
        try:
            linha = cloud_lines.linha_da_corretora(cid, session=session)
            tem_linha = linha is not None and linha is not cloud_lines.INDETERMINADO
        except Exception as e:  # noqa: BLE001 — cadastro ilegível não derruba a tela
            log.warning("boas_vindas_linha_indisponivel", corretora_id=cid,
                        erro=str(e)[:140])
            tem_linha = False
        textos = {v.id: v.render(nome_copy) for v in variantes}
        # Por qual número a boas-vindas dela sai, com a saúde (06/10): a linha
        # da própria corretora. Sem linha, ela recebe o lead e nada sai.
        from app.services import painel_automacoes as pa

        slug = linha.slug if tem_linha else None
        out.append({
            "corretora_id": cid,
            "nome": nome,
            "label": CORRETORA_LABEL.get(cid, nome.split(" ")[0]),
            "nome_na_copy": nome_copy,
            "tem_linha_cloud": tem_linha,
            "sai_por": slug,
            "linha": pa.linha_curta(slug),
            "na_roleta": cid in roleta,
            "posicao_roleta": (roleta.index(cid) + 1) if cid in roleta else None,
            "textos": textos,
            "previews": {
                vid: _preview_whatsapp(t, com_botao=True, cta=cta, nome_lead="João")
                for vid, t in textos.items()
            },
        })
    return out


def _mapa_boas_vindas(session: Any | None = None) -> dict[str, Any]:
    """Automação de BOAS-VINDAS do cadastro (lei Dona 10/07) — SEPARADA da cadência.

    Lead cai na etapa "Novo" → UMA msg pela linha Cloud da dona COM botão
    Saiba mais (reeng_b — SEM "Fico à disposição") → FIM.
    """
    copy: str | None = None
    cta: str | None = None
    copies_por_corretora: dict[str, str] = {}
    previews_por_corretora: dict[str, str] = {}
    por_corretora: list[dict[str, Any]] = []
    variantes: list[dict[str, Any]] = []
    controle_id: str | None = None
    continuidade: dict[str, Any] | None = None
    por_corretora_erro: str | None = None
    disponivel = False
    try:
        from app.services import boas_vindas_copies as bvc
        from app.services import lead_intake as li

        copy = li.BOAS_VINDAS_TEXTO
        cta = getattr(li, "BOAS_VINDAS_CTA", "continuarmos com o atendimento")
        controle_id = bvc.CONTROLE_ID
        variantes = _variantes_boas_vindas()
        if session is not None:
            try:
                por_corretora = _corretoras_boas_vindas(session)
            except Exception as e:  # noqa: BLE001 — mapa não cai por cadastro
                log.warning("boas_vindas_por_corretora_indisponivel",
                            erro=str(e)[:140])
                por_corretora = []
                # lista vazia por FALHA ≠ "nenhuma corretora cadastrada"
                por_corretora_erro = str(e)[:140]
        # Compat das telas antigas (mapa geral e vista Cards): mostram UMA copy
        # por corretora, e a que representa a automação ali é a de CONTROLE.
        for item in por_corretora:
            chave = item["label"].lower()
            copies_por_corretora[chave] = item["textos"].get(controle_id, "")
            previews_por_corretora[chave] = item["previews"].get(controle_id, "")
        # SAÍDA QUE NÃO ENTRA NO A/B: quem foi adotado por LIGAÇÃO recebe a copy
        # de continuidade. O desenho não mostrava isso, e quem olhasse a tela
        # concluiria que todo envio de boas-vindas participa do teste.
        from app.services import boas_vindas_continuidade as bvcont

        continuidade = {
            "id": bvcont.COPY_ID,
            "texto": bvcont.TEXTO,
            "preview": _preview_whatsapp(
                bvcont.render("Pessoa5"), com_botao=True, cta=cta
            ),
            "quando": (
                "lead que a corretora ADOTOU na janela de ligação "
                "(tag leads.adotado_ligacao_em)"
            ),
            "no_ab": False,
            "por_que": (
                "a pessoa acabou de falar por TELEFONE — 'você acabou de se "
                "cadastrar, certo?' seria constrangedor; e as respostas dela "
                "contaminariam o placar das três variantes"
            ),
        }
        disponivel = True
    except ImportError:
        pass
    # ROLETA do Meta e o que saiu/falhou por corretora (06/10). Cada um com o
    # seu erro: a roleta ilegível não apaga os envios, e vice-versa.
    roleta: dict[str, Any] | None = None
    roleta_erro: str | None = None
    envios: dict[str, Any] | None = None
    envios_erro: str | None = None
    if session is not None:
        from app.services import painel_automacoes as pa

        try:
            roleta = pa.roleta(session)
        except Exception as e:  # noqa: BLE001
            roleta_erro = str(e)[:160]
            log.warning("boas_vindas_roleta_indisponivel", erro=roleta_erro)
            session.rollback()
        try:
            envios = pa.bloco_boas_vindas(session)
        except Exception as e:  # noqa: BLE001
            envios_erro = str(e)[:160]
            session.rollback()

    # Estado EFETIVO (painel > .env): é a MESMA chave que o motor lê no intake.
    flag_ligada = config_operacional.lead_intake_ativo()
    ligada = disponivel and flag_ligada
    preview = (
        _preview_whatsapp(copy, com_botao=True, cta=cta) if copy else None
    )

    return {
        "descricao": (
            "Lead novo cai em 'Novo' → UMA mensagem de boas-vindas pela linha "
            "Cloud da dona (Pessoa10/Pessoa11/Pessoa), COM botão Saiba mais. "
            "SEM 'Fico à disposição'. Depois AGUARDA a corretora. Se ela "
            "arrastar pro Follow-up, entra a cadência (D0 = reencontro)."
        ),
        "gatilho": (
            "lead cai na etapa 'Novo' (webhook Meta → POST /intake/lead) — "
            "vale pro cadastro NOVO e pro lead JÁ EXISTENTE que está em 'Novo' "
            "e ainda não recebeu"
        ),
        # Os três caminhos que o motor executa de verdade. O desenho mostrava
        # só o primeiro — e é justamente o terceiro que fica FORA do A/B.
        "caminhos": [
            {
                "id": "cadastro_novo",
                "titulo": "Cadastro novo",
                "detalhe": "lead entra pelo intake e cai em 'Novo'",
                "copy": "uma das variantes do teste A/B (balde pelo id do lead)",
                "no_ab": True,
            },
            {
                "id": "lead_existente",
                "titulo": "Lead que já existia",
                "detalhe": (
                    "o intake reencontra o lead (mesmo telefone): se ele está "
                    "em 'Novo' e não tem o evento de idempotência, TAMBÉM "
                    "recebe. Quem já avançou de etapa não recebe."
                ),
                "copy": "uma das variantes do teste A/B",
                "no_ab": True,
            },
            {
                "id": "continuidade_ligacao",
                "titulo": "Veio de ligação",
                "detalhe": (
                    "adotado na janela de ligação (tag adotado_ligacao_em): "
                    "sai a copy de CONTINUIDADE, FORA do teste A/B"
                ),
                "copy": continuidade["texto"] if continuidade else None,
                "no_ab": False,
            },
        ],
        "continuidade": continuidade,
        "acao": "envia 1x (idempotente) COM botão Saiba mais pela linha da dona e aguarda",
        "copy": copy,
        "cta": cta,
        "com_botao": True,
        "botoes": ["Saiba mais", "Parar mensagens"],
        "template": "reeng_b (Olá {nome}, tudo bem? · COM botão · SEM Fico à disposição)",
        "preview": preview,
        "copies_por_corretora": copies_por_corretora,
        "previews_por_corretora": previews_por_corretora,
        # Teste A/B da primeira mensagem (Dona 05/08): as variantes com peso e
        # texto efetivos, e a copy de cada corretora em CADA variante.
        "variantes": variantes,
        "controle": controle_id,
        "por_corretora": por_corretora,
        "por_corretora_indisponivel": por_corretora_erro is not None,
        "por_corretora_motivo": por_corretora_erro,
        "estado": "ligado" if ligada else "desligado",
        "flag": "LEAD_INTAKE_ATIVO",
        # A chave que o motor lê de verdade (painel > .env) — é ela que o
        # interruptor da página grava.
        "chave_config": "lead_intake_ativo",
        "detalhe": (
            "ligada — 1 msg automática no cadastro (COM Saiba mais); nada mais até a corretora agir"
            if ligada
            else (
                "desligada — kill-switch LEAD_INTAKE_ATIVO"
                if disponivel
                else "módulo de intake ainda não deployado nesta árvore"
            )
        ),
        "fim": "aguarda a corretora dona",
        "disponivel": disponivel,
        # Roleta do Meta editável pela tela (`PUT …/boas-vindas/roleta`): quem
        # está, a ordem, de quem é a vez, quantos cada uma recebeu.
        "roleta": roleta,
        "roleta_indisponivel": roleta_erro is not None,
        "roleta_motivo": roleta_erro,
        # Saiu / entregue / falhou por corretora, hoje e na semana.
        "envios": envios,
        "envios_indisponivel": envios_erro is not None,
        "envios_motivo": envios_erro,
    }


def _mapa_cadencia(session: Any | None = None) -> dict[str, Any]:
    """Cronograma real dos 8 toques + copies + preview do que o lead VÊ.

    Copy efetiva (override de `cadencia_copies` quando existe). Preview = casca
    do template WABA + corpo — exatamente como sai no WhatsApp.
    """
    from app.services import cadencia as cad

    # Dona 28/07: follow-up SEM botão (vale pro remarketing também). Preview usa
    # rodapé neutro (não "Fico à disposição" — templates *_ola_fu_* em análise
    # na Meta). A exceção é o toque da oferta de áudio, que decide por si
    # (Toque.com_botao) — ver o laço abaixo.
    com_botao_real = False

    toques: list[dict[str, Any]] = []
    for t in cad.TOQUES:
        item: dict[str, Any] = {
            "indice": t.indice,
            "dia": f"D{t.offset_dias}",
            "offset_dias": t.offset_dias,
            "tipo": t.tipo,            # mensagem | tarefa_ligacao
            "canal": t.canal,          # whatsapp | ligacao
            "categoria": t.categoria,  # utility | marketing
            "titulo": (
                f"Mensagem WhatsApp · D{t.offset_dias}"
                if t.tipo == cad.TIPO_MENSAGEM
                else f"Tarefa de ligação · D{t.offset_dias}"
            ),
        }
        if t.tipo == cad.TIPO_MENSAGEM:
            default = cad._TEMPLATES_MSG.get(t.indice, "")
            efetiva = cad.copy_efetiva(session, t.indice)
            # Quem decide se o toque tem botão é a RÉGUA (Toque.com_botao), não
            # esta função: desde 05/08 a oferta de áudio sai COM botão pra o
            # "sim" ser um clique, e o resto segue sem.
            tem_botao = bool(t.com_botao)
            item["com_botao"] = tem_botao
            item["cta"] = t.cta
            item["copy"] = efetiva
            item["copy_padrao"] = default
            item["personalizada"] = efetiva != default
            item["preview"] = _preview_whatsapp(
                efetiva, com_botao=tem_botao, cta=t.cta
            )
            item["template"] = (
                "reeng_b (COM botão Saiba mais)" if tem_botao else "ola (SEM botão)"
            )
        else:
            item["com_botao"] = False
            item["copy"] = None
            item["preview"] = None
            item["acao"] = "vira tarefa no CRM pra corretora ligar — nenhuma msg WhatsApp"
        toques.append(item)

    rmk_default = cad._TEMPLATE_REMARKETING
    rmk_efetiva = cad.copy_efetiva(session, cad.TOQUE_REMARKETING)
    return {
        "descricao": (
            "8 toques em 10 dias (5 mensagens + 3 tarefas de ligação). Liga quando "
            "a corretora arrasta o lead pro Follow-up — D0 é SEMPRE reencontro "
            "(boas-vindas de cadastro é a automação separada acima). Sai pela "
            "linha Cloud da dona, SEM botão. Toda msg termina com ?"
        ),
        "toques": toques,
        "remarketing": {
            "regra": (
                f"após o D10 o lead vira status 'remarketing': 1 mensagem a cada "
                f"{settings.cadencia_cooldown_dias} dias (quem tem par na espinha "
                f"canônica segue pro funil de nutrição abaixo)"
            ),
            "intervalo_dias": settings.cadencia_cooldown_dias,
            "copy": rmk_efetiva,
            "copy_padrao": rmk_default,
            "personalizada": rmk_efetiva != rmk_default,
            "com_botao": com_botao_real,
            "preview": _preview_whatsapp(rmk_efetiva, com_botao=False),
            "template": "ola (SEM botão)",
        },
    }


def _estoque_carrossel(
    session: Any, tenant_id: int = DEFAULT_TENANT_ID_METRICAS
) -> dict[str, int]:
    """Quantos leads estão parados em cada etapa do funil de remarketing.

    Uma função só porque o mapa e a página de métricas mostram o MESMO número —
    duas queries iguais viram dois números diferentes na primeira mudança de
    etapa.

    MESMO recorte do motor (`carrossel_corretor._SQL_FILA` / `marcar_respondeu`):
    tenant + dono + a etapa que está configurada como origem do disparo
    (`remarketing_alvo`). Sem os dois primeiros, lead de OUTRO cliente (ou de
    outra dona que caísse nesses stages) aparecia como estoque da roda; sem o
    terceiro, a tela diria "1.736 na fila" com o disparo apontado pra outra etapa.

    Alvo inválido LEVANTA: quem chama mostra "não consegui ler", que é melhor
    do que contar a fila de uma etapa que o motor não vai usar.
    """
    from app.services import carrossel_corretor as cb
    from app.services import remarketing_alvo

    alvo = remarketing_alvo.ler(session, tenant_id)
    por_etapa = {
        int(r[0]): int(r[1])
        for r in session.execute(sa_text(
            "SELECT stage_id, COUNT(*) FROM leads "
            "WHERE tenant_id = :t AND owner_id = :dono "
            "  AND stage_id IN (:f, :d, :r) GROUP BY stage_id"
        ), {"t": tenant_id, "dono": alvo.dono, "f": alvo.etapa,
            "d": cb.STAGE_DISPARADO, "r": cb.STAGE_RESPONDEU}).all()
    }
    return {
        "fila": por_etapa.get(alvo.etapa, 0),
        "disparado": por_etapa.get(cb.STAGE_DISPARADO, 0),
        "respondeu": por_etapa.get(cb.STAGE_RESPONDEU, 0),
    }


def _nome_da_etapa(alvo: dict[str, Any] | None) -> str:
    """Nome da etapa configurada como origem do disparo, pro texto da tela.
    Sem leitura não inventa "Fila": diz que não leu."""
    if not alvo or alvo.get("etapa") is None:
        return "não consegui ler qual"
    for e in alvo.get("etapas") or []:
        if e["id"] == alvo["etapa"]:
            return f"{e['nome']}" + (f" ({e['funil']})" if e.get("funil") else "")
    return f"#{alvo['etapa']}"


def _mapa_carrossel_corretor() -> dict[str, Any]:
    """O carrossel da Corretor — a máquina que mais roda e que não aparecia aqui.

    A tela mostrava só a cadência e as boas-vindas. O remarketing inteiro (o
    disparo diário do 0000, a Pessoa atendendo quem responde, a qualificação e o
    handoff pra Pessoa12) era invisível — e é o fluxo com mais volume da operação.
    Pedido da Dona em 04/08/2026: "mostra lá o que a gente montou".

    Tudo lido do código/banco que executa de verdade — nada digitado à mão.
    """
    from app.services import carrossel_corretor as cb
    from app.services import carrossel_copies as cc

    # Peso e TEXTO efetivos (painel > código) + o padrão do código ao lado,
    # pra tela dizer o que foi mexido. `texto_igual_a`: as variantes que estão
    # saindo com o MESMO texto (o teste de categoria) aparecem amarradas.
    variantes, copies = _variantes_do_carrossel()

    # Estoque das etapas + resultado do dia.
    #
    # ZERO ≠ AUSÊNCIA. Aqui os números nasciam em 0 e o `except` largo devolvia
    # esses zeros como se fossem leitura boa: banco fora, migration faltando ou
    # SQL inválido chegavam na Dona como "não tem ninguém na fila". É o mesmo
    # padrão de bug que já custou dois dias (funil zerado, Corretor zerada) — e
    # estava justamente no painel feito pra não fazer isso. Falhou → None +
    # `indisponivel` com o motivo, e a tela diz "não consegui ler".
    fila = disparado = respondeu = disparos_hoje = None
    estoque_erro: str | None = None
    disparos_erro: str | None = None
    entrega: dict[str, Any] | None = None
    entrega_erro: str | None = None
    cap_rampa: int | None = None
    motivo_rampa: str | None = None
    agenda: dict[str, Any] | None = None
    agenda_erro: str | None = None
    alvo: dict[str, Any] | None = None
    alvo_erro: str | None = None
    # Garantia (05/10): o dia de hoje aceito × entregue, as reservas sem
    # disparo, as conferências gravadas e a última volta do robô. Cada um com
    # o seu erro — leitura que falha não vira zero nem apaga as outras.
    hoje_conta: dict[str, Any] | None = None
    orfas: dict[str, Any] | None = None
    conferencias: list[dict[str, Any]] | None = None
    ultima_volta: dict[str, Any] | None = None
    parar: dict[str, Any] | None = None
    garantia_erro: dict[str, str] = {}

    # POR ONDE sai: a linha marcada `disparo` no cadastro de números. Fora do
    # `with` do banco (é rede, local) e ANTES da rampa — a rampa e a saúde são
    # as DESTA linha. Sem linha resolvida não existe "rampa da linha de sempre"
    # pra mostrar: a tela diz que não há disparador, com o motivo.
    from app.services import linha_disparadora, remarketing_alvo

    linha_disp = linha_disparadora.resumo()
    slug_disparo: str | None = linha_disp["slug"]

    try:
        with SessionLocal() as s:
            # ALVO (etapa e dono) + as etapas reais do cliente pro seletor.
            try:
                alvo = remarketing_alvo.resumo(s, DEFAULT_TENANT_ID_METRICAS)
            except Exception as e:  # noqa: BLE001
                alvo_erro = str(e)[:140]
                s.rollback()
            # AGENDA do painel (janela, dias, caps) + quanto da semana já foi.
            # Vem antes da rampa: o limite por dia gravado é o PEDIDO que a
            # rampa precisa conhecer pra responder o teto certo.
            try:
                from app.services import remarketing_agenda

                agenda = remarketing_agenda.resumo(
                    s, DEFAULT_TENANT_ID_METRICAS, cb.EVENTOS_DE_SAIDA
                )
            except Exception as e:  # noqa: BLE001
                agenda_erro = str(e)[:140]
                s.rollback()
            if slug_disparo:
                try:
                    from app.services.rampa import cap_de_hoje

                    r = cap_de_hoje(
                        s, slug_disparo,
                        (alvo or {}).get("dono") or cb.CORRETORA_CORRETOR,
                        pedido=(agenda or {}).get("cap_diario"),
                    )
                    cap_rampa, motivo_rampa = r.cap, r.motivo
                except Exception:  # noqa: BLE001 — sem rampa a tela diz "sem dado"
                    pass
            # Dois try SEPARADOS: uma leitura que falha não pode apagar a outra
            # que deu certo (nem virar zero).
            try:
                estoque = _estoque_carrossel(s)
                fila = estoque["fila"]
                disparado = estoque["disparado"]
                respondeu = estoque["respondeu"]
            except Exception as e:  # noqa: BLE001
                estoque_erro = str(e)[:140]
                fila = disparado = respondeu = None
                s.rollback()
            try:
                # Mesma conta do motor (`carrossel_corretor._enviados_hoje`):
                # converte o LADO ESQUERDO pro fuso de SP e filtra tenant.
                # `at::date` cru comparava UTC com data local — perto da virada
                # do dia o painel e o cap real discordavam — e sem tenant o
                # evento de outro cliente inflava o contador da tela.
                # Conta também a RESERVA que sobrou (mensagem que pode ter
                # saído sem o registro fechar): é o número que o teto compara.
                disparos_hoje = int(s.execute(sa_text(
                    "SELECT COUNT(*) FROM lead_events "
                    "WHERE tenant_id = :t AND tipo IN (:ev, :ev_reserva) "
                    "  AND (at AT TIME ZONE 'America/Sao_Paulo')::date "
                    "    = (now() AT TIME ZONE 'America/Sao_Paulo')::date"
                ), {"t": DEFAULT_TENANT_ID_METRICAS, "ev": cb.EVENT_DISPARO,
                    "ev_reserva": cb.EVENT_RESERVA}).scalar() or 0)
            except Exception as e:  # noqa: BLE001
                disparos_erro = str(e)[:140]
                disparos_hoje = None
                s.rollback()
            # ENTREGA do último lote. Terceiro try, pela mesma razão dos dois
            # de cima — e porque aqui o zero mente mais que em qualquer outro
            # lugar: "0 falhas" por erro de leitura é exatamente o relatório
            # que dizia 100% de sucesso com 40% de falha.
            try:
                from app.services import remarketing_placar

                entrega = remarketing_placar.ultimo_lote(
                    s, tenant_id=DEFAULT_TENANT_ID_METRICAS
                )
            except Exception as e:  # noqa: BLE001
                entrega_erro = str(e)[:140]
                s.rollback()
            from app.services import remarketing_conferente as _conf

            for nome, ler in (
                ("hoje", lambda: _conf.conta_do_dia(
                    s, _conf._hoje_sp(), DEFAULT_TENANT_ID_METRICAS)),
                ("reservas", lambda: _conf.reservas_orfas(
                    s, DEFAULT_TENANT_ID_METRICAS)),
                ("conferencia", lambda: _conf.ultimas(
                    s, DEFAULT_TENANT_ID_METRICAS, 7)),
                ("ultima_volta", lambda: _ultima_volta(s)),
                ("parar", lambda: remarketing_alvo.destino_parar(
                    s, DEFAULT_TENANT_ID_METRICAS)),
            ):
                try:
                    valor = ler()
                    if nome == "hoje":
                        hoje_conta = valor
                    elif nome == "reservas":
                        orfas = valor
                    elif nome == "conferencia":
                        conferencias = valor
                    elif nome == "parar":
                        parar = valor
                    else:
                        ultima_volta = valor
                except Exception as e:  # noqa: BLE001
                    garantia_erro[nome] = str(e)[:140]
                    s.rollback()
    except Exception as e:  # noqa: BLE001 — mapa não pode cair por causa de número
        estoque_erro = disparos_erro = entrega_erro = agenda_erro = str(e)[:140]
        alvo_erro = alvo_erro or str(e)[:140]
        fila = disparado = respondeu = disparos_hoje = None
        agenda = None
    if estoque_erro or disparos_erro or entrega_erro or agenda_erro:
        log.warning(
            "mapa_carrossel_sem_numeros",
            estoque=estoque_erro, disparos_hoje=disparos_erro,
            entrega=entrega_erro, agenda=agenda_erro,
        )

    # Limite que VALE hoje: o menor entre a rampa e o campo "por dia" da tela.
    # É a mesma conta do tick, feita no mesmo lugar (`teto_efetivo`) — a tela
    # mostra o número pedido, o que vale e por que os dois diferem.
    from app.services import remarketing_agenda as _agenda

    cap_painel = (agenda or {}).get("cap_diario")
    cabe: int | None = None
    if cap_rampa is not None and agenda and not agenda.get("ilegivel"):
        # Quanto cabe no horário gravado, com o teto que vale. Janela curta
        # segura mais que a rampa, e isso também tem que aparecer.
        jan_txt = agenda.get("janela_horario")
        cabe = cb.maximo_que_cabe_no_dia(
            cap_rampa if cap_painel is None else min(cap_rampa, cap_painel),
            _agenda.parse_janela(jan_txt) if jan_txt else None,
        )
    teto = _agenda.teto_efetivo(cap_painel, cap_rampa, motivo_rampa,
                                cabe_no_horario=cabe)
    if not slug_disparo:
        # Sem linha disparadora o tick não envia: o limite de hoje é zero, e o
        # motivo é o da linha — não "rampa", que nem chegou a ser consultada.
        por_dia, fonte_do_limite = 0, "falta de linha disparadora"
        teto = {**teto, "efetivo": 0, "diverge": cap_painel not in (None, 0),
                "explicacao": linha_disp["motivo_texto"]
                or "não há linha disparadora"}
    elif teto["efetivo"] is None:
        por_dia = config_operacional.cap_da_corretora(cb.CORRETORA_CORRETOR)
        fonte_do_limite = "configuração"
    else:
        por_dia = teto["efetivo"]
        fonte_do_limite = ("limite por dia do painel"
                           if cap_painel is not None and cap_painel <= cap_rampa
                           else "rampa de aquecimento")

    # Saúde da linha na Meta (cache de 5 min, nunca levanta). Fora do `with`
    # do banco: é rede, e não pode segurar conexão aberta esperando a Graph.
    # É a saúde da linha que DISPARA; sem disparador não há bolinha pra pintar.
    from app.services import saude_linha

    # Saúde de TODOS os números que este sistema opera, não só a do
    # disparador (05/10): é com a linha pausada que a Dona precisa saber se
    # pode voltar. Mesmo cache de 5 minutos por linha.
    for n in linha_disp.get("numeros") or []:
        n["saude"] = _saude_compacta(saude_linha.ler(n["slug"])) if n.get("slug") else None
    # A bolinha do topo: a do disparador; sem disparador, a da linha que
    # PODERIA disparar (a da casa, pausada) — e o campo diz de qual é.
    slug_da_saude = slug_disparo or next(
        (n["slug"] for n in (linha_disp.get("numeros") or []) if n.get("operavel")),
        None,
    )
    saude = saude_linha.ler(slug_da_saude) if slug_da_saude else None
    if saude is not None:
        saude = {**saude, "de_qual": "disparador" if slug_disparo
                 else "linha_da_casa_sem_marca_de_disparo"}

    # Histórico: quem mexeu no painel e no papel dos números.
    historico: dict[str, Any] = {"config": None, "papel": None, "erros": {}}
    try:
        # O texto das variantes tem histórico próprio (`copies.historico`),
        # uma linha por variante; aqui ele viria como um mapa inteiro, ilegível.
        from app.services import automacao_por_corretora as _apc

        historico["config"] = config_operacional.historico(
            sorted(k for k in config_operacional.CHAVES_ESTRITAS
                   if k != "carrossel_copies" and k not in _apc.CHAVES), limite=20)
    except Exception as e:  # noqa: BLE001
        historico["erros"]["config"] = str(e)[:140]
    try:
        historico["papel"] = linha_disparadora.historico_papel(20)
    except Exception as e:  # noqa: BLE001
        historico["erros"]["papel"] = str(e)[:140]

    return {
        "titulo": "Remarketing da base (carrossel da Corretor)",
        "linha": "0000-0000 — o telefone da casa",
        "gatilho": (
            f"lead na etapa '{_nome_da_etapa(alvo)}' "
            + (f"({fila} esperando agora)" if fila is not None
               else "(não consegui ler quantos estão esperando)")
        ),
        "ritmo": {
            # Quem manda no volume é a RAMPA, não o cap de configuração — a
            # linha está em aquecimento. Mostrar o cap configurado aqui faria a
            # tela dizer 25 num dia em que saíram 50.
            "por_dia": por_dia,
            "fonte_do_limite": fonte_do_limite,
            # Teto da rampa sozinha — a tela mostra ao lado do campo "por dia"
            # pra ficar claro até onde o número digitado tem efeito.
            "cap_rampa": cap_rampa,
            "motivo_rampa": motivo_rampa,
            # Pedido × efetivo × por quê (05/10). `diverge: true` é a situação
            # que antes passava calada: 200 na tela, 80 saindo.
            "teto": teto,
            "cap_maximo_aceito": _agenda.CAP_DIARIO_MAXIMO,
            "cap_configurado": config_operacional.cap_da_corretora(cb.CORRETORA_CORRETOR),
            "disparos_hoje": disparos_hoje,
            "disparos_hoje_indisponivel": disparos_erro is not None,
            "disparos_hoje_motivo": disparos_erro,
            # "Hoje N de 80" conta o que GASTOU o teto: aceito pela Meta mais
            # reserva aberta. Entregue é outra coisa, e a tela precisa dos
            # dois (05/10). `hoje: None` = não consegui ler (nunca zero).
            "hoje": None if hoje_conta is None else {
                "conta_no_teto": disparos_hoje,
                "aceitos": hoje_conta["aceitos"],
                "entregues": hoje_conta["entregues"],
                "lidos": hoje_conta["lidos"],
                "falhas": hoje_conta["falhas"],
                "sem_confirmacao_ainda": hoje_conta["em_voo"],
                "sem_rastro": hoje_conta["sem_rastro"],
                "reservas_abertas": hoje_conta["reservas_abertas"],
                "incertos": hoje_conta["incertos"],
                "explicacao": (
                    "aceito = a Meta recebeu o pedido; entregue = chegou no "
                    "aparelho. O teto do dia conta aceitos e reservas abertas."
                ),
            },
            "hoje_indisponivel": "hoje" in garantia_erro,
            "hoje_motivo": garantia_erro.get("hoje"),
            "descanso_dias": cb.DESCANSO_DIAS,
            "detalhe": (
                f"quem é disparado descansa ~{cb.DESCANSO_DIAS} dias antes de "
                "voltar pra fila — o ciclo é circular, ninguém se esgota"
            ),
        },
        "variantes": variantes,
        "cota_marketing_pct": cc.COTA_MARKETING_PCT,
        # Edição do texto pela tela (05/10): espelho da variante de marketing,
        # teto de caracteres, problema no que está gravado (o mesmo que segura
        # o disparo) e o histórico de quem mudou o quê.
        "copies": copies,
        # "Quando a pessoa responde": quem vai pra corretora, a tag, a etapa
        # de entrada, a roleta do remarketing (e a do Meta, só leitura) e o
        # aviso. `config: None` = não consegui ler.
        "responde": _quando_responde_da_tela(),
        # `indisponivel` é o que separa "não tem ninguém na fila" de "não
        # consegui contar" — a tela mostra os dois de formas diferentes.
        "estoque": {
            "fila": fila, "disparado": disparado, "respondeu": respondeu,
            "indisponivel": estoque_erro is not None,
            "motivo_indisponivel": estoque_erro,
        },
        # O que a Meta ENTREGOU no último dia de disparo (não o que aceitou).
        # `placar` None + `indisponivel` False = o carrossel nunca disparou.
        # Ver `remarketing_placar` pro significado de cada balde.
        "entrega_ultimo_lote": {
            "placar": entrega,
            "indisponivel": entrega_erro is not None,
            "motivo_indisponivel": entrega_erro,
        },
        # Quando e quanto dispara, do jeito que a Dona gravou na tela, mais o
        # gasto da semana. `agenda: None` + `indisponivel` = não consegui ler.
        "agenda": {
            "config": agenda,
            "indisponivel": agenda_erro is not None,
            "motivo_indisponivel": agenda_erro,
        },
        # A última volta do robô, do registro durável (não do log): é o que
        # sustenta "ligado e rodando". `None` + sem erro = nunca rodou.
        "ultima_volta": ultima_volta,
        "ultima_volta_indisponivel": "ultima_volta" in garantia_erro,
        # Leads em Disparado sem disparo registrado (a mensagem pode ter saído
        # ou não). `None` = não consegui ler.
        "reservas_orfas": orfas,
        "reservas_orfas_indisponivel": "reservas" in garantia_erro,
        # O fechamento de cada dia, do conferente (previstos, aceitos,
        # entregues, lidos, falhas, respostas por tipo, alarmes).
        "conferencia": {
            "dias": conferencias,
            "indisponivel": "conferencia" in garantia_erro,
            "motivo_indisponivel": garantia_erro.get("conferencia"),
        },
        # Pra onde vai quem pede pra PARAR: as duas etapas que servem e a
        # que vale agora. `config: None` = não consegui ler.
        "parar": {
            "config": parar,
            "indisponivel": "parar" in garantia_erro,
            "motivo_indisponivel": garantia_erro.get("parar"),
        },
        # O aviso diário do conferente no Telegram (nasce desligado).
        "aviso_telegram": _aviso_telegram(),
        # Quem mudou o quê no painel e no papel dos números.
        "historico": historico,
        # Qualidade e estado da linha na Meta, com a hora da leitura.
        "saude_linha": saude,
        # Por qual número sai: os números do cliente, o papel de cada um e,
        # quando não há disparador, o motivo.
        "linha_disparadora": linha_disp,
        # De qual etapa e de quem: o gravado + as etapas reais do cliente.
        "alvo": {
            "config": alvo,
            "indisponivel": alvo_erro is not None,
            "motivo_indisponivel": alvo_erro,
        },
        "quando_responde": _passos_quando_responde(),
        "travas": [
            "não dispara pra quem tem opt-out",
            "não dispara pra quem está em cadência ou nutrição",
            "não escreve por cima de conversa humana viva",
            "não dispara pra telefone marcado como inválido "
            "(falhou e nunca deu sinal de vida)",
            "respeita a rampa de aquecimento da linha",
        ],
        # O interruptor DESTA automação é o `carrossel_corretor_ativo`, não o da
        # cadência: o carrossel roda no seu próprio tick e só olha essa chave.
        # Mostrar o estado da cadência aqui fazia a tela dizer "ligado" com o
        # disparo parado (e vice-versa).
        "estado": _estado_do_interruptor(),
        "flag": "carrossel_corretor_ativo",
    }


def _variantes_do_carrossel() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """As variantes como a tela desenha (peso e texto que valem, padrão,
    versão, desde quando) + o bloco da edição de copy."""
    from app.services import carrossel_copies as cc

    tela = cc.variantes_para_tela()
    padrao = cc.pesos_padrao()
    historico: list[dict[str, Any]] | None = None
    historico_erro: str | None = None
    try:
        historico = cc.historico_das_copies(30)
    except Exception as e:  # noqa: BLE001
        historico_erro = str(e)[:140]
    desde = cc.valendo_desde(historico or [])
    por_texto: dict[str, list[str]] = {}
    for v in tela["variantes"]:
        if v["ativa"]:
            por_texto.setdefault(v["texto"], []).append(v["id"])
    variantes = [
        {
            "id": v["id"],
            "peso": v["peso"],
            "peso_padrao": padrao.get(v["id"], v["peso"]),
            "categoria": "marketing" if v["marketing"] else "utility",
            "texto": v["texto"],
            "texto_padrao": v["texto_padrao"],
            "texto_personalizado": v["texto_personalizado"],
            "texto_versao": v["versao"],
            # Desde quando ESTE texto vale. None = é o do código e ninguém
            # mexeu. A que espelha acompanha a data da fonte.
            "texto_desde": desde.get(v["espelha"] if v["espelhando"] else v["id"]),
            "ativa": v["ativa"],
            "espelha": v["espelha"],
            "espelhando": v["espelhando"],
            "texto_igual_a": [x for x in por_texto.get(v["texto"], []) if x != v["id"]]
            if v["ativa"] else [],
            "texto_editavel": v["texto_editavel"],
            "previa": v["previa"],
        }
        for v in tela["variantes"]
    ]
    copies = {
        "espelho": tela["espelho"],
        "texto_max": tela["texto_max"],
        # Motivo pelo qual o disparo está segurando por causa do texto
        # (gravado por fora da tela). None = serve.
        "problema": tela["problema"],
        "indisponivel": tela["indisponivel"],
        "historico": historico,
        "historico_erro": historico_erro,
    }
    return variantes, copies


def _quando_responde_da_tela() -> dict[str, Any]:
    from app.services import quando_responde as qr

    try:
        with SessionLocal() as s:
            return {"config": qr.resumo(s, DEFAULT_TENANT_ID_METRICAS),
                    "indisponivel": False, "motivo_indisponivel": None}
    except Exception as e:  # noqa: BLE001 — a tela diz "não consegui ler"
        log.warning("mapa_quando_responde_indisponivel", erro=str(e)[:140])
        return {"config": None, "indisponivel": True,
                "motivo_indisponivel": str(e)[:140]}


def _passos_quando_responde() -> list[dict[str, Any]]:
    """Os passos de "respondeu → corretora", lidos da configuração que o
    handoff usa. Sem leitura não inventa passo: diz que não leu."""
    from app.services import quando_responde as qr

    r = _quando_responde_da_tela()
    cfg = r["config"]
    if not cfg or cfg.get("motivo") == "config_indisponivel":
        return [{"passo": 1, "quem": "sistema", "onde": "",
                 "o_que": "não consegui ler o que acontece com quem responde"}]
    if cfg.get("problema"):
        return [{"passo": 1, "quem": "sistema", "onde": "etapa Respondeu",
                 "o_que": f"ninguém é transferido agora: {cfg['problema']}"}]
    quem = ("todo mundo que cai em Respondeu"
            if cfg["quem_vai"] == qr.QUEM_TODOS
            else "quem clicou no botão ou mostrou interesse")
    nomes = [c["nome"] for c in cfg["roleta"]["corretoras"] if c["na_roleta"]]
    passos = [
        {"passo": 1, "quem": "sistema", "onde": "roleta do remarketing",
         "o_que": (f"{quem} ganha a tag {qr.TAG} e vai pra etapa "
                   f"'{cfg['etapa']['valendo_nome']}' do funil de vendas, com a "
                   f"próxima da roleta ({', '.join(nomes)})")},
    ]
    if cfg["aviso"]["ligado"]:
        passos.append({"passo": 2, "quem": "linha da corretora",
                       "onde": "template UTILITY, sem botão",
                       "o_que": "avisa a pessoa que o atendimento continua por ali"})
    passos.append({"passo": len(passos) + 1, "quem": "Pessoa (IA)",
                   "onde": "na linha da Corretor",
                   "o_que": "continua atendendo quem escreve pra linha, como hoje"})
    return passos


def _estado_do_interruptor() -> str:
    """`ligado`, `desligado` ou `indisponivel` (não consegui ler o banco —
    nesse estado o robô também não envia)."""
    try:
        return "ligado" if config_operacional.carrossel_corretor_ativo() else "desligado"
    except config_operacional.ConfigIndisponivel:
        return "indisponivel"


def _aviso_telegram() -> dict[str, Any]:
    """Estado da chave do aviso do conferente. Não consegui ler ≠ desligado."""
    try:
        return {"ligado": config_operacional.get(
            "remarketing_conferente_avisa_telegram") is True, "indisponivel": False}
    except config_operacional.ConfigIndisponivel:
        return {"ligado": None, "indisponivel": True}


def _saude_compacta(s: dict[str, Any] | None) -> dict[str, Any] | None:
    """O que o front novo lê em cada número (`PENDENCIAS-BACKEND.md`, item 1)."""
    if not s:
        return None
    return {"cor": s.get("cor"), "qualidade": s.get("qualidade"),
            "status": s.get("status"), "lido_em": s.get("lido_em"),
            "velha": bool(s.get("velha")), "erro": s.get("erro")}


def _ultima_volta(session: Any) -> dict[str, Any] | None:
    r = session.execute(sa_text(
        "SELECT em, ligado, motivo_parada, cap_do_dia, enviados, falhas, incertos "
        "  FROM remarketing_tick_log WHERE tenant_id = :t "
        " ORDER BY em DESC, id DESC LIMIT 1"
    ), {"t": DEFAULT_TENANT_ID_METRICAS}).first()
    if r is None:
        return None
    from app.services import linha_disparadora, remarketing_agenda, remarketing_alvo

    motivo = r[2]
    return {
        "em": r[0].isoformat() if r[0] else None,
        "ligado": r[1], "motivo_parada": motivo,
        "motivo_texto": (remarketing_agenda.MOTIVO_EM_PORTUGUES.get(motivo)
                         or linha_disparadora.MOTIVO_EM_PORTUGUES.get(motivo)
                         or remarketing_alvo.MOTIVO_EM_PORTUGUES.get(motivo)
                         or motivo),
        "cap_do_dia": r[3], "enviados": r[4], "falhas": r[5], "incertos": r[6],
    }


def _mapa_nutricao() -> dict[str, Any]:
    """Funil de nutrição (board /remarketing) — marcos e copies de `nutricao.py`."""
    from app.services import nutricao as nut

    marcos = [
        {
            "etapa": etapa,
            "tag": nut.TAG_POR_ETAPA.get(etapa),
            "copy": nut.COPIES_MARCO.get(etapa),
        }
        for etapa in nut.STAGES_NUTRICAO
    ]
    return {
        "descricao": (
            "Lead que ESGOTOU a cadência de 8 toques entra na etapa Base do funil "
            "de remarketing e recebe 1 mensagem a cada "
            f"{nut.INTERVALO_DIAS} dias pela linha da corretora dona do lead "
            "(ou pela da Pessoa12, quando a dela não tem linha ou modelo aprovado), "
            f"avançando de etapa a cada disparo. Para de vez no marco {nut.STAGE_FINAL}."
        ),
        "regra_entrada": (
            "cadência esgotada (status 'remarketing' pós-D10) → etapa Base "
            "(lead_events tipo='nutricao_entrada')"
        ),
        "intervalo_dias": nut.INTERVALO_DIAS,
        "etapa_final": nut.STAGE_FINAL,
        "marcos": marcos,
        # Kill-switch PRÓPRIO (05/08). Antes a lista de automações mostrava o
        # estado da cadência aqui — depois da separação isso viraria mentira na
        # primeira vez que as duas divergissem.
        "estado": "ligado" if config_operacional.nutricao_ativa() else "desligado",
        "flag": "nutricao_ativa",
        "copies_status": (
            "60/90/120 APROVADAS Dona 10/07; 30 e 150 RASCUNHO — copy real do "
            "Kommo chega via mapeamento"
        ),
    }


# Quem TIRA o lead da régua. A tela desenha a saída comum a partir de qualquer
# toque, e precisa saber quem interrompe e quem não: hoje o clique em botão é a
# única entrada DO LEAD que não interrompe (o auto-reply nem é o lead falando).
# `None` = não é saída do lead (é gatilho de entrada/trava do sistema).
_INTERROMPE_A_REGUA: dict[str, bool] = {
    "resposta_lead": True,
    "opt_out": True,
    "ja_encontrou": True,
    "pausa_30d": True,
    "esgotou_cadencia": True,
    "clique_botao": False,
    "auto_reply": False,
}


def _mapa_gatilhos() -> list[dict[str, Any]]:
    """Regras QUANDO → ENTÃO com o estado VIVO de cada flag."""
    from app.services import cadencia as cad

    def _estado(flag: bool) -> str:
        return "ligado" if flag else "desligado"

    gatilhos: list[dict[str, Any]] = [
        {
            "id": "auto_enroll_followup",
            # O Kommo saiu do circuito em 27/07/2026 — citar ele aqui manda a corretora
            # procurar num sistema que ninguém abre mais.
            "quando": "lead entra na etapa Follow-up (a corretora move no board)",
            "entao": "matricula na cadência de 8 toques (D0 dispara no próximo tick)",
            "estado": _estado(settings.cadencia_auto_enroll),
            "flag": "CADENCIA_AUTO_ENROLL",
            "detalhe": (
                "só leads da Pessoa12 (identificada por corretoras.evolution_instance, "
                "nunca por nome); idempotente — re-mover não duplica matrícula"
            ),
        },
        {
            "id": "resposta_lead",
            "quando": "lead matriculado responde no WhatsApp",
            "entao": (
                "cadência PARA (status 'respondida') + card move pra 'Em atendimento' "
                "+ badge 🟢 in-app pra corretora (aviso via WhatsApp foi aposentado — lei Dona 10/07)"
            ),
            "estado": "ligado",
            "flag": None,  # parte do inbound — sempre ativa junto com a cadência
            "detalhe": (
                f"timer de {cad.RESPOSTA_AUTO_REPLY_JANELA_S}s anti-secretária-eletrônica: "
                "inbound colado no nosso envio é auto-reply e NÃO conta como resposta; "
                "não rebaixa etapa avançada (Visita marcada, Proposta...)"
            ),
        },
        {
            "id": "opt_out",
            "quando": (
                "lead responde com pedido de saída "
                f"(ex: {', '.join(EXEMPLOS_OPT_OUT)})"
            ),
            "entao": "cadência PARA (status 'opt_out') + card move pra 'Venda perdida'",
            "estado": "ligado",
            "flag": None,
            "detalhe": "regra Dona 10/07 — opt-out nunca recebe mais nada",
        },
        # ── as outras saídas da régua ────────────────────────────────────────
        # Todas valem A PARTIR DE QUALQUER TOQUE — o mapa precisa mostrar isso,
        # senão a tela sugere que a pessoa percorre os 9 toques em linha reta.
        # `interrompe` diz quem TIRA o lead da régua: hoje o clique em botão é
        # a única entrada do lead que NÃO tira (e o auto-reply, que nem é o
        # lead falando).
        {
            "id": "ja_encontrou",
            "quando": (
                "lead responde que JÁ ENCONTROU / já resolveu (não é opt-out: "
                "não está bravo, terminou a busca)"
            ),
            "entao": (
                "Assistente se despede com a condição do mês + programa de "
                f"indicação, status '{cad.STATUS_JA_ENCONTROU}' e card move pra "
                "'Venda perdida' — SEM passar pelo atendimento da corretora"
            ),
            "estado": "ligado",
            "flag": None,
            "detalhe": (
                "opt-out GANHA desta regra: 'não quero mais' cai nas duas e "
                "mandar despedida com oferta pra quem pediu pra sair é o oposto "
                "do que ele pediu. Se a pessoa voltar a falar depois, aí vira "
                "atendimento normal"
            ),
        },
        {
            "id": "pausa_30d",
            "quando": (
                "lead escolhe '2' (pausa) no toque de encerramento — vale do "
                "último toque em diante, inclusive já no remarketing"
            ),
            "entao": (
                f"status 'pausada' por {cad.PAUSA_DIAS} dias; quando a data "
                "chega, o tick devolve o lead pro remarketing"
            ),
            "estado": "ligado",
            "flag": None,
            "detalhe": (
                "só vale NO FIM da régua: um '2' solto no meio é o lead "
                "respondendo outra coisa (quartos, horário), e virar pausa "
                "calaria alguém que quer conversar"
            ),
        },
        {
            "id": "clique_botao",
            "quando": (
                "lead CLICA no botão do template (hoje só o toque que oferece "
                "o áudio sai com botão)"
            ),
            "entao": (
                "manda o áudio da corretora dona e o lead CONTINUA na régua — "
                "o próximo toque chega na data prevista"
            ),
            "estado": "ligado",
            "flag": None,
            "detalhe": (
                "clique NÃO é resposta (Dona 05/08): antes o 'sim' vinha em "
                "texto, parava a cadência, e a pessoa saía sem nunca ter "
                "conversado com ninguém. 'Parar mensagens', mesmo clicado, é "
                "opt-out"
            ),
        },
        {
            "id": "auto_reply",
            "quando": (
                f"inbound chega em menos de {cad.RESPOSTA_AUTO_REPLY_JANELA_S}s "
                "depois do nosso envio, ou tem cara de secretária eletrônica / "
                "mensagem de empresa"
            ),
            "entao": "IGNORA — não marca respondida, não move o card, régua segue",
            "estado": "ligado",
            "flag": None,
            "detalhe": (
                "conservador: na dúvida NÃO marca como auto-reply. É a outra "
                "entrada que não interrompe a régua — mas não é o lead falando"
            ),
        },
        {
            "id": "esgotou_cadencia",
            "quando": "lead completa os 8 toques sem responder (pós-D10)",
            "entao": (
                "vira 'remarketing' e entra no funil de nutrição (etapa Base, "
                "board /remarketing) — marcos 30/60/90/120/150 dias"
            ),
            "estado": _estado(settings.cadencia_ativa),
            "flag": "CADENCIA_ATIVA",
            "detalhe": "kill-switch geral do motor de cadência",
        },
        {
            "id": "auto_followup_24h",
            "quando": (
                f"lead em 'Em atendimento' MUDO há {settings.cadencia_auto_followup_horas}h+ "
                "(última mensagem trocada é NOSSA — se o lead falou por último, quem "
                "deve resposta é a corretora e o lead NÃO se move)"
            ),
            "entao": "volta pra Follow-up, que religa a cadência sozinho (auto-enroll)",
            "estado": _estado(settings.cadencia_auto_followup_24h),
            "flag": "CADENCIA_AUTO_FOLLOWUP_24H",
            "detalhe": (
                "DESATIVADA por decisão da Dona — religa cadência REAL; só liga com OK "
                f"explícito dela (cap de segurança: {settings.cadencia_auto_followup_cap} moves/tick)"
            ),
        },
        {
            "id": "trava_so_corretor",
            "quando": "qualquer exibição do kanban /crm e matrícula automática",
            "entao": "só corretoras do piloto participam (demais ficam fora do CRM 2.0)",
            "estado": "ligado" if settings.crm_corretoras_ativas else "desligado",
            "flag": "CRM_CORRETORAS_ATIVAS",
            "detalhe": (
                f"corretoras ativas no piloto: {settings.crm_corretoras_ativas} "
                "(3 = Pessoa12); a sombra segue gravando TODAS na espinha — a trava é só de exibição"
            ),
        },
    ]
    for g in gatilhos:
        g["interrompe"] = _INTERROMPE_A_REGUA.get(g["id"])
    return gatilhos


def _matriculados(session: Any) -> dict[str, Any]:
    """Contagem viva de `cadencia_estado` por status."""
    from app.db.models import CadenciaEstado

    por_status: dict[str, int] = {
        st: int(n)
        for st, n in session.query(
            CadenciaEstado.status, sa_func.count(CadenciaEstado.id)
        )
        .group_by(CadenciaEstado.status)
        .all()
    }
    return {"por_status": por_status, "total": sum(por_status.values())}


# ── Endpoint ──────────────────────────────────────────────────────────────────


# ── Saúde de envio: "ligado" e "funcionando" são coisas diferentes ───────────
#
# 05/08: a nutrição aparecia como LIGADA no painel havia semanas e nunca tinha
# mandado uma única mensagem real (35 eventos, todos dry-run, todos de 22/07).
# Não era bug de uma automação — é a classe de falha que mais custou tempo na
# semana (funil zerado, Corretor zerada, templates fantasma): ausência se passando
# por número, sem erro nenhum em log.
#
# Aqui cada automação diz QUANDO mandou a última mensagem que SAIU DE VERDADE.
#
# LIMIAR POR AUTOMAÇÃO, não um "7 dias" pra todas — cada uma tem ritmo próprio e
# um número fixo dá alarme falso numa e silêncio na outra:
#   boas-vindas  3 dias — é reativa (dispara com lead novo), e a Acme recebe
#                lead todo dia útil; 3 dias sem nenhuma = tráfego parado ou
#                automação quebrada, e as duas coisas a Dona quer saber;
#   follow-up    3 dias — régua diária, com centenas de matriculados;
#   remarketing  3 dias — carrossel dispara todo dia por desenho;
#   nutrição     7 dias — o marco é de 30 em 30 dias POR LEAD, mas a fila tem
#                centenas de leads em datas diferentes: com gente na régua, uma
#                semana inteira sem nenhum disparo é anomalia. Usar 30 aqui
#                deixaria a automação "saudável" parada um mês inteiro.
#
# Fim de semana conta no prazo de propósito: a operação roda de segunda a sexta,
# então um limiar de 3 dias já absorve o sábado e o domingo sem alarme falso.
_LIMIAR_SEM_ENVIO_DIAS: dict[str, int] = {
    "boas-vindas": 3,
    "follow-up": 3,
    "remarketing": 3,
    "nutricao": 7,
}


def _saude_envio(session: Any, tenant_id: int = 1) -> dict[str, dict[str, Any]]:
    """Último envio REAL de cada automação + se isso já passou do ritmo dela.

    `sent=true` é a mesma régua do contador anti-ban: dry-run, toque pulado e
    falha de POST não contam — nada disso chegou em ninguém.

    Best-effort por automação: uma query que falha (tabela ausente em dev) vira
    "sem dado" naquela linha, nunca derruba o mapa inteiro.
    """
    from app.db.models import CadenciaEvento, LeadEvent
    from app.services import cadencia as cad

    # ORM (e não SQL cru) porque o mesmo código roda no Postgres de produção e
    # no SQLite dos testes — `payload->>'x'` + `::boolean` só existe num deles.
    def _cadencia_q(nutricao: bool):
        q = session.query(sa_func.max(CadenciaEvento.at)).filter(
            CadenciaEvento.tenant_id == tenant_id,
            CadenciaEvento.tipo == cad.TIPO_MENSAGEM,
            CadenciaEvento.payload["sent"].as_boolean().is_(True),
        )
        if nutricao:
            return q.filter(CadenciaEvento.payload["nutricao"].as_boolean().is_(True))
        # toque preenchido = toque da régua; a nutrição grava toque=None.
        return q.filter(CadenciaEvento.toque.isnot(None))

    def _lead_event_q(tipo: str, *, exige_sent: bool):
        q = session.query(sa_func.max(LeadEvent.at)).filter(
            LeadEvent.tenant_id == tenant_id, LeadEvent.tipo == tipo
        )
        if exige_sent:
            q = q.filter(LeadEvent.payload["sent"].as_boolean().is_(True))
        return q

    fontes = {
        "follow-up": lambda: _cadencia_q(False),
        "nutricao": lambda: _cadencia_q(True),
        "boas-vindas": lambda: _lead_event_q("boas_vindas_enviada", exige_sent=True),
        # carrossel: o evento só é gravado depois do envio (falha levanta
        # _NadaSaiu antes), mas o payload não carrega `sent` — em DRY-RUN global
        # ele entraria. É a leitura menos precisa das quatro, e o campo `fonte`
        # diz isso pra ninguém tratar as quatro como iguais.
        "remarketing": lambda: _lead_event_q("carrossel_disparo", exige_sent=False),
    }
    agora = datetime.now(timezone.utc)
    out: dict[str, dict[str, Any]] = {}
    for chave, montar in fontes.items():
        limiar = _LIMIAR_SEM_ENVIO_DIAS.get(chave, 7)
        try:
            ultimo = montar().scalar()
        except Exception as e:  # noqa: BLE001 — saúde não derruba o mapa
            log.warning("saude_envio_indisponivel", automacao=chave, erro=str(e)[:140])
            try:
                session.rollback()
            except Exception:  # noqa: BLE001
                pass
            out[chave] = {
                "disponivel": False, "ultimo_envio_real": None, "dias": None,
                "nunca_enviou": None, "alerta": False, "limiar_dias": limiar,
                "fonte": "indisponível",
            }
            continue
        if ultimo is not None and ultimo.tzinfo is None:
            ultimo = ultimo.replace(tzinfo=timezone.utc)
        dias = (agora - ultimo).days if ultimo else None
        out[chave] = {
            "disponivel": True,
            "ultimo_envio_real": ultimo.isoformat() if ultimo else None,
            "dias": dias,
            # NUNCA enviou é diferente de "faz tempo": é a automação que nasceu
            # morta, e foi exatamente o caso da nutrição.
            "nunca_enviou": ultimo is None,
            "alerta": ultimo is None or (dias is not None and dias > limiar),
            "limiar_dias": limiar,
            "fonte": (
                "evento de disparo (sem marca de envio real)"
                if chave == "remarketing"
                else "envio confirmado (sent=true)"
            ),
        }
    return out


def _bloco_ou_erro(session: Any, qual: str) -> dict[str, Any]:
    """Bloco por corretora do follow-up ou da nutrição. Falhou → só o motivo
    (a tela diz "não consegui ler"), o resto do mapa segue."""
    from app.services import painel_automacoes as pa

    try:
        return (pa.bloco_follow_up(session) if qual == "follow_up"
                else pa.bloco_nutricao(session))
    except Exception as e:  # noqa: BLE001
        log.warning("painel_bloco_indisponivel", bloco=qual, erro=str(e)[:140])
        try:
            session.rollback()
        except Exception:  # noqa: BLE001
            pass
        return {"indisponivel": True, "motivo": str(e)[:160]}


@router.get("/automacoes")
def painel_automacoes(
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Mapa vivo das automações: boas-vindas, cadência, nutrição, gatilhos e operação.

    Admin-only (dado agregado de disparo — mesma classe de /painel/cadencia).
    Tudo lido do código/config REAL na hora do request: mudou o código/env,
    mudou a tela.
    """
    # import tardio: painel.py é pesado — só os guards de auth interessam.
    from app.api.painel import _identificar_usuario, _require_admin

    user = _identificar_usuario(x_painel_user)
    _require_admin(user)

    with SessionLocal() as s:
        matriculados = _matriculados(s)
        cadencia_mapa = _mapa_cadencia(s)
        # A boas-vindas precisa da sessão pra listar as corretoras do CADASTRO
        # (a copy de cada linha sai do banco, não de um mapa de ids no código).
        boas_vindas_mapa = _mapa_boas_vindas(s)
        saude = _saude_envio(s)
        # Por corretora (06/10): interruptor, linha, régua, saiu/falhou.
        follow_up = _bloco_ou_erro(s, "follow_up")
        nutricao_pc = _bloco_ou_erro(s, "nutricao")

    nutricao_mapa = _mapa_nutricao()
    nutricao_mapa["por_corretora"] = nutricao_pc
    # Nos nomes que o front pediu (PENDENCIAS-BACKEND itens 6 e 7).
    from app.services import painel_automacoes as _pa
    from app.services.nutricao import LINHA_DA_NUTRICAO

    _ln = _pa.linha(LINHA_DA_NUTRICAO)
    nutricao_mapa["linha"] = _pa.linha_curta(LINHA_DA_NUTRICAO)
    nutricao_mapa["linha_corretora_id"] = (_ln or {}).get("corretora_id")
    _env_bv = boas_vindas_mapa.get("envios") or {}
    boas_vindas_mapa["aguardando"] = ((_env_bv.get("em_novo") or {}).get("sem_boas_vindas")
                                      if _env_bv.get("em_novo") is not None else None)
    mapa: dict[str, Any] = {
        "saude_envio": saude,
        "boas_vindas": boas_vindas_mapa,
        "follow_up": follow_up,
        "carrossel_corretor": _mapa_carrossel_corretor(),
        "nutricao": nutricao_mapa,
    }
    try:
        from app.services import painel_automacoes as pa

        resumo = pa.resumo_da_lista(mapa)
        resumo_erro = None
    except Exception as e:  # noqa: BLE001 — a lista não derruba o mapa
        log.warning("painel_resumo_indisponivel", erro=str(e)[:140])
        resumo, resumo_erro = None, str(e)[:160]

    return {
        # Uma linha por automação pra lista: estado (ligada/desligada/agindo/
        # parada e por quê), linhas com a cor, número do dia, última ação.
        "resumo": resumo,
        "resumo_indisponivel": resumo_erro is not None,
        "resumo_motivo": resumo_erro,
        # "ligado" não é "funcionando": quando cada automação mandou a última
        # mensagem que chegou em alguém de verdade.
        "saude_envio": saude,
        "gerado_em": datetime.now(timezone.utc).isoformat(),
        "operacao": {
            "dry_run": settings.dry_run,
            # Estado EFETIVO (painel > .env) — não o do arquivo. Antes a tela
            # mostrava o valor do `.env` e o motor podia estar rodando outro.
            "cadencia_ativa": config_operacional.cadencia_ativa(),
            "cap_diario": config_operacional.cap_diario(),
            "min_intervalo_s": settings.cadencia_min_intervalo_s,
            "cooldown_remarketing_dias": settings.cadencia_cooldown_dias,
            "timer": _timer_info(),
            "matriculados": matriculados,
        },
        "boas_vindas": boas_vindas_mapa,
        "cadencia": cadencia_mapa,
        # Follow-up por corretora: chave dela + geral, linha, régua, envios.
        "follow_up": follow_up,
        "carrossel_corretor": mapa["carrossel_corretor"],
        "nutricao": nutricao_mapa,
        "gatilhos": _mapa_gatilhos(),
        "todo": (
            "copies da cadência já são editáveis pela tela (overrides em "
            "cadencia_copies); nutrição e gatilhos seguem somente leitura"
        ),
    }


# ── Edição de copies da cadência (overrides em `cadencia_copies`) ────────────
#
# Só o TEXTO muda — cronograma, guards, cap diário e dry-run seguem intocados.
# `toque` aceita os índices de MENSAGEM da cadência (0, 2, 3, 5, 7) e -1 pro
# remarketing. Tarefas de ligação (1, 4, 6) não têm copy — 404.

_COPY_MAX_CHARS = 900  # o texto vai no {{2}} do template WABA — corpo tem teto ~1024

_PLACEHOLDER_RE = re.compile(r"\{\{?\s*\w+\s*\}?\}")


class CopyUpdateIn(BaseModel):
    texto: str


def _toques_editaveis() -> dict[int, dict[str, Any]]:
    """Toques com copy editável: os de mensagem + remarketing (-1)."""
    from app.services import cadencia as cad

    itens: dict[int, dict[str, Any]] = {}
    for t in cad.TOQUES:
        if t.tipo != cad.TIPO_MENSAGEM:
            continue
        itens[t.indice] = {
            "toque": t.indice,
            "dia": f"D{t.offset_dias}",
            "tipo": t.tipo,
            "categoria": t.categoria,
            "com_botao": False,  # Dona 28/07: follow-up SEM botão
            "default": cad._TEMPLATES_MSG.get(t.indice, ""),
        }
    itens[cad.TOQUE_REMARKETING] = {
        "toque": cad.TOQUE_REMARKETING,
        "dia": "D10+",
        "tipo": "remarketing",
        "categoria": "utility",
        "com_botao": False,
        "default": cad._TEMPLATE_REMARKETING,
    }
    return itens


def _validar_copy(texto: str, default: str) -> str:
    """Valida o texto do override. Regras da casa:
      - não vazio, teto de tamanho ({{2}} do template WABA);
      - termina com pergunta ("?") — regra do motor: toda mensagem pede próxima ação;
      - sem placeholders novos ({nome}, {{1}}...): as copies vão no {{2}} de um
        template cujo {{1}} JÁ cumprimenta — placeholder que o motor não
        substitui sairia literal pro lead.
    Devolve o texto normalizado (strip)."""
    t = (texto or "").strip()
    if not t:
        raise HTTPException(status_code=400, detail="copy_vazia")
    if len(t) > _COPY_MAX_CHARS:
        raise HTTPException(
            status_code=400,
            detail=f"copy_longa_demais (máx {_COPY_MAX_CHARS} caracteres)",
        )
    # remove emojis/pontuação decorativa do fim pra checar a pergunta
    fim = t.rstrip(" \n\t💛🏠😊🙂✨❤️🧡💚")
    if not fim.endswith("?"):
        raise HTTPException(
            status_code=400,
            detail="copy_sem_pergunta (toda mensagem da cadência termina com '?')",
        )
    permitidos = set(_PLACEHOLDER_RE.findall(default))
    novos = [p for p in _PLACEHOLDER_RE.findall(t) if p not in permitidos]
    if novos:
        raise HTTPException(
            status_code=400,
            detail=f"placeholder_nao_suportado: {', '.join(sorted(set(novos)))}",
        )
    return t


@router.get("/automacoes/copies")
def listar_copies(
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Lista os toques de mensagem com copy DEFAULT + override + EFETIVA.
    Admin-only. É a fonte da tela de edição no painel V2."""
    from app.api.painel import _identificar_usuario, _require_admin
    from app.db.models import CadenciaCopy

    user = _identificar_usuario(x_painel_user)
    _require_admin(user)

    itens = _toques_editaveis()
    with SessionLocal() as s:
        overrides = {
            c.toque: c
            for c in s.query(CadenciaCopy).filter(CadenciaCopy.tenant_id == 1).all()
            if c.toque in itens
        }
        out = []
        for toque, item in sorted(itens.items(), key=lambda kv: (kv[0] < 0, kv[0])):
            ov = overrides.get(toque)
            out.append({
                **item,
                "override": ov.texto if ov else None,
                "efetiva": ov.texto if ov and (ov.texto or "").strip() else item["default"],
                "personalizada": bool(ov and (ov.texto or "").strip()),
                "atualizado_em": ov.atualizado_em.isoformat() if ov and ov.atualizado_em else None,
                "atualizado_por": ov.atualizado_por if ov else None,
            })
    return {"copies": out, "max_chars": _COPY_MAX_CHARS}


@router.put("/automacoes/copies/{toque}")
def salvar_copy(
    toque: int,
    body: CopyUpdateIn,
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Salva o override de copy do toque (upsert em `cadencia_copies`).
    O motor passa a enfileirar este texto no próximo tick. Admin-only."""
    from app.api.painel import _identificar_usuario, _require_admin
    from app.db.models import CadenciaCopy

    user = _identificar_usuario(x_painel_user)
    _require_admin(user)

    itens = _toques_editaveis()
    if toque not in itens:
        raise HTTPException(status_code=404, detail="toque_sem_copy_editavel")

    texto = _validar_copy(body.texto, itens[toque]["default"])
    agora = datetime.now(timezone.utc)
    with SessionLocal() as s:
        row = (
            s.query(CadenciaCopy)
            .filter(CadenciaCopy.tenant_id == 1, CadenciaCopy.toque == toque)
            .first()
        )
        if row is None:
            row = CadenciaCopy(tenant_id=1, toque=toque, texto=texto)
            s.add(row)
        row.texto = texto
        row.atualizado_por = user
        row.atualizado_em = agora
        s.commit()
    log.info("cadencia_copy_salva", toque=toque, user=user, chars=len(texto))
    return {
        "ok": True,
        "toque": toque,
        "efetiva": texto,
        "personalizada": texto != itens[toque]["default"],
        "atualizado_em": agora.isoformat(),
    }


@router.delete("/automacoes/copies/{toque}")
def restaurar_copy(
    toque: int,
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Restaura o padrão: apaga o override — o motor volta pro texto do código.
    Idempotente (sem override → ok igual). Admin-only."""
    from app.api.painel import _identificar_usuario, _require_admin
    from app.db.models import CadenciaCopy

    user = _identificar_usuario(x_painel_user)
    _require_admin(user)

    itens = _toques_editaveis()
    if toque not in itens:
        raise HTTPException(status_code=404, detail="toque_sem_copy_editavel")

    with SessionLocal() as s:
        n = (
            s.query(CadenciaCopy)
            .filter(CadenciaCopy.tenant_id == 1, CadenciaCopy.toque == toque)
            .delete()
        )
        s.commit()
    log.info("cadencia_copy_restaurada", toque=toque, user=user, removidos=n)
    return {"ok": True, "toque": toque, "efetiva": itens[toque]["default"], "personalizada": False}


# ── Métricas por toque da cadência ───────────────────────────────────────────
#
# Pedido da Dona (04/08): "o D0 está com 2% de engajamento". Até aqui a tela de
# automação mostrava o que a máquina FAZ, nunca o que ela COLHE — dava pra
# reescrever uma copy sem nunca saber se a anterior funcionava.
#
# DEFINIÇÃO ESCOLHIDA (documentada porque é discutível):
#   enviados     = eventos tipo='mensagem' daquele toque com payload.sent = true
#                  (só o que SAIU DE VERDADE — dry-run, toque pulado por
#                  categoria e falha de POST não entram, mesma régua do
#                  contador anti-ban);
#   responderam  = cadências que, DEPOIS de receber aquele toque e ANTES do
#                  PRÓXIMO ENVIO DE MENSAGEM da mesma cadência, geraram um
#                  evento tipo='resposta' — ou seja, a resposta caiu na janela
#                  aberta por aquele toque (atribuição last-touch de mensagem);
#   opt_outs     = mesma janela, mas payload.opt_out = true. NÃO entram na taxa:
#                  opt-out é engajamento negativo e somar com resposta esconderia
#                  uma copy que está queimando a base;
#   taxa         = responderam / enviados (None quando enviados = 0 — "sem dado"
#                  não é 0%).
#
# Por que a janela fecha no próximo ENVIO DE MENSAGEM e não no próximo toque
# qualquer: tarefa de ligação não gera mensagem no WhatsApp, então uma resposta
# que chega depois dela ainda foi provocada pela última mensagem. Ligação não
# tem taxa de resposta — a tela mostra "tarefa de ligação", nunca 0%.
#
# AMOSTRA PEQUENA: abaixo de 20 envios a taxa é ruído. O número aparece, mas
# marcado — sem isso vira decisão de copy em cima de 3 envios.
#
# Agregação em Python (e não SQL com window function) de propósito: o volume é
# de milhares de linhas por mês e a mesma função roda igual em Postgres e no
# SQLite dos testes.

AMOSTRA_MINIMA = 20
METRICAS_DIAS_MAX = 365
MARCO_MOTIVO_MAX = 300


def _ultimo_marco(session: Any) -> Any | None:
    """Marco de régua mais recente, ou None se ninguém recomeçou a medição.

    Best-effort: árvore sem a tabela criada (dev, banco antigo) mede tudo, como
    antes. Métrica não pode cair porque uma tabela de metadado não existe.
    """
    from app.db.models import ReguaMarco

    try:
        return (
            session.query(ReguaMarco)
            .order_by(ReguaMarco.criado_em.desc(), ReguaMarco.id.desc())
            .first()
        )
    except Exception as e:  # noqa: BLE001
        log.warning("regua_marcos_indisponivel", erro=str(e)[:140])
        session.rollback()
        return None


# ── Visita: o que a resposta VIROU ───────────────────────────────────────────
#
# Medir resposta é medir conversa; a Acme vive de visita. Duas contagens
# SEPARADAS, nunca somadas (o ponto que a própria Dona levantou):
#
#   marcadas     = tudo que virou agenda (agendada + pendente_confirmacao +
#                  compareceu). Cancelada NÃO entra: agenda desfeita não é
#                  visita marcada pra efeito de resultado.
#   compareceram = só `compareceu`. É o único status que alguém confirmou.
#   vencidas_sem_confirmacao = data_hora no passado e status ainda
#                  `pendente_confirmacao`. Na prática não compareceu — só que
#                  ninguém fechou o registro. É GARGALO OPERACIONAL, não
#                  estatística: some com "marcadas" e a taxa de comparecimento
#                  vira ficção.
#
# ATRIBUIÇÃO — honestidade acima de granularidade: a visita é atribuída à
# AUTOMAÇÃO (total), não a um toque. `visitas` não guarda qual toque provocou a
# marcação, e inventar o "último toque antes da visita" seria dar a um número o
# ar de precisão que ele não tem. Fica no total, e o campo `definicao` diz isso.
STATUS_VISITA_MARCADA = ("agendada", "pendente_confirmacao", "compareceu")
STATUS_VISITA_COMPARECEU = "compareceu"
STATUS_VISITA_PENDENTE = "pendente_confirmacao"


def _metricas_visita(
    session: Any, *, desde: datetime, ate: datetime, tenant_id: int
) -> dict[str, Any]:
    """Visitas de quem passou pela cadência, na janela de medição.

    Casamento por DOIS caminhos, nesta ordem de cobertura:
      1. `lead_id` (é o que a maioria das visitas tem — `telefone_lead` está
         preenchido em pouquíssimas linhas, medido em 05/08: 4 de 191);
      2. sufixo de 8 dígitos do telefone, quando existir — a mesma chave que o
         resto do sistema usa (as grafias '+55…' e '55…' convivem no banco
         desde o seed legado).

    Best-effort: árvore sem a tabela `visitas` devolve `disponivel: False` em
    vez de derrubar a métrica inteira.
    """
    vazio: dict[str, Any] = {
        "disponivel": False,
        "marcadas": None,
        "compareceram": None,
        "vencidas_sem_confirmacao": None,
        "taxa_comparecimento": None,
        "sem_dado": True,
    }
    try:
        linhas = session.execute(
            sa_text(
                """
                SELECT v.status,
                       (v.data_hora < :ate) AS ja_passou
                  FROM visitas v
                 -- A janela é a data em que a visita foi MARCADA, não a data
                 -- dela: visita marcada hoje pra semana que vem é resultado de
                 -- hoje. Filtrar por `data_hora` deixaria de fora justamente a
                 -- agenda futura, que é o que a automação acabou de produzir.
                 WHERE v.criado_em >= :desde AND v.criado_em <= :ate
                   AND EXISTS (
                        SELECT 1
                          FROM cadencia_estado c
                         WHERE c.tenant_id = :t
                           AND (
                             (v.lead_id IS NOT NULL
                              AND c.lead_id IS NOT NULL
                              AND c.lead_id::text = v.lead_id::text)
                             OR (
                               coalesce(v.telefone_lead,'') <> ''
                               AND right(regexp_replace(v.telefone_lead,
                                                        '\\D', '', 'g'), 8)
                                 = right(regexp_replace(coalesce(c.telefone,''),
                                                        '\\D', '', 'g'), 8)
                             )
                           )
                   )
"""
            ),
            {"desde": desde, "ate": ate, "t": tenant_id},
        ).all()
    except Exception as e:  # noqa: BLE001 — métrica não cai por tabela ausente
        log.warning("metricas_visita_indisponivel", erro=str(e)[:140])
        try:
            session.rollback()
        except Exception:  # noqa: BLE001
            pass
        return vazio

    marcadas = compareceram = vencidas = 0
    for status, ja_passou in linhas:
        st = (status or "").strip()
        if st in STATUS_VISITA_MARCADA:
            marcadas += 1
        if st == STATUS_VISITA_COMPARECEU:
            compareceram += 1
        if st == STATUS_VISITA_PENDENTE and ja_passou:
            vencidas += 1

    return {
        "disponivel": True,
        "marcadas": marcadas,
        "compareceram": compareceram,
        "vencidas_sem_confirmacao": vencidas,
        # sem visita marcada não existe taxa — "sem dado" ≠ 0%.
        "taxa_comparecimento": (
            round(compareceram / marcadas * 100, 1) if marcadas else None
        ),
        "sem_dado": marcadas == 0,
        "amostra_pequena": 0 < marcadas < AMOSTRA_MINIMA,
    }


def calcular_metricas(
    session: Any,
    *,
    dias: int = 30,
    agora: datetime | None = None,
    tenant_id: int = DEFAULT_TENANT_ID_METRICAS,
) -> dict[str, Any]:
    """Engajamento por toque da cadência na janela de `dias`.

    Função pura sobre a sessão (sem HTTP) pra ser testável direto. Índice de
    toque NUNCA é fixado aqui: a régua vem de `cadencia.TOQUES`.
    """
    from app.db.models import CadenciaEvento
    from app.services import cadencia as cad

    agora = agora or datetime.now(timezone.utc)
    desde = agora - timedelta(days=dias)

    # Marco de régua: a métrica só conta o que veio DEPOIS do último recomeço.
    # Nada é apagado — o corte é de LEITURA. Se o marco for mais recente que a
    # janela pedida, quem manda é o marco (e a tela avisa desde quando mede,
    # senão a Dona olha 3 dias de dado achando que são 30).
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
            CadenciaEvento.tipo.in_(
                (cad.TIPO_MENSAGEM, cad.TIPO_TAREFA_LIGACAO, cad.TIPO_RESPOSTA)
            ),
        )
        .order_by(CadenciaEvento.cadencia_id, CadenciaEvento.at, CadenciaEvento.id)
        .all()
    )

    enviados: dict[int, int] = {}
    tarefas: dict[int, int] = {}
    respostas: dict[int, int] = {}
    opt_outs: dict[int, int] = {}

    por_cadencia: dict[int, list[Any]] = {}
    for ev in eventos:
        por_cadencia.setdefault(ev.cadencia_id, []).append(ev)
        payload = ev.payload or {}
        if ev.toque is None:
            continue
        if ev.tipo == cad.TIPO_MENSAGEM and payload.get("sent") is True:
            enviados[ev.toque] = enviados.get(ev.toque, 0) + 1
        elif ev.tipo == cad.TIPO_TAREFA_LIGACAO:
            tarefas[ev.toque] = tarefas.get(ev.toque, 0) + 1

    for linha in por_cadencia.values():
        atual: int | None = None
        for ev in linha:
            payload = ev.payload or {}
            if ev.tipo == cad.TIPO_MENSAGEM and ev.toque is not None:
                atual = ev.toque if payload.get("sent") is True else None
            elif ev.tipo == cad.TIPO_RESPOSTA and atual is not None:
                if payload.get("opt_out") is True:
                    opt_outs[atual] = opt_outs.get(atual, 0) + 1
                else:
                    respostas[atual] = respostas.get(atual, 0) + 1
                atual = None  # a cadência parou aqui — nunca credita duas vezes

    toques: list[dict[str, Any]] = []
    for t in cad.TOQUES:
        item: dict[str, Any] = {
            "indice": t.indice,
            "dia": f"D{t.offset_dias}",
            "offset_dias": t.offset_dias,
            "tipo": t.tipo,
            "canal": t.canal,
            "categoria": t.categoria,
        }
        if t.tipo == cad.TIPO_MENSAGEM:
            n = enviados.get(t.indice, 0)
            r = respostas.get(t.indice, 0)
            item.update({
                "enviados": n,
                "responderam": r,
                "opt_outs": opt_outs.get(t.indice, 0),
                # zero e ausência são coisas diferentes: sem envio não existe taxa.
                # PERCENTUAL, não fração: todo o resto do painel (conv_atendeu,
                # taxa_resposta_pct, taxa_comparecimento) usa 0-100. Devolver
                # 0.0357 aqui faria a tela mostrar "0,04%" onde é 3,6%.
                "taxa": round(r / n * 100, 1) if n else None,
                "sem_dado": n == 0,
                "amostra_pequena": 0 < n < AMOSTRA_MINIMA,
                "tem_taxa": True,
            })
        else:
            item.update({
                "tarefas": tarefas.get(t.indice, 0),
                "enviados": None,
                "responderam": None,
                "opt_outs": None,
                "taxa": None,
                "sem_dado": tarefas.get(t.indice, 0) == 0,
                "amostra_pequena": False,
                # ligação não é mensagem: não existe taxa de resposta pra ela.
                "tem_taxa": False,
            })
        toques.append(item)

    # Totais somados A PARTIR DAS LINHAS EXIBIDAS, não de todos os eventos.
    # Motivo: o índice do toque é gravado no evento, mas a RÉGUA muda (o D3 de
    # áudio entrou em 04/08 e deslocou todo mundo). Eventos de uma régua antiga
    # carregam índices que hoje são outro toque — ou nem existem mais. Somar o
    # balde inteiro dava um total maior que a soma da tabela, e tabela que não
    # fecha com o total é a primeira coisa que faz a Dona parar de confiar no
    # número. O que não aparece na régua de hoje fica de fora dos dois.
    total_enviados = sum(
        int(t["enviados"] or 0) for t in toques if t["tem_taxa"]
    )
    total_respostas = sum(
        int(t["responderam"] or 0) for t in toques if t["tem_taxa"]
    )
    total_opt_outs = sum(int(t["opt_outs"] or 0) for t in toques if t["tem_taxa"])
    total_tarefas = sum(int(t.get("tarefas") or 0) for t in toques if not t["tem_taxa"])
    return {
        "dias": dias,
        "desde": desde.isoformat(),
        "ate": agora.isoformat(),
        "visitas": _metricas_visita(session, desde=desde, ate=agora, tenant_id=tenant_id),
        # medindo_desde/motivo_do_marco: a tela precisa dizer que o número tem
        # 3 dias de vida e por quê — "últimos 30 dias" com 3 dias de dado é a
        # forma mais rápida de tomar decisão errada.
        "medindo_desde": marco_em.isoformat() if marco_em else None,
        "motivo_do_marco": getattr(marco, "motivo", None),
        "marco_por": getattr(marco, "criado_por", None),
        "amostra_minima": AMOSTRA_MINIMA,
        "toques": toques,
        "total": {
            "enviados": total_enviados,
            "responderam": total_respostas,
            "opt_outs": total_opt_outs,
            "tarefas": total_tarefas,
            "taxa": (round(total_respostas / total_enviados * 100, 1)
                     if total_enviados else None),
            "sem_dado": total_enviados == 0,
            "amostra_pequena": 0 < total_enviados < AMOSTRA_MINIMA,
        },
        "definicao": (
            "enviados = mensagens que SAÍRAM de verdade naquele toque; "
            "responderam = leads que responderam depois dele e antes da próxima "
            "mensagem; opt-out conta separado e não entra na taxa; abaixo de "
            f"{AMOSTRA_MINIMA} envios a taxa é ruído e vem marcada. Só entra na "
            "conta o que casa com a régua de HOJE — quando um toque novo entra "
            "no meio da régua, o histórico anterior a ele fica de fora. "
            "VISITA é do total da automação, não de um toque: o registro da "
            "visita não guarda qual toque a provocou, e atribuir por toque seria "
            "inventar precisão. 'Marcadas' e 'compareceram' NUNCA são somadas — "
            "visita vencida sem confirmação aparece à parte porque é pendência "
            "de registro, não estatística."
        ),
    }


@router.get("/automacoes/metricas")
def metricas_automacoes(
    dias: int = 30,
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Engajamento por toque da cadência (últimos `dias`). Admin-only."""
    from app.api.painel import _identificar_usuario, _require_admin

    user = _identificar_usuario(x_painel_user)
    _require_admin(user)

    if dias < 1 or dias > METRICAS_DIAS_MAX:
        raise HTTPException(
            status_code=422, detail=f"dias fora da faixa (1 a {METRICAS_DIAS_MAX})"
        )

    with SessionLocal() as s:
        dados = calcular_metricas(s, dias=dias)
        # Entregue e falha na mesma janela (06/10, pendência 8 do front).
        if isinstance(dados.get("total"), dict):
            from app.services import painel_automacoes as _pa

            dados["total"].update(_pa.entrega_da_janela(s, "follow-up", dados))
    return {"gerado_em": datetime.now(timezone.utc).isoformat(), **dados}


class MarcoIn(BaseModel):
    motivo: str


@router.post("/automacoes/metricas/marco")
def criar_marco_regua(
    body: MarcoIn,
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Recomeça a medição: daqui pra frente a métrica conta só o que vier depois.

    NÃO apaga nada. `cadencia_eventos` é auditoria — é como a gente prova o que
    saiu pra quem. O que muda é o ponto de partida da LEITURA; apagar a linha do
    marco devolve o histórico inteiro. Admin-only.
    """
    from app.api.painel import _identificar_usuario, _require_admin
    from app.db.models import ReguaMarco

    user = _identificar_usuario(x_painel_user)
    _require_admin(user)

    motivo = (body.motivo or "").strip()
    if not motivo:
        raise HTTPException(status_code=400, detail="motivo_obrigatorio")
    if len(motivo) > MARCO_MOTIVO_MAX:
        raise HTTPException(
            status_code=400,
            detail=f"motivo_longo_demais (máx {MARCO_MOTIVO_MAX} caracteres)",
        )

    agora = datetime.now(timezone.utc)
    with SessionLocal() as s:
        marco = ReguaMarco(criado_em=agora, motivo=motivo, criado_por=user)
        s.add(marco)
        s.commit()
        dados = calcular_metricas(s, dias=30)
    log.info("regua_marco_criado", motivo=motivo[:120], user=user)
    return {
        "ok": True,
        "medindo_desde": agora.isoformat(),
        "motivo_do_marco": motivo,
        "metricas": dados,
    }


# ── Métricas do remarketing (carrossel da Corretor) ───────────────────────────────
#
# O teste A/B do carrossel já roda e já é gravado (`carrossel_corretor.placar`), mas
# o resultado só existia em query manual: a Dona escolhia peso de variante sem
# ver a taxa de resposta de cada uma. Aqui ele vira tela.
#
# Mesma régua honesta das métricas da cadência, de propósito:
#   - `taxa = None` quando ninguém recebeu ("sem dado" ≠ 0%);
#   - abaixo de AMOSTRA_MINIMA envios a taxa vem marcada como ruído;
#   - o MARCO de régua manda: recomeçou a medição, conta do marco pra cá.
#
# A CATEGORIA é a que SAIU, não a pretendida — variante marcada marketing cai
# em UTILITY quando a cota do dia fecha ou não há template MARKETING aprovado.
# Por isso cada variante mostra também a quebra por categoria: uma linha
# "direta_marketing / UTILITY" com volume alto quer dizer que o braço de
# marketing não está saindo, e a comparação de categoria não está acontecendo.


def calcular_metricas_remarketing(
    session: Any,
    *,
    dias: int = 30,
    agora: datetime | None = None,
    tenant_id: int = DEFAULT_TENANT_ID_METRICAS,
) -> dict[str, Any]:
    """Placar do teste A/B do carrossel + estoque das etapas."""
    from app.services import carrossel_corretor as cb
    from app.services import carrossel_copies as cc

    agora = agora or datetime.now(timezone.utc)
    desde = agora - timedelta(days=dias)

    marco = _ultimo_marco(session)
    marco_em: datetime | None = getattr(marco, "criado_em", None)
    if marco_em is not None and marco_em.tzinfo is None:
        marco_em = marco_em.replace(tzinfo=timezone.utc)
    if marco_em is not None and marco_em > desde:
        desde = marco_em

    try:
        linhas = cb.placar(session, dias=dias, tenant_id=tenant_id, desde=desde,
                           por_versao=True)
    except Exception as e:  # noqa: BLE001 — tela sem número é ruim; tela caída é pior
        log.warning("placar_carrossel_indisponivel", erro=str(e)[:140])
        linhas = []

    # A MEDIÇÃO NÃO MISTURA DOIS TEXTOS (05/10): cada variante soma só os
    # disparos que saíram com o texto que vale AGORA (`copy_versao`). O que
    # saiu com outro texto da mesma variante vai pra `textos_anteriores` — não
    # some, só não entra na taxa da copy de hoje. Disparo sem versão gravada é
    # anterior à edição pela tela: saiu com o texto do código.
    do_carrossel, _copies = _variantes_do_carrossel()
    versao_de_agora = {v["id"]: v["texto_versao"] for v in do_carrossel}
    versao_do_codigo = {
        v.id: cc.versao_do_texto(cc.texto_padrao(v.espelha or v.id) or v.texto)
        for v in cc.VARIANTES
    }
    de_agora: list[Any] = []
    anteriores: dict[str, dict[str, int]] = {}
    for linha in linhas:
        vid = str(linha["variante"])
        if vid in versao_de_agora:
            versao = linha.get("copy_versao") or versao_do_codigo.get(vid)
            if versao != versao_de_agora[vid]:
                a = anteriores.setdefault(vid, {"enviados": 0, "respostas": 0})
                a["enviados"] += int(linha["enviados"] or 0)
                a["respostas"] += int(linha["respostas"] or 0)
                continue
        de_agora.append(linha)
    linhas = de_agora

    # O que ficou FORA da taxa (05/10/2026): recusa, pedido de parar, resposta
    # automática do aparelho do lead e resposta sem sinal. Vai em números
    # separados pra a tela mostrar sem somar em "responderam".
    fora = ("recusas", "pararam", "automaticas", "indefinidas", "numeros_errados")
    # `botoes` não é fora da taxa: é a parte de `respostas` que veio do clique.
    # Acompanha as outras contagens só pra a tela separar interesse de botão.
    contagens = (*fora, "botoes")

    por_variante: dict[str, dict[str, Any]] = {}
    for linha in linhas:
        alvo = por_variante.setdefault(
            str(linha["variante"]),
            {"enviados": 0, "respostas": 0, "por_categoria": [],
             **{c: 0 for c in contagens}},
        )
        alvo["enviados"] += int(linha["enviados"] or 0)
        alvo["respostas"] += int(linha["respostas"] or 0)
        for c in contagens:
            alvo[c] += int(linha.get(c) or 0)
        alvo["por_categoria"].append({
            "categoria": str(linha["categoria"]),
            "enviados": int(linha["enviados"] or 0),
            "respostas": int(linha["respostas"] or 0),
        })

    def _com_taxa(enviados: int, respostas: int) -> dict[str, Any]:
        return {
            "enviados": enviados,
            "respostas": respostas,
            "taxa": round(respostas / enviados * 100, 1) if enviados else None,
            "sem_dado": enviados == 0,
            "amostra_pequena": 0 < enviados < AMOSTRA_MINIMA,
        }

    variantes: list[dict[str, Any]] = []
    for v in do_carrossel:
        medido = por_variante.pop(v["id"], None) or {
            "enviados": 0, "respostas": 0, "por_categoria": [],
            **{c: 0 for c in contagens},
        }
        antes = anteriores.get(v["id"])
        variantes.append({
            **v,
            "peso_personalizado": v["peso"] != v["peso_padrao"],
            "por_categoria": sorted(
                medido["por_categoria"], key=lambda c: -c["enviados"]
            ),
            **_com_taxa(int(medido["enviados"]), int(medido["respostas"])),
            "fora_da_taxa": {c: int(medido[c]) for c in fora},
            "respostas_por_tipo": _por_tipo(int(medido["respostas"]), medido),
            # O que esta variante mediu com OUTRO texto, no mesmo período.
            # None = nunca teve outro texto no período.
            "textos_anteriores": (
                _com_taxa(antes["enviados"], antes["respostas"]) if antes else None
            ),
        })

    # O que sobrou não é variante de hoje: disparo anterior ao teste A/B
    # ("antes-do-teste") ou de variante já removida do código. Fica à parte —
    # somar no placar faria comparar copy que ninguém pode mais escolher.
    fora_do_teste = [
        {"variante": vid, **_com_taxa(int(d["enviados"]), int(d["respostas"]))}
        for vid, d in sorted(por_variante.items())
    ]

    # Total = só o que está NO TESTE de hoje, pra a soma fechar com a tabela.
    total_env = sum(int(v["enviados"]) for v in variantes)
    total_resp = sum(int(v["respostas"]) for v in variantes)
    total_fora = {
        c: sum(int(v["fora_da_taxa"][c]) for v in variantes) for c in fora
    }
    total_botoes = sum(int(v["respostas_por_tipo"]["botao"]) for v in variantes)

    try:
        estoque = _estoque_carrossel(session)
    except Exception as e:  # noqa: BLE001
        log.warning("estoque_carrossel_indisponivel", erro=str(e)[:140])
        session.rollback()
        estoque = {"fila": None, "disparado": None, "respondeu": None}

    return {
        "dias": dias,
        "desde": desde.isoformat(),
        "ate": agora.isoformat(),
        "medindo_desde": marco_em.isoformat() if marco_em else None,
        "motivo_do_marco": getattr(marco, "motivo", None),
        "marco_por": getattr(marco, "criado_por", None),
        "amostra_minima": AMOSTRA_MINIMA,
        "cota_marketing_pct": cc.COTA_MARKETING_PCT,
        "variantes": variantes,
        "copies": _copies,
        "fora_do_teste": fora_do_teste,
        "estoque": estoque,
        "total": {**_com_taxa(total_env, total_resp), "fora_da_taxa": total_fora,
                  "respostas_por_tipo": _por_tipo(
                      total_resp, {**total_fora, "botoes": total_botoes})},
        "definicao": (
            "enviados = disparos do carrossel que SAÍRAM na janela; respostas = "
            "leads que mostraram interesse ou clicaram no botão DEPOIS do último "
            "disparo (um lead que manda três mensagens conta uma vez). Quem "
            "disse não, quem pediu pra parar, a resposta automática do WhatsApp "
            "do lead e a resposta sem sinal ('bom dia', áudio) ficam em "
            "fora_da_taxa; taxa = respostas ÷ enviados, e "
            f"None quando ninguém recebeu — 'sem dado' não é 0%. Abaixo de "
            f"{AMOSTRA_MINIMA} envios a taxa é ruído e vem marcada. A CATEGORIA "
            "é a que saiu de verdade, não a pretendida: variante de marketing "
            "cai em UTILITY quando a cota do dia fecha ou não há template "
            "MARKETING aprovado. Cada variante soma só o que saiu com o texto "
            "que vale agora: mudar o texto começa uma medição nova, e o que foi "
            "medido com o texto de antes fica à parte."
        ),
    }


def _por_tipo(interessados: int, fora: dict[str, Any]) -> dict[str, int]:
    """Respostas por tipo, do jeito que a tela pede (`PENDENCIAS-BACKEND.md`,
    item 3): quem quis saber mais, quem pediu pra parar, quem respondeu outra
    coisa. `outro` soma quem disse não e quem respondeu sem sinal; a resposta
    automática do aparelho fica à parte — não é gente."""
    recusa = int(fora.get("recusas") or 0)
    indef = int(fora.get("indefinidas") or 0)
    errado = int(fora.get("numeros_errados") or 0)
    # `interessado` é o que entra na taxa (interesse + botão). Os dois
    # separados: `botao` é o clique; `interesse` é quem escreveu (e a resposta
    # antiga, sem classe gravada, que sempre contou aqui).
    botao = min(int(fora.get("botoes") or 0), int(interessados))
    return {
        "interessado": int(interessados),
        "interesse": int(interessados) - botao,
        "botao": botao,
        "parar": int(fora.get("pararam") or 0),
        "outro": recusa + indef + errado,
        "disse_nao": recusa,
        "sem_sinal": indef,
        "numero_errado": errado,
        "automatica": int(fora.get("automaticas") or 0),
    }


@router.get("/automacoes/remarketing/conferencia")
def conferencia_remarketing(
    dias: int = 14,
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """O fechamento de cada dia do remarketing, gravado pelo conferente
    (`remarketing_conferente`), mais as reservas sem disparo. Admin-only.
    Só lê: quem confere é o timer."""
    from app.api.painel import _identificar_usuario, _require_admin
    from app.services import remarketing_conferente as conf

    user = _identificar_usuario(x_painel_user)
    _require_admin(user)
    if dias < 1 or dias > 90:
        raise HTTPException(status_code=422, detail="dias fora da faixa (1 a 90)")
    try:
        with SessionLocal() as s:
            return {
                "gerado_em": datetime.now(timezone.utc).isoformat(),
                "dias": conf.ultimas(s, DEFAULT_TENANT_ID_METRICAS, dias),
                "reservas_orfas": conf.reservas_orfas(s, DEFAULT_TENANT_ID_METRICAS),
                "alarmes_possiveis": conf.ALARMES,
            }
    except Exception as e:  # noqa: BLE001
        raise HTTPException(503, f"não consegui ler a conferência: {str(e)[:140]}") from None


class AgendaPatch(BaseModel):
    """O "salvar" da agenda do remarketing, inteiro. Campo ausente não muda;
    campo `null` volta ao padrão."""

    model_config = {"extra": "forbid"}

    remarketing_janela_horario: Any = None
    remarketing_dias_semana: Any = None
    remarketing_cap_diario: Any = None
    remarketing_cap_semanal: Any = None
    remarketing_etapa: Any = None


@router.put("/automacoes/remarketing/agenda")
def gravar_agenda_remarketing(
    patch: AgendaPatch,
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> Any:
    """Grava horário, dias, tetos e etapa NUMA transação: ou entra tudo, ou
    nada. Valida todos os campos antes de escrever o primeiro.

    Campo a campo (`PUT /automacoes/config/{chave}`), o robô podia ler o
    horário novo com os tetos antigos no meio do salvamento, e a falha de um
    campo deixava a agenda pela metade. A rota antiga continua valendo.
    """
    from app.api.painel import _identificar_usuario, _require_admin

    user = _identificar_usuario(x_painel_user)
    _require_admin(user)
    enviados = patch.model_dump(exclude_unset=True)
    if not enviados:
        raise HTTPException(422, "nenhum campo enviado")
    # Valida TODOS antes de escrever o primeiro, e diz qual campo errou: a
    # tela mostra a recusa ao lado do campo. `detail` segue sendo o primeiro
    # motivo, em texto, como em toda rota de configuração.
    valores: dict[str, Any] = {}
    erros: dict[str, str] = {}
    for chave, valor in enviados.items():
        try:
            valores[chave] = _validar_config(chave, valor)
        except HTTPException as e:
            if e.status_code != 422:
                raise
            erros[chave] = str(e.detail)
    if erros:
        return JSONResponse(status_code=422, content={
            "detail": next(iter(erros.values())), "erros": erros, "gravadas": []})
    config_operacional.definir_varias(valores, autor=user)
    return {"ok": True, "gravadas": sorted(valores),
            "config": config_operacional.estado_atual()}


@router.get("/automacoes/remarketing/metricas")
def metricas_remarketing(
    dias: int = 30,
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Placar do teste A/B do carrossel da Corretor (últimos `dias`). Admin-only."""
    from app.api.painel import _identificar_usuario, _require_admin

    user = _identificar_usuario(x_painel_user)
    _require_admin(user)

    if dias < 1 or dias > METRICAS_DIAS_MAX:
        raise HTTPException(
            status_code=422, detail=f"dias fora da faixa (1 a {METRICAS_DIAS_MAX})"
        )

    with SessionLocal() as s:
        dados = calcular_metricas_remarketing(s, dias=dias)
    return {"gerado_em": datetime.now(timezone.utc).isoformat(), **dados}


def _gravar_varias_ou_422(enviados: dict[str, Any], user: str) -> Any:
    """Valida TODAS as chaves antes de escrever a primeira e grava numa
    transação só. Erro volta por chave (`erros`), pra tela mostrar ao lado do
    campo. `None` apaga a linha (a chave volta ao padrão)."""
    valores: dict[str, Any] = {}
    erros: dict[str, str] = {}
    for chave, valor in enviados.items():
        try:
            valores[chave] = None if valor is None else _validar_config(chave, valor)
        except HTTPException as e:
            if e.status_code != 422:
                raise
            erros[chave] = str(e.detail)
    if erros:
        return JSONResponse(status_code=422, content={
            "detail": next(iter(erros.values())), "erros": erros, "gravadas": []})
    config_operacional.definir_varias(valores, autor=user)
    return {"ok": True, "gravadas": sorted(valores),
            "config": config_operacional.estado_atual()}


class CopyPatch(BaseModel):
    """O "salvar" do texto das variantes. `textos`: variante → texto novo
    (`null` ou vazio = voltar ao texto do código). `espelho`: a variante de
    marketing segue (true) ou não (false) o texto da `direta`."""

    model_config = {"extra": "forbid"}

    textos: dict[str, Any] | None = None
    espelho: Any = None


@router.put("/automacoes/remarketing/copy")
def gravar_copy_remarketing(
    patch: CopyPatch,
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> Any:
    """Grava o texto de uma ou mais variantes (e o espelho) NUMA transação.

    O erro volta POR VARIANTE (`erros: {"direta": "..."}`), pra tela mostrar
    ao lado do campo. Texto que não passa na validação nunca é gravado. O
    histórico (`config_operacional_historico`) guarda autor, antes e depois;
    "voltar ao padrão" não apaga nada dele.

    Não reaprova template nem muda categoria: o texto vai na VARIÁVEL de um
    template UTILITY que já está aprovado.
    """
    from app.api.painel import _identificar_usuario, _require_admin
    from app.services import carrossel_copies as cc

    user = _identificar_usuario(x_painel_user)
    _require_admin(user)
    enviados = patch.model_dump(exclude_unset=True)
    if not enviados or (not enviados.get("textos") and enviados.get("espelho") is None):
        raise HTTPException(422, "nenhum campo enviado")

    erros: dict[str, str] = {}
    valores: dict[str, Any] = {}

    if enviados.get("espelho") is not None:
        try:
            valores[cc.CHAVE_ESPELHO] = cc.validar_espelho(enviados["espelho"])
        except ValueError as e:
            erros["espelho"] = str(e)

    if enviados.get("textos"):
        # Parte do que já está gravado (lido direto do banco, não do cache) e
        # troca só as variantes enviadas.
        try:
            config_operacional.recarregar_estrito()
            atual = config_operacional.get(cc.CHAVE_COPIES)
        except config_operacional.ConfigIndisponivel as e:
            raise HTTPException(503, f"não consegui ler a copy gravada: {str(e)[:120]}") from None
        novo = dict(atual) if isinstance(atual, dict) else {}
        conhecidas = {v.id: v for v in cc.VARIANTES}
        for vid, texto in enviados["textos"].items():
            if vid not in conhecidas:
                erros[vid] = f"variante desconhecida: {vid!r}"
                continue
            try:
                limpo = cc.validar_copies({vid: texto})
            except ValueError as e:
                erros[vid] = str(e)
                continue
            if vid in limpo:
                novo[vid] = limpo[vid]
            else:
                novo.pop(vid, None)
        if not erros:
            # O que sobrou do gravado antigo também passa: se alguém escreveu
            # por fora, o salvar da tela não regrava lixo junto.
            try:
                valores[cc.CHAVE_COPIES] = cc.validar_copies(novo) or None
            except ValueError as e:
                erros["textos"] = f"o que já estava gravado não serve: {e}"

    if erros:
        return JSONResponse(status_code=422, content={
            "detail": next(iter(erros.values())), "erros": erros, "gravadas": []})
    config_operacional.definir_varias(valores, autor=user)
    log.info("remarketing_copy_gravada", autor=user, chaves=sorted(valores),
             variantes=sorted((enviados.get("textos") or {}).keys()))
    variantes, copies = _variantes_do_carrossel()
    return {"ok": True, "gravadas": sorted(valores), "variantes": variantes,
            "copies": copies}


class QuandoRespondePatch(BaseModel):
    """O "salvar" do bloco "Quando a pessoa responde", inteiro. Campo ausente
    não muda; campo `null` volta ao padrão."""

    model_config = {"extra": "forbid"}

    handoff_remarketing_quem_vai: Any = None
    handoff_remarketing_etapa: Any = None
    handoff_remarketing_roleta: Any = None
    handoff_remarketing_avisa_lead: Any = None
    handoff_remarketing_aviso_texto: Any = None
    handoff_remarketing_aviso_resumo: Any = None
    handoff_remarketing_respeita_humano: Any = None
    ia_resumo_card: Any = None
    ia_opina_indefinida: Any = None
    ia_opina_corte: Any = None


@router.put("/automacoes/remarketing/quando-responde")
def gravar_quando_responde(
    patch: QuandoRespondePatch,
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> Any:
    """Grava quem vai pra corretora, a etapa de entrada, a roleta do
    remarketing e o aviso NUMA transação: ou entra tudo, ou nada. O handoff lê
    as mesmas chaves, do banco, na transferência seguinte.

    Não envia mensagem, não mexe na roleta do Meta e não transfere ninguém que
    já esteja em Respondeu.
    """
    from app.api.painel import _identificar_usuario, _require_admin

    user = _identificar_usuario(x_painel_user)
    _require_admin(user)
    enviados = patch.model_dump(exclude_unset=True)
    if not enviados:
        raise HTTPException(422, "nenhum campo enviado")
    # A roleta e o aviso gravam `[]` e `false` de verdade: `null` aqui é
    # "voltar ao padrão", e o padrão da roleta ([1, 2, 3] do .env) não é vazio.
    r = _gravar_varias_ou_422(enviados, user)
    if isinstance(r, dict):
        log.info("remarketing_quando_responde_gravado", autor=user,
                 chaves=r["gravadas"])
        r["responde"] = _quando_responde_da_tela()
    return r


# ── Painel de controle da operação ───────────────────────────────────────────
#
# O que torna isso produto e não painel: ligar/desligar a automação, mexer no
# limite diário de uma linha e escolher quem entra na roleta deixam de ser
# edição de `.env` + restart (ou seja, deixam de passar por mim) e viram
# controle na tela.
#
# Toda chave tem `padrao` do `.env` e botão de voltar pra ele — configuração sem
# caminho de volta é armadilha: um número errado às 18h de sexta vira fim de
# semana com a operação parada.


class ConfigPatch(BaseModel):
    valor: Any


@router.get("/automacoes/config")
def ler_config(
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Estado de cada chave: valor efetivo, se veio do painel, e o padrão."""
    from app.api.painel import _identificar_usuario, _require_admin

    user = _identificar_usuario(x_painel_user)
    _require_admin(user)
    # As corretoras vão junto porque a tela monta os controles em cima delas
    # (limite de cada uma, quem entra na roleta) — e buscar nome em outro
    # endpoint só pra desenhar um checkbox é ida e volta à toa.
    from app.api.painel import CORRETORA_LABEL

    with SessionLocal() as s:
        linhas = s.execute(
            sa_text(
                "SELECT id, nome FROM corretoras WHERE ativo = TRUE "
                "AND coalesce(papel,'corretora') LIKE 'corretora%' ORDER BY id"
            )
        ).all()
    corretoras = [
        {"id": int(r[0]), "nome": str(r[1]),
         "label": CORRETORA_LABEL.get(int(r[0]), str(r[1]).split(" ")[0])}
        for r in linhas
    ]
    return {"config": config_operacional.estado_atual(), "corretoras": corretoras}


@router.put("/automacoes/config/{chave}")
def gravar_config(
    chave: str,
    patch: ConfigPatch,
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    from app.api.painel import _identificar_usuario, _require_admin

    user = _identificar_usuario(x_painel_user)
    _require_admin(user)

    valor = _validar_config(chave, patch.valor)
    try:
        config_operacional.definir(chave, valor, autor=user)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return {"ok": True, "chave": chave, "config": config_operacional.estado_atual()}


@router.delete("/automacoes/config/{chave}")
def restaurar_config(
    chave: str,
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Volta a chave pro padrão do `.env`."""
    from app.api.painel import _identificar_usuario, _require_admin

    user = _identificar_usuario(x_painel_user)
    _require_admin(user)
    try:
        config_operacional.restaurar_padrao(chave, autor=user)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return {"ok": True, "chave": chave, "config": config_operacional.estado_atual()}


class PapelPatch(BaseModel):
    papel: str
    # Com papel="disparo": pausa, na MESMA transação, os outros números do
    # cliente que estavam como disparo — trocar de disparador num pedido só.
    pausar_os_outros: bool = False


@router.put("/automacoes/remarketing/linhas/{phone_number_id}/papel")
def trocar_papel_da_linha(
    phone_number_id: str,
    patch: PapelPatch,
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Troca o papel (disparo / atendimento / pausado) de um número do cliente.

    O dono do dado é o api-disparos (`phone_numbers.papel`); aqui é só a ponte
    com o login do painel. A validação é DELE — papel aceito, número que é
    deste cliente — e o motivo da recusa volta como veio.

    Não envia nada e não liga o disparo: quem liga é o interruptor.
    """
    from app.api.painel import _identificar_usuario, _require_admin
    from app.services import linha_disparadora

    user = _identificar_usuario(x_painel_user)
    _require_admin(user)
    if not phone_number_id.isdigit():
        raise HTTPException(422, "número inválido")
    try:
        r = linha_disparadora.trocar_papel(
            phone_number_id, patch.papel, autor=user,
            pausar_os_outros=patch.pausar_os_outros,
        )
    except linha_disparadora.Indisponivel as e:
        raise HTTPException(502, f"não consegui falar com o cadastro de números: {e}") from None
    if r["status"] != 200:
        detalhe = (r["corpo"] or {}).get("detail") or f"HTTP {r['status']}"
        raise HTTPException(r["status"] if r["status"] in (404, 422) else 502,
                            str(detalhe))
    log.info("remarketing_papel_da_linha_trocado", autor=user,
             numero=phone_number_id[-4:], papel=patch.papel)
    return {"ok": True, "linha_disparadora": linha_disparadora.resumo()}


# Teto de segurança do cap por linha. Não é palpite: a Cloud API começa em 250
# conversas/dia e número frio que salta pro teto perde qualidade e leva bloqueio
# — foi o que quase custou o 0000, que é o telefone DA CASA. Quem quiser passar
# disso mexe no `.env`, com a decisão na mão de quem conhece a consequência.
CAP_MAXIMO = 250


def _validar_config(chave: str, valor: Any) -> Any:
    """Recusa o que quebraria a operação. Erro de digitação em campo de
    configuração não pode virar mensagem a mais na linha do cliente."""
    if chave == "cadencia_ativa":
        if not isinstance(valor, bool):
            raise HTTPException(422, "cadencia_ativa precisa ser true ou false")
        return valor

    # Kill-switch próprio da nutrição (separado da cadência em 05/08).
    if chave == "nutricao_ativa":
        if not isinstance(valor, bool):
            raise HTTPException(422, "nutricao_ativa precisa ser true ou false")
        return valor

    # Por qual linha sai a nutrição (07/10): da dona do lead ou toda pela Pessoa12.
    if chave == "nutricao_pela_linha_da_dona":
        if not isinstance(valor, bool):
            raise HTTPException(422, "nutricao_pela_linha_da_dona precisa ser true ou false")
        return valor

    if chave == "cadencia_cap_diario":
        if not isinstance(valor, int) or isinstance(valor, bool):
            raise HTTPException(422, "cap diário precisa ser número inteiro")
        if not (0 <= valor <= CAP_MAXIMO):
            raise HTTPException(422, f"cap diário fora da faixa (0 a {CAP_MAXIMO})")
        return valor

    if chave == "cadencia_cap_por_corretora":
        if not isinstance(valor, dict):
            raise HTTPException(422, "esperado um mapa corretora → limite")
        out: dict[str, int] = {}
        for k, v in valor.items():
            try:
                cid = int(k)
            except (TypeError, ValueError):
                raise HTTPException(422, f"corretora inválida: {k!r}") from None
            if not isinstance(v, int) or isinstance(v, bool) or not (0 <= v <= CAP_MAXIMO):
                raise HTTPException(422, f"limite inválido pra corretora {cid}")
            out[str(cid)] = v
        return out

    if chave == "roleta_corretoras":
        if not isinstance(valor, list):
            raise HTTPException(422, "esperado uma lista de corretoras, na ordem")
        ids: list[int] = []
        for x in valor:
            try:
                ids.append(int(x))
            except (TypeError, ValueError):
                raise HTTPException(422, f"corretora inválida: {x!r}") from None
        if len(set(ids)) != len(ids):
            raise HTTPException(422, "corretora repetida na roleta")
        # Roleta vazia = lead novo nasce sem dono e some do CRM de todo mundo
        # (leads.owner_id não tem chave estrangeira). Recusa explícita.
        if not ids:
            raise HTTPException(422, "a roleta não pode ficar vazia")
        with SessionLocal() as s:
            existentes = {
                int(r[0])
                for r in s.execute(
                    sa_text("SELECT id FROM corretoras WHERE ativo = TRUE")
                ).all()
            }
        invalidas = [i for i in ids if i not in existentes]
        if invalidas:
            raise HTTPException(
                422, f"corretora inexistente ou inativa na roleta: {invalidas}"
            )
        return ids

    if chave == "carrossel_corretor_ativo":
        if not isinstance(valor, bool):
            raise HTTPException(422, "carrossel_corretor_ativo precisa ser true ou false")
        return valor

    # ── Agenda do remarketing (05/10): quando e quanto o carrossel dispara ──
    # A validação mora em `remarketing_agenda` porque o tick lê pelos MESMOS
    # parsers — um formato aceito aqui e ilegível lá viraria disparo segurado
    # sem ninguém entender por quê.
    _AGENDA = {
        "remarketing_janela_horario": "validar_janela",
        "remarketing_dias_semana": "validar_dias",
        "remarketing_cap_diario": "validar_cap_diario",
        "remarketing_cap_semanal": "validar_cap_semanal",
    }
    if chave in _AGENDA:
        from app.services import remarketing_agenda as agenda

        try:
            return getattr(agenda, _AGENDA[chave])(valor)
        except ValueError as e:
            raise HTTPException(422, str(e)) from None

    # ── Alvo do remarketing (05/10): de qual etapa e de qual dono ──
    # Confere NO BANCO que a etapa é deste cliente (e não é de dentro da roda)
    # e que o dono é corretora ativa dele. O tick confere de novo ao ler.
    if chave in ("remarketing_etapa", "remarketing_dono"):
        from app.services import remarketing_alvo

        validar = (remarketing_alvo.validar_etapa if chave == "remarketing_etapa"
                   else remarketing_alvo.validar_dono)
        try:
            return validar(valor, tenant_id=DEFAULT_TENANT_ID_METRICAS)
        except ValueError as e:
            raise HTTPException(422, str(e)) from None

    if chave == "remarketing_etapa_parar":
        from app.services import remarketing_alvo

        try:
            return remarketing_alvo.validar_etapa_parar(
                valor, tenant_id=DEFAULT_TENANT_ID_METRICAS)
        except ValueError as e:
            raise HTTPException(422, str(e)) from None

    if chave == "remarketing_conferente_avisa_telegram":
        if not isinstance(valor, bool):
            raise HTTPException(422, "o aviso do conferente precisa ser true ou false")
        return valor

    if chave == "carrossel_auto_resposta_segundos":
        # bool é int em Python: `true` viraria 1 segundo sem ninguém pedir.
        if isinstance(valor, bool) or not isinstance(valor, (int, float)):
            raise HTTPException(422, "o corte precisa ser um número de segundos")
        if not 0 <= valor <= 60:
            raise HTTPException(
                422, "o corte fica entre 0 e 60 segundos (0 desliga a regra do relógio)"
            )
        return valor

    # ── Texto das variantes (05/10) ──
    if chave == "carrossel_copies":
        from app.services import carrossel_copies as cc

        try:
            return cc.validar_copies(valor)
        except ValueError as e:
            raise HTTPException(422, str(e)) from None

    if chave == "carrossel_marketing_espelha_direta":
        from app.services import carrossel_copies as cc

        try:
            return cc.validar_espelho(valor)
        except ValueError as e:
            raise HTTPException(422, str(e)) from None

    # ── Quando a pessoa responde (05/10) ──
    # Validação em `quando_responde`, que é por onde o handoff lê: formato
    # aceito aqui e recusado lá viraria lead parado em Respondeu sem motivo.
    if chave.startswith("handoff_remarketing_") and chave in (
        "handoff_remarketing_quem_vai", "handoff_remarketing_etapa",
        "handoff_remarketing_roleta", "handoff_remarketing_avisa_lead",
        "handoff_remarketing_aviso_texto", "handoff_remarketing_aviso_resumo",
        "handoff_remarketing_respeita_humano",
    ):
        from app.services import quando_responde as qr

        try:
            if chave == qr.CHAVE_QUEM_VAI:
                return qr.validar_quem_vai(valor)
            if chave == qr.CHAVE_ETAPA:
                return qr.validar_etapa(valor, tenant_id=DEFAULT_TENANT_ID_METRICAS)
            if chave == qr.CHAVE_ROLETA:
                return qr.validar_roleta(valor, tenant_id=DEFAULT_TENANT_ID_METRICAS)
            if chave == qr.CHAVE_AVISO_TEXTO:
                return qr.validar_aviso_texto(valor)
            if not isinstance(valor, bool):
                raise ValueError("precisa ser true ou false")
            return valor
        except ValueError as e:
            raise HTTPException(422, str(e)) from None

    # ── IA do lado (05/10) ──
    if chave in ("ia_resumo_card", "ia_opina_indefinida"):
        if not isinstance(valor, bool):
            raise HTTPException(422, "precisa ser true ou false")
        return valor
    if chave == "ia_opina_corte":
        from app.services import ia_do_lado

        try:
            return ia_do_lado.validar_corte(valor)
        except ValueError as e:
            raise HTTPException(422, str(e)) from None

    if chave == "carrossel_pesos":
        # As DUAS travas do teste A/B moram aqui (e só aqui): soma 100 e cota
        # de MARKETING em 20%. É este o caminho que a tela usa pra gravar — se
        # a validação ficasse no JavaScript, um curl bastaria pra furar a cota
        # sem que nenhum teste do backend reclamasse.
        from app.services import carrossel_copies as cc

        try:
            return cc.validar_pesos(valor)
        except ValueError as e:
            raise HTTPException(422, str(e)) from None

    # ── Boas-vindas (automação separada da cadência) ──
    if chave == "lead_intake_ativo":
        if not isinstance(valor, bool):
            raise HTTPException(422, "o interruptor precisa ser true ou false")
        return valor

    if chave == "boas_vindas_copies":
        from app.services import boas_vindas_copies as bvc

        try:
            return bvc.validar_copies(valor)
        except ValueError as e:
            raise HTTPException(422, str(e)) from None

    if chave == "boas_vindas_pesos":
        from app.services import boas_vindas_copies as bvc

        try:
            return bvc.validar_pesos(valor)
        except ValueError as e:
            raise HTTPException(422, str(e)) from None

    # ── Interruptor por corretora do follow-up e da nutrição (06/10) ──
    from app.services import automacao_por_corretora as apc

    if chave in apc.CHAVES:
        try:
            with SessionLocal() as s:
                from app.services import painel_automacoes as pa

                ativas = {c["id"] for c in pa.corretoras_ativas(s)}
            return apc.validar(valor, ativas)
        except ValueError as e:
            raise HTTPException(422, str(e)) from None

    raise HTTPException(404, f"chave desconhecida: {chave}")


# ── Interruptor POR CORRETORA (06/10/2026) ───────────────────────────────────
#
# Um clique = uma corretora. Grava só o item dela no mapa (sem pisar no clique
# de outra corretora ao mesmo tempo), com autor no histórico, e devolve o que
# ficou gravado LIDO DE NOVO do banco — é isso que a tela mostra, não o que ela
# mandou. O motor relê a mesma linha antes de cada envio.


class CorretoraLigadaPatch(BaseModel):
    model_config = {"extra": "forbid"}

    ligada: Any


def gravar_interruptor_da_corretora(chave: str, corretora_id: int, ligada: Any,
                                    user: str) -> dict[str, Any]:
    from app.services import automacao_por_corretora as apc
    from app.services import painel_automacoes as pa

    if not isinstance(ligada, bool):
        raise HTTPException(422, "ligada precisa ser true ou false")
    with SessionLocal() as s:
        ativas = {c["id"] for c in pa.corretoras_ativas(s)}
    if corretora_id not in ativas:
        raise HTTPException(404, f"corretora inexistente ou inativa: {corretora_id}")
    antes = apc.estado(chave)
    if antes.get("ilegivel"):
        # Gravar por cima de UMA corretora deixaria o resto torto e o motor
        # segurando todas sem a tela explicar. Conserto = voltar ao padrão.
        raise HTTPException(409, (
            f"{chave}: o valor gravado está ilegível ({antes.get('motivo')}) — "
            "volte a chave ao padrão antes de mexer numa corretora"))
    try:
        config_operacional.definir_item(chave, str(corretora_id), ligada, autor=user)
    except config_operacional.ValorIlegivel as e:
        raise HTTPException(
            409, f"{e} — volte a chave ao padrão antes de mexer numa corretora"
        ) from None
    depois = apc.estado(chave)
    confirmado = apc.ligada_na_tela(depois, corretora_id)
    log.info("automacao_corretora_gravada", chave=chave, corretora_id=corretora_id,
             ligada=ligada, autor=user, confirmado=confirmado)
    return {
        "ok": confirmado is ligada,
        "chave": chave,
        "corretora_id": corretora_id,
        "pedido": ligada,
        # O que o banco tem AGORA (relido). Diferente do pedido = não confie.
        "gravado": confirmado,
        "antes": antes.get("mapa"),
        "depois": depois.get("mapa"),
    }


@router.put("/automacoes/follow-up/corretoras/{corretora_id}")
def gravar_follow_up_da_corretora(
    corretora_id: int,
    patch: CorretoraLigadaPatch,
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Liga/desliga o follow-up de UMA corretora. Não envia nada; o geral
    (`cadencia_ativa`) desligado continua vencendo."""
    from app.api.painel import _identificar_usuario, _require_admin
    from app.services import automacao_por_corretora as apc
    from app.services import painel_automacoes as pa

    user = _identificar_usuario(x_painel_user)
    _require_admin(user)
    r = gravar_interruptor_da_corretora(apc.CHAVE_FOLLOWUP, corretora_id,
                                        patch.ligada, user)
    with SessionLocal() as s:
        r["follow_up"] = pa.bloco_follow_up(s)
    return r


@router.get("/automacoes/linhas")
def linhas_das_automacoes(
    forcar: bool = False,
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Saúde de TODAS as linhas do cliente num endpoint só: dona, número,
    papel, qualidade na Meta e recusa por cobrança. Cache de 60 s (a saúde da
    Meta tem o dela, de 5 min por linha). `forcar=true` relê o cadastro e o
    papel agora (a saúde continua no cache de 5 min da Meta)."""
    from app.api.painel import _identificar_usuario, _require_admin
    from app.services import painel_automacoes as pa

    user = _identificar_usuario(x_painel_user)
    _require_admin(user)
    return {"gerado_em": datetime.now(timezone.utc).isoformat(),
            **pa.todas_as_linhas(forcar=forcar)}
