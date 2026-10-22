"""Atendimento da linha da Corretor — a Pessoa responde quem respondeu o carrossel.

O que protege, em ordem de dano:
1. Quem pediu pra PARAR nunca é atendido (atender depois disso = denúncia).
2. A resposta sai pela linha DA CORRETOR (outro número = cara de golpe).
3. Promoção pro funil de vendas é idempotente (não puxa lead de volta pra "Novo").
4. Nada disso derruba o webhook.
"""
from __future__ import annotations

import pytest

from app.services import atendimento_corretor as ab

# Guardado no import, ANTES da fixture trocar por um lambda: os testes que
# checam a função de verdade precisam da original.
_E_DA_RODA_REAL = ab._e_da_roda
_NOS_ABORDAMOS_REAL = ab._nos_abordamos_na_mao


@pytest.fixture(autouse=True)
def _ligado(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "atendimento_corretor_ativo", True, raising=False)
    # Default dos testes: o contato É da roda do carrossel — é o caminho normal
    # do atendimento. Quem testa o contrário sobrescreve explicitamente. Sem
    # isto cada teste bateria no banco e cairia em "fora_da_roda".
    monkeypatch.setattr(ab, "_e_da_roda", lambda t: True)
    monkeypatch.setattr(ab, "_nos_abordamos_na_mao", lambda t: False)
    yield


# ── 1. Travas de entrada ────────────────────────────────────────────────────

def test_desligado_nao_atende(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "atendimento_corretor_ativo", False, raising=False)
    monkeypatch.setattr(ab, "_rodar_pessoa", lambda *a: pytest.fail("rodou desligada"))
    assert ab.atender("+5500000000000", "oi")["motivo"] == "desligado"


def test_flag_nasce_desligada():
    """Default do CÓDIGO, não do .env: máquina nova / deploy novo não sai
    respondendo lead sozinho. Quem liga é a Dona, no .env."""
    from app.config import Settings

    assert Settings.model_fields["atendimento_corretor_ativo"].default is False


def test_audio_nao_e_atendido(monkeypatch):
    """Sem texto a Pessoa não tem o que ler — humano assume, não inventa resposta."""
    monkeypatch.setattr(ab, "_rodar_pessoa", lambda *a: pytest.fail("rodou sem texto"))
    assert ab.atender("+5500000000000", None)["motivo"] == "sem_texto"
    assert ab.atender("+5500000000000", "   ")["motivo"] == "sem_texto"


def test_opt_out_nao_chega_no_atendimento():
    """O webhook precisa BARRAR o opt-out antes de chamar a Pessoa."""
    import inspect

    from app.api import cloud_corretora

    fonte = inspect.getsource(cloud_corretora.corretora_inbound)
    # (05/10/2026) a resposta automática do aparelho do lead também não chega
    # na Pessoa — as duas saídas barram juntas.
    # E quem diz que o número é de outra pessoa (05/10) também não.
    assert 'saida not in ("opt_out", "automatica", "numero_errado")' in fonte
    assert fonte.index("processar_inbound") < fonte.index("atendimento_corretor.atender"), \
        "o opt-out tem que ser decidido ANTES de atender"


def test_duas_mensagens_juntas_nao_geram_duas_respostas(monkeypatch):
    """Lead mandando 3 mensagens seguidas não pode receber 3 respostas."""
    tel = "+5500000000000"
    lock = ab._lock_do(tel)
    lock.acquire()
    try:
        monkeypatch.setattr(ab, "_rodar_pessoa", lambda *a: pytest.fail("rodou em paralelo"))
        assert ab.atender(tel, "oi")["motivo"] == "ja_atendendo"
    finally:
        lock.release()


# ── 2. A resposta sai pela linha certa ──────────────────────────────────────

def test_responde_pela_linha_da_corretor(monkeypatch):
    from app.api import cloud_corretora_a as corretor
    from app.services import cloud_lines as cl

    vistos = {}
    monkeypatch.setattr(cl, "line_by_slug",
                        lambda s: type("L", (), {"slug": s, "phone_id": "X"})())
    monkeypatch.setattr(cl, "instance_do_slug", lambda s: "inst")
    monkeypatch.setattr(corretor, "enviar_cloud",
                        lambda tel, txt, **kw: vistos.update(kw, tel=tel))
    ab._responder("+5500000000000", "oi")
    assert vistos["linha"].slug == "corretor"
    assert vistos["corretora_id"] == 4


def test_sem_linha_nao_responde_pela_corretor(monkeypatch):
    from app.services import cloud_lines as cl

    monkeypatch.setattr(cl, "line_by_slug", lambda s: None)
    with pytest.raises(RuntimeError, match="corretor"):
        ab._responder("+5500000000000", "oi")


