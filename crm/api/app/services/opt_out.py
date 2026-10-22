"""Detecção de OPT-OUT ("pare de me mandar mensagem") — implementação canônica.

Existiam DUAS implementações no código, com comportamentos opostos:

* `app/api/cloud_corretora_a._is_opt_out` — regex com limite de palavra (boa).
* `app/services/cadencia.is_opt_out` — `"para" in texto` (substring solta).

A segunda causou dano real em produção (auditoria 26/07/2026): os 3 únicos
opt-outs do banco eram FALSOS POSITIVOS — a preposição "para" no meio de frases
inocentes ("Estamos ansiosos **para** atendê-lo", "convidamos você **para** a
inauguração") marcava o lead como descadastrado E o movia pra Venda perdida.

Regra desta implementação — opt-out só com intenção CLARA:

1. **Frase inequívoca em qualquer lugar do texto** ("não quero receber",
   "pare de mandar", "descadastrar", "sair da lista"…). Frase, não palavra
   solta: quem escreve isso está pedindo pra sair, não importa o tamanho da msg.
2. **Palavra-comando em mensagem CURTA** (até 4 palavras) — cobre a resposta
   seca ("parar", "sair", "stop") e o Quick Reply dos templates
   ("Parar mensagens"), sem pegar a mesma palavra dentro de uma conversa real.

O que esta regra NÃO marca (de propósito, casos reais de imobiliária):
  - "quero um apê **para** minha mãe"          → "para" nunca é opt-out
  - "preciso **cancelar** a visita de amanhã"  → cancelar visita ≠ descadastro
  - "não quero esse bairro"                    → recusa de imóvel ≠ descadastro
Falso positivo aqui custa um lead morto no funil; falso negativo custa uma
mensagem a mais. Erramos pro lado barato.
"""
from __future__ import annotations

import re
import unicodedata

# 1) Frases inequívocas — valem em qualquer posição, em qualquer tamanho de msg.
#
# `par[ae]r?` cobre para/pare/PARAR. O "r" faltava e custou caro (20/08/2026):
# a Flavia escreveu "gostaria de parar de receber mensagens, por favor. assim
# como já tinha deixado claro anteriormente" — pediu pela SEGUNDA vez, e as
# duas passaram batido. A Pessoa respondeu "não vamos mais entrar em contato",
# mas o opt-out nunca foi gravado, então ela seguia elegível pro próximo
# disparo. Pessoa que pede duas vezes e recebe de novo não reclama: denuncia,
# e quem paga é a qualidade do 0000.
#
# ATENÇÃO ao "de" obrigatório depois de para/pare/parar: sem ele, "estou com um
# cliente aqui PARA MANDAR contrato" vira opt-out — falso positivo achado na
# varredura de 20/08 e que já existia antes desta correção. "nao quero/desejo/
# me" seguem sem exigir o "de" ("não quero receber" é inequívoco).
_FRASES = re.compile(
    r"(?:par[ae]r?|parem)\s+de\s+"
    r"(?:mandar|manda|enviar|envia|receber|recebe|mensage|msg|whats|contat)"
    # `mande|mandar` com \b no fim: "não me MANDARAM proposta ainda" é relato
    # do que a OUTRA imobiliária fez, não pedido de descadastro (achado na
    # varredura de 20/08).
    r"|(?:nao\s+quero|nao\s+desejo|nao\s+me)\s+(?:de\s+)?"
    r"(?:mandar|manda|mande|enviar|envia|envie|receber|recebe|mensage|msg|whats|contat)\b"
    r"|nao\s+quero\s+mais\s+(?:nada|receber|mensage)"
    # "parar" INTEIRO aqui: com par[ae]r? o "peço PARA o RH amanhã" virava
    # opt-out — a preposição de novo, pelo outro lado da frase.
    r"|(?:gostaria|quero|peco|solicito|favor)\s+(?:de\s+)?parar\b"
    r"|nao\s+(?:envie|mande|mandem|enviem)\s+mais"
    r"|descadastr"
    r"|(?:remov\w*|excluir|tirar|sair)\s+(?:me\s+|meu\s+|minha\s+|da\s+|desta\s+|dessa\s+)"
    r"(?:numero|contato|lista|cadastro)"
    r"|me\s+(?:exclua|remova|retire|tire)\s+(?:da|do|desta|dessa|de\s+sua)"
    r"|(?:sair|me\s+tira[r]?)\s+da\s+lista"
    r"|nao\s+perturbe"
    r"|para\s+de\s+(?:mandar|enviar|encher)",
    re.IGNORECASE,
)

