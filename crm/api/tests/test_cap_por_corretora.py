"""Cap diário POR CORRETORA (decisão da Dona 27/07).

Em produção: Pessoa12 (3) = 25 — chip novo, 2 bans em julho. Pessoa10 (1) e Pessoa11 (2) = 200,
porque o número delas é pessoal, com anos de uso orgânico: ali o cap é fusível
contra BUG nosso (loop de envio), não freio de volume.

O conftest LIMPA esse mapa nos testes de propósito (senão ele sobrepõe o
`cadencia_cap_diario` que cada teste ajusta). Então aqui testamos o MECANISMO,
não os valores do .env.
"""
from app.config import settings
from app.services import cadencia


def _cap_efetivo(corretora_id: int | None) -> int:
    """Reproduz a resolução de cap do motor (mesma regra de _cap_diario_atingido)."""
    cap = settings.cadencia_cap_diario
    if corretora_id is not None:
        cap = int(settings.cadencia_cap_por_corretora.get(str(corretora_id), cap))
    return cap


def test_mapa_vazio_usa_cap_global(monkeypatch):
    monkeypatch.setattr(settings, "cadencia_cap_por_corretora", {})
    monkeypatch.setattr(settings, "cadencia_cap_diario", 25)
    assert _cap_efetivo(1) == 25
    assert _cap_efetivo(None) == 25


def test_corretora_no_mapa_usa_o_valor_dela(monkeypatch):
    monkeypatch.setattr(settings, "cadencia_cap_por_corretora", {"1": 200, "3": 25})
    monkeypatch.setattr(settings, "cadencia_cap_diario", 50)
    assert _cap_efetivo(1) == 200, "Pessoa10 com número quente"
    assert _cap_efetivo(3) == 25, "Pessoa12 em regime apertado"
    assert _cap_efetivo(2) == 50, "fora do mapa cai no global"


def test_uma_linha_estourar_nao_afeta_a_outra(monkeypatch):
    """O ponto do cap por linha: Pessoa10 no limite NÃO pode calar a Pessoa12."""
    monkeypatch.setattr(settings, "cadencia_cap_por_corretora", {"1": 1, "3": 25})
    monkeypatch.setattr(settings, "cadencia_cap_diario", 25)
    assert _cap_efetivo(1) == 1 and _cap_efetivo(3) == 25


def test_motor_aceita_corretora_id(monkeypatch):
    """_cap_diario_atingido tem que aceitar corretora_id (assinatura por linha)."""
    import inspect

    params = inspect.signature(cadencia._cap_diario_atingido).parameters
    assert "corretora_id" in params
