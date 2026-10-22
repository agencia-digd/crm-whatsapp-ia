"""
Testes do ShadowProvider — o contrato é "não atrapalha a produção".

O que está sendo protegido aqui: a sombra roda nas conversas REAIS das
corretoras. Se ela vazar pro retorno, atrasar o briefing ou derrubar o Inspector,
uma corretora recebe coisa errada (ou não recebe nada). Cada teste abaixo trava
um desses jeitos de dar errado.
"""
import json
import time
import pytest

from app.llm.provider import TokenUsage
from app.llm.shadow_provider import ShadowProvider


class _Fake:
    """Provider de mentira: responde fixo, demora o que mandarem, ou explode."""

    def __init__(self, resposta="", erro=None, demora=0.0) -> None:
        self.resposta = resposta
        self.erro = erro
        self.demora = demora
        self.chamadas = 0

    def _responder(self):
        self.chamadas += 1
        if self.demora:
            time.sleep(self.demora)
        if self.erro:
            raise self.erro
        # Uso de tokens no tipo REAL dos providers. Era um MagicMock: desde que
        # a sombra grava sombra_tokens_in/out no JSONL (17/08/2026), o mock ia
        # parar dentro do json.dumps e o registro do duelo nunca era escrito.
        return self.resposta, TokenUsage(input_tokens=6, output_tokens=4)

    def complete(self, system, user, json_schema=None):
        return self._responder()

    def complete_text(self, system, user):
        return self._responder()


def _shadow(tmp_path, primary, shadow, **kw):
    return ShadowProvider(
        primary, shadow,
        jsonl_path=str(tmp_path / "duelos.jsonl"),
        flag_path="",  # sem flag = sempre ligada
        **kw,
    )


def _esperar_duelos(path, n=1, timeout=5.0):
    """Sombra roda em thread — espera o registro aparecer (sem sleep fixo)."""
    fim = time.monotonic() + timeout
    while time.monotonic() < fim:
        try:
            linhas = [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]
            if len(linhas) >= n:
                return linhas
        except FileNotFoundError:
            pass
        time.sleep(0.05)
    return []


# ── o contrato principal ──────────────────────────────────────────────────────

class TestNaoAfetaProducao:
    def test_retorno_e_sempre_do_primario(self, tmp_path) -> None:
        """A corretora lê o Claude. O Terra é só medido."""
        sp = _shadow(tmp_path, _Fake("PRODUCAO"), _Fake("SOMBRA"))
        texto, _ = sp.complete_text("s", "u")
        assert texto == "PRODUCAO"

    def test_sombra_quebrada_nao_derruba_producao(self, tmp_path) -> None:
        sp = _shadow(tmp_path, _Fake("PRODUCAO"), _Fake(erro=RuntimeError("terra fora")))
        texto, _ = sp.complete_text("s", "u")
        assert texto == "PRODUCAO"
        duelos = _esperar_duelos(sp.jsonl_path)
        assert duelos and duelos[0]["sombra_ok"] is False
        assert "terra fora" in duelos[0]["sombra_erro"]

    def test_sombra_lenta_nao_atrasa_producao(self, tmp_path) -> None:
        """Sombra em thread: o briefing não pode esperar por ela."""
        sp = _shadow(tmp_path, _Fake("PRODUCAO"), _Fake("SOMBRA", demora=2.0))
        inicio = time.monotonic()
        sp.complete_text("s", "u")
        assert time.monotonic() - inicio < 0.5  # não esperou os 2s

    def test_primario_que_falha_propaga(self, tmp_path) -> None:
        """Falha de PRODUÇÃO tem que aparecer — a sombra não pode mascarar."""
        sp = _shadow(tmp_path, _Fake(erro=RuntimeError("claude fora")), _Fake("SOMBRA"))
        with pytest.raises(RuntimeError, match="claude fora"):
            sp.complete_text("s", "u")


# ── registro ──────────────────────────────────────────────────────────────────

