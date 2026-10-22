"""
Golden tests — app/delivery/corretora_formatter.py (Onda 2 · squad Acme).

Provam que montar_texto(), dado um DadosBriefing FIXO, produz o texto EXATO
capturado ANTES da extração de services/briefing_corretora.py (zero drift).

`oportunidades_pendentes` (única dependência externa, via CTA) é sempre mockada.
"""
from __future__ import annotations

from datetime import date
from unittest.mock import patch

from app.delivery import corretora_formatter as fmt
from app.services.briefing_corretora import (
    ConversasJanela,
    DadosBriefing,
    LeadQuente,
    VisitaSemanal,
)


def _viz(dia, d, hora, nome, status, emoji, bairro=None) -> VisitaSemanal:
    return VisitaSemanal(dia_semana=dia, data=d, hora=hora, lead_nome=nome,
                         bairro=bairro, status=status, cor_emoji=emoji)


def test_montar_texto_completo_golden(golden_corretora) -> None:
    dados = DadosBriefing(
        corretora_nome="Pessoa3",
        ligacoes_semana=87, contatos_semana=14, tarefas_atrasadas=3,
        visitas_semana=[
            _viz("Seg", date(2026, 5, 12), "10h", "João", "futura", "🟠", "Centro"),
            _viz("Ter", date(2026, 5, 13), "14h", "Pessoa", "compareceu", "🟢"),
            _viz("Qua", date(2026, 5, 14), "09h", "Pedro", "sem_status", "🟡"),
        ],
        leads_retomar=[], coaching=[],
        snapshot_disponivel=True,
        conversas_24h=ConversasJanela(enviadas=30, recebidas=20, contatos_unicos=15,
                                      destinatarios=12, responderam=6, pct_respondeu=50),
        leads_quentes=[LeadQuente(nome="Corretor", razao="Quer visitar amanhã")],
    )
    with patch("app.services.adocao_metricas.oportunidades_pendentes",
               return_value={"quentes": 2, "mornos": 1, "parados": 5}):
        out = fmt.montar_texto(dados)
    assert out == golden_corretora["completo"]


def test_montar_texto_minimo_golden(golden_corretora) -> None:
    dados = DadosBriefing(
        corretora_nome="Pessoa5",
        ligacoes_semana=0, contatos_semana=0, tarefas_atrasadas=0,
        conversas_24h=None,
    )
    with patch("app.services.adocao_metricas.oportunidades_pendentes",
               return_value={"quentes": 0, "mornos": 0, "parados": 0}):
        out = fmt.montar_texto(dados)
    assert out == golden_corretora["minimo"]


def test_montar_texto_tarefas_indisponivel_golden(golden_corretora) -> None:
    dados = DadosBriefing(
        corretora_nome="Pessoa1",
        ligacoes_semana=0, contatos_semana=0, tarefas_atrasadas=None,
        conversas_24h=ConversasJanela(enviadas=5, recebidas=2, contatos_unicos=3,
                                      destinatarios=0, responderam=0, pct_respondeu=0),
    )
    with patch("app.services.adocao_metricas.oportunidades_pendentes",
               return_value={"quentes": 0, "mornos": 0, "parados": 3}):
        out = fmt.montar_texto(dados)
    assert out == golden_corretora["tarefas_indisponivel"]


def test_label_status() -> None:
    assert fmt._label_status("compareceu") == " [compareceu]"
    assert fmt._label_status("no_show") == " [não compareceu]"
    assert fmt._label_status("sem_status") == " ⚠️ sem status"
    assert fmt._label_status("futura") == ""


def test_briefing_corretora_reexports_preservados() -> None:
    """briefing_corretora segue sendo entry-point: re-exporta montar_texto."""
    import app.services.briefing_corretora as bc
    assert hasattr(bc, "montar_texto")
    assert hasattr(bc, "_label_status")
    assert hasattr(bc, "_build_cta_atendimento")
    assert bc.montar_texto is fmt.montar_texto
