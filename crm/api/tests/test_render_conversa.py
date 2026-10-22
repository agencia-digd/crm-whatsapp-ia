"""
Testes do contrato de ingestão único `render_conversa()` (P1 — mata I2/contradição
entre camadas). Cobre:
  - representação canônica: ordem, timestamps BRT, rótulos LEAD/CORRETORA;
  - áudio hidratado (transcrição real, não placeholder);
  - HSM/template e mídia coalescidos;
  - classifier E analyzer recebem a MESMA string de conversa (fonte única).

Zero chamada real ao LLM — provider mockado captura o user_prompt.
"""
import json

from unittest.mock import MagicMock

from app.inspector import render_conversa as rc_mod
from app.inspector.analyzer import analyze_lead
from app.inspector.classifier import classify_lead
from app.inspector.render_conversa import render_conversa, render_message
from app.llm.provider import TokenUsage


# ── Fixtures ──────────────────────────────────────────────────────────────────

_LEAD = {"lead_id": "1", "nome": "João", "etapa_nome": "Primeiro Contato"}

# Conversa com áudio transcrito, imagem com legenda e texto. Timestamps UTC —
# render deve exibir em BRT (UTC-3).
_CONV = [
    {"from": "corretora", "text": "Oi João, vi seu interesse",
     "at": "2026-05-01T12:00:00+00:00", "type": "text"},
    {"from": "lead", "text": "quero um apto de 2 quartos na Bairro X",
     "at": "2026-05-01T12:05:00+00:00", "type": "audio",
     "transcricao": "quero um apto de 2 quartos na Bairro X",
     "transcricao_status": "concluida"},
    {"from": "corretora", "text": "Tenho essa opção aqui",
     "at": "2026-05-01T12:10:00+00:00", "type": "image"},
    {"from": "lead", "text": "gostei, qual o valor?",
     "at": "2026-05-01T12:15:00+00:00", "type": "text"},
]


def _capture_provider() -> MagicMock:
    """Provider mock que devolve JSON válido e permite capturar o user_prompt."""
    provider = MagicMock()
    payload = {
        "temperatura": "morno",
        "deserves_deep_analysis": True,
        "reason": "engajou",
        # campos do analyzer (ignorados pelo classifier):
        "conversa_resumo": "Lead pediu 2 quartos.",
        "o_que_corretora_disse": "vi seu interesse",
        "gap_identificado": None,
        "abordagem_sugerida": "Quer que eu mande as fotos?",
    }
    provider.complete.return_value = (json.dumps(payload), TokenUsage(100, 50))
    return provider


def _user_prompt(provider: MagicMock) -> str:
    """user_prompt (2º posicional) da última chamada complete(system, user, schema)."""
    args = provider.complete.call_args
    return args.args[1] if len(args.args) > 1 else args.kwargs["user_prompt"]


# ── render_conversa — representação canônica ─────────────────────────────────

class TestRenderConversaCanonico:
    def test_vazio(self) -> None:
        assert render_conversa([]) == "[sem mensagens]"

    def test_header_brt_presente(self) -> None:
        out = render_conversa(_CONV)
        assert out.splitlines()[0] == "(horários em Brasília / BRT)"

    def test_ordem_preservada(self) -> None:
        out = render_conversa(_CONV)
        idx_oi = out.index("vi seu interesse")
        idx_valor = out.index("qual o valor?")
        assert idx_oi < idx_valor

    def test_rotulos_lead_corretora(self) -> None:
        out = render_conversa(_CONV)
        assert "CORRETORA: Oi João, vi seu interesse" in out
        assert "LEAD:" in out

    def test_timestamp_convertido_para_brt(self) -> None:
        # 12:00 UTC → 09:00 BRT (UTC-3)
        out = render_conversa(_CONV)
        assert "[2026-05-01 09:00] CORRETORA: Oi João" in out

    def test_audio_hidratado_nao_placeholder(self) -> None:
        out = render_conversa(_CONV)
        assert '[áudio]: "quero um apto de 2 quartos na Bairro X"' in out
        assert "[áudio não disponível]" not in out
        assert "[áudio em processamento]" not in out

    def test_imagem_com_legenda_coalescida(self) -> None:
        out = render_conversa(_CONV)
        assert '[imagem]: "Tenho essa opção aqui"' in out


# ── render_message — hidratação por tipo ─────────────────────────────────────

class TestRenderMessage:
    def test_audio_pendente_vira_processamento(self) -> None:
        m = {"from": "lead", "type": "audio", "transcricao": None,
             "transcricao_status": "pendente"}
        assert render_message(m) == "[áudio em processamento]"

    def test_audio_sem_transcricao_indisponivel(self) -> None:
        m = {"from": "lead", "type": "audio", "transcricao": None,
             "transcricao_status": "erro"}
        assert render_message(m) == "[áudio não disponível]"

    def test_imagem_com_legenda(self) -> None:
        m = {"from": "lead", "type": "image", "text": "olha essa casa"}
        assert render_message(m) == '[imagem]: "olha essa casa"'

    def test_texto_vazio_vira_sem_texto(self) -> None:
        # HSM/template que chegou sem conteudo_texto hidratado
        m = {"from": "corretora", "type": "text", "text": ""}
        assert render_message(m) == "[sem texto]"

    def test_texto_hidratado(self) -> None:
        m = {"from": "corretora", "type": "text", "text": "Segue proposta"}
        assert render_message(m) == "Segue proposta"


# ── Contrato único: classifier e analyzer veem a MESMA conversa ──────────────

class TestFonteUnica:
    def test_classifier_e_analyzer_recebem_string_identica(self) -> None:
        """Mesma lista de mensagens → o bloco de conversa no prompt é byte-idêntico."""
        canonical = render_conversa(_CONV)

        prov_c = _capture_provider()
        classify_lead(_LEAD, _CONV, provider=prov_c)
        prompt_classifier = _user_prompt(prov_c)

        prov_a = _capture_provider()
        analyze_lead(_LEAD, _CONV, corretora_nome="Pessoa10",
                     provider=prov_a, dias_silencio_lead=0)
        prompt_analyzer = _user_prompt(prov_a)

        # o texto canônico da conversa aparece INTEGRAL nos dois prompts
        assert canonical in prompt_classifier
        assert canonical in prompt_analyzer

    def test_audio_visivel_para_ambas_camadas(self) -> None:
        """I2: a transcrição real do áudio chega às DUAS camadas (não placeholder)."""
        transcricao = "quero um apto de 2 quartos na Bairro X"

        prov_c = _capture_provider()
        classify_lead(_LEAD, _CONV, provider=prov_c)
        prov_a = _capture_provider()
        analyze_lead(_LEAD, _CONV, provider=prov_a, dias_silencio_lead=0)

        for prompt in (_user_prompt(prov_c), _user_prompt(prov_a)):
            assert f'[áudio]: "{transcricao}"' in prompt
            assert "[áudio não disponível]" not in prompt

    def test_render_conversa_e_a_unica_fonte(self) -> None:
        """Sanidade: os módulos consomem render_conversa (import direto)."""
        import app.inspector.classifier as clf
        import app.inspector.analyzer as corretor
        assert clf.render_conversa is rc_mod.render_conversa
        assert corretor.render_conversa is rc_mod.render_conversa
