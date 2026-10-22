"""
Assistente Copilota — assistente INTERNA da corretora dentro do dash (Fase 1: ler + responder).

Fluxo: pergunta → ROTEADOR (classifica intent + extrai entidades) → busca o dado certo
(reusa os endpoints do painel, JÁ escopados por corretora) → RESPONDE no tom da Assistente.

Fase 1 = só leitura. Nada escreve, nada envia. Escopo por corretora automático (X-Painel-User).
"""
from __future__ import annotations

import base64
import json as _json
import os
import re
import subprocess
import sys
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import structlog
from fastapi import APIRouter, Body, File, Header, UploadFile
from starlette.concurrency import run_in_threadpool

from app.config import get_llm_provider_assistente
from app.api.painel import _identificar_usuario, _viewer_info

log = structlog.get_logger()
router = APIRouter(prefix="/painel", tags=["assistente"])

# ASSISTENTE_BASE_URL só existe pra instância de teste (porta != 8002) não ler dado da produção.
_BASE = os.environ.get("ASSISTENTE_BASE_URL", "http://127.0.0.1:8002") + "/painel"
_DASH_EMP = "http://127.0.0.1:8014/api/empreendimentos"
# corretora_id → prefixo no Calendar da Acme (Pessoa3 aparece como "Pessoa")
_PREFIXO_CAL = {1: "Pessoa10", 2: "Pessoa11", 3: "Pessoa"}
# corretora_id → nome pro skill de ligações (registrar_ligacoes.py resolve alias)
_LIGACOES_CORR = {1: "Pessoa10", 2: "Pessoa11", 3: "Pessoa"}
# alias citado na pergunta → corretora_id (pro admin pedir "o dia da Pessoa12")
_CORRETORA_REF_ID = {
    "corretor": 1, "corretora_c": 1,
    "corretor": 2, "corretora_b": 2,
    "corretor": 3, "corretora_a": 3,
}
# corretora_id → nome curto usado na resposta ("esse é o dia da Pessoa12")
_CORRETORA_REF_NOME = {1: "Pessoa10", 2: "Pessoa11", 3: "Pessoa12"}
_CRIAR_VISITA = Path(__file__).resolve().parents[1] / "agents/assistente/skills/agenda"
_REGISTRAR_LIGACOES = Path(__file__).resolve().parents[1] / "agents/assistente/skills/funil"
_TZ_SP = ZoneInfo("America/Sao_Paulo")


def _semana_iso_sp() -> str:
    """ISO week da data corrente em São Paulo — formato YYYY-Www (ex: 2026-W27)."""
    y, w, _ = datetime.now(_TZ_SP).isocalendar()
    return f"{y}-W{w:02d}"


def _ligacoes_atual(corretora_id: int, semana: str) -> dict | None:
    """Lê a linha atual de ligacoes_semanais da corretora na semana (ou None)."""
    try:
        from sqlalchemy import text as _sql_text
        from app.db.session import SessionLocal
        with SessionLocal() as s:
            row = s.execute(
                _sql_text(
                    "SELECT ligacoes, atendeu, prosseguiu FROM ligacoes_semanais "
                    "WHERE corretora_id = :cid AND ano_semana_iso = :sem"
                ),
                {"cid": corretora_id, "sem": semana},
            ).first()
        if not row:
            return None
        return {"ligacoes": row[0], "atendeu": row[1], "prosseguiu": row[2]}
    except Exception as e:  # noqa: BLE001
        log.warning("assistente_ligacoes_atual_erro", erro=str(e)[:120])
        return None


def _achar_visita_agendada(corretora_id: int, cliente: str) -> dict | None:
    """Acha a visita AGENDADA mais próxima (data_hora <= amanhã) por nome ILIKE. Escopado."""
    try:
        from sqlalchemy import text as _sql_text
        from app.db.session import SessionLocal
        with SessionLocal() as s:
            row = s.execute(
                _sql_text(
                    "SELECT id, lead_nome, data_hora FROM visitas "
                    "WHERE corretora_id = :cid AND status = 'agendada' "
                    "AND lead_nome ILIKE :nome "
                    "AND data_hora <= (NOW() + interval '1 day') "
                    "ORDER BY data_hora DESC LIMIT 1"
                ),
                {"cid": corretora_id, "nome": f"%{cliente}%"},
            ).first()
        if not row:
            return None
        dh = row[2]
        quando = dh.astimezone(_TZ_SP).strftime("%d/%m %H:%M") if dh else "?"
        return {"id": row[0], "lead_nome": row[1], "quando": quando}
    except Exception as e:  # noqa: BLE001
        log.warning("assistente_achar_visita_erro", erro=str(e)[:120])
        return None


def _visitas_agendadas_recentes(corretora_id: int, limite: int = 5) -> list[dict]:
    """Lista as próximas/recentes visitas agendadas da corretora (pra resposta honesta)."""
    try:
        from sqlalchemy import text as _sql_text
        from app.db.session import SessionLocal
        with SessionLocal() as s:
            rows = s.execute(
                _sql_text(
                    "SELECT lead_nome, data_hora FROM visitas "
                    "WHERE corretora_id = :cid AND status = 'agendada' "
                    "ORDER BY data_hora DESC LIMIT :lim"
                ),
                {"cid": corretora_id, "lim": limite},
            ).all()
        out = []
        for nome, dh in rows:
            quando = dh.astimezone(_TZ_SP).strftime("%d/%m %H:%M") if dh else "?"
            out.append({"lead_nome": nome, "quando": quando})
        return out
    except Exception as e:  # noqa: BLE001
        log.warning("assistente_visitas_recentes_erro", erro=str(e)[:120])
        return []


def _get(path: str, user: str, params: dict | None = None) -> Any:
    try:
        with httpx.Client(timeout=20) as c:
            r = c.get(_BASE + path, headers={"X-Painel-User": user}, params=params or {})
            return r.json() if r.status_code == 200 else {}
    except Exception as e:  # noqa: BLE001
        log.warning("assistente_get_falha", path=path, erro=str(e)[:120])
        return {}


def _norm_txt(s: Any) -> str:
    """lowercase + sem acento + 'jd'→'jardim' (a planilha grafa 'AlÉM Ponte', 'Jd Europa'...)."""
    t = str(s or "").lower()
    t = "".join(ch for ch in unicodedata.normalize("NFKD", t) if not unicodedata.combining(ch))
    return re.sub(r"\bjd\b\.?", "jardim", t).strip()


def _corretora_ref_cid(ref: Any) -> int | None:
    """'Pessoa12'/'Pessoa3'/'a Pessoa10'... → corretora_id (ou None se não é do time)."""
    t = _norm_txt(ref)
    if not t:
        return None
    for palavra in t.split():
        cid = _CORRETORA_REF_ID.get(palavra)
        if cid:
            return cid
    return None


def _emp_tipo_alvo(tc: Any) -> str | None:
    """apto/apartamento(s)/apê → APTO · casa(s)/sobrado(s) → CASA · terreno/lote(amento) → TERR."""
    t = _norm_txt(tc)
    if not t:
        return None
    if t.startswith("ap"):
        return "APTO"
    if t.startswith(("cas", "sobrado")):
        return "CASA"
    if t.startswith(("ter", "lot")):
        return "TERR"
    return None


def _emp_quartos_alvo(q: Any) -> set[int] | None:
    """'2', 2, '2 ou 3', '2 e 3', '2-3', '2 a 3' → conjunto de quartos aceitos."""
    if q is None or q == "":
        return None
    if isinstance(q, int):
        return {q} if 0 < q < 10 else None
    txt = _norm_txt(q)
    digs = [int(d) for d in re.findall(r"\d", txt) if 0 < int(d) < 10]
    if not digs:
        return None
    if len(digs) >= 2 and re.search(r"\d\s*(?:-|a)\s*\d", txt):  # faixa "2-3"/"2 a 4"
        return set(range(min(digs), max(digs) + 1))
    return set(digs)


def _emp_quartos_item(e: dict) -> set[int] | None:
    """Quartos que o empreendimento OFERECE. Bruto '2-3' cobre {2,3} (quartos_norm=1º dígito esconde)."""
    bruto = str(e.get("quartos") or "")
    digs = [int(d) for d in re.findall(r"\d", bruto) if 0 < int(d) < 10]
    if digs:
        if "-" in bruto and len(digs) >= 2:
            return set(range(min(digs), max(digs) + 1))
        return set(digs)
    qn = e.get("quartos_norm")
    return {int(qn)} if qn else None


