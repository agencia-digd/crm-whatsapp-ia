"""Testes do motor de cadência híbrida (P2).

Cobre:
  - avanço de toque por dia (state machine avança conforme `agora` passa);
  - toques D1/D5/D9 geram tarefa-ligação (NÃO mensagem);
  - toda mensagem termina com "?";
  - botão alterna (com/sem) a cada mensagem;
  - dry-run NÃO envia (sender fake registra o que seria enviado);
  - idempotente: rodar tick 2x no mesmo dia não duplica.

DB isolado: SQLite in-memory (mesmo padrão de test_lead_spine). Zero prod.
Sender é injetado (fake) — nunca chama o motor real da Fase 7.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import BigInteger, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from app.db.models import Base, CadenciaEstado, CadenciaEvento, DisparoReengajamento, Lead
from app.services import cadencia


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(element, compiler, **kw):  # noqa: ANN001
    return "JSON"


@compiles(BigInteger, "sqlite")
def _compile_bigint_sqlite(element, compiler, **kw):  # noqa: ANN001
    return "INTEGER"


@pytest.fixture()
def session_factory():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(
        engine,
        tables=[CadenciaEstado.__table__, CadenciaEvento.__table__,
                # opt-out (07/10): o follow-up lê leads/disparos_reengajamento a cada
                # volta e, sem as tabelas, segura todo toque (fail-closed).
                Lead.__table__, DisparoReengajamento.__table__],
    )
    return sessionmaker(bind=engine)


@pytest.fixture(autouse=True)
def _reset_guard():
    # zera o guard in-process de espaçamento entre enfileiramentos (senão o
    # cooldown de 60s bloqueia os testes que rodam em milissegundos).
    cadencia._LAST_ENQUEUE["at"] = 0.0
    yield


class FakeSender:
    """Sender injetável — registra chamadas, nunca envia. Simula DRY-RUN."""

    def __init__(self):
        self.chamadas: list[dict] = []

    def __call__(self, telefone, texto, *, com_botao):
        self.chamadas.append({"telefone": telefone, "texto": texto, "com_botao": com_botao})
        return {"ok": True, "dry_run": True, "sent": False}


D0 = datetime(2026, 7, 10, 9, 0, 0, tzinfo=timezone.utc)


def _iniciar(session_factory, telefone="+5500000000000", agora=D0):
    with session_factory() as s:
        est = cadencia.iniciar_cadencia(
            s, telefone=telefone, lead_id="kommo_1", corretora_id=3, agora=agora
        )
        s.commit()
        return est.id


# minutos de folga pra fazer o cooldown de 60s não bloquear chamadas seguidas
# num mesmo teste: cada tick reseta _LAST_ENQUEUE só no autouse fixture, então
# aqui forçamos reset entre ticks quando preciso.
def _tick(session_factory, sender, agora):
    cadencia._LAST_ENQUEUE["at"] = 0.0
    with session_factory() as s:
        return cadencia.tick(agora=agora, session=s, sender=sender)


# ── 1. D0 enfileira mensagem de boas-vindas (não tarefa) ──────────────────────

def test_d0_enfileira_mensagem(session_factory):
    _iniciar(session_factory)
    sender = FakeSender()
    res = _tick(session_factory, sender, D0)
    assert res.mensagens_enfileiradas == 1
    assert res.tarefas_ligacao == 0
    assert len(sender.chamadas) == 1


# ── 2. Avanço de toque por dia ────────────────────────────────────────────────

def test_avanco_de_toque_por_dia(session_factory):
    _iniciar(session_factory)
    sender = FakeSender()
    # D0: toque 0 (msg). D1: toque 1 (tarefa). D2: toque 2 (msg).
    _tick(session_factory, sender, D0)
    res_d1 = _tick(session_factory, sender, D0 + timedelta(days=1))
    res_d2 = _tick(session_factory, sender, D0 + timedelta(days=2))

    with session_factory() as s:
        est = s.query(CadenciaEstado).one()
        # depois do D2 (msg), toque_atual avançou pra 3
        assert est.toque_atual == 3
        eventos = s.query(CadenciaEvento).order_by(CadenciaEvento.toque).all()
        tipos = [(e.toque, e.tipo) for e in eventos]
        assert tipos == [
            (0, cadencia.TIPO_MENSAGEM),
            (1, cadencia.TIPO_TAREFA_LIGACAO),
            (2, cadencia.TIPO_MENSAGEM),
        ]
    assert res_d1.tarefas_ligacao == 1
    assert res_d2.mensagens_enfileiradas == 1


# ── 3. D1/D5/D9 geram tarefa-ligação (não msg) ────────────────────────────────

def test_toques_ligacao_geram_tarefa(session_factory):
    _iniciar(session_factory)
    sender = FakeSender()
    # roda o dia de cada toque 0..7 (offsets: 0,1,2,4,5,7,9,10)
    offsets = [t.offset_dias for t in cadencia.TOQUES]
    for off in offsets:
        _tick(session_factory, sender, D0 + timedelta(days=off))

    with session_factory() as s:
        eventos = {e.toque: e for e in s.query(CadenciaEvento).all()}
        # Índices das ligações vêm da régua — ela muda (o D3 de áudio entrou
        # em 04/08 e deslocou todo mundo).
        _ligacoes = [t.indice for t in cadencia.TOQUES
                     if t.tipo == cadencia.TIPO_TAREFA_LIGACAO]
        for t in _ligacoes:
            assert eventos[t].tipo == cadencia.TIPO_TAREFA_LIGACAO, t
            assert eventos[t].canal == cadencia.CANAL_LIGACAO
        # os demais são mensagem — mas o de MARKETING é registrado como
        # PULADO: categoria 'marketing' não é despachada pela linha UTILITY.
        _msgs = [t.indice for t in cadencia.TOQUES
                 if t.tipo == cadencia.TIPO_MENSAGEM]
        for t in _msgs:
            assert eventos[t].tipo == cadencia.TIPO_MENSAGEM, t
        _mkt = next(t.indice for t in cadencia.TOQUES
                    if t.tipo == cadencia.TIPO_MENSAGEM
                    and t.categoria == "marketing")
        assert eventos[_mkt].payload["pulado"] == "categoria_marketing"
        assert eventos[_mkt].payload["sent"] is False

    # tarefa NUNCA chama o sender; o toque de marketing também não sai
    # toques de MENSAGEM menos o de marketing (que a linha UTILITY não despacha)
    assert len(sender.chamadas) == len(_msgs) - 1


# ── 4. Toda mensagem termina com "?" ──────────────────────────────────────────

def test_mensagem_sempre_termina_com_pergunta(session_factory):
    _iniciar(session_factory)
    sender = FakeSender()
    for off in [t.offset_dias for t in cadencia.TOQUES]:
        _tick(session_factory, sender, D0 + timedelta(days=off))

    for c in sender.chamadas:
        assert c["texto"].rstrip().endswith("?"), c["texto"]


def test_todos_templates_terminam_com_pergunta():
    for txt in cadencia._TEMPLATES_MSG.values():
        assert txt.rstrip().endswith("?")
    assert cadencia._TEMPLATE_REMARKETING.rstrip().endswith("?")


# ── 5. Follow-up só leva botão no toque da oferta de áudio ───────────────────

def test_followup_so_leva_botao_na_oferta_de_audio(session_factory):
    """Decisão da Dona (28/07): follow-up sai SEM botão, pelo pool `_ola_`.
    Revisão (05/08): a oferta de áudio é a ÚNICA exceção — lá o "sim" precisa
    ser um CLIQUE, senão qualquer texto do lead parava a cadência e ele saía da
    régua sem nunca ter conversado com ninguém.

    O teste não fixa índice: pergunta pra régua quem tem botão.
    """
    _iniciar(session_factory)
    sender = FakeSender()
    for off in [t.offset_dias for t in cadencia.TOQUES]:
        _tick(session_factory, sender, D0 + timedelta(days=off))

    flags = [c["com_botao"] for c in sender.chamadas]
    assert flags, "nenhuma mensagem despachada — o cenário não exercitou nada"
    # exatamente UM toque com botão, e é o da oferta de áudio
    assert sum(1 for f in flags if f) == 1, flags
    esperado = cadencia.toque_com_botao()
    assert esperado is not None and esperado.cta

    # o texto que saiu com botão é o do toque da oferta
    com_botao = [c["texto"] for c in sender.chamadas if c["com_botao"]]
    assert com_botao == [
        cadencia.aplicar_assinatura(
            cadencia._TEMPLATES_MSG[esperado.indice], 3
        )
    ]


# ── 6. dry-run não envia ──────────────────────────────────────────────────────

def test_dry_run_nao_envia(session_factory):
    _iniciar(session_factory)
    sender = FakeSender()
    _tick(session_factory, sender, D0)
    # sender fake sempre devolve sent=False / dry_run=True → evento marca isso
    with session_factory() as s:
        ev = s.query(CadenciaEvento).filter(CadenciaEvento.toque == 0).one()
        assert ev.payload["dry_run"] is True
        assert ev.payload["sent"] is False


# ── 7. Idempotência: 2 ticks no mesmo dia não duplicam ────────────────────────

def test_idempotente_no_mesmo_dia(session_factory):
    _iniciar(session_factory)
    sender = FakeSender()
    _tick(session_factory, sender, D0)
    chamadas_apos_1 = len(sender.chamadas)
    # segundo tick no MESMO dia: toque 0 já registrado + toque 1 ainda não devido (D1)
    _tick(session_factory, sender, D0)

    with session_factory() as s:
        # só 1 evento pro toque 0 (UNIQUE tenant/cadencia/toque)
        n = s.query(CadenciaEvento).filter(CadenciaEvento.toque == 0).count()
        assert n == 1
    assert len(sender.chamadas) == chamadas_apos_1  # não reenfileirou


# ── 8. Após 8 toques vira remarketing (1 msg / 30 dias) ───────────────────────

def test_remarketing_apos_cadencia(session_factory):
    _iniciar(session_factory)
    sender = FakeSender()
    for off in [t.offset_dias for t in cadencia.TOQUES]:
        _tick(session_factory, sender, D0 + timedelta(days=off))

    with session_factory() as s:
        est = s.query(CadenciaEstado).one()
        assert est.status == "remarketing"

    # antes de 30 dias: não dispara remarketing
    res_cedo = _tick(session_factory, sender, D0 + timedelta(days=20))
    assert res_cedo.remarketing == 0

    # passados 30 dias do fim: 1 msg de remarketing
    res_tarde = _tick(session_factory, sender, D0 + timedelta(days=10 + 31))
    assert res_tarde.remarketing == 1
    assert sender.chamadas[-1]["texto"].rstrip().endswith("?")


# ── 9. opt-out para de tocar ──────────────────────────────────────────────────

def test_opt_out_para_de_tocar(session_factory):
    _iniciar(session_factory)
    sender = FakeSender()
    _tick(session_factory, sender, D0)
    # `cadencia.opt_out` (igualdade exata de telefone, zero callers) foi
    # removida na auditoria 07/2026 — quem para a cadência é o caminho vivo do
    # inbound, que casa por sufixo-8 e para TODAS as gêmeas.
    with session_factory() as s:
        est = s.query(CadenciaEstado).one()
        est.status = "opt_out"
        s.commit()

    res = _tick(session_factory, sender, D0 + timedelta(days=1))
    # cadência opt_out não é 'ativa'/'remarketing' → nem entra na query de devidos
    assert res.considerados == 0


# ── 10. iniciar_cadencia é idempotente por (tenant, telefone) ─────────────────

def test_iniciar_idempotente(session_factory):
    with session_factory() as s:
        a = cadencia.iniciar_cadencia(s, telefone="+5500000000000", agora=D0)
        b = cadencia.iniciar_cadencia(s, telefone="+5500000000000", agora=D0)
        s.commit()
        assert a.id == b.id
        assert s.query(CadenciaEstado).count() == 1


# ── 12. Sugestão da IA aprovada entra no lugar do template ───────────────────
# A IA escreve a variável {{2}}; um humano aprova no painel; SÓ AÍ a cadência
# usa. A casca do template UTILITY não muda nunca. (Dona, 29/07)

def test_sugestao_aprovada_substitui_o_texto(session_factory, monkeypatch):
    from app.config import settings
    from app.services import sugestao_cadencia as sug

    monkeypatch.setattr(settings, "sugestao_ia_cadencia", True, raising=False)
    monkeypatch.setattr(
        sug, "texto_aprovado",
        lambda tel, toque: (42, "você comentou que voltava dia 20 — vamos marcar?"),
    )
    usadas = []
    monkeypatch.setattr(sug, "marcar_usada", lambda sid: usadas.append(sid))

    _iniciar(session_factory)
    sender = FakeSender()
    _tick(session_factory, sender, D0)

    assert sender.chamadas, "nada foi despachado"
    assert sender.chamadas[0]["texto"] == "você comentou que voltava dia 20 — vamos marcar?"
    assert usadas == [42], "sugestão usada não foi marcada"


def test_sem_sugestao_aprovada_usa_o_template(session_factory, monkeypatch):
    from app.config import settings
    from app.services import sugestao_cadencia as sug

    monkeypatch.setattr(settings, "sugestao_ia_cadencia", True, raising=False)
    monkeypatch.setattr(sug, "texto_aprovado", lambda tel, toque: None)

    _iniciar(session_factory)
    sender = FakeSender()
    _tick(session_factory, sender, D0)

    assert sender.chamadas[0]["texto"] == cadencia.aplicar_assinatura(
        cadencia._TEMPLATES_MSG[0], 3
    )


def test_flag_desligada_ignora_a_fila(session_factory, monkeypatch):
    """Kill-switch: com a flag off nem consulta a fila."""
    from app.config import settings
    from app.services import sugestao_cadencia as sug

    monkeypatch.setattr(settings, "sugestao_ia_cadencia", False, raising=False)

    def explode(*a, **k):
        raise AssertionError("consultou a fila com a flag desligada")

    monkeypatch.setattr(sug, "texto_aprovado", explode)

    _iniciar(session_factory)
    sender = FakeSender()
    _tick(session_factory, sender, D0)
    assert sender.chamadas[0]["texto"] == cadencia.aplicar_assinatura(
        cadencia._TEMPLATES_MSG[0], 3
    )


def test_erro_na_fila_nao_impede_o_followup(session_factory, monkeypatch):
    """FAIL-SAFE: a fila explodindo NÃO pode segurar a mensagem — vai o template."""
    from app.config import settings
    from app.services import sugestao_cadencia as sug

    monkeypatch.setattr(settings, "sugestao_ia_cadencia", True, raising=False)

    def explode(*a, **k):
        raise RuntimeError("banco fora do ar")

    monkeypatch.setattr(sug, "texto_aprovado", explode)

    _iniciar(session_factory)
    sender = FakeSender()
    res = _tick(session_factory, sender, D0)

    assert res.mensagens_enfileiradas == 1, "o follow-up tem que sair mesmo assim"
    assert sender.chamadas[0]["texto"] == cadencia.aplicar_assinatura(
        cadencia._TEMPLATES_MSG[0], 3
    )


def test_evento_registra_qual_sugestao_saiu(session_factory, monkeypatch):
    """Auditoria: dá pra saber se saiu template ou sugestão, e qual."""
    from app.config import settings
    from app.services import sugestao_cadencia as sug
    from app.db.models import CadenciaEvento

    monkeypatch.setattr(settings, "sugestao_ia_cadencia", True, raising=False)
    monkeypatch.setattr(sug, "texto_aprovado", lambda tel, toque: (7, "texto da IA?"))
    monkeypatch.setattr(sug, "marcar_usada", lambda sid: None)

    _iniciar(session_factory)
    _tick(session_factory, FakeSender(), D0)

    with session_factory() as s:
        ev = s.query(CadenciaEvento).filter(CadenciaEvento.tipo == "mensagem").first()
        assert ev is not None
        assert ev.payload.get("sugestao_ia_id") == 7


# ── 13. Rodapé não repete a sequência inteira (Dona 29/07) ──────────────────

def test_ordem_da_mensagem_percorre_a_rotacao_inteira():
    """Os toques de MENSAGEM são 0,2,3,5,7 — `% 5` disso dá {0,2,3,0,2}, ou seja
    duas posições da rotação nunca seriam visitadas e o lead podia receber o
    mesmo rodapé a sequência toda. A ordem ordinal corrige."""
    msgs = sorted(cadencia._TEMPLATES_MSG)
    ordens = [cadencia.ordem_da_mensagem(i) for i in msgs]
    assert ordens == list(range(len(msgs))), ordens


def test_todo_lead_recebe_rodapes_variados():
    """Nenhum lead pode terminar todas as mensagens com o mesmo fecho."""
    from app.api.cloud_corretora_a import preview_template_corretor

    telefones = [
        "+5500000000000", "+5500000000000", "+5500000000000",
        "+5500000000000", "+5500000000000", "+5500000000000",
    ]
    for tel in telefones:
        rodapes = set()
        for t in cadencia.TOQUES:
            if cadencia._TEMPLATES_MSG.get(t.indice) is None:
                continue
            p = preview_template_corretor(tel, "Lead", variacao=cadencia.ordem_da_mensagem(t.indice))
            rodapes.add(p["skeleton"].strip().split("\n")[-1])
        assert len(rodapes) >= 3, f"{tel} só teve {len(rodapes)} rodapé(s): {rodapes}"


def test_preview_bate_com_o_template_escolhido():
    """O preview tem que mostrar o rodapé do template que a rotação escolheu —
    antes ele mostrava sempre o do _OLA_BODY e mentia."""
    from app.api.cloud_corretora_a import preview_template_corretor, _OLA_BODIES

    for v in range(5):
        p = preview_template_corretor("+5500000000000", "Lead", variacao=v)
        corpo = _OLA_BODIES[p["template"]]
        assert p["skeleton"].strip().split("\n")[-1] == corpo.strip().split("\n")[-1]


# ── 14. Assinatura é de quem MANDA (achado do Grok, 29/07) ──────────────────

def test_assinatura_segue_a_dona_do_lead():
    """O envio roteia pela linha da DONA — a copy tem que assinar igual.

    Antes "Pessoa" estava cravado no texto: lead da Pessoa10 recebia, no WhatsApp
    da Pessoa10, uma mensagem assinada Pessoa."""
    t0 = cadencia._TEMPLATES_MSG[0]
    assert "{assinatura}" in t0, "o template perdeu o marcador"
    assert "Pessoa5" in cadencia.aplicar_assinatura(t0, 1)
    assert "Pessoa1" in cadencia.aplicar_assinatura(t0, 2)
    assert "Pessoa" in cadencia.aplicar_assinatura(t0, 3)      # Pessoa12 = persona da casa
    assert "Pessoa" in cadencia.aplicar_assinatura(t0, None)   # sem dona → padrão


def test_assinatura_nao_vaza_marcador():
    """Nenhum texto pode sair com '{assinatura}' cru pro lead."""
    for idx, txt in cadencia._TEMPLATES_MSG.items():
        for cid in (1, 2, 3, None):
            out = cadencia.aplicar_assinatura(txt, cid)
            assert "{assinatura}" not in out, f"toque {idx}, corretora {cid}"
    assert "{assinatura}" not in cadencia.aplicar_assinatura(
        cadencia._TEMPLATE_REMARKETING, 1
    )


def test_copy_antiga_sem_marcador_passa_intacta():
    """Override em `cadencia_copies` escrito antes disso não pode quebrar."""
    velha = "Texto antigo sem marcador nenhum, tudo certo?"
    assert cadencia.aplicar_assinatura(velha, 1) == velha


def test_assinatura_usa_a_mesma_fonte_do_intake():
    """Duas listas divergindo é como o bug nasce — tem que ser uma fonte só."""
    from app.services.lead_intake import _BOAS_VINDAS_NOME

    for cid, nome in _BOAS_VINDAS_NOME.items():
        assert cadencia.assinatura_da_corretora(cid) == nome


# ── 15. Tick concorrente não manda 2× (Grok + Sol, 29/07) ───────────────────

def test_tick_concorrente_e_barrado(session_factory, monkeypatch):
    """Dois ticks sobrepostos liam o MESMO estado antes de qualquer um gravar,
    e o lead recebia duas vezes. O UNIQUE protege o banco, não o WhatsApp."""
    _iniciar(session_factory)
    sender = FakeSender()

    # simula "outro tick já rodando"
    monkeypatch.setattr(cadencia, "travar", lambda ns, t: cadencia.OCUPADO)
    with session_factory() as s:
        res = cadencia.tick(agora=D0, session=s, sender=sender)

    assert not sender.chamadas, "mandou mensagem com outro tick rodando"
    assert res.bloqueados.get("tick_concorrente") == 1


def test_trava_indisponivel_nao_cala_a_cadencia(session_factory, monkeypatch):
    """FAIL-SAFE: banco sem advisory lock (SQLite dos testes) ou erro na trava
    NÃO pode impedir o follow-up — degrada pro comportamento histórico."""
    def explode(ns, t):
        raise RuntimeError("sem advisory lock")

    monkeypatch.setattr(cadencia, "travar", explode)
    _iniciar(session_factory)
    sender = FakeSender()
    with session_factory() as s:
        with pytest.raises(RuntimeError):
            cadencia.tick(agora=D0, session=s, sender=sender)

    # E a função REAL não devolve OCUPADO quando falha — devolve INDISPONIVEL,
    # que a cadência trata como "segue sem trava" (o contrário calaria o
    # follow-up sempre que o banco tossisse).
    #
    # ⚠️ SOLTAR É OBRIGATÓRIO. Advisory lock do Postgres é por CONEXÃO, e o pool
    # do SQLAlchemy sobrevive ao fim do processo por alguns segundos. A versão
    # anterior deste teste pegava a trava e ia embora: passava na primeira
    # execução, vazava a conexão travada, e da segunda em diante encontrava a
    # PRÓPRIA trava da rodada anterior e falhava pra sempre. Levou uma manhã
    # pra descobrir que o teste estava brigando consigo mesmo.
    monkeypatch.undo()
    from app.services import trava_tick as tt

    trava = tt.travar(tt.NS_CADENCIA, 999)
    try:
        assert trava is not tt.OCUPADO
    finally:
        tt.soltar(trava, tt.NS_CADENCIA, 999)


def test_trava_de_erro_devolve_indisponivel_e_nao_ocupado():
    """A distinção que existe desde 02/08: erro de trava ≠ outro tick rodando.

    Quem só precisa não calar (cadência, nutrição) segue sem trava com
    INDISPONIVEL; quem manda em massa (carrossel) para. Se os dois voltarem a
    ser o mesmo valor, o carrossel volta a disparar sem trava."""
    from app.services import trava_tick as tt

    assert tt.INDISPONIVEL is not tt.OCUPADO
    assert tt.INDISPONIVEL is not None


def test_soltar_trava_nunca_levanta():
    """Unlock é best-effort: erro ao soltar não pode derrubar o tick."""
    from app.services.trava_tick import soltar as _sl, NS_CADENCIA, OCUPADO as _OC

    _sl(None, NS_CADENCIA, 1)          # nada pra soltar
    _sl(_OC, NS_CADENCIA, 1)           # sentinela

    class ConnRuim:
        def execute(self, *a, **k):
            raise RuntimeError("conexão morta")

        def close(self):
            raise RuntimeError("close também falha")

    _sl(ConnRuim(), NS_CADENCIA, 1)   # não levanta
