"""A mensagem que continua a conversa depois da ligação.

O problema que ela resolve
--------------------------
A corretora liga, a pessoa atende e diz que quer continuar. Aí a conversa
morre: a corretora desliga e o lead fica sem nada no WhatsApp. Quando ela
lembra de escrever, horas depois, o contexto já esfriou e a mensagem chega como
abordagem fria de novo — a pessoa nem lembra que falou com ela.

Pedido da Dona (09/09/2026): ao tocar em "atendeu e quer continuar", o lead vai
pro CRM dela E sai a mensagem, na hora, dizendo que estavam falando por telefone
e que o atendimento continua ali.

Por que precisa de TEMPLATE e não de texto livre
------------------------------------------------
A janela de 24h do WhatsApp abre quando o CLIENTE escreve — ligação não abre
nada. Então a primeira mensagem depois da ligação está sempre fora da janela, e
fora da janela só passa template aprovado. Por isso os `acme_mc_pos_ligacao_*`
existem, em UTILITY (Marketing custa ~10x e queima a lista).

Template é POR WABA
-------------------
Cada corretora em coexistência tem WABA própria, e o mesmo nome pode estar em
categoria diferente em cada uma (`acme_ola_a_01` é UTILITY na Pessoa12 e MARKETING
na Pessoa10). Aqui a gente só usa o que `templates_utility()` confirmou APROVADO e
UTILITY *naquela* linha — linha nova sem template não dispara nada e diz por quê.

Travas
------
- Desarmado por padrão: `config_operacional.pos_ligacao_ativo` nasce False. A
  Dona liga quando quiser, sem deploy.
- Nunca envia pra quem pediu silêncio (`cadencia_estado.status='opt_out'` ou
  `disparos_reengajamento.opt_out`).
- Uma vez por lead por dia: registrar a ligação duas vezes (acontece, o botão é
  de rotina) não manda duas mensagens.
- Falha de envio NUNCA derruba o registro da ligação. O que a corretora fez
  precisa ficar gravado mesmo que a Meta esteja fora do ar — a mensagem é
  consequência, não condição.
"""
from __future__ import annotations

import re
import uuid
from typing import Any

from app.services.emission_gate import guarded

import httpx
import structlog
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from app.services import cloud_lines, config_operacional, nome_publico

log = structlog.get_logger()

API_VER = "v21.0"
DEFAULT_TENANT_ID = 1

# Prefixo do template por corretora. A WABA é dela, o template é dela.
#
# `acme_mc_continuidade` substituiu `acme_mc_pos_ligacao` em 09/09: o
# esqueleto genérico ("AVISO IMPORTANTE: {{1}}" + botões) aprova fácil em
# UTILITY, mas lido por quem acabou de desligar o telefone soa como aviso de
# banco — não como a corretora com quem a pessoa estava falando. A Dona pediu o
# texto direto e SEM botão. Os antigos seguem aprovados na WABA, sem uso.
# LISTA em ordem de preferência, não um nome só. O primeiro que tiver UTILITY
# aprovado ganha — assim, no minuto em que a Meta liberar `atend_a`, o sistema
# migra sozinho e para de gastar MARKETING, sem ninguém precisar lembrar de
# trocar. `continuidade` fica por último: é o texto certo, mas está em MARKETING.
# Veredito da Meta (09/09), testando o MESMO conteúdo em três roupagens:
#   atend_b       UTILITY   "seu atendimento ... foi REGISTRADO e continua por aqui"
#   atend_a       MARKETING "dando sequência ao atendimento ... sigo com você"
#   continuidade  MARKETING "podemos continuar nosso atendimento por aqui?"
# O que separou não foi o assunto — foi a FORMA. Frase que descreve um registro
# passa como utility; frase que convida a conversar vira marketing, mesmo dizendo
# a mesma coisa. Guardar isso vale mais que o template: é a régua pro próximo.
_BASE_POR_SLUG: dict[str, list[str]] = {
    "pessoa_d": ["acme_mc_atend_b", "acme_mc_atend_a", "acme_mc_continuidade"],
    # Corretor (20/09): mesmo papel da Pessoa7 — ligação + primeira mensagem. A WABA
    # dela nasceu vazia; enquanto os `acme_an_*` não aprovarem, o fluxo cai
    # em "sem_template_aprovado" e não manda nada, que é o certo.
    "corretor": ["acme_an_atend_b", "acme_an_atend_a"],
}

