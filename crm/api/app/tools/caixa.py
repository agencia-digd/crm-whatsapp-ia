"""
Tool Agno: simulação de financiamento Caixa (SIOPI) para a Corretor/Assistente.

A simulação real roda NO MAC MINI (IP residencial + Playwright headed; o VPS é
bloqueado por WAF da Caixa). Esta tool chama o Mac via SSH e devolve um resumo
em PT-BR pronto pro agente relatar ao lead.

Script no Mac: /Users/assistente-mac/acme-caixa/simulador_caixa.py
Fonte/ref: scripts/caixa/ (simulador_caixa.py + run_simulacao.py + README).

ATENÇÃO: a simulação leva ~70-90s (navegador headed). Idealmente chamada de forma
assíncrona no fluxo de conversa (avisar o lead "deixa eu simular rapidinho").
"""
from __future__ import annotations

import json
import shlex
import subprocess

import structlog
from agno.tools import Toolkit

log = structlog.get_logger()

_MAC = "contato@example.com"
_MAC_PY = "/Users/assistente-mac/corretor/venv/bin/python"
_MAC_SCRIPT = "/Users/assistente-mac/acme-caixa/simulador_caixa.py"
_TIMEOUT = 180


def _run_mac(params: dict) -> dict:
    payload = json.dumps(params, ensure_ascii=False)
    remote = f"{_MAC_PY} -u {_MAC_SCRIPT} {shlex.quote(payload)}"
    cmd = ["ssh", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=no", _MAC, remote]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {"ok": False, "erro": "timeout na simulação"}
    for line in reversed([l for l in out.stdout.strip().splitlines() if l.strip()]):
        try:
            return json.loads(line)
        except Exception:
            continue
    return {"ok": False, "erro": "sem retorno válido", "stderr": out.stderr[-200:]}


class CaixaSimuladorTools(Toolkit):
    """Tools de simulação de financiamento habitacional Caixa."""

    def __init__(self, **kwargs) -> None:
        super().__init__(name="caixa_simulador", tools=[self.simular_financiamento], **kwargs)

    def simular_financiamento(
        self,
        renda: str,
        data_nascimento: str,
        valor_imovel: str = "",
        cidade: str = "",
        uf: str = "SP",
        possui_fgts: bool = True,
    ) -> str:
        """
        Calcula QUANTO O LEAD PODE COMPRAR, no simulador oficial da Caixa.

        Use quando o lead topar simular e você já tiver a renda e a data de
        nascimento — que é tudo o que precisa. NÃO peça CPF: a gente usa a
        "Calculadora poder de compra", que não exige.

        Args:
            renda: renda bruta mensal somada da família, ex "8200,00"
            data_nascimento: do participante de maior idade, dd/mm/aaaa
            valor_imovel / cidade / uf / possui_fgts: ACEITOS mas IGNORADOS —
                mantidos só pra não quebrar chamadas antigas. O simulador novo
                não usa nenhum deles nesta modalidade.

        Returns:
            Resumo em PT-BR pronto pra relatar ao lead, ou erro amigável.
        """
        # 01/08: a Caixa desligou o simulador antigo (siopiweb). A "Simulação
        # Completa" do app novo exige CPF + código SMS no celular do lead —
        # inautomatizável, e pedir isso no WhatsApp parece golpe. A calculadora
        # de poder de compra dá o número com renda + nascimento.
        params = {"renda": renda, "data_nascimento": data_nascimento}
        log.info("caixa_simular_inicio", **params)
        r = {"ok": False, "erro": "nao iniciou"}
        for tentativa in range(3):
            r = _run_mac(params)
            if r.get("ok") and r.get("valor_imovel"):
                break
            log.warning("caixa_simular_retry", tentativa=tentativa + 1, erro=r.get("erro"))
        if not r.get("ok"):
            log.warning("caixa_simular_falha", erro=r.get("erro"))
            return ("Não consegui concluir a simulação agora "
                    f"(motivo técnico: {r.get('erro')}). Avisa a Dona.")

        linhas = [
            "✅ Simulação Caixa (poder de compra):",
            f"• Valor de imóvel que cabe: {r.get('valor_imovel') or '—'}",
            f"• Entrada mínima: {r.get('entrada_minima') or '—'}",
            f"• Financiamento máximo: {r.get('financiamento_maximo') or '—'}",
            f"• Parcela estimada: {r.get('prestacao') or '—'}",
            "",
            "(FGTS e subsídio do MCMV podem entrar na entrada e melhorar isso.)",
        ]
        log.info("caixa_simular_ok", valor_imovel=r.get("valor_imovel"),
                 financiamento=r.get("financiamento_maximo"))
        return "\n".join(linhas)
