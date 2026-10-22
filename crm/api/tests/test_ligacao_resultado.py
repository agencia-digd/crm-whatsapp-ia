"""Resultado de LIGAÇÃO — registro, adoção do lead, tag e métrica.

Cobre o que a Dona pediu e as travas que já custaram tempo:
  - registrar grava NOTA na conversa (observacao_corretora) E dado estruturado;
  - os três estados viram (atendeu, quis_continuar) certos — "não atendeu"
    nunca inventa quis_continuar=False;
  - tarefa concluída SEM resultado NÃO vira "não atendeu" (conta sem_registro);
  - taxa é None quando não há amostra (nunca 0%) e amostra pequena vem marcada;
  - métrica é admin-only;
  - adoção: move pra "Novo", dá dono, marca a tag — sem rebaixar etapa mais
    avançada e sem tirar o dono de quem já tem;
  - a tag faz a boas-vindas mandar a copy de CONTINUIDADE, fora do A/B.

Precisa de DB real (TEST_DATABASE_URL — ver conftest.py).
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text as sql_text

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="precisa de TEST_DATABASE_URL (Postgres de teste)",
)

from app.services import ligacao_resultado as lr  # noqa: E402


@pytest.fixture
def client():
    from app.main import app

    return TestClient(app)


def _hdr(user: str) -> dict[str, str]:
    return {"X-Painel-User": user}


@pytest.fixture
def lead_da_corretor():
    """Lead no snapshot pertencendo à Pessoa5 (corretora_id=1)."""
    from app.db.session import SessionLocal

    lid = f"t-lig-{uuid.uuid4().hex[:10]}"
    with SessionLocal() as s:
        s.execute(
            sql_text(
                "INSERT INTO leads_snapshot (lead_id, nome, telefone, corretora_id) "
                "VALUES (:lid, 'Lead Teste Ligacao', '+5500000000000', 1)"
            ),
            {"lid": lid},
        )
        s.commit()
    yield lid
    with SessionLocal() as s:
        s.execute(sql_text("DELETE FROM ligacao_resultado WHERE lead_id = :l"), {"l": lid})
        s.execute(sql_text("DELETE FROM observacao_corretora WHERE lead_id = :l"), {"l": lid})
        s.execute(sql_text("DELETE FROM tarefas_corretora WHERE lead_id = :l"), {"l": lid})
        s.execute(sql_text("DELETE FROM leads_snapshot WHERE lead_id = :l"), {"l": lid})
        s.commit()


def _criar_tarefa(lead_id: str, texto: str = "Ligar pro cliente") -> int:
    from app.db.session import SessionLocal

    with SessionLocal() as s:
        tid = s.execute(
            sql_text(
                "INSERT INTO tarefas_corretora "
                "(lead_id, lead_nome, corretora_id, data_tarefa, texto, status, autor) "
                "VALUES (:l, 'Lead Teste Ligacao', 1, CURRENT_DATE, :t, 'aberta', 'corretor') "
                "RETURNING id"
            ),
            {"l": lead_id, "t": texto},
        ).scalar_one()
        s.commit()
    return int(tid)


# ── os três estados ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "estado, atendeu, quis",
    [
        (lr.RESULTADO_NAO_ATENDEU, False, None),
        (lr.RESULTADO_SEM_CONTINUAR, True, False),
        (lr.RESULTADO_QUER_CONTINUAR, True, True),
    ],
)
def test_estado_vira_os_dois_campos(estado, atendeu, quis):
    res = lr.ResultadoLigacao.do_estado(estado)
    assert res.atendeu is atendeu
    assert res.quis_continuar is quis
    assert res.estado == estado


def test_nao_atendeu_nunca_inventa_quis_continuar():
    """Recebendo quis_continuar junto de 'não atendeu', o campo é ZERADO pra
    None — False ali entraria como amostra na taxa de continuidade."""
    res = lr.ResultadoLigacao(atendeu=False, quis_continuar=False).normalizado()
    assert res.quis_continuar is None


def test_estado_desconhecido_e_recusado():
    with pytest.raises(ValueError):
        lr.ResultadoLigacao.do_estado("mais_ou_menos")


def test_texto_da_nota_por_estado():
    def nota(e: str) -> str:
        return lr.texto_da_nota(lr.ResultadoLigacao.do_estado(e))

    # TODA tentativa vira nota — inclusive quando não atende
    assert nota(lr.RESULTADO_NAO_ATENDEU) == "📞 Tentativa de ligação — não atendeu"
    assert nota(lr.RESULTADO_SEM_CONTINUAR) == "📞 Ligação — atendeu, não quis seguir agora"
    assert nota(lr.RESULTADO_QUER_CONTINUAR) == "📞 Ligação — atendeu e quer continuar"
    com_obs = lr.texto_da_nota(
        lr.ResultadoLigacao.do_estado(lr.RESULTADO_QUER_CONTINUAR, "pediu 3 quartos")
    )
    assert com_obs.endswith("· pediu 3 quartos")


def test_nota_numera_da_segunda_tentativa_em_diante():
    res = lr.ResultadoLigacao.do_estado(lr.RESULTADO_NAO_ATENDEU)
    assert "tentativa)" not in lr.texto_da_nota(res, tentativa=1)
    assert lr.texto_da_nota(res, tentativa=3).endswith("(3ª tentativa)")


# ── concluir tarefa: nota + dado estruturado ─────────────────────────────────


def test_concluir_com_resultado_grava_nota_e_dado(client, lead_da_corretor):
    from app.db.session import SessionLocal

    tid = _criar_tarefa(lead_da_corretor)
    r = client.post(
        f"/painel/tarefa/{tid}/concluir",
        json={"resultado": lr.RESULTADO_QUER_CONTINUAR, "observacao": "vai visitar"},
        headers=_hdr("corretor"),
    )
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True

    # 1) nota na conversa do lead — é o que a thread e as oportunidades leem
    notas = client.get(f"/painel/lead/{lead_da_corretor}/notas", headers=_hdr("corretor")).json()
    assert notas["total"] == 1
    assert notas["notas"][0]["texto"] == "📞 Ligação — atendeu e quer continuar · vai visitar"

    # 2) dado estruturado
    with SessionLocal() as s:
        linha = s.execute(
            sql_text(
                "SELECT origem, origem_id, atendeu, quis_continuar, observacao, autor "
                "FROM ligacao_resultado WHERE lead_id = :l"
            ),
            {"l": lead_da_corretor},
        ).mappings().all()
    assert len(linha) == 1
    assert linha[0]["origem"] == lr.ORIGEM_TAREFA
    assert linha[0]["origem_id"] == tid
    assert linha[0]["atendeu"] is True
    assert linha[0]["quis_continuar"] is True
    assert linha[0]["observacao"] == "vai visitar"
    assert linha[0]["autor"] == "corretor"


def test_concluir_sem_resultado_nao_grava_nada(client, lead_da_corretor):
    """Concluir sem dizer o que aconteceu não pode virar 'não atendeu' — nem
    linha, nem nota. Vira `sem_registro` na métrica."""
    from app.db.session import SessionLocal

    tid = _criar_tarefa(lead_da_corretor)
    r = client.post(f"/painel/tarefa/{tid}/concluir", headers=_hdr("corretor"))
    assert r.status_code == 200, r.text
    assert r.json()["registro_ligacao"] is None

    with SessionLocal() as s:
        n = s.execute(
            sql_text("SELECT COUNT(*) FROM ligacao_resultado WHERE origem_id = :t"),
            {"t": tid},
        ).scalar()
    assert n == 0
    notas = client.get(f"/painel/lead/{lead_da_corretor}/notas", headers=_hdr("corretor")).json()
    assert notas["total"] == 0


def test_resultado_invalido_e_422(client, lead_da_corretor):
    tid = _criar_tarefa(lead_da_corretor)
    r = client.post(
        f"/painel/tarefa/{tid}/concluir",
        json={"resultado": "atendeu_mais_ou_menos"},
        headers=_hdr("corretor"),
    )
    assert r.status_code == 422


def test_registro_e_idempotente(client, lead_da_corretor):
    """Registrar 2x a mesma ligação não duplica linha nem nota."""
    from app.db.session import SessionLocal

    tid = _criar_tarefa(lead_da_corretor)
    for _ in range(2):
        client.post(
            f"/painel/tarefa/{tid}/concluir",
            json={"resultado": lr.RESULTADO_NAO_ATENDEU},
            headers=_hdr("corretor"),
        )
    with SessionLocal() as s:
        linhas = s.execute(
            sql_text("SELECT COUNT(*) FROM ligacao_resultado WHERE origem_id = :t"),
            {"t": tid},
        ).scalar()
        notas = s.execute(
            sql_text("SELECT COUNT(*) FROM observacao_corretora WHERE lead_id = :l"),
            {"l": lead_da_corretor},
        ).scalar()
    assert linhas == 1
    assert notas == 1


def test_ultima_ligacao_aparece_no_raiox_do_lead(lead_da_corretor):
    """O que faz a Assistente/Chat enxergar que já ligaram."""
    from app.db.session import SessionLocal

    with SessionLocal() as s:
        lr.registrar_resultado(
            s,
            origem=lr.ORIGEM_TAREFA,
            origem_id=987654,
            resultado=lr.ResultadoLigacao.do_estado(lr.RESULTADO_NAO_ATENDEU),
            lead_id=lead_da_corretor,
            corretora_id=1,
            autor="corretor",
        )
        s.commit()
        achado = lr.ultima_ligacao_do_lead(s, lead_id=lead_da_corretor)
        s.execute(sql_text("DELETE FROM ligacao_resultado WHERE origem_id = 987654"))
        s.commit()
    assert achado is not None
    assert achado["atendeu"] is False
    assert achado["resumo"] == "📞 Tentativa de ligação — não atendeu"


# ── métrica ──────────────────────────────────────────────────────────────────


def _sessao_vazia():
    from app.db.session import SessionLocal

    return SessionLocal()


def test_taxa_e_none_sem_amostra_nunca_zero():
    assert lr._taxa(0, 0) is None
    assert lr._taxa(0, 10) == 0.0  # com amostra, zero é zero mesmo


def test_metrica_sem_dado_nao_devolve_zero_por_cento():
    with _sessao_vazia() as s:
        # janela de 0 dia: nada dentro dela em nenhum ambiente
        dados = lr.metricas_ligacao(s, dias=1, agora=datetime(2000, 1, 1, tzinfo=timezone.utc))
    total = dados["total"]
    assert total["ligacoes"] == 0
    assert total["taxa_atendimento"] is None
    assert total["taxa_continuidade"] is None
    assert total["sem_dado"] is True
    assert total["amostra_pequena"] is False  # 0 não é "amostra pequena", é sem dado


def test_metrica_conta_sem_registro_separado(lead_da_corretor):
    """Duas tarefas de ligação concluídas, uma só com resultado: a outra é
    sem_registro e NÃO entra como 'não atendeu'."""
    from app.db.session import SessionLocal

    t1 = _criar_tarefa(lead_da_corretor, "Ligar pro cliente")
    t2 = _criar_tarefa(lead_da_corretor, "ligação de retorno")
    agora = datetime.now(timezone.utc)
    with SessionLocal() as s:
        s.execute(
            sql_text(
                "UPDATE tarefas_corretora SET status='concluida', concluido_em=:a "
                "WHERE id IN (:a1, :a2)"
            ),
            {"a": agora, "a1": t1, "a2": t2},
        )
        lr.registrar_resultado(
            s,
            origem=lr.ORIGEM_TAREFA,
            origem_id=t1,
            resultado=lr.ResultadoLigacao.do_estado(lr.RESULTADO_SEM_CONTINUAR),
            lead_id=lead_da_corretor,
            corretora_id=1,
            autor="corretor",
        )
        s.commit()
        dados = lr.metricas_ligacao(s, dias=1, corretora_label={1: "Pessoa10"})

    corretor = next(l for l in dados["por_corretora"] if l["corretora_id"] == 1)
    assert corretor["ligacoes"] >= 1
    assert corretor["sem_registro"] >= 1
    assert corretor["atenderam"] >= 1
    # o "sem registro" não engordou o "não atendeu"
    assert corretor["nao_atenderam"] + corretor["atenderam"] == corretor["ligacoes"]
    assert corretor["taxa_atendimento"] is not None
    assert corretor["amostra_pequena"] is True  # bem abaixo de 20


def test_texto_de_ligacao_reconhece_variacoes():
    assert lr.texto_e_de_ligacao("Ligar pro cliente")
    assert lr.texto_e_de_ligacao("ligação de retorno")
    assert lr.texto_e_de_ligacao("Ligou e não atendeu")
    assert not lr.texto_e_de_ligacao("Follow-up")
    assert not lr.texto_e_de_ligacao("Ver se respondeu")


def test_metricas_ligacao_e_admin_only(client):
    r = client.get("/painel/tarefas/metricas-ligacao?dias=30", headers=_hdr("corretor"))
    assert r.status_code == 403
    r2 = client.get("/painel/tarefas/metricas-ligacao?dias=30", headers=_hdr("dona"))
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["amostra_minima"] == 20
    assert body["referencia_historica"]["resumo"] == "100 → 30 → 12"
    assert "sem_registro" in body["definicao"]


# ── adoção do lead ───────────────────────────────────────────────────────────


@pytest.fixture
def lead_remarketing():
    """Lead na espinha, no funil de remarketing e SEM DONO."""
    from app.db.models import Lead, PipelineStage
    from app.db.session import SessionLocal

    ref = f"t-rmk-{uuid.uuid4().hex[:10]}"
    with SessionLocal() as s:
        etapa = (
            s.query(PipelineStage)
            .filter(PipelineStage.tenant_id == 1, PipelineStage.funil == "remarketing")
            .order_by(PipelineStage.ordem.asc().nullslast(), PipelineStage.id.asc())
            .first()
        )
        if etapa is None:
            etapa = PipelineStage(tenant_id=1, nome="Base", ordem=1, funil="remarketing")
            s.add(etapa)
            s.flush()
        lead = Lead(
            tenant_id=1, external_ref=ref, nome="Lead Remarketing",
            telefone="+5500000000000", owner_id=None, stage_id=etapa.id,
            created_at=datetime.now(timezone.utc) - timedelta(days=90),
            updated_at=datetime.now(timezone.utc) - timedelta(days=90),
        )
        s.add(lead)
        s.commit()
        lead_id = lead.id
        stage_remarketing = etapa.id
    yield lead_id, stage_remarketing, ref
    with SessionLocal() as s:
        s.execute(sql_text("DELETE FROM lead_events WHERE lead_id = :l"), {"l": lead_id})
        s.execute(
            sql_text("DELETE FROM ligacao_resultado WHERE origem = :o AND origem_id = :l"),
            {"o": lr.ORIGEM_REMARKETING, "l": lead_id},
        )
        s.execute(sql_text("DELETE FROM observacao_corretora WHERE lead_id = :r"), {"r": ref})
        s.execute(sql_text("DELETE FROM ligacao_reserva WHERE lead_id = :l"), {"l": lead_id})
        s.execute(sql_text("DELETE FROM leads WHERE id = :l"), {"l": lead_id})
        s.commit()


def test_quer_continuar_adota_lead(client, lead_remarketing):
    from app.db.models import Lead, PipelineStage
    from app.db.session import SessionLocal

    lead_id, stage_rmk, ref = lead_remarketing
    r = client.post(
        "/painel/ligacoes/registrar",
        json={"lead_id": lead_id, "resultado": lr.RESULTADO_QUER_CONTINUAR,
              "observacao": "quer ver apto 2 quartos"},
        headers=_hdr("corretor"),
    )
    assert r.status_code == 200, r.text
    adocao = r.json()["adocao"]
    assert adocao["moveu_etapa"] is True
    assert adocao["definiu_dono"] is True
    assert adocao["ja_tem_dono"] is False

    with SessionLocal() as s:
        lead = s.get(Lead, lead_id)
        etapa = s.get(PipelineStage, lead.stage_id)
        assert lead.owner_id == 1
        assert lead.owner_editado_manual is True
        assert etapa.nome == "Novo"
        assert etapa.funil == "principal"
        # TAG — é o que a boas-vindas consulta
        assert lead.adotado_ligacao_em is not None
        assert lr.veio_de_ligacao(s, lead_id) is True
        # auditoria
        tipos = [
            t[0] for t in s.execute(
                sql_text("SELECT tipo FROM lead_events WHERE lead_id = :l"),
                {"l": lead_id},
            ).all()
        ]
        assert lr.EVENTO_ADOCAO in tipos
        assert "stage_manual_v2" in tipos  # segura o espelho do Kommo por 72h


def test_nao_atendeu_nao_adota(client, lead_remarketing):
    from app.db.models import Lead
    from app.db.session import SessionLocal

    lead_id, stage_rmk, _ = lead_remarketing
    r = client.post(
        "/painel/ligacoes/registrar",
        json={"lead_id": lead_id, "resultado": lr.RESULTADO_NAO_ATENDEU},
        headers=_hdr("corretor"),
    )
    assert r.status_code == 200, r.text
    assert r.json()["adocao"] is None
    with SessionLocal() as s:
        lead = s.get(Lead, lead_id)
        assert lead.owner_id is None
        assert lead.stage_id == stage_rmk
        assert lead.adotado_ligacao_em is None


def test_adocao_nao_rebaixa_etapa_mais_avancada(lead_remarketing):
    from app.db.models import Lead
    from app.db.session import SessionLocal
    from app.services.lead_spine import _resolve_stage_id

    lead_id, _, _ = lead_remarketing
    with SessionLocal() as s:
        avancada = _resolve_stage_id(s, 1, None, "Visita marcada")
        lead = s.get(Lead, lead_id)
        lead.stage_id = avancada
        s.commit()

        out = lr.adotar_lead(s, lead_id=lead_id, corretora_id=1, autor="corretor")
        s.commit()
        lead = s.get(Lead, lead_id)
        assert out["moveu_etapa"] is False
        assert out["motivo_etapa"] == "etapa_mais_avancada"
        assert lead.stage_id == avancada  # ficou onde estava
        assert lead.owner_id == 1  # o dono continua sendo definido


def test_adocao_nao_tira_dono_de_quem_ja_tem(lead_remarketing):
    from app.db.models import Lead
    from app.db.session import SessionLocal

    lead_id, _, _ = lead_remarketing
    with SessionLocal() as s:
        lead = s.get(Lead, lead_id)
        lead.owner_id = 2  # Pessoa11 já é dona
        s.commit()

        out = lr.adotar_lead(s, lead_id=lead_id, corretora_id=1, autor="corretor")
        s.commit()
        lead = s.get(Lead, lead_id)
        assert out["ja_tem_dono"] is True
        assert out["definiu_dono"] is False
        assert lead.owner_id == 2  # intocado


def test_fila_so_traz_remarketing_sem_dono(client, lead_remarketing):
    lead_id, _, _ = lead_remarketing
    r = client.get("/painel/ligacoes/fila?limite=200", headers=_hdr("corretor"))
    assert r.status_code == 200, r.text
    body = r.json()
    item = next((i for i in body["itens"] if i["lead_id"] == lead_id), None)
    assert item is not None
    assert item["tentativas"] == 0
    assert item["tentativa_atual"] == 1
    assert body["referencia_historica"]["ligacoes"] == 1095

    # quem ATENDEU sai da fila (aqui: atendeu e não quis seguir agora)
    client.post(
        "/painel/ligacoes/registrar",
        json={"lead_id": lead_id, "resultado": lr.RESULTADO_SEM_CONTINUAR},
        headers=_hdr("corretor"),
    )
    r2 = client.get("/painel/ligacoes/fila?limite=200", headers=_hdr("corretor"))
    assert lead_id not in {i["lead_id"] for i in r2.json()["itens"]}


# ── contador de tentativas ───────────────────────────────────────────────────


def test_nao_atendeu_pode_ser_registrado_de_novo_e_conta_tentativa(
    client, lead_remarketing
):
    """Não atender NÃO tranca o lead: a corretora liga de novo, e cada tentativa
    é um registro + uma nota. É assim que dá pra contar 'quantas vezes ligamos'."""
    from app.db.session import SessionLocal

    lead_id, _, ref = lead_remarketing
    for _ in range(3):
        r = client.post(
            "/painel/ligacoes/registrar",
            json={"lead_id": lead_id, "resultado": lr.RESULTADO_NAO_ATENDEU},
            headers=_hdr("corretor"),
        )
        assert r.status_code == 200, r.text

    with SessionLocal() as s:
        assert lr.tentativas_do_lead(s, lead_id=ref, lead_espinha_id=lead_id) == 3
        toques = [
            t[0] for t in s.execute(
                sql_text(
                    "SELECT toque FROM ligacao_resultado "
                    "WHERE origem = :o AND origem_id = :l ORDER BY toque"
                ),
                {"o": lr.ORIGEM_REMARKETING, "l": lead_id},
            ).all()
        ]
        notas = [
            n[0] for n in s.execute(
                sql_text(
                    "SELECT texto FROM observacao_corretora WHERE lead_id = :r "
                    "ORDER BY id"
                ),
                {"r": ref},
            ).all()
        ]
    assert toques == [1, 2, 3]  # cada tentativa tem sua sequência
    assert len(notas) == 3
    assert notas[0].endswith("não atendeu")
    assert notas[2].endswith("(3ª tentativa)")


def test_fila_da_descanso_depois_da_tentativa(client, lead_remarketing):
    """Quem não atendeu volta pra fila — mas não na mesma hora."""
    from app.db.session import SessionLocal

    lead_id, _, _ = lead_remarketing
    client.post(
        "/painel/ligacoes/registrar",
        json={"lead_id": lead_id, "resultado": lr.RESULTADO_NAO_ATENDEU},
        headers=_hdr("corretor"),
    )
    fila = client.get("/painel/ligacoes/fila?limite=200", headers=_hdr("corretor")).json()
    assert lead_id not in {i["lead_id"] for i in fila["itens"]}

    # passado o descanso, ele volta — com o contador na frente da corretora
    with SessionLocal() as s:
        s.execute(
            sql_text(
                "UPDATE ligacao_resultado SET criado_em = NOW() - INTERVAL '48 hours' "
                "WHERE origem = :o AND origem_id = :l"
            ),
            {"o": lr.ORIGEM_REMARKETING, "l": lead_id},
        )
        s.commit()
    fila2 = client.get("/painel/ligacoes/fila?limite=200", headers=_hdr("corretor")).json()
    item = next((i for i in fila2["itens"] if i["lead_id"] == lead_id), None)
    assert item is not None
    assert item["tentativas"] == 1
    assert item["tentativa_atual"] == 2


def test_tentativas_ate_atender_media():
    """Só conta quem atendeu; sem ninguém atendendo é sem_dado, não zero."""
    agora = datetime.now(timezone.utc)

    def reg(lead: str, i: int, atendeu: bool):
        return {
            "origem": lr.ORIGEM_REMARKETING, "origem_id": 1, "toque": i,
            "lead_id": lead, "corretora_id": 1, "atendeu": atendeu,
            "quis_continuar": None, "criado_em": agora + timedelta(minutes=i),
        }

    # lead A atendeu na 3ª; lead B atendeu na 1ª; lead C nunca atendeu
    registros = [
        reg("A", 1, False), reg("A", 2, False), reg("A", 3, True),
        reg("B", 1, True),
        reg("C", 1, False), reg("C", 2, False),
    ]
    out = lr._tentativas_ate_atender(registros)
    assert out["leads_que_atenderam"] == 2
    assert out["media"] == 2.0  # (3 + 1) / 2
    assert out["maximo"] == 3
    assert out["sem_dado"] is False

    vazio = lr._tentativas_ate_atender([reg("C", 1, False)])
    assert vazio["media"] is None
    assert vazio["sem_dado"] is True


# ── reserva: duas corretoras não pegam o mesmo lead ──────────────────────────


def _ids_da_fila(client, user: str, limite: int = 200) -> set[int]:
    r = client.get(f"/painel/ligacoes/fila?limite={limite}", headers=_hdr(user))
    assert r.status_code == 200, r.text
    return {i["lead_id"] for i in r.json()["itens"]}


def test_fila_reserva_lead_pra_quem_pediu(client, lead_remarketing):
    """Pessoa10 abre a fila e PEGA os leads dela; a Pessoa11 não vê os mesmos."""
    lead_id, _, _ = lead_remarketing
    assert lead_id in _ids_da_fila(client, "corretor")
    assert lead_id not in _ids_da_fila(client, "corretor")
    # recarregar não perde a própria reserva
    assert lead_id in _ids_da_fila(client, "corretor")


def test_reserva_expira_e_o_lead_volta_pra_fila(client, lead_remarketing):
    """Reserva que não expira trancaria o lead pra sempre se a corretora
    fechasse a aba."""
    from app.db.session import SessionLocal

    lead_id, _, _ = lead_remarketing
    assert lead_id in _ids_da_fila(client, "corretor")
    assert lead_id not in _ids_da_fila(client, "corretor")

    with SessionLocal() as s:
        s.execute(
            sql_text(
                "UPDATE ligacao_reserva SET expira_em = NOW() - INTERVAL '1 minute' "
                "WHERE lead_id = :l"
            ),
            {"l": lead_id},
        )
        s.commit()
    assert lead_id in _ids_da_fila(client, "corretor")


def test_registrar_libera_a_reserva(client, lead_remarketing):
    from app.db.session import SessionLocal

    lead_id, _, _ = lead_remarketing
    assert lead_id in _ids_da_fila(client, "corretor")
    client.post(
        "/painel/ligacoes/registrar",
        json={"lead_id": lead_id, "resultado": lr.RESULTADO_NAO_ATENDEU},
        headers=_hdr("corretor"),
    )
    with SessionLocal() as s:
        n = s.execute(
            sql_text("SELECT COUNT(*) FROM ligacao_reserva WHERE lead_id = :l"),
            {"l": lead_id},
        ).scalar()
    assert n == 0


def _renovar(client, user: str, ids: list[int]):
    r = client.post(
        "/painel/ligacoes/renovar", json={"lead_ids": ids}, headers=_hdr(user)
    )
    assert r.status_code == 200, r.text
    return r.json()


def _vencer_reserva(lead_id: int) -> None:
    from app.db.session import SessionLocal

    with SessionLocal() as s:
        s.execute(
            sql_text(
                "UPDATE ligacao_reserva SET expira_em = NOW() - INTERVAL '1 minute' "
                "WHERE lead_id = :l"
            ),
            {"l": lead_id},
        )
        s.commit()


def test_renovar_segura_a_lista_aberta_alem_dos_30_minutos(client, lead_remarketing):
    """Caso Corretor (22/09): a aba aberta renova a reserva, e a outra corretora
    não recebe o lead nem depois que os 30 min originais passariam."""
    from app.db.session import SessionLocal

    lead_id, _, _ = lead_remarketing
    assert lead_id in _ids_da_fila(client, "corretor")
    _vencer_reserva(lead_id)

    out = _renovar(client, "corretor", [lead_id])
    assert out["meus"] == [lead_id]
    assert out["perdidos"] == []
    with SessionLocal() as s:
        vivo = s.execute(
            sql_text(
                "SELECT expira_em > NOW() FROM ligacao_reserva WHERE lead_id = :l"
            ),
            {"l": lead_id},
        ).scalar()
    assert vivo is True
    assert lead_id not in _ids_da_fila(client, "corretor")


def test_renovar_avisa_quando_outra_corretora_pegou(client, lead_remarketing):
    """Reserva venceu e a Pessoa11 pegou: a Pessoa10 NÃO recupera o lead ao renovar —
    ele volta como `perdido`, pra tela marcar o cartão."""
    lead_id, _, _ = lead_remarketing
    assert lead_id in _ids_da_fila(client, "corretor")
    _vencer_reserva(lead_id)
    assert lead_id in _ids_da_fila(client, "corretor")

    out = _renovar(client, "corretor", [lead_id])
    assert out["meus"] == []
    assert out["perdidos"] == [lead_id]
    # e a Pessoa11 continua com ele
    assert lead_id in _ids_da_fila(client, "corretor")


def test_renovar_avisa_quando_alguem_ja_registrou(client, lead_remarketing):
    """Outra corretora ligou e registrou: a reserva some, o lead é perdido."""
    lead_id, _, _ = lead_remarketing
    assert lead_id in _ids_da_fila(client, "corretor")
    _vencer_reserva(lead_id)
    assert lead_id in _ids_da_fila(client, "corretor")
    client.post(
        "/painel/ligacoes/registrar",
        json={"lead_id": lead_id, "resultado": lr.RESULTADO_NAO_ATENDEU},
        headers=_hdr("corretor"),
    )
    assert _renovar(client, "corretor", [lead_id])["perdidos"] == [lead_id]


def test_renovar_valida_o_corpo(client):
    r = client.post("/painel/ligacoes/renovar", json={}, headers=_hdr("corretor"))
    assert r.status_code == 422
    r = client.post(
        "/painel/ligacoes/renovar", json={"lead_ids": ["x"]}, headers=_hdr("corretor")
    )
    assert r.status_code == 422


def test_fila_concorrente_nao_entrega_o_mesmo_lead_duas_vezes(client, lead_remarketing):
    """Duas corretoras pedindo a fila ao mesmo tempo: o lead vai pra UMA só."""
    import threading

    lead_id, _, _ = lead_remarketing
    largada = threading.Barrier(2)
    saida: dict[str, set[int]] = {}

    def pede(user: str) -> None:
        largada.wait()
        saida[user] = _ids_da_fila(client, user)

    ts = [threading.Thread(target=pede, args=(u,)) for u in ("corretor", "corretor")]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    donos = [u for u, ids in saida.items() if lead_id in ids]
    assert len(donos) == 1, f"lead entregue pra {donos}"


# ── validação no BACKEND (o que um curl não pode furar) ──────────────────────


def test_registrar_recusa_lead_de_outra_corretora(client, lead_remarketing):
    from app.db.models import Lead
    from app.db.session import SessionLocal

    lead_id, _, _ = lead_remarketing
    with SessionLocal() as s:
        s.get(Lead, lead_id).owner_id = 2  # Pessoa11 já é dona
        s.commit()
    r = client.post(
        "/painel/ligacoes/registrar",
        json={"lead_id": lead_id, "resultado": lr.RESULTADO_QUER_CONTINUAR},
        headers=_hdr("corretor"),
    )
    assert r.status_code == 409, r.text
    with SessionLocal() as s:
        n = s.execute(
            sql_text("SELECT COUNT(*) FROM ligacao_resultado WHERE origem_id = :l "
                     "AND origem = :o"),
            {"l": lead_id, "o": lr.ORIGEM_REMARKETING},
        ).scalar()
    assert n == 0  # nem registro, nem nota na thread de lead alheio


def test_registrar_recusa_lead_fora_do_remarketing(client, lead_remarketing):
    from app.db.models import Lead
    from app.db.session import SessionLocal
    from app.services.lead_spine import _resolve_stage_id

    lead_id, _, _ = lead_remarketing
    with SessionLocal() as s:
        s.get(Lead, lead_id).stage_id = _resolve_stage_id(s, 1, None, "Visita marcada")
        s.commit()
    r = client.post(
        "/painel/ligacoes/registrar",
        json={"lead_id": lead_id, "resultado": lr.RESULTADO_NAO_ATENDEU},
        headers=_hdr("corretor"),
    )
    assert r.status_code == 422, r.text


def test_registrar_recusa_telefone_invalido(client, lead_remarketing):
    from app.db.models import Lead
    from app.db.session import SessionLocal

    lead_id, _, _ = lead_remarketing
    with SessionLocal() as s:
        s.get(Lead, lead_id).telefone_invalido = True
        s.commit()
    assert lead_id not in _ids_da_fila(client, "corretor")
    r = client.post(
        "/painel/ligacoes/registrar",
        json={"lead_id": lead_id, "resultado": lr.RESULTADO_NAO_ATENDEU},
        headers=_hdr("corretor"),
    )
    assert r.status_code == 422, r.text


def test_opt_out_sai_da_fila_e_o_registro_e_recusado(client, lead_remarketing):
    """Quem pediu pra parar não é oferecido nem aceito: a adoção dispara
    boas-vindas no WhatsApp, e opt-out é inviolável."""
    from app.db.models import Lead
    from app.db.session import SessionLocal

    lead_id, _, _ = lead_remarketing
    with SessionLocal() as s:
        tel = s.get(Lead, lead_id).telefone
        s.execute(
            sql_text(
                "INSERT INTO cadencia_estado (tenant_id, lead_id, telefone, status) "
                "VALUES (1, :lid, :tel, 'opt_out')"
            ),
            {"lid": f"optout-{lead_id}", "tel": tel},
        )
        s.commit()
    try:
        assert lead_id not in _ids_da_fila(client, "corretor")
        r = client.post(
            "/painel/ligacoes/registrar",
            json={"lead_id": lead_id, "resultado": lr.RESULTADO_QUER_CONTINUAR},
            headers=_hdr("corretor"),
        )
        assert r.status_code == 422, r.text
        assert "não receber" in r.json()["detail"]
    finally:
        with SessionLocal() as s:
            s.execute(
                sql_text("DELETE FROM cadencia_estado WHERE lead_id = :l"),
                {"l": f"optout-{lead_id}"},
            )
            s.commit()


def test_fila_nao_perde_lead_com_telefone_invalido_nulo(client, lead_remarketing):
    """`NOT l.telefone_invalido` some com o lead quando a coluna é NULL (em SQL,
    NOT NULL = NULL). A trava é `IS NOT TRUE`: ausência é 'ainda não marcado'."""
    from app.db.session import SessionLocal

    lead_id, _, _ = lead_remarketing
    with SessionLocal() as s:
        s.execute(sql_text("ALTER TABLE leads ALTER COLUMN telefone_invalido DROP NOT NULL"))
        s.execute(
            sql_text("UPDATE leads SET telefone_invalido = NULL WHERE id = :l"),
            {"l": lead_id},
        )
        s.commit()
    try:
        assert lead_id in _ids_da_fila(client, "corretor")
        with SessionLocal() as s:
            assert lr.validar_lead_da_janela(s, lead_id=lead_id, corretora_id=1) is None
    finally:
        with SessionLocal() as s:
            s.execute(
                sql_text("UPDATE leads SET telefone_invalido = FALSE WHERE id = :l"),
                {"l": lead_id},
            )
            s.execute(
                sql_text("ALTER TABLE leads ALTER COLUMN telefone_invalido SET NOT NULL")
            )
            s.commit()


# ── "última conversa" do cartão ──────────────────────────────────────────────


def _msg(tenant: int, telefone: str, texto: str) -> None:
    """Uma mensagem no histórico da Evolution."""
    from app.db.session import SessionLocal

    with SessionLocal() as s:
        s.execute(
            sql_text(
                "INSERT INTO mensagens_whatsapp (tenant_id, evolution_message_id, "
                " instance, remote_jid, contato_telefone, from_corretora, "
                " tipo_mensagem, conteudo_texto, timestamp_evolution) "
                "VALUES (:t, :mid, 'acme_corretora_c', :jid, :tel, false, "
                "        'conversation', :txt, NOW())"
            ),
            {
                "t": tenant, "mid": f"t-msg-{uuid.uuid4().hex[:12]}",
                "jid": f"{telefone}@s.whatsapp.net", "tel": telefone, "txt": texto,
            },
        )
        s.commit()


def test_ultima_conversa_nao_vaza_de_outro_tenant_nem_de_outro_contato(
    client, lead_remarketing
):
    """O lateral casava só pelos 8 últimos dígitos, sem tenant: trazia conversa
    de outro cliente — e de outra PESSOA com sufixo igual — como contexto."""
    from app.db.session import SessionLocal

    lead_id, _, _ = lead_remarketing  # telefone +5500000000000

    def ultima() -> str | None:
        r = client.get("/painel/ligacoes/fila?limite=200", headers=_hdr("corretor")).json()
        item = next(i for i in r["itens"] if i["lead_id"] == lead_id)
        return item["ultima_msg"]

    try:
        # 1) mensagem de OUTRO tenant não vira contexto deste lead
        _msg(2, "+5500000000000", "conversa de outro cliente")
        assert ultima() is None

        # 2) mensagem do mesmo tenant e do mesmo número: essa é a conversa dele
        _msg(1, "+5500000000000", "quero ver o apto")
        assert ultima() == "quero ver o apto"

        # 3) sufixo-8 ambíguo (outro DDD, outra pessoa): não mostra nada em vez
        #    de mostrar conversa de terceiro
        _msg(1, "+5500000000000", "sou outra pessoa")
        assert ultima() is None
    finally:
        with SessionLocal() as s:
            s.execute(
                sql_text(
                    "DELETE FROM mensagens_whatsapp "
                    "WHERE contato_telefone IN ('+5500000000000', '+5500000000000')"
                )
            )
            s.commit()


# ── corrida no contador de tentativas ────────────────────────────────────────


def test_duas_ligacoes_simultaneas_nao_perdem_registro(lead_remarketing, monkeypatch):
    """Sem lock, as duas calculavam o MESMO `toque`: uma inseria e a outra
    voltava como `ja_registrado` — a segunda ligação REAL perdia resultado,
    observação e nota.

    A corrida é FORÇADA (não depende de sorte de agendamento): a barreira só
    libera quando as DUAS threads terminam de calcular o número da tentativa.
    Com o lock, a segunda nem chega lá enquanto a primeira não commita — a
    barreira estoura no timeout e a execução segue serializada, que é
    exatamente o comportamento que se quer provar.
    """
    import threading

    from app.db.session import SessionLocal

    lead_id, _, ref = lead_remarketing
    largada = threading.Barrier(2)
    calculado = threading.Barrier(2)
    _original = lr._proximo_toque

    def _com_corrida(*a, **kw):
        n = _original(*a, **kw)
        try:
            calculado.wait(timeout=1.5)
        except threading.BrokenBarrierError:
            pass
        return n

    monkeypatch.setattr(lr, "_proximo_toque", _com_corrida)
    saida: list[dict] = []
    erros: list[BaseException] = []

    def registra(obs: str) -> None:
        try:
            with SessionLocal() as s:
                largada.wait()
                out = lr.registrar_resultado(
                    s,
                    origem=lr.ORIGEM_REMARKETING,
                    origem_id=lead_id,
                    resultado=lr.ResultadoLigacao.do_estado(
                        lr.RESULTADO_NAO_ATENDEU, obs
                    ),
                    lead_id=ref,
                    corretora_id=1,
                    autor="corretor",
                )
                s.commit()
                saida.append(out)
        except BaseException as e:  # noqa: BLE001
            erros.append(e)

    ts = [threading.Thread(target=registra, args=(o,)) for o in ("primeira", "segunda")]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    assert not erros, erros
    assert [o["ja_registrado"] for o in saida] == [False, False]
    with SessionLocal() as s:
        linhas = s.execute(
            sql_text(
                "SELECT toque, observacao FROM ligacao_resultado "
                "WHERE origem = :o AND origem_id = :l ORDER BY toque"
            ),
            {"o": lr.ORIGEM_REMARKETING, "l": lead_id},
        ).mappings().all()
        notas = s.execute(
            sql_text("SELECT COUNT(*) FROM observacao_corretora WHERE lead_id = :r"),
            {"r": ref},
        ).scalar()
    assert [l["toque"] for l in linhas] == [1, 2]
    assert {l["observacao"] for l in linhas} == {"primeira", "segunda"}
    assert notas == 2


# ── boas-vindas de continuidade ──────────────────────────────────────────────


def test_copy_de_continuidade_respeita_o_formato_da_casa():
    from app.services import boas_vindas_continuidade as bvc

    texto = bvc.render("Pessoa5")
    assert "Pessoa5" in texto
    assert bvc.PLACEHOLDER_NOME not in texto
    assert texto.rstrip().endswith("?")            # trava da casa: termina com pergunta
    assert "cadastr" not in texto.lower()          # o vexame que a copy evita
    assert "saiba mais" not in texto.lower()       # o template já fecha com isso
    assert not texto.lstrip().lower().startswith(("oi", "olá", "ola"))
    assert "telefone" in texto.lower()             # reconhece a ligação


def test_lead_adotado_recebe_continuidade_e_fica_fora_do_ab(lead_remarketing):
    """A tag manda a boas-vindas trocar a copy — e a variante do A/B não sai."""
    from app.db.models import Lead
    from app.db.session import SessionLocal
    from app.services import boas_vindas_continuidade as bvcont
    from app.services.lead_intake import boas_vindas_texto, nome_da_linha

    lead_id, _, _ = lead_remarketing
    with SessionLocal() as s:
        assert lr.veio_de_ligacao(s, lead_id) is False
        lr.adotar_lead(s, lead_id=lead_id, corretora_id=1, autor="corretor")
        s.commit()
        assert lr.veio_de_ligacao(s, lead_id) is True

    # a copy de continuidade é OUTRA coisa, não uma das variantes do teste
    continuidade = bvcont.render(nome_da_linha(1))
    assert continuidade != boas_vindas_texto(1, lead_id=lead_id)


# ── em que horário as pessoas atendem ────────────────────────────────────────


def _reg_hora(iso_utc: str, atendeu: bool) -> dict:
    return {
        "criado_em": datetime.fromisoformat(iso_utc).replace(tzinfo=timezone.utc),
        "atendeu": atendeu,
    }


def test_horario_agrupa_em_sao_paulo_e_nao_em_utc():
    """21h30 de SP é 00h30 UTC do dia SEGUINTE. Agrupado cru, viraria
    'madrugada de quinta' — e a conclusão sairia invertida."""
    sp = lr._em_sao_paulo(datetime(2026, 8, 6, 0, 30, tzinfo=timezone.utc))
    assert (sp.hour, sp.day) == (21, 5)

    # 14h de SP = 17h UTC — a faixa tem que ser a de 14h, não a de 17h
    out = lr._quando_atendem([_reg_hora("2026-08-05T17:00:00", True)])
    por_rotulo = {f["rotulo"]: f for f in out["por_faixa"]}
    assert por_rotulo["14h–15h"]["ligacoes"] == 1
    assert por_rotulo["17h–18h"]["ligacoes"] == 0
    # e o dia da semana segue o fuso de SP
    assert [d["rotulo"] for d in out["por_dia_semana"] if d["ligacoes"]] == ["quarta"]


def test_faixa_sem_ligacao_e_vazia_nunca_zero_por_cento():
    out = lr._quando_atendem([_reg_hora("2026-08-05T17:00:00", False)])
    por_rotulo = {f["rotulo"]: f for f in out["por_faixa"]}
    vazia = por_rotulo["09h–10h"]
    assert vazia["ligacoes"] == 0
    assert vazia["taxa_atendimento"] is None  # não é 0.0
    assert vazia["sem_dado"] is True
    # a faixa COM ligação e ninguém atendendo é 0% de verdade
    assert por_rotulo["14h–15h"]["taxa_atendimento"] == 0.0
    assert por_rotulo["14h–15h"]["sem_dado"] is False


def test_melhor_horario_so_aparece_com_amostra():
    """Com pouca ligação, 'as 14h convertem 100%' quer dizer 'uma pessoa
    atendeu uma vez' — o campo fica None e a tela diz que está juntando dado."""
    poucas = [_reg_hora("2026-08-05T17:00:00", True) for _ in range(3)]
    out = lr._quando_atendem(poucas)
    assert out["confiavel"] is False
    assert out["melhor_faixa"] is None
    assert out["por_faixa"][6]["amostra_pequena"] is True  # faixa das 14h

    # volume suficiente: 100 ligações, as 14h atendem mais que as 9h
    muitas = (
        [_reg_hora("2026-08-05T17:00:00", i % 2 == 0) for i in range(50)]     # 14h SP
        + [_reg_hora("2026-08-05T12:00:00", i % 10 == 0) for i in range(50)]  # 9h SP
    )
    out2 = lr._quando_atendem(muitas)
    assert out2["confiavel"] is True
    assert out2["melhor_faixa"]["rotulo"] == "14h–15h"
    assert out2["melhor_faixa"]["ligacoes"] == 50


