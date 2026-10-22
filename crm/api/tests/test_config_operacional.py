"""Painel de controle da operação — `config_operacional` + endpoints.

O que está em jogo: ligar/desligar a cadência, mexer no limite diário de uma
linha e escolher quem entra na roleta saíram do `.env` e viraram controle de
tela. Isso é o que transforma painel em produto — e também o que, mal feito,
deixa alguém desligar a operação sem querer ou estourar o limite de uma linha
quente.

As garantias que os testes seguram:
  1. Sem linha no banco, vale o `.env` — a migração é invisível pra Acme;
  2. Banco fora do ar não vira "cadência desligada" por acidente;
  3. Valor absurdo é recusado ANTES de virar mensagem a mais na linha;
  4. Roleta vazia é recusada: lead novo nascia sem dono e sumia do CRM;
  5. Dá pra voltar ao padrão — configuração sem volta é armadilha.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text as sql_text

from app.config import settings
from app.db.session import SessionLocal
from app.main import app
from app.services import config_operacional as cfg


@pytest.fixture(autouse=True)
def _limpo():
    def _wipe():
        with SessionLocal() as s:
            s.execute(sql_text("DELETE FROM config_operacional"))
            s.commit()
        cfg.invalidar_cache()

    _wipe()
    yield
    _wipe()


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


def _corretora(cid: int, nome: str) -> None:
    with SessionLocal() as s:
        s.execute(sql_text(
            "INSERT INTO corretoras (id, nome, telefone, ativo, created_at, papel, "
            "tenant_id, canal, briefing_optout) "
            "VALUES (:i, :n, :t, TRUE, NOW(), 'corretora', 1, 'cloud', FALSE) "
            "ON CONFLICT (id) DO UPDATE SET ativo = TRUE"),
            {"i": cid, "n": nome, "t": f"+5500000000000{cid:02d}"})
        s.commit()


# ── 1. Fallback: sem linha no banco, vale o .env ─────────────────────────────


def test_sem_configuracao_vale_o_env():
    """A migração tem que ser invisível: enquanto ninguém tocar no painel, o
    motor lê exatamente o que lia antes."""
    assert cfg.cadencia_ativa() == bool(settings.cadencia_ativa)
    assert cfg.cap_diario() == int(settings.cadencia_cap_diario)
    assert cfg.roleta_corretoras() == [int(x) for x in settings.roleta_corretoras]


def test_banco_fora_do_ar_nao_desliga_a_cadencia(monkeypatch):
    """O modo de falha que importa: se o banco cair no meio do tick, a cadência
    NÃO pode virar 'desligada'. Cai no valor do arquivo e segue."""
    def _explode():
        raise RuntimeError("banco fora")

    monkeypatch.setattr(cfg, "SessionLocal", _explode)
    cfg.invalidar_cache()
    assert cfg.cadencia_ativa() == bool(settings.cadencia_ativa)


# ── 2. Gravar e voltar atrás ────────────────────────────────────────────────


def test_gravar_muda_o_valor_efetivo():
    cfg.definir("cadencia_cap_diario", 42, autor="dona")
    assert cfg.cap_diario() == 42


def test_restaurar_padrao_volta_pro_env():
    cfg.definir("cadencia_cap_diario", 42, autor="dona")
    cfg.restaurar_padrao("cadencia_cap_diario", autor="dona")
    assert cfg.cap_diario() == int(settings.cadencia_cap_diario)


def test_cap_da_corretora_cai_no_global_quando_nao_tem_o_dela():
    cfg.definir("cadencia_cap_diario", 30, autor="dona")
    cfg.definir("cadencia_cap_por_corretora", {"2": 80}, autor="dona")
    assert cfg.cap_da_corretora(2) == 80
    assert cfg.cap_da_corretora(9) == 30
    assert cfg.cap_da_corretora(None) == 30


def test_estado_marca_o_que_veio_do_painel():
    """Sem essa marca não dá pra saber se o botão foi mexido ou se é o padrão."""
    cfg.definir("cadencia_ativa", False, autor="dona")
    st = cfg.estado_atual()
    assert st["cadencia_ativa"]["personalizado"] is True
    assert st["cadencia_ativa"]["valor"] is False
    assert st["cadencia_ativa"]["padrao"] == bool(settings.cadencia_ativa)
    assert st["cadencia_cap_diario"]["personalizado"] is False


def test_chave_desconhecida_e_recusada():
    """Evita o 'cadencia_ativo' sem o 'a' — chave que grava e nunca é lida."""
    with pytest.raises(KeyError):
        cfg.definir("cadencia_ativo", True, autor="dona")


# ── 3. Endpoints (admin-only) ───────────────────────────────────────────────


def test_endpoint_le_o_estado(client):
    r = client.get("/painel/automacoes/config", headers={"X-Painel-User": "dona"})
    assert r.status_code == 200, r.text
    assert set(r.json()["config"]) == set(cfg.DEFAULTS)


def test_corretora_nao_mexe_na_configuracao(client):
    """Quem opera não desliga a automação da casa."""
    r = client.put("/painel/automacoes/config/cadencia_ativa",
                   json={"valor": False}, headers={"X-Painel-User": "corretor"})
    assert r.status_code == 403


def test_desligar_e_religar_pelo_endpoint(client):
    r = client.put("/painel/automacoes/config/cadencia_ativa",
                   json={"valor": False}, headers={"X-Painel-User": "dona"})
    assert r.status_code == 200, r.text
    assert cfg.cadencia_ativa() is False

    r = client.delete("/painel/automacoes/config/cadencia_ativa",
                      headers={"X-Painel-User": "dona"})
    assert r.status_code == 200
    assert cfg.cadencia_ativa() == bool(settings.cadencia_ativa)


# ── 4. Validação — o que protege a linha ────────────────────────────────────


@pytest.mark.parametrize("valor", [999, -1, "50", 3.5, True])
def test_cap_absurdo_e_recusado(client, valor):
    """250/dia é o teto inicial da Cloud API. Número frio saltando pro teto
    perde qualidade e leva bloqueio — quase custou o 0000, telefone da casa."""
    r = client.put("/painel/automacoes/config/cadencia_cap_diario",
                   json={"valor": valor}, headers={"X-Painel-User": "dona"})
    assert r.status_code == 422, f"aceitou {valor!r}"


def test_roleta_vazia_e_recusada(client):
    """Roleta vazia faz lead novo nascer sem dono — e como owner_id não tem
    chave estrangeira, ele some do CRM de todo mundo, em silêncio."""
    r = client.put("/painel/automacoes/config/roleta_corretoras",
                   json={"valor": []}, headers={"X-Painel-User": "dona"})
    assert r.status_code == 422


def test_roleta_com_corretora_inexistente_e_recusada(client):
    r = client.put("/painel/automacoes/config/roleta_corretoras",
                   json={"valor": [1, 4242]}, headers={"X-Painel-User": "dona"})
    assert r.status_code == 422
    assert "4242" in r.text


def test_roleta_com_repetida_e_recusada(client):
    _corretora(1, "Corretor")
    r = client.put("/painel/automacoes/config/roleta_corretoras",
                   json={"valor": [1, 1]}, headers={"X-Painel-User": "dona"})
    assert r.status_code == 422


def test_roleta_valida_guarda_a_ordem(client):
    """A ordem é o estado do rodízio — trocar a ordem muda quem recebe agora."""
    _corretora(1, "Corretor")
    _corretora(2, "Bruno")
    r = client.put("/painel/automacoes/config/roleta_corretoras",
                   json={"valor": [2, 1]}, headers={"X-Painel-User": "dona"})
    assert r.status_code == 200, r.text
    assert cfg.roleta_corretoras() == [2, 1]


def test_cap_por_corretora_recusa_limite_absurdo(client):
    r = client.put("/painel/automacoes/config/cadencia_cap_por_corretora",
                   json={"valor": {"1": 5000}}, headers={"X-Painel-User": "dona"})
    assert r.status_code == 422


# ── 5. O motor lê o que o painel gravou ─────────────────────────────────────


def test_motor_da_cadencia_respeita_o_desligamento():
    """O teste que fecha o circuito: desligar na tela para o tick de verdade."""
    from app.services import cadencia

    cfg.definir("cadencia_ativa", False, autor="dona")
    resultado = cadencia.tick()
    assert resultado.considerados == 0
    assert resultado.mensagens_enfileiradas == 0
    assert resultado.tarefas_ligacao == 0


# ── 6. Boas-vindas: kill-switch, copies e pesos do A/B ──────────────────────
#
# O interruptor da página de boas-vindas era decorativo: `lead_intake_ativo` só
# existia como env (LEAD_INTAKE_ATIVO). Agora é chave de painel, com o mesmo
# fallback das outras — e o motor lê DELA.


def test_lead_intake_sem_linha_no_banco_vale_o_env():
    assert cfg.lead_intake_ativo() == bool(settings.lead_intake_ativo)


def test_interruptor_da_boas_vindas_liga_de_verdade(client):
    r = client.put("/painel/automacoes/config/lead_intake_ativo",
                   json={"valor": True}, headers={"X-Painel-User": "dona"})
    assert r.status_code == 200, r.text
    assert cfg.lead_intake_ativo() is True

    r = client.delete("/painel/automacoes/config/lead_intake_ativo",
                      headers={"X-Painel-User": "dona"})
    assert r.status_code == 200
    assert cfg.lead_intake_ativo() == bool(settings.lead_intake_ativo)


def test_motor_do_intake_le_a_chave_do_painel(monkeypatch):
    """Fecha o circuito: com o env DESLIGADO, ligar na tela faz o intake rodar."""
    from app.services import lead_intake

    monkeypatch.setattr(settings, "lead_intake_ativo", False)
    cfg.invalidar_cache()
    assert lead_intake.intake_lead(nome="x", telefone="+5500000000000") is None

    cfg.definir("lead_intake_ativo", True, autor="dona")
    assert cfg.lead_intake_ativo() is True


@pytest.mark.parametrize("valor", ["true", 1, None])
def test_interruptor_so_aceita_booleano(client, valor):
    r = client.put("/painel/automacoes/config/lead_intake_ativo",
                   json={"valor": valor}, headers={"X-Painel-User": "dona"})
    assert r.status_code == 422, f"aceitou {valor!r}"


def test_pesos_do_ab_precisam_fechar_100(client):
    from app.services import boas_vindas_copies as bvc

    ids = [v.id for v in bvc.VARIANTES]
    r = client.put("/painel/automacoes/config/boas_vindas_pesos",
                   json={"valor": {ids[0]: 10}}, headers={"X-Painel-User": "dona"})
    assert r.status_code == 422
    assert "100" in r.text


def test_pesos_do_ab_validos_valem_no_balde(client):
    from app.services import boas_vindas_copies as bvc

    ids = [v.id for v in bvc.VARIANTES]
    pesos = {ids[0]: 100, **{i: 0 for i in ids[1:]}}
    r = client.put("/painel/automacoes/config/boas_vindas_pesos",
                   json={"valor": pesos}, headers={"X-Painel-User": "dona"})
    assert r.status_code == 200, r.text
    assert [v.id for v in bvc.ativas()] == [ids[0]]
    assert bvc.escolher(12345).id == ids[0]


def test_copy_do_ab_editada_no_painel_vale_no_envio(client):
    from app.services import boas_vindas_copies as bvc
    from app.services.lead_intake import boas_vindas_texto

    vid = bvc.CONTROLE_ID
    novo = "Sou a {nome}, da Acme. Texto novo, escrito no painel?"
    r = client.put("/painel/automacoes/config/boas_vindas_copies",
                   json={"valor": {vid: novo}}, headers={"X-Painel-User": "dona"})
    assert r.status_code == 200, r.text
    variante = bvc.por_id(vid)
    assert variante is not None and variante.texto == novo
    assert boas_vindas_texto(1, variante=variante) == novo.replace("{nome}", "Pessoa5")


def test_copy_do_ab_recusa_placeholder_que_o_motor_nao_substitui(client):
    from app.services import boas_vindas_copies as bvc

    r = client.put(
        "/painel/automacoes/config/boas_vindas_copies",
        json={"valor": {bvc.CONTROLE_ID: "Sou a {nome} e {{1}} sai literal?"}},
        headers={"X-Painel-User": "dona"},
    )
    assert r.status_code == 422
    assert "placeholder" in r.text
