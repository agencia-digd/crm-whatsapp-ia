"""Porta de SAÍDA do remarketing — lead que volta a falar sai da fila.

Por que existe (medido em 19/09/2026, três casos reais da linha da Pessoa11):

  • Pessoa16 Cesarotti (…1978) — crédito aprovado em 31/08, conversa inteira na
    mão da Pessoa11. Em 07/09 recebeu "Te mandei umas mensagens e tentei te ligar, e
    não quero sumir nem insistir demais". O card estava em "Em atendimento" na
    hora do disparo; foi pra "Base" do funil remarketing em 15/09 (ver ACHADO 2).
  • Gabriel Proietti (…9853) — "Estou em negociação" em 25/07, documentação
    entregue. SEIS disparos de reengajamento em agosto, incluindo "Vi aqui que a
    sua busca de imóvel ficou parada". Respondeu "Pode mandar por favor" em
    21/08 e levou outro em 28/08.
  • William Feitosa (…0985) — quatro disparos, os dois últimos em 15/09 e 16/09.
    Em 17/09 a Pessoa11 montava a pasta de financiamento dele.

O ACHADO QUE MUDA O DIAGNÓSTICO: os três disparos indevidos saíram
respectivamente 7 dias e 3 minutos, 7 dias e 10 minutos e 7 dias e 2 horas
depois da última fala do lead. Não é coincidência: é a
`reengajamento_guard.conversa_humana_ativa` (JANELA_CONVERSA_HUMANA_DIAS = 7)
expirando. A trava FUNCIONOU e o disparo saiu no primeiro minuto em que ela
soltou. Ou seja: a casa protege o lead por 7 dias e nunca muda o ESTADO dele.
Enquanto o `cadencia_estado` do lead continuar 'ativa'/'remarketing', a régua
volta sozinha — com o negócio andando, 40 dias depois.

Por que o gancho que já existe não pega estes casos:

  1. `cadencia.processar_resposta_cadencia` faz a parada certa, mas só é chamado
     por `cloud_corretora_a.cloud_inbound` — a LINHA DA CORRETOR. Os disparos destes três
     saíram e as respostas chegaram em `acme_corretor`, que entra por
     `cloud_corretora.corretora_inbound` (rota magra: grava e nada mais).
  2. Mesmo NA linha da Pessoa12 ele não tira o lead da fila: `_STAGES_MOVIVEIS` é
     ("Novo", "Follow-up", "Primeiro Contato", "10000001") e as etapas do funil
     remarketing ("Base", "30 dias"…) não estão lá. O card fica na coluna de
     remarketing; o que para é a régua.
  3. `handoff_remarketing` faz a saída completa, mas só pra lead com owner = Corretor
     (4) vindo do carrossel. Os nossos têm owner = corretora humana.
  4. `disparos_reengajamento` (54 linhas, fluxo legado) não tem nenhum dos três
     — e seu monitor (`monitor_respostas_reeng.py`) só manda Telegram, não mexe
     em `stage_id`.

O ACHADO 2, que amplia o escopo: quem disparou nos três casos foi a CADÊNCIA DE 8
TOQUES (`cadencia_eventos` mostra os toques 0…8 pela linha da Pessoa11), matriculada
pelo `lead_spine._enroll_cadencia_followup` — a cadência do William (id 862)
nasceu no MESMO SEGUNDO em que a Pessoa11 arrastou o card dele pra "Follow-up"
(`stage_manual_v2`, 10/08 10:53:34). A régua obedece `cadencia_estado`, não a
etapa do card: o Pessoa16 levou o toque 6 em 07/09 estando em "Em atendimento"
(foi pra "Base" só em 15/09, pelo `nutricao_entrada`). O card em "Base" é o FIM
da engrenagem, não o começo.

O que ESTE módulo faz, então: escuta resposta em QUALQUER linha e decide uma de
quatro ações, para quem está numa etapa do funil `remarketing` OU tem
`cadencia_estado` viva. NUNCA manda mensagem — nem template, nem texto livre. É
estado, etapa e evento.

═══════════════════════════════════════════════════════════════════════════
AS QUATRO AÇÕES (e por que são quatro, não duas)
═══════════════════════════════════════════════════════════════════════════
As duas decisões são independentes, e tratá-las como uma só foi o que produziu
o "Kkkkk" continuar na fila:

  PARAR A RÉGUA  — qualquer resposta humana de verdade. Respondeu é respondeu:
                   `cadencia_estado.status` vira 'respondida' e o lead sai dos
                   DOIS motores de uma vez (`cadencia.tick` e `nutricao.tick`
                   filtram status IN ('ativa','remarketing')).
  PROMOVER       — resposta com SINAL de interesse (critério reaproveitado de
                   `handoff_remarketing.avaliar`): além de parar a régua, o card
                   volta pro funil principal.

Daí:
  • "promover" → para a régua + card pro funil principal;
  • "parar"    → só para a régua (respondeu, mas sem sinal: "Kkkkk", "bom dia",
                 um documento sem legenda — ou recusa branda, que não pode
                 voltar pro atendimento);
  • "perdido"  → pediu pra parar (régua em 'opt_out') ou disse que já resolveu /
                 desistiu (régua em 'ja_encontrou'): card pra "Venda perdida".
                 São as duas regras que a casa já tem — a da Dona 10/07 que
                 `_mover_lead_opt_out` aplica e a de 05/08 que
                 `cadencia._despedir_quem_ja_encontrou` aplica;
  • "nada"     → não houve resposta nova depois do último disparo, ou não há
                 nada a consertar (card fora da fila E nenhuma régua viva).

RÉGUA VIVA FORA DA FILA (o caso Pessoa16): card no funil principal + cadência
viva = a régua para e o CARD NÃO É TOCADO. Fora da fila não há promoção a fazer
(o lead já está no funil da corretora) e mexer na etapa de quem está em
atendimento seria rebaixar ou adivinhar. Motivo registrado como
`regua_viva_fora_da_fila:<motivo>` — 24 telefones nessa situação em 19/09.

ETAPA DE CHEGADA — decidida por EVIDÊNCIA, não por palpite
──────────────────────────────────────────────────────────
O handoff da Corretor usa "Novo" (6) e está certo lá: ninguém da casa tinha falado.
Aqui é diferente, e a pergunta da Dona ("'Em atendimento' me parece mais
honesto") tem resposta no banco: olho se alguém da CASA já escreveu pra essa
pessoa fora de template/automação (mesma regra do `atendimento_corretor`: robô carimba
`conteudo_metadata->>'origem'`, gente não).

  • humano já falou  → "Em atendimento" (7). É o caso dos três: a Pessoa11 conversou
    meses com eles. Pôr em "Novo" mandaria a corretora "fazer o primeiro
    contato" com quem já tem pasta de financiamento aberta.
  • humano nunca falou → "Novo" (6). O lead tem dona no cadastro, mas só recebeu
    automação; quem chega precisa ser abordado, e a coluna tem que dizer isso.

Etapas resolvidas por NOME dentro do funil (`resolve_stage_id_por_funil`), nunca
por id cravado: `pipeline_stages` tem "Novo" 4 e 6 e "Proposta" 2 e 11 — as
duplicatas são do tenant 2, e resolver por (tenant, funil, nome) passa longe
delas sem depender de eu ter decorado qual id é do quem.

═══════════════════════════════════════════════════════════════════════════
O QUE NÃO FAZ (e por quê)
═══════════════════════════════════════════════════════════════════════════
  • NÃO manda mensagem nenhuma. Nem aviso pro lead, nem pra corretora. A pessoa
    está conversando com a dona do card; o que faltava era o robô calar a boca.
  • NÃO troca o dono, e por isso NÃO usa `lead_transferencia.transferir_lead`:
    aqui não há transferência — o dono já é a corretora certa. Chamar aquela
    função gravaria um evento `transferencia` que nunca houve (o kanban desenha
    selo com ele) e ligaria `owner_editado_manual` sem ninguém ter editado dono.
    Do handoff eu reaproveito o que importa: o CRITÉRIO (`avaliar`).
  • NÃO toca em lead com owner = Corretor (4): esse é do `handoff_remarketing`, que
    além de promover escolhe destino na roleta. Dois serviços mexendo no mesmo
    card é o caminho para um desfazer o outro.
  • NÃO promove lead SEM DONO (487 hoje, o miolo da nutrição fria): promover
    criaria card órfão em "Em atendimento" e escolher dono é decisão da roleta
    (handoff) ou da adoção por ligação (`ligacao_resultado.adotar_lead`). Mas a
    régua PARA — quem respondeu não leva disparo, com dono ou sem.
  • NÃO escreve no Kommo (nunca escrevemos stage de volta: histórico de loop de
    PATCH).

TRAVA DE ARMAMENTO: nasce DESLIGADO. `ativa()` lê a chave
`saida_remarketing_ativa` do painel se ela existir e cai na env
`SAIDA_REMARKETING_ATIVA` (default False). A chave não está em
`config_operacional.DEFAULTS` de propósito — pôr lá é um arquivo que a Dona e
outro agente estão editando hoje; o dia que entrar, o painel passa a mandar sem
mudar uma linha daqui.

LIMITE CONHECIDO (não é este módulo que resolve): o espelho do Kommo pode
devolver o card pra etapa de lá. É por isso que o move grava também
`lead_events(tipo='stage_manual_v2')` — os 72h de proteção do
`lead_spine._manual_v2_recente`, mesmo recurso que `ligacao_resultado.adotar_lead`
usa. Passada a janela, se o Kommo insistir, o card oscila; o que NÃO oscila é a
parada da régua, que é o dano de verdade.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from app.db.models import CadenciaEstado, Lead, PipelineStage
from app.services import carrossel_corretor as cb
from app.services import config_operacional
from app.services import handoff_remarketing as hr
from app.services.atendimento_corretor import _e_recusa_branda, _pediu_parar
from app.services.cadencia import (
    CANAL_WHATSAPP,
    DEFAULT_TENANT_ID,
    STATUS_JA_ENCONTROU,
    STATUS_RESPONDIDA,
    TIPO_RESPOSTA,
    _e_auto_reply_conteudo,
    _ja_encontrou,
    _registrar_evento as _registrar_evento_cadencia,
    e_clique_de_botao,
)
from app.services.nutricao import FUNIL_REMARKETING
from app.services.opt_out import is_opt_out

log = structlog.get_logger()

# Quem assina o evento — é o que separa esta saída do botão do painel, do
# handoff da Corretor e da adoção por ligação no histórico do lead.
QUEM = "saida_remarketing"
EVENTO_SAIDA = "saida_remarketing"

FUNIL_PRINCIPAL = "principal"
STAGE_EM_ATENDIMENTO = "Em atendimento"
STAGE_NOVO = "Novo"
STAGE_VENDA_PERDIDA = "Venda perdida"
STATUS_OPT_OUT = "opt_out"

# Estados que ainda disparam: os dois motores (cadencia.tick e nutricao.tick)
# varrem exatamente estes.
STATUS_VIVOS = ("ativa", "remarketing")

# Sem disparo registrado pro telefone, "voltou a falar" é medido nesta janela.
# 7 dias = a mesma janela da `reengajamento_guard.conversa_humana_ativa`, que é
# o que a casa já chama de "conversa viva" — e cujo vencimento produziu os três
# casos de 19/09.
JANELA_SEM_DISPARO_DIAS = 7

# Tipos de mensagem que NÃO são fala: eventos de sistema e do próprio disparo.
# Base: `reengajamento_guard._TIPOS_NAO_HUMANOS`, mais figurinha — a lição do
# lead Fulano (31/07), que foi promovido por reagir com ❤️ a uma despedida.
_TIPOS_IGNORADOS: frozenset[str] = frozenset(
    {"template", "revoke", "reaction", "edit", "sticker"}
)

# Flag de armamento. Chave de painel primeiro (quando existir), env depois.
CHAVE_CONFIG = "saida_remarketing_ativa"
FLAG_ENV = "SAIDA_REMARKETING_ATIVA"
_VERDADEIROS = frozenset({"1", "true", "yes", "on", "sim"})


def ativa() -> bool:
    """Kill-switch. DESLIGADO por padrão.

    Painel > env, na ordem que o resto da casa usa. `get` levanta KeyError pra
    chave fora do mapa de `config_operacional.DEFAULTS` — enquanto a chave não
    estiver lá, vale a env, e nada muda quando ela entrar.
    """
    try:
        return bool(config_operacional.get(CHAVE_CONFIG))
    except KeyError:
        pass
    except Exception as e:  # noqa: BLE001 — banco fora não liga automação sozinho
        log.warning("saida_remarketing_config_indisponivel", erro=str(e)[:120])
    return (os.getenv(FLAG_ENV) or "").strip().lower() in _VERDADEIROS


# ── Classificação (pura: sem banco, sem rede) ────────────────────────────────

ACAO_PROMOVER = "promover"
ACAO_PARAR = "parar"
ACAO_PERDIDO = "perdido"
ACAO_NADA = "nada"


@dataclass
class Decisao:
    """O que fazer com este telefone. `falas` = o que entrou na conta."""

    acao: str
    motivo: str
    sinal: str | None = None
    trecho: str | None = None
    falas: int = 0

    def para_payload(self) -> dict[str, Any]:
        return {"acao": self.acao, "motivo": self.motivo, "sinal": self.sinal,
                "trecho": self.trecho, "falas": self.falas}


def _limpo(texto: str | None) -> str:
    return (texto or "").strip()


# VETO EXTRA DE RECUSA — reforço local, não cópia da régua.
#
# Achado no dry-run contra produção (19/09): "Oi Pessoa, apartamento não vou
# querer não, melhor deixar pra lá. Obrigada" vinha como PROMOVER. O
# `_RE_INTERESSE` casa "apartamento"; o `_RE_RECUSA_BRANDA` cobre "não quero
# comprar/procurar" e "não vou comprar", mas não "não vou querer" nem "deixar
# pra lá". É a mesma família dos ~19 falsos positivos que a Dona achou na
# curadoria do lote retroativo de 18/09.
#
# Por que aqui e não no `atendimento_corretor`: aquela regex é compartilhada com a
# Pessoa e com o handoff da Corretor (envio de template incluso), e mexer nela num dia
# em que dois agentes editam o repositório é trocar um falso positivo por um
# risco de regressão em três fluxos. Este veto só derruba a PROMOÇÃO — nunca
# transforma recusa em interesse, e a régua para do mesmo jeito.
_RE_RECUSA_EXTRA = re.compile(
    r"n[ãa]o\s+vou\s+(?:querer|ver|dar\s+andamento|seguir|continuar)"
    r"|n[ãa]o\s+(?:vai|d[áa])\s+(?:dar\s+)?(?:certo|pra\s+mim)"
    r"|(?:melhor\s+)?deixa(?:r)?\s+(?:isso\s+)?pra\s+l[áa]"
    r"|deixa\s+pra\s+outra\s+(?:hora|vez)"
    r"|por\s+(?:enquanto|hora)\s+n[ãa]o\s+vou"
    r"|n[ãa]o\s+(?:tenho|temos)\s+(?:mais\s+)?condi[çc]",
    re.IGNORECASE,
)


def _recusou(texto: str | None) -> bool:
    """Recusa que a régua do handoff não pega — ver `_RE_RECUSA_EXTRA`."""
    return bool(texto) and bool(_RE_RECUSA_EXTRA.search(texto))


def classificar(falas: list[tuple[str | None, str | None]]) -> Decisao:
    """Aplica a régua do docstring do módulo. `falas` = [(texto, tipo)], na
    ordem em que chegaram, já filtradas pela janela de quem chama.

    Pura de propósito: é a parte que os testes precisam poder exercitar com
    frases reais, sem banco.
    """
    reais: list[str] = []
    houve_fala = False          # mídia real conta como sinal de vida
    optou = False

    for texto, tipo in falas:
        t = _limpo(texto)
        if (tipo or "").lower() in _TIPOS_IGNORADOS:
            continue
        # Clique em botão não é resposta (regra Dona 05/08) — com UMA exceção:
        # "Parar mensagens" vale clicado do mesmo jeito.
        if e_clique_de_botao(tipo):
            if is_opt_out(t):
                optou = True
            continue
        # Auto-reply de empresa/secretária: não é gente. Mesmo detector da
        # cadência, pra as duas portas concordarem sobre o que é resposta.
        #
        # EXCEÇÃO (achada escrevendo os testes): o padrão `no momento (não|está)`
        # do detector casa "no momento não tenho interesse", que é gente dizendo
        # não — e ignorar isso deixaria a régua batendo em quem recusou, o pior
        # dos dois erros. Recusa branda e pedido de parar passam por cima do
        # veto de auto-reply; qualquer outra coisa com cara de secretária, não.
        if _e_auto_reply_conteudo(t) and not (
            _e_recusa_branda(t) or _pediu_parar(t) or is_opt_out(t)
        ):
            continue
        houve_fala = True
        if t and not hr._e_so_midia(t):
            reais.append(t)

    if optou or any(_pediu_parar(f) or is_opt_out(f) for f in reais):
        trecho = next((f for f in reais if _pediu_parar(f) or is_opt_out(f)), None)
        return Decisao(acao=ACAO_PERDIDO, motivo="opt_out",
                       trecho=hr._trecho(trecho), falas=len(reais))

    if not houve_fala:
        return Decisao(acao=ACAO_NADA, motivo="sem_resposta")

    if not reais:
        # Mídia sem texto nenhum (documento, imagem, localização): é resposta —
        # o Pessoa16 mandou a documentação em áudio e arquivo — mas não dá pra
        # ler interesse de um anexo. Para a régua, não promove.
        return Decisao(acao=ACAO_PARAR, motivo="so_midia", falas=0)

    # O CRITÉRIO é o do handoff da Corretor, inteiro: vetos (opt-out, recusa branda
    # sem interesse) e sinais (quer_vender, interesse, afirmativa, engajou).
    # Não reescrevo aqui — duas cópias da régua divergem na primeira correção.
    decisao = hr.avaliar(list(reais))
    if decisao.transferir:
        recusa = next((f for f in reais if _recusou(f)), None)
        if recusa is not None:
            # Disse que não com palavras que a régua do handoff não conhece.
            # Não volta pro atendimento; a régua para.
            return Decisao(acao=ACAO_PARAR, motivo="recusa",
                           trecho=hr._trecho(recusa), falas=len(reais))
        return Decisao(acao=ACAO_PROMOVER, motivo=decisao.motivo,
                       sinal=decisao.sinal, trecho=decisao.trecho,
                       falas=len(reais))
    if decisao.motivo == "pediu_parar":
        return Decisao(acao=ACAO_PERDIDO, motivo="opt_out",
                       trecho=decisao.trecho, falas=len(reais))

    # FIM DE LINHA ≠ "agora não". A casa já separa as duas coisas: quem diz "já
    # comprei", "já resolvi" ou "desisti" vai pra "Venda perdida" (é o que
    # `cadencia._despedir_quem_ja_encontrou` faz, e é o que a Dona escreveu no
    # levantamento de 19/09 sobre o Carlos Augusto — "No momento desisti de
    # comprar qualquer coisa nesse momento", parado em "30 dias": *deveria: Venda
    # perdida*). Uso o MESMO detector da cadência pra as duas portas não
    # discordarem sobre o que é fim de linha.
    #
    # Depois do `avaliar` de propósito: se na mesma janela a pessoa também
    # mostrou interesse, o interesse ganha (caso Renato, a permuta) — a mesma
    # ordem que o handoff usa no V2.
    fim = next((f for f in reais if _ja_encontrou(f)), None)
    if fim is not None:
        return Decisao(acao=ACAO_PERDIDO, motivo="ja_encontrou",
                       trecho=hr._trecho(fim), falas=len(reais))

    if decisao.motivo == "recusa":
        # "Não tenho interesse", "no momento não": NÃO volta pro atendimento
        # (pedido explícito da Dona) e também não segue na régua — insistir com
        # quem disse não é o caminho curto pra denúncia. O card fica onde está:
        # "agora não" não é perda declarada, e quem encerra isso é gente.
        return Decisao(acao=ACAO_PARAR, motivo="recusa",
                       trecho=decisao.trecho, falas=len(reais))
    # Falou, mas sem sinal ("Kkkkk", "bom dia", "?"). Respondeu é respondeu:
    # régua para, card fica onde está.
    return Decisao(acao=ACAO_PARAR, motivo=decisao.motivo or "sem_sinal",
                   trecho=decisao.trecho, falas=len(reais))


# ── Leitura do banco ─────────────────────────────────────────────────────────

_SUF = r"right(regexp_replace(coalesce({col}, ''), '\D', '', 'g'), 8)"

# Leads deste telefone parados numa etapa do funil remarketing. Owner = Corretor fica
# FORA: aquele é o território do `handoff_remarketing`.
_SQL_LEADS_NA_FILA = rf"""
    SELECT l.id
      FROM leads l
      JOIN pipeline_stages ps ON ps.id = l.stage_id
     WHERE l.tenant_id = :t
       AND ps.funil = :funil
       AND {_SUF.format(col='l.telefone')} = :suf
       AND coalesce(l.owner_id, 0) <> :corretor
     ORDER BY l.id
