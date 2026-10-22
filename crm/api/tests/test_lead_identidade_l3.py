"""L3 — identidade canônica do lead (telefone, dedupe, shadow, roleta).

Cobre os achados da auditoria de 26/07:
  - telefone canônico simétrico (12 vs 13 dígitos) e ambíguo sem auto-reparo
  - dedupe do intake sem o fallback frouxo `candidatos[0]` e com DDD igual
  - shadow do Kommo casando por telefone SEM sobrescrever etapa/dono
  - shadow NÃO arrastando lead do funil remarketing pro principal
  - roleta validando a corretora contra o banco (owner fantasma)

Arquivo NOVO de propósito: `tests/test_lead_intake.py` está no lote L8.
"""
from __future__ import annotations

import pytest
from sqlalchemy import BigInteger, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from app.db.models import Base, Corretora, Lead, LeadEvent, PipelineStage
from app.services.phone import (
    canonical_phone,
    canonical_phone_info,
    same_ddd,
    same_phone,
)


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(element, compiler, **kw):  # noqa: ANN001
    return "JSON"


@compiles(BigInteger, "sqlite")
def _compile_bigint_sqlite(element, compiler, **kw):  # noqa: ANN001
    return "INTEGER"


# ── 1. Telefone canônico ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "entrada,esperado",
    [
        # 13 dígitos: já canônico
        ("+5500000000000", "+5500000000000"),
        ("+5500000000000", "+5500000000000"),
        # 12 dígitos com assinante começando em 9 = celular que perdeu o 9
        ("+5500000000000", "+5500000000000"),
        # fixo legítimo (assinante começa com 3) — NÃO ganha o 9.
        # +5500000000000 é a própria linha 0000 da Acme.
        ("+5500000000000", "+5500000000000"),
        # legado Kommo com 0 na frente do DDD
        ("+011991649726", "+5500000000000"),
    ],
)
def test_canonical_phone_formas(entrada: str, esperado: str) -> None:
    assert canonical_phone(entrada) == esperado


def test_canonical_phone_simetrico_12_vs_13() -> None:
    """O mesmo celular gravado com 12 e com 13 dígitos reduz à MESMA string."""
    assert canonical_phone("+5500000000000") == canonical_phone("+5500000000000")
    assert same_phone("+5500000000000", "+5500000000000")


@pytest.mark.parametrize("ambiguo", ["+5500000000000", "+5500000000000"])
def test_canonical_phone_ambiguo_nao_decide(ambiguo: str) -> None:
    """12 dígitos começando com 7/8: pode ser fixo antigo ou celular sem o 9.
    Não auto-repara e não vira canônico — vai pra triagem humana."""
    canon, eh_ambiguo = canonical_phone_info(ambiguo)
    assert canon is None
    assert eh_ambiguo is True
    # e nunca casa com ninguém
    assert same_phone(ambiguo, "+5500000000000") is False


def test_canonical_phone_lixo() -> None:
    for lixo in (None, "", "pendente:marcos-gomes-385k", "+5500000000000"):
        canon, ambiguo = canonical_phone_info(lixo)
        assert canon is None, lixo
        assert ambiguo is False, lixo


def test_same_ddd_mata_colisao_sufixo8() -> None:
    """Sufixo-8 igual, DDD diferente: NÃO é a mesma pessoa."""
    a, b = "+5500000000000", "+5500000000000"
    assert a[-8:] == b[-8:]
    assert same_ddd(a, b) is False
    assert same_phone(a, b) is False


# ── Fixtures de espinha ──────────────────────────────────────────────────────


@pytest.fixture()
def db_session():
    """DB isolado em SQLite in-memory (mesmo padrão de
    test_lead_spine_manual_preservado — a suíte NUNCA toca produção)."""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(
        engine,
        tables=[
            Corretora.__table__,
            Lead.__table__,
            LeadEvent.__table__,
            PipelineStage.__table__,
        ],
    )
    factory = sessionmaker(bind=engine)
    with factory() as s:
        s.add_all(
            [
                Corretora(id=1, nome="Pessoa5", telefone="+5500000000000",
                          ativo=True, papel="corretora"),
                Corretora(id=2, nome="Pessoa1", telefone="+5500000000000",
                          ativo=True, papel="corretora"),
                Corretora(id=3, nome="Pessoa3", telefone="+5500000000000",
                          ativo=True, papel="corretora"),
            ]
        )
        s.commit()
        yield s


