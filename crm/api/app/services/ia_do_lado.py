"""IA do lado, nunca no caminho — resumo no card e opinião no que a regra não entendeu.

Decisão da Dona (05/10/2026, aba orquestradora): *"joia, faz isso funcionar
certinho, roda teste e vamos publicar"*. Em vez de a IA qualificar o lead com 4
ou 5 perguntas antes de passar (a casa já rodou isso: em 05/09, dos 40 que
responderam, zero chegaram numa corretora), a divisão é:

    mover o card                         código, na hora, como já está
    resumo da conversa no card           IA, depois da transferência, em segundo plano
    resposta que a regra chamou de       IA opina: interesse, recusa ou não sei,
      `indefinida`                         com o trecho que decidiu
    mensagem pro lead                    a copy fixa da Dona; a IA não escreve
    perguntar e qualificar               a corretora

AS TRAVAS (o que este módulo garante, e os testes provam):

1. **A IA nunca fica no caminho.** Fora do ar, lenta, JSON inválido, "não sei"
   ou confiança abaixo do corte = vale a regra de hoje, e isso fica no evento.
   O resumo roda numa thread DEPOIS do commit da transferência: o card chega
   primeiro, o resumo aparece quando ficar pronto, e se não ficar o card diz
   "sem resumo".
2. **A opinião só mexe no que a regra não decidiu.** Só é chamada quando a
   regra disse `indefinida` com texto (mídia não: não há o que ler) e o card
   ainda está na roda, antes de cair em Respondeu (Fila ou Disparado, com o
   dono do disparo). "recusa" com confiança ≥ corte → Venda perdida, pelo MESMO
   caminho da recusa da regra. "interesse" → conta na taxa (o card já ia pra
   roleta de qualquer jeito: todo mundo vai). Nunca desfaz recusa, parar ou
   automática; nunca grava opt-out; nunca devolve card de Venda perdida.
3. **Saída estruturada e conferida.** Opinião `{classe, confianca, trecho}`,
   resumo `{resumo, fatos[]}`; o `trecho` tem que estar na fala do lead e cada
   fato aponta a fala de onde saiu (`L1`, `L2`…) com um pedaço literal dela.
   Fato sem fala de origem é descartado. Número no resumo que a pessoa não
   escreveu derruba o resumo (fica só a lista de fatos).
4. **Privacidade.** Vai pro modelo só a conversa daquele lead na linha da Corretor
   (o disparo e as falas). Nada de outro lead, nada de credencial. O log leva o
   id e o resultado, nunca o texto.
5. **Mesmo provedor da Pessoa** (`app.config.get_llm_provider`: Claude CLI no
   plano Max, com a reserva que ela já tem). Sem API paga nova.

Chaves (tela "Quando a pessoa responde"), nenhuma estrita — leitura que falha
vale "desligado", que é o comportamento de antes:

    ia_resumo_card        true | false   padrão true
    ia_opina_indefinida   true | false   padrão true
    ia_opina_corte        0.5 a 1.0      padrão CORTE_PADRAO (medido nas 87)
"""
from __future__ import annotations

import json
import re
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import structlog
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

log = structlog.get_logger()

CHAVE_RESUMO = "ia_resumo_card"
CHAVE_OPINA = "ia_opina_indefinida"
CHAVE_CORTE = "ia_opina_corte"
CHAVES = (CHAVE_RESUMO, CHAVE_OPINA, CHAVE_CORTE)

# Medido nas 87 indefinidas reais (projetos/crm/docs/2026-10-05-ia-do-lado/):
# é o menor corte em que nenhuma "recusa" da IA contrariou a leitura de uma
# pessoa. Ver `IA-DO-LADO.md`, seção 2.
CORTE_PADRAO = 0.9
CORTE_MIN = 0.5
CORTE_MAX = 1.0

# Quanto a opinião pode segurar o turno. O api-disparos espera o copiloto 8 s
# no repasse (`projetos/api/app/api/webhooks.py:_forward`); a regra, a
# transferência e o aviso ainda precisam caber depois (~1–2 s). Medido nas 66
# chamadas reais: p50 5,8 s, p90 7,5 s — 6 s pega pouco mais da metade. A que
# chega depois não mexe em nada: só fica anotada no evento (`tardia`).
PRAZO_OPINIAO_S = 6.0
# O resumo não segura nada; o prazo só existe pra não deixar "saindo" pra sempre.
PRAZO_RESUMO_S = 120.0
# Depois disto um resumo "pendente" é mostrado como "sem resumo".
RESUMO_PENDENTE_VENCE_S = 180

EVENTO_RESUMO = "resumo_ia"
REGRA_IA = "ia_opiniao"

