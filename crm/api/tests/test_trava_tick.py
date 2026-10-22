"""Trava compartilhada dos motores que mandam mensagem.

Nasceu da auditoria de 29/07: a trava tinha sido posta só na cadência, e o Grok
e o Sol apontaram que `nutricao` e `msg_planejada` seguiam no mesmo padrão
"verifica → envia → marca". Centralizar evitou a terceira cópia divergindo —
que é o problema de fundo que os dois diagnosticaram.
"""
from __future__ import annotations

from app.services import trava_tick as tt


def test_namespaces_sao_distintos():
    """Cada motor trava só contra si mesmo. Namespace repetido faria um motor
    calar o outro sem querer."""
    ns = [tt.NS_CADENCIA, tt.NS_NUTRICAO, tt.NS_MSG_PLANEJADA]
    assert len(set(ns)) == 3, ns
    # e nenhum colide com o do Inspector
    from app.inspector.pipeline import _ADVISORY_LOCK_NAMESPACE

    assert _ADVISORY_LOCK_NAMESPACE not in ns


def test_segunda_tentativa_e_barrada_no_postgres():
    """O comportamento que importa: 2º tick do mesmo motor não entra."""
    a = tt.travar(tt.NS_CADENCIA, 4242)
    if a is None:
        return  # banco sem advisory lock (SQLite) — nada a testar
    try:
        assert a is not tt.OCUPADO
        b = tt.travar(tt.NS_CADENCIA, 4242)
        assert b is tt.OCUPADO, "2º tick entrou — a trava não está travando"
    finally:
        tt.soltar(a, tt.NS_CADENCIA, 4242)

    # depois de soltar, entra de novo
    c = tt.travar(tt.NS_CADENCIA, 4242)
    assert c is not tt.OCUPADO
    tt.soltar(c, tt.NS_CADENCIA, 4242)


def test_motores_diferentes_nao_se_bloqueiam():
    a = tt.travar(tt.NS_NUTRICAO, 4243)
    if a is None:
        return
    try:
        b = tt.travar(tt.NS_MSG_PLANEJADA, 4243)
        assert b is not tt.OCUPADO, "nutrição travou a mensagem planejada"
        tt.soltar(b, tt.NS_MSG_PLANEJADA, 4243)
    finally:
        tt.soltar(a, tt.NS_NUTRICAO, 4243)


def test_soltar_nunca_levanta():
    """Unlock é best-effort — erro ao soltar não pode derrubar o tick."""
    tt.soltar(None, tt.NS_CADENCIA, 1)
    tt.soltar(tt.OCUPADO, tt.NS_CADENCIA, 1)

    class ConnRuim:
        def execute(self, *a, **k):
            raise RuntimeError("conexão morta")

        def close(self):
            raise RuntimeError("close falha")

    tt.soltar(ConnRuim(), tt.NS_CADENCIA, 1)  # não levanta
