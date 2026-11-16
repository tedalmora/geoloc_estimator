"""Inferência periódica do FN990 sem iniciar ou persistir campanhas."""

import argparse
import json
import logging
import math
import os
import re
import signal
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

from acquisition.snapshot import take_snapshot
from estimador.estimate import PositionEstimate
from estimador.live import estimate_from_snapshot
from estimador.methods.cell_id import CellRegistry, load_registry
from estimador.methods.fingerprint import BundleError, WknnModel, load_bundle
from hal.fn990_driver import FN990Driver
from software_version import software_version


CONFIG_KEYS = {
    "TCC_MODE", "TCC_METHOD", "TCC_PORT", "TCC_INTERVAL", "TCC_MODEL",
    "TCC_REGISTRY", "TCC_OUTPUT", "TCC_HISTORY", "TCC_LOG",
}
METHODS = {"wknn", "cell-id", "gnss"}
OUTPUT_PATHS = ("output", "history", "log")
MODEM_RETRY_S = 5
logger = logging.getLogger("runtime.daemon")


class RuntimeConfigError(ValueError):
    """Configuração ausente, inválida ou incompatível."""


@dataclass(frozen=True)
class RuntimeConfig:
    mode: str
    method: str
    port: str
    interval: str
    model: str
    registry: str
    output: str
    history: str
    log: str


def load_config(path: str) -> RuntimeConfig:
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise RuntimeConfigError(f"não foi possível ler {path}: {exc}") from exc

    values = {}
    for number, line in enumerate(lines, 1):
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        key, separator, value = text.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or key not in CONFIG_KEYS:
            raise RuntimeConfigError(f"{path}:{number}: configuração inválida")
        if key in values:
            raise RuntimeConfigError(f"{path}:{number}: chave duplicada {key}")
        values[key] = value

    missing = sorted(CONFIG_KEYS - values.keys())
    if missing:
        raise RuntimeConfigError(f"{path}: configurações ausentes: {', '.join(missing)}")
    if values["TCC_MODE"] not in ("manual", "runtime"):
        raise RuntimeConfigError("TCC_MODE deve ser manual ou runtime")
    for key in ("TCC_OUTPUT", "TCC_HISTORY", "TCC_LOG"):
        if not values[key]:
            raise RuntimeConfigError(f"{key} não pode ficar vazio")
    return RuntimeConfig(
        mode=values["TCC_MODE"],
        method=values["TCC_METHOD"],
        port=values["TCC_PORT"],
        interval=values["TCC_INTERVAL"],
        model=values["TCC_MODEL"],
        registry=values["TCC_REGISTRY"],
        output=values["TCC_OUTPUT"],
        history=values["TCC_HISTORY"],
        log=values["TCC_LOG"],
    )


def validate_runtime_config(config: RuntimeConfig) -> Tuple[Optional[WknnModel], Optional[CellRegistry]]:
    if config.mode != "runtime":
        raise RuntimeConfigError("TCC_MODE deve ser runtime para iniciar a inferência")
    if config.method not in METHODS:
        raise RuntimeConfigError(f"TCC_METHOD inválido: {config.method!r}")
    if not config.port:
        raise RuntimeConfigError("TCC_PORT deve indicar a porta AT confirmada do FN990")
    if not re.fullmatch(r"[0-9]+", config.interval) or int(config.interval) <= 0:
        raise RuntimeConfigError("TCC_INTERVAL deve ser um inteiro positivo")

    if config.method == "wknn":
        if not config.model:
            raise RuntimeConfigError("TCC_MODEL é obrigatório para WKNN")
        try:
            return load_bundle(config.model), None
        except (BundleError, OSError) as exc:
            raise RuntimeConfigError(f"modelo WKNN inválido: {exc}") from exc
    if config.method == "cell-id":
        if not config.registry:
            raise RuntimeConfigError("TCC_REGISTRY é obrigatório para Cell-ID")
        registry = load_registry(config.registry)
        if not registry.valid:
            raise RuntimeConfigError(f"cadastro Cell-ID inválido: {registry.error}")
        return None, registry
    return None, None


def _write_probe(parent: Path) -> None:
    probe = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".tcc-write-test-", dir=str(parent), delete=False) as probe:
            pass
    finally:
        if probe is not None:
            try:
                os.unlink(probe.name)
            except OSError:
                pass


def prepare_output_paths(config: RuntimeConfig) -> None:
    paths = [Path(getattr(config, name)).expanduser() for name in OUTPUT_PATHS]
    resolved = [path.resolve() for path in paths]
    if len(set(resolved)) != len(resolved):
        raise RuntimeConfigError("TCC_OUTPUT, TCC_HISTORY e TCC_LOG devem ser arquivos diferentes")
    for name, path in zip(OUTPUT_PATHS, paths):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and not path.is_file():
                raise RuntimeConfigError(f"{path}: o destino precisa ser um arquivo")
            _write_probe(path.parent)
            if name in ("history", "log") and path.exists():
                with path.open("a", encoding="utf-8"):
                    pass
        except OSError as exc:
            raise RuntimeConfigError(f"{path.parent}: diretório ou arquivo não gravável ({exc})") from exc


def configure_logging(path: str) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        handlers=[logging.FileHandler(path, encoding="utf-8")],
        force=True,
    )


def _unavailable(method: str, reason: str, timings: dict) -> dict:
    return {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "method": method,
        "status": "unavailable",
        "latitude": None,
        "longitude": None,
        "technology": None,
        "reason": reason,
        "software_version": software_version(),
        "timings_s": timings,
    }


