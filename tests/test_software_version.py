"""Versão do software: variável de ambiente, arquivo VERSION, commit Git e 'unknown'."""
import subprocess

import software_version as sv


def sem_git(*args, **kwargs):
    raise FileNotFoundError("git não instalado")


def test_variavel_de_ambiente_tem_prioridade(monkeypatch, tmp_path):
    (tmp_path / "VERSION").write_text("1.0.0\n", encoding="utf-8")
    monkeypatch.setenv(sv.ENV_VAR, " 1.0.0+abc1234 ")
    assert sv.software_version(str(tmp_path)) == "1.0.0+abc1234"


def test_arquivo_version_quando_nao_ha_variavel(monkeypatch, tmp_path):
    monkeypatch.delenv(sv.ENV_VAR, raising=False)
    monkeypatch.setattr(subprocess, "run", sem_git)
    (tmp_path / "VERSION").write_text("2.1.0\nlinhas seguintes são ignoradas\n", encoding="utf-8")
    assert sv.software_version(str(tmp_path)) == "2.1.0"


def test_commit_git_quando_nao_ha_version(monkeypatch, tmp_path):
    monkeypatch.delenv(sv.ENV_VAR, raising=False)
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="abc1234\n", stderr=""))
    assert sv.software_version(str(tmp_path)) == "git-abc1234"


def test_sem_variavel_version_nem_git_e_unknown(monkeypatch, tmp_path):
    monkeypatch.setenv(sv.ENV_VAR, "  ")
    monkeypatch.setattr(subprocess, "run", sem_git)
    (tmp_path / "VERSION").write_text("\n", encoding="utf-8")
    assert sv.software_version(str(tmp_path)) == "unknown"


def test_version_do_repositorio_existe():
    assert sv.software_version() != "unknown"
