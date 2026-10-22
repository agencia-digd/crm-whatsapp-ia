"""Ligação: resultado, adoção do lead e métrica.

Concluir uma tarefa de ligação não deixava rastro nenhum: quem abria a conversa
do lead depois via um vão entre mensagens e não sabia que houve ligação, e a
Assistente seguia sugerindo "tenta entrar em contato" pra quem já tinha sido
ligado no mesmo dia.

TRÊS ESTADOS (um clique cada — concluir ligação é ação de rotina, dezenas de
vezes por dia; formulário mata o registro e a gente perde o dado que já tem):

    nao_atendeu             → atendeu=False, quis_continuar=None
    atendeu_sem_continuar   → atendeu=True,  quis_continuar=False
    atendeu_quer_continuar  → atendeu=True,  quis_continuar=True   ⭐ adota

O registro faz DUAS coisas de uma vez:
  1. NOTA na conversa do lead (`observacao_corretora`) — é o que o
     `GET /painel/lead/{lead_id}/notas` lê e o que a query de oportunidades
     (`/painel/atendimento/oportunidades`, campo `observacao.texto`) devolve
     como última observação. Ou seja: entra no histórico que a Assistente e o
     Inspector já leem, sem canal novo;
  2. LINHA ESTRUTURADA em `ligacao_resultado` — a fonte da métrica.

E no estado ⭐ faz uma TERCEIRA: a ADOÇÃO. Os leads que a corretora liga são os
do remarketing, que não têm dono — a ligação é o momento em que ela adota o
lead. Ver `adotar_lead`.

TRAVAS DE NÚMERO (valem em todo número deste painel):
  - "sem dado" ≠ 0%: taxa é None quando não há amostra, nunca zero;
  - abaixo de AMOSTRA_MINIMA a taxa vem marcada como ruído;
  - ligação concluída SEM resultado registrado (as antigas, e quem pular o
    passo) conta como `sem_registro` — NUNCA como "não atendeu".
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

import structlog
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from app.services import reengajamento_guard
from app.services.carrossel_corretor import CORRETORA_CORRETOR

log = structlog.get_logger()

DEFAULT_TENANT_ID = 1
OBSERVACAO_MAX = 500

ORIGEM_CADENCIA = "cadencia"      # origem_id = cadencia_estado.id
ORIGEM_TAREFA = "tarefa"          # origem_id = tarefas_corretora.id
ORIGEM_REMARKETING = "remarketing"  # origem_id = leads.id (janela de ligação)
ORIGENS = (ORIGEM_CADENCIA, ORIGEM_TAREFA, ORIGEM_REMARKETING)

# `toque` = a SEQUÊNCIA do registro dentro da origem.
#   - cadência: o índice do toque da régua (1/4/6);
#   - tarefa manual: 0 (a tarefa é única, e concluir 2x é a mesma ligação);
#   - remarketing: o NÚMERO DA TENTATIVA (1, 2, 3…) — a corretora liga de novo
#     pra quem não atendeu, e cada tentativa é um registro próprio. Sem isso o
#     UNIQUE trancaria a segunda ligação pro mesmo lead.
# Ele nunca é NULL porque NULL não colide com NULL em Postgres — o UNIQUE de
# idempotência viraria decoração.
TOQUE_MANUAL = 0

# Os três estados, na ordem em que aparecem na tela.
RESULTADO_NAO_ATENDEU = "nao_atendeu"
RESULTADO_SEM_CONTINUAR = "atendeu_sem_continuar"
RESULTADO_QUER_CONTINUAR = "atendeu_quer_continuar"

# estado → (atendeu, quis_continuar). `quis_continuar` é None quando não
# atendeu: a pergunta nem chega a ser feita, e inventar False viraria número
# errado na taxa de continuidade.
ESTADOS: dict[str, tuple[bool, bool | None]] = {
    RESULTADO_NAO_ATENDEU: (False, None),
    RESULTADO_SEM_CONTINUAR: (True, False),
    RESULTADO_QUER_CONTINUAR: (True, True),
}

# Referência histórica (planilha de março/2026, medição manual): 1.095 ligações,
# 31% de atendimento, 39% das atendidas prosseguiram → 100 → 30 → 12.
# É REFERÊNCIA, NÃO META: aquele número veio de um time de 5 pessoas com hora
# fixa do dia pra ligar; hoje são 3 corretoras sem horário reservado. Por
# corretora o atendimento variava de 16% a 51% — a comparação útil é a dela
# contra ela mesma ao longo do tempo, não contra a média.
REFERENCIA_HISTORICA: dict[str, Any] = {
    "periodo": "março/2026",
    "ligacoes": 1095,
    "taxa_atendimento": 31.0,
    "taxa_continuidade": 39.0,
    "resumo": "100 → 30 → 12",
    "nota": (
        "Referência histórica, não meta: medida à mão em março/2026 com um time "
        "de 5 pessoas e hora fixa do dia só pra ligar. Hoje são 3 corretoras sem "
        "horário reservado — e o atendimento variava de 16% a 51% entre elas. "
        "A comparação que vale é a sua contra a sua, ao longo do tempo."
    ),
}

# Tarefa MANUAL que é de ligação. Heurística de texto de propósito: a corretora
# escreve o texto livre ("ligar", "Ligação de retorno"), não existe tipo no
# banco. Serve só pra definir o UNIVERSO da métrica (quantas ligações eram pra
# ter sido feitas); o registro em si não depende dela.
_RE_TEXTO_LIGACAO = re.compile(r"lig(ar|ou|a[cç][aã]o|a[cç][oõ]es|amos)", re.IGNORECASE)


def texto_e_de_ligacao(texto: str | None) -> bool:
    """True quando o texto da tarefa manual descreve uma ligação."""
    return bool(_RE_TEXTO_LIGACAO.search(texto or ""))


@dataclass(frozen=True)
class ResultadoLigacao:
    """Os três dados do registro. `quis_continuar` só existe quando atendeu."""

    atendeu: bool
    quis_continuar: bool | None = None
    observacao: str | None = None

    @classmethod
    def do_estado(cls, estado: str, observacao: str | None = None) -> ResultadoLigacao:
        """Constrói a partir de um dos três estados da tela. ValueError se o
        estado for desconhecido — não inventamos default (default silencioso
        aqui viraria 'não atendeu' fantasma na métrica)."""
        if estado not in ESTADOS:
            raise ValueError(f"resultado_invalido: {estado}")
        atendeu, quis = ESTADOS[estado]
        return cls(atendeu=atendeu, quis_continuar=quis, observacao=observacao)

    @property
    def estado(self) -> str:
        if not self.atendeu:
            return RESULTADO_NAO_ATENDEU
        return (
            RESULTADO_QUER_CONTINUAR
            if self.quis_continuar
            else RESULTADO_SEM_CONTINUAR
        )

    def normalizado(self) -> ResultadoLigacao:
        obs = (self.observacao or "").strip()[:OBSERVACAO_MAX] or None
        quis = self.quis_continuar if self.atendeu else None
        return ResultadoLigacao(atendeu=self.atendeu, quis_continuar=quis, observacao=obs)


def texto_da_nota(res: ResultadoLigacao, *, tentativa: int | None = None) -> str:
    """Nota curta e legível pra thread do lead.

    TODA tentativa vira nota — inclusive "não atendeu" (regra Dona). Não atender
    também é informação: é o que permite responder depois "quantas vezes já
    tentamos falar com essa pessoa?". Por isso o número da tentativa entra no
    texto quando é da segunda em diante — dá pra contar batendo o olho, sem
    query.
    """
    if not res.atendeu:
        base = "📞 Tentativa de ligação — não atendeu"
    elif res.quis_continuar is True:
        base = "📞 Ligação — atendeu e quer continuar"
    elif res.quis_continuar is False:
        base = "📞 Ligação — atendeu, não quis seguir agora"
    else:
        base = "📞 Ligação — atendeu"
    if tentativa and tentativa > 1:
        base = f"{base} ({tentativa}ª tentativa)"
    return f"{base} · {res.observacao}" if res.observacao else base


def tentativas_do_lead(
    session: Session,
    *,
    lead_id: str | None = None,
    lead_espinha_id: int | None = None,
    tenant_id: int = DEFAULT_TENANT_ID,
) -> int:
    """Quantas vezes já ligaram pra este lead (todas as origens).

    DERIVADO dos registros que já gravamos — sem coluna nova em `leads`. Aceita
    as duas chaves porque elas convivem: `lead_id` é o external_ref (o que a
    thread e as tarefas usam) e `lead_espinha_id` é `leads.id` (o que a janela
    de remarketing usa, e alguns leads da base antiga nem têm external_ref).
    """
    if lead_id is None and lead_espinha_id is None:
        return 0
    return int(
        session.execute(
            sql_text(
                # CAST explícito: sem ele o Postgres não infere o tipo do
                # parâmetro dentro de `IS NOT NULL` (AmbiguousParameter).
                "SELECT COUNT(*) FROM ligacao_resultado "
                "WHERE tenant_id = :t AND ("
                "   (CAST(:lid AS TEXT) IS NOT NULL AND lead_id = CAST(:lid AS TEXT)) "
                "OR (CAST(:eid AS BIGINT) IS NOT NULL AND origem = :orig "
                "    AND origem_id = CAST(:eid AS BIGINT)))"
            ),
            {
                "t": tenant_id,
                "lid": str(lead_id) if lead_id else None,
                "eid": int(lead_espinha_id) if lead_espinha_id else None,
                "orig": ORIGEM_REMARKETING,
            },
        ).scalar()
        or 0
    )


def _proximo_toque(
    session: Session,
    *,
    origem: str,
    origem_id: int,
    tenant_id: int = DEFAULT_TENANT_ID,
) -> int:
    """Número da próxima tentativa desta fila (1 na primeira ligação).

    LOCK ANTES DE CONTAR. `MAX(toque)+1` sem lock é corrida: duas ligações
    simultâneas pro mesmo lead tiravam o MESMO número — uma inseria e a outra
    caía no `ON CONFLICT DO NOTHING` e voltava como `ja_registrado=True`. A
    segunda ligação REAL perdia resultado, observação e nota, confundida com
    repetição idempotente.

    Advisory lock por (tenant, origem, origem_id): serializa só as ligações do
    MESMO lead, e cai sozinho no fim da transação — não há nada pra destravar
    na mão se o processo morrer no meio.
    """
    session.execute(
        sql_text("SELECT pg_advisory_xact_lock(hashtext(:chave))"),
        {"chave": f"ligacao_resultado:{tenant_id}:{origem}:{int(origem_id)}"},
    )
    return int(
        session.execute(
            sql_text(
                "SELECT COALESCE(MAX(toque), 0) + 1 FROM ligacao_resultado "
                "WHERE tenant_id = :t AND origem = :o AND origem_id = :oid"
            ),
            {"t": tenant_id, "o": origem, "oid": int(origem_id)},
        ).scalar()
        or 1
    )


def registrar_resultado(
    session: Session,
    *,
    origem: str,
    origem_id: int,
    resultado: ResultadoLigacao,
    lead_id: str | None,
    corretora_id: int | None,
    autor: str | None,
    toque: int | None = None,
    tenant_id: int = DEFAULT_TENANT_ID,
) -> dict[str, Any]:
    """Grava o dado estruturado + a nota na conversa do lead.

    TODA tentativa vira nota — inclusive "não atendeu" (regra Dona): é o que
    permite contar quantas vezes já tentaram falar com a pessoa.

    `lead_id` é a chave EXTERNA do lead (external_ref do Kommo) — a mesma que
    `observacao_corretora`, `tarefas_corretora` e `/painel/lead/{id}/notas`
    usam. Sem ela grava só o estruturado (não há thread onde anotar).

    Idempotente por (tenant, origem, origem_id, toque): registrar 2x a MESMA
    ligação não duplica linha nem nota. Na origem `remarketing`, `toque` é o
    número da TENTATIVA e é calculado sozinho quando não vem — cada ligação
    nova pro mesmo lead é um registro novo, não um conflito. O cálculo do
    número roda sob advisory lock: sem ele, duas ligações simultâneas pro mesmo
    lead tiravam o mesmo número e uma delas era descartada como "já registrado".
    NÃO faz commit — quem chama fecha a transação.
    """
    if origem not in ORIGENS:
        raise ValueError(f"origem_invalida: {origem}")

    res = resultado.normalizado()
    if toque is not None:
        toque_efetivo = int(toque)
    elif origem == ORIGEM_REMARKETING:
        toque_efetivo = _proximo_toque(
            session, origem=origem, origem_id=int(origem_id), tenant_id=tenant_id
        )
    else:
        toque_efetivo = TOQUE_MANUAL

    # Nº da tentativa DESTE lead (todas as origens) — vai no texto da nota.
    tentativa = tentativas_do_lead(
        session,
        lead_id=lead_id,
        lead_espinha_id=int(origem_id) if origem == ORIGEM_REMARKETING else None,
        tenant_id=tenant_id,
    ) + 1

    row = session.execute(
        sql_text(
            "INSERT INTO ligacao_resultado "
            "(tenant_id, origem, origem_id, toque, lead_id, corretora_id, "
            " atendeu, quis_continuar, observacao, autor) "
            "VALUES (:t, :orig, :oid, :toque, :lid, :cid, :at, :qc, :obs, :autor) "
            "ON CONFLICT (tenant_id, origem, origem_id, toque) DO NOTHING "
            "RETURNING id"
        ),
        {
            "t": tenant_id, "orig": origem, "oid": int(origem_id),
            "toque": toque_efetivo, "lid": str(lead_id) if lead_id else None,
            "cid": corretora_id, "at": res.atendeu, "qc": res.quis_continuar,
            "obs": res.observacao, "autor": autor,
        },
    ).mappings().first()

    if row is None:  # já registrado antes — não duplica a nota
        log.info(
            "ligacao_resultado_ja_registrado",
            origem=origem, origem_id=int(origem_id), toque=toque_efetivo,
        )
        return {"ok": True, "ja_registrado": True, "resultado_id": None, "nota_id": None}

    nota_id: int | None = None
    if lead_id:
        nota = session.execute(
            sql_text(
                "INSERT INTO observacao_corretora "
                "(tenant_id, lead_id, corretora_id, autor, texto) "
                "VALUES (:t, :lid, :cid, :autor, :texto) "
                "RETURNING id"
            ),
            {
                "t": tenant_id, "lid": str(lead_id), "cid": corretora_id,
                "autor": autor,
                "texto": texto_da_nota(res, tentativa=tentativa),
            },
        ).mappings().first()
        nota_id = int(nota["id"]) if nota else None

    log.info(
        "ligacao_resultado_registrado",
        origem=origem, origem_id=int(origem_id), toque=toque_efetivo,
        lead_id=lead_id, corretora_id=corretora_id, estado=res.estado,
        tem_observacao=bool(res.observacao), nota_id=nota_id,
    )
    return {
        "ok": True,
        "ja_registrado": False,
        "resultado_id": int(row["id"]),
        "nota_id": nota_id,
    }


def ultima_ligacao_do_lead(
    session: Session, *, lead_id: str, tenant_id: int = DEFAULT_TENANT_ID
) -> dict[str, Any] | None:
    """Última ligação REGISTRADA do lead — o que faz o raio-X do Chat (e quem
    lê ele) saber que já ligaram hoje em vez de sugerir "tenta entrar em
    contato". `lead_id` é o external_ref."""
    r = session.execute(
        sql_text(
            "SELECT atendeu, quis_continuar, observacao, autor, criado_em "
            "FROM ligacao_resultado "
            "WHERE tenant_id = :t AND lead_id = :lid "
            "ORDER BY criado_em DESC, id DESC LIMIT 1"
        ),
        {"t": tenant_id, "lid": str(lead_id)},
    ).mappings().first()
    if not r:
        return None
    res = ResultadoLigacao(
        atendeu=bool(r["atendeu"]),
        quis_continuar=r["quis_continuar"],
        observacao=r["observacao"],
    )
    return {
        "atendeu": bool(r["atendeu"]),
        "quis_continuar": r["quis_continuar"],
        "estado": res.estado,
        "observacao": r["observacao"],
        "autor": r["autor"],
        "at": r["criado_em"].isoformat() if r["criado_em"] else None,
        "resumo": texto_da_nota(res),
    }