INTERESSE = "interesse"
RECUSA = "recusa"
NAO_SEI = "nao_sei"
CLASSES_IA = (INTERESSE, RECUSA, NAO_SEI)

# O que aconteceu com a chamada — vai no evento, sempre.
OK = "ok"
FORA_DO_AR = "fora_do_ar"
ATRASOU = "atrasou"
INVALIDA = "invalida"

MAX_FALAS = 30
MAX_CHARS_FALA = 600
RESUMO_MAX_CHARS = 160
FATO_MAX_CHARS = 90
MAX_FATOS = 4

# Trocado nos testes pelo modelo dublado. Em produção é o funil da Pessoa.
_provider_factory: Callable[[], Any] | None = None


def _provider() -> Any:
    if _provider_factory is not None:
        return _provider_factory()
    from app.config import get_llm_provider

    return get_llm_provider()


# ── Chaves ───────────────────────────────────────────────────────────────────


def _cfg(chave: str) -> Any:
    from app.services import config_operacional

    return config_operacional.get(chave)


def resumo_ligado() -> bool:
    """Só `true` liga. Banco fora ou valor torto = desligado (não chama a IA)."""
    try:
        return _cfg(CHAVE_RESUMO) is True
    except Exception as e:  # noqa: BLE001
        log.warning("ia_do_lado_chave_ilegivel", chave=CHAVE_RESUMO, erro=str(e)[:100])
        return False


def opiniao_ligada() -> bool:
    try:
        return _cfg(CHAVE_OPINA) is True
    except Exception as e:  # noqa: BLE001
        log.warning("ia_do_lado_chave_ilegivel", chave=CHAVE_OPINA, erro=str(e)[:100])
        return False


def validar_corte(valor: Any) -> float:
    if isinstance(valor, bool) or not isinstance(valor, (int, float)):
        raise ValueError("o corte de confiança precisa ser um número entre 0,5 e 1")
    v = float(valor)
    if not CORTE_MIN <= v <= CORTE_MAX:
        raise ValueError("o corte de confiança fica entre 0,5 e 1")
    return v


def corte() -> float | None:
    """O corte gravado, ou None se ilegível (aí a opinião não pesa)."""
    try:
        return validar_corte(_cfg(CHAVE_CORTE))
    except Exception as e:  # noqa: BLE001
        log.warning("ia_do_lado_corte_ilegivel", erro=str(e)[:100])
        return None


# ── Chamada com prazo ────────────────────────────────────────────────────────


def _ler(caixa: dict[str, Any], ms: int) -> tuple[str, Any, int]:
    if "erro" in caixa:
        return FORA_DO_AR, caixa["erro"], ms
    try:
        dado = json.loads(caixa.get("bruto") or "")
    except (TypeError, ValueError):
        return INVALIDA, None, ms
    if not isinstance(dado, dict):
        return INVALIDA, None, ms
    return OK, dado, ms


def _chamar(system: str, user: str, schema: dict[str, Any], prazo_s: float,
            ao_chegar_tarde: Callable[[str, Any, int], None] | None = None,
            ) -> tuple[str, Any, int]:
    """(resultado, json_ou_None, ms). Roda o provider numa thread e espera no
    máximo `prazo_s`. Passou do prazo, quem chamou segue pela regra e a thread
    segue sozinha; se `ao_chegar_tarde` vier, ela é chamada (na thread) com o
    que chegou — pra anotar, nunca pra mexer no card."""
    caixa: dict[str, Any] = {}
    trava = threading.Lock()
    t0 = time.monotonic()

    def _rodar() -> None:
        try:
            bruto, _uso = _provider().complete(system, user, json_schema=schema)
            caixa["bruto"] = bruto
        except Exception as e:  # noqa: BLE001
            caixa["erro"] = f"{type(e).__name__}: {str(e)[:120]}"
        with trava:
            caixa["pronto"] = True
            tarde = caixa.get("abandonado", False)
        if tarde and ao_chegar_tarde is not None:
            try:
                ao_chegar_tarde(*_ler(caixa, int((time.monotonic() - t0) * 1000)))
            except Exception as e:  # noqa: BLE001
                log.warning("ia_do_lado_tardia_falhou", erro=str(e)[:120])

    th = threading.Thread(target=_rodar, name="ia-do-lado", daemon=True)
    th.start()
    th.join(prazo_s)
    ms = int((time.monotonic() - t0) * 1000)
    with trava:
        if not caixa.get("pronto"):
            caixa["abandonado"] = True
            return ATRASOU, None, ms
    return _ler(caixa, ms)


