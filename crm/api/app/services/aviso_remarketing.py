"""Aviso do remarketing — quem chega na coluna "Novo" COM a tag remarketing
recebe a apresentação pela linha da corretora que pegou o lead.

Por que existe (Dona 18/09/2026): "o certo é colocar a tag de remarketing, jogar
esses leads pra roleta, e aí a automação — caiu no Novo e tem tag de
remarketing, manda essa mensagem". O gatilho é o ESTADO do lead, não o evento
que o colocou lá. Isso cobre de uma vez os quatro caminhos de entrada:

  • `handoff_remarketing.processar` — o inbound automático da linha da Corretor;
  • o botão de transferir do painel;
  • o script de lote que varre a etapa "Respondeu" parada;
  • a roleta, quando redistribui.

O desenho anterior amarrava o envio DENTRO do inbound, e por isso não alcançava
nenhum dos outros três. Os 136 leads parados na etapa "Respondeu" em 18/09 são
o retrato disso: o handoff automático nasceu em 05/09, eles pararam de falar em
agosto, e ninguém mais passou por um inbound pra o aviso disparar.

═══════════════════════════════════════════════════════════════════════════
O QUE ENTRA (todas as condições, na ordem em que são checadas)
═══════════════════════════════════════════════════════════════════════════
  1. `handoff_remarketing_avisa_lead` ligado — kill-switch, nasce desarmado;
  2. horário civilizado (9h-19h SP, sem domingo) — mesma régua do anti-ghosting;
  3. lead com `veio_remarketing_em` preenchido (a tag) E em "Novo" (6);
  4. dono é corretora válida, com linha Cloud, que não é a Corretor;
  5. o telefone ainda não recebeu este aviso (`_ja_avisado`, pra sempre);
  6. não passou pelo handoff rico da Pessoa (`_teve_handoff_rico`);
  7. não pediu silêncio (opt-out nas duas fontes);
  8. a linha dela ainda não bateu o cap do dia.

As checagens 4 a 7 vivem em `handoff_remarketing._avisar_lead` — este módulo
NÃO reimplementa nenhuma. Ele seleciona, respeita o cap e chama. Um lugar só
decide se a mensagem sai, e é o mesmo que o inbound usa.

═══════════════════════════════════════════════════════════════════════════
CAP POR LINHA
═══════════════════════════════════════════════════════════════════════════
`aviso_remarketing_cap_linha` (default 10/dia/linha). As linhas das corretoras
passam semanas sem disparar template; 30 de uma vez é pico, e pico é o que a
Meta lê como qualidade. Com 3 linhas, 30 leads levam 1 dia; a fila inteira da
etapa "Respondeu" leva menos de uma semana.

O cap conta o que JÁ SAIU hoje (por `origem` na thread), não o que este tick
mandou: reiniciar o serviço não reabre a cota.
"""
from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy import text as sql_text

from app.db.session import SessionLocal
from app.services import config_operacional as cfg
from app.services import carrossel_corretor as cb
from app.services import handoff_remarketing as hr
from app.services.anti_ghosting import dentro_do_horario
from app.services.atendimento_corretor import STAGE_NOVO

log = structlog.get_logger()

# Ordem: quem esperou mais vai primeiro. A fila da etapa "Respondeu" tem lead de
# agosto — começar pelos recentes deixaria os mais velhos sempre no fim.
_SQL_CANDIDATOS = r"""
    SELECT l.id, l.telefone, l.nome, l.owner_id, c.nome AS corretora
      FROM leads l
      JOIN corretoras c ON c.id = l.owner_id AND c.ativo IS TRUE
     WHERE l.tenant_id = :t
       AND l.veio_remarketing_em IS NOT NULL
       AND l.stage_id = :novo
       AND l.owner_id <> :corretor
       AND coalesce(l.telefone, '') <> ''
       AND l.telefone_invalido IS NOT TRUE
     ORDER BY l.veio_remarketing_em
"""

