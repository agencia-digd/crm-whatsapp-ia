"""Carrossel da Corretor — o motor de remarketing em massa do 0000-0000.

São 5.253 leads e o telefone DA CASA. O que estes testes protegem, em ordem:
1. NADA sai com o carrossel desligado (o default).
2. A rampa manda no volume — erro nunca libera mais.
3. Opt-out nunca recebe.
4. Dois ticks não mandam 2× pro mesmo lead.
"""
from __future__ import annotations

import pytest

from app.services import carrossel_corretor as cb


class SenderFake:
    def __init__(self, explode=None):
        self.chamadas = []
        self.explode = explode

    def __call__(self, telefone, texto, *, com_botao, **kw):
        if self.explode:
            raise RuntimeError(self.explode)
        self.chamadas.append({"telefone": telefone, "texto": texto, **kw})
        return {"ok": True, "dry_run": True, "sent": False}


@pytest.fixture(autouse=True)
def _desligado_por_padrao(monkeypatch):
    # Desde 05/10/2026 o interruptor é SÓ o banco (`config_operacional`); o
    # `.env` não liga mais nada. Estes testes ligam e desligam por aqui.
    monkeypatch.setattr("app.services.config_operacional.carrossel_corretor_ativo",
                        lambda: False)
    yield


# ── 1. A trava mais importante: desligado é desligado ───────────────────────

def test_desligado_nao_faz_nada():
    """Default de produção. Nem consulta banco."""
    s = SenderFake()
    res = cb.tick(session=object(), sender=s)   # session inválida de propósito
    assert res.motivo_parada == "desligado"
    assert not s.chamadas


def test_flag_nasce_desligada():
    """Default do CÓDIGO, não do .env: deploy novo não sai disparando sozinho."""
    from app.config import Settings
    from app.services import config_operacional as cfg

    assert Settings.model_fields["carrossel_corretor_ativo"].default is False
    assert cfg.DEFAULTS["carrossel_corretor_ativo"]() is False


def test_env_NAO_liga_o_disparo(monkeypatch):
    """`CARROSSEL_CORRETOR_ATIVO=true` no `.env`, sem linha no banco: DESLIGADO.
    A fonte do interruptor é o banco — o painel manda, o arquivo não."""
    from app.config import settings
    from app.services import config_operacional as cfg

    monkeypatch.undo()                       # tira o patch do autouse
    monkeypatch.setattr(settings, "carrossel_corretor_ativo", True, raising=False)
    monkeypatch.setattr(cfg, "_carregar", lambda: {})
    monkeypatch.setattr(cfg, "_cache_erro", None)
    assert cfg.carrossel_corretor_ativo() is False


@pytest.mark.parametrize("lixo", ["true", "sim", 1, "1", [True], {"on": 1}])
def test_so_true_booleano_liga(monkeypatch, lixo):
    """Valor gravado por fora da tela que não é `true`: desligado."""
    from app.services import config_operacional as cfg

    monkeypatch.undo()
    monkeypatch.setattr(cfg, "_carregar", lambda: {"carrossel_corretor_ativo": lixo})
    monkeypatch.setattr(cfg, "_cache_erro", None)
    assert cfg.carrossel_corretor_ativo() is False


# ── 2. A rampa manda no volume ──────────────────────────────────────────────

def test_cap_override_fura_o_amarelo_so_neste_tick(monkeypatch):
    """Lote autorizado passa cap_override; o timer de 3 min não tem esse argumento."""
    import inspect

    sig = inspect.signature(cb.tick)
    assert "cap_override" in sig.parameters
    assert sig.parameters["cap_override"].default is None


def test_cap_zero_nao_envia(monkeypatch):
    """Qualidade RED/FLAGGED → rampa devolve 0 → carrossel para."""
    monkeypatch.setattr("app.services.config_operacional.carrossel_corretor_ativo", lambda: True)
    monkeypatch.setattr(cb, "travar", lambda ns, t: None)
    monkeypatch.setattr(cb, "_voltar_pra_fila", lambda s, t: 0)

    from app.services import rampa

    monkeypatch.setattr(
        rampa, "cap_de_hoje",
        lambda s, slug, cid, **k: rampa.Rampa(slug, 0, 0, 0, "RED", "qualidade RED — linha parada"),
    )
    s = SenderFake()
    res = cb.tick(session=object(), sender=s)
    assert res.enviados == 0
    assert not s.chamadas
    assert "RED" in (res.motivo_parada or "")


