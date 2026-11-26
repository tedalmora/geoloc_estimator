"""Inferência periódica do FN990 sem iniciar ou persistir campanhas.

Dois perfis do mesmo daemon: normal (TCC_DEBUG=0, o produto) e debug (TCC_DEBUG=1, instrumentação de
engenharia). O debug só observa e persiste mais dados do mesmo ciclo (um snapshot, uma estimativa);
não executa AT adicional, não altera aquisição, inferência, intervalo nem resultado.
"""

import argparse
import hashlib
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
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Tuple

from acquisition.snapshot import take_snapshot
from estimador.estimate import PositionEstimate
from estimador.geometry import haversine_m, validate_lat_lon
from estimador.live import estimate_from_snapshot
from estimador.methods.cell_id import CellRegistry, load_registry
from estimador.methods.fingerprint import BundleError, WknnModel, load_bundle
from hal.fn990_driver import FN990Driver
from hal.models import READ_OK, to_jsonable
from software_version import software_version


CONFIG_KEYS = {
    "TCC_MODE", "TCC_METHOD", "TCC_PORT", "TCC_INTERVAL", "TCC_MODEL",
    "TCC_REGISTRY", "TCC_OUTPUT", "TCC_HISTORY", "TCC_LOG",
}
OPTIONAL_KEYS = {"TCC_DEBUG", "TCC_METRICS", "TCC_SNAPSHOTS_DIR", "TCC_RUN_MANIFEST"}
METHODS = {"wknn", "cell-id", "gnss"}
OUTPUT_PATHS = ("output", "history", "log")
DEBUG_PATHS = ("metrics", "snapshots_dir", "run_manifest")
MODEM_RETRY_S = 5
SAMPLER_INTERVAL_S = 1.0
HARDWARE_ERROR_PREFIX = "FN990 indisponível"
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
    debug: bool = False
    metrics: str = ""
    snapshots_dir: str = ""
    run_manifest: str = ""


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
        if not separator or key not in CONFIG_KEYS | OPTIONAL_KEYS:
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
    if values.get("TCC_DEBUG", "0") not in ("0", "1"):
        raise RuntimeConfigError("TCC_DEBUG deve ser 0 ou 1")
    debug = values.get("TCC_DEBUG", "0") == "1"
    metrics, snapshots_dir, run_manifest = (
        values.get("TCC_METRICS", ""), values.get("TCC_SNAPSHOTS_DIR", ""), values.get("TCC_RUN_MANIFEST", ""),
    )
    if debug:  # defaults next to TCC_OUTPUT; the debug files never replace the operational ones
        base = Path(values["TCC_OUTPUT"]).expanduser().parent
        metrics = metrics or str(base / "runtime_metrics.csv")
        snapshots_dir = snapshots_dir or str(base / "snapshots")
        run_manifest = run_manifest or str(base / "runtime_manifest.json")
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
        debug=debug,
        metrics=metrics,
        snapshots_dir=snapshots_dir,
        run_manifest=run_manifest,
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
    names = list(OUTPUT_PATHS)
    if config.debug:
        paths += [Path(getattr(config, name)).expanduser() for name in DEBUG_PATHS]
        names += list(DEBUG_PATHS)
    resolved = [path.resolve() for path in paths]
    if len(set(resolved)) != len(resolved):
        if config.debug:
            raise RuntimeConfigError(
                "TCC_OUTPUT, TCC_HISTORY, TCC_LOG, TCC_METRICS, TCC_SNAPSHOTS_DIR e TCC_RUN_MANIFEST "
                "devem ser caminhos diferentes"
            )
        raise RuntimeConfigError("TCC_OUTPUT, TCC_HISTORY e TCC_LOG devem ser arquivos diferentes")
    for name, path in zip(names, paths):
        try:
            if name == "snapshots_dir":
                path.mkdir(parents=True, exist_ok=True)
                if not path.is_dir():
                    raise RuntimeConfigError(f"{path}: o destino precisa ser um diretório")
                _write_probe(path)
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and not path.is_file():
                raise RuntimeConfigError(f"{path}: o destino precisa ser um arquivo")
            _write_probe(path.parent)
            if name in ("history", "log", "metrics") and path.exists():
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


