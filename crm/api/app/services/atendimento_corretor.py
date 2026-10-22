"""
Atendimento da linha da Corretor (0000-0000) — quem responde o carrossel é atendido.

Fecha o circuito que a Dona desenhou em 30/07:

    carrossel dispara ──► lead responde ──► PESSOA atende (qualifica até ter
    renda) ──► conseguiu interação ──► lead entra no FUNIL DE VENDAS, etapa
    "Novo" (o "Entrou" do Kommo), responsável CORRETORA_A.

A Pessoa já existia inteira (`app/agents/pessoa/`) mas nunca tinha sido plugada
em linha nenhuma — este módulo é o fio que faltava.

⚠️ Fim de linha (Dona 02/08/2026): quando a Pessoa chama `registrar_qualificacao`
ela abre a conversa na LINHA DA CORRETOR e o atendimento migra pra lá. Daí em diante
`_handoff_feito` cala esta linha — dois atendimentos paralelos em dois números
é pior que nenhum.

⚠️ Janela de 24h: a Pessoa responde com TEXTO LIVRE, o que a Meta só permite
dentro das 24h depois da mensagem do lead. Como ela só fala em resposta a uma
mensagem que acabou de chegar, a janela está sempre aberta. Nunca chame isso
fora do inbound.

⚠️ O que conta como "interação" (regra da Dona, traduzida): não é o lead
responder o disparo — é ele CONTINUAR depois que a Pessoa falou. Um "para de me
mandar mensagem" é resposta, não interação. Por isso a promoção só acontece a
partir da 2ª mensagem do lead, quando já houve troca de verdade.
"""
from __future__ import annotations

import json
import re as _re_mod
import threading
from typing import Any

import structlog
from sqlalchemy import text as sql_text

from app.config import settings
from app.services import carrossel_corretor as cb

log = structlog.get_logger()

# Destino da promoção: EM ATENDIMENTO, não "Novo" (12/08).
#
# "Entrou" é como se chamava no Kommo, e virou "Novo" (6) aqui. Mas o lead que
# a Pessoa promove NÃO é novo: a mensagem de abertura JÁ saiu pela linha da Pessoa12
# no mesmo instante. Caindo em "Novo" ele aparece como alguém pra abordar — e a
# Pessoa12 aborda de novo quem ela já abordou, do zero, na frente do lead.
#
# "Em atendimento" (7) descreve o que de fato está acontecendo: conversa
# começada, esperando a resposta dele.
FUNIL_VENDAS = "principal"
STAGE_ENTROU = 7          # pipeline_stages: (7, 'Em atendimento', principal)
# Lead que chega pelo SITE é outra história: ninguém falou com ele ainda, então
# ele É novo. Só a promoção da Pessoa vai pra "Em atendimento".
STAGE_NOVO = 6            # pipeline_stages: (6, 'Novo', principal)
CORRETORA_CORRETORA_A = 3

# Mensagens do lead nesta conversa a partir das quais consideramos que a Pessoa
# "conseguiu interação". 1 = respondeu o disparo. 2 = voltou a falar DEPOIS da
# Pessoa — aí houve conversa.
MSGS_PRA_PROMOVER = 2

# Um lock por telefone: duas mensagens em sequência rápida não podem gerar duas
# rodadas da Pessoa (o lead receberia duas respostas desencontradas).
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_do(telefone: str) -> threading.Lock:
    with _locks_guard:
        lk = _locks.get(telefone)
        if lk is None:
            lk = _locks[telefone] = threading.Lock()
        return lk


