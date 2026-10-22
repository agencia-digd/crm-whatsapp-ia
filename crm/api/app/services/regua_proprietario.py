"""Régua do proprietário — "seu imóvel ainda está à venda?" a cada 40 dias.

Pedido da Dona em 16/09, depois que a auditoria achou R$ 10,36 milhões de
captação parada: *"podemos colocar esses leads num outro funil né? pra ter a
etapa de proprietários, pra a cada 40 dias enviarmos mensagem perguntando se o
imóvel ainda tá à venda"*.

## Por que a régua roda sobre o IMÓVEL e não sobre o lead

Ela falou em mover o lead pra um funil de proprietário, e eu fiz diferente de
propósito: **oito dos 37 querem vender E comprar.** O Kiko vende o apto de
R$ 290 mil e quer outro de R$ 260 mil; a Caroline vende a casa de R$ 700 mil e
quer uma menor; a Michele e o Vitor estão num negócio de compra em andamento.

Mover o lead pra um funil de proprietário tiraria essas pessoas do funil de
compra — a casa perderia metade do negócio de cada uma. Como o imóvel já tem
registro próprio (`imoveis_captados`), a régua roda ali: o mesmo humano pode
estar em "Em atendimento" como comprador e ter três imóveis em régua como
vendedor, sem os dois ciclos se atropelarem.

## A roda

```
  imóvel disponível ──40 dias sem contato──► pergunta pela linha da Corretor
                                                      │
                          ┌───────────────────────────┤
                          ▼                           ▼
                  [Prosseguir]                      [Encerrar]
                  segue na roda                   sai da roda
                  +40 dias                        follow pergunta se
                                                  vendeu ou desistiu
```

## As travas, e o incidente que cada uma evita

  opt-out            a trava canônica de `enviar_template_linha` já barra, mas
                     a fila também filtra: mandar e ser bloqueado gasta cota e
                     conta como sinal ruim na linha da casa.

  situação           só `disponivel`. Vendido, desistiu ou "com outra
                     imobiliária" saem da roda na hora.

  conversa humana    se a corretora falou com o dono nos últimos 40 dias, o
                     robô não fala por cima. Foi o que estragou o handoff da
                     Corretor em 15/09.

  cap por tick       teto pequeno e diário. A linha é o 0000-0000, o telefone
                     DA CASA — rajada ali a Meta lê como spam.

  janela             9h-19h, sem domingo. Perguntar do imóvel de alguém às 22h
                     de um domingo é o tipo de coisa que faz a pessoa bloquear.

⚠️ NASCE DESARMADA. `regua_proprietario_ativa` é False até a Dona ligar.

O template é o `continuar_atendimento_zgsuyd`, UTILITY **já aprovado** na WABA
da Corretor — nada a submeter. O corpo é fixo e genérico; o detalhe do imóvel vai no
follow-up de texto livre, dentro da janela de 24h que o clique abre. Frase que
convida a conversar viraria MARKETING e custa ~9x (R$ 0,3125 × R$ 0,0340), e é
por isso que o template fala de REGISTRO e não de oferta.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.services import config_operacional as cfg

log = structlog.get_logger(__name__)

SP = ZoneInfo("America/Sao_Paulo")

DIAS_ENTRE_TOQUES = 40
CAP_POR_DIA = 15          # teto conservador: é o telefone da casa
HORA_INICIO, HORA_FIM = 9, 19
# Template UTILITY JÁ APROVADO na WABA da Corretor, reaproveitado a pedido da Dona
# em 16/09 ("já temos templates utility que vc pode adaptar"). Corpo fixo:
#
#   "Há um atendimento em andamento relacionado ao seu cadastro sobre imóvel
#    em Cidade. Deseja prosseguir ou encerrar o registro?"
#   [Prosseguir] [Encerrar]
#
# Escolhido entre os 22 UTILITY aprovados porque é o ÚNICO cujos botões falam
# de REGISTRO, não de assinatura. O `acme_corretor_prot_a_01` encaixaria melhor no
# motor (tem as 3 variáveis), mas o botão "não" dele é "Parar mensagens" — ou
# seja, opt-out permanente. Dono clicando ali achando que diz "não está mais à
# venda" nunca mais receberia nada da Acme.
#
# O preço de usar este é não personalizar (não tem {{1}}). A volta: o clique
# ABRE A JANELA DE 24H, e aí a Corretor manda texto livre, de graça, já com o
# imóvel e o valor — que é o `_texto()` daqui.
TEMPLATE = ("continuar_atendimento_zgsuyd", "pt_BR")
# A Corretor é a corretora 4 — a linha DA CASA. Trocou de número em 15/09/2026
# (0000-0000 → 0000-0000), mesma WABA e mesmo phone_number_id. Por isso aqui
# se resolve a linha pelo ID da corretora e nunca por número cravado: quem
# cravou o 0000 em algum lugar vai mandar pro telefone morto.
CORRETORA_CORRETOR = 4
EVENTO_TOQUE = "regua_proprietario"
ORIGEM = "regua_proprietario"


@dataclass
class ReguaResult:
    elegiveis: int = 0
    enviados: int = 0
    bloqueios: dict[str, int] = field(default_factory=dict)
    detalhes: list[dict[str, Any]] = field(default_factory=list)

    def bloqueio(self, motivo: str) -> None:
        self.bloqueios[motivo] = self.bloqueios.get(motivo, 0) + 1


# Imóveis que devem receber o toque hoje.
#
# O "último contato" considera o toque da régua E qualquer conversa humana com
# o dono: se a corretora falou com ele semana passada, perguntar por robô se o
# imóvel está à venda é constrangedor e queima a relação.
_SQL_FILA = r"""
    SELECT i.id, i.dono_nome, i.dono_telefone, i.tipo, i.bairro,
           i.preco_pedido, i.captado_em, i.corretora_id
      FROM imoveis_captados i
     WHERE i.tenant_id = :t
       AND i.situacao = 'disponivel'
       AND i.dono_telefone IS NOT NULL
       -- último toque da régua neste imóvel
       AND NOT EXISTS (
            SELECT 1 FROM imovel_evento e
             WHERE e.imovel_id = i.id
               AND e.tipo = :evento
               AND e.em > now() - make_interval(days => :dias)
       )
       -- conversa humana com o dono na janela: robô não fala por cima de gente
       AND NOT EXISTS (
            SELECT 1 FROM mensagens_whatsapp m
             WHERE right(regexp_replace(m.contato_telefone, '\D', '', 'g'), 8)
                 = right(regexp_replace(i.dono_telefone, '\D', '', 'g'), 8)
               AND m.timestamp_evolution > now() - make_interval(days => :dias)
               AND coalesce(m.conteudo_metadata->>'origem', '') = ''
       )
       -- nunca tocar antes de o imóvel ter 40 dias de casa: captado hoje e
       -- perguntado amanhã é o robô duvidando do próprio cadastro
       AND i.captado_em < now() - make_interval(days => :dias)
     ORDER BY i.captado_em
     LIMIT :cap
