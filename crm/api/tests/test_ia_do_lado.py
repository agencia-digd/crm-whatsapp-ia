"""IA DO LADO, NUNCA NO CAMINHO — resumo no card e opinião no que a regra não entendeu.

Spec `projetos/crm/specs/2026-10-05-ia-do-lado.md` (decisão da Dona, 05/10/2026).
Mesmo desenho de `test_copy_e_quando_responde.py` (e a mesma montagem): cada
teste faz o caminho inteiro — webhook da linha da Corretor → regra → IA dublada →
handoff → banco → o que o painel mostra. Nenhum envio, nenhuma chamada a modelo
de verdade (`Modelo` abaixo é o dublê).

O que é provado (os nomes estão na tabela de `projetos/crm/IA-DO-LADO.md`):
  * IA fora do ar, lenta, JSON inválido, "não sei", confiança baixa → idêntico
    ao de hoje (a pessoa vai pra roleta), e o que aconteceu fica no evento;
  * a IA nunca desfaz decisão da regra (nem é chamada quando a regra decidiu);
  * o resumo não contém fato sem fala de origem;
  * chave desligada = a IA não é chamada.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text as _t

from app.services import carrossel_corretor as cb
from app.services import config_operacional as cfg
from app.services import handoff_remarketing as hr
from app.services import ia_do_lado as ia
from app.services import quando_responde as qr
from tests.test_copy_e_quando_responde import (  # noqa: F401 — fixtures
    ADMIN,
    CORRETOR,
    CORRETOR,
    NOVO,
    TENANT,
    VENDA_PERDIDA,
    _grava_msg,
    chega,
    com_conversa_no_celular,
    disparado,
    eventos,
    linha_corretor,
    linha_do,
    mesa,
    painel,
    s,
    salvar_responde,
    tela,
)

CHAVES_IA = ("ia_resumo_card", "ia_opina_indefinida", "ia_opina_corte")

# "Olá tudo bem e vc": a regra não lê nada (indefinida), é texto, e a pessoa
# vai pra roleta como todo mundo. É o caso em que a IA é chamada.
INDEFINIDA = "Olá tudo bem e vc"


class Modelo:
    """Dublê do provider da Pessoa. `resposta` pode ser dict (vira JSON), str
    (vai crua), Exception (levanta) ou callable(system, user) → um desses.
    `demora` segura a resposta. Anota o que recebeu."""

    def __init__(self, resposta=None, *, demora: float = 0.0) -> None:
        self.resposta = resposta
        self.demora = demora
        self.chamadas: list[dict] = []
        self.terminou = threading.Event()

    def __call__(self):            # fábrica: `ia._provider_factory = modelo`
        return self

    def complete(self, system, user, json_schema=None):
        self.chamadas.append({"system": system, "user": user, "schema": json_schema})
        try:
            if self.demora:
                time.sleep(self.demora)
            r = self.resposta(system, user) if callable(self.resposta) else self.resposta
            if isinstance(r, Exception):
                raise r
            return (json.dumps(r, ensure_ascii=False) if isinstance(r, dict) else r), None
        finally:
            self.terminou.set()


def opiniao(classe: str, confianca: float, trecho: str) -> dict:
    return {"classe": classe, "confianca": confianca, "trecho": trecho}


@pytest.fixture()
def chaves_ia(s):
    def _limpar():
        s.execute(_t("DELETE FROM config_operacional WHERE chave = ANY(:c)"),
                  {"c": list(CHAVES_IA)})
        s.execute(_t("DELETE FROM config_operacional_historico WHERE chave = ANY(:c)"),
                  {"c": list(CHAVES_IA)})
        s.commit()
        cfg.invalidar_cache()

    _limpar()
    yield
    _limpar()


@pytest.fixture()
def modelo(monkeypatch, chaves_ia):
    """Modelo dublado + resumo síncrono (sem thread solta no teste)."""
    m = Modelo(opiniao("nao_sei", 0.5, ""))
    monkeypatch.setattr(ia, "_provider_factory", m)
    monkeypatch.setattr(ia, "_em_segundo_plano", lambda alvo: alvo())
    return m


@pytest.fixture()
def roleta_corretor(mesa):
    assert salvar_responde(mesa, handoff_remarketing_roleta=[CORRETOR],
                           handoff_remarketing_avisa_lead=False).status_code == 200
    return mesa


def resposta_do_disparo(s, lid: int) -> dict:
    (ev,) = eventos(s, lid, cb.EVENT_RESPOSTA)
    return ev


def opinioes(m: Modelo) -> list[dict]:
    return [c for c in m.chamadas if c["schema"] is ia.OPINIAO_SCHEMA]


def resumos(s, lid: int) -> list[dict]:
    return eventos(s, lid, ia.EVENTO_RESUMO)


# ═════════════════════════════════════════════════════════════════════════════
# 1. A opinião nunca fica no caminho: falhou = regra de hoje
# ═════════════════════════════════════════════════════════════════════════════


def _vai_pra_roleta_como_hoje(s, lid: int) -> dict:
    etapa, dono, marca, _ = linha_do(s, lid)
    assert (etapa, dono) == (NOVO, CORRETOR), "a IA mudou o caminho do card"
    assert marca is not None
    (tr,) = eventos(s, lid, hr.EVENTO_TRANSFERENCIA)
    assert tr["sinal"] == "respondeu"
    ev = resposta_do_disparo(s, lid)
    assert (ev["classe"], ev.get("regra")) == ("indefinida", "sem_sinal")
    return ev["ia"]


def test_ia_fora_do_ar_vale_a_regra(roleta_corretor, s, linha_corretor, modelo):
    modelo.resposta = RuntimeError("Claude AI usage limit reached")
    tel, lid = disparado(s, "C01")
    chega(roleta_corretor, tel, INDEFINIDA)
    r = _vai_pra_roleta_como_hoje(s, lid)
    assert (r["chamada"], r["resultado"], r["pesou"]) == (True, "fora_do_ar", False)
    assert r["classe_regra"] == "indefinida" and r["classe_final"] == "indefinida"
    assert linha_corretor[0]["recusa_decidida"] is False


def test_ia_lenta_estoura_o_prazo_e_vale_a_regra(roleta_corretor, s, linha_corretor, modelo,
                                                 monkeypatch):
    """Passou do prazo: a pessoa já foi pra roleta, e a resposta tardia da IA
    (uma recusa!) é jogada fora — não move o card depois."""
    monkeypatch.setattr(ia, "PRAZO_OPINIAO_S", 0.2)
    # o resumo fica pra depois, como em produção (thread): o turno é só a opinião
    monkeypatch.setattr(ia, "_em_segundo_plano", lambda alvo: None)
    modelo.resposta = opiniao("recusa", 0.99, "Olá")
    modelo.demora = 1.0
    tel, lid = disparado(s, "C02")
    t0 = time.monotonic()
    chega(roleta_corretor, tel, INDEFINIDA)
    assert time.monotonic() - t0 < 0.9, "o turno esperou a IA além do prazo"
    r = _vai_pra_roleta_como_hoje(s, lid)
    assert (r["resultado"], r["pesou"]) == ("atrasou", False)
    assert modelo.terminou.wait(3)
    tardia = _espera_tardia(s, lid)
    assert (tardia["classe_ia"], tardia["confianca"]) == ("recusa", 0.99)
    assert linha_do(s, lid)[:2] == (NOVO, CORRETOR), "a resposta tardia mexeu no card"
    ev = resposta_do_disparo(s, lid)
    assert ev["classe"] == "indefinida", "recusa tardia mudou a classe"


def _espera_tardia(s, lid: int) -> dict:
    for _ in range(40):
        s.expire_all()
        ev = resposta_do_disparo(s, lid)
        if "tardia" in (ev.get("ia") or {}):
            return ev["ia"]["tardia"]
        time.sleep(0.05)
    raise AssertionError("a opinião tardia não foi anotada")


def test_interesse_que_chega_tarde_so_conta_na_taxa(roleta_corretor, s, linha_corretor, modelo,
                                                    monkeypatch):
    monkeypatch.setattr(ia, "PRAZO_OPINIAO_S", 0.2)
    monkeypatch.setattr(ia, "_em_segundo_plano", lambda alvo: None)
    modelo.resposta = opiniao("interesse", 0.97, "pode sim")
    modelo.demora = 0.6
    tel, lid = disparado(s, "C08")
    chega(roleta_corretor, tel, "Oi tudo bem? pode sim")
    assert linha_do(s, lid)[:2] == (NOVO, CORRETOR)
    assert modelo.terminou.wait(3)
    assert _espera_tardia(s, lid)["classe_ia"] == "interesse"
    ev = resposta_do_disparo(s, lid)
    assert (ev["classe"], ev["regra"]) == ("interesse", "ia_opiniao")
    assert ev["ia"]["resultado"] == "atrasou" and ev["ia"]["pesou"] is False


@pytest.mark.parametrize("bruto", [
    "isso não é json",
    {"classe": "talvez", "confianca": 0.99, "trecho": "Olá"},
    {"classe": "recusa", "confianca": 1.7, "trecho": "Olá"},
    {"classe": "recusa", "confianca": "alta", "trecho": "Olá"},
    {"classe": "recusa", "trecho": "Olá"},
    ["recusa"],
])
def test_json_invalido_vale_a_regra(roleta_corretor, s, linha_corretor, modelo, bruto):
    modelo.resposta = bruto if isinstance(bruto, (dict, str)) else json.dumps(bruto)
    tel, lid = disparado(s, "C03")
    chega(roleta_corretor, tel, INDEFINIDA)
    r = _vai_pra_roleta_como_hoje(s, lid)
    assert (r["resultado"], r["pesou"]) == ("invalida", False)


def test_trecho_que_a_pessoa_nao_escreveu_e_invalido(roleta_corretor, s, linha_corretor, modelo):
    """O trecho que decidiu tem que estar na fala do lead. "não tenho
    interesse" inventado não manda ninguém pra Venda perdida."""
    modelo.resposta = opiniao("recusa", 0.99, "não tenho interesse")
    tel, lid = disparado(s, "C04")
    chega(roleta_corretor, tel, INDEFINIDA)
    r = _vai_pra_roleta_como_hoje(s, lid)
    assert (r["resultado"], r["pesou"]) == ("invalida", False)