class CycleProbe:
    """Observador de um ciclo (TCC_DEBUG=1). infer_once só grava nele; nunca lê dele nem muda o fluxo."""

    def __init__(self, sampler=None):
        self.sampler = sampler
        self.snapshot = None  # o Snapshot que gerou a estimativa (wknn e cell-id)
        self.gnss = None  # a GnssReading do mesmo ciclo (snapshot.gnss ou a leitura do método gnss)
        self.estimate = None

    def state(self, name: str) -> None:
        if self.sampler is not None:
            self.sampler.set(state=name)


def infer_once(config: RuntimeConfig, model: Optional[WknnModel],
               registry: Optional[CellRegistry], probe: Optional[CycleProbe] = None) -> dict:
    started = time.perf_counter()
    if probe is not None:
        probe.state("acquiring")
    driver = FN990Driver()
    try:
        driver.connect(config.port)
        acquired = time.perf_counter()
        if config.method == "gnss":
            reading = driver.read_gnss()
            if probe is not None:
                probe.gnss = reading
            timings = {"acquisition_s": time.perf_counter() - acquired}
            fix = reading.fix
            if reading.status == "ok" and fix is not None and fix.fix_type in (2, 3):
                estimate = PositionEstimate("gnss", "valid", lat=fix.lat, lon=fix.lon, technology="GNSS")
            else:
                timings["total_s"] = time.perf_counter() - started
                return _unavailable("gnss", f"GNSS sem fix válido: {reading.status}", timings)
        else:
            snapshot = take_snapshot(driver)
            if probe is not None:
                probe.snapshot, probe.gnss = snapshot, snapshot.gnss
                probe.state("predicting")
            acquired_done = time.perf_counter()
            estimate = estimate_from_snapshot(snapshot, model, registry)
            timings = {
                "acquisition_s": acquired_done - acquired,
                "estimate_s": time.perf_counter() - acquired_done,
            }
    except (ConnectionError, OSError, RuntimeError) as exc:
        if probe is not None:
            probe.state("failed")
        return _unavailable(config.method, f"{HARDWARE_ERROR_PREFIX}: {exc}",
                            {"total_s": time.perf_counter() - started})
    finally:
        driver.close()

    if probe is not None:
        probe.estimate = estimate
    timings["total_s"] = time.perf_counter() - started
    return _from_estimate(estimate, timings)


def _write_json_atomic(path, data, **dump_options) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=str(destination.parent),
            prefix=f".{destination.name}.", suffix=".tmp", delete=False,
        ) as file:
            temporary = file.name
            json.dump(data, file, ensure_ascii=False, **dump_options)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, destination)
    except (OSError, ValueError, TypeError):
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)
        raise


def write_latest(path: str, result: dict) -> None:
    _write_json_atomic(path, result, allow_nan=False)


def append_history(path: str, result: dict) -> None:
    with open(path, "a", encoding="utf-8") as file:
        json.dump(result, file, ensure_ascii=False, allow_nan=False)
        file.write("\n")
        file.flush()


# ------------------------------------------------------------------------------------------ debug
def _utc_iso(moment: datetime) -> str:
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _count_observations(observations, rat):
    items = [o for o in observations if o.rat == rat]
    return len(items), len({o.radio_key for o in items if o.radio_key})


def _valid_gnss_fix(reading):
    """Fix 2D/3D com coordenadas válidas (mesmo critério do método gnss); senão None."""
    fix = getattr(reading, "fix", None)
    if reading is None or reading.status != READ_OK or fix is None or fix.fix_type not in (2, 3):
        return None
    try:
        validate_lat_lon(fix.lat, fix.lon)
    except ValueError:
        return None
    return fix


