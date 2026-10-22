"""A nutrição respeita quem pediu pra parar, por QUALQUER caminho (07/10/2026).

O furo: a nutrição chamava o emissor sem `origem`, e a trava de opt-out do
emissor só confere envio de automação com `origem`. Quem pediu pra parar pela
própria régua saía; quem pediu por outro caminho seguia recebendo marco.

Agora (`nutricao.tick`): uma leitura de `opt_out.mapa_de_bloqueio` por volta,
antes da entrada; estado de quem está lá vira `opt_out` com o caminho no
evento; leitura fora do ar = nenhum marco (fail-closed); o envio leva
`origem="nutricao"` nos dois caminhos (Pessoa12 e linha da dona).

Caminho inteiro, como em `test_nutricao_linha_da_dona.py`: banco de teste →
`nutricao.tick` de verdade → remetente FALSO. Nada sai da máquina.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text as _t

from app.db.models import CadenciaEstado, DisparoReengajamento, Lead, LeadEvent
from app.services import cadencia, nutricao
from app.services import opt_out
from app.services import opt_out_api_disparos as oad
from tests.test_automacoes_painel_manda import (  # noqa: F401 — fixtures por nome
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
    nutrido,
    regua,
    tel,
    tela,
)
from tests.test_nutricao_linha_da_dona import linhas, sem_teto  # noqa: F401

ORIGEM = nutricao.ORIGEM_NUTRICAO


# O emissor de verdade, capturado antes da fixture da suíte trocar por "ninguém".
_real_buscar = oad._buscar_telefones


def _limpar_disparos(ses) -> None:
    ses.execute(_t("DELETE FROM disparos_reengajamento WHERE telefone LIKE :p"),
                {"p": "%955700%"})
    ses.commit()


@pytest.fixture()
def s():
    """A sessão de sempre, mais a limpeza dos opt-outs que estes testes gravam."""
    from app.db.session import SessionLocal

    with SessionLocal() as ses:
        _limpar(ses)
        _limpar_disparos(ses)
        yield ses
        ses.rollback()
        _limpar(ses)
        _limpar_disparos(ses)


class Anota(Remetente):
    """Anota os kwargs de cada envio (é onde a `origem` aparece) e por qual
    linha saiu. `resposta` troca o que o emissor devolve."""

    def __init__(self, resposta: dict | None = None) -> None:
        super().__init__()
        self.kwargs_de: dict[str, dict] = {}
        self.resposta = resposta

    def __call__(self, telefone, texto, *, com_botao, **kw):
        self.kwargs_de[telefone] = dict(kw)
        if self.resposta is not None:
            self.telefones.append(telefone)
            return dict(self.resposta)
        return super().__call__(telefone, texto, com_botao=com_botao, **kw)


def _estado(s, cid: int) -> CadenciaEstado:
    s.expire_all()
    return s.get(CadenciaEstado, cid)


def _evento_opt_out(s, cid: int) -> dict:
    ev = eventos_de(s, cid, nutricao.TIPO_OPT_OUT)
    assert len(ev) == 1, f"esperava 1 evento opt_out pro estado {cid}, veio {ev}"
    return ev[0]


def _parou(s, cid: int, caminho: str, rem: Anota, telefone: str) -> dict:
    """O estado saiu da régua pelo caminho certo e nada saiu pra ele."""
    assert telefone not in rem.telefones, "marco saiu pra quem pediu pra parar"
    assert _estado(s, cid).status == "opt_out"
    assert eventos_de(s, cid, cadencia.TIPO_MENSAGEM) == []
    ev = _evento_opt_out(s, cid)
    assert ev["caminho"] == caminho and caminho in ev["caminhos"]
    assert ev["status_antes"] == "remarketing" and ev["nutricao"] is True
    return ev


def _opt_out_disparo(s, telefone: str, enviado_por: str) -> None:
    s.add(DisparoReengajamento(tenant_id=TENANT, telefone=telefone,
                               template=enviado_por, enviado_por=enviado_por,
                               opt_out=True, opt_out_em=datetime.now(timezone.utc)))
    s.commit()


# ── 1. Não perturbe ──────────────────────────────────────────────────────────


def test_nao_perturbe_nao_recebe_marco_vira_opt_out_e_o_card_fica(
        s, tela, funil, linhas, sem_teto):
    gravar(tela, "nutricao_ativa", True)
    cid = regua(s, "801", CORRETOR, status="remarketing", toque=9)
    lead = Lead(tenant_id=TENANT, nome="Pediu", telefone=f"+{tel('801')}",
                stage_id=opt_out.ETAPA_NAO_PERTURBE)
    s.add(lead)
    s.commit()
    limpo, lead_limpo = nutrido(s, funil, "802", CORRETOR)

    rem = Anota()
    res = nutricao.tick(session=s, sender=rem)

    ev = _parou(s, cid, opt_out.CAMINHO_NAO_PERTURBE, rem, tel("801"))
    assert ev["caminho_texto"] == "card na etapa Não perturbe"
    # a entrada NÃO puxou o card de "Não perturbe" (funil principal) pra Base
    assert _etapa(s, lead.id) == "Não perturbe"
    assert s.query(LeadEvent).filter(LeadEvent.lead_id == lead.id,
                                     LeadEvent.tipo == "nutricao_opt_out").count() == 1
    # o limpo da mesma volta recebeu
    assert tel("802") in rem.telefones and _etapa(s, lead_limpo) == "30 dias"
    assert res.opt_out == 1 and res.opt_out_por_caminho == {"nao_perturbe": 1}
    assert res.marcos_disparados == 1


# ── 2. remarketing novo e disparo antigo ─────────────────────────────────────


@pytest.mark.parametrize("enviado_por, caminho", [
    ("carrossel", opt_out.CAMINHO_REMARKETING_NOVO),
    ("dona", opt_out.CAMINHO_DISPARO_ANTIGO),
])
def test_parar_no_disparo_vira_opt_out_com_o_caminho(
        s, tela, funil, linhas, sem_teto, enviado_por, caminho):
    gravar(tela, "nutricao_ativa", True)
    cid, lead_id = nutrido(s, funil, "811", CORRETOR)
    # gravado em outro formato (sem 55, sem o 9): a chave é o sufixo-8
    _opt_out_disparo(s, tel("811")[-10:], enviado_por)

    rem = Anota()
    res = nutricao.tick(session=s, sender=rem)

    _parou(s, cid, caminho, rem, tel("811"))
    assert _etapa(s, lead_id) == "Base", "etapa avançou sem marco"
    assert res.opt_out_por_caminho == {caminho: 1}


# ── 3. api-disparos ──────────────────────────────────────────────────────────


def test_opt_out_no_api_disparos_vira_opt_out_mesmo_sem_estar_devido(
        s, tela, funil, linhas, sem_teto, opt_out_api_disparos_de_teste):
    gravar(tela, "nutricao_ativa", True)
    devido, _ = nutrido(s, funil, "821", CORRETOR)
    longe, _ = nutrido(s, funil, "822", CORRETOR)
    est = s.get(CadenciaEstado, longe)
    est.proximo_toque_em = datetime.now(timezone.utc) + timedelta(days=20)
    s.commit()
    opt_out_api_disparos_de_teste.extend([tel("821"), "+" + tel("822")])

    rem = Anota()
    res = nutricao.tick(session=s, sender=rem)

    _parou(s, devido, opt_out.CAMINHO_API_DISPAROS, rem, tel("821"))
    # quem só teria marco daqui a 20 dias sai da régua JÁ, não no dia do marco
    _parou(s, longe, opt_out.CAMINHO_API_DISPAROS, rem, tel("822"))
    assert res.opt_out == 2 and rem.telefones == []


def test_api_disparos_sem_url_levanta_sem_vazar_nada(monkeypatch, tmp_path):
    from app.config import settings

    vazio = tmp_path / "api-disparos.env"
    vazio.write_text("OUTRA=1\n")
    monkeypatch.setattr(settings, "api_disparos_db_env_file", str(vazio))
    with pytest.raises(oad.Indisponivel):
        oad._url()
    # URL que não conecta: o erro leva só o tipo, nunca a URL (pode ter senha)
    vazio.write_text("DATABASE_URL=postgresql+psycopg://u:contato@example.com:1/x\n")
    monkeypatch.setattr(oad, "_buscar_telefones", _real_buscar)
    with pytest.raises(oad.Indisponivel) as e:
        oad.sufixos_bloqueados()
    assert "segredo" not in str(e.value)


# ── 4. consulta fora do ar segura o marco ────────────────────────────────────


def test_leitura_de_opt_out_fora_do_ar_segura_todo_marco(
        s, tela, funil, linhas, sem_teto, monkeypatch):
    gravar(tela, "nutricao_ativa", True)
    a, lead_a = nutrido(s, funil, "831", CORRETOR)
    b, _ = nutrido(s, funil, "832", CORRETOR)

    def _fora():
        raise oad.Indisponivel("api-disparos: OperationalError")

    monkeypatch.setattr(oad, "_buscar_telefones", _fora)
    rem = Anota()
    res = nutricao.tick(session=s, sender=rem)

    assert rem.telefones == [] and res.marcos_disparados == 0
    assert res.bloqueados.get(nutricao.BLOQUEIO_OPT_OUT_INDISPONIVEL) == 2
    for cid in (a, b):
        est = _estado(s, cid)
        assert est.status == "remarketing"                      # não inventou opt-out
        assert est.proximo_toque_em <= datetime.now(timezone.utc)  # segue devido
        assert eventos_de(s, cid, cadencia.TIPO_MENSAGEM) == []
    assert _etapa(s, lead_a) == "Base"

    # voltou a responder: o marco que ficou devido sai na volta seguinte
    monkeypatch.setattr(oad, "_buscar_telefones", lambda: [])
    res2 = nutricao.tick(session=s, sender=rem)
    assert res2.marcos_disparados == 2 and _etapa(s, lead_a) == "30 dias"


def test_leitura_do_copiloto_fora_do_ar_tambem_segura(
        s, tela, funil, linhas, sem_teto, monkeypatch):
    gravar(tela, "nutricao_ativa", True)
    nutrido(s, funil, "835", CORRETOR)
    real = opt_out.mapa_de_bloqueio

    def _quebra(session, **kw):
        session.execute(_t("SELECT * FROM tabela_que_nao_existe_optout"))
        return real(session, **kw)

    monkeypatch.setattr(opt_out, "mapa_de_bloqueio", _quebra)
    rem = Anota()
    res = nutricao.tick(session=s, sender=rem)
    assert rem.telefones == []
    assert res.bloqueados.get(nutricao.BLOQUEIO_OPT_OUT_INDISPONIVEL) == 1


# ── 5. lead limpo continua recebendo, com o carimbo ──────────────────────────


def test_lead_limpo_recebe_e_o_envio_leva_origem_nos_dois_caminhos(
        s, tela, funil, linhas, sem_teto):
    gravar(tela, "nutricao_ativa", True)
    pela_corretor, lead_corretor = nutrido(s, funil, "841", CORRETOR)
    pela_corretor, lead_corretor = nutrido(s, funil, "842", CORRETOR)

    rem = Anota()
    res = nutricao.tick(session=s, sender=rem)

    assert res.marcos_disparados == 2 and res.opt_out == 0
    assert rem.kwargs_de[tel("841")]["origem"] == ORIGEM
    assert rem.kwargs_de[tel("841")]["linha"] is None          # caminho da Pessoa12
    assert rem.kwargs_de[tel("842")]["origem"] == ORIGEM
    assert rem.kwargs_de[tel("842")]["linha"].slug == "corretor"
    assert _etapa(s, lead_corretor) == "30 dias" and _etapa(s, lead_corretor) == "30 dias"
    assert _estado(s, pela_corretor).status == "remarketing"


def test_sender_da_corretor_repassa_origem_pro_emissor(monkeypatch):
    """`_default_sender` sem linha descartava `origem` até 07/10: a trava do
    emissor nunca ligava pro marco que saía pela Pessoa12."""
    import app.api.cloud_corretora_a as cg

    pegou: dict = {}

    def _corretor(telefone, first_name, **kw):
        pegou.update(kw)
        return {"ok": True, "sent": False, "dry_run": True}

    monkeypatch.setattr(cg, "enviar_template_corretor", _corretor)
    cadencia._default_sender(tel("843"), "oi", com_botao=False, origem=ORIGEM)
    assert pegou.get("origem") == ORIGEM
    pegou.clear()
    cadencia._default_sender(tel("843"), "oi", com_botao=False)
    assert "origem" not in pegou                               # quem não passa, igual


# ── 6. a trava do emissor barra na hora de sair ─────────────────────────────


def test_emissor_barrou_vira_opt_out_e_nao_conta_marco(s, tela, funil, linhas, sem_teto):
    gravar(tela, "nutricao_ativa", True)
    cid, lead_id = nutrido(s, funil, "851", CORRETOR)
    rem = Anota(resposta={"ok": False, "dry_run": False, "sent": False,
                          "wamid": None, "template": None, "erro": "opt_out",
                          "messages": []})
    res = nutricao.tick(session=s, sender=rem)

    assert _estado(s, cid).status == "opt_out"
    assert eventos_de(s, cid, cadencia.TIPO_MENSAGEM) == []
    assert _evento_opt_out(s, cid)["caminho"] == nutricao.CAMINHO_EMISSOR
    assert _etapa(s, lead_id) == "Base" and res.marcos_disparados == 0


# ── 7. quem saiu, saiu ──────────────────────────────────────────────────────


def test_quem_saiu_nunca_mais_e_pego(s, tela, funil, linhas, sem_teto):
    gravar(tela, "nutricao_ativa", True)
    cid, _ = nutrido(s, funil, "861", CORRETOR)
    _opt_out_disparo(s, tel("861"), "carrossel")
    nutricao.tick(session=s, sender=Anota())
    assert _estado(s, cid).status == "opt_out"

    # o pedido some da fonte (card arrastado, linha apagada): o estado continua fora
    s.execute(_t("DELETE FROM disparos_reengajamento WHERE telefone = :t"), {"t": tel("861")})
    s.commit()
    rem = Anota()
    res = nutricao.tick(session=s, sender=rem)
    assert rem.telefones == [] and res.opt_out == 0
    assert _estado(s, cid).status == "opt_out"
    assert len(eventos_de(s, cid, nutricao.TIPO_OPT_OUT)) == 1
