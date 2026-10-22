"""Porta de SAÍDA do remarketing (19/09/2026) — lead que volta a falar sai da fila.

Os três casos que motivaram o módulo estão aqui como teste, com as frases reais:
Pessoa16 (…1978), Gabriel (…9853) e William (…0985), todos na linha da Pessoa11,
todos em etapa do funil remarketing com negócio andando.

O que estes testes protegem:
  1. resposta em QUALQUER linha (não só a da Pessoa12) tira o lead da fila;
  2. a régua PARA sempre que a pessoa responde — mesmo sem sinal de interesse
     ("Kkkkk" não promove, mas também não continua recebendo disparo);
  3. opt-out vai pra "Venda perdida", recusa branda NÃO volta pro atendimento;
  4. a etapa de chegada é decidida por evidência: "Em atendimento" pra quem a
     corretora já atendeu, "Novo" pra quem só levou automação;
  5. lead da Corretor não é tocado (é do handoff), lead sem dono só tem a régua
     parada;
  6. desarmado = o inbound não faz nada; `--dry-run` não escreve.

NENHUM teste manda mensagem: o módulo não tem caminho de envio — e o teste
`test_modulo_nao_envia_mensagem` existe pra isso continuar verdade.

Banco: Postgres de TESTE (conftest fail-closed).
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text as sql_text

from app.db.models import CadenciaEstado, Lead
from app.db.session import SessionLocal
from app.services import saida_remarketing as sr
from app.services.carrossel_corretor import CORRETORA_CORRETOR

CORRETOR, CORRETOR, CORRETOR = 1, 2, 3
AGORA = datetime(2026, 9, 19, 15, 0, tzinfo=timezone.utc)

# Etapas usadas: "Base" (funil remarketing) e as três de destino no principal.
_STAGES = [
    (16, "Base", "remarketing", 1),
    (6, "Novo", "principal", 1),
    (7, "Em atendimento", "principal", 1),
    (13, "Venda perdida", "principal", 8),
]


@pytest.fixture(autouse=True)
def _cenario(monkeypatch):
    with SessionLocal() as s:
        for sid, nome, funil, ordem in _STAGES:
            s.execute(sql_text(
                "INSERT INTO pipeline_stages (id, tenant_id, nome, funil, ordem) "
                "SELECT :id, 1, :nome, :funil, :ordem "
                " WHERE NOT EXISTS (SELECT 1 FROM pipeline_stages WHERE id = :id)"
            ), {"id": sid, "nome": nome, "funil": funil, "ordem": ordem})
        s.execute(sql_text(
            "INSERT INTO corretoras (id, nome, telefone, telefone_normalizado, ativo) "
            "VALUES (:id, 'Corretor', '+5500000000000', '+5500000000000', true) "
            "ON CONFLICT (id) DO UPDATE SET ativo = true"
        ), {"id": CORRETORA_CORRETOR})
        s.commit()
    # Flag ARMADA por default nos testes (cada teste de trava desarma de novo):
    # o que se quer exercitar é a régua, não o interruptor.
    monkeypatch.setenv(sr.FLAG_ENV, "1")
    yield


def _tel() -> str:
    return "5515" + "".join(random.choice("0123456789") for _ in range(9))


def _lead(owner: int | None, *, telefone: str, stage: int = 16) -> int:
    with SessionLocal() as s:
        lead = Lead(tenant_id=1, nome=f"Teste saida {telefone[-4:]}",
                    telefone=telefone, owner_id=owner, stage_id=stage,
                    external_ref=f"test-saida:{telefone}:{random.random()}")
        s.add(lead)
        s.flush()
        s.commit()
        return int(lead.id)


def _cadencia(telefone: str, *, status: str = "remarketing",
              corretora_id: int = CORRETOR, toque: int = 8) -> int:
    with SessionLocal() as s:
        est = CadenciaEstado(tenant_id=1, telefone=telefone,
                             corretora_id=corretora_id, toque_atual=toque,
                             status=status,
                             proximo_toque_em=AGORA + timedelta(days=11))
        s.add(est)
        s.flush()
        s.commit()
        return int(est.id)


def _msg(telefone: str, *, texto: str | None, do_lead: bool, quando: datetime,
         tipo: str = "text", instance: str = "acme_corretor",
         cid: int | None = CORRETOR, origem: str | None = None) -> None:
    with SessionLocal() as s:
        s.execute(sql_text(
            "INSERT INTO mensagens_whatsapp "
            "(id, evolution_message_id, instance, corretora_id, remote_jid, "
            " contato_telefone, from_corretora, tipo_mensagem, conteudo_texto, "
            " conteudo_metadata, timestamp_evolution) "
            "VALUES (gen_random_uuid(), :mid, :inst, :cid, :jid, :tel, :fc, "
            "        :tipo, :txt, CAST(:meta AS jsonb), :at)"
        ), {"mid": f"test-{random.random()}", "inst": instance, "cid": cid,
            "jid": f"{telefone}@s.whatsapp.net", "tel": telefone,
            "fc": not do_lead, "tipo": tipo, "txt": texto,
            "meta": f'{{"origem": "{origem}"}}' if origem else None,
            "at": quando})
        s.commit()


def _disparo(telefone: str, *, quando: datetime, hook: str = "oi") -> None:
    """Template de reengajamento pela linha da corretora — o que a régua mandou."""
    _msg(telefone, texto=f"[reengajamento: {hook}]", do_lead=False,
         quando=quando, tipo="template")


def _estado(telefone: str) -> tuple[str | None, str | None]:
    """(etapa do card, status da cadência) — o par que importa."""
    with SessionLocal() as s:
        etapa = s.execute(sql_text(
            "SELECT ps.nome FROM leads l JOIN pipeline_stages ps ON ps.id = l.stage_id "
            " WHERE right(regexp_replace(l.telefone,'\\D','','g'),8) = :suf"
        ), {"suf": telefone[-8:]}).scalar()
        status = s.execute(sql_text(
            "SELECT status FROM cadencia_estado "
            " WHERE right(regexp_replace(telefone,'\\D','','g'),8) = :suf"
        ), {"suf": telefone[-8:]}).scalar()
    return etapa, status


# ── 1. Classificação (pura, com as frases reais) ─────────────────────────────

@pytest.mark.parametrize("falas,acao,motivo", [
    # Gabriel, 21/08: respondeu ao disparo pedindo as opções.
    ([("Boa tarde Pessoa1!!", "text"), ("Pode mandar por favor", "text")],
     sr.ACAO_PROMOVER, None),
    # William, 16/09: um dia depois do disparo de 15/09.
    ([("Bom diab", "text"), ("Pode mandar sim", "text"), ("Eu vejo", "text")],
     sr.ACAO_PROMOVER, None),
    # Pessoa16, 13/08: disse o que procura.
    ([("esses outros são na região tb?", "text")], sr.ACAO_PROMOVER, None),
    # Respondeu, mas não disse nada: para a régua, não promove.
    ([("Kkkkk", "text")], sr.ACAO_PARAR, "sem_sinal"),
    ([("bom dia", "text")], sr.ACAO_PARAR, "sem_sinal"),
    # Recusa branda: nunca volta pro atendimento. "no momento não" também casa o
    # detector de auto-reply da cadência — e recusa tem que ganhar dele, senão a
    # régua segue batendo em quem disse não.
    ([("no momento não tenho interesse", "text")], sr.ACAO_PARAR, "recusa"),
    # Fim de linha declarado é Venda perdida, não "agora não" (Carlos Augusto,
    # 21/08, parado em "30 dias" — ver o levantamento da Dona de 19/09).
    ([("obrigado, mas já comprei", "text")], sr.ACAO_PERDIDO, "ja_encontrou"),
    ([("No momento desisti de comprar qualquer coisa nesse momento", "text")],
     sr.ACAO_PERDIDO, "ja_encontrou"),
    ([("Não obrigada, já encontrei", "text")], sr.ACAO_PERDIDO, "ja_encontrou"),
    # Pedido de parar: Venda perdida.
    ([("não quero mais receber mensagens, para", "text")], sr.ACAO_PERDIDO, "opt_out"),
    # Nada que conte como fala.
    ([], sr.ACAO_NADA, "sem_resposta"),
    ([("[reaction]", "reaction")], sr.ACAO_NADA, "sem_resposta"),
    ([("[reengajamento: oi]", "template")], sr.ACAO_NADA, "sem_resposta"),
    # Auto-reply de empresa não é gente (mesmo detector da cadência).
    ([("Mensagem automática: estamos fora do horário de atendimento", "text")],
     sr.ACAO_NADA, "sem_resposta"),
    # Clique em botão não é resposta…
    ([("Saiba mais", "button")], sr.ACAO_NADA, "sem_resposta"),
    # …mas "Parar mensagens" clicado vale.
    ([("Parar mensagens", "button")], sr.ACAO_PERDIDO, "opt_out"),
    # Mídia sem texto: é sinal de vida (documento do financiamento), não é sinal
    # de interesse legível.
    ([("[document]", "document")], sr.ACAO_PARAR, "so_midia"),
])
def test_classificar(falas, acao, motivo):
    d = sr.classificar(falas)
    assert d.acao == acao
    if motivo:
        assert d.motivo == motivo


@pytest.mark.parametrize("frase", [
    # Casos reais vistos no dry-run contra produção em 19/09 — todos vinham
    # como "promover" porque a palavra do imóvel casa o regex de interesse.
    "Oi Pessoa, apartamento não vou querer não, melhor deixar pra lá. Obrigada",
    "não vou dar andamento na compra do apartamento agora",
    "deixa pra lá, obrigado",
    "não tenho mais condições de financiar a casa",
])
def test_recusa_que_a_regua_do_handoff_nao_pega_nao_promove(frase):
    """O que importa é NÃO promover.

    O motivo varia de propósito: quando a frase também casa interesse (tem a
    palavra do imóvel), o veto extra derruba a promoção e assina "recusa"; quando
    não casa nada ("deixa pra lá"), a régua do handoff já devolve "sem_sinal". Os
    dois param a máquina e deixam o card onde está."""
    d = sr.classificar([(frase, "text")])
    assert d.acao == sr.ACAO_PARAR
    assert d.motivo in ("recusa", "sem_sinal")


def test_opt_out_ganha_de_interesse_na_mesma_conversa():
    """Quem pede pra parar não vira lead de ninguém, mesmo tendo dito o que quer."""
    d = sr.classificar([("quero um apto de 2 quartos", "text"),
                        ("na verdade pare de me mandar mensagem", "text")])
    assert d.acao == sr.ACAO_PERDIDO


# ── 2. Janela: só conta o que foi dito DEPOIS do disparo ─────────────────────

def test_fala_anterior_ao_disparo_nao_conta():
    """O caso Gabriel invertido: falou em 25/07, disparo em 03/08 → nada novo.

    Sem a janela, `_engajou` (3+ falas) aprovaria qualquer conversa antiga e o
    tick promoveria a base inteira de uma vez."""
    tel = _tel()
    _lead(CORRETOR, telefone=tel)
    _msg(tel, texto="Estou em negociação", do_lead=True,
         quando=AGORA - timedelta(days=30))
    _disparo(tel, quando=AGORA - timedelta(days=10))
    with SessionLocal() as s:
        d = sr.avaliar_telefone(s, tel, agora=AGORA)
    assert d.acao == sr.ACAO_NADA


def test_sem_disparo_usa_janela_de_7_dias():
    tel = _tel()
    _lead(CORRETOR, telefone=tel)
    _msg(tel, texto="pode me mandar as opções", do_lead=True,
         quando=AGORA - timedelta(days=30))
    with SessionLocal() as s:
        assert sr.avaliar_telefone(s, tel, agora=AGORA).acao == sr.ACAO_NADA
    _msg(tel, texto="pode me mandar as opções", do_lead=True,
         quando=AGORA - timedelta(days=2))
    with SessionLocal() as s:
        assert sr.avaliar_telefone(s, tel, agora=AGORA).acao == sr.ACAO_PROMOVER


# ── 3. O caso real, ponta a ponta, pela linha da CORRETOR ────────────────────────

def test_resposta_na_linha_da_corretor_tira_da_fila():
    """William: disparo em 15/09 pela linha da Pessoa11, resposta em 16/09.

    É o cenário que o gancho da cadência não vê (ele só escuta a linha da Pessoa12) —
    e o que fez o quarto disparo sair com a pasta de financiamento aberta."""
    tel = _tel()
    lead_id = _lead(CORRETOR, telefone=tel)
    cad = _cadencia(tel, status="ativa", toque=6)
    _msg(tel, texto="Vou ver aqui e retorno", do_lead=True,
         quando=AGORA - timedelta(days=11))   # conversa humana antiga
    _msg(tel, texto="Boa tarde, já te respondo", do_lead=False,
         quando=AGORA - timedelta(days=11))
    _disparo(tel, quando=AGORA - timedelta(days=4),
             hook="Vi aqui que a sua busca de imóvel ficou parada")
    _msg(tel, texto="Pode mandar sim", do_lead=True, quando=AGORA - timedelta(days=3))

    res = sr.processar_inbound(tel, "Eu vejo")
    assert res == sr.ACAO_PROMOVER

    etapa, status = _estado(tel)
    # A corretora JÁ falou com ele → "Em atendimento", não "Novo".
    assert etapa == "Em atendimento"
    assert status == "respondida"
    with SessionLocal() as s:
        tipos = [r[0] for r in s.execute(sql_text(
            "SELECT tipo FROM lead_events WHERE lead_id = :i"), {"i": lead_id}).all()]
        # os dois eventos: auditoria + proteção de 72h contra o espelho do Kommo
        assert sr.EVENTO_SAIDA in tipos and "stage_manual_v2" in tipos
        ev = s.execute(sql_text(
            "SELECT payload->>'origem' FROM cadencia_eventos WHERE cadencia_id = :i "
            " AND tipo = 'resposta'"), {"i": cad}).scalar()
        assert ev == sr.QUEM


def test_lead_que_nunca_teve_humano_vai_pra_novo():
    """Só automação no histórico → a corretora precisa abordar: "Novo"."""
    tel = _tel()
    _lead(CORRETOR, telefone=tel)
    _cadencia(tel, corretora_id=CORRETOR)
    _disparo(tel, quando=AGORA - timedelta(days=2))
    _msg(tel, texto="Oi, ainda estou procurando sim", do_lead=True,
         quando=AGORA - timedelta(days=1))
    sr.processar_inbound(tel, "pode mandar")
    assert _estado(tel)[0] == "Novo"


def test_respondeu_sem_sinal_para_a_regua_e_nao_move_o_card():
    tel = _tel()
    _lead(CORRETOR, telefone=tel)
    _cadencia(tel)
    _disparo(tel, quando=AGORA - timedelta(days=2))
    assert sr.processar_inbound(tel, "Kkkkk") == sr.ACAO_PARAR
    assert _estado(tel) == ("Base", "respondida")


def test_opt_out_vai_pra_venda_perdida():
    tel = _tel()
    _lead(CORRETOR, telefone=tel)
    _cadencia(tel)
    _disparo(tel, quando=AGORA - timedelta(days=1))
    assert sr.processar_inbound(tel, "para de mandar mensagem") == sr.ACAO_PERDIDO
    assert _estado(tel) == ("Venda perdida", "opt_out")


def test_recusa_branda_fica_parada_no_card():
    """"Não tenho interesse" não pode voltar pro atendimento (pedido da Dona)."""
    tel = _tel()
    _lead(CORRETOR, telefone=tel)
    _cadencia(tel)
    _disparo(tel, quando=AGORA - timedelta(days=1))
    assert sr.processar_inbound(tel, "obrigado, mas não tenho interesse") == sr.ACAO_PARAR
    assert _estado(tel) == ("Base", "respondida")


def test_ja_encontrou_vai_pra_venda_perdida_com_status_proprio():
    """Fim de linha declarado. O status separa "não precisa mais" de opt-out."""
    tel = _tel()
    _lead(CORRETOR, telefone=tel)
    _cadencia(tel)
    _disparo(tel, quando=AGORA - timedelta(days=1))
    assert sr.processar_inbound(tel, "Não obrigada, já encontrei") == sr.ACAO_PERDIDO
    assert _estado(tel) == ("Venda perdida", "ja_encontrou")


def test_desistiu_mas_voltou_a_querer_promove():
    """Interesse na mesma janela ganha do fim de linha — a ordem do handoff."""
    d = sr.classificar([("tinha desistido de comprar", "text"),
                        ("mas apareceu uma oportunidade, pode me mandar as opções", "text")])
    assert d.acao == sr.ACAO_PROMOVER


# ── 4. Territórios que não são nossos ────────────────────────────────────────

def test_card_da_corretor_nao_e_tocado():
    """O CARD da Corretor é do `handoff_remarketing` — dois serviços no mesmo card se
    desfazem. Se houver régua viva no telefone, ela para (isso nunca faz mal);
    o que não acontece é o card mudar de etapa por aqui."""
    tel = _tel()
    _lead(CORRETORA_CORRETOR, telefone=tel)
    _disparo(tel, quando=AGORA - timedelta(days=1))
    assert sr.processar_inbound(tel, "quero ver apartamento de 2 quartos") == sr.ACAO_NADA
    assert _estado(tel) == ("Base", None)

    tel2 = _tel()
    _lead(CORRETORA_CORRETOR, telefone=tel2)
    _cadencia(tel2, corretora_id=CORRETORA_CORRETOR)
    _disparo(tel2, quando=AGORA - timedelta(days=1))
    assert sr.processar_inbound(tel2, "quero ver apartamento de 2 quartos") == sr.ACAO_PARAR
    assert _estado(tel2) == ("Base", "respondida")


def test_regua_viva_para_mesmo_com_o_card_fora_da_fila():
    """O caso Pessoa16 (07/09): toque 6 pela linha da Pessoa11 com o card em
    "Em atendimento". A cadência obedece `cadencia_estado`, não a etapa — então a
    régua tem que parar aqui também, sem tocar no card."""
    tel = _tel()
    _lead(CORRETOR, telefone=tel, stage=7)
    _cadencia(tel, status="ativa", toque=6)
    _disparo(tel, quando=AGORA - timedelta(days=2),
             hook="Te mandei umas mensagens e tentei te ligar")
    res = sr.processar_inbound(tel, "ah, quanto a isso fique em paz que eu te procuro sim")
    assert res == sr.ACAO_PARAR
    etapa, status = _estado(tel)
    assert etapa == "Em atendimento"   # card intacto
    assert status == "respondida"      # régua parada


def test_lead_sem_dono_so_para_a_regua():
    """Promover sem dono criaria card órfão; escolher dono é da roleta."""
    tel = _tel()
    _lead(None, telefone=tel)
    _cadencia(tel, corretora_id=CORRETOR)
    _disparo(tel, quando=AGORA - timedelta(days=1))
    res = sr.processar_inbound(tel, "quero ver apartamento de 2 quartos")
    assert res == sr.ACAO_PARAR
    assert _estado(tel) == ("Base", "respondida")


def test_lead_fora_do_funil_e_sem_regua_viva_nao_e_mexido():
    """Sem card na fila e sem régua viva não há nada a consertar."""
    tel = _tel()
    _lead(CORRETOR, telefone=tel, stage=7)
    _disparo(tel, quando=AGORA - timedelta(days=1))
    assert sr.processar_inbound(tel, "pode mandar as opções") == sr.ACAO_NADA
    assert _estado(tel) == ("Em atendimento", None)


# ── 5. Travas ───────────────────────────────────────────────────────────────

def test_desarmado_nao_faz_nada(monkeypatch):
    monkeypatch.setenv(sr.FLAG_ENV, "")
    tel = _tel()
    _lead(CORRETOR, telefone=tel)
    _cadencia(tel)
    _disparo(tel, quando=AGORA - timedelta(days=1))
    assert sr.processar_inbound(tel, "pode mandar as opções") is None
    assert _estado(tel) == ("Base", "remarketing")


def test_dry_run_nao_escreve():
    tel = _tel()
    _lead(CORRETOR, telefone=tel)
    _cadencia(tel)
    _msg(tel, texto="Boa tarde, te mando hoje", do_lead=False,
         quando=AGORA - timedelta(days=20))   # a Pessoa11 já atendeu esse lead
    _disparo(tel, quando=AGORA - timedelta(days=2))
    _msg(tel, texto="Pode mandar sim", do_lead=True, quando=AGORA - timedelta(days=1))

    rel = sr.rodar(dry_run=True, telefone=tel, agora=AGORA)
    assert rel.considerados == 1
    assert [r.acao for r in rel.resultados] == [sr.ACAO_PROMOVER]
    # o dry-run promete o que o real faria — etapa e cadências incluídas
    assert rel.resultados[0].stage_para == "Em atendimento"
    assert rel.resultados[0].cadencias
    assert rel.resultados[0].aplicado is False
    assert _estado(tel) == ("Base", "remarketing")

    rel2 = sr.rodar(dry_run=False, telefone=tel, agora=AGORA)
    assert [r.acao for r in rel2.resultados] == [sr.ACAO_PROMOVER]
    assert _estado(tel) == ("Em atendimento", "respondida")


def test_rodar_e_idempotente():
    """Depois de sair da fila, a segunda passada não tem nada pra fazer."""
    tel = _tel()
    _lead(CORRETOR, telefone=tel)
    _cadencia(tel)
    _disparo(tel, quando=AGORA - timedelta(days=2))
    _msg(tel, texto="pode me mandar as opções", do_lead=True,
         quando=AGORA - timedelta(days=1))
    sr.rodar(dry_run=False, telefone=tel, agora=AGORA)
    rel = sr.rodar(dry_run=False, telefone=tel, agora=AGORA)
    assert rel.resultados == []


def test_erro_nao_derruba_o_inbound(monkeypatch):
    """A conversa da corretora vale mais que a nossa arrumação de card."""
    def _explode(*a, **k):
        raise RuntimeError("banco fora")

    monkeypatch.setattr(sr, "avaliar_telefone", _explode)
    assert sr.processar_inbound(_tel(), "oi") is None


def test_o_gancho_esta_plugado_nos_dois_inbounds():
    """Trava estrutural: o serviço só serve se alguém chamar.

    O bug original foi justamente um gancho existir e não ser chamado na linha
    certa — `processar_resposta_cadencia` mora em `cadencia` desde julho e nunca
    viu uma resposta da Pessoa11. Um teste de rota de verdade precisaria de payload
    Cloud + auth; aqui basta garantir que a chamada não seja removida sem
    ninguém notar."""
    import app.api.cloud_corretora as cc
    import app.api.cloud_corretora_a as cg

    for mod in (cc, cg):
        with open(mod.__file__, encoding="utf-8") as f:
            fonte = f.read()
        assert "saida_remarketing" in fonte, f"gancho fora de {mod.__name__}"
    assert hasattr(cg, "_tratar_saida_remarketing")


def test_modulo_nao_envia_mensagem():
    """Trava de desenho: nada de envio entra aqui sem alguém apagar este teste."""
    fonte = (sr.__file__).replace(".pyc", ".py")
    with open(fonte, encoding="utf-8") as f:
        codigo = [l for l in f if not l.lstrip().startswith("#")]
    texto = "".join(codigo)
    for proibido in ("enviar_template", "enviar_cloud", "httpx", "requests"):
        assert proibido not in texto, f"o módulo passou a enviar algo: {proibido}"