def test_nao_sei_vale_a_regra(roleta_corretor, s, linha_corretor, modelo):
    modelo.resposta = opiniao("nao_sei", 0.99, "")
    tel, lid = disparado(s, "C05")
    chega(roleta_corretor, tel, INDEFINIDA)
    r = _vai_pra_roleta_como_hoje(s, lid)
    assert (r["resultado"], r["classe_ia"], r["pesou"]) == ("ok", "nao_sei", False)


def test_confianca_abaixo_do_corte_vale_a_regra(roleta_corretor, s, linha_corretor, modelo):
    modelo.resposta = opiniao("recusa", ia.CORTE_PADRAO - 0.01, "Olá")
    tel, lid = disparado(s, "C06")
    chega(roleta_corretor, tel, INDEFINIDA)
    r = _vai_pra_roleta_como_hoje(s, lid)
    assert (r["classe_ia"], r["pesou"], r["corte"]) == ("recusa", False, ia.CORTE_PADRAO)
    assert r["confianca"] == pytest.approx(ia.CORTE_PADRAO - 0.01)


def test_corte_ilegivel_a_opiniao_nao_pesa(roleta_corretor, s, linha_corretor, modelo):
    s.execute(_t("INSERT INTO config_operacional (chave, valor, atualizado_por) "
                 "VALUES ('ia_opina_corte', CAST(:v AS jsonb), 'teste')"),
              {"v": json.dumps("alto")})
    s.commit()
    cfg.invalidar_cache()
    modelo.resposta = opiniao("recusa", 0.99, "Olá")
    tel, lid = disparado(s, "C07")
    chega(roleta_corretor, tel, INDEFINIDA)
    r = _vai_pra_roleta_como_hoje(s, lid)
    assert (r["pesou"], r["corte"]) == (False, None)