# ── Adoção do lead ("quer continuar") ────────────────────────────────────────
#
# Regra de negócio (Dona): os leads que a corretora liga são os do REMARKETING,
# que não têm dono. A ligação é o momento em que ela adota o lead — a janela
# deixa de ser lista de tarefa e vira o mecanismo de distribuição da base
# parada. Ao marcar "quer continuar", o lead vai pra "Novo" (a entrada do funil
# principal — "Entrou" é alias dela em lead_spine._STAGE_ALIASES) e ganha
# aquela corretora como responsável.
#
# DUAS PROTEÇÕES, e são as que custam caro se faltarem:
#   1. NÃO REBAIXA ETAPA. Só move quem está sem etapa, no funil de remarketing,
#      ou já em "Novo". Lead em Visita marcada/Proposta/Venda ganha fica onde
#      está — mesma ideia do `so_de_stages` do move de resposta da cadência.
#   2. NÃO TIRA O DONO DE QUEM JÁ TEM. `owner_id` preenchido por outra corretora
#      é mantido e devolvido em `ja_tem_dono` pra a tela avisar.
#
# Efeitos colaterais de propósito (espelham o move manual do kanban V2):
#   - `owner_editado_manual = True` → o sync do Kommo não desfaz a adoção
#     (lead_spine, migration 0061);
#   - evento `stage_manual_v2` → janela de 72h em que o espelho do Kommo não
#     sobrescreve o stage (lead_spine._manual_v2_recente);
#   - evento `adocao_ligacao` → auditoria de quem adotou o quê e quando.

STAGE_ENTRADA = "Novo"
EVENTO_ADOCAO = "adocao_ligacao"


def adotar_lead(
    session: Session,
    *,
    lead_id: int,
    corretora_id: int,
    autor: str | None,
    tenant_id: int = DEFAULT_TENANT_ID,
) -> dict[str, Any]:
    """Move o lead pra entrada do funil e dá dono. `lead_id` é `leads.id`
    (espinha), não o external_ref. NÃO faz commit."""
    from app.db.models import Lead, PipelineStage
    from app.services.lead_spine import FUNIL_REMARKETING, _registrar_evento
    from app.services.lead_spine import _resolve_stage_id

    lead = (
        session.query(Lead)
        .filter(Lead.tenant_id == tenant_id, Lead.id == int(lead_id))
        .first()
    )
    if lead is None:
        return {"ok": False, "motivo": "lead_nao_encontrado"}

    agora = datetime.now(timezone.utc)
    stage_destino = _resolve_stage_id(session, tenant_id, None, STAGE_ENTRADA)
    stage_de = lead.stage_id

    # 1) etapa — whitelist, nunca rebaixa
    mover = stage_destino is not None and stage_de != stage_destino
    motivo_etapa: str | None = None
    if mover and stage_de is not None:
        atual = session.get(PipelineStage, stage_de)
        funil_atual = ((atual.funil if atual else None) or "").strip()
        if funil_atual != FUNIL_REMARKETING:
            mover = False
            motivo_etapa = "etapa_mais_avancada"
    if mover:
        lead.stage_id = stage_destino

    # 2) dono — nunca sobrescreve
    owner_de = lead.owner_id
    ja_tem_dono = (owner_de is not None and not _eh_da_corretor(owner_de)
                   and int(owner_de) != int(corretora_id))
    definiu_dono = False
    # da Corretor passa pra corretora: é o que tira o lead do carrossel
    if owner_de is None or _eh_da_corretor(owner_de):
        lead.owner_id = int(corretora_id)
        lead.owner_editado_manual = True
        definiu_dono = True

    lead.updated_at = agora
    # 3) TAG "veio do remarketing por ligação" — o que faz a boas-vindas mandar
    # a copy de CONTINUIDADE em vez de "você acabou de se cadastrar, certo?"
    # pra quem acabou de falar por telefone. Ver
    # `app/services/boas_vindas_continuidade.py`.
    lead.adotado_ligacao_em = agora

    if mover:
        # mesma marca do arrasto manual no kanban: segura o espelho do Kommo 72h
        _registrar_evento(
            session, tenant_id, lead.id, "stage_manual_v2",
            {
                "origem": stage_de, "destino": stage_destino,
                "stage_destino": STAGE_ENTRADA, "funil": "principal",
                "quem": autor, "via": EVENTO_ADOCAO,
            },
        )
    _registrar_evento(
        session, tenant_id, lead.id, EVENTO_ADOCAO,
        {
            "corretora_id": int(corretora_id), "quem": autor,
            "stage_de": stage_de, "stage_para": lead.stage_id,
            "moveu_etapa": mover, "definiu_dono": definiu_dono,
            "ja_tem_dono": ja_tem_dono, "owner_de": owner_de,
            "motivo_etapa": motivo_etapa,
        },
    )
    log.info(
        "ligacao_adocao",
        lead_id=lead.id, corretora_id=int(corretora_id), quem=autor,
        moveu_etapa=mover, definiu_dono=definiu_dono, ja_tem_dono=ja_tem_dono,
        stage_de=stage_de, stage_para=lead.stage_id, motivo_etapa=motivo_etapa,
        adotado_ligacao_em=agora.isoformat(),
    )
    return {
        "ok": True,
        "lead_id": lead.id,
        "adotado_ligacao_em": agora.isoformat(),
        "moveu_etapa": mover,
        "stage_de": stage_de,
        "stage_para": lead.stage_id,
        "motivo_etapa": motivo_etapa,
        "definiu_dono": definiu_dono,
        "ja_tem_dono": ja_tem_dono,
        "owner_de": owner_de,
        "owner_atual": lead.owner_id,
    }


def veio_de_ligacao(session: Session, lead_id: int) -> bool:
    """True se o lead carrega a TAG de adoção por ligação.

    É o que a boas-vindas consulta pra NÃO mandar "você acabou de se cadastrar,
    certo?" pra quem acabou de falar por telefone com a corretora — nesse caso
    sai a copy de `boas_vindas_continuidade`, FORA do teste A/B.

    Best-effort: banco antigo sem a coluna (ou qualquer falha de leitura) devolve
    False e a boas-vindas segue como antes — a automação da casa não cai porque
    uma coluna nova não existe ainda.
    """
    from app.db.models import Lead

    try:
        return (
            session.query(Lead.adotado_ligacao_em)
            .filter(Lead.id == int(lead_id))
            .scalar()
        ) is not None
    except Exception as e:  # noqa: BLE001
        log.warning("veio_de_ligacao_indisponivel", lead_id=lead_id, erro=str(e)[:140])
        session.rollback()
        return False


