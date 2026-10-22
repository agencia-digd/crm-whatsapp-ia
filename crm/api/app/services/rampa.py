"""
Rampa anti-ban por linha — quanto a linha pode mandar HOJE.

Número frio que salta direto pro teto é o padrão clássico que derruba a
qualidade e leva ban. A linha da Corretor (0000-0000, que exibia 0000-0000 até
15/09/2026) é o número DA CASA: se ela cair, a Acme perde o telefone
principal, não só uma automação. E são 5.253 leads na fila esperando.

A rampa resolve subindo devagar e **olhando a qualidade real da Meta** a cada
passo — não é um agendamento cego.

  degrau 1 (dias 1-2):   20/dia   ← aquece
  degrau 2 (dias 3-4):   35/dia
  degrau 3 (dias 5-6):   50/dia
  degrau 4 (dia 7+):     60/dia   ← teto pedido pela Dona

REGRA DE AVANÇO: só sobe se a qualidade estiver **GREEN**. Em YELLOW congela no
degrau atual; em RED ou FLAGGED **zera** (a linha para de disparar até alguém
olhar). Não existe subir com qualidade ruim.

FAIL-SAFE: se não der pra consultar a Meta (rate limit, token, rede), o teto é
ZERO — sem leitura não envia (05/10/2026; antes assumia o primeiro degrau).
Erro nunca libera volume.

O "dia" da rampa conta dias em que a linha REALMENTE despachou (não dias de
calendário): uma linha parada no fim de semana não "ganha" degrau de graça.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import structlog
from sqlalchemy import text as sql_text

log = structlog.get_logger()

# (dias_ativos_minimos, cap_do_dia)
# Dona 30/07: começa em 50/dia, não em 20. O 0000 não é número frio — é a linha
# fixa da Acme, em coexistência, com histórico real e qualidade GREEN. A rampa
# continua valendo pra CIMA (60 depois de 3 dias ativos) e, principalmente, pros
# freios: YELLOW recua um degrau, RED/FLAGGED zera.
# 300/DIA DIRETO — ordem da Dona em 12/08, com o risco na mesa.
#
# A escada gradual (50→80→120→170→230→300) existiu por um dia e foi substituída
# a pedido dela. Fica registrado o que eu havia argumentado: o 0000 é o telefone
# da casa, e salto de 50 pra 300 é o padrão que mais provoca bloqueio em número
# aquecido. Ela decidiu assumir.
#
# O QUE AINDA PROTEGE: os freios de qualidade continuam mandando. YELLOW recua
# um degrau (aqui, zera — só existe um), RED/FLAGGED para tudo, e qualidade
# indisponível trava no mínimo. Se a linha reagir mal, ela freia sozinha antes
# de bloquear.
#
# Histórico: até 11/08 o teto era 60 — e era ELE que segurava a Corretor, não o cap.
# Mesmo com o cap em 100 ou 300, a rampa cortava em 50/60 e ninguém entendia
# por que o volume não subia.
# 80/dia — Dona, 15/09/2026, na volta do número novo (0000-0000).
#
# Por que 80 e não 300: o número é NOVO. Ele herdou os templates e o
# phone_number_id do 0000, mas a REPUTAÇÃO começa do zero (qualidade voltou a
# UNKNOWN) e os 5.120 leads da base têm o número ANTIGO salvo — a mensagem
# chega de um desconhecido. Sair a 300 num número sem histórico é o padrão que
# a Meta lê como spam, e o 0000 já foi pra YELLOW uma vez fazendo isso.
#
# 80/dia com 962 na fila = 12 dias de trabalho. Quando a Meta confirmar GREEN e
# a taxa de resposta se mostrar sadia, sobe.
#
# ATÉ 300, MAS SÓ PEDINDO — 05/10/2026.
#
# A Dona pediu a capacidade de 300/dia pela tela. O segundo degrau existe pra
# isso, com duas condições que valem JUNTAS:
#
#   1. alguém gravou um limite por dia acima de 80 no painel (`pedido`). Sem
#      pedido o teto é o primeiro degrau, como era: publicar este arquivo não
#      muda um disparo sequer, com a linha GREEN ou não.
#   2. a Meta confirma qualidade **GREEN**. UNKNOWN (número novo, sem
#      reputação — o estado do 2166 em 15/09), leitura que falhou e YELLOW não
#      sobem. Erro nunca libera volume.
#
# Antes disto o campo da tela aceitava 200, gravava 200 e o motor mandava 80
# calado. Agora o `motivo` da rampa diz por que o pedido não valeu, e a tela
# mostra o teto efetivo ao lado do número digitado.
#
# ⚠️ GREEN não é reputação. O 0000 foi pra YELLOW saltando pra 300; o salto de
# 80 pra 300 de um dia pro outro é o padrão que a Meta lê como spam. A escada
# sugerida na tela é 120 → 170 → 230 → 300, alguns dias em cada.
DEGRAU_BASE = 80
DEGRAU_MAXIMO = 300
DEGRAUS: tuple[tuple[int, int], ...] = (
    (0, DEGRAU_BASE),
    (0, DEGRAU_MAXIMO),
)

# Piso de segurança quando a qualidade cai pra YELLOW e não há degrau abaixo.
# É o mesmo 50/dia que a Dona cravou em 30/07 como ritmo seguro do 0000.
CAP_AMARELO_MINIMO = 50

# Qualidades que a Meta devolve. Só GREEN deixa subir.
_OK = "GREEN"
_CONGELA = {"YELLOW"}
_PARA = {"RED", "FLAGGED", "RESTRICTED"}

# Cache curto da consulta de qualidade: o tick roda a cada poucos minutos e não
# faz sentido bater na Graph API toda vez. 10 min é curto o bastante pra reagir
# a uma queda no mesmo dia.
_TTL_S = 600
_cache: dict[str, tuple[float, str | None]] = {}


@dataclass
class Rampa:
    slug: str
    cap: int
    degrau: int
    dias_ativos: int
    qualidade: str | None
    motivo: str


def _qualidade_meta(slug: str) -> str | None:
    """Qualidade atual da linha na Meta. None = não deu pra saber."""
    agora = time.monotonic()
    hit = _cache.get(slug)
    if hit and agora - hit[0] < _TTL_S:
        return hit[1]
    try:
        import requests

        from app.services import cloud_lines as cl

        linha = cl.line_by_slug(slug)
        token = cl.token_for(linha) if linha else None
        if not linha or not token:
            return None
        r = requests.get(
            f"https://graph.facebook.com/v21.0/{linha.phone_id}",
            params={"fields": "quality_rating", "access_token": token},
            timeout=15,
        )
        q = (r.json() or {}).get("quality_rating")
        q = str(q).upper() if q else None
    except Exception as e:  # noqa: BLE001
        log.warning("rampa_qualidade_indisponivel", slug=slug, erro=str(e)[:120])
        return None
    _cache[slug] = (agora, q)
    return q


def _dias_ativos(session, corretora_id: int) -> int:
    """Dias DISTINTOS em que a linha realmente despachou (fuso SP).

    Conta só evento que saiu (`sent=true`) — dia de tick sem envio não conta
    como aquecimento.
    """
    try:
        n = session.execute(
            sql_text(
                """
                SELECT count(DISTINCT (ce.at AT TIME ZONE 'America/Sao_Paulo')::date)
                  FROM cadencia_eventos ce
                  JOIN cadencia_estado est ON est.id = ce.cadencia_id
                 WHERE est.corretora_id = :cid
                   AND ce.tipo = 'mensagem'
                   AND ce.payload->>'sent' = 'true'
                """
            ),
            {"cid": int(corretora_id)},
        ).scalar()
        return int(n or 0)
    except Exception as e:  # noqa: BLE001
        log.warning("rampa_dias_ativos_falhou", erro=str(e)[:120])
        return 0


def cap_de_hoje(session, slug: str, corretora_id: int,
                pedido: int | None = None) -> Rampa:
    """Quanto esta linha pode mandar hoje, dado o aquecimento e a qualidade.

    `pedido` é o limite por dia gravado no painel. Só importa quando passa do
    primeiro degrau: aí a rampa sobe até ele (no máximo `DEGRAU_MAXIMO`) SE a
    qualidade for GREEN. Sem `pedido` o resultado é o de sempre.

    O `cap` devolvido é o TETO da rampa; quem chama ainda aplica o menor entre
    ele e o pedido (um pedido de 40 continua valendo 40).
    """
    qualidade = _qualidade_meta(slug)

    # Qualidade ruim: para tudo. Não é degrau — é freio.
    if qualidade in _PARA:
        log.warning("rampa_linha_travada_qualidade", slug=slug, qualidade=qualidade)
        return Rampa(slug, 0, 0, 0, qualidade, f"qualidade {qualidade} — linha parada")

    dias = _dias_ativos(session, corretora_id)

    # Primeiro degrau: é o que vale sem pedido, e o que vale sem GREEN.
    degrau = 0
    cap = DEGRAU_BASE
    quer_subir = pedido is not None and pedido > DEGRAU_BASE

    # Sem leitura da qualidade → NÃO ENVIA (05/10/2026).
    #
    # Até aqui "não consegui ler" valia o primeiro degrau (80 por dia). O furo:
    # a linha em RED, a Graph fora do ar por um minuto, e o teto saltava de 0
    # pra 80 — o freio de qualidade soltava justamente quando não dava pra ver
    # a qualidade. Na dúvida não manda; a próxima volta (3 minutos) lê de novo.
    #
    # ATENÇÃO: isto é a leitura que FALHOU (None). `UNKNOWN` devolvido pela
    # Meta é leitura boa — é o que ela responde pra número novo, sem histórico
    # — e segue no primeiro degrau, mais abaixo. Travar em UNKNOWN impediria
    # um número novo de sair pra sempre: a reputação só nasce enviando.
    if qualidade is None:
        log.warning("rampa_sem_leitura_de_qualidade_nao_envia", slug=slug)
        return Rampa(slug, 0, 0, dias, None,
                     "não consegui ler a qualidade da linha na Meta — sem "
                     "leitura não envia")

    # YELLOW: congela um degrau abaixo do que o aquecimento daria.
    #
    # COM UM DEGRAU SÓ (config de 12/08, 300 direto) não existe "abaixo" — e sem
    # este piso o freio virava enfeite: a linha seguiria mandando 300 com a
    # qualidade caindo, que é exatamente como se perde um número. Cai pro piso
    # histórico seguro (50/dia) e só volta a 300 quando a Meta disser GREEN.
    if qualidade in _CONGELA:
        if degrau > 0:
            degrau -= 1
            cap = DEGRAUS[degrau][1]
        else:
            cap = min(CAP_AMARELO_MINIMO, cap)
        log.info("rampa_congelada_amarelo", slug=slug, degrau=degrau, cap=cap)
        return Rampa(slug, cap, degrau, dias, qualidade, "qualidade YELLOW — congelado")

    # Segundo degrau: pedido explícito E qualidade GREEN. Qualquer outra coisa
    # que não seja GREEN (UNKNOWN de número novo, valor que a Meta inventar
    # amanhã) fica no primeiro — a lista de quem sobe é fechada, não a de quem
    # não sobe.
    if quer_subir:
        if qualidade == _OK:
            cap = min(int(pedido), DEGRAU_MAXIMO)
            return Rampa(slug, cap, 1, dias, qualidade,
                         f"degrau 2/{len(DEGRAUS)} — {cap}/dia pedido no painel, "
                         "qualidade GREEN")
        return Rampa(slug, cap, 0, dias, qualidade,
                     f"qualidade {qualidade} — acima de {DEGRAU_BASE} só com GREEN "
                     f"confirmado na Meta; o pedido de {pedido} não vale hoje")

    return Rampa(slug, cap, degrau, dias, qualidade, f"degrau {degrau + 1}/{len(DEGRAUS)}")
