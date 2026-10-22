"""Camada 1 do banco de imóveis — captação (endpoints /painel/imoveis).

O que estes testes travam, em ordem de importância:

  1. NADA faz o cadastro falhar. Só 4 campos obrigatórios; duplicata AVISA e o
     imóvel é salvo do mesmo jeito (é a regra que decide se a corretora usa a
     ferramenta ou volta pro bloco de notas).
  2. Escopo: corretora não vê nem edita imóvel de outra; admin vê tudo.
  3. Foto só sai por rota autenticada, e nunca é sobrescrita — a versão com a
     logo é LINHA NOVA com `tratada_de_id`.
  4. Logo ausente → 503 com mensagem clara, nunca logo improvisada.
  5. Transferência de captação exige admin e vira evento assinado.
"""
from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def _media_tmp(tmp_path, monkeypatch):
    """Fotos vão pro tmp_path — nunca no /var/lib real."""
    monkeypatch.setattr(settings, "imoveis_media_dir", str(tmp_path / "imoveis"))


def _hdr(user: str) -> dict[str, str]:
    return {"X-Painel-User": user}


def _jpeg(cor=(200, 120, 40), tamanho=(2400, 1800)) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", tamanho, cor).save(buf, format="JPEG")
    return buf.getvalue()


MINIMO = {
    "tipo": "apartamento",
    "bairro": "Bairro X",
    "dono_telefone": "+5500000000000",
    "preco_pedido": 380000,
}


def _criar(client, user="corretor", **extra):
    corpo = {**MINIMO, **extra}
    r = client.post("/painel/imoveis", json=corpo, headers=_hdr(user))
    assert r.status_code == 201, r.text
    return r.json()


# ── 1 · o cadastro não falha ─────────────────────────────────────────────────


def test_sem_header_bloqueia(client):
    assert client.get("/painel/imoveis").status_code == 403


def test_cria_com_os_4_campos_obrigatorios(client):
    out = _criar(client)
    assert out["ok"] is True
    im = out["imovel"]
    assert im["bairro"] == "Bairro X"
    assert im["corretora_id"] == 1  # corretor
    assert im["situacao"] == "disponivel"
    # completude parcial e a lista de faltas pronta pra tela ("faltam N campos")
    assert 0 < im["completude"] < 100
    assert "fotos" in im["faltando"] and "nome do dono" in im["faltando"]


def test_duplicata_avisa_e_nao_bloqueia(client):
    _criar(client, area_m2=72)
    out = _criar(client, area_m2=72)  # mesmo bairro, mesmo tipo, mesmo telefone
    assert out["ok"] is True and out["id"]
    assert out["avisos"], "parecido deveria virar aviso"
    aviso = out["avisos"][0]
    assert aviso["mesmo_telefone"] is True
    assert "captou" in aviso["aviso"]


def test_tipo_desconhecido_vira_outro_em_vez_de_422(client):
    out = _criar(client, tipo="loft-cobertura-duplex")
    assert out["imovel"]["tipo"] == "outro"


def test_telefone_sem_digito_e_o_unico_422(client):
    r = client.post("/painel/imoveis", json={**MINIMO, "dono_telefone": "não sei"},
                    headers=_hdr("corretor"))
    assert r.status_code == 422


# ── 2 · escopo ────────────────────────────────────────────────────────────────


def test_corretora_nao_ve_imovel_de_outra(client):
    out = _criar(client, user="corretor")
    assert client.get(f"/painel/imoveis/{out['id']}", headers=_hdr("corretor")).status_code == 403
    assert client.get(f"/painel/imoveis/{out['id']}", headers=_hdr("corretor")).status_code == 200
    # admin vê
    assert client.get(f"/painel/imoveis/{out['id']}", headers=_hdr("dona")).status_code == 200


def test_lista_da_corretora_so_traz_as_dela(client):
    _criar(client, user="corretor")
    _criar(client, user="corretor", bairro="Bairro A")
    da_corretor = client.get("/painel/imoveis", headers=_hdr("corretor")).json()
    assert {i["corretora_id"] for i in da_corretor["imoveis"]} == {1}
    do_admin = client.get("/painel/imoveis", headers=_hdr("dona")).json()
    assert {1, 2} <= {i["corretora_id"] for i in do_admin["imoveis"]}
    assert do_admin["is_admin"] is True


def test_admin_precisa_escolher_a_dona(client):
    r = client.post("/painel/imoveis", json=MINIMO, headers=_hdr("dona"))
    assert r.status_code == 400 and "corretora" in r.json()["detail"]
    r = client.post("/painel/imoveis", json={**MINIMO, "corretora_id": 3}, headers=_hdr("dona"))
    assert r.status_code == 201 and r.json()["imovel"]["corretora_id"] == 3


def test_corretora_nao_captura_no_nome_de_outra(client):
    """O `corretora_id` do body é ignorado pra corretora comum."""
    out = _criar(client, user="corretor", corretora_id=2)
    assert out["imovel"]["corretora_id"] == 1


# ── 3 · edição e eventos ──────────────────────────────────────────────────────


