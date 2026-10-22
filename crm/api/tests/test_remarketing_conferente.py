"""Conferente do remarketing — a conta do dia, os alarmes, a reserva órfã e o
status de entrega que não pode sumir (05/10/2026).

Banco de verdade (o de teste). Nada sai da máquina: o "Telegram" é uma lista.

O que estes testes seguram:

1. a conta do dia separa ACEITO de ENTREGUE, LIDO e FALHA, e diz as respostas
   por tipo — nunca soma aceite como entrega;
2. os alarmes disparam nas situações que ficaram invisíveis até hoje: robô
   ligado o dia inteiro sem enviar, robô que não rodou, aceitos sem nenhum
   status da Meta (os 519 de setembro), entrega ruim;
3. reserva órfã com PROVA de envio vira disparo; sem prova fica, marcada — e
   nunca volta pra fila;
4. status de entrega que chega antes da mensagem é guardado e reaplicado;
   status atrasado não rebaixa o que já chegou;
5. o aviso no Telegram nasce DESLIGADO e, ligado, sai uma vez por dia.
"""
from __future__ import annotations

import json
import uuid
from datetime import date, datetime, time, timedelta, timezone

import pytest
from sqlalchemy import text as _t

from app.db.session import SessionLocal
from app.services import carrossel_corretor as cb
from app.services import config_operacional as cfg
from app.services import remarketing_conferente as conf
from app.services import status_entrega

TENANT = 77                      # tenant só destes testes: a conta do dia é por tenant
_PREFIXO = "5500000000000"
ONTEM = conf._hoje_sp() - timedelta(days=1)
ANTEONTEM = conf._hoje_sp() - timedelta(days=2)


def _meio_dia(dia: date, minutos: int = 0) -> datetime:
    return datetime.combine(dia, time(12, 0), tzinfo=conf.TZ_SP) + timedelta(minutes=minutos)


@pytest.fixture()
def s():
    def _limpar(ses):
        for sql in (
            "DELETE FROM lead_events WHERE tenant_id = :t",
            "DELETE FROM remarketing_tick_log WHERE tenant_id = :t",
            "DELETE FROM remarketing_conferencia WHERE tenant_id = :t",
            "DELETE FROM leads WHERE tenant_id = :t",
        ):
            ses.execute(_t(sql), {"t": TENANT})
        ses.execute(_t("DELETE FROM mensagens_whatsapp WHERE contato_telefone LIKE :p"),
                    {"p": f"{_PREFIXO}%"})
        ses.execute(_t("DELETE FROM whatsapp_status_orfao WHERE wamid LIKE 'wamid.conf.%'"))
        ses.execute(_t("DELETE FROM config_operacional WHERE chave IN "
                       "('remarketing_conferente_avisa_telegram', 'remarketing_dias_semana')"))
        ses.commit()
        cfg.invalidar_cache()

    with SessionLocal() as ses:
        ses.execute(_t("INSERT INTO pipeline_stages (id, tenant_id, nome, funil) VALUES "
                       "(:i, 1, 'Disparado', 'remarketing') ON CONFLICT (id) DO NOTHING"),
                    {"i": cb.STAGE_DISPARADO})
        ses.commit()
        _limpar(ses)
        yield ses
        ses.rollback()
        _limpar(ses)


def _lead(s, sufixo: str) -> int:
    lid = s.execute(
        _t("INSERT INTO leads (tenant_id, nome, telefone, owner_id, stage_id, created_at, updated_at) "
           "VALUES (:t, 'Teste Conferente', :tel, :o, :st, now(), now()) RETURNING id"),
        {"t": TENANT, "tel": f"{_PREFIXO}{sufixo}", "o": cb.CORRETORA_CORRETOR,
         "st": cb.STAGE_DISPARADO},
    ).scalar()
    s.commit()
    return int(lid)


