"""A rota POST /cloud/inbound tem que ser o handler do webhook, não um helper.

Incidente 16-17/09: o commit bde8ade4 inseriu `_tratar_resposta_regua_
proprietario(telefone, texto)` ENTRE o `@router.post("/inbound")` e o
`async def cloud_inbound`. O decorador passou a registrar o helper; como ele
recebe dois `str` simples, o FastAPI virou a rota em query-params e todo
inbound da linha da Pessoa12 respondeu 422 — mensagem de cliente sumindo por 2 dias
sem erro visível em lugar nenhum.

O teste trava as duas propriedades que faltavam: a rota existe apontando pro
handler certo, e ela NÃO exige query-param (o corpo é o payload da Meta).
"""
from __future__ import annotations

from app.api import cloud_corretora_a as cg
from app.main import app


def _rota_inbound():
    for r in app.routes:
        if getattr(r, "path", None) == "/cloud/inbound" and "POST" in getattr(r, "methods", set()):
            return r
    return None


def test_rota_existe_e_aponta_pro_handler():
    rota = _rota_inbound()
    assert rota is not None, "POST /cloud/inbound sumiu do app"
    assert rota.endpoint is cg.cloud_inbound, (
        f"a rota está registrada em {rota.endpoint.__name__!r} em vez de cloud_inbound — "
        "o decorador grudou na função errada"
    )


def test_inbound_nao_exige_query_param():
    """Meta manda o payload no CORPO. Se a rota pedir query-param, todo webhook
    vira 422 e a mensagem do lead é perdida em silêncio."""
    rota = _rota_inbound()
    # `required` mudou de lugar entre versões do FastAPI/Pydantic (atributo →
    # field_info.is_required()). Tenta as duas e, na dúvida, trata como
    # obrigatório: o teste tem que gritar, não passar por acidente.
    def _obrigatorio(param) -> bool:
        fi = getattr(param, "field_info", None)
        if fi is not None and hasattr(fi, "is_required"):
            return bool(fi.is_required())
        return bool(getattr(param, "required", True))

    obrigatorios = [p.name for p in rota.dependant.query_params if _obrigatorio(p)]
    assert not obrigatorios, f"rota exige query-param {obrigatorios} — webhook vai dar 422"


def test_helper_da_regua_nao_e_endpoint():
    """O helper continua existindo como função interna, sem virar rota."""
    assert callable(cg._tratar_resposta_regua_proprietario)
    rotas_do_helper = [
        getattr(r, "path", None) for r in app.routes
        if getattr(r, "endpoint", None) is cg._tratar_resposta_regua_proprietario
    ]
    assert not rotas_do_helper, f"helper virou rota em {rotas_do_helper}"
