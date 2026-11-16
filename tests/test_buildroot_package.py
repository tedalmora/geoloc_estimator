"""Verifica as exclusoes usadas na sincronizacao do pacote Buildroot."""

import os
import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "buildroot-external-tcc" / "package" / "tcc-positioning" / "tcc-positioning.mk"
VARIABLE = "TCC_POSITIONING_OVERRIDE_SRCDIR_RSYNC_EXCLUSIONS"
APP_DIRS = "TCC_POSITIONING_APP_DIRS"
OVERLAY = ROOT / "buildroot-external-tcc" / "board" / "tcc-rpi3" / "rootfs-overlay"
LOCAL_SECRETS = ROOT / "buildroot-external-tcc" / "local-secrets"
POST_BUILD = ROOT / "buildroot-external-tcc" / "board" / "tcc-rpi3" / "post-build.sh"
BASH = shutil.which("bash")


def _bash_path(path):
    if os.name != "nt":
        return str(path)

    bash_path = Path(BASH)
    drive, tail = os.path.splitdrive(str(path))
    relative = tail.lstrip("\\/").replace("\\", "/")
    if bash_path.parent.name.lower() == "system32":
        return f"/mnt/{drive[0].lower()}/{relative}"

    cygpath = shutil.which("cygpath")
    if cygpath is None:
        candidate = bash_path.parent.parent / "usr" / "bin" / "cygpath.exe"
        if candidate.is_file():
            cygpath = str(candidate)
    if cygpath is not None:
        result = subprocess.run(
            [cygpath, "-u", str(path)], capture_output=True, text=True, check=True
        )
        return result.stdout.strip()
    return f"/{drive[0].lower()}/{relative}"


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
    def test_pacote_inclui_runtime_campaign_tools_e_servicos_do_overlay(self):
        lines = PACKAGE.read_text(encoding="utf-8").splitlines()
        app_dirs = next(line for line in lines if line.startswith(APP_DIRS + " ="))
        self.assertIn("runtime", app_dirs.split())
        self.assertIn("campaign_tools", app_dirs.split())
        for path in (
            "etc/init.d/S98tcc-runtime",
            "etc/init.d/S40tcc-hotspot",
            "etc/tcc/runtime.conf.example",
            "etc/tcc/hostapd.conf.example",
            "etc/dnsmasq.conf",
        ):
            self.assertTrue((OVERLAY / path).is_file(), path)
        self.assertTrue(POST_BUILD.is_file())
        self.assertTrue((LOCAL_SECRETS / "README.md").is_file())
        self.assertTrue((LOCAL_SECRETS / "hostapd.conf.example").is_file())
        self.assertTrue((LOCAL_SECRETS / "root_password_hash.example").is_file())

    def test_examples_are_placeholders_and_only_real_credentials_are_ignored(self):
        hostapd_example = (LOCAL_SECRETS / "hostapd.conf.example").read_text(encoding="utf-8")
        self.assertIn("interface=wlan0", hostapd_example)
        self.assertIn("ssid=TCC-RPI3", hostapd_example)
        self.assertIn("wpa=2", hostapd_example)
        self.assertIn("wpa_passphrase=SUBSTITUIR_LOCALMENTE", hostapd_example)
        hash_example = (LOCAL_SECRETS / "root_password_hash.example").read_text(encoding="utf-8").strip()
        self.assertFalse(hash_example.startswith("$6$"))

        gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        expected_ignored = {
            "buildroot-external-tcc/local-secrets/hostapd.conf",
            "buildroot-external-tcc/local-secrets/root_password_hash",
        }
        self.assertTrue(expected_ignored.issubset({line.strip() for line in gitignore}))
        self.assertNotIn("buildroot-external-tcc/local-secrets/", gitignore)

        ignored = subprocess.run(
            ["git", "-C", str(ROOT), "check-ignore", "--no-index", "--",
             *sorted(expected_ignored)],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(ignored.returncode, 0, ignored.stderr)
        for example in ("README.md", "hostapd.conf.example", "root_password_hash.example"):
            result = subprocess.run(
                ["git", "-C", str(ROOT), "check-ignore", "--no-index", "--quiet", "--",
                 f"buildroot-external-tcc/local-secrets/{example}"],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 1, example)

    @unittest.skipUnless(BASH, "requer bash para testar o post-build")
    def test_post_build_fails_for_missing_or_invalid_local_credentials(self):
        invalid_cases = (
            ("missing", None, None),
            ("wpa_placeholder", "wpa_passphrase=SUBSTITUIR_LOCALMENTE\n", "$6$salt$" + "A" * 86 + "\n"),
            ("wpa_short", "wpa_passphrase=short\n", "$6$salt$" + "A" * 86 + "\n"),
            ("hash_malformed", "wpa_passphrase=" + "x" * 16 + "\n", "HASH_INVALIDO\n"),
        )
        for name, passphrase, root_hash in invalid_cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory(prefix="tcc-post-build-") as directory:
                root = Path(directory)
                script = root / "ext" / "board" / "tcc-rpi3" / "post-build.sh"
                script.parent.mkdir(parents=True)
                shutil.copyfile(POST_BUILD, script)
                secrets = root / "ext" / "local-secrets"
                secrets.mkdir()
                target = root / "target"
                (target / "etc" / "tcc").mkdir(parents=True)
                shadow = target / "etc" / "shadow"
                shadow.write_text("root:oldhash:1:0:99999:7:::\ndaemon:*:1:0:99999:7:::\n", encoding="utf-8")
                overlay = script.parent / "rootfs-overlay" / "etc" / "tcc"
                overlay.mkdir(parents=True)
                shutil.copyfile(OVERLAY / "etc/tcc/runtime.conf.example", overlay / "runtime.conf.example")
                shutil.copyfile(LOCAL_SECRETS / "hostapd.conf.example", secrets / "hostapd.conf.example")
                if passphrase is not None:
                    (secrets / "hostapd.conf").write_text(
                        "interface=wlan0\nssid=TCC-RPI3\nwpa=2\nwpa_key_mgmt=WPA-PSK\n" + passphrase,
                        encoding="utf-8",
                    )
                if root_hash is not None:
                    (secrets / "root_password_hash").write_text(root_hash, encoding="utf-8")

                result = subprocess.run(
                    [BASH, _bash_path(script), _bash_path(target)],
                    capture_output=True, text=True, check=False,
                )
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertEqual(shadow.read_text(encoding="utf-8").splitlines()[0].split(":")[1], "oldhash")
                self.assertNotIn("$6$salt$", result.stdout + result.stderr)

    @unittest.skipUnless(BASH, "requer bash para testar o post-build")
    def test_post_build_installs_credentials_without_logging_hash(self):
        with tempfile.TemporaryDirectory(prefix="tcc-post-build-") as directory:
            root = Path(directory)
            script = root / "ext" / "board" / "tcc-rpi3" / "post-build.sh"
            script.parent.mkdir(parents=True)
            shutil.copyfile(POST_BUILD, script)
            secrets = root / "ext" / "local-secrets"
            secrets.mkdir()
            shutil.copyfile(LOCAL_SECRETS / "hostapd.conf.example", secrets / "hostapd.conf.example")
            passphrase = "tcc-test-" + "x" * 16
            hostapd = (LOCAL_SECRETS / "hostapd.conf.example").read_text(encoding="utf-8")
            hostapd = hostapd.replace("SUBSTITUIR_LOCALMENTE", passphrase)
            (secrets / "hostapd.conf").write_text(hostapd, encoding="utf-8")
            root_hash = "$6$testsalt$" + "A" * 86
            (secrets / "root_password_hash").write_bytes((root_hash + "\n").encode("ascii"))

            overlay = script.parent / "rootfs-overlay" / "etc" / "tcc"
            overlay.mkdir(parents=True)
            shutil.copyfile(OVERLAY / "etc/tcc/runtime.conf.example", overlay / "runtime.conf.example")
            target = root / "target"
            (target / "etc" / "tcc").mkdir(parents=True)
            shadow = target / "etc" / "shadow"
            shadow.write_text("root:oldhash:1:0:99999:7:::\ndaemon:*:1:0:99999:7:::\n", encoding="utf-8")

            result = subprocess.run(
                [BASH, _bash_path(script), _bash_path(target)],
                capture_output=True, text=True, check=False,
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn(root_hash, result.stdout + result.stderr)
            self.assertNotIn(passphrase, result.stdout + result.stderr)
            shadow_lines = shadow.read_text(encoding="utf-8").splitlines()
            self.assertEqual(shadow_lines[0].split(":")[1], root_hash)
            self.assertEqual(shadow_lines[1], "daemon:*:1:0:99999:7:::")
            self.assertEqual((target / "etc/tcc/hostapd.conf").read_text(encoding="utf-8"), hostapd)
            runtime = target / "etc/tcc/runtime.conf"
            self.assertIn("TCC_MODE=manual\n", runtime.read_text(encoding="utf-8"))
            if os.name != "nt":
                self.assertEqual((target / "etc/tcc/hostapd.conf").stat().st_mode & 0o777, 0o600)
                self.assertEqual(shadow.stat().st_mode & 0o777, 0o600)

            runtime.write_text("preserve this configuration\n", encoding="utf-8")
            result = subprocess.run(
                [BASH, _bash_path(script), _bash_path(target)],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(runtime.read_text(encoding="utf-8"), "preserve this configuration\n")

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
                Path("campaign_tools") / "visit.py",
                Path("runtime") / "__init__.py",
                Path("runtime") / "daemon.py",
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
