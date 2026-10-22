"""Handoff automático do remarketing — lead da Corretor com interesse real vira card
da corretora humana, na hora, sem WhatsApp.

Por que existe (medido em 05/09/2026): dos 40 leads que responderam ao
carrossel da Corretor na semana, 40 estavam com `corretora_label = Corretor`. Zero
transferidos. A coluna "Respondeu" era ponto final. A promoção que já existia
(`atendimento_corretor.promover_para_vendas`) só disparava a partir da 2ª mensagem
do lead — quem escreveu UMA vez, mesmo dizendo tudo ("é pra minha filha, até
450 mil, pode mandar pra mim"), ficava com o robô. Oito foram transferidos na
mão nesse dia via POST /painel/crm/transferir.

O que faz: no inbound da linha da Corretor (`atendimento_corretor.atender`), depois das
travas de opt-out/recusa, avalia TODAS as falas do lead na conversa e, se
houver sinal de interesse, aplica a mesma transferência do botão do painel
(`lead_transferencia.transferir_lead`) pra corretora configurada, levando o
card pra "Novo" do funil principal — é lá que a corretora trabalha, e "Novo"
é honesto: ninguém da casa falou com esse lead ainda pela linha dela.

O card chega MARCADO: `leads.veio_remarketing_em` (tag "remarketing" no
`/painel/crm/board`, pedido da Dona 15/09/2026). A corretora vê a procedência
no card, sem abrir o histórico.

═══════════════════════════════════════════════════════════════════════════
CRITÉRIO DE "INTERESSE REAL" (explícito, auditável, conservador)
═══════════════════════════════════════════════════════════════════════════
Entrada: todas as mensagens de TEXTO do lead nesta conversa (mídia/reação/
figurinha não contam — `atendimento_corretor._e_so_midia`). Ordem não importa.

NUNCA transfere (vetos, nesta ordem):
  V1. alguma fala é pedido de parar / opt-out (`_pediu_parar`, `opt_out.is_opt_out`)
      → "pediu_parar". Quem mandou parar não vira lead de ninguém.
  V2. alguma fala é recusa branda ("não tenho interesse", "já comprei", "no
      momento não") E nenhuma fala mostra interesse ou vontade de vender
      → "recusa". Educação depois do não ("ok", "obrigado") NÃO reabre.
      (Recusa seguida de interesse — caso Renato, permuta — transfere.)

TRANSFERE se qualquer um (o primeiro que casar vira o `sinal` do evento):
  S1. quer_vender  — `_RE_QUER_VENDER` ("estou vendendo meu apto"): captação.
  S2. interesse    — `_RE_INTERESSE` (o que comprador escreve: quero/procuro,
                     preço/parcela/entrada, apto/casa/quartos, bairro/região,
                     visita/agendar, "pode mandar", localização).
  S3. afirmativa   — resposta curta e afirmativa ao "ainda está procurando?"
                     do disparo: "Sim", "Pode sim", "Estou sim", "Quero sim",
                     "Claro", "Ok, manda". Até 6 palavras, começa com token
                     afirmativo, sem "não". Base: a classificação manual da
                     Dona em 19/08 (scripts/handoff_corretor_pendentes_1908.py) —
                     'Sim' ao ainda-procurando foi handoff, não descarte.
  S4. engajou      — 3+ falas de texto sem nenhuma recusa (`_engajou`): quem
                     insiste em responder está numa conversa.

NÃO transfere (fica com a Corretor, visível no painel): só cumprimento ("bom dia",
"olá tudo bem"), frase que nada acima entende ("?", "xpto"). Frase
desconhecida vira "não sei", e "não sei" não vai pra fila de ninguém.

Viés declarado: melhor um morno a mais na corretora do que um quente perdido
— por isso S3 existe. O custo do falso positivo é um card "Novo" a mais; o do
falso negativo foi a senhora 3 dias sem resposta.

═══════════════════════════════════════════════════════════════════════════
IDEMPOTÊNCIA / CONVIVÊNCIA COM O MANUAL
═══════════════════════════════════════════════════════════════════════════
  • só toca lead que está numa etapa do funil `remarketing` E com owner = Corretor
    (4). Quem já saiu da Corretor (transferência manual, promoção da Pessoa, roleta)
    não é mexido — nem de volta, nem pra frente;
  • `owner_editado_manual = True` (transferência pelo painel) → não mexe;
  • `lead_events tipo='transferencia'` já existente no lead → não mexe;
  • a própria transferência grava `owner_editado_manual = True`, então rodar
    de novo é noop e o shadow do Kommo não desfaz.

DESTINO: a ROLETA DO REMARKETING (`handoff_remarketing_roleta`) — Pessoa10(1),
Pessoa11(2) e Pessoa12(3) dividem quem sai da Corretor, em round-robin (Dona 18/09: "põe a
Pessoa10 e a Pessoa11 na roleta de remarketing"). É uma roleta PRÓPRIA: a do intake
(`roleta_corretoras`) continua sem a Pessoa12, porque lead novo do Meta não vai pra
ela — a regra de 26/07 é divisão de papel e segue valendo.

Desde 05/10/2026 quem manda é a tela (`quando_responde.py`): QUEM vai ("todo
mundo que cair em Respondeu", o padrão, ou só botão/interesse), pra QUAL etapa
do funil de vendas, por QUAL roleta e com QUAL aviso. Lido do banco na hora da
transferência, sem cache. Roleta vazia NÃO transfere mais (o destino único de
reserva saiu): o card fica em Respondeu e a tela avisa. Validado em runtime:
tem que existir em `corretoras`, estar ativa e não ser a Corretor. Ninguém válido,
etapa apagada ou banco fora = NÃO transfere e loga erro (fail-closed — nunca
"pra Corretor de novo", nunca "pelo padrão").

O card ganha a tag RMKT (`leads.veio_remarketing_em`) na MESMA transação do
dono e da etapa. Card com a tag nunca passa pela roleta do Meta
(`lead_intake._dono_pela_roleta`).

KILL-SWITCH: `config_operacional.handoff_remarketing_ativo()` (default ligado).

═══════════════════════════════════════════════════════════════════════════
O AVISO PELA LINHA DA CORRETORA (15/09/2026)
═══════════════════════════════════════════════════════════════════════════
Até aqui esta rota era MUDA por design — "o aviso é o card na coluna Novo".
Setembro mostrou o preço: 70 leads responderam ao remarketing, 24 foram pra
Pessoa12 e só 11 receberam mensagem. Os 11 são os da `registrar_qualificacao` (o
handoff rico, que abre a conversa na linha da Pessoa12); os outros 13 vieram por
aqui e nunca souberam de nada. Decisão da Dona: TODO transferido recebe
mensagem. Card só avisa quem está com o painel aberto; mensagem chega.

Como: `_avisar_lead` reusa o MESMO envio do handoff rico
(`cloud_corretora_a.enviar_template_linha`, o que está por baixo de
`enviar_template_corretor`) — mesma rotação, mesma trava de categoria, mesma
gravação na thread. Aqui só muda o texto do {{2}} e o carimbo `origem`.

  • TEMPLATE, não texto livre: a janela de 24h é por NÚMERO e o lead falou com
    a Corretor (0000), não com a linha da corretora. Fora da janela só passa
    template aprovado — e só UTILITY, que é o que `enviar_template_linha`
    deixa entrar (regra dura da Dona: automação nunca em MARKETING).
  • LINHA DA CORRETORA DE DESTINO, não "a linha da Pessoa12" fixa: o destino é
    configurável e a roleta da Acme mudou 4x em agosto. Corretora sem linha
    Cloud registrada → não envia, sem barulho (em 18/09 as três têm: corretor,
    corretor e corretor resolvem pelo registry).
  • DESARMADO por padrão: `handoff_remarketing_avisa_lead` nasce False.
  • O TEXTO é o da tela (`handoff_remarketing_aviso_texto`), com o da Dona de
    05/10 como padrão; texto gravado que a tela recusaria = o aviso não sai.
  • FALHA DE ENVIO NÃO DERRUBA A TRANSFERÊNCIA: o commit vem antes, o envio
    depois, dentro de try/except. O lead vai pra corretora mesmo com a Meta
    fora.
  • IDEMPOTENTE: uma mensagem por telefone, sempre (guarda em
    `mensagens_whatsapp` por `conteudo_metadata->>'origem'`), e no máximo uma
    por chamada mesmo que o telefone tenha 2 cards.
  • Respeita silêncio: opt-out nas duas fontes (`pos_ligacao._pediu_silencio`).

QUANDO O LEAD NÃO ENTRA POR AQUI: o aviso também é disparado por ESTADO —
`aviso_remarketing.rodar()` varre quem está em "Novo" com a tag e ainda não
recebeu, cobrindo transferência pelo painel, script de lote e redistribuição.
Os dois caminhos chamam o MESMO `avisar_lead`, então as travas valem igual.

O QUE (ainda) NÃO FAZ: não avisa a corretora (o aviso dela segue sendo o card
na coluna Novo + selo "transferido"); não escreve no Kommo; não cala a Pessoa
(quem cala é `_handoff_feito`, que olha a LeadHandoffNote da
`registrar_qualificacao` — o handoff rico, com o raio-X, continua sendo dela).
"""
from __future__ import annotations

