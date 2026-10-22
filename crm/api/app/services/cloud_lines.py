"""Registry de linhas WhatsApp Cloud API operadas pelo copiloto.

Cada "linha" é um número em coexistência Cloud (o número recebe/envia pelo
canal OFICIAL da Meta, não pelo Baileys/Evolution — que apanha de ban). O
registry mapeia `phone_number_id` → metadados da linha (slug, papel, arquivo de
credencial, nome da var do token, nome da var de dry-run).

Duas linhas hoje:
  - `corretor`   → 2591 (Pessoa Pessoa9 Acme / Pessoa3), token na review.env.
  - `assistente` → 2955 (Assistente/Assistente, canal responsivo), token na assistente-cloudapi.env.

Regras deste módulo:
  - NÃO lê valores de credencial na importação. A leitura do env-file é LAZY
    (só quando `token_for` é chamado) e TOLERANTE a ausência do arquivo — em
    teste/worktree os arquivos de /etc/agencia podem não existir, e isso não é erro.
  - NUNCA expõe o valor do token em log. `token_for` devolve a string crua pro
    caller usar no header Bearer; quem loga é responsabilidade do caller (e não
    deve logar o token).
"""
from __future__ import annotations

import os

from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger()


class CloudLine:
    """Metadados de uma linha Cloud. Imutável na prática (não mexer em runtime)."""

    __slots__ = ("phone_id", "slug", "papel", "env_file", "token_var", "dry_run_var")

    def __init__(
        self,
        *,
        phone_id: str,
        slug: str,
        papel: str,
        env_file: str,
        token_var: str,
        dry_run_var: str | None = None,
    ) -> None:
        self.phone_id = phone_id
        self.slug = slug
        self.papel = papel
        self.env_file = env_file
        self.token_var = token_var
        self.dry_run_var = dry_run_var

    def __repr__(self) -> str:  # pragma: no cover — debug only
        return f"CloudLine(slug={self.slug!r}, phone_id={self.phone_id!r}, papel={self.papel!r})"


# ─── Registry canônico ──────────────────────────────────────────────────────
# phone_number_id → CloudLine. Fonte única da verdade para roteamento e envio.
_LINES: dict[str, CloudLine] = {
    "000000000000000": CloudLine(
        phone_id="000000000000000",
        slug="corretor",
        papel="corretora",          # espelha pro CRM (thread da Pessoa12)
        env_file="/etc/agencia/review.env",
        token_var="META_REVIEW_TOKEN",
        dry_run_var=None,           # a linha da Pessoa12 não tem flag de dry-run própria
    ),
    "000000000000000": CloudLine(
        phone_id="000000000000000",
        slug="assistente",
        papel="responsivo",         # Assistente/Assistente, responde na janela de 24h
        env_file="/etc/agencia/assistente-cloudapi.env",
        token_var="ASSISTENTE_WA_TOKEN",
        dry_run_var="CLOUD_DRY_RUN",
    ),
}

