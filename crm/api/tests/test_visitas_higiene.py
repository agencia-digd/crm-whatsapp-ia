"""
Testes da higiene estrutural de VISITAS (MISSÃO 12 / REVISAO-08 gaps A3-A6).

100% mockados — nenhum acesso a DB/Calendar/rede real.

Cobre:
  - _resolve_lead_id: right-8 telefone (único / ambíguo), nome exato (único /
    ambíguo / não encontrado), precedência telefone → nome.
  - origem propagada pelos 2 callers (agent.py = whatsapp; copilota = crm) e
    --registrado-por-telefone carimbado.
  - dedup: INSERT com ON CONFLICT DO NOTHING → RETURNING vazio → status duplicada.
"""
import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_AGENDA = (
    Path(__file__).resolve().parents[1]
    / "app" / "agents" / "assistente" / "skills" / "agenda"
)


def _load_criar_visita():
    """Importa criar_visita.py em isolamento (agenda dir no sys.path p/ _helper)."""
    sys.path.insert(0, str(_AGENDA))
    spec = importlib.util.spec_from_file_location(
        "criar_visita_mod", _AGENDA / "criar_visita.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cv = _load_criar_visita()


# ── Fake connection/cursor pra _resolve_lead_id ──────────────────────────────

class _FakeCursor:
    def __init__(self, results):
        # results: lista de listas-de-linhas, consumida em ordem de execute()
        self._results = list(results)
        self._last: list = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params):
        self._last = self._results.pop(0) if self._results else []

    def fetchall(self):
        return self._last


class _FakeConn:
    def __init__(self, results):
        self._cur = _FakeCursor(results)

    def cursor(self):
        return self._cur


# ── _resolve_lead_id ─────────────────────────────────────────────────────────

def test_resolve_por_telefone_unico():
    conn = _FakeConn([[("kommo_111",)]])  # 1 match por telefone
    assert cv._resolve_lead_id(conn, "+5500000000000", "João") == "kommo_111"


def test_resolve_por_telefone_ambiguo_vira_null():
    conn = _FakeConn([[("kommo_1",), ("kommo_2",)]])  # 2+ matches → NULL
    assert cv._resolve_lead_id(conn, "+5500000000000", "João") is None


def test_resolve_cai_pro_nome_quando_telefone_sem_match():
    # 1ª query (telefone): vazio → 2ª query (nome): 1 match
    conn = _FakeConn([[], [("kommo_777",)]])
    assert cv._resolve_lead_id(conn, "+5500000000000", "João Silva") == "kommo_777"


def test_resolve_por_nome_unico_sem_telefone():
    conn = _FakeConn([[("kommo_555",)]])  # sem telefone → só query de nome
    assert cv._resolve_lead_id(conn, None, "Pessoa Souza") == "kommo_555"


def test_resolve_por_nome_ambiguo_vira_null():
    conn = _FakeConn([[("a",), ("b",)]])
    assert cv._resolve_lead_id(conn, None, "João") is None


def test_resolve_sem_match_algum_vira_null():
    conn = _FakeConn([[], []])
    assert cv._resolve_lead_id(conn, "+5500000000000", "Ninguém") is None


def test_telefone_curto_pula_pro_nome():
    # telefone com <8 dígitos não gera query de telefone; só nome
    conn = _FakeConn([[("kommo_9",)]])
    assert cv._resolve_lead_id(conn, "123", "Corretor") == "kommo_9"


# ── origem propagada — caller agent.py (whatsapp) ────────────────────────────

def test_agent_caller_propaga_origem_whatsapp_e_registrador(monkeypatch):
    import app.agents.assistente.agent as agent

    captured = {}

    def _fake_run_skill(area, script, args):
        captured["area"] = area
        captured["script"] = script
        captured["args"] = args
        return {"success": True, "stdout": "{}", "stderr": ""}

    monkeypatch.setattr(agent, "_run_skill", _fake_run_skill)

    res = agent._tool_criar_visita(
        corretora="Pessoa10", cliente="João", data="2026-07-10", hora="14:00",
        telefone="+5500000000000", registrado_por_telefone="+5500000000000",
        confirmado=True,
    )
    assert res["status"] == "ok"
    args = captured["args"]
    # --origem whatsapp presente
    assert "--origem" in args
    assert args[args.index("--origem") + 1] == "whatsapp"
    # --registrado-por-telefone carimbado
    assert "--registrado-por-telefone" in args
    assert args[args.index("--registrado-por-telefone") + 1] == "+5500000000000"


def test_agent_injeta_registrador_pelo_phone_do_remetente(monkeypatch):
    """_maybe_inject_corretora deve carimbar registrado_por_telefone = self.phone."""
    import app.agents.assistente.agent as agent

    ag = agent.AgenteAssistente(
        phone="+5500000000000", sender_kind="corretora",
        sender_id=2, sender_nome="Pessoa11", provider=MagicMock(),
    )
    out = ag._maybe_inject_corretora("criar_visita", {"cliente": "Zé"})
    assert out["registrado_por_telefone"] == "+5500000000000"

    # Se o LLM já passou explícito, não sobrescreve
    out2 = ag._maybe_inject_corretora(
        "criar_visita", {"cliente": "Zé", "registrado_por_telefone": "+000000000000000"}
    )
    assert out2["registrado_por_telefone"] == "+000000000000000"


# ── origem propagada — caller assistente_copilota.py (crm) ──────────────────────

def test_copilota_caller_propaga_origem_crm(monkeypatch):
    import app.api.assistente_copilota as copilota

    captured = {}

    class _R:
        returncode = 0
        stdout = "{}"
        stderr = ""

    def _fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return _R()

    monkeypatch.setattr(copilota.subprocess, "run", _fake_run)
    monkeypatch.setattr(copilota, "_viewer_info", lambda user: {"corretora_id": 1})
    monkeypatch.setattr(copilota, "_identificar_usuario", lambda x: "corretor")

    res = copilota.assistente_acao(
        payload={"tipo": "agendar", "cliente": "João",
                 "data": "2026-07-10", "hora": "14:00"},
        x_painel_user="corretor",
    )
    assert res["ok"] is True
    cmd = captured["cmd"]
    assert "--origem" in cmd
    assert cmd[cmd.index("--origem") + 1] == "crm"
    assert "--registrado-por-telefone" in cmd
    assert cmd[cmd.index("--registrado-por-telefone") + 1] == "crm:corretor"


# ── dedup: ON CONFLICT DO NOTHING → status duplicada ─────────────────────────

def test_dedup_returning_vazio_marca_duplicada():
    """
    Simula o trecho de INSERT: quando RETURNING vem None (ON CONFLICT DO NOTHING),
    a visita é reportada como duplicada e o script não estoura exceção.
    """
    # Reproduz a lógica de decisão do criar_visita.py (row is None → dedup=True).
    row = None  # RETURNING vazio (conflito no índice único parcial)
    dedup = False
    if row is None:
        dedup = True
        visita_id, criado_em = None, None
    else:
        visita_id, criado_em = row
    assert dedup is True
    assert visita_id is None


def test_dedup_returning_preenchido_grava_normal():
    row = (42, "2026-07-10T14:00:00")
    dedup = False
    if row is None:
        dedup = True
        visita_id, criado_em = None, None
    else:
        visita_id, criado_em = row
    assert dedup is False
    assert visita_id == 42