# corretora_id → slug da linha. Mesma tabela do _DESTINO em cloud_corretora,
# na direção inversa (aqui a gente parte do id que veio do registro).
_SLUG_POR_CORRETORA: dict[int, str] = {
    1: "corretor",
    2: "corretor",
    7: "pessoa_d",
    8: "corretor",
}

# Corpo do template (aprovado na WABA da corretora), com DUAS variáveis:
#   Oi {{1}}, acabamos de nos falar por telefone. Aqui é a {{2}}, da Acme
#   Imóveis — podemos continuar nosso atendimento por aqui?
# Sem botão, por decisão da Dona: a pessoa acabou de falar no telefone, ela
# responde escrevendo. Botão ali é fricção de formulário numa conversa.


def _texto(primeiro_nome: str, corretora_nome: str) -> str:
    """O que a pessoa lê — usado só pra gravar na thread (o envio manda as
    variáveis, a Meta monta o corpo). Espelha o `atend_b`, que é o aprovado."""
    return (
        f"Oi {primeiro_nome}, seu atendimento com a Acme Imóveis, iniciado "
        f"por telefone hoje, foi registrado e continua por aqui com "
        f"{corretora_nome}. Pode responder nesta conversa."
    )


def _primeiro_nome(nome: str | None) -> str:
    n = (nome or "").strip()
    if not n or n.lower().startswith("lead #"):
        return "Olá"
    return n.split()[0][:24]


def _e164(telefone: str | None) -> str | None:
    d = re.sub(r"\D", "", telefone or "")
    if not d:
        return None
    if not d.startswith("55"):
        d = "55" + d
    return d if 12 <= len(d) <= 13 else None


def _pediu_silencio(session: Session, telefone: str) -> bool:
    """Opt-out em qualquer uma das duas fontes. Na dúvida (erro de query),
    devolve True: silêncio é o lado seguro de errar."""
    suf = re.sub(r"\D", "", telefone)[-8:]
    try:
        row = session.execute(
            sql_text(
                """
                SELECT
                  EXISTS (SELECT 1 FROM cadencia_estado
                           WHERE right(regexp_replace(telefone,'\\D','','g'),8) = :s
                             AND status = 'opt_out')
               OR EXISTS (SELECT 1 FROM disparos_reengajamento
                           WHERE right(regexp_replace(telefone,'\\D','','g'),8) = :s
                             AND opt_out)
                """
            ),
            {"s": suf},
        ).scalar()
        return bool(row)
    except Exception:
        log.warning("pos_ligacao_optout_indisponivel", sufixo=suf)
        return True


def _ja_mandou_hoje(session: Session, lead_id: int) -> bool:
    """Já saiu um pós-ligação pra esse lead nas últimas 24h?

    `delivery_status = 'failed'` NÃO conta. A Meta recusou: a pessoa não recebeu
    nada, e a trava anti-duplicata estava impedindo o reenvio legítimo — em
    21/09/2026 três leads da Pessoa7 (1040, 1041, 1073) ficaram presos assim, e
    só saíram com um contorno manual. Falha calada: ninguém é avisado, a
    mensagem simplesmente não vai e o lead esfria.

    `NULL` conta como enviado, de propósito: a Meta devolveu `wamid` e o status
    ainda não voltou pelo webhook. Reenviar nesse estado é duplicar de verdade.
    Os outros valores (`sent`, `delivered`, `read`, `played`) também contam.
    """
    try:
        return bool(
            session.execute(
                sql_text(
                    """
                    SELECT EXISTS (
                      SELECT 1 FROM mensagens_whatsapp
                       WHERE conteudo_metadata->>'origem' = 'pos_ligacao'
                         AND conteudo_metadata->>'lead_id' = :lid
                         AND coalesce(conteudo_metadata->>'delivery_status', '') <> 'failed'
                         AND timestamp_evolution > now() - interval '1 day')
                    """
                ),
                {"lid": str(lead_id)},
            ).scalar()
        )
    except Exception:
        return False


