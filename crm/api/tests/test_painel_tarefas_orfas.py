"""GET /painel/tarefas — tarefa criada em lead SEM DONA não pode sumir.

Relato da Dona (12/08/2026): "tem tarefa que as corretoras colocam mas que não
parece se vincular a elas; tinha tarefa da Pessoa3 que só aparecia quando ela
clicava pra ver as tarefas de todas, e desaparecia quando via só as dela".

O que acontece: `corretora_id` da tarefa é herdado do DONO DO LEAD. Quando o
lead não tem dona (base antiga, lead de remarketing), a tarefa nasce com
`corretora_id = NULL` — e a lista, que filtra por corretora, não mostra pra
ninguém. Só aparecia na visão sem filtro. Eram 10 tarefas assim, todas criadas
pela conta `pessoa` (a Pessoa12), as mais novas do dia anterior.

Regra: quem CRIOU vê o que criou. Tarefa sem dona aparece pro autor dela,
qualquer que seja o filtro — sem inventar dona pro lead, que é outro assunto.

Precisa de DB real (TEST_DATABASE_URL): a listagem monta SQL cru com date/time,
que o SQLite devolve como string.
"""
from __future__ import annotations

import os
import uuid
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text as sql_text

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="precisa de TEST_DATABASE_URL (Postgres de teste)",
)

SUF = uuid.uuid4().hex[:8]
HOJE = date.today()


@pytest.fixture()
def cenario():
    """4 tarefas abertas: 2 com dona, 2 ÓRFÃS (uma da pessoa, uma da corretor)."""
    from app.db.session import SessionLocal

    ids: list[int] = []
    with SessionLocal() as s:
        for cid, autor in ((3, "pessoa"), (1, "corretor"), (None, "pessoa"), (None, "corretor")):
            novo = s.execute(
                sql_text(
                    "INSERT INTO tarefas_corretora "
                    "(tenant_id, lead_id, lead_nome, corretora_id, data_tarefa, "
                    " texto, status, autor) "
                    "VALUES (1, :lid, :nome, :cid, :data, :txt, 'aberta', :autor) "
                    "RETURNING id"
                ),
                {
                    "lid": f"orfa-{SUF}-{len(ids)}",
                    "nome": f"Lead {len(ids)}",
                    "cid": cid,
                    "data": HOJE,
                    "txt": f"tarefa {len(ids)} ({autor}, cid={cid})",
                    "autor": autor,
                },
            ).scalar()
            ids.append(int(novo))
        s.commit()

    yield ids  # [com_dona_corretor, com_dona_corretor, orfa_pessoa, orfa_corretor]

    with SessionLocal() as s:
        s.execute(
            sql_text("DELETE FROM tarefas_corretora WHERE id = ANY(:ids)"),
            {"ids": ids},
        )
        s.commit()


@pytest.fixture()
def client():
    from app.main import app
    return TestClient(app)


def _hdr(user: str) -> dict[str, str]:
    return {"X-Painel-User": user}


def _ids(body: dict) -> set[int]:
    return {t["id"] for t in body["tarefas"] if t.get("tipo") == "tarefa"}


def test_corretora_ve_a_orfa_que_ela_criou(client, cenario):
    corretor, corretor, orfa_pessoa, orfa_corretor = cenario
    ids = _ids(client.get("/painel/tarefas", headers=_hdr("corretor")).json())
    assert corretor in ids        # a dela, normal
    assert orfa_corretor in ids   # a órfã que ela criou — antes sumia
    assert orfa_pessoa not in ids  # órfã de outra pessoa continua fora
    assert corretor not in ids


def test_admin_filtrando_por_corretora_ainda_ve_as_proprias_orfas(client, cenario):
    """O caso do relato: a pessoa filtra por Pessoa3 e a tarefa dela sumia."""
    corretor, corretor, orfa_pessoa, _ = cenario
    body = client.get(
        "/painel/tarefas?corretora=Pessoa3", headers=_hdr("pessoa")
    ).json()
    ids = _ids(body)
    assert corretor in ids
    assert orfa_pessoa in ids   # o que sumia
    assert corretor not in ids     # tarefa da Pessoa10 não vaza pro filtro da Pessoa12


def test_orfa_de_outra_pessoa_nao_vaza_no_filtro(client, cenario):
    """Órfã não é curinga: só aparece pra quem criou."""
    corretor, _, orfa_pessoa, orfa_corretor = cenario
    ids = _ids(
        client.get("/painel/tarefas?corretora=Pessoa3", headers=_hdr("dona")).json()
    )
    assert corretor in ids
    assert orfa_pessoa not in ids  # criada pela pessoa, não pela dona
    assert orfa_corretor not in ids


def test_admin_sem_filtro_continua_vendo_tudo(client, cenario):
    ids = _ids(client.get("/painel/tarefas", headers=_hdr("dona")).json())
    assert set(cenario) <= ids