# 2) Palavras-comando — SÓ valem se a mensagem inteira for curta (resposta seca
#    ou botão de template). "para" NÃO entra aqui: é preposição, nunca comando.
_COMANDOS = {
    "parar", "pare", "parem", "stop", "descadastrar", "remover",
    "desinscrever", "unsubscribe",
}
# Palavras que TAMBÉM são conversa normal de imobiliária: "vai sair" (a obra),
# "cancela amanhã" (a visita), "preciso cancelar a visita". Como comando solto
# elas valem; acompanhadas, não. A doc deste módulo sempre disse que cancelar
# visita não é descadastro — mas "cancelar" estava na lista de cima, então
# "preciso cancelar a visita" marcava opt-out. Bug antigo, achado em 20/08.
_COMANDOS_AMBIGUOS = {"sair", "cancelar", "cancela"}
# Recusa global dita em mensagem CURTA ("não quero mais", "não quero nada"):
# pensamento completo = pare. Em mensagem longa é preferência de imóvel
# ("não quero mais caro que 300 mil"), por isso a trava de tamanho.
_RECUSA_CURTA = re.compile(r"nao\s+quero\s+(?:mais|nada)")
_MAX_PALAVRAS_CURTA = 4


# Exemplos legíveis pra UI (tela de Automações). NÃO é a regra — a regra é o
# _FRASES + _COMANDOS acima; isto é só o que a gente mostra pra Dona/corretora.
EXEMPLOS_HUMANOS: tuple[str, ...] = (
    "parar", "pare", "sair", "stop", "descadastrar",
    "não quero receber", "pare de mandar", "me tira da lista",
)


def _normalizar(texto: str) -> str:
    """minúsculas, sem acento, pontuação → espaço (compara igual a 'Parar!' e 'PARAR')."""
    t = unicodedata.normalize("NFD", texto.strip().lower())
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


# 3) Desinteresse claro (obra 10, 22/10/2026) — a lista DESINTERESSE do DEX
#    (`crm-dex/lib/fup.ts`), portada SEM o que é só do DEX e SEM o que pegava
#    conversa de imobiliária. A diferença de método importa:
#
#    O DEX procura a frase em qualquer lugar do texto. Aqui a frase tem que ser
#    a mensagem INTEIRA (descontadas saudação e agradecimento). Nas mensagens
#    reais da Acme, "não tenho interesse" aparece também em "essa região não
#    tenho interesse", "não estou procurando, estou vendendo um terreno" e
#    "no momento não tenho interesse" — nenhuma é pedido de saída. Mensagem
#    inteira = pensamento completo; o resto o atendente decide.
#
#    Ficaram DE FORA de propósito:
#      - "não quero (mais)?" solto do DEX: pega "não quero esse bairro";
#      - "já comprei/consegui/resolvi/fechei": tem fluxo próprio
#        (`cadencia._despedir_quem_ja_encontrou`: despedida + pedido de
#        indicação) e "já comprei, mas..." às vezes é permuta;
#      - "sair/remover/parar de mandar": já cobertos por `_FRASES`/`_COMANDOS`;
#      - qualquer recusa com tempo ("agora não", "no momento", "por enquanto",
#        "depois eu vejo", "não posso falar agora", "não é esse"): adia, não sai.
_CORTESIA = re.compile(
    r"\b(?:ola|oi|opa|bom dia|boa tarde|boa noite|tudo bem|tudo bom|por favor|"
    r"obrigad[oa]s?|obg|obgd|valeu|agradec\w*|pela atencao|pelo contato|"
    r"pelo retorno|mas|so que|ok|desculpa|desculpe|infelizmente)\b"
)
_DESINTERESSE = re.compile(
    r"(?:nao\s+(?:tenho|temos)\s+(?:mais\s+)?interesse"
    r"(?:\s+(?:no|em|nesse|neste)\s+(?:imovel|assunto|apartamento|empreendimento|nada))?"
    r"|sem\s+(?:mais\s+)?interesse"
    r"|nao\s+(?:estou|to|estamos|tamos)\s+(?:mais\s+)?(?:procurando|interessad[oa]s?)"
    r"(?:\s+(?:nada|imovel))?"
    r"|(?:pode\s+)?encerrar(?:\s+(?:o\s+)?atendimento)?"
    r"|nao\s+quero\s+mais)"
)