def atender(telefone: str, texto: str | None, nome: str | None = None, *,
            recusa_decidida: bool = False,
            interesse_decidido: bool = False) -> dict[str, Any]:
    """Roda a Pessoa pro lead e responde pela linha da Corretor.

    `recusa_decidida` / `interesse_decidido` (05/10/2026): a regra em código
    do remarketing (`remarketing_resposta`) já classificou ESTA mensagem como
    resposta ao disparo. Há frases que só ela lê, porque só ela sabe que é
    resposta direta — "Não", "Achei", "Olá, estou sim". O atendimento segue a
    decisão em vez de tratar como conversa aberta. Quem chama sem os
    argumentos (todo o resto do sistema) segue byte a byte como era.

    Best-effort: nunca levanta. Erro aqui é lead falando sozinho — grave, mas
    não pode derrubar o webhook (que também grava a mensagem na thread).
    """
    res: dict[str, Any] = {"atendeu": False, "promoveu": False}
    if not settings.atendimento_corretor_ativo:
        res["motivo"] = "desligado"
        return res
    if not (texto or "").strip():
        res["motivo"] = "sem_texto"       # áudio/imagem: humano assume
        return res
    # Trava própria de opt-out — NÃO confia só no caller. Hoje a única barreira
    # é o `saida != "opt_out"` no webhook, e ela depende de o banco estar de pé.
    # `is_opt_out` é texto puro: funciona mesmo com o Postgres fora. Responder
    # quem escreveu "PARAR" é a regra inviolável mais cara de quebrar.
    from app.services.opt_out import is_opt_out

    if is_opt_out(texto):
        res["motivo"] = "opt_out"
        log.info("atendimento_corretor_opt_out_calado", tel=telefone[-4:])
        return res
    if _e_so_midia(texto):
        # O webhook entrega mídia como marcador ("[audio]", "[image]"). Sem o
        # conteúdo, a Pessoa RESPONDE MESMO ASSIM e inventa contexto — em 30/07
        # ela recebeu uma foto e emendou "vi que você tem interesse em imóveis".
        # Melhor calar e deixar a pessoa assumir.
        res["motivo"] = "midia"
        log.info("atendimento_corretor_midia_ignorada", marcador=texto.strip()[:20])
        return res
    if _e_da_equipe(telefone):
        res["motivo"] = "equipe"
        log.warning("atendimento_corretor_numero_da_equipe", tel=telefone[-4:])
        return res
    if not _e_da_roda(telefone) and _nos_abordamos_na_mao(telefone):
        # O 0000 tem TRÊS usos ao mesmo tempo (Dona, 31/07):
        #   1. carrossel de remarketing  → é da Pessoa
        #   2. lead que chega do site    → é da Pessoa
        #   3. a Pessoa12 buscando imóvel/parceiro em outra imobiliária → NÃO é
        #
        # O que separa o 3 dos outros dois é quem falou primeiro: na busca da
        # Pessoa12, NÓS abordamos. Hoje saíram 113 mensagens humanas contra 36 da
        # Pessoa, e o robô entrou como "Pessoa" em conversas que a Pessoa3 tinha
        # aberto como "Pessoa3" — 5 acabaram apagadas por alguém da equipe.
        res["motivo"] = "conversa_da_corretor"
        log.info("atendimento_corretor_conversa_da_corretor", tel=telefone[-4:])
        return res
    # Humano assumiu ESTA conversa (depois do disparo que a abriu)? A Pessoa
    # cala e não volta — Dona 31/07: "assim que eu enviar mensagem, a Corretor para".
    if _humano_falando(telefone, desde=_inicio_da_conversa(telefone)):
        res["motivo"] = "humano_falando"
        log.info("atendimento_corretor_humano_no_comando", tel=telefone[-4:])
        return res
    # Já passou pra Pessoa12? O trabalho da Pessoa acabou (Dona 02/08). A conversa
    # continua na linha da Pessoa12 (2591) — se ela seguisse respondendo aqui, o lead
    # teria dois atendimentos paralelos em dois números.
    if _handoff_feito(telefone):
        res["motivo"] = "handoff_feito"
        log.info("atendimento_corretor_handoff_ja_feito", tel=telefone[-4:])
        return res
    # Já foi pra corretora (adendo da orquestradora, 05/10/2026)? Transferido
    # pela roleta do remarketing ou na mão, a conversa é dela, pela linha dela.
    # Mesma regra do handoff rico logo acima: a Pessoa aqui e "vamos continuar
    # por aqui" no número da corretora são dois atendimentos em dois números.
    # A mensagem já foi gravada pelo webhook e aparece no card de quem recebeu.
    if _transferido_pra_corretora(telefone):
        res["motivo"] = "transferido"
        log.info("atendimento_corretor_transferido_calada", tel=telefone[-4:])
        return res

    lock = _lock_do(telefone)
    if not lock.acquire(blocking=False):
        res["motivo"] = "ja_atendendo"
        return res
    try:
        from app.services.conversations import append_message, load_history

        historico = load_history(telefone, max_messages=20, max_age_hours=48)
        append_message(telefone, "lead", None, "user", texto)

        # Promoção ANTES de responder: se a Pessoa falhar, o lead já está com a
        # Pessoa3 — melhor um lead promovido sem resposta do que um lead
        # engajado que ninguém vê.
        #
        # Mas quem já disse "não" NUNCA é promovido, por mais que continue
        # escrevendo: educação não é interesse (caso Fulano, 31/07).
        # Olha a conversa inteira, não só esta mensagem — o "não" costuma vir
        # na PRIMEIRA e a promoção dispara na segunda.
        # `load_history` devolve a chave `text` — ler "content" aqui fazia
        # `falas` virar [msg_atual, None, None, ...]: como os detectores aceitam
        # None e devolvem False sem levantar, TODA trava que depende do
        # histórico (parar / quer vender / recusa) enxergava só a mensagem da
        # vez, em silêncio. Quem disse "não obrigado" e mandou um "Ok" educado
        # depois era promovido. (Astra 07/09, confirmado 08/09.)
        falas = [texto] + [
            m.get("text") for m in historico if m.get("role") == "user"
        ]

        # 1) PEDIU PRA SAIR — o mais forte de todos. Opt-out duro e cala a boca:
        # não manda nem o pedido de indicação. Quem mandou parar não quer ouvir
        # mais nada, e insistir aqui é o caminho da denúncia e do bloqueio.
        if any(_pediu_parar(f) for f in falas):
            res["motivo"] = "pediu_parar"
            _marcar_opt_out(telefone)
            log.info("corretor_opt_out_pedido", tel=telefone[-4:])
            return res

        # 2) QUER VENDER — vai pra Pessoa12 (Dona, 12/08). É captação: chegou pela
        # base de compradores mas tem imóvel pra vender, e isso é carteira nova.
        # Promove com a marca de VENDEDOR pra não cair no atendimento de compra.
        if any(_quer_vender(f) for f in falas):
            res["motivo"] = "quer_vender"
            res["promoveu"] = promover_para_vendas(telefone, nome, origem="vendedor")
            log.info("corretor_lead_vendedor", tel=telefone[-4:])
            # segue pro atendimento normal: a Pessoa responde e a Pessoa12 assume

        # "JÁ COMPREI, MAS..." NÃO É RECUSA (12/08). O Renato disse "já comprei,
        # obrigado" e na mensagem seguinte ofereceu permuta: casa dele por
        # chácara ou apartamento em outra região. Lead cheio. Encerrar por causa
        # da primeira frase perde a segunda.
        #
        # A regra: recusa só encerra se NADA depois dela mostrar interesse.
        # Quem só queria sair não volta pra falar de imóvel.
        recusou = recusa_decidida or any(_e_recusa_branda(f) for f in falas)
        # O interesse que salva a recusa tem que vir de OUTRA fala: "não tenho
        # interesse" casa `\binteress` na regex de interesse e se anulava
        # sozinho (achado 05/09 ao escrever o handoff automático).
        if recusou and any(
            (_tem_interesse(f) and not _e_recusa_branda(f)) or _quer_vender(f)
            for f in falas
        ):
            recusou = False
            log.info("corretor_recusa_seguida_de_interesse", tel=telefone[-4:])
        if recusou and not res.get("motivo") == "quer_vender":
            res["motivo"] = "recusou"
            log.info("atendimento_corretor_nao_promove_recusa", tel=telefone[-4:])
            # ENCERRA DIREITO (Dona, 11/08): quem não vai continuar não fica em
            # limbo. Pede indicação — é o último valor que essa conversa pode
            # dar — e vai pra Venda perdida. Antes o lead só "não era
            # promovido": ficava na base, voltava pra fila em 30 dias e recebia
            # de novo. Foi assim que 15 de 15 que recusaram seguiram recebendo.
            desfecho = encerrar_com_indicacao(telefone, nome)
            res["encerrou"] = desfecho
            return res

        # HANDOFF AUTOMÁTICO (05/09): interesse real → card da corretora humana
        # JÁ NA 1ª MENSAGEM, sem esperar a 2ª. Em 05/09 os 40 leads que
        # responderam na semana estavam 100% com a Corretor — "Respondeu" era ponto
        # final. A senhora comprando pra filha ("pode mandar pra mim") ficou 3
        # dias sem resposta porque só escreveu uma vez. Critério explícito e
        # auditável em `handoff_remarketing.avaliar`; sem WhatsApp; idempotente
        # (não mexe em quem já saiu da Corretor nem em transferência manual).
        # Quem quer vender já foi promovido acima — não passa duas vezes.
        if not res.get("promoveu"):
            from app.services import handoff_remarketing

            res["handoff"] = handoff_remarketing.processar(
                telefone, falas, nome, interesse_decidido=interesse_decidido)
            if (res["handoff"] or {}).get("transferiu"):
                # Transferiu agora (modo "só interesse"): a partir daqui quem
                # fala é a corretora, pela linha dela. A Pessoa não responde.
                res["motivo"] = "transferido"
                return res

        if _conta_msgs_do_lead(historico) + 1 >= MSGS_PRA_PROMOVER:
            # PROMOVER EXIGE SINAL DE INTERESSE (12/08). Antes bastava a pessoa
            # escrever duas vezes sem que a gente RECONHECESSE uma recusa — e aí
            # frase que o sistema não entendia virava "sim". Foi assim que
            # "Não estou no momento muito obrigada" e "não penso em adquirir
            # imóvel não" viraram lead novo na fila da Pessoa3.
            #
            # A inversão é o ponto: frase desconhecida agora vira "não sei", e
            # "não sei" não promove. O lead continua visível no painel — só não
            # entra na fila de quem está trabalhando interesse de verdade.
            if any(_tem_interesse(f) for f in falas) or _engajou(falas):
                res["promoveu"] = promover_para_vendas(telefone, nome)
            else:
                res["motivo"] = "sem_sinal_de_interesse"
                log.info("corretor_nao_promove_sem_sinal", tel=telefone[-4:])

        resposta = _rodar_pessoa(telefone, texto, nome, historico)
        if not resposta:
            res["motivo"] = "pessoa_sem_resposta"
            return res

        _responder(telefone, resposta)
        append_message(telefone, "pessoa", None, "assistant", resposta)
        res.update(atendeu=True, resposta=resposta[:160])
        # Dona 06/09: dump cru no Copiloto depois de ~5 falas na thread.
        from app.services.aviso_resposta_disparo import avisar_em_background

        avisar_em_background(telefone)
        return res
    except Exception as e:  # noqa: BLE001
        log.exception("atendimento_corretor_erro", erro=str(e)[:160])
        res["motivo"] = "erro"
        # A mensagem do lead não some por erro nosso: vai pra fila de retomada
        # (best-effort — `enfileirar` nunca levanta).
        from app.services import pessoa_retomada

        pessoa_retomada.enfileirar(telefone, texto, nome, erro=f"{type(e).__name__}: {e}"[:300])
        return res
    finally:
        lock.release()


