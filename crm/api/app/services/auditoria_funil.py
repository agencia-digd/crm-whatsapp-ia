"""Vasculhada semanal do funil — lê CONVERSA, não etapa.

━━ POR QUE EXISTE ━━

Em 17/08 a Dona pediu pra eu olhar as conversas do mês. Em uma hora achei:

  • uma lead que compareceu, entregou documentação e aguardava crédito — no
    sistema era NO-SHOW, card em "Follow-up" e fila de remarketing;
  • uma lead que AVISOU na véspera que não ia viajar — marcada como falta;
  • uma visita que aconteceu e nunca foi registrada;
  • 6 negociações avançadas que ninguém estava reportando;
  • 74% das visitas de agosto sem telefone, o que cega o funil inteiro.

Nada disso apareceu em relatório nenhum. Todos os números que a gente olhava —
taxa de agendamento, comparecimento, etapa do lead — vinham de campos que
dependem de alguém mover card na mão.

A Dona foi direta: "isso não pode acontecer, uma vez na semana você deveria
fazer essa vasculhada". Este módulo é isso, e roda na ORDEM que ela pediu:

  1. quem NÃO compareceu     → avisou? remarcou? sumiu?
  2. quem COMPARECEU         → e depois, o que aconteceu?
  3. todos que ENTRARAM no mês
  4. todos que CONVERSARAM no mês

A ordem não é arbitrária: vai do caso mais caro e mais raro pro mais amplo. Quem
faltou é pouca gente e cada um vale muito; quem conversou é muita gente e o que
interessa ali é padrão, não caso.

━━ O QUE ELE NÃO FAZ ━━

Não move card, não manda mensagem pra lead, não muda status. Só LÊ e aponta.
Corrigir dado de cliente é decisão de gente — e eu já errei fazendo isso sozinha
(mandei um lead pra Venda perdida lendo "já comprei" sem ler a frase seguinte,
onde ele oferecia permuta).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import structlog
from sqlalchemy import text as sql_text

from app.db.session import SessionLocal
from app.services import sinal_negocio

log = structlog.get_logger()


@dataclass
class Achado:
    """Uma coisa que o sistema está contando errado, com a prova."""
    camada: str          # "no_show" | "compareceu" | "entraram" | "conversaram"
    gravidade: str       # "alta" | "media" | "baixa"
    quem: str
    o_que: str           # o que o sistema diz
    evidencia: str       # a frase da conversa que contradiz
    acao: str            # o que fazer


@dataclass
class Auditoria:
    achados: list[Achado] = field(default_factory=list)
    numeros: dict[str, Any] = field(default_factory=dict)

    def por_camada(self, camada: str) -> list[Achado]:
        return [a for a in self.achados if a.camada == camada]


# ── vocabulário, por intenção ────────────────────────────────────────────────
#
# São listas curtas de propósito: cada expressão aqui saiu de uma conversa real.
# Lista inventada "por completude" gera falso positivo que ninguém entende.

_AVISOU_QUE_NAO_IA = (
    "não vou conseguir", "nao vou conseguir", "não consigo ir", "nao consigo ir",
    "não vai dar", "nao vai dar", "amanhã não", "amanha nao", "hoje não", "hoje nao",
    "vou ter que desmarcar", "preciso desmarcar", "não poderei", "nao poderei",
    "estou indo pra", "to indo pra", "viajar", "viagem", "imprevisto",
    "aconteceu um problema", "fiquei doente", "estou doente",
)

_REMARCOU = (
    "podemos remarcar", "remarcar", "outro dia", "outra data", "semana que vem",
    "amanhã posso", "amanha posso", "depois de amanhã", "que dia você tem",
    "que dia vc tem", "tô livre", "to livre", "estou livre", "consigo dia",
)

_COMPARECEU_DE_FATO = (
    "cheguei", "estou aqui", "to aqui", "tô aqui", "já estou", "ja estou",
    "estou chegando", "to chegando", "adorei", "gostei muito", "gostamos",
)

_SUMIU_SEM_FALAR = ("[sem resposta]",)   # sentinela; a lógica é por ausência


def _falas(s, suf: str, desde: datetime | None = None, ate: datetime | None = None) -> str:
    """Conversa inteira daquele telefone, achatada. Usa `transcricao` quando o
    texto é áudio — metade da conversa da Acme é áudio, e ignorar isso é
    auditar meia conversa."""
    cond = ["right(regexp_replace(coalesce(contato_telefone,''), '\\D', '', 'g'), 8) = :suf"]
    p: dict[str, Any] = {"suf": suf}
    if desde is not None:
        cond.append("timestamp_evolution >= :desde"); p["desde"] = desde
    if ate is not None:
        cond.append("timestamp_evolution <= :ate"); p["ate"] = ate
    r = s.execute(
        sql_text(
            f"""SELECT string_agg(coalesce(conteudo_texto, transcricao, ''), ' ¶ '
                        ORDER BY timestamp_evolution)
                  FROM mensagens_whatsapp WHERE {' AND '.join(cond)}"""
        ), p,
    ).scalar()
    return (r or "").lower()


def _tem(falas: str, termos: tuple[str, ...]) -> str | None:
    for t in termos:
        if t in falas:
            return t
    return None


def _suf(tel: str | None) -> str:
    d = "".join(c for c in (tel or "") if c.isdigit())
    return d[-8:] if len(d) >= 8 else ""


# ── CAMADA 1: quem não compareceu ────────────────────────────────────────────

def _camada_no_show(s, desde: datetime) -> list[Achado]:
    """A mais cara: falta registrada que na verdade era outra coisa.

    Dos 4 no-shows de agosto, 2 estavam errados — um avisou na véspera, outro
    remarcou e compareceu. Metade do indicador era ficção.
    """
    out: list[Achado] = []
    linhas = s.execute(
        sql_text(
            """SELECT id, lead_nome, telefone_lead, corretora_id, data_hora
                 FROM visitas
                WHERE status = 'no_show' AND data_hora >= :desde
                ORDER BY data_hora"""
        ), {"desde": desde},
    ).mappings().all()

    for v in linhas:
        suf = _suf(v["telefone_lead"])
        if not suf:
            out.append(Achado(
                camada="no_show", gravidade="media", quem=v["lead_nome"] or f"visita {v['id']}",
                o_que="marcada como falta, e a visita não tem telefone",
                evidencia="sem telefone não dá pra ler a conversa nem ligar ao lead",
                acao="preencher o telefone da visita",
            ))
            continue

        antes = _falas(s, suf, ate=v["data_hora"])
        depois = _falas(s, suf, desde=v["data_hora"])

        # avisou ANTES que não ia? então é cancelamento, não falta
        aviso = _tem(antes[-1500:], _AVISOU_QUE_NAO_IA) if antes else None
        if aviso:
            out.append(Achado(
                camada="no_show", gravidade="alta", quem=v["lead_nome"] or "?",
                o_que="marcada como FALTA",
                evidencia=f'avisou antes: "…{aviso}…"',
                acao="mudar pra CANCELADA — quem avisa não faltou",
            ))
            continue

        # compareceu DEPOIS (remarcou)? então existe visita não registrada
        veio = _tem(depois, _COMPARECEU_DE_FATO) if depois else None
        if veio:
            out.append(Achado(
                camada="no_show", gravidade="alta", quem=v["lead_nome"] or "?",
                o_que="marcada como FALTA e nunca mais nada",
                evidencia=f'depois disse: "…{veio}…"',
                acao="registrar a visita que aconteceu — ela não existe no sistema",
            ))
            continue

        remarcou = _tem(depois, _REMARCOU) if depois else None
        if remarcou:
            out.append(Achado(
                camada="no_show", gravidade="media", quem=v["lead_nome"] or "?",
                o_que="marcada como falta",
                evidencia=f'falou de remarcar: "…{remarcou}…"',
                acao="conferir se a nova data foi agendada",
            ))
            continue

        if not depois.strip():
            out.append(Achado(
                camada="no_show", gravidade="baixa", quem=v["lead_nome"] or "?",
                o_que="faltou e ninguém falou com ela depois",
                evidencia="nenhuma mensagem após a data da visita",
                acao="é o no-show de verdade — vale um toque de recuperação",
            ))
    return out


# ── CAMADA 2: quem compareceu ────────────────────────────────────────────────

def _camada_compareceu(s, desde: datetime) -> list[Achado]:
    """Visita que aconteceu é o ativo mais caro da casa. O que veio depois?"""
    out: list[Achado] = []
    linhas = s.execute(
        sql_text(
            """SELECT v.id, v.lead_nome, v.telefone_lead, v.corretora_id, v.data_hora,
                      (SELECT ps.nome FROM leads l JOIN pipeline_stages ps ON ps.id = l.stage_id
                        WHERE right(regexp_replace(coalesce(l.telefone,''),'\\D','','g'),8)
                            = right(regexp_replace(coalesce(v.telefone_lead,''),'\\D','','g'),8)
                        LIMIT 1) AS etapa
                 FROM visitas v
                WHERE v.status = 'compareceu' AND v.data_hora >= :desde
                ORDER BY v.data_hora"""
        ), {"desde": desde},
    ).mappings().all()

    for v in linhas:
        suf = _suf(v["telefone_lead"])
        if not suf:
            out.append(Achado(
                camada="compareceu", gravidade="alta", quem=v["lead_nome"] or f"visita {v['id']}",
                o_que="COMPARECEU e a visita não tem telefone",
                evidencia="não dá pra ligar essa visita a lead nenhum",
                acao="preencher o telefone — visita realizada é o dado mais caro que existe",
            ))
            continue

        depois = _falas(s, suf, desde=v["data_hora"])
        etapa = v["etapa"] or "sem card"

        if not depois.strip():
            out.append(Achado(
                camada="compareceu", gravidade="alta", quem=v["lead_nome"] or "?",
                o_que=f"compareceu e NINGUÉM falou com ela depois (está em {etapa})",
                evidencia="nenhuma mensagem após a visita",
                acao="retomar — visita sem follow-up é dinheiro na mesa",
            ))
            continue

        # sinal de negócio depois da visita, mas card parado atrás
        if sinal_negocio._FORTE.search(depois) and etapa not in sinal_negocio.ETAPAS_OK:
            m = sinal_negocio._FORTE.search(depois)
            out.append(Achado(
                camada="compareceu", gravidade="alta", quem=v["lead_nome"] or "?",
                o_que=f"negociando de verdade, mas o card está em {etapa}",
                evidencia=f'depois da visita: "…{m.group(0)}…"',
                acao="subir o card — pode estar fechando",
            ))
    return out


# ── CAMADA 3: todos que entraram no mês ──────────────────────────────────────

def _camada_entraram(s, desde: datetime) -> list[Achado]:
    """O volume. Aqui o que interessa é quem caiu no vão: entrou e nunca foi
    falado, ou respondeu e ninguém voltou."""
    out: list[Achado] = []
    linhas = s.execute(
        sql_text(
            """SELECT l.id, l.nome, l.telefone, l.owner_id, l.created_at,
                      coalesce(ps.nome,'sem card') AS etapa
                 FROM leads l LEFT JOIN pipeline_stages ps ON ps.id = l.stage_id
                WHERE l.created_at >= :desde AND l.telefone IS NOT NULL"""
        ), {"desde": desde},
    ).mappings().all()

    sem_toque = 0
    for l in linhas:
        suf = _suf(l["telefone"])
        if not suf:
            continue
        r = s.execute(
            sql_text(
                """SELECT
                     count(*) FILTER (WHERE from_corretora) AS nossas,
                     count(*) FILTER (WHERE NOT from_corretora) AS deles,
                     count(*) FILTER (WHERE from_corretora
                        AND coalesce(tipo_mensagem,'') <> 'template') AS humanas
                   FROM mensagens_whatsapp
                  WHERE right(regexp_replace(coalesce(contato_telefone,''),'\\D','','g'),8) = :suf
                    AND timestamp_evolution >= :desde"""
            ), {"suf": suf, "desde": l["created_at"]},
        ).mappings().first()

        if r["deles"] > 0 and r["humanas"] == 0:
            out.append(Achado(
                camada="entraram", gravidade="alta", quem=l["nome"] or "?",
                o_que=f"RESPONDEU e só recebeu automação (está em {l['etapa']})",
                evidencia=f"{r['deles']} mensagens dele, {r['nossas']} nossas — nenhuma humana",
                acao="alguém precisa falar com essa pessoa",
            ))
        elif r["nossas"] == 0:
            sem_toque += 1

    if sem_toque:
        out.append(Achado(
            camada="entraram", gravidade="media", quem=f"{sem_toque} leads",
            o_que="entraram e não receberam NADA — nem automação",
            evidencia="zero mensagens nossas desde o cadastro",
            acao="conferir a boas-vindas: lead que entra com ela desligada nunca recebe",
        ))
    return out


# ── CAMADA 4: todos que conversaram no mês ───────────────────────────────────

def _camada_conversaram(s, desde: datetime) -> list[Achado]:
    """O padrão, não o caso. Aqui a pergunta é: COMO as conversas morrem?"""
    out: list[Achado] = []
    negocios = [n for n in sinal_negocio.procurar(dias=30) if n.forca == "forte"]
    for n in negocios:
        out.append(Achado(
            camada="conversaram", gravidade="alta", quem=n.nome or n.telefone[-4:],
            o_que=f"sinal forte de negócio, card em {n.etapa or 'nenhum'}",
            evidencia=f'"{n.trechos[0][:80]}"' if n.trechos else "—",
            acao="subir o card",
        ))

    # Como as conversas terminaram do NOSSO lado — o padrão que mata o funil.
    r = s.execute(
        sql_text(
            """WITH ult AS (
                 SELECT right(regexp_replace(coalesce(contato_telefone,''),'\\D','','g'),8) AS suf,
                        (array_agg(from_corretora ORDER BY timestamp_evolution DESC))[1] AS nossa,
                        (array_agg(coalesce(conteudo_texto,'') ORDER BY timestamp_evolution DESC))[1] AS txt,
                        (array_agg(coalesce(tipo_mensagem,'') ORDER BY timestamp_evolution DESC))[1] AS tipo
                   FROM mensagens_whatsapp
                  WHERE timestamp_evolution >= :desde AND contato_telefone IS NOT NULL
                  GROUP BY 1)
               SELECT count(*) FILTER (WHERE nossa AND tipo <> 'template' AND txt NOT LIKE '%?%') AS sem_pergunta,
                      count(*) FILTER (WHERE nossa AND tipo <> 'template') AS nossas
                 FROM ult"""
        ), {"desde": desde},
    ).mappings().first()
    if r and r["nossas"]:
        pct = round(100 * r["sem_pergunta"] / r["nossas"])
        if pct >= 50:
            out.append(Achado(
                camada="conversaram", gravidade="media", quem=f"{r['sem_pergunta']} conversas",
                o_que=f"{pct}% das conversas terminaram numa mensagem nossa SEM pergunta",
                evidencia="a última coisa que dissemos não pedia nada",
                acao="regra do próximo passo: toda mensagem termina pedindo ou prometendo com prazo",
            ))
    return out


def rodar(*, dias: int = 30) -> Auditoria:
    """A vasculhada completa, nas 4 camadas."""
    from datetime import timedelta, timezone
    desde = datetime.now(timezone.utc) - timedelta(days=dias)
    a = Auditoria()
    with SessionLocal() as s:
        for nome, fn in (
            ("no_show", _camada_no_show),
            ("compareceu", _camada_compareceu),
            ("entraram", _camada_entraram),
            ("conversaram", _camada_conversaram),
        ):
            try:
                a.achados.extend(fn(s, desde))
            except Exception as e:  # noqa: BLE001 — camada que falha não derruba o resto
                log.warning("auditoria_camada_falhou", camada=nome, erro=str(e)[:140])
    log.info("auditoria_funil", dias=dias, achados=len(a.achados),
             altas=sum(1 for x in a.achados if x.gravidade == "alta"))
    return a


_ROTULO = {
    "no_show": "❌ QUEM NÃO COMPARECEU",
    "compareceu": "✅ QUEM COMPARECEU",
    "entraram": "📥 QUEM ENTROU NO MÊS",
    "conversaram": "💬 QUEM CONVERSOU NO MÊS",
}


def como_texto(a: Auditoria, max_por_camada: int = 5) -> str:
    altas = [x for x in a.achados if x.gravidade == "alta"]
    if not a.achados:
        return "🔍 *Vasculhada semanal do funil*\n\nNada fora de lugar. (Desconfie: eu também erro.)"
    linhas = [f"🔍 *Vasculhada semanal do funil*",
              f"{len(a.achados)} achados · {len(altas)} pedem ação hoje\n"]
    for camada in ("no_show", "compareceu", "entraram", "conversaram"):
        itens = a.por_camada(camada)
        if not itens:
            continue
        itens.sort(key=lambda x: {"alta": 0, "media": 1, "baixa": 2}[x.gravidade])
        linhas.append(f"*{_ROTULO[camada]}* ({len(itens)})")
        for x in itens[:max_por_camada]:
            marca = {"alta": "🔴", "media": "🟠", "baixa": "⚪"}[x.gravidade]
            linhas.append(f"{marca} *{x.quem}* — {x.o_que}")
            linhas.append(f"   {x.evidencia}")
            linhas.append(f"   → {x.acao}")
        if len(itens) > max_por_camada:
            linhas.append(f"   _… e mais {len(itens) - max_por_camada}_")
        linhas.append("")
    return "\n".join(linhas)
