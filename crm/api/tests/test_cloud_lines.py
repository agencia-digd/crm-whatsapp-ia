"""Registry de linhas Cloud (app/services/cloud_lines.py).

Sem rede, sem banco. Leitura de env-file é mockada via tmp_path pra não depender
de /etc/agencia (que pode não existir no worktree/CI).
"""
from __future__ import annotations

from app.services import cloud_lines as cl


def test_line_by_phone_id_corretor() -> None:
    line = cl.line_by_phone_id("000000000000000")
    assert line is not None
    assert line.slug == "corretor"
    assert line.papel == "corretora"
    assert line.token_var == "META_REVIEW_TOKEN"
    assert line.dry_run_var is None


def test_line_by_phone_id_assistente() -> None:
    line = cl.line_by_phone_id("000000000000000")
    assert line is not None
    assert line.slug == "assistente"
    assert line.papel == "responsivo"
    assert line.token_var == "ASSISTENTE_WA_TOKEN"
    assert line.dry_run_var == "CLOUD_DRY_RUN"


def test_line_by_slug() -> None:
    assert cl.line_by_slug("corretor").phone_id == "000000000000000"
    assert cl.line_by_slug("assistente").phone_id == "000000000000000"


def test_desconhecido_retorna_none() -> None:
    assert cl.line_by_phone_id("000000") is None
    assert cl.line_by_phone_id(None) is None
    assert cl.line_by_slug("naoexiste") is None
    assert cl.line_by_slug(None) is None


def test_token_for_le_do_env_file(tmp_path, monkeypatch) -> None:
    env = tmp_path / "fake.env"
    env.write_text('ASSISTENTE_WA_TOKEN="tok-123"\n# comentario\nOUTRA=x\n')
    line = cl.line_by_slug("assistente")
    monkeypatch.setattr(line, "env_file", str(env))
    assert cl.token_for(line) == "tok-123"
    # aceita slug e phone_id também
    monkeypatch.setattr(cl.line_by_slug("assistente"), "env_file", str(env))


def test_token_for_arquivo_ausente_tolerante() -> None:
    # env_file default aponta pra /etc/agencia/... que pode não existir → None, sem raise
    line = cl.CloudLine(
        phone_id="x", slug="x", papel="x",
        env_file="/caminho/que/nao/existe.env", token_var="TOK",
    )
    assert cl.token_for(line) is None


def test_token_for_var_ausente_retorna_none(tmp_path) -> None:
    env = tmp_path / "fake.env"
    env.write_text("OUTRA=x\n")
    line = cl.CloudLine(
        phone_id="x", slug="x", papel="x", env_file=str(env), token_var="TOK",
    )
    assert cl.token_for(line) is None


def test_dry_run_for_true(tmp_path) -> None:
    env = tmp_path / "d.env"
    env.write_text("CLOUD_DRY_RUN=true\n")
    line = cl.CloudLine(
        phone_id="x", slug="x", papel="x", env_file=str(env),
        token_var="TOK", dry_run_var="CLOUD_DRY_RUN",
    )
    assert cl.dry_run_for(line) is True


def test_dry_run_for_false_variacoes(tmp_path) -> None:
    env = tmp_path / "d.env"
    env.write_text("CLOUD_DRY_RUN=false\n")
    line = cl.CloudLine(
        phone_id="x", slug="x", papel="x", env_file=str(env),
        token_var="TOK", dry_run_var="CLOUD_DRY_RUN",
    )
    assert cl.dry_run_for(line) is False


def test_dry_run_for_sem_var_e_false() -> None:
    # linha sem dry_run_var (ex: Pessoa12) → sempre False
    corretor = cl.line_by_slug("corretor")
    assert cl.dry_run_for(corretor) is False


def test_dry_run_for_arquivo_ausente_e_false() -> None:
    line = cl.CloudLine(
        phone_id="x", slug="x", papel="x",
        env_file="/nao/existe.env", token_var="TOK", dry_run_var="CLOUD_DRY_RUN",
    )
    assert cl.dry_run_for(line) is False


def test_as_public_dict_sem_segredos() -> None:
    pub = cl.as_public_dict()
    assert "000000000000000" in pub
    assert "000000000000000" in pub
    # só metadados, nenhum valor de token
    for meta in pub.values():
        assert set(meta) == {"slug", "papel", "env_file", "token_var", "dry_run_var"}
