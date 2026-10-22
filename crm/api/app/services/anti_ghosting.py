"""Anti-ghosting — reabre a conversa de quem estava falando e parou no meio.

A DESCOBERTA QUE DESTRAVA ISSO: não precisa de template.

A régua de follow-up fala com quem NUNCA respondeu — janela fechada, tudo por
template, tudo pago e sujeito a aprovação da Meta. O anti-ghosting é o oposto:
fala com quem ESTAVA conversando e sumiu no meio, com a janela de 24h ainda
aberta. Texto livre, de graça, sem passar pela Meta.

É a automação mais barata que a casa pode ter.

CINCO TOQUES (Dona, 15/09/2026 — eram dois):
    1h30 → "👀"                        custo zero de responder; reabre sem cobrar
    3h   → "correria aí? me avisa"     dá ao lead uma DESCULPA pronta pra voltar
    6h   → retoma o assunto            sem cobrar resposta
    10h  → última tentativa de engate  direta, ainda no mesmo dia
    15h  → saída digna                 encerra e deixa a porta aberta

Por que PARA em cinco e não vai a dez: **a janela de 24h fecha**. Depois dela
não passa texto livre — só template pago, que é outra automação, com outro
custo e outra aprovação da Meta. Os cinco cabem dentro das 20h de margem; o
sexto já seria fora. Quem sumiu de vez não recebe mais toque: volta pra roda do
carrossel e recebe template daqui a 30 dias, que é o caminho certo pra isso.

O quinto toque não é insistência — é o contrário. Ele ENCERRA. Lead que levou
quatro mensagens sem responder não vai responder à quinta cobrança; o que a
quinta faz é não deixar a conversa morrer em silêncio constrangido, e dar à
pessoa uma saída sem precisar dizer "para de me mandar mensagem".

QUEM ENTRA (todas as condições, sem exceção):
  - a última mensagem da conversa é DO LEAD (se a corretora respondeu depois,
    não há ghosting nenhum);
  - a corretora já falou alguma vez naquela conversa — senão não é "parou no
    meio", é lead novo, e quem cuida disso é a boas-vindas;
  - o silêncio está dentro da faixa do toque E dentro da janela de 24h com
    margem;
  - horário civilizado (👀 às 23h vira bloqueio, não conversa);
  - não está em opt-out, não disse "já encontrei", telefone não está marcado
    como inválido.

DEDUP: `anti_ghosting_toques` com UNIQUE em (telefone, desde, toque). O `desde`
é o timestamp da mensagem que originou o silêncio — é o que faz cada parada ser
um evento próprio. Se a pessoa responder e sumir de novo depois, é uma parada
nova e o toque pode sair de novo. Sem isso, o tick mandaria "👀" a cada rodada
enquanto ela não respondesse, que é a diferença entre reabrir uma conversa e
perseguir alguém.

DESLIGADO POR PADRÃO (`anti_ghosting_ativo`, config do painel). Ligar é decisão
da Dona, não efeito colateral de deploy.
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

# ── os dois toques ───────────────────────────────────────────────────────────
#
# `min_h`/`max_h` são a faixa de silêncio que qualifica pro toque. A faixa tem
# TETO de propósito: conversa parada há 8h não recebe mais o "👀" — àquela
# altura o gesto virou estranho, e o lead entra na régua normal.


@dataclass(frozen=True)
class Toque:
    numero: int
    min_h: float
    max_h: float
    texto: str


TOQUES: tuple[Toque, ...] = (
    Toque(1, 1.5, 3.0, "👀"),
    Toque(2, 3.0, 6.0, "correria aí? me avisa quando puder que eu retomo 🙂"),
    Toque(3, 6.0, 10.0, "continuo por aqui — quando puder retomar, é só me chamar"),
    Toque(4, 10.0, 15.0, "consigo te ajudar rapidinho, é só me dizer o que você procura"),
    # O último fecha a conversa de propósito. Ver o bloco CINCO TOQUES acima.
    Toque(5, 15.0, 20.0,
          "vou parar por aqui pra não incomodar 🙂 se mudar de ideia, "
          "é só responder que eu retomo de onde paramos"),
)

# Margem da janela de 24h. A janela conta da última mensagem DO LEAD — que é
# exatamente o nosso `desde`. Parar em 20h dá 4h de folga pra atraso de tick,
# relógio fora de sincronia e fila cheia. Texto livre fora da janela é erro na
# Meta e mancha a linha; a folga é barata.
JANELA_MAX_H = 20.0

# Horário civilizado, em São Paulo.
HORA_INICIO = 9
HORA_FIM = 19


def _agora_sp() -> datetime:
    return datetime.now(SP)


def dentro_do_horario(agora: datetime | None = None) -> bool:
    """Entre 9h e 19h de São Paulo, dias úteis inclusive sábado.

    Domingo fica de fora: a conversa que parou no sábado à tarde espera segunda.
    Reabrir no domingo rende resposta pior e incomoda mais.
    """
    a = (agora or _agora_sp()).astimezone(SP)
    if a.weekday() == 6:  # domingo
        return False
    return HORA_INICIO <= a.hour < HORA_FIM


# Conversas paradas: a última mensagem é do lead, a corretora já falou antes, e
# o silêncio está na faixa. Uma linha por (instance, telefone).
#
# `ultimas` pega a última mensagem de cada conversa; `participou` garante que a
# corretora falou alguma vez ali. O filtro de tempo fica no Python pra a mesma
# consulta servir aos dois toques.
_SQL_PARADAS = """
WITH ultimas AS (
    SELECT DISTINCT ON (m.instance, m.contato_telefone)
           m.instance,
           m.contato_telefone AS telefone,
           m.contato_nome     AS nome,
           m.corretora_id,
           m.from_corretora,
           m.timestamp_evolution AS ultima_em
      FROM mensagens_whatsapp m
     WHERE m.tenant_id = :tenant
       AND m.contato_telefone IS NOT NULL
       AND m.timestamp_evolution > now() - interval '30 hours'
     ORDER BY m.instance, m.contato_telefone, m.timestamp_evolution DESC
),
participou AS (
    SELECT DISTINCT m.instance, m.contato_telefone AS telefone
      FROM mensagens_whatsapp m
     WHERE m.tenant_id = :tenant
       AND m.from_corretora IS TRUE
       AND m.timestamp_evolution > now() - interval '30 days'
)
SELECT u.instance, u.telefone, u.nome, u.corretora_id, u.ultima_em
  FROM ultimas u
  JOIN participou p
    ON p.instance = u.instance AND p.telefone = u.telefone
 WHERE u.from_corretora IS FALSE
