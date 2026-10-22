"""Reengajar numa linha SEM casco de mensagem livre → 422, não 502.

Caso real (10/09): a linha da Pessoa7 tinha 4 templates UTILITY aprovados, mas
todos de pós-ligação — nenhum com o contrato do reengajamento ({{2}} = a
mensagem que a corretora digita). O envio levantava RuntimeError, o painel
devolvia 502 "reengajamento_falhou" e o chat oferecia "tentar de novo": a
Pessoa7 apertou 5 vezes seguidas achando que era instabilidade.

Agora o erro tem tipo próprio (`SemTemplateDeRotacao`) e vira 422 com um código
que o front sabe traduzir e que NÃO ganha botão de retry.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.api import cloud_corretora_a as cg
from app.db.session import SessionLocal
from app.main import app
from app.services import cloud_lines

PESSOA_D_ID = 7
REF = "9900007002"
TEL = "+5500000000000"


@pytest.fixture()
def lead_da_pessoa_d():
    with SessionLocal() as s:
        s.execute(text("DELETE FROM leads_snapshot WHERE lead_id = :r"), {"r": REF})
        s.execute(text("DELETE FROM corretoras WHERE id = :i"), {"i": PESSOA_D_ID})
        s.execute(
            text(
                "INSERT INTO corretoras (id, nome, telefone, telefone_normalizado, "
                "                        evolution_instance, ativo) "
                "VALUES (:i, 'Pessoa7', '+5500000000000', '+5500000000000', "
                "        'acme_pessoa_d', TRUE)"
            ),
            {"i": PESSOA_D_ID},
        )
        s.execute(
            text(
                "INSERT INTO leads_snapshot "
                "(lead_id, nome, telefone, corretora_id, etapa_kommo, ultima_sync) "
                "VALUES (:r, 'Lead da Pessoa7', :t, :i, '10000005', NOW())"
            ),
            {"r": REF, "t": TEL, "i": PESSOA_D_ID},
        )
        s.commit()
    try:
        yield
    finally:
        with SessionLocal() as s:
            s.execute(text("DELETE FROM leads_snapshot WHERE lead_id = :r"), {"r": REF})
            s.execute(text("DELETE FROM corretoras WHERE id = :i"), {"i": PESSOA_D_ID})
            s.commit()


@pytest.fixture()
def linha_da_pessoa_d(monkeypatch):
    """Linha Cloud existente e habilitada a disparar — o que falta é o CASCO."""
    linha = cloud_lines.CloudLine(
        phone_id="000000000000000", slug="pessoa_d", papel="corretora",
        env_file="/etc/agencia/review.env", token_var="META_REVIEW_TOKEN",
    )
    monkeypatch.setattr(cloud_lines, "linha_da_corretora", lambda *a, **k: linha)
    monkeypatch.setattr(cloud_lines, "pode_disparar", lambda *a, **k: True)
    monkeypatch.setattr(cg, "ler_thread_cloud", lambda *a, **k: [])
    return linha


def test_422_quando_a_linha_nao_tem_casco(monkeypatch, lead_da_pessoa_d, linha_da_pessoa_d):
    def _sem_casco(*a, **k):
        raise cg.SemTemplateDeRotacao("linha pessoa_d: nenhum template da rotação está em UTILITY")

    monkeypatch.setattr(cg, "enviar_template_linha", _sem_casco)
    r = TestClient(app).post(
        "/painel/conversa/reengajar",
        json={"lead_id": REF, "texto": "Oi! Consegui a atualização que você pediu."},
        headers={"X-Painel-User": "pessoa_d"},
    )
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == "linha_sem_template_de_reengajamento"


def test_502_continua_pra_falha_de_verdade(monkeypatch, lead_da_pessoa_d, linha_da_pessoa_d):
    """Erro de rede/Meta segue 502 — esse SIM merece 'tentar de novo'."""
    def _cai(*a, **k):
        raise RuntimeError("cloud 500: internal error")

    monkeypatch.setattr(cg, "enviar_template_linha", _cai)
    r = TestClient(app).post(
        "/painel/conversa/reengajar",
        json={"lead_id": REF, "texto": "oi"},
        headers={"X-Painel-User": "pessoa_d"},
    )
    assert r.status_code == 502
    assert r.json()["detail"] == "reengajamento_falhou"