# ── Fila da janela de ligação ────────────────────────────────────────────────
#
# O QUE ELA CONTÉM DE VERDADE (medido em 05/08, e o comentário antigo dizia
# menos do que devia): o funil `remarketing` guarda DOIS mundos —
#
#   1. a RÉGUA DE NUTRIÇÃO — etapas Base/30/60/90/120/150 dias, leads SEM dono
#      que esgotaram a cadência e recebem 1 mensagem a cada 30 dias pela linha
#      da Pessoa12 (`app/services/nutricao.py`);
#   2. a RODA DO CARROSSEL da Corretor — etapas Fila/Disparado/Respondeu, 4.952 leads
#      que têm a Corretor (corretora_id 4) como dona.
#
# Como o filtro é `owner_id IS NULL`, a fila é hoje **100% mundo 1**: 545 leads
# de nutrição, zero do carrossel. Isso é INTENCIONAL e fica escrito aqui porque
# o comentário genérico ("remarketing sem dono") escondia o fato de que a fila
# come da régua automática — foi assim que a colisão passou.
#
# O carrossel fica FORA de propósito: aqueles leads estão numa roda que dispara
# TODO DIA (colisão diária, não eventual) e dentro de um teste A/B de copy vivo,
# que ligação por telefone contaminaria. Se um dia a fila precisar deles, a
# trava de silêncio abaixo já cobre — o filtro a mexer é o `owner_id IS NULL`.
#
# TRAVA LIGAÇÃO × AUTOMAÇÃO (05/08, achado da Dona): a mesma pessoa recebia
# ligação da corretora e mensagem automática da régua no mesmo dia, de duas
# fontes que não se falam. A trava é SIMÉTRICA e vive em
# `reengajamento_guard` (o módulo que já fazia isso entre Corretor e cadência):
#   - aqui: quem recebeu mensagem automática nos últimos
#     `JANELA_SILENCIO_DIAS` não é oferecido pra ligar;
#   - lá (`cadencia.tick` / `nutricao.tick`): quem levou ligação nos últimos
#     `JANELA_SILENCIO_DIAS` não recebe mensagem — o toque ADIA, não se perde.
# Não filtramos por "tem toque agendado no futuro": 536 dos 545 leads da fila
# têm (o descongelamento de 05/08 agendou até setembro), e isso esvaziaria a
# aba no dia em que ela entra em uso. O que mata o atropelo é a janela de
# silêncio dos dois lados, não o agendamento.
#
# QUEM SAI DA FILA:
#   - quem ATENDEU alguma vez. Ou quis continuar (aí foi adotado e ganhou dono,
#     some sozinho) ou não quis seguir agora — insistir em quem já disse não é
#     o mesmo atropelo;
#   - quem levou uma tentativa nas últimas `DESCANSO_HORAS`. Não atender não
#     tira ninguém da fila pra sempre: a pessoa volta, com o contador de
#     tentativas na frente da corretora, que decide se ainda vale;
#   - quem pediu pra parar (opt-out por sufixo-8, mesma fonte que o carrossel e
#     o disparo usam). A adoção dispara boas-vindas no WhatsApp — oferecer quem
#     mandou "pare" é caminho direto pra furar o opt-out;
#   - quem recebeu MENSAGEM AUTOMÁTICA (régua de nutrição, cadência ou
#     carrossel) nos últimos `JANELA_SILENCIO_DIAS` — a trava acima;
#   - quem OUTRA corretora já pegou (reserva, abaixo).
#
# Ordem: menos tentativas primeiro, e dentro disso o mais parado — quem nunca
# foi tentado vem antes de quem já levou quatro ligações.
#
# RESERVA (por que existe): a fila era GLOBAL. Pessoa10 e Pessoa11 abrindo a aba na
# mesma manhã viam o MESMO lead no topo e ligavam pra mesma pessoa. Quem pede a
# fila agora PEGA os leads que levou, por `RESERVA_MINUTOS` — a próxima
# corretora recebe os seguintes. A reserva EXPIRA sozinha (fechar a aba não
# tranca lead pra sempre) e é renovada a cada recarga de quem já é dono dela.
# Não é lock de banco: `FOR UPDATE SKIP LOCKED` só dura a transação, e a
# transação acaba quando a resposta HTTP sai — a corretora fica com a lista na
# tela por minutos, que é justamente o intervalo que precisa ser protegido.

FILA_LIMITE_MAX = 200
DESCANSO_HORAS = 24
RESERVA_MINUTOS = 30

# Leads da Corretor que entram na fila. A Corretor é a linha do carrossel, não uma
# corretora: lead dela é lead SEM DONA HUMANA. Até 24/09/2026 a fila só via
# `owner_id IS NULL` (428 de 5.941 no remarketing), esgotou em 21/09 e passou a
# girar os mesmos ~140 que não atendem. Pedido da Dona (24/09): ligar primeiro
# pra quem entrou este ano. Só a etapa "Fila" (quem o carrossel ainda não
# tocou) — "Disparado" e "Respondeu" seguem com o carrossel.
# A data é a de ENTRADA do cliente (`criado_em_origem`/Kommo), a mesma que o
# cartão mostra como "entrou em".
#
# 01/10/2026 — o corte de ano virou ORDENAÇÃO, não exclusão (pedido da Dona). Em
# setembro a Pessoa7 fez 1.102 ligações em 582 pessoas distintas: a base elegível
# era 417 e ela liga ~130/dia, então o estoque esgotava em 3 dias e os mesmos
# leads voltavam. Os 1.609 da etapa "Fila" que entraram em 2025 estavam
# trancados pelo corte; agora entram, e `CORRETOR_NA_FILA_PRIORIDADE` é o que garante
# que quem entrou este ano continua saindo PRIMEIRO (ver o ORDER BY da fila).
# "Disparado" e "Respondeu" seguem FORA — esses o carrossel já tocou, e ligar em
# cima de quem acabou de receber mensagem automática é o atropelo que a janela de
# silêncio existe pra evitar.
CORRETOR_NA_FILA_DESDE = "2025-01-01"
CORRETOR_NA_FILA_PRIORIDADE = "2026-01-01"
CORRETOR_NA_FILA_ETAPA = "Fila"

# Quem a fila de ligação trata como "sem dona". `{entrou}` é a expressão da
# data de entrada no contexto onde o trecho for interpolado.
_SQL_SEM_DONA = r"""
               AND (l.owner_id IS NULL
                    OR (l.owner_id = :corretor
                        AND ps.nome = :corretor_etapa
                        AND {entrou} >= CAST(:corretor_desde AS DATE)))
"""


def _eh_da_corretor(owner_id: Any) -> bool:
    return owner_id is not None and int(owner_id) == CORRETORA_CORRETOR

# Opt-out por sufixo-8 — as duas fontes da casa (disparo de reengajamento e
# cadência), iguais às de `carrossel_corretor._SQL_FILA`. `{tel}` é a expressão do
# telefone do lead no contexto onde o trecho for interpolado.
_SQL_SEM_OPT_OUT = r"""
               AND NOT EXISTS (
                     SELECT 1 FROM disparos_reengajamento d
                      WHERE d.opt_out IS TRUE
                        AND right(regexp_replace(d.telefone, '\D', '', 'g'), 8)
                          = right(regexp_replace({tel}, '\D', '', 'g'), 8)
               )
               AND NOT EXISTS (
                     SELECT 1 FROM cadencia_estado ce
                      WHERE ce.status = 'opt_out'
                        AND right(regexp_replace(ce.telefone, '\D', '', 'g'), 8)
                          = right(regexp_replace({tel}, '\D', '', 'g'), 8)
               )
"""

# Janela de silêncio: quem levou mensagem AUTOMÁTICA há pouco não é oferecido
# pra ligar. As duas fontes são os dois motores que escrevem pro lead frio —
# `cadencia_eventos(tipo='mensagem')` (cadência E nutrição, mesmo log) e
# `lead_events(tipo='carrossel_disparo')` (carrossel da Corretor). O outro lado da
# trava está em `reengajamento_guard.ligacao_recente`, aplicado nos dois ticks.
_SQL_SEM_AUTOMACAO_RECENTE = r"""
               AND NOT EXISTS (
                     SELECT 1 FROM cadencia_eventos ev
                       JOIN cadencia_estado ce ON ce.id = ev.cadencia_id
                      WHERE ev.tenant_id = l.tenant_id
                        AND ev.tipo = 'mensagem'
                        AND ev.at >= NOW() - (:silencio || ' days')::interval
                        AND right(regexp_replace(
                              COALESCE(ce.telefone, ''), '\D', '', 'g'), 8)
                          = right(regexp_replace({tel}, '\D', '', 'g'), 8)
               )
               AND NOT EXISTS (
                     SELECT 1 FROM lead_events le
                      WHERE le.tenant_id = l.tenant_id
                        AND le.tipo = 'carrossel_disparo'
                        AND le.at >= NOW() - (:silencio || ' days')::interval
                        AND le.lead_id = l.id
               )
"""


def _sufixo8(telefone: str | None) -> str:
    """Os 8 últimos dígitos — a chave de telefone que o resto da casa usa."""
    return re.sub(r"\D", "", telefone or "")[-8:]


def _ultimas_mensagens(
    session: Session,
    telefones: list[str | None],
    *,
    tenant_id: int = DEFAULT_TENANT_ID,
) -> dict[str, dict[str, Any]]:
    """Última mensagem de cada telefone da PÁGINA, numa query só.

    Era um LATERAL dentro da query da fila e custava **77 s** contra a base
    real: `mensagens_whatsapp` tem dezenas de milhares de linhas, o casamento é
    por `regexp_replace` (não usa índice) e o lateral rodava uma vez POR LEAD
    candidato. Aqui roda uma varredura só, já com os ≤50 sufixos da página.

    DUAS TRAVAS que o lateral antigo não tinha:
      - filtra `tenant_id`: sem isso a conversa de OUTRO cliente entrava como
        contexto do lead;
      - sufixo-8 que casa com MAIS DE UM número é ambíguo (outro DDD, outra
        pessoa): devolve vazio em vez de mostrar conversa de terceiro.
    """
    sufixos = sorted({s for s in (_sufixo8(t) for t in telefones) if s})
    if not sufixos:
        return {}
    # DESEMPATE DO SUFIXO AMBÍGUO (20/09).
    #
    # 87 leads do remarketing caíam em "dois números com o mesmo final-8" e
    # perdiam o contexto da conversa. Olhando de perto (caso Reinaldo), o
    # segundo número não era outra pessoa: era o MESMO envio gravado com o
    # telefone normalizado torto na época da Evolution — 326 mensagens em 53
    # números no formato `5555` + 8 dígitos, todas saídas nossas.
    #
    # Em vez de apagar histórico, o desempate é por PLAUSIBILIDADE: celular BR
    # em E.164 tem 13 dígitos (55 + DDD + 9XXXXXXXX). Quando só um dos
    # candidatos tem essa cara, ele ganha. Quando dois têm — aí são mesmo duas
    # pessoas e continuamos sem mostrar nada.
    rows = session.execute(
        sql_text(
            r"""
            SELECT right(regexp_replace(contato_telefone, '\D', '', 'g'), 8) AS suf,
                   (array_agg(conteudo_texto
                              ORDER BY timestamp_evolution DESC))[1] AS texto,
                   -- o número EXATO com que o WhatsApp conhece essa pessoa.
                   -- No Brasil o nono dígito é armadilha: conta antiga fica
                   -- registrada SEM ele, e o wa.me montado do cadastro abre
                   -- "número inválido". Quando já conversamos, o jid é a
                   -- verdade — melhor que qualquer palpite de formatação.
                   (array_agg(split_part(remote_jid, '@', 1)
                              ORDER BY timestamp_evolution DESC))[1] AS jid,
                   MAX(timestamp_evolution) AS at,
                   COUNT(DISTINCT regexp_replace(
                         contato_telefone, '\D', '', 'g')) AS numeros,
                   -- quantos candidatos têm cara de celular BR (55+DDD+9dddddddd)
                   COUNT(DISTINCT regexp_replace(contato_telefone, '\D', '', 'g'))
                     FILTER (WHERE regexp_replace(contato_telefone, '\D', '', 'g')
                             ~ '^55[1-9][1-9]9[0-9]{8}$') AS numeros_celular,
                   (array_agg(split_part(remote_jid, '@', 1)
                              ORDER BY timestamp_evolution DESC)
                    FILTER (WHERE regexp_replace(contato_telefone, '\D', '', 'g')
                            ~ '^55[1-9][1-9]9[0-9]{8}$'))[1] AS jid_celular
              FROM mensagens_whatsapp
             WHERE tenant_id = :t
               AND contato_telefone IS NOT NULL
               AND right(regexp_replace(contato_telefone, '\D', '', 'g'), 8)
                   = ANY(:sufs)
             GROUP BY 1
            """
        ),
        {"t": tenant_id, "sufs": sufixos},
    ).mappings().all()
    saida: dict[str, dict[str, Any]] = {}
    for r in rows:
        if int(r["numeros"] or 0) == 1:
            saida[r["suf"]] = {"texto": r["texto"], "at": r["at"], "jid": r["jid"]}
        elif int(r["numeros_celular"] or 0) == 1:
            # Ambíguo só na aparência: um candidato é celular, os outros são
            # lixo de normalização. O texto/data continuam sendo os do conjunto
            # (mesma pessoa), mas o jid sai do candidato plausível.
            saida[r["suf"]] = {"texto": r["texto"], "at": r["at"],
                               "jid": r["jid_celular"]}
    return saida


