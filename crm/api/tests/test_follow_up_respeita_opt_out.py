"""O follow-up de 8 toques respeita quem pediu pra parar, por QUALQUER caminho (07/10/2026).

O furo: `cadencia.tick` só olhava o próprio `status`. A lead 7880 clicou
"Parar mensagens" em 30/08 (gravado só no api-disparos) e levou mais 5
follow-ups, 3 deles com o card em "Não perturbe".

Agora o tick usa as MESMAS funções da nutrição (`opt_out.ler_mapa` e
`opt_out.tirar_da_regua`): uma leitura do mapa por volta; estado da régua de
quem está lá vira `opt_out` com o caminho no evento, devido ou não; leitura
fora do ar = nenhum toque (fail-closed); o toque e o remarketing genérico
levam `origem` e caem na trava do emissor.

Caminho inteiro: banco de teste → `cadencia.tick` de verdade → remetente FALSO.
Nada sai da máquina.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text as _t

from app.db.models import CadenciaEstado, Lead, LeadEvent
from app.services import cadencia, nutricao, opt_out
from app.services import opt_out_api_disparos as oad
from tests.test_automacoes_painel_manda import (  # noqa: F401 — fixtures por nome
    CORRETOR,
    CORRETOR,
    TENANT,
    eventos_de,
    funil,
    gravar,
    motor,
    nutrido,
    regua,
    tel,
    tela,
)
from tests.test_nutricao_respeita_opt_out import (  # noqa: F401 — fixture `s`
    Anota,
    _opt_out_disparo,
    s,
)

BARROU = {"ok": False, "dry_run": False, "sent": False, "wamid": None,
          "template": None, "erro": "opt_out", "messages": []}


def _estado(s, cid: int) -> CadenciaEstado:
    s.expire_all()
    return s.get(CadenciaEstado, cid)


def _evento_opt_out(s, cid: int) -> dict:
    ev = eventos_de(s, cid, opt_out.TIPO_OPT_OUT)
    assert len(ev) == 1, f"esperava 1 evento opt_out pro estado {cid}, veio {ev}"
    return ev[0]


def _parou(s, cid: int, caminho: str, rem: Anota, telefone: str, *,
           status_antes: str = "ativa") -> dict:
    """O estado saiu da régua pelo caminho certo e nada saiu pra ele."""
    assert telefone not in rem.telefones, "follow-up saiu pra quem pediu pra parar"
    est = _estado(s, cid)
    assert est.status == "opt_out"
    assert eventos_de(s, cid, cadencia.TIPO_MENSAGEM) == []
    ev = _evento_opt_out(s, cid)
    assert ev["caminho"] == caminho and caminho in ev["caminhos"]
    assert ev["status_antes"] == status_antes
    assert ev["cadencia"] is True and ev["motor"] == "cadencia"
    return ev


def _card(s, sufixo: str, stage_id: int | None = None) -> int:
    lead = Lead(tenant_id=TENANT, nome="Follow-up", telefone=f"+{tel(sufixo)}",
                stage_id=stage_id)
    s.add(lead)
    s.commit()
    return int(lead.id)


# ── 1. Não perturbe ──────────────────────────────────────────────────────────


def test_nao_perturbe_nao_recebe_toque_vira_opt_out_e_o_card_fica(s, tela, motor):
    gravar(tela, "cadencia_ativa", True)
    cid = regua(s, "901", CORRETOR)
    lead_id = _card(s, "901", opt_out.ETAPA_NAO_PERTURBE)
    limpo = regua(s, "902", CORRETOR)

    rem = Anota()
    res = cadencia.tick(session=s, sender=rem)

    ev = _parou(s, cid, opt_out.CAMINHO_NAO_PERTURBE, rem, tel("901"))
    assert ev["caminho_texto"] == "card na etapa Não perturbe"
    assert _estado(s, cid).toque_atual == 0, "o toque avançou sem sair"
    # o card continua onde a pessoa (ou a varredura) pôs
    s.expire_all()
    assert s.get(Lead, lead_id).stage_id == opt_out.ETAPA_NAO_PERTURBE
    assert s.query(LeadEvent).filter(LeadEvent.lead_id == lead_id,
                                     LeadEvent.tipo == "follow_up_opt_out").count() == 1
    # o limpo da mesma volta recebeu
    assert tel("902") in rem.telefones and eventos_de(s, limpo, cadencia.TIPO_MENSAGEM)
    assert res.opt_out == 1 and res.opt_out_por_caminho == {"nao_perturbe": 1}


# ── 2. remarketing novo e disparo antigo ─────────────────────────────────────


@pytest.mark.parametrize("enviado_por, caminho", [
    ("carrossel", opt_out.CAMINHO_REMARKETING_NOVO),
    ("dona", opt_out.CAMINHO_DISPARO_ANTIGO),
])
def test_parar_no_disparo_vira_opt_out_com_o_caminho(s, tela, motor, enviado_por, caminho):
    gravar(tela, "cadencia_ativa", True)
    cid = regua(s, "911", CORRETOR)
    # gravado em outro formato (sem 55, sem o 9): a chave é o sufixo-8
    _opt_out_disparo(s, tel("911")[-10:], enviado_por)

    rem = Anota()
    res = cadencia.tick(session=s, sender=rem)

    _parou(s, cid, caminho, rem, tel("911"))
    assert res.opt_out_por_caminho == {caminho: 1}


# ── 3. api-disparos (o caminho da 7880) ──────────────────────────────────────


def test_api_disparos_vira_opt_out_mesmo_sem_estar_devido(
        s, tela, motor, opt_out_api_disparos_de_teste):
    gravar(tela, "cadencia_ativa", True)
    devido = regua(s, "921", CORRETOR)
    longe = regua(s, "922", CORRETOR)
    est = s.get(CadenciaEstado, longe)
    est.proximo_toque_em = datetime.now(timezone.utc) + timedelta(days=5)
    s.commit()
    opt_out_api_disparos_de_teste.extend([tel("921"), "+" + tel("922")])

    rem = Anota()
    res = cadencia.tick(session=s, sender=rem)

    _parou(s, devido, opt_out.CAMINHO_API_DISPAROS, rem, tel("921"))
    # quem só teria toque daqui a 5 dias sai da régua JÁ
    _parou(s, longe, opt_out.CAMINHO_API_DISPAROS, rem, tel("922"))
    assert res.opt_out == 2 and [t for t in rem.telefones if t.startswith(tel(""))] == []


def test_remarketing_generico_tambem_respeita(s, tela, motor, opt_out_api_disparos_de_teste):
    """O caminho genérico de 30 em 30 dias (estado `remarketing` fora do funil
    da nutrição) cai na mesma trava."""
    gravar(tela, "cadencia_ativa", True)
    pediu = regua(s, "925", CORRETOR, status="remarketing", toque=9)
    limpo = regua(s, "926", CORRETOR, status="remarketing", toque=9)
    opt_out_api_disparos_de_teste.append(tel("925"))

    rem = Anota()
    res = cadencia.tick(session=s, sender=rem)

    _parou(s, pediu, opt_out.CAMINHO_API_DISPAROS, rem, tel("925"),
           status_antes="remarketing")
    assert tel("926") in rem.telefones and res.remarketing == 1
    assert rem.kwargs_de[tel("926")]["origem"] == cadencia.ORIGEM_CADENCIA_REMARKETING
    assert eventos_de(s, limpo, cadencia.TIPO_MENSAGEM)[0]["remarketing"] is True


# ── 4. consulta fora do ar segura o toque ────────────────────────────────────


def test_leitura_de_opt_out_fora_do_ar_segura_todo_toque(s, tela, motor, monkeypatch):
    gravar(tela, "cadencia_ativa", True)
    a = regua(s, "931", CORRETOR)
    b = regua(s, "932", CORRETOR)
    r = regua(s, "933", CORRETOR, status="remarketing", toque=9)

    def _fora():
        raise oad.Indisponivel("api-disparos: OperationalError")

    monkeypatch.setattr(oad, "_buscar_telefones", _fora)
    rem = Anota()
    res = cadencia.tick(session=s, sender=rem)

    assert [t for t in rem.telefones if t.startswith(tel(""))] == []
    assert res.mensagens_enfileiradas == 0 and res.remarketing == 0
    assert res.bloqueados.get(opt_out.BLOQUEIO_OPT_OUT_INDISPONIVEL, 0) >= 3
    for cid, status in ((a, "ativa"), (b, "ativa"), (r, "remarketing")):
        est = _estado(s, cid)
        assert est.status == status                                # não inventou opt-out
        assert est.proximo_toque_em <= datetime.now(timezone.utc)  # segue devido
        assert eventos_de(s, cid, cadencia.TIPO_MENSAGEM) == []
    assert _estado(s, a).toque_atual == 0

    # voltou a responder: o que ficou devido sai na volta seguinte
    monkeypatch.setattr(oad, "_buscar_telefones", lambda: [])
    cadencia._LAST_ENQUEUE["at"] = 0.0
    rem2 = Anota()
    cadencia.tick(session=s, sender=rem2)
    assert {tel("931"), tel("932"), tel("933")} <= set(rem2.telefones)


def test_leitura_do_copiloto_fora_do_ar_tambem_segura(s, tela, motor, monkeypatch):
    gravar(tela, "cadencia_ativa", True)
    regua(s, "935", CORRETOR)
    real = opt_out.mapa_de_bloqueio

    def _quebra(session, **kw):
        session.execute(_t("SELECT * FROM tabela_que_nao_existe_optout"))
        return real(session, **kw)

    monkeypatch.setattr(opt_out, "mapa_de_bloqueio", _quebra)
    rem = Anota()
    res = cadencia.tick(session=s, sender=rem)
    assert tel("935") not in rem.telefones
    assert res.bloqueados.get(opt_out.BLOQUEIO_OPT_OUT_INDISPONIVEL, 0) >= 1


# ── 5. lead limpo segue, com o carimbo ───────────────────────────────────────


def test_lead_limpo_recebe_o_toque_com_origem(s, tela, motor):
    gravar(tela, "cadencia_ativa", True)
    cid = regua(s, "941", CORRETOR)

    rem = Anota()
    res = cadencia.tick(session=s, sender=rem)

    assert tel("941") in rem.telefones and res.opt_out == 0
    assert rem.kwargs_de[tel("941")]["origem"] == cadencia.ORIGEM_CADENCIA
    est = _estado(s, cid)
    assert est.status == "ativa" and est.toque_atual == 1
    assert eventos_de(s, cid, cadencia.TIPO_MENSAGEM)


# ── 6. a trava do emissor barra na hora de sair ─────────────────────────────


@pytest.mark.parametrize("status, toque", [("ativa", 0), ("remarketing", 9)])
def test_emissor_barrou_vira_opt_out_e_nao_conta(s, tela, motor, status, toque):
    gravar(tela, "cadencia_ativa", True)
    cid = regua(s, "951", CORRETOR, status=status, toque=toque)
    rem = Anota(resposta=BARROU)
    res = cadencia.tick(session=s, sender=rem)

    est = _estado(s, cid)
    assert est.status == "opt_out" and est.toque_atual == toque
    assert eventos_de(s, cid, cadencia.TIPO_MENSAGEM) == []
    assert _evento_opt_out(s, cid)["caminho"] == opt_out.CAMINHO_EMISSOR
    assert res.mensagens_enfileiradas == 0 and res.remarketing == 0
    assert res.opt_out_por_caminho == {opt_out.CAMINHO_EMISSOR: 1}


# ── 7. quem saiu, saiu ──────────────────────────────────────────────────────


def test_quem_saiu_nunca_mais_e_pego(s, tela, motor):
    gravar(tela, "cadencia_ativa", True)
    cid = regua(s, "961", CORRETOR)
    _opt_out_disparo(s, tel("961"), "carrossel")
    cadencia.tick(session=s, sender=Anota())
    assert _estado(s, cid).status == "opt_out"

    # o pedido some da fonte (card arrastado, linha apagada): o estado continua fora
    s.execute(_t("DELETE FROM disparos_reengajamento WHERE telefone = :t"), {"t": tel("961")})
    s.commit()
    cadencia._LAST_ENQUEUE["at"] = 0.0
    rem = Anota()
    res = cadencia.tick(session=s, sender=rem)
    assert tel("961") not in rem.telefones and res.opt_out == 0
    assert _estado(s, cid).status == "opt_out"
    assert len(eventos_de(s, cid, opt_out.TIPO_OPT_OUT)) == 1


# ── 8. uma função só, nos dois motores ──────────────────────────────────────


def test_os_dois_motores_usam_a_mesma_leitura_e_a_mesma_marcacao(
        s, tela, motor, funil, monkeypatch, opt_out_api_disparos_de_teste):
    gravar(tela, "cadencia_ativa", True)
    gravar(tela, "nutricao_ativa", True)
    regua(s, "971", CORRETOR)
    nutrido(s, funil, "972", CORRETOR)
    opt_out_api_disparos_de_teste.extend([tel("971"), tel("972")])

    leram: list[str] = []
    marcaram: list[str] = []
    ler, tirar = opt_out.ler_mapa, opt_out.tirar_da_regua

    def _ler(session, *, motor):
        leram.append(motor)
        return ler(session, motor=motor)

    def _tirar(*a, motor, **kw):
        marcaram.append(motor)
        return tirar(*a, motor=motor, **kw)

    monkeypatch.setattr(opt_out, "ler_mapa", _ler)
    monkeypatch.setattr(opt_out, "tirar_da_regua", _tirar)
    nutricao.tick(session=s, sender=Anota())
    cadencia.tick(session=s, sender=Anota())

    assert leram == ["nutricao", "cadencia"]
    assert marcaram == ["nutricao", "cadencia"]
