"""Toque D3 — oferece o áudio da corretora e manda pra quem pediu.

O desenho (régua V2, 04/08/2026): o toque NÃO manda o arquivo. Pergunta se
pode mandar. O "sim" faz três coisas de uma vez — abre a janela de 24h (o áudio
sai sem template e sem custo), é micro-compromisso, e garante que só recebe
quem quis.

Os áudios foram gravados pelas próprias corretoras: a voz que o lead ouve é a
de quem já está conversando com ele.
"""
from __future__ import annotations

import pytest

from app.services import cadencia as c
from app.services import audios_corretora as ac


@pytest.mark.parametrize("texto", ["sim", "quero", "manda", "pode mandar",
                                   "claro", "manda sim", "Pode"])
def test_reconhece_o_sim(texto):
    assert c._quer_o_audio(texto) is True


@pytest.mark.parametrize("texto", ["não", "agora não", "depois", "2 quartos",
                                   "qual o valor?", "", None])
def test_nao_confunde_com_outra_coisa(texto):
    assert c._quer_o_audio(texto) is False


def test_oferta_pergunta_em_vez_de_mandar():
    """Se o toque mandasse o arquivo direto, precisaria de template com mídia,
    custaria por envio e chegaria pra quem não pediu."""
    texto = c._TEMPLATES_MSG[c.TOQUE_AUDIO]
    assert "áudio" in texto.lower()
    assert texto.rstrip().endswith("?")


def test_cada_corretora_tem_a_propria_voz():
    """A voz é de quem já conversa com o lead — Pessoa10, Pessoa11 e (na linha da Pessoa12)
    a Pessoa, que é quem atende ali."""
    assert set(ac.AUDIO_COMPARAR) == {1, 2, 3}
    assert len(set(ac.AUDIO_COMPARAR.values())) == 3


def test_arquivos_existem_e_sao_ogg():
    """`.oga` (OGG/Opus) é o que chega como MENSAGEM DE VOZ. Convertido, o
    WhatsApp trata como anexo — ícone de arquivo, sem player, quase ninguém
    abre."""
    for cid in ac.AUDIO_COMPARAR:
        caminho = ac.caminho_audio(cid)
        assert caminho is not None, f"áudio da corretora {cid} não está no disco"
        assert caminho.suffix == ".oga"
        assert caminho.read_bytes()[:4] == b"OggS", "não é OGG de verdade"


def test_corretora_sem_audio_nao_quebra():
    """Corretor (4) não tem — o carrossel dela é outra régua. Não pode estourar."""
    assert ac.caminho_audio(4) is None
    assert ac.caminho_audio(None) is None
    assert ac.tem_audio(4) is False


def test_audio_so_sai_no_toque_da_oferta(monkeypatch):
    """Um "sim" no meio da régua é resposta a outra coisa. Mandar o áudio ali
    seria mandar sem ninguém ter pedido."""
    enviados: list[str] = []
    monkeypatch.setattr(
        "app.api.cloud_corretora_a.enviar_cloud_midia",
        lambda *a, **k: enviados.append("foi") or {"ok": True},
    )
    # Desde 02/10 o áudio sai pela linha da DONA (aqui a Pessoa10): sem linha
    # registrada ele não sai — antes caía no 2591 da Pessoa12.
    from app.services import cloud_lines as cl

    monkeypatch.setattr(cl, "line_by_slug", lambda slug: object() if slug else None)

    class _Est:
        id, corretora_id, telefone = 1, 1, "+5500000000000"
        toque_atual = 0

    est = _Est()
    est.toque_atual = 1  # bem antes da oferta
    assert c._mandar_audio_da_corretora(est, "sim") is False
    assert not enviados

    est.toque_atual = c.TOQUE_AUDIO + 1  # acabou de receber a oferta
    assert c._mandar_audio_da_corretora(est, "sim, manda") is True
    assert enviados == ["foi"]
