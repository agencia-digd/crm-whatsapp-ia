"""No-show — recupera quem marcou visita e não apareceu.

Hoje não existe nada: a varredura de 04/08 achou visitas vencidas que ninguém
fechou, e duas foram descobertas por acaso numa conversa, não pelo sistema.

━━ A CORREÇÃO DO DESENHO ORIGINAL ━━

O desenho dizia: dispara em "visita com data_hora passada e status ainda
agendada/pendente_confirmacao". **Isso está errado e é perigoso.**

Visita vencida ainda "agendada" não quer dizer que a pessoa faltou — quer dizer
que NINGUÉM FECHOU O REGISTRO. Das 7 nessa situação hoje, não dá pra saber
quantas compareceram. Mandar "você não veio ontem, aconteceu algo?" pra quem
compareceu é o pior vexame que essa automação pode produzir, e queima a
corretora junto.

Então o gatilho é `status = 'no_show'` EXPLÍCITO — alguém marcou. Já são 77
casos no banco, o dado existe e é confiável. Visita vencida sem registro
continua sendo cobrança da corretora no painel, não palpite da automação.

━━ A RÉGUA ━━

    1º no-show → dia seguinte, tom leve, oferece encaixe
    2º no-show → dia seguinte, mesma linha, tom mais firme
    3º        → nada. Passou da cota: a conversa volta pra corretora, na mão.

A cota de 3 é do acervo comercial e existe pros dois lados: protege o lead de
virar perseguido e protege a agenda da corretora de quem não vai aparecer.

━━ POR QUE TEMPLATE, E NÃO TEXTO LIVRE ━━

A mensagem sai no DIA SEGUINTE — a janela de 24h já fechou. Fora dela a Meta
exige template. Usamos o esqueleto UTILITY que já está aprovado nas três linhas
({{1}}=nome, {{2}}=mensagem), com o texto do no-show na variável — que é
exatamente o padrão que a casa já usa pro handoff.

Nasce DESLIGADO (`no_show_ativo`).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import text as sql_text

from app.db.session import SessionLocal
from app.services import config_operacional as cfg

log = structlog.get_logger()

SP = ZoneInfo("America/Sao_Paulo")

# Cota do acervo: depois do 3º, para. Vira conversa de gente.
COTA_MAX = 3

# Faixa de disparo: no dia seguinte, entre 14h e 38h depois da visita. O piso
# de 14h evita mandar de madrugada pra visita do fim da tarde; o teto evita
# cobrar uma falta de três dias atrás como se fosse ontem.
MIN_H = 14.0
MAX_H = 38.0

HORA_INICIO = 9
HORA_FIM = 19


@dataclass(frozen=True)
class Copy:
    ordem: int
    texto: str
    cta: str


# O tom sobe UMA vez e para. "Nossa cota de não comparecimento é de 3" é do
# acervo e funciona porque é uma regra da casa, não uma bronca.
COPIES: tuple[Copy, ...] = (
    Copy(
        1,
        "vi aqui que a gente não conseguiu se encontrar ontem. Aconteceu alguma "
        "coisa? Se quiser, consigo te encaixar essa semana — me diz o melhor dia "
        "que eu organizo.",
        "remarcar",
    ),
    Copy(
        2,
        "a gente não conseguiu se encontrar de novo ontem. Sem problema, mas quero "
        "te perguntar direto: ainda faz sentido pra você conhecer o imóvel? Se "
        "fizer, eu remarco uma última vez e seguro o horário pra você.",
        "remarcar",
    ),
)


def dentro_do_horario(agora: datetime | None = None) -> bool:
    """9h-19h de São Paulo, sem domingo. Cobrança de falta às 22h é briga."""
    a = (agora or datetime.now(SP)).astimezone(SP)
    if a.weekday() == 6:
        return False
    return HORA_INICIO <= a.hour < HORA_FIM


# Faltas marcadas EXPLICITAMENTE, na faixa de tempo, com o número de faltas
# anteriores daquele lead (é o que decide a copy e a cota).
#
# `anteriores` conta por TELEFONE, não por lead_id: o mesmo comprador pode ter
# mais de um card no CRM, e a cota é da pessoa.
_SQL_FALTAS = """
WITH alvo AS (
    SELECT v.id, v.lead_id, v.lead_nome, v.telefone_lead, v.corretora_id,
           v.data_hora, v.bairro
      FROM visitas v
     WHERE v.tenant_id = :tenant
       AND v.status = 'no_show'
       AND v.telefone_lead IS NOT NULL
       AND v.data_hora < now() - (:min_h || ' hours')::interval
       AND v.data_hora > now() - (:max_h || ' hours')::interval
)
SELECT a.*,
       (SELECT count(*) FROM visitas o
         WHERE o.tenant_id = :tenant
           AND o.status = 'no_show'
           AND right(regexp_replace(coalesce(o.telefone_lead,''), '\\D', '', 'g'), 8)
             = right(regexp_replace(coalesce(a.telefone_lead,''), '\\D', '', 'g'), 8)
           AND o.data_hora <= a.data_hora) AS ordem
  FROM alvo a
"""

_SQL_JA_MANDOU = """
SELECT 1 FROM no_show_toques
 WHERE tenant_id = :tenant AND visita_id = :vid LIMIT 1
