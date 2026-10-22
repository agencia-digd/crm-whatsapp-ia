"""Aviso no Copiloto de lead que respondeu o disparo da Pessoa.

A Dona gostou do digest cru (lead falou / Pessoa falou) e pediu pra receber
sempre. O que protege:
1. o mesmo telefone não vira 2 avisos no mesmo dia;
2. falha de envio NÃO marca — o próximo tick tenta de novo;
3. a marca antiga do digest matinal não reenvia o que ela já leu;
4. conversa curta (< MIN_MSGS) não dispara nem marca — espera mais falas.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import text

from app.db.session import SessionLocal
from app.services import aviso_resposta_disparo as aviso

TEL = "+5500000000000"

_FALAS = [
    ("lead", "user", "Bom dia"),
    ("pessoa", "assistant", "Bom dia! Tudo bem?"),
    ("lead", "user", "Quero apto em Santa Rosália"),
    ("pessoa", "assistant", "Nessa região a gente tem lançamento sim"),
    ("lead", "user", "Dois quartos"),
    ("pessoa", "assistant", "Anoto. E a renda da família?"),
    ("lead", "user", "Uns 8 mil"),
    ("lead", "user", "Pode ser na planta"),
    ("lead", "user", "Renda de 15 mil"),
]


def _inserir(n: int) -> None:
    with SessionLocal() as s:
        s.execute(text("DELETE FROM conversations WHERE phone = :p"), {"p": TEL})
        s.commit()
    agora = datetime.now(timezone.utc)
    valores = []
    params: dict = {"p": TEL}
    for i, (kind, role, txt) in enumerate(_FALAS[:n]):
        valores.append(f"(:p, :k{i}, :r{i}, :t{i}, :c{i})")
        params[f"k{i}"] = kind
        params[f"r{i}"] = role
        params[f"t{i}"] = txt
        params[f"c{i}"] = agora
    with SessionLocal() as s:
        s.execute(
            text(
                "INSERT INTO conversations (phone, sender_kind, message_role, "
                f"text, created_at) VALUES {', '.join(valores)}"
            ),
            params,
        )
        s.commit()


def _limpar() -> None:
    with SessionLocal() as s:
        s.execute(text("DELETE FROM conversations WHERE phone = :p"), {"p": TEL})
        s.commit()


@pytest.fixture()
def estado(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "aviso.json"
    monkeypatch.setattr(aviso, "ESTADO", path)
    yield path


@pytest.fixture()
def conversa_curta():
    _inserir(2)
    yield
    _limpar()


@pytest.fixture()
def conversa():
    # 8 linhas = 5 falas do lead (o gatilho) + 3 da Pessoa
    _inserir(8)
    yield
    _limpar()


def test_montar_um_cita_fala_do_lead_e_da_pessoa(conversa):
    por, _ = aviso._conversas_hoje()
    assert TEL in por
    texto = aviso.montar_um(TEL, por[TEL], disparos=3)
    assert "Lead respondeu o disparo" in texto
    assert "...6958" in texto
    assert "Bom dia" in texto
    assert "Bom dia! Tudo bem?" in texto
    assert "👩 Pessoa" in texto
    assert "🧑 Lead" in texto
    assert "disparos hoje: *3*" in texto


def test_avisar_espera_cinco_falas(estado, conversa_curta):
    enviados: list[str] = []
    assert aviso.avisar(TEL, enviar=lambda t: enviados.append(t) or True) is False
    assert enviados == []
    assert aviso._chave(TEL) not in aviso._ler_estado()


def test_avisar_manda_uma_vez(estado, conversa):
    enviados: list[str] = []

    def enviar(t: str) -> bool:
        enviados.append(t)
        return True

    assert aviso.avisar(TEL, enviar=enviar) is True
    assert len(enviados) == 1
    assert "Bom dia" in enviados[0]
    assert "Pode ser na planta" in enviados[0]
    assert aviso.avisar(TEL, enviar=enviar) is True
    assert len(enviados) == 1  # segunda chamada não reenvia


def test_falha_de_envio_nao_marca(estado, conversa):
    assert aviso.avisar(TEL, enviar=lambda t: False) is False
    assert aviso.avisar(TEL, enviar=lambda t: True) is True


def test_pendentes_pula_quem_ja_foi(estado, conversa):
    aviso.avisar(TEL, enviar=lambda t: True)
    res = aviso.avisar_pendentes(enviar=lambda t: True)
    assert res["enviados"] == 0
    assert res["pulados"] >= 1


def test_pendentes_pula_conversa_curta(estado, conversa_curta):
    enviados: list[str] = []
    res = aviso.avisar_pendentes(enviar=lambda t: enviados.append(t) or True)
    assert res["enviados"] == 0
    assert enviados == []


def test_pendentes_envia_quem_ainda_nao_foi(estado, conversa):
    enviados: list[str] = []
    res = aviso.avisar_pendentes(enviar=lambda t: enviados.append(t) or True)
    assert res["enviados"] == 1
    assert "...6958" in enviados[0]
