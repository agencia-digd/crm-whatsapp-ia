"""
Variantes de copy do carrossel da Corretor — teste A/B com medição.

Dona 30/07: "varia as cópias pra gente saber qual copy está melhor", e uma das
variantes sai em MARKETING pra comparar categoria (não só texto).

Como funciona: cada lead cai num BALDE fixo, decidido pelo hash do id dele. Não
é sorteio a cada envio — é bucketing determinístico, e isso importa por três
motivos:

  1. A divisão sai exata. Sorteio com 50 envios/dia dá desvio grande; hash com
     peso dá a proporção pedida já nos primeiros dias.
  2. O mesmo lead, se voltar pra fila daqui a 30 dias, cai na MESMA variante
     (o balde é dele, não do envio). Sem isso, a segunda rodada contaminaria o
     resultado da primeira.
  3. Dá pra auditar depois: sabendo o id do lead, dá pra recalcular em qual
     variante ele estava — sem depender do que foi gravado.

⚠️ Mexer nos PESOS remexe os baldes: um lead pode mudar de variante entre
rodadas. Pra teste em andamento, prefira ligar/desligar variantes a re-partir
os pesos no meio.

⚠️ Variante MARKETING só sai se DUAS coisas forem verdade: a cota do dia
(`cota_marketing`, opt-in via DISPARO_COTA_MARKETING_PCT) e existir template
MARKETING aprovado com o prefixo da linha. Faltando qualquer uma, cai em UTILITY
— e o que foi ENVIADO de verdade é o que conta no relatório, nunca a intenção.
"""
from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass, replace
from typing import Any

import structlog

log = structlog.get_logger()


@dataclass(frozen=True)
class Variante:
    id: str
    texto: str
    peso: int                 # fatia em % — a soma das ativas tem que dar 100
    marketing: bool = False
    ativa: bool = True
    # Id da variante cujo TEXTO esta aqui acompanha (o teste de categoria).
    # Enquanto o espelho estiver ligado (`carrossel_marketing_espelha_direta`),
    # o texto que sai é o da outra, editado ou não.
    espelha: str | None = None


# ── As variantes em teste ────────────────────────────────────────────────────
# Regra pra escrever variante nova: muda UMA coisa por vez. Se A e B diferem em
# abertura E oferta E CTA, o resultado não diz qual das três mexeu o ponteiro.
# Texto da campeã. Fica em constante porque DUAS variantes usam ele: a utility
# e a de marketing. Se cada uma tivesse a sua cópia, bastava alguém ajustar uma
# palavra num lado pro teste de categoria virar teste de texto sem ninguém
# perceber — e aí o resultado não diz mais nada.
TEXTO_DIRETA = (
    "Aqui é da Acme Imóveis. Passando pra saber se você ainda está "
    "procurando imóvel em Cidade — apareceram opções novas essa "
    "semana. Quer que eu te mande?"
)

