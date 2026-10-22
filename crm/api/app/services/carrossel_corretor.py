"""
Carrossel da Corretor — o motor de remarketing em massa do 0000-0000.

Desenho da Dona (26/07): **Fila → Disparado → volta pra Fila**, auto-regulado.
Não é uma campanha com início e fim: é uma roda. O lead sai da fila, recebe,
descansa ~30 dias e volta pro fim da fila. Quem responde sai da roda.

    ┌──────────────────────────────────────────────┐
    │  Fila (23) ──dispara──► Disparado (24)       │
    │     ▲                        │               │
    │     └────── ~30 dias ────────┘               │
    │     ▲                        └─responde─► Respondeu (25)
    │     └────── ~45 dias, se ninguém atendeu ─────┘
    └──────────────────────────────────────────────┘

Quanto sai por dia é a RAMPA (`app/services/rampa.py`), não um número fixo:
20/dia enquanto o número aquece, até 60, e só sobe com qualidade GREEN na Meta.
O 0000 é o telefone DA CASA — se ele cai, a Acme perde o principal.

De ONDE, de QUEM e POR ONDE — configuração, não constante (05/10/2026):
  - a etapa de origem e o dono dos leads vêm de `remarketing_alvo`
    (`remarketing_etapa` / `remarketing_dono`; padrão 23 e 4, como sempre foi);
  - a linha é a que está marcada `disparo` no cadastro de números
    (`linha_disparadora`). Sem linha marcada o tick NÃO envia e diz o motivo.
O lead volta pra etapa DE ONDE SAIU (gravada no evento do disparo), não pra
etapa configurada hoje — trocar a etapa na tela não pode despejar quem está
descansando num funil que não é o dele.

⚠️ TRAVAS (as mesmas do resto do sistema, de propósito — não reimplementar):
  - trava de tick (`trava_tick.NS_CARROSSEL`): dois ticks não mandam 2×
  - cap do dia pela rampa, contando só o que SAIU
  - opt-out: quem pediu pra parar nunca entra
  - só template UTILITY com o prefixo da linha (`acme_corretor_`)
  - `settings.carrossel_corretor_ativo` nasce FALSE — nada roda sem a Dona ligar

NÃO envia direto: usa o mesmo `_default_sender` da cadência, que já está atrás
do DRY-RUN global e da trava de categoria.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Any, Callable

import structlog
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from app.config import settings
from app.services import config_operacional
from app.services import opt_out as _opt_out
from app.services import remarketing_alvo
from app.services.remarketing_alvo import Alvo
from app.services.trava_tick import (
    INDISPONIVEL,
    NS_CARROSSEL,
    OCUPADO,
    soltar,
    travar,
)

log = structlog.get_logger()

# A Corretor como PERSONA: dona histórica da roda, corretora do thread, destino do
# opt-out. Handoff, Pessoa e ligação usam isto pra saber "é lead do robô?".
# NÃO é mais o filtro do disparo — esse vem de `remarketing_alvo.ler()`.
CORRETORA_CORRETOR = remarketing_alvo.DONO_PADRAO
# A linha da Corretor como IDENTIDADE (inbound, atendimento da Pessoa). O disparo não
# sai mais "pela corretor" por constante: sai pela linha marcada `disparo`.
SLUG_CORRETOR = "corretor"
# instância da linha da Corretor — o que sai daqui é robô; o resto é gente
INSTANCE_CORRETOR = "acme_corretor"

# Etapas do funil remarketing (migration 0046 + criadas 26/07). A Fila deixou
# de ser constante: 23 é só o PADRÃO, e o destino de quem não tem a etapa de
# origem gravada no evento (todo disparo anterior a 05/10/2026).
STAGE_FILA_PADRAO = remarketing_alvo.ETAPA_FILA_PADRAO
STAGE_DISPARADO = remarketing_alvo.ETAPA_DISPARADO
STAGE_RESPONDEU = remarketing_alvo.ETAPA_RESPONDEU

# Quanto tempo o lead "descansa" em Disparado antes de voltar pra Fila.
DESCANSO_DIAS = 30

# Quanto tempo quem RESPONDEU descansa antes de voltar pra Fila.
#
# Até 15/09 esse número não existia: `_SQL_VOLTAR` só olhava Disparado, então
# Respondeu (25) era um BURACO NEGRO — 113 de 226 leads que responderam ao
# carrossel não foram atendidos por ninguém E nunca mais receberam disparo.
# Invisíveis dos dois lados. Foram limpos à mão em 15/09; sem esta volta a
# etapa encheria de novo em duas semanas.
#
# Por que 45 e não 30: responder é sinal de gente do outro lado. Bater de novo
# no mesmo intervalo de quem só recebeu e ficou mudo soa como "esqueceram que
# eu falei com vocês" — é o caminho curto pro bloqueio, e o 0000 é o telefone
# da casa. 45 = 1,5× o descanso do silencioso: no máximo 8 toques/ano contra 12,
# e ainda dentro do ciclo de decisão de quem procura imóvel (quem diz "no
# momento não" costuma revisitar em 2-3 meses).
# Não é 120 como o "já disse não" do `_SQL_FILA` de propósito: aqui a pessoa
# não recusou nada — ela respondeu e ninguém deu sequência. A recusa explícita
# continua travada pelos 120 dias, que valem por cima deste retorno.
DESCANSO_RESPONDEU_DIAS = 45

EVENT_DISPARO = "carrossel_disparo"
# Reserva: gravada NA MESMA transação do claim (lead sai da etapa de origem),
# ANTES de a mensagem sair. Vira `carrossel_disparo` quando o envio é
# confirmado e some quando nada saiu. Uma reserva que SOBRA quer dizer: a
# mensagem pode ter saído e o registro falhou — e ela conta no teto do dia.
#
# É um tipo SEPARADO de propósito. Gravar `carrossel_disparo` antes de enviar
# faria o placar dizer "enviado" sem ter enviado; não gravar nada antes (como
# era) fazia o teto do dia vazar um a cada falha de registro.
EVENT_RESERVA = "carrossel_reserva"
# O que conta no teto e no descanso: o que saiu + o que pode ter saído.
EVENTOS_DE_SAIDA = (EVENT_DISPARO, EVENT_RESERVA)
# (subiu pra cá em 15/09: o `_SQL_VOLTAR_RESPONDEU` precisa dele lá em cima)
EVENT_RESPOSTA = "carrossel_resposta"
# Regra gravada quando a IA trocou a `indefinida` (`ia_do_lado.REGRA_IA`).
REGRA_IA_OPINIAO = "ia_opiniao"
# Resposta AUTOMÁTICA do WhatsApp do lead (05/10/2026). Tipo separado: não é
# resposta, não tira o lead de Disparado e não entra em conta nenhuma que leia
# `carrossel_resposta`. Só existe pra a tela poder dizer quantas foram.
EVENT_AUTO_RESPOSTA = "carrossel_auto_resposta"

# Ritmo. O timer da cadência roda a cada 3 min entre 9h e 18h de SP (~180 ticks).
# A cota NÃO é linear: 60 dias de resposta mostraram almoço (12-14) e fim do
# dia (17h) ~50% melhores que manhã/tarde. A curva libera o volume aí.
# MAX_POR_TICK é o cinto: mesmo que a cota abra de repente, não sai lote.
HORA_INICIO = 9
HORA_FIM = 18
MAX_POR_TICK = 3

TZ_SP = ZoneInfo("America/Sao_Paulo")

# Fração do cap já liberada neste instante (Dona 06/09).
# 9h → 0 · 12h → 10% (pingo de manhã) · 14h → 60% (almoço) ·
# 17h → 75% · 18h → 100% (o resto no fim do dia).
_COTA_NOS: tuple[tuple[int, int, float], ...] = (
    (9, 0, 0.00),
    (12, 0, 0.10),
    (14, 0, 0.60),
    (17, 0, 0.75),
    (18, 0, 1.00),
)


def _cota_ate_agora(
    cap: int,
    agora: datetime | None = None,
    janela: tuple[int, int] | None = None,
) -> int:
    """Quanto do cap do dia já pode ter saído a esta hora.

    Curva concentrada no almoço e às 17h. Antes das 9h: zero. Das 18h em
    diante: o cap inteiro (o timer que não deixa mandar de noite).

    `janela` (minutos do dia, SP) é a janela de horário gravada no painel
    (`remarketing_janela_horario`). Com ela a MESMA curva é esticada ou
    encolhida pra caber entre o início e o fim: sem isso, uma janela 10:00-16:00
    fecharia com a curva em ~70% e 30% do cap do dia nunca sairia — a Dona
    configuraria 80 e veria 56 sem ninguém saber dizer por quê. Com a janela
    09:00-18:00 os nós caem exatamente onde já estavam.
    """
    agora = (agora or datetime.now(timezone.utc)).astimezone(TZ_SP)
    minutos = agora.hour * 60 + agora.minute
    nos = [(h * 60 + m, frac) for h, m, frac in _COTA_NOS]
    if janela is not None:
        ini, fim = janela
        t0, t1 = nos[0][0], nos[-1][0]
        nos = [(ini + (t - t0) * (fim - ini) / (t1 - t0), frac) for t, frac in nos]
    if minutos <= nos[0][0]:
        return 0
    if minutos >= nos[-1][0]:
        return cap
    for i in range(1, len(nos)):
        t0, f0 = nos[i - 1]
        t1, f1 = nos[i]
        if minutos <= t1:
            t = (minutos - t0) / (t1 - t0)
            frac = f0 + t * (f1 - f0)
            n = round(cap * frac)
            return max(1, n) if frac > 0 else 0
    return cap


# De quanto em quanto tempo o timer acorda o tick (`copiloto-cadencia-tick.timer`,
# `OnCalendar=*-*-* 12..21:00/3:00 UTC`). Mudou o timer, muda aqui.
TIMER_INTERVALO_MIN = 3


def maximo_que_cabe_no_dia(cap: int, janela: tuple[int, int] | None = None) -> int:
    """Quanto dá pra enviar num dia inteiro com este teto, no MELHOR caso.

    O teto do dia não é o único limite: saem no máximo `MAX_POR_TICK` por
    volta, uma volta a cada 3 minutos, só dentro do horário, e a cota libera
    aos poucos. Com janela curta o teto não cabe — 300 por dia numa janela de
    duas horas são 40 voltas de 3: saem 120, e a tela dizia 300.

    Simula as voltas do dia com a MESMA curva do tick. Melhor caso: fila cheia,
    nenhuma falha, nenhuma volta perdida. O real é isto ou menos.
    """
    from app.services import remarketing_agenda as agenda

    if cap <= 0:
        return 0
    ini, fim = janela if janela is not None else (agenda.TIMER_INICIO, agenda.TIMER_FIM)
    ini, fim = max(ini, agenda.TIMER_INICIO), min(fim, agenda.TIMER_FIM)
    base = datetime(2026, 1, 5, tzinfo=TZ_SP)      # o dia não importa, só a hora
    enviados = 0
    for minuto in range(agenda.TIMER_INICIO, agenda.TIMER_FIM, TIMER_INTERVALO_MIN):
        if not (ini <= minuto < fim):
            continue
        agora = base.replace(hour=minuto // 60, minute=minuto % 60)
        cota = (_cota_ate_agora(cap, agora) if janela is None
                else _cota_ate_agora(cap, agora, janela=janela))
        enviados += max(0, min(min(cap, cota) - enviados, MAX_POR_TICK))
    return enviados


@dataclass
class CarrosselResult:
    cap_do_dia: int = 0
    ja_enviados_hoje: int = 0
    cota_ate_agora: int = 0
    # Só preenchidos quando há cap semanal gravado no painel.
    cap_da_semana: int | None = None
    ja_enviados_semana: int | None = None
    disponiveis: int = 0
    enviados: int = 0
    falhas: int = 0
    voltaram_pra_fila: int = 0
    # separado de propósito: é a métrica que diz se a etapa Respondeu voltou a
    # esvaziar sozinha. Somado no `voltaram_pra_fila` viraria número cego.
    voltaram_de_respondeu: int = 0
    # Saiu (ou pode ter saído) e o registro do disparo falhou. Conta em
    # `falhas` também; separado porque estes CONSOMEM o teto do dia.
    sem_registro: int = 0
    # A mensagem PODE ter saído e não há confirmação (timeout depois do POST,
    # retorno fora do contrato). Conta em `falhas`; a reserva fica, segura o
    # teto e NÃO há nova tentativa — quem fecha é o conferente.
    incertos: int = 0
    bloqueios: dict[str, int] = field(default_factory=dict)
    motivo_parada: str | None = None
    # O que o registro durável de cada volta guarda (`remarketing_tick_log`).
    # `ligado` None = não consegui ler o interruptor.
    ligado: bool | None = None
    linha: str | None = None
    etapa: int | None = None


def _bloq(res: CarrosselResult, motivo: str) -> None:
    res.bloqueios[motivo] = res.bloqueios.get(motivo, 0) + 1


def _so_digitos(t: str | None) -> str:
    return "".join(c for c in (t or "") if c.isdigit())


# ── Volta pra fila: quem descansou o bastante ────────────────────────────────

# O lead volta pra etapa DE ONDE SAIU e só se ainda for de quem era — as duas
# coisas estão gravadas no último evento de saída dele (`etapa_origem`, `dono`).
# Evento antigo, sem os campos, cai no padrão (Fila 23, Corretor): é exatamente o
# que o SQL de antes fazia com as constantes.
#
# A reserva conta como saída aqui também: lead cujo registro falhou depois do
# envio descansa os mesmos 30 dias. Antes ele ficava preso em Disparado pra
# sempre (sem evento) ou voltava cedo demais (contando do disparo anterior).
_SQL_ULTIMA_SAIDA = """
    SELECT DISTINCT ON (le.lead_id)
           le.lead_id,
           le.at,
           COALESCE(NULLIF(le.payload->>'etapa_origem', '')::bigint, :fila_padrao) AS etapa,
           COALESCE(NULLIF(le.payload->>'dono', '')::bigint, :dono_padrao)         AS dono
      FROM lead_events le
     WHERE le.tipo IN (:ev_disp, :ev_reserva)
     ORDER BY le.lead_id, le.at DESC
