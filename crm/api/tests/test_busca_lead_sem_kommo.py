"""Lead sem vínculo Kommo tem que aparecer na busca e abrir no chat.

Caso Edison (21/09/2026): lead criado à mão no painel pra Pessoa12, visível no CRM,
mas o campo de busca jurava que não existia e o clique no card só dizia "esse
lead ainda não tem conversa no Chat".

Causa: `_fonte_busca_sql` filtrava `external_ref IS NOT NULL`, então lead sem
id do Kommo ficava fora do universo da busca. O chat já sabia abrir pelo id da
espinha (`_contexto_lead_nativo`) — faltava a busca devolver esse id.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db.session import SessionLocal
from app.main import app

CORRETORA_A = 3
NOME = "Edison Teste Busca"
TEL = "+5500000000000"


@pytest.fixture()
def lead_sem_kommo():
    with SessionLocal() as s:
        s.execute(text("DELETE FROM leads WHERE nome = :n AND tenant_id = 1"), {"n": NOME})
        lid = s.execute(
            text(
                "INSERT INTO leads (tenant_id, nome, telefone, owner_id, stage_id, "
                "                   origem, created_at, updated_at) "
                "VALUES (1, :n, :t, :o, 6, 'manual', now(), now()) RETURNING id"
            ),
            {"n": NOME, "t": TEL, "o": CORRETORA_A},
        ).scalar_one()
        s.commit()
    try:
        yield int(lid)
    finally:
        with SessionLocal() as s:
            s.execute(text("DELETE FROM leads WHERE nome = :n AND tenant_id = 1"), {"n": NOME})
            s.commit()


def _buscar(q: str) -> dict:
    r = TestClient(app).get(
        f"/painel/atendimento/busca?q={q}", headers={"X-Painel-User": "corretor"}
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_busca_por_nome_acha_lead_sem_kommo(lead_sem_kommo):
    achados = [x for x in _buscar("Edison Teste")["leads"] if x["nome"] == NOME]
    assert achados, "lead sem external_ref sumiu da busca"


def test_busca_por_telefone_acha_lead_sem_kommo(lead_sem_kommo):
    achados = [x for x in _buscar("99190000")["leads"] if x["nome"] == NOME]
    assert achados, "lead sem external_ref sumiu da busca por telefone"


def test_id_devolvido_abre_a_conversa(lead_sem_kommo):
    """O id da busca tem que servir pro chat — senão acha e não abre."""
    hit = next(x for x in _buscar("Edison Teste")["leads"] if x["nome"] == NOME)
    assert str(hit["lead_id"]) == str(lead_sem_kommo)

    r = TestClient(app).get(
        f"/painel/conversa?lead_id={hit['lead_id']}&hours_back=720",
        headers={"X-Painel-User": "corretor"},
    )
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["lead_nome"] == NOME
    assert d["mensagens"] == []  # sem conversa ainda, mas ABRE


def test_lead_com_kommo_continua_vindo_pelo_id_do_kommo():
    """Não pode trocar o id de quem TEM Kommo — quebraria todo o resto."""
    with SessionLocal() as s:
        row = s.execute(
            text("SELECT external_ref FROM leads WHERE external_ref IS NOT NULL "
                 "AND tenant_id = 1 LIMIT 1")
        ).first()
    if row is None:
        pytest.skip("base de teste sem lead com external_ref")
    from app.api.painel import _fonte_busca_sql
    assert "coalesce(l.external_ref, l.id::text)" in _fonte_busca_sql()


# ── a conversa dele no chat mostra o NOME, não o telefone solto ─────────────

def test_conversa_de_lead_sem_kommo_mostra_o_nome(lead_sem_kommo):
    """Sem isto, a conversa aparece como telefone solto em "Fora do funil" —
    parece um segundo contato, e a corretora acha que duplicou (caso Edison)."""
    from sqlalchemy import text as _t

    from app.api.painel import _sql_lead_por_p8

    with SessionLocal() as s:
        linhas = s.execute(_t(_sql_lead_por_p8())).all()
    por_p8 = {r[0]: (str(r[1]), r[2]) for r in linhas}
    assert TEL[-8:] in por_p8, "telefone do lead sem Kommo não mapeou pra lead nenhum"
    lead_id, nome = por_p8[TEL[-8:]]
    assert nome == NOME
    assert lead_id == str(lead_sem_kommo)


# ── o gate de escopo enxerga o lead pelo id da espinha ──────────────────────

def test_dono_canonico_acha_lead_sem_kommo(lead_sem_kommo):
    """Sem isto, TUDO que passa pelo gate responde 404 "lead_nao_encontrado":
    criar tarefa, nota, mudar etapa (caso Edison, 21/09)."""
    from app.api.painel import _dono_canonico

    dona, existe, nome = _dono_canonico(str(lead_sem_kommo))
    assert existe is True, "lead da espinha visto como inexistente"
    assert dona == CORRETORA_A
    assert nome == NOME


def test_dono_canonico_de_lead_com_kommo_nao_muda(lead_sem_kommo):
    """O braço por id da espinha não pode capturar lead que TEM Kommo."""
    from sqlalchemy import text as _t

    from app.api.painel import _dono_canonico

    with SessionLocal() as s:
        row = s.execute(
            _t("SELECT external_ref, owner_id FROM leads WHERE external_ref IS NOT NULL "
               "AND owner_id IS NOT NULL AND tenant_id = 1 LIMIT 1")
        ).mappings().first()
    if row is None:
        pytest.skip("base de teste sem lead com external_ref")
    _dona, existe, _n = _dono_canonico(str(row["external_ref"]))
    assert existe is True


def test_criar_tarefa_em_lead_sem_kommo(lead_sem_kommo):
    """O caminho de ponta: a corretora consegue agendar a tarefa."""
    from sqlalchemy import text as _t

    r = TestClient(app).post(
        f"/painel/lead/{lead_sem_kommo}/tarefa",
        json={"data": "2026-12-01", "texto": "Ligar pro lead"},
        headers={"X-Painel-User": "corretor"},
    )
    assert r.status_code == 200, r.text
    tid = r.json()["tarefa_id"]
    with SessionLocal() as s:
        row = s.execute(
            _t("SELECT lead_id, corretora_id FROM tarefas_corretora WHERE id = :i"),
            {"i": tid},
        ).mappings().first()
        s.execute(_t("DELETE FROM tarefas_corretora WHERE id = :i"), {"i": tid})
        s.commit()
    assert row["lead_id"] == str(lead_sem_kommo)
    assert row["corretora_id"] == CORRETORA_A


def test_lead_inexistente_continua_404():
    """A rede de segurança não pode virar 'aceita qualquer id'."""
    r = TestClient(app).post(
        "/painel/lead/999999999/tarefa",
        json={"data": "2026-12-01", "texto": "x"},
        headers={"X-Painel-User": "corretor"},
    )
    assert r.status_code == 404
