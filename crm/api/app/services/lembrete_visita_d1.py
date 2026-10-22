"""Lembrete de véspera (D-1) da visita — PRA CORRETORA, nunca pro cliente.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
POR QUE EXISTE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Conselho da agencia, 19/09/2026. Setembro (01-19): 104 leads → 21 visitas
marcadas → 10 CANCELADAS (47,6%) → 8 realizadas → 3 vendas. O gargalo não é
verba de mídia, é visita realizada. Recuperar 5 das 10 canceladas vale ~+1
venda ≈ R$ 4.800 pra imobiliária, com R$ 0 de mídia.

O `confirmar_domingo.py` (skills/agenda) é retrovisor: domingo 20h, pergunta se
as visitas da semana PASSADA aconteceram. Não existe nada olhando pra frente.
Este módulo é a véspera.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
A TRAVA QUE MANDA EM TUDO (decisão da Dona, 19/09/2026)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
A mensagem vai PARA A CORRETORA. Nunca para o lead. Quem fala com o cliente é
a corretora — o papel do copiloto é lembrar e cobrar a confirmação dela.

Isso não é só uma regra escrita: `_destino_valido` exige que o telefone de
destino case com uma corretora ATIVA do banco, e o envio não acontece sem isso.
O telefone do lead (`visitas.telefone_lead`) é lido do banco apenas para NÃO
ser usado — ele nunca entra em `_enviar`. Se um dia alguém tentar reaproveitar
este motor pra falar com o cliente, o teste `test_nunca_manda_pro_lead` cai.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
POR ONDE A MENSAGEM SAI
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Pela linha Cloud da Assistente (2955) — a mesma que já fala com as corretoras no
briefing de segunda e sexta. A Acme NÃO usa mais Evolution (instâncias
acme_* caídas desde julho/2026, de propósito); nada aqui encosta nela.

Dois caminhos, nesta ordem:

  1. `enviar_cloud_assistente` — texto livre. Se a corretora falou com a Assistente nas
     últimas 24h, a janela está aberta: a lista chega inteira, na hora, de
     graça. É o caminho normal (elas respondem o briefing toda semana).

  2. Janela fechada (erro 131047) → `recado_corretora.criar` guarda a lista
     como recado pendente e `briefing_push.enviar_aviso_briefing` manda o
     TEMPLATE UTILITY já aprovado, com o hook/cta desta automação nas
     variáveis. O clique em "Saiba mais" abre a janela e o handler de
     `cloud_assistente` entrega o recado — que tem prioridade sobre o briefing.

     ⚠️ EFEITO COLATERAL ACEITO: `recado_corretora` guarda UM pendente por
     corretora; criar o nosso cancela um recado que a Dona tenha deixado. Às
     18h isso é raro, e na véspera de uma visita a lista de amanhã é o que
     precisa ser lido primeiro. Está aqui escrito pra ninguém descobrir de
     susto.

NENHUM TEMPLATE NOVO É NECESSÁRIO. O esqueleto aprovado é genérico
("Olá {{1}}, tudo bem?\\n\\n{{2}}\\n\\nPara {{3}}, é só tocar no botão abaixo.")
e só as VARIÁVEIS mudam — o que não exige reaprovação na Meta. Um template
dedicado, com botões de confirmação, está PREPARADO e não submetido em
`scripts/lembrete_visita_d1_template.sh`.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
COMO A RESPOSTA VOLTA
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
O `confirmar_domingo.py` manda e morre: ninguém lê a resposta. Aqui ela volta.

A mensagem numera as visitas (1, 2, 3…) e pede resposta no formato "1 ok,
2 remarcou". O tick seguinte lê a thread da linha Assistente (`mensagens_whatsapp`,
instance `cloud_assistente`, inbound daquela corretora depois do envio) e interpreta
com um parser DETERMINÍSTICO — `interpretar`. Sem LLM: mudar o status de uma
visita por palpite de modelo é troca ruim.

O número é o contrato. Sem ele, casar "remarcou" com a visita certa seria
adivinhar nome de cliente no meio de um texto de WhatsApp. Quando o lote tem
UMA visita só, a palavra sozinha basta (não há o que confundir).

Resposta que chega e o parser não decifra vira `nao_entendido` — fica visível
como pendência humana e NUNCA muda status sozinha.

O que cada desfecho faz em `visitas`:
  • confirmada → NADA. A visita segue 'agendada', que já é "de pé". O registro
    da confirmação vive aqui.
  • cancelada  → `visitas.status = 'cancelada'`. É o dado que o conselho quer
    ver: cancelamento capturado NA VÉSPERA, não descoberto depois.
  • remarcada  → NADA em `visitas`, de propósito. A constraint
    `ck_visitas_status` só aceita agendada/compareceu/no_show/cancelada; criar
    um status 'remarcada' faria toda query que filtra 'agendada' (no-show,
    painel, funil) passar a ignorar essas visitas em silêncio. Remarcação fica
    registrada aqui e entra na lista de pendências: falta a nova data, e quem
    põe a nova data é gente.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
O SILÊNCIO É O CORAÇÃO DA COISA
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"O confirmar_domingo morre em silêncio quando ninguém responde, e a gente já
perdeu venda por isso." Então: na manhã do dia da visita, às `HORA_CORTE`, todo
lembrete sem resposta é carimbado (`sem_resposta_em`), aparece no endpoint
`/lembrete-visita-d1/pendentes` e vira um aviso no Telegram da Dona. Uma vez
por lembrete — o carimbo é o dedup.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
NASCE DESLIGADO
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
`lembrete_visita_d1_ativo` (config_operacional, default False). Desarmado, o
tick só LÊ e LOGA. Ligar é comando da Dona.
"""
from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import text as sql_text