class TestRegistro:
    def test_grava_os_dois_lados(self, tmp_path) -> None:
        sp = _shadow(tmp_path, _Fake('{"a":1}'), _Fake('{"a":2}'))
        sp.complete("sistema", "conversa do lead", {"type": "object"})
        d = _esperar_duelos(sp.jsonl_path)[0]
        assert d["primario"] == '{"a":1}'
        assert d["sombra"] == '{"a":2}'
        assert d["metodo"] == "complete"
        assert d["sombra_ms"] >= 0

    def test_prompt_truncado(self, tmp_path) -> None:
        """Guardar a conversa inteira incharia o arquivo e duplicaria dado de lead."""
        sp = _shadow(tmp_path, _Fake("p"), _Fake("s"))
        sp.complete_text("sys", "x" * 5000)
        d = _esperar_duelos(sp.jsonl_path)[0]
        assert len(d["prompt_preview"]) <= 400


# ── freios: cota e liga/desliga ───────────────────────────────────────────────

class TestFreios:
    def test_cap_diario_para_de_duelar(self, tmp_path) -> None:
        """Enquanto a assinatura Codex for $20, o teto protege a cota."""
        sombra = _Fake("SOMBRA")
        sp = _shadow(tmp_path, _Fake("PRODUCAO"), sombra, cap_diario=3)
        for _ in range(6):
            sp.complete_text("s", "u")
            _esperar_duelos(sp.jsonl_path, n=min(sombra.chamadas, 3))
        _esperar_duelos(sp.jsonl_path, n=3)
        time.sleep(0.3)
        assert sombra.chamadas == 3, f"estourou o teto: {sombra.chamadas}"

    def test_producao_segue_apos_o_cap(self, tmp_path) -> None:
        """Bater o teto para a MEDIÇÃO, nunca a operação."""
        sp = _shadow(tmp_path, _Fake("PRODUCAO"), _Fake("SOMBRA"), cap_diario=1)
        for _ in range(3):
            texto, _ = sp.complete_text("s", "u")
            assert texto == "PRODUCAO"

    def test_flag_desligada_nao_duela(self, tmp_path) -> None:
        """rm no flag-file desliga a sombra sem restart."""
        sombra = _Fake("SOMBRA")
        sp = ShadowProvider(
            _Fake("PRODUCAO"), sombra,
            jsonl_path=str(tmp_path / "d.jsonl"),
            flag_path=str(tmp_path / "flag-que-nao-existe"),
        )
        texto, _ = sp.complete_text("s", "u")
        assert texto == "PRODUCAO"
        time.sleep(0.3)
        assert sombra.chamadas == 0

    def test_flag_ligada_duela(self, tmp_path) -> None:
        flag = tmp_path / "on"
        flag.touch()
        sombra = _Fake("SOMBRA")
        sp = ShadowProvider(
            _Fake("PRODUCAO"), sombra,
            jsonl_path=str(tmp_path / "d.jsonl"), flag_path=str(flag),
        )
        sp.complete_text("s", "u")
        assert _esperar_duelos(sp.jsonl_path)


# ── fiação do config ──────────────────────────────────────────────────────────

class TestFiacao:
    def test_off_por_default(self) -> None:
        """Sombra desligada DE FÁBRICA: ligar é decisão explícita, por .env.

        Checa o default da classe, não o valor carregado — em produção o .env
        liga (SHADOW_LLM=true) e isso é esperado. O que não pode acontecer é
        um deploy novo nascer duelando sem ninguém ter pedido.
        """
        from app.config import Settings
        assert Settings.model_fields["shadow_llm"].default is False

    def test_envelopa_quando_ligada(self) -> None:
        from app.config import _envelopar_sombra, settings
        from app.llm.cli_provider import ClaudeCliProvider

        original = settings.shadow_llm
        try:
            settings.shadow_llm = True
            p = _envelopar_sombra(ClaudeCliProvider(model="x"))
            assert isinstance(p, ShadowProvider)
        finally:
            settings.shadow_llm = original

    def test_nao_duela_consigo_mesmo(self) -> None:
        """Se produção JÁ é codex, sombra de codex não faz sentido."""
        from app.config import _envelopar_sombra, settings
        from app.llm.cli_provider import ClaudeCliProvider

        orig_s, orig_p = settings.shadow_llm, settings.llm_provider
        try:
            settings.shadow_llm = True
            settings.llm_provider = "codex"
            p = _envelopar_sombra(ClaudeCliProvider(model="x"))
            assert not isinstance(p, ShadowProvider)
        finally:
            settings.shadow_llm, settings.llm_provider = orig_s, orig_p
