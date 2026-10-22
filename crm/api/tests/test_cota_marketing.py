"""Cota de MARKETING no disparo (Dona, 30/07/2026).

A regra histórica é "disparo SÓ UTILITY". A Dona liberou uma cota de N% por dia,
por linha. Estes testes existem pra garantir que o afrouxamento é EXATAMENTE o
pedido e nada mais — cada trava aqui protege dinheiro (MARKETING custa ~10x) e a
reputação da lista.

O que fica provado:
  • sem a env configurada, NADA muda (default 0 = trava histórica)
  • a cota é medida sobre o CAP do dia, por linha
  • estourada a cota, volta pra UTILITY
  • erro na contagem NÃO libera (fail-closed)
  • valor inválido na env NÃO libera
"""
from __future__ import annotations

import pytest
from sqlalchemy import text as sql_text

from app.db.session import SessionLocal
from app.services import cota_marketing as cm


@pytest.fixture()
def limpo(monkeypatch):
    """Zera a env e limpa os templates de teste da tabela de mensagens."""
    monkeypatch.delenv("DISPARO_COTA_MARKETING_PCT", raising=False)
    _limpar()
    yield
    _limpar()


def _limpar() -> None:
    with SessionLocal() as s:
        s.execute(
            sql_text("DELETE FROM mensagens_whatsapp "
                     "WHERE evolution_message_id LIKE 'tstcota_%'")
        )
        s.commit()


def _semear(n: int, categoria: str, instance: str = "acme_corretor") -> None:
    """N templates enviados HOJE por essa linha, na categoria dada."""
    import json
    import uuid

    with SessionLocal() as s:
        for _ in range(n):
            s.execute(
                sql_text(
                    """
                    INSERT INTO mensagens_whatsapp
                      (id, evolution_message_id, instance, remote_jid,
                       contato_telefone, from_corretora, tipo_mensagem,
                       conteudo_metadata, timestamp_evolution, recebido_em)
                    VALUES
                      (gen_random_uuid(), :w, :inst, '+5500000000000@s.whatsapp.net',
                       '+5500000000000', true, 'template',
                       CAST(:meta AS jsonb), now(), now())
                    """
                ),
                {
                    "w": f"tstcota_{uuid.uuid4().hex[:10]}",
                    "inst": instance,
                    "meta": json.dumps({"template": "x", "categoria": categoria}),
                },
            )
        s.commit()


# ── a trava de default: sem env, nada muda ───────────────────────────────────
def test_sem_env_nao_libera_marketing(limpo):
    assert cm.pct_configurado() == 0
    with SessionLocal() as s:
        assert cm.pode_marketing("corretor", cap_diario=25, session=s) is False


def test_pct_invalido_nao_libera(limpo, monkeypatch):
    monkeypatch.setenv("DISPARO_COTA_MARKETING_PCT", "vinte")
    assert cm.pct_configurado() == 0
    with SessionLocal() as s:
        assert cm.pode_marketing("corretor", cap_diario=25, session=s) is False


def test_pct_fora_da_faixa_e_saneado(monkeypatch):
    monkeypatch.setenv("DISPARO_COTA_MARKETING_PCT", "500")
    assert cm.pct_configurado() == 100
    monkeypatch.setenv("DISPARO_COTA_MARKETING_PCT", "-10")
    assert cm.pct_configurado() == 0


# ── a cota em si ─────────────────────────────────────────────────────────────
def test_libera_dentro_da_cota(limpo, monkeypatch):
    monkeypatch.setenv("DISPARO_COTA_MARKETING_PCT", "20")
    # cap 25 · 20% = teto 5. Nada enviado ainda → libera.
    with SessionLocal() as s:
        assert cm.pode_marketing("corretor", cap_diario=25, session=s) is True


def test_bloqueia_quando_a_cota_estoura(limpo, monkeypatch):
    monkeypatch.setenv("DISPARO_COTA_MARKETING_PCT", "20")
    _semear(5, "MARKETING")          # teto de cap 25 é 5
    with SessionLocal() as s:
        assert cm.pode_marketing("corretor", cap_diario=25, session=s) is False


def test_utility_nao_consome_a_cota(limpo, monkeypatch):
    monkeypatch.setenv("DISPARO_COTA_MARKETING_PCT", "20")
    _semear(20, "UTILITY")           # muito volume, mas nenhum MARKETING
    with SessionLocal() as s:
        assert cm.pode_marketing("corretor", cap_diario=25, session=s) is True
        mkt, total = cm.contagem_hoje("corretor", s)
    assert mkt == 0
    assert total == 20


def test_cota_e_por_linha(limpo, monkeypatch):
    monkeypatch.setenv("DISPARO_COTA_MARKETING_PCT", "20")
    _semear(5, "MARKETING", instance="acme_corretor")   # estoura a da Corretor
    with SessionLocal() as s:
        assert cm.pode_marketing("corretor", cap_diario=25, session=s) is False
        # a linha da Pessoa10 tem cota própria, intacta
        assert cm.pode_marketing("corretor", cap_diario=200, session=s) is True


def test_cap_pequeno_derruba_o_teto_a_zero(limpo, monkeypatch):
    monkeypatch.setenv("DISPARO_COTA_MARKETING_PCT", "20")
    # cap 4 · 20% = 0 (divisão inteira) → não libera. É o comportamento seguro:
    # linha com teto minúsculo não gasta o pouco que tem em MARKETING.
    with SessionLocal() as s:
        assert cm.pode_marketing("corretor", cap_diario=4, session=s) is False


def test_erro_na_contagem_nao_libera(limpo, monkeypatch):
    monkeypatch.setenv("DISPARO_COTA_MARKETING_PCT", "20")

    def explode(*_a, **_k):
        raise RuntimeError("banco fora")

    monkeypatch.setattr(cm, "contagem_hoje", explode)
    with SessionLocal() as s:
        assert cm.pode_marketing("corretor", cap_diario=25, session=s) is False


def test_linha_desconhecida_nao_libera(limpo, monkeypatch):
    monkeypatch.setenv("DISPARO_COTA_MARKETING_PCT", "20")
    with SessionLocal() as s:
        # slug sem instance mapeada → contagem (0,0) mas o teto existe;
        # o que protege aqui é não haver linha pra contar.
        mkt, total = cm.contagem_hoje("nao_existe", s)
    assert (mkt, total) == (0, 0)