def test_ligacao_fora_do_horario_comercial_nao_some():
    out = lr._quando_atendem([_reg_hora("2026-08-06T00:30:00", True)])
    assert out["fora_da_janela"]["ligacoes"] == 1
    assert out["amostra"] == 1


def test_metrica_do_endpoint_traz_o_bloco_de_horario(client):
    body = client.get(
        "/painel/tarefas/metricas-ligacao?dias=30", headers=_hdr("dona")
    ).json()
    q = body["quando_atendem"]
    assert q["fuso"] == "America/Sao_Paulo"
    assert len(q["por_faixa"]) == lr.HORA_FIM - lr.HORA_INICIO
    assert len(q["por_dia_semana"]) == 7
    assert isinstance(q["confiavel"], bool)


# ── privacidade: corretora só vê o que é dela ────────────────────────────────


def test_corretora_nao_ve_a_reserva_da_outra_e_admin_ve_tudo(client, lead_remarketing):
    """Regra dura da casa: corretora nunca vê lead de outra. Na aba Ligar,
    "dela" = reservado por ela."""
    lead_id, _, _ = lead_remarketing
    assert lead_id in _ids_da_fila(client, "corretor")
    assert lead_id not in _ids_da_fila(client, "corretor")

    # admin em modo supervisão vê, e vê DE QUEM está
    r = client.get(
        "/painel/ligacoes/fila?limite=200&ver_tudo=1", headers=_hdr("dona")
    )
    assert r.status_code == 200, r.text
    item = next(i for i in r.json()["itens"] if i["lead_id"] == lead_id)
    assert item["reservado_por"] == "corretor"