def _from_estimate(estimate: PositionEstimate, timings: dict) -> dict:
    latitude, longitude = estimate.lat, estimate.lon
    valid_coordinates = (
        isinstance(latitude, (int, float)) and not isinstance(latitude, bool)
        and isinstance(longitude, (int, float)) and not isinstance(longitude, bool)
        and math.isfinite(latitude) and math.isfinite(longitude)
        and -90 <= latitude <= 90 and -180 <= longitude <= 180
    )
    valid = estimate.valid and valid_coordinates
    if not estimate.valid:
        reason = estimate.reason or estimate.status
    elif not valid_coordinates:
        reason = "posição estimada com coordenadas inválidas"
    else:
        reason = None
    return {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "method": estimate.method,
        "status": "valid" if valid else "unavailable",
        "latitude": float(latitude) if valid else None,
        "longitude": float(longitude) if valid else None,
        "technology": estimate.technology,
        "reason": reason,
        "software_version": software_version(),
        "timings_s": timings,
    }


def infer_once(config: RuntimeConfig, model: Optional[WknnModel],
               registry: Optional[CellRegistry]) -> dict:
    started = time.perf_counter()
    driver = FN990Driver()
    try:
        driver.connect(config.port)
        acquired = time.perf_counter()
        if config.method == "gnss":
            reading = driver.read_gnss()
            timings = {"acquisition_s": time.perf_counter() - acquired}
            fix = reading.fix
            if reading.status == "ok" and fix is not None and fix.fix_type in (2, 3):
                estimate = PositionEstimate("gnss", "valid", lat=fix.lat, lon=fix.lon, technology="GNSS")
            else:
                timings["total_s"] = time.perf_counter() - started
                return _unavailable("gnss", f"GNSS sem fix válido: {reading.status}", timings)
        else:
            snapshot = take_snapshot(driver)
            acquired_done = time.perf_counter()
            estimate = estimate_from_snapshot(snapshot, model, registry)
            timings = {
                "acquisition_s": acquired_done - acquired,
                "estimate_s": time.perf_counter() - acquired_done,
            }
    except (ConnectionError, OSError, RuntimeError) as exc:
        return _unavailable(config.method, f"FN990 indisponível: {exc}", {"total_s": time.perf_counter() - started})
    finally:
        driver.close()

    timings["total_s"] = time.perf_counter() - started
    return _from_estimate(estimate, timings)


def write_latest(path: str, result: dict) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=str(destination.parent),
            prefix=f".{destination.name}.", suffix=".tmp", delete=False,
        ) as file:
            temporary = file.name
            json.dump(result, file, ensure_ascii=False, allow_nan=False)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, destination)
    except OSError:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)
        raise


def append_history(path: str, result: dict) -> None:
    with open(path, "a", encoding="utf-8") as file:
        json.dump(result, file, ensure_ascii=False, allow_nan=False)
        file.write("\n")
        file.flush()


def wait_for_modem(config: RuntimeConfig, stop_event: threading.Event) -> bool:
    while not stop_event.is_set():
        driver = FN990Driver()
        try:
            driver.connect(config.port)
        except (ConnectionError, OSError, RuntimeError) as exc:
            logger.warning("FN990 ainda indisponível; nova tentativa em %s s: %s", MODEM_RETRY_S, exc)
            stop_event.wait(MODEM_RETRY_S)
        else:
            logger.info("FN990 respondeu na porta configurada")
            return True
        finally:
            driver.close()
    return False


def run_loop(config: RuntimeConfig, model: Optional[WknnModel],
             registry: Optional[CellRegistry], stop_event: threading.Event,
             once: bool = False) -> int:
    initial = _unavailable(
        config.method,
        "runtime iniciado; aguardando FN990",
        {},
    )
    write_latest(config.output, initial)
    append_history(config.history, dict(initial, event="runtime_started"))

    if stop_event.is_set() or not wait_for_modem(config, stop_event):
        return 0

    ready = _unavailable(
        config.method,
        "FN990 disponível; iniciando inferência",
        {},
    )
    append_history(config.history, dict(ready, event="modem_ready"))

    while not stop_event.is_set():
        result = infer_once(config, model, registry)
        write_latest(config.output, result)
        append_history(config.history, result)
        logger.info("Inferência %s: %s", result["method"], result["status"])
        if once:
            break
        stop_event.wait(int(config.interval))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Inferência periódica do FN990; não inicia campanhas")
    parser.add_argument("--config", default="/etc/tcc/runtime.conf", help="arquivo runtime.conf")
    parser.add_argument("--once", action="store_true", help="executa uma inferência e encerra")
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
    except RuntimeConfigError as exc:
        print(f"runtime: {exc}", file=sys.stderr)
        return 2
    if config.mode == "manual":
        print("TCC_MODE=manual: runtime não iniciado.")
        return 0

    try:
        prepare_output_paths(config)
        configure_logging(config.log)
        model, registry = validate_runtime_config(config)
    except (RuntimeConfigError, OSError) as exc:
        if logging.getLogger().handlers:
            logger.error("Configuração runtime inválida: %s", exc)
        else:
            print(f"runtime: configuração inválida: {exc}", file=sys.stderr)
        return 2

    stop_event = threading.Event()
    previous_term = signal.signal(signal.SIGTERM, lambda _signum, _frame: stop_event.set())
    previous_int = signal.signal(signal.SIGINT, lambda _signum, _frame: stop_event.set())
    try:
        return run_loop(config, model, registry, stop_event, once=args.once)
    except OSError as exc:
        logger.error("Falha ao gravar os resultados; runtime encerrado: %s", exc)
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)


if __name__ == "__main__":
    sys.exit(main())
