"""A cadência não fala por cima de gente.

A trava existia só no carrossel (Dona 31/07). Só que o carrossel ENTREGA o lead
pra corretora, e daí em diante quem toca é a cadência — que não tinha a trava.
Em agosto/2026 isso rendeu 120 disparos em cima de 97 pessoas que estavam
conversando com uma corretora. A Eduarda Ribeiro falou de restrição no nome em
21/08, levou quatro templates até 01/09 e pediu "Parar mensagens".
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text as _t

from app.db.session import SessionLocal


def _limpar(s, suf: str) -> None:
    s.execute(_t("DELETE FROM mensagens_whatsapp WHERE contato_telefone LIKE :p"),
              {"p": f"%{suf}"})
    s.commit()


def _msg(s, *, tel: str, quando, from_corretora: bool, tipo: str, origem: str | None) -> None:
    import json
    import uuid
    s.execute(_t("""
        INSERT INTO mensagens_whatsapp
            (id, evolution_message_id, instance, corretora_id, remote_jid,
             contato_telefone, from_corretora, tipo_mensagem, conteudo_texto,
             conteudo_metadata, timestamp_evolution, recebido_em, tenant_id)
        VALUES (:id, :w, 'teste', 3, :jid, :tel, :fc, :tipo, 'x',
                CAST(:meta AS jsonb), :ts, now(), 1)"""),
        {"id": str(uuid.uuid4()), "w": str(uuid.uuid4()), "jid": f"{tel}@s.whatsapp.net",
         "tel": tel, "fc": from_corretora, "tipo": tipo,
         "meta": json.dumps({"origem": origem}) if origem else None, "ts": quando})
    s.commit()


def _sufs_travados(dias: int = 7) -> set[str]:
    """Reproduz a consulta da trava, que é o que decide o bloqueio."""
    with SessionLocal() as s:
        rows = s.execute(_t("""
            SELECT DISTINCT right(regexp_replace(m.contato_telefone,'\\D','','g'),8)
              FROM mensagens_whatsapp m
             WHERE m.from_corretora
               AND m.contato_telefone IS NOT NULL
               AND coalesce(m.tipo_mensagem,'') <> 'template'
               AND coalesce(m.conteudo_metadata->>'origem','') = ''
               AND m.timestamp_evolution > now() - make_interval(days => :dias)"""),
            {"dias": dias}).fetchall()
    return {r[0] for r in rows if r[0]}


@pytest.fixture()
def tel():
    t = "+5500000000000"
    with SessionLocal() as s:
        _limpar(s, t[-8:])
    yield t
    with SessionLocal() as s:
        _limpar(s, t[-8:])


def test_conversa_humana_recente_trava(tel):
    """O caso da Eduarda: humano falou há 4 dias → não pode disparar."""
    with SessionLocal() as s:
        _msg(s, tel=tel, quando=datetime.now(timezone.utc) - timedelta(days=4),
             from_corretora=True, tipo="text", origem=None)
    assert tel[-8:] in _sufs_travados()


def test_conversa_humana_antiga_nao_trava(tel):
    """Passados os 7 dias, a cadência volta a poder tocar — reengajar lead frio
    é justamente o trabalho dela."""
    with SessionLocal() as s:
        _msg(s, tel=tel, quando=datetime.now(timezone.utc) - timedelta(days=9),
             from_corretora=True, tipo="text", origem=None)
    assert tel[-8:] not in _sufs_travados()


def test_template_nao_conta_como_conversa_humana(tel):
    """Disparo anterior não pode travar o próximo — senão a cadência se
    autobloqueia depois do primeiro toque e nunca mais fala."""
    with SessionLocal() as s:
        _msg(s, tel=tel, quando=datetime.now(timezone.utc) - timedelta(days=1),
             from_corretora=True, tipo="template", origem=None)
    assert tel[-8:] not in _sufs_travados()


def test_resposta_de_robo_nao_conta_como_humano(tel):
    """Mensagem com carimbo de robô (origem preenchida) não é gente digitando."""
    with SessionLocal() as s:
        _msg(s, tel=tel, quando=datetime.now(timezone.utc) - timedelta(days=1),
             from_corretora=True, tipo="text", origem="pessoa")
    assert tel[-8:] not in _sufs_travados()


def test_mensagem_do_lead_nao_trava(tel):
    """O lead falando não é 'conversa humana da casa em andamento' — se ele
    escreveu e ninguém respondeu, a cadência é justamente quem deve agir."""
    with SessionLocal() as s:
        _msg(s, tel=tel, quando=datetime.now(timezone.utc) - timedelta(days=1),
             from_corretora=False, tipo="text", origem=None)
    assert tel[-8:] not in _sufs_travados()
