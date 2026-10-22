"""Nutrição pela linha da corretora DONA do lead (07/10/2026, decisão da Dona).

"Se tiver lead que entra na nutrição pelo nome da corretora, da Pessoa10 ou da
Cláudia, pode fazer a nutrição pelo número delas." A regra (`nutricao.saida_do_marco`):

    chave `nutricao_pela_linha_da_dona` desligada/ilegível → Pessoa12
    dona sem linha registrada                              → Pessoa12
    linha dela sem modelo sem botão UTILITY aprovado        → Pessoa12
    senão                                                   → linha da dona

O teto do dia é o da linha por onde o marco SAI (`payload.linha`).

Caminho inteiro, como nos testes de `test_automacoes_painel_manda.py`: rota da
tela → banco → `nutricao.tick` de verdade → remetente FALSO que anota por qual
linha mandou. Nada sai da máquina.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text as _t

from app.config import settings
from app.db.models import CadenciaEstado, CadenciaEvento
from app.db.session import SessionLocal
from app.services import cadencia
from app.services import cloud_lines as cl
from app.services import config_operacional as cfg
from app.services import nutricao
from tests.test_automacoes_painel_manda import (  # noqa: F401 — fixtures por nome
    ADMIN,
    CORRETOR,
    CORRETOR,
    CORRETOR,
    TENANT,
    Remetente,
    _etapa,
    _limpar,
    eventos_de,
    funil,
    gravar,
    mapa,
    nutrido,
    tel,
    tela,
)

CHAVE = nutricao.CHAVE_LINHA_DA_DONA

LINHA_CORRETOR = cl.CloudLine(phone_id="teste-corretor", slug="corretor", papel="corretora",
                          env_file="", token_var="")
LINHA_CORRETOR = cl.CloudLine(phone_id="teste-corretor", slug="corretor", papel="corretora",
                          env_file="", token_var="")


class PorLinha(Remetente):
    """Anota também por qual linha cada telefone saiu (`None` = caminho da Pessoa12,
    `enviar_template_corretor`). Falha pra uma linha inteira quando pedido."""

    def __init__(self, falha_na_linha: str | None = None) -> None:
        super().__init__()
        self.linha_de: dict[str, str] = {}
        self.falha_na_linha = falha_na_linha

    def __call__(self, telefone, texto, *, com_botao, **kw):
        linha = kw.get("linha")
        slug = linha.slug if linha is not None else "corretor"
        if self.falha_na_linha and slug == self.falha_na_linha:
            raise RuntimeError(f"400: linha {slug} recusou")
        self.linha_de[telefone] = slug
        return super().__call__(telefone, texto, com_botao=com_botao, **kw)


def _limpar_chave(ses) -> None:
    ses.execute(_t("DELETE FROM config_operacional WHERE chave = ANY(:c)"),
                {"c": [CHAVE, "cadencia_cap_por_corretora"]})
    ses.execute(_t("DELETE FROM config_operacional_historico WHERE chave = ANY(:c)"),
                {"c": [CHAVE, "cadencia_cap_por_corretora"]})
    ses.commit()
    cfg.invalidar_cache()


@pytest.fixture()
def s():
    with SessionLocal() as ses:
        _limpar(ses)
        _limpar_chave(ses)
        yield ses
        ses.rollback()
        _limpar(ses)
        _limpar_chave(ses)


@pytest.fixture()
def linhas(monkeypatch):
    """Pessoa10 e Pessoa11 com linha registrada e modelo aprovado; Pessoa12 a de sempre.
    O teste muda `donas`/`modelos` pra montar o caso dele."""
    corretor = cl.line_by_slug("corretor")
    donas = {CORRETOR: LINHA_CORRETOR, CORRETOR: LINHA_CORRETOR, CORRETOR: corretor}
    modelos = {
        "corretor": {"acme_jn_ola_fu_01", "acme_jn_ola_v4_01"},   # como na Graph em 07/10
        "corretor": {"acme_cl_ola_v5_01"},
        "corretor": {"acme_ola_a_01"},
    }
    monkeypatch.setattr(cl, "linha_da_corretora",
                        lambda cid, session=None: donas.get(cid))
    monkeypatch.setattr(cl, "templates_utility",
                        lambda slug, agora=None: set(modelos.get(slug, set())))
    monkeypatch.setattr(settings, "cadencia_min_intervalo_s", 0)
    monkeypatch.setattr("app.services.dia_util.pode_enviar", lambda *a, **k: True)
    cadencia._LAST_ENQUEUE["at"] = 0.0
    yield {"donas": donas, "modelos": modelos}
    cadencia._LAST_ENQUEUE["at"] = 0.0


@pytest.fixture()
def sem_teto(monkeypatch):
    monkeypatch.setattr(cadencia, "_cap_diario_atingido", lambda *a, **k: False)


def _marco(s, cid: int) -> dict:
    ev = eventos_de(s, cid, cadencia.TIPO_MENSAGEM)
    assert ev, f"nenhum marco gravado pro estado {cid}"
    return ev[-1]


def _na_tela(m: dict, corretora_id: int) -> dict:
    return next(c for c in m["nutricao"]["por_corretora"]["corretoras"]
                if c["corretora_id"] == corretora_id)


# ── 1. cada uma pela sua ─────────────────────────────────────────────────────


def test_lead_da_corretor_sai_pela_corretor_e_lead_da_corretor_pela_corretor(s, tela, funil, linhas, sem_teto):
    gravar(tela, "nutricao_ativa", True)
    da_corretor, lead_corretor = nutrido(s, funil, "701", CORRETOR)
    da_corretor, lead_corretor = nutrido(s, funil, "702", CORRETOR)
    sem_dona, _ = nutrido(s, funil, "703", None)        # estado legado: é da Pessoa12

    rem = PorLinha()
    res = nutricao.tick(session=s, sender=rem)

    assert rem.linha_de[tel("701")] == "corretor"
    assert rem.linha_de[tel("702")] == "corretor" and rem.linha_de[tel("703")] == "corretor"
    assert res.marcos_por_linha.get("corretor") == 1 and res.marcos_por_linha.get("corretor", 0) >= 2
    mj = _marco(s, da_corretor)
    assert (mj["linha"], mj["motivo_linha"], mj["corretora_id"]) == ("corretor", "dona_com_linha", CORRETOR)
    assert mj["wamid"] and mj["sent"] is True
    mg = _marco(s, da_corretor)
    assert (mg["linha"], mg["motivo_linha"], mg["corretora_id"]) == ("corretor", "dona_com_linha", CORRETOR)
    assert _marco(s, sem_dona)["linha"] == "corretor"
    assert _etapa(s, lead_corretor) == "30 dias" and _etapa(s, lead_corretor) == "30 dias"

    # a tela diz a mesma coisa, por corretora, e some o "sai toda pela Pessoa12"
    m = mapa(tela)
    pc = m["nutricao"]["por_corretora"]
    assert pc["toda_pela_corretor"] is False and pc["sai_por"] == "dona"
    assert "sai pela linha da corretora dona" in pc["por_qual_linha"]
    assert pc["pela_linha_da_dona"]["ligada"] is True
    assert _na_tela(m, CORRETOR)["sai_por"] == "corretor"
    assert _na_tela(m, CORRETOR)["motivo_linha"] == "dona_com_linha"
    assert _na_tela(m, CORRETOR)["sai_por"] == "corretor"
    nu = next(x for x in m["resumo"] if x["id"] == "nutricao")
    assert {"corretor", "corretor"} <= {x["slug"] for x in nu["linhas"]}


# ── 2. a linha dela não serve → Pessoa12, com o motivo ────────────────────────────


def test_sem_modelo_aprovado_ou_sem_linha_cai_pra_corretor_com_motivo(s, tela, funil, linhas, sem_teto):
    gravar(tela, "nutricao_ativa", True)
    # Pessoa10: só um casco MARKETING por nome na WABA dela → nada sem botão UTILITY
    linhas["modelos"]["corretor"] = {"acme_jn_ola_m_01"}
    # Pessoa11: sem linha registrada
    linhas["donas"][CORRETOR] = None
    da_corretor, _ = nutrido(s, funil, "711", CORRETOR)
    da_corretor, _ = nutrido(s, funil, "712", CORRETOR)

    rem = PorLinha()
    nutricao.tick(session=s, sender=rem)

    assert rem.linha_de[tel("711")] == "corretor" and rem.linha_de[tel("712")] == "corretor"
    mj, mc = _marco(s, da_corretor), _marco(s, da_corretor)
    assert (mj["linha"], mj["motivo_linha"]) == ("corretor", "template_nao_aprovado_na_waba")
    assert (mc["linha"], mc["motivo_linha"]) == ("corretor", "dona_sem_linha")
    assert mj["corretora_id"] == CORRETOR and mc["corretora_id"] == CORRETOR

    m = mapa(tela)
    assert _na_tela(m, CORRETOR)["sai_por"] == "corretor"
    assert _na_tela(m, CORRETOR)["motivo_linha"] == "template_nao_aprovado_na_waba"
    assert "modelo aprovado" in _na_tela(m, CORRETOR)["motivo_linha_texto"]
    assert _na_tela(m, CORRETOR)["motivo_linha"] == "dona_sem_linha"


# ── 3. chave desligada (ou torta) = tudo pela Pessoa12 ────────────────────────────


def test_chave_desligada_manda_tudo_pela_corretor_e_torta_tambem(s, tela, funil, linhas, sem_teto):
    gravar(tela, "nutricao_ativa", True)
    cfg_tela = tela.get("/painel/automacoes/config", headers=ADMIN).json()
    linha_cfg = (cfg_tela.get("config") or cfg_tela)[CHAVE]
    assert linha_cfg["valor"] is True and linha_cfg["padrao"] is True

    gravar(tela, CHAVE, False)
    assert cfg.historico([CHAVE], 1)[0]["depois"] is False
    da_corretor, _ = nutrido(s, funil, "721", CORRETOR)
    da_corretor, _ = nutrido(s, funil, "722", CORRETOR)
    rem = PorLinha()
    nutricao.tick(session=s, sender=rem)
    assert rem.linha_de[tel("721")] == "corretor" and rem.linha_de[tel("722")] == "corretor"
    assert _marco(s, da_corretor)["motivo_linha"] == "chave_desligada"
    pc = mapa(tela)["nutricao"]["por_corretora"]
    assert pc["toda_pela_corretor"] is True and pc["sai_por"] == "corretor"
    assert pc["pela_linha_da_dona"]["ligada"] is False

    # a tela recusa valor torto; gravado por fora, torto, vale Pessoa12
    r = tela.put(f"/painel/automacoes/config/{CHAVE}", json={"valor": "sim"}, headers=ADMIN)
    assert r.status_code == 422
    s.execute(_t("UPDATE config_operacional SET valor = CAST(:v AS jsonb) WHERE chave = :k"),
              {"v": '"sim"', "k": CHAVE})
    s.commit()
    da_corretor2, _ = nutrido(s, funil, "723", CORRETOR)
    cadencia._LAST_ENQUEUE["at"] = 0.0
    rem2 = PorLinha()
    nutricao.tick(session=s, sender=rem2)
    assert rem2.linha_de[tel("723")] == "corretor"
    m2 = _marco(s, da_corretor2)
    assert m2["motivo_linha"] == "chave_desligada" and m2["chave_linha_da_dona"] == "ilegivel"

    # religou → a próxima já sai pela Pessoa10 (relida antes de cada marco)
    gravar(tela, CHAVE, True)
    da_corretor3, _ = nutrido(s, funil, "724", CORRETOR)
    cadencia._LAST_ENQUEUE["at"] = 0.0
    rem3 = PorLinha()
    nutricao.tick(session=s, sender=rem3)
    assert rem3.linha_de[tel("724")] == "corretor"


# ── 4. teto por linha ────────────────────────────────────────────────────────


def test_teto_da_corretor_cheio_segura_so_a_corretor(s, tela, funil, linhas, monkeypatch):
    """Teto REAL (`_cap_diario_atingido`), com o limite por linha trocado só
    aqui: Pessoa10 com zero, Pessoa12 com sobra."""
    gravar(tela, "nutricao_ativa", True)
    vistos: list[int | None] = []

    def teto(cid):
        vistos.append(cid)
        return 0 if cid == CORRETOR else 10**6

    monkeypatch.setattr(cfg, "cap_da_corretora", teto)
    monkeypatch.setattr(settings, "cadencia_reserva_followup", 0)
    da_corretor, lead_corretor = nutrido(s, funil, "731", CORRETOR)
    da_corretor, _ = nutrido(s, funil, "732", CORRETOR)

    rem = PorLinha()
    res = nutricao.tick(session=s, sender=rem)

    assert tel("731") not in rem.linha_de and rem.linha_de[tel("732")] == "corretor"
    assert res.teto_por_linha == {"corretor": 1}
    assert res.bloqueados.get("cap_diario") == 1
    assert None not in vistos                       # nenhum teto global
    assert _etapa(s, lead_corretor) == "Base"           # marco continua devido
    s.expire_all()
    assert s.get(CadenciaEstado, da_corretor).proximo_toque_em <= datetime.now(timezone.utc)


def test_marco_conta_no_balde_da_linha_por_onde_saiu(s, tela, funil, linhas, sem_teto):
    """Lead da Pessoa10 que caiu pra Pessoa12 pesa na Pessoa12, não na Pessoa10; o que saiu pela
    Pessoa10 pesa na Pessoa10; o follow-up (sem `linha` no evento) segue pela dona."""
    gravar(tela, "nutricao_ativa", True)
    agora = datetime.now(timezone.utc)

    def balde(cid):
        return cadencia.mensagens_no_balde(s, TENANT, agora, corretora_id=cid)

    corretor0, corretor0 = balde(CORRETOR), balde(CORRETOR)

    linhas["modelos"]["corretor"] = set()                 # 1º: Pessoa10 sem modelo → Pessoa12
    nutrido(s, funil, "741", CORRETOR)
    nutricao.tick(session=s, sender=PorLinha())
    assert (balde(CORRETOR), balde(CORRETOR)) == (corretor0 + 1, corretor0)

    linhas["modelos"]["corretor"] = {"acme_jn_ola_fu_01"}   # 2º: pela Pessoa10
    nutrido(s, funil, "742", CORRETOR)
    cadencia._LAST_ENQUEUE["at"] = 0.0
    nutricao.tick(session=s, sender=PorLinha())
    assert (balde(CORRETOR), balde(CORRETOR)) == (corretor0 + 1, corretor0 + 1)

    # follow-up da Pessoa10: evento sem `linha` → balde da dona
    fu = CadenciaEstado(tenant_id=TENANT, telefone=tel("743"), toque_atual=1,
                        status="ativa", corretora_id=CORRETOR,
                        proximo_toque_em=agora + timedelta(days=1),
                        criado_em=agora, atualizado_em=agora)
    s.add(fu)
    s.flush()
    s.add(CadenciaEvento(tenant_id=TENANT, cadencia_id=fu.id, toque=0,
                         tipo=cadencia.TIPO_MENSAGEM, canal=cadencia.CANAL_WHATSAPP,
                         payload={"sent": True, "wamid": "wamid.fu"}, at=agora))
    s.commit()
    assert (balde(CORRETOR), balde(CORRETOR)) == (corretor0 + 1, corretor0 + 2)


# ── 5. erro na linha da Pessoa10 não derruba a volta ────────────────────────────


def test_erro_na_linha_da_corretor_nao_derruba_a_volta(s, tela, funil, linhas, sem_teto):
    gravar(tela, "nutricao_ativa", True)
    da_corretor, lead_corretor = nutrido(s, funil, "751", CORRETOR)
    da_corretor, lead_corretor = nutrido(s, funil, "752", CORRETOR)
    da_corretor, lead_corretor = nutrido(s, funil, "753", CORRETOR)

    rem = PorLinha(falha_na_linha="corretor")
    res = nutricao.tick(session=s, sender=rem)

    assert rem.linha_de[tel("752")] == "corretor" and rem.linha_de[tel("753")] == "corretor"
    assert res.falhas == 1
    falha = eventos_de(s, da_corretor, cadencia.TIPO_ENVIO_FALHOU)
    assert falha and falha[0]["linha"] == "corretor" and falha[0]["corretora_id"] == CORRETOR
    s.expire_all()
    assert s.get(CadenciaEstado, da_corretor).proximo_toque_em > datetime.now(timezone.utc) + timedelta(hours=23)
    assert _etapa(s, lead_corretor) == "Base"
    assert _etapa(s, lead_corretor) == "30 dias" and _etapa(s, lead_corretor) == "30 dias"


# ── 6. a mesma conta do emissor, sem enviar ─────────────────────────────────


def test_modelos_sem_botao_seguem_a_rotacao_do_emissor(linhas):
    m = linhas["modelos"]
    # rotação padrão da Pessoa12 quando a WABA tem algum `acme_ola_*` UTILITY
    m["corretor"] = {"acme_ola_a_01", "acme_reeng_b_01"}
    assert nutricao.modelos_sem_botao_aprovados("corretor") == ["acme_ola_a_01"]
    # rotação própria da linha: só `_ola_`, só do prefixo dela, sem casco MARKETING
    m["corretor"] = {"acme_cl_ola_v5_01", "acme_cl_reeng_b_01", "acme_jn_ola_fu_01",
                 "acme_cl_ola_m_01"}
    assert nutricao.modelos_sem_botao_aprovados("corretor") == ["acme_cl_ola_v5_01"]
    m["corretor"] = {"acme_jn_ola_fu_01", "acme_jn_ola_v3_01", "acme_jn_ola_v4_01"}
    assert len(nutricao.modelos_sem_botao_aprovados("corretor")) == 3
    m["corretor"] = set()
    assert nutricao.modelos_sem_botao_aprovados("corretor") == []
