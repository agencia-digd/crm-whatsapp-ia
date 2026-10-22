"""Sombra da cadência — testes.

O teste que mais importa é o primeiro: garantir que este caminho NÃO consegue
enviar mensagem. É experimento em cima de conversa real de cliente; se um dia
alguém plugar um sender aqui sem perceber, a suíte quebra.
"""
from __future__ import annotations

import json

from app.inspector import shadow_cadencia as sc


class FakeProvider:
    """Provider de LLM falso — devolve o JSON que a gente mandar."""

    def __init__(self, payload):
        self.payload = payload
        self.chamadas = []

    def complete(self, system, user, schema=None):
        self.chamadas.append({"system": system, "user": user})
        body = self.payload if isinstance(self.payload, str) else json.dumps(self.payload)
        return body, {}


CONVERSA = [
    {"from": "lead", "text": "oi, vi o anúncio do apto na Bairro X", "at": "2026-07-01T10:00:00", "type": "text"},
    {"from": "corretora", "text": "oi! tenho sim, quer ver?", "at": "2026-07-01T10:05:00", "type": "text"},
    {"from": "lead", "text": "quero, mas só depois do dia 20 que eu volto de viagem", "at": "2026-07-01T10:09:00", "type": "text"},
]


# ── 1. A TRAVA: este caminho não envia nada ──────────────────────────────────

def test_modulo_nao_importa_nenhum_sender():
    """Segurança estrutural: o módulo não pode alcançar um sender.

    Não basta 'não chamar' — não pode nem ter acesso. Se alguém importar um
    cliente de WhatsApp aqui, este teste quebra e a pessoa tem que justificar.
    """
    fonte = (sc.__file__ or "").replace(".pyc", ".py")
    with open(fonte, encoding="utf-8") as fh:
        codigo = fh.read()

    proibidos = [
        "enviar_template", "enviar_texto", "send_message", "sendMessage",
        "evolution_client", "cloud_corretora_a", "cloud_assistente", "requests.post",
        "httpx.post", "graph.facebook.com",
    ]
    achados = [p for p in proibidos if p in codigo]
    assert not achados, f"módulo da sombra alcança sender: {achados}"


def test_nao_faz_chamada_de_rede_alem_do_llm(monkeypatch):
    """Só o provider de LLM é chamado. Nenhum outro I/O sai daqui."""
    prov = FakeProvider({
        "corpo": "você tinha dito que voltava depois do dia 20 — já deu pra ver o apto?",
        "usou_contexto": True,
        "contexto_usado": "disse que voltaria de viagem depois do dia 20",
        "motivo": "usei a data que ele mesmo deu",
    })
    res = sc.decidir_sombra_cadencia(
        corretora_nome="Pessoa10", corretora_id=3, lead_nome="João",
        lead_telefone="+5500000000000", toque=2,
        template_atual="Oi João, tudo certo por aí?",
        conversa=CONVERSA, provider=prov,
    )
    assert res is not None
    assert len(prov.chamadas) == 1  # exatamente uma chamada, a do LLM


# ── 2. Comportamento ─────────────────────────────────────────────────────────

def test_usa_contexto_da_conversa():
    prov = FakeProvider({
        "corpo": "você comentou que voltava depois do dia 20 — quer marcar essa semana?",
        "usou_contexto": True,
        "contexto_usado": "volta de viagem depois do dia 20",
        "motivo": "retomo pelo compromisso que ele deu",
    })
    res = sc.decidir_sombra_cadencia(
        corretora_nome="Pessoa10", corretora_id=3, lead_nome="João",
        lead_telefone="+5500000000000", toque=2,
        template_atual="Oi João, tudo certo?", conversa=CONVERSA, provider=prov,
    )
    assert res.usou_contexto is True
    assert res.contexto_usado
    assert res.template_atual == "Oi João, tudo certo?"   # o par é preservado
    assert res.sugestao_ia != res.template_atual


def test_sem_contexto_util_admite_que_e_generica():
    """Dizer 'o template já serve' é resposta honesta e tem que ser possível."""
    prov = FakeProvider({
        "corpo": "ainda está procurando?",
        "usou_contexto": False,
        "contexto_usado": None,
        "motivo": "conversa não tem nada específico",
    })
    res = sc.decidir_sombra_cadencia(
        corretora_nome="Pessoa10", corretora_id=3, lead_nome="João",
        lead_telefone="+5500000000000", toque=1,
        template_atual="Oi João, tudo certo?",
        conversa=[{"from": "lead", "text": "oi", "at": "", "type": "text"}],
        provider=prov,
    )
    assert res.usou_contexto is False
    assert res.contexto_usado is None


