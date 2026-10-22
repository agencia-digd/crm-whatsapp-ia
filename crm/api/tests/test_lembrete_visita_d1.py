"""Lembrete de véspera (D-1) — as travas, em ordem de dano se quebrarem.

A pior de todas está no primeiro teste: a mensagem vai PRA CORRETORA, nunca pro
cliente. É decisão da Dona (19/09/2026) e é trava, não preferência — quem fala
com o lead é a corretora.

Depois dela, o parser. É ele que decide se uma visita vira 'cancelada' no
banco; um falso positivo aqui apaga visita de gente que ia comparecer.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.services import lembrete_visita_d1 as d1

SP = ZoneInfo("America/Sao_Paulo")


def _visita(ordem: int, nome: str, hora: int = 14, bairro: str | None = "Centro"):
    return d1.VisitaAmanha(
        visita_id=100 + ordem, lead_nome=nome, bairro=bairro,
        data_hora=datetime(2026, 9, 20, hora, 0, tzinfo=SP),
        corretora_id=1, corretora_nome="Pessoa5 Souza",
        corretora_tel="+5500000000000", ordem=ordem,
    )


def _lote(*visitas) -> d1.Lote:
    return d1.Lote(corretora_id=1, corretora_nome="Pessoa5 Souza",
                   telefone="+5500000000000", dia=date(2026, 9, 20),
                   visitas=list(visitas))


# ─── A TRAVA ────────────────────────────────────────────────────────────────
def test_nunca_manda_pro_lead():
    """A mensagem é pra CORRETORA. O telefone do lead não pode nem ser
    selecionado pelo motor — se aparecer na query, alguém está a um passo de
    mandar a véspera pro cliente."""
    assert "telefone_lead" not in d1._SQL_VISITAS_AMANHA
    # O destino do envio vem SEMPRE do lote (telefone da corretora), e passa
    # por _destino_valido antes de sair.
    import inspect

    fonte = inspect.getsource(d1._enviar)
    assert "_destino_valido" in fonte
    assert "lote.telefone" in fonte
    assert "telefone_lead" not in fonte


def test_destino_invalido_nao_envia(monkeypatch):
    """_destino_valido falha FECHADO: sem confirmar que o número é de uma
    corretora ativa, nada sai."""
    monkeypatch.setattr(d1, "_destino_valido", lambda tel, cid: False)
    enviou: list = []
    res = d1._enviar(_lote(_visita(1, "Poliana")), "oi")
    assert res["ok"] is False
    assert res["error"] == "destino_nao_e_corretora"
    assert enviou == []


def test_nasce_desligada(monkeypatch):
    """Nada vai pro ar ligado. Sem linha no banco e sem env, DESLIGADO."""
    monkeypatch.delenv(d1.ENV_ATIVO, raising=False)

    class _S:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, *a, **k):
            raise RuntimeError("banco fora")

    monkeypatch.setattr(d1, "SessionLocal", _S)
    assert d1.ativo() is False


def test_desligado_nao_envia_mas_continua_lendo(monkeypatch):
    """Desligar cala a automação, não apaga a operação da vista: respostas e
    silêncio seguem sendo lidos."""
    monkeypatch.setattr(d1, "ativo", lambda: False)
    chamou: dict = {}
    monkeypatch.setattr(d1, "enviar_lembretes",
                        lambda **k: chamou.setdefault("enviou", True))
    monkeypatch.setattr(d1, "ler_respostas", lambda **k: chamou.setdefault("leu", True) or {})
    monkeypatch.setattr(d1, "cobrar_silencio", lambda **k: chamou.setdefault("silencio", True) or {})
    r = d1.rodar()
    assert "enviou" not in chamou
    assert chamou.get("leu") is True
    assert chamou.get("silencio") is True
    assert r["envio"]["pulados"] == {"desligado": 1}


# ─── O PARSER ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "texto,esperado",
    [
        ("1 ok, 2 remarcou", {1: "confirmada", 2: "remarcada"}),
        ("1 ok 2 ok 3 cancelou", {1: "confirmada", 2: "confirmada", 3: "cancelada"}),
        ("2 cancelou", {2: "cancelada"}),
        ("1 confirmada, 2 confirmada", {1: "confirmada", 2: "confirmada"}),
        ("1 - remarcou pra quinta", {1: "remarcada"}),
        ("todas ok", {1: "confirmada", 2: "confirmada", 3: "confirmada"}),
        ("tudo certo com todas", {1: "confirmada", 2: "confirmada", 3: "confirmada"}),
    ],
)
def test_interpretar_formas_aceitas(texto, esperado):
    assert d1.interpretar(texto, [1, 2, 3]) == esperado


def test_remarcou_nao_vira_confirmacao():
    """'remarcou' contém 'marcou'. Quem procurar confirmação primeiro lê
    remarcação como visita de pé — e a visita fura."""
    assert d1.interpretar("1 remarcou", [1]) == {1: "remarcada"}


def test_negativa_nao_vira_confirmacao():
    """'não vai' tem 'vai'. Cancelar é checado antes de confirmar."""
    assert d1.interpretar("1 não vai mais", [1]) == {1: "cancelada"}
    assert d1.interpretar("1 não vem", [1]) == {1: "cancelada"}


def test_lote_de_uma_aceita_palavra_solta():
    """Com uma visita só não há o que confundir."""
    assert d1.interpretar("cancelou", [1]) == {1: "cancelada"}
    assert d1.interpretar("confirmei com ela", [2]) == {2: "confirmada"}


def test_palavra_solta_em_lote_grande_nao_adivinha():
    """Três visitas e um 'cancelou' sem número: NÃO escolhe uma. Vira pendência
    humana — cancelar a visita errada é dano real."""
    r = d1.interpretar("cancelou", [1, 2, 3])
    assert set(r.values()) == {"nao_entendido"}


def test_conversa_que_nao_e_resposta_nao_marca_nada():
    """'oi', 'depois te falo' — o lembrete segue pendente, sem desfecho."""
    assert d1.interpretar("oi Assistente", [1, 2]) == {}
    assert d1.interpretar("", [1, 2]) == {}
    assert d1.interpretar("depois eu te falo", [1, 2]) == {}


def test_cancelar_tudo_nao_tem_atalho():
    """'todas ok' existe; 'cancela todas' não. Apagar a agenda inteira num
    atalho é o tipo de erro que não dá pra desfazer."""
    r = d1.interpretar("cancela todas", [1, 2, 3])
    assert all(v == "nao_entendido" for v in r.values())


def test_numero_fora_do_lote_nao_inventa_visita():
    """'3 ok' num lote de 2: nenhuma ordem inexistente aparece no resultado, e
    o lote vira pendência humana em vez de confirmar as duas por conta própria.
    Número que não existe é sinal de confusão — e confusão não fecha visita."""
    r = d1.interpretar("3 ok", [1, 2])
    assert 3 not in r
    assert set(r) <= {1, 2}
    assert set(r.values()) == {"nao_entendido"}


def test_emoji_numerado_e_lido():
    assert d1.interpretar("1️⃣ ok", [1]) == {1: "confirmada"}
    assert d1.interpretar("2️⃣ ✅", [1, 2]) == {2: "confirmada"}


def test_cada_numero_leva_a_palavra_do_proprio_bloco():
    """O corte por número é o que impede 'cancelou' do fim vazar pro 1."""
    assert d1.interpretar("1 ok e a 2 cancelou", [1, 2]) == {
        1: "confirmada", 2: "cancelada"
    }


# ─── O QUE CADA DESFECHO FAZ NO BANCO ───────────────────────────────────────
def test_so_cancelamento_mexe_na_visita():
    """confirmada e remarcada NÃO tocam `visitas`. 'remarcada' não existe na
    constraint ck_visitas_status, e inventar o status faria toda query que
    filtra 'agendada' perder essas visitas em silêncio."""
    import inspect

    fonte = inspect.getsource(d1._aplicar)
    assert 'desfecho == "cancelada"' in fonte
    assert "_SQL_CANCELA_VISITA" in fonte
    # O UPDATE só pega visita ainda de pé (corrida com o painel).
    assert "status IN ('agendada', 'pendente_confirmacao')" in d1._SQL_CANCELA_VISITA
    assert "'cancelada'" in d1._SQL_CANCELA_VISITA
    assert "remarcada" not in d1._SQL_CANCELA_VISITA


# ─── HORÁRIOS ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "quando,esperado",
    [
        (datetime(2026, 9, 19, 18, 5, tzinfo=SP), True),
        (datetime(2026, 9, 19, 20, 59, tzinfo=SP), True),
        (datetime(2026, 9, 19, 17, 0, tzinfo=SP), False),   # cedo demais
        (datetime(2026, 9, 19, 23, 0, tzinfo=SP), False),   # véspera às 23h é susto
    ],
)
def test_janela_de_envio(quando, esperado):
    assert d1.janela_de_envio(quando) is esperado


def test_corte_da_manha_nao_cobra_de_madrugada(monkeypatch):
    r = d1.cobrar_silencio(agora=datetime(2026, 9, 20, 6, 0, tzinfo=SP), dry_run=True)
    assert r["pulou"] == "antes_do_corte"


# ─── A MENSAGEM ─────────────────────────────────────────────────────────────
def test_texto_numera_e_ensina_a_resposta():
    """O número é o contrato com o parser. Se sumir da mensagem, a corretora
    responde solto e nada é interpretado."""
    texto = d1.montar_texto(_lote(_visita(1, "Poliana", 14, "Bairro X"),
                                  _visita(2, "João Carlos", 16, "Centro")))
    assert "1. 14h — Poliana — Bairro X" in texto
    assert "2. 16h — João Carlos — Centro" in texto
    assert "1 ok" in texto and "1 remarcou" in texto and "1 cancelou" in texto
    assert "todas ok" in texto
    # É pra corretora, e diz o que ela tem que fazer.
    assert "Pessoa5" in texto
    assert "confirmar com cada cliente" in texto


def test_texto_de_uma_visita_nao_oferece_todas_ok():
    texto = d1.montar_texto(_lote(_visita(1, "Poliana")))
    assert "todas ok" not in texto
    assert "1 ok" in texto


def test_hook_do_template_cabe_na_variavel_da_meta():
    """{{2}} não pode ter \\n, \\t nem 4+ espaços — a Meta rejeita o parâmetro."""
    hook, cta = d1.montar_hook_template(_lote(_visita(1, "Poliana"),
                                              _visita(2, "João")))
    for s in (hook, cta):
        assert "\n" not in s and "\t" not in s and "    " not in s
    assert "2 visitas" in hook
    assert cta


def test_template_e_so_o_esqueleto_ja_aprovado():
    """Nenhum template novo é submetido por esta automação: o corpo aprovado é
    genérico e só as variáveis mudam."""
    import inspect

    fonte = inspect.getsource(d1._enviar)
    assert "enviar_aviso_briefing" in fonte
    assert "hook=hook" in fonte and "cta=cta" in fonte
    # Fora da janela, o conteúdo espera como recado — o template é só o aviso.
    assert "recado_corretora.criar" in fonte


def test_evolution_nao_entra():
    """A Acme não usa mais Evolution (instâncias acme_* caídas desde
    julho/2026, de propósito). A leitura da resposta é da linha Cloud da Assistente."""
    import inspect

    fonte = inspect.getsource(d1)
    # Nada de cliente, instância ou coluna do Evolution no caminho do código.
    for proibido in ("EvolutionClient", "evolution_client", "evolution_instance",
                     "instance = 'acme", "send_text"):
        assert proibido not in fonte, f"caminho Evolution no motor: {proibido}"
    # A resposta da corretora é lida da linha Cloud da Assistente, e só dela.
    assert "m.instance = 'cloud_assistente'" in d1._SQL_RESPOSTAS


# ─── SELEÇÃO ────────────────────────────────────────────────────────────────
def test_so_visita_de_pe_de_amanha():
    """Visita já cancelada, realizada ou de outro dia não vira lembrete. Os dois
    estados "de pé" do CHECK de produção entram — 'pendente_confirmacao' é
    justamente quem mais precisa da véspera."""
    assert "v.status IN ('agendada', 'pendente_confirmacao')" in d1._SQL_VISITAS_AMANHA
    assert "America/Sao_Paulo')::date = :dia" in d1._SQL_VISITAS_AMANHA
    assert "c.ativo IS TRUE" in d1._SQL_VISITAS_AMANHA


def test_um_lembrete_por_visita_para_sempre():
    """O UNIQUE é a trava: rodar o tick de novo às 18h10 não vira segunda
    mensagem pra mesma corretora."""
    assert "uq_lembrete_d1_visita" in d1._SQL_INSERT
    assert "DO NOTHING" in d1._SQL_INSERT


def test_amanha_e_o_dia_seguinte_em_sp():
    agora = datetime(2026, 9, 19, 18, 30, tzinfo=SP)
    assert d1._amanha(agora) == date(2026, 9, 20)


def test_ordem_do_lote_segue_o_horario():
    """A numeração acompanha a ordem da lista que a corretora lê."""
    assert "ORDER BY v.corretora_id, v.data_hora" in d1._SQL_VISITAS_AMANHA


# ─── MÉTRICA ────────────────────────────────────────────────────────────────
def test_metrica_tem_as_cinco_colunas_pedidas():
    """Por semana: lembretes, confirmadas, remarcadas, canceladas na véspera e
    sem resposta. Sem isso não dá pra saber se funcionou."""
    for coluna in ("lembretes", "confirmadas", "remarcadas", "canceladas",
                   "sem_resposta"):
        assert coluna in d1._SQL_METRICAS
    assert "date_trunc('week'" in d1._SQL_METRICAS


def test_sem_resposta_conta_so_o_que_foi_enviado():
    """Lembrete reservado e não enviado não pode contar como silêncio da
    corretora — ela nunca recebeu nada."""
    assert "enviado_em IS NOT NULL\n                              AND desfecho IS NULL" \
        in d1._SQL_METRICAS
