from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.inspector_v2.models import AnalysisPayload
from app.inspector_v2.providers import (
    CodexCliProvider,
    GrokCliProvider,
    ProviderError,
    QwenTokenPlanProvider,
    build_prompt,
)


PAYLOAD = {
    "temperatura_atual": "morno",
    "intencao_maxima": "media",
    "resumo_jornada": "ok",
    "evidencias_intencao": [],
    "financeiro": [],
    "objecoes": [],
    "perguntas_sem_resposta": [],
    "visita": "nao_proposta",
    "gap_corretora": None,
    "proxima_acao": "retomar",
    "proxima_acao_requer_revisao_humana": True,
    "acao_externa_executada": False,
    "citacoes": [],
    "confianca": "baixa",
}


def _proc(stdout="", stderr="", returncode=0):
    result = MagicMock()
    result.stdout, result.stderr, result.returncode = stdout, stderr, returncode
    return result


def test_all_adapters_share_identical_prompt_builder() -> None:
    schema = AnalysisPayload.model_json_schema()
    first = build_prompt("system", "user", schema)
    second = build_prompt("system", "user", schema)
    assert first == second and "JSON Schema" in first


def test_codex_contract_uses_stdin_and_tmp_schema() -> None:
    with patch("shutil.which", return_value="/bin/codex"), patch("subprocess.run", return_value=_proc()) as run:
        def side_effect(*args, **kwargs):
            cmd = args[0]
            output_path = Path(cmd[cmd.index("--output-last-message") + 1])
            output_path.write_text(json.dumps(PAYLOAD), encoding="utf-8")
            return _proc()
        run.side_effect = side_effect
        CodexCliProvider("default").complete("system", "CONVERSA_REDIGIDA", AnalysisPayload.model_json_schema())
    assert "CONVERSA_REDIGIDA" in run.call_args.kwargs["input"]
    assert "CONVERSA_REDIGIDA" not in " ".join(run.call_args.args[0])


def test_grok_contract_uses_prompt_file_and_safe_flags() -> None:
    outer = json.dumps({"result": PAYLOAD})
    with patch("shutil.which", return_value="/bin/grok"), patch("subprocess.run", return_value=_proc(outer)) as run:
        GrokCliProvider("default").complete("system", "user", AnalysisPayload.model_json_schema())
    command = run.call_args.args[0]
    assert "--prompt-file" in command and "--disable-web-search" in command and "--no-subagents" in command
    assert command[command.index("--max-turns") + 1] == "2"


def test_qwen_is_exact_token_plan_model_and_full_prompt_uses_remote_p_flag() -> None:
    outer = json.dumps([
        {"type": "system", "content": "started"},
        {"type": "assistant", "message": {"content": json.dumps(PAYLOAD)}},
        {"type": "result", "usage": {"total_tokens": 42}},
    ])
    with patch("subprocess.run", return_value=_proc(outer)) as run:
        response = QwenTokenPlanProvider().complete("system", "CONVERSA_REDIGIDA", AnalysisPayload.model_json_schema())
    command = " ".join(run.call_args.args[0])
    assert "/usr/local/bin/qwen-token-plan" in command
    assert "qwen3.8-max" in command
    assert "CONVERSA_REDIGIDA" in command
    assert run.call_args.kwargs["stdin"] is not None
    assert json.loads(response.content)["temperatura_atual"] == "morno"


def test_qwen_unwraps_functional_json_from_unknown_text_field() -> None:
    outer = json.dumps(
        [
            {"type": "system", "content": "started"},
            {"type": "assistant", "opaque_payload": json.dumps(PAYLOAD)},
        ]
    )
    with patch("subprocess.run", return_value=_proc(outer)):
        response = QwenTokenPlanProvider().complete(
            "system", "CONVERSA_REDIGIDA", AnalysisPayload.model_json_schema()
        )
    assert json.loads(response.content)["intencao_maxima"] == "media"


def test_qwen_fails_closed_when_success_envelope_contains_api_error() -> None:
    outer = json.dumps(
        [
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "API Error: invalid API-key"}]}},
            {"type": "result", "subtype": "success"},
        ]
    )
    with patch("subprocess.run", return_value=_proc(outer)):
        with pytest.raises(ProviderError, match="authentication failure"):
            QwenTokenPlanProvider().complete(
                "system", "CONVERSA_REDIGIDA", AnalysisPayload.model_json_schema()
            )