"""

_SQL_VOLTAR = """
    UPDATE leads l
       SET stage_id = u.etapa, updated_at = now()
      FROM (""" + _SQL_ULTIMA_SAIDA + """) u
     WHERE l.id = u.lead_id
       AND l.tenant_id = :t
       AND l.stage_id = :disparado
       AND l.owner_id = u.dono
       AND u.at < now() - (:dias || ' days')::interval
       -- a etapa de origem ainda existe e é DESTE cliente
       AND EXISTS (SELECT 1 FROM pipeline_stages ps
                    WHERE ps.id = u.etapa AND ps.tenant_id = :t)
    RETURNING l.id
"""


def _params_ultima_saida() -> dict[str, Any]:
    return {"fila_padrao": STAGE_FILA_PADRAO, "dono_padrao": CORRETORA_CORRETOR,
            "ev_disp": EVENT_DISPARO, "ev_reserva": EVENT_RESERVA}


def _voltar_pra_fila(session: Session, tenant_id: int) -> int:
    """Quem está em Disparado há mais de DESCANSO_DIAS volta pra etapa de onde
    saiu (o fim da fila dela)."""
    try:
        n = len(session.execute(
            sql_text(_SQL_VOLTAR),
            {"t": tenant_id, "disparado": STAGE_DISPARADO, "dias": DESCANSO_DIAS,
             **_params_ultima_saida()},
        ).fetchall())
        session.commit()
        return n
    except Exception as e:  # noqa: BLE001
        log.warning("carrossel_voltar_falhou", erro=str(e)[:140])
        session.rollback()
        return 0


# ── Volta pra fila: quem RESPONDEU e não engatou em nada ─────────────────────
#
# A outra metade da roda, que faltava (ver DESCANSO_RESPONDEU_DIAS). Volta SÓ
# quem respondeu e ficou no vácuo. Os quatro filtros abaixo são o que separa
# "ninguém deu sequência" de "já está sendo cuidado" — sem eles a roda passaria
# por cima de atendimento humano em andamento, que é o pecado capital deste
# motor (Dona 31/07: robô não fala por cima de gente).
_SQL_VOLTAR_RESPONDEU = r"""
    UPDATE leads
       SET stage_id = v.etapa, updated_at = now()
      FROM (
            SELECT m.id, m.etapa FROM (
                -- Marco do descanso: quando o lead respondeu. Cai no disparo
                -- que provocou a resposta e, em último caso, no updated_at —
                -- lead movido à mão pra cá não tem evento nenhum e ficaria
                -- preso pra sempre por falta de data.
                SELECT l.id, l.telefone,
                       COALESCE(
                         (SELECT max(le.at) FROM lead_events le
                           WHERE le.lead_id = l.id AND le.tipo = :ev_resp),
                         (SELECT max(le.at) FROM lead_events le
                           WHERE le.lead_id = l.id AND le.tipo = :ev_disp),
                         l.updated_at
                       ) AS marco,
                       -- Volta pra etapa de onde saiu, e só se ainda for de
                       -- quem era (mesma regra do `_SQL_VOLTAR`). Sem evento
                       -- com os campos: Fila padrão e Corretor, como sempre foi.
                       COALESCE(u.etapa, :fila_padrao) AS etapa
                  FROM leads l
                  LEFT JOIN (""" + _SQL_ULTIMA_SAIDA + r""") u ON u.lead_id = l.id
                 WHERE l.tenant_id = :t
                   AND l.owner_id = COALESCE(u.dono, :dono_padrao)
                   AND l.stage_id = :respondeu
                   AND l.telefone IS NOT NULL
            ) m
             WHERE m.marco IS NOT NULL
               AND m.marco < now() - (:dias || ' days')::interval
               -- 1. ATENDIMENTO HUMANO depois da resposta. Qualquer linha que
               -- não seja a da Corretor, sem carimbo de robô e sem ser template:
               -- é corretora falando. Quem já está em conversa NÃO volta pra
               -- roda — receberia template no meio de um atendimento vivo.
               -- (A lista de origens é gêmea da do `_SQL_FILA`: origem de robô
               -- lida como gente faz a automação se auto-bloquear.)
               AND NOT EXISTS (
                    SELECT 1 FROM mensagens_whatsapp w
                     WHERE w.from_corretora
                       AND w.instance <> :instance_corretor
                       AND w.contato_telefone IS NOT NULL
                       AND w.tipo_mensagem <> 'template'
                       AND w.conteudo_metadata->>'template' IS NULL
                       AND COALESCE(w.conteudo_metadata->>'origem', '') NOT IN (
                             'pessoa', 'anti_ghosting', 'pos_ligacao', 'carrossel'
                       )
                       AND w.timestamp_evolution > m.marco
                       AND right(regexp_replace(w.contato_telefone, '\D', '', 'g'), 8)
                         = right(regexp_replace(m.telefone, '\D', '', 'g'), 8)
               )
               -- 2. OPT-OUT, as duas fontes. O `_SQL_FILA` já barra na saída,
               -- mas devolver um opt-out pra Fila sujaria a contagem da fila e
               -- faria a etapa parecer maior do que é.
               AND NOT EXISTS (
                    SELECT 1 FROM disparos_reengajamento d
                     WHERE d.opt_out IS TRUE
                       AND right(regexp_replace(d.telefone, '\D', '', 'g'), 8)
                         = right(regexp_replace(m.telefone, '\D', '', 'g'), 8)
               )
               AND NOT EXISTS (
                    SELECT 1 FROM cadencia_estado ce
                     WHERE ce.status = 'opt_out'
                       AND right(regexp_replace(ce.telefone, '\D', '', 'g'), 8)
                         = right(regexp_replace(m.telefone, '\D', '', 'g'), 8)
               )
               -- 3. JÁ FOI TRIADO COMO NÃO. A triagem de 15/09 carimba
               -- `lead_events.tipo='triagem_respondeu'` com a classe. Quem foi
               -- 'NAO'/'LIXO'/'OPTOUT' já saiu da etapa — mas se a mudança de
               -- etapa falhar (ou alguém arrastar o card de volta), o veredito
               -- da triagem continua valendo e o lead não entra na roda.
               AND NOT EXISTS (
                    SELECT 1 FROM lead_events t
                     WHERE t.lead_id = m.id
                       AND t.tipo = 'triagem_respondeu'
                       AND t.payload->>'classe' IN ('NAO', 'LIXO', 'OPTOUT', 'NAO_POR_AUDIO')
               )
               AND EXISTS (SELECT 1 FROM pipeline_stages ps
                            WHERE ps.id = m.etapa AND ps.tenant_id = :t)
       ) v
     WHERE leads.id = v.id
       AND leads.tenant_id = :t
       AND leads.stage_id = :respondeu
    RETURNING leads.id
"""
# 4. Transferido pra corretora não volta: o `owner_id = dono do disparo` lá em
# cima já é esse filtro. Handoff (simples ou rico) troca o dono do lead —
# deixou de ser de quem recebeu o disparo, deixou de ser da roda.


def _voltar_respondeu_pra_fila(session: Session, tenant_id: int) -> int:
    """Quem respondeu, não foi atendido por ninguém e já descansou volta pra Fila."""
    try:
        n = len(session.execute(
            sql_text(_SQL_VOLTAR_RESPONDEU),
            {"t": tenant_id,
             "respondeu": STAGE_RESPONDEU, "instance_corretor": INSTANCE_CORRETOR,
             "ev_resp": EVENT_RESPOSTA,
             "dias": DESCANSO_RESPONDEU_DIAS, **_params_ultima_saida()},
        ).fetchall())
        session.commit()
        if n:
            log.info("carrossel_respondeu_voltou_pra_fila", n=n)
        return n
    except Exception as e:  # noqa: BLE001
        log.warning("carrossel_voltar_respondeu_falhou", erro=str(e)[:140])
        try:
            session.rollback()
        except Exception:  # noqa: BLE001
            pass
        return 0


# ── Quem sai da fila hoje ────────────────────────────────────────────────────
# Ordem: quem está na fila há mais tempo primeiro (FIFO por updated_at).
# Exclui opt-out por sufixo-8 — mesma fonte que a cadência e o disparo usam.

_SQL_FILA = r"""
    SELECT l.id, l.nome, l.telefone
      FROM leads l
     WHERE l.tenant_id = :t
       -- DONO: só lead de quem está configurado como dono do disparo. É a
       -- trava que tira da roda o lead transferido pra corretora (o handoff
       -- troca o dono). A etapa virou escolha; isto aqui não virou "qualquer um".
       AND l.owner_id = :dono
       AND l.stage_id = :fila
       -- A etapa precisa ser DESTE cliente. A gravação pela tela e a leitura
       -- do tick já conferem; esta linha é a que vale se as duas falharem.
       AND EXISTS (SELECT 1 FROM pipeline_stages ps
                    WHERE ps.id = l.stage_id AND ps.tenant_id = :t)
       AND l.telefone IS NOT NULL
       -- Número que já falhou na entrega e nunca deu sinal de vida: não existe
       -- no WhatsApp. Sem esta linha ele voltava pra fila a cada ~30 dias e
       -- falhava de novo, pra sempre, acumulando sinal ruim no 0000 — que é o
       -- telefone da casa. Ver app/services/telefone_invalido.py.
       AND l.telefone_invalido IS NOT TRUE
       -- OPT-OUT pelo predicado CANÔNICO (`opt_out.clausula`): disparo legado,
       -- cadência e a etapa "Não perturbe" — a terceira fonte faltava aqui
       -- (05/10/2026). Sem ela, quem tinha OUTRO card em Não perturbe entrava
       -- no lote, era barrado só no envio, voltava pra fila e gastava um
       -- lugar do lote a cada volta, pra sempre.
       AND NOT (""" + _opt_out.clausula("l.telefone") + r""")
       -- O número da própria equipe está na base (o da Pessoa11 chegou a receber
       -- disparo em 30/07 e a Pessoa começou a qualificá-la como lead). Além do
       -- ridículo, queima cota e polui o teste de copy.
       AND NOT EXISTS (
            SELECT 1 FROM corretoras c
             WHERE c.telefone IS NOT NULL
               AND right(regexp_replace(c.telefone, '\D', '', 'g'), 8)
                 = right(regexp_replace(l.telefone, '\D', '', 'g'), 8)
       )
       -- `corretoras` cobre só quem atende lead. A CASA é maior: gerente, dona,
       -- assistente, o número da própria Assistente — tudo em `internos_acme`. Em
       -- 10/08 a Corretor mandou remarketing de imóvel pra Pessoa5 e pro 2955.
       AND NOT EXISTS (
            SELECT 1 FROM internos_acme i
             WHERE i.telefone_normalizado IS NOT NULL
               AND right(regexp_replace(i.telefone_normalizado, '\D', '', 'g'), 8)
                 = right(regexp_replace(l.telefone, '\D', '', 'g'), 8)
       )
       -- JÁ DISSE NÃO. Descoberto em 11/08: 15 de 15 pessoas que recusaram
       -- continuaram na fila, porque "no momento não" não é opt-out formal e
       -- ninguém marcava nada. Um deles disse "já comprei, obrigado" e recebeu
       -- mais dois; outro disse "não mais" e recebeu mais seis.
       --
       -- Aqui a recusa vale por 120 dias — não é opt-out (a pessoa não pediu
       -- pra sair), é respeitar o "agora não" antes de voltar a insistir.
       AND NOT EXISTS (
            SELECT 1 FROM mensagens_whatsapp m
             WHERE m.from_corretora IS FALSE
               AND m.contato_telefone IS NOT NULL
               AND m.timestamp_evolution > now() - interval '120 days'
               AND right(regexp_replace(m.contato_telefone, '\D', '', 'g'), 8)
                 = right(regexp_replace(l.telefone, '\D', '', 'g'), 8)
               AND (
                    m.conteudo_texto ILIKE '%não quero%'   OR m.conteudo_texto ILIKE '%nao quero%'
                 OR m.conteudo_texto ILIKE '%não estou%'   OR m.conteudo_texto ILIKE '%nao estou%'
                 OR m.conteudo_texto ILIKE '%no momento não%' OR m.conteudo_texto ILIKE '%no momento nao%'
                 OR m.conteudo_texto ILIKE '%não mais%'    OR m.conteudo_texto ILIKE '%nao mais%'
                 OR m.conteudo_texto ILIKE '%já comprei%'  OR m.conteudo_texto ILIKE '%ja comprei%'
                 OR m.conteudo_texto ILIKE '%já encontrei%' OR m.conteudo_texto ILIKE '%ja encontrei%'
                 OR m.conteudo_texto ILIKE '%sem interesse%'
                 OR m.conteudo_texto ILIKE '%não tenho interesse%' OR m.conteudo_texto ILIKE '%nao tenho interesse%'
               )
       )
       -- Conversa humana EM ANDAMENTO em outra linha: robô não fala por cima de
       -- gente (Dona 31/07). Janela curta de propósito — 39% da base já falou
       -- com humano alguma vez, e reengajar lead frio é justamente o trabalho
       -- do carrossel. O que trava é o "estou falando com essa pessoa AGORA".
       AND NOT EXISTS (
            SELECT 1 FROM mensagens_whatsapp m
             WHERE m.from_corretora
               AND m.contato_telefone IS NOT NULL
               -- saída SEM carimbo de robô = gente falando (inclui a própria
               -- linha da Corretor, que é compartilhada com humano).
               --
               -- ⚠️ A lista precisa ter TODA origem automática, não só 'pessoa'.
               -- Achado em 15/09 (auditoria gpt-6-astra) no mesmo dia em que o
               -- anti-ghosting foi ligado: ele carimba `origem='anti_ghosting'`,
               -- que passava no `<> 'pessoa'` e era lido como GENTE FALANDO. O
               -- efeito: o follow-up do próprio robô tirava o lead do carrossel
               -- pela janela de pausa — a automação se auto-bloqueava, e isso
               -- não aparece em log nenhum (o lead só some da fila, e a queda
               -- de volume parece cansaço de base).
               AND COALESCE(m.conteudo_metadata->>'origem', '') NOT IN (
                     'pessoa', 'anti_ghosting', 'pos_ligacao', 'carrossel'
               )
               AND m.conteudo_metadata->>'template' IS NULL
               AND m.tipo_mensagem <> 'template'
               AND m.timestamp_evolution > now() - make_interval(days => :pausa_dias)
               AND right(regexp_replace(m.contato_telefone, '\D', '', 'g'), 8)
                 = right(regexp_replace(l.telefone, '\D', '', 'g'), 8)
       )
     ORDER BY l.updated_at NULLS FIRST, l.id
     LIMIT :lim
