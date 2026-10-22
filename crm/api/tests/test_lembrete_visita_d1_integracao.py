"""Lembrete D-1 contra o banco de verdade — os SQLs, não as strings.

O arquivo irmão (`test_lembrete_visita_d1.py`) aperta o parser e as travas
lendo o código. Este aqui roda o ciclo inteiro no Postgres de teste: visita de
amanhã → lote numerado → envio → resposta da corretora na thread da linha Assistente
→ status no banco → métrica da semana. É o que pega SQL que compila e não faz
o que promete.

Nenhuma mensagem sai: `_enviar` é substituído por um espião em todos os testes
(e, mesmo que não fosse, `enviar_cloud_assistente` tem guarda de pytest própria).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text as sql_text

from app.db.session import SessionLocal
from app.services import lembrete_visita_d1 as d1

SP = ZoneInfo("America/Sao_Paulo")

# Corretora 1 = Pessoa5, semeada pelo conftest com telefone_normalizado.
CORRETORA = 1


def _hoje_sp() -> date:
    """O dia em São Paulo — o mesmo relógio do serviço. `date.today()` segue o
    fuso do servidor (UTC): entre 21h e meia-noite de SP ele já é "amanhã", e a
    visita criada "pra hoje" caía num dia que o corte das 9h não olha."""
    return datetime.now(SP).date()


AMANHA = _hoje_sp() + timedelta(days=1)


@pytest.fixture(autouse=True)
def _limpa():
    with SessionLocal() as s:
        s.execute(sql_text("DELETE FROM visita_lembrete_d1"))
        s.execute(sql_text("DELETE FROM visitas"))
        s.execute(sql_text("DELETE FROM mensagens_whatsapp WHERE instance = 'cloud_assistente'"))
        s.commit()
    yield
    with SessionLocal() as s:
        s.execute(sql_text("DELETE FROM visita_lembrete_d1"))
        s.execute(sql_text("DELETE FROM visitas"))
        s.execute(sql_text("DELETE FROM mensagens_whatsapp WHERE instance = 'cloud_assistente'"))
        s.commit()


@pytest.fixture
def tel_corretora() -> str:
    with SessionLocal() as s:
        return s.execute(
            sql_text("SELECT telefone_normalizado FROM corretoras WHERE id = :i"),
            {"i": CORRETORA},
        ).scalar_one()


def _cria_visita(nome: str, hora: int, *, dia: date = AMANHA,
                 status: str = "agendada", bairro: str = "Centro") -> int:
    quando = datetime.combine(dia, datetime.min.time(), tzinfo=SP).replace(hour=hora)
    with SessionLocal() as s:
        vid = s.execute(
            sql_text(
                """INSERT INTO visitas (corretora_id, lead_nome, data_hora, bairro,
                                        telefone_lead, status, tenant_id)
                   VALUES (:c, :n, :d, :b, :tel, :st, 1) RETURNING id"""
            ),
            # telefone_lead preenchido DE PROPÓSITO: é o número que jamais pode
            # ser usado como destino. Se um dia virar destino, o teste
            # `test_destino_e_sempre_o_da_corretora` cai.
            {"c": CORRETORA, "n": nome, "d": quando, "b": bairro,
             "tel": "+5500000000000", "st": status},
        ).scalar_one()
        s.commit()
    return int(vid)


def _resposta_da_corretora(tel: str, texto: str, *, quando: datetime | None = None) -> None:
    """Insere uma fala INBOUND da corretora na thread da linha Assistente."""
    ts = quando or datetime.now(SP)
    with SessionLocal() as s:
        s.execute(
            sql_text(
                """INSERT INTO mensagens_whatsapp
                       (evolution_message_id, instance, remote_jid, contato_telefone,
                        from_corretora, tipo_mensagem, conteudo_texto,
                        timestamp_evolution, tenant_id)
                   VALUES (:w, 'cloud_assistente', :jid, :tel, FALSE, 'text', :txt, :ts, 1)"""
            ),
            {"w": f"wamid-{ts.timestamp()}-{texto[:8]}", "jid": f"{tel}@s.whatsapp.net",
             "tel": tel, "txt": texto, "ts": ts},
        )
        s.commit()


def _lembretes() -> list[dict]:
    with SessionLocal() as s:
        return [dict(r) for r in s.execute(
            sql_text("SELECT * FROM visita_lembrete_d1 ORDER BY ordem_no_lote")
        ).mappings().all()]


def _status(vid: int) -> str:
    with SessionLocal() as s:
        return s.execute(sql_text("SELECT status FROM visitas WHERE id = :i"),
                         {"i": vid}).scalar_one()


@pytest.fixture
def espiao(monkeypatch):
    """Substitui o envio e guarda o que teria saído (destino + texto)."""
    enviados: list[tuple[str, str]] = []

    def _fake(lote, texto):
        enviados.append((lote.telefone, texto))
        return {"ok": True, "sent": True, "canal": "texto_livre"}

    monkeypatch.setattr(d1, "_enviar", _fake)
    return enviados


# ─── o ciclo ────────────────────────────────────────────────────────────────
def test_seleciona_visita_de_amanha_e_numera(tel_corretora):
    _cria_visita("Poliana", 14)
    _cria_visita("João Carlos", 16)
    _cria_visita("Não é de amanhã", 10, dia=AMANHA + timedelta(days=3))
    _cria_visita("Já cancelou", 11, status="cancelada")

    lotes = d1.selecionar_lotes(dia=AMANHA)
    assert len(lotes) == 1
    lote = lotes[0]
    assert [v.lead_nome for v in lote.visitas] == ["Poliana", "João Carlos"]
    assert [v.ordem for v in lote.visitas] == [1, 2]
    assert lote.telefone == tel_corretora


def test_pendente_confirmacao_tambem_recebe_vespera():
    """Visita marcada e ainda não confirmada é a que mais fura."""
    _cria_visita("Meio confirmada", 15, status="pendente_confirmacao")
    lotes = d1.selecionar_lotes(dia=AMANHA)
    assert len(lotes) == 1
    assert lotes[0].visitas[0].lead_nome == "Meio confirmada"


def test_destino_e_sempre_o_da_corretora(espiao, tel_corretora):
    """A TRAVA, medida no banco: o telefone do lead existe na visita e mesmo
    assim o destino é o da corretora."""
    _cria_visita("Poliana", 14)
    d1.enviar_lembretes(dia=AMANHA)
    assert len(espiao) == 1
    destino, texto = espiao[0]
    assert destino == tel_corretora
    assert "+5500000000000" not in destino
    assert "Poliana" in texto


def test_nao_manda_duas_vezes(espiao):
    """Segundo tick às 18h10 não vira segunda mensagem."""
    _cria_visita("Poliana", 14)
    d1.enviar_lembretes(dia=AMANHA)
    r2 = d1.enviar_lembretes(dia=AMANHA)
    assert len(espiao) == 1
    assert r2["lotes"] == 0


def test_dry_run_nao_grava_nem_envia(espiao):
    _cria_visita("Poliana", 14)
    r = d1.enviar_lembretes(dia=AMANHA, dry_run=True)
    assert r["enviados"] == 1
    assert espiao == []
    assert _lembretes() == []


def test_envio_falho_nao_marca_como_enviado(monkeypatch):
    """Linha reservada fica sem `enviado_em` — e sem `enviado_em` ela não conta
    como silêncio da corretora na métrica."""
    monkeypatch.setattr(d1, "_enviar",
                        lambda lote, texto: {"ok": False, "error": "no_token"})
    _cria_visita("Poliana", 14)
    r = d1.enviar_lembretes(dia=AMANHA)
    assert r["enviados"] == 0
    assert r["pulados"]["no_token"] == 1
    linhas = _lembretes()
    assert len(linhas) == 1
    assert linhas[0]["enviado_em"] is None


# ─── a resposta voltando ────────────────────────────────────────────────────
def test_resposta_fecha_o_lembrete_e_cancela_a_visita(espiao, tel_corretora):
    v1 = _cria_visita("Poliana", 14)
    v2 = _cria_visita("João Carlos", 16)
    d1.enviar_lembretes(dia=AMANHA)

    _resposta_da_corretora(tel_corretora, "1 ok, 2 cancelou")
    r = d1.ler_respostas()

    assert r["lidos"] == 2
    assert r["por_desfecho"] == {"confirmada": 1, "cancelada": 1}
    linhas = {l["visita_id"]: l for l in _lembretes()}
    assert linhas[v1]["desfecho"] == "confirmada"
    assert linhas[v2]["desfecho"] == "cancelada"
    # Confirmada NÃO mexe na visita; cancelada mexe.
    assert _status(v1) == "agendada"
    assert _status(v2) == "cancelada"


def test_remarcada_nao_mexe_no_status_da_visita(espiao, tel_corretora):
    """'remarcada' não existe em ck_visitas_status. Fica registrada aqui e vira
    pendência de nova data — quem remarca de verdade é gente."""
    vid = _cria_visita("Poliana", 14)
    d1.enviar_lembretes(dia=AMANHA)
    _resposta_da_corretora(tel_corretora, "remarcou pra semana que vem")

    d1.ler_respostas()
    assert _lembretes()[0]["desfecho"] == "remarcada"
    assert _status(vid) == "agendada"
    assert any(i["motivo"] == "remarcada" for i in d1.pendencias()["itens"])


def test_conversa_solta_nao_fecha_lembrete(espiao, tel_corretora):
    _cria_visita("Poliana", 14)
    d1.enviar_lembretes(dia=AMANHA)
    _resposta_da_corretora(tel_corretora, "oi Assistente, tudo bem?")

    r = d1.ler_respostas()
    assert r["lidos"] == 0
    assert _lembretes()[0]["desfecho"] is None


def test_fala_anterior_ao_envio_nao_conta(espiao, tel_corretora):
    """Mensagem que a corretora mandou ANTES do lembrete não responde ele."""
    _cria_visita("Poliana", 14)
    _resposta_da_corretora(tel_corretora, "1 ok",
                           quando=datetime.now(SP) - timedelta(hours=5))
    d1.enviar_lembretes(dia=AMANHA)

    assert d1.ler_respostas()["lidos"] == 0


def test_desfecho_nao_e_reescrito(espiao, tel_corretora):
    """Depois de fechado, uma segunda leitura não sobrescreve o desfecho."""
    _cria_visita("Poliana", 14)
    d1.enviar_lembretes(dia=AMANHA)
    _resposta_da_corretora(tel_corretora, "1 cancelou")
    d1.ler_respostas()
    _resposta_da_corretora(tel_corretora, "1 ok")

    assert d1.ler_respostas()["lidos"] == 0
    assert _lembretes()[0]["desfecho"] == "cancelada"


# ─── o silêncio ─────────────────────────────────────────────────────────────
def test_silencio_vira_pendencia_visivel(espiao, monkeypatch):
    """O coração da coisa: quem não respondeu até a manhã da visita aparece."""
    avisos: list[str] = []
    monkeypatch.setattr(d1, "cobrar_silencio", d1.cobrar_silencio)  # não mocka

    class _Tel:
        def send_to_dona(self, msg):
            avisos.append(msg)
            return "ok"

    import app.tools.telegram as tg

    monkeypatch.setattr(tg, "TelegramTool", _Tel)

    hoje = _hoje_sp()
    _cria_visita("Poliana", 14, dia=hoje)
    d1.enviar_lembretes(dia=hoje)

    r = d1.cobrar_silencio(agora=datetime.now(SP).replace(hour=9, minute=30))
    assert r["sem_resposta"] == 1
    assert r["avisou_dona"] is True
    assert "Poliana" in avisos[0]

    pend = d1.pendencias()
    assert any(i["motivo"] == "sem_resposta" for i in pend["itens"])

    # Idempotente: o carimbo é o dedup, não avisa a Dona duas vezes.
    r2 = d1.cobrar_silencio(agora=datetime.now(SP).replace(hour=10))
    assert r2["sem_resposta"] == 0


def test_quem_respondeu_nao_e_cobrado(espiao, tel_corretora):
    hoje = _hoje_sp()
    _cria_visita("Poliana", 14, dia=hoje)
    d1.enviar_lembretes(dia=hoje)
    _resposta_da_corretora(tel_corretora, "1 ok")
    d1.ler_respostas()

    r = d1.cobrar_silencio(agora=datetime.now(SP).replace(hour=9, minute=30))
    assert r["sem_resposta"] == 0


# ─── a métrica ──────────────────────────────────────────────────────────────
def test_metrica_da_semana_conta_cada_desfecho(espiao, tel_corretora):
    hoje = _hoje_sp()
    _cria_visita("Poliana", 9, dia=hoje)
    _cria_visita("João", 11, dia=hoje)
    _cria_visita("Marcos", 15, dia=hoje)
    d1.enviar_lembretes(dia=hoje)
    _resposta_da_corretora(tel_corretora, "1 ok, 2 cancelou, 3 remarcou")
    d1.ler_respostas()

    semanas = d1.metricas_semanais(semanas=2)
    atual = [s for s in semanas if s["lembretes"]][0]
    assert atual["lembretes"] == 3
    assert atual["confirmadas"] == 1
    assert atual["canceladas"] == 1
    assert atual["remarcadas"] == 1
    assert atual["sem_resposta"] == 0
    assert atual["taxa_resposta"] == 100.0