_MARCADORES_MIDIA = frozenset({
    "[audio]", "[image]", "[video]", "[document]", "[sticker]",
    "[imagem]", "[áudio]", "[vídeo]", "[documento]", "[figurinha]",
    "[location]", "[contacts]", "[mídia]", "[midia]",
})


_RE_MARCADOR = _re_mod.compile(r"^\[[^\]]*\]$")


def _e_so_midia(texto: str) -> bool:
    """True quando a "mensagem" é só um marcador do webhook, não texto humano.

    A lista fixa não bastava: ela casa por igualdade, então marcador COM
    conteúdo passava batido. Foi o que aconteceu em 31/07 com o lead Lima
    Santos — ele reagiu com ❤️ à despedida da Pessoa, virou "[reaction]", isso
    contou como 2ª mensagem e ele foi promovido pra Pessoa3 como lead quente,
    logo depois de ter dito que NÃO tinha interesse.

    Reação, figurinha, contato compartilhado: nada disso é conversa.
    """
    limpo = texto.strip().lower()
    return limpo in _MARCADORES_MIDIA or bool(_RE_MARCADOR.match(limpo))


# Recusa BRANDA: o lead diz que não tem interesse agora, educadamente. NÃO é
# opt-out (não pede pra sair da lista, então continua elegível pro carrossel
# daqui a 30 dias — quem decide isso é o `opt_out.is_opt_out`, e ele está certo
# em não marcar isto). Mas também não é lead pra entregar pra corretora: mandar
# um "não obrigado" pro funil de vendas queima o lead e o tempo da corretora.
# Errar aqui é barato: a conversa continua visível no painel, só não é promovida.
# Ampliada em 12/08 depois de medir o dia: das 20 respostas, 8 recusas passaram
# batido e 2 viraram lead promovido. O que escapava eram as formas naturais de
# dizer não — "já fechei a compra", "já arrumei", "não penso em adquirir".
_RE_RECUSA_BRANDA = _re_mod.compile(
    r"n[ãa]o\s+(?:tenho|temos)\s+(?:mais\s+)?interesse"
    r"|sem\s+interesse"
    r"|(?:no\s+momento|agora|hoje|por\s+enquanto|por\s+hora)[,\s]*n[ãa]o\b"
    r"|n[ãa]o[,\s]+(?:no\s+momento|por\s+enquanto|por\s+hora|obrigad)"
    r"|j[áa]\s+(?:comprei|consegui|resolvi|aluguei|arrumei|achei|encontrei|fechei|adquiri)"
    r"|j[áa]\s+(?:fechei|fiz)\s+(?:a\s+)?(?:compra|neg[óo]cio)"
    r"|desisti\s+(?:de|da|do)?\s*(?:comprar|procurar)?"
    r"|n[ãa]o\s+(?:penso|pretendo|quero|vou)\s+(?:em\s+)?(?:adquirir|comprar|procurar)"
    r"|n[ãa]o\s+estou\s+(?:mais\s+)?(?:procurando|buscando|interessad)"
    r"|n[ãa]o\s+estou\s+no\s+momento"
    r"|n[ãa]o\s+(?:preciso|necessito)\s+(?:mais)?",
    _re_mod.IGNORECASE,
)

# PEDIDO DE SAÍDA — diferente de recusa. Aqui a pessoa não está dizendo "agora
# não", está mandando parar. Vira opt-out DURO: sai de tudo, pra sempre, e não
# recebe nem o pedido de indicação (insistir com quem mandou parar é o caminho
# do bloqueio e da reclamação).
_RE_PEDIU_PARAR = _re_mod.compile(
    r"parar?\s+de\s+receber|para\s+de\s+(?:me\s+)?(?:mandar|enviar)"
    r"|me\s+(?:tire|tira|remove|remova|exclu)"
    r"|sair\s+d[ao]\s+lista|descadastr|cancelar?\s+(?:o\s+)?envio"
    r"|n[ãa]o\s+(?:me\s+)?(?:mande|manda|envie|envia)\s+mais"
    r"|\bpare\b|\bstop\b",
    _re_mod.IGNORECASE,
)

# QUEM QUER VENDER — vai pra Pessoa12 mesmo (Dona, 12/08): é CAPTAÇÃO, não descarte.
# "Estou vendendo meu apto" chegando pela Corretor é oportunidade de carteira, e a
# Pessoa12 sabe avaliar. Só não pode entrar como se fosse comprador.
_RE_QUER_VENDER = _re_mod.compile(
    r"(?:estou|to|tô)\s+vendendo|quero\s+vender|vender\s+(?:o\s+)?meu"
    r"|tenho\s+(?:um\s+)?(?:im[óo]vel|apto|apartamento|casa|terreno)\s+(?:pra|para)\s+vender"
    r"|gostaria\s+de\s+vender|preciso\s+vender|coloca[r]?\s+(?:[àa]\s+)?venda",
    _re_mod.IGNORECASE,
)


# SINAL DE INTERESSE — o que autoriza mandar pra fila de vendas. Cobre o que
# comprador de verdade escreve: diz o que procura, responde a triagem, pergunta
# preço/local, ou pede pra ver. Cumprimento solto ("bom dia", "oi") NÃO é
# interesse: é educação, e educação virando lead é o que enche a fila da
# corretora de conversa que não vai a lugar nenhum.
_RE_INTERESSE = _re_mod.compile(
    r"\bmorar\b|\binvestir\b|\binvestimento\b"
    r"|\bquero\b|\bprocuro\b|\bprocurando\b|\bbuscando\b|\binteress"
    r"|\bquanto\b|\bvalor\b|\bpre[çc]o\b|\bparcela\b|\bentrada\b|\bfinanciam"
    r"|\bdormit|\bquarto|\bsu[íi]te|\bapartamento\b|\bapto\b|\bcasa\b|\bterreno\b"
    r"|\bbairro\b|\bregi[ãa]o\b|\bzona\b|\bcondom[íi]nio\b"
    r"|\bvisita|\bconhecer\b|\bver\s+o\s+im[óo]vel|\bagendar\b"
    r"|\bdispon[íi]vel|\btem\s+(?:algum|alguma|casa|apto|apartamento)"
    r"|\bsim\b.*\b(?:quero|tenho|estou)\b|\bpode\s+(?:mandar|enviar|me\s+mandar)"
    # LOCALIZAÇÃO é interesse (Dona, 12/08). "Perto da prefeitura" é a pessoa
    # dizendo ONDE quer morar — mais concreto que "estou procurando". Ficou de
    # fora na 1ª versão porque eu só olhei verbo, e quem responde triagem
    # responde com LUGAR.
    r"|\bperto\s+d|\bpr[óo]xim|\bpr[ée]dio\b|\bprefeitura\b|\bcentro\b"
    r"|\bvila\b|\bjardim\b|\bjd\b|\bparque\b|\bpq\b|\bloteamento\b",
    _re_mod.IGNORECASE,
)