VARIANTES: tuple[Variante, ...] = (
    Variante(
        id="direta",
        peso=50,
        texto=TEXTO_DIRETA,
    ),
    Variante(
        id="pergunta",
        peso=0,
        ativa=False,
        # APOSENTADA em 04/08/2026 com 30 dias de teste: 8,2% (6/73) contra
        # 13,1% (11/84) da "direta". Perguntar o estado da pessoa ("ainda está
        # nessa busca ou já resolveu?") pede que ela admita ter desistido — e
        # quem desistiu não responde. Fica registrada, não apagada: o resultado
        # dela é o que justifica a variante que entrou no lugar.
        # Muda só o ENQUADRAMENTO: em vez de anunciar novidade, pergunta o
        # estado da pessoa. Pede menos e costuma puxar mais resposta.
        texto=(
            "Aqui é da Acme Imóveis. Você chegou a procurar imóvel com a "
            "gente em Cidade — ainda está nessa busca ou já resolveu?"
        ),
    ),
    Variante(
        id="brendon_triagem",
        peso=0,
        ativa=False,
        # APOSENTADA em 06/09/2026 a pedido da Dona: a direta ganhou e a
        # triagem morar/investir não vale o volume. 30 dias medidos:
        # direta 9,4% (158/1677) · brendon 6,0% (100/1673) ·
        # direta_marketing 7,9% (68/866). 7 dias: 11,0% vs 4,7%.
        # Fica registrada. Quem já estava no balde dela, na próxima
        # rodada cai em direta/curta — mexer peso remexe balde, é o
        # preço de encerrar o teste.
        texto=(
            "Aqui é da Acme Imóveis. Você chegou a procurar imóvel com a "
            "gente em Cidade e queria te perguntar uma coisa rápida: era pra "
            "morar ou pra investir?"
        ),
    ),
    Variante(
        id="curta",
        peso=40,
        # Entrou em 06/09/2026 no lugar da brendon_triagem. É VARIAÇÃO da
        # direta campeã (9,4% em 30d), não um texto novo: mesma abertura,
        # mesmo "ainda está procurando", mesmo CTA. A UMA coisa que muda é
        # a novidade — de "opções novas" (genérico) pra "lançamento e
        # pronto" (concreto, sem inventar unidade). Hipótese: o que fez a
        # direta ganhar foi OFERECER; oferecer com mais carne responde mais.
        texto=(
            "Aqui é da Acme Imóveis. Passando pra saber se você ainda está "
            "procurando imóvel em Cidade — essa semana entrou lançamento "
            "e pronto. Quer que eu te mande?"
        ),
    ),
    Variante(
        id="direta_marketing",
        peso=10,
        marketing=True,
        espelha="direta",
        # MESMO TEXTO da "direta" — de propósito (decisão da Dona, 04/08/2026).
        #
        # A variante anterior (`marketing_novidade`) mudava texto E categoria ao
        # mesmo tempo, e fechou 30 dias em 6,8% contra 13,1% da direta. Só que
        # esse número não respondia a pergunta que importa: perdeu por ser
        # MARKETING (que custa ~10x mais) ou porque a copy era pior? Não dava
        # pra separar.
        #
        # Com o texto idêntico, a única diferença entre as duas é a categoria —
        # e aí a comparação mede exatamente o que a gente quer saber: vale a
        # pena pagar marketing?
        #
        # Cota 10% (era 20%) — Dona 06/09, remarketing da Corretor. Mix de
        # categoria, não performance: a direta em UTILITY continua na frente
        # (9,4% vs 7,9% em 30d) e mesmo assim a fatia de marketing não zera.
        texto=TEXTO_DIRETA,
    ),
)


# ── Pesos editáveis pelo painel ──────────────────────────────────────────────
#
# A tela mexe na FATIA de cada variante — a pergunta operacional "quanto do
# volume vai pra cada braço do teste" — e, desde 05/10/2026, também no TEXTO
# (ver "Texto editável pelo painel", no fim do arquivo).
#
# O override é uma chave em `config_operacional` (`carrossel_pesos`): um mapa
# id → peso. Chave ausente = vale o peso escrito acima, byte a byte como antes.
#
# ⚠️ DUAS TRAVAS, e elas são de OPERAÇÃO, não de estilo:
#   1. a soma das ativas tem que dar 100 — senão o bucketing distribui em cima
#      de outro total e a fatia real deixa de ser a pedida;
#   2. a fatia em MARKETING fica em COTA_MARKETING_PCT. É mix de categoria
#      (regra da casa), não performance: em 04/08 a variante marketing era a
#      segunda pior do teste e mesmo assim a cota vale. `test_carrossel_copies`
#      trava isso no código — se a tela pudesse gravar outro %, o teste
#      continuaria verde e a operação sairia do combinado sem ninguém ver.
CHAVE_PESOS = "carrossel_pesos"
COTA_MARKETING_PCT = 10


def pesos_padrao() -> dict[str, int]:
    """Os pesos escritos no código — o que o painel chama de 'padrão'."""
    return {v.id: v.peso for v in VARIANTES}


def pesos_efetivos() -> dict[str, int]:
    """Peso de cada variante hoje: o do painel se houver, senão o do código."""
    try:
        from app.services import config_operacional

        override = config_operacional.get(CHAVE_PESOS) or {}
    except Exception as e:  # noqa: BLE001 — banco fora não pode parar o disparo
        log.warning("carrossel_pesos_override_indisponivel", erro=str(e)[:140])
        override = {}
    out = pesos_padrao()
    for vid, peso in dict(override).items():
        if vid in out:
            try:
                out[vid] = int(peso)
            except (TypeError, ValueError):
                log.warning("carrossel_peso_invalido_ignorado", variante=vid)
    return out


