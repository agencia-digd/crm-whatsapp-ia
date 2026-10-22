"""Contratos da Assistente IA (tarefa 1) — exemplo válido e inválido de cada um.

Estes testes não abrem banco, não sobem app e não tocam rede: exercitam só
`app/services/instagram_acme/contracts.py`. O que eles defendem, em ordem de
importância:

1. **Não existe saída.** Nenhum endpoint de envio/cadastro declarado, nenhum
   cliente HTTP importado, e `recusar_envio` falha alto.
2. **Teto não afrouxa.** Configuração aceita reduzir para zero e recusa
   aumentar ou ligar envio.
3. **Janela é do evento.** 7 dias e 24h medidos do timestamp de origem, com
   relógio injetado — inclusive o instante exato do vencimento.
4. **Bloqueio ganha de aprovação**, e aprovação sem revisora/com pendência não
   é aprovação.

Como rodar (banco de teste é exigência do conftest da suíte, não deste arquivo):

    TEST_DATABASE_URL=postgresql+psycopg://copiloto_test:contato@example.com:5433/copiloto_test \\
        .venv/bin/python -m pytest tests/test_instagram_contracts.py -q
"""
from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone

import pytest

from app.services.instagram_acme import contracts as c

# ── Fixtures de dados sintéticos (nenhum dado de pessoa real) ────────────────

AGORA = datetime(2026, 9, 21, 12, 0, 0, tzinfo=timezone.utc)


def ident(ator_id: str = "IGSID-1", **kw) -> c.IdentidadeSocial:
    base = dict(
        plataforma=c.Plataforma.INSTAGRAM,
        conta=c.CONTA_INSTAGRAM,
        ator_id=ator_id,
        username="perfil_teste",
    )
    base.update(kw)
    return c.IdentidadeSocial(**base)


def evento_comentario(**kw) -> c.EventoNormalizado:
    base = dict(
        plataforma=c.Plataforma.INSTAGRAM,
        conta=c.CONTA_INSTAGRAM,
        evento_id="COMMENT-1",
        tipo=c.TipoEvento.COMENTARIO,
        autor=ident(),
        texto="Qual o bairro?",
        criado_em=AGORA - timedelta(days=1),
        recebido_em=AGORA - timedelta(days=1),
        contexto=c.ContextoAnuncio(
            media_id="MEDIA-1", ad_id="AD-1", no_grid=False, origem=c.OrigemTrafego.PAGO
        ),
    )
    base.update(kw)
    return c.EventoNormalizado(**base)


def proposta_publica(**kw) -> c.PropostaRespostaPublica:
    base = dict(
        proposta_id="P-1",
        estado=c.EstadoProposta.RASCUNHO,
        identidade=ident(),
        evento_id="COMMENT-1",
        comentario_id="COMMENT-1",
        classe=c.ClasseComentario.PERGUNTA_LOCALIZACAO,
        texto_sugerido="@perfil_teste esse é o Bairro X — me chama no direct.",
        criado_em=AGORA,
        atualizado_em=AGORA,
    )
    base.update(kw)
    return c.PropostaRespostaPublica(**base)


def consentimento() -> c.ConsentimentoCadastro:
    return c.ConsentimentoCadastro(
        evidencia_id="MSG-9",
        texto_evidencia="pode sim, meu telefone é ...",
        finalidade="encaminhamento para corretora e simulação",
        aceito_em=AGORA,
    )


def vinculo() -> c.VinculoTelefone:
    return c.VinculoTelefone(
        identidade=ident(),
        telefone_e164="+5500000000000",
        fonte=c.FonteEvidenciaVinculo.INFORMADO_EM_DM,
        evidencia_id="MSG-9",
        informado_em=AGORA,
    )


# ── 1. Não existe saída ──────────────────────────────────────────────────────


