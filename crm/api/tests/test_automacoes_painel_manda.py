"""O PAINEL MANDA nas outras automações — roleta, follow-up e nutrição (06/10/2026).

Pedido da Dona: boas-vindas com a roleta na tela; follow-up e nutrição com
"qual telefone tá ligado", ligado/desligado por corretora. E o conserto da
nutrição que travava no primeiro telefone inválido.

Cada teste faz o caminho inteiro, como em `test_garantia_painel_manda.py`:

    rota da tela → banco (`config_operacional` + histórico) → o motor DE VERDADE
    (`lead_intake.intake_lead`, `cadencia.tick`, `nutricao.tick`) → remetente
    FALSO que só anota.

Nada sai da máquina. Os nomes destes testes estão na tabela de
`projetos/crm/AUTOMACOES-BACKEND.md`.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text as _t

from app.config import settings
from app.db.models import CadenciaEstado, CadenciaEvento, Lead, PipelineStage
from app.db.session import SessionLocal
from app.main import app
from app.services import automacao_por_corretora as apc
from app.services import cadencia
from app.services import cloud_lines as cl
from app.services import config_operacional as cfg
from app.services import lead_intake, nutricao
from app.services import painel_automacoes as pa

TENANT = 1
ADMIN = {"X-Painel-User": "dona"}
CORRETOR, CORRETOR, CORRETOR = 1, 2, 3
_PREFIXO = "5500000000000"                 # faixa de telefone só destes testes
CHAVES = ("cadencia_por_corretora", "nutricao_por_corretora", "roleta_corretoras",
          "cadencia_ativa", "nutricao_ativa", "lead_intake_ativo")


class Remetente:
    """Contrato do remetente real quando a Meta aceita. Anota o telefone."""

    def __init__(self, ao_enviar=None, falha_para: set[str] | None = None) -> None:
        self.telefones: list[str] = []
        self.ao_enviar = ao_enviar
        self.falha_para = falha_para or set()

    def __call__(self, telefone, texto, *, com_botao, **kw):
        if telefone in self.falha_para:
            raise RuntimeError("400: (#131009) Parameter value is not valid")
        self.telefones.append(telefone)
        if self.ao_enviar:
            self.ao_enviar(telefone)
        return {"ok": True, "dry_run": False, "sent": True,
                "wamid": f"wamid.autom.{telefone[-4:]}.{len(self.telefones)}",
                "template": "acme_ola_a_01"}


# ── Montagem ─────────────────────────────────────────────────────────────────


def _limpar(ses) -> None:
    p = f"%{_PREFIXO[2:]}%"
    for sql in (
        "DELETE FROM cadencia_eventos WHERE cadencia_id IN "
        "(SELECT id FROM cadencia_estado WHERE telefone LIKE :p)",
        "DELETE FROM cadencia_estado WHERE telefone LIKE :p",
        "DELETE FROM lead_events WHERE lead_id IN "
        "(SELECT id FROM leads WHERE telefone LIKE :p)",
        "DELETE FROM leads WHERE telefone LIKE :p",
    ):
        ses.execute(_t(sql), {"p": p})
    ses.execute(_t("DELETE FROM config_operacional WHERE chave = ANY(:c)"),
                {"c": list(CHAVES)})
    ses.execute(_t("DELETE FROM config_operacional_historico WHERE chave = ANY(:c)"),
                {"c": list(CHAVES)})
    ses.commit()
    cfg.invalidar_cache()


@pytest.fixture()
def s():
    with SessionLocal() as ses:
        _limpar(ses)
        yield ses
        ses.rollback()
        _limpar(ses)


@pytest.fixture()
def tela() -> TestClient:
    return TestClient(app)


@pytest.fixture()
def motor(monkeypatch):
    """O tick como em produção, menos o que sai da máquina ou depende do
    relógio: toda corretora resolve pra uma linha registrada, sem teto do dia,
    sem espaçamento entre mensagens (pra uma volta poder mandar várias)."""
    corretor = cl.line_by_slug("corretor")
    monkeypatch.setattr(cl, "linha_da_corretora", lambda cid, session=None: corretor)
    monkeypatch.setattr(cadencia, "_cap_diario_atingido", lambda *a, **k: False)
    monkeypatch.setattr(settings, "cadencia_min_intervalo_s", 0)
    monkeypatch.setattr("app.services.dia_util.pode_enviar", lambda *a, **k: True)
    cadencia._LAST_ENQUEUE["at"] = 0.0
    yield
    cadencia._LAST_ENQUEUE["at"] = 0.0


def gravar(tela: TestClient, chave: str, valor) -> None:
    r = tela.put(f"/painel/automacoes/config/{chave}", json={"valor": valor},
                 headers=ADMIN)
    assert r.status_code == 200, f"{chave}={valor!r}: {r.status_code} {r.text}"


def tel(sufixo: str) -> str:
    return f"{_PREFIXO}{sufixo}"


def regua(s, sufixo: str, corretora_id: int | None, *, status: str = "ativa",
          telefone: str | None = None, toque: int = 0) -> int:
    agora = datetime.now(timezone.utc)
    est = CadenciaEstado(
        tenant_id=TENANT, telefone=telefone or tel(sufixo), toque_atual=toque,
        status=status, corretora_id=corretora_id,
        proximo_toque_em=agora - timedelta(minutes=5),
        criado_em=agora - timedelta(days=40), atualizado_em=agora,
    )
    s.add(est)
    s.commit()
    return int(est.id)


def eventos_de(s, cadencia_id: int, tipo: str) -> list[dict]:
    s.expire_all()
    return [e.payload or {} for e in s.query(CadenciaEvento)
            .filter(CadenciaEvento.cadencia_id == cadencia_id,
                    CadenciaEvento.tipo == tipo).all()]


def mapa(tela: TestClient) -> dict:
    r = tela.get("/painel/automacoes", headers=ADMIN)
    assert r.status_code == 200, r.text
    return r.json()


# ═════════════════════════════════════════════════════════════════════════════
# FOLLOW-UP — interruptor por corretora
# ═════════════════════════════════════════════════════════════════════════════


def test_follow_up_desligado_na_tela_nao_sai_pra_ela_e_sai_pras_outras(s, tela, motor):
    gravar(tela, "cadencia_ativa", True)
    r = tela.put(f"/painel/automacoes/follow-up/corretoras/{CORRETOR}",
                 json={"ligada": False}, headers=ADMIN)
    assert r.status_code == 200, r.text
    corpo = r.json()
    assert corpo["ok"] is True and corpo["gravado"] is False
    assert corpo["depois"] == {str(CORRETOR): False}
    corretor_na_tela = next(c for c in corpo["follow_up"]["corretoras"]
                        if c["corretora_id"] == CORRETOR)
    assert corretor_na_tela["ligada"] is False and corretor_na_tela["ligada_efetiva"] is False
    hist = cfg.historico([apc.CHAVE_FOLLOWUP], 5)
    assert hist[0]["autor"] == "dona" and hist[0]["depois"] == {"1": False}

    da_corretor = regua(s, "101", CORRETOR)
    da_corretor = regua(s, "102", CORRETOR)
    sem_dona = regua(s, "103", None)          # conta como da Pessoa12
    rem = Remetente()
    res = cadencia.tick(session=s, sender=rem)
    assert tel("101") not in rem.telefones
    assert {tel("102"), tel("103")} <= set(rem.telefones)
    assert res.bloqueados.get("corretora_desligada") == 1
    assert eventos_de(s, da_corretor, cadencia.TIPO_MENSAGEM) == []
    assert eventos_de(s, da_corretor, cadencia.TIPO_MENSAGEM)
    assert eventos_de(s, sem_dona, cadencia.TIPO_MENSAGEM)

    # religou na tela → a próxima volta manda (o toque continuava devido)
    r = tela.put(f"/painel/automacoes/follow-up/corretoras/{CORRETOR}",
                 json={"ligada": True}, headers=ADMIN)
    assert r.json()["gravado"] is True
    cadencia._LAST_ENQUEUE["at"] = 0.0
    rem2 = Remetente()
    cadencia.tick(session=s, sender=rem2)
    assert tel("101") in rem2.telefones


def test_follow_up_vale_antes_de_cada_mensagem_dentro_da_volta(s, tela, motor):
    """Desligou no meio da volta: a mensagem seguinte da mesma corretora já não
    sai. É a releitura do banco antes de CADA envio, não no começo da volta."""
    gravar(tela, "cadencia_ativa", True)
    regua(s, "111", CORRETOR)
    regua(s, "112", CORRETOR)

    def desliga_a_corretor(_telefone):
        cfg.definir_item(apc.CHAVE_FOLLOWUP, str(CORRETOR), False, autor="dona-no-meio")

    rem = Remetente(ao_enviar=desliga_a_corretor)
    res = cadencia.tick(session=s, sender=rem)
    meus = [t for t in rem.telefones if t.startswith(_PREFIXO)]
    assert len(meus) == 1, rem.telefones
    assert res.bloqueados.get("corretora_desligada", 0) >= 1


def test_follow_up_geral_desligado_vence_a_corretora_ligada(s, tela, motor):
    gravar(tela, "cadencia_ativa", False)
    tela.put(f"/painel/automacoes/follow-up/corretoras/{CORRETOR}", json={"ligada": True},
             headers=ADMIN)
    regua(s, "121", CORRETOR)
    rem = Remetente()
    cadencia.tick(session=s, sender=rem)
    assert rem.telefones == []
    corretor = next(c for c in mapa(tela)["follow_up"]["corretoras"] if c["corretora_id"] == CORRETOR)
    assert corretor["ligada"] is True and corretor["ligada_efetiva"] is False


def test_follow_up_valor_torto_segura_todo_mundo_e_a_tela_diz(s, tela, motor):
    gravar(tela, "cadencia_ativa", True)
    # gravado por fora da tela, torto: "sim" não é true
    s.execute(_t("INSERT INTO config_operacional (chave, valor, atualizado_em, atualizado_por) "
                 "VALUES ('cadencia_por_corretora', CAST(:v AS jsonb), now(), 'fora')"),
              {"v": '{"3": "sim"}'})
    s.commit()
    regua(s, "131", CORRETOR)
    rem = Remetente()
    res = cadencia.tick(session=s, sender=rem)
    assert [t for t in rem.telefones if t.startswith(_PREFIXO)] == []
    assert res.bloqueados.get("chave_ilegivel", 0) >= 1
    m = mapa(tela)
    assert m["follow_up"]["interruptor"]["ilegivel"] is True
    fu = next(x for x in m["resumo"] if x["id"] == "follow-up")
    assert fu["estado"] == "parada" and "ilegível" in fu["motivo"]
    # a tela recusa gravar torto e recusa corretora inativa/inexistente
    r = tela.put("/painel/automacoes/config/cadencia_por_corretora",
                 json={"valor": {"3": "sim"}}, headers=ADMIN)
    assert r.status_code == 422
    r = tela.put("/painel/automacoes/follow-up/corretoras/999",
                 json={"ligada": False}, headers=ADMIN)
    assert r.status_code == 404
    r = tela.put(f"/painel/automacoes/follow-up/corretoras/{CORRETOR}",
                 json={"ligada": "nao"}, headers=ADMIN)
    assert r.status_code == 422
    # mexer numa corretora com o mapa torto: 409, conserta voltando ao padrão
    r = tela.put(f"/painel/automacoes/follow-up/corretoras/{CORRETOR}",
                 json={"ligada": True}, headers=ADMIN)
    assert r.status_code == 409


def test_follow_up_banco_fora_segura_a_mensagem(s, monkeypatch, motor):
    def fora(chave):
        raise cfg.ConfigIndisponivel(f"{chave}: conexão recusada")

    monkeypatch.setattr(apc, "ler_fresco", fora)
    monkeypatch.setattr(cfg, "cadencia_ativa", lambda: True)
    regua(s, "141", CORRETOR)
    rem = Remetente()
    res = cadencia.tick(session=s, sender=rem)
    assert [t for t in rem.telefones if t.startswith(_PREFIXO)] == []
    assert res.bloqueados.get("config_indisponivel", 0) >= 1


# ═════════════════════════════════════════════════════════════════════════════
# NUTRIÇÃO — interruptor por corretora + não trava no primeiro erro
# ═════════════════════════════════════════════════════════════════════════════


@pytest.fixture()
def funil(s):
    """As etapas do funil de remarketing (a nutrição cria se faltar)."""
    stages = nutricao._ensure_stages(s, TENANT)
    s.commit()
    return stages


def nutrido(s, funil, sufixo: str, corretora_id: int | None, *,
            telefone: str | None = None) -> tuple[int, int]:
    """Lead que esgotou a cadência, na etapa Base, com o marco de 30 dias vencido."""
    telefone = telefone or tel(sufixo)
    cid = regua(s, sufixo, corretora_id, status="remarketing", telefone=telefone, toque=9)
    lead = Lead(tenant_id=TENANT, nome="Nutrição", telefone=f"+{tel(sufixo)}",
                stage_id=funil[nutricao.STAGE_BASE].id)
    s.add(lead)
    s.commit()
    return cid, int(lead.id)


def _etapa(s, lead_id: int) -> str | None:
    s.expire_all()
    lead = s.get(Lead, lead_id)
    st = s.get(PipelineStage, lead.stage_id) if lead and lead.stage_id else None
    return (st.nome or "").strip() if st else None


def test_nutricao_corretora_desligada_marco_fica_devido(s, tela, funil, motor):
    gravar(tela, "nutricao_ativa", True)
    r = tela.put(f"/painel/automacoes/nutricao/corretoras/{CORRETOR}",
                 json={"ligada": False}, headers=ADMIN)
    assert r.status_code == 200 and r.json()["gravado"] is False
    assert r.json()["nutricao"]["sai_por"] == "corretor"
    da_corretor, lead_corretor = nutrido(s, funil, "201", CORRETOR)
    da_corretor, lead_corretor = nutrido(s, funil, "202", CORRETOR)
    rem = Remetente()
    res = nutricao.tick(session=s, sender=rem)
    assert tel("201") not in rem.telefones and tel("202") in rem.telefones
    assert res.bloqueados.get("corretora_desligada") == 1
    assert _etapa(s, lead_corretor) == "Base" and _etapa(s, lead_corretor) == "30 dias"
    s.expire_all()
    assert s.get(CadenciaEstado, da_corretor).proximo_toque_em <= datetime.now(timezone.utc)
    # o evento do marco grava por qual linha saiu
    marco = eventos_de(s, da_corretor, cadencia.TIPO_MENSAGEM)[0]
    assert marco["linha"] == "corretor" and marco["corretora_id"] == CORRETOR


def test_nutricao_telefone_invalido_falha_de_um_lead_nao_para_a_volta(s, tela, funil, motor):
    """Os três casos do defeito de 06/10: 24 dígitos, 14 dígitos e a Meta
    recusando um número no padrão. Nenhum segura os outros; o marco que já
    saiu fica gravado; a tela conta os inválidos."""
    gravar(tela, "nutricao_ativa", True)
    antes = mapa(tela)["nutricao"]["por_corretora"]["telefone_invalido"]["marcados"]

    colado = tel("301") + "+5500000000000"            # 24 dígitos: dois números colados
    catorze = tel("302") + "0"                     # 14 dígitos
    e24, _ = nutrido(s, funil, "301", CORRETOR, telefone=colado)
    e14, _ = nutrido(s, funil, "302", CORRETOR, telefone=catorze)
    recusado, lead_rec = nutrido(s, funil, "303", CORRETOR)
    bom1, lead1 = nutrido(s, funil, "304", CORRETOR)
    bom2, lead2 = nutrido(s, funil, "305", CORRETOR)

    rem = Remetente(falha_para={tel("303")})
    res = nutricao.tick(session=s, sender=rem)

    # (a) fora do padrão: sai da régua com evento, sem bater na Meta
    s.expire_all()
    for cid, dig in ((e24, 24), (e14, 14)):
        est = s.get(CadenciaEstado, cid)
        assert est.status == nutricao.STATUS_TELEFONE_INVALIDO
        ev = eventos_de(s, cid, nutricao.TIPO_TELEFONE_INVALIDO)
        assert ev and ev[0]["digitos"] == dig
    assert colado not in rem.telefones and catorze not in rem.telefones
    assert res.telefone_invalido == 2
    # (b) a Meta recusou UM: falha registrada, adiado 1 dia, laço seguiu
    assert res.falhas == 1
    falha = eventos_de(s, recusado, cadencia.TIPO_ENVIO_FALHOU)
    assert falha and falha[0]["nutricao"] is True and "131009" in falha[0]["erro"]
    est_rec = s.get(CadenciaEstado, recusado)
    assert est_rec.proximo_toque_em > datetime.now(timezone.utc) + timedelta(hours=23)
    assert _etapa(s, lead_rec) == "Base"
    # os bons saíram e ficaram gravados
    assert {tel("304"), tel("305")} <= set(rem.telefones)
    assert res.marcos_disparados >= 2
    assert _etapa(s, lead1) == "30 dias" and _etapa(s, lead2) == "30 dias"
    # (c) a tela mostra quantos estão com telefone inválido
    depois = mapa(tela)["nutricao"]["por_corretora"]
    assert depois["telefone_invalido"]["marcados"] == antes + 2
    corretor = next(c for c in depois["corretoras"] if c["corretora_id"] == CORRETOR)
    assert corretor["hoje"]["falhou"] >= 1 and corretor["hoje"]["saiu"] >= 1


def test_nutricao_erro_depois_de_um_marco_nao_desfaz_o_que_saiu(s, tela, funil, motor, monkeypatch):
    """Antes do conserto, um erro qualquer no lead seguinte dava rollback da
    volta inteira — o marco que já tinha saído voltava a ficar devido e sairia
    de novo no tick seguinte."""
    gravar(tela, "nutricao_ativa", True)
    primeiro, lead1 = nutrido(s, funil, "401", CORRETOR)
    segundo, _ = nutrido(s, funil, "402", CORRETOR)
    original = nutricao.reengajamento_guard.conversa_humana_ativa

    def explode_no_segundo(telefone, **kw):
        if telefone == tel("402"):
            raise RuntimeError("consulta quebrou")
        return original(telefone, **kw)

    monkeypatch.setattr(nutricao.reengajamento_guard, "conversa_humana_ativa",
                        explode_no_segundo)
    rem = Remetente()
    res = nutricao.tick(session=s, sender=rem)
    assert tel("401") in rem.telefones
    assert res.bloqueados.get("erro_no_lead", 0) >= 1
    s.expire_all()
    assert _etapa(s, lead1) == "30 dias"
    assert s.get(CadenciaEstado, primeiro).proximo_toque_em > datetime.now(timezone.utc)
    # e a volta seguinte NÃO manda de novo pro primeiro
    cadencia._LAST_ENQUEUE["at"] = 0.0
    rem2 = Remetente()
    nutricao.tick(session=s, sender=rem2)
    assert tel("401") not in rem2.telefones


# ═════════════════════════════════════════════════════════════════════════════
# BOAS-VINDAS — a roleta do Meta pela tela
# ═════════════════════════════════════════════════════════════════════════════


def _intake(sufixo: str):
    return lead_intake.intake_lead(
        nome="Fulano Meta", telefone=tel(sufixo), origem="meta_leadgen",
        sender=lambda *a, **k: {"ok": True, "dry_run": True, "sent": False},
    )


def test_roleta_editada_na_tela_vale_no_proximo_lead(s, tela):
    gravar(tela, "lead_intake_ativo", True)
    r = tela.put("/painel/automacoes/boas-vindas/roleta",
                 json={"corretoras": [CORRETOR, CORRETOR]}, headers=ADMIN)
    assert r.status_code == 200, r.text
    corpo = r.json()
    assert corpo["ok"] is True and corpo["gravado"] == [CORRETOR, CORRETOR]
    assert corpo["ultima_mudanca"]["autor"] == "dona"
    assert corpo["ultima_mudanca"]["depois"] == [CORRETOR, CORRETOR]
    vez = corpo["roleta"]["vez"]
    assert vez in (CORRETOR, CORRETOR)
    assert _intake("501").corretora_id == vez

    # tirou a Pessoa11 → o lead SEGUINTE já é da Pessoa10 (sem esperar cache)
    tela.put("/painel/automacoes/boas-vindas/roleta",
             json={"corretoras": [CORRETOR]}, headers=ADMIN)
    assert _intake("502").corretora_id == CORRETOR
    rol = tela.get("/painel/automacoes/boas-vindas/roleta", headers=ADMIN).json()
    assert rol["valendo"] == [CORRETOR] and rol["vez"] == CORRETOR
    corretor = next(c for c in rol["corretoras"] if c["corretora_id"] == CORRETOR)
    assert corretor["recebeu_7d"] >= 1 and corretor["recebeu_30d"] >= corretor["recebeu_7d"]
    assert corretor["e_a_vez"] is True


def test_roleta_gravada_por_outro_processo_vale_sem_esperar_o_cache(s, tela):
    """O cache de 30 s é por processo. Gravação vinda de outro processo (outro
    worker, script, o outro corpo) não o invalida — e o intake relê o banco."""
    gravar(tela, "lead_intake_ativo", True)
    tela.put("/painel/automacoes/boas-vindas/roleta",
             json={"corretoras": [CORRETOR]}, headers=ADMIN)
    assert cfg.roleta_corretoras() == [CORRETOR]          # cache deste processo: Pessoa11
    s.execute(_t("UPDATE config_operacional SET valor = CAST('[1]' AS jsonb) "
                 "WHERE chave = 'roleta_corretoras'"))
    s.commit()
    assert cfg.roleta_corretoras() == [CORRETOR]          # cache ainda velho
    assert _intake("505").corretora_id == CORRETOR


def test_roleta_vazia_so_com_confirmacao_e_o_mapa_avisa(s, tela):
    gravar(tela, "lead_intake_ativo", True)
    r = tela.put("/painel/automacoes/boas-vindas/roleta",
                 json={"corretoras": []}, headers=ADMIN)
    assert r.status_code == 409
    assert apc.ler_fresco("roleta_corretoras") is None   # nada gravado
    r = tela.put("/painel/automacoes/boas-vindas/roleta",
                 json={"corretoras": [], "confirmar_vazia": True}, headers=ADMIN)
    assert r.status_code == 200 and r.json()["gravado"] == []
    assert _intake("511").corretora_id is None
    m = mapa(tela)
    assert m["boas_vindas"]["roleta"]["vazia"] is True
    assert "ninguém recebe" in m["boas_vindas"]["roleta"]["aviso"]
    bv = next(x for x in m["resumo"] if x["id"] == "boas-vindas")
    assert bv["estado"] == "parada" and "roleta vazia" in bv["motivo"]


def test_roleta_recusa_o_que_nao_pode(s, tela):
    for corpo, codigo in (
        ({"corretoras": [CORRETOR, CORRETOR]}, 422),
        ({"corretoras": [999]}, 422),
        ({"corretoras": ["x"]}, 422),
        ({"corretoras": [True]}, 422),
        ({"corretoras": CORRETOR}, 422),
        ({"corretoras": [CORRETOR], "outra": 1}, 422),
        ({}, 422),
    ):
        r = tela.put("/painel/automacoes/boas-vindas/roleta", json=corpo, headers=ADMIN)
        assert r.status_code == codigo, (corpo, r.text)
    assert apc.ler_fresco("roleta_corretoras") is None


def test_lead_com_rmkt_nunca_cai_na_roleta_do_meta_editada(s, tela):
    gravar(tela, "lead_intake_ativo", True)
    gravar(tela, "handoff_remarketing_roleta", [])
    tela.put("/painel/automacoes/boas-vindas/roleta",
             json={"corretoras": [CORRETOR]}, headers=ADMIN)
    lid = s.execute(_t(
        "INSERT INTO leads (tenant_id, nome, telefone, owner_id, created_at, updated_at, "
        " veio_remarketing_em) VALUES (:t, 'RMKT', :tel, NULL, now(), now(), now()) "
        "RETURNING id"), {"t": TENANT, "tel": tel("521")}).scalar()
    s.commit()
    _intake("521")
    s.expire_all()
    dono = s.execute(_t("SELECT owner_id FROM leads WHERE id = :i"), {"i": lid}).scalar()
    assert dono is None, "card RMKT caiu na roleta do Meta"
    s.execute(_t("DELETE FROM config_operacional WHERE chave = 'handoff_remarketing_roleta'"))
    s.commit()


# ═════════════════════════════════════════════════════════════════════════════
# LINHAS e LISTA
# ═════════════════════════════════════════════════════════════════════════════


def test_linhas_uma_por_slug_com_dona_e_sem_inventar_saude(s, tela):
    pa.esquecer_linhas()
    r = tela.get("/painel/automacoes/linhas", headers=ADMIN)
    assert r.status_code == 200, r.text
    corpo = r.json()
    slugs = [l["slug"] for l in corpo["linhas"]]
    assert {"corretor", "corretor", "corretor", "corretor", "pessoa_d", "corretor"} <= set(slugs)
    corretor = next(l for l in corpo["linhas"] if l["slug"] == "corretor")
    assert corretor["registrada"] is True
    # a suíte não fala com a Meta: sem leitura é "desconhecida", nunca verde
    assert corretor["saude"]["cor"] == "desconhecida" and corretor["saude"]["erro"]
    assert "cadastro_de_numeros" in corpo["erros"]
    assert corpo["cache_s"] == 60


def test_lista_tem_as_quatro_automacoes_com_estado_e_numero_do_dia(s, tela):
    gravar(tela, "cadencia_ativa", False)
    m = mapa(tela)
    ids = [x["id"] for x in m["resumo"]]
    assert ids == ["boas-vindas", "follow-up", "remarketing", "nutricao"]
    fu = next(x for x in m["resumo"] if x["id"] == "follow-up")
    assert fu["estado"] == "desligada" and fu["motivo"] == "interruptor desligado"
    for x in m["resumo"]:
        assert set(x) >= {"estado", "motivo", "linhas", "numero_do_dia", "ultima_acao_em"}
        assert x["estado"] in ("indisponivel", "desligada", "parada", "agindo", "ligada")
    bv = m["boas_vindas"]["envios"]
    assert bv["em_novo_indisponivel"] is False
    assert set(bv["em_novo"]) == {"total", "sem_boas_vindas", "sem_dona", "por_corretora"}
    assert all(c["sai_por"] or c["linha"] is None for c in bv["corretoras"])
    nu = m["nutricao"]["por_corretora"]
    assert nu["sai_por"] == "corretor" and "Pessoa12" in nu["por_qual_linha"].replace("corretor", "Pessoa12")


def test_publicar_nao_muda_estado_padroes_vazios(s):
    """Sem linha no banco, as chaves novas valem 'todo mundo ligado' — o que
    era o comportamento antes delas existirem."""
    assert cfg.DEFAULTS["cadencia_por_corretora"]() == {}
    assert cfg.DEFAULTS["nutricao_por_corretora"]() == {}
    for chave in apc.CHAVES:
        assert apc.pode_enviar(chave, CORRETOR) == (True, None)
        assert apc.pode_enviar(chave, None) == (True, None)


def test_pendencias_do_front_nos_nomes_pedidos(s, tela, funil, motor):
    """PENDENCIAS-BACKEND do front, itens 6, 7 e 8: `nutricao.linha`,
    `boas_vindas.aguardando` e entregue/falha no `total` das três métricas."""
    gravar(tela, "nutricao_ativa", True)
    da_corretor, _ = nutrido(s, funil, "601", CORRETOR)
    nutricao.tick(session=s, sender=Remetente())
    wamid = eventos_de(s, da_corretor, cadencia.TIPO_MENSAGEM)[0]["wamid"]
    s.execute(_t("INSERT INTO mensagens_whatsapp (evolution_message_id, instance, remote_jid, "
                 " contato_telefone, from_corretora, tipo_mensagem, conteudo_metadata, "
                 " timestamp_evolution) VALUES (:w, 'acme_corretora_a', :j, :t, true, "
                 " 'template', CAST('{\"delivery_status\": \"read\"}' AS jsonb), now())"),
              {"w": wamid, "j": f"{tel('601')}@s.whatsapp.net", "t": tel("601")})
    s.commit()
    try:
        m = mapa(tela)
        assert m["nutricao"]["linha"]["slug"] == "corretor"
        assert m["nutricao"]["linha_corretora_id"] in (CORRETOR, None)
        assert isinstance(m["boas_vindas"]["aguardando"], int)
        for rota in ("/painel/automacoes/metricas", "/painel/automacoes/nutricao/metricas",
                     "/painel/automacoes/boas-vindas/metricas"):
            r = tela.get(rota, params={"dias": 7}, headers=ADMIN)
            assert r.status_code == 200, (rota, r.text)
            tot = r.json()["total"]
            assert tot["entrega_indisponivel"] is False, rota
            assert {"entregues", "falhas", "lidos", "sem_rastro"} <= set(tot), rota
        nut = tela.get("/painel/automacoes/nutricao/metricas", params={"dias": 7},
                       headers=ADMIN).json()["total"]
        assert nut["entregues"] >= 1 and nut["lidos"] >= 1
    finally:
        s.execute(_t("DELETE FROM mensagens_whatsapp WHERE evolution_message_id = :w"),
                  {"w": wamid})
        s.commit()