def test_ver_tudo_e_admin_only(client):
    r = client.get("/painel/ligacoes/fila?ver_tudo=1", headers=_hdr("corretor"))
    assert r.status_code == 403
    assert r.json()["detail"] == "painel_admin_required"


def test_supervisao_do_admin_nao_rouba_lead_da_corretora(client, lead_remarketing):
    """Admin olhando a fila não pode tomar pra si o lead que já é da Pessoa10."""
    lead_id, _, _ = lead_remarketing
    assert lead_id in _ids_da_fila(client, "corretor")
    client.get("/painel/ligacoes/fila?limite=200&ver_tudo=1", headers=_hdr("dona"))
    assert lead_id in _ids_da_fila(client, "corretor")  # continua dela
    assert lead_id not in _ids_da_fila(client, "corretor")


# ── meta do dia e da semana ──────────────────────────────────────────────────


@pytest.fixture
def sem_ligacoes_da_corretor():
    """Zera o histórico da Pessoa10 pra a meta ser medida do zero."""
    from app.db.session import SessionLocal

    def _limpar():
        with SessionLocal() as s:
            s.execute(sql_text("DELETE FROM ligacao_resultado WHERE corretora_id = 1"))
            s.execute(sql_text("DELETE FROM ligacao_meta_dia WHERE corretora_id = 1"))
            s.commit()

    _limpar()
    yield
    _limpar()


