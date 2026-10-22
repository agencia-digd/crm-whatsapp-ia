"""O que as telas das automações leem por corretora (06/10/2026).

Pedido da Dona: em cada automação, "com qual telefone tá ligado", se está ativo
o da Pessoa7, o da Pessoa11, o da Pessoa12, e o que saiu e falhou. Até aqui o servidor não
ligava corretora ↔ número (a tela juntava por rótulo) e só a tela do
remarketing tinha saúde de linha.

Tudo aqui é LEITURA. As regras de sempre:

- **Não consegui ler ≠ zero.** Cada bloco que depende de consulta vem com
  `indisponivel` + `motivo` quando falha; número nenhum nasce em 0 por omissão.
- **De onde vem cada número** está no nome do campo e no docstring da função:
  `saiu` = a Meta aceitou (evento com `sent=true`); `entregue`/`lido` = o status
  que o webhook da linha gravou pro wamid; `falhou` = recusa na hora (evento de
  falha) + entrega que voltou `failed`.
- **Hoje e semana** em data de São Paulo. "Semana" = os últimos 7 dias contando
  hoje (é a janela que a tela já usava, `dias=7`).
"""
from __future__ import annotations

import threading
import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import text as sql_text

log = structlog.get_logger()

TENANT = 1
SP = ZoneInfo("America/Sao_Paulo")
SEMANA_DIAS = 7

# ── Corretoras e linhas ──────────────────────────────────────────────────────


def hoje_sp(agora: datetime | None = None) -> date:
    return (agora or datetime.now(timezone.utc)).astimezone(SP).date()


def inicio_da_semana_utc(agora: datetime | None = None) -> datetime:
    """00:00 de São Paulo de 6 dias atrás, em UTC."""
    d = hoje_sp(agora) - timedelta(days=SEMANA_DIAS - 1)
    return datetime.combine(d, dtime(0, 0), tzinfo=SP).astimezone(timezone.utc)


def _slug_da_instance(instance: str | None) -> str | None:
    from app.services import cloud_lines as cl

    if not instance:
        return None
    return next((s for s, i in cl._INSTANCE_POR_SLUG.items() if i == instance), None)


def corretoras_ativas(session: Any) -> list[dict[str, Any]]:
    """Corretoras ativas do cadastro (papel `corretora*`), com o slug da linha
    pelo `evolution_instance`. Levanta se o banco falhar."""
    from app.api.painel import CORRETORA_LABEL

    linhas = session.execute(sql_text(
        "SELECT id, nome, evolution_instance FROM corretoras "
        " WHERE ativo = TRUE AND coalesce(papel,'corretora') LIKE 'corretora%' "
        " ORDER BY id"
    )).all()
    return [
        {"id": int(r[0]), "nome": str(r[1]),
         "label": CORRETORA_LABEL.get(int(r[0]), str(r[1]).split(" ")[0]),
         "instance": r[2], "slug": _slug_da_instance(r[2])}
        for r in linhas
    ]


_LINHAS_TTL_S = 60
_linhas_lock = threading.Lock()
_linhas_cache: tuple[float, dict[str, Any]] | None = None


def _saude_compacta(s: dict[str, Any] | None) -> dict[str, Any] | None:
    if not s:
        return None
    return {"cor": s.get("cor"), "qualidade": s.get("qualidade"),
            "status": s.get("status"), "lido_em": s.get("lido_em"),
            "velha": bool(s.get("velha")), "erro": s.get("erro")}


def todas_as_linhas(*, forcar: bool = False) -> dict[str, Any]:
    """Os números do cliente, um por slug, com dona, telefone, papel e saúde.

    Uma leitura só pra todas as telas, com cache de 60 s por cima dos que já
    existem (saúde da Meta: 5 min por linha; cadastro de números: sem cache).
    Fontes, cada uma com o seu "não consegui ler":
      - registro de linhas do copiloto (`cloud_lines`): slug → phone_number_id;
      - cadastro de corretoras: quem é a dona (`evolution_instance`);
      - api-disparos (`/numbers/papel`): telefone exibido e papel;
      - Graph da Meta (`saude_linha`): qualidade e estado;
      - mensagens gravadas (`linha_saude`): a linha está recusando por cobrança?
    """
    global _linhas_cache
    agora = time.monotonic()
    with _linhas_lock:
        if not forcar and _linhas_cache and agora - _linhas_cache[0] < _LINHAS_TTL_S:
            return _linhas_cache[1]
    dados = _ler_linhas()
    with _linhas_lock:
        _linhas_cache = (time.monotonic(), dados)
    return dados


def esquecer_linhas() -> None:
    """Zera o cache (testes e o botão de reler)."""
    global _linhas_cache
    with _linhas_lock:
        _linhas_cache = None