# DDDs que EXISTEM no Brasil. Números com DDD fora desta lista não completam
# chamada nem existem no WhatsApp — em 20/09 a Corretor encontrou o "Jorge"
# (+55 **56** 95229-3109) na fila dela e o telefone respondeu "não é válido".
# Eram 6 na base, todos vindos da importação do Kommo (`nutricao_kommo`),
# nenhum com uma única mensagem recebida e com os envios voltando `failed`.
#
# Ficam FORA da fila de ligação: fila é tempo de corretora, e número que não
# disca gasta esse tempo duas vezes — na tentativa e na dúvida ("será que é o
# sistema?").
DDDS_BR = (
    "11", "12", "13", "14", "15", "16", "17", "18", "19",
    "21", "22", "24", "27", "28",
    "31", "32", "33", "34", "35", "37", "38",
    "41", "42", "43", "44", "45", "46", "47", "48", "49",
    "51", "53", "54", "55",
    "61", "62", "63", "64", "65", "66", "67", "68", "69",
    "71", "73", "74", "75", "77", "79",
    "81", "82", "83", "84", "85", "86", "87", "88", "89",
    "91", "92", "93", "94", "95", "96", "97", "98", "99",
)


def ddd_existe(telefone: str | None) -> bool:
    """True quando o DDD do número é um DDD brasileiro de verdade."""
    d = "".join(ch for ch in (telefone or "") if ch.isdigit())
    if d.startswith("55"):
        d = d[2:]
    return len(d) >= 10 and d[:2] in DDDS_BR


def telefone_discagem(telefone: str | None) -> str | None:
    """O número no formato que o telefone da corretora aceita DISCAR: 0 + DDD + número.

    O banco guarda E.164 (+5500000000000) porque é o que a Cloud API exige. Mas
    a Pessoa7 liga de um chip virtual (Salvy), e lá o "+55" não completa a chamada
    — a operadora devolve "o número foi discado incorretamente". O que funciona é
    `0` + DDD + número.

    Ela descobriu isso na mão, depois de bater com a cara na porta em vários
    leads. Traduzir 514 números na cabeça, um a um, não é trabalho de gente.

    Devolve None quando o número não tem a cara de celular brasileiro — a tela
    mostra o E.164 nesse caso, em vez de inventar um formato que não disca.
    """
    if not telefone:
        return None
    d = re.sub(r"\D", "", telefone)
    if d.startswith("55"):
        d = d[2:]
    # DDD (2) + 9 dígitos. Fora disso não arriscamos um palpite.
    if len(d) != 11 or not d[2] == "9":
        return None
    return "0" + d


def fila_ligacao(
    session: Session,
    *,
    limite: int = 50,
    dono: str | None = None,
    corretora_id: int | None = None,
    ver_reservados: bool = False,
    tenant_id: int = DEFAULT_TENANT_ID,
) -> list[dict[str, Any]]:
    """Fila de quem ligar: nome, telefone e o contexto que existir.

    Ordem: o mais parado primeiro (updated_at mais antigo) — é a base que
    esfriou, e o mais antigo é o que menos custa perder se a ligação der ruim.

    `dono` = usuário do painel que está pedindo a fila. Com ele, os leads
    devolvidos ficam RESERVADOS pra essa pessoa por `RESERVA_MINUTOS` e somem
    da fila de quem mais pedir nesse intervalo — é o que impede duas corretoras
    ligarem pra mesma pessoa. Sem ele (leitura administrativa), nada é
    reservado e o que está reservado por outro NÃO aparece.
    NÃO faz commit — quem chama fecha a transação (a reserva só vale depois do
    commit).
    """
    limite = max(1, min(int(limite), FILA_LIMITE_MAX))
    rows = session.execute(
        sql_text(
            """
            WITH cand AS (
            SELECT l.id            AS lead_id,
                   l.external_ref,
                   l.nome,
                   l.telefone,
                   l.origem,
                   l.updated_at,
                   l.created_at,
                   -- quando o cliente entrou DE VERDADE: a data do Kommo
                   -- (`criado_em_origem`), não a da importação em lote pra cá
                   -- (`created_at`, igual pra 555 leads). NULL = não sabemos.
                   COALESCE(l.criado_em_origem, ls.criado_em_kommo) AS entrou_em,
                   -- quem entrou ESTE ANO sai primeiro (pedido da Dona, 24/09).
                   -- Era corte de elegibilidade e virou ordenação em 01/10, pra
                   -- a base de 2025 entrar sem passar na frente da de 2026.
                   -- COALESCE porque entrada desconhecida é NULL, e NULL no
                   -- DESC subiria pro topo sem ninguém ter pedido.
                   COALESCE(
                     COALESCE(l.criado_em_origem, ls.criado_em_kommo)
                       >= CAST(:corretor_prio AS DATE), FALSE
                   ) AS entrou_este_ano,
                   ps.nome         AS etapa,
                   la.raw_analysis->>'conversa_resumo' AS resumo,
                   tent.n            AS tentativas,
                   tent.ultima       AS ultima_tentativa_em,
                   rvx.dono          AS reservado_por,
                   ag.quando         AS agendado_para
              FROM leads l
              JOIN pipeline_stages ps ON ps.id = l.stage_id
              LEFT JOIN leads_snapshot ls
                     ON ls.tenant_id = l.tenant_id
                    AND ls.lead_id = l.external_ref
              LEFT JOIN LATERAL (
                    SELECT raw_analysis
                      FROM lead_analyses a
                     WHERE a.lead_id = l.external_ref
                     ORDER BY a.analyzed_at DESC
                     LIMIT 1
              ) la ON TRUE
              LEFT JOIN ligacao_reserva rvx
                     ON rvx.tenant_id = l.tenant_id AND rvx.lead_id = l.id
                    AND rvx.expira_em > NOW()
              LEFT JOIN LATERAL (
                    -- "ligar mais tarde": retorno combinado com a pessoa na
                    -- própria ligação. Data + hora viram um instante no fuso da
                    -- operação; sem hora, vale o começo do expediente daquele
                    -- dia (senão meia-noite jogaria a tarefa pro topo como
                    -- atrasada logo cedo).
                    SELECT ((t.data_tarefa
                             + COALESCE(t.hora_tarefa, TIME '09:00'))
                            AT TIME ZONE :fuso) AS quando
                      FROM tarefas_corretora t
                     WHERE t.tenant_id = l.tenant_id
                       AND t.lead_id = l.external_ref
                       AND t.texto = :texto_ligar
                       AND t.status = 'aberta'
                     ORDER BY t.data_tarefa ASC, t.hora_tarefa ASC NULLS FIRST
                     LIMIT 1
              ) ag ON TRUE
              LEFT JOIN LATERAL (
                    -- contador de tentativas DERIVADO dos registros (sem coluna
                    -- nova): quantas vezes já ligamos e quando foi a última
                    SELECT COUNT(*) AS n, MAX(lr.criado_em) AS ultima
                      FROM ligacao_resultado lr
                     WHERE lr.tenant_id = l.tenant_id
                       AND ((lr.origem = :origem AND lr.origem_id = l.id)
                            OR (l.external_ref IS NOT NULL
                                AND lr.lead_id = l.external_ref))
              ) tent ON TRUE
             WHERE l.tenant_id = :t
               AND ps.funil = :funil
""" + _SQL_SEM_DONA.format(
                entrou="COALESCE(l.criado_em_origem, ls.criado_em_kommo)") + r"""
               AND COALESCE(l.telefone, '') <> ''
               -- IS NOT TRUE (não `NOT x`): em SQL `NOT NULL` é NULL e o lead
               -- sumiria da fila em silêncio. Mesmo padrão do resto do projeto.
               AND l.telefone_invalido IS NOT TRUE
               -- DDD que não existe não disca (ver DDDS_BR)
               AND substring(regexp_replace(l.telefone, '\D', '', 'g')
                             FROM 3 FOR 2) = ANY(:ddds)
               -- quem já atendeu sai da fila (adotado ou disse que não quer)
               AND NOT EXISTS (
                     SELECT 1 FROM ligacao_resultado lr
                      WHERE lr.tenant_id = l.tenant_id
                        AND lr.atendeu
                        AND ((lr.origem = :origem AND lr.origem_id = l.id)
                             OR (l.external_ref IS NOT NULL
                                 AND lr.lead_id = l.external_ref))
               )
               -- descanso: não reoferecer quem levou tentativa agora há pouco
               AND (tent.ultima IS NULL
                    OR tent.ultima < NOW() - (:descanso || ' hours')::interval)
               -- lead que OUTRA pessoa pegou e ainda não expirou. É a trava de
               -- PRIVACIDADE da aba: corretora só vê o que é dela. `:ver_tudo`
               -- é a visão admin (supervisão), que não reserva nada.
               AND (CAST(:ver_tudo AS BOOLEAN) OR NOT EXISTS (
                     SELECT 1 FROM ligacao_reserva rv
                      WHERE rv.tenant_id = l.tenant_id
                        AND rv.lead_id = l.id
                        AND rv.expira_em > NOW()
                        AND rv.dono IS DISTINCT FROM CAST(:dono AS TEXT)
               ))
"""
            + _SQL_SEM_OPT_OUT.format(tel="COALESCE(l.telefone, '')")
            + _SQL_SEM_AUTOMACAO_RECENTE.format(tel="COALESCE(l.telefone, '')")
            + """
             -- Quem tem retorno combinado vem PRIMEIRO, na ordem do relógio: é
             -- hora marcada com a pessoa, tem precedência sobre "o mais parado
             -- primeiro". O resto segue a ordem de sempre.
             ORDER BY (ag.quando IS NULL) ASC,
                      ag.quando ASC,
                      COALESCE(tent.n, 0) ASC,
                      COALESCE(
                        COALESCE(l.criado_em_origem, ls.criado_em_kommo)
                          >= CAST(:corretor_prio AS DATE), FALSE
                      ) DESC,
                      COALESCE(l.updated_at, l.created_at) ASC, l.id ASC
             LIMIT :lim
            ),
            reserva AS (
                -- PEGA os leads desta página. ON CONFLICT com WHERE = claim
                -- atômico: se duas corretoras pedirem a fila no mesmo instante,
                -- só uma leva cada lead (a outra não vê a linha voltar e o lead
                -- não entra na resposta dela).
                INSERT INTO ligacao_reserva
                    (tenant_id, lead_id, dono, corretora_id, reservado_em, expira_em)
                SELECT :t, cand.lead_id, CAST(:dono AS TEXT), :cid, NOW(),
                       NOW() + (:reserva || ' minutes')::interval
                  FROM cand
                 WHERE CAST(:dono AS TEXT) IS NOT NULL
                ON CONFLICT (tenant_id, lead_id) DO UPDATE
                   SET dono = EXCLUDED.dono,
                       corretora_id = EXCLUDED.corretora_id,
                       reservado_em = EXCLUDED.reservado_em,
                       expira_em = EXCLUDED.expira_em
                 WHERE ligacao_reserva.expira_em <= NOW()
                    OR ligacao_reserva.dono = EXCLUDED.dono
                RETURNING lead_id
            )
            SELECT cand.* FROM cand
             WHERE CAST(:dono AS TEXT) IS NULL
                OR EXISTS (SELECT 1 FROM reserva r WHERE r.lead_id = cand.lead_id)
             ORDER BY (cand.agendado_para IS NULL) ASC,
                      cand.agendado_para ASC,
                      COALESCE(cand.tentativas, 0) ASC,
                      cand.entrou_este_ano DESC,
                      COALESCE(cand.updated_at, cand.created_at) ASC,
                      cand.lead_id ASC
            """
        ),
        {
            "t": tenant_id,
            "funil": "remarketing",
            "ddds": list(DDDS_BR),
            "origem": ORIGEM_REMARKETING,
            "descanso": str(DESCANSO_HORAS),
            "lim": limite,
            "dono": dono,
            "cid": corretora_id,
            "reserva": str(RESERVA_MINUTOS),
            "silencio": str(reengajamento_guard.JANELA_SILENCIO_DIAS),
            "ver_tudo": bool(ver_reservados),
            "fuso": FUSO_OPERACAO,
            "texto_ligar": TEXTO_LIGAR_NOVAMENTE,
            "corretor": CORRETORA_CORRETOR,
            "corretor_etapa": CORRETOR_NA_FILA_ETAPA,
            "corretor_desde": CORRETOR_NA_FILA_DESDE,
            "corretor_prio": CORRETOR_NA_FILA_PRIORIDADE,
        },
    ).mappings().all()

    agora = datetime.now(timezone.utc)
    ultimas = _ultimas_mensagens(
        session, [r["telefone"] for r in rows], tenant_id=tenant_id
    )

    def _dias(dt: datetime | None) -> int | None:
        if dt is None:
            return None
        dt = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        return max((agora - dt).days, 0)

    def _msg(telefone: str | None) -> dict[str, Any]:
        return ultimas.get(_sufixo8(telefone), {})

    return [
        {
            "lead_id": int(r["lead_id"]),
            "external_ref": r["external_ref"],
            "nome": (r["nome"] or "").strip() or "Sem nome",
            "telefone": r["telefone"],
            # o mesmo número pronto pra discar (0+DDD+n). None = formato fora do
            # padrão; a tela cai no E.164 em vez de mostrar palpite.
            "telefone_discar": telefone_discagem(r["telefone"]),
            # Número pro link do WhatsApp: o jid da conversa quando existe,
            # senão None e a tela monta do cadastro.
            "telefone_whatsapp": (_msg(r["telefone"]).get("jid") or None),
            "origem": r["origem"],
            "etapa": r["etapa"],
            "dias_parado": _dias(r["updated_at"] or r["created_at"]),
            "resumo": r["resumo"],
            "ultima_msg": (_msg(r["telefone"]).get("texto") or None),
            "ultima_msg_at": (
                _msg(r["telefone"])["at"].isoformat()
                if _msg(r["telefone"]).get("at") else None
            ),
            # quantas vezes já ligamos + qual é ESTA ligação (a Nª tentativa)
            "tentativas": int(r["tentativas"] or 0),
            "tentativa_atual": int(r["tentativas"] or 0) + 1,
            # só a visão admin recebe isso preenchido de outra pessoa
            "reservado_por": r["reservado_por"],
            "ultima_tentativa_em": (
                r["ultima_tentativa_em"].isoformat()
                if r["ultima_tentativa_em"] else None
            ),
            # data de entrada do cliente (Kommo). None = desconhecida — a tela
            # mostra "—", nunca a data da importação.
            "entrou_em": (
                r["entrou_em"].isoformat() if r["entrou_em"] else None
            ),
            # retorno combinado na ligação ("me liga mais tarde") — o cartão
            # sobe pro topo e mostra a hora; passou da hora, ela fica vermelha.
            "agendado_para": (
                r["agendado_para"].isoformat() if r["agendado_para"] else None
            ),
        }
        for r in rows
    ]


