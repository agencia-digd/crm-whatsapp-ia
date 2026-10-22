"""Placar de entrega do remarketing — o que a Meta ENTREGOU, não o que aceitou.

## Por que existe

O disparo sempre soube contar o que mandou. O que ele não sabia era o que
chegou: o status de entrega mora em `mensagens_whatsapp.conteudo_metadata`
(gravado pelo webhook de cada linha, por wamid), e o registro do disparo não
guardava o wamid. Os dois lados existiam no banco e não se encontravam.

Medido em 04/10/2026 na Fulano (outro motor, mesmo buraco): 909 mensagens, 368
falhas (40,5%), 361 delas `131026 Message undeliverable` — e o relatório
dizendo 100% de sucesso. Se o carrossel rodasse aquela base, este placar é o
que faria a Dona ver os 40% sem abrir o banco.

## De onde vêm os envios

Duas fontes, porque o remarketing da Acme grava em dois lugares:

- **carrossel** — `lead_events` tipo `carrossel_disparo`; o wamid vai no
  `payload` (gravado em `carrossel_corretor._disparar`). O carrossel NÃO escreve em
  `disparos_reengajamento` no envio (só no opt-out), e não passou a escrever:
  aquela tabela alimenta o cooldown e o cap do disparo manual do painel, e um
  envio do robô ali mudaria quem a corretora consegue reengajar à mão.
- **painel** — `disparos_reengajamento.wamid` (migration 0087), o
  reengajamento que a corretora dispara pela tela.

## O que cada balde significa

```
entregues    delivered + read          chegou no aparelho
em_voo       sent + accepted + nada    a Meta aceitou e ainda não confirmou
falhas       failed                    não chegou, e há motivo
sem_rastro   envio sem wamid           anterior a 04/10/2026 — não dá pra saber
```

⚠️ **`accepted` e `sent` NÃO são entrega.** Contar aceite como entrega é o erro
que já custou caro; aqui eles ficam em `em_voo` e nunca somam em `entregues`.

`sem_rastro` não é falha nem entrega: é ausência de dado, e aparece como tal.
Os quatro baldes sempre somam `mandados`.

## Culpa da falha

A tabela de códigos é CÓPIA da do conferente de lote da Central
(`projetos/api/scripts/conferente_disparo.py`, `culpa_de`) — mesma
classificação nos dois sistemas, de propósito. Mudou lá, muda aqui. Código que
não está na tabela vira `desconhecida`: chutar "lista" num erro novo mandaria
limpar uma base boa.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

ENTREGUE = ("delivered", "read")
EM_VOO = ("sent", "accepted")

# Códigos da Meta por culpa — cópia de `conferente_disparo.py` (projetos/api).
CULPA_DESTINATARIO = {"131026", "131047", "131049", "470"}
CULPA_CONTA = {"131042", "368", "131031"}
CULPA_TEMPLATE = {"132000", "132001", "132005", "132007", "132012", "132015"}
_MARCAS_CONTA = ("payment", "eligibility", "business account", "sem qualidade")

FONTES = ("carrossel", "painel")

# Linha de `disparos_reengajamento` com este autor não é envio: é o marcador
# que `carrossel_corretor._gravar_opt_out` insere só pra segurar o opt-out.
_AUTOR_MARCADOR_OPT_OUT = "carrossel"


def culpa_de(codigo: str | None, titulo: str | None) -> str:
    """Quem precisa agir: 'lista', 'conta', 'template' ou 'desconhecida'."""
    c = (codigo or "").strip()
    if c in CULPA_DESTINATARIO:
        return "lista"
    if c in CULPA_CONTA:
        return "conta"
    if c in CULPA_TEMPLATE:
        return "template"
    if any(m in (titulo or "").lower() for m in _MARCAS_CONTA):
        return "conta"
    return "desconhecida"


def balde_de(wamid: str | None, status: str | None) -> str:
    """Em qual dos quatro baldes o envio cai. Função pura.

    Status que a Meta inventar amanhã cai em `em_voo`: não é entrega confirmada
    e não é falha declarada — o lado seguro é não contar como chegou.
    """
    if not wamid:
        return "sem_rastro"
    st = (status or "").strip().lower()
    if st in ENTREGUE:
        return "entregues"
    if st == "failed":
        return "falhas"
    return "em_voo"


_SQL_ENVIOS = """
WITH envios AS (
    SELECT 'carrossel' AS fonte, e.payload->>'wamid' AS wamid
      FROM lead_events e
     WHERE e.tenant_id = :t AND e.tipo = :ev
       AND (e.at AT TIME ZONE 'America/Sao_Paulo')::date BETWEEN :de AND :ate
    UNION ALL
    SELECT 'painel' AS fonte, d.wamid
      FROM disparos_reengajamento d
     WHERE d.tenant_id = :t
       AND d.enviado_por IS DISTINCT FROM :marcador
       AND (d.enviado_em AT TIME ZONE 'America/Sao_Paulo')::date BETWEEN :de AND :ate
)
SELECT en.fonte, en.wamid,
       m.meta->>'delivery_status'     AS status,
       m.meta->>'delivery_error_code' AS codigo,
       m.meta->>'delivery_error'      AS titulo
  FROM envios en
  -- LATERAL + LIMIT 1: o wamid é único por (wamid, instance), não sozinho. Um
  -- JOIN simples contaria o mesmo envio duas vezes se a mensagem existisse em
  -- duas instâncias.
  LEFT JOIN LATERAL (
        SELECT w.conteudo_metadata AS meta
          FROM mensagens_whatsapp w
         WHERE w.evolution_message_id = en.wamid
         ORDER BY (w.conteudo_metadata ? 'delivery_status') DESC
         LIMIT 1
  ) m ON TRUE