# ─── Linhas de corretora em coexistência, cadastradas por ENV ────────────────
# Fase 2 (Pessoa10 27/07, Pessoa11 depois): o phone_number_id só existe depois que a Dona
# conclui a verificação no Business Manager, então entra por env em vez de código:
#   CLOUD_LINHA_CORRETOR=<phone_number_id>   ·  CLOUD_LINHA_CORRETOR=<phone_number_id>
# ⚠️ WABA PRÓPRIA por corretora (o onboarding de coexistência cria uma nova) —
# ver _WABA_POR_SLUG. O TOKEN é o mesmo (system user do app "agencia api", inscrito
# em cada WABA), mas os TEMPLATES são de cada WABA: prefixo próprio por corretora,
# categoria conferida por slug em templates_utility(). Nunca supor pool comum.
# Sem a env, a linha simplesmente não existe: nada quebra, nada roteia.
# `corretor` = 0000-0000, a linha DA CASA (exibia 0000-0000 até 15/09/2026 — mesma
# WABA e mesmo phone_number_id, foi troca de número exibido, não linha nova).
# É o antigo número do recuperador, aposentado 23/06 e com as instâncias
# Evolution apagadas em 30/07 — Evolution + Cloud no mesmo número é ban. É a
# linha do REMARKETING em massa (5.253 leads, dona Corretor), não de atendimento
# humano como as outras três. Cap próprio, mais baixo.
# `pessoa_d` e `corretor` (09/09): reforço de ligação/resgate. Entram como corretora
# comum — atendem pelo próprio celular e a Pessoa12 cobre pelo painel quando precisa.
_CORRETORAS_ENV: dict[str, str] = {
    "corretor": "CLOUD_LINHA_CORRETOR",
    "corretor": "CLOUD_LINHA_CORRETOR",
    "corretor": "CLOUD_LINHA_CORRETOR",
    "pessoa_d": "CLOUD_LINHA_PESSOA_D",
    "corretor": "CLOUD_LINHA_CORRETOR",
}
# ⚠️ A `corretor` NÃO está na WABA da Pessoa12 — ela tem a dela ("Acme Imoveis",
# 000000000000000), e só o ACME_TOKEN a abre. Cadastrada junto das outras
# em 30/07, herdou o META_REVIEW_TOKEN por descuido.
#
# O sintoma foi mudo e durou 6 dias (09/09 a 15/09): o token não conseguia LER
# os templates da WABA, a trava concluía "linha sem template UTILITY aprovado" e
# não disparava — que é o comportamento certo dela, prova antes de volume. A cada
# 3 min: `enviados=0 falhas=3`. Nenhum erro subiu, nenhum alarme tocou; do lado
# de fora parecia base cansada.
#
# Como conferir de novo se voltar: `GET /<waba_id>?fields=name` com cada token.
# Quem devolve o nome enxerga; quem devolve "does not exist ... missing
# permissions" não — e a mensagem mente, o objeto existe.
_TOKEN_POR_SLUG: dict[str, tuple[str, str]] = {
    "corretor": ("/etc/agencia/acme-cloudapi.env", "ACME_TOKEN"),
}

for _slug, _var in _CORRETORAS_ENV.items():
    _pid = (os.getenv(_var) or "").strip()
    if _pid:
        _envf, _tokv = _TOKEN_POR_SLUG.get(
            _slug, ("/etc/agencia/review.env", "META_REVIEW_TOKEN")
        )
        _LINES[_pid] = CloudLine(
            phone_id=_pid,
            slug=_slug,
            papel="corretora",
            env_file=_envf,
            token_var=_tokv,
            dry_run_var=None,
        )

# slug → phone_id (índice reverso, conveniência).
_BY_SLUG: dict[str, str] = {line.slug: pid for pid, line in _LINES.items()}


def line_by_phone_id(phone_id: str | None) -> CloudLine | None:
    """Retorna a CloudLine de um phone_number_id, ou None se não cadastrado."""
    if not phone_id:
        return None
    return _LINES.get(str(phone_id))


def line_by_slug(slug: str | None) -> CloudLine | None:
    """Retorna a CloudLine por slug (ex: 'corretor', 'assistente'), ou None."""
    if not slug:
        return None
    pid = _BY_SLUG.get(slug)
    return _LINES.get(pid) if pid else None


def _parse_env_file(path: str) -> dict[str, str]:
    """Lê um arquivo .env simples (KEY=VALUE por linha) de forma tolerante.

    - Arquivo ausente/ilegível → dict vazio (sem levantar). Isso é esperado em
      teste/worktree, onde /etc/agencia não existe.
    - Ignora comentários e linhas em branco. Tira aspas do valor.
    """
    out: dict[str, str] = {}
    try:
        raw = Path(path).read_text()
    except OSError:
        return out
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        out[key.strip()] = val.strip().strip('"').strip("'")
    return out


def token_for(line: CloudLine | str | None) -> str | None:
    """Lê (LAZY) o token da linha do env-file dela. None se ausente.

    Aceita CloudLine, slug ou phone_id. NUNCA loga o valor do token.
    """
    resolved = _resolve(line)
    if resolved is None:
        return None
    env = _parse_env_file(resolved.env_file)
    tok = env.get(resolved.token_var)
    if not tok:
        log.debug("cloud_line_token_ausente", slug=resolved.slug, var=resolved.token_var)
        return None
    return tok


