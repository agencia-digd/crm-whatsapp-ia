"""GET /painel/visao/atividade — mensagens e ligações por corretora na Visão.

Router próprio pelo mesmo motivo do imoveis_captados: painel.py passa de 10k
linhas e várias mesas mexem nele ao mesmo tempo. Só leitura. As contas e as
definições estão em `app/services/visao_atividade.py`.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Header, Query

from app.api.painel import (
    CORRETORA_LABEL,
    _identificar_usuario,
    _viewer_info,
    parse_periodo,
)
from app.db.session import SessionLocal
from app.services import visao_atividade as svc

router = APIRouter(prefix="/painel", tags=["painel"])


@router.get("/visao/atividade")
def painel_visao_atividade(
    periodo: str = Query("mes_atual"),
    inicio: str | None = Query(None),
    fim: str | None = Query(None),
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Mensagens (escritas por gente × automáticas, e pra quantas pessoas) e
    ligações (feitas, atendidas, não atendidas) por corretora e no total.

    Mesmo `periodo` da `/painel/visao-geral` (a tela tem um seletor só).
    Admin vê o time; corretora vê só a linha dela.
    """
    user = _identificar_usuario(x_painel_user)
    viewer = _viewer_info(user)
    my_cid = viewer["corretora_id"]
    di, df, label = parse_periodo(periodo, inicio, fim)

    def _calc() -> dict[str, Any]:
        with SessionLocal() as s:
            return svc.calcular_atividade(
                s, di, df, corretora_label=dict(CORRETORA_LABEL), so_corretora=my_cid,
            ) | {"gerado_em": datetime.now(timezone.utc).isoformat()}

    dados = svc.com_cache((di, df, my_cid), _calc)
    return {
        "periodo": {"tipo": periodo, "inicio": di.isoformat(),
                    "fim": df.isoformat(), "label": label},
        "viewer": viewer,
        **dados,
        "definicao": {
            "mensagens_escritas": (
                "saída da linha sem template e sem carimbo de robô — a mesma conta "
                "que a Assistente responde no Telegram"),
            "mensagens_automaticas": (
                "template (disparo, régua, Reengajar) ou mensagem com origem de "
                "robô (Pessoa, pós-ligação, aviso)"),
            "pessoas": "contatos distintos pelo final do telefone; no total, cada pessoa conta uma vez",
            "ligacoes": (
                "registros de resultado na tela de ligações; não atendeu volta em 24 h "
                "e conta de novo. Venda perdida e número errado ficam à parte"),
        },
    }
