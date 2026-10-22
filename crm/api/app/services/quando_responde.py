"""Quando a pessoa responde ao remarketing — o que a tela decide e o motor lê.

Pedido da Dona (05/10/2026): *"quando cair na etapa respondeu vai ganhar tag
RMKT, vai para etapa de entrada do funil de vendas, e vai ter roleta para
remarketing. Tem tag RMKT, então passa por uma outra roleta ao invés da roleta
normal do Meta."* E, em seguida: *"todo mundo que cair em Respondeu vai para a
roleta"*, *"vai com histórico"*, e o texto do aviso que o lead recebe.

Cinco controles, cada um uma chave em `config_operacional`:

```
handoff_remarketing_quem_vai       "todos" | "interesse"      padrão "todos"
handoff_remarketing_etapa          id em pipeline_stages      padrão: "Novo" do funil principal
handoff_remarketing_roleta         [ids de corretora]         padrão do .env ([1, 2, 3])
handoff_remarketing_avisa_lead     true | false               padrão false (em produção: true desde 18/09)
handoff_remarketing_aviso_texto    texto com {corretora}      padrão: o texto da Dona
handoff_remarketing_aviso_resumo   true | false               padrão false
handoff_remarketing_respeita_humano true | false              padrão true
```

A última (adendo da orquestradora, 05/10/2026): quem já está conversando com
alguém da casa pelo celular da linha da Corretor cai em Respondeu mas NÃO é
transferido nem avisado sozinho. "Alguém da casa falou" é o mesmo critério que
cala a Pessoa (`atendimento_corretor._humano_falando` desde o último disparo).

Este módulo é a ÚNICA leitura dessas chaves que o handoff usa (`ler`), e ela é
estrita: relê o banco na hora, sem cache, e o que não der pra ler ou não servir
NÃO vira padrão — o card fica em Respondeu e o motivo vai pro log e pra tela.
É a mesma régua do disparo (`config_operacional`, "A exceção").

A tag RMKT é a marca `leads.veio_remarketing_em`: quem a tem nunca passa pela
roleta do Meta (`dono_para_lead_marcado`, usado pelo intake).
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Any

import structlog
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

log = structlog.get_logger()

CHAVE_QUEM_VAI = "handoff_remarketing_quem_vai"
CHAVE_ETAPA = "handoff_remarketing_etapa"
CHAVE_ROLETA = "handoff_remarketing_roleta"
CHAVE_AVISA = "handoff_remarketing_avisa_lead"
CHAVE_AVISO_TEXTO = "handoff_remarketing_aviso_texto"
CHAVE_AVISO_RESUMO = "handoff_remarketing_aviso_resumo"
CHAVE_RESPEITA_HUMANO = "handoff_remarketing_respeita_humano"
CHAVES = (CHAVE_QUEM_VAI, CHAVE_ETAPA, CHAVE_ROLETA, CHAVE_AVISA,
          CHAVE_AVISO_TEXTO, CHAVE_AVISO_RESUMO, CHAVE_RESPEITA_HUMANO)

# O evento que fica no card quando a transferência é segurada porque alguém da
# casa já está conversando pelo celular da linha. É ele que a tela conta e que
# o card mostra; a transferência manual continua valendo.
EVENTO_SEGURADO = "handoff_segurado"
MOTIVO_HUMANO = "humano_no_comando"

QUEM_TODOS = "todos"
QUEM_INTERESSE = "interesse"
MODOS = (QUEM_TODOS, QUEM_INTERESSE)

# O nome que o card mostra. A marca no banco continua sendo a data
# (`veio_remarketing_em`): quem já tinha a marca "remarketing" passa a exibir
# RMKT sem migração nenhuma.
TAG = "RMKT"

FUNIL_VENDAS = "principal"
NOME_ETAPA_PADRAO = "Novo"

PLACEHOLDER_CORRETORA = "{corretora}"
# A copy que a Dona ditou (05/10): "Olá, sou Fulana, estávamos falando em outro
# número, vamos continuar o atendimento por aqui!". O "Olá" já vem do casco do
# template (`acme_*_ola_*` abre com "Olá {{1}}, tudo bem?"), então a variável
# começa no "Sou a".
AVISO_PADRAO = (
    "Sou a {corretora}, da Acme Imóveis. Estávamos falando em outro número, "
    "vamos continuar o atendimento por aqui!"
)
# Mesmo teto e mesmo motivo da copy do disparo (`carrossel_copies`): o corpo do
# template cabe 1.024 caracteres depois de preenchido.
AVISO_MAX_CHARS = 900


class ConfigInvalida(ValueError):
    """O que está gravado não serve. `motivo` é o código curto que vai no
    resultado do handoff; a mensagem é a explicação em português."""

    def __init__(self, motivo: str, mensagem: str) -> None:
        super().__init__(mensagem)
        self.motivo = motivo


MOTIVO_EM_PORTUGUES: dict[str, str] = {
    "config_indisponivel": "não consegui ler a configuração, ninguém foi transferido",
    "quem_vai_invalido": "a opção de quem vai pra corretora está ilegível",
    "etapa_invalida": "a etapa de entrada escolhida não serve",
    "roleta_invalida": "a roleta do remarketing está ilegível",
    "roleta_vazia": "a roleta do remarketing está vazia: quem responde fica em Respondeu",
    "roleta_sem_ninguem_valido": "ninguém da roleta do remarketing está ativo",
    "aviso_invalido": "o texto do aviso não serve: a transferência vale, o aviso não sai",
    "respeita_humano_invalido": "a opção de não transferir quem já conversa pelo celular está ilegível",
}


@dataclass(frozen=True)
class Config:
    quem_vai: str
    etapa: int
    etapa_nome: str
    roleta: tuple[int, ...]
    respeita_humano: bool = True


# ── Formato (não vai ao banco) ───────────────────────────────────────────────


def validar_quem_vai(valor: Any) -> str:
    if valor not in MODOS:
        raise ValueError(
            'quem vai pra corretora: escolha "todos" (todo mundo que cair em '
            'Respondeu) ou "interesse" (só botão ou interesse)'
        )
    return str(valor)


def _ids(valor: Any, *, nome: str) -> list[int]:
    if not isinstance(valor, list):
        raise ValueError(f"{nome}: esperado uma lista de corretoras, na ordem")
    ids: list[int] = []
    for x in valor:
        if isinstance(x, bool) or not isinstance(x, int):
            raise ValueError(f"{nome}: corretora inválida: {x!r}")
        ids.append(x)
    if len(set(ids)) != len(ids):
        raise ValueError(f"{nome}: corretora repetida")
    return ids


def validar_aviso_texto(valor: Any) -> str | None:
    """O texto do aviso vindo da tela. None ou vazio = volta ao padrão.

    Vai na VARIÁVEL de um template UTILITY já aprovado, então vale o que vale
    pra copy do disparo (`carrossel_copies.validar_texto`): tamanho, sem
    caractere que a Meta recusa, quebra de linha limpa. A diferença é o
    placeholder: aqui existe UM, `{corretora}`, que o código troca pelo nome
    público de quem recebeu o lead.
    """
    from app.services import carrossel_copies as cc

    if valor is None or (isinstance(valor, str) and not valor.strip()):
        return None
    if not isinstance(valor, str):
        raise ValueError("o texto do aviso precisa ser texto")
    t = cc.canonizar(valor)
    if len(t) > AVISO_MAX_CHARS:
        raise ValueError(
            f"texto longo demais: {len(t)} caracteres, o máximo é {AVISO_MAX_CHARS}"
        )
    resto = t.replace(PLACEHOLDER_CORRETORA, "")
    if "{" in resto or "}" in resto:
        raise ValueError(
            f"só existe {PLACEHOLDER_CORRETORA} (o nome de quem recebeu o lead). "
            "O nome da pessoa já entra sozinho na abertura; tira as outras chaves"
        )
    ruins = sorted({c for c in t if c != "\n" and unicodedata.category(c) in ("Cc", "Cf")
                    and c not in ("‍", "️")})
    if ruins:
        raise ValueError(
            "o texto tem caractere invisível que a Meta recusa ("
            + ", ".join(f"U+{ord(c):04X}" for c in ruins)
            + "): apaga e digita de novo esse trecho"
        )
    return None if t == AVISO_PADRAO else t


def render_aviso(modelo: str, corretora: str | None) -> str:
    """Troca `{corretora}` pelo primeiro nome público. Sem nome no cadastro a
    mensagem fala pela casa — "Sou a , da Acme" seria pior que não dizer."""
    from app.services import nome_publico

    primeiro = nome_publico.primeiro_nome(corretora)
    if primeiro:
        return modelo.replace(PLACEHOLDER_CORRETORA, primeiro)
    return (modelo.replace("Sou a " + PLACEHOLDER_CORRETORA, "Sou da equipe")
                  .replace("a " + PLACEHOLDER_CORRETORA, "a equipe")
                  .replace(PLACEHOLDER_CORRETORA, "a equipe"))


def previa_do_aviso(modelo: str, corretora: str | None = "Pessoa",
                    nome_lead: str = "João") -> str:
    """A mensagem inteira como chega no celular: casco `acme_*_ola_*` + o
    texto. O fecho muda de casco pra casco (são 4 frases em rodízio); a prévia
    mostra a do casco mais usado."""
    return (f"Olá {nome_lead}, tudo bem?\n\n{render_aviso(modelo, corretora)}"
            "\n\nTô por aqui se precisar 🙂")


# ── Conferência no banco ─────────────────────────────────────────────────────


def etapa_padrao(session: Session, tenant_id: int) -> tuple[int, str] | None:
    """(id, nome) da etapa de entrada padrão: a "Novo" do funil principal
    DESTE cliente.

    É a mesma de sempre (`atendimento_corretor.STAGE_NOVO`, a 6 da Acme) quando
    ela existe no funil de vendas do cliente; senão, a primeira etapa chamada
    "Novo" desse funil. A conferência é no banco: se a etapa foi apagada ou
    trocada de funil, não é ela.
    """
    from app.services.atendimento_corretor import STAGE_NOVO

    linhas = session.execute(
        sql_text(
            """SELECT id, nome FROM pipeline_stages
                WHERE tenant_id = :t AND funil = :f
                  AND (id = :de_sempre OR lower(btrim(nome)) = :n)
                ORDER BY (id = :de_sempre) DESC, ordem NULLS LAST, id LIMIT 1"""
        ),
        {"t": tenant_id, "f": FUNIL_VENDAS, "n": NOME_ETAPA_PADRAO.casefold(),
         "de_sempre": STAGE_NOVO},
    ).first()
    return (int(linhas[0]), str(linhas[1])) if linhas else None


def conferir_etapa(session: Session, tenant_id: int, etapa: Any) -> str:
    """Devolve o nome da etapa, ou levanta ValueError (PT-BR) se ela não pode
    ser a entrada: tem que existir, ser deste cliente, do funil de vendas e não
    ser etapa de fechamento (ganho, perdido)."""
    if isinstance(etapa, bool) or not isinstance(etapa, int) or etapa <= 0:
        raise ValueError("a etapa de entrada precisa ser uma opção da lista")
    r = session.execute(
        sql_text("SELECT tenant_id, nome, funil, tipo, ordem FROM pipeline_stages WHERE id = :i"),
        {"i": etapa},
    ).first()
    if r is None:
        raise ValueError(f"a etapa {etapa} não existe mais")
    if int(r[0]) != int(tenant_id):
        raise ValueError(f"a etapa {etapa} é de outro cliente")
    if r[2] != FUNIL_VENDAS:
        raise ValueError(f'a etapa "{r[1]}" não é do funil de vendas')
    if r[4] is None:
        # Etapa sem posição no quadro (sobra de importação): o card entraria
        # num lugar que ninguém vê.
        raise ValueError(f'a etapa "{r[1]}" não aparece no quadro de vendas')
    if r[3] in ("ganho", "perdido"):
        raise ValueError(f'a etapa "{r[1]}" é de fechamento — lead novo não entra por ela')
    return str(r[1])


def validar_etapa(valor: Any, *, tenant_id: int, session: Session | None = None) -> int | None:
    """O que a tela grava em `handoff_remarketing_etapa`. None = "Novo"."""
    if valor is None:
        return None

    def conferir(s: Session) -> int | None:
        conferir_etapa(s, tenant_id, valor)
        padrao = etapa_padrao(s, tenant_id)
        # Escolher a padrão apaga a chave em vez de ficar "editado" à toa.
        return None if padrao and padrao[0] == valor else int(valor)

    return _com_sessao(session, conferir)


def conferir_corretora(session: Session, tenant_id: int, cid: int) -> None:
    """Levanta ValueError se a corretora não pode estar na roleta do
    remarketing: tem que existir neste cliente, estar ativa e não ser a dona
    da linha que dispara (a Corretor) — transferir "pra Corretor de novo" é não transferir."""
    from app.services import carrossel_corretor as cb

    if cid == cb.CORRETORA_CORRETOR:
        raise ValueError("a Corretor é de onde o lead sai: ela não entra na roleta")
    r = session.execute(
        sql_text("SELECT tenant_id, ativo, nome FROM corretoras WHERE id = :i"), {"i": cid}
    ).first()
    if r is None or int(r[0]) != int(tenant_id):
        raise ValueError(f"a corretora {cid} não existe neste cliente")
    if not r[1]:
        raise ValueError(f"{str(r[2]).split(' ')[0]} está inativa")


def validar_roleta(valor: Any, *, tenant_id: int, session: Session | None = None) -> list[int]:
    """O que a tela grava em `handoff_remarketing_roleta`. Lista vazia é
    ACEITA (e quer dizer: ninguém recebe, quem responde fica em Respondeu) —
    é a diferença pra roleta do Meta, que não pode ficar vazia porque lá o
    lead nasceria sem dono."""
    ids = _ids(valor, nome="roleta do remarketing")

    def conferir(s: Session) -> list[int]:
        for cid in ids:
            conferir_corretora(s, tenant_id, cid)
        return ids

    return _com_sessao(session, conferir)


def _com_sessao(session: Session | None, fn: Any) -> Any:
    if session is not None:
        return fn(session)
    from app.db.session import SessionLocal

    with SessionLocal() as s:
        return fn(s)


# ── Leitura (o que o handoff usa) ────────────────────────────────────────────


def ler(session: Session, tenant_id: int) -> Config:
    """O que vale AGORA, relido do banco. Levanta `ConfigInvalida` (com o
    motivo) ou `config_operacional.ConfigIndisponivel` — quem chama NÃO
    transfere.

    Corretora que saiu do ar depois de gravada (inativada, apagada) sai da
    roda sem derrubar as outras; se não sobrar ninguém, é `roleta_sem_ninguem_valido`.
    """
    from app.services import config_operacional

    config_operacional.recarregar_estrito()
    modo = config_operacional.get(CHAVE_QUEM_VAI)
    if modo not in MODOS:
        raise ConfigInvalida("quem_vai_invalido", f"{CHAVE_QUEM_VAI}: {modo!r}")

    bruta = config_operacional.get(CHAVE_ETAPA)
    if bruta is None:
        padrao = etapa_padrao(session, tenant_id)
        if padrao is None:
            raise ConfigInvalida(
                "etapa_invalida", 'o funil de vendas não tem a etapa "Novo"')
        etapa, etapa_nome = padrao
    else:
        try:
            etapa_nome = conferir_etapa(session, tenant_id, bruta)
        except ValueError as e:
            raise ConfigInvalida("etapa_invalida", f"{CHAVE_ETAPA}: {e}") from e
        etapa = int(bruta)

    try:
        ids = _ids(config_operacional.get(CHAVE_ROLETA) or [], nome=CHAVE_ROLETA)
    except ValueError as e:
        raise ConfigInvalida("roleta_invalida", str(e)) from e
    if not ids:
        raise ConfigInvalida("roleta_vazia", MOTIVO_EM_PORTUGUES["roleta_vazia"])
    validas: list[int] = []
    for cid in ids:
        try:
            conferir_corretora(session, tenant_id, cid)
            validas.append(cid)
        except ValueError as e:
            log.warning("roleta_remarketing_corretora_fora", corretora_id=cid,
                        motivo=str(e)[:120])
    if not validas:
        raise ConfigInvalida("roleta_sem_ninguem_valido",
                             MOTIVO_EM_PORTUGUES["roleta_sem_ninguem_valido"])
    respeita = config_operacional.get(CHAVE_RESPEITA_HUMANO)
    if not isinstance(respeita, bool):
        # Estrita como as outras: valor torto não vira "desligado" (que
        # transferiria quem está no meio de uma conversa) nem "ligado" calado.
        raise ConfigInvalida("respeita_humano_invalido",
                             f"{CHAVE_RESPEITA_HUMANO}: {respeita!r}")
    return Config(quem_vai=str(modo), etapa=etapa, etapa_nome=etapa_nome,
                  roleta=tuple(validas), respeita_humano=respeita)


def aviso_ligado() -> bool:
    """Estrito: só `true` (booleano) liga. Levanta `ConfigIndisponivel`."""
    from app.services import config_operacional

    return config_operacional.get(CHAVE_AVISA) is True


def modelo_do_aviso() -> str:
    """O texto do aviso que vale agora (com `{corretora}`). Levanta
    `ConfigInvalida('aviso_invalido')` se o gravado não passa na validação —
    o aviso não sai com texto que a tela teria recusado."""
    from app.services import config_operacional

    bruto = config_operacional.get(CHAVE_AVISO_TEXTO)
    if bruto is None:
        return AVISO_PADRAO
    try:
        limpo = validar_aviso_texto(bruto)
    except ValueError as e:
        raise ConfigInvalida("aviso_invalido", f"{CHAVE_AVISO_TEXTO}: {e}") from e
    if limpo is None:
        return AVISO_PADRAO
    if limpo != bruto:
        raise ConfigInvalida(
            "aviso_invalido",
            f"{CHAVE_AVISO_TEXTO}: o texto gravado não está na forma em que é "
            "enviado — regrave pela tela")
    return limpo


def aviso_com_resumo() -> bool:
    from app.services import config_operacional

    return config_operacional.get(CHAVE_AVISO_RESUMO) is True


# ── A roleta ─────────────────────────────────────────────────────────────────

_SQL_ULTIMO_DA_ROLETA = """
    SELECT owner_id FROM leads
     WHERE tenant_id = :t
       AND veio_remarketing_em IS NOT NULL
       AND owner_id = ANY(:elegiveis)
     ORDER BY veio_remarketing_em DESC, id DESC
     LIMIT 1
