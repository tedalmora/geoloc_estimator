"""Verifica as exclusoes usadas na sincronizacao do pacote Buildroot."""

import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "buildroot-external-tcc" / "package" / "tcc-positioning" / "tcc-positioning.mk"
VARIABLE = "TCC_POSITIONING_OVERRIDE_SRCDIR_RSYNC_EXCLUSIONS"


def _rsync_options():
    lines = PACKAGE.read_text(encoding="utf-8").splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith(VARIABLE + " ="))
    value = lines[start].split("=", 1)[1].strip()
    parts = []
    while value.endswith("\\"):
        parts.append(value[:-1])
        start += 1
        value = lines[start].strip()
    parts.append(value)
    return shlex.split(" ".join(parts))


class TestBuildrootPackage(unittest.TestCase):
    def test_exclusoes_sao_opcoes_do_rsync(self):
        self.assertEqual(
            _rsync_options(),
            [
                "--exclude=/venv",
                "--exclude=/.venv",
                "--exclude=/tests",
                "--exclude=/deprecated",
                "--exclude=/buildroot-external-tcc",
                "--exclude=__pycache__",
                "--exclude=.pytest_cache",
                "--exclude=pytest_*.txt",
            ],
        )

    @unittest.skipUnless(shutil.which("make") and shutil.which("rsync"), "requer make e rsync")
    def test_sincronizacao_exclui_arquivos_sem_perder_codigo(self):
        with tempfile.TemporaryDirectory(prefix="tcc-rsync-test-") as directory:
            root = Path(directory)
            source = root / "source"
            target = root / "target"
            source.mkdir()
            target.mkdir()
            kept = [
                Path("hal") / "fn990_driver.py",
                Path("acquisition") / "snapshot.py",
                Path("coletor") / "daemon.py",
                Path("estimador") / "live.py",
                Path("experimental") / "timing_advance.py",
                Path("nested") / "tests" / "example.py",
                Path("VERSION"),
                Path("software_version.py"),
            ]
            excluded = [
                Path("venv") / "file",
                Path(".venv") / "file",
                Path("tests") / "file",
                Path("deprecated") / "file",
                Path("buildroot-external-tcc") / "file",
                Path("hal") / "__pycache__" / "module.pyc",
                Path("hal") / ".pytest_cache" / "file",
                Path("pytest_report.txt"),
                Path("hal") / "pytest_nested.txt",
            ]
            for path in kept + excluded:
                file = source / path
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text("test", encoding="utf-8")

            (root / "--exclude=pytest_expanded.txt").write_text("test", encoding="utf-8")
            makefile = root / "Makefile"
            makefile.write_text(
                ".PHONY: sync\n"
                "sync:\n"
                "\trsync -au --chmod=u=rwX,go=rX "
                "$(TCC_POSITIONING_OVERRIDE_SRCDIR_RSYNC_EXCLUSIONS) "
                '"$(SOURCE)/" "$(DEST)"\n',
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    "make",
                    "--no-print-directory",
                    "-f",
                    str(PACKAGE),
                    "-f",
                    str(makefile),
                    "sync",
                    f"SOURCE={source}",
                    f"DEST={target}",
                ],
                cwd=root,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            for path in kept:
                self.assertTrue((target / path).is_file(), str(path))
            for path in excluded:
                self.assertFalse((target / path).exists(), str(path))