"""


def tick(
    *,
    session: Session | None = None,
    sender: Callable[..., dict[str, Any]] | None = None,
    tenant_id: int = 1,
    limite_manual: int | None = None,
    ignorar_ritmo: bool = False,
    max_por_tick: int | None = None,
    cap_override: int | None = None,
) -> CarrosselResult:
    """Uma volta do carrossel. Best-effort: nunca levanta.

    Toda volta — ligada ou não, enviando ou não — deixa uma linha em
    `remarketing_tick_log`. É o que permite ao conferente dizer "rodou ligado
    o dia inteiro e não mandou nada" e "não rodou", sem depender de log.
    """
    res = CarrosselResult()
    try:
        _tick(res, session=session, sender=sender, tenant_id=tenant_id,
              limite_manual=limite_manual, ignorar_ritmo=ignorar_ritmo,
              max_por_tick=max_por_tick, cap_override=cap_override)
    finally:
        _registrar_tick(tenant_id, res, manual=ignorar_ritmo or cap_override is not None)
    return res


def _registrar_tick(tenant_id: int, res: CarrosselResult, *, manual: bool = False) -> None:
    """Grava o que esta volta fez. Sessão própria e best-effort: falhar aqui
    não pode desfazer nem impedir nada do disparo — mas a falha tem nome no
    log, e a AUSÊNCIA de linhas é exatamente o que o conferente alarma."""
    import json

    try:
        from app.db.session import SessionLocal

        with SessionLocal() as s:
            s.execute(
                sql_text(
                    """
                    INSERT INTO remarketing_tick_log
                        (tenant_id, ligado, motivo_parada, cap_do_dia, cota,
                         ja_hoje, disponiveis, enviados, falhas, incertos, detalhe)
                    VALUES (:t, :ligado, :motivo, :cap, :cota, :ja, :disp,
                            :env, :falhas, :incertos, CAST(:det AS jsonb))
                    """
                ),
                {"t": tenant_id, "ligado": res.ligado, "motivo": res.motivo_parada,
                 "cap": res.cap_do_dia, "cota": res.cota_ate_agora,
                 "ja": res.ja_enviados_hoje, "disp": res.disponiveis,
                 "env": res.enviados, "falhas": res.falhas,
                 "incertos": res.incertos,
                 "det": json.dumps({
                     "linha": res.linha, "etapa": res.etapa,
                     "sem_registro": res.sem_registro,
                     "bloqueios": res.bloqueios, "manual": manual,
                     "voltaram": res.voltaram_pra_fila,
                     "voltaram_de_respondeu": res.voltaram_de_respondeu,
                 })},
            )
            s.commit()
    except Exception as e:  # noqa: BLE001
        log.error("carrossel_tick_log_falhou", erro=str(e)[:140])


def _autorizacao_final(session: Session, tenant_id: int, tel: str,
                       alvo: Alvo, linha: Any) -> tuple[str | None, bool]:
    """A última conferência, feita antes de CADA mensagem. Devolve
    `(motivo, para_o_lote)`; motivo None = pode enviar.

    O começo da volta já leu tudo isto — mas uma volta manda até 3 mensagens e
    cada uma pode levar dezenas de segundos (a rotação de templates tenta um
    por um). Sem esta releitura, desligar o disparo, pausar o número ou baixar
    o teto DEPOIS do começo da volta não segurava as mensagens que faltavam.

    Relê do banco e do cadastro de números, sem cache:
      interruptor · dia e horário · teto do dia e da semana gravados no
      painel · etapa e dono · linha disparadora · pesos e TEXTO do teste A/B ·
      opt-out do telefone.

    Qualquer coisa que não dê pra ler SEGURA. O opt-out barra só este lead; o
    resto para o lote inteiro (não é do lead, é da operação).
    """
    from app.services import linha_disparadora
    from app.services import remarketing_agenda as agenda

    try:
        config_operacional.recarregar_estrito()
        if not config_operacional.carrossel_corretor_ativo():
            return "desligado_no_meio_do_lote", True
        if not agenda.dentro_do_horario_do_robo():
            return "fora_do_horario_do_robo", True
        bloqueio = agenda.bloqueio_de_agenda()
        if bloqueio:
            return bloqueio, True
        try:
            cap_painel = agenda.cap_diario()
            cap_semana = agenda.cap_semanal()
        except agenda.ConfigIlegivel:
            return "agenda_ilegivel", True
        if cap_painel is not None and _enviados_hoje(session, tenant_id) >= cap_painel:
            # (a própria reserva deste lead ainda não existe: a conta é do
            # que saiu ANTES dele.)
            return "cap_do_dia_atingido", True
        if cap_semana is not None and agenda.enviados_na_semana(
            session, tenant_id, EVENTOS_DE_SAIDA
        ) >= cap_semana:
            return "cap_semanal_atingido", True
        try:
            agora = remarketing_alvo.ler(session, tenant_id)
        except remarketing_alvo.AlvoInvalido as e:
            return e.motivo, True
        if agora != alvo:
            return "alvo_mudou_no_meio_do_lote", True
        resolucao = linha_disparadora.resolver()
        if resolucao.linha is None:
            return resolucao.motivo or "sem_linha_disparadora", True
        if getattr(resolucao.linha, "phone_id", None) != getattr(linha, "phone_id", None):
            return "linha_mudou_no_meio_do_lote", True
        # Pesos do teste A/B gravados por fora da tela e que não fecham (soma
        # diferente de 100, variante que não existe): não dá pra saber qual
        # copy a pessoa quis — segura, em vez de sortear em cima do que sobrar.
        from app.services import carrossel_copies as cc

        problema = cc.problema_nos_pesos()
        if problema:
            log.error("carrossel_pesos_invalidos_segurando", erro=problema[:160])
            return "pesos_invalidos", True
        # Texto das variantes (05/10): o que a tela gravou é o que sai. Texto
        # ilegível, com caractere que a Meta recusa ou de variante que não
        # existe SEGURA — nunca cai calado no texto do código, que é o que
        # faria a Dona achar que a copy nova está no ar.
        problema = cc.problema_nas_copies()
        if problema:
            log.error("carrossel_copy_invalida_segurando", erro=problema[:200])
            return "copy_invalida", True
        if _opt_out.esta_bloqueado(session, tel):
            return "opt_out", False
        return None, False
    except config_operacional.ConfigIndisponivel as e:
        log.error("carrossel_config_indisponivel_nao_enviou", erro=str(e)[:140])
        return "config_indisponivel", True
    except Exception as e:  # noqa: BLE001
        # Não consegui conferir ⇒ não envio. (Diferente do opt-out de dentro
        # do sender, que falha aberto pra não parar os outros motores.)
        log.error("carrossel_autorizacao_falhou_nao_enviou", erro=str(e)[:140])
        try:
            session.rollback()
        except Exception:  # noqa: BLE001
            pass
        return "autorizacao_indisponivel", True


def _tick(
    res: CarrosselResult,
    *,
    session: Session | None,
    sender: Callable[..., dict[str, Any]] | None,
    tenant_id: int,
    limite_manual: int | None,
    ignorar_ritmo: bool,
    max_por_tick: int | None,
    cap_override: int | None,
) -> None:
    # Interruptor: SÓ o banco, lido agora (sem cache). Leitura que falha não é
    # "desligado" nem "ligado": é não saber — e sem saber não envia.
    try:
        config_operacional.recarregar_estrito()
        res.ligado = config_operacional.carrossel_corretor_ativo()
    except config_operacional.ConfigIndisponivel as e:
        res.motivo_parada = "config_indisponivel"
        log.error("carrossel_config_indisponivel_nao_enviou", erro=str(e)[:140])
        return
    if not res.ligado:
        res.motivo_parada = "desligado"
        log.info("carrossel_corretor_desligado")
        return

    from app.services.cadencia import _default_sender

    sender = sender or _default_sender
    owns = session is None
    if owns:
        from app.db.session import SessionLocal

        session = SessionLocal()

    trava = travar(NS_CARROSSEL, tenant_id)
    if trava is OCUPADO:
        res.motivo_parada = "tick_concorrente"
        log.info("carrossel_tick_concorrente_pulou")
        if owns:
            session.close()
        return
    if trava is INDISPONIVEL:
        # Este motor manda em MASSA. Sem trava, dois ticks leem a mesma fila e
        # o cap do dia sai dobrado — a Meta lê rajada como spam e derruba a
        # linha. Aqui não vale o fail-open dos outros motores: melhor perder
        # 3 minutos de disparo (o próximo tick pega) que dobrar o volume.
        res.motivo_parada = "trava_indisponivel"
        log.error("carrossel_trava_indisponivel_nao_disparou")
        if owns:
            session.close()
        return

    try:
        # 1. quem descansou volta pra fila — os dois lados da roda
        res.voltaram_pra_fila = _voltar_pra_fila(session, tenant_id)
        res.voltaram_de_respondeu = _voltar_respondeu_pra_fila(session, tenant_id)

        # 2. AGENDA do painel: dia da semana e janela de horário (05/10).
        #
        # Vem DEPOIS do passo 1 de propósito: devolver quem descansou pra fila
        # é arrumação, não envio, e precisa continuar acontecendo no sábado —
        # senão a segunda abre com a fila do tamanho de sexta.
        # Vem ANTES da rampa: fora da janela nem vale gastar a consulta de
        # qualidade na Graph.
        #
        # Vale também pro lote manual (`carrossel_burst.py`). Até 05/10 o
        # `ignorar_ritmo` pulava a agenda: era uma segunda autoridade, por
        # terminal, passando por cima do que a tela dizia. Agora o lote manual
        # só ignora a CURVA do dia e o máximo por volta — dia, horário e tetos
        # do painel valem pra todo mundo.
        from app.services import remarketing_agenda as agenda

        bloqueio = agenda.bloqueio_de_agenda()
        if bloqueio:
            res.motivo_parada = bloqueio
            log.info("carrossel_segurado_pela_agenda", motivo=bloqueio)
            return
        janela_painel = agenda.janela()

        # 3. DE ONDE e DE QUEM (05/10): etapa e dono vêm da configuração.
        # Valor que não serve (etapa de outro cliente, etapa de dentro da roda,
        # dono inexistente) SEGURA — não cai no padrão, que seria disparar pra
        # uma lista que ninguém escolheu.
        try:
            alvo = remarketing_alvo.ler(session, tenant_id)
        except remarketing_alvo.AlvoInvalido as e:
            res.motivo_parada = e.motivo
            log.error("carrossel_alvo_invalido_segurando",
                      motivo=e.motivo, erro=str(e)[:160])
            return
        res.etapa = alvo.etapa

        # 4. POR ONDE (05/10): a linha marcada `disparo` no cadastro de números.
        # Sem linha resolvida não envia — e NUNCA cai de volta na "linha de
        # sempre". Vem antes da rampa porque a rampa olha a qualidade DESTA
        # linha, e antes de tocar na fila porque sem linha não há o que reservar.
        from app.services import linha_disparadora

        resolucao = linha_disparadora.resolver()
        if resolucao.linha is None:
            res.motivo_parada = resolucao.motivo or "sem_linha_disparadora"
            log.warning("carrossel_sem_linha_disparadora_nao_enviou",
                        motivo=res.motivo_parada, detalhe=resolucao.detalhe)
            return
        linha = resolucao.linha
        res.linha = linha.slug

        # 5. quanto pode sair hoje — a RAMPA manda
        from app.services.rampa import cap_de_hoje

        try:
            cap_painel = agenda.cap_diario()
            cap_semana = agenda.cap_semanal()
        except agenda.ConfigIlegivel as e:
            res.motivo_parada = "agenda_ilegivel"
            log.error("carrossel_cap_ilegivel_segurando", erro=str(e)[:160])
            return
        # O limite por dia do painel é o PEDIDO. Até o primeiro degrau da
        # rampa ele só baixa; acima dele a rampa só acompanha com a linha
        # GREEN na Meta (`rampa.cap_de_hoje`). Vale sempre o menor dos
        # dois — número digitado nunca fura o freio de qualidade.
        r = cap_de_hoje(session, linha.slug, alvo.dono, pedido=cap_painel)
        cap = r.cap if cap_painel is None else min(r.cap, cap_painel)
        if cap_override is not None and r.cap > 0:
            # Lote pontual autorizado (Dona 06/09, furar o freio AMARELO). Só
            # troca o teto da rampa quando a rampa deixa enviar: qualidade
            # parada (RED/FLAGGED) ou ilegível continua zero, e o teto gravado
            # no painel continua mandando por cima.
            cap = int(cap_override) if cap_painel is None else min(int(cap_override), cap_painel)
        if cap_painel is not None and cap < cap_painel:
            # Era aqui que o sistema mentia calado: 200 gravado, 80
            # enviado, nenhum log. Agora fica escrito a cada tick.
            log.info("carrossel_pedido_acima_da_rampa", pedido=cap_painel,
                     efetivo=cap, motivo=r.motivo)
        res.cap_do_dia = cap
        if cap <= 0:
            # Zero pode ter vindo da rampa (qualidade ruim) ou do painel (a
            # Dona gravou 0). O motivo precisa dizer qual — "qualidade RED" num
            # dia em que foi o campo da tela mandaria alguém abrir a Meta à toa.
            res.motivo_parada = r.motivo if r.cap <= 0 else "cap_do_dia_zerado_no_painel"
            log.warning("carrossel_cap_zero", motivo=res.motivo_parada)
            return

        res.ja_enviados_hoje = _enviados_hoje(session, tenant_id)

        # O cap é do DIA, mas o timer roda a cada 3 min: sem ritmo, o primeiro
        # tick da manhã cospe os 20 de uma vez. Um número frio disparando em
        # rajada é exatamente o padrão que a Meta lê como spam.
        # `ignorar_ritmo` é só pra lote autorizado (ex.: domingo 06/09, 200
        # até 14h): ignora a curva, não o teto.
        if ignorar_ritmo:
            cota = cap
        else:
            # Sem janela gravada a chamada é a de sempre, argumento por
            # argumento — o caminho padrão não muda nem na assinatura.
            cota = (_cota_ate_agora(cap) if janela_painel is None
                    else _cota_ate_agora(cap, janela=janela_painel))
        res.cota_ate_agora = cota
        restante = max(0, min(cap, cota) - res.ja_enviados_hoje)
        restante = min(restante, max_por_tick if max_por_tick is not None else MAX_POR_TICK)
        if limite_manual is not None:
            restante = min(restante, limite_manual)

        # Cap SEMANAL do painel: soma com o diário, o que estourar primeiro
        # trava. Conta a semana de São Paulo, segunda a domingo. O saldo também
        # corta o lote deste tick — sem isso, faltando 1 pra bater o teto,
        # sairiam os 3 do MAX_POR_TICK e a semana fecharia 2 acima.
        if cap_semana is not None:
            res.ja_enviados_semana = agenda.enviados_na_semana(
                session, tenant_id, EVENTOS_DE_SAIDA
            )
            res.cap_da_semana = cap_semana
            saldo_semana = cap_semana - res.ja_enviados_semana
            if saldo_semana <= 0:
                res.motivo_parada = "cap_semanal_atingido"
                log.info("carrossel_cap_semanal_atingido",
                         cap=cap_semana, semana=res.ja_enviados_semana)
                return
            restante = min(restante, saldo_semana)

        if restante <= 0:
            res.motivo_parada = (
                "cap_do_dia_atingido" if res.ja_enviados_hoje >= cap else "no_ritmo"
            )
            return

        # 6. tira da fila e dispara
        fila = session.execute(
            sql_text(_SQL_FILA),
            {"t": tenant_id, "dono": alvo.dono, "fila": alvo.etapa, "lim": restante,
             "pausa_dias": settings.carrossel_pausa_conversa_humana_dias},
        ).mappings().all()
        res.disponiveis = len(fila)
        if not fila:
            res.motivo_parada = "fila_vazia"

        for lead in fila:
            tel = _so_digitos(lead["telefone"])
            if len(tel) < 10:
                _bloq(res, "telefone_invalido")
                continue
            # AUTORIZAÇÃO FINAL, por mensagem: o painel é relido agora. Antes
            # da reserva de propósito — lead barrado aqui não sai da fila nem
            # gasta o teto.
            motivo, para_o_lote = _autorizacao_final(session, tenant_id, tel, alvo, linha)
            if motivo:
                _bloq(res, motivo)
                if para_o_lote:
                    res.motivo_parada = motivo
                    log.warning("carrossel_lote_interrompido", motivo=motivo,
                                enviados_antes=res.enviados)
                    break
                continue
            # CLAIM antes do envio: tira o lead da Fila e COMMITA, só então
            # manda. Antes era o contrário — a mensagem saía e o commit vinha
            # depois, então morrer no meio (restart, deploy, OOM) deixava a
            # pessoa "recebida mas ainda na Fila", e o próximo tick mandava de
            # novo. O `AND stage_id = :fila` também torna a corrida inofensiva:
            # se dois ticks lerem a mesma fila, só um consegue reservar.
            if not _reservar(session, lead["id"], tenant_id, alvo, linha.slug):
                _bloq(res, "reservado_por_outro_tick")
                continue
            try:
                _disparar(session, lead, tel, sender, tenant_id, linha, alvo,
                          cap_do_dia=cap)
                session.commit()
                # Só conta como enviado o que ficou REGISTRADO. Antes o
                # contador subia antes do commit: um commit que falhasse
                # aparecia, na mesma volta, como enviado E como sem registro.
                res.enviados += 1
            except _NadaSaiu as e:  # noqa: PERF203
                # Falhou ANTES de a Meta receber: devolver pra fila é seguro e
                # o lead tenta de novo no próximo tick. A reserva some junto —
                # nada saiu, nada conta no teto.
                session.rollback()
                _devolver_pra_fila(session, lead["id"], tenant_id, alvo)
                res.falhas += 1
                causa = str(e.__cause__ or e)
                log.warning("carrossel_envio_falhou", lead_id=lead["id"], erro=causa[:140])
                # falha de linha (ban/limite) para o lote: não insiste
                if any(k in causa.lower() for k in ("limit", "banned", "restricted", "131049")):
                    res.motivo_parada = "linha_bloqueada"
                    break
            except _Incerto as e:
                # NÃO SEI se saiu (timeout depois do pedido, resposta fora do
                # contrato). Devolver pra fila mandaria de novo na próxima
                # volta — é assim que o lead recebe duas vezes. O lead fica em
                # Disparado, a reserva fica (segura o teto e os 30 dias) e sai
                # marcada `incerto`, pro conferente fechar com o registro da
                # mensagem. O lote PARA: timeout costuma ser rede ou linha
                # caindo, e insistir multiplica a dúvida.
                session.rollback()
                res.falhas += 1
                res.incertos += 1
                _anotar_reserva(session, lead["id"], tenant_id,
                                {"estado": "incerto", "motivo": str(e)[:200]})
                res.motivo_parada = "envio_incerto"
                log.error("carrossel_envio_incerto_reserva_fica",
                          lead_id=lead["id"], erro=str(e)[:140])
                break
            except Exception as e:  # noqa: BLE001
                # Falhou DEPOIS do envio (gravar o evento). A mensagem pode ter
                # saído — devolver pra fila arriscaria mandar duas vezes. Fica
                # em Disparado, e a RESERVA gravada no claim fica também: é ela
                # que segura o teto do dia (`_enviados_hoje` conta) e o descanso
                # de 30 dias. O placar de copy não conta — ele só conta o que
                # tem `carrossel_disparo`, e este não tem.
                session.rollback()
                res.falhas += 1
                res.sem_registro += 1
                # O que se sabe do envio (wamid, template) vai pra reserva: é
                # com isso que o conferente transforma a reserva em disparo.
                _anotar_reserva(session, lead["id"], tenant_id,
                                {"estado": "sem_registro", "motivo": str(e)[:200],
                                 **getattr(e, "conhecido", {})})
                log.error("carrossel_pos_envio_falhou_lead_fica_disparado",
                          lead_id=lead["id"], erro=str(e)[:140],
                          reserva_conta_no_teto=True)
    except config_operacional.ConfigIndisponivel as e:
        res.motivo_parada = "config_indisponivel"
        log.error("carrossel_config_indisponivel_nao_enviou", erro=str(e)[:140])
    except Exception as e:  # noqa: BLE001
        log.exception("carrossel_tick_erro", erro=str(e)[:160])
        res.motivo_parada = "erro"
    finally:
        if owns:
            session.close()
        soltar(trava, NS_CARROSSEL, tenant_id)

    log.info("carrossel_tick_done", enviados=res.enviados, cap=res.cap_do_dia,
             voltaram=res.voltaram_pra_fila,
             voltaram_de_respondeu=res.voltaram_de_respondeu, falhas=res.falhas,
             incertos=res.incertos, parada=res.motivo_parada)


def _enviados_hoje(session: Session, tenant_id: int) -> int:
    """O que saiu do carrossel HOJE (dia local SP) — é o que o teto compara.

    Conta o disparo registrado E a reserva que sobrou (mensagem que pode ter
    saído sem o registro fechar). Contar só o registrado deixava o teto vazar
    um a cada falha de gravação.
    """
    try:
        return int(session.execute(
            sql_text(
                """
                SELECT count(*) FROM lead_events
                 WHERE tenant_id = :t AND tipo IN (:ev, :ev_reserva)
                   AND (at AT TIME ZONE 'America/Sao_Paulo')::date
                     = (now() AT TIME ZONE 'America/Sao_Paulo')::date
                """
            ),
            {"t": tenant_id, "ev": EVENT_DISPARO, "ev_reserva": EVENT_RESERVA},
        ).scalar() or 0)
    except Exception as e:  # noqa: BLE001
        # Não deu pra contar → assume cheio. Erro nunca libera volume.
        log.warning("carrossel_contagem_falhou_assumindo_cheio", erro=str(e)[:120])
        return 10**6


class _NadaSaiu(RuntimeError):
    """Falha ANTES de a mensagem chegar na Meta — seguro devolver pra fila.

    Herda de RuntimeError pra não quebrar quem já tratava a falha de envio
    como tal (ex.: o teste da linha ausente).


    Serve pra separar as duas falhas que exigem reação oposta: se nada saiu,
    devolver e tentar de novo; se já saiu, jamais devolver (mandaria duas
    vezes). Sem essa distinção, qualquer erro virava reenvio potencial.
    """


class _Incerto(RuntimeError):
    """NÃO SE SABE se a mensagem saiu: o pedido foi feito e a resposta não
    veio (timeout de leitura), veio ilegível, ou o remetente devolveu algo
    fora do contrato.

    Reação oposta à de `_NadaSaiu`: o lead NÃO volta pra fila (voltaria a
    receber na próxima volta) e a reserva fica, contando no teto. Até 05/10
    todo erro do remetente virava `_NadaSaiu` — um timeout com a mensagem já
    aceita pela Meta era reenviado três minutos depois.
    """


class _SemRegistro(RuntimeError):
    """A mensagem SAIU (a Meta aceitou) e gravar o disparo falhou. Carrega o
    que se sabe do envio pra ir pra reserva (`conhecido`)."""

    def __init__(self, msg: str, conhecido: dict[str, Any]) -> None:
        super().__init__(msg)
        self.conhecido = conhecido


def _nada_saiu_com_certeza(e: BaseException) -> bool:
    """A exceção do remetente PROVA que nada saiu?

    A lista de quem prova é FECHADA; o que não está nela é incerto:

    - não conseguiu nem conectar (`ConnectError`, `ConnectTimeout`, fila de
      conexões cheia): o pedido não chegou a sair daqui;
    - `RuntimeError` do emissor (`cloud_corretora_a.enviar_template_linha`): é o
      que ele levanta quando o portão de emissão está fechado, quando a linha
      não tem template elegível, quando falta token, ou quando a Meta
      RESPONDEU recusando (HTTP 4xx em todos os templates da rotação).

    Timeout depois de conectar, conexão que caiu no meio, corpo de resposta
    que não é JSON e qualquer erro que ninguém previu: não provam nada.
    """
    try:
        import httpx

        if isinstance(e, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)):
            return True
        if isinstance(e, httpx.HTTPError):
            return False
    except ImportError:  # pragma: no cover — httpx é dependência do projeto
        pass
    return isinstance(e, RuntimeError)


def _anotar_reserva(session: Session, lead_id: int, tenant_id: int,
                    extra: dict[str, Any]) -> None:
    """Escreve na reserva ABERTA do lead o que se sabe do envio que não
    fechou (`estado`, `motivo`, e o `wamid` quando existe). Best-effort."""
    import json

    try:
        session.execute(
            sql_text(
                "UPDATE lead_events SET payload = payload || CAST(:x AS jsonb) "
                " WHERE tenant_id = :t AND lead_id = :i AND tipo = :ev "
                "   AND at > now() - interval '1 day'"
            ),
            {"t": tenant_id, "i": lead_id, "ev": EVENT_RESERVA,
             "x": json.dumps(extra, ensure_ascii=False)},
        )
        session.commit()
    except Exception as e:  # noqa: BLE001
        session.rollback()
        log.warning("carrossel_anotar_reserva_falhou", lead_id=lead_id,
                    erro=str(e)[:120])


def _reservar(session: Session, lead_id: int, tenant_id: int,
              alvo: Alvo | None = None, linha_slug: str | None = None) -> bool:
    """Tira o lead da etapa de origem, grava a RESERVA e COMMITA — o claim que
    autoriza o envio.

    Condicional na etapa E no dono atuais: se outro tick reservou primeiro, ou
    o lead mudou de dono entre a leitura da fila e agora (handoff no meio do
    tick), o UPDATE não pega nada e devolvemos False. É o que impede dois ticks
    de disparar pro mesmo lead mesmo que a trava falhe.

    A reserva (`carrossel_reserva`) entra na MESMA transação: daqui pra frente
    este lead conta no teto do dia, saia o registro do disparo ou não. Ela
    carrega a etapa de origem e o dono — é o que o `_SQL_VOLTAR` usa pra
    devolver o lead pro lugar certo depois do descanso.
    """
    import json

    alvo = alvo or Alvo(STAGE_FILA_PADRAO, CORRETORA_CORRETOR)
    n = session.execute(
        sql_text(
            "UPDATE leads SET stage_id = :d, updated_at = now() "
            " WHERE id = :i AND tenant_id = :t AND stage_id = :fila "
            "   AND owner_id = :dono"
        ),
        {"d": STAGE_DISPARADO, "i": lead_id, "t": tenant_id,
         "fila": alvo.etapa, "dono": alvo.dono},
    ).rowcount
    if n:
        session.execute(
            sql_text(
                """
                INSERT INTO lead_events (tenant_id, lead_id, tipo, payload, at)
                VALUES (:t, :i, :ev, CAST(:p AS jsonb), now())
                """
            ),
            {"t": tenant_id, "i": lead_id, "ev": EVENT_RESERVA,
             "p": json.dumps({"origem": "carrossel", "linha": linha_slug,
                              "etapa_origem": alvo.etapa, "dono": alvo.dono})},
        )
    session.commit()
    return bool(n)


def _devolver_pra_fila(session: Session, lead_id: int, tenant_id: int,
                       alvo: Alvo | None = None) -> None:
    """Desfaz o claim quando o envio falhou sem nada sair: o lead volta pra
    etapa de onde saiu e a reserva some (nada saiu, nada conta). Best-effort."""
    alvo = alvo or Alvo(STAGE_FILA_PADRAO, CORRETORA_CORRETOR)
    try:
        session.execute(
            sql_text(
                "UPDATE leads SET stage_id = :f, updated_at = now() "
                " WHERE id = :i AND tenant_id = :t AND stage_id = :d"
            ),
            {"f": alvo.etapa, "i": lead_id, "t": tenant_id, "d": STAGE_DISPARADO},
        )
        _apagar_reserva(session, lead_id, tenant_id)
        session.commit()
    except Exception as e:  # noqa: BLE001
        # Não conseguiu desfazer: o lead fica em Disparado COM a reserva, que
        # segue contando no teto. Erro nunca libera volume.
        session.rollback()
        log.warning("carrossel_devolver_fila_falhou", lead_id=lead_id, erro=str(e)[:120])


def _apagar_reserva(session: Session, lead_id: int, tenant_id: int) -> None:
    """Apaga a reserva ABERTA do lead (a de hoje). Sem commit — quem chama
    fecha a transação junto com o que estiver fazendo."""
    session.execute(
        sql_text(
            "DELETE FROM lead_events "
            " WHERE tenant_id = :t AND lead_id = :i AND tipo = :ev "
            "   AND at > now() - interval '1 day'"
        ),
        {"t": tenant_id, "i": lead_id, "ev": EVENT_RESERVA},
    )


def _disparar(session: Session, lead, tel: str, sender, tenant_id: int,
              linha: Any = None, alvo: Alvo | None = None,
              cap_do_dia: int | None = None) -> None:
    """Manda pela linha disparadora e grava o evento. O lead JÁ foi reservado
    (`_reservar`) — aqui só sai a mensagem e se registra o que saiu.

    `linha` é a que o tick resolveu pela marca `disparo`. Sem ela nada sai:
    esta função não procura linha nenhuma por conta própria.

    Três desfechos, três exceções (ou nenhuma):

        nada saiu, com certeza   `_NadaSaiu`     o lead volta pra fila
        não sei se saiu          `_Incerto`      fica em Disparado, reserva fica
        saiu e não registrei     `_SemRegistro`  idem, com o wamid na reserva
        saiu e registrei         (retorna)       reserva vira `carrossel_disparo`
    """
    import json

    from app.services import carrossel_copies as cc
    from app.services.cadencia import _chamar_sender

    if linha is None:
        raise _NadaSaiu("sem linha disparadora — nada enviado")
    alvo = alvo or Alvo(STAGE_FILA_PADRAO, CORRETORA_CORRETOR)

    var = cc.escolher(lead["id"])
    # O TEXTO que sai é o do banco (o da tela), lido agora; o do código é só o
    # padrão. Se não der pra ler ou o gravado não servir, NADA sai — a
    # autorização final já barra antes, isto é o cinto com o suspensório pro
    # caso de a copy mudar entre as duas leituras.
    try:
        texto, copy_versao = cc.copy_para_envio(var)
    except (cc.CopyInvalida, config_operacional.ConfigIndisponivel) as e:
        raise _NadaSaiu(f"copy inválida, nada enviado: {str(e)[:160]}") from e
    try:
        r = _chamar_sender(
            sender, tel, texto, com_botao=False,
            linha=linha, corretora_id=CORRETORA_CORRETOR,
            permitir_marketing=var.marketing,
            # A cota de MARKETING é uma fração do teto do DIA DESTE MOTOR. Até
            # 05/10 ia o teto da cadência (`cadencia_cap_diario`): com o
            # remarketing em 300 e a cadência em 50, os "10% em marketing" da
            # tela viravam 5 mensagens, não 30.
            cap_diario_da_linha=(cap_do_dia if cap_do_dia is not None
                                 else config_operacional.cap_diario()),
            # Carimbo da automação: é o que liga a última checagem de opt-out
            # dentro do emissor e marca a mensagem como de robô no thread.
            origem="carrossel",
        )
    except Exception as e:
        if _nada_saiu_com_certeza(e):
            raise _NadaSaiu(str(e)[:200]) from e
        raise _Incerto(f"{type(e).__name__}: {str(e)[:160]}") from e

    # CONTRATO do remetente (`cloud_corretora_a.enviar_template_linha`):
    #   saiu de verdade → {"sent": True,  "dry_run": False, "wamid": <id da Meta>}
    #   dry-run / barrado → {"sent": False, ...}
    # Só `sent is True` é envio. `None`, `{}` e `{"ok": false}` passavam por
    # "enviado" até 05/10 e viravam disparo registrado com wamid nulo.
    if not isinstance(r, dict):
        raise _Incerto(f"o remetente devolveu {type(r).__name__}, não o contrato")

    # DRY-RUN NÃO É ENVIO. Sem esta checagem o lead ia pra "Disparado" e
    # ficava DESCANSO_DIAS sem nunca ter recebido nada — invisível, porque o
    # placar conta o evento.
    #
    # Aconteceu em 15/09/2026 com 3 leads (6272, 1271, 1272): um tick rodou com
    # DRY_RUN=true contra o banco de produção às 14:35. `_NadaSaiu` devolve o
    # lead pra Fila, que é a verdade: nada saiu.
    if r.get("dry_run") or r.get("sent") is False:
        raise _NadaSaiu(
            f"nada saiu (dry_run={r.get('dry_run')!r} sent={r.get('sent')!r}"
            + (f" erro={r.get('erro')!r}" if r.get("erro") else "") + ")"
        )
    if r.get("sent") is not True:
        raise _Incerto(
            f"retorno do remetente sem 'sent' verdadeiro: chaves={sorted(r)[:8]}"
        )

    # O que CONTA é o que saiu. Uma variante marcada `marketing` pode ter caído
    # em UTILITY (cota estourada, ou nenhum template MARKETING aprovado nessa
    # linha) — gravar a intenção faria o relatório comparar categoria que nunca
    # foi usada e concluir bobagem.
    categoria = r.get("categoria") or (
        "MARKETING" if var.marketing else "UTILITY"
    )
    if not var.marketing and categoria == "MARKETING":
        # Já saiu na Meta — não dá pra desfazer. Grava a verdade e grita,
        # pra a próxima rodada trocar de modelo (Dona 06/09: direta = UTILITY).
        log.error(
            "carrossel_utility_saiu_marketing",
            variante=var.id, template=r.get("template"),
        )
    # Id que a META devolveu. Quando ela aceita e não devolve id, o emissor
    # fabrica um pra gravar o thread (`wamid_fabricado`): esse NÃO vem pra cá —
    # nenhum status de entrega chegaria nele, e o placar o contaria "em voo"
    # pra sempre. Sem id de verdade o disparo fica sem rastro, e diz que está.
    wamid = None if r.get("wamid_fabricado") else (r.get("wamid") or None)
    payload = {
        "linha": linha.slug,
        "origem": "carrossel",
        # De onde o lead saiu e de quem era: é pra lá que ele volta depois do
        # descanso (`_SQL_VOLTAR`), não pra etapa configurada no dia da volta.
        "etapa_origem": alvo.etapa,
        "dono": alvo.dono,
        "variante": var.id,
        # QUAL texto saiu (hash curto): a variante pode ter a copy editada na
        # tela, e o placar só soma resultado da versão que vale agora — senão
        # mudar o texto no meio do teste misturaria duas copies numa taxa só.
        "copy_versao": copy_versao,
        "categoria": categoria,
        "template": r.get("template"),
        # O id da mensagem na Meta. Sem ele o placar sabe que mandou e não tem
        # como saber se chegou: o status de entrega é gravado em
        # `mensagens_whatsapp` por wamid (`remarketing_placar` faz o cruzamento).
        "wamid": wamid,
    }
    if wamid is None:
        payload["sem_wamid"] = True
        log.error("carrossel_disparo_sem_wamid", lead_id=lead["id"],
                  template=r.get("template"))

    # (o stage já virou Disparado em `_reservar`, antes de a mensagem sair)
    #
    # A reserva vira disparo: sai uma, entra o outro, na mesma transação (o
    # commit é do tick). Se qualquer coisa daqui pra baixo falhar, o rollback
    # desfaz as duas e a reserva CONTINUA lá — segurando o teto do dia — e
    # leva o wamid, pro conferente fechar a conta.
    try:
        _apagar_reserva(session, lead["id"], tenant_id)
        session.execute(
            sql_text(
                """
                INSERT INTO lead_events (tenant_id, lead_id, tipo, payload, at)
                VALUES (:t, :i, :ev, CAST(:p AS jsonb), now())
                """
            ),
            {"t": tenant_id, "i": lead["id"], "ev": EVENT_DISPARO,
             "p": json.dumps(payload, ensure_ascii=False)},
        )
    except Exception as e:
        raise _SemRegistro(
            str(e)[:200],
            {k: payload[k] for k in ("wamid", "template", "categoria", "variante",
                                     "copy_versao")},
        ) from e


def processar_inbound(
    telefone: str,
    texto: str | None,
    tenant_id: int = 1,
    *,
    tipo: str | None = None,
    recebido_em: datetime | None = None,
) -> str | None:
    """Lead da roda mandou mensagem. Chamado pelo inbound da linha da Corretor.

    Quem decide o destino é `remarketing_resposta.classificar_resposta` — regra
    em código, sem IA (Dona 05/10/2026). O que esta função devolve:

      "opt_out"     pediu pra parar → opt-out gravado, card em Venda perdida.
                    Quem chama NÃO atende (regra antiga, inviolável).
      "automatica"  o WhatsApp do lead respondeu sozinho → nada muda, o card
                    fica em Disparado. Quem chama NÃO atende: responder robô é
                    conversa de duas secretárias eletrônicas.
      "recusa"      disse que não → card em Venda perdida, fora da roda.
                    Quem chama encerra a conversa como recusa.
      "numero_errado"  o telefone é de outra pessoa → Venda perdida e
                    telefone marcado. Quem chama NÃO atende.
      "interesse_lido"  pedido ou "sim" que só a regra do remarketing lê
                    ("Olá, estou sim", "manda por favor") → card em Respondeu,
                    e quem chama faz o handoff como interesse.
      "respondeu"   interesse, botão ou indefinida → card em Respondeu.
      None          não era lead da roda (ou o banco falhou).

    `tipo` e `recebido_em` vêm do webhook (tipo da mensagem na Cloud API e a
    hora que a Meta carimbou). Sem eles a regra continua valendo, só sem botão
    e sem relógio — é o que acontece com quem ainda chama do jeito antigo.

    Best-effort: NUNCA levanta. Inbound quebrado é mensagem de lead perdida.
    """
    from app.db.session import SessionLocal
    from app.services import remarketing_resposta as rr

    # Classifica ANTES de tocar no banco, e FORA do try: é texto puro. Assim,
    # se o Postgres cair no meio, ainda sabemos que a pessoa pediu pra parar —
    # e o caller não atende quem mandou "PARAR". Antes o except devolvia None
    # pra tudo e a Pessoa respondia em cima do pedido de saída: caminho curto
    # pra denúncia. (Sem os segundos, o relógio não opina; parar ganha dele.)
    parar = rr.classificar_resposta(texto, tipo, None) == rr.PARAR
    try:
        with SessionLocal() as session:
            if parar:
                classe, regra = rr.PARAR, rr.REGRA_PARAR
            else:
                classe, regra = rr.classificar_com_regra(
                    texto, tipo,
                    _segundos_desde_disparo(session, telefone, tenant_id, recebido_em),
                    corte_segundos=config_operacional.carrossel_auto_resposta_segundos(),
                    resposta_direta=_e_resposta_direta(
                        session, telefone, tenant_id, recebido_em),
                )
            # IA DO LADO (05/10/2026): a regra não entendeu e o card ainda vai
            # cair em Respondeu? A IA opina, com prazo curto. "recusa" com
            # confiança no corte vai pra Venda perdida pelo MESMO caminho da
            # recusa da regra; "interesse" conta na taxa. Fora do ar, lenta,
            # inválida, "não sei" ou abaixo do corte: vale a regra, e o que
            # aconteceu fica no evento. Ver `ia_do_lado`.
            ia_reg = None
            if not parar and classe == rr.INDEFINIDA:
                from app.services import ia_do_lado

                alvo = _alvo_ou_padrao(session, tenant_id)
                pesou, ia_reg = ia_do_lado.opinar_na_resposta(
                    session, telefone, texto, tenant_id,
                    classe_regra=classe, regra=regra,
                    etapa_fila=alvo.etapa, dono=alvo.dono,
                    recebido_em=recebido_em,
                )
                if pesou == ia_do_lado.RECUSA:
                    classe, regra = rr.RECUSA, ia_do_lado.REGRA_IA
                elif pesou == ia_do_lado.INTERESSE:
                    classe, regra = rr.INTERESSE, ia_do_lado.REGRA_IA
            if parar:
                _gravar_opt_out(session, telefone, tenant_id)
            n = aplicar_resposta(
                session, telefone, classe, tenant_id,
                # O que a IA leu nunca reabre card de Venda perdida.
                reabre=regra != REGRA_IA_OPINIAO and (classe == rr.BOTAO or (
                    classe == rr.INTERESSE and rr.interesse_explicito(texto)
                )),
                regra=regra,
            )
            if ia_reg is not None and n:
                _anotar_ia_na_resposta(session, telefone, tenant_id, ia_reg)
        if parar:
            return "opt_out"
        if not n:
            # Não é lead da roda: site, parceiro, conversa da Pessoa12. Segue a vida
            # como sempre — inclusive quando o texto parece automático.
            return None
        if classe == rr.AUTOMATICA:
            return "automatica"
        if classe == rr.NUMERO_ERRADO:
            return "numero_errado"
        if classe == rr.RECUSA:
            return "recusa"
        if classe == rr.INTERESSE and regra in rr.REGRAS_AMPLIADAS:
            return "interesse_lido"
        return "respondeu"
    except Exception as e:  # noqa: BLE001
        log.warning("carrossel_inbound_falhou", erro=str(e)[:140], parar=parar)
        # Falha fechada no que dói: opt-out não gravado é problema, mas
        # RESPONDER quem pediu pra parar é pior. A gravação perdida é
        # recuperada no próximo inbound (a pessoa não volta pra fila hoje
        # porque `_SQL_FILA` também exclui quem tem disparo recente).
        return "opt_out" if parar else None


# Relógio do webhook × relógio do servidor: o disparo é carimbado por nós
# (`now()` no INSERT, ~10 ms depois do envio) e a resposta pela Meta, em segundo
# cheio. Uma resposta automática pode chegar "antes" do disparo por 1–2 s.
# Além disto não é resposta a ESTE disparo: o relógio se cala.
_FOLGA_RELOGIO_S = 3.0


def _segundos_desde_disparo(session: Session, telefone: str, tenant_id: int,
                            recebido_em: datetime | None) -> float | None:
    """Quanto tempo depois do último disparo pra este telefone a mensagem
    chegou. None = sem disparo registrado (o relógio não opina)."""
    suf = _so_digitos(telefone)[-8:]
    if not suf:
        return None
    ultimo = session.execute(
        sql_text(
            r"""
            SELECT max(le.at)
              FROM lead_events le
              JOIN leads l ON l.id = le.lead_id
             WHERE le.tipo = :ev_disp
               AND le.tenant_id = :t
               AND right(regexp_replace(l.telefone, '\D', '', 'g'), 8) = :suf
            """
        ),
        {"t": tenant_id, "suf": suf, "ev_disp": EVENT_DISPARO},
    ).scalar()
    if ultimo is None:
        return None
    quando = recebido_em or datetime.now(timezone.utc)
    if quando.tzinfo is None:
        quando = quando.replace(tzinfo=timezone.utc)
    if ultimo.tzinfo is None:
        ultimo = ultimo.replace(tzinfo=timezone.utc)
    seg = (quando - ultimo).total_seconds()
    return seg if seg >= -_FOLGA_RELOGIO_S else None


# Até quando depois do disparo uma primeira mensagem ainda é "resposta a ele".
# Nas 395 respostas reais, a primeira chegou até 11 dias depois.
RESPOSTA_DIRETA_ATE_DIAS = 15


def _e_resposta_direta(session: Session, telefone: str, tenant_id: int,
                       recebido_em: datetime | None) -> bool:
    """Esta é a PRIMEIRA mensagem do lead depois do disparo, e ninguém falou
    com ele no meio?

    É a condição pra ler "Não", "Achei" e "estou sim": só querem dizer alguma
    coisa como resposta ao "ainda está procurando?". Qualquer mensagem no meio
    — do lead, da Pessoa, de uma corretora, em qualquer linha — e deixa de ser
    resposta direta. Sem hora da Meta (`recebido_em`) não dá pra saber: False.
    Erro de leitura também é False — na dúvida, a regra nova não opina.
    """
    suf = _so_digitos(telefone)[-8:]
    if not suf or recebido_em is None:
        return False
    quando = recebido_em if recebido_em.tzinfo else recebido_em.replace(tzinfo=timezone.utc)
    try:
        linha = session.execute(
            sql_text(
                r"""
                WITH d AS (
                    SELECT max(le.at) AS em
                      FROM lead_events le
                      JOIN leads l ON l.id = le.lead_id
                     WHERE le.tipo = :ev_disp AND le.tenant_id = :t
                       AND right(regexp_replace(l.telefone, '\D', '', 'g'), 8) = :suf
                )
                SELECT d.em,
                       (SELECT count(*) FROM mensagens_whatsapp m
                         WHERE m.fone8 = :suf
                           AND m.timestamp_evolution > d.em
                           AND m.timestamp_evolution < :quando
                           -- o template do próprio disparo não é "alguém no meio"
                           AND (m.from_corretora IS NOT TRUE
                                OR m.tipo_mensagem <> 'template')) AS no_meio
                  FROM d
                """
            ),
            {"t": tenant_id, "suf": suf, "ev_disp": EVENT_DISPARO, "quando": quando},
        ).first()
    except Exception as e:  # noqa: BLE001
        session.rollback()
        log.warning("carrossel_resposta_direta_ilegivel", erro=str(e)[:120])
        return False
    if linha is None or linha[0] is None:
        return False
    disparo = linha[0] if linha[0].tzinfo else linha[0].replace(tzinfo=timezone.utc)
    seg = (quando - disparo).total_seconds()
    if seg < -_FOLGA_RELOGIO_S or seg > RESPOSTA_DIRETA_ATE_DIAS * 86400:
        return False
    return int(linha[1] or 0) == 0


def _gravar_opt_out(session: Session, telefone: str, tenant_id: int) -> None:
    """Marca opt-out por sufixo-8 — a MESMA chave que o `_SQL_FILA` consulta.

    Se não havia disparo registrado pra esse telefone, insere um só pra segurar
    o opt-out: sem linha, o filtro da fila não teria o que ver e o lead voltaria.
    """
    suf = _so_digitos(telefone)[-8:]
    if not suf:
        return
    n = len(session.execute(
        sql_text(
            r"""
            UPDATE disparos_reengajamento
               SET opt_out = TRUE, opt_out_em = now()
             WHERE tenant_id = :t
               AND right(regexp_replace(telefone, '\D', '', 'g'), 8) = :suf
            RETURNING id
            """
        ),
        {"t": tenant_id, "suf": suf},
    ).fetchall())
    if not n:
        session.execute(
            sql_text(
                """
                INSERT INTO disparos_reengajamento
                    (tenant_id, corretora_id, telefone, template, enviado_por,
                     enviado_em, opt_out, opt_out_em)
                VALUES (:t, :corretor, :tel, 'carrossel', 'carrossel',
                        now(), TRUE, now())
                """
            ),
            {"t": tenant_id, "corretor": CORRETORA_CORRETOR, "tel": _so_digitos(telefone)},
        )
    session.commit()
    log.info("carrossel_opt_out", suf=suf, tinha_disparo=bool(n))


FUNIL_PRINCIPAL = "principal"
NOME_VENDA_PERDIDA = "Venda perdida"


def _stage_venda_perdida(session: Session, tenant_id: int) -> int | None:
    """Id da "Venda perdida" DESTE cliente. Por nome dentro do funil, nunca por
    id cravado: `pipeline_stages` tem etapas de dois clientes com nomes iguais
    ("Novo" 4 e 6, "Proposta" 2 e 11). Na Acme é a 13."""
    return session.execute(
        sql_text(
            """SELECT id FROM pipeline_stages
                WHERE tenant_id = :t AND funil = :f AND nome = :n
                ORDER BY id LIMIT 1"""
        ),
        {"t": tenant_id, "f": FUNIL_PRINCIPAL, "n": NOME_VENDA_PERDIDA},
    ).scalar()


def _stage_do_parar(session: Session, tenant_id: int, perdida: int) -> int:
    """Pra qual etapa vai quem pediu pra PARAR.

    Padrão: Venda perdida (decisão da Dona, 05/10/2026). Vira "Não perturbe"
    gravando `remarketing_etapa_parar` — a escolha entre as duas segue aberta
    com ela, e trocar não pode exigir deploy. Valor torto no banco não inventa
    destino: cai na Venda perdida, com erro no log.
    """
    try:
        bruto = config_operacional.get("remarketing_etapa_parar")
        if bruto is None:
            return perdida
        remarketing_alvo.conferir_etapa_parar(session, tenant_id, bruto)
        return int(bruto)
    except Exception as e:  # noqa: BLE001
        log.error("carrossel_etapa_parar_invalida_usando_venda_perdida",
                  erro=str(e)[:140])
        return perdida


def _alvo_ou_padrao(session: Session, tenant_id: int) -> Alvo:
    try:
        return remarketing_alvo.ler(session, tenant_id)
    except remarketing_alvo.AlvoInvalido:
        # Alvo torto segura o DISPARO; a resposta de quem já recebeu
        # continua sendo tratada, com o padrão.
        return Alvo(STAGE_FILA_PADRAO, CORRETORA_CORRETOR)


def marcar_respondeu(session: Session, telefone: str, tenant_id: int = 1,
                     classe: str | None = None, regra: str | None = None) -> int:
    """Lead da roda que respondeu sai dela (vai pra Respondeu).

    Chamado pelo webhook de inbound. Sufixo-8 porque é o que o webhook entrega.
    `classe` (de `remarketing_resposta`) vai no evento; None = chamada antiga.
    """
    suf = _so_digitos(telefone)[-8:]
    if not suf:
        return 0
    try:
        alvo = _alvo_ou_padrao(session, tenant_id)
        n = len(session.execute(
            sql_text(
                r"""
                UPDATE leads SET stage_id = :r, updated_at = now()
                 WHERE tenant_id = :t AND owner_id IN (:corretor, :dono)
                   AND stage_id IN (:fila_padrao, :fila, :disp)
                   AND right(regexp_replace(telefone, '\D', '', 'g'), 8) = :suf
                RETURNING id
                """
            ),
            # Vale a fila/dono de sempre E os configurados agora: lead que ainda
            # está na Fila 23 da Corretor continua saindo da roda quando responde,
            # mesmo com a tela apontando o disparo pra outra etapa.
            {"t": tenant_id, "corretor": CORRETORA_CORRETOR, "dono": alvo.dono,
             "r": STAGE_RESPONDEU, "fila_padrao": STAGE_FILA_PADRAO,
             "fila": alvo.etapa, "disp": STAGE_DISPARADO, "suf": suf},
        ).fetchall())
        if n:
            _marcar_resposta_na_variante(session, suf, tenant_id, classe, regra)
        session.commit()
        if n:
            log.info("carrossel_lead_respondeu", suf=suf, n=n, classe=classe)
        return n
    except Exception as e:  # noqa: BLE001
        session.rollback()
        log.warning("carrossel_marcar_respondeu_falhou", erro=str(e)[:140])
        return 0


# O card só volta de Venda perdida pra Respondeu se foi ESTA regra que o pôs
# lá, nesta rodada: existe `carrossel_resposta` com classe 'recusa' depois do
# último disparo. Venda perdida de corretora, de opt-out ou de outra época não
# é reaberta por um "quero" na linha da Corretor.
_SQL_REABRIR = r"""
    UPDATE leads l SET stage_id = :r, updated_at = now()
     WHERE l.tenant_id = :t AND l.owner_id IN (:corretor, :dono)
       AND l.stage_id = :perdida
       AND right(regexp_replace(l.telefone, '\D', '', 'g'), 8) = :suf
       AND EXISTS (
            SELECT 1 FROM lead_events r
             WHERE r.lead_id = l.id AND r.tipo = :ev_resp
               AND r.payload->>'classe' = 'recusa'
               AND r.at > (SELECT max(d.at) FROM lead_events d
                            WHERE d.lead_id = l.id AND d.tipo = :ev_disp)
       )
    RETURNING l.id