"""


def proxima_da_roleta(session: Session, tenant_id: int, elegiveis: tuple[int, ...]) -> int:
    """Round-robin sobre as elegíveis, na ordem da config.

    Estado da rotação = a última atribuição REAL (o dono do card com a marca
    RMKT mais recente), igual à roleta do intake. Guardar contador em tabela
    seria um estado a mais pra sair de sincronia com o board quando alguém
    transfere na mão.
    """
    if len(elegiveis) == 1:
        return elegiveis[0]
    ultimo = session.execute(
        sql_text(_SQL_ULTIMO_DA_ROLETA), {"t": tenant_id, "elegiveis": list(elegiveis)}
    ).scalar()
    if ultimo is None or int(ultimo) not in elegiveis:
        return elegiveis[0]
    return elegiveis[(elegiveis.index(int(ultimo)) + 1) % len(elegiveis)]


def dono_para_lead_marcado(session: Session, tenant_id: int) -> int | None:
    """Quem recebe um card que JÁ tem a tag RMKT e está sem dono.

    É a porta que o intake usa no lugar da roleta do Meta: card com a marca de
    remarketing passa pela roleta do remarketing ou fica como está (None).
    Nunca levanta.
    """
    try:
        cfg = ler(session, tenant_id)
    except Exception as e:  # noqa: BLE001
        log.warning("roleta_remarketing_indisponivel_card_fica_sem_dono",
                    erro=str(e)[:140])
        return None
    return proxima_da_roleta(session, tenant_id, cfg.roleta)


# ── O que a tela mostra ──────────────────────────────────────────────────────

PERIODO_DIAS = 30


def _corretoras(session: Session, tenant_id: int) -> list[dict[str, Any]]:
    from app.services import carrossel_corretor as cb
    from app.services import nome_publico

    linhas = session.execute(
        sql_text(
            """SELECT id, nome FROM corretoras
                WHERE tenant_id = :t AND ativo IS TRUE AND id <> :corretor
                  AND coalesce(papel, 'corretora') LIKE 'corretora%'
                ORDER BY id"""
        ),
        {"t": tenant_id, "corretor": cb.CORRETORA_CORRETOR},
    ).all()
    from app.services.lead_transferencia import label_corretora

    out = []
    for cid, nome in linhas:
        try:
            publico = nome_publico.publico(session, int(cid))
        except Exception:  # noqa: BLE001
            publico = None
        # `nome`: o rótulo curto do painel (apelido, senão o 1º nome) — é como
        # a casa chama cada uma ("Pessoa10", "Pessoa11"). `nome_publico`: como ela se
        # apresenta pro lead (a Pessoa3 assina "Pessoa").
        out.append({"id": int(cid),
                    "nome": label_corretora(session, int(cid)) or str(nome).split(" ")[0],
                    "nome_publico": nome_publico.primeiro_nome(publico or nome)})
    return out


def resumo(session: Session, tenant_id: int) -> dict[str, Any]:
    """Tudo que o bloco "Quando a pessoa responde" desenha. Nunca levanta por
    valor torto: devolve `problema` com o motivo (o mesmo que faz o handoff
    não transferir) e segue mostrando o que conseguiu ler."""
    from app.services import carrossel_corretor as cb
    from app.services import config_operacional
    from app.services.lead_transferencia import EVENTO_TRANSFERENCIA

    out: dict[str, Any] = {"problema": None, "motivo": None, "tag": TAG}
    cfg: Config | None = None
    try:
        cfg = ler(session, tenant_id)
    except ConfigInvalida as e:
        out.update(problema=MOTIVO_EM_PORTUGUES.get(e.motivo, str(e)), motivo=e.motivo,
                   detalhe=str(e)[:200])
    except config_operacional.ConfigIndisponivel as e:
        out.update(problema=MOTIVO_EM_PORTUGUES["config_indisponivel"],
                   motivo="config_indisponivel", detalhe=str(e)[:200])
        return out

    corretoras = _corretoras(session, tenant_id)
    nomes = {c["id"]: c["nome"] for c in corretoras}

    modo = config_operacional.get(CHAVE_QUEM_VAI)
    out["quem_vai"] = modo if modo in MODOS else None
    out["quem_vai_padrao"] = QUEM_TODOS

    # Etapa: a que vale + as que podem ser escolhidas (só funil de vendas).
    padrao = etapa_padrao(session, tenant_id)
    gravada = config_operacional.get(CHAVE_ETAPA)
    etapas = [
        {"id": int(r[0]), "nome": r[1],
         "permitida": r[2] not in ("ganho", "perdido"),
         "padrao": bool(padrao and padrao[0] == int(r[0]))}
        for r in session.execute(
            sql_text(
                """SELECT id, nome, tipo FROM pipeline_stages
                    WHERE tenant_id = :t AND funil = :f AND ordem IS NOT NULL
                    ORDER BY ordem, id"""
            ),
            {"t": tenant_id, "f": FUNIL_VENDAS},
        ).all()
    ]
    out["etapa"] = {
        "valendo": cfg.etapa if cfg else None,
        "valendo_nome": cfg.etapa_nome if cfg else None,
        "gravada": gravada,
        "padrao": padrao[0] if padrao else None,
        "opcoes": etapas,
    }

    # Roleta: a gravada, quem está fora dela, de quem é a vez, quanto cada uma
    # recebeu no período — e a do Meta ao lado, só pra leitura.
    try:
        gravadas = _ids(config_operacional.get(CHAVE_ROLETA) or [], nome=CHAVE_ROLETA)
    except ValueError:
        gravadas = []
    recebidos = {
        int(r[0]): int(r[1])
        for r in session.execute(
            sql_text(
                """SELECT (payload->>'para')::int, count(*)
                     FROM lead_events
                    WHERE tenant_id = :t AND tipo = :ev
                      AND payload->>'quem' = 'handoff_remarketing'
                      AND payload->>'para' ~ '^[0-9]+$'
                      AND at > now() - (:dias || ' days')::interval
                    GROUP BY 1"""
            ),
            {"t": tenant_id, "ev": EVENTO_TRANSFERENCIA, "dias": PERIODO_DIAS},
        ).all()
    }
    vez = proxima_da_roleta(session, tenant_id, cfg.roleta) if cfg else None
    out["roleta"] = {
        "ids": gravadas,
        "validas": list(cfg.roleta) if cfg else [],
        "da_vez": vez,
        "da_vez_nome": nomes.get(vez) if vez is not None else None,
        "periodo_dias": PERIODO_DIAS,
        "corretoras": [
            {**c, "na_roleta": c["id"] in gravadas,
             "recebeu": recebidos.get(c["id"], 0)}
            for c in corretoras
        ],
    }
    meta = [int(x) for x in (config_operacional.get("roleta_corretoras") or [])]
    out["roleta_do_meta"] = {
        "ids": meta, "nomes": [nomes.get(i, f"#{i}") for i in meta],
    }

    # Aviso: ligado?, o texto que vale, o padrão e a prévia inteira.
    aviso: dict[str, Any] = {"padrao": AVISO_PADRAO, "max": AVISO_MAX_CHARS,
                             "placeholder": PLACEHOLDER_CORRETORA}
    aviso["ligado"] = config_operacional.get(CHAVE_AVISA) is True
    aviso["com_resumo"] = config_operacional.get(CHAVE_AVISO_RESUMO) is True
    try:
        modelo = modelo_do_aviso()
        aviso.update(texto=modelo, personalizado=modelo != AVISO_PADRAO, problema=None)
    except ConfigInvalida as e:
        modelo = AVISO_PADRAO
        aviso.update(texto=config_operacional.get(CHAVE_AVISO_TEXTO),
                     personalizado=True, problema=str(e)[:200])
    quem = next((c for c in corretoras if c["id"] == vez), None) or (
        corretoras[0] if corretoras else None)
    aviso["previa"] = previa_do_aviso(
        modelo, quem["nome_publico"] if quem else None)
    aviso["previa_de"] = quem["nome"] if quem else None
    out["aviso"] = aviso

    # Não transferir quem já conversa pelo celular da linha (ligado por
    # padrão) e quantos estão assim agora: em Respondeu, com a Corretor, segurados.
    respeita = config_operacional.get(CHAVE_RESPEITA_HUMANO)
    out["respeita_humano"] = respeita if isinstance(respeita, bool) else None
    out["segurados_humano"] = contar_segurados(session, tenant_id)

    # IA do lado (05/10/2026): resumo no card e opinião no que a regra não
    # entendeu. As duas chaves, o corte e o que fizeram nos últimos 30 dias.
    from app.services import ia_do_lado

    out["ia"] = ia_do_lado.para_tela(session, tenant_id)

    # Quem está parado em Respondeu com a Corretor: é o que a roleta vazia deixa
    # acumular, e a tela precisa mostrar o tamanho.
    out["em_respondeu"] = int(session.execute(
        sql_text("SELECT count(*) FROM leads WHERE tenant_id = :t "
                 "AND stage_id = :r AND owner_id = :corretor"),
        {"t": tenant_id, "r": cb.STAGE_RESPONDEU, "corretor": cb.CORRETORA_CORRETOR},
    ).scalar() or 0)
    return out


# ── Segurado por conversa no celular ─────────────────────────────────────────

_SQL_SEGURADOS = """
    SELECT count(*) FROM leads l
     WHERE l.tenant_id = :t AND l.stage_id = :r AND l.owner_id = :corretor
       AND EXISTS (SELECT 1 FROM lead_events e
                    WHERE e.lead_id = l.id AND e.tipo = :ev)
