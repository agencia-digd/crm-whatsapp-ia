"""EDITAR A COPY e QUANDO A PESSOA RESPONDE — prova de ponta a ponta (05/10/2026).

Pedido da Dona: *"lá eu queria também poder mudar a copy"* e *"quando cair na
etapa respondeu vai ganhar tag RMKT, vai para etapa de entrada do funil de
vendas, e vai ter roleta para remarketing"*. Em seguida: *"todo mundo que cair
em Respondeu vai para a roleta"*, *"vai com histórico"* e o texto do aviso.

Mesmo desenho de `test_garantia_painel_manda.py` — cada teste faz o caminho
inteiro, sem enviar nada:

    API da tela (as rotas que o painel chama)
        → banco (`config_operacional`)
            → o motor DE VERDADE
                 copy:      `carrossel_corretor.tick` com um remetente falso
                 respostas: o webhook da linha da Corretor (`/cloud/corretora/corretor/inbound`)
                            → `carrossel_corretor.processar_inbound`
                            → `handoff_remarketing.processar_resposta`

A regra provada é a mesma: **mudou na tela, vale na volta seguinte** (e, dentro
de uma volta, antes da PRÓXIMA mensagem). O que a tela recusaria nunca é
gravado; o que chegou no banco por fora SEGURA em vez de virar padrão.

Estes nomes de teste estão na tabela de `projetos/crm/COPY-E-QUANDO-RESPONDE.md`.
"""
from __future__ import annotations

import inspect
import pathlib
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text as _t

from app.db.session import SessionLocal
from app.main import app
from app.services import atendimento_corretor as ab
from app.services import carrossel_corretor as cb
from app.services import carrossel_copies as cc
from app.services import cloud_lines as cl
from app.services import config_operacional as cfg
from app.services import handoff_remarketing as hr
from app.services import lead_intake
from app.services import quando_responde as qr
from app.services import rampa
from app.services import remarketing_agenda as agenda

TENANT = 1
ADMIN = {"X-Painel-User": "dona"}
# O atendimento de verdade, guardado antes de a fixture `linha_corretor` trocá-lo
# por um dublê: a seção E prova o que a própria Pessoa decide.
_ATENDER_DE_VERDADE = ab.atender
_PREFIXO = "5500000000000"                 # faixa de telefone só destes testes
SEGREDO = "s3-copy"

CORRETOR, CORRETOR, CORRETOR = 1, 2, 3
CORRETOR = cb.CORRETORA_CORRETOR

NOVO, EM_ATENDIMENTO, VENDA_PERDIDA = 6, 7, 13
ETAPA_SOME = 9302                       # criada e apagada num teste
ETAPA_DE_OUTRO_CLIENTE = 9303

QUARTA_15H = datetime(2026, 10, 7, 18, 0, tzinfo=timezone.utc)

CHAVES = (
    "carrossel_corretor_ativo", "carrossel_pesos", "carrossel_copies",
    "carrossel_marketing_espelha_direta", "remarketing_etapa",
    "handoff_remarketing_ativo", "handoff_remarketing_quem_vai",
    "handoff_remarketing_etapa", "handoff_remarketing_roleta",
    "handoff_remarketing_avisa_lead", "handoff_remarketing_aviso_texto",
    "handoff_remarketing_aviso_resumo", "handoff_remarketing_respeita_humano",
    "roleta_corretoras", "lead_intake_ativo",
)

TEXTO_NOVO = ("Aqui é da Acme Imóveis. Saiu uma leva nova de imóveis em "
              "Cidade. Quer que eu te mande?")


# ── Dublê: o remetente do disparo ────────────────────────────────────────────


class Remetente:
    """Contrato do remetente real quando a Meta ACEITA. Anota o texto."""

    def __init__(self, ao_enviar=None) -> None:
        self.envios: list[tuple[str, str]] = []
        self.ao_enviar = ao_enviar

    def __call__(self, telefone, texto, *, com_botao, **kw):
        self.envios.append((telefone, texto))
        if self.ao_enviar:
            self.ao_enviar(len(self.envios))
        return {"ok": True, "dry_run": False, "sent": True,
                "wamid": f"wamid.copy.{telefone[-4:]}.{len(self.envios)}",
                "template": "acme_corretor_ola_a_01", "categoria": "UTILITY"}

    @property
    def textos(self) -> list[str]:
        return [t for _, t in self.envios]


# ── Montagem ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def s():
    def _limpar(ses):
        for sql in (
            "DELETE FROM lead_events WHERE lead_id IN "
            "(SELECT id FROM leads WHERE telefone LIKE :p)",
            "DELETE FROM mensagens_whatsapp WHERE contato_telefone LIKE :p",
            "DELETE FROM disparos_reengajamento WHERE telefone LIKE :p",
            "DELETE FROM cadencia_estado WHERE telefone LIKE :p",
            "DELETE FROM conversations WHERE phone LIKE :p",
            "DELETE FROM opt_outs WHERE telefone LIKE :p",
            "DELETE FROM leads WHERE telefone LIKE :p",
        ):
            try:
                with ses.begin_nested():
                    ses.execute(_t(sql), {"p": f"%{_PREFIXO[2:]}%"})
            except Exception:  # noqa: BLE001 — tabela opcional no banco de teste
                pass
        ses.execute(_t("DELETE FROM config_operacional WHERE chave = ANY(:c)"),
                    {"c": list(CHAVES)})
        ses.execute(_t("DELETE FROM config_operacional_historico WHERE chave = ANY(:c)"),
                    {"c": list(CHAVES)})
        ses.execute(_t("DELETE FROM pipeline_stages WHERE id = ANY(:i)"),
                    {"i": [ETAPA_SOME, ETAPA_DE_OUTRO_CLIENTE]})
        ses.execute(_t("UPDATE corretoras SET ativo = TRUE WHERE id = ANY(:i)"),
                    {"i": [CORRETOR, CORRETOR, CORRETOR, CORRETOR]})
        ses.commit()
        cfg.invalidar_cache()

    with SessionLocal() as ses:
        for i, nome, funil, ordem, tipo in (
            (NOVO, "Novo", "principal", 1, None),
            (EM_ATENDIMENTO, "Em atendimento", "principal", 2, None),
            (VENDA_PERDIDA, "Venda perdida", "principal", 8, "perdido"),
            (cb.STAGE_FILA_PADRAO, "Fila", "remarketing", 7, None),
            (cb.STAGE_DISPARADO, "Disparado", "remarketing", 8, None),
            (cb.STAGE_RESPONDEU, "Respondeu", "remarketing", 9, None),
        ):
            ses.execute(_t(
                "INSERT INTO pipeline_stages (id, tenant_id, nome, funil, ordem, tipo) "
                "VALUES (:i, :t, :n, :f, :o, :tp) ON CONFLICT (id) DO UPDATE "
                "SET nome = :n, funil = :f, ordem = :o, tipo = :tp, tenant_id = :t"),
                {"i": i, "t": TENANT, "n": nome, "f": funil, "o": ordem, "tp": tipo})
        ses.execute(_t(
            "INSERT INTO corretoras (id, nome, telefone, telefone_normalizado, ativo) "
            "VALUES (:id, 'Corretor', '+5500000000000', '+5500000000000', true) "
            "ON CONFLICT (id) DO UPDATE SET nome = 'Corretor', ativo = true"), {"id": CORRETOR})
        # Como em produção: a Pessoa3 se apresenta como Pessoa (é o nome
        # público que o aviso usa) e a linha da casa é a instância `acme_corretor`.
        # O que estava no banco de teste volta no fim — outros arquivos contam
        # com o cadastro cru.
        antes = ses.execute(_t(
            "SELECT id, nome_publico, evolution_instance FROM corretoras "
            "WHERE id = ANY(:i)"), {"i": [CORRETOR, CORRETOR]}).all()
        ses.execute(_t("UPDATE corretoras SET nome_publico = 'Pessoa' WHERE id = :i"),
                    {"i": CORRETOR})
        ses.execute(_t("UPDATE corretoras SET evolution_instance = 'acme_corretor' "
                       "WHERE id = :i"), {"i": CORRETOR})
        ses.commit()
        import app.api.painel as _painel

        _painel._LINHA_MAPA_CACHE.update(at=0.0, mapa={})
        _limpar(ses)
        yield ses
        ses.rollback()
        for cid, publico, instancia in antes:
            ses.execute(_t("UPDATE corretoras SET nome_publico = :p, "
                           "evolution_instance = :e WHERE id = :i"),
                        {"p": publico, "e": instancia, "i": cid})
        ses.commit()
        _painel._LINHA_MAPA_CACHE.update(at=0.0, mapa={})
        _limpar(ses)


@pytest.fixture()
def painel() -> TestClient:
    return TestClient(app)


@pytest.fixture()
def mesa(monkeypatch, s, painel):
    """Ponto de partida: disparo desligado, quarta às 15h, sem curva de
    horário. A linha disparadora é a `corretor` (fixture do conftest)."""
    monkeypatch.setattr(agenda, "_agora_sp",
                        lambda agora=None: QUARTA_15H.astimezone(agenda.TZ_SP))
    monkeypatch.setattr(cb, "_cota_ate_agora", lambda cap, agora=None, janela=None: cap)
    rampa._cache.clear()
    return painel


@pytest.fixture()
def linha_corretor(monkeypatch, s):
    """O webhook da linha da Corretor de pé, com a Pessoa calada (nenhuma IA roda
    aqui) e sem opt-out em arquivo. O que decide é tudo de verdade."""
    from app.config import settings

    corretor = cl.CloudLine(phone_id="111", slug="corretor", papel="corretora",
                       env_file="/etc/agencia/review.env", token_var="META_REVIEW_TOKEN")
    linhas = dict(cl._LINES)
    linhas["111"] = corretor
    slugs = dict(cl._BY_SLUG)
    slugs["corretor"] = "111"
    monkeypatch.setattr(cl, "_LINES", linhas)
    monkeypatch.setattr(cl, "_BY_SLUG", slugs)
    monkeypatch.setattr(settings, "cloud_webhook_secret", SEGREDO)
    atendidos: list[dict] = []
    monkeypatch.setattr(ab, "atender",
                        lambda tel, texto, nome=None, **k: atendidos.append(
                            {"tel": tel, "texto": texto, **k}) or {"atendeu": False})
    monkeypatch.setattr("app.services.pos_ligacao._pediu_silencio", lambda ses, t: False)
    return atendidos


def tela(painel: TestClient, chave: str, valor) -> None:
    """Grava UM controle do jeito que a tela grava."""
    r = painel.put(f"/painel/automacoes/config/{chave}", json={"valor": valor},
                   headers=ADMIN)
    assert r.status_code == 200, f"{chave}={valor!r}: {r.status_code} {r.text}"


def salvar_copy(painel: TestClient, **corpo):
    return painel.put("/painel/automacoes/remarketing/copy", json=corpo, headers=ADMIN)


def salvar_responde(painel: TestClient, **corpo):
    return painel.put("/painel/automacoes/remarketing/quando-responde", json=corpo,
                      headers=ADMIN)