def _ler_linhas() -> dict[str, Any]:
    from app.db.session import SessionLocal
    from app.services import cloud_lines as cl
    from app.services import linha_disparadora, linha_saude, saude_linha

    erros: dict[str, str] = {}

    numeros: dict[str, dict[str, Any]] = {}
    try:
        for n in linha_disparadora._buscar_numeros():
            if isinstance(n, dict) and n.get("phone_number_id"):
                numeros[str(n["phone_number_id"])] = n
    except Exception as e:  # noqa: BLE001
        erros["cadastro_de_numeros"] = str(e)[:160]

    donas: dict[str, dict[str, Any]] = {}
    cobranca: dict[str, bool | None] = {}
    try:
        with SessionLocal() as s:
            for c in corretoras_ativas(s):
                if c["slug"]:
                    donas[c["slug"]] = c
            for slug, inst in cl._INSTANCE_POR_SLUG.items():
                try:
                    cobranca[slug] = linha_saude.bloqueada_por_cobranca(s, inst)
                except Exception:  # noqa: BLE001 — já é fail-open lá dentro
                    cobranca[slug] = None
    except Exception as e:  # noqa: BLE001
        erros["corretoras"] = str(e)[:160]

    linhas: list[dict[str, Any]] = []
    for slug, instance in cl._INSTANCE_POR_SLUG.items():
        reg = cl.line_by_slug(slug)
        pid = reg.phone_id if reg else None
        num = numeros.get(pid or "") or {}
        saude = _saude_compacta(saude_linha.ler(slug)) if reg else None
        dona = donas.get(slug)
        linhas.append({
            "slug": slug,
            "instance": instance,
            # Registrada = o copiloto tem o phone_number_id (env da linha) e
            # consegue mandar por ela. Não registrada = nada sai por aqui.
            "registrada": reg is not None,
            "phone_number_id": pid,
            "telefone": num.get("phone") or (saude_linha.ler(slug).get("numero") if reg else None),
            "nome": num.get("display_name"),
            "papel": num.get("papel"),
            "corretora_id": dona["id"] if dona else None,
            "corretora": dona["label"] if dona else None,
            "saude": saude,
            # True = a última notícia da linha é recusa de COBRANÇA da Meta
            # (WABA sem forma de pagamento): template sai 200 e volta failed.
            "cobranca_bloqueada": cobranca.get(slug),
        })
    return {
        "linhas": linhas,
        "lido_em": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "cache_s": _LINHAS_TTL_S,
        "erros": erros,
    }


def linha(slug: str | None) -> dict[str, Any] | None:
    """Uma linha de `todas_as_linhas()` (None = slug desconhecido/sem linha)."""
    if not slug:
        return None
    return next((l for l in todas_as_linhas()["linhas"] if l["slug"] == slug), None)


def linha_curta(slug: str | None) -> dict[str, Any] | None:
    """O que cabe numa célula: slug, número, cor e se está registrada."""
    l = linha(slug)
    if l is None:
        return None
    return {"slug": l["slug"], "telefone": l["telefone"],
            "registrada": l["registrada"],
            "cor": (l["saude"] or {}).get("cor") or "desconhecida",
            "saude": l["saude"], "cobranca_bloqueada": l["cobranca_bloqueada"]}


# ── O que saiu, entregou e falhou ────────────────────────────────────────────


_SQL_CADENCIA = """
SELECT coalesce(ce.corretora_id, :sem_dona) AS cid,
       (ev.at AT TIME ZONE 'America/Sao_Paulo')::date AS dia,
       ev.tipo,
       coalesce(ev.payload->>'sent', '') AS sent,
       m.status,
       (ev.payload->>'wamid') IS NOT NULL AS tem_wamid
  FROM cadencia_eventos ev
  JOIN cadencia_estado ce ON ce.id = ev.cadencia_id
  LEFT JOIN LATERAL (
        SELECT w.conteudo_metadata->>'delivery_status' AS status
          FROM mensagens_whatsapp w
         WHERE ev.payload->>'wamid' IS NOT NULL
           AND w.evolution_message_id = ev.payload->>'wamid'
         ORDER BY (w.conteudo_metadata ? 'delivery_status') DESC
         LIMIT 1
  ) m ON TRUE
 WHERE ev.tenant_id = :t
   AND ev.tipo IN ('mensagem', 'envio_falhou', 'telefone_invalido')
   AND ev.at >= :desde AND ev.at <= :ate
   AND (coalesce(ev.payload->>'nutricao', 'false') = 'true') = :nutricao
"""

