"""Placar de entrega do remarketing — o disparo passa a saber o que CHEGOU.

O buraco (04/10/2026): o webhook já gravava o status de entrega em
`mensagens_whatsapp` por wamid, mas o registro do disparo não guardava o wamid.
O placar contava "mandado" e parava. Na Fulano isso escondeu 40,5% de falha.

O que estes testes protegem, em ordem:
1. O envio GRAVA o wamid (carrossel no evento, painel na coluna nova).
2. O placar cruza por wamid — e só por ele.
3. `accepted` e `sent` não contam como entregue. Nunca.
4. Lote antigo, sem wamid, não quebra a leitura nem vira falha/entrega.
5. `131026` é culpa da lista.
6. Código desconhecido NÃO é chutado como lista.

Nenhum teste aqui envia nada: sender é fake, status é linha inserida à mão.
"""
from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text as _t

from app.api.cloud_corretora_a import build_status_patch
from app.db.session import SessionLocal
from app.services import carrossel_corretor as cb
from app.services import remarketing_placar as rp

TENANT = 1
_PREFIXO = "5500000000000"     # faixa só destes testes
_SP = ZoneInfo("America/Sao_Paulo")

# Dias fixos e antigos: o placar é por DIA de São Paulo, e usar "hoje" faria o
# teste depender de outros testes que disparam no mesmo dia (e da virada das
# 21h UTC). Meio-dia de SP fica longe das duas bordas.
DIA = date(2026, 3, 10)
OUTRO_DIA = date(2026, 3, 11)


def _meio_dia(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, 12, 0, tzinfo=_SP).astimezone(timezone.utc)


def _lead(s, n: int) -> int:
    lid = s.execute(
        _t("""INSERT INTO leads (tenant_id, nome, telefone, owner_id, stage_id,
                                 created_at, updated_at)
              VALUES (:t, 'Teste Placar', :tel, :o, :st, now(), now())
           RETURNING id"""),
        {"t": TENANT, "tel": f"{_PREFIXO}{n:03d}", "o": cb.CORRETORA_CORRETOR,
         "st": cb.STAGE_FILA_PADRAO},
    ).scalar()
    s.commit()
    return int(lid)


def _disparo(s, n: int, *, wamid: str | None, dia: date = DIA) -> None:
    """Um evento de disparo do carrossel, como `_disparar` grava."""
    payload = {"linha": "corretor", "origem": "carrossel", "variante": "x"}
    if wamid is not None:
        payload["wamid"] = wamid
    s.execute(
        _t("""INSERT INTO lead_events (tenant_id, lead_id, tipo, payload, at)
              VALUES (:t, :i, :tp, CAST(:p AS jsonb), :at)"""),
        {"t": TENANT, "i": _lead(s, n), "tp": cb.EVENT_DISPARO,
         "p": json.dumps(payload), "at": _meio_dia(dia)},
    )
    s.commit()


def _mensagem(s, n: int, wamid: str, *, status: str | None,
              errors: list[dict] | None = None, instance: str = "acme_corretor") -> None:
    """A mensagem na thread, com o status que o webhook teria gravado — montado
    pelo MESMO `build_status_patch` de produção, pra o teste quebrar se o
    formato do patch mudar."""
    meta: dict = {"origem": "carrossel"}
    if status is not None:
        meta.update(build_status_patch(status, None, errors))
    tel = f"{_PREFIXO}{n:03d}"
    s.execute(
        _t("""INSERT INTO mensagens_whatsapp
                (id, evolution_message_id, instance, corretora_id, remote_jid,
                 contato_telefone, from_corretora, tipo_mensagem, conteudo_texto,
                 conteudo_metadata, timestamp_evolution, recebido_em, tenant_id)
              VALUES (:id, :w, :inst, NULL, :jid, :tel, TRUE, 'template', 'oi',
                      CAST(:meta AS jsonb), :ts, now(), :t)"""),
        {"id": str(uuid.uuid4()), "w": wamid, "inst": instance,
         "jid": f"{tel}@s.whatsapp.net", "tel": tel, "meta": json.dumps(meta),
         "ts": _meio_dia(DIA), "t": TENANT},
    )
    s.commit()