def test_nenhum_endpoint_de_envio_ou_cadastro_declarado() -> None:
    declarados = set(c.ENDPOINTS_PAINEL.values())
    assert not declarados & set(c.ROTAS_PROIBIDAS_V1)
    proibido = ("enviar", "cadastrar", "send", "dispatch", "publish")
    for nome, rota in c.ENDPOINTS_PAINEL.items():
        assert not any(p in rota for p in proibido), f"{nome} parece caminho de saída: {rota}"


def test_modulo_nao_importa_cliente_http_nem_transporte() -> None:
    """Contrato não fala com ninguém. Se um dia `httpx` aparecer aqui, é porque
    alguém começou a implementar envio dentro do arquivo de contrato."""
    fonte = inspect.getsource(c)
    for proibido in ("import httpx", "import requests", "EvolutionClient", "graph.facebook.com"):
        assert proibido not in fonte, f"contrato não pode conter {proibido!r}"


def test_recusar_envio_falha_alto() -> None:
    with pytest.raises(c.EnvioNaoPermitido):
        c.recusar_envio("private reply")


def test_execucao_manual_nao_cobre_cadastro() -> None:
    comuns = dict(
        proposta_id="P-1",
        autorizacao_dona_ref="whatsapp Dona 21/09 10h",
        responsavel="Pessoa11",
        executado_em=AGORA,
        canal="app Instagram",
        evidencia="print da resposta",
        registrado_em=AGORA,
    )
    ok = c.RegistroExecucaoManual(acao=c.AcaoProposta.RESPOSTA_PUBLICA, **comuns)
    assert ok.acao is c.AcaoProposta.RESPOSTA_PUBLICA
    with pytest.raises(c.ContratoInvalido):
        c.RegistroExecucaoManual(acao=c.AcaoProposta.CADASTRO_LEAD, **comuns)


def test_execucao_manual_exige_autorizacao_e_evidencia() -> None:
    with pytest.raises(Exception):
        c.RegistroExecucaoManual(
            proposta_id="P-1",
            acao=c.AcaoProposta.RESPOSTA_PUBLICA,
            autorizacao_dona_ref="",
            responsavel="Pessoa11",
            executado_em=AGORA,
            canal="app Instagram",
            evidencia="print",
            registrado_em=AGORA,
        )


# ── 2. Tetos ─────────────────────────────────────────────────────────────────


def test_tetos_padrao_sao_um_um_zero_zero() -> None:
    t = c.TETOS_PADRAO
    assert (t.max_public_replies_per_comment, t.max_initial_dm_per_person) == (1, 1)
    assert (t.max_automatic_followups, t.system_send_limit) == (0, 0)


def test_teto_aceita_reduzir_para_zero() -> None:
    t = c.TetosAssistente(max_public_replies_per_comment=0, max_initial_dm_per_person=0)
    assert t.max_initial_dm_per_person == 0


@pytest.mark.parametrize(
    "kw",
    [
        {"max_public_replies_per_comment": 2},
        {"max_initial_dm_per_person": 3},
        {"max_automatic_followups": 1},
        {"system_send_limit": 1},
    ],
)
def test_teto_rejeita_afrouxar_ou_ligar_envio(kw: dict) -> None:
    with pytest.raises(c.TetoInvalido):
        c.TetosAssistente(**kw)


def test_consumo_de_teto_por_pessoa_e_por_comentario() -> None:
    zerado = c.ConsumoTeto(identidade=ident(), comentario_id="COMMENT-1")
    assert zerado.resposta_publica_disponivel() and zerado.dm_inicial_disponivel()

    gasto = c.ConsumoTeto(
        identidade=ident(),
        comentario_id="COMMENT-1",
        respostas_publicas_registradas=1,
        dms_iniciais_registradas=1,
    )
    assert not gasto.resposta_publica_disponivel()
    assert not gasto.dm_inicial_disponivel()