def _emp_entrega_ok(e: dict, entrega: Any) -> bool:
    """'pronto (pra morar)' → bucket pronto · 'lançamento/na planta' → lancamento · ano → entrega_fmt."""
    t = _norm_txt(entrega)
    if not t:
        return True
    if "pronto" in t or "morar" in t:
        return e.get("entrega_bucket") == "pronto"
    if "lanc" in t or "planta" in t:
        return e.get("entrega_bucket") == "lancamento"
    m = re.search(r"\b(?:20)?(2[5-9])\b", t)  # 2027, 27...
    if m:
        yy = m.group(1)
        fmt = _norm_txt(e.get("entrega_fmt"))
        return bool(re.search(rf"(?:20{yy}|/{yy}\b|\b{yy}\b)", fmt))
    return True  # critério que não entendi → não filtra


def _emp_frescor(payload: dict, out: dict) -> None:
    """Injeta selo de frescor/staleness no contexto — DEFENSIVO: campos podem não existir ainda."""
    at = payload.get("at") or payload.get("atualizado_em")
    if at:
        try:
            dt = datetime.fromisoformat(str(at).replace("Z", "+00:00"))
            horas = max(0, (datetime.now(dt.tzinfo) - dt).total_seconds() / 3600)
            if horas < 1:
                out["frescor"] = "catálogo de menos de 1h atrás"
            elif horas < 48:
                out["frescor"] = f"catálogo de {int(horas)}h atrás"
            else:
                out["frescor"] = f"catálogo de {int(horas // 24)} dias atrás"
        except Exception:  # noqa: BLE001
            pass
    if payload.get("stale"):
        out["atencao"] = ("dados possivelmente desatualizados — sugira confirmar "
                          "disponibilidade antes de oferecer")


def _empreendimentos(criterios: dict) -> dict:
    """Filtra o catálogo do dash (:8014). Retorna {itens, aviso?, frescor?, atencao?}."""
    try:
        with httpx.Client(timeout=20) as c:
            payload = c.get(_DASH_EMP).json() or {}
    except Exception:
        return {"itens": [], "erro": "catálogo indisponível agora"}
    itens = payload.get("itens", []) or []
    teto = criterios.get("preco_max")
    if teto and teto < 10000:  # "300 mil" às vezes vem como 300 → normaliza pra reais
        teto = teto * 1000
    tipo_alvo = _emp_tipo_alvo(criterios.get("tipo"))
    quartos_alvo = _emp_quartos_alvo(criterios.get("quartos"))
    bairro_alvo = _norm_txt(criterios.get("bairro"))
    entrega = criterios.get("entrega")

    def ok(e: dict, teto_: int | None) -> bool:
        if tipo_alvo and not e.get("tipo", "").upper().startswith(tipo_alvo):
            return False
        if bairro_alvo:
            local = _norm_txt(f"{e.get('bairro', '')} {e.get('zona', '')}")
            centro = bairro_alvo in ("centro", "central", "regiao central") and (
                "centro" in local or "central" in local)
            if bairro_alvo not in local and not centro:
                return False
        if quartos_alvo:
            oferece = _emp_quartos_item(e)
            if oferece and not (oferece & quartos_alvo):
                return False
        if not _emp_entrega_ok(e, entrega):
            return False
        if teto_ and (not e.get("preco_num") or e["preco_num"] > teto_):
            # com teto pedido, preço desconhecido não conta como "até X"
            return False
        return True

    res = [e for e in itens if ok(e, teto)]
    aviso = None
    if not res and teto:
        # 0 resultados → relaxa o teto em +10% antes de desistir
        res = [e for e in itens if ok(e, int(teto * 1.10))]
        if res:
            aviso = (f"nada dentro de R$ {int(teto):,} — mostrando opções até ~10% "
                     "acima do teto".replace(",", "."))
        else:
            # ainda 0 → alternativas mais próximas (mesmos filtros, ordenadas por preço)
            alt = sorted([e for e in itens if ok(e, None)],
                         key=lambda e: e.get("preco_num") or 10**12)
            if alt:
                res = alt
                aviso = ("nenhuma opção dentro do preço pedido — estas são as "
                         "alternativas mais próximas (acima do teto); avise a corretora do valor")
    if teto:
        # preço conhecido primeiro (item sem preço não é resposta pra "até X"), mais barato no topo
        res.sort(key=lambda e: e.get("preco_num") or 10**12)
    if quartos_alvo:
        # match explícito de quartos primeiro; sem info de quartos vai pro fim
        res.sort(key=lambda e: 0 if (_emp_quartos_item(e) or set()) & quartos_alvo else 1)
    # NUNCA mandar link de Drive pro LLM (regra inviolável — defesa em profundidade)
    out: dict[str, Any] = {"itens": [{k: v for k, v in e.items() if k != "drive"} for e in res[:8]]}
    if aviso:
        out["aviso"] = aviso
    _emp_frescor(payload, out)
    return out


_ROUTER_SYS = (
    "Você roteia a pergunta de uma CORRETORA imobiliária pra buscar o dado certo. "
    "Classifique a INTENÇÃO e extraia entidades. Responda APENAS JSON.\n"
    "intents: agendar (ela quer MARCAR uma visita: 'agendei o João sábado 10h', 'marca visita da Pessoa amanhã 15h no Bairro A') | "
    "anotar (REGISTRANDO algo sobre um lead SEM ser ligação: 'anota que quer 3 quartos', 'marca que sumiu', 'o Y pediu fotos') | "
    "mover (ela quer MUDAR A ETAPA de um lead no funil: 'move o João pro Follow-up', 'coloca a Pessoa em Venda ganha', 'passa o Pedro pra Proposta', 'joga a Corretor pro remarketing 30 dias') | "
    "ligacao (ela REPORTA UMA LIGAÇÃO pra um lead específico: 'liguei pra Pessoa8, não atendeu', 'falei com o Pedro no telefone, vai pensar', 'liguei pro João, caiu na caixa postal') | "
    "rascunho (ela quer um TEXTO pronto pra mandar/copiar pro lead: 'me dá uma mensagem pro João', 'o que respondo pra Elaine?', 'escreve uma resposta pro fulano', 'rascunha uma resposta pra Fernanda', 'como respondo o \"tá caro\" do X') | "
    "ligacoes (ela REPORTA os TOTAIS de ligações da semana, sem citar lead: 'fiz 12 ligações, 5 atenderam, 2 prosseguiram', '20 ligações hoje, 8 atenderam') | "
    "visita_status (ela informa se um lead COMPARECEU ou FUROU a visita: 'a Elaine compareceu', 'o João furou', 'a Pessoa não veio', 'o cliente deu no-show') | "
    "briefing (resumo da manhã / meu dia completo / 'me dá o briefing') | "
    "lead (PERGUNTA sobre um lead específico) | dia (o que fazer hoje / pendências) | "
    "numeros (desempenho/funil dela) | visitas (VER a agenda) | empreendimento (buscar imóvel pra oferecer) | "
    "coaching (como melhorar / abordagem / objeção) | outro. "
    "Se intent=agendar, preencha 'agenda' com cliente, data (YYYY-MM-DD resolvendo relativas pela data de hoje informada), hora (HH:MM 24h) e bairro. "
    "Se intent=mover, preencha 'mover' com cliente (nome do lead) e etapa (o nome da etapa destino como ela falou). "
    "Se intent=ligacao, preencha 'ligacao' com cliente, resultado ('atendeu' se falou com o lead; 'nao_atendeu' se não atendeu/não respondeu; 'caixa_postal' se caiu na caixa) e nota (o que rolou na ligação, ex: 'vai pensar' — ou null). "
    "Se intent=ligacoes, preencha 'ligacoes' com ligacoes(int total), atendeu(int) e prosseguiu(int) — se algum não foi dito, use null. "
    "Se intent=visita_status, preencha 'visita' com cliente (nome do lead) e status ('compareceu' se veio/apareceu/compareceu; 'no_show' se furou/não veio/deu bolo/no-show). "
    "IMPORTANTE: AFIRMA fato/ação → anotar/agendar/mover/ligacao/ligacoes/visita_status; PERGUNTA → lead/visitas; pede TEXTO pra mandar → rascunho. "
    "Ligação pra UM lead com nome → ligacao; totais da semana sem lead → ligacoes. "
    "Se a pergunta cita uma CORRETORA do time pelo nome ('o que a Pessoa12 tem hoje', 'dia da Pessoa10'), "
    "preencha corretora_ref com o nome citado — corretoras do time: Pessoa12/Pessoa3, Pessoa10/Pessoa5, Pessoa11/Pessoa1. "
    "Nome de LEAD/cliente NÃO é corretora_ref."
)
_ROUTER_SCHEMA = {
    "intent": "agendar | anotar | mover | ligacao | rascunho | ligacoes | visita_status | briefing | lead | dia | numeros | visitas | empreendimento | coaching | outro",
    "lead_ref": "nome ou telefone do lead citado (ou null)",
    "corretora_ref": "nome da CORRETORA do time citada na pergunta ('Pessoa12', 'Pessoa3', 'Pessoa10', 'Pessoa11'...) ou null",
    "emp": "objeto {tipo?, bairro?, quartos?(int OU texto tipo '2 ou 3'), preco_max?(int reais), entrega?('pronto' | 'lancamento' | ano tipo '2027')} se intent=empreendimento, senão null",
    "agenda": "objeto {cliente, data(YYYY-MM-DD), hora(HH:MM), bairro?} se intent=agendar, senão null",
    "mover": "objeto {cliente, etapa} se intent=mover, senão null",
    "ligacao": "objeto {cliente, resultado('atendeu'|'nao_atendeu'|'caixa_postal'), nota?} se intent=ligacao, senão null",
    "ligacoes": "objeto {ligacoes(int), atendeu(int), prosseguiu(int)} se intent=ligacoes, senão null",
    "visita": "objeto {cliente, status('compareceu'|'no_show')} se intent=visita_status, senão null",
}


