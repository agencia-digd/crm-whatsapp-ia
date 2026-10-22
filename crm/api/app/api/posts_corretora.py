"""Aba "Posts" do painel Acme — IDÊNTICA ao dashboard (agencia-central):
cada post tem aprovação separada de COPY e IMAGEM (copy_aprovada / imagem_aprovada),
e só entra na fila quando os dois estão ok.

O "Novo post" aqui é um PEDIDO/BRIEF (não post pronto):
- formato (carrossel | single | reel)
- url_referencia: link de um post-modelo que a corretora achou e colou
- observacao: o que ela quer ("copia a copy desse post, faz as imagens, fala sobre X")
- SEM data (entra sem agendamento; data definida depois)

Daí o OD/Assistente gera copy + imagem a partir do brief; corretora/admin aprova
copy e imagem separados. Posts vivem em tarefas.social_posts (DB agencia_cerebro
:5433, via env TAREFAS_DATABASE_URL). Isolado de propósito (não toca painel.py).
"""
from __future__ import annotations

import os

import psycopg
import structlog
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/painel", tags=["posts-corretora"])

USUARIO_SLUG = {"corretor": "corretor", "corretor": "corretor", "corretor": "corretor"}
ADMINS = {"dona", "pessoa", "assistente"}
SLUGS_CORRETORA = set(USUARIO_SLUG.values())
POSTS_POR_SEMANA = 2
# Corretoras com IG conectado ao token de publicação (espelha IG_USER_BY_CLIENTE do publisher)
IG_CONECTADO = {"corretor"}


def _tarefas_dsn() -> str:
    dsn = os.environ.get("TAREFAS_DATABASE_URL")
    if not dsn:
        raise HTTPException(500, "TAREFAS_DATABASE_URL não configurada na copiloto-api")
    return dsn


def _conn():
    return psycopg.connect(_tarefas_dsn(), autocommit=True)


def _quem(x_painel_user: str | None) -> tuple[str, bool]:
    if not x_painel_user:
        raise HTTPException(403, "painel_auth_required")
    u = x_painel_user.strip().lower()
    if u in ADMINS:
        return u, True
    slug = USUARIO_SLUG.get(u)
    if not slug:
        raise HTTPException(403, "usuario_sem_acesso_posts")
    return slug, False


class NovoPost(BaseModel):
    formato: str = "single"                 # single | carrossel | reel
    url_referencia: str | None = None       # post-modelo colado pela corretora
    observacao: str | None = None           # o que ela quer com o post (brief)
    corretora: str | None = None            # admin cria pra uma corretora específica


class ToggleAprov(BaseModel):
    value: bool = True


def _row_to_post(r) -> dict:
    (pid, cliente, data, hora, formato, modo, hook, caption, image_url, carousel_urls,
     status, permalink, publicado_em, copy_aprovada, imagem_aprovada,
     url_referencia, observacao, recusa_motivo) = r
    return {
        "recusa_motivo": recusa_motivo,
        "id": pid,
        "corretora": cliente,
        "data": data.isoformat() if data else None,
        "hora": str(hora)[:5] if hora else None,
        "formato": formato,
        "modo": modo,
        "hook": hook,
        "caption": caption or "",
        "tem_caption": bool(caption and caption.strip()),
        "image_url": image_url,
        "carousel_urls": carousel_urls,
        "status": status,
        "permalink": permalink,
        "publicado_em": publicado_em.isoformat() if publicado_em else None,
        "copy_aprovada": bool(copy_aprovada),
        "imagem_aprovada": bool(imagem_aprovada),
        "url_referencia": url_referencia,
        "observacao": observacao or "",
    }


_SELECT = """
    SELECT id, cliente, data, hora, formato, modo, hook, caption, image_url, carousel_urls,
           status, permalink, publicado_em, copy_aprovada, imagem_aprovada,
           url_referencia, observacao, recusa_motivo
    FROM tarefas.social_posts
"""


@router.get("/posts/me")
async def listar_meus_posts(
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
    corretora: str | None = None,
):
    quem, is_admin = _quem(x_painel_user)
    if is_admin:
        alvo = corretora if corretora in SLUGS_CORRETORA else None
        where, params = ("cliente = %s", (alvo,)) if alvo else ("cliente = ANY(%s)", (list(SLUGS_CORRETORA),))
    else:
        where, params = "cliente = %s", (quem,)

    with _conn() as c, c.cursor() as cur:
        cur.execute(_SELECT + f" WHERE {where} ORDER BY (data IS NULL) DESC, data DESC, id DESC", params)
        posts = [_row_to_post(r) for r in cur.fetchall()]

    return {
        "posts": posts,
        "posts_por_semana": POSTS_POR_SEMANA,
        "ig_conectado": _ig_conectado(quem if not is_admin else (corretora or "")),
        "is_admin": is_admin,
    }


def _ig_conectado(slug: str) -> bool:
    return slug in IG_CONECTADO