def classificar_desinteresse(texto: str | None) -> str | None:
    """Devolve a frase (normalizada) se a mensagem INTEIRA é desinteresse claro.

    É o que vai pro log/evento de auditoria. None = não é.
    """
    if not texto:
        return None
    t = _CORTESIA.sub(" ", _normalizar(texto))
    t = re.sub(r"\s+", " ", t).strip()
    if t and len(t.split()) <= 6 and _DESINTERESSE.fullmatch(t):
        return t
    return None


def motivo_opt_out(texto: str | None) -> str | None:
    """Por que a RÉGUA deve tirar o lead: 'pedido' | 'desinteresse' | None.

    'pedido' é exatamente o que `is_opt_out` diz. 'desinteresse' é a leitura da
    obra 10 e só vale na resposta inbound da cadência (`registrar_resposta`):
    os outros fluxos (remarketing, Corretor, saída) tratam "não tenho interesse"
    como recusa BRANDA de propósito — card fica parado pra corretora decidir,
    sem trava de reenvio — e têm testes que travam isso. Por isso `is_opt_out`
    NÃO muda.

    Pra auditoria: o evento mostra se foi pedido de parar ou desinteresse e, no
    segundo caso, a frase exata (`classificar_desinteresse`).
    """
    if not texto:
        return None
    t = _normalizar(texto)
    if not t:
        return None
    if _is_pedido(t):
        return "pedido"
    return "desinteresse" if classificar_desinteresse(texto) else None


def is_opt_out(texto: str | None) -> bool:
    """True só quando o lead pede CLARAMENTE pra parar de receber mensagem."""
    if not texto:
        return False
    t = _normalizar(texto)
    return bool(t) and _is_pedido(t)


def _is_pedido(t: str) -> bool:
    """Regras 1 e 2 (frase inequívoca / comando curto) sobre texto normalizado."""
    if _FRASES.search(t):
        return True
    palavras = t.split()
    # Comando ambíguo só vale SOZINHO ("sair", "cancelar") — acompanhado é
    # conversa ("vai sair", "cancela amanhã").
    if len(palavras) == 1 and palavras[0] in _COMANDOS_AMBIGUOS:
        return True
    if len(palavras) <= _MAX_PALAVRAS_CURTA:
        if any(p in _COMANDOS for p in palavras):
            return True
        return bool(_RECUSA_CURTA.search(t))
    return False


# ── Parte 2: quem NÃO PODE receber ───────────────────────────────────────────
#
# Detectar era só metade. Em 16/09/2026 uma varredura achou 33 telefones que
# pediram pra parar, e SETE deles receberam mensagem depois — quatro pelo robô,
# com `[reengajamento:]`. O motivo não foi a detecção falhar: foi o pedido nunca
# ter sido GRAVADO. `is_opt_out` só era chamado nos caminhos de inbound de
# alguns motores; quem clicou "Parar mensagens" numa conversa que não passou por
# esses caminhos ficou sem registro, e a fila do disparo não tinha o que ver.
#
# Pior: `nutricao`, `pessoa_retomada` e `lead_intake` não consultavam opt-out de
# forma nenhuma, e NENHUM motor conhecia a etapa "Não perturbe" — mover o card
# pra lá era proteção zero.
#
# Daqui pra frente há UMA fonte de verdade, e ela mora aqui.

ETAPA_NAO_PERTURBE = 26

