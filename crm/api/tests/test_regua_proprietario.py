"""Régua do proprietário — "seu imóvel ainda está à venda?" a cada 40 dias.

O que estes testes protegem, em ordem de dano:

  1. a régua NASCE DESARMADA — sai pela linha da casa, pro telefone de gente
     real, perguntando sobre o imóvel dela;
  2. o texto sai em português de gente ("sua casa está cadastrada", não "seu
     casa está cadastrado") — é o que separa mensagem de imobiliária de
     mensagem de robô;
  3. a classificação da resposta é CONSERVADORA: marcar "vendido" por engano
     some com o imóvel da carteira.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.services import regua_proprietario as rp

SP = ZoneInfo("America/Sao_Paulo")


def test_nasce_desarmada():
    """Sem a Dona ligar, nada sai — nem em dry-run."""
    res = rp.tick(dry_run=True)
    assert res.enviados == 0
    assert res.bloqueios.get("desarmada") == 1


def test_janela_respeita_domingo_e_horario():
    dom = datetime(2026, 9, 20, 14, 0, tzinfo=SP)      # domingo
    assert rp._dentro_da_janela(dom) == (False, "domingo")

    madrugada = datetime(2026, 9, 17, 3, 0, tzinfo=SP)
    assert rp._dentro_da_janela(madrugada) == (False, "fora_do_horario")

    noite = datetime(2026, 9, 17, 19, 30, tzinfo=SP)   # 19h é o fim
    assert rp._dentro_da_janela(noite) == (False, "fora_do_horario")

    comercial = datetime(2026, 9, 17, 10, 0, tzinfo=SP)
    assert rp._dentro_da_janela(comercial) == (True, "")


def test_texto_concorda_em_genero():
    casa, _ = rp._texto({"tipo": "casa", "bairro": "Bairro X", "preco_pedido": 425000})
    assert "sua casa" in casa and "cadastrada" in casa and "Ela continua" in casa

    apto, _ = rp._texto({"tipo": "apartamento", "bairro": "Bairro X", "preco_pedido": 285000})
    assert "seu apartamento" in apto and "cadastrado" in apto and "Ele continua" in apto


def test_texto_usa_em_e_nao_no():
    """O gênero do bairro é imprevisível: "no Bairro X" e "na Bairro A" erram."""
    t, _ = rp._texto({"tipo": "apartamento", "bairro": "Bairro X", "preco_pedido": None})
    assert " em Bairro X" in t
    assert " no Bairro X" not in t


def test_texto_acentua_chacara_e_expande_comercial():
    t, _ = rp._texto({"tipo": "chacara", "bairro": "", "preco_pedido": None})
    assert "sua chácara" in t and "chacara" not in t

    c, _ = rp._texto({"tipo": "comercial", "bairro": "Bairro A", "preco_pedido": None})
    assert "imóvel comercial" in c


def test_texto_omite_bairro_a_confirmar():
    """21 dos 39 imóveis entraram sem bairro. "no (a confirmar)" é pior que nada."""
    t, _ = rp._texto({"tipo": "casa", "bairro": "(a confirmar)", "preco_pedido": 300000})
    assert "confirmar" not in t
    assert "sua casa está cadastrada" in t


def test_texto_sem_preco_nao_inventa_valor():
    """"cadastrado por R$ None" é o tipo de coisa que queima a captação."""
    t, _ = rp._texto({"tipo": "casa", "bairro": "Bairro X", "preco_pedido": None})
    assert "None" not in t and "R$" not in t
    assert "está cadastrada com a gente." in t


def test_texto_descreve_registro_e_nao_convida():
    """Utility × marketing: descrever um registro passa, convidar a conversar não.

    A diferença custa ~9x por mensagem (R$ 0,3125 contra R$ 0,0340), então o
    texto é uma pergunta sobre um CADASTRO, nunca uma oferta.
    """
    t, _ = rp._texto({"tipo": "casa", "bairro": "Bairro X", "preco_pedido": 425000})
    assert "está cadastrada com a gente" in t
    for convite in ("aproveite", "oportunidade", "temos opções", "quer que eu",
                    "confira", "novidade"):
        assert convite not in t.lower()


def test_classificar_resposta_entende_o_essencial():
    assert rp.classificar_resposta("já vendi, obrigado") == "vendido"
    assert rp.classificar_resposta("Desisti de vender") == "desistiu"
    assert rp.classificar_resposta("aluguei") == "desistiu"
    assert rp.classificar_resposta("sim") == "disponivel"
    assert rp.classificar_resposta("continua sim") == "disponivel"


def test_classificar_resposta_e_conservadora():
    """Na dúvida devolve None e um humano decide.

    "vendi o carro pra dar entrada" fala de carro, não do imóvel — mas contém
    "vendi". O custo de errar aqui é sumir com um imóvel da carteira, então
    frases longas com "sim" também caem fora.
    """
    assert rp.classificar_resposta("sim, mas quero mudar o preço pra 400") is None
    assert rp.classificar_resposta("me liga depois que eu te explico") is None
    assert rp.classificar_resposta("") is None
    assert rp.classificar_resposta(None) is None


def test_fila_exige_quarenta_dias_de_casa():
    """Imóvel cadastrado hoje não leva toque amanhã.

    Perguntar "ainda está à venda?" uma semana depois de cadastrar é o robô
    duvidando do próprio cadastro. Os 39 da auditoria entraram hoje: nenhum
    pode ser elegível agora.
    """
    from sqlalchemy import text as sql_text

    from app.db.session import SessionLocal

    with SessionLocal() as s:
        n = s.execute(
            sql_text(rp._SQL_FILA),
            {"t": 1, "evento": rp.EVENTO_TOQUE, "dias": rp.DIAS_ENTRE_TOQUES, "cap": 999},
        ).rowcount
    assert n == 0


def test_cap_e_intervalo_conservadores():
    """A linha é o telefone DA CASA — rajada ali a Meta lê como spam."""
    assert rp.DIAS_ENTRE_TOQUES == 40
    assert rp.CAP_POR_DIA <= 20
    assert (rp.HORA_INICIO, rp.HORA_FIM) == (9, 19)


# ── Os botões (pedido da Dona, 16/09) ────────────────────────────────────────


def test_botoes_sao_os_do_template_aprovado():
    """Os rótulos são EXATAMENTE os do `continuar_atendimento_zgsuyd`.

    O clique chega no webhook em `button.text`, então o casamento é por texto:
    mudar aqui sem recriar o template na Meta quebra o reconhecimento.
    """
    assert set(rp.BOTOES) == {"Prosseguir", "Encerrar"}
    assert rp.TEMPLATE == ("continuar_atendimento_zgsuyd", "pt_BR")


def test_encerrar_nao_afirma_venda():
    """"Encerrar" não diz SE vendeu. Chutar "vendido" afirma o que ninguém viu.

    "desistiu" é o conservador: tira da roda igual e é o estado que volta pro
    mercado. A diferença é perguntada no follow-up, dentro da janela de 24h que
    o próprio clique abriu.
    """
    assert rp.BOTOES["Encerrar"] == "desistiu"
    assert "vendido" not in rp.BOTOES.values()


def test_clique_de_botao_e_resposta_exata():
    """O rótulo chega no webhook em `button.text` — casamento é por texto."""
    for rotulo, esperado in rp.BOTOES.items():
        assert rp.classificar_resposta(rotulo) == esperado


def test_texto_livre_continua_valendo():
    """Nem todo mundo clica. Quem escreve também tem que ser entendido."""
    assert rp.classificar_resposta("ja vendi") == "vendido"
    assert rp.classificar_resposta("desisti") == "desistiu"


def test_texto_nao_pede_resposta_escrita():
    """Com botão na tela, pedir texto livre é dar duas instruções contraditórias."""
    t, _ = rp._texto({"tipo": "casa", "bairro": "Bairro X", "preco_pedido": 425000})
    for pedido in ("me avisa", "responde aqui", "me responde", "escreve", "manda um"):
        assert pedido not in t.lower()
    assert t.rstrip().endswith("?")


def test_clique_muda_a_situacao_do_imovel_certo(tmp_path):
    """Ponta a ponta: toque registrado → clique → imóvel muda e sai da roda.

    O caso que importa é o do dono com MAIS DE UM imóvel (a Dulcineia tem
    três): o clique casa com o toque mais recente, não com qualquer imóvel
    daquele telefone.
    """
    from sqlalchemy import text as sql_text

    from app.db.session import SessionLocal
    from app.services import imoveis_captados as svc

    tel = "+5500000000000"
    with SessionLocal() as s:
        # dois imóveis do MESMO dono
        a, _ = svc.criar(s, svc.ImovelIn(tipo="casa", bairro="Bairro X",
                                         dono_telefone=tel, preco_pedido=300000),
                         corretora_id=1, captado_por="teste")
        b, _ = svc.criar(s, svc.ImovelIn(tipo="apartamento", bairro="Centro",
                                         dono_telefone=tel, preco_pedido=200000),
                         corretora_id=1, captado_por="teste")
        s.flush()
        # a régua tocou só o SEGUNDO
        s.execute(
            sql_text("""INSERT INTO imovel_evento (imovel_id, tipo, por, nota, em)
                        VALUES (:i, :t, 'regua', 'toque', now())"""),
            {"i": b.id, "t": rp.EVENTO_TOQUE},
        )
        s.flush()

        out = rp.tratar_resposta_inbound(tel, "Encerrar", session=s)
        assert out is not None
        assert out["imovel_id"] == b.id, "casou com o imóvel errado"
        assert out["para"] == "desistiu"

        # o primeiro, que não foi tocado, segue intacto
        sit_a = s.execute(sql_text("SELECT situacao FROM imoveis_captados WHERE id=:i"),
                          {"i": a.id}).scalar()
        assert sit_a == "disponivel"
        s.rollback()


def test_clique_sem_toque_recente_nao_faz_nada():
    """Mudar situação por palpite é sumir com o imóvel da carteira."""
    from app.db.session import SessionLocal

    with SessionLocal() as s:
        assert rp.tratar_resposta_inbound("+5500000000000", "Encerrar", session=s) is None