def problema_nos_pesos() -> str | None:
    """O que está gravado em `carrossel_pesos` serve? None = serve (ou não há
    nada gravado e valem os pesos do código).

    É a leitura ESTRITA, a que o disparo faz antes de cada mensagem: levanta
    `config_operacional.ConfigIndisponivel` se não der pra ler o banco, e
    devolve o motivo quando o valor gravado não passa na validação da tela
    (alguém escreveu por fora). `pesos_efetivos`, que a tela usa pra desenhar,
    é tolerante de propósito — ignora o que não entende; o motor não pode ser.
    """
    from app.services import config_operacional

    override = config_operacional.get(CHAVE_PESOS)
    if not override:
        return None
    try:
        validar_pesos(override)
    except ValueError as e:
        return str(e)
    return None


def variantes_efetivas() -> tuple[Variante, ...]:
    """Todas as variantes com o peso que vale AGORA (painel > código)."""
    pesos = pesos_efetivos()
    return tuple(replace(v, peso=pesos.get(v.id, v.peso)) for v in VARIANTES)


def validar_pesos(pesos: dict[str, Any]) -> dict[str, int]:
    """Normaliza e valida um mapa id → peso vindo da tela.

    Levanta `ValueError` com a explicação em português — quem chama traduz pro
    HTTP. Sai um mapa completo (toda variante conhecida), pra o que foi gravado
    ser legível sem consultar o código.
    """
    if not isinstance(pesos, dict):
        raise ValueError("esperado um mapa variante → peso")

    conhecidas = {v.id: v for v in VARIANTES}
    for vid in pesos:
        if vid not in conhecidas:
            raise ValueError(f"variante desconhecida: {vid!r}")

    out = pesos_padrao()
    for vid, bruto in pesos.items():
        if isinstance(bruto, bool) or not isinstance(bruto, int):
            raise ValueError(f"peso de {vid!r} precisa ser número inteiro")
        if not (0 <= bruto <= 100):
            raise ValueError(f"peso de {vid!r} fora da faixa (0 a 100)")
        out[vid] = bruto

    # Variante aposentada não volta pela tela: ela foi desligada com um
    # resultado documentado no código, e reativar sem esse contexto é reabrir
    # um teste que já terminou.
    for vid, v in conhecidas.items():
        if not v.ativa and out[vid] > 0:
            raise ValueError(
                f"{vid!r} está aposentada no código — pra reativar, mexe no código"
            )

    soma = sum(out[vid] for vid, v in conhecidas.items() if v.ativa)
    if soma != 100:
        raise ValueError(f"os pesos das variantes ativas somam {soma}, não 100")

    mkt = sum(out[vid] for vid, v in conhecidas.items() if v.ativa and v.marketing)
    if mkt != COTA_MARKETING_PCT:
        raise ValueError(
            f"a fatia em MARKETING tem que ficar em {COTA_MARKETING_PCT}% "
            f"(veio {mkt}%) — é mix de categoria, regra da casa, não performance"
        )
    return out


def ativas() -> tuple[Variante, ...]:
    return tuple(v for v in variantes_efetivas() if v.ativa and v.peso > 0)


def escolher(lead_id: int | str) -> Variante:
    """Balde do lead. Determinístico: mesmo lead → mesma variante, sempre."""
    vs = ativas()
    if not vs:
        raise RuntimeError("nenhuma variante de copy ativa no carrossel")
    total = sum(v.peso for v in vs)
    # md5 do id (não `hash()`: o hash do Python é aleatorizado por processo e
    # a variante mudaria a cada restart do serviço).
    h = int(hashlib.md5(str(lead_id).encode()).hexdigest()[:8], 16)
    ponto = h % total
    acum = 0
    for v in vs:
        acum += v.peso
        if ponto < acum:
            return v
    return vs[-1]


def por_id(vid: str | None) -> Variante | None:
    """Variante pelo id, já com o peso EFETIVO — o placar mostra o peso que
    está valendo, não o que está escrito no arquivo."""
    return next((v for v in variantes_efetivas() if v.id == vid), None)


def conferir_pesos() -> list[str]:
    """Problemas de configuração. Vazio = tudo certo."""
    problemas = []
    vs = ativas()
    if not vs:
        problemas.append("nenhuma variante ativa")
        return problemas
    soma = sum(v.peso for v in vs)
    if soma != 100:
        problemas.append(f"pesos das ativas somam {soma}, não 100")
    ids = [v.id for v in VARIANTES]
    if len(set(ids)) != len(ids):
        problemas.append("id de variante repetido")
    return problemas


