"""
Formatação do briefing matinal da corretora (WhatsApp) — função pura.

Extraído de `app/services/briefing_corretora.py` (Onda 2 · squad Acme). O
serviço `briefing_corretora` segue sendo o entry-point (coleta dados + envia);
aqui vive só a MONTAGEM DO TEXTO, testável isolada a partir de um DadosBriefing.

Nota: `_build_cta_atendimento` consulta `adocao_metricas.oportunidades_pendentes`
(import diferido, degradação silenciosa em falha) — comportamento idêntico ao
original. É a única dependência externa da montagem; o resto é puro.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from app.services.briefing_corretora import DadosBriefing

log = structlog.get_logger()


def _label_status(status: str) -> str:
    if status == "compareceu":
        return " [compareceu]"
    if status == "no_show":
        return " [não compareceu]"
    if status == "sem_status":
        return " ⚠️ sem status"
    return ""


def montar_texto(dados: "DadosBriefing") -> str:
    nome_curto = dados.corretora_nome.split()[0]
    if dados.corretora_nome == "Pessoa3":
        nome_curto = "Pessoa12"
    elif dados.corretora_nome == "Pessoa1":
        nome_curto = "Pessoa11"
    elif dados.corretora_nome == "Pessoa5":
        nome_curto = "Pessoa10"

    linhas: list[str] = [f"Bom dia, {nome_curto}! 💛 assistente aqui 🐱", ""]

    # 📊 conversas 24h
    c = dados.conversas_24h
    if c and (c.enviadas or c.recebidas):
        linhas.append("📊 *conversas 24h*")
        linhas.append(f"• {c.enviadas} mensagens enviadas")
        linhas.append(f"• {c.recebidas} recebidas")
        linhas.append(f"• {c.contatos_unicos} contatos diferentes")
        if c.destinatarios:
            linhas.append(
                f"• {c.responderam} de {c.destinatarios} destinatários "
                f"responderam ({c.pct_respondeu}%)"
            )
        linhas.append("")

    # ⏰ tarefas atrasadas (formato enxuto)
    # 31/07: era "na kommo". O Kommo saiu de operação e a contagem passou a vir
    # do painel (ver `_coletar_tarefas_atrasadas`) — o texto acompanha a fonte.
    if dados.tarefas_atrasadas is None:
        linhas.append("⏰ tarefas atrasadas: indisponível agora (dá uma olhada no painel).")
        linhas.append("")
    elif dados.tarefas_atrasadas > 0:
        linhas.append(
            f"⏰ você tem *{dados.tarefas_atrasadas} tarefas atrasadas* no painel, "
            f"vale dar uma verificada!"
        )
        linhas.append("")

    # 📅 agenda essa semana
    if dados.visitas_semana:
        linhas.append("📅 *agenda essa semana*")
        for v in dados.visitas_semana:
            bairro = f" ({v.bairro})" if v.bairro else ""
            linhas.append(
                f"• {v.cor_emoji} {v.dia_semana} {v.hora} — {v.lead_nome}{bairro}"
                f"{_label_status(v.status)}"
            )
        linhas.append("")

    # 🔥 leads quentes
    if dados.leads_quentes:
        n = len(dados.leads_quentes)
        titulo = "🔥 *lead quente pra cuidar hoje*" if n == 1 else "🔥 *leads quentes pra cuidar hoje*"
        linhas.append(titulo)
        for lq in dados.leads_quentes:
            linhas.append(f"• *{lq.nome}* — {lq.razao}")
        linhas.append("")

    # Coaching removido a pedido da Dona em 14/05/2026 — corretoras
    # não gostavam de receber feedback de atendimento no briefing matinal.
    # A coleta (`coletar_coaching`) continua rodando pro Agente Dona usar
    # no resumo das 20h, mas não aparece mais no WhatsApp das corretoras.

    # 👉 CTA painel /atendimento — Onda Consumo Fase 1
    cta = _build_cta_atendimento(dados)
    if cta:
        linhas.append(cta)
        linhas.append("")

    linhas.append("bom dia bom 💛 me responde se quiser ajustar alguma coisa 🐱")
    return "\n".join(linhas)


def _build_cta_atendimento(dados: "DadosBriefing") -> str | None:
    """Gera CTA pro painel /atendimento se a corretora tem oportunidades pendentes.
    Retorna None se total=0 ou se a contagem falhar (degradação silenciosa)."""
    try:
        from app.services.adocao_metricas import oportunidades_pendentes
        # corretora_id não está no DadosBriefing — precisa resolver via nome.
        # Mapeamento fixo (consistente com o resto do projeto).
        nome_to_id = {"Pessoa5": 1, "Pessoa1": 2, "Pessoa3": 3}
        cid = nome_to_id.get(dados.corretora_nome)
        if cid is None:
            return None
        counts = oportunidades_pendentes(cid)
    except Exception:
        log.exception("cta_atendimento_failed", corretora=dados.corretora_nome)
        return None

    # CTA enfatiza quente+morno (acionáveis hoje). Parados ficam como linha
    # secundária pra evitar fardo na primeira manhã. Quando adoção subir,
    # avaliar trazer parados pro headline.
    acionaveis = counts["quentes"] + counts["mornos"]
    if acionaveis == 0 and counts["parados"] == 0:
        return None

    if acionaveis > 0:
        partes = []
        if counts["quentes"]:
            partes.append(f"{counts['quentes']} 🔥")
        if counts["mornos"]:
            partes.append(f"{counts['mornos']} 🌡️")
        breakdown = " · ".join(partes)
        headline = (
            f"👉 *{acionaveis} oportunidades pra cuidar hoje*"
            if acionaveis > 1
            else "👉 *1 oportunidade pra cuidar hoje*"
        )
        linhas_cta = [headline, breakdown]
    else:
        # Só parados — CTA mais leve, sem listar quantidade na headline
        linhas_cta = ["👉 *leads em atendimento pra revisar*"]

    linhas_cta.append("abre aqui: https://app.example.com/painel/#atendimento")

    if counts["parados"] > 0 and acionaveis > 0:
        linhas_cta.append(f"_+ {counts['parados']} pendentes lá pra revisar quando der_")

    return "\n".join(linhas_cta)