from app.db.session import SessionLocal

log = structlog.get_logger()

SP = ZoneInfo("America/Sao_Paulo")

# Chave do kill-switch em `config_operacional`. Lida por SQL direto (e não por
# `config_operacional.get`) porque o mapa DEFAULTS de lá recusa chave que não
# esteja cadastrada nele — e cadastrar exige editar um arquivo que está em uso
# por outra frente. O fallback é o mesmo do resto da casa: sem linha no banco,
# vale o `.env`; sem env, DESLIGADO.
CHAVE_ATIVO = "lembrete_visita_d1_ativo"
ENV_ATIVO = "LEMBRETE_VISITA_D1_ATIVO"

# Hora de SP em que o lembrete da véspera sai. 18h: a corretora ainda está em
# horário de trabalho e o cliente dela ainda atende telefone à noite.
HORA_LEMBRETE = 18
# Depois disso o lembrete do dia não sai mais — véspera avisada às 23h não é
# véspera, é susto. Quem perdeu a janela vira pendência da manhã.
HORA_LIMITE_LEMBRETE = 21
# Manhã do dia da visita: quem não respondeu até aqui é cobrado.
HORA_CORTE = 9

DESFECHOS = ("confirmada", "remarcada", "cancelada", "nao_entendido")

_EMOJI_NUM = {"1️⃣": 1, "2️⃣": 2, "3️⃣": 3, "4️⃣": 4, "5️⃣": 5,
              "6️⃣": 6, "7️⃣": 7, "8️⃣": 8, "9️⃣": 9}

# Ordem de checagem IMPORTA: "remarcou" seria lido como confirmação por quem
# procurasse "marcou" primeiro, e "não vai" como positivo por quem procurasse
# "vai". Cancelar e remarcar vêm antes de confirmar, sempre.
_PALAVRAS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("cancelada", (
        "cancelou", "cancelada", "cancelado", "cancelei", "cancelar", "cancela",
        "desistiu", "desistencia", "nao vai mais", "nao vai", "nao vem",
        "nao podera", "nao pode mais", "furou", "caiu", "❌", "🚫",
    )),
    ("remarcada", (
        "remarcou", "remarcada", "remarcado", "remarcar", "remarquei", "remarca",
        "reagendou", "reagendar", "reagendada", "adiou", "adiar", "adiada",
        "mudou o dia", "mudou pra", "mudou para", "passou pra", "passou para",
        "transferiu", "outro dia", "outro horario", "🔄", "📅",
    )),
    ("confirmada", (
        "confirmada", "confirmado", "confirmei", "confirmou", "confirma",
        "esta de pe", "ta de pe", "de pe", "mantida", "mantem", "mantido",
        "tudo certo", "certo", "ok", "okay", "oks", "sim", "vai acontecer",
        "positivo", "beleza", "blz", "✅", "👍",
    )),
)

# "todas ok" / "todas confirmadas" / "tudo certo com todas" — atalho honesto:
# só vale acompanhado de uma palavra de CONFIRMAÇÃO. "todas canceladas" não
# ganha atalho nenhum: cancelar tudo de uma vez tem que ser dito visita a
# visita, senão um "cancela todas" no calor do dia apaga a agenda da semana.
_TODAS = ("todas", "todos", "tudo", "geral")


# ─── normalização ────────────────────────────────────────────────────────────
def _sem_acento(s: str) -> str:
    nfkd = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _normalizar(texto: str) -> str:
    """minúsculo, sem acento, com os emojis-número virados em dígito."""
    t = texto or ""
    for emoji, n in _EMOJI_NUM.items():
        t = t.replace(emoji, f" {n} ")
    return _sem_acento(t).lower()


def _so_digitos(tel: str | None) -> str:
    return "".join(c for c in (tel or "") if c.isdigit())


def _suf8(tel: str | None) -> str:
    d = _so_digitos(tel)
    return d[-8:] if len(d) >= 8 else ""