# ── Texto ────────────────────────────────────────────────────────────────────


def _norm(t: str | None) -> str:
    t = unicodedata.normalize("NFKD", (t or "").lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _contido(trecho: str | None, fala: str | None) -> bool:
    a, b = _norm(trecho), _norm(fala)
    return bool(a) and a in b


@dataclass(frozen=True)
class Fala:
    id: str          # "L1", "L2"… pro lead; "C1"… pra casa (disparo, Pessoa, celular)
    quem: str        # "lead" | "casa"
    texto: str


def _formatar(falas: list[Fala]) -> str:
    linhas = []
    for f in falas:
        rotulo = "Lead" if f.quem == "lead" else "Acme"
        linhas.append(f"[{f.id}] {rotulo}: {f.texto[:MAX_CHARS_FALA]}")
    return "\n".join(linhas)


# ── Opinião ──────────────────────────────────────────────────────────────────

OPINIAO_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["classe", "confianca", "trecho"],
    "properties": {
        "classe": {"type": "string", "enum": list(CLASSES_IA)},
        "confianca": {"type": "number", "minimum": 0, "maximum": 1},
        "trecho": {"type": "string", "maxLength": 120,
                   "description": "pedaço LITERAL da fala do lead que decidiu"},
    },
}

OPINIAO_SYSTEM = (
    "Você lê a resposta de uma pessoa a uma mensagem de remarketing da Acme "
    "Imóveis (Cidade), que perguntou se ela ainda procura imóvel. Diga só o "
    "que a pessoa quis dizer:\n"
    "- interesse: quer ver opções, quer conversar, diz que ainda procura;\n"
    "- recusa: não procura mais, já comprou, não é o momento, não quer;\n"
    "- nao_sei: cumprimento, pergunta solta, agradecimento, figurinha, número "
    "de outra pessoa, ou qualquer coisa em que você precisaria adivinhar.\n"
    "Cumprimento sozinho ('bom dia', 'olá', 'oi tudo bem') é nao_sei. "
    "'trecho' é copiado LITERALMENTE da fala do lead. 'confianca' de 0 a 1. "
    "Você não escreve para o lead."
)


@dataclass
class Opiniao:
    resultado: str                 # ok | fora_do_ar | atrasou | invalida
    classe: str | None = None      # interesse | recusa | nao_sei
    confianca: float | None = None
    trecho: str | None = None
    ms: int = 0
    erro: str | None = None


def validar_opiniao(dado: dict[str, Any], falas_lead: list[str]) -> Opiniao:
    """Levanta ValueError se não serve. O trecho tem que estar numa fala do lead."""
    classe = dado.get("classe")
    if classe not in CLASSES_IA:
        raise ValueError("classe fora do esquema")
    conf = dado.get("confianca")
    if isinstance(conf, bool) or not isinstance(conf, (int, float)) or not 0 <= conf <= 1:
        raise ValueError("confianca fora do esquema")
    trecho = dado.get("trecho")
    if not isinstance(trecho, str):
        raise ValueError("trecho fora do esquema")
    trecho = trecho.strip()[:120]
    if classe != NAO_SEI and not any(_contido(trecho, f) for f in falas_lead):
        raise ValueError("trecho não está na fala do lead")
    return Opiniao(resultado=OK, classe=classe, confianca=float(conf), trecho=trecho)


def _opiniao_de(resultado: str, dado: Any, ms: int, falas_lead: list[str]) -> Opiniao:
    if resultado == FORA_DO_AR:
        return Opiniao(resultado=FORA_DO_AR, ms=ms, erro=str(dado)[:120])
    if resultado != OK:
        return Opiniao(resultado=resultado, ms=ms)
    try:
        op = validar_opiniao(dado, falas_lead)
    except ValueError as e:
        return Opiniao(resultado=INVALIDA, ms=ms, erro=str(e)[:120])
    op.ms = ms
    return op


def opinar(falas: list[Fala], *, prazo_s: float | None = None,
           ao_chegar_tarde: Callable[[Opiniao], None] | None = None) -> Opiniao:
    """Pergunta ao modelo. Nunca levanta. `ao_chegar_tarde` recebe a opinião
    que passou do prazo (já conferida), pra ser anotada."""
    prazo_s = PRAZO_OPINIAO_S if prazo_s is None else prazo_s
    falas_lead = [f.texto for f in falas if f.quem == "lead"]
    tarde = None
    if ao_chegar_tarde is not None:
        def tarde(resultado: str, dado: Any, ms: int) -> None:
            ao_chegar_tarde(_opiniao_de(resultado, dado, ms, falas_lead))
    try:
        resultado, dado, ms = _chamar(OPINIAO_SYSTEM, _formatar(falas),
                                      OPINIAO_SCHEMA, prazo_s, tarde)
    except Exception as e:  # noqa: BLE001
        return Opiniao(resultado=FORA_DO_AR, erro=str(e)[:120])
    return _opiniao_de(resultado, dado, ms, falas_lead)


