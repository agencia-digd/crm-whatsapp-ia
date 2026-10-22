"""Handoff automático do remarketing (05/09/2026).

O que protege:
  1. Interesse real → o lead sai da Corretor e vira card "Novo" da corretora
     configurada, com evento `transferencia` explicando o porquê.
  2. "Não tenho interesse" / "pare" → NUNCA transfere.
  3. Lead já transferido na mão (owner_editado_manual / owner ≠ Corretor) → não mexe.
  4. Rodar duas vezes = um evento só (idempotente).
  5. Destino vem da config, não do código; destino inválido = não transfere.
  6. O gancho roda JÁ NA 1ª MENSAGEM do lead em `atendimento_corretor.atender`.
  7. Nada disso manda WhatsApp.

Banco: Postgres de TESTE (conftest fail-closed). Cada teste usa um sufixo de
telefone único — o banco persiste entre runs.
"""
from __future__ import annotations

import inspect
import random

import pytest
from sqlalchemy import text as sql_text

from app.db.models import Lead, LeadEvent
from app.db.session import SessionLocal
from app.services import atendimento_corretor as ab
from app.services import config_operacional
from app.services import handoff_remarketing as hr
from app.services.carrossel_corretor import CORRETORA_CORRETOR, STAGE_RESPONDEU

CORRETOR = 3
CORRETOR = 1


# ── Setup ───────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _cenario(monkeypatch):
    """Etapas + corretora Corretor no banco de teste; config no default (ligado, Pessoa12)."""
    with SessionLocal() as s:
        for sid, nome, funil, ordem in (
            (6, "Novo", "principal", 1),
            (STAGE_RESPONDEU, "Respondeu", "remarketing", 9),
        ):
            s.execute(sql_text(
                "INSERT INTO pipeline_stages (id, tenant_id, nome, funil, ordem) "
                "SELECT :id, 1, :n, :f, :o "
                "WHERE NOT EXISTS (SELECT 1 FROM pipeline_stages WHERE id = :id)"
            ), {"id": sid, "n": nome, "f": funil, "o": ordem})
        s.execute(sql_text(
            "INSERT INTO corretoras (id, nome, telefone, telefone_normalizado, ativo) "
            "VALUES (:id, 'Corretor', '+5500000000000', '+5500000000000', true) "
            "ON CONFLICT (id) DO UPDATE SET nome = 'Corretor', ativo = true"
        ), {"id": CORRETORA_CORRETOR})
        s.commit()
    # config efetiva = defaults do código (sem linha no banco de teste)
    monkeypatch.setattr(config_operacional, "_carregar", lambda: {})
    monkeypatch.setattr(config_operacional.settings, "handoff_remarketing_ativo", True)
    monkeypatch.setattr(config_operacional.settings, "handoff_remarketing_corretora", CORRETOR)
    # Roleta de UMA pessoa aqui de propósito: este arquivo protege a
    # transferência em si (critério, idempotência, fail-closed), e pra isso o
    # destino tem que ser determinístico. O round-robin entre Pessoa10/Pessoa11/Pessoa12
    # (18/09) tem arquivo próprio — tests/test_aviso_remarketing.py.
    # (Até 05/10/2026 era roleta vazia + destino único de reserva; a reserva
    # saiu: roleta vazia agora NÃO transfere — tests/test_copy_e_quando_responde.py.)
    monkeypatch.setattr(config_operacional.settings, "handoff_remarketing_roleta", [CORRETOR])
    yield


def _tel() -> str:
    return "5515" + "".join(random.choice("0123456789") for _ in range(9))


def _novo_lead(telefone: str, owner: int = CORRETORA_CORRETOR,
               stage: int = STAGE_RESPONDEU, manual: bool = False,
               ref: str | None = None) -> int:
    with SessionLocal() as s:
        lead = Lead(tenant_id=1, nome="Teste handoff", telefone=telefone,
                    owner_id=owner, stage_id=stage, owner_editado_manual=manual,
                    external_ref=ref or f"test:{telefone}")
        s.add(lead)
        s.commit()
        return int(lead.id)