def test_estado_de_envio_desconhecido_bloqueia() -> None:
    """Não saber se já mandamos é motivo para não mandar, não para mandar."""
    incerto = c.ConsumoTeto(identidade=ident(), estado_conhecido=False)
    assert not incerto.resposta_publica_disponivel()
    assert not incerto.dm_inicial_disponivel()


# ── 3. Janelas ───────────────────────────────────────────────────────────────


def test_private_reply_antes_no_instante_e_depois_de_7_dias() -> None:
    criado = AGORA
    assert c.janela_private_reply(criado, criado + timedelta(days=7) - timedelta(seconds=1)).aberta
    # No instante exato já está fechada — margem a favor da conta do cliente.
    no_instante = c.janela_private_reply(criado, criado + timedelta(days=7))
    assert not no_instante.aberta
    assert no_instante.motivo is c.MotivoBloqueio.JANELA_PRIVATE_REPLY_VENCIDA
    assert not c.janela_private_reply(criado, criado + timedelta(days=7, seconds=1)).aberta


def test_private_reply_conta_da_criacao_nao_da_aprovacao() -> None:
    """Rascunho parado 6 dias na fila não ganha 7 dias novos ao ser aprovado."""
    criado = AGORA - timedelta(days=6, hours=23)
    aprovado_em = AGORA + timedelta(hours=2)
    assert not c.janela_private_reply(criado, aprovado_em).aberta


def test_continuidade_antes_no_instante_e_depois_de_24h() -> None:
    ultima = AGORA
    assert c.janela_continuidade(ultima, ultima + timedelta(hours=23, minutes=59)).aberta
    assert not c.janela_continuidade(ultima, ultima + timedelta(hours=24)).aberta
    assert not c.janela_continuidade(ultima, ultima + timedelta(hours=25)).aberta


def test_sem_resposta_da_pessoa_nao_ha_continuidade() -> None:
    r = c.janela_continuidade(None, AGORA)
    assert not r.aberta
    assert r.motivo is c.MotivoBloqueio.JANELA_CONTINUIDADE_VENCIDA


def test_janela_recusa_datetime_ingenuo() -> None:
    with pytest.raises(c.ContratoInvalido):
        c.janela_private_reply(datetime(2026, 9, 21, 12, 0, 0), AGORA)


# ── 4. Evento normalizado ────────────────────────────────────────────────────


def test_evento_valido() -> None:
    e = evento_comentario()
    assert e.tenant_id == c.TENANT_ID
    assert not e.e_eco
    assert e.criado_em.tzinfo is not None


def test_evento_recusa_autor_de_outra_conta() -> None:
    with pytest.raises(c.ContratoInvalido):
        evento_comentario(autor=ident(conta="000000000000000"))


def test_evento_recusa_autor_de_outra_plataforma() -> None:
    with pytest.raises(c.ContratoInvalido):
        evento_comentario(
            autor=c.IdentidadeSocial(
                plataforma=c.Plataforma.FACEBOOK, conta=c.CONTA_INSTAGRAM, ator_id="PSID-1"
            )
        )


def test_comentario_sem_media_id_nao_e_rastreavel() -> None:
    with pytest.raises(c.ContratoInvalido):
        evento_comentario(contexto=c.ContextoAnuncio())


def test_evento_recusa_campo_desconhecido() -> None:
    """`extra=forbid`: um typo em nome de campo tem que estourar, não sumir."""
    with pytest.raises(Exception):
        evento_comentario(bloqueio="opt_out")


def test_eco_e_reconhecido_como_eco() -> None:
    e = evento_comentario(tipo=c.TipoEvento.ECO_COMENTARIO)
    assert e.e_eco


def test_falta_de_ad_id_nao_vira_organico() -> None:
    sem_ad = c.ContextoAnuncio(media_id="MEDIA-1")
    assert sem_ad.origem is c.OrigemTrafego.NAO_DETERMINADO
    with pytest.raises(c.ContratoInvalido):
        c.ContextoAnuncio(media_id="MEDIA-1", ad_id="AD-1", origem=c.OrigemTrafego.ORGANICO)