_SQL_BOAS_VINDAS = """
SELECT (e.payload->>'corretora_id')::int AS cid,
       (e.at AT TIME ZONE 'America/Sao_Paulo')::date AS dia,
       e.tipo,
       coalesce(e.payload->>'sent', '') AS sent,
       m.status,
       (e.payload->>'wamid') IS NOT NULL AS tem_wamid
  FROM lead_events e
  LEFT JOIN LATERAL (
        SELECT w.conteudo_metadata->>'delivery_status' AS status
          FROM mensagens_whatsapp w
         WHERE e.payload->>'wamid' IS NOT NULL
           AND w.evolution_message_id = e.payload->>'wamid'
         ORDER BY (w.conteudo_metadata ? 'delivery_status') DESC
         LIMIT 1
  ) m ON TRUE
 WHERE e.tenant_id = :t
   AND e.tipo IN ('boas_vindas_enviada', 'boas_vindas_falhou')
   AND e.at >= :desde AND e.at <= :ate
"""


def _vazio() -> dict[str, int]:
    # sem_rastro = saiu, mas o evento não guardou o wamid: não dá pra saber se
    # entregou (boas-vindas antes de 06/10). Fica fora de entregue E de falhou.
    return {"saiu": 0, "entregue": 0, "lido": 0, "falhou": 0, "sem_rastro": 0}


def envios_por_corretora(session: Any, automacao: str, *,
                         agora: datetime | None = None,
                         desde: datetime | None = None,
                         ate: datetime | None = None) -> dict[int, dict[str, Any]]:
    """{corretora_id: {"hoje": {...}, "semana": {...}}} de uma automação.

    `automacao`: "follow-up" (cadência, sem a nutrição), "nutricao" ou
    "boas-vindas". Só conta envio real (`sent=true`); dry-run não entra.
    Levanta se a consulta falhar — quem chama diz "não consegui ler".
    """
    from app.services.automacao_por_corretora import CORRETORA_SEM_DONA

    hoje = hoje_sp(agora)
    desde = desde or inicio_da_semana_utc(agora)
    ate = ate or datetime.now(timezone.utc) + timedelta(days=1)
    if automacao == "boas-vindas":
        linhas = session.execute(sql_text(_SQL_BOAS_VINDAS),
                                 {"t": TENANT, "desde": desde, "ate": ate}).all()
        tipo_ok, tipo_falha = "boas_vindas_enviada", "boas_vindas_falhou"
    else:
        linhas = session.execute(sql_text(_SQL_CADENCIA), {
            "t": TENANT, "desde": desde, "ate": ate, "sem_dona": CORRETORA_SEM_DONA,
            "nutricao": automacao == "nutricao"}).all()
        tipo_ok, tipo_falha = "mensagem", "envio_falhou"

    out: dict[int, dict[str, Any]] = {}
    for cid, dia, tipo, sent, status, tem_wamid in linhas:
        if cid is None:
            continue
        alvo = out.setdefault(int(cid), {"hoje": _vazio(), "semana": _vazio(),
                                         "telefone_invalido_semana": 0})
        janelas = [alvo["semana"]] + ([alvo["hoje"]] if dia == hoje else [])
        for j in janelas:
            if tipo == tipo_ok and sent == "true":
                j["saiu"] += 1
                if not tem_wamid:
                    j["sem_rastro"] += 1
                if status in ("delivered", "read"):
                    j["entregue"] += 1
                if status == "read":
                    j["lido"] += 1
                if status == "failed":
                    j["falhou"] += 1
            elif tipo == tipo_falha:
                j["falhou"] += 1
        if tipo == "telefone_invalido":
            alvo["telefone_invalido_semana"] += 1
    return out


def entrega_da_janela(session: Any, automacao: str, dados: dict[str, Any]) -> dict[str, Any]:
    """Entregue e falha na MESMA janela de um endpoint de métricas (`dados`
    traz `desde`/`ate` em ISO). Campos pro `total` (PENDENCIAS-BACKEND item 8):
    `entregues`, `lidos`, `falhas`, `sem_rastro`; `None` + `entrega_indisponivel`
    = não consegui ler. Mesma régua do mapa (status do webhook pelo wamid)."""
    try:
        desde = datetime.fromisoformat(str(dados["desde"]))
        ate = datetime.fromisoformat(str(dados["ate"]))
        por = envios_por_corretora(session, automacao, desde=desde, ate=ate)
    except Exception as e:  # noqa: BLE001
        log.warning("painel_entrega_indisponivel", automacao=automacao, erro=str(e)[:140])
        try:
            session.rollback()
        except Exception:  # noqa: BLE001
            pass
        return {"entregues": None, "lidos": None, "falhas": None, "sem_rastro": None,
                "entrega_indisponivel": True}
    tot = _somar(por, "semana") or _vazio()
    return {"entregues": tot["entregue"], "lidos": tot["lido"], "falhas": tot["falhou"],
            "sem_rastro": tot["sem_rastro"], "entrega_indisponivel": False}


def _envios_ou_erro(session: Any, automacao: str) -> tuple[dict[int, Any] | None, str | None]:
    try:
        return envios_por_corretora(session, automacao), None
    except Exception as e:  # noqa: BLE001
        log.warning("painel_envios_indisponivel", automacao=automacao, erro=str(e)[:140])
        try:
            session.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None, str(e)[:160]


