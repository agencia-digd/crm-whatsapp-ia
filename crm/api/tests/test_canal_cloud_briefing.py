"""Prova do conserto de 23/09: corretora canal='cloud' não pode ser marcada offline.

Antes deste conserto, fetch_whatsapp_metrics comparava a instância com o nome
cravado "acme_corretora_a". Pessoa1, Pessoa5, Pessoa7, Corretor e Corretor — todas
canal='cloud' no banco — caíam no check da Evolution, voltavam "close" e tinham
as métricas DESCARTADAS, mesmo com as mensagens gravadas em mensagens_whatsapp.

A Evolution aqui responde 404 pra qualquer instância inventada, que é justamente
o cenário real: a instância Cloud não existe lá (ou existe com status close de
propósito, como diz a migration 0056).
"""
from app.agents import agente_assistente as ab

CLOUD = [
    ("Pessoa3", "acme_corretora_a"),
    ("Pessoa1", "acme_corretor"),
    ("Pessoa5", "acme_corretora_c"),
    ("Pessoa7", "acme_pessoa_d"),
    ("Corretor", "acme_corretor"),
    ("Corretor", "acme_corretor"),
]


def _c(nome, instance, canal):
    return {
        "nome": nome,
        "telefone": None,
        "evolution_instance": instance,
        "evolution_connected_since": None,
        "canal": canal,
    }


def test_canal_cloud_nunca_fica_offline():
    """As seis do Cloud passam, mesmo a Evolution não conhecendo a instância."""
    corretoras = [_c(n, i, "cloud") for n, i in CLOUD]
    out = ab.fetch_whatsapp_metrics(corretoras)

    for nome, _ in CLOUD:
        m = out.get(nome)
        assert m is not None, f"{nome} sumiu do resultado"
        assert not m.get("is_offline"), (
            f"{nome} marcada offline mesmo com canal=cloud — o bug voltou"
        )


def test_canal_evolution_continua_sendo_checado():
    """A trava não pode virar 'nunca marca offline': evolution ainda é checado."""
    out = ab.fetch_whatsapp_metrics([_c("Legado", "instancia_que_nao_existe", "evolution")])
    assert out["Legado"]["is_offline"] is True, (
        "canal=evolution com instância inexistente tem que continuar offline"
    )


def test_canal_ausente_cai_no_fallback_da_corretor():
    """Cadastro antigo sem canal: a Pessoa12 segue reconhecida pelo nome da instância."""
    out = ab.fetch_whatsapp_metrics([
        {"nome": "Pessoa3", "telefone": None,
         "evolution_instance": "acme_corretora_a", "evolution_connected_since": None},
    ])
    assert not out.get("Pessoa3", {}).get("is_offline")