import re
from typing import Any

import structlog
from pydantic import BaseModel
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from app.db.models import Lead
from app.services import carrossel_corretor as cb
from app.services import config_operacional
from app.services import nome_publico
from app.services import quando_responde
from app.services.atendimento_corretor import (
    STAGE_NOVO,
    _e_recusa_branda,
    _e_so_midia,
    _engajou,
    _pediu_parar,
    _quer_vender,
    _tem_interesse,
)
from app.services.lead_transferencia import (
    EVENTO_TRANSFERENCIA,
    Transferencia,
    label_corretora,
    transferir_lead,
)
from app.services.nutricao import FUNIL_REMARKETING

log = structlog.get_logger()

# Quem assina o evento `transferencia` — é o que separa o automático do botão
# do painel no histórico (lá o `quem` é o usuário).
QUEM = "handoff_remarketing"
# Etapa de chegada PADRÃO: "Novo" do funil principal (6). NÃO é "Em
# atendimento" (7, destino da promoção da Pessoa): lá a abertura já saiu pela
# linha da Pessoa12; aqui nada saiu — a corretora precisa abordar. Desde 05/10/2026 a
# etapa que vale é a de `quando_responde.ler` (a tela escolhe); esta constante
# fica pro que ainda a importa.
STAGE_DESTINO = STAGE_NOVO

SINAL_QUER_VENDER = "quer_vender"
SINAL_INTERESSE = "interesse"
SINAL_AFIRMATIVA = "afirmativa"
SINAL_ENGAJOU = "engajou"
# Modo "todo mundo que cair em Respondeu" (Dona 05/10/2026): não houve leitura
# de interesse — o card caiu em Respondeu e isso basta.
SINAL_RESPONDEU = "respondeu"

# S3 — afirmativa curta. Primeiro token (ou par) da lista, até 6 palavras, sem
# "não" em lugar nenhum. "Pode ser", "Sim, pode mandar", "Estou sim!".
_AFIRMATIVAS_1 = frozenset({
    "sim", "isso", "claro", "pode", "ok", "okay", "opa", "manda", "bora",
    "vamos", "estou", "tenho", "quero", "ainda", "to", "tô", "aceito",
    "gostaria", "positivo", "certo", "beleza", "show", "top", "demais",
    "perfeito", "otimo", "ótimo",
})
# "tudo bem" fica de fora: é cumprimento ("Oi, tudo bem?"), não resposta.
_AFIRMATIVAS_2 = frozenset({"com certeza", "pode ser", "pode sim",
                            "ta bom", "tá bom", "por favor"})
_MAX_PALAVRAS_AFIRMATIVA = 6
_RE_PONTUACAO = re.compile(r"[^\w\sáàâãéêíóôõúüç]", re.IGNORECASE)


def _e_afirmativa(texto: str | None) -> bool:
    if not texto:
        return False
    limpo = _RE_PONTUACAO.sub(" ", texto.lower()).split()
    if not limpo or len(limpo) > _MAX_PALAVRAS_AFIRMATIVA:
        return False
    if any(w in ("nao", "não", "nunca", "nem") for w in limpo):
        return False
    if limpo[0] in _AFIRMATIVAS_1:
        return True
    return " ".join(limpo[:2]) in _AFIRMATIVAS_2


