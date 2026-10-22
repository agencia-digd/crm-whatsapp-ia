"""
Comparador sombra — "bateu ou não": rascunho da IA × resposta REAL da corretora.

Pra cada decisão sombra que tem rascunho (`shadow_decisions.resposta_sugerida`),
localiza a resposta que a corretora de fato mandou pro MESMO lead logo depois
(`mensagens_whatsapp`, `from_corretora=TRUE`, mesmo telefone por sufixo de 8
dígitos, dentro da janela) e mede a similaridade com heurística 100% LOCAL —
NUNCA chama LLM nem API externa.

Heurística de similaridade (documentada — thresholds simples):
  1. Normalização: minúsculas, remove acentos (NFKD), remove pontuação/emoji
     (só letras/dígitos/espaço), colapsa espaços.
  2. Jaccard de tokens (palavras): |A∩B| / |A∪B|.
  3. Dice de trigramas de caracteres: 2·|T(a)∩T(b)| / (|T(a)|+|T(b)|).
  4. Score final = 0.4·jaccard + 0.6·dice_trigramas  (trigrama pesa mais
     porque tolera flexão/typo, comum em WhatsApp).

Veredito:
  score >= 0.55 → "bateu"        (mesma resposta na essência)
  score >= 0.30 → "parcial"      (mesma direção, texto diferente)
  score <  0.30 → "nao_bateu"
  sem resposta da corretora na janela → "sem_resposta_real"
  corretora respondeu mas sem texto comparável (ex.: áudio não transcrito,
  imagem) → "sem_comparacao"

Resposta real = "rajada" da corretora: a partir da 1ª mensagem dela após o
rascunho, junta as mensagens consecutivas dela em até 30 min (máx 5 bolhas) —
corretora costuma responder em várias bolhas.

Tudo puro/testável: recebe listas de dicts, devolve dicts. Quem consulta o
banco é o endpoint (app/api/aprendiz.py).
"""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog

log = structlog.get_logger()

# ── Parâmetros da heurística (mudar aqui = mudar a doc acima) ────────────────
THRESHOLD_BATEU = 0.55
THRESHOLD_PARCIAL = 0.30
PESO_JACCARD_TOKENS = 0.4
PESO_DICE_TRIGRAMAS = 0.6
JANELA_RESPOSTA_HORAS = 72      # até quando procurar a resposta real
RAJADA_MINUTOS = 30             # bolhas consecutivas da corretora viram 1 resposta
RAJADA_MAX_BOLHAS = 5
MIN_CHARS_COMPARAVEIS = 3       # texto normalizado menor que isso não compara

VEREDITOS = ("bateu", "parcial", "nao_bateu", "sem_resposta_real", "sem_comparacao")

# placeholder de mídia gravado pelo webhook quando não há texto: "[audio]", "[image]"…
_PLACEHOLDER_RE = re.compile(r"^\[[a-z_]+\]$")
_NAO_ALFANUM_RE = re.compile(r"[^a-z0-9\s]+")
_ESPACOS_RE = re.compile(r"\s+")


# ── Normalização e similaridade ──────────────────────────────────────────────

def normalizar(texto: str | None) -> str:
    """minúsculas + sem acento + só [a-z0-9 espaço] + espaços colapsados."""
    if not texto:
        return ""
    s = unicodedata.normalize("NFKD", texto)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = _NAO_ALFANUM_RE.sub(" ", s)
    return _ESPACOS_RE.sub(" ", s).strip()


def _trigramas(s: str) -> set[str]:
    s = f"  {s} "  # padding pra valorizar início/fim de palavra
    return {s[i : i + 3] for i in range(len(s) - 2)}


def similaridade(a: str | None, b: str | None) -> float | None:
    """Score 0-1 entre dois textos. None se algum lado não tem texto comparável."""
    na, nb = normalizar(a), normalizar(b)
    if len(na) < MIN_CHARS_COMPARAVEIS or len(nb) < MIN_CHARS_COMPARAVEIS:
        return None
    ta, tb = set(na.split()), set(nb.split())
    jaccard = len(ta & tb) / len(ta | tb) if (ta | tb) else 0.0
    ga, gb = _trigramas(na), _trigramas(nb)
    dice = 2 * len(ga & gb) / (len(ga) + len(gb)) if (ga or gb) else 0.0
    return round(PESO_JACCARD_TOKENS * jaccard + PESO_DICE_TRIGRAMAS * dice, 4)