# Predicado SQL canônico. Vale por SUFIXO-8 do telefone (a mesma chave que a
# cadência e o carrossel já usavam) porque o mesmo humano aparece na base com
# +55, sem 55, com e sem o 9. Use com `AND NOT (%s)` % SQL_BLOQUEADO, passando
# a coluna de telefone do seu SELECT em `:tel_col` — ou chame `clausula()`.
_SQL_BLOQUEADO = r"""
    EXISTS (
        SELECT 1 FROM disparos_reengajamento d
         WHERE right(regexp_replace(d.telefone, '\D', '', 'g'), 8)
             = right(regexp_replace({tel}, '\D', '', 'g'), 8)
           AND d.opt_out IS TRUE
    )
    OR EXISTS (
        SELECT 1 FROM cadencia_estado ce
         WHERE ce.status = 'opt_out'
           AND right(regexp_replace(ce.telefone, '\D', '', 'g'), 8)
             = right(regexp_replace({tel}, '\D', '', 'g'), 8)
    )
    OR EXISTS (
        SELECT 1 FROM leads lx
         WHERE lx.stage_id = %d
           AND right(regexp_replace(lx.telefone, '\D', '', 'g'), 8)
             = right(regexp_replace({tel}, '\D', '', 'g'), 8)
    )
""" % ETAPA_NAO_PERTURBE


def clausula(coluna_telefone: str) -> str:
    """Devolve o predicado SQL "este telefone pediu pra parar".

    Uso em qualquer fila de envio:

        WHERE ... AND NOT (%s)  % opt_out.clausula("l.telefone")

    Cobre as três fontes: o disparo legado, a cadência e a etapa Não perturbe.
    """
    return _SQL_BLOQUEADO.format(tel=coluna_telefone)


def esta_bloqueado(session, telefone: str | None) -> bool:
    """Checagem avulsa, pra motor que monta a lista em Python."""
    if not telefone:
        return False
    from sqlalchemy import text as _sql

    r = session.execute(
        _sql("SELECT (%s) AS b" % clausula(":tel")), {"tel": telefone}
    ).scalar()
    return bool(r)


# ── Parte 3: POR QUAL CAMINHO a pessoa pediu (07/10/2026) ───────────────────
#
# `clausula`/`esta_bloqueado` dizem SIM ou NÃO. Quem tira alguém da régua
# precisa dizer também DE ONDE veio o pedido, senão ninguém confere depois.
# Caminhos, na ordem em que aparecem no evento:
CAMINHO_NAO_PERTURBE = "nao_perturbe"          # card na etapa 26
CAMINHO_REMARKETING_NOVO = "remarketing_novo"  # "parar" no carrossel (enviado_por='carrossel')
CAMINHO_DISPARO_ANTIGO = "disparo_antigo"      # disparos_reengajamento do painel/Corretor
CAMINHO_CADENCIA = "cadencia_opt_out"          # outro cadencia_estado do mesmo número
CAMINHO_API_DISPAROS = "api_disparos"          # contacts.opted_out lá (opt_out_api_disparos)

CAMINHOS_EM_PORTUGUES: dict[str, str] = {
    CAMINHO_NAO_PERTURBE: "card na etapa Não perturbe",
    CAMINHO_REMARKETING_NOVO: "pediu pra parar no remarketing novo",
    CAMINHO_DISPARO_ANTIGO: "pediu pra parar num disparo antigo",
    CAMINHO_CADENCIA: "pediu pra parar numa cadência do mesmo número",
    CAMINHO_API_DISPAROS: "pediu pra parar numa linha do api-disparos",
}


