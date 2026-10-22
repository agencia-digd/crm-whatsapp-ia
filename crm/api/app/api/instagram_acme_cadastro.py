"""Endpoint de cadastro efetivo da Assistente — tarefa 9, liberada pela
`OS-ASSISTENTE-IA-TAREFA-9E10.md`.

`POST /painel/instagram-acme/propostas/{proposta_id}/cadastrar`

## Divergência declarada com o contrato da tarefa 1

Este caminho está listado em `contracts.ROTAS_PROIBIDAS_V1`. Era para estar: a
constante existe justamente para que criar esta rota fosse **decisão
consciente com revisão da OS**, e não um `@router.post` a mais num arquivo. A
revisão aconteceu (OS 9E10, autorização da Dona de 21/09/2026 09:29 UTC) e a
rota nasce aqui, num módulo separado, fora de `ENDPOINTS_PAINEL`.

O que **não** mudou: `contracts.py` não foi tocado (é de outro executor), a
fila de revisão da V1 segue sem botão de cadastrar, e este router **não é
registrado em `app/main.py`** — registrar é tarefa de integração própria, com
nova autorização. Enquanto isso a rota não existe em produção.

Pendência para o dono da tarefa 1: `ROTAS_PROIBIDAS_V1` precisa deixar de
listar `.../cadastrar` (e `.../enviar`, da tarefa 10) ou ganhar uma nota de que
elas passaram a existir fora da V1. Ver `RELATORIO-9E10.md`.

## Por que a proposta chega por um provedor injetado

O repositório de propostas é da tarefa 3 e o montador de proposta de cadastro é
da tarefa 6 — nenhum dos dois existe hoje. Em vez de inventar schema de outro
executor, este módulo declara um provedor (`definir_provedor`) cujo padrão
**recusa** com 503. É o mesmo movimento que a tarefa 4A fez com o repositório
de eventos: a porta existe, testada, e não abre até a peça que falta chegar.
"""
from __future__ import annotations

from typing import Any, Callable, Protocol

import structlog
from fastapi import APIRouter, Body, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.services.instagram_acme.contracts import (
    ROTA_PAINEL,
    PropostaCadastroLead,
    exigir_versao,
)
from app.services.instagram_acme.lead_writer import (
    AutorizacaoCadastro,
    CadastroBloqueado,
    ContextoCadastro,
    ResultadoCadastro,
    gravar_cadastro,
)

log = structlog.get_logger()

router = APIRouter(tags=["instagram-acme-cadastro"])

#: A rota. Escrita aqui, e não em `contracts.ENDPOINTS_PAINEL`, porque a fila de
#: revisão da V1 continua sem ela.
ROTA_CADASTRO = f"{ROTA_PAINEL}/propostas/{{proposta_id}}/cadastrar"


# ── Provedor de proposta (tarefas 3 e 6) ─────────────────────────────────────


class ProvedorProposta(Protocol):
    def __call__(
        self, tenant_id: int, proposta_id: str
    ) -> tuple[PropostaCadastroLead, ContextoCadastro] | None: ...


def _provedor_indisponivel(
    tenant_id: int, proposta_id: str
) -> tuple[PropostaCadastroLead, ContextoCadastro] | None:
    raise HTTPException(503, "repositorio_de_propostas_indisponivel")


_provedor: ProvedorProposta = _provedor_indisponivel


def definir_provedor(provedor: ProvedorProposta) -> None:
    """Ponto de troca para as tarefas 3/6 ligarem o repositório real no boot."""
    global _provedor
    _provedor = provedor


def provedor_atual() -> ProvedorProposta:
    return _provedor


# ── Sessão (injetável no teste) ──────────────────────────────────────────────

SessionFactory = Callable[[], Any]


def _session_factory_padrao() -> Any:
    from app.db.session import SessionLocal

    return SessionLocal()


_session_factory: SessionFactory = _session_factory_padrao


def definir_session_factory(fabrica: SessionFactory) -> None:
    global _session_factory
    _session_factory = fabrica


# ── Autenticação ─────────────────────────────────────────────────────────────


def _usuario(remote_user: str | None) -> str:
    """Mesma autenticação do painel: `X-Painel-User` posto pelo nginx, validado
    no backend. Cadastro é operação de admin — corretora não grava lead da
    Assistente, e quem é corretora é decidido aqui, nunca pelo navegador."""
    from app.api.painel import _corretora_id_do_user, _identificar_usuario

    user = _identificar_usuario(remote_user)
    if _corretora_id_do_user(user) is not None:
        raise HTTPException(403, "cadastro_assistente_exige_admin")
    return user


# ── Payload ──────────────────────────────────────────────────────────────────


class PedidoCadastro(BaseModel):
    """Corpo do pedido. `versao` é a revisão otimista: quem manda cadastrar
    manda a versão que leu, e uma edição no meio recusa a gravação."""

    model_config = ConfigDict(extra="forbid")

    autorizacao_dona_ref: str = Field(min_length=1)
    versao: int = Field(ge=1)


class RespostaCadastro(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lead_id: int
    criado: bool
    anexado: bool
    repetido: bool
    owner_id: int | None
    stage_id: int | None
    origem: str


@router.post(ROTA_CADASTRO, response_model=RespostaCadastro)
def cadastrar(
    proposta_id: str,
    pedido: PedidoCadastro = Body(...),
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> RespostaCadastro:
    """Grava a proposta aprovada em `public.leads`.

    422 com a lista de motivos quando a revalidação recusa — e nesse caso nada
    foi escrito. 409 quando alguém editou a proposta entre a leitura e o clique.
    """
    user = _usuario(x_painel_user)

    from app.services.instagram_acme.contracts import TENANT_ID

    carregado = provedor_atual()(TENANT_ID, proposta_id)
    if carregado is None:
        raise HTTPException(404, "proposta_nao_encontrada")
    proposta, contexto = carregado

    try:
        exigir_versao(proposta, pedido.versao)
    except Exception as exc:  # VersaoDesatualizada
        raise HTTPException(409, "versao_desatualizada") from exc

    session = _session_factory()
    try:
        resultado: ResultadoCadastro = gravar_cadastro(
            proposta,
            autorizacao=AutorizacaoCadastro(
                autorizacao_dona_ref=pedido.autorizacao_dona_ref,
                responsavel=user,
            ),
            session=session,
            contexto=contexto,
        )
    except CadastroBloqueado as exc:
        log.warning(
            "assistente_cadastro_recusado",
            proposta_id=proposta_id, user=user, motivos=list(exc.motivos),
        )
        raise HTTPException(422, {"erro": "cadastro_bloqueado", "motivos": list(exc.motivos)})
    finally:
        session.close()

    return RespostaCadastro(
        lead_id=resultado.lead_id,
        criado=resultado.criado,
        anexado=resultado.anexado,
        repetido=resultado.repetido,
        owner_id=resultado.owner_id,
        stage_id=resultado.stage_id,
        origem=resultado.origem,
    )


__all__ = [
    "ROTA_CADASTRO",
    "PedidoCadastro",
    "RespostaCadastro",
    "definir_provedor",
    "definir_session_factory",
    "provedor_atual",
    "router",
]