def lead(s, sufixo: str, etapa: int = cb.STAGE_FILA_PADRAO, *, minutos_atras: int = 0,
         dono: int | None = CORRETOR, **campos) -> int:
    lid = s.execute(
        _t("""INSERT INTO leads (tenant_id, nome, telefone, owner_id, stage_id,
                                 created_at, updated_at, veio_remarketing_em)
              VALUES (:t, 'Teste Copy', :tel, :o, :st, now(),
                      now() - make_interval(mins => :m), :rmkt)
           RETURNING id"""),
        {"t": TENANT, "tel": f"{_PREFIXO}{sufixo}", "o": dono, "st": etapa,
         "m": minutos_atras, "rmkt": campos.get("veio_remarketing_em")},
    ).scalar()
    s.commit()
    return int(lid)


def fila_com(s, quer: dict[str, int], comeca: int = 400) -> dict[str, list[int]]:
    """Cria leads na Fila até ter `quer[variante]` em cada balde pedido; quem
    cair em balde que já encheu (ou não foi pedido) é apagado. O balde é o
    hash do id, então só dá pra saber depois de criar."""
    por: dict[str, list[int]] = {v: [] for v in quer}
    for i in range(400):
        if all(len(por[v]) >= n for v, n in quer.items()):
            return por
        lid = lead(s, f"{comeca + i:03d}", minutos_atras=1000 - i)
        v = cc.escolher(lid).id
        if v in por and len(por[v]) < quer[v]:
            por[v].append(lid)
        else:
            s.execute(_t("DELETE FROM leads WHERE id = :i"), {"i": lid})
            s.commit()
    raise AssertionError(f"não consegui encher os baldes: {por}")


def tel_de(s, lead_id: int) -> str:
    return str(s.execute(_t("SELECT telefone FROM leads WHERE id = :i"),
                         {"i": lead_id}).scalar())


def linha_do(s, lead_id: int):
    s.expire_all()
    return s.execute(
        _t("SELECT stage_id, owner_id, veio_remarketing_em, owner_editado_manual "
           "FROM leads WHERE id = :i"), {"i": lead_id}).first()


def eventos(s, lead_id: int, tipo: str) -> list[dict]:
    return [dict(r[0]) for r in s.execute(
        _t("SELECT payload FROM lead_events WHERE lead_id = :i AND tipo = :tp ORDER BY at, id"),
        {"i": lead_id, "tp": tipo}).all()]


def volta(remetente: Remetente, **kw) -> cb.CarrosselResult:
    return cb.tick(sender=remetente, tenant_id=TENANT, **kw)


def disparado(s, sufixo: str, *, ha_minutos: int = 60, variante: str = "direta",
              versao: str | None = None) -> tuple[str, int]:
    """Um lead que JÁ recebeu o disparo: em Disparado, com a Corretor, e com o
    `carrossel_disparo` registrado há `ha_minutos`."""
    lid = lead(s, sufixo, cb.STAGE_DISPARADO)
    payload = {"variante": variante, "categoria": "UTILITY", "origem": "carrossel"}
    if versao:
        payload["copy_versao"] = versao
    import json

    s.execute(_t(
        "INSERT INTO lead_events (tenant_id, lead_id, tipo, payload, at) "
        "VALUES (:t, :i, :ev, CAST(:p AS jsonb), now() - make_interval(mins => :m))"),
        {"t": TENANT, "i": lid, "ev": cb.EVENT_DISPARO, "p": json.dumps(payload),
         "m": ha_minutos})
    s.commit()
    return f"{_PREFIXO}{sufixo}", lid


def chega(painel: TestClient, tel: str, corpo: str | None = "Bom dia", *,
          tipo: str = "text", nome: str = "Fulano", quando: float | None = None):
    """A Meta entrega uma mensagem do lead na linha da Corretor."""
    msg: dict = {"from": tel, "id": f"wamid.in.{tel[-4:]}.{time.time_ns()}",
                 "timestamp": str(int(quando or time.time())), "type": tipo}
    if tipo == "text":
        msg["text"] = {"body": corpo}
    elif tipo == "button":
        msg["button"] = {"text": corpo, "payload": corpo}
    else:
        msg[tipo] = {"id": "midia-1", "mime_type": "audio/ogg"}
    r = painel.post("/cloud/corretora/corretor/inbound", headers={"X-Cloud-Secret": SEGREDO},
                    json={"messages": [msg],
                          "contacts": [{"wa_id": tel, "profile": {"name": nome}}]})
    assert r.status_code == 200, r.text
    return r


# ═════════════════════════════════════════════════════════════════════════════
# A. EDITAR A COPY — `carrossel_copies` e `carrossel_marketing_espelha_direta`
# ═════════════════════════════════════════════════════════════════════════════


def esvazia_a_fila() -> dict[str, str]:
    """Roda voltas até não sair mais nada; devolve sufixo do telefone → texto."""
    r = Remetente()
    while volta(r).enviados:
        pass
    return {t[-8:]: txt for t, txt in r.envios}


def test_copy_editada_na_tela_sai_na_volta_seguinte(mesa, s):
    padrao_curta = cc.texto_padrao("curta")
    lote1 = fila_com(s, {"direta": 2, "curta": 2}, comeca=100)
    tela(mesa, "carrossel_corretor_ativo", True)

    # antes de qualquer edição, sai o texto do código
    saiu = esvazia_a_fila()
    assert len(saiu) == 4
    for lid in lote1["direta"]:
        assert saiu[tel_de(s, lid)[-8:]] == cc.TEXTO_DIRETA
        assert eventos(s, lid, cb.EVENT_DISPARO)[0]["copy_versao"] == \
            cc.versao_do_texto(cc.TEXTO_DIRETA)

    resp = salvar_copy(mesa, textos={"direta": TEXTO_NOVO})
    assert resp.status_code == 200, resp.text

    lote2 = fila_com(s, {"direta": 2, "curta": 2}, comeca=150)
    saiu = esvazia_a_fila()
    assert len(saiu) == 4
    for lid in lote2["direta"]:
        assert saiu[tel_de(s, lid)[-8:]] == TEXTO_NOVO
        (ev,) = eventos(s, lid, cb.EVENT_DISPARO)
        assert ev["copy_versao"] == cc.versao_do_texto(TEXTO_NOVO)
    # a outra variante não foi mexida: segue no texto do código
    for lid in lote2["curta"]:
        assert saiu[tel_de(s, lid)[-8:]] == padrao_curta
        (ev,) = eventos(s, lid, cb.EVENT_DISPARO)
        assert ev["copy_versao"] == cc.versao_do_texto(padrao_curta)


def test_copy_mudada_no_meio_do_lote_vale_na_proxima_mensagem(mesa, s):
    """Uma volta manda até 3, e o texto é relido antes de CADA mensagem."""
    tela(mesa, "carrossel_pesos", {"direta": 90, "curta": 0, "direta_marketing": 10})
    for i in range(3):
        lead(s, f"{200 + i}", minutos_atras=1000 - i)
    tela(mesa, "carrossel_corretor_ativo", True)
    r = Remetente(ao_enviar=lambda n: n == 1 and salvar_copy(mesa, textos={"direta": TEXTO_NOVO}))

    assert volta(r).enviados == 3
    # (com o espelho ligado a variante de marketing sai com o mesmo texto)
    assert r.textos == [cc.TEXTO_DIRETA, TEXTO_NOVO, TEXTO_NOVO]


@pytest.mark.parametrize("texto,trecho", [
    ("Oi {nome}, tudo certo?", "chaves"),
    ("x" * (cc.TEXTO_MAX_CHARS + 1), "longo demais"),
    ("Oi​ tudo certo", "invisível"),
    (123, "precisa ser texto"),
])
def test_texto_invalido_nao_e_gravado_e_o_erro_vem_na_variante(mesa, s, texto, trecho):
    r = salvar_copy(mesa, textos={"direta": texto, "curta": TEXTO_NOVO})

    assert r.status_code == 422, r.text
    corpo = r.json()
    assert trecho in corpo["erros"]["direta"]
    assert "curta" not in corpo["erros"]
    # tudo ou nada: nem a variante que estava certa foi gravada
    assert corpo["gravadas"] == []
    assert s.execute(_t("SELECT count(*) FROM config_operacional "
                        "WHERE chave = 'carrossel_copies'")).scalar() == 0


def test_variante_desconhecida_ou_aposentada_nao_se_edita(mesa, s):
    r = salvar_copy(mesa, textos={"nao_existe": TEXTO_NOVO})
    assert r.status_code == 422 and "desconhecida" in r.json()["erros"]["nao_existe"]
    r = salvar_copy(mesa, textos={"pergunta": TEXTO_NOVO})
    assert r.status_code == 422 and "aposentada" in r.json()["erros"]["pergunta"]


def test_a_rota_generica_tem_a_mesma_validacao(mesa, s):
    """Um curl direto na chave bate na mesma trava que a tela."""
    r = mesa.put("/painel/automacoes/config/carrossel_copies",
                 json={"valor": {"direta": "Oi {nome}"}}, headers=ADMIN)
    assert r.status_code == 422 and "chaves" in r.json()["detail"]
    r = mesa.put("/painel/automacoes/config/carrossel_marketing_espelha_direta",
                 json={"valor": "sim"}, headers=ADMIN)
    assert r.status_code == 422


@pytest.mark.parametrize("gravado", [
    {"direta": "Oi {nome}, ainda procurando?"},     # placeholder que sairia literal
    {"direta": "x" * 2000},                          # não cabe na variável
    {"direta": "tem  dois espaços"},                 # não está na forma enviada
    {"variante_que_nao_existe": "texto"},
    "isto não é um mapa",
])
def test_texto_gravado_por_fora_segura_o_disparo(mesa, s, gravado):
    """Falha fechada, igual aos pesos inválidos: não envia e diz por quê.
    Nunca cai calado no texto do código."""
    import json

    ids = [lead(s, f"{300 + i}", minutos_atras=1000 - i) for i in range(3)]
    tela(mesa, "carrossel_corretor_ativo", True)
    s.execute(_t("INSERT INTO config_operacional (chave, valor, atualizado_em, atualizado_por) "
                 "VALUES ('carrossel_copies', CAST(:v AS jsonb), now(), 'sql na mão')"),
              {"v": json.dumps(gravado)})
    s.commit()

    r = Remetente()
    res = volta(r)

    assert r.envios == [], "saiu mensagem com a copy gravada inválida"
    assert res.motivo_parada == "copy_invalida"
    assert agenda.MOTIVO_EM_PORTUGUES["copy_invalida"]
    s.expire_all()
    assert [linha_do(s, i)[0] for i in ids] == [cb.STAGE_FILA_PADRAO] * 3
    # e a tela diz o mesmo motivo
    mapa = mesa.get("/painel/automacoes", headers=ADMIN).json()["carrossel_corretor"]
    assert mapa["copies"]["problema"]