def test_le_a_chave_certa_do_retorno_da_pessoa(monkeypatch):
    """Contrato com AgentePessoa.run: a fala vem em `response_text`.

    Este teste existe porque a primeira versão lia `text` e caía num
    `str(out)` — o lead teria recebido o dict Python inteiro no WhatsApp.
    """
    class MariaFake:
        def __init__(self, **kw): pass
        def run(self, msg): return {"response_text": "oi, tudo bem? 😊",
                                    "tool_calls": []}

    import app.agents.pessoa.agent as ma

    monkeypatch.setattr(ma, "AgentePessoa", MariaFake)
    assert ab._rodar_pessoa("5500000000000", "oi", None, []) == "oi, tudo bem? 😊"


def test_retorno_estranho_nao_vira_mensagem(monkeypatch):
    """Se o contrato quebrar, cala a boca — não manda lixo pro lead."""
    class MariaMuda:
        def __init__(self, **kw): pass
        def run(self, msg): return "string solta"

    import app.agents.pessoa.agent as ma

    monkeypatch.setattr(ma, "AgentePessoa", MariaMuda)
    assert ab._rodar_pessoa("5500000000000", "oi", None, []) is None


# ── 3. Promoção pro funil de vendas ─────────────────────────────────────────

def test_promove_pra_corretora_a_em_atendimento():
    """12/08: o destino virou "Em atendimento" (7), não "Novo" (6).

    O lead que a Pessoa promove NÃO é novo — a mensagem de abertura já saiu pela
    linha da Pessoa12 no mesmo instante. Em "Novo" ele apareceria como alguém pra
    abordar, e a Pessoa12 abordaria do zero quem ela já abordou.

    Lead do SITE continua em "Novo": esse ninguém falou com ele ainda."""
    assert ab.STAGE_ENTROU == 7
    assert ab.STAGE_NOVO == 6
    assert ab.CORRETORA_CORRETORA_A == 3


def test_promocao_so_tira_de_quem_esta_no_remarketing():
    """Idempotente: lead que já avançou pra 'Visita marcada' não volta pra 'Novo'."""
    import inspect

    fonte = inspect.getsource(ab.promover_para_vendas)
    assert "ps.funil = 'remarketing'" in fonte


def test_promove_so_apos_troca_de_verdade():
    """Responder o disparo é resposta; VOLTAR a falar depois da Pessoa é interação."""
    assert ab.MSGS_PRA_PROMOVER == 2


def test_promocao_falha_nao_derruba(monkeypatch):
    from app.db import session as sess

    monkeypatch.setattr(sess, "SessionLocal",
                        lambda: (_ for _ in ()).throw(RuntimeError("banco fora")))
    assert ab.promover_para_vendas("+5500000000000") is False


# ── 4. Nada disso derruba o inbound ─────────────────────────────────────────

def test_pessoa_explodindo_nao_levanta(monkeypatch):
    monkeypatch.setattr(ab, "_rodar_pessoa",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("llm fora")))
    monkeypatch.setattr(ab, "promover_para_vendas", lambda *a, **k: False)
    assert ab.atender("+5500000000000", "oi")["motivo"] == "erro"


# ── 5. Avaliação vai pra Pessoa12 (Dona 30/07) ────────────────────────────────────

def test_avaliacao_e_uma_tool_que_a_pessoa_pode_chamar():
    """Sem estar no enum do schema, o LLM não consegue emitir o nome da tool —
    a regra existiria na SOUL e nunca rodaria."""
    from app.agents.pessoa.agent import RESPONSE_SCHEMA, TOOL_HANDLERS

    assert "encaminhar_avaliacao" in TOOL_HANDLERS
    enum = RESPONSE_SCHEMA["properties"]["tool_call"]["properties"]["name"]["enum"]
    assert "encaminhar_avaliacao" in enum


def test_avaliacao_vai_pra_corretora_a():
    from app.agents.pessoa import agent as ma

    assert ma.HANDOFF_CORRETORA_ID == ab.CORRETORA_CORRETORA_A == 3


def test_avaliacao_tira_o_lead_do_carrossel():
    """Quem quer VENDER não pode seguir recebendo 'ainda procurando imóvel?'."""
    import inspect

    fonte = inspect.getsource(ma_tool := __import__(
        "app.agents.pessoa.agent", fromlist=["x"])._tool_encaminhar_avaliacao)
    assert "promover_para_vendas" in fonte


def test_avaliacao_sem_telefone_nao_persiste():
    from app.agents.pessoa.agent import _tool_encaminhar_avaliacao

    r = _tool_encaminhar_avaliacao(nome="Corretor", imovel="apto")
    assert r["encaminhado"] is False