def _somar(envios: dict[int, Any] | None, janela: str) -> dict[str, int] | None:
    if envios is None:
        return None
    tot = _vazio()
    for v in envios.values():
        for k in tot:
            tot[k] += v[janela][k]
    return tot


# ── Roleta do Meta (boas-vindas) ─────────────────────────────────────────────


def validar_roleta(valor: Any, session: Any) -> list[int]:
    """Lista de corretoras na ordem da rotação. Só corretora ATIVA do cadastro
    (papel corretora*); sem repetida. Vazia é aceita aqui — quem chama exige a
    confirmação. Levanta `ValueError` com a frase pra tela."""
    if not isinstance(valor, list):
        raise ValueError("esperado uma lista de corretoras, na ordem")
    ids: list[int] = []
    for x in valor:
        if isinstance(x, bool):
            raise ValueError(f"corretora inválida: {x!r}")
        try:
            ids.append(int(x))
        except (TypeError, ValueError):
            raise ValueError(f"corretora inválida: {x!r}") from None
    if len(set(ids)) != len(ids):
        raise ValueError("corretora repetida na roleta")
    ativas = {c["id"] for c in corretoras_ativas(session)}
    fora = [i for i in ids if i not in ativas]
    if fora:
        raise ValueError(f"corretora inexistente ou inativa na roleta: {fora}")
    return ids


def roleta(session: Any, *, agora: datetime | None = None) -> dict[str, Any]:
    """A roleta do Meta como o intake vai usar no PRÓXIMO lead.

    `gravada` = o que está no banco (ou o padrão do .env); `valendo` = as que
    o intake aceita de fato (ativas); `vez` = quem recebe o próximo lead novo
    (mesma conta do intake, sem gravar nada). Por corretora: quantos leads a
    roleta deu em 7 e 30 dias (evento `intake` com a dona), se a boas-vindas
    sai pra ela (precisa de linha Cloud) e a saúde dessa linha.
    """
    from app.services import config_operacional
    from app.services import lead_intake as li

    agora = agora or datetime.now(timezone.utc)
    gravada = list(li.roleta_configurada())
    valendo = list(li.corretoras_roleta_validas(session))
    vez = li.vez_da_roleta(session, TENANT, tuple(valendo))

    recebeu: dict[int, dict[str, int]] = {}
    recebeu_erro: str | None = None
    try:
        for cid, d7, d30 in session.execute(sql_text(
            "SELECT (payload->>'corretora_id')::int, "
            "       count(*) FILTER (WHERE at >= :d7), count(*) "
            "  FROM lead_events "
            " WHERE tenant_id = :t AND tipo = 'intake' AND at >= :d30 "
            "   AND payload->>'corretora_id' IS NOT NULL "
            " GROUP BY 1"
        ), {"t": TENANT, "d7": agora - timedelta(days=7),
            "d30": agora - timedelta(days=30)}).all():
            recebeu[int(cid)] = {"7d": int(d7), "30d": int(d30)}
    except Exception as e:  # noqa: BLE001
        recebeu_erro = str(e)[:160]
        session.rollback()

    corretoras = []
    for c in corretoras_ativas(session):
        cid = c["id"]
        l = linha_curta(c["slug"])
        corretoras.append({
            "corretora_id": cid, "nome": c["nome"], "label": c["label"],
            "na_roleta": cid in valendo,
            "posicao": (valendo.index(cid) + 1) if cid in valendo else None,
            "e_a_vez": cid == vez,
            "recebeu_7d": None if recebeu_erro else recebeu.get(cid, {}).get("7d", 0),
            "recebeu_30d": None if recebeu_erro else recebeu.get(cid, {}).get("30d", 0),
            # Sem linha Cloud registrada a corretora RECEBE o lead mas a
            # boas-vindas não sai (fail-closed em `boas_vindas_habilitada`).
            "boas_vindas_sai": bool(l and l["registrada"]),
            "linha": l,
        })
    ignoradas = [i for i in gravada if i not in valendo]

    try:
        historico = config_operacional.historico(["roleta_corretoras"], limite=10)
        historico_erro = None
    except Exception as e:  # noqa: BLE001
        historico, historico_erro = None, str(e)[:160]

    aviso = None
    if not valendo:
        aviso = ("roleta vazia: lead novo do Meta entra SEM dona e ninguém recebe "
                 "a boas-vindas até alguém pôr uma corretora na roleta")
    elif ignoradas:
        aviso = f"corretoras gravadas mas inativas, puladas pela roleta: {ignoradas}"
    return {
        "gravada": gravada,
        "valendo": valendo,
        "vez": vez,
        "vazia": not valendo,
        "aviso": aviso,
        "ignoradas": ignoradas,
        "corretoras": corretoras,
        "recebeu_indisponivel": recebeu_erro is not None,
        "recebeu_motivo": recebeu_erro,
        "historico": historico,
        "historico_indisponivel": historico_erro is not None,
        "chave": "roleta_corretoras",
        "regra_rmkt": ("card com a tag RMKT nunca passa por esta roleta: vai pra "
                       "roleta do remarketing (ou fica sem dona)"),
    }