class Decisao(BaseModel):
    """Saída de `avaliar` — vai inteira pro payload do evento."""
    transferir: bool
    sinal: str | None = None
    motivo: str
    trecho: str | None = None


def _trecho(texto: str | None) -> str | None:
    return (texto or "").strip()[:120] or None


def avaliar(falas: list[str | None]) -> Decisao:
    """Aplica o critério do docstring do módulo. Puro: sem banco, sem rede."""
    from app.services.opt_out import is_opt_out

    reais = [f for f in falas if f and f.strip() and not _e_so_midia(f)]
    if not reais:
        return Decisao(transferir=False, motivo="sem_texto")

    # V1 — pediu pra parar: veto absoluto.
    if any(_pediu_parar(f) or is_opt_out(f) for f in reais):
        return Decisao(transferir=False, motivo="pediu_parar")

    vender = next((f for f in reais if _quer_vender(f)), None)
    # Interesse tem que vir de uma fala que NÃO é recusa: "não tenho interesse"
    # casa `\binteress` na regex de interesse — sem esta exclusão a recusa se
    # anularia sozinha e viraria handoff.
    interesse = next(
        (f for f in reais if _tem_interesse(f) and not _e_recusa_branda(f)), None
    )

    # V2 — recusa branda sem interesse em nenhuma fala.
    if any(_e_recusa_branda(f) for f in reais) and not (vender or interesse):
        return Decisao(transferir=False, motivo="recusa")

    if vender:
        return Decisao(transferir=True, sinal=SINAL_QUER_VENDER,
                       motivo="quer vender imóvel (captação)", trecho=_trecho(vender))
    if interesse:
        return Decisao(transferir=True, sinal=SINAL_INTERESSE,
                       motivo="disse o que procura / pediu opções",
                       trecho=_trecho(interesse))
    afirm = next((f for f in reais if _e_afirmativa(f)), None)
    if afirm:
        return Decisao(transferir=True, sinal=SINAL_AFIRMATIVA,
                       motivo="respondeu sim ao 'ainda está procurando?'",
                       trecho=_trecho(afirm))
    if _engajou(reais):
        return Decisao(transferir=True, sinal=SINAL_ENGAJOU,
                       motivo=f"{len(reais)} mensagens sem recusa",
                       trecho=_trecho(reais[0]))
    return Decisao(transferir=False, motivo="sem_sinal", trecho=_trecho(reais[0]))


# ── Destino ──────────────────────────────────────────────────────────────────

def _corretora_ok(session: Session, cid: int | None) -> bool:
    """Existe, está ativa e não é a Corretor."""
    if cid is None or cid == cb.CORRETORA_CORRETOR:
        return False
    row = session.execute(
        sql_text("SELECT ativo FROM corretoras WHERE id = :id"), {"id": cid}
    ).first()
    return row is not None and bool(row[0])


def _proxima_da_roleta(session: Session, elegiveis: list[int],
                       tenant_id: int = 1) -> int:
    """Round-robin sobre as elegíveis — ver `quando_responde.proxima_da_roleta`."""
    return quando_responde.proxima_da_roleta(session, tenant_id, tuple(elegiveis))


def destino_valido(session: Session, tenant_id: int = 1) -> tuple[int, str | None] | None:
    """(corretora_id, label) de quem recebe ESTE lead, ou None se não dá.

    A ROLETA DO REMARKETING (`handoff_remarketing_roleta`, editada na tela):
    quem sai da Corretor é dividido em round-robin. É uma roleta PRÓPRIA — a do
    Meta (`roleta_corretoras`) continua sem a Pessoa12, porque lead novo do Meta não
    vai pra ela (regra de 26/07, que segue de pé).

    Roleta VAZIA não transfere (05/10/2026). Até aqui a lista vazia caía num
    destino único de reserva (`handoff_remarketing_corretora`, a Pessoa12): esvaziar
    a roleta na tela continuaria mandando lead pra alguém, sem a tela dizer.
    Agora vazio é vazio — o card fica em Respondeu e a tela avisa.

    Fail-closed: id inexistente, inativo ou a própria Corretor sai da roda; se não
    sobrar ninguém, ou se a configuração não puder ser lida, None + log de
    erro. Melhor o lead ficar visível na Corretor do que "transferido" pra ninguém.
    """
    try:
        cfg = quando_responde.ler(session, tenant_id)
    except (quando_responde.ConfigInvalida, config_operacional.ConfigIndisponivel) as e:
        log.error("handoff_remarketing_destino_invalido",
                  motivo=getattr(e, "motivo", "config_indisponivel"), erro=str(e)[:160])
        return None
    cid = quando_responde.proxima_da_roleta(session, tenant_id, cfg.roleta)
    return cid, label_corretora(session, cid)


# ── Aviso pela linha da corretora ────────────────────────────────────────────

# Carimbo em `conteudo_metadata->>'origem'`. É o que (a) separa esta mensagem
# de uma escrita à mão — o painel e a trava de conversa humana da cadência
# contam humano pela AUSÊNCIA de origem — e (b) dá a idempotência.
ORIGEM_AVISO = QUEM

# ⚠️ `delivery_status <> 'failed'`: mensagem que a Meta RECUSOU não conta como
# apresentação feita. Em 18/09 as WABAs da Pessoa10 e da Pessoa11 travaram em "Business
# eligibility payment issue" (cobrança) e 18 leads do handoff foram marcados
# como avisados sem nunca receber nada — com o dedup cego ao status, eles
# ficariam sem a mensagem PARA SEMPRE, inclusive depois de a conta voltar.
# Status ainda nulo (webhook de status não chegou) segue contando: acabou de
# sair, repetir agora é que seria erro.
_SQL_JA_AVISADO = r"""
    SELECT EXISTS (
      SELECT 1 FROM mensagens_whatsapp
       WHERE conteudo_metadata->>'origem' = :o
         AND coalesce(conteudo_metadata->>'delivery_status', '') <> 'failed'
         AND right(regexp_replace(coalesce(contato_telefone,''), '\D', '', 'g'), 8) = :suf)
"""