"""


def aplicar_resposta(session: Session, telefone: str, classe: str,
                     tenant_id: int = 1, *, reabre: bool = False,
                     regra: str | None = None) -> int:
    """Move o card pela classe da resposta. Devolve quantos leads da roda a
    mensagem alcançou (0 = não era lead da roda).

        interesse / botão / indefinida → Respondeu
        recusa / parar                 → Venda perdida (sai da roda de vez);
                                         o destino do parar é configurável
        número errado                  → Venda perdida + telefone marcado
        automática                     → fica onde está; só registra

    `regra` é qual regra do classificador decidiu — vai gravada no evento.

    `reabre`: a fala traz interesse explícito (ou é clique de botão) — desfaz a
    recusa desta rodada (caso Renato). Parar nunca é desfeito.

    Só toca lead cujo dono ainda é o do disparo: quem já foi transferido pra
    corretora não é de robô, e mexer no card dela é passar por cima de gente.
    """
    from app.services import remarketing_resposta as rr

    suf = _so_digitos(telefone)[-8:]
    if not suf:
        return 0
    if classe in rr.CLASSES_RESPONDEU:
        n = marcar_respondeu(session, telefone, tenant_id, classe, regra)
    else:
        n = 0
    try:
        alvo = _alvo_ou_padrao(session, tenant_id)
        base = {"t": tenant_id, "corretor": CORRETORA_CORRETOR, "dono": alvo.dono,
                "suf": suf, "ev_disp": EVENT_DISPARO, "ev_resp": EVENT_RESPOSTA}

        if classe == rr.AUTOMATICA:
            n = _registrar_automatica(session, suf, tenant_id, alvo)

        elif classe in rr.CLASSES_VENDA_PERDIDA:
            perdida = _stage_venda_perdida(session, tenant_id)
            if perdida is None:
                # Cliente sem "Venda perdida" no funil: o que não pode é o lead
                # ficar na roda. Sai pra Respondeu e grita.
                log.error("carrossel_sem_venda_perdida", tenant_id=tenant_id)
                n = marcar_respondeu(session, telefone, tenant_id, classe, regra)
            else:
                destino = (_stage_do_parar(session, tenant_id, perdida)
                           if classe == rr.PARAR else perdida)
                # Inclui quem já está em Respondeu: "bom dia" ontem, "não quero"
                # hoje — a recusa vale igual.
                movidos = session.execute(
                    sql_text(
                        r"""
                        UPDATE leads SET stage_id = :perdida, updated_at = now()
                         WHERE tenant_id = :t AND owner_id IN (:corretor, :dono)
                           AND stage_id IN (:fila_padrao, :fila, :disp, :resp)
                           AND right(regexp_replace(telefone, '\D', '', 'g'), 8) = :suf
                        RETURNING id
                        """
                    ),
                    {**{k: base[k] for k in ("t", "corretor", "dono", "suf")},
                     "perdida": destino, "fila_padrao": STAGE_FILA_PADRAO,
                     "fila": alvo.etapa, "disp": STAGE_DISPARADO,
                     "resp": STAGE_RESPONDEU},
                ).fetchall()
                n = len(movidos)
                if n:
                    _marcar_resposta_na_variante(session, suf, tenant_id, classe, regra)
                    log.info("carrossel_lead_venda_perdida", suf=suf, n=n,
                             classe=classe, etapa=destino)
                if n and classe == rr.NUMERO_ERRADO:
                    _marcar_telefone_de_outra_pessoa(
                        session, [int(r[0]) for r in movidos], tenant_id)

        elif reabre and classe in rr.CLASSES_QUE_CONTAM:
            perdida = _stage_venda_perdida(session, tenant_id)
            if perdida is not None:
                reabertos = len(session.execute(
                    sql_text(_SQL_REABRIR),
                    {**base, "r": STAGE_RESPONDEU, "perdida": perdida},
                ).fetchall())
                if reabertos:
                    log.info("carrossel_recusa_reaberta", suf=suf, n=reabertos)
                n += reabertos

        if classe != rr.AUTOMATICA:
            # A classe do evento acompanha a conversa: "bom dia" e depois
            # "quero sim" é um respondente com interesse, não um indefinido.
            _reclassificar_resposta(session, suf, tenant_id, classe, reabre, regra)
        session.commit()
        return n
    except Exception as e:  # noqa: BLE001
        session.rollback()
        log.warning("carrossel_aplicar_resposta_falhou", erro=str(e)[:140],
                    classe=classe)
        return n


def _marcar_telefone_de_outra_pessoa(session: Session, lead_ids: list[int],
                                     tenant_id: int) -> None:
    """Quem respondeu disse que o telefone não é do lead. O card já foi pra
    Venda perdida; aqui o TELEFONE fica marcado (`telefone_invalido`, a mesma
    marca que tira da fila o número que não existe) e fica um evento dizendo
    por quê. Sem commit — fecha junto com a transação de quem chama."""
    session.execute(
        sql_text("UPDATE leads SET telefone_invalido = TRUE, updated_at = now() "
                 " WHERE id = ANY(:ids) AND tenant_id = :t"),
        {"ids": lead_ids, "t": tenant_id},
    )
    session.execute(
        sql_text(
            """
            INSERT INTO lead_events (tenant_id, lead_id, tipo, payload, at)
            SELECT :t, x, 'telefone_de_outra_pessoa',
                   jsonb_build_object('origem', 'carrossel',
                                      'motivo', 'quem respondeu disse que o número não é do lead'),
                   now()
              FROM unnest(CAST(:ids AS bigint[])) AS x
            """
        ),
        {"ids": lead_ids, "t": tenant_id},
    )
    log.info("carrossel_telefone_de_outra_pessoa", n=len(lead_ids))


# Último disparo de cada lead do telefone — o disparo que esta mensagem responde.
_SQL_ULTIMO_DISPARO_DO_TELEFONE = r"""
    SELECT DISTINCT ON (le.lead_id) le.lead_id, le.payload, le.at
      FROM lead_events le
      JOIN leads l ON l.id = le.lead_id
     WHERE le.tipo = :ev_disp
       AND le.tenant_id = :t
       AND right(regexp_replace(l.telefone, '\D', '', 'g'), 8) = :suf
     ORDER BY le.lead_id, le.at DESC