# ── Blocos por corretora: follow-up e nutrição ───────────────────────────────


def _interruptor_geral(chave: str) -> dict[str, Any]:
    from app.services import config_operacional

    try:
        return {"ligado": bool(config_operacional.get(chave)), "indisponivel": False}
    except config_operacional.ConfigIndisponivel:
        return {"ligado": None, "indisponivel": True}


def bloco_follow_up(session: Any) -> dict[str, Any]:
    """Follow-up por corretora: interruptor (o dela e o geral), por qual linha
    sai, quantos estão na régua e o que saiu/falhou hoje e na semana."""
    from app.services import automacao_por_corretora as apc
    from app.services.cadencia import _CORRETORA_CORRETOR

    geral = _interruptor_geral("cadencia_ativa")
    est = apc.estado(apc.CHAVE_FOLLOWUP)
    envios, envios_erro = _envios_ou_erro(session, "follow-up")

    regua: dict[int, dict[str, int]] = {}
    regua_erro = None
    try:
        for cid, ativas, devidos in session.execute(sql_text(
            "SELECT coalesce(corretora_id, :g), count(*), "
            "       count(*) FILTER (WHERE proximo_toque_em <= now()) "
            "  FROM cadencia_estado WHERE tenant_id = :t AND status = 'ativa' "
            " GROUP BY 1"), {"t": TENANT, "g": _CORRETORA_CORRETOR}).all():
            regua[int(cid)] = {"ativas": int(ativas), "devidas": int(devidos)}
    except Exception as e:  # noqa: BLE001
        regua_erro = str(e)[:160]
        session.rollback()

    corretoras = []
    for c in corretoras_ativas(session):
        cid = c["id"]
        propria = apc.ligada_na_tela(est, cid)
        e = (envios or {}).get(cid)
        corretoras.append({
            "corretora_id": cid, "nome": c["nome"], "label": c["label"],
            "ligada": propria,
            # O que vale de fato: o geral desligado vence.
            "ligada_efetiva": (None if propria is None or geral["ligado"] is None
                               else bool(propria and geral["ligado"])),
            # A cadência sai pela linha Cloud da DONA; sem linha, não sai nada
            # pra ela (bloqueio `sem_linha_cloud` no tick).
            "linha": linha_curta(c["slug"]),
            "sai_por": c["slug"],
            "na_regua": None if regua_erro else regua.get(cid, {}).get("ativas", 0),
            "devidas_agora": None if regua_erro else regua.get(cid, {}).get("devidas", 0),
            "hoje": None if envios is None else (e or {}).get("hoje", _vazio()),
            "semana": None if envios is None else (e or {}).get("semana", _vazio()),
        })
    return {
        "geral": {**geral, "chave": "cadencia_ativa"},
        "chave": apc.CHAVE_FOLLOWUP,
        "interruptor": est,
        "corretoras": corretoras,
        "total_hoje": _somar(envios, "hoje"),
        "total_semana": _somar(envios, "semana"),
        "envios_indisponivel": envios_erro is not None,
        "envios_motivo": envios_erro,
        "regua_indisponivel": regua_erro is not None,
        "regua_motivo": regua_erro,
        "sem_dona": "estado de régua sem dona gravada sai pela linha da Pessoa12 e conta na Pessoa12",
        "definicao": ("saiu = a Meta aceitou; entregue/lido = status do webhook "
                      "da linha; falhou = recusa na hora + entrega que voltou "
                      "failed; sem_rastro = saiu sem wamid gravado (não dá pra "
                      "cruzar). Semana = últimos 7 dias (São Paulo)."),
    }


