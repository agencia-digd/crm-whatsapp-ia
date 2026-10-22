"""Leitura de conversa por IA: só o parser/filtro, com provider falso (sem rede)."""
import json

from app.services import leitura_ia


class _Falso:
    def __init__(self, resposta):
        self.resposta = resposta

    def complete(self, system, user, schema=None):
        if isinstance(self.resposta, Exception):
            raise self.resposta
        return self.resposta, None


def test_ler_conversa_devolve_json():
    r = {"fechando": True, "confianca": "alta", "motivo": "m", "trecho": "t"}
    out = leitura_ia.ler_conversa(_Falso(json.dumps(r)), [(False, "mandei a documentação"), (True, "ok")])
    assert out == r


def test_ler_conversa_falha_da_ia_vira_none():
    assert leitura_ia.ler_conversa(_Falso(RuntimeError("timeout")), [(False, "oi")]) is None


def test_ler_conversa_vazia_nao_chama_ia():
    assert leitura_ia.ler_conversa(_Falso(RuntimeError("não deveria chamar")), [(False, "  ")]) is None