def test_contagem_falha_assume_cheio(monkeypatch):
    """FAIL-SAFE: não deu pra contar o que já saiu hoje → assume cap cheio.
    Erro nunca libera volume."""
    class SessaoQuebrada:
        def execute(self, *a, **k):
            raise RuntimeError("banco fora")

    assert cb._enviados_hoje(SessaoQuebrada(), 1) > 100000


# ── 3. Tick concorrente ─────────────────────────────────────────────────────

def test_tick_concorrente_nao_envia(monkeypatch):
    from app.services import trava_tick

    monkeypatch.setattr("app.services.config_operacional.carrossel_corretor_ativo", lambda: True)
    monkeypatch.setattr(cb, "travar", lambda ns, t: trava_tick.OCUPADO)
    s = SenderFake()
    res = cb.tick(session=object(), sender=s)
    assert res.motivo_parada == "tick_concorrente"
    assert not s.chamadas


def test_namespace_do_carrossel_e_proprio():
    from app.services import trava_tick as tt

    todos = [tt.NS_CADENCIA, tt.NS_NUTRICAO, tt.NS_MSG_PLANEJADA, tt.NS_CARROSSEL]
    assert len(set(todos)) == 4, "namespace repetido faz um motor calar o outro"


# ── 4. O desenho da roda ────────────────────────────────────────────────────

def test_etapas_do_carrossel():
    """Fila → Disparado → volta. Respondeu sai da roda."""
    assert (cb.STAGE_FILA_PADRAO, cb.STAGE_DISPARADO, cb.STAGE_RESPONDEU) == (23, 24, 25)
    assert cb.DESCANSO_DIAS >= 15, "descanso curto demais = lead recebendo direto"


def test_sql_da_fila_exclui_opt_out():
    """Quem pediu pra parar nunca pode entrar na roda — as TRÊS fontes do
    predicado canônico (`opt_out.clausula`): disparo legado, cadência e a
    etapa Não perturbe."""
    from app.services import opt_out

    sql = cb._SQL_FILA
    assert "disparos_reengajamento" in sql and "opt_out IS TRUE" in sql
    assert "cadencia_estado" in sql and "'opt_out'" in sql
    assert f"lx.stage_id = {opt_out.ETAPA_NAO_PERTURBE}" in sql
    assert opt_out.clausula("l.telefone") in sql


def test_fila_e_fifo():
    """Quem está na fila há mais tempo sai primeiro."""
    assert "ORDER BY l.updated_at" in cb._SQL_FILA


def test_marcar_respondeu_so_tira_de_fila_e_disparado():
    """Quem já saiu da roda (ou nem era da Corretor) não é mexido."""
    sql = cb.marcar_respondeu.__doc__ or ""
    fonte = __import__("inspect").getsource(cb.marcar_respondeu)
    # a fila de sempre (23), a configurada na tela e Disparado — nada além
    assert "stage_id IN (:fila_padrao, :fila, :disp)" in fonte
    assert "owner_id IN (:corretor, :dono)" in fonte, "não pode mexer em lead de outra corretora"


# ── 4b. Ritmo: o cap do dia se dilui na janela, não sai em rajada ───────────

def _sp(h, m=0):
    from datetime import datetime

    return datetime(2026, 7, 30, h, m, tzinfo=cb.TZ_SP)


@pytest.mark.parametrize("hora,esperado", [
    (7, 0),    # antes de abrir
    (9, 0),    # abrindo
    (18, 20),  # fechou: cota cheia
    (22, 20),  # depois não libera mais que o cap
])
def test_cota_respeita_a_janela(hora, esperado):
    assert cb._cota_ate_agora(20, _sp(hora)) == esperado


def test_cota_cresce_ao_longo_do_dia():
    horas = [cb._cota_ate_agora(20, _sp(h)) for h in range(9, 19)]
    assert horas == sorted(horas), "cota não pode diminuir no meio do dia"


def test_cota_concentra_almoco_e_fim_do_dia():
    """Dona 06/09: almoço e 17h respondem ~50% mais. A curva libera o volume aí."""
    cap = 100
    assert cb._cota_ate_agora(cap, _sp(9)) == 0
    assert cb._cota_ate_agora(cap, _sp(12)) == 10
    assert cb._cota_ate_agora(cap, _sp(14)) == 60
    assert cb._cota_ate_agora(cap, _sp(17)) == 75
    assert cb._cota_ate_agora(cap, _sp(18)) == 100
    # 12-14 é onde mais libera: 50 pontos em 2h, vs 10 em 3h de manhã
    assert cb._cota_ate_agora(cap, _sp(14)) - cb._cota_ate_agora(cap, _sp(12)) == 50


