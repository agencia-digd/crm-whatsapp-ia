"""Testa parser de output da Corretor (separa texto do marcador <state>)."""
from app.recovery.responder import _parse_output


def test_parse_normal_qualificando() -> None:
    raw = (
        "Oi João! Aqui é a Corretor da Acme. Ainda tá procurando imóvel pra comprar?\n\n"
        '<state>{"qualified": false, "ended": false, "reason": null}</state>'
    )
    text, qualified, ended, reason = _parse_output(raw)
    assert "Corretor da Acme" in text
    assert "<state>" not in text
    assert qualified is False
    assert ended is False
    assert reason is None


def test_parse_qualified() -> None:
    raw = (
        "Perfeito, João. Anotei tudo. Vou passar pra corretora.\n\n"
        '<state>{"qualified": true, "ended": false, "reason": null}</state>'
    )
    text, qualified, ended, reason = _parse_output(raw)
    assert qualified is True
    assert ended is False


def test_parse_ended_aluguel() -> None:
    raw = (
        "Entendi, João. A Acme trabalha só com vendas. Boa sorte na busca!\n\n"
        '<state>{"qualified": false, "ended": true, "reason": "aluguel"}</state>'
    )
    text, qualified, ended, reason = _parse_output(raw)
    assert ended is True
    assert reason == "aluguel"


def test_parse_ended_opt_out() -> None:
    raw = (
        "Sem problema. Obrigada e bom dia!\n\n"
        '<state>{"qualified": false, "ended": true, "reason": "opt_out"}</state>'
    )
    _, _, ended, reason = _parse_output(raw)
    assert ended is True
    assert reason == "opt_out"


def test_parse_invalid_reason_falls_back_to_none() -> None:
    raw = (
        "texto\n\n"
        '<state>{"qualified": false, "ended": true, "reason": "pessoa_dno"}</state>'
    )
    _, _, ended, reason = _parse_output(raw)
    assert ended is True
    assert reason is None


def test_parse_missing_state_returns_text_only() -> None:
    raw = "Oi, tudo bem?"
    text, qualified, ended, reason = _parse_output(raw)
    assert text == "Oi, tudo bem?"
    assert qualified is False
    assert ended is False
    assert reason is None


def test_parse_malformed_state_json_is_safe() -> None:
    raw = "texto\n\n<state>{not json}</state>"
    text, qualified, ended, reason = _parse_output(raw)
    assert "texto" in text
    assert qualified is False
    assert ended is False


# ── cérebro: a Corretor usa o provider central (migração 28/07) ────────────────────
# Antes chamava `claude -p --model claude-opus-4-7` por subprocess, furando o
# provider — ficaria pra trás na migração e mantinha o modelo mais caro fixado
# em código. Estes testes travam a correção.

class _FakeProvider:
    def __init__(self, saida: str) -> None:
        self.saida = saida
        self.chamadas: list[tuple[str, str]] = []

    def complete(self, system, user, json_schema=None):  # pragma: no cover
        raise AssertionError("Corretor devolve texto + <state>, não JSON puro — use complete_text")

    def complete_text(self, system, user):
        from unittest.mock import MagicMock
        self.chamadas.append((system, user))
        return self.saida, MagicMock(total=42)


def test_generate_reply_usa_o_provider_injetado() -> None:
    from app.recovery.responder import Message, generate_reply

    prov = _FakeProvider('Oi! Tudo bem?\n<state>{"qualified": true, "ended": false}</state>')
    r = generate_reply("Marcos", [Message(role="lead", text="oi")], provider=prov)

    assert r.text == "Oi! Tudo bem?"
    assert r.qualified is True
    assert len(prov.chamadas) == 1
    assert "Marcos" in prov.chamadas[0][1]  # histórico chegou no prompt


def test_sem_provider_cai_no_cerebro_do_sistema(monkeypatch) -> None:
    """None = get_llm_provider() → migra junto com o resto quando virar codex."""
    import app.config as cfg
    from app.recovery.responder import Message, generate_reply

    prov = _FakeProvider("resposta\n<state>{}</state>")
    monkeypatch.setattr(cfg, "get_llm_provider", lambda: prov)
    r = generate_reply("Corretor", [Message(role="lead", text="oi")])
    assert r.text == "resposta"
    assert len(prov.chamadas) == 1


def test_nao_ha_modelo_hardcoded_no_modulo() -> None:
    """Regressão: nenhum modelo fixado em código (era claude-opus-4-7)."""
    from pathlib import Path

    import app.recovery.responder as mod

    fonte = Path(mod.__file__).read_text(encoding="utf-8")
    codigo = "\n".join(
        ln for ln in fonte.splitlines()
        if not ln.strip().startswith("#")
    ).split('"""')  # remove docstrings (o histórico cita o modelo antigo)
    codigo = "".join(codigo[::2])
    assert "claude-opus" not in codigo
    assert "subprocess" not in codigo
