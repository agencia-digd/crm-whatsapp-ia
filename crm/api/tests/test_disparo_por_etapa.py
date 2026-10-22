"""Etapa escolhível — de qual etapa e de quem saem os leads do carrossel.

Até 05/10/2026 a etapa (`STAGE_FILA = 23`) e o dono (`CORRETORA_CORRETOR = 4`) eram
constantes. Viraram configuração (`remarketing_etapa` / `remarketing_dono`).

O laudo que originou isto avisou: as travas do `_SQL_FILA` dependem do dono.
Então estes testes não herdam a segurança — provam, com banco de verdade e a
etapa TROCADA:

1. sem nada gravado, o comportamento é o de antes (Fila 23, Corretor);
2. com a etapa trocada, o lote sai da etapa nova — e só dela;
3. lead de OUTRO cliente na mesma etapa NÃO entra;
4. lead de OUTRO dono na mesma etapa NÃO entra;
5. as travas de sempre (opt-out, conversa humana viva) valem na etapa nova;
6. etapa de outro cliente / de dentro da roda é recusada na gravação (422) e,
   se chegar no banco por fora da tela, o tick SEGURA em vez de adivinhar;
7. o lead volta pra etapa DE ONDE SAIU, não pra etapa configurada no dia.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text as _t

from app.db.session import SessionLocal
from app.main import app
from app.services import carrossel_corretor as cb
from app.services import config_operacional as cfg
from app.services import remarketing_alvo as ra

TENANT = 1
OUTRO_TENANT = 2
ADMIN = {"X-Painel-User": "dona"}
_PREFIXO = "5500000000000"          # faixa de telefone só destes testes

# Etapas criadas só pra estes testes (ids altos pra não colidir com o seed).
ETAPA_BASE = 9101                # do cliente, funil remarketing — a "etapa base"
ETAPA_DE_OUTRO_CLIENTE = 9102    # tenant 2
CORRETOR = 1                         # corretora humana do seed do conftest


def _agora() -> datetime:
    return datetime.now(timezone.utc)


def _lead(s, *, sufixo: str, etapa: int, dono: int = cb.CORRETORA_CORRETOR,
          tenant: int = TENANT, minutos_atras: int = 0) -> int:
    lid = s.execute(
        _t("""INSERT INTO leads (tenant_id, nome, telefone, owner_id, stage_id,
                                 created_at, updated_at)
              VALUES (:t, 'Teste Etapa', :tel, :o, :st, now(),
                      now() - make_interval(mins => :m))
           RETURNING id"""),
        {"t": tenant, "tel": f"{_PREFIXO}{sufixo}", "o": dono, "st": etapa,
         "m": minutos_atras},
    ).scalar()
    s.commit()
    return int(lid)


def _etapa_de(s, lead_id: int) -> int:
    return int(s.execute(_t("SELECT stage_id FROM leads WHERE id = :i"),
                         {"i": lead_id}).scalar())


def _eventos(s, lead_id: int, tipo: str) -> list[dict]:
    return [r[0] for r in s.execute(
        _t("SELECT payload FROM lead_events WHERE lead_id = :i AND tipo = :tp "
           "ORDER BY at"), {"i": lead_id, "tp": tipo}).all()]


class SenderQueEnvia:
    """Contrato do sender real quando a Meta ACEITA. Nada sai da máquina —
    isto é um objeto de teste; o envio de verdade está atrás do DRY_RUN."""

    def __init__(self):
        self.telefones: list[str] = []

    def __call__(self, telefone, texto, *, com_botao, **kw):
        self.telefones.append(telefone)
        return {"ok": True, "dry_run": False, "sent": True,
                "wamid": f"wamid.teste.{len(self.telefones)}",
                "template": "acme_corretor_ola_a_01", "categoria": "UTILITY"}


@pytest.fixture()
def s():
    def _limpar(ses):
        for sql in (
            "DELETE FROM lead_events WHERE lead_id IN "
            "(SELECT id FROM leads WHERE telefone LIKE :p)",
            "DELETE FROM mensagens_whatsapp WHERE contato_telefone LIKE :p",
            "DELETE FROM disparos_reengajamento WHERE telefone LIKE :p",
            "DELETE FROM cadencia_estado WHERE telefone LIKE :p",
            "DELETE FROM leads WHERE telefone LIKE :p",
        ):
            ses.execute(_t(sql), {"p": f"{_PREFIXO}%"})
        ses.execute(_t("DELETE FROM config_operacional WHERE chave LIKE 'remarketing_%'"))
        ses.commit()
        cfg.invalidar_cache()

    with SessionLocal() as ses:
        for i, t, nome in (
            (cb.STAGE_FILA_PADRAO, TENANT, "Fila"),
            (cb.STAGE_DISPARADO, TENANT, "Disparado"),
            (cb.STAGE_RESPONDEU, TENANT, "Respondeu"),
            (ETAPA_BASE, TENANT, "Base (teste)"),
            (ETAPA_DE_OUTRO_CLIENTE, OUTRO_TENANT, "Base de outro cliente"),
        ):
            ses.execute(_t("INSERT INTO pipeline_stages (id, tenant_id, nome, funil) "
                           "VALUES (:i, :t, :n, 'remarketing') "
                           "ON CONFLICT (id) DO NOTHING"), {"i": i, "t": t, "n": nome})
        ses.commit()
        _limpar(ses)
        yield ses
        ses.rollback()
        _limpar(ses)


@pytest.fixture()
def roda_ligada(monkeypatch, s):
    """Tudo liberado MENOS o que cada teste mexe: ligado, rampa GREEN folgada,
    sem ritmo de horário, fila de verdade no banco. `_SQL_FILA`, `_reservar` e
    `_disparar` são os REAIS — é neles que mora o que estes testes provam."""
    from app.services import rampa

    cfg.definir("carrossel_corretor_ativo", True, autor="teste")
    monkeypatch.setattr(cb, "travar", lambda ns, t: None)
    monkeypatch.setattr(cb, "soltar", lambda *a, **k: None)
    monkeypatch.setattr(cb, "_cota_ate_agora", lambda cap, agora=None, janela=None: cap)
    monkeypatch.setattr(cb, "_enviados_hoje", lambda ses, t: 0)
    monkeypatch.setattr(rampa, "cap_de_hoje",
                        lambda ses, slug, cid, **k: rampa.Rampa(slug, 80, 0, 9, "GREEN", "ok"))
    yield
    with SessionLocal() as ses:
        ses.execute(_t("DELETE FROM config_operacional WHERE chave = 'carrossel_corretor_ativo'"))
        ses.commit()
    cfg.invalidar_cache()


def _tick(s, sender=None, tenant: int = TENANT):
    sender = sender or SenderQueEnvia()
    res = cb.tick(session=s, sender=sender, tenant_id=tenant, max_por_tick=50)
    return res, sender


def _escolher(etapa: int | None = None, dono: int | None = None) -> None:
    """Grava direto na config, como a tela gravaria depois de validar."""
    if etapa is not None:
        cfg.definir("remarketing_etapa", etapa, autor="teste")
    if dono is not None:
        cfg.definir("remarketing_dono", dono, autor="teste")


# ── 1. Sem nada gravado: Fila 23, Corretor — como sempre ──────────────────────────


def test_o_padrao_continua_sendo_fila_23_e_corretor(s):
    alvo = ra.ler(s, TENANT)
    assert (alvo.etapa, alvo.dono) == (23, 4)
    assert alvo.padrao
    assert cfg.DEFAULTS["remarketing_etapa"]() == 23
    assert cfg.DEFAULTS["remarketing_dono"]() == 4


def test_sem_config_o_lote_sai_da_fila_23(s, roda_ligada):
    na_fila = _lead(s, sufixo="001", etapa=cb.STAGE_FILA_PADRAO)
    na_base = _lead(s, sufixo="002", etapa=ETAPA_BASE)

    res, sender = _tick(s)

    assert res.enviados == 1
    assert _etapa_de(s, na_fila) == cb.STAGE_DISPARADO
    assert _etapa_de(s, na_base) == ETAPA_BASE, "pegou lead de etapa que ninguém escolheu"


# ── 2. Etapa trocada: sai da nova, e só dela ─────────────────────────────────


def test_etapa_escolhida_e_de_onde_o_lote_sai(s, roda_ligada):
    na_fila = _lead(s, sufixo="011", etapa=cb.STAGE_FILA_PADRAO)
    na_base = _lead(s, sufixo="012", etapa=ETAPA_BASE)
    _escolher(etapa=ETAPA_BASE)

    res, sender = _tick(s)

    assert res.enviados == 1
    assert sender.telefones == [f"{_PREFIXO}012"]
    assert _etapa_de(s, na_base) == cb.STAGE_DISPARADO
    assert _etapa_de(s, na_fila) == cb.STAGE_FILA_PADRAO, \
        "com a etapa trocada, a Fila 23 não pode mais ser tocada"


# ── 3 e 4. As travas que dependem de tenant e de dono, com a etapa trocada ───


def test_lead_de_OUTRO_CLIENTE_na_mesma_etapa_NAO_entra(s, roda_ligada):
    """Mesma etapa, mesmo dono, tenant diferente."""
    meu = _lead(s, sufixo="021", etapa=ETAPA_BASE)
    alheio = _lead(s, sufixo="022", etapa=ETAPA_BASE, tenant=OUTRO_TENANT)
    _escolher(etapa=ETAPA_BASE)

    res, sender = _tick(s)

    assert sender.telefones == [f"{_PREFIXO}021"]
    assert _etapa_de(s, meu) == cb.STAGE_DISPARADO
    assert _etapa_de(s, alheio) == ETAPA_BASE
    assert _eventos(s, alheio, cb.EVENT_DISPARO) == []


def test_lead_de_OUTRO_DONO_na_mesma_etapa_NAO_entra(s, roda_ligada):
    """A "etapa base" de produção tem 121 leads e NENHUM da Corretor: são de
    corretora. Escolher a etapa não pode arrastar lead de gente pro robô."""
    da_corretor = _lead(s, sufixo="031", etapa=ETAPA_BASE)
    da_corretor = _lead(s, sufixo="032", etapa=ETAPA_BASE, dono=CORRETOR)
    _escolher(etapa=ETAPA_BASE)

    res, sender = _tick(s)

    assert sender.telefones == [f"{_PREFIXO}031"]
    assert _etapa_de(s, da_corretor) == cb.STAGE_DISPARADO
    assert _etapa_de(s, da_corretor) == ETAPA_BASE, "lead de corretora entrou no lote"
    assert _eventos(s, da_corretor, cb.EVENT_DISPARO) == []
    assert _eventos(s, da_corretor, cb.EVENT_RESERVA) == []


def test_etapa_so_com_lead_de_corretora_nao_dispara_nada(s, roda_ligada):
    """O caso real da 'etapa base': escolher e não sair nada tem que ser zero
    envio limpo, não erro nem lead alheio."""
    _lead(s, sufixo="041", etapa=ETAPA_BASE, dono=CORRETOR)
    _escolher(etapa=ETAPA_BASE)

    res, sender = _tick(s)

    assert sender.telefones == []
    assert res.enviados == 0 and res.disponiveis == 0


def test_dono_que_muda_entre_a_fila_e_a_reserva_nao_recebe(s, roda_ligada, monkeypatch):
    """Handoff no meio do tick: o lead estava na fila quando o lote foi lido e
    virou da corretora antes do claim. O `_reservar` confere o dono de novo."""
    lead = _lead(s, sufixo="051", etapa=ETAPA_BASE)
    _escolher(etapa=ETAPA_BASE)
    reservar_real = cb._reservar

    def _handoff_e_reserva(ses, lead_id, tenant_id, *a):
        ses.execute(_t("UPDATE leads SET owner_id = :j WHERE id = :i"),
                    {"j": CORRETOR, "i": lead_id})
        ses.commit()
        return reservar_real(ses, lead_id, tenant_id, *a)

    monkeypatch.setattr(cb, "_reservar", _handoff_e_reserva)
    res, sender = _tick(s)

    assert sender.telefones == []
    assert _etapa_de(s, lead) == ETAPA_BASE
    assert res.bloqueios.get("reservado_por_outro_tick") == 1


# ── 5. As travas de sempre valem na etapa nova ───────────────────────────────


def test_opt_out_vale_na_etapa_escolhida(s, roda_ligada):
    livre = _lead(s, sufixo="061", etapa=ETAPA_BASE)
    saiu = _lead(s, sufixo="062", etapa=ETAPA_BASE)
    s.execute(_t("""INSERT INTO disparos_reengajamento
                      (tenant_id, corretora_id, telefone, template, enviado_por,
                       enviado_em, opt_out, opt_out_em)
                    VALUES (:t, 4, :tel, 'x', 'teste', now(), TRUE, now())"""),
              {"t": TENANT, "tel": f"{_PREFIXO}062"})
    s.commit()
    _escolher(etapa=ETAPA_BASE)

    res, sender = _tick(s)

    assert sender.telefones == [f"{_PREFIXO}061"]
    assert _etapa_de(s, saiu) == ETAPA_BASE


def test_conversa_humana_viva_vale_na_etapa_escolhida(s, roda_ligada):
    """Robô não fala por cima de gente — também na etapa nova."""
    livre = _lead(s, sufixo="071", etapa=ETAPA_BASE)
    em_conversa = _lead(s, sufixo="072", etapa=ETAPA_BASE)
    tel = f"{_PREFIXO}072"
    s.execute(
        _t("""INSERT INTO mensagens_whatsapp
                (id, evolution_message_id, instance, corretora_id, remote_jid,
                 contato_telefone, from_corretora, tipo_mensagem, conteudo_texto,
                 conteudo_metadata, timestamp_evolution, recebido_em, tenant_id)
              VALUES (:id, :w, 'acme_corretora_c', NULL, :jid, :tel, TRUE,
                      'conversation', 'oi, tudo bem?', NULL, :ts, now(), :t)"""),
        {"id": str(uuid.uuid4()), "w": str(uuid.uuid4()),
         "jid": f"{tel}@s.whatsapp.net", "tel": tel,
         "ts": _agora() - timedelta(days=1), "t": TENANT})
    s.commit()
    _escolher(etapa=ETAPA_BASE)

    res, sender = _tick(s)

    assert sender.telefones == [f"{_PREFIXO}071"]
    assert _etapa_de(s, em_conversa) == ETAPA_BASE


# ── 6. Etapa que não serve: recusada na tela, segurada no tick ───────────────


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


@pytest.mark.parametrize("valor,trecho", [
    (ETAPA_DE_OUTRO_CLIENTE, "outro cliente"),
    (987654, "não existe"),
    (cb.STAGE_DISPARADO, "Disparado"),
    (cb.STAGE_RESPONDEU, "Respondeu"),
    (26, "Não perturbe"),
    ("23", "número"),
    (True, "número"),
    (-5, "inválid"),
])
def test_endpoint_recusa_etapa_que_nao_serve(client, s, valor, trecho):
    r = client.put("/painel/automacoes/config/remarketing_etapa",
                   json={"valor": valor}, headers=ADMIN)
    assert r.status_code == 422, r.text
    assert trecho in r.json()["detail"]
    assert s.execute(_t("SELECT count(*) FROM config_operacional "
                        "WHERE chave = 'remarketing_etapa'")).scalar() == 0, \
        "etapa recusada não pode ter sido gravada"


def test_endpoint_grava_etapa_do_cliente_e_o_motor_le(client, s):
    r = client.put("/painel/automacoes/config/remarketing_etapa",
                   json={"valor": ETAPA_BASE}, headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["config"]["remarketing_etapa"]["valor"] == ETAPA_BASE
    assert ra.ler(s, TENANT).etapa == ETAPA_BASE


@pytest.mark.parametrize("valor,trecho", [
    (987654, "não existe"), ("4", "número"),
])
def test_endpoint_recusa_dono_que_nao_serve(client, s, valor, trecho):
    r = client.put("/painel/automacoes/config/remarketing_dono",
                   json={"valor": valor}, headers=ADMIN)
    assert r.status_code == 422, r.text
    assert trecho in r.json()["detail"]


def test_gravar_a_etapa_exige_gestao(client, s):
    r = client.put("/painel/automacoes/config/remarketing_etapa",
                   json={"valor": ETAPA_BASE}, headers={"X-Painel-User": "corretora_c"})
    assert r.status_code in (401, 403), r.text


@pytest.mark.parametrize("etapa_no_banco,motivo", [
    (ETAPA_DE_OUTRO_CLIENTE, "etapa_invalida"),
    (cb.STAGE_DISPARADO, "etapa_invalida"),
    (987654, "etapa_invalida"),
    ("vinte e três", "etapa_invalida"),
])
def test_etapa_torta_gravada_por_fora_SEGURA_o_tick(s, roda_ligada, etapa_no_banco, motivo):
    """Alguém escreveu no banco por fora da tela. O tick não cai no padrão
    (seria disparar pra uma lista que ninguém escolheu): não envia e diz por quê."""
    na_fila = _lead(s, sufixo="081", etapa=cb.STAGE_FILA_PADRAO)
    alheio = _lead(s, sufixo="082", etapa=ETAPA_DE_OUTRO_CLIENTE, tenant=OUTRO_TENANT)
    _escolher(etapa=etapa_no_banco)

    res, sender = _tick(s)

    assert sender.telefones == []
    assert res.enviados == 0
    assert res.motivo_parada == motivo
    assert _etapa_de(s, na_fila) == cb.STAGE_FILA_PADRAO


def test_o_SQL_da_fila_recusa_etapa_de_outro_cliente_sozinho(s):
    """Última linha de defesa: mesmo que a validação da tela E a do tick
    falhassem, o `_SQL_FILA` não devolve lead de etapa que não é do cliente.
    Aqui o lead é do tenant 1 mas está numa etapa do tenant 2 (dado torto)."""
    _lead(s, sufixo="091", etapa=ETAPA_DE_OUTRO_CLIENTE, tenant=TENANT)
    fila = s.execute(_t(cb._SQL_FILA), {
        "t": TENANT, "dono": cb.CORRETORA_CORRETOR, "fila": ETAPA_DE_OUTRO_CLIENTE,
        "lim": 10, "pausa_dias": 7}).mappings().all()
    assert fila == []


# ── 7. Volta pra etapa de onde saiu ──────────────────────────────────────────


def _envelhecer_saida(s, lead_id: int, dias: int) -> None:
    s.execute(_t("UPDATE lead_events SET at = now() - make_interval(days => :d) "
                 "WHERE lead_id = :i AND tipo = :tp"),
              {"d": dias, "i": lead_id, "tp": cb.EVENT_DISPARO})
    s.commit()


def test_o_disparo_grava_de_onde_o_lead_saiu(s, roda_ligada):
    lead = _lead(s, sufixo="101", etapa=ETAPA_BASE)
    _escolher(etapa=ETAPA_BASE)
    _tick(s)
    (payload,) = _eventos(s, lead, cb.EVENT_DISPARO)
    assert payload["etapa_origem"] == ETAPA_BASE
    assert payload["dono"] == cb.CORRETORA_CORRETOR
    assert payload["linha"] == "corretor"


def test_lead_volta_pra_etapa_de_ORIGEM_nao_pra_configurada_hoje(s, roda_ligada):
    """Saiu da Base; enquanto descansava, a tela voltou pro padrão (Fila 23).
    Ele volta pra Base — trocar a etapa não despeja ninguém em funil alheio."""
    lead = _lead(s, sufixo="111", etapa=ETAPA_BASE)
    _escolher(etapa=ETAPA_BASE)
    _tick(s)
    assert _etapa_de(s, lead) == cb.STAGE_DISPARADO

    cfg.restaurar_padrao("remarketing_etapa", autor="teste")   # tela voltou pro 23
    _envelhecer_saida(s, lead, cb.DESCANSO_DIAS + 1)
    n = cb._voltar_pra_fila(s, TENANT)

    assert n == 1
    assert _etapa_de(s, lead) == ETAPA_BASE


def test_disparo_antigo_sem_origem_gravada_volta_pra_fila_23(s):
    """Os 3.087 que estão em Disparado hoje não têm `etapa_origem` no evento:
    voltam pra Fila 23, exatamente como o SQL de antes fazia."""
    lead = _lead(s, sufixo="121", etapa=cb.STAGE_DISPARADO)
    s.execute(_t("""INSERT INTO lead_events (tenant_id, lead_id, tipo, payload, at)
                    VALUES (:t, :i, :tp, CAST(:p AS jsonb), now() - interval '40 days')"""),
              {"t": TENANT, "i": lead, "tp": cb.EVENT_DISPARO,
               "p": json.dumps({"variante": "x", "linha": "corretor"})})
    s.commit()
    cfg.definir("remarketing_etapa", ETAPA_BASE, autor="teste")   # tela aponta pra Base

    assert cb._voltar_pra_fila(s, TENANT) == 1
    assert _etapa_de(s, lead) == cb.STAGE_FILA_PADRAO


def test_quem_ainda_descansa_nao_volta(s, roda_ligada):
    lead = _lead(s, sufixo="131", etapa=ETAPA_BASE)
    _escolher(etapa=ETAPA_BASE)
    _tick(s)
    _envelhecer_saida(s, lead, cb.DESCANSO_DIAS - 2)
    assert cb._voltar_pra_fila(s, TENANT) == 0
    assert _etapa_de(s, lead) == cb.STAGE_DISPARADO


def test_transferido_enquanto_descansava_nao_volta(s, roda_ligada):
    """O dono mudou (handoff) com o lead em Disparado: deixou de ser da roda."""
    lead = _lead(s, sufixo="141", etapa=ETAPA_BASE)
    _escolher(etapa=ETAPA_BASE)
    _tick(s)
    s.execute(_t("UPDATE leads SET owner_id = :j WHERE id = :i"), {"j": CORRETOR, "i": lead})
    s.commit()
    _envelhecer_saida(s, lead, cb.DESCANSO_DIAS + 1)
    assert cb._voltar_pra_fila(s, TENANT) == 0
    assert _etapa_de(s, lead) == cb.STAGE_DISPARADO


# ── 8. O que a tela recebe pro seletor ───────────────────────────────────────


def test_seletor_recebe_as_etapas_REAIS_do_cliente_com_a_contagem_do_dono(s):
    _lead(s, sufixo="151", etapa=ETAPA_BASE)
    _lead(s, sufixo="152", etapa=ETAPA_BASE, dono=CORRETOR)
    _lead(s, sufixo="153", etapa=ETAPA_BASE, dono=CORRETOR)

    resumo = ra.resumo(s, TENANT)
    por_id = {e["id"]: e for e in resumo["etapas"]}

    assert ETAPA_DE_OUTRO_CLIENTE not in por_id, "etapa de outro cliente no seletor"
    assert por_id[ETAPA_BASE]["leads"] == 3
    assert por_id[ETAPA_BASE]["leads_do_dono"] == 1, \
        "a tela precisa mostrar quantos o disparo PEGARIA, não quantos existem"
    assert por_id[cb.STAGE_DISPARADO]["permitida"] is False
    assert por_id[cb.STAGE_FILA_PADRAO]["padrao"] is True
    assert resumo["etapa"] == cb.STAGE_FILA_PADRAO


def test_resumo_com_alvo_torto_diz_que_esta_invalido(s):
    cfg.definir("remarketing_etapa", ETAPA_DE_OUTRO_CLIENTE, autor="teste")
    resumo = ra.resumo(s, TENANT)
    assert resumo["etapa"] is None
    assert resumo["motivo"] == "etapa_invalida"
    assert "outro cliente" in resumo["invalido"]


def test_estoque_da_tela_conta_a_etapa_configurada(s):
    """A caixa 'esperando agora' tem que ser a fila que o motor vai usar."""
    from app.api.automacoes import _estoque_carrossel

    _lead(s, sufixo="161", etapa=ETAPA_BASE)
    _lead(s, sufixo="162", etapa=ETAPA_BASE)
    cfg.definir("remarketing_etapa", ETAPA_BASE, autor="teste")
    assert _estoque_carrossel(s)["fila"] == 2