def mapa_de_bloqueio(session, *, api_disparos: bool = True) -> dict[str, list[str]]:
    """`{sufixo-8: [caminhos]}` de TODO telefone que pediu pra parar.

    Mesmas fontes de `clausula` (e mais o api-disparos), lidas de uma vez: o
    motor que varre a régua inteira faz UMA leitura por volta, não uma por
    lead. O sufixo é calculado aqui, não no SQL — as três tabelas somam
    dezenas de linhas, e assim a leitura não depende de `regexp_replace`.

    LEVANTA em qualquer falha (banco, tabela, api-disparos). Quem chama decide;
    a nutrição decide SEGURAR o marco (fail-closed).
    """
    from sqlalchemy import text as _sql

    out: dict[str, list[str]] = {}

    def _anota(telefone: str | None, caminho: str) -> None:
        suf = "".join(ch for ch in (telefone or "") if ch.isdigit())[-8:]
        if len(suf) == 8 and caminho not in out.setdefault(suf, []):
            out[suf].append(caminho)

    for (tel,) in session.execute(
        _sql("SELECT telefone FROM leads WHERE stage_id = :st"),
        {"st": ETAPA_NAO_PERTURBE},
    ):
        _anota(tel, CAMINHO_NAO_PERTURBE)
    for tel, por in session.execute(
        _sql("SELECT telefone, enviado_por FROM disparos_reengajamento"
             " WHERE opt_out = :sim"),
        {"sim": True},
    ):
        _anota(tel, CAMINHO_REMARKETING_NOVO if (por or "") == "carrossel"
               else CAMINHO_DISPARO_ANTIGO)
    for (tel,) in session.execute(
        _sql("SELECT telefone FROM cadencia_estado WHERE status = 'opt_out'"),
    ):
        _anota(tel, CAMINHO_CADENCIA)
    if api_disparos:
        from app.services import opt_out_api_disparos

        for suf in opt_out_api_disparos.sufixos_bloqueados():
            _anota(suf, CAMINHO_API_DISPAROS)
    return out


# ── Parte 4: o que o MOTOR faz com o mapa (07/10/2026) ──────────────────────
#
# A nutrição ganhou isto de manhã; à tarde a régua de 8 toques (cadencia.tick)
# passou a usar as MESMAS duas funções. Uma regra só pros dois motores: quem
# está no mapa sai da régua com o caminho gravado, e mapa ilegível segura o
# envio (fail-closed). A lead 7880 levou 5 follow-ups depois de pedir pra
# parar, 3 deles com o card em "Não perturbe", porque o follow-up só olhava o
# próprio `status`.
STATUS_OPT_OUT = "opt_out"
TIPO_OPT_OUT = "opt_out"                       # cadencia_eventos.tipo
BLOQUEIO_OPT_OUT_INDISPONIVEL = "opt_out_indisponivel"
CAMINHO_EMISSOR = "emissor"                    # a trava do emissor barrou na hora de sair
_TEXTO_EMISSOR = "a trava do emissor barrou na hora de sair"
# Tipo do evento no CARD (lead_events), por motor.
TIPO_LEAD_EVENT = {"nutricao": "nutricao_opt_out", "cadencia": "follow_up_opt_out"}


def ler_mapa(session, *, motor: str) -> dict[str, list[str]] | None:
    """`mapa_de_bloqueio` em SAVEPOINT. Falhou = None: quem chama não manda
    nada nesta volta (fail-closed). Erro de banco aqui não envenena a sessão
    do resto da volta."""
    import structlog

    try:
        with session.begin_nested():
            return mapa_de_bloqueio(session)
    except Exception as e:  # noqa: BLE001
        structlog.get_logger(__name__).warning(
            f"{motor}_opt_out_indisponivel", erro=f"{type(e).__name__}: {str(e)[:160]}")
        return None