"""

# Último disparo AUTOMÁTICO pro telefone, em qualquer linha. Template é o
# reengajamento; `origem` no metadata é a marca que todo robô da casa carimba.
_SQL_ULTIMO_DISPARO = rf"""
    SELECT max(timestamp_evolution)
      FROM mensagens_whatsapp
     WHERE {_SUF.format(col='contato_telefone')} = :suf
       AND from_corretora IS TRUE
       AND (coalesce(tipo_mensagem, '') = 'template'
            OR conteudo_metadata ->> 'origem' IS NOT NULL)
"""

# Falas do LEAD depois de `desde`, em qualquer linha, mais antiga primeiro (a
# ordem importa pro `_engajou` e pro trecho do evento). 40 = teto de sanidade:
# conversa de negociação tem centenas de mensagens e o que decide são as de
# depois do disparo.
_SQL_FALAS_DEPOIS = rf"""
    SELECT coalesce(nullif(m.conteudo_texto, ''), m.transcricao) AS txt,
           m.tipo_mensagem
      FROM mensagens_whatsapp m
     WHERE {_SUF.format(col='m.contato_telefone')} = :suf
       AND m.from_corretora IS FALSE
       AND m.timestamp_evolution > :desde
     ORDER BY m.timestamp_evolution
     LIMIT 40