def decidir(op: Opiniao, corte_: float | None) -> str | None:
    """A classe que a opinião põe no lugar de `indefinida`, ou None (vale a
    regra). Só `recusa` precisa passar do corte: ela mexe no card. `interesse`
    só conta na taxa, e também respeita o corte pra a taxa não inflar."""
    if op.resultado != OK or corte_ is None or op.confianca is None:
        return None
    if op.classe in (RECUSA, INTERESSE) and op.confianca >= corte_:
        return op.classe
    return None


def registro(op: Opiniao | None, *, classe_regra: str, pesou: str | None,
             corte_: float | None, chamada: bool = True,
             motivo: str | None = None) -> dict[str, Any]:
    """O que vai no evento `carrossel_resposta` (chave `ia`)."""
    r: dict[str, Any] = {"chamada": chamada, "classe_regra": classe_regra,
                         "pesou": pesou is not None, "classe_final": pesou or classe_regra,
                         "corte": corte_}
    if motivo:
        r["motivo"] = motivo
    if op is not None:
        r.update(resultado=op.resultado, classe_ia=op.classe,
                 confianca=op.confianca, trecho=op.trecho, ms=op.ms)
    return r


# ── Conversa da linha da Corretor ─────────────────────────────────────────────────

_SQL_CONVERSA = r"""
    SELECT from_corretora, tipo_mensagem, conteudo_texto, transcricao,
           timestamp_evolution
      FROM mensagens_whatsapp
     WHERE instance = :inst AND fone8 = :suf AND tenant_id = :t
       AND (CAST(:desde AS timestamptz) IS NULL OR timestamp_evolution >= :desde)
       AND (CAST(:ate AS timestamptz) IS NULL OR timestamp_evolution <= :ate)
     ORDER BY timestamp_evolution DESC
     LIMIT :lim
"""

INSTANCIA_CORRETOR = "acme_corretor"
_FOLGA_S = 3


def _texto_da_linha(from_corretora: bool, tipo: str | None, texto: str | None,
                    transcricao: str | None) -> str | None:
    t = (texto or "").strip()
    if not from_corretora and (tipo or "") == "audio" and (transcricao or "").strip():
        return f"(áudio) {transcricao.strip()}"
    if not t or (t.startswith("[") and t.endswith("]") and len(t) < 20):
        return None   # marcador de mídia sem conteúdo
    if from_corretora and (tipo or "") == "template":
        # "[reengajamento: Aqui é da Acme…]" → o texto do disparo
        t = re.sub(r"^\[[^:\]]{1,40}:\s*", "", t).rstrip("]")
    return t


def conversa_da_corretor(session: Session, telefone: str, tenant_id: int = 1, *,
                    desde: datetime | None = None, ate: datetime | None = None,
                    limite: int = MAX_FALAS) -> list[Fala]:
    """As falas desta pessoa na linha da Corretor, em ordem, com ids L1/C1. Só esta
    pessoa, só esta linha."""
    from app.services import carrossel_corretor as cb

    suf = cb._so_digitos(telefone)[-8:]
    if not suf:
        return []
    linhas = session.execute(
        sql_text(_SQL_CONVERSA),
        {"inst": INSTANCIA_CORRETOR, "suf": suf, "t": tenant_id,
         "desde": (desde - timedelta(seconds=_FOLGA_S)) if desde else None,
         # Até a mensagem de agora, sem folga pra frente: o que a pessoa
         # escreveu DEPOIS não existe ainda na hora do turno.
         "ate": ate,
         "lim": limite},
    ).all()
    falas: list[Fala] = []
    nl = nc = 0
    for de_casa, tipo, texto, transcr, _ts in reversed(linhas):
        t = _texto_da_linha(bool(de_casa), tipo, texto, transcr)
        if not t:
            continue
        if de_casa:
            nc += 1
            falas.append(Fala(f"C{nc}", "casa", t))
        else:
            nl += 1
            falas.append(Fala(f"L{nl}", "lead", t))
    return falas