def tirar_da_regua(session, est, agora, caminhos: list[str], *, motor: str,
                   res, lead_id: int | None = None) -> None:
    """Tira o estado da régua: `status='opt_out'` + `cadencia_eventos(tipo=
    'opt_out')` com o caminho do pedido + evento no card, quando há card.

    NÃO move o card — etapa é decisão de gente (o caso que abriu isto foi um
    card arrastado pra fora de "Não perturbe"). NÃO faz commit: quem chama
    commita. `res` é o resultado do tick do motor (conta `opt_out`,
    `opt_out_por_caminho` e anota em `detalhes`)."""
    import structlog

    from app.services.cadencia import CANAL_WHATSAPP, _registrar_evento

    status_antes = est.status
    est.status = STATUS_OPT_OUT
    est.atualizado_em = agora
    payload = {
        motor: True,
        "motor": motor,
        "caminho": caminhos[0],
        "caminhos": list(caminhos),
        "caminho_texto": CAMINHOS_EM_PORTUGUES.get(caminhos[0], _TEXTO_EMISSOR),
        "status_antes": status_antes,
        "corretora_id": est.corretora_id,
    }
    _registrar_evento(session, est, toque=None, tipo=TIPO_OPT_OUT,
                      canal=CANAL_WHATSAPP, payload=payload, agora=agora)
    log = structlog.get_logger(__name__)
    if lead_id is not None:
        # best-effort em SAVEPOINT: espinha parcial em teste não derruba o tick
        try:
            from app.services.lead_spine import _registrar_evento as _lead_event

            with session.begin_nested():
                _lead_event(session, est.tenant_id, lead_id,
                            TIPO_LEAD_EVENT.get(motor, f"{motor}_opt_out"),
                            {"cadencia_id": est.id, **payload})
        except Exception as e:  # noqa: BLE001
            log.debug(f"{motor}_lead_event_falhou", erro=str(e)[:120])
    res.opt_out += 1
    for c in caminhos:
        res.opt_out_por_caminho[c] = res.opt_out_por_caminho.get(c, 0) + 1
    res.detalhes.append({"telefone": (est.telefone or "")[-4:], "tipo": "opt_out",
                         "caminhos": list(caminhos)})
    log.warning(f"{motor}_opt_out_marcado", cadencia_id=est.id, caminhos=caminhos,
                telefone_last4=(est.telefone or "")[-4:])


def registrar(session, telefone: str | None, tenant_id: int = 1,
              motivo: str = "pedido do lead") -> bool:
    """Grava o opt-out nas fontes que as filas consultam. Idempotente.

    Grava em `cadencia_estado` (a fonte viva) e, se já existir registro de
    disparo pro número, marca `disparos_reengajamento.opt_out` também. Sem
    isso o lead segue elegível: foi exatamente o que aconteceu com 9 dos 25
    movidos em 16/09 — estavam na etapa certa e continuavam na fila.

    NÃO move etapa: quem move é quem chamou, porque o destino depende do
    contexto (a Corretor manda pra fora da roda, a cadência marca o card).
    """
    import structlog
    from sqlalchemy import text as _sql

    log = structlog.get_logger(__name__)
    suf = "".join(ch for ch in (telefone or "") if ch.isdigit())[-8:]
    if len(suf) < 8:
        log.warning("opt_out_telefone_curto", telefone=telefone)
        return False

    try:
        session.execute(
            _sql("""
                UPDATE disparos_reengajamento
                   SET opt_out = TRUE, opt_out_em = coalesce(opt_out_em, now())
                 WHERE right(regexp_replace(telefone, '\\D', '', 'g'), 8) = :suf
                   AND opt_out IS NOT TRUE
            """), {"suf": suf},
        )
        # `cadencia_estado` é a fonte que TODA fila consulta. Se não existir
        # linha pro telefone, cria — senão o filtro não tem o que ver.
        atualizou = session.execute(
            _sql("""
                UPDATE cadencia_estado
                   SET status = 'opt_out'
                 WHERE right(regexp_replace(telefone, '\\D', '', 'g'), 8) = :suf
                   AND status IS DISTINCT FROM 'opt_out'
            """), {"suf": suf},
        ).rowcount
        existe = session.execute(
            _sql("""SELECT 1 FROM cadencia_estado
                     WHERE right(regexp_replace(telefone,'\\D','','g'),8) = :suf
                     LIMIT 1"""), {"suf": suf},
        ).scalar()
        if not existe:
            session.execute(
                _sql("""
                    INSERT INTO cadencia_estado (tenant_id, telefone, status, atualizado_em)
                    VALUES (:t, :tel, 'opt_out', now())
                """), {"t": tenant_id, "tel": telefone},
            )
        log.info("opt_out_registrado", suf=suf, motivo=motivo,
                 cadencia_atualizada=atualizou, criou=not existe)
        return True
    except Exception:  # noqa: BLE001
        # Falha fechada no que dói: sem registro, a pessoa recebe de novo.
        log.exception("opt_out_registrar_falhou", suf=suf)
        return False