def test_espelho_ilegivel_segura_o_disparo(mesa, s):
    lead(s, "350", minutos_atras=10)
    tela(mesa, "carrossel_corretor_ativo", True)
    s.execute(_t("INSERT INTO config_operacional (chave, valor, atualizado_em, atualizado_por) "
                 "VALUES ('carrossel_marketing_espelha_direta', '\"talvez\"'::jsonb, now(), 'sql')"))
    s.commit()
    r = Remetente()
    assert volta(r).motivo_parada == "copy_invalida"
    assert r.envios == []


def test_copy_que_estraga_entre_a_conferencia_e_o_envio_nao_sai(mesa, s, monkeypatch):
    """O cinto com o suspensório: se o texto ficar ilegível DEPOIS da
    autorização e antes do envio, nada sai e o lead volta pra fila."""
    lid = lead(s, "360", minutos_atras=10)
    tela(mesa, "carrossel_corretor_ativo", True)
    monkeypatch.setattr(cc, "problema_nas_copies", lambda: None)

    def _quebrado():
        raise cc.CopyInvalida("carrossel_copies[direta]: estragou no caminho")

    monkeypatch.setattr(cc, "_ler_copies_estrito", _quebrado)
    r = Remetente()
    res = volta(r)
    assert r.envios == [] and res.enviados == 0
    assert linha_do(s, lid)[0] == cb.STAGE_FILA_PADRAO
    assert eventos(s, lid, cb.EVENT_DISPARO) == []


def test_o_que_e_gravado_e_o_que_chega_no_celular(mesa, s):
    """Quebra de linha e espaço a mais são limpos como o envio limpa; a prévia
    é o casco do template + o texto, e o que vai pra Meta é o mesmo texto com
    a quebra trocada pelo caractere que ela aceita."""
    from app.api.cloud_corretora_a import normalizar_param_template

    digitado = "Aqui é da Acme.  \n\n\n\nApareceram\topções novas. \r\nQuer ver?  "
    r = salvar_copy(mesa, textos={"curta": digitado})
    assert r.status_code == 200, r.text
    curta = next(v for v in r.json()["variantes"] if v["id"] == "curta")
    gravado = "Aqui é da Acme.\n\nApareceram opções novas.\nQuer ver?"
    assert curta["texto"] == gravado
    assert cfg.get("carrossel_copies") == {"curta": gravado}
    assert curta["previa"] == (
        f"Olá João, tudo bem?\n\n{gravado}\n\nTô por aqui se precisar 🙂")
    assert normalizar_param_template(gravado) == gravado.replace("\n", "\v")
    # o casco mais comprido da linha + um nome de 60 letras ainda cabe na Meta
    assert 62 + 60 + cc.TEXTO_MAX_CHARS <= 1024


def test_espelho_da_variante_de_marketing(mesa, s):
    """Por padrão `direta_marketing` segue o texto da `direta` (teste de
    categoria). Desligar o espelho deixa editar as duas separadas."""
    tela(mesa, "carrossel_pesos", {"direta": 90, "curta": 0, "direta_marketing": 10})
    lote1 = fila_com(s, {"direta": 2, "direta_marketing": 2})
    tela(mesa, "carrossel_corretor_ativo", True)

    assert salvar_copy(mesa, textos={"direta": TEXTO_NOVO}).status_code == 200
    mapa = mesa.get("/painel/automacoes", headers=ADMIN).json()["carrossel_corretor"]
    mkt = next(v for v in mapa["variantes"] if v["id"] == "direta_marketing")
    assert (mkt["texto"], mkt["espelhando"], mkt["texto_editavel"]) == (TEXTO_NOVO, True, False)
    assert mkt["texto_igual_a"] == ["direta"]

    saiu = esvazia_a_fila()
    assert len(saiu) == 4 and set(saiu.values()) == {TEXTO_NOVO}
    for lid in lote1["direta_marketing"]:
        (ev,) = eventos(s, lid, cb.EVENT_DISPARO)
        assert ev["copy_versao"] == cc.versao_do_texto(TEXTO_NOVO)

    # desliga o espelho e edita só a de marketing
    outro = "Aqui é da Acme Imóveis. Tem condição especial essa semana. Posso te mandar?"
    resp = salvar_copy(mesa, espelho=False, textos={"direta_marketing": outro})
    assert resp.status_code == 200, resp.text
    mkt = next(v for v in resp.json()["variantes"] if v["id"] == "direta_marketing")
    assert (mkt["texto"], mkt["espelhando"], mkt["texto_editavel"]) == (outro, False, True)

    lote2 = fila_com(s, {"direta": 2, "direta_marketing": 2}, comeca=600)
    saiu = esvazia_a_fila()
    assert len(saiu) == 4
    for lid in lote2["direta_marketing"]:
        assert saiu[tel_de(s, lid)[-8:]] == outro
    for lid in lote2["direta"]:
        assert saiu[tel_de(s, lid)[-8:]] == TEXTO_NOVO


def test_voltar_ao_padrao_nao_apaga_o_historico(mesa, s):
    assert salvar_copy(mesa, textos={"direta": TEXTO_NOVO}).status_code == 200
    r = salvar_copy(mesa, textos={"direta": None})
    assert r.status_code == 200, r.text
    direta = next(v for v in r.json()["variantes"] if v["id"] == "direta")
    assert (direta["texto"], direta["texto_personalizado"]) == (cc.TEXTO_DIRETA, False)

    hist = [h for h in cc.historico_das_copies() if h["variante"] == "direta"]
    assert [(h["antes"], h["depois"], h["autor"]) for h in hist] == [
        (TEXTO_NOVO, None, "dona"),               # voltou ao padrão
        (cc.TEXTO_DIRETA, TEXTO_NOVO, "dona"),    # a edição
    ]
    assert all(h["em"] for h in hist)
    # e a tela recebe o mesmo histórico, com "desde quando" do texto que vale
    mapa = mesa.get("/painel/automacoes", headers=ADMIN).json()["carrossel_corretor"]
    assert len([h for h in mapa["copies"]["historico"] if h["variante"] == "direta"]) == 2
    direta = next(v for v in mapa["variantes"] if v["id"] == "direta")
    assert direta["texto_desde"] == hist[0]["em"]
    curta = next(v for v in mapa["variantes"] if v["id"] == "curta")
    assert curta["texto_desde"] is None       # ninguém mexeu: é o texto do código


def test_mudar_o_texto_comeca_uma_medicao_nova(mesa, s, linha_corretor):
    """A medição não mente: o que saiu com o texto antigo não soma na taxa do
    texto novo — fica à parte. E voltar ao texto antigo traz os números dele."""
    def metricas():
        r = mesa.get("/painel/automacoes/remarketing/metricas?dias=30", headers=ADMIN)
        assert r.status_code == 200, r.text
        return next(v for v in r.json()["variantes"] if v["id"] == "direta")

    # 3 disparos com o texto do código (um deles sem versão gravada: é disparo
    # de antes desta entrega), e um respondeu com interesse.
    v_codigo = cc.versao_do_texto(cc.TEXTO_DIRETA)
    tel, lid = disparado(s, "501", versao=v_codigo)
    disparado(s, "502", versao=v_codigo)
    disparado(s, "503")
    chega(mesa, tel, "Quero sim")
    (resp,) = eventos(s, lid, cb.EVENT_RESPOSTA)
    assert resp["copy_versao"] == v_codigo, "a resposta não levou a versão do texto"

    m = metricas()
    assert (m["enviados"], m["respostas"], m["textos_anteriores"]) == (3, 1, None)

    assert salvar_copy(mesa, textos={"direta": TEXTO_NOVO}).status_code == 200
    m = metricas()
    assert (m["enviados"], m["respostas"], m["sem_dado"]) == (0, 0, True)
    assert (m["textos_anteriores"]["enviados"], m["textos_anteriores"]["respostas"]) == (3, 1)
    assert m["texto_desde"], "a tela não sabe desde quando o texto novo vale"

    disparado(s, "504", versao=cc.versao_do_texto(TEXTO_NOVO))
    m = metricas()
    assert (m["enviados"], m["textos_anteriores"]["enviados"]) == (1, 3)

    assert salvar_copy(mesa, textos={"direta": None}).status_code == 200
    m = metricas()
    assert (m["enviados"], m["respostas"]) == (3, 1)
    assert m["textos_anteriores"]["enviados"] == 1


def test_o_motor_le_o_texto_do_banco_antes_de_cada_mensagem():
    """Contrato de código: a autorização final confere a copy, e o envio usa
    `copy_para_envio` (banco), nunca `var.texto` (código) direto."""
    fonte_aut = inspect.getsource(cb._autorizacao_final)
    assert "problema_nas_copies()" in fonte_aut and '"copy_invalida"' in fonte_aut
    fonte = inspect.getsource(cb._disparar)
    assert "cc.copy_para_envio(var)" in fonte
    assert "var.texto" not in fonte
    for chave in (cc.CHAVE_COPIES, cc.CHAVE_ESPELHO, *qr.CHAVES):
        assert chave in cfg.CHAVES_ESTRITAS, chave


# ═════════════════════════════════════════════════════════════════════════════
# B. QUANDO A PESSOA RESPONDE
# ═════════════════════════════════════════════════════════════════════════════


def test_o_padrao_e_todo_mundo_e_o_aviso_e_o_texto_da_dona(s):
    assert cfg.DEFAULTS["handoff_remarketing_quem_vai"]() == "todos"
    assert cfg.DEFAULTS["handoff_remarketing_etapa"]() is None
    assert cfg.DEFAULTS["handoff_remarketing_aviso_resumo"]() is False
    assert qr.AVISO_PADRAO == ("Sou a {corretora}, da Acme Imóveis. Estávamos falando "
                               "em outro número, vamos continuar o atendimento por aqui!")
    assert qr.etapa_padrao(s, TENANT) == (NOVO, "Novo")


@pytest.mark.parametrize("corpo,tipo", [
    ("Bom dia", "text"),
    (None, "audio"),
    (None, "sticker"),
    ("Saiba mais", "button"),
    ("Quero sim, pode mandar", "text"),
])
def test_todo_mundo_que_cai_em_respondeu_vai_pra_roleta(mesa, s, linha_corretor, corpo, tipo):
    """Decisão da Dona (05/10): "todo mundo que cair em Respondeu vai para a
    roleta" — inclui bom dia, áudio e figurinha. Tag, roleta e etapa juntas."""
    tela(mesa, "handoff_remarketing_roleta", [CORRETOR])
    tel, lid = disparado(s, "601")

    chega(mesa, tel, corpo, tipo=tipo)

    etapa, dono, marca, manual = linha_do(s, lid)
    assert (etapa, dono) == (NOVO, CORRETOR)
    assert marca is not None, "o card foi transferido sem a tag RMKT"
    assert manual is True
    (ev,) = eventos(s, lid, hr.EVENTO_TRANSFERENCIA)
    assert (ev["de"], ev["para"], ev["quem"]) == (CORRETOR, CORRETOR, "handoff_remarketing")
    assert (ev["tag"], ev["roleta"], ev["quem_vai"]) == ("RMKT", "remarketing", "todos")
    assert (ev["stage_de"], ev["stage_para"]) == (cb.STAGE_RESPONDEU, NOVO)
    assert ev["sinal"] == "respondeu"
    # o webhook ainda entrega a mensagem ao atendimento; quem decide calar a
    # Pessoa depois da transferência é ele (seção E)
    assert [a["tel"] for a in linha_corretor] == [tel]