def total_fila_ligacao(session: Session, *, tenant_id: int = DEFAULT_TENANT_ID) -> int:
    """Estoque de quem ainda espera ligação.

    Mesmo filtro da fila MENOS o descanso de `DESCANSO_HORAS` e MENOS a reserva
    — este número é o tamanho da base a percorrer, não o da tela do momento
    (senão ele encolheria e voltaria a crescer sozinho no dia seguinte, e
    ninguém confia em número que sobe sem nada acontecer).
    """
    return int(
        session.execute(
            sql_text(
                "SELECT COUNT(*) FROM leads l "
                "JOIN pipeline_stages ps ON ps.id = l.stage_id "
                "LEFT JOIN leads_snapshot ls "
                "       ON ls.tenant_id = l.tenant_id "
                "      AND ls.lead_id = l.external_ref "
                "WHERE l.tenant_id = :t AND ps.funil = :funil "
                + _SQL_SEM_DONA.format(
                    entrou="COALESCE(l.criado_em_origem, ls.criado_em_kommo)")
                + "  AND COALESCE(l.telefone, '') <> '' "
                "  AND COALESCE(l.telefone, '') <> '' "
                "  AND l.telefone_invalido IS NOT TRUE "
                "  AND NOT EXISTS (SELECT 1 FROM ligacao_resultado lr "
                "                   WHERE lr.tenant_id = l.tenant_id AND lr.atendeu "
                "                     AND ((lr.origem = :origem AND lr.origem_id = l.id) "
                "                          OR (l.external_ref IS NOT NULL "
                "                              AND lr.lead_id = l.external_ref)))"
                + _SQL_SEM_OPT_OUT.format(tel="COALESCE(l.telefone, '')")
                + _SQL_SEM_AUTOMACAO_RECENTE.format(tel="COALESCE(l.telefone, '')")
            ),
            {
                "t": tenant_id, "funil": "remarketing",
                "origem": ORIGEM_REMARKETING,
                "silencio": str(reengajamento_guard.JANELA_SILENCIO_DIAS),
                "corretor": CORRETORA_CORRETOR,
                "corretor_etapa": CORRETOR_NA_FILA_ETAPA,
                "corretor_desde": CORRETOR_NA_FILA_DESDE,
            },
        ).scalar()
        or 0
    )


# ── Validação de backend do registro ─────────────────────────────────────────
#
# A tela só oferece lead que passou pelos filtros da fila. O ENDPOINT aceitava
# qualquer `lead_id`: uma aba velha, um retry ou um curl furavam a regra toda
# (lead de outra corretora, fora do remarketing, com opt-out, telefone morto).
# Validação que só existe no front é validação que um curl fura — então as
# MESMAS regras da fila valem aqui, no banco.

MOTIVO_SEM_TELEFONE = "lead_sem_telefone"
MOTIVO_TELEFONE_INVALIDO = "lead_telefone_invalido"
MOTIVO_OPT_OUT = "lead_opt_out"
MOTIVO_OUTRA_CORRETORA = "lead_de_outra_corretora"
MOTIVO_FORA_REMARKETING = "lead_fora_do_remarketing"

MOTIVO_EXPLICACAO: dict[str, str] = {
    MOTIVO_SEM_TELEFONE: "esse lead não tem telefone cadastrado",
    MOTIVO_TELEFONE_INVALIDO: "o telefone desse lead está marcado como inválido",
    MOTIVO_OPT_OUT: "essa pessoa pediu pra não receber mais contato",
    MOTIVO_OUTRA_CORRETORA: "esse lead já tem outra dona — fala com a gestão",
    MOTIVO_FORA_REMARKETING: "esse lead não está na fila de remarketing",
}


def validar_lead_da_janela(
    session: Session,
    *,
    lead_id: int,
    corretora_id: int | None,
    tenant_id: int = DEFAULT_TENANT_ID,
    permitir_outra_corretora: bool = False,
) -> str | None:
    """Motivo pelo qual este lead NÃO pode receber registro de ligação da janela
    de remarketing — ou None quando pode.

    `corretora_id` é quem está registrando (None = admin sem corretora alvo).
    Lead que JÁ é dela passa mesmo fora do remarketing: é o caso normal de
    registrar a segunda ligação depois da adoção (a adoção move o lead pro
    funil principal).

    `permitir_outra_corretora` solta SÓ a trava de dono — hoje só a "venda
    perdida" usa (pedido da Dona, 10/08/2026): se o lead apareceu na fila e
    alguém falou com a pessoa, o lead está perdido independente de quem é a
    dona, e segurar o botão deixa lead morto circulando na fila. Perder não
    rouba o lead: `marcar_venda_perdida` só define dono quando não há nenhum.
    `/registrar` NÃO usa isso — ele adota, e adotar lead alheio é outra coisa.
    """
    r = session.execute(
        sql_text(
            """
            SELECT l.owner_id,
                   COALESCE(l.telefone, '') AS telefone,
                   l.telefone_invalido,
                   ps.funil,
                   EXISTS (
                     SELECT 1 FROM disparos_reengajamento d
                      WHERE d.opt_out IS TRUE
                        AND right(regexp_replace(d.telefone, '\\D', '', 'g'), 8)
                          = right(regexp_replace(COALESCE(l.telefone, ''), '\\D', '', 'g'), 8)
                   ) OR EXISTS (
                     SELECT 1 FROM cadencia_estado ce
                      WHERE ce.status = 'opt_out'
                        AND right(regexp_replace(ce.telefone, '\\D', '', 'g'), 8)
                          = right(regexp_replace(COALESCE(l.telefone, ''), '\\D', '', 'g'), 8)
                   ) AS opt_out
              FROM leads l
              LEFT JOIN pipeline_stages ps ON ps.id = l.stage_id
             WHERE l.tenant_id = :t AND l.id = :lid
            """
        ),
        {"t": tenant_id, "lid": int(lead_id)},
    ).mappings().first()
    if r is None:
        return "lead_nao_encontrado"

    owner = r["owner_id"]
    # lead da Corretor no remarketing é lead sem dona humana (ver CORRETOR_NA_FILA_DESDE)
    if _eh_da_corretor(owner) and (r["funil"] or "") == "remarketing":
        owner = None
    dela = owner is not None and corretora_id is not None and int(owner) == int(corretora_id)
    if owner is not None and not dela and not permitir_outra_corretora:
        return MOTIVO_OUTRA_CORRETORA
    if not (r["telefone"] or "").strip():
        return MOTIVO_SEM_TELEFONE
    if r["telefone_invalido"] is True:
        return MOTIVO_TELEFONE_INVALIDO
    if r["opt_out"]:
        return MOTIVO_OPT_OUT
    if not dela and (r["funil"] or "") != "remarketing":
        return MOTIVO_FORA_REMARKETING
    return None


# ── Meta do dia e da semana ──────────────────────────────────────────────────
#
# Pedido da Dona (05/08): 100 ligações por semana por corretora, entregues em
# blocos de 25 por dia (20/dia fecharia exato em 5 dias; 25 dá folga e deixa a
# semana fechar antes da sexta). Os dois números são config
# (`config_operacional.ligacao_meta_diaria` / `_semanal`) — mudar meta não pode
# exigir deploy.
#
# POR QUE A FILA TEM TAMANHO: hoje a Pessoa10 tem 114 tarefas em aberto e a Pessoa11 e a
# Pessoa12 26 atrasadas há semanas. Lista infinita não vira rotina — vira dívida. O
# bloco do dia é o contrato: acabou, acabou.
#
# CONTAGEM: derivada de `ligacao_resultado` (corretora + `criado_em`), nunca de
# contador próprio. Contador dessincroniza; registro é a verdade.
#
# ⚠️ FUSO: dia e semana em America/Sao_Paulo. Ligação às 21h de SP é 00h UTC do
# dia seguinte — contada crua, ela cairia no dia (e na semana) errados. Semana
# de segunda a domingo.
#
# ADIANTAR: "quer adiantar a de amanhã?" libera MAIS UM bloco no dia de HOJE
# (`blocos_extras`). Escolha de contagem: por DIA CIVIL, não por saldo puxado do
# futuro. Quem adiantou hoje NÃO abre amanhã com 50 (dobrar a meta pune quem se
# esforçou) nem com 0 (zerar o dia tira a rotina) — abre com os mesmos 25. O
# ganho aparece onde a Dona pediu que aparecesse: no progresso da SEMANA, que
# pode fechar na quinta.


