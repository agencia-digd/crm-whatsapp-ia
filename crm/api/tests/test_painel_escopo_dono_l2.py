"""L2 — dono canônico do lead e escopo entre corretoras (painel.py).

Prova, contra o DB de teste (nunca produção — ver trava no conftest), que:

1. `_dono_canonico` resolve o dono por `leads.owner_id` NORMALIZADO (o campo
   guarda o id interno OU o kommo_user_id), com fallback pro snapshot, e
   devolve a flag de existência SEPARADA do dono.
2. `lead_analyses` NÃO manda mais no escopo: lead cuja última análise aponta
   pra corretora B, mas cujo dono canônico é A, pertence a A — era a causa dos
   leads com card no board de uma e 403 no chat.
3. O vazamento fechou nos DOIS caminhos (leitura do chat e escrita de
   nome/telefone), inclusive pros leads que só existem na espinha.
4. Lead inexistente → 404; lead que existe sem dona → admin-only (nunca 404,
   nunca liberado pra corretora).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.api import painel
from app.db.session import SessionLocal
from app.main import app

TENANT = 1

# Leads sintéticos deste teste (refs fora de qualquer faixa real do Kommo).
REF_DA_CORRETOR = "9900000001"        # snapshot + espinha, owner_id = 1 (id interno)
REF_KOMMO_UID = "9900000002"      # espinha com owner_id = kommo_user_id da Pessoa12
REF_SO_ESPINHA = "9900000003"     # SEM snapshot, owner_id = 3  (caso dos 5.704)
REF_SO_SNAPSHOT = "9900000004"    # SEM espinha, snapshot.corretora_id = 2
REF_ANALISE_DIVERGE = "9900000005"  # dono = 1, lead_analyses aponta 2
REF_SEM_DONA = "9900000006"       # existe, owner NULL e snapshot sem corretora
REF_INEXISTENTE = "9900000999"

TODOS = [
    REF_DA_CORRETOR, REF_KOMMO_UID, REF_SO_ESPINHA, REF_SO_SNAPSHOT,
    REF_ANALISE_DIVERGE, REF_SEM_DONA,
]


def _limpa(s) -> None:
    s.execute(
        text("DELETE FROM lead_analyses WHERE lead_id = ANY(:refs)"),
        {"refs": TODOS},
    )
    s.execute(
        text("DELETE FROM leads_snapshot WHERE lead_id = ANY(:refs)"),
        {"refs": TODOS},
    )
    s.execute(
        text("DELETE FROM leads WHERE external_ref = ANY(:refs) "
             "AND tenant_id = :t"),
        {"refs": TODOS, "t": TENANT},
    )


def _snapshot(s, ref: str, cid: int | None, nome: str, tel: str) -> None:
    s.execute(
        text(
            "INSERT INTO leads_snapshot "
            "(lead_id, nome, telefone, corretora_id, etapa_kommo, ultima_sync) "
            "VALUES (:ref, :n, :tel, :cid, '10000005', NOW())"
        ),
        {"ref": ref, "n": nome, "tel": tel, "cid": cid},
    )


def _espinha(s, ref: str, owner_id: int | None, nome: str) -> None:
    s.execute(
        text(
            "INSERT INTO leads (tenant_id, external_ref, nome, owner_id, "
            "                   origem, created_at, updated_at) "
            "VALUES (:t, :ref, :n, :owner, 'teste', NOW(), NOW())"
        ),
        {"t": TENANT, "ref": ref, "n": nome, "owner": owner_id},
    )


@pytest.fixture()
def cenario():
    """Semeia os leads do cenário e limpa no fim (idempotente).

    A limpeza roda em try/finally: teste que falha no meio NÃO pode deixar
    linha órfã pra trás — `leads_snapshot.corretora_id` tem FK pra
    `corretoras`, e o seed de sessão do conftest faz `DELETE FROM corretoras`,
    então resíduo aqui quebra a suíte INTEIRA (de todo mundo) no próximo run.
    """
    with SessionLocal() as s:
        _limpa(s)

        # dona = Pessoa10 (1) nas duas metades
        _snapshot(s, REF_DA_CORRETOR, 1, "Lead da Pessoa10", "+5500000000000")
        _espinha(s, REF_DA_CORRETOR, 1, "Lead da Pessoa10")

        # owner_id gravado como kommo_user_id da Pessoa12 (10000003) → normaliza p/ 3
        _snapshot(s, REF_KOMMO_UID, None, "Lead Kommo UID", "+5500000000000")
        _espinha(s, REF_KOMMO_UID, painel.CORRETORA_KOMMO_USER[3], "Lead Kommo UID")

        # só espinha (sem snapshot): dona = Pessoa12 (3)
        _espinha(s, REF_SO_ESPINHA, 3, "Lead só espinha")

        # só snapshot (sem espinha): dona = Pessoa11 (2) — fallback
        _snapshot(s, REF_SO_SNAPSHOT, 2, "Lead só snapshot", "+5500000000000")

        # dono canônico = Pessoa10 (1), mas a última análise diz Pessoa11 (2)
        _snapshot(s, REF_ANALISE_DIVERGE, 1, "Lead divergente", "+5500000000000")
        _espinha(s, REF_ANALISE_DIVERGE, 1, "Lead divergente")
        for cid, horas in ((1, 48), (2, 1)):
            s.execute(
                text(
                    "INSERT INTO lead_analyses "
                    "(lead_id, corretora_id, analyzed_at, status, temperatura, "
                    " raw_analysis, consumed_by_dona, consumed_by_pessoa, "
                    " consumed_by_corretora) "
                    "VALUES (:ref, :cid, NOW() - make_interval(hours => :horas), "
                    "        'atendido', 'morno', CAST('{}' AS jsonb), "
                    "        false, false, false)"
                ),
                {"ref": REF_ANALISE_DIVERGE, "cid": cid, "horas": horas},
            )

        # existe, mas sem dona nenhuma
        _snapshot(s, REF_SEM_DONA, None, "Lead sem dona", "+5500000000000")
        _espinha(s, REF_SEM_DONA, None, "Lead sem dona")

        s.commit()

    try:
        yield
    finally:
        with SessionLocal() as s:
            _limpa(s)
            s.commit()


@pytest.fixture()
def client():
    return TestClient(app)


def _h(user: str) -> dict[str, str]:
    return {"X-Painel-User": user}


# ── 1. Helper de dono canônico ───────────────────────────────────────────────

def test_dono_por_owner_id_interno(cenario):
    assert painel._dono_canonico(REF_DA_CORRETOR) == (1, True, "Lead da Pessoa10")


def test_dono_normaliza_kommo_user_id(cenario):
    """owner_id gravado como kommo_user_id vira o corretora_id interno."""
    dona, existe, _ = painel._dono_canonico(REF_KOMMO_UID)
    assert (dona, existe) == (3, True)


def test_dono_de_lead_so_na_espinha(cenario):
    """Sem linha no snapshot o dono ainda resolve (os 5.704 leads da espinha)."""
    dona, existe, _ = painel._dono_canonico(REF_SO_ESPINHA)
    assert (dona, existe) == (3, True)


def test_dono_cai_no_snapshot_quando_espinha_nao_tem(cenario):
    """Fallback explícito: sem linha em `leads`, vale o snapshot."""
    dona, existe, _ = painel._dono_canonico(REF_SO_SNAPSHOT)
    assert (dona, existe) == (2, True)


def test_lead_analyses_nao_manda_no_dono(cenario):
    """A última análise aponta pra Pessoa11(2); o dono canônico continua Pessoa10(1).

    Era exatamente esse o bug dos leads inatendíveis: `_save_analysis` insere
    linha nova a cada rodada com o responsible do Kommo e vencia o transferir.
    """
    dona, existe, _ = painel._dono_canonico(REF_ANALISE_DIVERGE)
    assert (dona, existe) == (1, True)


def test_existencia_separada_do_dono(cenario):
    """Lead sem dona ≠ lead inexistente."""
    assert painel._dono_canonico(REF_SEM_DONA) == (None, True, "Lead sem dona")
    assert painel._dono_canonico(REF_INEXISTENTE) == (None, False, None)


# ── 2. Gate compartilhado ────────────────────────────────────────────────────

def test_gate_404_para_inexistente(cenario):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as ei:
        painel._checar_escopo_lead(REF_INEXISTENTE, my_cid=1, is_admin=False)
    assert ei.value.status_code == 404


def test_gate_403_para_lead_de_outra(cenario):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as ei:
        painel._checar_escopo_lead(REF_DA_CORRETOR, my_cid=2, is_admin=False)
    assert ei.value.status_code == 403


def test_gate_lead_sem_dona_e_admin_only(cenario):
    """Não vira 404 (existe) e não fica liberado pra corretora."""
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as ei:
        painel._checar_escopo_lead(REF_SEM_DONA, my_cid=1, is_admin=False)
    assert ei.value.status_code == 403
    # admin passa
    assert painel._checar_escopo_lead(
        REF_SEM_DONA, my_cid=None, is_admin=True
    )[0] is None


# ── 3. Vazamento fechado nos DOIS caminhos ───────────────────────────────────

@pytest.mark.parametrize("ref", [REF_DA_CORRETOR, REF_ANALISE_DIVERGE])
def test_escrita_de_nome_bloqueada_para_outra_corretora(cenario, client, ref):
    r = client.post(f"/painel/lead/{ref}/nome",
                    json={"nome": "Invasao"}, headers=_h("corretor"))
    assert r.status_code == 403
    assert r.json()["detail"] == "lead_nao_pertence_corretora"


@pytest.mark.parametrize("ref", [REF_DA_CORRETOR, REF_ANALISE_DIVERGE])
def test_escrita_de_telefone_bloqueada_para_outra_corretora(cenario, client, ref):
    r = client.post(f"/painel/lead/{ref}/contato",
                    json={"telefone": "+5500000000000"}, headers=_h("corretor"))
    assert r.status_code == 403


def test_telefone_de_lead_so_na_espinha_nao_e_reescrito_por_outra(cenario, client):
    """O furo mais grave: `dona is None` no snapshot liberava a escrita."""
    r = client.post(f"/painel/lead/{REF_SO_ESPINHA}/contato",
                    json={"telefone": "+5500000000000"}, headers=_h("corretor"))
    assert r.status_code == 403

    with SessionLocal() as s:
        tel = s.execute(
            text("SELECT telefone FROM leads WHERE external_ref = :r "
                 "AND tenant_id = :t"),
            {"r": REF_SO_ESPINHA, "t": TENANT},
        ).scalar()
    assert tel != "+5500000000000", "telefone foi reescrito por corretora alheia"


def test_dona_legitima_escreve(cenario, client):
    r = client.post(f"/painel/lead/{REF_SO_ESPINHA}/nome",
                    json={"nome": "Nome Corrigido"}, headers=_h("corretor"))
    assert r.status_code == 200
    assert r.json()["nome"] == "Nome Corrigido"


def test_lead_inexistente_devolve_404_na_escrita(cenario, client):
    r = client.post(f"/painel/lead/{REF_INEXISTENTE}/nome",
                    json={"nome": "Fantasma"}, headers=_h("dona"))
    assert r.status_code == 404


def test_notas_respeitam_o_mesmo_escopo(cenario, client):
    assert client.get(f"/painel/lead/{REF_DA_CORRETOR}/notas",
                      headers=_h("corretor")).status_code == 403
    assert client.get(f"/painel/lead/{REF_DA_CORRETOR}/notas",
                      headers=_h("corretor")).status_code == 200


# ── 4. Rota duplicada ────────────────────────────────────────────────────────

def test_reengajar_declarada_uma_unica_vez():
    """A duplicata (body `gancho`) não checava opt-out e vencia o roteamento."""
    rotas = [
        r for r in app.routes
        if getattr(r, "path", None) == "/painel/conversa/reengajar"
        and "POST" in getattr(r, "methods", set())
    ]
    assert len(rotas) == 1, f"{len(rotas)} declarações de /conversa/reengajar"
    assert rotas[0].endpoint.__name__ == "painel_conversa_reengajar_template"


# ── 5. Criar tarefa: o gate que ficou pra trás ───────────────────────────────
# A rota POST /painel/lead/{id}/tarefa era a ÚLTIMA que ainda decidia escopo por
# `lead_analyses`. Caso real (30/07): a lead Bianca Belline passou pra Pessoa10, mas
# a última análise (13/07) ainda dizia Pessoa11 — a Pessoa10 via o card como dela,
# conseguia CONCLUIR tarefa, e tomava 403 só ao CRIAR. Nove leads assim.

def _criar_tarefa(client, ref: str, user: str):
    return client.post(
        f"/painel/lead/{ref}/tarefa",
        json={"texto": "Follow-up", "data": "2026-12-01"},
        headers=_h(user),
    )


def test_criar_tarefa_ignora_lead_analyses_divergente(cenario, client):
    """Dono canônico = Pessoa10; última análise diz Pessoa11. Quem cria é a CORRETOR."""
    r = _criar_tarefa(client, REF_ANALISE_DIVERGE, "corretor")
    assert r.status_code == 200, r.text

    with SessionLocal() as s:
        cid = s.execute(
            text("SELECT corretora_id FROM tarefas_corretora WHERE lead_id = :ref"),
            {"ref": REF_ANALISE_DIVERGE},
        ).scalar()
        s.execute(
            text("DELETE FROM tarefas_corretora WHERE lead_id = :ref"),
            {"ref": REF_ANALISE_DIVERGE},
        )
        s.commit()
    assert cid == 1, "tarefa saiu na corretora errada"


def test_criar_tarefa_de_lead_de_outra_corretora_barra(cenario, client):
    """A trava continua: a Pessoa11 NÃO cria tarefa em lead da Pessoa10."""
    assert _criar_tarefa(client, REF_DA_CORRETOR, "corretor").status_code == 403


def test_criar_tarefa_em_lead_inexistente_da_404(cenario, client):
    assert _criar_tarefa(client, REF_INEXISTENTE, "corretor").status_code == 404