def _evento(s, lead_id: int, tipo: str, quando: datetime, **payload) -> None:
    s.execute(_t("INSERT INTO lead_events (tenant_id, lead_id, tipo, payload, at) "
                 "VALUES (:t, :i, :tp, CAST(:p AS jsonb), :at)"),
              {"t": TENANT, "i": lead_id, "tp": tipo, "at": quando,
               "p": json.dumps(payload)})
    s.commit()


def _mensagem(s, sufixo: str, wamid: str, quando: datetime, *, status: str | None = None,
              codigo: str | None = None, template: str = "acme_corretor_ola_a_01") -> None:
    meta: dict = {"template": template, "categoria": "UTILITY"}
    if status:
        meta["delivery_status"] = status
    if codigo:
        meta.update(delivery_error_code=codigo, delivery_error="Message undeliverable")
    tel = f"{_PREFIXO}{sufixo}"
    s.execute(
        _t("""INSERT INTO mensagens_whatsapp
                (id, evolution_message_id, instance, corretora_id, remote_jid,
                 contato_telefone, from_corretora, tipo_mensagem, conteudo_texto,
                 conteudo_metadata, timestamp_evolution, recebido_em, tenant_id)
              VALUES (:id, :w, :inst, NULL, :jid, :tel, TRUE, 'template', 'oi',
                      CAST(:meta AS jsonb), :ts, now(), 1)"""),
        {"id": str(uuid.uuid4()), "w": wamid, "inst": cb.INSTANCE_CORRETOR,
         "jid": f"{tel}@s.whatsapp.net", "tel": tel, "meta": json.dumps(meta), "ts": quando},
    )
    s.commit()


def _voltas(s, dia: date, n: int, *, ligado: bool | None = True, motivo: str | None = None,
            cap: int = 80, enviados: int = 0) -> None:
    for i in range(n):
        s.execute(_t("INSERT INTO remarketing_tick_log (tenant_id, em, ligado, motivo_parada, "
                     "cap_do_dia, enviados) VALUES (:t, :em, :l, :m, :c, :e)"),
                  {"t": TENANT, "em": _meio_dia(dia, i * 3 - 180), "l": ligado, "m": motivo,
                   "c": cap, "e": enviados if i == 0 else 0})
    s.commit()


def _disparo(s, sufixo: str, dia: date, status: str | None, *, codigo: str | None = None,
             minuto: int = 0, wamid: str | None = "auto") -> int:
    lid = _lead(s, sufixo)
    if wamid == "auto":
        wamid = f"wamid.conf.{sufixo}"
    quando = _meio_dia(dia, minuto)
    _evento(s, lid, cb.EVENT_DISPARO, quando, variante="direta", categoria="UTILITY",
            wamid=wamid)
    if wamid:
        _mensagem(s, sufixo, wamid, quando, status=status, codigo=codigo)
    return lid


# ── 1. A conta do dia ────────────────────────────────────────────────────────


def test_conta_do_dia_separa_aceito_de_entregue_lido_e_falha(s):
    _voltas(s, ONTEM, 150, cap=80, enviados=6)
    _disparo(s, "01", ONTEM, "read")
    _disparo(s, "02", ONTEM, "delivered")
    _disparo(s, "03", ONTEM, "sent")                    # aceito, sem confirmação
    _disparo(s, "04", ONTEM, None)                      # aceito, nenhum status
    _disparo(s, "05", ONTEM, "failed", codigo="131026")
    _disparo(s, "06", ONTEM, None, wamid=None)          # sem id da Meta
    _disparo(s, "07", ANTEONTEM, "read")                # outro dia: não entra

    d = conf.conta_do_dia(s, ONTEM, TENANT)

    assert d["previstos"] == 80
    assert d["aceitos"] == 6
    assert (d["entregues"], d["lidos"], d["falhas"]) == (2, 1, 1)
    assert (d["em_voo"], d["sem_rastro"]) == (2, 1)
    assert d["entregues"] + d["em_voo"] + d["falhas"] + d["sem_rastro"] == d["aceitos"]
    # a taxa é sobre o que tem rastro (5), não sobre os 6
    assert (d["pct_entregue"], d["pct_falha"]) == (40.0, 20.0)
    assert d["motivos_de_falha"] == [{"codigo": "131026", "titulo": "Message undeliverable",
                                      "n": 1, "culpa": "lista"}]
    assert d["parcial"] is False