def _progresso(cid: int = 1) -> dict:
    from app.db.session import SessionLocal

    with SessionLocal() as s:
        return lr.progresso_ligacao(s, corretora_id=cid)


def _registrar_n(n: int, cid: int = 1, quando: datetime | None = None) -> None:
    """N ligações da corretora, sem passar pela fila (mede só a meta)."""
    from app.db.session import SessionLocal

    with SessionLocal() as s:
        for i in range(n):
            lr.registrar_resultado(
                s,
                origem=lr.ORIGEM_TAREFA,
                origem_id=900000 + i,
                resultado=lr.ResultadoLigacao.do_estado(lr.RESULTADO_NAO_ATENDEU),
                lead_id=None,
                corretora_id=cid,
                autor="corretor",
            )
        if quando is not None:
            s.execute(
                sql_text(
                    "UPDATE ligacao_resultado SET criado_em = :q "
                    "WHERE corretora_id = :c AND origem = 'tarefa'"
                ),
                {"q": quando, "c": cid},
            )
        s.commit()


def test_meta_do_dia_e_da_semana(sem_ligacoes_da_corretor):
    from app.services import config_operacional

    meta_dia = config_operacional.ligacao_meta_diaria()
    p = _progresso()
    assert p["meta_diaria"] == meta_dia
    assert p["feitas_hoje"] == 0
    assert p["restante_hoje"] == meta_dia
    assert p["concluiu_dia"] is False

    _registrar_n(meta_dia)
    p2 = _progresso()
    assert p2["feitas_hoje"] == meta_dia
    assert p2["restante_hoje"] == 0
    assert p2["concluiu_dia"] is True
    assert p2["faltam_semana"] == p2["meta_semanal"] - meta_dia


