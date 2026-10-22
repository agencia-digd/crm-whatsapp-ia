"""Automações MULTI-LINHA — ligar as 2 automações da Pessoa12 também na Pessoa10 (27/07).

O que este arquivo trava (cada teste = um requisito da Dona):

  1. roteamento: o envio sai pela linha da CORRETORA DONA do lead (Pessoa12 pela 2591,
     Pessoa10 pela linha dela) — nunca pela linha da outra;
  2. fail-closed: corretora SEM linha Cloud (Pessoa11, ainda no Evolution) não
     dispara NADA — sem exception, silencioso;
  3. CAP DIÁRIO POR LINHA (o requisito mais caro): a Pessoa10 estourando a cota dela
     NÃO bloqueia a Pessoa12, e vice-versa. Antes o cap era global e a Pessoa10 comeria a
     cota da Pessoa12 — cuja linha tomou 2 bans em julho e opera a ≤25/dia;
  4. boas-vindas: sai pela linha da dona, e vale pra toda corretora com linha
     (deixou de ser a lista fixa `BOAS_VINDAS_CORRETORAS = (3,)`);
  5. auto-enroll no Follow-up: funciona pra Pessoa10, não só pra Pessoa12.

SEGURANÇA: o sender é SEMPRE fake e o registry de linhas é montado à mão — ZERO
chamada de rede, nenhuma linha real é tocada. Nenhum teste aqui chega perto da
Graph API (ver CLAUDE.md regra #9).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import BigInteger, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from app.db.models import (
    Base, CadenciaEstado, CadenciaEvento, Corretora, DisparoReengajamento, Lead,
)
from app.api import cloud_corretora_a
from app.services import cadencia, cloud_lines


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(element, compiler, **kw):  # noqa: ANN001
    return "JSON"


@compiles(BigInteger, "sqlite")
def _compile_bigint_sqlite(element, compiler, **kw):  # noqa: ANN001
    return "INTEGER"


# ids do cadastro canônico (mesma numeração do conftest/painel.py):
# 1 = Pessoa5 (linha Cloud a partir de 27/07) · 2 = Pessoa1 (SEM linha, segue
# no Evolution) · 3 = Pessoa3 (linha 2591, a original).
CORRETOR_ID, CORRETOR_ID, CORRETOR_ID = 1, 2, 3
CORRETOR_KOMMO, CORRETOR_KOMMO, CORRETOR_KOMMO = 10000006, 10000007, 10000003

PHONE_ID_CORRETOR = "000000000000000"
PHONE_ID_CORRETOR = "000000000000000"


@pytest.fixture()
def session_factory():
    """SQLite in-memory com as tabelas de cadência + `corretoras` (o roteamento
    por linha resolve o cadastro, então a tabela precisa existir aqui)."""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(
        engine,
        tables=[
            CadenciaEstado.__table__,
            CadenciaEvento.__table__,
            Corretora.__table__,
            # opt-out (07/10): o follow-up lê leads/disparos_reengajamento a cada
            # volta e, sem as tabelas, segura todo toque (fail-closed).
            Lead.__table__,
            DisparoReengajamento.__table__,
        ],
    )
    factory = sessionmaker(bind=engine)
    with factory() as s:
        s.add_all([
            Corretora(id=CORRETOR_ID, tenant_id=1, nome="Pessoa5", telefone="+5500000000000",
                      kommo_user_id=CORRETOR_KOMMO, evolution_instance="acme_corretora_c",
                      canal="cloud", ativo=True),
            Corretora(id=CORRETOR_ID, tenant_id=1, nome="Pessoa1", telefone="+5500000000000",
                      kommo_user_id=CORRETOR_KOMMO, evolution_instance="acme_corretor",
                      canal="evolution", ativo=True),
            Corretora(id=CORRETOR_ID, tenant_id=1, nome="Pessoa3", telefone="+5500000000000",
                      kommo_user_id=CORRETOR_KOMMO, evolution_instance="acme_corretora_a",
                      canal="cloud", ativo=True),
        ])
        s.commit()
    return factory


@pytest.fixture(autouse=True)
def _linhas_registradas(monkeypatch):
    """Registry com AS DUAS linhas de corretora (Pessoa12 + Pessoa10) e SEM a da Pessoa11.

    Monta o `_LINES`/`_BY_SLUG` à mão: em produção a linha da Pessoa10 entra por env
    (`CLOUD_LINHA_CORRETOR`), que pode não estar setada na árvore de teste.
    """
    corretor = cloud_lines.CloudLine(
        phone_id=PHONE_ID_CORRETOR, slug="corretor", papel="corretora",
        env_file="/etc/agencia/review.env", token_var="META_REVIEW_TOKEN",
    )
    corretor = cloud_lines.CloudLine(
        phone_id=PHONE_ID_CORRETOR, slug="corretor", papel="corretora",
        env_file="/etc/agencia/review.env", token_var="META_REVIEW_TOKEN",
    )
    linhas = {PHONE_ID_CORRETOR: corretor, PHONE_ID_CORRETOR: corretor}
    monkeypatch.setattr(cloud_lines, "_LINES", linhas)
    monkeypatch.setattr(
        cloud_lines, "_BY_SLUG", {l.slug: pid for pid, l in linhas.items()}
    )
    yield


@pytest.fixture(autouse=True)
def _reset_guard(monkeypatch):
    """Zera o cooldown in-process (60s entre envios) — bloquearia o 2º lead de um
    mesmo tick, e aqui o que se testa é o roteamento/cap, não o pacing."""
    from app.config import settings

    cadencia._LAST_ENQUEUE["at"] = 0.0
    monkeypatch.setattr(settings, "cadencia_min_intervalo_s", 0)
    yield


D0 = datetime(2026, 7, 27, 9, 0, 0, tzinfo=timezone.utc)


class SenderEspiao:
    """Registra por qual LINHA cada envio saiu. Nunca toca a rede."""

    def __init__(self) -> None:
        self.envios: list[dict[str, Any]] = []

    def __call__(
        self,
        telefone: str,
        texto: str,
        *,
        com_botao: bool,
        linha: Any = None,
        corretora_id: int | None = None,
    ) -> dict[str, Any]:
        self.envios.append({
            "telefone": telefone,
            "linha": getattr(linha, "slug", None),
            "phone_id": getattr(linha, "phone_id", None),
            "corretora_id": corretora_id,
        })
        n = len(self.envios)
        return {"ok": True, "dry_run": False, "sent": True,
                "wamid": f"wamid.{n}", "template": "acme_ola_a_01"}

    @property
    def linhas(self) -> list[str | None]:
        return [e["linha"] for e in self.envios]


def _matricular(session_factory, telefone: str, corretora_id: int | None) -> int:
    with session_factory() as s:
        est = cadencia.iniciar_cadencia(
            s, telefone=telefone, corretora_id=corretora_id, agora=D0
        )
        s.commit()
        return est.id


def _tick(session_factory, sender, agora=D0):
    cadencia._LAST_ENQUEUE["at"] = 0.0
    with session_factory() as s:
        return cadencia.tick(agora=agora, session=s, sender=sender)


# ── 1. Roteamento: cada lead sai pela linha da SUA dona ──────────────────────

def test_envio_roteia_pela_linha_da_dona(session_factory):
    """Lead da Pessoa12 sai pela 2591; lead da Pessoa10 sai pela linha DELA. É o coração
    do multi-linha: uma corretora nunca manda pela linha da outra."""
    _matricular(session_factory, "+5500000000000", CORRETOR_ID)
    _matricular(session_factory, "+5500000000000", CORRETOR_ID)

    sender = SenderEspiao()
    res = _tick(session_factory, sender)

    assert res.mensagens_enfileiradas == 2
    por_telefone = {e["telefone"]: e for e in sender.envios}
    assert por_telefone["+5500000000000"]["linha"] == "corretor"
    assert por_telefone["+5500000000000"]["phone_id"] == PHONE_ID_CORRETOR
    assert por_telefone["+5500000000000"]["linha"] == "corretor"
    assert por_telefone["+5500000000000"]["phone_id"] == PHONE_ID_CORRETOR


def test_dona_resolvida_tambem_pelo_kommo_user_id(session_factory):
    """`corretora_id` gravado no estado pode ser o kommo_user_id (webhook fino)
    em vez do id interno — o roteamento aceita os dois."""
    _matricular(session_factory, "+5500000000000", CORRETOR_KOMMO)

    sender = SenderEspiao()
    _tick(session_factory, sender)
    assert sender.linhas == ["corretor"]


def test_estado_legado_sem_dona_cai_na_linha_da_corretor(session_factory):
    """Cadência antiga sem `corretora_id` gravado sempre saiu pela Pessoa12 — continua
    saindo por lá (nada regride)."""
    _matricular(session_factory, "+5500000000000", None)

    sender = SenderEspiao()
    _tick(session_factory, sender)
    assert sender.linhas == ["corretor"]


# ── 2. Fail-closed: corretora sem linha Cloud não dispara nada ───────────────

def test_corretora_sem_linha_nao_dispara_nada(session_factory):
    """A Pessoa11 ainda está no Evolution (sem linha Cloud): a automação NÃO roda pra
    ela. Silencioso e sem exception — e o toque NÃO avança (não queima o slot)."""
    _matricular(session_factory, "+5500000000000", CORRETOR_ID)

    sender = SenderEspiao()
    res = _tick(session_factory, sender)

    assert sender.envios == [], "nada saiu pela Pessoa11"
    assert res.mensagens_enfileiradas == 0
    assert res.bloqueados.get("sem_linha_cloud") == 1
    with session_factory() as s:
        assert s.query(CadenciaEstado).one().toque_atual == 0


def test_lead_sem_linha_nao_impede_os_outros(session_factory):
    """O lead da Pessoa11 é pulado, mas Pessoa12 e Pessoa10 continuam sendo tocadas no MESMO
    tick (skip é por lead, não aborta a varredura)."""
    _matricular(session_factory, "+5500000000000", CORRETOR_ID)
    _matricular(session_factory, "+5500000000000", CORRETOR_ID)
    _matricular(session_factory, "+5500000000000", CORRETOR_ID)

    sender = SenderEspiao()
    res = _tick(session_factory, sender)

    assert res.mensagens_enfileiradas == 2
    assert sorted(sender.linhas) == ["corretor", "corretor"]
    assert res.bloqueados.get("sem_linha_cloud") == 1


def test_linha_da_corretora_e_fail_closed_sem_excecao(session_factory):
    """Contrato do resolvedor: Pessoa12/Pessoa10 resolvem; Pessoa11 e id inexistente devolvem
    None (nunca levantam)."""
    with session_factory() as s:
        assert cloud_lines.linha_da_corretora(CORRETOR_ID, session=s).slug == "corretor"
        assert cloud_lines.linha_da_corretora(CORRETOR_ID, session=s).slug == "corretor"
        assert cloud_lines.linha_da_corretora(CORRETOR_ID, session=s) is None
        assert cloud_lines.linha_da_corretora(9999, session=s) is None
        assert cloud_lines.linha_da_corretora(None, session=s) is None


# ── 3. CAP DIÁRIO POR LINHA (requisito, não opcional) ────────────────────────

def _queimar_cota(session_factory, corretora_id: int, quantas: int) -> None:
    """Gera `quantas` mensagens JÁ enviadas hoje pela linha da corretora (cada
    uma num estado próprio, como em produção)."""
    with session_factory() as s:
        for i in range(quantas):
            est = CadenciaEstado(
                tenant_id=1, telefone=f"5500000000000{corretora_id:02d}{i:03d}",
                corretora_id=corretora_id, toque_atual=2,
                proximo_toque_em=D0 + timedelta(days=30), status="ativa",
                criado_em=D0, atualizado_em=D0,
            )
            s.add(est)
            s.flush()
            s.add(CadenciaEvento(
                tenant_id=1, cadencia_id=est.id, toque=None,
                tipo=cadencia.TIPO_MENSAGEM, canal="whatsapp", payload={"sent": True}, at=D0,
            ))
        s.commit()


def test_cap_da_corretor_estourado_nao_bloqueia_a_corretor(session_factory, monkeypatch):
    """⚠️ O requisito mais caro: cap POR LINHA. A Pessoa10 queima a cota DELA; a Pessoa12
    tem que continuar enviando. Com o cap global antigo, a Pessoa10 comeria a cota da
    Pessoa12 — cuja linha já tomou 2 bans em julho (regime ≤25/dia)."""
    from app.config import settings

    monkeypatch.setattr(settings, "cadencia_cap_diario", 3)
    _queimar_cota(session_factory, CORRETOR_ID, 3)          # Pessoa10 no teto
    _matricular(session_factory, "+5500000000000", CORRETOR_ID)
    _matricular(session_factory, "+5500000000000", CORRETOR_ID)

    sender = SenderEspiao()
    res = _tick(session_factory, sender)

    assert sender.linhas == ["corretor"], "só a Pessoa12 passou; a Pessoa10 bateu no cap DELA"
    assert res.bloqueados.get("cap_diario") == 1
    with session_factory() as s:
        corretor = (
            s.query(CadenciaEstado)
            .filter(CadenciaEstado.telefone == "+5500000000000")
            .one()
        )
        assert corretor.toque_atual == 0, "lead barrado pelo cap não avança o toque"


def test_cap_da_corretor_estourado_nao_bloqueia_a_corretor(session_factory, monkeypatch):
    """Simétrico do anterior — o isolamento vale nas duas direções."""
    from app.config import settings

    monkeypatch.setattr(settings, "cadencia_cap_diario", 3)
    _queimar_cota(session_factory, CORRETOR_ID, 3)            # Pessoa12 no teto
    _matricular(session_factory, "+5500000000000", CORRETOR_ID)
    _matricular(session_factory, "+5500000000000", CORRETOR_ID)

    sender = SenderEspiao()
    res = _tick(session_factory, sender)

    assert sender.linhas == ["corretor"]
    assert res.bloqueados.get("cap_diario") == 1


def test_cap_conta_apenas_a_propria_linha(session_factory, monkeypatch):
    """`_cap_diario_atingido(corretora_id=...)` olha só o balde daquela linha."""
    from app.config import settings

    monkeypatch.setattr(settings, "cadencia_cap_diario", 3)
    _queimar_cota(session_factory, CORRETOR_ID, 3)

    with session_factory() as s:
        assert cadencia._cap_diario_atingido(s, 1, D0, corretora_id=CORRETOR_ID) is True
        assert cadencia._cap_diario_atingido(s, 1, D0, corretora_id=CORRETOR_ID) is False
        # sem corretora_id = cap GLOBAL (comportamento histórico preservado)
        assert cadencia._cap_diario_atingido(s, 1, D0) is True


def test_reserva_de_followup_vale_dentro_de_cada_linha(session_factory, monkeypatch):
    """A reserva (vagas garantidas pro follow-up, que faz a nutrição parar antes)
    é aplicada DENTRO do balde da linha, não sobre um total global."""
    from app.config import settings

    monkeypatch.setattr(settings, "cadencia_cap_diario", 10)
    _queimar_cota(session_factory, CORRETOR_ID, 6)

    with session_factory() as s:
        # 6 usados, cap 10, reserva 4 → teto efetivo 6 → ATINGIU (só pra Pessoa10)
        assert cadencia._cap_diario_atingido(
            s, 1, D0, reserva=4, corretora_id=CORRETOR_ID
        ) is True
        assert cadencia._cap_diario_atingido(
            s, 1, D0, reserva=4, corretora_id=CORRETOR_ID
        ) is False
        # sem reserva a Pessoa10 ainda tem folga no balde dela
        assert cadencia._cap_diario_atingido(
            s, 1, D0, corretora_id=CORRETOR_ID
        ) is False


def test_cap_da_corretor_absorve_estados_legados_sem_dona(session_factory, monkeypatch):
    """Estado sem `corretora_id` saiu pela linha da Pessoa12, então consome o balde
    DELA (senão essas mensagens ficariam fora de qualquer cap)."""
    from app.config import settings

    monkeypatch.setattr(settings, "cadencia_cap_diario", 2)
    _queimar_cota(session_factory, CORRETOR_ID, 1)
    # +1 envio de estado legado (corretora_id = NULL)
    with session_factory() as s:
        est = CadenciaEstado(
            tenant_id=1, telefone="+5500000000000", corretora_id=None,
            toque_atual=2, proximo_toque_em=D0 + timedelta(days=30),
            status="ativa", criado_em=D0, atualizado_em=D0,
        )
        s.add(est)
        s.flush()
        s.add(CadenciaEvento(
            tenant_id=1, cadencia_id=est.id, toque=None,
            tipo=cadencia.TIPO_MENSAGEM, canal="whatsapp", payload={"sent": True}, at=D0,
        ))
        s.commit()

    with session_factory() as s:
        assert cadencia._cap_diario_atingido(s, 1, D0, corretora_id=CORRETOR_ID) is True
        assert cadencia._cap_diario_atingido(s, 1, D0, corretora_id=CORRETOR_ID) is False


# ── 4. Boas-vindas pela linha da dona ────────────────────────────────────────

def test_boas_vindas_habilitada_segue_a_linha_nao_lista_fixa(session_factory):
    """Deixou de ser `BOAS_VINDAS_CORRETORAS = (3,)`: vale pra toda corretora com
    linha Cloud (Pessoa12 + Pessoa10) e não vale pra quem não tem (Pessoa11)."""
    from app.services import lead_intake

    with session_factory() as s:
        assert lead_intake.boas_vindas_habilitada(s, CORRETOR_ID) is True
        assert lead_intake.boas_vindas_habilitada(s, CORRETOR_ID) is True
        assert lead_intake.boas_vindas_habilitada(s, CORRETOR_ID) is False
        assert lead_intake.boas_vindas_habilitada(s, None) is False


def test_boas_vindas_sai_pela_linha_da_dona(monkeypatch):
    """O sender de boas-vindas recebe a linha da DONA do lead — Pessoa10 pela linha
    dela, Pessoa12 pela 2591."""
    from app.services import lead_intake

    vistos: list[dict[str, Any]] = []

    def sender_fake(telefone, texto, *, linha=None, corretora_id=None):
        vistos.append({"linha": getattr(linha, "slug", None),
                       "corretora_id": corretora_id})
        return {"ok": True, "dry_run": True, "sent": False}

    for cid, slug in ((CORRETOR_ID, "corretor"), (CORRETOR_ID, "corretor")):
        linha = cloud_lines.line_by_slug(slug)
        lead_intake._chamar_sender(
            sender_fake, "+5500000000000", lead_intake.BOAS_VINDAS_TEXTO,
            linha=linha, corretora_id=cid,
        )

    assert vistos == [
        {"linha": "corretor", "corretora_id": CORRETOR_ID},
        {"linha": "corretor", "corretora_id": CORRETOR_ID},
    ]


def test_sender_legado_de_boas_vindas_nao_quebra():
    """Sender antigo com assinatura `(telefone, texto)` (é o que vários testes
    existentes injetam) continua funcionando — os kwargs novos só vão pra quem
    os declara."""
    from app.services import lead_intake

    chamadas: list[tuple[str, str]] = []

    def sender_legado(telefone, texto):
        chamadas.append((telefone, texto))
        return {"ok": True, "dry_run": True, "sent": False}

    out = lead_intake._chamar_sender(
        sender_legado, "+5500000000000", "oi",
        linha=cloud_lines.line_by_slug("corretor"), corretora_id=CORRETOR_ID,
    )
    assert out["ok"] is True
    assert chamadas == [("+5500000000000", "oi")]


def test_sender_legado_da_cadencia_nao_quebra():
    """Mesma proteção no sender da cadência: `(telefone, texto, *, com_botao)`."""
    chamadas: list[str] = []

    def sender_legado(telefone, texto, *, com_botao):
        chamadas.append(telefone)
        return {"ok": True, "dry_run": True, "sent": False}

    out = cadencia._chamar_sender(
        sender_legado, "+5500000000000", "oi", com_botao=False,
        linha=cloud_lines.line_by_slug("corretor"), corretora_id=CORRETOR_ID,
    )
    assert out["ok"] is True
    assert chamadas == ["+5500000000000"]


# ── 5. Auto-enroll no Follow-up pra qualquer linha ──────────────────────────

def test_enroll_followup_funciona_pra_corretor(session_factory, monkeypatch):
    """Corretora com linha Cloud entra no auto-enroll — não é mais privilégio da
    Pessoa12. O estado nasce com o id INTERNO da dona (é por ele que o tick resolve a
    linha e contabiliza o cap)."""
    from app.config import settings
    from app.db.models import Lead
    from app.services import lead_spine

    monkeypatch.setattr(settings, "cadencia_auto_enroll", True)

    # owner_id da 2ª volta é o KOMMO_USER_ID da Pessoa12 (não o id interno): o enroll
    # tem que aceitar os dois e gravar o id INTERNO no estado.
    casos = ((CORRETOR_ID, CORRETOR_ID, "+5500000000000"), (CORRETOR_KOMMO, CORRETOR_ID, "+5500000000000"))
    for cid, esperado, telefone in casos:
        with session_factory() as s:
            lead = Lead(
                tenant_id=1, nome="Lead", telefone=telefone,
                owner_id=cid, external_ref=f"kommo_{cid}",
            )
            cadencia_id = lead_spine._enroll_cadencia_followup(s, lead, 1)
            s.commit()
            assert cadencia_id is not None, f"owner {cid} devia matricular"
            est = (
                s.query(CadenciaEstado)
                .filter(CadenciaEstado.id == cadencia_id)
                .one()
            )
            assert est.corretora_id == esperado


def test_enroll_followup_nao_matricula_corretora_sem_linha(session_factory, monkeypatch):
    """Pessoa11 (sem linha Cloud) NÃO é matriculada — fail-closed, sem exception."""
    from app.config import settings
    from app.db.models import Lead
    from app.services import lead_spine

    monkeypatch.setattr(settings, "cadencia_auto_enroll", True)

    with session_factory() as s:
        lead = Lead(tenant_id=1, nome="Lead da Pessoa11", telefone="+5500000000000",
                    owner_id=CORRETOR_ID, external_ref="kommo_corretor")
        assert lead_spine._enroll_cadencia_followup(s, lead, 1) is None
        assert s.query(CadenciaEstado).count() == 0


def test_enroll_followup_respeita_a_flag(session_factory, monkeypatch):
    """A flag `cadencia_auto_enroll` continua sendo o interruptor (default OFF —
    a Dona liga quando os templates da Pessoa10 aprovarem)."""
    from app.config import settings
    from app.db.models import Lead
    from app.services import lead_spine

    monkeypatch.setattr(settings, "cadencia_auto_enroll", False)
    with session_factory() as s:
        lead = Lead(tenant_id=1, nome="Lead", telefone="+5500000000000",
                    owner_id=CORRETOR_ID, external_ref="kommo_x")
        assert lead_spine._enroll_cadencia_followup(s, lead, 1) is None


# ── 6. Guard de dono com a semântica nova ───────────────────────────────────

def test_guard_bloqueia_quando_dono_do_lead_nao_e_dono_do_estado(
    session_factory, monkeypatch
):
    """Semântica nova do guard: bloqueia quando o dono do LEAD na espinha não é o
    dono DAQUELE estado de cadência (antes o teste era "não é a Pessoa12"). Cadência da
    Pessoa10 cujo lead virou pra Pessoa12 não pode seguir tocando pela linha da Pessoa10."""
    _matricular(session_factory, "+5500000000000", CORRETOR_ID)
    monkeypatch.setattr(
        cadencia, "_donos_por_suf8", lambda s, t: {"99990020": CORRETOR_ID}
    )

    sender = SenderEspiao()
    res = _tick(session_factory, sender)

    assert sender.envios == []
    assert res.bloqueados.get("dono_mudou") == 1


def test_guard_deixa_passar_quando_dono_confere(session_factory, monkeypatch):
    """Dono do lead == dono do estado → toca normalmente, pela linha dele."""
    _matricular(session_factory, "+5500000000000", CORRETOR_ID)
    monkeypatch.setattr(
        cadencia, "_donos_por_suf8", lambda s, t: {"99990021": CORRETOR_KOMMO}
    )

    sender = SenderEspiao()
    res = _tick(session_factory, sender)

    assert res.mensagens_enfileiradas == 1
    assert sender.linhas == ["corretor"]


def test_guard_nao_bloqueia_lead_ausente_da_espinha(session_factory, monkeypatch):
    """Lead sem registro na espinha (owner desconhecido) NÃO é bloqueado — mesma
    tolerância de antes, pra não matar cadência legítima."""
    _matricular(session_factory, "+5500000000000", CORRETOR_ID)
    monkeypatch.setattr(cadencia, "_donos_por_suf8", lambda s, t: {})

    res = _tick(session_factory, SenderEspiao())
    assert res.mensagens_enfileiradas == 1
    assert "dono_mudou" not in res.bloqueados


# ── 7. Envio parametrizado por linha (motor) ────────────────────────────────

@pytest.fixture(autouse=True)
def _templates_utility_liberados(monkeypatch):
    """A trava de categoria (cloud_lines.templates_utility) consulta a Meta pra
    garantir que só UTILITY sai. Em teste não há rede: declaramos que TODA a
    rotação está em UTILITY. Os testes que checam o BLOQUEIO sobrescrevem isso."""
    monkeypatch.setattr(
        cloud_lines, "templates_utility",
        lambda slug, **kw: {t[0] for t in cloud_corretora_a._OLA_ROTATION}
        | {t[0] for t in cloud_corretora_a._REENG_ROTATION},
    )


def test_enviar_template_linha_usa_phone_id_e_token_da_linha(monkeypatch):
    """O POST vai pro `/{phone_id}/messages` DA LINHA, com o token DELA. Sem rede:
    httpx.Client é substituído."""
    from app.api import cloud_corretora_a as cg

    capturado: dict[str, Any] = {}

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"messages": [{"id": "wamid.CORRETOR"}]}

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, **k):
            capturado["url"] = url
            capturado["auth"] = k["headers"]["Authorization"]
            return _Resp()

    monkeypatch.setattr(cg, "_dry_run_ativo", lambda: False)
    monkeypatch.setattr(cg.httpx, "Client", _Client)
    monkeypatch.setattr(cg, "_nome_para_template", lambda *a, **k: "João")
    monkeypatch.setattr(cloud_lines, "token_for", lambda linha: "tok-corretor")
    gravado: dict[str, Any] = {}
    monkeypatch.setattr(cg, "gravar", lambda **kw: gravado.update(kw) or True)

    linha = cloud_lines.line_by_slug("corretor")
    out = cg.enviar_template_linha(
        linha, "+5500000000000", "João", hook="oi",
        instance="acme_corretora_c", corretora_id=CORRETOR_ID,
    )

    assert PHONE_ID_CORRETOR in capturado["url"], "usou o phone_id da Pessoa10"
    assert capturado["auth"] == "Bearer tok-corretor"
    # contrato de retorno preservado byte a byte
    assert out["ok"] is True and out["sent"] is True and out["dry_run"] is False
    assert out["wamid"] == "wamid.CORRETOR"
    assert out["template"].startswith("acme_ola_")
    # grava no THREAD da corretora certa
    assert gravado["instance"] == "acme_corretora_c"
    assert gravado["corretora_id"] == CORRETOR_ID


def test_template_pendente_na_waba_da_corretor_rotaciona(monkeypatch):
    """Template ainda PENDING na WABA própria da Pessoa10 faz o POST falhar e a
    rotação cair pro próximo — comportamento que já existia, preservado."""
    from app.api import cloud_corretora_a as cg

    tentativas = {"n": 0}

    class _Resp:
        status_code = 400
        text = '{"error":{"code":132001}}'

        @staticmethod
        def json():
            return {"error": {"code": 132001, "message": "template não existe"}}

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **k):
            tentativas["n"] += 1
            return _Resp()

    monkeypatch.setattr(cg, "_dry_run_ativo", lambda: False)
    monkeypatch.setattr(cg.httpx, "Client", _Client)
    monkeypatch.setattr(cg, "_nome_para_template", lambda *a, **k: "João")
    monkeypatch.setattr(cloud_lines, "token_for", lambda linha: "tok")

    with pytest.raises(RuntimeError, match="sem template aprovado"):
        cg.enviar_template_linha(
            cloud_lines.line_by_slug("corretor"), "+5500000000000", "João", hook="oi"
        )
    # com_botao=False (default): só pool ola, NÃO reeng
    assert tentativas["n"] == len(cg._OLA_ROTATION)


def test_erro_de_linha_da_corretor_corta_a_rotacao(monkeypatch):
    """Erro de LINHA/conta (número não registrado, WABA restrita) aborta na
    PRIMEIRA tentativa — não queima o pool inteiro contra a mesma parede."""
    from app.api import cloud_corretora_a as cg

    tentativas = {"n": 0}

    class _Resp:
        status_code = 400
        text = '{"error":{"code":133010}}'

        @staticmethod
        def json():
            return {"error": {"code": 133010, "message": "não registrado"}}

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **k):
            tentativas["n"] += 1
            return _Resp()

    monkeypatch.setattr(cg, "_dry_run_ativo", lambda: False)
    monkeypatch.setattr(cg.httpx, "Client", _Client)
    monkeypatch.setattr(cg, "_nome_para_template", lambda *a, **k: "João")
    monkeypatch.setattr(cloud_lines, "token_for", lambda linha: "tok")

    with pytest.raises(RuntimeError, match="erro de linha/conta"):
        cg.enviar_template_linha(
            cloud_lines.line_by_slug("corretor"), "+5500000000000", "João", hook="oi"
        )
    assert tentativas["n"] == 1


def test_enviar_template_corretor_continua_saindo_pela_2591(monkeypatch):
    """O wrapper histórico da Pessoa12 não regrediu: mesma linha (2591), mesmo thread
    (acme_corretora_a / corretora 3)."""
    from app.api import cloud_corretora_a as cg

    capturado: dict[str, Any] = {}
    monkeypatch.setattr(cg, "_dry_run_ativo", lambda: True)
    monkeypatch.setattr(cg, "gravar", lambda **kw: capturado.update(kw) or True)

    out = cg.enviar_template_corretor("+5500000000000", "João", hook="oi")

    assert out["dry_run"] is True and out["sent"] is False
    assert out["wamid"].startswith("dry-")
    assert capturado["instance"] == "acme_corretora_a"
    assert capturado["corretora_id"] == 3


def test_dry_run_nao_bate_na_graph_em_nenhuma_linha(monkeypatch):
    """Rede de segurança: sob DRY-RUN nenhuma linha chega na Graph API."""
    from app.api import cloud_corretora_a as cg

    def _explode(*a, **k):
        raise AssertionError("DRY-RUN não pode tocar a rede")

    monkeypatch.setattr(cg, "_dry_run_ativo", lambda: True)
    monkeypatch.setattr(cg.httpx, "Client", _explode)
    monkeypatch.setattr(cg, "gravar", lambda **kw: True)

    for slug in ("corretor", "corretor"):
        out = cg.enviar_template_linha(
            cloud_lines.line_by_slug(slug), "+5500000000000", "João", hook="oi"
        )
        assert out == {
            "ok": True, "dry_run": True, "sent": False,
            "wamid": out["wamid"], "template": None,
            "messages": [{"id": out["wamid"]}],
        }


# ── 8. Trava de categoria: linha sem UTILITY não dispara ────────────────────

def test_linha_sem_template_utility_nao_dispara(monkeypatch):
    """Caso REAL da Pessoa10 em 27/07: a Meta aprovou os 13 templates dela como
    MARKETING. Regra dura da Dona = só UTILITY. Então a linha não manda NADA —
    e não gasta 13 requests pra falhar (era isso que estourava o circuit breaker
    global e calava a linha da Pessoa12 no mesmo tick)."""
    from app.api import cloud_corretora_a as cg

    monkeypatch.setattr(cg, "_dry_run_ativo", lambda: False)
    monkeypatch.setattr(cloud_lines, "templates_utility", lambda slug, **kw: set())

    chamou_rede = {"n": 0}

    class _ClientProibido:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **k):
            chamou_rede["n"] += 1
            raise AssertionError("não deveria tentar enviar sem template UTILITY")

    monkeypatch.setattr(cg.httpx, "Client", _ClientProibido)
    linha = cloud_lines.line_by_slug("corretor")

    with pytest.raises(RuntimeError, match="UTILITY"):
        cg.enviar_template_linha(linha, "+5500000000000", "João", hook="oi")
    assert chamou_rede["n"] == 0, "zero request — falhou ANTES de tocar a rede"


def test_template_marketing_e_tirado_da_rotacao(monkeypatch):
    """Se só PARTE da rotação está em UTILITY, os MARKETING são descartados e o
    envio usa exclusivamente os permitidos."""
    from app.api import cloud_corretora_a as cg

    permitido = cg._OLA_ROTATION[0][0]
    monkeypatch.setattr(cg, "_dry_run_ativo", lambda: False)
    monkeypatch.setattr(cg, "_nome_para_template", lambda *a, **k: "João")
    monkeypatch.setattr(cloud_lines, "token_for", lambda linha: "tok")
    monkeypatch.setattr(cloud_lines, "templates_utility", lambda slug, **kw: {permitido})
    monkeypatch.setattr(cg, "gravar", lambda **kw: True)
    usados: list[str] = []

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"messages": [{"id": "wamid.X"}]}

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, **k):
            usados.append(k["json"]["template"]["name"])
            return _Resp()

    monkeypatch.setattr(cg.httpx, "Client", _Client)
    linha = cloud_lines.line_by_slug("corretor")
    out = cg.enviar_template_linha(linha, "+5500000000000", "João", hook="oi")

    assert out["sent"] is True
    assert out.get("categoria") == "UTILITY"
    assert usados == [permitido], f"usou só o UTILITY permitido, foi: {usados}"


def test_casco_ola_m_nao_sai_quando_caller_pede_utility(monkeypatch):
    """Dona 06/09: copy direta tem que sair em UTILITY. Mesmo que o cache
    ainda liste o casco `_ola_m_`, ele não entra na rotação."""
    from app.api import cloud_corretora_a as cg

    utility = {"acme_corretor_ola_a_01", "acme_corretor_ola_m_01"}
    monkeypatch.setattr(cg, "_dry_run_ativo", lambda: False)
    monkeypatch.setattr(cg, "_nome_para_template", lambda *a, **k: "João")
    monkeypatch.setattr(cloud_lines, "token_for", lambda linha: "tok")
    monkeypatch.setattr(cloud_lines, "templates_utility", lambda slug, **kw: utility)
    monkeypatch.setattr(
        cloud_lines, "templates_marketing",
        lambda slug, **kw: {"acme_corretor_ola_m_01"},
    )
    monkeypatch.setattr(cloud_lines, "prefixo_do_slug", lambda slug: "acme_corretor_")
    monkeypatch.setattr(cg, "gravar", lambda **kw: True)
    usados: list[str] = []

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"messages": [{"id": "wamid.U"}]}

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, **k):
            usados.append(k["json"]["template"]["name"])
            return _Resp()

    monkeypatch.setattr(cg.httpx, "Client", _Client)
    out = cg.enviar_template_linha(
        cloud_lines.line_by_slug("corretor"), "+5500000000000", "João", hook="oi"
    )
    assert out["sent"] is True
    assert out["categoria"] == "UTILITY"
    assert usados == ["acme_corretor_ola_a_01"]
    assert all("_ola_m_" not in t for t in usados)


def test_linha_com_nomes_proprios_monta_rotacao(monkeypatch):
    """Caso REAL da Pessoa10: os templates UTILITY dela têm nomes PRÓPRIOS
    (`acme_jn_*`), fora da rotação padrão (que usa os nomes da linha da Pessoa12).
    O envio precisa montar a rotação a partir do que a linha tem — senão a linha
    fica muda mesmo com template aprovado."""
    from app.api import cloud_corretora_a as cg

    proprios = {"acme_jn_ola_a_01", "acme_jn_reeng_a_01", "acme_jn_reeng_b_01"}
    monkeypatch.setattr(cg, "_dry_run_ativo", lambda: False)
    monkeypatch.setattr(cg, "_nome_para_template", lambda *a, **k: "João")
    monkeypatch.setattr(cloud_lines, "token_for", lambda linha: "tok")
    monkeypatch.setattr(cloud_lines, "templates_utility", lambda slug, **kw: proprios)
    monkeypatch.setattr(cg, "gravar", lambda **kw: True)
    usados: list[str] = []

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"messages": [{"id": "wamid.Y"}]}

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, **k):
            usados.append(k["json"]["template"]["name"])
            return _Resp()

    monkeypatch.setattr(cg.httpx, "Client", _Client)
    out = cg.enviar_template_linha(
        cloud_lines.line_by_slug("corretor"), "+5500000000000", "João", hook="oi"
    )
    assert out["sent"] is True
    assert usados and usados[0] in proprios, f"usou template próprio da linha: {usados}"
