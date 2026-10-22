"""Teto de consulta de foto de perfil vale sob concorrência.

POR QUE EXISTE (08/10/2026): o teto `_FOTO_TETO_HORA = 30` foi furado em 13
horas distintas entre 05 e 08/10 — até 68 consultas em 9 segundos. O painel
abria um avatar por card da lista e as requests chegavam juntas; o endpoint
lia o contador, consultava o Evolution e só gravava depois. Todas liam o mesmo
número abaixo do teto e todas passavam. A leitora é a linha da Dona (2244), a
linha crítica dos disparos: rajada estrangula a consulta dela como estrangulou
a 2955 em 02/10.

Nada aqui fala com o Evolution: `_foto_url_corretor` é stub com latência, e é ele
que conta as consultas "reais".
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import text

from app.api import painel
from app.db.session import SessionLocal

# Faixa fictícia, fora de qualquer telefone real.
_PREFIXO = "5599970"


def _tel(i: int) -> str:
    return f"{_PREFIXO}{i:06d}"


@pytest.fixture()
def foto_isolada(monkeypatch):
    """contato_foto vazia + endpoint sem auth/contexto real + Evolution em stub."""
    with SessionLocal() as s:
        # Mesmo schema de produção (tabela criada à mão, sem migration).
        s.execute(text(
            "CREATE TABLE IF NOT EXISTS contato_foto ("
            " telefone text PRIMARY KEY, url text,"
            " checked_at timestamp without time zone DEFAULT now())"
        ))
        # O teto conta a tabela INTEIRA na última hora: o banco de teste tem
        # que começar zerado (a trava do conftest garante que não é produção).
        s.execute(text("DELETE FROM contato_foto"))
        s.commit()

    monkeypatch.setattr(painel, "_identificar_usuario", lambda _u: "dona")
    monkeypatch.setattr(painel, "_viewer_info", lambda _u: {"corretora_id": None})
    monkeypatch.setattr(painel, "_pode_atender", lambda *_a: True)
    monkeypatch.setattr(
        painel, "_lead_conversa_contexto",
        lambda lead_id: {"telefone": "+" + lead_id, "corretora_id": 3},
    )

    consultas: list[str] = []
    lock = threading.Lock()
    resposta = {"url": "https://pps.whatsapp.net/x.jpg", "origem": "mac:dona-pessoal"}

    def _stub(num: str):
        time.sleep(0.05)  # latência de rede: alarga a janela da corrida
        with lock:
            consultas.append(num)
        return resposta["url"], resposta["origem"]

    monkeypatch.setattr(painel, "_foto_url_corretor", _stub)
    yield consultas, resposta

    with SessionLocal() as s:
        s.execute(text("DELETE FROM contato_foto"))
        s.commit()


def _rajada(leads: list[str]) -> list[dict]:
    """Solta todas as requests juntas (barreira), como o painel fazia."""
    barreira = threading.Barrier(len(leads))

    def _uma(lead_id: str) -> dict:
        barreira.wait()
        return painel.painel_conversa_foto(lead_id=lead_id, x_painel_user="dona")

    with ThreadPoolExecutor(max_workers=len(leads)) as ex:
        return list(ex.map(_uma, leads))


def _gravadas_na_hora() -> int:
    with SessionLocal() as s:
        return int(s.execute(text(
            "SELECT count(*) FROM contato_foto "
            "WHERE checked_at > now() - interval '1 hour'"
        )).scalar_one())


def test_rajada_maior_que_o_teto_nao_passa_do_teto(foto_isolada):
    consultas, _ = foto_isolada
    teto = painel._FOTO_TETO_HORA
    n = teto * 2 + 8  # 68: o tamanho da rajada medida em 08/10

    respostas = _rajada([_tel(i) for i in range(n)])

    assert len(consultas) == teto, f"{len(consultas)} consultas reais com teto {teto}"
    assert _gravadas_na_hora() == teto
    barradas = [r for r in respostas if r.get("motivo") == "teto_horario"]
    assert len(barradas) == n - teto
    assert all(r["consultas_na_hora"] >= teto for r in barradas)


def test_mesmo_numero_em_paralelo_consulta_uma_vez(foto_isolada):
    consultas, _ = foto_isolada
    # O mesmo lead pedido 10x juntas (card da lista + cabeçalho da conversa,
    # duas abas): só a primeira gasta orçamento da linha.
    respostas = _rajada([_tel(1)] * 10)

    assert len(consultas) == 1
    assert _gravadas_na_hora() == 1
    assert all(r.get("motivo") != "teto_horario" for r in respostas)


def test_sem_instancia_devolve_a_vaga(foto_isolada):
    consultas, resposta = foto_isolada
    resposta["url"], resposta["origem"] = None, "sem_instancia"

    r = painel.painel_conversa_foto(lead_id=_tel(2), x_painel_user="dona")

    # Consulta que não aconteceu não ocupa o teto nem vira cache de "sem foto".
    assert r == {"url": None, "motivo": "sem_instancia_de_consulta"}
    assert _gravadas_na_hora() == 0


def test_sem_instancia_restaura_registro_antigo(foto_isolada):
    _, resposta = foto_isolada
    with SessionLocal() as s:
        s.execute(text(
            "INSERT INTO contato_foto (telefone, url, checked_at) "
            "VALUES (:t, 'https://antiga', now() - interval '30 hours')"
        ), {"t": _tel(3)})
        s.commit()
    resposta["url"], resposta["origem"] = None, "sem_instancia"

    painel.painel_conversa_foto(lead_id=_tel(3), x_painel_user="dona")

    with SessionLocal() as s:
        url, velho = s.execute(text(
            "SELECT url, checked_at < now() - interval '29 hours' "
            "FROM contato_foto WHERE telefone = :t"
        ), {"t": _tel(3)}).one()
    assert url == "https://antiga"
    assert velho is True