@pytest.mark.parametrize("corpo,etapa_final", [
    ("Não tenho interesse", VENDA_PERDIDA),
    ("PARAR", VENDA_PERDIDA),
])
def test_recusa_e_parar_nunca_vao_pra_corretora(mesa, s, linha_corretor, monkeypatch,
                                                 corpo, etapa_final):
    monkeypatch.setattr(cb, "_gravar_opt_out", lambda *a, **k: None)
    tel, lid = disparado(s, "611")
    chega(mesa, tel, corpo)
    etapa, dono, marca, _ = linha_do(s, lid)
    assert (etapa, dono, marca) == (etapa_final, CORRETOR, None)
    assert eventos(s, lid, hr.EVENTO_TRANSFERENCIA) == []


def test_resposta_automatica_nunca_vai_pra_corretora(mesa, s, linha_corretor):
    """O WhatsApp da pessoa respondeu sozinho 2 s depois do disparo: o card
    fica em Disparado e ninguém recebe lead de secretária eletrônica."""
    tel, lid = disparado(s, "612", ha_minutos=0)
    chega(mesa, tel, "Olá! Agradecemos seu contato, em breve retornaremos.",
          quando=time.time() + 2)
    etapa, dono, marca, _ = linha_do(s, lid)
    assert (etapa, dono, marca) == (cb.STAGE_DISPARADO, CORRETOR, None)
    assert eventos(s, lid, hr.EVENTO_TRANSFERENCIA) == []
    assert linha_corretor == [], "a Pessoa atendeu uma resposta automática"


def test_mensagem_velha_nao_transfere(mesa, s, linha_corretor):
    """Reentrega da Meta com mais de 30 min: o card anda, a automação não."""
    tel, lid = disparado(s, "613")
    r = mesa.post("/cloud/corretora/corretor/inbound",
                  headers={"X-Cloud-Secret": SEGREDO, "X-Cloud-Velho": "1"},
                  json={"messages": [{"from": tel, "id": "wamid.velha", "type": "text",
                                      "timestamp": str(int(time.time())),
                                      "text": {"body": "bom dia"}}]})
    assert r.status_code == 200
    etapa, dono, _, _ = linha_do(s, lid)
    assert (etapa, dono) == (cb.STAGE_RESPONDEU, CORRETOR)


def test_modo_so_interesse_escolhido_na_tela(mesa, s, linha_corretor, monkeypatch):
    """A outra opção do controle: só quem clicou no botão ou mostrou interesse.
    Quem decide é o atendimento (`handoff_remarketing.processar`), como era."""
    assert salvar_responde(mesa, handoff_remarketing_quem_vai="interesse",
                           handoff_remarketing_roleta=[CORRETOR]).status_code == 200

    tel, lid = disparado(s, "621")
    chega(mesa, tel, "Bom dia")
    assert linha_do(s, lid)[:2] == (cb.STAGE_RESPONDEU, CORRETOR), \
        "no modo interesse, 'bom dia' não vai pra corretora"
    assert hr.processar_resposta(tel, "Bom dia")["motivo"] == "modo_interesse"

    # com interesse lido, o atendimento transfere — pela MESMA roleta e etapa
    res = hr.processar(tel, ["Bom dia", "quero um apartamento de 2 quartos"])
    assert res["transferiu"] is True and res["para"] == CORRETOR
    etapa, dono, marca, _ = linha_do(s, lid)
    assert (etapa, dono) == (NOVO, CORRETOR) and marca is not None
    assert eventos(s, lid, hr.EVENTO_TRANSFERENCIA)[0]["quem_vai"] == "interesse"

    # e voltando pra "todos" na tela, o próximo "bom dia" vai
    assert salvar_responde(mesa, handoff_remarketing_quem_vai="todos").status_code == 200
    tel2, lid2 = disparado(s, "622")
    chega(mesa, tel2, "Bom dia")
    assert linha_do(s, lid2)[:2] == (NOVO, CORRETOR)


def test_etapa_de_entrada_escolhida_na_tela(mesa, s, linha_corretor):
    tela(mesa, "handoff_remarketing_roleta", [CORRETOR])
    tel, lid = disparado(s, "631")
    chega(mesa, tel)
    assert linha_do(s, lid)[0] == NOVO          # padrão: "Novo" do funil principal

    assert salvar_responde(mesa, handoff_remarketing_etapa=EM_ATENDIMENTO).status_code == 200
    tel2, lid2 = disparado(s, "632")
    chega(mesa, tel2)
    assert linha_do(s, lid2)[:2] == (EM_ATENDIMENTO, CORRETOR)
    assert eventos(s, lid2, hr.EVENTO_TRANSFERENCIA)[0]["etapa_nome"] == "Em atendimento"

    # escolher a padrão de volta apaga a chave
    assert salvar_responde(mesa, handoff_remarketing_etapa=NOVO).status_code == 200
    assert s.execute(_t("SELECT count(*) FROM config_operacional "
                        "WHERE chave = 'handoff_remarketing_etapa'")).scalar() == 0


def test_a_tela_so_aceita_etapa_valida_do_funil_de_vendas(mesa, s):
    s.execute(_t("INSERT INTO pipeline_stages (id, tenant_id, nome, funil, ordem) "
                 "VALUES (:i, 2, 'Novo', 'principal', 1)"), {"i": ETAPA_DE_OUTRO_CLIENTE})
    s.commit()
    for etapa, trecho in (
        (ETAPA_DE_OUTRO_CLIENTE, "outro cliente"),
        (cb.STAGE_RESPONDEU, "não é do funil de vendas"),
        (VENDA_PERDIDA, "fechamento"),
        (987654, "não existe"),
        ("6", "opção da lista"),
    ):
        r = salvar_responde(mesa, handoff_remarketing_etapa=etapa)
        assert r.status_code == 422, (etapa, r.text)
        assert trecho in r.json()["erros"]["handoff_remarketing_etapa"], r.text
    assert s.execute(_t("SELECT count(*) FROM config_operacional "
                        "WHERE chave = 'handoff_remarketing_etapa'")).scalar() == 0


@pytest.mark.parametrize("como", ["apagada", "de_outro_cliente"])
def test_etapa_apagada_ou_de_outro_cliente_nao_transfere(mesa, s, linha_corretor, como):
    """A etapa serviu quando foi gravada e deixou de servir: não transfere, o
    card fica em Respondeu, e o motivo aparece no resultado e na tela."""
    tela(mesa, "handoff_remarketing_roleta", [CORRETOR])
    s.execute(_t("INSERT INTO pipeline_stages (id, tenant_id, nome, funil, ordem) "
                 "VALUES (:i, :t, 'Triagem', 'principal', 3)"), {"i": ETAPA_SOME, "t": TENANT})
    s.commit()
    assert salvar_responde(mesa, handoff_remarketing_etapa=ETAPA_SOME).status_code == 200
    if como == "apagada":
        s.execute(_t("DELETE FROM pipeline_stages WHERE id = :i"), {"i": ETAPA_SOME})
    else:
        s.execute(_t("UPDATE pipeline_stages SET tenant_id = 2 WHERE id = :i"),
                  {"i": ETAPA_SOME})
    s.commit()

    tel, lid = disparado(s, "641")
    chega(mesa, tel)

    assert linha_do(s, lid)[:3] == (cb.STAGE_RESPONDEU, CORRETOR, None)
    assert eventos(s, lid, hr.EVENTO_TRANSFERENCIA) == []
    res = hr.processar_resposta(tel, "bom dia")
    assert res["transferiu"] is False and res["motivo"] == "etapa_invalida"
    mapa = mesa.get("/painel/automacoes", headers=ADMIN).json()["carrossel_corretor"]
    assert mapa["responde"]["config"]["motivo"] == "etapa_invalida"
    assert "ninguém é transferido" in mapa["quando_responde"][0]["o_que"]


def test_roleta_do_remarketing_editada_na_tela(mesa, s, linha_corretor):
    """A roleta é a da tela, em rodízio, e NÃO é a do Meta."""
    tela(mesa, "roleta_corretoras", [CORRETOR])              # a do Meta: só a Pessoa10
    assert salvar_responde(mesa, handoff_remarketing_roleta=[CORRETOR, CORRETOR]).status_code == 200

    donos = []
    for i in range(4):
        tel, lid = disparado(s, f"65{i}")
        chega(mesa, tel)
        donos.append(linha_do(s, lid)[1])
    assert donos == [CORRETOR, CORRETOR, CORRETOR, CORRETOR]
    assert CORRETOR not in donos, "lead do remarketing caiu na roleta do Meta"

    # desmarcou a Pessoa11 na tela: a próxima já não vai pra ela
    assert salvar_responde(mesa, handoff_remarketing_roleta=[CORRETOR]).status_code == 200
    tel, lid = disparado(s, "659")
    chega(mesa, tel)
    assert linha_do(s, lid)[1] == CORRETOR


def test_roleta_vazia_nao_transfere(mesa, s, linha_corretor):
    """Lista vazia = ninguém recebe: o card fica em Respondeu e a tela avisa.
    Não existe mais destino de reserva."""
    assert salvar_responde(mesa, handoff_remarketing_roleta=[]).status_code == 200

    tel, lid = disparado(s, "661")
    chega(mesa, tel)

    assert linha_do(s, lid)[:3] == (cb.STAGE_RESPONDEU, CORRETOR, None)
    assert eventos(s, lid, hr.EVENTO_TRANSFERENCIA) == []
    assert hr.processar_resposta(tel, "oi")["motivo"] == "roleta_vazia"
    cfg_tela = mesa.get("/painel/automacoes", headers=ADMIN).json()["carrossel_corretor"]["responde"]["config"]
    assert cfg_tela["motivo"] == "roleta_vazia"
    assert cfg_tela["em_respondeu"] >= 1
    assert cfg_tela["roleta"]["da_vez"] is None


def test_a_tela_recusa_roleta_com_gente_que_nao_pode(mesa, s):
    for roleta, trecho in (
        ([CORRETOR], "Corretor"),
        ([987], "não existe"),
        ([CORRETOR, CORRETOR], "repetida"),
        (["1"], "inválida"),
        ("1,2", "lista"),
    ):
        r = salvar_responde(mesa, handoff_remarketing_roleta=roleta)
        assert r.status_code == 422, (roleta, r.text)
        assert trecho in r.json()["erros"]["handoff_remarketing_roleta"], r.text
    s.execute(_t("UPDATE corretoras SET ativo = FALSE WHERE id = :i"), {"i": CORRETOR})
    s.commit()
    r = salvar_responde(mesa, handoff_remarketing_roleta=[CORRETOR])
    assert r.status_code == 422 and "inativa" in r.json()["detail"]