def test_fora_do_horario_nao_manda():
    """Remarketing às 22h é o que gera denúncia — e denúncia é o que mata o número."""
    for hora in (0, 6, 8, 19, 23):
        assert cb._cota_ate_agora(60, _sp(hora)) in (0, 60)
        if hora < cb.HORA_INICIO:
            assert cb._cota_ate_agora(60, _sp(hora)) == 0


def test_teto_por_tick_impede_rajada():
    """Mesmo com cota cheia e fila enorme, um tick não despeja o dia inteiro."""
    assert cb.MAX_POR_TICK <= 5


# ── 5. Inbound: quem responde sai, quem pede pra parar nunca volta ──────────

def test_inbound_opt_out_grava_e_tira_da_roda(monkeypatch):
    from app.services import opt_out

    assert opt_out.is_opt_out("PARAR") is True, "premissa: 'PARAR' é opt-out"

    chamadas = {}
    monkeypatch.setattr(cb, "_gravar_opt_out",
                        lambda s, tel, t: chamadas.setdefault("opt_out", tel))
    monkeypatch.setattr(cb, "marcar_respondeu", lambda s, tel, t=1: 1)
    assert cb.processar_inbound("+5500000000000", "PARAR") == "opt_out"
    assert chamadas.get("opt_out") == "+5500000000000"


def test_inbound_resposta_normal_nao_gera_opt_out(monkeypatch):
    """Responder "quero ver" tira da roda mas NÃO bloqueia o lead pra sempre."""
    monkeypatch.setattr(cb, "_gravar_opt_out",
                        lambda *a: pytest.fail("resposta normal virou opt-out"))
    # (05/10/2026) quem move o card agora é `aplicar_resposta`, pela classe.
    monkeypatch.setattr(cb, "aplicar_resposta", lambda *a, **k: 1)
    assert cb.processar_inbound("+5500000000000", "quero ver sim") == "respondeu"