"""


def _marcar_resposta_na_variante(session: Session, suf: str, tenant_id: int,
                                 classe: str | None = None,
                                 regra: str | None = None) -> None:
    """Carimba a resposta com a variante do disparo que a provocou.

    Sem isso o relatório saberia quantos responderam, mas não a QUAL copy — que
    é justamente o que o teste existe pra descobrir. Copia a variante do último
    `carrossel_disparo` do lead.

    Só conta a PRIMEIRA resposta de cada disparo: lead que manda três mensagens
    seguidas é um respondente, não três. A `classe` vai junto (05/10/2026): é
    ela que diz se a resposta entra na taxa (`placar`).
    """
    session.execute(
        sql_text(
            r"""
            INSERT INTO lead_events (tenant_id, lead_id, tipo, payload, at)
            SELECT :t, d.lead_id, :ev_resp,
                   jsonb_build_object(
                       'variante', d.payload->>'variante',
                       'copy_versao', d.payload->>'copy_versao',
                       'categoria', d.payload->>'categoria',
                       'disparo_em', d.at
                   ) || CASE WHEN CAST(:classe AS text) IS NULL THEN '{}'::jsonb
                             ELSE jsonb_build_object('classe', CAST(:classe AS text))
                        END
                     || CASE WHEN CAST(:regra AS text) IS NULL THEN '{}'::jsonb
                             ELSE jsonb_build_object('regra', CAST(:regra AS text))
                        END,
                   now()
              FROM (""" + _SQL_ULTIMO_DISPARO_DO_TELEFONE + r""") d
             WHERE NOT EXISTS (
                SELECT 1 FROM lead_events r
                 WHERE r.lead_id = d.lead_id
                   AND r.tipo = :ev_resp
                   AND r.at > d.at
             )
            """
        ),
        {"t": tenant_id, "suf": suf, "ev_disp": EVENT_DISPARO,
         "ev_resp": EVENT_RESPOSTA, "classe": classe, "regra": regra},
    )


def _anotar_ia_na_resposta(session: Session, telefone: str, tenant_id: int,
                           ia: dict[str, Any]) -> None:
    """Põe o que a IA disse (classe da regra, da IA, confiança, trecho, se
    pesou) no `carrossel_resposta` desta rodada. Best-effort: anotar não pode
    desfazer o que a regra já gravou."""
    suf = _so_digitos(telefone)[-8:]
    try:
        session.execute(
            sql_text(
                r"""
                UPDATE lead_events r SET payload = r.payload || jsonb_build_object('ia', CAST(:ia AS jsonb))
                  FROM (""" + _SQL_ULTIMO_DISPARO_DO_TELEFONE + r""") d
                 WHERE r.lead_id = d.lead_id AND r.tipo = :ev_resp AND r.at > d.at
                """
            ),
            {"t": tenant_id, "suf": suf, "ev_disp": EVENT_DISPARO,
             "ev_resp": EVENT_RESPOSTA, "ia": json.dumps(ia, ensure_ascii=False)},
        )
        session.commit()
    except Exception as e:  # noqa: BLE001
        session.rollback()
        log.warning("carrossel_anotar_ia_falhou", erro=str(e)[:120])


def anotar_ia_tardia(session: Session, telefone: str, tenant_id: int,
                     tardia: dict[str, Any], *, conta_interesse: bool = False) -> None:
    """A opinião da IA que chegou depois do prazo do turno (`ia_do_lado`). Só
    anota (`ia.tardia`); com `conta_interesse`, uma resposta que ficou
    `indefinida` passa a `interesse` NO EVENTO (taxa). Não toca no card."""
    suf = _so_digitos(telefone)[-8:]
    try:
        session.execute(
            sql_text(
                r"""
                UPDATE lead_events r
                   SET payload = jsonb_set(
                         r.payload || CASE WHEN CAST(:conta AS boolean)
                                            AND r.payload->>'classe' = 'indefinida'
                                      THEN jsonb_build_object('classe', 'interesse',
                                                              'regra', CAST(:regra AS text))
                                      ELSE '{}'::jsonb END,
                         '{ia}', COALESCE(r.payload->'ia', '{}'::jsonb)
                                 || jsonb_build_object('tardia', CAST(:tardia AS jsonb)))
                  FROM (""" + _SQL_ULTIMO_DISPARO_DO_TELEFONE + r""") d
                 WHERE r.lead_id = d.lead_id AND r.tipo = :ev_resp AND r.at > d.at
                """
            ),
            {"t": tenant_id, "suf": suf, "ev_disp": EVENT_DISPARO,
             "ev_resp": EVENT_RESPOSTA, "tardia": json.dumps(tardia, ensure_ascii=False),
             "conta": conta_interesse, "regra": REGRA_IA_OPINIAO},
        )
        session.commit()
    except Exception as e:  # noqa: BLE001
        session.rollback()
        log.warning("carrossel_anotar_ia_tardia_falhou", erro=str(e)[:120])


def _reclassificar_resposta(session: Session, suf: str, tenant_id: int,
                            classe: str, reabre: bool,
                            regra: str | None = None) -> None:
    """A classe do `carrossel_resposta` desta rodada segue a conversa.

    Quem pode virar o quê (a classe que está → a que chega):
        parar       nunca muda — pedido de saída não se desfaz
        indefinida  vira interesse, botão, recusa ou parar
        interesse / botão  viram recusa ou parar ("quero"… "na verdade não")
        recusa      vira parar; vira interesse/botão SÓ com `reabre`
    Evento antigo, sem classe, é tratado como indefinida.
    """
    from app.services import remarketing_resposta as rr

    if classe == rr.PARAR:
        de = [rr.INDEFINIDA, rr.INTERESSE, rr.BOTAO, rr.RECUSA]
    elif classe in (rr.RECUSA, rr.NUMERO_ERRADO):
        # Número errado fecha como a recusa — e ganha dela: "não quero" seguido
        # de "esse telefone não é da Fulano" é telefone de outra pessoa.
        de = [rr.INDEFINIDA, rr.INTERESSE, rr.BOTAO] + (
            [rr.RECUSA] if classe == rr.NUMERO_ERRADO else [])
    elif classe in rr.CLASSES_QUE_CONTAM:
        de = [rr.INDEFINIDA] + ([rr.RECUSA] if reabre else [])
    else:
        return
    session.execute(
        sql_text(
            r"""
            UPDATE lead_events r
               SET payload = r.payload || jsonb_build_object(
                       'classe', CAST(:classe AS text),
                       'regra', CAST(:regra AS text),
                       'classe_anterior', COALESCE(r.payload->>'classe', 'indefinida'),
                       'reclassificado_em', now())
              FROM (""" + _SQL_ULTIMO_DISPARO_DO_TELEFONE + r""") d
             WHERE r.lead_id = d.lead_id
               AND r.tipo = :ev_resp
               AND r.at > d.at
               AND COALESCE(r.payload->>'classe', 'indefinida') = ANY(:de)
            """
        ),
        {"t": tenant_id, "suf": suf, "ev_disp": EVENT_DISPARO,
         "ev_resp": EVENT_RESPOSTA, "classe": classe, "de": de, "regra": regra},
    )


def _registrar_automatica(session: Session, suf: str, tenant_id: int,
                          alvo: Alvo) -> int:
    """Resposta automática: o card NÃO se mexe e a variante NÃO ganha resposta.

    Fica um `carrossel_auto_resposta` (tipo próprio, de propósito: nada que
    conta `carrossel_resposta` passa a enxergar robô) — um por disparo, pra a
    tela poder mostrar quantas foram. Devolve quantos leads AINDA NA RODA a
    mensagem alcançou: é o que diz ao webhook "isto era do carrossel".
    """
    na_roda = session.execute(
        sql_text(
            r"""
            SELECT l.id FROM leads l
             WHERE l.tenant_id = :t AND l.owner_id IN (:corretor, :dono)
               AND l.stage_id IN (:fila_padrao, :fila, :disp)
               AND right(regexp_replace(l.telefone, '\D', '', 'g'), 8) = :suf
            """
        ),
        {"t": tenant_id, "corretor": CORRETORA_CORRETOR, "dono": alvo.dono,
         "fila_padrao": STAGE_FILA_PADRAO, "fila": alvo.etapa,
         "disp": STAGE_DISPARADO, "suf": suf},
    ).fetchall()
    if not na_roda:
        return 0
    session.execute(
        sql_text(
            r"""
            INSERT INTO lead_events (tenant_id, lead_id, tipo, payload, at)
            SELECT :t, d.lead_id, :ev_auto,
                   jsonb_build_object(
                       'variante', d.payload->>'variante',
                       'copy_versao', d.payload->>'copy_versao',
                       'categoria', d.payload->>'categoria',
                       'disparo_em', d.at),
                   now()
              FROM (""" + _SQL_ULTIMO_DISPARO_DO_TELEFONE + r""") d
             WHERE d.lead_id = ANY(:ids)
               AND NOT EXISTS (
                SELECT 1 FROM lead_events r
                 WHERE r.lead_id = d.lead_id AND r.tipo = :ev_auto AND r.at > d.at
             )
            """
        ),
        {"t": tenant_id, "suf": suf, "ev_disp": EVENT_DISPARO,
         "ev_auto": EVENT_AUTO_RESPOSTA, "ids": [int(r[0]) for r in na_roda]},
    )
    log.info("carrossel_resposta_automatica", suf=suf, n=len(na_roda))
    return len(na_roda)


# Colunas de contagem do placar, na ordem em que a tela lê.
PLACAR_CONTAGENS = ("enviados", "respostas", "sem_classe", "recusas",
                    "pararam", "indefinidas", "automaticas", "numeros_errados",
                    "botoes")


def placar(session: Session | None = None, dias: int = 30,
           tenant_id: int = 1, desde: datetime | None = None,
           *, por_versao: bool = False) -> list[dict[str, Any]]:
    """Qual copy está ganhando. Uma linha por variante.

    `taxa` é resposta/enviado. Compara só o que teve volume parecido — variante
    com 12 envios não "ganha" de outra com 300, ainda que a taxa seja maior.

    `respostas` (e a taxa) só conta INTERESSE e BOTÃO (Dona 05/10/2026): quem
    disse não, quem pediu pra parar e o WhatsApp que respondeu sozinho não são
    copy funcionando. Vão em colunas próprias — `recusas`, `pararam`,
    `automaticas`, `indefinidas` — pra a tela mostrar sem somar na taxa.
    Resposta anterior à regra não tem classe gravada: segue contando em
    `respostas`, como contava no dia em que foi medida, e aparece em
    `sem_classe` pra quem quiser separar.

    `desde` (opcional) sobrepõe a janela de `dias`. Existe pro painel respeitar
    o MARCO de régua: quando alguém recomeça a medição, "últimos 30 dias" passa
    a valer só do marco pra cá — e a tela mostra desde quando conta.

    `por_versao` (05/10/2026): abre cada variante pela `copy_versao` do disparo
    — o texto que saiu. Cada linha ganha `copy_versao` (None = disparo anterior
    à edição de copy pela tela, que saiu com o texto do código). Quem chama
    decide qual versão é "a de agora"; aqui só se separa.
    """
    from app.services import carrossel_copies as cc

    owns = session is None
    if owns:
        from app.db.session import SessionLocal

        session = SessionLocal()
    try:
        linhas = session.execute(
            sql_text(
                """
                SELECT payload->>'variante'  AS variante,
                       payload->>'categoria' AS categoria,
                       CASE WHEN CAST(:por_versao AS boolean)
                            THEN payload->>'copy_versao' END AS copy_versao,
                       count(*) FILTER (WHERE tipo = :ev_disp) AS enviados,
                       count(*) FILTER (WHERE tipo = :ev_resp AND (
                             payload->>'classe' IS NULL
                          OR payload->>'classe' IN ('interesse', 'botao'))) AS respostas,
                       count(*) FILTER (WHERE tipo = :ev_resp
                             AND payload->>'classe' IS NULL)         AS sem_classe,
                       count(*) FILTER (WHERE tipo = :ev_resp
                             AND payload->>'classe' = 'recusa')      AS recusas,
                       count(*) FILTER (WHERE tipo = :ev_resp
                             AND payload->>'classe' = 'parar')       AS pararam,
                       count(*) FILTER (WHERE tipo = :ev_resp
                             AND payload->>'classe' = 'indefinida')  AS indefinidas,
                       count(*) FILTER (WHERE tipo = :ev_auto)       AS automaticas,
                       count(*) FILTER (WHERE tipo = :ev_resp
                             AND payload->>'classe' = 'numero_errado') AS numeros_errados,
                       -- Parte de `respostas`: quem clicou no botão. A tela
                       -- mostra interesse e botão separados; a taxa soma os dois.
                       count(*) FILTER (WHERE tipo = :ev_resp
                             AND payload->>'classe' = 'botao')       AS botoes
                  FROM lead_events
                 WHERE tenant_id = :t
                   AND tipo IN (:ev_disp, :ev_resp, :ev_auto)
                   AND at > coalesce(
                         CAST(:desde AS timestamptz),
                         now() - (:dias || ' days')::interval
                       )
                 GROUP BY 1, 2, 3
                 ORDER BY 1, 2, 3
                """
            ),
            {"t": tenant_id, "dias": dias, "desde": desde, "por_versao": por_versao,
             "ev_disp": EVENT_DISPARO, "ev_resp": EVENT_RESPOSTA,
             "ev_auto": EVENT_AUTO_RESPOSTA},
        ).mappings().all()
    finally:
        if owns:
            session.close()

    juntos: dict[tuple[str, str, str | None], dict[str, Any]] = {}
    for r in linhas:
        # Disparos anteriores ao teste A/B não têm variante gravada. Rotular
        # como "antes-do-teste" e não "(sem)" evita que a linha pareça uma
        # variante pausada no relatório.
        k = (r["variante"] or "antes-do-teste", r["categoria"] or "UTILITY",
             r["copy_versao"])
        alvo = juntos.setdefault(k, {"variante": k[0], "categoria": k[1],
                                     **({"copy_versao": k[2]} if por_versao else {}),
                                     **{c: 0 for c in PLACAR_CONTAGENS}})
        for c in PLACAR_CONTAGENS:
            alvo[c] += r[c]

    saida = []
    for v in juntos.values():
        v["taxa"] = round(100 * v["respostas"] / v["enviados"], 1) if v["enviados"] else 0.0
        var = cc.por_id(v["variante"])
        v["ativa"] = bool(var and var.ativa)
        v["peso"] = var.peso if var else 0
        saida.append(v)
    return sorted(saida, key=lambda x: (-x["taxa"], -x["enviados"]))
