"""Endpoints do Painel Acme (Onda 10).

Lê de:
- Postgres copiloto (vendas_mensais, visitas, mensagens_whatsapp, view
  vw_metricas_mes_corretora, corretoras)
- SQLite gestor-trafego /opt/gestor-trafego/data/trafego.db (READONLY)
  filtro cliente_slug='acme'

Auth: nginx faz Basic Auth na frente. Backend confia no header
X-Painel-User passado pelo nginx (auth_basic_user). Sem header → 403.
"""
from __future__ import annotations

import os
import unicodedata
import re
import sqlite3
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal
from urllib.parse import quote
from zoneinfo import ZoneInfo

import structlog
from fastapi import (
    APIRouter,
    Body,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Response,
    UploadFile,
)
from pydantic import BaseModel, Field
from sqlalchemy import text as sql_text

from app.db.models import HistoricoCache
from app.db.session import SessionLocal
from app.services import funil_local

log = structlog.get_logger()

SP_TZ = ZoneInfo("America/Sao_Paulo")
TRAFEGO_DB = "/opt/gestor-trafego/data/trafego.db"
ACME_SLUG = "acme"

router = APIRouter(prefix="/painel", tags=["painel"])

class _MapaCorretoras(dict):
    """corretora_id → nome, lido da tabela `corretoras` em vez do código.

    Era um dicionário literal com os nomes da Acme escritos aqui dentro.
    Funcionava enquanto existia um cliente só — mas em qualquer outra instância
    (a demo, o próximo cliente) o painel exibia "Pessoa5/Pessoa1/Pessoa3"
    por cima das corretoras de verdade. Apareceu em 04/08/2026, no primeiro
    minuto do ambiente de demonstração.

    Herda de `dict` de propósito: são dezenas de `.get(cid, default)` espalhados
    pelo arquivo, e trocar todos por chamada de função seria um diff enorme com
    risco proporcional. Aqui o acesso continua igual — o que muda é de onde vem.

    Cache de 5 min: nome de corretora quase não muda, e isso roda em toda carga
    de tela. `fallback` cobre banco fora do ar — melhor um nome velho que uma
    tela quebrada.
    """

    _TTL_SEC = 300

    def __init__(self, coluna: str, fallback: dict[int, str]):
        super().__init__(fallback)
        self._coluna = coluna
        self._fallback = dict(fallback)
        self._carregado_em = 0.0

    def _atualizar(self) -> None:
        import time as _t

        if (_t.time() - self._carregado_em) < self._TTL_SEC:
            return
        try:
            with SessionLocal() as s:
                linhas = s.execute(
                    sql_text(
                        f"SELECT id, {self._coluna} FROM corretoras "
                        # `papel` varia (corretora_solo, corretora_atendedora_de_dona…) — o que
                        # importa é excluir quem não atende lead (casa, interno).
                        "WHERE ativo = TRUE AND coalesce(papel,'corretora') LIKE 'corretora%'"
                    )
                ).all()
            if linhas:
                super().clear()
                super().update({int(r[0]): str(r[1]) for r in linhas if r[1]})
            self._carregado_em = _t.time()
        except Exception as e:  # banco fora do ar → segue com o que tem
            log.warning("corretora_mapa_fallback", coluna=self._coluna, erro=str(e)[:120])

    def get(self, key, default=None):  # noqa: ANN001
        self._atualizar()
        return super().get(key, default)

    def __getitem__(self, key):  # noqa: ANN001
        self._atualizar()
        return super().__getitem__(key)

    def items(self):
        self._atualizar()
        return super().items()

    def values(self):
        self._atualizar()
        return super().values()

    def keys(self):
        self._atualizar()
        return super().keys()

    def __contains__(self, key) -> bool:  # noqa: ANN001
        self._atualizar()
        return super().__contains__(key)

    def __iter__(self):
        self._atualizar()
        return super().__iter__()


# Label curto do painel. `apelido` quando a corretora tem um; senão o 1º nome.
CORRETORA_LABEL = _MapaCorretoras(
    "coalesce(nullif(apelido, ''), split_part(nome, ' ', 1))",
    {1: "Pessoa10", 2: "Pessoa11", 3: "Pessoa12"},
)


def _corretora_id_do_filtro(valor: str | None) -> int | None:
    """Resolve o ?corretora= das telas (admin) → corretora_id. Aceita o nome do
    DB ("Pessoa5"), o rótulo curto ("Pessoa10"), sem acento e em qualquer caixa, ou
    o id numérico. Devolve None quando vazio/desconhecido (= sem filtro), pra
    filtro errado nunca virar "lista vazia" silenciosa."""
    if valor is None:
        return None
    v = unicodedata.normalize("NFD", str(valor).strip().lower())
    v = "".join(c for c in v if unicodedata.category(c) != "Mn")
    if not v or v in ("todas", "todos"):
        return None
    if v.isdigit():
        cid = int(v)
        return cid if cid in CORRETORA_LABEL else None
    for cid, lbl in CORRETORA_LABEL.items():
        if v == lbl.lower():
            return cid
    for cid, nome in CORRETORA_NOME_DB.items():
        n = unicodedata.normalize("NFD", nome.lower())
        n = "".join(c for c in n if unicodedata.category(c) != "Mn")
        if v == n or v == n.split()[0]:
            return cid
    return None
CORRETORA_NOME_DB = _MapaCorretoras("nome", {1: "Pessoa5", 2: "Pessoa1", 3: "Pessoa3"})
# Mapeamento corretora_id (Postgres) → kommo_user_id (pra cruzar com events Kommo)
CORRETORA_KOMMO_USER = {1: 10000006, 2: 10000007, 3: 10000003}
KOMMO_USER_CORRETORA = {v: k for k, v in CORRETORA_KOMMO_USER.items()}
# Corretoras migradas pro CRM novo (Twenty): a observação vira nota no Twenty,
# não no Kommo. Pessoa3 (id=3) usa o Twenty. Demais → Kommo (fallback seguro).
CORRETORAS_TWENTY = {3}

# Mapeamento login htpasswd → corretora_id. Admins (dona/pessoa) NÃO estão aqui — eles veem tudo.
USUARIO_CORRETORA = {"corretor": 1, "corretor": 2, "corretor": 3, "pessoa_d": 7, "corretor": 8}
# Corretoras cujo telefone só completa a chamada com 0 + DDD (chip virtual —
# Salvy). Em celular comum esse formato FALHA, então não dá pra ser o padrão:
# quem não está aqui segue discando DDD + número, como sempre.
# 20/09: a Corretor entrou no mesmo arranjo da Pessoa7 (+5500000000000, faixa
# Salvy). Se ela passar a ligar de celular comum, tirar o 8 daqui — com o
# formato errado a operadora devolve "número discado incorretamente".
CORRETORAS_CHIP_VIRTUAL = {7, 8}
# Delegação de ATENDIMENTO: quem pode atender NO LUGAR de outra corretora.
# 3 (Pessoa12) cobre a linha da Pessoa7 (7): a Pessoa7 liga/resgata pelo chip virtual e,
# quando o lead responde no WhatsApp, quem conversa é a Pessoa12 — pelo painel, e a
# mensagem sai pelo número DA PESSOA_D (o lead vê uma linha só). É a regra já
# escrita em app/api/cloud_corretora.py quando a linha dela entrou (09/09).
# Vale pro CHAT (thread, envio, mídia, foto, reengajamento) e, desde 01/10/2026,
# pro CARD no kanban do CRM. Número da operação — meta de ligação, ranking, fila
# de ligação, visão geral — continua lendo o próprio corretora_id: a Pessoa12 cobre a
# conversa e trabalha o card, não vira dona da agenda da Pessoa7.
#
# 01/10/2026 — entra a Corretor (8) e a delegação passa a valer no kanban. Decisão da
# Dona: "o que é da Pessoa12 fica na Pessoa12, o que é da Corretor fica no card de usuário da
# Corretor, o que é da Pessoa7 no da Pessoa7. A Pessoa12 tem acesso a todos — aí ela trabalha
# por lá." A Corretor e a Pessoa7 adotam lead na janela de ligação e o lead nasce com
# elas como donas; sem isto ele ficava invisível no kanban (Fulano, lead
# 681) ou tinha de trocar de dona pra aparecer (Fulano, lead 728, posta na Pessoa12 em
# 30/09). O que a delegação abre pra Pessoa12 no card da delegada: board do CRM e do
# remarketing, mover etapa, vínculo/etapa pelo chat (lead-por-ref, adotar) e o
# gate padrão `_checar_escopo_lead` (nota, tarefa, nome, telefone). Mão única:
# Corretor e Pessoa7 não enxergam a Pessoa12 nem uma à outra; Pessoa10 e Pessoa11 não ganham nada.
# Transferir o lead de dona continua sendo só da dona ou do admin.
DELEGACAO_ATENDIMENTO: dict[int, set[int]] = {3: {7, 8}}
# Quem vê tudo. Vem de env pra cada instância ter os seus — a da Acme segue
# com dona/pessoa/assistente, a de demonstração tem o login do sócio comercial. Era
# literal aqui, e como as duas instâncias rodam o MESMO código, liberar alguém
# numa exigia escrever o nome dele na lista que a outra também lê.
# Sem `PAINEL_ADMINS` no ambiente, nada muda pra quem já roda.
ADMINS = {
    u.strip().lower()
    for u in os.environ.get("PAINEL_ADMINS", "dona,pessoa,assistente").split(",")
    if u.strip()
}

# Meta individual (referência do FunilPro antigo)
META_VISITAS_SEMANA = 4
STATUS_ENTROU = 10000009
STATUS_EM_ATENDIMENTO = 10000005
# (STATUS_RECUPERADOS = 10000010 já definido abaixo)


# ── Período ───────────────────────────────────────────────────────────────────


PeriodoLiteral = Literal["hoje", "semana", "mes_atual", "mes_passado", "custom"]


def _hoje_sp() -> date:
    return datetime.now(SP_TZ).date()


# Primeiro mês inteiro com o painel V2 dono do lead (cutover do Kommo: 27/07).
INICIO_CRM_NOVO = date(2026, 8, 1)


def parse_periodo(
    periodo: str,
    inicio: str | None = None,
    fim: str | None = None,
) -> tuple[date, date, str]:
    """Retorna (data_inicio, data_fim, label_humano)."""
    hoje = _hoje_sp()
    if periodo == "hoje":
        return hoje, hoje, hoje.strftime("%d/%m/%Y")
    # "ontem" e "semana_atual" (19/08): as tools do MCP anunciavam os dois e o
    # parser rejeitava — quem perguntasse "e ontem?" no Telegram levava
    # "periodo inválido". Aceitar aqui é mais barato que corrigir cada chamador,
    # e "semana_atual" é sinônimo honesto de "semana".
    if periodo == "ontem":
        ont = hoje - timedelta(days=1)
        return ont, ont, ont.strftime("%d/%m/%Y")
    if periodo in ("semana", "semana_atual"):
        seg = hoje - timedelta(days=hoje.weekday())
        return seg, seg + timedelta(days=6), f"semana de {seg.strftime('%d/%m')}"
    # Janelas corridas da Visão (06/10): "7 dias, um mês, quanto ao todo" (Dona).
    # Contam o dia de hoje. "tudo" começa em INICIO_CRM_NOVO: antes disso o funil
    # vinha do Kommo e da planilha, e somar as duas bases dá número que não fecha.
    if periodo in ("7d", "30d"):
        n = 7 if periodo == "7d" else 30
        return hoje - timedelta(days=n - 1), hoje, f"últimos {n} dias"
    if periodo == "tudo":
        return INICIO_CRM_NOVO, hoje, f"desde {INICIO_CRM_NOVO.strftime('%d/%m/%Y')}"
    if periodo == "mes_atual":
        ini = hoje.replace(day=1)
        return ini, hoje, ini.strftime("%B/%Y")
    if periodo == "mes_passado":
        primeiro = hoje.replace(day=1)
        fim_passado = primeiro - timedelta(days=1)
        ini_passado = fim_passado.replace(day=1)
        return ini_passado, fim_passado, ini_passado.strftime("%B/%Y")
    if periodo == "custom":
        if not (inicio and fim):
            raise HTTPException(400, "periodo=custom exige inicio + fim (YYYY-MM-DD)")
        try:
            di = datetime.strptime(inicio, "%Y-%m-%d").date()
            df = datetime.strptime(fim, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(400, "inicio/fim em formato YYYY-MM-DD")
        if df < di:
            raise HTTPException(400, "fim < inicio")
        return di, df, f"{di.strftime('%d/%m')} → {df.strftime('%d/%m')}"
    raise HTTPException(400, f"periodo inválido: {periodo}")


def _meses_no_periodo(di: date, df: date) -> list[str]:
    """Retorna lista de 'YYYY-MM' que o período intersecta."""
    out: list[str] = []
    cur = di.replace(day=1)
    end = df.replace(day=1)
    while cur <= end:
        out.append(cur.strftime("%Y-%m"))
        if cur.month == 12:
            cur = cur.replace(year=cur.year + 1, month=1)
        else:
            cur = cur.replace(month=cur.month + 1)
    return out


# ── Helpers de dados ──────────────────────────────────────────────────────────


def calcular_metricas_meta_ads(di: date, df: date) -> dict[str, Any]:
    """Lê gestor-trafego/data/trafego.db (READONLY)."""
    try:
        uri = f"file:{TRAFEGO_DB}?mode=ro"
        c = sqlite3.connect(uri, uri=True)
        try:
            c.row_factory = sqlite3.Row
            r = c.execute(
                """SELECT
                     COALESCE(SUM(spend),0)       AS spend,
                     COALESCE(SUM(impressions),0) AS impressions,
                     COALESCE(SUM(clicks),0)      AS clicks,
                     COALESCE(SUM(leads),0)       AS leads
                   FROM insights_diario
                   WHERE cliente_slug = ? AND data BETWEEN ? AND ?""",
                (ACME_SLUG, di.isoformat(), df.isoformat()),
            ).fetchone()
            spend = float(r["spend"] or 0)
            leads = int(r["leads"] or 0)
            clicks = int(r["clicks"] or 0)
            impressions = int(r["impressions"] or 0)
            return {
                "spend": round(spend, 2),
                "leads_meta": leads,
                "clicks": clicks,
                "impressions": impressions,
                "cpl": round(spend / leads, 2) if leads else None,
                "cpc": round(spend / clicks, 2) if clicks else None,
            }
        finally:
            c.close()
    except Exception as e:
        log.error("painel_meta_ads_error", error=str(e))
        return {"spend": 0, "leads_meta": 0, "clicks": 0, "impressions": 0,
                "cpl": None, "cpc": None, "erro": str(e)}


def calcular_funil(di: date, df: date, *, agenda_completa: bool = False,
                   visitas_ate: date | None = None) -> dict[str, Any]:
    """Agrega vendas_mensais por corretora pros meses do período.

    `agenda_completa=True` (a Visão usa; ranking e comparativo ainda não):
    o degrau `agendados` passa a ser TODA visita da agenda no período — inclusive
    cancelada e as que ainda vão acontecer — e `conv_visitou` vira
    compareceu ÷ (compareceu + no-show + cancelada). Antes `agendados` excluía
    cancelada e a conversão ignorava cancelada: set/2026 mostrava 12 visitas de
    13 agendados (92,3%) com 26 visitas na agenda, 11 delas canceladas.
    `visitas_ate` estica só a janela das visitas (a Visão manda o último dia
    do mês em "mês atual", pra visita já marcada pra semana que vem contar).

    Trade-off: vendas_mensais é por mês completo. Se o período é parcial
    (ex: "hoje"), retorna o mês corrente inteiro. Pra granularidade
    diária precisaria de outra fonte (Inspector daily_metrics_corretora).

    Exceção: `visitas` (compareceu) vem da tabela `visitas` direto,
    filtrada pelo período real — desde a descomissionalização do
    update.py (Sub-onda 10.1.c) o qtde_visitas em vendas_mensais não
    é mais alimentado. Fonte canônica passou a ser a tabela visitas.
    """
    meses = _meses_no_periodo(di, df)
    if not meses:
        return {"corretoras": [], "equipe": _empty_funil()}

    di_dt = datetime.combine(di, datetime.min.time(), tzinfo=SP_TZ)
    df_dt = datetime.combine(df, datetime.max.time(), tzinfo=SP_TZ)

    # Período "parcial" (ex: hoje, ontem, últimos 7d) — não cobre o mês inteiro.
    # vendas_mensais é granular por mês: usar leads_snapshot.criado_em_kommo
    # pra contar leads do período REAL e evitar inflar com o mês todo.
    _last_month = meses[-1]
    _y, _m = (int(x) for x in _last_month.split("-"))
    _next = date(_y + 1, 1, 1) if _m == 12 else date(_y, _m + 1, 1)
    _last_day = _next - timedelta(days=1)
    is_partial = (di.day != 1) or (df != _last_day)
    # Período "curto" — não começa no dia 1 (ex: hoje, ontem, últimos_7d).
    # Vendas/VGV de vendas_mensais (mensal) só fazem sentido qd di.day==1.
    is_short = di.day != 1

    with SessionLocal() as s:
        rows = s.execute(
            sql_text(
                """SELECT corretora_id,
                          SUM(qtde_leads)     AS leads,
                          SUM(qtde_atendidos) AS atendidos,
                          SUM(qtde_agendados) AS agendados,
                          SUM(qtde_vendas)    AS vendas,
                          SUM(vgv)            AS vgv
                   FROM vendas_mensais
                   WHERE ano_mes = ANY(:meses)
                   GROUP BY corretora_id
                   ORDER BY corretora_id"""
            ),
            {"meses": meses},
        ).all()

        # Override de `leads` e `atendidos` quando período é parcial.
        # Fonte: tabela `leads` do painel V2, a MESMA agregação que o fechamento
        # mensal usa (app/services/funil_local.py).
        #
        # Era `leads_snapshot.criado_em_kommo` — cache do Kommo, que congelou em
        # 29/07 quando o cutover desligou a criação de lead lá. Como "mês atual
        # até hoje" é sempre parcial, o /visao passou a mostrar o funil de agosto
        # inteiro ZERADO (e, com leads=0 e spend>0, acendia o alerta falso
        # "investiu R$X sem nenhum lead") enquanto vendas_mensais tinha o número
        # certo o tempo todo. Encontrado em 04/08/2026.
        leads_diarios: dict[int, int] = {}
        atend_diarios: dict[int, int] = {}
        vendas_janela: dict[int, tuple[int, float]] = {}
        if is_partial:
            for row in funil_local.agregar_janela(
                s,
                datetime.combine(di, datetime.min.time(), tzinfo=SP_TZ),
                datetime.combine(df + timedelta(days=1), datetime.min.time(), tzinfo=SP_TZ),
            ):
                cid = int(row["owner_id"])
                leads_diarios[cid] = int(row["qtde_leads"] or 0)
                atend_diarios[cid] = int(row["qtde_atendidos"] or 0)
                vendas_janela[cid] = (int(row["qtde_vendas"] or 0), float(row["vgv"] or 0))
            # Garante que TODAS as corretoras presentes em leads_diarios apareçam,
            # mesmo se não tiverem linha em vendas_mensais (ex: mês novo, sem venda)
            existing = {int(r[0]) for r in rows}
            missing = [cid for cid in leads_diarios if cid not in existing]
            if missing:
                rows = list(rows) + [(cid, 0, 0, 0, 0, 0) for cid in missing]
                rows.sort(key=lambda x: x[0])

        visitas_df_dt = (
            datetime.combine(visitas_ate, datetime.max.time(), tzinfo=SP_TZ)
            if visitas_ate else df_dt
        )
        visitas_rows = s.execute(
            sql_text(
                """SELECT corretora_id,
                          COUNT(*) FILTER (WHERE status = 'compareceu') AS compareceu,
                          COUNT(*) FILTER (WHERE status <> 'cancelada') AS agendados,
                          COUNT(*)                                      AS na_agenda,
                          COUNT(*) FILTER (WHERE status IN
                              ('compareceu', 'no_show', 'cancelada'))   AS encerradas
                   FROM visitas
                   WHERE data_hora BETWEEN :di AND :df
                   GROUP BY corretora_id"""
            ),
            {"di": di_dt, "df": visitas_df_dt},
        ).all()

        # atendidos_evolution: contagem real via WhatsApp (2-via), em paralelo
        # ao qtde_atendidos do Kommo. Inclui leads novos + reengajados.
        atend_evo_rows = s.execute(
            sql_text(
                """SELECT corretora_id,
                          SUM(atendidos_2via + reengajados_2via) AS atend_evo,
                          MAX(atualizado_em)  AS atualizado_em
                   FROM atendimentos_evolution_mensais
                   WHERE ano_mes = ANY(:meses)
                   GROUP BY corretora_id"""
            ),
            {"meses": meses},
        ).all()
    visitas_por_corr: dict[int, int] = {int(r[0]): int(r[1] or 0) for r in visitas_rows}
    agendados_por_corr: dict[int, int] = {
        int(r[0]): int((r[3] if agenda_completa else r[2]) or 0) for r in visitas_rows
    }
    encerradas_por_corr: dict[int, int] = {int(r[0]): int(r[4] or 0) for r in visitas_rows}
    # Base da conversão "visitou": com a agenda completa, só visita encerrada
    # (a que ainda vai acontecer não é falta de ninguém).
    base_visitou_por_corr = encerradas_por_corr if agenda_completa else agendados_por_corr
    if agenda_completa:
        # Quem só tem visita no período (sem lead nem venda) não pode sumir do
        # funil — senão o degrau "agendados" deixa de bater com a agenda.
        com_linha = {int(r[0]) for r in rows}
        faltando = sorted(c for c in agendados_por_corr if c not in com_linha)
        if faltando:
            rows = sorted(list(rows) + [(c, 0, 0, 0, 0, 0) for c in faltando],
                          key=lambda x: x[0])
    atend_evo_por_corr: dict[int, int] = {int(r[0]): int(r[1] or 0) for r in atend_evo_rows}
    atend_evo_atualizado_em = max(
        (r[2] for r in atend_evo_rows if r[2] is not None), default=None
    )

    # Refinamento Maio/2026: separa atendidos novos vs recuperados via events Kommo
    atend_breakdown = calcular_atendidos_breakdown(di, df)
    recup_por_corr = calcular_recuperados_por_corretora(di, df)

    corretoras = []
    total = {"leads": 0, "atendidos": 0, "agendados": 0, "visitas": 0,
             "vendas": 0, "vgv": 0.0, "base_visitou": 0}
    corretoras_em_vendas_mensais: set[int] = set()
    for r in rows:
        cid = int(r[0])
        corretoras_em_vendas_mensais.add(cid)
        if is_partial:
            leads = leads_diarios.get(cid, 0)
            atend_total = atend_diarios.get(cid, 0)
            # Período que não começa no dia 1 (hoje, semana, 7/30 dias): venda da
            # própria janela, pela `vendas.data_venda` (mesma tabela que alimenta
            # vendas_mensais). Até 06/10 zerava: "30 dias" mostrava 0 venda com 4
            # vendas de setembro dentro da janela. Em mes_atual mantém o mensal.
            if is_short:
                vend, vgv = vendas_janela.get(cid, (0, 0.0))
            else:
                vend = int(r[4] or 0)
                vgv = float(r[5] or 0)
        else:
            leads = int(r[1] or 0)
            atend_total = int(r[2] or 0)
            vend = int(r[4] or 0)
            vgv = float(r[5] or 0)
        agend = agendados_por_corr.get(cid, 0)
        visi = visitas_por_corr.get(cid, 0)

        # Atendidos refinado: só novos (vindos de Entrou no mês via events).
        # Fallback: se Kommo não retornou events ou dados antigos, mantém atend_total.
        atend_breakdown_corr = atend_breakdown["por_corretora"].get(cid, {})
        atend_novos = atend_breakdown_corr.get("novos", 0)
        atend_recup = atend_breakdown_corr.get("recuperados", 0)
        recup_corr = recup_por_corr.get(cid, 0)

        # Se o breakdown de events deu zero MAS vendas_mensais tem atendidos, usa o histórico
        # (acontece em meses sem events na janela analisada, ex: jan/fev/mar antiga)
        atend_evo = atend_evo_por_corr.get(cid, 0)
        if is_short:
            # atendimentos_evolution_mensais é por MÊS: em "semana" a Visão
            # mostrava 33 atendidos pra 11 leads (o mês todo). Na janela curta o
            # atendido é o da própria janela (lead que respondeu, funil_local).
            atend_evo = atend_diarios.get(cid, 0)
        if is_partial:
            # Período parcial: atend_total já é diário (status mudou de "entrou").
            atend_usado = atend_total
        elif atend_evo == 0 and atend_total > 0:
            # Mês pré-Evolution: planilha (funil_manual/funil_semanal) é a fonte canônica.
            atend_usado = atend_total
        else:
            atend_usado = atend_novos if (atend_novos + atend_recup) > 0 else atend_total
        corretoras.append({
            "corretora_id": cid,
            "label": CORRETORA_LABEL.get(cid, f"C{cid}"),
            "nome": CORRETORA_NOME_DB.get(cid, f"Corretora {cid}"),
            "leads": leads,
            "atendidos": atend_usado,
            "atendidos_total": atend_total,       # debugging / referência
            "atendidos_evolution": atend_evo,     # conversa 2-via real WhatsApp (paralelo)
            "conv_atendeu_evolution": _pct(atend_evo, leads),
            "recuperados": recup_corr,            # leads movidos pra Recuperados no período
            "agendados": agend,
            "visitas": visi,
            "vendas": vend,
            "vgv": round(vgv, 2),
            "ticket_medio": round(vgv / vend, 2) if vend else None,
            "conv_atendeu": _pct(atend_usado, leads),
            "conv_agendou": _pct(agend, atend_evo),
            "conv_visitou": _pct(visi, base_visitou_por_corr.get(cid, 0), encaixado=True),
            "conv_vendeu": _pct(vend, visi),
        })
        total["base_visitou"] += base_visitou_por_corr.get(cid, 0)
        for k in ("leads", "agendados", "visitas", "vendas"):
            total[k] += {"leads": leads, "agendados": agend,
                         "visitas": visi, "vendas": vend}[k]
        total["atendidos"] = total.get("atendidos", 0) + atend_usado
        total["atendidos_evolution"] = total.get("atendidos_evolution", 0) + atend_evo
        total["recuperados"] = total.get("recuperados", 0) + recup_corr
        total["vgv"] += vgv

    equipe = {
        "leads": total["leads"],
        "atendidos": total["atendidos"],
        "atendidos_evolution": total.get("atendidos_evolution", 0),
        "conv_atendeu_evolution": _pct(total.get("atendidos_evolution", 0), total["leads"]),
        "recuperados": total.get("recuperados", 0),
        "agendados": total["agendados"],
        "visitas": total["visitas"],
        "vendas": total["vendas"],
        "vgv": round(total["vgv"], 2),
        "ticket_medio": round(total["vgv"] / total["vendas"], 2) if total["vendas"] else None,
        "conv_atendeu": _pct(total["atendidos"], total["leads"]),
        "conv_agendou": _pct(total["agendados"], total.get("atendidos_evolution", 0)),
        "conv_visitou": _pct(total["visitas"], total["base_visitou"], encaixado=True),
        "conv_vendeu": _pct(total["vendas"], total["visitas"]),
    }
    return {
        "meses_considerados": meses,
        "corretoras": corretoras,
        "equipe": equipe,
        "atendidos_evolution_atualizado_em": (
            atend_evo_atualizado_em.isoformat() if atend_evo_atualizado_em else None
        ),
    }


def _pct(num: int, den: int, encaixado: bool = False) -> float | None:
    """Percentual em pontos (0-100).

    `encaixado=True` para taxas de funil onde o numerador é, por construção,
    subconjunto do denominador (ex: visitas realizadas ⊂ visitas agendadas,
    ambas da mesma query em `visitas`). Nesse caso num > den não é taxa alta,
    é dado inconsistente — devolve None em vez de mostrar "233%" pra cliente.
    As demais taxas cruzam fontes diferentes (Kommo × Evolution × vendas) e
    podem legitimamente passar de 100%, então não são capadas.
    """
    if not den:
        return None
    if encaixado and num > den:
        return None
    return round((num / den) * 100, 1)


def _empty_funil() -> dict[str, Any]:
    return {"leads": 0, "atendidos": 0, "agendados": 0, "visitas": 0,
            "vendas": 0, "vgv": 0.0, "ticket_medio": None,
            "conv_atendeu": None, "conv_agendou": None,
            "conv_visitou": None, "conv_vendeu": None}


def listar_visitas(di: date, df: date) -> dict[str, Any]:
    """Visitas no período + taxa de comparecimento."""
    di_dt = datetime.combine(di, datetime.min.time(), tzinfo=SP_TZ)
    df_dt = datetime.combine(df, datetime.max.time(), tzinfo=SP_TZ)
    with SessionLocal() as s:
        rows = s.execute(
            sql_text(
                """SELECT id, corretora_id, lead_nome, data_hora, bairro, status,
                          origem, is_revisita
                   FROM visitas
                   WHERE data_hora BETWEEN :di AND :df
                   ORDER BY data_hora"""
            ),
            {"di": di_dt, "df": df_dt},
        ).all()
    items = []
    by_status: dict[str, int] = {}
    by_status_primeiras: dict[str, int] = {}
    for r in rows:
        dh = r[3]
        if dh.tzinfo is None:
            dh = dh.replace(tzinfo=timezone.utc)
        dh_sp = dh.astimezone(SP_TZ)
        st = r[5]
        is_rev = bool(r[7])
        by_status[st] = by_status.get(st, 0) + 1
        if not is_rev:
            by_status_primeiras[st] = by_status_primeiras.get(st, 0) + 1
        items.append({
            "id": int(r[0]),
            "corretora_id": int(r[1]),
            "corretora_label": CORRETORA_LABEL.get(int(r[1]), "?"),
            "lead_nome": r[2],
            "data_hora_sp": dh_sp.isoformat(),
            "data": dh_sp.date().isoformat(),
            "hora": dh_sp.strftime("%H:%M"),
            "bairro": r[4],
            "status": st,
            "origem": r[6],
            "is_revisita": is_rev,
        })
    compareceu = by_status.get("compareceu", 0)
    no_show = by_status.get("no_show", 0)
    finalizadas = compareceu + no_show
    taxa = round((compareceu / finalizadas) * 100, 1) if finalizadas else None

    comp_p = by_status_primeiras.get("compareceu", 0)
    ns_p = by_status_primeiras.get("no_show", 0)
    fin_p = comp_p + ns_p
    taxa_p = round((comp_p / fin_p) * 100, 1) if fin_p else None
    total_primeiras = sum(by_status_primeiras.values())
    return {
        "total": len(items),
        "por_status": by_status,
        "taxa_comparecimento": taxa,
        "total_primeiras": total_primeiras,
        "total_revisitas": len(items) - total_primeiras,
        "por_status_primeiras": by_status_primeiras,
        "taxa_comparecimento_primeiras": taxa_p,
        "items": items,
    }


def resumo_comparecimento(por_status: dict[str, int]) -> dict[str, Any]:
    """Comparecimento da Visão: compareceu ÷ visitas ENCERRADAS.

    Encerrada = compareceu + no-show + cancelada, ou seja, a visita que já teve
    desfecho. Cancelada entra no denominador de propósito: pra operação, visita
    marcada que caiu é visita que não aconteceu. A fórmula antiga
    (compareceu ÷ compareceu + no-show) deixava as 11 canceladas de set/2026 de
    fora e mostrava 92,3%.

    Fica fora do denominador só o que ainda não teve desfecho: `a_realizar`
    (agendada/pendente de confirmação, passada ou futura). A tela mostra essa
    conta aberta à parte.
    """
    comp = int(por_status.get("compareceu", 0))
    no_show = int(por_status.get("no_show", 0))
    canceladas = int(por_status.get("cancelada", 0))
    encerradas = comp + no_show + canceladas
    return {
        "na_agenda": sum(int(v) for v in por_status.values()),
        "compareceu": comp,
        "no_show": no_show,
        "canceladas": canceladas,
        "encerradas": encerradas,
        "a_realizar": (int(por_status.get("agendada", 0))
                       + int(por_status.get("pendente_confirmacao", 0))),
        "taxa_comparecimento": round(comp / encerradas * 100, 1) if encerradas else None,
    }


ORIGENS_VENDA = ("midia", "indicacao", "parceiro", "nao_informada")


def vendas_por_origem(di: date, df: date) -> dict[str, dict[str, float]]:
    """Vendas e VGV do período por `vendas.origem`.

    Mesma janela do VGV do funil (que vem de `vendas_mensais`, alimentada por
    trigger a partir de `vendas`): mês inteiro de `data_venda`; quando o
    período não começa no dia 1, só a venda com `data_venda` dentro dele — é a
    regra `is_short` de `calcular_funil`. Nunca a venda do mês inteiro dividida
    pelo gasto de 7 dias.
    """
    out = {o: {"vendas": 0, "vgv": 0.0} for o in ORIGENS_VENDA}
    meses = _meses_no_periodo(di, df)
    if not meses:
        return out
    if di.day != 1:
        # Janela curta: venda com data_venda DENTRO da janela, como o VGV do
        # funil desde 06/10. Nunca a venda do mês inteiro sobre o gasto de 7 dias.
        with SessionLocal() as s:
            rows = s.execute(
                sql_text(
                    """SELECT origem, COUNT(*), COALESCE(SUM(valor), 0)
                         FROM vendas
                        WHERE tenant_id = 1 AND corretora_id IS NOT NULL
                          AND data_venda BETWEEN :di AND :df
                        GROUP BY origem"""
                ),
                {"di": di, "df": df},
            ).all()
        for origem, n, vgv in rows:
            o = origem if origem in out else "nao_informada"
            out[o]["vendas"] += int(n or 0)
            out[o]["vgv"] += float(vgv or 0)
        return out
    with SessionLocal() as s:
        rows = s.execute(
            sql_text(
                """SELECT origem, COUNT(*), COALESCE(SUM(valor), 0)
                     FROM vendas
                    WHERE tenant_id = 1
                      AND to_char(data_venda, 'YYYY-MM') = ANY(:meses)
                    GROUP BY origem"""
            ),
            {"meses": meses},
        ).all()
    for origem, n, vgv in rows:
        o = origem if origem in out else "nao_informada"
        out[o]["vendas"] += int(n or 0)
        out[o]["vgv"] += float(vgv or 0)
    return out


def montar_retorno(spend: float, vgv_total: float,
                   por_origem: dict[str, dict[str, float]]) -> dict[str, Any]:
    """ROI e custo por venda contando SÓ venda de mídia.

    `vgv_total` é o VGV do card (vendas_mensais) e não muda. O que não é mídia
    sai por diferença: VGV de mês antigo que está em `vendas_mensais` sem linha
    em `vendas` não tem origem conhecida, e vai pra `nao_informada` em vez de
    sumir. Assim VGV mídia + fora da mídia fecha sempre com o total.
    """
    vgv_midia = float(por_origem["midia"]["vgv"])
    vendas_midia = int(por_origem["midia"]["vendas"])
    indic = float(por_origem["indicacao"]["vgv"])
    parc = float(por_origem["parceiro"]["vgv"])
    fora = max(0.0, vgv_total - vgv_midia)
    return {
        "vgv_total": round(vgv_total, 2),
        "vgv_midia": round(vgv_midia, 2),
        "vendas_midia": vendas_midia,
        "vgv_fora_midia": round(fora, 2),
        "fora_midia": {
            "indicacao": {"vendas": int(por_origem["indicacao"]["vendas"]), "vgv": round(indic, 2)},
            "parceiro": {"vendas": int(por_origem["parceiro"]["vendas"]), "vgv": round(parc, 2)},
            "nao_informada": {"vendas": int(por_origem["nao_informada"]["vendas"]),
                              "vgv": round(max(0.0, fora - indic - parc), 2)},
        },
        "roi": round(vgv_midia / spend, 2) if spend else None,
        "custo_venda": round(spend / vendas_midia, 2) if spend and vendas_midia else None,
    }


def calcular_conversas(di: date, df: date) -> dict[str, Any]:
    di_dt = datetime.combine(di, datetime.min.time(), tzinfo=SP_TZ)
    df_dt = datetime.combine(df, datetime.max.time(), tzinfo=SP_TZ)
    with SessionLocal() as s:
        rows = s.execute(
            sql_text(
                """SELECT corretora_id,
                          COUNT(*) FILTER (WHERE from_corretora)       AS enviadas,
                          COUNT(*) FILTER (WHERE NOT from_corretora)   AS recebidas,
                          COUNT(DISTINCT contato_telefone)              AS contatos
                   FROM mensagens_whatsapp
                   WHERE corretora_id IS NOT NULL
                     AND timestamp_evolution BETWEEN :di AND :df
                   GROUP BY corretora_id
                   ORDER BY corretora_id"""
            ),
            {"di": di_dt, "df": df_dt},
        ).all()
    por_corretora = []
    total_env = total_rec = total_cont = 0
    for r in rows:
        cid = int(r[0])
        env = int(r[1] or 0)
        rec = int(r[2] or 0)
        cont = int(r[3] or 0)
        por_corretora.append({
            "corretora_id": cid,
            "label": CORRETORA_LABEL.get(cid, "?"),
            "enviadas": env,
            "recebidas": rec,
            "contatos_unicos": cont,
        })
        total_env += env
        total_rec += rec
        total_cont += cont
    ligacoes = _calcular_ligacoes(di, df)

    return {
        "equipe": {
            "enviadas": total_env,
            "recebidas": total_rec,
            "contatos_unicos_soma": total_cont,
        },
        "por_corretora": por_corretora,
        "ligacoes": ligacoes,
    }


def _calcular_ligacoes(di: date, df: date) -> dict[str, Any]:
    """Agrega ligacoes_semanais por corretora pros meses do período.

    Mapeamento semana → mês: segunda-feira da semana ISO determina o
    mês de referência (regra ISO 8601 padrão). Ex: semana 2026-W18
    começa em 2026-04-27 (seg) → conta em abril.
    """
    meses = _meses_no_periodo(di, df)
    if not meses:
        return {"equipe": _empty_ligacoes_equipe(), "por_corretora": []}

    with SessionLocal() as s:
        rows = s.execute(
            sql_text(
                """SELECT c.id, c.nome,
                          COALESCE(SUM(l.ligacoes),0)   AS total,
                          COALESCE(SUM(l.atendeu),0)    AS atendeu,
                          COALESCE(SUM(l.prosseguiu),0) AS prosseguiu,
                          COUNT(l.id)                    AS n_semanas
                   FROM corretoras c
                   LEFT JOIN ligacoes_semanais l
                     ON l.corretora_id = c.id
                    AND to_char(
                          to_date(l.ano_semana_iso || '-1', 'IYYY"-W"IW-ID'),
                          'YYYY-MM'
                        ) = ANY(:meses)
                   WHERE c.ativo = TRUE
                   GROUP BY c.id, c.nome
                   ORDER BY c.id"""
            ),
            {"meses": meses},
        ).all()

    por_corretora = []
    tot_lig = tot_at = tot_pr = 0
    for r in rows:
        cid = int(r[0])
        lig = int(r[2] or 0)
        at = int(r[3] or 0)
        pr = int(r[4] or 0)
        n_sem = int(r[5] or 0)
        por_corretora.append({
            "corretora_id": cid,
            "label": CORRETORA_LABEL.get(cid, "?"),
            "total": lig,
            "atendeu": at,
            "prosseguiu": pr,
            "taxa_atendimento_pct": _pct(at, lig),
            "taxa_conversao_pct": _pct(pr, at),
            "sem_reporte": n_sem == 0,
        })
        tot_lig += lig
        tot_at += at
        tot_pr += pr

    return {
        "equipe": {
            "total": tot_lig,
            "atendeu": tot_at,
            "prosseguiu": tot_pr,
            "taxa_atendimento_pct": _pct(tot_at, tot_lig),
            "taxa_conversao_pct": _pct(tot_pr, tot_at),
        },
        "por_corretora": por_corretora,
    }


def _empty_ligacoes_equipe() -> dict[str, Any]:
    return {"total": 0, "atendeu": 0, "prosseguiu": 0,
            "taxa_atendimento_pct": None, "taxa_conversao_pct": None}


# ── Período anterior (comparativo) ────────────────────────────────────────────


def _periodo_anterior(periodo: str, di: date, df: date) -> tuple[date, date]:
    """Calcula período de comparação:
       - mes_atual → mes_passado inteiro
       - mes_passado → mês anterior a ele
       - custom/range → mesma duração no mês anterior (shift -1 mês)"""
    if periodo == "mes_atual":
        primeiro = di.replace(day=1)
        fim_prev = primeiro - timedelta(days=1)
        ini_prev = fim_prev.replace(day=1)
        return ini_prev, fim_prev
    if periodo == "mes_passado":
        fim_prev = di - timedelta(days=1)
        ini_prev = fim_prev.replace(day=1)
        return ini_prev, fim_prev
    # custom / hoje / semana: shift de 1 mês mantendo mesmo dia
    def shift_month_back(d: date) -> date:
        if d.month == 1:
            y, m = d.year - 1, 12
        else:
            y, m = d.year, d.month - 1
        last_day = monthrange(y, m)[1]
        return d.replace(year=y, month=m, day=min(d.day, last_day))
    return shift_month_back(di), shift_month_back(df)


def _delta_sentido(atual: float, anterior: float) -> tuple[float | None, str]:
    """Retorna (delta_pct, sentido). sentido = maior/menor/igual (threshold ±2%)."""
    if not anterior:
        if atual == 0:
            return 0.0, "igual"
        return None, "maior"
    delta = ((atual - anterior) / abs(anterior)) * 100
    if abs(delta) < 2:
        return round(delta, 1), "igual"
    return round(delta, 1), "maior" if delta > 0 else "menor"


def calcular_comparativo(periodo: str, di: date, df: date,
                         meta_atual: dict[str, Any], funil_atual: dict[str, Any]) -> dict[str, Any]:
    """Compara 6 KPIs (Meta spend, leads_meta, CPL, vendas, VGV, ROI) atual vs anterior."""
    di_p, df_p = _periodo_anterior(periodo, di, df)
    meta_p = calcular_metricas_meta_ads(di_p, df_p)
    funil_p = calcular_funil(di_p, df_p)
    eq_p = funil_p["equipe"]

    eq_a = funil_atual["equipe"]
    spend_a = float(meta_atual.get("spend") or 0)
    spend_p = float(meta_p.get("spend") or 0)
    vgv_a = float(eq_a["vgv"] or 0)
    vgv_p = float(eq_p["vgv"] or 0)
    roi_a = (vgv_a / spend_a) if spend_a else 0.0
    roi_p = (vgv_p / spend_p) if spend_p else 0.0
    cpl_a = float(meta_atual.get("cpl") or 0)
    cpl_p = float(meta_p.get("cpl") or 0)
    leads_a = int(meta_atual.get("leads_meta") or 0)
    leads_p = int(meta_p.get("leads_meta") or 0)
    vendas_a = int(eq_a["vendas"] or 0)
    vendas_p = int(eq_p["vendas"] or 0)

    def block(atual: float, anterior: float) -> dict[str, Any]:
        delta_pct, sentido = _delta_sentido(atual, anterior)
        return {
            "atual": round(atual, 2),
            "anterior": round(anterior, 2),
            "delta_pct": delta_pct,
            "sentido": sentido,
        }

    return {
        "periodo_anterior": {"inicio": di_p.isoformat(), "fim": df_p.isoformat()},
        "investimento_meta_ads": block(spend_a, spend_p),
        "vgv": block(vgv_a, vgv_p),
        "roi": block(roi_a, roi_p),
        "cpl": block(cpl_a, cpl_p),
        "leads_meta": block(leads_a, leads_p),
        "vendas": block(vendas_a, vendas_p),
    }


# ── Vazamento de captação ─────────────────────────────────────────────────────


def calcular_vazamento(meta: dict[str, Any], funil_eq: dict[str, Any]) -> dict[str, Any]:
    """LEGADO: cálculo ingênuo (Meta vs leads_snapshot). Mantido pra compat."""
    captados = int(meta.get("leads_meta") or 0)
    no_funil = int(funil_eq.get("leads") or 0)
    fantasmas = max(0, captados - no_funil)
    taxa = round((fantasmas / captados) * 100, 1) if captados else None
    return {
        "captados_meta": captados,
        "no_funil_kommo": no_funil,
        "fantasmas": fantasmas,
        "taxa_vazamento_pct": taxa,
    }


META_LEADS_DB = "/opt/acme-kommo/data/kommo.db"
_PHONE_NON_DIGIT = re.compile(r"\D")


def _data_sp(dt: datetime) -> date:
    """Data no fuso de São Paulo. Datetime ingênuo é tratado como UTC (é como
    o banco devolve)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(SP_TZ).date()


def _norm9(p: str | None) -> str:
    d = _PHONE_NON_DIGIT.sub("", p or "")
    return d[-9:] if len(d) >= 9 else d


def calcular_vazamento_real(di: date, df: date) -> dict[str, Any]:
    """Cruzamento real meta_leads_log ↔ leads (painel V2) por phone normalizado.

    Retorna 4 buckets:
      novos_no_funil      — phone bate + lead criado no funil no período
      reengajados         — phone bate + lead criado ANTES (já estava na base)
      phone_invalido      — log do automacao-kommo com status='erro_phone_invalido'
      realmente_perdidos  — phone OK + sem match no funil

    Duas coisas mudaram no cutover de 27/07/2026 e zeravam este card (achado em
    04/08): o bridge passou a gravar status `kommo_off` em vez de `ok` — o lead
    é captado igual, só não vai mais pro Kommo — e o cruzamento era contra
    `leads_snapshot`, cache do Kommo que parou de ser reabastecido. Sem os dois
    ajustes o painel dizia "0 captados" com 30 leads entrando pela Meta.
    """
    di_iso = di.isoformat()
    df_iso = df.isoformat() + "T23:59:59"

    # 1) Carrega leads Meta do período (status ok + erro_phone_invalido)
    out_ok: list[dict[str, Any]] = []
    out_invalid: list[dict[str, Any]] = []
    try:
        c = sqlite3.connect(f"file:{META_LEADS_DB}?mode=ro", uri=True)
        try:
            c.row_factory = sqlite3.Row
            rows = c.execute(
                """SELECT leadgen_id, received_at, status, detail
                   FROM meta_leads_log
                   WHERE received_at BETWEEN ? AND ?
                   ORDER BY received_at""",
                (di_iso, df_iso),
            ).fetchall()
            import json as _json
            for r in rows:
                try:
                    d = _json.loads(r["detail"]) if r["detail"] else {}
                except Exception:
                    d = {}
                # `kommo_off` é o status pós-cutover: o webhook recebeu o lead e
                # deixou de encaminhar pro Kommo. Captação idêntica ao `ok`.
                if r["status"] in ("ok", "kommo_off"):
                    out_ok.append({"leadgen_id": r["leadgen_id"],
                                   "received_at": r["received_at"],
                                   "nome": d.get("name"), "phone": d.get("phone")})
                elif r["status"] == "erro_phone_invalido":
                    out_invalid.append({"leadgen_id": r["leadgen_id"],
                                        "received_at": r["received_at"],
                                        "nome": d.get("name"),
                                        "phone_raw": d.get("phone_raw")})
        finally:
            c.close()
    except Exception as e:
        log.error("vazamento_sqlite_error", error=str(e))

    if not out_ok and not out_invalid:
        return {
            "captados_meta": 0,
            "novos_no_funil": 0,
            "reengajados": 0,
            "phone_invalido": 0,
            "realmente_perdidos": 0,
            "taxa_vazamento_real_pct": None,
            "perdidos_detalhe": [],
        }

    # 2) Carrega o funil e indexa por sufixo 9 dígitos.
    # `leads` (V2) é a base viva; `leads_snapshot` entra só pro histórico
    # pré-cutover, quando o lead nasceu no Kommo e nunca foi migrado.
    with SessionLocal() as s:
        snap_rows = s.execute(
            sql_text(
                """SELECT CAST(id AS TEXT), telefone, created_at
                     FROM leads WHERE tenant_id = :t
                   UNION ALL
                   SELECT lead_id, telefone, criado_em_kommo FROM leads_snapshot"""
            ),
            {"t": 1},
        ).all()
    snap_by9: dict[str, list[tuple[str, datetime | None]]] = {}
    for r in snap_rows:
        key = _norm9(r[1] or "")
        if key:
            snap_by9.setdefault(key, []).append((str(r[0]), r[2]))

    # 3) Classifica
    novos = 0
    reengajados = 0
    sem_telefone = 0
    perdidos: list[dict[str, Any]] = []
    for m in out_ok:
        key = _norm9(m["phone"])
        if not key:
            # Depois do cutover o bridge grava `detail` como texto livre, sem
            # nome/telefone — dá pra saber que o lead chegou, não dá pra cruzar
            # com o funil. Chamar isso de "perdido" pintava 100% de vazamento
            # num mês em que o funil recebeu os leads normalmente. Fica num
            # bucket próprio até o bridge voltar a gravar o payload.
            sem_telefone += 1
            continue
        matches = snap_by9.get(key, [])
        if not matches:
            perdidos.append({**m, "tipo": "sem_match"})
            continue
        # Algum criado no período?
        criado_no_periodo = any(
            # Converte pro fuso do NEGÓCIO antes de comparar: `created_at` vem
            # timezone-aware (UTC) e a janela é em São Paulo. Sem isso, lead que
            # entra depois das 21h tem `.date()` do dia seguinte e cai FORA do
            # período — some do "novos no funil" e vira "perdido" sem ter sido.
            (cr is not None and di <= _data_sp(cr) <= df)
            for (_lid, cr) in matches
        )
        if criado_no_periodo:
            novos += 1
        else:
            reengajados += 1

    total_capt = len(out_ok) + len(out_invalid)
    perdidos_count = len(perdidos)
    invalid_count = len(out_invalid)
    # taxa real = perdidos / auditáveis. Phone inválido NÃO entra como perdido
    # (é bug), e lead sem telefone no log não entra em nenhum dos lados — senão
    # a taxa mede a lacuna do log, não o vazamento do funil.
    auditaveis = total_capt - sem_telefone
    taxa = round((perdidos_count / auditaveis) * 100, 1) if auditaveis else None

    # Lista pra detalhe (sumiço + inválido)
    detalhe = [{**p, "fonte": "meta_leads_log", "kind": "sumico"} for p in perdidos] + \
              [{**i, "fonte": "meta_leads_log", "kind": "phone_invalido"} for i in out_invalid]

    return {
        "captados_meta": total_capt,
        "novos_no_funil": novos,
        "reengajados": reengajados,
        "phone_invalido": invalid_count,
        "realmente_perdidos": perdidos_count,
        "sem_telefone_no_log": sem_telefone,
        "taxa_vazamento_real_pct": taxa,
        "perdidos_detalhe": detalhe[:50],  # limita pra payload não explodir
    }


# ── Atenção HOJE (KPI prioridade do dia) ─────────────────────────────────────


_TENDENCIA_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_TENDENCIA_TTL_SEC = 300  # 5min


def calcular_saude_sistema() -> dict[str, Any]:
    """Status dos jobs e fontes de dado.

    Retorna pra cada componente: ok/warning/error, última execução, detalhe.
    Pensado pra render em footer/admin do painel.
    """
    agora_utc = datetime.now(timezone.utc)
    out: dict[str, Any] = {"checked_at": agora_utc.isoformat()}

    def stale(dt: datetime | None, threshold_h: float) -> tuple[str, str]:
        """Retorna (nivel, msg) baseado em quão velha é a última execução."""
        if dt is None:
            return "error", "sem registro"
        delta_h = (agora_utc - dt).total_seconds() / 3600
        if delta_h <= threshold_h:
            return "ok", f"há {fmt_delta(delta_h)}"
        if delta_h <= threshold_h * 2:
            return "warn", f"há {fmt_delta(delta_h)} (atrasado)"
        return "error", f"há {fmt_delta(delta_h)} — verificar"

    def fmt_delta(h: float) -> str:
        if h < 1:
            return f"{int(h * 60)}min"
        if h < 24:
            return f"{int(h)}h"
        return f"{int(h / 24)}d"

    with SessionLocal() as s:
        # Inspector noturno (daily_metrics_corretora)
        r = s.execute(sql_text("SELECT MAX(gerado_em) FROM daily_metrics_corretora")).scalar()
        dt = r if isinstance(r, datetime) else None
        if dt and dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        nivel, msg = stale(dt, 26)  # roda 19:30 SP, 26h tolerância
        out["inspector"] = {"nivel": nivel, "ultima": dt.isoformat() if dt else None, "msg": msg}

        # Briefing 20h (Dona via Telegram)
        r = s.execute(sql_text(
            "SELECT MAX(sent_at) FROM briefings_sent WHERE destinatario='dona' AND success=TRUE"
        )).scalar()
        dt = r if isinstance(r, datetime) else None
        if dt and dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        nivel, msg = stale(dt, 26)
        out["briefing_dona"] = {"nivel": nivel, "ultima": dt.isoformat() if dt else None, "msg": msg}

        # Briefing 8h (Pessoa via WhatsApp)
        r = s.execute(sql_text(
            "SELECT MAX(sent_at) FROM briefings_sent WHERE destinatario='pessoa' AND success=TRUE"
        )).scalar()
        dt = r if isinstance(r, datetime) else None
        if dt and dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        nivel, msg = stale(dt, 26)
        out["briefing_pessoa"] = {"nivel": nivel, "ultima": dt.isoformat() if dt else None, "msg": msg}

        # Webhook Evolution (mensagens_whatsapp)
        r = s.execute(sql_text("SELECT MAX(recebido_em) FROM mensagens_whatsapp")).scalar()
        dt = r if isinstance(r, datetime) else None
        if dt and dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        nivel, msg = stale(dt, 4)  # esperamos mensagens nas últimas horas
        out["webhook_evolution"] = {"nivel": nivel, "ultima": dt.isoformat() if dt else None, "msg": msg}

        # Backfill visitas (visitas.atualizado_em ou data_hora — usamos created_at do upsert)
        r = s.execute(sql_text("SELECT MAX(GREATEST(criado_em, atualizado_em)) FROM visitas")).scalar()
        dt = r if isinstance(r, datetime) else None
        if dt and dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        nivel, msg = stale(dt, 4)  # cron hora a hora
        out["backfill_visitas"] = {"nivel": nivel, "ultima": dt.isoformat() if dt else None, "msg": msg}

        # Webhook Meta Lead Ads (kommo.db.meta_leads_log via timestamp received_at)
        # — fonte SQLite, faz separado
    try:
        c = sqlite3.connect(f"file:{META_LEADS_DB}?mode=ro", uri=True)
        try:
            row = c.execute("SELECT MAX(received_at) FROM meta_leads_log").fetchone()
        finally:
            c.close()
        raw = row[0] if row else None
        dt = datetime.fromisoformat(raw) if raw else None
        if dt and dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        nivel, msg = stale(dt, 48)  # leads podem ter intervalo grande
        out["webhook_meta_ads"] = {"nivel": nivel, "ultima": dt.isoformat() if dt else None, "msg": msg}
    except Exception as e:
        log.error("saude_meta_error", error=str(e))
        out["webhook_meta_ads"] = {"nivel": "error", "ultima": None, "msg": "erro ao ler kommo.db"}

    # Taxa de sucesso briefings (30 dias)
    with SessionLocal() as s:
        rows = s.execute(sql_text(
            """SELECT destinatario,
                      COUNT(*) FILTER (WHERE success) AS ok,
                      COUNT(*) FILTER (WHERE NOT success) AS falha
               FROM briefings_sent
               WHERE sent_at >= NOW() - INTERVAL '30 days'
               GROUP BY destinatario"""
        )).all()
    taxa_falha = []
    for dest, ok, falha in rows:
        total = (ok or 0) + (falha or 0)
        if total == 0:
            continue
        taxa_falha.append({
            "destinatario": dest,
            "ok": int(ok or 0),
            "falha": int(falha or 0),
            "taxa_falha_pct": round((falha / total) * 100, 1),
        })
    out["briefings_30d"] = taxa_falha
    return out


# ── Carimbo de frescor (F5 / P0.2 da Revisão-02-Funil) ──────────────────────────
# As telas do imob leem de fontes com idades diferentes (análises 07:00/19:30,
# snapshot Kommo diário, conversas ao vivo, agregados mensais). Sem carimbo, o
# usuário não sabe a idade do que vê — caso real 01/07: card da Pessoa12 mostrava 5
# recebidos às 20h com 41 no banco. Este bloco expõe o "dados de quando" REAL
# de cada fonte, com cache de 60s pra não pesar (1 query agregada por fonte).

# Cache em memória: {"ts": epoch_do_cache, "data": {fonte: iso}}
_FRESCOR_CACHE: dict[str, Any] = {"ts": 0.0, "data": None}
_FRESCOR_TTL_S = 60.0


def _iso_utc(dt: datetime | None) -> str | None:
    """Normaliza um timestamp pra ISO UTC (naive → assume UTC)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _frescor_fontes() -> dict[str, str | None]:
    """Retorna {nome_da_fonte: timestamp_iso} com o 'dados de quando' REAL.

    Fontes cobertas (todas somente-leitura):
      - analises            → MAX(lead_analyses.analyzed_at)      (Inspector 07:00/19:30)
      - snapshot_kommo      → MAX(leads_snapshot.ultima_sync)     (etapa no card, diário)
      - conversas           → MAX(mensagens_whatsapp.timestamp_evolution) (ao vivo)
      - vendas_mensais      → MAX(vendas_mensais.atualizado_em)    (métricas mensais, 23h)
      - atendidos_evolution → MAX(atendimentos_evolution_mensais.atualizado_em)

    Cache de 60s: telas do imob chamam vários endpoints em sequência; sem cache
    seriam 5 MAX() por request. Cada MAX() usa índice/scan barato, mas o cache
    corta o custo sob refresh rápido.
    """
    import time as _time

    now = _time.monotonic()
    cached = _FRESCOR_CACHE.get("data")
    if cached is not None and (now - _FRESCOR_CACHE["ts"]) < _FRESCOR_TTL_S:
        return cached  # type: ignore[return-value]

    out: dict[str, str | None] = {}
    # (nome exposto, SQL de MAX). Cada um é isolado: falha de um não derruba os outros.
    queries: list[tuple[str, str]] = [
        ("analises", "SELECT MAX(analyzed_at) FROM lead_analyses"),
        ("snapshot_kommo", "SELECT MAX(ultima_sync) FROM leads_snapshot"),
        ("conversas", "SELECT MAX(timestamp_evolution) FROM mensagens_whatsapp"),
        ("vendas_mensais", "SELECT MAX(atualizado_em) FROM vendas_mensais"),
        ("atendidos_evolution",
         "SELECT MAX(atualizado_em) FROM atendimentos_evolution_mensais"),
    ]
    try:
        with SessionLocal() as s:
            for nome, sql in queries:
                try:
                    r = s.execute(sql_text(sql)).scalar()
                    out[nome] = _iso_utc(r if isinstance(r, datetime) else None)
                except Exception:
                    log.warning("frescor_fonte_falhou", fonte=nome)
                    out[nome] = None
    except Exception:
        log.exception("frescor_sessao_falhou")
        # Degradação graciosa: devolve o que der (todas None) sem quebrar o endpoint.
        out = {nome: None for nome, _ in queries}

    _FRESCOR_CACHE["ts"] = now
    _FRESCOR_CACHE["data"] = out
    return out


def calcular_tendencia_14d() -> dict[str, list[dict[str, Any]]]:
    """Série diária dos últimos 14 dias pra sparklines:
       - meta_leads_diario: leads do Meta Ads (insights_diario, cliente_slug=acme)
       - mensagens_diario: total/enviadas/recebidas
       - mensagens_por_corretora: dict corretora_id → série diária
    """
    import time as _t
    cached = _TENDENCIA_CACHE.get("_")
    if cached and (_t.time() - cached[0]) < _TENDENCIA_TTL_SEC:
        return cached[1]
    hoje = _hoje_sp()
    di = hoje - timedelta(days=13)  # 14 dias incluindo hoje
    di_dt = datetime.combine(di, datetime.min.time(), tzinfo=SP_TZ)
    df_dt = datetime.combine(hoje, datetime.max.time(), tzinfo=SP_TZ)

    # Pré-popula com zeros pra garantir 14 entradas mesmo sem dados
    dias = [(di + timedelta(days=i)).isoformat() for i in range(14)]

    # Meta Ads (SQLite trafego)
    meta_diario: dict[str, int] = {d: 0 for d in dias}
    try:
        uri = f"file:{TRAFEGO_DB}?mode=ro"
        c = sqlite3.connect(uri, uri=True)
        try:
            rows = c.execute(
                """SELECT data, COALESCE(SUM(leads), 0)
                   FROM insights_diario
                   WHERE cliente_slug = ?
                     AND data BETWEEN ? AND ?
                   GROUP BY data""",
                (ACME_SLUG, di.isoformat(), hoje.isoformat()),
            ).fetchall()
            for d, q in rows:
                if d in meta_diario:
                    meta_diario[d] = int(q or 0)
        finally:
            c.close()
    except Exception as e:
        log.error("tendencia_meta_error", error=str(e))

    # Mensagens Evolution (agregado por dia em SP timezone)
    msg_total: dict[str, int] = {d: 0 for d in dias}
    msg_env: dict[str, int] = {d: 0 for d in dias}
    msg_rec: dict[str, int] = {d: 0 for d in dias}
    msg_corr: dict[int, dict[str, int]] = {cid: {d: 0 for d in dias} for cid in CORRETORA_KOMMO_USER}

    with SessionLocal() as s:
        rows = s.execute(
            sql_text(
                """SELECT (timestamp_evolution AT TIME ZONE 'America/Sao_Paulo')::date::text AS d,
                          corretora_id,
                          from_corretora,
                          COUNT(*) AS n
                   FROM mensagens_whatsapp
                   WHERE corretora_id IS NOT NULL
                     AND timestamp_evolution BETWEEN :di AND :df
                   GROUP BY 1, 2, 3"""
            ),
            {"di": di_dt, "df": df_dt},
        ).all()
    for d, cid_raw, from_corr, n in rows:
        if d not in msg_total:
            continue
        msg_total[d] += int(n)
        if from_corr:
            msg_env[d] += int(n)
        else:
            msg_rec[d] += int(n)
        cid = int(cid_raw) if cid_raw else 0
        if cid in msg_corr:
            msg_corr[cid][d] += int(n)

    def to_series(d: dict[str, int]) -> list[dict[str, Any]]:
        return [{"data": k, "qtde": v} for k, v in sorted(d.items())]

    result = {
        "dias": dias,
        "meta_leads": to_series(meta_diario),
        "mensagens_total": to_series(msg_total),
        "mensagens_enviadas": to_series(msg_env),
        "mensagens_recebidas": to_series(msg_rec),
        "mensagens_por_corretora": {
            str(cid): to_series(msg_corr[cid]) for cid in msg_corr
        },
    }
    _TENDENCIA_CACHE["_"] = (_t.time(), result)
    return result


def calcular_atencao_hoje() -> dict[str, Any]:
    """Resumo do que precisa atenção HOJE — usado pelo card no topo da Visão Geral.

    Combina:
    - lead_analyses do dia (temperatura + gap)
    - visitas agendadas pra hoje
    - tarefas atrasadas do painel V2 (do cache existente)
    """
    hoje_sp = _hoje_sp()
    di_dt = datetime.combine(hoje_sp, datetime.min.time(), tzinfo=SP_TZ)
    df_dt = datetime.combine(hoje_sp, datetime.max.time(), tzinfo=SP_TZ)

    with SessionLocal() as s:
        # Lead analyses do dia
        rows = s.execute(
            sql_text(
                """SELECT corretora_id, temperatura, COUNT(*) FILTER (WHERE has_gap) com_gap, COUNT(*) total
                   FROM lead_analyses
                   WHERE analyzed_at::date = :hoje
                   GROUP BY 1, 2"""
            ),
            {"hoje": hoje_sp},
        ).all()

        # Visitas agendadas pra hoje (qualquer status exceto cancelada)
        v_rows = s.execute(
            sql_text(
                """SELECT id, lead_nome, data_hora, status, corretora_id
                   FROM visitas
                   WHERE data_hora BETWEEN :di AND :df
                     AND status != 'cancelada'
                   ORDER BY data_hora"""
            ),
            {"di": di_dt, "df": df_dt},
        ).all()

    quentes_total = 0
    mornos_gap_total = 0
    por_corretora_hot: dict[int, dict[str, int]] = {}
    for cid_raw, temp, com_gap, total in rows:
        cid = int(cid_raw) if cid_raw else 0
        por_corretora_hot.setdefault(cid, {"quentes": 0, "mornos_com_gap": 0})
        if temp == "quente":
            quentes_total += int(total)
            por_corretora_hot[cid]["quentes"] += int(total)
        elif temp == "morno":
            mornos_gap_total += int(com_gap or 0)
            por_corretora_hot[cid]["mornos_com_gap"] += int(com_gap or 0)

    visitas_hoje = []
    for r in v_rows:
        dh = r[2]
        if dh.tzinfo is None:
            dh = dh.replace(tzinfo=timezone.utc)
        dh_sp = dh.astimezone(SP_TZ)
        visitas_hoje.append({
            "id": int(r[0]),
            "lead_nome": r[1],
            "hora": dh_sp.strftime("%H:%M"),
            "status": r[3],
            "corretora_id": int(r[4]),
            "corretora_label": CORRETORA_LABEL.get(int(r[4]), "?"),
        })

    # Tarefas atrasadas — usa o cache existente (1 chamada Kommo, 10min TTL)
    tarefas = _coletar_tarefas_atrasadas_por_corretora()
    tarefas_por_corr = [
        {"corretora_id": cid, "label": CORRETORA_LABEL.get(cid, "?"), "qtde": n}
        for cid, n in sorted(tarefas.items(), key=lambda x: x[1], reverse=True)
        if n > 0
    ]

    return {
        "data": hoje_sp.isoformat(),
        "leads_quentes": {
            "total": quentes_total,
            "por_corretora": [
                {"corretora_id": cid, "label": CORRETORA_LABEL.get(cid, "?"), "qtde": v["quentes"]}
                for cid, v in por_corretora_hot.items() if v["quentes"] > 0
            ],
        },
        "leads_mornos_com_gap": {
            "total": mornos_gap_total,
            "por_corretora": [
                {"corretora_id": cid, "label": CORRETORA_LABEL.get(cid, "?"), "qtde": v["mornos_com_gap"]}
                for cid, v in por_corretora_hot.items() if v["mornos_com_gap"] > 0
            ],
        },
        "visitas_hoje": visitas_hoje,
        "tarefas_atrasadas": {
            "total": sum(tarefas.values()),
            "por_corretora": tarefas_por_corr,
        },
    }


# ── Recuperados (Remarketing/Follow-up que voltam pro funil) ────────────────


KOMMO_API_BASE = "https://example.kommo.com/api/v4"
STATUS_RECUPERADOS = 10000010  # pipeline 10000004 (Funil de Vendas) / "Recuperados"


def _kommo_token() -> str | None:
    """Lê KOMMO_TOKEN do .env do automacao-kommo."""
    import os
    token = os.environ.get("ACME_KOMMO_TOKEN")
    if token:
        return token
    try:
        with open("/opt/acme-kommo/.env") as f:
            for line in f:
                if line.startswith("KOMMO_TOKEN="):
                    return line.strip().split("=", 1)[1]
    except Exception as e:
        log.error("kommo_token_read_error", error=str(e))
    return None


_KOMMO_EVENTS_CACHE: dict[tuple[str, str], tuple[float, list[dict[str, Any]]]] = {}
_KOMMO_CACHE_TTL_SEC = 120
import threading as _threading
_KOMMO_EVENTS_LOCK = _threading.Lock()


def _kommo_fetch_status_events(di: date, df: date) -> list[dict[str, Any]]:
    """Pega TODOS lead_status_changed events do período (paginado).
    Cache em memória com TTL 120s — múltiplas chamadas no mesmo request lifecycle reusam.
    Single-flight via lock pra evitar thundering herd ao expirar."""
    import time as _t
    key = (di.isoformat(), df.isoformat())
    now = _t.time()
    cached = _KOMMO_EVENTS_CACHE.get(key)
    if cached and (now - cached[0]) < _KOMMO_CACHE_TTL_SEC:
        return cached[1]
    with _KOMMO_EVENTS_LOCK:
        cached = _KOMMO_EVENTS_CACHE.get(key)
        if cached and (_t.time() - cached[0]) < _KOMMO_CACHE_TTL_SEC:
            return cached[1]
        token = _kommo_token()
        if not token:
            return []
        di_ts = int(datetime.combine(di, datetime.min.time(), tzinfo=timezone.utc).timestamp())
        df_ts = int(datetime.combine(df, datetime.max.time(), tzinfo=timezone.utc).timestamp())
        all_events: list[dict[str, Any]] = []
        try:
            import httpx
            with httpx.Client(timeout=30.0, headers={"Authorization": f"Bearer {token}"}) as cli:
                page = 1
                while True:
                    r = cli.get(f"{KOMMO_API_BASE}/events", params={
                        "filter[type]": "lead_status_changed",
                        "filter[created_at][from]": di_ts,
                        "filter[created_at][to]": df_ts,
                        "page": page, "limit": 250,
                    })
                    if r.status_code == 204:
                        break
                    if r.status_code != 200:
                        log.error("kommo_events_error", status=r.status_code)
                        break
                    evs = r.json().get("_embedded", {}).get("events", [])
                    if not evs:
                        break
                    all_events.extend(evs)
                    if len(evs) < 250:
                        break
                    page += 1
                    if page > 25:
                        break
        except Exception as e:
            log.error("kommo_events_fetch_error", error=str(e))
        _KOMMO_EVENTS_CACHE[key] = (_t.time(), all_events)
        return all_events


def calcular_atendidos_breakdown(di: date, df: date) -> dict[str, Any]:
    """Pra cada lead que entrou em 'em atendimento' no período, classifica:
       - 'novo' se veio direto de Entrou
       - 'recuperado' se veio via Recuperados
       - 'outro' qualquer outro caminho

    Agrega por corretora_id (Postgres) usando mapeamento KOMMO_USER_CORRETORA.
    Retorna `equipe.{novos,recuperados,outros}` e `por_corretora[cid].{novos,recuperados}`.
    """
    events = _kommo_fetch_status_events(di, df)

    # Pra classificar atendimento, precisa do value_before. Filtra events
    # → em_atendimento e olha what status era antes.
    atend_events = []
    for e in events:
        after = e.get("value_after") or []
        if not after or not isinstance(after, list):
            continue
        ls = after[0].get("lead_status") if isinstance(after[0], dict) else None
        if not ls or ls.get("id") != STATUS_EM_ATENDIMENTO:
            continue
        if ls.get("pipeline_id") != 10000004:
            continue
        before = e.get("value_before") or []
        before_id = None
        if before and isinstance(before, list) and isinstance(before[0], dict):
            ls_b = before[0].get("lead_status")
            if ls_b:
                before_id = ls_b.get("id")
        atend_events.append({
            "lead_id": e.get("entity_id"),
            "by": e.get("created_by"),
            "before": before_id,
        })

    # Dedup por lead — se passou por em_atendimento múltiplas vezes, conta a primeira.
    seen: dict[int, dict[str, Any]] = {}
    for ev in atend_events:
        lid = ev["lead_id"]
        if lid in seen:
            continue
        seen[lid] = ev

    # Cruza com leads_snapshot.criado_em_kommo pra garantir "atend NOVO no mês"
    # = lead que VEM via Entrou + foi criado no período (não de meses anteriores).
    lead_ids_via_entrou = [str(lid) for lid, ev in seen.items() if ev["before"] == STATUS_ENTROU]
    leads_novos_do_periodo: set[int] = set()
    if lead_ids_via_entrou:
        with SessionLocal() as s:
            rows = s.execute(
                sql_text(
                    "SELECT lead_id FROM leads_snapshot "
                    "WHERE lead_id = ANY(:ids) "
                    "AND criado_em_kommo >= :di AND criado_em_kommo < :df_next"
                ),
                {"ids": lead_ids_via_entrou,
                 "di": datetime.combine(di, datetime.min.time()),
                 "df_next": datetime.combine(df + timedelta(days=1), datetime.min.time())},
            ).all()
        leads_novos_do_periodo = {int(r[0]) for r in rows}

    eq_novos = eq_recup = eq_outros = 0
    por_corr: dict[int, dict[str, int]] = {}
    for cid in CORRETORA_KOMMO_USER:
        por_corr[cid] = {"novos": 0, "recuperados": 0}

    for lid, ev in seen.items():
        before_id = ev["before"]
        cid = KOMMO_USER_CORRETORA.get(ev["by"])  # corretora que moveu pra atendimento
        if before_id == STATUS_ENTROU and lid in leads_novos_do_periodo:
            # Atend de lead criado no mês — conta como novo
            eq_novos += 1
            if cid:
                por_corr[cid]["novos"] += 1
        elif before_id == STATUS_RECUPERADOS:
            eq_recup += 1
            if cid:
                por_corr[cid]["recuperados"] += 1
        elif before_id == STATUS_ENTROU:
            # Atendido via Entrou mas lead é de meses anteriores — "atrasado"
            eq_outros += 1
        else:
            eq_outros += 1

    return {
        "equipe": {"novos": eq_novos, "recuperados": eq_recup, "outros": eq_outros},
        "por_corretora": por_corr,
        "total_classificados": len(seen),
    }


def calcular_recuperados_por_corretora(di: date, df: date) -> dict[int, int]:
    """Quantos leads únicos foram pra 'Recuperados' no período, atribuídos
    à corretora responsável atual (pra leads movidos pela automação Kommo).
    Retorna {corretora_id: count}."""
    events = _kommo_fetch_status_events(di, df)

    eventos_recup = []
    for e in events:
        after = e.get("value_after") or []
        if not after or not isinstance(after, list):
            continue
        ls = after[0].get("lead_status") if isinstance(after[0], dict) else None
        if not ls or ls.get("id") != STATUS_RECUPERADOS:
            continue
        eventos_recup.append({"lead_id": e.get("entity_id"), "by": e.get("created_by")})

    lead_corretora: dict[int, int] = {}
    # Pass 1: corretora direta
    for ev in eventos_recup:
        cid = KOMMO_USER_CORRETORA.get(ev["by"])
        if cid:
            lead_corretora[ev["lead_id"]] = cid

    # Pass 2: automação Kommo → busca responsible atual via BULK
    # (GET /leads?filter[id][]=X1&filter[id][]=X2... resolve até 250 num call)
    token = _kommo_token()
    leads_auto = [ev["lead_id"] for ev in eventos_recup
                  if ev["by"] == 0 and ev["lead_id"] not in lead_corretora]
    if leads_auto and token:
        try:
            import httpx
            import urllib.parse as _up
            with httpx.Client(timeout=30.0, headers={"Authorization": f"Bearer {token}"}) as cli:
                # Bulk em batches de 50 ids — 1 chamada por batch
                for i in range(0, len(leads_auto), 50):
                    batch = leads_auto[i:i+50]
                    params = [("filter[id][]", str(x)) for x in batch] + [("limit", "250")]
                    url = f"{KOMMO_API_BASE}/leads?{_up.urlencode(params)}"
                    r = cli.get(url)
                    if r.status_code != 200:
                        continue
                    for l in r.json().get("_embedded", {}).get("leads", []):
                        ruid = l.get("responsible_user_id")
                        cid = KOMMO_USER_CORRETORA.get(ruid)
                        if cid:
                            lead_corretora[l["id"]] = cid
        except Exception as e:
            log.error("recuperados_por_corretora_lookup_error", error=str(e))

    out: dict[int, int] = {cid: 0 for cid in CORRETORA_KOMMO_USER}
    for cid in lead_corretora.values():
        out[cid] = out.get(cid, 0) + 1
    return out


def calcular_recuperados(di: date, df: date) -> dict[str, Any]:
    """Mantida pra retro-compat — agora consome `calcular_recuperados_por_corretora`."""
    por_corr = calcular_recuperados_por_corretora(di, df)
    total = sum(por_corr.values())
    por_user_list = []
    for cid, qtde in sorted(por_corr.items(), key=lambda x: x[1], reverse=True):
        if qtde > 0:
            por_user_list.append({
                "user_id": CORRETORA_KOMMO_USER.get(cid, 0),
                "label": CORRETORA_LABEL.get(cid, "?"),
                "qtde": qtde,
            })
    return {"total": total, "movimentos": total, "por_user": por_user_list}


def _empty_recuperados() -> dict[str, Any]:
    return {"total": 0, "movimentos": 0, "por_user": []}


# ── Operação Corretor (HSM Recovery) ────────────────────────────────────────────────


def calcular_recovery_corretor(di: date, df: date) -> dict[str, Any]:
    """Operação da Corretor no período — o CARROSSEL (`app/services/carrossel_corretor.py`).

    Lia `recovery_conversations`, que é o "recuperador legado": ele foi
    aposentado DE PROPÓSITO em 08/07/2026 e a última linha da tabela é de 07/07.
    Como o card só somava dali, ele mostrava zero desde julho — enquanto a Corretor
    seguia trabalhando todo dia (109 mensagens no dia em que isso foi
    encontrado, 04/08). Erro de fonte, não de operação parada.

    A operação viva é o carrossel: leads passeiam por Fila → Disparado →
    Respondeu, com descanso de ~30 dias antes de voltar pra fila. Cada disparo
    grava `carrossel_disparo` em `lead_events`, e cada resposta,
    `carrossel_resposta` — é de lá que sai a contagem do período.
    """
    di_dt = datetime.combine(di, datetime.min.time(), tzinfo=SP_TZ)
    df_dt = datetime.combine(df + timedelta(days=1), datetime.min.time(), tzinfo=SP_TZ)
    params = {"di": di_dt, "df": df_dt}

    with SessionLocal() as s:
        disparados = int(s.execute(sql_text(
            """SELECT COUNT(DISTINCT lead_id) FROM lead_events
               WHERE tipo = 'carrossel_disparo' AND at >= :di AND at < :df"""
        ), params).scalar() or 0)

        responderam = int(s.execute(sql_text(
            # Mesma régua do placar (Dona 05/10/2026): na taxa só entra
            # interesse e botão. Recusa, parar e indefinida ficam de fora;
            # evento anterior à regra não tem classe e segue contando.
            """SELECT COUNT(DISTINCT lead_id) FROM lead_events
               WHERE tipo = 'carrossel_resposta' AND at >= :di AND at < :df
                 AND (payload->>'classe' IS NULL
                      OR payload->>'classe' IN ('interesse', 'botao'))"""
        ), params).scalar() or 0)

        # Estoque atual das etapas — não depende do período, e é o que responde
        # "quanta base ainda tem pra trabalhar".
        por_etapa = {
            int(r[0]): int(r[1])
            for r in s.execute(sql_text(
                """SELECT stage_id, COUNT(*) FROM leads
                   WHERE stage_id IN (:fila, :disp, :resp) GROUP BY stage_id"""
            ), {"fila": 23, "disp": 24, "resp": 25}).all()
        }

    return {
        "disparados": disparados,
        # Disparado e ainda calado. Nunca negativo: resposta pode chegar de um
        # disparo do período anterior, e "-3 aguardando" numa tela é pior que
        # um número levemente conservador.
        "aguardando": max(disparados - responderam, 0),
        "responderam": responderam,
        "taxa_resposta_pct": (
            round(responderam / disparados * 100, 1) if disparados else None
        ),
        "na_fila": por_etapa.get(23, 0),
        "disparados_total": por_etapa.get(24, 0),
        "responderam_total": por_etapa.get(25, 0),
    }


def _empty_recovery_corretor() -> dict[str, Any]:
    return {"disparados": 0, "aguardando": 0, "responderam": 0,
            "taxa_resposta_pct": None, "na_fila": 0,
            "disparados_total": 0, "responderam_total": 0}


# ── Remarketing Pessoa12 (instância Cloud acme_corretora_a) ─────────────────────────

# Decisão Dona 26/07: Pessoa3 (corretora_id 3) saiu da roleta de leads novos
# e virou a operação de REMARKETING (follow-up/reengajamento da base).
# Régua dela: respostas → agendamentos (não venda).
REMARKETING_INSTANCE = "acme_corretora_a"
REMARKETING_CORRETORA_ID = 3


def calcular_remarketing_corretor(di: date, df: date) -> dict[str, Any]:
    """Indicadores da operação de remarketing da Pessoa12 no período.

    Fontes:
    - toques_enviados: mensagens_whatsapp (instance=acme_corretora_a,
      from_corretora=TRUE, tipo_mensagem='template').
    - leads_responderam: contatos DISTINTOS com inbound na instância no
      período. Aproximação documentada: não correlaciona cada inbound a um
      toque anterior específico — aceitável porque a instância é dedicada
      ao remarketing (todo inbound ali é reação à régua).
    - respostas_cadencia: cadencia_estado status='respondida' da Pessoa12, usando
      atualizado_em como proxy do momento da resposta (não há timestamp
      dedicado de transição de status).
    - agendamentos: MESMA fonte do funil da Visão (tabela visitas,
      status <> 'cancelada', data_hora no período, corretora_id=3).
    """
    di_dt = datetime.combine(di, datetime.min.time(), tzinfo=SP_TZ)
    df_dt = datetime.combine(df, datetime.max.time(), tzinfo=SP_TZ)
    params = {"inst": REMARKETING_INSTANCE, "cid": REMARKETING_CORRETORA_ID,
              "di": di_dt, "df": df_dt}
    with SessionLocal() as s:
        toques = int(s.execute(sql_text(
            """SELECT COUNT(*) FROM mensagens_whatsapp
               WHERE instance = :inst AND from_corretora = TRUE
                 AND tipo_mensagem = 'template'
                 AND timestamp_evolution BETWEEN :di AND :df"""
        ), params).scalar() or 0)
        responderam = int(s.execute(sql_text(
            """SELECT COUNT(DISTINCT contato_telefone) FROM mensagens_whatsapp
               WHERE instance = :inst AND from_corretora = FALSE
                 AND timestamp_evolution BETWEEN :di AND :df"""
        ), params).scalar() or 0)
        respostas_cad = int(s.execute(sql_text(
            """SELECT COUNT(*) FROM cadencia_estado
               WHERE status = 'respondida' AND corretora_id = :cid
                 AND atualizado_em BETWEEN :di AND :df"""
        ), params).scalar() or 0)
        agendamentos = int(s.execute(sql_text(
            """SELECT COUNT(*) FROM visitas
               WHERE corretora_id = :cid AND status <> 'cancelada'
                 AND data_hora BETWEEN :di AND :df"""
        ), params).scalar() or 0)
    return {
        "corretora_id": REMARKETING_CORRETORA_ID,
        "label": CORRETORA_LABEL.get(REMARKETING_CORRETORA_ID, "Pessoa12"),
        "instance": REMARKETING_INSTANCE,
        "toques_enviados": toques,
        "leads_responderam": responderam,
        "respostas_cadencia": respostas_cad,
        "agendamentos": agendamentos,
        "taxa_resposta_para_agendamento": _pct(agendamentos, responderam),
    }


# ── Tempo de resposta (TTR) ───────────────────────────────────────────────────


def calcular_ttr(di: date, df: date) -> dict[str, Any]:
    """Tempo de resposta mediano/médio/p90 por corretora.

    Janela: 24h, sem filtro de horário comercial.
    Pra cada (corretora, contato, dia): primeiro par lead→resposta da corretora.
    Filtra DISTINCT ON pra não duplicar respostas mid-stream.
    """
    di_dt = datetime.combine(di, datetime.min.time(), tzinfo=SP_TZ)
    df_dt = datetime.combine(df, datetime.max.time(), tzinfo=SP_TZ)
    with SessionLocal() as s:
        rows = s.execute(
            sql_text(
                """WITH leadmsg AS (
                     SELECT DISTINCT ON (m.corretora_id, m.contato_telefone, date_trunc('day', m.timestamp_evolution))
                            m.corretora_id, m.contato_telefone, m.timestamp_evolution AS lead_ts
                     FROM mensagens_whatsapp m
                     WHERE m.corretora_id IS NOT NULL
                       AND m.from_corretora = FALSE
                       AND m.timestamp_evolution BETWEEN :di AND :df
                     ORDER BY m.corretora_id, m.contato_telefone, date_trunc('day', m.timestamp_evolution), m.timestamp_evolution
                   ),
                   pairs AS (
                     SELECT l.corretora_id, l.lead_ts,
                            (SELECT MIN(r.timestamp_evolution)
                             FROM mensagens_whatsapp r
                             WHERE r.corretora_id = l.corretora_id
                               AND r.contato_telefone = l.contato_telefone
                               AND r.from_corretora = TRUE
                               AND r.timestamp_evolution > l.lead_ts
                               AND r.timestamp_evolution <= l.lead_ts + interval '24 hours'
                            ) AS resp_ts
                     FROM leadmsg l
                   )
                   SELECT corretora_id,
                          COUNT(*) FILTER (WHERE resp_ts IS NOT NULL) AS n_respondidas,
                          COUNT(*)                                      AS n_total,
                          percentile_cont(0.5) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM (resp_ts - lead_ts))/60) AS mediana_min,
                          AVG(EXTRACT(EPOCH FROM (resp_ts - lead_ts))/60)                                          AS media_min,
                          percentile_cont(0.9) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM (resp_ts - lead_ts))/60) AS p90_min
                   FROM pairs
                   WHERE resp_ts IS NOT NULL
                   GROUP BY corretora_id
                   ORDER BY corretora_id"""
            ),
            {"di": di_dt, "df": df_dt},
        ).all()

    por_corretora = []
    todos_medianas: list[float] = []
    todos_medias: list[float] = []
    todos_p90: list[float] = []
    for r in rows:
        cid = int(r[0])
        n_resp = int(r[1] or 0)
        n_tot = int(r[2] or 0)
        med = float(r[3]) if r[3] is not None else None
        avg = float(r[4]) if r[4] is not None else None
        p90 = float(r[5]) if r[5] is not None else None
        por_corretora.append({
            "corretora_id": cid,
            "label": CORRETORA_LABEL.get(cid, "?"),
            "n_respondidas": n_resp,
            "n_iniciadas": n_tot,
            "mediana_min": round(med, 1) if med is not None else None,
            "media_min": round(avg, 1) if avg is not None else None,
            "p90_min": round(p90, 1) if p90 is not None else None,
        })
        if med is not None: todos_medianas.append(med)
        if avg is not None: todos_medias.append(avg)
        if p90 is not None: todos_p90.append(p90)

    def med_of(arr: list[float]) -> float | None:
        if not arr:
            return None
        sorted_arr = sorted(arr)
        n = len(sorted_arr)
        if n % 2:
            return round(sorted_arr[n // 2], 1)
        return round((sorted_arr[n // 2 - 1] + sorted_arr[n // 2]) / 2, 1)

    return {
        "equipe": {
            "mediana_min": med_of(todos_medianas),
            "media_min": round(sum(todos_medias) / len(todos_medias), 1) if todos_medias else None,
            "p90_min": med_of(todos_p90),
        },
        "por_corretora": por_corretora,
    }


TAREFAS_ATRASADAS_LIMITE = 40


_TAREFAS_CACHE: dict[str, tuple[float, dict[int, int]]] = {}
_TAREFAS_TTL_SEC = 600  # 10 min — tarefas atrasadas mudam devagar
_TAREFAS_LOCK = _threading.Lock()


def _coletar_tarefas_atrasadas_por_corretora() -> dict[int, int]:
    """Mapeia corretora_id → count de tarefas abertas com data vencida no V2.

    Lia a API do Kommo (`get_tasks(filter_overdue=True)`). Depois do cutover de
    27/07/2026 ninguém mais mexe em tarefa lá: o número parou de andar e o
    painel ficou acusando 132/66/57 tarefas atrasadas — estoque congelado de um
    CRM que a equipe não abre mais. A fila viva é `tarefas_corretora`, criada e
    concluída dentro do próprio painel. Trocado em 04/08/2026.

    Bônus: acabou a chamada externa serial (3x Kommo), que era o gargalo do
    /visao-geral. O cache de 10min fica porque o alerta não precisa ser exato ao
    minuto e a query roda em toda carga da tela.
    """
    import time as _t
    cached = _TAREFAS_CACHE.get("_")
    if cached and (_t.time() - cached[0]) < _TAREFAS_TTL_SEC:
        return cached[1]
    with _TAREFAS_LOCK:
        cached = _TAREFAS_CACHE.get("_")
        if cached and (_t.time() - cached[0]) < _TAREFAS_TTL_SEC:
            return cached[1]
        with SessionLocal() as s:
            # Toda corretora ativa entra no mapa, mesmo com zero atrasada —
            # senão "sumiu do alerta" e "não tem tarefa" viram a mesma coisa.
            out = {
                int(r[0]): 0
                for r in s.execute(
                    sql_text("SELECT id FROM corretoras WHERE ativo = TRUE")
                ).all()
            }
            rows = s.execute(
                sql_text(
                    """SELECT corretora_id, COUNT(*)
                         FROM tarefas_corretora
                        WHERE status = 'aberta'
                          AND corretora_id IS NOT NULL
                          AND data_tarefa < (NOW() AT TIME ZONE 'America/Sao_Paulo')::date
                        GROUP BY corretora_id"""
                )
            ).all()
        for cid, n in rows:
            out[int(cid)] = int(n or 0)
        _TAREFAS_CACHE["_"] = (_t.time(), out)
        return out


def detectar_alertas(funil_eq: dict[str, Any],
                     meta: dict[str, Any],
                     visitas: dict[str, Any],
                     funil_corretoras: list[dict[str, Any]] | None = None,
                     vgv_midia: float | None = None) -> list[dict[str, Any]]:
    """Regra A — conservador, só extremos.

    `vgv_midia`: quando vem, o alerta de ROI mede só venda de mídia — o mesmo
    ROI do card. Sem ele, cai no VGV total (comportamento antigo).
    """
    alertas: list[dict[str, Any]] = []
    spend = float(meta.get("spend") or 0)
    leads = int(funil_eq.get("leads") or 0)
    vendas = int(funil_eq.get("vendas") or 0)
    vgv = float(funil_eq.get("vgv") or 0) if vgv_midia is None else float(vgv_midia)
    cpl = meta.get("cpl")
    taxa_comp = visitas.get("taxa_comparecimento")

    if spend > 0 and leads == 0:
        alertas.append({"nivel": "alto", "categoria": "ads",
                        "msg": f"Investiu R${spend:.0f} sem nenhum lead no período."})
    if cpl is not None and cpl > 200:
        alertas.append({"nivel": "alto", "categoria": "cpl",
                        "msg": f"CPL alto: R${cpl:.0f}/lead."})
    if leads >= 20 and vendas == 0:
        alertas.append({"nivel": "alto", "categoria": "conversao",
                        "msg": f"{leads} leads no período sem nenhuma venda."})
    if taxa_comp is not None and taxa_comp < 30 and visitas.get("total", 0) >= 5:
        alertas.append({"nivel": "alto", "categoria": "comparecimento",
                        "msg": f"Taxa de comparecimento muito baixa: {taxa_comp}%."})
    if spend > 0 and vgv > 0:
        roi = vgv / spend
        if roi < 5 and vendas > 0:
            alertas.append({"nivel": "medio", "categoria": "roi",
                            "msg": f"ROI baixo: {roi:.1f}x (VGV/investimento)."})

    # Tarefas atrasadas por corretora (snapshot atual; não depende do período).
    tarefas = _coletar_tarefas_atrasadas_por_corretora()
    for cid, n in tarefas.items():
        if n > TAREFAS_ATRASADAS_LIMITE:
            alertas.append({
                "nivel": "alto",
                "categoria": "tarefas_atrasadas",
                "corretora_id": cid,
                "corretora_label": CORRETORA_LABEL.get(cid, f"C{cid}"),
                "valor": n,
                "limite": TAREFAS_ATRASADAS_LIMITE,
                "msg": f"{CORRETORA_LABEL.get(cid, '?')}: {n} tarefas atrasadas (limite {TAREFAS_ATRASADAS_LIMITE}).",
            })

    # Mês sem visita por corretora (no período).
    visitas_por_corretora: dict[int, int] = {}
    for v in visitas.get("items", []):
        cid = int(v.get("corretora_id") or 0)
        if cid:
            visitas_por_corretora[cid] = visitas_por_corretora.get(cid, 0) + 1
    if funil_corretoras:
        for c in funil_corretoras:
            cid = int(c.get("corretora_id") or 0)
            if not cid:
                continue
            if visitas_por_corretora.get(cid, 0) == 0:
                alertas.append({
                    "nivel": "medio",
                    "categoria": "mes_sem_visita",
                    "corretora_id": cid,
                    "corretora_label": c.get("label") or CORRETORA_LABEL.get(cid, f"C{cid}"),
                    "msg": f"{c.get('label') or '?'}: nenhuma visita agendada no período.",
                })

    return alertas


# ── Auth helper ───────────────────────────────────────────────────────────────


def _identificar_usuario(remote_user: str | None) -> str:
    if not remote_user:
        # nginx repassa o usuário autenticado em X-Painel-User. Sem isso → bloqueia.
        raise HTTPException(status_code=403, detail="painel_auth_required")
    # Fail-CLOSED: usuário precisa ser admin OU corretora conhecida. Sem isso, um
    # login htpasswd fora dos mapas cairia em my_cid=None e seria tratado como
    # visão total (fail-open) pelos endpoints. Todo usuário legítimo está em
    # ADMINS ou USUARIO_CORRETORA — quem não está, não entra.
    u = remote_user.lower()
    if u not in ADMINS and u not in USUARIO_CORRETORA:
        raise HTTPException(status_code=403, detail="painel_user_desconhecido")
    return remote_user


def _corretora_id_do_user(user: str) -> int | None:
    """Retorna corretora_id se user for corretora, None se for admin."""
    return USUARIO_CORRETORA.get(user.lower())


def _checar_cobranca(instance: str | None, *, lead_id: str, user: str) -> None:
    """422 quando a linha está recusando por cobrança na Meta.

    Não é frescura de UX: enquanto a conta está travada, TUDO que sai pela API
    daquela linha é recusado, e a corretora fica mandando no vazio achando que
    falou. O contorno (mandar pelo celular dela) só acontece se alguém contar.
    Sai sozinho quando a linha volta a entregar — ver `linha_saude`.
    """
    from app.services import linha_saude

    with SessionLocal() as s:
        if not linha_saude.bloqueada_por_cobranca(s, instance):
            return
    log.warning(
        "painel_envio_bloqueado_cobranca",
        lead_id=lead_id, instance=instance, user=user,
    )
    raise HTTPException(422, "linha_bloqueada_cobranca")


def _cids_atendimento(my_cid: int | None) -> list[int] | None:
    """corretora_ids que o viewer pode ATENDER (a dela + as delegadas).

    None = admin (sem filtro). Usado nas listas do chat; as consultas fazem
    `corretora_id = ANY(:cids)` com `CAST(:cids AS INT[]) IS NULL` pro admin.
    """
    if my_cid is None:
        return None
    return [my_cid, *sorted(DELEGACAO_ATENDIMENTO.get(my_cid, ()))]


def _pode_atender(my_cid: int | None, dona_cid: int | None) -> bool:
    """True se o viewer pode abrir/responder a conversa de `dona_cid`.

    Admin (my_cid None) pode sempre. Corretora pode a própria e as delegadas
    (ver DELEGACAO_ATENDIMENTO).
    """
    if my_cid is None:
        return True
    if dona_cid is not None and int(dona_cid) == my_cid:
        return True
    return dona_cid in DELEGACAO_ATENDIMENTO.get(my_cid, set())


def _owner_ids_atendimento(my_cid: int) -> set[int]:
    """Valores de `leads.owner_id` que a corretora pode trabalhar no CRM.

    `owner_id` na espinha guarda o id interno (inspector) OU o kommo_user_id
    (webhook fino), então cada corretora de `_cids_atendimento` entra com os
    dois. Sem delegação devolve o mesmo conjunto de sempre ({my_cid, kuid}).
    """
    ids: set[int] = set()
    for cid in _cids_atendimento(my_cid) or ():
        ids.add(cid)
        kuid = CORRETORA_KOMMO_USER.get(cid)
        if kuid is not None:
            ids.add(kuid)
    return ids


def _escopo_board(
    cids_visiveis: list[int] | None,
    corretora: int | None,
    *,
    com_kommo_user: bool = False,
) -> tuple[int | None, set[int] | None]:
    """Escopo dos boards do CRM → (filtro_corretora, owner_ids | None).

    `cids_visiveis` = `_cids_atendimento(my_cid)`: None pra admin, senão a
    corretora + as delegadas. `corretora` é o `?corretora=` da tela.

      • admin: o filtro é o que ele pedir (None = todas → owner_ids None);
      • corretora: o filtro só vale se cair DENTRO do que ela já vê — fora
        disso é ignorado e ela segue vendo o escopo inteiro dela (nunca amplia).

    `filtro_corretora` é a corretora única do recorte, ou None quando o recorte
    tem mais de uma (admin sem filtro, Pessoa12 sem filtro).

    `com_kommo_user`: o board de remarketing sempre aceitou `owner_id` gravado
    como kommo_user_id; o principal compara só o id interno. Cada um mantém a
    regra que já tinha — Pessoa10 e Pessoa11 não mudam de resultado em nenhum dos dois.
    """
    if cids_visiveis is None:
        cids = [corretora] if corretora is not None else None
    elif corretora is not None and corretora in cids_visiveis:
        cids = [corretora]
    else:
        cids = list(cids_visiveis)
    if cids is None:
        return None, None
    owner_ids = set(cids)
    if com_kommo_user:
        for cid in cids:
            kuid = CORRETORA_KOMMO_USER.get(cid)
            if kuid is not None:
                owner_ids.add(kuid)
    return (cids[0] if len(cids) == 1 else None), owner_ids


def _is_admin(user: str) -> bool:
    return user.lower() in ADMINS


def _require_admin(user: str) -> None:
    """Gate pra endpoints admin-only (dados agregados de todas as corretoras:
    ranking, histórico com spend/ROI, remarketing global). Corretora → 403."""
    if not _is_admin(user):
        raise HTTPException(status_code=403, detail="painel_admin_required")


# ── Endpoints ─────────────────────────────────────────────────────────────────


def calcular_coaching(di: date, df: date, filter_cid: int | None = None,
                      apenas_pendentes: bool = False) -> dict[str, Any]:
    """Coaching: leads quentes e mornos-com-gap por corretora no período.

    `filter_cid`: se passado, filtra só leads dessa corretora.
    `apenas_pendentes`: se True, mostra só os ainda não consumidos
       (consumed_by_corretora=FALSE) — usado pelo painel da corretora.

    Lê lead_analyses, monta lista por corretora com:
    - lead_name, conversa_resumo, gap_identificado, abordagem_sugerida
    Limita 10 por corretora pra payload não explodir / mostrar 10 cards.
    """
    di_dt = datetime.combine(di, datetime.min.time())
    df_dt = datetime.combine(df + timedelta(days=1), datetime.min.time())

    where_extra = ""
    params: dict[str, Any] = {"di": di_dt, "df": df_dt}
    if filter_cid is not None:
        where_extra += " AND la.corretora_id = :cid"
        params["cid"] = filter_cid
    if apenas_pendentes:
        where_extra += " AND la.consumed_by_corretora = FALSE"

    with SessionLocal() as s:
        rows = s.execute(
            sql_text(
                f"""SELECT la.id, la.lead_id, la.corretora_id, la.temperatura, la.has_gap,
                          la.raw_analysis, la.analyzed_at,
                          la.consumed_by_corretora,
                          ls.nome AS lead_nome
                   FROM lead_analyses la
                   LEFT JOIN leads_snapshot ls ON ls.lead_id = la.lead_id
                   WHERE la.analyzed_at >= :di AND la.analyzed_at < :df
                     AND (la.temperatura IN ('quente', 'morno') OR la.has_gap = TRUE)
                     {where_extra}
                   ORDER BY
                     CASE la.temperatura WHEN 'quente' THEN 1 WHEN 'morno' THEN 2 ELSE 3 END,
                     la.analyzed_at DESC"""
            ),
            params,
        ).all()

    por_corretora: dict[int, dict[str, list]] = {}
    for cid in CORRETORA_KOMMO_USER:
        por_corretora[cid] = {"quentes": [], "mornos_com_gap": []}

    totais = {"quentes": 0, "mornos_com_gap": 0, "frios_com_gap": 0}

    for r in rows:
        analysis_id = int(r[0])
        cid = int(r[2]) if r[2] else 0
        temp = r[3]
        has_gap = bool(r[4])
        raw = r[5] or {}
        consumed = bool(r[7])
        nome = raw.get("lead_name") or r[8] or "?"
        item = {
            "analysis_id": analysis_id,
            "lead_id": r[1],
            "nome": nome,
            "temperatura": temp,
            "has_gap": has_gap,
            "ultima_msg_at": raw.get("ultima_msg_at"),
            "ultima_msg_de": raw.get("ultima_msg_de"),
            "conversa_resumo": raw.get("conversa_resumo"),
            "gap_identificado": raw.get("gap_identificado"),
            "abordagem_sugerida": raw.get("abordagem_sugerida"),
            "o_que_corretora_disse": raw.get("o_que_corretora_disse"),
            "tarefas_atrasadas": len(raw.get("tarefas_kommo_atrasadas") or []),
            "etapa_desatualizada": bool(raw.get("etapa_kommo_desatualizada")),
            "analyzed_at": str(r[6]),
            "consumido": consumed,
        }
        if cid not in por_corretora:
            continue
        if temp == "quente":
            if len(por_corretora[cid]["quentes"]) < 10:
                por_corretora[cid]["quentes"].append(item)
            totais["quentes"] += 1
        elif temp == "morno" and has_gap:
            if len(por_corretora[cid]["mornos_com_gap"]) < 10:
                por_corretora[cid]["mornos_com_gap"].append(item)
            totais["mornos_com_gap"] += 1
        elif has_gap:
            totais["frios_com_gap"] += 1

    return {
        "totais": totais,
        "por_corretora": [
            {
                "corretora_id": cid,
                "label": CORRETORA_LABEL.get(cid, "?"),
                "nome": CORRETORA_NOME_DB.get(cid, "?"),
                "quentes": por_corretora[cid]["quentes"],
                "mornos_com_gap": por_corretora[cid]["mornos_com_gap"],
            }
            for cid in CORRETORA_KOMMO_USER
        ],
    }


@router.get("/coaching")
def painel_coaching(
    periodo: str = Query("mes_atual"),
    inicio: str | None = Query(None),
    fim: str | None = Query(None),
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    user = _identificar_usuario(x_painel_user)
    my_cid = _corretora_id_do_user(user)
    di, df, label = parse_periodo(periodo, inicio, fim)
    # Corretora: filtra por cid + apenas pendentes (não checados). Admin vê tudo.
    data = calcular_coaching(di, df, filter_cid=my_cid, apenas_pendentes=bool(my_cid))
    return {
        "user": user,
        "viewer": _viewer_info(user),
        "periodo": {"tipo": periodo, "inicio": di.isoformat(),
                    "fim": df.isoformat(), "label": label},
        **data,
    }


@router.post("/coaching/visto")
def painel_coaching_marca_visto(
    body: dict[str, Any],
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Marca uma análise como vista/resolvida pela corretora.

    Body: {"analysis_id": int}
    Só permite se o lead pertence à corretora logada.
    """
    user = _identificar_usuario(x_painel_user)
    my_cid = _corretora_id_do_user(user)
    if my_cid is None and not _is_admin(user):
        raise HTTPException(403, "Só corretoras podem marcar")
    aid = body.get("analysis_id")
    if not aid:
        raise HTTPException(400, "analysis_id obrigatório")
    with SessionLocal() as s:
        # Verifica ownership se for corretora
        if my_cid is not None:
            owned = s.execute(sql_text(
                "SELECT 1 FROM lead_analyses WHERE id = :aid AND corretora_id = :cid"
            ), {"aid": aid, "cid": my_cid}).first()
            if not owned:
                raise HTTPException(403, "Análise não pertence à corretora")
        s.execute(sql_text(
            "UPDATE lead_analyses SET consumed_by_corretora = TRUE WHERE id = :aid"
        ), {"aid": aid})
        s.commit()
    return {"status": "ok", "analysis_id": aid}


# ── Escrita de etapa (Missão 11 · Twenty) ────────────────────────────────────


def _corretora_id_do_lead(lead_id: str) -> int | None:
    """corretora_id dona do lead, via leads_snapshot (None se lead desconhecido)."""
    with SessionLocal() as s:
        row = s.execute(
            sql_text("SELECT corretora_id FROM leads_snapshot WHERE lead_id = :lid"),
            {"lid": str(lead_id)},
        ).first()
    return int(row[0]) if row and row[0] is not None else None


@router.post("/lead/{lead_id}/etapa")
def painel_lead_muda_etapa(
    lead_id: str,
    body: dict[str, Any] = Body(...),
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Muda a etapa (stage) de um lead — escreve no Twenty (Missão 11).

    Body: {"novo_stage": "<VALUE>"} onde VALUE é um twenty_stage ATIVO do stage_map.
    Contrato pro front (drag-and-drop do kanban): manda o twenty_stage da coluna
    de destino; recebe {ok, de, para} (de = stage anterior, para = novo).

    Gates:
      • viewer precisa ser a corretora dona do lead OU admin (403 caso contrário);
      • novo_stage precisa existir e estar ativo no stage_map (400);
      • lead precisa ter opportunity no Twenty (kommoLeadId == lead_id) — senão 404
        {"error": "lead_fora_do_piloto"} (Twenty hoje só tem os leads da Pessoa12).
    """
    from app.integrations.twenty_client import TwentyClient
    from app.services import stage_map as _stage_map

    user = _identificar_usuario(x_painel_user)
    is_admin = _is_admin(user)
    my_cid = _corretora_id_do_user(user)

    novo_stage = (body.get("novo_stage") or "").strip()
    if not novo_stage:
        raise HTTPException(400, "novo_stage_obrigatorio")

    validos = _stage_map.stages_validos()
    if validos and novo_stage not in validos:
        raise HTTPException(400, "stage_invalido")

    # Ownership: corretora só mexe no próprio lead. Admin mexe em qualquer um.
    if not is_admin:
        if my_cid is None:
            raise HTTPException(403, "painel_user_sem_corretora")
        dono = _corretora_id_do_lead(lead_id)
        if dono is not None and dono != my_cid:
            raise HTTPException(403, "lead_de_outra_corretora")
        if dono is None:
            # lead sem dono conhecido no snapshot → corretora não pode agir às cegas.
            raise HTTPException(403, "lead_sem_corretora")

    client = TwentyClient()
    opp = client.get_opportunity_by_kommo_id(lead_id)
    if not opp:
        raise HTTPException(404, {"error": "lead_fora_do_piloto"})

    de = opp.get("stage")
    if de == novo_stage:
        log.info("painel_lead_etapa_noop", lead_id=lead_id, stage=novo_stage, user=user)
        return {"ok": True, "de": de, "para": novo_stage, "noop": True}

    client.update_stage(str(opp["id"]), novo_stage)
    log.info("painel_lead_etapa_mudada", lead_id=lead_id, opp=opp.get("id"),
             de=de, para=novo_stage, user=user)
    return {"ok": True, "de": de, "para": novo_stage}


# ── Atendimento (Onda Inspector v2 · Fase 6) ─────────────────────────────────

_TEMPERATURA_ORDER = {"quente": 0, "morno": 1, "frio": 2, None: 3}


def _kommo_lead_url(lead_id: str) -> str:
    return f"https://example.kommo.com/leads/detail/{lead_id}"


def _stage_map() -> dict[str, str]:
    """Lê nomes de etapas Kommo. Falha → dict vazio (degradação graciosa)."""
    import json as _json
    try:
        from app.tools.kommo import KommoTool
        return _json.loads(KommoTool().get_pipeline_stages())
    except Exception:
        log.exception("painel_atendimento_stage_map_failed")
        return {}


# ── Dono canônico do lead (fonte ÚNICA de verdade pra escopo) ────────────────
#
# Regra (L2 · auditoria 26/07): o dono do lead é `leads.owner_id` (espinha
# canônica), NORMALIZADO — owner_id guarda o id interno da corretora OU o
# kommo_user_id, dependendo de quem escreveu (inspector vs webhook fino).
# Fallback explícito: owner_id normalizado → leads_snapshot.corretora_id,
# porque 557 leads da espinha têm owner_id NULL e alguns leads só existem
# no snapshot (sem linha em `leads`) — ler owner_id cru trancaria a corretora
# fora deles.
#
# `lead_analyses.corretora_id` NÃO é fonte de dono em NENHUM gate: é artefato
# de rodada de análise (pipeline._save_analysis insere linha nova a cada run
# com o `responsible` do Kommo), então sempre vencia o UPDATE do /crm/transferir
# — causa dos leads com card no board da dona certa e 403 no chat.
#
# Expressão SQL equivalente ao helper, pra usar em WHERE/JOIN sem round-trip.
# Espera `ls` (leads_snapshot) no escopo e faz LEFT JOIN próprio na espinha.
_DONO_CANONICO_SQL = """
    COALESCE(
        CASE
            WHEN ldc.owner_id IS NULL THEN NULL
            WHEN ldc.owner_id IN :kommo_user_ids THEN
                CASE ldc.owner_id
                    {kommo_cases}
                END
            ELSE ldc.owner_id
        END,
        ls.corretora_id
    )
"""


def _dono_canonico_sql() -> str:
    """Expressão SQL do dono canônico (mesma regra de `_dono_canonico`).

    Usa `ldc` como alias da espinha (`leads`) e `ls` como alias do snapshot —
    quem chama precisa ter feito o LEFT JOIN de `ldc`. Ver `_join_dono_sql`.
    """
    cases = " ".join(
        f"WHEN {kuid} THEN {cid}" for kuid, cid in KOMMO_USER_CORRETORA.items()
    )
    ids = ", ".join(str(k) for k in KOMMO_USER_CORRETORA)
    return _DONO_CANONICO_SQL.format(kommo_cases=cases).replace(
        "IN :kommo_user_ids", f"IN ({ids})"
    )


def _fonte_busca_sql(tenant_id: int = 1) -> str:
    """Universo de leads da busca: espinha (`leads`) UNIÃO cache do Kommo.

    O `leads_snapshot` é carga ANTIGA do Kommo e não é reabastecido por nada —
    em 30/07/2026 tinha 1.426 linhas contra 7.612 da espinha. Como a busca lia
    só o snapshot, 6.263 leads REAIS não apareciam nem por nome nem por
    telefone (a lead Fulano, da Pessoa12, foi o caso que levantou isso): o card
    existia no CRM e no Chat, mas o campo de busca jurava que não existia.

    Lead SEM vínculo Kommo (`external_ref` nulo — criado à mão no painel ou
    nativo do intake) entra com o id da ESPINHA no lugar. Antes ele era
    filtrado fora e ficava invisível na busca: existia no CRM e o campo de
    busca jurava que não (caso Edison, 21/09). O chat aceita os dois ids —
    `_lead_conversa_contexto` tenta o snapshot e cai em
    `_contexto_lead_nativo`.

    Sai um `lead_id` por lead (id do Kommo). O snapshot manda nos campos quando
    existe — é ele que tem etapa_kommo e as edições manuais antigas; o que só
    vive na espinha entra pelo segundo braço, com a etapa canônica resolvida
    por `pipeline_stages` (o snapshot guarda id do Kommo, a espinha o nome).
    """
    t = int(tenant_id)
    return f"""
        SELECT s.lead_id, s.nome, s.telefone, s.corretora_id, s.etapa_kommo,
               s.criado_em_kommo::timestamptz AS criado_em_kommo,
               NULL::text AS etapa_canonica
          FROM leads_snapshot s
         WHERE s.tenant_id = {t}
        UNION ALL
        SELECT coalesce(l.external_ref, l.id::text), l.nome, l.telefone,
               NULL::int, NULL::text, l.created_at, ps.nome
          FROM leads l
          LEFT JOIN pipeline_stages ps ON ps.id = l.stage_id
         WHERE l.tenant_id = {t}
           AND NOT EXISTS (
                 SELECT 1 FROM leads_snapshot s2
                  WHERE s2.lead_id = l.external_ref AND s2.tenant_id = l.tenant_id
               )
    """


def _join_dono_sql(tenant_id: int = 1) -> str:
    """LEFT JOIN da espinha usado junto de `_dono_canonico_sql()`.

    Casa por `external_ref` (id do Kommo) e, pra quem NÃO tem vínculo Kommo,
    pelo id da própria espinha — é assim que lead criado à mão no painel
    encontra o dono dele. Sem o segundo braço, o dono saía NULL e o lead era
    descartado pelo filtro de corretora: sumia da busca mesmo existindo no CRM
    (caso Edison, 21/09).

    O segundo braço exige `external_ref IS NULL` de propósito: sem isso, um id
    de espinha que por acaso tenha o mesmo valor de um id do Kommo casaria duas
    linhas e duplicaria o resultado. Assim cada lead entra por um caminho só.

    O cast fica dentro de CASE: com `WHERE ls.lead_id = :lid` o Postgres troca
    ls.lead_id pela constante e dobra o `::bigint` no plano, ANTES do regex —
    ref não numérico (`corretor:…`, `manual:…`) estourava o /conversa inteiro
    (Isa e leads do 0000, 21/10). CASE é a única ordem que o Postgres garante.
    """
    t = int(tenant_id)
    return (
        "LEFT JOIN leads ldc "
        f"ON ldc.tenant_id = {t} AND ("
        "     ldc.external_ref = ls.lead_id"
        "  OR (ldc.external_ref IS NULL"
        "      AND ldc.id = CASE WHEN ls.lead_id ~ '^[0-9]+$'"
        "                        THEN ls.lead_id::bigint END)"
        ")"
    )


def _normaliza_owner(owner_id: int | None) -> int | None:
    """owner_id cru (id interno OU kommo_user_id) → corretora_id interno."""
    if owner_id is None:
        return None
    owner_id = int(owner_id)
    return KOMMO_USER_CORRETORA.get(owner_id, owner_id)


# ── Acesso emprestado: quem disparou vê SE o lead respondeu ──────────────────
#
# Decisão Dona 03/09/2026 (caso Rebeca, lead da Pessoa10): a nutrição/remarketing
# dispara TODO toque pela linha da Pessoa12 (`services/nutricao.py`, spec 10/07). Se o
# lead responde, a conversa passa a viver no aparelho de quem disparou — mas o
# card é da dona, e o gate de escopo dava 403 em quem estava com o cliente na
# mão. A resposta da Rebeca ("quando tiver oportunidade boa pode mandar") ficou
# um dia parada por causa disso: a Pessoa10 não via a mensagem (thread filtrada por
# linha, corrigido em 02/09) e a Pessoa12 não via o card.
#
# A regra é literal: **silêncio não abre nada**. Disparo sozinho não dá acesso a
# lead alheio — só a RESPOSTA do cliente abre, e só enquanto a conversa está
# viva. É o mesmo critério que já acende o card na fila `/conversa/aguardando`
# da corretora, então ela não ganha visibilidade de nada que já não estivesse
# piscando pra ela.
#
# E é acesso de LEITURA. Atender pelo painel (enviar, reengajar, mover,
# transferir, adotar) segue com a dona: quem disparou responde pelo próprio
# WhatsApp, onde a conversa já está.
ACESSO_RESPOSTA_DIAS = 30


def _respondeu_na_linha(
    telefone: str | None, corretora_id: int | None, dias: int = ACESSO_RESPOSTA_DIAS
) -> bool:
    """True se o LEAD respondeu na linha desta corretora dentro da janela."""
    if not telefone or corretora_id is None:
        return False
    suf = "".join(ch for ch in str(telefone) if ch.isdigit())[-8:]
    if len(suf) < 8:
        return False
    with SessionLocal() as s:
        achou = s.execute(
            sql_text(
                """
                SELECT 1
                  FROM mensagens_whatsapp
                 WHERE corretora_id = :cid
                   AND from_corretora = FALSE
                   AND right(regexp_replace(coalesce(contato_telefone, ''),
                                            '\\D', '', 'g'), 8) = :suf
                   AND timestamp_evolution > now() - (:dias || ' days')::interval
                 LIMIT 1
                """
            ),
            {"cid": int(corretora_id), "suf": suf, "dias": int(dias)},
        ).first()
    return achou is not None


def _sql_lead_por_p8() -> str:
    """SELECT (p8, lead_id, nome, etapa) de TODO lead que tem telefone.

    Une snapshot do Kommo + espinha (`leads`), snapshot com prioridade (`ord`).
    Existe porque o chat resolvia o card SÓ pelo snapshot: lead que nasceu de
    anúncio (meta_leadgen) não tem linha lá, então o card caía em
    `kommoLeadId = "tel:<telefone>"` — e criar tarefa nele dava 404, porque o
    backend procura por id de lead, não por telefone (caso Felipe Batista,
    Pessoa10, 31/07: duas tentativas às 12:38 e 12:40).

    `etapa` vem como id do Kommo no snapshot e como NOME na espinha; quem usa
    passa pelo `_stage_map()`, que devolve o valor intacto quando não é id.

    Lead SEM vínculo Kommo entra com o id da espinha (mesma convenção de
    `_ref_id_espinha`: id de espinha é numérico e curto, external_ref do Kommo
    tem 15-16 dígitos — não colidem). Antes ele era filtrado fora e a conversa
    dele aparecia no chat como TELEFONE SOLTO na coluna "Fora do funil", como
    se fosse outra pessoa — o card existia no CRM com nome e tudo (caso Edison,
    21/09).
    """
    return r"""
        SELECT DISTINCT ON (p8) p8, lead_id, nome, etapa FROM (
            SELECT right(regexp_replace(ls.telefone, '\D', '', 'g'), 8) AS p8,
                   ls.lead_id::text AS lead_id, ls.nome, ls.etapa_kommo AS etapa, 0 AS ord
              FROM leads_snapshot ls
             WHERE ls.telefone IS NOT NULL
            UNION ALL
            SELECT right(regexp_replace(l.telefone, '\D', '', 'g'), 8),
                   coalesce(l.external_ref, l.id::text), l.nome, ps.nome, 1
              FROM leads l
              LEFT JOIN pipeline_stages ps ON ps.id = l.stage_id
             WHERE l.telefone IS NOT NULL
        ) u
        WHERE p8 <> ''
        ORDER BY p8, ord
    """


def _ref_id_espinha(ref: str) -> int | None:
    """`ref` é um id de `leads` (espinha) e não um external_ref?

    Regra igual à do _contexto_lead_nativo: numérico e curto (< 10 dígitos).
    external_ref do Kommo/WhatsApp tem 15-16 dígitos, então não colide.
    """
    r = str(ref or "").strip()
    if r.isdigit() and len(r) < 10:
        return int(r)
    return None


def _dono_canonico(lead_ref: str) -> tuple[int | None, bool, str | None]:
    """Dono canônico do lead → (corretora_id | None, existe, lead_nome).

    `existe` é devolvido SEPARADO do dono pra distinguir "lead não existe"
    (→ 404) de "lead existe e não tem dona" (→ admin-only, nunca 404: são os
    5.704 leads legítimos que só vivem na espinha/snapshot sem owner).

    Considera o lead existente se ele tem linha em `leads_snapshot` OU em
    `leads` — por `external_ref` (id do Kommo) ou pelo id da própria espinha,
    que é como lead criado à mão / nativo do intake é referenciado. Sem o
    segundo caminho ele respondia 404 "lead_nao_encontrado" em tudo que passa
    por este gate: criar tarefa, nota, mudar etapa (caso Edison, 21/09).
    """
    from app.services.lead_spine import DEFAULT_TENANT_ID

    # "tel:<dígitos>" = card que o chat não conseguiu casar com um lead. Se o
    # telefone TEM lead, resolve por ele em vez de devolver 404 (rede de
    # segurança do caso Felipe Batista, 31/07 — ver _sql_lead_por_p8).
    if str(lead_ref).startswith("tel:"):
        p8 = "".join(c for c in str(lead_ref)[4:] if c.isdigit())[-8:]
        if len(p8) == 8:
            with SessionLocal() as s:
                achado = s.execute(
                    sql_text(f"SELECT lead_id FROM ({_sql_lead_por_p8()}) m WHERE p8 = :p8"),
                    {"p8": p8},
                ).scalar()
            if achado:
                lead_ref = achado

    with SessionLocal() as s:
        row = s.execute(
            sql_text(
                """
                SELECT ls.lead_id     AS snap_id,
                       ls.nome        AS snap_nome,
                       ls.corretora_id AS snap_cid,
                       l.id           AS spine_id,
                       l.nome         AS spine_nome,
                       l.owner_id     AS spine_owner
                FROM (SELECT CAST(:lid AS TEXT) AS lead_id) k
                LEFT JOIN leads_snapshot ls ON ls.lead_id = k.lead_id
                LEFT JOIN leads l
                       ON l.tenant_id = :tenant
                      AND (
                            l.external_ref = k.lead_id
                            -- Lead SEM vínculo Kommo é referenciado pelo id da
                            -- própria espinha. `external_ref IS NULL` trava o
                            -- braço: sem isso, um id do Kommo (8 dígitos) que
                            -- por acaso valesse um id de espinha casaria o
                            -- lead errado.
                            -- O CAST não é decoração: sem ele o psycopg
                            -- levanta AmbiguousParameter quando o bind vem
                            -- None, e aí QUEBRA todo lead cujo ref não é id de
                            -- espinha (ref do Meta, 16 dígitos).
                         OR (CAST(:spine_id AS BIGINT) IS NOT NULL
                             AND l.external_ref IS NULL
                             AND l.id = CAST(:spine_id AS BIGINT))
                          )
                """
            ),
            {"lid": str(lead_ref), "tenant": DEFAULT_TENANT_ID,
             "spine_id": _ref_id_espinha(lead_ref)},
        ).mappings().first()

    if row is None or (row["snap_id"] is None and row["spine_id"] is None):
        return None, False, None

    dono = _normaliza_owner(row["spine_owner"])
    if dono is None:
        dono = row["snap_cid"]
    nome = row["snap_nome"] or row["spine_nome"]
    return (int(dono) if dono is not None else None), True, nome


def _checar_escopo_lead(
    lead_ref: str, my_cid: int | None, is_admin: bool
) -> tuple[int | None, str | None]:
    """Gate de escopo padrão: 404 se o lead não existe, 403 se não é dela.

    "Dela" inclui a delegada (`_pode_atender`): a Pessoa12 vê o card da Corretor e da
    Pessoa7 no kanban e precisa anotar/agendar nele — com igualdade estrita ela
    abria o card e levava 403 no meio do caminho.

    Lead SEM dona (owner NULL e snapshot sem corretora) é admin-only — não
    vira 404 (ele existe) nem fica liberado pra qualquer corretora.
    Devolve (dona_cid, lead_nome) quando libera.
    """
    dona_cid, existe, nome = _dono_canonico(lead_ref)
    if not existe:
        raise HTTPException(404, "lead_nao_encontrado")
    if not is_admin and (my_cid is None or not _pode_atender(my_cid, dona_cid)):
        raise HTTPException(403, "lead_nao_pertence_corretora")
    return dona_cid, nome


@router.get("/atendimento/me")
def painel_atendimento_me(
    corretora: int | None = Query(None),
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Lista leads em atendimento da corretora com sugestões prontas.

    Lê a análise MAIS RECENTE por lead em `lead_analyses`, filtra:
      • Corretora: só leads dela. Admin (dona/pessoa/assistente): todos.
      • `?corretora=<id>` (SÓ admin): restringe a visão a UMA corretora —
        usado pela Assistente ("o que a Pessoa12 tem hoje"). Corretora comum passando
        o param = ignorado (mantém o escopo dela, nunca amplia).
      • Lead deve estar em "em atendimento" no Kommo (status_id 10000005),
        OU ter `entrou_por='em_atendimento'` na análise (cobre snapshot stale).
    Ordena por temperatura (quente > morno > frio) → ultima_msg_at desc.
    """
    user = _identificar_usuario(x_painel_user)
    viewer = _viewer_info(user)
    my_cid = viewer["corretora_id"]
    is_admin = _is_admin(user)

    if my_cid is None and not is_admin:
        raise HTTPException(403, "painel_user_sem_corretora")

    # Filtro opcional do admin por corretora (id conhecido); corretora comum ignora.
    if is_admin and corretora is not None and corretora in CORRETORA_LABEL:
        my_cid = corretora

    stage_map = _stage_map()

    # Última análise por lead (corretora) — DISTINCT ON.
    # Filtro de corretora: pelo DONO CANÔNICO (espinha normalizada → snapshot),
    # não por `la.corretora_id` — lead_analyses é artefato de rodada do
    # Inspector e divergia do board/chat (lead sumia da lista da dona certa e
    # aparecia na da corretora antiga).
    # Filtro de "em atendimento":
    #   - leads_snapshot.etapa_kommo = '10000005'  OU
    #   - raw_analysis->>'entrou_por' = 'em_atendimento'
    dono_expr = _dono_canonico_sql()
    sql = f"""
        WITH ultima AS (
            SELECT DISTINCT ON (la.lead_id)
                la.id, la.lead_id, la.corretora_id, la.analyzed_at,
                la.status, la.temperatura, la.raw_analysis,
                dono.corretora_id AS dono_cid
            FROM lead_analyses la
            -- Dono canônico do lead. O LEFT JOIN parte de um "ls" sintético
            -- (COALESCE do snapshot) pra também resolver lead que só existe na
            -- espinha — sem isso o dono viria NULL e o filtro precisaria cair
            -- em `la.corretora_id`, que é justamente a fonte que estamos
            -- abandonando.
            LEFT JOIN LATERAL (
                SELECT corretora_id FROM leads_snapshot s2
                WHERE s2.lead_id = la.lead_id
            ) snap ON TRUE
            LEFT JOIN LATERAL (
                SELECT {dono_expr} AS corretora_id
                FROM (SELECT la.lead_id AS lead_id,
                             snap.corretora_id AS corretora_id) ls
                {_join_dono_sql()}
            ) dono ON TRUE
            WHERE (CAST(:cid AS INT) IS NULL
                   OR dono.corretora_id = CAST(:cid AS INT))
            ORDER BY la.lead_id, la.analyzed_at DESC
        ),
        convo AS (
            SELECT right(regexp_replace(contato_telefone, '\\D', '', 'g'), 8) AS p8,
                   max(timestamp_evolution) AS last_msg
            FROM mensagens_whatsapp
            WHERE contato_telefone IS NOT NULL
            GROUP BY 1
        )
        SELECT
            u.id            AS analysis_id,
            u.lead_id,
            u.corretora_id,
            ls.nome         AS lead_nome,
            ls.etapa_kommo  AS etapa_id,
            u.temperatura,
            u.status,
            u.raw_analysis->>'ultima_msg_at'      AS ultima_msg_at,
            u.raw_analysis->>'ultima_msg_de'      AS ultima_msg_de,
            u.raw_analysis->>'conversa_resumo'    AS resumo,
            u.raw_analysis->>'gap_identificado'   AS gap,
            u.raw_analysis->>'abordagem_sugerida' AS sugestao,
            u.raw_analysis->>'entrou_por'         AS entrou_por,
            u.raw_analysis->>'analysis_mode'      AS analysis_mode,
            oc.marcado_em   AS cuidado_em,
            -- Coaching curado do Inspector (categoria + trecho + sugestão pronta).
            -- Observação MAIS RECENTE do lead. lead_id é numérico em ambas, mas
            -- coaching_observations.lead_id é BIGINT e lead_analyses.lead_id TEXT:
            -- compara como texto pra nunca falhar cast.
            co.categoria        AS coaching_categoria,
            co.trecho_corretora AS coaching_trecho,
            co.observacao       AS coaching_observacao,
            co.sugestao         AS coaching_sugestao,
            ob.texto            AS obs_texto,
            ob.ia_motivo        AS obs_motivo,
            ob.ia_temperatura   AS obs_temperatura,
            ob.ia_descartar     AS obs_descartar,
            ob.criado_em        AS obs_em,
            cv.last_msg         AS ultima_conversa
        FROM ultima u
        LEFT JOIN leads_snapshot ls ON ls.lead_id = u.lead_id
        LEFT JOIN oportunidade_cuidada oc
               ON oc.corretora_id = u.corretora_id
              AND oc.lead_id = u.lead_id
        LEFT JOIN LATERAL (
            SELECT categoria, trecho_corretora, observacao, sugestao
            FROM coaching_observations cob
            WHERE cob.lead_id::text = u.lead_id
              AND cob.corretora_id = u.corretora_id
            ORDER BY cob.analyzed_at DESC
            LIMIT 1
        ) co ON TRUE
        LEFT JOIN LATERAL (
            SELECT texto, ia_motivo, ia_temperatura, ia_descartar, criado_em
            FROM observacao_corretora obc
            WHERE obc.lead_id = u.lead_id
              AND obc.corretora_id = u.corretora_id
            ORDER BY obc.criado_em DESC
            LIMIT 1
        ) ob ON TRUE
        LEFT JOIN convo cv
               ON cv.p8 = right(regexp_replace(COALESCE(ls.telefone, ''), '\\D', '', 'g'), 8)
        WHERE
            -- cond1: em atendimento no snapshot — só vale se snapshot está fresco
            -- (Inspector atualiza diariamente; sync > 5 dias = lead saiu do funil principal)
            (
                ls.etapa_kommo = :status_em_atend
                AND ls.ultima_sync >= NOW() - INTERVAL '5 days'
            )
            -- cond2: analysis recente com entrou_por=em_atendimento (fallback pra snapshot stale)
            -- Guard duplo: análise fresca OU snapshot fresco — se nenhum dos dois está fresco,
            -- o lead não está mais no funil ativo.
            OR (
                u.raw_analysis->>'entrou_por' = 'em_atendimento'
                AND (
                    u.analyzed_at >= NOW() - INTERVAL '5 days'
                    OR ls.ultima_sync >= NOW() - INTERVAL '5 days'
                )
            )
            -- Radar: lead quente recente em qualquer etapa (exceto ganho/perdido).
            -- analyzed_at recente garante que o snapshot também é fresco aqui.
            -- Absorve o que a aba Coaching (removida 2026-06) mostrava de único.
            OR (
                u.temperatura = 'quente'
                AND u.analyzed_at > NOW() - INTERVAL '7 days'
                AND COALESCE(ls.etapa_kommo, '') NOT IN ('142', '143')
            )
            -- Esfriando: lead ativo no funil que não conversa há +10 dias.
            -- Guard de snapshot fresco: se Inspector parou de processar, lead migrou de pipeline.
            -- (em atendimento já entra acima; aqui pega Follow-up, Recuperados etc.)
            OR (
                COALESCE(ls.etapa_kommo, '') IN (
                    '10000008', '10000009', '10000001', '10000010', '10000011',
                    '10000002', '10000012', '10000013', '10000014'
                )
                AND cv.last_msg < NOW() - INTERVAL '10 days'
                AND ls.ultima_sync >= NOW() - INTERVAL '5 days'
            )
    """
    with SessionLocal() as s:
        rows = s.execute(
            sql_text(sql),
            {"cid": my_cid, "status_em_atend": str(STATUS_EM_ATENDIMENTO)},
        ).mappings().all()

    leads_out: list[dict[str, Any]] = []
    for r in rows:
        etapa_id = (r["etapa_id"] or "").strip()
        # Reabre cuidado se o lead mandou mensagem nova depois do "cuidei".
        cuidado_em = r["cuidado_em"]
        if cuidado_em and r["ultima_msg_de"] == "lead" and r["ultima_msg_at"]:
            try:
                if _iso_to_epoch(r["ultima_msg_at"]) > cuidado_em.timestamp():
                    cuidado_em = None
            except Exception:
                pass
        # Observação da corretora reclassifica a temperatura (override).
        temperatura = r["obs_temperatura"] or r["temperatura"]
        # Dias desde a última conversa real no WhatsApp (mensagens_whatsapp).
        ultima_conversa = r["ultima_conversa"]
        dias_sem_conversa = (
            (datetime.now(timezone.utc) - ultima_conversa).days
            if ultima_conversa is not None else None
        )
        leads_out.append({
            "analysis_id":   r["analysis_id"],
            "lead_id":       r["lead_id"],
            "nome":          r["lead_nome"] or "?",
            "corretora_id":  r["corretora_id"],
            "corretora_label": CORRETORA_LABEL.get(int(r["corretora_id"])) if r["corretora_id"] else None,
            "etapa_id":      etapa_id or None,
            "etapa":         stage_map.get(etapa_id, etapa_id) if etapa_id else None,
            "temperatura":   temperatura,
            "status":        r["status"],
            "ultima_msg_at": r["ultima_msg_at"],
            "ultima_msg_de": r["ultima_msg_de"],
            "resumo":        r["resumo"],
            "gap":           r["gap"],
            "sugestao":      r["sugestao"],
            "entrou_por":    r["entrou_por"],
            "analysis_mode": r["analysis_mode"],
            "cuidado_em":    cuidado_em.isoformat() if cuidado_em else None,
            "dias_sem_conversa": dias_sem_conversa,
            "ultima_conversa":   ultima_conversa.isoformat() if ultima_conversa else None,
            "kommo_url":     _kommo_lead_url(r["lead_id"]),
            # Coaching curado (pull, contextual ao lead) — None quando o Inspector
            # não gerou observação. Nunca inventa: o card cai no gap cru.
            "coaching": ({
                "categoria":  r["coaching_categoria"],
                "trecho":     r["coaching_trecho"],
                "observacao": r["coaching_observacao"],
                "sugestao":   r["coaching_sugestao"],
            } if r["coaching_categoria"] else None),
            # Observação da corretora (última) — texto cru + entendimento da IA.
            "observacao": ({
                "texto":       r["obs_texto"],
                "motivo":      r["obs_motivo"],
                "temperatura": r["obs_temperatura"],
                "descartar":   r["obs_descartar"],
                "em":          r["obs_em"].isoformat() if r["obs_em"] else None,
            } if r["obs_texto"] else None),
        })

    # Ordena: temperatura (quente > morno > frio/None) → ultima_msg_at desc.
    leads_out.sort(
        key=lambda x: (
            _TEMPERATURA_ORDER.get(x["temperatura"], 3),
            -(int(_iso_to_epoch(x["ultima_msg_at"]))),
        )
    )

    return {
        "user": user,
        "viewer": viewer,
        "total": len(leads_out),
        "leads": leads_out,
        "fontes": _frescor_fontes(),
    }


@router.get("/atendimento/handoffs")
def painel_atendimento_handoffs(
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Leads passados pela Pessoa (agente) pra corretora humana assumir.

    Lista as notas de passagem (LeadHandoffNote) ativas — é o "avisar no CRM"
    do handoff. Corretora vê só as dela; admin (dona/pessoa/assistente) vê todas.
    """
    user = _identificar_usuario(x_painel_user)
    viewer = _viewer_info(user)
    my_cid = viewer["corretora_id"]
    is_admin = _is_admin(user)
    if my_cid is None and not is_admin:
        raise HTTPException(403, "painel_user_sem_corretora")

    from app.db import models as _models
    with SessionLocal() as s:
        q = s.query(_models.LeadHandoffNote).filter(
            _models.LeadHandoffNote.status == "ativo"
        )
        if not is_admin:
            q = q.filter(_models.LeadHandoffNote.corretora_id == my_cid)
        rows = q.order_by(_models.LeadHandoffNote.criado_em.desc()).limit(100).all()

    _CORR = {1: "Pessoa10", 2: "Pessoa11", 3: "Pessoa12", 4: "Corretor"}
    itens = [
        {
            "id": r.id,
            "lead_nome": r.lead_nome,
            "telefone": r.telefone,
            "corretora": _CORR.get(r.corretora_id) if r.corretora_id else None,
            "origem": r.origem,
            "contexto": r.contexto,
            "criado_em": r.criado_em.isoformat() if r.criado_em else None,
        }
        for r in rows
    ]
    return {"total": len(itens), "handoffs": itens}


@router.get("/atendimento/busca")
def painel_atendimento_busca(
    q: str = Query(..., min_length=2, max_length=60),
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Busca lead por telefone ou nome.

    - `q` com 4+ dígitos → match por sufixo do telefone (últimos 8 dígitos),
      robusto a 9º dígito, +55 e formatação.
    - Senão → match por nome (ILIKE).
    Corretora só enxerga leads dela (`leads_snapshot.corretora_id`); admin vê tudo.
    Retorna o mesmo shape de lead do /atendimento/me + telefone (lead pode não
    ter análise do Inspector — campos vêm null).
    """
    user = _identificar_usuario(x_painel_user)
    viewer = _viewer_info(user)
    my_cid = viewer["corretora_id"]
    is_admin = _is_admin(user)

    if my_cid is None and not is_admin:
        raise HTTPException(403, "painel_user_sem_corretora")

    digits = re.sub(r"\D", "", q)
    if len(digits) >= 4:
        modo = "telefone"
        match_sql = "regexp_replace(COALESCE(ls.telefone, ''), '\\D', '', 'g') LIKE :pat"
        pat = f"%{digits[-8:]}"
    else:
        modo = "nome"
        # Busca por TOKENS, não por trecho contíguo. "Alessandra Ducco" tem que
        # achar "Alessandra B. Ducco" — o nome no sistema quase sempre tem uma
        # peça a mais no meio (inicial, sobrenome, prefixo "BR - "), e o ILIKE
        # contíguo falhava justamente quando a pessoa lembra bem do nome.
        # Todos os pedaços precisam aparecer (AND), em qualquer ordem.
        tokens = [t for t in re.split(r"\s+", q.strip()) if t]
        match_sql = "ls.nome ILIKE :pat"
        pat = f"%{q.strip()}%"
        if len(tokens) > 1:
            match_sql = " AND ".join(
                f"ls.nome ILIKE :tok{i}" for i in range(len(tokens))
            )

    stage_map = _stage_map()

    # Escopo pelo dono CANÔNICO (mesma expressão do chat/gate) — antes o WHERE
    # usava `ls.corretora_id` puro enquanto o chat usava lead_analyses: fontes
    # divergentes = lead achado na busca e 403 ao abrir (e vice-versa).
    dono_expr = _dono_canonico_sql()
    sql = f"""
        WITH ultima AS (
            SELECT DISTINCT ON (la.lead_id)
                la.id, la.lead_id, la.corretora_id, la.analyzed_at,
                la.status, la.temperatura, la.raw_analysis
            FROM lead_analyses la
            ORDER BY la.lead_id, la.analyzed_at DESC
        )
        SELECT
            u.id            AS analysis_id,
            ls.lead_id,
            {dono_expr}     AS corretora_id,
            ls.nome         AS lead_nome,
            ls.telefone,
            ls.etapa_kommo  AS etapa_id,
            ls.etapa_canonica,
            u.temperatura,
            u.status,
            u.raw_analysis->>'ultima_msg_at'      AS ultima_msg_at,
            u.raw_analysis->>'ultima_msg_de'      AS ultima_msg_de,
            u.raw_analysis->>'conversa_resumo'    AS resumo,
            u.raw_analysis->>'gap_identificado'   AS gap,
            u.raw_analysis->>'abordagem_sugerida' AS sugestao,
            u.raw_analysis->>'entrou_por'         AS entrou_por,
            u.raw_analysis->>'analysis_mode'      AS analysis_mode,
            oc.marcado_em   AS cuidado_em
        FROM ({_fonte_busca_sql()}) ls
        {_join_dono_sql()}
        LEFT JOIN ultima u ON u.lead_id = ls.lead_id
        LEFT JOIN oportunidade_cuidada oc
               ON oc.lead_id = ls.lead_id
              AND oc.corretora_id = {dono_expr}
        WHERE (CAST(:cid AS INT) IS NULL OR {dono_expr} = CAST(:cid AS INT))
          AND {match_sql}
        ORDER BY ls.criado_em_kommo DESC NULLS LAST
        LIMIT 20
    """
    with SessionLocal() as s:
        binds: dict[str, Any] = {"cid": my_cid, "pat": pat}
        if modo == "nome":
            for i, tok in enumerate(re.split(r"\s+", q.strip())):
                if tok:
                    binds[f"tok{i}"] = f"%{tok}%"
        rows = s.execute(sql_text(sql), binds).mappings().all()

    leads_out: list[dict[str, Any]] = []
    for r in rows:
        etapa_id = (r["etapa_id"] or "").strip()
        leads_out.append({
            "analysis_id":   r["analysis_id"],
            "lead_id":       r["lead_id"],
            "nome":          r["lead_nome"] or "?",
            "telefone":      r["telefone"],
            "corretora_id":  r["corretora_id"],
            "corretora_label": CORRETORA_LABEL.get(int(r["corretora_id"])) if r["corretora_id"] else None,
            "etapa_id":      etapa_id or None,
            # etapa_id vem do snapshot (id do Kommo); quem só existe na
            # espinha já traz o nome resolvido por pipeline_stages.
            "etapa":         (stage_map.get(etapa_id, etapa_id) if etapa_id
                              else r["etapa_canonica"]),
            "temperatura":   r["temperatura"],
            "status":        r["status"],
            "ultima_msg_at": r["ultima_msg_at"],
            "ultima_msg_de": r["ultima_msg_de"],
            "resumo":        r["resumo"],
            "gap":           r["gap"],
            "sugestao":      r["sugestao"],
            "entrou_por":    r["entrou_por"],
            "analysis_mode": r["analysis_mode"],
            "cuidado_em":    r["cuidado_em"].isoformat() if r["cuidado_em"] else None,
            "kommo_url":     _kommo_lead_url(r["lead_id"]),
        })

    log.info("painel_busca", user=user, modo=modo, q_len=len(q), resultados=len(leads_out))
    return {"user": user, "viewer": viewer, "modo": modo, "total": len(leads_out), "leads": leads_out}


def _iso_to_epoch(iso: str | None) -> int:
    if not iso:
        return 0
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except (ValueError, TypeError):
        return 0


@router.post("/atendimento/{lead_id}/cuidei")
def painel_atendimento_cuidei(
    lead_id: str,
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Marca um lead como "já cuidei" (corretora ou admin).

    Idempotente: ON CONFLICT atualiza marcado_em pra agora.
    Admin (dona/pessoa/assistente) precisa que o lead tenha corretora_id no Kommo —
    pega a corretora_id da análise mais recente do lead.
    """
    user = _identificar_usuario(x_painel_user)
    my_cid = _corretora_id_do_user(user)
    is_admin = _is_admin(user)

    if my_cid is None and not is_admin:
        raise HTTPException(403, "painel_user_sem_corretora")

    with SessionLocal() as s:
        # Admin: resolve corretora pelo lead.
        target_cid = my_cid
        if target_cid is None:
            row = s.execute(
                sql_text(
                    "SELECT corretora_id FROM lead_analyses "
                    "WHERE lead_id = :lid AND corretora_id IS NOT NULL "
                    "ORDER BY analyzed_at DESC LIMIT 1"
                ),
                {"lid": lead_id},
            ).first()
            if not row:
                raise HTTPException(404, "lead_sem_analise")
            target_cid = row[0]
        else:
            # Corretora: garantir ownership.
            owned = s.execute(
                sql_text(
                    "SELECT 1 FROM lead_analyses "
                    "WHERE lead_id = :lid AND corretora_id = :cid LIMIT 1"
                ),
                {"lid": lead_id, "cid": my_cid},
            ).first()
            if not owned:
                raise HTTPException(403, "lead_nao_pertence_corretora")

        s.execute(
            sql_text(
                "INSERT INTO oportunidade_cuidada (corretora_id, lead_id) "
                "VALUES (:cid, :lid) "
                "ON CONFLICT (corretora_id, lead_id) "
                "DO UPDATE SET marcado_em = NOW()"
            ),
            {"cid": target_cid, "lid": lead_id},
        )
        s.commit()

    return {"status": "ok", "lead_id": lead_id, "corretora_id": target_cid}


def _entender_observacao(
    *, lead_nome: str, temp_atual: str | None,
    resumo: str | None, gap: str | None, observacao: str,
) -> dict[str, Any]:
    """Haiku lê a observação da corretora e devolve nota limpa + reclassificação.

    Falha de IA → degrada graciosamente: posta o texto cru, não reclassifica.
    """
    import json as _json
    schema = {
        "type": "object",
        "properties": {
            "nota_kommo": {"type": "string"},
            "temperatura": {"type": ["string", "null"]},
            "descartar": {"type": "boolean"},
            "motivo": {"type": "string"},
        },
        "required": ["nota_kommo", "descartar", "motivo"],
    }
    system = (
        "Você processa a observação que uma corretora escreveu sobre um lead "
        "imobiliário no CRM. Responda SEMPRE em JSON. Tarefas:\n"
        "1. nota_kommo: reescreva a observação como uma nota profissional, curta "
        "e clara pra timeline do CRM (1-3 frases em PT-BR). NÃO invente nada além "
        "do que a corretora disse.\n"
        "2. temperatura: se a observação indicar mudança de interesse, classifique "
        "'frio', 'morno' ou 'quente'; senão null. Ex.: 'nome sujo', 'desistiu', "
        "'só pesquisando' => frio; 'quer visitar', 'fechando' => quente.\n"
        "3. descartar: true SOMENTE se o lead ficou inviável/morto agora (nome sujo "
        "sem condição, número errado, desistiu de vez, fora do público). Senão false.\n"
        "4. motivo: resumo de 1 linha do que você entendeu, pra mostrar no card."
    )
    user = (
        f"Lead: {lead_nome}\n"
        f"Temperatura atual do sistema: {temp_atual or 'desconhecida'}\n"
        f"Resumo da conversa: {resumo or '—'}\n"
        f"Gap apontado: {gap or '—'}\n\n"
        f"Observação da corretora:\n{observacao}"
    )
    try:
        from app.config import get_classifier_provider
        raw, _usage = get_classifier_provider().complete(
            system=system, user=user, json_schema=schema,
        )
        data = _json.loads(raw)
    except Exception:
        log.exception("entender_observacao_falhou")
        return {
            "nota_kommo": observacao, "temperatura": None,
            "descartar": False, "motivo": observacao[:200],
        }
    temp = data.get("temperatura")
    if temp not in ("frio", "morno", "quente"):
        temp = None
    return {
        "nota_kommo": (data.get("nota_kommo") or observacao).strip()[:1500],
        "temperatura": temp,
        "descartar": bool(data.get("descartar")),
        "motivo": (data.get("motivo") or "").strip()[:280],
    }


@router.post("/atendimento/{lead_id}/observacao")
def painel_atendimento_observacao(
    lead_id: str,
    texto: str = Body(..., embed=True, min_length=2, max_length=2000),
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Corretora salva observação no lead. A IA lê, entende, posta nota no Kommo
    e pode reclassificar/descartar. Admin (dona/pessoa/assistente) age em qualquer lead.
    """
    import json as _json
    user = _identificar_usuario(x_painel_user)
    my_cid = _corretora_id_do_user(user)
    is_admin = _is_admin(user)
    if my_cid is None and not is_admin:
        raise HTTPException(403, "painel_user_sem_corretora")

    texto = (texto or "").strip()
    if len(texto) < 2:
        raise HTTPException(422, "observacao_vazia")

    with SessionLocal() as s:
        row = s.execute(
            sql_text(
                "SELECT la.corretora_id, la.temperatura, la.raw_analysis, ls.nome "
                "FROM lead_analyses la "
                "LEFT JOIN leads_snapshot ls ON ls.lead_id = la.lead_id "
                "WHERE la.lead_id = :lid "
                "ORDER BY la.analyzed_at DESC LIMIT 1"
            ),
            {"lid": lead_id},
        ).mappings().first()

    target_cid = my_cid
    if target_cid is None:
        if not row or row["corretora_id"] is None:
            raise HTTPException(404, "lead_sem_analise")
        target_cid = row["corretora_id"]
    elif row and row["corretora_id"] not in (None, my_cid):
        raise HTTPException(403, "lead_nao_pertence_corretora")

    ra = row["raw_analysis"] if row else None
    if isinstance(ra, str):
        try:
            ra = _json.loads(ra)
        except Exception:
            ra = {}
    ra = ra or {}
    lead_nome = (row["nome"] if row else None) or ra.get("lead_name") or "?"

    ia = _entender_observacao(
        lead_nome=lead_nome,
        temp_atual=row["temperatura"] if row else None,
        resumo=ra.get("conversa_resumo"),
        gap=ra.get("gap_identificado"),
        observacao=texto,
    )

    # Roteamento do destino externo por corretora dona do lead:
    #  - Pessoa3 (target_cid em CORRETORAS_TWENTY) → nota no Twenty (CRM novo)
    #  - Pessoa10/Pessoa11/None → nota no Kommo (comportamento atual, fallback seguro)
    # A auditoria local (observacao_corretora) e a reclassificação de temperatura
    # acontecem SEMPRE, independente do destino. note_ok = postou no destino externo.
    note_ok = False
    if target_cid in CORRETORAS_TWENTY:
        destino = "twenty"
        try:
            from app.integrations.twenty_client import TwentyClient
            note_ok = TwentyClient().add_note_to_lead(lead_id, ia["nota_kommo"])
        except Exception:
            log.exception("observacao_twenty_note_falhou", lead_id=lead_id)
    else:
        destino = "kommo"
        try:
            from app.tools.kommo import KommoTool
            KommoTool().add_lead_note(lead_id, f"📝 Observação (Copiloto): {ia['nota_kommo']}")
            note_ok = True
        except Exception:
            log.exception("observacao_kommo_note_falhou", lead_id=lead_id)

    with SessionLocal() as s:
        s.execute(
            sql_text(
                "INSERT INTO observacao_corretora "
                "(lead_id, corretora_id, autor, texto, ia_nota_kommo, "
                " ia_temperatura, ia_descartar, ia_motivo, kommo_note_ok) "
                "VALUES (:lid,:cid,:autor,:texto,:nota,:temp,:desc,:motivo,:ok)"
            ),
            {
                "lid": lead_id, "cid": target_cid, "autor": user, "texto": texto,
                "nota": ia["nota_kommo"], "temp": ia["temperatura"],
                "desc": ia["descartar"], "motivo": ia["motivo"], "ok": note_ok,
            },
        )
        if ia["descartar"]:
            s.execute(
                sql_text(
                    "INSERT INTO oportunidade_cuidada (corretora_id, lead_id) "
                    "VALUES (:cid,:lid) "
                    "ON CONFLICT (corretora_id, lead_id) "
                    "DO UPDATE SET marcado_em = NOW()"
                ),
                {"cid": target_cid, "lid": lead_id},
            )
        s.commit()

    log.info(
        "observacao_salva", lead_id=lead_id, corretora_id=target_cid,
        destino=destino, note_ok=note_ok, temperatura=ia["temperatura"],
        descartar=ia["descartar"],
    )
    return {
        "status": "ok", "lead_id": lead_id, "corretora_id": target_cid,
        "destino": destino, "note_ok": note_ok,
        "kommo_note_ok": note_ok, "ia": ia,
    }


@router.delete("/atendimento/{lead_id}/cuidei")
def painel_atendimento_descuidei(
    lead_id: str,
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Reabre um lead: remove a marca "já cuidei" e devolve ele pra fila ativa.

    Idempotente: se não havia marca, retorna ok do mesmo jeito.
    Corretora só desmarca o que é dela; admin (dona/pessoa/assistente) desmarca qualquer.
    """
    user = _identificar_usuario(x_painel_user)
    my_cid = _corretora_id_do_user(user)
    is_admin = _is_admin(user)

    if my_cid is None and not is_admin:
        raise HTTPException(403, "painel_user_sem_corretora")

    with SessionLocal() as s:
        if my_cid is not None:
            s.execute(
                sql_text(
                    "DELETE FROM oportunidade_cuidada "
                    "WHERE corretora_id = :cid AND lead_id = :lid"
                ),
                {"cid": my_cid, "lid": lead_id},
            )
        else:
            s.execute(
                sql_text("DELETE FROM oportunidade_cuidada WHERE lead_id = :lid"),
                {"lid": lead_id},
            )
        s.commit()

    return {"status": "ok", "lead_id": lead_id}


# ── Tarefas da corretora ────────────────────────────────────────────────────────


@router.post("/lead/{lead_id}/tarefa")
def painel_criar_tarefa(
    lead_id: str,
    data: str = Body(..., embed=True),
    texto: str = Body(..., embed=True),
    lead_nome: str | None = Body(default=None, embed=True),
    hora: str | None = Body(default=None, embed=True),
    anotacoes: str | None = Body(default=None, embed=True),
    mensagem_planejada: str | None = Body(default=None, embed=True),
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Cria tarefa num lead, agendada pra um dia. Aparece no Hoje da corretora
    dona quando data_tarefa <= hoje.

    `anotacoes` (campo próprio da tarefa) e `mensagem_planejada` (corpo que
    dispara sozinho em UTILITY na data/hora, pela linha da Pessoa12) são opcionais.
    Vazio → None (tarefa vira só lembrete).

    corretora_id da tarefa:
     - se o user é corretora → a própria (ela só cria no que é dela).
     - se admin → a corretora dona do lead (via lead_analyses); pode ser None se
       o lead ainda não tem análise.
    """
    user = _identificar_usuario(x_painel_user)
    my_cid = _corretora_id_do_user(user)
    is_admin = _is_admin(user)
    if my_cid is None and not is_admin:
        raise HTTPException(403, "painel_user_sem_corretora")

    texto = (texto or "").strip()
    if not texto:
        raise HTTPException(422, "tarefa_texto_vazio")

    try:
        data_tarefa = datetime.strptime(data.strip(), "%Y-%m-%d").date()
    except (ValueError, AttributeError):
        raise HTTPException(422, "tarefa_data_invalida")

    # hora opcional: "HH:MM" (aceita "HH:MM:SS" também). vazio → None.
    hora_tarefa = None
    hora_str = (hora or "").strip()
    if hora_str:
        try:
            fmt = "%H:%M:%S" if hora_str.count(":") == 2 else "%H:%M"
            hora_tarefa = datetime.strptime(hora_str, fmt).time()
        except (ValueError, AttributeError):
            raise HTTPException(422, "tarefa_hora_invalida")

    nome = (lead_nome or "").strip() or None
    anot = (anotacoes or "").strip() or None
    msg_plan = (mensagem_planejada or "").strip() or None

    # 🔴 Achado 30/07 (lead Bianca Belline, 25067496): este gate consultava
    # `lead_analyses` — uma TERCEIRA fonte de dono, além da espinha e do
    # snapshot. Ela é histórico de análise e NÃO acompanha transferência: a
    # Bianca passou pra Pessoa10, mas a última análise (13/07) ainda dizia Pessoa11.
    # Resultado: 403 ao criar tarefa num lead que é dela, no painel que mostra
    # o card como dela. Ela conseguia CONCLUIR tarefa (esse caminho já usava o
    # dono canônico) e só a criação falhava — 9 leads estavam assim.
    #
    # Agora usa `_checar_escopo_lead`, o mesmo gate do resto do painel: 404 se
    # não existe, 403 se não é dela, dono canônico (espinha → snapshot).
    dona_cid, nome_canonico = _checar_escopo_lead(lead_id, my_cid, is_admin)

    with SessionLocal() as s:
        # corretora cria pra si; admin cria pra dona do lead (None se sem dona).
        target_cid = my_cid if my_cid is not None else dona_cid

        if nome is None and nome_canonico:
            nome = nome_canonico

        new_id = s.execute(
            sql_text(
                "INSERT INTO tarefas_corretora "
                "(lead_id, lead_nome, corretora_id, data_tarefa, hora_tarefa, texto, "
                " status, autor, anotacoes, mensagem_planejada) "
                "VALUES (:lid,:nome,:cid,:data,:hora,:texto,'aberta',:autor,"
                "        :anot,:msg_plan) "
                "RETURNING id"
            ),
            {
                "lid": lead_id, "nome": nome, "cid": target_cid,
                "data": data_tarefa, "hora": hora_tarefa,
                "texto": texto, "autor": user,
                "anot": anot, "msg_plan": msg_plan,
            },
        ).scalar_one()
        s.commit()

    log.info(
        "tarefa_criada", tarefa_id=new_id, lead_id=lead_id,
        corretora_id=target_cid, data=str(data_tarefa),
        hora=(hora_tarefa.strftime("%H:%M") if hora_tarefa else None), autor=user,
        tem_anotacoes=bool(anot), tem_msg_planejada=bool(msg_plan),
    )
    return {"ok": True, "tarefa_id": int(new_id)}


# ── Resultado de LIGAÇÃO — os três estados ───────────────────────────────────
# Concluir ligação é ação de ROTINA (dezenas de vezes por dia). Por isso são
# três botões de UM clique cada, não um formulário de duas perguntas:
#   nao_atendeu · atendeu_sem_continuar · atendeu_quer_continuar
# `observacao` é opcional e nunca bloqueia. Ausência de resultado NÃO vira
# "não atendeu" — vira "sem registro" na métrica.
def _resultado_do_body(bruto: Any) -> Any:
    """dict/str do body → ResultadoLigacao, ou None. 422 se vier estado inválido
    (aceitar silenciosamente inventaria número)."""
    from app.services.ligacao_resultado import ResultadoLigacao

    if bruto is None:
        return None
    if isinstance(bruto, str):
        bruto = {"resultado": bruto}
    if not isinstance(bruto, dict):
        raise HTTPException(422, "resultado_invalido")
    estado = (bruto.get("resultado") or bruto.get("estado") or "").strip()
    if not estado:
        return None
    try:
        return ResultadoLigacao.do_estado(estado, bruto.get("observacao"))
    except ValueError:
        raise HTTPException(422, "resultado_invalido")


@router.post("/tarefa/{tarefa_id}/concluir")
def painel_concluir_tarefa(
    tarefa_id: int,
    body: dict[str, Any] | None = Body(default=None),
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Marca a tarefa como concluída. Só a corretora dona ou admin. Idempotente.

    Body OPCIONAL (tarefa de LIGAÇÃO): {"resultado": "nao_atendeu" |
    "atendeu_sem_continuar" | "atendeu_quer_continuar", "observacao"?: str}.
    Com resultado, grava a nota na conversa do lead + o dado estruturado.
    Sem body, conclui como sempre (e a tarefa fica "sem registro" na métrica —
    nunca "não atendeu").
    """
    from app.services.ligacao_resultado import ORIGEM_TAREFA, registrar_resultado

    user = _identificar_usuario(x_painel_user)
    my_cid = _corretora_id_do_user(user)
    is_admin = _is_admin(user)
    if my_cid is None and not is_admin:
        raise HTTPException(403, "painel_user_sem_corretora")

    resultado = _resultado_do_body(body)

    with SessionLocal() as s:
        row = s.execute(
            sql_text(
                "SELECT corretora_id, lead_id FROM tarefas_corretora WHERE id = :tid"
            ),
            {"tid": tarefa_id},
        ).mappings().first()
        if not row:
            raise HTTPException(404, "tarefa_nao_encontrada")
        if not is_admin and row["corretora_id"] != my_cid:
            raise HTTPException(403, "tarefa_nao_pertence_corretora")

        s.execute(
            sql_text(
                "UPDATE tarefas_corretora "
                "SET status = 'concluida', concluido_em = CURRENT_TIMESTAMP, "
                "    concluido_por = :who "
                "WHERE id = :tid AND status = 'aberta'"
            ),
            {"tid": tarefa_id, "who": user},
        )

        registro: dict[str, Any] | None = None
        if resultado is not None:
            registro = registrar_resultado(
                s,
                origem=ORIGEM_TAREFA,
                origem_id=tarefa_id,
                resultado=resultado,
                lead_id=row["lead_id"],
                corretora_id=row["corretora_id"] if row["corretora_id"] else my_cid,
                autor=user,
            )
        s.commit()

    log.info(
        "tarefa_concluida", tarefa_id=tarefa_id, autor=user,
        resultado=(resultado.estado if resultado else None),
    )
    return {"ok": True, "registro_ligacao": registro}


@router.post("/tarefa/{tarefa_id}/editar")
def painel_editar_tarefa(
    tarefa_id: int,
    texto: str = Body(..., embed=True),
    data: str = Body(..., embed=True),
    hora: str | None = Body(default=None, embed=True),
    anotacoes: str | None = Body(default=None, embed=True),
    mensagem_planejada: str | None = Body(default=None, embed=True),
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Edita texto/data/hora/anotações/mensagem-planejada de uma tarefa ABERTA.
    Só a corretora dona ou admin. Tarefa concluída não pode ser editada (422).

    `anotacoes` e `mensagem_planejada` são sempre gravados com o valor recebido
    (vazio → NULL: desmarcar a msg planejada no UI limpa o campo). Se o CORPO da
    mensagem planejada MUDA, reseta `msg_planejada_enviada_em` (re-agenda o
    disparo). Se não muda, preserva o enviada_em (não reenvia por editar data)."""
    user = _identificar_usuario(x_painel_user)
    my_cid = _corretora_id_do_user(user)
    is_admin = _is_admin(user)
    if my_cid is None and not is_admin:
        raise HTTPException(403, "painel_user_sem_corretora")

    texto = (texto or "").strip()
    if len(texto) < 2:
        raise HTTPException(422, "tarefa_texto_vazio")

    try:
        data_tarefa = datetime.strptime(data.strip(), "%Y-%m-%d").date()
    except (ValueError, AttributeError):
        raise HTTPException(422, "tarefa_data_invalida")

    # hora opcional: "HH:MM" (aceita "HH:MM:SS" também). vazio → None.
    hora_tarefa = None
    hora_str = (hora or "").strip()
    if hora_str:
        try:
            fmt = "%H:%M:%S" if hora_str.count(":") == 2 else "%H:%M"
            hora_tarefa = datetime.strptime(hora_str, fmt).time()
        except (ValueError, AttributeError):
            raise HTTPException(422, "tarefa_hora_invalida")

    anot = (anotacoes or "").strip() or None
    msg_plan = (mensagem_planejada or "").strip() or None

    with SessionLocal() as s:
        row = s.execute(
            sql_text(
                "SELECT corretora_id, status, mensagem_planejada "
                "FROM tarefas_corretora WHERE id = :tid"
            ),
            {"tid": tarefa_id},
        ).mappings().first()
        if not row:
            raise HTTPException(404, "tarefa_nao_encontrada")
        if not is_admin and row["corretora_id"] != my_cid:
            raise HTTPException(403, "tarefa_nao_pertence_corretora")
        if row["status"] != "aberta":
            raise HTTPException(422, "tarefa_nao_editavel_concluida")

        # Corpo da msg planejada mudou? → re-agenda (reset enviada_em). Senão,
        # preserva o enviada_em (editar data/texto não reenvia o que já saiu).
        msg_mudou = (row["mensagem_planejada"] or None) != msg_plan

        updated = s.execute(
            sql_text(
                "UPDATE tarefas_corretora "
                "SET texto = :texto, data_tarefa = :data, hora_tarefa = :hora, "
                "    anotacoes = :anot, mensagem_planejada = :msg_plan"
                + (", msg_planejada_enviada_em = NULL" if msg_mudou else "")
                + " WHERE id = :tid AND status = 'aberta' "
                "RETURNING id, lead_id, lead_nome, corretora_id, data_tarefa, "
                "          hora_tarefa, texto, status, anotacoes, "
                "          mensagem_planejada, msg_planejada_enviada_em"
            ),
            {
                "tid": tarefa_id, "texto": texto,
                "data": data_tarefa, "hora": hora_tarefa,
                "anot": anot, "msg_plan": msg_plan,
            },
        ).mappings().first()
        s.commit()

    if not updated:
        # corrida: mudou de status entre o SELECT e o UPDATE
        raise HTTPException(422, "tarefa_nao_editavel_concluida")

    log.info(
        "tarefa_editada", tarefa_id=tarefa_id,
        data=str(data_tarefa),
        hora=(hora_tarefa.strftime("%H:%M") if hora_tarefa else None), autor=user,
    )
    return {
        "ok": True,
        "tarefa": {
            "id": int(updated["id"]),
            "lead_id": updated["lead_id"],
            "lead_nome": updated["lead_nome"],
            "corretora": (
                CORRETORA_LABEL.get(updated["corretora_id"])
                if updated["corretora_id"] else None
            ),
            "data": updated["data_tarefa"].isoformat(),
            "hora": (
                updated["hora_tarefa"].strftime("%H:%M")
                if updated["hora_tarefa"] else None
            ),
            "texto": updated["texto"],
            "status": updated["status"],
            "anotacoes": updated["anotacoes"],
            "mensagem_planejada": updated["mensagem_planejada"],
            "tem_msg_planejada": bool(updated["mensagem_planejada"]),
            "msg_planejada_enviada_em": (
                updated["msg_planejada_enviada_em"].isoformat()
                if updated["msg_planejada_enviada_em"] else None
            ),
        },
    }


# ── Tarefas de LIGAÇÃO da cadência (sistema NOVO) ────────────────────────────
# A cadência híbrida (app/services/cadencia.py) tem 3 toques de LIGAÇÃO: D1, D5, D9
# (índices 1, 4, 6 em TOQUES). Uma tarefa de ligação DEVIDA = um cadencia_estado
# 'ativa' cujo próximo toque a executar (TOQUES[toque_atual]) é do tipo
# tarefa_ligacao E proximo_toque_em <= agora. Diferente das tarefas MANUAIS
# (tarefas_corretora), estas são GERADAS pelo motor — a corretora não edita, só
# executa (liga) e marca "liguei" (POST /painel/cadencia/ligacao-feita).
#
# Antes esta aba (/painel/tarefas) só lia tarefas_corretora — por isso as tarefas
# de ligação da cadência NÃO apareciam, gerando a confusão da Pessoa12 (via alerta
# "Ligação · dia N do FUP" no Chat mas nada na aba). Agora unificamos.

# offset_dias por índice de toque de ligação — DERIVADO de TOQUES (cadencia.py),
# nunca copiado. A cópia hardcode ({1,4,6}) sobreviveu à régua V2 de 04/08/2026,
# que inseriu o D3 e empurrou as ligações pra 1/5/7: a fila passou a listar toque
# de MENSAGEM como "🔔 Ligar" (clique morria em `toque_atual_nao_e_ligacao` — o
# "não consegui registrar" que a Pessoa11 viu) e as ligações reais sumiram da aba.
def _labels_ligacao() -> dict[int, tuple[str, int]]:
    from app.services.cadencia import TIPO_TAREFA_LIGACAO, TOQUES

    return {
        t.indice: (f"D{t.offset_dias}", t.offset_dias)
        for t in TOQUES
        if t.tipo == TIPO_TAREFA_LIGACAO
    }


_TOQUE_LIGACAO_LABEL = _labels_ligacao()
_INDICES_LIGACAO = tuple(_TOQUE_LIGACAO_LABEL.keys())


def _escopo_tarefas(cid: int | None, autor: str) -> tuple[str, dict[str, Any]]:
    """Fragmento de WHERE que limita `tarefas_corretora` a uma pessoa.

    Além das tarefas DELA, traz as ÓRFÃS que ELA criou — relato da Dona
    (12/08/2026): "tarefa da Pessoa12 que só aparecia quando via as de todas".

    A tarefa herda a dona do LEAD. Lead sem dona (base antiga, remarketing)
    gera tarefa com `corretora_id` NULL, e o filtro por corretora escondia de
    todo mundo: a pessoa criava o lembrete e ele sumia da lista. Eram 10 assim,
    todas da conta `pessoa` (a Pessoa12) — as últimas do dia anterior ao relato.

    Não inventa dona pro lead (isso é decisão de operação, não da listagem) e
    não vira curinga: órfã de OUTRA pessoa continua fora.

    Vive numa função porque são TRÊS consumidores — a lista de abertas, o
    histórico de concluídas e o contador do Meu dia. Repetir a regra é como
    nasceu o bug dos índices de ligação: uma cópia envelheceu sozinha.
    """
    if cid is None:
        return "", {}
    return (
        " AND (corretora_id = :escopo_cid"
        " OR (corretora_id IS NULL AND autor = :escopo_autor))",
        {"escopo_cid": cid, "escopo_autor": autor},
    )


def _tarefas_ligacao_cadencia(
    cid: int | None,
    *,
    is_admin: bool,
    lead_id: str | None = None,
) -> list[dict[str, Any]]:
    """Tarefas de LIGAÇÃO da cadência DEVIDAS (proximo_toque_em <= agora) cujo
    toque atual é de ligação. Query agregada (join único com leads_snapshot pra
    resolver nome + lead_id do Kommo). Escopo por corretora; admin vê todas.

    Se `lead_id` for dado, filtra só aquele lead (uso no raio-X do Chat). Filtra
    por lead_id — não por telefone — porque a cadência guarda o fone sem "+" e o
    snapshot com "+" (formatos divergem); lead_id é a chave estável.

    Retorna items no MESMO shape das tarefas manuais + `tipo="ligacao"`,
    `cadencia_id`, `toque` — pra o front diferenciar e plugar o "✓ liguei".
    """
    agora = datetime.now(timezone.utc)
    where = [
        "ce.status = 'ativa'",
        "ce.proximo_toque_em <= :agora",
        "ce.toque_atual = ANY(:indices)",
    ]
    params: dict[str, Any] = {
        "agora": agora,
        "indices": list(_INDICES_LIGACAO),
    }
    if not is_admin:
        where.append("ce.corretora_id = :cid")
        params["cid"] = cid
    if lead_id:
        where.append("ce.lead_id = :lid")
        params["lid"] = str(lead_id)

    sql = (
        "SELECT ce.id AS cadencia_id, ce.lead_id, ce.telefone, ce.corretora_id, "
        "       ce.toque_atual, ce.proximo_toque_em, ls.nome AS lead_nome "
        "FROM cadencia_estado ce "
        "LEFT JOIN leads_snapshot ls ON ls.lead_id = ce.lead_id "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY ce.proximo_toque_em ASC, ce.id ASC"
    )
    with SessionLocal() as s:
        rows = s.execute(sql_text(sql), params).mappings().all()

    hoje = _hoje_sp()
    out: list[dict[str, Any]] = []
    for r in rows:
        rot, dia = _TOQUE_LIGACAO_LABEL.get(int(r["toque_atual"]), ("?", 0))
        devido_dt = r["proximo_toque_em"]
        # data devida em SP (a fila é por dia SP, igual às manuais)
        data_sp = devido_dt.astimezone(SP_TZ).date() if devido_dt else hoje
        out.append({
            # id negativo estável (evita colisão com id de tarefas_corretora no front)
            "id": -int(r["cadencia_id"]),
            "cadencia_id": int(r["cadencia_id"]),
            "toque": int(r["toque_atual"]),
            "lead_id": r["lead_id"],
            "lead_nome": r["lead_nome"],
            "corretora": CORRETORA_LABEL.get(r["corretora_id"]) if r["corretora_id"] else None,
            "data": data_sp.isoformat(),
            "hora": None,
            "texto": f"🔔 Ligar — {rot} do follow-up (dia {dia})",
            "status": "aberta",
            "atrasada": data_sp < hoje,
            "anotacoes": None,
            "mensagem_planejada": None,
            "tem_msg_planejada": False,
            "msg_planejada_enviada_em": None,
            "tipo": "ligacao",   # ← diferencia da manual ("tarefa")
        })
    return out


@router.get("/tarefas")
def painel_listar_tarefas(
    escopo: str = Query("hoje"),
    corretora: str | None = Query(None, description="admin: filtra por corretora (nome, rótulo ou id)"),
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Lista tarefas ABERTAS — MANUAIS (tarefas_corretora) + de LIGAÇÃO da cadência.

    - escopo=hoje (default): data_tarefa <= hoje (vencendo hoje + atrasadas).
    - escopo=todas: qualquer data (inclui futuras).
    Corretora vê só as dela; admin vê todas. `atrasada` = data_tarefa < hoje.
    Cada item tem `tipo`: "tarefa" (manual, editável) | "ligacao" (cadência, gerada).
    """
    user = _identificar_usuario(x_painel_user)
    my_cid = _corretora_id_do_user(user)
    is_admin = _is_admin(user)
    if my_cid is None and not is_admin:
        raise HTTPException(403, "painel_user_sem_corretora")

    hoje = _hoje_sp()
    params: dict[str, Any] = {"hoje": hoje}
    where = "status = 'aberta'"
    if escopo != "todas":
        where += " AND data_tarefa <= :hoje"
    # Escopo: corretora vê só as dela (ignora o ?corretora=, não é dela a escolha).
    # Admin pode filtrar por uma corretora — mesmo padrão das pills do Chat.
    filtro_cid = _corretora_id_do_filtro(corretora) if is_admin else None
    cid_efetivo = my_cid if not is_admin else filtro_cid
    frag, p_escopo = _escopo_tarefas(cid_efetivo, user)
    where += frag
    params.update(p_escopo)

    with SessionLocal() as s:
        rows = s.execute(
            sql_text(
                f"SELECT id, lead_id, lead_nome, corretora_id, data_tarefa, "
                f"       hora_tarefa, texto, status, anotacoes, mensagem_planejada, "
                f"       msg_planejada_enviada_em "
                f"FROM tarefas_corretora "
                f"WHERE {where} "
                f"ORDER BY data_tarefa ASC, hora_tarefa ASC NULLS LAST, id ASC"
            ),
            params,
        ).mappings().all()

    tarefas = [
        {
            "id": int(r["id"]),
            "lead_id": r["lead_id"],
            "lead_nome": r["lead_nome"],
            "corretora": CORRETORA_LABEL.get(r["corretora_id"]) if r["corretora_id"] else None,
            "data": r["data_tarefa"].isoformat(),
            "hora": r["hora_tarefa"].strftime("%H:%M") if r["hora_tarefa"] else None,
            "texto": r["texto"],
            "status": r["status"],
            "atrasada": r["data_tarefa"] < hoje,
            "anotacoes": r["anotacoes"],
            "mensagem_planejada": r["mensagem_planejada"],
            "tem_msg_planejada": bool(r["mensagem_planejada"]),
            "msg_planejada_enviada_em": (
                r["msg_planejada_enviada_em"].isoformat()
                if r["msg_planejada_enviada_em"] else None
            ),
            "tipo": "tarefa",  # manual — editável/concluível
        }
        for r in rows
    ]

    # ── Unifica: junta as tarefas de LIGAÇÃO da cadência (sistema novo) ──
    # As de ligação são sempre DEVIDAS (proximo_toque_em <= agora), então entram
    # tanto em escopo=hoje quanto escopo=todas. O agrupamento (atrasadas/hoje/
    # próximas) é feito no front por `data`/`atrasada`, igual às manuais.
    ligacoes = _tarefas_ligacao_cadencia(
        cid_efetivo if cid_efetivo is not None else my_cid,
        is_admin=is_admin and cid_efetivo is None,
    )
    tarefas.extend(ligacoes)

    return {"tarefas": tarefas}


@router.get("/tarefas/concluidas")
def painel_listar_tarefas_concluidas(
    page: int = Query(1, ge=1),
    por_pagina: int = Query(20, ge=1, le=100),
    corretora: str | None = Query(None, description="admin: filtra por corretora (nome, rótulo ou id)"),
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Histórico de tarefas CONCLUÍDAS (aba "Concluídas" da tela ☑️ Tarefas V2).

    Paginado, mais recente primeiro (concluido_em DESC). Mostra quem concluiu
    (`concluido_por`, migration 0062 — linhas antigas vêm NULL) e quando.
    Corretora vê só as dela; admin vê todas. Só as MANUAIS (tarefas_corretora);
    ligações da cadência têm histórico próprio no motor.
    """
    user = _identificar_usuario(x_painel_user)
    my_cid = _corretora_id_do_user(user)
    is_admin = _is_admin(user)
    if my_cid is None and not is_admin:
        raise HTTPException(403, "painel_user_sem_corretora")

    where = "status = 'concluida'"
    params: dict[str, Any] = {}
    # Filtro precisa ser no SQL: a paginação é do servidor, filtrar no front
    # só peneiraria a página atual e o total viria errado.
    filtro_cid = _corretora_id_do_filtro(corretora) if is_admin else None
    cid_efetivo = my_cid if not is_admin else filtro_cid
    frag, p_escopo = _escopo_tarefas(cid_efetivo, user)
    where += frag
    params.update(p_escopo)

    offset = (page - 1) * por_pagina
    with SessionLocal() as s:
        total = int(s.execute(
            sql_text(f"SELECT COUNT(*) FROM tarefas_corretora WHERE {where}"),
            params,
        ).scalar() or 0)
        rows = s.execute(
            sql_text(
                f"SELECT id, lead_id, lead_nome, corretora_id, data_tarefa, "
                f"       texto, autor, concluido_em, concluido_por "
                f"FROM tarefas_corretora "
                f"WHERE {where} "
                f"ORDER BY concluido_em DESC NULLS LAST, id DESC "
                f"LIMIT :lim OFFSET :off"
            ),
            {**params, "lim": por_pagina, "off": offset},
        ).mappings().all()

    def _iso(v: Any) -> str | None:
        # Postgres devolve date/datetime; SQLite (testes) devolve str.
        if v is None:
            return None
        return v if isinstance(v, str) else v.isoformat()

    tarefas = [
        {
            "id": int(r["id"]),
            "lead_id": r["lead_id"],
            "lead_nome": r["lead_nome"],
            "corretora": CORRETORA_LABEL.get(r["corretora_id"]) if r["corretora_id"] else None,
            "data": _iso(r["data_tarefa"]),
            "texto": r["texto"],
            "autor": r["autor"],
            "concluido_em": _iso(r["concluido_em"]),
            "concluido_por": r["concluido_por"],
        }
        for r in rows
    ]
    return {
        "tarefas": tarefas,
        "total": total,
        "page": page,
        "por_pagina": por_pagina,
        "tem_mais": offset + len(tarefas) < total,
    }


@router.get("/tarefas/metricas-ligacao")
def painel_metricas_ligacao(
    dias: int = Query(30, ge=1, le=365),
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Ligações feitas · atenderam · quiseram continuar, por corretora e total.
    Admin-only (mesmo padrão de `/painel/automacoes/config`).

    Os três números que a Dona media na mão. Travas: taxa None quando não há
    amostra (nunca 0%), amostra abaixo de AMOSTRA_MINIMA vem marcada, e ligação
    concluída sem resultado conta em `sem_registro` — jamais em "não atendeu".
    """
    from app.api.automacoes import AMOSTRA_MINIMA
    from app.services.ligacao_resultado import metricas_ligacao

    user = _identificar_usuario(x_painel_user)
    _require_admin(user)

    with SessionLocal() as s:
        dados = metricas_ligacao(
            s, dias=dias, amostra_minima=AMOSTRA_MINIMA,
            corretora_label=CORRETORA_LABEL,
        )
    return {"gerado_em": datetime.now(timezone.utc).isoformat(), **dados}


# ── Janela de ligação ────────────────────────────────────────────────────────
# A fila de quem ligar vem do REMARKETING e só de lead SEM DONO: a ligação é o
# momento em que a corretora adota o lead (regra Dona). Lead com dono já está
# sendo atendido por alguém — ligar de novo é atropelo.
#
# Fica como ABA da tela /tarefas (não tela nova) porque é o mesmo gesto mental
# da corretora ali ("o que eu faço agora"), reusa as pills de corretora e o
# toast que já existem, e uma entrada nova de menu pra uma fila que se esvazia
# viraria item morto no dia em que a base acabar.


@router.get("/ligacoes/fila")
def painel_ligacoes_fila(
    limite: int = Query(50, ge=1, le=200),
    ver_tudo: bool = Query(False),
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Fila de quem ligar (remarketing sem dono) + meta do dia + referência.

    RESERVA: os leads devolvidos ficam PEGOS por quem pediu, por
    `RESERVA_MINUTOS`, e somem da fila das outras corretoras nesse intervalo —
    é o que impede a Pessoa10 e a Pessoa11 ligarem pra mesma pessoa abrindo a aba na
    mesma manhã. A reserva expira sozinha (fechar a aba não tranca lead) e é
    renovada a cada recarga de quem já é dono dela.

    PRIVACIDADE: corretora só recebe o que está reservado PRA ELA — é o mesmo
    escopo por corretora da aba de tarefas. `ver_tudo=1` é ADMIN-ONLY (visão de
    supervisão): mostra o que está com cada uma, com `reservado_por`, e não
    reserva nada.

    TAMANHO DO BLOCO: a fila entrega no máximo o que falta pra fechar a meta do
    dia (`config_operacional.ligacao_meta_diaria`, 25). Lista infinita não vira
    rotina — vira dívida (hoje são 114 tarefas em aberto de uma corretora só).
    """
    from app.services.ligacao_resultado import (
        REFERENCIA_HISTORICA,
        RESERVA_MINUTOS,
        fila_ligacao,
        progresso_ligacao,
        total_fila_ligacao,
    )

    user = _identificar_usuario(x_painel_user)
    my_cid = _corretora_id_do_user(user)
    is_admin = _is_admin(user)
    if my_cid is None and not is_admin:
        raise HTTPException(403, "painel_user_sem_corretora")
    if ver_tudo and not is_admin:
        raise HTTPException(403, "painel_admin_required")

    with SessionLocal() as s:
        meta = progresso_ligacao(s, corretora_id=my_cid)
        # o bloco do dia manda no tamanho da página (admin sem corretora vê a
        # fila inteira: ele supervisiona, não cumpre meta)
        cabe = limite if meta is None else min(limite, int(meta["restante_hoje"]))
        itens = (
            fila_ligacao(
                s,
                limite=cabe,
                dono=None if ver_tudo else user,
                corretora_id=my_cid,
                ver_reservados=ver_tudo,
            )
            if cabe > 0
            else []
        )
        total = total_fila_ligacao(s)
        s.commit()  # sem commit a reserva não vale pra próxima corretora
    return {
        "total": total,
        "itens": itens,
        "meta": meta,
        "reserva_minutos": RESERVA_MINUTOS,
        "referencia_historica": REFERENCIA_HISTORICA,
        # Como o telefone DESTA corretora disca. O link `tel:` do card usa isto:
        # mostrar o número certo e abrir o discador com o formato errado é pior
        # que não mostrar nada, porque ela toca, erra, e culpa o número do lead.
        "discagem": ("zero_ddd" if my_cid in CORRETORAS_CHIP_VIRTUAL else "padrao"),
    }


@router.post("/ligacoes/renovar")
def painel_ligacoes_renovar(
    body: dict[str, Any] = Body(...),
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Mantém a lista na tela reservada enquanto a aba está aberta.

    A tela chama isto de tempos em tempos e sempre que a corretora volta pra
    aba. `perdidos` = leads que estavam na tela e não são mais dela (outra
    corretora pegou depois que a reserva venceu, ou alguém já registrou a
    ligação) — a tela marca o cartão pra ela não ligar de novo. Ver
    `ligacao_resultado.renovar_reserva` (caso Corretor, 22/09).
    """
    from app.services.ligacao_resultado import RESERVA_MINUTOS, renovar_reserva

    user = _identificar_usuario(x_painel_user)
    if _corretora_id_do_user(user) is None and not _is_admin(user):
        raise HTTPException(403, "painel_user_sem_corretora")
    brutos = body.get("lead_ids")
    if not isinstance(brutos, list):
        raise HTTPException(422, "lead_ids_obrigatorio")
    try:
        pedidos = sorted({int(i) for i in brutos})
    except (TypeError, ValueError):
        raise HTTPException(422, "lead_ids_invalido")

    with SessionLocal() as s:
        meus = renovar_reserva(s, dono=user, lead_ids=pedidos)
        s.commit()
    ficaram = set(meus)
    return {
        "ok": True,
        "meus": meus,
        "perdidos": [i for i in pedidos if i not in ficaram],
        "reserva_minutos": RESERVA_MINUTOS,
    }


@router.post("/ligacoes/adiantar")
def painel_ligacoes_adiantar(
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """"Quer adiantar a tarefa de amanhã?" — libera mais um bloco pra HOJE.

    Só depois de fechar o bloco corrente (senão a fila volta a ser infinita).
    Adiantar NÃO dobra nem zera o dia seguinte: amanhã abre com a mesma meta —
    o que anda é o progresso da SEMANA, que é onde a folga aparece.
    """
    from app.services.ligacao_resultado import liberar_bloco_extra

    user = _identificar_usuario(x_painel_user)
    my_cid = _corretora_id_do_user(user)
    if my_cid is None:
        raise HTTPException(403, "painel_user_sem_corretora")

    with SessionLocal() as s:
        out = liberar_bloco_extra(s, corretora_id=my_cid)
        s.commit()
    if not out.get("ok"):
        raise HTTPException(409, out.get("motivo") or "nao_liberou")
    return out


@router.post("/ligacoes/agendar")
def painel_ligacoes_agendar(
    body: dict[str, Any] = Body(...),
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """"Ligar mais tarde" — a pessoa atendeu e pediu pra ligar em outra hora.

    Body: {"lead_id": <leads.id>, "data": "YYYY-MM-DD", "hora": "HH:MM",
           "observacao"?: str, "corretora_id"?: int (só admin)}

    Cria a tarefa "Ligar novamente" (mesma `tarefas_corretora` do chat, então
    ela aparece no Hoje e na aba de tarefas) e NÃO registra resultado de
    ligação: o lead CONTINUA na fila, agora no topo e na hora marcada.

    Escopo: usa `validar_lead_da_janela` — o mesmo do /registrar. O gate de dono
    do resto do painel não serve aqui: a fila é feita de lead SEM dona, e ele é
    admin-only nesse caso; a corretora não conseguiria agendar o próprio retorno.
    """
    from app.db.models import Lead
    from app.services.ligacao_resultado import (
        MOTIVO_EXPLICACAO,
        MOTIVO_OUTRA_CORRETORA,
        agendar_retorno,
    )
    from app.services.ligacao_resultado import (
        validar_lead_da_janela,
    )

    user = _identificar_usuario(x_painel_user)
    my_cid = _corretora_id_do_user(user)
    is_admin = _is_admin(user)
    if my_cid is None and not is_admin:
        raise HTTPException(403, "painel_user_sem_corretora")

    lead_id = body.get("lead_id")
    if lead_id is None:
        raise HTTPException(400, "lead_id obrigatório")

    alvo_cid = my_cid
    if alvo_cid is None:
        bruto = body.get("corretora_id")
        alvo_cid = int(bruto) if bruto is not None else None

    # data + hora → instante no fuso da operação (o que a corretora digitou é
    # hora de Cidade, não UTC).
    try:
        d = datetime.strptime(str(body.get("data") or "").strip(), "%Y-%m-%d").date()
    except (ValueError, AttributeError):
        raise HTTPException(422, "data_invalida") from None
    hora_str = str(body.get("hora") or "").strip()
    if not hora_str:
        raise HTTPException(422, "hora_obrigatoria")
    try:
        fmt = "%H:%M:%S" if hora_str.count(":") == 2 else "%H:%M"
        h = datetime.strptime(hora_str, fmt).time()
    except (ValueError, AttributeError):
        raise HTTPException(422, "hora_invalida") from None
    quando = datetime.combine(d, h).replace(tzinfo=ZoneInfo("America/Sao_Paulo"))

    with SessionLocal() as s:
        lead = s.query(Lead).filter(Lead.tenant_id == 1, Lead.id == int(lead_id)).first()
        if lead is None:
            raise HTTPException(404, "lead_nao_encontrado")

        motivo = validar_lead_da_janela(s, lead_id=int(lead_id), corretora_id=alvo_cid)
        if motivo is not None:
            log.warning(
                "ligacao_agendar_recusado",
                lead_id=int(lead_id), user=user, corretora_id=alvo_cid, motivo=motivo,
            )
            raise HTTPException(
                409 if motivo == MOTIVO_OUTRA_CORRETORA else 422,
                MOTIVO_EXPLICACAO.get(motivo, motivo),
            )

        out = agendar_retorno(
            s,
            lead_id=int(lead_id),
            quando=quando,
            corretora_id=alvo_cid,
            autor=user,
            external_ref=lead.external_ref,
            lead_nome=lead.nome,
            observacao=body.get("observacao"),
        )
        s.commit()
    return out


@router.post("/ligacoes/perder")
def painel_ligacoes_perder(
    body: dict[str, Any] = Body(...),
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """"Venda perdida" — tira o lead da fila e manda pra etapa Venda perdida.

    Body: {"lead_id": <leads.id>, "motivo"?: str, "corretora_id"?: int (admin)}

    Escopo pela regra da JANELA (`validar_lead_da_janela`), igual ao /registrar
    e ao /agendar: o gate de dono do resto do painel não serve aqui porque a
    fila é feita de lead SEM DONA.

    O lead ganha dona junto (quem marcou) — sem isso ele sairia da fila e não
    apareceria no board, que só mostra lead de corretora ativa. Ver
    `marcar_venda_perdida`.
    """
    from app.db.models import Lead
    from app.services.ligacao_resultado import (
        MOTIVO_EXPLICACAO,
        MOTIVO_OUTRA_CORRETORA,
        liberar_reserva,
        marcar_venda_perdida,
        validar_lead_da_janela,
    )

    user = _identificar_usuario(x_painel_user)
    my_cid = _corretora_id_do_user(user)
    is_admin = _is_admin(user)
    if my_cid is None and not is_admin:
        raise HTTPException(403, "painel_user_sem_corretora")

    lead_id = body.get("lead_id")
    if lead_id is None:
        raise HTTPException(400, "lead_id obrigatório")

    alvo_cid = my_cid
    if alvo_cid is None:
        bruto = body.get("corretora_id")
        alvo_cid = int(bruto) if bruto is not None else None

    with SessionLocal() as s:
        lead = s.query(Lead).filter(Lead.tenant_id == 1, Lead.id == int(lead_id)).first()
        if lead is None:
            raise HTTPException(404, "lead_nao_encontrado")

        # Sem corretora alvo o lead iria pra "Venda perdida" SEM DONA — sairia
        # da fila e sumiria do board (que só mostra lead de corretora ativa).
        # Recusar é melhor que fazer o lead desaparecer em silêncio.
        if alvo_cid is None and lead.owner_id is None:
            raise HTTPException(422, "sem_corretora_alvo")

        # Dona diferente NÃO barra o "venda perdida" (Dona, 10/08/2026): se o
        # lead apareceu na fila e alguém falou com a pessoa, ele está perdido —
        # segurar o botão deixa lead morto circulando. O lead continua da dona
        # original; `marcar_venda_perdida` só define dono quando não há nenhum.
        motivo_recusa = validar_lead_da_janela(
            s, lead_id=int(lead_id), corretora_id=alvo_cid,
            permitir_outra_corretora=True,
        )
        if motivo_recusa is not None:
            log.warning(
                "ligacao_perder_recusado",
                lead_id=int(lead_id), user=user, corretora_id=alvo_cid,
                motivo=motivo_recusa,
            )
            raise HTTPException(
                409 if motivo_recusa == MOTIVO_OUTRA_CORRETORA else 422,
                MOTIVO_EXPLICACAO.get(motivo_recusa, motivo_recusa),
            )

        out = marcar_venda_perdida(
            s,
            lead_id=int(lead_id),
            corretora_id=alvo_cid,
            autor=user,
            motivo=body.get("motivo"),
        )
        if not out.get("ok"):
            raise HTTPException(422, out.get("motivo") or "nao_moveu")
        # saiu da fila: não faz sentido seguir reservado pra ninguém
        liberar_reserva(s, lead_id=int(lead_id))
        s.commit()
    return out


@router.post("/ligacoes/telefone-errado")
def painel_ligacoes_telefone_errado(
    body: dict[str, Any] = Body(...),
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """"Número errado" — marca o telefone como inválido e tira o lead da fila.

    Body: {"lead_id": <leads.id>, "observacao"?: str, "corretora_id"?: int (admin)}

    NÃO registra resultado de ligação: número que não completa a chamada não é
    "não atendeu" (ver `marcar_telefone_errado`). Antes deste botão o único
    caminho era marcar "não atendeu", e o lead voltava a cada 24h pra sempre.

    Escopo pela regra da JANELA, com `permitir_outra_corretora` igual ao
    "venda perdida": cadastro furado é furado independente de quem é a dona, e
    segurar o botão deixa número morto girando na fila. Lead JÁ marcado volta
    `ja_marcado: true` em vez de 422 — o 422 de `telefone_invalido` aqui seria
    erro pra ação que já está feita (aba velha, clique duplo).
    """
    from app.db.models import Lead
    from app.services.ligacao_resultado import (
        MOTIVO_EXPLICACAO,
        MOTIVO_OUTRA_CORRETORA,
        MOTIVO_TELEFONE_INVALIDO,
        liberar_reserva,
        marcar_telefone_errado,
        validar_lead_da_janela,
    )

    user = _identificar_usuario(x_painel_user)
    my_cid = _corretora_id_do_user(user)
    is_admin = _is_admin(user)
    if my_cid is None and not is_admin:
        raise HTTPException(403, "painel_user_sem_corretora")

    lead_id = body.get("lead_id")
    if lead_id is None:
        raise HTTPException(400, "lead_id obrigatório")

    alvo_cid = my_cid
    if alvo_cid is None:
        bruto = body.get("corretora_id")
        alvo_cid = int(bruto) if bruto is not None else None

    with SessionLocal() as s:
        lead = s.query(Lead).filter(Lead.tenant_id == 1, Lead.id == int(lead_id)).first()
        if lead is None:
            raise HTTPException(404, "lead_nao_encontrado")

        motivo = validar_lead_da_janela(
            s, lead_id=int(lead_id), corretora_id=alvo_cid,
            permitir_outra_corretora=True,
        )
        # já marcado não é erro: a ação pedida é exatamente o estado atual
        if motivo is not None and motivo != MOTIVO_TELEFONE_INVALIDO:
            log.warning(
                "ligacao_telefone_errado_recusado",
                lead_id=int(lead_id), user=user, corretora_id=alvo_cid, motivo=motivo,
            )
            raise HTTPException(
                409 if motivo == MOTIVO_OUTRA_CORRETORA else 422,
                MOTIVO_EXPLICACAO.get(motivo, motivo),
            )

        out = marcar_telefone_errado(
            s,
            lead_id=int(lead_id),
            corretora_id=alvo_cid,
            autor=user,
            observacao=body.get("observacao"),
        )
        if not out.get("ok"):
            raise HTTPException(422, out.get("motivo") or "nao_marcou")
        liberar_reserva(s, lead_id=int(lead_id))
        s.commit()
    return out


@router.post("/ligacoes/registrar")
def painel_ligacoes_registrar(
    body: dict[str, Any] = Body(...),
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Registra o resultado de uma ligação da janela — e adota o lead quando o
    resultado é "atendeu e quer continuar".

    Body: {"lead_id": <leads.id>, "resultado": <um dos três estados>,
           "observacao"?: str, "corretora_id"?: int (só admin)}

    Adoção: lead vai pra "Novo" (entrada do funil) + ganha a corretora como
    dona + recebe a tag `adotado_ligacao_em`. NUNCA rebaixa etapa mais avançada
    e NUNCA tira o dono de quem já tem — nesses casos devolve `moveu_etapa`/
    `ja_tem_dono` pra a tela avisar.

    As regras da fila valem AQUI, no backend (`validar_lead_da_janela`): lead
    de outra corretora, fora do remarketing, com opt-out ou telefone morto é
    recusado com 409/422. Validação que só existe no front é validação que uma
    aba velha (ou um curl) fura.
    """
    from app.db.models import Lead
    from app.services.ligacao_resultado import (
        MOTIVO_EXPLICACAO,
        MOTIVO_OUTRA_CORRETORA,
        ORIGEM_REMARKETING,
        RESULTADO_QUER_CONTINUAR,
        adotar_lead,
        liberar_reserva,
        marcar_confete,
        progresso_ligacao,
        registrar_resultado,
        validar_lead_da_janela,
    )

    user = _identificar_usuario(x_painel_user)
    my_cid = _corretora_id_do_user(user)
    is_admin = _is_admin(user)
    if my_cid is None and not is_admin:
        raise HTTPException(403, "painel_user_sem_corretora")

    lead_id = body.get("lead_id")
    if lead_id is None:
        raise HTTPException(400, "lead_id obrigatório")
    resultado = _resultado_do_body(body)
    if resultado is None:
        raise HTTPException(422, "resultado_obrigatorio")

    # Admin não tem corretora própria: pra adotar precisa dizer por quem.
    alvo_cid = my_cid
    if alvo_cid is None:
        bruto = body.get("corretora_id")
        alvo_cid = int(bruto) if bruto is not None else None

    with SessionLocal() as s:
        lead = (
            s.query(Lead)
            .filter(Lead.tenant_id == 1, Lead.id == int(lead_id))
            .first()
        )
        if lead is None:
            raise HTTPException(404, "lead_nao_encontrado")

        motivo = validar_lead_da_janela(s, lead_id=int(lead_id), corretora_id=alvo_cid)
        if motivo is not None:
            log.warning(
                "ligacao_registrar_recusado",
                lead_id=int(lead_id), user=user, corretora_id=alvo_cid, motivo=motivo,
            )
            raise HTTPException(
                409 if motivo == MOTIVO_OUTRA_CORRETORA else 422,
                MOTIVO_EXPLICACAO.get(motivo, motivo),
            )

        registro = registrar_resultado(
            s,
            origem=ORIGEM_REMARKETING,
            origem_id=int(lead_id),
            resultado=resultado,
            lead_id=lead.external_ref,
            corretora_id=alvo_cid,
            autor=user,
        )

        adocao: dict[str, Any] | None = None
        envio: dict[str, Any] = {"ok": False, "motivo": "nao_se_aplica"}
        if resultado.estado == RESULTADO_QUER_CONTINUAR:
            if alvo_cid is None:
                adocao = {"ok": False, "motivo": "sem_corretora_alvo"}
            else:
                adocao = adotar_lead(
                    s, lead_id=int(lead_id), corretora_id=alvo_cid, autor=user
                )
                # A mensagem que continua a conversa no WhatsApp. Dentro de try
                # pelo mesmo motivo do confete abaixo: NADA pode fazer a
                # corretora perder o registro da ligação que ela acabou de
                # fazer. Se a Meta cair, o trabalho dela fica gravado e só a
                # mensagem falta — e o motivo volta na resposta pra tela poder
                # dizer o que aconteceu em vez de fingir que enviou.
                try:
                    from app.services import pos_ligacao

                    lead_row = s.execute(
                        sql_text(
                            "SELECT nome, telefone FROM leads "
                            " WHERE id = :lid AND tenant_id = 1"
                        ),
                        {"lid": int(lead_id)},
                    ).mappings().first()
                    envio = pos_ligacao.enviar(
                        s,
                        lead_id=int(lead_id),
                        telefone=(lead_row or {}).get("telefone"),
                        nome=(lead_row or {}).get("nome"),
                        corretora_id=alvo_cid,
                        corretora_nome=CORRETORA_LABEL.get(alvo_cid),
                    )
                except Exception as e:  # noqa: BLE001
                    log.warning("pos_ligacao_erro", erro=str(e)[:140])
                    envio = {"ok": False, "motivo": "erro_interno"}
        # lead trabalhado não fica segurado: ou saiu da fila (atendeu) ou volta
        # pro rodízio depois do descanso, pra qualquer corretora.
        liberar_reserva(s, lead_id=int(lead_id))

        # Meta do dia — a comemoração é ENFEITE: calculada depois do registro já
        # gravado e dentro de try, porque nada disso pode fazer a corretora
        # perder uma ligação. Se quebrar, `meta` vem None e o registro fica.
        meta: dict[str, Any] | None = None
        confete = False
        try:
            meta = progresso_ligacao(s, corretora_id=alvo_cid)
            if meta and meta["concluiu_dia"] and alvo_cid is not None:
                # True só na PRIMEIRA vez do dia — não cai confete a cada F5
                confete = marcar_confete(s, corretora_id=alvo_cid)
                meta = progresso_ligacao(s, corretora_id=alvo_cid)
        except Exception as e:  # noqa: BLE001
            log.warning("ligacao_meta_indisponivel", erro=str(e)[:140])
            meta, confete = None, False
        s.commit()

    return {
        "ok": True,
        "registro_ligacao": registro,
        "adocao": adocao,
        "mensagem_enviada": envio,
        "meta": meta,
        "confete": confete,
    }


@router.post("/cadencia/ligacao-feita")
def painel_cadencia_ligacao_feita(
    body: dict[str, Any] = Body(...),
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Marca o toque de LIGAÇÃO atual da cadência como FEITO ("a Pessoa12 ligou").

    Body: {"cadencia_id": int, "toque"?: int, "resultado"?: str, "observacao"?: str}
    Registra o evento tarefa_ligacao e AVANÇA o estado (tira da fila de devidas).
    Com `resultado` (um dos três estados), grava também a nota na conversa do
    lead + o dado estruturado da métrica. Sem ele, a ligação fica "sem registro"
    — nunca "não atendeu".
    Ownership: corretora só marca cadência dela; admin marca qualquer.
    """
    from app.services.cadencia import marcar_ligacao_feita
    from app.services.ligacao_resultado import ORIGEM_CADENCIA, registrar_resultado

    user = _identificar_usuario(x_painel_user)
    my_cid = _corretora_id_do_user(user)
    is_admin = _is_admin(user)
    if my_cid is None and not is_admin:
        raise HTTPException(403, "painel_user_sem_corretora")

    cadencia_id = body.get("cadencia_id")
    if not cadencia_id:
        raise HTTPException(400, "cadencia_id obrigatório")
    toque_esperado = body.get("toque")
    resultado = _resultado_do_body(body)

    with SessionLocal() as s:
        # Ownership: corretora só mexe na cadência dela.
        estado = s.execute(
            sql_text(
                "SELECT lead_id, corretora_id FROM cadencia_estado WHERE id = :cid"
            ),
            {"cid": cadencia_id},
        ).mappings().first()
        if my_cid is not None:
            if not estado or estado["corretora_id"] != my_cid:
                raise HTTPException(403, "cadencia_de_outra_corretora")
        res = marcar_ligacao_feita(
            s,
            cadencia_id=int(cadencia_id),
            toque_esperado=int(toque_esperado) if toque_esperado is not None else None,
        )
        registro: dict[str, Any] | None = None
        if res.get("ok") and resultado is not None:
            registro = registrar_resultado(
                s,
                origem=ORIGEM_CADENCIA,
                origem_id=int(cadencia_id),
                # o toque que o motor de fato marcou (não o que o front achava)
                toque=res.get("toque_marcado"),
                resultado=resultado,
                lead_id=(estado["lead_id"] if estado else None),
                corretora_id=(estado["corretora_id"] if estado else my_cid),
                autor=user,
            )
            s.commit()
    if not res.get("ok"):
        # 200 com ok:false é o contrato do front (ele já mostra o toast de erro),
        # mas SEM log a recusa é invisível: o bug dos índices de ligação passou
        # dias assim — nginx só via 200 e ninguém soube até a corretora reclamar.
        log.warning(
            "cadencia_ligacao_feita_recusada",
            cadencia_id=int(cadencia_id), user=user, motivo=res.get("motivo"),
        )
        return {"ok": False, "motivo": res.get("motivo")}
    return {"ok": True, **res, "registro_ligacao": registro}


@router.get("/me")
def painel_me(
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Endpoint leve pra frontend detectar quem é o viewer (role + corretora).
    Usado no boot do App pra renderizar a versão correta."""
    user = _identificar_usuario(x_painel_user)
    return _viewer_info(user)


def _viewer_info(user: str) -> dict[str, Any]:
    cid = _corretora_id_do_user(user)
    return {
        "user": user,
        "role": "corretora" if cid else ("admin" if _is_admin(user) else "unknown"),
        "corretora_id": cid,
        "corretora_label": CORRETORA_LABEL.get(cid) if cid else None,
    }


@router.get("/visao-geral")
def painel_visao_geral(
    periodo: str = Query("mes_atual"),
    inicio: str | None = Query(None),
    fim: str | None = Query(None),
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    user = _identificar_usuario(x_painel_user)
    viewer = _viewer_info(user)
    my_cid = viewer["corretora_id"]
    di, df, label = parse_periodo(periodo, inicio, fim)
    # Agenda do mês inteiro em "mês atual": visita já marcada pro dia 27 é
    # agendamento deste mês. Leads, gasto e vendas seguem até hoje.
    visitas_ate = None
    if periodo == "mes_atual":
        prox = (df.replace(day=28) + timedelta(days=4)).replace(day=1)
        visitas_ate = prox - timedelta(days=1)
    funil = calcular_funil(di, df, agenda_completa=True, visitas_ate=visitas_ate)
    meta = calcular_metricas_meta_ads(di, df)
    visitas = listar_visitas(di, visitas_ate or df)
    comparecimento = resumo_comparecimento(visitas["por_status"])
    eq = funil["equipe"]
    spend = float(meta["spend"] or 0)
    vgv = float(eq["vgv"] or 0)
    # ROI e custo por venda: só venda de mídia (vendas.origem = 'midia').
    retorno = montar_retorno(spend, vgv, vendas_por_origem(di, df))
    roi = retorno["roi"]
    custo_visita = round(spend / eq["visitas"], 2) if eq["visitas"] else None
    custo_venda = retorno["custo_venda"]
    alertas = detectar_alertas(
        eq, meta, {**visitas, "taxa_comparecimento": comparecimento["taxa_comparecimento"]},
        funil["corretoras"], vgv_midia=retorno["vgv_midia"],
    )
    comparativo = calcular_comparativo(periodo, di, df, meta, funil)
    vazamento = calcular_vazamento_real(di, df)
    recuperados = calcular_recuperados(di, df)
    # Corretora não vê recovery da Corretor → None (mesmo padrão de remarketing_corretor e
    # saude, logo abaixo). Devolver o dict zerado acendia o card e o título
    # "Recuperação & atenção" com tudo em 0 — e dict vazio é truthy, então a
    # guarda do front não pegava.
    recovery_corretor = calcular_recovery_corretor(di, df) if not my_cid else None
    # Remarketing Pessoa12: admin vê sempre; a própria Pessoa12 também (escopo viewer).
    # Outras corretoras não veem (privacidade: dado é da operação da Pessoa12).
    remarketing_corretor = (
        calcular_remarketing_corretor(di, df)
        if my_cid in (None, REMARKETING_CORRETORA_ID)
        else None
    )
    atencao = calcular_atencao_hoje()
    tendencia = calcular_tendencia_14d()
    saude = calcular_saude_sistema() if not my_cid else None

    # ── Privacidade: se viewer é corretora, filtra/redact ──
    if my_cid is not None:
        # Funil: mostra só a corretora dela
        funil_filtered = [c for c in funil["corretoras"] if c.get("corretora_id") == my_cid]
        # Equipe vira a métrica pessoal dela
        eq_pessoal = next((c for c in funil_filtered), None)
        if eq_pessoal:
            # Mantém compatibilidade com type FunilEquipe
            eq = {
                "leads": eq_pessoal["leads"],
                "atendidos": eq_pessoal["atendidos"],
                "recuperados": eq_pessoal.get("recuperados", 0),
                "agendados": eq_pessoal["agendados"],
                "visitas": eq_pessoal["visitas"],
                "vendas": eq_pessoal["vendas"],
                "vgv": eq_pessoal["vgv"],
                "ticket_medio": eq_pessoal["ticket_medio"],
                "conv_atendeu": eq_pessoal["conv_atendeu"],
                "conv_agendou": eq_pessoal["conv_agendou"],
                "conv_visitou": eq_pessoal["conv_visitou"],
                "conv_vendeu": eq_pessoal["conv_vendeu"],
            }
        funil_corretoras_resp = funil_filtered
        # Esconde dados de agência: Meta Ads spend, vazamento, comparativo
        meta_resp = {"spend": 0, "leads_meta": 0, "clicks": 0, "impressions": 0,
                     "cpl": None, "cpc": None}
        vazamento_resp = None
        comparativo_resp = None
        # ROI/derivados — escondidos
        derivados_resp = {"roi": None, "custo_visita": None, "custo_venda": None}
        # Visitas: filtra só dela
        visitas_filtered = {
            "total": sum(1 for v in visitas["items"] if v.get("corretora_id") == my_cid),
            "por_status": {},
            "taxa_comparecimento": None,
            "total_primeiras": 0,
            "total_revisitas": 0,
            "por_status_primeiras": {},
            "taxa_comparecimento_primeiras": None,
            "items": [v for v in visitas["items"] if v.get("corretora_id") == my_cid],
        }
        # Conta status só dela
        for v in visitas_filtered["items"]:
            s = v["status"]
            visitas_filtered["por_status"][s] = visitas_filtered["por_status"].get(s, 0) + 1
            if v.get("is_revisita"):
                visitas_filtered["total_revisitas"] += 1
            else:
                visitas_filtered["total_primeiras"] += 1
                visitas_filtered["por_status_primeiras"][s] = visitas_filtered["por_status_primeiras"].get(s, 0) + 1
        comparecimento = resumo_comparecimento(visitas_filtered["por_status"])
        visitas_filtered["taxa_comparecimento"] = comparecimento["taxa_comparecimento"]
        comp_p = visitas_filtered["por_status_primeiras"].get("compareceu", 0)
        ns_p = visitas_filtered["por_status_primeiras"].get("no_show", 0)
        fin_p = comp_p + ns_p
        visitas_filtered["taxa_comparecimento_primeiras"] = round(comp_p / fin_p * 100, 1) if fin_p else None
        visitas = visitas_filtered
        # Atenção: filtra leads quentes/mornos/visitas/tarefas só dela
        atencao = {
            "data": atencao["data"],
            "leads_quentes": {
                "total": next((x["qtde"] for x in atencao["leads_quentes"]["por_corretora"] if x["corretora_id"] == my_cid), 0),
                "por_corretora": [x for x in atencao["leads_quentes"]["por_corretora"] if x["corretora_id"] == my_cid],
            },
            "leads_mornos_com_gap": {
                "total": next((x["qtde"] for x in atencao["leads_mornos_com_gap"]["por_corretora"] if x["corretora_id"] == my_cid), 0),
                "por_corretora": [x for x in atencao["leads_mornos_com_gap"]["por_corretora"] if x["corretora_id"] == my_cid],
            },
            "visitas_hoje": [v for v in atencao["visitas_hoje"] if v["corretora_id"] == my_cid],
            "tarefas_atrasadas": {
                "total": next((x["qtde"] for x in atencao["tarefas_atrasadas"]["por_corretora"] if x["corretora_id"] == my_cid), 0),
                "por_corretora": [x for x in atencao["tarefas_atrasadas"]["por_corretora"] if x["corretora_id"] == my_cid],
            },
        }
        # Alertas: só os que se referem a ela
        alertas_resp = [a for a in alertas if a.get("corretora_id") in (None, my_cid)]
        # Recuperados: só dela
        recup_mine = next((x["qtde"] for x in recuperados.get("por_user", []) if x.get("user_id") == CORRETORA_KOMMO_USER.get(my_cid)), 0)
        recuperados_resp = {"total": recup_mine, "movimentos": recup_mine, "por_user": []}
        # Tendência: meta_leads → escondido (info agência); mensagens só dela
        my_msg = tendencia["mensagens_por_corretora"].get(str(my_cid), [])
        tendencia_resp = {
            "dias": tendencia["dias"],
            # Série VAZIA, não zerada: fingir "0 lead por dia" é informação
            # falsa. Com [] a guarda `dados.length === 0` do front esconde o
            # gráfico sozinha.
            "meta_leads": [],
            "mensagens_total": my_msg,
            "mensagens_enviadas": my_msg,
            "mensagens_recebidas": my_msg,
            "mensagens_por_corretora": {str(my_cid): my_msg},
        }
    else:
        funil_corretoras_resp = funil["corretoras"]
        meta_resp = meta
        vazamento_resp = vazamento
        comparativo_resp = comparativo
        derivados_resp = {"roi": roi, "custo_visita": custo_visita,
                          "custo_venda": custo_venda, "retorno": retorno}
        alertas_resp = alertas
        recuperados_resp = recuperados
        tendencia_resp = tendencia

    # Meta individual (corretoras): visitas dessa semana vs META_VISITAS_SEMANA
    meta_pessoal_resp = None
    if my_cid is not None:
        from datetime import date as _date
        hoje_sp = _hoje_sp()
        seg = hoje_sp - timedelta(days=hoje_sp.weekday())
        fim_sem = seg + timedelta(days=6)
        seg_dt = datetime.combine(seg, datetime.min.time(), tzinfo=SP_TZ)
        fim_dt = datetime.combine(fim_sem, datetime.max.time(), tzinfo=SP_TZ)
        with SessionLocal() as s:
            row = s.execute(sql_text(
                """SELECT
                     COUNT(*) FILTER (WHERE status != 'cancelada') AS agendadas,
                     COUNT(*) FILTER (WHERE status = 'compareceu')  AS realizadas
                   FROM visitas
                   WHERE corretora_id = :cid AND data_hora BETWEEN :di AND :df"""
            ), {"cid": my_cid, "di": seg_dt, "df": fim_dt}).first()
        agendadas = int(row[0] or 0) if row else 0
        realizadas = int(row[1] or 0) if row else 0
        meta_pessoal_resp = {
            "meta_visitas_semana": META_VISITAS_SEMANA,
            "agendadas_semana": agendadas,
            "realizadas_semana": realizadas,
            "semana_inicio": seg.isoformat(),
            "semana_fim": fim_sem.isoformat(),
        }
    return {
        "user": user,
        "viewer": viewer,
        "meta_pessoal": meta_pessoal_resp,
        "periodo": {"tipo": periodo, "inicio": di.isoformat(),
                    "fim": df.isoformat(), "label": label},
        "meta_ads": meta_resp,
        "funil_equipe": eq,
        "funil_por_corretora": funil_corretoras_resp,
        "derivados": derivados_resp,
        "visitas_resumo": {
            "total": visitas["total"],
            **comparecimento,
        },
        "alertas": alertas_resp,
        "comparativo": comparativo_resp,
        "vazamento_captacao": vazamento_resp,
        "recuperados": recuperados_resp,
        "recovery_corretor": recovery_corretor,
        "remarketing_corretor": remarketing_corretor,
        "atencao_hoje": atencao,
        "tendencia_14d": tendencia_resp,
        "saude_sistema": saude,
        "fontes": _frescor_fontes(),
    }


@router.get("/agenda")
def painel_agenda(
    periodo: str = Query("mes_atual"),
    inicio: str | None = Query(None),
    fim: str | None = Query(None),
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    user = _identificar_usuario(x_painel_user)
    my_cid = _corretora_id_do_user(user)
    di, df, label = parse_periodo(periodo, inicio, fim)
    visitas = listar_visitas(di, df)
    if my_cid is not None:
        items_filtered = [v for v in visitas["items"] if v.get("corretora_id") == my_cid]
        por_status: dict[str, int] = {}
        for v in items_filtered:
            por_status[v["status"]] = por_status.get(v["status"], 0) + 1
        comp = por_status.get("compareceu", 0)
        no_show = por_status.get("no_show", 0)
        fin = comp + no_show
        visitas = {
            "total": len(items_filtered),
            "por_status": por_status,
            "taxa_comparecimento": round(comp / fin * 100, 1) if fin else None,
            "items": items_filtered,
        }
    # ── VENCIDAS EM ABERTO: fora do filtro de propósito ──────────────────────
    #
    # Visita passada que ninguém fechou é SEMPRE de dias atrás — e a tela abria
    # em "semana", então ela ficava invisível justamente pra quem tem que
    # resolver. Foi assim que a Pessoa10 ficou com 2 visitas paradas sem conseguir
    # marcar (11/08): não era permissão nem botão quebrado, a lista vinha vazia.
    #
    # Estas vêm SEMPRE, independente do período escolhido. Escopadas por
    # corretora igual ao resto: cada uma vê só as suas.
    pendentes: list[dict[str, Any]] = []
    try:
        with SessionLocal() as s:
            linhas = s.execute(
                sql_text(
                    """
                    SELECT id, lead_nome, telefone_lead, corretora_id, bairro,
                           data_hora,
                           EXTRACT(DAY FROM (now() - data_hora))::int AS dias
                      FROM visitas
                     WHERE tenant_id = 1
                       AND status IN ('agendada', 'pendente_confirmacao')
                       AND data_hora < now()
                       AND data_hora > now() - interval '120 days'
                     ORDER BY data_hora DESC
                    """
                )
            ).mappings().all()
        for r in linhas:
            if my_cid is not None and r["corretora_id"] != my_cid:
                continue
            pendentes.append({
                "id": r["id"],
                "lead_nome": r["lead_nome"],
                "telefone_lead": r["telefone_lead"],
                "corretora_id": r["corretora_id"],
                "bairro": r["bairro"],
                "data_hora": r["data_hora"].isoformat() if r["data_hora"] else None,
                "dias_atras": r["dias"],
            })
    except Exception as e:  # noqa: BLE001 — o bloco extra nunca derruba a agenda
        log.warning("agenda_pendentes_falhou", erro=str(e)[:140])

    # ── VISITA MARCADA LONGE: o risco medido (13/08) ──────────────────────────
    #
    # Antecedência × comparecimento nos últimos 90 dias, dado real:
    #     mesmo dia   13/13 = 100%
    #     amanhã       3/6  =  50%
    #     2-3 dias     7/11 =  64%
    #     4-7 dias     2/10 =  20%   ← aqui
    #
    # Não é falta de persuasão nem de confirmação: é DISTÂNCIA. O lead esfria e
    # a vida acontece. Cada visita marcada pra 4+ dias tem 4 em 5 chance de não
    # acontecer, e a corretora não tem como saber disso olhando a agenda.
    #
    # O aviso não bloqueia nada — a visita distante às vezes é a única opção da
    # pessoa. Ele só diz "essa aqui precisa de confirmação na véspera", que é a
    # única coisa que dá pra fazer depois de marcada.
    ANTECEDENCIA_RISCO = 4
    em_risco: list[dict[str, Any]] = []
    try:
        with SessionLocal() as s:
            linhas = s.execute(
                sql_text(
                    """
                    SELECT id, lead_nome, telefone_lead, corretora_id, bairro,
                           data_hora,
                           (data_hora::date - criado_em::date) AS antecedencia,
                           (data_hora::date - (now() AT TIME ZONE 'America/Sao_Paulo')::date) AS falta
                      FROM visitas
                     WHERE tenant_id = 1
                       AND status IN ('agendada', 'pendente_confirmacao')
                       AND data_hora > now()
                       AND (data_hora::date - criado_em::date) >= :antec
                     ORDER BY data_hora
                    """
                ),
                {"antec": ANTECEDENCIA_RISCO},
            ).mappings().all()
        for r in linhas:
            if my_cid is not None and r["corretora_id"] != my_cid:
                continue
            em_risco.append({
                "id": r["id"],
                "lead_nome": r["lead_nome"],
                "telefone_lead": r["telefone_lead"],
                "corretora_id": r["corretora_id"],
                "bairro": r["bairro"],
                "data_hora": r["data_hora"].isoformat() if r["data_hora"] else None,
                "antecedencia_dias": r["antecedencia"],
                "falta_dias": r["falta"],
            })
    except Exception as e:  # noqa: BLE001
        log.warning("agenda_risco_falhou", erro=str(e)[:140])

    # ── SEM TELEFONE: a visita não se liga a lead nenhum ─────────────────────
    #
    # Medido em 17/08: 74% das visitas de agosto e 100% de julho nasceram sem
    # telefone. Sem ele a visita é um registro solto — não entra no funil, não
    # cruza com a conversa, não conta pra nada. Foi assim que a Lucilene
    # compareceu, entregou documentação e ficou invisível.
    #
    # A raiz já foi consertada (criar_visita.py resolve pelo nome), mas quando o
    # nome é ambíguo ou não existe no CRM ele não tem como adivinhar — e chutar
    # seria pior: telefone errado aponta pra pessoa errada e ninguém desconfia.
    # Nesse caso a corretora confirma, que é quem sabe.
    sem_telefone: list[dict[str, Any]] = []
    try:
        with SessionLocal() as s:
            linhas = s.execute(
                sql_text(
                    """
                    SELECT id, lead_nome, corretora_id, bairro, data_hora, status
                      FROM visitas
                     WHERE tenant_id = 1
                       AND (telefone_lead IS NULL OR btrim(telefone_lead) = '')
                       AND data_hora > now() - interval '45 days'
                     ORDER BY data_hora DESC
                    """
                )
            ).mappings().all()
        for r in linhas:
            if my_cid is not None and r["corretora_id"] != my_cid:
                continue
            sem_telefone.append({
                "id": r["id"],
                "lead_nome": r["lead_nome"],
                "corretora_id": r["corretora_id"],
                "bairro": r["bairro"],
                "status": r["status"],
                "data_hora": r["data_hora"].isoformat() if r["data_hora"] else None,
            })
    except Exception as e:  # noqa: BLE001
        log.warning("agenda_sem_telefone_falhou", erro=str(e)[:140])

    return {
        "user": user,
        "viewer": _viewer_info(user),
        "periodo": {"tipo": periodo, "inicio": di.isoformat(),
                    "fim": df.isoformat(), "label": label},
        "pendentes_vencidas": pendentes,
        "marcadas_longe": em_risco,
        "sem_telefone": sem_telefone,
        **visitas,
    }


@router.get("/conversas")
def painel_conversas(
    periodo: str = Query("mes_atual"),
    inicio: str | None = Query(None),
    fim: str | None = Query(None),
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    user = _identificar_usuario(x_painel_user)
    my_cid = _corretora_id_do_user(user)
    di, df, label = parse_periodo(periodo, inicio, fim)
    conv = calcular_conversas(di, df)
    ttr = calcular_ttr(di, df)
    tendencia = calcular_tendencia_14d()
    if my_cid is not None:
        conv["por_corretora"] = [c for c in conv["por_corretora"] if c["corretora_id"] == my_cid]
        my = next((c for c in conv["por_corretora"]), None)
        if my:
            conv["equipe"] = {
                "enviadas": my["enviadas"],
                "recebidas": my["recebidas"],
                "contatos_unicos_soma": my["contatos_unicos"],
            }
        else:
            conv["equipe"] = {"enviadas": 0, "recebidas": 0, "contatos_unicos_soma": 0}
        conv["ligacoes"]["por_corretora"] = [
            c for c in conv["ligacoes"]["por_corretora"] if c["corretora_id"] == my_cid
        ]
        lig_my = next((c for c in conv["ligacoes"]["por_corretora"]), None)
        if lig_my:
            conv["ligacoes"]["equipe"] = {
                "total": lig_my["total"], "atendeu": lig_my["atendeu"],
                "prosseguiu": lig_my["prosseguiu"],
                "taxa_atendimento_pct": lig_my["taxa_atendimento_pct"],
                "taxa_conversao_pct": lig_my["taxa_conversao_pct"],
            }
        ttr["por_corretora"] = [c for c in ttr["por_corretora"] if c["corretora_id"] == my_cid]
        my_msg = tendencia["mensagens_por_corretora"].get(str(my_cid), [])
        tendencia = {
            "dias": tendencia["dias"],
            # Série VAZIA, não zerada: fingir "0 lead por dia" é informação
            # falsa. Com [] a guarda `dados.length === 0` do front esconde o
            # gráfico sozinha.
            "meta_leads": [],
            "mensagens_total": my_msg, "mensagens_enviadas": my_msg,
            "mensagens_recebidas": my_msg,
            "mensagens_por_corretora": {str(my_cid): my_msg},
        }
    return {
        "user": user,
        "viewer": _viewer_info(user),
        "periodo": {"tipo": periodo, "inicio": di.isoformat(),
                    "fim": df.isoformat(), "label": label},
        **conv,
        "tempo_resposta": ttr,
        "tendencia_14d": tendencia,
        "fontes": _frescor_fontes(),
    }


@router.get("/ranking")
def painel_ranking(
    periodo: str = Query("mes_atual"),
    inicio: str | None = Query(None),
    fim: str | None = Query(None),
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    user = _identificar_usuario(x_painel_user)
    _require_admin(user)  # ranking de toda a equipe — só admin
    di, df, label = parse_periodo(periodo, inicio, fim)
    funil = calcular_funil(di, df)
    cors = funil["corretoras"]

    def rank_by(key: str, reverse: bool = True) -> list[dict[str, Any]]:
        items = [c for c in cors if c.get(key) is not None]
        items.sort(key=lambda c: c[key] or 0, reverse=reverse)
        return [{"label": c["label"], "valor": c[key]} for c in items]

    return {
        "user": user,
        "periodo": {"tipo": periodo, "inicio": di.isoformat(),
                    "fim": df.isoformat(), "label": label},
        "atendimento": rank_by("conv_atendeu"),
        "agendamento": rank_by("conv_agendou"),
        "comparecimento": rank_by("conv_visitou"),
        "vendas_qtde": rank_by("vendas"),
        "vgv": rank_by("vgv"),
    }


# ── Cache do /painel/historico ────────────────────────────────────────────────
#
# Mês FECHADO nunca muda → calculado 1x e persistido em historico_cache
# (Postgres, migration 0059). Mês corrente muda ao longo do dia → TTL curto.
# A validade é decidida na LEITURA comparando computed_at com o fechamento do
# mês (fuso SP): payload calculado APÓS o mês fechar é imutável; calculado
# ainda DENTRO do mês (inclui o mês corrente) vale por HISTORICO_TTL.

HISTORICO_TTL_MES_ABERTO_SEC = 15 * 60


def _historico_cache_valido(ano_mes: str, computed_at: datetime, now: datetime) -> bool:
    """True se o payload cacheado de `ano_mes` ainda vale em `now` (aware UTC)."""
    y, m = (int(x) for x in ano_mes.split("-"))
    fim_mes = datetime(
        y + 1 if m == 12 else y, 1 if m == 12 else m + 1, 1, tzinfo=SP_TZ
    )
    if computed_at.tzinfo is None:
        computed_at = computed_at.replace(tzinfo=timezone.utc)
    if computed_at >= fim_mes:
        return True  # mês fechado, calculado após o fechamento → imutável
    return (now - computed_at).total_seconds() < HISTORICO_TTL_MES_ABERTO_SEC


def _historico_mes_payload(ano_alvo: int, m: int) -> dict[str, Any]:
    """Calcula o bloco de UM mês do histórico (funil + Meta Ads). Caro (~5-10s):
    bate no Kommo (events paginados), Postgres e SQLite do gestor-trafego."""
    di = date(ano_alvo, m, 1)
    if m == 12:
        df = date(ano_alvo, 12, 31)
    else:
        df = date(ano_alvo, m + 1, 1) - timedelta(days=1)
    funil = calcular_funil(di, df)
    meta = calcular_metricas_meta_ads(di, df)
    eq = funil["equipe"]
    gasto = meta.get("spend") or 0
    vgv = eq.get("vgv") or 0
    roi = round(vgv / gasto, 1) if gasto > 0 else None

    # Por corretora
    por_corretora = []
    for c in funil["corretoras"]:
        por_corretora.append({
            "corretora_id": c["corretora_id"],
            "label": c["label"],
            "nome": c["nome"],
            "leads": c["leads"],
            "atendidos": c["atendidos"],
            "atendidos_evolution": c.get("atendidos_evolution") or 0,
            "agendados": c["agendados"],
            "visitas": c["visitas"],
            "vendas": c["vendas"],
            "vgv": c["vgv"],
            "ticket_medio": c.get("ticket_medio"),
            "conv_atendeu": c["conv_atendeu"],
            "conv_agendou": c["conv_agendou"],
            "conv_visitou": c["conv_visitou"],
            "conv_vendeu": c["conv_vendeu"],
        })

    return {
        "ano_mes": f"{ano_alvo:04d}-{m:02d}",
        "label": di.strftime("%b/%y"),
        "meta_ads": {
            "spend": gasto,
            "leads_meta": meta.get("leads_meta") or 0,
            "cpl": meta.get("cpl"),
        },
        "equipe": {
            "leads": eq["leads"],
            "atendidos": eq["atendidos"],
            "atendidos_evolution": eq.get("atendidos_evolution") or 0,
            "agendados": eq["agendados"],
            "visitas": eq["visitas"],
            "vendas": eq["vendas"],
            "vgv": vgv,
            "ticket_medio": eq.get("ticket_medio"),
            "roi": roi,
            "conv_atendeu": eq["conv_atendeu"],
            "conv_agendou": eq["conv_agendou"],
            "conv_visitou": eq["conv_visitou"],
            "conv_vendeu": eq["conv_vendeu"],
        },
        "por_corretora": por_corretora,
    }


@router.get("/historico")
def painel_historico(
    ano: int | None = Query(None, description="Ano (default: ano corrente)"),
    force: int = Query(0, description="1 = invalida o cache do ano e recalcula tudo"),
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Visão mensal do ano: meta_ads + funil (equipe + por corretora).

    Loop Jan-mês_atual do ano alvo, com cache persistente (historico_cache):
      - mês FECHADO: calculado 1x e servido do cache pra sempre;
      - mês corrente: recalculado no máximo a cada HISTORICO_TTL_MES_ABERTO_SEC.
    Primeira chamada sem cache se comporta como antes (calcula e grava).

    `?force=1` (admin — endpoint já é admin-only) apaga o cache do ano e
    recalcula tudo. É o remédio quando venda/lead entra RETROATIVAMENTE num
    mês já fechado (registro atrasado acontece): sem force, o mês fechado
    seguiria servindo o payload antigo indefinidamente.
    """
    user = _identificar_usuario(x_painel_user)
    _require_admin(user)  # histórico traz spend/ROI/funil de todas as corretoras — só admin
    hoje = date.today()
    ano_alvo = ano or hoje.year
    ultimo_mes = hoje.month if ano_alvo == hoje.year else 12
    keys_ano = [f"{ano_alvo:04d}-{m:02d}" for m in range(1, ultimo_mes + 1)]

    cache: dict[str, HistoricoCache] = {}
    with SessionLocal() as s:
        if force:
            s.query(HistoricoCache).filter(HistoricoCache.ano_mes.in_(keys_ano)).delete(
                synchronize_session=False
            )
            s.commit()
            log.info("painel_historico_force", ano=ano_alvo, user=user)
        else:
            for row in s.query(HistoricoCache).filter(
                HistoricoCache.ano_mes.in_(keys_ano)
            ):
                cache[row.ano_mes] = row

    now = datetime.now(timezone.utc)
    meses: list[dict[str, Any]] = []
    hits = misses = 0
    for m in range(1, ultimo_mes + 1):
        key = f"{ano_alvo:04d}-{m:02d}"
        row = cache.get(key)
        if row is not None and _historico_cache_valido(key, row.computed_at, now):
            meses.append(row.payload)
            hits += 1
            continue
        payload = _historico_mes_payload(ano_alvo, m)
        misses += 1
        with SessionLocal() as s:
            s.merge(HistoricoCache(ano_mes=key, payload=payload, computed_at=now))
            s.commit()
        meses.append(payload)
    log.info("painel_historico_cache", ano=ano_alvo, hits=hits, misses=misses,
             force=bool(force))

    # Totais ano (acumulado)
    total_gasto = sum(m["meta_ads"]["spend"] or 0 for m in meses)
    total_leads_meta = sum(m["meta_ads"]["leads_meta"] or 0 for m in meses)
    total_vgv = sum(m["equipe"]["vgv"] or 0 for m in meses)
    total_vendas = sum(m["equipe"]["vendas"] or 0 for m in meses)
    total_leads = sum(m["equipe"]["leads"] or 0 for m in meses)
    # Atendidos: a COLUNA da tabela mostra atendidos_evolution (conversa 2-via
    # real); o total precisa somar a MESMA métrica, senão diverge da própria
    # tela (era 678 no total vs 856 somando a coluna). O `or 0` envolve TODO o
    # fallback de propósito: mês com evolution=0 e atendidos=None estouraria.
    total_atend = sum(
        (m["equipe"].get("atendidos_evolution") or m["equipe"]["atendidos"]) or 0
        for m in meses
    )
    total_atend_kommo = sum(m["equipe"]["atendidos"] or 0 for m in meses)
    total_agend = sum(m["equipe"]["agendados"] or 0 for m in meses)
    total_visi = sum(m["equipe"]["visitas"] or 0 for m in meses)

    return {
        "user": user,
        "ano": ano_alvo,
        "meses": meses,
        "totais": {
            "spend": round(total_gasto, 2),
            "leads_meta": total_leads_meta,
            "cpl": round(total_gasto / total_leads_meta, 2) if total_leads_meta else None,
            "leads": total_leads,
            "atendidos": total_atend,
            # Reconciliação: base Kommo (movimento de etapa) do mesmo período.
            # `atendidos` acima é a conversa real do WhatsApp — divergir dos
            # dois é esperado, ver comentário no cálculo.
            "atendidos_kommo": total_atend_kommo,
            "agendados": total_agend,
            "visitas": total_visi,
            "vendas": total_vendas,
            "vgv": round(total_vgv, 2),
            "ticket_medio": round(total_vgv / total_vendas, 2) if total_vendas else None,
            "roi": round(total_vgv / total_gasto, 1) if total_gasto else None,
        },
    }


@router.get("/alertas")
def painel_alertas(
    periodo: str = Query("mes_atual"),
    inicio: str | None = Query(None),
    fim: str | None = Query(None),
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    user = _identificar_usuario(x_painel_user)
    di, df, label = parse_periodo(periodo, inicio, fim)
    funil = calcular_funil(di, df)
    meta = calcular_metricas_meta_ads(di, df)
    visitas = listar_visitas(di, df)
    alertas = detectar_alertas(funil["equipe"], meta, visitas, funil["corretoras"])
    return {
        "user": user,
        "periodo": {"tipo": periodo, "inicio": di.isoformat(),
                    "fim": df.isoformat(), "label": label},
        "alertas": alertas,
    }


# ── Conversa (thread WhatsApp do lead via Evolution) ───────────────────────────
#
# Fatia vertical do "Sistema Imobiliária": clicar no card do lead abre a conversa
# real e responde PELO NÚMERO DA CORRETORA RESPONSÁVEL. O roteamento é decidido
# pelo campo corretora do lead → corretoras.evolution_instance. Leitura reusa
# EvolutionTool.get_conversation (já normaliza + resolve fallback @lid).


def _tel_conversa_contexto(telefone_digits: str) -> dict[str, Any]:
    """Contexto de conversa SEM lead — contato "fora do funil" (chat V2).

    Resolve a corretora dona pela mensagem mais recente daquele telefone em
    mensagens_whatsapp (corretora_id → corretoras.evolution_instance). O
    nome vem do contato_nome mais recente não-nulo. Mesmo shape do contexto
    de lead pra reusar painel_conversa/enviar sem bifurcação.
    """
    p8 = telefone_digits[-8:]
    with SessionLocal() as s:
        row = s.execute(
            sql_text(
                """
                SELECT me.contato_telefone AS telefone,
                       me.corretora_id     AS corretora_id,
                       c.nome              AS corretora_nome,
                       c.evolution_instance AS instance,
                       c.ativo             AS corretora_ativa,
                       (SELECT contato_nome FROM mensagens_whatsapp
                         WHERE right(regexp_replace(contato_telefone, '\\D', '', 'g'), 8) = :p8
                           AND contato_nome IS NOT NULL
                         ORDER BY timestamp_evolution DESC LIMIT 1) AS lead_nome
                FROM mensagens_whatsapp me
                LEFT JOIN corretoras c ON c.id = me.corretora_id
                WHERE right(regexp_replace(me.contato_telefone, '\\D', '', 'g'), 8) = :p8
                  AND me.corretora_id IS NOT NULL
                ORDER BY me.timestamp_evolution DESC
                LIMIT 1
                """
            ),
            {"p8": p8},
        ).mappings().first()
    if row is None:
        raise HTTPException(404, "conversa_nao_encontrada")
    ctx = dict(row)
    ctx.update({
        "lead_id": f"tel:{telefone_digits}",
        "origem": None, "criado_em_kommo": None, "etapa_kommo": None,
        "lead_nome": ctx.get("lead_nome") or ctx.get("telefone"),
    })
    if not ctx.get("corretora_id"):
        raise HTTPException(422, "lead_sem_corretora")
    return ctx


def _linha_pelo_historico(telefone: str | None) -> dict[str, Any] | None:
    """Qual linha (corretora) realmente conversou com este telefone.

    Usada quando o LEAD não tem dono. É o caso do funil de remarketing: a
    nutrição é disparada pela casa, o card nasce sem `owner_id` e só ganha dona
    quando alguém adota. Sem este fallback o chat respondia `lead_sem_corretora`
    e o front traduzia pra "sem conversa ainda" — ou seja, o lead respondia, a
    resposta estava gravada, e a corretora via uma tela vazia.

    Devolve a linha da mensagem mais recente, ou None se não há histórico.
    """
    digits = "".join(ch for ch in (telefone or "") if ch.isdigit())
    if len(digits) < 8:
        return None
    with SessionLocal() as s:
        row = s.execute(
            sql_text(
                """
                SELECT me.corretora_id      AS corretora_id,
                       c.nome               AS corretora_nome,
                       c.evolution_instance AS instance,
                       c.ativo              AS corretora_ativa
                FROM mensagens_whatsapp me
                JOIN corretoras c ON c.id = me.corretora_id
                WHERE right(regexp_replace(me.contato_telefone, '\\D', '', 'g'), 8) = :p8
                  AND me.corretora_id IS NOT NULL
                ORDER BY me.timestamp_evolution DESC
                LIMIT 1
                """
            ),
            {"p8": digits[-8:]},
        ).mappings().first()
    return dict(row) if row else None


def _contexto_lead_nativo(lead_id: str) -> dict[str, Any] | None:
    """Contexto de lead que vive SÓ na espinha (`leads`), sem espelho do Kommo.

    Mesmas chaves que a query do snapshot devolve, pra o resto do endpoint não
    saber a diferença. Aceita o id numérico da espinha OU o external_ref (o
    front manda `lead_id` do card, que pra lead nativo é o id da espinha).

    🔴 01/08: havia um `if not lead_id.isdigit(): return None` aqui que recusava
    qualquer ref não-numérico. O carrossel cria lead com ref `corretor:<telefone>`
    (lead Lúcio, o primeiro a responder) — a Dona clicava no card e tomava
    "lead_nao_encontrado". Agora só o casamento por ID exige dígito; o
    external_ref pode ser qualquer string, que é o que os motores novos geram.
    """
    _ref = str(lead_id)
    with SessionLocal() as s:
        row = s.execute(
            sql_text(
                """
                SELECT
                    coalesce(l.external_ref, l.id::text) AS lead_id,
                    l.nome                AS lead_nome,
                    l.telefone            AS telefone,
                    l.origem              AS origem,
                    l.created_at          AS criado_em_kommo,
                    NULL::text            AS etapa_kommo,
                    l.owner_id            AS corretora_id,
                    c.nome                AS corretora_nome,
                    c.evolution_instance  AS instance,
                    c.ativo               AS corretora_ativa
                FROM leads l
                LEFT JOIN corretoras c ON c.id = l.owner_id
                 -- aceita o id da espinha OU o external_ref: o card do board
                 -- manda `external_ref` quando existe (é o que o Chat recebe na
                 -- URL), e o id quando o lead é 100% nativo. Sem os dois, lead do
                 -- intake próprio abria como "lead não encontrado".
                 WHERE l.tenant_id = 1
                   AND (l.external_ref = :ref OR l.id = :lid)
                 ORDER BY (l.external_ref = :ref) DESC, l.id ASC
                 LIMIT 1
                """
            ),
            {"ref": _ref, "lid": int(_ref) if _ref.isdigit() and len(_ref) < 10 else 0},
        ).mappings().first()
    return dict(row) if row else None


def _lead_conversa_contexto(
    lead_id: str, viewer_cid: int | None = None
) -> dict[str, Any]:
    """Resolve telefone + corretora (id/nome/instância) de um lead.

    Une leads_snapshot (telefone/etapa) com a corretora dona via
    lead_analyses (corretora_id mais recente) e a tabela corretoras
    (evolution_instance). Levanta HTTPException com motivo claro.

    lead_id "tel:<dígitos>" = contato fora do funil (sem lead) — resolve
    direto pelo histórico de mensagens (chat V2, coluna Fora do funil).
    """
    if lead_id.startswith("tel:"):
        digits = "".join(ch for ch in lead_id[4:] if ch.isdigit())
        if len(digits) < 10:
            raise HTTPException(422, "telefone_invalido")
        return _tel_conversa_contexto(digits)
    # Dono canônico (espinha normalizada → snapshot). O JOIN de `corretoras`
    # usa a MESMA expressão do gate — senão o 403 libera a dona nova mas
    # `instance`/`corretora_nome` resolvem pra corretora antiga e o chat lê a
    # linha WhatsApp ERRADA.
    dono_expr = _dono_canonico_sql()
    with SessionLocal() as s:
        row = s.execute(
            sql_text(
                f"""
                SELECT
                    ls.lead_id,
                    ls.nome                              AS lead_nome,
                    ls.telefone                          AS telefone,
                    ls.origem                            AS origem,
                    ls.criado_em_kommo                   AS criado_em_kommo,
                    ls.etapa_kommo                       AS etapa_kommo,
                    {dono_expr}                          AS corretora_id,
                    c.nome                               AS corretora_nome,
                    c.evolution_instance                 AS instance,
                    c.ativo                              AS corretora_ativa
                FROM leads_snapshot ls
                {_join_dono_sql()}
                LEFT JOIN corretoras c ON c.id = {dono_expr}
                WHERE ls.lead_id = :lid
                """
            ),
            {"lid": lead_id},
        ).mappings().first()

    if row is None:
        # Lead NATIVO (criado pelo intake próprio da Meta ou à mão no painel):
        # não tem espelho em leads_snapshot, que só existe pra lead do Kommo.
        # Sem este fallback, conversa/reengajar/editar contato respondem
        # "lead_nao_encontrado" — e depois do corte do Kommo TODO lead é nativo,
        # ou seja, o painel pararia inteiro. Aceita o id da espinha.
        row = _contexto_lead_nativo(lead_id)

    if row is None:
        raise HTTPException(404, "lead_nao_encontrado")
    ctx = dict(row)
    if not ctx.get("telefone"):
        raise HTTPException(422, "lead_sem_telefone")
    # Lead sem dono ainda TEM conversa: no remarketing a nutrição sai pela linha
    # da casa e o card só ganha dona quando alguém adota. Cair fora aqui fazia o
    # chat dizer "sem conversa ainda" pra quem tinha acabado de responder.
    if not ctx.get("corretora_id"):
        linha = _linha_pelo_historico(ctx.get("telefone"))
        if linha:
            ctx.update(linha)
            ctx["sem_dono"] = True  # o front mostra "lead sem dona" no cabeçalho
    # Lead sem dona e SEM conversa nenhuma — típico da fila de ligação, onde o
    # lead do remarketing nunca recebeu mensagem. Não há histórico de onde
    # deduzir a linha, e recusar aqui deixava a corretora sem conseguir ABRIR o
    # contato que ela acabou de ligar. Quem está olhando é quem vai falar:
    # usa a linha dela. Só vale pra lead SEM DONA — lead de outra corretora
    # continua barrado pelo gate de escopo dos endpoints.
    if not ctx.get("corretora_id") and viewer_cid is not None:
        with SessionLocal() as s:
            linha = s.execute(
                sql_text(
                    "SELECT id AS corretora_id, nome AS corretora_nome, "
                    "       evolution_instance AS instance, ativo AS corretora_ativa "
                    "  FROM corretoras WHERE id = :cid"
                ),
                {"cid": viewer_cid},
            ).mappings().first()
        if linha:
            ctx.update(dict(linha))
            ctx["sem_dono"] = True
            ctx["sem_conversa"] = True  # thread vazia: ela vai INICIAR o contato
    if not ctx.get("corretora_id"):
        raise HTTPException(422, "lead_sem_corretora")
    if not ctx.get("instance"):
        raise HTTPException(422, "corretora_sem_instancia_evolution")
    return ctx


def _status_janela_24h(
    mensagens: list[dict[str, Any]],
) -> tuple[bool, str | None]:
    """Deriva (janela_aberta, ultima_entrada_at_iso) da thread.

    janela_aberta = existe mensagem RECEBIDA do lead (from == "lead") com `at`
    < 24h atrás. Sem inbound → janela fechada (False, None). Datas naive são
    tratadas como UTC. Retorna o ISO da última entrada mesmo se a janela estiver
    fechada (o front usa pra dizer "não responde há X").
    """
    ultima: datetime | None = None
    for m in mensagens:
        if m.get("from") != "lead":
            continue
        at = m.get("at")
        if not at:
            continue
        try:
            dt = datetime.fromisoformat(at)
        except (ValueError, TypeError):
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if ultima is None or dt > ultima:
            ultima = dt
    if ultima is None:
        return False, None
    aberta = (datetime.now(timezone.utc) - ultima) < timedelta(hours=24)
    return aberta, ultima.isoformat()


@router.get("/conversa")
def painel_conversa(
    lead_id: str = Query(..., description="lead_id do Kommo (leads_snapshot)"),
    hours_back: int = Query(720, ge=1, le=4320, description="janela em horas (default 30d)"),
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Thread WhatsApp do lead, lida pela linha Evolution da corretora dona.

    Privacidade: corretora só lê leads dela; admin (dona/pessoa/assistente) lê todos.
    Read-only — não envia nada.
    """
    user = _identificar_usuario(x_painel_user)
    viewer = _viewer_info(user)
    my_cid = viewer["corretora_id"]

    try:
        ctx = _lead_conversa_contexto(lead_id, viewer_cid=my_cid)
    except HTTPException as e:
        # Lead sem dona, sem histórico e quem olha é ADMIN: não dá pra escolher
        # a linha (admin não tem uma), mas a tela é só leitura — devolver 422
        # deixava a gestora batendo num lead que existe, com o front retentando
        # em loop. Abre vazia; ENVIAR segue barrado, que é o que exige linha.
        if e.detail != "lead_sem_corretora" or my_cid is not None:
            raise
        vazio = _contexto_lead_nativo(lead_id) or {}
        if not vazio.get("telefone"):
            raise
        return {
            "lead_id": vazio.get("lead_id") or lead_id,
            "lead_nome": vazio.get("lead_nome"),
            "corretora_id": None,
            "corretora_nome": None,
            "telefone": vazio.get("telefone"),
            "origem": vazio.get("origem"),
            "criado_em_kommo": (
                vazio["criado_em_kommo"].isoformat()
                if vazio.get("criado_em_kommo") else None
            ),
            "etapa_kommo": vazio.get("etapa_kommo"),
            "linha_atual": None,
            "coaching": None,
            "raiox": None,
            "tarefas_ligacao": [],
            "tarefas_lead": [],
            "ultima_ligacao": None,
            "janela_aberta": False,
            "ultima_entrada_at": None,
            "total": 0,
            "mensagens": [],
            "sem_dono": True,
            "sem_conversa": True,
        }

    # Privacidade: corretora nunca vê conversa de lead de outra corretora —
    # EXCETO se o cliente respondeu na linha dela (ver `_respondeu_na_linha`).
    # Nesse caso é leitura emprestada, sinalizada na resposta pro front deixar
    # claro que o card não é dela.
    acesso_emprestado = False
    if not _pode_atender(my_cid, ctx["corretora_id"]):
        if not _respondeu_na_linha(ctx.get("telefone"), my_cid):
            raise HTTPException(403, "lead_de_outra_corretora")
        acesso_emprestado = True

    # Linha Cloud (coexistência: Pessoa12 / Pessoa10 / Pessoa11) grava e lê em
    # mensagens_whatsapp. Evolution/Baileys NÃO vê template enviado via Graph —
    # se a corretora tem linha Cloud, a thread TEM que vir do Postgres, senão a
    # msg "some" do chat (aparece no celular, some no painel — BR-Jeison 28/07).
    # Sem linha Cloud, segue pull no Evolution.
    from app.services import cloud_lines as _cl_thr

    _linha_thr = _cl_thr.linha_da_corretora(ctx["corretora_id"])
    # `acesso_emprestado`: a conversa está na linha de QUEM OLHA, não na da
    # dona — puxar do Evolution da dona devolveria thread vazia justamente pra
    # quem tem o cliente na mão. Postgres tem as duas linhas.
    usa_cloud_thread = (
        _linha_thr is not None
        and _linha_thr is not _cl_thr.INDETERMINADO
    ) or ctx["instance"] == "acme_corretora_a" or acesso_emprestado

    if usa_cloud_thread:
        from app.api.cloud_corretora_a import ler_thread_cloud
        mensagens = ler_thread_cloud(
            ctx["telefone"],
            hours_back=hours_back,
            corretora_id=ctx["corretora_id"],
        )
    else:
        # Import local: EvolutionTool puxa cliente HTTP — evita custo no import do módulo.
        from app.tools.evolution import EvolutionTool

        tool = EvolutionTool()
        try:
            mensagens = tool.get_conversation(
                instance=ctx["instance"],
                lead_phone=ctx["telefone"],
                hours_back=hours_back,
            )
        except Exception as e:  # noqa: BLE001 — degradação graciosa no painel
            log.warning(
                "painel_conversa_falha",
                lead_id=lead_id,
                instance=ctx["instance"],
                erro=str(e)[:160],
            )
            raise HTTPException(502, "evolution_indisponivel") from e

    # Enriquecimento: a transcrição dos áudios já vive no Postgres
    # (mensagens_whatsapp.transcricao, gerada pelo Whisper). Anexa por
    # evolution_message_id == key.id — funciona até pra áudio cuja mídia
    # já expirou no WhatsApp.
    ids = [m.get("id") for m in mensagens if m.get("id") and m.get("type") == "audio"]
    if ids:
        with SessionLocal() as s:
            rows = s.execute(
                sql_text(
                    """
                    SELECT evolution_message_id, transcricao,
                           conteudo_metadata->>'duracao_segundos' AS duracao
                    FROM mensagens_whatsapp
                    WHERE evolution_message_id = ANY(:ids)
                      AND (transcricao IS NOT NULL
                           OR conteudo_metadata ? 'duracao_segundos')
                    """
                ),
                {"ids": ids},
            ).mappings().all()
        transcr = {r["evolution_message_id"]: r["transcricao"] for r in rows if r["transcricao"]}
        # Duração do áudio (pacote A do Chat): o player mostra antes de tocar.
        # Só existe quando a linha manda (Evolution grava `seconds`); a Cloud não
        # manda, e aí o player lê do próprio arquivo.
        duracoes: dict[str, int] = {}
        for r in rows:
            try:
                if r["duracao"] is not None and int(float(r["duracao"])) > 0:
                    duracoes[r["evolution_message_id"]] = int(float(r["duracao"]))
            except (TypeError, ValueError):
                pass
        for m in mensagens:
            if m.get("type") == "audio" and m.get("id") in transcr:
                m["transcricao"] = transcr[m["id"]]
            if m.get("type") == "audio" and m.get("id") in duracoes:
                m["duracao"] = duracoes[m["id"]]

    # Status de entrega (sent/delivered/read/failed) das mensagens ENVIADAS —
    # vive no conteudo_metadata (populado pelo webhook /cloud/status da Meta na
    # linha Cloud da Pessoa12). Anexa `delivery_status` por evolution_message_id quando
    # existir. Linhas Evolution normais simplesmente não têm o campo.
    ids_out = [
        m.get("id") for m in mensagens if m.get("id") and m.get("from") == "corretora"
    ]
    if ids_out:
        with SessionLocal() as s:
            rows = s.execute(
                sql_text(
                    """
                    SELECT evolution_message_id,
                           conteudo_metadata->>'delivery_status' AS delivery_status,
                           conteudo_metadata->>'delivery_error' AS delivery_error
                    FROM mensagens_whatsapp
                    WHERE evolution_message_id = ANY(:ids)
                      AND conteudo_metadata ? 'delivery_status'
                    """
                ),
                {"ids": ids_out},
            ).mappings().all()
        deliv = {r["evolution_message_id"]: r for r in rows}
        for m in mensagens:
            if m.get("id") in deliv:
                m["delivery_status"] = deliv[m["id"]]["delivery_status"]
                if deliv[m["id"]]["delivery_error"]:
                    m["delivery_error"] = deliv[m["id"]]["delivery_error"]

    # Tag de linha (best-effort): por qual TELEFONE/LINHA cada trecho rolou.
    # Motivo (leva 2 do Chat, pedido Dona): lead transferido de corretora leva
    # o histórico junto — quem recebe precisa saber por onde foi cada trecho
    # pra disparar pela PRÓPRIA linha. Fonte: mensagens_whatsapp.instance (o
    # webhook grava a instância que capturou cada msg; o caminho Cloud da Pessoa12
    # grava instance='acme_corretora_a' também — mesma linha, transporte
    # diferente). Msg sem match no banco herda a linha da instância consultada.
    linha_atual = _linha_da_instance(ctx["instance"])
    ids_linha = [m.get("id") for m in mensagens if m.get("id")]
    linhas_msg = _linhas_por_msg_ids(ids_linha) if ids_linha else {}
    for m in mensagens:
        m["linha"] = linhas_msg.get(m.get("id")) or linha_atual

    # Coaching curado do Inspector: categoria + trecho que a corretora disse +
    # observação gentil + sugestão de resposta pronta. None se o lead não tem
    # análise (coaching_observations.lead_id é BIGINT → cast p/ casar com TEXT).
    with SessionLocal() as s:
        co = s.execute(
            sql_text(
                """
                SELECT categoria, trecho_corretora, observacao, sugestao, analyzed_at
                FROM coaching_observations
                WHERE lead_id::text = :lid
                ORDER BY analyzed_at DESC
                LIMIT 1
                """
            ),
            {"lid": str(lead_id)},
        ).mappings().first()
    coaching = {
        "categoria": co["categoria"],
        "trecho": co["trecho_corretora"],
        "observacao": co["observacao"],
        "sugestao": co["sugestao"],
    } if co else None

    # Raio-X do Copiloto (Lead Inspector): última análise do lead em lead_analyses.
    with SessionLocal() as s:
        la = s.execute(
            sql_text(
                """
                SELECT temperatura, status, raw_analysis, analyzed_at
                FROM lead_analyses
                WHERE lead_id::text = :lid
                ORDER BY analyzed_at DESC
                LIMIT 1
                """
            ),
            {"lid": str(lead_id)},
        ).mappings().first()
    raiox = None
    if la:
        ra = la["raw_analysis"] or {}
        raiox = {
            "temperatura": la["temperatura"] or ra.get("temperatura"),
            "status": la["status"] or ra.get("status"),
            "resumo": ra.get("conversa_resumo"),
            "gap": ra.get("gap_identificado"),
            "abordagem": ra.get("abordagem_sugerida"),
            "tarefas_atrasadas": ra.get("tarefas_kommo_atrasadas") or [],
            "etapa_desatualizada": ra.get("etapa_kommo_desatualizada"),
            "analyzed_at": la["analyzed_at"].isoformat() if la["analyzed_at"] else None,
        }

    # Janela de 24h (regra WhatsApp): fora da janela, texto livre NÃO entrega —
    # a Cloud API só aceita template (erro "Re-engagement message"). Derivamos
    # da ÚLTIMA mensagem RECEBIDA do lead (from == "lead"). Se não houver inbound,
    # janela fechada. O front usa isso pra BLOQUEAR o texto livre e oferecer só
    # reengajamento (template UTILITY) ou abrir no WhatsApp do celular.
    janela_aberta, ultima_entrada_at = _status_janela_24h(mensagens)

    # ── Tarefa(s) de LIGAÇÃO da cadência devidas pra ESTE lead (sistema novo) ──
    # É a origem CERTA do alerta "Ligar — D1/D5/D9 do follow-up". O bloco
    # raiox.tarefas_atrasadas ao lado vem do KOMMO (legado, a largar). Aqui a Pessoa12
    # vê a tarefa da NOSSA cadência e pode marcar "✓ liguei".
    tarefas_ligacao = _tarefas_ligacao_cadencia(
        my_cid,
        is_admin=(my_cid is None),
        lead_id=ctx.get("lead_id"),
    )

    # ── Tarefas MANUAIS abertas DESTE lead (tarefas_corretora) ──
    # A corretora cria tarefa manual pro cliente (aba /tarefas OU modal do chat);
    # aqui devolvemos as ABERTAS deste lead pra aparecerem/editarem/concluírem
    # dentro do chat. Casamento: tarefas_corretora.lead_id == lead_id do Kommo
    # == ctx["lead_id"] (mesma chave que o POST de criação grava). Privacidade já
    # garantida acima (my_cid vs ctx["corretora_id"]).
    # ── Última LIGAÇÃO registrada deste lead ──
    # Sem isso, quem abre a conversa vê um vão entre mensagens e não sabe que
    # houve ligação — e quem sugere a próxima ação segue mandando "tenta entrar
    # em contato" pra quem já foi ligado hoje. A nota também entra em
    # `observacao_corretora` (thread + campo `observacao` das oportunidades);
    # aqui é o campo direto do raio-X.
    from app.services.ligacao_resultado import ultima_ligacao_do_lead

    hoje_sp = _hoje_sp()
    with SessionLocal() as s:
        ultima_ligacao = ultima_ligacao_do_lead(s, lead_id=str(ctx["lead_id"]))
        rows_tl = s.execute(
            sql_text(
                "SELECT id, data_tarefa, hora_tarefa, texto, anotacoes, "
                "       mensagem_planejada "
                "FROM tarefas_corretora "
                "WHERE status = 'aberta' AND lead_id = :lid "
                "ORDER BY data_tarefa ASC, hora_tarefa ASC NULLS LAST, id ASC"
            ),
            {"lid": str(ctx["lead_id"])},
        ).mappings().all()
    tarefas_lead = [
        {
            "id": int(r["id"]),
            "texto": r["texto"],
            "data": r["data_tarefa"].isoformat(),
            "hora": r["hora_tarefa"].strftime("%H:%M") if r["hora_tarefa"] else None,
            "atrasada": r["data_tarefa"] < hoje_sp,
            "anotacoes": r["anotacoes"],
            "mensagem_planejada": r["mensagem_planejada"],
            "tem_msg_planejada": bool(r["mensagem_planejada"]),
            "tipo": "tarefa",  # manual — editável/concluível
        }
        for r in rows_tl
    ]

    rmkt_em = _marca_rmkt(ctx.get("lead_id"))

    return {
        "lead_id": ctx["lead_id"],
        "lead_nome": ctx["lead_nome"],
        "corretora_id": ctx["corretora_id"],
        "corretora_nome": ctx["corretora_nome"],
        "telefone": ctx["telefone"],
        "origem": ctx.get("origem"),
        "criado_em_kommo": (
            ctx["criado_em_kommo"].isoformat() if ctx.get("criado_em_kommo") else None
        ),
        "etapa_kommo": ctx.get("etapa_kommo"),
        "linha_atual": linha_atual,  # linha da corretora dona (label curto)
        "coaching": coaching,
        "raiox": raiox,
        "tarefas_ligacao": tarefas_ligacao,  # cadência (nosso sistema); [] se nenhuma devida
        "tarefas_lead": tarefas_lead,  # manuais abertas deste lead (tarefas_corretora)
        "ultima_ligacao": ultima_ligacao,  # None se nunca ligaram (ou nunca registraram)
        "janela_aberta": janela_aberta,
        "ultima_entrada_at": ultima_entrada_at,
        "total": len(mensagens),
        "mensagens": mensagens,  # [{from, text, at, type, id, transcricao?}]
        # Leitura emprestada (o lead respondeu na MINHA linha, mas o card é de
        # outra): o front avisa em vez de deixar parecer card dela. None pra
        # dona e pra admin — nada muda pra quem já podia.
        "acesso": "leitura_por_resposta" if acesso_emprestado else None,
        "dono_outra_corretora": ctx["corretora_nome"] if acesso_emprestado else None,
        # Tag RMKT no card aberto: este lead veio do remarketing (e desde
        # quando). A conversa da linha da Corretor vem junto na thread acima.
        "tag_remarketing": _TAG_RMKT if rmkt_em else None,
        "veio_remarketing_em": rmkt_em,
        # Resumo por IA do que a pessoa disse na linha da Corretor, se houver.
        "resumo_ia": _resumo_ia_do_lead(ctx.get("lead_id")),
    }


# Nome da tag de quem veio do remarketing — o mesmo que o handoff grava no
# evento da transferência (`quando_responde.TAG`).
_TAG_RMKT = "RMKT"


def _resumo_ia_do_lead(lead_ref: Any) -> dict[str, Any] | None:
    """O último `resumo_ia` do card (external_ref ou id da espinha), ou None.
    Nunca levanta: é informação, não pode derrubar a conversa."""
    ref = str(lead_ref or "")
    if not ref:
        return None
    try:
        from app.services import ia_do_lado

        with SessionLocal() as s:
            lid = s.execute(
                sql_text(
                    "SELECT id FROM leads "
                    " WHERE tenant_id = 1 AND (external_ref = :ref OR id::text = :ref) "
                    " ORDER BY (external_ref = :ref) DESC, id LIMIT 1"
                ),
                {"ref": ref},
            ).scalar()
            if lid is None:
                return None
            return ia_do_lado.resumos_por_lead(s, [int(lid)]).get(int(lid))
    except Exception as e:  # noqa: BLE001
        log.warning("painel_conversa_resumo_ia_indisponivel", erro=str(e)[:120])
        return None


def _marca_rmkt(lead_ref: Any) -> str | None:
    """ISO de quando o lead ganhou a tag RMKT (`leads.veio_remarketing_em`),
    ou None. Aceita o external_ref ou o id da espinha, como o resto do Chat.
    Nunca levanta: a tag é informação, não pode derrubar a conversa."""
    ref = str(lead_ref or "")
    if not ref:
        return None
    try:
        with SessionLocal() as s:
            em = s.execute(
                sql_text(
                    "SELECT veio_remarketing_em FROM leads "
                    " WHERE tenant_id = 1 AND (external_ref = :ref OR id::text = :ref) "
                    " ORDER BY (external_ref = :ref) DESC, id LIMIT 1"
                ),
                {"ref": ref},
            ).scalar()
        return _iso_utc(em) if em else None
    except Exception as e:  # noqa: BLE001
        log.warning("painel_conversa_tag_rmkt_indisponivel", erro=str(e)[:120])
        return None


MEDIA_DIR = "/var/lib/copiloto-media"


def _id_da_resposta_de_envio(resp: dict[str, Any] | None) -> str | None:
    """Id da mensagem recém-enviada, nos dois formatos de resposta.

    Cloud API: {"messages": [{"id": "wamid..."}]}.
    Evolution: {"key": {"id": "3EB0..."}}.
    """
    if not isinstance(resp, dict):
        return None
    msgs = resp.get("messages")
    if isinstance(msgs, list) and msgs and isinstance(msgs[0], dict):
        mid = msgs[0].get("id")
        if mid:
            return str(mid)
    key = resp.get("key")
    if isinstance(key, dict) and key.get("id"):
        return str(key["id"])
    return None


def _cachear_midia_enviada(
    resp: dict[str, Any] | None, conteudo: bytes, mimetype: str | None
) -> None:
    """Guarda no cache em disco a mídia que a corretora acabou de enviar.

    Best-effort puro: falhar aqui NÃO pode derrubar um envio que já aconteceu —
    a mensagem foi entregue, o cache é conforto pra reabrir depois.
    """
    msg_id = _id_da_resposta_de_envio(resp)
    if not msg_id:
        log.info("midia_cache_sem_id", motivo="resposta_sem_id_de_mensagem")
        return
    safe_id = re.sub(r"[^A-Za-z0-9]", "", msg_id)[:80]
    if not safe_id:
        return
    try:
        os.makedirs(MEDIA_DIR, exist_ok=True)
        with open(os.path.join(MEDIA_DIR, safe_id), "wb") as f:
            f.write(conteudo)
        with open(os.path.join(MEDIA_DIR, safe_id + ".mime"), "w") as f:
            f.write(mimetype or "application/octet-stream")
        log.info("midia_cache_gravada", msg_id=msg_id[:24], bytes=len(conteudo))
    except Exception as e:  # noqa: BLE001
        log.warning("midia_cache_falhou", erro=str(e)[:140])


def _audio_tocavel_no_iphone(
    content: bytes, mimetype: str, safe_id: str
) -> tuple[bytes, str]:
    """Converte áudio Ogg/Opus pra AAC quando preciso — o iPhone não toca Ogg.

    O WhatsApp entrega áudio em `audio/ogg; codecs=opus`. O Safari do iOS não
    reproduz esse container: o servidor responde 200, o arquivo é válido, e a
    corretora só vê um player que não anda. Como a equipe da Acme trabalha no
    iPhone, na prática TODO áudio estava mudo pra elas.

    Converte uma vez e guarda o resultado ao lado do original (`.m4a`), então o
    custo é só na primeira abertura de cada áudio (~0,3s).

    Fail-open: ffmpeg ausente, lento ou com erro → devolve o original. Um áudio
    que talvez não toque é melhor que erro na tela.
    """
    if "ogg" not in (mimetype or "").lower() and "opus" not in (mimetype or "").lower():
        return content, mimetype
    if not safe_id:
        return content, mimetype

    destino = os.path.join(MEDIA_DIR, safe_id + ".m4a")
    if os.path.exists(destino):
        try:
            with open(destino, "rb") as f:
                return f.read(), "audio/mp4"
        except OSError:
            pass

    import subprocess
    import tempfile

    try:
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp.write(content)
            origem = tmp.name
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-i", origem,
                 "-c:a", "aac", "-b:a", "64k", destino],
                check=True, timeout=25, capture_output=True,
            )
            with open(destino, "rb") as f:
                convertido = f.read()
        finally:
            try:
                os.unlink(origem)
            except OSError:
                pass
    except Exception as e:  # noqa: BLE001 — sem conversão, serve o original
        log.warning("audio_conversao_falhou", erro=str(e)[:140])
        return content, mimetype

    log.info("audio_convertido_para_aac", msg=safe_id[:20], bytes=len(convertido))
    return convertido, "audio/mp4"


def _nome_arquivo_midia(raw_payload: dict[str, Any] | None) -> str | None:
    """Nome original do arquivo de uma mensagem de mídia, nos DOIS formatos.

    - Cloud API: `{tipo}.filename` (ex: raw["document"]["filename"]).
    - Evolution/Baileys: `data.message.documentMessage.fileName` (ou `title`).

    Sem nome (áudio, imagem sem nome) → None; o caller decide o fallback.
    """
    raw = raw_payload or {}

    tipo = raw.get("type")
    if tipo:
        obj = raw.get(tipo)
        if isinstance(obj, dict):
            nome = obj.get("filename") or obj.get("file_name")
            if nome:
                return str(nome)

    msg = ((raw.get("data") or {}).get("message")) or raw.get("message") or {}
    if isinstance(msg, dict):
        for chave in ("documentMessage", "documentWithCaptionMessage", "videoMessage", "imageMessage"):
            obj = msg.get(chave)
            if isinstance(obj, dict):
                nome = obj.get("fileName") or obj.get("title")
                if nome:
                    return str(nome)
    return None


def _cabecalhos_midia(nome_arquivo: str | None) -> dict[str, str]:
    """Headers da resposta de mídia — cache privado + nome original do arquivo.

    `inline` pra PDF/imagem abrirem na aba em vez de baixar direto; o nome vai
    junto pra que "salvar como" sugira o nome certo (contrato assinado etc).
    Aspas e caracteres de controle saem do nome — senão quebram o header.
    """
    headers = {"Cache-Control": "private, max-age=604800"}
    if nome_arquivo:
        limpo = re.sub(r'[\r\n"\\]', "", nome_arquivo)[:120].strip()
        if limpo:
            # ASCII puro no filename= (compat) + UTF-8 no filename*= (RFC 5987)
            ascii_nome = limpo.encode("ascii", "ignore").decode() or "arquivo"
            headers["Content-Disposition"] = (
                f'inline; filename="{ascii_nome}"; '
                f"filename*=UTF-8''{quote(limpo)}"
            )
    return headers


@router.get("/conversa/midia")
def painel_conversa_midia(
    lead_id: str = Query(...),
    msg_id: str = Query(..., description="key.id da mensagem de mídia"),
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> Response:
    """Baixa a mídia (áudio, imagem, vídeo ou documento) de uma mensagem.

    Devolve o binário com o mimetype certo — o front usa no <audio>, no <img>,
    no <video> e no link de documento (PDF do contrato, HEIC, planilha…).
    Privacidade: corretora só acessa mídia de lead dela.
    """
    import base64 as _b64
    import os
    import re

    user = _identificar_usuario(x_painel_user)
    my_cid = _viewer_info(user)["corretora_id"]
    ctx = _lead_conversa_contexto(lead_id)
    # Mesma leitura emprestada do /conversa: sem a mídia, a thread liberada pra
    # quem disparou fica com áudio e foto quebrados no meio da conversa.
    if (
        not _pode_atender(my_cid, ctx["corretora_id"])
        and not _respondeu_na_linha(ctx.get("telefone"), my_cid)
    ):
        raise HTTPException(403, "lead_de_outra_corretora")

    # Cache em disco: baixa do WhatsApp UMA vez e guarda pra sempre. Resolve a
    # expiração da URL do WhatsApp — depois de salvo, toca direto do nosso disco.
    media_dir = "/var/lib/copiloto-media"
    safe_id = re.sub(r"[^A-Za-z0-9]", "", msg_id)[:80]
    bin_path = os.path.join(media_dir, safe_id)
    mime_path = bin_path + ".mime"
    # Marcador de EXPIRADA: mídia que o WhatsApp já não serve não volta nunca.
    # Sem isto, cada abertura da conversa tentava de novo e gastava 5s por
    # arquivo — 382 tentativas fracassadas num dia. Com o marcador, a resposta
    # é imediata e a tela para de travar.
    gone_path = bin_path + ".gone"
    if safe_id and os.path.exists(gone_path):
        raise HTTPException(410, "midia_expirada")

    # Payload da mensagem: dá o nome do arquivo (documento) e diz por ONDE
    # baixar. Busca por evolution_message_id em qualquer instância — o escopo
    # de privacidade já foi resolvido acima, pelo lead.
    with SessionLocal() as s:
        row = s.execute(
            sql_text(
                """
                SELECT raw_payload, conteudo_metadata
                FROM mensagens_whatsapp
                WHERE evolution_message_id = :mid
                ORDER BY timestamp_evolution DESC
                LIMIT 1
                """
            ),
            {"mid": msg_id},
        ).first()
    raw_payload = row[0] if row else None
    meta_msg = row[1] if row else None
    nome_arquivo = _nome_arquivo_midia(raw_payload)
    headers = _cabecalhos_midia(nome_arquivo)

    if safe_id and os.path.exists(bin_path):
        with open(bin_path, "rb") as f:
            content = f.read()
        # Default histórico era audio/ogg (só havia áudio no cache). Documento
        # antigo sem .mime abriria como áudio quebrado — octet-stream faz o
        # browser tratar pelo nome/conteúdo.
        mimetype = "application/octet-stream"
        if os.path.exists(mime_path):
            with open(mime_path, "r") as f:
                mimetype = f.read().strip() or mimetype
        content, mimetype = _audio_tocavel_no_iphone(content, mimetype, safe_id)
        return Response(content=content, media_type=mimetype, headers=headers)

    # Por onde baixar é decidido pela MENSAGEM, não pela corretora: Pessoa10, Pessoa11
    # e Corretor estão em coexistência e hoje recebem mídia pela Cloud API (wamid)
    # mesmo com instância Evolution no cadastro. Roteando pela corretora, todo
    # documento novo delas ia parar no Baileys — que não conhece a mensagem —
    # e a mídia não abria.
    from app.services.transcricao_service import _cloud_media_id_e_mime

    media_id, mime = _cloud_media_id_e_mime(raw_payload, meta_msg)

    if media_id:
        from app.api.cloud_corretora_a import baixar_media_cloud

        content = baixar_media_cloud(media_id)
        if not content:
            raise HTTPException(502, "midia_indisponivel")
        mimetype = mime or "application/octet-stream"
    else:
        from app.integrations.evolution_client import EvolutionClient

        try:
            data = EvolutionClient().get_base64_media(ctx["instance"], msg_id)
        except Exception as e:  # noqa: BLE001
            # MÍDIA EXPIRADA ≠ ERRO NOSSO (12/08). O WhatsApp serve o arquivo por
            # um prazo (o `oe=` na URL é a validade) e depois para. Quando isso
            # acontece a Evolution devolve 400 "Failed to fetch stream" — não há
            # o que tentar de novo, o arquivo não existe mais na origem.
            #
            # Antes tudo virava 502 genérico: o front tratava como falha de
            # servidor, tentava de novo, e cada tentativa gastava 5s. Deu 382
            # falhas num dia só — numa conversa cheia de áudio, a tela trava.
            #
            # 410 GONE é o código certo: "existiu, não existe mais, não insista".
            texto = str(e)
            # A exceção do httpx traz só "400 Bad Request" — o motivo real
            # ("Failed to fetch stream from mmg.whatsapp.net/...?oe=<validade>")
            # fica no CORPO, que não vem na mensagem. Na prática, 400 nesta
            # chamada só acontece quando a origem não serve mais o arquivo:
            # a Evolution valida o id antes, e id inexistente dá 404.
            expirou = (
                "Failed to fetch stream" in texto
                or "400 Bad Request" in texto
                or "410" in texto
            )
            log.info(
                "painel_conversa_midia_expirada" if expirou else "painel_conversa_midia_falha",
                lead_id=lead_id, erro=texto[:140],
            )
            if expirou:
                # Grava o marcador pra não tentar de novo (best-effort).
                try:
                    if safe_id:
                        os.makedirs(media_dir, exist_ok=True)
                        with open(gone_path, "w") as f:
                            f.write(datetime.now(timezone.utc).isoformat())
                except OSError:
                    pass
                raise HTTPException(410, "midia_expirada") from e
            raise HTTPException(502, "midia_indisponivel") from e

        b64 = data.get("base64") or ""
        if not b64:
            raise HTTPException(404, "midia_vazia")
        mimetype = data.get("mimetype") or "application/octet-stream"
        content = _b64.b64decode(b64)

    # Persiste no cache (best-effort — nunca bloqueia a resposta).
    if safe_id:
        try:
            os.makedirs(media_dir, exist_ok=True)
            with open(bin_path, "wb") as f:
                f.write(content)
            with open(mime_path, "w") as f:
                f.write(mimetype)
        except Exception as e:  # noqa: BLE001
            log.warning("painel_midia_cache_falhou", erro=str(e)[:120])

    content, mimetype = _audio_tocavel_no_iphone(content, mimetype, safe_id)
    return Response(content=content, media_type=mimetype, headers=headers)


# Duração do áudio pro player do Chat (pacote A, 16/10/2026). O iPhone ignora
# `preload` e só lê a duração depois do play — o player mostrava `0:00 / 0:00`.
# A linha Evolution grava `seconds` (vai no /conversa como `duracao`); a Cloud
# não manda nada, então medimos o arquivo aqui. Memória do processo: o mesmo
# áudio não é medido duas vezes.
_DURACAO_CACHE: dict[str, float | None] = {}


def _duracao_audio(content: bytes) -> float | None:
    """Segundos do áudio via ffprobe. Fail-open: qualquer erro → None."""
    import subprocess
    import tempfile

    if not content:
        return None
    try:
        with tempfile.NamedTemporaryFile(suffix=".audio") as tmp:
            tmp.write(content)
            tmp.flush()
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", tmp.name],
                check=True, timeout=10, capture_output=True, text=True,
            ).stdout.strip()
        seg = float(out)
        return round(seg, 1) if seg > 0 else None
    except Exception as e:  # noqa: BLE001 — sem duração o player só lê ao tocar
        log.warning("painel_audio_duracao_falhou", erro=str(e)[:120])
        return None


@router.get("/conversa/midia/duracao")
def painel_conversa_midia_duracao(
    lead_id: str = Query(...),
    msg_id: str = Query(..., description="key.id da mensagem de áudio"),
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Duração (segundos) de um áudio da conversa. Só leitura.

    Mesma privacidade e mesmo cache em disco do /conversa/midia (chama ele).
    """
    # sempre passa pelo /conversa/midia: a trava de privacidade vale mesmo
    # quando a duração já está em memória (o arquivo vem do disco, é barato)
    resp = painel_conversa_midia(lead_id=lead_id, msg_id=msg_id, x_painel_user=x_painel_user)
    if msg_id not in _DURACAO_CACHE:
        _DURACAO_CACHE[msg_id] = _duracao_audio(bytes(resp.body))
    return {"msg_id": msg_id, "duracao": _DURACAO_CACHE[msg_id]}


# ── Kanban a partir do Kommo (fonte da verdade) ──────────────────────────────
# O painel lia do Twenty (espelho incompleto/defasado). Aqui lemos o Funil de
# Vendas direto do Kommo: todas as etapas na ORDEM real, todos os leads, contagem
# correta (ex: Venda ganha). Cache 90s pra não bater no Kommo a cada load.

import time as _time  # noqa: E402

_KANBAN_CACHE: dict[str, Any] = {"data": None, "at": 0.0}
_KANBAN_TTL = 90.0
# Nome que o front V2 usa por corretora (cards do chat + filtro do admin).
CID_TO_FRONT = {1: "Pessoa6", 2: "Pessoa1", 3: "Pessoa3", 7: "Pessoa7", 8: "Corretor"}
STATUS_TERMINAIS = {142, 143}  # Venda ganha / Venda perdida


def _kommo_funil_snapshot() -> dict[str, Any]:
    """Lê o Funil de Vendas do Kommo (etapas em ordem + todos os leads). Cache 90s."""
    now = _time.time()
    cached = _KANBAN_CACHE.get("data")
    if cached is not None and now - _KANBAN_CACHE["at"] < _KANBAN_TTL:
        return cached

    from app.config import settings
    from app.integrations.kommo_client import KommoClient

    k = KommoClient(subdomain=settings.kommo_subdomain, token=settings.kommo_token)
    main = settings.kommo_pipeline_id_main

    pipe = k._get(f"/leads/pipelines/{main}")
    statuses = sorted(
        pipe.get("_embedded", {}).get("statuses", []),
        key=lambda s: (s.get("sort", 0), s.get("id", 0)),
    )

    users: dict[int, str] = {}
    try:
        u = k._get("/users?limit=250")
        for usr in u.get("_embedded", {}).get("users", []):
            users[int(usr["id"])] = usr.get("name", "")
    except Exception:  # noqa: BLE001
        pass

    leads: list[dict[str, Any]] = []
    page = 1
    while page <= 40:
        r = k._get(f"/leads?filter[pipeline_id]={main}&limit=250&page={page}")
        batch = r.get("_embedded", {}).get("leads", [])
        if not batch:
            break
        leads.extend(batch)
        if not r.get("_links", {}).get("next"):
            break
        page += 1

    data = {"statuses": statuses, "leads": leads, "users": users}
    _KANBAN_CACHE["data"] = data
    _KANBAN_CACHE["at"] = now
    return data


def _cid_de_nome(nome: str | None) -> int | None:
    if not nome:
        return None
    n = nome.strip().lower()
    for cid, fn in CID_TO_FRONT.items():
        if fn.lower() == n:
            return cid
    return None


# ── Chat igual ao WhatsApp, pacote A (16/10/2026) ────────────────────────────
# Não lidas com número (persiste ao recarregar) e prévia da última mensagem em
# todas as filas. "Lido até" vive em `conversa_lida` (0092), por LOGIN do painel
# e conversa (p8): a Dona abrir a conversa da Pessoa12 não zera o contador da Pessoa12.

_PREVIEW_MAX = 90
_PREVIEW_MIDIA = {
    "audio": "🎙️ Áudio",
    "image": "🖼️ Foto",
    "video": "🎬 Vídeo",
    "document": "📎 Documento",
    "sticker": "💟 Figurinha",
}
_PREVIEW_MARCADOR = re.compile(r"^\[(document|video|image|audio|sticker|midia)\]$", re.I)


def _preview_msg(tipo: str | None, texto: str | None) -> str:
    """Uma linha curta da mensagem, como o WhatsApp mostra na lista."""
    t = " ".join((texto or "").split())
    if _PREVIEW_MARCADOR.match(t):
        t = ""
    rotulo = _PREVIEW_MIDIA.get((tipo or "").lower())
    if rotulo:
        t = f"{rotulo} · {t}" if t else rotulo
    if not t:
        t = "📩 Mensagem"
    return t if len(t) <= _PREVIEW_MAX else t[: _PREVIEW_MAX - 1].rstrip() + "…"


def _nao_lidas_por_p8(s: Any, usuario: str, cids: list[int] | None) -> dict[str, int]:
    """Mensagens do lead ainda não lidas por ESTE login, por conversa (p8).

    Conta o que o lead mandou depois do mais recente entre (a) o "lido até"
    gravado pra esse login e (b) a última mensagem da linha (a corretora
    respondeu pelo celular = ela leu). Sem registro de leitura vale só (b): no
    primeiro dia ninguém acorda com 300 não lidas, só o que está sem resposta.
    """
    rows = s.execute(
        sql_text(
            """
            WITH m AS (
                SELECT right(regexp_replace(contato_telefone, '\\D', '', 'g'), 8) AS p8,
                       timestamp_evolution AS at, from_corretora
                FROM mensagens_whatsapp
                WHERE contato_telefone IS NOT NULL
                  AND (CAST(:cids AS INT[]) IS NULL OR corretora_id = ANY(CAST(:cids AS INT[])))
            ), saida AS (
                SELECT p8, max(at) AS at FROM m WHERE from_corretora GROUP BY p8
            )
            SELECT m.p8, count(*) AS n
            FROM m
            LEFT JOIN saida ON saida.p8 = m.p8
            LEFT JOIN conversa_lida l ON l.p8 = m.p8 AND l.usuario = :u
            WHERE NOT m.from_corretora
              AND m.at > GREATEST(
                    COALESCE(saida.at, '-infinity'::timestamptz),
                    COALESCE(l.lido_ate, '-infinity'::timestamptz))
            GROUP BY m.p8
            """
        ),
        {"cids": cids, "u": usuario.lower()},
    ).all()
    return {r[0]: int(r[1]) for r in rows if r[0]}


class ConversaLidaIn(BaseModel):
    telefone: str = Field(..., min_length=8, max_length=40)
    # instante da última mensagem que a pessoa viu na tela; sem ele, agora
    ate: datetime | None = None


@router.post("/conversa/lida")
def painel_conversa_lida(
    body: ConversaLidaIn,
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Marca a conversa como lida ATÉ `ate`, só pra quem está olhando.

    Nunca volta no tempo (GREATEST): abrir a conversa num aparelho com a tela
    velha não desmarca o que foi lido no outro. Não escreve em mensagem nem
    em lead — só no contador deste login.
    """
    user = _identificar_usuario(x_painel_user)
    p8 = re.sub(r"\D", "", body.telefone)[-8:]
    if len(p8) < 8:
        raise HTTPException(422, "telefone_invalido")
    agora = datetime.now(timezone.utc)
    ate = body.ate or agora
    if ate.tzinfo is None:
        ate = ate.replace(tzinfo=timezone.utc)
    ate = min(ate, agora)
    with SessionLocal() as s:
        lido = s.execute(
            sql_text(
                """
                INSERT INTO conversa_lida (usuario, p8, lido_ate, atualizado_em)
                VALUES (:u, :p8, :ate, now())
                ON CONFLICT (usuario, p8) DO UPDATE
                   SET lido_ate = GREATEST(conversa_lida.lido_ate, EXCLUDED.lido_ate),
                       atualizado_em = now()
                RETURNING lido_ate
                """
            ),
            {"u": user.lower(), "p8": p8, "ate": ate},
        ).scalar_one()
        s.commit()
    log.info("painel_conversa_lida", usuario=user.lower(), p8=p8)
    return {"ok": True, "p8": p8, "lido_ate": lido.isoformat()}


@router.get("/kanban")
def painel_kanban(
    corretora: str | None = Query(None),
    ordem: str = Query("parados"),
    incluir_fora_funil: int = Query(0, description="1 = anexa coluna com conversas WhatsApp sem lead no funil (chat V2)"),
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Board do Funil de Vendas direto do Kommo, na ordem real das etapas.

    Enriquece cada card com 'dias parado' e 'acende' (atividade no WhatsApp).
    Privacidade: corretora vê só os leads dela (responsible_user_id).
    """
    import re as _re

    user = _identificar_usuario(x_painel_user)
    my_cid = _viewer_info(user)["corretora_id"]
    alvo_cid = my_cid if my_cid is not None else _cid_de_nome(corretora)
    # Escopo do board = a linha dela + as delegadas (Pessoa12 cobre a Pessoa7).
    cids_alvo = _cids_atendimento(alvo_cid)

    snap = _kommo_funil_snapshot()
    statuses = snap["statuses"]
    leads = snap["leads"]
    users = snap["users"]

    # telefone por lead_id (leads_snapshot) — casa atividade/conversa
    with SessionLocal() as s:
        prows = s.execute(
            sql_text("SELECT lead_id, telefone FROM leads_snapshot WHERE telefone IS NOT NULL")
        ).all()
        # Lead TRANSFERIDO no painel (/crm/transferir): a dona vem do NOSSO
        # banco, não do responsible do Kommo — o lead some do escopo da
        # corretora antiga e aparece pra nova (corretora_editado_manual=true,
        # protegido do sync — migration 0061).
        orows = s.execute(
            sql_text(
                "SELECT lead_id, corretora_id FROM leads_snapshot "
                "WHERE corretora_editado_manual = TRUE AND corretora_id IS NOT NULL"
            )
        ).all()
        # selo "transferido" recente (7 dias) — aviso in-app pra quem recebeu
        trows = s.execute(
            sql_text(
                """
                SELECT DISTINCT l.external_ref
                FROM lead_events e
                JOIN leads l ON l.id = e.lead_id
                WHERE e.tipo = 'transferencia'
                  AND e.at >= NOW() - INTERVAL '7 days'
                  AND l.external_ref IS NOT NULL
                """
            )
        ).all()
    phone_by_id = {str(r[0]): r[1] for r in prows}
    cid_override = {str(r[0]): int(r[1]) for r in orows}
    transferidos_recentes = {str(r[0]) for r in trows}

    # atividade (última msg por p8), privacidade por corretora.
    #
    # `acende` (fila "🟢 Esperando resposta" do Chat) NÃO é mais "o lead falou
    # por último" e sim "o lead falou por último E PEDE RESPOSTA" — a limpeza do
    # "OK" (Dona, 08/2026): "ok"/"obrigado"/"👍" não são pendência. `dias`
    # (tempo parado) continua vindo da última msg seja ela qual for.
    from app.services.pede_resposta import classificar as _classificar_pede

    ativ: dict[str, tuple[Any, bool]] = {}
    with SessionLocal() as s:
        arows = s.execute(
            sql_text(
                """
                SELECT DISTINCT ON (right(regexp_replace(contato_telefone, '\\D', '', 'g'), 8))
                    right(regexp_replace(contato_telefone, '\\D', '', 'g'), 8) AS p8,
                    timestamp_evolution AS at, (NOT from_corretora) AS do_lead,
                    tipo_mensagem, conteudo_texto
                FROM mensagens_whatsapp
                WHERE contato_telefone IS NOT NULL
                  AND (CAST(:cids AS INT[]) IS NULL OR corretora_id = ANY(CAST(:cids AS INT[])))
                ORDER BY right(regexp_replace(contato_telefone, '\\D', '', 'g'), 8),
                         timestamp_evolution DESC
                """
            ),
            {"cids": cids_alvo},
        ).mappings().all()
    prev: dict[str, tuple[str, str]] = {}
    for r in arows:
        acende = bool(r["do_lead"]) and _classificar_pede(
            r["conteudo_texto"], r["tipo_mensagem"]
        ).pede
        ativ[r["p8"]] = (r["at"], acende)
        prev[r["p8"]] = (
            _preview_msg(r["tipo_mensagem"], r["conteudo_texto"]),
            "lead" if r["do_lead"] else "corretora",
        )
    with SessionLocal() as s:
        nao_lidas = _nao_lidas_por_p8(s, user, cids_alvo)

    agora = datetime.now(timezone.utc)
    by_status: dict[int, list[dict[str, Any]]] = {st["id"]: [] for st in statuses}
    esperando = 0

    for lead in leads:
        ruid = lead.get("responsible_user_id")
        lead_id = str(lead.get("id"))
        # transferência manual vence o responsible do Kommo (ver cid_override)
        cid = cid_override.get(lead_id, KOMMO_USER_CORRETORA.get(ruid))
        if cids_alvo is not None and cid not in cids_alvo:
            continue
        sid = lead.get("status_id")
        if sid not in by_status:
            continue
        tel = phone_by_id.get(lead_id, "")
        p8 = _re.sub(r"\D", "", tel)[-8:] if tel else ""
        a = ativ.get(p8)
        at = a[0] if a else None
        do_lead = a[1] if a else False
        if do_lead:
            esperando += 1
        dias = (agora - at).days if at else None
        corr = CID_TO_FRONT.get(cid) or (users.get(ruid, "") if ruid else "")
        tags = [t.get("name") for t in lead.get("_embedded", {}).get("tags", []) if t.get("name")]
        by_status[sid].append({
            "kommoLeadId": lead_id,
            "nome": lead.get("name") or "Sem nome",
            "telefone": tel,
            "corretora": corr,
            "tags": tags,
            "stage": str(sid),
            "ultima_at": at.isoformat() if at else None,
            "dias": dias,
            "acende": do_lead,
            # selo in-app: lead trocou de corretora nos últimos 7 dias
            "transferido": lead_id in transferidos_recentes,
            # pacote A do Chat: prévia + quem falou por último + não lidas
            "preview": prev.get(p8, (None, None))[0] if p8 else None,
            "preview_de": prev.get(p8, (None, None))[1] if p8 else None,
            "nao_lidas": nao_lidas.get(p8, 0) if p8 else 0,
        })

    def _key(c: dict[str, Any]) -> tuple:
        d = c["dias"]
        if d is None:
            return (1, 0, c["nome"])
        return (0, d if ordem == "recentes" else -d, c["nome"])

    colunas = []
    for st in statuses:
        sid = st["id"]
        cards = sorted(by_status.get(sid, []), key=_key)
        parados = sum(1 for c in cards if c["dias"] is not None and c["dias"] > 7)
        colunas.append({
            "stage": str(sid),
            "label": st.get("name", ""),
            "ativo": sid not in STATUS_TERMINAIS,
            "total": len(cards),
            "parados": parados,
            "cards": cards[:100],
        })

    # Coluna extra do chat V2: TODA conversa do WhatsApp que não casa com lead
    # do funil (contato sem lead, lead de outro pipeline, número interno). O
    # chat é o espelho do celular — a conversa existe mesmo sem card no Kommo.
    # kommoLeadId usa o lead_id do snapshot quando há (thread abre pelo caminho
    # normal); senão "tel:<dígitos>" (painel_conversa resolve pelo histórico).
    if incluir_fora_funil:
        no_board = {
            _re.sub(r"\D", "", c["telefone"])[-8:]
            for col in colunas for c in col["cards"] if c.get("telefone")
        }
        with SessionLocal() as s:
            frows = s.execute(
                sql_text(
                    """
                    SELECT DISTINCT ON (p8) p8, contato_telefone, corretora_id,
                           timestamp_evolution AS at, (NOT from_corretora) AS do_lead,
                           contato_nome, tipo_mensagem, conteudo_texto
                    FROM (
                        SELECT right(regexp_replace(contato_telefone, '\\D', '', 'g'), 8) AS p8,
                               contato_telefone, corretora_id, timestamp_evolution,
                               from_corretora, contato_nome, tipo_mensagem, conteudo_texto
                        FROM mensagens_whatsapp
                        WHERE contato_telefone IS NOT NULL
                          AND remote_jid NOT LIKE '%@g.us'
                          AND (CAST(:cids AS INT[]) IS NULL OR corretora_id = ANY(CAST(:cids AS INT[])))
                    ) t
                    ORDER BY p8, timestamp_evolution DESC
                    """
                ),
                {"cids": cids_alvo},
            ).mappings().all()
            # nome/lead_id do snapshot quando o telefone tem lead fora do funil
            snaprows = s.execute(sql_text(_sql_lead_por_p8())).all()
        snap_by_p8 = {}
        for p8, lid, nome, _etapa in snaprows:
            snap_by_p8.setdefault(p8, (str(lid), nome))
        fora: list[dict[str, Any]] = []
        for r in frows:
            if r["p8"] in no_board or not r["p8"]:
                continue
            digits = _re.sub(r"\D", "", r["contato_telefone"])
            lid, nome = snap_by_p8.get(r["p8"], (None, None))
            at = r["at"]
            fora.append({
                "kommoLeadId": lid or f"tel:{digits}",
                "nome": nome or r["contato_nome"] or f"+{digits}",
                "telefone": f"+{digits}",
                "corretora": CID_TO_FRONT.get(r["corretora_id"]) or "",
                "tags": [],
                "stage": "fora_funil",
                "ultima_at": at.isoformat() if at else None,
                "dias": (agora - at).days if at else None,
                # mesma limpeza do "OK" da coluna do funil (ver acima)
                "acende": bool(r["do_lead"]) and _classificar_pede(
                    r["conteudo_texto"], r["tipo_mensagem"]
                ).pede,
                "preview": _preview_msg(r["tipo_mensagem"], r["conteudo_texto"]),
                "preview_de": "lead" if r["do_lead"] else "corretora",
                "nao_lidas": nao_lidas.get(r["p8"], 0),
            })
        fora.sort(key=lambda c: c["ultima_at"] or "", reverse=True)
        colunas.append({
            "stage": "fora_funil",
            "label": "Fora do funil",
            "ativo": True,
            "total": len(fora),
            "parados": 0,
            "cards": fora[:100],
        })

    return {
        "viewer": {
            "user": user,
            "isAdmin": my_cid is None,
            "corretora": CORRETORA_NOME_DB.get(my_cid) if my_cid else None,
        },
        "filtro": CID_TO_FRONT.get(alvo_cid) if alvo_cid else None,
        "esperando": esperando,
        "total": sum(c["total"] for c in colunas),
        "colunas": colunas,
        "fontes": _frescor_fontes(),
    }


@router.get("/conversa/atividade")
def painel_conversa_atividade(
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Última mensagem por lead (data + se foi do lead).

    Alimenta o "X dias parado" no card e o acender (do_lead). Por p8 (8
    dígitos do telefone). Privacidade por corretora. Sem filtro de tempo —
    pega a última de cada telefone (índice telefone+ts cobre).
    """
    user = _identificar_usuario(x_painel_user)
    my_cid = _viewer_info(user)["corretora_id"]

    sql = """
        SELECT DISTINCT ON (right(regexp_replace(contato_telefone, '\\D', '', 'g'), 8))
            right(regexp_replace(contato_telefone, '\\D', '', 'g'), 8) AS p8,
            timestamp_evolution AS at,
            (NOT from_corretora) AS do_lead
        FROM mensagens_whatsapp
        WHERE contato_telefone IS NOT NULL
          AND (CAST(:cids AS INT[]) IS NULL OR corretora_id = ANY(CAST(:cids AS INT[])))
        ORDER BY right(regexp_replace(contato_telefone, '\\D', '', 'g'), 8),
                 timestamp_evolution DESC
    """
    with SessionLocal() as s:
        rows = s.execute(sql_text(sql), {"cids": _cids_atendimento(my_cid)}).mappings().all()

    return {
        "telefones": {
            r["p8"]: {
                "at": r["at"].isoformat() if r["at"] else None,
                "do_lead": bool(r["do_lead"]),
            }
            for r in rows
        },
        # Carimbo de frescor pra view Funil: o board lê Twenty (via Next), mas a
        # atividade/etapa traça pro nosso Postgres. Expõe as fontes reais aqui pro
        # /api/leads surfar o "dados de quando" sem uma chamada extra.
        "fontes": _frescor_fontes(),
    }


@router.get("/conversas/por-corretora")
def painel_conversas_por_corretora(
    periodo: str = Query("hoje"),
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Quantas mensagens cada corretora trocou — e com quanta gente.

    POR QUE EXISTE (19/08): a Dona perguntou no Telegram "quantas mensagens a
    Pessoa1 trocou hoje" e a Assistente não tinha como responder. As tools de dado
    cobriam funil (`acme_resumo`) e lead a lead (`acme_leads`), mas nada
    media ATIVIDADE de conversa. Ela respondeu que não sabia — certo, mas
    inútil.

    O que conta como mensagem DELA: saída que não é template e não tem carimbo
    de robô. Disparo automático e resposta da Pessoa não são trabalho da
    corretora, e misturar isso infla o número de quem tem mais automação
    rodando na linha — exatamente o contrário do que a pergunta quer saber.

    `pessoas` importa tanto quanto `mensagens`: 200 mensagens com 3 pessoas é
    outra coisa que 200 com 40.
    """
    _identificar_usuario(x_painel_user)   # 403 se não conhecer
    di, df, label = parse_periodo(periodo, None, None)

    sql = """
        WITH msgs AS (
            SELECT m.corretora_id,
                   m.from_corretora,
                   right(regexp_replace(coalesce(m.contato_telefone,''), '\D', '', 'g'), 8) AS p8,
                   (m.from_corretora
                    AND coalesce(m.tipo_mensagem,'') <> 'template'
                    AND coalesce(m.conteudo_metadata->>'origem','') = '') AS humana
              FROM mensagens_whatsapp m
             WHERE m.tenant_id = 1
               AND m.contato_telefone IS NOT NULL
               AND (m.timestamp_evolution AT TIME ZONE 'America/Sao_Paulo')::date
                   BETWEEN :di AND :df
        )
        SELECT corretora_id,
               count(*) FILTER (WHERE humana)                    AS enviadas,
               count(*) FILTER (WHERE NOT from_corretora)         AS recebidas,
               count(*) FILTER (WHERE from_corretora AND NOT humana) AS automaticas,
               count(DISTINCT p8) FILTER (WHERE humana)           AS pessoas,
               count(DISTINCT p8) FILTER (WHERE NOT from_corretora) AS pessoas_que_falaram
          FROM msgs
         WHERE corretora_id IS NOT NULL
         GROUP BY 1
    """
    with SessionLocal() as s:
        linhas = s.execute(sql_text(sql), {"di": di, "df": df}).mappings().all()

    por: list[dict[str, Any]] = []
    for r in linhas:
        cid = r["corretora_id"]
        por.append({
            "corretora_id": cid,
            "corretora": CORRETORA_LABEL.get(cid, str(cid)),
            "enviadas": int(r["enviadas"] or 0),
            "recebidas": int(r["recebidas"] or 0),
            "trocadas": int(r["enviadas"] or 0) + int(r["recebidas"] or 0),
            "automaticas": int(r["automaticas"] or 0),
            "pessoas": int(r["pessoas"] or 0),
            "pessoas_que_falaram": int(r["pessoas_que_falaram"] or 0),
        })
    por.sort(key=lambda x: -x["trocadas"])

    return {
        "periodo": {"tipo": periodo, "inicio": di.isoformat(),
                    "fim": df.isoformat(), "label": label},
        "por_corretora": por,
        "total": {
            "enviadas": sum(x["enviadas"] for x in por),
            "recebidas": sum(x["recebidas"] for x in por),
            "trocadas": sum(x["trocadas"] for x in por),
            "automaticas": sum(x["automaticas"] for x in por),
        },
        # O que NÃO está contado — pra ninguém ler o número como "tudo que saiu".
        "nota": ("mensagens DELA = saída humana (sem template e sem carimbo de "
                 "robô). Disparo e resposta da Pessoa entram em 'automaticas'."),
    }


@router.get("/conversa/aguardando")
def painel_conversa_aguardando(
    dias: int = Query(14, ge=1, le=60),
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Leads cuja ÚLTIMA mensagem foi do lead E QUE PEDE RESPOSTA.

    Stateless: o card "acende" quando o lead falou por último e apaga quando a
    corretora responde (a mensagem dela vira a mais recente). Retorna os últimos
    8 dígitos do telefone — o front casa com o card. Privacidade por corretora.

    LIMPEZA DO "OK" (Dona, 08/2026): antes contava QUALQUER última mensagem do
    lead — então "ok", "obrigado" e "👍" viravam pendência e a corretora mandava
    joinha só pra limpar a tela. Isso treinava ela a fazer trabalho falso e a
    desconfiar do card. Agora passa por `pede_resposta.classificar`: só entra
    quem PEDE resposta. Trava: na dúvida MANTÉM (ver o módulo).
    `total_bruto` e `encerramentos_filtrados` ficam expostos pra dar pra
    conferir quantos sumiram e por quê.
    """
    from app.services.pede_resposta import classificar

    user = _identificar_usuario(x_painel_user)
    my_cid = _viewer_info(user)["corretora_id"]

    sql = """
        WITH ult AS (
            SELECT DISTINCT ON (right(regexp_replace(contato_telefone, '\\D', '', 'g'), 8))
                right(regexp_replace(contato_telefone, '\\D', '', 'g'), 8) AS p8,
                from_corretora,
                tipo_mensagem,
                conteudo_texto,
                timestamp_evolution
            FROM mensagens_whatsapp
            WHERE timestamp_evolution > NOW() - (:dias || ' days')::interval
              AND contato_telefone IS NOT NULL
              AND (CAST(:cids AS INT[]) IS NULL OR corretora_id = ANY(CAST(:cids AS INT[])))
            ORDER BY right(regexp_replace(contato_telefone, '\\D', '', 'g'), 8),
                     timestamp_evolution DESC
        )
        SELECT p8, timestamp_evolution AS at, tipo_mensagem, conteudo_texto
        FROM ult
        WHERE from_corretora = FALSE
    """
    with SessionLocal() as s:
        rows = s.execute(sql_text(sql), {"dias": dias, "cids": _cids_atendimento(my_cid)}).mappings().all()

    telefones: dict[str, str | None] = {}
    filtrados = 0
    for r in rows:
        if not classificar(r["conteudo_texto"], r["tipo_mensagem"]).pede:
            filtrados += 1
            continue
        telefones[r["p8"]] = r["at"].isoformat() if r["at"] else None

    return {
        "total": len(telefones),
        "total_bruto": len(rows),
        "encerramentos_filtrados": filtrados,
        "telefones": telefones,
    }


@router.post("/conversa/encerrar")
def painel_conversa_encerrar(
    body: dict[str, Any] = Body(...),
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """"Feito" — a conversa acabou com a mensagem do LEAD, sem precisar de resposta.

    Body: {"telefone": "+55..."} (aceita `p8` também).

    Marca a conversa como encerrada NO INSTANTE da última mensagem dela. Guarda
    o instante, não um "encerrada=true": se a pessoa escrever de novo depois, a
    mensagem nova é mais recente que a marca e a conversa VOLTA pro bloco
    sozinha. Com um booleano, um clique sumiria com o lead pra sempre.

    O instante vem do BANCO, nunca do cliente: uma aba velha (ou um curl) que
    mandasse um `at` no futuro apagaria a conversa pra sempre.
    """
    user = _identificar_usuario(x_painel_user)
    my_cid = _viewer_info(user)["corretora_id"]

    bruto = str(body.get("telefone") or body.get("p8") or "")
    p8 = re.sub(r"\D", "", bruto)[-8:]
    if len(p8) < 8:
        raise HTTPException(422, "telefone_invalido")

    with SessionLocal() as s:
        # Última mensagem DA CONVERSA, no escopo de quem está marcando: a
        # corretora não encerra conversa de lead de outra.
        row = s.execute(
            sql_text(
                """
                SELECT timestamp_evolution AS at, corretora_id
                  FROM mensagens_whatsapp
                 WHERE right(regexp_replace(contato_telefone, '\\D', '', 'g'), 8) = :p8
                   AND (CAST(:cids AS INT[]) IS NULL OR corretora_id = ANY(CAST(:cids AS INT[])))
                 ORDER BY timestamp_evolution DESC
                 LIMIT 1
                """
            ),
            {"p8": p8, "cids": _cids_atendimento(my_cid)},
        ).mappings().first()
        # SEM MENSAGEM NENHUMA ainda é caso válido (07/08): no bloco "aguardam
        # primeiro contato" a pessoa às vezes responde por LIGAÇÃO, e não existe
        # thread pra datar. Marca o instante de agora — se ela escrever depois,
        # a mensagem é mais recente que a marca e o lead volta sozinho.
        marca = row["at"] if row is not None else datetime.now(timezone.utc)
        cid_marca = row["corretora_id"] if row is not None else my_cid

        s.execute(
            sql_text(
                """
                INSERT INTO conversa_encerrada
                    (tenant_id, p8, encerrado_ate, corretora_id, autor)
                VALUES (1, :p8, :ate, :cid, :autor)
                ON CONFLICT (tenant_id, p8) DO UPDATE
                   SET encerrado_ate = GREATEST(conversa_encerrada.encerrado_ate,
                                                EXCLUDED.encerrado_ate),
                       corretora_id = EXCLUDED.corretora_id,
                       autor = EXCLUDED.autor,
                       criado_em = NOW()
                """
            ),
            {"p8": p8, "ate": marca, "cid": cid_marca, "autor": user},
        )
        s.commit()

    log.info("conversa_encerrada_manual", p8=p8, user=user)
    return {
        "ok": True,
        "p8": p8,
        "encerrado_ate": marca.isoformat() if marca else None,
        "sem_conversa": row is None,
    }


@router.get("/conversa/esperando_resposta")
def painel_conversa_esperando_resposta(
    dias: int = Query(30, ge=1, le=90),
    corretora: str | None = Query(None),
    incluir_automaticas: int = Query(
        0, description="1 = inclui conversas cuja última msg é template automático"
    ),
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Conversas cuja ÚLTIMA mensagem é DA CORRETORA — o lead ainda não respondeu.

    Fila inversa do /conversa/aguardando: lá o LEAD espera a corretora; aqui a
    CORRETORA espera o lead. Ordenada da espera mais LONGA pra mais curta (msg
    da corretora mais antiga primeiro). `dias` corta o arquivado: conversa sem
    movimento há mais de `dias` (default 30) fica FORA da fila.

    Mensagem automática: última msg com tipo_mensagem='template' (cadência/
    reengajamento via Cloud API — enviar_template_corretor grava tipo="template")
    fica FORA por default, a cadência já cuida desses leads.
    `incluir_automaticas=1` traz de volta. Limite conhecido: msg automática
    de TEXTO LIVRE (não-template) não é distinguível e entra na fila.

    Privacidade por corretora (X-Painel-User). Admin vê todas e filtra com
    ?corretora=<nome front, ex. Pessoa6>. Números internos (corretoras/dona)
    e grupos ficam fora. Enriquece com nome/etapa do leads_snapshot quando o
    telefone tem lead no board; kommoLeadId cai em "tel:<dígitos>" quando não.
    """
    import re as _re

    user = _identificar_usuario(x_painel_user)
    my_cid = _viewer_info(user)["corretora_id"]
    alvo_cid = my_cid if my_cid is not None else _cid_de_nome(corretora)

    sql = r"""
        WITH ult AS (
            SELECT DISTINCT ON (right(regexp_replace(contato_telefone, '\D', '', 'g'), 8))
                right(regexp_replace(contato_telefone, '\D', '', 'g'), 8) AS p8,
                contato_telefone, contato_nome, corretora_id,
                from_corretora, tipo_mensagem, conteudo_texto,
                timestamp_evolution AS at
            FROM mensagens_whatsapp
            WHERE timestamp_evolution > NOW() - (:dias || ' days')::interval
              AND contato_telefone IS NOT NULL
              AND remote_jid NOT LIKE '%@g.us'
              AND (CAST(:cids AS INT[]) IS NULL OR corretora_id = ANY(CAST(:cids AS INT[])))
            ORDER BY right(regexp_replace(contato_telefone, '\D', '', 'g'), 8),
                     timestamp_evolution DESC
        )
        SELECT u.p8, u.contato_telefone, u.contato_nome, u.corretora_id,
               u.tipo_mensagem, u.conteudo_texto, u.at
        FROM ult u
        WHERE u.from_corretora = TRUE
          AND (CAST(:inc_auto AS INT) = 1 OR coalesce(u.tipo_mensagem, '') <> 'template')
          AND u.p8 NOT IN (
              SELECT right(regexp_replace(telefone_normalizado, '\D', '', 'g'), 8)
              FROM corretoras WHERE telefone_normalizado IS NOT NULL
              UNION
              SELECT right(regexp_replace(telefone_normalizado, '\D', '', 'g'), 8)
              FROM internos_acme WHERE telefone_normalizado IS NOT NULL
          )
        ORDER BY u.at ASC
    """
    with SessionLocal() as s:
        rows = s.execute(
            sql_text(sql),
            {"dias": dias, "cids": _cids_atendimento(alvo_cid), "inc_auto": incluir_automaticas},
        ).mappings().all()
        snaprows = s.execute(
            sql_text(_sql_lead_por_p8())
        ).all()

    snap_by_p8: dict[str, tuple[str, str | None, str | None]] = {}
    for p8, lid, nome, etapa in snaprows:
        snap_by_p8.setdefault(p8, (str(lid), nome, etapa))

    # etapa_kommo é o ID cru do status ("10000005"). O kanban já resolve pelo
    # nome — a fila mostrava o número. Mesmo namespace, mesmo mapa.
    stage_map = _stage_map()

    agora = datetime.now(timezone.utc)
    conversas: list[dict[str, Any]] = []
    for r in rows:
        digits = _re.sub(r"\D", "", r["contato_telefone"])
        lid, nome, etapa = snap_by_p8.get(r["p8"], (None, None, None))
        at = r["at"]
        conversas.append({
            "kommoLeadId": lid or f"tel:{digits}",
            "nome": nome or r["contato_nome"] or f"+{digits}",
            "telefone": f"+{digits}",
            "corretora": CID_TO_FRONT.get(r["corretora_id"]) or "",
            "etapa": (stage_map.get(str(etapa).strip(), etapa) if etapa else None),
            # mesma prévia da lista (pacote A): "🎙️ Áudio" no lugar de "[audio]"
            "preview": _preview_msg(r["tipo_mensagem"], r["conteudo_texto"]),
            "preview_de": "corretora",
            "ultima_at": at.isoformat() if at else None,
            "espera_horas": round((agora - at).total_seconds() / 3600, 1) if at else None,
            "automatica": (r["tipo_mensagem"] or "") == "template",
        })

    return {
        "viewer": {
            "user": user,
            "isAdmin": my_cid is None,
            "corretora": CORRETORA_NOME_DB.get(my_cid) if my_cid else None,
        },
        "filtro": CID_TO_FRONT.get(alvo_cid) if alvo_cid else None,
        "dias": dias,
        "total": len(conversas),
        "conversas": conversas,
        "fontes": _frescor_fontes(),
    }


@router.post("/conversa/enviar")
def painel_conversa_enviar(
    payload: dict[str, Any] = Body(...),
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Envia uma mensagem ao lead PELA LINHA da corretora dona do card.

    Body: {"lead_id": str, "texto": str}.
    Privacidade: corretora só envia pra lead dela; admin envia por qualquer linha.
    Em DRY_RUN (settings.dry_run, default True) NÃO envia — só loga e devolve
    dry_run=True. O primeiro envio real depende de virar a chave no .env.
    """
    user = _identificar_usuario(x_painel_user)
    viewer = _viewer_info(user)
    my_cid = viewer["corretora_id"]

    lead_id = str(payload.get("lead_id") or "").strip()
    texto = str(payload.get("texto") or "").strip()
    if not lead_id:
        raise HTTPException(422, "lead_id_obrigatorio")
    if not texto:
        raise HTTPException(422, "texto_vazio")
    if len(texto) > 4096:
        raise HTTPException(422, "texto_muito_longo")

    ctx = _lead_conversa_contexto(lead_id, viewer_cid=my_cid)
    if not _pode_atender(my_cid, ctx["corretora_id"]):
        raise HTTPException(403, "lead_de_outra_corretora")

    # Linha recusando por COBRANÇA: avisar antes é melhor que dizer "enviado" e
    # a mensagem não chegar (Pessoa10 → Sonia, 18/09). Ver linha_saude.
    _checar_cobranca(ctx.get("instance"), lead_id=lead_id, user=user)

    from app.config import settings
    from app.integrations.evolution_client import EvolutionClient

    to_number = ctx["telefone"].lstrip("+").replace(" ", "").replace("-", "")

    if settings.dry_run:
        log.info(
            "painel_conversa_enviar_dry_run",
            lead_id=lead_id, instance=ctx["instance"],
            to_last4=to_number[-4:], chars=len(texto), user=user,
        )
        return {
            "ok": True,
            "dry_run": True,
            "corretora_nome": ctx["corretora_nome"],
            "enviado_por_instance": ctx["instance"],
        }

    # Linha de saída = a linha Cloud da DONA do lead (Fase 2: Pessoa12 2591, Pessoa10 2211,
    # Pessoa11 depois). Antes só a Pessoa12 tinha caminho Cloud e as outras caíam no
    # Evolution — que fica DESCONECTADO justamente quando a corretora entra em
    # coexistência, então a mensagem ia pro vazio (a Pessoa10 ficou 1 dia assim).
    # Número de coexistência não segura sessão Evolution/Baileys: se a linha é
    # Cloud, TEM que sair por Cloud.
    from app.services import cloud_lines as _cl_env

    linha_saida = _cl_env.linha_da_corretora(ctx["corretora_id"])
    if linha_saida is _cl_env.INDETERMINADO:
        # "não deu pra saber" (sem cadastro da corretora) → mantém o caminho
        # histórico do Evolution em vez de arriscar sair pela linha errada.
        linha_saida = None
    try:
        if linha_saida is not None:
            from app.api.cloud_corretora_a import enviar_cloud
            enviar_cloud(to_number, texto, linha=linha_saida,
                         instance=ctx["instance"], corretora_id=ctx["corretora_id"])
        else:
            EvolutionClient().send_text(
                instance_name=ctx["instance"], to_number=to_number, text=texto,
            )
    except Exception as e:  # noqa: BLE001
        log.error(
            "painel_conversa_enviar_falha",
            lead_id=lead_id, instance=ctx["instance"], erro=str(e)[:160],
        )
        raise HTTPException(502, "envio_falhou") from e

    log.info(
        "painel_conversa_enviado",
        lead_id=lead_id, instance=ctx["instance"],
        to_last4=to_number[-4:], chars=len(texto), user=user,
    )
    return {
        "ok": True,
        "dry_run": False,
        "corretora_nome": ctx["corretora_nome"],
        "enviado_por_instance": ctx["instance"],
    }


@router.post("/conversa/reengajar")
def painel_conversa_reengajar_template(
    payload: dict[str, Any] = Body(...),
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Reengaja um lead FORA da janela de 24h, a partir do Chat do painel.

    ÚNICA implementação desta rota (havia uma duplicata sem opt-out, removida
    no L2 · 26/07 — ver nota onde ela ficava).

    Body: {"lead_id": str, "texto": str}. Manda um template UTILITY (nunca
    MARKETING) via `enviar_template_corretor` — a saudação ("Olá {nome}, tudo bem?" /
    "Oi, tudo bem?") vem no próprio template; o `texto` da corretora é o corpo.
    `_limpar_saudacao` remove UMA saudação duplicada no início (rede de segurança).

    Escopo: SÓ a linha da Pessoa12 (acme_corretora_a) usa Cloud API/templates. Outras
    corretoras ainda estão no Evolution e não têm esse caminho.

    Privacidade/DRY_RUN idênticos ao /conversa/enviar. Grava no thread pelo mesmo
    caminho do enviar_template_corretor (gravar()). Respeita opt-out.
    """
    user = _identificar_usuario(x_painel_user)
    viewer = _viewer_info(user)
    my_cid = viewer["corretora_id"]

    lead_id = str(payload.get("lead_id") or "").strip()
    texto = str(payload.get("texto") or "").strip()
    if not lead_id:
        raise HTTPException(422, "lead_id_obrigatorio")
    if not texto:
        raise HTTPException(422, "texto_vazio")
    if len(texto) > 900:
        # Parâmetro de template Meta tem limite; corpo de reengajamento é curto.
        raise HTTPException(422, "texto_muito_longo")

    ctx = _lead_conversa_contexto(lead_id)
    if not _pode_atender(my_cid, ctx["corretora_id"]):
        raise HTTPException(403, "lead_de_outra_corretora")

    # Template é o caminho PAGO — é justamente o que a recusa de cobrança
    # derruba. Sem este check o painel dizia "reengajamento enviado ✔️" e a
    # Meta recusava em seguida (Pessoa10 → Sonia, 18/09 18:46).
    _checar_cobranca(ctx.get("instance"), lead_id=lead_id, user=user)

    # Reengajamento por template vale pra QUALQUER linha Cloud em coexistência
    # (Fase 2). Antes era exclusivo da Pessoa12 e o botão da Pessoa10 dava 422.
    # Duas condições, nesta ordem — a segunda é a regra dura da Dona (só UTILITY):
    #   1. a dona do lead tem linha Cloud;
    #   2. essa linha tem template UTILITY aprovado na Meta (a Meta classifica a
    #      MESMA copy diferente por conta: os 13 primeiros da Pessoa10 viraram
    #      MARKETING, que custa 10x e queima a lista).
    from app.services import cloud_lines as _cl

    _linha_reeng = _cl.linha_da_corretora(ctx["corretora_id"])
    if _linha_reeng is _cl.INDETERMINADO or _linha_reeng is None:
        raise HTTPException(422, "reengajamento_indisponivel_nesta_linha")
    if not _cl.pode_disparar(_linha_reeng.slug):
        raise HTTPException(422, "linha_sem_template_utility_aprovado")

    from app.api.cloud_corretora_a import (
        SemTemplateDeRotacao, _is_opt_out, _limpar_saudacao,
    )

    # Opt-out: se a ÚLTIMA mensagem do lead pediu descadastro, não reengaja.
    from app.api.cloud_corretora_a import ler_thread_cloud

    thread = ler_thread_cloud(ctx["telefone"], hours_back=24 * 90)
    ultima_lead = next(
        (m for m in reversed(thread) if m.get("from") == "lead" and m.get("text")),
        None,
    )
    if ultima_lead and _is_opt_out(ultima_lead.get("text")):
        raise HTTPException(409, "lead_opt_out")

    corpo = _limpar_saudacao(texto)
    if not corpo.strip():
        raise HTTPException(422, "texto_vazio")

    to_number = ctx["telefone"].lstrip("+").replace(" ", "").replace("-", "")

    try:
        # enviar_template_linha já respeita DRY_RUN internamente (loga + grava no
        # thread com wamid dry-*, não bate na Graph API). UTILITY only; greeting
        # automático (acme_ola com nome / acme_oi sem nome). first_name=None
        # → resolve o nome pelo telefone.
        # Sai pela linha da DONA do lead (não pela da Pessoa12). Faltava isto: o gate
        # em cima já resolvia a linha, mas o envio ainda era hardcoded na Pessoa12 —
        # bug pego em 27/07 mandando template da linha da Pessoa12 pra lead da Pessoa10.
        from app.api.cloud_corretora_a import enviar_template_linha

        # Dona 28/07: follow-up / reengajar SEM botão (pool ola).
        resp = enviar_template_linha(
            _linha_reeng, to_number, None, hook=corpo,
            com_botao=False,
            instance=ctx["instance"], corretora_id=ctx["corretora_id"],
        )
    except SemTemplateDeRotacao as e:
        # A linha não tem casco de mensagem livre aprovado. NÃO é falha de
        # entrega: tentar de novo dá exatamente no mesmo lugar. 422 (não 502)
        # pro front dizer o que é e esconder o "tentar de novo" — a Pessoa7
        # apertou 5x seguidas em 10/09 achando que era instabilidade.
        log.warning(
            "painel_conversa_reengajar_sem_template",
            lead_id=lead_id, linha=_linha_reeng.slug, erro=str(e)[:160], user=user,
        )
        raise HTTPException(422, "linha_sem_template_de_reengajamento") from e
    except Exception as e:  # noqa: BLE001
        log.error(
            "painel_conversa_reengajar_falha",
            lead_id=lead_id, to_last4=to_number[-4:], erro=str(e)[:160], user=user,
        )
        raise HTTPException(502, "reengajamento_falhou") from e

    dry = bool(resp.get("dry_run"))
    log.info(
        "painel_conversa_reengajado",
        lead_id=lead_id, to_last4=to_number[-4:], chars=len(corpo),
        dry_run=dry, user=user,
    )
    return {
        "ok": True,
        "dry_run": dry,
        "corretora_nome": ctx["corretora_nome"],
        "enviado_por_instance": ctx["instance"],
        "tipo": "template_utility",
    }


# Ordem de preferência de instância de CONSULTA pra foto de perfil.
# A foto de um número é pública/visível a partir de qualquer instância conectada;
# preferimos a linha da própria Acme (0000/recuperador e corretora_a) porque o lead
# costuma ser contato dela → mais chance de ver a foto.
_FOTO_INSTANCIAS_PREF = [
    "recuperador",       # +5500000000000 — número que a linha da casa exibia até
                         # 15/09/2026 (hoje 0000-0000). Instância Evolution
                         # aposentada: histórico, não trocar pelo novo.
    "acme_corretora_a",   # 2591 coexistência
    "acme_corretora_c",
    "acme_corretor",
    "assistente",
]
_FOTO_TTL_HORAS = 24

# Teto de consultas de foto por HORA, somando todas as linhas.
#
# Por que existe (04/10/2026): o WhatsApp dá a cada número um orçamento de
# consulta de foto, e linha nova tem orçamento pequeno. Em 02/10 um backfill
# fez ~800 consultas em 2h pela linha nova da Assistente (2955) e ela ficou cega —
# medido depois: dos MESMOS 15 telefones que têm foto, ela via 3 e as linhas
# maduras viam 15. A degradação é progressiva: cada consulta a mais afunda
# mais. O teto existe pra isso nunca mais acontecer com a linha da Dona, que
# é a leitora agora (ela autorizou em 04/10).
#
# 30/h casa com o uso natural do painel (corretora abre conversa, pede foto).
# Backfill em rajada continua PROIBIDO — o teto o transforma em fila lenta,
# não em permissão.
_FOTO_TETO_HORA = 30


def _instancia_consulta_aberta() -> str | None:
    """Primeira instância da lista de preferência que estiver 'open'."""
    from app.integrations.evolution_client import EvolutionClient
    cli = EvolutionClient()
    for inst in _FOTO_INSTANCIAS_PREF:
        try:
            if str(cli.get_instance_status(inst)).lower() == "open":
                return inst
        except Exception:  # noqa: BLE001
            continue
    return None


# Ordem de preferência das instâncias do Evolution do MAC pra CONSULTAR foto.
#
# Medido em 03-04/10/2026, nos MESMOS 15 telefones que comprovadamente têm foto:
#   dona-pessoal (2244, linha madura) .. 15/15
#   yashav       (linha madura) ....... 15/15
#   lumea ............................. 9/15
#   leitor-fotos (2955, linha nova) ... 3/15  ← queimada pelo backfill de 02/10
#
# Não é privacidade do lead nem agenda de contatos (testado: os números que só a
# linha madura vê NÃO estão na agenda dela). É orçamento de consulta por número.
#
# A linha da Dona é a leitora desde 04/10 — ela autorizou ("pode consultar pelo
# meu celular"). Protegida pelo _FOTO_TETO_HORA: ler foto não bane linha, mas
# rajada estrangula a consulta, e a 2244 é a linha crítica dos disparos.
# `corretora_a` fica FORA: está `close` e religar significa WhatsApp Web na linha
# que já tomou dois bans — decisão da Dona em 04/10 foi deixar parada.
_FOTO_MAC_PREF = ["dona-pessoal", "leitor-fotos"]


def _mac_evolution_corretor() -> tuple[Any, list[str]] | None:
    """Client Evolution do MAC (IP residencial) + instâncias candidatas.

    Lê /etc/agencia/evolution-mac.env (EVOLUTION_MAC_URL/KEY/CORRETOR_INSTANCE). A
    instância do .env entra na frente das demais candidatas — é o botão de
    trocar a linha sem mexer no código.
    """
    from pathlib import Path as _P
    cfg_path = _P("/etc/agencia/evolution-mac.env")
    if not cfg_path.exists():
        return None
    conf: dict[str, str] = {}
    for line in cfg_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            conf[k.strip()] = v.strip()
    url = conf.get("EVOLUTION_MAC_URL")
    key = conf.get("EVOLUTION_MAC_KEY")
    if not url or not key:
        return None
    escolhida = conf.get("EVOLUTION_MAC_CORRETOR_INSTANCE")
    candidatas = ([escolhida] if escolhida else []) + [
        i for i in _FOTO_MAC_PREF if i != escolhida
    ]
    from app.integrations.evolution_client import EvolutionClient
    return EvolutionClient(base_url=url, api_key=key), candidatas


def _foto_url_corretor(num: str) -> tuple[str | None, str]:
    """Busca a foto do lead: instâncias do MAC em ordem, depois fallback no VPS.

    Devolve (url, origem). `origem` == "sem_instancia" significa que a consulta
    NÃO aconteceu — quem chama não deve cachear isso como "não tem foto".
    """
    respondeu = False  # alguma instância ABERTA chegou a responder?
    mac = _mac_evolution_corretor()
    if mac is not None:
        cli, candidatas = mac
        for inst in candidatas:
            try:
                if str(cli.get_instance_status(inst)).lower() != "open":
                    continue
                url = cli.fetch_profile_picture_url(inst, num)
            except Exception:  # noqa: BLE001
                continue
            respondeu = True
            if url:
                return url, f"mac:{inst}"
            # Instância aberta que devolve vazio pode ser privacidade do lead OU
            # linha estrangulada. Segue pra próxima: se NENHUMA vê, aí sim é
            # "não tem foto" — e a resposta veio de consulta real.
    inst_vps = _instancia_consulta_aberta()
    if inst_vps:
        from app.integrations.evolution_client import EvolutionClient
        url = EvolutionClient().fetch_profile_picture_url(inst_vps, num)
        return url, f"vps:{inst_vps}"
    return (None, "sem_foto") if respondeu else (None, "sem_instancia")


def _foto_fresca(checked: Any) -> bool:
    """O registro de contato_foto ainda vale (mais novo que _FOTO_TTL_HORAS)?"""
    if checked is None:
        return False
    if checked.tzinfo is None:
        checked = checked.replace(tzinfo=timezone.utc)
    idade_h = (datetime.now(timezone.utc) - checked).total_seconds() / 3600
    return idade_h < _FOTO_TTL_HORAS


def _foto_reservar(num: str) -> tuple[str, Any]:
    """Reserva UMA consulta do _FOTO_TETO_HORA, de forma atômica.

    Lock de sessão no Postgres (`pg_advisory_xact_lock`) em volta de: rechecar o
    cache, contar a última hora e gravar a reserva (`checked_at = now()`). Quem
    chega junto espera o lock e já conta a reserva do anterior — o teto vale com
    qualquer número de requests simultâneas. A consulta ao Evolution fica FORA
    do lock (não segura conexão durante a chamada HTTP).

    Devolve ("cache", url) se outra request acabou de consultar este número,
    ("teto", consultas_na_hora) se não há vaga, ou ("reservada", anterior) com
    o (url, checked_at) de antes da reserva — None se o número era novo — pra
    `_foto_devolver_vaga` desfazer quando a consulta não acontece.
    """
    with SessionLocal() as s:
        s.execute(sql_text("SELECT pg_advisory_xact_lock(hashtext('contato_foto_teto'))"))
        row = s.execute(
            sql_text("SELECT url, checked_at FROM contato_foto WHERE telefone = :t"),
            {"t": num},
        ).first()
        if row is not None and _foto_fresca(row[1]):
            s.commit()
            return "cache", row[0]
        feitas = int(
            s.execute(
                sql_text(
                    "SELECT count(*) FROM contato_foto "
                    "WHERE checked_at > now() - interval '1 hour'"
                )
            ).scalar_one()
        )
        if feitas >= _FOTO_TETO_HORA:
            s.commit()
            return "teto", feitas
        # A reserva mantém a url antiga: quem pedir o mesmo número enquanto a
        # consulta roda recebe a foto que já tinha, não um nulo.
        s.execute(
            sql_text(
                """
                INSERT INTO contato_foto (telefone, url, checked_at)
                VALUES (:t, NULL, now())
                ON CONFLICT (telefone) DO UPDATE SET checked_at = now()
                """
            ),
            {"t": num},
        )
        s.commit()
        return "reservada", (tuple(row) if row is not None else None)


def _foto_devolver_vaga(num: str, anterior: tuple[Any, Any] | None) -> None:
    """Desfaz a reserva de `_foto_reservar` quando a consulta não aconteceu."""
    with SessionLocal() as s:
        if anterior is None:
            # Só a linha que a própria reserva criou (url ainda nula).
            s.execute(
                sql_text("DELETE FROM contato_foto WHERE telefone = :t AND url IS NULL"),
                {"t": num},
            )
        else:
            s.execute(
                sql_text(
                    "UPDATE contato_foto SET url = :u, checked_at = :c WHERE telefone = :t"
                ),
                {"t": num, "u": anterior[0], "c": anterior[1]},
            )
        s.commit()


@router.get("/conversa/foto")
def painel_conversa_foto(
    lead_id: str = Query(...),
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Foto de perfil (WhatsApp) do lead, pra exibir no chat.

    Por ora SÓ pros leads da Pessoa12 (corretora_id=3), conforme pedido. A Pessoa12 não está
    no Evolution (envia pela Cloud), então a foto é consultada por uma instância
    Evolution conectada (preferência: linha 0000 da Acme). Cacheado 24h em
    contato_foto. Retorna {"url": <str|null>}.
    """
    user = _identificar_usuario(x_painel_user)
    my_cid = _viewer_info(user)["corretora_id"]
    ctx = _lead_conversa_contexto(lead_id)

    # Privacidade (Revisão Fable etapa 5): corretora só acessa lead DELA — 403
    # como nos demais endpoints do chat. Sem isso, o 200 com url null confirmava
    # a existência de lead alheio por força bruta de lead_id.
    if (
        not _pode_atender(my_cid, ctx.get("corretora_id"))
        and not _respondeu_na_linha(ctx.get("telefone"), my_cid)
    ):
        raise HTTPException(403, "lead_de_outra_corretora")

    # Escopo: TODAS as corretoras (07/08). Nasceu só pra Pessoa12, mas a busca nunca
    # dependeu da linha dela — `_foto_url_corretor` usa a instância Evolution que
    # estiver aberta e consulta pelo telefone. A trava era de escopo, não
    # técnica, e deixava Pessoa10 e Pessoa11 com avatar cinza no chat sem motivo.

    telefone = ctx.get("telefone") or ""
    num = "".join(c for c in telefone if c.isdigit())
    if not num:
        return {"url": None, "motivo": "sem_telefone"}

    with SessionLocal() as s:
        row = s.execute(
            sql_text("SELECT url, checked_at FROM contato_foto WHERE telefone = :t"),
            {"t": num},
        ).first()
    if row is not None and _foto_fresca(row[1]):
        return {"url": row[0], "cache": True}

    # Trava de ritmo: reserva a vaga de forma ATÔMICA antes de consultar. Ler o
    # contador e só gravar depois da consulta deixava N requests simultâneas
    # passarem juntas — o painel abria um avatar por card e furou o teto em 13
    # horas entre 05 e 08/10 (até 68 consultas em 9 segundos).
    estado, info = _foto_reservar(num)
    if estado == "cache":
        return {"url": info, "cache": True}
    if estado == "teto":
        return {"url": None, "motivo": "teto_horario", "consultas_na_hora": info}

    try:
        url, origem = _foto_url_corretor(num)
    except Exception:
        _foto_devolver_vaga(num, info)
        raise

    # Consulta que NÃO aconteceu (nenhuma instância aberta) não vira cache: senão
    # o painel passa 24h servindo avatar cinza por causa de uma linha que estava
    # fora do ar na hora errada — foi o que aconteceu em 02-03/10/2026.
    if url is None and origem == "sem_instancia":
        _foto_devolver_vaga(num, info)
        return {"url": None, "motivo": "sem_instancia_de_consulta"}

    with SessionLocal() as s:
        s.execute(
            sql_text(
                """
                INSERT INTO contato_foto (telefone, url, checked_at)
                VALUES (:t, :u, now())
                ON CONFLICT (telefone)
                DO UPDATE SET url = EXCLUDED.url, checked_at = now()
                """
            ),
            {"t": num, "u": url},
        )
        s.commit()

    return {"url": url, "origem": origem}


# NOTA (L2 · 26/07): aqui existia uma SEGUNDA declaração de
# @router.post("/conversa/reengajar") — body {"lead_id","gancho"}, SEM checagem
# de opt-out. Por ser a última registrada, era ELA que atendia a rota (o
# FastAPI resolve na ordem de registro, mas o nome no módulo ficava com esta,
# então go-to-definition levava à versão errada). Removida: a sobrevivente é
# `painel_conversa_reengajar_template` (~:4940), que valida texto, limita a 900
# chars e bloqueia 409 em lead_opt_out.


# ── Stop-loss do Disparo (etapa 7 · Revisão Fable, autorizado pela Dona) ─────
# Trava o disparo em lote quando: (a) quality_rating da linha da Pessoa12 sai de GREEN
# (Graph API, cache 10 min) ou (b) 3+ envios com delivery_status=failed no dia.
_STOPLOSS_CACHE: dict[str, Any] = {"at": 0.0, "res": None}


def _disparo_stop_loss() -> dict[str, Any]:
    import os as _os
    import time as _t
    if _os.environ.get("PYTEST_CURRENT_TEST"):
        return {"locked": False, "motivo": None, "quality": "TEST"}
    if _STOPLOSS_CACHE["res"] is not None and _t.monotonic() - _STOPLOSS_CACHE["at"] < 600:
        return _STOPLOSS_CACHE["res"]

    locked, motivo, quality = False, None, None
    # (a) quality rating da linha
    try:
        import httpx as _httpx
        from app.services.cloud_lines import line_by_slug, token_for
        line = line_by_slug("corretor")
        r = _httpx.get(
            f"https://graph.facebook.com/v25.0/{line.phone_id}",
            params={"fields": "quality_rating"},
            headers={"Authorization": f"Bearer {token_for(line)}"},
            timeout=10,
        )
        quality = (r.json() or {}).get("quality_rating")
        if quality and quality.upper() not in ("GREEN", "UNKNOWN"):
            locked = True
            motivo = f"quality rating da linha caiu pra {quality} — disparo pausado até revisão"
    except Exception as e:  # noqa: BLE001 — sem sinal ≠ travado; loga e segue
        log.warning("stoploss_quality_indisponivel", erro=str(e)[:100])

    # (b) falhas de entrega hoje
    if not locked:
        try:
            with SessionLocal() as sess:
                n = sess.execute(sql_text("""
                    SELECT count(*) FROM mensagens_whatsapp
                    WHERE instance='acme_corretora_a' AND from_corretora
                      AND conteudo_metadata->>'delivery_status' = 'failed'
                      AND recebido_em >= date_trunc('day',
                          NOW() AT TIME ZONE 'America/Sao_Paulo') AT TIME ZONE 'America/Sao_Paulo'
                """)).scalar() or 0
            if int(n) >= 3:
                locked = True
                motivo = f"{n} envios falharam hoje na linha da Pessoa12 — disparo pausado até revisão"
        except Exception as e:  # noqa: BLE001
            log.warning("stoploss_failed_count_indisponivel", erro=str(e)[:100])

    res = {"locked": locked, "motivo": motivo, "quality": quality}
    _STOPLOSS_CACHE.update(at=_t.monotonic(), res=res)
    if locked:
        log.warning("disparo_stop_loss_ATIVO", motivo=motivo)
    return res


# ── Anti-ban do Disparo por telefone (2591 recém-recuperado de 3 bans) ────────
# Além do stop-loss global, cada envio real passa por guards por telefone:
# opt-out · cooldown 14d · máx 2 tentativas sem resposta · cap diário · <60s.
# Espaçamento in-process (last_send) evita 2 envios colados na mesma instância.
_DISPARO_LAST_SEND: dict[str, float] = {"at": 0.0}


def _disparo_guard(to_number: str) -> tuple[bool, str | None]:
    """Checa se dá pra disparar reengajamento pro telefone. Retorna (ok, motivo).

    `motivo` é um código estável pro front tratar: "opt_out" | "em_cooldown" |
    "max_tentativas" | "cap_diario_atingido" | "muito_rapido".
    Consulta `disparos_reengajamento` (sufixo de 8 dígitos, mesmo matching do resto).
    """
    from app.config import settings

    suf = to_number[-8:]
    with SessionLocal() as s:
        # opt-out: qualquer registro do telefone marcado opt_out.
        opt = s.execute(
            sql_text(
                r"""SELECT 1 FROM disparos_reengajamento
                    WHERE opt_out = TRUE
                      AND right(regexp_replace(telefone,'\D','','g'),8) = :suf
                    LIMIT 1"""
            ),
            {"suf": suf},
        ).first()
        if opt:
            return False, "opt_out"

        # cooldown: já recebeu disparo nos últimos N dias.
        recente = s.execute(
            sql_text(
                r"""SELECT 1 FROM disparos_reengajamento
                    WHERE right(regexp_replace(telefone,'\D','','g'),8) = :suf
                      AND enviado_em >= now() - (:dias || ' days')::interval
                    LIMIT 1"""
            ),
            {"suf": suf, "dias": settings.disparo_cooldown_dias},
        ).first()
        if recente:
            return False, "em_cooldown"

        # máx tentativas sem resposta.
        sem_resposta = s.execute(
            sql_text(
                r"""SELECT count(*) FROM disparos_reengajamento
                    WHERE right(regexp_replace(telefone,'\D','','g'),8) = :suf
                      AND respondeu_em IS NULL"""
            ),
            {"suf": suf},
        ).scalar() or 0
        if int(sem_resposta) >= settings.disparo_max_tentativas:
            return False, "max_tentativas"

        # cap diário (envios reais hoje, fuso SP).
        hoje = s.execute(
            sql_text(
                """SELECT count(*) FROM disparos_reengajamento
                   WHERE enviado_em >= date_trunc('day',
                       NOW() AT TIME ZONE 'America/Sao_Paulo')
                       AT TIME ZONE 'America/Sao_Paulo'"""
            )
        ).scalar() or 0
        if int(hoje) >= settings.disparo_cap_diario:
            return False, "cap_diario_atingido"

    # espaçamento mínimo entre envios reais (guard in-process).
    import time as _t
    if _t.monotonic() - _DISPARO_LAST_SEND["at"] < settings.disparo_min_intervalo_s:
        return False, "muito_rapido"

    return True, None


def _wamid_do_retorno(ret: Any) -> str | None:
    """Tira o id da mensagem do que o envio devolveu, nos dois formatos.

    O template (`enviar_template_linha`) devolve `wamid` no topo; o texto livre
    (`enviar_cloud`) devolve a resposta crua da Meta, com o id em
    `messages[0].id`. Guardar esse id é o que deixa o placar de entrega achar o
    status da mensagem depois.
    """
    if not isinstance(ret, dict):
        return None
    wamid = ret.get("wamid")
    if not wamid:
        msgs = ret.get("messages") or [{}]
        wamid = (msgs[0] or {}).get("id") if isinstance(msgs, list) else None
    return str(wamid) if wamid else None


def _registrar_disparo(
    *, lead_id: str, corretora_id: int | None, telefone: str,
    template: str | None, variacao: int | None, texto: str | None, autor: str,
    wamid: str | None = None,
) -> None:
    """Registra um disparo real bem-sucedido em disparos_reengajamento (best-effort)."""
    try:
        with SessionLocal() as s:
            s.execute(
                sql_text(
                    """INSERT INTO disparos_reengajamento
                        (lead_id, corretora_id, telefone, template, variacao,
                         texto_enviado, enviado_por, wamid)
                       VALUES (:lid, :cid, :tel, :tmpl, :var, :txt, :por, :wamid)"""
                ),
                {"lid": lead_id, "cid": corretora_id, "tel": telefone,
                 "tmpl": template, "var": variacao, "txt": texto, "por": autor,
                 "wamid": wamid},
            )
            s.commit()
    except Exception as e:  # noqa: BLE001 — registro não pode derrubar o envio já feito
        log.warning("disparo_registro_falha", lead_id=lead_id, erro=str(e)[:160])


@router.get("/disparo/candidatos")
def painel_disparo_candidatos(
    corretora: int | None = Query(None, description="filtro admin: corretora_id"),
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Frios pra reabordar HOJE, POR CORRETORA (leads mornos/quentes fora das 24h,
    com resumo + sugestão prontos do Inspector).

    Escopo (pool multinúmero): cada corretora vê SÓ os frios DELA; admin vê a Pessoa12
    por padrão e pode filtrar com ?corretora=<id>. A trava é por `corretora_id`
    na análise (lead_analyses) e nas mensagens (mensagens_whatsapp).

    Disparo por template (linha Cloud da Pessoa12) só existe pra corretora_id=3:
      • Pessoa12 (acme_corretora_a, Cloud): `pode_disparar=True`, `sugestao` = o {{2}}
        do template WABA (texto EXATO do envio), + preview do template. O botão
        de disparo do front funciona (POST /disparo/enviar).
      • Pessoa11/Pessoa10 (Evolution, SEM template WABA): `pode_disparar=False`,
        `sugestao` = a `abordagem_sugerida` do Inspector (copy pra a corretora
        COPIAR e mandar manual pelo Chat). Sem template/preview. O front esconde
        o botão de disparo e mostra só a lista + copy (o POST /disparo/enviar já
        recusa com `disparo_so_linha_corretor`).
    """
    user = _identificar_usuario(x_painel_user)
    viewer = _viewer_info(user)
    my_cid = viewer["corretora_id"]
    # corretora nunca escolhe filtro — vê só a dela; admin filtra (default Pessoa12=3
    # pra não mudar o comportamento histórico do painel da Dona/Pessoa).
    alvo_cid = my_cid if my_cid is not None else (corretora if corretora is not None else 3)
    is_corretor = alvo_cid == 3

    sql = r"""
        WITH ultima AS (
            SELECT DISTINCT ON (la.lead_id)
                   la.lead_id, la.temperatura, la.raw_analysis, la.analyzed_at
            FROM lead_analyses la
            WHERE la.corretora_id = :cid
            ORDER BY la.lead_id, la.analyzed_at DESC
        ),
        lastmsg AS (
            SELECT DISTINCT ON (p8) *
            FROM (
                SELECT right(regexp_replace(contato_telefone,'\D','','g'),8) AS p8,
                       timestamp_evolution AS last_any,
                       from_corretora AS last_de_corretora,
                       left(coalesce(nullif(transcricao,''), conteudo_texto, ''), 200) AS last_texto
                FROM mensagens_whatsapp
                WHERE corretora_id = :cid AND contato_telefone IS NOT NULL
            ) t
            ORDER BY p8, last_any DESC
        )
        SELECT u.lead_id, ls.nome, ls.telefone, u.temperatura,
               u.raw_analysis->>'conversa_resumo'    AS resumo,
               u.raw_analysis->>'abordagem_sugerida' AS sugestao,
               lm.last_any, lm.last_de_corretora, lm.last_texto
        FROM ultima u
        JOIN leads_snapshot ls ON ls.lead_id = u.lead_id
        LEFT JOIN lastmsg lm
               ON lm.p8 = right(regexp_replace(COALESCE(ls.telefone,''),'\D','','g'),8)
        WHERE u.raw_analysis->>'analysis_mode' = 'conversa_whatsapp'
          AND u.temperatura IN ('morno','quente')
          AND COALESCE(ls.etapa_kommo,'') NOT IN ('142','143')
          AND (lm.last_any IS NULL OR lm.last_any < NOW() - INTERVAL '24 hours')
        ORDER BY (u.temperatura = 'quente') DESC, lm.last_any DESC NULLS LAST
        LIMIT 60
    """
    with SessionLocal() as s:
        rows = s.execute(sql_text(sql), {"cid": alvo_cid}).mappings().all()

    from datetime import timezone as _tz
    agora = datetime.now(_tz.utc)
    # Linha Cloud (só Pessoa12) tem template/variação; import tardio (registry pesado).
    variacao_disparo = preview_template_corretor = None
    if is_corretor:
        from app.api.cloud_corretora_a import preview_template_corretor, variacao_disparo
    out = []
    for r in rows:
        last = r["last_any"]
        dias = None
        if last is not None:
            if last.tzinfo is None:
                last = last.replace(tzinfo=_tz.utc)
            dias = int((agora - last).total_seconds() // 86400)
        tel = (r["telefone"] or "")
        _titulos = {"sr", "sra", "dr", "dra", "pra", "pr", "exmo", "ilmo", "sto", "sta"}
        _toks = [t for t in (r["nome"] or "").split() if t.lower().strip(".") not in _titulos]
        primeiro = _toks[0] if _toks else "tudo bem"

        if is_corretor:
            # Linha da Pessoa12: o {{2}} do template WABA rotaciona entre 3 variações por
            # lead (determinístico por telefone). Texto que a corretora aprova E que
            # o lead recebe (bate com o envio). Preview EXATO do envio (etapa 7 Fable).
            var_idx, msg = variacao_disparo(tel or primeiro)
            pv = preview_template_corretor(tel, primeiro) if tel else {"template": "", "skeleton": "{{2}}"}
            sugestao = msg
            variacao = var_idx
            template = pv["template"]
            template_skeleton = pv["skeleton"]
            pode_disparar = True
        else:
            # Pessoa11/Pessoa10 (Evolution, sem template): a sugestão é a abordagem do
            # Inspector — copy pra COPIAR e mandar manual pelo Chat. Sem disparo
            # automático (linha delas não tem template WABA).
            sugestao = r["sugestao"] or ""
            variacao = None
            template = ""
            template_skeleton = ""
            pode_disparar = False

        out.append({
            "lead_id": r["lead_id"],
            "nome": r["nome"],
            "telefone_mask": ("…" + "".join(c for c in tel if c.isdigit())[-4:]) if tel else "",
            # telefone completo: a corretora precisa reconhecer o lead dela pra decidir
            # aprovar/pular (ex: "já falei com essa por fora"). É lead dela — sem privacy issue.
            "telefone": r["telefone"] or "",
            "temperatura": r["temperatura"],
            "resumo": r["resumo"],
            "sugestao": sugestao,
            "variacao": variacao,
            "dias_parado": dias,
            "ultima_msg": {
                "de": ("corretora" if r["last_de_corretora"] else "lead") if r["last_any"] is not None else None,
                "texto": r["last_texto"] or "",
                "quando": last.isoformat() if last is not None else None,
            },
            "template": template,
            "template_skeleton": template_skeleton,
            # front: True → botão de disparo (Pessoa12/Cloud); False → só copy manual.
            "pode_disparar": pode_disparar,
        })
    return {
        "candidatos": out,
        "total": len(out),
        "corretora_id": alvo_cid,
        # linha só-copy (Evolution, sem template WABA) → o front esconde o botão
        # de disparo e mostra "copiar sugestão + enviar manual pelo Chat".
        "disparo_disponivel": is_corretor,
        "stop_loss": _disparo_stop_loss() if is_corretor else None,
    }


@router.post("/disparo/enviar")
def painel_disparo_enviar(
    payload: dict[str, Any] = Body(...),
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Dispara a mensagem aprovada pra um lead da Pessoa12. Lead frio (fora 24h) → template
    WABA ({{2}} = texto aprovado). Dentro de 24h → texto livre pela Cloud.
    """
    user = _identificar_usuario(x_painel_user)
    viewer = _viewer_info(user)
    my_cid = viewer["corretora_id"]

    lead_id = str(payload.get("lead_id") or "").strip()
    texto = str(payload.get("texto") or "").strip()
    if not lead_id:
        raise HTTPException(422, "lead_id_obrigatorio")
    if not texto:
        raise HTTPException(422, "texto_vazio")

    ctx = _lead_conversa_contexto(lead_id)
    if my_cid is not None and my_cid != ctx["corretora_id"]:
        raise HTTPException(403, "lead_de_outra_corretora")
    if ctx["instance"] != "acme_corretora_a":
        raise HTTPException(400, "disparo_so_linha_corretor")

    # Stop-loss (etapa 7): quality rating fora de GREEN ou 3+ falhas hoje → trava.
    sl = _disparo_stop_loss()
    if sl["locked"]:
        raise HTTPException(423, f"disparo_travado: {sl['motivo']}")

    from app.config import settings
    to_number = ctx["telefone"].lstrip("+").replace(" ", "").replace("-", "")

    # Anti-ban por telefone (2591 recém-recuperado): opt-out · cooldown 14d ·
    # máx tentativas · cap diário · espaçamento <60s. Front pula o card no 409.
    ok, motivo = _disparo_guard(to_number)
    if not ok:
        log.info("painel_disparo_bloqueado", lead_id=lead_id,
                 to_last4=to_number[-4:], motivo=motivo, user=user)
        raise HTTPException(409, motivo or "disparo_bloqueado")

    # dentro de 24h? (última mensagem qualquer < 24h → texto livre)
    with SessionLocal() as s:
        last = s.execute(
            sql_text(
                r"""SELECT max(timestamp_evolution) FROM mensagens_whatsapp
                    WHERE corretora_id = 3
                      AND right(regexp_replace(contato_telefone,'\D','','g'),8) = :suf"""
            ),
            {"suf": to_number[-8:]},
        ).scalar()
    dentro_24h = False
    if last is not None:
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        dentro_24h = (datetime.now(timezone.utc) - last).total_seconds() < 86400

    if settings.dry_run:
        log.info("painel_disparo_dry_run", lead_id=lead_id,
                 to_last4=to_number[-4:], dentro_24h=dentro_24h, user=user)
        return {"ok": True, "dry_run": True, "via": "livre" if dentro_24h else "template"}

    from app.api.cloud_corretora_a import (
        preview_template_corretor, variacao_disparo,
    )
    var_idx, _var_txt = variacao_disparo(to_number)
    templ = None
    try:
        # Linha de saída pela DONA do lead — o disparo pode ter candidato de
        # qualquer corretora, e antes tudo saía pela linha da Pessoa12 (lead da Pessoa10
        # receberia do número errado e responderia pra pessoa errada).
        from app.services import cloud_lines as _cl_disp

        _linha_d = _cl_disp.linha_da_corretora(ctx.get("corretora_id"))
        if _linha_d is _cl_disp.INDETERMINADO:
            _linha_d = None
        if dentro_24h:
            from app.api.cloud_corretora_a import enviar_cloud
            _ret = enviar_cloud(to_number, texto, linha=_linha_d,
                                instance=ctx.get("instance"),
                                corretora_id=ctx.get("corretora_id"))
            via = "livre"
        else:
            # Lead frio → SEM botão (pool ola). Tira saudação do texto.
            import re as _re
            from app.api.cloud_corretora_a import enviar_template_corretor
            first = (ctx.get("lead_nome") or "").strip()
            corpo = _re.sub(r"^\s*(ol[áa]|oi)\b[^.!?\n]*[.!?]\s*", "", texto,
                            count=1, flags=_re.I).strip() or texto
            _kwargs_tmpl = dict(hook=corpo, com_botao=False)
            if _linha_d is not None:
                from app.api.cloud_corretora_a import enviar_template_linha
                _ret = enviar_template_linha(
                    _linha_d, to_number, first,
                    instance=ctx.get("instance"),
                    corretora_id=ctx.get("corretora_id"),
                    **_kwargs_tmpl,
                )
            else:
                _ret = enviar_template_corretor(to_number, first, **_kwargs_tmpl)
            templ = (preview_template_corretor(to_number, first) or {}).get("template")
            via = "template"
    except Exception as e:  # noqa: BLE001
        log.error("painel_disparo_falha", lead_id=lead_id, erro=str(e)[:200])
        raise HTTPException(502, "disparo_falhou") from e

    # Registro do disparo (anti-ban se apoia nele: cooldown, tentativas, cap).
    _DISPARO_LAST_SEND["at"] = __import__("time").monotonic()
    _registrar_disparo(
        lead_id=lead_id, corretora_id=ctx.get("corretora_id"), telefone=to_number,
        template=templ, variacao=var_idx if not dentro_24h else None,
        texto=texto, autor=user or "", wamid=_wamid_do_retorno(_ret),
    )

    log.info("painel_disparo_enviado", lead_id=lead_id,
             to_last4=to_number[-4:], via=via, user=user)
    return {"ok": True, "dry_run": False, "via": via}


_MIDIA_MAX_BYTES = 16 * 1024 * 1024  # 16 MB (limite prático WhatsApp)


@router.post("/conversa/enviar-midia")
async def painel_conversa_enviar_midia(
    lead_id: str = Form(...),
    tipo: str = Form(...),                 # "image" | "document" | "audio"
    arquivo: UploadFile = File(...),
    legenda: str = Form(""),
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Envia mídia (foto/pdf/documento/áudio) ao lead PELA LINHA da corretora.

    Mesmo contrato de privacidade e DRY_RUN do /conversa/enviar. Roteia Cloud
    (Pessoa3) x Evolution conforme a instância dona do card.
    """
    import base64 as _b64

    user = _identificar_usuario(x_painel_user)
    viewer = _viewer_info(user)
    my_cid = viewer["corretora_id"]

    if tipo not in ("image", "document", "audio"):
        raise HTTPException(422, "tipo_invalido")

    ctx = _lead_conversa_contexto(lead_id, viewer_cid=my_cid)
    if not _pode_atender(my_cid, ctx["corretora_id"]):
        raise HTTPException(403, "lead_de_outra_corretora")

    conteudo = await arquivo.read()
    if not conteudo:
        raise HTTPException(422, "arquivo_vazio")
    if len(conteudo) > _MIDIA_MAX_BYTES:
        raise HTTPException(413, "arquivo_muito_grande")

    mimetype = arquivo.content_type or (
        "audio/ogg" if tipo == "audio"
        else "image/jpeg" if tipo == "image"
        else "application/octet-stream"
    )
    filename = arquivo.filename or f"arquivo.{mimetype.split('/')[-1]}"
    legenda = (legenda or "").strip()[:1024]

    from app.config import settings
    to_number = ctx["telefone"].lstrip("+").replace(" ", "").replace("-", "")

    if settings.dry_run:
        log.info("painel_conversa_midia_dry_run", lead_id=lead_id,
                 instance=ctx["instance"], tipo=tipo, bytes=len(conteudo), user=user)
        return {"ok": True, "dry_run": True, "tipo": tipo,
                "enviado_por_instance": ctx["instance"]}

    # Mesma regra do /conversa/enviar: se a dona do lead tem linha Cloud, a mídia
    # TEM que sair por Cloud. Antes só a Pessoa12 (acme_corretora_a) ia por Cloud e o
    # resto caía no Evolution — a Pessoa7 não tem instância lá e a foto dava 404
    # (24/09). INDETERMINADO → caminho histórico (Pessoa12 por Cloud, resto Evolution).
    from app.services import cloud_lines as _cl_midia

    linha_midia = _cl_midia.linha_da_corretora(ctx["corretora_id"])
    if linha_midia is _cl_midia.INDETERMINADO:
        linha_midia = None

    resposta_envio: dict[str, Any] | None = None
    try:
        if linha_midia is not None or ctx["instance"] == "acme_corretora_a":
            from app.api.cloud_corretora_a import enviar_cloud_midia
            resposta_envio = enviar_cloud_midia(
                to_number, conteudo, mimetype=mimetype,
                filename=filename, tipo=tipo, caption=legenda,
                linha=linha_midia, instance=ctx["instance"],
                corretora_id=ctx["corretora_id"])
        else:
            from app.integrations.evolution_client import EvolutionClient
            cli = EvolutionClient()
            b64 = _b64.b64encode(conteudo).decode()
            if tipo == "audio":
                resposta_envio = cli.send_audio(ctx["instance"], to_number, b64)
            else:
                resposta_envio = cli.send_media(
                    ctx["instance"], to_number, b64,
                    mediatype=tipo, mimetype=mimetype,
                    file_name=filename, caption=legenda)
    except Exception as e:  # noqa: BLE001
        log.error("painel_conversa_midia_falha", lead_id=lead_id,
                  instance=ctx["instance"], erro=str(e)[:160])
        raise HTTPException(502, "envio_falhou") from e

    # GUARDA O ARQUIVO QUE ACABAMOS DE MANDAR (06/08).
    # Achado: 123 mídias não abriam no chat — TODAS enviadas pela corretora,
    # nenhuma recebida. O motivo é que a mídia de saída não deixa rastro de onde
    # buscar depois: a resposta da Cloud só devolve o wamid (sem media_id), e a
    # do Evolution depende de uma sessão Baileys que na Pessoa12 vive caindo. Ou seja,
    # a corretora mandava o PDF e dois minutos depois não conseguia reabrir.
    # Guardar os bytes aqui resolve pra sempre e de graça: o GET /conversa/midia
    # olha este cache ANTES de tentar qualquer API externa.
    _cachear_midia_enviada(resposta_envio, conteudo, mimetype)

    log.info("painel_conversa_midia_enviado", lead_id=lead_id,
             instance=ctx["instance"], tipo=tipo, bytes=len(conteudo), user=user)
    return {"ok": True, "dry_run": False, "tipo": tipo,
            "enviado_por_instance": ctx["instance"]}


# ── Cadência Pessoa12 (P2) — visão do painel ────────────────────────────────────────


@router.get("/cadencia")
def painel_cadencia(
    status: str | None = Query(None, description="filtro: ativa | remarketing | opt_out"),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Visão em tempo real da cadência híbrida (8 toques D0-D14 + remarketing).

    Resumo (ativas / concluídas→remarketing / opt-out / enviadas hoje) + lista
    paginada dos leads matriculados com toque atual, próximo toque, status e os
    últimos eventos (mensagens enfileiradas / tarefas de ligação).

    Admin-only: dado agregado de disparo (mesma classe de ranking/histórico).
    Telefone vai COMPLETO — painel interno atrás de basic auth.
    """
    from app.config import settings
    from app.services.cadencia import TIPO_MENSAGEM, TIPO_TAREFA_LIGACAO, TOQUES

    user = _identificar_usuario(x_painel_user)
    _require_admin(user)

    from app.db.models import CadenciaEstado, CadenciaEvento, MensagemWhatsapp
    from sqlalchemy import func as sa_func

    if status is not None and status not in ("ativa", "concluida", "respondida", "remarketing", "opt_out"):
        raise HTTPException(400, "status inválido: use ativa | concluida | respondida | remarketing | opt_out")

    inicio_dia_sp = datetime.now(SP_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    inicio_dia_utc = inicio_dia_sp.astimezone(timezone.utc)

    with SessionLocal() as s:
        # ── resumo ──
        por_status: dict[str, int] = {
            st: n
            for st, n in s.query(CadenciaEstado.status, sa_func.count(CadenciaEstado.id))
            .group_by(CadenciaEstado.status)
            .all()
        }
        eventos_hoje = (
            s.query(CadenciaEvento.tipo, sa_func.count(CadenciaEvento.id))
            .filter(CadenciaEvento.at >= inicio_dia_utc)
            .group_by(CadenciaEvento.tipo)
            .all()
        )
        por_tipo_hoje = {t: n for t, n in eventos_hoje}

        # respostas hoje: leads matriculados na cadência que mandaram mensagem
        # inbound hoje (match por sufixo de 9 dígitos — formatos variam entre
        # Kommo/Evolution; mesma heurística _norm9 do vazamento).
        tels_cadencia = {
            _norm9(t) for (t,) in s.query(CadenciaEstado.telefone).all() if t
        }
        respostas_hoje = 0
        if tels_cadencia:
            inbound_hoje = (
                s.query(MensagemWhatsapp.contato_telefone)
                .filter(
                    MensagemWhatsapp.from_corretora.is_(False),
                    MensagemWhatsapp.timestamp_evolution >= inicio_dia_utc,
                    MensagemWhatsapp.contato_telefone.isnot(None),
                )
                .distinct()
                .all()
            )
            respostas_hoje = len(
                {_norm9(t) for (t,) in inbound_hoje} & tels_cadencia
            )

        # ── lista paginada ──
        q = s.query(CadenciaEstado)
        if status is not None:
            q = q.filter(CadenciaEstado.status == status)
        total_filtrado = q.count()
        rows = (
            q.order_by(
                CadenciaEstado.proximo_toque_em.asc().nullslast(),
                CadenciaEstado.id.asc(),
            )
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )

        # últimos eventos das cadências da página (1 query, agrupa em Python)
        eventos_por_cadencia: dict[int, list[dict[str, Any]]] = {}
        if rows:
            ids = [r.id for r in rows]
            evs = (
                s.query(CadenciaEvento)
                .filter(CadenciaEvento.cadencia_id.in_(ids))
                .order_by(CadenciaEvento.at.desc(), CadenciaEvento.id.desc())
                .all()
            )
            for ev in evs:
                bucket = eventos_por_cadencia.setdefault(ev.cadencia_id, [])
                if len(bucket) >= 3:  # só os 3 últimos por lead
                    continue
                payload = ev.payload or {}
                bucket.append({
                    "toque": ev.toque,
                    "tipo": ev.tipo,
                    "canal": ev.canal,
                    "at": _iso_utc(ev.at),
                    "dry_run": bool(payload.get("dry_run")),
                    "sent": bool(payload.get("sent")),
                    "remarketing": bool(payload.get("remarketing")),
                    "texto": (payload.get("texto") or "")[:160] or None,
                })

        itens: list[dict[str, Any]] = []
        for r in rows:
            prox = None
            if r.status == "ativa" and r.toque_atual < len(TOQUES):
                t = TOQUES[r.toque_atual]
                prox = {"indice": t.indice, "dia": f"D{t.offset_dias}",
                        "tipo": t.tipo, "canal": t.canal, "categoria": t.categoria}
            elif r.status == "remarketing":
                prox = {"indice": None, "dia": "D+30", "tipo": TIPO_MENSAGEM,
                        "canal": "whatsapp", "categoria": "utility"}
            itens.append({
                "cadencia_id": r.id,
                "lead_id": r.lead_id,
                "telefone": r.telefone,
                "corretora_id": r.corretora_id,
                "corretora_label": CORRETORA_LABEL.get(r.corretora_id) if r.corretora_id else None,
                "status": r.status,
                "toque_atual": r.toque_atual,
                "total_toques": len(TOQUES),
                "proximo_toque": prox,
                "proximo_toque_em": _iso_utc(r.proximo_toque_em),
                "matriculado_em": _iso_utc(r.criado_em),
                "ultimos_eventos": eventos_por_cadencia.get(r.id, []),
            })

    total_geral = sum(por_status.values())
    return {
        "resumo": {
            "total": total_geral,
            "ativas": por_status.get("ativa", 0),
            # "concluída" = passou pelos 8 toques; remarketing = ciclo 30/30d
            "concluidas": por_status.get("concluida", 0),
            # respondida = lead respondeu e a cadência PAROU (hook do inbound)
            "respondidas": por_status.get("respondida", 0),
            "concluidas_remarketing": por_status.get("remarketing", 0),
            "opt_out": por_status.get("opt_out", 0),
            "enviadas_hoje": por_tipo_hoje.get(TIPO_MENSAGEM, 0),
            "tarefas_ligacao_hoje": por_tipo_hoje.get(TIPO_TAREFA_LIGACAO, 0),
            "respostas_hoje": respostas_hoje,
            "dry_run": bool(settings.dry_run),
            "cadencia_ativa": bool(settings.cadencia_ativa),
        },
        "page": page,
        "page_size": page_size,
        "total_filtrado": total_filtrado,
        "itens": itens,
    }


@router.get("/cadencia/sugestoes")
def painel_sugestoes_listar(
    status: str = Query("pendente", description="pendente | aprovada | rejeitada | usada | expirada | sombra"),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Fila de aprovação do follow-up com IA.

    A IA leu a conversa real do lead e escreveu a variável {{2}} do template
    UTILITY. Nada sai antes de alguém aprovar aqui — decisão da Dona (29/07):
    "ninguém manda pro cliente da Acme um texto que nenhuma pessoa leu".

    Cada item traz o PAR (o que sairia hoje × o que a IA propõe) e o preview
    montado com a casca do template, que é o que o lead de fato veria.

    Admin-only.
    """
    from app.services import sugestao_cadencia as sug

    user = _identificar_usuario(x_painel_user)
    _require_admin(user)

    if status not in sug.STATUS_VALIDOS:
        raise HTTPException(status_code=400, detail="status_invalido")

    off = (page - 1) * page_size
    with SessionLocal() as s:
        total = s.execute(
            sql_text("SELECT count(*) FROM shadow_cadencia WHERE status = :st"),
            {"st": status},
        ).scalar() or 0
        rows = s.execute(
            sql_text(
                """
                SELECT id, corretora_id, lead_nome, lead_telefone, toque,
                       template_atual, sugestao_ia, contexto_usado, motivo,
                       texto_final, aprovado_por, aprovado_em, usada_em,
                       modelo, created_at
                  FROM shadow_cadencia
                 WHERE status = :st
                 ORDER BY created_at DESC
                 LIMIT :lim OFFSET :off
                """
            ),
            {"st": status, "lim": page_size, "off": off},
        ).mappings().all()

        # contagem por status pras abas da tela
        contagem = {
            r[0]: r[1]
            for r in s.execute(
                sql_text("SELECT status, count(*) FROM shadow_cadencia GROUP BY 1")
            ).all()
        }

    from app.api.automacoes import _preview_whatsapp

    itens = []
    for r in rows:
        nome = (r["lead_nome"] or "").split(" ")[0] or "{nome}"
        itens.append({
            "id": r["id"],
            "corretora_id": r["corretora_id"],
            "lead_nome": r["lead_nome"],
            "lead_telefone": r["lead_telefone"],
            "toque": r["toque"],
            "template_atual": r["template_atual"],
            "sugestao_ia": r["sugestao_ia"],
            "contexto_usado": r["contexto_usado"],
            "motivo": r["motivo"],
            "texto_final": r["texto_final"],
            "aprovado_por": r["aprovado_por"],
            "aprovado_em": r["aprovado_em"].isoformat() if r["aprovado_em"] else None,
            "usada_em": r["usada_em"].isoformat() if r["usada_em"] else None,
            "modelo": r["modelo"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            # o que o lead veria, com a casca do template UTILITY
            "preview_hoje": _preview_whatsapp(r["template_atual"] or "", com_botao=False, nome_lead=nome),
            "preview_ia": _preview_whatsapp(r["sugestao_ia"] or "", com_botao=False, nome_lead=nome),
        })

    return {
        "status": status,
        "page": page,
        "page_size": page_size,
        "total": total,
        "contagem_por_status": contagem,
        "itens": itens,
    }


@router.post("/cadencia/sugestoes/{sugestao_id}/aprovar")
def painel_sugestao_aprovar(
    sugestao_id: int,
    payload: dict[str, Any] | None = None,
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Aprova a sugestão — a cadência vai usar esse texto na variável.

    `payload.texto` (opcional) permite EDITAR antes de aprovar. O que a IA
    escreveu fica guardado em `sugestao_ia`, o que foi aprovado vai pra
    `texto_final` — assim dá pra medir se a IA melhora ou se é a humana
    consertando.

    Aprovar NÃO envia nada: só libera pro próximo toque devido daquele lead.
    """
    from app.services import sugestao_cadencia as sug

    user = _identificar_usuario(x_painel_user)
    _require_admin(user)

    texto = (payload or {}).get("texto")
    if texto is not None and not isinstance(texto, str):
        raise HTTPException(status_code=400, detail="texto_invalido")

    ok = sug.aprovar(sugestao_id, por=user, texto_editado=texto)
    if not ok:
        # já aprovada/rejeitada, inexistente, ou edição fora do limite
        raise HTTPException(status_code=409, detail="sugestao_nao_pendente_ou_texto_invalido")
    return {"ok": True, "id": sugestao_id, "aprovado_por": user}


@router.post("/cadencia/sugestoes/{sugestao_id}/rejeitar")
def painel_sugestao_rejeitar(
    sugestao_id: int,
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Rejeita — o lead recebe o template de sempre. Zero risco."""
    from app.services import sugestao_cadencia as sug

    user = _identificar_usuario(x_painel_user)
    _require_admin(user)

    if not sug.rejeitar(sugestao_id, por=user):
        raise HTTPException(status_code=409, detail="sugestao_nao_pendente")
    return {"ok": True, "id": sugestao_id, "rejeitado_por": user}


@router.get("/crm/board")
def painel_crm_board(
    corretora: int | None = Query(None, description="filtro admin: corretora_id"),
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Kanban do CRM 2.0 — lê a espinha canônica (leads / pipeline_stages /
    lead_events, tenant Acme) em vez do Kommo/Twenty.

    Colunas = pipeline_stages na ordem; cards = nome, telefone, valor, origem,
    corretora. Se pipeline_stages do tenant estiver vazia (espinha ainda não
    sincronizada), deriva colunas dos stage_id distintos dos leads e marca
    `stages_derivados=True`. Leads sem stage caem na coluna "Sem etapa".

    Escopo: corretora vê os leads dela (owner_id) e os das corretoras que ela
    atende por delegação (`DELEGACAO_ATENDIMENTO` — a Pessoa12 vê os da Pessoa7 e da
    Corretor); o card traz `corretora_id`/`corretora_label` pra dizer de quem é.
    `?corretora=<id>` filtra: admin escolhe qualquer uma, corretora só DENTRO
    do que já pode ver (fora disso o parâmetro é ignorado — nunca amplia).
    Somente leitura — sem drag-drop nessa v1.
    """
    from app.config import settings
    from app.db.models import CadenciaEstado, CadenciaEvento, Lead, LeadEvent, PipelineStage
    from app.services.lead_spine import DEFAULT_TENANT_ID
    from app.services.phone import normalize_phone_kommo
    from sqlalchemy import func as sa_func
    from sqlalchemy import or_ as sa_or

    user = _identificar_usuario(x_painel_user)
    my_cid = _corretora_id_do_user(user)
    is_admin = _is_admin(user)
    if my_cid is None and not is_admin:
        raise HTTPException(403, "painel_user_sem_corretora")
    # privacidade entre corretoras: quem não é admin vê a dela + as delegadas
    # (None = admin, sem escopo). O filtro só RECORTA esse escopo, nunca amplia.
    cids_visiveis = _cids_atendimento(my_cid)
    filter_cid, owner_ids_escopo = _escopo_board(cids_visiveis, corretora)

    tenant = DEFAULT_TENANT_ID

    with SessionLocal() as s:
        stages = (
            s.query(PipelineStage)
            .filter(PipelineStage.tenant_id == tenant)
            # segundo funil (0046): o board principal só mostra funil='principal';
            # as etapas de remarketing vivem no /crm/remarketing/board.
            .filter(PipelineStage.funil == "principal")
            .order_by(PipelineStage.ordem.asc().nullslast(), PipelineStage.id.asc())
            .all()
        )

        q = s.query(Lead).filter(Lead.tenant_id == tenant)

        # leads que já estão no funil de remarketing (nutrição) saem do board
        # principal — senão apareceriam como coluna órfã "Etapa {id}".
        ids_remarketing = [
            sid
            for (sid,) in s.query(PipelineStage.id)
            .filter(
                PipelineStage.tenant_id == tenant,
                PipelineStage.funil != "principal",
            )
            .all()
        ]
        if ids_remarketing:
            q = q.filter(
                sa_or(Lead.stage_id.is_(None), Lead.stage_id.notin_(ids_remarketing))
            )

        # Trava do piloto (item 2 blueprint): só corretoras com CRM ativo
        # aparecem no board (default = só Pessoa12). A sombra segue gravando TODAS —
        # a trava é de EXIBIÇÃO. owner_id na espinha pode ser o id interno
        # (inspector) OU o kommo_user_id (webhook fino) — aceita os dois.
        crm_ativas = set(settings.crm_corretoras_ativas or [])
        if crm_ativas:
            owner_ids_permitidos = set(crm_ativas)
            # SAVEPOINT (padrão _veio_de_intake): falha aqui (ex: DB de teste
            # sem a tabela) degrada sem poisonar a sessão do board
            try:
                from app.db.models import Corretora
                with s.begin_nested():
                    for (kuid,) in (
                        s.query(Corretora.kommo_user_id)
                        .filter(
                            Corretora.id.in_(crm_ativas),
                            Corretora.kommo_user_id.isnot(None),
                        )
                        .all()
                    ):
                        owner_ids_permitidos.add(int(kuid))
            except Exception:
                log.exception("crm_board_corretoras_ativas_lookup_failed")
            q = q.filter(Lead.owner_id.in_(owner_ids_permitidos))

        if owner_ids_escopo is not None:
            q = q.filter(Lead.owner_id.in_(owner_ids_escopo))
        leads = q.order_by(
            Lead.updated_at.desc().nullslast(), Lead.id.desc()
        ).all()

        # última atividade por lead (lead_events) — 1 query agregada
        ultimo_evento: dict[int, datetime] = {}
        if leads:
            ids = [l.id for l in leads]
            for lead_id, at_max in (
                s.query(LeadEvent.lead_id, sa_func.max(LeadEvent.at))
                .filter(LeadEvent.tenant_id == tenant, LeadEvent.lead_id.in_(ids))
                .group_by(LeadEvent.lead_id)
                .all()
            ):
                if lead_id is not None and at_max is not None:
                    ultimo_evento[lead_id] = at_max

        # cadência invisível: tag CD{toque_atual+1} no card quando a cadência
        # do telefone está ATIVA ou RESPONDIDA (regra Dona 10/07: a tag CD fica
        # DEPOIS da resposta — congela no toque que recuperou o lead; o badge 🟢
        # aparece JUNTO dela). Concluída/remarketing/opt-out → sem tag.
        # Join por telefone normalizado — 1 query agregada.
        cadencia_por_fone: dict[str, int] = {}
        fones_norm = {
            fone
            for l in leads
            if l.telefone
            for fone in [normalize_phone_kommo(l.telefone)]
            if fone
        }
        if fones_norm:
            # best-effort: a tag é acessória — falha aqui NUNCA derruba o board
            # (SAVEPOINT pra não poisonar a sessão — padrão _veio_de_intake)
            try:
                with s.begin_nested():
                    for telefone, toque_atual in (
                        s.query(CadenciaEstado.telefone, CadenciaEstado.toque_atual)
                        .filter(
                            CadenciaEstado.tenant_id == tenant,
                            CadenciaEstado.status.in_(("ativa", "respondida")),
                            CadenciaEstado.telefone.in_(fones_norm),
                        )
                        .all()
                    ):
                        cadencia_por_fone[telefone] = int(toque_atual or 0)
            except Exception:
                log.exception("crm_board_cadencia_toque_failed")

        # respondeu na cadência: telefone → timestamp do ÚLTIMO evento
        # tipo='resposta' (status 'respondida'). O front mostra o badge verde
        # "🟢 respondeu" JUNTO da tag CD. Best-effort: falha nunca derruba o board.
        respondeu_por_fone: dict[str, datetime] = {}
        if fones_norm:
            try:
                with s.begin_nested():
                    for telefone, at_max in (
                        s.query(CadenciaEstado.telefone, sa_func.max(CadenciaEvento.at))
                        .join(CadenciaEvento, CadenciaEvento.cadencia_id == CadenciaEstado.id)
                        .filter(
                            CadenciaEstado.tenant_id == tenant,
                            CadenciaEstado.status == "respondida",
                            CadenciaEstado.telefone.in_(fones_norm),
                            CadenciaEvento.tipo == "resposta",
                        )
                        .group_by(CadenciaEstado.telefone)
                        .all()
                    ):
                        if telefone and at_max is not None:
                            respondeu_por_fone[telefone] = at_max
            except Exception:
                log.exception("crm_board_cadencia_respondeu_failed")

        # último OUTBOUND da corretora por telefone (from_corretora=true,
        # instance da corretora dona do CRM = Pessoa12). Usado pra ZERAR o
        # `respondeu_em` quando a corretora JÁ retornou o lead: se ela mandou
        # msg DEPOIS da última resposta do lead → o lead foi atendido → a tag
        # 🟢 e o contador do menu somem (reclamação Pessoa12 17/07 — a tag ficava pra
        # sempre até expirar as 48h, sem jeito de zerar atendendo).
        # `contato_telefone` vem cru do JID → normaliza em Python e casa com
        # `fones_norm` (mesmo padrão de campanha_por_fone). 1 query agregada.
        # Best-effort: falha nunca derruba o board.
        ultimo_outbound_por_fone: dict[str, datetime] = {}
        if fones_norm:
            try:
                with s.begin_nested():
                    rows = s.execute(
                        sql_text(
                            """
                            SELECT contato_telefone, max(timestamp_evolution) AS ult
                            FROM mensagens_whatsapp
                            WHERE tenant_id = :tenant
                              AND from_corretora = true
                              AND instance = 'acme_corretora_a'
                              AND contato_telefone IS NOT NULL
                            GROUP BY contato_telefone
                            """
                        ),
                        {"tenant": tenant},
                    ).all()
                    for telefone, ult in rows:
                        fn = normalize_phone_kommo(telefone)
                        if not fn or fn not in fones_norm or ult is None:
                            continue
                        atual = ultimo_outbound_por_fone.get(fn)
                        if atual is None or ult > atual:
                            ultimo_outbound_por_fone[fn] = ult
            except Exception:
                log.exception("crm_board_ultimo_outbound_failed")

        # tem_tarefa: leads com tarefa ABERTA em tarefas_corretora. A tarefa
        # casa por lead_id do KOMMO = leads.external_ref (a tarefa é criada com
        # o id do Kommo). 1 query agregada — próxima tarefa (menor data/hora)
        # por lead pro title do badge. Best-effort: falha nunca derruba o board.
        tarefa_por_ref: dict[str, str | None] = {}
        refs = [l.external_ref for l in leads if l.external_ref]
        if refs:
            try:
                with s.begin_nested():
                    rows = s.execute(
                        sql_text(
                            """
                            SELECT lead_id,
                                   min(data_tarefa + COALESCE(hora_tarefa, time '00:00'))
                                       AS proxima
                            FROM tarefas_corretora
                            WHERE tenant_id = :tenant
                              AND status = 'aberta'
                              AND lead_id = ANY(:refs)
                            GROUP BY lead_id
                            """
                        ),
                        {"tenant": tenant, "refs": refs},
                    ).all()
                    for ref, proxima in rows:
                        tarefa_por_ref[ref] = (
                            proxima.isoformat() if proxima is not None else None
                        )
            except Exception:
                log.exception("crm_board_tem_tarefa_failed")

        # campanha: origem publicitária do lead (Meta lead ads). O board lê a
        # espinha canônica (Kommo) que NÃO guarda campaign_name; o dado vive na
        # base do Hunter (yashav.leads.campaign_name), casada por telefone
        # normalizado. 1 query agregada. Best-effort: falha nunca derruba o board.
        # Hoje pode não haver overlap → campanha = null (só origem no card).
        campanha_por_fone: dict[str, str] = {}
        if fones_norm:
            try:
                with s.begin_nested():
                    rows = s.execute(
                        sql_text(
                            "SELECT telefone, campaign_name FROM yashav.leads "
                            "WHERE campaign_name IS NOT NULL AND campaign_name <> ''"
                        )
                    ).all()
                    for telefone, campaign_name in rows:
                        fn = normalize_phone_kommo(telefone)
                        if fn and fn in fones_norm and fn not in campanha_por_fone:
                            campanha_por_fone[fn] = campaign_name
            except Exception:
                log.exception("crm_board_campanha_failed")

    agora_utc = datetime.now(timezone.utc)

    def _tz(dt: datetime | None) -> datetime | None:
        if dt is None:
            return None
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt

    # Resumo por IA do que a pessoa disse na linha da Corretor (05/10/2026): só
    # cards que passaram pelo handoff têm. Uma consulta pro quadro todo; falha
    # vira "sem resumo" no card, nunca quadro quebrado.
    from app.services import ia_do_lado as _ia

    with SessionLocal() as s3:
        resumos_ia = _ia.resumos_por_lead(s3, [int(l.id) for l in leads])

    def _card(l: Lead) -> dict[str, Any]:
        # dias parado = hoje - max(último evento, updated_at), em dias.
        # Sem atividade nenhuma, cai no created_at (parado desde que entrou).
        atividade = max(
            (d for d in (_tz(ultimo_evento.get(l.id)), _tz(l.updated_at)) if d is not None),
            default=_tz(l.created_at),
        )
        dias_parado = max((agora_utc - atividade).days, 0) if atividade else None
        fone_norm = normalize_phone_kommo(l.telefone) if l.telefone else None
        toque = cadencia_por_fone.get(fone_norm) if fone_norm else None
        cadencia_toque = f"CD{toque + 1}" if toque is not None else None
        # respondeu_em só "acende" (tag 🟢 + contador do menu) enquanto o lead
        # respondeu e a corretora AINDA NÃO retornou. Se existe outbound da Pessoa12
        # com timestamp > respondeu_em → o lead foi atendido → zera (some).
        respondeu_at = respondeu_por_fone.get(fone_norm) if fone_norm else None
        if respondeu_at is not None:
            ult_out = ultimo_outbound_por_fone.get(fone_norm) if fone_norm else None
            r_tz, o_tz = _tz(respondeu_at), _tz(ult_out)
            if o_tz is not None and r_tz is not None and o_tz > r_tz:
                respondeu_at = None  # já atendida — apaga a tag
        return {
            "lead_id": l.id,
            "external_ref": l.external_ref,
            "nome": (l.nome or "").strip() or "Sem nome",
            "telefone": l.telefone,
            "valor": float(l.valor) if l.valor is not None else None,
            "origem": l.origem,
            # campanha publicitária (Meta lead ads) — casada por telefone com a
            # base do Hunter (yashav.leads). null quando não há match.
            "campanha": campanha_por_fone.get(fone_norm) if fone_norm else None,
            # tem tarefa aberta? casa por external_ref (id Kommo) com
            # tarefas_corretora. `proxima_tarefa` = data/hora ISO da mais próxima.
            "tem_tarefa": bool(l.external_ref and l.external_ref in tarefa_por_ref),
            "proxima_tarefa": (
                tarefa_por_ref.get(l.external_ref) if l.external_ref else None
            ),
            # tag do card no V2 — por ora espelha a origem (a espinha ainda
            # não sincroniza tags próprias do Kommo)
            "tag": l.origem,
            # tag "remarketing": este card veio da linha da Corretor (handoff
            # automático ou promoção da Pessoa). Pedido da Dona 15/09/2026 — a
            # corretora precisa ver a procedência SEM abrir o histórico.
            # Sai de `leads.veio_remarketing_em`, já carregado no Lead: zero
            # query extra. `veio_remarketing_em` vai junto pro title do chip
            # ("chegou em 15/09").
            "veio_remarketing": l.veio_remarketing_em is not None,
            "veio_remarketing_em": _iso_utc(l.veio_remarketing_em),
            # O NOME da tag que o card mostra (05/10/2026: "RMKT"). A marca no
            # banco é a mesma data de sempre — quem já tinha passa a exibir
            # RMKT sem migração. É ela que tira o card da roleta do Meta.
            "tag_remarketing": _TAG_RMKT if l.veio_remarketing_em is not None else None,
            "corretora_id": l.owner_id,
            "corretora_label": CORRETORA_LABEL.get(l.owner_id) if l.owner_id else None,
            "created_at": _iso_utc(l.created_at),
            "updated_at": _iso_utc(l.updated_at),
            "ultimo_evento_at": _iso_utc(ultimo_evento.get(l.id)),
            "dias_parado": dias_parado,
            "cadencia_toque": cadencia_toque,
            # timestamp do último evento 'resposta' da cadência (status
            # respondida) — badge verde "🟢 respondeu e ainda não foi retornado"
            # no front JUNTO da tag CD congelada. Zera quando a corretora já
            # respondeu de volta (ver respondeu_at acima).
            "respondeu_em": _iso_utc(respondeu_at),
            # {status: ok|pendente|sem_resumo, resumo, fatos[{fato, trecho}]}
            "resumo_ia": resumos_ia.get(int(l.id)),
        }

    # colunas órfãs (item 3 blueprint): stage fora do funil de 8 criado pela
    # sombra antiga (ex: "Primeiro Contato", "10000001") é DOBRADO na coluna
    # canônica equivalente — a linha órfã some do kanban e os cards migram.
    from app.services.lead_spine import STAGES_CANONICOS, stage_canonico

    canonico_id_por_nome = {
        (st.nome or "").strip(): st.id
        for st in stages
        if (st.nome or "").strip() in STAGES_CANONICOS
    }
    stage_remap: dict[int, int] = {}
    stages_orfaos_dobrados: list[int] = []
    for st in stages:
        nome_st = (st.nome or "").strip()
        if nome_st in STAGES_CANONICOS:
            continue
        destino = stage_canonico(nome_st)
        if destino and destino in canonico_id_por_nome:
            stage_remap[st.id] = canonico_id_por_nome[destino]
            stages_orfaos_dobrados.append(st.id)
    if stages_orfaos_dobrados:
        stages = [st for st in stages if st.id not in set(stages_orfaos_dobrados)]

    # agrupa por stage (órfã dobrada aponta pra canônica)
    por_stage: dict[int | None, list[dict[str, Any]]] = {}
    for l in leads:
        sid = stage_remap.get(l.stage_id, l.stage_id) if l.stage_id is not None else None
        por_stage.setdefault(sid, []).append(_card(l))

    stages_derivados = not stages
    colunas: list[dict[str, Any]] = []
    if stages:
        for st in stages:
            cards = por_stage.pop(st.id, [])
            colunas.append({
                "stage_id": st.id,
                "nome": st.nome or f"Etapa {st.id}",
                "ordem": st.ordem,
                "tipo": st.tipo,
                "total": len(cards),
                "cards": cards[:200],
            })
    # sobras: stage_id sem linha em pipeline_stages (ou tabela vazia → tudo derivado)
    for sid in sorted((k for k in por_stage if k is not None)):
        cards = por_stage.pop(sid)
        colunas.append({
            "stage_id": sid,
            "nome": f"Etapa {sid}",
            "ordem": None,
            "tipo": None,
            "total": len(cards),
            "cards": cards[:200],
        })
    sem_etapa = por_stage.pop(None, [])
    if sem_etapa:
        colunas.append({
            "stage_id": None,
            "nome": "Sem etapa",
            "ordem": None,
            "tipo": None,
            "total": len(sem_etapa),
            "cards": sem_etapa[:200],
        })

    return {
        "viewer": _viewer_info(user),
        "filtro_corretora": filter_cid,
        # de quem são os cards que este viewer pode ver (None = admin, todas)
        "corretoras_visiveis": cids_visiveis,
        "total": len(leads),
        "vazio": len(leads) == 0 and not stages,
        "stages_derivados": stages_derivados,
        "colunas": colunas,
        "corretoras": [
            {"id": cid, "label": lbl} for cid, lbl in sorted(CORRETORA_LABEL.items())
        ],
    }


@router.post("/crm/mover")
def painel_crm_mover(
    body: dict[str, Any] = Body(...),
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Move um card no kanban do CRM 2.0 (B7 — escrita no kanban).

    Body: {"lead_id": <leads.id>, "stage_destino": "<etapa>",
           "funil": "principal" | "remarketing" (opcional, default "principal")}.

    Regras:
      • auth = mesma do board (X-Painel-User via nginx; fail-closed);
      • corretora só move lead DELA (owner_id = id interno OU kommo_user_id)
        ou de quem ela atende por delegação (`DELEGACAO_ATENDIMENTO`); mover
        NÃO troca a dona. Admin move qualquer um;
      • funil="principal" → stage_destino precisa ser um dos 8 canônicos;
        funil="remarketing" → stage_destino precisa ser uma das 6 etapas de
        nutrição (Base/30/60/90/120/150 dias). 400 caso contrário;
      • o stage_id é resolvido DENTRO do funil escolhido (nomes podem colidir
        entre funis; principal reusa _resolve_stage_id/aliases, remarketing usa
        resolve_stage_id_por_funil com nome literal);
      • grava leads.stage_id + lead_events(tipo='stage_manual_v2',
        payload={origem, destino, stage_destino, funil, quem}) — esse evento é o
        que protege o move manual de ser desfeito pela sombra do Kommo por 72h
        (lead_spine._manual_v2_recente);
      • destino "Follow-up" (principal) → liga a cadência na hora (pedido literal
        da Dona 10/07) reusando lead_spine._enroll_cadencia_followup (mesma flag
        CADENCIA_AUTO_ENROLL + só Pessoa12 + idempotente) — best-effort: falha na
        matrícula NUNCA desfaz o move;
      • destino "Venda ganha"/"Venda perdida" só move (sem ação extra);
      • MOVE PARA REMARKETING = SÓ MOVE A ETAPA. NÃO cria/mexe em CadenciaEstado.
        A nutrição (nutricao.tick) é disparada por CadenciaEstado.status=
        'remarketing' (setado quando a cadência de vendas esgota os 8 toques),
        NÃO pela etapa. Logo mover o card à mão pra uma etapa de remarketing
        NÃO liga a nutrição nem dispara mensagem — é inerte pro motor. Se a Dona
        quiser que o move manual ENROLLE a nutrição, é passo separado a decidir
        (ver relatório). NÃO paramos a cadência de vendas ativa por ora (evitar
        efeito colateral de disparo);
      • NÃO escreve de volta no Kommo (histórico de incidente de loop de PATCH).
    """
    from app.db.models import Lead
    from app.services import lead_spine
    from app.services.lead_spine import (
        DEFAULT_TENANT_ID,
        STAGES_CANONICOS,
        _registrar_evento,
        _resolve_stage_id,
        resolve_stage_id_por_funil,
    )
    from app.services.nutricao import FUNIL_REMARKETING, STAGES_NUTRICAO

    FUNIL_PRINCIPAL = "principal"

    user = _identificar_usuario(x_painel_user)
    is_admin = _is_admin(user)
    my_cid = _corretora_id_do_user(user)
    if my_cid is None and not is_admin:
        raise HTTPException(403, "painel_user_sem_corretora")

    try:
        lead_id = int(body.get("lead_id"))
    except (TypeError, ValueError):
        raise HTTPException(400, "lead_id_obrigatorio")
    funil = str(body.get("funil") or FUNIL_PRINCIPAL).strip().lower()
    if funil not in (FUNIL_PRINCIPAL, FUNIL_REMARKETING):
        raise HTTPException(400, "funil_invalido")
    stage_destino = str(body.get("stage_destino") or "").strip()
    if funil == FUNIL_PRINCIPAL:
        if stage_destino not in STAGES_CANONICOS:
            raise HTTPException(400, "stage_invalido")
    else:
        if stage_destino not in STAGES_NUTRICAO:
            raise HTTPException(400, "stage_invalido")

    tenant = DEFAULT_TENANT_ID
    with SessionLocal() as s:
        lead = (
            s.query(Lead)
            .filter(Lead.tenant_id == tenant, Lead.id == lead_id)
            .first()
        )
        if lead is None:
            raise HTTPException(404, "lead_nao_encontrado")

        # Escopo: corretora só move lead dela ou da delegada. owner_id na
        # espinha pode ser o id interno (inspector) OU o kommo_user_id (webhook
        # fino) — aceita os dois (mesma regra do board).
        if not is_admin:
            permitidos = _owner_ids_atendimento(my_cid)
            if lead.owner_id is None or int(lead.owner_id) not in permitidos:
                raise HTTPException(403, "lead_de_outra_corretora")

        if funil == FUNIL_PRINCIPAL:
            stage_id_destino = _resolve_stage_id(s, tenant, None, stage_destino)
        else:
            stage_id_destino = resolve_stage_id_por_funil(
                s, tenant, FUNIL_REMARKETING, stage_destino
            )
        if stage_id_destino is None:
            raise HTTPException(400, "stage_nao_resolvido")
        de = lead.stage_id
        if stage_id_destino == de:
            log.info(
                "crm_mover_noop", lead_id=lead.id, funil=funil,
                stage=stage_destino, user=user,
            )
            return {
                "ok": True, "noop": True, "de": de, "para": stage_id_destino,
                "funil": funil, "stage_destino": stage_destino,
                "cadencia_ligada": False,
            }

        lead.stage_id = stage_id_destino
        lead.updated_at = datetime.now(timezone.utc)
        _registrar_evento(
            s, tenant, lead.id, "stage_manual_v2",
            {
                "origem": de,
                "destino": stage_id_destino,
                "stage_destino": stage_destino,
                "funil": funil,
                "quem": user,
            },
        )
        s.commit()
        log.info(
            "crm_mover_ok", lead_id=lead.id, de=de, para=stage_id_destino,
            funil=funil, stage=stage_destino, user=user,
        )

        # Follow-up (principal) → liga a cadência na hora. Best-effort DEPOIS do
        # commit do move: falha na matrícula não desfaz o arrasto. Matrícula só
        # grava estado — quem toca é o tick (atrás de DRY_RUN/cap).
        # Move pra remarketing NÃO enrolla nutrição (inerte pro motor — ver
        # docstring): a nutrição vive em CadenciaEstado.status='remarketing'.
        cadencia_id: int | None = None
        if funil == FUNIL_PRINCIPAL and stage_destino == "Follow-up":
            try:
                cadencia_id = lead_spine._enroll_cadencia_followup(s, lead, tenant)
                s.commit()
            except Exception:
                s.rollback()
                log.exception("crm_mover_enroll_failed", lead_id=lead.id)

    return {
        "ok": True,
        "de": de,
        "para": stage_id_destino,
        "funil": funil,
        "stage_destino": stage_destino,
        "cadencia_ligada": cadencia_id is not None,
        "cadencia_id": cadencia_id,
    }


@router.post("/crm/transferir")
def painel_crm_transferir(
    body: dict[str, Any] = Body(...),
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Transfere um lead de uma corretora pra outra (spec do TODO do Chat V2:
    "botão transferir lead Pessoa12→Pessoa10 — muda ownership + linha de disparo;
    histórico fica com as tags de linha").

    Body: {"lead_id": <leads.id>, "corretora_destino": <corretoras.id 1|2|3>,
           "motivo": "<opcional>"}.

    Regras:
      • auth = mesma do board (X-Painel-User via nginx; fail-closed);
      • admin transfere QUALQUER lead; corretora só transfere lead DELA
        (owner_id = id interno OU kommo_user_id — mesma regra do /crm/mover);
      • muda o dono nas 3 fontes de escopo:
          - leads.owner_id (espinha — CRM board/mover) + owner_editado_manual
            (o shadow do Kommo NÃO desfaz — migration 0061);
          - leads_snapshot.corretora_id (thread do chat / _lead_conversa_contexto)
            + corretora_editado_manual (o inspector NÃO desfaz);
          - lead_analyses.corretora_id (raio-X / coaching / frios);
      • grava lead_events tipo='transferencia' payload
        {de, para, quem, motivo, de_label, para_label} — o kanban usa esse
        evento pro selo "transferido" recente;
      • ZERO WhatsApp — nenhum aviso sai daqui; o aviso é in-app (selo);
      • NÃO escreve de volta no Kommo (mesma política do /crm/mover);
      • histórico de mensagens NÃO migra de linha — a thread carrega as tags
        de linha (leva 2 do chat) e os trechos antigos seguem marcados com a
        linha original.
    """
    from app.db.models import Lead
    from app.services.lead_spine import DEFAULT_TENANT_ID
    from app.services.lead_transferencia import transferir_lead

    user = _identificar_usuario(x_painel_user)
    is_admin = _is_admin(user)
    my_cid = _corretora_id_do_user(user)
    if my_cid is None and not is_admin:
        raise HTTPException(403, "painel_user_sem_corretora")

    try:
        lead_id = int(body.get("lead_id"))
    except (TypeError, ValueError):
        raise HTTPException(400, "lead_id_obrigatorio")
    try:
        destino_cid = int(body.get("corretora_destino"))
    except (TypeError, ValueError):
        raise HTTPException(400, "corretora_destino_obrigatoria")
    if destino_cid not in CORRETORA_NOME_DB:
        raise HTTPException(400, "corretora_invalida")
    motivo = str(body.get("motivo") or "").strip()[:500] or None

    tenant = DEFAULT_TENANT_ID
    with SessionLocal() as s:
        lead = (
            s.query(Lead)
            .filter(Lead.tenant_id == tenant, Lead.id == lead_id)
            .first()
        )
        if lead is None:
            raise HTTPException(404, "lead_nao_encontrado")

        # Escopo: corretora só transfere lead dela (owner_id pode ser id
        # interno OU kommo_user_id — mesma regra do /crm/mover).
        if not is_admin:
            permitidos = {my_cid}
            kuid = CORRETORA_KOMMO_USER.get(my_cid)
            if kuid is not None:
                permitidos.add(kuid)
            if lead.owner_id is None or int(lead.owner_id) not in permitidos:
                raise HTTPException(403, "lead_de_outra_corretora")

        # dono atual normalizado pra corretora_id interno (1|2|3)
        owner_anterior = int(lead.owner_id) if lead.owner_id is not None else None
        de_cid = (
            owner_anterior
            if owner_anterior in CORRETORA_NOME_DB
            else KOMMO_USER_CORRETORA.get(owner_anterior)
        )
        if de_cid == destino_cid:
            log.info(
                "crm_transferir_noop", lead_id=lead.id,
                corretora=destino_cid, user=user,
            )
            return {
                "ok": True, "noop": True, "de": de_cid, "para": destino_cid,
                "de_label": CORRETORA_LABEL.get(de_cid),
                "para_label": CORRETORA_LABEL.get(destino_cid),
            }

        # Miolo compartilhado com o handoff automático do remarketing
        # (app/services/lead_transferencia.py): owner + flags + evento +
        # snapshot/analyses. Sem commit lá dentro — fecha aqui.
        # Card que sai da CORRETOR de dentro do funil de remarketing (o "segurado"
        # porque alguém já conversava pelo celular da linha, ou qualquer outro
        # que a gestão resolva mandar na mão) ganha a tag RMKT, como o
        # automático: a procedência é certa e é ela que mantém o card fora da
        # roleta do Meta. De qualquer outra origem o botão não inventa tag.
        from app.services import carrossel_corretor as _cb
        from app.services.nutricao import FUNIL_REMARKETING as _FUNIL_RMKT

        sai_do_remarketing = bool(
            owner_anterior == _cb.CORRETORA_CORRETOR
            and lead.stage_id is not None
            and s.execute(
                sql_text("SELECT 1 FROM pipeline_stages WHERE id = :i AND funil = :f"),
                {"i": int(lead.stage_id), "f": _FUNIL_RMKT},
            ).first()
        )
        transferir_lead(
            s, lead, destino_cid, de_cid=de_cid, quem=user, motivo=motivo,
            de_label=CORRETORA_LABEL.get(de_cid),
            para_label=CORRETORA_LABEL.get(destino_cid),
            marcar_remarketing=sai_do_remarketing,
            extra={"tag": _TAG_RMKT, "roleta": "manual"} if sai_do_remarketing else None,
        )
        s.commit()
        log.info(
            "crm_transferir_ok", lead_id=lead.id, external_ref=lead.external_ref,
            de=de_cid, para=destino_cid, user=user, motivo_len=len(motivo or ""),
        )

    return {
        "ok": True,
        "de": de_cid,
        "para": destino_cid,
        "de_label": CORRETORA_LABEL.get(de_cid),
        "para_label": CORRETORA_LABEL.get(destino_cid),
        "lead_id": lead_id,
    }


@router.get("/crm/remarketing/board")
def painel_crm_remarketing_board(
    corretora: int | None = Query(None, description="filtro admin: corretora_id"),
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Kanban do funil REMARKETING/NUTRIÇÃO (spec Dona 10/07) — board /remarketing
    do acme-painel. Lê `pipeline_stages` com funil='remarketing' (migration 0046):

        Base → 30 dias → 60 dias → 90 dias → 120 dias → 150 dias

    Cards = leads canônicos cujo `stage_id` está numa etapa desse funil (quem
    move é o motor `app/services/nutricao.py`, anexado ao tick da cadência).
    Campo `nutricao_tag` no card (30D/60D/90D/120D/150D — análogo ao `cadencia_toque`
    do /crm; Base não tem tag, ainda não houve disparo). Somente leitura.

    Escopo/privacidade: mesma regra do board principal — corretora vê os
    leads dela e os das delegadas (`DELEGACAO_ATENDIMENTO`); admin vê tudo e
    pode filtrar com ?corretora=<id>.
    """
    from app.db.models import Lead, PipelineStage
    from app.services.lead_spine import DEFAULT_TENANT_ID
    from app.services.nutricao import FUNIL_REMARKETING, TAG_POR_ETAPA

    user = _identificar_usuario(x_painel_user)
    my_cid = _corretora_id_do_user(user)
    is_admin = _is_admin(user)
    if my_cid is None and not is_admin:
        raise HTTPException(403, "painel_user_sem_corretora")
    cids_visiveis = _cids_atendimento(my_cid)
    filter_cid, owner_ids_escopo = _escopo_board(
        cids_visiveis, corretora, com_kommo_user=True
    )

    tenant = DEFAULT_TENANT_ID

    with SessionLocal() as s:
        stages = (
            s.query(PipelineStage)
            .filter(
                PipelineStage.tenant_id == tenant,
                PipelineStage.funil == FUNIL_REMARKETING,
            )
            .order_by(PipelineStage.ordem.asc().nullslast(), PipelineStage.id.asc())
            .all()
        )

        stage_ids = [st.id for st in stages]
        leads: list[Lead] = []
        if stage_ids:
            q = s.query(Lead).filter(
                Lead.tenant_id == tenant, Lead.stage_id.in_(stage_ids)
            )
            if owner_ids_escopo is not None:
                # owner_id pode ser id interno OU kommo_user_id
                q = q.filter(Lead.owner_id.in_(owner_ids_escopo))
            leads = q.order_by(
                Lead.updated_at.desc().nullslast(), Lead.id.desc()
            ).all()

    agora_utc = datetime.now(timezone.utc)

    def _tz(dt: datetime | None) -> datetime | None:
        if dt is None:
            return None
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt

    nome_por_stage = {st.id: (st.nome or "").strip() for st in stages}

    # Card segurado em Respondeu porque alguém da casa já conversa pelo celular
    # da linha da Corretor (`handoff_segurado`): o motivo vai no card. Uma consulta
    # pro quadro todo; falha vira "sem motivo", nunca tela quebrada.
    from app.services import quando_responde as _qr

    from app.services import ia_do_lado as _ia

    with SessionLocal() as s2:
        segurados = _qr.segurados_por_lead(s2, [int(l.id) for l in leads])
        resumos_ia = _ia.resumos_por_lead(s2, [int(l.id) for l in leads])

    def _card(l: Lead) -> dict[str, Any]:
        atividade = _tz(l.updated_at) or _tz(l.created_at)
        dias_parado = max((agora_utc - atividade).days, 0) if atividade else None
        etapa = nome_por_stage.get(l.stage_id, "")
        return {
            "lead_id": l.id,
            "external_ref": l.external_ref,
            "nome": (l.nome or "").strip() or "Sem nome",
            "telefone": l.telefone,
            "valor": float(l.valor) if l.valor is not None else None,
            "origem": l.origem,
            "tag": l.origem,
            "corretora_id": l.owner_id,
            "corretora_label": CORRETORA_LABEL.get(l.owner_id) if l.owner_id else None,
            "created_at": _iso_utc(l.created_at),
            "updated_at": _iso_utc(l.updated_at),
            "dias_parado": dias_parado,
            # tag do funil de nutrição — troca a cada marco (30D→60D→90D→120D→150D)
            "nutricao_tag": TAG_POR_ETAPA.get(etapa),
            # não foi pra roleta: alguém já conversa pelo celular da linha
            "handoff_segurado": (
                {"motivo": segurados[int(l.id)]["motivo"],
                 "em": _iso_utc(segurados[int(l.id)]["at"])}
                if int(l.id) in segurados else None
            ),
            "resumo_ia": resumos_ia.get(int(l.id)),
        }

    por_stage: dict[int, list[dict[str, Any]]] = {}
    for l in leads:
        por_stage.setdefault(l.stage_id, []).append(_card(l))

    colunas = [
        {
            "stage_id": st.id,
            "nome": st.nome or f"Etapa {st.id}",
            "ordem": st.ordem,
            "tipo": st.tipo,
            "total": len(por_stage.get(st.id, [])),
            "cards": por_stage.get(st.id, [])[:200],
        }
        for st in stages
    ]

    return {
        "viewer": _viewer_info(user),
        "filtro_corretora": filter_cid,
        "corretoras_visiveis": cids_visiveis,
        "funil": FUNIL_REMARKETING,
        "total": len(leads),
        "vazio": len(leads) == 0 and not stages,
        "colunas": colunas,
        "corretoras": [
            {"id": cid, "label": lbl} for cid, lbl in sorted(CORRETORA_LABEL.items())
        ],
    }


# ── Tag de linha: instância Evolution → corretora/linha ───────────────────────
#
# Cada mensagem da thread pode ter rolado por um TELEFONE diferente (lead
# transferido de corretora leva o histórico junto; recuperador e Corretor usam a
# linha da Assistente). O mapa vem do cadastro (corretoras.evolution_instance →
# CORRETORA_LABEL) + instâncias fixas fora do cadastro (históricas/sistema).

_INSTANCE_LINHA_FIXA = {
    "acme_corretor": "Pessoa10",         # instância antiga da Pessoa5 (número anterior)
    "recuperador": "Recuperador",  # linha de recovery/disparo (número da Assistente)
    "assistente": "Corretor",                 # linha da Corretor/Assistente (+5500000000000)
    "cloud_assistente": "Corretor",           # mesma linha da Corretor, transporte Cloud API
}

_LINHA_MAPA_CACHE: dict[str, Any] = {"at": 0.0, "mapa": {}}
_LINHA_MAPA_TTL_SEC = 600  # cadastro de corretoras muda raramente


def _mapa_instance_linha() -> dict[str, str]:
    """instance → label curto da linha ('Pessoa10'/'Pessoa11'/'Pessoa12'/...). Cache 10 min."""
    import time as _t

    if (
        _LINHA_MAPA_CACHE["mapa"]
        and (_t.time() - _LINHA_MAPA_CACHE["at"]) < _LINHA_MAPA_TTL_SEC
    ):
        return _LINHA_MAPA_CACHE["mapa"]
    mapa: dict[str, str] = dict(_INSTANCE_LINHA_FIXA)
    with SessionLocal() as s:
        rows = s.execute(
            sql_text(
                "SELECT id, evolution_instance FROM corretoras "
                "WHERE evolution_instance IS NOT NULL"
            )
        ).mappings().all()
    for r in rows:
        mapa[r["evolution_instance"]] = CORRETORA_LABEL.get(
            r["id"], r["evolution_instance"]
        )
    _LINHA_MAPA_CACHE.update(at=_t.time(), mapa=mapa)
    return mapa


def _linha_da_instance(instance: str | None) -> str | None:
    """Label da linha de uma instância; instância desconhecida volta o nome cru."""
    if not instance:
        return None
    return _mapa_instance_linha().get(instance, instance)


def _linhas_por_msg_ids(ids: list[str]) -> dict[str, str]:
    """evolution_message_id → label da linha, via mensagens_whatsapp.instance."""
    with SessionLocal() as s:
        rows = s.execute(
            sql_text(
                "SELECT evolution_message_id, instance FROM mensagens_whatsapp "
                "WHERE evolution_message_id = ANY(:ids)"
            ),
            {"ids": ids},
        ).mappings().all()
    mapa = _mapa_instance_linha()
    return {
        r["evolution_message_id"]: mapa.get(r["instance"], r["instance"])
        for r in rows
        if r["instance"]
    }


# ── Notas internas da thread (anotações privadas da equipe) ───────────────────
#
# "Liguei, não atendeu" — visível SÓ no painel interno, NUNCA vai pro lead.
# Reusa a tabela observacao_corretora (migration 0033) SEM IA e SEM nota
# externa (Kommo/Twenty) — diferente do POST /atendimento/{lead}/observacao,
# que roda Haiku e posta no CRM. Aqui é INSERT puro + listagem cronológica;
# o GET devolve TODAS as anotações do lead (as observações-IA também são
# notas internas legítimas na thread — flag via_ia diferencia no front).


def _corretora_dona_do_lead(lead_id: str) -> tuple[int | None, str | None]:
    """(corretora_id, lead_nome) da dona do lead — dono canônico (espinha
    normalizada → snapshot), sem exigir telefone/instância.

    Compat: (None, None) se o lead não existe. Quem precisa distinguir
    "não existe" de "existe sem dona" usa `_dono_canonico` direto (ou o gate
    pronto `_checar_escopo_lead`) — este wrapper mistura os dois casos e NÃO
    serve pra gate sozinho.
    """
    dona_cid, existe, nome = _dono_canonico(lead_id)
    if not existe:
        return None, None
    return dona_cid, nome


@router.get("/lead/{lead_id}/notas")
def painel_listar_notas(
    lead_id: str,
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Notas internas do lead, cronológicas. Corretora só lê lead dela; admin
    lê qualquer. `via_ia` marca observação que passou pela IA (painel antigo)."""
    user = _identificar_usuario(x_painel_user)
    my_cid = _corretora_id_do_user(user)
    is_admin = _is_admin(user)
    if my_cid is None and not is_admin:
        raise HTTPException(403, "painel_user_sem_corretora")

    _checar_escopo_lead(lead_id, my_cid, is_admin)

    with SessionLocal() as s:
        rows = s.execute(
            sql_text(
                "SELECT id, autor, texto, criado_em, corretora_id, "
                "       (ia_nota_kommo IS NOT NULL) AS via_ia "
                "FROM observacao_corretora "
                "WHERE lead_id = :lid "
                "ORDER BY criado_em ASC, id ASC"
            ),
            {"lid": lead_id},
        ).mappings().all()

    notas = [
        {
            "id": int(r["id"]),
            "autor": r["autor"],
            "texto": r["texto"],
            "at": r["criado_em"].isoformat() if r["criado_em"] else None,
            "corretora": CORRETORA_LABEL.get(r["corretora_id"]) if r["corretora_id"] else None,
            "via_ia": bool(r["via_ia"]),
        }
        for r in rows
    ]
    return {"lead_id": lead_id, "total": len(notas), "notas": notas}


@router.post("/lead/{lead_id}/nota")
def painel_criar_nota(
    lead_id: str,
    texto: str = Body(..., embed=True, min_length=2, max_length=2000),
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Cria nota interna da equipe na thread do lead. NÃO envia nada pro lead,
    não roda IA, não posta no CRM — anotação privada ('liguei, não atendeu').
    Corretora só anota lead dela; admin anota qualquer um."""
    user = _identificar_usuario(x_painel_user)
    my_cid = _corretora_id_do_user(user)
    is_admin = _is_admin(user)
    if my_cid is None and not is_admin:
        raise HTTPException(403, "painel_user_sem_corretora")

    texto = (texto or "").strip()
    if len(texto) < 2:
        raise HTTPException(422, "nota_vazia")

    dona_cid, _ = _checar_escopo_lead(lead_id, my_cid, is_admin)
    target_cid = my_cid if my_cid is not None else dona_cid

    with SessionLocal() as s:
        row = s.execute(
            sql_text(
                "INSERT INTO observacao_corretora "
                "(lead_id, corretora_id, autor, texto) "
                "VALUES (:lid, :cid, :autor, :texto) "
                "RETURNING id, criado_em"
            ),
            {"lid": lead_id, "cid": target_cid, "autor": user, "texto": texto},
        ).mappings().first()
        s.commit()

    log.info(
        "nota_interna_criada",
        lead_id=lead_id, nota_id=int(row["id"]), corretora_id=target_cid, autor=user,
    )
    return {
        "ok": True,
        "nota": {
            "id": int(row["id"]),
            "autor": user,
            "texto": texto,
            "at": row["criado_em"].isoformat() if row["criado_em"] else None,
            "corretora": CORRETORA_LABEL.get(target_cid) if target_cid else None,
            "via_ia": False,
        },
    }


@router.post("/lead/{lead_id}/nome")
def painel_editar_nome(
    lead_id: str,
    nome: str = Body(..., embed=True, min_length=2, max_length=120),
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Edita o NOME do lead no NOSSO banco (ex: "Lead #13344706" → nome real).

    NÃO escreve no Kommo — a agência vai largar o Kommo. Marca
    nome_editado_manual=true nas duas tabelas fonte do nome exibido:
      • leads_snapshot.nome → header da conversa e /painel/kanban
      • leads.nome (espinha) → /painel/crm/board
    A flag protege o nome do sync Kommo→local (inspector + lead_spine).

    Auth (padrão /nota, /tarefa): corretora só edita lead dela; admin qualquer.
    Lead sem dona é admin-only (não é 404: ele existe, só não tem responsável).
    """
    user = _identificar_usuario(x_painel_user)
    my_cid = _corretora_id_do_user(user)
    is_admin = _is_admin(user)
    if my_cid is None and not is_admin:
        raise HTTPException(403, "painel_user_sem_corretora")

    nome = (nome or "").strip()
    if len(nome) < 2:
        raise HTTPException(422, "nome_curto")
    if len(nome) > 120:
        nome = nome[:120]

    # 404 se não existe / 403 se não é dela. O gate antigo (`not in (None,
    # my_cid)`) consultava só o snapshot e liberava qualquer corretora a
    # reescrever os 5.704 leads que só vivem na espinha.
    dona_cid, _ = _checar_escopo_lead(lead_id, my_cid, is_admin)

    with SessionLocal() as s:
        snap = s.execute(
            sql_text(
                "UPDATE leads_snapshot "
                "SET nome = :nome, nome_editado_manual = TRUE "
                "WHERE lead_id = :lid "
                "RETURNING lead_id"
            ),
            {"nome": nome, "lid": str(lead_id)},
        ).first()
        # espinha canônica (/crm/board): leads.external_ref == lead_id do Kommo
        spine = s.execute(
            sql_text(
                "UPDATE leads "
                "SET nome = :nome, nome_editado_manual = TRUE, updated_at = now() "
                "WHERE external_ref = :lid "
                "RETURNING id"
            ),
            {"nome": nome, "lid": str(lead_id)},
        ).first()
        s.commit()

    if snap is None and spine is None:
        raise HTTPException(404, "lead_nao_encontrado")

    log.info(
        "painel_lead_nome_editado",
        lead_id=lead_id, autor=user,
        # dona do lead E quem editou — antes gravava só `corretora_id=dona_cid`
        # (quase sempre None), o que tornava edição indevida invisível no log.
        dona_cid=dona_cid, editor_cid=my_cid, admin=is_admin,
        snapshot=snap is not None, spine=spine is not None,
    )
    return {"ok": True, "nome": nome}


@router.post("/lead/{lead_id}/contato")
def painel_editar_contato(
    lead_id: str,
    telefone: str = Body(..., embed=True, min_length=8, max_length=40),
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Corrige o TELEFONE do lead no NOSSO banco (ex: lead incontactável com
    número inválido "+55000000000009" → número real). Espelha /lead/{id}/nome.

    NÃO escreve no Kommo. Normaliza pra BR E.164 (+55DD9XXXXXXXX) e marca
    telefone_editado_manual=true nas duas tabelas fonte do telefone:
      • leads_snapshot.telefone → resolução da thread do WhatsApp (por sufixo)
      • leads.telefone (espinha) → /painel/crm/board
    A flag protege o telefone do sync Kommo→local (inspector + lead_spine).

    Efeito colateral esperado (NÃO é bug): a thread do chat resolve por telefone
    (sufixo). Corrigir o número religa o lead à conversa real do WhatsApp — se o
    número novo não tem histórico, a thread vem vazia até a 1ª mensagem.
    NÃO mexemos na cadência: estado antigo (por sufixo do número velho) fica
    órfão — aceitável, sem sincronização automática.

    Auth (padrão /nome): corretora só edita lead dela; admin qualquer.
    Lead sem dona é admin-only (não é 404: ele existe, só não tem responsável).
    """
    user = _identificar_usuario(x_painel_user)
    my_cid = _corretora_id_do_user(user)
    is_admin = _is_admin(user)
    if my_cid is None and not is_admin:
        raise HTTPException(403, "painel_user_sem_corretora")

    # Normalização/validação BR (E.164). normalize_phone retorna dígitos sem '+'
    # (ex "+5500000000000"); armazenamos com '+' pra bater o formato das tabelas
    # (+55DD9XXXXXXXX). Rejeita < 12 dígitos com DDI / formato inválido → 422.
    from app.services.phone import normalize_phone

    try:
        digits = normalize_phone(telefone or "")
    except ValueError:
        raise HTTPException(422, "telefone_invalido")
    if not (digits.startswith("55") and len(digits) in (12, 13)):
        raise HTTPException(422, "telefone_invalido")
    telefone_norm = "+" + digits

    # 404 se não existe / 403 se não é dela. Ver comentário em /lead/{id}/nome:
    # o gate antigo deixava qualquer corretora reescrever o TELEFONE de lead
    # alheio que só tivesse linha na espinha.
    dona_cid, _ = _checar_escopo_lead(lead_id, my_cid, is_admin)

    with SessionLocal() as s:
        snap = s.execute(
            sql_text(
                "UPDATE leads_snapshot "
                "SET telefone = :tel, telefone_editado_manual = TRUE "
                "WHERE lead_id = :lid "
                "RETURNING lead_id"
            ),
            {"tel": telefone_norm, "lid": str(lead_id)},
        ).first()
        # espinha canônica (/crm/board): leads.external_ref == lead_id do Kommo
        spine = s.execute(
            sql_text(
                "UPDATE leads "
                "SET telefone = :tel, telefone_editado_manual = TRUE, "
                "updated_at = now() "
                "WHERE external_ref = :lid "
                "RETURNING id"
            ),
            {"tel": telefone_norm, "lid": str(lead_id)},
        ).first()
        s.commit()

    if snap is None and spine is None:
        raise HTTPException(404, "lead_nao_encontrado")

    log.info(
        "painel_lead_contato_editado",
        lead_id=lead_id, autor=user,
        dona_cid=dona_cid, editor_cid=my_cid, admin=is_admin,
        snapshot=snap is not None, spine=spine is not None,
    )
    return {"ok": True, "telefone": telefone_norm}


# TODO (spec futura — NÃO implementar ainda, pedido Dona leva 2 do Chat):
# botão "transferir lead" Pessoa12→Pessoa10 (e vice-versa) na thread — muda ownership
# (corretora_id no snapshot/analyses/CRM) E a linha de disparo. O histórico
# fica (as tags de linha acima já mostram por onde cada trecho rolou).


# ── Visitas: ação de status (V2 📅 Visitas) ──────────────────────────────────
#
# Conflito com o backfill Calendar→Postgres (copiloto-backfill-visitas, hora
# em hora): o UPSERT do backfill JÁ preserva status terminal — o CASE em
# scripts/backfill_visitas_calendar.py só sobrescreve status quando a linha
# ainda está provisória (agendada/pendente_confirmacao). Como este endpoint
# só grava status terminal (compareceu/no_show/cancelada), o Postgres nunca
# perde a marcação feita aqui.
# O PATCH de cor no Calendar abaixo é best-effort e existe pra manter o
# calendário coerente (🟠→🟢/🔴/⚫): o fluxo de domingo da Assistente
# (skills/agenda/confirmar_domingo.py) lê o Calendar POR COR — sem o PATCH,
# a corretora seria cobrada no domingo por visita que já marcou no painel.

_VISITA_STATUS_ACIONAVEIS = {"compareceu", "no_show", "cancelada"}
# Mesmo mapa de skills/agenda/_helper.py (COLOR_SHOWED/NOSHOW/CANCELED)
_VISITA_STATUS_COLOR = {"compareceu": "10", "no_show": "11", "cancelada": "8"}


@router.post("/visita/{visita_id}/status")
def painel_visita_muda_status(
    visita_id: int,
    body: dict[str, Any] = Body(...),
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Marca visita como compareceu / no_show / cancelada (V2 📅 Visitas).

    Body: {"status": "compareceu" | "no_show" | "cancelada"}

    Gates:
      • corretora só muda visita DELA (403 caso contrário); admin muda qualquer;
      • status precisa ser um dos acionáveis acima (400) — subset do CHECK
        ck_visitas_status (agendada/pendente_confirmacao não são alvo de ação).

    Grava status + atualizado_em + rastro em status_atualizado_por_tel
    ("painel:<user>" — mesmo campo que a Assistente usa, prefixado pra
    distinguir origem). Depois, best-effort, PATCHa a cor do evento no
    Calendar via Gateway (falha só loga warning — o backfill não sobrescreve
    status terminal, ver comentário acima).
    """
    user = _identificar_usuario(x_painel_user)
    my_cid = _corretora_id_do_user(user)  # None = admin

    novo = (body.get("status") or "").strip()
    if novo not in _VISITA_STATUS_ACIONAVEIS:
        raise HTTPException(400, "status_invalido")

    with SessionLocal() as s:
        row = s.execute(
            sql_text(
                "SELECT corretora_id, status, evento_calendar_id, lead_nome "
                "FROM visitas WHERE id = :vid"
            ),
            {"vid": visita_id},
        ).first()
        if not row:
            raise HTTPException(404, "visita_nao_encontrada")
        dona_cid, status_atual, evento_id, lead_nome = int(row[0]), row[1], row[2], row[3]

        # Ownership: corretora só mexe na própria visita. Admin em qualquer.
        if my_cid is not None and dona_cid != my_cid:
            raise HTTPException(403, "visita_de_outra_corretora")

        if status_atual == novo:
            log.info("painel_visita_status_noop", visita_id=visita_id,
                     status=novo, user=user)
            return {"ok": True, "visita_id": visita_id, "de": status_atual,
                    "para": novo, "noop": True}

        s.execute(
            sql_text(
                "UPDATE visitas SET status = :st, atualizado_em = NOW(), "
                "status_atualizado_por_tel = :who WHERE id = :vid"
            ),
            {"st": novo, "who": f"painel:{user}", "vid": visita_id},
        )
        s.commit()

    # Best-effort: cor no Calendar (visitas do backfill têm evento_calendar_id;
    # visitas criadas só no banco, não — aí não há o que sincronizar).
    calendar_ok = False
    if evento_id:
        try:
            from app.integrations.gateway_client import GatewayClient

            GatewayClient().update_event(evento_id, {"colorId": _VISITA_STATUS_COLOR[novo]})
            calendar_ok = True
        except Exception as e:  # noqa: BLE001 — falha de Calendar não desfaz o UPDATE
            log.warning("painel_visita_status_calendar_fail",
                        visita_id=visita_id, evento_id=evento_id,
                        erro=str(e)[:150])

    log.info("painel_visita_status_mudado", visita_id=visita_id,
             lead=lead_nome, de=status_atual, para=novo, user=user,
             corretora_id=dona_cid, calendar_sync=calendar_ok)
    return {"ok": True, "visita_id": visita_id, "de": status_atual,
            "para": novo, "calendar_sync": calendar_ok}


# ── Visitas: criação (V2 📅 Visitas — botão "+ Agendar visita") ──────────────
#
# MESMO caminho que a Assistente (Telegram) e a copilota (chat do painel) usam:
# subprocess de skills/agenda/criar_visita.py, que 1) cria o evento 🟠 no
# Calendar "Acme Imóveis" via Gateway e 2) INSERT em `visitas` (dedup por
# índice único parcial, detecção de re-visita 90d, resolução de lead_id).
# NÃO envia NENHUMA mensagem ao lead ou à corretora — criar_visita.py não tem
# caminho de envio; o agendamento é só Calendar + painel (lei da Dona).

# corretora_id → prefixo do título no Calendar ("[Pessoa10] Fulano") — mesmo mapa
# _PREFIXO_CAL de app/api/assistente_copilota.py (Pessoa3 usa "Pessoa" por
# convenção histórica do calendário).
_VISITA_PREFIXO_CAL = {1: "Pessoa10", 2: "Pessoa11", 3: "Pessoa"}

# ── Visita retroativa: até 30 dias atrás ─────────────────────────────────────
#
# Até 21/09 a tela recusava QUALQUER data passada (422 data_no_passado). Na
# prática isso significava: visita que aconteceu e não estava na agenda nunca
# entrava no sistema. Foi o que travou a Pessoa11 — a semana dela (14→20/09) ficou
# em branco no painel, não porque não houve visita, mas porque na segunda já
# não dava pra lançar.
#
# A visita retroativa nasce `agendada` igual a qualquer outra, então cai
# sozinha no bloco "vencidas em aberto" da tela (data_hora < now + status
# provisório) com os botões ✓ Compareceu / ✗ Não veio ao lado. Registrar e
# fechar viram dois cliques seguidos, não dois fluxos.
#
# 30 dias é a janela: cobre "esqueci de lançar o mês passado" e ainda segura
# digitação errada de ano/mês (2025-09-22 vira 422, não vira visita fantasma
# um ano atrás).
_VISITA_RETROATIVO_MAX_DIAS = 30


def _criar_visita_dir():
    from pathlib import Path as _P
    return _P(__file__).resolve().parents[1] / "agents" / "assistente" / "skills" / "agenda"


@router.post("/visitas")
def painel_criar_visita(
    body: dict[str, Any] = Body(...),
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Agenda uma visita pela tela 📅 Visitas do V2.

    Body: {
      "lead_nome": str (obrigatório),
      "data": "YYYY-MM-DD", "hora": "HH:MM" (obrigatórios; a data pode ser
          retroativa até _VISITA_RETROATIVO_MAX_DIAS dias),
      "telefone"?: str, "lead_id"?: str (kommo_id, quando veio da busca),
      "empreendimento"?: str (endereço/empreendimento — vira `bairro`),
      "observacao"?: str (vai só na description do evento do Calendar),
      "corretora_id"?: int (SÓ admin — corretora agenda sempre pra ela)
    }

    Gates: corretora agenda só pra ela (corretora_id divergente → 403);
    admin precisa mandar corretora_id (400). Duplicada (mesma corretora +
    data/hora + nome, dedup do criar_visita.py) → 409. Data mais de 30 dias
    atrás → 422 data_muito_antiga (ver _VISITA_RETROATIVO_MAX_DIAS).

    Endpoint `def` síncrono de propósito (threadpool) — subprocess bloqueante
    dentro de `async def` travaria o event loop (gotcha da casa).
    """
    import json as _json
    import subprocess as _subprocess
    import sys as _sys

    user = _identificar_usuario(x_painel_user)
    my_cid = _corretora_id_do_user(user)
    is_admin = _is_admin(user)
    if my_cid is None and not is_admin:
        raise HTTPException(403, "painel_user_sem_corretora")

    lead_nome = (body.get("lead_nome") or "").strip()
    if len(lead_nome) < 2:
        raise HTTPException(422, "lead_nome_obrigatorio")

    data_str = (body.get("data") or "").strip()
    hora_str = (body.get("hora") or "").strip()
    try:
        data_visita = datetime.strptime(data_str, "%Y-%m-%d").date()
    except (ValueError, AttributeError):
        raise HTTPException(422, "data_invalida")
    try:
        datetime.strptime(hora_str, "%H:%M")
    except (ValueError, AttributeError):
        raise HTTPException(422, "hora_invalida")
    dias_atras = (_hoje_sp() - data_visita).days
    if dias_atras > _VISITA_RETROATIVO_MAX_DIAS:
        raise HTTPException(422, "data_muito_antiga")

    # Corretora alvo: corretora agenda pra ela mesma; admin escolhe.
    body_cid = body.get("corretora_id")
    if my_cid is not None:
        if body_cid not in (None, my_cid):
            raise HTTPException(403, "corretora_so_agenda_pra_ela")
        target_cid = my_cid
    else:
        try:
            target_cid = int(body_cid)
        except (TypeError, ValueError):
            raise HTTPException(400, "corretora_id_obrigatorio")
    prefixo = _VISITA_PREFIXO_CAL.get(target_cid)
    if not prefixo:
        raise HTTPException(400, "corretora_id_invalido")

    telefone = (body.get("telefone") or "").strip() or None
    lead_id = (str(body.get("lead_id") or "")).strip() or None
    empreendimento = (body.get("empreendimento") or "").strip() or None
    observacao = (body.get("observacao") or "").strip() or None

    skill_dir = _criar_visita_dir()
    cmd = [
        _sys.executable, str(skill_dir / "criar_visita.py"),
        "--corretora", prefixo,
        "--cliente", lead_nome,
        "--data", data_str,
        "--hora", hora_str,
        "--origem", "crm",
        "--registrado-por-telefone", f"painel:{user}",
    ]
    if telefone:
        cmd += ["--telefone", telefone]
    if empreendimento:
        cmd += ["--bairro", empreendimento]
    if observacao:
        cmd += ["--obs", observacao]
    if lead_id:
        cmd += ["--lead-id", lead_id]
    # Retroativa: o criar_visita.py barra data passada por padrão (proteção do
    # caminho da Assistente, onde a data vem de texto livre do WhatsApp). Aqui a
    # data veio de um date picker com min/max, então liberamos explicitamente.
    if dias_atras > 0:
        cmd += ["--allow-past"]

    try:
        r = _subprocess.run(
            cmd, cwd=str(skill_dir),
            capture_output=True, text=True, timeout=70,
        )
    except _subprocess.TimeoutExpired:
        log.warning("painel_visita_criar_timeout", user=user, lead=lead_nome)
        raise HTTPException(504, "criar_visita_timeout")

    if r.returncode != 0:
        detalhe = (r.stderr or r.stdout or "").strip()[-200:]
        log.warning("painel_visita_criar_falhou", user=user, lead=lead_nome,
                    rc=r.returncode, detalhe=detalhe)
        # rc=2 = evento criado no Calendar mas Postgres falhou (warning loud
        # do criar_visita.py) — 502 pra revisão manual.
        raise HTTPException(502, f"criar_visita_falhou: {detalhe}")

    try:
        out = _json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        log.warning("painel_visita_criar_saida_invalida", user=user,
                    stdout=(r.stdout or "")[:200])
        raise HTTPException(502, "criar_visita_saida_invalida")

    if out.get("status") == "duplicada":
        log.info("painel_visita_criar_duplicada", user=user, lead=lead_nome,
                 corretora_id=target_cid, data=data_str, hora=hora_str)
        raise HTTPException(409, "visita_duplicada")

    log.info(
        "painel_visita_criada", user=user, corretora_id=target_cid,
        visita_id=out.get("visita_id"), lead=lead_nome, lead_id=lead_id,
        data=data_str, hora=hora_str, is_revisita=out.get("is_revisita"),
        evento_calendar_id=out.get("evento_calendar_id"),
        retroativa=dias_atras > 0, dias_atras=dias_atras,
    )
    return {
        "ok": True,
        "visita_id": out.get("visita_id"),
        "corretora_id": target_cid,
        "corretora_label": CORRETORA_LABEL.get(target_cid),
        "is_revisita": bool(out.get("is_revisita")),
        "evento_calendar_id": out.get("evento_calendar_id"),
        # o front usa pra dizer "registrada ✓ — marca se a pessoa foi"
        "retroativa": dias_atras > 0,
    }


@router.post("/atendimento/{lead_id}/reanalisar")
def painel_reanalisar_lead(
    lead_id: str,
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Reanálise sob demanda do Inspector (botão 🔁 no raio-X do chat V2).

    Refaz classifier+analyzer pro lead AGORA, com a conversa mais recente, e
    grava em lead_analyses (entrou_por="reanalise_manual"). Privacidade igual
    ao painel_conversa: corretora só reanalisará lead dela; admin qualquer.

    Endpoint `def` síncrono de propósito (threadpool do FastAPI) — a análise
    leva 30-90s de LLM; `async def` com chamada bloqueante travaria o event
    loop inteiro (gotcha da casa).

    Rate-limit anti clique-duplo: se a última análise do lead tem < 2 min,
    devolve a existente com {"ja_recente": true} em vez de gastar LLM 2x.
    """
    user = _identificar_usuario(x_painel_user)
    viewer = _viewer_info(user)
    my_cid = viewer["corretora_id"]

    from app.inspector.reanalise import reanalisar_lead

    # Contato fora do funil (tel:...) — sem linha em leads_snapshot, não dá
    # pra gravar análise. reanalisar_lead devolve o erro amigável sem LLM.
    if lead_id.startswith("tel:"):
        return reanalisar_lead(lead_id)

    ctx = _lead_conversa_contexto(lead_id)
    if not _pode_atender(my_cid, ctx["corretora_id"]):
        raise HTTPException(403, "lead_de_outra_corretora")

    # Rate-limit: análise fresquinha (< 2 min) → devolve a existente.
    with SessionLocal() as s:
        la = s.execute(
            sql_text(
                """
                SELECT temperatura, status, raw_analysis, analyzed_at
                FROM lead_analyses
                WHERE lead_id::text = :lid
                ORDER BY analyzed_at DESC
                LIMIT 1
                """
            ),
            {"lid": str(lead_id)},
        ).mappings().first()
    if la and la["analyzed_at"]:
        at = la["analyzed_at"]
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - at < timedelta(minutes=2):
            ra = la["raw_analysis"] or {}
            log.info("painel_reanalisar_ja_recente", lead_id=lead_id, user=user)
            return {
                "ja_recente": True,
                "temperatura": la["temperatura"] or ra.get("temperatura"),
                "status": la["status"] or ra.get("status"),
                "dias_sem_msg_lead": ra.get("dias_sem_msg_lead"),
                "resumo": ra.get("conversa_resumo"),
                "gap": ra.get("gap_identificado"),
                "sugestao": ra.get("abordagem_sugerida"),
                "analyzed_at": at.isoformat(),
            }

    log.info("painel_reanalisar_inicio", lead_id=lead_id, user=user)
    return reanalisar_lead(lead_id)


# ── CRM 2.0 · resolver lead pelo id do Kommo + adoção pra espinha (Chat V2) ──


@router.get("/crm/lead-por-ref")
def painel_crm_lead_por_ref(
    external_ref: str = Query(..., description="lead_id do Kommo (leads.external_ref)"),
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Resolve o vínculo do lead DIRETO na espinha canônica (`leads`), sem
    passar pelo board — o Chat V2 usa pra decidir o seletor de etapa. Antes o
    chat varria a RESPOSTA dos boards e o corte de cards por coluna escondia
    lead real (bug 24/07: lead fora do top da coluna ficava sem seletor).

    Retorno: {vinculado: true, lead_id, funil, etapa} ou {vinculado: false}.
    Etapa órfã do funil principal é dobrada pra canônica (mesma regra de
    exibição do board). Lead com stage NULL → funil/etapa null.

    Escopo (mesmo padrão do board/mover): corretora só resolve lead DELA ou
    da delegada (owner_id = id interno OU kommo_user_id; lead só-snapshot →
    corretora_id do snapshot) — 403 caso contrário; admin resolve qualquer um.
    """
    from app.db.models import Lead, LeadSnapshot, PipelineStage
    from app.services.lead_spine import DEFAULT_TENANT_ID, stage_canonico

    user = _identificar_usuario(x_painel_user)
    is_admin = _is_admin(user)
    my_cid = _corretora_id_do_user(user)
    if my_cid is None and not is_admin:
        raise HTTPException(403, "painel_user_sem_corretora")

    ref = (external_ref or "").strip()
    if not ref:
        raise HTTPException(400, "external_ref_obrigatorio")

    tenant = DEFAULT_TENANT_ID
    with SessionLocal() as s:
        row = (
            s.query(Lead, PipelineStage)
            .outerjoin(PipelineStage, PipelineStage.id == Lead.stage_id)
            .filter(Lead.tenant_id == tenant, Lead.external_ref == ref)
            .first()
        )
        if row is None and _ref_id_espinha(ref) is not None:
            # ref numérico curto = id da espinha. O /conversa sempre aceitou os
            # dois (ver _contexto_lead_nativo); aqui não aceitava, e link antigo
            # com leads.id (Meu Dia "retomar", até 20/08) abria a conversa mas
            # sem vínculo — o seletor de etapa virava adoção e dava 404.
            row = (
                s.query(Lead, PipelineStage)
                .outerjoin(PipelineStage, PipelineStage.id == Lead.stage_id)
                .filter(Lead.tenant_id == tenant, Lead.id == _ref_id_espinha(ref))
                .first()
            )
        if row is None:
            # sem card na espinha — checa a posse pelo snapshot (Kommo) antes
            # de liberar a adoção pro chat. Snapshot inexistente ou sem dona →
            # vinculado=false (nada a vazar).
            snap = (
                s.query(LeadSnapshot)
                .filter(
                    LeadSnapshot.tenant_id == tenant,
                    LeadSnapshot.lead_id == ref,
                )
                .first()
            )
            if (
                snap is not None
                and not is_admin
                and snap.corretora_id is not None
                and not _pode_atender(my_cid, int(snap.corretora_id))
            ):
                raise HTTPException(403, "lead_de_outra_corretora")
            return {"vinculado": False}

        lead, stage = row
        # corretora só enxerga lead dela ou da delegada — owner_id pode ser o
        # id interno (inspector) OU o kommo_user_id (webhook fino), mesma regra
        # do mover.
        if not is_admin:
            permitidos = _owner_ids_atendimento(my_cid)
            if lead.owner_id is None or int(lead.owner_id) not in permitidos:
                raise HTTPException(403, "lead_de_outra_corretora")

        funil = stage.funil if stage is not None else None
        etapa = ((stage.nome or "").strip() or None) if stage is not None else None
        # órfã do principal (ex: "Primeiro Contato") dobra pra canônica
        if etapa and funil == "principal":
            etapa = stage_canonico(etapa) or etapa

    return {"vinculado": True, "lead_id": lead.id, "funil": funil, "etapa": etapa}


@router.post("/crm/adotar")
def painel_crm_adotar(
    body: dict[str, Any] = Body(...),
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Adota pro funil principal um lead que existe no Kommo (leads_snapshot)
    mas ainda não tem card na espinha canônica (`leads`) — os 701 do bug 24/07.

    Body: {"external_ref": "<kommo_lead_id>", "etapa": "<uma das 8 canônicas>"}.

    Cria o vínculo REUSANDO o upsert da sombra (lead_spine.upsert_lead_shadow —
    idempotente, respeita aliases/flags de edição manual) e seta a etapa pelo
    MESMO caminho do mover manual do board (painel_crm_mover): leads.stage_id +
    lead_events(tipo='stage_manual_v2') — proteção de 72h contra a sombra do
    Kommo — + cadência ligada quando destino = Follow-up (best-effort). Lead já
    vinculado cai no comportamento do mover (só muda a etapa). Escopo: corretora
    só adota lead DELA ou da delegada (a dona do card não muda); admin qualquer.
    NÃO escreve de volta no Kommo.
    """
    from app.db.models import Lead, LeadSnapshot
    from app.services import lead_spine
    from app.services.lead_spine import (
        DEFAULT_TENANT_ID,
        STAGES_CANONICOS,
        _registrar_evento,
        _resolve_stage_id,
        upsert_lead_shadow,
    )

    user = _identificar_usuario(x_painel_user)
    is_admin = _is_admin(user)
    my_cid = _corretora_id_do_user(user)
    if my_cid is None and not is_admin:
        raise HTTPException(403, "painel_user_sem_corretora")

    ref = str(body.get("external_ref") or "").strip()
    if not ref:
        raise HTTPException(400, "external_ref_obrigatorio")
    etapa = str(body.get("etapa") or "").strip()
    if etapa not in STAGES_CANONICOS:
        raise HTTPException(400, "stage_invalido")

    tenant = DEFAULT_TENANT_ID
    with SessionLocal() as s:
        lead = (
            s.query(Lead)
            .filter(Lead.tenant_id == tenant, Lead.external_ref == ref)
            .first()
        )
        if lead is None and _ref_id_espinha(ref) is not None:
            # mesma tolerância do lead-por-ref: link antigo manda leads.id
            lead = (
                s.query(Lead)
                .filter(Lead.tenant_id == tenant, Lead.id == _ref_id_espinha(ref))
                .first()
            )
        vinculado_novo = False
        if lead is None:
            snap = (
                s.query(LeadSnapshot)
                .filter(
                    LeadSnapshot.tenant_id == tenant,
                    LeadSnapshot.lead_id == ref,
                )
                .first()
            )
            if snap is None:
                raise HTTPException(404, "lead_nao_encontrado")
            if not is_admin and (
                snap.corretora_id is None
                or not _pode_atender(my_cid, int(snap.corretora_id))
            ):
                raise HTTPException(403, "lead_de_outra_corretora")
            # cria o vínculo pelo upsert canônico da sombra — SEM etapa aqui:
            # o stage entra abaixo pelo caminho manual (stage_manual_v2),
            # igual ao drag do board.
            lead_id_novo = upsert_lead_shadow(
                {
                    "lead_id": snap.lead_id,
                    "nome": snap.nome,
                    "telefone": snap.telefone,
                    "corretora_id": snap.corretora_id,
                    "origem": snap.origem,
                },
                tenant_id=tenant,
            )
            if lead_id_novo is None:
                raise HTTPException(503, "lead_spine_desligado")
            vinculado_novo = True
            # o upsert commitou em sessão própria — recarrega na nossa
            lead = s.query(Lead).filter(Lead.id == lead_id_novo).first()
            if lead is None:
                raise HTTPException(500, "lead_nao_criado")
        else:
            if not is_admin:
                permitidos = _owner_ids_atendimento(my_cid)
                if lead.owner_id is None or int(lead.owner_id) not in permitidos:
                    raise HTTPException(403, "lead_de_outra_corretora")

        stage_id_destino = _resolve_stage_id(s, tenant, None, etapa)
        if stage_id_destino is None:
            raise HTTPException(400, "stage_nao_resolvido")
        de = lead.stage_id
        if stage_id_destino != de:
            lead.stage_id = stage_id_destino
            lead.updated_at = datetime.now(timezone.utc)
            _registrar_evento(
                s, tenant, lead.id, "stage_manual_v2",
                {
                    "origem": de,
                    "destino": stage_id_destino,
                    "stage_destino": etapa,
                    "funil": "principal",
                    "quem": user,
                    "via": "adotar_chat_v2",
                },
            )
        s.commit()
        log.info(
            "crm_adotar_ok", external_ref=ref, lead_id=lead.id,
            vinculado_novo=vinculado_novo, de=de, para=stage_id_destino,
            etapa=etapa, user=user,
        )

        # Follow-up → liga a cadência na hora (mesma regra do mover). Best-effort
        # DEPOIS do commit: falha na matrícula não desfaz a adoção.
        cadencia_id: int | None = None
        if etapa == "Follow-up":
            try:
                cadencia_id = lead_spine._enroll_cadencia_followup(s, lead, tenant)
                s.commit()
            except Exception:
                s.rollback()
                log.exception("crm_adotar_enroll_failed", lead_id=lead.id)

        lead_id_final = lead.id

    return {
        "ok": True,
        "vinculado_novo": vinculado_novo,
        "lead_id": lead_id_final,
        "de": de,
        "para": stage_id_destino,
        "funil": "principal",
        "etapa": etapa,
        "cadencia_ligada": cadencia_id is not None,
        "cadencia_id": cadencia_id,
    }


# ── 👋 Meu dia ───────────────────────────────────────────────────────────────
# A tela que responde "o que eu faço AGORA" pra corretora. É a única tela do
# painel que muda a rotina dela — o resto é gestão. Por isso ela é MONTAGEM dos
# dados que já existem (cadência, conversas, leads do dia, tarefas, fila de
# ligação) num endpoint só, e não uma fonte nova de verdade.
#
# A ORDEM DOS BLOCOS É POR CUSTO DE DEMORA, não por tipo — não reordenar:
#   1. responderam e esperam você   → mão levantada; é onde o tempo mais pesa
#   2. conversas com pergunta em aberto → última msg é do lead E pede resposta
#   3. aguardando seu primeiro contato  → chegou hoje, recebeu a automática,
#      ainda calado. NÃO é "lead calado": não é problema dele, é oportunidade
#      nova (distinção da Dona — muda de lista de pendência pra fila).
#   4. tarefas de hoje
#   5. ligações → importante e nunca urgente; se ela só fizer com o resto em
#      dia, está certo. Link pra aba Ligar que já existe em /tarefas.
#
# "SEM DADO" ≠ ZERO: cada bloco carrega `status` ("ok" | "erro"). Bloco vazio
# porque não há nada mostra "tudo em dia"; bloco que falhou diz que falhou. O
# confete só aparece com TODOS os blocos em "ok" e zerados.

MEU_DIA_JANELA_RESPOSTA_H = 48   # mesma janela do badge 🟢 do CRM
MEU_DIA_DIAS_CONVERSA = 14       # mesma janela do /conversa/aguardando


def _recusou_na_conversa(texto: str | None) -> bool:
    """Reusa a leitura de recusa da Corretor — de propósito.

    Duas ideias diferentes de "essa pessoa disse não" na mesma casa é como
    aparece lead encerrado num lugar e sendo cobrado no outro. Se a regra
    melhorar lá (e ela melhorou três vezes esta semana), melhora aqui junto.
    """
    if not texto:
        return False
    try:
        from app.services.atendimento_corretor import _e_recusa_branda, _pediu_parar
        return bool(_e_recusa_branda(texto) or _pediu_parar(texto))
    except Exception:  # noqa: BLE001 — na dúvida, MANTÉM na lista
        return False


def _meu_dia_bloco(
    chave: str, icone: str, titulo: str, itens: list[dict[str, Any]] | None = None,
    *, total: int | None = None, erro: str | None = None, extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Molde único de bloco. `erro` != None → status 'erro' e total None."""
    if erro is not None:
        return {
            "chave": chave, "icone": icone, "titulo": titulo,
            "status": "erro", "erro": erro, "total": None, "itens": [],
            **(extra or {}),
        }
    itens = itens or []
    return {
        "chave": chave, "icone": icone, "titulo": titulo,
        "status": "ok", "erro": None,
        "total": len(itens) if total is None else int(total),
        "itens": itens,
        **(extra or {}),
    }


def _meu_dia_desde(at: datetime | None, agora: datetime) -> dict[str, Any]:
    """'há quanto tempo' pronto pro front (evita fuso divergente no browser)."""
    if at is None:
        return {"iso": None, "horas": None, "rotulo": None}
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    horas = max((agora - at).total_seconds() / 3600, 0)
    if horas < 1:
        rotulo = f"há {max(int(horas * 60), 1)} min"
    elif horas < 48:
        rotulo = f"há {int(horas)}h"
    else:
        rotulo = f"há {int(horas // 24)} dias"
    return {"iso": at.isoformat(), "horas": round(horas, 1), "rotulo": rotulo}


@router.get("/meu-dia")
def painel_meu_dia(
    limite: int = Query(25, ge=1, le=100, description="itens por bloco (o total é sempre exato)"),
    corretora: str | None = Query(
        None, description="admin: vê o Meu dia de UMA corretora (nome, rótulo ou id)"
    ),
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """👋 Meu dia — o que a corretora faz agora, em 5 blocos por custo de demora.

    PRIVACIDADE: corretora vê SÓ o dela (escopo por `_viewer_info`) e o
    `?corretora=` é IGNORADO pra ela — não é dela a escolha (mesma regra do
    /painel/tarefas). Admin sem filtro vê a equipe somada; com filtro vê o dia
    daquela corretora EXATAMENTE como ela veria.

    `pode_comemorar` só é True pra quem de fato trabalha a fila (corretora).
    Admin supervisiona — se o dia da Pessoa11 estiver limpo ele VÊ o estado limpo,
    mas a comemoração é dela, não dele.

    Performance: é a tela que abre toda manhã. Roda em UMA sessão, com um
    punhado de queries agregadas e SAVEPOINT por bloco (falha de um bloco não
    derruba a tela nem poisona a sessão dos outros). O filtro por corretora NÃO
    acrescenta consulta nenhuma: é o mesmo `:cid` que já parametrizava todas.
    """
    import re as _re

    from app.services.lead_spine import DEFAULT_TENANT_ID
    from app.services.ligacao_resultado import progresso_ligacao
    from app.services.pede_resposta import classificar

    user = _identificar_usuario(x_painel_user)
    viewer = _viewer_info(user)
    my_cid = viewer["corretora_id"]
    is_admin = _is_admin(user)
    if my_cid is None and not is_admin:
        raise HTTPException(403, "painel_user_sem_corretora")

    # ESCOPO EFETIVO — é a única variável que as 6 consultas enxergam.
    # Corretora: sempre o cid dela (o ?corretora= dela é descartado aqui, não
    # no front). Admin: o filtro escolhido, ou None (= equipe toda).
    filtro_cid = _corretora_id_do_filtro(corretora) if is_admin else None
    cid_efetivo = my_cid if not is_admin else filtro_cid

    tenant = DEFAULT_TENANT_ID
    agora = datetime.now(timezone.utc)
    hoje = _hoje_sp()
    inicio_dia = datetime.combine(hoje, datetime.min.time(), tzinfo=SP_TZ).astimezone(timezone.utc)

    # ── coleta ───────────────────────────────────────────────────────────────
    ult: dict[str, dict[str, Any]] = {}       # p8 → última msg da conversa
    teve_inbound: set[str] = set()            # p8 que já falou alguma vez
    erro_conversas: str | None = None
    snap_by_p8: dict[str, tuple[str | None, str | None]] = {}
    respondidos: list[dict[str, Any]] = []
    erro_respondidos: str | None = None
    leads_hoje: list[dict[str, Any]] = []
    erro_novos: str | None = None
    tarefas_total: int | None = None
    erro_tarefas: str | None = None
    meta_ligacao: dict[str, Any] | None = None
    erro_ligacoes: str | None = None

    with SessionLocal() as s:
        # (1) última mensagem por conversa + "já falou alguma vez", numa query.
        # Alimenta os blocos 1, 2 e 3.
        try:
            with s.begin_nested():
                rows = s.execute(
                    sql_text(r"""
                        WITH base AS (
                            SELECT right(regexp_replace(contato_telefone, '\D', '', 'g'), 8) AS p8,
                                   contato_telefone, contato_nome, corretora_id,
                                   from_corretora, tipo_mensagem, conteudo_texto,
                                   timestamp_evolution AS at
                              FROM mensagens_whatsapp
                             WHERE timestamp_evolution > NOW() - (:dias || ' days')::interval
                               AND contato_telefone IS NOT NULL
                               AND remote_jid NOT LIKE '%@g.us'
                               AND (CAST(:cid AS INT) IS NULL OR corretora_id = CAST(:cid AS INT))
                        ),
                        ult AS (
                            SELECT DISTINCT ON (p8) * FROM base ORDER BY p8, at DESC
                        ),
                        inb AS (
                            SELECT p8, bool_or(NOT from_corretora) AS teve_inbound
                              FROM base GROUP BY p8
                        )
                        SELECT u.p8, u.contato_telefone, u.contato_nome, u.corretora_id,
                               u.from_corretora, u.tipo_mensagem, u.conteudo_texto, u.at,
                               i.teve_inbound
                          FROM ult u JOIN inb i USING (p8)
                         WHERE u.p8 <> ''
                           AND u.p8 NOT IN (
                               SELECT right(regexp_replace(telefone_normalizado, '\D', '', 'g'), 8)
                                 FROM corretoras WHERE telefone_normalizado IS NOT NULL
                               UNION
                               SELECT right(regexp_replace(telefone_normalizado, '\D', '', 'g'), 8)
                                 FROM internos_acme WHERE telefone_normalizado IS NOT NULL
                           )
                    """),
                    {"dias": MEU_DIA_DIAS_CONVERSA, "cid": cid_efetivo},
                ).mappings().all()
            for r in rows:
                ult[r["p8"]] = dict(r)
                if r["teve_inbound"]:
                    teve_inbound.add(r["p8"])
            with s.begin_nested():
                for p8, lid, nome, _etapa in s.execute(sql_text(_sql_lead_por_p8())).all():
                    snap_by_p8.setdefault(p8, (str(lid) if lid else None, nome))
        except Exception as e:
            log.exception("meu_dia_conversas_failed", user=user)
            erro_conversas = type(e).__name__

        # (2) quem RESPONDEU a cadência nas últimas 48h (a mão levantada)
        try:
            with s.begin_nested():
                respondidos = [
                    dict(r) for r in s.execute(
                        sql_text("""
                            SELECT ce.telefone, ce.lead_id, ce.corretora_id,
                                   MAX(ev.at) AS respondeu_em
                              FROM cadencia_estado ce
                              JOIN cadencia_eventos ev
                                ON ev.cadencia_id = ce.id AND ev.tipo = 'resposta'
                             WHERE ce.tenant_id = :t
                               AND ce.status = 'respondida'
                               AND ev.at > NOW() - (:h || ' hours')::interval
                               AND (CAST(:cid AS INT) IS NULL OR ce.corretora_id = CAST(:cid AS INT))
                             GROUP BY ce.telefone, ce.lead_id, ce.corretora_id
                             ORDER BY 4 ASC
                        """),
                        {"t": tenant, "h": MEU_DIA_JANELA_RESPOSTA_H, "cid": cid_efetivo},
                    ).mappings().all()
                ]
        except Exception as e:
            log.exception("meu_dia_respondidos_failed", user=user)
            erro_respondidos = type(e).__name__

        # (2b) conversas que a corretora marcou como "feito" — o lead encerrou
        # e não há o que responder. Guardamos o INSTANTE, então mensagem nova
        # depois disso traz a conversa de volta sozinha.
        encerradas: dict[str, datetime] = {}
        try:
            with s.begin_nested():
                encerradas = {
                    r["p8"]: r["encerrado_ate"]
                    for r in s.execute(
                        sql_text(
                            "SELECT p8, encerrado_ate FROM conversa_encerrada "
                            "WHERE tenant_id = :t"
                        ),
                        {"t": tenant},
                    ).mappings().all()
                }
        except Exception as e:  # noqa: BLE001 — sem a marca o bloco só mostra mais
            log.warning("meu_dia_encerradas_indisponivel", erro=str(e)[:140])

        # (3) leads que CHEGARAM HOJE (oportunidade nova, ainda sem gente)
        try:
            with s.begin_nested():
                leads_hoje = [
                    dict(r) for r in s.execute(
                        sql_text("""
                            SELECT l.id, l.external_ref, l.nome, l.telefone,
                                   l.owner_id, l.origem, l.created_at
                              FROM leads l
                             WHERE l.tenant_id = :t
                               AND l.created_at >= :desde
                               AND (CAST(:cid AS INT) IS NULL OR l.owner_id = CAST(:cid AS INT))
                             ORDER BY l.created_at ASC
                        """),
                        {"t": tenant, "desde": inicio_dia, "cid": cid_efetivo},
                    ).mappings().all()
                ]
        except Exception as e:
            log.exception("meu_dia_leads_hoje_failed", user=user)
            erro_novos = type(e).__name__

        # (4) tarefas de hoje — manuais + ligação da cadência (mesma conta da
        # tela ☑️ Tarefas, escopo=hoje)
        try:
            with s.begin_nested():
                where = "status = 'aberta' AND data_tarefa <= :hoje"
                params: dict[str, Any] = {"hoje": hoje}
                frag, p_escopo = _escopo_tarefas(cid_efetivo, user)
                where += frag
                params.update(p_escopo)
                tarefas_total = int(s.execute(
                    sql_text(f"SELECT COUNT(*) FROM tarefas_corretora WHERE {where}"), params
                ).scalar() or 0)
            tarefas_total += len(_tarefas_ligacao_cadencia(
                cid_efetivo, is_admin=is_admin and cid_efetivo is None
            ))
        except Exception as e:
            log.exception("meu_dia_tarefas_failed", user=user)
            erro_tarefas = type(e).__name__

        # (5) ligações — meta do dia. `None` pra admin sem corretora: meta é de
        # quem liga, inventar uma pro admin poluiria o número dela.
        try:
            with s.begin_nested():
                meta_ligacao = progresso_ligacao(s, corretora_id=cid_efetivo)
        except Exception as e:
            log.exception("meu_dia_ligacoes_failed", user=user)
            erro_ligacoes = type(e).__name__

    # ── montagem ─────────────────────────────────────────────────────────────
    def _item(p8: str, *, at: datetime | None, preview: str | None,
              corretora_id: int | None, telefone: str | None,
              nome_fallback: str | None = None, contexto: str | None = None) -> dict[str, Any]:
        lid, nome = snap_by_p8.get(p8, (None, None))
        digitos = _re.sub(r"\D", "", telefone or "")
        return {
            "lead_id": lid or (f"tel:{digitos}" if digitos else None),
            "nome": nome or nome_fallback or (f"+{digitos}" if digitos else "Sem nome"),
            "telefone": f"+{digitos}" if digitos else None,
            "corretora": CORRETORA_LABEL.get(corretora_id) if corretora_id else None,
            "preview": (preview or "")[:140] or None,
            "contexto": contexto,
            "desde": _meu_dia_desde(at, agora),
        }

    def _encerrada(p8: str, at: datetime | None) -> bool:
        """Marcada como "feito" e SEM mensagem nova depois disso."""
        marca = encerradas.get(p8)
        if marca is None:
            return False
        if at is None:
            return True
        a = at if at.tzinfo else at.replace(tzinfo=timezone.utc)
        m = marca if marca.tzinfo else marca.replace(tzinfo=timezone.utc)
        return a <= m

    # bloco 1 — responderam e esperam você
    p8_responderam: set[str] = set()
    if erro_respondidos is None and erro_conversas is None:
        itens_resp: list[dict[str, Any]] = []
        encerradas_resp = 0
        for r in respondidos:
            p8 = _re.sub(r"\D", "", r["telefone"] or "")[-8:]
            u = ult.get(p8)
            # a corretora já retornou? (última msg da conversa é dela) → sai.
            # É a mesma regra do badge 🟢 do CRM, sem depender do board inteiro.
            if u is not None and u["from_corretora"]:
                continue
            # "feito": quem encerrou foi o lead, não havia o que responder
            if _encerrada(p8, (u or {}).get("at") or r["respondeu_em"]):
                encerradas_resp += 1
                continue
            p8_responderam.add(p8)
            itens_resp.append(_item(
                p8,
                at=(u or {}).get("at") or r["respondeu_em"],
                preview=(u or {}).get("conteudo_texto"),
                corretora_id=r["corretora_id"],
                telefone=r["telefone"],
                nome_fallback=(u or {}).get("contato_nome"),
                contexto="respondeu a cadência",
            ))
        itens_resp.sort(key=lambda i: i["desde"]["horas"] or 0, reverse=True)
        bloco_responderam = _meu_dia_bloco(
            "responderam", "🔴", "responderam e esperam você",
            itens_resp[:limite], total=len(itens_resp),
            # quantas saíram por "feito" — mesma transparência do bloco de
            # perguntas, pra o número menor não parecer bug.
            extra={"encerradas_manualmente": encerradas_resp},
        )
    else:
        bloco_responderam = _meu_dia_bloco(
            "responderam", "🔴", "responderam e esperam você",
            erro=erro_respondidos or erro_conversas,
        )

    # bloco 2 — conversas com pergunta em aberto (já sem os "ok" e "obrigado")
    if erro_conversas is None:
        itens_perg: list[dict[str, Any]] = []
        descartados = 0
        encerradas_perg = 0
        for p8, u in ult.items():
            if u["from_corretora"] or p8 in p8_responderam:
                continue
            decisao = classificar(u["conteudo_texto"], u["tipo_mensagem"])
            if not decisao.pede:
                descartados += 1
                continue
            # "feito" também vale aqui (pedido Dona 07/08): às vezes o filtro
            # automático mantém na dúvida e só a corretora sabe que acabou.
            if _encerrada(p8, u["at"]):
                encerradas_perg += 1
                continue
            item = _item(
                p8, at=u["at"], preview=u["conteudo_texto"],
                corretora_id=u["corretora_id"], telefone=u["contato_telefone"],
                nome_fallback=u["contato_nome"],
            )
            item["motivo"] = decisao.motivo
            item["duvida"] = decisao.duvida
            itens_perg.append(item)
        itens_perg.sort(key=lambda i: i["desde"]["horas"] or 0, reverse=True)
        bloco_perguntas = _meu_dia_bloco(
            "perguntas", "🟠", "perguntas em aberto",
            itens_perg[:limite], total=len(itens_perg),
            # transparência da limpeza do "OK": quantos SUMIRAM por serem
            # encerramento. Sem isso a corretora estranha o número menor.
            extra={"encerramentos_filtrados": descartados,
                   "encerradas_manualmente": encerradas_perg},
        )
    else:
        bloco_perguntas = _meu_dia_bloco(
            "perguntas", "🟠", "perguntas em aberto", erro=erro_conversas,
        )

    # bloco 3 — aguardando seu primeiro contato (oportunidade nova, não "calado")
    if erro_novos is None and erro_conversas is None:
        itens_novos: list[dict[str, Any]] = []
        encerradas_novos = 0
        for l in leads_hoje:
            p8 = _re.sub(r"\D", "", l["telefone"] or "")[-8:]
            if p8 and (p8 in teve_inbound or p8 in p8_responderam):
                continue  # já falou → não é "primeiro contato", é conversa
            # "feito" aqui costuma ser "falei por LIGAÇÃO" — não há mensagem
            # pra datar, então a marca é o instante do clique.
            if p8 and _encerrada(p8, None):
                encerradas_novos += 1
                continue
            criado = l["created_at"]
            digitos = _re.sub(r"\D", "", l["telefone"] or "")
            itens_novos.append({
                "lead_id": l["external_ref"] or (f"tel:{digitos}" if digitos else None),
                "nome": (l["nome"] or "").strip() or "Sem nome",
                "telefone": l["telefone"],
                "corretora": CORRETORA_LABEL.get(l["owner_id"]) if l["owner_id"] else None,
                "preview": None,
                "contexto": l["origem"] or "chegou hoje",
                "desde": _meu_dia_desde(criado, agora),
            })
        bloco_novos = _meu_dia_bloco(
            "primeiro_contato", "🟡", "aguardam primeiro contato",
            itens_novos[:limite], total=len(itens_novos),
            extra={"encerradas_manualmente": encerradas_novos},
        )
    else:
        bloco_novos = _meu_dia_bloco(
            "primeiro_contato", "🟡", "aguardam primeiro contato",
            erro=erro_novos or erro_conversas,
        )

    # bloco 4 — tarefas de hoje
    bloco_tarefas = (
        _meu_dia_bloco("tarefas", "🟢", "tarefas de hoje", total=tarefas_total or 0,
                       extra={"href": "/tarefas"})
        if erro_tarefas is None
        else _meu_dia_bloco("tarefas", "🟢", "tarefas de hoje", erro=erro_tarefas,
                            extra={"href": "/tarefas"})
    )

    # bloco 5 — ligações (importante e nunca urgente: por isso é o último)
    if erro_ligacoes is None:
        restante = int(meta_ligacao["restante_hoje"]) if meta_ligacao else 0
        bloco_ligacoes = _meu_dia_bloco(
            "ligacoes", "📞", "ligações pendentes", total=restante,
            extra={"href": "/tarefas?aba=ligar", "meta": meta_ligacao,
                   "sem_meta": meta_ligacao is None},
        )
    else:
        bloco_ligacoes = _meu_dia_bloco(
            "ligacoes", "📞", "ligações pendentes", erro=erro_ligacoes,
            extra={"href": "/tarefas?aba=ligar", "meta": None, "sem_meta": True},
        )

    # bloco 6 — RETOMAR (Dona, 17/08). Conversa do mês que esfriou e não morreu.
    #
    # Medido: 58 leads de agosto conversaram de verdade, não disseram não, e
    # pararam. A Elisangela trocou 30 mensagens e a última coisa que disse foi
    # "vou ver com ele e confirmo dia 22" — ninguém voltou. O Higor perguntou
    # "fica colocado ao shopping?" e espera há 10 dias.
    #
    # Fica ANTES das ligações de propósito: a ligação é base fria de
    # remarketing; aqui é gente que já falou com a corretora esta semana. Lead
    # morno de 4 dias volta muito mais fácil que lead de 6 meses.
    #
    # O que ordena é ENGAJAMENTO (quantas mensagens o lead mandou), não data:
    # quem escreveu 30 vezes vale mais que quem escreveu 2, mesmo que tenha
    # parado antes.
    retomar_itens: list[dict[str, Any]] = []
    erro_retomar: str | None = None
    try:
        with SessionLocal() as s:
            linhas = s.execute(
                sql_text(
                    r"""
                    WITH base AS (
                      SELECT l.id, l.external_ref, l.nome, l.telefone, l.owner_id,
                             coalesce(ps.nome, '-') AS etapa,
                             right(regexp_replace(coalesce(l.telefone,''),'\D','','g'),8) AS suf
                        FROM leads l
                        LEFT JOIN pipeline_stages ps ON ps.id = l.stage_id
                       WHERE l.tenant_id = 1
                         AND l.telefone IS NOT NULL
                         AND (l.created_at AT TIME ZONE 'America/Sao_Paulo')::date
                             >= date_trunc('month', now() AT TIME ZONE 'America/Sao_Paulo')::date
                         AND coalesce(ps.nome,'') NOT IN
                             ('Venda ganha','Cliente','Proposta','Compareceu','Venda perdida')
                    )
                    SELECT b.id, b.external_ref, b.nome, b.telefone, b.owner_id, b.etapa,
                           x.n_lead, x.ultima, x.ultima_fala,
                           EXTRACT(DAY FROM (now() - x.ultima))::int AS dias
                      FROM base b
                      JOIN LATERAL (
                        SELECT count(*) FILTER (WHERE NOT m.from_corretora) AS n_lead,
                               max(m.timestamp_evolution) AS ultima,
                               (array_agg(coalesce(m.conteudo_texto, m.transcricao,
                                                   '[' || m.tipo_mensagem || ']')
                                          ORDER BY m.timestamp_evolution DESC)
                                FILTER (WHERE NOT m.from_corretora))[1] AS ultima_fala
                          FROM mensagens_whatsapp m
                         -- `fone8` é a MESMA expressão, só que coluna gerada com
                         -- índice (ix_msg_fone8_ts). Escrita por extenso o banco
                         -- não usa o índice e relê as 129 mil mensagens por lead
                         -- do mês: 37s de 40s do Meu dia (25/09). Com fone8: 0,3s.
                         WHERE m.fone8 = b.suf
                      ) x ON TRUE
                     WHERE x.n_lead > 0
                       AND x.ultima < now() - interval '2 days'
                       AND NOT EXISTS (
                         SELECT 1 FROM cadencia_estado c
                          WHERE right(regexp_replace(c.telefone,'\D','','g'),8) = b.suf
                            AND c.status IN ('opt_out','ja_encontrou'))
                     ORDER BY x.n_lead DESC
                     LIMIT 60
                    """
                )
            ).mappings().all()
        for r in linhas:
            if cid_efetivo is not None and r["owner_id"] != cid_efetivo:
                continue
            fala = (r["ultima_fala"] or "").strip()
            # Quem já disse não NÃO entra — a mesma leitura que a Corretor usa, pra
            # não haver duas ideias diferentes de "recusa" na casa.
            if _recusou_na_conversa(fala):
                continue
            retomar_itens.append({
                # external_ref primeiro: é o id que o resto da casa entende
                # (/chat?lead=, lead-por-ref, adotar). Mandar `leads.id` aqui
                # abria a conversa (o /conversa aceita os dois) mas deixava o
                # seletor de etapa sem vínculo — a corretora escolhia a etapa e
                # tomava 404 lead_nao_encontrado (Pessoa11, 19-20/08).
                "lead_id": str(r["external_ref"] or r["id"]),
                "nome": r["nome"],
                "telefone": r["telefone"],
                "corretora": CORRETORA_LABEL.get(r["owner_id"]),
                "preview": fala[:90],
                "contexto": f"{r['n_lead']} mensagens · {r['etapa']}",
                "desde": {"iso": r["ultima"].isoformat() if r["ultima"] else None,
                          "horas": None,
                          "rotulo": f"parado {r['dias']}d" if r["dias"] is not None else None},
            })
    except Exception as e:  # noqa: BLE001
        erro_retomar = str(e)[:140]
        log.warning("meu_dia_retomar_falhou", erro=erro_retomar)

    bloco_retomar = (
        _meu_dia_bloco("retomar", "🔁", "conversas do mês pra retomar",
                       itens=retomar_itens[:25], total=len(retomar_itens))
        if erro_retomar is None
        else _meu_dia_bloco("retomar", "🔁", "conversas do mês pra retomar", erro=erro_retomar)
    )

    blocos = [bloco_responderam, bloco_perguntas, bloco_novos, bloco_tarefas,
              bloco_retomar, bloco_ligacoes]

    # Confete só com TUDO certo E TUDO zerado. Bloco em erro NUNCA vira "dia
    # limpo" — celebrar em cima de consulta que falhou é mentir pra ela.
    tudo_ok = all(b["status"] == "ok" for b in blocos)
    tudo_limpo = tudo_ok and all((b["total"] or 0) == 0 for b in blocos)

    return {
        "viewer": viewer,
        # quem pode escolher de quem é a tela (pills no front)
        "pode_filtrar": is_admin,
        "corretoras": [{"id": cid, "label": lbl, "nome": CORRETORA_NOME_DB.get(cid)}
                       for cid, lbl in sorted(CORRETORA_LABEL.items())] if is_admin else [],
        "filtro": CORRETORA_LABEL.get(filtro_cid) if filtro_cid else None,
        "filtro_corretora_id": filtro_cid,
        # de quem é o dia que está na tela (vira o "Meu dia — Pessoa11" do título)
        "dono": (
            CORRETORA_LABEL.get(cid_efetivo) if cid_efetivo
            else ("equipe" if is_admin else None)
        ),
        "data": hoje.isoformat(),
        "gerado_em": agora.isoformat(),
        "blocos": blocos,
        "tudo_ok": tudo_ok,
        "tudo_limpo": tudo_limpo,
        # Quem "limpa o dia" é a CORRETORA. Admin supervisiona — vê o estado
        # limpo, mas a comemoração (confete) não é dele, nem quando ele está
        # olhando o dia de alguém. Mesmo motivo pro atalho "começar" da fila de
        # ligação: a fila é de quem liga.
        "pode_comemorar": my_cid is not None,
        "pode_agir": my_cid is not None,
        "fontes": _frescor_fontes(),
    }


# ── Web Push · aviso de lead novo no celular ─────────────────────────────────
#
# Fluxo: o painel pede a chave pública (/push/chave), o navegador cria a
# inscrição e manda de volta (/push/inscrever). Quando cai lead novo, o
# lead_intake chama app.services.push.avisar_lead_novo.
#
# iPhone: só funciona com o painel INSTALADO na tela de início. As corretoras
# da Acme são todas iPhone, então esse passo é obrigatório no onboarding.


@router.get("/push/chave")
def painel_push_chave(
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Chave pública VAPID + se a corretora já tem algum aparelho ligado."""
    user = _identificar_usuario(x_painel_user)
    cid = _corretora_id_do_user(user)

    from app.config import settings
    from app.db import models as _models
    from app.db.session import SessionLocal

    with SessionLocal() as s:
        q = s.query(_models.PushInscricao).filter(
            _models.PushInscricao.ativa.is_(True)
        )
        # Admin não tem corretora_id: os aparelhos dela são identificados
        # pelo login.
        q = (
            q.filter(_models.PushInscricao.corretora_id == cid)
            if cid is not None
            else q.filter(_models.PushInscricao.usuario == user.lower())
        )
        ativos = q.count()
    return {
        "chave_publica": settings.vapid_public_key or None,
        "disponivel": bool(settings.vapid_public_key),
        "ativo": settings.push_lead_novo_ativo,
        "aparelhos": ativos,
    }


@router.post("/push/inscrever")
def painel_push_inscrever(
    body: dict[str, Any],
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
    user_agent: str | None = Header(default=None, alias="User-Agent"),
) -> dict[str, Any]:
    """Guarda (ou reativa) o aparelho da corretora logada.

    Body: {"endpoint": str, "keys": {"p256dh": str, "auth": str}}

    O endpoint é único por instalação — reinscrever o mesmo aparelho faz
    upsert, não duplica. A corretora vem do login, NUNCA do body: senão
    qualquer uma inscreveria um aparelho no nome de outra e passaria a receber
    os leads dela.
    """
    user = _identificar_usuario(x_painel_user)
    cid = _corretora_id_do_user(user)  # None = admin → recebe TODO lead novo

    endpoint = (body or {}).get("endpoint")
    keys = (body or {}).get("keys") or {}
    p256dh, auth = keys.get("p256dh"), keys.get("auth")
    if not endpoint or not p256dh or not auth:
        raise HTTPException(422, "inscrição incompleta (endpoint/p256dh/auth)")

    from app.db import models as _models
    from app.db.session import SessionLocal

    with SessionLocal() as s:
        ins = (
            s.query(_models.PushInscricao)
            .filter(_models.PushInscricao.endpoint == endpoint)
            .one_or_none()
        )
        if ins is None:
            ins = _models.PushInscricao(endpoint=endpoint)
            s.add(ins)
        ins.corretora_id = cid
        ins.usuario = user.lower()
        ins.p256dh = p256dh
        ins.auth = auth
        ins.user_agent = (user_agent or "")[:400] or None
        ins.ativa = True
        ins.desativada_motivo = None
        s.commit()
        ins_id = ins.id

    log.info("push_inscrito", user=user, corretora_id=cid, inscricao_id=ins_id)
    return {"ok": True, "inscricao_id": ins_id}


@router.post("/push/desinscrever")
def painel_push_desinscrever(
    body: dict[str, Any],
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Desliga o aviso naquele aparelho. Body: {"endpoint": str}."""
    user = _identificar_usuario(x_painel_user)
    cid = _corretora_id_do_user(user)
    endpoint = (body or {}).get("endpoint")
    if not endpoint:
        raise HTTPException(422, "endpoint obrigatório")

    from app.db import models as _models
    from app.db.session import SessionLocal

    with SessionLocal() as s:
        q = s.query(_models.PushInscricao).filter(
            _models.PushInscricao.endpoint == endpoint
        )
        # Cada uma só desliga o próprio aparelho.
        q = (
            q.filter(_models.PushInscricao.corretora_id == cid)
            if cid is not None
            else q.filter(_models.PushInscricao.usuario == user.lower())
        )
        ins = q.one_or_none()
        if ins is None:
            return {"ok": True, "status": "nao_encontrado"}
        ins.ativa = False
        ins.desativada_motivo = "desligado pela corretora"
        s.commit()

    log.info("push_desinscrito", user=user)
    return {"ok": True}


@router.post("/push/testar")
def painel_push_testar(
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Manda uma notificação de teste pros aparelhos da corretora logada.

    É o que prova pra ela, na hora, que o aviso funciona — sem esperar cair
    lead de verdade.
    """
    user = _identificar_usuario(x_painel_user)
    cid = _corretora_id_do_user(user)  # None = admin (o envio acha pelo NULL)

    from app.services.push import enviar_push

    r = enviar_push(
        corretora_id=cid,
        titulo="Testando 🔔",
        corpo="É assim que você vai saber que caiu lead.",
        url="/crm",
        tag="teste",
    )
    return r


@router.get("/lead/por-telefone")
def painel_lead_por_telefone(
    tel: str = Query(..., description="telefone em qualquer formato"),
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Dados mínimos de um lead pelo telefone — alimenta a tela que abre do push.

    É best-effort de propósito: a tela do celular já funciona só com o que veio
    na URL (nome e telefone), então os botões de ligar e WhatsApp não dependem
    disto. Aqui só acrescentamos origem, dona e o id do Kommo (quando o sync já
    criou), pra poder oferecer o link da conversa.

    Escopo: corretora só enxerga lead dela (fail-closed, igual às outras).
    """
    import re as _re

    user = _identificar_usuario(x_painel_user)
    cid = _corretora_id_do_user(user)

    from app.db import models as _models
    from app.db.session import SessionLocal

    digits = _re.sub(r"\D", "", tel or "")
    if len(digits) < 8:
        raise HTTPException(422, "telefone inválido")
    sufixo = digits[-8:]

    with SessionLocal() as s:
        q = s.query(_models.Lead).filter(_models.Lead.telefone.ilike(f"%{sufixo}%"))
        if cid is not None:
            q = q.filter(_models.Lead.owner_id == cid)
        lead = q.order_by(_models.Lead.created_at.desc()).first()
        if lead is None:
            return {"encontrado": False}

        dona = None
        if lead.owner_id is not None:
            c = (
                s.query(_models.Corretora)
                .filter(_models.Corretora.id == lead.owner_id)
                .one_or_none()
            )
            dona = c.nome if c else None

        return {
            "encontrado": True,
            "nome": lead.nome,
            "telefone": lead.telefone,
            "origem": lead.origem_campanha or lead.origem,
            "corretora": dona,
            "kommo_lead_id": lead.external_ref,
            "entrou": (
                lead.created_at.astimezone(SP_TZ).strftime("%d/%m às %H:%M")
                if lead.created_at
                else None
            ),
        }
