"""Assistente IA — atendimento de Instagram da Acme, em sombra.

Recebe comentário e DM do @acme_imoveis, verifica elegibilidade, monta
rascunho e põe numa fila para uma pessoa revisar. **Aprovar grava uma decisão
e nada mais**: não existe função, endpoint ou credencial de saída neste pacote.
Quem envia é uma pessoa, no app, com autorização da Dona — e o que ela fez fica
registrado em `RegistroExecucaoManual`.

Módulos (ver `projetos/acme/OS-ASSISTENTE-IA.md` para o dono de cada tarefa):

| Arquivo | Tarefa | O que faz |
|---|---|---|
| `contracts.py` | 1 | tipos, estados, janelas, tetos e interfaces (este é o contrato) |
| `ATENDIMENTO.md` + `conhecimento.json` | 2 | manual de conduta e fatos comerciais com fonte |
| `repository.py` | 3 | persistência idempotente e auditoria — **não existe** |
| `ingest.py` | 4A | ingestão em sombra (o receptor do repasse do painel-corretor) |
| `meta_reader.py` | 4 | leitura GET na Graph — **não existe** |
| `policy.py`, `identity.py`, `drafts.py`, `config.py` | 5 | elegibilidade, opt-out, rascunhos — **não existem** |
| `lead_proposals.py` | 6 | proposta de cadastro — **não existe** |
| `lead_writer.py` | 9 | grava em `public.leads`, fora do intake |
| `sender.py` | 10 | envio assistido, **desarmado** |

Em 21/09/2026 existem `contracts.py`, `ingest.py`, `lead_writer.py` e
`sender.py`. A tarefa 3 é a que falta pra valer: sem o repositório **durável**
de reservas, `sender.armado()` devolve `False` mesmo com a flag ligada, e a
idempotência da ingestão só dura até o próximo restart.

O import abaixo é deliberadamente enxuto: quem consome pede de `contracts` o que
precisa, e nenhum módulo de saída aparece aqui — quem for enviar importa
`sender` de propósito, com o nome à vista.
"""
from __future__ import annotations

from app.services.instagram_acme.contracts import (  # noqa: F401
    CONTA_ANUNCIOS,
    CONTA_INSTAGRAM,
    ORIGEM_LEAD,
    PAGINA_FACEBOOK,
    ROTA_PAINEL,
    ROTA_WEBHOOK,
    TENANT_ID,
    TETOS_PADRAO,
    AcaoProposta,
    ClasseComentario,
    EstadoProposta,
    EventoNormalizado,
    IdentidadeSocial,
    MotivoBloqueio,
    Plataforma,
    TetosAssistente,
    TipoEvento,
    recusar_envio,
)

__all__ = [
    "CONTA_ANUNCIOS",
    "CONTA_INSTAGRAM",
    "ORIGEM_LEAD",
    "PAGINA_FACEBOOK",
    "ROTA_PAINEL",
    "ROTA_WEBHOOK",
    "TENANT_ID",
    "TETOS_PADRAO",
    "AcaoProposta",
    "ClasseComentario",
    "EstadoProposta",
    "EventoNormalizado",
    "IdentidadeSocial",
    "MotivoBloqueio",
    "Plataforma",
    "TetosAssistente",
    "TipoEvento",
    "recusar_envio",
]