def test_respostas_por_tipo_sao_as_do_disparo_daquele_dia(s):
    _voltas(s, ONTEM, 150)
    em = _meio_dia(ONTEM).isoformat()
    for i, classe in enumerate(["interesse", "interesse", "botao", "recusa", "parar",
                                "indefinida", "numero_errado", None]):
        lid = _disparo(s, f"1{i}", ONTEM, "delivered", minuto=i)
        extra = {"classe": classe} if classe else {}
        _evento(s, lid, cb.EVENT_RESPOSTA, datetime.now(timezone.utc),
                variante="direta", disparo_em=em, **extra)
    lid = _disparo(s, "19", ONTEM, "delivered", minuto=9)
    _evento(s, lid, cb.EVENT_AUTO_RESPOSTA, datetime.now(timezone.utc), disparo_em=em)

    r = conf.conta_do_dia(s, ONTEM, TENANT)["respostas"]

    assert r == {"interesse": 2, "botao": 1, "recusa": 1, "parar": 1, "indefinida": 1,
                 "sem_classe": 1, "automatica": 1, "numero_errado": 1}
    assert conf.conta_do_dia(s, ONTEM, TENANT)["respostas_que_contam"] == 3


def test_dia_de_hoje_e_parcial(s):
    assert conf.conta_do_dia(s, conf._hoje_sp(), TENANT)["parcial"] is True


# ── 2. Alarmes ───────────────────────────────────────────────────────────────


def _alarmes(s, dia: date = ONTEM) -> list[str]:
    return [a["codigo"] for a in conf.conferir_dia(s, dia, TENANT)["alarmes"]]


def test_ligado_o_dia_inteiro_sem_enviar_e_alarme(s):
    """O furo 9 do DISPARO-POR-ETAPA: o timer roda, sai "done", e ninguém vê
    que não saiu nada."""
    _voltas(s, ANTEONTEM, 1)                             # o registro já existia antes
    _voltas(s, ONTEM, 200, ligado=True, motivo="sem_linha_disparadora")
    c = conf.conferir_dia(s, ONTEM, TENANT)
    assert [a["codigo"] for a in c["alarmes"]] == ["ligado_sem_envio"]
    assert "sem_linha_disparadora" in c["alarmes"][0]["detalhe"]


def test_desligado_o_dia_inteiro_nao_e_alarme(s):
    _voltas(s, ANTEONTEM, 1)
    _voltas(s, ONTEM, 200, ligado=False, motivo="desligado")
    assert _alarmes(s) == []


def test_ligado_fora_do_dia_de_disparo_nao_e_alarme(s):
    _voltas(s, ANTEONTEM, 1)
    _voltas(s, ONTEM, 200, ligado=True, motivo="fora_dos_dias")
    cfg.definir("remarketing_dias_semana", [(ONTEM.weekday() + 1) % 7], autor="teste")
    assert _alarmes(s) == []


def test_robo_que_nao_rodou_e_alarme(s):
    _voltas(s, ANTEONTEM, 5)                             # rodava; ontem, nada
    assert _alarmes(s) == ["robo_nao_rodou"]


def test_dia_anterior_ao_registro_de_voltas_nao_e_alarme(s):
    """No dia em que a tabela nasce, os dias anteriores não são "não rodou"."""
    assert _alarmes(s) == []


