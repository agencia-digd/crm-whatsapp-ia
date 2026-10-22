"""Vigia do silêncio (porte da DEX, 02/10/2026).

Pronto quando (documento reunioes/2026-10-02-motores-dex-para-acme.md, item 3):
  - a consulta acha o lead de anúncio mudo e ignora quem recebeu mensagem
    (o replay de 60 dias contra produção está no relatório de entrega: 0 casos
    de anúncio; com todas as origens ela acha os casos reais de indicação);
  - o marcador de cooldown só é gravado depois de o aviso sair.

Banco: Postgres de teste (:5433). Semeia e apaga só o que é deste arquivo.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text as sql_text

from app.db.session import SessionLocal
from app.services import vigia_silencio as vs

AGORA = datetime(2026, 10, 2, 15, 0, tzinfo=timezone.utc)  # sexta, 12h em SP
NOME = "tst-vigia-silencio"
FONE_MUDO = "+5500000000000"
FONE_ATENDIDO = "+5500000000000"
FONE_NOVO = "+5500000000000"
FONE_INDICACAO = "+5500000000000"


def _limpar(s) -> None:
    s.execute(sql_text("DELETE FROM mensagens_whatsapp WHERE contato_nome = :n"), {"n": NOME})
    s.execute(sql_text("DELETE FROM leads WHERE nome LIKE :n"), {"n": NOME + "%"})
    s.commit()


def _lead(s, sufixo: str, fone: str, *, minutos: int, origem: str = "meta_leadgen") -> int:
    return s.execute(sql_text(
        """INSERT INTO leads (tenant_id, nome, telefone, origem, owner_id, created_at, updated_at)
           VALUES (1, :n, :t, :o, NULL, :c, :c) RETURNING id"""),
        {"n": f"{NOME}-{sufixo}", "t": "+" + fone, "o": origem,
         "c": AGORA - timedelta(minutes=minutos)}).scalar()


def _saida(s, fone: str, quando: datetime) -> None:
    s.execute(sql_text(
        """INSERT INTO mensagens_whatsapp (evolution_message_id, instance, remote_jid,
               contato_telefone, contato_nome, from_corretora, tipo_mensagem,
               conteudo_texto, timestamp_evolution, recebido_em)
           VALUES (:w, 'acme_corretor', :j, :t, :n, true, 'text', 'oi', :q, :q)"""),
        {"w": f"wamid.{NOME}.{fone}", "j": fone + "@s.whatsapp.net", "t": fone, "n": NOME,
         "q": quando})


@pytest.fixture()
def sessao():
    with SessionLocal() as s:
        _limpar(s)
        ids = {
            "mudo": _lead(s, "mudo", FONE_MUDO, minutos=45),
            "atendido": _lead(s, "atendido", FONE_ATENDIDO, minutos=45),
            "novo": _lead(s, "novo", FONE_NOVO, minutos=10),
            "indicacao": _lead(s, "indicacao", FONE_INDICACAO, minutos=45, origem="indicacao"),
        }
        _saida(s, FONE_ATENDIDO, AGORA - timedelta(minutes=44))
        s.commit()
        yield s, ids
        _limpar(s)


def _meus(leads, ids):
    return {l.id for l in leads} & set(ids.values())


def test_acha_so_o_lead_de_anuncio_mudo_ha_mais_de_30_min(sessao):
    s, ids = sessao
    achados = _meus(vs.procurar(s, AGORA), ids)
    assert achados == {ids["mudo"]}


def test_todas_as_origens_pega_a_indicacao_tambem(sessao):
    s, ids = sessao
    achados = _meus(vs.procurar(s, AGORA, origens=("meta_leadgen", "indicacao")), ids)
    assert achados == {ids["mudo"], ids["indicacao"]}


def test_replay_ignora_mensagem_que_saiu_depois_do_instante(sessao):
    """No passado (antes da saída do 'atendido'), ele também estava mudo."""
    s, ids = sessao
    _saida(s, FONE_MUDO, AGORA + timedelta(minutes=5))
    s.commit()
    assert _meus(vs.procurar(s, AGORA), ids) == {ids["mudo"]}
    assert _meus(vs.procurar(s, AGORA + timedelta(minutes=10)), ids) == set()


def test_lead_velho_demais_sai_da_vigia(sessao):
    s, ids = sessao
    assert _meus(vs.procurar(s, AGORA + timedelta(hours=vs.JANELA_H + 1)), ids) == set()


def test_cooldown_so_e_gravado_depois_do_aviso_sair(sessao, tmp_path):
    s, ids = sessao
    estado = str(tmp_path / "vigia.json")
    # aviso FALHA → nada gravado, e a próxima passada tenta de novo
    r = vs.rodar(s, agora=AGORA, enviar=lambda t: False, caminho_estado=estado)
    assert r["status"] == "falha_no_aviso"
    assert vs.ler_estado(estado) == {}
    # aviso SAI → grava
    textos: list[str] = []
    r = vs.rodar(s, agora=AGORA, enviar=lambda t: textos.append(t) or True,
                 caminho_estado=estado)
    assert r["status"] == "avisado" and ids["mudo"] in json.load(open(estado))["ids"]
    assert f"lead {ids['mudo']}" in textos[0]
    # mesma lista dentro de 2 h → cooldown, sem aviso
    r = vs.rodar(s, agora=AGORA + timedelta(minutes=15), enviar=lambda t: pytest.fail("avisou de novo"),
                 caminho_estado=estado)
    assert r["status"] == "cooldown"


def test_lead_novo_no_silencio_fura_o_cooldown(sessao, tmp_path):
    s, ids = sessao
    estado = str(tmp_path / "vigia.json")
    vs.gravar_estado([ids["mudo"]], AGORA.timestamp(), estado)
    # 25 min depois o "novo" (10 min de vida) passa dos 30 → avisa já
    avisos: list[str] = []
    r = vs.rodar(s, agora=AGORA + timedelta(minutes=25),
                 enviar=lambda t: avisos.append(t) or True, caminho_estado=estado)
    assert r["status"] == "avisado" and len(avisos) == 1


def test_fora_do_horario_nao_consulta_nem_avisa(tmp_path):
    noite = datetime(2026, 10, 2, 1, 0, tzinfo=timezone.utc)  # 22h em SP
    r = vs.rodar(None, agora=noite, enviar=lambda t: pytest.fail("avisou"),
                 caminho_estado=str(tmp_path / "x.json"))
    assert r == {"status": "fora_do_horario"}


def test_dry_run_do_telegram_nao_conta_como_aviso(monkeypatch):
    from app.tools import telegram

    monkeypatch.setattr(telegram.TelegramTool, "send_to_dona", lambda self, m: "ok (dry_run)")
    assert vs._enviar_padrao("x") is False
    monkeypatch.setattr(telegram.TelegramTool, "send_to_dona", lambda self, m: "ok")
    assert vs._enviar_padrao("x") is True