# ── FORMA da mensagem: o que ela FAZ, não o que ela diz ──────────────────────
#
# A similaridade textual responde "escreveu igual?" — e a resposta é quase
# sempre não: duas mensagens certas raramente têm as mesmas palavras. Medido em
# 100 pares reais: 0% "bateu" por texto. Isso não quer dizer que a IA errou 100
# das vezes; quer dizer que a régua estava medindo a coisa errada.
#
# A FORMA responde outra pergunta, mais útil: "a IA faria a mesma coisa que a
# corretora fez?". Quatro formas, e elas cobrem o que uma mensagem de WhatsApp
# de corretora faz no fim das contas:
#
#   pergunta  → devolve a bola pro lead
#   promessa  → assume uma tarefa ("te mando", "vou confirmar")
#   despedida → encerra a conversa
#   afirmacao → informa e não pede nada
#
# Continua 100% local, sem LLM. É heurística — não separa uma pergunta boa de
# uma ruim. Mas separa "a IA ia perguntar e a corretora perguntou" de "a IA ia
# se despedir e a corretora marcou visita", que é a diferença que importa.

_PROMESSA = (
    "vou te mandar", "te mando", "te aviso", "ja te", "já te", "vou verificar",
    "vou confirmar", "vou ver", "te falo", "assim que", "vou providenciar",
    "vou separar", "deixa comigo", "pode deixar", "te retorno", "te envio",
)
_DESPEDIDA = (
    "boa sorte", "pode me chamar", "estou a disposicao", "to a disposicao",
    "fico a disposicao", "qualquer coisa", "me chama", "disponha",
    "conte comigo", "qualquer duvida",
)

FORMAS = ("pergunta", "promessa", "despedida", "afirmacao")


def forma(texto: str | None) -> str | None:
    """Classifica o que a mensagem FAZ. None se não há texto comparável.

    A ordem importa e não é arbitrária:
      1. PERGUNTA ganha de tudo — se pediu algo, a bola está com o lead, mesmo
         que a mensagem também prometa;
      2. PROMESSA vem antes de DESPEDIDA porque "te mando amanhã, qualquer coisa
         me chama" é um compromisso com um rodapé, não uma despedida;
      3. DESPEDIDA só quando é o que sobra.
    """
    n = normalizar(texto)
    if not n:
        return None
    if "?" in (texto or ""):
        return "pergunta"
    if any(p in n for p in _PROMESSA):
        return "promessa"
    if any(d in n for d in _DESPEDIDA):
        return "despedida"
    return "afirmacao"


def veredito_do_score(score: float | None) -> str:
    if score is None:
        return "sem_comparacao"
    if score >= THRESHOLD_BATEU:
        return "bateu"
    if score >= THRESHOLD_PARCIAL:
        return "parcial"
    return "nao_bateu"


# ── Helpers de dados ──────────────────────────────────────────────────────────

def _sufixo8(telefone: str | None) -> str | None:
    digits = "".join(c for c in (telefone or "") if c.isdigit())
    return digits[-8:] if len(digits) >= 8 else (digits or None)


def _as_utc(dt: datetime | str | None) -> datetime | None:
    """Coage pra datetime UTC. SQLite (raw SQL) devolve string e naive;
    Postgres devolve datetime aware."""
    if dt is None:
        return None
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _texto_da_msg(m: dict[str, Any]) -> str | None:
    """Texto comparável: transcrição (áudio) > conteúdo; placeholder não vale."""
    for campo in ("transcricao", "conteudo_texto"):
        t = (m.get(campo) or "").strip()
        if t and not _PLACEHOLDER_RE.match(t):
            return t
    return None