def bloco_nutricao(session: Any) -> dict[str, Any]:
    """Nutrição por corretora (dona do lead): interruptor, linha REAL de saída
    (a da dona ou a da Pessoa12, com o motivo — mesma conta do motor,
    `nutricao.saida_do_marco`), régua, marcos e falhas, telefones inválidos."""
    from app.services import automacao_por_corretora as apc
    from app.services import nutricao as nut

    geral = _interruptor_geral("nutricao_ativa")
    est = apc.estado(apc.CHAVE_NUTRICAO)
    envios, envios_erro = _envios_ou_erro(session, "nutricao")

    regua: dict[int, dict[str, int]] = {}
    invalidos: dict[str, int] = {}
    regua_erro = None
    try:
        for cid, na, devidos, fora in session.execute(sql_text(
            "SELECT coalesce(corretora_id, :g), "
            "       count(*) FILTER (WHERE proximo_toque_em IS NOT NULL), "
            "       count(*) FILTER (WHERE proximo_toque_em <= now()), "
            "       count(*) FILTER (WHERE regexp_replace(telefone, '\\D', '', 'g') "
            "                         !~ '^55[0-9]{10,11}$') "
            "  FROM cadencia_estado WHERE tenant_id = :t AND status = 'remarketing' "
            " GROUP BY 1"), {"t": TENANT, "g": apc.CORRETORA_SEM_DONA}).all():
            regua[int(cid)] = {"na_regua": int(na), "devidos": int(devidos),
                               "fora_do_padrao": int(fora)}
        marcados = session.execute(sql_text(
            "SELECT count(*) FROM cadencia_estado WHERE tenant_id = :t AND status = :s"
        ), {"t": TENANT, "s": nut.STATUS_TELEFONE_INVALIDO}).scalar() or 0
        invalidos = {
            "marcados": int(marcados),
            "ainda_na_regua": sum(v["fora_do_padrao"] for v in regua.values()),
        }
    except Exception as e:  # noqa: BLE001
        regua_erro = str(e)[:160]
        session.rollback()

    linha_saida = linha_curta(nut.LINHA_DA_NUTRICAO)
    pela_dona, chave_problema = nut.pela_linha_da_dona()
    corretoras = []
    for c in corretoras_ativas(session):
        cid = c["id"]
        propria = apc.ligada_na_tela(est, cid)
        e = (envios or {}).get(cid)
        r = regua.get(cid, {})
        # A MESMA conta que o motor faz antes de cada marco (07/10).
        saida = nut.saida_do_marco(cid, session=session)
        corretoras.append({
            "corretora_id": cid, "nome": c["nome"], "label": c["label"],
            "ligada": propria,
            "ligada_efetiva": (None if propria is None or geral["ligado"] is None
                               else bool(propria and geral["ligado"])),
            # Por onde o marco do lead DELA sai agora, e por quê.
            "sai_por": saida.slug,
            "linha": linha_curta(saida.slug),
            "motivo_linha": saida.motivo,
            "motivo_linha_texto": nut.MOTIVO_LINHA_EM_PORTUGUES.get(saida.motivo, saida.motivo),
            "linha_da_corretora": c["slug"],
            "na_regua": None if regua_erro else r.get("na_regua", 0),
            "devidos_agora": None if regua_erro else r.get("devidos", 0),
            "telefone_fora_do_padrao": None if regua_erro else r.get("fora_do_padrao", 0),
            "hoje": None if envios is None else (e or {}).get("hoje", _vazio()),
            "semana": None if envios is None else (e or {}).get("semana", _vazio()),
        })
    saidas = {c["sai_por"] for c in corretoras}
    toda_pela_corretor = saidas <= {nut.LINHA_DA_NUTRICAO}
    return {
        "geral": {**geral, "chave": "nutricao_ativa"},
        "chave": apc.CHAVE_NUTRICAO,
        "interruptor": est,
        # `nutricao_pela_linha_da_dona` (07/10): ligada = cada marco sai pela
        # linha da dona quando ela serve; desligada = tudo pela Pessoa12.
        "pela_linha_da_dona": {"chave": nut.CHAVE_LINHA_DA_DONA, "ligada": pela_dona,
                               "problema": chave_problema},
        # "corretor" quando TODA corretora sai pela Pessoa12; senão "dona" (ver por corretora).
        "sai_por": nut.LINHA_DA_NUTRICAO if toda_pela_corretor else "dona",
        "toda_pela_corretor": toda_pela_corretor,
        # Linha de reserva (pra onde cai o marco quando a da dona não serve).
        "linha": linha_saida,
        "por_qual_linha": (
            "todo marco sai pela linha da Pessoa12, inclusive de lead das outras corretoras"
            + ("" if pela_dona else " (nutrição pela linha da dona desligada)")
            if toda_pela_corretor else
            "cada marco sai pela linha da corretora dona do lead quando ela tem "
            "linha e modelo aprovado na WABA dela; senão, pela da Pessoa12"),
        "corretoras": corretoras,
        "telefone_invalido": None if regua_erro else invalidos,
        "total_hoje": _somar(envios, "hoje"),
        "total_semana": _somar(envios, "semana"),
        "envios_indisponivel": envios_erro is not None,
        "envios_motivo": envios_erro,
        "regua_indisponivel": regua_erro is not None,
        "regua_motivo": regua_erro,
        "definicao": ("saiu = marco aceito pela Meta; falhou = recusa na hora "
                      "(o marco é adiado 1 dia) + entrega que voltou failed. "
                      "Telefone fora do padrão (55 + 10 ou 11 dígitos) sai da "
                      "régua como 'telefone_invalido'."),
    }


