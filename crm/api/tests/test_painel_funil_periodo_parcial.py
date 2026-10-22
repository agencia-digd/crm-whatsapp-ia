"""Funil do /visao-geral em período PARCIAL + alerta de tarefas atrasadas.

As duas regressões que motivaram o arquivo (encontradas em 04/08/2026 com o
painel mostrando agosto inteiro zerado):

1. `calcular_funil`, quando o período não fecha o mês (o caso normal: "mês
   atual" até hoje), descartava `vendas_mensais` e recalculava leads/atendidos
   a partir de `leads_snapshot.criado_em_kommo` — cache do Kommo, congelado
   desde o cutover de 27/07. Resultado: funil zerado com leads entrando, e o
   alerta falso "investiu R$X sem nenhum lead" em cima disso.

2. O alerta de tarefas atrasadas perguntava pra API do Kommo, onde ninguém
   mexe desde o cutover: acusava 132/66/57 tarefas de estoque parado. A fila
   viva é `tarefas_corretora`, do próprio painel.

DB de teste (Postgres isolado via conftest). Zero prod, zero envio.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text as sql_text

import app.api.painel as painel
from app.db.session import SessionLocal
from app.services import funil_local

TENANT = 1

# O "hoje" do NEGÓCIO é São Paulo, não o do servidor (UTC). Entre 21h e
# meia-noite os dois discordam — o servidor já virou o dia e o Brasil não — e as
# consultas do painel usam `NOW() AT TIME ZONE 'America/Sao_Paulo'`. Usar
# `date.today()` aqui fazia estes testes falharem só nessas 3 horas: verdes o
# dia inteiro, vermelhos à noite, sem bug nenhum no código.
SP = ZoneInfo("America/Sao_Paulo")


def hoje_sp() -> date:
    return datetime.now(SP).date()


def hoje_ao_meio_dia() -> datetime:
    """Meio-dia de HOJE em São Paulo.

    Os testes criavam o lead com `agora - 2h`, o que quebra nas duas primeiras
    horas do dia: à 00h11, "2 horas atrás" é ontem, e o lead nasce fora da
    janela que o teste mede. Ancorar no meio-dia mantém o lead dentro do dia
    qualquer que seja a hora em que a suíte rodar.
    """
    return datetime.combine(hoje_sp(), time(12, 0), tzinfo=SP)


def agora_sp() -> datetime:
    """Agora no fuso do NEGÓCIO.

    Os leads criados nos testes têm que nascer no mesmo dia que `hoje_sp()`
    enxerga. Usar `now(utc)` fazia o lead nascer em 05/08 (UTC) enquanto o
    painel media 04/08 (SP) — verde o dia todo, vermelho entre 21h e meia-noite.
    """
    return datetime.now(SP)
CORRETOR, CORRETOR = 1, 2

STAGE_NOVO = 4
STAGE_GANHA = funil_local.STAGE_GANHA  # 12


def _limpa(s) -> None:
    s.rollback()  # teste que estourou deixa a transação abortada
    s.execute(sql_text("DELETE FROM tarefas_corretora WHERE lead_nome LIKE 'TST-%'"))
    s.execute(sql_text("DELETE FROM leads WHERE nome LIKE 'TST-%'"))
    s.execute(sql_text(
        "DELETE FROM mensagens_whatsapp WHERE contato_telefone LIKE '5500000000000%'"))
    s.commit()


@pytest.fixture()
def db():
    with SessionLocal() as s:
        _limpa(s)
        # `leads.stage_id` tem FK pra pipeline_stages.
        for sid, nome in ((STAGE_NOVO, "Novo"), (STAGE_GANHA, "Venda ganha")):
            s.execute(sql_text(
                "INSERT INTO pipeline_stages (id, tenant_id, nome) VALUES (:i, :t, :n) "
                "ON CONFLICT (id) DO NOTHING"), {"i": sid, "t": TENANT, "n": nome})
        # Corretoras precisam existir: o mapa de tarefas parte de `corretoras`.
        for cid, nome in ((CORRETOR, "Pessoa5"), (CORRETOR, "Pessoa1")):
            s.execute(sql_text(
                "INSERT INTO corretoras (id, nome, telefone, ativo) "
                "VALUES (:i, :n, :t, TRUE) ON CONFLICT (id) DO UPDATE SET ativo = TRUE"),
                {"i": cid, "n": nome, "t": f"+5500000000000{cid:03d}"})
        s.commit()
        yield s
        _limpa(s)


def _cria_lead(s, *, nome: str, owner: int, tel: str, criado: datetime,
               stage: int = STAGE_NOVO, origem: str = "meta_leadgen",
               valor: float | None = None) -> None:
    s.execute(
        sql_text(
            """INSERT INTO leads (tenant_id, nome, telefone, owner_id, stage_id,
                                  origem, valor, created_at)
               VALUES (:t, :n, :tel, :o, :st, :org, :v, :c)"""
        ),
        {"t": TENANT, "n": nome, "tel": tel, "o": owner, "st": stage,
         "org": origem, "v": valor, "c": criado},
    )
    s.commit()


def _resposta_do_lead(s, tel: str, quando: datetime) -> None:
    """Mensagem RECEBIDA — é o que define `atendido` (conversa nos dois sentidos)."""
    s.execute(
        sql_text(
            """INSERT INTO mensagens_whatsapp
                 (id, evolution_message_id, instance, remote_jid, contato_telefone,
                  from_corretora, tipo_mensagem, conteudo_texto,
                  timestamp_evolution, recebido_em)
               VALUES (gen_random_uuid(), :w, 'tst', :jid, :tel,
                       false, 'text', 'oi', :ts, :ts)"""
        ),
        {"w": f"wamid.TST.{tel}.{quando.timestamp()}",
         "jid": f"{tel}@s.whatsapp.net", "tel": tel, "ts": quando},
    )
    s.commit()


# ── 1. Funil em período parcial ───────────────────────────────────────────────


def test_periodo_parcial_conta_lead_do_painel_v2(db):
    """A regressão: lead criado no V2 tem que aparecer no funil de hoje.

    Antes, com a fonte em `leads_snapshot` (Kommo, congelado), este lead era
    invisível e o funil vinha zerado.
    """
    hoje = hoje_sp()
    ontem_dt = hoje_ao_meio_dia()
    _cria_lead(db, nome="TST-Corretor", owner=CORRETOR, tel="+000000000000000", criado=ontem_dt)

    funil = painel.calcular_funil(hoje.replace(day=1), hoje)
    corretor = next(c for c in funil["corretoras"] if c["corretora_id"] == CORRETOR)
    assert corretor["leads"] >= 1, "lead do V2 sumiu do funil parcial"


def test_atendido_e_quem_respondeu_nao_quem_mudou_de_etapa(db):
    """No V2 ninguém move o card na mão — etapa não serve de proxy de atendimento.
    Quem respondeu conta; quem não respondeu, não."""
    hoje = hoje_sp()
    base = hoje_ao_meio_dia()
    _cria_lead(db, nome="TST-Respondeu", owner=CORRETOR, tel="+000000000000000", criado=base)
    _cria_lead(db, nome="TST-Calado", owner=CORRETOR, tel="+000000000000000", criado=base)
    _resposta_do_lead(db, "+000000000000000", base + timedelta(minutes=5))

    funil = painel.calcular_funil(hoje.replace(day=1), hoje)
    corretor = next(c for c in funil["corretoras"] if c["corretora_id"] == CORRETOR)
    assert corretor["leads"] >= 2
    assert corretor["atendidos_total"] >= 1
    assert corretor["atendidos_total"] < corretor["leads"], "quem não respondeu virou atendido"


def test_base_importada_nao_infla_o_funil(db):
    """`leads` também guarda backfill de remarketing (5k+ linhas). Só captação conta."""
    hoje = hoje_sp()
    base = hoje_ao_meio_dia()
    _cria_lead(db, nome="TST-Backfill", owner=CORRETOR, tel="+000000000000000",
               criado=base, origem="kommo_remarketing_4990")

    ini = datetime.combine(hoje, datetime.min.time(), tzinfo=painel.SP_TZ)
    linhas = funil_local.agregar_janela(db, ini, ini + timedelta(days=1))
    nomes = {int(r["owner_id"]): int(r["qtde_leads"]) for r in linhas}
    assert nomes.get(CORRETOR, 0) == 0, "lead de base importada entrou como captação"


def test_janela_e_fim_exclusivo(db):
    """Lead de amanhã não conta hoje — a janela é [ini, fim)."""
    amanha = agora_sp() + timedelta(days=1)
    _cria_lead(db, nome="TST-Futuro", owner=CORRETOR, tel="+000000000000000", criado=amanha)

    hoje = hoje_sp()
    ini = datetime.combine(hoje, datetime.min.time(), tzinfo=painel.SP_TZ)
    linhas = funil_local.agregar_janela(db, ini, ini + timedelta(days=1))
    assert not any(int(r["owner_id"]) == CORRETOR and int(r["qtde_leads"]) > 0
                   for r in linhas)


# ── 2. Tarefas atrasadas ──────────────────────────────────────────────────────


def _tarefa(s, *, corretora: int, data_tarefa: date, status: str = "aberta") -> None:
    s.execute(
        sql_text(
            """INSERT INTO tarefas_corretora
                 (lead_id, lead_nome, corretora_id, data_tarefa, texto, status,
                  autor, criado_em, tenant_id)
               VALUES ('tst', 'TST-lead', :c, :d, 'acompanhar', :st, 'teste',
                       NOW(), :t)"""
        ),
        {"c": corretora, "d": data_tarefa, "st": status, "t": TENANT},
    )
    s.commit()


@pytest.fixture(autouse=True)
def _sem_cache():
    """O contador tem cache de 10min — senão um teste enxerga o outro."""
    painel._TAREFAS_CACHE.clear()
    yield
    painel._TAREFAS_CACHE.clear()


def test_conta_tarefa_vencida_e_aberta(db):
    hoje = hoje_sp()
    _tarefa(db, corretora=CORRETOR, data_tarefa=hoje - timedelta(days=3))
    _tarefa(db, corretora=CORRETOR, data_tarefa=hoje - timedelta(days=1))
    assert painel._coletar_tarefas_atrasadas_por_corretora().get(CORRETOR) == 2


def test_nao_conta_concluida_nem_a_vencer(db):
    hoje = hoje_sp()
    _tarefa(db, corretora=CORRETOR, data_tarefa=hoje - timedelta(days=5), status="concluida")
    _tarefa(db, corretora=CORRETOR, data_tarefa=hoje)                      # vence hoje
    _tarefa(db, corretora=CORRETOR, data_tarefa=hoje + timedelta(days=2))  # futura
    assert painel._coletar_tarefas_atrasadas_por_corretora().get(CORRETOR) == 0


def test_corretora_ativa_sem_tarefa_aparece_zerada(db):
    """Zero explícito ≠ ausente. Sem isso "não tem tarefa" e "sumiu do cálculo"
    ficam indistinguíveis — foi assim que o alerta do Kommo morreu calado."""
    mapa = painel._coletar_tarefas_atrasadas_por_corretora()
    assert mapa.get(CORRETOR) == 0 and mapa.get(CORRETOR) == 0


def test_alerta_nao_menciona_mais_o_kommo(db):
    """Kommo saiu do circuito em 27/07 — o alerta não pode mandar a Dona olhar lá."""
    hoje = hoje_sp()
    for _ in range(painel.TAREFAS_ATRASADAS_LIMITE + 1):
        _tarefa(db, corretora=CORRETOR, data_tarefa=hoje - timedelta(days=2))

    alertas = painel.detectar_alertas(
        {"leads": 10, "vendas": 1, "vgv": 1000.0},
        {"spend": 100.0, "cpl": 10.0},
        {"taxa_comparecimento": None, "items": [], "total": 0},
    )
    tarefa_alerta = [a for a in alertas if a["categoria"] == "tarefas_atrasadas"]
    assert len(tarefa_alerta) == 1
    assert "kommo" not in tarefa_alerta[0]["msg"].lower()
    assert tarefa_alerta[0]["valor"] == painel.TAREFAS_ATRASADAS_LIMITE + 1


# ── 3. Vazamento (meta_leads_log ↔ funil) ─────────────────────────────────────


@pytest.fixture()
def meta_log(tmp_path, monkeypatch):
    """SQLite temporário no lugar do log do bridge (/opt/acme-kommo/data)."""
    import sqlite3

    caminho = tmp_path / "kommo.db"
    con = sqlite3.connect(caminho)
    con.execute("CREATE TABLE meta_leads_log "
                "(leadgen_id TEXT, received_at TEXT, status TEXT, detail TEXT)")
    con.commit()
    monkeypatch.setattr(painel, "META_LEADS_DB", str(caminho))

    def _add(leadgen_id: str, quando: str, status: str, detail: str) -> None:
        con.execute("INSERT INTO meta_leads_log VALUES (?,?,?,?)",
                    (leadgen_id, quando, status, detail))
        con.commit()

    yield _add
    con.close()


def test_kommo_off_conta_como_captado(db, meta_log):
    """Pós-cutover o bridge grava `kommo_off` no lugar de `ok`. O lead foi
    captado do mesmo jeito — só não foi pro Kommo. Antes o card dizia
    "0 captados" com 30 leads entrando pela Meta."""
    hoje = hoje_sp()
    meta_log("lg1", f"{hoje.isoformat()}T10:00:00", "kommo_off",
             "KOMMO_CRIAR_LEAD=false — lead entregue só ao painel novo")

    v = painel.calcular_vazamento_real(hoje, hoje)
    assert v["captados_meta"] == 1


def test_lead_sem_telefone_no_log_nao_vira_perdido(db, meta_log):
    """`detail` virou texto livre no cutover: sem telefone não dá pra cruzar.
    Chamar de "perdido" pintava 100% de vazamento num mês em que o funil
    recebeu tudo — o bucket é `sem_telefone_no_log`, e a taxa ignora ele."""
    hoje = hoje_sp()
    meta_log("lg1", f"{hoje.isoformat()}T10:00:00", "kommo_off", "texto livre")

    v = painel.calcular_vazamento_real(hoje, hoje)
    assert v["sem_telefone_no_log"] == 1
    assert v["realmente_perdidos"] == 0
    assert v["taxa_vazamento_real_pct"] is None


def test_lead_com_telefone_bate_com_o_funil_v2(db, meta_log):
    """O cruzamento tem que olhar `leads` (V2), não só `leads_snapshot` (Kommo)."""
    import json as _json

    hoje = hoje_sp()
    tel = "+000000000000000"
    _cria_lead(db, nome="TST-Meta", owner=CORRETOR, tel=f"+{tel}",
               criado=hoje_ao_meio_dia())
    meta_log("lg1", f"{hoje.isoformat()}T10:00:00", "kommo_off",
             _json.dumps({"name": "TST-Meta", "phone": tel}))

    v = painel.calcular_vazamento_real(hoje, hoje)
    assert v["novos_no_funil"] == 1
    assert v["realmente_perdidos"] == 0