"""


def contar_segurados(session: Session, tenant_id: int) -> int:
    """Quantos cards estão em Respondeu, com a Corretor, segurados porque alguém da
    casa já conversava pelo celular da linha. Quem foi transferido na mão sai
    da conta sozinho (o dono deixa de ser a Corretor)."""
    from app.services import carrossel_corretor as cb

    return int(session.execute(
        sql_text(_SQL_SEGURADOS),
        {"t": tenant_id, "r": cb.STAGE_RESPONDEU, "corretor": cb.CORRETORA_CORRETOR,
         "ev": EVENTO_SEGURADO},
    ).scalar() or 0)


def segurados_por_lead(session: Session, lead_ids: list[int]) -> dict[int, dict[str, Any]]:
    """lead_id → o último `handoff_segurado` ({motivo, at}) dos cards pedidos
    que ainda estão com a Corretor. Nunca levanta (é informação de card)."""
    from app.services import carrossel_corretor as cb

    if not lead_ids:
        return {}
    try:
        linhas = session.execute(
            sql_text(
                """SELECT DISTINCT ON (e.lead_id) e.lead_id, e.payload->>'motivo', e.at
                     FROM lead_events e JOIN leads l ON l.id = e.lead_id
                    WHERE e.lead_id = ANY(:ids) AND e.tipo = :ev AND l.owner_id = :corretor
                    ORDER BY e.lead_id, e.at DESC"""
            ),
            {"ids": list(lead_ids), "ev": EVENTO_SEGURADO, "corretor": cb.CORRETORA_CORRETOR},
        ).all()
    except Exception as e:  # noqa: BLE001
        log.warning("segurados_por_lead_indisponivel", erro=str(e)[:120])
        return {}
    return {int(r[0]): {"motivo": r[1], "at": r[2]} for r in linhas}