def _w() -> str:
    return f"wamid.TESTE{uuid.uuid4().hex}"


@pytest.fixture()
def s():
    def _limpar(ses):
        ses.execute(_t("DELETE FROM lead_events WHERE lead_id IN "
                       "(SELECT id FROM leads WHERE telefone LIKE :p)"),
                    {"p": f"{_PREFIXO}%"})
        ses.execute(_t("DELETE FROM mensagens_whatsapp WHERE contato_telefone LIKE :p"),
                    {"p": f"{_PREFIXO}%"})
        ses.execute(_t("DELETE FROM disparos_reengajamento WHERE telefone LIKE :p"),
                    {"p": f"{_PREFIXO}%"})
        ses.execute(_t("DELETE FROM leads WHERE telefone LIKE :p"), {"p": f"{_PREFIXO}%"})
        ses.commit()

    with SessionLocal() as ses:
        # `leads.stage_id` tem FK pra pipeline_stages e o seed do conftest não
        # garante a Fila (23) quando este arquivo roda sozinho.
        ses.execute(_t("INSERT INTO pipeline_stages (id, tenant_id, nome, funil) "
                       "VALUES (:i, :t, 'Fila', 'remarketing') "
                       "ON CONFLICT (id) DO NOTHING"),
                    {"i": cb.STAGE_FILA_PADRAO, "t": TENANT})
        ses.commit()
        _limpar(ses)
        yield ses
        _limpar(ses)


def _so_carrossel(s, de: date = DIA, ate: date = DIA) -> dict:
    return rp.placar(s, de, ate, tenant_id=TENANT, fontes=("carrossel",))


# ── 1. O envio grava o wamid ────────────────────────────────────────────────

class _SessionGravadora:
    """Guarda os parâmetros de cada execute — é o que `_disparar` mandou gravar."""

    def __init__(self):
        self.params: list[dict] = []

    def execute(self, _sql, params=None):
        self.params.append(params or {})
        return self


def test_disparo_do_carrossel_grava_o_wamid_que_o_sender_devolveu(monkeypatch):
    """O campo é `wamid`, no topo do retorno de `enviar_template_linha`."""
    from app.services import cloud_lines as cl

    linha = cl.CloudLine(phone_id="1", slug="corretor", papel="corretora",
                         env_file="", token_var="")

    def sender(telefone, texto, *, com_botao, **kw):
        return {"ok": True, "dry_run": False, "sent": True, "wamid": "wamid.ABC",
                "template": "acme_ola_a_01", "categoria": "UTILITY"}

    ses = _SessionGravadora()
    cb._disparar(ses, {"id": 1}, "+5500000000000", sender, TENANT, linha=linha)

    payload = json.loads(ses.params[-1]["p"])
    assert payload["wamid"] == "wamid.ABC"


def test_o_contrato_do_sender_real_tem_o_campo_wamid():
    """Se alguém renomear o campo no sender, o carrossel passa a gravar None
    calado — e o placar volta a ser cego. Este teste é o alarme."""
    import inspect

    from app.api import cloud_corretora_a

    fonte = inspect.getsource(cloud_corretora_a.enviar_template_linha)
    assert '"wamid": wamid' in fonte


def test_registro_do_painel_grava_o_wamid(s):
    from app.api import painel

    w = _w()
    painel._registrar_disparo(
        lead_id="1", corretora_id=1, telefone=f"{_PREFIXO}900", template="t",
        variacao=0, texto="oi", autor="dona", wamid=w,
    )
    gravado = s.execute(
        _t("SELECT wamid FROM disparos_reengajamento WHERE telefone = :tel"),
        {"tel": f"{_PREFIXO}900"},
    ).scalar()
    assert gravado == w


