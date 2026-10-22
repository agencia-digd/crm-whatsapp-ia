"""Histórico da conversa e encerramento — os dois furos que o Astra achou (07/09).

Achados confirmados em 08/09, ambos silenciosos em produção:

1. `atender` lia `m.get("content")` do histórico, mas `conversations.load_history`
   devolve a chave **`text`**. Como os detectores (`_pediu_parar`, `_quer_vender`,
   `_e_recusa_branda`) aceitam `None` e devolvem False sem levantar, `falas`
   virava `[mensagem_atual, None, None, ...]`: TODA trava que depende do
   histórico enxergava só a mensagem da vez. Quem disse "não obrigado" e depois
   mandou um "Ok" educado era promovido.

   O teste que deveria pegar isso (`test_quem_recusou_antes_nunca_e_promovido`)
   montava o histórico com `"content"` — a mesma chave errada do código. Ficou
   verde o tempo todo. Os testes daqui montam o histórico no formato REAL, e
   `test_historico_do_banco_tem_a_chave_text` trava o contrato entre os dois
   módulos pra não voltar a divergir.

2. `encerrar_com_indicacao` e `marcar_opt_out` usam `append_message` e
   `SessionLocal` sem importar (o módulo importa dentro das funções, e nessas
   duas faltou). O `except` amplo transformava o NameError num
   `log.warning("indicacao_falhou")` — 16 vezes em 14 dias, sem ninguém ver.
   Pior que falhar limpo: `_responder` roda ANTES da linha que quebra, então o
   lead RECEBE a mensagem dos R$500 e ela não fica registrada — e sem registro
   não há idempotência pra impedir o reenvio.

Sem envio real: `_responder` e o banco são trocados por dublês em todo teste.
"""
from __future__ import annotations

import pytest

from app.services import atendimento_corretor as ab


@pytest.fixture(autouse=True)
def _ligado(monkeypatch):
    monkeypatch.setattr(ab.settings, "atendimento_corretor_ativo", True, raising=False)
    monkeypatch.setattr(ab, "_e_da_roda", lambda t: True)
    monkeypatch.setattr(ab, "_nos_abordamos_na_mao", lambda t: False)
    monkeypatch.setattr(ab, "_humano_falando", lambda tel, desde=None: False)
    monkeypatch.setattr(ab, "_e_da_equipe", lambda t: False)
    yield


def _historico(*mensagens: tuple[str, str]) -> list[dict]:
    """Histórico no formato que `load_history` devolve DE VERDADE (chave `text`)."""
    return [{"role": papel, "sender_kind": "lead" if papel == "user" else "pessoa",
             "text": txt, "created_at": None} for papel, txt in mensagens]


def _muda_conversas(monkeypatch, historico: list[dict]) -> None:
    monkeypatch.setattr("app.services.conversations.load_history",
                        lambda *a, **k: historico)
    monkeypatch.setattr("app.services.conversations.append_message",
                        lambda *a, **k: None)


# ── 1. O histórico precisa CHEGAR nas travas ────────────────────────────────


def test_historico_do_banco_tem_a_chave_text():
    """Contrato entre `load_history` e quem lê o histórico.

    É o teste que faltava: enquanto os dois lados forem escritos separados,
    alguém troca a chave de novo e nada acusa.
    """
    import inspect

    from app.services import conversations

    fonte = inspect.getsource(conversations.load_history)
    assert '"text": r.text' in fonte, "load_history mudou de chave"
    assert 'm.get("text")' in inspect.getsource(ab.atender), \
        "atender voltou a ler uma chave que load_history não devolve"


def test_recusa_no_historico_barra_promocao(monkeypatch):
    """O caso Fulano: o 'não' vem na 1ª msg, a promoção dispararia na 2ª."""
    _muda_conversas(monkeypatch, _historico(
        ("user", "No momento não, mas obrigado pelo contato"),
        ("assistant", "Imagina! Se voltar a procurar, me chama."),
    ))
    monkeypatch.setattr(ab, "_rodar_pessoa", lambda *a: None)
    monkeypatch.setattr(ab, "promover_para_vendas",
                        lambda *a, **k: pytest.fail("promoveu quem já disse não"))

    assert ab.atender("+5500000000000", "obrigado você também")["promoveu"] is False


def test_pedido_de_parar_no_historico_ainda_vale(monkeypatch):
    """Quem mandou parar não volta a ser atendido porque escreveu de novo.

    A trava mais cara da lista: insistir com quem pediu pra sair é o caminho
    da denúncia e do bloqueio do número.
    """
    _muda_conversas(monkeypatch, _historico(
        ("user", "para de me mandar mensagem"),
    ))
    monkeypatch.setattr(ab, "_marcar_opt_out", lambda t: None)
    monkeypatch.setattr(ab, "_rodar_pessoa",
                        lambda *a: pytest.fail("atendeu quem pediu pra parar"))

    assert ab.atender("+5500000000000", "ok")["motivo"] == "pediu_parar"