def test_semana_comeca_na_segunda_em_sao_paulo():
    """Ligação às 21h de SP é 00h UTC do dia seguinte — contada crua, ela cairia
    no dia (e às vezes na SEMANA) errados."""
    # 00h30 UTC de segunda 10/08 = 21h30 de DOMINGO 09/08 em SP
    quase_segunda = datetime(2026, 8, 10, 0, 30, tzinfo=timezone.utc)
    assert lr._hoje_sp(quase_segunda).isoformat() == "2026-08-09"
    seg, dom = lr._semana_sp(quase_segunda)
    assert (seg.isoformat(), dom.isoformat()) == ("2026-08-03", "2026-08-09")
    # meia hora depois já é segunda em SP — semana nova
    seg2, _ = lr._semana_sp(quase_segunda + timedelta(hours=4))
    assert seg2.isoformat() == "2026-08-10"


def test_fila_entrega_no_maximo_o_bloco_do_dia(client, sem_ligacoes_da_corretor):
    from app.services import config_operacional

    meta_dia = config_operacional.ligacao_meta_diaria()
    itens = client.get(
        "/painel/ligacoes/fila?limite=200", headers=_hdr("corretor")
    ).json()["itens"]
    assert len(itens) <= meta_dia


def test_confete_cai_uma_vez_so_no_dia(sem_ligacoes_da_corretor):
    from app.db.session import SessionLocal

    with SessionLocal() as s:
        assert lr.marcar_confete(s, corretora_id=1) is True
        s.commit()
    with SessionLocal() as s:
        assert lr.marcar_confete(s, corretora_id=1) is False  # F5 não repete
        s.commit()


