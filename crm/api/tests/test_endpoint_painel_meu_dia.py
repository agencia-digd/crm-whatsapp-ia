"""GET /painel/meu-dia — a tela "👋 Meu dia" da corretora.

Cobre:
  - auth: 403 sem header / usuário desconhecido;
  - a ORDEM dos blocos (custo de demora) — é regra de produto, não estética;
  - bloco 🔴 "responderam e esperam você": entra quem respondeu a cadência nas
    últimas 48h; SAI assim que a corretora responde de volta;
  - bloco 🟠 "pergunta em aberto": última msg é do lead E pede resposta —
    "ok"/"obrigado"/"👍" ficam de fora e são contados em
    `encerramentos_filtrados`;
  - bloco 🟡 "aguardando seu primeiro contato": lead que chegou HOJE e ainda
    não falou; some assim que ele manda a primeira mensagem;
  - bloco 🟢 tarefas de hoje e 📞 ligações;
  - PRIVACIDADE: corretora vê só o dela; admin vê tudo;
  - "sem dado" ≠ zero: `tudo_limpo` exige `tudo_ok`.

Precisa de DB real (TEST_DATABASE_URL) — as queries usam DISTINCT ON,
regexp_replace e interval (Postgres).
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text as sql_text

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="precisa de TEST_DATABASE_URL (Postgres de teste)",
)

AGORA = datetime.now(timezone.utc)

# telefones do cenário (sufixo-8 improvável de colidir com outros testes)
TEL_CORRETOR_PERGUNTA = "+5500000000000"   # lead perguntou → 🟠 entra
TEL_CORRETOR_OK = "+5500000000000"         # lead mandou "ok" → 🟠 NÃO entra
TEL_CORRETOR_EMOJI = "+5500000000000"      # lead mandou "👍" → 🟠 NÃO entra
TEL_CORRETOR_RESPONDEU = "+5500000000000"  # respondeu a cadência → 🔴 entra
TEL_CORRETOR_ATENDIDA = "+5500000000000"   # respondeu MAS a Pessoa10 já retornou → sai
TEL_CORRETOR_ELA_FALOU = "+5500000000000"  # Pessoa10 falou por último → nenhum bloco
TEL_CORRETOR_PERGUNTA = "+5500000000000"   # da Pessoa11 → fora do escopo da Pessoa10
TEL_CORRETOR_NOVO_CALADO = "+5500000000000"    # chegou hoje, calado → 🟡 entra
TEL_CORRETOR_NOVO_JA_FALOU = "+5500000000000"  # chegou hoje e já falou → 🟡 não

SUF = uuid.uuid4().hex[:8]


def _hdr(user: str) -> dict[str, str]:
    return {"X-Painel-User": user}


def _ins_msg(s, *, tel: str, cid: int, from_corretora: bool, at: datetime,
             texto: str = "oi", tipo: str = "text") -> str:
    mid = f"WAMID-MD-{uuid.uuid4().hex[:12]}"
    s.execute(
        sql_text(
            "INSERT INTO mensagens_whatsapp "
            "(id, evolution_message_id, instance, corretora_id, remote_jid, "
            " contato_telefone, contato_nome, from_corretora, tipo_mensagem, "
            " conteudo_texto, timestamp_evolution) "
            "VALUES (gen_random_uuid(), :mid, :inst, :cid, :jid, :tel, :nome, "
            "        :fromc, :tipo, :texto, :at)"
        ),
        {
            "mid": mid, "inst": f"acme_c{cid}", "cid": cid,
            "jid": f"{tel}@s.whatsapp.net", "tel": tel,
            "nome": f"Contato {tel[-4:]}", "fromc": from_corretora,
            "tipo": tipo, "texto": texto, "at": at,
        },
    )
    return mid


@pytest.fixture(scope="module")
def seed():
    from app.db.session import SessionLocal

    mids: list[str] = []
    cad_ids: list[int] = []
    lead_ids: list[int] = []
    with SessionLocal() as s:
        # 🟠 pergunta em aberto
        mids.append(_ins_msg(s, tel=TEL_CORRETOR_PERGUNTA, cid=1, from_corretora=False,
                             at=AGORA - timedelta(hours=6),
                             texto="qual o valor do condomínio?"))
        # 🟠 "ok" e "👍" — a limpeza do OK
        mids.append(_ins_msg(s, tel=TEL_CORRETOR_OK, cid=1, from_corretora=False,
                             at=AGORA - timedelta(hours=5), texto="ok, obrigada!"))
        mids.append(_ins_msg(s, tel=TEL_CORRETOR_EMOJI, cid=1, from_corretora=False,
                             at=AGORA - timedelta(hours=4), texto="👍"))
        # Pessoa10 falou por último → não é pendência dela
        mids.append(_ins_msg(s, tel=TEL_CORRETOR_ELA_FALOU, cid=1, from_corretora=True,
                             at=AGORA - timedelta(hours=3), texto="te mando as fotos"))
        # Pessoa11 — escopo
        mids.append(_ins_msg(s, tel=TEL_CORRETOR_PERGUNTA, cid=2, from_corretora=False,
                             at=AGORA - timedelta(hours=2), texto="tem 3 dormitórios?"))
        # 🔴 respondeu a cadência (última msg é dele)
        mids.append(_ins_msg(s, tel=TEL_CORRETOR_RESPONDEU, cid=1, from_corretora=False,
                             at=AGORA - timedelta(hours=8), texto="oi, ainda tá disponível?"))
        # 🔴 respondeu MAS a Pessoa10 já retornou → sai do bloco
        mids.append(_ins_msg(s, tel=TEL_CORRETOR_ATENDIDA, cid=1, from_corretora=False,
                             at=AGORA - timedelta(hours=9), texto="oi, quero ver"))
        mids.append(_ins_msg(s, tel=TEL_CORRETOR_ATENDIDA, cid=1, from_corretora=True,
                             at=AGORA - timedelta(hours=1), texto="claro! te ligo já"))
        # 🟡 lead que chegou hoje e JÁ falou → não é "primeiro contato"
        mids.append(_ins_msg(s, tel=TEL_CORRETOR_NOVO_JA_FALOU, cid=1, from_corretora=False,
                             at=AGORA - timedelta(minutes=30), texto="oi tudo bem?"))

        # cadência respondida pros dois telefones do bloco 🔴
        for tel in (TEL_CORRETOR_RESPONDEU, TEL_CORRETOR_ATENDIDA):
            cid_row = s.execute(
                sql_text(
                    "INSERT INTO cadencia_estado "
                    "(tenant_id, lead_id, telefone, corretora_id, toque_atual, status) "
                    "VALUES (1, :lid, :tel, 1, 2, 'respondida') RETURNING id"
                ),
                {"lid": f"md-{SUF}-{tel[-4:]}", "tel": tel},
            ).scalar()
            cad_ids.append(int(cid_row))
            s.execute(
                sql_text(
                    "INSERT INTO cadencia_eventos (tenant_id, cadencia_id, toque, tipo, at) "
                    "VALUES (1, :cid, 2, 'resposta', :at)"
                ),
                {"cid": cid_row, "at": AGORA - timedelta(hours=9)},
            )

        # 🟡 leads que chegaram HOJE
        for tel, nome in ((TEL_CORRETOR_NOVO_CALADO, "Novo Calado"),
                          (TEL_CORRETOR_NOVO_JA_FALOU, "Novo Falante")):
            lid = s.execute(
                sql_text(
                    "INSERT INTO leads (tenant_id, external_ref, nome, telefone, "
                    "                   owner_id, origem, created_at) "
                    "VALUES (1, :ref, :nome, :tel, 1, 'meta', :at) RETURNING id"
                ),
                {"ref": f"md-{SUF}-{tel[-4:]}", "nome": nome,
                 "tel": f"+{tel}", "at": AGORA},
            ).scalar()
            lead_ids.append(int(lid))
        s.commit()

    yield

    with SessionLocal() as s:
        s.execute(sql_text("DELETE FROM mensagens_whatsapp "
                           "WHERE evolution_message_id = ANY(:ids)"), {"ids": mids})
        s.execute(sql_text("DELETE FROM cadencia_eventos WHERE cadencia_id = ANY(:ids)"),
                  {"ids": cad_ids})
        s.execute(sql_text("DELETE FROM cadencia_estado WHERE id = ANY(:ids)"),
                  {"ids": cad_ids})
        s.execute(sql_text("DELETE FROM leads WHERE id = ANY(:ids)"), {"ids": lead_ids})
        s.commit()


@pytest.fixture
def client(seed):
    from app.main import app
    return TestClient(app)


def _blocos(body: dict) -> dict[str, dict]:
    return {b["chave"]: b for b in body["blocos"]}


def _tels(bloco: dict) -> list[str]:
    return [i["telefone"] for i in bloco["itens"] if i.get("telefone")]


# ── auth ─────────────────────────────────────────────────────────────────────

def test_sem_header_403(client):
    assert client.get("/painel/meu-dia").status_code == 403


def test_usuario_desconhecido_403(client):
    assert client.get("/painel/meu-dia", headers=_hdr("hacker")).status_code == 403


# ── a ordem é regra de produto ───────────────────────────────────────────────

def test_ordem_dos_blocos_e_por_custo_de_demora(client):
    """A ordem é a régua da tela e não pode mudar por acidente.

    `retomar` entrou em 17/08 ANTES de `ligacoes`: ali é gente que falou com a
    corretora esta semana e esfriou; a ligação é base fria de remarketing. Lead
    morno de 4 dias volta mais fácil que lead de 6 meses.
    """
    r = client.get("/painel/meu-dia", headers=_hdr("corretor"))
    assert r.status_code == 200, r.text
    assert [b["chave"] for b in r.json()["blocos"]] == [
        "responderam", "perguntas", "primeiro_contato", "tarefas",
        "retomar", "ligacoes",
    ]


def test_primeiro_contato_nao_se_chama_lead_calado(client):
    # A Dona foi explícita: não é problema do lead, é oportunidade nova.
    # O que importa não é a frase exata (ela encurtou em 05/08 pra caber na
    # leitura de 3 segundos) — é o ENQUADRAMENTO: fila de oportunidade, não
    # lista de gente que sumiu.
    b = _blocos(client.get("/painel/meu-dia", headers=_hdr("corretor")).json())
    titulo = b["primeiro_contato"]["titulo"].lower()
    assert "primeiro contato" in titulo
    assert "calado" not in titulo
    assert "sem resposta" not in titulo


# ── 🔴 responderam e esperam você ────────────────────────────────────────────

def test_responderam_entra_e_sai_quando_a_corretora_retorna(client):
    b = _blocos(client.get("/painel/meu-dia", headers=_hdr("corretor")).json())
    tels = _tels(b["responderam"])
    assert f"+{TEL_CORRETOR_RESPONDEU}" in tels
    # a Pessoa10 já respondeu esse → deixa de ser pendência
    assert f"+{TEL_CORRETOR_ATENDIDA}" not in tels


def test_quem_esta_no_bloco_vermelho_nao_repete_no_laranja(client):
    b = _blocos(client.get("/painel/meu-dia", headers=_hdr("corretor")).json())
    assert f"+{TEL_CORRETOR_RESPONDEU}" in _tels(b["responderam"])
    assert f"+{TEL_CORRETOR_RESPONDEU}" not in _tels(b["perguntas"])


# ── 🟠 pergunta em aberto (a limpeza do "OK") ────────────────────────────────

def test_pergunta_entra_e_ok_emoji_ficam_de_fora(client):
    b = _blocos(client.get("/painel/meu-dia", headers=_hdr("corretor")).json())["perguntas"]
    tels = _tels(b)
    assert f"+{TEL_CORRETOR_PERGUNTA}" in tels
    assert f"+{TEL_CORRETOR_OK}" not in tels, '"ok, obrigada!" virou pendência'
    assert f"+{TEL_CORRETOR_EMOJI}" not in tels, '"👍" virou pendência'
    # a corretora falou por último → não é pendência dela
    assert f"+{TEL_CORRETOR_ELA_FALOU}" not in tels
    # e a tela consegue explicar quantos sumiram
    assert b["encerramentos_filtrados"] >= 2


def test_motivo_da_classificacao_vem_no_item(client):
    b = _blocos(client.get("/painel/meu-dia", headers=_hdr("corretor")).json())["perguntas"]
    item = next(i for i in b["itens"] if i["telefone"] == f"+{TEL_CORRETOR_PERGUNTA}")
    assert item["motivo"] == "pergunta"
    assert item["duvida"] is False
    assert item["desde"]["rotulo"]


# ── 🟡 aguardando seu primeiro contato ───────────────────────────────────────

def test_primeiro_contato_so_quem_ainda_nao_falou(client):
    b = _blocos(client.get("/painel/meu-dia", headers=_hdr("corretor")).json())["primeiro_contato"]
    nomes = [i["nome"] for i in b["itens"]]
    assert "Novo Calado" in nomes
    assert "Novo Falante" not in nomes, "lead que já falou não é primeiro contato"


# ── 🟢 tarefas e 📞 ligações ─────────────────────────────────────────────────

def test_tarefas_e_ligacoes_tem_atalho_e_total(client):
    b = _blocos(client.get("/painel/meu-dia", headers=_hdr("corretor")).json())
    assert b["tarefas"]["status"] == "ok"
    assert isinstance(b["tarefas"]["total"], int)
    assert b["tarefas"]["href"] == "/tarefas"
    assert b["ligacoes"]["href"] == "/tarefas?aba=ligar"
    assert b["ligacoes"]["status"] == "ok"


def test_admin_sem_corretora_nao_ganha_meta_de_ligacao_inventada(client):
    # meta é de quem liga; inventar uma pro admin poluiria o número dela
    b = _blocos(client.get("/painel/meu-dia", headers=_hdr("dona")).json())
    assert b["ligacoes"]["sem_meta"] is True
    assert b["ligacoes"]["meta"] is None


def _alvo(client, cid: int) -> str:
    """Valor de filtro pra `cid`, lido do PRÓPRIO payload.

    É o que o front manda (`c.nome ?? c.label`, da lista `corretoras`). Não dá
    pra cravar "Pessoa1" aqui: os rótulos vêm da tabela `corretoras` via
    `_MapaCorretoras` (com cache e fallback), então o nome exibido depende do
    ambiente — e o teste tem que provar o CONTRATO, não o texto.
    """
    lista = client.get("/painel/meu-dia", headers=_hdr("dona")).json()["corretoras"]
    c = next(x for x in lista if x["id"] == cid)
    return c["nome"] or c["label"]


def test_todo_rotulo_oferecido_e_filtravel(client):
    """Contrato das pills: tudo que a API oferece, a API aceita de volta."""
    lista = client.get("/painel/meu-dia", headers=_hdr("dona")).json()["corretoras"]
    assert lista
    for c in lista:
        for valor in (c["nome"] or c["label"], c["label"], str(c["id"])):
            body = client.get(f"/painel/meu-dia?corretora={valor}",
                              headers=_hdr("dona")).json()
            assert body["filtro_corretora_id"] == c["id"], valor


def test_admin_com_filtro_ve_a_meta_DELA(client):
    """"A Pessoa11 já fez quantas das 25?" — é o que a Dona vai querer ver."""
    b = _blocos(client.get(f"/painel/meu-dia?corretora={_alvo(client, 2)}",
                           headers=_hdr("dona")).json())
    meta = b["ligacoes"]["meta"]
    assert b["ligacoes"]["sem_meta"] is False
    assert meta is not None
    assert meta["corretora_id"] == 2
    assert meta["meta_diaria"] >= 1
    assert b["ligacoes"]["total"] == meta["restante_hoje"]


# ── filtro por pessoa (admin) ────────────────────────────────────────────────

def test_admin_filtra_por_corretora(client):
    body = client.get(f"/painel/meu-dia?corretora={_alvo(client, 2)}",
                      headers=_hdr("dona")).json()
    tels = _tels(_blocos(body)["perguntas"])
    assert f"+{TEL_CORRETOR_PERGUNTA}" in tels
    assert f"+{TEL_CORRETOR_PERGUNTA}" not in tels
    assert body["filtro_corretora_id"] == 2
    assert body["filtro"] == body["dono"]  # título "Meu dia — <dono>"


def test_filtro_aceita_rotulo_curto_e_id(client):
    for valor in ("Pessoa11", "2", "corretor"):
        body = client.get(f"/painel/meu-dia?corretora={valor}", headers=_hdr("dona")).json()
        assert body["filtro_corretora_id"] == 2, valor


def test_admin_sem_filtro_continua_vendo_a_equipe(client):
    body = client.get("/painel/meu-dia", headers=_hdr("dona")).json()
    tels = _tels(_blocos(body)["perguntas"])
    assert f"+{TEL_CORRETOR_PERGUNTA}" in tels
    assert f"+{TEL_CORRETOR_PERGUNTA}" in tels
    assert body["filtro"] is None
    assert body["dono"] == "equipe"


def test_admin_com_filtro_ve_o_dia_igual_a_dona_ve(client):
    """Filtro = a Meu dia da Pessoa11, exatamente como ela veria."""
    admin = client.get(f"/painel/meu-dia?corretora={_alvo(client, 2)}",
                       headers=_hdr("dona")).json()
    dela = client.get("/painel/meu-dia", headers=_hdr("corretor")).json()
    for b_admin, b_dela in zip(admin["blocos"], dela["blocos"]):
        assert b_admin["chave"] == b_dela["chave"]
        assert b_admin["total"] == b_dela["total"], b_admin["chave"]


def test_pills_so_para_admin(client):
    admin = client.get("/painel/meu-dia", headers=_hdr("dona")).json()
    assert admin["pode_filtrar"] is True
    assert [c["id"] for c in admin["corretoras"]] == [1, 2, 3]

    corretora = client.get("/painel/meu-dia", headers=_hdr("corretor")).json()
    assert corretora["pode_filtrar"] is False
    assert corretora["corretoras"] == []


def test_corretora_nao_escapa_do_escopo_com_o_parametro(client):
    """REGRA DURA: o ?corretora= de uma corretora é ignorado, não obedecido."""
    body = client.get(f"/painel/meu-dia?corretora={_alvo(client, 2)}",
                      headers=_hdr("corretor")).json()
    b = _blocos(body)
    todos = _tels(b["responderam"]) + _tels(b["perguntas"]) + _tels(b["primeiro_contato"])
    assert f"+{TEL_CORRETOR_PERGUNTA}" not in todos, "a Pessoa10 viu conversa da Pessoa11"
    assert f"+{TEL_CORRETOR_PERGUNTA}" in _tels(b["perguntas"])
    assert body["filtro"] is None
    assert body["dono"] == "Pessoa10"
    # e a meta de ligação continua sendo a DELA
    assert b["ligacoes"]["meta"]["corretora_id"] == 1


def test_filtro_desconhecido_nao_vira_lista_vazia_silenciosa(client):
    body = client.get("/painel/meu-dia?corretora=fulana", headers=_hdr("dona")).json()
    assert body["filtro"] is None  # sem filtro, não "zero resultados"
    assert f"+{TEL_CORRETOR_PERGUNTA}" in _tels(_blocos(body)["perguntas"])


# ── confete: de quem é a comemoração ─────────────────────────────────────────

def test_admin_nunca_comemora(client):
    """Quem limpa o dia é a corretora; admin está supervisionando."""
    assert client.get("/painel/meu-dia", headers=_hdr("dona")).json()["pode_comemorar"] is False
    filtrado = client.get(f"/painel/meu-dia?corretora={_alvo(client, 2)}",
                          headers=_hdr("dona")).json()
    assert filtrado["pode_comemorar"] is False
    assert filtrado["pode_agir"] is False


def test_corretora_pode_comemorar(client):
    body = client.get("/painel/meu-dia", headers=_hdr("corretor")).json()
    assert body["pode_comemorar"] is True
    assert body["pode_agir"] is True


# ── privacidade ──────────────────────────────────────────────────────────────

def test_corretora_ve_so_o_dela(client):
    body = client.get("/painel/meu-dia", headers=_hdr("corretor")).json()
    b = _blocos(body)
    todos = _tels(b["responderam"]) + _tels(b["perguntas"]) + _tels(b["primeiro_contato"])
    assert f"+{TEL_CORRETOR_PERGUNTA}" not in todos
    assert body["viewer"]["corretora_id"] == 1


def test_corretor_nao_ve_lead_da_corretor(client):
    b = _blocos(client.get("/painel/meu-dia", headers=_hdr("corretor")).json())
    tels = _tels(b["perguntas"])
    assert f"+{TEL_CORRETOR_PERGUNTA}" in tels
    assert f"+{TEL_CORRETOR_PERGUNTA}" not in tels


def test_admin_ve_as_duas(client):
    body = client.get("/painel/meu-dia", headers=_hdr("dona")).json()
    tels = _tels(_blocos(body)["perguntas"])
    assert f"+{TEL_CORRETOR_PERGUNTA}" in tels
    assert f"+{TEL_CORRETOR_PERGUNTA}" in tels
    assert body["viewer"]["corretora_id"] is None


# ── "sem dado" ≠ zero ────────────────────────────────────────────────────────

def test_tudo_limpo_exige_tudo_ok(client):
    body = client.get("/painel/meu-dia", headers=_hdr("corretor")).json()
    assert body["tudo_ok"] is True
    # com o cenário semeado a Pessoa10 TEM pendência → nada de confete
    assert body["tudo_limpo"] is False


def test_bloco_que_falha_nao_vira_zero(monkeypatch, client):
    """Consulta quebrada → status 'erro' e total None, nunca 0/tudo limpo."""
    import app.api.painel as painel

    def _explode(*a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(painel, "_tarefas_ligacao_cadencia", _explode)
    body = client.get("/painel/meu-dia", headers=_hdr("corretor")).json()
    b = _blocos(body)["tarefas"]
    assert b["status"] == "erro"
    assert b["total"] is None
    assert body["tudo_ok"] is False
    assert body["tudo_limpo"] is False
