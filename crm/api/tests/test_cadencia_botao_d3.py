"""D3 com BOTÃO — clique manda o áudio e NÃO tira o lead da régua (Dona 05/08).

O desenho: o toque que oferece o áudio sai com botão de template ("Saiba mais",
pool reeng_b já aprovado). Aí:

  - CLIQUE           → manda o áudio da corretora dona e o lead SEGUE na régua
                       (o próximo toque continua agendado). Clique não é
                       resposta: antes, o "sim" em texto parava a cadência e o
                       lead saía sem nunca ter conversado com ninguém;
  - clicou e calou   → nada mais acontece, o D4 chega normalmente;
  - falou depois     → caminho normal de resposta → atendimento da corretora;
  - falou "já achei" → despedida com pedido de indicação (não vai pro
                       atendimento);
  - clicou em "Parar mensagens" → opt-out, porque pedido de saída vale clicado.

Régua NUNCA fixada no teste: quem tem botão sai de `cadencia.toque_com_botao()`.

DB isolado: SQLite in-memory. Zero prod, zero envio (o de mídia é fake).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import BigInteger, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from app.db.models import (
    Base,
    CadenciaEstado,
    CadenciaEvento,
    Corretora,
    Lead,
    LeadEvent,
    PipelineStage,
)
from app.services import cadencia as c


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(element, compiler, **kw):  # noqa: ANN001
    return "JSON"


@compiles(BigInteger, "sqlite")
def _compile_bigint_sqlite(element, compiler, **kw):  # noqa: ANN001
    return "INTEGER"


CORRETOR_ID = 3
LEAD_FONE = "+5500000000000"
TOQUE_BOTAO = c.toque_com_botao()


@pytest.fixture()
def session_factory():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(
        engine,
        tables=[
            Corretora.__table__,
            CadenciaEstado.__table__,
            CadenciaEvento.__table__,
            Lead.__table__,
            LeadEvent.__table__,
            PipelineStage.__table__,
        ],
    )
    factory = sessionmaker(bind=engine)
    with factory() as s:
        s.add(
            Corretora(
                id=CORRETOR_ID, nome="Pessoa3", telefone="+5500000000000",
                telefone_normalizado="+5500000000000", kommo_user_id=10000003,
                evolution_instance="acme_corretora_a", ativo=True,
                papel="corretora_atendedora_de_dona",
            )
        )
        s.commit()
    return factory


def _matricular(factory, *, toque_atual: int) -> int:
    with factory() as s:
        est = c.iniciar_cadencia(
            s, telefone=LEAD_FONE, lead_id="777", corretora_id=CORRETOR_ID
        )
        est.toque_atual = toque_atual
        s.commit()
        return est.id


@pytest.fixture()
def audios(monkeypatch):
    """Captura o envio de mídia (o áudio) sem tocar em rede."""
    enviados: list[tuple] = []
    monkeypatch.setattr(
        "app.api.cloud_corretora_a.enviar_cloud_midia",
        lambda tel, dados, **k: enviados.append((tel, len(dados))) or {"ok": True},
    )
    return enviados


# ── a régua ──────────────────────────────────────────────────────────────────


def test_existe_exatamente_um_toque_com_botao():
    """O follow-up é sem botão (Dona 28/07). A oferta de áudio é a exceção —
    se um dia virarem dois, é decisão de produto, não acidente de código."""
    com_botao = [t for t in c.TOQUES if t.tipo == c.TIPO_MENSAGEM and t.com_botao]
    assert len(com_botao) == 1
    assert com_botao[0] is TOQUE_BOTAO
    assert TOQUE_BOTAO.cta, "toque com botão precisa do verbo do CTA ({{3}})"


def test_so_o_toque_da_oferta_tem_botao():
    for t in c.TOQUES:
        assert c._com_botao(t.indice) is (t is TOQUE_BOTAO)


def test_o_toque_da_oferta_manda_o_sender_usar_botao():
    """O sender precisa receber com_botao=True e o cta — senão o texto fala em
    'tocar no botão' e o lead não tem botão nenhum."""
    chamadas: list[dict] = []

    def sender(telefone, texto, *, com_botao, cta=None, **kw):
        chamadas.append({"com_botao": com_botao, "cta": cta})
        return {"ok": True, "dry_run": True, "sent": False}

    c._chamar_sender(
        sender, LEAD_FONE, "texto?", com_botao=c._com_botao(TOQUE_BOTAO.indice),
        cta=TOQUE_BOTAO.cta,
    )
    assert chamadas == [{"com_botao": True, "cta": TOQUE_BOTAO.cta}]


def test_sender_antigo_sem_cta_nao_quebra():
    """Fakes de teste e callers do painel têm assinatura curta — passar kwargs
    às cegas estouraria TypeError e calaria a cadência inteira."""
    def sender_curto(telefone, texto, *, com_botao):
        return {"ok": True, "dry_run": True, "sent": False}

    assert c._chamar_sender(
        sender_curto, LEAD_FONE, "texto?", com_botao=True, cta="receber o áudio"
    )["ok"] is True


# ── o clique ─────────────────────────────────────────────────────────────────


def test_clique_manda_audio_e_lead_continua_na_regua(session_factory, audios):
    cid = _matricular(session_factory, toque_atual=TOQUE_BOTAO.indice + 1)

    with session_factory() as s:
        saida = c.processar_resposta_cadencia(
            LEAD_FONE, "Saiba mais", tipo_mensagem="button", session=s
        )
    assert saida == "clique_audio"
    assert len(audios) == 1

    with session_factory() as s:
        est = s.get(CadenciaEstado, cid)
        assert est.status == "ativa", "clique NÃO pode tirar o lead da régua"
        assert est.toque_atual == TOQUE_BOTAO.indice + 1  # segue pro próximo
        eventos = s.query(CadenciaEvento).all()
        assert [e.tipo for e in eventos] == [c.TIPO_CLIQUE_BOTAO]
        assert eventos[0].payload["audio_enviado"] is True
        # clique NÃO grava evento de resposta — senão a métrica contaria como
        # engajamento de conversa o que foi só um toque de dedo.
        assert not [e for e in eventos if e.tipo == c.TIPO_RESPOSTA]


def test_clique_fora_do_toque_da_oferta_nao_manda_audio(session_factory, audios):
    """Botão de outro toque (ou clique atrasado) não vira áudio sem pedido."""
    _matricular(session_factory, toque_atual=0)
    with session_factory() as s:
        saida = c.processar_resposta_cadencia(
            LEAD_FONE, "Saiba mais", tipo_mensagem="button", session=s
        )
    assert saida == "clique_botao"
    assert audios == []
    with session_factory() as s:
        assert s.query(CadenciaEstado).one().status == "ativa"


def test_falar_depois_do_clique_vai_pro_atendimento(session_factory, audios):
    """Clique não é resposta; TEXTO é. Quem escreve tem assunto — e assunto é
    da corretora."""
    _matricular(session_factory, toque_atual=TOQUE_BOTAO.indice + 1)
    with session_factory() as s:
        c.processar_resposta_cadencia(
            LEAD_FONE, "Saiba mais", tipo_mensagem="button", session=s
        )
    with session_factory() as s:
        saida = c.processar_resposta_cadencia(
            LEAD_FONE, "gostei, tem no Bairro A?", tipo_mensagem="text", session=s
        )
    assert saida == c.STATUS_RESPONDIDA
    with session_factory() as s:
        assert s.query(CadenciaEstado).one().status == c.STATUS_RESPONDIDA


def test_ja_encontrei_depois_do_clique_se_despede(session_factory, audios, monkeypatch):
    """A saída "já achei" ganha da resposta comum: encerra com pedido de
    indicação, sem passar pelo atendimento."""
    monkeypatch.setattr("app.api.cloud_corretora_a.enviar_cloud",
                        lambda *a, **k: {"ok": True})
    _matricular(session_factory, toque_atual=TOQUE_BOTAO.indice + 1)
    with session_factory() as s:
        saida = c.processar_resposta_cadencia(
            LEAD_FONE, "obrigado, mas já encontrei um imóvel", session=s
        )
    assert saida == "ja_encontrou"
    with session_factory() as s:
        assert s.query(CadenciaEstado).one().status == c.STATUS_JA_ENCONTROU


def test_clique_em_parar_mensagens_e_opt_out(session_factory, audios):
    """Pedido de saída vale clicado. Tratar como "clique não é resposta" aqui
    seria continuar mandando mensagem pra quem pediu pra parar."""
    _matricular(session_factory, toque_atual=TOQUE_BOTAO.indice + 1)
    with session_factory() as s:
        saida = c.processar_resposta_cadencia(
            LEAD_FONE, "Parar mensagens", tipo_mensagem="button", session=s
        )
    assert saida == "opt_out"
    assert audios == [], "quem pede pra sair não recebe áudio"
    with session_factory() as s:
        assert s.query(CadenciaEstado).one().status == "opt_out"


def test_clique_logo_apos_o_envio_nao_e_auto_reply(session_factory, audios):
    """O clique chega segundos depois do template (é pra isso que o botão
    existe) e cairia na janela anti-secretária-eletrônica. Bot não clica."""
    cid = _matricular(session_factory, toque_atual=TOQUE_BOTAO.indice + 1)
    agora = datetime.now(timezone.utc)
    with session_factory() as s:
        s.add(CadenciaEvento(
            tenant_id=1, cadencia_id=cid, toque=TOQUE_BOTAO.indice,
            tipo=c.TIPO_MENSAGEM, canal="whatsapp",
            payload={"texto": "quer o áudio?", "sent": True}, at=agora,
        ))
        s.commit()
    with session_factory() as s:
        saida = c.processar_resposta_cadencia(
            LEAD_FONE, "Saiba mais", tipo_mensagem="button", session=s
        )
    assert saida == "clique_audio"
    assert len(audios) == 1


def test_tipos_de_clique_reconhecidos():
    for t in ("button", "interactive", "BUTTON"):
        assert c.e_clique_de_botao(t) is True
    for t in ("text", "audio", "image", None, ""):
        assert c.e_clique_de_botao(t) is False


# ── copies: cada toque fecha de um jeito ─────────────────────────────────────


def _fecho(texto: str) -> str:
    """Última pergunta da mensagem — é o que o lead lê por último."""
    return texto.strip().split("\n")[-1].strip().lower()


def test_fechos_das_copies_nao_se_repetem():
    """A Dona reparou: a régua inteira terminava quase igual, e quem recebe os
    9 toques lê a mesma pergunta várias vezes."""
    fechos = [_fecho(t) for t in c._TEMPLATES_MSG.values()]
    assert len(fechos) == len(set(fechos)), f"fecho repetido: {fechos}"


def test_toda_copy_termina_com_pergunta():
    for indice, texto in c._TEMPLATES_MSG.items():
        assert texto.rstrip().endswith("?"), f"toque {indice} não pede próxima ação"


# ── o mapa não pode mentir sobre as saídas ───────────────────────────────────


def test_mapa_lista_todas_as_saidas_da_regua():
    """A tela desenha a saída comum a partir do que o MAPA diz. Se uma saída
    real do motor não estiver aqui, o desenho some com ela — foi assim que a
    régua pareceu uma esteira em linha reta."""
    from app.api.automacoes import _mapa_gatilhos

    por_id = {g["id"]: g for g in _mapa_gatilhos()}
    # todas as saídas que `processar_resposta_cadencia` sabe produzir
    for gid in ("resposta_lead", "opt_out", "ja_encontrou", "pausa_30d",
                "esgotou_cadencia", "clique_botao", "auto_reply"):
        assert gid in por_id, f"saída '{gid}' existe no motor e sumiu do mapa"


def test_mapa_marca_quem_interrompe_a_regua():
    """Clique e auto-reply NÃO tiram o lead da régua; o resto tira."""
    from app.api.automacoes import _mapa_gatilhos

    por_id = {g["id"]: g for g in _mapa_gatilhos()}
    assert por_id["clique_botao"]["interrompe"] is False
    assert por_id["auto_reply"]["interrompe"] is False
    for gid in ("resposta_lead", "opt_out", "ja_encontrou", "pausa_30d"):
        assert por_id[gid]["interrompe"] is True, gid
    # gatilho de ENTRADA não é saída — não pode aparecer como interrupção
    assert por_id["auto_enroll_followup"]["interrompe"] is None