# ENGAJAMENTO por COMPORTAMENTO, não por palavra: quem manda 3+ mensagens e não
# recusa nenhuma vez está numa conversa, mesmo que use palavras que nenhuma
# regex prevê ("Sim", "Sim", "Perto da prefeitura"). Texto livre é infinito;
# insistência em responder, não.
MSGS_PRA_ENGAJAMENTO = 3


def _engajou(falas: list[str | None]) -> bool:
    reais = [f for f in falas if f and f.strip()]
    if len(reais) < MSGS_PRA_ENGAJAMENTO:
        return False
    return not any(_e_recusa_branda(f) or _pediu_parar(f) for f in reais)


def _tem_interesse(texto: str | None) -> bool:
    return bool(texto) and bool(_RE_INTERESSE.search(texto))


def _pediu_parar(texto: str | None) -> bool:
    return bool(texto) and bool(_RE_PEDIU_PARAR.search(texto))


def _quer_vender(texto: str | None) -> bool:
    return bool(texto) and bool(_RE_QUER_VENDER.search(texto))


def _e_recusa_branda(texto: str | None) -> bool:
    return bool(texto) and bool(_RE_RECUSA_BRANDA.search(texto))


ORIGEM_PESSOA = "pessoa"

# O que sai da linha COM esta marca é robô; sem ela, é gente (corretora
# respondendo pelo painel ou digitando no celular). Template também é robô:
# é o disparo do carrossel.
_SQL_HUMANO_FALOU = r"""
    SELECT 1 FROM mensagens_whatsapp
     WHERE from_corretora
       AND contato_telefone IS NOT NULL
       AND right(regexp_replace(contato_telefone, '\D', '', 'g'), 8) = :suf
       AND COALESCE(conteudo_metadata->>'origem', '') <> :origem_robo
       AND conteudo_metadata->>'template' IS NULL
       AND tipo_mensagem <> 'template'
       AND timestamp_evolution > :desde
     LIMIT 1
"""


def _humano_falando(telefone: str, desde=None) -> bool:
    """Alguém da casa já falou com essa pessoa depois de `desde`?

    Sem `desde`, usa a janela de dias do settings. O atendimento passa a data
    do último disparo: o que importa pra Pessoa calar é um humano ter assumido
    ESTA conversa, não ter falado com o contato meses atrás.

    Best-effort: banco fora → False (calar o atendimento de um lead real é pior
    que uma sobreposição eventual, mesmo critério do `_e_da_equipe`).
    """
    from datetime import datetime, timedelta, timezone as _tz

    from sqlalchemy import text as _t

    from app.db.session import SessionLocal

    suf = cb._so_digitos(telefone)[-8:]
    if not suf:
        return False
    if desde is None:
        desde = datetime.now(_tz.utc) - timedelta(
            days=settings.carrossel_pausa_conversa_humana_dias)
    try:
        with SessionLocal() as s:
            return bool(s.execute(
                _t(_SQL_HUMANO_FALOU),
                {"suf": suf, "origem_robo": ORIGEM_PESSOA, "desde": desde},
            ).first())
    except Exception as e:  # noqa: BLE001
        log.warning("humano_falando_indisponivel", erro=str(e)[:120])
        return False


# Quanto tempo um handoff da Pessoa "cala" a linha da Corretor. 30 dias = o intervalo
# do carrossel: se o contato voltar pra roda e receber disparo de novo, é lead
# novo e a Pessoa pode atender outra vez.
HANDOFF_SILENCIA_DIAS = 30

_SQL_HANDOFF_ATIVO = r"""
    SELECT 1 FROM lead_handoff_notes
     WHERE status = 'ativo'
       AND origem LIKE 'pessoa%'
       AND criado_em > now() - make_interval(days => :dias)
       AND right(regexp_replace(telefone, '\D', '', 'g'), 8) = :suf
     LIMIT 1
"""


def _handoff_feito(telefone: str) -> bool:
    """A Pessoa já entregou esse lead pra Pessoa3?

    A LeadHandoffNote (origem `pessoa` ou `pessoa_avaliacao`) é o marco: ela é
    escrita no mesmo instante em que o lead é promovido pro funil principal e a
    conversa é aberta na linha da Pessoa12. Depois disso a Pessoa não tem mais o que
    fazer no 0000.

    Best-effort igual ao `_humano_falando`: banco fora → False. Uma sobreposição
    eventual é ruim; deixar lead falando sozinho porque o Postgres piscou é pior.
    """
    from sqlalchemy import text as _t

    from app.db.session import SessionLocal

    suf = cb._so_digitos(telefone)[-8:]
    if not suf:
        return False
    try:
        with SessionLocal() as s:
            return bool(s.execute(
                _t(_SQL_HANDOFF_ATIVO),
                {"suf": suf, "dias": HANDOFF_SILENCIA_DIAS},
            ).first())
    except Exception as e:  # noqa: BLE001
        log.warning("handoff_feito_indisponivel", erro=str(e)[:120])
        return False


# Card que saiu da Corretor pra uma corretora (evento `transferencia` com `de` = a
# Corretor, automático ou pelo botão do painel) e continua fora dela. Só vale a
# transferência DESTA conversa: depois do último disparo do carrossel no card,
# ou nos últimos 30 dias quando não há disparo registrado. Devolvido pra Corretor, a
# Pessoa volta. (Não usa `_inicio_da_conversa`: o aviso que sai pela linha da
# corretora também é template e passaria a contar como "início".)
_SQL_TRANSFERIDO = r"""
    SELECT 1 FROM leads l
      JOIN lead_events e ON e.lead_id = l.id AND e.tipo = 'transferencia'
     WHERE right(regexp_replace(coalesce(l.telefone, ''), '\D', '', 'g'), 8) = :suf
       AND coalesce(l.owner_id, 0) <> :corretor
       AND (e.payload->>'de' = :corretor_txt OR e.payload->>'de_owner_id' = :corretor_txt)
       AND e.at > coalesce(
             (SELECT max(d.at) FROM lead_events d
               WHERE d.lead_id = l.id AND d.tipo = :disparo),
             now() - make_interval(days => :dias))
     LIMIT 1
"""


def _transferido_pra_corretora(telefone: str) -> bool:
    """Este contato já foi transferido da Corretor pra uma corretora nesta conversa?

    Best-effort igual ao `_handoff_feito`: banco fora → False.
    """
    from sqlalchemy import text as _t

    from app.db.session import SessionLocal

    suf = cb._so_digitos(telefone)[-8:]
    if not suf:
        return False
    try:
        with SessionLocal() as s:
            return bool(s.execute(
                _t(_SQL_TRANSFERIDO),
                {"suf": suf, "corretor": cb.CORRETORA_CORRETOR,
                 "corretor_txt": str(cb.CORRETORA_CORRETOR), "disparo": cb.EVENT_DISPARO,
                 "dias": HANDOFF_SILENCIA_DIAS},
            ).first())
    except Exception as e:  # noqa: BLE001
        log.warning("transferido_indisponivel", erro=str(e)[:120])
        return False


