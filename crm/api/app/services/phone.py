"""Normalização de telefones BR pra E.164 sem '+'."""
from __future__ import annotations

import re


def normalize_phone(raw: str) -> str:
    """
    E.164 sem '+'. Aceita celular (13 díg, com 9) e fixo (12 díg) BR.

    - retira não-dígito
    - 12-13 díg começando com '55' → mantém
    - 10-11 díg → assume BR, prepend '55'
    - else → ValueError
    """
    if not raw or not isinstance(raw, str):
        raise ValueError(f"telefone vazio ou inválido: {raw!r}")
    digits = re.sub(r"\D", "", raw)
    if not digits:
        raise ValueError(f"sem dígitos: {raw!r}")

    # Já vem com 55 → manter (se 12 ou 13 dígitos)
    if digits.startswith("55") and len(digits) in (12, 13):
        return digits

    # Sem 55, 10 ou 11 dígitos (ddd + número) → assume BR
    if len(digits) in (10, 11):
        # remove '0' inicial de DDD se vier com '015...' (já foi removido pelo regex)
        return "55" + digits

    raise ValueError(f"formato desconhecido: {raw!r} → digits={digits}")


def looks_like_brazilian_mobile(phone_normalized: str) -> bool:
    """13 díg, começa com 55, 9º char é '9'."""
    return (
        len(phone_normalized) == 13
        and phone_normalized.startswith("55")
        and phone_normalized[4] == "9"
    )


# ── Forma canônica (L3 · identidade de lead) ─────────────────────────────────
#
# UMA definição de identidade de telefone pra TODOS os writers da espinha.
# Motivo (auditoria 26/07): o intake dedupava por sufixo-8, o shadow do Kommo
# por external_ref, e a base tinha 48 leads com 12 dígitos contra 6.404 com 13
# — a mesma pessoa virava 2 cards no board da corretora.
#
# Regra do 9º dígito, deliberadamente CONSERVADORA:
#   len==13 → já canônico.
#   len==12 → só insere o '9' quando o número de assinante (8 díg) JÁ COMEÇA
#             com '9' (celular pós-migração que perdeu o dígito, caso
#             inequívoco: 34 dos 48 na base).
#   len==12 começando com '3' (5 na base) → FIXO legítimo, canônico como está.
#             A faixa +5500000000000... é a própria linha 0000 da Acme — inserir
#             o 9 nela corromperia o número.
#   len==12 começando com 7/8 (9 na base) → AMBÍGUO. Não decide: devolve
#             canônico=None e ambiguo=True, pra triagem humana. Nunca
#             auto-reparo, nunca descarte pro limbo.


# TODO(L3 · fora do escopo deste lote): existem 4 cópias históricas do
# normalizador que NÃO passam por `canonical_phone` — enquanto elas viverem, o
# dado ruim (12 dígitos sem o 9) reentra por um caminho não corrigido, mesmo
# com o intake e o shadow consertados. Migrar cada uma pra este módulo:
#   - app/api/kommo_webhook.py::_normalizar_telefone_kommo
#   - app/tools/kommo.py::_normalize_phone
#   - app/inspector/conversation_reader.py::_normalize_phone
#   - scripts/sync_atendimentos_evolution.py::_normalize_phone


def phone_digits(raw: str | None) -> str:
    """Só os dígitos de `raw` (nunca levanta; '' se vazio/None)."""
    if not raw:
        return ""
    return re.sub(r"\D", "", str(raw))


def canonical_phone(raw: str | None) -> str | None:
    """Forma canônica de identidade: 13 dígitos ('55' + DDD + 9 + 8 díg) pra
    celular, 12 dígitos pra fixo. `None` quando não dá pra decidir com
    segurança (vazio, lixo, ou os ~9 casos ambíguos de 12 dígitos).

    Use ESTA função em todo writer/matcher da espinha. Ela é simétrica: reduz
    tanto o lado de 12 quanto o de 13 dígitos à MESMA string antes de comparar.
    """
    canon, _ = canonical_phone_info(raw)
    return canon


def canonical_phone_info(raw: str | None) -> tuple[str | None, bool]:
    """`(canonico, ambiguo)`.

    `ambiguo=True` significa "tem cara de telefone mas não dá pra decidir se
    falta o 9" → o caller deve marcar pra revisão humana (telefone_invalido),
    NÃO filtrar e NÃO gravar como canônico.
    """
    base = normalize_phone_kommo(raw)
    if base is None:
        return None, False

    digits = phone_digits(base)

    # Guarda contra o "55" duplicado: `normalize_phone` prefixa '55' em
    # qualquer coisa de 10-11 dígitos, inclusive num número que JÁ começa com
    # 55 mas está truncado (produção: lead 143 '+5500000000000', 11 díg → viraria
    # '+5500000000000'). DDD '55' existe (RS/Santa Rosa), então só rejeitamos
    # quando o resultado ficou com '5555' na frente E o original já tinha o 55.
    original = phone_digits(raw)
    if digits.startswith("5555") and original.startswith("55") and len(original) in (10, 11):
        return None, False
    if len(digits) == 13:
        return digits, False

    if len(digits) == 12:
        assinante = digits[4:]  # 8 dígitos do assinante (sem 55 e sem DDD)
        primeiro = assinante[0] if assinante else ""
        if primeiro == "9":
            # Celular que perdeu o 9 na migração: 55 + DDD + '9' + assinante.
            return digits[:4] + "9" + assinante, False
        if primeiro in ("2", "3", "4", "5"):
            # Fixo legítimo (inclui a faixa 3211 da própria linha 0000).
            return digits, False
        # 6/7/8 → pode ser fixo antigo OU celular sem o 9. Não decide.
        return None, True

    return None, False


def same_phone(a: str | None, b: str | None) -> bool:
    """True se `a` e `b` são o MESMO telefone na forma canônica.

    Comparação de identidade completa (país+DDD+assinante) — nunca por
    sufixo-8, que colide entre DDDs diferentes. Ambíguo nunca casa.
    """
    ca = canonical_phone(a)
    if ca is None:
        return False
    return ca == canonical_phone(b)


def same_ddd(a: str | None, b: str | None) -> bool:
    """True se os dois telefones têm o MESMO DDD (predicado que mata a colisão
    cross-DDD do match por sufixo). Sem canônico dos dois lados → False."""
    da, db = phone_digits(a), phone_digits(b)
    if len(da) < 4 or len(db) < 4:
        return False
    return da[2:4] == db[2:4]


def normalize_phone_kommo(raw: str | None) -> str | None:
    """`normalize_phone` tolerante: aceita None, retorna None em inválido (nunca
    levanta) e trata o formato legado do Kommo com '0' na frente do DDD
    (ex: '+5500000000000' → '+5500000000000').

    Mesmo tratamento de app/api/kommo_webhook._normalizar_telefone_kommo e
    scripts/sync_atendimentos_evolution._normalize_phone (cópias históricas —
    código novo deve usar ESTA função).
    """
    if not raw:
        return None
    digits = re.sub(r"\D", "", str(raw))
    if not (digits.startswith("55") and len(digits) in (12, 13)):
        digits = digits.lstrip("0")
    if not digits:
        return None
    try:
        return normalize_phone(digits)
    except ValueError:
        return None