def test_adiantar_so_depois_de_fechar_o_bloco_e_nao_dobra_amanha(
    client, sem_ligacoes_da_corretor
):
    from app.db.session import SessionLocal
    from app.services import config_operacional

    meta_dia = config_operacional.ligacao_meta_diaria()

    # com o bloco em aberto, o botão não libera nada
    r = client.post("/painel/ligacoes/adiantar", headers=_hdr("corretor"))
    assert r.status_code == 409
    assert r.json()["detail"] == "bloco_atual_em_aberto"

    _registrar_n(meta_dia)
    r2 = client.post("/painel/ligacoes/adiantar", headers=_hdr("corretor"))
    assert r2.status_code == 200, r2.text
    p = r2.json()["progresso"]
    assert p["blocos_extras"] == 1
    assert p["alvo_hoje"] == meta_dia * 2
    assert p["restante_hoje"] == meta_dia
    assert p["concluiu_dia"] is False

    # AMANHÃ não dobra nem zera: o bloco extra é do dia civil de hoje
    with SessionLocal() as s:
        amanha = datetime.now(timezone.utc) + timedelta(days=1)
        p_amanha = lr.progresso_ligacao(s, corretora_id=1, agora=amanha)
    assert p_amanha["blocos_extras"] == 0
    assert p_amanha["alvo_hoje"] == meta_dia
    # o que ANDOU foi a semana — é lá que a folga aparece
    assert p_amanha["feitas_semana"] >= meta_dia


