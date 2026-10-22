"""
Módulo Recovery — reativação de leads parados no funil Remarketing da Acme.

Fluxo:
  Entrada (manual) → API (HSM disparado) → Em atendimento IA (Corretor conversa)
  → Respondida (corretora pega) / Perdido (sem interesse) / Opt-out

Agente Corretor (Claude Opus 4.7) qualifica em até 4 trocas:
  1. Ainda quer comprar? (Acme não trabalha aluguel)
  2. Região de interesse em Cidade
  3. Tem FGTS pra entrada?
  4. Faixa de valor pretendida

Não é um agente Agno scheduled — é event-driven (webhook Evolution → responder).
"""