def _inicio_da_conversa(telefone: str):
    """Quando o carrossel abriu a conversa atual (último disparo). None = sem
    disparo registrado; aí o chamador cai na janela por dias."""
    from sqlalchemy import text as _t

    from app.db.session import SessionLocal

    suf = cb._so_digitos(telefone)[-8:]
    if not suf:
        return None
    try:
        with SessionLocal() as s:
            return s.execute(
                _t(r"""SELECT max(timestamp_evolution) FROM mensagens_whatsapp
                        WHERE from_corretora
                          AND (conteudo_metadata->>'template') IS NOT NULL
                          AND right(regexp_replace(contato_telefone,'\D','','g'),8) = :suf"""),
                {"suf": suf},
            ).scalar()
    except Exception:  # noqa: BLE001
        return None


_SQL_DA_RODA = r"""
    SELECT 1
      FROM leads l
      JOIN pipeline_stages ps ON ps.id = l.stage_id
     WHERE l.owner_id = :corretor
       AND ps.funil = 'remarketing'
       AND right(regexp_replace(l.telefone, '\D', '', 'g'), 8) = :suf
     LIMIT 1
"""

_SQL_TEVE_DISPARO = r"""
    SELECT 1
      FROM lead_events le
      JOIN leads l ON l.id = le.lead_id
     WHERE le.tipo = :ev
       AND right(regexp_replace(l.telefone, '\D', '', 'g'), 8) = :suf
     LIMIT 1
"""


def _e_da_roda(telefone: str) -> bool:
    """Este contato é do CARROSSEL, ou é conversa de humano nesta linha?

    Vale se o contato está no funil de remarketing da Corretor OU já recebeu disparo
    (quem responde sai pro funil de vendas e some do primeiro critério, mas
    segue sendo conversa da Pessoa).

    ⚠️ FAIL-CLOSED, ao contrário do `_e_da_equipe`. Aqui, na dúvida, a Pessoa
    CALA. O risco dos dois lados não é simétrico: não atender um lead custa uma
    espera que a Pessoa3 resolve — atender por cima dela custa o lead ouvindo
    dois nomes diferentes do mesmo número, que é cara de golpe.
    """
    from sqlalchemy import text as _t

    from app.db.session import SessionLocal

    suf = cb._so_digitos(telefone)[-8:]
    if not suf:
        return False
    try:
        with SessionLocal() as s:
            if s.execute(_t(_SQL_DA_RODA),
                         {"corretor": cb.CORRETORA_CORRETOR, "suf": suf}).first():
                return True
            return bool(s.execute(_t(_SQL_TEVE_DISPARO),
                                  {"ev": cb.EVENT_DISPARO, "suf": suf}).first())
    except Exception as e:  # noqa: BLE001
        log.warning("checagem_da_roda_falhou_calando", erro=str(e)[:120])
        return False


_SQL_QUEM_ABRIU = r"""
    SELECT from_corretora, conteudo_texto, conteudo_metadata->>'template' AS tmpl
      FROM mensagens_whatsapp
     WHERE contato_telefone IS NOT NULL
       AND right(regexp_replace(contato_telefone, '\D', '', 'g'), 8) = :suf
     ORDER BY recebido_em
     LIMIT 1
"""


def _nos_abordamos_na_mao(telefone: str) -> bool:
    """Fomos NÓS que puxamos conversa com essa pessoa, na mão?

    É o que separa a busca da Pessoa12 (ela procura imóvel/parceiro em outra
    imobiliária pelo 0000) de quem chega pra gente — lead do site, orgânico,
    quem responde o carrossel. Na busca da Pessoa12, a primeira mensagem é nossa e
    digitada; nas outras, ou a pessoa escreveu primeiro, ou a primeira nossa foi
    o template do carrossel (e aí o `_e_da_roda` já resolveu antes daqui).

    Sem histórico nenhum = contato novo escrevendo agora = NÃO é busca da Pessoa12.

    ⚠️ FAIL-CLOSED: erro devolve True (a Pessoa cala). Mesmo critério do
    `_e_da_roda` — não atender custa uma espera que a Pessoa12 resolve; atender por
    cima dela custa a credibilidade do número.
    """
    from sqlalchemy import text as _t

    from app.db.session import SessionLocal

    suf = cb._so_digitos(telefone)[-8:]
    if not suf:
        return True
    try:
        with SessionLocal() as s:
            linha = s.execute(_t(_SQL_QUEM_ABRIU), {"suf": suf}).first()
        if linha is None:
            return False          # ninguém falou ainda: é ela chegando agora
        nos, texto, tmpl = linha
        if not nos:
            return False          # a pessoa escreveu primeiro
        # Fomos nós — mas se foi template, foi o carrossel, não a Pessoa12.
        eh_template = bool(tmpl) or (texto or "").startswith("[reengajamento")
        return not eh_template
    except Exception as e:  # noqa: BLE001
        log.warning("checagem_quem_abriu_falhou_calando", erro=str(e)[:120])
        return True


def _e_da_equipe(telefone: str) -> bool:
    """Número de corretora nunca é atendido como lead. Best-effort: na dúvida
    (banco fora) NÃO bloqueia — calar atendimento de lead real é pior."""
    from sqlalchemy import text as _t

    from app.db.session import SessionLocal

    suf = cb._so_digitos(telefone)[-8:]
    if not suf:
        return False
    try:
        with SessionLocal() as s:
            return bool(s.execute(_t(
                r"""SELECT 1 FROM corretoras
                     WHERE telefone IS NOT NULL
                       AND right(regexp_replace(telefone, '\D', '', 'g'), 8) = :suf
                     LIMIT 1"""), {"suf": suf}).scalar())
    except Exception as e:  # noqa: BLE001
        log.warning("checagem_equipe_falhou", erro=str(e)[:120])
        return False


def _conta_msgs_do_lead(historico: list[dict[str, Any]]) -> int:
    return sum(1 for h in historico if h.get("role") == "user")


def _executar_pessoa(telefone: str, texto: str, nome: str | None,
                    historico: list[dict[str, Any]]) -> dict[str, Any]:
    """Roda a Pessoa e devolve {resposta, provider_falhou, erro} — sem tocar na
    fila. É o que o tick de retomada usa (ele já É a fila)."""
    from app.agents.pessoa.agent import AgentePessoa

    agente = AgentePessoa(phone=telefone, lead_nome=nome, history=historico)
    out = agente.run(texto)
    # `run` devolve {"response_text": ..., "tool_calls": [...]}. Nada de cair
    # num str(out) genérico: se a chave mudar, é melhor não responder do que
    # despejar o dict inteiro no WhatsApp do lead.
    if not isinstance(out, dict):
        log.error("pessoa_retorno_inesperado", tipo=type(out).__name__)
        return {"resposta": None, "provider_falhou": False, "erro": "retorno inesperado"}
    if out.get("provider_falhou"):
        return {"resposta": None, "provider_falhou": True,
                "erro": out.get("erro") or "provider indisponível"}
    resposta = (out.get("response_text") or "").strip() or None
    if resposta:
        resposta = _sem_pergunta_repetida(resposta, historico, telefone)
    return {"resposta": resposta, "provider_falhou": False, "erro": None}


def _ja_pediu_um_instante(historico: list[dict[str, Any]]) -> bool:
    """A última fala da Pessoa nesta conversa já foi o "probleminha técnico"?"""
    from app.agents.pessoa.agent import FRASE_PROBLEMINHA

    for h in reversed(historico):
        if h.get("role") == "assistant":
            return (h.get("text") or "").strip() == FRASE_PROBLEMINHA
    return False