# ── 5. Identidade e vínculo ──────────────────────────────────────────────────


def test_chave_de_identidade_e_escopada() -> None:
    assert ident().chave == (1, "instagram", c.CONTA_INSTAGRAM, "IGSID-1")
    # Mesmo ator_id em conta diferente é outra pessoa: ID da Meta é escopado.
    assert ident().chave != ident(conta="000000000000000").chave


def test_vinculo_aceita_telefone_informado_em_dm() -> None:
    assert vinculo().telefone_e164 == "+5500000000000"


@pytest.mark.parametrize(
    "fonte",
    [c.FonteEvidenciaVinculo.PUBLICADO_EM_COMENTARIO, c.FonteEvidenciaVinculo.INFERIDO],
)
def test_vinculo_recusa_telefone_publico_ou_inferido(fonte) -> None:
    with pytest.raises(c.ContratoInvalido):
        c.VinculoTelefone(
            identidade=ident(),
            telefone_e164="+5500000000000",
            fonte=fonte,
            evidencia_id="COMMENT-1",
            informado_em=AGORA,
        )


def test_vinculo_recusa_telefone_fora_de_e164_br() -> None:
    for ruim in ("+5500000000000", "+5500000000000", "991232211"):
        with pytest.raises(Exception):
            c.VinculoTelefone(
                identidade=ident(),
                telefone_e164=ruim,
                fonte=c.FonteEvidenciaVinculo.INFORMADO_EM_DM,
                evidencia_id="MSG-9",
                informado_em=AGORA,
            )


# ── 6. Supressão ─────────────────────────────────────────────────────────────


def test_consulta_de_supressao_livre() -> None:
    r = c.ConsultaSupressao(disponivel=True, bloqueado=False, consultado_em=AGORA)
    assert not r.bloqueado


def test_consulta_indisponivel_precisa_bloquear() -> None:
    ok = c.ConsultaSupressao(
        disponivel=False,
        bloqueado=True,
        motivos=(c.MotivoBloqueio.CONSULTA_SUPRESSAO_INDISPONIVEL,),
        consultado_em=AGORA,
    )
    assert ok.bloqueado
    with pytest.raises(c.ContratoInvalido):
        c.ConsultaSupressao(disponivel=False, bloqueado=False, consultado_em=AGORA)
    with pytest.raises(c.ContratoInvalido):
        c.ConsultaSupressao(disponivel=False, bloqueado=True, consultado_em=AGORA)


def test_bloqueio_sem_motivo_nao_e_auditavel() -> None:
    with pytest.raises(c.ContratoInvalido):
        c.ConsultaSupressao(disponivel=True, bloqueado=True, consultado_em=AGORA)


def test_supressao_aceita_so_identidade_social() -> None:
    s = c.Supressao(identidade=ident(), motivo="pediu para sair na DM", registrado_em=AGORA)
    assert s.telefone_e164 is None


def test_supressao_sem_alvo_e_invalida() -> None:
    with pytest.raises(c.ContratoInvalido):
        c.Supressao(motivo="pediu para sair", registrado_em=AGORA)


# ── 7. Propostas ─────────────────────────────────────────────────────────────


def test_proposta_publica_valida() -> None:
    p = proposta_publica()
    assert p.acao is c.AcaoProposta.RESPOSTA_PUBLICA
    assert p.texto_vigente is not None


def test_texto_revisado_vence_o_sugerido() -> None:
    p = proposta_publica(texto_revisado="@perfil_teste é no Bairro X.")
    assert p.texto_vigente == "@perfil_teste é no Bairro X."


def test_rascunho_publico_sem_texto_e_invalido() -> None:
    with pytest.raises(c.ContratoInvalido):
        proposta_publica(texto_sugerido=None)