def test_corretora_inativada_depois_sai_da_roda_sem_parar_as_outras(mesa, s, linha_corretor):
    assert salvar_responde(mesa, handoff_remarketing_roleta=[CORRETOR, CORRETOR]).status_code == 200
    s.execute(_t("UPDATE corretoras SET ativo = FALSE WHERE id = :i"), {"i": CORRETOR})
    s.commit()
    tel, lid = disparado(s, "671")
    chega(mesa, tel)
    assert linha_do(s, lid)[1] == CORRETOR

    s.execute(_t("UPDATE corretoras SET ativo = FALSE WHERE id = :i"), {"i": CORRETOR})
    s.commit()
    tel2, lid2 = disparado(s, "672")
    chega(mesa, tel2)
    assert linha_do(s, lid2)[:2] == (cb.STAGE_RESPONDEU, CORRETOR)
    assert hr.processar_resposta(tel2)["motivo"] == "roleta_sem_ninguem_valido"


def test_tag_roleta_e_etapa_na_mesma_transacao(mesa, s, linha_corretor, monkeypatch):
    """Se qualquer parte falhar, NENHUMA fica: não existe card com dono novo
    sem a tag, nem tag sem etapa."""
    from app.services import lead_transferencia as lt

    tela(mesa, "handoff_remarketing_roleta", [CORRETOR])
    tel, lid = disparado(s, "681")

    def _explode(*a, **k):
        raise RuntimeError("falhou gravando o evento")

    monkeypatch.setattr(lt, "_registrar_evento", _explode)
    chega(mesa, tel)
    assert linha_do(s, lid)[:3] == (cb.STAGE_RESPONDEU, CORRETOR, None)
    assert not linha_do(s, lid)[3]
    assert eventos(s, lid, hr.EVENTO_TRANSFERENCIA) == []

    monkeypatch.undo()
    # (a fixture da linha foi desfeita junto: chama o handoff direto)
    res = hr.processar_resposta(tel, "bom dia")
    assert res["transferiu"] is True
    etapa, dono, marca, _ = linha_do(s, lid)
    assert (etapa, dono) == (NOVO, CORRETOR) and marca is not None
    # contrato de código: no caminho da transferência, um commit só, depois de
    # dono + etapa + tag + evento. (O caminho "segurado por conversa no
    # celular" tem o seu, antes, e volta sem transferir ninguém.)
    fonte = inspect.getsource(hr._transferir)
    caminho = fonte[fonte.index("feitas: list[Transferencia]"):]
    assert caminho.count("s.commit()") == 1
    assert caminho.index("transferir_lead(") < caminho.index("s.commit()")
    assert "marcar_remarketing=True" in fonte and "stage_para=cfg.etapa" in fonte


def test_banco_fora_nao_transfere_pelo_padrao(mesa, s, linha_corretor, monkeypatch):
    """Não consegui ler ≠ vale o padrão: o card fica em Respondeu."""
    tela(mesa, "handoff_remarketing_roleta", [CORRETOR])
    tel, lid = disparado(s, "691")

    def _fora():
        raise cfg.ConfigIndisponivel("connection refused")

    monkeypatch.setattr(cfg, "recarregar_estrito", _fora)
    chega(mesa, tel)
    assert linha_do(s, lid)[:3] == (cb.STAGE_RESPONDEU, CORRETOR, None)
    assert hr.processar_resposta(tel)["motivo"] == "config_indisponivel"
    assert hr.processar(tel, ["quero um apartamento"])["motivo"] == "config_indisponivel"


def test_valor_torto_gravado_por_fora_nao_transfere(mesa, s, linha_corretor):
    import json

    for n, (chave, valor, motivo) in enumerate((
        ("handoff_remarketing_quem_vai", "qualquer um", "quem_vai_invalido"),
        ("handoff_remarketing_roleta", "1,2,3", "roleta_invalida"),
        ("handoff_remarketing_etapa", "seis", "etapa_invalida"),
    )):
        s.execute(_t("DELETE FROM config_operacional WHERE chave LIKE 'handoff_remarketing_%'"))
        s.execute(_t("INSERT INTO config_operacional (chave, valor, atualizado_em, atualizado_por) "
                     "VALUES (:k, CAST(:v AS jsonb), now(), 'sql na mão')"),
                  {"k": chave, "v": json.dumps(valor)})
        s.commit()
        tel, lid = disparado(s, f"70{n}")
        chega(mesa, tel)
        assert linha_do(s, lid)[:2] == (cb.STAGE_RESPONDEU, CORRETOR), chave
        assert hr.processar_resposta(tel)["motivo"] == motivo, chave


def test_salvar_o_bloco_e_tudo_ou_nada(mesa, s):
    r = salvar_responde(mesa, handoff_remarketing_quem_vai="interesse",
                        handoff_remarketing_roleta=[CORRETOR],
                        handoff_remarketing_avisa_lead="sim")
    assert r.status_code == 422
    assert set(r.json()["erros"]) == {"handoff_remarketing_roleta",
                                      "handoff_remarketing_avisa_lead"}
    assert s.execute(_t("SELECT count(*) FROM config_operacional "
                        "WHERE chave LIKE 'handoff_remarketing_%'")).scalar() == 0
    assert salvar_responde(mesa).status_code == 422
    assert mesa.put("/painel/automacoes/remarketing/quando-responde",
                    json={"handoff_remarketing_roleta": [CORRETOR]},
                    headers={"X-Painel-User": "corretor"}).status_code == 403


# ── O aviso ao lead ──────────────────────────────────────────────────────────


def _avisos(s, tel: str) -> list[tuple]:
    return s.execute(_t(
        """SELECT corretora_id, tipo_mensagem, conteudo_texto
             FROM mensagens_whatsapp
            WHERE contato_telefone LIKE :p AND conteudo_metadata->>'origem' = :o
            ORDER BY id"""), {"p": f"%{tel[-8:]}", "o": hr.ORIGEM_AVISO}).all()


def test_aviso_sai_com_o_texto_da_dona_pela_linha_de_quem_recebeu(mesa, s, linha_corretor):
    assert salvar_responde(mesa, handoff_remarketing_roleta=[CORRETOR],
                           handoff_remarketing_avisa_lead=True).status_code == 200
    tel, lid = disparado(s, "801")
    chega(mesa, tel, "Bom dia")

    assert linha_do(s, lid)[1] == CORRETOR
    (aviso,) = _avisos(s, tel)
    assert aviso[0] == CORRETOR and aviso[1] == "template"
    # nome PÚBLICO (a Pessoa3 se apresenta como Pessoa), sem repetir o "Olá"
    assert ("Sou a Pessoa, da Acme Imóveis. Estávamos falando em outro número, "
            "vamos continuar o atendimento por aqui!") in aviso[2]
    assert "você me disse que" not in aviso[2]      # o resumo saiu do padrão


def test_aviso_desligado_na_tela_nao_sai(mesa, s, linha_corretor):
    assert salvar_responde(mesa, handoff_remarketing_roleta=[CORRETOR],
                           handoff_remarketing_avisa_lead=True).status_code == 200
    assert salvar_responde(mesa, handoff_remarketing_avisa_lead=False).status_code == 200
    tel, lid = disparado(s, "811")
    chega(mesa, tel)
    assert linha_do(s, lid)[1] == CORRETOR                # transferiu
    assert _avisos(s, tel) == []                    # e não avisou


def test_texto_do_aviso_editado_na_tela(mesa, s, linha_corretor):
    novo = "Aqui é a {corretora}, da Acme. A gente se falou pelo outro número, sigo com você por aqui."
    r = salvar_responde(mesa, handoff_remarketing_roleta=[CORRETOR],
                        handoff_remarketing_avisa_lead=True,
                        handoff_remarketing_aviso_texto=novo)
    assert r.status_code == 200, r.text
    aviso = r.json()["responde"]["config"]["aviso"]
    assert aviso["texto"] == novo and aviso["personalizado"] is True
    assert aviso["previa"] == (
        "Olá João, tudo bem?\n\nAqui é a Pessoa, da Acme. A gente se falou pelo "
        "outro número, sigo com você por aqui.\n\nTô por aqui se precisar 🙂")

    tel, _ = disparado(s, "821")
    chega(mesa, tel)
    (enviado,) = _avisos(s, tel)
    assert "Aqui é a Pessoa, da Acme. A gente se falou pelo outro número" in enviado[2]

    # histórico com autor, antes e depois
    (h,) = cfg.historico(["handoff_remarketing_aviso_texto"], limite=5)
    assert (h["antes"], h["depois"], h["autor"]) == (None, novo, "dona")

    # voltar ao padrão: `null` apaga a linha, o histórico fica
    assert salvar_responde(mesa, handoff_remarketing_aviso_texto=None).status_code == 200
    assert qr.modelo_do_aviso() == qr.AVISO_PADRAO
    assert len(cfg.historico(["handoff_remarketing_aviso_texto"], limite=5)) == 2


@pytest.mark.parametrize("texto,trecho", [
    ("Sou a {corretora}. Oi {nome}!", "só existe {corretora}"),
    ("x" * (qr.AVISO_MAX_CHARS + 1), "longo demais"),
    ("Sou a {corretora}​", "invisível"),
    (42, "precisa ser texto"),
])
def test_texto_do_aviso_invalido_nao_e_gravado(mesa, s, texto, trecho):
    r = salvar_responde(mesa, handoff_remarketing_aviso_texto=texto)
    assert r.status_code == 422, r.text
    assert trecho in r.json()["erros"]["handoff_remarketing_aviso_texto"]
    assert s.execute(_t("SELECT count(*) FROM config_operacional "
                        "WHERE chave = 'handoff_remarketing_aviso_texto'")).scalar() == 0


def test_aviso_gravado_por_fora_invalido_transfere_mas_nao_avisa(mesa, s, linha_corretor):
    """O lead vai pra corretora (o card não fica refém do texto), mas a
    mensagem que a tela teria recusado não sai — e o motivo fica no resultado."""
    assert salvar_responde(mesa, handoff_remarketing_roleta=[CORRETOR],
                           handoff_remarketing_avisa_lead=True).status_code == 200
    s.execute(_t("INSERT INTO config_operacional (chave, valor, atualizado_em, atualizado_por) "
                 "VALUES ('handoff_remarketing_aviso_texto', '\"Oi {primeiro_nome}!\"'::jsonb, "
                 "now(), 'sql na mão')"))
    s.commit()
    tel, lid = disparado(s, "831")
    cb.processar_inbound(tel, "Bom dia", TENANT, tipo="text",
                         recebido_em=datetime.now(timezone.utc))
    res = hr.processar_resposta(tel, "Bom dia")
    assert res["transferiu"] is True and linha_do(s, lid)[1] == CORRETOR
    assert res["aviso"]["ok"] is False and res["aviso"]["motivo"] == "aviso_invalido"
    assert _avisos(s, tel) == []
    aviso = mesa.get("/painel/automacoes", headers=ADMIN).json()["carrossel_corretor"]["responde"]["config"]["aviso"]
    assert aviso["problema"]