def _primeiro_nome(nome: str | None) -> str:
    partes = (nome or "").strip().split()
    return partes[0] if partes else "corretora"


# ─── kill-switch ─────────────────────────────────────────────────────────────
def ativo() -> bool:
    """Interruptor da automação. Banco > env > DESLIGADO.

    Nunca levanta: banco fora no meio de um tick não pode LIGAR a automação por
    acidente — e também não pode derrubar o timer. Erro → desligado.
    """
    try:
        with SessionLocal() as s:
            v = s.execute(
                sql_text("SELECT valor FROM config_operacional WHERE chave = :k"),
                {"k": CHAVE_ATIVO},
            ).scalar()
        if v is not None:
            return v is True or str(v).strip().lower() in ("true", "1", "sim")
    except Exception as e:  # noqa: BLE001 — falha FECHADO
        log.warning("lembrete_d1_config_indisponivel", erro=str(e)[:140])
    return os.environ.get(ENV_ATIVO, "").strip().lower() in ("true", "1", "sim")


# ─── o parser da resposta ────────────────────────────────────────────────────
def _classificar(trecho: str) -> str | None:
    """Primeiro desfecho que aparece no trecho, na ordem de precedência."""
    for desfecho, palavras in _PALAVRAS:
        for p in palavras:
            if p in trecho:
                return desfecho
    return None


def interpretar(texto: str, ordens: list[int]) -> dict[int, str]:
    """Lê a resposta da corretora e devolve {ordem_no_lote: desfecho}.

    `ordens` são os números ainda pendentes naquele lote. Função PURA — é ela
    que os testes apertam, porque é onde um erro vira status errado no banco.

    Três formas de resposta, nesta ordem:

      1. "todas ok" → confirma o lote inteiro (só pra CONFIRMAÇÃO, ver `_TODAS`).
      2. "1 ok, 2 remarcou" → cada número com a palavra que vem depois dele.
      3. lote de UMA visita e uma palavra solta ("cancelou") → aplica à única.

    Resposta com palavra de desfecho que não encaixa em nenhuma das três (dois
    números e nenhuma palavra, palavra e nenhum número num lote de três) vira
    `nao_entendido` no lote todo: alguém precisa ler com olhos. Resposta sem
    NENHUMA palavra de desfecho ("oi", "tá bom vou ver") devolve {} — não é uma
    resposta ao lembrete, e o lembrete segue pendente.
    """
    if not ordens:
        return {}
    t = _normalizar(texto)
    if not t.strip():
        return {}

    # 1. atalho do lote inteiro
    if any(p in t for p in _TODAS) and _classificar(t) == "confirmada":
        return {o: "confirmada" for o in ordens}

    # 2. pares "<número> <palavra>". Corta o texto em blocos que começam em cada
    #    número e vão até o próximo — a palavra que decide é a que está DENTRO
    #    do bloco do número, e não a primeira do texto inteiro.
    achados: dict[int, str] = {}
    marcas = [(m.start(), int(m.group(1))) for m in re.finditer(r"\b(\d{1,2})\b", t)]
    marcas = [(pos, n) for pos, n in marcas if n in ordens]
    for i, (pos, n) in enumerate(marcas):
        fim = marcas[i + 1][0] if i + 1 < len(marcas) else len(t)
        d = _classificar(t[pos:fim])
        if d:
            achados[n] = d
    if achados:
        return achados

    # 3. lote de uma visita: a palavra solta basta
    solto = _classificar(t)
    if solto and len(ordens) == 1:
        return {ordens[0]: solto}

    # Tem palavra de desfecho mas não dá pra saber de QUEM — não inventa.
    if solto:
        return {o: "nao_entendido" for o in ordens}
    return {}


# ─── seleção ─────────────────────────────────────────────────────────────────
# Visitas de AMANHÃ ainda de pé, com a corretora que as conduz. `telefone_lead`
# não é selecionado de propósito: ele não tem o que fazer neste motor.
_SQL_VISITAS_AMANHA = """
    SELECT v.id, v.lead_nome, v.bairro, v.data_hora,
           v.corretora_id, c.nome AS corretora_nome,
           c.telefone_normalizado AS corretora_tel
      FROM visitas v
      JOIN corretoras c ON c.id = v.corretora_id
     WHERE v.tenant_id = :t
       -- 'pendente_confirmacao' também está de pé (o CHECK de produção aceita
       -- os dois) e é justamente quem MAIS precisa da véspera: visita marcada
       -- e ainda não confirmada é a que fura.
       AND v.status IN ('agendada', 'pendente_confirmacao')
       AND (v.data_hora AT TIME ZONE 'America/Sao_Paulo')::date = :dia
       AND c.ativo IS TRUE
       AND coalesce(c.telefone_normalizado, '') <> ''
     ORDER BY v.corretora_id, v.data_hora
"""

