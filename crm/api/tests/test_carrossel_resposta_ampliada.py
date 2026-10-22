"""A ampliação dos detectores NO MOTOR, com banco (05/10/2026).

`test_remarketing_resposta_ampliada.py` prova a função pura. Aqui é o caminho
inteiro do inbound da linha da Corretor: a mensagem chega, o motor decide se é
resposta DIRETA ao disparo, classifica, move o card e diz ao webhook o que
fazer com a Pessoa.

1. "Não" como primeira resposta ao disparo → Venda perdida;
2. "Não" depois de alguém já ter falado com o lead → não é lido (Respondeu);
3. número errado → Venda perdida, telefone marcado, e a Pessoa não é chamada;
4. o destino do "parar" é configuração: padrão Venda perdida, e a tela pode
   trocar pra Não perturbe — nunca pra uma etapa da roda;
5. a regra que decidiu fica gravada no evento;
6. o atendimento recebe a decisão (recusa / interesse) em vez de reclassificar.
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
from app.services import opt_out
from app.services import remarketing_resposta as rr

TENANT = 1
ADMIN = {"X-Painel-User": "dona"}
_PREFIXO = "5500000000000"
ETAPA_QUALQUER = 9301            # etapa do cliente que NÃO serve de destino do parar


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
        ses.execute(_t("DELETE FROM config_operacional WHERE chave = 'remarketing_etapa_parar'"))
        ses.commit()
        cfg.invalidar_cache()

    with SessionLocal() as ses:
        for i, nome, funil in (
            (cb.STAGE_FILA_PADRAO, "Fila", "remarketing"),
            (cb.STAGE_DISPARADO, "Disparado", "remarketing"),
            (cb.STAGE_RESPONDEU, "Respondeu", "remarketing"),
            (opt_out.ETAPA_NAO_PERTURBE, "Não perturbe", "principal"),
            (ETAPA_QUALQUER, "Em atendimento (teste)", "principal"),
        ):
            ses.execute(_t("INSERT INTO pipeline_stages (id, tenant_id, nome, funil) "
                           "VALUES (:i, :t, :n, :f) ON CONFLICT (id) DO NOTHING"),
                        {"i": i, "t": TENANT, "n": nome, "f": funil})
        if cb._stage_venda_perdida(ses, TENANT) is None:
            ses.execute(_t("INSERT INTO pipeline_stages (tenant_id, nome, funil) "
                           "VALUES (:t, 'Venda perdida', 'principal')"), {"t": TENANT})
        ses.commit()
        _limpar(ses)
        yield ses
        ses.rollback()
        _limpar(ses)


def _disparado(s, sufixo: str, ha_segundos: float = 600) -> tuple[str, int, datetime]:
    tel = f"{_PREFIXO}{sufixo}"
    lid = s.execute(
        _t("INSERT INTO leads (tenant_id, nome, telefone, owner_id, stage_id, created_at, updated_at) "
           "VALUES (:t, 'Teste Ampliada', :tel, :o, :st, now(), now()) RETURNING id"),
        {"t": TENANT, "tel": tel, "o": cb.CORRETORA_CORRETOR, "st": cb.STAGE_DISPARADO},
    ).scalar()
    em = datetime.now(timezone.utc) - timedelta(seconds=ha_segundos)
    s.execute(_t("INSERT INTO lead_events (tenant_id, lead_id, tipo, payload, at) "
                 "VALUES (:t, :i, :tp, CAST(:p AS jsonb), :at)"),
              {"t": TENANT, "i": lid, "tp": cb.EVENT_DISPARO, "at": em,
               "p": json.dumps({"variante": "direta", "categoria": "UTILITY"})})
    s.commit()
    return tel, int(lid), em


def _mensagem(s, tel: str, quando: datetime, *, de_nos: bool, tipo: str = "text",
              instance: str = cb.INSTANCE_CORRETOR) -> None:
    s.execute(
        _t("""INSERT INTO mensagens_whatsapp
                (id, evolution_message_id, instance, corretora_id, remote_jid,
                 contato_telefone, from_corretora, tipo_mensagem, conteudo_texto,
                 conteudo_metadata, timestamp_evolution, recebido_em, tenant_id)
              VALUES (:id, :w, :inst, NULL, :jid, :tel, :nos, :tp, 'x', '{}'::jsonb,
                      :ts, now(), 1)"""),
        {"id": str(uuid.uuid4()), "w": f"wamid.amp.{uuid.uuid4().hex[:10]}", "inst": instance,
         "jid": f"{tel}@s.whatsapp.net", "tel": tel, "nos": de_nos, "tp": tipo, "ts": quando},
    )
    s.commit()


def _etapa(s, lid: int) -> int:
    s.expire_all()
    return int(s.execute(_t("SELECT stage_id FROM leads WHERE id = :i"), {"i": lid}).scalar())


def _resposta(s, lid: int) -> dict:
    return dict(s.execute(
        _t("SELECT payload FROM lead_events WHERE lead_id = :i AND tipo = :tp ORDER BY at DESC LIMIT 1"),
        {"i": lid, "tp": cb.EVENT_RESPOSTA}).scalar())


def _perdida(s) -> int:
    return int(cb._stage_venda_perdida(s, TENANT))


def _chega(tel: str, texto: str, em: datetime, depois: float = 120) -> str | None:
    return cb.processar_inbound(tel, texto, TENANT, tipo="text",
                                recebido_em=em + timedelta(seconds=depois))


# ── 1 e 2. "Não": só como resposta direta ────────────────────────────────────


def test_nao_como_primeira_resposta_vai_pra_venda_perdida(s):
    tel, lid, em = _disparado(s, "01")
    # o template do próprio disparo, gravado no thread, não é "alguém no meio"
    _mensagem(s, tel, em + timedelta(seconds=1), de_nos=True, tipo="template")
    assert _chega(tel, "Não", em) == "recusa"
    assert _etapa(s, lid) == _perdida(s)
    ev = _resposta(s, lid)
    assert (ev["classe"], ev["regra"]) == (rr.RECUSA, rr.REGRA_NEGATIVA_CURTA)


def test_nao_depois_de_a_pessoa_ter_falado_nao_e_lido(s):
    """A Pessoa perguntou alguma coisa e o lead respondeu "Não": é resposta à
    Pessoa, não ao disparo. Fica em Respondeu, como sempre foi."""
    tel, lid, em = _disparado(s, "02")
    assert _chega(tel, "Bom dia", em, depois=60) == "respondeu"
    _mensagem(s, tel, em + timedelta(seconds=60), de_nos=False)            # o "bom dia"
    _mensagem(s, tel, em + timedelta(seconds=70), de_nos=True)             # a Pessoa respondeu
    # (card que já está em Respondeu não é movido de novo: o motor devolve
    # None, como sempre devolveu, e o webhook segue com o atendimento.)
    assert _chega(tel, "Não", em, depois=120) is None
    assert _etapa(s, lid) == cb.STAGE_RESPONDEU
    assert _resposta(s, lid)["classe"] == rr.INDEFINIDA


def test_nao_depois_de_uma_corretora_ter_falado_em_outra_linha_nao_e_lido(s):
    tel, lid, em = _disparado(s, "03")
    _mensagem(s, tel, em + timedelta(seconds=30), de_nos=True, instance="acme_corretor")
    assert _chega(tel, "Não", em) == "respondeu"
    assert _etapa(s, lid) == cb.STAGE_RESPONDEU


def test_nao_sem_hora_da_meta_nao_e_lido(s):
    """Sem `recebido_em` não dá pra saber se é a primeira mensagem."""
    tel, lid, _em = _disparado(s, "04")
    assert cb.processar_inbound(tel, "Não", TENANT, tipo="text") == "respondeu"
    assert _etapa(s, lid) == cb.STAGE_RESPONDEU


def test_nao_muito_tempo_depois_do_disparo_nao_e_resposta_direta(s):
    dias = cb.RESPOSTA_DIRETA_ATE_DIAS + 2
    tel, lid, em = _disparado(s, "05", ha_segundos=dias * 86400 + 600)
    assert _chega(tel, "Não", em, depois=dias * 86400) == "respondeu"
    assert _etapa(s, lid) == cb.STAGE_RESPONDEU


def test_ja_resolveu_e_sim_direto(s):
    tel, lid, em = _disparado(s, "06")
    assert _chega(tel, "Já compramos obrigada", em) == "recusa"
    assert _etapa(s, lid) == _perdida(s)
    assert _resposta(s, lid)["regra"] == rr.REGRA_JA_RESOLVEU

    tel2, lid2, em2 = _disparado(s, "07")
    assert _chega(tel2, "Olá, estou sim", em2) == "interesse_lido"
    assert _etapa(s, lid2) == cb.STAGE_RESPONDEU
    ev = _resposta(s, lid2)
    assert (ev["classe"], ev["regra"]) == (rr.INTERESSE, rr.REGRA_SIM_DIRETO)


def test_interesse_que_a_casa_ja_lia_continua_devolvendo_respondeu(s):
    """O valor novo (`interesse_lido`) é só pro que só a regra nova lê."""
    tel, lid, em = _disparado(s, "08")
    assert _chega(tel, "quero ver sim", em) == "respondeu"
    assert _resposta(s, lid)["regra"] == rr.REGRA_HANDOFF


# ── 3. Número errado ─────────────────────────────────────────────────────────


def test_numero_errado_vai_pra_venda_perdida_e_marca_o_telefone(s):
    tel, lid, em = _disparado(s, "10")
    assert _chega(tel, "Oi esse contato não é mais da Fulana", em) == "numero_errado"
    assert _etapa(s, lid) == _perdida(s)
    assert s.execute(_t("SELECT telefone_invalido FROM leads WHERE id = :i"),
                     {"i": lid}).scalar() is True
    assert s.execute(_t("SELECT count(*) FROM lead_events WHERE lead_id = :i "
                        "AND tipo = 'telefone_de_outra_pessoa'"), {"i": lid}).scalar() == 1
    assert _resposta(s, lid)["classe"] == rr.NUMERO_ERRADO


def test_numero_errado_nao_e_reaberto_por_um_quero(s):
    tel, lid, em = _disparado(s, "11")
    _chega(tel, "Número errado", em, depois=60)
    _chega(tel, "quero ver apartamento no centro", em, depois=180)
    assert _etapa(s, lid) == _perdida(s)


def test_numero_errado_entra_no_placar_fora_da_taxa(s):
    tel, lid, em = _disparado(s, "12")
    _chega(tel, "Este fone é do Beltrano", em)
    linhas = cb.placar(s, tenant_id=TENANT, desde=em - timedelta(seconds=5))
    total = {c: sum(l[c] for l in linhas) for c in cb.PLACAR_CONTAGENS}
    assert total["numeros_errados"] >= 1
    # não conta como resposta na taxa
    assert all(l["respostas"] == 0 for l in linhas if l["enviados"] == 1 and l["numeros_errados"] == 1)


def test_webhook_nao_chama_a_pessoa_pra_numero_errado_e_passa_a_decisao(monkeypatch):
    """O webhook recebe a decisão do motor e a repassa ao atendimento."""
    import inspect

    from app.api import cloud_corretora

    fonte = inspect.getsource(cloud_corretora.corretora_inbound)
    assert 'saida not in ("opt_out", "automatica", "numero_errado")' in fonte
    assert 'recusa_decidida=saida == "recusa"' in fonte
    assert 'interesse_decidido=saida == "interesse_lido"' in fonte


# ── 4. O destino do "parar" é configuração ───────────────────────────────────


def test_parar_vai_pra_venda_perdida_por_padrao(s):
    tel, lid, em = _disparado(s, "20")
    assert _chega(tel, "parar", em) == "opt_out"
    assert _etapa(s, lid) == _perdida(s)


def test_parar_vai_pra_nao_perturbe_quando_a_tela_grava(s):
    painel = TestClient(app)
    r = painel.put("/painel/automacoes/config/remarketing_etapa_parar",
                   json={"valor": opt_out.ETAPA_NAO_PERTURBE}, headers=ADMIN)
    assert r.status_code == 200, r.text

    tel, lid, em = _disparado(s, "21")
    assert _chega(tel, "parar", em) == "opt_out"
    assert _etapa(s, lid) == opt_out.ETAPA_NAO_PERTURBE
    # a RECUSA não segue o parar: continua indo pra Venda perdida
    tel2, lid2, em2 = _disparado(s, "22")
    _chega(tel2, "Não tenho interesse", em2)
    assert _etapa(s, lid2) == _perdida(s)

    # voltar ao padrão volta pra Venda perdida
    painel.delete("/painel/automacoes/config/remarketing_etapa_parar", headers=ADMIN)
    tel3, lid3, em3 = _disparado(s, "23")
    _chega(tel3, "parar", em3)
    assert _etapa(s, lid3) == _perdida(s)


@pytest.mark.parametrize("etapa", [cb.STAGE_FILA_PADRAO, cb.STAGE_DISPARADO,
                                   cb.STAGE_RESPONDEU, ETAPA_QUALQUER, 987654])
def test_tela_recusa_etapa_da_roda_como_destino_do_parar(s, etapa):
    r = TestClient(app).put("/painel/automacoes/config/remarketing_etapa_parar",
                            json={"valor": etapa}, headers=ADMIN)
    assert r.status_code == 422, r.text


def test_destino_torto_gravado_por_fora_cai_na_venda_perdida(s):
    """Alguém escreveu a Fila como destino do parar direto no banco: quem pede
    pra parar NÃO pode cair de volta na roda."""
    s.execute(_t("INSERT INTO config_operacional (chave, valor, atualizado_por) "
                 "VALUES ('remarketing_etapa_parar', CAST(:v AS jsonb), 'por fora')"),
              {"v": json.dumps(cb.STAGE_FILA_PADRAO)})
    s.commit()
    cfg.invalidar_cache()
    tel, lid, em = _disparado(s, "24")
    assert _chega(tel, "parar", em) == "opt_out"
    assert _etapa(s, lid) == _perdida(s)


# ── 6. O atendimento recebe a decisão ────────────────────────────────────────


def test_handoff_sobe_so_o_sem_sinal_quando_o_interesse_foi_lido(monkeypatch):
    from app.services import handoff_remarketing as hr

    visto: dict = {}

    def _para_antes_do_banco(*a, **k):
        raise RuntimeError("parou aqui de propósito")

    monkeypatch.setattr(hr.config_operacional, "handoff_remarketing_ativo", lambda: True)
    monkeypatch.setattr(hr.cb, "_so_digitos", _para_antes_do_banco)

    # "Olá, estou sim": o critério do handoff diz "sem sinal"; com a decisão, sobe
    sem = hr.processar("+5500000000000", ["Olá, estou sim"], None)
    assert sem["decisao"]["transferir"] is False and sem["motivo"] == "sem_sinal"
    com = hr.processar("+5500000000000", ["Olá, estou sim"], None, interesse_decidido=True)
    assert com["decisao"]["transferir"] is True
    assert "regra do remarketing" in com["decisao"]["motivo"]

    # veto continua veto: quem pediu pra parar ou recusou não é promovido
    for falas in (["parar"], ["não tenho interesse"]):
        r = hr.processar("+5500000000000", falas, None, interesse_decidido=True)
        assert r["decisao"]["transferir"] is False
    del visto


def test_atender_aceita_a_decisao_e_o_resto_do_sistema_chama_como_antes():
    import inspect

    from app.services import atendimento_corretor

    params = inspect.signature(atendimento_corretor.atender).parameters
    assert params["recusa_decidida"].default is False
    assert params["interesse_decidido"].default is False
    assert params["recusa_decidida"].kind is inspect.Parameter.KEYWORD_ONLY
    fonte = inspect.getsource(atendimento_corretor.atender)
    assert "recusou = recusa_decidida or any(_e_recusa_branda(f) for f in falas)" in fonte


# ── 7. A tela recebe as duas opções do "parar" e qual vale ───────────────────


def _mapa_do_carrossel(painel) -> dict:
    r = painel.get("/painel/automacoes", headers=ADMIN)
    assert r.status_code == 200, r.text
    return r.json()["carrossel_corretor"]


def test_mapa_traz_os_dois_destinos_do_parar_e_qual_vale(s):
    painel = TestClient(app)
    parar = _mapa_do_carrossel(painel)["parar"]
    assert parar["indisponivel"] is False
    c = parar["config"]
    assert c["padrao"] is True and c["etapa"] == _perdida(s) and c["invalido"] is None
    assert [(o["id"], o["nome"], o["padrao"]) for o in c["opcoes"]] == [
        (_perdida(s), "Venda perdida", True),
        (opt_out.ETAPA_NAO_PERTURBE, "Não perturbe", False),
    ]

    r = painel.put("/painel/automacoes/config/remarketing_etapa_parar",
                   json={"valor": opt_out.ETAPA_NAO_PERTURBE}, headers=ADMIN)
    assert r.status_code == 200, r.text
    c = _mapa_do_carrossel(painel)["parar"]["config"]
    assert c["padrao"] is False and c["etapa"] == opt_out.ETAPA_NAO_PERTURBE


def test_mapa_diz_quando_o_destino_gravado_nao_serve(s):
    s.execute(_t("INSERT INTO config_operacional (chave, valor, atualizado_por) "
                 "VALUES ('remarketing_etapa_parar', CAST(:v AS jsonb), 'por fora')"),
              {"v": json.dumps(cb.STAGE_FILA_PADRAO)})
    s.commit()
    cfg.invalidar_cache()
    c = _mapa_do_carrossel(TestClient(app))["parar"]["config"]
    # o motor cai na Venda perdida; a tela mostra a que VALE e o motivo
    assert c["etapa"] == _perdida(s) and c["padrao"] is False
    assert "Venda perdida" in c["invalido"]


def test_mapa_traz_o_aviso_do_telegram_desligado_por_padrao(s):
    painel = TestClient(app)
    with SessionLocal() as ses:
        ses.execute(_t("DELETE FROM config_operacional "
                       "WHERE chave = 'remarketing_conferente_avisa_telegram'"))
        ses.commit()
    cfg.invalidar_cache()
    assert _mapa_do_carrossel(painel)["aviso_telegram"] == {"ligado": False,
                                                           "indisponivel": False}
    r = painel.put("/painel/automacoes/config/remarketing_conferente_avisa_telegram",
                   json={"valor": True}, headers=ADMIN)
    assert r.status_code == 200, r.text
    assert _mapa_do_carrossel(painel)["aviso_telegram"]["ligado"] is True
    painel.delete("/painel/automacoes/config/remarketing_conferente_avisa_telegram",
                  headers=ADMIN)