def _le(lead_id: int) -> tuple[Lead, list[LeadEvent]]:
    with SessionLocal() as s:
        lead = s.get(Lead, lead_id)
        evs = (s.query(LeadEvent).filter(LeadEvent.lead_id == lead_id,
                                         LeadEvent.tipo == "transferencia")
               .order_by(LeadEvent.id).all())
        s.expunge_all()
        return lead, evs


# ── 1. Critério (puro, sem banco) ───────────────────────────────────────────

@pytest.mark.parametrize("fala, sinal", [
    ("pode mandar pra mim, é pra minha filha, região do Bairro A, até 450 mil",
     hr.SINAL_INTERESSE),
    ("Estou procurando apartamento de 2 quartos", hr.SINAL_INTERESSE),
    ("Quanto tá o valor?", hr.SINAL_INTERESSE),
    ("Perto da prefeitura", hr.SINAL_INTERESSE),
    ("Sim", hr.SINAL_AFIRMATIVA),
    ("Pode sim!", hr.SINAL_AFIRMATIVA),
    ("Estou sim", hr.SINAL_AFIRMATIVA),
    ("Ok, manda", hr.SINAL_AFIRMATIVA),
    ("Com certeza", hr.SINAL_AFIRMATIVA),
    ("Quero sim", hr.SINAL_INTERESSE),
    ("Estou vendendo meu apto", hr.SINAL_QUER_VENDER),
])
def test_interesse_real_transfere(fala, sinal):
    d = hr.avaliar([fala])
    assert d.transferir is True
    assert d.sinal == sinal
    assert d.trecho and d.motivo


@pytest.mark.parametrize("falas, motivo", [
    (["não tenho interesse"], "recusa"),
    (["No momento não, mas obrigado pelo contato"], "recusa"),
    (["Já comprei"], "recusa"),
    (["No momento não", "ok", "obrigado você"], "recusa"),   # educação não reabre
    (["Sim", "não penso em adquirir imóvel não"], "recusa"),
    (["Quero sim", "para de me mandar mensagem"], "pediu_parar"),
    (["PARAR"], "pediu_parar"),
    (["Bom dia"], "sem_sinal"),
    (["Olá, tudo bem?"], "sem_sinal"),
    (["xpto lorem ipsum"], "sem_sinal"),
    (["?"], "sem_sinal"),
    (["Não"], "sem_sinal"),
    (["[audio]"], "sem_texto"),
    ([], "sem_texto"),
])
def test_sem_interesse_nao_transfere(falas, motivo):
    d = hr.avaliar(falas)
    assert d.transferir is False
    assert d.motivo == motivo


def test_recusa_seguida_de_interesse_transfere():
    """Caso Renato (12/08): 'já comprei' + permuta na frase seguinte."""
    d = hr.avaliar(["Ja comprei, obrigado",
                    "Mas se vc tiver chacaras ou apartamentos na região de Alumínio"])
    assert d.transferir is True and d.sinal == hr.SINAL_INTERESSE


def test_tres_respostas_sem_recusa_e_engajamento():
    d = hr.avaliar(["Oi", "Olá", "Tudo bem"])
    assert d.transferir is True and d.sinal == hr.SINAL_ENGAJOU


def test_afirmativa_nao_engole_negacao():
    assert hr._e_afirmativa("sim, mas não agora") is False
    assert hr._e_afirmativa("pode ser que sim, estou vendo com meu marido ainda hoje") is False  # longa


# ── 2. Transferência de verdade (banco de teste) ────────────────────────────

def test_transfere_lead_da_corretor_pra_corretora_configurada():
    tel = _tel()
    lid = _novo_lead(tel)

    res = hr.processar(tel, ["pode mandar pra mim, é pra minha filha, até 450 mil"],
                       nome="Dona Pessoa")

    assert res["transferiu"] is True and res["leads"] == [lid]
    assert res["para"] == CORRETOR and res["para_label"] == "Pessoa3"
    lead, evs = _le(lid)
    assert lead.owner_id == CORRETOR
    assert lead.stage_id == hr.STAGE_DESTINO == 6
    assert lead.owner_editado_manual is True
    assert len(evs) == 1
    p = evs[0].payload
    assert p["quem"] == hr.QUEM and p["de"] == CORRETORA_CORRETOR and p["para"] == CORRETOR
    assert p["sinal"] == hr.SINAL_INTERESSE
    assert "pode mandar pra mim" in p["trecho"]
    assert p["motivo"].startswith("handoff automático:")
    assert p["stage_de"] == STAGE_RESPONDEU and p["stage_para"] == 6
    assert p["lead_nome"] == "Dona Pessoa"