def _rodar_pessoa(telefone: str, texto: str, nome: str | None,
                 historico: list[dict[str, Any]]) -> str | None:
    from app.agents.pessoa.agent import FRASE_PROBLEMINHA

    out = _executar_pessoa(telefone, texto, nome, historico)
    if out["provider_falhou"]:
        # NENHUM provider respondeu. O turno não morre aqui: vai pra fila de
        # retomada (pessoa_retomadas) e a Dona é avisada. A frase "probleminha
        # técnico" sai UMA vez por conversa — em 05/09 saiu 6x e nunca voltou.
        from app.services import pessoa_retomada

        pessoa_retomada.enfileirar(telefone, texto, nome, erro=out["erro"])
        if _ja_pediu_um_instante(historico):
            return None
        return FRASE_PROBLEMINHA
    return out["resposta"]


def _normalizar(frase: str) -> frozenset[str]:
    """Palavras que importam da frase — sem pontuação, emoji nem miudezas.

    "E como posso te chamar?" e "Como posso te chamar?" viram o mesmo conjunto.
    """
    limpo = _re_mod.sub(r"[^\w\s]", " ", frase.lower(), flags=_re_mod.UNICODE)
    return frozenset(p for p in limpo.split() if len(p) > 2 and p not in _VAZIAS)


_VAZIAS = frozenset({"que", "com", "para", "por", "uma", "seu", "sua", "você",
                     "voce", "the", "and", "aqui", "então", "entao", "tem"})


def _mesma_pergunta(a: str, b: str) -> bool:
    """Duas perguntas pedindo a mesma coisa.

    Mede CONTENÇÃO, não semelhança: a repetição costuma vir encurtada. A
    primeira foi "Pra te indicar as melhores opções, me conta: você está
    procurando pra morar ou investir?" e a segunda virou "Pra morar ou
    investir?" — quase nada em comum proporcionalmente, mas a curta está
    inteira dentro da longa. Jaccard dava 0.3 e deixava passar.
    """
    na, nb = _normalizar(a), _normalizar(b)
    if len(na) < 2 or len(nb) < 2:   # 1 palavra casa com qualquer coisa
        return False
    return len(na & nb) / min(len(na), len(nb)) >= 0.8


def _perguntas(texto: str) -> list[str]:
    return [f.strip() for f in _re_mod.split(r"(?<=[.!?😊🙂])\s+", texto)
            if "?" in f]


def _sem_pergunta_repetida(resposta: str, historico: list[dict[str, Any]],
                           telefone: str) -> str:
    """Corta pergunta que a Pessoa JÁ fez nesta conversa.

    O defeito medido em 31/07: ela trata a qualificação como formulário e
    reperguntou "como posso te chamar?" 5 vezes pro mesmo lead, ignorando o que
    ele dizia no meio. Depois de corrigir só o nome, ela passou a repetir
    "pra morar ou investir?" — mesmo defeito, outra frase. Por isso a trava é
    genérica, não uma lista de perguntas proibidas.

    Se o lead não respondeu, ele não quer responder. Insistir é o que faz a
    pessoa bloquear. A regra também está na SOUL; isto aqui é o determinístico,
    porque instrução o modelo fura.
    """
    ja_ditas = [h.get("text") or "" for h in historico
                if h.get("role") == "assistant"]
    if not ja_ditas:
        return resposta

    frases = [f.strip() for f in _re_mod.split(r"(?<=[.!?😊🙂])\s+", resposta)]
    manter, cortadas = [], []
    for f in frases:
        if not f:
            continue
        repetida = "?" in f and any(
            _mesma_pergunta(f, ant)
            for dita in ja_ditas for ant in _perguntas(dita)
        )
        (cortadas if repetida else manter).append(f)

    if not cortadas:
        return resposta

    limpo = " ".join(manter).strip()
    if not limpo:
        limpo = _AVANCO
    elif "?" not in limpo:
        # Só sobrou cumprimento: beco sem saída, o lead não tem o que responder.
        limpo = f"{limpo} {_AVANCO}"
    log.info("pessoa_pergunta_repetida_cortada", tel=telefone[-4:],
             n=len(cortadas), exemplo=cortadas[0][:60])
    return limpo


# A Pessoa trata "pegar o nome" como pré-requisito e trava: em 31/07 perguntou
# "como posso te chamar?" 5 vezes pro MESMO lead, ignorando o que ele dizia no
# meio ("Qual imóvel seria?"). A regra está na SOUL, mas instrução o modelo
# fura — esta trava é determinística.
_RE_PERGUNTA_NOME = _re_mod.compile(
    r"como (?:posso|devo) (?:te |lhe )?chamar"
    r"|qual (?:é |eh )?(?:o )?seu nome"
    r"|me diz(?:er)? seu nome"
    r"|(?:e )?como (?:é |eh )?seu nome",
    _re_mod.IGNORECASE,
)


def _repete_pergunta_do_nome(resposta: str,
                             historico: list[dict[str, Any]]) -> bool:
    """True se ela vai perguntar o nome DE NOVO — já perguntou nesta conversa."""
    if not _RE_PERGUNTA_NOME.search(resposta):
        return False
    return any(
        h.get("role") == "assistant" and _RE_PERGUNTA_NOME.search(h.get("text") or "")
        for h in historico
    )


def _tirar_pergunta_do_nome(resposta: str) -> str:
    """Corta só a frase da pergunta, preservando o resto do que ela escreveu.

    Se sobrar nada (a resposta era SÓ a pergunta), devolve uma abertura neutra
    que não trava a conversa — melhor seguir sem o nome do que insistir.
    """
    frases = [f.strip() for f in _re_mod.split(r"(?<=[.!?😊🙂])\s+", resposta)]
    sobrou = [f for f in frases if f and not _RE_PERGUNTA_NOME.search(f)]
    limpo = " ".join(sobrou).strip()
    if not limpo:
        return _AVANCO
    # Tirar a pergunta pode deixar só um cumprimento ("Boa tarde! 😊"), que é
    # beco sem saída: o lead não tem o que responder e a conversa morre ali.
    # Toda mensagem tem que terminar com um avanço.
    if "?" not in limpo:
        limpo = f"{limpo} {_AVANCO}"
    return limpo


# Sem emoji: era a frase de escape do fluxo e aparecia em TODA conversa, então
# virava o emoji mais repetido de todos (Dona 01/08: "está muito cara de IA").
_AVANCO = "Me conta o que você está procurando"


# ── Ritmo humano ─────────────────────────────────────────────────────────────
# Responder em 5s, sempre, é um dos tells mais fortes de robô — corretora real
# demora, some, volta. O atraso é proporcional ao TAMANHO da resposta (quem
# escreve mais, digita mais tempo) com uma variação pseudo-aleatória por
# telefone+texto, pra não virar um padrão constante e previsível.
_ATRASO_MIN_S = 12.0
_ATRASO_MAX_S = 55.0


def _atraso_humano(telefone: str, texto: str) -> float:
    """Segundos de espera antes de responder. Determinístico por (tel, texto) —
    testável, e sem Math.random espalhado pelo fluxo."""
    base = _ATRASO_MIN_S + min(len(texto), 400) * 0.06   # ~24s numa resposta média
    # jitter estável de ±35% a partir do hash do par (tel, texto)
    h = abs(hash((telefone[-4:], texto[:60]))) % 1000 / 1000.0   # 0.0–1.0
    fator = 0.65 + h * 0.7                                       # 0.65–1.35
    return max(_ATRASO_MIN_S, min(_ATRASO_MAX_S, base * fator))


