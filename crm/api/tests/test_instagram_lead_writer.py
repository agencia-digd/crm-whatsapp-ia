"""Cadastro efetivo da Assistente (tarefa 9) — prova de que gravar o lead NÃO
liga roleta, boas-vindas, cadência ou envio.

Estes testes escrevem em `leads` de verdade, num banco **descartável**. O
`conftest` aborta no import se `TEST_DATABASE_URL` apontar para produção.

Como rodar:

    TEST_DATABASE_URL=postgresql+psycopg://<user>:<senha>@127.0.0.1:5433/<banco_descartavel> \\
        .venv/bin/python -m pytest tests/test_instagram_lead_writer.py -q

Rede externa bloqueada por fixture autouse: se alguém puser uma chamada HTTP no
caminho de cadastro, o teste quebra aqui, não em produção.
"""
from __future__ import annotations

import ast
import contextlib
import inspect
import socket
import threading
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text as sql_text

from app.services.instagram_acme import contracts as c
from app.services.instagram_acme import lead_writer as lw

AGORA = datetime(2026, 9, 21, 12, 0, 0, tzinfo=timezone.utc)

#: Faixa de telefone reservada a estes testes. A limpeza apaga por este prefixo
#: — nunca `DELETE FROM leads` sem WHERE.
PREFIXO = "5500000000000"
TEL_A = "+5500000000000"
#: Mesmo sufixo-8 de TEL_A, DDD diferente. Tem que virar lead SEPARADO.
TEL_OUTRO_DDD = "+5500000000000"
TEL_B = "+5500000000000"
TEL_AMBIGUO = "+5500000000000"  # 12 dígitos começando em 7 → não decide o 9

CORRETORA_OK = 901
CORRETORA_INATIVA = 902
CORRETORA_AUTOMACAO = 903