@router.post("/posts")
async def criar_post(
    body: NovoPost,
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
):
    quem, is_admin = _quem(x_painel_user)
    slug = (body.corretora or "").lower() if is_admin else quem
    if slug not in SLUGS_CORRETORA:
        raise HTTPException(400, "corretora_invalida")
    if not (body.observacao and body.observacao.strip()):
        raise HTTPException(422, "observacao_obrigatoria")

    with _conn() as c, c.cursor() as cur:
        cur.execute("SELECT COALESCE(MAX(ordem),0)+1 FROM tarefas.social_posts WHERE cliente=%s", (slug,))
        ordem = cur.fetchone()[0]
        cur.execute(
            """
            INSERT INTO tarefas.social_posts
              (cliente, ordem, linha, linha_slug, formato, hook, status,
               url_referencia, observacao, copy_aprovada, imagem_aprovada)
            VALUES (%s,%s,'Corretora','corretora',%s,'',%s,%s,%s,false,false)
            RETURNING id
            """,
            (slug, ordem, body.formato, "rascunho", body.url_referencia, body.observacao),
        )
        pid = cur.fetchone()[0]
    log.info("post_corretora_brief_criado", id=pid, cliente=slug, por=quem)
    return {"id": pid, "status": "rascunho"}


def _post_corretora(cur, pid: int, quem: str, is_admin: bool):
    cur.execute("SELECT cliente, status FROM tarefas.social_posts WHERE id=%s", (pid,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(404, "post_nao_encontrado")
    cliente, status = row
    if cliente not in SLUGS_CORRETORA:
        raise HTTPException(403, "post_nao_e_de_corretora")
    if not is_admin and cliente != quem:
        raise HTTPException(403, "post_de_outra_corretora")
    return cliente, status


def _recompute_status(cur, pid: int):
    """Quando copy E imagem aprovadas -> status 'aprovado' (entra na fila).
    Se alguma cair e estava aprovado -> volta pra rascunho."""
    cur.execute("SELECT copy_aprovada, imagem_aprovada, status FROM tarefas.social_posts WHERE id=%s", (pid,))
    copy_ok, img_ok, status = cur.fetchone()
    if copy_ok and img_ok and status in ("rascunho", "proposta", "aguardando_aprovacao"):
        cur.execute("UPDATE tarefas.social_posts SET status='aprovado', aprovado_em=now(), updated_at=now() WHERE id=%s", (pid,))
        return "aprovado"
    if (not (copy_ok and img_ok)) and status == "aprovado":
        cur.execute("UPDATE tarefas.social_posts SET status='rascunho', aprovado_em=NULL, updated_at=now() WHERE id=%s", (pid,))
        return "rascunho"
    return status


@router.post("/posts/{pid}/aprovar-copy")
async def aprovar_copy(pid: int, body: ToggleAprov, x_painel_user: str | None = Header(default=None, alias="X-Painel-User")):
    quem, is_admin = _quem(x_painel_user)
    with _conn() as c, c.cursor() as cur:
        _post_corretora(cur, pid, quem, is_admin)
        cur.execute("UPDATE tarefas.social_posts SET copy_aprovada=%s, updated_at=now() WHERE id=%s", (body.value, pid))
        st = _recompute_status(cur, pid)
    return {"id": pid, "copy_aprovada": body.value, "status": st}


@router.post("/posts/{pid}/aprovar-imagem")
async def aprovar_imagem(pid: int, body: ToggleAprov, x_painel_user: str | None = Header(default=None, alias="X-Painel-User")):
    quem, is_admin = _quem(x_painel_user)
    with _conn() as c, c.cursor() as cur:
        _post_corretora(cur, pid, quem, is_admin)
        cur.execute("UPDATE tarefas.social_posts SET imagem_aprovada=%s, updated_at=now() WHERE id=%s", (body.value, pid))
        st = _recompute_status(cur, pid)
    return {"id": pid, "imagem_aprovada": body.value, "status": st}


class Recusa(BaseModel):
    motivo: str = ""


@router.post("/posts/{pid}/recusar")
async def recusar_post(pid: int, body: Recusa, x_painel_user: str | None = Header(default=None, alias="X-Painel-User")):
    quem, is_admin = _quem(x_painel_user)
    with _conn() as c, c.cursor() as cur:
        _post_corretora(cur, pid, quem, is_admin)
        cur.execute(
            "UPDATE tarefas.social_posts SET status='recusado', recusa_motivo=%s, "
            "copy_aprovada=false, imagem_aprovada=false, aprovado_em=NULL, updated_at=now() WHERE id=%s",
            (body.motivo or None, pid),
        )
    log.info("post_corretora_recusado", id=pid, por=quem, motivo=body.motivo[:80])
    return {"id": pid, "status": "recusado", "motivo": body.motivo}


@router.post("/posts/{pid}/reabrir")
async def reabrir_post(pid: int, x_painel_user: str | None = Header(default=None, alias="X-Painel-User")):
    quem, is_admin = _quem(x_painel_user)
    with _conn() as c, c.cursor() as cur:
        _post_corretora(cur, pid, quem, is_admin)
        cur.execute("UPDATE tarefas.social_posts SET status='rascunho', recusa_motivo=NULL, updated_at=now() WHERE id=%s", (pid,))
    return {"id": pid, "status": "rascunho"}


@router.delete("/posts/{pid}")
async def apagar_post(pid: int, x_painel_user: str | None = Header(default=None, alias="X-Painel-User")):
    quem, is_admin = _quem(x_painel_user)
    with _conn() as c, c.cursor() as cur:
        _, status = _post_corretora(cur, pid, quem, is_admin)
        if status in ("publicado", "publicando"):
            raise HTTPException(409, "post_ja_publicado")
        cur.execute("DELETE FROM tarefas.social_posts WHERE id=%s", (pid,))
    return {"id": pid, "deletado": True}