def test_pessoa_recebe_o_telefone_na_tool_de_avaliacao():
    """O LLM não sabe (nem deve inventar) o telefone — ele vem do contexto."""
    import inspect

    from app.agents.pessoa.agent import AgentePessoa

    fonte = inspect.getsource(AgentePessoa._execute_tool)
    assert "encaminhar_avaliacao" in fonte and "_phone" in fonte


# ── 6. Nem equipe nem mídia (achados nas conversas reais de 30/07) ──────────

@pytest.mark.parametrize("marcador", ["[audio]", "[image]", " [IMAGE] ", "[figurinha]"])
def test_midia_pura_nao_e_atendida(monkeypatch, marcador):
    """A Pessoa responde a "[image]" inventando contexto — em 30/07 emendou
    "vi que você tem interesse em imóveis" numa foto que ela não viu."""
    monkeypatch.setattr(ab, "_rodar_pessoa", lambda *a: pytest.fail("rodou com mídia"))
    assert ab.atender("+5500000000000", marcador)["motivo"] == "midia"


def test_texto_com_colchete_ainda_e_atendido(monkeypatch):
    """Só o marcador SOZINHO é mídia — não pode calar quem escreveu de verdade."""
    monkeypatch.setattr(ab, "_e_da_equipe", lambda t: False)
    monkeypatch.setattr(ab, "_rodar_pessoa", lambda *a: "oi")
    monkeypatch.setattr(ab, "_responder", lambda *a: None)
    monkeypatch.setattr(ab, "promover_para_vendas", lambda *a, **k: False)
    assert ab.atender("+5500000000000", "mandei [audio] antes, ouviu?")["atendeu"]


def test_numero_de_corretora_nao_e_atendido(monkeypatch):
    """O número da Pessoa11 estava na base e recebeu disparo — a Pessoa começou a
    qualificar a própria corretora como lead."""
    monkeypatch.setattr(ab, "_e_da_equipe", lambda t: True)
    monkeypatch.setattr(ab, "_rodar_pessoa", lambda *a: pytest.fail("atendeu equipe"))
    assert ab.atender("+5500000000000", "oi")["motivo"] == "equipe"


def test_banco_fora_nao_bloqueia_lead_real(monkeypatch):
    """Fail-open aqui de propósito: calar lead de verdade é pior que atender
    um número da equipe por engano."""
    from app.db import session as sess

    monkeypatch.setattr(sess, "SessionLocal",
                        lambda: (_ for _ in ()).throw(RuntimeError("fora")))
    assert ab._e_da_equipe("+5500000000000") is False


def test_fila_exclui_numero_da_equipe():
    from app.services import carrossel_corretor as cb

    assert "FROM corretoras c" in cb._SQL_FILA


# ── 7. Não-comprador sai da roda (Dona 30/07, lendo as conversas reais) ─────

def test_encerrar_nao_e_lead_e_chamavel():
    from app.agents.pessoa.agent import RESPONSE_SCHEMA, TOOL_HANDLERS

    assert "encerrar_nao_e_lead" in TOOL_HANDLERS
    enum = RESPONSE_SCHEMA["properties"]["tool_call"]["properties"]["name"]["enum"]
    assert "encerrar_nao_e_lead" in enum


def test_nao_e_lead_sai_do_funil_de_remarketing():
    """Sem sair, o contato volta pra Fila e recebe disparo de novo em 30 dias."""
    assert ab.STAGE_NAO_E_LEAD == 13


def test_nao_e_lead_nao_vira_opt_out():
    """A pessoa não PEDIU pra parar. Misturar apaga a diferença entre 'não quis'
    e 'não era pra ser' — e opt-out é registro do que o lead pediu."""
    import inspect

    fonte = inspect.getsource(ab.marcar_nao_e_lead)
    assert "opt_out" not in fonte.lower().replace("não grava opt-out", "")


def test_nao_e_lead_falha_nao_derruba(monkeypatch):
    from app.db import session as sess

    monkeypatch.setattr(sess, "SessionLocal",
                        lambda: (_ for _ in ()).throw(RuntimeError("fora")))
    assert ab.marcar_nao_e_lead("+5500000000000", "x") is False


def test_soul_manda_aceitar_o_nao_na_primeira():
    """Insistir com quem disse não é o caminho curto pro bloqueio."""
    from pathlib import Path

    soul = Path("app/agents/pessoa/SOUL.md").read_text(encoding="utf-8")
    assert "Aceite na primeira vez" in soul
    assert "encerrar_nao_e_lead" in soul


# ── 5. Caso Fulano (31/07): recusa educada + ❤️ viraram lead ────────────
# O lead disse "no momento não, mas obrigado", a Pessoa se despediu, ele reagiu
# com um coração à despedida — e o coração contou como 2ª mensagem, promovendo
# ele pra Pessoa3 como lead quente. Duas travas nasceram daqui.