# ── Ambiente ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _sem_rede(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cadastro não fala com ninguém. O Postgres de teste é local e já está
    conectado pelo pool antes desta fixture; qualquer socket NOVO levanta."""

    def proibido(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("cadastro tentou abrir conexão de rede")

    monkeypatch.setattr(socket, "create_connection", proibido)


@pytest.fixture
def session():
    from app.db.session import SessionLocal

    s = SessionLocal()
    _limpar(s)
    yield s
    s.rollback()
    _limpar(s)
    s.close()


#: Todos os telefones que estes testes usam. A limpeza apaga só estes — nunca
#: `DELETE FROM leads` sem WHERE (o conftest tem uma seção inteira sobre o dia
#: em que isso rodou contra produção).
_TELEFONES = (TEL_A, TEL_B, TEL_OUTRO_DDD, TEL_AMBIGUO, "+000000000000000")


def _limpar(s) -> None:
    s.rollback()
    alvo = {"tels": list(_TELEFONES), "p": f"{PREFIXO}%"}
    s.execute(
        sql_text(
            "DELETE FROM lead_events WHERE lead_id IN "
            "(SELECT id FROM leads WHERE telefone = ANY(:tels) OR telefone LIKE :p)"
        ),
        alvo,
    )
    s.execute(
        sql_text("DELETE FROM leads WHERE telefone = ANY(:tels) OR telefone LIKE :p"), alvo
    )
    s.execute(
        sql_text(
            "DELETE FROM cadencia_estado WHERE telefone = ANY(:tels) OR telefone LIKE :p"
        ),
        alvo,
    )
    s.commit()


@pytest.fixture(autouse=True)
def corretoras(session):
    """Três corretoras de teste: uma válida, uma inativa e uma na linha de
    automação (a "Corretor"), que é a que o cadastro tem que recusar."""
    for cid, nome, ativo, instance in (
        (CORRETORA_OK, "Teste 9E10", True, "acme_teste_9e10"),
        (CORRETORA_INATIVA, "Teste Inativa", False, "acme_teste_inativa"),
        (CORRETORA_AUTOMACAO, "Teste Corretor", True, lw.INSTANCE_LINHA_REMARKETING),
    ):
        session.execute(
            sql_text(
                "INSERT INTO corretoras (id, tenant_id, nome, telefone, ativo, papel, "
                "                        evolution_instance) "
                "VALUES (:i, 1, :n, :t, :a, 'corretora_solo', :inst) "
                "ON CONFLICT (id) DO UPDATE SET ativo = EXCLUDED.ativo, "
                "  evolution_instance = EXCLUDED.evolution_instance"
            ),
            {"i": cid, "n": nome, "t": f"5500000000000{cid}", "a": ativo, "inst": instance},
        )
    session.commit()
    yield


# ── Fábrica de proposta ──────────────────────────────────────────────────────


def _identidade(ator_id: str = "IGSID-1") -> c.IdentidadeSocial:
    return c.IdentidadeSocial(
        plataforma=c.Plataforma.INSTAGRAM,
        conta=c.CONTA_INSTAGRAM,
        ator_id=ator_id,
        username="fulana",
    )


def proposta(
    *,
    proposta_id: str = "P-9-1",
    telefone: str = TEL_A,
    owner_id: int | None = CORRETORA_OK,
    estado: c.EstadoProposta = c.EstadoProposta.APROVADO,
    ator_id: str = "IGSID-1",
    anexar: bool = False,
    lead_existente_id: int | None = None,
    bloqueios: tuple[c.MotivoBloqueio, ...] = (),
    consentimento: bool = True,
) -> c.PropostaCadastroLead:
    identidade = _identidade(ator_id)
    return c.PropostaCadastroLead(
        proposta_id=proposta_id,
        estado=estado,
        identidade=identidade,
        evento_id="MSG-1",
        consentimento=(
            c.ConsentimentoCadastro(
                evidencia_id="MSG-ACEITE-1",
                texto_evidencia="pode me chamar, meu número é esse",
                finalidade="falar com uma corretora e fazer a simulação",
                aceito_em=AGORA - timedelta(minutes=5),
            )
            if consentimento
            else None
        ),
        vinculo=c.VinculoTelefone(
            identidade=identidade,
            telefone_e164=telefone,
            fonte=c.FonteEvidenciaVinculo.INFORMADO_EM_DM,
            evidencia_id="MSG-ACEITE-1",
            informado_em=AGORA - timedelta(minutes=5),
        ),
        owner_id=owner_id,
        origem_trafego=c.OrigemTrafego.PAGO,
        anexar_a_lead_existente=anexar,
        lead_existente_id=lead_existente_id,
        bloqueios=bloqueios,
        revisora="corretor",
        criado_em=AGORA - timedelta(hours=1),
        atualizado_em=AGORA - timedelta(minutes=1),
    )


AUTORIZACAO = lw.AutorizacaoCadastro(
    autorizacao_dona_ref="OS-ASSISTENTE-IA-TAREFA-9E10 · Dona 21/09/2026 09:29 UTC",
    responsavel="assistente",
)


def _gravar(session, prop, **kw):
    return lw.gravar_cadastro(
        prop, autorizacao=AUTORIZACAO, session=session, agora=AGORA, **kw
    )


def _conta_leads(session, telefone: str) -> int:
    return int(
        session.execute(
            sql_text("SELECT count(*) FROM leads WHERE tenant_id = 1 AND telefone = :t"),
            {"t": telefone},
        ).scalar()
    )


# ── 1. O caminho feliz, e o que ele NÃO faz ──────────────────────────────────


def test_cadastro_grava_owner_origem_e_etapa_novo(session):
    r = _gravar(session, proposta())
    assert r.criado and not r.anexado and not r.repetido

    linha = session.execute(
        sql_text(
            "SELECT l.telefone, l.owner_id, l.origem, ps.nome, l.external_ref "
            "  FROM leads l LEFT JOIN pipeline_stages ps ON ps.id = l.stage_id "
            " WHERE l.id = :i"
        ),
        {"i": r.lead_id},
    ).first()
    telefone, owner_id, origem, etapa, external_ref = linha
    assert telefone == TEL_A
    assert owner_id == CORRETORA_OK
    assert origem == c.ORIGEM_LEAD == "Instagram / rede social"
    assert etapa == "Novo"
    assert external_ref is None


def test_fone8_e_calculado_pelo_banco_nao_escrito(session):
    """`fone8` é coluna GERADA em produção. O writer não a menciona; quem
    calcula é o Postgres. Num banco de teste montado só por `create_all` a
    coluna não existe (ela veio de migration) — aí o teste diz o que falta em
    vez de mentir que passou."""
    existe = session.execute(
        sql_text(
            "SELECT 1 FROM information_schema.columns "
            " WHERE table_name = 'leads' AND column_name = 'fone8'"
        )
    ).scalar()
    if not existe:
        pytest.skip(
            "banco de teste sem a coluna gerada `fone8` — ver RELATORIO-9E10 §preparo"
        )
    r = _gravar(session, proposta())
    fone8 = session.execute(
        sql_text("SELECT fone8 FROM leads WHERE id = :i"), {"i": r.lead_id}
    ).scalar()
    assert fone8 == TEL_A[-8:]


_TABELAS_DE_MENSAGEM = (
    ("cadencia_estado", "telefone"),
    ("disparos_reengajamento", "telefone"),
    ("mensagens_whatsapp", "contato_telefone"),
)


def _fotografia_das_filas(session) -> dict[str, int]:
    """Quantas linhas cada motor de mensagem tem AGORA.

    Contagem relativa, não absoluta: o banco de teste é compartilhado com a
    suíte inteira e outro teste pode ter deixado linha na faixa. O que importa
    é que o cadastro não ACRESCENTE nenhuma.
    """
    return {
        tabela: int(
            session.execute(
                sql_text(f"SELECT count(*) FROM {tabela}")
            ).scalar()
        )
        for tabela, _ in _TABELAS_DE_MENSAGEM
    }


def test_cadastro_nao_dispara_boas_vindas_nem_cadencia_nem_disparo(session):
    """A prova que a OS pede: gravar não acorda nenhum motor de mensagem."""
    antes = _fotografia_das_filas(session)
    r = _gravar(session, proposta())

    tipos = [
        t[0]
        for t in session.execute(
            sql_text("SELECT tipo FROM lead_events WHERE lead_id = :i"), {"i": r.lead_id}
        ).all()
    ]
    assert tipos == [lw.EVENTO_CADASTRO]
    for proibido in ("boas_vindas_enviada", "intake", "cadencia_iniciada", "disparo"):
        assert proibido not in tipos

    assert _fotografia_das_filas(session) == antes, "um motor de mensagem ganhou linha"


def test_aprovar_sem_gravar_nao_cria_lead(session):
    """Aprovação é decisão; gravação é operação separada. Construir a proposta
    APROVADA e não chamar `gravar_cadastro` tem que deixar o banco intacto."""
    prop = proposta()
    assert prop.estado is c.EstadoProposta.APROVADO
    assert _conta_leads(session, TEL_A) == 0


def _codigo_sem_prosa(modulo) -> str:
    """Fonte do módulo SEM docstring e SEM comentário.

    A varredura tem que olhar o que o código faz, não o que ele explica: este
    módulo justifica em texto por que NÃO chama o intake, e uma busca em cima
    do arquivo cru acusaria a própria explicação.
    """
    arvore = ast.parse(inspect.getsource(modulo))
    for no in ast.walk(arvore):
        corpo = getattr(no, "body", None)
        if isinstance(corpo, list) and corpo:
            primeiro = corpo[0]
            if isinstance(primeiro, ast.Expr) and isinstance(primeiro.value, ast.Constant):
                if isinstance(primeiro.value.value, str):
                    corpo.pop(0)
    return ast.unparse(arvore)


def test_modulo_nao_importa_intake_roleta_nem_transporte():
    codigo = _codigo_sem_prosa(lw)
    for proibido in (
        "lead_intake",
        "registrar_lead",
        "roleta",
        "boas_vindas",
        "enviar_template",
        "EvolutionClient",
        "httpx",
        "requests",
        "graph.facebook.com",
        "fone8",
    ):
        assert proibido not in codigo, f"lead_writer não pode conter {proibido!r}"


def test_linha_de_automacao_bate_com_a_do_carrossel():
    """Se a linha da Corretor mudar de instância lá, o guard aqui tem que acompanhar
    — e o jeito de descobrir é este teste quebrar, não um lead entrar na roda."""
    from app.services import carrossel_corretor

    assert lw.INSTANCE_LINHA_REMARKETING == carrossel_corretor.INSTANCE_CORRETOR


# ── 2. Duplicata e DDD ───────────────────────────────────────────────────────


def test_duplicata_anexa_preservando_owner_etapa_e_origem(session):
    """Lead que já existe não é recriado nem editado: ganha uma interação."""
    session.execute(
        sql_text(
            "INSERT INTO leads (tenant_id, telefone, owner_id, origem, stage_id) "
            "VALUES (1, :t, :o, 'Kommo', "
            "        (SELECT id FROM pipeline_stages WHERE tenant_id=1 AND nome='Follow-up'))"
        ),
        {"t": TEL_A, "o": 2},
    )
    session.commit()
    antes = session.execute(
        sql_text("SELECT id, owner_id, origem, stage_id, updated_at FROM leads WHERE telefone=:t"),
        {"t": TEL_A},
    ).first()

    r = _gravar(session, proposta())
    assert r.anexado and not r.criado
    assert r.lead_id == antes[0]

    depois = session.execute(
        sql_text("SELECT owner_id, origem, stage_id, updated_at FROM leads WHERE id=:i"),
        {"i": r.lead_id},
    ).first()
    assert depois == (antes[1], antes[2], antes[3], antes[4])
    assert _conta_leads(session, TEL_A) == 1

    tipos = [
        t[0]
        for t in session.execute(
            sql_text("SELECT tipo FROM lead_events WHERE lead_id = :i"), {"i": r.lead_id}
        ).all()
    ]
    assert tipos == [lw.EVENTO_INTERACAO]


def test_telefone_de_outro_ddd_com_mesmo_sufixo_vira_lead_separado(session):
    a = _gravar(session, proposta(proposta_id="P-A", telefone=TEL_A))
    b = _gravar(
        session,
        proposta(proposta_id="P-B", telefone=TEL_OUTRO_DDD, ator_id="IGSID-2"),
    )
    assert a.lead_id != b.lead_id
    assert TEL_A[-8:] == TEL_OUTRO_DDD[-8:]  # o sufixo colide de propósito
    assert _conta_leads(session, TEL_A) == 1
    assert _conta_leads(session, TEL_OUTRO_DDD) == 1


def test_telefone_ambiguo_nao_vira_cadastro(session):
    with pytest.raises(lw.CadastroBloqueado) as erro:
        _gravar(session, proposta(telefone=TEL_AMBIGUO))
    assert c.MotivoBloqueio.TELEFONE_AMBIGUO.value in erro.value.motivos
    assert _conta_leads(session, TEL_AMBIGUO) == 0


# ── 3. Idempotência e concorrência ───────────────────────────────────────────


def test_mesma_proposta_duas_vezes_grava_um_lead_so(session):
    primeiro = _gravar(session, proposta())
    segundo = _gravar(session, proposta())
    assert segundo.lead_id == primeiro.lead_id
    assert segundo.repetido and not segundo.criado
    assert _conta_leads(session, TEL_A) == 1
    n = session.execute(
        sql_text("SELECT count(*) FROM lead_events WHERE lead_id = :i"),
        {"i": primeiro.lead_id},
    ).scalar()
    assert int(n) == 1


def test_duas_gravacoes_concorrentes_geram_exatamente_um_lead(session):
    """Duas propostas DIFERENTES, mesmo telefone, ao mesmo tempo.

    `leads` não tem unique por telefone — quem segura é o advisory lock. Sem
    ele este teste cria dois cards para a mesma pessoa, que é o bug que a
    corretora descobre ligando duas vezes.
    """
    from app.db.session import SessionLocal

    resultados: list[object] = []
    barreira = threading.Barrier(2)

    def grava(pid: str) -> None:
        s = SessionLocal()
        try:
            barreira.wait(timeout=10)
            resultados.append(
                lw.gravar_cadastro(
                    proposta(proposta_id=pid, ator_id=f"IGSID-{pid}"),
                    autorizacao=AUTORIZACAO,
                    session=s,
                    agora=AGORA,
                )
            )
        except Exception as exc:  # noqa: BLE001
            resultados.append(exc)
        finally:
            s.close()

    fios = [threading.Thread(target=grava, args=(p,)) for p in ("P-C1", "P-C2")]
    for f in fios:
        f.start()
    for f in fios:
        f.join(timeout=30)

    session.commit()  # enxerga o que os outros commitaram
    assert _conta_leads(session, TEL_A) == 1, resultados
    ok = [r for r in resultados if isinstance(r, lw.ResultadoCadastro)]
    assert len(ok) == 2
    assert len({r.lead_id for r in ok}) == 1
    # Um criou, o outro anexou. Ninguém recriou.
    assert sorted(r.criado for r in ok) == [False, True]


# ── 4. Revalidação na hora de gravar ─────────────────────────────────────────


def test_opt_out_que_chega_depois_da_aprovacao_bloqueia(session):
    """A proposta foi aprovada ontem; o "pare de me mandar" chegou hoje."""
    prop = proposta()
    session.execute(
        sql_text(
            "INSERT INTO cadencia_estado (tenant_id, telefone, status) "
            "VALUES (1, :t, 'opt_out')"
        ),
        {"t": TEL_A},
    )
    session.commit()

    with pytest.raises(lw.CadastroBloqueado) as erro:
        _gravar(session, prop)
    assert c.MotivoBloqueio.OPT_OUT_TELEFONE.value in erro.value.motivos
    assert _conta_leads(session, TEL_A) == 0


def test_consulta_de_opt_out_indisponivel_bloqueia(session):
    def explode(session_, telefone):  # noqa: ANN001
        raise RuntimeError("banco de opt-out fora do ar")

    with pytest.raises(lw.CadastroBloqueado) as erro:
        _gravar(session, proposta(), consulta_opt_out=explode)
    assert c.MotivoBloqueio.CONSULTA_SUPRESSAO_INDISPONIVEL.value in erro.value.motivos
    assert _conta_leads(session, TEL_A) == 0


def test_supressao_social_indisponivel_bloqueia(session):
    contexto = lw.ContextoCadastro(
        supressao_social=c.ConsultaSupressao(
            disponivel=False,
            bloqueado=True,
            motivos=(c.MotivoBloqueio.CONSULTA_SUPRESSAO_INDISPONIVEL,),
            consultado_em=AGORA,
        )
    )
    with pytest.raises(lw.CadastroBloqueado):
        _gravar(session, proposta(), contexto=contexto)
    assert _conta_leads(session, TEL_A) == 0


def test_opt_out_em_outro_telefone_vinculado_bloqueia(session):
    session.execute(
        sql_text(
            "INSERT INTO cadencia_estado (tenant_id, telefone, status) "
            "VALUES (1, :t, 'opt_out')"
        ),
        {"t": TEL_B},
    )
    session.commit()
    contexto = lw.ContextoCadastro(telefones_vinculados=(TEL_B,))
    with pytest.raises(lw.CadastroBloqueado) as erro:
        _gravar(session, proposta(), contexto=contexto)
    assert c.MotivoBloqueio.OPT_OUT_TELEFONE.value in erro.value.motivos


def test_nutricao_capturaria_o_lead_bloqueia(session):
    """Achado da auditoria: `nutricao.tick` casa por sufixo-8 e move o lead
    para o funil de remarketing, onde ele passa a receber template a cada 30
    dias. Gravar nessa condição é entregar a pessoa para a régua."""
    session.execute(
        sql_text(
            "INSERT INTO cadencia_estado (tenant_id, telefone, status) "
            "VALUES (1, :t, 'remarketing')"
        ),
        {"t": TEL_A},
    )
    session.commit()
    with pytest.raises(lw.CadastroBloqueado) as erro:
        _gravar(session, proposta())
    assert lw.MotivoCadastro.NUTRICAO_CAPTURARIA_O_LEAD.value in erro.value.motivos
    assert _conta_leads(session, TEL_A) == 0


@pytest.mark.parametrize(
    "owner,motivo",
    [
        (999_999, lw.MotivoCadastro.OWNER_INEXISTENTE),
        (CORRETORA_INATIVA, lw.MotivoCadastro.OWNER_INATIVO),
        (CORRETORA_AUTOMACAO, lw.MotivoCadastro.OWNER_DE_AUTOMACAO),
    ],
)
def test_owner_invalido_bloqueia(session, owner, motivo):
    with pytest.raises(lw.CadastroBloqueado) as erro:
        _gravar(session, proposta(owner_id=owner))
    assert motivo.value in erro.value.motivos
    assert _conta_leads(session, TEL_A) == 0


def test_sem_owner_escolhido_a_proposta_nem_fica_pronta():
    """A trava vem antes do writer: sem corretora escolhida por humano, o
    contrato da tarefa 1 já recusa montar a proposta aprovada."""
    with pytest.raises(c.ContratoInvalido):
        proposta(owner_id=None)


def test_proposta_nao_aprovada_bloqueia(session):
    with pytest.raises(lw.CadastroBloqueado) as erro:
        _gravar(session, proposta(estado=c.EstadoProposta.RASCUNHO))
    assert lw.MotivoCadastro.PROPOSTA_NAO_APROVADA.value in erro.value.motivos
    assert _conta_leads(session, TEL_A) == 0


def test_sem_autorizacao_da_dona_nao_grava(session):
    with pytest.raises(lw.CadastroBloqueado) as erro:
        lw.gravar_cadastro(
            proposta(),
            autorizacao=lw.AutorizacaoCadastro(autorizacao_dona_ref="  ", responsavel="assistente"),
            session=session,
            agora=AGORA,
        )
    assert lw.MotivoCadastro.AUTORIZACAO_AUSENTE.value in erro.value.motivos
    assert _conta_leads(session, TEL_A) == 0


def test_tenant_errado_bloqueia(session):
    prop = proposta().model_copy(update={"tenant_id": 2})
    with pytest.raises(lw.CadastroBloqueado) as erro:
        _gravar(session, prop)
    assert lw.MotivoCadastro.TENANT_ERRADO.value in erro.value.motivos


def test_bloqueio_da_proposta_nao_e_sobreposto(session):
    """Proposta bloqueada não fica aprovada (o contrato recusa), mas se alguém
    montar um objeto com bloqueio em outro estado, gravar continua recusando."""
    prop = proposta(
        estado=c.EstadoProposta.BLOQUEADO, bloqueios=(c.MotivoBloqueio.OPT_OUT_SOCIAL,)
    )
    with pytest.raises(lw.CadastroBloqueado) as erro:
        _gravar(session, prop)
    assert c.MotivoBloqueio.OPT_OUT_SOCIAL.value in erro.value.motivos


def test_idempotencia_indisponivel_bloqueia_em_vez_de_gravar_de_novo():
    """Fail-OPEN aqui seria "não consegui ver se já gravei, então gravo de
    novo" — que é a duplicata que a checagem existe para evitar."""

    class SessaoQuebrada:
        def begin_nested(self):
            return contextlib.nullcontext()

        def execute(self, *args, **kwargs):  # noqa: ANN002, ANN003
            raise RuntimeError("lead_events fora do ar")

    with pytest.raises(lw.CadastroBloqueado) as erro:
        lw._ja_gravado(SessaoQuebrada(), 1, "P-9-1")
    assert lw.MotivoCadastro.IDEMPOTENCIA_INDISPONIVEL.value in erro.value.motivos


