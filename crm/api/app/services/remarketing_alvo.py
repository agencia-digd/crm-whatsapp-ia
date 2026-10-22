"""Alvo do remarketing — DE QUAL ETAPA e DE QUEM são os leads que o carrossel pega.

## Por que existe

Até 05/10/2026 a etapa e o dono eram constantes no código (`STAGE_FILA = 23`,
`CORRETORA_CORRETOR = 4`). Pedido da Dona: *"na automação, a gente pode escolher os
leads que estão na etapa tal, tipo na etapa base"*. Agora são duas chaves em
`config_operacional`:

```
remarketing_etapa   23   id em `pipeline_stages` — de onde o carrossel tira os leads
remarketing_dono     4   id em `corretoras`      — só lead DESTE dono entra no lote
```

**Sem linha no banco vale 23 e 4, byte a byte como antes.** Publicar isto não
muda um disparo sequer.

## O dono continua sendo trava, não detalhe

O filtro `owner_id = dono` do `_SQL_FILA` é o que separa "lead do robô" de
"lead de corretora": o handoff troca o dono, e é assim que o lead transferido
sai da roda. Por isso a etapa virou escolha e o dono **não virou "qualquer
um"**: o lote só leva lead do dono configurado. Etapa escolhida com zero lead
desse dono simplesmente não dispara nada — e a tela mostra a contagem ao lado
de cada etapa pra isso não ser surpresa.

⚠️ Tudo que acontece DEPOIS do disparo (a Pessoa atendendo quem responde, o
handoff, o aviso pela linha da corretora) continua amarrado à Corretor
(`carrossel_corretor.CORRETORA_CORRETOR`). Trocar o dono aqui muda quem ENTRA no lote;
não ensina o resto do sistema a tratar a resposta de lead de outra dona.

## O que é recusado, e por quê

- etapa que não existe, ou de **outro cliente** (tenant): o lote sairia vazio
  ou, pior, alguém acharia que configurou;
- **Disparado** e **Respondeu**: são as etapas de dentro da roda. Usar
  Disparado como fila reenviaria pra quem acabou de receber;
- **Não perturbe**: é onde mora quem pediu silêncio;
- dono que não é corretora ativa do cliente.

A validação roda em DOIS lugares de propósito: na gravação (422 em português)
e na leitura do tick (valor que chegou no banco por fora da tela SEGURA o
disparo — não dá pra saber o que a pessoa quis dizer, e adivinhar etapa é como
se manda mensagem pra lista errada).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog
from sqlalchemy import text as sql_text

log = structlog.get_logger()

# Etapas do funil remarketing (migration 0046 + criadas em 26/07).
ETAPA_FILA_PADRAO = 23
ETAPA_DISPARADO = 24
ETAPA_RESPONDEU = 25
# A "Corretor" — dona histórica dos leads da roda.
DONO_PADRAO = 4

CHAVE_ETAPA = "remarketing_etapa"
CHAVE_DONO = "remarketing_dono"


@dataclass(frozen=True)
class Alvo:
    etapa: int
    dono: int

    @property
    def padrao(self) -> bool:
        return self.etapa == ETAPA_FILA_PADRAO and self.dono == DONO_PADRAO


class AlvoInvalido(ValueError):
    """O alvo gravado não serve. `motivo` é o código curto que o tick devolve
    em `motivo_parada`; a mensagem é a explicação em português."""

    def __init__(self, motivo: str, mensagem: str) -> None:
        super().__init__(mensagem)
        self.motivo = motivo


def _etapas_proibidas() -> dict[int, str]:
    from app.services.opt_out import ETAPA_NAO_PERTURBE

    return {
        ETAPA_DISPARADO: (
            "a etapa Disparado é de dentro da roda — usar ela como origem "
            "reenviaria pra quem acabou de receber"
        ),
        ETAPA_RESPONDEU: (
            "a etapa Respondeu é de quem já respondeu ao disparo — eles voltam "
            "pra fila sozinhos depois do descanso"
        ),
        ETAPA_NAO_PERTURBE: (
            "a etapa Não perturbe é de quem pediu pra não receber mensagem"
        ),
    }


# Venda perdida é pra onde vai quem recusou o remarketing ou pediu pra parar
# (05/10/2026). Como origem do disparo ela repescaria exatamente essas pessoas.
# Vai por NOME e não por id: o id muda de cliente pra cliente (13 na Acme).
NOME_VENDA_PERDIDA = "Venda perdida"
MOTIVO_VENDA_PERDIDA = (
    "a etapa Venda perdida é de quem disse não ou pediu pra parar — "
    "usar ela como origem mandaria mensagem de novo pra quem recusou"
)


def _e_venda_perdida(nome: Any) -> bool:
    return str(nome or "").strip().casefold() == NOME_VENDA_PERDIDA.casefold()


# ── Formato (não vai ao banco) ───────────────────────────────────────────────


def _inteiro_positivo(valor: Any, *, nome: str) -> int | None:
    if valor is None:
        return None
    if not isinstance(valor, int) or isinstance(valor, bool):
        raise ValueError(f"{nome} precisa ser o número (id) de uma opção da lista")
    if valor <= 0:
        raise ValueError(f"{nome} inválido: {valor}")
    return valor


# ── Conferência no banco ─────────────────────────────────────────────────────


def conferir_etapa(session, tenant_id: int, etapa: int) -> None:
    """Levanta ValueError (PT-BR) se a etapa não pode ser origem do disparo."""
    proibidas = _etapas_proibidas()
    if etapa in proibidas:
        raise ValueError(proibidas[etapa])
    linha = session.execute(
        sql_text("SELECT tenant_id, nome FROM pipeline_stages WHERE id = :i"),
        {"i": etapa},
    ).first()
    if linha is None:
        raise ValueError(f"a etapa {etapa} não existe")
    if _e_venda_perdida(linha[1]):
        raise ValueError(MOTIVO_VENDA_PERDIDA)
    if int(linha[0]) != int(tenant_id):
        # Mesma mensagem de "não existe" seria mais discreta, mas quem lê isto
        # é a operação da casa, e saber que o id é de outro cliente evita uma
        # tarde procurando erro de digitação.
        raise ValueError(f"a etapa {etapa} é de outro cliente — não pertence a este funil")


def conferir_dono(session, tenant_id: int, dono: int) -> None:
    """Levanta ValueError (PT-BR) se o dono não é corretora ativa do cliente."""
    linha = session.execute(
        sql_text("SELECT tenant_id, ativo FROM corretoras WHERE id = :i"),
        {"i": dono},
    ).first()
    if linha is None or int(linha[0]) != int(tenant_id):
        raise ValueError(f"a corretora {dono} não existe neste cliente")
    if not linha[1]:
        raise ValueError(f"a corretora {dono} está inativa")


def validar_etapa(valor: Any, *, tenant_id: int, session=None) -> int | None:
    """O que a tela grava em `remarketing_etapa`. None = volta pro padrão."""
    etapa = _inteiro_positivo(valor, nome="a etapa")
    if etapa is None:
        return None
    _com_sessao(session, lambda s: conferir_etapa(s, tenant_id, etapa))
    return etapa


NOME_NAO_PERTURBE = "Não perturbe"


def conferir_etapa_parar(session, tenant_id: int, etapa: Any) -> None:
    """Levanta ValueError (PT-BR) se a etapa não pode receber quem pediu pra
    PARAR. Só duas servem: "Venda perdida" e "Não perturbe" DESTE cliente —
    são as duas que a roda não lê de volta (nenhuma pode ser origem do
    disparo, e a volta dos 30/45 dias não olha pra elas)."""
    etapa = _inteiro_positivo(etapa, nome="a etapa de quem pede pra parar")
    linha = session.execute(
        sql_text("SELECT tenant_id, nome FROM pipeline_stages WHERE id = :i"),
        {"i": etapa},
    ).first()
    if linha is None or int(linha[0]) != int(tenant_id):
        raise ValueError(f"a etapa {etapa} não existe neste cliente")
    nome = str(linha[1] or "").strip().casefold()
    if nome not in (NOME_VENDA_PERDIDA.casefold(), NOME_NAO_PERTURBE.casefold()):
        raise ValueError(
            'quem pede pra parar só pode ir pra "Venda perdida" ou "Não perturbe" '
            f'— "{linha[1]}" não é nenhuma das duas'
        )


def validar_etapa_parar(valor: Any, *, tenant_id: int, session=None) -> int | None:
    """O que a tela grava em `remarketing_etapa_parar`. None = Venda perdida."""
    if valor is None:
        return None
    _com_sessao(session, lambda s: conferir_etapa_parar(s, tenant_id, valor))
    return int(valor)


def destino_parar(session, tenant_id: int) -> dict[str, Any]:
    """O que a tela precisa pro seletor de "quem pede pra parar vai pra onde":
    as duas etapas que servem (com o id DESTE cliente, por nome, nunca cravado)
    e qual vale agora — a mesma conta de `carrossel_corretor._stage_do_parar`.

    `etapa: None` = o cliente não tem "Venda perdida" no funil principal (o
    motor também não acha). `invalido` = há valor gravado que o motor recusa;
    ele cai na Venda perdida, e a tela diz.
    """
    from app.services import config_operacional

    linhas = session.execute(
        sql_text(
            """SELECT id, nome, funil FROM pipeline_stages
                WHERE tenant_id = :t AND lower(btrim(nome)) IN (:vp, :np)
                ORDER BY (funil = 'principal') DESC, id"""
        ),
        {"t": tenant_id, "vp": NOME_VENDA_PERDIDA.casefold(),
         "np": NOME_NAO_PERTURBE.casefold()},
    ).all()
    perdida = next((int(r[0]) for r in linhas
                    if _e_venda_perdida(r[1]) and r[2] == "principal"), None)
    perturbe = next((int(r[0]) for r in linhas if not _e_venda_perdida(r[1])), None)
    opcoes = [
        {"id": i, "nome": nome, "padrao": padrao}
        for i, nome, padrao in ((perdida, NOME_VENDA_PERDIDA, True),
                                (perturbe, NOME_NAO_PERTURBE, False))
        if i is not None
    ]
    gravada = config_operacional.get("remarketing_etapa_parar")
    invalido: str | None = None
    efetiva = perdida
    if gravada is not None:
        try:
            conferir_etapa_parar(session, tenant_id, gravada)
            efetiva = int(gravada)
        except ValueError as e:
            invalido = str(e)[:200]
    return {"etapa": efetiva, "gravada": gravada, "padrao": gravada is None,
            "invalido": invalido, "opcoes": opcoes}


def validar_dono(valor: Any, *, tenant_id: int, session=None) -> int | None:
    """O que a tela grava em `remarketing_dono`. None = volta pro padrão."""
    dono = _inteiro_positivo(valor, nome="o dono")
    if dono is None:
        return None
    _com_sessao(session, lambda s: conferir_dono(s, tenant_id, dono))
    return dono


def _com_sessao(session, fn) -> None:
    if session is not None:
        fn(session)
        return
    from app.db.session import SessionLocal

    with SessionLocal() as s:
        fn(s)


# ── Leitura (o que o tick usa) ───────────────────────────────────────────────


def ler(session, tenant_id: int) -> Alvo:
    """Etapa e dono que valem agora. Levanta `AlvoInvalido` — o tick SEGURA.

    Com as duas chaves no padrão não consulta o banco: é o caminho de sempre,
    sem query nova no meio do tick.
    """
    from app.services import config_operacional

    bruto_etapa = config_operacional.get(CHAVE_ETAPA)
    bruto_dono = config_operacional.get(CHAVE_DONO)
    try:
        etapa = _inteiro_positivo(bruto_etapa, nome="a etapa") or ETAPA_FILA_PADRAO
    except ValueError as e:
        raise AlvoInvalido("etapa_invalida", f"{CHAVE_ETAPA}: {e}") from e
    try:
        dono = _inteiro_positivo(bruto_dono, nome="o dono") or DONO_PADRAO
    except ValueError as e:
        raise AlvoInvalido("dono_invalido", f"{CHAVE_DONO}: {e}") from e

    alvo = Alvo(etapa=etapa, dono=dono)
    if alvo.padrao:
        return alvo
    try:
        if etapa != ETAPA_FILA_PADRAO:
            conferir_etapa(session, tenant_id, etapa)
    except ValueError as e:
        raise AlvoInvalido("etapa_invalida", f"{CHAVE_ETAPA}: {e}") from e
    try:
        if dono != DONO_PADRAO:
            conferir_dono(session, tenant_id, dono)
    except ValueError as e:
        raise AlvoInvalido("dono_invalido", f"{CHAVE_DONO}: {e}") from e
    return alvo


MOTIVO_EM_PORTUGUES: dict[str, str] = {
    "etapa_invalida": "a etapa escolhida pro disparo não serve — segurando",
    "dono_invalido": "o dono configurado pro disparo não serve — segurando",
}


# ── O que a tela mostra ──────────────────────────────────────────────────────


def etapas_do_cliente(session, tenant_id: int, dono: int) -> list[dict[str, Any]]:
    """Etapas REAIS do funil do cliente, pro seletor da tela.

    `leads_do_dono` vai junto porque é o número que decide: etapa com 200 leads
    de corretora e zero do dono configurado não dispara nada, e escolher sem
    ver isso seria configurar um disparo que nunca sai.
    """
    proibidas = _etapas_proibidas()
    linhas = session.execute(
        sql_text(
            """
            SELECT s.id, s.nome, s.funil, s.ordem, s.tipo,
                   count(l.id)                                   AS leads,
                   count(l.id) FILTER (WHERE l.owner_id = :dono) AS leads_do_dono
              FROM pipeline_stages s
              LEFT JOIN leads l
                     ON l.stage_id = s.id AND l.tenant_id = s.tenant_id
             WHERE s.tenant_id = :t
             GROUP BY s.id, s.nome, s.funil, s.ordem, s.tipo
             ORDER BY s.funil, s.ordem NULLS LAST, s.id
            """
        ),
        {"t": tenant_id, "dono": dono},
    ).mappings().all()
    return [
        {
            "id": int(r["id"]),
            "nome": r["nome"],
            "funil": r["funil"],
            "tipo": r["tipo"],
            "leads": int(r["leads"]),
            "leads_do_dono": int(r["leads_do_dono"]),
            "permitida": int(r["id"]) not in proibidas
            and not _e_venda_perdida(r["nome"]),
            "motivo_bloqueio": proibidas.get(int(r["id"])) or (
                MOTIVO_VENDA_PERDIDA if _e_venda_perdida(r["nome"]) else None
            ),
            "padrao": int(r["id"]) == ETAPA_FILA_PADRAO,
        }
        for r in linhas
    ]


def resumo(session, tenant_id: int) -> dict[str, Any]:
    """Alvo gravado + as etapas do cliente. Nunca levanta por causa de alvo
    inválido: devolve `invalido` com o motivo, que é o que a tela precisa dizer."""
    from app.services import config_operacional

    out: dict[str, Any] = {"invalido": None, "motivo": None}
    try:
        alvo = ler(session, tenant_id)
        out.update(etapa=alvo.etapa, dono=alvo.dono, padrao=alvo.padrao)
        dono_das_contas = alvo.dono
    except AlvoInvalido as e:
        bruto = config_operacional.get(CHAVE_DONO)
        dono_das_contas = bruto if isinstance(bruto, int) and not isinstance(bruto, bool) \
            else DONO_PADRAO
        out.update(etapa=None, dono=None, padrao=False,
                   invalido=str(e)[:200], motivo=e.motivo)
    nome = session.execute(
        sql_text("SELECT nome FROM corretoras WHERE id = :i"), {"i": dono_das_contas}
    ).scalar()
    out["dono_nome"] = nome
    out["etapas"] = etapas_do_cliente(session, tenant_id, dono_das_contas)
    out["etapa_padrao"] = ETAPA_FILA_PADRAO
    out["dono_padrao"] = DONO_PADRAO
    return out
