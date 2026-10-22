"""VGV conta pela data da VENDA, não pela data em que o lead nasceu.

A regressão que motivou o arquivo (05/09/2026 — agosto e setembro inteiros
aparecendo com VGV zero no painel, no Histórico e na Assistente):

`SQL_AGREGA` calculava venda/VGV dentro de `lead_mes`, que é filtrado por
`l.created_at` na janela e por `l.origem` em ORIGENS_CAPTACAO. Três efeitos,
cada um sozinho suficiente pra zerar o mês:

1. O ciclo lead→venda da Acme é de 60-90 dias, então a venda quase NUNCA
   cai no mês em que o lead nasceu. A venda real de agosto (R$ 305.212,03,
   da Pessoa12) veio de leads criados em MARÇO — invisível pra janela de agosto.
2. O valor somado era `leads.valor`, NULL em 14 dos 36 leads em "Venda ganha".
   O valor de verdade é o registrado pela corretora em `vendas`.
3. Lead com `origem` vazia (o caso dos leads dessa venda) era descartado pelo
   filtro de captação antes de qualquer conta de venda.

A correção: venda e VGV saem de `vendas` (registro humano do negócio, com
`data_venda` e `corretora_id`), independente de quando o lead nasceu e de onde
ele veio. Isso também acerta a CONTAGEM: João e Sarah são dois cards no CRM e
um negócio só — `vendas` tem uma linha, os cards teriam virado 2 vendas.

DB de teste (Postgres isolado via conftest). Zero prod, zero envio.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text as sql_text

from app.db.session import SessionLocal
from app.services import funil_local

TENANT = 1
CORRETOR, CORRETOR, CORRETOR = 1, 2, 3

STAGE_NOVO = 4
STAGE_GANHA = funil_local.STAGE_GANHA  # 12

SP = ZoneInfo("America/Sao_Paulo")


def _limpa(s) -> None:
    s.rollback()
    s.execute(sql_text("DELETE FROM vendas WHERE cliente_nome LIKE 'TST-%'"))
    s.execute(sql_text("DELETE FROM leads WHERE nome LIKE 'TST-%'"))
    s.commit()


@pytest.fixture()
def db():
    with SessionLocal() as s:
        _limpa(s)
        for sid, nome in ((STAGE_NOVO, "Novo"), (STAGE_GANHA, "Venda ganha")):
            s.execute(sql_text(
                "INSERT INTO pipeline_stages (id, tenant_id, nome) VALUES (:i, :t, :n) "
                "ON CONFLICT (id) DO NOTHING"), {"i": sid, "t": TENANT, "n": nome})
        for cid, nome in ((CORRETOR, "Pessoa5"), (CORRETOR, "Pessoa1"), (CORRETOR, "Pessoa3")):
            s.execute(sql_text(
                "INSERT INTO corretoras (id, nome, telefone, ativo) "
                "VALUES (:i, :n, :t, TRUE) ON CONFLICT (id) DO UPDATE SET ativo = TRUE"),
                {"i": cid, "n": nome, "t": f"+5500000000000{cid:03d}"})
        s.commit()
        yield s
        _limpa(s)


def _mes(ano: int, mes: int) -> tuple[datetime, datetime]:
    ini = datetime(ano, mes, 1, tzinfo=SP)
    fim = (datetime(ano + 1, 1, 1, tzinfo=SP) if mes == 12
           else datetime(ano, mes + 1, 1, tzinfo=SP))
    return ini, fim


def _cria_lead(s, *, nome: str, owner: int, criado: datetime,
               stage: int = STAGE_NOVO, origem: str | None = "meta_leadgen",
               valor: float | None = None) -> int:
    return s.execute(
        sql_text(
            """INSERT INTO leads (tenant_id, nome, telefone, owner_id, stage_id,
                                  origem, valor, created_at)
               VALUES (:t, :n, :tel, :o, :st, :org, :v, :c) RETURNING id"""
        ),
        {"t": TENANT, "n": nome, "tel": None, "o": owner, "st": stage,
         "org": origem, "v": valor, "c": criado},
    ).scalar_one()


def _registra_venda(s, *, cliente: str, corretora: int, quando: date,
                    valor: float) -> None:
    s.execute(
        sql_text(
            """INSERT INTO vendas (tenant_id, corretora_id, cliente_nome, valor,
                                   data_venda, registrado_em)
               VALUES (:t, :c, :n, :v, :d, NOW())"""
        ),
        {"t": TENANT, "c": corretora, "n": cliente, "v": valor, "d": quando},
    )
    s.commit()


def _linha(rows: list[dict], owner: int) -> dict:
    return next((r for r in rows if r["owner_id"] == owner),
                {"qtde_vendas": 0, "vgv": 0})


# ── o caso real de agosto/2026 ────────────────────────────────────────────────


def test_venda_de_lead_antigo_conta_no_mes_da_venda(db):
    """Lead de março, venda em agosto: o VGV é de AGOSTO.

    Era o buraco principal — a janela filtrava por `created_at` e a venda
    caía num mês em que ninguém a procurava.
    """
    _cria_lead(db, nome="TST-João", owner=CORRETOR, criado=datetime(2026, 3, 28, tzinfo=SP),
               stage=STAGE_GANHA, origem=None)
    _registra_venda(db, cliente="TST-João e Sarah", corretora=CORRETOR,
                    quando=date(2026, 8, 31), valor=305212.03)

    ini, fim = _mes(2026, 8)
    corretor = _linha(funil_local.agregar_janela(db, ini, fim), CORRETOR)
    assert corretor["qtde_vendas"] == 1
    assert float(corretor["vgv"]) == pytest.approx(305212.03)


def test_venda_nao_vaza_pro_mes_em_que_o_lead_nasceu(db):
    """O espelho do teste acima: março não pode receber a venda de agosto."""
    _cria_lead(db, nome="TST-João", owner=CORRETOR, criado=datetime(2026, 3, 28, tzinfo=SP),
               stage=STAGE_GANHA, origem=None)
    _registra_venda(db, cliente="TST-João e Sarah", corretora=CORRETOR,
                    quando=date(2026, 8, 31), valor=305212.03)

    ini, fim = _mes(2026, 3)
    corretor = _linha(funil_local.agregar_janela(db, ini, fim), CORRETOR)
    assert corretor["qtde_vendas"] == 0
    assert float(corretor["vgv"]) == 0


def test_dois_cards_um_negocio_contam_uma_venda_so(db):
    """João e Sarah são dois cards no CRM e UMA venda.

    Contar pelo card (stage_id = 12) dobrava o número de vendas do mês.
    """
    for nome in ("TST-João", "TST-Sarah"):
        _cria_lead(db, nome=nome, owner=CORRETOR, criado=datetime(2026, 3, 28, tzinfo=SP),
                   stage=STAGE_GANHA, origem=None)
    _registra_venda(db, cliente="TST-João e Sarah", corretora=CORRETOR,
                    quando=date(2026, 8, 31), valor=305212.03)

    ini, fim = _mes(2026, 8)
    corretor = _linha(funil_local.agregar_janela(db, ini, fim), CORRETOR)
    assert corretor["qtde_vendas"] == 1, "card virou venda; a fonte do negócio é `vendas`"


def test_venda_de_lead_sem_origem_de_captacao_conta(db):
    """`so_captacao` filtra o FUNIL, nunca a venda.

    Os leads da venda de agosto estavam com origem vazia — o filtro os
    descartava antes de qualquer conta de dinheiro.
    """
    _cria_lead(db, nome="TST-Indicação", owner=CORRETOR,
               criado=datetime(2026, 8, 2, tzinfo=SP), stage=STAGE_GANHA, origem=None)
    _registra_venda(db, cliente="TST-Indicação", corretora=CORRETOR,
                    quando=date(2026, 8, 20), valor=250000)

    ini, fim = _mes(2026, 8)
    rows = funil_local.agregar_janela(db, ini, fim, so_captacao=True)
    corretor = _linha(rows, CORRETOR)
    assert float(corretor["vgv"]) == pytest.approx(250000)
    assert corretor["qtde_leads"] == 0, "lead sem origem de captação segue fora do funil"


def test_corretora_que_so_vendeu_aparece_na_linha(db):
    """Vendeu no mês sem lead novo nenhum: a corretora não pode sumir.

    O agregado agrupava por `lead_mes.owner_id`; sem lead na janela, a linha
    inteira (e o VGV junto) desaparecia do resultado.
    """
    _registra_venda(db, cliente="TST-Antigo", corretora=CORRETOR,
                    quando=date(2026, 8, 15), valor=400000)

    ini, fim = _mes(2026, 8)
    corretor = _linha(funil_local.agregar_janela(db, ini, fim), CORRETOR)
    assert corretor["qtde_vendas"] == 1
    assert float(corretor["vgv"]) == pytest.approx(400000)


def test_venda_no_ultimo_dia_do_mes_nao_escapa(db):
    """Borda da janela: `data_venda` é DATE e `fim` é exclusivo.

    31/08 tem que cair em agosto, não em setembro — o erro clássico de
    comparar date com timestamptz sem converter pro fuso do negócio.
    """
    _registra_venda(db, cliente="TST-Ultimo dia", corretora=CORRETOR,
                    quando=date(2026, 8, 31), valor=100000)

    ago = _linha(funil_local.agregar_janela(db, *_mes(2026, 8)), CORRETOR)
    set_ = _linha(funil_local.agregar_janela(db, *_mes(2026, 9)), CORRETOR)
    assert float(ago["vgv"]) == pytest.approx(100000)
    assert float(set_["vgv"]) == 0


def test_funil_segue_contando_pela_criacao_do_lead(db):
    """A mudança é só na venda — lead/atendido/agendado seguem por `created_at`.

    Sem esta trava, "consertar o VGV" viraria licença pra mexer no funil todo.
    """
    _cria_lead(db, nome="TST-Novo de agosto", owner=CORRETOR,
               criado=datetime(2026, 8, 10, tzinfo=SP))
    _cria_lead(db, nome="TST-Novo de julho", owner=CORRETOR,
               criado=datetime(2026, 7, 10, tzinfo=SP))

    corretor = _linha(funil_local.agregar_janela(db, *_mes(2026, 8)), CORRETOR)
    assert corretor["qtde_leads"] == 1