"""

# Já mandamos esse toque pra essa parada?
_SQL_JA_MANDOU = """
SELECT 1 FROM anti_ghosting_toques
 WHERE tenant_id = :tenant AND telefone = :tel
   AND desde = :desde AND toque = :toque
 LIMIT 1
"""

_SQL_GRAVA = """
INSERT INTO anti_ghosting_toques
       (tenant_id, telefone, corretora_id, toque, desde, texto)
VALUES (:tenant, :tel, :corr, :toque, :desde, :texto)
ON CONFLICT ON CONSTRAINT uq_anti_ghosting_toque DO NOTHING
RETURNING id
"""

# Bloqueios. Todos consultam o dado que já existe — nenhuma tabela nova.
#
# `opt_out` e "já encontrei" moram no estado da cadência; telefone morto tem
# tabela própria. O casamento é pelos últimos 8 dígitos porque DDI, DDD e nono
# dígito variam entre as fontes (mesma heurística do resto do sistema).
_SQL_BLOQUEADO = """
SELECT 1
  FROM cadencia_estado c
 WHERE c.tenant_id = :tenant
   AND right(regexp_replace(c.telefone, '\\D', '', 'g'), 8) = :suf
   AND c.status IN ('opt_out', 'ja_encontrou')
 LIMIT 1