"""

_SQL_GRAVA = """
INSERT INTO no_show_toques (tenant_id, visita_id, telefone, corretora_id, ordem, texto)
VALUES (:tenant, :vid, :tel, :corr, :ordem, :texto)
ON CONFLICT ON CONSTRAINT uq_no_show_visita DO NOTHING
RETURNING id
"""

# Mesmas travas do resto da casa. Quem pediu pra sair não é cobrado por falta.
_SQL_BLOQUEADO = """
SELECT 1 FROM cadencia_estado
 WHERE tenant_id = :tenant
   AND right(regexp_replace(telefone, '\\D', '', 'g'), 8) = :suf
   AND status IN ('opt_out', 'ja_encontrou')
 LIMIT 1
"""

# A visita foi remarcada depois da falta? Então não há o que cobrar — a pessoa
# já voltou. Sem isso, a automação manda "aconteceu algo?" pra quem já reagendou.
_SQL_REMARCOU = """
SELECT 1 FROM visitas
 WHERE tenant_id = :tenant
   AND right(regexp_replace(coalesce(telefone_lead,''), '\\D', '', 'g'), 8) = :suf
   AND data_hora > :depois
 LIMIT 1
"""


@dataclass
class Falta:
    visita_id: int
    lead_id: str | None
    nome: str | None
    telefone: str
    corretora_id: int | None
    quando: datetime
    ordem: int
    copy: Copy


def _suf8(t: str | None) -> str:
    d = "".join(c for c in (t or "") if c.isdigit())
    return d[-8:] if len(d) >= 8 else ""


def _primeiro_nome(nome: str | None) -> str:
    n = (nome or "").strip().split()
    return n[0] if n else "tudo bem"


def selecionar(*, tenant: int = 1) -> list[Falta]:
    """Faltas que merecem um toque agora."""
    out: list[Falta] = []
    with SessionLocal() as s:
        linhas = s.execute(
            sql_text(_SQL_FALTAS),
            {"tenant": tenant, "min_h": MIN_H, "max_h": MAX_H},
        ).mappings().all()
        for r in linhas:
            ordem = int(r["ordem"] or 1)
            if ordem > len(COPIES):
                log.info("no_show_cota_estourada", vid=r["id"], ordem=ordem)
                continue  # passou da cota: vira conversa de gente
            if s.execute(sql_text(_SQL_JA_MANDOU), {"tenant": tenant, "vid": r["id"]}).first():
                continue
            tel = r["telefone_lead"]
            suf = _suf8(tel)
            if not suf:
                continue
            try:
                if s.execute(sql_text(_SQL_BLOQUEADO), {"tenant": tenant, "suf": suf}).first():
                    continue
                if s.execute(
                    sql_text(_SQL_REMARCOU),
                    {"tenant": tenant, "suf": suf, "depois": r["data_hora"]},
                ).first():
                    log.info("no_show_ja_remarcou", vid=r["id"])
                    continue
            except Exception as e:  # noqa: BLE001 — falha FECHADO
                log.warning("no_show_checagem_falhou", erro=str(e)[:140])
                continue
            out.append(
                Falta(
                    visita_id=r["id"],
                    lead_id=r["lead_id"],
                    nome=r["lead_nome"],
                    telefone=tel,
                    corretora_id=r["corretora_id"],
                    quando=r["data_hora"],
                    ordem=ordem,
                    copy=COPIES[ordem - 1],
                )
            )
    return out


def rodar(*, tenant: int = 1, limite: int = 20, dry_run: bool = False) -> dict:
    """Um tick do no-show."""
    res: dict = {"enviados": 0, "candidatos": 0, "pulados": [], "dry_run": dry_run}

    if not cfg.no_show_ativo():
        res["pulados"].append("desligado")
        return res
    if not dentro_do_horario():
        res["pulados"].append("fora_do_horario")
        return res

    faltas = selecionar(tenant=tenant)
    res["candidatos"] = len(faltas)

    from app.api.cloud_corretora_a import enviar_template_linha
    from app.services import cloud_lines

    with SessionLocal() as s:
        for f in faltas[:limite]:
            linha = cloud_lines.linha_da_corretora(f.corretora_id, session=s)
            if linha is None or linha is cloud_lines.INDETERMINADO:
                log.info("no_show_sem_linha", vid=f.visita_id, corretora=f.corretora_id)
                continue
            if dry_run:
                log.info("no_show_dry_run", vid=f.visita_id, ordem=f.ordem,
                         nome=f.nome, texto=f.copy.texto[:80])
                res["enviados"] += 1
                continue
            gravou = s.execute(
                sql_text(_SQL_GRAVA),
                {"tenant": tenant, "vid": f.visita_id, "tel": f.telefone,
                 "corr": f.corretora_id, "ordem": f.ordem, "texto": f.copy.texto},
            ).first()
            s.commit()
            if not gravou:
                continue
            try:
                enviar_template_linha(
                    linha,
                    f.telefone,
                    _primeiro_nome(f.nome),
                    hook=f.copy.texto,
                    cta=f.copy.cta,
                    corretora_id=f.corretora_id,
                )
                res["enviados"] += 1
                log.info("no_show_enviado", vid=f.visita_id, ordem=f.ordem,
                         tel=f.telefone[-4:])
            except Exception as e:  # noqa: BLE001
                log.warning("no_show_falhou", vid=f.visita_id, erro=str(e)[:160])
    return res
