"""Pessoa sem LLM não engole lead (incidente 05/09/2026).

Claude (janela 5h) e codex (cota até dia 7) caíram juntos; a Pessoa respondeu
"probleminha técnico" 6x e nunca voltou. O que protege, em ordem:
1. provider falha 1x e se recupera → retry, resposta normal;
2. falha permanente → o turno vai pra fila `pessoa_retomadas` + aviso à Dona;
3. a mensagem do lead NUNCA some (fica na fila até um provider voltar);
4. o tick retoma e responde pela linha da Corretor; humano/handoff cancela;
5. diagnóstico: a causa real (usage limit) aparece no erro, não o ruído.
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import delete, select

import app.agents.pessoa.agent as ma
from app.agents.pessoa.agent import AgentePessoa, FRASE_PROBLEMINHA
from app.db.models import Conversation, PessoaRetomada
from app.db.session import SessionLocal
from app.services import atendimento_corretor as ab
from app.services import pessoa_retomada as mr

TEL = "+5500000000000"
RESPOSTA_OK = json.dumps({"text": "Oi! Que bom te ver por aqui 😊 Me conta: pra morar ou investir?",
                          "tool_call": None}, ensure_ascii=False)


class ProviderFake:
    """Falha `falhas` vezes, depois responde. `permanente` marca transient=False."""

    def __init__(self, falhas: int = 0, permanente: bool = False) -> None:
        self.falhas = falhas
        self.permanente = permanente
        self.chamadas = 0

    def complete(self, system, user, json_schema=None):
        self.chamadas += 1
        if self.chamadas <= self.falhas:
            exc = RuntimeError("CodexProvider: exit 1 (permanente): ERROR: You've hit your usage limit")
            if self.permanente:
                exc.transient = False  # type: ignore[attr-defined]
            raise exc
        return RESPOSTA_OK, MagicMock()

    def complete_text(self, system, user):
        return "ok", MagicMock()


@pytest.fixture(autouse=True)
def _sem_dormir_e_limpo(monkeypatch):
    monkeypatch.setattr(ma, "_dormir", lambda s: None)
    # O interruptor nasce desligado no código (`atendimento_corretor_ativo = False`)
    # e ligado só no .env de produção. Sem ligar aqui, estes testes passavam
    # na máquina que tem o .env e, em ambiente limpo, o `atender` devolvia
    # "desligado" antes de chegar no que o teste exercita.
    monkeypatch.setattr(ab.settings, "atendimento_corretor_ativo", True, raising=False)
    monkeypatch.setattr(ab, "_e_da_roda", lambda t: True)
    monkeypatch.setattr(ab, "_nos_abordamos_na_mao", lambda t: False)
    monkeypatch.setattr(ab, "_humano_falando", lambda t, desde=None: False)
    monkeypatch.setattr(ab, "_handoff_feito", lambda t: False)
    monkeypatch.setattr(ab, "promover_para_vendas", lambda *a, **k: False)
    with SessionLocal() as s:
        # fila inteira (banco de TESTE): outros testes deixam pendentes de
        # outros telefones — o `atender` enfileira em qualquer erro.
        s.execute(delete(PessoaRetomada))
        s.execute(delete(Conversation).where(Conversation.phone == TEL))
        s.commit()
    yield


def _pendente(tel: str = TEL) -> PessoaRetomada | None:
    with SessionLocal() as s:
        return s.execute(
            select(PessoaRetomada).where(PessoaRetomada.telefone == tel)
            .order_by(PessoaRetomada.id.desc())
        ).scalars().first()


# ── 1. retry no agente ───────────────────────────────────────────────────────

def test_provider_falha_uma_vez_e_se_recupera():
    p = ProviderFake(falhas=1)
    out = AgentePessoa(phone=TEL, provider=p).run("Quero")
    assert p.chamadas == 2
    assert out["response_text"].startswith("Oi!")
    assert not out.get("provider_falhou")


def test_provider_fora_de_vez_marca_falha_e_frase_e_ultimo_recurso():
    p = ProviderFake(falhas=99)
    out = AgentePessoa(phone=TEL, provider=p).run("Quero")
    assert p.chamadas == ma.PROVIDER_TENTATIVAS
    assert out["provider_falhou"] is True
    assert out["response_text"] == FRASE_PROBLEMINHA
    assert "usage limit" in out["erro"]


def test_falha_permanente_nao_ganha_retry_inutil():
    p = ProviderFake(falhas=99, permanente=True)
    out = AgentePessoa(phone=TEL, provider=p).run("Quero")
    assert p.chamadas == 1
    assert out["provider_falhou"] is True


# ── 2/3. fila: a mensagem do lead nunca some ─────────────────────────────────

def test_atender_enfileira_o_lead_e_avisa_a_dona(monkeypatch):
    class MariaMorta:
        def __init__(self, **kw): pass
        def run(self, msg):
            return {"response_text": FRASE_PROBLEMINHA, "tool_calls": [],
                    "provider_falhou": True, "erro": "todos fora"}

    monkeypatch.setattr(ma, "AgentePessoa", MariaMorta)
    enviadas: list[tuple[str, str]] = []
    monkeypatch.setattr(ab, "_responder", lambda tel, txt: enviadas.append((tel, txt)))
    alertas: list[dict] = []
    monkeypatch.setattr(mr, "_alertar_dona",
                        lambda tel, erro, rid, agora, desistiu=False: alertas.append(
                            {"tel": tel, "rid": rid, "desistiu": desistiu}) or True)

    res = ab.atender(TEL, "Quero")

    assert res["atendeu"] is True
    assert enviadas == [(TEL, FRASE_PROBLEMINHA)]       # frase saiu UMA vez, como último recurso
    row = _pendente()
    assert row is not None and row.status == "pendente"
    assert row.texto == "Quero"                          # a mensagem do lead está guardada
    assert alertas and alertas[0]["rid"] == row.id and alertas[0]["desistiu"] is False


def test_segunda_mensagem_sem_llm_nao_repete_a_frase_e_junta_na_fila(monkeypatch):
    class MariaMorta:
        def __init__(self, **kw): pass
        def run(self, msg):
            return {"response_text": FRASE_PROBLEMINHA, "tool_calls": [],
                    "provider_falhou": True, "erro": "todos fora"}

    monkeypatch.setattr(ma, "AgentePessoa", MariaMorta)
    monkeypatch.setattr(mr, "_alertar_dona", lambda *a, **k: True)
    enviadas: list[str] = []
    monkeypatch.setattr(ab, "_responder", lambda tel, txt: enviadas.append(txt))

    ab.atender(TEL, "Quero")
    ab.atender(TEL, "apartamento 2 quartos acima de 70m², bem localizado")

    assert enviadas == [FRASE_PROBLEMINHA]               # não repete "probleminha"
    row = _pendente()
    assert row.texto.splitlines() == ["Quero", "apartamento 2 quartos acima de 70m², bem localizado"]
    with SessionLocal() as s:
        n = s.execute(select(PessoaRetomada).where(PessoaRetomada.telefone == TEL)).scalars().all()
    assert len(n) == 1                                   # 1 pendente por telefone


def test_erro_generico_no_atendimento_tambem_enfileira(monkeypatch):
    monkeypatch.setattr(ab, "_rodar_pessoa",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("banco caiu")))
    monkeypatch.setattr(mr, "_alertar_dona", lambda *a, **k: True)
    assert ab.atender(TEL, "Quero")["motivo"] == "erro"
    row = _pendente()
    assert row is not None and row.texto == "Quero" and "banco caiu" in (row.erro or "")


# ── 4. tick de retomada ──────────────────────────────────────────────────────

def test_tick_retoma_e_responde_pela_linha_da_corretor(monkeypatch):
    agora = datetime.now(timezone.utc)
    monkeypatch.setattr(mr, "_alertar_dona", lambda *a, **k: True)
    mr.enfileirar(TEL, "Quero", "Elisangela", erro="todos fora", agora=agora - timedelta(hours=1))
    from app.services.conversations import append_message
    append_message(TEL, "lead", None, "user", "Quero")
    append_message(TEL, "pessoa", None, "assistant", FRASE_PROBLEMINHA)

    vistos: dict = {}

    def _executar(tel, texto, nome, historico):
        vistos.update(tel=tel, texto=texto, nome=nome, historico=historico)
        return {"resposta": "Voltei! Me conta: pra morar ou investir?", "provider_falhou": False, "erro": None}

    monkeypatch.setattr(ab, "_executar_pessoa", _executar)
    enviadas: list[tuple[str, str]] = []
    monkeypatch.setattr(ab, "_responder", lambda tel, txt: enviadas.append((tel, txt)))

    res = mr.tick(agora=agora)

    assert res.respondidas == 1 and res.considerados == 1
    assert vistos["texto"] == "Quero" and vistos["nome"] == "Elisangela"
    # o turno gravado na 1ª tentativa sai do histórico (senão o modelo lê 2x)
    assert all(h["text"] != "Quero" for h in vistos["historico"] if h["role"] == "user")
    assert enviadas == [(TEL, "Voltei! Me conta: pra morar ou investir?")]
    row = _pendente()
    assert row.status == "feita" and row.tentativas == 1 and row.texto == "Quero"
    from app.services.conversations import load_history
    assert load_history(TEL)[-1]["text"] == "Voltei! Me conta: pra morar ou investir?"


def test_tick_provider_ainda_fora_adia_com_backoff_e_nao_perde_o_texto(monkeypatch):
    agora = datetime.now(timezone.utc)
    monkeypatch.setattr(mr, "_alertar_dona", lambda *a, **k: True)
    mr.enfileirar(TEL, "Quero", None, erro="x", agora=agora - timedelta(hours=1))
    monkeypatch.setattr(ab, "_executar_pessoa",
                        lambda *a: {"resposta": None, "provider_falhou": True, "erro": "ainda fora"})
    monkeypatch.setattr(ab, "_responder", lambda *a: pytest.fail("não pode enviar sem resposta"))

    res = mr.tick(agora=agora)

    assert res.adiadas == 1
    row = _pendente()
    assert row.status == "pendente" and row.tentativas == 1 and row.texto == "Quero"
    assert row.proxima_em > agora
    # ainda não venceu: um novo tick agora não pega
    assert mr.tick(agora=agora).considerados == 0


def test_tick_desiste_no_limite_e_avisa_a_dona(monkeypatch):
    agora = datetime.now(timezone.utc)
    alertas: list[bool] = []
    monkeypatch.setattr(mr, "_alertar_dona",
                        lambda tel, erro, rid, ag, desistiu=False: alertas.append(desistiu) or True)
    mr.enfileirar(TEL, "Quero", None, erro="x", agora=agora - timedelta(days=1))
    with SessionLocal() as s:
        row = s.get(PessoaRetomada, _pendente().id)
        row.tentativas = mr.MAX_TENTATIVAS - 1
        row.proxima_em = agora - timedelta(minutes=1)
        s.commit()
    monkeypatch.setattr(ab, "_executar_pessoa",
                        lambda *a: {"resposta": None, "provider_falhou": True, "erro": "ainda fora"})

    res = mr.tick(agora=agora)

    assert res.desistidas == 1
    assert _pendente().status == "desistiu"
    assert alertas == [False, True]      # 1 aviso ao enfileirar + 1 ao desistir


def test_tick_cancela_se_humano_assumiu(monkeypatch):
    agora = datetime.now(timezone.utc)
    monkeypatch.setattr(mr, "_alertar_dona", lambda *a, **k: True)
    mr.enfileirar(TEL, "Quero", None, erro="x", agora=agora - timedelta(hours=1))
    monkeypatch.setattr(ab, "_humano_falando", lambda t, desde=None: True)
    monkeypatch.setattr(ab, "_executar_pessoa", lambda *a: pytest.fail("não roda com humano no comando"))

    res = mr.tick(agora=agora)

    assert res.canceladas == 1 and _pendente().status == "cancelada"


def test_worker_nao_sobe_sob_pytest():
    assert mr.iniciar_worker() is None


# ── 5. diagnóstico: a causa real aparece ─────────────────────────────────────

def test_claude_cli_le_o_usage_limit_do_stdout():
    """05/09: o `claude -p` sai exit 1 com o motivo no STDOUT e só ruído de
    settings no stderr — era classificado 'transitório' e o log escondia tudo."""
    from app.llm.cli_provider import ClaudeCliProvider, CliProviderError

    proc = MagicMock(returncode=1, stdout="Claude AI usage limit reached|1757100000",
                     stderr="Permission deny rule (.claude/settings.local.json): Write(/etc/**) is not matched\n")
    with patch("subprocess.run", return_value=proc):
        with pytest.raises(CliProviderError) as ei:
            ClaudeCliProvider(model="claude-sonnet-4-6").complete("s", "u", {"type": "object"})
    assert ei.value.transient is False
    assert "usage limit" in str(ei.value)
    assert "Permission deny" not in str(ei.value)


def test_codex_mostra_a_linha_error_e_nao_o_eco_do_prompt():
    """O stderr do codex traz preâmbulo + o prompt inteiro + 'ERROR: ...' no
    fim. O log de 300 chars mostrava só 'Reading additional input from stdin'."""
    from app.llm.codex_provider import CodexProvider, CodexProviderError, _erro_relevante

    stderr = (
        "Reading additional input from stdin...\nOpenAI Codex v0.153.4\n--------\n"
        "workdir: /tmp\nmodel: gpt-5.6-terra\n--------\nuser\n" + ("bla " * 5000) +
        "\nERROR: You've hit your usage limit. try again at Sep 7th, 2026 2:39 AM.\n"
        "ERROR: You've hit your usage limit. try again at Sep 7th, 2026 2:39 AM.\n"
    )
    rel = _erro_relevante(stderr)
    assert rel.startswith("ERROR: You've hit your usage limit") and rel.count("ERROR") == 1
    proc = MagicMock(returncode=1, stdout="", stderr=stderr)
    with patch("subprocess.run", return_value=proc):
        with pytest.raises(CodexProviderError) as ei:
            CodexProvider().complete_text("s", "u")
    assert ei.value.transient is False
    assert "usage limit" in str(ei.value) and "Reading additional" not in str(ei.value)


def test_palavra_da_conversa_nao_vira_falha_permanente():
    """'quota'/'credit' no ECO do prompt não pode classificar a falha."""
    from app.llm.codex_provider import _classify_stderr, _erro_relevante

    stderr = "user\nlead: minha quota do FGTS e credit score\nstream disconnected\n"
    assert _classify_stderr(_erro_relevante(stderr, limite=2000)) is True


# ── 6. terceiro nível: Qwen quando Claude E codex caem ───────────────────────

def test_reserva_final_qwen_entra_quando_os_dois_de_assinatura_caem(monkeypatch):
    from app import config
    from app.llm.codex_provider import FallbackProvider
    from app.llm.modelstudio_provider import ModelStudioProvider, StatusDegrau

    monkeypatch.setattr(config.settings, "modelstudio_api_key", "chave-de-teste", raising=False)
    monkeypatch.setattr(config.settings, "modelstudio_base_url", "https://ws-teste.example/v1", raising=False)
    monkeypatch.setattr(config.settings, "modelstudio_model_analyzer", "qwen3.8-max", raising=False)
    monkeypatch.setattr(config.settings, "cli_reserva_modelstudio", True, raising=False)
    monkeypatch.setattr(config.settings, "cli_reserva_codex", True, raising=False)
    # O degrau só entra se RESPONDER (05/09) — aqui a prova é simulada.
    monkeypatch.setattr(ModelStudioProvider, "verificar", lambda self, **k: StatusDegrau(ok=True))

    prov = config._claude_com_reserva("claude-sonnet-4-6", "gpt-5.6-terra")
    assert isinstance(prov, FallbackProvider)
    assert isinstance(prov.backup, ModelStudioProvider)
    assert isinstance(prov.primary, FallbackProvider)

    def _cai(*a, **k):
        raise RuntimeError("cota")

    monkeypatch.setattr(prov.primary.primary, "complete", _cai)
    monkeypatch.setattr(prov.primary.backup, "complete", _cai)
    monkeypatch.setattr(prov.backup, "complete", lambda *a, **k: (RESPOSTA_OK, MagicMock()))
    json_str, _ = prov.complete("s", "u", {"type": "object"})
    assert json.loads(json_str)["text"].startswith("Oi!")


def test_sem_chave_qwen_a_rede_fica_em_dois_niveis(monkeypatch):
    from app import config
    from app.llm.codex_provider import CodexProvider, FallbackProvider

    monkeypatch.setattr(config.settings, "modelstudio_api_key", "", raising=False)
    monkeypatch.setattr(config.settings, "modelstudio_base_url", "", raising=False)
    monkeypatch.setattr(config.settings, "qwen_env_file", "/nao/existe/qwen.env", raising=False)
    prov = config._claude_com_reserva("claude-sonnet-4-6", "gpt-5.6-terra")
    assert isinstance(prov, FallbackProvider) and isinstance(prov.backup, CodexProvider)
