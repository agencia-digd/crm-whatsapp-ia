"""Segunda opinião do corretor no opt-out — ele sugere, a regra decide.

Medido em 21/09/2026 contra 42.521 mensagens inbound de 14 meses
(`projetos/acme/RELATORIO-CORRETOR-OPTOUT.md`). O veredito de lá, em uma linha:

    Sozinho ele PERDE da regra nos dois eixos. Como segunda opinião, com a
    regra decidindo e ele podendo discordar, a base sai de 35 acertos / 7 erros
    para 40 / 2, ao custo de US$ 1,50 por ano e 7 linhas de conferência.

Por que ele não decide nada, com número: 78% dos opt-outs desta operação são
clique de botão ("Parar mensagens") — comparar string, não entender português —
e o corretor dá 0,77 pra esse clique, no meio da faixa do "não tenho mais interesse".
Não existe corte em que ele bata a regra: no melhor ponto dele (0,70) fica em
precisão 0,829 / recall 0,919 contra 0,833 / 0,946 dela.

Os dois usos que sobraram, e são os que estão aqui:

* VETO (corte 0,50) — a regra marcou e o corretor discorda. Na base inteira isso
  aparece nos 7 falsos positivos dela e em nenhum opt-out real (menor nota
  verdadeira 0,53, maior nota falsa 0,42 — margem de 0,11). **Não apaga o
  opt-out**: vira "confira este, o classificador discorda". O relatório é
  explícito de que o veto é o lado frágil ao enunciado — sob outra redação da
  pergunta ele perde 1 ou 2 dos 7 vetos (nunca mata um opt-out real, o erro do
  enunciado é sempre pro lado de deixar passar).
* REDE (corte 0,85) — a regra não marcou e o corretor pontuou alto. Vira linha de
  conferência pra Dona/corretora, **nunca** descadastro automático. É o único
  arranjo estável nas três redações testadas: traz o mesmo caso certo e nenhum
  errado nas três.

━━ O ENUNCIADO É CONGELADO ━━

`ENUNCIADO` abaixo é a frase literal com que os cortes 0,50 e 0,85 foram
medidos. **Mudar essa frase obriga a refazer a medição inteira** — não é figura
de retórica: trocar "parar de receber mensagens" por "sair da lista" moveu a
nota de UMA MESMA fala de 0,10 pra 0,82 (+0,72), enquanto repetir a mesma
pergunta move 0,01. Ele lê a pergunta tanto quanto lê a mensagem. Por isso o
corte é propriedade do enunciado, não do caso, e por isso enunciado, modelo,
endpoint e cortes moram no CÓDIGO (versionados, revisáveis no diff) e não no
`.env`, onde mudariam sem deixar rastro.

━━ FALHA ABERTA ━━

Sem chave, sem rede, com erro ou timeout da API: a regra decide igual e quem
chamou recebe `corretor_rodou=False` pra dizer no rodapé que a segunda passada não
rodou. Classificador fora do ar não pode nem apagar nem inventar descadastro.

━━ O QUE ESTE MÓDULO NUNCA FAZ ━━

Nunca chama `opt_out.registrar()`. Nunca move card. Nunca envia. A fonte
canônica continua sendo `app/services/opt_out.is_opt_out` — este arquivo é uma
camada de LEITURA por cima dela.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import structlog

from app.config import settings
from app.services.opt_out import is_opt_out

log = structlog.get_logger(__name__)

# ── O que foi medido. Não mexer sem refazer a medição. ───────────────────────

#: A pergunta LITERAL de `RELATORIO-CORRETOR-OPTOUT.md` §3. Mudou a frase, mudou o
#: corte: a §6 do relatório mede +0,72 de deslocamento por reescrever isto.
ENUNCIADO = "essa pessoa está pedindo para parar de receber mensagens?"

#: Abaixo disto o corretor está DISCORDANDO de uma marcação da regra → aviso.
CORTE_VETO = 0.50
#: Acima disto ele está apontando algo que a regra não viu → linha de conferência.
CORTE_REDE = 0.85

#: Escolha de infraestrutura (justificada em RELATORIO-CORRETOR-IMPLANTACAO.md):
#: é o par modelo+endpoint em que os cortes acima foram medidos. O manual
#: oficial (api.typesafe.ai/v1/systemone, corretor-1.13.0) é outro caminho de
#: serviço e outra build — trocar ali é a mesma classe de mudança que trocar o
#: enunciado, e exigiria refazer a medição.
MODELO = "typesafe/corretor-1.13"
ENDPOINT = "https://openrouter.ai/api/alpha/decisions"

#: Nome da única pergunta do payload — chave de ida e de volta.
_PERGUNTA = "optout"
#: Teto de texto enviado. Mensagem de WhatsApp não chega perto; é fusível.
_MAX_CHARS = 4000


# ── Transporte (injetável — é o que os testes trocam por um falso) ───────────

#: Recebe o payload já montado e devolve o JSON da resposta. Levantar exceção
#: aqui é um caminho previsto: vira falha aberta, não erro pra quem chamou.
Transporte = Callable[[dict], dict]


def _chave() -> str:
    """Chave do OpenRouter, lida NO MOMENTO DO USO. Nunca vai pra log.

    Ordem: `OPENROUTER_API_KEY` do ambiente/.env > arquivo
    `settings.openrouter_env_file` (/etc/assistente/openrouter.env, root-only).
    Trocar a chave no arquivo vale na chamada seguinte, sem restart.
    """
    if settings.openrouter_api_key:
        return settings.openrouter_api_key
    from app.config import _ler_env_file

    return _ler_env_file(settings.openrouter_env_file).get("OPENROUTER_API_KEY", "")


def _transporte_http(payload: dict) -> dict:
    """POST real no OpenRouter. Levanta em qualquer coisa que não seja 2xx."""
    import httpx

    chave = _chave()
    if not chave:
        raise RuntimeError("sem OPENROUTER_API_KEY")
    r = httpx.post(
        ENDPOINT,
        json=payload,
        headers={"Authorization": f"Bearer {chave}", "Content-Type": "application/json"},
        timeout=settings.opt_out_corretor_timeout,
    )
    r.raise_for_status()
    return r.json()


# ── Entrada sem conteúdo: barrada ANTES da API ───────────────────────────────

def sem_conteudo(texto: str | None) -> bool:
    """`''`, espaço em branco e placeholder (`[audio]`, `[reaction]`…).

    O modo de falha caro do teste da DEX (20/09) foi exatamente este: lá o corretor
    deu 0,77 pra `[unsupported]` e dez leads entraram na lista por causa de
    placeholder. Com o enunciado desta medição não se repetiu — o pior aqui é
    `[revoke]` com 0,41, abaixo de qualquer corte —, mas 0,41 não é 0: entrada
    vazia devolve 0,2-0,4, não devolve "não sei". A margem pro pior opt-out
    real (0,53) é de 0,12, e 0,12 é fino demais pra ser a única defesa.

    Então a defesa é estrutural e vem antes: mensagem sem fala não chega na API.
    Não gasta chamada, não recebe nota, não vira linha de conferência nenhuma.
    """
    if not texto:
        return True
    t = texto.strip()
    if not t:
        return True
    # `[audio]`, `[reaction]`, `[unsupported]`, `[image]`, `[sticker]`… — o
    # literal que `mensagens_whatsapp.conteudo_texto` guarda quando não há fala.
    return t.startswith("[") and t.endswith("]") and " " not in t


# ── A consulta ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Opiniao:
    """O que o corretor respondeu — ou por que não respondeu."""

    nota: float | None = None
    #: False = a segunda passada NÃO rodou (sem chave, sem rede, erro, timeout,
    #: desligada, ou entrada sem conteúdo). Quem chama diz isso no rodapé.
    rodou: bool = False
    motivo: str = ""


def perguntar(texto: str, *, transporte: Transporte | None = None) -> Opiniao:
    """Pergunta ao corretor e devolve a nota. **Nunca levanta** — falha aberta.

    Qualquer problema (sem chave, rede, 5xx, timeout, resposta fora do formato)
    vira `Opiniao(nota=None, rodou=False, motivo=...)`.
    """
    payload = {
        "model": MODELO,
        "state": texto[:_MAX_CHARS],
        "questions": {_PERGUNTA: {"type": "noul", "instructions": ENUNCIADO}},
    }
    try:
        dados = (transporte or _transporte_http)(payload)
        nota = (dados or {}).get("answers", {}).get(_PERGUNTA, {}).get("noul")
        if nota is None:
            return Opiniao(motivo="resposta sem nota")
        return Opiniao(nota=float(nota), rodou=True)
    except Exception as e:  # noqa: BLE001 — falha aberta é o comportamento
        # Só a CLASSE do erro no log: a mensagem pode carregar URL com chave.
        motivo = type(e).__name__
        log.warning("opt_out_corretor_falhou", erro=motivo)
        return Opiniao(motivo=motivo)


# ── O veredito composto ──────────────────────────────────────────────────────

#: `tipo` do veredito. None = o corretor concordou (ou não rodou).
VETO = "veto"
REDE = "rede"


@dataclass(frozen=True)
class Veredito:
    """A decisão (da REGRA) e, ao lado, o que o corretor achou dela."""

    #: O que vale. É `opt_out.is_opt_out`, intocado — o corretor não mexe nisto.
    decisao: bool
    nota: float | None = None
    #: None | "veto" (a regra marcou e ele discorda) | "rede" (ele viu algo que
    #: a regra não viu). Os dois são AVISO pra humano, nunca ação.
    tipo: str | None = None
    corretor_rodou: bool = False
    motivo: str = ""

    @property
    def confere(self) -> bool:
        """Esta fala vira linha de conferência humana?"""
        return self.tipo is not None

    @property
    def aviso(self) -> str:
        """A frase que a Dona/corretora lê na linha de conferência."""
        if self.tipo == VETO:
            return (
                f"confira este: a regra marcou opt-out e o classificador discorda "
                f"({self.nota:.2f}). O opt-out continua valendo até alguém olhar."
            )
        if self.tipo == REDE:
            return (
                f"confira este: o classificador acha que é pedido de descadastro "
                f"({self.nota:.2f}) e a regra não marcou. Ninguém foi descadastrado."
            )
        return ""


def segunda_opiniao(texto: str | None, *, transporte: Transporte | None = None) -> Veredito:
    """Roda a regra e, quando vale a pena, pede a segunda opinião ao corretor.

    `Veredito.decisao` é SEMPRE o que `opt_out.is_opt_out` respondeu — o corretor
    não muda esse bit em nenhum caminho deste arquivo. O que ele produz é
    `tipo`: veto ou rede, e os dois são texto pra humano.
    """
    decisao = is_opt_out(texto)

    if sem_conteudo(texto):
        # Nem chama: ver `sem_conteudo`. Sem nota = sem linha de conferência.
        return Veredito(decisao=decisao, motivo="sem conteúdo")
    if not settings.opt_out_corretor_ativo:
        return Veredito(decisao=decisao, motivo="desligado (OPT_OUT_CORRETOR_ATIVO)")

    op = perguntar(texto or "", transporte=transporte)
    if not op.rodou or op.nota is None:
        return Veredito(decisao=decisao, motivo=op.motivo)

    tipo: str | None = None
    if decisao and op.nota < CORTE_VETO:
        tipo = VETO
    elif not decisao and op.nota >= CORTE_REDE:
        tipo = REDE
    return Veredito(decisao=decisao, nota=op.nota, tipo=tipo, corretor_rodou=True)


def rodape(vereditos: list[Veredito]) -> str:
    """A linha de estado que todo relatório que usa isto precisa imprimir.

    Existe porque a falha aberta só é honesta se for VISÍVEL: sem esta linha,
    "nenhuma sugestão hoje" e "a API estava fora" viram a mesma tela, e aí
    ninguém percebe que a segunda passada morreu.
    """
    if not vereditos:
        return "Segunda opinião (corretor): nada a conferir."
    rodaram = [v for v in vereditos if v.corretor_rodou]
    if not rodaram:
        return (
            "Segunda opinião (corretor): NÃO RODOU — a regra decidiu sozinha "
            f"({vereditos[0].motivo or 'motivo não registrado'})."
        )
    vetos = sum(1 for v in rodaram if v.tipo == VETO)
    redes = sum(1 for v in rodaram if v.tipo == REDE)
    linha = (
        f"Segunda opinião (corretor {MODELO}): {len(rodaram)} de {len(vereditos)} conferidas · "
        f"{vetos} discordância(s) da regra · {redes} sugestão(ões) nova(s)"
    )
    if len(rodaram) < len(vereditos):
        linha += f" · {len(vereditos) - len(rodaram)} não rodaram (falha aberta)"
    return linha + ". Nada disso descadastra ninguém sozinho."


# ── A gravação: linha de conferência, e só ───────────────────────────────────

def registrar_conferencia(
    session,
    veredito: Veredito,
    *,
    telefone: str | None = None,
    mensagem_id=None,
    texto: str | None = None,
    tenant_id: int = 1,
) -> bool:
    """Grava a linha de conferência em `opt_out_conferencia`. Idempotente.

    É o ÚNICO write deste módulo, e ele não muda estado de ninguém: não chama
    `opt_out.registrar()`, não apaga opt-out, não move card, não enfileira
    envio. Quem age depois de ler a linha é a Dona ou a corretora, na mão.

    Devolve True quando a linha entrou; False quando não havia o que conferir,
    quando ela já existia (UNIQUE por mensagem+tipo) ou quando o INSERT falhou.
    Falhar aqui não pode derrubar quem chamou: perder um AVISO é barato, e a
    decisão do opt-out já foi tomada pela regra antes de chegar neste ponto.
    """
    if not veredito.confere:
        return False
    from sqlalchemy import text as _sql

    try:
        r = session.execute(
            _sql("""
                INSERT INTO opt_out_conferencia
                    (tenant_id, tipo, telefone, mensagem_id, texto, nota,
                     regra_marcou, modelo, enunciado)
                VALUES (:t, :tipo, :tel, :msg, :txt, :nota, :regra, :modelo, :enunciado)
                ON CONFLICT DO NOTHING
            """),
            {
                "t": tenant_id, "tipo": veredito.tipo, "tel": telefone,
                "msg": mensagem_id, "txt": texto, "nota": veredito.nota,
                "regra": veredito.decisao, "modelo": MODELO, "enunciado": ENUNCIADO,
            },
        )
        # Só o sufixo do telefone no log — a fala fica no banco, onde quem
        # confere a lê; log não é lugar de conversa de lead.
        log.info("opt_out_conferencia", tipo=veredito.tipo, nota=veredito.nota,
                 suf=(telefone or "")[-4:], nova=bool(r.rowcount))
        return bool(r.rowcount)
    except Exception:  # noqa: BLE001 — aviso perdido não derruba a operação
        log.exception("opt_out_conferencia_falhou", tipo=veredito.tipo)
        return False


__all__ = [
    "CORTE_REDE", "CORTE_VETO", "ENDPOINT", "ENUNCIADO", "MODELO", "REDE", "VETO",
    "Opiniao", "Transporte", "Veredito", "perguntar", "registrar_conferencia",
    "rodape", "sem_conteudo",
    "segunda_opiniao",
]