_SQL_JA_TEM_LEMBRETE = """
    SELECT visita_id FROM visita_lembrete_d1
     WHERE tenant_id = :t AND visita_id = ANY(:ids)
"""

_SQL_INSERT = """
    INSERT INTO visita_lembrete_d1
        (tenant_id, visita_id, corretora_id, data_visita, ordem_no_lote,
         telefone_corretora, lead_nome)
    VALUES (:t, :vid, :corr, :dia, :ordem, :tel, :nome)
    ON CONFLICT ON CONSTRAINT uq_lembrete_d1_visita DO NOTHING
    RETURNING id
"""

_SQL_MARCA_ENVIO = """
    UPDATE visita_lembrete_d1
       SET enviado_em = now(), canal = :canal
     WHERE id = ANY(:ids)
"""

# Pendentes: já saiu o lembrete e a corretora ainda não disse nada.
_SQL_PENDENTES = """
    SELECT l.id, l.visita_id, l.corretora_id, l.ordem_no_lote, l.data_visita,
           l.telefone_corretora, l.lead_nome, l.enviado_em, l.sem_resposta_em,
           c.nome AS corretora_nome
      FROM visita_lembrete_d1 l
      LEFT JOIN corretoras c ON c.id = l.corretora_id
     WHERE l.tenant_id = :t
       AND l.enviado_em IS NOT NULL
       AND l.desfecho IS NULL
       AND l.data_visita >= :desde
     ORDER BY l.corretora_id, l.ordem_no_lote
"""

# Falas da corretora na linha da Assistente depois que o lembrete saiu. `cloud_assistente`
# é a instance da linha 2955 — a mesma do briefing. Evolution não entra aqui.
_SQL_RESPOSTAS = r"""
    SELECT coalesce(nullif(m.conteudo_texto, ''), m.transcricao) AS texto,
           m.timestamp_evolution
      FROM mensagens_whatsapp m
     WHERE m.instance = 'cloud_assistente'
       AND m.from_corretora IS FALSE
       AND right(regexp_replace(coalesce(m.contato_telefone, ''), '\D', '', 'g'), 8) = :suf
       AND m.timestamp_evolution > :depois
       AND coalesce(nullif(m.conteudo_texto, ''), m.transcricao) IS NOT NULL
     ORDER BY m.timestamp_evolution
     LIMIT 30
"""

_SQL_GRAVA_DESFECHO = """
    UPDATE visita_lembrete_d1
       SET desfecho = :d, desfecho_em = now(), resposta_texto = :txt
     WHERE id = :id AND desfecho IS NULL
"""

# Só cancela o que ainda está DE PÉ — corrida com o painel não pode reescrever
# uma visita que já virou 'compareceu' enquanto a corretora digitava.
_SQL_CANCELA_VISITA = """
    UPDATE visitas
       SET status = 'cancelada', atualizado_em = now(),
           status_atualizado_por_tel = :tel
     WHERE id = :vid AND status IN ('agendada', 'pendente_confirmacao')
"""

_SQL_CARIMBA_SILENCIO = """
    UPDATE visita_lembrete_d1
       SET sem_resposta_em = now()
     WHERE id = ANY(:ids) AND sem_resposta_em IS NULL
"""


@dataclass
class VisitaAmanha:
    visita_id: int
    lead_nome: str | None
    bairro: str | None
    data_hora: datetime
    corretora_id: int
    corretora_nome: str
    corretora_tel: str
    ordem: int = 0


@dataclass
class Lote:
    """As visitas de amanhã de UMA corretora — a unidade de envio."""

    corretora_id: int
    corretora_nome: str
    telefone: str
    dia: date
    visitas: list[VisitaAmanha] = field(default_factory=list)


def _amanha(agora: datetime | None = None) -> date:
    return ((agora or datetime.now(SP)).astimezone(SP) + timedelta(days=1)).date()


def janela_de_envio(agora: datetime | None = None) -> bool:
    """18h-21h de SP. Fora disso o lembrete do dia não sai."""
    a = (agora or datetime.now(SP)).astimezone(SP)
    return HORA_LEMBRETE <= a.hour < HORA_LIMITE_LEMBRETE