"""


def _dentro_da_janela(agora: datetime | None = None) -> tuple[bool, str]:
    a = (agora or datetime.now(timezone.utc)).astimezone(SP)
    if a.weekday() == 6:
        return False, "domingo"
    if not (HORA_INICIO <= a.hour < HORA_FIM):
        return False, "fora_do_horario"
    return True, ""


def _texto(imovel: dict[str, Any]) -> tuple[str, str]:
    """(gancho, cta) do template. Descreve um REGISTRO — é o que passa utility.

    Sem preço o texto muda de propósito: "cadastrado por R$ None" é pior que
    não citar valor, e 21 dos 39 imóveis entraram sem preço justamente porque
    ninguém perguntou.

    ⚠️ O texto NÃO pede pra escrever ("me avisa", "responde aqui"): o template
    tem BOTÃO (`BOTOES`), e pedir texto livre com botão na tela é dar duas
    instruções contraditórias pro dono. A pergunta termina em "?" e os botões
    respondem — pedido da Dona em 16/09: "aí não tem botão nenhum que a pessoa
    vai falar sim ou não, e ela vai saber: se for sim ela continua, se for não
    ela tira do coiso".
    """
    # "seu casa" e "chacara" sem acento saindo pro dono do imóvel é o tipo de
    # detalhe que faz a mensagem parecer robô antes de ela ser lida.
    tipo = {"chacara": "chácara", "comercial": "imóvel comercial"}.get(
        imovel["tipo"], imovel["tipo"] or "imóvel"
    )
    fem = tipo in ("casa", "chácara")
    posse, artigo = ("sua", "Ela") if fem else ("seu", "Ele")
    bairro = imovel["bairro"] or ""
    # "em" e não "no/na": o gênero do bairro é imprevisível ("no Bairro X"
    # está errado, "na Bairro A" também) e "em" funciona nos dois.
    onde = f" em {bairro}" if bairro and "confirmar" not in bairro.lower() else ""
    preco = imovel.get("preco_pedido")
    if preco:
        valor = f"R$ {float(preco):,.0f}".replace(",", ".")
        gancho = (f"{posse} {tipo}{onde} está cadastrad{'a' if fem else 'o'} com a "
                  f"gente por {valor}. {artigo} continua disponível pra venda?")
    else:
        gancho = (f"{posse} {tipo}{onde} está cadastrad{'a' if fem else 'o'} com a "
                  f"gente. {artigo} continua disponível pra venda?")
    return gancho, "Responder"


def tick(
    *,
    tenant_id: int = 1,
    agora: datetime | None = None,
    dry_run: bool = False,
    session: Session | None = None,
) -> ReguaResult:
    """Uma rodada da régua. Seguro pra rodar de minuto em minuto."""
    res = ReguaResult()

    if not cfg.get("regua_proprietario_ativa"):
        res.bloqueio("desarmada")
        return res

    ok, motivo = _dentro_da_janela(agora)
    if not ok:
        res.bloqueio(motivo)
        return res

    fechar = session is None
    s = session or SessionLocal()
    try:
        linhas = s.execute(
            sql_text(_SQL_FILA),
            {"t": tenant_id, "evento": EVENTO_TOQUE,
             "dias": DIAS_ENTRE_TOQUES, "cap": CAP_POR_DIA},
        ).mappings().all()
        res.elegiveis = len(linhas)
        if not linhas:
            return res

        from app.api.cloud_corretora_a import enviar_template_linha
        from app.services import cloud_lines

        # A Corretor é a corretora 4 — o 0000-0000, a linha DA CASA. É o número
        # certo pra falar com proprietário: a captação é da imobiliária, não
        # da corretora que atendeu, e a comissão nasce do corretora_id do
        # imóvel, não de quem mandou a mensagem.
        linha = cloud_lines.linha_da_corretora(CORRETORA_CORRETOR, session=s)
        if linha is None:
            log.error("regua_proprietario_sem_linha", slug="corretor")
            res.bloqueio("linha_indisponivel")
            return res

        for im in linhas:
            gancho, cta = _texto(dict(im))
            if dry_run:
                res.detalhes.append({
                    "imovel_id": im["id"], "para": (im["dono_telefone"] or "")[-4:],
                    "texto": gancho,
                })
                res.enviados += 1
                continue

            r = enviar_template_linha(
                linha,
                im["dono_telefone"],
                im["dono_nome"],
                hook=gancho,
                cta=cta,
                instance="acme_corretor",
                corretora_id=CORRETORA_CORRETOR,
                origem=ORIGEM,
                template_fixo=TEMPLATE,
            )
            if not r.get("ok"):
                res.bloqueio(str(r.get("erro") or "envio_falhou"))
                continue

            # O evento É o relógio da régua: sem ele o imóvel seria tocado de
            # novo no próximo tick. Gravar ANTES de qualquer outra coisa.
            s.execute(
                sql_text("""
                    INSERT INTO imovel_evento (imovel_id, tipo, por, nota, em)
                    VALUES (:i, :tipo, :por, :nota, now())
                """),
                {"i": im["id"], "tipo": EVENTO_TOQUE, "por": ORIGEM,
                 "nota": gancho[:300]},
            )
            s.commit()
            res.enviados += 1
            res.detalhes.append({"imovel_id": im["id"], "wamid": r.get("wamid")})

        log.info("regua_proprietario_tick", elegiveis=res.elegiveis,
                 enviados=res.enviados, bloqueios=res.bloqueios, dry_run=dry_run)
        return res
    finally:
        if fechar:
            s.close()


# ── Resposta do dono ─────────────────────────────────────────────────────────
#
# Os três botões do template. O rótulo chega no webhook em `button.text`
# (`cloud_corretora_a`, tipo "button"), então o casamento é por TEXTO — mudar um
# rótulo aqui sem recriar o template na Meta quebra o reconhecimento.
#
# São três e não dois de propósito: "não" junta duas coisas diferentes. Vendeu
# é desfecho (e a casa quer saber por quanto, pra calibrar preço na região);
# desistiu é o imóvel voltando pro mercado daqui a alguns meses. Tratar os dois
# como o mesmo "não" joga fora a metade que volta.
BOTOES = {
    # Rótulos EXATOS do template aprovado. Mudar aqui sem recriar o template na
    # Meta quebra o reconhecimento — o clique chega no webhook em `button.text`.
    "Prosseguir": "disponivel",
    # "Encerrar" não diz SE vendeu ou desistiu, e chutar "vendido" afirma uma
    # venda que ninguém viu. "desistiu" é o conservador: tira da roda igual, e
    # é o estado que volta pro mercado. A diferença é perguntada na janela de
    # 24h que o próprio clique abriu, por texto livre e sem custo.
    "Encerrar": "desistiu",
}
# Quanto tempo depois do toque um clique ainda conta. Gente responde template
# dias depois; passou disso, o clique provavelmente é de outra conversa.
JANELA_RESPOSTA_DIAS = 7

_VENDEU = ("vendi", "vendido", "ja vendi", "já vendi", "foi vendido", "negociei")
_DESISTIU = ("desisti", "nao vou mais vender", "não vou mais vender",
             "tirei do mercado", "nao esta mais", "não está mais",
             # "aluguei" é a forma correta; "alugei" aparece muito no WhatsApp
             "aluguei", "alugei", "aluguei ele", "ja alugue")
_SIM = ("sim", "continua", "ainda", "esta sim", "está sim", "disponivel", "disponível")


def classificar_resposta(texto: str | None) -> str | None:
    """'vendido' | 'desistiu' | 'disponivel' | None (não deu pra saber).

    Conservador de propósito: na dúvida devolve None e um humano decide. Marcar
    "vendido" por engano some com o imóvel da carteira, e a pessoa que
    respondeu "vendi o carro pra dar entrada" não está falando do imóvel.
    """
    if not texto:
        return None
    # Clique de botão é resposta EXATA e sem ambiguidade — vale antes de
    # qualquer heurística de texto.
    if texto.strip() in BOTOES:
        return BOTOES[texto.strip()]
    t = texto.strip().lower()
    if any(p in t for p in _VENDEU):
        return "vendido"
    if any(p in t for p in _DESISTIU):
        return "desistiu"
    # "sim" só vale em mensagem curta: "sim, mas quero mudar o preço" é conversa
    if len(t.split()) <= 4 and any(t.startswith(p) for p in _SIM):
        return "disponivel"
    return None


def registrar_resposta(
    imovel_id: int, texto: str, *, por: str = "dono", session: Session | None = None
) -> str | None:
    """Aplica a resposta do dono na situação do imóvel. Devolve o que entendeu."""
    veredito = classificar_resposta(texto)
    if veredito is None:
        return None

    fechar = session is None
    s = session or SessionLocal()
    try:
        atual = s.execute(
            sql_text("SELECT situacao FROM imoveis_captados WHERE id = :i"),
            {"i": imovel_id},
        ).scalar()
        if atual is None or atual == veredito:
            return veredito
        s.execute(
            sql_text("""UPDATE imoveis_captados
                           SET situacao = :nova, atualizado_em = now()
                         WHERE id = :i"""),
            {"nova": veredito, "i": imovel_id},
        )
        # `de` e `para` são JSONB (guardam o estado inteiro, não só o rótulo),
        # então string crua explode com "invalid input syntax for type json".
        # `to_jsonb(text)` produz a string JSON válida.
        s.execute(
            sql_text("""
                INSERT INTO imovel_evento (imovel_id, tipo, de, para, por, nota, em)
                VALUES (:i, 'situacao', to_jsonb(CAST(:de AS text)),
                        to_jsonb(CAST(:para AS text)), :por, :nota, now())
            """),
            {"i": imovel_id, "de": atual, "para": veredito, "por": por,
             "nota": texto[:300]},
        )
        s.commit()
        log.info("regua_proprietario_resposta", imovel_id=imovel_id,
                 de=atual, para=veredito)
        return veredito
    finally:
        if fechar:
            s.close()


def _followup(session: Session, alvo: dict[str, Any], aplicado: str | None) -> None:
    """Texto livre logo após o clique, dentro da janela de 24h que ele abriu.

    Best-effort: se falhar, a situação do imóvel JÁ foi atualizada — que é a
    parte que não pode se perder. A mensagem é cortesia e desambiguação.
    """
    try:
        from app.api.cloud_corretora_a import enviar_cloud
        from app.services import cloud_lines

        linha = cloud_lines.linha_da_corretora(CORRETORA_CORRETOR, session=session)
        if linha is None:
            return

        imovel = session.execute(
            sql_text("""SELECT tipo, bairro, preco_pedido
                          FROM imoveis_captados WHERE id = :i"""),
            {"i": alvo["imovel_id"]},
        ).mappings().first()
        if imovel is None:
            return

        detalhe, _ = _texto(dict(imovel))
        if aplicado == "disponivel":
            # Confirma o que ficou registrado e abre espaço pra corrigir o
            # preço — que é a informação que mais envelhece nesta base.
            msg = (f"Perfeito, obrigada! Só pra confirmar: {detalhe[:-1]}"
                   f"\n\nSe o valor mudou, é só me falar que eu atualizo aqui.")
        else:
            # "Encerrar" não distingue vendido de desistiu. Perguntar aqui sai
            # de graça e a resposta vale: uma some do mercado, a outra volta.
            msg = ("Entendi, vou encerrar o registro. Só pra eu anotar certo: "
                   "o imóvel foi vendido ou você desistiu de vender por agora?")

        enviar_cloud(
            alvo.get("dono_telefone") or "",
            msg,
            linha=linha,
            instance="acme_corretor",
            corretora_id=CORRETORA_CORRETOR,
            origem=ORIGEM,
        )
    except Exception:  # noqa: BLE001
        log.exception("regua_proprietario_followup_falhou",
                      imovel_id=alvo.get("imovel_id"))


def tratar_resposta_inbound(
    telefone: str, texto: str, *, session: Session | None = None
) -> dict[str, Any] | None:
    """Ponto de entrada do webhook: dono clicou no botão (ou escreveu).

    Descobre QUAL imóvel a pessoa está respondendo pelo último toque da régua
    enviado pra aquele telefone. Isso importa porque dono com mais de um imóvel
    existe e é comum aqui: a Dulcineia ofereceu TRÊS casas na mesma rua, e a
    régua toca uma de cada vez, com 40 dias de intervalo — então o toque mais
    recente é sempre o que ela está respondendo.

    Sem toque na janela, devolve None e não faz nada: a pessoa pode estar
    respondendo a outra conversa qualquer, e mexer na situação do imóvel por
    palpite é como sumir com ele da carteira.

    Best-effort por contrato — é chamado de dentro do webhook, e exceção aqui
    não pode derrubar a gravação da mensagem.
    """
    veredito = classificar_resposta(texto)
    if veredito is None:
        return None

    suf = "".join(ch for ch in (telefone or "") if ch.isdigit())[-8:]
    if len(suf) < 8:
        return None

    fechar = session is None
    s = session or SessionLocal()
    try:
        alvo = s.execute(
            sql_text("""
                SELECT e.imovel_id, i.situacao, i.dono_nome, i.tipo, i.bairro, i.dono_telefone
                  FROM imovel_evento e
                  JOIN imoveis_captados i ON i.id = e.imovel_id
                 WHERE e.tipo = :evento
                   AND e.em > now() - make_interval(days => :janela)
                   AND right(regexp_replace(i.dono_telefone, '\\D', '', 'g'), 8) = :suf
                 ORDER BY e.em DESC
                 LIMIT 1
            """),
            {"evento": EVENTO_TOQUE, "janela": JANELA_RESPOSTA_DIAS, "suf": suf},
        ).mappings().first()
        if not alvo:
            return None

        aplicado = registrar_resposta(
            int(alvo["imovel_id"]), texto, por="dono_botao", session=s
        )
        log.info("regua_proprietario_inbound", imovel_id=alvo["imovel_id"],
                 de=alvo["situacao"], para=aplicado, clique=texto[:40])

        # O clique ABRIU A JANELA DE 24H. É aqui que a mensagem vira útil: o
        # template é genérico ("imóvel em Cidade"), e agora dá pra dizer QUAL
        # imóvel e por QUANTO, em texto livre e sem custo de template.
        #
        # É também onde se resolve a ambiguidade do "Encerrar": ele não diz se
        # vendeu ou desistiu, e as duas coisas valem informações diferentes pra
        # casa — vendeu calibra preço na região, desistiu volta pro mercado.
        _followup(s, dict(alvo), aplicado)
        return {
            "imovel_id": int(alvo["imovel_id"]),
            "de": alvo["situacao"],
            "para": aplicado,
            "dono": alvo["dono_nome"],
            # a corretora dona precisa saber que o imóvel saiu da carteira —
            # quem avisa é o caller, aqui só devolvemos o material
            "resumo": f"{alvo['tipo']} em {alvo['bairro']}",
        }
    except Exception:  # noqa: BLE001
        log.exception("regua_proprietario_inbound_falhou", suf=suf)
        return None
    finally:
        if fechar:
            s.close()