def build_debug_record(result: dict, probe: CycleProbe, cycle: dict, model_sha256: Optional[str]) -> dict:
    """Campos de comparação de um ciclo, a partir do snapshot e da estimativa já produzidos."""
    snapshot, estimate, reading = probe.snapshot, probe.estimate, probe.gnss
    timings = snapshot.timings_s if snapshot is not None else {}
    metadata = estimate.metadata if estimate is not None else {}
    valid = result["status"] == "valid"
    fix = _valid_gnss_fix(reading)
    distance = None
    if valid and fix is not None and result.get("method") == "wknn":
        distance = haversine_m(result["latitude"], result["longitude"], fix.lat, fix.lon)
    lte = _count_observations(snapshot.observations, "LTE") if snapshot is not None else (None, None)
    nr = _count_observations(snapshot.observations, "NR") if snapshot is not None else (None, None)
    response = getattr(reading, "response", None)
    record = dict(result)  # campos operacionais atuais primeiro, sem alteração
    record.update({
        "cycle_id": cycle["cycle_id"], "scheduled_utc": cycle["scheduled_utc"],
        "started_utc": cycle["started_utc"], "finished_utc": cycle["finished_utc"],
        "period_s": cycle["period_s"], "schedule_delay_s": cycle["schedule_delay_s"],
        "cycle_total_s": cycle["cycle_total_s"],
        "snapshot_started_at": snapshot.started_at if snapshot is not None else None,
        "snapshot_status": snapshot.status if snapshot is not None else None,
        "snapshot_reasons": list(snapshot.reasons) if snapshot is not None else None,
        "access_mode": snapshot.access_mode if snapshot is not None else None,
        "moni_s": timings.get("MONI"), "servinfo_s": timings.get("SERVINFO"), "csurv_s": timings.get("CSURV"),
        "gnss_s": timings.get("GNSS"), "snapshot_total_s": timings.get("total"),
        "n_observations": len(snapshot.observations) if snapshot is not None else None,
        "n_lte_observations": lte[0], "n_nr_observations": nr[0],
        "n_lte_radio_keys": lte[1], "n_nr_radio_keys": nr[1],
        "serving_radio_key": snapshot.serving.radio_key if snapshot is not None else None,
        "serving_global_cell_key": snapshot.serving.global_cell_key if snapshot is not None else None,
        "transport_error": snapshot.transport_error if snapshot is not None else None,
        "anomaly_count": len(snapshot.anomalies) if snapshot is not None else None,
        "model_sha256": model_sha256, "prediction_s": result["timings_s"].get("estimate_s"),
        "valid": valid, "lat": result["latitude"], "lon": result["longitude"],
        "x_m": estimate.x_m if valid and estimate is not None else None,
        "y_m": estimate.y_m if valid and estimate is not None else None,
        "n_observed_keys": metadata.get("n_observed_keys"), "n_ignored_keys": metadata.get("n_ignored_keys"),
        "n_neighbors": metadata.get("n_neighbors"), "zero_distance": metadata.get("zero_distance"),
        "gnss_status": reading.status if reading is not None else None,
        "gnss_lat": fix.lat if fix is not None else None, "gnss_lon": fix.lon if fix is not None else None,
        "gnss_alt_m": fix.alt_m if fix is not None else None,
        "gnss_fix_type": fix.fix_type if fix is not None else None,
        "gnss_hdop": fix.hdop if fix is not None else None, "gnss_nsat": fix.nsat if fix is not None else None,
        "gnss_utc": fix.utc if fix is not None else None,
        "gnss_elapsed_s": response.elapsed_s if response is not None else None,
        "wknn_gnss_distance_m": distance,
    })
    return record


def build_run_manifest(config: RuntimeConfig, run_id: str, started_utc: str, model_sha256: Optional[str],
                       proc_root: str = "/proc") -> dict:
    """Identificação da execução e da plataforma para comparar Raspberry Pi OS e Buildroot."""
    import platform
    import socket

    from campaign_tools import configure, monitor

    try:
        platform_id = configure.detect_system()
    except (OSError, ValueError):
        platform_id = None
    return {
        "run_id": run_id, "platform_id": platform_id, "hostname": socket.gethostname(),
        "os_release": monitor.os_release(configure.OS_RELEASE), "kernel": platform.release(),
        "architecture": platform.machine(), "python": platform.python_version(),
        "software_version": software_version(),
        "git_commit": monitor.git_commit(Path(__file__).resolve().parent.parent),
        "runtime_started_utc": started_utc, "tcc_debug": 1,
        "interval_s": int(config.interval), "method": config.method, "port": config.port,
        "model": config.model or None, "model_sha256": model_sha256,
        "configuration": {"TCC_MODE": config.mode, "TCC_METHOD": config.method, "TCC_INTERVAL": config.interval,
                          "TCC_DEBUG": 1, "TCC_REGISTRY": config.registry or None},
        "paths": {"output": config.output, "history": config.history, "log": config.log,
                  "metrics": config.metrics, "snapshots": config.snapshots_dir, "manifest": config.run_manifest},
        "sampler_interval_s": SAMPLER_INTERVAL_S,
        "cpu": monitor.cpu_info(proc_root), "memory_total_kb": monitor.memory_total_kb(proc_root),
    }