# ═════════════════════════════════════════════════════════════════════════════
# 2. O que a opinião pode fazer (e só isso)
# ═════════════════════════════════════════════════════════════════════════════


def test_recusa_com_confianca_vai_pra_venda_perdida(roleta_corretor, s, linha_corretor, modelo):
    """A recusa que a regra não leu ANTES de o card ir pra roleta. Vai pelo
    mesmo caminho da recusa da regra (o atendimento recebe `recusa_decidida`)
    e o evento diz quem decidiu: classe da regra, da IA, confiança e trecho."""
    modelo.resposta = opiniao("recusa", 0.97, "parei a procura")
    tel, lid = disparado(s, "C10")
    chega(roleta_corretor, tel, "Boa tarde No momento parei a procura Quando voltar entro em contato.")

    etapa, dono, marca, _ = linha_do(s, lid)
    assert (etapa, dono, marca) == (VENDA_PERDIDA, CORRETOR, None)
    assert eventos(s, lid, hr.EVENTO_TRANSFERENCIA) == []
    ev = resposta_do_disparo(s, lid)
    assert (ev["classe"], ev["regra"]) == ("recusa", "ia_opiniao")
    r = ev["ia"]
    assert (r["classe_regra"], r["classe_ia"], r["classe_final"], r["pesou"]) == \
        ("indefinida", "recusa", "recusa", True)
    assert (r["confianca"], r["trecho"]) == (0.97, "parei a procura")
    assert linha_corretor[0]["recusa_decidida"] is True
    # nunca grava opt-out: recusa não é "parar"
    assert s.execute(_t("SELECT count(*) FROM disparos_reengajamento WHERE opt_out "
                        "AND telefone = :t"), {"t": tel}).scalar() == 0


