"""Cache de categoria UTILITY — a trava que não pode furar.

Contexto (auditoria Sol, 29/07): o cache em disco guardava só os NOMES dos
templates, sem hora de parede. Como o cache em memória usa `time.monotonic()`
— que zera a cada boot — todo restart carimbava o horário ATUAL. Um cache de
MESES atrás parecia recém-validado por mais 24h.

Consequência: se a Meta reclassificasse um template pra MARKETING e a consulta
caísse no mesmo momento, o MARKETING passava pela trava. Marketing custa ~10x e
queima a lista — a linha da Pessoa12 já tomou ban uma vez.

Estes testes trancam o comportamento novo: cache vencido não ressuscita.
"""
from __future__ import annotations

import json
import time

import pytest

from app.services import cloud_lines as cl


@pytest.fixture()
def cache_limpo(tmp_path, monkeypatch):
    """Arquivo de cache isolado + estado em memória zerado."""
    arq = tmp_path / "linhas_utility.json"
    monkeypatch.setattr(cl, "_CAT_ARQUIVO", str(arq))
    cl._cat_cache.clear()
    cl._cat_ultimo_bom.clear()
    yield arq
    cl._cat_cache.clear()
    cl._cat_ultimo_bom.clear()


# ── 1. O bug: cache velho não pode voltar como novo ──────────────────────────

def test_cache_vencido_nao_ressuscita(cache_limpo, monkeypatch):
    """REGRESSÃO: cache de 30 dias atrás NÃO entra como 'bom recente'."""
    trinta_dias = 30 * 24 * 3600
    cache_limpo.write_text(
        json.dumps({
            "utility": {
                "corretor": {"nomes": ["acme_ola_a_01"], "em": time.time() - trinta_dias}
            }
        }),
        encoding="utf-8",
    )
    cl._carregar_do_disco()
    assert "corretor" not in cl._cat_ultimo_bom, (
        "cache de 30 dias entrou como válido — era o bug: a Meta pode ter "
        "reclassificado o template pra MARKETING nesse meio-tempo"
    )


def test_cache_recente_entra_com_a_idade_certa(cache_limpo):
    """Cache de 1h atrás entra, mas envelhecido de 1h — não zerado."""
    uma_hora = 3600
    cache_limpo.write_text(
        json.dumps({
            "utility": {
                "corretor": {"nomes": ["acme_ola_a_01"], "em": time.time() - uma_hora}
            }
        }),
        encoding="utf-8",
    )
    agora = time.monotonic()
    cl._carregar_do_disco()

    assert "corretor" in cl._cat_ultimo_bom
    quando, nomes = cl._cat_ultimo_bom["corretor"]
    assert nomes == {"acme_ola_a_01"}
    idade = agora - quando
    assert 3500 < idade < 3700, f"idade deveria ser ~1h, veio {idade:.0f}s"


def test_formato_antigo_e_ignorado(cache_limpo):
    """Lista pura (sem `em`) não dá pra datar → não entra. Otimismo aqui custa a conta."""
    cache_limpo.write_text(
        json.dumps({"utility": {"corretor": ["acme_ola_a_01", "acme_ola_b_01"]}}),
        encoding="utf-8",
    )
    cl._carregar_do_disco()
    assert "corretor" not in cl._cat_ultimo_bom


# ── 2. Ida e volta: o que salvo tem que voltar com a idade preservada ────────

def test_salva_e_recarrega_preservando_idade(cache_limpo):
    agora = time.monotonic()
    # entrada com 2h de idade
    cl._cat_ultimo_bom["corretor"] = (agora - 7200, {"acme_ola_a_01"})
    cl._salvar_no_disco()

    dados = json.loads(cache_limpo.read_text(encoding="utf-8"))
    entrada = dados["utility"]["corretor"]
    assert entrada["nomes"] == ["acme_ola_a_01"]
    assert "em" in entrada, "hora de parede não foi gravada — o bug volta"
    # gravado como ~2h atrás em hora de parede
    assert 7100 < (time.time() - entrada["em"]) < 7300

    cl._cat_ultimo_bom.clear()
    novo_agora = time.monotonic()
    cl._carregar_do_disco()
    quando, _ = cl._cat_ultimo_bom["corretor"]
    assert 7100 < (novo_agora - quando) < 7300, "a idade se perdeu no round-trip"


def test_arquivo_corrompido_nao_derruba(cache_limpo):
    cache_limpo.write_text("{isso não é json", encoding="utf-8")
    cl._carregar_do_disco()   # não levanta
    assert not cl._cat_ultimo_bom


# ── 3. A política de fundo continua valendo ─────────────────────────────────

def test_linha_sem_prova_nao_dispara(cache_limpo, monkeypatch):
    """Nunca confirmamos UTILITY pra essa linha + Meta fora do ar → set() vazio.
    Linha nova só ganha permissão com prova, não com otimismo."""
    monkeypatch.setattr(cl, "_buscar_utility_na_meta", lambda slug: None)
    assert cl.templates_utility("linha_nova") == set()
    assert cl.pode_disparar("linha_nova") is False
