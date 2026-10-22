"""Classe da resposta do lead ao remarketing — regra em código, sem IA.

Decisão da Dona (05/10/2026): o que o lead responde ao disparo do carrossel
decide a etapa do card, e quem decide é uma função, não um agente.

    interesse   → Respondeu (e o handoff pra corretora segue como já era)
    botao       → Respondeu (clicou no botão do template; robô não clica)
    recusa      → Venda perdida ("não quero", "já comprei", "não tenho interesse")
    parar       → opt-out gravado + Venda perdida (texto ou botão "Parar mensagens")
    automatica  → fica em Disparado e fora da taxa: é o WhatsApp do lead
                  respondendo sozinho (saudação de empresa, ausência)
    indefinida  → Respondeu, como sempre foi ("bom dia", "?", áudio, figurinha).
                  O destino destas é decisão da Dona; até ela decidir, nada muda.

De onde sai cada veredito — este módulo ordena os detectores que a casa já
tem, pra as portas concordarem sobre o que é o quê. A única regex daqui é o
"não quero" (ver `_nao_quero`), que nenhum deles lia:

    parar       `opt_out.is_opt_out` + `atendimento_corretor._pediu_parar`
    botao       `cadencia.e_clique_de_botao`
    automatica  tempo (corte em segundos, `config_operacional`) ou
                `cadencia._e_auto_reply_conteudo`
    recusa      `atendimento_corretor._e_recusa_branda`, `cadencia._ja_encontrou`,
                `saida_remarketing._recusou`, e o "não quero" daqui
    interesse   `handoff_remarketing.avaliar` — o MESMO critério do handoff,
                de propósito: o que conta na taxa é o que vira card de corretora

A ORDEM É A REGRA (medida nas 395 respostas reais de 30/07 a 09/09/2026, em
`projetos/crm/docs/2026-10-05-respostas-remarketing/`):

  1. PARAR ganha de tudo, inclusive do relógio. Pedido de saída ignorado é o
     erro caro; honrar um "pare" que veio de robô não custa nada.
  2. BOTÃO ganha do relógio: o clique mais rápido medido foi 16 s, mas a regra
     não depende disso — secretária eletrônica não aperta botão.
  3. RELÓGIO: resposta antes do corte é automática, seja qual for o texto. Nas
     395, as 16 que chegaram em menos de 5 s eram todas saudação de empresa; a
     pessoa mais rápida levou 10,5 s.
  4. Mídia sem texto, reação e figurinha: indefinida. Não dá pra ler intenção
     de um anexo.
  5. CONTEÚDO de automática perde pra recusa: o detector casa "no momento não",
     que é a forma mais comum de gente dizer não (24 casos reais). Mesma
     exceção que `saida_remarketing.classificar` já faz.
  6. O resto é o critério do handoff aplicado à fala. Recusa e interesse na
     MESMA frase ("não tenho interesse", "não quero mais apartamento") é
     recusa — menos quando a pessoa quer VENDER, que é captação e segue pro
     handoff como já seguia. Recusa numa mensagem e interesse em outra (caso
     Renato, a permuta) se resolve em `carrossel_corretor.processar_inbound`, que
     reabre o card.

Puro: sem banco, sem rede, sem relógio. Quem chama entrega os segundos.

## Ampliação de 05/10/2026 (decisão da Dona: "pode ampliar sim")

Das 183 primeiras respostas que ficavam `indefinida`, três grupos tinham
leitura óbvia e um quarto (número errado) tinha destino decidido:

    negativa curta   "Não", "Não estou", "Não mais"        → recusa
    já resolveu      "Já compramos", "Achei", "Comprei um" → recusa
    pedido não lido  "Olá, estou sim", "manda por favor"   → interesse
    número errado    "esse contato não é mais da Fulano"    → numero_errado
                     (Venda perdida + telefone marcado)

DUAS TRAVAS de desenho, pra a ampliação não poder piorar o que já funcionava:

1. **Só refinam o que hoje é `indefinida`.** Os detectores novos rodam no FIM,
   depois de todos os da casa terem dito "sem sinal". Uma frase que hoje é
   interesse, recusa, parar ou automática continua sendo exatamente o que era
   — nenhum detector compartilhado (`_tem_interesse`, `_e_recusa_branda`,
   `handoff_remarketing.avaliar`) foi alterado, então a Pessoa e o handoff
   decidem como decidiam.
2. **O que depende da pergunta só vale como RESPOSTA DIRETA.** "Não", "Achei"
   e "estou sim" só querem dizer alguma coisa como resposta ao "ainda está
   procurando?". `resposta_direta=True` é quem chama dizendo: é a primeira
   mensagem do lead depois do disparo e ninguém falou com ele no meio
   (`carrossel_corretor._e_resposta_direta`). No meio de uma conversa com a Pessoa
   ou com corretora, "não" continua `indefinida`.

Medição contra as respostas reais e contra o corpo de mensagens de todas as
linhas: `projetos/crm/docs/2026-10-05-garantia-remarketing/DETECTORES.md`.
"""
from __future__ import annotations

