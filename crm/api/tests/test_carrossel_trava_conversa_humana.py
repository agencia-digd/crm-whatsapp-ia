"""A trava de "conversa humana em andamento" do carrossel da Corretor.

Caso real que originou estes testes (15/09/2026): lead 6462 · Ludmila Campos ·
+5500000000000 · fone8 96180354. A Pessoa5 falou com ela em 10/09 às 10:25 e
10:28 (texto "Ola bom dia Ludmila como vai?" + um áudio) pela instance
`acme_corretora_c`. A suspeita era que a trava não pegasse e a Corretor disparasse por
cima do atendimento.

Auditado contra o banco de produção (só leitura): a trava PEGA esse caso —
`NOT EXISTS` fecha pelas duas mensagens de 10/09 e ela não sai em `_SQL_FILA`.
Em 30 dias e 4.174 disparos do carrossel, ZERO saíram com mensagem humana
(saída sem carimbo de robô, fora da linha da Corretor) nos 7 dias anteriores.

Estes testes existem pra que isso continue verdade — e, principalmente, pra
que as DUAS bordas da trava não se percam num refactor futuro:

  - barra quem tem gente falando AGORA (texto e áudio; o caso da Ludmila);
  - NÃO barra base fria (39% da base já falou com humano alguma vez — reengajar
    lead frio é o trabalho do carrossel);
  - NÃO barra quem só recebeu robô — origem carimbada (`anti_ghosting`,
    `pos_ligacao`, `pessoa`, `carrossel`) ou template de reengajamento. Sem
    isso a automação se auto-bloqueia: o follow-up do próprio robô tirava o
    lead da fila (bug de 15/09, commit 0d88d15c, 29 leads afetados).
"""
from __future__ import annotations

import uuid
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text as _t

from app.config import settings
from app.db.session import SessionLocal
from app.services import carrossel_corretor as cb

TENANT = 1
_PREFIXO = "5500000000000"     # faixa só destes testes
PAUSA = settings.carrossel_pausa_conversa_humana_dias


def _agora() -> datetime:
    return datetime.now(timezone.utc)


def _criar_lead(s, *, tel: str) -> int:
    lid = s.execute(
        _t("""INSERT INTO leads (tenant_id, nome, telefone, owner_id, stage_id,
                                 created_at, updated_at)
              VALUES (:t, 'Ludmila (teste)', :tel, :o, :st, now(), now())
           RETURNING id"""),
        {"t": TENANT, "tel": tel, "o": cb.CORRETORA_CORRETOR, "st": cb.STAGE_FILA_PADRAO},
    ).scalar()
    s.commit()
    return int(lid)


def _msg_saida(
    s,
    *,
    tel: str,
    dias_atras: float,
    instance: str = "acme_corretora_c",
    tipo: str = "text",
    origem: str | None = None,
    template: str | None = None,
) -> None:
    """Mensagem de SAÍDA (from_corretora) — o que a trava olha."""
    meta: dict[str, str] = {}
    if origem:
        meta["origem"] = origem
    if template:
        meta["template"] = template
    s.execute(
        _t("""INSERT INTO mensagens_whatsapp
                (id, evolution_message_id, instance, corretora_id, remote_jid,
                 contato_telefone, from_corretora, tipo_mensagem, conteudo_texto,
                 conteudo_metadata, timestamp_evolution, recebido_em, tenant_id)
              VALUES (:id, :w, :inst, NULL, :jid, :tel, TRUE, :tipo,
                      'Ola bom dia Ludmila como vai?',
                      CAST(:meta AS jsonb), :ts, now(), :t)"""),
        {"id": str(uuid.uuid4()), "w": str(uuid.uuid4()), "inst": instance,
         "jid": f"{tel}@s.whatsapp.net", "tel": tel, "tipo": tipo,
         "meta": json.dumps(meta) if meta else None,
         "ts": _agora() - timedelta(days=dias_atras), "t": TENANT},
    )
    s.commit()


def _fila_ids(s) -> set[int]:
    """Quem `_SQL_FILA` devolveria agora — a query REAL do motor."""
    return {
        r["id"]
        for r in s.execute(
            _t(cb._SQL_FILA),
            {"t": TENANT, "dono": cb.CORRETORA_CORRETOR, "fila": cb.STAGE_FILA_PADRAO,
             "lim": 10_000, "pausa_dias": PAUSA},
        ).mappings().all()
    }


