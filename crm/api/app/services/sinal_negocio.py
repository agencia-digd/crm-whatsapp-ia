"""Acha negociação avançada LENDO A CONVERSA — não a etapa do card.

━━ POR QUE EXISTE ━━

Em 13/08 a Lucilene Rodrigues compareceu numa visita, entregou documentação e
ficou aguardando análise de crédito até dia 20. No sistema ela era: uma visita
marcada como NO-SHOW, um card em "Follow-up", e um lead na fila de remarketing
de 30 dias. Ninguém sabia que havia uma proposta viva — a Dona descobriu porque
me pediu pra ler as conversas.

A informação estava lá desde 14/08, escrita pelo próprio lead:
    "mais detalhada hoje — vamos aguardar até dia 20"

Tudo o que o copiloto olhava (etapa, tarefa, status de visita) depende de
alguém MOVER o card na mão. As corretoras movem quando lembram, e o dado que
importa mais — "essa pessoa está comprando" — mora na conversa, não no CRM.

━━ O QUE ELE FAZ ━━

Varre as conversas recentes procurando sinais de negociação avançada e devolve
uma lista pra avisar. Não move nada, não manda mensagem: só aponta.

Os sinais são o vocabulário de quem está FECHANDO, não de quem está olhando:
documentação, aprovação de crédito, cartório, sinal, assinatura, data marcada
com o banco. É diferente de interesse ("quero ver", "quanto custa") — interesse
o sistema já detecta e trata.

━━ POR QUE HEURÍSTICA E NÃO IA ━━

Isso precisa rodar de hora em hora, em cima de toda conversa ativa, e nunca
falhar. Uma lista de expressões é auditável (dá pra você ler e discordar),
custa zero e não inventa. Se ela deixar caso passar, a gente adiciona a
expressão — e o histórico fica no git.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import structlog
from sqlalchemy import text as sql_text

from app.db.session import SessionLocal

log = structlog.get_logger()

# ── os sinais, por peso ──────────────────────────────────────────────────────
#
# FORTE: só aparece quando o negócio está de fato em curso. Um destes já basta
# pra avisar.
_FORTE = re.compile(
    r"assinatura|assinei|assinar\s+(?:o\s+)?contrato"
    r"|cart[óo]rio|escritura|itbi"
    r"|entrei?\s+com\s+(?:a\s+)?documenta|entreguei\s+(?:a\s+)?document"
    r"|cr[ée]dito\s+(?:foi\s+)?aprovad|aprovaram\s+(?:meu\s+)?cr[ée]dito"
    r"|financiamento\s+aprovad|passou\s+(?:na\s+)?an[áa]lise"
    r"|dei\s+(?:o\s+)?sinal|paguei\s+(?:o\s+)?sinal|sinal\s+pago"
    r"|reserv(?:ei|ado)\s+(?:a\s+)?unidade|fechei\s+(?:com\s+)?voc[êe]s"
    r"|vou\s+fechar|quero\s+fechar",
    re.IGNORECASE,
)

# MÉDIO: sugere andamento, mas também aparece em conversa que não vai a lugar
# nenhum. Precisa de DOIS pra virar aviso — um sozinho gera alarme falso.
_MEDIO = re.compile(
    r"documenta[çc][ãa]o|documento|contrach|holerite|imposto\s+de\s+renda"
    r"|extrato|comprovante|certid[ãa]o"
    r"|an[áa]lise|banco|caixa\b|correspondente"
    r"|simula[çc][ãa]o|aprova[çc][ãa]o|score|serasa"
    r"|fgts|entrada\s+parcelad|subs[íi]dio"
    r"|proposta|neg[óo]cio|assessoria|advogad"
    r"|aguardar?\s+at[ée]\s+(?:dia|o\s+dia)|volto\s+(?:dia|no\s+dia)"
    r"|vou\s+levar\s+(?:os\s+)?document",
    re.IGNORECASE,
)

# Palavras que DERRUBAM o sinal: a conversa fala de negócio mas é a corretora
# explicando como funciona, ou é gente do mercado.
_RUIDO = re.compile(
    r"fazemos\s+parceria|sou\s+corretor|nossa\s+constru|da\s+construtora"
    r"|comiss[ãa]o|repasse|permuta\s+de|capta[çc][ãa]o"
    # Parceria B2B: CNPJ e liberação de assinatura eletrônica aparecem quando o
    # outro lado é EMPRESA, não comprador (caso Fulano, 13/08).
    r"|cnpj|assinatura\s+eletr[ôo]nica|libera[çc][ãa]o\s+para\s+voc[êe]s",
    re.IGNORECASE,
)


# CONTEXTO IMOBILIÁRIO — requisito positivo, e a trava mais eficaz de todas.
#
# Perseguir ruído com lista de proibições é jogo sem fim: cada palavra que eu
# bloqueio ("CNPJ") mata um caso legítimo, e cada uma que eu libero traz três
# falsos. Em 17/08 apareceram como "negociação" conversas da Pessoa12 sobre
# transferência de linkpay e liberação de assinatura — nada a ver com imóvel.
#
# Inverti: pra ser negociação de IMÓVEL, a conversa precisa falar de imóvel em
# algum momento. É o que separa "documentação + aprovação" de compra de
# "documentação + aprovação" de fornecedor.
_IMOVEL = re.compile(
    r"im[óo]vel|apartamento|\bapto\b|casa\b|terreno|sobrado|ch[áa]cara"
    r"|empreendimento|condom[íi]nio|unidade|planta|decorado|stand"
    r"|quarto|dormit[óo]rio|su[íi]te|garagem|vaga\b"
    r"|financiamento|minha\s+casa|mcmv|caixa\b|entrada\b|parcela"
    r"|visita|bairro|zona\s+(?:norte|sul|leste|oeste)|metro?s?\s*(?:quadrado|²|ms)\b"
    r"|aluguel|loca[çc][ãa]o|escritura|matr[íi]cula",
    re.IGNORECASE,
)


@dataclass
class Sinal:
    telefone: str
    nome: str | None
    corretora_id: int | None
    etapa: str | None
    forca: str                 # "forte" | "medio"
    trechos: list[str]
    ultima_msg_em: datetime | None


_SQL_CONVERSAS = """
WITH ativas AS (
    SELECT right(regexp_replace(coalesce(m.contato_telefone,''), '\\D', '', 'g'), 8) AS suf,
           max(m.contato_telefone)  AS telefone,
           max(m.contato_nome)      AS nome_wpp,
           max(m.corretora_id)      AS corretora_id,
           max(m.timestamp_evolution) AS ultima,
           string_agg(
               coalesce(m.conteudo_texto, m.transcricao, ''),
               ' ¶ ' ORDER BY m.timestamp_evolution
           ) AS falas
      FROM mensagens_whatsapp m
     WHERE m.tenant_id = 1
       AND m.timestamp_evolution > now() - make_interval(days => :dias)
       AND coalesce(m.tipo_mensagem, '') <> 'template'
       AND m.contato_telefone IS NOT NULL
     GROUP BY 1
)
SELECT a.suf, a.telefone, a.nome_wpp, a.corretora_id, a.ultima, a.falas,
       (SELECT l.nome FROM leads l
         WHERE right(regexp_replace(coalesce(l.telefone,''), '\\D', '', 'g'), 8) = a.suf
         LIMIT 1) AS nome_crm,
       (SELECT ps.nome FROM leads l
          JOIN pipeline_stages ps ON ps.id = l.stage_id
         WHERE right(regexp_replace(coalesce(l.telefone,''), '\\D', '', 'g'), 8) = a.suf
         LIMIT 1) AS etapa
  FROM ativas a
 WHERE length(a.suf) = 8
