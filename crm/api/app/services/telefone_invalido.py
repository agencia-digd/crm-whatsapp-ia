"""Marca telefone que não existe no WhatsApp, pra parar de tentar pra sempre.

O problema (medido em 03-04/08/2026): a taxa de falha de entrega da linha da Corretor
— o 0000, telefone DA CASA — subiu de 2,3% pra 15% em três dias. Os números que
falhavam tinham a mesma assinatura: **nunca receberam nada com sucesso e nunca
responderam**. Não é gente que bloqueou; é número que não existe no WhatsApp,
resto da base velha da era Acme.

E eles voltavam. O carrossel devolve o lead pra fila depois de ~30 dias de
descanso, então o mesmo número morto era discado de novo, pra sempre, gastando
tentativa e acumulando sinal ruim na Meta.

`leads.telefone_invalido` existia e estava `false` em todos os 7.655 leads —
nada escrevia nele.

A REGRA É CONSERVADORA DE PROPÓSITO: só marca quem falhou E nunca teve UMA
entrega bem-sucedida E nunca respondeu. Se o contato já conversou com a gente
antes, uma falha isolada não marca nada — falha temporária existe (aparelho
fora do ar, problema na nossa conta, limite atingido), e é melhor deixar passar
número ruim do que queimar contato bom.

Reversível: `desmarcar()` limpa, e o `motivo` fica registrado pra auditoria.
"""
from __future__ import annotations

import structlog
from sqlalchemy import text as sql_text

from app.db.session import SessionLocal

log = structlog.get_logger()


def _suf8(telefone: str | None) -> str:
    """Últimos 8 dígitos — mesma heurística de casamento do resto do sistema
    (DDI, DDD e nono dígito variam entre as fontes)."""
    d = "".join(c for c in (telefone or "") if c.isdigit())
    return d[-8:] if len(d) >= 8 else ""


def nunca_teve_sucesso(telefone: str) -> bool:
    """True se o número nunca recebeu com sucesso e nunca respondeu.

    É o que separa "número não existe" de "pessoa bloqueou" ou "falha do dia".
    """
    suf = _suf8(telefone)
    if not suf:
        return False
    with SessionLocal() as s:
        houve = s.execute(
            sql_text(
                """SELECT 1 FROM mensagens_whatsapp
                    WHERE right(regexp_replace(coalesce(contato_telefone,''),
                                               '\\D', '', 'g'), 8) = :suf
                      AND (NOT from_corretora
                           OR conteudo_metadata->>'delivery_status'
                               IN ('delivered', 'read'))
                    LIMIT 1"""
            ),
            {"suf": suf},
        ).first()
    return houve is None


def marcar_se_morto(telefone: str, *, motivo: str = "falha_de_entrega") -> bool:
    """Marca o telefone como inválido SE ele nunca deu sinal de vida.

    Chamado quando a Meta devolve `failed`. Best-effort: erro aqui não pode
    derrubar o webhook de status.
    """
    suf = _suf8(telefone)
    if not suf:
        return False
    try:
        if not nunca_teve_sucesso(telefone):
            return False  # já conversou com a gente — falha isolada não marca
        with SessionLocal() as s:
            res = s.execute(
                sql_text(
                    """UPDATE leads
                          SET telefone_invalido = TRUE, updated_at = NOW()
                        WHERE right(regexp_replace(coalesce(telefone,''),
                                                   '\\D', '', 'g'), 8) = :suf
                          AND telefone_invalido = FALSE"""
                ),
                {"suf": suf},
            )
            s.commit()
        n = res.rowcount or 0
        if n:
            log.info("telefone_marcado_invalido", suf=suf, leads=n, motivo=motivo)
        return n > 0
    except Exception as e:  # noqa: BLE001
        log.warning("marcar_telefone_invalido_falhou", suf=suf, erro=str(e)[:140])
        return False


def desmarcar(telefone: str) -> int:
    """Desfaz a marcação. Existe porque marcação sem volta é armadilha — se a
    regra errar (ou a Meta devolver `failed` por motivo nosso), tem que dar
    pra reverter sem mexer no banco na mão."""
    suf = _suf8(telefone)
    if not suf:
        return 0
    with SessionLocal() as s:
        res = s.execute(
            sql_text(
                """UPDATE leads SET telefone_invalido = FALSE, updated_at = NOW()
                    WHERE right(regexp_replace(coalesce(telefone,''),
                                               '\\D', '', 'g'), 8) = :suf"""
            ),
            {"suf": suf},
        )
        s.commit()
    return res.rowcount or 0