@pytest.mark.parametrize("ret,esperado", [
    ({"wamid": "wamid.T"}, "wamid.T"),                       # template
    ({"messages": [{"id": "wamid.L"}]}, "wamid.L"),          # texto livre (resposta crua)
    ({"messages": []}, None),
    ({}, None),
    (None, None),
])
def test_wamid_sai_dos_dois_formatos_de_retorno(ret, esperado):
    from app.api import painel

    assert painel._wamid_do_retorno(ret) == esperado


# ── 2. O placar cruza por wamid ─────────────────────────────────────────────

def test_placar_cruza_o_disparo_com_o_status_pelo_wamid(s):
    ent, lido, falhou = _w(), _w(), _w()
    _disparo(s, 1, wamid=ent)
    _mensagem(s, 1, ent, status="delivered")
    _disparo(s, 2, wamid=lido)
    _mensagem(s, 2, lido, status="read")
    _disparo(s, 3, wamid=falhou)
    _mensagem(s, 3, falhou, status="failed",
              errors=[{"code": 131026, "title": "Message undeliverable"}])
    # Mensagem entregue que NÃO é de disparo nenhum: não pode entrar na conta.
    _mensagem(s, 4, _w(), status="delivered")

    r = _so_carrossel(s)
    assert (r["mandados"], r["entregues"], r["falhas"], r["em_voo"]) == (3, 2, 1, 0)
    assert r["pct_falha"] == 33.3


def test_mesmo_wamid_em_duas_instancias_conta_uma_vez(s):
    """A unicidade é (wamid, instance). JOIN simples dobraria o envio."""
    w = _w()
    _disparo(s, 1, wamid=w)
    _mensagem(s, 1, w, status="delivered", instance="acme_corretor")
    _mensagem(s, 1, w, status=None, instance="acme_corretora_a")

    r = _so_carrossel(s)
    assert (r["mandados"], r["entregues"]) == (1, 1)


def test_placar_respeita_o_recorte_de_datas(s):
    a, b = _w(), _w()
    _disparo(s, 1, wamid=a, dia=DIA)
    _mensagem(s, 1, a, status="delivered")
    _disparo(s, 2, wamid=b, dia=OUTRO_DIA)
    _mensagem(s, 2, b, status="failed", errors=[{"code": 131026, "title": "x"}])

    assert _so_carrossel(s, DIA, DIA)["mandados"] == 1
    assert _so_carrossel(s, DIA, DIA)["falhas"] == 0
    assert _so_carrossel(s, DIA, OUTRO_DIA)["mandados"] == 2


def test_disparo_do_painel_entra_e_marcador_de_opt_out_nao(s):
    w = _w()
    s.execute(
        _t("""INSERT INTO disparos_reengajamento
                (tenant_id, telefone, template, enviado_por, enviado_em, wamid)
              VALUES (:t, :tel, 't', 'dona', :em, :w)"""),
        {"t": TENANT, "tel": f"{_PREFIXO}010", "em": _meio_dia(DIA), "w": w},
    )
    # A linha que `_gravar_opt_out` insere só pra segurar o opt-out: não é envio.
    s.execute(
        _t("""INSERT INTO disparos_reengajamento
                (tenant_id, telefone, template, enviado_por, enviado_em, opt_out)
              VALUES (:t, :tel, 'carrossel', 'carrossel', :em, TRUE)"""),
        {"t": TENANT, "tel": f"{_PREFIXO}011", "em": _meio_dia(DIA)},
    )
    s.commit()
    _mensagem(s, 10, w, status="read")

    r = rp.placar(s, DIA, DIA, tenant_id=TENANT, fontes=("painel",))
    assert (r["mandados"], r["entregues"], r["sem_rastro"]) == (1, 1, 0)


# ── 3. Aceite não é entrega ─────────────────────────────────────────────────

@pytest.mark.parametrize("status", ["accepted", "sent", None])
def test_aceite_e_sent_nao_contam_como_entregue(s, status):
    w = _w()
    _disparo(s, 1, wamid=w)
    _mensagem(s, 1, w, status=status)

    r = _so_carrossel(s)
    assert r["entregues"] == 0
    assert r["em_voo"] == 1
    assert r["pct_entregue"] == 0.0


