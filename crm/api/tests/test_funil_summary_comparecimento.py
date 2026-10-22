"""
Testes do bug "comparecimento 233%" no quadro do funil da Acme.

Contexto do bug (diagnóstico 07/08/2026):
  - `qtde_agendados` em vendas_mensais nasceu SUBCONTADO (o robô do
    kommo-api/update.py descartava passagem de etapa feita por automação) e,
    depois de 14/05/2026, passou a ser recontado por FOTO do status atual —
    lead que agendou e virou Perdida/Remarketing SOME da conta, então o
    denominador DECAI a cada rodada do sync.
  - `qtde_visitas` (numerador) ficou congelado no valor histórico da planilha,
    porque o UPSERT do sync não escreve essa coluna.
  - Resultado: Realizada/Agendou passou de 100% e chegou a 233% no quadro que
    a Assistente entrega pra cliente.

Aqui provamos:
  (a) o cap defensivo — taxa de funil encaixado nunca sai > 100%;
  (b) a troca de fonte — Agendou/Realizada vêm da tabela `visitas`, que por
      construção não deixa o subconjunto passar do conjunto;
  (c) o guard de precedência do UPSERT do sync (não pisa em 'funil_manual').

100% mockado — nenhum acesso a DB.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

_FUNIL = (
    Path(__file__).resolve().parents[1]
    / "app" / "agents" / "assistente" / "skills" / "funil"
)


def _load_summary():
    """Importa summary.py em isolamento (não é pacote — não tem __init__.py)."""
    sys.path.insert(0, str(_FUNIL))
    spec = importlib.util.spec_from_file_location("funil_summary_mod", _FUNIL / "summary.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sm = _load_summary()


def _row(cid, nome, total, atendeu, agendou, realizada, vendas=0, vgv=0, fonte="kommo_realtime"):
    return (cid, nome, total, atendeu, agendou, realizada, vendas, vgv, fonte)


# ── (a) cap defensivo em _pct ────────────────────────────────────────────────

class TestPctCap:

    def test_reproduz_233_pct_sem_cap(self) -> None:
        """A conta crua que gerava o número absurdo: 7 realizadas / 3 agendadas."""
        assert round(7 / 3, 4) == 2.3333  # 233% — o que a cliente via

    def test_numerador_maior_que_denominador_nao_retorna_pct(self) -> None:
        """O mesmo par, agora pelo _pct: taxa encaixada não passa de 100%."""
        assert sm._pct(7, 3) is None

    def test_numerador_maior_marca_inconsistencia(self) -> None:
        entry = sm._pct_entry(7, 3)
        assert entry["pct"] is None
        assert entry["inconsistente"] is True

    def test_denominador_zero_continua_none_sem_flag(self) -> None:
        entry = sm._pct_entry(0, 0)
        assert entry["pct"] is None
        assert "inconsistente" not in entry

    def test_taxa_normal_intacta(self) -> None:
        assert sm._pct(3, 4) == 0.75
        assert sm._pct_entry(3, 4) == {"pct": 0.75}

    def test_cem_por_cento_e_valido(self) -> None:
        """Borda: todo mundo compareceu — 100% é legítimo, não é inconsistência."""
        entry = sm._pct_entry(4, 4)
        assert entry["pct"] == 1.0
        assert "inconsistente" not in entry


class TestPctCapNoPayload:

    def test_payload_nunca_expoe_pct_acima_de_100(self) -> None:
        """Cenário real da Acme: 3 agendados (subcontado) x 7 visitas (congelado)."""
        rows = [_row(1, "Pessoa5", 40, 20, 3, 7, fonte="kommo_realtime")]
        payload = sm._build_payload(rows, {}, "2026-05")

        realizada = payload["por_corretora"][0]["indicadores"]["Realizada"]
        assert realizada["valor"] == 7
        assert realizada["pct"] is None
        assert realizada["inconsistente"] is True

        # e o total geral também
        assert payload["total_geral"]["Realizada"]["pct"] is None

    def test_inconsistencias_listadas_com_contexto(self) -> None:
        rows = [_row(1, "Pessoa5", 40, 20, 3, 7)]
        payload = sm._build_payload(rows, {}, "2026-05")

        achados = [i for i in payload["inconsistencias"] if i["escopo"] == "Pessoa5"]
        assert achados == [{
            "escopo": "Pessoa5",
            "indicador": "Realizada",
            "base": "Agendou",
            "valor": 7,
            "valor_base": 3,
        }]

    def test_mes_saudavel_nao_gera_inconsistencia(self) -> None:
        rows = [_row(1, "Pessoa5", 40, 20, 10, 6)]
        payload = sm._build_payload(rows, {}, "2026-05")
        assert payload["inconsistencias"] == []
        assert payload["por_corretora"][0]["indicadores"]["Realizada"]["pct"] == 0.6


# ── (b) fonte: tabela `visitas` ──────────────────────────────────────────────

class TestFonteVisitas:

    def test_visitas_sobrepoe_vendas_mensais(self) -> None:
        """vendas_mensais diz 3/7 (quebrado); `visitas` diz 12 agendadas / 5 compareceu."""
        rows = [_row(1, "Pessoa5", 40, 20, 3, 7)]
        payload = sm._build_payload(rows, {1: (12, 5)}, "2026-05")

        ind = payload["por_corretora"][0]["indicadores"]
        assert ind["Agendou"]["valor"] == 12
        assert ind["Realizada"]["valor"] == 5
        assert ind["Realizada"]["pct"] == round(5 / 12, 4)
        assert payload["por_corretora"][0]["fonte_visitas"] == "visitas"
        assert payload["inconsistencias"] == []

    def test_indicadores_fora_do_funil_de_visita_nao_mudam(self) -> None:
        rows = [_row(1, "Pessoa5", 40, 20, 3, 7, vendas=2, vgv=800000)]
        payload = sm._build_payload(rows, {1: (12, 5)}, "2026-05")

        ind = payload["por_corretora"][0]["indicadores"]
        assert ind["Total"]["valor"] == 40
        assert ind["Atendeu"]["valor"] == 20
        assert ind["Vendas"]["valor"] == 2
        assert ind["VGV"]["valor"] == 800000.0

    def test_fallback_para_vendas_mensais_em_mes_historico(self) -> None:
        """Antes de 09/05/2026 a tabela `visitas` não existia — não zerar histórico."""
        rows = [_row(1, "Pessoa5", 40, 20, 9, 6, fonte="funil_manual")]
        payload = sm._build_payload(rows, {}, "2026-03")

        ind = payload["por_corretora"][0]["indicadores"]
        assert ind["Agendou"]["valor"] == 9
        assert ind["Realizada"]["valor"] == 6
        assert payload["por_corretora"][0]["fonte_visitas"] == "vendas_mensais"

    def test_total_geral_soma_a_fonte_certa(self) -> None:
        rows = [
            _row(1, "Pessoa5", 40, 20, 3, 7),
            _row(2, "Pessoa1", 30, 15, 2, 9),
        ]
        payload = sm._build_payload(rows, {1: (12, 5), 2: (8, 4)}, "2026-05")

        tot = payload["total_geral"]
        assert tot["Agendou"]["valor"] == 20
        assert tot["Realizada"]["valor"] == 9
        assert tot["Realizada"]["pct"] == round(9 / 20, 4)

    def test_mistura_de_fontes_e_reportada(self) -> None:
        rows = [
            _row(1, "Pessoa5", 40, 20, 3, 7),
            _row(2, "Pessoa1", 30, 15, 2, 9),
        ]
        payload = sm._build_payload(rows, {1: (12, 5)}, "2026-05")
        assert payload["fonte_agendados_visitas"] == ["vendas_mensais", "visitas"]

    def test_query_de_visitas_usa_os_filtros_canonicos(self) -> None:
        """Mesmos FILTERs do painel (adotados em 21/05) — sem isso o cap é cosmético."""
        sql = " ".join(sm.SQL_VISITAS_MES.split())
        assert "COUNT(*) FILTER (WHERE status <> 'cancelada') AS agendados" in sql
        assert "COUNT(*) FILTER (WHERE status = 'compareceu') AS realizadas" in sql
        assert "FROM visitas" in sql


class TestFormatoDeSaida:

    def test_chaves_do_formato_antigo_preservadas(self) -> None:
        rows = [_row(1, "Pessoa5", 40, 20, 10, 6, vendas=1, vgv=350000)]
        payload = sm._build_payload(rows, {1: (10, 6)}, "2026-05")

        for k in ("status", "ano_mes", "label", "fonte", "fontes_origem",
                  "total_geral", "por_corretora"):
            assert k in payload
        assert payload["status"] == "ok"
        assert payload["ano_mes"] == "2026-05"
        assert payload["label"] == "Maio 2026"
        assert payload["fontes_origem"] == ["kommo_realtime"]

        corr = payload["por_corretora"][0]
        for k in ("corretora_id", "nome", "fonte", "indicadores"):
            assert k in corr
        assert set(corr["indicadores"]) == set(sm.INDICADORES)
        assert set(corr["indicadores"]["Total"]) == {"valor"}
        assert set(corr["indicadores"]["Realizada"]) == {"valor", "pct"}

    def test_month_bounds_meia_noite_sp_e_virada_de_ano(self) -> None:
        ini, fim = sm._month_bounds(2026, 5)
        assert (ini.year, ini.month, ini.day, ini.hour) == (2026, 5, 1, 0)
        assert (fim.year, fim.month, fim.day) == (2026, 6, 1)
        assert str(ini.tzinfo) == "America/Sao_Paulo"

        ini, fim = sm._month_bounds(2026, 12)
        assert (fim.year, fim.month, fim.day) == (2027, 1, 1)


# ── (c) guard de precedência no UPSERT do sync ───────────────────────────────

class TestUpsertPrecedencia:

    def test_upsert_nao_sobrescreve_funil_manual(self) -> None:
        """Espelha o WHERE de fn_reconcilia_vendas_mensais (migration 0024)."""
        mod = pytest.importorskip(
            "scripts.sync_vendas_mensais_kommo",
            reason="sync depende de app.config/httpx",
        )
        sql = " ".join(mod.SQL_UPSERT_VENDAS_MENSAIS.split())
        assert "WHERE vendas_mensais.fonte IS DISTINCT FROM 'funil_manual'" in sql
        assert sql.index("ON CONFLICT") < sql.index("WHERE vendas_mensais.fonte")