"""


def placar(
    session: Session,
    de: date,
    ate: date,
    *,
    tenant_id: int = 1,
    fontes: tuple[str, ...] = FONTES,
) -> dict[str, Any]:
    """Placar de entrega dos disparos entre `de` e `ate` (dias de São Paulo,
    inclusive nos dois lados).

    Levanta se o banco falhar — quem chama decide como mostrar "não consegui
    ler". Devolver zeros aqui seria dizer "não falhou nada" sem ter olhado.
    """
    from app.services.carrossel_corretor import EVENT_DISPARO

    linhas = session.execute(
        sql_text(_SQL_ENVIOS),
        {"t": tenant_id, "ev": EVENT_DISPARO, "de": de, "ate": ate,
         "marcador": _AUTOR_MARCADOR_OPT_OUT},
    ).all()

    r: dict[str, Any] = {
        "de": de.isoformat(), "ate": ate.isoformat(),
        "mandados": 0, "entregues": 0, "em_voo": 0, "falhas": 0, "sem_rastro": 0,
    }
    motivos: dict[tuple[str | None, str | None], int] = {}
    culpas: dict[str, int] = {}
    for fonte, wamid, status, codigo, titulo in linhas:
        if fonte not in fontes:
            continue
        balde = balde_de(wamid, status)
        r["mandados"] += 1
        r[balde] += 1
        if balde == "falhas":
            motivos[(codigo, titulo)] = motivos.get((codigo, titulo), 0) + 1
            culpa = culpa_de(codigo, titulo)
            culpas[culpa] = culpas.get(culpa, 0) + 1

    # Taxa sobre o que TEM rastro: envio sem wamid não pode entrar no
    # denominador, senão um lote antigo dilui a falha de um lote novo.
    rastreados = r["mandados"] - r["sem_rastro"]
    r["pct_falha"] = round(100 * r["falhas"] / rastreados, 1) if rastreados else None
    r["pct_entregue"] = (
        round(100 * r["entregues"] / rastreados, 1) if rastreados else None
    )
    r["culpas"] = culpas
    r["motivos"] = [
        {"codigo": c, "titulo": t, "n": n, "culpa": culpa_de(c, t)}
        for (c, t), n in sorted(motivos.items(), key=lambda kv: -kv[1])
    ]
    return r


def ultimo_lote(session: Session, *, tenant_id: int = 1) -> dict[str, Any] | None:
    """Placar do último dia em que o CARROSSEL disparou. None se nunca disparou.

    "Lote" é o dia de São Paulo: é a unidade em que o carrossel trabalha (cap e
    rampa são diários) e a que a Dona pergunta ("como foi o disparo de ontem?").
    """
    from app.services.carrossel_corretor import EVENT_DISPARO

    dia = session.execute(
        sql_text(
            "SELECT max((at AT TIME ZONE 'America/Sao_Paulo')::date) "
            "FROM lead_events WHERE tenant_id = :t AND tipo = :ev"
        ),
        {"t": tenant_id, "ev": EVENT_DISPARO},
    ).scalar()
    if dia is None:
        return None
    return placar(session, dia, dia, tenant_id=tenant_id, fontes=("carrossel",))