def test_disparo_cuja_mensagem_ainda_nao_tem_linha_fica_em_voo(s):
    """Wamid gravado, webhook ainda não chegou: não é entrega nem falha."""
    _disparo(s, 1, wamid=_w())
    r = _so_carrossel(s)
    assert (r["entregues"], r["em_voo"], r["falhas"]) == (0, 1, 0)


@pytest.mark.parametrize("status,balde", [
    ("delivered", "entregues"), ("read", "entregues"), ("READ", "entregues"),
    ("sent", "em_voo"), ("accepted", "em_voo"), (None, "em_voo"),
    ("status_que_nao_existe", "em_voo"),
    ("failed", "falhas"),
])
def test_balde_de_cada_status(status, balde):
    assert rp.balde_de("wamid.X", status) == balde


# ── 4. Lote antigo sem wamid ────────────────────────────────────────────────

def test_lote_antigo_sem_wamid_nao_quebra_e_fica_sem_rastro(s):
    _disparo(s, 1, wamid=None)
    _disparo(s, 2, wamid=None)

    r = _so_carrossel(s)
    assert (r["mandados"], r["sem_rastro"]) == (2, 2)
    assert (r["entregues"], r["em_voo"], r["falhas"]) == (0, 0, 0)
    # Sem envio rastreável não existe taxa — None, não 0% de falha.
    assert r["pct_falha"] is None
    assert r["pct_entregue"] is None


def test_sem_rastro_nao_dilui_a_taxa_de_falha(s):
    """1 falha em 1 rastreado = 100%, mesmo com 3 envios antigos no mesmo dia."""
    for n in (1, 2, 3):
        _disparo(s, n, wamid=None)
    w = _w()
    _disparo(s, 4, wamid=w)
    _mensagem(s, 4, w, status="failed", errors=[{"code": 131026, "title": "x"}])

    r = _so_carrossel(s)
    assert r["mandados"] == 4
    assert r["pct_falha"] == 100.0


def test_os_quatro_baldes_somam_os_mandados(s):
    e, f, v = _w(), _w(), _w()
    _disparo(s, 1, wamid=e)
    _mensagem(s, 1, e, status="delivered")
    _disparo(s, 2, wamid=f)
    _mensagem(s, 2, f, status="failed", errors=[{"code": 368, "title": "x"}])
    _disparo(s, 3, wamid=v)
    _mensagem(s, 3, v, status="sent")
    _disparo(s, 4, wamid=None)

    r = _so_carrossel(s)
    assert r["mandados"] == 4
    assert r["entregues"] + r["em_voo"] + r["falhas"] + r["sem_rastro"] == 4


def test_ultimo_lote_e_o_ultimo_dia_com_disparo(s, monkeypatch):
    # Evento próprio: o banco de teste é compartilhado e outro teste pode ter
    # disparo "de hoje" com o tipo real.
    monkeypatch.setattr(cb, "EVENT_DISPARO", "carrossel_disparo_teste_placar")
    assert rp.ultimo_lote(s, tenant_id=TENANT) is None

    a, b = _w(), _w()
    _disparo(s, 1, wamid=a, dia=DIA)
    _mensagem(s, 1, a, status="delivered")
    _disparo(s, 2, wamid=b, dia=OUTRO_DIA)
    _mensagem(s, 2, b, status="failed",
              errors=[{"code": 131026, "title": "Message undeliverable"}])

    r = rp.ultimo_lote(s, tenant_id=TENANT)
    assert r["de"] == r["ate"] == OUTRO_DIA.isoformat()
    assert (r["mandados"], r["falhas"], r["entregues"]) == (1, 1, 0)


# ── 5 e 6. Culpa ────────────────────────────────────────────────────────────

def test_131026_e_culpa_da_lista(s):
    w = _w()
    _disparo(s, 1, wamid=w)
    _mensagem(s, 1, w, status="failed",
              errors=[{"code": 131026, "title": "Message undeliverable"}])

    r = _so_carrossel(s)
    assert r["culpas"] == {"lista": 1}
    assert r["motivos"] == [{"codigo": "131026", "titulo": "Message undeliverable",
                             "n": 1, "culpa": "lista"}]