def test_anexar_a_lead_inexistente_bloqueia(session):
    with pytest.raises(lw.CadastroBloqueado) as erro:
        _gravar(session, proposta(anexar=True, lead_existente_id=999_999))
    assert lw.MotivoCadastro.LEAD_EXISTENTE_DIVERGENTE.value in erro.value.motivos


# ── 5. O endpoint (tarefa 9) ─────────────────────────────────────────────────


@pytest.fixture
def api_cadastro(session, monkeypatch):
    """App isolado com só o router do cadastro. `app/main.py` não é tocado —
    registrar o router é integração própria, com nova autorização."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import instagram_acme_cadastro as api

    app = FastAPI()
    app.include_router(api.router)

    anterior = api.provedor_atual()
    monkeypatch.setattr(api, "_session_factory", lambda: session)
    yield api, TestClient(app, raise_server_exceptions=False)
    api.definir_provedor(anterior)


CORPO = {"autorizacao_dona_ref": "OS 9E10 · Dona 21/09", "versao": 1}


def _url(api, proposta_id: str = "P-9-1") -> str:
    return api.ROTA_CADASTRO.format(proposta_id=proposta_id)


def test_endpoint_de_cadastro_e_o_caminho_que_a_v1_reservou():
    """Divergência DECLARADA: `ROTAS_PROIBIDAS_V1` listava este caminho para
    que criá-lo exigisse revisão da OS. A revisão aconteceu (OS 9E10)."""
    from app.api import instagram_acme_cadastro as api

    assert api.ROTA_CADASTRO in c.ROTAS_PROIBIDAS_V1
    # E continua fora da fila de revisão da V1.
    assert api.ROTA_CADASTRO not in set(c.ENDPOINTS_PAINEL.values())
    assert {r.path for r in api.router.routes} == {api.ROTA_CADASTRO}


def test_router_de_cadastro_nao_esta_registrado_em_main():
    import pathlib

    main = pathlib.Path("app/main.py").read_text()
    assert "instagram_acme_cadastro" not in main
    assert "instagram_acme_envio" not in main


def test_endpoint_recusa_anonimo_e_corretora(api_cadastro):
    api, cliente = api_cadastro
    assert cliente.post(_url(api), json=CORPO).status_code == 403
    r = cliente.post(_url(api), json=CORPO, headers={"X-Painel-User": "corretor"})
    assert r.status_code == 403


def test_endpoint_sem_repositorio_de_propostas_devolve_503(api_cadastro):
    api, cliente = api_cadastro
    r = cliente.post(_url(api), json=CORPO, headers={"X-Painel-User": "assistente"})
    assert r.status_code == 503


def test_endpoint_grava_e_e_idempotente(api_cadastro, session):
    api, cliente = api_cadastro
    api.definir_provedor(lambda tenant_id, pid: (proposta(proposta_id=pid), lw.ContextoCadastro()))

    r = cliente.post(_url(api), json=CORPO, headers={"X-Painel-User": "assistente"})
    assert r.status_code == 200, r.text
    corpo = r.json()
    assert corpo["criado"] is True and corpo["origem"] == c.ORIGEM_LEAD

    de_novo = cliente.post(_url(api), json=CORPO, headers={"X-Painel-User": "assistente"})
    assert de_novo.json()["repetido"] is True
    assert de_novo.json()["lead_id"] == corpo["lead_id"]
    assert _conta_leads(session, TEL_A) == 1


def test_endpoint_devolve_409_quando_a_proposta_mudou_de_versao(api_cadastro):
    api, cliente = api_cadastro
    api.definir_provedor(
        lambda tenant_id, pid: (
            proposta(proposta_id=pid).model_copy(update={"versao": 3}),
            lw.ContextoCadastro(),
        )
    )
    r = cliente.post(_url(api), json=CORPO, headers={"X-Painel-User": "assistente"})
    assert r.status_code == 409


def test_endpoint_devolve_422_com_os_motivos(api_cadastro, session):
    api, cliente = api_cadastro
    api.definir_provedor(
        lambda tenant_id, pid: (
            proposta(proposta_id=pid, owner_id=CORRETORA_AUTOMACAO),
            lw.ContextoCadastro(),
        )
    )
    r = cliente.post(_url(api), json=CORPO, headers={"X-Painel-User": "assistente"})
    assert r.status_code == 422
    assert lw.MotivoCadastro.OWNER_DE_AUTOMACAO.value in str(r.json())
    assert _conta_leads(session, TEL_A) == 0