def test_inbound_nunca_levanta(monkeypatch):
    """Inbound que explode = mensagem de lead perdida. Engole e loga."""
    monkeypatch.setattr(cb, "marcar_respondeu",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert cb.processar_inbound("+5500000000000", "oi") is None


def test_inbound_so_e_chamado_na_linha_da_corretor():
    """A Pessoa12/Pessoa10/Pessoa11 não podem ter lead movido pro funil da Corretor."""
    import inspect

    from app.api import cloud_corretora

    fonte = inspect.getsource(cloud_corretora.corretora_inbound)
    assert "slug == cb.SLUG_CORRETOR" in fonte


def test_linha_ausente_levanta_em_vez_de_mandar_pela_corretor(monkeypatch):
    """Sem linha disparadora, NÃO cai na Pessoa12 (nem na Corretor) por acidente — melhor
    não enviar. Desde 05/10 `_disparar` nem procura linha: recebe a que o tick
    resolveu pela marca `disparo`, ou não manda."""
    with pytest.raises(RuntimeError, match="sem linha disparadora"):
        cb._disparar(object(), {"id": 1}, "+5500000000000", SenderFake(), 1)


# ── 6. Claim antes do envio (achados 1 e 2 da auditoria do Grok, 02/08) ─────
# O padrão perigoso do módulo era: manda na Meta → só então grava. Morrer no
# meio (restart, deploy) deixava a pessoa "recebida mas ainda na Fila", e o
# tick seguinte mandava de novo.

class _SessionFake:
    """Session mínima: só registra o que o tick pediu, na ordem."""

    def __init__(self, fila):
        self._fila = fila
        self.ordem: list[str] = []

    def execute(self, *a, **k):
        self.ordem.append("execute")
        return self

    def mappings(self): return self
    def all(self): return self._fila
    def scalar(self): return 0
    def commit(self): self.ordem.append("commit")
    def rollback(self): self.ordem.append("rollback")
    def close(self): pass


def _tick_pronto_pra_disparar(monkeypatch, fila):
    """Deixa o tick passar por todos os portões até o loop de disparo."""
    from app.services import rampa

    monkeypatch.setattr("app.services.config_operacional.carrossel_corretor_ativo", lambda: True)
    monkeypatch.setattr(cb, "travar", lambda ns, t: None)
    monkeypatch.setattr(cb, "_voltar_pra_fila", lambda s, t: 0)
    monkeypatch.setattr(cb, "_enviados_hoje", lambda s, t: 0)
    monkeypatch.setattr(cb, "_cota_ate_agora", lambda cap, agora=None: cap)
    monkeypatch.setattr(rampa, "cap_de_hoje",
                        lambda s, slug, cid, **k: rampa.Rampa("corretor", 10, 0, 9, "GREEN", "ok"))
    return _SessionFake(fila)


def test_reserva_acontece_ANTES_de_a_mensagem_sair(monkeypatch):
    """O claim tem que vir primeiro — senão morrer no meio duplica o envio."""
    ordem = []
    ses = _tick_pronto_pra_disparar(monkeypatch, [{"id": 1, "telefone": "+5500000000000"}])
    monkeypatch.setattr(cb, "_reservar", lambda s, i, t, *a: ordem.append("reservou") or True)
    monkeypatch.setattr(cb, "_disparar", lambda *a, **k: ordem.append("enviou"))

    cb.tick(session=ses, sender=SenderFake())
    assert ordem == ["reservou", "enviou"], f"ordem errada: {ordem}"


def test_lead_ja_reservado_por_outro_tick_nao_recebe(monkeypatch):
    """Dois ticks lendo a mesma fila: só quem venceu o UPDATE dispara."""
    ses = _tick_pronto_pra_disparar(monkeypatch, [{"id": 1, "telefone": "+5500000000000"}])
    monkeypatch.setattr(cb, "_reservar", lambda s, i, t, *a: False)   # outro pegou
    monkeypatch.setattr(cb, "_disparar",
                        lambda *a, **k: pytest.fail("disparou lead que não reservou"))

    res = cb.tick(session=ses, sender=SenderFake())
    assert res.enviados == 0


def test_falha_ANTES_do_envio_devolve_pra_fila(monkeypatch):
    """Nada saiu → o lead volta e tenta de novo no próximo tick."""
    devolvidos = []
    ses = _tick_pronto_pra_disparar(monkeypatch, [{"id": 7, "telefone": "+5500000000000"}])
    monkeypatch.setattr(cb, "_reservar", lambda s, i, t, *a: True)
    monkeypatch.setattr(cb, "_devolver_pra_fila", lambda s, i, t, *a: devolvidos.append(i))

    def _falha_antes(*a, **k):
        raise cb._NadaSaiu("template inválido")
    monkeypatch.setattr(cb, "_disparar", _falha_antes)

    cb.tick(session=ses, sender=SenderFake())
    assert devolvidos == [7]


def test_falha_DEPOIS_do_envio_nao_devolve_pra_fila(monkeypatch):
    """A mensagem pode ter saído — devolver arriscaria mandar duas vezes.
    Perde-se a linha do placar, não a pessoa."""
    ses = _tick_pronto_pra_disparar(monkeypatch, [{"id": 7, "telefone": "+5500000000000"}])
    monkeypatch.setattr(cb, "_reservar", lambda s, i, t, *a: True)
    monkeypatch.setattr(cb, "_devolver_pra_fila",
                        lambda *a: pytest.fail("devolveu lead que já pode ter recebido"))

    def _falha_ao_gravar(*a, **k):
        raise RuntimeError("INSERT lead_events falhou")
    monkeypatch.setattr(cb, "_disparar", _falha_ao_gravar)

    res = cb.tick(session=ses, sender=SenderFake())
    assert res.falhas == 1


def test_trava_indisponivel_NAO_dispara(monkeypatch):
    """Emissor em massa falha FECHADO: sem trava, dois ticks dobram o volume
    do dia e a Meta lê rajada como spam. Perder 3 min é mais barato."""
    from app.services import trava_tick

    monkeypatch.setattr("app.services.config_operacional.carrossel_corretor_ativo", lambda: True)
    monkeypatch.setattr(cb, "travar", lambda ns, t: trava_tick.INDISPONIVEL)
    s = SenderFake()
    res = cb.tick(session=object(), sender=s)
    assert res.motivo_parada == "trava_indisponivel"
    assert not s.chamadas


def test_erro_na_trava_vira_INDISPONIVEL_e_nao_None(monkeypatch):
    """A distinção que sustenta o teste acima: erro ≠ 'banco não é Postgres'."""
    from app.services import trava_tick

    class _EngineRuim:
        dialect = type("d", (), {"name": "postgresql"})()

        def connect(self):
            raise RuntimeError("too many connections")

    import app.db.session as dbs
    monkeypatch.setattr(dbs, "engine", _EngineRuim())
    assert trava_tick.travar(trava_tick.NS_CARROSSEL, 1) is trava_tick.INDISPONIVEL
