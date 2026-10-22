"""
Agente Pessoa — atendimento/qualificação de leads da Acme (primeiro contato).

Conversa com o lead pelo WhatsApp (linha da Corretor, 0000-0000), qualifica até ter o que
a simulação precisa e PARA. Não simula, não oferece visita: chama
`registrar_qualificacao`, que deixa o raio-X pronto, promove o lead pra Pessoa3
e abre a conversa na LINHA DA CORRETOR (2591). Daí em diante o atendimento acontece
lá, com gente — a Pessoa silencia no 0000.

Decisão da Dona (02/08/2026): a simulação da Caixa saiu do fluxo do robô. Ela
demorava 1-2 min, travava a conversa e o lead ficava preso num chat automático
depois de já ter entregado tudo que importava. Melhor entregar cedo pra Pessoa12.

NÃO confundir com a Corretor, que só reaquece e transfere (remarketing/recovery).

Mesmo padrão do Assistente: sem Agno.Agent nem SDK Anthropic — roda via
app.config.get_llm_provider() (Claude CLI, plano Max). Tools são funções simples.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import structlog

from app.config import get_llm_provider
from app.persona.dna_corretor import aplicar_dna
from app.persona.metodo_pessoa_e import aplicar_metodo
from app.tools.caixa import CaixaSimuladorTools
from app.tools._archive.empreendimentos import consultar_empreendimentos  # aposentada 11/07/2026

log = structlog.get_logger()

PESSOA_DIR = Path(__file__).parent
SOUL_PATH = PESSOA_DIR / "SOUL.md"
MAX_TOOL_ITERATIONS = 5

# ── Resiliência do provider (05/09/2026) ─────────────────────────────────────
# Claude (janela de 5h) e codex (cota até dia 7) caíram JUNTOS; a Pessoa disse
# "probleminha técnico" 6x e nunca voltou — engoliu "Quero" e um pedido de
# apto de 2 quartos. Agora: retry com backoff aqui; fallback de provider vive
# em app.config (Claude → codex → Qwen); e se AINDA assim falhar, o turno sai
# marcado `provider_falhou=True` pra quem chamou enfileirar a retomada
# (app.services.pessoa_retomada) em vez de descartar a mensagem do lead.
FRASE_PROBLEMINHA = "Tive um probleminha técnico aqui, me dá um instante 😊"
PROVIDER_TENTATIVAS = 3
PROVIDER_BACKOFF_S: tuple[float, ...] = (2.0, 5.0)
_dormir = time.sleep  # monkeypatch nos testes


class MariaProviderIndisponivel(RuntimeError):
    """Nenhum provider respondeu depois de todas as tentativas."""

RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "text": {
            "type": "string",
            "description": "Sua resposta natural pro lead (PT-BR, tom Pessoa, 1ª pessoa).",
        },
        "tool_call": {
            "type": ["object", "null"],
            "description": "null se não precisar usar tool; senão {name, args}.",
            "properties": {
                "name": {
                    "type": "string",
                    "enum": [
                        # `simular_caixa` saiu do enum em 02/08/2026 (ver docstring
                        # do módulo). Fora do enum o LLM não consegue nem emitir o
                        # nome — é a trava dura; a SOUL é só a explicação.
                        "consultar_empreendimentos",
                        "registrar_qualificacao",
                        "encaminhar_avaliacao",
                        "encerrar_nao_e_lead",
                        "registrar_indicacao",
                    ],
                },
                "args": {"type": "object"},
            },
            "required": ["name", "args"],
        },
    },
    "required": ["text", "tool_call"],
}


# ── Tools ────────────────────────────────────────────────────────────────────

def _tool_simular_caixa(renda: str, valor_imovel: str, data_nascimento: str,
                        cidade: str, uf: str = "SP", possui_fgts: bool = True,
                        **_: Any) -> dict[str, Any]:
    """APOSENTADA como tool do fluxo (Dona 02/08/2026) — fora do enum e do
    TOOL_HANDLERS. Fica aqui porque o simulador segue vivo pra uso manual
    (scripts/caixa) e pra quando a Pessoa12 quiser rodar a simulação pelo painel."""
    resumo = CaixaSimuladorTools().simular_financiamento(
        renda=renda, valor_imovel=valor_imovel, data_nascimento=data_nascimento,
        cidade=cidade, uf=uf, possui_fgts=possui_fgts)
    return {"status": "ok", "resumo": resumo}


def _tool_consultar_empreendimentos(regiao: str | None = None, valor_max: int | None = None,
                                    quartos: str | None = None, **_: Any) -> dict[str, Any]:
    return consultar_empreendimentos(regiao=regiao, valor_max=valor_max, quartos=quartos)


# Corretora que assume o handoff da Pessoa (Pessoa3 = "Pessoa humana" no CRM).
HANDOFF_CORRETORA_ID = 3

# Linha por onde a conversa CONTINUA depois do handoff (Pessoa12, 2591). A Pessoa fala
# pelo 0000; a abertura do handoff sai daqui.
SLUG_CORRETOR = "corretor"

# Última fala da Pessoa no 0000 depois do handoff (Dona 02/08/2026): no passado,
# porque `_abrir_conversa_na_corretor` já mandou a msg na linha da Pessoa12. Forçamos o texto
# no agent.run — o LLM não inventa "vou te chamar" / "já te chamo".
FECHAMENTO_HANDOFF = (
    "Perfeito! Já te mandei uma mensagem pelo outro número "
    "pra gente continuar o atendimento por lá, tá?"
)
FECHAMENTO_HANDOFF_PENDENTE = (
    "Perfeito! Já te chamo pelo outro número "
    "pra gente continuar o atendimento por lá, tá?"
)


def _perfil_de_busca(q: dict[str, Any]) -> str:
    """"casa 2 quartos em Bairro X" — o que o lead procura, em uma linha."""
    perfil: list[str] = []
    if q.get("tipo"):
        perfil.append(str(q["tipo"]))
    if q.get("quartos"):
        perfil.append(f"{q['quartos']} quartos")
    if q.get("regiao"):
        perfil.append(f"em {q['regiao']}")
    return " ".join(perfil)


def _montar_raiox(q: dict[str, Any]) -> str:
    """Monta a nota narrativa de passagem a partir dos campos da qualificação."""
    linhas: list[str] = []
    if q.get("quente"):
        linhas.append("🔥 LEAD QUENTE — priorizar.")
    perfil = _perfil_de_busca(q)
    if perfil:
        linhas.append(f"Busca: {perfil}.")
    if q.get("para_quem"):
        linhas.append(f"Pra quem: {q['para_quem']}.")
    if q.get("faixa_valor"):
        linhas.append(f"Faixa de valor: {q['faixa_valor']}.")
    if q.get("momento"):
        linhas.append(f"Momento de compra: {q['momento']}.")
    if q.get("entrada"):
        linhas.append(f"Entrada disponível: {q['entrada']}.")
    # Dados que a simulação pede — a Pessoa coleta, quem simula é a Pessoa12.
    if q.get("renda"):
        linhas.append(f"Renda: {q['renda']}.")
    if q.get("data_nascimento"):
        linhas.append(f"Nascimento: {q['data_nascimento']}.")
    if q.get("fgts") is not None:
        linhas.append("FGTS 3 anos: sim." if q["fgts"] else "FGTS 3 anos: não.")
    if q.get("simulacao"):
        linhas.append(f"Simulação Caixa: {q['simulacao']}")
    if q.get("empreendimento_sugerido"):
        linhas.append(f"Empreendimento sugerido: {q['empreendimento_sugerido']}.")
    if q.get("observacao"):
        linhas.append(f"Obs: {q['observacao']}")
    return " ".join(linhas) or "Lead qualificado pela Pessoa — sem detalhes adicionais."


def _mensagem_abertura_corretor(q: dict[str, Any], nome: str | None) -> str:
    """A primeira mensagem que o lead recebe NA LINHA DA CORRETOR.

    Precisa fazer três coisas em quatro linhas: dizer quem é (senão o lead acha
    que é golpe — número novo do nada), provar que é a mesma conversa (o resumo
    do que ele já contou) e pedir permissão pra seguir por ali.
    """
    ola = f"Oi {nome}, Pessoa aqui!" if nome else "Oi, Pessoa aqui!"
    return (f"{ola}\n\n{corpo_abertura_corretor(q)} Podemos seguir com a simulação "
            "por aqui, nesse telefone? Pode ser?")


def corpo_abertura_corretor(q: dict[str, Any]) -> str:
    """O RESUMO — a parte que prova que é a mesma conversa.

    Fonte única de propósito: este mesmo corpo vai no texto livre E dentro da
    variável do template. Antes eram dois textos diferentes, e o do template era
    mais pobre — só a busca, sem a renda.

    Isso importava mais do que parecia: a janela de 24h da linha da Pessoa12 quase
    nunca está aberta (o lead nunca escreveu PRA ELA, só pro 0000), então o
    template é o caminho NORMAL, não a exceção. Na prática o resumo bom quase
    nunca saía.

    Corrigido em 04/08/2026 — a Dona lembrou que template tem variável, e texto
    livre cabe dentro dela.
    """
    resumo: list[str] = []
    busca = _perfil_de_busca(q)
    if busca:
        resumo.append(f"você tá procurando {busca}")
    if q.get("renda"):
        resumo.append(f"renda ~{q['renda']}")
    corpo = "A gente conversou pelo outro número"
    if resumo:
        corpo += " — te resumo: " + ", ".join(resumo)
    return corpo + "."


def _entrega_falhou(resposta: dict[str, Any] | None) -> bool:
    """A Graph API devolve 200 com wamid e ainda assim não entrega.

    O caso conhecido é a janela de 24h fechada: volta `message_status: failed`
    ("Re-engagement message"). Sem olhar isso, a gente acharia que abriu a
    conversa e o lead nunca receberia nada.
    """
    if not isinstance(resposta, dict):
        return False
    if resposta.get("error"):
        return True
    msgs = resposta.get("messages") or []
    if not msgs or not isinstance(msgs[0], dict):
        return False
    return str(msgs[0].get("message_status") or "").lower() == "failed"


def _abrir_conversa_na_corretor(phone: str, nome: str | None,
                          q: dict[str, Any]) -> dict[str, Any]:
    """Manda a abertura pela linha da Pessoa12 — SEMPRE por template UTILITY.

    Decisão da Dona (04/08/2026): "o texto que vc vai mandar pelo telefone da corretor
    é template utility, e o template tem variáveis, e é por lá que vc vai
    escrever o resumo".

    Antes tentava texto livre primeiro e só caía no template quando a entrega
    falhava. Só que a janela de 24h é POR NÚMERO: o lead escreveu pro 0000, não
    pra linha da Pessoa12 — então a janela dela está fechada em praticamente todo
    handoff. O texto livre era uma chamada que quase sempre falhava antes de
    cair no template, e falha de entrega não é de graça: conta contra a
    qualidade da linha na Meta.

    O resumo vai inteiro na variável do template, que é o que ele serve pra
    fazer. Mesmo corpo de `corpo_abertura_corretor` — uma fonte só.

    A Pessoa calar no 0000 depois disso não dependia deste caminho: quem faz
    isso é `_handoff_feito`, que olha a nota gravada.
    """
    from app.api import cloud_corretora_a as corretor

    hook = corpo_abertura_corretor(q) + " Podemos seguir com a simulação por aqui?"
    try:
        corretor.enviar_template_corretor(phone, nome, hook=hook,
                              cta="seguir com a simulação")
        log.info("pessoa_abertura_corretor", via="template", tel=phone[-4:])
        return {"aberta": True, "via": "template"}
    except Exception as e:  # noqa: BLE001
        log.error("pessoa_abertura_corretor_erro", erro=str(e)[:160])
        return {"aberta": False, "erro": str(e)[:120]}


def _tool_registrar_qualificacao(nome: str | None = None, regiao: str | None = None,
                                 tipo: str | None = None, quartos: str | None = None,
                                 para_quem: str | None = None, faixa_valor: str | None = None,
                                 momento: str | None = None, entrada: str | None = None,
                                 quente: bool = False, simulacao: str | None = None,
                                 renda: str | None = None,
                                 data_nascimento: str | None = None,
                                 fgts: bool | None = None,
                                 empreendimento_sugerido: str | None = None,
                                 observacao: str | None = None,
                                 _phone: str | None = None, _lead_nome: str | None = None,
                                 **_: Any) -> dict[str, Any]:
    """Encerra o atendimento da Pessoa e passa o lead pra Pessoa3, de verdade.

    Três coisas, nesta ordem (Dona 02/08/2026):
      1. persiste a LeadHandoffNote — o raio-X que a Pessoa12 e a Assistente leem;
      2. promove pro funil principal com a Pessoa12 como responsável;
      3. abre a conversa NA LINHA DA CORRETOR com um resumo do que o lead contou.

    O passo 3 é o que muda o jogo: antes a Pessoa ficava conversando no 0000
    depois de já ter tudo, e o lead seguia preso num atendimento automático. A
    partir daqui o 0000 silencia (ver `atendimento_corretor._handoff_feito`).
    """
    qualificacao = {
        "nome": nome, "regiao": regiao, "tipo": tipo, "quartos": quartos,
        "para_quem": para_quem, "faixa_valor": faixa_valor, "momento": momento,
        "entrada": entrada, "quente": quente, "simulacao": simulacao,
        "renda": renda, "data_nascimento": data_nascimento, "fgts": fgts,
        "empreendimento_sugerido": empreendimento_sugerido, "observacao": observacao,
    }
    log.info("pessoa_qualificacao", **{k: v for k, v in qualificacao.items() if v is not None})

    if not _phone:
        log.warning("pessoa_qualificacao_sem_telefone")
        return {"status": "ok", "registrado": True, "handoff": False,
                "aviso": "sem telefone no contexto — não persisti a nota"}

    if os.environ.get("PYTEST_CURRENT_TEST"):
        return {"status": "ok", "registrado": True, "handoff": True, "pytest": True}

    lead_nome = nome or _lead_nome
    try:
        from app.db.session import SessionLocal
        from app.db import models as _models
        contexto = _montar_raiox(qualificacao)
        with SessionLocal() as s:
            s.add(_models.LeadHandoffNote(
                telefone=_phone,
                lead_nome=lead_nome,
                corretora_id=HANDOFF_CORRETORA_ID,
                contexto=contexto,
                origem="pessoa",
                status="ativo",
            ))
            s.commit()
        log.info("pessoa_handoff_persistido", telefone=_phone, corretora_id=HANDOFF_CORRETORA_ID, quente=quente)
    except Exception as e:  # noqa: BLE001
        log.error("pessoa_handoff_erro", erro=str(e)[:160])
        return {"status": "ok", "registrado": True, "handoff": False, "erro": str(e)[:120]}

    # Promoção e abertura são independentes da nota e uma da outra: se a
    # promoção falhar, o lead ainda recebe a mensagem da Pessoa12; se a abertura
    # falhar, ele já está no funil dela com o raio-X pronto.
    promovido = False
    try:
        from app.services.atendimento_corretor import promover_para_vendas

        promovido = promover_para_vendas(_phone, lead_nome)
    except Exception as e:  # noqa: BLE001
        log.error("pessoa_promocao_erro", erro=str(e)[:160])

    abertura = _abrir_conversa_na_corretor(_phone, lead_nome, qualificacao)
    # Texto que a Pessoa manda no 0000 depois do handoff (passado se a Pessoa12 já
    # recebeu a abertura; futuro só se a entrega falhou).
    aberta = bool(isinstance(abertura, dict) and abertura.get("aberta"))
    mensagem_lead = FECHAMENTO_HANDOFF if aberta else FECHAMENTO_HANDOFF_PENDENTE
    return {"status": "ok", "registrado": True, "handoff": True,
            "promovido": promovido, "abertura_corretor": abertura,
            "mensagem_lead": mensagem_lead}


def _tool_encaminhar_avaliacao(nome: str | None = None, imovel: str | None = None,
                               regiao: str | None = None, motivo: str | None = None,
                               observacao: str | None = None,
                               _phone: str | None = None, _lead_nome: str | None = None,
                               **_: Any) -> dict[str, Any]:
    """Lead quer AVALIAR/VENDER o imóvel dele — não é compra. Passa pra Pessoa3.

    Dona 30/07: "se a pessoa quiser avaliação, manda pra Pessoa12". A Pessoa vende
    imóvel novo; avaliação é captação, outro processo e outra pessoa. Antes
    disso a Pessoa improvisava ("posso te conectar com a pessoa certa") e o
    sistema não fazia NADA — o lead ficava esperando um contato que ninguém
    sabia que era pra fazer.
    """
    partes = ["🏠 QUER AVALIAÇÃO (captação, não compra)."]
    if imovel:
        partes.append(f"Imóvel: {imovel}.")
    if regiao:
        partes.append(f"Região: {regiao}.")
    if motivo:
        partes.append(f"Motivo: {motivo}.")
    if observacao:
        partes.append(f"Obs: {observacao}")
    contexto = " ".join(partes)
    log.info("pessoa_avaliacao_encaminhada", tem_imovel=bool(imovel), regiao=regiao)

    if not _phone:
        return {"status": "ok", "encaminhado": False,
                "aviso": "sem telefone no contexto — não persisti"}
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return {"status": "ok", "encaminhado": True, "pytest": True}

    try:
        from app.db import models as _models
        from app.db.session import SessionLocal
        from app.services.atendimento_corretor import promover_para_vendas

        with SessionLocal() as s:
            s.add(_models.LeadHandoffNote(
                telefone=_phone,
                lead_nome=nome or _lead_nome,
                corretora_id=HANDOFF_CORRETORA_ID,
                contexto=contexto,
                origem="pessoa_avaliacao",
                status="ativo",
            ))
            s.commit()
        # Tira do funil de remarketing e põe com a Pessoa12 — senão o carrossel
        # continuaria mandando "ainda procurando imóvel?" pra quem quer VENDER.
        promover_para_vendas(_phone, nome or _lead_nome)
        return {"status": "ok", "encaminhado": True}
    except Exception as e:  # noqa: BLE001
        log.error("pessoa_avaliacao_erro", erro=str(e)[:160])
        return {"status": "ok", "encaminhado": False, "erro": str(e)[:120]}


def _tool_registrar_indicacao(contato: str | None = None,
                              nome_indicado: str | None = None,
                              observacao: str | None = None,
                              nome: str | None = None,
                              _phone: str | None = None, _lead_nome: str | None = None,
                              **_: Any) -> dict[str, Any]:
    """Quem disse não indicou alguém — registra o contato pra Pessoa3 trabalhar.

    Dona 03/08/2026: quando o lead recusa de forma branda, a Pessoa emenda o
    pedido de indicação na despedida. Sem esta tool a indicação morreria dentro
    da conversa — ninguém ficaria sabendo que existe um nome novo pra chamar.

    O que ela NÃO faz, de propósito:
      - não promove quem RECUSOU pro funil (ele disse não; promover suja o
        funil da Pessoa12 e faz o carrossel voltar a perseguir quem já saiu);
      - não manda mensagem pro número indicado. Contato frio que nunca pediu
        contato é a Pessoa12 quem aborda, na mão, com a referência de quem indicou.

    A nota fica em `lead_handoff_notes` com origem `pessoa_indicacao`, no telefone
    de QUEM INDICOU — é lá que a Pessoa12/Assistente leem o contexto.
    """
    partes = ["🤝 INDICAÇÃO (veio de quem recusou — NÃO é lead do próprio número)."]
    if nome_indicado:
        partes.append(f"Indicado: {nome_indicado}.")
    if contato:
        partes.append(f"Contato: {contato}.")
    if observacao:
        partes.append(f"Obs: {observacao}")
    partes.append(f"Indicado por: {nome or _lead_nome or _phone or '?'}.")
    contexto = " ".join(partes)
    log.info("pessoa_indicacao", tem_contato=bool(contato))

    if not (contato or nome_indicado):
        return {"status": "ok", "registrado": False, "aviso": "sem contato nem nome"}
    if not _phone:
        return {"status": "ok", "registrado": False, "aviso": "sem telefone no contexto"}
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return {"status": "ok", "registrado": True, "pytest": True}

    try:
        from app.db import models as _models
        from app.db.session import SessionLocal

        with SessionLocal() as s:
            s.add(_models.LeadHandoffNote(
                telefone=_phone,
                lead_nome=nome or _lead_nome,
                corretora_id=HANDOFF_CORRETORA_ID,
                contexto=contexto,
                origem="pessoa_indicacao",
                status="ativo",
            ))
            s.commit()
        return {"status": "ok", "registrado": True}
    except Exception as e:  # noqa: BLE001
        log.error("pessoa_indicacao_erro", erro=str(e)[:160])
        return {"status": "ok", "registrado": False, "erro": str(e)[:120]}


def _tool_encerrar_nao_e_lead(motivo: str | None = None, nome: str | None = None,
                              _phone: str | None = None, _lead_nome: str | None = None,
                              **_: Any) -> dict[str, Any]:
    """Quem está do outro lado NÃO é comprador — encerra e tira da roda.

    Dona 30/07, lendo a primeira leva de conversas reais: um contato escreveu
    "MCMV simula avalia e pro soluto montamos até dez 2029 com ou sem
    intermediárias" — alguém OFERECENDO serviço, não procurando imóvel. A Pessoa
    tratou como comprador e seguiu qualificando.

    Sem isso o contato continuaria na roda e receberia disparo de novo em 30
    dias, pra sempre.
    """
    log.info("pessoa_nao_e_lead", motivo=(motivo or "")[:120])
    if not _phone:
        return {"status": "ok", "encerrado": False, "aviso": "sem telefone"}
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return {"status": "ok", "encerrado": True, "pytest": True}
    try:
        from app.services.atendimento_corretor import marcar_nao_e_lead

        return {"status": "ok", "encerrado": marcar_nao_e_lead(_phone, motivo)}
    except Exception as e:  # noqa: BLE001
        log.error("pessoa_nao_e_lead_erro", erro=str(e)[:160])
        return {"status": "ok", "encerrado": False, "erro": str(e)[:120]}


TOOL_HANDLERS = {
    # `simular_caixa` saiu daqui em 02/08/2026 — a Pessoa não simula mais.
    "consultar_empreendimentos": _tool_consultar_empreendimentos,
    "registrar_qualificacao": _tool_registrar_qualificacao,
    "encaminhar_avaliacao": _tool_encaminhar_avaliacao,
    "encerrar_nao_e_lead": _tool_encerrar_nao_e_lead,
    "registrar_indicacao": _tool_registrar_indicacao,
}


def _load_soul() -> str:
    if not SOUL_PATH.exists():
        raise FileNotFoundError(f"SOUL.md não encontrado em {SOUL_PATH}")
    return SOUL_PATH.read_text(encoding="utf-8")


_TOOLS_BLOCK = (
    "Tools disponíveis (chame via campo tool_call do JSON):\n"
    "- consultar_empreendimentos(regiao?, valor_max?, quartos?) — catálogo vivo da Acme. "
    "Use pra entender o que casa com o lead (uso INTERNO; na Fase 1 não despeje opções pro lead).\n"
    "- registrar_qualificacao(nome, regiao, tipo, quartos, para_quem, faixa_valor, momento, "
    "entrada, quente, renda?, data_nascimento?, fgts?, empreendimento_sugerido?, observacao?) — "
    "ENCERRA seu atendimento. Chame assim que tiver o mínimo (nome + o que busca + renda) — não "
    "continue conversando depois disso. Ela passa o lead pra Pessoa3, manda a msg no outro "
    "número e a sua última fala no 0000 avisa: já mandou mensagem pelo outro número pra "
    "continuar o atendimento. Chame UMA vez só.\n\n"
    "- encaminhar_avaliacao(nome?, imovel?, regiao?, motivo?, observacao?) — quando o lead "
    "quer AVALIAR ou VENDER um imóvel dele (não é compra). Chame assim que ficar claro.\n"
    "- encerrar_nao_e_lead(motivo) — quando quem está do outro lado NÃO é comprador "
    "(oferece serviço, é corretor/parceiro, ligou errado). Chame e encerre educada.\n"
    "- registrar_indicacao(contato, nome_indicado, observacao) — quem recusou passou o contato "
    "de um amigo/parente que está procurando. Registra pra Pessoa3 chamar. NÃO promove quem "
    "recusou e NÃO manda mensagem pro indicado — quem aborda é a humana.\n\n"
    "Uma pergunta por vez. Nunca chame tool antes de ter o que ela precisa."
)


class AgentePessoa:
    """Agente conversacional de atendimento. Roda via get_llm_provider() (claude -p)."""

    def __init__(self, phone: str | None = None, lead_nome: str | None = None,
                 history: list[dict[str, Any]] | None = None, provider: Any | None = None) -> None:
        self.phone = phone
        self.lead_nome = lead_nome
        self.history = history or []
        self.soul = _load_soul()
        self._provider = provider

    @property
    def tools(self) -> list[str]:
        return list(TOOL_HANDLERS.keys())

    def _history_block(self) -> str:
        if not self.history:
            return ""
        lines = ["Mensagens recentes da conversa:"]
        for h in self.history:
            who = "você (Pessoa)" if h.get("role") == "assistant" else "lead"
            lines.append(f"{who}: {h.get('text', '')}")
        return "\n".join(lines)

    def _build_system_prompt(self) -> str:
        # Persona = identidade Pessoa (SOUL) + camada de conversa compartilhada
        # (TOM da Pessoa11 + régua Pessoa) + tools + histórico. A base de conversa é
        # fonte única, a mesma que a Corretor usa; a Pessoa veste sua identidade por cima.
        # Usa "tom" (só o jeito Pessoa11 de conversar): o roteiro da Pessoa vive na SOUL
        # dela — ela QUALIFICA e PASSA pra Pessoa12 (não simula, não marca visita), então
        # não herda o playbook de resgate genérico.
        parts = [
            self.soul,
            "---",
            "# Como você conversa (jeito Pessoa11 — provado em conversa real)",
            aplicar_dna("tom"),
            "---",
            "# A régua que guia o que você descobre (método Pessoa)",
            aplicar_metodo(),
            "---",
            _TOOLS_BLOCK,
        ]
        hb = self._history_block()
        if hb:
            parts += ["---", hb]
        return "\n\n".join(parts)

    def _execute_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name not in TOOL_HANDLERS:
            return {"status": "error", "message": f"tool desconhecida: {name}"}
        # Injeta identidade do lead (telefone/nome) nas tools que persistem handoff —
        # o LLM não tem/não deve inventar o telefone; ele vem do contexto da conversa.
        if name in ("registrar_qualificacao", "encaminhar_avaliacao",
                    "encerrar_nao_e_lead"):
            args = {**args, "_phone": self.phone, "_lead_nome": self.lead_nome}
        try:
            return TOOL_HANDLERS[name](**args)
        except TypeError as e:
            return {"status": "error", "message": f"argumentos inválidos: {e}"}

    def _provider_complete(self, system: str, user: str) -> str:
        provider = self._provider or get_llm_provider()
        json_str, _ = provider.complete(system=system, user=user, json_schema=RESPONSE_SCHEMA)
        return json_str

    def _provider_complete_com_retry(self, system: str, user: str) -> str:
        """Retry com backoff por cima do provider (que já tem o fallback dele).

        Falha marcada `transient=False` pelo último nível (cota/auth) não ganha
        nova tentativa: repetir só atrasa a resposta que vai pra fila.
        """
        ultimo: Exception | None = None
        for tentativa in range(1, PROVIDER_TENTATIVAS + 1):
            try:
                return self._provider_complete(system, user)
            except Exception as e:  # noqa: BLE001 — qualquer falha vira retry/fila
                ultimo = e
                permanente = getattr(e, "transient", True) is False
                log.warning(
                    "pessoa_provider_falhou",
                    tentativa=tentativa,
                    permanente=permanente,
                    error=str(e)[:200],
                )
                if permanente or tentativa >= PROVIDER_TENTATIVAS:
                    break
                _dormir(PROVIDER_BACKOFF_S[min(tentativa - 1, len(PROVIDER_BACKOFF_S) - 1)])
        raise MariaProviderIndisponivel(str(ultimo)[:300]) from ultimo

    def run(self, message: str) -> dict[str, Any]:
        system = self._build_system_prompt()
        user_buffer = message
        tool_calls_log: list[dict[str, Any]] = []
        last_text = ""

        for iteration in range(MAX_TOOL_ITERATIONS):
            try:
                json_str = self._provider_complete_com_retry(system, user_buffer)
            except MariaProviderIndisponivel as e:
                # Último recurso. Quem chama (atendimento_corretor) ENFILEIRA o turno
                # pra retomada — a frase sozinha, sem fila, foi o incidente.
                log.error("pessoa_provider_error", error=str(e), tentativas=PROVIDER_TENTATIVAS)
                return {
                    "response_text": FRASE_PROBLEMINHA,
                    "tool_calls": tool_calls_log,
                    "provider_falhou": True,
                    "erro": str(e),
                }
            try:
                parsed = json.loads(json_str)
            except json.JSONDecodeError as e:
                log.error("pessoa_invalid_json", error=str(e), raw=json_str[:200])
                return {"response_text": "Pera, me explica de novo? 😊", "tool_calls": tool_calls_log}

            text = parsed.get("text", "")
            last_text = text or last_text
            tool_call = parsed.get("tool_call")
            if not tool_call:
                return {"response_text": text, "tool_calls": tool_calls_log}

            name = tool_call.get("name")
            args = tool_call.get("args") or {}
            log.info("pessoa_tool_call", name=name, iteration=iteration)
            result = self._execute_tool(name, args)
            tool_calls_log.append({"name": name, "args": args, "result": result})

            # Handoff: a abertura na Pessoa12 já saiu na tool. Fecha a conversa no 0000
            # com o texto fixo (Dona: avisar que JÁ mandou pelo outro número).
            if name == "registrar_qualificacao" and result.get("handoff"):
                fechamento = (
                    result.get("mensagem_lead")
                    or FECHAMENTO_HANDOFF
                )
                return {"response_text": fechamento, "tool_calls": tool_calls_log}

            user_buffer = (
                f"{message}\n\n"
                f"[resultado da tool {name}]: {json.dumps(result, ensure_ascii=False)}\n\n"
                "Use esse resultado pra responder ao lead de forma natural, no seu tom."
            )

        return {"response_text": last_text or "Já já te respondo 😊", "tool_calls": tool_calls_log}