def _write_manifest(path: str, manifest: dict) -> None:
    target = Path(path)
    if target.exists():  # preserve the manifest of the previous run, next to the new one
        try:
            previous = json.loads(target.read_text(encoding="utf-8"))["run_id"]
        except (OSError, ValueError, KeyError, TypeError):
            previous = "previous"
        os.replace(target, target.with_name(f"{target.stem}.{previous}{target.suffix}"))
    _write_json_atomic(target, manifest, indent=2, default=str)


class DebugSession:
    """Estado do modo debug: execução, sampler de métricas e arquivos por ciclo (só observação)."""

    def __init__(self, config: RuntimeConfig, sampler, run_id: str, model_sha256: Optional[str]):
        self.config, self.sampler, self.run_id, self.model_sha256 = config, sampler, run_id, model_sha256
        self._cycles = 0

    def new_cycle(self) -> Tuple[str, CycleProbe]:
        self._cycles += 1
        cycle_id = f"{self.run_id}-C{self._cycles:06d}"
        self.sampler.set(session=cycle_id, state="acquiring")
        return cycle_id, CycleProbe(self.sampler)

    def state(self, name: str) -> None:
        self.sampler.set(state=name)

    def write_snapshot(self, cycle: dict, probe: CycleProbe, result: dict) -> Optional[Path]:
        if probe.snapshot is None:
            return None
        target = Path(self.config.snapshots_dir) / f"{cycle['cycle_id']}.json"
        if target.exists():
            raise FileExistsError(f"{target}: já existe; snapshot de ciclo nunca é sobrescrito")
        envelope = {
            "cycle_id": cycle["cycle_id"], "scheduled_utc": cycle["scheduled_utc"],
            "started_utc": cycle["started_utc"], "finished_utc": cycle["finished_utc"],
            "model_sha256": self.model_sha256, "result": result, "snapshot": to_jsonable(probe.snapshot),
        }
        _write_json_atomic(target, envelope, default=str)
        return target

    def stop(self, final_state: str) -> None:
        self.sampler.stop(final_state)
        if self.sampler.error is not None:
            logger.warning("métricas do runtime interrompidas: %s", self.sampler.error)


def start_debug_session(config: RuntimeConfig, model_sha256: Optional[str] = None) -> DebugSession:
    """Manifesto da execução e sampler do processo (campaign_tools.monitor), iniciados uma vez."""
    from campaign_tools import monitor

    started = datetime.now(timezone.utc)
    run_id = f"{started.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}"
    if model_sha256 is None and config.method == "wknn" and config.model:
        try:
            model_sha256 = hashlib.sha256(Path(config.model).read_bytes()).hexdigest()
        except OSError:
            model_sha256 = None
    try:
        _write_manifest(config.run_manifest, build_run_manifest(config, run_id, _utc_iso(started), model_sha256))
        sampler = monitor.ProcessSampler(
            "", str(Path(config.output).expanduser().parent), config.metrics, "runtime", "",
            interval_s=SAMPLER_INTERVAL_S,
        )
        sampler.set(state="starting")
        sampler.start()
    except (OSError, ValueError) as exc:
        raise RuntimeConfigError(f"instrumentação debug indisponível: {exc}") from exc
    return DebugSession(config, sampler, run_id, model_sha256)


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


def _fmt_s(value) -> str:
    return "-" if value is None else f"{value:.3f}"


def _hardware_failure(result: dict, probe: Optional[CycleProbe]) -> Optional[str]:
    reason = result.get("reason") or ""
    if reason.startswith(HARDWARE_ERROR_PREFIX):
        return reason
    snapshot = probe.snapshot if probe is not None else None
    if snapshot is not None and snapshot.transport_error:
        return snapshot.transport_error
    return reason if "erro_de_porta" in reason else None


def _log_cycle(label: str, result: dict, probe: Optional[CycleProbe]) -> None:
    failure = _hardware_failure(result, probe)
    if failure:
        logger.warning("hardware_error %s reason=%s", label, failure)
    timings = result.get("timings_s") or {}
    detail = f"method={result['method']} status={result['status']} total_s={_fmt_s(timings.get('total_s'))}"
    if probe is not None:
        snapshot_total = probe.snapshot.timings_s.get("total") if probe.snapshot is not None else None
        detail += f" snapshot_total_s={_fmt_s(snapshot_total)} prediction_s={_fmt_s(timings.get('estimate_s'))}"
    if result["status"] == "valid":
        logger.info("cycle_completed %s %s", label, detail)
    else:
        logger.warning("cycle_unavailable %s %s reason=%s", label, detail, result.get("reason"))