def test_aprovacao_nao_sobrepoe_bloqueio() -> None:
    with pytest.raises(c.ContratoInvalido):
        proposta_publica(
            estado=c.EstadoProposta.APROVADO,
            revisora="Pessoa11",
            bloqueios=(c.MotivoBloqueio.OPT_OUT_SOCIAL,),
        )


def test_aprovacao_exige_revisora() -> None:
    with pytest.raises(c.ContratoInvalido):
        proposta_publica(estado=c.EstadoProposta.APROVADO)


def test_pendencia_nao_aprovada() -> None:
    """`{{PENDENTE: ...}}` é recado para a revisora, não texto publicável."""
    with pytest.raises(c.ContratoInvalido):
        proposta_publica(
            estado=c.EstadoProposta.APROVADO,
            revisora="Pessoa11",
            pendencias=("corretora confirma bairro",),
        )


def test_bloqueio_sem_motivo_na_proposta() -> None:
    with pytest.raises(c.ContratoInvalido):
        proposta_publica(estado=c.EstadoProposta.BLOQUEADO)


def dm(**kw) -> c.PropostaDmInicial:
    base = dict(
        proposta_id="P-2",
        estado=c.EstadoProposta.RASCUNHO,
        identidade=ident(),
        evento_id="COMMENT-1",
        comentario_id="COMMENT-1",
        comentario_criado_em=AGORA - timedelta(days=1),
        classe=c.ClasseComentario.INTERESSE_SEM_DETALHE,
        texto_sugerido="Oi! Vi seu comentário. Posso te mandar os detalhes por aqui?",
        criado_em=AGORA,
        atualizado_em=AGORA,
    )
    base.update(kw)
    return c.PropostaDmInicial(**base)


def test_dm_para_interesse_e_valida() -> None:
    assert dm().acao is c.AcaoProposta.DM_INICIAL


@pytest.mark.parametrize(
    "classe",
    [
        c.ClasseComentario.CRITICA_OBJECAO,
        c.ClasseComentario.AUTOPROMOCAO_TERCEIRO,
        c.ClasseComentario.MARCOU_ALGUEM,
        c.ClasseComentario.ELOGIO_REACAO,
        c.ClasseComentario.TENTATIVA_INSTRUIR_IA,
        c.ClasseComentario.PEDIDO_DE_SAIDA,
    ],
)
def test_classe_que_nao_gera_dm(classe) -> None:
    with pytest.raises(c.ContratoInvalido):
        dm(classe=classe)


def test_classe_bloqueada_pode_existir_como_proposta_bloqueada() -> None:
    """A proposta bloqueada existe para a revisora VER o motivo — o que não
    existe é rascunho aprovável de DM para crítica."""
    p = dm(
        classe=c.ClasseComentario.CRITICA_OBJECAO,
        estado=c.EstadoProposta.BLOQUEADO,
        bloqueios=(c.MotivoBloqueio.CLASSE_SEM_DM,),
    )
    assert p.estado is c.EstadoProposta.BLOQUEADO


def test_grupos_sem_dm_cobrem_critica_spam_e_ambiguidade() -> None:
    assert c.GRUPO_DA_CLASSE[c.ClasseComentario.CRITICA_OBJECAO] in c.GRUPOS_SEM_DM
    assert c.GRUPO_DA_CLASSE[c.ClasseComentario.PERGUNTA_PRECO] not in c.GRUPOS_SEM_DM
    assert set(c.GRUPO_DA_CLASSE) == set(c.ClasseComentario)


def cadastro(**kw) -> c.PropostaCadastroLead:
    base = dict(
        proposta_id="P-3",
        estado=c.EstadoProposta.RASCUNHO,
        identidade=ident(),
        evento_id="MSG-9",
        consentimento=consentimento(),
        vinculo=vinculo(),
        owner_id=2,
        stage_id=6,
        criado_em=AGORA,
        atualizado_em=AGORA,
    )
    base.update(kw)
    return c.PropostaCadastroLead(**base)