def dry_run_for(line: CloudLine | str | None) -> bool:
    """True se a linha está em modo dry-run (loga em vez de enviar).

    Lê a var `dry_run_var` do env-file da linha (LAZY, tolerante a ausência).
    Linha sem `dry_run_var` → False (ex: a Pessoa12 não tem flag própria). Ausência do
    arquivo/var também → False (produção assume envio real; o guard de pytest
    dentro da função de envio é a rede de segurança contra envio em teste).
    """
    resolved = _resolve(line)
    if resolved is None or not resolved.dry_run_var:
        return False
    env = _parse_env_file(resolved.env_file)
    val = env.get(resolved.dry_run_var, "")
    return val.strip().lower() in ("1", "true", "yes", "on")


def _resolve(line: CloudLine | str | None) -> CloudLine | None:
    """Normaliza CloudLine | slug | phone_id → CloudLine (ou None)."""
    if isinstance(line, CloudLine):
        return line
    if not line:
        return None
    # tenta phone_id primeiro, depois slug
    return _LINES.get(str(line)) or line_by_slug(str(line))


# ─── Corretora → linha Cloud (roteamento das automações multi-linha) ─────────
# `corretoras.evolution_instance` é a identidade canônica da corretora no
# copiloto (NUNCA casar por nome — a conta da Pessoa12 no Kommo aparece como "Pessoa
# Pessoa9"). Mapeia slug da linha → instance dela.
#
# ⚠️ Os nomes são os REAIS do cadastro (`select evolution_instance from
# corretoras`), não derivados do nome da corretora: a Pessoa11 é `acme_corretor`, NÃO
# `acme_corretora_b`. Mesmos valores do `_DESTINO` em app/api/cloud_corretora.py
# (que roteia o inbound) — os dois precisam concordar, senão a automação grava a
# resposta num thread e o envio em outro.
_INSTANCE_POR_SLUG: dict[str, str] = {
    "corretor": "acme_corretora_a",
    "corretor": "acme_corretora_c",
    "corretor": "acme_corretor",
    "corretor": "acme_corretor",
    # 09/09: linha de reforço de ligação/resgate. Sem esta entrada,
    # `linha_da_corretora(7)` devolvia None e o envio do painel caía no
    # Evolution — que está DESCONECTADO no número dela (coexistência). A
    # mensagem ia pro vazio, o mesmo buraco que a Pessoa10 pegou em 07/26.
    "pessoa_d": "acme_pessoa_d",
    # 20/09: linha nova da Corretor (+5500000000000, WABA "Corretor Acme Imóveis"
    # 000000000000000). Conectada na BM pela Dona; entra aqui pra
    # `linha_da_corretora` resolver pelo `evolution_instance` do cadastro.
    "corretor": "acme_corretor",
}


def instance_da_linha(line: CloudLine | str | None) -> str | None:
    """`corretoras.evolution_instance` da linha (identidade do thread em
    mensagens_whatsapp), ou None se a linha não é de corretora."""
    resolved = _resolve(line)
    return _INSTANCE_POR_SLUG.get(resolved.slug) if resolved else None


# slug → prefixo dos templates QUE SÃO NOSSOS naquela WABA.
#
# 🔴 Achado 30/07: a WABA do 0000 (Corretor) NÃO estava vazia — herdou 44 templates do
# sistema antigo (recuperador), 12 deles UTILITY aprovados. Dois casavam com a
# convenção da rotação (`acme_reeng_1_gv7cym`, `acme_reeng_2_iv1nso`) mas têm
# 2 variáveis, enquanto o pool "com botão" manda 3 parâmetros — a Meta recusaria,
# ou pior, sairia copy de outro sistema em nome da Acme.
#
# Convenção de nome sozinha não basta quando a WABA tem passado. O prefixo é o
# que diz "este template é nosso, com o contrato de variáveis que o código usa".
# Slug fora do mapa → sem filtro (comportamento histórico).
_PREFIXO_POR_SLUG: dict[str, str] = {
    "corretor": "acme_corretor_",
    "corretor": "acme_cl_",
    # WABA nova e VAZIA (0 templates em 20/09) — o prefixo entra junto com as
    # cópias que forem subidas, pra rotação nunca pegar template de fora.
    "corretor": "acme_an_",
}


