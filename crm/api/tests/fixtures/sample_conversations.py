"""
Conversas realistas de imobiliária para validação manual da IA do Inspector.

Não são testes pytest — são fixtures de dados para scripts/validate_inspector_ai.py.
"""
from typing import Any

# ── Cenário A: Lead engajado que pediu visita ─────────────────────────────────

LEAD_A = {
    "lead_id": "fake_A",
    "nome": "Lead Teste A",
    "telefone": "+5500000000000",
    "corretora_id": 3,
    "etapa_nome": "Primeiro Contato",
    "origem": "Instagram",
}

CONV_A: list[dict[str, Any]] = [
    {"from": "lead",      "text": "Oi, vi o anúncio do apto Bairro X",              "at": "2026-05-04T10:00:00-03:00", "type": "text"},
    {"from": "corretora", "text": "Oi! Tudo bem? É 2 quartos, 80m², R$380mil",          "at": "2026-05-04T10:15:00-03:00", "type": "text"},
    {"from": "lead",      "text": "O bairro é exatamente o que eu queria. Dá pra ver no sábado?", "at": "2026-05-04T11:30:00-03:00", "type": "text"},
    {"from": "corretora", "text": "Que bom! Qualquer coisa me chama 😊",                "at": "2026-05-04T11:45:00-03:00", "type": "text"},
]

EXPECTATIVA_A = """
- **Status esperado:** atendido
- **Temperatura esperada:** quente
- **Gap esperado:** corretora não respondeu o pedido de visita no sábado; resposta genérica e vazia
- **Sugestão esperada:** oferecer horário concreto para a visita no sábado
""".strip()


# ── Cenário B: Lead pediu valor e sumiu ───────────────────────────────────────

LEAD_B = {
    "lead_id": "fake_B",
    "nome": "Lead Teste B",
    "telefone": "+5500000000000",
    "corretora_id": 3,
    "etapa_nome": "Qualificação",
    "origem": "Portais",
}

CONV_B: list[dict[str, Any]] = [
    {"from": "lead",      "text": "Oi, quanto tá esse apto?",                          "at": "2026-05-04T14:00:00-03:00", "type": "text"},
    {"from": "corretora", "text": "Oi! R$ 420.000",                                   "at": "2026-05-04T14:05:00-03:00", "type": "text"},
    {"from": "lead",      "text": "Ah ok. Vou pensar e te aviso",                     "at": "2026-05-04T14:10:00-03:00", "type": "text"},
    {"from": "corretora", "text": "Beleza, qualquer coisa é só falar 👍",              "at": "2026-05-04T14:11:00-03:00", "type": "text"},
]

EXPECTATIVA_B = """
- **Status esperado:** atendido
- **Temperatura esperada:** morno (engajou mas saiu sem decisão)
- **Gap esperado:** corretora mandou valor sem qualificar o lead; aceitou "vou pensar" sem reengajar ou propor próximo passo
- **Sugestão esperada:** qualificar antes de mandar valor / oferecer próximo passo concreto
""".strip()


# ── Cenário C: Lead respondeu, corretora sumiu ────────────────────────────────

LEAD_C = {
    "lead_id": "fake_C",
    "nome": "Lead Teste C",
    "telefone": "+5500000000000",
    "corretora_id": 3,
    "etapa_nome": "Primeiro Contato",
    "origem": "WhatsApp Ads",
}

CONV_C: list[dict[str, Any]] = [
    {"from": "lead",      "text": "Olá, vi o anúncio do imóvel na Bairro X",              "at": "2026-05-04T09:00:00-03:00", "type": "text"},
    {"from": "corretora", "text": "Oi! Posso te mandar mais detalhes. Qual sua faixa de preço?", "at": "2026-05-04T09:30:00-03:00", "type": "text"},
    {"from": "lead",      "text": "Até 350mil. Tem financiamento?",                              "at": "2026-05-04T10:00:00-03:00", "type": "text"},
]

EXPECTATIVA_C = """
- **Status esperado:** sem_resposta (lead aguardando, corretora não voltou)
- **Temperatura esperada:** quente (mencionou financiamento = sinal de intenção forte)
- **Gap esperado:** lead perguntou sobre financiamento e ficou sem resposta; corretora sumiu
- **Sugestão esperada:** responder sobre financiamento e tentar fechar visita
""".strip()


# ── Índice ────────────────────────────────────────────────────────────────────

SCENARIOS: list[dict[str, Any]] = [
    {
        "id": "A",
        "titulo": "Lead engajado que pediu visita",
        "lead": LEAD_A,
        "conversa": CONV_A,
        "expectativa": EXPECTATIVA_A,
    },
    {
        "id": "B",
        "titulo": "Lead pediu valor e sumiu",
        "lead": LEAD_B,
        "conversa": CONV_B,
        "expectativa": EXPECTATIVA_B,
    },
    {
        "id": "C",
        "titulo": "Lead respondeu, corretora sumiu",
        "lead": LEAD_C,
        "conversa": CONV_C,
        "expectativa": EXPECTATIVA_C,
    },
]