def test_interesse_com_confianca_conta_na_taxa(roleta_corretor, s, linha_corretor, modelo):
    """"interesse" não muda o caminho (todo mundo já vai pra roleta), só a
    taxa: a classe do evento passa a ser `interesse`, que é a que conta."""
    modelo.resposta = opiniao("interesse", 0.95, "pode sim")
    tel, lid = disparado(s, "C11")
    chega(roleta_corretor, tel, "Oi tudo bem? pode sim")
    assert linha_do(s, lid)[:2] == (NOVO, CORRETOR)
    ev = resposta_do_disparo(s, lid)
    assert (ev["classe"], ev["regra"]) == ("interesse", "ia_opiniao")
    assert ev["ia"]["classe_regra"] == "indefinida" and ev["ia"]["pesou"] is True
    assert "interesse" in __import__("app.services.remarketing_resposta",
                                     fromlist=["x"]).CLASSES_QUE_CONTAM


@pytest.mark.parametrize("corpo,tipo,etapa", [
    ("Não tenho interesse", "text", VENDA_PERDIDA),        # recusa da regra
    ("PARAR", "text", VENDA_PERDIDA),                      # parar da regra
    ("Quero sim, pode mandar", "text", NOVO),              # interesse da regra
    ("Saiba mais", "button", NOVO),                        # botão
    (None, "audio", NOVO),                                 # mídia: não há o que ler
])
def test_ia_nao_e_chamada_quando_a_regra_decidiu(roleta_corretor, s, linha_corretor, modelo,
                                                monkeypatch, corpo, tipo, etapa):
    """A IA só opina no que a regra NÃO decidiu. Com a regra decidida ela nem
    é chamada — logo não tem como desfazer recusa, parar ou interesse."""
    monkeypatch.setattr(cb, "_gravar_opt_out", lambda *a, **k: None)
    modelo.resposta = opiniao("interesse", 0.99, "x")
    tel, lid = disparado(s, "C12")
    chega(roleta_corretor, tel, corpo, tipo=tipo)
    assert opinioes(modelo) == []
    assert linha_do(s, lid)[0] == etapa
    assert all("ia" not in e for e in eventos(s, lid, cb.EVENT_RESPOSTA))


def test_resposta_automatica_nao_chama_a_ia(roleta_corretor, s, linha_corretor, modelo):
    tel, lid = disparado(s, "C13", ha_minutos=0)
    chega(roleta_corretor, tel, "Olá! Agradecemos seu contato, em breve retornaremos.",
          quando=time.time() + 2)
    assert modelo.chamadas == []
    assert linha_do(s, lid)[0] == cb.STAGE_DISPARADO


def test_card_que_ja_saiu_da_roda_nao_chama_a_ia(roleta_corretor, s, linha_corretor, modelo):
    """Card em Venda perdida, já com a corretora, ou em Respondeu (segurado no
    celular): a IA não é chamada — "nunca devolve card de Venda perdida" e não
    se mete em conversa que já anda."""
    modelo.resposta = opiniao("recusa", 0.99, "Olá")
    tel, lid = disparado(s, "C14")
    s.execute(_t("UPDATE leads SET stage_id = :p WHERE id = :i"),
              {"p": VENDA_PERDIDA, "i": lid})
    s.commit()
    chega(roleta_corretor, tel, INDEFINIDA)
    assert modelo.chamadas == [] and linha_do(s, lid)[0] == VENDA_PERDIDA

    tel2, lid2 = com_conversa_no_celular(s, "C15")
    chega(roleta_corretor, tel2, "Bom dia")                      # vai pra Respondeu, segurado
    assert linha_do(s, lid2)[:2] == (cb.STAGE_RESPONDEU, CORRETOR)
    n = len(modelo.chamadas)
    chega(roleta_corretor, tel2, INDEFINIDA)                     # já em Respondeu
    assert len(modelo.chamadas) == n
    assert linha_do(s, lid2)[:2] == (cb.STAGE_RESPONDEU, CORRETOR)


