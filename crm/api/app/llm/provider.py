"""
Abstração de provider LLM para o Lead Inspector.

Permite trocar de Claude CLI (MVP, Max plan da Dona) para
Anthropic API (SaaS, billing por token) sem alterar classifier/analyzer.
"""
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass
class TokenUsage:
    input_tokens: int
    output_tokens: int

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


@runtime_checkable
class LLMProvider(Protocol):
    def complete(
        self,
        system: str,
        user: str,
        json_schema: dict | None = None,
    ) -> tuple[str, TokenUsage]:
        """
        Envia prompt e retorna (json_string, tokens_usados).

        Args:
            system: instrução de sistema / persona
            user: conteúdo do usuário (lead + conversa)
            json_schema: se fornecido, provider instrui o modelo a
                         retornar JSON conforme esse schema

        Returns:
            Tuple de (json_string, TokenUsage).
            Em caso de falha irrecuperável: raise RuntimeError.
        """
        ...

    def complete_text(
        self,
        system: str,
        user: str,
    ) -> tuple[str, TokenUsage]:
        """
        Envia prompt e retorna texto livre (sem extração de JSON).

        Args:
            system: instrução de sistema / persona
            user: conteúdo do usuário

        Returns:
            Tuple de (texto_bruto, TokenUsage).
        """
        ...