def _linhas_distintas(linhas: list[Any]) -> list[Any]:
    vistas: set[Any] = set()
    out = []
    for x in linhas:
        k = (x or {}).get("slug") if isinstance(x, dict) else x
        if x and k not in vistas:
            vistas.add(k)
            out.append(x)
    return out


def bloco_boas_vindas(session: Any) -> dict[str, Any]:
    """Boas-vindas por corretora: linha de saída (a da corretora que recebeu o
    lead), saúde e o que saiu/falhou hoje e na semana. A roleta vai à parte."""
    envios, envios_erro = _envios_ou_erro(session, "boas-vindas")
    corretoras = []
    for c in corretoras_ativas(session):
        e = (envios or {}).get(c["id"])
        corretoras.append({
            "corretora_id": c["id"], "label": c["label"],
            "sai_por": c["slug"], "linha": linha_curta(c["slug"]),
            "hoje": None if envios is None else (e or {}).get("hoje", _vazio()),
            "semana": None if envios is None else (e or {}).get("semana", _vazio()),
        })
    # Quem está em "Novo" agora (por dona) e quantos nunca receberam a
    # boas-vindas. A boas-vindas não é fila — sai no cadastro —, então "em Novo
    # sem boas-vindas" é o lead que não pôde receber: dona sem linha, sem dona,
    # envio que falhou, ou adotado de outro jeito.
    em_novo: dict[str, Any] | None = None
    em_novo_erro: str | None = None
    try:
        from app.services.lead_intake import STAGE_ENTRADA

        linhas = session.execute(sql_text(
            "SELECT l.owner_id, count(*), "
            "       count(*) FILTER (WHERE NOT EXISTS ("
            "           SELECT 1 FROM lead_events e WHERE e.lead_id = l.id "
            "              AND e.tipo = 'boas_vindas_enviada')) "
            "  FROM leads l JOIN pipeline_stages ps ON ps.id = l.stage_id "
            " WHERE l.tenant_id = :t AND ps.nome = :novo "
            "   AND coalesce(ps.funil, 'principal') = 'principal' "
            " GROUP BY 1"), {"t": TENANT, "novo": STAGE_ENTRADA}).all()
        em_novo = {
            "total": sum(int(r[1]) for r in linhas),
            "sem_boas_vindas": sum(int(r[2]) for r in linhas),
            "sem_dona": sum(int(r[1]) for r in linhas if r[0] is None),
            "por_corretora": {str(r[0]): {"total": int(r[1]), "sem_boas_vindas": int(r[2])}
                              for r in linhas if r[0] is not None},
        }
    except Exception as e:  # noqa: BLE001
        em_novo_erro = str(e)[:160]
        session.rollback()
    return {
        "corretoras": corretoras,
        "em_novo": em_novo,
        "em_novo_indisponivel": em_novo_erro is not None,
        "em_novo_motivo": em_novo_erro,
        "total_hoje": _somar(envios, "hoje"),
        "total_semana": _somar(envios, "semana"),
        "envios_indisponivel": envios_erro is not None,
        "envios_motivo": envios_erro,
        "por_qual_linha": ("sai pela linha Cloud da corretora que a roleta "
                           "escolheu; sem linha registrada, ela recebe o lead e "
                           "a boas-vindas não sai"),
    }


# ── Lista: uma olhada em cada automação ──────────────────────────────────────


def _estado(ligada: bool | None, parada: str | None, agiu_hoje: bool | None) -> str:
    if ligada is None:
        return "indisponivel"
    if not ligada:
        return "desligada"
    if parada:
        return "parada"
    return "agindo" if agiu_hoje else "ligada"