def test_reacao_nao_e_conversa():
    """Reagir com emoji não é responder — nem conta pra promoção."""
    for marcador in ("[reaction]", "[reação: ❤️]", "[sticker]",
                     "[contato compartilhado: Zé]"):
        assert ab._e_so_midia(marcador), f"{marcador} deveria ser ignorado"
    assert not ab._e_so_midia("Olá, quero ver o apartamento")


def test_reacao_nao_promove(monkeypatch):
    monkeypatch.setattr(ab.settings, "atendimento_corretor_ativo", True)
    monkeypatch.setattr(ab, "promover_para_vendas",
                        lambda *a, **k: pytest.fail("promoveu por uma reação"))
    assert ab.atender("+5500000000000", "[reaction]")["motivo"] == "midia"


def test_recusa_educada_nao_vira_lead():
    """'No momento não' não é opt-out (segue na lista), mas não é lead."""
    assert ab._e_recusa_branda("Olá, boa tarde\n\nNo momento não, mas obrigado pelo contato")
    assert ab._e_recusa_branda("não tenho interesse")
    assert ab._e_recusa_branda("já comprei")
    # o que NÃO pode ser confundido com recusa — preferência de imóvel é conversa
    assert not ab._e_recusa_branda("quero um apê para minha mãe")
    assert not ab._e_recusa_branda("não quero esse bairro")
    assert not ab._e_recusa_branda("Quero sim! Pode mandar")


def test_quem_recusou_antes_nunca_e_promovido(monkeypatch):
    """A recusa vem na 1ª msg e a promoção dispararia na 2ª — olha o histórico."""
    monkeypatch.setattr(ab.settings, "atendimento_corretor_ativo", True)
    monkeypatch.setattr(ab, "_rodar_pessoa", lambda *a: None)
    monkeypatch.setattr(ab, "promover_para_vendas",
                        lambda *a, **k: pytest.fail("promoveu quem já tinha dito não"))
    # Chave `text`: é o que `load_history` devolve. Este fixture usava "content"
    # — a mesma chave errada que o código lia — então o teste passava verde
    # enquanto a produção ignorava o histórico inteiro (corrigido 08/09).
    historico = [{"role": "user", "text": "No momento não, mas obrigado pelo contato"},
                 {"role": "assistant", "text": "Imagina! Se voltar a procurar, me chama."}]
    monkeypatch.setattr("app.services.conversations.load_history", lambda *a, **k: historico)
    monkeypatch.setattr("app.services.conversations.append_message", lambda *a, **k: None)
    assert ab.atender("+5500000000000", "obrigado você também")["promoveu"] is False


# ── 6. Robô não fala por cima de gente (Dona 31/07) ─────────────────────────

def test_humano_falando_barra_a_pessoa(monkeypatch):
    monkeypatch.setattr(ab.settings, "atendimento_corretor_ativo", True)
    monkeypatch.setattr(ab, "_humano_falando", lambda tel, desde=None: True)
    monkeypatch.setattr(ab, "_rodar_pessoa",
                        lambda *a: pytest.fail("respondeu por cima do humano"))
    assert ab.atender("+5500000000000", "oi")["motivo"] == "humano_falando"


def test_banco_fora_nao_cala_o_atendimento(monkeypatch):
    """Na dúvida a Pessoa atende: perder lead real é pior que sobrepor."""
    from app.db import session as sess
    monkeypatch.setattr(sess, "SessionLocal",
                        lambda: (_ for _ in ()).throw(RuntimeError("banco fora")))
    assert ab._humano_falando("+5500000000000") is False


def test_a_fila_do_carrossel_tem_a_mesma_trava():
    """Barrar só no atendimento deixaria o disparo sair mesmo assim."""
    from app.services import carrossel_corretor as cb_
    assert "mensagens_whatsapp m" in cb_._SQL_FILA
    assert ":pausa_dias" in cb_._SQL_FILA
    # o critério é o MESMO dos dois lados: saída sem carimbo de robô
    assert "'origem'" in cb_._SQL_FILA and "'pessoa'" in cb_._SQL_FILA


def test_pessoa_carimba_o_que_manda():
    """Sem o carimbo, a própria resposta da Pessoa seria lida como humana e ela
    se calaria sozinha na mensagem seguinte."""
    import inspect
    fonte = inspect.getsource(ab._responder)
    assert "origem=ORIGEM_PESSOA" in fonte


def test_saida_sem_carimbo_e_humana():
    """A regra que define 'humano falou' — travada aqui pra não derivar."""
    sql = ab._SQL_HUMANO_FALOU
    assert "from_corretora" in sql
    assert "conteudo_metadata->>'origem'" in sql
    assert "tipo_mensagem <> 'template'" in sql, "disparo do carrossel é robô"


