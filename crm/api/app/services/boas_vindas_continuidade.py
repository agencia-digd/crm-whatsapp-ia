"""Boas-vindas de CONTINUIDADE — pra quem acabou de falar no telefone.

O FLUXO COMPLETO (por que este módulo existe):

    lead do REMARKETING, sem dono
        → corretora liga (janela de ligação, aba "📞 Ligar" em /tarefas)
        → marca "atendeu e quer continuar"
        → ADOÇÃO: lead vai pra "Novo" (entrada do funil principal) + ganha a
          corretora como dona + recebe a TAG `leads.adotado_ligacao_em`
        → se esse lead voltar a passar pelo intake (ele preenche um anúncio,
          o webhook do Kommo repassa…), a boas-vindas AUTOMÁTICA dispararia
        → `lead_intake` consulta a tag e manda ESTA copy, não a padrão.

POR QUE ISSO É OBRIGATÓRIO: a copy padrão de boas-vindas diz "Você acabou de se
cadastrar com interesse em imóveis aqui na cidade de Cidade, certo?". Mandar
isso pra quem ACABOU de conversar por telefone com a corretora é constrangedor
e destrói a credibilidade da conversa que acabou de acontecer. A pessoa não
acabou de se cadastrar — ela acabou de falar com um ser humano.

⚠️ FORA DO TESTE A/B, DE PROPÓSITO. A boas-vindas roda um A/B de 3 variantes
(`boas_vindas_copies`: A controle, B triagem por intenção, C triagem por prazo).
Quem vem de ligação NÃO entra no sorteio: o contexto é outro (já houve conversa
por voz) e as respostas dessa gente contaminariam o resultado das três — a
variante que calhasse de pegar mais adotados pareceria vencedora sem ser. Por
isso a variante gravada no evento vem `None` e o payload marca
`continuidade=True`.

FORMATO: é o corpo ({{2}}) do template UTILITY `reeng_b`, o MESMO das outras.
O template já abre com "Olá {lead}, tudo bem?" e já fecha com "Para {CTA}, é só
tocar no botão abaixo." + botão [Saiba mais]. Então aqui: não cumprimentar de
novo, não repetir "Saiba mais", e TERMINAR COM PERGUNTA (trava da casa).
"""
from __future__ import annotations

import structlog

log = structlog.get_logger()

# Mesmo placeholder das variantes do A/B — quem renderiza é `lead_intake`.
PLACEHOLDER_NOME = "{nome}"

# Identificador que vai no payload do evento (no lugar do id da variante do
# A/B) — deixa auditável que este envio ficou fora do teste.
COPY_ID = "continuidade_ligacao"

TEXTO = (
    "Sou a {nome}, da Acme 😊 Acabamos de falar por telefone — tô te "
    "chamando por aqui pra a gente seguir com calma. Já vou separando o que "
    "conversamos. Prefere que eu te mande as opções ainda hoje?"
)


def render(nome_da_linha: str) -> str:
    """Corpo com o nome da corretora dona da linha no lugar do {nome}."""
    return TEXTO.replace(PLACEHOLDER_NOME, nome_da_linha)
