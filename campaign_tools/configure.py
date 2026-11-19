"""Discover the FN990 and persist location setup without creating campaign data."""

import argparse
import math
import sys
from pathlib import Path

from acquisition.snapshot import SNAPSHOT_INVALID, take_snapshot
from hal.fn990_driver import FN990Driver
from hal.models import GNSS_NO_FIX, GNSS_OFF, READ_OK, ServInfoReading

from . import config

OS_RELEASE = Path("/etc/os-release")
DEVICE_MODEL = Path("/proc/device-tree/model")


def detect_system() -> str:
    release = {}
    for line in OS_RELEASE.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator:
            release[key] = value.strip().strip("\"'")
    identity = " ".join(release.get(key, "") for key in ("ID", "NAME", "PRETTY_NAME")).lower()
    if "buildroot" in identity:
        return "buildroot"
    if "raspbian" in identity or "raspberry pi" in identity:
        return "raspberrypi_os"
    if release.get("ID") == "debian" and DEVICE_MODEL.is_file():
        if "raspberry pi" in DEVICE_MODEL.read_text(encoding="utf-8").lower():
            return "raspberrypi_os"
    raise ValueError("sistema nao reconhecido como Raspberry Pi OS ou Buildroot")


def default_data_dir(system: str) -> Path:
    if system == "raspberrypi_os":
        return Path.home() / "tcc-dados"
    if system == "buildroot":
        return Path("/data/tcc")
    raise ValueError(f"sistema nao suportado: {system}")


def extract_plmn(reading: ServInfoReading) -> str:
    if reading.status != READ_OK:
        raise ValueError(f"SERVINFO indisponivel: {reading.status}")
    plmns = {obs.plmn for obs in reading.observations if obs.plmn is not None}
    if len(plmns) != 1:
        raise ValueError("SERVINFO sem PLMN unico")
    plmn = plmns.pop()
    config.validate_config({"plmn": plmn})
    return plmn


def discover_ports():
    device_dir = Path("/dev")
    return sorted(str(path) for pattern in ("ttyUSB*", "ttyACM*") for path in device_dir.glob(pattern))


def select_port(ports, input_fn=input) -> str:
    valid = []
    for port in ports:
        driver = FN990Driver()
        try:
            driver.connect(port)
            if not driver.is_alive():
                raise RuntimeError("alive sem resposta")
            extract_plmn(driver.read_serving_info())
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"AVISO: {port}: {exc}", file=sys.stderr)
        else:
            valid.append(port)
        finally:
            driver.close()
    if not valid:
        raise RuntimeError("nenhuma porta AT valida com alive e SERVINFO")
    if len(valid) == 1:
        return valid[0]
    for index, port in enumerate(valid, 1):
        print(f"{index}: {port}")
    choice = input_fn("Porta AT (numero): ").strip()
    if not choice.isascii() or not choice.isdigit() or not 1 <= int(choice) <= len(valid):
        raise ValueError("escolha de porta invalida")
    return valid[int(choice) - 1]


def run_configure(args, input_fn=input) -> Path:
    if args.snapshots <= 0:
        raise ValueError("--snapshots precisa ser positivo")
    system = detect_system()
    if args.config is not None:
        target = Path(args.config).expanduser()
    else:
        target = config.find_config()
        if target is None:
            target = config.SYSTEM_CONFIG if system == "buildroot" else config.user_config_path()
    if target.exists() and not args.force:
        raise FileExistsError(f"{target}: ja existe; use --force para sobrescrever")

    port = select_port(discover_ports(), input_fn=input_fn)
    driver = FN990Driver()
    try:
        driver.connect(port)
        prepared = driver.prepare_for_collection()
        if prepared.gnss_power_on_sent and prepared.gnss_power_on_final != "OK":
            raise RuntimeError("falha ao ativar GNSS")
        gnss = driver.read_gnss()
        if gnss.status == READ_OK and gnss.fix is not None and gnss.fix.fix_type in (2, 3):
            print("GNSS: fix")
        elif gnss.status in (GNSS_OFF, GNSS_NO_FIX):
            print(f"GNSS: {gnss.status}")
            print(f"AVISO: GNSS {gnss.status}; a visita ainda exige cinco fixes validos.", file=sys.stderr)
        else:
            raise RuntimeError(f"leitura GNSS invalida: {gnss.status}")
        plmn = extract_plmn(driver.read_serving_info())
        durations = []
        successful = 0
        for index in range(1, args.snapshots + 1):
            snapshot = take_snapshot(driver)
            total = snapshot.timings_s.get("total")
            if total is None or not math.isfinite(total) or total <= 0:
                print(f"AVISO: snapshot {index}: duracao total invalida", file=sys.stderr)
                continue
            durations.append(total)
            print(f"Snapshot {index}/{args.snapshots}: {snapshot.status}, {total:.3f} s")
            if snapshot.status != SNAPSHOT_INVALID and snapshot.transport_error is None:
                successful += 1
            else:
                print(f"AVISO: snapshot {index} falhou", file=sys.stderr)
        if not successful:
            raise RuntimeError("todos os snapshots falharam; configuracao nao gravada")
        interval = math.ceil((max(durations) + 30) / 30) * 30
        data_dir = default_data_dir(system)
        data_dir.mkdir(parents=True, exist_ok=True)
        values = {
            "system": system, "data_dir": str(data_dir), "port": port, "plmn": plmn,
            "environment": args.environment, "interval_s": str(interval),
        }
        config.save_config(target, values, force=args.force)
    finally:
        driver.close()
    print(f"Configuracao: {target}")
    print(f"{system}; {port}; PLMN {plmn}; {args.environment}; intervalo {interval} s; dados {data_dir}")
    return target


def _main(argv=None):
    parser = argparse.ArgumentParser(description="Configura a plataforma de localizacao uma vez, antes das visitas.")
    parser.add_argument("--environment", choices=config.ENVIRONMENTS, required=True)
    parser.add_argument("--config", help="destino tcc-location.conf (padrao: arquivo existente ou local do sistema)")
    parser.add_argument("--snapshots", type=int, default=4)
    parser.add_argument("--force", action="store_true", help="permite sobrescrever a configuracao existente")
    args = parser.parse_args(argv)
    if args.snapshots <= 0:
        parser.error("--snapshots precisa ser positivo")
    try:
        run_configure(args)
    except (EOFError, OSError, RuntimeError, ValueError) as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