def test_edicao_de_preco_vira_evento_proprio(client):
    out = _criar(client)
    r = client.patch(f"/painel/imoveis/{out['id']}", json={"preco_pedido": 350000},
                     headers=_hdr("corretor"))
    assert r.status_code == 200 and "preco_pedido" in r.json()["alterado"]
    eventos = client.get(f"/painel/imoveis/{out['id']}", headers=_hdr("corretor")).json()["eventos"]
    tipos = {e["tipo"] for e in eventos}
    assert {"criado", "preco_mudou"} <= tipos


def test_transferencia_de_captacao_exige_admin(client):
    out = _criar(client, user="corretor")
    r = client.patch(f"/painel/imoveis/{out['id']}", json={"corretora_id": 2},
                     headers=_hdr("corretor"))
    assert r.status_code == 403
    r = client.patch(f"/painel/imoveis/{out['id']}",
                     json={"corretora_id": 2, "nota": "acordo entre elas"},
                     headers=_hdr("dona"))
    assert r.status_code == 200
    eventos = client.get(f"/painel/imoveis/{out['id']}", headers=_hdr("dona")).json()["eventos"]
    ev = next(e for e in eventos if e["tipo"] == "captacao_transferida")
    assert ev["de"]["corretora_id"] == 1 and ev["para"]["corretora_id"] == 2
    assert ev["por"] == "dona" and ev["nota"] == "acordo entre elas"


# ── 4 · fotos ─────────────────────────────────────────────────────────────────


def _subir(client, imovel_id, user="corretor", conteudo=None):
    return client.post(
        f"/painel/imoveis/{imovel_id}/fotos",
        files={"arquivo": ("casa.jpg", conteudo or _jpeg(), "image/jpeg")},
        data={"origem": "dono", "autorizacao_dono": "true"},
        headers=_hdr(user),
    )


def test_foto_sobe_redimensionada_e_so_sai_autenticada(client):
    out = _criar(client)
    r = _subir(client, out["id"])
    assert r.status_code == 201, r.text
    foto = r.json()["fotos"][0]
    assert max(foto["largura"], foto["altura"]) == settings.imoveis_foto_lado_max
    assert foto["url"] == f"/painel/imoveis/fotos/{foto['id']}"
    # rota autenticada devolve o binário…
    ok = client.get(foto["url"], headers=_hdr("corretor"))
    assert ok.status_code == 200 and ok.headers["content-type"].startswith("image/")
    # …e a de outra corretora, não
    assert client.get(foto["url"], headers=_hdr("corretor")).status_code == 403
    assert client.get(foto["url"]).status_code == 403


def test_foto_igual_duas_vezes_nao_duplica(client):
    out = _criar(client)
    conteudo = _jpeg()
    _subir(client, out["id"], conteudo=conteudo)
    r = _subir(client, out["id"], conteudo=conteudo)
    assert r.status_code == 201
    assert len(r.json()["fotos"]) == 1


def test_origem_portal_e_recusada(client):
    """🔴 Foto de anúncio de terceiro não entra — §3 do plano."""
    out = _criar(client)
    r = client.post(
        f"/painel/imoveis/{out['id']}/fotos",
        files={"arquivo": ("x.jpg", _jpeg(), "image/jpeg")},
        data={"origem": "portal"},
        headers=_hdr("corretor"),
    )
    assert r.status_code == 422 and "origem_invalida" in r.json()["detail"]


def test_logo_gera_arquivo_novo_e_preserva_a_original(client):
    out = _criar(client)
    original = _subir(client, out["id"]).json()["fotos"][0]
    r = client.post(f"/painel/imoveis/fotos/{original['id']}/logo", headers=_hdr("corretor"))
    assert r.status_code == 200, r.text
    nova = r.json()["foto"]
    assert nova["id"] != original["id"]
    assert nova["tratada"] is True and nova["tratada_de_id"] == original["id"]
    assert nova["tratamento"] == "logo_acme"
    # a original continua servível e com os mesmos bytes
    assert client.get(original["url"], headers=_hdr("corretor")).status_code == 200
    d = client.get(f"/painel/imoveis/{out['id']}", headers=_hdr("corretor")).json()
    assert {f["id"] for f in d["fotos"]} == {original["id"], nova["id"]}


def test_logo_ausente_da_erro_claro_e_nao_improvisa(client, monkeypatch):
    monkeypatch.setattr(settings, "acme_logo_path", "app/assets/nao-existe.png")
    out = _criar(client)
    foto = _subir(client, out["id"]).json()["fotos"][0]
    r = client.post(f"/painel/imoveis/fotos/{foto['id']}/logo", headers=_hdr("corretor"))
    assert r.status_code == 503
    assert "logo_indisponivel" in r.json()["detail"]
    # e nenhuma foto tratada foi criada
    d = client.get(f"/painel/imoveis/{out['id']}", headers=_hdr("corretor")).json()
    assert all(f["tratada"] is False for f in d["fotos"])


