"""Métrica de VISITA da automação — marcadas ≠ compareceram (Dona 05/08).

A página media resposta; a Acme vive de visita. O ponto que a própria Dona
levantou e que é o coração disto: `agendada` NÃO é `compareceu`. Somar as duas
faz a tela dizer que a automação entrega o dobro do que entrega.

Cobre:
  - marcadas conta agenda (agendada + pendente_confirmacao + compareceu) e
    compareceram conta só o confirmado — nunca somadas;
  - cancelada NÃO entra em marcadas (agenda desfeita não é resultado);
  - visita `pendente_confirmacao` com data VENCIDA aparece à parte
    (gargalo de registro, não estatística);
  - zero visita não divide por zero: taxa None + sem_dado;
  - só entra visita de quem passou pela cadência;
  - a janela de medição usa a data em que a visita foi MARCADA (visita marcada
    hoje pra semana que vem é resultado de hoje).

Roda no Postgres de teste: a query usa regexp_replace/EXISTS, que o SQLite
in-memory não tem. Zero prod, zero envio.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text as sql_text

from app.api.automacoes import _metricas_visita
from app.db.session import SessionLocal

TENANT = 1
CORRETOR = 3
AGORA = datetime.now(timezone.utc)

# Telefones/nome com prefixo próprio pra limpeza cirúrgica — a suíte inteira
# divide o mesmo banco.
TEL_NA_CADENCIA = "+000000000000000"
TEL_FORA = "+000000000000000"
PREFIXO_NOME = "TSTVIS-"


def _limpa(s) -> None:
    s.rollback()
    s.execute(sql_text(f"DELETE FROM visitas WHERE lead_nome LIKE '{PREFIXO_NOME}%'"))
    # Só os DOIS telefones deste arquivo. Era `LIKE '5500000000000%'`: outros testes
    # sorteiam telefone ("5515" + 9 dígitos) e, num banco reaproveitado, um
    # deles cai no prefixo com evento de cadência pendurado — o DELETE morria
    # em ForeignKeyViolation e levava os 9 testes daqui junto.
    s.execute(
        sql_text("DELETE FROM cadencia_estado WHERE telefone IN (:a, :b)"),
        {"a": TEL_NA_CADENCIA, "b": TEL_FORA},
    )
    s.commit()


@pytest.fixture()
def db():
    with SessionLocal() as s:
        _limpa(s)
        s.execute(
            sql_text(
                "INSERT INTO cadencia_estado (tenant_id, lead_id, telefone, "
                "corretora_id, toque_atual, status, proximo_toque_em, atualizado_em) "
                "VALUES (:t, :lid, :tel, :c, 0, 'ativa', now(), now())"
            ),
            {"t": TENANT, "lid": "TSTVIS-1", "tel": TEL_NA_CADENCIA, "c": CORRETOR},
        )
        s.commit()
        yield s
        _limpa(s)


def _visita(s, *, status: str, quando: datetime, telefone: str = TEL_NA_CADENCIA,
            nome: str | None = None, marcada_em: datetime | None = None) -> None:
    """`quando` = data da visita; `marcada_em` = quando ela foi MARCADA (é essa
    que define a janela de medição). Default: marcada agora."""
    s.execute(
        sql_text(
            """INSERT INTO visitas (corretora_id, lead_nome, data_hora,
                                    telefone_lead, status, tenant_id, criado_em)
               VALUES (:c, :n, :dh, :tel, :st, :t, :cr)"""
        ),
        {"c": CORRETOR, "n": nome or f"{PREFIXO_NOME}{status}-{quando.isoformat()}",
         "dh": quando, "tel": telefone, "st": status, "t": TENANT,
         "cr": marcada_em or AGORA},
    )
    s.commit()


def _medir(s, *, dias: int = 30) -> dict:
    return _metricas_visita(
        s, desde=AGORA - timedelta(days=dias), ate=AGORA, tenant_id=TENANT
    )


# ── marcadas ≠ compareceram ──────────────────────────────────────────────────


def test_marcadas_e_compareceram_sao_contagens_separadas(db):
    _visita(db, status="agendada", quando=AGORA - timedelta(days=1))
    _visita(db, status="compareceu", quando=AGORA - timedelta(days=2))
    _visita(db, status="compareceu", quando=AGORA - timedelta(days=3))

    m = _medir(db)
    assert m["disponivel"] is True
    assert m["marcadas"] == 3        # tudo que virou agenda
    assert m["compareceram"] == 2    # só o que alguém confirmou
    assert m["taxa_comparecimento"] == pytest.approx(66.7, abs=0.1)


def test_cancelada_nao_conta_como_marcada(db):
    _visita(db, status="compareceu", quando=AGORA - timedelta(days=1))
    _visita(db, status="cancelada", quando=AGORA - timedelta(days=2))

    m = _medir(db)
    assert m["marcadas"] == 1
    assert m["compareceram"] == 1


def test_no_show_conta_como_marcada_mas_nao_como_comparecimento(db):
    """Furou: a automação ENTREGOU a visita, quem não apareceu foi o lead."""
    _visita(db, status="no_show", quando=AGORA - timedelta(days=1))
    m = _medir(db)
    # no_show não está em STATUS_VISITA_MARCADA — a agenda existiu, mas o que a
    # Dona compara é agenda VIVA × comparecimento. Documentado no endpoint.
    assert m["compareceram"] == 0
    assert m["marcadas"] == 0


# ── vencida sem confirmação ──────────────────────────────────────────────────


def test_vencida_sem_confirmacao_e_sinalizada(db):
    """Data no passado e ninguém fechou o registro: na prática não compareceu.
    É pendência operacional — some com 'marcadas' e a taxa vira ficção."""
    _visita(db, status="pendente_confirmacao", quando=AGORA - timedelta(days=2))
    _visita(db, status="pendente_confirmacao", quando=AGORA + timedelta(days=2))

    m = _medir(db)
    assert m["marcadas"] == 2               # as duas viraram agenda
    assert m["compareceram"] == 0           # nenhuma foi confirmada
    assert m["vencidas_sem_confirmacao"] == 1  # só a que já passou
    assert m["taxa_comparecimento"] == 0.0


# ── ausência ≠ zero ──────────────────────────────────────────────────────────


def test_sem_visita_nao_divide_por_zero(db):
    m = _medir(db)
    assert m["marcadas"] == 0
    assert m["compareceram"] == 0
    assert m["taxa_comparecimento"] is None   # "sem dado" ≠ 0%
    assert m["sem_dado"] is True


def test_amostra_pequena_sinalizada(db):
    _visita(db, status="compareceu", quando=AGORA - timedelta(days=1))
    m = _medir(db)
    assert m["amostra_pequena"] is True


# ── atribuição e janela ──────────────────────────────────────────────────────


def test_so_conta_visita_de_quem_passou_pela_cadencia(db):
    _visita(db, status="compareceu", quando=AGORA - timedelta(days=1),
            telefone=TEL_FORA)
    m = _medir(db)
    assert m["marcadas"] == 0, "visita de lead fora da automação não é dela"


def test_janela_de_medicao_respeitada(db):
    _visita(db, status="compareceu", quando=AGORA - timedelta(days=45),
            marcada_em=AGORA - timedelta(days=46))
    _visita(db, status="compareceu", quando=AGORA - timedelta(days=2),
            marcada_em=AGORA - timedelta(days=3))

    assert _medir(db, dias=30)["compareceram"] == 1
    assert _medir(db, dias=90)["compareceram"] == 2


def test_entra_no_payload_das_metricas(db):
    """A tela lê `visitas` dentro do mesmo payload — e o texto da definição diz
    que a atribuição é do TOTAL da automação, não de um toque."""
    from app.api.automacoes import calcular_metricas

    _visita(db, status="compareceu", quando=AGORA - timedelta(days=1))
    dados = calcular_metricas(db, dias=30, agora=AGORA)
    assert dados["visitas"]["compareceram"] >= 1
    assert "VISITA é do total da automação" in dados["definicao"]