@pytest.fixture()
def s():
    def _limpar(ses):
        ses.execute(_t("DELETE FROM lead_events WHERE lead_id IN "
                       "(SELECT id FROM leads WHERE telefone LIKE :p)"),
                    {"p": f"{_PREFIXO}%"})
        ses.execute(_t("DELETE FROM mensagens_whatsapp WHERE contato_telefone LIKE :p"),
                    {"p": f"{_PREFIXO}%"})
        ses.execute(_t("DELETE FROM leads WHERE telefone LIKE :p"), {"p": f"{_PREFIXO}%"})
        ses.commit()

    with SessionLocal() as ses:
        _limpar(ses)
        yield ses
        _limpar(ses)


# ── 1. O caso da Ludmila: gente falando AGORA barra ─────────────────────────

def test_texto_de_corretora_dentro_da_janela_tira_da_fila(s):
    """Pessoa5 mandou texto 5 dias atrás (janela = 7): não dispara por cima."""
    tel = _PREFIXO + "01"
    lid = _criar_lead(s, tel=tel)
    assert lid in _fila_ids(s), "sem mensagem nenhuma o lead tem que estar na fila"

    _msg_saida(s, tel=tel, dias_atras=PAUSA - 2)
    assert lid not in _fila_ids(s)


def test_audio_de_corretora_tambem_conta_como_conversa(s):
    """O 2º toque da Pessoa5 foi ÁUDIO — não pode escapar por tipo_mensagem."""
    tel = _PREFIXO + "02"
    lid = _criar_lead(s, tel=tel)
    _msg_saida(s, tel=tel, dias_atras=PAUSA - 2, tipo="audio")
    assert lid not in _fila_ids(s)


def test_a_propria_linha_da_corretor_com_humano_tambem_barra(s):
    """A linha da Corretor é compartilhada com gente: saída sem carimbo é humano."""
    tel = _PREFIXO + "03"
    lid = _criar_lead(s, tel=tel)
    _msg_saida(s, tel=tel, dias_atras=1, instance=cb.INSTANCE_CORRETOR)
    assert lid not in _fila_ids(s)


# ── 2. Base fria NÃO é barrada — é o trabalho do carrossel ──────────────────

def test_conversa_fora_da_janela_nao_barra(s):
    """Falou há muito tempo = base fria. Reengajar frio é o produto."""
    tel = _PREFIXO + "04"
    lid = _criar_lead(s, tel=tel)
    _msg_saida(s, tel=tel, dias_atras=PAUSA + 30)
    assert lid in _fila_ids(s)


# ── 3. Robô não conta como gente (senão a automação se auto-bloqueia) ───────

@pytest.mark.parametrize("origem", ["pessoa", "anti_ghosting", "pos_ligacao", "carrossel"])
def test_saida_carimbada_de_robo_nao_barra(s, origem):
    """Regressão do 0d88d15c: `<> 'pessoa'` lia anti_ghosting como humano."""
    tel = _PREFIXO + "05"
    lid = _criar_lead(s, tel=tel)
    _msg_saida(s, tel=tel, dias_atras=1, origem=origem)
    assert lid in _fila_ids(s)


def test_template_de_reengajamento_nao_barra(s):
    """Template é disparo (robô), não conversa — inclusive na linha da corretora.

    Foi o que aconteceu com os leads 6197/6425 em 09-10/09: o único contato
    recente era `acme_ola_a_01` / `acme_jn_ola_fu_01`, disparo da cadência.
    Contar isso como "humano falando" esvaziaria a fila sozinho.
    """
    tel = _PREFIXO + "06"
    lid = _criar_lead(s, tel=tel)
    _msg_saida(s, tel=tel, dias_atras=1, tipo="template",
               template="acme_jn_ola_fu_01")
    assert lid in _fila_ids(s)


# ── 4. Entrada do lead não é conversa humana ───────────────────────────────

def test_mensagem_recebida_do_lead_nao_barra_por_esta_trava(s):
    """A trava é sobre a CASA falando. Resposta do lead tem outro caminho
    (`marcar_respondeu`), e não pode ser lida como atendimento em andamento."""
    tel = _PREFIXO + "07"
    lid = _criar_lead(s, tel=tel)
    s.execute(
        _t("""INSERT INTO mensagens_whatsapp
                (id, evolution_message_id, instance, corretora_id, remote_jid,
                 contato_telefone, from_corretora, tipo_mensagem, conteudo_texto,
                 conteudo_metadata, timestamp_evolution, recebido_em, tenant_id)
              VALUES (:id, :w, 'acme_corretora_c', NULL, :jid, :tel, FALSE, 'text',
                      'oi', NULL, :ts, now(), :t)"""),
        {"id": str(uuid.uuid4()), "w": str(uuid.uuid4()),
         "jid": f"{tel}@s.whatsapp.net", "tel": tel,
         "ts": _agora() - timedelta(days=1), "t": TENANT},
    )
    s.commit()
    assert lid in _fila_ids(s)
