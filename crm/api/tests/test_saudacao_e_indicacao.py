"""Testes das 2 mudanças no copiloto da Pessoa12:

TAREFA 1 — saudação DOBRADA no template de follow-up:
  - {{1}} do template resolve o 1º nome real do lead pelo TELEFONE quando a
    cadência chama com first_name=None (leads_snapshot → mensagens_whatsapp);
  - textos do {{2}} (_TEMPLATES_MSG / remarketing) NÃO recumprimentam;
  - fallback neutro quando não há nome em lugar nenhum.

TAREFA 2 — nota interna de INDICAÇÃO (R$500) quando o lead sinaliza "já comprei":
  - detecção conservadora da intenção;
  - cria observacao_corretora (nunca envia nada pro lead);
  - idempotente (não duplica);
  - não cria sem card / com telefone ambíguo.

Precisam do DB de teste (TEST_DATABASE_URL). Sem ele, são pulados.
"""
from __future__ import annotations

import os

import pytest
from sqlalchemy import text

from app.api import cloud_corretora_a as cg
from app.services import cadencia

_DB = bool(os.environ.get("TEST_DATABASE_URL"))
requires_db = pytest.mark.skipif(not _DB, reason="precisa de TEST_DATABASE_URL")

CORRETORA_A_ID = 3


# ── TAREFA 1: saudação não dobra ─────────────────────────────────────────────


def test_nome_valido_rejeita_placeholders():
    assert cg._nome_valido("José")
    assert cg._nome_valido("Pessoa")
    assert not cg._nome_valido("")
    assert not cg._nome_valido("0")
    assert not cg._nome_valido("   ")
    assert not cg._nome_valido("123")
    assert not cg._nome_valido("lead")
    assert not cg._nome_valido(None)


def test_templates_msg_nao_recumprimentam():
    """Nenhum texto do {{2}} pode começar com saudação (o {{1}} já cumprimenta)."""
    saudacoes = ("oi", "olá", "ola", "e aí", "bom dia", "boa tarde", "boa noite")
    for idx, txt in cadencia._TEMPLATES_MSG.items():
        low = txt.strip().lower()
        assert not any(low.startswith(s) for s in saudacoes), (
            f"toque {idx} recumprimenta: {txt!r}"
        )
    rem = cadencia._TEMPLATE_REMARKETING.strip().lower()
    assert not any(rem.startswith(s) for s in saudacoes), "remarketing recumprimenta"


def test_nome_para_template_usa_first_name_explicito():
    assert cg._nome_para_template("+5500000000000", "José Carlos") == "José"


def test_nome_para_template_fallback_neutro_sem_nome():
    # Telefone que não existe em nenhuma tabela → fallback neutro.
    assert cg._nome_para_template("+5500000000000", None) == cg._NOME_FALLBACK


def _rotacao_ola_e_utility(monkeypatch) -> None:
    """Declara a rotação `ola` como UTILITY aprovado, sem perguntar pra Meta.

    Estes dois testes substituem só o POST do envio; a consulta de categoria
    (`cloud_lines`, httpx.get) ia de verdade à Graph com o token da linha da
    Pessoa12. Desde 05/10/2026 o conftest corta essa consulta, e o cenário passa a
    declarar a resposta de que precisa.
    """
    from app.services import cloud_lines as _cl

    monkeypatch.setattr(
        _cl, "templates_utility",
        lambda slug, **kw: {t[0] for t in cg._OLA_ROTATION},
    )
    monkeypatch.setattr(_cl, "templates_marketing", lambda slug, **kw: set())


