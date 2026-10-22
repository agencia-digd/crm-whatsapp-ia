"""Trava LIGAÇÃO × AUTOMAÇÃO — a colisão que a Dona achou em 05/08.

A fila da janela de ligação é feita de lead do funil `remarketing` SEM DONO —
ou seja, exatamente os leads que estão na RÉGUA DE NUTRIÇÃO (etapas 30/60/90/
120/150 dias, 1 mensagem a cada 30 dias pela linha da Pessoa12). Com os 548 leads
descongelados em 05/08 (~30/dia voltando a ficar devidos), a mesma pessoa
receberia ligação da corretora e mensagem automática NO MESMO DIA, de duas
fontes que não se falam.

A trava é SIMÉTRICA (`reengajamento_guard.JANELA_SILENCIO_DIAS`):
  - quem levou LIGAÇÃO há pouco não recebe mensagem — o marco ADIA, não some;
  - quem recebeu MENSAGEM automática há pouco não é oferecido pra ligar.

E a PRECEDÊNCIA: lead adotado na ligação (foi pra "Novo" com dona) não é puxado
de volta pra régua pela fase de entrada da nutrição.

Precisa de DB real (TEST_DATABASE_URL — ver conftest.py): as consultas usam
`regexp_replace`, que não existe no SQLite (lá a trava é inerte por fail-open).
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text as sql_text

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="precisa de TEST_DATABASE_URL (Postgres de teste)",
)

from app.services import ligacao_resultado as lr  # noqa: E402
from app.services import nutricao  # noqa: E402
from app.services import reengajamento_guard as guard  # noqa: E402


class FakeSender:
    """Nunca chama o motor real de envio."""

    def __init__(self) -> None:
        self.chamadas: list[dict] = []

    def __call__(self, telefone, texto, *, com_botao):  # noqa: ANN001
        self.chamadas.append({"telefone": telefone, "texto": texto})
        return {"ok": True, "dry_run": True, "sent": False}


@pytest.fixture
def lead_na_nutricao():
    """Lead SEM DONO numa etapa da régua de nutrição + o estado que a alimenta.

    É o perfil exato do que a fila de ligação devolve hoje (545 leads, todos
    `origem='nutricao_kommo'`).
    """
    from app.db.models import CadenciaEstado, Lead, PipelineStage
    from app.db.session import SessionLocal
    from app.services import cadencia

    cadencia._LAST_ENQUEUE["at"] = 0.0
    ref = f"t-nut-{uuid.uuid4().hex[:10]}"
    tel = f"5515{uuid.uuid4().int % 900000000 + 100000000}"
    agora = datetime.now(timezone.utc)
    with SessionLocal() as s:
        etapa = (
            s.query(PipelineStage)
            .filter(
                PipelineStage.tenant_id == 1,
                PipelineStage.funil == "remarketing",
                PipelineStage.nome == "Base",
            )
            .first()
        )
        if etapa is None:
            etapa = PipelineStage(tenant_id=1, nome="Base", ordem=1, funil="remarketing")
            s.add(etapa)
            s.flush()
        lead = Lead(
            tenant_id=1, external_ref=ref, nome="Lead Nutrição",
            telefone=f"+{tel}", owner_id=None, stage_id=etapa.id,
            created_at=agora - timedelta(days=120),
            updated_at=agora - timedelta(days=120),
        )
        est = CadenciaEstado(
            tenant_id=1, lead_id=ref, telefone=tel, corretora_id=3,
            toque_atual=8, status="remarketing",
            proximo_toque_em=agora - timedelta(minutes=5),  # DEVIDO agora
            criado_em=agora - timedelta(days=60), atualizado_em=agora,
        )
        s.add_all([lead, est])
        s.commit()
        ids = (lead.id, est.id, ref, tel, etapa.id)
    yield ids
    lead_id, est_id, ref, tel, _ = ids
    with SessionLocal() as s:
        s.execute(sql_text("DELETE FROM lead_events WHERE lead_id = :l"), {"l": lead_id})
        s.execute(
            sql_text("DELETE FROM cadencia_eventos WHERE cadencia_id = :c"), {"c": est_id}
        )
        s.execute(sql_text("DELETE FROM cadencia_estado WHERE id = :c"), {"c": est_id})
        s.execute(
            sql_text("DELETE FROM ligacao_resultado WHERE origem_id = :l OR lead_id = :r"),
            {"l": lead_id, "r": ref},
        )
        s.execute(sql_text("DELETE FROM observacao_corretora WHERE lead_id = :r"), {"r": ref})
        s.execute(sql_text("DELETE FROM ligacao_reserva WHERE lead_id = :l"), {"l": lead_id})
        s.execute(sql_text("DELETE FROM leads WHERE id = :l"), {"l": lead_id})
        s.commit()


def _registrar_ligacao(lead_id: int, ref: str, *, ha_dias: float = 0) -> None:
    from app.db.session import SessionLocal

    with SessionLocal() as s:
        lr.registrar_resultado(
            s,
            origem=lr.ORIGEM_REMARKETING,
            origem_id=lead_id,
            resultado=lr.ResultadoLigacao.do_estado(lr.RESULTADO_NAO_ATENDEU),
            lead_id=ref,
            corretora_id=1,
            autor="corretor",
        )
        if ha_dias:
            s.execute(
                sql_text(
                    "UPDATE ligacao_resultado SET criado_em = NOW() - "
                    "(:d || ' days')::interval WHERE origem_id = :l AND origem = :o"
                ),
                {"d": str(ha_dias), "l": lead_id, "o": lr.ORIGEM_REMARKETING},
            )
        s.commit()


# ── lado A: quem levou ligação não recebe mensagem automática ────────────────


def test_guard_enxerga_a_ligacao_dentro_da_janela(lead_na_nutricao):
    from app.db.session import SessionLocal

    lead_id, _, ref, tel, _ = lead_na_nutricao
    with SessionLocal() as s:
        assert guard.ligacao_recente(tel, session=s) is False
    _registrar_ligacao(lead_id, ref)
    with SessionLocal() as s:
        assert guard.ligacao_recente(tel, session=s) is True
    # fora da janela volta a liberar — o silêncio é temporário
    _registrar_ligacao(lead_id, ref, ha_dias=guard.JANELA_SILENCIO_DIAS + 1)
    with SessionLocal() as s:
        assert guard.ligacao_recente(tel, session=s) is False


def test_nutricao_nao_dispara_marco_pra_quem_levou_ligacao(lead_na_nutricao):
    """ADIA, não perde: etapa e `proximo_toque_em` ficam intactos."""
    from app.db.models import CadenciaEstado, Lead
    from app.db.session import SessionLocal

    lead_id, est_id, ref, tel, stage_base = lead_na_nutricao
    _registrar_ligacao(lead_id, ref)

    sender = FakeSender()
    with SessionLocal() as s:
        devido_antes = s.get(CadenciaEstado, est_id).proximo_toque_em
        res = nutricao.tick(session=s, sender=sender)

    assert res.bloqueados.get("ligacao_recente", 0) >= 1
    assert not any(c["telefone"] == tel for c in sender.chamadas), \
        "mandou mensagem automática pra quem acabou de receber ligação"
    with SessionLocal() as s:
        assert s.get(Lead, lead_id).stage_id == stage_base   # não avançou etapa
        assert s.get(CadenciaEstado, est_id).proximo_toque_em == devido_antes


def test_marco_volta_a_sair_quando_a_janela_de_silencio_fecha(lead_na_nutricao):
    from app.db.models import Lead
    from app.db.session import SessionLocal

    lead_id, _, ref, tel, stage_base = lead_na_nutricao
    _registrar_ligacao(lead_id, ref, ha_dias=guard.JANELA_SILENCIO_DIAS + 1)

    sender = FakeSender()
    with SessionLocal() as s:
        nutricao.tick(session=s, sender=sender)
    assert any(c["telefone"] == tel for c in sender.chamadas), \
        "o marco tinha que voltar a sair depois da janela — ele ATRASA, não some"
    with SessionLocal() as s:
        assert s.get(Lead, lead_id).stage_id != stage_base  # avançou pro 30 dias


def test_adotado_por_ligacao_nao_volta_pra_regua(lead_na_nutricao):
    """PRECEDÊNCIA: contato humano vale mais que régua. A fase de ENTRADA da
    nutrição puxava o lead adotado de volta pra 'Base' — desfazendo a adoção e
    tirando o lead do funil da corretora."""
    from app.db.models import Lead
    from app.db.session import SessionLocal

    lead_id, _, ref, _, stage_base = lead_na_nutricao
    with SessionLocal() as s:
        out = lr.adotar_lead(s, lead_id=lead_id, corretora_id=1, autor="corretor")
        s.commit()
        assert out["ok"] is True
        stage_novo = s.get(Lead, lead_id).stage_id
    assert stage_novo != stage_base

    with SessionLocal() as s:
        nutricao.tick(session=s, sender=FakeSender())
    with SessionLocal() as s:
        lead = s.get(Lead, lead_id)
        assert lead.stage_id == stage_novo, "a nutrição puxou o lead adotado de volta"
        assert lead.owner_id == 1


# ── lado B: quem recebeu mensagem automática não é oferecido pra ligar ───────


def _fila_tem(lead_id: int) -> bool:
    from app.db.session import SessionLocal

    with SessionLocal() as s:
        itens = lr.fila_ligacao(s, limite=200, dono="corretor", corretora_id=1)
        s.rollback()
    return lead_id in {i["lead_id"] for i in itens}


def test_fila_traz_o_lead_de_nutricao_sem_toque_recente(lead_na_nutricao):
    """Confirma o que a fila REALMENTE contém: lead da régua de nutrição, sem
    dono. Se um dia isso mudar, este teste avisa."""
    lead_id, _, _, _, _ = lead_na_nutricao
    assert _fila_tem(lead_id) is True


def test_fila_nao_oferece_quem_recebeu_mensagem_automatica(lead_na_nutricao):
    from app.db.session import SessionLocal

    lead_id, est_id, _, _, _ = lead_na_nutricao
    assert _fila_tem(lead_id) is True

    with SessionLocal() as s:
        s.execute(
            sql_text(
                "INSERT INTO cadencia_eventos (tenant_id, cadencia_id, tipo, canal, at) "
                "VALUES (1, :c, 'mensagem', 'whatsapp', NOW())"
            ),
            {"c": est_id},
        )
        s.commit()
    assert _fila_tem(lead_id) is False, "ligação + mensagem automática no mesmo dia"

    # fora da janela de silêncio o lead volta pra fila
    with SessionLocal() as s:
        s.execute(
            sql_text(
                "UPDATE cadencia_eventos SET at = NOW() - (:d || ' days')::interval "
                "WHERE cadencia_id = :c"
            ),
            {"d": str(guard.JANELA_SILENCIO_DIAS + 1), "c": est_id},
        )
        s.commit()
    assert _fila_tem(lead_id) is True


def test_fila_nao_oferece_quem_o_carrossel_acabou_de_disparar(lead_na_nutricao):
    """O carrossel hoje fica fora da fila (os leads dele têm a Corretor como dona),
    mas a trava já cobre o dia em que isso mudar."""
    from app.db.session import SessionLocal

    lead_id, _, _, _, _ = lead_na_nutricao
    with SessionLocal() as s:
        s.execute(
            sql_text(
                "INSERT INTO lead_events (tenant_id, lead_id, tipo, payload, at) "
                "VALUES (1, :l, 'carrossel_disparo', '{}'::jsonb, NOW())"
            ),
            {"l": lead_id},
        )
        s.commit()
    assert _fila_tem(lead_id) is False


def test_total_da_fila_usa_o_mesmo_filtro(lead_na_nutricao):
    """O estoque não pode contar quem a fila não oferece — número que não bate
    com a tela é número em que ninguém confia."""
    from app.db.session import SessionLocal

    lead_id, est_id, _, _, _ = lead_na_nutricao
    with SessionLocal() as s:
        antes = lr.total_fila_ligacao(s)
        s.execute(
            sql_text(
                "INSERT INTO cadencia_eventos (tenant_id, cadencia_id, tipo, canal, at) "
                "VALUES (1, :c, 'mensagem', 'whatsapp', NOW())"
            ),
            {"c": est_id},
        )
        s.commit()
        assert lr.total_fila_ligacao(s) == antes - 1