"""

# Alguém da CASA já escreveu pra essa pessoa fora de automação? Mesma régua do
# `atendimento_corretor._SQL_HUMANO_FALOU`: robô carimba `origem`/`template`, gente
# não. Sem janela de tempo — a pergunta é "a corretora conhece esse lead?".
_SQL_HUMANO_JA_FALOU = rf"""
    SELECT 1
      FROM mensagens_whatsapp
     WHERE {_SUF.format(col='contato_telefone')} = :suf
       AND from_corretora IS TRUE
       AND coalesce(tipo_mensagem, '') <> 'template'
       AND conteudo_metadata ->> 'origem' IS NULL
       AND conteudo_metadata ->> 'template' IS NULL
     LIMIT 1
"""


def _suf8(telefone: str | None) -> str:
    return re.sub(r"\D", "", telefone or "")[-8:]


def _ultimo_disparo(session: Session, suf: str) -> datetime | None:
    try:
        return session.execute(sql_text(_SQL_ULTIMO_DISPARO), {"suf": suf}).scalar()
    except Exception as e:  # noqa: BLE001
        log.warning("saida_remarketing_disparo_indisponivel", suf=suf[-4:],
                    erro=str(e)[:120])
        return None


def _falas_depois(session: Session, suf: str,
                  desde: datetime) -> list[tuple[str | None, str | None]]:
    try:
        return [(r[0], r[1]) for r in session.execute(
            sql_text(_SQL_FALAS_DEPOIS), {"suf": suf, "desde": desde}).all()]
    except Exception as e:  # noqa: BLE001
        log.warning("saida_remarketing_falas_indisponiveis", suf=suf[-4:],
                    erro=str(e)[:120])
        return []


def _humano_ja_falou(session: Session, suf: str) -> bool:
    """Erro de consulta devolve True → o card cai em "Em atendimento".

    É o lado barato de errar: "Em atendimento" para quem nunca foi abordado
    custa um card numa coluna morna; "Novo" para quem já tem pasta de
    financiamento aberta manda a corretora se apresentar a um cliente.
    """
    try:
        return bool(session.execute(
            sql_text(_SQL_HUMANO_JA_FALOU), {"suf": suf}).first())
    except Exception as e:  # noqa: BLE001
        log.warning("saida_remarketing_humano_indisponivel", suf=suf[-4:],
                    erro=str(e)[:120])
        return True


def desde_quando(session: Session, suf: str, agora: datetime) -> datetime:
    """A partir de quando uma fala conta como "voltou a falar".

    O último disparo automático, quando existe: responder ao disparo é
    exatamente o sinal que a régua ignorou. Sem disparo registrado, os últimos
    `JANELA_SEM_DISPARO_DIAS` dias.
    """
    ultimo = _ultimo_disparo(session, suf)
    if ultimo is None:
        return agora - timedelta(days=JANELA_SEM_DISPARO_DIAS)
    if ultimo.tzinfo is None:  # banco de teste devolve naive (UTC)
        ultimo = ultimo.replace(tzinfo=timezone.utc)
    return ultimo


def avaliar_telefone(session: Session, telefone: str, *,
                     agora: datetime | None = None,
                     texto_agora: str | None = None,
                     tipo_agora: str | None = None) -> Decisao:
    """Classifica este telefone a partir do que ele falou depois do disparo.

    `texto_agora`/`tipo_agora`: a mensagem que ACABOU de chegar no inbound. Entra
    na conta mesmo que a gravação no thread ainda não tenha comitado — o webhook
    grava e chama na mesma requisição, e depender da ordem seria perder a
    primeira resposta de quem só fala uma vez.
    """
    agora = agora or datetime.now(timezone.utc)
    suf = _suf8(telefone)
    if not suf:
        return Decisao(acao=ACAO_NADA, motivo="sem_telefone")
    falas = _falas_depois(session, suf, desde_quando(session, suf, agora))
    if texto_agora is not None or tipo_agora is not None:
        par = (texto_agora, tipo_agora)
        if par not in falas:
            falas.append(par)
    return classificar(falas)


# ── Mutação ──────────────────────────────────────────────────────────────────


def _cadencias_vivas(session: Session, suf: str,
                     tenant_id: int) -> list[CadenciaEstado]:
    """As cadências ainda disparando deste telefone.

    TODAS as do sufixo, não só a primeira: enquanto existirem cadências gêmeas
    do mesmo número (grafias '+55…'/'55…' do seed legado), parar uma deixava a
    irmã tocando — o mesmo motivo que fez `processar_resposta_cadencia` virar
    plural.

    O filtro de sufixo vai no SQL (e não em Python, como o hook da cadência faz)
    porque aqui existe o TICK: carregar todas as cadências vivas do tenant uma
    vez por telefone seriam mil varreduras da tabela num lote de mil.
    """
    return (
        session.query(CadenciaEstado)
        .filter(CadenciaEstado.tenant_id == tenant_id,
                CadenciaEstado.status.in_(STATUS_VIVOS))
        .filter(sql_text(
            r"right(regexp_replace(coalesce(telefone, ''), '\D', '', 'g'), 8)"
            " = :suf_cad").bindparams(suf_cad=suf))
        .all()
    )


def _parar_regua(session: Session, suf: str, *, status: str, motivo: str,
                 agora: datetime, tenant_id: int) -> list[int]:
    """Marca as cadências vivas deste telefone e devolve os ids tocados."""
    tocadas = _cadencias_vivas(session, suf, tenant_id)
    for est in tocadas:
        est.status = status
        est.atualizado_em = agora
        # Evento no MESMO log que a cadência usa (`tipo='resposta'`, toque=None
        # pra não bater no UNIQUE de toque): quem audita a régua de um lead lê
        # um lugar só.
        _registrar_evento_cadencia(
            session, est, toque=None, tipo=TIPO_RESPOSTA, canal=CANAL_WHATSAPP,
            payload={"origem": QUEM, "motivo": motivo, "status": status,
                     "toque_atual": est.toque_atual},
            agora=agora,
        )
    return [int(e.id) for e in tocadas]


def _stage_id(session: Session, tenant_id: int, funil: str, nome: str) -> int | None:
    from app.services.lead_spine import resolve_stage_id_por_funil

    return resolve_stage_id_por_funil(session, tenant_id, funil, nome)


def _mover_card(session: Session, lead: Lead, *, stage_destino: int,
                stage_nome: str, agora: datetime, decisao: Decisao,
                tenant_id: int) -> None:
    """Move o card e deixa o rastro. NÃO faz commit (quem abriu a sessão fecha).

    Grava DOIS eventos, de propósito:
      • `stage_manual_v2` — os 72h em que o espelho do Kommo não sobrescreve o
        stage (`lead_spine._manual_v2_recente`). Mesmo recurso que a adoção por
        ligação usa; sem ele o sync devolve o card pra etapa do Kommo e o lead
        reaparece na fila amanhã.
      • `saida_remarketing` — a auditoria de verdade: o que a pessoa disse, qual
        sinal casou, de onde saiu e pra onde foi.
    """
    from app.services.lead_spine import _registrar_evento as _registrar_lead_event

    stage_de = int(lead.stage_id) if lead.stage_id is not None else None
    lead.stage_id = stage_destino
    lead.updated_at = agora
    # Tag "veio do remarketing" no card (campo datado, escrito UMA vez): a
    # corretora vê a procedência sem abrir o histórico. Vale aqui tanto quanto
    # no handoff da Corretor — o lead estava, de fato, numa etapa do remarketing.
    if lead.veio_remarketing_em is None:
        lead.veio_remarketing_em = agora

    _registrar_lead_event(
        session, tenant_id, int(lead.id), "stage_manual_v2",
        {"origem": stage_de, "destino": stage_destino,
         "stage_destino": stage_nome, "funil": FUNIL_PRINCIPAL,
         "quem": QUEM, "via": EVENTO_SAIDA},
    )
    _registrar_lead_event(
        session, tenant_id, int(lead.id), EVENTO_SAIDA,
        {"quem": QUEM, "stage_de": stage_de, "stage_para": stage_destino,
         "stage_destino": stage_nome, "owner_id": lead.owner_id,
         **decisao.para_payload()},
    )


@dataclass
class Resultado:
    """O que aconteceu com UM telefone."""

    telefone: str
    acao: str
    motivo: str
    sinal: str | None = None
    trecho: str | None = None
    leads: list[int] = field(default_factory=list)
    stage_para: str | None = None
    cadencias: list[int] = field(default_factory=list)
    aplicado: bool = False

    def resumo(self) -> str:
        alvo = f" → {self.stage_para}" if self.stage_para else ""
        sinal = f" ({self.sinal})" if self.sinal else ""
        return (f"{self.telefone[-4:]} · {self.acao}{alvo} · {self.motivo}{sinal}"
                f" · leads={self.leads or '-'} · cadencias={self.cadencias or '-'}")


def aplicar(session: Session, telefone: str, decisao: Decisao, *,
            agora: datetime | None = None, tenant_id: int = DEFAULT_TENANT_ID,
            dry_run: bool = False) -> Resultado:
    """Executa a decisão. NÃO faz commit — quem abriu a sessão decide.

    Em `dry_run` lê tudo (inclusive a etapa de destino, pro relatório bater com
    o que o envio real faria) e não escreve nada.
    """
    agora = agora or datetime.now(timezone.utc)
    suf = _suf8(telefone)
    res = Resultado(telefone=telefone, acao=decisao.acao, motivo=decisao.motivo,
                    sinal=decisao.sinal, trecho=decisao.trecho)
    if decisao.acao == ACAO_NADA or not suf:
        return res

    ids = [int(r[0]) for r in session.execute(
        sql_text(_SQL_LEADS_NA_FILA),
        {"t": tenant_id, "funil": FUNIL_REMARKETING, "suf": suf,
         "corretor": cb.CORRETORA_CORRETOR},
    ).all()]
    if not ids:
        # O card não está no funil remarketing (ou o único é da Corretor) — mas a
        # RÉGUA pode estar viva mesmo assim, e é ela que manda o disparo.
        #
        # Este é o caso Pessoa16 (07/09): quando o toque 6 saiu pela linha da
        # Pessoa11, o card dele estava em "Em atendimento" (7) — a Pessoa11 tinha movido
        # em 28/08. Ele só foi pra "Base" em 15/09, quando a Fase 1 da nutrição
        # puxou (`lead_events nutricao_entrada`). Quem disparou foi a CADÊNCIA DE
        # 8 TOQUES, que obedece `cadencia_estado` e não olha a etapa do card.
        # Sem este ramo, o conserto não impediria a repetição do caso que o
        # motivou.
        #
        # Aqui a régua para e o CARD NÃO É TOCADO: fora do funil remarketing não
        # há promoção a fazer (o lead já está no funil da corretora), e mexer na
        # etapa de quem está em atendimento seria rebaixar ou adivinhar.
        vivas = _cadencias_vivas(session, suf, tenant_id)
        if not vivas:
            res.acao = ACAO_NADA
            res.motivo = "fora_da_fila"
            return res
        res.acao = ACAO_PARAR
        res.motivo = f"regua_viva_fora_da_fila:{decisao.motivo}"
        if dry_run:
            res.cadencias = [int(e.id) for e in vivas]
            return res
        res.cadencias = _parar_regua(session, suf, status=STATUS_RESPONDIDA,
                                     motivo=res.motivo, agora=agora,
                                     tenant_id=tenant_id)
        res.aplicado = True
        log.info("saida_remarketing_regua_parada", tel=telefone[-4:],
                 motivo=res.motivo, cadencias=res.cadencias)
        return res

    leads = session.query(Lead).filter(Lead.id.in_(ids)).all()
    promovivel = [l for l in leads if l.owner_id is not None]

    stage_nome: str | None = None
    if decisao.acao == ACAO_PERDIDO:
        stage_nome = STAGE_VENDA_PERDIDA
    elif decisao.acao == ACAO_PROMOVER:
        if not promovivel:
            # Lead sem dono: a régua para, o card não anda (ver docstring).
            res.acao = ACAO_PARAR
            res.motivo = "sem_dono"
        else:
            stage_nome = (STAGE_EM_ATENDIMENTO if _humano_ja_falou(session, suf)
                          else STAGE_NOVO)

    destino: int | None = None
    if stage_nome is not None:
        destino = _stage_id(session, tenant_id, FUNIL_PRINCIPAL, stage_nome)
        if destino is None:
            # Etapa ausente no catálogo: não invento coluna nem chuto id.
            log.error("saida_remarketing_stage_ausente", etapa=stage_nome,
                      tenant_id=tenant_id)
            res.acao = ACAO_PARAR
            res.motivo = "stage_ausente"
            stage_nome = None

    alvos = promovivel if decisao.acao == ACAO_PROMOVER else leads
    if stage_nome is not None and destino is not None:
        res.stage_para = stage_nome
        res.leads = [int(l.id) for l in alvos]

    if dry_run:
        # O que a régua PARARIA — sem escrever. Contado pelo MESMO caminho do
        # modo real, pra o relatório do --dry-run não prometer mais do que a
        # aplicação faz (foi o defeito que a Dona apontou no dry-run do aviso:
        # "mandaria 8" onde o envio mandava 3).
        res.cadencias = [int(e.id) for e in _cadencias_vivas(session, suf, tenant_id)]
        return res

    # O status diz QUAL fim de linha foi: 'opt_out' é pedido de parar (tem peso
    # legal e trava de reenvio), 'ja_encontrou' é a pessoa tendo resolvido — a
    # cadência já separa os dois, e misturar apaga a diferença entre "não quis" e
    # "não precisa mais". Qualquer um deles tira o lead dos dois motores.
    if decisao.acao == ACAO_PERDIDO:
        status = (STATUS_JA_ENCONTROU if decisao.motivo == "ja_encontrou"
                  else STATUS_OPT_OUT)
    else:
        status = STATUS_RESPONDIDA
    res.cadencias = _parar_regua(session, suf, status=status,
                                 motivo=decisao.motivo, agora=agora,
                                 tenant_id=tenant_id)
    if stage_nome is not None and destino is not None:
        for lead in alvos:
            # Confere a etapa na hora de escrever: entre a leitura e agora
            # alguém pode ter arrastado o card pra fora do remarketing, e aí
            # este move seria um rebaixamento.
            atual = session.get(PipelineStage, lead.stage_id) if lead.stage_id else None
            if atual is None or (atual.funil or "").strip() != FUNIL_REMARKETING:
                res.leads = [i for i in res.leads if i != int(lead.id)]
                continue
            _mover_card(session, lead, stage_destino=destino,
                        stage_nome=stage_nome, agora=agora, decisao=decisao,
                        tenant_id=tenant_id)
    res.aplicado = True
    log.info("saida_remarketing_aplicada", tel=telefone[-4:], acao=res.acao,
             motivo=res.motivo, sinal=res.sinal, leads=res.leads,
             stage_para=res.stage_para, cadencias=res.cadencias)
    return res


# ── Porta 1: inbound (qualquer linha) ────────────────────────────────────────


def processar_inbound(telefone: str, texto: str | None = None, *,
                      tipo_mensagem: str | None = None,
                      tenant_id: int = DEFAULT_TENANT_ID,
                      session: Session | None = None) -> str | None:
    """Gancho do webhook: o lead falou. Devolve a ação tomada, ou None.

    Best-effort ABSOLUTO: nunca levanta. Um erro aqui não pode derrubar a
    gravação da mensagem no thread — a conversa da corretora vale mais que a
    nossa arrumação de card.
    """
    try:
        if not ativa():
            return None
        suf = _suf8(telefone)
        if not suf:
            return None

        def _corpo(s: Session) -> str:
            decisao = avaliar_telefone(s, telefone, texto_agora=texto,
                                       tipo_agora=tipo_mensagem)
            if decisao.acao == ACAO_NADA:
                return ACAO_NADA
            res = aplicar(s, telefone, decisao, tenant_id=tenant_id)
            s.commit()
            return res.acao

        if session is not None:
            return _corpo(session)
        from app.db.session import SessionLocal

        with SessionLocal() as s:
            return _corpo(s)
    except Exception as e:  # noqa: BLE001 — nunca derruba o inbound
        log.warning("saida_remarketing_inbound_erro", erro=str(e)[:160])
        return None


# ── Porta 2: tick por estado (o acervo) ──────────────────────────────────────

# Telefones parados no funil remarketing, um por número. O inbound só alcança
# quem fala DE NOVO a partir de agora; em 19/09 havia 70 leads que já tinham
# respondido depois do último disparo e seguiam na fila (16 na "30 dias" da
# Pessoa10, 17 na "Base" da Pessoa12, 8 na "Respondeu"…). Este tick é a porta que alcança
# esse acervo — e o que faz a correção não depender de a pessoa escrever mais uma
# vez.
#
# São DUAS origens, e as duas precisam estar aqui:
#   (a) card numa etapa do funil remarketing (a fila visível no board);
#   (b) `cadencia_estado` ainda viva — a régua que realmente dispara, e que não
#       olha a etapa do card. É por (b) que o Pessoa16 aparece: em 07/09 ele
#       levou o toque 6 estando em "Em atendimento".
_SQL_FILA = rf"""
    SELECT suf, min(telefone) AS telefone
      FROM (
        SELECT {_SUF.format(col='l.telefone')} AS suf, l.telefone
          FROM leads l
          JOIN pipeline_stages ps ON ps.id = l.stage_id
         WHERE l.tenant_id = :t
           AND ps.funil = :funil
           AND coalesce(l.owner_id, 0) <> :corretor
           AND coalesce(l.telefone, '') <> ''
           AND l.telefone_invalido IS NOT TRUE
        UNION ALL
        SELECT {_SUF.format(col='c.telefone')} AS suf, c.telefone
          FROM cadencia_estado c
         WHERE c.tenant_id = :t
           AND c.status IN ('ativa', 'remarketing')
           AND coalesce(c.telefone, '') <> ''
      ) u
     WHERE suf <> ''
     GROUP BY suf
     ORDER BY suf