import re

from app.services import handoff_remarketing as hr
from app.services import saida_remarketing as sr
from app.services.atendimento_corretor import (
    _e_recusa_branda,
    _pediu_parar,
    _quer_vender,
    _tem_interesse,
)
from app.services.cadencia import (
    _e_auto_reply_conteudo,
    _ja_encontrou,
    e_clique_de_botao,
)
from app.services.opt_out import _normalizar, is_opt_out

INTERESSE = "interesse"
BOTAO = "botao"
RECUSA = "recusa"
PARAR = "parar"
AUTOMATICA = "automatica"
INDEFINIDA = "indefinida"
# O telefone é de outra pessoa (05/10/2026). Vai pra Venda perdida como a
# recusa, e o telefone fica marcado: não é "não quero", é "não sou eu".
NUMERO_ERRADO = "numero_errado"

CLASSES = (INTERESSE, BOTAO, RECUSA, PARAR, AUTOMATICA, INDEFINIDA, NUMERO_ERRADO)

# O que entra na taxa de resposta da tela e do teste A/B (Dona 05/10).
CLASSES_QUE_CONTAM = (INTERESSE, BOTAO)
# Pra onde o card vai. Automática não está aqui: não sai de Disparado.
CLASSES_RESPONDEU = (INTERESSE, BOTAO, INDEFINIDA)
CLASSES_VENDA_PERDIDA = (RECUSA, PARAR, NUMERO_ERRADO)

# Qual regra decidiu — vai no evento, pra dar pra auditar depois.
REGRA_PARAR = "parar"
REGRA_BOTAO = "botao"
REGRA_RELOGIO = "relogio"
REGRA_MIDIA = "midia_ou_vazio"
REGRA_AUTO_CONTEUDO = "automatica_pelo_texto"
REGRA_HANDOFF = "criterio_do_handoff"
REGRA_RECUSA = "recusa"
REGRA_SEM_SINAL = "sem_sinal"
REGRA_NUMERO_ERRADO = "numero_errado"
REGRA_JA_RESOLVEU = "ja_resolveu"
REGRA_NEGATIVA_CURTA = "negativa_curta"
REGRA_PEDIDO = "pedido_nao_lido"
REGRA_SIM_DIRETO = "sim_direto"
# As regras da ampliação de 05/10 — só refinam o que era `indefinida`.
REGRAS_AMPLIADAS = (REGRA_NUMERO_ERRADO, REGRA_JA_RESOLVEU, REGRA_NEGATIVA_CURTA,
                    REGRA_PEDIDO, REGRA_SIM_DIRETO)

# Padrão do corte. O valor que vale em produção é o de `config_operacional`
# (`carrossel_auto_resposta_segundos`); este é o default da função pura.
CORTE_PADRAO_S = 5.0