def test_numero_que_nao_e_lead_da_roda_nao_chama_a_ia(roleta_corretor, s, linha_corretor, modelo):
    chega(roleta_corretor, "+5500000000000", INDEFINIDA)
    assert modelo.chamadas == []


# ═════════════════════════════════════════════════════════════════════════════
# 3. Chaves desligadas = a IA não é chamada
# ═════════════════════════════════════════════════════════════════════════════


def test_opiniao_desligada_nao_chama_a_ia(roleta_corretor, s, linha_corretor, modelo):
    tela(roleta_corretor, "ia_opina_indefinida", False)
    modelo.resposta = opiniao("recusa", 0.99, "Olá")
    tel, lid = disparado(s, "C20")
    chega(roleta_corretor, tel, INDEFINIDA)
    assert [c for c in modelo.chamadas if c["schema"] is ia.OPINIAO_SCHEMA] == []
    assert linha_do(s, lid)[:2] == (NOVO, CORRETOR)
    assert "ia" not in resposta_do_disparo(s, lid)


def test_resumo_desligado_nao_chama_a_ia(roleta_corretor, s, linha_corretor, modelo):
    tela(roleta_corretor, "ia_resumo_card", False)
    tela(roleta_corretor, "ia_opina_indefinida", False)
    tel, lid = disparado(s, "C21")
    chega(roleta_corretor, tel, "Quero sim, pode mandar")
    assert linha_do(s, lid)[:2] == (NOVO, CORRETOR)
    assert modelo.chamadas == []
    assert resumos(s, lid) == []


@pytest.mark.parametrize("valor", ["sim", 1, {"x": 1}])
def test_valor_torto_gravado_por_fora_vale_desligado(roleta_corretor, s, linha_corretor, modelo, valor):
    for chave in ("ia_resumo_card", "ia_opina_indefinida"):
        s.execute(_t("INSERT INTO config_operacional (chave, valor, atualizado_por) "
                     "VALUES (:c, CAST(:v AS jsonb), 'teste')"),
                  {"c": chave, "v": json.dumps(valor)})
    s.commit()
    cfg.invalidar_cache()
    tel, lid = disparado(s, "C22")
    chega(roleta_corretor, tel, INDEFINIDA)
    assert modelo.chamadas == []
    assert linha_do(s, lid)[:2] == (NOVO, CORRETOR)
    tela_ia = qr.resumo(s, TENANT)["ia"]
    assert (tela_ia["resumo_ligado"], tela_ia["opina_ligada"]) == (None, None)


def test_banco_de_config_fora_vale_desligado(monkeypatch):
    def _cai(_chave):
        raise cfg.ConfigIndisponivel("banco fora")

    monkeypatch.setattr(ia, "_cfg", _cai)
    assert ia.resumo_ligado() is False and ia.opiniao_ligada() is False
    assert ia.corte() is None


# ═════════════════════════════════════════════════════════════════════════════
# 4. Resumo no card
# ═════════════════════════════════════════════════════════════════════════════


def _resumo_do_modelo(system, user):
    if "resumo" not in system:
        return opiniao("nao_sei", 0.5, "")
    return {
        "resumo": "Procura apartamento de 2 quartos pra morar com a filha.",
        "fatos": [
            {"fato": "quer 2 quartos", "fala": "L1", "trecho": "2 quartos"},
            {"fato": "é pra morar com a filha", "fala": "L2", "trecho": "com minha filha"},
            # inventado: não está em fala nenhuma
            {"fato": "orçamento de 300 mil", "fala": "L1", "trecho": "300 mil"},
            # fala da casa (disparo) não vira fato
            {"fato": "apareceram opções novas", "fala": "C1", "trecho": "opções novas"},
            # id que não existe
            {"fato": "mora no Bairro A", "fala": "L9", "trecho": "Bairro A"},
            # número que a pessoa não escreveu, numa fala que existe
            {"fato": "quer 3 quartos", "fala": "L1", "trecho": "quartos"},
        ],
    }