def _responder(telefone: str, texto: str) -> None:
    """Manda pela linha DA CORRETOR — não pela da Pessoa12.

    O lead recebeu o disparo do 0000; resposta saindo de outro número faz ele
    achar que é golpe.
    """
    import os as _os
    import time as _time

    from app.api import cloud_corretora_a as corretor
    from app.services import cloud_lines as cl

    linha = cl.line_by_slug(cb.SLUG_CORRETOR)
    if linha is None:
        raise RuntimeError("linha 'corretor' ausente do registry — não respondi")

    # Espera antes de mandar (ver `_atraso_humano`). Sob pytest NUNCA dorme —
    # 55s por teste inviabilizaria a suíte.
    if not _os.environ.get("PYTEST_CURRENT_TEST"):
        espera = _atraso_humano(telefone, texto)
        log.info("pessoa_atraso_humano", tel=telefone[-4:], segundos=round(espera, 1))
        _time.sleep(espera)

    corretor.enviar_cloud(telefone, texto, linha=linha,
                    corretora_id=cb.CORRETORA_CORRETOR,
                    instance=cl.instance_do_slug(cb.SLUG_CORRETOR),
                    origem=ORIGEM_PESSOA)


# Prêmio de indicação — o mesmo que a Pessoa10 já usa na linha dela (53 mensagens
# em 60 dias). Vive aqui como constante pra não haver duas promessas de valor
# diferente saindo da casa.
PREMIO_INDICACAO = 500
STAGE_VENDA_PERDIDA = 13

_COPY_INDICACAO = (
    "Entendi, {nome}! Sem problema nenhum 😊\n\n"
    "Só uma última coisa antes de eu te deixar em paz: se você conhecer alguém "
    "procurando imóvel, me indica? Se a pessoa fechar com a gente, "
    "você ganha R$ {premio}.\n\n"
    "E se um dia voltar a procurar, é só me chamar aqui. Boa sorte! 🍀"
)


def encerrar_com_indicacao(telefone: str, nome: str | None = None) -> dict[str, Any]:
    """Fecha a conversa de quem disse que não vai continuar.

    Faz três coisas, nesta ordem: manda o pedido de indicação, move pra Venda
    perdida e tira da fila do carrossel. A ordem importa — se o envio falhar, o
    lead ainda sai da fila, que é o que evita reabordar quem já disse não.
    """
    # Importados aqui dentro como no resto do módulo (evita ciclo). Faltavam:
    # o `except` amplo abaixo transformava o NameError num warning silencioso, e
    # como `_responder` roda ANTES, o lead recebia os R$500 sem ficar registrado
    # — sem registro não há idempotência que impeça o reenvio.
    from app.db.session import SessionLocal
    from app.services.conversations import append_message

    out: dict[str, Any] = {"indicacao": False, "perdido": False}
    primeiro = (nome or "").strip().split()
    texto = _COPY_INDICACAO.format(
        nome=primeiro[0] if primeiro else "tudo bem",
        premio=PREMIO_INDICACAO,
    )
    try:
        _responder(telefone, texto)
        append_message(telefone, "pessoa", None, "assistant", texto)
        out["indicacao"] = True
    except Exception as e:  # noqa: BLE001 — mover pra perdido é o que não pode falhar
        log.warning("indicacao_falhou", tel=telefone[-4:], erro=str(e)[:120])

    suf = "".join(c for c in (telefone or "") if c.isdigit())[-8:]
    try:
        with SessionLocal() as s:
            r = s.execute(
                sql_text(
                    r"""UPDATE leads SET stage_id = :perdido, updated_at = now()
                         WHERE tenant_id = 1
                           AND right(regexp_replace(coalesce(telefone,''), '\D', '', 'g'), 8) = :suf
                           AND stage_id <> :perdido"""
                ),
                {"perdido": STAGE_VENDA_PERDIDA, "suf": suf},
            )
            # Sai da cadência também: recusa vale pros dois motores.
            s.execute(
                sql_text(
                    r"""UPDATE cadencia_estado SET status = 'ja_encontrou', atualizado_em = now()
                         WHERE right(regexp_replace(telefone, '\D', '', 'g'), 8) = :suf
                           AND status NOT IN ('opt_out', 'ja_encontrou')"""
                ),
                {"suf": suf},
            )
            s.commit()
        out["perdido"] = bool(r.rowcount)
        log.info("corretor_encerrou_com_indicacao", tel=telefone[-4:], **out)
    except Exception as e:  # noqa: BLE001
        log.warning("mover_perdido_falhou", tel=telefone[-4:], erro=str(e)[:120])
    return out


def marcar_opt_out(telefone: str) -> None:
    """Opt-out DURO: sai da cadência e do carrossel, pra sempre.

    Usado só quando a pessoa PEDE pra parar (diferente de "agora não"). Erro
    aqui não pode derrubar o atendimento — mas também não pode passar calado,
    porque não marcar significa continuar mandando pra quem mandou parar.
    """
    from app.db.session import SessionLocal

    suf = "".join(c for c in (telefone or "") if c.isdigit())[-8:]
    if not suf:
        return
    try:
        with SessionLocal() as s:
            s.execute(
                sql_text(
                    r"""UPDATE cadencia_estado SET status='opt_out', atualizado_em=now()
                         WHERE right(regexp_replace(telefone,'\D','','g'),8) = :suf
                           AND status <> 'opt_out'"""
                ), {"suf": suf},
            )
            s.execute(
                sql_text(
                    r"""INSERT INTO cadencia_estado (tenant_id, telefone, status, toque_atual,
                                                     criado_em, atualizado_em)
                        SELECT 1, :tel, 'opt_out', 0, now(), now()
                         WHERE NOT EXISTS (SELECT 1 FROM cadencia_estado
                            WHERE right(regexp_replace(telefone,'\D','','g'),8) = :suf)"""
                ), {"tel": telefone, "suf": suf},
            )
            s.commit()
    except Exception as e:  # noqa: BLE001
        log.warning("marcar_opt_out_falhou", tel=telefone[-4:], erro=str(e)[:120])


# Nome antigo — chamadores internos e o script de handoff de 19/08 usam este.
_marcar_opt_out = marcar_opt_out


