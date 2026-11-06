"""Records campaign environment metadata without collecting device or subscriber identifiers."""

import argparse
import math
import platform
import re
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import serial

from software_version import software_version


def _git_metadata(root):
    try:
        commit = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            check=True, capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "nao_disponivel", "nao_disponivel"
    return commit or "nao_disponivel", "limpo" if not status else "alteracoes_locais"


def create_manifest(output, campaign_id, system, operator, db_path, plmn, port,
                    interval_s, antenna_height_m, orientation, weather="nao_informado",
                    buildroot_image_sha256="nao_informado", phone_used="nao", root=None):
    if (not math.isfinite(interval_s) or interval_s <= 0 or
            not math.isfinite(antenna_height_m) or antenna_height_m < 0):
        raise ValueError("intervalo precisa ser positivo e altura não pode ser negativa")
    if not all((campaign_id, system, operator, plmn, port, orientation)):
        raise ValueError("identificação da campanha, sistema, responsável, PLMN, porta e orientação são obrigatórios")
    if (buildroot_image_sha256 and buildroot_image_sha256 != "nao_informado" and
            re.fullmatch(r"[0-9a-fA-F]{64}", buildroot_image_sha256) is None):
        raise ValueError("hash da imagem Buildroot precisa ser SHA-256 hexadecimal de 64 caracteres")
    root_path = Path(root) if root is not None else Path(__file__).resolve().parent.parent
    commit, git_status = _git_metadata(root_path)
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    fields = (
        ("campaign_id", campaign_id),
        ("system", system),
        ("created_utc", datetime.now(timezone.utc).isoformat(timespec="seconds")),
        ("operator", operator),
        ("operating_system", platform.platform()),
        ("kernel", platform.release()),
        ("python", platform.python_version()),
        ("pyserial", serial.__version__),
        ("sqlite", sqlite3.sqlite_version),
        ("application_version", software_version()),
        ("git_commit", commit),
        ("git_status", git_status),
        ("buildroot_image_sha256", buildroot_image_sha256 or "nao_informado"),
        ("phone_as_reference_check", phone_used),
        ("database_file", Path(db_path).name),
        ("campaign_plmn", plmn),
        ("at_port", port),
        ("interval_s", interval_s),
        ("antenna_height_m", antenna_height_m),
        ("antenna_orientation", orientation),
        ("weather", weather or "nao_informado"),
        ("planned_points", 8),
        ("sessions_per_point", 2),
        ("measurements_per_session", 3),
        ("valid_gnss_fixes_per_reference", 5),
    )
    with target.open("x", encoding="utf-8", newline="\n") as stream:
        for key, value in fields:
            stream.write(f"{key}={value}\n")
    return target


def _main(argv=None):
    parser = argparse.ArgumentParser(description="Cria o manifesto de uma execução da campanha.")
    parser.add_argument("--output", required=True)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--system", choices=("raspberrypi_os", "buildroot"), required=True)
    parser.add_argument("--operator", required=True)
    parser.add_argument("--db", required=True)
    parser.add_argument("--plmn", required=True)
    parser.add_argument("--port", required=True)
    parser.add_argument("--interval-s", type=float, required=True)
    parser.add_argument("--antenna-height-m", type=float, required=True)
    parser.add_argument("--orientation", required=True)
    parser.add_argument("--weather", default="nao_informado")
    parser.add_argument("--buildroot-image-sha256", default="nao_informado")
    parser.add_argument("--phone-used", choices=("sim", "nao"), default="nao")
    parser.add_argument("--root")
    args = parser.parse_args(argv)
    create_manifest(
        args.output, args.campaign_id, args.system, args.operator, args.db, args.plmn,
        args.port, args.interval_s, args.antenna_height_m, args.orientation, args.weather,
        args.buildroot_image_sha256, args.phone_used, args.root,
    )
    print(f"Manifesto criado: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