def _hoje_sp(agora: datetime | None = None) -> date:
    from zoneinfo import ZoneInfo

    agora = agora or datetime.now(timezone.utc)
    if agora.tzinfo is None:
        agora = agora.replace(tzinfo=timezone.utc)
    return agora.astimezone(ZoneInfo(FUSO_OPERACAO)).date()


def _semana_sp(agora: datetime | None = None) -> tuple[date, date]:
    """Segunda e domingo da semana corrente, no fuso da operação."""
    hoje = _hoje_sp(agora)
    segunda = hoje - timedelta(days=hoje.weekday())
    return segunda, segunda + timedelta(days=6)


def _linha_meta(
    session: Session, *, corretora_id: int, dia: date, tenant_id: int
) -> Any:
    return session.execute(
        sql_text(
            "SELECT blocos_extras, confete_em FROM ligacao_meta_dia "
            "WHERE tenant_id = :t AND corretora_id = :c AND dia = :d"
        ),
        {"t": tenant_id, "c": int(corretora_id), "d": dia},
    ).mappings().first()


def progresso_ligacao(
    session: Session,
    *,
    corretora_id: int | None,
    tenant_id: int = DEFAULT_TENANT_ID,
    agora: datetime | None = None,
) -> dict[str, Any] | None:
    """Quanto falta pra fechar o dia e a semana desta corretora.

    None quando não há corretora alvo (admin sem `corretora_id`): meta é da
    pessoa que liga, e inventar uma meta pro admin poluiria o número dela.
    """
    if corretora_id is None:
        return None
    from app.services import config_operacional

    dia = _hoje_sp(agora)
    seg, dom = _semana_sp(agora)
    meta_dia = config_operacional.ligacao_meta_diaria(corretora_id)
    meta_semana = config_operacional.ligacao_meta_semanal()

    contagem = session.execute(
        sql_text(
            """
            SELECT
              COUNT(*) FILTER (
                WHERE (criado_em AT TIME ZONE :fuso)::date = :dia
              ) AS hoje,
              COUNT(*) FILTER (
                WHERE (criado_em AT TIME ZONE :fuso)::date BETWEEN :seg AND :dom
              ) AS semana
              FROM ligacao_resultado
             WHERE tenant_id = :t AND corretora_id = :c
            """
        ),
        {"t": tenant_id, "c": int(corretora_id), "fuso": FUSO_OPERACAO,
         "dia": dia, "seg": seg, "dom": dom},
    ).mappings().first()
    hoje = int((contagem or {}).get("hoje") or 0)
    semana = int((contagem or {}).get("semana") or 0)

    linha = _linha_meta(session, corretora_id=corretora_id, dia=dia, tenant_id=tenant_id)
    extras = int((linha or {}).get("blocos_extras") or 0)
    alvo_hoje = meta_dia * (1 + extras)
    restante = max(alvo_hoje - hoje, 0)

    return {
        "corretora_id": int(corretora_id),
        "dia": dia.isoformat(),
        "semana_inicio": seg.isoformat(),
        "semana_fim": dom.isoformat(),
        "fuso": FUSO_OPERACAO,
        "meta_diaria": meta_dia,
        "meta_semanal": meta_semana,
        "blocos_extras": extras,
        "alvo_hoje": alvo_hoje,
        "feitas_hoje": hoje,
        "restante_hoje": restante,
        "concluiu_dia": restante == 0,
        "feitas_semana": semana,
        "faltam_semana": max(meta_semana - semana, 0),
        "concluiu_semana": semana >= meta_semana,
        # o confete é da CONCLUSÃO, e cai uma vez só por dia
        "confete_em": (
            linha["confete_em"].isoformat()
            if linha and linha["confete_em"] else None
        ),
    }


def marcar_confete(
    session: Session,
    *,
    corretora_id: int,
    tenant_id: int = DEFAULT_TENANT_ID,
    agora: datetime | None = None,
) -> bool:
    """Carimba a comemoração do dia. True = é a PRIMEIRA vez hoje (pode soltar
    o confete); False = já caiu, a tela não repete a cada F5."""
    dia = _hoje_sp(agora)
    row = session.execute(
        sql_text(
            "INSERT INTO ligacao_meta_dia (tenant_id, corretora_id, dia, confete_em) "
            "VALUES (:t, :c, :d, NOW()) "
            "ON CONFLICT (tenant_id, corretora_id, dia) DO UPDATE "
            "   SET confete_em = NOW() "
            " WHERE ligacao_meta_dia.confete_em IS NULL "
            "RETURNING id"
        ),
        {"t": tenant_id, "c": int(corretora_id), "d": dia},
    ).mappings().first()
    return row is not None


def liberar_bloco_extra(
    session: Session,
    *,
    corretora_id: int,
    tenant_id: int = DEFAULT_TENANT_ID,
    agora: datetime | None = None,
) -> dict[str, Any]:
    """"Quer adiantar a tarefa de amanhã?" — libera MAIS um bloco pra HOJE.

    Só depois de fechar o bloco corrente: liberar antes viraria fila infinita de
    novo, que é justamente o que a meta resolve.
    """
    prog = progresso_ligacao(
        session, corretora_id=corretora_id, tenant_id=tenant_id, agora=agora
    )
    if prog is None:
        return {"ok": False, "motivo": "sem_corretora"}
    if not prog["concluiu_dia"]:
        return {"ok": False, "motivo": "bloco_atual_em_aberto", "progresso": prog}

    dia = _hoje_sp(agora)
    session.execute(
        sql_text(
            "INSERT INTO ligacao_meta_dia (tenant_id, corretora_id, dia, blocos_extras) "
            "VALUES (:t, :c, :d, 1) "
            "ON CONFLICT (tenant_id, corretora_id, dia) DO UPDATE "
            "   SET blocos_extras = ligacao_meta_dia.blocos_extras + 1"
        ),
        {"t": tenant_id, "c": int(corretora_id), "d": dia},
    )
    log.info(
        "ligacao_bloco_extra",
        corretora_id=int(corretora_id), dia=dia.isoformat(),
        blocos_antes=prog["blocos_extras"],
    )
    return {
        "ok": True,
        "progresso": progresso_ligacao(
            session, corretora_id=corretora_id, tenant_id=tenant_id, agora=agora
        ),
    }


STAGE_PERDIDA = "Venda perdida"
EVENTO_PERDIDA = "ligacao_venda_perdida"


def marcar_venda_perdida(
    session: Session,
    *,
    lead_id: int,
    corretora_id: int | None,
    autor: str | None,
    motivo: str | None = None,
    tenant_id: int = DEFAULT_TENANT_ID,
) -> dict[str, Any]:
    """Manda o lead da fila de ligação direto pra "Venda perdida".

    DÁ DONO junto, de propósito: o board do CRM só mostra lead de corretora
    ativa (`Lead.owner_id.in_(...)`), então mover um lead SEM DONA pra "Venda
    perdida" o tiraria da fila e ele não apareceria em lugar nenhum — sumiria.
    Quem marcou é quem falou com a pessoa; o lead fica com ela, perdido.

    Sai da fila de ligação automaticamente: a fila só olha o funil
    `remarketing`, e "Venda perdida" é etapa do principal.

    NÃO faz commit — quem chama fecha a transação.
    """
    from app.db.models import Lead
    from app.services.lead_spine import _registrar_evento, _resolve_stage_id

    lead = (
        session.query(Lead)
        .filter(Lead.tenant_id == tenant_id, Lead.id == int(lead_id))
        .first()
    )
    if lead is None:
        return {"ok": False, "motivo": "lead_nao_encontrado"}

    agora = datetime.now(timezone.utc)
    stage_destino = _resolve_stage_id(session, tenant_id, None, STAGE_PERDIDA)
    if stage_destino is None:
        return {"ok": False, "motivo": "stage_nao_resolvido"}

    stage_de = lead.stage_id
    owner_de = lead.owner_id
    definiu_dono = False
    if (owner_de is None or _eh_da_corretor(owner_de)) and corretora_id is not None:
        lead.owner_id = int(corretora_id)
        lead.owner_editado_manual = True
        definiu_dono = True

    lead.stage_id = stage_destino
    lead.updated_at = agora

    # mesma marca do arrasto manual no kanban: segura o espelho do Kommo por 72h
    # (senão a sombra devolve o lead pra etapa antiga e ele volta pra fila).
    _registrar_evento(
        session, tenant_id, lead.id, "stage_manual_v2",
        {
            "origem": stage_de, "destino": stage_destino,
            "stage_destino": STAGE_PERDIDA, "funil": "principal",
            "quem": autor, "via": EVENTO_PERDIDA,
        },
    )
    _registrar_evento(
        session, tenant_id, lead.id, EVENTO_PERDIDA,
        {
            "corretora_id": corretora_id, "quem": autor, "motivo": motivo,
            "stage_de": stage_de, "stage_para": stage_destino,
            "definiu_dono": definiu_dono, "owner_de": owner_de,
        },
    )
    log.info(
        "ligacao_venda_perdida",
        lead_id=lead.id, corretora_id=corretora_id, quem=autor,
        stage_de=stage_de, stage_para=stage_destino, definiu_dono=definiu_dono,
    )
    return {
        "ok": True,
        "lead_id": lead.id,
        "stage_de": stage_de,
        "stage_para": stage_destino,
        "definiu_dono": definiu_dono,
        "owner_atual": lead.owner_id,
    }


EVENTO_TELEFONE_ERRADO = "ligacao_telefone_errado"


def marcar_telefone_errado(
    session: Session,
    *,
    lead_id: int,
    corretora_id: int | None,
    autor: str | None,
    observacao: str | None = None,
    tenant_id: int = DEFAULT_TENANT_ID,
) -> dict[str, Any]:
    """"Número errado" — tira o lead da fila PRA SEMPRE, sem virar "não atendeu".

    POR QUE EXISTE (medido em 01/10/2026): a fila só solta quem ATENDEU, e os
    três estados não tinham onde encaixar número que nem completa a chamada. A
    corretora marcava "não atendeu" — o único botão possível — e o lead voltava
    24h depois, pra sempre. A Pessoa7 fez 1.102 ligações em 582 pessoas: 384
    leads levaram 2 ou 3 chamadas, muitas pro mesmo número quebrado.

    DELIBERADAMENTE NÃO grava `ligacao_resultado` (mesma escolha do
    `agendar_retorno`): número errado não é tentativa de contato, é cadastro
    furado. Contar como "não atendeu" afundaria a taxa de atendimento dela com
    um problema que não é de ligação. O rastro fica na nota da conversa e no
    evento do lead.

    Marca `telefone_invalido` em TODOS os leads com o telefone idêntico (mesmos
    dígitos), não por sufixo-8 como o `telefone_invalido.marcar_se_morto`: aqui
    quem decide é uma pessoa olhando UM cadastro, e sufixo-8 casa com número de
    outro DDD (87 casos conhecidos na base). Dígito igual é o que ela viu.

    Reversível por `telefone_invalido.desmarcar(telefone)`.

    NÃO faz commit — quem chama fecha a transação.
    """
    from app.db.models import Lead
    from app.services.lead_spine import _registrar_evento

    lead = (
        session.query(Lead)
        .filter(Lead.tenant_id == tenant_id, Lead.id == int(lead_id))
        .first()
    )
    if lead is None:
        return {"ok": False, "motivo": "lead_nao_encontrado"}

    digitos = re.sub(r"\D", "", lead.telefone or "")
    if len(digitos) < 10:
        # sem número não há o que marcar como errado — e marcar o cadastro todo
        # por causa de um campo vazio esconderia o lead sem motivo
        return {"ok": False, "motivo": MOTIVO_SEM_TELEFONE}

    if lead.telefone_invalido is True:
        return {"ok": True, "ja_marcado": True, "lead_id": lead.id, "leads_marcados": 0}

    marcados = session.execute(
        sql_text(
            r"""UPDATE leads
                   SET telefone_invalido = TRUE, updated_at = NOW()
                 WHERE tenant_id = :t
                   AND telefone_invalido IS NOT TRUE
                   AND regexp_replace(COALESCE(telefone, ''), '\D', '', 'g') = :dig
            """
        ),
        {"t": tenant_id, "dig": digitos},
    ).rowcount or 0

    nota_id: int | None = None
    if lead.external_ref:
        texto = "📵 Número errado — tirei da fila de ligação"
        extra = (observacao or "").strip()[:OBSERVACAO_MAX]
        if extra:
            texto = f"{texto}. {extra}"
        nota = session.execute(
            sql_text(
                "INSERT INTO observacao_corretora "
                "(tenant_id, lead_id, corretora_id, autor, texto) "
                "VALUES (:t, :lid, :cid, :autor, :texto) "
                "RETURNING id"
            ),
            {
                "t": tenant_id, "lid": str(lead.external_ref),
                "cid": corretora_id, "autor": autor, "texto": texto,
            },
        ).mappings().first()
        nota_id = int(nota["id"]) if nota else None

    _registrar_evento(
        session, tenant_id, lead.id, EVENTO_TELEFONE_ERRADO,
        {
            "corretora_id": corretora_id, "quem": autor,
            "observacao": (observacao or None), "leads_marcados": marcados,
        },
    )
    log.info(
        "ligacao_telefone_errado",
        lead_id=lead.id, corretora_id=corretora_id, quem=autor,
        leads_marcados=marcados, nota_id=nota_id,
    )
    return {
        "ok": True,
        "ja_marcado": False,
        "lead_id": lead.id,
        "leads_marcados": marcados,
        "nota_id": nota_id,
    }