def _ja_avisado(session: Session, to: str) -> bool:
    """Uma apresentação por telefone, para sempre. Sem janela de tempo: o lead
    só chega aqui uma vez (evento `transferencia` prévio já barra a segunda
    transferência), então repetir seria sempre erro.

    Erro de consulta devolve True — "não sei" vira silêncio, que é o lado
    barato de errar quando o outro lado é mandar duas vezes."""
    try:
        return bool(session.execute(
            sql_text(_SQL_JA_AVISADO), {"o": ORIGEM_AVISO, "suf": to[-8:]}
        ).scalar())
    except Exception:  # noqa: BLE001
        log.warning("handoff_aviso_dedup_indisponivel", suf=to[-8:])
        return True


_SQL_TEVE_HANDOFF_RICO = r"""
    SELECT EXISTS (
      SELECT 1 FROM lead_handoff_notes
       WHERE origem LIKE 'pessoa%'
         AND right(regexp_replace(coalesce(telefone,''), '\D', '', 'g'), 8) = :suf)
"""


def _teve_handoff_rico(session: Session, to: str) -> bool:
    """Já passou pela `registrar_qualificacao` da Pessoa?

    Quem passou recebeu a abertura COM o raio-X na linha da Pessoa12 ("te resumo:
    você tá procurando casa 3 quartos…"). Mandar a apresentação genérica depois
    dessa seria dizer duas vezes a mesma coisa, a segunda pior. Na prática o
    handoff rico deixa o lead em "Em atendimento" (7) e este aviso só olha
    "Novo" (6) — o veto é pro dia em que alguém arrastar o card de volta.

    Erro de consulta devolve True: silêncio é o lado barato de errar."""
    try:
        return bool(session.execute(
            sql_text(_SQL_TEVE_HANDOFF_RICO), {"suf": to[-8:]}
        ).scalar())
    except Exception:  # noqa: BLE001
        log.warning("handoff_aviso_rico_indisponivel", suf=to[-8:])
        return True


# ── O resumo do que a pessoa falou ───────────────────────────────────────────
#
# Dona 18/09: "vc fala: eu te enviei (a pergunta) e vc disse que eu poderia
# mandar... podemos continuar por aqui". A mensagem lembra a pessoa da conversa
# que ela teve — e não existe nada melhor pra isso do que a frase dela mesma.
#
# Por que NÃO é o resumo rico da Pessoa (`corpo_abertura_corretor`): aquele precisa da
# qualificação (o que busca + renda), e quem chega aqui não tem nenhuma — a
# conversa morreu antes. Quem TEM qualificação não passa por este caminho:
# `_teve_handoff_rico` veta, porque a mensagem boa já saiu pela linha da Pessoa12.
#
# Determinístico de propósito (regex, não LLM): o {{2}} vai pra Meta num
# template UTILITY e precisa ser sempre auditável. Sem sinal reconhecido, cai no
# texto de registro — dizer "você me disse que" sem saber o que ela disse seria
# inventar.
_RE_REGIAO = re.compile(
    r"\b(?:zona\s+(?:leste|oeste|norte|sul|central)|centro|cidade|votorantim"
    r"|salto\s+de\s+pirapora|iper[oó]|araçoiaba|boituva|itu|bairro"
    r"|cajuru|[eé]den|wanel|aparecidinha|brigadeiro|vila\s+\w+|jardim\s+\w+"
    r"|jd\.?\s+\w+)\b",
    re.IGNORECASE,
)
_RE_INVESTIR = re.compile(r"\binvest(?:ir|imento)\b", re.IGNORECASE)
_RE_MORAR = re.compile(r"\bmorar\b", re.IGNORECASE)
_RE_ALUGAR = re.compile(r"\b(?:alugar|aluguel|loca[çc][ãa]o)\b", re.IGNORECASE)
_RE_LANCAMENTO = re.compile(r"\blan[çc]amento", re.IGNORECASE)
# "pode mandar", "pode enviar", "manda pra mim", "me manda", "favor enviar".
_RE_PODE_MANDAR = re.compile(
    r"\b(?:pode(?:ria)?|favor|por\s+gentileza|quero)\b[^.!?]{0,20}"
    r"\b(?:mandar?|manda|enviar|envia|me\s+mande)\b"
    r"|\b(?:me\s+)?(?:manda|mande|envia|envie)\b",
    re.IGNORECASE,
)
_RE_NEGACAO = re.compile(r"\b(?:n[ãa]o|nunca|nem|jamais)\b", re.IGNORECASE)
_BOTAO = "saiba mais"


def resumo_do_lead(falas: list[str | None]) -> str | None:
    """A oração que entra depois de "você me disse que" — ou None.

    Lê as falas do lead da mais RECENTE pra mais antiga e devolve o primeiro
    sinal reconhecido. Ordem importa: "é pra investimento" diz mais do que
    "pode mandar", então finalidade e região ganham do consentimento genérico.
    """
    limpas = [f.strip() for f in falas if f and f.strip()]
    if not limpas:
        return None

    # Fala com negação NÃO vira resumo. Caso real (lead 5883, 06/09): "Nunca
    # estive procurando imóvel em Cidade para comprar. Apenas venda." — o
    # padrão de região casaria "cidade" e a mensagem diria "você me disse que
    # procura em cidade", que é o oposto do que a pessoa falou. Descarto a
    # fala inteira: perder um resumo custa o texto de registro; inventar um
    # custa a confiança de quem lê.
    limpas = [f for f in limpas if not _RE_NEGACAO.search(f)]
    if not limpas:
        return None

    texto_tudo = " ".join(limpas)
    # Finalidade e região primeiro: são o que a corretora precisa saber.
    if _RE_INVESTIR.search(texto_tudo):
        return "é pra investimento"
    if _RE_ALUGAR.search(texto_tudo):
        return "é pra alugar"
    if _RE_LANCAMENTO.search(texto_tudo):
        return "queria informações sobre os lançamentos"
    regiao = _RE_REGIAO.search(texto_tudo)
    if regiao:
        return f"procura em {regiao.group(0).strip().lower()}"
    if _RE_MORAR.search(texto_tudo):
        return "é pra morar"
    if _RE_PODE_MANDAR.search(texto_tudo):
        return "eu podia te mandar as opções"
    if any(f.strip().lower() == _BOTAO for f in limpas):
        return "queria saber mais"
    if any(_e_afirmativa(f) for f in limpas):
        return "ainda estava procurando"
    return None


