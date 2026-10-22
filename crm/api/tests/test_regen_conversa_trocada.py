"""
Regressão da Revisão 03 · I1 (conversa trocada) + I2 (transcrição invisível)
no script de regeneração `regen_sugestoes_brain._pull_conv`.

I1: o fallback antigo por NOME (`contato_nome ILIKE '%primeiro%'`) fazia "Sandra"
    casar com "AlesSANDRA" e "Ricardo" com "Júlio Ricardo" → conversa de OUTRO lead
    entrava na análise. O fix removeu o fallback: quando o telefone do lead não tem
    mensagem de lead (só HSM cego), a conversa NÃO deve puxar o chat de outra pessoa.

I2: `_rows_to_conv` não passava `transcricao`/`transcricao_status`, então o analyzer
    via "[áudio não disponível]" mesmo com transcrição concluída no banco. O fix
    passa os campos no formato do conversation_reader.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.db.session import SessionLocal
from scripts.regen_sugestoes_brain import _pull_conv, _rows_to_conv

_INSTANCE = "test_regen_i1"
_CID = 3  # corretora existente (Pessoa12)


def _ins(s, *, tel, nome, from_corretora, tipo, texto, transc=None, transc_status="pendente", ts=None):
    ts = ts or datetime.now(timezone.utc)
    s.execute(text("""
        INSERT INTO mensagens_whatsapp
          (evolution_message_id, instance, corretora_id, remote_jid, contato_telefone,
           contato_nome, from_corretora, tipo_mensagem, conteudo_texto, timestamp_evolution,
           transcricao, transcricao_status)
        VALUES
          (:mid, :inst, :cid, :jid, :tel, :nome, :fc, :tipo, :txt, :ts, :tr, :trs)
    """), {
        "mid": f"regen-test-{tel}-{ts.timestamp()}-{from_corretora}-{tipo}",
        "inst": _INSTANCE, "cid": _CID, "jid": f"{tel}@s.whatsapp.net",
        "tel": tel, "nome": nome, "fc": from_corretora, "tipo": tipo,
        "txt": texto, "ts": ts, "tr": transc, "trs": transc_status,
    })


@pytest.fixture(autouse=True)
def _clean():
    with SessionLocal() as s:
        s.execute(text("DELETE FROM mensagens_whatsapp WHERE instance = :i"), {"i": _INSTANCE})
        s.commit()
    yield
    with SessionLocal() as s:
        s.execute(text("DELETE FROM mensagens_whatsapp WHERE instance = :i"), {"i": _INSTANCE})
        s.commit()


def test_i1_conversa_cega_nao_puxa_chat_de_outro_lead():
    # "Sandra" (lead alvo): SÓ mensagens da corretora (HSM cego, texto NULL).
    tel_sandra = "+5500000000000"
    # "Alessandra" (decoy): nome é SUPERSTRING de "Sandra" e tem conversa real do lead.
    tel_ale = "+5500000000000"
    base = datetime.now(timezone.utc) - timedelta(days=20)
    with SessionLocal() as s:
        _ins(s, tel=tel_sandra, nome="Você", from_corretora=True, tipo="text", texto=None, ts=base)
        _ins(s, tel=tel_sandra, nome="Você", from_corretora=True, tipo="text", texto=None, ts=base + timedelta(hours=1))
        # decoy com mensagem de LEAD e nome "Alessandra" (contém "sandra")
        _ins(s, tel=tel_ale, nome="Alessandra", from_corretora=False, tipo="text",
             texto="Quero apto na zona leste", ts=base + timedelta(days=1))
        s.commit()

    conv = _pull_conv(_CID, f"+{tel_sandra}", None, nome="Fulano")
    # Não pode ter puxado a conversa da Alessandra.
    textos = [m["text"] for m in conv]
    assert "Quero apto na zona leste" not in textos
    # E como a conversa da Sandra é só corretora (sem msg de lead), o regen vai
    # PULAR (o próprio caller checa any(from==lead)). Confirma que aqui não há lead:
    assert not any(m["from"] == "lead" for m in conv)


def test_i1_match_exato_por_telefone_ainda_funciona():
    tel = "+5500000000000"
    base = datetime.now(timezone.utc) - timedelta(days=10)
    with SessionLocal() as s:
        _ins(s, tel=tel, nome="Julio", from_corretora=True, tipo="text", texto="Oi, tudo bem?", ts=base)
        _ins(s, tel=tel, nome="Julio", from_corretora=False, tipo="text",
             texto="Tenho interesse na zona oeste", ts=base + timedelta(minutes=5))
        s.commit()
    conv = _pull_conv(_CID, f"+{tel}", None, nome="Julio")
    assert any(m["from"] == "lead" and "zona oeste" in m["text"] for m in conv)


def test_i2_transcricao_passa_pro_conv():
    tel = "+5500000000000"
    base = datetime.now(timezone.utc) - timedelta(days=5)
    with SessionLocal() as s:
        _ins(s, tel=tel, nome="Stefany", from_corretora=False, tipo="audio",
             texto=None, transc="Tenho pendência no FIES pra resolver",
             transc_status="concluida", ts=base)
        s.commit()
    conv = _pull_conv(_CID, f"+{tel}", None, nome="Stefany")
    audio = [m for m in conv if m["type"] == "audio"]
    assert audio, "áudio deveria estar na conversa"
    assert audio[0]["transcricao"] == "Tenho pendência no FIES pra resolver"
    assert audio[0]["transcricao_status"] == "concluida"


def test_i2_rows_to_conv_nao_expoe_transcricao_de_nao_audio():
    # transcricao só faz sentido pra áudio; text/image devem vir None
    rows = [
        (False, "oi", datetime.now(timezone.utc), "text", "lixo", "concluida"),
        (True, None, datetime.now(timezone.utc), "audio", "transcrito", "concluida"),
    ]
    conv = _rows_to_conv(rows)
    assert conv[0]["transcricao"] is None
    assert conv[1]["transcricao"] == "transcrito"