# "NÃO QUERO" — o primeiro exemplo de recusa da Dona, e nenhum detector da casa
# o lia sozinho: a recusa branda exige um verbo depois ("não quero comprar"), o
# opt-out exige "mais"/"nada", e o detector de INTERESSE casa a palavra "quero".
# Resultado: "Não quero" virava interesse e ia pra fila da corretora. (Furo
# apontado pelo parecer da Astra em 05/10/2026; nas 395 respostas reais ele não
# aparece, mas é a frase que a regra existe pra tratar.)
#
# Não é recusa quando o resto da frase diz o que a pessoa QUER: "não quero
# apartamento, quero casa" é preferência, e preferência é lead.
_RE_NAO_QUERO = re.compile(r"\bn[ãa]o\s+quero\b", re.IGNORECASE)


def _nao_quero(texto: str) -> bool:
    if not _RE_NAO_QUERO.search(texto):
        return False
    resto = _RE_NAO_QUERO.sub(" ", texto)
    return not (_tem_interesse(resto) or _quer_vender(resto))


def _recusou(texto: str) -> bool:
    return (_e_recusa_branda(texto) or _ja_encontrou(texto)
            or sr._recusou(texto) or _nao_quero(texto))


# ── Ampliação de 05/10/2026 ──────────────────────────────────────────────────
# Tudo daqui opera no texto NORMALIZADO (`opt_out._normalizar`: minúsculas, sem
# acento, pontuação virando espaço) e só é consultado quando os detectores da
# casa disseram "sem sinal".

_SAUDACAO = frozenset({"ola", "oi", "oie", "opa", "bom", "boa", "dia", "tarde", "noite"})
_NAO = frozenset({"nao", "n", "nn", "naum"})
_AGRADECE = frozenset({"obrigado", "obrigada", "obg", "obgd", "brigado", "brigada",
                       "valeu", "vlw", "grato", "grata"})

# NÚMERO ERRADO — a pessoa diz que o telefone não é de quem a gente procura.
#
# Duas forças, medidas no corpo de 37 mil mensagens (05/10/2026):
#
# FORTE — fala do TELEFONE, não depende da pergunta: vale em qualquer mensagem.
#   "esse contato não é mais da Fulano", "este fone é do Fulano",
#   "te passou o número errado", "mandou msg para pessoa errada".
# FRACA — só quer dizer "não sou eu" como resposta direta ao disparo:
#   "Não conheço Marcelo", "foi engano", "mensagem errada". No meio de uma
#   conversa são outra coisa — "não conheço os bairros" é gente interessada
#   que acabou de chegar na cidade, "foi engano" é a pessoa se desculpando da
#   própria mensagem. A primeira versão desta regra casava as duas; a medição
#   pegou.
_RE_NUMERO_ERRADO = re.compile(
    r"\b(?:numero|contato|telefone|fone|celular|whats\w*|zap)\b.{0,30}"
    r"\bnao e (?:mais )?(?:d[aeo]|meu|minha)\b"
    r"|\b(?:este|esse) (?:numero|contato|telefone|fone|celular|whats\w*|zap) "
    r"(?:agora )?e (?:d[aeo]|outro|de outra)\b"
    r"|\b(?:numero|contato|telefone) errad[oa]\b"
    r"|\b(?:para|pra|pro|a) (?:a |uma |o )?pessoa errada\b"
)
_RE_ENGANO = re.compile(r"\b(?:foi|e|deve ser) engano\b|\bmensagem errada\b|\bpessoa errada\b")
_RE_NAO_CONHECO = re.compile(r"^(?:eu )?(?:nao )?nao conheco(?: [a-z]+){0,2}$")
# "Não conheço…" seguido disto é assunto de IMÓVEL, não de pessoa.
_NAO_CONHECO_E_LUGAR = frozenset({
    "o", "a", "os", "as", "um", "uma", "muito", "nada", "aqui", "ai", "la",
    "bem", "ainda", "esse", "essa", "este", "esta", "isso", "bairro", "bairros",
    "cidade", "cidade", "regiao", "lugar", "area", "rua", "predio", "imovel",
    "condominio", "empreendimento", "voces", "vcs", "vc", "voce", "mais",
})