def test_nao_tenho_interesse_nao_transfere():
    tel = _tel()
    lid = _novo_lead(tel)

    res = hr.processar(tel, ["não tenho interesse, obrigado"])

    assert res["transferiu"] is False and res["motivo"] == "recusa"
    lead, evs = _le(lid)
    assert lead.owner_id == CORRETORA_CORRETOR and lead.stage_id == STAGE_RESPONDEU
    assert evs == []


def test_idempotente_duas_chamadas_um_evento():
    tel = _tel()
    lid = _novo_lead(tel)

    r1 = hr.processar(tel, ["Sim"])
    r2 = hr.processar(tel, ["Sim", "quero ver o apartamento"])

    assert r1["transferiu"] is True
    assert r2["transferiu"] is False and r2["motivo"] == "sem_lead_no_remarketing"
    lead, evs = _le(lid)
    assert lead.owner_id == CORRETOR and len(evs) == 1


def test_transferencia_manual_nao_e_mexida():
    """Lead que a Assistente mandou pra Pessoa10 pelo painel (owner_editado_manual) fica lá,
    mesmo que continue em 'Respondeu' e escreva de novo com interesse."""
    tel = _tel()
    lid = _novo_lead(tel, owner=CORRETOR, manual=True)

    res = hr.processar(tel, ["quero um apartamento de 2 quartos"])

    assert res["transferiu"] is False and res["motivo"] == "ja_com_corretora"
    lead, evs = _le(lid)
    assert lead.owner_id == CORRETOR and lead.stage_id == STAGE_RESPONDEU
    assert evs == []


def test_lead_da_corretor_com_flag_manual_nao_e_mexido():
    tel = _tel()
    lid = _novo_lead(tel, owner=CORRETORA_CORRETOR, manual=True)

    res = hr.processar(tel, ["quero um apartamento"])

    assert res["transferiu"] is False and res["motivo"] == "editado_manual"
    assert _le(lid)[0].owner_id == CORRETORA_CORRETOR


def test_evento_transferencia_previo_bloqueia():
    """Cinto e suspensório: mesmo sem a flag, evento `transferencia` no lead
    (ex.: transferido e devolvido pra Corretor na mão) segura o automático."""
    tel = _tel()
    lid = _novo_lead(tel)
    with SessionLocal() as s:
        s.add(LeadEvent(tenant_id=1, lead_id=lid, tipo="transferencia",
                        payload={"quem": "assistente", "de": 3, "para": 4}))
        s.commit()

    res = hr.processar(tel, ["quero um apartamento"])

    assert res["transferiu"] is False and res["motivo"] == "ja_transferido"
    assert _le(lid)[0].owner_id == CORRETORA_CORRETOR


def test_destino_vem_da_config(monkeypatch):
    monkeypatch.setattr(config_operacional, "_carregar",
                        lambda: {"handoff_remarketing_roleta": [CORRETOR]})
    tel = _tel()
    lid = _novo_lead(tel)

    res = hr.processar(tel, ["Estou sim"])

    assert res["transferiu"] is True and res["para"] == CORRETOR
    assert _le(lid)[0].owner_id == CORRETOR


@pytest.mark.parametrize("roleta,motivo", [
    ([99], "roleta_sem_ninguem_valido"),
    ([CORRETORA_CORRETOR], "roleta_sem_ninguem_valido"),
    (["abc"], "roleta_invalida"),
    ([], "roleta_vazia"),
])
def test_destino_invalido_nao_transfere(monkeypatch, roleta, motivo):
    """Fail-closed: id inexistente, a própria Corretor, lixo ou roleta vazia → fica
    na Corretor, visível, e o resultado diz por quê. O destino único de reserva
    (`handoff_remarketing_corretora`) não é mais consultado: mesmo apontando
    pra uma corretora boa, roleta sem ninguém não transfere."""
    monkeypatch.setattr(config_operacional, "_carregar",
                        lambda: {"handoff_remarketing_roleta": roleta,
                                 "handoff_remarketing_corretora": CORRETOR})
    tel = _tel()
    lid = _novo_lead(tel)

    res = hr.processar(tel, ["quero um apartamento"])

    assert res["transferiu"] is False and res["motivo"] == motivo
    assert _le(lid)[0].owner_id == CORRETORA_CORRETOR


