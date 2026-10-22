"""Áudios gravados pelas corretoras, pra mandar quando o lead pede.

Gravados em 04/08/2026 pelas próprias corretoras, a partir do roteiro aprovado
pela Dona (projetos/acme/gestao/COPIES-REGUA-V2.md). Assunto: como comparar
dois imóveis sem errar — posição do sol, condomínio, planta, e que o valor do
anúncio não é o final.

O conteúdo é genérico DE PROPÓSITO: não fala de imóvel específico, de valor nem
de condição. Por isso não vence, e o mesmo arquivo serve por meses.

POR QUE .oga E NÃO .wav: o `.oga` é o arquivo que saiu do WhatsApp, OGG/Opus
mono 48kHz — o formato nativo de mensagem de voz. Reenviar exatamente ele faz o
áudio chegar como mensagem de voz de verdade, com a onda sonora no player e o
avanço de velocidade. Converter (mesmo pra mp3 de qualidade alta) faz o WhatsApp
tratar como ANEXO de áudio: ícone de arquivo, sem onda, sem player embutido.
Parece detalhe e não é — anexo tem muito menos gente que abre.

A voz é a da corretora DONA do lead: quem já conversou com ele. Na linha da Pessoa12
quem grava é a Pessoa, que é quem atende ali.
"""
from __future__ import annotations

from pathlib import Path

import structlog

log = structlog.get_logger()

DIR_AUDIOS = Path("/var/lib/copiloto/audios")

# corretora_id → arquivo. Pessoa10=1, Pessoa11=2, Pessoa12=3 (a Corretor não entra: o carrossel
# dela é outra régua, sem esse toque).
AUDIO_COMPARAR: dict[int, str] = {
    1: "comparar-imoveis-corretor.oga",
    2: "comparar-imoveis-corretor.oga",
    3: "comparar-imoveis-corretor.oga",
}

MIME_OGG = "audio/ogg; codecs=opus"


def caminho_audio(corretora_id: int | None) -> Path | None:
    """Arquivo do áudio da corretora, se existir no disco.

    Devolve None em vez de estourar: áudio faltando não pode derrubar o tick.
    Quem chama decide o que fazer (o toque simplesmente não manda mídia).
    """
    nome = AUDIO_COMPARAR.get(int(corretora_id)) if corretora_id else None
    if not nome:
        return None
    caminho = DIR_AUDIOS / nome
    if not caminho.exists():
        log.warning("audio_corretora_ausente", corretora_id=corretora_id, arquivo=nome)
        return None
    return caminho


def tem_audio(corretora_id: int | None) -> bool:
    return caminho_audio(corretora_id) is not None