def _ultimo_disparo(session: Session, telefone: str, tenant_id: int) -> datetime | None:
    from app.services import carrossel_corretor as cb

    suf = cb._so_digitos(telefone)[-8:]
    return session.execute(
        sql_text(
            r"""SELECT max(le.at) FROM lead_events le JOIN leads l ON l.id = le.lead_id
                 WHERE le.tipo = :ev AND le.tenant_id = :t
                   AND right(regexp_replace(l.telefone, '\D', '', 'g'), 8) = :suf"""
        ),
        {"ev": cb.EVENT_DISPARO, "t": tenant_id, "suf": suf},
    ).scalar()


# ── Opinião na resposta ao disparo (chamada por carrossel_corretor) ───────────────

_SQL_NA_RODA_ANTES_DE_RESPONDEU = r"""
    SELECT 1 FROM leads
     WHERE tenant_id = :t AND owner_id IN (:corretor, :dono)
       AND stage_id IN (:fila_padrao, :fila, :disp)
       AND right(regexp_replace(telefone, '\D', '', 'g'), 8) = :suf
     LIMIT 1
"""


def na_roda_antes_de_respondeu(session: Session, telefone: str, tenant_id: int,
                               etapa_fila: int, dono: int) -> bool:
    from app.services import carrossel_corretor as cb

    suf = cb._so_digitos(telefone)[-8:]
    if not suf:
        return False
    return session.execute(
        sql_text(_SQL_NA_RODA_ANTES_DE_RESPONDEU),
        {"t": tenant_id, "corretor": cb.CORRETORA_CORRETOR, "dono": dono, "suf": suf,
         "fila_padrao": cb.STAGE_FILA_PADRAO, "fila": etapa_fila,
         "disp": cb.STAGE_DISPARADO},
    ).first() is not None


def opinar_na_resposta(session: Session, telefone: str, texto: str | None,
                       tenant_id: int, *, classe_regra: str, regra: str,
                       etapa_fila: int, dono: int,
                       recebido_em: datetime | None = None) -> tuple[str | None, dict[str, Any] | None]:
    """A regra disse `indefinida`. Devolve (classe que a IA põe no lugar ou
    None, registro pro evento ou None se a IA nem foi considerada).

    Só considera: regra `indefinida` com texto, chave ligada, card na roda
    antes de Respondeu. Nunca levanta; qualquer erro = (None, registro)."""
    from app.services import remarketing_resposta as rr

    if classe_regra != rr.INDEFINIDA or regra == rr.REGRA_MIDIA or not (texto or "").strip():
        return None, None
    try:
        if not opiniao_ligada():
            return None, None
        if not na_roda_antes_de_respondeu(session, telefone, tenant_id, etapa_fila, dono):
            return None, None
        corte_ = corte()
        desde = _ultimo_disparo(session, telefone, tenant_id)
        falas = conversa_da_corretor(session, telefone, tenant_id, desde=desde,
                                ate=recebido_em, limite=12)
        # O webhook grava a mensagem antes; se não estiver lá (ordem, relógio),
        # a fala de agora entra no fim.
        if not any(f.quem == "lead" and _norm(f.texto) == _norm(texto) for f in falas):
            n = sum(1 for f in falas if f.quem == "lead") + 1
            falas.append(Fala(f"L{n}", "lead", (texto or "").strip()))
        op = opinar(falas, ao_chegar_tarde=_anotar_tardia(telefone, tenant_id, corte_))
        pesou = decidir(op, corte_)
        reg = registro(op, classe_regra=classe_regra, pesou=pesou, corte_=corte_)
        log.info("ia_opiniao", tel=telefone[-4:], resultado=op.resultado,
                 classe_ia=op.classe, confianca=op.confianca, pesou=pesou, ms=op.ms)
        return pesou, reg
    except Exception as e:  # noqa: BLE001
        try:
            session.rollback()
        except Exception:  # noqa: BLE001
            pass
        log.warning("ia_opiniao_falhou", tel=telefone[-4:], erro=str(e)[:120])
        return None, registro(None, classe_regra=classe_regra, pesou=None,
                              corte_=None, motivo="erro")


def _anotar_tardia(telefone: str, tenant_id: int, corte_: float | None):
    """A opinião que chegou depois do prazo. O card já seguiu pela regra e não
    volta: aqui só se anota no evento o que a IA disse (`ia.tardia`) — e, se
    for interesse no corte, a resposta passa a contar na taxa (a classe do
    evento sai de `indefinida`). Recusa tardia não move nada."""
    def _anotar(op: Opiniao) -> None:
        from app.db.session import SessionLocal
        from app.services import carrossel_corretor as cb

        tardia = {"resultado": op.resultado, "classe_ia": op.classe,
                  "confianca": op.confianca, "trecho": op.trecho, "ms": op.ms}
        conta = decidir(op, corte_) == INTERESSE
        with SessionLocal() as s:
            cb.anotar_ia_tardia(s, telefone, tenant_id, tardia, conta_interesse=conta)
        log.info("ia_opiniao_tardia", tel=telefone[-4:], resultado=op.resultado,
                 classe_ia=op.classe, confianca=op.confianca, ms=op.ms, conta=conta)
    return _anotar