def selecionar_lotes(*, tenant: int = 1, dia: date | None = None) -> list[Lote]:
    """Visitas de amanhã ainda de pé, agrupadas por corretora, sem as que já
    receberam lembrete."""
    alvo = dia or _amanha()
    lotes: dict[int, Lote] = {}
    with SessionLocal() as s:
        linhas = s.execute(
            sql_text(_SQL_VISITAS_AMANHA), {"t": tenant, "dia": alvo}
        ).mappings().all()
        if not linhas:
            return []
        ids = [int(r["id"]) for r in linhas]
        ja = {
            int(r[0])
            for r in s.execute(sql_text(_SQL_JA_TEM_LEMBRETE), {"t": tenant, "ids": ids}).all()
        }
    for r in linhas:
        vid = int(r["id"])
        if vid in ja:
            continue
        cid = int(r["corretora_id"])
        lote = lotes.setdefault(
            cid,
            Lote(corretora_id=cid, corretora_nome=r["corretora_nome"],
                 telefone=r["corretora_tel"], dia=alvo),
        )
        lote.visitas.append(
            VisitaAmanha(
                visita_id=vid, lead_nome=r["lead_nome"], bairro=r["bairro"],
                data_hora=r["data_hora"], corretora_id=cid,
                corretora_nome=r["corretora_nome"], corretora_tel=r["corretora_tel"],
            )
        )
    for lote in lotes.values():
        for i, v in enumerate(lote.visitas, start=1):
            v.ordem = i
    return [l for l in lotes.values() if l.visitas]


# ─── a mensagem ──────────────────────────────────────────────────────────────
_DIAS = ("segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo")


def _hora_local(dt: datetime) -> str:
    return dt.astimezone(SP).strftime("%Hh%M").replace("h00", "h")


def montar_texto(lote: Lote) -> str:
    """A lista numerada. O número é o contrato com o parser — não tire."""
    dia = lote.dia
    cab = f"{_DIAS[dia.weekday()]}, {dia.strftime('%d/%m')}"
    n = len(lote.visitas)
    plural = "visita" if n == 1 else "visitas"
    linhas = [
        f"Oi, {_primeiro_nome(lote.corretora_nome)}! 🌹 Aqui é a Assistente.",
        "",
        f"Amanhã ({cab}) você tem {n} {plural}:",
        "",
    ]
    for v in lote.visitas:
        bairro = f" — {v.bairro}" if v.bairro else ""
        linhas.append(
            f"{v.ordem}. {_hora_local(v.data_hora)} — {v.lead_nome or 'cliente'}{bairro}"
        )
    linhas += [
        "",
        "Consegue confirmar com cada cliente ainda hoje? Confirmação na véspera "
        "é o que evita a visita furar.",
        "",
        "Depois me responde por aqui, assim:",
        f"  {lote.visitas[0].ordem} ok   ·   "
        f"{lote.visitas[0].ordem} remarcou   ·   {lote.visitas[0].ordem} cancelou",
    ]
    if n > 1:
        linhas.append('  (se estiverem todas de pé, pode mandar só "todas ok")')
    linhas += ["", "Obrigada! 💛"]
    return "\n".join(linhas)


def montar_hook_template(lote: Lote) -> tuple[str, str]:
    """({{2}}, {{3}}) do template UTILITY já aprovado. Sem \\n — a Meta rejeita
    quebra de linha dentro de parâmetro."""
    n = len(lote.visitas)
    plural = "visita" if n == 1 else "visitas"
    hook = (
        f"amanhã você tem {n} {plural} agendada{'s' if n > 1 else ''} e eu "
        f"preciso saber se está tudo confirmado com os clientes"
    )
    return hook, "ver as visitas de amanhã"


# ─── envio ───────────────────────────────────────────────────────────────────
def _destino_valido(telefone: str, corretora_id: int) -> bool:
    """A TRAVA. Só sai mensagem pra telefone de corretora ATIVA do banco.

    Não é redundância com a query de seleção: é a última porta antes do envio,
    e é ela que garante que nenhum caminho novo (um retry, um script de
    reenvio, uma refatoração distraída) consiga endereçar o telefone do lead.
    """
    suf = _suf8(telefone)
    if not suf:
        return False
    try:
        with SessionLocal() as s:
            achou = s.execute(
                sql_text(
                    r"""SELECT 1 FROM corretoras
                         WHERE id = :cid AND ativo IS TRUE
                           AND right(regexp_replace(coalesce(telefone_normalizado, ''),
                                                    '\D', '', 'g'), 8) = :suf
                         LIMIT 1"""
                ),
                {"cid": corretora_id, "suf": suf},
            ).first()
        return achou is not None
    except Exception as e:  # noqa: BLE001 — falha FECHADO: na dúvida não manda
        log.warning("lembrete_d1_destino_indeterminado", erro=str(e)[:140])
        return False