def test_quer_vender_no_historico_ainda_vale(monkeypatch):
    """Captação dita na 1ª mensagem não pode se perder na 2ª."""
    _muda_conversas(monkeypatch, _historico(
        ("user", "na verdade eu quero vender meu apartamento"),
    ))
    monkeypatch.setattr(ab, "_rodar_pessoa", lambda *a: None)
    vistos: list = []
    monkeypatch.setattr(ab, "promover_para_vendas",
                        lambda *a, **k: vistos.append(k.get("origem")) or True)

    # `motivo` aqui NÃO é "quer_vender": a captação promove e SEGUE pro
    # atendimento normal (a Pessoa ainda responde). O que se mede é a PRIMEIRA
    # promoção ter saído com a marca certa, vinda do histórico.
    ab.atender("+5500000000000", "pode ser essa semana")
    assert vistos and vistos[0] == "vendedor", \
        "perdeu a marca de captação vinda do histórico"
    # NOTA: `vistos` fica ["vendedor", None] — o atendimento normal promove de
    # novo, sem origem. Não é regressão desta correção (o 2º caminho já existia)
    # e é o item 13 da auditoria do Astra: a origem não é persistida, então a
    # 2ª chamada pode apagar a distinção de captação. Fica pra quando mexermos
    # na unificação do handoff — aqui só travamos que a 1ª carrega a marca.


def test_historico_vazio_nao_quebra(monkeypatch):
    """Primeira mensagem da vida do lead: sem histórico, segue o baile."""
    _muda_conversas(monkeypatch, [])
    monkeypatch.setattr(ab, "_rodar_pessoa", lambda *a: "oi, tudo bem?")
    monkeypatch.setattr(ab, "_responder", lambda *a: None)
    monkeypatch.setattr(ab, "promover_para_vendas", lambda *a, **k: False)

    # Sucesso não traz `motivo` — o que se mede é não cair numa trava do
    # histórico que não existe.
    assert ab.atender("+5500000000000", "oi").get("motivo") != "pediu_parar"


def test_mensagem_sem_texto_no_historico_nao_quebra(monkeypatch):
    """Áudio/imagem entram no histórico com `text` nulo — não podem derrubar."""
    _muda_conversas(monkeypatch, _historico(
        ("user", None),  # type: ignore[arg-type]
        ("user", "para de me mandar mensagem"),
    ))
    monkeypatch.setattr(ab, "_marcar_opt_out", lambda t: None)
    monkeypatch.setattr(ab, "_rodar_pessoa", lambda *a: pytest.fail("atendeu"))

    assert ab.atender("+5500000000000", "ok")["motivo"] == "pediu_parar"


# ── 2. Encerramento não pode quebrar em silêncio ────────────────────────────


def test_encerrar_com_indicacao_nao_levanta_nameerror(monkeypatch):
    """O bug real: 16 `indicacao_falhou` em 14 dias, escondidos num except amplo.

    Aqui o envio é dublê; o que se mede é o registro TER acontecido.
    """
    enviados: list = []
    gravados: list = []
    monkeypatch.setattr(ab, "_responder", lambda tel, txt: enviados.append(txt))
    monkeypatch.setattr("app.services.conversations.append_message",
                        lambda *a, **k: gravados.append(a))
    monkeypatch.setattr(ab, "_mover_para_perdido", lambda *a, **k: True, raising=False)

    out = ab.encerrar_com_indicacao("+5500000000000", "Túlio")

    assert enviados, "não mandou a mensagem de indicação"
    assert gravados, "mandou e NÃO registrou — sem registro não há idempotência"
    assert out["indicacao"] is True


def test_marcar_opt_out_nao_levanta_nameerror(monkeypatch):
    """Mesmo defeito no opt-out — e aqui falhar significa continuar disparando."""
    import inspect

    fonte = inspect.getsource(ab.marcar_opt_out)
    assert "SessionLocal" in fonte
    assert "from app.db.session import SessionLocal" in fonte, \
        "marcar_opt_out usa SessionLocal sem importar no escopo"


def test_nenhuma_funcao_do_modulo_usa_nome_nao_importado():
    """Varre o módulo inteiro: qualquer função que use um nome sem import local
    nem global cai aqui, em vez de virar `warning` silencioso em produção."""
    import ast

    fonte = open(ab.__file__).read()
    arvore = ast.parse(fonte)

    topo = {"__file__", "__name__"}
    for no in arvore.body:
        if isinstance(no, (ast.Import, ast.ImportFrom)):
            for a in no.names:
                topo.add(a.asname or a.name.split(".")[0])
        elif isinstance(no, ast.Assign):
            for alvo in no.targets:
                if isinstance(alvo, ast.Name):
                    topo.add(alvo.id)
        elif isinstance(no, (ast.FunctionDef, ast.ClassDef)):
            topo.add(no.name)

    suspeitos = {"append_message", "load_history", "SessionLocal", "sql_text"}
    faltando: list[str] = []

    for fn in ast.walk(arvore):
        if not isinstance(fn, ast.FunctionDef):
            continue
        locais = set(topo)
        for no in ast.walk(fn):
            if isinstance(no, (ast.Import, ast.ImportFrom)):
                for a in no.names:
                    locais.add(a.asname or a.name.split(".")[0])
        for nome in suspeitos:
            usa = any(isinstance(n, ast.Name) and n.id == nome for n in ast.walk(fn))
            if usa and nome not in locais:
                faltando.append(f"{fn.name}() usa {nome}")

    assert not faltando, "nome usado sem import: " + "; ".join(faltando)