# A corretora já falou com essa pessoa DEPOIS de receber o lead? Então a
# apresentação chegaria atrasada e errada — "seu atendimento continua comigo"
# pra quem está no meio de uma conversa com ela. Achado do piloto de 18/09: o
# tick por estado pegou 5 leads de 06-07/09 que a Pessoa12 já estava trabalhando (um
# deles respondeu na véspera). O dedup de `_ja_avisado` não cobria: eles nunca
# receberam ESTE aviso, o que não quer dizer que precisassem dele.
#
# Mensagem da linha da CORRETOR não conta (é o robô que já estava lá antes).
_SQL_CONVERSOU_DEPOIS = r"""
    SELECT 1
      FROM leads l
      JOIN mensagens_whatsapp m
        ON right(regexp_replace(coalesce(m.contato_telefone, ''), '\D', '', 'g'), 8)
         = right(regexp_replace(coalesce(l.telefone, ''), '\D', '', 'g'), 8)
     WHERE right(regexp_replace(coalesce(l.telefone, ''), '\D', '', 'g'), 8) = :suf
       AND l.veio_remarketing_em IS NOT NULL
       AND m.from_corretora IS TRUE
       AND coalesce(m.corretora_id, 0) <> :corretor
       AND m.timestamp_evolution > l.veio_remarketing_em
     LIMIT 1
"""


def _ja_conversou_com_a_corretora(session: Session, to: str) -> bool:
    """Erro de consulta devolve True — "não sei" vira silêncio, o lado barato."""
    try:
        return bool(session.execute(
            sql_text(_SQL_CONVERSOU_DEPOIS), {"suf": to[-8:], "corretor": cb.CORRETORA_CORRETOR}
        ).scalar())
    except Exception:  # noqa: BLE001
        log.warning("handoff_aviso_conversa_indisponivel", suf=to[-8:])
        return True


def texto_aviso(corretora_nome: str | None, resumo: str | None = None,
                modelo: str | None = None) -> str:
    """O {{2}} do template — a única parte que este módulo escreve.

    PADRÃO (Dona, 05/10/2026): "Sou a X, da Acme Imóveis. Estávamos falando
    em outro número, vamos continuar o atendimento por aqui!". É o `modelo`
    (o texto que a tela gravou, ou o padrão), com `{corretora}` trocado pelo
    nome público. O "Olá" não entra: o casco `acme_*_ola_*` já abre com
    "Olá {{1}}, tudo bem?".

    O convite mora na VARIÁVEL, não no corpo do template — a Meta classifica
    UTILITY/MARKETING pelo texto FIXO, na aprovação, então convidar aqui não
    reclassifica nada. (O mesmo convite como texto fixo vira MARKETING: medido
    em `pos_ligacao.py`. Por isso não existe template novo pra este aviso.)

    COM resumo (opção desligada por padrão desde 05/10): a forma de 18/09, que
    lembra o que a pessoa disse. Só entra quando quem chama passa o resumo.
    """
    # `corretora_nome` JÁ chega resolvido pelo nome PÚBLICO (ver `_avisar_lead`):
    # a Pessoa3 se apresenta como "Pessoa", e dizer "Pessoa3" aqui é um nome
    # que a cliente nunca mais ouve (caso Kauane, 18/09).
    if resumo:
        primeiro = nome_publico.primeiro_nome(corretora_nome)
        # Sem nome no cadastro, a mensagem fala pela casa — "Aqui é a a, da
        # Acme" seria pior que não dizer nome nenhum.
        quem = f"a {primeiro}" if primeiro else "a equipe"
        return (
            f"Aqui é {quem}, da Acme Imóveis. Eu te escrevi pelo nosso outro "
            f"número perguntando se você ainda estava procurando imóvel em "
            f"Cidade, e você me disse que {resumo}. Podemos continuar por aqui?"
        )
    return quando_responde.render_aviso(
        modelo or quando_responde.AVISO_PADRAO, corretora_nome)


# Falas do lead na linha da Corretor, da mais recente pra trás. Mídia sem texto não
# entra (o `_e_so_midia` do atendimento cuida disso na entrada do handoff; aqui
# o filtro é o próprio `conteudo_texto` vazio). Limite de 20: conversa de
# remarketing é curta, e o resumo olha o topo da pilha.
_SQL_FALAS_DO_LEAD = r"""
    SELECT coalesce(nullif(m.conteudo_texto, ''), m.transcricao)
      FROM mensagens_whatsapp m
     WHERE right(regexp_replace(coalesce(m.contato_telefone, ''), '\D', '', 'g'), 8) = :suf
       AND m.from_corretora IS FALSE
       AND m.corretora_id = :corretor
       AND coalesce(nullif(m.conteudo_texto, ''), m.transcricao) IS NOT NULL
     ORDER BY m.timestamp_evolution DESC
     LIMIT 20
"""


def _falas_do_lead(session: Session, to: str) -> list[str | None]:
    """Nunca levanta: sem falas o aviso sai na forma de registro."""
    try:
        return [r[0] for r in session.execute(
            sql_text(_SQL_FALAS_DO_LEAD), {"suf": to[-8:], "corretor": cb.CORRETORA_CORRETOR}
        ).all()]
    except Exception as e:  # noqa: BLE001
        log.warning("handoff_aviso_falas_indisponiveis", erro=str(e)[:120])
        return []


def motivo_para_pular(session: Session, to: str) -> str | None:
    """Por que este telefone NÃO deve receber o aviso — ou None se deve.

    Pública e separada do envio porque o tick precisa das MESMAS respostas no
    `--seco`. Antes o dry-run só contava candidatos e dizia "mandaria 8" onde o
    envio real mandaria 3; relatório que não bate com a realidade é pior que
    nenhum, ainda mais num tick que fala com lead.
    """
    from app.services.pos_ligacao import _pediu_silencio

    if _pediu_silencio(session, to):
        return "opt_out"
    if _ja_avisado(session, to):
        return "ja_avisado"
    if _teve_handoff_rico(session, to):
        return "handoff_rico"
    if _ja_conversou_com_a_corretora(session, to):
        return "conversa_em_andamento"
    return None


