"""Versão do software, registrada nas sessões do Coletor, no bundle WKNN e nos relatórios.

Ordem: variável de ambiente TCC_SOFTWARE_VERSION; arquivo VERSION na raiz de codigo/; commit
Git (só onde o git existir, como no PC de desenvolvimento); senão "unknown". Versão
desconhecida nunca impede coleta, treino ou inferência.
"""
import os
import subprocess

ENV_VAR = "TCC_SOFTWARE_VERSION"
UNKNOWN = "unknown"
ROOT = os.path.dirname(os.path.abspath(__file__))


def software_version(root: str = ROOT) -> str:
    value = os.environ.get(ENV_VAR, "").strip()
    if value:
        return value
    try:
        with open(os.path.join(root, "VERSION"), encoding="utf-8") as f:
            value = f.readline().strip()
    except OSError:
        value = ""
    if value:
        return value
    try:
        out = subprocess.run(["git", "-C", root, "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5, check=True)
    except (OSError, subprocess.SubprocessError):
        return UNKNOWN
    commit = out.stdout.strip()
    return f"git-{commit}" if commit else UNKNOWN