def test_registrar_devolve_meta_e_nao_quebra_o_registro(client, lead_remarketing,
                                                        sem_ligacoes_da_corretor):
    lead_id, _, _ = lead_remarketing
    r = client.post(
        "/painel/ligacoes/registrar",
        json={"lead_id": lead_id, "resultado": lr.RESULTADO_NAO_ATENDEU},
        headers=_hdr("corretor"),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["registro_ligacao"]["ja_registrado"] is False
    assert body["meta"]["feitas_hoje"] == 1
    assert body["confete"] is False  # longe da meta


# ── "Venda perdida" na janela vale mesmo em lead de outra corretora ──────────
#
# Pedido da Dona (10/08/2026), depois de a Pessoa10 ver "não consegui marcar (esse
# lead já tem outra dona — fala com a gestão)": se o lead APARECEU na área de
# ligação e alguém falou com a pessoa, "venda perdida" tem que valer. Segurar o
# botão deixa o lead perdido circulando na fila e some com a informação de quem
# fez a ligação.
#
# O que NÃO muda: a dona original continua dona (`marcar_venda_perdida` só
# define dono quando não há nenhum). Perder ≠ adotar — `/registrar`, que adota,
# mantém a trava.


def test_perder_aceita_lead_de_outra_corretora(client, lead_remarketing):
    from app.db.models import Lead
    from app.db.session import SessionLocal

    lead_id, _, _ = lead_remarketing
    with SessionLocal() as s:
        s.get(Lead, lead_id).owner_id = 2  # Pessoa11 é a dona
        s.commit()

    r = client.post(
        "/painel/ligacoes/perder",
        json={"lead_id": lead_id, "motivo": "comprou com outra"},
        headers=_hdr("corretor"),
    )
    assert r.status_code == 200, r.text

    with SessionLocal() as s:
        lead = s.get(Lead, lead_id)
        etapa = s.execute(
            sql_text("SELECT nome FROM pipeline_stages WHERE id = :s"),
            {"s": lead.stage_id},
        ).scalar()
        assert etapa == lr.STAGE_PERDIDA
        # a Pessoa10 marcou, mas o lead continua da Pessoa11 — perder não é adotar
        assert lead.owner_id == 2


def test_registrar_continua_recusando_lead_de_outra_corretora(client, lead_remarketing):
    """A trava do /registrar fica: registrar "quer continuar" ADOTA o lead."""
    from app.db.models import Lead
    from app.db.session import SessionLocal

    lead_id, _, _ = lead_remarketing
    with SessionLocal() as s:
        s.get(Lead, lead_id).owner_id = 2
        s.commit()
    r = client.post(
        "/painel/ligacoes/registrar",
        json={"lead_id": lead_id, "resultado": lr.RESULTADO_QUER_CONTINUAR},
        headers=_hdr("corretor"),
    )
    assert r.status_code == 409, r.text


# ── leads da Corretor que entraram este ano (pedido da Dona, 24/09/2026) ──────────
#
# A fila só via lead SEM DONO e esgotou: as corretoras passaram a girar os
# mesmos ~140 que não atendem. A Corretor é a linha do carrossel, não corretora —
# lead dela na etapa "Fila" (carrossel ainda não tocou) que entrou em 2026 vai
# pra fila de ligação, e a adoção passa o lead pra corretora.


@pytest.fixture
def lead_da_corretor():
    """Fábrica de lead da Corretor no remarketing: `(etapa_id, entrou_em) -> id`."""
    import random

    from app.db.models import Lead
    from app.db.session import SessionLocal
    from app.services import carrossel_corretor as cb

    with SessionLocal() as s:
        s.execute(sql_text(
            "INSERT INTO corretoras (id, nome, telefone, telefone_normalizado, ativo) "
            "VALUES (:id, 'Corretor', '+5500000000000', '+5500000000000', true) "
            "ON CONFLICT (id) DO UPDATE SET nome = 'Corretor', ativo = true"
        ), {"id": cb.CORRETORA_CORRETOR})
        s.commit()

    criados: list[tuple[int, str]] = []

    def _criar(etapa_id: int = cb.STAGE_FILA_PADRAO,
               entrou_em: datetime = datetime(2026, 3, 10, tzinfo=timezone.utc)) -> int:
        ref = f"t-corretor-{uuid.uuid4().hex[:10]}"
        tel = "+5515" + "9" + "".join(random.choice("0123456789") for _ in range(8))
        with SessionLocal() as s:
            lead = Lead(
                tenant_id=1, external_ref=ref, nome="Lead da Corretor",
                telefone=tel, owner_id=cb.CORRETORA_CORRETOR, stage_id=etapa_id,
                criado_em_origem=entrou_em,
                created_at=datetime.now(timezone.utc) - timedelta(days=90),
                updated_at=datetime.now(timezone.utc) - timedelta(days=90),
            )
            s.add(lead)
            s.commit()
            criados.append((lead.id, ref))
            return lead.id

    yield _criar

    with SessionLocal() as s:
        for lead_id, ref in criados:
            s.execute(sql_text("DELETE FROM lead_events WHERE lead_id = :l"), {"l": lead_id})
            s.execute(
                sql_text("DELETE FROM ligacao_resultado WHERE origem = :o AND origem_id = :l"),
                {"o": lr.ORIGEM_REMARKETING, "l": lead_id},
            )
            s.execute(sql_text("DELETE FROM observacao_corretora WHERE lead_id = :r"), {"r": ref})
            s.execute(sql_text("DELETE FROM ligacao_reserva WHERE lead_id = :l"), {"l": lead_id})
            s.execute(sql_text("DELETE FROM leads WHERE id = :l"), {"l": lead_id})
        s.commit()


def test_fila_traz_lead_da_corretor_que_entrou_este_ano(client, lead_da_corretor):
    lead_id = lead_da_corretor()
    assert lead_id in _ids_da_fila(client, "corretor")


def test_fila_traz_lead_da_corretor_de_2025(client, lead_da_corretor):
    """Até 01/10/2026 o lote de 2025 ficava FORA (corte por ano). Ele entra: a
    base elegível era 417 pra ~130 ligações/dia e o estoque girava em 3 dias."""
    lead_id = lead_da_corretor(entrou_em=datetime(2025, 12, 31, tzinfo=timezone.utc))
    assert lead_id in _ids_da_fila(client, "corretor")


def test_fila_deixa_fora_quem_entrou_antes_de_2025(client, lead_da_corretor):
    lead_id = lead_da_corretor(entrou_em=datetime(2024, 12, 31, tzinfo=timezone.utc))
    assert lead_id not in _ids_da_fila(client, "corretor")


def test_fila_poe_quem_entrou_este_ano_na_frente_do_lote_de_2025(
    client, lead_da_corretor
):
    """O corte por ano virou ORDENAÇÃO: o pedido da Dona (24/09) de ligar
    primeiro pra quem entrou este ano continua valendo depois da liberação.

    O de 2025 é criado PRIMEIRO de propósito — os dois empatam em tentativas e
    em `updated_at`, então sem a prioridade o desempate por `lead_id` o
    colocaria na frente."""
    velho = lead_da_corretor(entrou_em=datetime(2025, 2, 10, tzinfo=timezone.utc))
    novo = lead_da_corretor(entrou_em=datetime(2026, 2, 10, tzinfo=timezone.utc))
    r = client.get("/painel/ligacoes/fila?limite=200", headers=_hdr("corretor"))
    assert r.status_code == 200, r.text
    ordem = [i["lead_id"] for i in r.json()["itens"]]
    assert novo in ordem and velho in ordem
    assert ordem.index(novo) < ordem.index(velho)


def test_fila_deixa_com_a_corretor_quem_o_carrossel_ja_tocou(client, lead_da_corretor):
    disparado = 24  # etapa "Disparado" do remarketing
    lead_id = lead_da_corretor(etapa_id=disparado)
    assert lead_id not in _ids_da_fila(client, "corretor")


def test_quer_continuar_tira_o_lead_da_corretor_e_da_pra_corretora(client, lead_da_corretor):
    from app.db.models import Lead
    from app.db.session import SessionLocal

    lead_id = lead_da_corretor()
    r = client.post(
        "/painel/ligacoes/registrar",
        json={"lead_id": lead_id, "resultado": lr.RESULTADO_QUER_CONTINUAR},
        headers=_hdr("corretor"),
    )
    assert r.status_code == 200, r.text
    adocao = r.json()["adocao"]
    assert adocao["definiu_dono"] is True
    assert adocao["ja_tem_dono"] is False
    with SessionLocal() as s:
        assert s.get(Lead, lead_id).owner_id == 1


def test_nao_atendeu_deixa_o_lead_com_a_corretor(client, lead_da_corretor):
    from app.db.models import Lead
    from app.db.session import SessionLocal
    from app.services import carrossel_corretor as cb

    lead_id = lead_da_corretor()
    r = client.post(
        "/painel/ligacoes/registrar",
        json={"lead_id": lead_id, "resultado": lr.RESULTADO_NAO_ATENDEU},
        headers=_hdr("corretor"),
    )
    assert r.status_code == 200, r.text
    with SessionLocal() as s:
        assert s.get(Lead, lead_id).owner_id == cb.CORRETORA_CORRETOR


# ── "número errado" (01/10/2026) ─────────────────────────────────────────────
#
# Antes deste botão o único registro possível pra número que não completa a
# chamada era "não atendeu" — e a fila só solta quem ATENDEU, então o lead
# voltava a cada 24h, pra sempre. Medido: 1.102 ligações da Pessoa7 em 582
# pessoas, 384 leads com 2 ou 3 chamadas.


def test_telefone_errado_tira_o_lead_da_fila_pra_sempre(client, lead_remarketing):
    from app.db.models import Lead
    from app.db.session import SessionLocal

    lead_id, _, _ = lead_remarketing
    assert lead_id in _ids_da_fila(client, "corretor")

    r = client.post(
        "/painel/ligacoes/telefone-errado",
        json={"lead_id": lead_id, "observacao": "caiu em outra pessoa"},
        headers=_hdr("corretor"),
    )
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert r.json()["ja_marcado"] is False
    assert r.json()["leads_marcados"] >= 1

    with SessionLocal() as s:
        assert s.get(Lead, lead_id).telefone_invalido is True
    assert lead_id not in _ids_da_fila(client, "corretor")


def test_telefone_errado_nao_conta_como_ligacao(client, lead_remarketing):
    """A taxa de atendimento não pode cair por causa de cadastro furado: o
    registro NÃO entra em `ligacao_resultado` (nem como "não atendeu")."""
    from app.db.session import SessionLocal

    lead_id, _, _ = lead_remarketing
    r = client.post(
        "/painel/ligacoes/telefone-errado",
        json={"lead_id": lead_id},
        headers=_hdr("corretor"),
    )
    assert r.status_code == 200, r.text
    with SessionLocal() as s:
        n = s.execute(
            sql_text(
                "SELECT COUNT(*) FROM ligacao_resultado "
                " WHERE origem = :o AND origem_id = :l"
            ),
            {"o": lr.ORIGEM_REMARKETING, "l": lead_id},
        ).scalar()
    assert n == 0


def test_telefone_errado_deixa_nota_na_conversa(client, lead_remarketing):
    from app.db.session import SessionLocal

    lead_id, _, ref = lead_remarketing
    r = client.post(
        "/painel/ligacoes/telefone-errado",
        json={"lead_id": lead_id, "observacao": "número de outra pessoa"},
        headers=_hdr("corretor"),
    )
    assert r.status_code == 200, r.text
    with SessionLocal() as s:
        texto = s.execute(
            sql_text(
                "SELECT texto FROM observacao_corretora WHERE lead_id = :r "
                " ORDER BY id DESC LIMIT 1"
            ),
            {"r": ref},
        ).scalar()
    assert texto is not None
    assert "Número errado" in texto
    assert "número de outra pessoa" in texto


def test_telefone_errado_duas_vezes_nao_e_erro(client, lead_remarketing):
    """Clique duplo ou aba velha: a ação pedida já é o estado atual. 422 aqui
    apareceria pra corretora como falha de algo que deu certo."""
    lead_id, _, _ = lead_remarketing
    primeiro = client.post(
        "/painel/ligacoes/telefone-errado",
        json={"lead_id": lead_id},
        headers=_hdr("corretor"),
    )
    assert primeiro.status_code == 200, primeiro.text
    segundo = client.post(
        "/painel/ligacoes/telefone-errado",
        json={"lead_id": lead_id},
        headers=_hdr("corretor"),
    )
    assert segundo.status_code == 200, segundo.text
    assert segundo.json() == {
        "ok": True, "ja_marcado": True, "lead_id": lead_id, "leads_marcados": 0,
    }
