"""Vigia de automação dormindo: avisa timer parado, calado em fim de semana,
interruptor desligado e fora da janela, e não repete o aviso.

Sem systemd, sem banco, sem Telegram: leitura, interruptores e envio são mock.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.services import vigia_automacao as vg
from app.services.vigia_automacao import Leitura, TimerVigiado

UTC = timezone.utc
CADENCIA = TimerVigiado("copiloto-cadencia-tick", 3, (12, 21), True, "cadencia_ativa")
NOSHOW = TimerVigiado("copiloto-noshow", 60, None, False, "no_show_ativo")

QUARTA_15H = datetime(2026, 10, 21, 15, 0, tzinfo=UTC)   # dia útil, dentro da janela
SABADO_15H = datetime(2026, 10, 24, 15, 0, tzinfo=UTC)


class Envios:
    def __init__(self, ok: bool = True) -> None:
        self.ok, self.mensagens = ok, []

    def __call__(self, task_id: str, texto: str) -> bool:
        self.mensagens.append((task_id, texto))
        return self.ok


def volta(agora, estado, ultimo_tick, *, timers=(CADENCIA,), ligado=True, uteis=True, envios=None,
          ativo=True):
    envios = envios if envios is not None else Envios()
    res = vg.rodar(agora, estado, ler=lambda nome: Leitura(ativo, ultimo_tick),
                   interruptor=lambda chave: ligado, dias_uteis_ligado=uteis,
                   enviar=envios, timers=timers)
    return res, envios


def test_timer_parado_em_dia_util_avisa(tmp_path):
    estado = tmp_path / "estado.json"
    res, envios = volta(QUARTA_15H, estado, QUARTA_15H - timedelta(minutes=40))
    assert res[0].estado == vg.DORMINDO and res[0].parado_min == 40
    assert len(envios.mensagens) == 1
    assert "copiloto-cadencia-tick" in envios.mensagens[0][1]


def test_timer_em_dia_nao_avisa(tmp_path):
    res, envios = volta(QUARTA_15H, tmp_path / "e.json", QUARTA_15H - timedelta(minutes=5))
    assert res[0].estado == vg.OK and envios.mensagens == []


def test_mesmo_timer_no_sabado_nao_avisa(tmp_path):
    res, envios = volta(SABADO_15H, tmp_path / "e.json", SABADO_15H - timedelta(days=2))
    assert res[0].estado == vg.FIM_DE_SEMANA and envios.mensagens == []


def test_sabado_avisa_se_trava_de_dia_util_estiver_desligada(tmp_path):
    res, envios = volta(SABADO_15H, tmp_path / "e.json", SABADO_15H - timedelta(hours=2), uteis=False)
    assert res[0].estado == vg.DORMINDO and len(envios.mensagens) == 1


def test_interruptor_desligado_nao_avisa(tmp_path):
    res, envios = volta(QUARTA_15H, tmp_path / "e.json", QUARTA_15H - timedelta(hours=5), ligado=False)
    assert res[0].estado == vg.DESLIGADO and envios.mensagens == []


def test_timer_desativado_no_systemd_nao_avisa(tmp_path):
    res, envios = volta(QUARTA_15H, tmp_path / "e.json", None, ativo=False)
    assert res[0].estado == vg.DESATIVADO and envios.mensagens == []


def test_segundo_tick_nao_repete_e_reaviso_so_depois_de_6h(tmp_path):
    estado = tmp_path / "estado.json"
    parado_desde = QUARTA_15H - timedelta(minutes=40)
    _, envios = volta(QUARTA_15H, estado, parado_desde)
    assert len(envios.mensagens) == 1

    _, envios2 = volta(QUARTA_15H + timedelta(minutes=10), estado, parado_desde)
    assert envios2.mensagens == []                      # segundo tick: calado

    _, envios3 = volta(QUARTA_15H + timedelta(hours=5, minutes=50), estado, parado_desde, timers=(NOSHOW,))
    assert len(envios3.mensagens) == 1                  # outro timer parado: avisa (estado é por timer)

    tarde = QUARTA_15H + timedelta(hours=6, minutes=1)
    res4, envios4 = volta(tarde, estado, parado_desde)
    # 21:01 UTC ainda está na janela (12-21): passou de 6 h → reavisa
    assert res4[0].estado == vg.DORMINDO and len(envios4.mensagens) == 1


def test_voltou_ao_normal_e_caiu_de_novo_avisa_na_hora(tmp_path):
    estado = tmp_path / "estado.json"
    volta(QUARTA_15H, estado, QUARTA_15H - timedelta(minutes=40))
    volta(QUARTA_15H + timedelta(minutes=10), estado, QUARTA_15H + timedelta(minutes=9))   # voltou
    outra = QUARTA_15H + timedelta(minutes=30)
    _, envios = volta(outra, estado, QUARTA_15H + timedelta(minutes=9))
    assert len(envios.mensagens) == 1


def test_envio_que_falha_nao_marca_como_avisado(tmp_path):
    estado = tmp_path / "estado.json"
    parado = QUARTA_15H - timedelta(minutes=40)
    _, falhou = volta(QUARTA_15H, estado, parado, envios=Envios(ok=False))
    assert len(falhou.mensagens) == 1
    _, de_novo = volta(QUARTA_15H + timedelta(minutes=10), estado, parado)
    assert len(de_novo.mensagens) == 1                   # tentou de novo


def test_fora_da_janela_nao_avisa_e_janela_conta_da_abertura(tmp_path):
    noite = datetime(2026, 10, 21, 23, 0, tzinfo=UTC)
    res, envios = volta(noite, tmp_path / "e.json", noite - timedelta(hours=8))
    assert res[0].estado == vg.FORA_DA_JANELA and envios.mensagens == []

    # 12:04 UTC: último tick foi ontem 21:57, mas a janela abriu há 4 min → ainda não é dormindo
    abre = datetime(2026, 10, 22, 12, 4, tzinfo=UTC)
    res2, envios2 = volta(abre, tmp_path / "e2.json", datetime(2026, 10, 21, 21, 57, tzinfo=UTC))
    assert res2[0].estado == vg.OK and envios2.mensagens == []
    # 12:10 UTC e nada de tick desde a abertura (> 6 min) → dormindo
    res3, _ = volta(datetime(2026, 10, 22, 12, 10, tzinfo=UTC), tmp_path / "e3.json",
                    datetime(2026, 10, 21, 21, 57, tzinfo=UTC))
    assert res3[0].estado == vg.DORMINDO


def test_um_aviso_por_volta_junta_os_timers_parados(tmp_path):
    res, envios = volta(QUARTA_15H, tmp_path / "e.json", QUARTA_15H - timedelta(hours=3),
                        timers=(CADENCIA, NOSHOW))
    assert len(envios.mensagens) == 1
    texto = envios.mensagens[0][1]
    assert "cadencia-tick" in texto and "noshow" in texto