def test_desligado_nao_transfere(monkeypatch):
    monkeypatch.setattr(config_operacional, "_carregar",
                        lambda: {"handoff_remarketing_ativo": False})
    tel = _tel()
    lid = _novo_lead(tel)
    res = hr.processar(tel, ["quero um apartamento"])
    assert res["transferiu"] is False and res["motivo"] == "desligado"
    assert _le(lid)[0].owner_id == CORRETORA_CORRETOR


def test_banco_fora_nao_derruba(monkeypatch):
    from app.db import session as sess

    monkeypatch.setattr(sess, "SessionLocal",
                        lambda: (_ for _ in ()).throw(RuntimeError("banco fora")))
    res = hr.processar("+5500000000000", ["quero um apartamento"])
    assert res["transferiu"] is False and res["motivo"] == "erro"


def test_chaves_de_config_existem():
    assert "handoff_remarketing_ativo" in config_operacional.DEFAULTS
    assert "handoff_remarketing_corretora" in config_operacional.DEFAULTS


# ── 3. O gancho no atendimento ──────────────────────────────────────────────

@pytest.fixture()
def _atender_mockado(monkeypatch):
    monkeypatch.setattr(ab.settings, "atendimento_corretor_ativo", True, raising=False)
    monkeypatch.setattr(ab, "_e_da_roda", lambda t: True)
    monkeypatch.setattr(ab, "_nos_abordamos_na_mao", lambda t: False)
    monkeypatch.setattr(ab, "_e_da_equipe", lambda t: False)
    monkeypatch.setattr(ab, "_humano_falando", lambda t, desde=None: False)
    monkeypatch.setattr(ab, "_inicio_da_conversa", lambda t: None)
    monkeypatch.setattr(ab, "_handoff_feito", lambda t: False)
    monkeypatch.setattr(ab, "_rodar_pessoa", lambda *a: "oi 😊")
    monkeypatch.setattr(ab, "_responder", lambda *a: None)
    monkeypatch.setattr("app.services.conversations.load_history", lambda *a, **k: [])
    monkeypatch.setattr("app.services.conversations.append_message", lambda *a, **k: None)
    chamadas: list[dict] = []
    monkeypatch.setattr(hr, "processar",
                        lambda tel, falas, nome=None, **k: chamadas.append(
                            {"tel": tel, "falas": falas}) or {"transferiu": True})
    monkeypatch.setattr(ab, "promover_para_vendas",
                        lambda *a, **k: pytest.fail("promoveu na 1ª mensagem"))
    return chamadas


def test_atender_chama_handoff_na_primeira_mensagem(_atender_mockado):
    """Antes só a 2ª mensagem promovia — a senhora ficou 3 dias na Corretor.

    Desde 05/10/2026 (adendo da orquestradora), transferiu no turno, a Pessoa
    não responde: a conversa segue pela linha da corretora."""
    res = ab.atender("+5500000000000", "pode mandar pra mim, é pra minha filha")
    assert res["atendeu"] is False and res["motivo"] == "transferido"
    assert res["handoff"] == {"transferiu": True}
    assert _atender_mockado[0]["falas"] == ["pode mandar pra mim, é pra minha filha"]


def test_atender_recusa_nao_chega_no_handoff(_atender_mockado, monkeypatch):
    monkeypatch.setattr(ab, "encerrar_com_indicacao", lambda *a, **k: {"ok": True})
    res = ab.atender("+5500000000000", "não tenho interesse")
    assert res["motivo"] == "recusou"
    assert _atender_mockado == []


def test_atender_quem_quer_vender_nao_passa_duas_vezes(_atender_mockado, monkeypatch):
    monkeypatch.setattr(ab, "promover_para_vendas", lambda *a, **k: True)
    res = ab.atender("+5500000000000", "estou vendendo meu apto")
    assert res["promoveu"] is True
    assert _atender_mockado == []


