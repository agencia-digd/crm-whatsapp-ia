"""Testes de wiring — Onda 2 do refactor do squad Acme.

Garante que os consumidores da persona compartilhada (`dna_corretor` + `metodo_pessoa_e`)
estão de fato acoplados às fontes únicas:

(a) `atendimento_brain.METODO` continua IDÊNTICO à régua Pessoa (fonte única, sem drift).
(b) `CORRETOR_SYSTEM_PROMPT` carrega o TOM da Pessoa11 (sem o playbook de fechar) E o método
    Pessoa, sem perder a identidade específica da Corretor (auto-reply, LGPD/opt-out,
    formato, saída <state>). Escopo Onda 2b: a Corretor reaquece e TRANSFERE — não roda
    simulação nem agenda visita — logo NÃO herda o PLAYBOOK_RESGATE.
(c) o system prompt da Pessoa contém o tom da Pessoa11 + método Pessoa + a identidade
    dela. Escopo desde 02/08/2026: a Pessoa QUALIFICA e PASSA pra Pessoa12 — não simula
    nem agenda visita; o roteiro de fechar vive na SOUL dela, então ela também
    usa "tom", sem o PLAYBOOK_RESGATE.
"""

from __future__ import annotations

from app.agents.pessoa.agent import AgentePessoa
from app.persona.dna_corretor import DNA_CORRETOR, PLAYBOOK_RESGATE  # noqa: F401
from app.persona.metodo_pessoa_e import METODO_PESSOA_E
from app.recovery.prompts import CORRETOR_SYSTEM_PROMPT
from app.services.atendimento_brain import METODO as METODO_BRAIN


# ── (a) fonte única do método ────────────────────────────────────────────────

def test_atendimento_brain_usa_a_fonte_unica_do_metodo() -> None:
    """atendimento_brain.METODO agora É a régua Pessoa (mesma string, sem drift)."""
    assert METODO_BRAIN == METODO_PESSOA_E


def test_atendimento_brain_comportamento_preservado() -> None:
    """O cérebro montado segue contendo a régua diagnóstica (comportamento idêntico)."""
    from app.services.atendimento_brain import brain

    texto = brain(sugerir=True)
    assert "Pare de tentar CONVENCER" in texto
    assert "AS 9 COISAS QUE PRECISAM FICAR CLARAS" in texto


# ── (b) Corretor = identidade + DNA Pessoa11 + método Pessoa ─────────────────────────

def test_corretor_prompt_carrega_tom_corretor_sem_playbook() -> None:
    """A Corretor conversa no TOM da Pessoa11 (princípios), mas NÃO herda o playbook de fechar.

    Ela reaquece e transfere — quem roda simulação e agenda visita é a corretora.
    """
    assert DNA_CORRETOR in CORRETOR_SYSTEM_PROMPT
    assert PLAYBOOK_RESGATE not in CORRETOR_SYSTEM_PROMPT
    assert "termina em pergunta" in CORRETOR_SYSTEM_PROMPT.lower()


def test_corretor_prompt_deixa_explicito_que_transfere() -> None:
    """Identidade da Corretor deixa claro o papel: reaquece, sonda visita/Caixa, transfere."""
    txt = CORRETOR_SYSTEM_PROMPT.lower()
    assert "transfer" in txt  # transferir / transfere / transferência
    assert "não roda a simulação" in txt or "não roda simulação" in txt
    assert "não marca visita" in txt


def test_corretor_prompt_carrega_metodo_pessoa_e() -> None:
    """A Corretor agora tem a régua diagnóstica que não tinha antes."""
    assert METODO_PESSOA_E in CORRETOR_SYSTEM_PROMPT
    assert "Pare de tentar CONVENCER" in CORRETOR_SYSTEM_PROMPT


def test_corretor_prompt_preserva_identidade_especifica() -> None:
    """Nada do que é EXCLUSIVO da Corretor pode ter se perdido na recomposição."""
    # identidade + escopo
    assert "Você é a Corretor" in CORRETOR_SYSTEM_PROMPT
    assert "Acme Imóveis" in CORRETOR_SYSTEM_PROMPT
    # auto-reply / não-lead-real
    assert "auto-reply" in CORRETOR_SYSTEM_PROMPT
    assert 'reason="opt_out"' in CORRETOR_SYSTEM_PROMPT
    # LGPD / opt-out / encerramento
    assert "não tenho mais interesse" in CORRETOR_SYSTEM_PROMPT
    assert "Marca como perdido" in CORRETOR_SYSTEM_PROMPT
    # saída estruturada
    assert "<state>" in CORRETOR_SYSTEM_PROMPT
    assert "qualified" in CORRETOR_SYSTEM_PROMPT


# ── (c) Pessoa = SOUL (identidade) + DNA Pessoa11 + método Pessoa ────────────────

def _pessoa_prompt() -> str:
    return AgentePessoa(phone="x", lead_nome="Lead")._build_system_prompt()


def test_pessoa_prompt_tem_as_duas_camadas_de_conversa() -> None:
    prompt = _pessoa_prompt()
    assert DNA_CORRETOR in prompt
    assert METODO_PESSOA_E in prompt
    # Onda 2b: o roteiro de fechar vive na SOUL da Pessoa; ela usa "tom", não o
    # playbook de resgate genérico.
    assert PLAYBOOK_RESGATE not in prompt


def test_pessoa_prompt_descreve_qualifica_e_passa() -> None:
    """A SOUL descreve o fluxo novo (Dona 02/08): qualifica → renda → passa.

    O anterior (simula Caixa → oferece 2 horários → transfere) foi aposentado: a
    simulação demorava 1-2 min e prendia o lead num chat automático depois de ele
    já ter dado tudo que importava.
    """
    prompt = _pessoa_prompt().lower()
    assert "renda" in prompt                      # gatilho de saída
    assert "registrar_qualificacao" in prompt     # como ela encerra
    assert "pessoa humana" in prompt               # passa pra humana


def test_pessoa_nao_pode_mais_simular() -> None:
    """Fora do enum e do TOOL_HANDLERS o LLM não consegue nem emitir o nome —
    a instrução da SOUL sozinha o modelo fura."""
    from app.agents.pessoa.agent import RESPONSE_SCHEMA, TOOL_HANDLERS

    assert "simular_caixa" not in TOOL_HANDLERS
    enum = RESPONSE_SCHEMA["properties"]["tool_call"]["properties"]["name"]["enum"]
    assert "simular_caixa" not in enum


def test_pessoa_prompt_preserva_identidade_dela() -> None:
    """A Pessoa continua sendo a corretora Pessoa (SOUL), não vira a Corretor."""
    prompt = _pessoa_prompt()
    assert "Pessoa" in prompt
    # âncora da SOUL da Pessoa (simula ser a corretora)
    assert "consultora da **Acme Imóveis**" in prompt or "Acme Imóveis" in prompt
    # o bloco de tools da Pessoa segue presente (schema/loop intactos)
    assert "consultar_empreendimentos" in prompt
    assert "registrar_qualificacao" in prompt
