"""
Fonte de leads do Inspector — lê do banco próprio, não do Kommo.

POR QUE ISTO EXISTE
-------------------
Até 05/08/2026 o lead entrava pelo Kommo, e o Inspector montava a lista de
quem analisar perguntando ao Kommo. Em 05/08 a entrada mudou de casa: o
webhook `meta_leadgen` passou a gravar direto em `public.leads`. Ninguém
religou o Inspector na fonte nova.

Resultado medido em 25/08/2026: **127 leads criados desde 06/08, ZERO
analisados** — inclusive 93 que responderam a corretora. O briefing das 20h
nunca viu nenhum deles. Não era o classificador errando: os leads mais novos
(e pagos, CPL ~R$14) nunca chegavam a ele.

IDENTIDADE DO LEAD
------------------
`lead_id` continua sendo `leads.external_ref` — é a chave que as 18.446
análises já existentes usam. Lead do Kommo trazia o id do Kommo; lead novo
traz o id do formulário da Meta. São faixas distintas, não colidem, e o
histórico segue casando.

CASAMENTO COM A CONVERSA
------------------------
Pela coluna gerada `fone8` (últimos 8 dígitos), presente e indexada em
`leads` e em `mensagens_whatsapp`. Ignora +55, DDD com/sem o 9 e formatação.
Antes disso o casamento era `regexp_replace` dos dois lados dentro do JOIN:
sem índice possível, a consulta dos parados levava mais de 7 minutos e chegou
a travar a fila do banco. Com a coluna, 0,2s.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import text

from app.db.session import SessionLocal

# Espelha a janela que o Kommo usava (get_leads_for_inspector, days_back=2).
_DIAS_LEAD_NOVO = 2
_HORAS_CONVERSA_RECENTE = 24
# "Parado" = a pessoa falou por último e ninguém respondeu. É o que a operação
# perde de vista. 14 dias porque além disso o lead esfria e vira reativação,
# que é outra conversa (e outra abordagem).
_DIAS_PARADO = 14

_SQL = """
with ult as (
    select distinct on (l.id)
        l.id,
        l.external_ref,
        l.nome,
        l.telefone,
        l.stage_id,
        l.created_at,
        m.from_corretora   as ultimo_foi_corretora,
        m.timestamp_evolution as ultima_msg
    from leads l
    left join mensagens_whatsapp m on m.fone8 = l.fone8 and l.fone8 <> ''
    where l.owner_id = :corretora_id
      and coalesce(l.telefone_invalido, false) = false
    order by l.id, m.timestamp_evolution desc nulls last
)
select external_ref, nome, telefone, stage_id, created_at,
    case
        when created_at >= now() - make_interval(days => :dias_novo) then 'lead_novo'
        when ultima_msg >= now() - make_interval(hours => :horas_recente) then 'atividade_24h'
        else 'parado'
    end as entrou_por,
    ultima_msg,
    ultimo_foi_corretora
from ult
where external_ref is not null and external_ref <> ''
  and (
        created_at >= now() - make_interval(days => :dias_novo)
     or ultima_msg >= now() - make_interval(hours => :horas_recente)
     or (ultimo_foi_corretora = false
         and ultima_msg >= now() - make_interval(days => :dias_parado))
  )
order by ultima_msg desc nulls last
"""


def get_leads_for_inspector_db(corretora_id: int) -> list[dict[str, Any]]:
    """
    Leads de uma corretora que merecem passar pelo Inspector hoje.

    Devolve o MESMO schema achatado que `KommoTool.get_leads_for_inspector`
    devolvia, pra o pipeline não precisar saber de onde veio.

    Três motivos de entrar, no campo `entrou_por`:
      - `lead_novo`     — criado nas últimas 48h (nunca foi analisado)
      - `atividade_24h` — trocou mensagem nas últimas 24h
      - `parado`        — a PESSOA falou por último e ninguém respondeu,
                          há até 14 dias. É o balde que a operação perdia.
    """
    with SessionLocal() as s:
        rows = s.execute(
            text(_SQL),
            {
                "corretora_id": corretora_id,
                "dias_novo": _DIAS_LEAD_NOVO,
                "horas_recente": _HORAS_CONVERSA_RECENTE,
                "dias_parado": _DIAS_PARADO,
            },
        ).mappings().all()

    return [
        {
            "lead_id": str(r["external_ref"]),
            "nome": r["nome"] or "",
            "telefone": r["telefone"] or "",
            "etapa_id": r["stage_id"],
            # O pipeline grava isso em LeadSnapshot.criado_em_kommo via
            # _unix_to_dt, que espera epoch — mesmo contrato do Kommo.
            "criado_em": int(r["created_at"].timestamp()) if r["created_at"] else None,
            "entrou_por": r["entrou_por"],
            "ultima_msg": r["ultima_msg"],
            "aguardando_resposta": r["ultimo_foi_corretora"] is False,
        }
        for r in rows
    ]
