"""Endpoints da Camada 1 do banco de imóveis — captação da corretora.

Prefixo `/painel`, mesmo contrato de auth do resto do painel: o nginx faz Basic
Auth (ou o link mágico) e repassa `X-Painel-User`. Sem header → 403.

Os helpers de identidade vêm de `app.api.painel` de propósito, e não copiados
pra cá: `ADMINS` e `USUARIO_CORRETORA` são a lista de acesso da instância (vêm
de env) e duplicar mapa de permissão é o jeito garantido de ter duas regras
diferentes na primeira mudança. O `posts_corretora.py` fez a própria cópia e o
resultado é que a Pessoa7 existe num arquivo e não no outro.

Escopo: corretora vê e edita SÓ as captações dela; admin vê todas. A delegação
de atendimento (Pessoa12 cobre a linha da Pessoa7) NÃO se aplica: ela é sobre
conversa, e captação é propriedade — comissão nasce do `corretora_id`.

Fotos são servidas SÓ por `GET /painel/imoveis/fotos/{id}`, com o mesmo
escopo. Nunca por caminho público: tem telefone de dono e foto da casa de gente
real do outro lado.
"""
from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, File, Form, Header, HTTPException, Query, Response, UploadFile

from app.api.painel import (
    USUARIO_CORRETORA,
    _corretora_id_do_user,
    _identificar_usuario,
    _is_admin,
)
from app.db.session import SessionLocal
from app.services import imoveis_captados as svc

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/painel", tags=["imoveis-captados"])


def _viewer(x_painel_user: str | None) -> tuple[str, int | None, bool]:
    """(user, corretora_id, is_admin). corretora_id None = admin (vê tudo)."""
    user = _identificar_usuario(x_painel_user)
    admin = _is_admin(user)
    return user, (None if admin else _corretora_id_do_user(user)), admin


def _corretoras_disponiveis() -> list[dict[str, Any]]:
    """Pra o admin escolher a dona da captação no formulário."""
    from app.api.painel import CORRETORA_NOME_DB

    return [
        {"id": cid, "label": CORRETORA_NOME_DB.get(cid) or f"C{cid}"}
        for cid in sorted(set(USUARIO_CORRETORA.values()))
    ]


def _carregar(s, imovel_id: int, my_cid: int | None):  # type: ignore[no-untyped-def]
    from app.db.models import ImovelCaptado

    imovel = s.get(ImovelCaptado, imovel_id)
    if imovel is None:
        raise HTTPException(404, "imovel_nao_encontrado")
    if my_cid is not None and int(imovel.corretora_id) != int(my_cid):
        raise HTTPException(403, "imovel_de_outra_corretora")
    return imovel


def _carregar_foto(s, foto_id: int, my_cid: int | None):  # type: ignore[no-untyped-def]
    from app.db.models import ImovelFoto

    foto = s.get(ImovelFoto, foto_id)
    if foto is None:
        raise HTTPException(404, "foto_nao_encontrada")
    imovel = _carregar(s, foto.imovel_id, my_cid)
    return foto, imovel


# ── Carteira ──────────────────────────────────────────────────────────────────


@router.get("/imoveis")
def listar_imoveis(
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
    corretora_id: int | None = Query(default=None, description="só admin"),
    situacao: str | None = Query(default=None),
    q: str | None = Query(default=None, description="bairro, endereço, dono ou telefone"),
    limite: int = Query(default=200, ge=1, le=500),
) -> dict[str, Any]:
    user, my_cid, admin = _viewer(x_painel_user)
    with SessionLocal() as s:
        itens = svc.listar(
            s,
            corretora_id=my_cid,
            filtro_corretora=corretora_id if admin else None,
            situacao=situacao,
            busca=q,
            limite=limite,
        )
    return {
        "imoveis": [i.model_dump() for i in itens],
        "total": len(itens),
        "is_admin": admin,
        "corretoras": _corretoras_disponiveis() if admin else [],
        "situacoes": list(svc.SITUACOES),
        "tipos": list(svc.TIPOS),
    }