def test_resumo_da_conversa_e_opcao_desligada(mesa, s, linha_corretor):
    """"Você me disse que…" saiu do padrão; continua existindo, desligado."""
    assert salvar_responde(mesa, handoff_remarketing_roleta=[CORRETOR],
                           handoff_remarketing_avisa_lead=True,
                           handoff_remarketing_aviso_resumo=True).status_code == 200
    tel, _ = disparado(s, "841")
    chega(mesa, tel, "é pra investimento, pode mandar")
    (aviso,) = _avisos(s, tel)
    assert "você me disse que é pra investimento" in aviso[2]


def test_o_tick_do_aviso_olha_a_etapa_de_entrada_da_tela():
    from app.services import aviso_remarketing

    fonte = inspect.getsource(aviso_remarketing.rodar)
    assert "quando_responde.ler(s, tenant).etapa" in fonte
    assert '"novo": STAGE_NOVO' not in fonte


# ── Vai com histórico ────────────────────────────────────────────────────────


def test_o_card_chega_na_corretora_com_a_conversa_inteira(mesa, s, linha_corretor):
    """Padrão da casa: lead transferido leva o histórico. A corretora que
    recebeu abre o card e vê o disparo, o que a pessoa respondeu, o que a
    Pessoa disse e o que foi escrito pelo celular da linha da Corretor — cada trecho
    marcado com a linha por onde passou. E os eventos do card continuam nele."""
    # A conversa tem uma fala do celular da linha (h4): com a trava "não
    # transferir quem já está em conversa pelo celular" ligada, o card ficaria
    # com a Corretor (provado na seção E). Aqui o assunto é o histórico.
    assert salvar_responde(mesa, handoff_remarketing_roleta=[CORRETOR],
                           handoff_remarketing_avisa_lead=True,
                           handoff_remarketing_respeita_humano=False).status_code == 200
    tel, lid = disparado(s, "901")
    agora = datetime.now(timezone.utc)

    def grava(wamid, texto, de_corretora, minutos, tipo="text", origem=None):
        import json

        s.execute(_t(
            """INSERT INTO mensagens_whatsapp
                 (tenant_id, evolution_message_id, remote_jid, contato_telefone,
                  corretora_id, instance, from_corretora, tipo_mensagem,
                  conteudo_texto, timestamp_evolution, conteudo_metadata)
               VALUES (1, :w, :jid, :tel, :cid, 'acme_corretor', :fc, :tp, :tx, :ts,
                       CAST(:meta AS jsonb))"""),
            {"w": wamid, "jid": f"{tel}@s.whatsapp.net", "tel": tel, "cid": CORRETOR,
             "fc": de_corretora, "tp": tipo, "tx": texto,
             "ts": agora - timedelta(minutes=minutos),
             "meta": json.dumps({"origem": origem} if origem else {})})

    grava("wamid.h1", "[reengajamento: " + cc.TEXTO_DIRETA + "]", True, 50,
          tipo="template", origem="carrossel")
    grava("wamid.h2", "oi, bom dia", False, 40)
    grava("wamid.h3", "Oi! Você ainda está procurando imóvel em Cidade?", True, 39,
          origem="pessoa")
    grava("wamid.h4", "Aqui é do escritório, já te chamo.", True, 30)      # celular
    s.commit()

    chega(mesa, tel, "pode mandar sim")
    assert linha_do(s, lid)[:2] == (NOVO, CORRETOR)

    # a corretora que recebeu abre o card
    r = mesa.get(f"/painel/conversa?lead_id={lid}", headers={"X-Painel-User": "corretor"})
    assert r.status_code == 200, r.text
    corpo = r.json()
    textos = [m["text"] for m in corpo["mensagens"]]
    assert cc.TEXTO_DIRETA in textos                                  # o disparo
    assert "oi, bom dia" in textos and "pode mandar sim" in textos     # a pessoa
    assert "Oi! Você ainda está procurando imóvel em Cidade?" in textos   # a Pessoa
    assert "Aqui é do escritório, já te chamo." in textos              # o celular
    assert any("Estávamos falando em outro número" in t for t in textos)   # o aviso
    assert corpo["total"] >= 6
    por_texto = {m["text"]: m for m in corpo["mensagens"]}
    # cada trecho marcado com a linha por onde passou: a conversa antiga é da
    # linha da Corretor, o aviso é da linha de quem recebeu
    assert "corretor" in por_texto["oi, bom dia"]["linha"].lower()
    aviso = next(m for m in corpo["mensagens"] if "Estávamos falando" in m["text"])
    assert "corretor" not in aviso["linha"].lower()
    assert corpo["tag_remarketing"] == "RMKT" and corpo["veio_remarketing_em"]
    assert corpo["corretora_id"] == CORRETOR

    # outra corretora NÃO abre (cada uma vê o seu)
    assert mesa.get(f"/painel/conversa?lead_id={lid}",
                    headers={"X-Painel-User": "corretor"}).status_code == 403

    # os eventos de antes continuam no mesmo card
    tipos = {r[0] for r in s.execute(
        _t("SELECT tipo FROM lead_events WHERE lead_id = :i"), {"i": lid}).all()}
    assert {cb.EVENT_DISPARO, cb.EVENT_RESPOSTA, hr.EVENTO_TRANSFERENCIA} <= tipos


def test_board_e_card_mostram_rmkt(mesa, s, linha_corretor):
    tela(mesa, "handoff_remarketing_roleta", [CORRETOR])
    tel, lid = disparado(s, "911")
    chega(mesa, tel)
    r = mesa.get("/painel/crm/board", headers=ADMIN)
    assert r.status_code == 200, r.text
    cards = [c for col in r.json()["colunas"] for c in col["cards"] if c["lead_id"] == lid]
    assert cards and cards[0]["tag_remarketing"] == "RMKT"
    assert cards[0]["veio_remarketing"] is True
    # quem JÁ tinha a marca antiga ("remarketing") passa a exibir RMKT sem migração
    antigo = lead(s, "912", NOVO, dono=CORRETOR,
                  veio_remarketing_em=datetime(2026, 9, 15, tzinfo=timezone.utc))
    cards = [c for col in mesa.get("/painel/crm/board", headers=ADMIN).json()["colunas"]
             for c in col["cards"] if c["lead_id"] == antigo]
    assert cards[0]["tag_remarketing"] == "RMKT"
    assert cards[0]["veio_remarketing_em"].startswith("2026-09-15")


# ═════════════════════════════════════════════════════════════════════════════
# C. LEAD COM TAG RMKT NUNCA CAI NA ROLETA DO META
# ═════════════════════════════════════════════════════════════════════════════


@pytest.fixture()
def roletas(mesa, s):
    """Meta = só a Pessoa10. Remarketing = só a Pessoa12. Dá pra ver qual foi usada."""
    tela(mesa, "roleta_corretoras", [CORRETOR])
    tela(mesa, "handoff_remarketing_roleta", [CORRETOR])
    tela(mesa, "lead_intake_ativo", True)
    return mesa


def _intake(tel: str):
    enviados: list = []
    return lead_intake.intake_lead(
        nome="Fulano Meta", telefone=tel, origem="meta_leadgen",
        sender=lambda *a, **k: enviados.append(a) or {"ok": True, "dry_run": True},
    )


def test_intake_do_meta_card_com_rmkt_sem_dono_usa_a_roleta_do_remarketing(roletas, s):
    marcado = lead(s, "951", NOVO, dono=None,
                   veio_remarketing_em=datetime.now(timezone.utc))
    r = _intake(tel_de(s, marcado))
    assert r is not None and r.ja_existia is True
    assert linha_do(s, marcado)[1] == CORRETOR, "card RMKT sem dono caiu na roleta do Meta"

    # controle: card SEM a tag, sem dono, segue na roleta do Meta
    comum = lead(s, "952", NOVO, dono=None)
    _intake(tel_de(s, comum))
    assert linha_do(s, comum)[1] == CORRETOR


def test_intake_do_meta_card_com_rmkt_e_roleta_vazia_fica_onde_esta(roletas, s):
    tela(roletas, "handoff_remarketing_roleta", [])
    marcado = lead(s, "953", NOVO, dono=None,
                   veio_remarketing_em=datetime.now(timezone.utc))
    _intake(tel_de(s, marcado))
    assert linha_do(s, marcado)[1] is None, "sem roleta do remarketing o card não ganha dono do Meta"


def test_intake_do_meta_card_com_rmkt_e_dono_nao_e_remexido(roletas, s):
    marcado = lead(s, "954", EM_ATENDIMENTO, dono=CORRETOR,
                   veio_remarketing_em=datetime.now(timezone.utc))
    _intake(tel_de(s, marcado))
    assert linha_do(s, marcado)[:2] == (EM_ATENDIMENTO, CORRETOR)


def test_so_existe_uma_porta_pra_roleta_do_meta():
    """Estrutural: em todo o `app/`, só `_dono_pela_roleta` chama a roleta do
    Meta — e ele desvia o card com a tag antes. Caminho novo que chame a
    roleta direto derruba este teste."""
    raiz = pathlib.Path(lead_intake.__file__).resolve().parents[1]
    chamadas = []
    for arq in raiz.rglob("*.py"):
        for n, linha in enumerate(arq.read_text(encoding="utf-8").splitlines(), 1):
            if "_proxima_corretora_roleta(" in linha and "def _proxima_corretora_roleta" not in linha:
                chamadas.append(f"{arq.relative_to(raiz)}:{n}")
    assert len(chamadas) == 1, chamadas
    assert chamadas[0].startswith("services/lead_intake.py")
    fonte = inspect.getsource(lead_intake._dono_pela_roleta)
    assert fonte.index("veio_remarketing_em is not None") < fonte.index(
        "_proxima_corretora_roleta(")
    assert "dono_para_lead_marcado" in fonte


def test_promocao_da_pessoa_nao_mexe_em_card_com_rmkt(roletas, s, monkeypatch):
    """Quem já saiu pela roleta do remarketing não é puxado de volta nem
    mandado pra outra pessoa pela promoção da Pessoa; e ela nunca consulta a
    roleta do Meta (o destino dela é fixo: a Pessoa3, que está FORA da
    roleta do Meta)."""
    def _proibido(*a, **k):
        raise AssertionError("a promoção da Pessoa consultou a roleta do Meta")

    monkeypatch.setattr(lead_intake, "_proxima_corretora_roleta", _proibido)
    monkeypatch.setattr(cfg, "roleta_corretoras", _proibido)
    marcado = lead(s, "961", NOVO, dono=CORRETOR,
                   veio_remarketing_em=datetime.now(timezone.utc))
    assert ab.promover_para_vendas(tel_de(s, marcado), "Fulano") is False
    assert linha_do(s, marcado)[:2] == (NOVO, CORRETOR)

    # card ainda no funil de remarketing (a roleta não o levou): vai pra Pessoa12,
    # nunca pra roleta do Meta, e ganha a tag
    na_roda = lead(s, "962", cb.STAGE_RESPONDEU)
    assert ab.promover_para_vendas(tel_de(s, na_roda), "Fulano") is True
    etapa, dono, marca, _ = linha_do(s, na_roda)
    assert dono == CORRETOR and dono != CORRETOR and marca is not None


