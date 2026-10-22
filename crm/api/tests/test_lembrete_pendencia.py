"""Testes da heurística do lembrete de pendência de registro da Assistente.

Cobre as funções PURAS de app/services/lembrete_pendencia (sem banco, sem
envio): eh_pergunta_registro, detectar_pendencia_thread e montar_mensagem.

Caso-âncora: incidente Pessoa10/Guilherme 22/07/2026 — a Assistente pediu hora da
visita + valor da venda numa cauda de 2 mensagens assistant e a Pessoa10 nunca
respondeu; a pergunta era a PENÚLTIMA msg da cauda (por isso a detecção varre
a cauda inteira, não só a última mensagem).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.services.lembrete_pendencia import (
    detectar_pendencia_thread,
    eh_pergunta_registro,
    montar_mensagem,
)

AGORA = datetime(2026, 7, 23, 12, 40, tzinfo=timezone.utc)

PERGUNTA_INCIDENTE = (
    "Pessoa10! Que ótima notícia, Guilherme fechou! 🎉\n\n"
    "Só preciso de duas infos pra registrar tudo certinho:\n\n"
    "1️⃣ Que horas foi a visita na quinta (17/07)?\n"
    "2️⃣ Qual foi o valor da venda?"
)


def _h(horas: float) -> datetime:
    """Timestamp `horas` atrás de AGORA."""
    return AGORA - timedelta(hours=horas)


# ── eh_pergunta_registro ─────────────────────────────────────────────────────


def test_pergunta_incidente_corretor_eh_registro():
    assert eh_pergunta_registro(PERGUNTA_INCIDENTE)


def test_pergunta_sem_interrogacao_nao_eh_registro():
    assert not eh_pergunta_registro("Registrei a visita do Guilherme certinho!")


def test_pergunta_sem_keyword_nao_eh_registro():
    assert not eh_pergunta_registro("Oi Pessoa10, tudo bem com você?")


def test_pergunta_de_visita_eh_registro():
    assert eh_pergunta_registro("Ele foi na visita? Me confirma o status que já registro.")


# ── detectar_pendencia_thread ────────────────────────────────────────────────


def _thread_incidente(idade_ultima_h: float) -> list[tuple[str, str, datetime]]:
    """Reproduz o fim da thread da Pessoa10: pergunta na PENÚLTIMA msg da cauda."""
    return [
        ("user", "Ele visitou e fechou a venda", _h(idade_ultima_h + 1.0)),
        ("assistant", PERGUNTA_INCIDENTE, _h(idade_ultima_h + 0.1)),
        ("assistant", "Anotei, Pessoa10! Deixa eu criar a visita da Rosana aqui.", _h(idade_ultima_h)),
    ]


def test_detecta_pendencia_do_incidente():
    resultado = detectar_pendencia_thread(_thread_incidente(20.0), AGORA)
    assert resultado is not None
    pergunta, ultima_at = resultado
    assert pergunta == PERGUNTA_INCIDENTE
    assert ultima_at == _h(20.0)


def test_corretora_respondeu_depois_nao_ha_pendencia():
    thread = _thread_incidente(20.5) + [("user", "Foi às 15h, 423 mil", _h(20.0))]
    assert detectar_pendencia_thread(thread, AGORA) is None


def test_pendencia_muito_recente_fora_da_janela():
    # 10h < 18h — ainda não é hora de cobrar (cobra no dia seguinte)
    assert detectar_pendencia_thread(_thread_incidente(10.0), AGORA) is None


def test_pendencia_muito_antiga_fora_da_janela():
    # 50h > 48h — janela perdida, não cobra mais
    assert detectar_pendencia_thread(_thread_incidente(50.0), AGORA) is None


def test_cauda_assistant_sem_pergunta_de_registro_nao_ha_pendencia():
    thread = [
        ("user", "Oi Assistente", _h(21.0)),
        ("assistant", "Oi, Pessoa10! Tudo bem? Como posso te ajudar hoje?", _h(20.0)),
    ]
    assert detectar_pendencia_thread(thread, AGORA) is None


def test_thread_vazia_nao_ha_pendencia():
    assert detectar_pendencia_thread([], AGORA) is None


# ── montar_mensagem ──────────────────────────────────────────────────────────


def test_mensagem_do_incidente_menciona_o_que_falta():
    msg = montar_mensagem("Pessoa5", PERGUNTA_INCIDENTE)
    assert msg.startswith("Oi Pessoa5!")
    assert "o horário da visita" in msg
    assert "o valor da venda" in msg
    # tom gentil, nunca cobrança
    assert "cobrar" not in msg.lower()


def test_mensagem_fallback_sem_keywords_especificas():
    msg = montar_mensagem("Pessoa1 Silva", "Você consegue me confirmar pra eu registrar?")
    assert msg.startswith("Oi Pessoa1!")
    assert "algumas informações" in msg