"""

# É LEAD MESMO? A trava que quase faltou.
#
# A linha da corretora é o WhatsApp de trabalho dela, mas tem a vida dela junto:
# na primeira medição em produção, 3 dos 5 candidatos eram contato pessoal
# ("Marcelinho", "|_Jotapê_|") — gente que ela conversou e não respondeu, como
# qualquer pessoa faz. Sem esta trava, a automação manda "👀" pro amigo da
# corretora pelo número de trabalho dela.
#
# Fail-closed: quem não está no CRM não recebe. O anti-ghosting existe pra lead.
_SQL_E_LEAD = """
SELECT 1 FROM leads
 WHERE tenant_id = :tenant
   AND right(regexp_replace(coalesce(telefone, ''), '\\D', '', 'g'), 8) = :suf
 LIMIT 1
"""

# Equipe da casa nunca entra: corretora, dona e gerente conversam entre si pelas
# mesmas linhas, e "👀" automático pra colega é constrangimento, não automação.
_SQL_E_INTERNO = """
SELECT 1 FROM internos_acme
 WHERE tenant_id = :tenant
   AND right(regexp_replace(coalesce(telefone_normalizado, ''), '\\D', '', 'g'), 8) = :suf
 LIMIT 1
"""

# Telefone morto é COLUNA em `leads` (não tabela própria) — é onde
# telefone_invalido.marcar_se_morto grava.
_SQL_TELEFONE_MORTO = """
SELECT 1 FROM leads
 WHERE tenant_id = :tenant
   AND telefone_invalido IS TRUE
   AND right(regexp_replace(coalesce(telefone, ''), '\\D', '', 'g'), 8) = :suf
 LIMIT 1
