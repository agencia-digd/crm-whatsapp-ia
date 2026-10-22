"""Endpoint de envio assistido da Assistente — tarefa 10, liberada em parte pela
`OS-ASSISTENTE-IA-TAREFA-9E10.md`.

`POST /painel/instagram-acme/propostas/{proposta_id}/enviar`

Mesma divergência declarada do módulo de cadastro: este caminho está em
`contracts.ROTAS_PROIBIDAS_V1`, onde foi posto para que criá-lo exigisse
revisão da OS. A revisão aconteceu. `contracts.py` não foi tocado, a rota vive
fora de `ENDPOINTS_PAINEL`, e **o router não é registrado em `app/main.py`** —
enquanto a integração não for autorizada, a rota não sobe.

Três coisas que este arquivo deliberadamente NÃO faz:

* **Não envia ao aprovar.** Não existe `on_approve`, hook, listener ou
  observador de fila. Envio é um POST separado, com autorização no corpo.
* **Não tem cron.** Nenhum timer, nenhum `@repeat_every`, nenhum consumo
  automático de fila. O teste varre a fonte.
* **Não manda em lote.** Um POST, uma proposta, uma pessoa. "Enviar todos" é o
  caminho mais curto entre uma fila e um bloqueio de conta.

`GET .../envio-status` existe para o operador ver em que estado a reserva
parou — inclusive `incerto`, que é o único caso em que alguém precisa olhar a
caixa de entrada do Instagram antes de decidir qualquer coisa.
"""
from __future__ import annotations

from typing import Any, Protocol

import structlog
from fastapi import APIRouter, Body, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.services.instagram_acme.contracts import (
    ROTA_PAINEL,
    TENANT_ID,
    EnvioNaoPermitido,
    PropostaBase,
    VersaoDesatualizada,
)
from app.services.instagram_acme.sender import (
    AcaoEnvio,
    AlvoEnvio,
    AutorizacaoEnvio,
    EnvioBloqueado,
    EstadoNoEnvio,
    armado,
    chave_de_reserva,
    enviar,
    repositorio_atual,
)

log = structlog.get_logger()

router = APIRouter(tags=["instagram-acme-envio"])

ROTA_ENVIO = f"{ROTA_PAINEL}/propostas/{{proposta_id}}/enviar"
ROTA_ENVIO_STATUS = f"{ROTA_PAINEL}/propostas/{{proposta_id}}/envio-status"


# ── Provedor (tarefas 3, 4 e 5) ──────────────────────────────────────────────


class ProvedorEnvio(Protocol):
    """Devolve tudo que o envio precisa revalidar, lido AGORA do banco.

    Quem implementa é a integração (tarefas 3/5): proposta, alvo e o estado do
    instante — opt-out, teto, identidade, conversa humana. A tela não manda
    nada disso.
    """

    def __call__(
        self, tenant_id: int, proposta_id: str
    ) -> tuple[PropostaBase, AlvoEnvio, EstadoNoEnvio] | None: ...


def _provedor_indisponivel(
    tenant_id: int, proposta_id: str
) -> tuple[PropostaBase, AlvoEnvio, EstadoNoEnvio] | None:
    raise HTTPException(503, "repositorio_de_propostas_indisponivel")


_provedor: ProvedorEnvio = _provedor_indisponivel


def definir_provedor(provedor: ProvedorEnvio) -> None:
    global _provedor
    _provedor = provedor


def provedor_atual() -> ProvedorEnvio:
    return _provedor


# ── Autenticação ─────────────────────────────────────────────────────────────


def _usuario(remote_user: str | None) -> str:
    from app.api.painel import _corretora_id_do_user, _identificar_usuario

    user = _identificar_usuario(remote_user)
    if _corretora_id_do_user(user) is not None:
        raise HTTPException(403, "envio_assistente_exige_admin")
    return user


# ── Payload ──────────────────────────────────────────────────────────────────


class PedidoEnvio(BaseModel):
    model_config = ConfigDict(extra="forbid")

    autorizacao_dona_ref: str = Field(min_length=1)
    versao: int = Field(ge=1)


class RespostaEnvio(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enviado: bool
    estado: str
    id_externo: str | None = None
    erro: str | None = None


@router.post(ROTA_ENVIO, response_model=RespostaEnvio)
def enviar_proposta(
    proposta_id: str,
    pedido: PedidoEnvio = Body(...),
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> RespostaEnvio:
    """Manda UMA mensagem. 423 quando o envio está desarmado (é o estado de
    hoje), 422 com os motivos quando a revalidação recusa, 409 quando a
    proposta mudou de versão entre a leitura e o clique."""
    user = _usuario(x_painel_user)

    carregado = provedor_atual()(TENANT_ID, proposta_id)
    if carregado is None:
        raise HTTPException(404, "proposta_nao_encontrada")
    proposta, alvo, estado = carregado

    try:
        resultado = enviar(
            proposta,
            alvo,
            estado,
            autorizacao=AutorizacaoEnvio(
                autorizacao_dona_ref=pedido.autorizacao_dona_ref,
                operador=user,
                versao=pedido.versao,
            ),
        )
    except EnvioNaoPermitido as exc:
        # 423 Locked: não é erro do pedido, é o sistema desarmado de propósito.
        log.warning("assistente_envio_desarmado_http", proposta_id=proposta_id, user=user)
        raise HTTPException(423, "envio_desarmado") from exc
    except VersaoDesatualizada as exc:
        raise HTTPException(409, "versao_desatualizada") from exc
    except EnvioBloqueado as exc:
        log.warning(
            "assistente_envio_recusado",
            proposta_id=proposta_id, user=user, motivos=list(exc.motivos),
        )
        raise HTTPException(422, {"erro": "envio_bloqueado", "motivos": list(exc.motivos)})

    return RespostaEnvio(
        enviado=resultado.enviado,
        estado=resultado.estado.value,
        id_externo=resultado.id_externo,
        erro=resultado.erro,
    )


@router.get(ROTA_ENVIO_STATUS)
def status_envio(
    proposta_id: str,
    versao: int,
    acao: AcaoEnvio,
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Estado da reserva. Leitura pura — não envia, não reserva, não reconcilia."""
    _usuario(x_painel_user)
    chave = chave_de_reserva(proposta_id, versao, acao)
    reserva = repositorio_atual().por_chave(chave)
    return {
        "armado": armado(),
        "chave": chave,
        "estado": reserva.estado.value if reserva else None,
        "tentativas": reserva.tentativas if reserva else 0,
        "id_externo": reserva.id_externo if reserva else None,
        "erro": reserva.erro if reserva else None,
    }


__all__ = [
    "ROTA_ENVIO",
    "ROTA_ENVIO_STATUS",
    "PedidoEnvio",
    "RespostaEnvio",
    "definir_provedor",
    "provedor_atual",
    "router",
]