@guarded
def enviar(
    session: Session,
    *,
    lead_id: int,
    telefone: str | None,
    nome: str | None,
    corretora_id: int | None,
    corretora_nome: str | None,
    tenant_id: int = DEFAULT_TENANT_ID,
) -> dict[str, Any]:
    """Manda a mensagem de continuidade. Nunca levanta — devolve o motivo.

    Quem chama registra a ligação de qualquer jeito; isto aqui é um extra que
    pode falhar sem levar o resto junto.
    """
    if not bool(config_operacional.get("pos_ligacao_ativo")):
        return {"ok": False, "motivo": "desarmado"}

    slug = _SLUG_POR_CORRETORA.get(int(corretora_id or 0))
    bases = _BASE_POR_SLUG.get(slug or "") or []
    if not slug or not bases:
        return {"ok": False, "motivo": "corretora_sem_template"}

    to = _e164(telefone)
    if not to:
        return {"ok": False, "motivo": "telefone_invalido"}
    if _pediu_silencio(session, to):
        return {"ok": False, "motivo": "opt_out"}
    if _ja_mandou_hoje(session, lead_id):
        return {"ok": False, "motivo": "ja_enviado_hoje"}

    linha = cloud_lines.line_by_slug(slug)
    token = cloud_lines.token_for(linha) if linha else None
    if not linha or not token:
        log.error("pos_ligacao_linha_sem_token", slug=slug)
        return {"ok": False, "motivo": "linha_sem_token"}

    # Só o que a Meta confirmou APROVADO nesta WABA. UTILITY sempre; MARKETING
    # apenas enquanto a ponte estiver aberta.
    #
    # Por que a ponte existe: o texto natural que a Dona quer ("podemos
    # continuar nosso atendimento por aqui?") foi classificado pela Meta como
    # MARKETING — o "AVISO IMPORTANTE" que ela cortou era justamente o que fazia
    # a mensagem ler como aviso de serviço. Deixar o lead quente esperando pela
    # reanálise custa mais que a tarifa; a troca pra UTILITY é a próxima parada.
    util = cloud_lines.templates_utility(slug)
    aprovados: list[str] = []
    categoria_usada = "UTILITY"
    base = ""
    for b in bases:
        achados = sorted(n for n in util if n.startswith(b))
        if achados:
            aprovados, base = achados, b
            break
    if not aprovados and bool(config_operacional.get("pos_ligacao_permite_marketing")):
        mkt = cloud_lines._buscar_categoria_na_meta(slug, "MARKETING") or set()
        for b in bases:
            achados = sorted(n for n in mkt if n.startswith(b))
            if achados:
                aprovados, base, categoria_usada = achados, b, "MARKETING"
                log.warning("pos_ligacao_usando_marketing", slug=slug, base=b,
                            n=len(achados))
                break
    if not aprovados:
        log.warning("pos_ligacao_sem_template_aprovado", slug=slug, bases=bases)
        return {"ok": False, "motivo": "sem_template_aprovado"}

    primeiro = _primeiro_nome(nome)
    # Nome PÚBLICO: a Pessoa3 atende como "Pessoa" e é esse nome que o lead
    # conhece. `corretora_nome` (interno) só entra se o cadastro não souber
    # dizer — ver app/services/nome_publico.py e o caso Kauane (18/09).
    corretora = (
        nome_publico.publico(session, corretora_id)
        or nome_publico.primeiro_nome(corretora_nome)
        or "sua corretora"
    )
    params = [
        {"type": "text", "text": primeiro},
        {"type": "text", "text": corretora},
    ]
    url = f"https://graph.facebook.com/{API_VER}/{linha.phone_id}/messages"

    # Rotação: espalha o uso entre as variantes aprovadas. Se uma for
    # reclassificada pra marketing, as outras seguram o fluxo.
    start = (sum(ord(c) for c in to)) % len(aprovados)
    ultimo_erro = ""
    for k in range(len(aprovados)):
        tmpl = aprovados[(start + k) % len(aprovados)]
        body = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "template",
            "template": {
                "name": tmpl,
                "language": {"code": "pt_BR"},
                "components": [{"type": "body", "parameters": params}],
            },
        }
        try:
            with httpx.Client(timeout=20) as c:
                resp = c.post(
                    url,
                    headers={"Authorization": f"Bearer {token}",
                             "Content-Type": "application/json"},
                    json=body,
                )
        except Exception as e:  # noqa: BLE001 — rede
            ultimo_erro = f"rede: {str(e)[:120]}"
            continue

        if resp.status_code < 400:
            data = resp.json()
            wamid = (data.get("messages") or [{}])[0].get("id") or str(uuid.uuid4())
            _gravar(session, wamid=wamid, to=to, nome=nome, lead_id=lead_id,
                    corretora_id=corretora_id, instancia=f"acme_{slug}",
                    texto=_texto(primeiro, corretora), template=tmpl,
                    tenant_id=tenant_id)
            log.info("pos_ligacao_enviado", lead_id=lead_id, slug=slug,
                     template=tmpl, categoria=categoria_usada, wamid=wamid)
            return {"ok": True, "wamid": wamid, "template": tmpl,
                    "categoria": categoria_usada}

        ultimo_erro = f"{resp.status_code}: {resp.text[:160]}"
        log.warning("pos_ligacao_template_pulou", template=tmpl, erro=ultimo_erro)

    log.error("pos_ligacao_falhou", lead_id=lead_id, slug=slug, erro=ultimo_erro)
    return {"ok": False, "motivo": "envio_falhou", "erro": ultimo_erro}


