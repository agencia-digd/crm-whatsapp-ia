"""Mensagem que saiu e não foi registrada — o teto parou de vazar.

O laudo de 05/10/2026 achou (`carrossel_corretor.py`, bloco "falhou DEPOIS do
envio"): se a gravação do `carrossel_disparo` falhasse depois de a Meta aceitar
a mensagem, o lead ficava em Disparado SEM evento. Como o teto do dia conta
evento, aquela mensagem não contava — e o dia fechava um acima do limite a cada
falha dessas.

Fechado com uma RESERVA gravada na mesma transação do claim, antes do envio:

    claim + carrossel_reserva  →  envia  →  apaga a reserva + grava o disparo
                                     │
                    nada saiu ───────┴──→ apaga a reserva, devolve o lead

Se o último passo falhar, a reserva FICA — e ela conta no teto.

Tudo aqui roda contra banco de verdade, com `_reservar` e `_disparar` reais.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text as _t

from app.db.session import SessionLocal
from app.services import carrossel_corretor as cb
from app.services import config_operacional as cfg
from app.services import remarketing_agenda as ag

TENANT = 1
_PREFIXO = "5500000000000"


class _NaoSerializavel:
    """Vai no campo `template` do retorno do sender. O envio "aconteceu"; a
    gravação do evento quebra no `json.dumps` — depois do envio, que é
    exatamente a janela do defeito."""


def _sender_que_envia_e_quebra_o_registro(telefone, texto, *, com_botao, **kw):
    return {"ok": True, "dry_run": False, "sent": True, "wamid": "wamid.saiu",
            "template": _NaoSerializavel(), "categoria": "UTILITY"}


def _sender_ok(telefone, texto, *, com_botao, **kw):
    return {"ok": True, "dry_run": False, "sent": True, "wamid": "wamid.ok",
            "template": "acme_corretor_ola_a_01", "categoria": "UTILITY"}


def _sender_dry_run(telefone, texto, *, com_botao, **kw):
    return {"ok": True, "dry_run": True, "sent": False, "wamid": "dry-x",
            "template": None}


def _sender_recusado(telefone, texto, *, com_botao, **kw):
    raise RuntimeError("(#131026) Message undeliverable")


def _lead(s, sufixo: str, etapa: int | None = None) -> int:
    lid = s.execute(
        _t("""INSERT INTO leads (tenant_id, nome, telefone, owner_id, stage_id,
                                 created_at, updated_at)
              VALUES (:t, 'Teste Reserva', :tel, :o, :st, now(), now())
           RETURNING id"""),
        {"t": TENANT, "tel": f"{_PREFIXO}{sufixo}", "o": cb.CORRETORA_CORRETOR,
         "st": etapa or cb.STAGE_FILA_PADRAO}).scalar()
    s.commit()
    return int(lid)


def _etapa_de(s, lead_id: int) -> int:
    return int(s.execute(_t("SELECT stage_id FROM leads WHERE id = :i"),
                         {"i": lead_id}).scalar())


def _conta(s, lead_id: int, tipo: str) -> int:
    return int(s.execute(_t("SELECT count(*) FROM lead_events "
                            "WHERE lead_id = :i AND tipo = :tp"),
                         {"i": lead_id, "tp": tipo}).scalar())


def _meus_de_hoje(s) -> int:
    """O que `_enviados_hoje` contaria SÓ dos leads destes testes — o banco de
    teste é compartilhado com o resto da suíte."""
    return int(s.execute(_t(
        """SELECT count(*) FROM lead_events le JOIN leads l ON l.id = le.lead_id
            WHERE l.telefone LIKE :p AND le.tipo IN (:d, :r)
              AND (le.at AT TIME ZONE 'America/Sao_Paulo')::date
                = (now() AT TIME ZONE 'America/Sao_Paulo')::date"""),
        {"p": f"{_PREFIXO}%", "d": cb.EVENT_DISPARO, "r": cb.EVENT_RESERVA}).scalar())


@pytest.fixture()
def s():
    def _limpar(ses):
        ses.execute(_t("DELETE FROM lead_events WHERE lead_id IN "
                       "(SELECT id FROM leads WHERE telefone LIKE :p)"),
                    {"p": f"{_PREFIXO}%"})
        ses.execute(_t("DELETE FROM leads WHERE telefone LIKE :p"), {"p": f"{_PREFIXO}%"})
        ses.execute(_t("DELETE FROM config_operacional"))
        ses.commit()
        cfg.invalidar_cache()

    with SessionLocal() as ses:
        for i, nome in ((cb.STAGE_FILA_PADRAO, "Fila"), (cb.STAGE_DISPARADO, "Disparado")):
            ses.execute(_t("INSERT INTO pipeline_stages (id, tenant_id, nome, funil) "
                           "VALUES (:i, :t, :n, 'remarketing') ON CONFLICT (id) DO NOTHING"),
                        {"i": i, "t": TENANT, "n": nome})
        ses.commit()
        _limpar(ses)
        # Só os leads destes testes na Fila: tira do caminho qualquer resto de
        # outro arquivo da suíte (devolvido no fim).
        outros = [r[0] for r in ses.execute(_t(
            "SELECT id FROM leads WHERE stage_id = :f AND tenant_id = :t"),
            {"f": cb.STAGE_FILA_PADRAO, "t": TENANT}).all()]
        if outros:
            ses.execute(_t("UPDATE leads SET stage_id = NULL WHERE id = ANY(:ids)"),
                        {"ids": outros})
            ses.commit()
        yield ses
        ses.rollback()
        if outros:
            ses.execute(_t("UPDATE leads SET stage_id = :f WHERE id = ANY(:ids)"),
                        {"f": cb.STAGE_FILA_PADRAO, "ids": outros})
        _limpar(ses)


@pytest.fixture()
def roda_ligada(monkeypatch, s):
    """Ligado, rampa folgada, sem ritmo. `_enviados_hoje` conta SÓ os leads
    destes testes, com a MESMA regra do motor (disparo + reserva) — a função
    real é provada à parte em `test_enviados_hoje_conta_a_reserva`."""
    from app.services import rampa

    cfg.definir("carrossel_corretor_ativo", True, autor="teste")
    monkeypatch.setattr(cb, "travar", lambda ns, t: None)
    monkeypatch.setattr(cb, "soltar", lambda *a, **k: None)
    monkeypatch.setattr(cb, "_cota_ate_agora", lambda cap, agora=None, janela=None: cap)
    monkeypatch.setattr(cb, "_enviados_hoje", lambda ses, t: _meus_de_hoje(ses))
    return rampa


def _teto(monkeypatch, rampa, n: int) -> None:
    monkeypatch.setattr(rampa, "cap_de_hoje",
                        lambda ses, slug, cid, **k: rampa.Rampa(slug, n, 0, 9, "GREEN", "ok"))


# ── O defeito do laudo ───────────────────────────────────────────────────────


def test_mensagem_que_saiu_sem_registro_CONTA_no_teto(s, roda_ligada, monkeypatch):
    """Teto de 2. A primeira sai e o registro quebra. Antes: o dia ficava em 0
    contados e saíam mais 2 (3 no total, um acima do teto). Agora: a reserva
    conta 1 e só sai mais 1."""
    _teto(monkeypatch, roda_ligada, 2)
    a, b, c = (_lead(s, "001"), _lead(s, "002"), _lead(s, "003"))

    res1 = cb.tick(session=s, sender=_sender_que_envia_e_quebra_o_registro,
                   max_por_tick=1)

    assert res1.falhas == 1 and res1.sem_registro == 1
    assert res1.enviados == 0, "não registrou — não pode dizer que enviou"
    assert _etapa_de(s, a) == cb.STAGE_DISPARADO, "a mensagem pode ter saído: não volta pra fila"
    assert _conta(s, a, cb.EVENT_DISPARO) == 0
    assert _conta(s, a, cb.EVENT_RESERVA) == 1, "a reserva é o rastro do que saiu"
    assert _meus_de_hoje(s) == 1, "o que saiu sem registro tem que contar no dia"

    # O resto do dia: sobra 1 do teto de 2, não 2.
    saidas = []

    def _conta_saidas(telefone, texto, *, com_botao, **kw):
        saidas.append(telefone)
        return _sender_ok(telefone, texto, com_botao=com_botao)

    res2 = cb.tick(session=s, sender=_conta_saidas, max_por_tick=10)
    res3 = cb.tick(session=s, sender=_conta_saidas, max_por_tick=10)

    assert len(saidas) == 1, f"o teto de 2 vazou: saíram {1 + len(saidas)} no dia"
    assert res3.motivo_parada == "cap_do_dia_atingido"
    assert _etapa_de(s, c) == cb.STAGE_FILA_PADRAO


def test_enviados_hoje_conta_a_reserva(s):
    """A função real do motor: disparo registrado + reserva que sobrou."""
    antes = cb._enviados_hoje(s, TENANT)
    lead = _lead(s, "011")
    assert cb._reservar(s, lead, TENANT) is True
    assert cb._enviados_hoje(s, TENANT) == antes + 1


def test_semana_tambem_conta_a_reserva(s):
    antes = ag.enviados_na_semana(s, TENANT, cb.EVENTOS_DE_SAIDA)
    lead = _lead(s, "012")
    cb._reservar(s, lead, TENANT)
    assert ag.enviados_na_semana(s, TENANT, cb.EVENTOS_DE_SAIDA) == antes + 1
    # e o formato antigo (um tipo só, em texto) continua aceito
    assert ag.enviados_na_semana(s, TENANT, cb.EVENT_DISPARO) >= 0


# ── O caminho bom não mudou ──────────────────────────────────────────────────


def test_envio_registrado_vira_UM_disparo_e_nenhuma_reserva(s, roda_ligada, monkeypatch):
    """Reserva + disparo contariam 2 pro mesmo envio e o teto fecharia na metade."""
    _teto(monkeypatch, roda_ligada, 10)
    lead = _lead(s, "021")

    res = cb.tick(session=s, sender=_sender_ok, max_por_tick=1)

    assert res.enviados == 1 and res.sem_registro == 0
    assert _conta(s, lead, cb.EVENT_DISPARO) == 1
    assert _conta(s, lead, cb.EVENT_RESERVA) == 0
    assert _meus_de_hoje(s) == 1


@pytest.mark.parametrize("sender", [_sender_dry_run, _sender_recusado])
def test_nada_saiu_nao_deixa_reserva_nem_conta(s, roda_ligada, monkeypatch, sender):
    """Dry-run e recusa da Meta: nada saiu. O lead volta pra fila e o dia não
    gasta — reserva esquecida aqui travaria o teto com mensagem que nunca foi."""
    _teto(monkeypatch, roda_ligada, 10)
    lead = _lead(s, "031")

    res = cb.tick(session=s, sender=sender, max_por_tick=1)

    assert res.enviados == 0 and res.falhas == 1 and res.sem_registro == 0
    assert _etapa_de(s, lead) == cb.STAGE_FILA_PADRAO
    assert _conta(s, lead, cb.EVENT_RESERVA) == 0
    assert _conta(s, lead, cb.EVENT_DISPARO) == 0
    assert _meus_de_hoje(s) == 0


def test_placar_de_copy_nao_conta_reserva_como_enviado(s, roda_ligada, monkeypatch):
    """A reserva segura o TETO; não vira 'enviado' em relatório. O placar só
    conta `carrossel_disparo`."""
    _teto(monkeypatch, roda_ligada, 10)
    lead = _lead(s, "041")
    antes = sum(v["enviados"] for v in cb.placar(session=s, dias=1))

    cb.tick(session=s, sender=_sender_que_envia_e_quebra_o_registro, max_por_tick=1)

    assert _conta(s, lead, cb.EVENT_RESERVA) == 1
    assert sum(v["enviados"] for v in cb.placar(session=s, dias=1)) == antes


# ── A reserva também segura o descanso ───────────────────────────────────────


def test_lead_sem_registro_descansa_os_30_dias(s, roda_ligada, monkeypatch):
    """Antes: lead sem evento ficava em Disparado pra sempre (primeiro disparo)
    ou voltava cedo demais (contando do disparo anterior)."""
    _teto(monkeypatch, roda_ligada, 10)
    lead = _lead(s, "051")
    # um disparo antigo, de 40 dias atrás
    s.execute(_t("""INSERT INTO lead_events (tenant_id, lead_id, tipo, payload, at)
                    VALUES (:t, :i, :tp, '{}'::jsonb, now() - interval '40 days')"""),
              {"t": TENANT, "i": lead, "tp": cb.EVENT_DISPARO})
    s.commit()

    cb.tick(session=s, sender=_sender_que_envia_e_quebra_o_registro, max_por_tick=1)
    assert _etapa_de(s, lead) == cb.STAGE_DISPARADO

    # Mesmo com um disparo de 40 dias atrás, a saída de HOJE é que conta.
    assert cb._voltar_pra_fila(s, TENANT) == 0
    assert _etapa_de(s, lead) == cb.STAGE_DISPARADO

    # 31 dias depois da reserva, volta — não fica preso.
    s.execute(_t("UPDATE lead_events SET at = now() - interval '31 days' "
                 "WHERE lead_id = :i AND tipo = :tp"),
              {"i": lead, "tp": cb.EVENT_RESERVA})
    s.commit()
    assert cb._voltar_pra_fila(s, TENANT) == 1
    assert _etapa_de(s, lead) == cb.STAGE_FILA_PADRAO


def test_reserva_e_claim_sao_a_mesma_transacao(s):
    """Claim sem reserva é a janela do defeito; reserva sem claim é teto gasto
    à toa. Lead que não está mais na fila: nem um, nem outro."""
    lead = _lead(s, "061", etapa=cb.STAGE_DISPARADO)   # já saiu da fila
    assert cb._reservar(s, lead, TENANT) is False
    assert _conta(s, lead, cb.EVENT_RESERVA) == 0