def test_cadastro_valido() -> None:
    p = cadastro()
    assert p.origem == c.ORIGEM_LEAD
    assert p.origem_trafego is c.OrigemTrafego.NAO_DETERMINADO


@pytest.mark.parametrize("faltando", ["consentimento", "vinculo", "owner_id"])
def test_cadastro_incompleto_nao_fica_pronto(faltando: str) -> None:
    with pytest.raises(c.ContratoInvalido):
        cadastro(**{faltando: None})


def test_cadastro_de_duplicata_anexa_em_vez_de_recriar() -> None:
    p = cadastro(owner_id=None, anexar_a_lead_existente=True, lead_existente_id=1234)
    assert p.anexar_a_lead_existente and p.owner_id is None
    with pytest.raises(c.ContratoInvalido):
        cadastro(anexar_a_lead_existente=True, lead_existente_id=None)


def test_cadastro_recusa_origem_fora_do_contrato() -> None:
    with pytest.raises(c.ContratoInvalido):
        cadastro(origem="Instagram orgânico")


def test_cadastro_nao_tem_campo_fone8() -> None:
    """`fone8` é coluna gerada no banco. Se aparecer no contrato, alguém vai
    preencher."""
    assert "fone8" not in c.PropostaCadastroLead.model_fields


# ── 8. Estados, versão e rechecagem ──────────────────────────────────────────


@pytest.mark.parametrize(
    "de,para",
    [
        (c.EstadoProposta.RECEBIDO, c.EstadoProposta.RASCUNHO),
        (c.EstadoProposta.RASCUNHO, c.EstadoProposta.APROVADO),
        (c.EstadoProposta.APROVADO, c.EstadoProposta.EXECUTADO_MANUALMENTE),
        (c.EstadoProposta.APROVADO, c.EstadoProposta.RASCUNHO),  # edição invalida
        (c.EstadoProposta.APROVADO, c.EstadoProposta.BLOQUEADO),  # opt-out ganha
        (c.EstadoProposta.APROVADO, c.EstadoProposta.EXPIRADO),  # relógio ganha
    ],
)
def test_transicoes_permitidas(de, para) -> None:
    c.validar_transicao(de, para)


@pytest.mark.parametrize(
    "de,para",
    [
        (c.EstadoProposta.RASCUNHO, c.EstadoProposta.EXECUTADO_MANUALMENTE),
        (c.EstadoProposta.RECEBIDO, c.EstadoProposta.APROVADO),
        (c.EstadoProposta.BLOQUEADO, c.EstadoProposta.APROVADO),
        (c.EstadoProposta.REJEITADO, c.EstadoProposta.RASCUNHO),
        (c.EstadoProposta.EXPIRADO, c.EstadoProposta.APROVADO),
        (c.EstadoProposta.EXECUTADO_MANUALMENTE, c.EstadoProposta.RASCUNHO),
    ],
)
def test_transicoes_recusadas(de, para) -> None:
    with pytest.raises(c.TransicaoInvalida):
        c.validar_transicao(de, para)


def test_toda_transicao_declarada_aponta_para_estado_conhecido() -> None:
    assert set(c.TRANSICOES) == set(c.EstadoProposta)
    for destinos in c.TRANSICOES.values():
        assert destinos <= set(c.EstadoProposta)


def test_revisao_otimista() -> None:
    p = proposta_publica(versao=3)
    c.exigir_versao(p, 3)
    with pytest.raises(c.VersaoDesatualizada):
        c.exigir_versao(p, 2)