def test_resumo_chega_no_card_depois_da_transferencia(roleta_corretor, s, linha_corretor, modelo):
    modelo.resposta = _resumo_do_modelo
    tel, lid = disparado(s, "C30")
    _grava_msg(s, tel, texto="[reengajamento: Aqui é da Acme. Apareceram opções novas]",
               de_corretora=True, ha=timedelta(minutes=59), tipo="template")
    chega(roleta_corretor, tel, "Oi, quero ver apto de 2 quartos")
    chega(roleta_corretor, tel, "é pra morar com minha filha")

    assert linha_do(s, lid)[:2] == (NOVO, CORRETOR)
    final = resumos(s, lid)[-1]
    assert final["status"] == "ok" and final["origem"] == "transferencia"
    assert [f["fato"] for f in final["fatos"]] == ["quer 2 quartos"]
    assert final["descartados"] == 5
    assert final["resumo"] == "Procura apartamento de 2 quartos pra morar com a filha."

    # o que foi pro modelo: só esta pessoa, só a linha da Corretor
    (pedido,) = [c for c in modelo.chamadas if c["schema"] is ia.RESUMO_SCHEMA]
    assert "2 quartos" in pedido["user"] and "[L1] Lead:" in pedido["user"]

    # board de vendas e card aberto mostram
    cards = [c for col in roleta_corretor.get("/painel/crm/board", headers=ADMIN).json()["colunas"]
             for c in col["cards"] if c["lead_id"] == lid]
    assert cards[0]["resumo_ia"]["status"] == "ok"
    assert cards[0]["resumo_ia"]["fatos"] == [{"fato": "quer 2 quartos", "trecho": "2 quartos"}]
    conv = roleta_corretor.get(f"/painel/conversa?lead_id={lid}", headers=ADMIN)
    if conv.status_code == 200:
        assert conv.json()["resumo_ia"]["status"] == "ok"


def test_resumo_nao_tem_fato_sem_fala_de_origem():
    falas = [ia.Fala("C1", "casa", "Ainda procura imóvel? Temos 2 lançamentos."),
             ia.Fala("L1", "lead", "procuro casa térrea"),
             ia.Fala("L2", "lead", "no máximo 400 mil")]
    r = ia.validar_resumo({
        "resumo": "Quer casa térrea até 400 mil.",
        "fatos": [
            {"fato": "quer casa térrea", "fala": "L1", "trecho": "casa térrea"},
            {"fato": "até 400 mil", "fala": "L2", "trecho": "400 mil"},
            {"fato": "tem 2 filhos", "fala": "L1", "trecho": "2 filhos"},
            {"fato": "interessou nos 2 lançamentos", "fala": "C1", "trecho": "2 lançamentos"},
            {"fato": "até 500 mil", "fala": "L2", "trecho": "mil"},
            {"fato": "sem origem"},
            "texto solto",
        ],
    }, falas)
    assert [f["fato"] for f in r.fatos] == ["quer casa térrea", "até 400 mil"]
    assert r.descartados == 5
    for f in r.fatos:
        origem = {x.id: x.texto for x in falas}[f["fala"]]
        assert ia._contido(f["trecho"], origem) and f["fala"].startswith("L")

    # resumo que cita número que a pessoa não escreveu cai; os fatos ficam
    r2 = ia.validar_resumo({"resumo": "Quer casa de 3 quartos.",
                            "fatos": [{"fato": "quer casa térrea", "fala": "L1",
                                       "trecho": "casa térrea"}]}, falas)
    assert r2.resumo is None and len(r2.fatos) == 1
    # nenhum fato de pé = sem resumo
    r3 = ia.validar_resumo({"resumo": "Quer casa.", "fatos": [
        {"fato": "x", "fala": "C1", "trecho": "Ainda"}]}, falas)
    assert r3.resultado == "invalida" and r3.fatos == []