# Quantos avisos cada linha já mandou HOJE (fuso SP), pela thread — a mesma
# fonte que dá a idempotência por telefone.
_SQL_ENVIADOS_HOJE = """
    SELECT corretora_id, count(*)
      FROM mensagens_whatsapp
     WHERE conteudo_metadata->>'origem' = :o
       AND (timestamp_evolution AT TIME ZONE 'America/Sao_Paulo')::date
           = (now() AT TIME ZONE 'America/Sao_Paulo')::date
     GROUP BY corretora_id
"""


def _enviados_hoje(session: Any) -> dict[int, int]:
    return {
        int(cid): int(n)
        for cid, n in session.execute(
            sql_text(_SQL_ENVIADOS_HOJE), {"o": hr.ORIGEM_AVISO}
        ).all()
        if cid is not None
    }


def rodar(*, tenant: int = 1, limite: int = 60, dry_run: bool = False) -> dict[str, Any]:
    """Um tick. Devolve o que fez e NUNCA levanta — um erro aqui não pode
    derrubar o timer nem segurar o lead na corretora errada.

    `limite` é o teto do tick inteiro (soma das linhas); o cap por linha é que
    manda de verdade.
    """
    r: dict[str, Any] = {"candidatos": 0, "enviados": 0, "pulados": {},
                         "dry_run": bool(dry_run)}

    def pulou(motivo: str) -> None:
        r["pulados"][motivo] = r["pulados"].get(motivo, 0) + 1

    try:
        if not cfg.handoff_remarketing_avisa_lead():
            r["pulados"]["desarmado"] = 1
            return r
        if not dentro_do_horario():
            r["pulados"]["fora_do_horario"] = 1
            return r

        cap = cfg.aviso_remarketing_cap_linha()
        with SessionLocal() as s:
            # A etapa de entrada é a da tela (05/10): com outra etapa escolhida,
            # é lá que o transferido chega, e olhar "Novo" não acharia ninguém.
            # Configuração ilegível levanta e cai no `except` de fora: não envia.
            from app.services import quando_responde

            etapa = quando_responde.ler(s, tenant).etapa
            linhas = s.execute(
                sql_text(_SQL_CANDIDATOS),
                {"t": tenant, "novo": etapa, "corretor": cb.CORRETORA_CORRETOR},
            ).all()
            r["candidatos"] = len(linhas)
            usado = _enviados_hoje(s)
            vistos: set[str] = set()

            for lead_id, telefone, nome, owner_id, corretora in linhas:
                if r["enviados"] >= limite:
                    pulou("limite_do_tick")
                    break
                owner_id = int(owner_id)
                # Mesmo número em dois cards: uma pessoa, uma mensagem.
                suf = "".join(ch for ch in (telefone or "") if ch.isdigit())[-8:]
                if suf in vistos:
                    pulou("telefone_repetido")
                    continue
                if cap and usado.get(owner_id, 0) >= cap:
                    pulou("cap_da_linha")
                    continue
                if dry_run:
                    # As MESMAS travas do envio real (opt-out, dedup, handoff
                    # rico, conversa em andamento) — senão o `--seco` promete
                    # um número que o tick de verdade não cumpre.
                    pular = hr.motivo_para_pular(s, cb._so_digitos(telefone or ""))
                    if pular:
                        pulou(pular)
                        vistos.add(suf)
                        continue
                    log.info("aviso_remarketing_dry_run", lead_id=lead_id,
                             corretora=corretora, tel=str(telefone)[-4:])
                    vistos.add(suf)
                    usado[owner_id] = usado.get(owner_id, 0) + 1
                    r["enviados"] += 1
                    continue

                res = hr.avisar_lead(
                    s, telefone=telefone, nome=nome,
                    corretora_id=owner_id, corretora_label=corretora,
                )
                vistos.add(suf)
                if res.get("ok"):
                    usado[owner_id] = usado.get(owner_id, 0) + 1
                    r["enviados"] += 1
                    log.info("aviso_remarketing_enviado", lead_id=lead_id,
                             corretora=corretora, tel=str(telefone)[-4:])
                else:
                    pulou(str(res.get("motivo") or "erro"))
        return r
    except Exception as e:  # noqa: BLE001
        log.warning("aviso_remarketing_falhou", erro=str(e)[:160])
        r["pulados"]["erro"] = r["pulados"].get("erro", 0) + 1
        return r