def test_dry_run_sem_nome_usa_acme_ola_com_fallback_neutro(monkeypatch):
    """Lead SEM nome resolvido (telefone inexistente + first_name=None): o
    acme_oi foi DESATIVADO da rotação (Meta o reclassificou como MARKETING —
    commit 7192ea61; regra da Dona: disparo SÓ UTILITY). No-name agora cai no
    acme_ola com {{1}}=fallback neutro ("tudo bem") e {{2}}=gancho; acme_oi
    NUNCA é tentado. Nenhum template da rotação ativa é MARKETING."""
    tentados: list[dict] = []

    class _FakeResp:
        status_code = 400
        text = "mock: força rotação completa"

    def fake_post(self, url, headers=None, json=None):
        tentados.append(json["template"])
        return _FakeResp()

    _rotacao_ola_e_utility(monkeypatch)
    monkeypatch.setattr(cg, "_dry_run_ativo", lambda: False)
    monkeypatch.setattr(cg, "_token", lambda: "TOK-MASK")
    monkeypatch.setattr(cg.httpx.Client, "post", fake_post)

    with pytest.raises(RuntimeError, match="sem template aprovado"):
        cg.enviar_template_corretor("+5500000000000", None, hook="Passa lá no plantão")

    # 1ª tentativa: acme_ola ({{1}}=fallback neutro, {{2}}=gancho).
    tmpl = tentados[0]
    assert tmpl["name"].startswith("acme_ola")
    params = tmpl["components"][0]["parameters"]
    assert len(params) == 2
    assert params[0]["text"] == cg._NOME_FALLBACK    # "tudo bem" (sem nome)
    assert params[1]["text"] == "Passa lá no plantão"
    # acme_oi fora da rotação inteira (todas as tentativas percorridas).
    assert not any(t["name"].startswith("acme_oi") for t in tentados)
    # Rotação ativa = só acme_ola + acme_reeng, todos UTILITY, sem saud.
    nomes = [t["name"] for t in tentados]
    assert all(n.startswith(("acme_ola", "acme_reeng")) for n in nomes)
    assert not any("saud" in n for n in nomes)


def test_dry_run_com_nome_nao_usa_acme_oi(monkeypatch):
    """Lead COM nome explícito: acme_oi NÃO entra na rotação — a 1ª tentativa é
    o acme_ola personalizado ({{1}}=nome, {{2}}=gancho)."""
    capturado: dict = {}

    def fake_post(self, url, headers=None, json=None):
        capturado["primeiro"] = json["template"]
        raise AssertionError("interrompe após capturar a 1ª tentativa")

    _rotacao_ola_e_utility(monkeypatch)
    monkeypatch.setattr(cg, "_dry_run_ativo", lambda: False)
    monkeypatch.setattr(cg, "_token", lambda: "TOK-MASK")
    monkeypatch.setattr(cg.httpx.Client, "post", fake_post)

    with pytest.raises(AssertionError):
        cg.enviar_template_corretor("+5500000000000", "José Carlos", hook="Passa lá")

    tmpl = capturado["primeiro"]
    assert tmpl["name"].startswith("acme_ola")     # com nome → ola primeiro
    assert not tmpl["name"].startswith("acme_oi")
    params = tmpl["components"][0]["parameters"]
    assert len(params) == 2                           # nome + gancho
    assert params[0]["text"] == "José"


@requires_db
def test_nome_para_template_resolve_pelo_telefone_leads_snapshot():
    tel = "+5500000000000"
    with cg.SessionLocal() as s:
        s.execute(text("DELETE FROM leads_snapshot WHERE lead_id = :l"), {"l": "tt-nome-1"})
        s.execute(
            text(
                "INSERT INTO leads_snapshot (lead_id, nome, telefone, corretora_id, ultima_sync) "
                "VALUES (:l,:n,:t,:c,NOW())"
            ),
            {"l": "tt-nome-1", "n": "José da Silva", "t": tel, "c": CORRETORA_A_ID},
        )
        s.commit()
    try:
        # first_name=None (como a cadência chama) → resolve "José" pelo telefone.
        assert cg._nome_para_template(tel, None) == "José"
        # E o preview renderiza UMA saudação só (sem "tudo bem, tudo bem").
        #
        # A asserção mudou em 04/10/2026. Era `count("tudo bem") == 1`, que embutia QUAL template
        # a rotação devolve: os `*_ola_fu_*` saíram do pool por estarem MARKETING na WABA, e agora
        # o preview pode cair no `b_01`, que abre "Oi {nome}! 😊" e não tem "tudo bem" nenhum —
        # 0 ocorrências, teste vermelho, e a regra de verdade intacta. O que o teste protege é a
        # saudação DUPLICADA, então conta o conjunto de aberturas conhecidas e exige exatamente
        # uma, qualquer que seja o template sorteado.
        prev = cg.preview_template_corretor(tel, None)
        assert "José" in prev["skeleton"]
        texto = prev["skeleton"].lower()
        saudacoes = texto.count("tudo bem") + texto.count("oi josé")
        assert saudacoes == 1, f"esperava 1 saudação, achei {saudacoes} em {texto!r}"
    finally:
        with cg.SessionLocal() as s:
            s.execute(text("DELETE FROM leads_snapshot WHERE lead_id = :l"), {"l": "tt-nome-1"})
            s.commit()