# ── Resumo ───────────────────────────────────────────────────────────────────

RESUMO_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["resumo", "fatos"],
    "properties": {
        "resumo": {"type": "string", "maxLength": RESUMO_MAX_CHARS},
        "fatos": {
            "type": "array",
            "maxItems": MAX_FATOS,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["fato", "fala", "trecho"],
                "properties": {
                    "fato": {"type": "string", "maxLength": FATO_MAX_CHARS},
                    "fala": {"type": "string", "description": "id da fala do lead: L1, L2…"},
                    "trecho": {"type": "string", "description": "pedaço LITERAL dessa fala"},
                },
            },
        },
    },
}

RESUMO_SYSTEM = (
    "Você escreve um resumo curtíssimo de uma conversa de WhatsApp para a "
    "corretora que vai atender essa pessoa. A corretora lê em 5 segundos.\n"
    "Regras:\n"
    "- Só o que a PESSOA (Lead) escreveu. Nada do que a Acme escreveu vira fato.\n"
    "- Cada fato aponta a fala de onde saiu (id L1, L2…) e copia um trecho "
    "LITERAL dela.\n"
    "- Não deduza, não complete, não invente número, bairro, valor ou prazo.\n"
    f"- 'resumo': uma frase de até {RESUMO_MAX_CHARS} caracteres. Se a pessoa só "
    "cumprimentou, diga isso.\n"
    f"- Até {MAX_FATOS} fatos, cada um com até {FATO_MAX_CHARS} caracteres."
)


@dataclass
class Resumo:
    resultado: str
    resumo: str | None = None
    fatos: list[dict[str, str]] = field(default_factory=list)
    descartados: int = 0
    ms: int = 0
    erro: str | None = None


def _numeros(t: str) -> set[str]:
    return set(re.findall(r"\d+", t or ""))


def validar_resumo(dado: dict[str, Any], falas: list[Fala]) -> Resumo:
    """Levanta ValueError se o formato não serve. Fato sem fala de origem (id
    que não é de fala do lead, ou trecho que não está nela) é descartado. Se o
    resumo cita número que a pessoa não escreveu, o resumo cai e ficam os fatos.
    Sem nenhum fato de pé, o resumo inteiro cai: não há o que mostrar."""
    resumo = dado.get("resumo")
    fatos = dado.get("fatos")
    if not isinstance(resumo, str) or not isinstance(fatos, list):
        raise ValueError("fora do esquema")
    do_lead = {f.id: f.texto for f in falas if f.quem == "lead"}
    bons: list[dict[str, str]] = []
    descartados = 0
    for fato in fatos[: MAX_FATOS * 2]:
        if not isinstance(fato, dict):
            descartados += 1
            continue
        fid = str(fato.get("fala") or "").strip().upper()
        trecho = str(fato.get("trecho") or "").strip()
        texto = str(fato.get("fato") or "").strip()
        origem = do_lead.get(fid)
        if not texto or origem is None or not _contido(trecho, origem):
            descartados += 1
            continue
        # Número no fato que não está na fala de origem: o modelo completou.
        if _numeros(texto) - _numeros(origem):
            descartados += 1
            continue
        bons.append({"fato": texto[:FATO_MAX_CHARS], "fala": fid,
                     "trecho": trecho[:120]})
        if len(bons) >= MAX_FATOS:
            break
    if not bons:
        return Resumo(resultado=INVALIDA, descartados=descartados,
                      erro="nenhum fato com fala de origem")
    resumo = resumo.strip()[:RESUMO_MAX_CHARS]
    todas = " ".join(do_lead.values())
    if not resumo or (_numeros(resumo) - _numeros(todas)):
        resumo = None
    return Resumo(resultado=OK, resumo=resumo, fatos=bons, descartados=descartados)


def resumir(falas: list[Fala], *, prazo_s: float | None = None) -> Resumo:
    """Nunca levanta."""
    prazo_s = PRAZO_RESUMO_S if prazo_s is None else prazo_s
    if not any(f.quem == "lead" for f in falas):
        return Resumo(resultado=INVALIDA, erro="sem fala do lead")
    try:
        resultado, dado, ms = _chamar(RESUMO_SYSTEM, _formatar(falas),
                                      RESUMO_SCHEMA, prazo_s)
    except Exception as e:  # noqa: BLE001
        return Resumo(resultado=FORA_DO_AR, erro=str(e)[:120])
    if resultado == FORA_DO_AR:
        return Resumo(resultado=FORA_DO_AR, ms=ms, erro=str(dado)[:120])
    if resultado != OK:
        return Resumo(resultado=resultado, ms=ms)
    try:
        r = validar_resumo(dado, falas)
    except ValueError as e:
        return Resumo(resultado=INVALIDA, ms=ms, erro=str(e)[:120])
    r.ms = ms
    return r