def _gravar(
    session: Session,
    *,
    wamid: str,
    to: str,
    nome: str | None,
    lead_id: int,
    corretora_id: int | None,
    instancia: str,
    texto: str,
    template: str,
    tenant_id: int,
) -> None:
    """Grava na thread do lead, com `origem='pos_ligacao'`.

    Sem isto a mensagem sai e some: a corretora abre a conversa e vê um vão, e a
    trava de conversa humana da cadência não enxerga que houve contato. A marca
    `origem` também é o que distingue esta mensagem de uma escrita à mão — o
    relatório conta mensagem humana pela AUSÊNCIA de origem na metadata.
    """
    try:
        session.execute(
            sql_text(
                """
                INSERT INTO mensagens_whatsapp
                  (evolution_message_id, instance, corretora_id, remote_jid,
                   contato_telefone, contato_nome, from_corretora, tipo_mensagem,
                   conteudo_texto, conteudo_metadata, timestamp_evolution,
                   recebido_em, tenant_id)
                VALUES
                  (:wamid, :inst, :cid, :jid, :tel, :nome, true, 'template', :texto,
                   -- CAST(... AS text) e não `:lid::text`: o SQLAlchemy lê o
                   -- `::` como outro parâmetro nomeado e quebra a sintaxe. E o
                   -- CAST não é decorativo — sem ele o Postgres não infere o
                   -- tipo dentro do jsonb_build_object e recusa com
                   -- "could not determine data type of parameter".
                   jsonb_build_object('origem','pos_ligacao',
                                      'lead_id', CAST(:lid AS text),
                                      'template', CAST(:tmpl AS text)),
                   now(), now(), :tid)
                ON CONFLICT DO NOTHING
                """
            ),
            {"wamid": wamid, "inst": instancia, "cid": corretora_id,
             "jid": f"{to}@s.whatsapp.net", "tel": to, "nome": nome,
             "texto": texto, "lid": str(lead_id), "tmpl": template,
             "tid": tenant_id},
        )
    except Exception as e:  # noqa: BLE001
        # A mensagem JÁ foi entregue — falhar aqui não desfaz nada, mas o
        # resultado é o PIOR dos mundos: o lead recebeu e ninguém vê. Aconteceu
        # em 09/09 (faltava `remote_jid`, NOT NULL) e passou batido porque o
        # erro só existia no log. Se voltar a falhar, é pra doer.
        log.error("pos_ligacao_gravacao_falhou", lead_id=lead_id,
                  wamid=wamid, telefone=to, erro=str(e)[:200])