# ── TAREFA 2: nota de indicação R$500 ────────────────────────────────────────


def test_is_ja_comprou_casos_claros_e_negativos():
    for pos in ("já comprei", "Já encontrei outro", "ja fechei", "achei em outra imobiliária",
                "comprei em outro lugar", "já consegui", "ja resolvi"):
        assert cg._is_ja_comprou(pos), pos
    for neg in ("ainda não comprei", "quero comprar", "vou encontrar", "não encontrei nada",
                "ainda tô procurando", "", None):
        assert not cg._is_ja_comprou(neg), neg


def test_montar_nota_indicacao_tem_rotulo_e_brinde():
    nota = cg._montar_nota_indicacao("Corretor")
    assert nota.startswith(cg._NOTA_INDICACAO_ROTULO)
    assert "R$500" in nota
    assert "Corretor" in nota
    # Sem nome → sem vocativo quebrado.
    nota2 = cg._montar_nota_indicacao(None)
    assert "R$500" in nota2


def _seed_lead(lead_id: str, tel: str, nome: str = "Carlos Souza"):
    with cg.SessionLocal() as s:
        s.execute(text("DELETE FROM observacao_corretora WHERE lead_id = :l"), {"l": lead_id})
        s.execute(text("DELETE FROM leads_snapshot WHERE lead_id = :l"), {"l": lead_id})
        s.execute(
            text(
                "INSERT INTO leads_snapshot (lead_id, nome, telefone, corretora_id, ultima_sync) "
                "VALUES (:l,:n,:t,:c,NOW())"
            ),
            {"l": lead_id, "n": nome, "t": tel, "c": CORRETORA_A_ID},
        )
        s.commit()


def _limpa_lead(lead_id: str):
    with cg.SessionLocal() as s:
        s.execute(text("DELETE FROM observacao_corretora WHERE lead_id = :l"), {"l": lead_id})
        s.execute(text("DELETE FROM leads_snapshot WHERE lead_id = :l"), {"l": lead_id})
        s.commit()


@requires_db
def test_intencao_indicacao_cria_nota_e_e_idempotente():
    lead_id, tel = "tt-ind-1", "+5500000000000"
    _seed_lead(lead_id, tel)
    try:
        r1 = cg.tratar_intencao_indicacao(tel, "já comprei em outro lugar, obrigado")
        assert r1 == "criada"
        # 2ª vez NÃO duplica.
        r2 = cg.tratar_intencao_indicacao(tel, "já comprei mesmo")
        assert r2 == "duplicada"
        with cg.SessionLocal() as s:
            n = s.execute(
                text("SELECT count(*), max(texto) FROM observacao_corretora WHERE lead_id = :l"),
                {"l": lead_id},
            ).first()
        assert n[0] == 1
        assert cg._NOTA_INDICACAO_ROTULO[:20] in n[1]
        assert "R$500" in n[1]
        assert "Carlos" in n[1]  # 1º nome do lead
    finally:
        _limpa_lead(lead_id)


@requires_db
def test_intencao_indicacao_ignora_sem_intencao():
    lead_id, tel = "tt-ind-2", "+5500000000000"
    _seed_lead(lead_id, tel)
    try:
        assert cg.tratar_intencao_indicacao(tel, "ainda tô procurando") == "sem_intencao"
        with cg.SessionLocal() as s:
            c = s.execute(
                text("SELECT count(*) FROM observacao_corretora WHERE lead_id = :l"),
                {"l": lead_id},
            ).scalar()
        assert c == 0
    finally:
        _limpa_lead(lead_id)


@requires_db
def test_intencao_indicacao_sem_card_nao_cria():
    # Telefone sem lead no snapshot → não cria nota (nada a que anexar).
    assert cg.tratar_intencao_indicacao("+5500000000000", "já comprei") == "sem_lead"