# ── 4. Contratos ────────────────────────────────────────────────────────────

def test_painel_transferir_usa_o_mesmo_miolo():
    """Botão do painel e handoff automático não podem divergir."""
    from app.api.painel import painel_crm_transferir

    assert "transferir_lead(" in inspect.getsource(painel_crm_transferir)
    # Os dois caminhos automáticos (interesse lido e "todo mundo que cair em
    # Respondeu") passam pelo MESMO `_transferir`, e é ele que chama o miolo.
    assert "transferir_lead(" in inspect.getsource(hr._transferir)
    assert "_transferir(" in inspect.getsource(hr.processar)
    assert "_transferir(" in inspect.getsource(hr.processar_resposta)


def test_handoff_nao_manda_texto_livre():
    """A janela de 24h é do 0000, não da linha da corretora — fora dela só
    template passa. Texto livre aqui seria entrega falhada contando contra a
    qualidade da linha na Meta."""
    fonte = inspect.getsource(hr)
    for proibido in ("enviar_cloud(", "send_text", "send_message"):
        assert proibido not in fonte, proibido
    assert "enviar_template_linha(" in fonte


# ── 5. Aviso pela linha da corretora (15/09) ────────────────────────────────
#
# Sob pytest o envio SEMPRE cai no dry-run de `cloud_corretora_a` (nunca bate na
# Graph) — mas grava na thread igual ao caminho real, que é justamente o que
# precisa ser verificado aqui.

@pytest.fixture()
def _armado(monkeypatch):
    monkeypatch.setattr(config_operacional, "_carregar",
                        lambda: {"handoff_remarketing_avisa_lead": True})
    monkeypatch.setattr("app.services.pos_ligacao._pediu_silencio",
                        lambda s, t: False)


def _mensagens_do_aviso(tel: str) -> list[tuple]:
    with SessionLocal() as s:
        return s.execute(sql_text(
            """SELECT remote_jid, contato_telefone, corretora_id, instance,
                      tipo_mensagem, from_corretora, conteudo_texto,
                      conteudo_metadata->>'origem'
                 FROM mensagens_whatsapp
                WHERE right(regexp_replace(contato_telefone,'\\D','','g'),8) = :suf"""),
            {"suf": tel[-8:]}).all()


def test_desarmado_por_padrao_nao_manda_nada():
    """Nasce desligado: a Dona arma no painel, não o deploy."""
    tel = _tel()
    _novo_lead(tel)
    res = hr.processar(tel, ["quero um apartamento de 2 quartos"])
    assert res["transferiu"] is True
    assert res["aviso"] == {"ok": False, "motivo": "desarmado"}
    assert _mensagens_do_aviso(tel) == []


def test_armado_manda_template_e_grava_na_thread(_armado):
    tel = _tel()
    _novo_lead(tel)

    res = hr.processar(tel, ["pode mandar pra mim, até 450 mil"], nome="Dona Pessoa")

    assert res["transferiu"] is True and res["para"] == CORRETOR
    assert res["aviso"]["ok"] is True
    linhas = _mensagens_do_aviso(tel)
    assert len(linhas) == 1
    jid, contato, cid, instancia, tipo, from_c, texto, origem = linhas[0]
    assert origem == hr.ORIGEM_AVISO == "handoff_remarketing"
    assert jid and jid.endswith("@s.whatsapp.net")       # NOT NULL na tabela
    assert contato.endswith(tel[-8:])
    assert cid == CORRETOR and instancia == "acme_corretora_a"  # linha da Pessoa12, não a da Corretor
    assert tipo == "template" and from_c is True
    assert "Pessoa3" in texto and "Acme Imóveis" in texto


def test_nao_manda_duas_vezes_pro_mesmo_telefone(_armado):
    """Idempotência: o telefone pode ter 2 cards no remarketing; a pessoa é uma."""
    tel = _tel()
    _novo_lead(tel)
    _novo_lead(tel, ref=f"test:{tel}:2")

    res = hr.processar(tel, ["quero um apartamento"])

    assert len(res["leads"]) == 2
    assert len(_mensagens_do_aviso(tel)) == 1

    # e uma transferência nova no mesmo número (devolvido pra Corretor na mão) também
    # não repete a apresentação
    with SessionLocal() as s:
        assert hr._avisar_lead(s, telefone=tel, nome=None, corretora_id=CORRETOR,
                               corretora_label="Pessoa3") == {
            "ok": False, "motivo": "ja_avisado"}