# ── Mover etapa (reusa o caminho do /painel/crm/mover) ────────────────────────
# Destinos que a Assistente SEMPRE confirma com destaque antes de executar.
_ETAPAS_DESTRUTIVAS = {"Venda ganha", "Venda perdida"}
# Resultados válidos de uma ligação registrada por lead (lead_events tipo='ligacao').
_LIGACAO_RESULTADOS = ("atendeu", "nao_atendeu", "caixa_postal")
_LIGACAO_LABEL = {
    "atendeu": "✅ atendeu",
    "nao_atendeu": "📵 não atendeu",
    "caixa_postal": "📮 caixa postal",
}


def _resolver_etapa_mover(etapa: Any) -> tuple[str, str] | None:
    """Texto da corretora → (funil, stage_destino) ou None.

    Principal: reusa os aliases canônicos do lead_spine (stage_canonico) — 'follow up',
    'negociação', 'venda'... viram um dos 8 canônicos. Remarketing: 'base' ou
    '30/60/90/120/150 (dias)' viram etapa de nutrição. NÃO cria etapa nova.
    """
    from app.services.lead_spine import stage_canonico
    from app.services.nutricao import FUNIL_REMARKETING, STAGES_NUTRICAO

    t = _norm_txt(etapa)
    if not t:
        return None
    for cand in (t, t.replace(" ", "-"), t.replace("-", " ")):
        canon = stage_canonico(cand)
        if canon:
            return ("principal", canon)
    if "base" in t:
        return (FUNIL_REMARKETING, "Base")
    m = re.search(r"\b(30|60|90|120|150)\b", t)
    if m:
        nome = f"{m.group(1)} dias"
        if nome in STAGES_NUTRICAO:
            return (FUNIL_REMARKETING, nome)
    return None


def _leads_spine_por_nome(nome: str, corretora_id: int | None) -> list[dict]:
    """Busca leads na ESPINHA canônica (tabela leads, tenant 1) por nome ILIKE.

    Escopo: corretora_id != None → só leads dela (owner_id = id interno OU
    kommo_user_id — mesma regra do board). None (admin) → todos. Até 6, mais
    recentes primeiro. Retorna [{id, nome, etapa}].
    """
    try:
        from app.api.painel import CORRETORA_KOMMO_USER
        from app.db.models import Lead, PipelineStage
        from app.db.session import SessionLocal
        from app.services.lead_spine import DEFAULT_TENANT_ID

        with SessionLocal() as s:
            q = (
                s.query(Lead.id, Lead.nome, PipelineStage.nome)
                .outerjoin(PipelineStage, Lead.stage_id == PipelineStage.id)
                .filter(Lead.tenant_id == DEFAULT_TENANT_ID,
                        Lead.nome.ilike(f"%{nome}%"))
            )
            if corretora_id is not None:
                permitidos = {int(corretora_id)}
                kuid = CORRETORA_KOMMO_USER.get(int(corretora_id))
                if kuid is not None:
                    permitidos.add(kuid)
                q = q.filter(Lead.owner_id.in_(permitidos))
            rows = (
                q.order_by(Lead.updated_at.desc().nullslast(), Lead.id.desc())
                .limit(6).all()
            )
        return [{"id": r[0], "nome": r[1], "etapa": r[2]} for r in rows]
    except Exception as e:  # noqa: BLE001
        log.warning("assistente_leads_spine_erro", erro=str(e)[:120])
        return []


def _escolher_lead_spine(achados: list[dict], citado: str) -> dict | None:
    """1 achado → ele. Vários → só se EXATAMENTE um casa o nome citado por inteiro."""
    if len(achados) == 1:
        return achados[0]
    exatos = [a for a in achados if _norm_txt(a.get("nome")) == _norm_txt(citado)]
    return exatos[0] if len(exatos) == 1 else None


_LIGACOES_SYS = (
    "Você extrai números de um reporte de LIGAÇÕES de uma corretora imobiliária. "
    "Ela diz quantas ligou, quantas atenderam e quantas prosseguiram (avançaram no funil). "
    "Extraia só o que ela disse; se algum número não foi dito, use null. Responda APENAS JSON."
)
_LIGACOES_SCHEMA = {
    "ligacoes": "int total de ligações feitas (ou null)",
    "atendeu": "int quantas foram atendidas (ou null)",
    "prosseguiu": "int quantas prosseguiram/avançaram (ou null)",
}