def test_aceitos_sem_nenhum_status_da_meta_e_alarme(s):
    """Os 519 de 15 a 19/09: aceitos, nenhum `delivered`, nenhum `failed`."""
    _voltas(s, ONTEM, 150, enviados=6)
    for i in range(6):
        _disparo(s, f"2{i}", ONTEM, None, minuto=i)
    assert _alarmes(s) == ["sem_status_da_meta"]


def test_entrega_ruim_e_alarme_e_diz_de_quem_e_a_culpa(s):
    _voltas(s, ONTEM, 150, enviados=10)
    for i in range(7):
        _disparo(s, f"3{i}", ONTEM, "delivered", minuto=i)
    for i in range(3):
        _disparo(s, f"4{i}", ONTEM, "failed", codigo="131026", minuto=10 + i)
    c = conf.conferir_dia(s, ONTEM, TENANT)
    assert [a["codigo"] for a in c["alarmes"]] == ["entrega_ruim"]
    assert "30.0%" in c["alarmes"][0]["detalhe"] and "lista" in c["alarmes"][0]["detalhe"]


def test_dia_bom_nao_tem_alarme(s):
    _voltas(s, ONTEM, 150, enviados=10)
    for i in range(10):
        _disparo(s, f"5{i}", ONTEM, "delivered" if i % 2 else "read", minuto=i)
    assert _alarmes(s) == []


def test_conferir_grava_uma_linha_por_dia_e_reconferir_atualiza(s):
    _voltas(s, ONTEM, 150, enviados=1)
    _disparo(s, "60", ONTEM, "sent")
    conf.conferir_dia(s, ONTEM, TENANT)
    s.execute(_t("UPDATE mensagens_whatsapp SET conteudo_metadata = conteudo_metadata || "
                 "'{\"delivery_status\": \"read\"}'::jsonb WHERE evolution_message_id = 'wamid.conf.60'"))
    s.commit()
    conf.conferir_dia(s, ONTEM, TENANT)                  # o status chegou depois

    gravadas = conf.ultimas(s, TENANT, 7)
    assert len(gravadas) == 1
    assert (gravadas[0]["aceitos"], gravadas[0]["entregues"], gravadas[0]["lidos"]) == (1, 1, 1)


# ── 3. Reserva órfã ──────────────────────────────────────────────────────────


def _reserva(s, sufixo: str, minutos_atras: int = 60, **extra) -> tuple[int, datetime]:
    lid = _lead(s, sufixo)
    quando = datetime.now(timezone.utc) - timedelta(minutes=minutos_atras)
    _evento(s, lid, cb.EVENT_RESERVA, quando, origem="carrossel", linha="corretor",
            etapa_origem=23, dono=4, **extra)
    return lid, quando


def _tipos(s, lead_id: int) -> list[tuple[str, dict]]:
    return [(r[0], dict(r[1])) for r in s.execute(
        _t("SELECT tipo, payload FROM lead_events WHERE lead_id = :i ORDER BY at, id"),
        {"i": lead_id}).all()]


def test_reserva_com_wamid_guardado_vira_disparo(s):
    """O registro do disparo falhou depois do envio; o wamid ficou na reserva."""
    lid, _ = _reserva(s, "70", estado="sem_registro", wamid="wamid.conf.70",
                      template="acme_corretor_ola_a_01", categoria="UTILITY", variante="curta")
    assert conf.conciliar_reservas(s, TENANT) == {"conciliadas": 1, "sem_prova": 0}
    [(tipo, p)] = _tipos(s, lid)
    assert tipo == cb.EVENT_DISPARO
    assert (p["wamid"], p["variante"], p["conciliado"]) == (
        "wamid.conf.70", "curta", "wamid_na_reserva")
    assert p["etapa_origem"] == 23, "a origem do lead (pra onde ele volta) se perdeu"