def _avisar_lead(session: Session, *, telefone: str, nome: str | None,
                 corretora_id: int, corretora_label: str | None) -> dict[str, Any]:
    """Template de apresentação pela linha da corretora que recebeu o lead.

    Nunca levanta: quem chama já commitou a transferência e isto é o extra.
    """
    # O que a tela gravou, relido: ligado?, qual texto, com resumo ou sem. O
    # que não der pra ler ou não servir NÃO vira padrão — o aviso não sai e o
    # motivo volta pra quem chamou (a transferência já está commitada).
    try:
        if not quando_responde.aviso_ligado():
            return {"ok": False, "motivo": "desarmado"}
        modelo = quando_responde.modelo_do_aviso()
        com_resumo = quando_responde.aviso_com_resumo()
    except quando_responde.ConfigInvalida as e:
        log.error("handoff_aviso_texto_invalido_nao_enviou", erro=str(e)[:160])
        return {"ok": False, "motivo": e.motivo, "erro": str(e)[:160]}
    except config_operacional.ConfigIndisponivel as e:
        log.error("handoff_aviso_config_indisponivel_nao_enviou", erro=str(e)[:140])
        return {"ok": False, "motivo": "config_indisponivel"}

    to = cb._so_digitos(telefone)
    if not (12 <= len(to) <= 13):
        return {"ok": False, "motivo": "telefone_invalido"}

    from app.services import cloud_lines

    linha = cloud_lines.linha_da_corretora(corretora_id, session=session)
    if linha is None or linha is cloud_lines.INDETERMINADO or not linha.phone_id:
        # Corretora sem linha Cloud (Pessoa11, ainda no Evolution) ou cadastro
        # ilegível: a transferência vale, o aviso não sai. Enviar pela linha de
        # OUTRA corretora seria pior que não enviar.
        log.info("handoff_aviso_sem_linha", corretora_id=corretora_id)
        return {"ok": False, "motivo": "corretora_sem_linha"}

    pular = motivo_para_pular(session, to)
    if pular:
        return {"ok": False, "motivo": pular}

    from app.api import cloud_corretora_a as cg

    try:
        r = cg.enviar_template_linha(
            linha, to, nome,
            # Nome PÚBLICO da corretora (Pessoa3 → "Pessoa"). Cai no
            # `corretora_label` quando o cadastro não sabe dizer. O resumo do
            # que a pessoa falou com a Corretor só entra com a opção ligada na tela.
            hook=texto_aviso(
                nome_publico.publico(session, corretora_id) or corretora_label,
                resumo_do_lead(_falas_do_lead(session, to)) if com_resumo else None,
                modelo,
            ),
            instance=cloud_lines.instance_da_linha(linha),
            corretora_id=corretora_id,
            origem=ORIGEM_AVISO,
        )
    except Exception as e:  # noqa: BLE001 — Meta fora / linha sem UTILITY
        log.warning("handoff_aviso_falhou", corretora_id=corretora_id,
                    tel=to[-4:], erro=str(e)[:160])
        return {"ok": False, "motivo": "envio_falhou", "erro": str(e)[:120]}

    log.info("handoff_aviso_enviado", corretora_id=corretora_id, tel=to[-4:],
             linha=linha.slug, template=r.get("template"),
             dry_run=bool(r.get("dry_run")))
    return {"ok": True, "template": r.get("template"),
            "dry_run": bool(r.get("dry_run")), "wamid": r.get("wamid")}


# Nome público do envio. O tick `aviso_remarketing` (gatilho por estado: tag +
# coluna "Novo") chama por aqui — as travas de linha, opt-out, dedup e handoff
# rico vivem todas dentro dele, e é isso que mantém uma decisão só sobre se a
# mensagem sai, valendo igual pro inbound e pro tick.
avisar_lead = _avisar_lead


# ── Execução ─────────────────────────────────────────────────────────────────

_SQL_LEADS_NA_CORRETOR = r"""
    SELECT l.id
      FROM leads l
      JOIN pipeline_stages ps ON ps.id = l.stage_id
     WHERE l.tenant_id = :t
       AND ps.funil = :funil
       AND (CAST(:so_etapa AS bigint) IS NULL OR l.stage_id = CAST(:so_etapa AS bigint))
       AND right(regexp_replace(coalesce(l.telefone, ''), '\D', '', 'g'), 8) = :suf
     ORDER BY l.id
"""

_SQL_JA_TRANSFERIDO = """
    SELECT 1 FROM lead_events
     WHERE lead_id = :i AND tipo = :tipo
     LIMIT 1
"""


def _ja_transferido(session: Session, lead_id: int) -> bool:
    return bool(session.execute(
        sql_text(_SQL_JA_TRANSFERIDO), {"i": lead_id, "tipo": EVENTO_TRANSFERENCIA}
    ).first())


def humano_no_comando(telefone: str) -> bool:
    """Alguém da casa já falou com este contato DEPOIS do último disparo?

    É a régua da Pessoa (`atendimento_corretor._humano_falando` desde
    `_inicio_da_conversa`), reaproveitada e não copiada: o que a cala é o que
    segura a transferência. Ela olha a fala por telefone, sem filtrar linha nem
    instância — as 485 falas do celular da Corretor de 15/09 a 05/10 estão gravadas
    como `cloud_assistente` e contam do mesmo jeito.
    """
    from app.services import atendimento_corretor as ab

    return ab._humano_falando(telefone, desde=ab._inicio_da_conversa(telefone))


_SQL_JA_SEGURADO = """
    SELECT 1 FROM lead_events
     WHERE lead_id = :i AND tipo = :ev
       AND at > coalesce((SELECT max(at) FROM lead_events
                           WHERE lead_id = :i AND tipo = :disparo), '-infinity')
     LIMIT 1
"""


def _resumo_em_segundo_plano(lead_ids: list[int], telefone: str, tenant_id: int,
                             origem: str) -> bool:
    """Agenda o resumo por IA. Nunca levanta: a transferência já aconteceu."""
    try:
        from app.services import ia_do_lado

        return ia_do_lado.resumir_em_segundo_plano(lead_ids, telefone, tenant_id, origem)
    except Exception as e:  # noqa: BLE001
        log.warning("handoff_resumo_ia_nao_agendado", erro=str(e)[:120])
        return False