def extrair_resposta_real(
    msgs_corretora: list[dict[str, Any]],
    apos: datetime,
    janela_horas: int = JANELA_RESPOSTA_HORAS,
) -> dict[str, Any] | None:
    """Acha a rajada de resposta da corretora depois de `apos`.

    `msgs_corretora`: mensagens from_corretora do MESMO lead/telefone, cada uma
    com timestamp_evolution/conteudo_texto/transcricao. Ordena por timestamp.

    Retorna {"texto": str|None, "primeira_at": datetime, "bolhas": int} ou None
    se a corretora não mandou nada na janela. texto=None → só mídia sem texto.
    """
    apos = _as_utc(apos)
    limite = apos + timedelta(hours=janela_horas)
    candidatas = sorted(
        (m for m in msgs_corretora
         if apos < (_as_utc(m.get("timestamp_evolution")) or apos) <= limite),
        key=lambda m: _as_utc(m["timestamp_evolution"]),
    )
    if not candidatas:
        return None
    t0 = _as_utc(candidatas[0]["timestamp_evolution"])
    fim_rajada = t0 + timedelta(minutes=RAJADA_MINUTOS)
    rajada = [m for m in candidatas
              if _as_utc(m["timestamp_evolution"]) <= fim_rajada][:RAJADA_MAX_BOLHAS]
    textos = [t for t in (_texto_da_msg(m) for m in rajada) if t]
    return {
        "texto": "\n".join(textos) if textos else None,
        "primeira_at": t0,
        "bolhas": len(rajada),
    }


# ── Montagem dos pares ────────────────────────────────────────────────────────

VEREDITOS_FINAIS = ("bateu", "parcial", "nao_bateu", "pendente", "sem_resposta_real")


def veredito_final(
    acerto: bool | None, juiz: str | None, *, tem_real: bool
) -> tuple[str, str | None]:
    """(veredito, fonte). Humano (acerto) > juiz IA > pendente/sem_resposta_real.

    A marcação humana vale mesmo sem resposta real ("o rascunho serviria?").
    """
    if acerto is not None:  # SQLite/raw SQL devolve 0/1; Postgres devolve bool
        return ("bateu" if bool(acerto) else "nao_bateu"), "humano"
    if juiz in ("bateu", "parcial", "nao_bateu"):
        return juiz, "ia"
    return ("pendente" if tem_real else "sem_resposta_real"), None


def montar_pares(
    decisoes: list[dict[str, Any]],
    mensagens_corretora: list[dict[str, Any]],
    janela_horas: int = JANELA_RESPOSTA_HORAS,
) -> list[dict[str, Any]]:
    """Cruza decisões sombra (com rascunho) × mensagens reais e dá o veredito.

    `decisoes`: linhas de shadow_decisions com resposta_sugerida preenchida.
    `mensagens_corretora`: linhas de mensagens_whatsapp com from_corretora=TRUE
    (já filtradas por período/corretora no SQL).
    """
    # indexa mensagens por (corretora_id, sufixo8 do telefone do lead)
    por_chave: dict[tuple[int | None, str | None], list[dict[str, Any]]] = {}
    for m in mensagens_corretora:
        chave = (m.get("corretora_id"), _sufixo8(m.get("contato_telefone")))
        por_chave.setdefault(chave, []).append(m)

    pares: list[dict[str, Any]] = []
    for d in decisoes:
        ancora = _as_utc(d.get("ultima_msg_at")) or _as_utc(d.get("created_at"))
        chave = (d.get("corretora_id"), _sufixo8(d.get("lead_telefone")))
        real = extrair_resposta_real(por_chave.get(chave, []), ancora, janela_horas)

        if real is None:
            verd, score, resposta_real, real_at = "sem_resposta_real", None, None, None
        else:
            resposta_real, real_at = real["texto"], real["primeira_at"]
            score = similaridade(d.get("resposta_sugerida"), resposta_real)
            verd = veredito_do_score(score)

        final, fonte = veredito_final(
            d.get("acerto"), d.get("juiz_veredito"), tem_real=real is not None
        )
        pares.append({
            "decisao_id": d.get("id"),
            "corretora_id": d.get("corretora_id"),
            "lead_id": d.get("lead_id"),
            "lead_nome": d.get("lead_nome"),
            "lead_telefone": d.get("lead_telefone"),
            "bucket": d.get("bucket"),
            "confianca": d.get("confianca"),
            "ultima_msg_lead": d.get("ultima_msg_lead"),
            "rascunho": d.get("resposta_sugerida"),
            "rascunho_at": _as_utc(d.get("created_at")),
            "resposta_real": resposta_real,
            "resposta_real_at": real_at,
            "similaridade": score,
            "veredito": verd,
            # Placar honesto: humano > juiz IA > pendente. O trigrama acima fica só
            # como informação (escrever parecido nunca foi o objetivo).
            "acerto": d.get("acerto"),
            "avaliado_por": d.get("avaliado_por"),
            "juiz_veredito": d.get("juiz_veredito"),
            "juiz_motivo": d.get("juiz_motivo"),
            "veredito_final": final,
            "fonte": fonte,
        })
    return pares