def test_falha_de_envio_nao_derruba_a_transferencia(_armado, monkeypatch):
    """Meta fora do ar não segura lead na Corretor."""
    from app.api import cloud_corretora_a as cg

    monkeypatch.setattr(cg, "enviar_template_linha",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("Meta fora")))
    tel = _tel()
    lid = _novo_lead(tel)

    res = hr.processar(tel, ["quero um apartamento"])

    assert res["transferiu"] is True
    assert res["aviso"]["motivo"] == "envio_falhou"
    assert _le(lid)[0].owner_id == CORRETOR
    assert _mensagens_do_aviso(tel) == []


def test_corretora_sem_linha_cloud_nao_avisa(_armado, monkeypatch):
    from app.services import cloud_lines

    monkeypatch.setattr(cloud_lines, "linha_da_corretora", lambda *a, **k: None)
    tel = _tel()
    lid = _novo_lead(tel)

    res = hr.processar(tel, ["quero um apartamento"])

    assert res["transferiu"] is True and _le(lid)[0].owner_id == CORRETOR
    assert res["aviso"]["motivo"] == "corretora_sem_linha"
    assert _mensagens_do_aviso(tel) == []


def test_opt_out_nao_recebe(_armado, monkeypatch):
    monkeypatch.setattr("app.services.pos_ligacao._pediu_silencio", lambda s, t: True)
    tel = _tel()
    _novo_lead(tel)

    res = hr.processar(tel, ["quero um apartamento"])

    assert res["transferiu"] is True
    assert res["aviso"]["motivo"] == "opt_out"
    assert _mensagens_do_aviso(tel) == []


def test_texto_do_aviso_e_o_que_a_dona_ditou():
    """Dona 05/10/2026: "Olá, sou Fulana, estávamos falando em outro número,
    vamos continuar o atendimento por aqui!". O "Olá" é do casco do template;
    o texto vai na VARIÁVEL (a Meta classifica categoria pelo texto fixo)."""
    t = hr.texto_aviso("Pessoa3 Silva")
    assert t == ("Sou a Pessoa3, da Acme Imóveis. Estávamos falando em outro "
                 "número, vamos continuar o atendimento por aqui!")
    assert not t.lower().startswith("olá")
    assert hr.texto_aviso(None).startswith("Sou da equipe, da Acme")


def test_chave_do_aviso_nasce_desarmada():
    assert config_operacional.DEFAULTS["handoff_remarketing_avisa_lead"]() is False


def test_aviso_so_sai_em_template_utility():
    """Contrato com o envio: `enviar_template_linha` só deixa passar UTILITY
    (trava de categoria) — o handoff não pode ter um caminho paralelo."""
    fonte = inspect.getsource(hr._avisar_lead)
    assert "enviar_template_linha(" in fonte
    assert "permitir_marketing" not in fonte


def test_destino_default_e_a_corretor_hoje():
    from app.config import Settings

    assert Settings.model_fields["handoff_remarketing_corretora"].default == 3
    assert Settings.model_fields["handoff_remarketing_ativo"].default is True


# ── dedup do aviso vs mensagem que a Meta RECUSOU (18/09/2026) ───────────────