def test_codigo_desconhecido_nao_e_chutado_como_lista(s):
    w = _w()
    _disparo(s, 1, wamid=w)
    _mensagem(s, 1, w, status="failed",
              errors=[{"code": 999999, "title": "Erro que a Meta inventou ontem"}])

    r = _so_carrossel(s)
    assert r["culpas"] == {"desconhecida": 1}


def test_falha_antiga_sem_codigo_nao_vira_lista(s):
    """Status gravado ANTES de o patch guardar o código: só há o título.
    'Message undeliverable' sem código fica desconhecida — não adivinha."""
    w = _w()
    _disparo(s, 1, wamid=w)
    _mensagem(s, 1, w, status="failed", errors=[{"title": "Message undeliverable"}])

    assert _so_carrossel(s)["culpas"] == {"desconhecida": 1}


@pytest.mark.parametrize("codigo,titulo,culpa", [
    ("131026", "Message undeliverable", "lista"),
    ("131047", None, "lista"),
    ("131049", None, "lista"),
    ("470", None, "lista"),
    ("131042", "Business eligibility payment issue", "conta"),
    ("368", None, "conta"),
    ("131031", None, "conta"),
    ("132001", "Template name does not exist", "template"),
    ("132015", None, "template"),
    (None, "Payment method problem", "conta"),       # sem código: marca no título
    ("999999", "qualquer coisa", "desconhecida"),
    (None, None, "desconhecida"),
    ("", "", "desconhecida"),
])
def test_tabela_de_culpa(codigo, titulo, culpa):
    assert rp.culpa_de(codigo, titulo) == culpa


def test_patch_de_status_guarda_o_codigo_do_erro():
    """Sem o código no metadata o placar não tem como classificar a culpa."""
    patch = build_status_patch(
        "failed", errors=[{"code": 131026, "title": "Message undeliverable"}]
    )
    assert patch["delivery_error_code"] == "131026"
    assert patch["delivery_error"] == "Message undeliverable"
    assert "delivery_error_code" not in build_status_patch("delivered")
    assert "delivery_error_code" not in build_status_patch(
        "failed", errors=[{"title": "sem código"}]
    )


# ── O mapa do painel ────────────────────────────────────────────────────────

def test_mapa_publica_o_placar_do_ultimo_lote(s, monkeypatch):
    from app.api import automacoes

    monkeypatch.setattr(cb, "EVENT_DISPARO", "carrossel_disparo_teste_placar")
    w = _w()
    _disparo(s, 1, wamid=w)
    _mensagem(s, 1, w, status="accepted")

    bloco = automacoes._mapa_carrossel_corretor()["entrega_ultimo_lote"]
    assert bloco["indisponivel"] is False
    assert bloco["placar"]["mandados"] == 1
    assert bloco["placar"]["entregues"] == 0     # aceite não é entrega, na tela também
    assert bloco["placar"]["em_voo"] == 1


def test_mapa_diz_indisponivel_quando_o_placar_falha(monkeypatch):
    """Erro de leitura não pode chegar na tela como '0 falhas'."""
    from app.api import automacoes

    def _quebra(*a, **k):
        raise RuntimeError("banco fora")

    monkeypatch.setattr(rp, "ultimo_lote", _quebra)
    bloco = automacoes._mapa_carrossel_corretor()["entrega_ultimo_lote"]
    assert bloco["indisponivel"] is True
    assert bloco["placar"] is None
    assert "banco fora" in bloco["motivo_indisponivel"]


def test_o_mapa_nao_liga_o_carrossel():
    """Instrumentação não é operação: ler o placar não mexe no interruptor."""
    from app.services import config_operacional

    antes = config_operacional.carrossel_corretor_ativo()
    from app.api import automacoes

    automacoes._mapa_carrossel_corretor()
    assert config_operacional.carrossel_corretor_ativo() == antes