# JÁ RESOLVEU, dito de um jeito que `_e_recusa_branda` e `_ja_encontrou` não
# leem: o plural ("já compramos") e o "comprei" sem o "já". Só como RESPOSTA
# DIRETA: no meio de uma conversa "comprei em Londrina" é a pessoa contando do
# imóvel que tem, não encerrando a busca.
_RE_JA_RESOLVEU = re.compile(
    r"\bja (?:compramos|adquirimos|fechamos|alugamos|achamos|encontramos|"
    r"conseguimos|resolvemos|mudamos|estamos morando|estou morando)\b"
    r"|\bcomprei (?:um|uma|outro|outra|ja|faz|ha|aqui|la|em|no|na)\b"
    r"|\bcompramos (?:um|uma|outro|outra|ja|faz|ha|aqui|la|em|no|na)\b"
)
# Sozinho ("Achei", "Comprei, obrigada").
_JA_RESOLVEU_SECO = frozenset({"achei", "comprei", "compramos", "encontrei",
                               "encontramos", "consegui", "conseguimos",
                               "resolvi", "resolvemos", "aluguei", "alugamos"})
_MAX_PALAVRAS_JA_RESOLVEU = 12

_MAX_PALAVRAS_CURTA = 6
_MAX_PALAVRAS_PEDIDO = 8


def _palavras(texto: str) -> list[str]:
    return _normalizar(texto).split()


def _sem_saudacao(palavras: list[str]) -> list[str]:
    i = 0
    while i < len(palavras) and palavras[i] in _SAUDACAO:
        i += 1
    return palavras[i:]


def _numero_errado(texto: str, resposta_direta: bool) -> bool:
    norm = _normalizar(texto)
    # Pergunta não é afirmação: "Esse número é da Pessoa1?" é a pessoa
    # conferindo com quem fala.
    if _RE_NUMERO_ERRADO.search(norm) and not texto.rstrip().endswith("?"):
        return True
    if not resposta_direta:
        return False
    resto = [w for w in _sem_saudacao(norm.split()) if w not in _AGRADECE]
    if len(resto) > _MAX_PALAVRAS_PEDIDO:
        return False
    if _RE_ENGANO.search(" ".join(resto)):
        return True
    frase = " ".join(resto)
    return bool(_RE_NAO_CONHECO.match(frase)) and not (set(resto) & _NAO_CONHECO_E_LUGAR)


def _ja_resolveu(texto: str) -> bool:
    """Só chamada pra resposta direta (ver `_refinar_indefinida`)."""
    palavras = _palavras(texto)
    if not palavras or len(palavras) > _MAX_PALAVRAS_JA_RESOLVEU:
        return False
    if _RE_JA_RESOLVEU.search(" ".join(palavras)):
        return True
    resto = [w for w in _sem_saudacao(palavras)
             if w not in _AGRADECE and w not in ("ja", "eu", "mas")]
    return len(resto) == 1 and resto[0] in _JA_RESOLVEU_SECO


# O "não" seco. Vocabulário FECHADO de propósito: a mensagem inteira precisa
# ser feita só destas palavras. "Não estou bem", "ainda não" (ainda não achei?
# ainda não quero?) e "não sei" ficam de fora — na dúvida, indefinida.
_VOCAB_NEGATIVA = _NAO | _AGRADECE | _SAUDACAO | frozenset({
    "eu", "estou", "estamos", "to", "tou", "mais", "mas", "nada",
})


