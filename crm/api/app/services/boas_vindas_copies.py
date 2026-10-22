"""
Variantes de copy da BOAS-VINDAS — teste A/B com medição.

Dona 05/08: a primeira mensagem do lead novo passa a ter TRÊS copies
concorrendo, igual já roda no carrossel da Corretor (`carrossel_copies.py`). Mesma
mecânica, mesmos motivos — o que muda é que aqui o texto carrega o nome da
corretora dona da linha (Pessoa5 / Pessoa1 / Pessoa), então a variante guarda
um `{nome}` e quem renderiza é `lead_intake.boas_vindas_texto`.

RODADA 1 (05/08): **A × B**, 50/50. A `triagem_prazo` (C) fica cadastrada com
peso 0 e `ativa=False`, esperando a rodada 2 — ela NÃO é uma variante aposentada,
é uma que ainda não competiu. B e C atacam o mesmo eixo (triagem); rodar as duas
junto com o controle daria 1/3 do volume pra cada braço e triplicaria o tempo até
a amostra fechar.

POR QUE O CONTROLE (A) FICA NA FILA, em vez de B × C: A é o que roda HOJE. Sem
ela, a gente descobre qual triagem é melhor ENTRE AS DUAS, mas não se alguma
delas é melhor que o que já existe — e aí troca-se por trocar. Foi o controle que
salvou o teste do carrossel: só se soube que a "direta" prestava porque a copy
antiga estava lá como linha de base (13,1% × 2,0%).

⚠️ COMO LER O RESULTADO — A muda DUAS coisas em relação a B e C, não uma:
  1. o ENQUADRAMENTO (confirma o cadastro e fala de nós × faz uma triagem);
  2. o FECHO — A termina em AFIRMAÇÃO ("estamos com algumas oportunidades nessa
     semana."), B e C terminam em PERGUNTA.
Ou seja: o que este teste mede é "pergunta que qualifica × aviso", não
enquadramento puro. Isso não invalida nada — o objetivo declarado é RESPOSTA, e
as duas coisas empurram na mesma direção — mas quem for ler o placar daqui a três
semanas precisa saber que as duas variáveis andaram juntas, e não atribuir o
ganho só ao enquadramento. Pra isolar, seria preciso uma quarta variante: o texto
da A terminando em pergunta.

Abertura, assinatura, categoria (UTILITY) e template são iguais nas três.

BUCKETING DETERMINÍSTICO (não sorteio): cada lead cai num balde fixo, decidido
pelo hash do id dele. Três motivos, os mesmos do carrossel:
  1. a divisão sai na proporção pedida já nos primeiros dias — sorteio com
     poucos envios/dia dá desvio grande;
  2. o mesmo lead cai SEMPRE na mesma variante (o balde é dele, não do envio);
  3. dá pra auditar depois: com o id do lead dá pra recalcular a variante.

⚠️ O que CONTA no relatório é o que efetivamente SAIU: a variante vai gravada
no `lead_events(tipo='boas_vindas_enviada').payload.variante`, e é esse campo
que a métrica lê — nunca o recálculo da intenção.

⚠️ Variante perdedora se APOSENTA (`ativa=False` / peso 0), nunca se apaga: o
resultado dela é o que justifica a que entrar no lugar. `ativa=False` também
guarda quem ainda NÃO competiu (é o caso da C hoje) — quem lê a tela vê a
diferença, porque o rótulo vem do campo `hipotese`.

⚠️ A boas-vindas sai por TEMPLATE (janela fechada — o lead nunca escreveu).
As três variantes viajam na MESMA variável ({{2}}) do template UTILITY já
aprovado: nenhuma delas exige template novo na Meta.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace

import structlog

log = structlog.get_logger()

# Único placeholder aceito no corpo — é o nome da corretora dona da linha.
PLACEHOLDER_NOME = "{nome}"


@dataclass(frozen=True)
class Variante:
    id: str
    rotulo: str
    texto: str          # corpo {{2}} com {nome} — SEM "Olá" (o template cumprimenta)
    peso: int           # fatia em % — a soma das ativas tem que dar 100
    hipotese: str
    ativa: bool = True
    # True = está fora da fila mas AINDA NÃO COMPETIU (entra numa rodada
    # seguinte). Sem esta marca, a tela chapessoa de "aposentada" quem só está
    # esperando a vez — e aposentada é quem PERDEU. A diferença muda a leitura.
    aguardando: bool = False

    def render(self, nome: str) -> str:
        """Corpo com o nome da corretora no lugar do {nome}."""
        return self.texto.replace(PLACEHOLDER_NOME, nome)


VARIANTES: tuple[Variante, ...] = (
    Variante(
        id="cadastro",
        rotulo="A · confirmação do cadastro",
        peso=50,
        hipotese=(
            "CONTROLE — a copy que já rodava. Pede confirmação do óbvio ('você "
            "se cadastrou, certo?') e fala de NÓS ('estamos com oportunidades'): "
            "quem lê não precisa fazer nada, e não faz."
        ),
        texto=(
            "Sou a {nome}, da Acme. Você acabou de se cadastrar com interesse "
            "em imóveis aqui na cidade de Cidade, certo? Estamos com algumas "
            "oportunidades nessa semana."
        ),
    ),
    Variante(
        id="triagem_intencao",
        rotulo="B · triagem morar × investir",
        peso=50,
        hipotese=(
            "muda só o ENQUADRAMENTO: em vez de confirmar o cadastro, faz a "
            "triagem morar × investir. A pergunta é fácil de responder (não "
            "exige admitir desistência) e quem responde JÁ se qualifica — mesma "
            "aposta do `brendon_triagem` no carrossel."
        ),
        texto=(
            "Sou a {nome}, da Acme 😊 Vi seu cadastro de interesse em imóvel "
            "aqui e já quero começar certo: você está procurando pra morar ou "
            "pra investir?"
        ),
    ),
    Variante(
        id="triagem_prazo",
        rotulo="C · triagem por prazo",
        # SEGUNDA RODADA (Dona, 05/08). Não está aposentada — ela ainda NÃO
        # competiu: `ativa=False` + peso 0 só a tira da fila da rodada 1.
        # Motivo: B e C atacam o MESMO eixo (triagem). Com as duas na fila junto
        # com o controle, cada braço fica com 1/3 do volume e o tempo até 20
        # envios por braço triplica. Roda A × B, fecha, e aí a C entra contra a
        # vencedora. Pra ligar: peso 50/50 entre ela e a campeã, `ativa=True`.
        peso=0,
        ativa=False,
        aguardando=True,
        hipotese=(
            "AGUARDANDO A SEGUNDA RODADA — ainda não competiu. "
            "vai atrás do dado mais valioso pra priorizar atendimento: o PRAZO. "
            "A formulação é deliberada — 'se organizando com mais calma' é uma "
            "resposta DIGNA, então quem está longe responde a verdade em vez de "
            "sumir; 'sair do aluguel' dá contexto emocional em vez de virar "
            "formulário; e 'pra eu te ajudar do jeito certo' justifica a "
            "pergunta (sem isso parece triagem, com isso parece cuidado). "
            "NÃO reescrever pra 'está apenas pesquisando': essa versão foi "
            "descartada (Dona, 05/08) porque julga — e quem se sente julgado "
            "mente ou some."
        ),
        texto=(
            "Sou a {nome}, da Acme 😊 Vi seu cadastro de interesse em imóvel "
            "aqui. Pra eu te ajudar do jeito certo: você pretende sair do "
            "aluguel ainda esse ano, ou está se organizando com mais calma?"
        ),
    ),
)

CONTROLE_ID = VARIANTES[0].id


def _pesos_do_painel() -> dict[str, int]:
    """Pesos editados no painel (config_operacional). Vazio = os do código."""
    from app.services import config_operacional

    try:
        bruto = config_operacional.get("boas_vindas_pesos") or {}
    except Exception as e:  # noqa: BLE001 — peso não derruba envio
        log.warning("boas_vindas_pesos_indisponivel", erro=str(e)[:140])
        return {}
    out: dict[str, int] = {}
    for k, v in bruto.items():
        try:
            out[str(k)] = int(v)
        except (TypeError, ValueError):
            continue
    return out


def _overrides_de_texto() -> dict[str, str]:
    """Textos editados no painel, por variante."""
    from app.services import config_operacional

    try:
        return config_operacional.boas_vindas_copies()
    except Exception as e:  # noqa: BLE001 — copy não cai por config indisponível
        log.warning("boas_vindas_copy_override_indisponivel", erro=str(e)[:140])
        return {}


def todas() -> tuple[Variante, ...]:
    """As variantes com peso e texto EFETIVOS (painel > código)."""
    pesos = _pesos_do_painel()
    textos = _overrides_de_texto()
    out: list[Variante] = []
    for v in VARIANTES:
        peso = pesos.get(v.id, v.peso)
        peso = max(0, min(100, peso))
        texto = (textos.get(v.id) or "").strip() or v.texto
        out.append(replace(v, peso=peso, texto=texto, ativa=v.ativa and peso > 0))
    return tuple(out)


def ativas() -> tuple[Variante, ...]:
    return tuple(v for v in todas() if v.ativa and v.peso > 0)


def escolher(lead_id: int | str | None) -> Variante:
    """Balde do lead. Determinístico: mesmo lead → mesma variante, sempre.

    Sem id (chamada de preview, por exemplo) devolve o CONTROLE — nunca um
    sorteio: o preview da tela não pode mudar a cada F5.
    """
    vs = ativas()
    if not vs:
        # Todas desligadas seria "boas-vindas sem texto". Cai no controle do
        # código: preferimos a copy antiga a um envio vazio.
        log.warning("boas_vindas_sem_variante_ativa")
        return VARIANTES[0]
    if lead_id is None:
        return next((v for v in vs if v.id == CONTROLE_ID), vs[0])
    total = sum(v.peso for v in vs)
    # md5 do id (não `hash()`: o hash do Python é aleatorizado por processo e a
    # variante mudaria a cada restart do serviço).
    h = int(hashlib.md5(str(lead_id).encode()).hexdigest()[:8], 16)
    ponto = h % total
    acum = 0
    for v in vs:
        acum += v.peso
        if ponto < acum:
            return v
    return vs[-1]


def por_id(vid: str | None) -> Variante | None:
    return next((v for v in todas() if v.id == vid), None)


def ids_conhecidos() -> tuple[str, ...]:
    return tuple(v.id for v in VARIANTES)


def padrao_de(vid: str) -> str | None:
    """Texto do CÓDIGO da variante (o 'voltar ao padrão' da tela)."""
    return next((v.texto for v in VARIANTES if v.id == vid), None)


ESTADO_NA_RODADA = "na rodada"
ESTADO_AGUARDANDO = "aguardando a próxima rodada"
ESTADO_APOSENTADA = "aposentada"


def estado_da_variante(v: Variante) -> str:
    """Rótulo de estado: quem está competindo, quem espera a vez, quem perdeu."""
    if v.ativa and v.peso > 0:
        return ESTADO_NA_RODADA
    return ESTADO_AGUARDANDO if v.aguardando else ESTADO_APOSENTADA


def pesos_padrao() -> dict[str, int]:
    """Os pesos escritos no código — o que o painel chama de 'padrão'."""
    return {v.id: v.peso for v in VARIANTES}


TEXTO_MAX_CHARS = 900  # o corpo vai no {{2}} do template WABA (teto ~1024)


def validar_texto(texto: str) -> str:
    """Valida o corpo de uma variante vindo da tela. `ValueError` em PT-BR.

    Travas (as mesmas que já valem pra copy da cadência, menos a do '?', que
    aqui não faz sentido — a variante de controle termina em afirmação):
      - não vazio e dentro do teto do {{2}};
      - só o placeholder {nome}. Quem substitui é o nosso código
        (`Variante.render` = `str.replace("{nome}", ...)`), não a Meta e não o
        `str.format` — então QUALQUER outra chave sai literal pro cliente,
        inclusive as malformadas ({nome sem fechar, {nome!r}, {} sozinho). Por
        isso a trava não procura "placeholder parecido com placeholder": ela
        exige que, tirando os {nome}, NÃO SOBRE chave nenhuma no texto;
      - sem "Saiba mais" no corpo: o template já fecha com o botão, e repetir
        vira mensagem gaguejando.
    """
    t = (texto or "").strip()
    if not t:
        raise ValueError("a copy não pode ficar vazia")
    if len(t) > TEXTO_MAX_CHARS:
        raise ValueError(f"copy longa demais (máx {TEXTO_MAX_CHARS} caracteres)")
    import re

    # Tira os {nome} legítimos e olha o que sobrou de chave. Pega tanto o
    # placeholder inventado ({{1}}, {telefone}) quanto o malformado ({nome,
    # {nome!r}, }) — os dois viajam literais até o WhatsApp do cliente.
    resto = t.replace(PLACEHOLDER_NOME, "")
    if "{" in resto or "}" in resto:
        # trecho legível pra tela: cada pedaço com chave que sobrou
        invalidos = sorted(
            set(re.findall(r"\{[^{}]{0,40}\}?|\}", resto))
        ) or ["{"]
        raise ValueError(
            "placeholder não suportado: " + ", ".join(invalidos)
            + f" (só existe {PLACEHOLDER_NOME})"
        )
    if "saiba mais" in t.lower():
        raise ValueError(
            "tira o 'Saiba mais' do corpo — o template já fecha com o botão"
        )
    return t


def validar_pesos(pesos: dict[str, object]) -> dict[str, int]:
    """Normaliza e valida um mapa variante → peso vindo da tela.

    Sai o mapa COMPLETO (toda variante conhecida), pra o que foi gravado ser
    legível sem consultar o código. `ValueError` em PT-BR; quem chama traduz
    pro HTTP.
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

    # Variante fora da rodada não entra na fila pela tela. Vale pras duas
    # situações: a que JÁ competiu e perdeu (reativar sem o contexto reabre um
    # teste encerrado) e a que AINDA NÃO competiu (entra na rodada seguinte, com
    # a decisão registrada no código, não num clique de terça-feira).
    for vid, v in conhecidas.items():
        if not v.ativa and out[vid] > 0:
            raise ValueError(
                f"{vid!r} está fora da rodada atual — pra colocar na fila, "
                "mexe no código (é lá que fica o porquê)"
            )

    soma = sum(out[vid] for vid, v in conhecidas.items() if v.ativa)
    if soma != 100:
        raise ValueError(f"os pesos das variantes ativas somam {soma}, não 100")
    return out


def validar_copies(copies: dict[str, object]) -> dict[str, str]:
    """Normaliza e valida o mapa variante → texto vindo da tela."""
    if not isinstance(copies, dict):
        raise ValueError("esperado um mapa variante → copy")
    conhecidas = {v.id for v in VARIANTES}
    out: dict[str, str] = {}
    for vid, texto in copies.items():
        if vid not in conhecidas:
            raise ValueError(f"variante desconhecida: {vid!r}")
        if texto is None or not str(texto).strip():
            continue  # apagar o override = voltar ao padrão do código
        if not isinstance(texto, str):
            raise ValueError(f"copy de {vid!r} precisa ser texto")
        out[str(vid)] = validar_texto(texto)
    return out


def conferir_pesos() -> list[str]:
    """Problemas de configuração. Vazio = tudo certo."""
    problemas: list[str] = []
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