def test_reserva_com_a_mensagem_gravada_no_thread_vira_disparo(s):
    """Timeout: não sei se saiu. Mas a mensagem ESTÁ gravada no thread daquele
    telefone, naqueles minutos — saiu."""
    lid, quando = _reserva(s, "71", estado="incerto", motivo="ReadTimeout")
    _mensagem(s, "71", "wamid.conf.71", quando + timedelta(seconds=4), status="delivered")
    assert conf.conciliar_reservas(s, TENANT)["conciliadas"] == 1
    [(tipo, p)] = _tipos(s, lid)
    assert tipo == cb.EVENT_DISPARO
    assert (p["wamid"], p["conciliado"], p["estado_da_reserva"]) == (
        "wamid.conf.71", "mensagem_gravada", "incerto")
    assert p["variante_recalculada"] is True


def test_mensagem_de_outro_dia_nao_prova_nada(s):
    lid, quando = _reserva(s, "72", estado="incerto")
    _mensagem(s, "72", "wamid.conf.72", quando - timedelta(days=3), status="delivered")
    assert conf.conciliar_reservas(s, TENANT) == {"conciliadas": 0, "sem_prova": 1}
    assert _tipos(s, lid)[0][0] == cb.EVENT_RESERVA


def test_reserva_sem_prova_fica_marcada_e_aparece_na_tela(s):
    lid, _ = _reserva(s, "73")
    assert conf.conciliar_reservas(s, TENANT) == {"conciliadas": 0, "sem_prova": 1}
    [(tipo, p)] = _tipos(s, lid)
    assert tipo == cb.EVENT_RESERVA and p["sem_prova_de_envio"] is True
    # nunca volta pra fila sozinha: sem prova de que NÃO saiu, reenviar é arriscar
    assert s.execute(_t("SELECT stage_id FROM leads WHERE id = :i"),
                     {"i": lid}).scalar() == cb.STAGE_DISPARADO

    tela = conf.reservas_orfas(s, TENANT)
    assert tela["total"] == 1
    assert tela["itens"][0]["lead_id"] == lid
    assert tela["por_estado"] == {"sem_prova": 1}


def test_reserva_recente_nao_e_tocada(s):
    """Menos de 10 minutos: pode ser um envio acontecendo agora."""
    lid, _ = _reserva(s, "74", minutos_atras=2)
    assert conf.conciliar_reservas(s, TENANT) == {"conciliadas": 0, "sem_prova": 0}
    assert _tipos(s, lid)[0][1].get("sem_prova_de_envio") is None


# ── 4. Status de entrega ─────────────────────────────────────────────────────


def _status_gravado(s, wamid: str) -> str | None:
    s.expire_all()
    return s.execute(_t("SELECT conteudo_metadata->>'delivery_status' FROM mensagens_whatsapp "
                        "WHERE evolution_message_id = :w"), {"w": wamid}).scalar()


def test_status_que_chega_antes_da_mensagem_e_guardado_e_reaplicado(s):
    """Antes: UPDATE em zero linhas, webhook respondia ok, status sumia."""
    patch = {"delivery_status": "delivered", "delivery_ts": "2026-10-05T12:00:00+00:00"}
    assert status_entrega.aplicar(s, "wamid.conf.80", patch, "corretor") == status_entrega.ORFAO
    s.commit()
    assert status_entrega.reaplicar_orfaos(s)["pendentes"] >= 1

    _mensagem(s, "80", "wamid.conf.80", datetime.now(timezone.utc))     # a mensagem aparece
    r = status_entrega.reaplicar_orfaos(s)
    assert r["reaplicados"] == 1
    assert _status_gravado(s, "wamid.conf.80") == "delivered"
    assert status_entrega.reaplicar_orfaos(s)["reaplicados"] == 0       # não repete


def test_status_atrasado_nao_rebaixa_o_que_ja_chegou(s):
    _mensagem(s, "81", "wamid.conf.81", datetime.now(timezone.utc), status="read")
    r = status_entrega.aplicar(s, "wamid.conf.81", {"delivery_status": "sent"}, "corretor")
    s.commit()
    assert r == status_entrega.FORA_DE_ORDEM
    assert _status_gravado(s, "wamid.conf.81") == "read"