# ── Texto editável pelo painel (05/10/2026) ──────────────────────────────────
#
# Pedido da Dona: "lá eu queria também poder mudar a copy". O texto de cada
# variante ATIVA passa a morar no banco, com o do código como padrão:
#
#   carrossel_copies                     {"direta": "texto novo", ...}
#   carrossel_marketing_espelha_direta   true  (padrão)
#
# Chave ausente (ou variante fora do mapa) = o texto escrito acima, byte a byte.
#
# TRÊS coisas que este bloco garante:
#
#   1. O que o servidor recusaria nunca é gravado (`validar_texto`), e o que
#      chegou no banco por fora da tela SEGURA o disparo (`problema_nas_copies`,
#      lida antes de cada mensagem). Nunca cai calado no texto do código: quem
#      editou a copy acharia que ela está saindo.
#   2. A medição não mistura dois textos: cada disparo grava a `copy_versao`
#      (um hash do texto que saiu) e o placar da variante conta só a versão que
#      vale agora. Voltar ao texto antigo traz os números dele de volta.
#   3. O teste de CATEGORIA continua de pé por padrão: `direta_marketing` segue
#      o texto da `direta` enquanto o espelho estiver ligado. Desligar é um ato
#      de quem está na tela, com aviso — e aí as duas viram copies separadas.
CHAVE_COPIES = "carrossel_copies"
CHAVE_ESPELHO = "carrossel_marketing_espelha_direta"

# O texto viaja na variável {{2}} do casco `acme_corretor_ola_*` (UTILITY já
# aprovado). A Meta aceita 1.024 caracteres no CORPO inteiro, depois de
# preenchido: casco fixo (até 62, medido em 05/10 nos 6 cascos da linha) +
# o nome da pessoa no {{1}} + este texto. 900 deixa 60 de folga pro nome — é o
# mesmo teto da boas-vindas (`boas_vindas_copies.TEXTO_MAX_CHARS`).
TEXTO_MAX_CHARS = 900


class CopyInvalida(ValueError):
    """A copy gravada não serve. O motor segura o disparo; a tela mostra."""


