"""
AgenteAssistente — secretária da Acme Imóveis (Onda 2).

Refatorada pra usar `app.llm.cli_provider` (Claude CLI via plano Max) em vez
do SDK Anthropic. Loop conversacional com tool-calling via JSON estruturado.

Contexto da chamada (sender_kind, sender_id, sender_nome, sender_papel,
history) entra no system prompt em runtime — SOUL.md fica intocado.

Tools que afetam estado têm trava de confirmação estruturada:
  marcar_visita / registrar_venda
quando confirmado=False → retorna {"status":"needs_confirmation", ...}.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import structlog

from app.config import get_llm_provider, settings

log = structlog.get_logger()

ASSISTENTE_DIR = Path(__file__).parent
SOUL_PATH = ASSISTENTE_DIR / "SOUL.md"
SKILLS_DIR = ASSISTENTE_DIR / "skills"
SP_TZ = ZoneInfo("America/Sao_Paulo")

MAX_TOOL_ITERATIONS = 5

# Frases em que ela ANUNCIA a busca em vez de fazer. Como o turno acaba no
# `return`, isso vira promessa que ninguém cumpre — a Dona respondia "Eai" só
# pra empurrar o próximo passo. Medido no telegram_chat_log de 05-10/08/2026.
_PADROES_PROMESSA = (
    "vou puxar", "vou pegar", "vou buscar", "vou conferir", "vou checar",
    "vou olhar", "vou ver ", "vou procurar", "vou verificar", "vou consultar",
    "deixa eu conferir", "deixa eu ver", "deixa eu puxar", "deixa eu buscar",
    "deixa eu checar", "já te trago", "ja te trago", "já volto", "ja volto",
    "te respondo em", "me dá um minuto", "me da um minuto", "um instante",
    "tô puxando", "to puxando", "estou puxando", "aguarda", "peraí", "perai",
    "vou organizar", "vou levantar", "vou cruzar",
)


def _promete_sem_fazer(texto: str) -> bool:
    """True quando a resposta promete uma busca futura em vez de entregar."""
    t = (texto or "").lower()
    return any(p in t for p in _PADROES_PROMESSA)
RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "text": {
            "type": "string",
            "description": "Sua resposta natural pro usuário (PT-BR, tom Assistente).",
        },
        "tool_call": {
            "type": ["object", "null"],
            "description": "null se não precisar usar tool; senão {name, args}.",
            "properties": {
                "name": {
                    "type": "string",
                    "enum": [
                        "criar_visita",
                        "marcar_visita",
                        "registrar_ligacao",
                        "registrar_venda",
                        "add_note",
                        "enviar_whatsapp_corretora",
                        "consultar_visao_geral",
                        "consultar_coaching",
                        "consultar_agenda",
                        "consultar_prospeccao",
                        "listar_leads",
                        "buscar_lead",
                        "ler_conversa",
                        "ler_ficha_lead",
                        "ler_handoff_lead",
                        "ler_respostas_disparo",
                    ],
                },
                "args": {"type": "object"},
            },
            "required": ["name", "args"],
        },
    },
    "required": ["text", "tool_call"],
}


def _load_soul() -> str:
    if not SOUL_PATH.exists():
        raise FileNotFoundError(f"SOUL.md não encontrado em {SOUL_PATH}")
    return SOUL_PATH.read_text(encoding="utf-8")


def _skill_env() -> dict[str, str]:
    env = os.environ.copy()
    if settings.gateway_api_key:
        env["GATEWAY_API_KEY"] = settings.gateway_api_key
    if settings.acme_sheets_cred:
        env["ACME_SHEETS_CRED"] = settings.acme_sheets_cred
    # marcar_visita.py atualiza Postgres além do Calendar — sem isso a skill
    # só funciona quando o processo pai herda DATABASE_URL do systemd.
    env["DATABASE_URL"] = settings.database_url
    return env


def _run_skill(skill: str, script: str, args: list[str]) -> dict[str, Any]:
    script_path = SKILLS_DIR / skill / script
    if not script_path.exists():
        return {"success": False, "error": f"script não encontrado: {script_path}",
                "stdout": "", "stderr": ""}

    cmd = [sys.executable, str(script_path), *args]
    log.info("assistente_run_skill", skill=skill, script=script, cmd=shlex.join(cmd))
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60,
            env=_skill_env(), cwd=str(SKILLS_DIR / skill),
        )
        return {
            "success": result.returncode == 0,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "timeout", "stdout": "", "stderr": ""}


# ── Tool implementations (puras, sem contexto de remetente) ──────────────────


def _tool_criar_visita(corretora: str, cliente: str, data: str, hora: str,
                       telefone: str | None = None, bairro: str | None = None,
                       is_revisita: bool | None = None,
                       registrado_por_telefone: str | None = None,
                       confirmado: bool = False, **_: Any) -> dict[str, Any]:
    if not confirmado:
        try:
            data_fmt = datetime.strptime(data, "%Y-%m-%d").strftime("%d/%m/%Y")
        except ValueError:
            data_fmt = data
        extras = []
        if telefone:
            extras.append(f"tel {telefone}")
        if bairro:
            extras.append(f"bairro {bairro}")
        extra_str = f" ({', '.join(extras)})" if extras else ""

        # Detecta re-visita antes de confirmar
        check_args = ["--corretora", corretora, "--cliente", cliente,
                      "--data", data, "--hora", hora, "--check-revisita-only",
                      "--allow-past"]
        if telefone:
            check_args += ["--telefone", telefone]
        chk = _run_skill("agenda", "criar_visita.py", check_args)
        detected = False
        anterior_str = ""
        if chk.get("success"):
            try:
                payload = json.loads(chk["stdout"])
                detected = bool(payload.get("is_revisita_detectada"))
                ant = payload.get("anterior")
                if ant:
                    try:
                        ant_dt = datetime.fromisoformat(ant["data_hora"]).strftime("%d/%m")
                    except Exception:
                        ant_dt = ant.get("data_hora", "?")
                    anterior_str = f" (anterior: {ant['lead_nome']} em {ant_dt}, status={ant['status']})"
            except Exception:
                pass

        if is_revisita is None:
            is_revisita = detected

        revisita_line = ""
        if detected and is_revisita:
            revisita_line = f"\n⚠️ Detectei re-visita{anterior_str} — vou marcar com flag is_revisita=true (não conta como primeira visita)."
        elif detected and not is_revisita:
            revisita_line = f"\n⚠️ Tem visita anterior{anterior_str}, mas você pediu pra contar como primeira."

        return {
            "status": "needs_confirmation",
            "preview": (f"Vou criar visita pra {corretora}: {cliente} em "
                        f"{data_fmt} às {hora}{extra_str}.{revisita_line}\nConfirma?"),
            "action": "criar_visita",
            "params": {"corretora": corretora, "cliente": cliente,
                       "data": data, "hora": hora,
                       "telefone": telefone, "bairro": bairro,
                       "is_revisita": is_revisita},
        }
    args = ["--corretora", corretora, "--cliente", cliente,
            "--data", data, "--hora", hora]
    if telefone:
        args += ["--telefone", telefone]
    if bairro:
        args += ["--bairro", bairro]
    if is_revisita:
        args += ["--is-revisita"]
    args += ["--origem", "whatsapp"]
    if registrado_por_telefone:
        args += ["--registrado-por-telefone", registrado_por_telefone]
    out = _run_skill("agenda", "criar_visita.py", args)
    return {"status": "ok" if out["success"] else "error",
            "stdout": out.get("stdout", ""),
            "stderr": out.get("stderr", "") or out.get("error", "")}


# Sinônimos que o LLM manda na vida real → status canônico da skill.
_MARCAR_STATUS_ALIASES = {
    "compareceu": "compareceu", "apareceu": "compareceu", "foi": "compareceu",
    "realizada": "compareceu", "aconteceu": "compareceu",
    "no_show": "no_show", "no show": "no_show", "noshow": "no_show",
    "nao compareceu": "no_show", "não compareceu": "no_show",
    "nao_compareceu": "no_show", "não_compareceu": "no_show",
    "faltou": "no_show", "deu bolo": "no_show", "deu_bolo": "no_show",
    "cancelada": "cancelada", "cancelado": "cancelada", "cancelou": "cancelada",
    "desmarcou": "cancelada", "desmarcada": "cancelada",
}

_CORRETORA_NOME_DB = {
    "corretor": "Pessoa1", "corretora_b": "Pessoa1", "corretora_b": "Pessoa1",
    "corretor": "Pessoa5", "corretora_c": "Pessoa5", "corretora_c": "Pessoa5",
    "pessoa": "Pessoa3", "corretora_a": "Pessoa3", "corretora_a": "Pessoa3", "corretor": "Pessoa3",
}


def _resolver_visita_por_cliente(cliente: str, corretora: str | None) -> dict[str, Any]:
    """Acha a visita aberta (agendada/pendente) mais recente pelo nome do cliente.

    A Assistente não tem como saber evento_id — corretora fala "Kleber não
    compareceu" e cabe a nós resolver. Retorna dict com 'evento_id' ou
    'error'/'opcoes' pro agente devolver algo útil.
    """
    from sqlalchemy import text as _sql_text
    from app.db.session import SessionLocal as _SessionLocal

    nome_db = _CORRETORA_NOME_DB.get((corretora or "").strip().lower())
    sql = """
        SELECT v.id, v.evento_calendar_id, v.lead_nome, v.status,
               v.data_hora AT TIME ZONE 'America/Sao_Paulo' AS data_sp,
               c.nome AS corretora_nome
        FROM visitas v JOIN corretoras c ON c.id = v.corretora_id
        WHERE v.lead_nome ILIKE :pat
          AND v.status IN ('agendada', 'pendente_confirmacao')
          AND (CAST(:cnome AS TEXT) IS NULL OR c.nome = :cnome)
        ORDER BY v.data_hora DESC
        LIMIT 5
    """
    with _SessionLocal() as s:
        rows = s.execute(
            _sql_text(sql), {"pat": f"%{cliente.strip()}%", "cnome": nome_db}
        ).mappings().all()

    if not rows:
        return {"error": (f"Nenhuma visita aberta encontrada pra '{cliente}'"
                          + (f" da corretora {nome_db}" if nome_db else "")
                          + ". Pode já estar marcada, ou o nome tá diferente na agenda.")}
    if len(rows) > 1:
        opcoes = [f"{r['lead_nome']} — {r['data_sp']:%d/%m %H:%M} ({r['corretora_nome']})"
                  for r in rows]
        return {"error": f"Achei {len(rows)} visitas abertas pra '{cliente}'. Qual delas?",
                "opcoes": opcoes}
    r = rows[0]
    if not r["evento_calendar_id"]:
        return {"error": f"Visita de {r['lead_nome']} ({r['data_sp']:%d/%m %H:%M}) "
                         "existe no banco mas sem evento no calendário — avisa a Dona."}
    return {"evento_id": r["evento_calendar_id"], "lead_nome": r["lead_nome"],
            "data_sp": f"{r['data_sp']:%d/%m %H:%M}", "corretora": r["corretora_nome"]}


def _tool_marcar_visita(status: str, evento_id: str | None = None,
                        cliente: str | None = None, corretora: str | None = None,
                        confirmado: bool = False, **_: Any) -> dict[str, Any]:
    status_norm = _MARCAR_STATUS_ALIASES.get(
        (status or "").strip().lower().replace("-", " "))
    if not status_norm:
        return {"status": "error",
                "message": f"status '{status}' inválido — use: compareceu, no_show ou cancelada"}

    rotulo = cliente or evento_id
    if not evento_id:
        if not cliente:
            return {"status": "error", "message": "passa o nome do cliente da visita"}
        res = _resolver_visita_por_cliente(cliente, corretora)
        if "error" in res:
            return {"status": "error", "message": res["error"],
                    **({"opcoes": res["opcoes"]} if "opcoes" in res else {})}
        evento_id = res["evento_id"]
        rotulo = f"{res['lead_nome']} ({res['data_sp']}, {res['corretora']})"

    if not confirmado:
        return {
            "status": "needs_confirmation",
            "preview": f"Vou marcar a visita de {rotulo} como {status_norm}. Confirma?",
            "action": "marcar_visita",
            "params": {"evento_id": evento_id, "cliente": cliente,
                       "corretora": corretora, "status": status_norm},
        }
    out = _run_skill("agenda", "marcar_visita.py",
                     ["--event-id", evento_id, "--status", status_norm])
    return {"status": "ok" if out["success"] else "error",
            "stdout": out.get("stdout", ""),
            "stderr": out.get("stderr", "") or out.get("error", "")}


def _tool_registrar_ligacao(corretora: str, ligacoes: int, atendeu: int,
                            prosseguiu: int, semana: str | None = None,
                            confirmado: bool = False, **_: Any) -> dict[str, Any]:
    if not confirmado:
        return {
            "status": "needs_confirmation",
            "preview": (f"Vou registrar pra {corretora} na semana {semana or 'corrente'}: "
                        f"{ligacoes} ligações, {atendeu} atenderam, {prosseguiu} prosseguiram. "
                        f"Confirma?"),
            "action": "registrar_ligacao",
            "params": {"corretora": corretora, "ligacoes": ligacoes,
                       "atendeu": atendeu, "prosseguiu": prosseguiu, "semana": semana},
        }
    args = [
        "--corretora", corretora,
        "--ligacoes", str(ligacoes),
        "--atendeu", str(atendeu),
        "--prosseguiu", str(prosseguiu),
    ]
    if semana:
        args += ["--semana", semana]
    out = _run_skill("funil", "registrar_ligacoes.py", args)
    return {"status": "ok" if out["success"] else "error",
            "stdout": out.get("stdout", ""),
            "stderr": out.get("stderr", "") or out.get("error", "")}


def _tool_registrar_venda(corretora: str, cliente: str, valor: float,
                          mes: str | None = None,
                          confirmado: bool = False, **_: Any) -> dict[str, Any]:
    if not confirmado:
        valor_fmt = f"R$ {float(valor):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        return {
            "status": "needs_confirmation",
            "preview": (f"Vou registrar venda da {corretora}: {cliente}, "
                        f"{valor_fmt}, mês {mes or 'corrente'}. Confirma?"),
            "action": "registrar_venda",
            "params": {"corretora": corretora, "cliente": cliente,
                       "valor": valor, "mes": mes},
        }
    args = ["--corretora", corretora, "--cliente", cliente, "--valor", str(int(valor))]
    if mes:
        args += ["--month", mes]
    out = _run_skill("funil", "registrar_venda.py", args)
    return {"status": "ok" if out["success"] else "error",
            "stdout": out.get("stdout", ""),
            "stderr": out.get("stderr", "") or out.get("error", "")}


def _tool_add_note(contexto: str, texto: str, **_: Any) -> dict[str, Any]:
    out = _run_skill("funil", "add_note.py",
                     ["--cell", contexto, "--text", texto])
    return {"status": "ok" if out["success"] else "error",
            "stdout": out.get("stdout", ""),
            "stderr": out.get("stderr", "") or out.get("error", "")}


# Mapeamento corretora → WhatsApp pessoal (não-instância). Manter sincronizado
# com a tabela `corretoras` do Postgres + memória `feedback_evolution_instancias`.
_CORRETORA_WHATSAPP = {
    "corretor":      "+5500000000000",
    "corretora_c":   "+5500000000000",
    "corretora_c":   "+5500000000000",
    "corretor":      "+5500000000000",
    "corretora_b":"+5500000000000",
    "corretora_b":"+5500000000000",
    "corretor":        "+5500000000000",   # pessoal, NÃO o número da Acme Imóveis
    "corretora_a":  "+5500000000000",
    "pessoa":     "+5500000000000",   # alias Acme: Pessoa→Pessoa3
}


# Mapeamento corretora_id (Postgres) → login htpasswd do painel (X-Painel-User).
# Espelha USUARIO_CORRETORA de app/api/painel.py, invertido. Usado pra propagar a
# IDENTIDADE AUTENTICADA do remetente pro painel — quando o sender é corretora, o
# painel aplica sozinho o escopo/redação por corretora (fail-closed lá dentro).
# Só a dona/admin validada envia "dona" (visão total).
_CORRETORA_ID_PAINEL_USER = {1: "corretor", 2: "corretor", 3: "corretor"}


def _fetch_painel(
    endpoint: str,
    periodo: str = "mes_atual",
    _for_corretora_id: int | None = None,
) -> dict[str, Any]:
    """Chama copiloto-api local (mesmo processo) pra puxar dados do painel.

    O header X-Painel-User carrega a identidade autenticada do remetente:
    - remetente corretora → login dela (corretor/corretor/corretor) → painel devolve só os
      dados dela (escopo/redação feita no próprio endpoint, fail-closed).
    - remetente dona/admin (nenhum corretora_id injetado) → "dona" (visão total).

    FURO 2 (P0): antes mandava "dona" fixo pra QUALQUER remetente, entregando
    visão admin de todas as corretoras pro contexto de qualquer uma. Agora o
    controle está no header propagado, não só no system prompt do LLM.

    Timeout generoso (120s) porque cold-start /visao-geral pode demorar:
    busca Kommo events + tarefas atrasadas + cruzamento meta_leads. Cache
    aquece subsequentes em ~2s.
    """
    import httpx
    # Fail-closed: se veio corretora_id mas não está no mapa, NÃO cai pra "dona"
    # (evita escalonar pra admin um sender corretora desconhecido). Nesse caso
    # usa um login inexistente → painel responde 403 → tool devolve erro (nega).
    if _for_corretora_id is not None:
        painel_user = _CORRETORA_ID_PAINEL_USER.get(_for_corretora_id)
        if painel_user is None:
            return {"_error": "escopo de corretora não reconhecido — acesso negado"}
    else:
        painel_user = "dona"
    url = f"http://127.0.0.1:8002/painel/{endpoint}"
    try:
        r = httpx.get(url, params={"periodo": periodo},
                      headers={"X-Painel-User": painel_user}, timeout=120.0)
        if r.status_code != 200:
            return {"_error": f"painel http {r.status_code}"}
        return r.json()
    except Exception as e:
        return {"_error": str(e)[:200]}


def _fmt_brl(v: float | int | None) -> str:
    v = float(v or 0)
    return f"R$ {v:,.0f}".replace(",", ".")


def _tool_consultar_visao_geral(periodo: str = "mes_atual",
                                _for_corretora_id: int | None = None,
                                **_: Any) -> dict[str, Any]:
    """Resume Visão Geral do painel — funil, KPIs, atenção hoje, alertas, vazamento."""
    d = _fetch_painel("visao-geral", periodo, _for_corretora_id=_for_corretora_id)
    if d.get("_error"):
        return {"status": "error", "message": d["_error"]}
    eq = d.get("funil_equipe", {})
    cors = d.get("funil_por_corretora", [])
    meta = d.get("meta_ads", {})
    atencao = d.get("atencao_hoje", {})
    alertas = d.get("alertas", [])
    vaz = d.get("vazamento_captacao") or {}
    recup = d.get("recuperados", {})
    period_label = d.get("periodo", {}).get("label", periodo)

    linhas = [
        f"VISÃO GERAL · {period_label}",
        "",
        "EQUIPE:",
        f"  Leads novos: {eq.get('leads', 0)}  |  Atendidos (novos): {eq.get('atendidos', 0)}  |  Conv atend: {eq.get('conv_atendeu')}%",
        f"  Agendados: {eq.get('agendados', 0)}  |  Visitas (compareceu): {eq.get('visitas', 0)}  |  Vendas: {eq.get('vendas', 0)}",
        f"  VGV: {_fmt_brl(eq.get('vgv', 0))}  |  Ticket médio: {_fmt_brl(eq.get('ticket_medio'))}",
        f"  Recuperados (Remarketing): {eq.get('recuperados', 0)} leads voltaram pelo funil",
        "",
        "META ADS:",
        f"  Invest: {_fmt_brl(meta.get('spend', 0))}  |  CPL: {_fmt_brl(meta.get('cpl'))}  |  Leads Meta: {meta.get('leads_meta', 0)}",
    ]
    if d.get("derivados", {}).get("roi") is not None:
        linhas.append(f"  ROI: {round(d['derivados']['roi'])}× (VGV ÷ investimento)")

    if vaz.get("captados_meta", 0):
        linhas += [
            "",
            f"VAZAMENTO Meta→CRM: {vaz.get('captados_meta', 0)} captados | "
            f"{vaz.get('novos_no_funil', 0)} novos | {vaz.get('reengajados', 0)} reengajados | "
            f"{vaz.get('phone_invalido', 0)} phone-bug | {vaz.get('realmente_perdidos', 0)} perdidos "
            f"({vaz.get('taxa_vazamento_real_pct')}%)",
        ]

    linhas += ["", "POR CORRETORA:"]
    for c in cors:
        linhas.append(
            f"  {c.get('label'):5s}: {c.get('leads')} leads, {c.get('atendidos')} atend, "
            f"{c.get('agendados')} agend, {c.get('visitas')} visitou, {c.get('vendas')} vendeu, "
            f"VGV {_fmt_brl(c.get('vgv'))} · recup {c.get('recuperados', 0)}"
        )

    linhas += [
        "",
        f"ATENÇÃO HOJE: {atencao.get('leads_quentes', {}).get('total', 0)} leads quentes, "
        f"{atencao.get('leads_mornos_com_gap', {}).get('total', 0)} mornos c/ gap, "
        f"{len(atencao.get('visitas_hoje', []))} visitas hoje, "
        f"{atencao.get('tarefas_atrasadas', {}).get('total', 0)} tarefas atrasadas",
    ]

    if recup.get("por_user"):
        linhas.append("")
        linhas.append(f"RECUPERADOS POR CORRETORA: " + " · ".join(
            f"{u.get('label')} {u.get('qtde')}" for u in recup.get("por_user", [])
        ))

    if alertas:
        linhas += ["", "ALERTAS:"]
        for a in alertas[:5]:
            linhas.append(f"  [{a.get('nivel')}] {a.get('msg')}")

    return {"status": "ok", "resumo": "\n".join(linhas)}


def _tool_consultar_coaching(periodo: str = "mes_atual",
                             _for_corretora_id: int | None = None,
                             **_: Any) -> dict[str, Any]:
    """Resume Coaching — leads quentes e mornos-com-gap por corretora."""
    d = _fetch_painel("coaching", periodo, _for_corretora_id=_for_corretora_id)
    if d.get("_error"):
        return {"status": "error", "message": d["_error"]}
    totais = d.get("totais", {})
    cors = d.get("por_corretora", [])
    period_label = d.get("periodo", {}).get("label", periodo)

    linhas = [
        f"COACHING · {period_label}",
        f"Total: {totais.get('quentes', 0)} quentes, {totais.get('mornos_com_gap', 0)} mornos c/ gap",
        "",
    ]
    for c in cors:
        qs = c.get("quentes", [])
        ms = c.get("mornos_com_gap", [])
        if not qs and not ms:
            continue
        linhas.append(f"{c.get('label')} ({len(qs)} quentes, {len(ms)} mornos):")
        for lead in qs[:3]:  # top 3 quentes
            gap = (lead.get("gap_identificado") or "")[:140]
            linhas.append(f"  🔥 {lead.get('nome')}: {gap}")
        for lead in ms[:2]:  # top 2 mornos
            gap = (lead.get("gap_identificado") or "")[:120]
            linhas.append(f"  ⚠ {lead.get('nome')}: {gap}")
        linhas.append("")
    return {"status": "ok", "resumo": "\n".join(linhas)}


def _tool_consultar_agenda(periodo: str = "mes_atual",
                           _for_corretora_id: int | None = None,
                           **_: Any) -> dict[str, Any]:
    """Resume Agenda — visitas no período + status."""
    d = _fetch_painel("agenda", periodo, _for_corretora_id=_for_corretora_id)
    if d.get("_error"):
        return {"status": "error", "message": d["_error"]}
    total = d.get("total", 0)
    por_status = d.get("por_status", {})
    taxa = d.get("taxa_comparecimento")
    total_primeiras = d.get("total_primeiras", 0)
    total_revisitas = d.get("total_revisitas", 0)
    taxa_primeiras = d.get("taxa_comparecimento_primeiras")
    items = d.get("items", [])[:15]  # próximas 15
    period_label = d.get("periodo", {}).get("label", periodo)
    linhas = [
        f"AGENDA · {period_label}",
        f"Total: {total} visitas ({total_primeiras} primeiras + {total_revisitas} re-visitas)",
        f"Taxa comparecimento (todas): {taxa}% | (só primeiras): {taxa_primeiras}%",
        f"Por status: {dict(por_status)}",
        "",
        "Visitas (até 15):",
    ]
    for v in items:
        flag = " 🔁" if v.get("is_revisita") else ""
        linhas.append(
            f"  {v.get('data')} {v.get('hora')} · {v.get('lead_nome', '?')}{flag} · {v.get('corretora_label')} · {v.get('status')}"
        )
    return {"status": "ok", "resumo": "\n".join(linhas)}


def _tool_consultar_prospeccao(periodo: str = "mes_atual",
                               _for_corretora_id: int | None = None,
                               **_: Any) -> dict[str, Any]:
    """Resume Prospecção — mensagens WhatsApp + ligações + tempo de resposta."""
    d = _fetch_painel("conversas", periodo, _for_corretora_id=_for_corretora_id)
    if d.get("_error"):
        return {"status": "error", "message": d["_error"]}
    eq = d.get("equipe", {})
    cors = d.get("por_corretora", [])
    lig = d.get("ligacoes", {})
    lig_eq = lig.get("equipe", {})
    lig_cors = lig.get("por_corretora", [])
    ttr = d.get("tempo_resposta", {})
    ttr_eq = ttr.get("equipe", {})
    period_label = d.get("periodo", {}).get("label", periodo)

    linhas = [
        f"PROSPECÇÃO · {period_label}",
        "",
        f"WhatsApp equipe: {eq.get('enviadas', 0)} enviadas | {eq.get('recebidas', 0)} recebidas | {eq.get('contatos_unicos_soma', 0)} contatos",
        "  Por corretora:",
    ]
    for c in cors:
        linhas.append(f"    {c.get('label')}: {c.get('enviadas')} env / {c.get('recebidas')} rec / {c.get('contatos_unicos')} contatos")

    linhas += [
        "",
        f"Ligações equipe: {lig_eq.get('total', 0)} ligadas | {lig_eq.get('atendeu', 0)} atend | {lig_eq.get('prosseguiu', 0)} pross | taxa atend {lig_eq.get('taxa_atendimento_pct')}%",
    ]
    for c in lig_cors:
        sem = " (sem reporte)" if c.get("sem_reporte") else ""
        linhas.append(f"    {c.get('label')}: {c.get('total')} / {c.get('atendeu')} / {c.get('prosseguiu')}{sem}")

    if ttr_eq:
        linhas += [
            "",
            f"Tempo de resposta equipe: mediana {ttr_eq.get('mediana_min')}min | média {ttr_eq.get('media_min')}min | p90 {ttr_eq.get('p90_min')}min",
        ]
        for c in ttr.get("por_corretora", []):
            linhas.append(f"    {c.get('label')}: mediana {c.get('mediana_min')}min ({c.get('n_respondidas')} conversas)")

    return {"status": "ok", "resumo": "\n".join(linhas)}


def _tool_ler_conversa(telefone: str, limite: int = 30,
                       _for_corretora_id: int | None = None,
                       **_: Any) -> dict[str, Any]:
    """Lê histórico de mensagens WhatsApp com um número (Postgres mensagens_whatsapp).

    Args:
        telefone: número do contato (qualquer formato: '+5500000000000', '+5500000000000', etc).
        limite: quantas mensagens recentes retornar (default 30).
        _for_corretora_id: injetado quando o sender é corretora — filtra a query
            por corretora_id dela no BANCO (privacy/autorização). Quando é
            dona/admin, vem None e a busca é ampla.

    Returns dict com status='ok' e 'mensagens' (lista cronológica) ou status='empty'.

    FURO 1 (P0): antes qualquer corretora conseguia ler conversa de QUALQUER
    outra — a query não filtrava por corretora e a "regra" só existia no prompt
    do LLM. Agora o escopo é aplicado na SQL (fail-closed): sem match dela → vazio.
    """
    import re
    from sqlalchemy import or_

    from app.db.session import SessionLocal
    from app.db import models as _models

    digits = re.sub(r"\D", "", telefone or "")
    if len(digits) < 8:
        # Nome em vez de número: resolve pelo banco em vez de mandar a Dona
        # procurar o telefone que ela também não tem.
        r = _resolver_para_telefone(telefone, _for_corretora_id=_for_corretora_id)
        if r.get("status") != "ok":
            return r
        telefone = r["telefone"]
        digits = re.sub(r"\D", "", telefone)
    # tenta com e sem prefixo 55 + com/sem 9 móvel
    candidatos = {digits}
    if digits.startswith("55") and len(digits) >= 12:
        candidatos.add(digits[2:])  # sem código país
    if not digits.startswith("55"):
        candidatos.add("55" + digits)
    likes = [f"%{d}%" for d in candidatos]

    with SessionLocal() as session:
        q = (
            session.query(_models.MensagemWhatsapp)
            .filter(
                or_(
                    *[_models.MensagemWhatsapp.contato_telefone.ilike(p) for p in likes],
                    *[_models.MensagemWhatsapp.remote_jid.ilike(p) for p in likes],
                )
            )
        )
        # Escopo de autorização por corretora (aplicado no BANCO, não no prompt).
        # Fail-closed: se veio corretora_id, só mensagens dela; mensagens com
        # corretora_id NULL (ex.: linha compartilhada) NÃO vazam pra corretora.
        if _for_corretora_id is not None:
            q = q.filter(_models.MensagemWhatsapp.corretora_id == _for_corretora_id)
        q = (
            q.order_by(_models.MensagemWhatsapp.timestamp_evolution.desc())
            .limit(max(1, min(limite, 100)))
        )
        rows = list(reversed(q.all()))

    if not rows:
        if _for_corretora_id is not None:
            return {
                "status": "empty",
                "message": (
                    f"Nenhuma conversa SUA encontrada com o telefone {telefone}. "
                    "Só consigo mostrar conversas dos seus próprios atendimentos."
                ),
                "telefone_normalizado": list(candidatos),
            }
        return {
            "status": "empty",
            "message": f"Nenhuma mensagem encontrada pro telefone {telefone}.",
            "telefone_normalizado": list(candidatos),
        }

    mensagens = []
    for m in rows:
        ts = m.timestamp_evolution.astimezone(SP_TZ).strftime("%d/%m %H:%M")
        de = "corretora" if m.from_corretora else "contato"
        texto = m.conteudo_texto or m.transcricao or f"[{m.tipo_mensagem}]"
        mensagens.append({
            "ts": ts,
            "de": de,
            "instance": m.instance,
            "contato_nome": m.contato_nome,
            "texto": texto[:500],
        })

    return {
        "status": "ok",
        "telefone": telefone,
        "total": len(mensagens),
        "mensagens": mensagens,
    }


_KOMMO_STAGES_CACHE: dict[str, Any] = {"ts": 0.0, "data": {}}
_KOMMO_STAGES_TTL = 3600.0  # 1h


def _get_kommo_stage_names() -> dict[str, str]:
    """Mapeia status_id → nome da etapa, cacheado por 1h."""
    import time as _time
    now = _time.monotonic()
    if now - _KOMMO_STAGES_CACHE["ts"] < _KOMMO_STAGES_TTL and _KOMMO_STAGES_CACHE["data"]:
        return _KOMMO_STAGES_CACHE["data"]  # type: ignore[return-value]
    try:
        from app.config import settings as _settings
        from app.integrations.kommo_client import KommoClient
        kc = KommoClient(subdomain=_settings.kommo_subdomain, token=_settings.kommo_token)
        pipes = kc._get("/leads/pipelines").get("_embedded", {}).get("pipelines", [])  # type: ignore[attr-defined]
        mapa: dict[str, str] = {}
        for p in pipes:
            for st in p.get("_embedded", {}).get("statuses", []) or []:
                mapa[str(st.get("id"))] = st.get("name") or str(st.get("id"))
        _KOMMO_STAGES_CACHE["ts"] = now
        _KOMMO_STAGES_CACHE["data"] = mapa
        return mapa
    except Exception:
        return _KOMMO_STAGES_CACHE.get("data", {}) or {}  # type: ignore[return-value]


def _resolver_para_telefone(termo: str,
                            _for_corretora_id: int | None = None) -> dict[str, Any]:
    """Aceita telefone OU nome e devolve o telefone do lead.

    Existe porque a Assistente vive com o NOME na mão e não com o número: ela
    acabou de listar "Magda, Santana, Corretor Paula" e as tools de leitura só
    aceitavam dígitos — então ela pedia o telefone pra Dona, que também não
    tem. Quem tem o número é o banco.

    Returns:
        {"status":"ok", "telefone": str, "nome": str} quando resolve;
        {"status":"ambiguo", "opcoes":[...]} quando mais de um lead bate —
        aí quem escolhe é a Dona, não um chute nosso;
        {"status":"empty"} quando não achou ninguém.
    """
    import re

    from sqlalchemy import or_

    from app.db.session import SessionLocal
    from app.db import models as _models

    termo = (termo or "").strip()
    digits = re.sub(r"\D", "", termo)

    # Já é telefone — devolve como veio.
    if len(digits) >= 8:
        return {"status": "ok", "telefone": termo, "nome": None}

    if len(termo) < 3:
        return {"status": "error",
                "message": f"termo curto demais pra buscar: {termo!r}"}

    with SessionLocal() as session:
        def _busca(condicao):
            q = session.query(_models.LeadSnapshot).filter(
                condicao, _models.LeadSnapshot.telefone.isnot(None),
            )
            if _for_corretora_id is not None:
                q = q.filter(_models.LeadSnapshot.corretora_id == _for_corretora_id)
            return q.order_by(_models.LeadSnapshot.criado_em_kommo.desc()).limit(12).all()

        achados = _busca(_models.LeadSnapshot.nome.ilike(f"%{termo}%"))

        # Fallback por palavra. O Kommo guarda o que o lead digitou: no CRM ela
        # é "Rita", a Dona chama de "Rita Landi" — e `%Rita Landi%` não casa com
        # "Rita". A Assistente respondia "não achei ninguém com esse nome" e
        # devolvia a busca pra Dona, que também não tem o telefone.
        tokens = [t for t in re.split(r"\s+", termo) if len(t) >= 3]
        if not achados and tokens:
            achados = _busca(or_(*[
                _models.LeadSnapshot.nome.ilike(f"%{t}%") for t in tokens
            ]))
            # ILIKE casa substring: "Rita" traz "Aritana" e "Erita". Só vale
            # quem bate o token como PALAVRA inteira.
            def _pontua(r):
                palavras = re.findall(r"[\wÀ-ÿ]+", (r.nome or "").lower())
                return sum(1 for t in tokens if t.lower() in palavras)

            # O primeiro nome é o que identifica. Sem essa trava, "Arnaldo
            # Alves" (que não existe na base) voltava 6 "Alves" quaisquer e a
            # Assistente oferecia gente que não tem nada a ver com a pergunta.
            primeiro = tokens[0].lower()
            achados = [
                r for r in achados
                if primeiro in re.findall(r"[\wÀ-ÿ]+", (r.nome or "").lower())
            ]
            achados.sort(key=lambda r: -_pontua(r))
            # Um casando mais tokens que todos os outros = é esse.
            if len(achados) > 1 and _pontua(achados[0]) > _pontua(achados[1]):
                achados = achados[:1]

        achados = achados[:6]
        donas = {c.id: c.nome for c in session.query(_models.Corretora).all()}

        opcoes = [
            {
                "nome": r.nome,
                "telefone": r.telefone,
                "corretora": donas.get(r.corretora_id) or "(sem dona)",
            }
            for r in achados
        ]

    if not opcoes:
        return {"status": "empty",
                "message": f"não achei nenhum lead com nome parecido com {termo!r}."}

    # Nome exato bate → resolve direto mesmo com homônimos parciais na lista.
    exatos = [o for o in opcoes
              if (o["nome"] or "").strip().lower() == termo.lower()]
    if len(exatos) == 1:
        return {"status": "ok", **exatos[0]}
    if len(opcoes) == 1:
        return {"status": "ok", **opcoes[0]}

    return {
        "status": "ambiguo",
        "message": f"{len(opcoes)} leads batem com {termo!r} — qual deles?",
        "opcoes": opcoes,
    }


def _tool_buscar_lead(termo: str, _for_corretora_id: int | None = None,
                      **_: Any) -> dict[str, Any]:
    """Acha o telefone de um lead pelo nome (ou confirma um telefone).

    É a ponte entre "a Dona falou o nome" e as tools que exigem número.
    """
    r = _resolver_para_telefone(termo, _for_corretora_id=_for_corretora_id)
    if r.get("status") == "ok":
        return {
            "status": "ok",
            "resumo": f"{r.get('nome') or termo} · {r['telefone']}"
                      + (f" · {r['corretora']}" if r.get("corretora") else ""),
            **r,
        }
    return r


def _tool_ler_ficha_lead(telefone: str, _for_corretora_id: int | None = None,
                         **_: Any) -> dict[str, Any]:
    """Lê a ficha do lead no NOSSO banco (etapa, tarefas, responsável) por telefone.

    Use quando o WhatsApp tá vazio mas o lead existe na base — pra dar
    contexto baseado em etapa/tarefa em vez de chutar.

    Args:
        telefone: número do lead (qualquer formato).
        _for_corretora_id: injetado se sender é corretora — filtra só leads dela.

    Returns dict com lead_id, nome, etapa, corretora, criado_em, tarefas_pendentes.
    """
    import re
    from sqlalchemy import or_

    from app.db.session import SessionLocal
    from app.db import models as _models

    digits = re.sub(r"\D", "", telefone or "")
    if len(digits) < 8:
        # Veio nome em vez de número — resolve em vez de devolver erro.
        r = _resolver_para_telefone(telefone, _for_corretora_id=_for_corretora_id)
        if r.get("status") != "ok":
            return r
        telefone = r["telefone"]
        digits = re.sub(r"\D", "", telefone)

    candidatos = {digits}
    if digits.startswith("55") and len(digits) >= 12:
        candidatos.add(digits[2:])
    if not digits.startswith("55"):
        candidatos.add("55" + digits)
    likes = [f"%{d}%" for d in candidatos]

    with SessionLocal() as session:
        rows = (
            session.query(_models.LeadSnapshot)
            .filter(or_(*[_models.LeadSnapshot.telefone.ilike(p) for p in likes]))
            .order_by(_models.LeadSnapshot.criado_em_kommo.desc())
            .limit(5)
            .all()
        )

    if not rows:
        return {
            "status": "empty",
            "message": f"Nenhum lead encontrado pro telefone {telefone}.",
            "telefone_normalizado": list(candidatos),
        }

    # privacy: corretora só vê leads dela
    if _for_corretora_id:
        rows = [r for r in rows if r.corretora_id == _for_corretora_id]
        if not rows:
            return {
                "status": "lead_not_yours",
                "message": "Esse lead não é seu — não posso compartilhar dados de leads de outras corretoras.",
            }

    # busca tarefas pendentes + nome das etapas via Kommo (stages cacheados 1h)
    try:
        from app.config import settings
        from app.integrations.kommo_client import KommoClient
        kc = KommoClient(subdomain=settings.kommo_subdomain, token=settings.kommo_token)
    except Exception:
        kc = None

    stage_name_by_id = _get_kommo_stage_names()

    CORRETORA_BY_ID = {1: "Pessoa10", 2: "Pessoa11", 3: "Pessoa12"}
    leads_out = []
    for r in rows[:3]:
        tarefas: list[dict[str, Any]] = []
        if kc is not None:
            try:
                data = kc._get("/tasks", params={  # type: ignore[attr-defined]
                    "filter[entity_type]": "leads",
                    "filter[entity_id]": int(r.lead_id),
                    "filter[is_completed]": 0,
                    "limit": 50,
                })
                raw = data.get("_embedded", {}).get("tasks", []) or []
                for t in raw:
                    ts = int(t.get("complete_till") or 0)
                    venc = datetime.fromtimestamp(ts, tz=SP_TZ).strftime("%d/%m %H:%M") if ts else None
                    atrasada = ts > 0 and ts < datetime.now(SP_TZ).timestamp()
                    tarefas.append({
                        "id": t.get("id"),
                        "texto": (t.get("text") or "")[:200],
                        "vence_em": venc,
                        "atrasada": atrasada,
                    })
            except Exception as e:
                tarefas = [{"erro": str(e)[:120]}]

        criado = r.criado_em_kommo.astimezone(SP_TZ).strftime("%d/%m/%Y") if r.criado_em_kommo else None
        leads_out.append({
            "lead_id": r.lead_id,
            "nome": r.nome,
            "telefone": r.telefone,
            "corretora": CORRETORA_BY_ID.get(r.corretora_id, f"id={r.corretora_id}"),
            "etapa_kommo": stage_name_by_id.get(str(r.etapa_kommo), r.etapa_kommo),
            "origem": r.origem,
            "criado_em": criado,
            "tarefas_pendentes": tarefas,
            "total_tarefas_atrasadas": sum(1 for t in tarefas if t.get("atrasada")),
        })

    return {"status": "ok", "telefone": telefone, "total": len(leads_out), "leads": leads_out}


def _tool_ler_handoff_lead(telefone: str, _for_corretora_id: int | None = None,
                           **_: Any) -> dict[str, Any]:
    """Lê a nota de passagem (handoff) curada de um lead, por telefone.

    É o contexto narrativo de quando o lead foi qualificado/resgatado e passado
    pra corretora (ex: pela Corretor) — o "porquê" e o "como conduzir" que não estão
    nas mensagens nem no Kommo. Use quando a corretora falar de um lead
    recuperado/resgatado e você precisar entender a história antes de orientar.

    Args:
        telefone: número do lead (qualquer formato).
        _for_corretora_id: injetado se sender é corretora — filtra só leads dela.

    Returns dict com status='ok' e 'notas', ou status='empty'/'lead_not_yours'.
    """
    import re
    from sqlalchemy import or_

    from app.db.session import SessionLocal
    from app.db import models as _models

    digits = re.sub(r"\D", "", telefone or "")
    if len(digits) < 8:
        return {"status": "error", "message": f"telefone inválido: {telefone!r}"}

    candidatos = {digits}
    if digits.startswith("55") and len(digits) >= 12:
        candidatos.add(digits[2:])
    if not digits.startswith("55"):
        candidatos.add("55" + digits)
    likes = [f"%{d}%" for d in candidatos]

    with SessionLocal() as session:
        rows = (
            session.query(_models.LeadHandoffNote)
            .filter(_models.LeadHandoffNote.status == "ativo")
            .filter(or_(*[_models.LeadHandoffNote.telefone.ilike(p) for p in likes]))
            .order_by(_models.LeadHandoffNote.atualizado_em.desc())
            .limit(5)
            .all()
        )

    if not rows:
        return {
            "status": "empty",
            "message": f"Nenhuma nota de passagem encontrada pro telefone {telefone}.",
            "telefone_normalizado": list(candidatos),
        }

    # privacy: corretora só vê notas de leads dela
    if _for_corretora_id:
        rows = [r for r in rows if r.corretora_id == _for_corretora_id]
        if not rows:
            return {
                "status": "lead_not_yours",
                "message": "Esse lead não é seu — não posso compartilhar contexto de leads de outras corretoras.",
            }

    CORRETORA_BY_ID = {1: "Pessoa10", 2: "Pessoa11", 3: "Pessoa12"}
    notas = [
        {
            "lead_nome": r.lead_nome,
            "telefone": r.telefone,
            "corretora": CORRETORA_BY_ID.get(r.corretora_id, None) if r.corretora_id else None,
            "origem": r.origem,
            "contexto": r.contexto,
        }
        for r in rows
    ]
    return {"status": "ok", "telefone": telefone, "total": len(notas), "notas": notas}


def _tool_enviar_whatsapp_corretora(corretora: str, mensagem: str,
                                    confirmado: bool = False, **_: Any) -> dict[str, Any]:
    """Envia mensagem WhatsApp pra uma corretora via instância `assistente`.

    Requer confirmação explícita pra evitar disparo não-intencional.
    """
    key = corretora.strip().lower()
    phone = _CORRETORA_WHATSAPP.get(key)
    if not phone:
        return {
            "status": "error",
            "message": f"corretora '{corretora}' não reconhecida. Use Pessoa10, Pessoa11 ou Pessoa12.",
        }

    nome_canonico = {
        "+5500000000000": "Pessoa10",
        "+5500000000000": "Pessoa11",
        "+5500000000000":  "Pessoa12",
    }.get(phone, corretora)

    if not confirmado:
        return {
            "status": "needs_confirmation",
            "preview": (
                f"Vou mandar WhatsApp pra *{nome_canonico}* (...{phone[-4:]}):\n\n"
                f"\"{mensagem}\"\n\n"
                f"Confirma?"
            ),
            "action": "enviar_whatsapp_corretora",
            "params": {"corretora": nome_canonico, "mensagem": mensagem},
        }

    # Envio real via instância `assistente`
    try:
        from app.integrations.evolution_client import EvolutionClient
        cli = EvolutionClient()
        r = cli.send_text("assistente", phone, mensagem)
        msg_id = (r.get("key") or {}).get("id", "?")
        log.info("assistente_whatsapp_sent", to=nome_canonico, msg_id=msg_id[:24])
        return {
            "status": "ok",
            "message": f"Mandei pra {nome_canonico}! (msg_id={msg_id[:14]}...)",
            "msg_id": msg_id,
        }
    except Exception as e:
        log.error("assistente_whatsapp_error", error=str(e))
        return {
            "status": "error",
            "message": f"Não consegui enviar — {str(e)[:100]}",
        }


def _tool_ler_respostas_disparo(periodo: str = "tudo", limite: int = 20,
                                _for_corretora_id: int | None = None,
                                **_: Any) -> dict[str, Any]:
    """Lê o resultado do disparo de reengajamento da Corretor — quem respondeu e O QUÊ.

    Fecha o buraco de antes: os números do disparo (enviados/entregues/respondeu)
    viviam em `disparos_reengajamento` e o TEXTO das respostas em
    `mensagens_whatsapp`, mas nada cruzava as duas — a Assistente só sabia ler
    conversa se a Dona desse o telefone na mão. Aqui o join é feito no banco:
    pra cada telefone que respondeu, traz as primeiras mensagens do lead
    DEPOIS do disparo.

    Args:
        periodo: 'tudo' | '7d' | '30d' | 'mes_atual'.
        limite: quantos respondentes retornar (default 20, teto 50).
        _for_corretora_id: injetado quando o sender é corretora — escopa por
            corretora_id no BANCO (fail-closed, igual ler_conversa).

    Returns dict com status='ok', 'resumo' (contagens) e 'respostas'.
    """
    import re
    from datetime import timedelta

    from sqlalchemy import or_

    from app.db.session import SessionLocal
    from app.db import models as _models

    agora = datetime.now(SP_TZ)
    p = (periodo or "tudo").strip().lower()
    if p in ("7d", "semana", "ultimos_7_dias"):
        desde, label = agora - timedelta(days=7), "últimos 7 dias"
    elif p in ("30d", "ultimos_30_dias"):
        desde, label = agora - timedelta(days=30), "últimos 30 dias"
    elif p in ("mes_atual", "mes"):
        desde, label = agora.replace(day=1, hour=0, minute=0, second=0, microsecond=0), "mês atual"
    else:
        desde, label = None, "todo o histórico"

    lim = max(1, min(limite, 50))

    with SessionLocal() as session:
        base = session.query(_models.DisparoReengajamento)
        if desde is not None:
            base = base.filter(_models.DisparoReengajamento.enviado_em >= desde)
        if _for_corretora_id is not None:
            base = base.filter(_models.DisparoReengajamento.corretora_id == _for_corretora_id)

        enviados = base.count()
        opt_outs = base.filter(_models.DisparoReengajamento.opt_out.is_(True)).count()
        responderam = base.filter(
            _models.DisparoReengajamento.respondeu_em.isnot(None)
        ).count()
        rows = (
            base.filter(_models.DisparoReengajamento.respondeu_em.isnot(None))
            .order_by(_models.DisparoReengajamento.respondeu_em.desc())
            .limit(lim)
            .all()
        )

        if enviados == 0:
            return {
                "status": "empty",
                "message": f"Nenhum disparo de reengajamento registrado em {label}.",
            }

        respostas = []
        for d in rows:
            digits = re.sub(r"\D", "", d.telefone or "")
            # casa por sufixo (últimos 10 dígitos) pra tolerar 55/9-móvel
            suf = digits[-10:] if len(digits) >= 10 else digits
            like = f"%{suf}%"
            msgs = (
                session.query(_models.MensagemWhatsapp)
                .filter(
                    or_(
                        _models.MensagemWhatsapp.contato_telefone.ilike(like),
                        _models.MensagemWhatsapp.remote_jid.ilike(like),
                    ),
                    _models.MensagemWhatsapp.from_corretora.is_(False),
                    _models.MensagemWhatsapp.timestamp_evolution >= d.enviado_em,
                )
                .order_by(_models.MensagemWhatsapp.timestamp_evolution.asc())
                .limit(3)
                .all()
            )
            respostas.append({
                "telefone": d.telefone,
                "nome": next((m.contato_nome for m in msgs if m.contato_nome), None),
                "template": d.template,
                "variacao": d.variacao,
                "enviado_em": d.enviado_em.astimezone(SP_TZ).strftime("%d/%m %H:%M"),
                "respondeu_em": d.respondeu_em.astimezone(SP_TZ).strftime("%d/%m %H:%M"),
                "opt_out": bool(d.opt_out),
                "disse": [
                    (m.conteudo_texto or m.transcricao or f"[{m.tipo_mensagem}]")[:300]
                    for m in msgs
                ] or None,
            })

    taxa = round(responderam / enviados * 100, 1) if enviados else 0.0
    sem_texto = sum(1 for r in respostas if not r["disse"])
    resumo = (
        f"DISPARO DE REENGAJAMENTO (Corretor) · {label}\n"
        f"  Enviados: {enviados}  |  Responderam: {responderam} ({taxa}%)  |  Opt-out: {opt_outs}\n"
        f"  Mostrando as {len(respostas)} respostas mais recentes"
        + (f" ({sem_texto} sem texto salvo no WhatsApp)" if sem_texto else "")
    )
    return {
        "status": "ok",
        "resumo": resumo,
        "enviados": enviados,
        "responderam": responderam,
        "opt_outs": opt_outs,
        "respostas": respostas,
    }


def _tool_listar_leads(periodo: str = "hoje", limite: int = 30,
                       corretora: str | None = None,
                       _for_corretora_id: int | None = None,
                       **_: Any) -> dict[str, Any]:
    """Lista os leads de um período COM nome e telefone de cada um.

    O elo que faltava: `consultar_visao_geral` diz QUANTOS leads caíram, mas só
    o número. `ler_conversa`/`ler_ficha_lead` exigem um telefone. Sem esta tool
    a Assistente sabia "caíram 5 leads hoje" e no instante seguinte precisava
    pedir os telefones pra Dona — que é justamente o que ela não tem à mão.

    Args:
        periodo: 'hoje', 'ontem', '7d', '30d'.
        limite: máximo de leads retornados (default 30, teto 100).
        corretora: filtra pelo nome/apelido da corretora (ex.: 'Pessoa10').
        _for_corretora_id: injetado quando quem pergunta é corretora — trava
            no BANCO pra ela só ver os próprios leads (fail-closed, igual
            ler_conversa).

    Returns dict com 'leads' (lista com nome, telefone, corretora, origem,
    hora, atendido) pronta pra alimentar ler_conversa em seguida.
    """
    from datetime import timedelta

    from app.db.session import SessionLocal
    from app.db import models as _models

    agora = datetime.now(SP_TZ)
    inicio_hoje = agora.replace(hour=0, minute=0, second=0, microsecond=0)
    janelas = {
        "hoje":  (inicio_hoje, agora),
        "ontem": (inicio_hoje - timedelta(days=1), inicio_hoje),
        "7d":    (inicio_hoje - timedelta(days=7), agora),
        "30d":   (inicio_hoje - timedelta(days=30), agora),
    }
    if periodo not in janelas:
        return {"status": "error",
                "message": f"período inválido: {periodo!r}. Use hoje, ontem, 7d ou 30d."}
    desde, ate = janelas[periodo]

    with SessionLocal() as session:
        donas = {c.id: c.nome for c in session.query(_models.Corretora).all()}

        q = (
            session.query(_models.Lead)
            .filter(_models.Lead.created_at >= desde,
                    _models.Lead.created_at < ate)
        )
        # Escopo de autorização — mesma regra das outras tools de leitura.
        if _for_corretora_id is not None:
            q = q.filter(_models.Lead.owner_id == _for_corretora_id)
        elif corretora:
            alvo = [cid for cid, nome in donas.items()
                    if corretora.strip().lower() in (nome or "").lower()]
            if not alvo:
                return {"status": "empty",
                        "message": f"não achei corretora com nome parecido com {corretora!r}."}
            q = q.filter(_models.Lead.owner_id.in_(alvo))

        rows = (q.order_by(_models.Lead.created_at.desc())
                 .limit(max(1, min(limite, 100))).all())

        leads = []
        for l in rows:
            # "atendido" = existe mensagem NOSSA pro número depois que o lead entrou
            atendido = False
            if l.telefone:
                import re as _re
                d = _re.sub(r"\D", "", l.telefone)
                if len(d) >= 8:
                    atendido = bool(
                        session.query(_models.MensagemWhatsapp.id)
                        .filter(
                            _models.MensagemWhatsapp.contato_telefone.ilike(f"%{d[-8:]}%"),
                            _models.MensagemWhatsapp.from_corretora.is_(True),
                            _models.MensagemWhatsapp.timestamp_evolution >= l.created_at,
                        ).first()
                    )
            leads.append({
                "nome": l.nome or "(sem nome)",
                "telefone": l.telefone,
                "corretora": donas.get(l.owner_id) or "(sem dona)",
                "origem": l.origem_campanha or l.origem or "—",
                "entrou": l.created_at.astimezone(SP_TZ).strftime("%d/%m %H:%M")
                          if l.created_at else "—",
                "atendido": atendido,
            })

    if not leads:
        return {"status": "empty",
                "message": f"nenhum lead no período '{periodo}'."}

    sem_resposta = [l for l in leads if not l["atendido"]]
    linhas = [f"LEADS · {periodo} ({len(leads)})", ""]
    for l in leads:
        marca = "✓" if l["atendido"] else "✗ SEM RESPOSTA"
        linhas.append(f"  {l['entrou']} · {l['nome']} · {l['telefone']} · "
                      f"{l['corretora']} · {l['origem']} · {marca}")
    if sem_resposta:
        linhas += ["", f"⚠️ {len(sem_resposta)} sem resposta: "
                       + ", ".join(f"{l['nome']} ({l['telefone']})" for l in sem_resposta)]

    return {
        "status": "ok",
        "resumo": "\n".join(linhas),
        "total": len(leads),
        "sem_resposta": len(sem_resposta),
        "leads": leads,
    }


TOOL_HANDLERS: dict[str, Callable[..., dict[str, Any]]] = {
    "criar_visita": _tool_criar_visita,
    "marcar_visita": _tool_marcar_visita,
    "registrar_ligacao": _tool_registrar_ligacao,
    "registrar_venda": _tool_registrar_venda,
    "add_note": _tool_add_note,
    "enviar_whatsapp_corretora": _tool_enviar_whatsapp_corretora,
    "consultar_visao_geral": _tool_consultar_visao_geral,
    "consultar_coaching": _tool_consultar_coaching,
    "consultar_agenda": _tool_consultar_agenda,
    "consultar_prospeccao": _tool_consultar_prospeccao,
    "listar_leads": _tool_listar_leads,
    "buscar_lead": _tool_buscar_lead,
    "ler_conversa": _tool_ler_conversa,
    "ler_ficha_lead": _tool_ler_ficha_lead,
    "ler_handoff_lead": _tool_ler_handoff_lead,
    "ler_respostas_disparo": _tool_ler_respostas_disparo,
}

TOOLS_NEED_CORRETORA = {"criar_visita", "registrar_ligacao", "registrar_venda"}

# Tools de LEITURA cujo escopo tem que ser filtrado por corretora no backend
# (banco/painel) quando o remetente é corretora — não confiar no prompt do LLM.
# Furo 1 (ler_conversa) + Furo 2 (tools de painel). ler_ficha_lead/ler_handoff_lead
# já filtravam; incluídos aqui pra centralizar a injeção do escopo.
TOOLS_SCOPED_TO_CORRETORA = {
    "ler_ficha_lead",
    "ler_handoff_lead",
    "ler_conversa",
    "listar_leads",
    "buscar_lead",
    "consultar_visao_geral",
    "consultar_coaching",
    "consultar_agenda",
    "consultar_prospeccao",
    "ler_respostas_disparo",
}


# ── Toolkit-like wrapper (compat com testes Onda 1) ──────────────────────────


class AssistenteSkillsToolkit:
    """
    Surface compatível com testes que esperam objeto com `.tools` (5 callables).
    """

    def __init__(self) -> None:
        self.criar_visita = _tool_criar_visita
        self.marcar_visita = _tool_marcar_visita
        self.registrar_ligacao = _tool_registrar_ligacao
        self.registrar_venda = _tool_registrar_venda
        self.add_note = _tool_add_note
        self.enviar_whatsapp_corretora = _tool_enviar_whatsapp_corretora
        self.consultar_visao_geral = _tool_consultar_visao_geral
        self.consultar_coaching = _tool_consultar_coaching
        self.consultar_agenda = _tool_consultar_agenda
        self.consultar_prospeccao = _tool_consultar_prospeccao
        self.ler_conversa = _tool_ler_conversa
        self.ler_ficha_lead = _tool_ler_ficha_lead
        self.ler_handoff_lead = _tool_ler_handoff_lead
        self.ler_respostas_disparo = _tool_ler_respostas_disparo
        # `.name` é setado abaixo pra compat com test que itera tools
        for fn_name in TOOL_HANDLERS:
            handler = TOOL_HANDLERS[fn_name]
            handler.name = fn_name  # type: ignore[attr-defined]
        self.tools = [
            _tool_criar_visita,
            _tool_marcar_visita,
            _tool_registrar_ligacao,
            _tool_registrar_venda,
            _tool_add_note,
            _tool_enviar_whatsapp_corretora,
            _tool_consultar_visao_geral,
            _tool_consultar_coaching,
            _tool_consultar_agenda,
            _tool_consultar_prospeccao,
            _tool_ler_conversa,
            _tool_ler_ficha_lead,
            _tool_ler_handoff_lead,
        ]


# ── Agente principal ─────────────────────────────────────────────────────────


class AgenteAssistente:
    """
    Agente conversacional. Sem dependência de Agno.Agent ou SDK Anthropic.
    Roda via app.llm.cli_provider (claude -p).
    """

    def __init__(
        self,
        phone: str | None = None,
        sender_kind: str | None = None,
        sender_id: int | None = None,
        sender_nome: str | None = None,
        sender_papel: str | None = None,
        history: list[dict[str, Any]] | None = None,
        provider: Any | None = None,
    ) -> None:
        self.phone = phone
        self.sender_kind = sender_kind
        self.sender_id = sender_id
        self.sender_nome = sender_nome
        self.sender_papel = sender_papel
        self.history = history or []
        self.soul = _load_soul()
        self.toolkit = AssistenteSkillsToolkit()
        self._provider = provider  # se None, lazy-load via get_llm_provider()

    # Compat com test_assistente_tem_5_tools_registradas
    @property
    def tools(self) -> list[Any]:
        return list(self.toolkit.tools)

    # ── system prompt construction ───────────────────────────────────────────

    def _sender_block(self) -> str:
        if self.sender_kind == "corretora":
            return (
                f"Você está conversando com {self.sender_nome} (corretora).\n"
                "Mostre apenas dados dela. Não compare com outras corretoras.\n"
                "Não revele dados de outras corretoras, mesmo se perguntada.\n"
                "Se ela registrar visita/ligação/venda, registra em nome dela "
                "(corretora_id já vinculado ao contexto)."
            )
        if self.sender_kind == "dona":
            return (
                f"Você está conversando com {self.sender_nome} (dona da imobiliária).\n"
                "Ela tem autoridade total: pode pedir dados de qualquer corretora, "
                "registrar coisas em nome de qualquer uma, ver comparativos.\n"
                "A regra de privacidade entre corretoras NÃO se aplica a ela.\n"
                "Quando ela registrar algo em nome de uma corretora, peça "
                "confirmação clara de qual corretora antes de gravar.\n"
                "\n"
                "REGRA Pessoa→Pessoa3: quando Pessoa falar em 1ª pessoa sobre "
                "venda/visita/ligação ('vendi', 'minha venda', 'fechei', 'agendei'), "
                "trate como ação dela atuando como corretora pela conta da Pessoa3 — "
                "passe corretora=Pessoa (o alias resolve pra Pessoa3). Não pergunte "
                "'qual corretora?' nesse caso.\n"
                "\n"
                "Antes de gravar QUALQUER venda, peça confirmação explícita de "
                "cliente + valor + corretora identificada. Exemplo:\n"
                "  Pessoa: 'vendi pra Carlos 200k'\n"
                "  Você:  'Confirma: venda da Pessoa12, cliente Carlos, R$ 200.000. Pode gravar?'\n"
                "  Pessoa: 'sim'\n"
                "  Você:  registra + confirma OK\n"
                "\n"
                "Só pergunte 'qual corretora?' se ela falar em 3ª pessoa ('a Pessoa11 vendeu', "
                "'venda da Pessoa10') ou se for genuinamente ambíguo."
            )
        return "Você está conversando com alguém sem contexto identificado."

    def _history_block(self) -> str:
        if not self.history:
            return ""
        lines = ["Mensagens recentes da conversa:"]
        for h in self.history:
            ts = h.get("created_at")
            hhmm = ts.astimezone(SP_TZ).strftime("%H:%M") if isinstance(ts, datetime) else "??:??"
            who = "você" if h.get("role") == "assistant" else "usuário"
            text = h.get("text", "")
            lines.append(f"[{hhmm}] {who}: {text}")
        return "\n".join(lines)

    def _tools_block(self) -> str:
        return (
            "Tools disponíveis (chame via campo tool_call do JSON):\n"
            "- criar_visita(corretora, cliente, data, hora, telefone?, bairro?, is_revisita?, confirmado=false) — detecta re-visita automaticamente (mesma corretora+nome ou telefone nos últimos 90d) e avisa no preview; corretora pode discordar via is_revisita=false explícito\n"
            "- marcar_visita(cliente, status, confirmado=false) — status DEVE ser: compareceu | no_show | cancelada ('não compareceu'/'deu bolo' → no_show). Passe o NOME do cliente como a corretora falou — eu acho a visita aberta sozinha, NÃO precisa de consultar_agenda nem de evento_id antes.\n"
            "- registrar_ligacao(corretora, ligacoes, atendeu, prosseguiu, semana?, confirmado=false)\n"
            "- registrar_venda(corretora, cliente, valor, mes, confirmado=false)\n"
            "- add_note(contexto, texto)\n"
            "- enviar_whatsapp_corretora(corretora, mensagem, confirmado=false) — manda WhatsApp em nome da Acme pra Pessoa10/Pessoa11/Pessoa12\n"
            "- consultar_visao_geral(periodo='mes_atual') — KPIs do funil, Meta Ads, vazamento, alertas\n"
            "- consultar_coaching(periodo='mes_atual') — leads quentes + gaps por corretora\n"
            "- consultar_agenda(periodo='mes_atual') — visitas + status + taxa comparecimento\n"
            "- consultar_prospeccao(periodo='mes_atual') — mensagens WhatsApp + ligações + tempo resposta\n"
            "- listar_leads(periodo='hoje'|'ontem'|'7d'|'30d', corretora=None, limite=30) — lista os leads do período COM nome, telefone, dona, origem, hora e se já foram atendidos. É a sua porta de entrada: SEMPRE que perguntarem sobre os leads de um período ('quantos caíram hoje', 'quem caiu', 'me mostra o histórico das conversas dos leads de hoje'), chame esta tool PRIMEIRO pra pegar os telefones e DEPOIS chame ler_conversa em cada um. NUNCA peça telefone ou nome pra Dona — você tem como descobrir sozinha.\n"
            "- buscar_lead(termo) — acha o telefone de um lead pelo NOME. Use quando você tem o nome mas não o número. Se vier 'ambiguo', mostre as opções pra Dona escolher.\n"
            "- ler_conversa(telefone, limite=30) — puxa histórico WhatsApp com um número (da base mensagens_whatsapp). Use quando a Dona perguntar 'o que a corretora falou com fulano', 'me mostra a conversa com 5515...', ou quando você precisar entender o que rolou num atendimento específico antes de responder. Se não tiver o telefone em mãos, pegue com listar_leads antes — não pergunte pra Dona. ler_conversa e ler_ficha_lead TAMBÉM aceitam o NOME no lugar do número: se você sabe o nome, mande o nome.\n"
            "- ler_ficha_lead(telefone) — busca a ficha do lead na base (etapa atual, tarefas pendentes, responsável, origem). Use SEMPRE que ler_conversa voltar vazio mas existir suspeita de que o lead esteja cadastrado — dá contexto pra dar coaching de verdade mesmo sem histórico WhatsApp. Também usa quando perguntarem 'em que etapa está fulano' ou 'quais tarefas atrasadas tem com esse lead'.\n"
            "- ler_handoff_lead(telefone) — lê a nota de PASSAGEM curada de um lead recuperado/resgatado (o porquê e o como conduzir, ex: 'permuta — apoiar na mecânica', 'em documentação, quase-venda'). Use quando a corretora falar de um lead que foi passado/recuperado e você precisar da história antes de orientar. Complementa ler_conversa/ler_ficha_lead: aqui está o contexto que NÃO está nas mensagens nem na ficha.\n"
            "- ler_respostas_disparo(periodo='tudo', limite=20) — resultado do disparo de reengajamento da CORRETOR: quantos foram enviados, quantos responderam, quantos deram opt-out E o texto do que cada um respondeu. Use SEMPRE que a Dona falar de 'disparo da Corretor', 'reengajamento', 'o que as X responderam', 'quem respondeu o disparo' — você NÃO precisa mais pedir telefone pra ver isso.\n\n"
            "Quando a Dona pedir análise do painel ('como tá o funil', 'leads quentes', 'visitas da semana', 'tempo de resposta'), USE essas tools de consulta antes de responder — não chute números.\n\n"
            "Pra criar_visita, marcar_visita, registrar_ligacao, registrar_venda e enviar_whatsapp_corretora: NUNCA chame\n"
            "com confirmado=true antes de mostrar preview e receber 'sim'/'confirma' do usuário.\n"
        )

    def _build_system_prompt(self) -> str:
        from app.services.atendimento_brain import brain as _atend_brain
        parts = [self.soul, "---", _atend_brain(sugerir=True), "---", self._sender_block(), "---", self._tools_block()]
        history_block = self._history_block()
        if history_block:
            parts += ["---", history_block]
        return "\n\n".join(parts)

    # ── Tool dispatch ────────────────────────────────────────────────────────

    def _maybe_inject_corretora(self, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Auto-fill 'corretora' arg quando sender é corretora."""
        if (
            # marcar_visita não entra em TOOLS_NEED_CORRETORA (dona pode marcar
            # sem dizer corretora — lookup por cliente resolve), mas corretora
            # remetente SEMPRE tem a sua injetada: só marca visita dela.
            (tool_name in TOOLS_NEED_CORRETORA or tool_name == "marcar_visita")
            and self.sender_kind == "corretora"
            and "corretora" not in args
            and self.sender_nome
        ):
            args = {**args, "corretora": self.sender_nome}
        # ler_ficha_lead / ler_handoff_lead / ler_conversa / tools de painel:
        # injeta corretora_id pra escopar a autorização no BANCO/painel (NÃO no
        # prompt). Furo 1 (ler_conversa) + Furo 2 (tools de painel) — antes a
        # corretora via dado de qualquer outra / resposta admin.
        if (
            tool_name in TOOLS_SCOPED_TO_CORRETORA
            and self.sender_kind == "corretora"
            and self.sender_id
        ):
            args = {**args, "_for_corretora_id": self.sender_id}
        # criar_visita: carimba quem registrou (auditoria A5) com o telefone do
        # remetente, quando o LLM não passou explícito.
        if (
            tool_name == "criar_visita"
            and self.phone
            and not args.get("registrado_por_telefone")
        ):
            args = {**args, "registrado_por_telefone": self.phone}
        return args

    def _validate_dona_corretora(self, tool_name: str, args: dict[str, Any]) -> dict[str, Any] | None:
        """Se sender é dona e tool exige corretora, mas args não tem → bloquear."""
        if (
            tool_name in TOOLS_NEED_CORRETORA
            and self.sender_kind == "dona"
            and not args.get("corretora")
        ):
            return {
                "status": "needs_corretora_specified",
                "message": "De qual corretora é esse registro?",
            }
        return None

    def _execute_tool(self, name: str, raw_args: dict[str, Any]) -> dict[str, Any]:
        if name not in TOOL_HANDLERS:
            return {"status": "error", "message": f"tool desconhecida: {name}"}

        guard = self._validate_dona_corretora(name, raw_args)
        if guard is not None:
            return guard

        args = self._maybe_inject_corretora(name, raw_args)
        try:
            return TOOL_HANDLERS[name](**args)
        except TypeError as e:
            return {"status": "error", "message": f"argumentos inválidos: {e}"}

    # ── Run loop ─────────────────────────────────────────────────────────────

    def _provider_complete(self, system: str, user: str) -> str:
        provider = self._provider or get_llm_provider()
        json_str, _ = provider.complete(system=system, user=user, json_schema=RESPONSE_SCHEMA)
        return json_str

    def run(self, message: str) -> dict[str, Any]:
        """
        Retorna {response_text, tool_calls}. Rola um loop curto (max 3) pra
        permitir tool → resposta final → tool extra → fim.
        """
        system = self._build_system_prompt()
        user_buffer = message
        tool_calls_log: list[dict[str, Any]] = []
        last_text = ""
        empurrado = False  # já devolvemos a bola uma vez neste turno?
        resultados: list[str] = []  # todas as tools do turno, em ordem

        for iteration in range(MAX_TOOL_ITERATIONS):
            try:
                json_str = self._provider_complete(system, user_buffer)
            except Exception as e:
                log.error("assistente_provider_error", error=str(e))
                return {
                    "response_text": "Tive um problema técnico aqui. Tenta de novo daqui a pouco.",
                    "tool_calls": tool_calls_log,
                }

            try:
                parsed = json.loads(json_str)
            except json.JSONDecodeError as e:
                log.error("assistente_invalid_json", error=str(e), raw=json_str[:200])
                return {
                    "response_text": "Tive um problema interpretando a resposta. Tenta perguntar de outro jeito?",
                    "tool_calls": tool_calls_log,
                }

            text = parsed.get("text", "")
            last_text = text or last_text
            tool_call = parsed.get("tool_call")

            if not tool_call:
                # "Vou puxar a conversa da Rita agora." — e o turno acabava aqui.
                # A Dona ficava mandando "Eai", "Vai", "Esi" pra cada passo, e a
                # promessa nunca era cumprida: quem devolve texto sem tool_call
                # encerra o turno. Uma vez por turno, devolvemos a bola: execute.
                if not empurrado and _promete_sem_fazer(text):
                    empurrado = True
                    log.info("assistente_promessa_sem_tool", iteration=iteration)
                    user_buffer = (
                        f"{user_buffer}\n\n[sistema]: Você respondeu "
                        f"\"{text[:160]}\" — anunciou uma busca em vez de fazer. "
                        "Você não roda nada depois de enviar esta resposta: ou "
                        "chama a tool AGORA, ou diz que não consegue e por quê. "
                        "Não prometa buscar depois."
                    )
                    continue
                return {"response_text": text, "tool_calls": tool_calls_log}

            name = tool_call.get("name")
            args = tool_call.get("args") or {}
            log.info("assistente_tool_call", name=name, iteration=iteration)
            result = self._execute_tool(name, args)
            tool_calls_log.append({"name": name, "args": args, "result": result})

            # ACUMULA. Antes reescrevia do zero com o último resultado, então a
            # segunda tool não enxergava a primeira: "achei o telefone do
            # Arnaldo" sumia antes de virar "puxa a conversa dele", e o agente
            # ficava girando no mesmo passo.
            resultados.append(
                f"[resultado da tool {name}]: {json.dumps(result, ensure_ascii=False)}"
            )
            user_buffer = (
                f"{message}\n\n" + "\n\n".join(resultados) + "\n\n"
                f"Use esses resultados pra responder. Se ainda falta um passo "
                f"(ex: já tem o telefone e falta ler a conversa), chame a "
                f"próxima tool agora — você não tem outra chance depois. "
                f"Se status='needs_confirmation', mostre a pergunta de preview "
                f"ao usuário e termine sem chamar mais tools."
            )

        # Estourou o cap de iterações
        return {
            "response_text": last_text or "Pera, deixa eu organizar aqui e te respondo.",
            "tool_calls": tool_calls_log,
        }