# ── 8. Não repetir pergunta (medido em 31/07: nome perguntado 5x) ───────────

_HIST = [
    {"role": "assistant", "text": "Oi! 😊 Sou a Pessoa. Como posso te chamar?"},
    {"role": "assistant",
     "text": "Pra te indicar as melhores opções, me conta: você está "
             "procurando pra morar ou investir?"},
]


@pytest.mark.parametrize("resposta", [
    "Boa tarde! 😊 E como posso te chamar?",
    "Que ótimo! 😊 Como posso te chamar?",
    "Sim, tudo certo! 😊 Pra morar ou investir?",
    "Ótimo! 😊 E você está procurando pra morar ou investir?",
])
def test_corta_pergunta_ja_feita(resposta):
    novo = ab._sem_pergunta_repetida(resposta, _HIST, "5599")
    assert novo != resposta, f"deixou repetir: {resposta}"


@pytest.mark.parametrize("resposta", [
    "Legal! 😊 Tem alguma região de Cidade em mente?",
    "Perfeito! 😊 Quantos quartos você precisa?",
    "Entendi! 😊 Você já tem valor de entrada separado?",
    "São lançamentos aqui em Cidade! 😊 Prefere pronto ou na planta?",
])
def test_pergunta_nova_passa_intacta(resposta):
    """Trava agressiva demais mataria a qualificação inteira."""
    assert ab._sem_pergunta_repetida(resposta, _HIST, "5599") == resposta


def test_primeira_vez_sempre_passa():
    r = "Oi! 😊 Como posso te chamar?"
    assert ab._sem_pergunta_repetida(r, [], "5599") == r


def test_nunca_sobra_beco_sem_saida():
    """Cortar a pergunta pode deixar só "Boa tarde! 😊" — o lead não tem o que
    responder e a conversa morre. Toda mensagem termina com um avanço.

    O avanço é convite ("me conta..."), não pergunta: exigir "?" seria trocar
    o tom dela por uma regra de pontuação.
    """
    for r in ("Boa tarde! 😊 Como posso te chamar?", "Como posso te chamar?"):
        novo = ab._sem_pergunta_repetida(r, _HIST, "5599")
        assert ab._AVANCO in novo, f"virou beco sem saída: {novo!r}"


def test_contencao_e_nao_jaccard():
    """A repetição vem ENCURTADA: "Pra morar ou investir?" está inteira dentro
    da pergunta longa original. Jaccard dava 0.3 e deixava passar."""
    assert ab._mesma_pergunta(
        "Pra morar ou investir?",
        "me conta: você está procurando pra morar ou investir?")


def test_pergunta_de_uma_palavra_nao_casa_com_tudo():
    assert not ab._mesma_pergunta("Certo?", "Quantos quartos você precisa?")


def test_soul_manda_responder_antes_de_perguntar():
    from pathlib import Path

    soul = Path("app/agents/pessoa/SOUL.md").read_text(encoding="utf-8")
    assert "RESPONDA o que a pessoa falou" in soul
    assert "O nome se pergunta UMA vez" in soul


# ── 9. A Pessoa só atende quem O CARROSSEL abordou (31/07) ──────────────────

def test_busca_da_corretor_nao_e_atendida(monkeypatch):
    """O 0000 é a linha DA CASA: a Pessoa3 prospecta por ela na mão. Em 31/07
    saíram 113 mensagens humanas contra 36 da Pessoa, e o robô entrou como
    "Pessoa" em conversas abertas como "Pessoa3" — 5 acabaram apagadas."""
    monkeypatch.setattr(ab, "_e_da_equipe", lambda t: False)
    monkeypatch.setattr(ab, "_e_da_roda", lambda t: False)
    monkeypatch.setattr(ab, "_nos_abordamos_na_mao", lambda t: True)
    monkeypatch.setattr(ab, "_rodar_pessoa",
                        lambda *a: pytest.fail("atendeu conversa da Pessoa12"))
    assert ab.atender("+5500000000000", "oi")["motivo"] == "conversa_da_corretor"


def test_da_roda_e_atendido(monkeypatch):
    monkeypatch.setattr(ab, "_e_da_equipe", lambda t: False)
    monkeypatch.setattr(ab, "_e_da_roda", lambda t: True)
    monkeypatch.setattr(ab, "_humano_falando", lambda t, desde=None: False)
    monkeypatch.setattr(ab, "_rodar_pessoa", lambda *a: "oi")
    monkeypatch.setattr(ab, "_responder", lambda *a: None)
    monkeypatch.setattr(ab, "promover_para_vendas", lambda *a, **k: False)
    assert ab.atender("+5500000000000", "quero ver")["atendeu"]