def _gravar_resumo(session: Session, tenant_id: int, lead_ids: list[int],
                   payload: dict[str, Any]) -> None:
    from app.services.lead_spine import _registrar_evento

    for lid in lead_ids:
        _registrar_evento(session, tenant_id, int(lid), EVENTO_RESUMO, dict(payload))


def resumir_e_gravar(lead_ids: list[int], telefone: str, tenant_id: int = 1,
                     origem: str = "transferencia") -> dict[str, Any]:
    """Lê a conversa, chama o modelo e grava `resumo_ia` em cada card. Grava
    um "pendente" antes, pra o card dizer que está saindo. Nunca levanta."""
    from app.db.session import SessionLocal

    ids = [int(i) for i in lead_ids if i is not None]
    if not ids:
        return {"resultado": "sem_lead"}
    try:
        with SessionLocal() as s:
            _gravar_resumo(s, tenant_id, ids, {"status": "pendente", "origem": origem})
            s.commit()
            desde = _ultimo_disparo(s, telefone, tenant_id)
            falas = conversa_da_corretor(s, telefone, tenant_id, desde=desde)
        r = resumir(falas)
        payload: dict[str, Any] = {
            "status": "ok" if r.resultado == OK else "falhou",
            "origem": origem, "resultado": r.resultado, "ms": r.ms,
            "falas_lead": sum(1 for f in falas if f.quem == "lead"),
        }
        if r.resultado == OK:
            payload.update(resumo=r.resumo, fatos=r.fatos, descartados=r.descartados)
        else:
            payload["erro"] = (r.erro or r.resultado)[:120]
        with SessionLocal() as s:
            _gravar_resumo(s, tenant_id, ids, payload)
            s.commit()
        log.info("ia_resumo", leads=ids, resultado=r.resultado, ms=r.ms,
                 fatos=len(r.fatos), descartados=r.descartados)
        return payload
    except Exception as e:  # noqa: BLE001
        log.warning("ia_resumo_falhou", leads=ids, erro=str(e)[:120])
        try:
            with SessionLocal() as s:
                _gravar_resumo(s, tenant_id, ids, {"status": "falhou", "origem": origem,
                                                   "resultado": "erro"})
                s.commit()
        except Exception:  # noqa: BLE001
            pass
        return {"status": "falhou", "resultado": "erro"}


# Os testes trocam por execução síncrona (sem thread solta no meio do teste).
def _em_segundo_plano(alvo: Callable[[], Any]) -> None:
    threading.Thread(target=alvo, name="ia-resumo", daemon=True).start()


def resumir_em_segundo_plano(lead_ids: list[int], telefone: str,
                             tenant_id: int = 1, origem: str = "transferencia") -> bool:
    """Chamado DEPOIS do commit da transferência (ou do segurado). Devolve se
    agendou. Chave desligada = a IA não é chamada. Nunca levanta."""
    try:
        if not lead_ids or not resumo_ligado():
            return False
        ids = list(lead_ids)
        _em_segundo_plano(lambda: resumir_e_gravar(ids, telefone, tenant_id, origem))
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("ia_resumo_nao_agendado", erro=str(e)[:120])
        return False


# ── Leitura pro card ─────────────────────────────────────────────────────────


def _para_card(payload: dict[str, Any] | None, at: datetime | None,
               agora: datetime | None = None) -> dict[str, Any] | None:
    if not payload:
        return None
    status = payload.get("status")
    agora = agora or datetime.now(timezone.utc)
    if at is not None and at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    if status == "pendente" and at is not None and (agora - at).total_seconds() > RESUMO_PENDENTE_VENCE_S:
        status = "falhou"
    out: dict[str, Any] = {"status": status if status in ("ok", "pendente") else "sem_resumo",
                           "em": at.isoformat() if at else None}
    if status == "ok":
        out["resumo"] = payload.get("resumo")
        out["fatos"] = [
            {"fato": f.get("fato"), "trecho": f.get("trecho")}
            for f in (payload.get("fatos") or []) if isinstance(f, dict)
        ]
    return out


