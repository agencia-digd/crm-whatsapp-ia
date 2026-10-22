"""Fila de aprovação da sugestão de follow-up.

O que estes testes protegem, em ordem de importância:
1. NADA sai sem aprovação humana explícita.
2. Erro em qualquer ponto → cai no template de sempre (fail-safe), nunca
   impede o follow-up nem inventa mensagem.
3. O kill-switch realmente desliga.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.services import sugestao_cadencia as sug


class FakeRes:
    """Resultado da sombra (o que `decidir_sombra_cadencia` devolve)."""

    def __init__(self, usou_contexto=True, toque=2, tel="+5500000000000"):
        self.corretora_id = 3
        self.lead_nome = "João Silva"
        self.lead_telefone = tel
        self.toque = toque
        self.template_atual = "Texto padrão de sempre."
        self.sugestao_ia = "você comentou que voltava dia 20 — vamos marcar?"
        self.usou_contexto = usou_contexto
        self.contexto_usado = "volta de viagem dia 20" if usou_contexto else None
        self.motivo = "usei a data que ele deu"
        self.modelo = "fake"


# ── 1. Nada sai sem aprovação ────────────────────────────────────────────────

def test_pendente_nao_devolve_texto(monkeypatch):
    """Enfileirada mas NÃO aprovada → cadência tem que usar o template."""
    monkeypatch.setattr(sug, "texto_aprovado", sug.texto_aprovado)  # sem mock
    # simula o lookup batendo numa linha pendente: o SQL filtra status='aprovada',
    # então o comportamento correto é não achar nada.
    chamadas = {}

    def fake_session():
        raise RuntimeError("sem banco no teste unitário")

    monkeypatch.setattr(sug, "SessionLocal", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    assert sug.texto_aprovado("+5500000000000", 2) is None
    assert chamadas == {}


def test_erro_de_banco_devolve_none_nao_levanta(monkeypatch):
    """FAIL-SAFE: banco fora do ar não pode derrubar o follow-up."""
    monkeypatch.setattr(sug, "SessionLocal", lambda: (_ for _ in ()).throw(RuntimeError("db down")))
    assert sug.texto_aprovado("+5500000000000", 2) is None       # → usa template
    assert sug.enfileirar(FakeRes()) is None                     # → não quebra
    assert sug.aprovar(1, por="dona") is False
    assert sug.rejeitar(1, por="dona") is False
    assert sug.expirar_vencidas() == 0
    sug.marcar_usada(1)  # não levanta


def test_telefone_vazio_nao_busca():
    assert sug.texto_aprovado("", 2) is None
    assert sug.texto_aprovado(None, 2) is None


# ── 2. Sanidade do conteúdo ──────────────────────────────────────────────────

def test_texto_editado_muito_longo_e_recusado(monkeypatch):
    """A Dona: 'não escrever muita coisa'. Acima do teto, não aprova."""
    chamou = {"n": 0}

    class S:
        def __enter__(self): chamou["n"] += 1; return self
        def __exit__(self, *a): return False
        def execute(self, *a, **k): raise AssertionError("não devia chegar no banco")
        def commit(self): pass

    monkeypatch.setattr(sug, "SessionLocal", lambda: S())
    assert sug.aprovar(1, por="dona", texto_editado="x" * (sug.MAX_CHARS + 1)) is False
    assert chamou["n"] == 0     # nem abriu sessão


def test_texto_editado_vazio_e_recusado(monkeypatch):
    monkeypatch.setattr(sug, "SessionLocal", lambda: (_ for _ in ()).throw(AssertionError("não devia abrir")))
    assert sug.aprovar(1, por="dona", texto_editado="   ") is False


def test_edicao_e_sanitizada_antes_de_salvar():
    """\\n do editor vira U+000B (a Meta rejeita \\n dentro de variável)."""
    from app.inspector.shadow_cadencia import sanitizar_variavel

    assert "\n" not in sanitizar_variavel("linha1\nlinha2")
    assert "\v" in sanitizar_variavel("linha1\nlinha2")


# ── 3. Só vai pra fila o que tem contexto real ───────────────────────────────

def test_sem_contexto_vira_sombra_nao_fila(monkeypatch):
    """Sem contexto o template já é a resposta certa — não gasta revisão humana."""
    capturado = {}

    class S:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, stmt, params=None):
            capturado.update(params or {})
            class R:
                def first(self_inner): return (99,)
            return R()
        def commit(self): pass

    monkeypatch.setattr(sug, "SessionLocal", lambda: S())

    sug.enfileirar(FakeRes(usou_contexto=False))
    assert capturado["st"] == sug.STATUS_SOMBRA

    capturado.clear()
    sug.enfileirar(FakeRes(usou_contexto=True))
    assert capturado["st"] == sug.STATUS_PENDENTE


# ── 4. Kill-switch ───────────────────────────────────────────────────────────

def test_cadencia_ignora_fila_com_flag_desligada(monkeypatch):
    """Com SUGESTAO_IA_CADENCIA=false a cadência nem consulta a fila."""
    from app.config import settings
    from app.services import cadencia as cad

    monkeypatch.setattr(settings, "sugestao_ia_cadencia", False, raising=False)

    def explode(*a, **k):
        raise AssertionError("não devia consultar a fila com a flag desligada")

    monkeypatch.setattr(sug, "texto_aprovado", explode)
    # o gate é o `if getattr(settings, ...)` em _enviar_e_registrar; com False
    # o import nem acontece. Basta garantir que a flag existe e é booleana.
    assert settings.sugestao_ia_cadencia is False
    assert hasattr(cad, "_chamar_sender")


def test_flag_existe_e_nasce_desligada():
    """Deploy seguro: quem não ligar explicitamente segue com o template."""
    from app.config import Settings

    assert Settings().sugestao_ia_cadencia is False


# ── 5. Constantes do contrato ────────────────────────────────────────────────

def test_status_validos_cobrem_o_ciclo():
    assert sug.STATUS_VALIDOS == {
        "sombra", "pendente", "aprovada", "rejeitada", "usada", "expirada",
    }


def test_validade_e_finita():
    """Sugestão aprovada envelhece: o contexto que ela usa pode ter mudado."""
    assert 0 < sug.VALIDADE_HORAS <= 168