def test_lead_do_site_e_atendido(monkeypatch):
    """O 0000 também recebe lead do site. Ele não está na roda do carrossel —
    se a trava olhasse só a roda, o lead novo ficaria sem resposta."""
    monkeypatch.setattr(ab, "_e_da_equipe", lambda t: False)
    monkeypatch.setattr(ab, "_e_da_roda", lambda t: False)
    monkeypatch.setattr(ab, "_nos_abordamos_na_mao", lambda t: False)
    monkeypatch.setattr(ab, "_humano_falando", lambda t, desde=None: False)
    monkeypatch.setattr(ab, "_rodar_pessoa", lambda *a: "oi")
    monkeypatch.setattr(ab, "_responder", lambda *a: None)
    monkeypatch.setattr(ab, "promover_para_vendas", lambda *a, **k: False)
    assert ab.atender("+5500000000000", "vi um imóvel no site de vocês")["atendeu"]


def test_contato_novo_nao_e_busca_da_corretor(monkeypatch):
    """Sem histórico = pessoa chegando agora. Tratar como busca da Pessoa12 calaria
    todo lead novo do site."""
    from app.db import session as sess

    class _S:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, *a, **k):
            class R:
                def first(self_inner): return None
            return R()
    monkeypatch.setattr(sess, "SessionLocal", lambda: _S())
    assert _NOS_ABORDAMOS_REAL("+5500000000000") is False


def test_carrossel_nao_conta_como_abordagem_da_corretor(monkeypatch):
    """A primeira mensagem nossa pode ser o TEMPLATE do carrossel — isso é a
    Pessoa, não a Pessoa12."""
    from app.db import session as sess

    class _S:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, *a, **k):
            class R:
                def first(self_inner): return (True, "[reengajamento: oi]", None)
            return R()
    monkeypatch.setattr(sess, "SessionLocal", lambda: _S())
    assert _NOS_ABORDAMOS_REAL("+5500000000000") is False


def test_quem_abriu_falha_CALANDO(monkeypatch):
    from app.db import session as sess

    monkeypatch.setattr(sess, "SessionLocal",
                        lambda: (_ for _ in ()).throw(RuntimeError("fora")))
    assert _NOS_ABORDAMOS_REAL("+5500000000000") is True


def test_roda_checada_antes_de_gastar_llm():
    """A checagem tem que vir ANTES do lock e da chamada do modelo."""
    import inspect

    fonte = inspect.getsource(ab.atender)
    assert fonte.index("_e_da_roda") < fonte.index("_lock_do")


def test_fora_da_roda_falha_CALANDO(monkeypatch):
    """Fail-CLOSED, ao contrário do `_e_da_equipe`. O risco não é simétrico:
    não atender custa uma espera; atender por cima da Pessoa3 faz o lead ouvir
    dois nomes do mesmo número."""
    from app.db import session as sess

    monkeypatch.setattr(sess, "SessionLocal",
                        lambda: (_ for _ in ()).throw(RuntimeError("fora")))
    assert _E_DA_RODA_REAL("+5500000000000") is False


def test_quem_respondeu_e_saiu_do_funil_continua_atendido():
    """Quem responde vai pro funil de VENDAS e some do remarketing — mas segue
    sendo conversa da Pessoa. Por isso o segundo critério (teve disparo)."""
    import inspect

    fonte = inspect.getsource(_E_DA_RODA_REAL)
    assert "_SQL_TEVE_DISPARO" in fonte


# ── 10. Lead do site vira card na Pessoa3 (Dona 31/07) ────────────────────

def test_lead_novo_e_criado_na_corretora_a(monkeypatch):
    """O 0000 recebe lead do site e eles NÃO existem no CRM: em 31/07 a Pessoa
    conversou com dois e nada ficou registrado — nem card, nem dono."""
    criados = {}

    class _S:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, params=None):
            txt = str(sql)
            class R:
                def fetchall(self_inner):
                    if "UPDATE leads l" in txt:
                        return []                      # não estava no remarketing
                    if "INSERT INTO leads" in txt:
                        criados.update(params or {})
                        return [(999,)]
                    return []

                def first(self_inner):
                    # A checagem de duplicidade ("SELECT 1 FROM leads ... suf")
                    # entrou depois deste teste. Sem `.first` o stub estourava
                    # AttributeError, a função engolia a exceção e devolvia
                    # False — falha que parecia regra de negócio e era stub velho.
                    return None                        # nenhum lead com esse final
            return R()
        def commit(self): pass

    from app.db import session as sess

    monkeypatch.setattr(sess, "SessionLocal", lambda: _S())
    monkeypatch.setattr(ab, "_e_da_equipe", lambda t: False)
    assert ab.promover_para_vendas("+5500000000000", "Corretor") is True
    assert criados["corretor"] == ab.CORRETORA_CORRETORA_A
    # Lead do SITE entra em "Novo" (6) — ninguém falou com ele ainda. Só a
    # promoção da Pessoa vai pra "Em atendimento" (7).
    assert criados["entrou"] == ab.STAGE_NOVO
    assert criados["origem"] == ab.ORIGEM_SITE