def resumos_por_lead(session: Session, lead_ids: list[int]) -> dict[int, dict[str, Any]]:
    """lead_id → o último resumo ({status: ok|pendente|sem_resumo, resumo,
    fatos, em}). Card sem evento não aparece aqui (não mostra nada). Nunca
    levanta: é informação de card."""
    if not lead_ids:
        return {}
    try:
        linhas = session.execute(
            sql_text(
                """SELECT DISTINCT ON (lead_id) lead_id, payload, at
                     FROM lead_events
                    WHERE lead_id = ANY(:ids) AND tipo = :ev
                    ORDER BY lead_id, at DESC, id DESC"""
            ),
            {"ids": [int(i) for i in lead_ids], "ev": EVENTO_RESUMO},
        ).all()
    except Exception as e:  # noqa: BLE001
        try:
            session.rollback()
        except Exception:  # noqa: BLE001
            pass
        log.warning("resumos_por_lead_indisponivel", erro=str(e)[:120])
        return {}
    agora = datetime.now(timezone.utc)
    out: dict[int, dict[str, Any]] = {}
    for lid, payload, at in linhas:
        c = _para_card(payload, at, agora)
        if c:
            out[int(lid)] = c
    return out


# ── O que a tela mostra ──────────────────────────────────────────────────────

FALHA_RESUMO = ("Se a IA falhar ou demorar, o card chega na corretora do mesmo jeito "
                "e mostra \"sem resumo\".")
FALHA_OPINIAO = ("Se a IA falhar, demorar mais de {prazo} segundos ou não tiver certeza, "
                 "vale a regra: a pessoa vai pra roleta como hoje.")
PARA_QUE_SERVE_OPINIAO = ("Todo mundo que responde já vai pra corretora; a opinião da IA só "
                          "manda pra Venda perdida a recusa que a regra não leu e conta o "
                          "interesse na taxa.")

_SQL_CONTAGENS = r"""
    SELECT
      count(*) FILTER (WHERE tipo = :ev_res AND payload->>'status' = 'ok')       AS resumos_ok,
      count(*) FILTER (WHERE tipo = :ev_res AND payload->>'status' = 'falhou')   AS resumos_falhou,
      count(*) FILTER (WHERE tipo = :ev_resp AND payload ? 'ia')                 AS opinioes,
      count(*) FILTER (WHERE tipo = :ev_resp AND (payload->'ia'->>'pesou')::boolean) AS pesou,
      count(*) FILTER (WHERE tipo = :ev_resp AND payload->'ia'->>'classe_final' = 'recusa'
                         AND (payload->'ia'->>'pesou')::boolean)                 AS recusas_ia
      FROM lead_events
     WHERE tenant_id = :t AND tipo IN (:ev_res, :ev_resp)
       AND at > now() - interval '30 days'
"""


def para_tela(session: Session, tenant_id: int = 1) -> dict[str, Any]:
    """As duas chaves, o corte e o que aconteceu nos últimos 30 dias."""
    from app.services import carrossel_corretor as cb

    def _bool(chave: str) -> bool | None:
        try:
            v = _cfg(chave)
        except Exception:  # noqa: BLE001
            return None
        return v if isinstance(v, bool) else None

    try:
        corte_bruto = _cfg(CHAVE_CORTE)
        corte_ = validar_corte(corte_bruto)
        corte_problema = None
    except Exception as e:  # noqa: BLE001
        corte_, corte_problema = None, str(e)[:120]
    out: dict[str, Any] = {
        "resumo_ligado": _bool(CHAVE_RESUMO),
        "opina_ligada": _bool(CHAVE_OPINA),
        "corte": corte_,
        "corte_padrao": CORTE_PADRAO,
        "corte_problema": corte_problema,
        "prazo_opiniao_s": PRAZO_OPINIAO_S,
        "falha_resumo": FALHA_RESUMO,
        "falha_opiniao": FALHA_OPINIAO.format(prazo=f"{PRAZO_OPINIAO_S:g}"),
        "para_que_serve_opiniao": PARA_QUE_SERVE_OPINIAO,
    }
    try:
        r = session.execute(
            sql_text(_SQL_CONTAGENS),
            {"t": tenant_id, "ev_res": EVENTO_RESUMO, "ev_resp": cb.EVENT_RESPOSTA},
        ).mappings().first() or {}
        out["ultimos_30_dias"] = {k: int(r.get(k) or 0) for k in
                                  ("resumos_ok", "resumos_falhou", "opinioes", "pesou",
                                   "recusas_ia")}
    except Exception as e:  # noqa: BLE001
        session.rollback()
        log.warning("ia_do_lado_contagem_indisponivel", erro=str(e)[:120])
        out["ultimos_30_dias"] = None
    return out