@pytest.fixture()
def stages(db_session):
    """Etapas mínimas: 2 do funil principal + 1 do remarketing."""
    criadas = {}
    for nome, funil in (
        ("Novo", "principal"),
        ("Em atendimento", "principal"),
        ("Base", "remarketing"),
    ):
        st = PipelineStage(tenant_id=1, nome=nome, funil=funil)
        db_session.add(st)
        db_session.flush()
        criadas[nome] = st
    db_session.commit()
    return criadas


# ── 2. Dedupe do intake ──────────────────────────────────────────────────────


def test_encontrar_lead_nao_funde_por_sufixo(db_session, stages) -> None:
    """O fallback `candidatos[0]` fundia lead novo no lead errado quando o
    sufixo-8 batia por acaso entre DDDs. Agora: sem match exato → None."""
    from app.services.lead_spine import encontrar_lead

    outro_ddd = Lead(
        tenant_id=1, external_ref="ddd18", nome="Outro",
        telefone="+5500000000000", stage_id=stages["Novo"].id,
    )
    db_session.add(outro_ddd)
    db_session.commit()

    achado = encontrar_lead(db_session, 1, telefone="+5500000000000")
    assert achado is None


def test_encontrar_lead_casa_12_com_13_digitos(db_session, stages) -> None:
    """Lead gravado com 12 dígitos é encontrado pela forma de 13 (simetria)."""
    from app.services.lead_spine import encontrar_lead

    antigo = Lead(
        tenant_id=1, external_ref="len12", nome="Sem o 9",
        telefone="+5500000000000", stage_id=stages["Novo"].id,
    )
    db_session.add(antigo)
    db_session.commit()

    achado = encontrar_lead(db_session, 1, telefone="+5500000000000")
    assert achado is not None and achado.id == antigo.id


def test_encontrar_lead_por_external_ref_tem_prioridade(db_session, stages) -> None:
    from app.services.lead_spine import encontrar_lead

    lead = Lead(
        tenant_id=1, external_ref="ref-meta-1", nome="Ref",
        telefone="+5500000000000", stage_id=stages["Novo"].id,
    )
    db_session.add(lead)
    db_session.commit()

    achado = encontrar_lead(db_session, 1, external_ref="ref-meta-1", telefone=None)
    assert achado is not None and achado.id == lead.id


# ── 3. Shadow não sobrescreve etapa/dono ─────────────────────────────────────


def test_shadow_match_por_telefone_preserva_stage_e_owner(
    db_session, stages, monkeypatch
) -> None:
    """Regressão do bug real (leads 6474/6476 "Fulano"): o intake da
    Meta criou o lead, o shadow do Kommo chega com OUTRO external_ref e o
    mesmo telefone. Não pode duplicar NEM reverter etapa/dono."""
    from app.config import settings
    from app.services import lead_spine

    monkeypatch.setattr(settings, "lead_spine_shadow", True)

    lead = Lead(
        tenant_id=1, external_ref="000000000000000", nome="Fulano",
        telefone="+5500000000000", owner_id=2,
        stage_id=stages["Em atendimento"].id,
    )
    db_session.add(lead)
    db_session.commit()
    lead_id, stage_antes = lead.id, lead.stage_id

    devolvido = lead_spine.upsert_lead_shadow(
        {
            "lead_id": "25913164",  # ref do KOMMO, diferente do da Meta
            "nome": "Fulano",
            "telefone": "+5500000000000",
            "etapa_nome": "Novo",   # Kommo tentando puxar de volta pra Novo
            "corretora_id": 3,      # e trocar a dona
        },
        tenant_id=1,
        session_factory=lambda: db_session,
    )

    assert devolvido == lead_id, "shadow duplicou o lead do intake"
    # upsert_lead_shadow fecha a sessão no finally — re-consulta em vez de
    # refresh (a instância anterior fica detached).
    atual = db_session.query(Lead).filter(Lead.id == lead_id).one()
    assert atual.stage_id == stage_antes, "shadow reverteu a etapa"
    assert atual.owner_id == 2, "shadow trocou a dona"
    assert db_session.query(Lead).filter(Lead.tenant_id == 1,
                                         Lead.telefone == "+5500000000000").count() == 1

    # sem stage_change espúrio (ele alimentaria o auto-enroll da cadência)
    tipos = [
        t for (t,) in db_session.query(LeadEvent.tipo)
        .filter(LeadEvent.lead_id == lead_id).all()
    ]
    assert "stage_change" not in tipos
    assert "lead_ref_vinculado" in tipos