def canonizar(texto: str) -> str:
    """O texto do jeito que o envio vai mandar, com `\n` no lugar da quebra.

    É a MESMA limpeza de `cloud_corretora_a.normalizar_param_template` (a Meta
    recusa parâmetro com tab ou 4+ espaços seguidos): tab vira espaço, espaços
    repetidos viram um, no máximo uma linha em branco entre parágrafos. A
    diferença é só a quebra de linha, que lá vira U+000B na hora do envio e
    aqui fica `\n` — é o que a pessoa digitou e o que a prévia mostra.
    Gravar já canonizado faz a prévia ser igual ao que chega no celular.
    """
    import re

    t = (texto or "").replace("\r\n", "\n").replace("\r", "\n").replace("\v", "\n")
    t = t.replace("\t", " ")
    t = re.sub(r" {2,}", " ", t)
    t = re.sub(r" *\n *", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def validar_texto(texto: Any, *, variante: str | None = None) -> str:
    """Valida o texto de uma variante vindo da tela. `ValueError` em PT-BR.

    Devolve o texto canonizado (o que é gravado e o que sai).
    """
    de = f" de {variante!r}" if variante else ""
    if not isinstance(texto, str):
        raise ValueError(f"o texto{de} precisa ser texto")
    t = canonizar(texto)
    if not t:
        raise ValueError("o texto não pode ficar vazio")
    if len(t) > TEXTO_MAX_CHARS:
        raise ValueError(
            f"texto longo demais: {len(t)} caracteres, o máximo é {TEXTO_MAX_CHARS}"
        )
    # Aqui não existe placeholder: o nome da pessoa entra sozinho na abertura
    # do casco ("Olá Fulano, tudo bem?") e esta variável vai como está. Chave
    # digitada chegaria literal no WhatsApp ("Oi {nome}").
    if "{" in t or "}" in t:
        raise ValueError(
            "tira as chaves { } do texto: o nome da pessoa já entra sozinho na "
            "abertura (\"Olá Fulano, tudo bem?\")"
        )
    ruins = sorted({c for c in t if c != "\n" and unicodedata.category(c) in ("Cc", "Cf")
                    # ZWJ e seletor de variação fazem parte de emoji composto.
                    and c not in ("\u200d", "\ufe0f")})
    if ruins:
        raise ValueError(
            "o texto tem caractere invisível que a Meta recusa ("
            + ", ".join(f"U+{ord(c):04X}" for c in ruins)
            + "): apaga e digita de novo esse trecho"
        )
    return t


def versao_do_texto(texto: str) -> str:
    """Identidade curta do texto que saiu — vai no evento do disparo."""
    return hashlib.sha1(canonizar(texto).encode("utf-8")).hexdigest()[:10]


def texto_padrao(vid: str) -> str | None:
    """Texto do CÓDIGO da variante (o "voltar ao padrão" da tela)."""
    return next((v.texto for v in VARIANTES if v.id == vid), None)


def validar_copies(copies: Any) -> dict[str, str]:
    """Normaliza e valida o mapa variante → texto vindo da tela.

    Texto vazio ou `None` = sem override (a variante volta ao texto do código).
    Sai só o que é override de verdade: texto igual ao do código não é gravado,
    pra "personalizado" na tela querer dizer alguma coisa.
    """
    if not isinstance(copies, dict):
        raise ValueError("esperado um mapa variante → texto")
    conhecidas = {v.id: v for v in VARIANTES}
    out: dict[str, str] = {}
    for vid, texto in copies.items():
        v = conhecidas.get(vid)
        if v is None:
            raise ValueError(f"variante desconhecida: {vid!r}")
        if texto is None or (isinstance(texto, str) and not texto.strip()):
            continue
        if not v.ativa:
            raise ValueError(
                f"{vid!r} está aposentada no código: o texto dela não se edita"
            )
        t = validar_texto(texto, variante=vid)
        if t != canonizar(v.texto):
            out[str(vid)] = t
    return out


def validar_espelho(valor: Any) -> bool:
    if not isinstance(valor, bool):
        raise ValueError("o espelho da variante de marketing precisa ser true ou false")
    return valor


def _ler_copies_estrito() -> tuple[dict[str, str], bool]:
    """(overrides, espelho) do banco. Levanta `ConfigIndisponivel` se não deu
    pra ler e `CopyInvalida` se o que está gravado não passa na validação."""
    from app.services import config_operacional

    bruto = config_operacional.get(CHAVE_COPIES)
    espelho = config_operacional.get(CHAVE_ESPELHO)
    if not isinstance(espelho, bool):
        raise CopyInvalida(f"{CHAVE_ESPELHO}: esperado true ou false, veio {espelho!r}")
    if not bruto:
        return {}, espelho
    if not isinstance(bruto, dict):
        raise CopyInvalida(f"{CHAVE_COPIES}: esperado um mapa variante → texto")
    conhecidas = {v.id: v for v in VARIANTES}
    out: dict[str, str] = {}
    for vid, texto in bruto.items():
        if vid not in conhecidas:
            raise CopyInvalida(f"{CHAVE_COPIES}: variante desconhecida {vid!r}")
        try:
            limpo = validar_texto(texto, variante=vid)
        except ValueError as e:
            raise CopyInvalida(f"{CHAVE_COPIES}[{vid}]: {e}") from e
        if limpo != texto:
            # Gravado por fora sem passar pela limpeza: o que sairia não é o
            # que está escrito. Não adivinho qual dos dois a pessoa quis.
            raise CopyInvalida(
                f"{CHAVE_COPIES}[{vid}]: o texto gravado não está na forma em "
                "que é enviado (espaço ou quebra de linha a mais): regrave pela tela"
            )
        out[vid] = texto
    return out, espelho


def _texto_de(v: Variante, overrides: dict[str, str], espelho: bool) -> str:
    fonte = v.espelha if (v.espelha and espelho) else v.id
    return overrides.get(fonte) or (texto_padrao(fonte) or v.texto)


def problema_nas_copies() -> str | None:
    """O que está gravado em `carrossel_copies` serve? None = serve.

    Leitura ESTRITA, a que o disparo faz antes de cada mensagem — irmã de
    `problema_nos_pesos`. Levanta `config_operacional.ConfigIndisponivel` se
    não der pra ler o banco.
    """
    try:
        _ler_copies_estrito()
    except CopyInvalida as e:
        return str(e)
    return None


def copy_para_envio(v: Variante) -> tuple[str, str]:
    """(texto, versão) que sai AGORA pra esta variante. Estrito: levanta
    `CopyInvalida` ou `ConfigIndisponivel` — quem envia não manda nada."""
    overrides, espelho = _ler_copies_estrito()
    texto = _texto_de(v, overrides, espelho)
    return texto, versao_do_texto(texto)


def previa_no_celular(texto: str, nome_lead: str = "João") -> str:
    """A mensagem inteira como chega: casco do template + o texto. O casco é o
    `acme_corretor_ola_a_*` (o mais usado dos seis; os outros mudam só a última
    linha)."""
    return f"Olá {nome_lead}, tudo bem?\n\n{canonizar(texto)}\n\nTô por aqui se precisar 🙂"


def variantes_para_tela() -> dict[str, Any]:
    """Texto que vale, padrão, versão e o estado do espelho — pra tela desenhar.

    Tolerante: copy gravada inválida não derruba a tela. Vem em `problema` (é o
    mesmo motivo pelo qual o motor está segurando o disparo) e os textos caem
    no do código SÓ pra exibição.
    """
    problema: str | None = None
    indisponivel: str | None = None
    overrides: dict[str, str] = {}
    espelho = True
    try:
        overrides, espelho = _ler_copies_estrito()
    except CopyInvalida as e:
        problema = str(e)
    except Exception as e:  # noqa: BLE001 — ConfigIndisponivel e o resto
        indisponivel = str(e)[:160]
    pesos = pesos_efetivos()
    itens = []
    for v in VARIANTES:
        texto = _texto_de(v, overrides, espelho)
        espelhando = bool(v.espelha and espelho)
        fonte = v.espelha if espelhando else v.id
        itens.append({
            "id": v.id,
            "peso": pesos.get(v.id, v.peso),
            "marketing": v.marketing,
            "ativa": v.ativa,
            "texto": texto,
            "texto_padrao": texto_padrao(fonte) or v.texto,
            "texto_personalizado": fonte in overrides,
            "versao": versao_do_texto(texto),
            "espelha": v.espelha,
            "espelhando": espelhando,
            "texto_editavel": v.ativa and not espelhando,
            "previa": previa_no_celular(texto),
        })
    return {"variantes": itens, "espelho": espelho, "problema": problema,
            "indisponivel": indisponivel, "texto_max": TEXTO_MAX_CHARS}


def historico_das_copies(limite: int = 30) -> list[dict[str, Any]]:
    """Cada mudança de texto, da mais nova pra mais velha: variante, antes,
    depois, autor, quando. `depois: None` = voltou ao texto do código.

    Sai de `config_operacional_historico` (a chave guarda o mapa inteiro; aqui
    vira uma linha por variante que mudou). Levanta se o banco falhar.
    """
    from app.services import config_operacional

    out: list[dict[str, Any]] = []
    for h in config_operacional.historico([CHAVE_COPIES, CHAVE_ESPELHO], limite=200):
        if h["chave"] == CHAVE_ESPELHO:
            antes = True if h["antes"] is None else h["antes"]
            depois = True if h["depois"] is None else h["depois"]
            if antes != depois:
                out.append({"variante": None, "espelho": depois, "antes": None,
                            "depois": None, "autor": h["autor"], "em": h["em"]})
            continue
        antes = h["antes"] if isinstance(h["antes"], dict) else {}
        depois = h["depois"] if isinstance(h["depois"], dict) else {}
        for vid in sorted(set(antes) | set(depois)):
            if antes.get(vid) != depois.get(vid):
                out.append({
                    "variante": vid, "espelho": None,
                    "antes": antes.get(vid) or texto_padrao(vid),
                    "depois": depois.get(vid),
                    "depois_e_o_padrao": depois.get(vid) is None,
                    "autor": h["autor"], "em": h["em"],
                })
    return out[:limite]


def valendo_desde(historico: list[dict[str, Any]]) -> dict[str, str | None]:
    """Desde quando o texto de cada variante é o de agora. None = é o texto do
    código e ninguém mexeu (desde sempre). A que espelha acompanha a fonte."""
    desde: dict[str, str | None] = {}
    for h in historico:  # da mais nova pra mais velha: a primeira vista vale
        if h.get("variante") and h["variante"] not in desde:
            desde[h["variante"]] = h["em"]
    return {v.id: desde.get(v.id) for v in VARIANTES}