def test_aviso_falho_nao_conta_como_avisado():
    """Template recusado pela Meta não é apresentação feita.

    Em 18/09 as WABAs da Pessoa10 e da Pessoa11 travaram em "Business eligibility
    payment issue" e 18 leads do handoff foram marcados como avisados sem
    receber nada. Com o dedup cego ao status, ficariam sem a mensagem para
    sempre — inclusive depois de a cobrança voltar.
    """
    from sqlalchemy import text

    from app.db.session import SessionLocal
    from app.services import handoff_remarketing as _hr

    tel = "+5500000000000"
    wamid = "wamid.teste-dedup-falha"
    with SessionLocal() as s:
        s.execute(text("DELETE FROM mensagens_whatsapp WHERE evolution_message_id = :w"),
                  {"w": wamid})
        s.execute(
            text(
                "INSERT INTO mensagens_whatsapp (evolution_message_id, instance, "
                "  remote_jid, contato_telefone, from_corretora, tipo_mensagem, "
                "  conteudo_texto, conteudo_metadata, timestamp_evolution) "
                "VALUES (:w, 'acme_corretora_c', :jid, :tel, TRUE, 'template', 'x', "
                "        CAST(:meta AS jsonb), NOW())"
            ),
            {"w": wamid, "jid": f"{tel}@s.whatsapp.net", "tel": tel,
             "meta": '{"origem": "handoff_remarketing", '
                     '"delivery_status": "failed", '
                     '"delivery_error": "Business eligibility payment issue"}'},
        )
        s.commit()
    try:
        with SessionLocal() as s:
            assert _hr._ja_avisado(s, tel) is False
    finally:
        with SessionLocal() as s:
            s.execute(text("DELETE FROM mensagens_whatsapp WHERE evolution_message_id = :w"),
                      {"w": wamid})
            s.commit()


def test_aviso_entregue_continua_contando():
    """O dedup segue valendo pra quem REALMENTE recebeu — nada de mandar 2x."""
    from sqlalchemy import text

    from app.db.session import SessionLocal
    from app.services import handoff_remarketing as _hr

    tel = "+5500000000000"
    wamid = "wamid.teste-dedup-ok"
    with SessionLocal() as s:
        s.execute(text("DELETE FROM mensagens_whatsapp WHERE evolution_message_id = :w"),
                  {"w": wamid})
        s.execute(
            text(
                "INSERT INTO mensagens_whatsapp (evolution_message_id, instance, "
                "  remote_jid, contato_telefone, from_corretora, tipo_mensagem, "
                "  conteudo_texto, conteudo_metadata, timestamp_evolution) "
                "VALUES (:w, 'acme_corretora_c', :jid, :tel, TRUE, 'template', 'x', "
                "        CAST(:meta AS jsonb), NOW())"
            ),
            {"w": wamid, "jid": f"{tel}@s.whatsapp.net", "tel": tel,
             "meta": '{"origem": "handoff_remarketing", "delivery_status": "delivered"}'},
        )
        s.commit()
    try:
        with SessionLocal() as s:
            assert _hr._ja_avisado(s, tel) is True
    finally:
        with SessionLocal() as s:
            s.execute(text("DELETE FROM mensagens_whatsapp WHERE evolution_message_id = :w"),
                      {"w": wamid})
            s.commit()
# ── O resumo do que a pessoa falou (Dona 18/09) ──────────────────────────────

@pytest.mark.parametrize("fala,esperado", [
    ("Pode mandar por gentileza.", "eu podia te mandar as opções"),
    ("Manda pra mim", "eu podia te mandar as opções"),
    ("Zona leste", "procura em zona leste"),
    ("Investimento", "é pra investimento"),
    ("Morar", "é pra morar"),
    ("Gostaria de informações sobre lançamento",
     "queria informações sobre os lançamentos"),
    ("SAIBA MAIS", "queria saber mais"),
    ("Estou sim", "ainda estava procurando"),
])
def test_resumo_le_o_que_o_lead_disse(fala, esperado):
    assert hr.resumo_do_lead([fala]) == esperado


def test_resumo_sem_sinal_e_none():
    """"Obrigado" não é resumo. Sem sinal, o aviso volta pra forma de registro —
    dizer "você me disse que" sem saber o que foi seria inventar."""
    assert hr.resumo_do_lead(["Obrigado"]) is None
    assert hr.resumo_do_lead([]) is None
    assert hr.resumo_do_lead([None, "  "]) is None


def test_resumo_prefere_finalidade_a_consentimento():
    """Quem disse "é pra investir, pode mandar" tem MAIS conteúdo que o
    consentimento genérico — a corretora precisa do primeiro."""
    assert hr.resumo_do_lead(["pode mandar", "é pra investimento"]) == "é pra investimento"


def test_texto_aviso_com_resumo_lembra_a_conversa_e_convida():
    t = hr.texto_aviso("Pessoa5", "procura em zona leste")
    assert t.startswith("Aqui é a Pessoa5, da Acme Imóveis.")
    assert "você me disse que procura em zona leste" in t
    assert t.endswith("Podemos continuar por aqui?")