def test_status_na_ordem_avanca(s):
    _mensagem(s, "82", "wamid.conf.82", datetime.now(timezone.utc), status="sent")
    for st in ("delivered", "read"):
        assert status_entrega.aplicar(s, "wamid.conf.82", {"delivery_status": st},
                                      "corretor") == status_entrega.APLICADO
        s.commit()
    assert _status_gravado(s, "wamid.conf.82") == "read"


def test_webhook_de_status_diz_o_que_fez_com_cada_um(s, monkeypatch):
    """A rota respondia `{"ok": true, "vistos": N}` mesmo sem gravar nada."""
    from fastapi.testclient import TestClient

    from app.api import cloud_corretora
    from app.main import app

    monkeypatch.setattr(cloud_corretora.corretor, "_auth_cloud_webhook", lambda segredo: None)
    monkeypatch.setattr(cloud_corretora, "_resolver", lambda slug: ("acme_corretor", 4))
    _mensagem(s, "83", "wamid.conf.83", datetime.now(timezone.utc), status="read")

    r = TestClient(app).post("/cloud/corretora/corretor/status", json={"statuses": [
        {"id": "wamid.conf.83", "status": "sent", "timestamp": "1790000000"},      # atrasado
        {"id": "wamid.conf.84", "status": "delivered", "timestamp": "1790000000"},  # sem mensagem
    ]})
    assert r.status_code == 200, r.text
    corpo = r.json()
    assert (corpo["vistos"], corpo["fora_de_ordem"], corpo["orfao"]) == (2, 1, 1)


# ── 5. Aviso no Telegram: pronto e desligado ─────────────────────────────────


def _dia_com_disparo(s) -> dict:
    _voltas(s, ONTEM, 150, enviados=2)
    _disparo(s, "90", ONTEM, "delivered")
    _disparo(s, "91", ONTEM, "read", minuto=1)
    return conf.conferir_dia(s, ONTEM, TENANT)


def test_aviso_nasce_desligado_e_nao_envia(s):
    c = _dia_com_disparo(s)
    mandados: list[str] = []
    assert cfg.get("remarketing_conferente_avisa_telegram") is False
    assert conf.avisar(s, c, TENANT, enviar=mandados.append) == "desligado"
    assert mandados == []


def test_aviso_ligado_sai_uma_vez_por_dia(s):
    c = _dia_com_disparo(s)
    cfg.definir("remarketing_conferente_avisa_telegram", True, autor="teste")
    mandados: list[str] = []
    assert conf.avisar(s, c, TENANT, enviar=mandados.append) == "enviado"
    assert conf.avisar(s, c, TENANT, enviar=mandados.append) == "ja_avisado"
    assert len(mandados) == 1
    texto = mandados[0]
    assert "aceitos 2" in texto and "entregues 2" in texto and "lidos 1" in texto
    assert "Nenhum alarme." in texto


def test_aviso_nao_sai_pra_dia_parcial_nem_pra_dia_sem_nada(s):
    cfg.definir("remarketing_conferente_avisa_telegram", True, autor="teste")
    mandados: list[str] = []
    hoje = conf.conferir_dia(s, conf._hoje_sp(), TENANT)
    assert conf.avisar(s, hoje, TENANT, enviar=mandados.append) == "dia_parcial"
    vazio = conf.conferir_dia(s, ONTEM, TENANT)
    assert conf.avisar(s, vazio, TENANT, enviar=mandados.append) == "sem_o_que_dizer"
    assert mandados == []