# Texto FIXO da tarefa criada pelo "ligar mais tarde". É fixo de propósito: a
# corretora está no meio de uma ligação e só tem que dizer a hora — digitar o
# assunto de novo, dezenas de vezes por dia, é o que mata o registro. É também
# o que a fila procura pra saber que o lead está agendado.
TEXTO_LIGAR_NOVAMENTE = "Ligar novamente"


def agendar_retorno(
    session: Session,
    *,
    lead_id: int,
    quando: datetime,
    corretora_id: int | None,
    autor: str,
    external_ref: str | None,
    lead_nome: str | None = None,
    observacao: str | None = None,
    tenant_id: int = DEFAULT_TENANT_ID,
) -> dict[str, Any]:
    """"Me liga mais tarde" — agenda o retorno SEM tirar o lead da fila.

    Deliberadamente NÃO grava `ligacao_resultado`: a pessoa atendeu, mas o
    assunto não terminou. Registrar "atendeu" eliminaria o lead da fila (a
    query tem `NOT EXISTS ... lr.atendeu`), que é o oposto do que a corretora
    quis dizer. Quem atendeu e resolveu já tem os três botões de sempre.

    A tarefa é a MESMA de `tarefas_corretora` que o chat cria — assim o retorno
    aparece no Hoje e na aba de tarefas, e não vira um combinado que só existe
    dentro da janela de ligação.

    A reserva é esticada até depois do horário marcado: sem isso ela expira em
    `RESERVA_MINUTOS` e o lead que ELA agendou reaparece pra outra corretora
    antes da hora combinada.
    """
    if quando.tzinfo is None:
        quando = quando.replace(tzinfo=timezone.utc)

    # Uma tarefa de retorno em aberto por lead: remarcar substitui a anterior,
    # senão a fila acumula dois horários e mostra o errado no topo.
    session.execute(
        sql_text(
            """
            UPDATE tarefas_corretora
               SET status = 'concluida', concluido_em = NOW(), concluido_por = :autor
             WHERE tenant_id = :t AND lead_id = :ref
               AND texto = :texto AND status = 'aberta'
            """
        ),
        {"t": tenant_id, "ref": external_ref, "texto": TEXTO_LIGAR_NOVAMENTE,
         "autor": autor},
    )

    # A tarefa guarda data/hora LOCAIS (é o que a corretora vê no Hoje), então
    # o instante volta pro fuso da operação antes de virar colunas.
    from zoneinfo import ZoneInfo

    local = quando.astimezone(ZoneInfo(FUSO_OPERACAO))
    tarefa_id = session.execute(
        sql_text(
            """
            INSERT INTO tarefas_corretora
                (tenant_id, lead_id, lead_nome, corretora_id, data_tarefa,
                 hora_tarefa, texto, status, autor, anotacoes)
            VALUES (:t, :ref, :nome, :cid, :data, :hora, :texto, 'aberta',
                    :autor, :anot)
            RETURNING id
            """
        ),
        {
            "t": tenant_id, "ref": external_ref, "nome": lead_nome,
            "cid": corretora_id, "data": local.date(), "hora": local.time(),
            "texto": TEXTO_LIGAR_NOVAMENTE, "autor": autor,
            "anot": (observacao or "").strip() or None,
        },
    ).scalar()

    # Segura o lead com ela até o horário marcado (+ folga pra ligação em si).
    expira = quando + timedelta(minutes=RESERVA_MINUTOS)
    session.execute(
        sql_text(
            """
            INSERT INTO ligacao_reserva
                (tenant_id, lead_id, dono, corretora_id, reservado_em, expira_em)
            VALUES (:t, :lid, :dono, :cid, NOW(), :expira)
            ON CONFLICT (tenant_id, lead_id) DO UPDATE
               SET dono = EXCLUDED.dono,
                   corretora_id = EXCLUDED.corretora_id,
                   expira_em = GREATEST(ligacao_reserva.expira_em,
                                        EXCLUDED.expira_em)
            """
        ),
        {"t": tenant_id, "lid": int(lead_id), "dono": autor,
         "cid": corretora_id, "expira": expira},
    )

    log.info(
        "ligacao_retorno_agendado",
        lead_id=int(lead_id), corretora_id=corretora_id, quando=quando.isoformat(),
    )
    return {
        "ok": True,
        "tarefa_id": int(tarefa_id) if tarefa_id else None,
        "agendado_para": quando.isoformat(),
    }


def liberar_reserva(
    session: Session, *, lead_id: int, tenant_id: int = DEFAULT_TENANT_ID
) -> None:
    """Devolve o lead pra fila (ou tira ele de circulação, quando foi adotado).

    Chamado depois de registrar: segurar reserva de lead já trabalhado só
    atrasaria a volta dele pro rodízio depois do descanso."""
    session.execute(
        sql_text(
            "DELETE FROM ligacao_reserva WHERE tenant_id = :t AND lead_id = :lid"
        ),
        {"t": tenant_id, "lid": int(lead_id)},
    )


def renovar_reserva(
    session: Session,
    *,
    dono: str,
    lead_ids: list[int],
    tenant_id: int = DEFAULT_TENANT_ID,
) -> list[int]:
    """Renova a reserva dos leads que ainda estão com `dono` e devolve quais são.

    POR QUE EXISTE (22/09, caso Corretor): a reserva vence em `RESERVA_MINUTOS`, mas
    a tela carrega a fila UMA vez e nunca mais olha. A Corretor abriu a lista às
    17:59 de 21/09, trabalhou nela até 20:26 e voltou no dia seguinte pra dar
    baixa — nesse meio-tempo a fila da Pessoa7 (09:13 de 22/09) pegou os mesmos
    leads, que eram os próximos da ordem, e ligou pra eles. Quando a Corretor
    recarregou, a lista dela era outra: "os leads sumiram". Pior que o sumiço:
    ela ainda ligou pra quatro que a Pessoa7 já tinha ligado (um atendeu a
    Pessoa7 de manhã e levou outra ligação à noite).

    Renova só linha cujo dono AINDA é quem pede: reserva vencida que ninguém
    pegou continua dela (ninguém quis o lead, não há atropelo); reserva que
    outra corretora sobrescreveu, ou que sumiu porque alguém registrou a
    ligação, NÃO volta — o lead não é mais dela e a tela precisa dizer isso.

    Não faz commit — quem chama fecha a transação.
    """
    ids = sorted({int(i) for i in lead_ids})[:FILA_LIMITE_MAX]
    if not ids or not dono:
        return []
    rows = session.execute(
        sql_text(
            """
            UPDATE ligacao_reserva
               SET expira_em = GREATEST(
                       expira_em, NOW() + (:reserva || ' minutes')::interval)
             WHERE tenant_id = :t
               AND dono = :dono
               AND lead_id = ANY(:ids)
            RETURNING lead_id
            """
        ),
        {"t": tenant_id, "dono": dono, "ids": ids,
         "reserva": str(RESERVA_MINUTOS)},
    ).scalars().all()
    return sorted(int(r) for r in rows)


# ── Métrica ──────────────────────────────────────────────────────────────────
#
# UNIVERSO (o que conta como "ligação que era pra ter sido feita"):
#   - cadência: `cadencia_eventos(tipo='tarefa_ligacao')` na janela — a fila da
#     régua (D1/D5/D9), de onde vem a maioria;
#   - manual: `tarefas_corretora` concluída na janela cujo TEXTO é de ligação;
#   - janela de ligação: os próprios registros de origem 'remarketing' (não
#     existe "tarefa" prévia ali — a ligação nasce do registro).
#
# Desse universo, `ligacoes` = as que têm resultado registrado. O resto é
# `sem_registro` e fica FORA das taxas — não é "não atendeu".

TIPO_EVENTO_LIGACAO = "tarefa_ligacao"


def _taxa(numerador: int, denominador: int) -> float | None:
    """Percentual com 1 casa. None (não 0.0) quando não há amostra."""
    if denominador <= 0:
        return None
    return round(numerador / denominador * 100, 1)


def _linha_zerada(corretora_id: int | None, label: str | None) -> dict[str, Any]:
    return {
        "corretora_id": corretora_id,
        "corretora": label,
        "total_na_fila": 0,
        "ligacoes": 0,
        "sem_registro": 0,
        "atenderam": 0,
        "nao_atenderam": 0,
        "quiseram_continuar": 0,
        "nao_quiseram_continuar": 0,
    }


def _fechar_linha(linha: dict[str, Any], amostra_minima: int) -> dict[str, Any]:
    ligacoes = int(linha["ligacoes"])
    atenderam = int(linha["atenderam"])
    return {
        **linha,
        "taxa_atendimento": _taxa(atenderam, ligacoes),
        "taxa_continuidade": _taxa(int(linha["quiseram_continuar"]), atenderam),
        # "sem dado" é sobre a AMOSTRA (o que tem resultado), não sobre a fila.
        "sem_dado": ligacoes == 0,
        "amostra_pequena": 0 < ligacoes < amostra_minima,
        "amostra_pequena_continuidade": 0 < atenderam < amostra_minima,
    }


# ── Quando as pessoas atendem ────────────────────────────────────────────────
#
# Pergunta da Dona (05/08): "em que horário a ligação mais é atendida?", pra o
# time concentrar esforço. Sai dos registros que já existem — `criado_em` é a
# hora da ligação; nenhuma coluna nova.
#
# ⚠️ FUSO: agrupado em America/Sao_Paulo, NUNCA UTC. Ligação às 21h de SP é 00h
# UTC do dia seguinte — agrupada crua, ela viraria "madrugada" e a conclusão
# sairia invertida (e o dia da semana também andaria um). Mesma classe de bug
# que hoje mesmo apareceu no `disparos_hoje` do painel.
#
# ⚠️ AMOSTRA: 20 ligações espalhadas em 13 faixas dá 1 ou 2 por faixa — e aí
# "as 14h converte 100%" quer dizer "uma pessoa atendeu uma vez". Por isso o
# bloco só se declara legível (`confiavel`) a partir de AMOSTRA_MINIMA_HORARIO
# ligações no período, e `melhor_faixa` só é preenchida quando ALÉM disso a
# faixa vencedora tem AMOSTRA_MINIMA_FAIXA. Abaixo disso a tela diz "ainda
# juntando dado" — é a diferença entre informar e induzir.
FUSO_OPERACAO = "America/Sao_Paulo"
HORA_INICIO = 8   # antes disso ninguém liga (e quem ligar aparece em `fora_da_janela`)
HORA_FIM = 21     # última faixa é 20h–21h
AMOSTRA_MINIMA_HORARIO = 100
AMOSTRA_MINIMA_FAIXA = 10

