"""Cadência híbrida (P2) — state machine dos 8 toques + remarketing.

O CORAÇÃO do valor novo: hoje não existe cadência automática. Este serviço decide,
por lead na cadência, se há toque DEVIDO hoje e QUAL — e **enfileira** (NUNCA envia
direto). Mensagens vão pro disparo em DRY-RUN reusando o motor da Fase 7
(`app.api.cloud_corretora_a`) atrás das travas existentes (`settings.dry_run`). Tarefas
de ligação geram um registro de tarefa (evento tipo='tarefa_ligacao').

Cronograma híbrido (8 toques em 10 dias · mix ~80% utility / 20% marketing):

    toque  dia   tipo            canal        botão
    0      D0    boas-vindas     whatsapp     não   (utility)
    1      D1    tarefa-ligação  ligacao      —     (🔔 corretora liga)
    2      D2    mensagem        whatsapp     sim   (utility)
    3      D4    mensagem        whatsapp     não   (utility)
    4      D5    tarefa-ligação  ligacao      —     (🔔 corretora liga)
    5      D7    mensagem        whatsapp     sim   (utility)
    6      D9    tarefa-ligação  ligacao      —     (🔔 última ligação)
    7      D10   mensagem        whatsapp     não   (marketing — NÃO SAI, ver abaixo)
    →      remarketing: 1 msg / 30 dias (utility), enquanto status='remarketing'.

⚠️ TOQUE 7 (categoria 'marketing') NÃO É DESPACHADO. A linha da Pessoa12 só manda
template UTILITY (regra Dona) e o pool do `enviar_template_corretor` é 100% UTILITY —
antes o rótulo 'marketing' era morto e o texto saía por template UTILITY assim
mesmo. Agora o tick tem gate: toque marketing é registrado como pulado e o lead
avança direto pro remarketing. O toque CONTINUA na lista `TOQUES` de propósito —
27 leads em produção estão com toque_atual=7 e encurtar a tupla daria IndexError
em `_avancar`.

Regras:
  - toda MENSAGEM termina com PERGUNTA ("?") — coaching pede próxima ação.
  - o D0 é SEMPRE o reencontro (`_TEMPLATES_MSG[0]`): quem entra na cadência
    entrou pelo Follow-up (move manual da Pessoa12 / auto-enroll). A boas-vindas de
    cadastro é AUTOMAÇÃO SEPARADA (lei Dona 10/07) — vive no intake
    (`app/services/lead_intake.py`): 1 msg quando o lead cai em "Novo" e
    AGUARDA; não é toque daqui.
  - botão alterna (com/sem) a cada mensagem enviada.
  - anti-ban: opt-out, cooldown (remarketing 30d), cap diário, cooldown mínimo
    entre enfileiramentos reais.
  - idempotente: `cadencia_eventos` tem UNIQUE(tenant, cadencia, toque). Rodar
    `tick` 2x no mesmo dia NÃO duplica (o toque já registrado é pulado).

O `tick(agora)` despacha pelo motor da Fase 7 (`enviar_template_corretor`). Em produção
`DRY_RUN=false` e o timer roda a cada 3 min: **cada tick manda template REAL**.
`settings.dry_run=True` (dev/teste) é o que segura o envio.

Durabilidade: o tick commita LEAD A LEAD (logo após `_avancar`). Falha de envio
não avança o toque, registra `cadencia_eventos(tipo='envio_falhou', toque=NULL)`
e o loop segue; N falhas SEGUIDAS abortam a varredura (circuit breaker).

TODO(twenty): a tarefa de ligação hoje vira um `cadencia_eventos(tipo='tarefa_ligacao')`.
Quando o Twenty/Postgres de tarefas estiver plugado, espelhar esse evento como card de
tarefa lá (owner = corretora_id). Não bloqueia o motor.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import func
from sqlalchemy import text as _sql_text
from sqlalchemy.orm import Session

from app.services.trava_tick import NS_CADENCIA, OCUPADO, soltar, travar

from app.config import settings
from app.services import config_operacional
from app.db.models import CadenciaEstado, CadenciaEvento, Lead
from app.services import cloud_lines, reengajamento_guard

log = structlog.get_logger()

DEFAULT_TENANT_ID = 1

# ── Definição dos toques ─────────────────────────────────────────────────────
# offset_dias = dias após o D0 (entrada na cadência).

TIPO_MENSAGEM = "mensagem"
TIPO_TAREFA_LIGACAO = "tarefa_ligacao"

CANAL_WHATSAPP = "whatsapp"
CANAL_LIGACAO = "ligacao"


@dataclass(frozen=True)
class Toque:
    indice: int
    offset_dias: int
    tipo: str          # TIPO_MENSAGEM | TIPO_TAREFA_LIGACAO
    canal: str
    categoria: str     # "utility" | "marketing" (mix ~80/20)
    # Follow-up é SEM botão por decisão da Dona (28/07) — a exceção é o toque
    # que OFERECE o áudio (D3): lá o "sim" precisa ser um clique, não um texto
    # que a gente tenta adivinhar. Quem quiser saber se um toque tem botão
    # pergunta pro toque; NUNCA compara índice.
    com_botao: bool = False
    # Verbo do CTA — vira o {{3}} do template com botão ("Para <cta>, é só
    # tocar no botão abaixo."). Só faz sentido com com_botao=True.
    cta: str | None = None


# 8 toques híbridos em 10 dias. 5 mensagens (4 utility + 1 marketing ~= 80/20) + 3 ligações.
TOQUES: tuple[Toque, ...] = (
    Toque(0, 0, TIPO_MENSAGEM, CANAL_WHATSAPP, "utility"),        # D0 boas-vindas
    Toque(1, 1, TIPO_TAREFA_LIGACAO, CANAL_LIGACAO, "utility"),   # D1 🔔
    Toque(2, 2, TIPO_MENSAGEM, CANAL_WHATSAPP, "utility"),        # D2
    # D3 — OFERTA DE ÁUDIO (régua V2, 04/08/2026). Não manda o arquivo: pergunta
    # se pode mandar. O "sim" abre a janela de 24h (aí o áudio sai sem template
    # e sem custo), é micro-compromisso, e garante que só recebe quem quis.
    #
    # COM BOTÃO desde 05/08 (Dona): o "sim" vira CLIQUE. Antes a gente lia a
    # resposta em texto — e qualquer texto parava a cadência, então quem só
    # queria o áudio saía da régua sem nunca ter conversado. Clique não é
    # resposta: manda o áudio e o lead SEGUE pro D4.
    Toque(3, 3, TIPO_MENSAGEM, CANAL_WHATSAPP, "utility",         # D3 🎙
          com_botao=True, cta="receber o áudio"),
    Toque(4, 4, TIPO_MENSAGEM, CANAL_WHATSAPP, "utility"),        # D4
    Toque(5, 5, TIPO_TAREFA_LIGACAO, CANAL_LIGACAO, "utility"),   # D5 🔔
    Toque(6, 7, TIPO_MENSAGEM, CANAL_WHATSAPP, "utility"),        # D7
    Toque(7, 9, TIPO_TAREFA_LIGACAO, CANAL_LIGACAO, "utility"),   # D9 🔔 última ligação
    Toque(8, 10, TIPO_MENSAGEM, CANAL_WHATSAPP, "marketing"),     # D10 (o único marketing — encerramento)
)

# Templates das mensagens (toque → texto). TODA mensagem TERMINA COM "?".
# Copy no tom Assistente/gentil; a corretora aprova antes do envio real.
#
# O toque 0 (D0) é SEMPRE o reencontro — a boas-vindas de cadastro é automação
# separada do intake (lei Dona 10/07, app/services/lead_intake.py), não passa
# pela cadência.
# NOTA: estes textos vão no {{2}} dos templates da Pessoa12, cujo {{1}} JÁ traz a
# saudação ("Olá {nome}, tudo bem?" / "Oi {nome}! 😊"). Por isso NÃO recumprimentam
# aqui — evita a saudação dobrada ("Olá tudo bem, tudo bem?\n\nOi, tudo bem?...").
# A identidade da Pessoa (1ª pessoa, calorosa) fica no corpo, sem "Oi/Olá" inicial.
# ⚠️ `{assinatura}` é substituída pelo nome de quem MANDA (achado do Grok,
# 29/07). O envio já roteia pela linha da DONA do lead — então um lead da Pessoa10
# recebia, no WhatsApp da Pessoa10, um texto assinado "Pessoa". Quem resolve o nome
# é `assinatura_da_corretora()`, mesma fonte do lead_intake.
_TEMPLATES_MSG: dict[int, str] = {
    0: "Vi aqui que a sua busca de imóvel ficou parada e não quero te deixar sem retorno, sou a {assinatura}, da Acme Imóveis 🏠\n\nMe conta: ainda tá procurando ou já encontrou?",
    2: "Aqui é a {assinatura} de novo! Tentei te ligar mas acho que te peguei num horário ruim.\n\nMe fala um horário bom pra você, ou prefere que a gente se fale por aqui mesmo?",
    # TODO(fase-2): no toque 3, trocar por áudio da corretora quando a janela de 24h estiver aberta.
    3: "Uma coisa que quase ninguém olha na hora de escolher, e faz toda diferença: gravei um áudio rapidinho explicando como comparar dois imóveis sem errar.\n\nQuer que eu te mande?",
    # D4 e D7 tiveram o FECHO trocado (Dona 05/08): o D4 terminava em "Quer que
    # eu te mande por aqui?", quase a mesma frase do D3 ("Quer que eu te
    # mande?") — quem recebe a régua inteira lia duas vezes o mesmo pedido. Cada
    # toque agora fecha com uma pergunta diferente das outras (há teste travando
    # isso: `test_fechos_das_copies_nao_se_repetem`).
    4: "Chegaram opções novas essa semana e separei as que mais combinam com o que você procurava 👀\n\nSe eu te mandar 3, você dá uma olhada?",
    6: "Te mandei umas mensagens e tentei te ligar, e não quero sumir nem insistir demais 😅\n\nA busca do imóvel ainda faz parte dos seus planos?",
    # Encerramento com TRÊS saídas (régua V2, aprovado pela Dona em 04/08/2026).
    # Antes era uma despedida sem pergunta — quem estava só num momento ruim
    # sumia, e sumir era indistinguível de não ter interesse. A última linha
    # existe pra tirar o peso de escolher a 3: quem se sente mal dizendo não,
    # some em vez de responder.
    8: "Não quero ser chata 💛 então prefiro te perguntar direto: quer que eu continue te avisando quando aparecer algo com a sua cara, ou prefere que eu pare por aqui?\n\nMe responde só com o número:\n\n1 — pode continuar\n2 — pausa por 30 dias\n3 — pode encerrar\n\nQualquer uma tá ótima pra mim — só não quero te incomodar sem querer, tudo bem?",
}

# Remarketing (após os 8 toques): 1 msg a cada 30 dias. Também vai no {{2}} —
# sem saudação inicial (o {{1}} do template já cumprimenta).
_TEMPLATE_REMARKETING = (
    "{assinatura}, da Acme, por aqui de novo 🏠 Apareceram imóveis novos que lembram o que você buscava, e pensei em você. Quer que eu te mande?"
)


def assinatura_da_corretora(corretora_id: int | None) -> str:
    """Nome que ASSINA a mensagem — o de quem realmente manda.

    Reusa o mapa do `lead_intake` (Pessoa12 = "Pessoa", persona da casa; Pessoa10 e Pessoa11
    com nome real) pra não existirem duas listas divergindo. Sem corretora
    resolvida cai no padrão, que é a linha da Pessoa12.
    """
    try:
        from app.services.lead_intake import (
            _BOAS_VINDAS_NOME,
            _BOAS_VINDAS_NOME_PADRAO,
        )

        return _BOAS_VINDAS_NOME.get(corretora_id or 0, _BOAS_VINDAS_NOME_PADRAO)
    except Exception:  # noqa: BLE001 — nome nunca pode derrubar o envio
        return "Pessoa"


def aplicar_assinatura(texto: str, corretora_id: int | None) -> str:
    """Troca `{assinatura}` pelo nome de quem manda. Texto sem o marcador passa
    intacto (copies antigas em `cadencia_copies` continuam funcionando)."""
    if "{assinatura}" not in (texto or ""):
        return texto
    return texto.replace("{assinatura}", assinatura_da_corretora(corretora_id))

# Chave da copy de remarketing na tabela de overrides (`cadencia_copies`):
# os toques de mensagem usam o próprio índice (0, 2, 3, 5, 7); remarketing = -1.
TOQUE_REMARKETING = -1


def copy_default(toque: int) -> str | None:
    """Copy DEFAULT (hardcoded) de um toque de mensagem, ou do remarketing (-1).
    None se o toque não é de mensagem (ligações não têm copy)."""
    if toque == TOQUE_REMARKETING:
        return _TEMPLATE_REMARKETING
    return _TEMPLATES_MSG.get(toque)


def copy_efetiva(
    session: Session | None,
    toque: int,
    *,
    tenant_id: int = DEFAULT_TENANT_ID,
) -> str:
    """Copy EFETIVA de um toque: override em `cadencia_copies` quando existe,
    senão o default do código. Best-effort SEMPRE — tabela ausente (árvore sem
    a migration 0057), session None ou erro de DB degradam pro default. NUNCA
    levanta: a origem do TEXTO muda, o comportamento de envio não."""
    default = copy_default(toque) or ""
    if session is None:
        return default
    try:
        from app.db.models import CadenciaCopy

        # SAVEPOINT: tabela ausente não pode poisonar a transação do tick
        # (em Postgres um SELECT que falha aborta a transação inteira).
        with session.begin_nested():
            row = (
                session.query(CadenciaCopy)
                .filter(CadenciaCopy.tenant_id == tenant_id, CadenciaCopy.toque == toque)
                .first()
            )
        if row is not None and (row.texto or "").strip():
            return row.texto
    except Exception as e:  # noqa: BLE001 — override indisponível não para o motor
        log.warning("cadencia_copy_override_falha", toque=toque, erro=str(e)[:120])
    return default


# Opt-out: implementação canônica em app/services/opt_out.py. A versão antiga
# aqui era `"para" in texto` (substring) e matou 3 leads em produção com falso
# positivo ("Estamos ansiosos PARA atendê-lo" → opt_out + Venda perdida).
# Reexportado com o nome antigo pra não quebrar os imports/testes existentes.
from app.services.opt_out import (  # noqa: E402  (reexport proposital)
    classificar_desinteresse,
    is_opt_out,
    motivo_opt_out,
)
from app.services import opt_out as opt_out_svc  # noqa: E402


# ── Sender injetável (default = motor Fase 7, em DRY-RUN atrás de settings.dry_run) ──

def ordem_da_mensagem(indice_toque: int) -> int:
    """Posição do toque entre os toques de MENSAGEM (0ª, 1ª, 2ª...).

    Serve pra girar a rotação de template. Usar o `indice_toque` cru não funciona:
    os toques de mensagem são 0, 2, 3, 5 e 7 — e `% 5` disso dá {0,2,3,0,2}, ou
    seja, duas posições da rotação NUNCA são visitadas e o lead pode receber o
    mesmo rodapé a sequência inteira. Foi o que aconteceu num teste com 3 leads:
    dois variavam, o terceiro repetia sempre.

    A ordem ordinal (0,1,2,3,4) percorre a rotação inteira.
    """
    msgs = sorted(_TEMPLATES_MSG)
    try:
        return msgs.index(indice_toque)
    except ValueError:
        return indice_toque


def _default_sender(
    telefone: str,
    texto: str,
    *,
    com_botao: bool,
    linha: Any = None,
    corretora_id: int | None = None,
    variacao: int = 0,
    permitir_marketing: bool = False,
    cap_diario_da_linha: int = 0,
    cta: str | None = None,
    origem: str | None = None,
) -> dict[str, Any]:
    """Enfileira a mensagem no motor de disparo da Fase 7, PELA LINHA DA DONA.

    Multi-linha (27/07): quando o tick passa `linha` (CloudLine resolvida pelo
    dono do lead), o envio sai por ELA — phone_id, token, `instance` e
    `corretora_id` do thread. Sem `linha` (chamada legada / sender manual) cai no
    caminho histórico da Pessoa12, byte a byte.

    Reusa o motor do cloud_corretora_a, que já está atrás da trava
    DRY-RUN (`_dry_run_ativo()` == settings.dry_run). Em DRY-RUN loga e devolve
    {"dry_run": True, "sent": False} — NÃO manda nada. Import tardio pra não
    puxar o router em teste.

    Dona 28/07: follow-up SEM botão (pool _ola_). Prefere templates novos
    `*_ola_fu_*` (sem "Fico à disposição"); se ainda PENDING, cai no ola
    clássico. NÃO usa reeng_b (tem botão).

    EXCEÇÃO (Dona 05/08): o toque que OFERECE o áudio sai COM botão — aí vale o
    pool reeng_b, que já está aprovado ("Saiba mais" + "Parar mensagens"), com o
    `cta` do toque virando o {{3}} ("Para receber o áudio, é só tocar no botão
    abaixo."). Nenhum template novo foi submetido pra Meta.
    """
    from app.api.cloud_corretora_a import enviar_template_corretor, enviar_template_linha

    # first_name=None → vocativo neutro ("tudo bem"); cadencia_estado não guarda
    # nome. Sem o argumento o toque real estoura TypeError (posicional obrigatório).
    kwargs: dict[str, Any] = {
        "hook": texto,
        # SEM botão em toda a régua, MENOS no toque da oferta de áudio — quem
        # decide é o Toque (com_botao), não este arquivo.
        "com_botao": bool(com_botao),
        # Dona 29/07: "usa cada hora um template pra não terminar sempre igual".
        # O hash da rotação é do telefone, então o MESMO lead recebia sempre o
        # mesmo rodapé nos 4 toques. Passar o índice do toque desloca a rotação
        # e faz o fecho variar ao longo da sequência dele.
        "variacao": variacao,
    }
    if com_botao and cta:
        kwargs["cta"] = cta
        # Variante b é a única viva do pool com botão (Dona 28/07) — pedir
        # explicitamente evita cair numa casca aposentada se o pool crescer.
        kwargs["variantes"] = ("b",)
    # O carimbo da automação liga a checagem final de opt-out dentro do
    # emissor e marca a mensagem como de robô no thread. Vale pras DUAS
    # saídas: até 07/10 o caminho da Pessoa12 descartava `origem`, e o marco da
    # nutrição pela Pessoa12 passava por fora da trava. Quem não passa segue byte a
    # byte como era.
    if origem:
        kwargs["origem"] = origem
    if linha is None:
        # ⚠️ `enviar_template_corretor` NÃO aceita os kwargs de marketing — passá-los
        # aqui estoura TypeError e cala a cadência inteira. `origem` ele aceita.
        return enviar_template_corretor(telefone, None, **kwargs)
    # Só o carrossel passa isso hoje. Default False mantém a regra dura
    # (disparo só UTILITY) pra todo caller que não pediu nada.
    kwargs["permitir_marketing"] = permitir_marketing
    # Quem PEDE marketing quer marketing (braço do teste A/B), não um fallback
    # que só acontece se todo UTILITY falhar. A cota segue sendo o teto.
    kwargs["preferir_marketing"] = permitir_marketing
    kwargs["cap_diario_da_linha"] = cap_diario_da_linha
    # `instance` = o cadastro `corretoras.evolution_instance` da linha; é o que
    # identifica o THREAD da corretora em mensagens_whatsapp.
    return enviar_template_linha(
        linha, telefone, None,
        instance=cloud_lines.instance_da_linha(linha),
        corretora_id=corretora_id,
        **kwargs,
    )


def _chamar_sender(
    sender: Callable[..., dict[str, Any]],
    telefone: str,
    texto: str,
    *,
    com_botao: bool,
    linha: Any = None,
    corretora_id: int | None = None,
    variacao: int = 0,
    **extras_opcionais: Any,
) -> dict[str, Any]:
    """Chama o sender passando `linha`/`corretora_id` SÓ se ele aceitar.

    O contrato histórico do sender é `(telefone, texto, *, com_botao)` — é o que
    os fakes dos testes e os callers do painel implementam. Multi-linha adicionou
    dois kwargs; passá-los às cegas estouraria TypeError em todo sender antigo.
    Inspeciona a assinatura uma vez por chamada (barato: o tick faz ≤ cap/dia).
    Sender com **kwargs recebe tudo.
    """
    extras: dict[str, Any] = {}
    try:
        import inspect

        params = inspect.signature(sender).parameters
        aceita_tudo = any(
            p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
        )
        if aceita_tudo or "linha" in params:
            extras["linha"] = linha
        if aceita_tudo or "corretora_id" in params:
            extras["corretora_id"] = corretora_id
        if aceita_tudo or "variacao" in params:
            extras["variacao"] = variacao
        # Kwargs opcionais (ex: permitir_marketing / cap_diario_da_linha do
        # carrossel): mesma regra — só passa pro sender que declara ou aceita
        # **kwargs. Sender de teste com assinatura curta segue intacto.
        for k, v in extras_opcionais.items():
            if aceita_tudo or k in params:
                extras[k] = v
    except (TypeError, ValueError):  # builtin/C sem assinatura introspectável
        extras = {}
    return sender(telefone, texto, com_botao=com_botao, **extras)


@dataclass
class TickResult:
    considerados: int = 0
    mensagens_enfileiradas: int = 0
    tarefas_ligacao: int = 0
    remarketing: int = 0
    concluidas: int = 0
    pulados_ja_feito: int = 0
    bloqueados: dict[str, int] = field(default_factory=dict)
    # Opt-out (07/10): estados tirados da régua nesta volta, por caminho do
    # pedido. `bloqueados["opt_out_indisponivel"]` = toque devido segurado
    # porque a leitura de opt-out falhou.
    opt_out: int = 0
    opt_out_por_caminho: dict[str, int] = field(default_factory=dict)
    detalhes: list[dict[str, Any]] = field(default_factory=list)


# ── Quem pediu pra parar, por QUALQUER caminho (07/10/2026) ─────────────────
#
# O follow-up só olhava o próprio `status`. A lead 7880 clicou "Parar
# mensagens" em 30/08 (gravado só no api-disparos) e levou mais 5 follow-ups,
# 3 deles com o card em "Não perturbe". Mesmo conserto da nutrição, com as
# MESMAS funções (`opt_out.ler_mapa` / `opt_out.tirar_da_regua`):
#   1. UMA leitura do mapa por volta. Estado da régua (ativa, remarketing,
#      pausada) cujo telefone está lá vira `opt_out` com o caminho no evento —
#      devido ou não. O tick só lê ativa/remarketing: nunca mais é pego.
#   2. FAIL-CLOSED: leitura falhou = nada sai nesta volta; cada devido conta
#      em `bloqueados["opt_out_indisponivel"]` e continua devido.
#   3. O envio leva `origem` (toque e remarketing genérico): a trava do emissor
#      confere de novo na hora de sair. Barrou (`erro="opt_out"`) = estado vira
#      `opt_out` com caminho `emissor`, o toque não conta nem avança.
ORIGEM_CADENCIA = "cadencia"
ORIGEM_CADENCIA_REMARKETING = "cadencia_remarketing"
# "pausada" (pediu pausa de 30 dias) também: volta sozinha pra régua.
_STATUS_NA_REGUA = ("ativa", "remarketing", "pausada")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _bloqueio(res: TickResult, motivo: str) -> None:
    res.bloqueados[motivo] = res.bloqueados.get(motivo, 0) + 1


def _canonizar_telefone(telefone: str) -> str:
    """Forma CANÔNICA do telefone na cadência: E.164 sem '+' (o mesmo que
    `normalize_phone` devolve e que os writers já gravam).

    Degrada pra "só os dígitos" quando o número não é normalizável (curto,
    estrangeiro, mock de teste) — o importante é nunca gravar duas grafias do
    MESMO número ('+5515...' e '5515...'), que era a origem das cadências gêmeas.
    """
    from app.services.phone import normalize_phone_kommo

    canon = normalize_phone_kommo(telefone)
    if canon:
        return canon
    return re.sub(r"\D", "", telefone or "") or (telefone or "")


def iniciar_cadencia(
    session: Session,
    *,
    telefone: str,
    lead_id: str | None = None,
    corretora_id: int | None = None,
    tenant_id: int = DEFAULT_TENANT_ID,
    agora: datetime | None = None,
) -> CadenciaEstado:
    """Cria (ou devolve) o estado de cadência do lead. Idempotente por
    (tenant, telefone): 2 chamadas não criam 2 cadências. proximo_toque_em = agora
    (o D0 vence imediatamente).

    Toda matrícula nasce do Follow-up (move manual da Pessoa12 / auto-enroll) — o D0
    é SEMPRE o reencontro. Boas-vindas de cadastro NÃO passa por aqui: é a
    automação separada do intake (lei Dona 10/07, app/services/lead_intake.py).
    """
    agora = agora or _now()
    # CANONIZAÇÃO NA FRONTEIRA DE ESCRITA: uma forma só (E.164 sem '+', igual ao
    # que normalize_phone devolve e ao que os writers já gravam). O seed legado
    # gravou COM '+' e a comparação por igualdade exata criava cadência gêmea
    # ('+5515...' vs '5515...'), com opt-out parando só uma delas.
    telefone_canon = _canonizar_telefone(telefone)

    # Lookup defensivo por SUFIXO-8 (heurística — nunca chave de banco: colide
    # entre DDDs diferentes). Pega as gêmeas legadas com '+' / sem o 9.
    suf = _suf8(telefone_canon)
    q = session.query(CadenciaEstado).filter(CadenciaEstado.tenant_id == tenant_id)
    if suf:
        q = q.filter(CadenciaEstado.telefone.like(f"%{suf}"))
    candidatos = q.all()
    existente = next(
        (e for e in candidatos if e.telefone == telefone_canon),
        None,
    ) or next(
        (e for e in candidatos if suf and _suf8(e.telefone) == suf),
        None,
    )
    if existente is not None:
        return existente

    estado = CadenciaEstado(
        tenant_id=tenant_id,
        lead_id=lead_id,
        telefone=telefone_canon,
        corretora_id=corretora_id,
        toque_atual=0,
        proximo_toque_em=agora,
        status="ativa",
        criado_em=agora,
        atualizado_em=agora,
    )
    session.add(estado)
    session.flush()
    log.info(
        "cadencia_iniciada",
        tenant_id=tenant_id,
        telefone=telefone[-4:],
        cadencia_id=estado.id,
    )
    return estado


# NOTA: existia aqui um `opt_out(session, telefone=...)` com ZERO callers, que
# casava telefone por igualdade EXATA de string — logo, com a duplicata legada
# ('+5515...' vs '5515...') ele silenciava só UMA das gêmeas. Removido de
# propósito (auditoria 07/2026) pra ninguém plugá-lo depois e reintroduzir o
# bug. Quem para a cadência hoje é `processar_resposta_cadencia`, que agenda
# TODAS as vivas do sufixo.


def _evento_ja_registrado(session: Session, tenant_id: int, cadencia_id: int, toque: int) -> bool:
    return (
        session.query(CadenciaEvento)
        .filter(
            CadenciaEvento.tenant_id == tenant_id,
            CadenciaEvento.cadencia_id == cadencia_id,
            CadenciaEvento.toque == toque,
        )
        .first()
        is not None
    )


def _com_botao(indice_msg: int) -> bool:
    """O toque tem botão? Pergunta pra RÉGUA, nunca compara índice.

    Follow-up é sem botão desde 28/07 (Dona) — a exceção é o toque que oferece
    o áudio (D3, `com_botao=True` na tupla TOQUES): lá o "sim" é um clique.
    Índice fixado aqui quebraria de novo na próxima mudança de régua.
    """
    for t in TOQUES:
        if t.indice == indice_msg:
            return bool(t.com_botao)
    return False


def toque_com_botao() -> Toque | None:
    """O toque de mensagem que sai COM botão (hoje: a oferta de áudio do D3).

    Devolve o Toque inteiro — quem precisa do índice tira daqui, em vez de
    escrever `3` e quebrar quando a régua mudar de novo.
    """
    for t in TOQUES:
        if t.tipo == TIPO_MENSAGEM and t.com_botao:
            return t
    return None


TZ_OPERACAO = ZoneInfo("America/Sao_Paulo")


def _janela_dia_local(agora: datetime) -> tuple[datetime, datetime]:
    """[início, fim) do DIA LOCAL (America/Sao_Paulo) que contém `agora`,
    expressos em UTC (é assim que `cadencia_eventos.at` é gravado).

    Calculado em Python, não em SQL: `AT TIME ZONE` não existe no SQLite
    in-memory dos testes. Aceita `agora` naive tratando como UTC.
    """
    if agora.tzinfo is None:
        agora = agora.replace(tzinfo=timezone.utc)
    local = agora.astimezone(TZ_OPERACAO)
    inicio_local = local.replace(hour=0, minute=0, second=0, microsecond=0)
    fim_local = inicio_local + timedelta(days=1)
    return inicio_local.astimezone(timezone.utc), fim_local.astimezone(timezone.utc)


# Linha default do _default_sender quando o estado não tem dona gravada.
# É o balde contra o qual essas mensagens têm que ser medidas.
_CORRETORA_CORRETOR = 3


def _cap_diario_atingido(
    session: Session,
    tenant_id: int,
    agora: datetime,
    reserva: int = 0,
    *,
    corretora_id: int | None = None,
) -> bool:
    """Cap diário de mensagens — POR LINHA Cloud, não global (multi-linha 27/07).

    ⚠️ A mudança que importa: antes o cap era GLOBAL, então a Pessoa10 entrando na
    automação comeria a cota da Pessoa12 (cuja linha já tomou 2 bans em julho e opera a
    ≤25/dia). Agora `settings.cadencia_cap_diario` vale POR LINHA: cada corretora
    tem o SEU balde diário, e a reserva de follow-up (`reserva`) é aplicada
    dentro do balde dela.

    `corretora_id=None` = cap GLOBAL (comportamento histórico) — usado por quem
    ainda não sabe de qual linha está falando (chamadas legadas). A nutrição
    passou a mandar a corretora da linha por onde o marco sai em 07/10/2026.

    Correções da auditoria 07/2026 preservadas:
      - a janela é o dia LOCAL de São Paulo (não o dia UTC, que virava às 21h de
        SP e liberava cota nova à noite);
      - conta também as mensagens planejadas
        (`tarefas_corretora.msg_planejada_enviada_em`), que saem pela MESMA linha
        e consomem o MESMO limite anti-ban.
    """
    n = mensagens_no_balde(session, tenant_id, agora, corretora_id=corretora_id)
    # Cap vem do painel (config_operacional) com fallback pro .env — ver
    # app/services/config_operacional.py. Era settings.* direto, o que fazia
    # cada ajuste de limite virar edição de arquivo + restart.
    cap = config_operacional.cap_da_corretora(corretora_id)
    return _aplicar_rampa_e_comparar(session, n, cap, reserva, corretora_id)


def mensagens_no_balde(
    session: Session,
    tenant_id: int,
    agora: datetime,
    *,
    corretora_id: int | None = None,
) -> int:
    """Quantas mensagens já saíram hoje (dia de SP) no balde desta linha —
    o número que `_cap_diario_atingido` compara com o teto. `None` = global."""
    inicio_dia, fim_dia = _janela_dia_local(agora)
    # ⚠️ Conta só o que SAIU DE VERDADE (achado do Grok, 29/07). Antes qualquer
    # evento tipo='mensagem' comia cota: toque pulado por categoria marketing
    # (`payload.pulado`), envio em DRY-RUN e falha de POST entravam no balde
    # anti-ban sem nenhuma mensagem ter chegado na Meta. O limite existe pra
    # proteger a LINHA — o que não foi despachado não a expõe.
    q = (
        session.query(func.count(CadenciaEvento.id))
        .filter(
            CadenciaEvento.tenant_id == tenant_id,
            CadenciaEvento.tipo == TIPO_MENSAGEM,
            CadenciaEvento.at >= inicio_dia,
            CadenciaEvento.at < fim_dia,
            CadenciaEvento.payload["sent"].as_boolean().is_(True),
        )
    )
    if corretora_id is not None:
        # Atribui o evento à linha pelo dono do ESTADO de cadência (é a linha por
        # onde a mensagem saiu). Estado sem corretora_id gravado saiu pela linha
        # da Pessoa12 (default histórico do _default_sender) — conta no balde dela.
        ids = _ids_da_corretora(session, corretora_id)
        cond = CadenciaEstado.corretora_id.in_(tuple(ids))
        e_corretor = _e_linha_corretor(session, corretora_id)
        if e_corretor:
            cond = cond | CadenciaEstado.corretora_id.is_(None)
        pelo_estado = CadenciaEvento.cadencia_id.in_(
            session.query(CadenciaEstado.id).filter(
                CadenciaEstado.tenant_id == tenant_id, cond
            )
        )
        # EXCEÇÃO (07/10/2026): evento que GRAVOU a linha (`payload.linha`, a
        # nutrição grava) conta no balde DESSA linha, não no da dona. Marco de
        # lead da Pessoa10 que saiu pela Pessoa12 pesa na Pessoa12; antes pesaria na Pessoa10 e a Pessoa12
        # passaria do teto dela. Evento sem `linha` segue a regra da dona.
        slug_balde = _slug_do_balde(session, corretora_id, e_corretor)
        linha_ev = CadenciaEvento.payload["linha"].astext
        if slug_balde is None:
            q = q.filter(pelo_estado)
        else:
            q = q.filter(
                ((linha_ev.is_(None)) & pelo_estado) | (linha_ev == slug_balde)
            )
    n = q.scalar() or 0
    n += _msg_planejadas_no_dia(
        session, tenant_id, inicio_dia, fim_dia, corretora_id=corretora_id
    )
    return int(n)


def _aplicar_rampa_e_comparar(
    session: Session, n: int, cap: int, reserva: int, corretora_id: int | None
) -> bool:
    if corretora_id is not None:
        # RAMPA (30/07): linha em AQUECIMENTO tem teto menor que o configurado.
        # Número frio saltando pro teto é o padrão que derruba qualidade e leva
        # ban — e o 0000 é o telefone DA CASA (se cai, a Acme perde o
        # principal, não só uma automação). Sobe de 20 até 60 em ~1 semana, e só
        # avança com qualidade GREEN na Meta. Ver app/services/rampa.py.
        slug_rampa = settings.rampa_slugs.get(str(corretora_id))
        if slug_rampa:
            try:
                from app.services.rampa import cap_de_hoje

                r = cap_de_hoje(session, slug_rampa, corretora_id)
                if r.cap < cap:
                    log.info("cap_limitado_pela_rampa", slug=slug_rampa,
                             cap_config=cap, cap_rampa=r.cap, motivo=r.motivo)
                    cap = r.cap
            except Exception as e:  # noqa: BLE001
                # Rampa indisponível NÃO libera volume: cai no degrau mínimo.
                from app.services.rampa import DEGRAUS

                log.warning("rampa_falhou_usando_degrau_minimo", erro=str(e)[:120])
                cap = min(cap, DEGRAUS[0][1])

    return int(n) >= cap - reserva


def _ids_da_corretora(session: Session, corretora_id: int) -> set[int]:
    """{id interno, kommo_user_id} da corretora — o `corretora_id` gravado nos
    estados/tarefas pode ser qualquer um dos dois, dependendo de quem escreveu.
    Degrada pro id recebido se o cadastro não resolver."""
    ids = {int(corretora_id)}
    try:
        from sqlalchemy import or_

        from app.db.models import Corretora

        with session.begin_nested():
            row = (
                session.query(Corretora.id, Corretora.kommo_user_id)
                .filter(
                    or_(
                        Corretora.id == int(corretora_id),
                        Corretora.kommo_user_id == int(corretora_id),
                    )
                )
                .first()
            )
        if row is not None:
            ids.add(int(row[0]))
            if row[1] is not None:
                ids.add(int(row[1]))
    except Exception as e:  # noqa: BLE001 — sem cadastro: só o id recebido
        log.warning("cadencia_ids_corretora_indisponivel", erro=str(e)[:120])
    return ids


def _e_linha_corretor(session: Session, corretora_id: int) -> bool:
    """True se `corretora_id` é a dona da linha da Pessoa12. Só pra decidir se o balde
    dela também absorve os estados SEM corretora_id gravado (legado).

    Cadastro ilegível (INDETERMINADO) conta como Pessoa12: é onde os estados legados
    sempre foram contabilizados, e é o balde mais conservador (o da linha que já
    tomou ban)."""
    linha = cloud_lines.linha_da_corretora(corretora_id, session=session)
    if linha is cloud_lines.INDETERMINADO:
        return True
    return linha is not None and linha.slug == "corretor"


def _slug_do_balde(session: Session, corretora_id: int, e_corretor: bool) -> str | None:
    """Slug da linha do balde (pra casar com `payload.linha`). None quando a
    corretora não tem linha resolvível: aí só a regra da dona vale."""
    if e_corretor:
        return "corretor"
    linha = cloud_lines.linha_da_corretora(corretora_id, session=session)
    if linha is None or linha is cloud_lines.INDETERMINADO:
        return None
    return linha.slug


def _msg_planejadas_no_dia(
    session: Session,
    tenant_id: int,
    inicio_dia: datetime,
    fim_dia: datetime,
    *,
    corretora_id: int | None = None,
) -> int:
    """Mensagens planejadas já enviadas na janela — mesma linha, mesmo cap.
    Filtra por corretora quando o cap é por linha. Best-effort: tabela/coluna
    ausente (DB de teste enxuto) conta 0."""
    try:
        extra = ""
        params: dict[str, Any] = {"t": tenant_id, "ini": inicio_dia, "fim": fim_dia}
        if corretora_id is not None:
            ids = tuple(_ids_da_corretora(session, corretora_id))
            marcadores = ", ".join(f":c{i}" for i in range(len(ids)))
            extra = f" AND corretora_id IN ({marcadores})"
            params.update({f"c{i}": cid for i, cid in enumerate(ids)})
        with session.begin_nested():
            row = session.execute(_sql_text(
                """SELECT count(*) FROM tarefas_corretora
                   WHERE tenant_id = :t
                     AND msg_planejada_enviada_em >= :ini
                     AND msg_planejada_enviada_em < :fim""" + extra),
                params,
            ).scalar()
        return int(row or 0)
    except Exception as e:  # noqa: BLE001 — cap degrada pro comportamento antigo
        log.warning("cadencia_cap_msgplanejada_indisponivel", erro=str(e)[:120])
        return 0


# guard in-process de espaçamento mínimo entre enfileiramentos reais.
_LAST_ENQUEUE: dict[str, float] = {"at": 0.0}

# Falhas de envio SEGUIDAS que abortam a varredura (circuit breaker). Erro de
# linha/token não melhora no lead seguinte — parar cedo evita queimar a WABA.
_MAX_FALHAS_SEGUIDAS = 3


def _owner_ids_corretor(session: Session) -> set[int]:
    """IDs que representam a Pessoa12 como dono de lead: id interno + kommo_user_id
    (mesma identificação do enroll-on-followup — NUNCA por nome). Vazio quando a
    Pessoa12 não é resolvível: nesse caso o guard de dono fica DESLIGADO (não pode
    matar cadência legítima por falta de cadastro)."""
    try:
        from app.api.cloud_corretora_a import INSTANCE as CORRETOR_INSTANCE
        from app.db.models import Corretora

        corretor = (
            session.query(Corretora)
            .filter(Corretora.evolution_instance == CORRETOR_INSTANCE, Corretora.ativo.is_(True))
            .first()
        )
        if corretor is None:
            return set()
        ids = {int(corretor.id)}
        if corretor.kommo_user_id is not None:
            ids.add(int(corretor.kommo_user_id))
        return ids
    except Exception as e:  # noqa: BLE001 — sem espinha/corretoras: guard desligado
        log.warning("cadencia_owner_corretor_indisponivel", erro=str(e)[:120])
        return set()


def _owner_ids_automacao(session: Session) -> set[int]:
    """IDs (internos + kommo_user_id) de TODAS as corretoras com linha Cloud —
    ou seja, as que estão no regime de automação (multi-linha 27/07).

    Substitui o `_owner_ids_corretor` no guard de dono do tick: antes o tick só
    processava leads da Pessoa12; agora processa de qualquer corretora com linha, e o
    guard passa a significar "o dono do lead é o dono DAQUELE estado de cadência".
    Vazio = guard desligado (não pode matar cadência por falta de cadastro).
    """
    ids = set(cloud_lines.corretoras_com_linha(session))
    if not ids:
        # Fallback: registry sem linha resolvível → mantém a semântica antiga
        # (guard só da Pessoa12) em vez de liberar tudo.
        return _owner_ids_corretor(session)
    return ids


def _donos_por_suf8(session: Session, tenant_id: int) -> dict[str, int]:
    """dict[sufixo-8 do telefone -> owner_id] dos leads da espinha, em UMA query.

    Só entram leads com owner_id NÃO nulo: owner NULL = "sem dono conhecido",
    que o guard trata como não-bloqueio. Sufixo-8 é heurística de LOOKUP (pode
    colidir entre DDDs) — por isso serve só pra bloquear, nunca como chave.
    Degrada pra {} (guard desligado) se a espinha não existir.
    """
    try:
        rows = session.execute(_sql_text(
            """SELECT right(regexp_replace(l.telefone, '\\D', '', 'g'), 8) AS suf,
                      l.owner_id
               FROM leads l
               WHERE l.tenant_id = :t AND l.telefone IS NOT NULL
                 AND l.owner_id IS NOT NULL"""), {"t": tenant_id}).fetchall()
        return {r[0]: int(r[1]) for r in rows if r[0] and r[1] is not None}
    except Exception as e:  # noqa: BLE001 — DB de teste enxuto/SQLite sem regexp
        log.warning("cadencia_donos_indisponivel", erro=str(e)[:120])
        return {}


def tick(
    agora: datetime | None = None,
    *,
    session: Session | None = None,
    session_factory: Callable[[], Session] | None = None,
    sender: Callable[..., dict[str, Any]] | None = None,
    tenant_id: int = DEFAULT_TENANT_ID,
) -> TickResult:
    """Roda 1 varredura da cadência. Para cada lead ativo, decide se há toque
    devido HOJE e o quê — e ENFILEIRA (mensagem no disparo DRY-RUN / tarefa de
    ligação como evento). Idempotente: toque já registrado é pulado.

    Args:
        agora: instante de referência (injetável pra teste de avanço por dia).
        session / session_factory: injeção de DB (teste usa SQLite in-memory).
        sender: fn de envio (default = motor Fase 7 em DRY-RUN). Só chamada pra
            mensagens; tarefa de ligação nunca envia.
    """
    if not config_operacional.cadencia_ativa():
        log.info("cadencia_inativa_skip")
        return TickResult()

    agora = agora or _now()
    # Dia útil (02/10/2026): sábado/domingo o tick não manda nada. O que está
    # devido continua devido e sai na segunda. Ver app/services/dia_util.py.
    from app.services import dia_util

    if not dia_util.pode_enviar(agora, motor="cadencia"):
        return TickResult()
    sender = sender or _default_sender

    owns_session = session is None
    if owns_session:
        if session_factory is None:
            from app.db.session import SessionLocal

            session_factory = SessionLocal
        session = session_factory()

    res = TickResult()

    # ── trava contra tick sobreposto (achado convergente Grok + Sol, 29/07) ──
    # O fluxo é "verifica → envia na Meta → grava o evento". Dois ticks rodando
    # ao mesmo tempo (timer + chamada manual, cron duplicado, execução anterior
    # lenta) liam o MESMO estado antes de qualquer um gravar — e o lead recebia
    # duas vezes. O UNIQUE de (cadencia_id, toque) protege o BANCO, não o
    # WhatsApp: quando ele estoura, a mensagem já saiu.
    #
    # Advisory lock de sessão do Postgres, o MESMO padrão que o Inspector já usa
    # (app/inspector/pipeline.py). Não-bloqueante: se outro tick está rodando,
    # este não roda, e o minuto seguinte pega o que sobrou.
    conn_trava = travar(NS_CADENCIA, tenant_id)
    if conn_trava is OCUPADO:
        log.info("cadencia_tick_concorrente_pulou", tenant_id=tenant_id)
        _bloqueio(res, "tick_concorrente")
        if owns_session:
            session.close()
        return res

    try:
        # Fim da pausa a pedido: quem escolheu "2 — pausa por 30 dias" volta
        # pro remarketing quando a data chega. Passo explícito em vez de somar
        # 'pausada' na query abaixo — assim a volta fica auditável no log e o
        # loop do tick não precisa saber que o estado existe.
        voltaram = session.execute(
            _sql_text(
                """UPDATE cadencia_estado
                      SET status = 'remarketing', atualizado_em = :agora
                    WHERE tenant_id = :t AND status = :pausada
                      AND proximo_toque_em <= :agora"""
            ),
            {"t": tenant_id, "pausada": STATUS_PAUSADA, "agora": agora},
        ).rowcount
        if voltaram:
            session.commit()
            log.info("cadencia_pausa_terminou", quantidade=voltaram)

        # OPT-OUT por qualquer caminho, ANTES dos devidos (07/10). Quem está
        # no mapa sai da régua já — devido ou não —, então a lista abaixo nem
        # o enxerga. None = leitura falhou → nada sai nesta volta.
        bloqueio = opt_out_svc.ler_mapa(session, motor=ORIGEM_CADENCIA)
        if bloqueio:
            _tirar_da_regua_quem_pediu(session, tenant_id, agora, bloqueio, res)

        devidos = (
            session.query(CadenciaEstado)
            .filter(
                CadenciaEstado.tenant_id == tenant_id,
                CadenciaEstado.status.in_(("ativa", "remarketing")),
                CadenciaEstado.proximo_toque_em <= agora,
            )
            .all()
        )
        res.considerados = len(devidos)
        if bloqueio is None:
            # FAIL-CLOSED: sem saber quem pediu pra parar, nenhum toque sai
            # (nem a tarefa de ligação avança). Tudo continua devido.
            for _ in devidos:
                _bloqueio(res, opt_out_svc.BLOQUEIO_OPT_OUT_INDISPONIVEL)
            devidos = []

        # Sufixos (8 díg) dos leads que estão no funil de NUTRIÇÃO — estados de
        # remarketing desses leads pertencem ao nutricao.tick, não a este loop.
        nutricao_sufs: set[str] = set()
        try:
            _rows = session.execute(_sql_text(
                """SELECT right(regexp_replace(l.telefone, '\\D', '', 'g'), 8)
                   FROM leads l JOIN pipeline_stages s ON s.id = l.stage_id
                   WHERE l.tenant_id = :t AND s.funil = 'remarketing'
                     AND l.telefone IS NOT NULL"""), {"t": tenant_id}).fetchall()
            nutricao_sufs = {r[0] for r in _rows if r[0]}
        except Exception:
            nutricao_sufs = set()  # degrada: sem skip (comportamento antigo)

        # CONVERSA HUMANA EM ANDAMENTO: robô não fala por cima de gente.
        #
        # A trava existia só no carrossel (Dona 31/07) — e o carrossel entrega o
        # lead pra corretora, momento em que ele passa a ser tocado POR AQUI.
        # Resultado em agosto/2026: 120 disparos caíram em cima de 97 pessoas
        # que estavam conversando com uma corretora de verdade. A Eduarda
        # Ribeiro falou de restrição no nome em 21/08, levou quatro templates
        # entre 25/08 e 01/09 e pediu "Parar mensagens" — não recusou o imóvel,
        # cansou da insistência.
        #
        # Mesma janela e mesmo critério do carrossel: saída NOSSA, sem carimbo
        # de robô e que não seja template = gente digitando.
        humano_sufs: set[str] = set()
        try:
            _rows = session.execute(_sql_text(
                """SELECT DISTINCT right(regexp_replace(m.contato_telefone,'\\D','','g'),8)
                     FROM mensagens_whatsapp m
                    WHERE m.from_corretora
                      AND m.contato_telefone IS NOT NULL
                      AND coalesce(m.tipo_mensagem,'') <> 'template'
                      AND coalesce(m.conteudo_metadata->>'origem','') = ''
                      AND m.timestamp_evolution > now() - make_interval(days => :dias)"""),
                {"dias": settings.carrossel_pausa_conversa_humana_dias}).fetchall()
            humano_sufs = {r[0] for r in _rows if r[0]}
        except Exception:
            # Degrada ABERTO de propósito: se a consulta falhar, a cadência
            # segue como antes. Travar tudo por erro de leitura seria pior que
            # o problema que esta trava resolve.
            humano_sufs = set()

        # Dono do lead na espinha (uma query só, não por iteração): cadência que
        # virou pra outra corretora NÃO pode continuar tocando pela linha antiga.
        donos_por_suf = _donos_por_suf8(session, tenant_id)
        owners_automacao = _owner_ids_automacao(session)

        falhas_seguidas = 0
        for est in devidos:
            if est.status == "opt_out":
                _bloqueio(res, "opt_out")
                continue

            # Alguém da casa falou com essa pessoa nos últimos dias: não manda
            # automático por cima. Quem está conversando resolve melhor que um
            # template — e insistir por cima é o que faz a pessoa pedir pra sair.
            if _suf8(est.telefone) in humano_sufs:
                _bloqueio(res, "conversa_humana")
                log.info(
                    "cadencia_conversa_humana_skip",
                    cadencia_id=est.id,
                    telefone_last4=est.telefone[-4:],
                    janela_dias=settings.carrossel_pausa_conversa_humana_dias,
                )
                continue

            # GUARD DE DONO (semântica multi-linha): bloqueia quando o dono do
            # LEAD na espinha não é o dono DESTE estado de cadência — cobre as
            # duas portas de troca (transferência manual no /crm/transferir e
            # sobrescrita do Kommo no sync). Antes o teste era "não é a Pessoa12";
            # agora é "não é quem matriculou", o que vale pra N linhas.
            # owner NULL ou lead ausente na espinha = NÃO bloqueia (cadência
            # legítima sem lead canônico não pode morrer aqui).
            if owners_automacao:
                dono = donos_por_suf.get(_suf8(est.telefone))
                donos_do_estado = (
                    _ids_da_corretora(session, est.corretora_id)
                    if est.corretora_id is not None
                    else owners_automacao
                )
                if dono is not None and dono not in donos_do_estado:
                    _bloqueio(res, "dono_mudou")
                    log.info(
                        "cadencia_dono_mudou_skip",
                        cadencia_id=est.id,
                        telefone_last4=est.telefone[-4:],
                        owner_id=dono,
                        dono_do_estado=est.corretora_id,
                    )
                    continue

            # LINHA DA DONA: resolve por qual linha Cloud este lead é tocado.
            # FAIL-CLOSED: corretora sem linha (hoje a Pessoa11, ainda no Evolution)
            # não é tocada pela automação — skip silencioso, sem exception.
            # Estado SEM corretora_id gravado (legado) cai na linha da Pessoa12, que é
            # de onde essas cadências sempre saíram. Cadastro ILEGÍVEL
            # (INDETERMINADO) também degrada pra Pessoa12: é o comportamento histórico,
            # e travar aí mataria cadência legítima por falta de cadastro.
            linha = (
                cloud_lines.linha_da_corretora(est.corretora_id, session=session)
                if est.corretora_id is not None
                else cloud_lines.line_by_slug("corretor")
            )
            if linha is cloud_lines.INDETERMINADO:
                linha = cloud_lines.line_by_slug("corretor")
            if linha is None:
                _bloqueio(res, "sem_linha_cloud")
                log.info(
                    "cadencia_sem_linha_skip",
                    cadencia_id=est.id, corretora_id=est.corretora_id,
                )
                continue

            # TRAVA ANTI-COLISÃO (F1 do B9): lead em conversa VIVA com a Corretor
            # (recovery) não recebe toque da cadência — ADIA pro próximo tick
            # SEM avançar o toque (proximo_toque_em fica devido; quando a
            # conversa da Corretor esfriar, a cadência retoma de onde parou).
            # `agora=agora` (não o relógio real): a janela de 7 dias tem que ser
            # medida do MESMO instante que o tick usa pra decidir o que é devido.
            # Sem isso o guard olhava now() enquanto o tick olhava `agora`, e as
            # duas visões divergiam — invisível em produção (agora É now), mas
            # fazia o teste do guard expirar sozinho com a passagem do tempo e
            # deixaria um tick retroativo (backfill) medir a janela errada.
            if reengajamento_guard.corretor_conversa_ativa(
                est.telefone, agora=agora, session=session
            ):
                _bloqueio(res, "corretor_ativa")
                log.info(
                    "reengajamento_guard_bloqueou",
                    motor="cadencia_tick",
                    telefone_last4=est.telefone[-4:],
                )
                continue

            # TRAVA LIGAÇÃO × AUTOMAÇÃO (05/08): a corretora acabou de ligar pra
            # essa pessoa (janela de ligação). Mensagem automática entrando por
            # cima no mesmo dia, de uma fonte que não sabe da outra, é o
            # "vocês não se conversam aí?". ADIA igual ao guard da Corretor — o toque
            # continua devido e sai quando a janela de silêncio fechar.
            if reengajamento_guard.ligacao_recente(
                est.telefone, agora=agora, session=session,
                tenant_id=tenant_id,
            ):
                _bloqueio(res, "ligacao_recente")
                log.info(
                    "reengajamento_guard_bloqueou",
                    motor="cadencia_tick",
                    trava="ligacao_recente",
                    telefone_last4=est.telefone[-4:],
                )
                continue

            # TRAVA CONVERSA HUMANA (06/08): a corretora está conversando com
            # essa pessoa AGORA, na mão. Reengajar quem já está engajado não é
            # só inútil — chega como se a imobiliária não soubesse o que a
            # própria equipe está fazendo, e a corretora tem que pedir desculpa
            # ("desconsidera essa última mensagem"). ADIA, não cancela: se a
            # conversa esfriar de verdade, o toque volta a ficar devido.
            if reengajamento_guard.conversa_humana_ativa(
                est.telefone, agora=agora, session=session,
                tenant_id=tenant_id,
            ):
                _bloqueio(res, "conversa_humana")
                log.info(
                    "reengajamento_guard_bloqueou",
                    motor="cadencia_tick",
                    trava="conversa_humana",
                    telefone_last4=est.telefone[-4:],
                )
                continue

            if est.status == "remarketing":
                # Leads no FUNIL DE NUTRIÇÃO (etapas Base/30/60/90/120/150) são do
                # motor nutricao.py (marcos com copy própria + avanço de etapa).
                # O caminho genérico daqui NÃO pode tocá-los: mandaria a copy
                # errada e travaria a progressão (não avança etapa). Skip.
                if _suf8(est.telefone) in nutricao_sufs:
                    _bloqueio(res, "nutricao_owned")
                    continue
                try:
                    _processar_remarketing(session, est, agora, res, sender, linha=linha)
                    falhas_seguidas = 0
                except Exception as e:  # noqa: BLE001 — falha não derruba o tick
                    falhas_seguidas += 1
                    _bloqueio(res, "envio_falhou")
                    log.warning(
                        "cadencia_envio_falhou",
                        cadencia_id=est.id, toque=None,
                        telefone_last4=est.telefone[-4:], erro=str(e)[:200],
                    )
                    _registrar_falha_envio(session, est, None, agora, str(e))
                    if falhas_seguidas >= _MAX_FALHAS_SEGUIDAS:
                        log.error("cadencia_circuit_breaker", falhas_seguidas=falhas_seguidas)
                        _bloqueio(res, "circuit_breaker")
                        session.commit()
                        break
                session.commit()
                continue

            toque = TOQUES[est.toque_atual] if est.toque_atual < len(TOQUES) else None
            if toque is None:
                # já passou dos 8 — vira remarketing
                _virar_remarketing(session, est, agora)
                continue

            # idempotência: esse toque já foi registrado?
            if _evento_ja_registrado(session, tenant_id, est.id, toque.indice):
                res.pulados_ja_feito += 1
                _avancar(session, est, agora)
                continue

            if toque.tipo == TIPO_TAREFA_LIGACAO:
                _registrar_tarefa_ligacao(session, est, toque, agora)
                res.tarefas_ligacao += 1
                res.detalhes.append({"telefone": est.telefone[-4:], "toque": toque.indice, "tipo": "tarefa_ligacao"})
                _avancar(session, est, agora)
                session.commit()
                continue

            # GATE DE CATEGORIA: a linha da Pessoa12 só despacha template UTILITY
            # (regra Dona). Toque rotulado 'marketing' era rótulo morto — o
            # texto saía por template UTILITY do mesmo jeito. Agora ele NÃO sai:
            # registra o pulo e avança (o toque 7 fica na lista pra não quebrar
            # os 27 leads com toque_atual=7 — encurtar TOQUES daria IndexError).
            if toque.categoria == "marketing":
                _bloqueio(res, "toque_marketing")
                _registrar_evento(
                    session, est, toque=toque.indice, tipo=TIPO_MENSAGEM, canal=toque.canal,
                    payload={
                        "texto": None,
                        "categoria": toque.categoria,
                        "pulado": "categoria_marketing",
                        "dry_run": None,
                        "sent": False,
                    },
                    agora=agora,
                )
                _avancar(session, est, agora)
                session.commit()
                continue

            # mensagem: passa pelos guards anti-ban antes de enfileirar.
            # CAP POR LINHA (não global): a Pessoa10 estourando a cota dela NÃO
            # bloqueia a Pessoa12, e vice-versa. A linha da Pessoa12 opera ≤25/dia (2 bans em
            # julho) — se o cap fosse global, a Pessoa10 comeria a cota dela.
            # Estado SEM corretora_id sai pela linha da Pessoa12 (default histórico do
            # _default_sender) — logo tem que ser medido contra o balde DELA, não
            # contra o global. Achado do Grok (29/07): com None a conta virava
            # global, misturando o volume de todas as linhas. Como hoje o cap
            # global é 25, igual ao da Pessoa12, o efeito era bloquear à toa (a Pessoa10
            # enchendo 25 calava os leads sem dona). Se o global subisse, viraria
            # o contrário: a Pessoa12 passando do teto anti-ban dela.
            cid_cap = est.corretora_id if est.corretora_id is not None else _CORRETORA_CORRETOR
            if _cap_diario_atingido(
                session, tenant_id, agora, corretora_id=cid_cap
            ):
                _bloqueio(res, "cap_diario")
                continue
            if time.monotonic() - _LAST_ENQUEUE["at"] < settings.cadencia_min_intervalo_s:
                _bloqueio(res, "muito_rapido")
                continue
            # INTERRUPTOR DA CORRETORA (06/10): relido do banco agora, antes
            # desta mensagem. Desligada/ilegível/sem leitura = não sai; o toque
            # continua devido e sai quando ela for religada.
            if not _corretora_liberada(res, est):
                continue

            # copy efetiva: override de `cadencia_copies` quando existe, senão default.
            # A assinatura é a de quem MANDA — o envio roteia pela linha da dona.
            texto = aplicar_assinatura(
                copy_efetiva(session, toque.indice, tenant_id=tenant_id),
                est.corretora_id,
            )
            com_botao = _com_botao(toque.indice)
            try:
                saiu = _enfileirar_mensagem(
                    session, est, toque, texto, com_botao, agora, sender, linha=linha,
                    res=res,
                )
            except Exception as e:  # noqa: BLE001 — falha de envio não derruba o tick
                # NÃO marca _LAST_ENQUEUE: nada saiu na linha, então o cooldown
                # anti-ban não corre — se corresse, os leads seguintes cairiam
                # em 'muito_rapido' e o circuit breaker nunca contaria as falhas.
                falhas_seguidas += 1
                _bloqueio(res, "envio_falhou")
                log.warning(
                    "cadencia_envio_falhou",
                    cadencia_id=est.id,
                    toque=toque.indice,
                    telefone_last4=est.telefone[-4:],
                    erro=str(e)[:200],
                )
                _registrar_falha_envio(session, est, toque, agora, str(e))
                # NÃO avança o toque: o lead tenta de novo no próximo tick.
                session.commit()
                if falhas_seguidas >= _MAX_FALHAS_SEGUIDAS:
                    log.error(
                        "cadencia_circuit_breaker",
                        falhas_seguidas=falhas_seguidas,
                        processados=res.mensagens_enfileiradas,
                    )
                    _bloqueio(res, "circuit_breaker")
                    break
                continue
            falhas_seguidas = 0
            if not saiu:
                # a trava do emissor barrou: o estado já virou opt_out, nada
                # saiu, o toque não avança.
                session.commit()
                continue
            _LAST_ENQUEUE["at"] = time.monotonic()
            res.mensagens_enfileiradas += 1
            res.detalhes.append({"telefone": est.telefone[-4:], "toque": toque.indice, "tipo": "mensagem", "com_botao": com_botao})
            _avancar(session, est, agora)
            # COMMIT POR LEAD: cada lead vira durável assim que o envio sai.
            # Antes o commit era só no fim — envio real acontecia ANTES da
            # gravação de idempotência, e um crash em lead posterior dava
            # rollback da linha inteira (o lead recebia de novo no tick seguinte).
            session.commit()

        session.commit()
    finally:
        # a trava sai POR ÚLTIMO: só depois de tudo commitado outro tick pode
        # entrar, senão o segundo voltaria a ler estado desatualizado.
        if owns_session:
            session.close()
        soltar(conn_trava, NS_CADENCIA, tenant_id)

    log.info(
        "cadencia_tick_done",
        considerados=res.considerados,
        mensagens=res.mensagens_enfileiradas,
        tarefas=res.tarefas_ligacao,
        remarketing=res.remarketing,
        pulados=res.pulados_ja_feito,
        bloqueados=res.bloqueados,
        opt_out=res.opt_out,
        opt_out_por_caminho=res.opt_out_por_caminho,
    )
    return res


# ── internals ─────────────────────────────────────────────────────────────────

def _registrar_evento(
    session: Session,
    est: CadenciaEstado,
    *,
    toque: int | None,
    tipo: str,
    canal: str,
    payload: dict[str, Any],
    agora: datetime,
) -> None:
    session.add(
        CadenciaEvento(
            tenant_id=est.tenant_id,
            cadencia_id=est.id,
            toque=toque,
            tipo=tipo,
            canal=canal,
            payload=payload,
            at=agora,
        )
    )
    session.flush()


TIPO_ENVIO_FALHOU = "envio_falhou"


def _enfileirar_mensagem(
    session: Session,
    est: CadenciaEstado,
    toque: Toque,
    texto: str,
    com_botao: bool,
    agora: datetime,
    sender: Callable[..., dict[str, Any]],
    *,
    linha: Any = None,
    res: TickResult | None = None,
) -> bool:
    """Envia (via motor Fase 7, DRY-RUN atrás de settings.dry_run) e grava o
    evento do toque. Levanta se o envio falhar — o caller decide o que fazer
    (NÃO avançar o toque, contar bloqueio, seguir o loop).

    Devolve False quando a trava de opt-out do emissor barrou (o estado já foi
    tirado da régua aqui, sem evento de mensagem); True quando saiu.

    `linha` (CloudLine da dona do lead) é repassada ao sender como kwarg APENAS
    quando ela existe E o sender aceita — senders de teste têm assinatura
    `(telefone, texto, *, com_botao)` e não devem quebrar."""
    # Gate de categoria: a cadência só despacha por template UTILITY. Toque
    # rotulado 'marketing' NÃO pode sair pela linha da Pessoa12 (regra Dona: disparo
    # nunca em MARKETING) — o rótulo era morto e o texto saía por UTILITY.
    if toque.categoria == "marketing":
        raise RuntimeError(f"toque_{toque.indice}_marketing_bloqueado")

    # ── sugestão da IA aprovada por humano (29/07) ──────────────────────────
    # A IA leu a conversa e escreveu a variável {{2}}; alguém aprovou no painel.
    # A CASCA do template não muda — só o miolo. Sem sugestão aprovada, ou com
    # a flag desligada, segue o texto padrão de sempre.
    #
    # Fail-safe: erro aqui NÃO pode impedir o follow-up de sair. Na dúvida vai o
    # template — que é exatamente o comportamento de hoje.
    sugestao_id: int | None = None
    if getattr(settings, "sugestao_ia_cadencia", False):
        try:
            from app.services import sugestao_cadencia as sug

            achado = sug.texto_aprovado(est.telefone, toque.indice)
            if achado:
                # `aplicar_assinatura` também aqui: a sugestão sobrescreve o
                # texto DEPOIS de ele já ter passado pela substituição, então um
                # `{assinatura}` digitado na aprovação sairia CRU pro lead.
                # Achado do Sol (29/07).
                sugestao_id, texto = achado[0], aplicar_assinatura(
                    achado[1], est.corretora_id
                )
                log.info("cadencia_usando_sugestao_ia",
                         toque=toque.indice, sugestao_id=sugestao_id)
        except Exception as e:  # noqa: BLE001
            log.warning("cadencia_sugestao_falhou_usando_template",
                        toque=toque.indice, erro=str(e)[:160])
            sugestao_id = None

    resultado = _chamar_sender(
        sender, est.telefone, texto, com_botao=com_botao,
        linha=linha, corretora_id=est.corretora_id,
        variacao=ordem_da_mensagem(toque.indice),
        # verbo do {{3}} do template com botão ("Para receber o áudio, é só
        # tocar no botão abaixo"). Sender de teste sem o kwarg segue intacto.
        cta=toque.cta,
        # 07/10: liga a trava de opt-out do emissor e carimba como robô.
        origem=ORIGEM_CADENCIA,
    )
    if _barrado_pelo_emissor(session, est, agora, resultado, res):
        return False

    # marca usada só DEPOIS do envio dar certo (o sender levanta se falha)
    if sugestao_id is not None:
        try:
            from app.services import sugestao_cadencia as sug

            sug.marcar_usada(sugestao_id)
        except Exception:  # noqa: BLE001
            pass  # best-effort: já enviou, não trava o fluxo

    _registrar_evento(
        session, est, toque=toque.indice, tipo=TIPO_MENSAGEM, canal=toque.canal,
        payload={
            "texto": texto,
            "sugestao_ia_id": sugestao_id,   # None = saiu o template padrão
            "com_botao": com_botao,
            # O que REALMENTE saiu: hoje só o toque da oferta de áudio tem
            # botão (Dona 05/08); o resto da régua segue no pool ola, sem.
            "com_botao_efetivo": bool(com_botao),
            "cta": toque.cta,
            "categoria": toque.categoria,
            "dry_run": bool(resultado.get("dry_run")),
            "sent": bool(resultado.get("sent")),
            "wamid": resultado.get("wamid"),
            "template": resultado.get("template"),
        },
        agora=agora,
    )
    return True


def _barrado_pelo_emissor(session: Session, est: CadenciaEstado, agora: datetime,
                          resultado: dict[str, Any] | None,
                          res: TickResult | None) -> bool:
    """A trava do emissor devolveu `erro="opt_out"` (o pedido chegou depois da
    leitura desta volta): tira o estado da régua com caminho `emissor`."""
    if (resultado or {}).get("erro") != "opt_out":
        return False
    opt_out_svc.tirar_da_regua(
        session, est, agora, [opt_out_svc.CAMINHO_EMISSOR], motor=ORIGEM_CADENCIA,
        res=res if res is not None else TickResult(),
        lead_id=_lead_do_estado(session, est),
    )
    return True


def _lead_do_estado(session: Session, est: CadenciaEstado) -> int | None:
    """Card canônico do telefone do estado (pelo sufixo-8), pro evento no card.
    Best-effort: sem card, o estado sai da régua do mesmo jeito."""
    suf = _suf8(est.telefone)
    if len(suf) < 8:
        return None
    try:
        with session.begin_nested():
            lead = (
                session.query(Lead)
                .filter(Lead.tenant_id == est.tenant_id, Lead.telefone.like(f"%{suf}"))
                .order_by(Lead.id.desc())
                .first()
            )
        return int(lead.id) if lead is not None else None
    except Exception:  # noqa: BLE001
        return None


def _tirar_da_regua_quem_pediu(session: Session, tenant_id: int, agora: datetime,
                               bloqueio: dict[str, list[str]], res: TickResult) -> None:
    """Todo estado da régua (ativa/remarketing/pausada) cujo telefone está no
    mapa vira `opt_out`, devido ou não. Commit por estado."""
    na_regua = (
        session.query(CadenciaEstado)
        .filter(CadenciaEstado.tenant_id == tenant_id,
                CadenciaEstado.status.in_(_STATUS_NA_REGUA))
        .all()
    )
    for est in na_regua:
        caminhos = bloqueio.get(_suf8(est.telefone))
        if not caminhos:
            continue
        opt_out_svc.tirar_da_regua(session, est, agora, caminhos, motor=ORIGEM_CADENCIA,
                                   res=res, lead_id=_lead_do_estado(session, est))
        session.commit()


def _registrar_falha_envio(
    session: Session,
    est: CadenciaEstado,
    toque: Toque | None,
    agora: datetime,
    erro: str,
) -> None:
    """Grava a FALHA de envio sem consumir o slot do toque.

    ATENÇÃO à UNIQUE uq_cadencia_eventos_toque (tenant, cadencia, toque): se o
    evento de falha gravasse o `toque`, o retry no próximo tick veria
    `_evento_ja_registrado` True e PULARIA o toque (lead nunca receberia). Por
    isso toque=None + tipo distinto; o índice do toque vai no payload.
    Best-effort: falha aqui não pode derrubar o tick.
    """
    try:
        with session.begin_nested():
            _registrar_evento(
                session, est, toque=None, tipo=TIPO_ENVIO_FALHOU, canal=CANAL_WHATSAPP,
                payload={
                    "toque_tentado": toque.indice if toque is not None else None,
                    "erro": erro[:300],
                },
                agora=agora,
            )
    except Exception as e:  # noqa: BLE001
        log.warning("cadencia_falha_evento_nao_gravado", cadencia_id=est.id, erro=str(e)[:120])


def _registrar_tarefa_ligacao(
    session: Session, est: CadenciaEstado, toque: Toque, agora: datetime
) -> None:
    # TODO(twenty): espelhar como card de tarefa no Twenty/Postgres (owner=corretora_id).
    _registrar_evento(
        session, est, toque=toque.indice, tipo=TIPO_TAREFA_LIGACAO, canal=toque.canal,
        payload={
            "acao": "ligar",
            "corretora_id": est.corretora_id,
            "lead_id": est.lead_id,
            "twenty_task": "TODO",
        },
        agora=agora,
    )


def marcar_ligacao_feita(
    session: Session,
    *,
    cadencia_id: int,
    toque_esperado: int | None = None,
    tenant_id: int = DEFAULT_TENANT_ID,
    agora: datetime | None = None,
) -> dict[str, Any]:
    """Marca o toque de LIGAÇÃO ATUAL como feito ("a Pessoa12 ligou") — o equivalente
    manual do que o tick faria: registra o cadencia_eventos(tipo='tarefa_ligacao')
    e AVANÇA o estado pro próximo toque. Idempotente por (tenant, cadencia, toque)
    via UNIQUE — chamar 2x não duplica evento nem pula toque.

    Usado pelo painel (botão "✓ liguei" no Chat / aba Tarefas). Só faz sentido
    quando o toque atual é de ligação; caso contrário devolve {"ok": False}.

    Args:
        toque_esperado: se dado, valida que o estado ainda está nesse toque
            (evita marcar o toque errado se a fila mudou entre render e clique).
    """
    agora = agora or _now()
    est = (
        session.query(CadenciaEstado)
        .filter(CadenciaEstado.id == cadencia_id, CadenciaEstado.tenant_id == tenant_id)
        .first()
    )
    if est is None:
        return {"ok": False, "motivo": "cadencia_nao_encontrada"}
    if est.status != "ativa":
        return {"ok": False, "motivo": f"status_{est.status}"}
    if est.toque_atual >= len(TOQUES):
        return {"ok": False, "motivo": "fora_dos_toques"}
    toque = TOQUES[est.toque_atual]
    if toque.tipo != TIPO_TAREFA_LIGACAO:
        return {"ok": False, "motivo": "toque_atual_nao_e_ligacao"}
    if toque_esperado is not None and toque_esperado != toque.indice:
        return {"ok": False, "motivo": "toque_mudou"}

    ja = _evento_ja_registrado(session, tenant_id, est.id, toque.indice)
    if not ja:
        _registrar_tarefa_ligacao(session, est, toque, agora)
    _avancar(session, est, agora)
    session.commit()
    log.info(
        "cadencia_ligacao_feita",
        cadencia_id=est.id,
        toque=toque.indice,
        telefone=est.telefone[-4:],
        ja_registrado=ja,
    )
    return {"ok": True, "cadencia_id": est.id, "toque_marcado": toque.indice}


def _avancar(session: Session, est: CadenciaEstado, agora: datetime) -> None:
    """Avança pro próximo toque e recalcula proximo_toque_em. Se estourou os 8,
    vira remarketing."""
    proximo = est.toque_atual + 1
    if proximo >= len(TOQUES):
        _virar_remarketing(session, est, agora)
        return
    # Espaça a partir de AGORA pelo INTERVALO entre os toques (não de criado_em).
    # Senão um lead "atrasado" (criado dias atrás) dispara vários toques no MESMO
    # dia correndo atrás do offset passado. Mínimo 1 dia entre toques.
    intervalo = max(1, TOQUES[proximo].offset_dias - TOQUES[est.toque_atual].offset_dias)
    est.toque_atual = proximo
    est.proximo_toque_em = agora + timedelta(days=intervalo)
    est.atualizado_em = agora
    session.flush()


def _proximo_devido(base: datetime, offset_dias: int) -> datetime:
    return base + timedelta(days=offset_dias)


def _virar_remarketing(session: Session, est: CadenciaEstado, agora: datetime) -> None:
    est.status = "remarketing"
    est.proximo_toque_em = agora + timedelta(days=settings.cadencia_cooldown_dias)
    est.atualizado_em = agora
    session.flush()
    log.info("cadencia_virou_remarketing", cadencia_id=est.id, telefone=est.telefone[-4:])


def _corretora_liberada(res: TickResult, est: CadenciaEstado) -> bool:
    """O interruptor da corretora dona deste estado (`cadencia_por_corretora`),
    lido do banco na hora. Falso = conta o motivo no tick e não envia."""
    from app.services import automacao_por_corretora as apc

    pode, motivo = apc.pode_enviar(apc.CHAVE_FOLLOWUP, est.corretora_id)
    if not pode:
        _bloqueio(res, motivo or "corretora_desligada")
        log.info("cadencia_corretora_segurada", cadencia_id=est.id,
                 corretora_id=est.corretora_id, motivo=motivo)
    return pode


def _processar_remarketing(
    session: Session,
    est: CadenciaEstado,
    agora: datetime,
    res: TickResult,
    sender: Callable[..., dict[str, Any]],
    *,
    linha: Any = None,
) -> None:
    tenant_id = est.tenant_id
    # cap POR LINHA (ver _cap_diario_atingido): o remarketing da Pessoa10 não come a
    # cota da Pessoa12.
    if _cap_diario_atingido(session, tenant_id, agora, corretora_id=est.corretora_id):
        _bloqueio(res, "cap_diario")
        return
    if time.monotonic() - _LAST_ENQUEUE["at"] < settings.cadencia_min_intervalo_s:
        _bloqueio(res, "muito_rapido")
        return
    if not _corretora_liberada(res, est):
        return

    # copy efetiva: override (toque=-1) quando existe, senão default do código.
    texto_rmk = aplicar_assinatura(
        copy_efetiva(session, TOQUE_REMARKETING, tenant_id=tenant_id),
        est.corretora_id,
    )
    resultado = _chamar_sender(
        sender, est.telefone, texto_rmk, com_botao=False,
        linha=linha, corretora_id=est.corretora_id,
        origem=ORIGEM_CADENCIA_REMARKETING,
    )
    if _barrado_pelo_emissor(session, est, agora, resultado, res):
        return
    # remarketing: toque=None (não bate no UNIQUE de toque) — 1 evento por ciclo de 30d.
    _registrar_evento(
        session, est, toque=None, tipo=TIPO_MENSAGEM, canal=CANAL_WHATSAPP,
        payload={
            "texto": texto_rmk,
            "com_botao": False,
            "categoria": "utility",
            "remarketing": True,
            "dry_run": bool(resultado.get("dry_run")),
            "sent": bool(resultado.get("sent")),
            "wamid": resultado.get("wamid"),
            "template": resultado.get("template"),
        },
        agora=agora,
    )
    _LAST_ENQUEUE["at"] = time.monotonic()
    est.proximo_toque_em = agora + timedelta(days=settings.cadencia_cooldown_dias)
    est.atualizado_em = agora
    session.flush()
    res.remarketing += 1
    res.detalhes.append({"telefone": est.telefone[-4:], "tipo": "remarketing"})


# ── Resposta do lead (inbound) — a cadência PARA e o CRM reage ────────────────
#
# Hook chamado pelo inbound da linha Pessoa12 (app/api/cloud_corretora_a.cloud_inbound)
# quando uma mensagem do lead é gravada. Best-effort SEMPRE: nenhuma exceção
# escapa (o inbound nunca cai por causa disso).

TIPO_RESPOSTA = "resposta"
STATUS_RESPONDIDA = "respondida"

# Terceira saída do toque de encerramento (régua V2, decisão Dona 04/08/2026):
# "1 continuar · 2 pausa 30 dias · 3 encerrar". Antes existiam só dois destinos
# — respondeu (vai pro atendimento) ou opt-out — e quem estava só num momento
# ruim sumia. Sumir era indistinguível de não ter interesse.
#
# "1" NÃO cai aqui de propósito: quem responde depois de 10 dias calado deu
# sinal de vida, e sinal de vida vai pro atendimento da corretora como qualquer
# resposta — não pro remarketing. (Correção da Dona, 04/08.)
# Índices simbólicos: a régua muda (o D3 de áudio entrou em 04/08 e deslocou
# todo mundo). Quem depende de "o último toque" tem que perguntar, não fixar o
# número — foi o que quebrou 9 testes na inserção do D3.
TOQUE_AUDIO = 3  # índice do toque que OFERECE o áudio (D3)

# "sim, manda" — o lead aceitando receber o áudio do toque D3. Aceita o que
# gente escreve de verdade; qualquer outra coisa segue o caminho normal de
# resposta (vai pro atendimento da corretora).
_RE_QUER_AUDIO = re.compile(
    r"\b(sim|quero|manda|pode mandar|pode|claro|bora|aceito|manda sim|qro)\b",
    re.IGNORECASE,
)


def _quer_o_audio(texto: str | None) -> bool:
    return bool(_RE_QUER_AUDIO.search((texto or "").strip()))


STATUS_JA_ENCONTROU = "ja_encontrou"
# corretora_id → slug da linha Cloud. Mesmo mapa que o resto do sistema usa.
_SLUG_POR_CORRETORA = {1: "corretor", 2: "corretor", 3: "corretor", 4: "corretor"}


def _slug_da_corretora(corretora_id: int | None) -> str | None:
    return _SLUG_POR_CORRETORA.get(int(corretora_id)) if corretora_id else None

# Id da etapa "Venda perdida". Nome diferente da constante de baixo, que é o
# RÓTULO da etapa e já existia — dois nomes iguais com valores diferentes é
# como o valor certo some sem ninguém ver.
STAGE_ID_VENDA_PERDIDA = 13


def _despedir_quem_ja_encontrou(session: Any, est: Any, texto: str,
                                agora: Any, *, velho: bool = False) -> bool:
    """Despede bem, pede indicação e encerra — sem passar pelo atendimento.

    Só vale pra quem está NA RÉGUA (status ativa/remarketing). Se a pessoa
    voltar a falar depois, cai no caminho normal de resposta e vai pra
    corretora — quem responde de novo tem assunto.

    `velho` (14/10/2026): mensagem reentregue com mais de 30 min de atraso
    encerra e move o card igual, mas a despedida NÃO sai — responder agora o
    que a pessoa escreveu há horas é a casa falando fora de hora.
    """
    # OPT-OUT GANHA SEMPRE. "não quero mais" cai nas duas regras, e a diferença
    # importa: opt-out é pedido explícito de parar, com peso legal e trava de
    # reenvio. Mandar despedida com oferta e programa de indicação pra quem
    # pediu pra sair é exatamente o oposto do que ele pediu.
    if is_opt_out(texto) or not _ja_encontrou(texto):
        return False
    if velho:
        log.warning("cadencia_ja_encontrou_velho_sem_despedida",
                    cadencia_id=est.id, tel=est.telefone[-4:])
    try:
        from app.api.cloud_corretora_a import enviar_cloud
        from app.services import cloud_lines as cl

        slug = _slug_da_corretora(est.corretora_id)
        linha = cl.line_by_slug(slug) if slug else None
        if linha is not None and not velho:
            enviar_cloud(est.telefone, DESPEDIDA_JA_ENCONTROU, linha=linha,
                         corretora_id=est.corretora_id,
                         instance=cl.instance_do_slug(slug))
    except Exception as e:  # noqa: BLE001 — a despedida falhar não pode
        log.warning("despedida_ja_encontrou_falhou", erro=str(e)[:140])

    est.status = STATUS_JA_ENCONTROU
    est.atualizado_em = agora
    try:
        session.execute(
            _sql_text(
                """UPDATE leads SET stage_id = :perdida, updated_at = NOW()
                    WHERE right(regexp_replace(coalesce(telefone,''),
                                               '\\D', '', 'g'), 8) = :suf"""
            ),
            {"perdida": STAGE_ID_VENDA_PERDIDA, "suf": _suf8(est.telefone)},
        )
    except Exception as e:  # noqa: BLE001
        log.warning("marcar_perdido_falhou", erro=str(e)[:140])
    log.info("cadencia_ja_encontrou", cadencia_id=est.id, tel=est.telefone[-4:])
    return True


TIPO_CLIQUE_BOTAO = "clique_botao"
# Tipos de mensagem da Cloud API que são CLIQUE, não texto digitado:
# `button` = botão de template (o nosso caso), `interactive` = botão/lista de
# mensagem interativa. Um bot de secretária eletrônica não clica em botão.
TIPOS_CLIQUE = ("button", "interactive")


def e_clique_de_botao(tipo_mensagem: str | None) -> bool:
    return (tipo_mensagem or "").lower() in TIPOS_CLIQUE


def _enviar_audio(est: Any) -> bool:
    """Manda o áudio da corretora dona pro lead. True se saiu.

    O áudio vai como mensagem de voz nativa (o .oga original, OGG/Opus):
    convertido, o WhatsApp trata como anexo e quase ninguém abre.
    """
    try:
        from app.api.cloud_corretora_a import enviar_cloud_midia
        from app.services.audios_corretora import MIME_OGG, caminho_audio

        caminho = caminho_audio(est.corretora_id)
        if caminho is None:
            return False
        # Pela linha da DONA (02/10/2026). Sem `linha` a mídia sai pelo 2591 da
        # Pessoa12 — certo pra cadência da Pessoa12 (que segue byte a byte igual), errado
        # pra da Pessoa10/Pessoa11: o toque com botão saiu pela linha delas, o clique
        # chega lá (agora que o inbound das corretoras também trata resposta),
        # e o áudio sairia de outro número, com a janela de 24h fechada.
        extra: dict[str, Any] = {}
        slug = _slug_da_corretora(est.corretora_id)
        if slug and slug != "corretor":
            from app.services import cloud_lines as cl

            linha = cl.line_by_slug(slug)
            if linha is None:
                log.warning("cadencia_audio_sem_linha_da_dona",
                            corretora_id=est.corretora_id, slug=slug)
                return False
            extra = {"linha": linha, "instance": cl.instance_do_slug(slug),
                     "corretora_id": est.corretora_id}
        enviar_cloud_midia(
            est.telefone, caminho.read_bytes(), mimetype=MIME_OGG,
            filename=caminho.name, tipo="audio", **extra,
        )
        log.info("cadencia_audio_enviado", cadencia_id=est.id,
                 corretora_id=est.corretora_id, tel=est.telefone[-4:])
        return True
    except Exception as e:  # noqa: BLE001 — falhar o áudio não pode perder a resposta
        log.warning("cadencia_audio_falhou", erro=str(e)[:140])
        return False


def _no_toque_do_botao(est: Any) -> bool:
    """O lead está parado logo DEPOIS do toque que ofereceu o áudio?

    `toque_atual` já aponta pro PRÓXIMO toque quando o anterior saiu — por isso
    a comparação é com `indice + 1`. O índice vem da régua (`toque_com_botao`),
    nunca escrito à mão.
    """
    t = toque_com_botao()
    return t is not None and est.toque_atual == t.indice + 1


def _tratar_clique_botao(
    session: Any, est: Any, irmas: list[Any], texto: str | None, agora: Any,
    *, velho: bool = False,
) -> str:
    """Lead CLICOU num botão do template. Clique NÃO é resposta.

    Regra da Dona (05/08): quem clica em "Saiba mais" pra receber o áudio não
    está começando conversa — está aceitando uma coisa. Se a gente parasse a
    cadência aqui (que é o que acontecia quando o "sim" vinha em texto), o lead
    saía da régua sem nunca ter falado com ninguém, e o D4 nunca chegava.

    Então: manda o áudio se ele estava no toque da oferta, registra o clique
    como evento próprio (pra medir) e o lead SEGUE na régua. Se ele falar
    QUALQUER COISA depois, aí sim vira atendimento da corretora, pelo caminho
    normal de resposta.

    `velho` (14/10/2026): clique reentregue com mais de 30 min de atraso é
    registrado, mas o áudio não sai.
    """
    audio = not velho and _no_toque_do_botao(est) and _enviar_audio(est)
    for e in irmas:
        _registrar_evento(
            session, e, toque=None, tipo=TIPO_CLIQUE_BOTAO, canal=CANAL_WHATSAPP,
            payload={
                "botao": (texto or "")[:80],
                "toque_atual": e.toque_atual,
                "audio_enviado": bool(audio) if e is est else False,
                **({"velho": True} if velho else {}),
            },
            agora=agora,
        )
    session.commit()
    log.info("cadencia_clique_botao", cadencia_id=est.id,
             telefone=est.telefone[-4:], audio_enviado=audio,
             toque_atual=est.toque_atual)
    return "clique_audio" if audio else "clique_botao"


def _mandar_audio_da_corretora(est: Any, texto: str, *, velho: bool = False) -> bool:
    """Manda o áudio da corretora dona, se o lead acabou de pedir.

    Só roda quando o lead está NO toque de oferta — fora dali um "sim" é
    resposta a outra coisa. E o áudio vai como mensagem de voz nativa (o .oga
    original, OGG/Opus): convertido, o WhatsApp trata como anexo e quase
    ninguém abre.

    A janela de 24h está aberta por definição: ele acabou de escrever.

    O caminho preferido hoje é o CLIQUE no botão (`_tratar_clique_botao`) — isto
    aqui cobre quem responde em texto mesmo assim ("sim, manda"). Nesse caso o
    lead FALOU, então segue o fluxo normal e vai pro atendimento da corretora.

    `velho` (14/10/2026): pedido reentregue com mais de 30 min de atraso não
    recebe o áudio.
    """
    if velho:
        return False
    if not _no_toque_do_botao(est) or not _quer_o_audio(texto):
        return False
    return _enviar_audio(est)


def toque_encerramento() -> int:
    """Índice do último toque — o do encerramento com 3 saídas."""
    return len(TOQUES) - 1


STATUS_PAUSADA = "pausada"
PAUSA_DIAS = 30

# Aceita "2", "2)", "opção 2", "pausar", "pausa"… O lead responde como quer, e
# exigir o dígito puro faria a pausa falhar justamente com quem escreve mais.
_RE_PAUSA = re.compile(
    r"^\s*(?:op(?:ç|c)[ãa]o\s*)?2\s*[\)\.\-]?\s*$|pausa(?:r)?", re.IGNORECASE
)


# ── "Já encontrei" — encerramento com pedido de indicação ────────────────────
#
# Decisão da Dona (05/08/2026), a partir das respostas reais de 30 dias: "tava
# procurando, mas já desisti" e "no momento já resolvemos" NÃO são opt-out — a
# pessoa não está brava, terminou a busca. Só que hoje ela continua na régua
# recebendo toque, e isso é o caminho mais curto pra alguém marcar como spam.
#
# Agora: a Assistente se despede bem, oferece a condição do mês e apresenta o
# programa de indicação (R$ 500 quando o indicado FECHA o pagamento do imóvel),
# e o lead sai como perdido — sem passar pelo atendimento da corretora.
#
# Se a pessoa CONTINUAR falando depois disso, aí sim vai pro atendimento: quem
# responde de novo tem assunto, e assunto é da corretora.
#
# Texto livre, sem template: ela acabou de escrever, então a janela de 24h está
# aberta — não custa marketing e não espera aprovação da Meta.

_RE_JA_ENCONTROU = re.compile(
    r"j[áa]\s*(encontrei|achei|comprei|resolv|consegui|fechei)"
    r"|j[áa]\s*(t|est)\w*\s*(resolvido|comprado)"
    r"|desisti"
    r"|n[ãa]o\s*(quero|preciso|estou|to|tô)\s*(mais|procurando)"
    r"|no momento\s*j[áa]",
    re.IGNORECASE,
)

DESPEDIDA_JA_ENCONTROU = (
    "Ahh que bom que você já encontrou! Fico feliz de verdade 💛\n\n"
    "Se um dia precisar de novo, é só me chamar aqui que eu te atendo na hora.\n\n"
    "Ah, e aproveitando: esse mês a gente está com uma condição exclusiva num "
    "dos empreendimentos — se quiser, te mando pra dar uma olhada, mesmo que "
    "seja só por curiosidade.\n\n"
    "E uma coisa que talvez você não saiba: a gente trabalha com indicação. Se "
    "você indicar alguém e essa pessoa comprar com a gente, você ganha R$ 500 "
    "de presente (pago quando a compra do imóvel é concluída).\n\n"
    "Você conhece alguém que esteja procurando imóvel agora?"
)


def _ja_encontrou(texto: str | None) -> bool:
    return bool(_RE_JA_ENCONTROU.search((texto or "").strip()))


def _pediu_pausa(texto: str | None) -> bool:
    return bool(_RE_PAUSA.search((texto or "").strip()))
STAGE_EM_ATENDIMENTO = "Em atendimento"
STAGE_VENDA_PERDIDA = "Venda perdida"

# Timer anti-secretária-eletrônica (regra Dona 10/07): inbound que chega em
# menos de N segundos depois do NOSSO envio é resposta automática (recado,
# auto-reply de operadora) — não conta como "respondeu", a cadência segue.
RESPOSTA_AUTO_REPLY_JANELA_S = 15  # auto-reply de empresa dispara ~5-10s depois; gente demora mais pra ler/responder reengajamento frio (era 5, deixou passar resposta em exatos 5s)

# Etapas das quais a resposta PODE mover o lead pra "Em atendimento" — mesma
# regra do fluxo antigo (disparos_reengajamento): não rebaixa etapa mais
# avançada (Visita marcada, Proposta...). "10000001" = stage criado no shadow
# pelo etapa_id do Follow-up do Kommo (KOMMO_STAGE_FOLLOWUP) sem nome resolvido.
_STAGES_MOVIVEIS = ("Novo", "Follow-up", "Primeiro Contato", "10000001")


def _suf8(telefone: str | None) -> str:
    """Últimos 8 dígitos — heurística de match entre formatos (Kommo grava
    '+5515...', Cloud manda wa_id '5515...', às vezes sem o 9)."""
    return re.sub(r"\D", "", telefone or "")[-8:]


def _envio_recente_demais(session: Session, est: CadenciaEstado, agora: datetime) -> bool:
    """True se o ÚLTIMO envio da cadência (cadencia_eventos tipo='mensagem' —
    é assim que o tick grava toque E remarketing) aconteceu há menos de
    `RESPOSTA_AUTO_REPLY_JANELA_S` segundos. Inbound nessa janela é
    secretária-eletrônica/auto-reply, não gente."""
    ultimo_envio = (
        session.query(func.max(CadenciaEvento.at))
        .filter(
            CadenciaEvento.tenant_id == est.tenant_id,
            CadenciaEvento.cadencia_id == est.id,
            CadenciaEvento.tipo == TIPO_MENSAGEM,
        )
        .scalar()
    )
    if ultimo_envio is None:
        return False
    if ultimo_envio.tzinfo is None:  # SQLite de teste devolve naive (UTC)
        ultimo_envio = ultimo_envio.replace(tzinfo=timezone.utc)
    return (agora - ultimo_envio).total_seconds() < RESPOSTA_AUTO_REPLY_JANELA_S


# Padrões de AUTO-REPLY por CONTEÚDO (secretária/away-message/intro de empresa).
# Mesmo FORA da janela de tempo, mensagem assim não é lead interessado — não
# promove. CONSERVADOR: só casa sinais claros de automação/comercial (na dúvida
# NÃO marca, pra nunca engolir resposta de gente).
_AUTO_REPLY_CONTEUDO_RE = re.compile(
    r"mensagem autom[\u00e1a]tica|resposta autom[\u00e1a]tica|"
    r"est(ou|amos) ausente|fora do (hor[\u00e1a]rio|expediente)|"
    r"hor[\u00e1a]rio de (atendimento|funcionamento|expediente)|"
    r"obrigad[oa] por (entrar em )?contat|agradecemos (o|seu|pelo) contat|"
    # Saudação automática do WhatsApp Business (05/10/2026). O texto de fábrica
    # do aplicativo é "<Empresa> agradece seu contato. Como podemos ajudar?", e
    # a outra metade das empresas troca por "Seja bem-vindo(a) à <Empresa>".
    # Medido nas 37.174 mensagens de texto recebidas em todas as linhas: 159
    # casam, nenhuma escrita por gente. Lead não dá boas-vindas a imobiliária.
    r"agradece\s+(o\s+|seu\s+|pelo\s+|pelo\s+seu\s+)contat|"
    r"sej[ao]m?\s+(muito\s+)?bem[\s-]*vind|"
    r"bem[\s-]*vind[oa]s?(\([ao]\))?\s+(ao|\u00e0|a|aos)\s|"
    r"recebemos (a sua|sua|seu|o seu) (mensagem|contato)|"
    r"retornaremos|responderemos (assim|em breve|o mais)|em breve retorn|"
    r"n[\u00e3a]o estou dispon[\u00edi]vel no momento|no momento (n[\u00e3a]o|est[ao])|"
    r"(sou|aqui [\u00e9e]) .{1,45}\bd[ao]s?\b.{0,45}"
    r"(ltda|equipamentos|com[\u00e9e]rcio|ind[\u00fau]stria|servi[\u00e7c]os|"
    r"solu[\u00e7c][\u00f5o]es|distribuidora|tecnologia|assist[\u00eae]ncia|"
    r"consult[\u00f3o]ria|grupo|group)|"
    r"protocolo (de atendimento|n[\u00ba\u00b0o]|:)|n[\u00fau]mero de protocolo",
    re.IGNORECASE,
)


def _e_auto_reply_conteudo(texto: str | None) -> bool:
    """True se o texto tem cara de auto-reply/away-message/intro de empresa
    (mesmo FORA da janela de tempo). Conservador — na dúvida, NÃO marca."""
    return bool(texto and _AUTO_REPLY_CONTEUDO_RE.search(texto))


def processar_resposta_cadencia(
    telefone: str,
    texto: str | None,
    *,
    tipo_mensagem: str | None = None,
    velho: bool = False,
    tenant_id: int = DEFAULT_TENANT_ID,
    session: Session | None = None,
    session_factory: Callable[[], Session] | None = None,
) -> str | None:
    """Lead RESPONDEU no WhatsApp → costura inbound × cadência × CRM canônico.

    1. Se o telefone tem `cadencia_estado` status 'ativa' OU 'remarketing':
       - inbound < RESPOSTA_AUTO_REPLY_JANELA_S segundos depois do nosso último
         envio → auto-reply (secretária eletrônica): IGNORA, cadência segue;
       - texto é opt-out (`is_opt_out`)  → status='opt_out' + card canônico
         move pra "Venda perdida" (regra Dona 10/07);
       - senão                            → status='respondida' + card move
         pra "Em atendimento" (sem rebaixar etapa avançada).
       Nos casos processados grava `cadencia_eventos(tipo='resposta')`.
    2. Moves na espinha rodam em SAVEPOINT (`begin_nested` best-effort): espinha
       indisponível degrada sem poisonar a transação.
    3. Aviso à corretora é SÓ in-app (badge 🟢 no board + fila Hoje) — aviso
       via WhatsApp foi aposentado (proibição Dona 10/07: nenhum template/
       mensagem gasto com aviso interno).

    `velho=True` (14/10/2026): o api-disparos marcou a mensagem com
    `X-Cloud-Velho: 1` (reentrega da Meta com mais de 30 min). Status, eventos e
    card andam igual, mas NADA sai pro lead: nem o áudio da corretora (clique ou
    texto), nem a despedida do "já encontrei".

    Best-effort: NUNCA levanta — retorna None em erro (só observabilidade).
    Retorna: "respondida" | "opt_out" | "auto_reply" | "sem_cadencia" | None.
    """
    try:
        suf = _suf8(telefone)
        if not suf:
            return "sem_cadencia"

        owns_session = session is None
        if owns_session:
            if session_factory is None:
                from app.db.session import SessionLocal

                session_factory = SessionLocal
            session = session_factory()

        try:
            agora = _now()
            vivas = (
                session.query(CadenciaEstado)
                .filter(
                    CadenciaEstado.tenant_id == tenant_id,
                    CadenciaEstado.status.in_(("ativa", "remarketing")),
                )
                .all()
            )
            # TODAS as vivas do sufixo, não só a primeira: enquanto existirem
            # cadências gêmeas do mesmo número (grafias '+55...' / '55...' do
            # seed legado), parar só uma deixava a irmã tocando depois do 'PARA'.
            irmas = [e for e in vivas if _suf8(e.telefone) == suf]
            if not irmas:
                return "sem_cadencia"
            est = irmas[0]

            # CLIQUE em botão do template (Dona 05/08): não é resposta.
            # Vem ANTES do guard de auto-reply de propósito — o clique chega
            # segundos depois do nosso envio (é pra isso que o botão existe) e
            # cairia na janela anti-secretária-eletrônica. Bot não clica.
            # "Parar mensagens" é a exceção: pedido de saída vale clicado do
            # mesmo jeito, então segue pro caminho de opt-out lá embaixo.
            if e_clique_de_botao(tipo_mensagem) and not is_opt_out(texto):
                return _tratar_clique_botao(session, est, irmas, texto, agora,
                                           velho=velho)

            # auto-reply: colado no envio (janela de tempo) OU com cara de
            # secretária/empresa (conteúdo) — não marca respondida, não move.
            auto_por = ("tempo" if _envio_recente_demais(session, est, agora)
                        else "conteudo" if _e_auto_reply_conteudo(texto) else None)
            if auto_por:
                log.info(
                    "cadencia_resposta_ignorada_auto_reply",
                    cadencia_id=est.id,
                    telefone=est.telefone[-4:],
                    motivo=auto_por,
                    janela_s=RESPOSTA_AUTO_REPLY_JANELA_S,
                )
                return "auto_reply"

            # "Já encontrei / já resolvi": despede com pedido de indicação e
            # encerra. NÃO vai pro atendimento — a pessoa não tem assunto, tem
            # conclusão. Se voltar a falar, o próximo inbound cai no fluxo
            # normal e aí sim vira atendimento da corretora.
            if _despedir_quem_ja_encontrou(session, est, texto, agora,
                                           velho=velho):
                for e in irmas[1:]:
                    e.status = STATUS_JA_ENCONTROU
                    e.atualizado_em = agora
                session.commit()
                return "ja_encontrou"

            # Pediu o áudio do D3: manda AGORA (janela aberta — ele acabou de
            # escrever) e segue o fluxo normal de resposta, que leva pro
            # atendimento da corretora.
            _mandar_audio_da_corretora(est, texto, velho=velho)

            # "Pausa por 30 dias" — a terceira saída do toque de encerramento.
            # Só vale pra quem está NO último toque: fora dali um "2" solto é
            # o lead respondendo outra coisa (quantidade de quartos, horário,
            # o que for), e virar pausa seria calar alguém que quer conversar.
            if _pediu_pausa(texto) and est.toque_atual >= len(TOQUES) - 1:
                volta_em = agora + timedelta(days=PAUSA_DIAS)
                for e in irmas:
                    e.status = STATUS_PAUSADA
                    e.proximo_toque_em = volta_em
                    e.atualizado_em = agora
                session.flush()
                for e in irmas:
                    _registrar_evento(session, e, toque=None, tipo=TIPO_RESPOSTA,
                                      canal=CANAL_WHATSAPP,
                                      payload={"acao": "pausa_30d",
                                               "volta_em": volta_em.isoformat()})
                session.commit()
                log.info("cadencia_pausada_a_pedido", cadencia_id=est.id,
                         telefone=est.telefone[-4:], volta_em=volta_em.isoformat())
                return "pausada"

            motivo = motivo_opt_out(texto)
            optou = motivo is not None
            # Auditoria (obra 10): desinteresse lido do texto grava a FRASE no
            # evento e no log — pedido explícito de parar grava só o motivo.
            frase = classificar_desinteresse(texto) if motivo == "desinteresse" else None
            novo_status = "opt_out" if optou else STATUS_RESPONDIDA
            for e in irmas:  # a gêmea também para
                e.status = novo_status
                e.atualizado_em = agora
            session.flush()

            for e in irmas:
                _registrar_evento(
                    session, e, toque=None, tipo=TIPO_RESPOSTA, canal=CANAL_WHATSAPP,
                    payload={
                        "texto": (texto or "")[:160],
                        "opt_out": optou,
                        "opt_out_motivo": motivo,
                        "opt_out_frase": frase,
                        "toque_atual": e.toque_atual,
                    },
                    agora=agora,
                )

            if optou:
                _mover_lead_opt_out(session, est, suf, texto, agora)
            else:
                _mover_lead_respondeu(session, est, suf, texto, agora)

            session.commit()
            log.info(
                "cadencia_resposta",
                cadencia_id=est.id,
                telefone=est.telefone[-4:],
                status=est.status,
                opt_out=optou,
                opt_out_motivo=motivo,
                opt_out_frase=frase,
            )

            return "opt_out" if optou else STATUS_RESPONDIDA
        finally:
            if owns_session:
                session.close()
    except Exception as e:  # noqa: BLE001 — nunca derruba o inbound
        log.warning("cadencia_resposta_erro", erro=str(e)[:160])
        return None


def _mover_lead_respondeu(
    session: Session,
    est: CadenciaEstado,
    suf: str,
    texto: str | None,
    agora: datetime,
) -> str | None:
    """Resposta normal → lead canônico move pra "Em atendimento" +
    `lead_events(tipo='respondeu')`. NÃO rebaixa etapa mais avançada
    (`_STAGES_MOVIVEIS`) — mas o evento é registrado sempre."""
    return _mover_lead_cadencia(
        session, est, suf, texto, agora,
        stage_destino=STAGE_EM_ATENDIMENTO,
        tipo_evento="respondeu",
        so_de_stages=_STAGES_MOVIVEIS,
    )


def _mover_lead_opt_out(
    session: Session,
    est: CadenciaEstado,
    suf: str,
    texto: str | None,
    agora: datetime,
) -> str | None:
    """Opt-out → lead canônico move pra "Venda perdida" (regra Dona 10/07) +
    `lead_events(tipo='opt_out')`. Única exceção: não rebaixa "Venda ganha"."""
    return _mover_lead_cadencia(
        session, est, suf, texto, agora,
        stage_destino=STAGE_VENDA_PERDIDA,
        tipo_evento="opt_out",
        nunca_de_stages=("Venda ganha",),
    )


def _mover_lead_cadencia(
    session: Session,
    est: CadenciaEstado,
    suf: str,
    texto: str | None,
    agora: datetime,
    *,
    stage_destino: str,
    tipo_evento: str,
    so_de_stages: tuple[str, ...] | None = None,
    nunca_de_stages: tuple[str, ...] = (),
) -> str | None:
    """Move o lead canônico (leads, tenant, match por sufixo do telefone) pra
    `stage_destino` + registra `lead_events(tipo=tipo_evento)`.

    - `so_de_stages`: se dado, só move QUANDO a etapa atual está na lista
      (guard "não rebaixa etapa avançada" do respondeu);
    - `nunca_de_stages`: etapas das quais NUNCA move (ex: "Venda ganha").
    O evento é registrado sempre, movendo ou não.

    Roda num SAVEPOINT (`begin_nested` best-effort): se as tabelas da espinha não
    existem (DB de teste enxuto) ou algo falhar, degrada sem poisonar a transação.
    Retorna o nome do lead canônico ou None.
    """
    from app.db.models import Lead, PipelineStage
    from app.services.lead_spine import _registrar_evento as _registrar_lead_event
    from app.services.lead_spine import _resolve_stage_id

    try:
        with session.begin_nested():
            leads = (
                session.query(Lead)
                .filter(Lead.tenant_id == est.tenant_id, Lead.telefone.isnot(None))
                .all()
            )
            # TODOS os cards do mesmo número (o CRM tem duplicatas do mesmo lead
            # em grafias diferentes): mover só o primeiro deixava o card gêmeo
            # em etapa errada — e, no opt-out, um card "vivo" de quem pediu pra
            # parar. O evento é registrado por card.
            alvos = [l for l in leads if _suf8(l.telefone) == suf]
            if not alvos:
                log.info(
                    "cadencia_resposta_sem_lead_canonico",
                    cadencia_id=est.id, telefone=est.telefone[-4:],
                )
                return None

            stage_id = _resolve_stage_id(session, est.tenant_id, None, stage_destino)
            for lead in alvos:
                stage_anterior = lead.stage_id
                mover = stage_id is not None and stage_anterior != stage_id
                if mover and stage_anterior is not None:
                    atual = session.get(PipelineStage, stage_anterior)
                    nome_atual = ((atual.nome if atual else None) or "").strip()
                    if nome_atual and nome_atual in nunca_de_stages:
                        mover = False
                    elif so_de_stages is not None and nome_atual and nome_atual not in so_de_stages:
                        mover = False  # não rebaixa etapa mais avançada
                if mover:
                    lead.stage_id = stage_id
                lead.updated_at = agora

                _registrar_lead_event(
                    session, est.tenant_id, lead.id, tipo_evento,
                    {
                        "origem": "cadencia",
                        "cadencia_id": est.id,
                        "movido": mover,
                        "stage_de": stage_anterior,
                        "stage_para": stage_id if mover else stage_anterior,
                        "texto": (texto or "")[:160],
                    },
                )
                log.info(
                    "cadencia_resposta_lead_movido" if mover else "cadencia_resposta_lead_mantido",
                    lead_id=lead.id, cadencia_id=est.id, evento=tipo_evento,
                    stage_de=stage_anterior, stage_para=lead.stage_id,
                )
            return alvos[0].nome
    except Exception as e:  # noqa: BLE001 — espinha indisponível não derruba o hook
        log.warning("cadencia_resposta_move_falha", cadencia_id=est.id, erro=str(e)[:160])
        return None


# ── Automação 24h — "Em atendimento" mudo → Follow-up (regra Dona 10/07) ──────
#
# Lead parado em "Em atendimento" cuja ÚLTIMA mensagem trocada (thread em
# mensagens_whatsapp, match por sufixo-8 do telefone) é NOSSA (from_corretora)
# e tem 24h+ → o lead está MUDO: volta pra Follow-up, que liga a cadência
# sozinho (auto-enroll do lead_spine, mesma flag/critérios do enroll-on-followup).
# Se o LEAD falou por último, quem deve resposta é a corretora — NÃO move.
#
# Roda anexado ao tick da cadência (scripts/cadencia_tick.py, de hora em hora)
# — SEM cron novo (P003). Atrás de settings.cadencia_auto_followup_24h
# (default False): mover pra Follow-up com auto-enroll dispara cadência REAL.
# Só leads da Pessoa12 (mesma identificação do enroll: corretoras.evolution_instance,
# owner_id aceita id interno OU kommo_user_id). Best-effort com cap de
# segurança (settings.cadencia_auto_followup_cap, default 30 moves/tick).

AUTO_FOLLOWUP_TIPO_EVENTO = "auto_followup_24h"
STAGE_FOLLOW_UP = "Follow-up"

# Última mensagem por telefone (sufixo-8) dos leads em "Em atendimento" da Pessoa12.
# Só devolve os MUDOS: último remetente = nós (from_corretora) e mais velho
# que :corte. Lead sem NENHUMA mensagem não qualifica (sem evidência de mudez).
_SQL_MUDOS = r"""
    WITH candidatos AS (
        SELECT l.id, l.nome, l.telefone,
               right(regexp_replace(l.telefone, '\D', '', 'g'), 8) AS p8
        FROM leads l
        JOIN pipeline_stages ps ON ps.id = l.stage_id
        WHERE l.tenant_id = :tenant_id
          AND btrim(coalesce(ps.nome, '')) = :stage_origem
          AND l.telefone IS NOT NULL
          AND l.owner_id IN :owners
    ),
    ult AS (
        SELECT DISTINCT ON (right(regexp_replace(m.contato_telefone, '\D', '', 'g'), 8))
               right(regexp_replace(m.contato_telefone, '\D', '', 'g'), 8) AS p8,
               m.from_corretora,
               m.timestamp_evolution
        FROM mensagens_whatsapp m
        WHERE m.contato_telefone IS NOT NULL
        ORDER BY right(regexp_replace(m.contato_telefone, '\D', '', 'g'), 8),
                 m.timestamp_evolution DESC
    )
    SELECT c.id AS lead_id, u.timestamp_evolution AS ultima_msg_at
    FROM candidatos c
    JOIN ult u USING (p8)
    WHERE u.from_corretora = TRUE
      AND u.timestamp_evolution < :corte
    ORDER BY u.timestamp_evolution ASC
"""

_SQL_CANDIDATOS_COUNT = r"""
    SELECT count(*)
    FROM leads l
    JOIN pipeline_stages ps ON ps.id = l.stage_id
    WHERE l.tenant_id = :tenant_id
      AND btrim(coalesce(ps.nome, '')) = :stage_origem
      AND l.telefone IS NOT NULL
      AND l.owner_id IN :owners
"""


@dataclass
class AutoFollowupResult:
    ativo: bool = False
    candidatos: int = 0        # leads em "Em atendimento" da Pessoa12 (universo varrido)
    mudos: int = 0             # quantos qualificaram (mudos 24h+)
    movidos: int = 0           # quantos foram DE FATO movidos pra Follow-up
    falhas: int = 0
    detalhes: list[dict[str, Any]] = field(default_factory=list)


def auto_followup_24h(
    agora: datetime | None = None,
    *,
    session: Session | None = None,
    session_factory: Callable[[], Session] | None = None,
    tenant_id: int = DEFAULT_TENANT_ID,
    cap: int | None = None,
) -> AutoFollowupResult:
    """Move leads MUDOS (24h+ sem resposta ao nosso último contato) de
    "Em atendimento" pra "Follow-up" + `lead_events(tipo='auto_followup_24h')`.
    Mover pra Follow-up liga a cadência (auto-enroll — mesma flag CADENCIA_AUTO_ENROLL).

    Best-effort SEMPRE: falha em 1 lead não derruba a varredura; falha geral
    não derruba o tick (o caller loga e segue). Requer Postgres (regexp no SQL).
    """
    from sqlalchemy import bindparam
    from sqlalchemy import text as sql_text

    res = AutoFollowupResult(ativo=bool(settings.cadencia_auto_followup_24h))
    if not res.ativo:
        log.info("auto_followup_24h_skip", motivo="flag_desligada")
        return res

    agora = agora or _now()
    cap = cap if cap is not None else int(settings.cadencia_auto_followup_cap)
    corte = agora - timedelta(hours=int(settings.cadencia_auto_followup_horas))

    owns_session = session is None
    if owns_session:
        if session_factory is None:
            from app.db.session import SessionLocal

            session_factory = SessionLocal
        session = session_factory()

    try:
        # Pessoa12 — mesma identificação do enroll-on-followup (NUNCA por nome).
        from app.api.cloud_corretora_a import INSTANCE as CORRETOR_INSTANCE
        from app.db.models import Corretora, Lead

        corretor = (
            session.query(Corretora)
            .filter(Corretora.evolution_instance == CORRETOR_INSTANCE, Corretora.ativo.is_(True))
            .first()
        )
        if corretor is None:
            log.info("auto_followup_24h_skip", motivo="corretor_nao_cadastrada")
            return res
        owners = [corretor.id]
        if corretor.kommo_user_id is not None:
            owners.append(int(corretor.kommo_user_id))

        params = {
            "tenant_id": tenant_id,
            "stage_origem": STAGE_EM_ATENDIMENTO,
            "owners": owners,
            "corte": corte,
        }
        res.candidatos = int(
            session.execute(
                sql_text(_SQL_CANDIDATOS_COUNT).bindparams(bindparam("owners", expanding=True)),
                params,
            ).scalar()
            or 0
        )
        mudos = session.execute(
            sql_text(_SQL_MUDOS).bindparams(bindparam("owners", expanding=True)), params
        ).mappings().all()
        res.mudos = len(mudos)

        if not mudos:
            log.info("auto_followup_24h_done", candidatos=res.candidatos, mudos=0, movidos=0)
            return res

        from app.services.lead_spine import _enroll_cadencia_followup, _resolve_stage_id
        from app.services.lead_spine import _registrar_evento as _registrar_lead_event

        followup_id = _resolve_stage_id(session, tenant_id, None, STAGE_FOLLOW_UP)
        if followup_id is None:
            log.warning("auto_followup_24h_abort", motivo="stage_followup_nao_resolvido")
            return res

        for row in mudos[:cap]:
            lead_id = row["lead_id"]
            ultima_msg_at = row["ultima_msg_at"]
            try:
                # move + evento num SAVEPOINT: falha degrada só esse lead.
                with session.begin_nested():
                    lead = session.get(Lead, lead_id)
                    if lead is None or lead.stage_id == followup_id:
                        continue
                    stage_de = lead.stage_id
                    lead.stage_id = followup_id
                    lead.updated_at = agora
                    _registrar_lead_event(
                        session, tenant_id, lead.id, AUTO_FOLLOWUP_TIPO_EVENTO,
                        {
                            "origem": "cadencia_tick",
                            "regra": f"mudo_{int(settings.cadencia_auto_followup_horas)}h",
                            "stage_de": stage_de,
                            "stage_para": followup_id,
                            "ultima_msg_at": ultima_msg_at.isoformat()
                            if ultima_msg_at is not None else None,
                        },
                    )
                res.movidos += 1
                res.detalhes.append({"lead_id": lead_id, "telefone": (lead.telefone or "")[-4:]})
                log.info(
                    "auto_followup_24h_movido",
                    lead_id=lead_id,
                    telefone=(lead.telefone or "")[-4:],
                    ultima_msg_at=str(ultima_msg_at),
                    stage_de=stage_de,
                    stage_para=followup_id,
                )
                # liga a cadência (auto-enroll — flag CADENCIA_AUTO_ENROLL,
                # idempotente). Best-effort: falha no enroll NÃO desfaz o move.
                try:
                    _enroll_cadencia_followup(session, lead, tenant_id)
                except Exception:
                    log.exception("auto_followup_24h_enroll_falhou", lead_id=lead_id)
            except Exception:
                res.falhas += 1
                log.exception("auto_followup_24h_lead_falhou", lead_id=lead_id)

        session.commit()
        log.info(
            "auto_followup_24h_done",
            candidatos=res.candidatos,
            mudos=res.mudos,
            movidos=res.movidos,
            falhas=res.falhas,
            cap=cap,
        )
        return res
    except Exception:
        log.exception("auto_followup_24h_erro")
        return res
    finally:
        if owns_session:
            session.close()