def test_telegram_que_devolve_erro_em_texto_nao_vira_aviso_enviado(s):
    """O `TelegramTool.send_to_dona` não levanta: devolve "error: …". Antes isso
    gravava a marca de enviado e o dia nunca mais era avisado."""
    c = _dia_com_disparo(s)
    cfg.definir("remarketing_conferente_avisa_telegram", True, autor="teste")
    assert conf.avisar(s, c, TENANT, enviar=lambda texto: "error: 502 Bad Gateway") == "falhou"
    assert conf.ultimas(s, TENANT, 7)[0]["aviso_enviado_em"] is None
    # a rodada seguinte tenta de novo, e aí sai
    mandados: list[str] = []
    assert conf.avisar(s, c, TENANT, enviar=lambda x: mandados.append(x) or "ok") == "enviado"
    assert len(mandados) == 1 and conf.ultimas(s, TENANT, 7)[0]["aviso_enviado_em"]


def test_telegram_em_simulacao_nao_marca_o_aviso_como_enviado(s):
    c = _dia_com_disparo(s)
    cfg.definir("remarketing_conferente_avisa_telegram", True, autor="teste")
    assert conf.avisar(s, c, TENANT, enviar=lambda texto: "ok (dry_run)") == "simulado"
    assert conf.ultimas(s, TENANT, 7)[0]["aviso_enviado_em"] is None


def test_o_caminho_de_producao_do_aviso_passa_pelo_retorno_do_telegram(s, monkeypatch):
    """Sem `enviar` explícito o conferente usa o `TelegramTool` de verdade, que
    aqui cai (sem token, sem rede) e devolve erro em texto."""
    from app.config import settings
    from app.tools import telegram

    c = _dia_com_disparo(s)
    cfg.definir("remarketing_conferente_avisa_telegram", True, autor="teste")
    monkeypatch.setattr(settings, "dry_run", False)
    monkeypatch.setattr(telegram, "_record_briefing", lambda **k: None)

    def _cai(self, mensagem):
        raise RuntimeError("telegram fora do ar")

    monkeypatch.setattr(telegram.TelegramTool, "_post_with_retry", _cai)
    assert conf.avisar(s, c, TENANT) == "falhou"
    assert conf.ultimas(s, TENANT, 7)[0]["aviso_enviado_em"] is None


def test_aviso_que_falha_nao_marca_como_enviado(s):
    c = _dia_com_disparo(s)
    cfg.definir("remarketing_conferente_avisa_telegram", True, autor="teste")

    def _cai(texto):
        raise RuntimeError("telegram fora")

    assert conf.avisar(s, c, TENANT, enviar=_cai) == "falhou"
    mandados: list[str] = []
    assert conf.avisar(s, c, TENANT, enviar=mandados.append) == "enviado"


def test_aviso_leva_os_alarmes(s):
    _voltas(s, ANTEONTEM, 1)
    _voltas(s, ONTEM, 200, ligado=True, motivo="sem_linha_disparadora")
    c = conf.conferir_dia(s, ONTEM, TENANT)
    texto = conf.montar_aviso(c)
    assert "ATENÇÃO: o disparo ficou ligado o dia inteiro e não mandou nenhuma mensagem" in texto


# ── 6. A rodada do timer ─────────────────────────────────────────────────────


def test_rodada_confere_hoje_ontem_e_anteontem_e_nunca_levanta(s, monkeypatch):
    _voltas(s, ONTEM, 150, enviados=1)
    _disparo(s, "95", ONTEM, "delivered")
    mandados: list[str] = []
    res = conf.rodar(tenant_id=TENANT, enviar=mandados.append)
    assert res["erro"] is None
    assert [d["dia"] for d in res["dias"]] == [
        ANTEONTEM.isoformat(), ONTEM.isoformat(), conf._hoje_sp().isoformat()]
    assert {d["aviso"] for d in res["dias"]} == {"desligado"}
    assert mandados == []

    monkeypatch.setattr(conf, "conciliar_reservas",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("banco fora")))
    assert "banco fora" in conf.rodar(tenant_id=TENANT)["erro"]