def test_transferencia_manual_mantem_a_tag_e_nao_passa_por_roleta(roletas, s):
    marcado = lead(s, "971", NOVO, dono=CORRETOR,
                   veio_remarketing_em=datetime(2026, 10, 1, tzinfo=timezone.utc))
    r = roletas.post("/painel/crm/transferir", headers=ADMIN,
                     json={"lead_id": marcado, "corretora_destino": CORRETOR,
                           "motivo": "teste"})
    assert r.status_code == 200, r.text
    etapa, dono, marca, manual = linha_do(s, marcado)
    assert (etapa, dono, manual) == (NOVO, CORRETOR, True)
    assert marca is not None and marca.date().isoformat() == "2026-10-01"


def test_sync_nao_devolve_card_com_rmkt_pra_dono_antigo(roletas, s, monkeypatch):
    """O espelho do Kommo não desfaz a transferência (`owner_editado_manual`)."""
    from app.config import settings
    from app.services import lead_spine

    monkeypatch.setattr(settings, "lead_spine_shadow", True, raising=False)
    tel, lid = disparado(s, "981")
    s.execute(_t("UPDATE leads SET external_ref = 'copyteste981' WHERE id = :i"), {"i": lid})
    s.commit()
    cb.processar_inbound(tel, "Bom dia", TENANT, tipo="text",
                         recebido_em=datetime.now(timezone.utc))
    assert hr.processar_resposta(tel, "Bom dia")["transferiu"] is True
    assert linha_do(s, lid)[1] == CORRETOR

    lead_spine.upsert_lead_shadow({"lead_id": "copyteste981", "nome": "Fulano",
                                   "telefone": tel, "corretora_id": CORRETOR})
    assert linha_do(s, lid)[1] == CORRETOR, "o sync devolveu o card do remarketing pra outra dona"


# ═════════════════════════════════════════════════════════════════════════════
# D. O QUE A TELA LÊ
# ═════════════════════════════════════════════════════════════════════════════


def test_o_mapa_e_o_desenho_saem_da_configuracao(mesa, s, linha_corretor):
    tela(mesa, "roleta_corretoras", [CORRETOR, CORRETOR])
    assert salvar_responde(mesa, handoff_remarketing_roleta=[CORRETOR, CORRETOR],
                           handoff_remarketing_etapa=EM_ATENDIMENTO,
                           handoff_remarketing_avisa_lead=True).status_code == 200
    antes = {x["id"]: x["recebeu"] for x in mesa.get(
        "/painel/automacoes", headers=ADMIN).json()["carrossel_corretor"]["responde"]["config"]["roleta"]["corretoras"]}
    tel, lid = disparado(s, "991")
    chega(mesa, tel)
    primeira = linha_do(s, lid)[1]                      # quem estava na vez
    outra = CORRETOR if primeira == CORRETOR else CORRETOR

    car = mesa.get("/painel/automacoes", headers=ADMIN).json()["carrossel_corretor"]
    c = car["responde"]["config"]
    assert c["problema"] is None and c["tag"] == "RMKT"
    assert c["quem_vai"] == "todos"
    assert (c["etapa"]["valendo"], c["etapa"]["valendo_nome"]) == (EM_ATENDIMENTO, "Em atendimento")
    assert [o["id"] for o in c["etapa"]["opcoes"] if o["permitida"]] == [NOVO, EM_ATENDIMENTO]
    assert c["roleta"]["ids"] == [CORRETOR, CORRETOR]
    assert c["roleta"]["da_vez"] == outra               # a primeira acabou de receber
    recebeu = {x["id"]: x["recebeu"] for x in c["roleta"]["corretoras"]}
    assert recebeu[primeira] == antes[primeira] + 1
    assert recebeu[outra] == antes[outra]
    assert CORRETOR not in recebeu
    assert c["roleta_do_meta"]["ids"] == [CORRETOR, CORRETOR]
    assert c["aviso"]["ligado"] is True and "Estávamos falando em outro número" in c["aviso"]["previa"]

    passo = car["quando_responde"][0]["o_que"]
    assert "todo mundo que cai em Respondeu" in passo
    assert "RMKT" in passo and "'Em atendimento'" in passo
    for c_ in c["roleta"]["corretoras"]:
        assert (c_["nome"] in passo) == c_["na_roleta"], c_
    assert any("avisa" in p["o_que"] for p in car["quando_responde"])
    assert "Pessoa" in car["quando_responde"][-1]["quem"]

    # o histórico da tela enxerga as chaves novas, com autor
    chaves = {h["chave"] for h in car["historico"]["config"]}
    assert {"handoff_remarketing_roleta", "handoff_remarketing_etapa"} <= chaves


# ═════════════════════════════════════════════════════════════════════════════
# E. AS DUAS TRAVAS ANTES DE PUBLICAR (adendo da orquestradora, 05/10/2026)
#    1. A Pessoa se cala quando o card vai pra corretora.
#    2. Não tirar da mão de quem já está conversando pelo celular da linha.
# ═════════════════════════════════════════════════════════════════════════════


@pytest.fixture()
def pessoa(monkeypatch):
    """A Pessoa de verdade (`atendimento_corretor.atender`), com a IA e o envio
    trocados por dublês que anotam o que ela responderia."""
    from app.config import settings

    respostas: list[tuple[str, str]] = []
    monkeypatch.setattr(settings, "atendimento_corretor_ativo", True)
    monkeypatch.setattr(ab, "_rodar_pessoa", lambda tel, texto, nome, hist: "Oi! Aqui é a Pessoa.")
    monkeypatch.setattr(ab, "_responder", lambda tel, texto: respostas.append((tel, texto)))
    monkeypatch.setattr("app.services.aviso_resposta_disparo.avisar_em_background",
                        lambda tel: None)
    return respostas


def _grava_msg(s, tel: str, *, texto: str, de_corretora: bool, ha: timedelta,
               instance: str = "acme_corretor", corretora_id: int | None = CORRETOR,
               tipo: str = "text", meta: dict | None = None) -> None:
    import json

    s.execute(_t(
        """INSERT INTO mensagens_whatsapp
             (tenant_id, evolution_message_id, remote_jid, contato_telefone,
              corretora_id, instance, from_corretora, tipo_mensagem,
              conteudo_texto, timestamp_evolution, conteudo_metadata)
           VALUES (1, :w, :jid, :tel, :cid, :inst, :fc, :tp, :tx, :ts,
                   CAST(:meta AS jsonb))"""),
        {"w": f"wamid.trava.{tel[-4:]}.{time.time_ns()}", "jid": f"{tel}@s.whatsapp.net",
         "tel": tel, "cid": corretora_id, "inst": instance, "fc": de_corretora,
         "tp": tipo, "tx": texto, "ts": datetime.now(timezone.utc) - ha,
         "meta": json.dumps(meta or {})})
    s.commit()


def com_conversa_no_celular(s, sufixo: str, *, celular_ha=timedelta(days=2),
                            instance: str = "cloud_assistente", corretora_id: int | None = None):
    """Disparo há 5 dias e, depois dele, alguém da casa escrevendo pelo celular
    da linha — por padrão gravado como `cloud_assistente`, sem corretora, como as 485
    falas de 15/09 a 05/10 estavam (`INBOUND-CORRETOR.md`, item 2). Com
    `instance="acme_corretor", corretora_id=CORRETOR` é como elas ficaram depois de
    movidas em 06/10 (`MOVER-485-E-8-CARTOES.md`)."""
    tel, lid = disparado(s, sufixo, ha_minutos=5 * 24 * 60)
    _grava_msg(s, tel, texto="[reengajamento: " + cc.TEXTO_DIRETA + "]", de_corretora=True,
               ha=timedelta(days=5), tipo="template",
               meta={"template": "acme_corretor_ola_a_01", "origem": "carrossel"})
    _grava_msg(s, tel, texto="Oi! Tudo bem? Me fala o que você procura.",
               de_corretora=True, ha=celular_ha, instance=instance, corretora_id=corretora_id)
    return tel, lid


# ── 1. A Pessoa se cala quando o card vai pra corretora ──────────────────────


def test_transferido_nao_recebe_mais_a_pessoa(mesa, s, linha_corretor, pessoa):
    tela(mesa, "handoff_remarketing_roleta", [CORRETOR])
    tel, lid = disparado(s, "A01")
    chega(mesa, tel, "Bom dia")
    assert linha_do(s, lid)[:2] == (NOVO, CORRETOR)

    # a mensagem chega à Pessoa (o webhook entrega) e ela não responde
    r = _ATENDER_DE_VERDADE(tel, "Bom dia", "Fulano")
    assert (r["atendeu"], r["motivo"]) == (False, "transferido")
    # e continua calada no que a pessoa escrever depois no número da Corretor
    chega(mesa, tel, "vocês têm casa no Bairro A?")
    r = _ATENDER_DE_VERDADE(tel, "vocês têm casa no Bairro A?", "Fulano")
    assert r["motivo"] == "transferido"
    assert pessoa == []
    # o que ela escreveu ficou gravado e aparece no card de quem recebeu
    corpo = mesa.get(f"/painel/conversa?lead_id={lid}",
                     headers={"X-Painel-User": "corretor"}).json()
    assert "vocês têm casa no Bairro A?" in [m["text"] for m in corpo["mensagens"]]


def test_nao_transferido_continua_recebendo_a_pessoa(mesa, s, linha_corretor, pessoa):
    assert salvar_responde(mesa, handoff_remarketing_quem_vai="interesse").status_code == 200
    tel, lid = disparado(s, "A02")
    chega(mesa, tel, "Bom dia")
    assert linha_do(s, lid)[:2] == (cb.STAGE_RESPONDEU, CORRETOR)
    r = _ATENDER_DE_VERDADE(tel, "Bom dia", "Fulano")
    assert r["atendeu"] is True, r
    assert pessoa == [(tel, "Oi! Aqui é a Pessoa.")]


def test_transferido_na_mao_tambem_cala_a_pessoa(mesa, s, linha_corretor, pessoa):
    assert salvar_responde(mesa, handoff_remarketing_quem_vai="interesse").status_code == 200
    tel, lid = disparado(s, "A03")
    chega(mesa, tel, "Bom dia")
    r = mesa.post("/painel/crm/transferir", headers=ADMIN,
                  json={"lead_id": lid, "corretora_destino": CORRETOR})
    assert r.status_code == 200, r.text
    r = _ATENDER_DE_VERDADE(tel, "oi?", "Fulano")
    assert r["motivo"] == "transferido" and pessoa == []


def test_transferiu_no_proprio_turno_a_pessoa_nao_responde(mesa, s, linha_corretor, pessoa):
    """Modo "só interesse": quem transfere é o atendimento, no meio do turno.
    Transferiu, a Pessoa não fala nesse turno."""
    assert salvar_responde(mesa, handoff_remarketing_quem_vai="interesse",
                           handoff_remarketing_roleta=[CORRETOR]).status_code == 200
    tel, lid = disparado(s, "A04")
    chega(mesa, tel, "quero um apartamento de 2 quartos")
    r = _ATENDER_DE_VERDADE(tel, "quero um apartamento de 2 quartos", "Fulano")
    assert r["handoff"]["transferiu"] is True and r["motivo"] == "transferido"
    assert linha_do(s, lid)[1] == CORRETOR and pessoa == []