def test_resumo_que_falha_vira_sem_resumo_e_o_card_ja_chegou(roleta_corretor, s, linha_corretor,
                                                             modelo):
    modelo.resposta = RuntimeError("fora")
    tel, lid = disparado(s, "C31")
    chega(roleta_corretor, tel, "Quero sim, pode mandar")
    assert linha_do(s, lid)[:2] == (NOVO, CORRETOR)
    status = [r["status"] for r in resumos(s, lid)]
    assert status == ["pendente", "falhou"]
    cards = [c for col in roleta_corretor.get("/painel/crm/board", headers=ADMIN).json()["colunas"]
             for c in col["cards"] if c["lead_id"] == lid]
    assert cards[0]["resumo_ia"]["status"] == "sem_resumo"


def test_resumo_pendente_ha_muito_tempo_vira_sem_resumo():
    agora = datetime.now(timezone.utc)
    assert ia._para_card({"status": "pendente"}, agora - timedelta(seconds=30), agora)["status"] == "pendente"
    assert ia._para_card({"status": "pendente"}, agora - timedelta(minutes=10), agora)["status"] == "sem_resumo"


def test_transferencia_nao_espera_o_resumo(roleta_corretor, s, linha_corretor, chaves_ia,
                                           monkeypatch, ia_resumos_agendados):
    """Em produção o resumo sai numa thread depois do commit: o webhook volta
    com o card JÁ na corretora e o resumo ainda nem começou."""
    m = Modelo(opiniao("nao_sei", 0.5, ""))
    monkeypatch.setattr(ia, "_provider_factory", m)
    tel, lid = disparado(s, "C32")
    chega(roleta_corretor, tel, "Quero sim, pode mandar")
    assert linha_do(s, lid)[:2] == (NOVO, CORRETOR)
    assert len(ia_resumos_agendados) == 1 and resumos(s, lid) == []
    assert m.chamadas == []
    ia_resumos_agendados[0]()                        # a thread roda depois
    assert resumos(s, lid)[-1]["status"] in ("ok", "falhou")


def test_segurado_pelo_celular_tambem_ganha_resumo(roleta_corretor, s, linha_corretor, modelo):
    modelo.resposta = lambda system, user: (
        {"resumo": "Disse bom dia.", "fatos": [{"fato": "cumprimentou", "fala": "L1",
                                                 "trecho": "Bom dia"}]}
        if "resumo" in system else opiniao("nao_sei", 0.5, ""))
    tel, lid = com_conversa_no_celular(s, "C33")
    chega(roleta_corretor, tel, "Bom dia")
    assert linha_do(s, lid)[:2] == (cb.STAGE_RESPONDEU, CORRETOR)
    final = resumos(s, lid)[-1]
    assert (final["status"], final["origem"]) == ("ok", "segurado")
    cards = [c for col in roleta_corretor.get("/painel/crm/remarketing/board", headers=ADMIN).json()["colunas"]
             for c in col["cards"] if c["lead_id"] == lid]
    assert cards[0]["resumo_ia"]["resumo"] == "Disse bom dia."
    # a pessoa escreve de novo: segurado de novo não refaz o resumo
    n = len(resumos(s, lid))
    chega(roleta_corretor, tel, "alô?")
    assert len(resumos(s, lid)) == n


def test_resumo_viaja_com_o_lead_na_transferencia_manual(roleta_corretor, s, linha_corretor, modelo):
    """O resumo mora no card (evento do lead): quem recebe depois, na mão, vê."""
    modelo.resposta = lambda system, user: (
        {"resumo": "Disse bom dia.", "fatos": [{"fato": "cumprimentou", "fala": "L1",
                                                 "trecho": "Bom dia"}]}
        if "resumo" in system else opiniao("nao_sei", 0.5, ""))
    tel, lid = com_conversa_no_celular(s, "C34")
    chega(roleta_corretor, tel, "Bom dia")
    r = roleta_corretor.post("/painel/crm/transferir", headers=ADMIN,
                       json={"lead_id": lid, "corretora_destino": CORRETOR})
    assert r.status_code == 200, r.text
    assert linha_do(s, lid)[1] == CORRETOR
    cards = [c for quadro in ("/painel/crm/board", "/painel/crm/remarketing/board")
             for col in roleta_corretor.get(quadro, headers=ADMIN).json()["colunas"]
             for c in col["cards"] if c["lead_id"] == lid]
    assert cards and cards[0]["resumo_ia"]["resumo"] == "Disse bom dia."