def promover_para_vendas(telefone: str, nome: str | None = None,
                         tenant_id: int = 1, origem: str | None = None) -> bool:
    """Lead sai do funil de remarketing e entra no de VENDAS, com a Pessoa3.

    Idempotente: quem já está no funil principal não é mexido (senão um lead que
    avançou pra "Visita marcada" voltaria pra "Novo" a cada mensagem).

    O UPDATE também carimba `veio_remarketing_em` — a tag "remarketing" que a
    corretora lê no card (Dona 15/09/2026). Cabe AQUI e não no chamador porque
    é este WHERE que prova a procedência: só é atualizado lead que estava numa
    etapa do funil `remarketing`. `coalesce` mantém a PRIMEIRA data: quem já
    tinha a marca (handoff automático) não é recarimbado. O
    `_criar_lead_do_site` do fallback NÃO marca — lead que nasce ali nunca
    esteve no remarketing.
    """
    from app.db.session import SessionLocal

    suf = cb._so_digitos(telefone)[-8:]
    if not suf:
        return False
    try:
        with SessionLocal() as s:
            movidos = s.execute(
                sql_text(
                    r"""
                    UPDATE leads l
                       SET stage_id = :entrou, owner_id = :corretor, updated_at = now(),
                           veio_remarketing_em = coalesce(l.veio_remarketing_em, now())
                      FROM pipeline_stages ps
                     WHERE ps.id = l.stage_id
                       AND ps.funil = 'remarketing'
                       AND l.tenant_id = :t
                       AND right(regexp_replace(l.telefone, '\D', '', 'g'), 8) = :suf
                    RETURNING l.id
                    """
                ),
                {"entrou": STAGE_ENTROU, "corretor": CORRETORA_CORRETORA_A,
                 "t": tenant_id, "suf": suf},
            ).fetchall()
            if movidos:
                for (lead_id,) in movidos:
                    s.execute(
                        sql_text(
                            """
                            INSERT INTO lead_events (tenant_id, lead_id, tipo, payload, at)
                            VALUES (:t, :i, 'promovido_vendas', CAST(:p AS jsonb), now())
                            """
                        ),
                        {"t": tenant_id, "i": lead_id,
                         "p": '{"origem":"carrossel_corretor","para":"corretora_a"}'},
                    )
            if not movidos:
                movidos = _criar_lead_do_site(s, suf, telefone, nome, tenant_id)
            s.commit()
        if movidos:
            log.info("carrossel_lead_promovido", suf=suf, n=len(movidos),
                     etapa=STAGE_ENTROU, responsavel=CORRETORA_CORRETORA_A)
        return bool(movidos)
    except Exception as e:  # noqa: BLE001
        log.warning("carrossel_promocao_falhou", erro=str(e)[:140])
        return False


# Etapa de saída pra quem não é comprador. "Venda perdida" é o balde de saída do
# funil principal — não é derrota comercial, é só o lugar de quem não vai andar.
STAGE_NAO_E_LEAD = 13


def marcar_nao_e_lead(telefone: str, motivo: str | None = None,
                      tenant_id: int = 1) -> bool:
    """Tira da roda quem não é comprador (corretor, parceiro, ligou errado).

    Sem isso o contato volta pra Fila e recebe disparo de novo daqui a 30 dias,
    pra sempre. NÃO grava opt-out: a pessoa não pediu pra parar, e opt-out é
    registro do que ELA pediu — misturar as duas coisas apaga a diferença entre
    "não quis" e "não era pra ser".
    """
    from app.db.session import SessionLocal

    suf = cb._so_digitos(telefone)[-8:]
    if not suf:
        return False
    try:
        with SessionLocal() as s:
            movidos = s.execute(
                sql_text(
                    r"""
                    UPDATE leads
                       SET stage_id = :fim, updated_at = now()
                     WHERE tenant_id = :t
                       AND right(regexp_replace(telefone, '\D', '', 'g'), 8) = :suf
                    RETURNING id
                    """
                ),
                {"fim": STAGE_NAO_E_LEAD, "t": tenant_id, "suf": suf},
            ).fetchall()
            for (lead_id,) in movidos:
                s.execute(
                    sql_text(
                        """
                        INSERT INTO lead_events (tenant_id, lead_id, tipo, payload, at)
                        VALUES (:t, :i, 'nao_e_lead', CAST(:p AS jsonb), now())
                        """
                    ),
                    {"t": tenant_id, "i": lead_id,
                     "p": json.dumps({"origem": "pessoa", "motivo": (motivo or "")[:200]},
                                     ensure_ascii=False)},
                )
            s.commit()
        if movidos:
            log.info("carrossel_nao_e_lead", suf=suf, n=len(movidos),
                     motivo=(motivo or "")[:80])
        return bool(movidos)
    except Exception as e:  # noqa: BLE001
        log.warning("marcar_nao_e_lead_falhou", erro=str(e)[:140])
        return False


# Origem gravada no lead que a Pessoa abriu sozinha (não veio do Kommo nem do
# carrossel). Serve pra medir depois quanto o 0000 traz de orgânico.
ORIGEM_SITE = "site_4990"


def _criar_lead_do_site(session, suf: str, telefone: str,
                        nome: str | None, tenant_id: int) -> list:
    """Cria o card do lead que chegou pelo 0000 e não existia no CRM.

    O 0000 recebe lead do site (Dona, 31/07), e esses não vêm do Kommo nem do
    carrossel: em 31/07 a Pessoa conversou com dois deles e NADA ficou registrado
    — nem card, nem dono, nem histórico no funil. A conversa existia só na
    thread do WhatsApp.

    Quem já tem card (em qualquer funil) não é tocado, senão um lead em
    "Visita marcada" voltaria pra "Novo".

    `external_ref` sintético (`corretor:<sufixo>`) porque não há id do Kommo. O
    UNIQUE (tenant_id, external_ref) faz a criação ser idempotente — duas
    mensagens quase simultâneas não geram dois cards.

    A guarda por TELEFONE abaixo é obrigatória: o chamador só sabe que o UPDATE
    não pegou nada, e ele filtra por `funil = 'remarketing'`. Na 2ª mensagem de
    um lead que JÁ foi promovido, o UPDATE não casa (ele saiu do remarketing) e
    a função é chamada mesmo com o card existindo — foi assim que a Carla
    (+5500000000000) virou dois cards da Pessoa12 em 01/08/2026, um "Carla" e um
    "Reese" (o push name do WhatsApp).
    """
    if _e_da_equipe(telefone):
        return []
    ja_existe = session.execute(
        sql_text(
            r"""
            SELECT 1 FROM leads
             WHERE tenant_id = :t
               AND right(regexp_replace(telefone, '\D', '', 'g'), 8) = :suf
             LIMIT 1
            """
        ),
        {"t": tenant_id, "suf": suf},
    ).first()
    if ja_existe:
        log.info("lead_do_site_ja_existe", suf=suf)
        return []
    ref = f"corretor:{suf}"
    linhas = session.execute(
        sql_text(
            """
            INSERT INTO leads (tenant_id, external_ref, nome, telefone,
                               owner_id, stage_id, origem, created_at, updated_at)
            VALUES (:t, :ref, :nome, :tel, :corretor, :entrou, :origem, now(), now())
            ON CONFLICT (tenant_id, external_ref) DO NOTHING
            RETURNING id
            """
        ),
        {"t": tenant_id, "ref": ref, "nome": nome or telefone,
         "tel": telefone, "corretor": CORRETORA_CORRETORA_A,
         "entrou": STAGE_NOVO, "origem": ORIGEM_SITE},
    ).fetchall()
    for (lead_id,) in linhas:
        session.execute(
            sql_text(
                """
                INSERT INTO lead_events (tenant_id, lead_id, tipo, payload, at)
                VALUES (:t, :i, 'lead_criado', CAST(:p AS jsonb), now())
                """
            ),
            {"t": tenant_id, "i": lead_id,
             "p": json.dumps({"origem": ORIGEM_SITE, "linha": cb.SLUG_CORRETOR,
                              "por": "pessoa"}, ensure_ascii=False)},
        )
    if linhas:
        log.info("lead_do_site_criado", suf=suf, n=len(linhas),
                 responsavel=CORRETORA_CORRETORA_A)
    return linhas
