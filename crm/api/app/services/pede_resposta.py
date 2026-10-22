"""Classificação "essa mensagem do lead PEDE resposta?" — a limpeza do "OK".

O problema (apontado pela Dona, 08/2026): os cards de espera contavam QUALQUER
última mensagem do lead como pendência. Aí "ok", "obrigado" e "👍" entravam na
lista, e a corretora mandava um joinha só pra limpar a tela. A lista treinava
ela a fazer trabalho falso — e o efeito pior é que ela parava de confiar no
card.

A regra certa: não é a ÚLTIMA mensagem que importa, é se ela **pede resposta**.

Duas camadas, nesta ordem:

1. **determinística** (este módulo, sem IA): lista de encerramentos comuns
   (ok · obrigado · valeu · blz · tá bom · entendi · 👍 · 🙏 · tks) + padrões de
   "a bola está com o lead" ("vou ver e te falo"). Resolve o grosso, é grátis e
   roda em microssegundos — importa porque isso alimenta a tela que abre toda
   manhã;
2. **IA** (`refinar_duvidas_com_ia`, opcional, desligada por default): só é
   chamada pros casos marcados `duvida=True` e SÓ pode tirar da lista — nunca
   colocar.

**A TRAVA — na dúvida, MANTÉM na lista.** Erro pra menos custa um clique; erro
pra mais custa um lead. Toda decisão ambígua aqui devolve `pede=True`.

Consequências práticas da trava (foram escolhas, não esquecimento):
- mensagem de MÍDIA (áudio, foto, documento) sempre pede resposta — não dá pra
  ler o conteúdo aqui, e um áudio ignorado é um lead perdido;
- "sim", "isso", "certo", "perfeito", "combinado", "fechado" NÃO são
  encerramento: quase sempre são "sim, quero" e exigem ação da corretora;
- "bom dia" sozinho NÃO é encerramento — é abertura de conversa;
- qualquer coisa com "?" pede resposta, sempre.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

import structlog

log = structlog.get_logger()

# ── vocabulário ──────────────────────────────────────────────────────────────

# NÚCLEO do encerramento: sozinho já encerra a conversa. Se a mensagem não tem
# nenhum destes, ela NÃO é encerramento (a trava mora aqui).
NUCLEO_ENCERRAMENTO: frozenset[str] = frozenset({
    "ok", "okay", "okey", "oki", "okk", "okkk", "okok", "k",
    "blz", "blza", "beleza", "belezaa", "bls",
    "valeu", "valew", "vlw", "vlws", "vlww",
    "obrigado", "obrigada", "obrigadoo", "obrigadaa", "obrigadao", "obrigadona",
    "obrigadinho", "obrigadinha", "obg", "obgd", "obgda", "obgado", "obgd0",
    "brigado", "brigada", "brigadao",
    "grato", "grata", "agradecido", "agradecida", "agradeco",
    "tks", "thanks", "thx", "ty",
    "entendi", "entendido", "entendida", "entendii",
    "anotado", "anotada",
})

# SATÉLITES: sozinhos não dizem nada, mas acompanham o núcleo sem mudar o
# sentido ("muito obrigado", "ok então", "valeu viu"). NÃO entram aqui palavras
# que sinalizam decisão do lead (sim/isso/certo/fechado/combinado) — essas
# pedem ação da corretora.
SATELITE_ENCERRAMENTO: frozenset[str] = frozenset({
    "ta", "tah", "to", "muito", "mto", "mt", "muita", "entao", "entaum",
    "viu", "hein", "ein", "demais", "mesmo", "ai", "aa", "ah", "aah", "ahh",
    "pela", "pelas", "pelo", "pelos", "atencao", "informacao", "informacoes",
    "info", "resposta", "retorno", "ajuda", "e", "por", "enquanto", "agora",
    "ja", "sim",  # "ok sim" ainda é ok; "sim" SOZINHO não passa (falta núcleo)
    "de", "nada", "isso",  # idem: só sobrevivem acompanhando um núcleo
    "bom", "boa", "bem", "tudo", "certo", "otimo", "otima", "show", "joia",
    "legal", "bacana", "massa", "top", "tranquilo", "tranquila", "suave",
    "perfeito", "perfeita", "combinado", "fechado", "beleza",
})

# Frases inteiras que encerram mas não têm núcleo (o teste de tokens não pega).
FRASES_ENCERRAMENTO: frozenset[str] = frozenset({
    "ta bom", "ta bom entao", "tabom", "ta bem", "ta certo", "ta otimo",
    "ta joia", "ta tranquilo", "ta suave", "ta anotado", "ta combinado",
    "tudo bem", "tudo bem entao", "tudo certo", "de nada", "por nada",
    "sem problemas", "sem problema", "imagina", "magina", "que isso",
    "nao precisa", "nao precisa nao", "por enquanto e so", "so isso",
    "era so isso", "e so isso", "era isso", "por ora e so",
})

# Emojis que sozinhos são um "recebi, obrigado" — nada a responder.
EMOJIS_ENCERRAMENTO: frozenset[str] = frozenset({
    "👍", "👍🏻", "👍🏼", "👍🏽", "👍🏾", "👍🏿",
    "🙏", "🙏🏻", "🙏🏼", "🙏🏽", "🙏🏾", "🙏🏿",
    "👌", "👌🏻", "👌🏼", "👌🏽", "👌🏾", "👌🏿",
    "🙌", "👏", "✅", "☑️", "✔️", "🤝", "😊", "😉", "🥰", "❤️", "♥️", "❤",
    "💚", "💙", "💜", "🧡", "💛", "😍", "😁", "😄", "🤗", "🫡", "💪",
})

# "A bola está com o LEAD": ele assumiu o próximo passo. Cobrar resposta aqui é
# atropelo — não entra na lista.
PADROES_BOLA_COM_ELE: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bvou (ver|pensar|analisar|olhar|checar|conferir|avaliar)\b"),
    re.compile(r"\bvou (dar uma|da uma) (olhada|pensada|analisada)\b"),
    re.compile(r"\bvou (falar|conversar) com\b"),
    re.compile(r"\b(te|lhe) (falo|aviso|retorno|respondo|chamo|procuro)\b"),
    re.compile(r"\b(depois|amanha|mais tarde|semana que vem|logo) (eu )?(te |lhe )?"
               r"(falo|aviso|retorno|respondo|vejo|chamo|procuro)\b"),
    re.compile(r"\b(qualquer coisa|qq coisa|se precisar|se der certo|se rolar) "
               r"(eu )?(te |lhe )?(falo|aviso|chamo|procuro)\b"),
    re.compile(r"\bassim que .{0,40}\b(te |lhe )?(falo|aviso|retorno|chamo)\b"),
    re.compile(r"\b(preciso|vou precisar) (ver|falar|conversar|pensar)\b"),
    re.compile(r"\bqualquer novidade (eu )?(te |lhe )?(falo|aviso|chamo)\b"),
    re.compile(r"\bdepois (eu )?(vejo|penso|analiso|olho)\b"),
)

# Tipos de mensagem do `mensagens_whatsapp` que NÃO são texto lido aqui.
# Não dá pra classificar o conteúdo → pede resposta (trava).
TIPOS_MIDIA: frozenset[str] = frozenset({
    "audio", "image", "imagem", "video", "document", "documento", "sticker",
    "figurinha", "location", "localizacao", "contact", "contato", "ptt",
    "audioMessage", "imageMessage", "videoMessage", "documentMessage",
})

# Acima disso a mensagem já é conversa de verdade, não um "ok".
MAX_TOKENS_ENCERRAMENTO = 6
# Acima disso nem tentamos o padrão "bola com ele" — texto longo é assunto.
MAX_CHARS_BOLA_COM_ELE = 180
# Abaixo disso, mensagem curta sem "?" e sem encerramento conhecido fica
# marcada como DÚVIDA (mantida na lista; candidata à leitura da Assistente).
MAX_CHARS_DUVIDA = 28

_RE_NAO_PALAVRA = re.compile(r"[^\w\s]", flags=re.UNICODE)
_RE_ESPACOS = re.compile(r"\s+")


@dataclass(frozen=True)
class Decisao:
    """Resultado da classificação.

    `pede`   — entra na lista de pendência?
    `motivo` — chave curta e estável (serve pro front e pra teste/telemetria).
    `duvida` — caso ambíguo: ficou na lista pela trava, mas é o candidato que a
               camada de IA pode revisar.
    """

    pede: bool
    motivo: str
    duvida: bool = False


def _sem_acento(texto: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", texto)
        if unicodedata.category(c) != "Mn"
    )


def _so_emoji(texto: str) -> bool:
    """Sobrou só emoji/símbolo depois de tirar letras, números e espaço?"""
    resto = re.sub(r"[\w\s]", "", texto, flags=re.UNICODE)
    return bool(resto) and not re.search(r"[\w]", texto, flags=re.UNICODE)


def _emojis_todos_de_agradecimento(texto: str) -> bool:
    """Todo caractere visível é emoji da lista de 'recebi, obrigado'?

    Compara por caractere-base pra não depender de modificador de tom de pele
    nem de variation selector (U+FE0F).
    """
    bases = {
        ch for ch in texto
        if not ch.isspace()
        and unicodedata.category(ch) not in ("Mn", "Cf")
        and ch not in "\U0001F3FB\U0001F3FC\U0001F3FD\U0001F3FE\U0001F3FF"
    }
    if not bases:
        return False
    permitidos = {
        ch for e in EMOJIS_ENCERRAMENTO for ch in e
        if not ch.isspace()
        and unicodedata.category(ch) not in ("Mn", "Cf")
        and ch not in "\U0001F3FB\U0001F3FC\U0001F3FD\U0001F3FE\U0001F3FF"
    }
    return bases <= permitidos


def normalizar(texto: str) -> str:
    """minúsculo, sem acento, sem pontuação/emoji, espaço colapsado."""
    t = _sem_acento(texto.lower())
    t = _RE_NAO_PALAVRA.sub(" ", t)
    return _RE_ESPACOS.sub(" ", t).strip()


def classificar(texto: str | None, tipo: str | None = None) -> Decisao:
    """Essa mensagem do LEAD pede resposta da corretora?

    Só faz sentido chamar com mensagem DO LEAD — quem decide isso é o chamador
    (`from_corretora = False`).
    """
    tipo_norm = (tipo or "").strip()
    if tipo_norm and tipo_norm.lower() != "text" and tipo_norm in TIPOS_MIDIA:
        # não dá pra ler o conteúdo aqui: mantém (trava)
        return Decisao(True, "midia")

    bruto = (texto or "").strip()
    if not bruto:
        # sem texto e sem tipo conhecido: mantém (trava)
        return Decisao(True, "sem_texto")

    if "?" in bruto:
        return Decisao(True, "pergunta")

    if _so_emoji(bruto):
        if _emojis_todos_de_agradecimento(bruto):
            return Decisao(False, "emoji_encerramento")
        return Decisao(True, "emoji_desconhecido", duvida=True)

    norm = normalizar(bruto)
    if not norm:
        # tinha texto, mas sobrou só pontuação/emoji não catalogado
        return Decisao(True, "sem_conteudo_legivel", duvida=True)

    if norm in FRASES_ENCERRAMENTO:
        return Decisao(False, "frase_encerramento")

    tokens = norm.split()
    if len(tokens) <= MAX_TOKENS_ENCERRAMENTO:
        vocabulario = NUCLEO_ENCERRAMENTO | SATELITE_ENCERRAMENTO
        if set(tokens) <= vocabulario and set(tokens) & NUCLEO_ENCERRAMENTO:
            return Decisao(False, "encerramento")

    if len(bruto) <= MAX_CHARS_BOLA_COM_ELE:
        for padrao in PADROES_BOLA_COM_ELE:
            if padrao.search(norm):
                return Decisao(False, "bola_com_o_lead")

    if len(bruto) <= MAX_CHARS_DUVIDA:
        # curta, sem "?", sem encerramento conhecido: fica NA LISTA (trava) e
        # marcada pra eventual leitura da Assistente.
        return Decisao(True, "curta_ambigua", duvida=True)

    return Decisao(True, "conteudo")


def pede_resposta(texto: str | None, tipo: str | None = None) -> bool:
    """Atalho booleano de `classificar` — o que os endpoints usam no filtro."""
    return classificar(texto, tipo).pede


# ── camada 2: a Assistente, só na dúvida ────────────────────────────────────────

_SYSTEM_IA = (
    "Você lê a ÚLTIMA mensagem que um lead mandou pra uma corretora de imóveis "
    "no WhatsApp e diz se ela PEDE uma resposta da corretora.\n"
    "PEDE resposta: pergunta, pedido, demonstração de interesse, qualquer coisa "
    "que fique estranho ficar sem retorno.\n"
    "NÃO pede: agradecimento, confirmação de recebimento, despedida, ou quando "
    "o próprio lead assumiu o próximo passo.\n"
    "NA DÚVIDA, responda que PEDE. Errar pra menos custa um clique da corretora; "
    "errar pra mais custa um lead.\n"
    'Responda SÓ JSON: {"itens":[{"i":0,"pede":true},...]}'
)


def refinar_duvidas_com_ia(
    textos: list[str],
    *,
    provider: Any | None = None,
    limite: int = 12,
) -> dict[int, bool]:
    """Segunda camada — a leitura da Assistente nos casos `duvida=True`.

    Recebe os textos ambíguos (índice → texto) e devolve `{índice: pede}` SÓ
    pros que o modelo conseguiu classificar. Regras:

    - só é chamada pra quem a camada 1 marcou `duvida`;
    - no máximo `limite` itens por chamada (é tela de manhã, não batch);
    - QUALQUER falha (modelo fora, JSON inválido, item faltando) devolve o
      índice ausente → o chamador mantém na lista. Fail-safe pro lado seguro.

    Desligada por default no endpoint (`settings.meu_dia_ia_duvida`): a camada
    1 já resolve o grosso e a tela abre toda manhã.
    """
    if not textos:
        return {}
    amostra = textos[:limite]
    if provider is None:
        from app.config import get_classifier_provider
        provider = get_classifier_provider()

    linhas = "\n".join(f"{i}: {t[:200]}" for i, t in enumerate(amostra))
    try:
        import json

        cru, _uso = provider.complete(system=_SYSTEM_IA, user=linhas)
        dados = json.loads(cru) if isinstance(cru, str) else cru
        itens = dados.get("itens") or []
    except Exception:
        log.warning("pede_resposta_ia_falhou", qtde=len(amostra), exc_info=True)
        return {}

    out: dict[int, bool] = {}
    for item in itens:
        try:
            i = int(item["i"])
            if 0 <= i < len(amostra):
                out[i] = bool(item["pede"])
        except (KeyError, TypeError, ValueError):
            continue  # item torto: some do mapa → chamador mantém na lista
    return out
