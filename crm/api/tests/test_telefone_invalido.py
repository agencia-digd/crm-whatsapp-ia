"""Marcação de telefone que não existe no WhatsApp.

O que motivou (medido em 03-04/08/2026): a taxa de falha do 0000 — telefone DA
CASA — foi de 2,3% a 15% em três dias. Os números que falhavam nunca tinham
recebido nada com sucesso nem respondido: base velha da era Acme, número
que não existe mais. E voltavam pra fila a cada ~30 dias pra falhar de novo.

O risco desta feature é o OPOSTO do problema: marcar demais. Se a Meta devolver
`failed` por motivo nosso (token vencido, template reprovado, limite estourado),
uma regra frouxa marcaria contato bom em lote e a gente perderia base viva sem
perceber. Por isso a regra é conservadora, e é isso que os testes seguram.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text as sql_text

from app.db.session import SessionLocal
from app.services import telefone_invalido as ti

TEL_MORTO = "+5500000000000"
TEL_VIVO = "+5500000000000"
TEL_RESPONDEU = "+5500000000000"


def _limpa():
    with SessionLocal() as s:
        s.execute(sql_text(
            "DELETE FROM mensagens_whatsapp WHERE contato_telefone LIKE '+5500000000000%'"))
        s.execute(sql_text("DELETE FROM leads WHERE telefone LIKE '+5500000000000%'"))
        s.commit()


@pytest.fixture(autouse=True)
def _ambiente():
    _limpa()
    yield
    _limpa()


def _lead(tel: str) -> None:
    with SessionLocal() as s:
        s.execute(sql_text(
            "INSERT INTO leads (tenant_id, nome, telefone, created_at, updated_at, "
            "nome_editado_manual, telefone_editado_manual, owner_editado_manual, "
            "telefone_invalido) VALUES (1, 'TST', :t, NOW(), NOW(), "
            "FALSE, FALSE, FALSE, FALSE)"), {"t": tel})
        s.commit()


def _msg(tel: str, *, de_corretora: bool, status: str | None) -> None:
    meta = f'{{"delivery_status": "{status}"}}' if status else "{}"
    with SessionLocal() as s:
        s.execute(sql_text(
            "INSERT INTO mensagens_whatsapp (id, evolution_message_id, instance, "
            "remote_jid, contato_telefone, from_corretora, tipo_mensagem, "
            "conteudo_texto, conteudo_metadata, timestamp_evolution, recebido_em) "
            "VALUES (gen_random_uuid(), :w, 'tst', :j, :t, :fc, 'text', 'oi', "
            "CAST(:m AS jsonb), NOW(), NOW())"),
            {"w": f"wamid.{tel}.{status}.{de_corretora}", "j": f"{tel}@s.whatsapp.net",
             "t": tel, "fc": de_corretora, "m": meta})
        s.commit()


def _invalido(tel: str) -> bool:
    with SessionLocal() as s:
        return bool(s.execute(sql_text(
            "SELECT telefone_invalido FROM leads WHERE telefone = :t"), {"t": tel}
        ).scalar())


# ── Marca o que tem que marcar ──────────────────────────────────────────────


def test_marca_quem_so_falhou():
    """A assinatura do número morto: falhou, nunca entregou, nunca respondeu."""
    _lead(TEL_MORTO)
    _msg(TEL_MORTO, de_corretora=True, status="failed")
    assert ti.marcar_se_morto(TEL_MORTO) is True
    assert _invalido(TEL_MORTO) is True


# ── E principalmente: NÃO marca o que não deve ──────────────────────────────


def test_nao_marca_quem_ja_recebeu_com_sucesso():
    """Falha isolada em quem já recebeu antes é falha temporária — aparelho
    fora do ar, problema da nossa conta. Marcar aqui queima contato bom."""
    _lead(TEL_VIVO)
    _msg(TEL_VIVO, de_corretora=True, status="delivered")
    _msg(TEL_VIVO, de_corretora=True, status="failed")
    assert ti.marcar_se_morto(TEL_VIVO) is False
    assert _invalido(TEL_VIVO) is False


def test_nao_marca_quem_respondeu():
    """Quem já escreveu pra gente existe, ponto — mesmo que uma mensagem falhe
    depois."""
    _lead(TEL_RESPONDEU)
    _msg(TEL_RESPONDEU, de_corretora=False, status=None)  # inbound
    _msg(TEL_RESPONDEU, de_corretora=True, status="failed")
    assert ti.marcar_se_morto(TEL_RESPONDEU) is False
    assert _invalido(TEL_RESPONDEU) is False


def test_read_tambem_conta_como_sinal_de_vida():
    _lead(TEL_VIVO)
    _msg(TEL_VIVO, de_corretora=True, status="read")
    _msg(TEL_VIVO, de_corretora=True, status="failed")
    assert ti.marcar_se_morto(TEL_VIVO) is False


def test_telefone_curto_ou_vazio_nao_quebra():
    assert ti.marcar_se_morto("") is False
    assert ti.marcar_se_morto("123") is False
    assert ti.nunca_teve_sucesso("") is False


# ── Reversível ──────────────────────────────────────────────────────────────


def test_da_pra_desmarcar():
    """Marcação sem volta é armadilha: se a regra errar, tem que dar pra
    reverter sem mexer no banco na mão."""
    _lead(TEL_MORTO)
    _msg(TEL_MORTO, de_corretora=True, status="failed")
    ti.marcar_se_morto(TEL_MORTO)
    assert _invalido(TEL_MORTO) is True
    assert ti.desmarcar(TEL_MORTO) >= 1
    assert _invalido(TEL_MORTO) is False


# ── O carrossel respeita ────────────────────────────────────────────────────


def test_carrossel_pula_telefone_invalido():
    """Sem isso a marcação não serve pra nada: o número morto volta pra fila
    depois do descanso de 30 dias e falha de novo, pra sempre."""
    from app.services import carrossel_corretor

    assert "telefone_invalido IS NOT TRUE" in carrossel_corretor._SQL_FILA