def _enviar(lote: Lote, texto: str) -> dict[str, Any]:
    """Texto livre pela janela; template + recado se ela estiver fechada."""
    from app.api.cloud_assistente import enviar_cloud_assistente

    if not _destino_valido(lote.telefone, lote.corretora_id):
        log.error("lembrete_d1_destino_recusado", corretora_id=lote.corretora_id)
        return {"ok": False, "error": "destino_nao_e_corretora", "sent": False}

    res = enviar_cloud_assistente(lote.telefone, texto)
    if res.get("ok"):
        return {**res, "canal": "texto_livre"}
    if res.get("error") != "fora_de_janela":
        return {**res, "canal": "texto_livre"}

    # Janela fechada: o conteúdo espera como recado e o template avisa.
    from app.services import briefing_push, recado_corretora

    try:
        recado_corretora.criar(
            corretora_id=lote.corretora_id, texto=texto, autor="lembrete_visita_d1"
        )
    except Exception as e:  # noqa: BLE001 — sem recado o template vira aviso vazio
        log.warning("lembrete_d1_recado_falhou", corretora_id=lote.corretora_id,
                    erro=str(e)[:160])
        return {"ok": False, "error": "recado_falhou", "sent": False}

    hook, cta = montar_hook_template(lote)
    # `edicao` escolhe o POOL de templates; os dois pools têm o MESMO corpo
    # ("Olá {{1}}, tudo bem?…"), o nome não aparece pra quem recebe. Sexta usa o
    # pool de sexta só pra rodízio — martelar um template só é o que a Meta lê
    # como qualidade ruim.
    edicao = "sex" if lote.dia.weekday() == 5 else "seg"
    res_t = briefing_push.enviar_aviso_briefing(
        corretora_id=lote.corretora_id, nome=lote.corretora_nome,
        telefone=lote.telefone, edicao=edicao, hook=hook, cta=cta,
    )
    return {**res_t, "canal": "template"}


def enviar_lembretes(
    *, tenant: int = 1, dia: date | None = None, dry_run: bool = False
) -> dict[str, Any]:
    """Fase 1 do tick: manda a véspera pra cada corretora com visita amanhã."""
    r: dict[str, Any] = {"lotes": 0, "visitas": 0, "enviados": 0, "pulados": {}}

    def pulou(m: str) -> None:
        r["pulados"][m] = r["pulados"].get(m, 0) + 1

    lotes = selecionar_lotes(tenant=tenant, dia=dia)
    r["lotes"] = len(lotes)
    r["visitas"] = sum(len(l.visitas) for l in lotes)
    if not lotes:
        return r

    for lote in lotes:
        texto = montar_texto(lote)
        if dry_run:
            log.info("lembrete_d1_dry_run", corretora=lote.corretora_nome,
                     visitas=len(lote.visitas), preview=texto[:160])
            r["enviados"] += 1
            r.setdefault("previa", []).append(
                {"corretora": lote.corretora_nome, "texto": texto}
            )
            continue

        # Reserva ANTES de enviar: se o envio falhar no meio, a linha existe sem
        # `enviado_em` e o próximo tick não manda de novo por engano — quem não
        # foi enviado aparece no log, não numa segunda mensagem pra corretora.
        with SessionLocal() as s:
            ids: list[int] = []
            for v in lote.visitas:
                got = s.execute(
                    sql_text(_SQL_INSERT),
                    {"t": tenant, "vid": v.visita_id, "corr": lote.corretora_id,
                     "dia": lote.dia, "ordem": v.ordem, "tel": lote.telefone,
                     "nome": v.lead_nome},
                ).first()
                if got:
                    ids.append(int(got[0]))
            s.commit()
        if not ids:
            pulou("ja_lembrado")
            continue

        res = _enviar(lote, texto)
        if res.get("ok"):
            with SessionLocal() as s:
                s.execute(sql_text(_SQL_MARCA_ENVIO),
                          {"ids": ids, "canal": res.get("canal")})
                s.commit()
            r["enviados"] += 1
            log.info("lembrete_d1_enviado", corretora=lote.corretora_nome,
                     visitas=len(lote.visitas), canal=res.get("canal"),
                     sent=res.get("sent"))
        else:
            pulou(str(res.get("error") or "erro"))
            log.warning("lembrete_d1_envio_falhou", corretora=lote.corretora_nome,
                        erro=str(res.get("error"))[:120])
    return r


# ─── leitura das respostas ───────────────────────────────────────────────────
def _pendentes(s: Any, tenant: int, desde: date) -> list[dict[str, Any]]:
    return [dict(r) for r in s.execute(
        sql_text(_SQL_PENDENTES), {"t": tenant, "desde": desde}
    ).mappings().all()]


def _aplicar(s: Any, linha: dict[str, Any], desfecho: str, texto: str) -> bool:
    """Grava o desfecho e, só pra 'cancelada', mexe em `visitas`."""
    ok = s.execute(
        sql_text(_SQL_GRAVA_DESFECHO),
        {"d": desfecho, "txt": texto[:1000], "id": linha["id"]},
    ).rowcount
    if not ok:
        return False
    if desfecho == "cancelada":
        s.execute(
            sql_text(_SQL_CANCELA_VISITA),
            {"vid": linha["visita_id"], "tel": linha["telefone_corretora"]},
        )
    return True