def _parse_ligacoes(texto: str) -> dict:
    """Lê o texto da corretora → {ligacoes, atendeu, prosseguiu, valido, erro}.

    Valida: todos int >= 0, atendeu <= ligacoes, prosseguiu <= atendeu.
    """
    out: dict[str, Any] = {"ligacoes": None, "atendeu": None, "prosseguiu": None,
                           "valido": False, "erro": None}
    try:
        raw, _ = get_llm_provider_assistente().complete(_LIGACOES_SYS, texto, _LIGACOES_SCHEMA)
        p = _json.loads(raw)
    except Exception as e:  # noqa: BLE001
        log.warning("assistente_ligacoes_parse", erro=str(e)[:120])
        out["erro"] = "não entendi os números"
        return out

    def _int(v: Any) -> int | None:
        try:
            return int(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    lig, atd, pro = _int(p.get("ligacoes")), _int(p.get("atendeu")), _int(p.get("prosseguiu"))
    # atendeu/prosseguiu default 0 quando só o total veio
    if lig is None:
        out["erro"] = "não achei o total de ligações"
        return out
    atd = 0 if atd is None else atd
    pro = 0 if pro is None else pro
    if lig < 0 or atd < 0 or pro < 0:
        out["erro"] = "número negativo não rola"
        return out
    if atd > lig:
        out["erro"] = f"atenderam ({atd}) não pode ser mais que ligações ({lig})"
        return out
    if pro > atd:
        out["erro"] = f"prosseguiram ({pro}) não pode ser mais que atenderam ({atd})"
        return out
    out.update({"ligacoes": lig, "atendeu": atd, "prosseguiu": pro, "valido": True})
    return out


def _log_conversa(
    *,
    corretora_id: int | None,
    usuario: str,
    intent: str | None,
    pergunta: str | None,
    resposta_resumo: str | None,
    acao_tipo: str | None = None,
    acao_executada: bool = False,
    latencia_ms: int | None = None,
) -> None:
    """Grava a interação em assistente_conversas — BEST-EFFORT. Nunca quebra o fluxo.

    Guarda anti-teste embutida (padrão da casa): sob pytest não escreve no banco.
    """
    if os.environ.get("PYTEST_CURRENT_TEST"):
        log.info("assistente_conversa_bloqueado_pytest", intent=intent, acao=acao_tipo)
        return
    try:
        from app.db.models import AssistenteConversa
        from app.db.session import SessionLocal
        with SessionLocal() as s:
            s.add(AssistenteConversa(
                corretora_id=corretora_id,
                usuario=usuario,
                intent=intent,
                pergunta=(pergunta or "")[:2000] or None,
                resposta_resumo=(resposta_resumo or "")[:300] or None,
                acao_tipo=acao_tipo,
                acao_executada=acao_executada,
                latencia_ms=latencia_ms,
            ))
            s.commit()
    except Exception as e:  # noqa: BLE001
        log.warning("assistente_log_conversa_falha", erro=str(e)[:160])


def _persona(nome: str) -> str:
    from app.services.atendimento_brain import brain
    return (
        brain(sugerir=True) + "\n\n"
        f"Você é a Assistente, assistente da corretora {nome} da Acme Imóveis (Cidade). "
        "Fala como uma colega prática que ajuda: curto, direto, PT-BR, caloroso mas objetivo. "
        "Use SOMENTE os DADOS fornecidos abaixo; se o dado não veio, diga que não encontrou (não invente NADA). "
        "Você é assistente INTERNA — nunca escreve como se fosse falar com o cliente, você fala COM a corretora. "
        "Quando listar leads/imóveis, seja enxuta (bullets curtos). Se tiver sugestão de próximo passo, dá em 1 linha. "
        "NUNCA cole links de Drive/material na resposta — se o imóvel tem material, diga só 'tem material' (a corretora abre pelo painel 🏢 Empreendimentos)."
    )


def _rascunho_persona(nome: str, lead_nome: str | None) -> str:
    """Persona pro RASCUNHO: gera a mensagem que a corretora vai COPIAR e mandar pro lead.

    Reusa o cérebro de atendimento (brain sugerir=True traz a regra do respiro +
    como sugerir). Aqui o texto é PRA O LEAD (1ª pessoa da corretora), não pra ela.
    """
    from app.services.atendimento_brain import brain
    alvo = lead_nome or "o lead"
    return (
        brain(sugerir=True) + "\n\n"
        f"Você é a Assistente ajudando a corretora {nome} da Acme Imóveis (Cidade). "
        f"Ela quer uma MENSAGEM pronta pra mandar pro lead {alvo}. "
        "ESCREVA A MENSAGEM COMO SE FOSSE A CORRETORA falando com o lead (1ª pessoa dela, WhatsApp, PT-BR, calorosa e natural). "
        "Baseie-se na conversa e no contexto fornecidos; não invente fatos que não estão nos dados. "
        "Aplique a regra do respiro (frases curtas, uma ideia por linha, linha em branco entre elas). "
        "Responda SÓ o texto da mensagem — sem aspas, sem 'aqui está', sem explicação, sem assinar. "
        "Não coloque link de Drive/material. Se faltar dado, faça uma mensagem que abre a conversa sem inventar."
    )


_ARTE_SYS = (
    "Você extrai os dados de um imóvel do texto de uma corretora pra montar uma ARTE. "
    "Use SÓ o que ela escreveu (não invente). Se ela citou o NOME de um empreendimento, ponha em titulo; "
    "se ela só descreveu (ex: 'casa bairro 200 mil 70m 3 quartos'), monte um titulo curto tipo 'Casa em Bairro A'. "
    "Responda APENAS JSON."
)
_ARTE_SCHEMA = {
    "titulo": "nome do empreendimento OU título curto (ex: 'Casa em Bairro A')",
    "tipo": "casa | apto | terreno | null",
    "bairro": "bairro citado (ou null)",
    "preco": "preço formatado tipo 'R$ 200 mil' (ou null)",
    "quartos": "int de quartos (ou null)",
    "metros": "int de m² (ou null)",
    "entrega": "Pronto | Lançamento | uma data | null (só se citado)",
}


def _dados_arte(texto: str) -> dict:
    """Lê o texto da corretora → dados do card. Enriquece da planilha SÓ se ela citou um empreendimento de lá."""
    dados = {"nome": (texto or "Imóvel").strip()[:40]}
    try:
        raw, _ = get_llm_provider_assistente().complete(_ARTE_SYS, texto, _ARTE_SCHEMA)
        p = _json.loads(raw)
        dados = {
            "nome": p.get("titulo") or dados["nome"],
            "tipo": p.get("tipo"),
            "bairro": p.get("bairro"),
            "preco_fmt": p.get("preco"),
            "quartos": p.get("quartos"),
            "metros": p.get("metros"),
            "entrega_fmt": p.get("entrega"),
        }
    except Exception as e:  # noqa: BLE001
        log.warning("assistente_arte_parse", erro=str(e)[:120])
    # enriquece SÓ se ela citou um empreendimento da planilha (preenche buracos)
    try:
        t = (texto or "").lower()
        with httpx.Client(timeout=12) as c:
            itens = (c.get(_DASH_EMP).json() or {}).get("itens", [])
        for e in itens:
            nome = (e.get("empreendimento") or "").lower()
            if nome and len(nome) > 4 and nome in t:
                dados["nome"] = dados.get("nome") or e.get("empreendimento")
                dados["preco_fmt"] = dados.get("preco_fmt") or e.get("preco_fmt")
                dados["bairro"] = dados.get("bairro") or e.get("bairro")
                dados["zona"] = e.get("zona")
                dados["entrega_fmt"] = dados.get("entrega_fmt") or e.get("entrega_fmt")
                dados["quartos"] = dados.get("quartos") or e.get("quartos_norm")
                break
    except Exception:
        pass
    return dados


@router.post("/assistente/arte/dados")
def assistente_arte_dados(
    payload: dict = Body(...),
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Só LÊ os dados do texto pra corretora CONFIRMAR antes de gerar a arte."""
    return {"dados": _dados_arte(payload.get("texto") or "")}


@router.post("/assistente/arte")
def assistente_arte(
    payload: dict = Body(...),
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Fase 2: recebe foto + dados (já confirmados) OU texto → compõe arte branded Acme."""
    from app.services.arte_empreend import compor
    img_b64 = payload.get("imagem_b64") or ""
    if not img_b64:
        return {"erro": "sem imagem"}
    dados = payload.get("dados") or _dados_arte(payload.get("texto") or payload.get("empreendimento") or "")
    try:
        foto = base64.b64decode(img_b64.split(",")[-1])
        png = compor(foto, dados)
        return {
            "png_b64": "data:image/png;base64," + base64.b64encode(png).decode(),
            "empreendimento": dados.get("nome"),
            "casou": bool(dados.get("preco_fmt") or dados.get("bairro")),
        }
    except Exception as e:  # noqa: BLE001
        log.warning("assistente_arte_erro", erro=str(e)[:160])
        return {"erro": f"não consegui montar a arte: {str(e)[:80]}"}


@router.post("/assistente/acao")
def assistente_acao(
    payload: dict = Body(...),
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Executa uma ação JÁ CONFIRMADA pela corretora (Fase 3). Escopado + logado."""
    user = _identificar_usuario(x_painel_user)
    t0 = time.monotonic()
    result = _executar_acao(user, payload)
    # histórico best-effort — nunca quebra o fluxo
    _log_conversa(
        corretora_id=_viewer_info(user).get("corretora_id"),
        usuario=user,
        intent=payload.get("tipo"),
        pergunta=payload.get("nota") or payload.get("resumo") or None,
        resposta_resumo=result.get("msg"),
        acao_tipo=payload.get("tipo"),
        acao_executada=bool(result.get("ok")),
        latencia_ms=int((time.monotonic() - t0) * 1000),
    )
    return result


def _upsert_ligacoes(user: str, payload: dict) -> dict[str, Any]:
    """UPSERT em ligacoes_semanais via skill registrar_ligacoes.py. Guarda anti-teste."""
    cid = _viewer_info(user).get("corretora_id")
    if not cid:
        return {"ok": False, "msg": "Como gestão você vê tudo, mas registrar ligações é pela corretora — peça pra ela reportar. Em breve libero 😉"}
    corr = _LIGACOES_CORR.get(int(cid))
    lig, atd, pro = payload.get("ligacoes"), payload.get("atendeu"), payload.get("prosseguiu")
    semana = payload.get("semana") or _semana_iso_sp()
    if corr is None or lig is None or atd is None or pro is None:
        return {"ok": False, "msg": "faltou algum número da semana."}
    if os.environ.get("PYTEST_CURRENT_TEST"):
        log.info("assistente_ligacoes_bloqueado_pytest", corr=corr, semana=semana)
        return {"ok": True, "msg": f"Registrado! 📞 {lig}/{atd}/{pro} na semana {semana}. (pytest)"}
    cmd = [sys.executable, str(_REGISTRAR_LIGACOES / "registrar_ligacoes.py"),
           "--corretora", corr, "--ligacoes", str(lig), "--atendeu", str(atd),
           "--prosseguiu", str(pro), "--semana", semana]
    try:
        r = subprocess.run(cmd, cwd=str(_REGISTRAR_LIGACOES), capture_output=True, text=True, timeout=45)
        if r.returncode == 0:
            log.info("assistente_acao_ligacoes", user=user, semana=semana, lig=lig, atd=atd, pro=pro)
            return {"ok": True, "msg": f"Registrado! 📞 {lig} ligações · {atd} atenderam · {pro} prosseguiram na semana {semana}. Já tá na Prospecção 📡"}
        log.warning("assistente_ligacoes_falhou", rc=r.returncode, out=(r.stdout + r.stderr)[-300:])
        return {"ok": False, "msg": f"Não consegui registrar 😿 {(r.stdout + r.stderr).strip()[-160:]}"}
    except Exception as e:  # noqa: BLE001
        log.warning("assistente_ligacoes_erro", erro=str(e)[:120])
        return {"ok": False, "msg": "Deu erro ao registrar as ligações 😿 tenta de novo?"}


def _atualizar_visita_status(user: str, payload: dict) -> dict[str, Any]:
    """UPDATE status da visita (compareceu|no_show). Escopado + guarda anti-teste."""
    cid = _viewer_info(user).get("corretora_id")
    if not cid:
        return {"ok": False, "msg": "Como gestão você vê tudo, mas marcar visita é pela corretora — em breve libero 😉"}
    visita_id = payload.get("visita_id")
    status = payload.get("status")
    if not visita_id or status not in ("compareceu", "no_show"):
        return {"ok": False, "msg": "faltou qual visita ou o status."}
    if os.environ.get("PYTEST_CURRENT_TEST"):
        log.info("assistente_visita_status_bloqueado_pytest", visita_id=visita_id, status=status)
        return {"ok": True, "msg": f"Anotado! {'✅ compareceu' if status == 'compareceu' else '❌ no-show'} (pytest)"}
    try:
        from sqlalchemy import text as _sql_text
        from app.db.session import SessionLocal
        with SessionLocal() as s:
            # UPDATE escopado: só a visita da própria corretora
            row = s.execute(
                _sql_text(
                    "UPDATE visitas SET status = :st, atualizado_em = NOW(), "
                    "status_atualizado_por_tel = :who "
                    "WHERE id = :vid AND corretora_id = :cid RETURNING lead_nome"
                ),
                {"st": status, "who": user, "vid": int(visita_id), "cid": int(cid)},
            ).first()
            s.commit()
        if not row:
            return {"ok": False, "msg": "Essa visita não é sua ou não existe mais 🔒"}
        nome = row[0]
        emoji = "✅" if status == "compareceu" else "❌"
        label = "compareceu" if status == "compareceu" else "não veio (no-show)"
        log.info("assistente_acao_visita_status", user=user, visita_id=visita_id, status=status)
        return {"ok": True, "msg": f"Anotado! {emoji} {nome} {label} — atualizei a Agenda."}
    except Exception as e:  # noqa: BLE001
        log.warning("assistente_visita_status_erro", erro=str(e)[:120])
        return {"ok": False, "msg": "Deu erro ao atualizar a visita 😿 tenta de novo?"}


def _mover_lead(user: str, payload: dict) -> dict[str, Any]:
    """Move a etapa do lead REUSANDO o endpoint /painel/crm/mover (POST interno
    com o mesmo X-Painel-User): mesma auth/escopo (corretora só move lead dela,
    admin move qualquer um), resolução funil-aware (principal/remarketing),
    lead_events 'stage_manual_v2' (protege da sombra do Kommo por 72h) e enroll
    de cadência no Follow-up. NÃO duplica lógica de mover aqui."""
    lead_id = payload.get("lead_id")
    destino = (payload.get("stage_destino") or "").strip()
    funil = (payload.get("funil") or "principal").strip()
    nome = payload.get("lead_nome") or "lead"
    if not lead_id or not destino:
        return {"ok": False, "msg": "faltou o lead ou a etapa destino."}
    if os.environ.get("PYTEST_CURRENT_TEST"):
        log.info("assistente_mover_bloqueado_pytest", lead_id=lead_id, destino=destino)
        return {"ok": True, "msg": f"Movido! 🎯 {nome} agora tá em **{destino}**. (pytest)"}
    try:
        with httpx.Client(timeout=30) as c:
            r = c.post(f"{_BASE}/crm/mover", headers={"X-Painel-User": user},
                       json={"lead_id": int(lead_id), "stage_destino": destino,
                             "funil": funil})
        if r.status_code == 200:
            d = r.json() or {}
            if d.get("noop"):
                return {"ok": True, "msg": f"O {nome} já tava em **{destino}** 😉 nada a mudar."}
            extra = " Liguei a cadência de follow-up também 🔁" if d.get("cadencia_ligada") else ""
            log.info("assistente_acao_mover", user=user, lead_id=lead_id,
                     destino=destino, funil=funil)
            return {"ok": True, "msg": f"Movido! 🎯 {nome} agora tá em **{destino}**.{extra}"}
        if r.status_code == 403:
            return {"ok": False, "msg": "Esse lead não é seu, não posso mover 🔒"}
        if r.status_code == 404:
            return {"ok": False, "msg": "Não achei esse lead no funil 😿"}
        if r.status_code == 400:
            return {"ok": False, "msg": "Não reconheci essa etapa 😿 confere o nome dela?"}
        return {"ok": False, "msg": f"Não consegui mover ({r.status_code})."}
    except Exception as e:  # noqa: BLE001
        log.warning("assistente_mover_erro", erro=str(e)[:120])
        return {"ok": False, "msg": "Deu erro ao mover 😿 tenta de novo?"}


def _registrar_ligacao(user: str, payload: dict) -> dict[str, Any]:
    """Grava lead_events(tipo='ligacao', payload={resultado, nota, quem}) no lead
    da ESPINHA. Escopado: corretora só registra em lead dela; admin em qualquer
    um. Distinto do 'ligacoes' (totais semanais em ligacoes_semanais)."""
    lead_id = payload.get("lead_id")
    resultado = payload.get("resultado")
    nota = (payload.get("nota") or "").strip() or None
    if not lead_id or resultado not in _LIGACAO_RESULTADOS:
        return {"ok": False, "msg": "faltou o lead ou o resultado da ligação (atendeu / não atendeu / caixa postal)."}
    nome = payload.get("lead_nome") or "lead"
    label = _LIGACAO_LABEL[resultado]
    if os.environ.get("PYTEST_CURRENT_TEST"):
        log.info("assistente_ligacao_bloqueado_pytest", lead_id=lead_id, resultado=resultado)
        return {"ok": True, "msg": f"Registrado! 📞 {nome}: {label}. (pytest)"}
    try:
        from app.api.painel import CORRETORA_KOMMO_USER
        from app.db.models import Lead
        from app.db.session import SessionLocal
        from app.services.lead_spine import DEFAULT_TENANT_ID, _registrar_evento

        cid = _viewer_info(user).get("corretora_id")
        with SessionLocal() as s:
            lead = (
                s.query(Lead)
                .filter(Lead.tenant_id == DEFAULT_TENANT_ID, Lead.id == int(lead_id))
                .first()
            )
            if lead is None:
                return {"ok": False, "msg": "Não achei esse lead no funil 😿"}
            if cid is not None:  # corretora: só lead dela (admin passa direto)
                permitidos = {int(cid)}
                kuid = CORRETORA_KOMMO_USER.get(int(cid))
                if kuid is not None:
                    permitidos.add(kuid)
                if lead.owner_id is None or int(lead.owner_id) not in permitidos:
                    return {"ok": False, "msg": "Esse lead não é seu, não posso registrar 🔒"}
            nome = lead.nome or nome
            _registrar_evento(
                s, DEFAULT_TENANT_ID, lead.id, "ligacao",
                {"resultado": resultado, "nota": nota, "quem": user},
            )
            s.commit()
        log.info("assistente_acao_ligacao", user=user, lead_id=lead_id, resultado=resultado)
        sufixo = f" Anotei: “{nota}”." if nota else ""
        return {"ok": True, "msg": f"Registrado! 📞 Ligação pro {nome}: {label}.{sufixo}"}
    except Exception as e:  # noqa: BLE001
        log.warning("assistente_ligacao_erro", erro=str(e)[:120])
        return {"ok": False, "msg": "Deu erro ao registrar a ligação 😿 tenta de novo?"}


def _executar_acao(user: str, payload: dict) -> dict[str, Any]:
    tipo = payload.get("tipo")
    if tipo == "ligacoes":
        return _upsert_ligacoes(user, payload)
    if tipo == "visita_status":
        return _atualizar_visita_status(user, payload)
    if tipo == "mover":
        return _mover_lead(user, payload)
    if tipo == "ligacao":
        return _registrar_ligacao(user, payload)
    if tipo == "anotar":
        lead_id = payload.get("lead_id")
        nota = (payload.get("nota") or "").strip()
        if not lead_id or len(nota) < 2:
            return {"ok": False, "msg": "faltou o lead ou o texto."}
        try:
            with httpx.Client(timeout=45) as c:
                r = c.post(f"{_BASE}/atendimento/{lead_id}/observacao",
                           headers={"X-Painel-User": user}, json={"texto": nota})
            if r.status_code == 200:
                ia = (r.json() or {}).get("ia", {}) or {}
                extras = []
                if ia.get("temperatura"):
                    extras.append(f"marquei como {ia['temperatura']}")
                if ia.get("descartar"):
                    extras.append("tirei da fila (descartado)")
                msg = "Anotado! 📝 Registrei no lead" + (f" e {', '.join(extras)}" if extras else "") + "."
                log.info("assistente_acao_anotar", user=user, lead_id=lead_id)
                return {"ok": True, "msg": msg}
            if r.status_code == 403:
                return {"ok": False, "msg": "Esse lead não é seu, não posso registrar 🔒"}
            return {"ok": False, "msg": f"Não consegui anotar ({r.status_code})."}
        except Exception as e:  # noqa: BLE001
            log.warning("assistente_acao_erro", erro=str(e)[:120])
            return {"ok": False, "msg": "Deu erro ao anotar 😿 tenta de novo?"}

    if tipo == "agendar":
        cid = _viewer_info(user).get("corretora_id")
        if not cid:
            return {"ok": False, "msg": "Como gestão você vê tudo, mas agendar pela dona ainda não — peça pra corretora agendar. Em breve libero 😉"}
        prefixo = _PREFIXO_CAL.get(int(cid))
        cli, data, hora = payload.get("cliente"), payload.get("data"), payload.get("hora")
        bairro = payload.get("bairro")
        if not (prefixo and cli and data and hora):
            return {"ok": False, "msg": "faltou algum dado da visita."}
        cmd = [sys.executable, str(_CRIAR_VISITA / "criar_visita.py"),
               "--corretora", prefixo, "--cliente", cli, "--data", data, "--hora", hora,
               "--origem", "crm", "--registrado-por-telefone", f"crm:{user}"]
        if bairro:
            cmd += ["--bairro", bairro]
        try:
            r = subprocess.run(cmd, cwd=str(_CRIAR_VISITA), capture_output=True, text=True, timeout=70)
            if r.returncode == 0:
                log.info("assistente_acao_agendar", user=user, cliente=cli, data=data, hora=hora)
                return {"ok": True, "msg": f"Agendado! 📅 Visita do {cli} em {data} às {hora} — já tá na sua Agenda."}
            log.warning("assistente_agendar_falhou", rc=r.returncode, out=(r.stdout + r.stderr)[-300:])
            return {"ok": False, "msg": f"Não consegui agendar 😿 {(r.stdout + r.stderr).strip()[-160:]}"}
        except Exception as e:  # noqa: BLE001
            log.warning("assistente_agendar_erro", erro=str(e)[:120])
            return {"ok": False, "msg": "Deu erro ao agendar 😿 tenta de novo?"}

    return {"ok": False, "msg": "Ação desconhecida."}


@router.post("/assistente")
def assistente(
    payload: dict = Body(...),
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    user = _identificar_usuario(x_painel_user)
    viewer = _viewer_info(user)
    nome = viewer.get("corretora_label") or viewer.get("corretora") or user
    pergunta = (payload.get("pergunta") or "").strip()
    if not pergunta:
        return {"resposta": "Pode perguntar 🐱 — tipo 'como tá o lead João?', 'o que faço hoje?', 'tem apto 2 quartos no Bairro A até 300k?'"}

    t0 = time.monotonic()
    result = _responder(user, viewer, nome, pergunta)
    # histórico best-effort — nunca quebra o fluxo
    _log_conversa(
        corretora_id=viewer.get("corretora_id"),
        usuario=user,
        intent=result.get("intent"),
        pergunta=pergunta,
        resposta_resumo=result.get("resposta"),
        acao_tipo=(result.get("acao") or {}).get("tipo") if isinstance(result.get("acao"), dict) else None,
        acao_executada=False,  # /assistente só propõe; execução real é no /assistente/acao
        latencia_ms=int((time.monotonic() - t0) * 1000),
    )
    return result


# ── Áudio pra Assistente (a corretora fala em vez de digitar) ────────────────
# Teto de 2 min de fala. Como não dá pra medir duração do webm sem decodificar,
# usamos o tamanho como proxy: opus a ~32kbps ⇒ 2min ≈ 480KB. 3MB dá folga
# larga pra bitrate maior sem deixar passar áudio de 10 minutos.
_AUDIO_MAX_BYTES = 3 * 1024 * 1024
_AUDIO_MIN_BYTES = 1024  # menos que isso é clique sem fala

# Mensagem única pra qualquer falha de transcrição (Whisper do Mac fora,
# timeout, áudio ininteligível): a corretora não precisa ver erro técnico.
_AUDIO_ERRO_MSG = "Não consegui te ouvir agora 😿 escreve pra mim?"


@router.post("/assistente/audio")
async def assistente_audio(
    audio: UploadFile = File(...),
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Recebe o áudio gravado no painel, transcreve e responde como se fosse texto.

    Reusa `transcrever_audio` (mesmo provider whisper_mac/gemini que transcreve
    áudio de lead) e depois `_responder` — a lógica de conversa não é duplicada.
    O áudio NÃO é gravado em disco: fica só em memória durante a requisição.
    """
    user = _identificar_usuario(x_painel_user)
    viewer = _viewer_info(user)
    nome = viewer.get("corretora_label") or viewer.get("corretora") or user

    audio_bytes = await audio.read()
    tamanho = len(audio_bytes)

    if tamanho < _AUDIO_MIN_BYTES:
        return {"transcricao": None, "resposta": "O áudio veio vazio 😿 segura o botão enquanto fala e solta no fim."}
    if tamanho > _AUDIO_MAX_BYTES:
        return {
            "transcricao": None,
            "resposta": "Esse áudio ficou muito longo pra mim (o limite é ~2 minutos). Manda em pedaços menores?",
        }

    mime_type = audio.content_type or "audio/webm"
    t0 = time.monotonic()
    try:
        from app.services.transcricao_service import transcrever_audio
        resultado = await run_in_threadpool(transcrever_audio, audio_bytes, mime_type)
    except Exception as e:  # noqa: BLE001
        log.warning("assistente_audio_transcricao_excecao", user=user, erro=str(e)[:160])
        return {"transcricao": None, "resposta": _AUDIO_ERRO_MSG}

    pergunta = (resultado.get("texto") or "").strip()
    if resultado.get("erro") or not pergunta:
        log.warning(
            "assistente_audio_transcricao_falhou",
            user=user,
            bytes=tamanho,
            erro=str(resultado.get("erro"))[:160],
        )
        return {"transcricao": None, "resposta": _AUDIO_ERRO_MSG}

    log.info(
        "assistente_audio_transcrito",
        user=user,
        bytes=tamanho,
        modelo=resultado.get("modelo"),
        texto_len=len(pergunta),
        elapsed_s=round(time.monotonic() - t0, 2),
    )

    result = await run_in_threadpool(_responder, user, viewer, nome, pergunta)
    _log_conversa(
        corretora_id=viewer.get("corretora_id"),
        usuario=user,
        intent=result.get("intent"),
        pergunta=f"[áudio] {pergunta}",
        resposta_resumo=result.get("resposta"),
        acao_tipo=(result.get("acao") or {}).get("tipo") if isinstance(result.get("acao"), dict) else None,
        acao_executada=False,
        latencia_ms=int((time.monotonic() - t0) * 1000),
    )
    # A transcrição volta pra tela como a mensagem "enviada" — se o Whisper
    # entendeu errado, a corretora vê na hora.
    return {**result, "transcricao": pergunta}


def _responder(user: str, viewer: dict, nome: str, pergunta: str) -> dict[str, Any]:
    prov = get_llm_provider_assistente()

    # 1) ROTEADOR
    intent, lead_ref, emp, agenda = "outro", None, {}, {}
    ligacoes_ent, visita_ent = {}, {}
    mover_ent, ligacao_ent = {}, {}
    corretora_ref = None
    hoje = datetime.now().strftime("%Y-%m-%d (%A)")
    try:
        raw, _ = prov.complete(_ROUTER_SYS, f"[hoje é {hoje}] {pergunta}", _ROUTER_SCHEMA)
        d = _json.loads(raw)
        intent = d.get("intent") or "outro"
        lead_ref = d.get("lead_ref")
        corretora_ref = d.get("corretora_ref")
        emp = d.get("emp") or {}
        agenda = d.get("agenda") or {}
        ligacoes_ent = d.get("ligacoes") or {}
        visita_ent = d.get("visita") or {}
        mover_ent = d.get("mover") or {}
        ligacao_ent = d.get("ligacao") or {}
    except Exception as e:  # noqa: BLE001
        log.warning("assistente_router_erro", erro=str(e)[:120])

    # Escopo por corretora citada ("o que a Pessoa12 tem hoje") — só muda algo pra ADMIN.
    is_admin = viewer.get("role") == "admin"
    ref_cid = _corretora_ref_cid(corretora_ref)
    ref_nome = _CORRETORA_REF_NOME.get(ref_cid) if ref_cid else None

    # 1.3) REPORTE "ligacoes" → parse + valida + devolve CONFIRMAÇÃO (não grava ainda)
    if intent == "ligacoes":
        cid = viewer.get("corretora_id")
        if not cid:
            return {"resposta": "Como gestão você vê tudo, mas reportar ligações é pela corretora — em breve libero 😉", "intent": intent}
        p = _parse_ligacoes(pergunta)
        if not p["valido"]:
            return {"resposta": f"Não consegui registrar: {p.get('erro') or 'não entendi os números'}. Ex: 'fiz 12 ligações, 5 atenderam, 2 prosseguiram'.", "intent": intent}
        semana = _semana_iso_sp()
        anterior = _ligacoes_atual(int(cid), semana)
        prefixo = ""
        if anterior:
            prefixo = (f"Essa semana já tem {anterior['ligacoes']}/{anterior['atendeu']}/{anterior['prosseguiu']} — "
                       f"vou SUBSTITUIR. ")
        return {
            "resposta": f"{prefixo}Confere o reporte 👇",
            "acao": {
                "tipo": "ligacoes", "ligacoes": p["ligacoes"], "atendeu": p["atendeu"],
                "prosseguiu": p["prosseguiu"], "semana": semana,
                "resumo": f"📞 {p['ligacoes']} ligações · {p['atendeu']} atenderam · {p['prosseguiu']} prosseguiram · semana {semana}",
            },
            "intent": intent,
        }

    # 1.35) STATUS "visita_status" → acha a visita agendada mais próxima → CONFIRMAÇÃO
    if intent == "visita_status":
        cid = viewer.get("corretora_id")
        if not cid:
            return {"resposta": "Como gestão você vê tudo, mas marcar compareceu/no-show é pela corretora — em breve libero 😉", "intent": intent}
        cli = (visita_ent.get("cliente") or lead_ref or "").strip()
        status = visita_ent.get("status")
        if status not in ("compareceu", "no_show"):
            return {"resposta": "Ela compareceu ou furou? Fala 'a Elaine compareceu' ou 'o João furou'.", "intent": intent}
        if not cli:
            return {"resposta": "De qual lead é a visita? Ex: 'a Elaine compareceu'.", "intent": intent}
        v = _achar_visita_agendada(int(cid), cli)
        if not v:
            recentes = _visitas_agendadas_recentes(int(cid))
            if recentes:
                lista = "; ".join(f"{r['lead_nome']} ({r['quando']})" for r in recentes)
                return {"resposta": f"Não achei visita agendada pra '{cli}' 😿. Suas agendadas recentes: {lista}.", "intent": intent}
            return {"resposta": f"Não achei nenhuma visita agendada pra '{cli}' — nem outras agendadas suas. Confere o nome?", "intent": intent}
        emoji = "✅ compareceu" if status == "compareceu" else "❌ no-show (furou)"
        return {
            "resposta": f"Confere 👇",
            "acao": {
                "tipo": "visita_status", "visita_id": v["id"], "status": status,
                "lead_nome": v["lead_nome"],
                "resumo": f"{v['lead_nome']} — {v['quando']} → {emoji}",
            },
            "intent": intent,
        }

    # 1.36) AÇÃO "mover" → resolve lead + etapa/funil → CONFIRMAÇÃO (não move ainda).
    # Corretora só move lead dela (escopo na busca E no /crm/mover); admin move
    # qualquer um. Venda ganha/perdida → confirmação com destaque (destrutivo).
    if intent == "mover":
        cli = (mover_ent.get("cliente") or lead_ref or "").strip()
        etapa_txt = (mover_ent.get("etapa") or "").strip()
        if not cli or not etapa_txt:
            return {"resposta": "Pra mover preciso do lead e da etapa. Ex: 'move o João pro Follow-up'.", "intent": intent}
        alvo = _resolver_etapa_mover(etapa_txt)
        if alvo is None:
            from app.services.lead_spine import STAGES_CANONICOS
            from app.services.nutricao import STAGES_NUTRICAO
            etapas = " · ".join(STAGES_CANONICOS)
            rmk = " · ".join(STAGES_NUTRICAO)
            return {"resposta": f"Não reconheci a etapa '{etapa_txt}' 🤔 As do funil são: {etapas}. No remarketing: {rmk}.", "intent": intent}
        funil, destino = alvo
        cid = viewer.get("corretora_id")  # None = admin → busca em todos
        achados = _leads_spine_por_nome(cli, cid)
        if not achados:
            return {"resposta": f"Não achei um lead {'seu ' if cid else ''}com '{cli}' no funil 😿 Confere o nome?", "intent": intent}
        escolhido = _escolher_lead_spine(achados, cli)
        if escolhido is None:
            lista = "; ".join(f"**{a['nome']}**" + (f" ({a['etapa']})" if a.get("etapa") else "") for a in achados)
            return {"resposta": f"Achei mais de um com '{cli}': {lista}. Qual deles? Me fala o nome completo 😉", "intent": intent}
        destrutivo = destino in _ETAPAS_DESTRUTIVAS
        de = f" ({escolhido['etapa']})" if escolhido.get("etapa") else ""
        rmk_tag = " · funil remarketing" if funil != "principal" else ""
        resumo = f"{escolhido['nome']}{de} → {destino}{rmk_tag}"
        if destrutivo:
            resposta = (f"⚠️ Isso fecha o funil do lead: **{escolhido['nome']}** vai pra "
                        f"**{destino}**. Confirma? 👇")
        else:
            resposta = "Confere a mudança de etapa 👇"
        return {
            "resposta": resposta,
            "acao": {"tipo": "mover", "lead_id": escolhido["id"], "lead_nome": escolhido["nome"],
                     "stage_destino": destino, "funil": funil, "destrutivo": destrutivo,
                     "resumo": resumo},
            "intent": intent,
        }

    # 1.37) AÇÃO "ligacao" → resolve o lead → CONFIRMAÇÃO (não grava ainda).
    # Grava lead_events tipo='ligacao' (por lead) — diferente do 'ligacoes'
    # (totais semanais). Corretora só no lead dela; admin em qualquer um.
    if intent == "ligacao":
        cli = (ligacao_ent.get("cliente") or lead_ref or "").strip()
        resultado = (ligacao_ent.get("resultado") or "").strip()
        nota = (ligacao_ent.get("nota") or "").strip()
        if not cli:
            return {"resposta": "Pra qual lead foi a ligação? Ex: 'liguei pra Pessoa8, não atendeu'.", "intent": intent}
        if resultado not in _LIGACAO_RESULTADOS:
            return {"resposta": f"E aí, o {cli} atendeu, não atendeu ou caiu na caixa postal?", "intent": intent}
        cid = viewer.get("corretora_id")  # None = admin → busca em todos
        achados = _leads_spine_por_nome(cli, cid)
        if not achados:
            return {"resposta": f"Não achei um lead {'seu ' if cid else ''}com '{cli}' pra registrar a ligação 😿 Confere o nome?", "intent": intent}
        escolhido = _escolher_lead_spine(achados, cli)
        if escolhido is None:
            lista = "; ".join(f"**{a['nome']}**" + (f" ({a['etapa']})" if a.get("etapa") else "") for a in achados)
            return {"resposta": f"Achei mais de um com '{cli}': {lista}. Qual deles? Me fala o nome completo 😉", "intent": intent}
        label = _LIGACAO_LABEL[resultado]
        resumo = f"📞 {escolhido['nome']} — {label}" + (f" · “{nota}”" if nota else "")
        return {
            "resposta": "Confere a ligação 👇",
            "acao": {"tipo": "ligacao", "lead_id": escolhido["id"], "lead_nome": escolhido["nome"],
                     "resultado": resultado, "nota": nota, "resumo": resumo},
            "intent": intent,
        }

    # 1.4) AÇÃO "agendar" → confirma a visita (não cria ainda)
    if intent == "agendar":
        cli = (agenda.get("cliente") or lead_ref or "").strip()
        data = (agenda.get("data") or "").strip()
        hora = (agenda.get("hora") or "").strip()
        if not (cli and data and hora):
            faltou = ", ".join([n for n, v in [("cliente", cli), ("data", data), ("hora", hora)] if not v])
            return {"resposta": f"Pra agendar preciso do {faltou}. Ex: 'agenda visita do João sábado 10h'.", "intent": intent}
        try:
            dfmt = datetime.strptime(data, "%Y-%m-%d").strftime("%d/%m")
        except Exception:
            dfmt = data
        loc = f" · {agenda['bairro']}" if agenda.get("bairro") else ""
        return {
            "resposta": f"Confere a visita 👇",
            "acao": {"tipo": "agendar", "cliente": cli, "data": data, "hora": hora,
                     "bairro": agenda.get("bairro") or "", "resumo": f"{cli} — {dfmt} às {hora}{loc}"},
            "intent": intent,
        }

    # 1.5) AÇÃO "anotar" → acha o lead e devolve CONFIRMAÇÃO (não executa ainda)
    if intent == "anotar":
        alvo = lead_ref or ""
        b = _get("/atendimento/busca", user, {"q": alvo}) if alvo else {}
        leads = b.get("leads") or b.get("resultados") or []
        if leads:
            lead = leads[0]
            nome = lead.get("nome") or lead.get("lead_nome")
            return {
                "resposta": f"Quer que eu registre isso no lead **{nome}**? A IA lê, resume e posta a nota no funil 📝",
                "acao": {"tipo": "anotar", "lead_id": lead.get("lead_id"), "lead_nome": nome, "nota": pergunta},
                "intent": intent,
            }
        return {"resposta": f"Não achei um lead seu com '{alvo or pergunta[:20]}' pra registrar. Confere o nome do lead?", "intent": intent}

    # 1.6) RASCUNHO → acha o lead, monta mensagem pronta pra corretora COPIAR (sem ação/envio)
    if intent == "rascunho":
        alvo = lead_ref or ""
        b = _get("/atendimento/busca", user, {"q": alvo}) if alvo else {}
        leads = b.get("leads") or b.get("resultados") or []
        if not leads:
            return {"resposta": f"Pra escrever a mensagem preciso saber pra quem 🐱. Me diz o lead — ex: 'me dá uma mensagem pro João'.", "intent": intent}
        lead = leads[0]
        lead_nome = lead.get("nome") or lead.get("lead_nome")
        conv = _get("/conversa", user, {"lead_id": lead.get("lead_id"), "hours_back": 4320})
        ctx_rasc = {
            "lead_nome": lead_nome,
            "raiox": conv.get("raiox"),
            "coaching": conv.get("coaching"),
            "ultimas_msgs": (conv.get("mensagens") or [])[-14:],
            "pedido_da_corretora": pergunta,
        }
        try:
            texto, _ = prov.complete_text(_rascunho_persona(nome, lead_nome),
                                          _json.dumps(ctx_rasc, ensure_ascii=False)[:9000])
            texto = texto.strip()
        except Exception as e:  # noqa: BLE001
            log.warning("assistente_rascunho_erro", erro=str(e)[:120])
            return {"resposta": "Deu um probleminha pra montar a mensagem 😿 tenta de novo?", "intent": intent}
        return {
            "resposta": f"Sugestão de mensagem pro **{lead_nome}** — copia e manda do teu jeito 👇\n\n{texto}",
            "intent": intent,
        }

    # 2) BUSCA (escopada por corretora nos próprios endpoints)
    ctx: dict[str, Any] = {}
    if intent in ("dia", "coaching", "outro", "briefing"):
        # Corretora perguntando do dia de OUTRA corretora → recusa (privacidade).
        if not is_admin and ref_cid and ref_cid != viewer.get("corretora_id"):
            log.info("assistente_escopo_recusado", user=user, ref_cid=ref_cid)
            return {
                "resposta": f"Aqui cada corretora vê só o dela 😉 O dia da {ref_nome} fica com ela e com a gestão. Quer ver o SEU dia?",
                "intent": intent,
            }
        # Admin citando corretora → pede o /atendimento/me FILTRADO por ela.
        me_params = {"corretora": ref_cid} if (is_admin and ref_cid) else None
        me = _get("/atendimento/me", user, me_params)
        leads = me.get("leads", [])[:15]
        ctx["hoje"] = [
            {k: l.get(k) for k in ("lead_id", "nome", "corretora_label", "temperatura",
                                   "resumo", "gap", "sugestao", "cuidado_em")}
            for l in leads
        ]
    if intent == "briefing":
        ag = _get("/agenda", user, {"periodo": "hoje"})
        ctx["visitas_hoje"] = {"total": ag.get("total"), "items": (ag.get("items") or [])[:12]}
    if intent == "numeros":
        vg = _get("/visao-geral", user, {"periodo": "mes_atual"})
        ctx["meus_numeros"] = vg.get("funil_por_corretora") or vg.get("funil_equipe")
        ctx["periodo"] = (vg.get("periodo") or {}).get("label")
    if intent == "visitas":
        ag = _get("/agenda", user, {"periodo": "semana"})
        ctx["visitas"] = {"total": ag.get("total"), "items": (ag.get("items") or [])[:15]}
    if lead_ref:
        b = _get("/atendimento/busca", user, {"q": lead_ref})
        leads = b.get("leads") or b.get("resultados") or []
        # O TELEFONE É A CHAVE MAIS FORTE — e estava sendo jogado fora.
        # Quando a corretora diz nome E contato ("o lead Alessandra Duco (15)
        # 99666-4454"), o roteador escolhe UM dos dois pra `lead_ref`; se
        # escolhe o nome e o nome está levemente diferente do cadastrado, a
        # busca falha mesmo com o número correto na frente. Aqui, se não achou,
        # tenta de novo com os dígitos da PERGUNTA inteira.
        if not leads:
            digitos = re.sub(r"\D", "", pergunta)
            if len(digitos) >= 8 and re.sub(r"\D", "", lead_ref) != digitos:
                b2 = _get("/atendimento/busca", user, {"q": digitos[-11:]})
                leads = b2.get("leads") or b2.get("resultados") or []
                if leads:
                    log.info("assistente_lead_achado_por_telefone",
                             lead_ref=lead_ref[:30], user=user)
        if leads:
            lead = leads[0]
            conv = _get("/conversa", user, {"lead_id": lead.get("lead_id"), "hours_back": 4320})
            ctx["lead"] = {
                "lead_id": lead.get("lead_id"),
                "nome": lead.get("nome") or lead.get("lead_nome"),
                "raiox": conv.get("raiox"),
                "coaching": conv.get("coaching"),
                "ultimas_msgs": (conv.get("mensagens") or [])[-14:],
            }
        else:
            ctx["lead"] = {"erro": f"não achei nenhum lead seu com '{lead_ref}'"}
    if intent == "empreendimento":
        ctx["empreendimentos"] = _empreendimentos(emp)

    # Botões abre-chat: leads citados no contexto viram [{lead_id, nome}] pro
    # front renderizar "💬 Nome" → /chat?lead=<id>. Dedup por lead_id, máx 12.
    leads_botoes: list[dict[str, str]] = []
    vistos: set[str] = set()
    for item in list(ctx.get("hoje") or []) + ([ctx["lead"]] if ctx.get("lead") else []):
        lid = item.get("lead_id")
        if not lid or str(lid) in vistos or len(leads_botoes) >= 12:
            continue
        vistos.add(str(lid))
        leads_botoes.append({"lead_id": str(lid), "nome": item.get("nome") or "?"})

    # 3) RESPOSTA
    extra = ""
    if intent == "briefing":
        extra = (
            "\n\nFORMATO: monte um BRIEFING DA MANHÃ pra ela, escaneável: "
            "1) cumprimento rápido; 2) 🔥 leads quentes pra cuidar (com o próximo passo em 1 linha cada); "
            "3) ⏰ quem tá esperando resposta; 4) 📅 visitas de hoje (se tiver); 5) 💡 uma dica do dia. "
            "Curto, direto, sem enrolação — é pra ela bater o olho e sair fazendo."
        )
    if is_admin and intent in ("dia", "coaching", "outro", "briefing"):
        if ref_nome:
            extra += (
                f"\n\nATENÇÃO ESCOPO: quem pergunta é a GESTÃO ({nome}), e os DADOS são o dia da "
                f"corretora {ref_nome}. Abra a resposta deixando claro que esse é o dia DA {ref_nome} "
                f"(ex: 'Esse é o dia da {ref_nome} 👇') e fale SOBRE ela em 3ª pessoa — "
                f"NUNCA escreva como se estivesse falando COM a {ref_nome} (nada de 'teu dia', 'seus leads')."
            )
        else:
            extra += (
                "\n\nATENÇÃO ESCOPO: quem pergunta é a GESTÃO, e os DADOS misturam leads de TODAS as "
                "corretoras do time. Apresente como VISÃO GERAL DO TIME (cite a corretora de cada lead "
                "quando o dado trouxer corretora_label) — NUNCA como se fosse o dia pessoal de alguém "
                "(nada de 'teu dia')."
            )
    user_prompt = (
        f"Pergunta da corretora: {pergunta}{extra}\n\n"
        f"DADOS (só o que é dela; use apenas isto):\n{_json.dumps(ctx, ensure_ascii=False)[:9000]}"
    )
    try:
        resposta, _ = prov.complete_text(_persona(nome), user_prompt)
    except Exception as e:  # noqa: BLE001
        log.warning("assistente_answer_erro", erro=str(e)[:120])
        resposta = "Deu um probleminha aqui pra buscar isso 😿 tenta de novo?"

    out: dict[str, Any] = {"resposta": resposta.strip(), "intent": intent}
    if leads_botoes:
        out["leads_botoes"] = leads_botoes
    return out