def _persist_debug(session: DebugSession, config: RuntimeConfig, cycle: dict, probe: CycleProbe,
                   result: dict) -> None:
    """History debug e snapshot bruto. Uma falha aqui é registrada e nunca derruba o produto."""
    try:
        append_history(config.history, build_debug_record(result, probe, cycle, session.model_sha256))
    except (OSError, ValueError, TypeError, KeyError) as exc:
        logger.error("debug_record_failed cycle_id=%s: %s", cycle["cycle_id"], exc)
        append_history(config.history, result)
    try:
        session.write_snapshot(cycle, probe, result)
    except (OSError, ValueError, TypeError) as exc:
        logger.error("debug_snapshot_failed cycle_id=%s: %s", cycle["cycle_id"], exc)


def run_loop(config: RuntimeConfig, model: Optional[WknnModel],
             registry: Optional[CellRegistry], stop_event: threading.Event,
             once: bool = False, session: Optional[DebugSession] = None) -> int:
    initial = _unavailable(
        config.method,
        "runtime iniciado; aguardando FN990",
        {},
    )
    write_latest(config.output, initial)
    append_history(config.history, dict(initial, event="runtime_started"))

    if session is not None:
        session.state("waiting")
    if stop_event.is_set() or not wait_for_modem(config, stop_event):
        return 0

    ready = _unavailable(
        config.method,
        "FN990 disponível; iniciando inferência",
        {},
    )
    append_history(config.history, dict(ready, event="modem_ready"))

    interval = int(config.interval)
    due_mono, due_dt = time.monotonic(), datetime.now(timezone.utc)
    previous_started = None
    cycles = 0
    while not stop_event.is_set():
        cycles += 1
        started_mono, started_dt = time.monotonic(), datetime.now(timezone.utc)
        if session is None:
            label, probe = f"n={cycles}", None
        else:
            cycle_id, probe = session.new_cycle()
            label = f"cycle_id={cycle_id}"
        logger.info("cycle_started %s", label)
        result = infer_once(config, model, registry) if probe is None else infer_once(config, model, registry, probe)
        if session is not None:
            session.state("writing")
        write_latest(config.output, result)
        if session is None:
            append_history(config.history, result)
        else:
            finished_mono, finished_dt = time.monotonic(), datetime.now(timezone.utc)
            cycle = {
                "cycle_id": cycle_id, "scheduled_utc": _utc_iso(due_dt), "started_utc": _utc_iso(started_dt),
                "finished_utc": _utc_iso(finished_dt),
                "period_s": None if previous_started is None else started_mono - previous_started,
                "schedule_delay_s": started_mono - due_mono, "cycle_total_s": finished_mono - started_mono,
            }
            _persist_debug(session, config, cycle, probe, result)
        _log_cycle(label, result, probe)
        if once:
            break
        previous_started = started_mono
        due_mono, due_dt = time.monotonic() + interval, datetime.now(timezone.utc) + timedelta(seconds=interval)
        if session is not None:
            session.state("waiting")
        stop_event.wait(interval)
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

    session = None
    if config.debug:
        try:
            session = start_debug_session(config)
        except RuntimeConfigError as exc:
            logger.error("Configuração runtime inválida: %s", exc)
            return 2

    stop_event = threading.Event()
    previous_term = signal.signal(signal.SIGTERM, lambda _signum, _frame: stop_event.set())
    previous_int = signal.signal(signal.SIGINT, lambda _signum, _frame: stop_event.set())
    code = 1
    try:
        logger.info("runtime_started method=%s interval_s=%s debug=%d%s", config.method, config.interval,
                    int(config.debug), f" run_id={session.run_id}" if session is not None else "")
        code = run_loop(config, model, registry, stop_event, once=args.once, session=session)
    except KeyboardInterrupt:
        stop_event.set()
        code = 0
    except OSError as exc:
        logger.error("Falha ao gravar os resultados; runtime encerrado: %s", exc)
    except Exception as exc:
        logger.exception("unexpected_error: %s", exc)
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)
        logger.info("runtime_stopped")
        if session is not None:
            session.stop("stopped" if code == 0 else "failed")
    return code


if __name__ == "__main__":
    sys.exit(main())