def prefixo_do_slug(slug: str | None) -> str | None:
    """Prefixo dos templates próprios da linha, ou None se ela não tem um."""
    return _PREFIXO_POR_SLUG.get(slug) if slug else None


def instance_do_slug(slug: str | None) -> str | None:
    """Instance de gravação de um slug, direto do mapa ESTÁTICO.

    Diferente de `instance_da_linha`, não exige que a linha esteja no registry
    (que depende da env `CLOUD_LINHA_<SLUG>` existir no processo). Usar aqui
    quando a resposta precisa ser confiável mesmo sem env — por exemplo a
    contagem da cota de MARKETING, onde devolver None por env ausente viraria
    "contei zero" e liberaria MARKETING indevidamente.
    """
    return _INSTANCE_POR_SLUG.get(slug) if slug else None


# Sentinela de "não deu pra saber" (cadastro/DB indisponível), distinta de
# "essa corretora NÃO tem linha". A diferença importa: sem cadastro, as
# automações degradam pro caminho histórico (linha da Pessoa12) em vez de travar tudo —
# o mesmo padrão de `_owner_ids_corretor`/`_donos_por_suf8`, cujos guards se DESLIGAM
# quando não conseguem consultar. Travar nesse caso mataria cadência legítima em
# qualquer árvore sem a tabela `corretoras` (SQLite dos testes, worktree enxuto).
INDETERMINADO = CloudLine(
    phone_id="", slug="__indeterminado__", papel="", env_file="", token_var=""
)


def linha_da_corretora(corretora_id: int | None, *, session: Any = None) -> CloudLine | None:
    """Linha Cloud da corretora dona do lead, ou None se ela não tem linha.

    FAIL-CLOSED E SILENCIOSO (requisito das automações): corretora sem linha
    Cloud (hoje a Pessoa11, ainda no Evolution) devolve None — a automação
    simplesmente NÃO roda pra ela, sem exception. Idem pra corretora inexistente,
    inativa, sem `evolution_instance`, ou com instance que não mapeia pra linha
    registrada (ex: a env CLOUD_LINHA_CORRETOR ainda não configurada).

    Devolve `INDETERMINADO` quando o CADASTRO não pôde ser consultado (tabela
    `corretoras` ausente / DB fora): é diferente de "não tem linha". Quem chama
    decide — as automações degradam pro caminho histórico nesse caso, em vez de
    travar (ver comentário na sentinela).

    Resolve por `corretoras.evolution_instance` → slug → registry. Aceita tanto o
    id INTERNO da corretora quanto o `kommo_user_id` (o `owner_id` do lead na
    espinha pode ser qualquer um dos dois, dependendo de quem escreveu — mesma
    tolerância do enroll/guard de dono).

    Args:
        corretora_id: id interno OU kommo_user_id da corretora.
        session: Session SQLAlchemy. Se None, abre uma própria (fecha no fim).
    """
    if corretora_id is None:
        return None
    try:
        from sqlalchemy import or_

        from app.db.models import Corretora

        owns = session is None
        if owns:
            from app.db.session import SessionLocal

            session = SessionLocal()
        try:
            row = (
                session.query(Corretora)
                .filter(
                    or_(
                        Corretora.id == int(corretora_id),
                        Corretora.kommo_user_id == int(corretora_id),
                    ),
                    Corretora.ativo.is_(True),
                )
                .first()
            )
        finally:
            if owns:
                session.close()
    except Exception as e:  # noqa: BLE001 — cadastro ilegível: INDETERMINADO
        log.warning("linha_da_corretora_indisponivel",
                    corretora_id=corretora_id, erro=str(e)[:120])
        return INDETERMINADO

    if row is None or not row.evolution_instance:
        return None
    slug = next(
        (s for s, inst in _INSTANCE_POR_SLUG.items() if inst == row.evolution_instance),
        None,
    )
    return line_by_slug(slug)