def _segurar(session: Session, ids: list[int], decisao: Decisao,
             novos: list[int] | None = None) -> list[int]:
    """Grava `handoff_segurado` nos cards que SERIAM transferidos (com a Corretor,
    sem edição manual, nunca transferidos). Um por card por disparo: a pessoa
    escrevendo dez vezes não vira dez eventos. Não faz commit. `novos`, se
    vier, recebe os cards que ganharam o evento agora (são os que ganham o
    resumo por IA)."""
    from app.services.lead_spine import _registrar_evento

    segurados: list[int] = []
    for lead in session.query(Lead).filter(Lead.id.in_(ids)).all():
        owner = int(lead.owner_id) if lead.owner_id is not None else None
        if owner != cb.CORRETORA_CORRETOR or bool(lead.owner_editado_manual):
            continue
        if _ja_transferido(session, int(lead.id)):
            continue
        segurados.append(int(lead.id))
        if session.execute(
            sql_text(_SQL_JA_SEGURADO),
            {"i": int(lead.id), "ev": quando_responde.EVENTO_SEGURADO,
             "disparo": cb.EVENT_DISPARO},
        ).first():
            continue
        if novos is not None:
            novos.append(int(lead.id))
        _registrar_evento(session, int(lead.tenant_id), int(lead.id),
                          quando_responde.EVENTO_SEGURADO, {
                              "motivo": quando_responde.MOTIVO_HUMANO,
                              "explicacao": "alguém da casa já está conversando com "
                                            "essa pessoa pelo celular da linha da Corretor",
                              "sinal": decisao.sinal,
                              "trecho": decisao.trecho,
                              "quem": QUEM,
                          })
    return segurados


def _transferir(res: dict[str, Any], telefone: str, nome: str | None,
                tenant_id: int, decisao: Decisao, *,
                so_em_respondeu: bool = False) -> dict[str, Any]:
    """Aplica a decisão: lê a configuração da tela, escolhe a próxima da
    roleta do remarketing e move o card — dono, etapa de entrada, tag RMKT e o
    evento NUMA transação. Depois do commit, o aviso pela linha da corretora.

    `so_em_respondeu`: só toca card que está na etapa Respondeu (o modo "todo
    mundo que cair em Respondeu"). Sem isso vale o recorte de sempre: qualquer
    etapa do funil `remarketing`, dono = Corretor.

    Preenche e devolve `res`. Levanta só o que `processar*` já espera tratar.
    """
    suf = cb._so_digitos(telefone)[-8:]
    if not suf:
        res["motivo"] = "sem_telefone"
        return res

    from app.db.session import SessionLocal

    with SessionLocal() as s:
        ids = [int(r[0]) for r in s.execute(
            sql_text(_SQL_LEADS_NA_CORRETOR),
            {"t": tenant_id, "funil": FUNIL_REMARKETING, "suf": suf,
             "so_etapa": cb.STAGE_RESPONDEU if so_em_respondeu else None},
        ).all()]
        if not ids:
            res["motivo"] = "sem_lead_no_remarketing"
            return res

        # Configuração relida do banco AGORA (sem cache). Etapa apagada, de
        # outro cliente, roleta vazia, banco fora: não transfere e diz por quê.
        try:
            cfg = quando_responde.ler(s, tenant_id)
        except quando_responde.ConfigInvalida as e:
            log.error("handoff_remarketing_nao_transferiu", motivo=e.motivo,
                      erro=str(e)[:160], tel=telefone[-4:])
            res["motivo"] = e.motivo
            res["detalhe"] = str(e)[:200]
            return res
        except config_operacional.ConfigIndisponivel as e:
            log.error("handoff_remarketing_nao_transferiu",
                      motivo="config_indisponivel", erro=str(e)[:140],
                      tel=telefone[-4:])
            res["motivo"] = "config_indisponivel"
            return res
        # NÃO TIRAR DA MÃO DE QUEM JÁ ESTÁ CONVERSANDO (adendo da
        # orquestradora, 05/10/2026). Tem gente atendendo o celular da linha da
        # Corretor ao vivo. Se alguém da casa já falou com esse contato nesta
        # conversa — o MESMO critério que cala a Pessoa —, o card fica em
        # Respondeu com a Corretor, sem transferência e sem aviso, e o motivo vai
        # pro card (`handoff_segurado`). A transferência manual segue valendo.
        if cfg.respeita_humano and humano_no_comando(telefone):
            res["motivo"] = quando_responde.MOTIVO_HUMANO
            novos: list[int] = []
            res["segurados"] = _segurar(s, ids, decisao, novos)
            s.commit()
            log.info("handoff_remarketing_segurado_humano", tel=telefone[-4:],
                     leads=res["segurados"])
            # Quem pega o celular também lê o resumo. Depois do commit, em
            # segundo plano; nada aqui espera a IA.
            res["resumo_ia"] = _resumo_em_segundo_plano(novos, telefone, tenant_id,
                                                        "segurado")
            return res

        destino_cid = quando_responde.proxima_da_roleta(s, tenant_id, cfg.roleta)
        destino_label = label_corretora(s, destino_cid)

        feitas: list[Transferencia] = []
        pulados: list[dict[str, Any]] = []
        for lead in s.query(Lead).filter(Lead.id.in_(ids)).all():
            owner = int(lead.owner_id) if lead.owner_id is not None else None
            if owner != cb.CORRETORA_CORRETOR:
                pulados.append({"lead_id": lead.id, "por": "ja_com_corretora",
                                "owner_id": owner})
                continue
            if bool(lead.owner_editado_manual):
                pulados.append({"lead_id": lead.id, "por": "editado_manual"})
                continue
            if _ja_transferido(s, int(lead.id)):
                pulados.append({"lead_id": lead.id, "por": "ja_transferido"})
                continue
            feitas.append(transferir_lead(
                s, lead, destino_cid,
                de_cid=cb.CORRETORA_CORRETOR,
                quem=QUEM,
                motivo=f"handoff automático: {decisao.motivo}",
                para_label=destino_label,
                stage_para=cfg.etapa,
                # A TAG RMKT (`leads.veio_remarketing_em`), na MESMA transação
                # do dono e da etapa: é ela que diz, daqui pra frente, que este
                # card não passa pela roleta do Meta. Aqui a procedência é
                # certa — o lead estava numa etapa do funil `remarketing` com a
                # Corretor (ver _SQL_LEADS_NA_CORRETOR).
                marcar_remarketing=True,
                extra={
                    "origem": QUEM,
                    "sinal": decisao.sinal,
                    "trecho": decisao.trecho,
                    "lead_nome": nome,
                    "tag": quando_responde.TAG,
                    "roleta": "remarketing",
                    "quem_vai": cfg.quem_vai,
                    "etapa_nome": cfg.etapa_nome,
                },
            ))
        # COMMIT PRIMEIRO, mensagem depois: a transferência é o que não
        # pode falhar. Meta fora do ar não segura lead na Corretor.
        s.commit()

        if feitas:
            # Uma mensagem por TELEFONE, não por card: o mesmo número pode
            # ter 2 leads no remarketing e a pessoa é uma só.
            # try/except PRÓPRIO (o de fora marca a chamada inteira como
            # "erro", e o lead JÁ está com a corretora — reportar falha aqui
            # seria mentir sobre o que aconteceu).
            try:
                res["aviso"] = _avisar_lead(
                    s, telefone=telefone, nome=nome,
                    corretora_id=destino_cid, corretora_label=destino_label,
                )
            except Exception as e:  # noqa: BLE001
                log.warning("handoff_aviso_erro", erro=str(e)[:160])
                res["aviso"] = {"ok": False, "motivo": "erro"}

    res["pulados"] = pulados
    if not feitas:
        res["motivo"] = pulados[0]["por"] if pulados else "nada_a_fazer"
        return res
    # RESUMO POR IA NO CARD (05/10/2026). O card JÁ está com a corretora (o
    # commit e o aviso vieram antes); o resumo sai numa thread e aparece quando
    # ficar pronto. Falhou = o card diz "sem resumo". Ver `ia_do_lado`.
    res["resumo_ia"] = _resumo_em_segundo_plano(
        [t.lead_id for t in feitas], telefone, tenant_id, "transferencia")
    res.update(
        transferiu=True,
        motivo=decisao.sinal,
        leads=[t.lead_id for t in feitas],
        para=destino_cid,
        para_label=destino_label,
        etapa=cfg.etapa,
    )
    log.info(
        "handoff_remarketing_ok", tel=telefone[-4:], leads=res["leads"],
        para=destino_cid, sinal=decisao.sinal, trecho=decisao.trecho,
        etapa=cfg.etapa,
    )
    return res