def test_arquivar_foto_nao_apaga_linha(client):
    out = _criar(client)
    foto = _subir(client, out["id"]).json()["fotos"][0]
    assert client.post(f"/painel/imoveis/fotos/{foto['id']}/arquivar",
                       headers=_hdr("corretor")).status_code == 200
    d = client.get(f"/painel/imoveis/{out['id']}", headers=_hdr("corretor")).json()
    assert d["fotos"] == []
    # a linha continua no banco (arquivada), e o binário ainda é servível
    assert client.get(foto["url"], headers=_hdr("corretor")).status_code == 200


def _tres_fotos(client, user="corretor"):
    out = _criar(client, user=user)
    for cor in [(10, 10, 10), (120, 120, 120), (240, 240, 240)]:
        r = _subir(client, out["id"], user=user, conteudo=_jpeg(cor=cor, tamanho=(300, 200)))
    return out["id"], [f["id"] for f in r.json()["fotos"]]


def _capa_na_lista(client, imovel_id, user="corretor"):
    itens = client.get("/painel/imoveis", headers=_hdr(user)).json()["imoveis"]
    return next(i for i in itens if i["id"] == imovel_id)["capa_url"]


def test_reordenar_muda_a_capa_no_detalhe_e_na_lista(client):
    imovel_id, ids = _tres_fotos(client)
    assert _capa_na_lista(client, imovel_id).endswith(f"/{ids[0]}")
    nova = [ids[2], ids[0], ids[1]]
    r = client.put(f"/painel/imoveis/{imovel_id}/fotos/ordem",
                   json={"ordem": nova}, headers=_hdr("corretor"))
    assert r.status_code == 200, r.text
    assert [f["id"] for f in r.json()["fotos"]] == nova
    assert r.json()["imovel"]["capa_url"].endswith(f"/{ids[2]}")
    assert _capa_na_lista(client, imovel_id).endswith(f"/{ids[2]}")


def test_reordenar_com_lista_desatualizada_recusa_sem_mexer(client):
    imovel_id, ids = _tres_fotos(client)
    for ruim in ([ids[1], ids[0]], ids + [999999], [ids[0], ids[0], ids[1]]):
        r = client.put(f"/painel/imoveis/{imovel_id}/fotos/ordem",
                       json={"ordem": ruim}, headers=_hdr("corretor"))
        assert r.status_code == 409
    d = client.get(f"/painel/imoveis/{imovel_id}", headers=_hdr("corretor")).json()
    assert [f["id"] for f in d["fotos"]] == ids


def test_reordenar_imovel_de_outra_corretora_e_403(client):
    imovel_id, ids = _tres_fotos(client)
    r = client.put(f"/painel/imoveis/{imovel_id}/fotos/ordem",
                   json={"ordem": ids[::-1]}, headers=_hdr("corretor"))
    assert r.status_code == 403


def test_capa_e_a_primeira_mesmo_com_logo_em_outra_foto(client):
    """Antes, qualquer foto com logo virava capa e passava por cima da ordem."""
    imovel_id, ids = _tres_fotos(client)
    r = client.post(f"/painel/imoveis/fotos/{ids[2]}/logo", headers=_hdr("corretor"))
    if r.status_code == 503:
        pytest.skip("logo da Acme indisponível neste ambiente")
    assert _capa_na_lista(client, imovel_id).endswith(f"/{ids[0]}")
    # a 1ª passando a ser a que tem logo → a capa é a versão com logo dela
    client.put(f"/painel/imoveis/{imovel_id}/fotos/ordem",
               json={"ordem": [ids[2], ids[0], ids[1]]}, headers=_hdr("corretor"))
    tratada = r.json()["foto"]["id"]
    assert _capa_na_lista(client, imovel_id).endswith(f"/{tratada}")


def test_excluir_a_capa_passa_a_capa_pra_proxima(client):
    imovel_id, ids = _tres_fotos(client)
    client.post(f"/painel/imoveis/fotos/{ids[0]}/arquivar", headers=_hdr("corretor"))
    assert _capa_na_lista(client, imovel_id).endswith(f"/{ids[1]}")


# ── 5 · leads interessados (item 4, versão honesta) ──────────────────────────


def test_leads_interessados_casa_por_bairro_no_raiox(client):
    from app.db.models import LeadHandoffNote
    from app.db.session import SessionLocal

    with SessionLocal() as s:
        s.add(
            LeadHandoffNote(
                telefone="+5500000000000",
                lead_nome="Cliente Teste",
                corretora_id=3,
                contexto="Busca: apartamento 2 quartos no Bairro X. "
                         "Faixa de valor: até 400 mil.",
                origem="pessoa",
                status="ativo",
            )
        )
        s.commit()

    out = _criar(client)
    r = client.get(f"/painel/imoveis/{out['id']}/leads-interessados", headers=_hdr("corretor"))
    assert r.status_code == 200
    corpo = r.json()
    assert corpo["total"] >= 1
    assert any(x["telefone"] == "+5500000000000" for x in corpo["leads"])
    # a explicação diz o que NÃO foi cruzado — sem número que parece medido
    assert "faixa de valor" in corpo["explicacao"].lower()