def resumo_da_lista(mapa: dict[str, Any]) -> list[dict[str, Any]]:
    """Cada automação numa linha: estado, motivo, linhas, número do dia, hora
    da última ação.

    Estados (nessa ordem de precedência):
      indisponivel  não consegui ler o interruptor;
      desligada     interruptor desligado (geral);
      parada        ligada, mas algo segura o envio agora (motivo em 1 frase);
      agindo        ligada e saiu mensagem real HOJE;
      ligada        ligada, nada segurando, nada saiu hoje ainda.
    `ultima_acao_em` = último envio REAL (`saude_envio`), não a última volta.
    """
    from app.services import dia_util

    saude = mapa.get("saude_envio") or {}
    fim_de_semana = dia_util.trava_ligada() and not dia_util.e_dia_util(None)
    motivo_fds = "fim de semana: segura até segunda" if fim_de_semana else None
    out: list[dict[str, Any]] = []

    # Boas-vindas
    bv = mapa.get("boas_vindas") or {}
    rol = bv.get("roleta") or {}
    bv_por = bv.get("envios") or {}
    bv_hoje = (bv_por.get("total_hoje") or {}).get("saiu") if bv_por.get("total_hoje") else None
    parada = None
    if rol.get("vazia"):
        parada = "roleta vazia: lead novo fica sem dona"
    elif rol and not any(c.get("boas_vindas_sai") for c in rol.get("corretoras", [])
                         if c.get("na_roleta")):
        parada = "nenhuma corretora da roleta tem linha registrada"
    ligada_bv = None if bv.get("estado") is None else bv.get("estado") == "ligado"
    out.append({
        "id": "boas-vindas", "nome": "Boas-vindas",
        "estado": _estado(ligada_bv, parada, (bv_hoje or 0) > 0 if bv_hoje is not None else None),
        "motivo": parada or (None if ligada_bv else "interruptor desligado"),
        "linhas": [c["linha"] for c in rol.get("corretoras", [])
                   if c.get("na_roleta") and c.get("linha")],
        "numero_do_dia": {"rotulo": "enviadas hoje", "valor": bv_hoje},
        "ultima_acao_em": (saude.get("boas-vindas") or {}).get("ultimo_envio_real"),
    })

    # Follow-up
    fu = mapa.get("follow_up") or {}
    geral = (fu.get("geral") or {}).get("ligado")
    corrs = fu.get("corretoras") or []
    fu_hoje = (fu.get("total_hoje") or {}).get("saiu") if fu.get("total_hoje") else None
    parada = motivo_fds
    if (fu.get("interruptor") or {}).get("ilegivel"):
        parada = "interruptor por corretora ilegível: segurando todas"
    elif (fu.get("interruptor") or {}).get("indisponivel"):
        parada = "não consegui ler o interruptor por corretora: segurando"
    elif corrs and not any(c.get("ligada") for c in corrs):
        parada = "desligada pra todas as corretoras"
    out.append({
        "id": "follow-up", "nome": "Follow-up",
        "estado": _estado(geral, parada, (fu_hoje or 0) > 0 if fu_hoje is not None else None),
        "motivo": parada or (None if geral else "interruptor desligado"),
        "linhas": [c["linha"] for c in corrs if c.get("ligada") and c.get("linha")],
        "numero_do_dia": {"rotulo": "mensagens hoje", "valor": fu_hoje},
        "ultima_acao_em": (saude.get("follow-up") or {}).get("ultimo_envio_real"),
    })

    # Remarketing
    rm = mapa.get("carrossel_corretor") or {}
    est_rm = rm.get("estado")
    ligada_rm = None if est_rm == "indisponivel" else est_rm == "ligado"
    uv = rm.get("ultima_volta") or {}
    parada = None
    if ligada_rm and uv.get("motivo_parada"):
        parada = uv.get("motivo_texto") or uv.get("motivo_parada")
    disp_linha = (rm.get("linha_disparadora") or {}).get("slug")
    rm_hoje = (rm.get("ritmo") or {}).get("disparos_hoje")
    out.append({
        "id": "remarketing", "nome": "Remarketing",
        "estado": _estado(ligada_rm, parada, (rm_hoje or 0) > 0 if rm_hoje is not None else None),
        "motivo": parada or (None if ligada_rm else
                             ("não consegui ler o interruptor" if ligada_rm is None
                              else "interruptor desligado")),
        "linhas": [x for x in [linha_curta(disp_linha)] if x],
        "numero_do_dia": {"rotulo": "disparos hoje", "valor": rm_hoje},
        "ultima_acao_em": (saude.get("remarketing") or {}).get("ultimo_envio_real"),
    })

    # Nutrição
    nu = mapa.get("nutricao") or {}
    pc = nu.get("por_corretora") or {}
    geral = (pc.get("geral") or {}).get("ligado")
    nu_hoje = (pc.get("total_hoje") or {}).get("saiu") if pc.get("total_hoje") else None
    parada = motivo_fds
    if (pc.get("interruptor") or {}).get("ilegivel"):
        parada = "interruptor por corretora ilegível: segurando todas"
    elif (pc.get("interruptor") or {}).get("indisponivel"):
        parada = "não consegui ler o interruptor por corretora: segurando"
    elif pc.get("corretoras") and not any(c.get("ligada") for c in pc["corretoras"]):
        parada = "desligada pra todas as corretoras"
    out.append({
        "id": "nutricao", "nome": "Nutrição",
        "estado": _estado(geral, parada, (nu_hoje or 0) > 0 if nu_hoje is not None else None),
        "motivo": parada or (None if geral else "interruptor desligado"),
        # As linhas por onde os marcos saem de fato (uma por saída distinta).
        "linhas": _linhas_distintas(
            [c.get("linha") for c in pc.get("corretoras") or [] if c.get("ligada")]
        ) or [x for x in [pc.get("linha")] if x],
        "numero_do_dia": {"rotulo": "marcos hoje", "valor": nu_hoje},
        "ultima_acao_em": (saude.get("nutricao") or {}).get("ultimo_envio_real"),
    })
    return out
