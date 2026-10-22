"""
AgenteHunter — Assistente-da-agencia vendendo sites do Prospector via WhatsApp.

Loop conversacional com tool-calling JSON, mesmo padrão da Assistente mas sem
skills externas (subprocess). Tools mexem só em hunter_repo + Telegram notify.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import structlog

from app.config import get_llm_provider, settings
from app.services import hunter_repo

log = structlog.get_logger()

HUNTER_DIR = Path(__file__).parent
SOUL_PATH = HUNTER_DIR / "SOUL.md"
SP_TZ = ZoneInfo("America/Sao_Paulo")

MAX_TOOL_ITERATIONS = 4

RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "text": {
            "type": "string",
            "description": "Sua resposta natural pro prospect (PT-BR, tom Assistente agencia).",
        },
        "tool_call": {
            "type": ["object", "null"],
            "description": "null se não precisar usar tool; senão {name, args}.",
            "properties": {
                "name": {
                    "type": "string",
                    "enum": [
                        "marcar_qualificado",
                        "marcar_opt_out",
                        "marcar_perdido",
                        "avisar_dona",
                    ],
                },
                "args": {"type": "object"},
            },
            "required": ["name", "args"],
        },
    },
    "required": ["text", "tool_call"],
}


def _load_soul() -> str:
    if not SOUL_PATH.exists():
        raise FileNotFoundError(f"SOUL.md não encontrado em {SOUL_PATH}")
    return SOUL_PATH.read_text(encoding="utf-8")


# ── Tools ────────────────────────────────────────────────────────────────────


def _telegram_dona(mensagem: str) -> dict[str, Any]:
    """Manda pra Dona via um bot do Telegram (endpoint de notificação).

    Lê config direto de /opt/telegram-claude/.env — zero acoplamento com
    o copiloto-acme (Hunter é agencia, não Acme).
    """
    env_path = Path("/opt/telegram-claude/.env")
    token = ""
    host = "127.0.0.1"
    port = "8099"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line.startswith("NOTIFY_API_TOKEN="):
                token = line.split("=", 1)[1].strip()
            elif line.startswith("NOTIFY_API_PORT="):
                port = line.split("=", 1)[1].strip()
    task_id = f"hunter-{datetime.now(SP_TZ).strftime('%Y%m%d-%H%M%S')}"
    try:
        r = httpx.post(
            f"http://{host}:{port}/api/notify",
            json={
                "task_id": task_id,
                "text": mensagem,
                "descricao": "Hunter agencia · evento de prospect",
            },
            headers={"Authorization": f"Bearer {token}"} if token else {},
            timeout=10,
        )
        return {"status": "ok" if r.status_code < 300 else "fail",
                "code": r.status_code}
    except Exception as e:
        log.error("hunter_notify_dona_failed", error=str(e))
        return {"status": "fail", "error": str(e)}


class HunterAgent:
    def __init__(
        self,
        phone: str,
        prospect: Any | None = None,
        history: list[dict[str, str]] | None = None,
        provider: Any | None = None,
    ) -> None:
        self.phone = phone
        self.prospect = prospect
        self.history = history or []
        self.soul = _load_soul()
        self._provider = provider

    # ── prompt building ─────────────────────────────────────────────────────

    def _prospect_block(self) -> str:
        if not self.prospect:
            return "Prospect sem registro prévio (lead novo)."
        p = self.prospect
        lines = [
            "Contexto do prospect (do CRM Hunter):",
            f"- Nome: {p.nome or '(não capturado)'}",
            f"- Segmento: {p.segmento or '(não informado)'}",
            f"- Cidade: {p.cidade or '(não informado)'}",
            f"- Telefone: {p.phone}",
        ]
        if p.preview_url:
            lines.append(f"- Site exemplo que mandamos: {p.preview_url}")
        lines.append(f"- Estado atual da conversa: {p.state}")
        if p.qualified_data:
            lines.append(f"- Dados já coletados: {json.dumps(p.qualified_data, ensure_ascii=False)}")
        return "\n".join(lines)

    def _history_block(self) -> str:
        if not self.history:
            return ""
        lines = ["Mensagens anteriores da conversa:"]
        for h in self.history[-20:]:
            who = "você (Assistente)" if h["role"] == "assistente" else "prospect"
            lines.append(f"- {who}: {h['text']}")
        return "\n".join(lines)

    def _tools_block(self) -> str:
        return (
            "Tools disponíveis (chame via campo tool_call do JSON):\n"
            "- marcar_qualificado(dados: dict) — quando lead aceita fechar OU já passou "
            "todos os critérios pra Dona fechar. `dados` deve ter: gostou, decisor, "
            "ajustes_pedidos, valor_aceito, nome_completo, email, cnpj_ou_cpf (o que tiver).\n"
            "- marcar_opt_out(motivo: str) — lead pediu 'para', 'stop', 'não tenho "
            "interesse', 'remove'. Sempre que isso aparecer, chama IMEDIATO.\n"
            "- marcar_perdido(motivo: str) — lead disse não, sem dinheiro, é cliente "
            "Wix, etc. (motivos válidos: sem_interesse, ja_tem_site, sem_orcamento, outro)\n"
            "- avisar_dona(mensagem: str) — chama Dona via Telegram quando: lead "
            "qualificado pediu pra falar com humano, perguntou algo técnico que você "
            "não sabe, ou pediu desconto.\n\n"
            "Regras:\n"
            "- Pra marcar_qualificado, antes texte uma confirmação tipo "
            "'Show, fechei aqui o pedido. Pra eu mandar pra Dona gerar o contrato, "
            "me confirma seu nome completo e e-mail.' e SÓ chama a tool quando tiver os dados.\n"
            "- Pra marcar_opt_out, NÃO precisa confirmar — chama direto + manda "
            "'Sem problema, te tiro da lista 👋'.\n"
            "- avisar_dona inclui RESUMO do contexto (nome do negócio, pergunta).\n"
        )

    def _build_system_prompt(self) -> str:
        agora = datetime.now(SP_TZ).strftime("%A, %d/%m/%Y %H:%M")
        parts = [
            self.soul,
            "---",
            f"Agora: {agora} (horário Cidade/SP).",
            self._prospect_block(),
            "---",
            self._tools_block(),
        ]
        hist = self._history_block()
        if hist:
            parts += ["---", hist]
        return "\n\n".join(parts)

    # ── tool dispatch ───────────────────────────────────────────────────────

    def _execute_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name == "marcar_qualificado":
            ok = hunter_repo.mark_qualified(self.phone, args.get("dados") or args)
            _telegram_dona(
                f"🎯 *Hunter qualificado*: {self.prospect.nome if self.prospect else '?'} "
                f"({self.phone[-4:]})\n"
                f"Dados: {json.dumps(args.get('dados') or args, ensure_ascii=False)}"
            )
            return {"status": "ok" if ok else "fail"}
        if name == "marcar_opt_out":
            ok = hunter_repo.mark_opted_out(self.phone, args.get("motivo", "opt_out"))
            return {"status": "ok" if ok else "fail"}
        if name == "marcar_perdido":
            ok = hunter_repo.mark_perdido(self.phone, args.get("motivo", "sem_interesse"))
            return {"status": "ok" if ok else "fail"}
        if name == "avisar_dona":
            return _telegram_dona(args.get("mensagem", "(sem texto)"))
        return {"status": "error", "message": f"tool desconhecida: {name}"}

    # ── run loop ────────────────────────────────────────────────────────────

    def _provider_complete(self, system: str, user: str) -> str:
        provider = self._provider or get_llm_provider()
        json_str, _ = provider.complete(system=system, user=user, json_schema=RESPONSE_SCHEMA)
        return json_str

    def run(self, message: str) -> dict[str, Any]:
        system = self._build_system_prompt()
        user_buffer = message
        tool_calls_log: list[dict[str, Any]] = []
        last_text = ""

        for iteration in range(MAX_TOOL_ITERATIONS):
            try:
                json_str = self._provider_complete(system, user_buffer)
            except Exception as e:
                log.error("hunter_provider_error", error=str(e))
                return {
                    "response_text": "Pera, tive um problema aqui. Já volto.",
                    "tool_calls": tool_calls_log,
                }
            try:
                parsed = json.loads(json_str)
            except json.JSONDecodeError as e:
                log.error("hunter_invalid_json", error=str(e), raw=json_str[:200])
                return {
                    "response_text": "Tive um problema interpretando aqui. Manda de novo?",
                    "tool_calls": tool_calls_log,
                }
            text = parsed.get("text", "")
            last_text = text or last_text
            tool_call = parsed.get("tool_call")

            if not tool_call:
                return {"response_text": text, "tool_calls": tool_calls_log}

            name = tool_call.get("name")
            args = tool_call.get("args") or {}
            log.info("hunter_tool_call", name=name, iteration=iteration,
                     phone_last4=self.phone[-4:])
            result = self._execute_tool(name, args)
            tool_calls_log.append({"name": name, "args": args, "result": result})

            user_buffer = (
                f"{message}\n\n"
                f"[resultado da tool {name}]: {json.dumps(result, ensure_ascii=False)}\n\n"
                f"Conclui a conversa naturalmente. Não chama mais tools desnecessárias."
            )

        return {
            "response_text": last_text or "Manda de novo?",
            "tool_calls": tool_calls_log,
        }