def corretoras_com_linha(session: Any = None) -> tuple[int, ...]:
    """IDs (internos E kommo_user_id) das corretoras que TÊM linha Cloud.

    Usado pelas automações multi-linha pra decidir "essa corretora está no
    regime automático?" sem uma query por lead. Fail-closed: erro/DB ausente
    devolve () — nenhuma automação roda, ninguém recebe nada por engano.
    """
    instances = {
        inst for slug, inst in _INSTANCE_POR_SLUG.items() if line_by_slug(slug) is not None
    }
    if not instances:
        return ()
    try:
        from app.db.models import Corretora

        owns = session is None
        if owns:
            from app.db.session import SessionLocal

            session = SessionLocal()
        try:
            rows = (
                session.query(Corretora.id, Corretora.kommo_user_id)
                .filter(
                    Corretora.evolution_instance.in_(tuple(instances)),
                    Corretora.ativo.is_(True),
                )
                .all()
            )
        finally:
            if owns:
                session.close()
    except Exception as e:  # noqa: BLE001
        log.warning("corretoras_com_linha_indisponivel", erro=str(e)[:120])
        return ()
    out: set[int] = set()
    for cid, kommo in rows:
        out.add(int(cid))
        if kommo is not None:
            out.add(int(kommo))
    return tuple(sorted(out))


def all_lines() -> dict[str, CloudLine]:
    """Cópia rasa do registry (phone_id → CloudLine). Pra introspecção/testes."""
    return dict(_LINES)


def as_public_dict() -> dict[str, dict[str, Any]]:
    """Registry serializável SEM segredos (só metadados). Pra debug/health."""
    return {
        pid: {
            "slug": line.slug,
            "papel": line.papel,
            "env_file": line.env_file,
            "token_var": line.token_var,
            "dry_run_var": line.dry_run_var,
        }
        for pid, line in _LINES.items()
    }

# ─── Trava de categoria: só UTILITY sai (regra dura da Dona) ─────────────────
# A Meta classifica a MESMA copy de forma diferente por conta: os templates da
# linha da Pessoa12 são UTILITY, e os idênticos criados na WABA da Pessoa10 (27/07)
# voltaram TODOS como MARKETING. Marketing custa ~10x e queima a lista, então
# aqui é fail-closed: linha sem template UTILITY comprovado NÃO dispara nada.
#
# Fonte da verdade = a Meta (consultada e cacheada), não o nosso rótulo interno:
# o gate de `toque.categoria` da cadência olha o rótulo NOSSO e não pegaria este
# caso. Cache curto porque a Meta reclassifica template vivo sem avisar.
_CAT_TTL_S = 900.0          # consulta a Meta no máximo a cada 15 min por linha
_CAT_VALIDADE_BOA_S = 86400.0  # último resultado BOM vale 24h se a Meta falhar
_cat_cache: dict[str, tuple[float, set[str]]] = {}
_cat_ultimo_bom: dict[str, tuple[float, set[str]]] = {}
# O "último bom" também vai pra DISCO: o rate limit da Meta (#80008) é comum e o
# cache de memória morre em cada restart — sem isso, um deploy no minuto errado
# calaria uma linha que funciona perfeitamente.
_CAT_ARQUIVO = os.getenv("CLOUD_LINES_CACHE", "/var/lib/copiloto/linhas_utility.json")


def _carregar_do_disco() -> None:
    """Repovoa o "último bom" respeitando a IDADE REAL do cache.

    ⚠️ Bug corrigido em 29/07 (auditoria Sol): o formato antigo guardava só os
    NOMES, sem hora de parede. Como `_cat_ultimo_bom` usa `monotonic()` — que
    zera a cada boot — todo restart carimbava o horário ATUAL, e um cache de
    meses atrás parecia recém-validado por mais 24h. Se a Meta reclassificasse
    um template pra MARKETING e a consulta caísse no mesmo momento, o MARKETING
    passava pela trava. É exatamente a regra que não pode furar — a linha da Pessoa12
    já tomou ban uma vez.

    Agora grava `em` (epoch de parede) junto e converte a idade real pro relógio
    monotônico na carga. Cache mais velho que a validade NÃO entra: a linha
    volta a exigir prova da Meta, que é o comportamento seguro.

    Formato antigo (lista pura, sem `em`) é IGNORADO de propósito — sem
    timestamp não dá pra saber a idade, e otimismo aqui custa a conta.
    """
    import json
    import time as _time

    try:
        with open(_CAT_ARQUIVO, encoding="utf-8") as f:
            dados = json.load(f)
    except (OSError, ValueError):
        return

    agora_mono = _time.monotonic()
    agora_parede = _time.time()
    for slug, entrada in (dados.get("utility") or {}).items():
        if not isinstance(entrada, dict):
            log.info("cloud_lines_cache_formato_antigo_ignorado", slug=slug)
            continue
        nomes = entrada.get("nomes") or []
        gravado_em = entrada.get("em")
        if not nomes or not isinstance(gravado_em, (int, float)):
            continue
        idade = max(0.0, agora_parede - float(gravado_em))
        if idade >= _CAT_VALIDADE_BOA_S:
            log.info("cloud_lines_cache_vencido_ignorado",
                     slug=slug, idade_h=round(idade / 3600, 1))
            continue
        # desloca o carimbo monotônico pra trás pela idade real, pra o teste de
        # validade em `templates_utility` enxergar a idade de verdade.
        _cat_ultimo_bom.setdefault(slug, (agora_mono - idade, set(nomes)))


