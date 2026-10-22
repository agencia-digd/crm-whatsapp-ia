"""O PAINEL MANDA — prova de ponta a ponta, sem enviar nada (05/10/2026).

Ordem da Dona: "Garante que esse sistema de envio de remarketing esteja certo,
criado no painel e funcionando sem precisar de mim aqui. Se mudarem no painel,
vai obedecer."

Cada teste daqui faz o caminho inteiro:

    API da tela (as mesmas rotas que o painel chama)
        → banco (`config_operacional`) / cadastro de números
            → o tick DE VERDADE (`carrossel_corretor.tick`, fila, reserva e disparo reais)
                → um REMETENTE FALSO, que só anota pra quem "enviaria"

Nada sai da máquina: o remetente é um objeto de teste e o cadastro de números
é um dicionário. O que é de verdade é tudo o que decide — a leitura do painel,
a fila, a autorização antes de cada mensagem, a reserva e o registro.

A regra provada em cada controle é a mesma: **mudou na tela, vale na volta
seguinte** — e, dentro de uma volta, vale antes da PRÓXIMA mensagem.

Estes nomes de teste estão na tabela de `projetos/crm/GARANTIA-REMARKETING.md`.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text as _t

from app.db.session import SessionLocal
from app.main import app
from app.services import carrossel_corretor as cb
from app.services import cloud_lines as cl
from app.services import config_operacional as cfg
from app.services import linha_disparadora as ld
from app.services import rampa
from app.services import remarketing_agenda as agenda

TENANT = 1
ADMIN = {"X-Painel-User": "dona"}
_PREFIXO = "5500000000000"                 # faixa de telefone só destes testes

# Etapas próprias: a fila do teste não disputa lead com a de outro arquivo.
ETAPA_A = 9201
ETAPA_B = 9202
ETAPA_SOME = 9203                       # criada e apagada num teste

PID_CORRETOR = "000000000000000"
PID_CORRETOR = "000000000000000"
LINHA_CORRETOR = cl.CloudLine(phone_id=PID_CORRETOR, slug="corretor", papel="corretora",
                         env_file="", token_var="")
LINHA_CORRETOR = cl.CloudLine(phone_id=PID_CORRETOR, slug="corretor", papel="corretora",
                          env_file="", token_var="")

# Quarta-feira, 15:00 de São Paulo. O relógio da AGENDA é fixado aqui pra o
# teste de dia e de horário dar o mesmo resultado a qualquer hora.
QUARTA_15H = datetime(2026, 10, 7, 18, 0, tzinfo=timezone.utc)
QUARTA = 2

CHAVES = ("carrossel_corretor_ativo", "carrossel_pesos", "carrossel_auto_resposta_segundos",
          "remarketing_janela_horario", "remarketing_dias_semana",
          "remarketing_cap_diario", "remarketing_cap_semanal",
          "remarketing_etapa", "remarketing_dono", "remarketing_etapa_parar")


# ── Dublês: o cadastro de números e o remetente ──────────────────────────────


class CadastroFalso:
    """O que o api-disparos guarda: o papel de cada número. A rota da tela
    (`PUT …/linhas/{id}/papel`) escreve aqui e o tick lê daqui."""

    def __init__(self) -> None:
        self.papel = {PID_CORRETOR: "pausado", PID_CORRETOR: "atendimento"}
        self.fora_do_ar = False

    def buscar(self):
        if self.fora_do_ar:
            raise ld.Indisponivel("api-disparos não respondeu: ConnectError")
        return [{"phone_number_id": pid, "phone": "+5500000000000",
                 "display_name": None, "papel": papel}
                for pid, papel in self.papel.items()]

    def trocar(self, pid, papel, autor, pausar_os_outros=False):
        if papel not in ("disparo", "atendimento", "pausado"):
            return {"status": 422, "corpo": {"detail": "papel inválido"}}
        if pausar_os_outros and papel == "disparo":
            for outro, p in self.papel.items():
                if outro != pid and p == "disparo":
                    self.papel[outro] = "pausado"
        self.papel[pid] = papel
        return {"status": 200, "corpo": {"ok": True}}


class Remetente:
    """Contrato do remetente real quando a Meta ACEITA. Anota e devolve."""

    def __init__(self, ao_enviar=None) -> None:
        self.telefones: list[str] = []
        self.chamadas: list[dict] = []
        self.ao_enviar = ao_enviar

    def __call__(self, telefone, texto, *, com_botao, **kw):
        self.telefones.append(telefone)
        self.chamadas.append(kw)
        if self.ao_enviar:
            self.ao_enviar(len(self.telefones))
        return {"ok": True, "dry_run": False, "sent": True,
                "wamid": f"wamid.garantia.{telefone[-4:]}.{len(self.telefones)}",
                "template": "acme_corretor_ola_a_01", "categoria": "UTILITY"}


# ── Montagem ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def linha_disparadora_de_teste():
    """Redefine a fixture do conftest: aqui o `resolver` é o REAL, lendo o
    cadastro falso."""
    yield


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
        ses.execute(_t("DELETE FROM config_operacional WHERE chave = ANY(:c)"),
                    {"c": list(CHAVES)})
        ses.execute(_t("DELETE FROM pipeline_stages WHERE id = :i"), {"i": ETAPA_SOME})
        ses.commit()
        cfg.invalidar_cache()

    with SessionLocal() as ses:
        for i, nome, funil in (
            (cb.STAGE_FILA_PADRAO, "Fila", "remarketing"),
            (cb.STAGE_DISPARADO, "Disparado", "remarketing"),
            (cb.STAGE_RESPONDEU, "Respondeu", "remarketing"),
            (ETAPA_A, "Etapa A (garantia)", "remarketing"),
            (ETAPA_B, "Etapa B (garantia)", "remarketing"),
        ):
            ses.execute(_t("INSERT INTO pipeline_stages (id, tenant_id, nome, funil) "
                           "VALUES (:i, :t, :n, :f) ON CONFLICT (id) DO NOTHING"),
                        {"i": i, "t": TENANT, "n": nome, "f": funil})
        ses.commit()
        _limpar(ses)
        yield ses
        ses.rollback()
        _limpar(ses)


@pytest.fixture()
def painel() -> TestClient:
    return TestClient(app)


@pytest.fixture()
def cadastro(monkeypatch) -> CadastroFalso:
    c = CadastroFalso()
    linhas = {PID_CORRETOR: LINHA_CORRETOR, PID_CORRETOR: LINHA_CORRETOR}
    monkeypatch.setattr(cl, "line_by_phone_id", lambda pid: linhas.get(str(pid or "")))
    monkeypatch.setattr(ld, "_buscar_numeros", c.buscar)
    monkeypatch.setattr(ld, "trocar_papel", c.trocar)
    return c


@pytest.fixture()
def mesa(monkeypatch, s, painel, cadastro):
    """O estado de partida de todo teste: disparo DESLIGADO, número da casa
    PAUSADO (o estado real de produção em 05/10), relógio da agenda numa
    quarta às 15h, sem curva de horário. Quem liga é cada teste, pela tela."""
    monkeypatch.setattr(agenda, "_agora_sp",
                        lambda agora=None: QUARTA_15H.astimezone(agenda.TZ_SP))
    monkeypatch.setattr(cb, "_cota_ate_agora", lambda cap, agora=None, janela=None: cap)
    rampa._cache.clear()
    tela(painel, "remarketing_etapa", ETAPA_A)
    return painel


def tela(painel: TestClient, chave: str, valor) -> None:
    """Grava UM controle do jeito que a tela grava."""
    r = painel.put(f"/painel/automacoes/config/{chave}", json={"valor": valor},
                   headers=ADMIN)
    assert r.status_code == 200, f"{chave}={valor!r}: {r.status_code} {r.text}"


def papel(painel: TestClient, pid: str, valor: str, **extra) -> None:
    r = painel.put(f"/painel/automacoes/remarketing/linhas/{pid}/papel",
                   json={"papel": valor, **extra}, headers=ADMIN)
    assert r.status_code == 200, r.text


def ligar_tudo(painel: TestClient) -> None:
    """Os dois atos da Dona: marcar o disparador e ligar o interruptor."""
    papel(painel, PID_CORRETOR, "disparo")
    tela(painel, "carrossel_corretor_ativo", True)


def lead(s, sufixo: str, etapa: int = ETAPA_A, *, minutos_atras: int = 0,
         dono: int = cb.CORRETORA_CORRETOR) -> int:
    lid = s.execute(
        _t("""INSERT INTO leads (tenant_id, nome, telefone, owner_id, stage_id,
                                 created_at, updated_at)
              VALUES (:t, 'Teste Garantia', :tel, :o, :st, now(),
                      now() - make_interval(mins => :m))
           RETURNING id"""),
        {"t": TENANT, "tel": f"{_PREFIXO}{sufixo}", "o": dono, "st": etapa,
         "m": minutos_atras},
    ).scalar()
    s.commit()
    return int(lid)


def fila(s, n: int, etapa: int = ETAPA_A, comeca: int = 10) -> list[int]:
    """n leads na etapa, o primeiro há mais tempo (a fila é FIFO)."""
    return [lead(s, f"{comeca + i:02d}", etapa, minutos_atras=1000 - i) for i in range(n)]


def etapa_de(s, lead_id: int) -> int:
    s.expire_all()
    return int(s.execute(_t("SELECT stage_id FROM leads WHERE id = :i"),
                         {"i": lead_id}).scalar())


def eventos(s, lead_id: int, tipo: str) -> list[dict]:
    return [dict(r[0]) for r in s.execute(
        _t("SELECT payload FROM lead_events WHERE lead_id = :i AND tipo = :tp ORDER BY at, id"),
        {"i": lead_id, "tp": tipo}).all()]


def volta(remetente: Remetente, **kw) -> cb.CarrosselResult:
    """Uma volta do robô, do jeito que o timer chama — só com o remetente falso."""
    return cb.tick(sender=remetente, tenant_id=TENANT, **kw)


# ═════════════════════════════════════════════════════════════════════════════
# 1. INTERRUPTOR — `carrossel_corretor_ativo`
# ═════════════════════════════════════════════════════════════════════════════


def test_interruptor_liga_e_desliga_pela_tela(mesa, s):
    fila(s, 6)
    papel(mesa, PID_CORRETOR, "disparo")
    r = Remetente()

    assert volta(r).motivo_parada == "desligado"
    assert r.telefones == [], "saiu mensagem com o disparo desligado"

    tela(mesa, "carrossel_corretor_ativo", True)
    assert volta(r).enviados == 3
    assert len(r.telefones) == 3

    tela(mesa, "carrossel_corretor_ativo", False)
    assert volta(r).motivo_parada == "desligado"
    assert len(r.telefones) == 3, "desligou na tela e a volta seguinte ainda enviou"


def test_interruptor_desligado_no_meio_do_lote_segura_as_proximas(mesa, s):
    """Uma volta manda até 3. Desligar depois da PRIMEIRA mensagem não pode
    deixar sair a segunda: o painel é relido antes de cada uma."""
    ids = fila(s, 3)
    ligar_tudo(mesa)
    r = Remetente(ao_enviar=lambda n: n == 1 and tela(mesa, "carrossel_corretor_ativo", False))

    res = volta(r)

    assert len(r.telefones) == 1
    assert res.enviados == 1
    assert res.motivo_parada == "desligado_no_meio_do_lote"
    # quem não recebeu NÃO saiu da fila nem ganhou reserva
    assert [etapa_de(s, i) for i in ids] == [cb.STAGE_DISPARADO, ETAPA_A, ETAPA_A]
    assert eventos(s, ids[1], cb.EVENT_RESERVA) == []


def test_voltar_ao_padrao_desliga_e_nao_apaga_a_decisao(mesa, s):
    """O "voltar ao padrão" apagava a linha do banco: o interruptor ficava
    sem dono e sem data. Agora grava DESLIGADO e deixa histórico."""
    fila(s, 3)
    ligar_tudo(mesa)
    r = mesa.delete("/painel/automacoes/config/carrossel_corretor_ativo", headers=ADMIN)
    assert r.status_code == 200, r.text

    assert volta(Remetente()).motivo_parada == "desligado"
    linha = s.execute(_t("SELECT valor, atualizado_por FROM config_operacional "
                         "WHERE chave = 'carrossel_corretor_ativo'")).first()
    assert linha is not None, "a linha do interruptor foi apagada"
    assert (linha[0], linha[1]) == (False, "dona")
    hist = cfg.historico(["carrossel_corretor_ativo"], limite=2)
    assert [(h["acao"], h["antes"], h["depois"], h["autor"]) for h in hist] == [
        ("restaurar", True, False, "dona"), ("definir", None, True, "dona"),
    ]


def test_env_ligado_nao_liga_o_disparo(mesa, s, monkeypatch):
    """`CARROSSEL_CORRETOR_ATIVO=true` no `.env`, sem linha no banco: não envia."""
    from app.config import settings

    fila(s, 3)
    papel(mesa, PID_CORRETOR, "disparo")
    monkeypatch.setattr(settings, "carrossel_corretor_ativo", True, raising=False)
    r = Remetente()
    assert volta(r).motivo_parada == "desligado"
    assert r.telefones == []


# ═════════════════════════════════════════════════════════════════════════════
# 2. NÚMERO DISPARADOR — `phone_numbers.papel` (api-disparos)
# ═════════════════════════════════════════════════════════════════════════════


def test_numero_disparador_marcado_e_pausado_pela_tela(mesa, s):
    fila(s, 6)
    tela(mesa, "carrossel_corretor_ativo", True)
    r = Remetente()

    # estado de produção em 05/10: interruptor ligado NÃO basta, a linha está pausada
    assert volta(r).motivo_parada == "sem_linha_disparadora"
    assert r.telefones == []

    papel(mesa, PID_CORRETOR, "disparo")
    assert volta(r).enviados == 3
    assert {c["linha"].slug for c in r.chamadas} == {"corretor"}

    papel(mesa, PID_CORRETOR, "pausado")
    assert volta(r).motivo_parada == "sem_linha_disparadora"
    assert len(r.telefones) == 3


def test_numero_pausado_no_meio_do_lote_segura_as_proximas(mesa, s):
    ids = fila(s, 3)
    ligar_tudo(mesa)
    r = Remetente(ao_enviar=lambda n: n == 1 and papel(mesa, PID_CORRETOR, "pausado"))

    res = volta(r)

    assert len(r.telefones) == 1
    assert res.motivo_parada == "sem_linha_disparadora"
    assert etapa_de(s, ids[1]) == ETAPA_A


def test_dois_disparadores_nao_envia_e_a_troca_num_pedido_so_resolve(mesa, s, cadastro):
    fila(s, 3)
    ligar_tudo(mesa)
    papel(mesa, PID_CORRETOR, "disparo")                       # dois marcados
    r = Remetente()
    assert volta(r).motivo_parada == "linha_disparadora_ambigua"
    assert r.telefones == []

    # trocar de disparador num pedido só: os outros são pausados juntos
    papel(mesa, PID_CORRETOR, "disparo", pausar_os_outros=True)
    assert cadastro.papel == {PID_CORRETOR: "disparo", PID_CORRETOR: "pausado"}
    assert volta(r).enviados == 3


def test_cadastro_de_numeros_fora_do_ar_nao_envia(mesa, s, cadastro):
    fila(s, 3)
    ligar_tudo(mesa)
    cadastro.fora_do_ar = True
    r = Remetente()
    assert volta(r).motivo_parada == "linha_disparadora_indisponivel"
    assert r.telefones == []


# ═════════════════════════════════════════════════════════════════════════════
# 3. ETAPA — `remarketing_etapa`
# ═════════════════════════════════════════════════════════════════════════════


def test_etapa_trocada_pela_tela_muda_de_onde_o_lote_sai(mesa, s):
    da_a = fila(s, 3, ETAPA_A, comeca=10)
    da_b = fila(s, 3, ETAPA_B, comeca=20)
    ligar_tudo(mesa)
    r = Remetente()

    volta(r)
    assert [etapa_de(s, i) for i in da_a] == [cb.STAGE_DISPARADO] * 3
    assert [etapa_de(s, i) for i in da_b] == [ETAPA_B] * 3

    for i in fila(s, 2, ETAPA_A, comeca=30):               # sobra gente na A
        assert etapa_de(s, i) == ETAPA_A
    tela(mesa, "remarketing_etapa", ETAPA_B)
    volta(r)
    assert [etapa_de(s, i) for i in da_b] == [cb.STAGE_DISPARADO] * 3
    assert r.telefones[3:] == [f"{_PREFIXO}{n}" for n in (20, 21, 22)]


def test_etapa_trocada_no_meio_do_lote_segura_as_proximas(mesa, s):
    ids = fila(s, 3)
    ligar_tudo(mesa)
    r = Remetente(ao_enviar=lambda n: n == 1 and tela(mesa, "remarketing_etapa", ETAPA_B))
    res = volta(r)
    assert len(r.telefones) == 1
    assert res.motivo_parada == "alvo_mudou_no_meio_do_lote"
    assert etapa_de(s, ids[1]) == ETAPA_A


def test_etapa_apagada_do_funil_nao_envia(mesa, s):
    """A etapa escolhida deixa de existir: o robô segura — não cai na Fila
    de sempre, que seria disparar pra uma lista que ninguém escolheu."""
    s.execute(_t("INSERT INTO pipeline_stages (id, tenant_id, nome, funil) "
                 "VALUES (:i, :t, 'Etapa que some', 'remarketing')"),
              {"i": ETAPA_SOME, "t": TENANT})
    s.commit()
    tela(mesa, "remarketing_etapa", ETAPA_SOME)
    fila(s, 3, cb.STAGE_FILA_PADRAO)
    ligar_tudo(mesa)
    s.execute(_t("DELETE FROM pipeline_stages WHERE id = :i"), {"i": ETAPA_SOME})
    s.commit()

    r = Remetente()
    assert volta(r).motivo_parada == "etapa_invalida"
    assert r.telefones == []


# ═════════════════════════════════════════════════════════════════════════════
# 4. HORÁRIO — `remarketing_janela_horario`   5. DIAS — `remarketing_dias_semana`
# ═════════════════════════════════════════════════════════════════════════════


def test_horario_gravado_pela_tela_segura_e_solta(mesa, s):
    fila(s, 6)
    ligar_tudo(mesa)
    r = Remetente()

    tela(mesa, "remarketing_janela_horario", "09:00-12:00")    # o relógio marca 15h
    assert volta(r).motivo_parada == "fora_da_janela"
    assert r.telefones == []

    tela(mesa, "remarketing_janela_horario", "14:00-16:00")
    assert volta(r).enviados == 3

    tela(mesa, "remarketing_janela_horario", "09:00-15:00")    # fim exclusivo: 15:00 já é fora
    assert volta(r).motivo_parada == "fora_da_janela"
    assert len(r.telefones) == 3


def test_fora_do_horario_do_robo_nao_envia_nem_sem_janela_gravada(mesa, s, monkeypatch):
    """Sem horário próprio na tela, o limite é o do robô (09:00 às 19:00). Uma
    volta que se arrasta além das 19h não manda a mensagem seguinte."""
    fila(s, 3)
    ligar_tudo(mesa)
    monkeypatch.setattr(agenda, "dentro_do_horario_do_robo", lambda agora=None: False)
    r = Remetente()
    res = volta(r)
    assert r.telefones == []
    assert res.motivo_parada == "fora_do_horario_do_robo"


def test_dias_gravados_pela_tela_seguram_e_soltam(mesa, s):
    fila(s, 6)
    ligar_tudo(mesa)
    r = Remetente()

    tela(mesa, "remarketing_dias_semana", [0, 1])              # seg e ter; hoje é quarta
    assert volta(r).motivo_parada == "fora_dos_dias"
    assert r.telefones == []

    tela(mesa, "remarketing_dias_semana", [0, 1, QUARTA])
    assert volta(r).enviados == 3

    tela(mesa, "remarketing_dias_semana", [5, 6])
    assert volta(r).motivo_parada == "fora_dos_dias"
    assert len(r.telefones) == 3


# ═════════════════════════════════════════════════════════════════════════════
# 6. TETO POR DIA — `remarketing_cap_diario`   7. POR SEMANA — `remarketing_cap_semanal`
# ═════════════════════════════════════════════════════════════════════════════


def test_teto_por_dia_gravado_pela_tela_vale_na_volta_seguinte(mesa, s):
    fila(s, 8)
    ligar_tudo(mesa)
    r = Remetente()
    ja = cb._enviados_hoje(s, TENANT)          # o que outros testes já gastaram hoje

    tela(mesa, "remarketing_cap_diario", ja + 2)
    assert volta(r).enviados == 2
    assert volta(r).motivo_parada == "cap_do_dia_atingido"
    assert len(r.telefones) == 2

    tela(mesa, "remarketing_cap_diario", ja + 3)               # subiu 1
    assert volta(r).enviados == 1

    tela(mesa, "remarketing_cap_diario", 0)                    # zero = não sai
    assert volta(r).motivo_parada == "cap_do_dia_zerado_no_painel"
    assert len(r.telefones) == 3


def test_teto_por_dia_baixado_no_meio_do_lote_segura_as_proximas(mesa, s):
    fila(s, 3)
    ligar_tudo(mesa)
    ja = cb._enviados_hoje(s, TENANT)
    r = Remetente(ao_enviar=lambda n: n == 1 and tela(mesa, "remarketing_cap_diario", ja + 1))
    res = volta(r)
    assert len(r.telefones) == 1
    assert res.motivo_parada == "cap_do_dia_atingido"


def test_teto_por_semana_gravado_pela_tela_vale_na_volta_seguinte(mesa, s):
    fila(s, 8)
    ligar_tudo(mesa)
    r = Remetente()
    ja = agenda.enviados_na_semana(s, TENANT, cb.EVENTOS_DE_SAIDA)

    tela(mesa, "remarketing_cap_semanal", ja + 2)
    assert volta(r).enviados == 2                              # o saldo corta o lote de 3
    assert volta(r).motivo_parada == "cap_semanal_atingido"

    tela(mesa, "remarketing_cap_semanal", ja + 3)
    assert volta(r).enviados == 1

    tela(mesa, "remarketing_cap_semanal", 0)
    assert volta(r).motivo_parada == "cap_semanal_atingido"
    assert len(r.telefones) == 3


def test_agenda_grava_tudo_numa_transacao_ou_nada(mesa, s):
    """O "salvar" da agenda inteiro: um campo inválido e NENHUM é gravado —
    o robô nunca lê horário novo com teto antigo."""
    tela(mesa, "remarketing_cap_diario", 40)
    r = mesa.put("/painel/automacoes/remarketing/agenda", headers=ADMIN, json={
        "remarketing_janela_horario": "10:00-12:00",
        "remarketing_dias_semana": [0, 1],
        "remarketing_cap_diario": 999,                         # fora da faixa
    })
    assert r.status_code == 422
    # a recusa diz QUAL campo errou (a tela mostra ao lado dele) e segue
    # trazendo `detail` em texto, como toda rota de configuração
    corpo = r.json()
    assert list(corpo["erros"]) == ["remarketing_cap_diario"]
    assert corpo["detail"] == corpo["erros"]["remarketing_cap_diario"]
    assert corpo["gravadas"] == []
    cfg.invalidar_cache()
    assert cfg.get("remarketing_janela_horario") is None
    assert cfg.get("remarketing_dias_semana") == []
    assert cfg.get("remarketing_cap_diario") == 40

    r = mesa.put("/painel/automacoes/remarketing/agenda", headers=ADMIN, json={
        "remarketing_janela_horario": "10:00-12:00",
        "remarketing_dias_semana": [0, 1],
        "remarketing_cap_diario": None,                        # volta ao padrão
    })
    assert r.status_code == 200, r.text
    cfg.invalidar_cache()
    assert cfg.get("remarketing_janela_horario") == "10:00-12:00"
    assert cfg.get("remarketing_dias_semana") == [0, 1]
    assert cfg.get("remarketing_cap_diario") is None


def test_agenda_com_dois_campos_tortos_diz_os_dois_e_nao_grava_nenhum(mesa, s):
    r = mesa.put("/painel/automacoes/remarketing/agenda", headers=ADMIN, json={
        "remarketing_janela_horario": "25:00-26:00",
        "remarketing_dias_semana": [0, 1],
        "remarketing_cap_semanal": -5,
    })
    assert r.status_code == 422
    assert sorted(r.json()["erros"]) == ["remarketing_cap_semanal",
                                         "remarketing_janela_horario"]
    cfg.invalidar_cache()
    assert cfg.get("remarketing_dias_semana") == []      # o campo bom também não entrou


# ═════════════════════════════════════════════════════════════════════════════
# 8. PESOS A/B — `carrossel_pesos`
# ═════════════════════════════════════════════════════════════════════════════


def test_pesos_gravados_pela_tela_mudam_a_variante_que_sai(mesa, s):
    ligar_tudo(mesa)
    r = Remetente()

    tela(mesa, "carrossel_pesos", {"direta": 90, "curta": 0, "direta_marketing": 10})
    primeiros = fila(s, 3, comeca=10)
    volta(r)
    variantes = {eventos(s, i, cb.EVENT_DISPARO)[0]["variante"] for i in primeiros}
    assert variantes <= {"direta", "direta_marketing"}

    tela(mesa, "carrossel_pesos", {"direta": 0, "curta": 90, "direta_marketing": 10})
    segundos = fila(s, 3, comeca=20)
    volta(r)
    variantes = {eventos(s, i, cb.EVENT_DISPARO)[0]["variante"] for i in segundos}
    assert variantes <= {"curta", "direta_marketing"}
    assert "direta" not in variantes


def test_cota_de_marketing_e_fracao_do_teto_do_remarketing(mesa, s):
    """A cota de MARKETING ia sobre o teto da CADÊNCIA; com o remarketing em
    300 e a cadência em 50, "10% em marketing" viravam 5 mensagens."""
    fila(s, 3)
    ligar_tudo(mesa)
    r = Remetente()
    res = volta(r)
    assert res.cap_do_dia == 80
    assert {c["cap_diario_da_linha"] for c in r.chamadas} == {80}
    assert {c["origem"] for c in r.chamadas} == {"carrossel"}


# ═════════════════════════════════════════════════════════════════════════════
# 9. CORTE DA RESPOSTA AUTOMÁTICA — `carrossel_auto_resposta_segundos`
# ═════════════════════════════════════════════════════════════════════════════


def _disparado_ha(s, sufixo: str, segundos: float) -> tuple[str, int, datetime]:
    import json

    lid = lead(s, sufixo, cb.STAGE_DISPARADO)
    em = datetime.now(timezone.utc) - timedelta(seconds=segundos)
    s.execute(_t("INSERT INTO lead_events (tenant_id, lead_id, tipo, payload, at) "
                 "VALUES (:t, :i, :tp, CAST(:p AS jsonb), :at)"),
              {"t": TENANT, "i": lid, "tp": cb.EVENT_DISPARO, "at": em,
               "p": json.dumps({"variante": "direta", "categoria": "UTILITY"})})
    s.commit()
    return f"{_PREFIXO}{sufixo}", lid, em


def test_corte_da_resposta_automatica_gravado_pela_tela(mesa, s):
    """"Bom dia" 8 segundos depois do envio: com o corte em 5 é gente (vai pra
    Respondeu); com o corte em 10, gravado na tela, é o aparelho (fica em
    Disparado)."""
    tel1, lid1, em1 = _disparado_ha(s, "41", 600)
    assert cb.processar_inbound(tel1, "Bom dia", TENANT, tipo="text",
                                recebido_em=em1 + timedelta(seconds=8)) == "respondeu"
    assert etapa_de(s, lid1) == cb.STAGE_RESPONDEU

    tela(mesa, "carrossel_auto_resposta_segundos", 10)
    tel2, lid2, em2 = _disparado_ha(s, "42", 600)
    assert cb.processar_inbound(tel2, "Bom dia", TENANT, tipo="text",
                                recebido_em=em2 + timedelta(seconds=8)) == "automatica"
    assert etapa_de(s, lid2) == cb.STAGE_DISPARADO


# ═════════════════════════════════════════════════════════════════════════════
# FALHA FECHADA — "não consegui ler" nunca vira "manda"
# ═════════════════════════════════════════════════════════════════════════════


def test_banco_de_configuracao_fora_do_ar_nao_envia(mesa, s, monkeypatch):
    """Ligado, com tudo certo — e a leitura do painel falha. Antes caía no
    padrão (`.env`); agora o robô não sabe se está ligado e não envia."""
    fila(s, 3)
    ligar_tudo(mesa)
    cfg.get("carrossel_corretor_ativo")                 # cache quente dizendo "ligado"

    def _banco_fora():
        raise RuntimeError("could not connect to server")

    monkeypatch.setattr(cfg, "SessionLocal", _banco_fora)
    r = Remetente()
    res = volta(r)
    assert res.motivo_parada == "config_indisponivel"
    assert res.ligado is None
    assert r.telefones == []


def test_banco_de_configuracao_cai_no_meio_do_lote_segura_as_proximas(mesa, s, monkeypatch):
    fila(s, 3)
    ligar_tudo(mesa)

    def _derruba(n):
        if n == 1:
            monkeypatch.setattr(cfg, "SessionLocal",
                                lambda: (_ for _ in ()).throw(RuntimeError("banco fora")))

    r = Remetente(ao_enviar=_derruba)
    res = volta(r)
    assert len(r.telefones) == 1
    assert res.motivo_parada == "config_indisponivel"


@pytest.mark.parametrize("chave,lixo,motivo", [
    ("remarketing_janela_horario", "das nove às seis", "agenda_ilegivel"),
    ("remarketing_dias_semana", [9], "agenda_ilegivel"),
    ("remarketing_cap_diario", "muitos", "agenda_ilegivel"),
    ("remarketing_cap_semanal", -5, "agenda_ilegivel"),
    ("remarketing_etapa", "base", "etapa_invalida"),
    ("remarketing_etapa", 987654, "etapa_invalida"),
    ("remarketing_dono", 987654, "dono_invalido"),
    ("carrossel_corretor_ativo", "sim", "desligado"),
    ("carrossel_pesos", {"direta": 500}, "pesos_invalidos"),
    ("carrossel_pesos", {"variante_que_nao_existe": 100}, "pesos_invalidos"),
    ("carrossel_pesos", {"direta": 50, "curta": 50, "direta_marketing": 0}, "pesos_invalidos"),
])
def test_valor_torto_gravado_por_fora_da_tela_nao_envia(mesa, s, chave, lixo, motivo):
    import json

    fila(s, 3)
    ligar_tudo(mesa)
    s.execute(_t("INSERT INTO config_operacional (chave, valor, atualizado_por) "
                 "VALUES (:k, CAST(:v AS jsonb), 'por fora') "
                 "ON CONFLICT (chave) DO UPDATE SET valor = EXCLUDED.valor"),
              {"k": chave, "v": json.dumps(lixo)})
    s.commit()
    r = Remetente()
    assert volta(r).motivo_parada == motivo
    assert r.telefones == []


@pytest.mark.parametrize("qualidade,trecho", [
    (None, "não consegui ler"), ("RED", "RED"), ("FLAGGED", "FLAGGED"),
])
def test_qualidade_ilegivel_ou_ruim_nao_envia(mesa, s, monkeypatch, qualidade, trecho):
    fila(s, 3)
    ligar_tudo(mesa)
    monkeypatch.setattr(rampa, "_qualidade_meta", lambda slug: qualidade)
    r = Remetente()
    res = volta(r)
    assert r.telefones == []
    assert trecho in (res.motivo_parada or "")


def test_lote_manual_nao_fura_o_painel_nem_a_qualidade_parada(mesa, s, monkeypatch):
    """`carrossel_burst.py` (ignorar_ritmo + cap_override) era uma segunda
    autoridade por terminal. Agora obedece: desligado, dia, teto do painel e
    qualidade parada valem pro lote manual também."""
    fila(s, 6)
    papel(mesa, PID_CORRETOR, "disparo")
    r = Remetente()
    lote = dict(ignorar_ritmo=True, cap_override=250, max_por_tick=10, limite_manual=10)

    assert volta(r, **lote).motivo_parada == "desligado"

    tela(mesa, "carrossel_corretor_ativo", True)
    tela(mesa, "remarketing_dias_semana", [0])
    assert volta(r, **lote).motivo_parada == "fora_dos_dias"

    tela(mesa, "remarketing_dias_semana", [])
    monkeypatch.setattr(rampa, "_qualidade_meta", lambda slug: "RED")
    assert "RED" in (volta(r, **lote).motivo_parada or "")

    monkeypatch.setattr(rampa, "_qualidade_meta", lambda slug: "GREEN")
    ja = cb._enviados_hoje(s, TENANT)
    tela(mesa, "remarketing_cap_diario", ja + 2)
    assert r.telefones == []
    assert volta(r, **lote).enviados == 2, "o lote manual furou o teto gravado na tela"


# ═════════════════════════════════════════════════════════════════════════════
# O ENVIO — nada saiu / não sei / saiu
# ═════════════════════════════════════════════════════════════════════════════


def test_timeout_depois_do_pedido_nao_reenvia(mesa, s):
    """O pedido foi feito e a resposta não voltou. Antes virava "nada saiu" e
    o lead recebia de novo 3 minutos depois. Agora: fica em Disparado, a
    reserva segura o teto, e NÃO há segunda tentativa."""
    ids = fila(s, 3)
    ligar_tudo(mesa)

    def _timeout(telefone, texto, *, com_botao, **kw):
        raise httpx.ReadTimeout("a Meta não respondeu em 20 s")

    res = cb.tick(sender=_timeout, tenant_id=TENANT)
    assert (res.enviados, res.incertos, res.motivo_parada) == (0, 1, "envio_incerto")
    assert etapa_de(s, ids[0]) == cb.STAGE_DISPARADO
    reserva = eventos(s, ids[0], cb.EVENT_RESERVA)
    assert len(reserva) == 1 and reserva[0]["estado"] == "incerto"
    assert "ReadTimeout" in reserva[0]["motivo"]
    assert eventos(s, ids[0], cb.EVENT_DISPARO) == []
    # o lote parou: os outros dois não foram tocados
    assert [etapa_de(s, i) for i in ids[1:]] == [ETAPA_A, ETAPA_A]

    r = Remetente()
    volta(r)
    assert f"{_PREFIXO}10" not in r.telefones, "reenviou pra quem pode já ter recebido"


@pytest.mark.parametrize("retorno", [{}, None, {"ok": False}, {"sent": "true"}])
def test_retorno_fora_do_contrato_nao_vira_disparo(mesa, s, retorno):
    ids = fila(s, 1)
    ligar_tudo(mesa)
    res = cb.tick(sender=lambda *a, **k: retorno, tenant_id=TENANT)
    assert (res.enviados, res.incertos) == (0, 1)
    assert eventos(s, ids[0], cb.EVENT_DISPARO) == []
    assert eventos(s, ids[0], cb.EVENT_RESERVA)[0]["estado"] == "incerto"


def test_recusa_da_meta_devolve_o_lead_pra_fila(mesa, s):
    """A Meta RESPONDEU recusando: nada saiu, com certeza. O lead volta pra
    etapa de onde saiu e a reserva some (não gasta o teto)."""
    ids = fila(s, 1)
    ligar_tudo(mesa)

    def _recusado(telefone, texto, *, com_botao, **kw):
        raise RuntimeError("reengajar sem template aprovado — 400: template paused")

    res = cb.tick(sender=_recusado, tenant_id=TENANT)
    assert (res.enviados, res.falhas, res.incertos) == (0, 1, 0)
    assert etapa_de(s, ids[0]) == ETAPA_A
    assert eventos(s, ids[0], cb.EVENT_RESERVA) == []


def test_nao_conseguir_conectar_e_nada_saiu(mesa, s):
    ids = fila(s, 1)
    ligar_tudo(mesa)

    def _sem_rede(telefone, texto, *, com_botao, **kw):
        raise httpx.ConnectError("rede fora")

    res = cb.tick(sender=_sem_rede, tenant_id=TENANT)
    assert (res.falhas, res.incertos) == (1, 0)
    assert etapa_de(s, ids[0]) == ETAPA_A


def test_meta_aceita_sem_devolver_id_fica_sem_rastro_e_diz(mesa, s):
    """O emissor fabrica um id quando a Meta não devolve um. Esse id não vai
    pro disparo: nenhum status chegaria nele e o placar o contaria "em voo"
    pra sempre."""
    ids = fila(s, 1)
    ligar_tudo(mesa)

    def _sem_id(telefone, texto, *, com_botao, **kw):
        return {"ok": True, "dry_run": False, "sent": True, "wamid": "uuid-fabricado",
                "wamid_fabricado": True, "template": "t", "categoria": "UTILITY"}

    assert cb.tick(sender=_sem_id, tenant_id=TENANT).enviados == 1
    ev = eventos(s, ids[0], cb.EVENT_DISPARO)[0]
    assert ev["wamid"] is None and ev["sem_wamid"] is True


def test_registro_que_falha_depois_do_envio_guarda_o_wamid_na_reserva(mesa, s, monkeypatch):
    ids = fila(s, 1)
    ligar_tudo(mesa)
    monkeypatch.setattr(cb, "_apagar_reserva",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("banco piscou")))
    r = Remetente()
    res = volta(r)
    assert (res.enviados, res.sem_registro) == (0, 1)
    reserva = eventos(s, ids[0], cb.EVENT_RESERVA)[0]
    assert reserva["estado"] == "sem_registro"
    assert reserva["wamid"].startswith("wamid.garantia.")
    assert etapa_de(s, ids[0]) == cb.STAGE_DISPARADO


# ═════════════════════════════════════════════════════════════════════════════
# OPT-OUT — quem pediu pra parar não recebe e não gasta o lote
# ═════════════════════════════════════════════════════════════════════════════


def test_opt_out_gravado_depois_de_montada_a_fila_nao_recebe(mesa, s):
    """A fila foi lida; antes de chegar a vez do segundo lead ele pede pra
    parar. A conferência final, por mensagem, barra — sem reservar."""
    from app.services import opt_out

    ids = fila(s, 3)
    ligar_tudo(mesa)

    def _pede_pra_parar(n):
        if n == 1:
            with SessionLocal() as outra:
                opt_out.registrar(outra, f"{_PREFIXO}11", TENANT)
                outra.commit()

    r = Remetente(ao_enviar=_pede_pra_parar)
    res = volta(r)
    assert r.telefones == [f"{_PREFIXO}10", f"{_PREFIXO}12"]
    assert res.bloqueios == {"opt_out": 1}
    assert etapa_de(s, ids[1]) == ETAPA_A
    assert eventos(s, ids[1], cb.EVENT_RESERVA) == []


def test_opt_out_que_nao_da_pra_conferir_nao_envia(mesa, s, monkeypatch):
    """A última conferência de opt-out falha (banco, consulta). Dentro do
    emissor essa checagem falha ABERTA, pra não parar os outros motores; no
    disparo em massa, não conferi = não envio."""
    ids = fila(s, 3)
    ligar_tudo(mesa)

    def _nao_consigo(session, telefone):
        raise RuntimeError("consulta de opt-out falhou")

    monkeypatch.setattr(cb._opt_out, "esta_bloqueado", _nao_consigo)
    r = Remetente()
    res = volta(r)
    assert r.telefones == []
    assert res.motivo_parada == "autorizacao_indisponivel"
    assert [etapa_de(s, i) for i in ids] == [ETAPA_A] * 3


def test_outro_card_em_nao_perturbe_tira_o_lead_da_fila(mesa, s):
    """A terceira fonte do opt-out (um card do mesmo telefone em Não perturbe)
    não estava na fila do carrossel: o lead entrava no lote, era barrado só no
    envio, voltava pra fila e gastava um lugar do lote a cada volta."""
    from app.services import opt_out

    s.execute(_t("INSERT INTO pipeline_stages (id, tenant_id, nome, funil) VALUES "
                 "(:i, :t, 'Não perturbe', 'principal') ON CONFLICT (id) DO NOTHING"),
              {"i": opt_out.ETAPA_NAO_PERTURBE, "t": TENANT})
    s.commit()
    ids = fila(s, 3)
    # o MESMO telefone do primeiro da fila, em outro card, na Não perturbe
    s.execute(_t("INSERT INTO leads (tenant_id, nome, telefone, owner_id, stage_id, "
                 "created_at, updated_at) VALUES (:t, 'Outro card', :tel, 1, :st, now(), now())"),
              {"t": TENANT, "tel": f"{_PREFIXO}10", "st": opt_out.ETAPA_NAO_PERTURBE})
    s.commit()
    ligar_tudo(mesa)

    r = Remetente()
    res = volta(r)
    assert r.telefones == [f"{_PREFIXO}11", f"{_PREFIXO}12"]
    assert res.disponiveis == 2, "o lead bloqueado ocupou um lugar do lote"
    assert etapa_de(s, ids[0]) == ETAPA_A


# ═════════════════════════════════════════════════════════════════════════════
# REGISTRO — cada volta e cada mudança ficam escritas
# ═════════════════════════════════════════════════════════════════════════════


def _voltas_desde(s, inicio) -> list[tuple]:
    return s.execute(
        _t("SELECT ligado, motivo_parada, enviados, incertos FROM remarketing_tick_log "
           "WHERE tenant_id = :t AND em >= :i ORDER BY em, id"),
        {"t": TENANT, "i": inicio}).all()


def test_cada_volta_fica_registrada_ligada_ou_nao(mesa, s):
    inicio = s.execute(_t("SELECT now()")).scalar()
    fila(s, 3)
    r = Remetente()
    volta(r)                                            # desligado
    tela(mesa, "carrossel_corretor_ativo", True)
    volta(r)                                            # ligado, sem número
    papel(mesa, PID_CORRETOR, "disparo")
    volta(r)                                            # envia 3
    volta(r)                                            # fila vazia
    assert [tuple(v) for v in _voltas_desde(s, inicio)] == [
        (False, "desligado", 0, 0),
        (True, "sem_linha_disparadora", 0, 0),
        (True, None, 3, 0),
        (True, "fila_vazia", 0, 0),
    ]


def test_toda_mudanca_na_tela_fica_no_historico(mesa, s):
    tela(mesa, "remarketing_cap_diario", 40)
    tela(mesa, "remarketing_cap_diario", 60)
    mesa.delete("/painel/automacoes/config/remarketing_cap_diario", headers=ADMIN)
    hist = cfg.historico(["remarketing_cap_diario"], limite=3)
    assert [(h["acao"], h["antes"], h["depois"], h["autor"]) for h in hist] == [
        ("restaurar", 60, None, "dona"),
        ("definir", 40, 60, "dona"),
        ("definir", None, 40, "dona"),
    ]
    assert all(h["em"] for h in hist)


def test_o_mapa_da_tela_mostra_o_que_o_robo_fez(mesa, s):
    """A tela precisa de: a última volta, hoje aceito × entregue, as reservas
    sem disparo, a saúde de cada número e o histórico."""
    fila(s, 3)
    ligar_tudo(mesa)
    volta(Remetente())
    r = mesa.get("/painel/automacoes", headers=ADMIN)
    assert r.status_code == 200, r.text
    mapa = next(v for v in _achar_mapas(r.json()) if v.get("flag") == "carrossel_corretor_ativo")

    assert mapa["estado"] == "ligado"
    assert mapa["ultima_volta"]["ligado"] is True
    assert mapa["ultima_volta"]["enviados"] == 3
    hoje = mapa["ritmo"]["hoje"]
    assert hoje["aceitos"] >= 3 and hoje["entregues"] == 0
    assert hoje["sem_confirmacao_ainda"] >= 3
    assert mapa["reservas_orfas"]["total"] >= 0
    numeros = {n["slug"]: n for n in mapa["linha_disparadora"]["numeros"]}
    assert numeros["corretor"]["papel"] == "disparo" and "cor" in numeros["corretor"]["saude"]
    assert numeros["corretor"]["saude"] is not None
    assert mapa["saude_linha"]["de_qual"] == "disparador"
    assert any(h["chave"] == "carrossel_corretor_ativo" for h in mapa["historico"]["config"])


def _achar_mapas(obj):
    """Todo dicionário com a chave `flag` dentro da resposta do mapa."""
    if isinstance(obj, dict):
        if "flag" in obj:
            yield obj
        for v in obj.values():
            yield from _achar_mapas(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _achar_mapas(v)
