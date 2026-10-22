"""Respondeu (25) deixou de ser buraco negro.

Medido em 15/09/2026: 113 de 226 leads que responderam ao carrossel da Corretor não
foram atendidos por ninguém E nunca mais receberam disparo — o `_SQL_VOLTAR` só
devolvia pra Fila quem estava em Disparado. Ficavam invisíveis dos dois lados.
Foram limpos à mão; sem esta volta a etapa enche de novo em duas semanas.

O que estes testes protegem, em ordem:
1. Quem respondeu, descansou e ficou no vácuo VOLTA pra Fila.
2. Quem teve atendimento humano depois de responder NÃO volta (robô não fala
   por cima de gente — Dona 31/07).
3. Opt-out não volta (as duas fontes).
4. Transferido pra corretora não volta.
5. O descanso de Respondeu é MAIOR que o de Disparado.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text as _t

from app.db.session import SessionLocal
from app.services import carrossel_corretor as cb

TENANT = 1
_PREFIXO = "5500000000000"     # faixa só destes testes


def _agora() -> datetime:
    return datetime.now(timezone.utc)


def _criar_lead(s, *, tel: str, stage: int, owner: int = cb.CORRETORA_CORRETOR) -> int:
    lid = s.execute(
        _t("""INSERT INTO leads (tenant_id, nome, telefone, owner_id, stage_id,
                                 created_at, updated_at)
              VALUES (:t, 'Teste Respondeu', :tel, :o, :st, now(), now())
           RETURNING id"""),
        {"t": TENANT, "tel": tel, "o": owner, "st": stage},
    ).scalar()
    s.commit()
    return int(lid)


def _evento(s, lead_id: int, tipo: str, dias_atras: float) -> None:
    s.execute(
        _t("""INSERT INTO lead_events (tenant_id, lead_id, tipo, payload, at)
              VALUES (:t, :i, :tp, CAST(:p AS jsonb), :at)"""),
        {"t": TENANT, "i": lead_id, "tp": tipo, "p": json.dumps({"variante": "x"}),
         "at": _agora() - timedelta(days=dias_atras)},
    )
    s.commit()


def _msg_saida(s, *, tel: str, dias_atras: float, instance: str,
               tipo: str = "conversation", origem: str | None = None) -> None:
    s.execute(
        _t("""INSERT INTO mensagens_whatsapp
                (id, evolution_message_id, instance, corretora_id, remote_jid,
                 contato_telefone, from_corretora, tipo_mensagem, conteudo_texto,
                 conteudo_metadata, timestamp_evolution, recebido_em, tenant_id)
              VALUES (:id, :w, :inst, NULL, :jid, :tel, TRUE, :tipo, 'oi, tudo bem?',
                      CAST(:meta AS jsonb), :ts, now(), :t)"""),
        {"id": str(uuid.uuid4()), "w": str(uuid.uuid4()), "inst": instance,
         "jid": f"{tel}@s.whatsapp.net", "tel": tel, "tipo": tipo,
         "meta": json.dumps({"origem": origem}) if origem else None,
         "ts": _agora() - timedelta(days=dias_atras), "t": TENANT},
    )
    s.commit()


def _stage(s, lead_id: int) -> int:
    return int(s.execute(_t("SELECT stage_id FROM leads WHERE id = :i"),
                         {"i": lead_id}).scalar())


@pytest.fixture()
def s():
    """Sessão com limpeza da faixa de telefone destes testes, antes e depois."""
    def _limpar(ses):
        ses.execute(_t("DELETE FROM lead_events WHERE lead_id IN "
                       "(SELECT id FROM leads WHERE telefone LIKE :p)"),
                    {"p": f"{_PREFIXO}%"})
        ses.execute(_t("DELETE FROM mensagens_whatsapp WHERE contato_telefone LIKE :p"),
                    {"p": f"{_PREFIXO}%"})
        ses.execute(_t("DELETE FROM disparos_reengajamento WHERE telefone LIKE :p"),
                    {"p": f"{_PREFIXO}%"})
        ses.execute(_t("DELETE FROM cadencia_estado WHERE telefone LIKE :p"),
                    {"p": f"{_PREFIXO}%"})
        ses.execute(_t("DELETE FROM leads WHERE telefone LIKE :p"), {"p": f"{_PREFIXO}%"})
        ses.commit()

    with SessionLocal() as ses:
        # O seed do conftest tem Fila (23) e Respondeu (25), mas não Disparado
        # (24) — e `leads.stage_id` tem FK pra pipeline_stages.
        ses.execute(_t("INSERT INTO pipeline_stages (id, tenant_id, nome, funil) "
                       "VALUES (:i, :t, 'Disparado', 'remarketing') "
                       "ON CONFLICT (id) DO NOTHING"),
                    {"i": cb.STAGE_DISPARADO, "t": TENANT})
        ses.commit()
        _limpar(ses)
        yield ses
        _limpar(ses)


# ── 1. O buraco negro fechou ────────────────────────────────────────────────

def test_respondeu_no_vacuo_volta_pra_fila(s):
    tel = _PREFIXO + "01"
    lid = _criar_lead(s, tel=tel, stage=cb.STAGE_RESPONDEU)
    _evento(s, lid, cb.EVENT_DISPARO, cb.DESCANSO_RESPONDEU_DIAS + 12)
    _evento(s, lid, cb.EVENT_RESPOSTA, cb.DESCANSO_RESPONDEU_DIAS + 10)

    assert cb._voltar_respondeu_pra_fila(s, TENANT) == 1
    assert _stage(s, lid) == cb.STAGE_FILA_PADRAO


def test_respondeu_recente_ainda_descansa(s):
    """Quem respondeu ontem não leva template hoje."""
    tel = _PREFIXO + "02"
    lid = _criar_lead(s, tel=tel, stage=cb.STAGE_RESPONDEU)
    _evento(s, lid, cb.EVENT_RESPOSTA, 1)

    assert cb._voltar_respondeu_pra_fila(s, TENANT) == 0
    assert _stage(s, lid) == cb.STAGE_RESPONDEU


def test_descanso_conta_da_resposta_e_nao_do_disparo(s):
    """O disparo é velho, mas a pessoa respondeu ontem: o marco é a RESPOSTA."""
    tel = _PREFIXO + "03"
    lid = _criar_lead(s, tel=tel, stage=cb.STAGE_RESPONDEU)
    _evento(s, lid, cb.EVENT_DISPARO, cb.DESCANSO_RESPONDEU_DIAS + 30)
    _evento(s, lid, cb.EVENT_RESPOSTA, 1)

    assert cb._voltar_respondeu_pra_fila(s, TENANT) == 0
    assert _stage(s, lid) == cb.STAGE_RESPONDEU


# ── 2. Robô não fala por cima de gente ──────────────────────────────────────

def test_atendimento_humano_depois_da_resposta_segura_o_lead(s):
    """Corretora falou com ele em outra linha: NÃO volta pra roda."""
    tel = _PREFIXO + "04"
    lid = _criar_lead(s, tel=tel, stage=cb.STAGE_RESPONDEU)
    _evento(s, lid, cb.EVENT_RESPOSTA, cb.DESCANSO_RESPONDEU_DIAS + 20)
    _msg_saida(s, tel=tel, dias_atras=cb.DESCANSO_RESPONDEU_DIAS + 5, instance="acme_corretor")

    assert cb._voltar_respondeu_pra_fila(s, TENANT) == 0
    assert _stage(s, lid) == cb.STAGE_RESPONDEU


def test_atendimento_ANTES_da_resposta_nao_segura(s):
    """Conversa velha, anterior à resposta, não é 'engatou em algo' — é passado."""
    tel = _PREFIXO + "05"
    lid = _criar_lead(s, tel=tel, stage=cb.STAGE_RESPONDEU)
    _msg_saida(s, tel=tel, dias_atras=cb.DESCANSO_RESPONDEU_DIAS + 40, instance="acme_corretor")
    _evento(s, lid, cb.EVENT_RESPOSTA, cb.DESCANSO_RESPONDEU_DIAS + 20)

    assert cb._voltar_respondeu_pra_fila(s, TENANT) == 1


def test_mensagem_do_robo_nao_conta_como_atendimento(s):
    """Template e origem automática são robô. Lê-los como 'gente falando' faz a
    automação se auto-bloquear — foi o achado de 15/09 no anti-ghosting."""
    for i, (tipo, origem) in enumerate(
        [("template", None), ("conversation", "pessoa"),
         ("conversation", "anti_ghosting"), ("conversation", "carrossel")]
    ):
        tel = f"{_PREFIXO}1{i}"
        lid = _criar_lead(s, tel=tel, stage=cb.STAGE_RESPONDEU)
        _evento(s, lid, cb.EVENT_RESPOSTA, cb.DESCANSO_RESPONDEU_DIAS + 20)
        _msg_saida(s, tel=tel, dias_atras=1, instance="acme_corretor", tipo=tipo, origem=origem)

    assert cb._voltar_respondeu_pra_fila(s, TENANT) == 4


def test_mensagem_na_propria_linha_da_corretor_nao_conta(s):
    """O 0000 é a linha do robô — o que sai de lá não é atendimento humano."""
    tel = _PREFIXO + "06"
    lid = _criar_lead(s, tel=tel, stage=cb.STAGE_RESPONDEU)
    _evento(s, lid, cb.EVENT_RESPOSTA, cb.DESCANSO_RESPONDEU_DIAS + 20)
    _msg_saida(s, tel=tel, dias_atras=1, instance=cb.INSTANCE_CORRETOR)

    assert cb._voltar_respondeu_pra_fila(s, TENANT) == 1


# ── 3. Opt-out nunca volta ──────────────────────────────────────────────────

def test_opt_out_em_disparos_reengajamento_nao_volta(s):
    tel = _PREFIXO + "07"
    lid = _criar_lead(s, tel=tel, stage=cb.STAGE_RESPONDEU)
    _evento(s, lid, cb.EVENT_RESPOSTA, cb.DESCANSO_RESPONDEU_DIAS + 20)
    s.execute(_t("""INSERT INTO disparos_reengajamento
                      (tenant_id, corretora_id, telefone, template, enviado_por,
                       enviado_em, opt_out, opt_out_em)
                    VALUES (:t, :corretor, :tel, 'carrossel', 'carrossel', now(), TRUE, now())"""),
              {"t": TENANT, "corretor": cb.CORRETORA_CORRETOR, "tel": tel})
    s.commit()

    assert cb._voltar_respondeu_pra_fila(s, TENANT) == 0
    assert _stage(s, lid) == cb.STAGE_RESPONDEU


def test_opt_out_na_cadencia_nao_volta(s):
    tel = _PREFIXO + "08"
    lid = _criar_lead(s, tel=tel, stage=cb.STAGE_RESPONDEU)
    _evento(s, lid, cb.EVENT_RESPOSTA, cb.DESCANSO_RESPONDEU_DIAS + 20)
    s.execute(_t("INSERT INTO cadencia_estado (tenant_id, telefone, status) "
                 "VALUES (:t, :tel, 'opt_out')"),
              {"t": TENANT, "tel": tel})
    s.commit()

    assert cb._voltar_respondeu_pra_fila(s, TENANT) == 0
    assert _stage(s, lid) == cb.STAGE_RESPONDEU


# ── 4. Quem saiu da mão da Corretor não é mais da roda ───────────────────────────

def test_transferido_pra_corretora_nao_volta(s):
    """Handoff troca o dono do lead. Dono ≠ Corretor, fora da roda."""
    tel = _PREFIXO + "09"
    lid = _criar_lead(s, tel=tel, stage=cb.STAGE_RESPONDEU, owner=3)
    _evento(s, lid, cb.EVENT_RESPOSTA, cb.DESCANSO_RESPONDEU_DIAS + 20)

    assert cb._voltar_respondeu_pra_fila(s, TENANT) == 0
    assert _stage(s, lid) == cb.STAGE_RESPONDEU


def test_triado_como_nao_nao_volta(s):
    """A triagem de 15/09 já deu o veredito: 'NAO'/'LIXO'/'OPTOUT' não reentram
    na roda nem se o card voltar pra etapa Respondeu."""
    for i, classe in enumerate(("NAO", "LIXO", "OPTOUT")):
        tel = f"{_PREFIXO}2{i}"
        lid = _criar_lead(s, tel=tel, stage=cb.STAGE_RESPONDEU)
        _evento(s, lid, cb.EVENT_RESPOSTA, cb.DESCANSO_RESPONDEU_DIAS + 20)
        s.execute(
            _t("""INSERT INTO lead_events (tenant_id, lead_id, tipo, payload, at)
                  VALUES (:t, :i, 'triagem_respondeu', CAST(:p AS jsonb), now())"""),
            {"t": TENANT, "i": lid, "p": json.dumps({"classe": classe})},
        )
        s.commit()

    assert cb._voltar_respondeu_pra_fila(s, TENANT) == 0


def test_outra_etapa_nao_e_tocada(s):
    """Só Respondeu. Disparado continua com o `_SQL_VOLTAR` de sempre."""
    tel = _PREFIXO + "30"
    lid = _criar_lead(s, tel=tel, stage=cb.STAGE_DISPARADO)
    _evento(s, lid, cb.EVENT_DISPARO, cb.DESCANSO_RESPONDEU_DIAS + 20)

    assert cb._voltar_respondeu_pra_fila(s, TENANT) == 0
    assert _stage(s, lid) == cb.STAGE_DISPARADO


# ── 5. O desenho ────────────────────────────────────────────────────────────

def test_respondeu_descansa_mais_que_disparado():
    """Quem respondeu merece mais folga que quem só recebeu e ficou mudo."""
    assert cb.DESCANSO_RESPONDEU_DIAS > cb.DESCANSO_DIAS


def test_tick_chama_as_duas_voltas(monkeypatch):
    """A roda tem dois retornos — o tick não pode esquecer o novo."""
    from app.services import rampa

    chamou = []
    monkeypatch.setattr("app.services.config_operacional.carrossel_corretor_ativo", lambda: True)
    monkeypatch.setattr(cb, "travar", lambda ns, t: None)
    monkeypatch.setattr(cb, "soltar", lambda *a: None)
    monkeypatch.setattr(cb, "_voltar_pra_fila", lambda s, t: chamou.append("disparado") or 0)
    monkeypatch.setattr(cb, "_voltar_respondeu_pra_fila",
                        lambda s, t: chamou.append("respondeu") or 2)
    monkeypatch.setattr(
        rampa, "cap_de_hoje",
        lambda s, slug, cid, **k: rampa.Rampa(slug, 0, 0, 0, "RED", "qualidade RED"),
    )

    res = cb.tick(session=object(), sender=lambda *a, **k: {})
    assert chamou == ["disparado", "respondeu"]
    assert res.voltaram_de_respondeu == 2