"""


@dataclass
class Candidato:
    telefone: str
    nome: str | None
    instance: str
    corretora_id: int | None
    desde: datetime
    toque: Toque

    @property
    def horas_parado(self) -> float:
        return (datetime.now(timezone.utc) - self.desde).total_seconds() / 3600.0


def _suf8(telefone: str | None) -> str:
    d = "".join(c for c in (telefone or "") if c.isdigit())
    return d[-8:] if len(d) >= 8 else ""


def _bloqueado(session, telefone: str, tenant: int = 1) -> str | None:
    """Motivo do bloqueio, ou None se está liberado. Falha FECHADO: erro de
    consulta bloqueia o envio — perder um toque é barato, mandar pra quem pediu
    pra sair não é."""
    suf = _suf8(telefone)
    if not suf:
        return "telefone_invalido"
    try:
        p = {"tenant": tenant, "suf": suf}
        # A ordem é a do dano: contato pessoal primeiro (é o mais provável e o
        # mais constrangedor), depois equipe, depois as travas de lead.
        if not session.execute(sql_text(_SQL_E_LEAD), p).first():
            return "nao_e_lead"
        if session.execute(sql_text(_SQL_E_INTERNO), p).first():
            return "equipe_da_casa"
        if session.execute(sql_text(_SQL_BLOQUEADO), p).first():
            return "opt_out_ou_ja_encontrou"
        if session.execute(sql_text(_SQL_TELEFONE_MORTO), p).first():
            return "telefone_morto"
    except Exception as e:  # noqa: BLE001
        log.warning("anti_ghosting_bloqueio_indisponivel", erro=str(e)[:140])
        return "checagem_falhou"
    return None


def selecionar(*, tenant: int = 1, agora: datetime | None = None) -> list[Candidato]:
    """Conversas paradas que qualificam pra um toque agora.

    Uma conversa entra no MAIOR toque que couber e ainda não foi enviado — se o
    tick atrasou e a parada já está em 4h, ela recebe o toque 2 direto em vez de
    receber um "👀" atrasado e depois o segundo em seguida.
    """
    ref = (agora or datetime.now(timezone.utc)).astimezone(timezone.utc)
    out: list[Candidato] = []
    with SessionLocal() as s:
        linhas = s.execute(sql_text(_SQL_PARADAS), {"tenant": tenant}).mappings().all()
        for r in linhas:
            desde = r["ultima_em"]
            if desde.tzinfo is None:
                desde = desde.replace(tzinfo=timezone.utc)
            horas = (ref - desde).total_seconds() / 3600.0
            if horas >= JANELA_MAX_H:
                continue  # janela de 24h — texto livre não vale mais
            cabe = [t for t in TOQUES if t.min_h <= horas < t.max_h]
            if not cabe:
                continue
            toque = max(cabe, key=lambda t: t.numero)
            tel = r["telefone"]
            ja = s.execute(
                sql_text(_SQL_JA_MANDOU),
                {"tenant": tenant, "tel": tel, "desde": desde, "toque": toque.numero},
            ).first()
            if ja:
                continue
            motivo = _bloqueado(s, tel, tenant)
            if motivo:
                log.info("anti_ghosting_bloqueado", tel=tel[-4:], motivo=motivo)
                continue
            out.append(
                Candidato(
                    telefone=tel,
                    nome=r["nome"],
                    instance=r["instance"],
                    corretora_id=r["corretora_id"],
                    desde=desde,
                    toque=toque,
                )
            )
    return out


def rodar(*, tenant: int = 1, limite: int = 40, dry_run: bool | None = None) -> dict:
    """Um tick do anti-ghosting. Devolve o que fez, sem levantar exceção.

    A ordem das travas importa: interruptor → horário → seleção → envio. Checar
    horário antes de consultar evita varrer o banco de madrugada à toa.
    """
    resultado: dict = {"enviados": 0, "candidatos": 0, "pulados": [], "dry_run": bool(dry_run)}

    if not cfg.anti_ghosting_ativo():
        resultado["pulados"].append("desligado")
        return resultado
    if not dentro_do_horario():
        resultado["pulados"].append("fora_do_horario")
        return resultado

    candidatos = selecionar(tenant=tenant)
    resultado["candidatos"] = len(candidatos)

    from app.api.cloud_corretora_a import enviar_cloud
    from app.services import cloud_lines

    with SessionLocal() as s:
        for c in candidatos[:limite]:
            linha = cloud_lines.linha_da_corretora(c.corretora_id, session=s)
            # None = corretora sem linha Cloud (a Pessoa11, ainda no Evolution).
            # INDETERMINADO = o cadastro não pôde ser lido. Nos DOIS casos não
            # manda: sem linha resolvida o texto sairia pela Pessoa12 por padrão, e o
            # lead responderia pra uma pessoa com quem nunca falou.
            if linha is None or linha is cloud_lines.INDETERMINADO:
                log.info("anti_ghosting_sem_linha", tel=c.telefone[-4:],
                         corretora=c.corretora_id,
                         motivo="indeterminado" if linha else "sem_linha")
                continue
            if dry_run:
                log.info("anti_ghosting_dry_run", tel=c.telefone[-4:],
                         toque=c.toque.numero, texto=c.toque.texto,
                         horas=round(c.horas_parado, 1))
                resultado["enviados"] += 1
                continue
            # Grava ANTES de enviar: se o envio estourar depois de a Meta ter
            # aceitado, o pior caso é um toque perdido. O contrário — enviar e
            # não gravar — repete a mensagem no próximo tick.
            gravou = s.execute(
                sql_text(_SQL_GRAVA),
                {"tenant": tenant, "tel": c.telefone, "corr": c.corretora_id,
                 "toque": c.toque.numero, "desde": c.desde, "texto": c.toque.texto},
            ).first()
            s.commit()
            if not gravou:
                continue  # outro worker pegou essa parada primeiro
            try:
                enviar_cloud(
                    c.telefone,
                    c.toque.texto,
                    linha=linha,
                    corretora_id=c.corretora_id,
                    instance=c.instance,
                    origem="anti_ghosting",
                )
                resultado["enviados"] += 1
                log.info("anti_ghosting_enviado", tel=c.telefone[-4:],
                         toque=c.toque.numero, horas=round(c.horas_parado, 1))
            except Exception as e:  # noqa: BLE001
                log.warning("anti_ghosting_falhou", tel=c.telefone[-4:],
                            erro=str(e)[:160])
    return resultado
