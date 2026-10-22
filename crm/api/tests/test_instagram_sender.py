"""Envio assistido da Assistente (tarefa 10) — transporte FALSO, rede bloqueada.

Nada aqui manda mensagem. O transporte é um duplê que registra as chamadas, o
socket está bloqueado por fixture autouse, e o `conftest` da suíte já roda com
`COPILOTO_UNDER_PYTEST=1` e `DRY_RUN=true`.

O que estes testes provam, na ordem da OS:

* aprovar sem acionar o envio = **zero** chamadas de rede;
* envio nasce DESARMADO e continua desarmado sem as três travas abertas;
* duplo clique, concorrência, opt-out tardio, janela vencida, texto editado,
  autorização ausente e timeout **não** produzem envio indevido;
* timeout vira `incerto` e incerto **não** repete.

Como rodar:

    TEST_DATABASE_URL=postgresql+psycopg://<user>:<senha>@127.0.0.1:5433/<banco_descartavel> \\
        .venv/bin/python -m pytest tests/test_instagram_sender.py -q
"""
from __future__ import annotations

import ast
import inspect
import socket
import threading
from datetime import datetime, timedelta, timezone

import pytest

from app.services.instagram_acme import contracts as c
from app.services.instagram_acme import sender as snd

AGORA = datetime(2026, 9, 21, 12, 0, 0, tzinfo=timezone.utc)
IGSID = "000000000000000"
COMENTARIO_ID = "000000000000000"