# ═════════════════════════════════════════════════════════════════════════════
# 5. Privacidade: só a conversa daquele lead, só a linha da Corretor; log sem texto
# ═════════════════════════════════════════════════════════════════════════════


def test_vai_pro_modelo_so_a_conversa_deste_lead_na_linha_da_corretor(roleta_corretor, s, linha_corretor,
                                                                 modelo, monkeypatch):
    logs: list[dict] = []
    monkeypatch.setattr(ia.log, "info", lambda ev, **kw: logs.append({"ev": ev, **kw}))
    tel_outro, _ = disparado(s, "C40")
    _grava_msg(s, tel_outro, texto="segredo do outro lead", de_corretora=False,
               ha=timedelta(minutes=5))
    tel, lid = disparado(s, "C41")
    _grava_msg(s, tel, texto="conversa na linha da Pessoa10", de_corretora=False,
               ha=timedelta(minutes=5), instance="acme_corretor", corretora_id=1)
    modelo.resposta = opiniao("nao_sei", 0.4, "")
    chega(roleta_corretor, tel, INDEFINIDA)

    user = "\n".join(c["user"] for c in modelo.chamadas)
    assert INDEFINIDA in user
    assert "segredo do outro lead" not in user
    assert "linha da Pessoa10" not in user
    for c in modelo.chamadas:
        assert "token" not in (c["system"] + c["user"]).lower()
    # o log leva id e resultado, nunca o texto
    for linha in logs:
        assert INDEFINIDA not in json.dumps(linha, ensure_ascii=False, default=str)


# ═════════════════════════════════════════════════════════════════════════════
# 6. A tela
# ═════════════════════════════════════════════════════════════════════════════


def test_padrao_resumo_e_opiniao_ligados_com_o_corte_medido(s, chaves_ia):
    assert cfg.DEFAULTS["ia_resumo_card"]() is True
    assert cfg.DEFAULTS["ia_opina_indefinida"]() is True
    assert cfg.DEFAULTS["ia_opina_corte"]() == ia.CORTE_PADRAO
    t = qr.resumo(s, TENANT)["ia"]
    assert (t["resumo_ligado"], t["opina_ligada"], t["corte"]) == (True, True, ia.CORTE_PADRAO)
    assert "sem resumo" in t["falha_resumo"]
    assert "vale a regra" in t["falha_opiniao"]
    assert "Todo mundo que responde já vai pra corretora" in t["para_que_serve_opiniao"]


def test_tela_grava_e_recusa_valor_torto(mesa, s, chaves_ia):
    r = salvar_responde(mesa, ia_resumo_card=False, ia_opina_indefinida=False,
                        ia_opina_corte=0.9)
    assert r.status_code == 200, r.text
    t = r.json()["responde"]["config"]["ia"]
    assert (t["resumo_ligado"], t["opina_ligada"], t["corte"]) == (False, False, 0.9)
    for corpo in ({"ia_opina_corte": 1.5}, {"ia_opina_corte": 0.2},
                  {"ia_opina_corte": True}, {"ia_opina_corte": "0.9"},
                  {"ia_resumo_card": "sim"}, {"ia_opina_indefinida": 1}):
        r = salvar_responde(mesa, **corpo)
        assert r.status_code == 422, (corpo, r.text)
    # nada do que foi recusado entrou
    t = qr.resumo(s, TENANT)["ia"]
    assert (t["resumo_ligado"], t["opina_ligada"], t["corte"]) == (False, False, 0.9)
    # e a rota genérica de config aceita as mesmas chaves (o "voltar ao padrão")
    assert mesa.delete("/painel/automacoes/config/ia_opina_corte",
                       headers=ADMIN).status_code == 200
    assert qr.resumo(s, TENANT)["ia"]["corte"] == ia.CORTE_PADRAO


def test_tela_conta_o_que_a_ia_fez(roleta_corretor, s, linha_corretor, modelo):
    modelo.resposta = lambda system, user: (
        {"resumo": "x", "fatos": []} if "resumo" in system
        else opiniao("recusa", 0.99, "Já investi"))
    tel, lid = disparado(s, "C50")
    chega(roleta_corretor, tel, "Oi bem graças a Deus e vc ? Já investi")
    c = qr.resumo(s, TENANT)["ia"]["ultimos_30_dias"]
    assert c["opinioes"] >= 1 and c["pesou"] >= 1 and c["recusas_ia"] >= 1