def _salvar_no_disco() -> None:
    import json
    import os as _os
    import tempfile
    import time as _time

    try:
        _os.makedirs(_os.path.dirname(_CAT_ARQUIVO), exist_ok=True)
        agora_mono = _time.monotonic()
        agora_parede = _time.time()
        # converte o carimbo monotônico de cada entrada pra hora de PAREDE — é o
        # que sobrevive ao restart (monotonic zera no boot).
        payload = {
            "utility": {
                slug: {
                    "nomes": sorted(nomes),
                    "em": agora_parede - max(0.0, agora_mono - quando),
                }
                for slug, (quando, nomes) in _cat_ultimo_bom.items()
            }
        }
        d = _os.path.dirname(_CAT_ARQUIVO)
        with tempfile.NamedTemporaryFile("w", dir=d, delete=False, encoding="utf-8") as tmp:
            json.dump(payload, tmp, ensure_ascii=False, indent=1)
            temp = tmp.name
        _os.replace(temp, _CAT_ARQUIVO)   # atômico: nunca deixa arquivo pela metade
    except OSError as e:
        log.warning("cloud_lines_cache_disco_falhou", erro=str(e)[:120])

# slug → WABA (pra consultar a categoria real do template)
_WABA_POR_SLUG: dict[str, str] = {
    "corretor": "000000000000000",
    # "Pessoa8- Acme Imóveis" — coexistência 09/09. A tela do Business Manager
    # mostra ESTE id como "Identificação" do número, mas ele é da WABA; o
    # phone_number_id (000000000000000) só aparece em GET /<waba>/phone_numbers.
    # Confundir os dois faz o GET devolver só {"id"} sem campo nenhum, que parece
    # falta de permissão e não é.
    "pessoa_d": "000000000000000",
    "assistente": "1+000000000000000",
    "corretor": "000000000000000",
    "corretor": "000000000000000",   # "Acme Imoveis" — 0000, coexistência 30/07
    # "Corretor Acme Imóveis" — linha nova de 20/09 (+5500000000000). Fixo aqui
    # E aceito por env: sem WABA conhecida, `templates_utility` devolve set() e
    # a linha fica muda — foi o que aconteceu no primeiro teste dela.
    "corretor": "000000000000000",
}
# A coexistência cria uma WABA NOVA por número (a da Pessoa10 virou "Pessoa10"
# 000000000000000) e não existe API pra enumerar — a Dona lê o id no Business
# Manager. Enquanto não vier, a linha fica sem WABA conhecida → templates_utility
# devolve set() → não dispara. Passar por env: CLOUD_WABA_CORRETOR=<id>
for _s in ("corretor", "corretor", "corretor", "corretor", "pessoa_d"):
    _w = (os.getenv(f"CLOUD_WABA_{_s.upper()}") or "").strip()
    if _w:
        _WABA_POR_SLUG[_s] = _w