def test_shadow_match_telefone_e_idempotente(db_session, stages, monkeypatch) -> None:
    """Inspector passa 2x/dia — não pode empilhar lead_ref_vinculado."""
    from app.config import settings
    from app.services import lead_spine

    monkeypatch.setattr(settings, "lead_spine_shadow", True)

    lead = Lead(
        tenant_id=1, external_ref="meta-idem", nome="Idem",
        telefone="+5500000000000", owner_id=2, stage_id=stages["Novo"].id,
    )
    db_session.add(lead)
    db_session.commit()

    dados = {"lead_id": "kommo-idem", "telefone": "+5500000000000",
             "etapa_nome": "Em atendimento"}
    for _ in range(3):
        lead_spine.upsert_lead_shadow(
            dados, tenant_id=1, session_factory=lambda: db_session
        )

    n = (
        db_session.query(LeadEvent)
        .filter(LeadEvent.lead_id == lead.id,
                LeadEvent.tipo == "lead_ref_vinculado")
        .count()
    )
    assert n == 1


def test_shadow_nao_arrasta_lead_de_remarketing(db_session, stages, monkeypatch) -> None:
    """Lead na base 0000 (funil remarketing) fica FORA do match por telefone —
    senão o sync o puxaria pro funil principal (caso real: lead 23 "Lê")."""
    from app.config import settings
    from app.services import lead_spine

    monkeypatch.setattr(settings, "lead_spine_shadow", True)

    base = Lead(
        tenant_id=1, external_ref="base-0000", nome="Lê",
        telefone="+5500000000000", owner_id=3, stage_id=stages["Base"].id,
    )
    db_session.add(base)
    db_session.commit()
    base_id, stage_base = base.id, base.stage_id

    novo_id = lead_spine.upsert_lead_shadow(
        {"lead_id": "23420108", "telefone": "+5500000000000",
         "etapa_nome": "Em atendimento"},
        tenant_id=1,
        session_factory=lambda: db_session,
    )

    assert novo_id != base_id, "shadow casou com lead de remarketing"
    atual = db_session.query(Lead).filter(Lead.id == base_id).one()
    assert atual.stage_id == stage_base, "lead de remarketing foi arrastado"


# ── 4. Roleta ────────────────────────────────────────────────────────────────


def test_roleta_rejeita_corretora_inexistente(db_session, monkeypatch) -> None:
    """Typo em ROLETA_CORRETORAS não pode gravar owner fantasma (leads.owner_id
    não tem FK). Sem corretora válida → None, lead visível pro admin."""
    from app.config import settings
    from app.services.lead_intake import _proxima_corretora_roleta

    monkeypatch.setattr(settings, "roleta_corretoras", [9999])
    assert _proxima_corretora_roleta(db_session, 1) is None


def test_roleta_ignora_corretora_inativa(db_session, monkeypatch) -> None:
    from app.config import settings
    from app.services.lead_intake import corretoras_roleta_validas

    inativa = db_session.query(Corretora).filter(Corretora.id == 2).first()
    assert inativa is not None
    inativa.ativo = False
    db_session.commit()

    monkeypatch.setattr(settings, "roleta_corretoras", [1, 2])
    assert corretoras_roleta_validas(db_session) == (1,)


def test_roleta_devolve_apenas_validas_na_ordem(db_session, monkeypatch) -> None:
    from app.config import settings
    from app.services.lead_intake import corretoras_roleta_validas

    monkeypatch.setattr(settings, "roleta_corretoras", [2, 9999, 1])
    assert corretoras_roleta_validas(db_session) == (2, 1)