def _negativa_curta(texto: str) -> bool:
    palavras = _palavras(texto)
    if not palavras or len(palavras) > _MAX_PALAVRAS_CURTA:
        return False
    if not any(w in _NAO for w in palavras):
        return False
    # "Não, mas…" é frase que continua na mensagem seguinte.
    if palavras[-1] == "mas":
        return False
    return all(w in _VOCAB_NEGATIVA for w in palavras)


# PEDIDO que `_tem_interesse` não lê: ele conhece "pode mandar"; não conhece
# "manda", "favor mandar", "me envia". Tem que ser PEDIDO — o verbo abre a
# frase (depois do cumprimento e da gentileza). "Vou mandar", "ele manda hoje"
# e "quer enviar o contato?" têm o verbo e não pedem nada: a primeira versão
# desta regra casava os três, e a medição pegou.
_VERBOS_DE_PEDIDO = frozenset({"manda", "mande", "mandar", "envia", "envie", "enviar"})
_ANTES_DO_PEDIDO = frozenset({"por", "favor", "pfv", "pf", "pode", "poderia", "podia",
                              "consegue", "me", "entao", "sim", "ok", "claro"})


def _pedido(texto: str) -> bool:
    palavras = _sem_saudacao(_palavras(texto))
    if not palavras or len(palavras) > _MAX_PALAVRAS_PEDIDO:
        return False
    if any(w in _NAO or w in ("nunca", "nem", "para", "pare", "parem") for w in palavras):
        return False
    for w in palavras:
        if w in _VERBOS_DE_PEDIDO:
            return True
        if w not in _ANTES_DO_PEDIDO:
            return False
    return False


_RE_SIM_ESTICADO = re.compile(r"^s+i+m+$")
# O "sim" que o handoff não lê porque a frase começa pelo cumprimento: "Olá,
# estou sim", "Oi boa tarde pode sim", "Siimmm". Vocabulário FECHADO, como a
# negativa: "tudo bem sim", "ah sim" e "estou bem e você?" respondem o
# cumprimento, não a pergunta do disparo — e ficam de fora.
_VOCAB_SIM = _AGRADECE | frozenset({
    "sim", "estou", "estamos", "ainda", "quero", "queremos", "tenho", "temos",
    "interesse", "pode", "claro", "gostaria", "aceito", "desejo", "com",
    "certeza", "por", "favor", "eu", "agradeco", "procurando", "buscando",
})
_NUCLEO_SIM = frozenset({
    "sim", "estou", "estamos", "quero", "queremos", "tenho", "temos", "pode",
    "claro", "gostaria", "aceito", "desejo", "certeza",
})


def _sim_direto(texto: str) -> bool:
    palavras = ["sim" if _RE_SIM_ESTICADO.match(w) else w
                for w in _sem_saudacao(_palavras(texto))]
    if not palavras or len(palavras) > _MAX_PALAVRAS_CURTA:
        return False
    return (all(w in _VOCAB_SIM for w in palavras)
            and any(w in _NUCLEO_SIM for w in palavras))


def _refinar_indefinida(t: str, resposta_direta: bool) -> tuple[str, str]:
    """A ampliação de 05/10. Só é chamada quando o resto disse "sem sinal".

    Tudo que manda o card pra Venda perdida só vale como RESPOSTA DIRETA ao
    disparo — menos o número errado dito com todas as letras, que fala do
    telefone e vale sempre.
    """
    if _numero_errado(t, resposta_direta):
        return NUMERO_ERRADO, REGRA_NUMERO_ERRADO
    if _pedido(t):
        return INTERESSE, REGRA_PEDIDO
    if resposta_direta:
        if _ja_resolveu(t):
            return RECUSA, REGRA_JA_RESOLVEU
        if _negativa_curta(t):
            return RECUSA, REGRA_NEGATIVA_CURTA
        if _sim_direto(t):
            return INTERESSE, REGRA_SIM_DIRETO
    return INDEFINIDA, REGRA_SEM_SINAL