# ── 2. Não tirar da mão de quem já conversa pelo celular da linha ────────────


def test_a_trava_nasce_ligada():
    assert cfg.DEFAULTS["handoff_remarketing_respeita_humano"]() is True


def test_humano_falou_ha_dois_dias_nao_transfere_nem_avisa(mesa, s, linha_corretor):
    assert salvar_responde(mesa, handoff_remarketing_roleta=[CORRETOR],
                           handoff_remarketing_avisa_lead=True).status_code == 200
    tel, lid = com_conversa_no_celular(s, "B01")

    chega(mesa, tel, "Bom dia")

    etapa, dono, marca, manual = linha_do(s, lid)
    assert (etapa, dono, marca) == (cb.STAGE_RESPONDEU, CORRETOR, None), \
        "tirou da mão de quem estava conversando pelo celular"
    assert eventos(s, lid, hr.EVENTO_TRANSFERENCIA) == []
    assert _avisos(s, tel) == [], "avisou sozinho quem estava em conversa"
    (seg,) = eventos(s, lid, qr.EVENTO_SEGURADO)
    assert seg["motivo"] == "humano_no_comando" and seg["sinal"] == "respondeu"

    # a pessoa escreve de novo: continua segurado, sem evento repetido
    chega(mesa, tel, "alô?")
    assert linha_do(s, lid)[1] == CORRETOR
    # (e o handoff chamado de novo pra esta conversa não grava outro evento)
    assert hr.processar_resposta(tel, "alô?")["motivo"] == "humano_no_comando"
    assert len(eventos(s, lid, qr.EVENTO_SEGURADO)) == 1

    # visível: a tela conta, o card mostra o motivo
    assert qr.resumo(s, TENANT)["segurados_humano"] == 1
    assert qr.resumo(s, TENANT)["respeita_humano"] is True
    cards = [c for col in mesa.get("/painel/crm/remarketing/board", headers=ADMIN).json()["colunas"]
             for c in col["cards"] if c["lead_id"] == lid]
    assert cards[0]["handoff_segurado"]["motivo"] == "humano_no_comando"


def test_fala_do_celular_antes_do_disparo_nao_segura(mesa, s, linha_corretor):
    """O critério é o da Pessoa: conversa DESTA vez (depois do último disparo).
    Ter falado com a pessoa antes do disparo não segura nada."""
    tela(mesa, "handoff_remarketing_roleta", [CORRETOR])
    tel, lid = com_conversa_no_celular(s, "B02", celular_ha=timedelta(days=6))
    chega(mesa, tel, "Bom dia")
    assert linha_do(s, lid)[:2] == (NOVO, CORRETOR)


def test_pessoa_e_a_trava_usam_a_mesma_regua(mesa, s, pessoa):
    """Reaproveitada, não duplicada: o que cala a Pessoa é o que segura a
    transferência — e a fala `cloud_assistente` sem corretora conta pros dois."""
    tel, lid = com_conversa_no_celular(s, "B03")
    assert hr.humano_no_comando(tel) is True
    assert _ATENDER_DE_VERDADE(tel, "Bom dia", "Fulano")["motivo"] == "humano_falando"
    fonte = inspect.getsource(hr.humano_no_comando)
    assert "ab._humano_falando(" in fonte and "ab._inicio_da_conversa(" in fonte
    assert "instance" not in ab._SQL_HUMANO_FALOU


def test_chave_desligada_transfere_todo_mundo(mesa, s, linha_corretor):
    r = salvar_responde(mesa, handoff_remarketing_roleta=[CORRETOR],
                        handoff_remarketing_respeita_humano=False)
    assert r.status_code == 200, r.text
    assert r.json()["responde"]["config"]["respeita_humano"] is False
    tel, lid = com_conversa_no_celular(s, "B04")
    chega(mesa, tel, "Bom dia")
    etapa, dono, marca, _ = linha_do(s, lid)
    assert (etapa, dono) == (NOVO, CORRETOR) and marca is not None
    assert eventos(s, lid, qr.EVENTO_SEGURADO) == []


def test_transferencia_manual_do_segurado_leva_rmkt_e_historico(mesa, s, linha_corretor, pessoa):
    tela(mesa, "handoff_remarketing_roleta", [CORRETOR])
    tel, lid = com_conversa_no_celular(s, "B05")
    chega(mesa, tel, "Bom dia")
    assert linha_do(s, lid)[1] == CORRETOR

    r = mesa.post("/painel/crm/transferir", headers=ADMIN,
                  json={"lead_id": lid, "corretora_destino": CORRETOR, "motivo": "estava comigo"})
    assert r.status_code == 200, r.text
    etapa, dono, marca, manual = linha_do(s, lid)
    assert (etapa, dono, manual) == (cb.STAGE_RESPONDEU, CORRETOR, True)
    assert marca is not None, "transferência manual saindo da Corretor sem a tag RMKT"
    (ev,) = eventos(s, lid, hr.EVENTO_TRANSFERENCIA)
    assert (ev["para"], ev["tag"], ev["quem"]) == (CORRETOR, "RMKT", "dona")
    assert "handoff_remarketing" != ev["quem"]
    # saiu da conta da tela e do chip do card (o dono não é mais a Corretor)
    assert qr.resumo(s, TENANT)["segurados_humano"] == 0
    # o histórico vai junto: quem recebeu vê o disparo, a pessoa e o que o
    # celular da linha escreveu DEPOIS do conserto do inbound (17:48 de 05/10:
    # o eco entra como `acme_corretor`)...
    _grava_msg(s, tel, texto="Já te mando as opções.", de_corretora=True,
               ha=timedelta(minutes=1))
    corpo = mesa.get(f"/painel/conversa?lead_id={lid}",
                     headers={"X-Painel-User": "corretor"}).json()
    textos = [m["text"] for m in corpo["mensagens"]]
    assert cc.TEXTO_DIRETA in textos and "Bom dia" in textos
    assert "Já te mando as opções." in textos
    assert corpo["tag_remarketing"] == "RMKT"
    # ...mas NÃO as falas antigas gravadas como `cloud_assistente` (15/09–05/10): a
    # thread do card exclui essa instância de propósito (é a da conversa
    # Assistente↔Dona). Fala do celular gravada assim conta pra segurar a
    # transferência, mas não aparece pra quem recebe. Os 485 ecos de verdade
    # foram movidos pra `acme_corretor` em 06/10 (teste logo abaixo).
    assert "Oi! Tudo bem? Me fala o que você procura." not in textos
    # e a Pessoa não volta a falar na linha da Corretor
    assert _ATENDER_DE_VERDADE(tel, "oi", "Fulano")["motivo"] in ("transferido", "humano_falando")


def test_fala_do_celular_movida_pra_corretor_continua_segurando_e_aparece_no_card(
        mesa, s, linha_corretor, pessoa):
    """As 485 falas foram movidas de `cloud_assistente` pra `acme_corretor` (corretora
    Corretor) em 06/10. A trava "humano no comando" não filtra por instância: a
    Pessoa continua calada, a transferência continua segurada — e agora quem
    recebe o card na mão vê o que o celular escreveu."""
    tela(mesa, "handoff_remarketing_roleta", [CORRETOR])
    tel, lid = com_conversa_no_celular(s, "B07", instance="acme_corretor", corretora_id=CORRETOR)
    assert hr.humano_no_comando(tel) is True
    assert _ATENDER_DE_VERDADE(tel, "Bom dia", "Fulano")["motivo"] == "humano_falando"
    assert pessoa == []

    chega(mesa, tel, "Bom dia")
    assert linha_do(s, lid)[:2] == (cb.STAGE_RESPONDEU, CORRETOR), \
        "a fala movida deixou de segurar a transferência"
    (seg,) = eventos(s, lid, qr.EVENTO_SEGURADO)
    assert seg["motivo"] == "humano_no_comando"

    r = mesa.post("/painel/crm/transferir", headers=ADMIN,
                  json={"lead_id": lid, "corretora_destino": CORRETOR})
    assert r.status_code == 200, r.text
    corpo = mesa.get(f"/painel/conversa?lead_id={lid}",
                     headers={"X-Painel-User": "corretor"}).json()
    textos = [m["text"] for m in corpo["mensagens"]]
    assert "Oi! Tudo bem? Me fala o que você procura." in textos
    assert cc.TEXTO_DIRETA in textos and "Bom dia" in textos


def test_transferencia_manual_de_fora_do_remarketing_nao_inventa_tag(mesa, s):
    comum = lead(s, "B06", NOVO, dono=CORRETOR)
    r = mesa.post("/painel/crm/transferir", headers=ADMIN,
                  json={"lead_id": comum, "corretora_destino": CORRETOR})
    assert r.status_code == 200, r.text
    assert linha_do(s, comum)[2] is None


def test_valor_torto_da_trava_nao_transfere(mesa, s, linha_corretor):
    """Estrita como as outras: lixo gravado por fora não vira "desligado"."""
    tela(mesa, "handoff_remarketing_roleta", [CORRETOR])
    s.execute(_t("INSERT INTO config_operacional (chave, valor, atualizado_por) "
                 "VALUES ('handoff_remarketing_respeita_humano', '\"sim\"'::jsonb, 'fora') "
                 "ON CONFLICT (chave) DO UPDATE SET valor = EXCLUDED.valor"))
    s.commit()
    cfg.invalidar_cache()
    tel, lid = disparado(s, "B07")
    chega(mesa, tel, "Bom dia")
    assert linha_do(s, lid)[:2] == (cb.STAGE_RESPONDEU, CORRETOR)
    assert qr.resumo(s, TENANT)["motivo"] == "respeita_humano_invalido"
    assert mesa.put("/painel/automacoes/remarketing/quando-responde", headers=ADMIN,
                    json={"handoff_remarketing_respeita_humano": "sim"}).status_code == 422


# ── 3. Publicar não mexe em quem já está em Respondeu ────────────────────────


def test_publicar_nao_transfere_nem_avisa_quem_ja_esta_em_respondeu(mesa, s, linha_corretor):
    """Os 47 de hoje: a regra nova vale pra quem responder DEPOIS. Ler a tela,
    salvar o bloco, a volta do robô e a volta do aviso não transferem ninguém
    que já estava parado em Respondeu."""
    from app.services import aviso_remarketing

    parados = [lead(s, f"C0{i}", cb.STAGE_RESPONDEU) for i in range(3)]
    assert salvar_responde(mesa, handoff_remarketing_roleta=[CORRETOR, CORRETOR, CORRETOR],
                           handoff_remarketing_avisa_lead=True).status_code == 200
    assert mesa.get("/painel/automacoes", headers=ADMIN).status_code == 200
    volta(Remetente())                                   # disparo desligado
    aviso_remarketing.rodar(tenant=TENANT, dry_run=False)
    for lid in parados:
        assert linha_do(s, lid)[:3] == (cb.STAGE_RESPONDEU, CORRETOR, None)
        assert eventos(s, lid, hr.EVENTO_TRANSFERENCIA) == []
    assert qr.resumo(s, TENANT)["em_respondeu"] >= 3