"""


@dataclass
class Relatorio:
    considerados: int = 0
    por_acao: dict[str, int] = field(default_factory=dict)
    resultados: list[Resultado] = field(default_factory=list)
    dry_run: bool = True

    def conta(self, acao: str) -> None:
        self.por_acao[acao] = self.por_acao.get(acao, 0) + 1

    def linha_final(self) -> str:
        modo = "SIMULAÇÃO" if self.dry_run else "APLICADO"
        detalhe = ", ".join(f"{k}={v}" for k, v in sorted(self.por_acao.items()))
        return f"{modo} · {self.considerados} telefones · {detalhe or '-'}"


def rodar(*, dry_run: bool = True, limite: int = 50,
          tenant_id: int = DEFAULT_TENANT_ID, telefone: str | None = None,
          agora: datetime | None = None,
          session: Session | None = None) -> Relatorio:
    """Varre a fila do remarketing e aplica (ou simula) a saída.

    `limite` conta só quem TEM ação — um lote de 50 correções, não 50 leituras.
    `telefone` restringe a um número (o caminho de conferência caso a caso).
    """
    agora = agora or datetime.now(timezone.utc)
    rel = Relatorio(dry_run=dry_run)

    def _corpo(s: Session) -> Relatorio:
        alvo = _suf8(telefone) if telefone else None
        fila = s.execute(sql_text(_SQL_FILA), {
            "t": tenant_id, "funil": FUNIL_REMARKETING, "corretor": cb.CORRETORA_CORRETOR,
        }).all()
        agindo = 0
        for suf, fone in fila:
            if alvo and suf != alvo:
                continue
            rel.considerados += 1
            decisao = avaliar_telefone(s, fone, agora=agora)
            if decisao.acao == ACAO_NADA:
                rel.conta(ACAO_NADA)
                continue
            try:
                res = aplicar(s, fone, decisao, agora=agora, tenant_id=tenant_id,
                              dry_run=dry_run)
            except Exception as e:  # noqa: BLE001
                # Um telefone com problema não pode derrubar o lote nem deixar a
                # transação suja pro próximo — o resto da fila continua.
                s.rollback()
                rel.conta("erro")
                log.warning("saida_remarketing_lead_falhou", tel=fone[-4:],
                            erro=str(e)[:160])
                continue
            rel.conta(res.acao)
            if res.acao == ACAO_NADA:
                continue
            rel.resultados.append(res)
            if not dry_run:
                s.commit()
            agindo += 1
            if agindo >= limite:
                break
        return rel

    if session is not None:
        return _corpo(session)
    from app.db.session import SessionLocal

    with SessionLocal() as s:
        return _corpo(s)
