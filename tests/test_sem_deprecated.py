"""O código ativo não pode depender de codigo/deprecated/ (material só de valor histórico)."""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEPRECATED = ROOT / "deprecated"
REFERENCIA = re.compile(
    r"^\s*(?:from|import)\s+deprecated\b"  # import do pacote
    r"|deprecated[/\\]"  # caminho para a pasta
    r"|sys\.path\.(?:append|insert)",  # atalho para importar fora dos pacotes
    re.M,
)


def _arquivos_ativos():
    for path in ROOT.rglob("*.py"):
        partes = set(path.relative_to(ROOT).parts)
        if partes & {"venv", ".venv", ".git", "deprecated"} or path == pathlib.Path(__file__).resolve():
            continue
        yield path


def test_codigo_ativo_nao_referencia_deprecated():
    ofensores = [str(p.relative_to(ROOT)) for p in _arquivos_ativos() if REFERENCIA.search(p.read_text(encoding="utf-8"))]
    assert ofensores == []


def test_deprecated_nao_e_pacote_importavel():
    if DEPRECATED.exists():
        assert list(DEPRECATED.rglob("__init__.py")) == []
