"""Thread do chat: a conversa é do LEAD, não da linha que a transportou.

POR QUE EXISTE (02/09/2026 — lead Rebeca, +5500000000000, dona Pessoa5):
o funil de nutrição/remarketing dispara TODO toque pela linha da Pessoa12
(`app/services/nutricao.py` — spec Dona 10/07). A resposta do lead volta pela
linha da Pessoa12 e é gravada com `corretora_id=3`. Como `ler_thread_cloud` filtrava
`corretora_id = ANY([dona, casa])`, essas mensagens sumiam do painel: a Pessoa10 não
via (filtro) e a Pessoa12 não via (o card não é dela → 403). Duas mensagens quentes
("quando tiver alguma oportunidade boa pode mandar") ficaram invisíveis por um dia.

É o MESMO defeito de classe do BR-Jeison (28/07, "aparece no celular, some no
painel"), que foi corrigido só pra linha da Corretor.

Regra que estes testes travam: a thread é resolvida por TELEFONE, e o escopo de
privacidade é o gate de DONO do lead (em `/painel/conversa`), não o filtro de
linha. O que NÃO é conversa da operação Acme (bridge da Assistente com a Dona) fica
de fora.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text as sql_text

from app.api.cloud_corretora_a import ler_thread_cloud
from app.db.session import SessionLocal

LEAD = "+5500000000000"
SUF = "97357691"


def _insere(instance: str, corretora_id: int | None, texto: str, minutos: int,
            *, from_corretora: bool = False, tipo: str = "text") -> None:
    with SessionLocal() as s:
        s.execute(
            sql_text(
                """
                INSERT INTO mensagens_whatsapp
                    (evolution_message_id, instance, corretora_id, remote_jid,
                     contato_telefone, from_corretora, tipo_mensagem,
                     conteudo_texto, timestamp_evolution, tenant_id)
                VALUES (:mid, :inst, :cid, :jid, :tel, :from_c, :tipo, :txt, :ts, 1)
                """
            ),
            {
                "mid": f"test-thread-{instance}-{minutos}",
                "inst": instance,
                "cid": corretora_id,
                "jid": f"{SUF}@s.whatsapp.net",
                "tel": LEAD,
                "from_c": from_corretora,
                "tipo": tipo,
                "txt": texto,
                "ts": datetime.now(timezone.utc) - timedelta(minutes=minutos),
            },
        )
        s.commit()


@pytest.fixture(autouse=True)
def _limpa():
    def _wipe() -> None:
        with SessionLocal() as s:
            s.execute(
                sql_text("DELETE FROM mensagens_whatsapp WHERE contato_telefone = :t"),
                {"t": LEAD},
            )
            s.commit()
    _wipe()
    yield
    _wipe()


def test_thread_traz_toque_de_remarketing_da_linha_da_corretor():
    """Lead da Pessoa10: o toque de nutrição saiu pela Pessoa12 e a resposta voltou por lá."""
    _insere("acme_corretora_c", 1, "e ai como estão as coisas?", 300, from_corretora=True)
    _insere("acme_corretora_a", 3, "[reengajamento: tem imóvel que entra e sai do mercado]",
            120, from_corretora=True, tipo="template")
    _insere("acme_corretora_a", 3, "Quando tiver alguma oportunidade boa pode mandar", 60)

    thread = ler_thread_cloud(LEAD, corretora_id=1)

    textos = [m["text"] for m in thread]
    assert len(thread) == 3, f"thread perdeu mensagem de outra linha: {textos}"
    assert "Quando tiver alguma oportunidade boa pode mandar" in textos
    # template mostra o corpo limpo, sem o marcador [reengajamento: ...]
    assert "tem imóvel que entra e sai do mercado" in textos
    # ordem cronológica preservada
    assert textos[0] == "e ai como estão as coisas?"


def test_thread_da_corretor_tambem_ve_o_que_saiu_pela_linha_da_corretor():
    """Simétrico: lead transferido leva o histórico junto, qualquer que seja a linha."""
    _insere("acme_corretora_c", 1, "oi, sou a Pessoa10", 300, from_corretora=True)
    _insere("acme_corretora_a", 3, "oi, agora quem te atende sou eu", 60, from_corretora=True)

    thread = ler_thread_cloud(LEAD, corretora_id=3)

    assert [m["text"] for m in thread] == ["oi, sou a Pessoa10", "oi, agora quem te atende sou eu"]


def test_thread_ignora_conversa_que_nao_e_da_operacao_acme():
    """A bridge da Assistente com a Dona (cloud_assistente, sem corretora) nunca entra na thread."""
    _insere("acme_corretora_c", 1, "mensagem do lead", 120)
    _insere("cloud_assistente", None, "conversa interna Assistente↔Dona", 60)

    thread = ler_thread_cloud(LEAD, corretora_id=1)

    assert [m["text"] for m in thread] == ["mensagem do lead"]


def test_thread_inclui_disparo_do_recuperador():
    """`recuperador` não tem corretora_id, mas é disparo pra base da Acme."""
    _insere("recuperador", None, "oi, ainda procurando imóvel?", 120, from_corretora=True)
    _insere("acme_corretora_c", 1, "ainda sim", 60)

    thread = ler_thread_cloud(LEAD, corretora_id=1)

    assert len(thread) == 2
