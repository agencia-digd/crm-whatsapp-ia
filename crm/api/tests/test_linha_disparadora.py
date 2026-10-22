"""A marca de disparador vale pro remarketing.

O cadastro de números do api-disparos tem `phone_numbers.papel`
(`disparo` / `atendimento` / `pausado`). Até 05/10/2026 o remarketing da Acme
ignorava: saía sempre pela linha cravada `corretor`.

O que estes testes seguram:

1. com UMA linha marcada `disparo`, e operável, o tick sai por ELA;
2. sem linha marcada (o estado de produção hoje: a da Corretor está `pausado`), o
   tick NÃO envia e diz o motivo — com fila cheia e tudo o mais liberado;
3. nunca cai de volta numa linha por adivinhação: api fora, chave ausente,
   duas marcadas, número que o copiloto não opera — tudo SEGURA;
4. `pausado` é dado que existe no banco: é lido sem quebrar e vale "não dispara";
5. a chave do api-disparos nunca aparece em mensagem de erro.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import carrossel_corretor as cb
from app.services import cloud_lines as cl
from app.services import config_operacional as cfg
from app.services import linha_disparadora as ld

ADMIN = {"X-Painel-User": "dona"}

PID_CORRETOR = "000000000000000"
PID_CORRETOR = "000000000000000"
PID_NOVO = "000000000000000"      # número cadastrado lá que o copiloto não opera

LINHA_CORRETOR = cl.CloudLine(phone_id=PID_CORRETOR, slug="corretor", papel="corretora",
                         env_file="", token_var="")
LINHA_CORRETOR = cl.CloudLine(phone_id=PID_CORRETOR, slug="corretor", papel="corretora",
                          env_file="", token_var="")


@pytest.fixture(autouse=True)
def linha_disparadora_de_teste():
    """Redefine a fixture do conftest: aqui o `resolver` é o REAL."""
    yield


@pytest.fixture(autouse=True)
def registro(monkeypatch):
    """O registry de linhas do copiloto, só com o que estes testes usam."""
    linhas = {PID_CORRETOR: LINHA_CORRETOR, PID_CORRETOR: LINHA_CORRETOR}
    monkeypatch.setattr(cl, "line_by_phone_id", lambda pid: linhas.get(str(pid or "")))


def _cadastro(monkeypatch, numeros):
    """O que o api-disparos responderia em GET /numbers/papel."""
    monkeypatch.setattr(ld, "_buscar_numeros", lambda: numeros)


def _n(pid, papel, phone="+5500000000000", nome=None):
    return {"phone_number_id": pid, "phone": phone, "display_name": nome, "papel": papel}


# ── resolver ─────────────────────────────────────────────────────────────────


def test_uma_linha_marcada_disparo_e_a_que_sai(monkeypatch):
    _cadastro(monkeypatch, [_n(PID_CORRETOR, "disparo"), _n(PID_CORRETOR, "atendimento")])
    r = ld.resolver()
    assert r.linha is LINHA_CORRETOR
    assert r.motivo is None


def test_linha_PAUSADA_nao_dispara_e_nao_quebra(monkeypatch):
    """O estado real de produção em 05/10: a linha da Corretor com papel='pausado'
    (posto à mão) e nenhuma `disparo`. Tem que ler sem erro e segurar."""
    _cadastro(monkeypatch, [_n(PID_CORRETOR, "pausado"), _n(PID_CORRETOR, "atendimento")])
    r = ld.resolver()
    assert r.linha is None
    assert r.motivo == "sem_linha_disparadora"
    assert "pausado" in r.detalhe          # diz o que achou, não só "não tem"
    assert [n["papel"] for n in r.numeros] == ["pausado", "atendimento"]


def test_nenhum_numero_cadastrado(monkeypatch):
    _cadastro(monkeypatch, [])
    r = ld.resolver()
    assert (r.linha, r.motivo) == (None, "sem_linha_disparadora")


def test_duas_marcadas_e_ambiguo_nao_escolhe(monkeypatch):
    _cadastro(monkeypatch, [_n(PID_CORRETOR, "disparo"), _n(PID_NOVO, "disparo")])
    r = ld.resolver()
    assert r.linha is None
    assert r.motivo == "linha_disparadora_ambigua"


def test_numero_marcado_que_o_copiloto_nao_opera(monkeypatch):
    """Número novo, cadastrado e marcado lá, sem token aqui: não dá pra enviar
    por ele — e NÃO se manda pela Corretor no lugar."""
    _cadastro(monkeypatch, [_n(PID_NOVO, "disparo"), _n(PID_CORRETOR, "pausado")])
    r = ld.resolver()
    assert r.linha is None
    assert r.motivo == "linha_disparadora_fora_do_registro"


def test_linha_de_corretora_marcada_como_disparo_e_recusada(monkeypatch):
    """Alguém marcou a linha pessoal da Pessoa10 como disparo. A resposta do lead
    lá não tira ninguém da roda — disparar por ela reenviaria em 30 dias pra
    quem respondeu, e queipessoa o número de uma pessoa."""
    _cadastro(monkeypatch, [_n(PID_CORRETOR, "disparo"), _n(PID_CORRETOR, "pausado")])
    r = ld.resolver()
    assert r.linha is None
    assert r.motivo == "linha_disparadora_sem_resposta_tratada"


def test_api_fora_segura(monkeypatch):
    def _cai():
        raise ld.Indisponivel("api-disparos não respondeu: ConnectError")
    monkeypatch.setattr(ld, "_buscar_numeros", _cai)
    r = ld.resolver()
    assert (r.linha, r.motivo) == (None, "linha_disparadora_indisponivel")


def test_erro_inesperado_tambem_segura(monkeypatch):
    def _explode():
        raise KeyError("numeros")
    monkeypatch.setattr(ld, "_buscar_numeros", _explode)
    r = ld.resolver()
    assert (r.linha, r.motivo) == (None, "linha_disparadora_indisponivel")


def test_na_suite_a_consulta_real_e_bloqueada():
    """Sem trocar `_buscar_numeros`, nada sai da máquina de teste."""
    r = ld.resolver()
    assert (r.linha, r.motivo) == (None, "linha_disparadora_indisponivel")


def test_chave_ausente_segura_e_nao_vaza(monkeypatch, tmp_path):
    from app.config import settings

    arq = tmp_path / "vazio.env"
    arq.write_text("OUTRA=1\n")
    monkeypatch.setattr(settings, "api_disparos_env_file", str(arq))
    with pytest.raises(ld.Indisponivel, match="ausente"):
        ld._chave()


def test_a_chave_nunca_aparece_no_erro(monkeypatch, tmp_path):
    """A chave vai no cabeçalho; a mensagem de erro só leva o TIPO da falha."""
    import httpx

    from app.config import settings

    segredo = "chave-super-secreta-0123456789"
    arq = tmp_path / "k.env"
    arq.write_text(f"ACME_DISPAROS_API_KEY={segredo}\n")
    monkeypatch.setattr(settings, "api_disparos_env_file", str(arq))
    monkeypatch.delenv("COPILOTO_UNDER_PYTEST", raising=False)

    visto = {}

    def _get(url, headers=None, timeout=None, **k):
        visto["url"], visto["headers"] = url, headers
        raise httpx.ConnectError(f"falhou com {headers}")

    monkeypatch.setattr(httpx, "get", _get)
    r = ld.resolver()

    assert r.motivo == "linha_disparadora_indisponivel"
    assert segredo not in (r.detalhe or "")
    assert visto["headers"] == {"X-API-Key": segredo}       # foi no cabeçalho
    assert segredo not in visto["url"]                      # e não na URL
    assert visto["url"].endswith("/numbers/papel")


# ── tick: sem linha, não envia ───────────────────────────────────────────────


class _SenderFake:
    def __init__(self):
        self.chamadas = []

    def __call__(self, telefone, texto, *, com_botao, **kw):
        self.chamadas.append((telefone, kw.get("linha")))
        return {"ok": True, "dry_run": False, "sent": True, "wamid": "wamid.x",
                "template": "acme_corretor_ola_a_01", "categoria": "UTILITY"}


class _SessionFake:
    def __init__(self, fila):
        self._fila, self._lim = fila, len(fila)

    def execute(self, sql, params=None, *a, **k):
        if params and "lim" in params:
            self._lim = params["lim"]
        return self

    rowcount = 1
    def mappings(self): return self
    def all(self): return self._fila[: self._lim]
    def fetchall(self): return []
    def first(self): return None
    def scalar(self): return 0
    def commit(self): pass
    def rollback(self): pass
    def close(self): pass


FILA = [{"id": i, "telefone": f"+5500000000000{i}"} for i in range(1, 4)]


@pytest.fixture()
def tick_liberado(monkeypatch):
    """Ligado, rampa GREEN, fila cheia, sem agenda. Só a LINHA decide."""
    from app.services import rampa

    monkeypatch.setattr(cfg, "_carregar", lambda: {"carrossel_corretor_ativo": True})
    monkeypatch.setattr(cb, "travar", lambda ns, t: None)
    monkeypatch.setattr(cb, "soltar", lambda *a, **k: None)
    monkeypatch.setattr(cb, "_voltar_pra_fila", lambda s, t: 0)
    monkeypatch.setattr(cb, "_voltar_respondeu_pra_fila", lambda s, t: 0)
    monkeypatch.setattr(cb, "_enviados_hoje", lambda s, t: 0)
    monkeypatch.setattr(cb, "_cota_ate_agora", lambda cap, agora=None, janela=None: cap)
    consultas: list[str] = []

    def _rampa(s, slug, cid, **k):
        consultas.append(slug)
        return rampa.Rampa(slug, 80, 0, 9, "GREEN", "ok")

    monkeypatch.setattr(rampa, "cap_de_hoje", _rampa)
    return consultas


def test_tick_sai_pela_linha_marcada(monkeypatch, tick_liberado):
    _cadastro(monkeypatch, [_n(PID_CORRETOR, "disparo"), _n(PID_CORRETOR, "atendimento")])
    sender = _SenderFake()

    res = cb.tick(session=_SessionFake(FILA), sender=sender)

    assert res.enviados == cb.MAX_POR_TICK
    assert {linha for _, linha in sender.chamadas} == {LINHA_CORRETOR}
    assert tick_liberado == ["corretor"], "a rampa tem que olhar a qualidade da linha que dispara"


@pytest.mark.parametrize("cadastro,motivo", [
    ([_n(PID_CORRETOR, "pausado"), _n(PID_CORRETOR, "atendimento")], "sem_linha_disparadora"),
    ([_n(PID_CORRETOR, "atendimento")], "sem_linha_disparadora"),
    ([], "sem_linha_disparadora"),
    ([_n(PID_CORRETOR, "disparo"), _n(PID_NOVO, "disparo")], "linha_disparadora_ambigua"),
    ([_n(PID_NOVO, "disparo")], "linha_disparadora_fora_do_registro"),
    ([_n(PID_CORRETOR, "disparo")], "linha_disparadora_sem_resposta_tratada"),
])
def test_sem_linha_resolvida_o_tick_NAO_envia_e_diz_o_motivo(
        monkeypatch, tick_liberado, cadastro, motivo):
    """A prova pedida: ligado, GREEN, fila cheia — e nenhuma mensagem sai.
    E nunca pela linha da Corretor 'porque sempre foi ela'."""
    _cadastro(monkeypatch, cadastro)
    sender = _SenderFake()
    reservas: list[int] = []
    monkeypatch.setattr(cb, "_reservar", lambda s, i, t, *a: reservas.append(i) or True)

    res = cb.tick(session=_SessionFake(FILA), sender=sender)

    assert sender.chamadas == [], "saiu mensagem sem linha disparadora"
    assert reservas == [], "tirou lead da fila sem ter por onde enviar"
    assert res.enviados == 0
    assert res.motivo_parada == motivo
    assert tick_liberado == [], "nem a rampa deve ser consultada sem linha"


def test_api_fora_o_tick_nao_envia(monkeypatch, tick_liberado):
    monkeypatch.setattr(ld, "_buscar_numeros",
                        lambda: (_ for _ in ()).throw(ld.Indisponivel("fora")))
    sender = _SenderFake()
    res = cb.tick(session=_SessionFake(FILA), sender=sender)
    assert sender.chamadas == []
    assert res.motivo_parada == "linha_disparadora_indisponivel"


def test_desligado_nem_pergunta_pela_linha(monkeypatch):
    """Com o interruptor desligado o tick não bate no api-disparos a cada 3 min."""
    monkeypatch.setattr(cfg, "_carregar", lambda: {"carrossel_corretor_ativo": False})
    monkeypatch.setattr(ld, "_buscar_numeros",
                        lambda: pytest.fail("consultou o cadastro com o disparo desligado"))
    assert cb.tick(session=_SessionFake(FILA), sender=_SenderFake()).motivo_parada == "desligado"


def test_disparar_nao_procura_linha_por_conta_propria(monkeypatch):
    """`_disparar` sem linha levanta — não existe mais `line_by_slug('corretor')` ali."""
    import inspect

    fonte = inspect.getsource(cb._disparar)
    assert "line_by_slug" not in fonte
    with pytest.raises(cb._NadaSaiu, match="sem linha disparadora"):
        cb._disparar(object(), {"id": 1}, "+5500000000000", _SenderFake(), 1)


# ── tela: resumo e troca de papel ────────────────────────────────────────────


def test_resumo_pra_tela_mostra_numeros_papel_e_motivo(monkeypatch):
    _cadastro(monkeypatch, [_n(PID_CORRETOR, "pausado", phone="+5500000000000", nome="Acme"),
                            _n(PID_CORRETOR, "atendimento")])
    r = ld.resumo()
    assert r["slug"] is None
    assert r["motivo"] == "sem_linha_disparadora"
    assert r["motivo_texto"] == "nenhum número está marcado como disparador"
    assert r["indisponivel"] is False
    corretor = r["numeros"][0]
    assert (corretor["slug"], corretor["papel"], corretor["operavel"]) == ("corretor", "pausado", True)
    assert r["numeros"][1]["operavel"] is False      # a da Pessoa10 não serve pro disparo


def test_resumo_indisponivel_nao_e_lista_vazia(monkeypatch):
    """'Não consegui ler' ≠ 'não há número'. A tela mostra diferente."""
    monkeypatch.setattr(ld, "_buscar_numeros",
                        lambda: (_ for _ in ()).throw(ld.Indisponivel("fora")))
    r = ld.resumo()
    assert r["indisponivel"] is True and r["numeros"] == []


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


def test_endpoint_troca_o_papel_e_devolve_o_estado_novo(client, monkeypatch):
    pedidos = []

    def _trocar(pid, papel, autor, pausar_os_outros=False):
        pedidos.append((pid, papel, autor, pausar_os_outros))
        return {"status": 200, "corpo": {"ok": True}}

    monkeypatch.setattr(ld, "trocar_papel", _trocar)
    _cadastro(monkeypatch, [_n(PID_CORRETOR, "disparo")])

    r = client.put(f"/painel/automacoes/remarketing/linhas/{PID_CORRETOR}/papel",
                   json={"papel": "disparo"}, headers=ADMIN)

    assert r.status_code == 200, r.text
    assert pedidos == [(PID_CORRETOR, "disparo", "dona", False)]

    # Trocar de disparador num pedido só: a opção atravessa a ponte.
    r = client.put(f"/painel/automacoes/remarketing/linhas/{PID_CORRETOR}/papel",
                   json={"papel": "disparo", "pausar_os_outros": True}, headers=ADMIN)
    assert r.status_code == 200, r.text
    assert pedidos[-1] == (PID_CORRETOR, "disparo", "dona", True)
    assert r.json()["linha_disparadora"]["slug"] == "corretor"


def test_endpoint_repassa_a_recusa_do_cadastro(client, monkeypatch):
    monkeypatch.setattr(ld, "trocar_papel", lambda *a, **k: {
        "status": 422, "corpo": {"detail": "papel deve ser 'atendimento', 'disparo' ou 'pausado'"}})
    r = client.put(f"/painel/automacoes/remarketing/linhas/{PID_CORRETOR}/papel",
                   json={"papel": "qualquer"}, headers=ADMIN)
    assert r.status_code == 422
    assert "pausado" in r.json()["detail"]


def test_endpoint_numero_de_outro_cliente_vira_404(client, monkeypatch):
    monkeypatch.setattr(ld, "trocar_papel", lambda *a, **k: {
        "status": 404, "corpo": {"detail": "número não encontrado neste cliente"}})
    r = client.put(f"/painel/automacoes/remarketing/linhas/{PID_NOVO}/papel",
                   json={"papel": "disparo"}, headers=ADMIN)
    assert r.status_code == 404


def test_endpoint_exige_gestao(client, monkeypatch):
    monkeypatch.setattr(ld, "trocar_papel",
                        lambda *a, **k: pytest.fail("corretora trocou papel de linha"))
    r = client.put(f"/painel/automacoes/remarketing/linhas/{PID_CORRETOR}/papel",
                   json={"papel": "disparo"}, headers={"X-Painel-User": "corretora_c"})
    assert r.status_code in (401, 403)


def test_endpoint_com_cadastro_fora_e_502_nao_200(client, monkeypatch):
    def _cai(*a, **k):
        raise ld.Indisponivel("api-disparos não respondeu: ConnectError")
    monkeypatch.setattr(ld, "trocar_papel", _cai)
    r = client.put(f"/painel/automacoes/remarketing/linhas/{PID_CORRETOR}/papel",
                   json={"papel": "disparo"}, headers=ADMIN)
    assert r.status_code == 502


def test_mapa_da_tela_sem_disparador_diz_zero_e_o_motivo(monkeypatch):
    """A tela não pode mostrar 'limite de hoje: 80' com o disparo sem linha."""
    from app.api.automacoes import _mapa_carrossel_corretor

    _cadastro(monkeypatch, [_n(PID_CORRETOR, "pausado")])
    mapa = _mapa_carrossel_corretor()
    assert mapa["linha_disparadora"]["motivo"] == "sem_linha_disparadora"
    assert mapa["ritmo"]["por_dia"] == 0
    assert mapa["ritmo"]["fonte_do_limite"] == "falta de linha disparadora"
    # A saúde aparece MESMO sem disparador (05/10/2026): é com a linha pausada
    # que a Dona precisa ver se pode voltar. O campo diz de qual linha é, e sem
    # leitura a cor é "desconhecida" — nunca verde por omissão.
    saude = mapa["saude_linha"]
    assert saude["de_qual"] == "linha_da_casa_sem_marca_de_disparo"
    assert saude["cor"] == "desconhecida"
    numero = mapa["linha_disparadora"]["numeros"][0]
    assert numero["papel"] == "pausado"
    assert numero["saude"]["cor"] == "desconhecida"
    assert numero["saude"]["erro"]