def ler_respostas(*, tenant: int = 1, dry_run: bool = False) -> dict[str, Any]:
    """Fase 2: lê a thread da linha Assistente e fecha os lembretes respondidos."""
    r: dict[str, Any] = {"pendentes": 0, "lidos": 0, "por_desfecho": {}}
    desde = (datetime.now(SP) - timedelta(days=3)).date()

    with SessionLocal() as s:
        linhas = _pendentes(s, tenant, desde)
        r["pendentes"] = len(linhas)
        if not linhas:
            return r

        # Agrupa por (corretora, dia): a resposta "1 ok, 2 remarcou" vale pro
        # LOTE inteiro daquele dia, não pra uma linha isolada.
        lotes: dict[tuple[int, date], list[dict[str, Any]]] = {}
        for l in linhas:
            lotes.setdefault((l["corretora_id"], l["data_visita"]), []).append(l)

        for (cid, dia), grupo in lotes.items():
            primeiro_envio = min(g["enviado_em"] for g in grupo)
            falas = s.execute(
                sql_text(_SQL_RESPOSTAS),
                {"suf": _suf8(grupo[0]["telefone_corretora"]), "depois": primeiro_envio},
            ).all()
            if not falas:
                continue
            texto = "\n".join(f[0] for f in falas if f[0])
            ordens = [int(g["ordem_no_lote"]) for g in grupo]
            leitura = interpretar(texto, ordens)
            if not leitura:
                continue
            por_ordem = {int(g["ordem_no_lote"]): g for g in grupo}
            for ordem, desfecho in leitura.items():
                linha = por_ordem.get(ordem)
                if linha is None:
                    continue
                if dry_run:
                    log.info("lembrete_d1_leitura_dry_run", corretora_id=cid,
                             visita_id=linha["visita_id"], desfecho=desfecho)
                elif not _aplicar(s, linha, desfecho, texto):
                    continue
                r["lidos"] += 1
                r["por_desfecho"][desfecho] = r["por_desfecho"].get(desfecho, 0) + 1
                log.info("lembrete_d1_desfecho", corretora_id=cid,
                         visita_id=linha["visita_id"], desfecho=desfecho,
                         dry_run=dry_run)
        if not dry_run:
            s.commit()
    return r


# ─── a cobrança do silêncio ──────────────────────────────────────────────────
def cobrar_silencio(
    *, tenant: int = 1, agora: datetime | None = None, dry_run: bool = False
) -> dict[str, Any]:
    """Fase 3: na manhã da visita, quem não respondeu vira pendência visível.

    É o que o `confirmar_domingo` nunca teve. Sem isto, o lembrete vira mais uma
    mensagem que ninguém responde — e a visita fura do mesmo jeito.
    """
    a = (agora or datetime.now(SP)).astimezone(SP)
    r: dict[str, Any] = {"sem_resposta": 0, "avisou_dona": False}
    if a.hour < HORA_CORTE:
        r["pulou"] = "antes_do_corte"
        return r

    hoje = a.date()
    with SessionLocal() as s:
        linhas = [
            l for l in _pendentes(s, tenant, hoje)
            if l["data_visita"] == hoje and l["sem_resposta_em"] is None
        ]
        r["sem_resposta"] = len(linhas)
        if not linhas:
            return r
        if not dry_run:
            s.execute(sql_text(_SQL_CARIMBA_SILENCIO), {"ids": [l["id"] for l in linhas]})
            s.commit()

    por_corretora: dict[str, list[str]] = {}
    for l in linhas:
        nome = l["corretora_nome"] or f"corretora {l['corretora_id']}"
        por_corretora.setdefault(nome, []).append(l["lead_nome"] or "cliente")

    partes = [
        "⚠️ Visita de HOJE sem confirmação da corretora",
        "",
        "Mandei o lembrete ontem à noite e não voltou resposta:",
        "",
    ]
    for nome, clientes in sorted(por_corretora.items()):
        partes.append(f"• {nome}: {', '.join(clientes)}")
    partes += ["", "Vale uma cobrança na mão antes do horário."]
    aviso = "\n".join(partes)

    if dry_run:
        log.info("lembrete_d1_silencio_dry_run", total=len(linhas), preview=aviso[:200])
        r["aviso"] = aviso
        return r

    try:
        from app.tools.telegram import TelegramTool

        TelegramTool().send_to_dona(aviso)
        r["avisou_dona"] = True
    except Exception as e:  # noqa: BLE001 — Telegram fora não pode derrubar o tick
        log.warning("lembrete_d1_aviso_dona_falhou", erro=str(e)[:160])
    log.info("lembrete_d1_silencio", total=len(linhas),
             corretoras=len(por_corretora), avisou=r["avisou_dona"])
    return r