@router.post("/imoveis", status_code=201)
def criar_imovel(
    dados: svc.ImovelIn,
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Cria a captação. NADA aqui pode fazer o cadastro falhar.

    Duplicata não bloqueia: se houver parecido, o imóvel é salvo do mesmo jeito
    e o aviso volta em `avisos` pra tela mostrar depois de salvar.
    """
    user, my_cid, admin = _viewer(x_painel_user)

    if admin:
        dona = dados.corretora_id
        if not dona:
            raise HTTPException(400, "admin_precisa_escolher_corretora")
        if dona not in set(USUARIO_CORRETORA.values()):
            raise HTTPException(422, "corretora_desconhecida")
    else:
        # Corretora comum é sempre a dona — o campo do body é ignorado de
        # propósito (senão vira caminho pra captar no nome de outra).
        dona = my_cid
        if dona is None:
            raise HTTPException(403, "usuario_sem_corretora")

    with SessionLocal() as s:
        try:
            imovel, achados = svc.criar(
                s, dados, corretora_id=int(dona), captado_por=user
            )
        except Exception as e:  # noqa: BLE001
            log.error("imovel_captado_criar_falhou", por=user, erro=str(e)[:200])
            raise HTTPException(500, "nao_consegui_salvar") from e
        detalhe = svc.detalhe(s, imovel)

    return {
        "ok": True,
        "id": imovel.id,
        "imovel": detalhe["imovel"].model_dump(),
        "avisos": [p.model_dump() for p in achados],
    }


# ⚠️ ORDEM IMPORTA: `/imoveis/fotos/...` precisa ser declarado ANTES de
# `/imoveis/{imovel_id}`, senão o FastAPI casa "fotos" com o path param.


@router.get("/imoveis/fotos/{foto_id}")
def baixar_foto(
    foto_id: int,
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> Response:
    """Única porta de saída da foto. Não existe caminho público."""
    _user, my_cid, _admin = _viewer(x_painel_user)
    with SessionLocal() as s:
        foto, _imovel = _carregar_foto(s, foto_id, my_cid)
        try:
            conteudo, mime = svc.ler_foto(foto)
        except FileNotFoundError:
            raise HTTPException(404, "arquivo_sumiu_do_disco") from None
        except ValueError as e:
            log.error("imovel_foto_caminho_suspeito", foto_id=foto_id, erro=str(e)[:120])
            raise HTTPException(400, "caminho_invalido") from e
    return Response(
        content=conteudo,
        media_type=mime,
        headers={"Cache-Control": "private, max-age=86400"},
    )


@router.post("/imoveis/fotos/{foto_id}/logo")
def gerar_com_logo(
    foto_id: int,
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Gera a versão com a logo da Acme — ARQUIVO NOVO. Original intocada."""
    user, my_cid, _admin = _viewer(x_painel_user)
    with SessionLocal() as s:
        foto, imovel = _carregar_foto(s, foto_id, my_cid)
        try:
            nova = svc.aplicar_logo(s, imovel, foto, por=user)
        except svc.LogoIndisponivel as e:
            # Erro CLARO, e nada de logo improvisada (regra do plano).
            raise HTTPException(503, f"logo_indisponivel: {e}") from e
        except svc.PillowIndisponivel as e:
            raise HTTPException(503, f"tratamento_indisponivel: {e}") from e
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        except FileNotFoundError:
            raise HTTPException(404, "arquivo_sumiu_do_disco") from None
        return {
            "ok": True,
            "foto": svc.FotoOut(
                id=nova.id, ordem=nova.ordem, mime=nova.mime, largura=nova.largura,
                altura=nova.altura, bytes=nova.bytes, origem=nova.origem, tratada=True,
                tratada_de_id=nova.tratada_de_id, tratamento=nova.tratamento,
                autorizacao_dono=nova.autorizacao_dono, criado_em=nova.criado_em,
                url=f"/painel/imoveis/fotos/{nova.id}",
            ).model_dump(),
        }


@router.post("/imoveis/fotos/{foto_id}/arquivar")
def arquivar_foto(
    foto_id: int,
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Tira de circulação. Nada se apaga — é `arquivada=true`, não DELETE."""
    user, my_cid, _admin = _viewer(x_painel_user)
    with SessionLocal() as s:
        foto, imovel = _carregar_foto(s, foto_id, my_cid)
        svc.arquivar_foto(s, imovel, foto, por=user)
    return {"ok": True, "id": foto_id, "arquivada": True}


@router.put("/imoveis/{imovel_id}/fotos/ordem")
def reordenar_fotos(
    imovel_id: int,
    body: dict[str, Any],
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Body: {"ordem": [foto_id, ...]} com TODAS as fotos originais do imóvel.
    A primeira vira a capa. 409 se a lista não bate com o que está no banco."""
    user, my_cid, _admin = _viewer(x_painel_user)
    ordem = body.get("ordem")
    if not isinstance(ordem, list) or not all(
        isinstance(x, int) and not isinstance(x, bool) for x in ordem
    ):
        raise HTTPException(422, "ordem precisa ser a lista de ids das fotos")
    with SessionLocal() as s:
        imovel = _carregar(s, imovel_id, my_cid)
        try:
            svc.reordenar_fotos(s, imovel, ordem, por=user)
        except ValueError as e:
            raise HTTPException(409, str(e)) from e
        d = svc.detalhe(s, imovel)
    return {
        "ok": True,
        "fotos": [f.model_dump() for f in d["fotos"]],
        "imovel": d["imovel"].model_dump(),
    }


@router.get("/imoveis/{imovel_id}")
def ver_imovel(
    imovel_id: int,
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    _user, my_cid, admin = _viewer(x_painel_user)
    with SessionLocal() as s:
        imovel = _carregar(s, imovel_id, my_cid)
        d = svc.detalhe(s, imovel)
        leads, explicacao = svc.leads_interessados(s, imovel)
    return {
        "imovel": d["imovel"].model_dump(),
        "fotos": [f.model_dump() for f in d["fotos"]],
        "eventos": [e.model_dump() for e in d["eventos"]],
        "leads_interessados": {
            "total": len(leads),
            "explicacao": explicacao,
            "leads": [x.model_dump() for x in leads],
        },
        "is_admin": admin,
        "corretoras": _corretoras_disponiveis() if admin else [],
    }


@router.patch("/imoveis/{imovel_id}")
def editar_imovel(
    imovel_id: int,
    patch: svc.ImovelPatch,
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    user, my_cid, admin = _viewer(x_painel_user)
    with SessionLocal() as s:
        imovel = _carregar(s, imovel_id, my_cid)
        try:
            diff = svc.editar(s, imovel, patch, por=user, pode_transferir=admin)
        except PermissionError as e:
            raise HTTPException(403, str(e)) from e
        d = svc.detalhe(s, imovel)
    return {"ok": True, "alterado": list(diff), "imovel": d["imovel"].model_dump()}


@router.post("/imoveis/{imovel_id}/fotos", status_code=201)
async def subir_foto(
    imovel_id: int,
    arquivo: UploadFile = File(...),
    origem: str = Form("dono"),
    autorizacao_dono: bool = Form(False),
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Sobe UMA foto. O front já reduz no canvas antes (1600px / JPEG 0.8) —
    aqui o servidor reduz de novo como rede de segurança pra quem bate direto.

    🔴 `origem` só aceita 'dono' e 'corretora'. Foto de portal de terceiro não
    entra nem por engano: o CHECK do banco recusa (§3 do plano).
    """
    user, my_cid, _admin = _viewer(x_painel_user)
    conteudo = await arquivo.read()
    with SessionLocal() as s:
        imovel = _carregar(s, imovel_id, my_cid)
        try:
            foto = svc.salvar_foto(
                s, imovel, conteudo,
                mime=arquivo.content_type or "image/jpeg",
                origem=(origem or "dono").strip().lower(),
                autorizacao_dono=bool(autorizacao_dono),
                enviado_por=user,
            )
        except svc.PillowIndisponivel as e:
            raise HTTPException(503, f"tratamento_indisponivel: {e}") from e
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        except Exception as e:  # noqa: BLE001
            log.error("imovel_foto_falhou", imovel_id=imovel_id, erro=str(e)[:200])
            raise HTTPException(500, "nao_consegui_salvar_a_foto") from e
        d = svc.detalhe(s, imovel)
    return {
        "ok": True,
        "foto_id": foto.id,
        "fotos": [f.model_dump() for f in d["fotos"]],
        "imovel": d["imovel"].model_dump(),
    }


@router.get("/imoveis/{imovel_id}/leads-interessados")
def leads_interessados(
    imovel_id: int,
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """"N leads da base pediram isso" — cruzamento com o raio-X da Pessoa.

    ⚠️ Casa BAIRRO e TIPO por menção no texto. Faixa de valor e quartos vêm em
    português livre no raio-X e NÃO são cruzados — prometer isso com regex daria
    número que parece medido e não é.
    """
    _user, my_cid, _admin = _viewer(x_painel_user)
    with SessionLocal() as s:
        imovel = _carregar(s, imovel_id, my_cid)
        leads, explicacao = svc.leads_interessados(s, imovel)
    return {
        "total": len(leads),
        "explicacao": explicacao,
        "leads": [x.model_dump() for x in leads],
    }