DIAS_SEMANA = ("segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo")


def _em_sao_paulo(dt: datetime | None) -> datetime | None:
    """`criado_em` no fuso da operação. Naive (SQLite de teste) é lido como UTC —
    é como o Postgres devolveria a mesma linha."""
    if dt is None:
        return None
    from zoneinfo import ZoneInfo

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ZoneInfo(FUSO_OPERACAO))


def _linha_horario(rotulo: str, chave: Any) -> dict[str, Any]:
    return {"chave": chave, "rotulo": rotulo, "ligacoes": 0, "atenderam": 0}


def _fechar_horario(linha: dict[str, Any]) -> dict[str, Any]:
    ligacoes = int(linha["ligacoes"])
    return {
        **linha,
        "taxa_atendimento": _taxa(int(linha["atenderam"]), ligacoes),
        # faixa sem ligação nenhuma é VAZIA, não "0% de atendimento"
        "sem_dado": ligacoes == 0,
        "amostra_pequena": 0 < ligacoes < AMOSTRA_MINIMA_FAIXA,
    }


def _quando_atendem(registros: Any) -> dict[str, Any]:
    """Taxa de atendimento por faixa de hora e por dia da semana (fuso SP)."""
    faixas: dict[int, dict[str, Any]] = {
        h: _linha_horario(f"{h:02d}h–{h + 1:02d}h", h)
        for h in range(HORA_INICIO, HORA_FIM)
    }
    fora = _linha_horario("fora do horário comercial", None)
    dias: dict[int, dict[str, Any]] = {
        i: _linha_horario(nome, i) for i, nome in enumerate(DIAS_SEMANA)
    }

    total = 0
    for r in registros:
        quando = _em_sao_paulo(r["criado_em"])
        if quando is None:
            continue
        total += 1
        atendeu = bool(r["atendeu"])
        alvo = faixas.get(quando.hour, fora)
        alvo["ligacoes"] += 1
        dia = dias[quando.weekday()]
        dia["ligacoes"] += 1
        if atendeu:
            alvo["atenderam"] += 1
            dia["atenderam"] += 1

    por_faixa = [_fechar_horario(f) for _, f in sorted(faixas.items())]
    confiavel = total >= AMOSTRA_MINIMA_HORARIO

    melhor = None
    if confiavel:
        candidatas = [
            f for f in por_faixa
            if f["taxa_atendimento"] is not None
            and int(f["ligacoes"]) >= AMOSTRA_MINIMA_FAIXA
        ]
        if candidatas:
            melhor = max(candidatas, key=lambda f: (f["taxa_atendimento"], f["ligacoes"]))

    return {
        "fuso": FUSO_OPERACAO,
        "amostra": total,
        "amostra_minima": AMOSTRA_MINIMA_HORARIO,
        "amostra_minima_faixa": AMOSTRA_MINIMA_FAIXA,
        # False = ainda não dá pra tirar conclusão; a tela mostra "juntando dado"
        "confiavel": confiavel,
        "por_faixa": por_faixa,
        "fora_da_janela": _fechar_horario(fora),
        "por_dia_semana": [_fechar_horario(d) for _, d in sorted(dias.items())],
        "melhor_faixa": (
            {"rotulo": melhor["rotulo"], "taxa": melhor["taxa_atendimento"],
             "ligacoes": melhor["ligacoes"]}
            if melhor else None
        ),
        "definicao": (
            f"taxa de atendimento por hora do dia, no fuso de {FUSO_OPERACAO} "
            "(nunca UTC — 21h daqui é 00h lá, e o gráfico sairia invertido). "
            "Faixa sem ligação é VAZIA, não 0%. O melhor horário só é apontado "
            f"a partir de {AMOSTRA_MINIMA_HORARIO} ligações no período e "
            f"{AMOSTRA_MINIMA_FAIXA} na faixa vencedora: com pouca amostra, "
            "'as 14h convertem 100%' quer dizer 'uma pessoa atendeu uma vez'. "
            "Referência de mercado é 17h — mas o horário da Acme é o que este "
            "número disser, porque o público é outro."
        ),
    }


def _chave_do_lead(registro: Any) -> tuple[str, str]:
    """Identidade do lead num registro: external_ref quando existe, senão a
    chave da origem (leads.id, no caso do remarketing)."""
    if registro["lead_id"]:
        return ("ref", str(registro["lead_id"]))
    return (str(registro["origem"]), str(registro["origem_id"]))


def _tentativas_ate_atender(registros: Any) -> dict[str, Any]:
    """Quantas ligações, em média, até alguém atender.

    Só conta lead que ATENDEU dentro da janela — pra quem nunca atendeu não há
    resposta ainda (contar as tentativas dele como se fossem "até atender"
    inflaria a média com casos em aberto). `media` None = ninguém atendeu na
    janela: sem dado, não zero.

    Os registros já vêm ordenados por `criado_em` — a conta é uma passada só.
    """
    por_lead: dict[tuple[str, str], list[Any]] = {}
    for r in registros:
        por_lead.setdefault(_chave_do_lead(r), []).append(r)

    tentativas: list[int] = []
    for linha in por_lead.values():
        for i, r in enumerate(linha, start=1):
            if r["atendeu"]:
                tentativas.append(i)
                break

    return {
        "leads_que_atenderam": len(tentativas),
        "media": (
            round(sum(tentativas) / len(tentativas), 1) if tentativas else None
        ),
        "maximo": max(tentativas) if tentativas else None,
        "sem_dado": not tentativas,
        "definicao": (
            "média de ligações até o lead atender, contando só quem atendeu "
            "DENTRO da janela — quem nunca atendeu ainda não tem resposta e "
            "ficaria inflando a média como caso em aberto."
        ),
    }


def metricas_ligacao(
    session: Session,
    *,
    dias: int = 30,
    amostra_minima: int = 20,
    corretora_label: dict[int, str] | None = None,
    tenant_id: int = DEFAULT_TENANT_ID,
    agora: datetime | None = None,
) -> dict[str, Any]:
    """Ligações feitas · atenderam · quiseram continuar, por corretora e total."""
    agora = agora or datetime.now(timezone.utc)
    desde = agora - timedelta(days=dias)
    labels = corretora_label or {}

    # 1) universo — fila de ligação da CADÊNCIA
    fila = session.execute(
        sql_text(
            "SELECT ev.cadencia_id, ev.toque, ce.corretora_id "
            "FROM cadencia_eventos ev "
            "JOIN cadencia_estado ce ON ce.id = ev.cadencia_id "
            "WHERE ev.tenant_id = :t AND ev.tipo = :tipo "
            "  AND ev.at >= :desde AND ev.at <= :ate"
        ),
        {"t": tenant_id, "tipo": TIPO_EVENTO_LIGACAO, "desde": desde, "ate": agora},
    ).mappings().all()
    universo: dict[tuple[str, int, int], int | None] = {
        (ORIGEM_CADENCIA, int(r["cadencia_id"]), int(r["toque"] or 0)): r["corretora_id"]
        for r in fila
    }

    # 2) universo — tarefas MANUAIS de ligação concluídas na janela
    manuais = session.execute(
        sql_text(
            "SELECT id, texto, corretora_id FROM tarefas_corretora "
            "WHERE tenant_id = :t AND status = 'concluida' "
            "  AND concluido_em >= :desde AND concluido_em <= :ate"
        ),
        {"t": tenant_id, "desde": desde, "ate": agora},
    ).mappings().all()
    for r in manuais:
        if texto_e_de_ligacao(r["texto"]):
            universo[(ORIGEM_TAREFA, int(r["id"]), TOQUE_MANUAL)] = r["corretora_id"]

    # 3) resultados registrados na janela
    registros = session.execute(
        sql_text(
            "SELECT origem, origem_id, toque, lead_id, corretora_id, atendeu, "
            "       quis_continuar, criado_em "
            "FROM ligacao_resultado "
            "WHERE tenant_id = :t AND criado_em >= :desde AND criado_em <= :ate "
            "ORDER BY criado_em ASC, id ASC"
        ),
        {"t": tenant_id, "desde": desde, "ate": agora},
    ).mappings().all()

    linhas: dict[int | None, dict[str, Any]] = {}

    def _linha(cid: int | None) -> dict[str, Any]:
        if cid not in linhas:
            linhas[cid] = _linha_zerada(cid, labels.get(cid) if cid else None)
        return linhas[cid]

    chaves_com_registro: set[tuple[str, int, int]] = set()
    for r in registros:
        chave = (str(r["origem"]), int(r["origem_id"]), int(r["toque"] or 0))
        chaves_com_registro.add(chave)
        # Ligação da janela de remarketing não tem "fila" prévia: ela entra no
        # universo pelo próprio registro.
        universo.setdefault(chave, r["corretora_id"])
        # A corretora do registro manda; se vier vazia, cai na do universo.
        cid = r["corretora_id"] if r["corretora_id"] is not None else universo.get(chave)
        linha = _linha(cid)
        linha["ligacoes"] += 1
        if r["atendeu"]:
            linha["atenderam"] += 1
            if r["quis_continuar"] is True:
                linha["quiseram_continuar"] += 1
            elif r["quis_continuar"] is False:
                linha["nao_quiseram_continuar"] += 1
        else:
            linha["nao_atenderam"] += 1

    for chave, cid in universo.items():
        linha = _linha(cid)
        linha["total_na_fila"] += 1
        if chave not in chaves_com_registro:
            linha["sem_registro"] += 1

    # total: soma dos contadores crus; as taxas são recalculadas do total,
    # nunca média de médias.
    total = _linha_zerada(None, None)
    for linha in linhas.values():
        for campo in (
            "total_na_fila", "ligacoes", "sem_registro", "atenderam",
            "nao_atenderam", "quiseram_continuar", "nao_quiseram_continuar",
        ):
            total[campo] += int(linha[campo])

    por_corretora = sorted(
        (_fechar_linha(linha, amostra_minima) for linha in linhas.values()),
        key=lambda x: (-int(x["ligacoes"]), x["corretora"] or "zzz"),
    )

    return {
        "tentativas_ate_atender": _tentativas_ate_atender(registros),
        "quando_atendem": _quando_atendem(registros),
        "dias": dias,
        "desde": desde.isoformat(),
        "ate": agora.isoformat(),
        "amostra_minima": amostra_minima,
        "referencia_historica": REFERENCIA_HISTORICA,
        "por_corretora": por_corretora,
        "total": _fechar_linha(total, amostra_minima),
        "definicao": (
            "ligações = tarefas de ligação concluídas COM resultado registrado; "
            "sem_registro = concluídas sem ninguém dizer o que aconteceu — não "
            "entram em nenhuma taxa e NUNCA contam como 'não atendeu'; "
            "taxa_atendimento = atenderam / ligações; taxa_continuidade = "
            "quiseram continuar / atenderam (só de quem atendeu, é a única base "
            "honesta); taxa None = sem amostra, nunca 0%; abaixo de "
            f"{amostra_minima} a taxa vem marcada como ruído. A fila conta as "
            "ligações da régua (D1/D5/D9), as tarefas manuais de ligação "
            "concluídas na janela e as ligações feitas pela janela de "
            "remarketing."
        ),
    }