def test_criacao_e_idempotente():
    """Duas mensagens quase simultâneas não podem virar dois cards."""
    import inspect

    fonte = inspect.getsource(ab._criar_lead_do_site)
    assert "ON CONFLICT (tenant_id, external_ref) DO NOTHING" in fonte
    assert 'f"corretor:{suf}"' in fonte


def test_nao_cria_card_pra_numero_da_equipe(monkeypatch):
    monkeypatch.setattr(ab, "_e_da_equipe", lambda t: True)
    assert ab._criar_lead_do_site(None, "91098596", "+5500000000000", None, 1) == []


def test_quem_ja_tem_card_nao_ganha_outro():
    """Só cria quando o UPDATE não achou nada — lead em 'Visita marcada' não
    volta pra 'Novo' nem ganha card duplicado."""
    import inspect

    fonte = inspect.getsource(ab.promover_para_vendas)
    assert "if not movidos:" in fonte


# ── 10. Fim de linha: depois do handoff, a Pessoa cala (Dona 02/08) ──────────

def test_handoff_feito_cala_a_pessoa(monkeypatch):
    """A conversa continua na linha da Pessoa12. Se a Pessoa seguisse respondendo aqui,
    o lead teria dois atendimentos paralelos em dois números diferentes."""
    monkeypatch.setattr(ab, "_handoff_feito", lambda t: True)
    monkeypatch.setattr(ab, "_rodar_pessoa",
                        lambda *a: pytest.fail("respondeu depois de passar pra Pessoa12"))
    assert ab.atender("+5500000000000", "e aí?")["motivo"] == "handoff_feito"


def test_handoff_com_banco_fora_nao_cala(monkeypatch):
    """Best-effort igual ao `_humano_falando`: Postgres piscando não pode deixar
    lead falando sozinho."""
    from app.db import session as sess

    monkeypatch.setattr(sess, "SessionLocal",
                        lambda: (_ for _ in ()).throw(RuntimeError("banco fora")))
    assert ab._handoff_feito("+5500000000000") is False


def test_handoff_so_vale_o_da_pessoa():
    """Nota de outra origem (recovery, painel) não cala a linha da Corretor — e
    handoff velho também não: 30 dias depois o contato volta pra roda."""
    sql = ab._SQL_HANDOFF_ATIVO
    assert "origem LIKE 'pessoa%'" in sql
    assert "status = 'ativo'" in sql
    assert ":dias" in sql and ab.HANDOFF_SILENCIA_DIAS == 30


# ── 11. registrar_qualificacao é o fim do atendimento, não uma anotação ─────

def test_qualificacao_promove_e_abre_na_corretor():
    """As três coisas na mesma tool: nota + promoção + abertura na linha da Pessoa12."""
    import inspect

    from app.agents.pessoa import agent as ma

    fonte = inspect.getsource(ma._tool_registrar_qualificacao)
    assert "LeadHandoffNote" in fonte
    assert "promover_para_vendas" in fonte
    assert "_abrir_conversa_na_corretor" in fonte


def test_abertura_sai_sempre_por_template_utility(monkeypatch):
    """Dona 04/08: janela da Pessoa12 está fechada — texto livre falha e suja qualidade.
    A função canônica é a de template; a cópia texto-primeiro foi apagada em 19/08
    depois de 14 handoffs saírem como text/failed."""
    from app.agents.pessoa import agent as ma
    from app.api import cloud_corretora_a as corretor

    visto = {}

    def _tmpl(tel, nome, hook=None, cta=None, **kw):
        visto.update(tel=tel, nome=nome, hook=hook, cta=cta)
        return {"ok": True}

    monkeypatch.setattr(corretor, "enviar_template_corretor", _tmpl)
    monkeypatch.setattr(
        corretor, "enviar_cloud",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("não tenta texto")),
        raising=False,
    )
    r = ma._abrir_conversa_na_corretor("+5500000000000", "Paula",
                                 {"tipo": "apto", "quartos": "2", "renda": "R$ 5 mil"})
    assert r == {"aberta": True, "via": "template"}
    assert visto["nome"] == "Paula"
    assert "5 mil" in visto["hook"]


def test_abertura_template_falhou_nao_explode(monkeypatch):
    from app.agents.pessoa import agent as ma
    from app.api import cloud_corretora_a as corretor

    monkeypatch.setattr(
        corretor, "enviar_template_corretor",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("sem template")),
    )
    assert ma._abrir_conversa_na_corretor("+5500000000000", "Paula", {})["aberta"] is False


