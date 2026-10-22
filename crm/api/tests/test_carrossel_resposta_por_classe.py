"""A resposta do lead ao remarketing move o card pela CLASSE (Dona 05/10/2026).

    interesse / botão / indefinida → Respondeu
    recusa / parar                 → Venda perdida, fora da roda de vez
    automática                     → fica em Disparado e fora da taxa

O que estes testes protegem, em ordem:
1. Cada classe leva o card pra etapa certa e devolve o que o webhook espera.
2. Venda perdida é saída definitiva: nem a volta dos 30 dias, nem a dos 45, nem
   a fila repescam — e a tela não deixa escolher a etapa como origem.
3. Resposta automática não mexe no card, não carimba a variante e cala a Pessoa.
4. A taxa só conta interesse e botão; o resto aparece em coluna própria.
5. Recusa seguida de interesse explícito reabre (caso Renato); educação não.
6. Quem já é de corretora não é tocado.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import text as _t

from app.db.session import SessionLocal
from app.services import carrossel_corretor as cb
from app.services import config_operacional, remarketing_alvo
from app.services import remarketing_resposta as rr

TENANT = 1
_PREFIXO = "5500000000000"     # faixa só destes testes
CORRETOR = 3                      # corretora humana do seed


def _agora() -> datetime:
    return datetime.now(timezone.utc)


def _perdida(s) -> int:
    return int(cb._stage_venda_perdida(s, TENANT))


def _criar_lead(s, *, tel: str, stage: int, owner: int = cb.CORRETORA_CORRETOR) -> int:
    lid = s.execute(
        _t("""INSERT INTO leads (tenant_id, nome, telefone, owner_id, stage_id,
                                 created_at, updated_at)
              VALUES (:t, 'Teste Classe', :tel, :o, :st, now(), now())
           RETURNING id"""),
        {"t": TENANT, "tel": tel, "o": owner, "st": stage},
    ).scalar()
    s.commit()
    return int(lid)


def _disparo(s, lead_id: int, *, ha_segundos: float = 600, variante: str = "direta") -> datetime:
    """Grava o `carrossel_disparo` e devolve a hora dele."""
    at = _agora() - timedelta(seconds=ha_segundos)
    s.execute(
        _t("""INSERT INTO lead_events (tenant_id, lead_id, tipo, payload, at)
              VALUES (:t, :i, :tp, CAST(:p AS jsonb), :at)"""),
        {"t": TENANT, "i": lead_id, "tp": cb.EVENT_DISPARO, "at": at,
         "p": json.dumps({"variante": variante, "categoria": "UTILITY"})},
    )
    s.commit()
    return at


def _disparado(s, sufixo: str, *, ha_segundos: float = 600) -> tuple[str, int, datetime]:
    tel = _PREFIXO + sufixo
    lid = _criar_lead(s, tel=tel, stage=cb.STAGE_DISPARADO)
    return tel, lid, _disparo(s, lid, ha_segundos=ha_segundos)


def _stage(s, lead_id: int) -> int:
    s.expire_all()
    return int(s.execute(_t("SELECT stage_id FROM leads WHERE id = :i"),
                         {"i": lead_id}).scalar())


def _eventos(s, lead_id: int, tipo: str) -> list[dict]:
    return [dict(r[0]) for r in s.execute(
        _t("SELECT payload FROM lead_events WHERE lead_id = :i AND tipo = :tp ORDER BY at, id"),
        {"i": lead_id, "tp": tipo},
    ).all()]


def _responder(tel: str, texto: str, disparo_em: datetime, *, depois: float = 120,
               tipo: str = "text") -> str | None:
    return cb.processar_inbound(
        tel, texto, TENANT, tipo=tipo,
        recebido_em=disparo_em + timedelta(seconds=depois),
    )


@pytest.fixture()
def s():
    """Sessão com limpeza da faixa de telefone destes testes, antes e depois."""
    def _limpar(ses):
        ses.execute(_t("DELETE FROM lead_events WHERE lead_id IN "
                       "(SELECT id FROM leads WHERE telefone LIKE :p)"),
                    {"p": f"{_PREFIXO}%"})
        ses.execute(_t("DELETE FROM disparos_reengajamento WHERE telefone LIKE :p"),
                    {"p": f"{_PREFIXO}%"})
        ses.execute(_t("DELETE FROM cadencia_estado WHERE telefone LIKE :p"),
                    {"p": f"{_PREFIXO}%"})
        ses.execute(_t("DELETE FROM leads WHERE telefone LIKE :p"), {"p": f"{_PREFIXO}%"})
        ses.commit()

    with SessionLocal() as ses:
        # `leads.stage_id` tem FK pra pipeline_stages; banco zerado pode não ter
        # as etapas que este teste usa.
        for i, nome, funil in ((cb.STAGE_DISPARADO, "Disparado", "remarketing"),
                               (cb.STAGE_RESPONDEU, "Respondeu", "remarketing"),
                               (cb.STAGE_FILA_PADRAO, "Fila", "remarketing")):
            ses.execute(_t("INSERT INTO pipeline_stages (id, tenant_id, nome, funil) "
                           "VALUES (:i, :t, :n, :f) ON CONFLICT (id) DO NOTHING"),
                        {"i": i, "t": TENANT, "n": nome, "f": funil})
        ses.execute(_t(
            "INSERT INTO pipeline_stages (tenant_id, nome, funil) "
            "SELECT :t, 'Venda perdida', 'principal' WHERE NOT EXISTS ("
            "  SELECT 1 FROM pipeline_stages WHERE tenant_id = :t "
            "     AND funil = 'principal' AND nome = 'Venda perdida')"),
            {"t": TENANT})
        ses.commit()
        _limpar(ses)
        config_operacional.invalidar_cache()
        yield ses
        _limpar(ses)


# ── 1. Cada classe na sua etapa ─────────────────────────────────────────────

def test_interesse_vai_pra_respondeu(s):
    tel, lid, em = _disparado(s, "01")
    assert _responder(tel, "Quero sim, pode mandar", em) == "respondeu"
    assert _stage(s, lid) == cb.STAGE_RESPONDEU
    assert _eventos(s, lid, cb.EVENT_RESPOSTA)[0]["classe"] == rr.INTERESSE


def test_clique_no_botao_vai_pra_respondeu(s):
    tel, lid, em = _disparado(s, "02")
    assert _responder(tel, "Saiba mais", em, tipo="button", depois=16) == "respondeu"
    assert _stage(s, lid) == cb.STAGE_RESPONDEU
    assert _eventos(s, lid, cb.EVENT_RESPOSTA)[0]["classe"] == rr.BOTAO


def test_indefinida_segue_indo_pra_respondeu(s):
    """"Bom dia", áudio, figurinha: o destino é decisão da Dona. Até ela
    decidir, o card faz o que sempre fez."""
    tel, lid, em = _disparado(s, "03")
    assert _responder(tel, "Bom dia", em) == "respondeu"
    assert _stage(s, lid) == cb.STAGE_RESPONDEU
    assert _eventos(s, lid, cb.EVENT_RESPOSTA)[0]["classe"] == rr.INDEFINIDA

    tel2, lid2, em2 = _disparado(s, "04")
    assert _responder(tel2, "[audio]", em2, tipo="audio") == "respondeu"
    assert _stage(s, lid2) == cb.STAGE_RESPONDEU


def test_nao_isolado_como_resposta_direta_e_recusa(s):
    """Decisão da Dona de 05/10 ("pode ampliar sim"): "Não" como PRIMEIRA
    resposta ao disparo é recusa. Até então ficava em Respondeu."""
    tel, lid, em = _disparado(s, "05")
    assert _responder(tel, "Não", em, depois=10.7) == "recusa"
    assert _stage(s, lid) == _perdida(s)
    ev = _eventos(s, lid, cb.EVENT_RESPOSTA)[0]
    assert (ev["classe"], ev["regra"]) == (rr.RECUSA, rr.REGRA_NEGATIVA_CURTA)


@pytest.mark.parametrize("frase", [
    "Não quero", "Não tenho interesse", "Já comprei, obrigada", "No momento não",
    "Não estou mais procurando",
])
def test_recusa_vai_pra_venda_perdida(s, frase):
    tel, lid, em = _disparado(s, "06")
    assert _responder(tel, frase, em) == "recusa"
    assert _stage(s, lid) == _perdida(s)
    assert _eventos(s, lid, cb.EVENT_RESPOSTA)[0]["classe"] == rr.RECUSA


def test_parar_por_texto_grava_opt_out_e_vai_pra_venda_perdida(s):
    tel, lid, em = _disparado(s, "07")
    assert _responder(tel, "parar", em) == "opt_out"
    assert _stage(s, lid) == _perdida(s)
    assert _eventos(s, lid, cb.EVENT_RESPOSTA)[0]["classe"] == rr.PARAR
    assert s.execute(_t("SELECT opt_out FROM disparos_reengajamento WHERE telefone = :t"),
                     {"t": tel}).scalar() is True


def test_parar_pelo_botao_vale_igual(s):
    tel, lid, em = _disparado(s, "08")
    assert _responder(tel, "Parar mensagens", em, tipo="button") == "opt_out"
    assert _stage(s, lid) == _perdida(s)
    assert s.execute(_t("SELECT count(*) FROM disparos_reengajamento "
                        "WHERE telefone = :t AND opt_out"), {"t": tel}).scalar() == 1


def test_lead_ainda_na_fila_tambem_sai_por_recusa(s):
    tel = _PREFIXO + "09"
    lid = _criar_lead(s, tel=tel, stage=cb.STAGE_FILA_PADRAO)
    assert cb.processar_inbound(tel, "não tenho interesse", TENANT, tipo="text") == "recusa"
    assert _stage(s, lid) == _perdida(s)


# ── 2. Venda perdida é saída definitiva ─────────────────────────────────────

def test_quem_recusou_nao_volta_pra_fila_depois_do_descanso(s):
    """Nem com o disparo e a resposta envelhecidos além dos 30 e dos 45 dias."""
    tel, lid, em = _disparado(s, "10")
    assert _responder(tel, "Já comprei", em) == "recusa"
    s.execute(_t("UPDATE lead_events SET at = at - interval '200 days' WHERE lead_id = :i"),
              {"i": lid})
    s.execute(_t("UPDATE leads SET updated_at = now() - interval '200 days' WHERE id = :i"),
              {"i": lid})
    s.commit()

    cb._voltar_pra_fila(s, TENANT)
    cb._voltar_respondeu_pra_fila(s, TENANT)
    assert _stage(s, lid) == _perdida(s)

    # e a fila não enxerga: nem com a etapa de origem no padrão…
    fila = s.execute(_t(cb._SQL_FILA), {
        "t": TENANT, "dono": cb.CORRETORA_CORRETOR, "fila": cb.STAGE_FILA_PADRAO,
        "pausa_dias": 7, "lim": 500,
    }).mappings().all()
    assert lid not in {int(r["id"]) for r in fila}


def test_venda_perdida_nao_pode_ser_origem_do_disparo(s):
    """…nem apontando o disparo pra lá: a tela recusa e o tick segura."""
    perdida = _perdida(s)
    with pytest.raises(ValueError, match="Venda perdida"):
        remarketing_alvo.conferir_etapa(s, TENANT, perdida)
    etapa = next(e for e in remarketing_alvo.etapas_do_cliente(s, TENANT, cb.CORRETORA_CORRETOR)
                 if e["id"] == perdida)
    assert etapa["permitida"] is False
    assert "Venda perdida" in etapa["motivo_bloqueio"]


def test_tick_segura_se_a_etapa_gravada_for_venda_perdida(s, monkeypatch):
    perdida = _perdida(s)
    valores = {remarketing_alvo.CHAVE_ETAPA: perdida, remarketing_alvo.CHAVE_DONO: None}
    monkeypatch.setattr(config_operacional, "get", lambda k: valores.get(k))
    with pytest.raises(remarketing_alvo.AlvoInvalido):
        remarketing_alvo.ler(s, TENANT)


def test_quem_ja_estava_em_respondeu_e_recusa_depois_sai(s):
    tel, lid, em = _disparado(s, "11")
    assert _responder(tel, "Boa tarde", em, depois=60) == "respondeu"
    assert _responder(tel, "não tenho interesse, obrigada", em, depois=300) == "recusa"
    assert _stage(s, lid) == _perdida(s)
    (ev,) = _eventos(s, lid, cb.EVENT_RESPOSTA)
    assert ev["classe"] == rr.RECUSA and ev["classe_anterior"] == rr.INDEFINIDA


# ── 3. Resposta automática ──────────────────────────────────────────────────

def test_resposta_em_3s_fica_em_disparado_e_nao_carimba(s):
    tel, lid, em = _disparado(s, "12", ha_segundos=30)
    assert _responder(tel, "Olá, seja bem vindo!", em, depois=3) == "automatica"
    assert _stage(s, lid) == cb.STAGE_DISPARADO
    assert _eventos(s, lid, cb.EVENT_RESPOSTA) == []
    assert len(_eventos(s, lid, cb.EVENT_AUTO_RESPOSTA)) == 1


def test_resposta_em_3s_com_conteudo_de_gente_tambem_e_automatica(s):
    tel, lid, em = _disparado(s, "13", ha_segundos=30)
    assert _responder(tel, "Quero sim", em, depois=3) == "automatica"
    assert _stage(s, lid) == cb.STAGE_DISPARADO


def test_automatica_em_40s_e_pega_pelo_conteudo(s):
    tel, lid, em = _disparado(s, "14")
    texto = "Loja Exemplo agradece seu contato. Como podemos ajudar?"
    assert _responder(tel, texto, em, depois=40) == "automatica"
    assert _stage(s, lid) == cb.STAGE_DISPARADO
    assert _eventos(s, lid, cb.EVENT_RESPOSTA) == []


def test_reentrega_da_mesma_automatica_nao_duplica(s):
    tel, lid, em = _disparado(s, "15", ha_segundos=30)
    for _ in range(3):
        assert _responder(tel, "Agradeço seu contato.", em, depois=2) == "automatica"
    assert len(_eventos(s, lid, cb.EVENT_AUTO_RESPOSTA)) == 1


def test_gente_que_fala_depois_da_automatica_e_tratada(s):
    """O robô do aparelho respondeu em 2 s; a pessoa, 10 minutos depois."""
    tel, lid, em = _disparado(s, "16", ha_segundos=900)
    assert _responder(tel, "Loja agradece seu contato.", em, depois=2) == "automatica"
    assert _responder(tel, "Oi, quero ver as opções", em, depois=600) == "respondeu"
    assert _stage(s, lid) == cb.STAGE_RESPONDEU
    assert _eventos(s, lid, cb.EVENT_RESPOSTA)[0]["classe"] == rr.INTERESSE


def test_parar_e_botao_ganham_do_relogio(s):
    tel, lid, em = _disparado(s, "17", ha_segundos=30)
    assert _responder(tel, "PARAR", em, depois=2) == "opt_out"
    assert _stage(s, lid) == _perdida(s)

    tel2, lid2, em2 = _disparado(s, "18", ha_segundos=30)
    assert _responder(tel2, "Saiba mais", em2, tipo="button", depois=2) == "respondeu"
    assert _stage(s, lid2) == cb.STAGE_RESPONDEU


def test_corte_vem_da_configuracao(s, monkeypatch):
    tel, lid, em = _disparado(s, "19", ha_segundos=60)
    monkeypatch.setattr(config_operacional, "carrossel_auto_resposta_segundos", lambda: 12.0)
    assert _responder(tel, "Bom dia", em, depois=10.5) == "automatica"
    assert _stage(s, lid) == cb.STAGE_DISPARADO

    monkeypatch.setattr(config_operacional, "carrossel_auto_resposta_segundos", lambda: 0.0)
    assert _responder(tel, "Bom dia", em, depois=1) == "respondeu"
    assert _stage(s, lid) == cb.STAGE_RESPONDEU


def test_corte_padrao_e_5_e_valor_torto_nao_vira_tudo_robo(monkeypatch):
    assert config_operacional.DEFAULTS["carrossel_auto_resposta_segundos"]() == 5
    for torto in (9999, -1, "abc", None):
        monkeypatch.setattr(config_operacional, "get", lambda _k, v=torto: v)
        assert config_operacional.carrossel_auto_resposta_segundos() == 5.0


def test_validacao_da_chave_do_corte():
    from app.api.automacoes import _validar_config

    assert _validar_config("carrossel_auto_resposta_segundos", 8) == 8
    for ruim in (True, "5", -1, 61, None):
        with pytest.raises(HTTPException):
            _validar_config("carrossel_auto_resposta_segundos", ruim)


def test_texto_automatico_de_quem_nao_e_da_roda_segue_como_antes(s):
    """Site, parceiro, conversa da Pessoa12: None — a Pessoa decide como sempre."""
    tel = _PREFIXO + "20"
    assert cb.processar_inbound(tel, "Loja agradece seu contato.", TENANT, tipo="text") is None


def test_webhook_nao_chama_a_pessoa_pra_resposta_automatica(monkeypatch):
    """A saída "automatica" cala o atendimento, igual ao opt-out."""
    from fastapi.testclient import TestClient

    from app.api import cloud_corretora
    from app.config import settings
    from app.main import app
    from app.services import cloud_lines

    corretor = cloud_lines.CloudLine(
        phone_id="111", slug="corretor", papel="corretora",
        env_file="/etc/agencia/review.env", token_var="META_REVIEW_TOKEN",
    )
    monkeypatch.setattr(cloud_lines, "_LINES", {"111": corretor})
    monkeypatch.setattr(cloud_lines, "_BY_SLUG", {"corretor": "111"})
    monkeypatch.setattr(settings, "cloud_webhook_secret", "s3")
    monkeypatch.setattr(cloud_corretora.corretor, "gravar", lambda **_k: True)

    visto: dict = {}
    atendidos: list[str] = []

    def _falso_inbound(tel, texto, **kw):
        visto.update(kw, texto=texto)
        return visto["devolve"]

    monkeypatch.setattr(cloud_corretora.cb, "processar_inbound", _falso_inbound)
    monkeypatch.setattr(cloud_corretora.atendimento_corretor, "atender",
                        lambda tel, *_a, **_k: atendidos.append(tel))

    def _post(client):
        return client.post(
            "/cloud/corretora/corretor/inbound",
            json={"messages": [{"from": _PREFIXO + "99", "id": "wamid.x", "type": "text",
                                "timestamp": "1756000000", "text": {"body": "oi"}}]},
            headers={"X-Cloud-Secret": "s3"},
        )

    with TestClient(app) as client:
        visto["devolve"] = "automatica"
        assert _post(client).status_code == 200
        assert atendidos == []
        # o tipo e a hora da Meta chegam na regra
        assert visto["tipo"] == "text"
        assert visto["recebido_em"] == datetime.fromtimestamp(1756000000, tz=timezone.utc)

        visto["devolve"] = "opt_out"
        _post(client)
        assert atendidos == []

        for segue in ("respondeu", "recusa", None):
            visto["devolve"] = segue
            _post(client)
        assert len(atendidos) == 3


# ── 4. A taxa só conta interesse e botão ────────────────────────────────────

def test_placar_separa_o_que_conta_do_que_nao_conta(s):
    variante = "teste-classe-placar"
    falas = [
        ("31", "Quero sim", "text", 120),            # interesse
        ("32", "Saiba mais", "button", 20),          # botão
        ("33", "Não tenho interesse", "text", 120),  # recusa
        ("34", "parar", "text", 120),                # parar
        ("35", "Bom dia", "text", 120),              # indefinida
        ("36", "Olá, seja bem vindo!", "text", 2),   # automática
    ]
    for suf, texto, tipo, depois in falas:
        tel = _PREFIXO + suf
        lid = _criar_lead(s, tel=tel, stage=cb.STAGE_DISPARADO)
        em = _disparo(s, lid, variante=variante)
        _responder(tel, texto, em, tipo=tipo, depois=depois)
    # e um que ficou calado
    _disparo(s, _criar_lead(s, tel=_PREFIXO + "37", stage=cb.STAGE_DISPARADO),
             variante=variante)

    (linha,) = [p for p in cb.placar(s, dias=1, tenant_id=TENANT)
                if p["variante"] == variante]
    assert linha["enviados"] == 7
    assert linha["respostas"] == 2            # interesse + botão
    assert linha["botoes"] == 1               # o clique, separado (parte dos 2)
    assert linha["recusas"] == 1
    assert linha["pararam"] == 1
    assert linha["indefinidas"] == 1
    assert linha["automaticas"] == 1
    assert linha["sem_classe"] == 0
    assert linha["taxa"] == round(100 * 2 / 7, 1)


def test_resposta_antiga_sem_classe_segue_contando(s):
    """O que foi medido antes da regra não muda de número sozinho."""
    variante = "teste-classe-legado"
    tel = _PREFIXO + "38"
    lid = _criar_lead(s, tel=tel, stage=cb.STAGE_DISPARADO)
    _disparo(s, lid, variante=variante)
    assert cb.marcar_respondeu(s, tel, TENANT) == 1     # chamada antiga, sem classe
    assert "classe" not in _eventos(s, lid, cb.EVENT_RESPOSTA)[0]

    (linha,) = [p for p in cb.placar(s, dias=1, tenant_id=TENANT)
                if p["variante"] == variante]
    assert linha["respostas"] == 1 and linha["sem_classe"] == 1


def test_bom_dia_e_depois_quero_conta_como_interesse(s):
    tel, lid, em = _disparado(s, "39")
    _responder(tel, "Bom dia", em, depois=60)
    _responder(tel, "Quero sim", em, depois=200)
    (ev,) = _eventos(s, lid, cb.EVENT_RESPOSTA)      # um respondente, não dois
    assert ev["classe"] == rr.INTERESSE
    assert ev["classe_anterior"] == rr.INDEFINIDA
    assert _stage(s, lid) == cb.STAGE_RESPONDEU


# ── 5. Recusa seguida de interesse (caso Renato) ────────────────────────────

def test_recusa_seguida_de_interesse_explicito_reabre(s):
    tel, lid, em = _disparado(s, "40")
    assert _responder(tel, "Já comprei, obrigado", em, depois=120) == "recusa"
    assert _stage(s, lid) == _perdida(s)

    permuta = ("Se você tiver uma chácara em condomínio e aceitar a minha casa "
               "na permuta talvez interessa")
    assert _responder(tel, permuta, em, depois=400) == "respondeu"
    assert _stage(s, lid) == cb.STAGE_RESPONDEU
    (ev,) = _eventos(s, lid, cb.EVENT_RESPOSTA)
    assert ev["classe"] == rr.INTERESSE and ev["classe_anterior"] == rr.RECUSA


@pytest.mark.parametrize("educacao", ["Ok", "Tá bom", "Claro, pode deixar!", "Obrigada"])
def test_educacao_depois_da_recusa_nao_reabre(s, educacao):
    """17 de 20 "interesses" depois de recusa, nas conversas reais, eram isto:
    a pessoa respondendo com educação ao pedido de indicação."""
    tel, lid, em = _disparado(s, "41")
    _responder(tel, "No momento não, obrigada", em, depois=120)
    _responder(tel, educacao, em, depois=300)
    assert _stage(s, lid) == _perdida(s)
    assert _eventos(s, lid, cb.EVENT_RESPOSTA)[0]["classe"] == rr.RECUSA


def test_quem_pediu_pra_parar_nao_e_reaberto(s):
    tel, lid, em = _disparado(s, "42")
    assert _responder(tel, "parar", em, depois=120) == "opt_out"
    _responder(tel, "quero ver apartamento no centro", em, depois=400)
    assert _stage(s, lid) == _perdida(s)
    assert _eventos(s, lid, cb.EVENT_RESPOSTA)[0]["classe"] == rr.PARAR


def test_venda_perdida_de_outra_origem_nao_e_reaberta(s):
    """Card que a corretora (ou outra época) pôs em Venda perdida não volta por
    causa de um "quero" na linha da Corretor: só reabre o que ESTA regra fechou."""
    tel = _PREFIXO + "43"
    lid = _criar_lead(s, tel=tel, stage=_perdida(s))
    em = _disparo(s, lid, ha_segundos=86400 * 40)
    assert _responder(tel, "quero ver apartamento no centro", em, depois=500) is None
    assert _stage(s, lid) == _perdida(s)


def test_interesse_e_depois_recusa_vai_pra_venda_perdida(s):
    tel, lid, em = _disparado(s, "44")
    assert _responder(tel, "Estou procurando casa para aluguel", em, depois=60) == "respondeu"
    assert _responder(tel, "Não vou comprar agora", em, depois=70) == "recusa"
    assert _stage(s, lid) == _perdida(s)
    assert _eventos(s, lid, cb.EVENT_RESPOSTA)[0]["classe"] == rr.RECUSA


# ── 6. Card de corretora não é de robô ──────────────────────────────────────

def test_lead_ja_transferido_nao_e_tocado(s):
    tel = _PREFIXO + "45"
    lid = _criar_lead(s, tel=tel, stage=cb.STAGE_RESPONDEU, owner=CORRETOR)
    em = _disparo(s, lid)
    assert _responder(tel, "não tenho interesse", em) is None
    assert _stage(s, lid) == cb.STAGE_RESPONDEU


def test_parar_de_lead_transferido_ainda_grava_opt_out(s):
    tel = _PREFIXO + "46"
    lid = _criar_lead(s, tel=tel, stage=cb.STAGE_RESPONDEU, owner=CORRETOR)
    em = _disparo(s, lid)
    assert _responder(tel, "parar", em) == "opt_out"
    assert _stage(s, lid) == cb.STAGE_RESPONDEU     # o card é da corretora
    assert s.execute(_t("SELECT count(*) FROM disparos_reengajamento "
                        "WHERE telefone = :t AND opt_out"), {"t": tel}).scalar() == 1


def test_banco_fora_ainda_devolve_opt_out(monkeypatch):
    """Regra antiga, mantida: se o banco cair, quem mandou PARAR não é atendido."""
    from app.db import session as db_session

    def _quebra():
        raise RuntimeError("banco fora")

    monkeypatch.setattr(db_session, "SessionLocal", _quebra)
    assert cb.processar_inbound(_PREFIXO + "47", "parar", TENANT) == "opt_out"
    assert cb.processar_inbound(_PREFIXO + "47", "bom dia", TENANT) is None