"""

# Etapas onde o negócio JÁ está reconhecido — não precisa avisar de novo.
ETAPAS_OK = {"Proposta", "Venda ganha", "Cliente", "Compareceu"}


# A EQUIPE conversa entre si pelas mesmas linhas, e conversa de trabalho é
# cheia de "documentação", "aprovação", "cartório". Sem esta trava a Pessoa10 aparece
# como negociação todo dia (aconteceu em 13/08).
_INTERNOS: set[str] | None = None


def _e_da_casa(suf: str) -> bool:
    global _INTERNOS
    if _INTERNOS is None:
        _INTERNOS = set()
        try:
            with SessionLocal() as s2:
                for fonte in (
                    "SELECT telefone FROM corretoras WHERE telefone IS NOT NULL",
                    "SELECT telefone_normalizado FROM internos_acme"
                    " WHERE telefone_normalizado IS NOT NULL",
                ):
                    for (tel,) in s2.execute(sql_text(fonte)).all():
                        d = "".join(c for c in (tel or "") if c.isdigit())[-8:]
                        if len(d) == 8:
                            _INTERNOS.add(d)
        except Exception as e:  # noqa: BLE001
            log.warning("sinal_internos_falhou", erro=str(e)[:120])
    return suf in _INTERNOS


def _trechos(falas: str, limite: int = 3) -> list[str]:
    """Os pedaços da conversa que dispararam o sinal — sem eles o aviso é uma
    afirmação sem prova, e quem recebe não tem como julgar."""
    out: list[str] = []
    for parte in (falas or "").split(" ¶ "):
        p = parte.strip()
        if not p or len(p) < 8:
            continue
        if _FORTE.search(p) or _MEDIO.search(p):
            out.append(p[:110])
        if len(out) >= limite:
            break
    return out


def procurar(*, dias: int = 7, tenant: int = 1) -> list[Sinal]:
    """Conversas com cara de negociação avançada nos últimos `dias`."""
    achados: list[Sinal] = []
    with SessionLocal() as s:
        linhas = s.execute(sql_text(_SQL_CONVERSAS), {"dias": dias}).mappings().all()

    for r in linhas:
        falas = r["falas"] or ""
        if not falas.strip():
            continue
        if _e_da_casa(r["suf"]):
            continue                       # equipe falando entre si (caso Pessoa10)
        etapa = r["etapa"]
        if etapa in ETAPAS_OK:
            continue                       # já reconhecido no CRM

        # Sem contexto de imóvel não é negociação nossa — é assunto de
        # fornecedor, plataforma ou banco pessoal.
        if not _IMOVEL.search(falas):
            continue

        forte = bool(_FORTE.search(falas))
        medios = len(set(m.group(0).lower() for m in _MEDIO.finditer(falas)))
        if not forte and medios < 2:
            continue

        # O RUÍDO DERRUBA SÓ O SINAL MÉDIO (17/08). Na primeira versão ele
        # derrubava tudo, e matou o Junyor Neres — que estava subindo a
        # documentação da Sarah e do João, esperando aprovação pra reservar a
        # unidade. Caiu porque a palavra "CNPJ" aparecia na conversa.
        #
        # Compra de imóvel MENCIONA CNPJ (empresa do comprador, MEI, imobiliária
        # no meio). Quem tem sinal FORTE — documentação entregue, crédito
        # aprovado, cartório — está negociando, e nenhuma palavra solta desmente
        # isso. Ruído só decide quando a evidência é fraca.
        if not forte and _RUIDO.search(falas):
            continue

        achados.append(Sinal(
            telefone=r["telefone"],
            nome=r["nome_crm"] or r["nome_wpp"],
            corretora_id=r["corretora_id"],
            etapa=etapa,
            forca="forte" if forte else "medio",
            trechos=_trechos(falas),
            ultima_msg_em=r["ultima"],
        ))

    # Forte primeiro, e dentro de cada grupo o mais recente antes: quem está
    # fechando agora não pode ficar embaixo de quem falou há uma semana.
    achados.sort(key=lambda a: (a.forca != "forte", -(a.ultima_msg_em.timestamp() if a.ultima_msg_em else 0)))
    log.info("sinal_negocio_procurou", dias=dias, achados=len(achados),
             fortes=sum(1 for a in achados if a.forca == "forte"))
    return achados


def como_texto(sinais: list[Sinal], limite: int = 8) -> str:
    """Formata pro Telegram da Dona. Curto: quem, em que etapa está (o erro) e
    a frase que prova."""
    if not sinais:
        return ""
    fortes = [s for s in sinais if s.forca == "forte"]
    linhas = [f"💰 *{len(sinais)} conversa(s) com cara de negócio andando*"]
    if fortes:
        linhas.append(f"({len(fortes)} com sinal forte)")
    linhas.append("")
    for s in sinais[:limite]:
        marca = "🔴" if s.forca == "forte" else "🟠"
        etapa = s.etapa or "sem card"
        linhas.append(f"{marca} *{s.nome or s.telefone[-4:]}* — está em _{etapa}_")
        if s.trechos:
            linhas.append(f'   "{s.trechos[0]}"')
    if len(sinais) > limite:
        linhas.append(f"\n… e mais {len(sinais) - limite}.")
    linhas.append("\nSe alguma estiver fechando, o card precisa subir.")
    return "\n".join(linhas)