# ── opt-out: achado do Grok na auditoria de 02/08/2026 ────────────────────────
# O caller só olha `saida != "opt_out"`. Se o banco cair, `processar_inbound`
# devolvia None pra TUDO — inclusive pra quem escreveu "PARAR" — e a Pessoa
# respondia em cima do pedido de saída. Duas travas agora: a saída falha
# fechada, e `atender` revalida por conta própria.

class TestOptOutFalhaFechado:
    def test_processar_inbound_com_banco_fora_ainda_diz_opt_out(self, monkeypatch) -> None:
        from app.services import carrossel_corretor as cb

        def _explode(*a, **k):
            raise RuntimeError("could not connect to server")

        monkeypatch.setattr("app.db.session.SessionLocal", _explode)
        assert cb.processar_inbound("+5500000000000", "PARAR") == "opt_out"
        # quem não pediu pra parar segue como None (não vira falso opt-out)
        assert cb.processar_inbound("+5500000000000", "quero ver o apto") is None

    def test_atender_cala_em_opt_out_mesmo_se_o_caller_deixar_passar(self) -> None:
        from app.services import atendimento_corretor as ab

        res = ab.atender("+5500000000000", "PARAR", None)
        assert res["atendeu"] is False
        assert res["motivo"] == "opt_out"


# ── Classificação da resposta (12/08) ────────────────────────────────────────
#
# Medido no dia em que a Corretor foi a 232 disparos: das 20 respostas, 8 recusas
# passaram batido e 2 viraram lead PROMOVIDO na fila da Pessoa3. A causa era
# uma lista de frases curta demais — e o efeito colateral pior: frase não
# reconhecida virava "sim" em vez de "não sei".

from app.services.atendimento_corretor import (  # noqa: E402
    _e_recusa_branda, _pediu_parar, _quer_vender, _tem_interesse,
)


@pytest.mark.parametrize("frase", [
    "já fechei a compra", "já adquiri um imóvel", "já arrumei obrigada",
    "No momento não", "não penso em adquirir imóvel não",
    "Não estou no momento muito obrigada", "Por hora, não penso em adquirir",
    "não estou mais procurando", "Não, obrigada",
])
def test_recusa_reconhecida(frase):
    """Todas estas passaram batido em 12/08 e viraram disparo repetido."""
    assert _e_recusa_branda(frase) is True


@pytest.mark.parametrize("frase", [
    "PARAR DE RECEBER", "me tira dessa lista", "não me mande mais mensagens",
    "para de me enviar", "quero sair da lista",
])
def test_pedido_de_saida_e_mais_forte_que_recusa(frase):
    """Pedir pra parar ≠ "agora não". Vira opt-out duro e NÃO recebe nem o
    pedido de indicação — insistir com quem mandou parar é denúncia na certa."""
    assert _pediu_parar(frase) is True


@pytest.mark.parametrize("frase", [
    "Estou vendendo meu apto", "quero vender minha casa",
    "tenho um imóvel pra vender", "gostaria de vender",
])
def test_quem_quer_vender_e_captacao(frase):
    """Dona 12/08: vai pra Pessoa12 mesmo — é carteira nova, não descarte."""
    assert _quer_vender(frase) is True


@pytest.mark.parametrize("frase,esperado", [
    ("Pra morar", True), ("Morar", True), ("quanto custa?", True),
    ("tem casa dois dormitórios na região?", True),
    ("pode me mandar as opções", True),
    # Educação NÃO é interesse — foi o que enchia a fila da corretora.
    ("Bom dia", False), ("Oiii", False), ("Olá. Tudo bem", False),
    ("Boa tarde", False), ("obrigado", False),
])
def test_interesse_separa_comprador_de_educacao(frase, esperado):
    assert _tem_interesse(frase) is esperado


def test_frase_desconhecida_nao_promove():
    """A INVERSÃO que corrige o defeito: o que o sistema não entende vira
    "não sei", e "não sei" não vai pra fila de vendas."""
    assert _tem_interesse("xpto lorem ipsum") is False


def test_ja_comprei_mas_com_interesse_depois_nao_encerra():
    """Caso Renato Conde, 12/08: disse "já comprei, obrigado" e na mensagem
    seguinte ofereceu PERMUTA — casa dele por chácara em outra região. Lead
    cheio. Encerrar pela primeira frase perde a segunda.

    A regra é de ordem, não de palavra: recusa só encerra quando nada depois
    dela mostra interesse. Quem só queria sair não volta a falar de imóvel."""
    falas = [
        "Ja comprei, obrigado",
        "Mas se vc tiver chacaras ou apartamentos na região de Alumínio",
    ]
    assert any(_e_recusa_branda(f) for f in falas) is True
    assert any(_tem_interesse(f) for f in falas) is True   # a 2ª salva o lead
