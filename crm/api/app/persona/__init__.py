"""
Persona compartilhada do squad Acme — régua/tom destilados como conhecimento reusável.

Onda 1 do refactor (aditiva): expõe o **método Pessoa** como módulo limpo, fonte única,
consumível por Copiloto (análise/coaching) e pelos agentes de atendimento (Corretor/Pessoa).
Ver `MAPA-ALVO-SQUAD-ACME.md` §3a/§4.
"""

from app.persona.metodo_pessoa_e import METODO_PESSOA_E, aplicar_metodo

__all__ = ["METODO_PESSOA_E", "aplicar_metodo"]