# ─── métricas ────────────────────────────────────────────────────────────────
# Sem isto não dá pra saber se funcionou — e "achei que melhorou" não paga
# reunião de conselho. Semana ISO (segunda a domingo), fuso SP.
_SQL_METRICAS = """
    SELECT to_char(date_trunc('week', data_visita), 'IYYY-"W"IW') AS semana,
           date_trunc('week', data_visita)::date                  AS inicio,
           count(*) FILTER (WHERE enviado_em IS NOT NULL)          AS lembretes,
           count(*) FILTER (WHERE desfecho = 'confirmada')         AS confirmadas,
           count(*) FILTER (WHERE desfecho = 'remarcada')          AS remarcadas,
           count(*) FILTER (WHERE desfecho = 'cancelada')          AS canceladas,
           count(*) FILTER (WHERE desfecho = 'nao_entendido')      AS nao_entendidas,
           count(*) FILTER (WHERE enviado_em IS NOT NULL
                              AND desfecho IS NULL)                AS sem_resposta
      FROM visita_lembrete_d1
     WHERE tenant_id = :t AND data_visita >= :desde
     GROUP BY 1, 2
     ORDER BY 2 DESC
"""


def metricas_semanais(*, tenant: int = 1, semanas: int = 8) -> list[dict[str, Any]]:
    """Por semana: quantas visitas tiveram lembrete, quantas a corretora
    confirmou, remarcou, cancelou NA VÉSPERA, e quantas ficaram sem resposta."""
    desde = (datetime.now(SP) - timedelta(weeks=max(1, semanas))).date()
    with SessionLocal() as s:
        linhas = s.execute(sql_text(_SQL_METRICAS), {"t": tenant, "desde": desde}).mappings().all()
    out: list[dict[str, Any]] = []
    for r in linhas:
        d = dict(r)
        d["inicio"] = d["inicio"].isoformat() if d["inicio"] else None
        enviados = int(d["lembretes"] or 0)
        respondidos = enviados - int(d["sem_resposta"] or 0)
        d["taxa_resposta"] = round(respondidos / enviados * 100, 1) if enviados else 0.0
        out.append(d)
    return out


def pendencias(*, tenant: int = 1, dias: int = 7) -> dict[str, Any]:
    """O que precisa de olho humano: silêncio carimbado, resposta indecifrada e
    remarcação esperando data nova."""
    desde = (datetime.now(SP) - timedelta(days=max(1, dias))).date()
    with SessionLocal() as s:
        linhas = s.execute(
            sql_text(
                """SELECT l.id, l.visita_id, l.data_visita, l.lead_nome,
                          l.desfecho, l.sem_resposta_em, l.resposta_texto,
                          c.nome AS corretora
                     FROM visita_lembrete_d1 l
                     LEFT JOIN corretoras c ON c.id = l.corretora_id
                    WHERE l.tenant_id = :t
                      AND l.data_visita >= :desde
                      AND l.enviado_em IS NOT NULL
                      AND (l.sem_resposta_em IS NOT NULL
                           OR l.desfecho IN ('nao_entendido', 'remarcada'))
                    ORDER BY l.data_visita DESC, c.nome"""
            ),
            {"t": tenant, "desde": desde},
        ).mappings().all()
    itens = []
    for r in linhas:
        d = dict(r)
        d["data_visita"] = d["data_visita"].isoformat() if d["data_visita"] else None
        d["sem_resposta_em"] = (
            d["sem_resposta_em"].isoformat() if d["sem_resposta_em"] else None
        )
        d["motivo"] = (
            "sem_resposta" if r["sem_resposta_em"] and not r["desfecho"]
            else str(r["desfecho"])
        )
        itens.append(d)
    return {"total": len(itens), "itens": itens}


# ─── o tick ──────────────────────────────────────────────────────────────────
def rodar(*, tenant: int = 1, dry_run: bool = False,
          agora: datetime | None = None) -> dict[str, Any]:
    """Um tick completo. NUNCA levanta — erro aqui não derruba o timer."""
    r: dict[str, Any] = {"dry_run": bool(dry_run), "ativo": False}
    try:
        ligado = ativo()
        r["ativo"] = ligado
        # Desarmado: continua LENDO (respostas e silêncio são leitura de estado,
        # não envio) mas nunca manda mensagem. Assim o interruptor não esconde a
        # operação — só cala a automação.
        if ligado and janela_de_envio(agora):
            r["envio"] = enviar_lembretes(tenant=tenant, dry_run=dry_run)
        else:
            r["envio"] = {"pulados": {"desligado" if not ligado else "fora_da_janela": 1}}
        r["respostas"] = ler_respostas(tenant=tenant, dry_run=dry_run)
        r["silencio"] = cobrar_silencio(tenant=tenant, agora=agora,
                                        dry_run=dry_run or not ligado)
    except Exception as e:  # noqa: BLE001
        log.warning("lembrete_d1_tick_falhou", erro=str(e)[:200])
        r["erro"] = str(e)[:200]
    return r
