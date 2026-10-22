"""Kanban do CRM: Corretor (8) e Pessoa7 (7) com o próprio funil; a Pessoa12 (3) acessa as duas.

Decisão da Dona, 01/10/2026: "o que é da Pessoa12 fica na Pessoa12, o que é da Corretor fica no
card de usuário da Corretor, o que é da Pessoa7 no da Pessoa7. A Pessoa12 tem acesso a todos
— aí ela trabalha por lá."

Antes: a Corretor e a Pessoa7 adotavam lead na janela de ligação, o lead nascia com
elas como donas e ficava INVISÍVEL no kanban pra todo mundo, porque a trava de
exibição era `CRM_CORRETORAS_ATIVAS=[1,2,3]` (casos Fulano, lead 681, e
Fulano, lead 728).

Prova aqui que:
  1. Corretor vê só os dela, Pessoa7 só os dela;
  2. a Pessoa12 vê os dela + os da Corretor + os da Pessoa7, e cada card diz de quem é;
  3. Pessoa10 e Pessoa11 continuam vendo exatamente os próprios leads;
  4. admin vê tudo e `?corretora=8` filtra;
  5. a visão da Pessoa12 sai de `DELEGACAO_ATENDIMENTO` — tirar a delegação derruba
     o teste (ele morde);
  6. a Pessoa12 consegue TRABALHAR o card da delegada (mover etapa, resolver o
     vínculo do chat, adotar etapa, nota) e ninguém mais ganha esse alcance;
  7. o board de remarketing segue a mesma regra.

DB isolado: SQLite in-memory injetado via monkeypatch de painel.SessionLocal
(mesmo padrão de test_endpoint_painel_crm_board.py). Zero prod, zero envio.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import BigInteger, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.api.painel as painel
from app.config import Settings, settings
from app.db.models import (
    Base,
    CadenciaEstado,
    CadenciaEvento,
    Corretora,
    Lead,
    LeadEvent,
    LeadSnapshot,
    PipelineStage,
)
from app.main import app


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(element, compiler, **kw):  # noqa: ANN001
    return "JSON"


@compiles(BigInteger, "sqlite")
def _compile_bigint_sqlite(element, compiler, **kw):  # noqa: ANN001
    return "INTEGER"


AGORA = datetime.now(timezone.utc)

CORRETOR, CORRETOR, CORRETOR, PESSOA_D, CORRETOR = 1, 2, 3, 7, 8
ROTULOS = {CORRETOR: "Pessoa10", CORRETOR: "Pessoa11", CORRETOR: "Pessoa12", PESSOA_D: "Pessoa7", CORRETOR: "Corretor"}

# lead_id → dona. Funil principal.
PRINCIPAL = {
    101: CORRETOR, 102: CORRETOR,
    201: CORRETOR,
    301: CORRETOR, 302: CORRETOR,
    701: PESSOA_D,
    801: CORRETOR, 802: CORRETOR,   # 801 = "Fulano", 802 = "Fulano" do caso real
}
# lead_id → dona. Funil de remarketing (etapa "Base").
REMARKETING = {191: CORRETOR, 391: CORRETOR, 791: PESSOA_D, 891: CORRETOR}


def _ids_de(dono: int, mapa: dict[int, int] = PRINCIPAL) -> set[int]:
    return {lid for lid, cid in mapa.items() if cid == dono}


@pytest.fixture()
def factory(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        engine,
        tables=[
            Corretora.__table__,
            PipelineStage.__table__,
            Lead.__table__,
            LeadEvent.__table__,
            LeadSnapshot.__table__,
            CadenciaEstado.__table__,
            CadenciaEvento.__table__,
        ],
    )
    f = sessionmaker(bind=engine)
    monkeypatch.setattr(painel, "SessionLocal", f)
    # A trava de exibição roda com o DEFAULT do código — é ele que vai pra
    # produção. Se o default perder o 7 ou o 8, estes testes caem junto.
    monkeypatch.setattr(
        settings,
        "crm_corretoras_ativas",
        list(Settings.model_fields["crm_corretoras_ativas"].default),
    )
    # O rótulo vem do cadastro (`corretoras`) com cache de 5 min; no SQLite a
    # consulta não roda (split_part) e cairia no fallback {1,2,3}. O que este
    # teste precisa provar é que o CARD carrega o rótulo da dona — o cadastro
    # de produção é conferido na prova do deploy.
    monkeypatch.setattr(painel, "CORRETORA_LABEL", dict(ROTULOS))
    with f() as s:
        st_novo = PipelineStage(id=1, tenant_id=1, nome="Novo", ordem=1, funil="principal")
        st_atend = PipelineStage(
            id=2, tenant_id=1, nome="Em atendimento", ordem=2, funil="principal"
        )
        st_base = PipelineStage(id=9, tenant_id=1, nome="Base", ordem=1, funil="remarketing")
        s.add_all([st_novo, st_atend, st_base])
        s.flush()
        for lid, dono in PRINCIPAL.items():
            s.add(Lead(
                id=lid, tenant_id=1, external_ref=f"ref_{lid}",
                nome=f"Lead {lid} de {ROTULOS[dono]}",
                telefone=f"+5500000000000{lid:05d}", owner_id=dono, stage_id=st_novo.id,
                created_at=AGORA, updated_at=AGORA,
            ))
        for lid, dono in REMARKETING.items():
            s.add(Lead(
                id=lid, tenant_id=1, external_ref=f"ref_{lid}",
                nome=f"Remarketing {lid} de {ROTULOS[dono]}",
                telefone=f"+5500000000000{lid:05d}", owner_id=dono, stage_id=st_base.id,
                created_at=AGORA, updated_at=AGORA,
            ))
        s.commit()
    return f


@pytest.fixture()
def client(factory):
    return TestClient(app)


def _board(client, user: str, qs: str = "") -> dict:
    r = client.get(f"/painel/crm/board{qs}", headers={"X-Painel-User": user})
    assert r.status_code == 200, r.text
    return r.json()


def _cards(d: dict) -> list[dict]:
    return [c for col in d["colunas"] for c in col["cards"]]


def _ids(d: dict) -> set[int]:
    return {c["lead_id"] for c in _cards(d)}


def _donas(d: dict) -> set[int]:
    return {c["corretora_id"] for c in _cards(d)}


# ── 0. Config: a trava de exibição abre pra Pessoa7 e pra Corretor ─────────────────

def test_default_da_trava_inclui_pessoa_d_e_corretor():
    assert Settings.model_fields["crm_corretoras_ativas"].default == [1, 2, 3, 7, 8]


def test_delegacao_e_corretor_cobrindo_pessoa_d_e_corretor():
    assert painel.DELEGACAO_ATENDIMENTO == {CORRETOR: {PESSOA_D, CORRETOR}}
    assert painel._cids_atendimento(CORRETOR) == [CORRETOR, PESSOA_D, CORRETOR]


# ── 1. Cada uma com o próprio funil ──────────────────────────────────────────

def test_corretor_ve_so_os_dela(client):
    d = _board(client, "corretor")
    assert _ids(d) == _ids_de(CORRETOR)
    assert _donas(d) == {CORRETOR}
    assert d["total"] == 2


def test_pessoa_d_ve_so_os_dela(client):
    d = _board(client, "pessoa_d")
    assert _ids(d) == _ids_de(PESSOA_D)
    assert _donas(d) == {PESSOA_D}
    assert d["total"] == 1


def test_corretor_e_pessoa_d_nao_se_enxergam_nem_enxergam_a_corretor(client):
    """Delegação é mão única: quem empresta o funil não herda o de ninguém."""
    assert _ids(_board(client, "corretor")).isdisjoint(_ids_de(CORRETOR) | _ids_de(PESSOA_D))
    assert _ids(_board(client, "pessoa_d")).isdisjoint(_ids_de(CORRETOR) | _ids_de(CORRETOR))


# ── 2. A Pessoa12 vê as três visões, e o card diz de quem é ────────────────────────

def test_corretor_ve_os_dela_mais_corretor_e_pessoa_d(client):
    d = _board(client, "corretor")
    assert _ids(d) == _ids_de(CORRETOR) | _ids_de(CORRETOR) | _ids_de(PESSOA_D)
    assert _donas(d) == {CORRETOR, CORRETOR, PESSOA_D}
    assert d["total"] == 5
    assert d["corretoras_visiveis"] == [CORRETOR, PESSOA_D, CORRETOR]


def test_card_da_corretor_diz_de_quem_e(client):
    """Card sem nome de dona não serve: a Pessoa12 precisa saber de quem é."""
    for c in _cards(_board(client, "corretor")):
        assert c["corretora_label"] == ROTULOS[c["corretora_id"]], c
    por_lead = {c["lead_id"]: c["corretora_label"] for c in _cards(_board(client, "corretor"))}
    assert por_lead[801] == "Corretor"
    assert por_lead[802] == "Corretor"
    assert por_lead[701] == "Pessoa7"
    assert por_lead[301] == "Pessoa12"


def test_corretor_nunca_ve_lead_da_corretor_nem_da_corretor(client):
    assert _ids(_board(client, "corretor")).isdisjoint(_ids_de(CORRETOR) | _ids_de(CORRETOR))


def test_corretor_filtra_uma_das_visoes(client):
    """`?corretora=` vale pra corretora só DENTRO do que ela já pode ver."""
    d = _board(client, "corretor", "?corretora=8")
    assert _ids(d) == _ids_de(CORRETOR)
    assert d["filtro_corretora"] == CORRETOR
    d = _board(client, "corretor", "?corretora=7")
    assert _ids(d) == _ids_de(PESSOA_D)
    d = _board(client, "corretor", "?corretora=3")
    assert _ids(d) == _ids_de(CORRETOR)


def test_corretor_nao_burla_o_filtro_pra_ver_a_corretor(client):
    d = _board(client, "corretor", "?corretora=1")
    assert _ids(d) == _ids_de(CORRETOR) | _ids_de(CORRETOR) | _ids_de(PESSOA_D)
    assert CORRETOR not in _donas(d)


# ── 3. Pessoa10 e Pessoa11: idêntico ao de antes ─────────────────────────────────────

@pytest.mark.parametrize("user,cid", [("corretor", CORRETOR), ("corretor", CORRETOR)])
def test_corretor_e_corretor_inalteradas(client, user, cid):
    d = _board(client, user)
    assert _ids(d) == _ids_de(cid)
    assert _donas(d) == {cid}
    assert d["filtro_corretora"] == cid
    assert d["corretoras_visiveis"] == [cid]


@pytest.mark.parametrize("user,cid", [("corretor", CORRETOR), ("corretor", CORRETOR), ("corretor", CORRETOR),
                                      ("pessoa_d", PESSOA_D)])
@pytest.mark.parametrize("alheia", [CORRETOR, PESSOA_D, CORRETOR, CORRETOR])
def test_quem_nao_tem_delegacao_nao_burla_filtro(client, user, cid, alheia):
    d = _board(client, user, f"?corretora={alheia}")
    assert _ids(d) == _ids_de(cid)


# ── 4. Admin ─────────────────────────────────────────────────────────────────

def test_admin_ve_todas(client):
    d = _board(client, "dona")
    assert _ids(d) == set(PRINCIPAL)
    assert _donas(d) == {CORRETOR, CORRETOR, CORRETOR, PESSOA_D, CORRETOR}
    assert d["filtro_corretora"] is None
    assert d["corretoras_visiveis"] is None


def test_admin_filtra_a_corretor(client):
    d = _board(client, "dona", "?corretora=8")
    assert _ids(d) == _ids_de(CORRETOR)
    assert d["filtro_corretora"] == CORRETOR


def test_admin_filtra_a_pessoa_d(client):
    assert _ids(_board(client, "dona", "?corretora=7")) == _ids_de(PESSOA_D)


# ── 5. O teste morde: sem delegação, a Pessoa12 volta a ver só os dela ─────────────

def test_sem_delegacao_corretor_ve_so_os_dela(client, monkeypatch):
    monkeypatch.setattr(painel, "DELEGACAO_ATENDIMENTO", {})
    d = _board(client, "corretor")
    assert _ids(d) == _ids_de(CORRETOR)
    assert d["corretoras_visiveis"] == [CORRETOR]


def test_delegacao_so_da_pessoa_d_nao_abre_a_corretor(client, monkeypatch):
    """O estado de antes (`{3: {7}}`): a Corretor fica de fora da visão da Pessoa12."""
    monkeypatch.setattr(painel, "DELEGACAO_ATENDIMENTO", {CORRETOR: {PESSOA_D}})
    assert _ids(_board(client, "corretor")) == _ids_de(CORRETOR) | _ids_de(PESSOA_D)


def test_trava_de_exibicao_ainda_manda(client, monkeypatch):
    """Delegação não fura a trava do piloto: fora de CRM_CORRETORAS_ATIVAS o
    lead não aparece nem pra dona, nem pra Pessoa12, nem pro admin."""
    monkeypatch.setattr(settings, "crm_corretoras_ativas", [1, 2, 3])
    assert _ids(_board(client, "corretor")) == set()
    assert _ids(_board(client, "corretor")) == _ids_de(CORRETOR)
    assert _ids(_board(client, "dona")) == _ids_de(CORRETOR) | _ids_de(CORRETOR) | _ids_de(CORRETOR)


# ── 6. A Pessoa12 trabalha o card da delegada ──────────────────────────────────────

def _mover(client, lead_id: int, user: str, etapa: str = "Em atendimento"):
    return client.post(
        "/painel/crm/mover",
        json={"lead_id": lead_id, "stage_destino": etapa},
        headers={"X-Painel-User": user},
    )


def test_corretor_move_card_da_corretor_e_da_pessoa_d(client, factory):
    for lid in (801, 701):
        r = _mover(client, lid, "corretor")
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is True
    with factory() as s:
        assert s.get(Lead, 801).stage_id == 2
        # mover NÃO troca a dona: o card segue no funil da Corretor
        assert s.get(Lead, 801).owner_id == CORRETOR
        ev = s.query(LeadEvent).filter(LeadEvent.lead_id == 801).one()
        assert ev.tipo == "stage_manual_v2"
        assert ev.payload["quem"] == "corretor"


def test_dona_move_o_proprio_card(client):
    assert _mover(client, 801, "corretor").status_code == 200
    assert _mover(client, 701, "pessoa_d").status_code == 200


@pytest.mark.parametrize("user,lead_id", [
    ("corretor", 801), ("corretor", 801), ("pessoa_d", 801),   # ninguém mais move o da Corretor
    ("corretor", 701), ("corretor", 701),                     # nem o da Pessoa7
    ("corretor", 301), ("pessoa_d", 301),                   # mão única: não movem o da Pessoa12
    ("corretor", 101), ("corretor", 201),                        # Pessoa12 não ganhou Pessoa10/Pessoa11
])
def test_mover_continua_barrado_fora_da_delegacao(client, user, lead_id):
    r = _mover(client, lead_id, user)
    assert r.status_code == 403
    assert r.json()["detail"] == "lead_de_outra_corretora"


def test_sem_delegacao_corretor_nao_move_card_da_corretor(client, monkeypatch):
    monkeypatch.setattr(painel, "DELEGACAO_ATENDIMENTO", {})
    assert _mover(client, 801, "corretor").status_code == 403


def _por_ref(client, lead_id: int, user: str):
    return client.get(
        f"/painel/crm/lead-por-ref?external_ref=ref_{lead_id}",
        headers={"X-Painel-User": user},
    )


def test_corretor_resolve_vinculo_do_lead_da_corretor(client):
    """O chat usa isso pra montar o seletor de etapa — sem delegação aqui a Pessoa12
    abriria a conversa da Corretor e ficaria sem como mover o card."""
    r = _por_ref(client, 801, "corretor")
    assert r.status_code == 200, r.text
    assert r.json() == {
        "vinculado": True, "lead_id": 801, "funil": "principal", "etapa": "Novo",
    }
    assert _por_ref(client, 701, "corretor").status_code == 200


@pytest.mark.parametrize("user,lead_id", [
    ("corretor", 801), ("pessoa_d", 801), ("corretor", 701), ("corretor", 301), ("corretor", 101),
])
def test_lead_por_ref_barrado_fora_da_delegacao(client, user, lead_id):
    assert _por_ref(client, lead_id, user).status_code == 403


def _adotar(client, lead_id: int, user: str, etapa: str = "Em atendimento"):
    return client.post(
        "/painel/crm/adotar",
        json={"external_ref": f"ref_{lead_id}", "etapa": etapa},
        headers={"X-Painel-User": user},
    )


def test_corretor_muda_etapa_do_lead_da_corretor_pelo_seletor_do_chat(client, factory):
    r = _adotar(client, 802, "corretor")
    assert r.status_code == 200, r.text
    with factory() as s:
        assert s.get(Lead, 802).stage_id == 2
        assert s.get(Lead, 802).owner_id == CORRETOR


@pytest.mark.parametrize("user,lead_id", [("corretor", 802), ("pessoa_d", 802), ("corretor", 101)])
def test_adotar_barrado_fora_da_delegacao(client, user, lead_id):
    assert _adotar(client, lead_id, user).status_code == 403


def test_gate_padrao_de_escopo_aceita_a_delegada(monkeypatch):
    """`_checar_escopo_lead` é o gate de nota, tarefa, nome e telefone do lead.
    Com igualdade estrita a Pessoa12 veria o card da Corretor e levaria 403 ao anotar."""
    monkeypatch.setattr(painel, "_dono_canonico", lambda ref: (CORRETOR, True, "Fulano"))
    assert painel._checar_escopo_lead("728", CORRETOR, False) == (CORRETOR, "Fulano")
    assert painel._checar_escopo_lead("728", CORRETOR, False) == (CORRETOR, "Fulano")
    assert painel._checar_escopo_lead("728", None, True) == (CORRETOR, "Fulano")
    for alheia in (CORRETOR, CORRETOR, PESSOA_D):
        with pytest.raises(HTTPException) as exc:
            painel._checar_escopo_lead("728", alheia, False)
        assert exc.value.status_code == 403


def test_gate_padrao_nao_abre_lead_da_corretor_pra_delegada(monkeypatch):
    monkeypatch.setattr(painel, "_dono_canonico", lambda ref: (CORRETOR, True, "Lead da Pessoa12"))
    for delegada in (CORRETOR, PESSOA_D):
        with pytest.raises(HTTPException) as exc:
            painel._checar_escopo_lead("1", delegada, False)
        assert exc.value.status_code == 403


def test_gate_padrao_lead_sem_dona_segue_admin_only(monkeypatch):
    monkeypatch.setattr(painel, "_dono_canonico", lambda ref: (None, True, "Sem dona"))
    with pytest.raises(HTTPException) as exc:
        painel._checar_escopo_lead("1", CORRETOR, False)
    assert exc.value.status_code == 403
    assert painel._checar_escopo_lead("1", None, True) == (None, "Sem dona")


def test_gate_padrao_sem_delegacao_barra_a_corretor(monkeypatch):
    monkeypatch.setattr(painel, "_dono_canonico", lambda ref: (CORRETOR, True, "Fulano"))
    monkeypatch.setattr(painel, "DELEGACAO_ATENDIMENTO", {})
    with pytest.raises(HTTPException) as exc:
        painel._checar_escopo_lead("728", CORRETOR, False)
    assert exc.value.status_code == 403


# ── 7. Board de remarketing: mesma regra ─────────────────────────────────────

def _remarketing(client, user: str, qs: str = "") -> dict:
    r = client.get(
        f"/painel/crm/remarketing/board{qs}", headers={"X-Painel-User": user}
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_remarketing_corretor_ve_as_tres(client):
    d = _remarketing(client, "corretor")
    assert _ids(d) == {391, 791, 891}
    for c in _cards(d):
        assert c["corretora_label"] == ROTULOS[c["corretora_id"]]


@pytest.mark.parametrize("user,esperado", [
    ("corretor", {891}), ("pessoa_d", {791}), ("corretor", {191}), ("corretor", set()),
])
def test_remarketing_cada_uma_so_o_dela(client, user, esperado):
    assert _ids(_remarketing(client, user)) == esperado


def test_remarketing_admin_filtra_a_corretor(client):
    assert _ids(_remarketing(client, "dona")) == set(REMARKETING)
    assert _ids(_remarketing(client, "dona", "?corretora=8")) == {891}