def test_diz_que_usou_contexto_mas_nao_aponta_qual_vira_generica():
    """Anti-alucinação: alegar contexto sem dizer QUAL não conta como contexto."""
    prov = FakeProvider({
        "corpo": "lembrei de você!",
        "usou_contexto": True,
        "contexto_usado": None,          # alegou mas não apontou
        "motivo": "achei que tinha algo",
    })
    res = sc.decidir_sombra_cadencia(
        corretora_nome="Pessoa10", corretora_id=3, lead_nome="João",
        lead_telefone="+5500000000000", toque=1,
        template_atual="Oi João?", conversa=CONVERSA, provider=prov,
    )
    assert res.usou_contexto is False


# ── 3. Falha não derruba nada ────────────────────────────────────────────────

def test_llm_quebrado_devolve_none():
    class Explode:
        def complete(self, *a, **k):
            raise RuntimeError("provider fora do ar")

    res = sc.decidir_sombra_cadencia(
        corretora_nome="Pessoa10", corretora_id=3, lead_nome="João",
        lead_telefone="x", toque=1, template_atual="Oi?",
        conversa=CONVERSA, provider=Explode(),
    )
    assert res is None


def test_json_invalido_devolve_none():
    res = sc.decidir_sombra_cadencia(
        corretora_nome="Pessoa10", corretora_id=3, lead_nome="João",
        lead_telefone="x", toque=1, template_atual="Oi?",
        conversa=CONVERSA, provider=FakeProvider("isso não é json"),
    )
    assert res is None


def test_conversa_vazia_devolve_none():
    res = sc.decidir_sombra_cadencia(
        corretora_nome="Pessoa10", corretora_id=3, lead_nome="João",
        lead_telefone="x", toque=1, template_atual="Oi?",
        conversa=[], provider=FakeProvider({"corpo": "oi?"}),
    )
    assert res is None


def test_mensagem_vazia_devolve_none():
    """Sem mensagem não há par pra julgar — não grava lixo."""
    res = sc.decidir_sombra_cadencia(
        corretora_nome="Pessoa10", corretora_id=3, lead_nome="João",
        lead_telefone="x", toque=1, template_atual="Oi?",
        conversa=CONVERSA,
        provider=FakeProvider({"corpo": "   ", "usou_contexto": False}),
    )
    assert res is None


# ── 4. O prompt leva o que precisa ───────────────────────────────────────────

def test_prompt_recebe_template_e_conversa():
    prov = FakeProvider({"corpo": "ainda procurando?", "usou_contexto": False})
    sc.decidir_sombra_cadencia(
        corretora_nome="Pessoa10", corretora_id=3, lead_nome="João",
        lead_telefone="x", toque=2, template_atual="TEMPLATE-MARCADOR",
        conversa=CONVERSA, provider=prov,
    )
    user = prov.chamadas[0]["user"]
    assert "TEMPLATE-MARCADOR" in user            # sabe com o que está competindo
    assert "Bairro X" in user                 # e viu a conversa real
    system = prov.chamadas[0]["system"]
    assert "NUNCA fale de preço" in system        # trava de negociação presente


# ── 5. A restrição da Meta: só a variável muda ───────────────────────────────

def test_prompt_explica_que_so_a_variavel_muda():
    """A Dona corrigiu (29/07): fora da janela 24h só sai template UTILITY
    aprovado. A IA escreve o miolo {{2}}, nunca a mensagem inteira."""
    prov = FakeProvider({"corpo": "ainda procurando?", "usou_contexto": False})
    sc.decidir_sombra_cadencia(
        corretora_nome="Pessoa10", corretora_id=3, lead_nome="João",
        lead_telefone="x", toque=1, template_atual="Oi?",
        conversa=CONVERSA, provider=prov,
    )
    system = prov.chamadas[0]["system"]
    assert "TEMPLATE UTILITY" in system
    assert "NÃO escreva saudação" in system
    assert "NÃO escreva despedida" in system


def test_detecta_saudacao_no_miolo():
    """Miolo com 'Oi' duplicaria a casca ('Olá X, tudo bem? Oi X!')."""
    assert sc._parece_ter_saudacao("Oi João, tudo certo?") is True
    assert sc._parece_ter_saudacao("Olá! ainda procurando?") is True
    assert sc._parece_ter_saudacao("Bom dia, viu o apto?") is True
    assert sc._parece_ter_saudacao("ainda está procurando na Bairro X?") is False
    assert sc._parece_ter_saudacao("você comentou que voltava dia 20 — vamos ver?") is False