def processar(telefone: str, falas: list[str | None], nome: str | None = None,
              tenant_id: int = 1, *, interesse_decidido: bool = False) -> dict[str, Any]:
    """Avalia e, se for o caso, transfere. Best-effort: NUNCA levanta — erro
    aqui não pode derrubar o atendimento (o lead ainda recebe a Pessoa).

    É o caminho do INTERESSE LIDO (S1–S4), chamado de dentro do atendimento da
    Pessoa. Vale nos dois modos da tela; no modo "todos" o card normalmente já
    saiu por `processar_resposta` e aqui não sobra nada pra fazer.
    """
    res: dict[str, Any] = {"transferiu": False, "motivo": None, "leads": []}
    try:
        if not config_operacional.handoff_remarketing_ativo():
            res["motivo"] = "desligado"
            return res
        decisao = avaliar(falas)
        if interesse_decidido and not decisao.transferir and decisao.motivo == "sem_sinal":
            # A regra do remarketing leu interesse numa resposta DIRETA ao
            # disparo ("Olá, estou sim", "manda por favor") que este critério,
            # sem saber que é resposta direta, não lê. Só sobe o "sem sinal":
            # veto (parar, recusa) continua veto.
            decisao = Decisao(
                transferir=True, sinal=SINAL_INTERESSE,
                motivo="respondeu sim / pediu opções ao disparo (regra do remarketing)",
                trecho=decisao.trecho,
            )
        res["decisao"] = decisao.model_dump()
        if not decisao.transferir:
            res["motivo"] = decisao.motivo
            return res
        return _transferir(res, telefone, nome, tenant_id, decisao)
    except Exception as e:  # noqa: BLE001
        log.warning("handoff_remarketing_falhou", erro=str(e)[:160])
        res["motivo"] = "erro"
        return res


def processar_resposta(telefone: str, texto: str | None = None,
                       nome: str | None = None, tenant_id: int = 1) -> dict[str, Any]:
    """O card acabou de cair em Respondeu — no modo "todos", vai pra corretora.

    Chamado pelo webhook da linha da Corretor logo depois de
    `carrossel_corretor.processar_inbound` devolver `respondeu` ou `interesse_lido`
    (ou seja: a regra em código já pôs o card em Respondeu; recusa, parar,
    resposta automática e número errado NÃO chegam aqui). Não lê interesse:
    "bom dia", áudio e figurinha vão (Dona 05/10/2026: "todo mundo que cair em
    Respondeu vai para a roleta").

    No modo "interesse" não faz nada — quem decide é `processar`, dentro do
    atendimento da Pessoa, como era até 05/10.

    Só toca card que está NA ETAPA Respondeu com a Corretor. Best-effort: NUNCA
    levanta.
    """
    res: dict[str, Any] = {"transferiu": False, "motivo": None, "leads": []}
    try:
        from app.db.session import SessionLocal

        with SessionLocal() as s:
            try:
                cfg = quando_responde.ler(s, tenant_id)
            except quando_responde.ConfigInvalida as e:
                log.error("handoff_remarketing_nao_transferiu", motivo=e.motivo,
                          erro=str(e)[:160], tel=telefone[-4:])
                res["motivo"] = e.motivo
                res["detalhe"] = str(e)[:200]
                return res
            except config_operacional.ConfigIndisponivel as e:
                log.error("handoff_remarketing_nao_transferiu",
                          motivo="config_indisponivel", erro=str(e)[:140],
                          tel=telefone[-4:])
                res["motivo"] = "config_indisponivel"
                return res
        # (o cache acabou de ser relido: o interruptor é o de agora)
        if not config_operacional.handoff_remarketing_ativo():
            res["motivo"] = "desligado"
            return res
        if cfg.quem_vai != quando_responde.QUEM_TODOS:
            res["motivo"] = "modo_interesse"
            return res
        decisao = Decisao(
            transferir=True, sinal=SINAL_RESPONDEU,
            motivo="caiu em Respondeu (todo mundo que responde vai)",
            trecho=_trecho(texto),
        )
        res["decisao"] = decisao.model_dump()
        return _transferir(res, telefone, nome, tenant_id, decisao,
                           so_em_respondeu=True)
    except Exception as e:  # noqa: BLE001
        log.warning("handoff_remarketing_falhou", erro=str(e)[:160])
        res["motivo"] = "erro"
        return res