# ── Ambiente ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _sem_rede(monkeypatch: pytest.MonkeyPatch) -> None:
    def proibido(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("teste de envio tentou abrir conexão de rede")

    monkeypatch.setattr(socket, "create_connection", proibido)
    monkeypatch.setattr(socket.socket, "connect", proibido)


class TransporteFalso:
    """Duplê. Guarda cada chamada e devolve o que o teste mandar devolver."""

    def __init__(self, resposta: snd.RespostaTransporte | None = None, erro: Exception | None = None):
        self.chamadas: list[dict] = []
        self.resposta = resposta or snd.RespostaTransporte(ok=True, id_externo="mid_1")
        self.erro = erro

    def enviar(self, *, acao, destino, texto):  # noqa: ANN001, ANN003
        self.chamadas.append({"acao": acao, "destino": destino, "texto": texto})
        if self.erro is not None:
            raise self.erro
        return self.resposta


@pytest.fixture
def repo():
    anterior, duravel = snd.repositorio_atual(), snd._repositorio_e_duravel()
    novo = snd.RegistroEnviosMemoria()
    # `duravel=True` é a afirmação que a tarefa 3 vai fazer de verdade. Aqui é
    # o teste assumindo a responsabilidade, explicitamente, para poder exercer
    # o caminho armado.
    snd.definir_repositorio(novo, duravel=True)
    yield novo
    snd.definir_repositorio(anterior, duravel=duravel)


@pytest.fixture
def armado(monkeypatch: pytest.MonkeyPatch, tmp_path, repo):
    """Abre as três travas: chave no banco, sem arquivo de pânico, repo durável."""
    monkeypatch.setattr(snd, "_chave_ativa_no_banco", lambda: True)
    return str(tmp_path / "assistente-envio.off")  # caminho que NÃO existe


# ── Fábricas ─────────────────────────────────────────────────────────────────


def identidade(ator_id: str = IGSID) -> c.IdentidadeSocial:
    return c.IdentidadeSocial(
        plataforma=c.Plataforma.INSTAGRAM,
        conta=c.CONTA_INSTAGRAM,
        ator_id=ator_id,
        username="fulana",
    )


def proposta(
    *,
    proposta_id: str = "P-10-1",
    versao: int = 1,
    estado: c.EstadoProposta = c.EstadoProposta.APROVADO,
    acao: c.AcaoProposta = c.AcaoProposta.DM_INICIAL,
    bloqueios: tuple[c.MotivoBloqueio, ...] = (),
    pendencias: tuple[str, ...] = (),
    ator_id: str = IGSID,
) -> c.PropostaBase:
    return c.PropostaBase(
        proposta_id=proposta_id,
        acao=acao,
        estado=estado,
        versao=versao,
        identidade=identidade(ator_id),
        evento_id="MSG-1",
        bloqueios=bloqueios,
        pendencias=pendencias,
        revisora="corretor",
        criado_em=AGORA - timedelta(hours=2),
        atualizado_em=AGORA - timedelta(minutes=10),
    )


_PADRAO = object()


def alvo(
    *,
    acao: snd.AcaoEnvio = snd.AcaoEnvio.CONTINUIDADE,
    texto: str = "Oi! Aqui é a Acme. Vi sua mensagem — te respondo já.",
    ultima_recebida=_PADRAO,
    ator_id: str = IGSID,
) -> snd.AlvoEnvio:
    return snd.AlvoEnvio(
        acao=acao,
        identidade=identidade(ator_id),
        texto=texto,
        comentario_id=COMENTARIO_ID,
        comentario_criado_em=AGORA - timedelta(days=1),
        thread_id="THREAD-1",
        ultima_mensagem_recebida_em=(
            AGORA - timedelta(hours=1) if ultima_recebida is _PADRAO else ultima_recebida
        ),
    )


def estado(
    *,
    bloqueado: bool = False,
    disponivel: bool = True,
    conversa_humana: bool = False,
    estado_conhecido: bool = True,
    dms: int = 0,
    publicas: int = 0,
    ident: c.EstadoIdentidade = c.EstadoIdentidade.NAO_CONCILIADA,
) -> snd.EstadoNoEnvio:
    motivos: tuple[c.MotivoBloqueio, ...] = ()
    if not disponivel:
        motivos = (c.MotivoBloqueio.CONSULTA_SUPRESSAO_INDISPONIVEL,)
        bloqueado = True
    elif bloqueado:
        motivos = (c.MotivoBloqueio.OPT_OUT_SOCIAL,)
    return snd.EstadoNoEnvio(
        supressao=c.ConsultaSupressao(
            disponivel=disponivel,
            bloqueado=bloqueado,
            motivos=motivos,
            consultado_em=AGORA,
        ),
        consumo=c.ConsumoTeto(
            identidade=identidade(),
            comentario_id=COMENTARIO_ID,
            respostas_publicas_registradas=publicas,
            dms_iniciais_registradas=dms,
            estado_conhecido=estado_conhecido,
        ),
        estado_identidade=ident,
        conversa_humana_em_andamento=conversa_humana,
    )


AUTORIZACAO = snd.AutorizacaoEnvio(
    autorizacao_dona_ref="OS-ASSISTENTE-IA-TAREFA-9E10 · Dona 21/09/2026 09:29 UTC",
    operador="assistente",
    versao=1,
)


def _enviar(transporte, panico, *, prop=None, alv=None, est=None, autorizacao=AUTORIZACAO, agora=AGORA):
    return snd.enviar(
        prop or proposta(),
        alv or alvo(),
        est or estado(),
        autorizacao=autorizacao,
        transporte=transporte,
        agora=agora,
        arquivo_panico=panico,
    )


# ── 1. Armamento ─────────────────────────────────────────────────────────────


def test_envio_nasce_desarmado(monkeypatch, tmp_path):
    """Sem tocar em nada: desarmado. É o estado de entrega desta tarefa."""
    monkeypatch.setattr(snd, "_chave_ativa_no_banco", lambda: True)
    # Repositório é o de memória (volátil) → não arma, mesmo com a chave em true.
    assert snd.armado(arquivo_panico=str(tmp_path / "nao-existe")) is False


def test_tres_travas_precisam_estar_abertas(monkeypatch, tmp_path, repo):
    inexistente = str(tmp_path / "assistente-envio.off")

    monkeypatch.setattr(snd, "_chave_ativa_no_banco", lambda: False)
    assert snd.armado(arquivo_panico=inexistente) is False

    monkeypatch.setattr(snd, "_chave_ativa_no_banco", lambda: True)
    assert snd.armado(arquivo_panico=inexistente) is True


def test_botao_de_panico_vence_a_chave_do_banco(monkeypatch, tmp_path, repo):
    monkeypatch.setattr(snd, "_chave_ativa_no_banco", lambda: True)
    panico = tmp_path / "assistente-envio.off"
    assert snd.armado(arquivo_panico=str(panico)) is True
    panico.touch()  # o comando do botão de pânico é exatamente isto
    assert snd.armado(arquivo_panico=str(panico)) is False


def test_banco_fora_do_ar_desarma(monkeypatch):
    """Fail-closed: não conseguir ler a chave não é "pode mandar"."""
    import app.db.session as dbs

    def explode():
        raise RuntimeError("banco fora do ar")

    monkeypatch.setattr(dbs, "SessionLocal", explode)
    assert snd._chave_ativa_no_banco() is False


def test_desarmado_recusa_e_nao_chama_transporte(tmp_path):
    t = TransporteFalso()
    with pytest.raises(c.EnvioNaoPermitido):
        _enviar(t, str(tmp_path / "nao-existe"))
    assert t.chamadas == []


def test_transporte_padrao_recusa_alto():
    with pytest.raises(c.EnvioNaoPermitido):
        snd.TransporteBloqueado().enviar(
            acao=snd.AcaoEnvio.CONTINUIDADE,
            destino=snd.DestinoTransporte(destinatario_id=IGSID),
            texto="oi",
        )


# ── 2. Aprovar não envia ─────────────────────────────────────────────────────


def test_aprovar_sem_acionar_envio_nao_gera_chamada(armado, repo):
    """Construir a proposta APROVADA e não chamar `enviar` = zero rede, zero
    reserva. Não existe hook que feche esse caminho por fora."""
    t = TransporteFalso()
    prop = proposta()
    assert prop.estado is c.EstadoProposta.APROVADO
    assert t.chamadas == []
    assert repo._por_chave == {}


def test_nenhum_cron_ou_consumo_automatico_de_fila():
    codigo = _codigo_sem_prosa(snd)
    for proibido in ("repeat_every", "BackgroundScheduler", "crontab", "APScheduler",
                     "celery", "while True"):
        assert proibido not in codigo, f"sender não pode conter {proibido!r}"


def _codigo_sem_prosa(modulo) -> str:
    arvore = ast.parse(inspect.getsource(modulo))
    for no in ast.walk(arvore):
        corpo = getattr(no, "body", None)
        if isinstance(corpo, list) and corpo:
            primeiro = corpo[0]
            if isinstance(primeiro, ast.Expr) and isinstance(primeiro.value, ast.Constant):
                if isinstance(primeiro.value.value, str):
                    corpo.pop(0)
    return ast.unparse(arvore)


# ── 3. O caminho liberado: continuidade ──────────────────────────────────────


def test_continuidade_dentro_de_24h_envia_uma_vez(armado, repo):
    t = TransporteFalso()
    r = _enviar(t, armado)
    assert r.enviado and r.estado is snd.EstadoReserva.ENVIADO and r.id_externo == "mid_1"
    assert len(t.chamadas) == 1
    chamada = t.chamadas[0]
    assert chamada["acao"] is snd.AcaoEnvio.CONTINUIDADE
    # Continuidade vai pelo IGSID da pessoa, não por comentário e não por @.
    assert chamada["destino"].destinatario_id == IGSID
    assert chamada["destino"].comentario_id is None


def test_continuidade_nao_consome_o_teto_de_dm_inicial(armado, repo):
    """CONTRATOS §5: continuidade é resposta, não abordagem. Quem já recebeu a
    DM inicial continua podendo ser respondido quando escreve."""
    t = TransporteFalso()
    r = _enviar(t, armado, est=estado(dms=1))
    assert r.enviado


@pytest.mark.parametrize(
    "atraso,esperado",
    [
        (timedelta(hours=23, minutes=59), True),
        (timedelta(hours=24), False),          # fecha NO instante exato
        (timedelta(hours=24, seconds=1), False),
    ],
)
def test_janela_de_24h_fecha_no_instante(armado, repo, atraso, esperado):
    t = TransporteFalso()
    alv = alvo(ultima_recebida=AGORA - atraso)
    if esperado:
        assert _enviar(t, armado, alv=alv).enviado
        assert len(t.chamadas) == 1
    else:
        with pytest.raises(snd.EnvioBloqueado) as erro:
            _enviar(t, armado, alv=alv)
        assert c.MotivoBloqueio.JANELA_CONTINUIDADE_VENCIDA.value in erro.value.motivos
        assert t.chamadas == []


def test_pessoa_que_nunca_escreveu_nao_tem_continuidade(armado, repo):
    t = TransporteFalso()
    with pytest.raises(snd.EnvioBloqueado):
        _enviar(t, armado, alv=alvo(ultima_recebida=None))
    assert t.chamadas == []


# ── 4. As ações que a autorização de 21/09 NÃO cobre ─────────────────────────


@pytest.mark.parametrize(
    "acao,acao_proposta",
    [
        (snd.AcaoEnvio.DM_INICIAL, c.AcaoProposta.DM_INICIAL),
        (snd.AcaoEnvio.RESPOSTA_PUBLICA, c.AcaoProposta.RESPOSTA_PUBLICA),
    ],
)
def test_acao_nao_liberada_e_recusada_mesmo_armado(armado, repo, acao, acao_proposta):
    """Private reply e resposta pública têm código e teste; o que falta é
    autorização. `ACOES_LIBERADAS` é a trava, e ela não é configuração."""
    t = TransporteFalso()
    with pytest.raises(snd.EnvioBloqueado) as erro:
        _enviar(t, armado, prop=proposta(acao=acao_proposta), alv=alvo(acao=acao))
    assert snd.MotivoEnvio.ACAO_NAO_LIBERADA.value in erro.value.motivos
    assert t.chamadas == []


def test_acao_pedida_tem_que_bater_com_a_proposta_aprovada(armado, repo):
    t = TransporteFalso()
    with pytest.raises(snd.EnvioBloqueado) as erro:
        _enviar(t, armado, prop=proposta(acao=c.AcaoProposta.RESPOSTA_PUBLICA))
    assert snd.MotivoEnvio.ACAO_INCOMPATIVEL_COM_A_PROPOSTA.value in erro.value.motivos
    assert t.chamadas == []


def test_identidade_da_proposta_tem_que_ser_a_do_alvo(armado, repo):
    t = TransporteFalso()
    with pytest.raises(snd.EnvioBloqueado) as erro:
        _enviar(t, armado, alv=alvo(ator_id="OUTRA-PESSOA"))
    assert c.MotivoBloqueio.IDENTIDADE_NAO_CONCILIADA.value in erro.value.motivos
    assert t.chamadas == []


# ── 5. Revalidação no instante do envio ──────────────────────────────────────


def test_opt_out_que_chega_depois_da_aprovacao_bloqueia(armado, repo):
    t = TransporteFalso()
    with pytest.raises(snd.EnvioBloqueado) as erro:
        _enviar(t, armado, est=estado(bloqueado=True))
    assert c.MotivoBloqueio.OPT_OUT_SOCIAL.value in erro.value.motivos
    assert t.chamadas == []
    assert repo._por_chave == {}  # nem reserva foi criada


def test_consulta_de_supressao_indisponivel_bloqueia(armado, repo):
    t = TransporteFalso()
    with pytest.raises(snd.EnvioBloqueado) as erro:
        _enviar(t, armado, est=estado(disponivel=False))
    assert c.MotivoBloqueio.CONSULTA_SUPRESSAO_INDISPONIVEL.value in erro.value.motivos
    assert t.chamadas == []


def test_conversa_humana_em_andamento_bloqueia(armado, repo):
    t = TransporteFalso()
    with pytest.raises(snd.EnvioBloqueado) as erro:
        _enviar(t, armado, est=estado(conversa_humana=True))
    assert c.MotivoBloqueio.CONVERSA_HUMANA_EM_ANDAMENTO.value in erro.value.motivos
    assert t.chamadas == []


def test_estado_de_envio_desconhecido_bloqueia(armado, repo):
    t = TransporteFalso()
    with pytest.raises(snd.EnvioBloqueado) as erro:
        _enviar(t, armado, est=estado(estado_conhecido=False))
    assert c.MotivoBloqueio.ESTADO_DE_ENVIO_DESCONHECIDO.value in erro.value.motivos


def test_texto_com_pendencia_nao_sai(armado, repo):
    """`{{PENDENTE: ...}}` é recado para a revisora (ATENDIMENTO.md §4).
    Sair assim é a Acme mandando o próprio to-do para o cliente."""
    t = TransporteFalso()
    with pytest.raises(snd.EnvioBloqueado) as erro:
        _enviar(t, armado, alv=alvo(texto="Esse é o Bairro X. {{PENDENTE: bairro}}"))
    assert snd.MotivoEnvio.TEXTO_COM_PENDENCIA.value in erro.value.motivos
    assert t.chamadas == []


def test_texto_vazio_nao_sai(armado, repo):
    t = TransporteFalso()
    with pytest.raises(snd.EnvioBloqueado):
        _enviar(t, armado, alv=alvo(texto="   "))
    assert t.chamadas == []


def test_proposta_nao_aprovada_nao_sai(armado, repo):
    t = TransporteFalso()
    with pytest.raises(snd.EnvioBloqueado) as erro:
        _enviar(t, armado, prop=proposta(estado=c.EstadoProposta.RASCUNHO))
    assert snd.MotivoEnvio.PROPOSTA_NAO_APROVADA.value in erro.value.motivos
    assert t.chamadas == []


def test_autorizacao_ausente_nao_sai(armado, repo):
    t = TransporteFalso()
    sem = snd.AutorizacaoEnvio(autorizacao_dona_ref="  ", operador="assistente", versao=1)
    with pytest.raises(snd.EnvioBloqueado) as erro:
        _enviar(t, armado, autorizacao=sem)
    assert snd.MotivoEnvio.AUTORIZACAO_AUSENTE.value in erro.value.motivos
    assert t.chamadas == []


def test_texto_editado_invalida_a_autorizacao(armado, repo):
    """Editar gera versão nova (CONTRATOS §7.3). A autorização veio da versão
    que o operador leu — se a proposta andou, a decisão não é mais a mesma."""
    t = TransporteFalso()
    with pytest.raises(c.VersaoDesatualizada):
        _enviar(t, armado, prop=proposta(versao=2))
    assert t.chamadas == []


# ── 6. Reserva: duplo clique, concorrência, timeout ──────────────────────────


def test_duplo_clique_manda_uma_vez_so(armado, repo):
    t = TransporteFalso()
    assert _enviar(t, armado).enviado
    with pytest.raises(snd.EnvioBloqueado) as erro:
        _enviar(t, armado)
    assert snd.MotivoEnvio.RESERVA_JA_EXISTE.value in erro.value.motivos
    assert len(t.chamadas) == 1


def test_concorrencia_manda_uma_vez_so(armado, repo):
    """Dois cliques ao mesmo tempo, em fios diferentes. A reserva vem ANTES da
    rede justamente para que o segundo encontre o primeiro."""
    t = TransporteFalso()
    barreira = threading.Barrier(2)
    resultados: list[object] = []
    trava = threading.Lock()

    def clica():
        try:
            barreira.wait(timeout=10)
            r = _enviar(t, armado)
        except Exception as exc:  # noqa: BLE001
            r = exc
        with trava:
            resultados.append(r)

    fios = [threading.Thread(target=clica) for _ in range(2)]
    for f in fios:
        f.start()
    for f in fios:
        f.join(timeout=30)

    assert len(t.chamadas) == 1, resultados
    enviados = [r for r in resultados if isinstance(r, snd.ResultadoEnvio) and r.enviado]
    assert len(enviados) == 1


def test_timeout_vira_incerto_e_incerto_nao_repete(armado, repo):
    """A Meta pode ter processado. Repetir é mandar a segunda mensagem para
    quem pediu uma — e a OS é explícita: timeout não autoriza repetir."""
    t = TransporteFalso(
        resposta=snd.RespostaTransporte(
            ok=False, erro="ReadTimeout", classe=snd.ClasseFalha.INCERTA
        )
    )
    r = _enviar(t, armado)
    assert not r.enviado and r.estado is snd.EstadoReserva.INCERTO

    with pytest.raises(snd.EnvioBloqueado) as erro:
        _enviar(t, armado)
    assert snd.MotivoEnvio.RESULTADO_INCERTO_ANTERIOR.value in erro.value.motivos
    assert len(t.chamadas) == 1


def test_excecao_dentro_do_transporte_tambem_vira_incerto(armado, repo):
    t = TransporteFalso(erro=OSError("conexão cortada"))
    r = _enviar(t, armado)
    assert r.estado is snd.EstadoReserva.INCERTO
    assert repo.por_chave(r.chave).estado is snd.EstadoReserva.INCERTO


def test_rate_limit_recua_e_permite_nova_tentativa_manual(armado, repo):
    """Rate limit = a Meta NÃO processou. Tentar de novo depois não é sequência
    comercial: é a mesma mensagem, que nunca saiu."""
    t = TransporteFalso(
        resposta=snd.RespostaTransporte(
            ok=False, erro="4:rate limit", classe=snd.ClasseFalha.TRANSITORIA
        )
    )
    r = _enviar(t, armado)
    assert r.estado is snd.EstadoReserva.FALHOU_TRANSITORIO

    t.resposta = snd.RespostaTransporte(ok=True, id_externo="mid_2")
    r2 = _enviar(t, armado)
    assert r2.enviado and repo.por_chave(r2.chave).tentativas == 2
    assert len(t.chamadas) == 2


def test_erro_de_permissao_para_e_nao_tenta_de_novo(armado, repo):
    t = TransporteFalso(
        resposta=snd.RespostaTransporte(
            ok=False, erro="10:sem permissão", classe=snd.ClasseFalha.PERMANENTE
        )
    )
    r = _enviar(t, armado)
    assert r.estado is snd.EstadoReserva.FALHOU_PERMANENTE
    with pytest.raises(snd.EnvioBloqueado):
        _enviar(t, armado)
    assert len(t.chamadas) == 1


def test_chave_de_reserva_separa_versao_e_acao():
    a = snd.chave_de_reserva("P-1", 1, snd.AcaoEnvio.CONTINUIDADE)
    assert a != snd.chave_de_reserva("P-1", 2, snd.AcaoEnvio.CONTINUIDADE)
    assert a != snd.chave_de_reserva("P-1", 1, snd.AcaoEnvio.DM_INICIAL)


# ── 7. Transporte real: forma da chamada, sem rede ───────────────────────────


def test_private_reply_usa_id_do_comentario_nunca_username(monkeypatch):
    """A OS é literal: "Private reply usa ID do comentário, não username"."""
    import httpx

    capturado: dict = {}

    class RespostaFake:
        status_code = 200

        @staticmethod
        def json():
            return {"message_id": "mid_graph"}

    def fake_post(url, **kwargs):  # noqa: ANN001, ANN003
        capturado["url"] = url
        capturado["json"] = kwargs.get("json")
        capturado["params"] = kwargs.get("params")
        return RespostaFake()

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(
        "app.config.settings.intake_meta_page_token", "token-descartavel", raising=False
    )

    r = snd.TransporteGraph().enviar(
        acao=snd.AcaoEnvio.DM_INICIAL,
        destino=snd.DestinoTransporte(comentario_id=COMENTARIO_ID),
        texto="oi",
    )
    assert r.ok and r.id_externo == "mid_graph"
    assert capturado["json"]["recipient"] == {"comment_id": COMENTARIO_ID}
    assert "username" not in str(capturado["json"])
    assert c.PAGINA_FACEBOOK in capturado["url"]


def test_continuidade_no_transporte_real_usa_igsid(monkeypatch):
    import httpx

    capturado: dict = {}

    class RespostaFake:
        status_code = 200

        @staticmethod
        def json():
            return {"message_id": "mid_graph"}

    monkeypatch.setattr(
        httpx, "post", lambda url, **kw: (capturado.update(kw), RespostaFake())[1]
    )
    monkeypatch.setattr(
        "app.config.settings.intake_meta_page_token", "token-descartavel", raising=False
    )
    snd.TransporteGraph().enviar(
        acao=snd.AcaoEnvio.CONTINUIDADE,
        destino=snd.DestinoTransporte(destinatario_id=IGSID),
        texto="oi",
    )
    assert capturado["json"]["recipient"] == {"id": IGSID}


def test_transporte_real_sem_token_nao_chama_a_graph(monkeypatch):
    import httpx

    monkeypatch.setattr(
        httpx, "post", lambda *a, **k: pytest.fail("chamou a Graph sem token")
    )
    monkeypatch.setattr("app.config.settings.intake_meta_page_token", "", raising=False)
    r = snd.TransporteGraph().enviar(
        acao=snd.AcaoEnvio.CONTINUIDADE,
        destino=snd.DestinoTransporte(destinatario_id=IGSID),
        texto="oi",
    )
    assert not r.ok and r.classe is snd.ClasseFalha.PERMANENTE


def test_erro_da_graph_nao_vaza_token(monkeypatch):
    import httpx

    class RespostaErro:
        status_code = 400

        @staticmethod
        def json():
            return {
                "error": {
                    "code": 190,
                    "message": "Invalid OAuth: access_token=SEGREDO-DE-VERDADE",
                }
            }

    monkeypatch.setattr(httpx, "post", lambda *a, **k: RespostaErro())
    monkeypatch.setattr(
        "app.config.settings.intake_meta_page_token", "SEGREDO-DE-VERDADE", raising=False
    )
    r = snd.TransporteGraph().enviar(
        acao=snd.AcaoEnvio.CONTINUIDADE,
        destino=snd.DestinoTransporte(destinatario_id=IGSID),
        texto="oi",
    )
    assert not r.ok
    assert "SEGREDO-DE-VERDADE" not in (r.erro or "")


def test_5xx_da_graph_vira_incerto_e_nao_permanente(monkeypatch):
    import httpx

    class Resposta500:
        status_code = 500

        @staticmethod
        def json():
            return {"error": {"code": 2, "message": "temporário"}}

    monkeypatch.setattr(httpx, "post", lambda *a, **k: Resposta500())
    monkeypatch.setattr("app.config.settings.intake_meta_page_token", "t", raising=False)
    r = snd.TransporteGraph().enviar(
        acao=snd.AcaoEnvio.CONTINUIDADE,
        destino=snd.DestinoTransporte(destinatario_id=IGSID),
        texto="oi",
    )
    assert r.classe is snd.ClasseFalha.INCERTA


def test_resposta_publica_ainda_nao_tem_transporte(monkeypatch):
    """Honestidade de escopo: a ação está travada, então o caminho HTTP dela
    não foi escrito. Melhor recusar explícito do que ter código que nunca roda."""
    monkeypatch.setattr("app.config.settings.intake_meta_page_token", "t", raising=False)
    r = snd.TransporteGraph().enviar(
        acao=snd.AcaoEnvio.RESPOSTA_PUBLICA,
        destino=snd.DestinoTransporte(comentario_id=COMENTARIO_ID),
        texto="oi",
    )
    assert not r.ok and r.erro == "acao_sem_transporte"


# ── 8. Tetos do contrato seguem válidos ──────────────────────────────────────


def test_teto_de_envio_do_sistema_continua_zero():
    """A OS 9E10 abriu uma EXCEÇÃO nomeada, não afrouxou o teto."""
    assert c.TETOS_PADRAO.system_send_limit == 0
    assert c.TETOS_PADRAO.max_automatic_followups == 0
    with pytest.raises(c.TetoInvalido):
        c.TetosAssistente(system_send_limit=1)


def test_acoes_liberadas_e_so_a_continuidade():
    assert snd.ACOES_LIBERADAS == frozenset({snd.AcaoEnvio.CONTINUIDADE})


# ── 9. O endpoint (tarefa 10) ────────────────────────────────────────────────


@pytest.fixture
def api_envio():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import instagram_acme_envio as api

    app = FastAPI()
    app.include_router(api.router)
    anterior = api.provedor_atual()
    yield api, TestClient(app, raise_server_exceptions=False)
    api.definir_provedor(anterior)


CORPO = {"autorizacao_dona_ref": "OS 9E10 · Dona 21/09", "versao": 1}


def test_endpoint_de_envio_e_o_caminho_que_a_v1_reservou():
    from app.api import instagram_acme_envio as api

    assert api.ROTA_ENVIO in c.ROTAS_PROIBIDAS_V1
    assert api.ROTA_ENVIO not in set(c.ENDPOINTS_PAINEL.values())
    assert {r.path for r in api.router.routes} == {api.ROTA_ENVIO, api.ROTA_ENVIO_STATUS}


def test_endpoint_nao_tem_rota_de_lote():
    from app.api import instagram_acme_envio as api

    for rota in {r.path for r in api.router.routes}:
        for proibido in ("lote", "todas", "fila", "batch", "bulk"):
            assert proibido not in rota, f"{rota} parece envio em massa"


def test_endpoint_recusa_anonimo_e_corretora(api_envio):
    api, cliente = api_envio
    url = api.ROTA_ENVIO.format(proposta_id="P-10-1")
    assert cliente.post(url, json=CORPO).status_code == 403
    assert cliente.post(url, json=CORPO, headers={"X-Painel-User": "corretor"}).status_code == 403


def test_endpoint_devolve_423_enquanto_o_envio_estiver_desarmado(api_envio, monkeypatch):
    """É o estado de HOJE, e a resposta diz isso sem ambiguidade: 423 Locked,
    não 500 e não 200."""
    api, cliente = api_envio
    api.definir_provedor(lambda tenant_id, pid: (proposta(proposta_id=pid), alvo(), estado()))
    monkeypatch.setattr(snd, "_chave_ativa_no_banco", lambda: True)  # nem assim
    r = cliente.post(
        api.ROTA_ENVIO.format(proposta_id="P-10-1"),
        json=CORPO,
        headers={"X-Painel-User": "assistente"},
    )
    assert r.status_code == 423


def test_endpoint_sem_provedor_devolve_503(api_envio):
    api, cliente = api_envio
    r = cliente.post(
        api.ROTA_ENVIO.format(proposta_id="P-10-1"),
        json=CORPO,
        headers={"X-Painel-User": "assistente"},
    )
    assert r.status_code == 503


def test_status_do_envio_e_leitura_pura(api_envio, armado, repo):
    api, cliente = api_envio
    api.definir_provedor(lambda tenant_id, pid: (proposta(proposta_id=pid), alvo(), estado()))
    r = cliente.get(
        api.ROTA_ENVIO_STATUS.format(proposta_id="P-10-1"),
        params={"versao": 1, "acao": "continuidade"},
        headers={"X-Painel-User": "assistente"},
    )
    assert r.status_code == 200
    assert r.json()["estado"] is None  # nenhuma reserva foi criada pela leitura
    assert repo._por_chave == {}