def agregar_placar(pares: list[dict[str, Any]]) -> dict[str, Any]:
    """Agrega uma lista de pares num placar: contagens + % bateu."""
    contagem = {v: 0 for v in VEREDITOS}
    scores: list[float] = []
    for p in pares:
        contagem[p["veredito"]] += 1
        if p["similaridade"] is not None:
            scores.append(p["similaridade"])
    comparaveis = contagem["bateu"] + contagem["parcial"] + contagem["nao_bateu"]

    # Concordância de FORMA: das vezes em que dá pra comparar, quantas a IA
    # faria a mesma COISA que a corretora fez (perguntar, prometer, encerrar)?
    # É a métrica que diz se ela está pronta — a de texto só diz se ela escreve
    # parecido, e escrever parecido nunca foi o objetivo.
    formas_ia: dict[str, int] = {f: 0 for f in FORMAS}
    formas_real: dict[str, int] = {f: 0 for f in FORMAS}
    mesma, com_forma = 0, 0
    for p in pares:
        fi, fr = forma(p.get("rascunho")), forma(p.get("resposta_real"))
        if fi:
            formas_ia[fi] = formas_ia.get(fi, 0) + 1
        if fi and fr:
            com_forma += 1
            formas_real[fr] = formas_real.get(fr, 0) + 1
            if fi == fr:
                mesma += 1

    final = {v: 0 for v in VEREDITOS_FINAIS}
    fontes = {"humano": 0, "ia": 0}
    for p in pares:
        final[p.get("veredito_final") or "pendente"] += 1
        if p.get("fonte") in fontes:
            fontes[p["fonte"]] += 1
    avaliados = final["bateu"] + final["parcial"] + final["nao_bateu"]

    return {
        # Placar honesto (por intenção/ação, não por trigrama). `acertou_pct` conta
        # bateu; `acertou_ou_parcial_pct` também o parcial.
        "final": final,
        "fonte_avaliacao": fontes,
        "avaliados": avaliados,
        "pendentes_avaliacao": final["pendente"],
        "acertou_pct": round(100 * final["bateu"] / avaliados) if avaliados else None,
        "acertou_ou_parcial_pct": (
            round(100 * (final["bateu"] + final["parcial"]) / avaliados) if avaliados else None
        ),
        "forma_ia": formas_ia,
        "forma_corretora": formas_real,
        "mesma_forma_pct": round(100 * mesma / com_forma) if com_forma else None,
        "com_forma": com_forma,
        # A regra do próximo passo (05/08): quanto dos rascunhos da IA termina
        # ENCERRANDO uma conversa. É o número que a mudança de prompt tem que
        # derrubar — e o que sobe se alguém afrouxar a regra depois.
        "despedidas_ia_pct": (
            round(100 * formas_ia["despedida"] / sum(formas_ia.values()))
            if sum(formas_ia.values()) else None
        ),
        "total_rascunhos": len(pares),
        "comparaveis": comparaveis,
        "vereditos": contagem,
        "bateu_pct": round(100 * contagem["bateu"] / comparaveis) if comparaveis else None,
        "bateu_ou_parcial_pct": (
            round(100 * (contagem["bateu"] + contagem["parcial"]) / comparaveis)
            if comparaveis else None
        ),
        "similaridade_media": round(100 * sum(scores) / len(scores)) if scores else None,
    }