def _buscar_categoria_na_meta(slug: str, categoria: str) -> set[str] | None:
    """Consulta a Graph. Devolve o set de nomes APROVADOS na categoria pedida, ou
    None quando NÃO deu pra saber (rate limit #80008, token sem permissão, rede
    fora) — 'não sei' é diferente de 'não tem', e os dois casos são tratados
    diferente."""
    waba = _WABA_POR_SLUG.get(slug)
    if not waba:
        return None
    try:
        import httpx

        tok = token_for(slug)
        if not tok:
            return None
        r = httpx.get(
            f"https://graph.facebook.com/v21.0/{waba}/message_templates",
            params={"fields": "name,status,category", "limit": 100, "access_token": tok},
            timeout=15,
        )
        if r.status_code >= 400:
            log.warning("cloud_lines_categoria_indisponivel", slug=slug,
                        status=r.status_code, erro=str(r.text)[:120])
            return None
        return {
            t["name"] for t in (r.json().get("data") or [])
            if t.get("status") == "APPROVED" and t.get("category") == categoria
        }
    except Exception as e:  # noqa: BLE001
        log.warning("cloud_lines_categoria_erro", slug=slug, erro=str(e)[:120])
        return None


def _buscar_utility_na_meta(slug: str) -> set[str] | None:
    """Compat: só UTILITY. Mantido porque é o caminho usado pelo gate padrão."""
    return _buscar_categoria_na_meta(slug, "UTILITY")


def templates_utility(slug: str, *, agora: float | None = None) -> set[str]:
    """Nomes de template APROVADOS e em categoria UTILITY na WABA da linha.

    Política de indisponibilidade (rate limit da Meta é comum — #80008):
      • já sabíamos que a linha tem UTILITY → mantém o último resultado bom por
        até 24h. Um erro passageiro NÃO pode calar uma linha que funciona.
      • nunca confirmamos nada pra essa linha → set() vazio, e o caller não
        dispara. Linha nova só ganha permissão com prova, não com otimismo.
    """
    import time as _time

    now = agora if agora is not None else _time.monotonic()
    hit = _cat_cache.get(slug)
    if hit and now - hit[0] < _CAT_TTL_S:
        return hit[1]

    achado = _buscar_utility_na_meta(slug)
    if achado is None:
        bom = _cat_ultimo_bom.get(slug)
        if bom and now - bom[0] < _CAT_VALIDADE_BOA_S:
            log.info("cloud_lines_categoria_cache_antigo", slug=slug, n=len(bom[1]))
            return bom[1]
        return set()

    _cat_cache[slug] = (now, achado)
    if achado:
        _cat_ultimo_bom[slug] = (now, achado)
        _salvar_no_disco()
    return achado


# Cache de MARKETING — separado do de UTILITY de propósito. A política aqui é a
# INVERSA: quando a consulta falha, NÃO herdamos cache antigo. Autorizar MARKETING
# (10x o custo, queima a lista) com base em informação velha é o erro caro; se a
# Meta não responde, a cota simplesmente não é usada hoje e o disparo segue todo
# em UTILITY. Sem prova fresca, sem MARKETING.
_cat_mkt_cache: dict[str, tuple[float, set[str]]] = {}


def templates_marketing(slug: str, *, agora: float | None = None) -> set[str]:
    """Nomes APROVADOS em MARKETING na WABA da linha (pra cota de 20%).

    Diferente de `templates_utility`: aqui NÃO há fallback pra cache vencido.
    Falha na consulta → set() → nenhum MARKETING é liberado.
    """
    import time as _time

    now = agora if agora is not None else _time.monotonic()
    hit = _cat_mkt_cache.get(slug)
    if hit and now - hit[0] < _CAT_TTL_S:
        return hit[1]

    achado = _buscar_categoria_na_meta(slug, "MARKETING")
    if achado is None:
        log.info("cloud_lines_marketing_indisponivel_sem_fallback", slug=slug)
        return set()

    _cat_mkt_cache[slug] = (now, achado)
    return achado


def pode_disparar(slug: str) -> bool:
    """True só se a linha tem ao menos UM template UTILITY aprovado na Meta."""
    return bool(templates_utility(slug))


def template_e_utility(slug: str, nome: str) -> bool:
    """True se ESTE template está aprovado como UTILITY na WABA da linha.
    Usado antes de cada despacho — a rotação nunca escolhe um MARKETING."""
    return nome in templates_utility(slug)


_carregar_do_disco()