def interesse_explicito(texto: str | None) -> bool:
    """A fala diz o que a pessoa quer (imóvel, lugar, preço, vender) — e não só
    "ok", "claro", "pode deixar".

    É o que REABRE um card que acabou de ir pra Venda perdida por recusa (caso
    Renato: "já comprei, obrigado" e, na mensagem seguinte, a permuta). A
    afirmativa curta não reabre: medido nas conversas reais, de 20 recusas
    seguidas de uma fala "com sinal", 17 eram só educação respondendo o pedido
    de indicação ("Ok", "Tá bom", "Claro, pode deixar"). Mesma régua que
    `atendimento_corretor.atender` usa pra desfazer a recusa.
    """
    t = (texto or "").strip()
    if not t or _recusou(t):
        return False
    return _quer_vender(t) or _tem_interesse(t)


def classificar_resposta(
    texto: str | None,
    tipo: str | None,
    segundos_desde_disparo: float | None,
    *,
    corte_segundos: float = CORTE_PADRAO_S,
    resposta_direta: bool = False,
) -> str:
    """Classe de UMA mensagem do lead depois do disparo.

    `tipo` é o tipo da mensagem na Cloud API ("text", "button", "audio"...).
    `segundos_desde_disparo` é None quando não se sabe (sem disparo registrado):
    aí o relógio não opina. `corte_segundos <= 0` desliga a regra do relógio.
    `resposta_direta`: é a primeira mensagem do lead depois do disparo, sem
    ninguém ter falado com ele no meio — só aí "Não" e "estou sim" são lidos.
    """
    return classificar_com_regra(
        texto, tipo, segundos_desde_disparo,
        corte_segundos=corte_segundos, resposta_direta=resposta_direta,
    )[0]


def classificar_com_regra(
    texto: str | None,
    tipo: str | None,
    segundos_desde_disparo: float | None,
    *,
    corte_segundos: float = CORTE_PADRAO_S,
    resposta_direta: bool = False,
) -> tuple[str, str]:
    """`(classe, regra)` — a classe e QUAL regra decidiu. A regra vai gravada
    no evento da resposta: daqui a um mês dá pra dizer por que aquele card foi
    pra Venda perdida sem reexecutar nada."""
    t = (texto or "").strip()
    tp = (tipo or "text").strip().lower()

    if is_opt_out(t) or _pediu_parar(t):
        return PARAR, REGRA_PARAR
    if e_clique_de_botao(tp):
        return BOTAO, REGRA_BOTAO
    if (
        segundos_desde_disparo is not None
        and corte_segundos > 0
        and segundos_desde_disparo < corte_segundos
    ):
        return AUTOMATICA, REGRA_RELOGIO
    if tp in sr._TIPOS_IGNORADOS or not t or hr._e_so_midia(t):
        return INDEFINIDA, REGRA_MIDIA

    recusou = _recusou(t)
    if _e_auto_reply_conteudo(t) and not recusou:
        return AUTOMATICA, REGRA_AUTO_CONTEUDO

    decisao = hr.avaliar([t])
    if decisao.transferir:
        # Disse que não com palavras que a régua do handoff não conhece ("não
        # quero", "não vou querer", "não estou mais", "deixa pra lá") — mesmo
        # veto da porta de saída. Quem quer VENDER passa: é captação.
        if recusou and decisao.sinal != hr.SINAL_QUER_VENDER:
            return RECUSA, REGRA_RECUSA
        return INTERESSE, REGRA_HANDOFF
    if decisao.motivo == "pediu_parar":
        return PARAR, REGRA_PARAR
    if decisao.motivo == "recusa" or recusou:
        return RECUSA, REGRA_RECUSA
    # Daqui pra baixo a regra de até 05/10 respondia `indefinida`.
    return _refinar_indefinida(t, resposta_direta)