def test_rechecagem_obrigatoria_por_acao() -> None:
    dm_checks = c.rechecagem_obrigatoria(c.AcaoProposta.DM_INICIAL)
    assert "janela_private_reply" in dm_checks and "supressao" in dm_checks
    cad = c.rechecagem_obrigatoria(c.AcaoProposta.CADASTRO_LEAD)
    assert {"consentimento", "duplicidade_telefone", "owner_ativo"} <= set(cad)
    pub = c.rechecagem_obrigatoria(c.AcaoProposta.RESPOSTA_PUBLICA)
    assert "janela_private_reply" not in pub and "teto" in pub


# ── 9. Contratos de tela ─────────────────────────────────────────────────────


def test_decisao_so_aceita_aprovar_ou_rejeitar() -> None:
    assert c.PedidoDecisao(decisao=c.EstadoProposta.APROVADO, versao=1).versao == 1
    with pytest.raises(c.ContratoInvalido):
        c.PedidoDecisao(decisao=c.EstadoProposta.EXECUTADO_MANUALMENTE, versao=1)
    with pytest.raises(c.ContratoInvalido):
        c.PedidoDecisao(decisao=c.EstadoProposta.BLOQUEADO, versao=1)


def test_rejeicao_exige_motivo() -> None:
    with pytest.raises(c.ContratoInvalido):
        c.PedidoDecisao(decisao=c.EstadoProposta.REJEITADO, versao=1)
    ok = c.PedidoDecisao(decisao=c.EstadoProposta.REJEITADO, versao=1, motivo="fora de contexto")
    assert ok.motivo


def test_filtro_da_fila_nao_aceita_tenant_do_navegador() -> None:
    with pytest.raises(Exception):
        c.FiltroFila(tenant_id=2)


def test_item_da_fila_nasce_marcado_como_rascunho() -> None:
    item = c.ItemFila(
        proposta_id="P-1",
        acao=c.AcaoProposta.RESPOSTA_PUBLICA,
        estado=c.EstadoProposta.RASCUNHO,
        versao=1,
    )
    assert item.marca == c.MARCA_RASCUNHO == "Rascunho — não enviado"
    assert c.MARCA_CADASTRO == "Proposta — não cadastrada"


def test_telefone_vai_mascarado_na_lista() -> None:
    assert c.mascarar_telefone("+5500000000000") == "+55 15 ****-2211"
    assert c.mascarar_telefone(None) is None
    assert "9912" not in (c.mascarar_telefone("+5500000000000") or "")


# ── 10. Auditoria ────────────────────────────────────────────────────────────


def test_auditoria_guarda_o_texto_do_momento() -> None:
    a = c.EventoAuditoria(
        proposta_id="P-1",
        acao=c.AcaoProposta.RESPOSTA_PUBLICA,
        de_estado=c.EstadoProposta.RASCUNHO,
        para_estado=c.EstadoProposta.APROVADO,
        versao=2,
        autor="Pessoa11",
        texto_no_momento="@perfil_teste esse é o Bairro X.",
        em=AGORA,
    )
    assert a.texto_no_momento and a.versao == 2


def test_auditoria_recusa_autor_vazio() -> None:
    with pytest.raises(Exception):
        c.EventoAuditoria(
            proposta_id="P-1",
            acao=c.AcaoProposta.RESPOSTA_PUBLICA,
            de_estado=None,
            para_estado=c.EstadoProposta.RECEBIDO,
            versao=1,
            autor="",
            em=AGORA,
        )


# ── 11. Imutabilidade ────────────────────────────────────────────────────────


def test_contratos_sao_imutaveis() -> None:
    """Proposta que se altera no lugar faz a auditoria mentir."""
    p = proposta_publica()
    with pytest.raises(Exception):
        p.estado = c.EstadoProposta.APROVADO  # type: ignore[misc]


def test_bloqueios_so_de_dm_nao_matam_resposta_publica() -> None:
    assert c.MotivoBloqueio.IDENTIDADE_NAO_CONCILIADA in c.BLOQUEIOS_SO_DE_DM
    assert c.MotivoBloqueio.OPT_OUT_SOCIAL not in c.BLOQUEIOS_SO_DE_DM