def test_texto_aviso_sem_resumo_mantem_a_forma_de_registro():
    assert hr.texto_aviso("Pessoa5") == hr.texto_aviso("Pessoa5", None)


def test_aviso_monta_o_resumo_das_falas_do_lead():
    """Contrato: com a opção do resumo ligada, o {{2}} sai da conversa real,
    não de texto fixo. (Desligada por padrão desde 05/10/2026.)"""
    fonte = inspect.getsource(hr._avisar_lead)
    assert "resumo_do_lead(" in fonte and "_falas_do_lead(" in fonte
    assert "if com_resumo else None" in fonte


def test_aviso_nao_atropela_conversa_em_andamento():
    """Piloto 18/09: 5 leads já transferidos e JÁ em conversa com a Pessoa12 entraram
    na varredura por estado. Apresentação pra quem já está sendo atendido é
    ruído — e o dedup do próprio aviso não pegava isso."""
    fonte = inspect.getsource(hr.motivo_para_pular)
    assert "_ja_conversou_com_a_corretora(" in fonte
    assert "conversa_em_andamento" in fonte
    # e o envio de verdade consulta essa lista de vetos
    assert "motivo_para_pular(" in inspect.getsource(hr._avisar_lead)
    # a linha da Corretor não conta como "a corretora falou"
    assert "coalesce(m.corretora_id, 0) <> :corretor" in hr._SQL_CONVERSOU_DEPOIS


@pytest.mark.parametrize("fala", [
    "Nunca estive procurando imóvel em Cidade para comprar. Apenas venda.",
    "Não estou procurando mais, obrigado",
    "não é pra investimento não",
])
def test_resumo_ignora_fala_com_negacao(fala):
    """Caso real (lead 5883): a região aparecia numa frase que NEGAVA a busca.
    "você me disse que procura em cidade" pra quem disse "nunca estive
    procurando" é pior que não mandar resumo nenhum."""
    assert hr.resumo_do_lead([fala]) is None


def test_corretor_espelha_a_pessoa_d_no_pos_ligacao():
    """Dona 20/09: "a Corretor vai ficar igual a Pessoa7 — ligação + primeira
    mensagem". Mesmo fluxo, prefixo próprio de template, e FORA da roleta do
    remarketing (ela não recebe lead, atende o que ligou)."""
    from app.services import cloud_lines as cl
    from app.services import pos_ligacao as pl

    assert pl._SLUG_POR_CORRETORA[8] == "corretor"
    assert pl._BASE_POR_SLUG["corretor"] == ["acme_an_atend_b", "acme_an_atend_a"]
    assert cl._INSTANCE_POR_SLUG["corretor"] == "acme_corretor"
    assert cl.prefixo_do_slug("corretor") == "acme_an_"
    # a roleta do remarketing continua sendo só Pessoa10, Pessoa11 e Pessoa12
    from app.config import Settings
    assert Settings.model_fields["handoff_remarketing_roleta"].default == [1, 2, 3]


def test_toda_linha_de_corretora_tem_waba_conhecida():
    """Sem WABA no mapa, `templates_utility` devolve set() e a linha fica MUDA —
    sem erro, sem log de falha. Foi o que aconteceu com a Corretor em 20/09: número
    conectado, templates aprovados na Meta, e o código não enxergava nenhum
    porque o slug não estava nem no mapa fixo nem no loop de env."""
    from app.services import cloud_lines as cl

    import inspect

    # a Pessoa11 entra pelo loop de env (`CLOUD_WABA_CORRETOR`), que não está setada na
    # suíte — por isso vale a fonte: ou o slug está no mapa fixo, ou o loop lê
    # a env dele. O que não pode é não estar em lugar nenhum, que foi o caso da
    # Corretor e deixou a linha muda.
    fonte = inspect.getsource(cl)
    por_env = fonte.split('for _s in (', 1)[1].split(')', 1)[0]
    for slug in ("corretor", "corretor", "corretor", "corretor", "pessoa_d", "corretor"):
        assert cl._WABA_POR_SLUG.get(slug) or f'"{slug}"' in por_env, (
            f"slug {slug} sem WABA conhecida: templates_utility devolveria set()"
        )