# ── 6. Quebra de linha dentro de variável (regra da Meta) ────────────────────

def test_sanitiza_quebra_de_linha_para_vertical_tab():
    """A Meta rejeita \\n dentro de variável; só U+000B passa e o WhatsApp
    renderiza como quebra. Regra confirmada 21/06/2026 (skill
    templates-utility-api). Sanitizamos na saída em vez de confiar no modelo."""
    out = sc.sanitizar_variavel("primeira linha\nsegunda linha")
    assert "\n" not in out
    assert "\v" in out
    assert out == "primeira linha\vsegunda linha"


def test_sanitiza_tab_e_espacos_multiplos():
    """Tab e 4+ espaços também são rejeitados pela Meta."""
    assert "\t" not in sc.sanitizar_variavel("a\tb")
    assert "    " not in sc.sanitizar_variavel("a        b")
    assert sc.sanitizar_variavel("a\tb") == "a b"


def test_sanitiza_colapsa_paragrafo_duplo_e_apara_bordas():
    assert sc.sanitizar_variavel("a\n\n\nb") == "a\vb"
    assert sc.sanitizar_variavel("\n oi \n") == "oi"


def test_corpo_da_ia_sai_sanitizado():
    """Fim a fim: se o modelo mandar \\n, o resultado já vem em U+000B."""
    prov = FakeProvider({
        "corpo": "a obra começa em agosto.\nAinda quer visitar?",
        "usou_contexto": True, "contexto_usado": "obra em agosto",
        "motivo": "x",
    })
    res = sc.decidir_sombra_cadencia(
        corretora_nome="Pessoa10", corretora_id=3, lead_nome="João",
        lead_telefone="x", toque=2, template_atual="Oi?",
        conversa=CONVERSA, provider=prov,
    )
    assert "\n" not in res.sugestao_ia
    assert "\v" in res.sugestao_ia


def test_prompt_pede_texto_curto():
    """A Dona: 'aconselho não escrever muita coisa'."""
    prov = FakeProvider({"corpo": "ainda procurando?", "usou_contexto": False})
    sc.decidir_sombra_cadencia(
        corretora_nome="Pessoa10", corretora_id=3, lead_nome="João",
        lead_telefone="x", toque=1, template_atual="Oi?",
        conversa=CONVERSA, provider=prov,
    )
    system = prov.chamadas[0]["system"]
    assert "MÁXIMO 2 frases" in system
    assert "Não use quebra de linha" in system


# ── 7. "Tentei te ligar" é fato, não enfeite (Dona 29/07) ────────────────────

def test_toque_pos_ligacao_recebe_trava_do_telefonema():
    """Toques 2/3/5 vêm depois de uma tarefa de LIGAÇÃO real (D+1 e D+5).
    A corretora ligou mesmo — a IA não pode apagar esse enquadramento."""
    for toque in (2, 3, 5):
        prov = FakeProvider({"corpo": "consegui te pegar num horário melhor?", "usou_contexto": False})
        sc.decidir_sombra_cadencia(
            corretora_nome="Pessoa", corretora_id=3, lead_nome="João",
            lead_telefone="x", toque=toque, template_atual="Tentei te ligar...",
            conversa=CONVERSA, provider=prov,
        )
        system = prov.chamadas[0]["system"]
        assert "TENTOU LIGAR" in system, f"toque {toque} sem a trava do telefonema"


def test_toque_sem_ligacao_nao_recebe_a_trava():
    """Toque 0 é o primeiro contato — não houve ligação antes."""
    prov = FakeProvider({"corpo": "ainda procurando?", "usou_contexto": False})
    sc.decidir_sombra_cadencia(
        corretora_nome="Pessoa", corretora_id=3, lead_nome="João",
        lead_telefone="x", toque=0, template_atual="Vi que sua busca parou...",
        conversa=CONVERSA, provider=prov,
    )
    assert "TENTOU LIGAR" not in prov.chamadas[0]["system"]


def test_toques_pos_ligacao_batem_com_a_cadencia_real():
    """Guarda contra a cadência mudar e a lista ficar desatualizada."""
    from app.services import cadencia as cad

    ligacoes = {t.indice for t in cad.TOQUES if cad._TEMPLATES_MSG.get(t.indice) is None}
    for toque in sc.TOQUES_POS_LIGACAO:
        anteriores = [i for i in ligacoes if i < toque]
        assert anteriores, f"toque {toque} marcado como pós-ligação, mas nenhuma ligação vem antes"
