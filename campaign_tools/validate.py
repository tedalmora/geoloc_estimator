"""Reproducible WKNN benchmark for developers: one shared snapshot per repetition, all models.

point: for each repetition takes ONE snapshot (acquisition.snapshot.take_snapshot) and applies that same
object to the spatial, temporal and complete bundles through estimador.live.estimate_from_snapshot, while
a sampler thread records process/system metrics. summarize: consolidates the artifacts without hardware.
The end-user flow (campaign_tools.position) is not touched.
"""

import argparse
import csv
import hashlib
import json
import math
import os
import platform
import re
import socket
import statistics
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from acquisition.snapshot import SNAPSHOT_INVALID, take_snapshot
from estimador.evaluate import error_summary, percentile
from estimador.geometry import haversine_m
from estimador.live import estimate_from_snapshot
from estimador.methods.fingerprint import BundleError, load_bundle
from hal.fn990_driver import FN990Driver
from hal.models import to_jsonable
from software_version import software_version

from . import config, manifest, monitor, position, prepare, wknn
from .reference import distance_m, read_canonical

MODELS = wknn.MODEL_PROTOCOLS
BASE_ORDER = ("complete", "temporal", "spatial")
VALIDATION_DIR = Path("avaliacoes") / "validation"
MODE = "validation"
STAGES = ("stabilization", "connect", "snapshot", "spatial", "temporal", "complete", "write")

ACQUISITION_FIELDS = (
    "platform", "campaign", "point", "repetition", "snapshot_id", "started_at", "snapshot_status", "reasons",
    "access_mode", "total_s", "moni_s", "servinfo_s", "csurv_s", "gnss_s", "n_observations", "n_lte_observations",
    "n_nr_observations", "n_lte_radio_keys", "n_nr_radio_keys", "serving_radio_key", "serving_global_cell_key",
    "transport_error", "anomaly_count", "repetition_s",
)
PREDICTION_FIELDS = (
    "platform", "campaign", "true_point", "repetition", "snapshot_id", "model", "model_protocol", "bundle_sha256",
    "valid", "status", "reason", "technology", "snapshot_status", "true_lat", "true_lon", "estimated_lat",
    "estimated_lon", "estimated_x_m", "estimated_y_m", "error_m", "n_observed_keys", "n_ignored_keys",
    "n_neighbors", "distance_metric", "zero_distance", "neighbor_measurement_ids", "model_load_s", "prediction_s",
)
FAILURE_FIELDS = ("point", "repetition", "snapshot_id", "stage", "model", "status", "reason", "timestamp_utc")
POINT_COLUMN = {"acquisitions.csv": "point", "predictions.csv": "true_point", "system_metrics.csv": "point",
                "failures.csv": "point"}
CSV_FIELDS = {"acquisitions.csv": ACQUISITION_FIELDS, "predictions.csv": PREDICTION_FIELDS,
              "system_metrics.csv": monitor.FIELDS, "failures.csv": FAILURE_FIELDS}
COMPAT_PROTOCOL = ("campaign", "technology", "models", "repetitions", "true_reference_source")


class ValidationError(RuntimeError):
    """Benchmark impossible or inconsistent; the message is shown to the operator."""


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def prediction_order(repetition, models):
    """Rotates the base order by repetition (T1 complete,temporal,spatial; T2 temporal,spatial,complete; ...)."""
    shift = (repetition - 1) % len(BASE_ORDER)
    rotated = BASE_ORDER[shift:] + BASE_ORDER[:shift]
    return [model for model in rotated if model in models]


# ---------------------------------------------------------------------------------------------- files
def write_json(path, data, replace=False):
    target = Path(path)
    if target.exists() and not replace:
        raise FileExistsError(f"{target}: ja existe")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2, ensure_ascii=False, default=str)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)


def read_rows(path, fields):
    target = Path(path)
    if not target.exists() or target.stat().st_size == 0:
        return []
    with target.open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != list(fields):
            raise ValidationError(f"{target}: cabecalho incompativel")
        return list(reader)


def append_rows(path, fields, rows):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    read_rows(target, fields)  # validates an existing header before appending
    new = not target.exists() or target.stat().st_size == 0
    with target.open("a", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(fields), extrasaction="ignore")
        if new:
            writer.writeheader()
        writer.writerows(rows)
        stream.flush()
        os.fsync(stream.fileno())


def rewrite_rows(path, fields, rows):
    target = Path(path)
    tmp = target.with_name(target.name + ".tmp")
    try:
        with tmp.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(fields), extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)


def point_artifacts(out, point):
    """Fingerprint files and CSV files that already hold data of this point."""
    found = sorted(Path(out, "fingerprints").glob(f"{point}_T*.json")) if Path(out, "fingerprints").is_dir() else []
    for name, column in POINT_COLUMN.items():
        if any(row[column] == point for row in read_rows(Path(out) / name, CSV_FIELDS[name])):
            found.append(Path(out) / name)
    return found


def remove_point(out, point):
    """Removes only this point's fingerprints and CSV rows (--force)."""
    for path in sorted(Path(out, "fingerprints").glob(f"{point}_T*.json")) if Path(out, "fingerprints").is_dir() else []:
        path.unlink()
    for name, column in POINT_COLUMN.items():
        target = Path(out) / name
        rows = read_rows(target, CSV_FIELDS[name])
        kept = [row for row in rows if row[column] != point]
        if len(kept) != len(rows):
            rewrite_rows(target, CSV_FIELDS[name], kept)


# ------------------------------------------------------------------------------------------ platform
def _read_text(path):
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _os_release(path):
    text = _read_text(path)
    if text is None:
        return None
    values = {}
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator and key in ("ID", "NAME", "VERSION", "VERSION_ID", "PRETTY_NAME"):
            values[key] = value.strip().strip("\"'")
    return values or None


def _cpu_info(proc_root):
    text = _read_text(Path(proc_root) / "cpuinfo")
    if text is None:
        return None
    info = {"cores": sum(1 for line in text.splitlines() if re.match(r"processor\s*:", line))}
    for line in text.splitlines():
        key, _, value = line.partition(":")
        key = key.strip()
        if key in ("model name", "Model", "Hardware", "CPU part", "Revision") and key not in info:
            info[key] = value.strip()
    return info


def _mem_total_kb(proc_root):
    for line in (_read_text(Path(proc_root) / "meminfo") or "").splitlines():
        if line.startswith("MemTotal:"):
            try:
                return int(line.split()[1])
            except (IndexError, ValueError):
                return None
    return None


def platform_info(platform_id, campaign, collection, collection_path, location, location_path, technology,
                  bundles, deployment, proc_root="/proc", os_release="/etc/os-release"):
    commit, _ = manifest._git_metadata(Path(__file__).resolve().parent.parent)
    return {
        "platform_id": platform_id, "campaign": campaign, "technology": technology, "created_utc": utc_now(),
        "hostname": socket.gethostname(), "os_release": _os_release(os_release), "kernel": platform.release(),
        "architecture": platform.machine(), "python": platform.python_version(),
        "software_version": software_version(), "git_commit": None if commit == "nao_disponivel" else commit,
        "cpu": _cpu_info(proc_root), "memory_total_kb": _mem_total_kb(proc_root),
        "tcc_location_conf": {"path": str(location_path), "sha256": sha256_file(location_path),
                              "values": {key: location.get(key) for key in config.FIELDS}},
        "collection_conf": {"path": str(collection_path), "sha256": sha256_file(collection_path)},
        "bundles": bundles, "deployment": deployment,
        "deployed_matches_complete": deployment["matches_complete"] if deployment else None,
    }


def deployment_info(results, technology, bundles):
    """Optional check of deployment.json; recorded, never fatal and never a fourth prediction."""
    directory = Path(results) / "modelos" / wknn.DEPLOY_DIR
    record_path = directory / "deployment.json"
    if not record_path.is_file():
        return None
    deployed = directory / f"wknn_{technology.lower()}.json"
    info = {"path": str(record_path), "sha256": sha256_file(record_path), "deployed_bundle": str(deployed),
            "deployed_sha256": sha256_file(deployed) if deployed.is_file() else None, "matches_complete": False}
    try:
        record = position.verify_deployment(deployed, record_path)
        info["selected_model"], info["check"] = record["selected_model"], "ok"
    except (OSError, ValueError) as exc:
        info["selected_model"], info["check"] = None, str(exc)
    complete = bundles.get("complete")
    info["matches_complete"] = bool(complete and info["deployed_sha256"] == complete["sha256"]
                                    and info["selected_model"] == "complete")
    return info


# ------------------------------------------------------------------------------------------ context
def parse_models(text):
    models = [item.strip() for item in str(text).split(",") if item.strip()]
    if not models or len(set(models)) != len(models) or any(model not in MODELS for model in models):
        raise ValueError(f"--models invalido: use uma lista sem repeticoes de {', '.join(MODELS)}")
    return tuple(models)


def load_models(results, models, collection):
    """Loads each bundle once (timed) and validates them before any hardware access."""
    loaded, technology = {}, None
    for name in models:
        path = next((candidate for candidate in (wknn.artifact_paths(results, tech, name)["model"]
                                                 for tech in ("LTE", "NR")) if candidate.is_file()), None)
        if path is None:
            raise ValidationError(f"modelo {name} nao treinado: {wknn.artifact_paths(results, 'LTE', name)['model']}; "
                                  "execute 'campaign_tools.wknn train'")
        start = time.perf_counter()
        try:
            model = load_bundle(str(path))
        except (BundleError, OSError) as exc:
            raise ValidationError(f"bundle {name} invalido: {exc}") from exc
        load_s = time.perf_counter() - start
        bundle = model.bundle
        if bundle["protocol"] != name:
            raise ValidationError(f"{path}: protocolo {bundle['protocol']!r}, esperado {name!r}")
        if technology not in (None, model.technology):
            raise ValidationError(f"bundles com tecnologias diferentes: {technology} e {model.technology}")
        technology = model.technology
        source = bundle.get("dataset", {}).get("source_db")
        if source and source != Path(collection["db"]).name:
            raise ValidationError(f"{path}: treinado com {source}, a campanha usa {Path(collection['db']).name}")
        loaded[name] = {"model": model, "path": str(path), "sha256": sha256_file(path), "load_s": load_s,
                        "dataset_hashes": {key: bundle["dataset"].get(key) for key in
                                           ("samples_sha256", "training_samples_sha256", "test_samples_sha256")}}
    return loaded, technology


def _manifest_values(results):
    values = {}
    for line in (_read_text(Path(results) / "manifesto_campanha.txt") or "").splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key.strip()] = value.strip()
    return values


def build_protocol(args, collection, platform_id, technology, models):
    info = _manifest_values(collection["results"])
    return {
        "campaign": collection["name"], "points": collection["points"], "technology": technology,
        "true_reference_source": "canonical_S1", "models": list(models), "repetitions": args.repetitions,
        "stabilization_s": args.stabilization_s, "between_repetitions_s": args.between_repetitions_s,
        "metrics_interval_s": args.metrics_interval_s, "one_snapshot_per_repetition": True,
        "one_csurv_per_repetition": True, "snapshot_shared_between_models": True, "created_utc": utc_now(),
        "software_version": software_version(), "platform_id": platform_id,
        "antenna_height_m": info.get("antenna_height_m"), "antenna_orientation": info.get("antenna_orientation"),
        "instruction": "manter o equipamento imovel durante todo o ponto, na altura e orientacao da campanha",
        "prediction_order": {f"T{r}": prediction_order(r, models) for r in range(1, args.repetitions + 1)},
    }


def ensure_json(path, data, compat, label):
    """Creates the file once; an existing incompatible file is never overwritten."""
    if not Path(path).exists():
        write_json(path, data)
        return
    try:
        existing = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValidationError(f"{path}: {label} ilegivel ({exc})") from exc
    differences = [key for key in compat if existing.get(key) != data.get(key)]
    if differences:
        raise ValidationError(f"{path}: {label} incompativel com esta execucao ({', '.join(differences)}); "
                              "use outro --output ou remova a validacao anterior")


# ----------------------------------------------------------------------------------------- one point
def _count(observations, rat):
    items = [o for o in observations if o.rat == rat]
    return len(items), len({o.radio_key for o in items if o.radio_key})


def acquisition_row(platform_id, campaign, point, repetition, snapshot_id, snapshot, repetition_s):
    lte, lte_keys = _count(snapshot.observations, "LTE")
    nr, nr_keys = _count(snapshot.observations, "NR")
    timing = snapshot.timings_s
    return {
        "platform": platform_id, "campaign": campaign, "point": point, "repetition": repetition,
        "snapshot_id": snapshot_id, "started_at": snapshot.started_at, "snapshot_status": snapshot.status,
        "reasons": ";".join(snapshot.reasons), "access_mode": snapshot.access_mode,
        "total_s": timing.get("total", ""), "moni_s": timing.get("MONI", ""), "servinfo_s": timing.get("SERVINFO", ""),
        "csurv_s": timing.get("CSURV", ""), "gnss_s": timing.get("GNSS", ""),
        "n_observations": len(snapshot.observations), "n_lte_observations": lte, "n_nr_observations": nr,
        "n_lte_radio_keys": lte_keys, "n_nr_radio_keys": nr_keys, "serving_radio_key": snapshot.serving.radio_key or "",
        "serving_global_cell_key": snapshot.serving.global_cell_key or "",
        "transport_error": snapshot.transport_error or "", "anomaly_count": len(snapshot.anomalies),
        "repetition_s": f"{repetition_s:.6f}",
    }


def prediction_row(platform_id, campaign, point, repetition, snapshot_id, name, loaded, estimate, prediction_s, truth):
    meta = estimate.metadata or {}
    row = {
        "platform": platform_id, "campaign": campaign, "true_point": point, "repetition": repetition,
        "snapshot_id": snapshot_id, "model": name, "model_protocol": loaded["model"].bundle["protocol"],
        "bundle_sha256": loaded["sha256"], "valid": estimate.valid, "status": estimate.status,
        "reason": estimate.reason or "", "technology": estimate.technology or "",
        "snapshot_status": estimate.snapshot_status or "", "true_lat": truth["lat"], "true_lon": truth["lon"],
        "estimated_lat": "" if estimate.lat is None else estimate.lat,
        "estimated_lon": "" if estimate.lon is None else estimate.lon,
        "estimated_x_m": "" if estimate.x_m is None else estimate.x_m,
        "estimated_y_m": "" if estimate.y_m is None else estimate.y_m, "error_m": "",
        "n_observed_keys": meta.get("n_observed_keys", ""), "n_ignored_keys": meta.get("n_ignored_keys", ""),
        "n_neighbors": meta.get("n_neighbors", ""), "distance_metric": meta.get("distance_metric", ""),
        "zero_distance": meta.get("zero_distance", ""),
        "neighbor_measurement_ids": ";".join(str(i) for i in meta.get("neighbor_measurement_ids", [])),
        "model_load_s": f"{loaded['load_s']:.6f}", "prediction_s": f"{prediction_s:.6f}",
    }
    if estimate.valid and estimate.lat is not None and estimate.lon is not None:
        try:
            row["error_m"] = distance_m(truth["lat"], truth["lon"], estimate.lat, estimate.lon)
        except ValueError as exc:
            row["reason"] = f"erro nao calculado: {exc}"
    return row


def failure_row(point, repetition, snapshot_id, stage, status, reason, model=""):
    return {"point": point, "repetition": repetition, "snapshot_id": snapshot_id, "stage": stage, "model": model,
            "status": status, "reason": reason, "timestamp_utc": utc_now()}


def _record_failure(out, row):
    try:
        append_rows(Path(out) / "failures.csv", FAILURE_FIELDS, [row])
    except OSError:
        pass


def run_repetition(ctx, repetition, snapshot_fn, models):
    """One snapshot, shared by every model. Returns (prediction rows, snapshot), or None if nothing was acquired."""
    out, point, platform_id = ctx["out"], ctx["point"], ctx["platform_id"]
    started = time.perf_counter()
    driver = FN990Driver()
    try:
        driver.connect(ctx["port"])
    except (ConnectionError, OSError) as exc:
        _record_failure(out, failure_row(point, repetition, "", "connect", "unavailable", str(exc)))
        return None
    ctx["sampler"].set(state="acquiring")
    try:
        snapshot = snapshot_fn(driver)
    except (OSError, RuntimeError) as exc:
        _record_failure(out, failure_row(point, repetition, "", "snapshot", "error", str(exc)))
        return None
    finally:
        driver.close()

    snapshot_id = f"{platform_id}:{ctx['campaign']}:{point}:T{repetition}:{snapshot.started_at}"
    ctx["sampler"].set(state="predicting")
    estimates = []
    for name in prediction_order(repetition, tuple(models)):
        begin = time.perf_counter()
        estimate = estimate_from_snapshot(snapshot, model=models[name]["model"])
        estimates.append((name, estimate, time.perf_counter() - begin))
    repetition_s = time.perf_counter() - started
    failures = []
    if snapshot.status == SNAPSHOT_INVALID or snapshot.transport_error:
        failures.append(failure_row(point, repetition, snapshot_id, "snapshot", snapshot.status,
                                    snapshot.transport_error or ";".join(snapshot.reasons)))
    failures += [failure_row(point, repetition, snapshot_id, name, est.status, est.reason or "", name)
                 for name, est, _ in estimates if not est.valid]
    rows = [prediction_row(platform_id, ctx["campaign"], point, repetition, snapshot_id, name, models[name], est,
                           seconds, ctx["truth"]) for name, est, seconds in estimates]
    try:
        write_json(Path(out) / "fingerprints" / f"{point}_T{repetition}.json",
                   {"snapshot_id": snapshot_id, "point": point, "repetition": repetition, **to_jsonable(snapshot)})
        append_rows(Path(out) / "acquisitions.csv", ACQUISITION_FIELDS,
                    [acquisition_row(platform_id, ctx["campaign"], point, repetition, snapshot_id, snapshot,
                                     repetition_s)])
        append_rows(Path(out) / "predictions.csv", PREDICTION_FIELDS, rows)
        if failures:
            append_rows(Path(out) / "failures.csv", FAILURE_FIELDS, failures)
    except OSError as exc:
        _record_failure(out, failure_row(point, repetition, snapshot_id, "write", "error", str(exc)))
        raise ValidationError(f"erro de escrita em T{repetition}: {exc}; dados ja gravados foram preservados") from exc
    return rows, snapshot


def run_point(args, input_fn=input, sleep=time.sleep, snapshot_fn=None):
    snapshot_fn = snapshot_fn or take_snapshot
    collection, location = wknn.load_context(args.campaign, args.config)
    location_path = config.find_config(args.config)
    if location_path is None or not location.get("port"):
        raise ValidationError("tcc-location.conf ausente ou sem port; execute campaign_tools.configure")
    point = args.point
    if point not in collection["points"]:
        raise ValidationError(f"{point} fora da campanha ({', '.join(collection['points'])})")
    models_wanted = parse_models(args.models)
    results = Path(collection["results"])
    out = Path(args.output).expanduser() if args.output else results / VALIDATION_DIR
    truth = read_canonical(collection["canonical"]).get(point)
    if truth is None:
        raise ValidationError(f"{point}: ausente em {collection['canonical']}")
    loaded, technology = load_models(results, models_wanted, collection)
    existing = point_artifacts(out, point)
    if existing and not args.force:
        raise ValidationError(f"{point}: ja ha artefatos (possivelmente parciais) em {out}; "
                              "use --force para refazer somente este ponto")

    prompt = (f"Equipamento imovel em {point}, na altura e orientacao da campanha.\nDigite OK para iniciar: ")
    if input_fn(prompt).strip() != "OK":
        raise ValidationError("validacao cancelada; nenhum artefato do ponto foi criado")
    out.mkdir(parents=True, exist_ok=True)
    if existing:
        remove_point(out, point)
    platform_id = location["system"]
    bundles = {name: {key: item[key] for key in ("path", "sha256", "dataset_hashes")} for name, item in loaded.items()}
    deployment = deployment_info(results, technology, bundles)
    ensure_json(out / "platform.json", platform_info(
        platform_id, collection["name"], collection, prepare.resolve_collection(args.campaign, location.get("data_dir")),
        location, location_path, technology, bundles, deployment),
        ("platform_id", "campaign", "technology", "bundles"), "platform.json")
    ensure_json(out / "protocol.json", build_protocol(args, collection, platform_id, technology, models_wanted),
                COMPAT_PROTOCOL, "protocol.json")

    ctx = {"out": out, "point": point, "platform_id": platform_id, "campaign": collection["name"],
           "port": location["port"], "truth": truth}
    produced = []
    sampler = monitor.ProcessSampler(collection["db"], str(results), out / "system_metrics.csv", MODE, point,
                                     interval_s=args.metrics_interval_s)
    ctx["sampler"] = sampler
    sampler.set(state="stabilizing", session="T1")
    sampler.start()
    final_state = "failed"
    try:
        sleep(args.stabilization_s)
        for repetition in range(1, args.repetitions + 1):
            sampler.set(session=f"T{repetition}")
            if repetition > 1:
                sampler.set(state="stabilizing")
                sleep(args.between_repetitions_s)
            result = run_repetition(ctx, repetition, snapshot_fn, loaded)
            if result is not None:
                produced.append(result)
        final_state = "completed"
    finally:
        sampler.stop(final_state)
    summary_text(point, produced, out, collection)
    return produced


def summary_text(point, produced, out, collection):
    statuses = Counter(snapshot.status for _, snapshot in produced)
    print(f"Ponto {point} concluido")
    print(f"Snapshots: {statuses['complete']} complete, {statuses['partial']} partial, {statuses['invalid']} invalid")
    errors = {}
    for rows, _ in produced:
        for row in rows:
            errors.setdefault(row["model"], []).append(row)
    for name in MODELS:
        if name in errors:
            rows = errors[name]
            values = [row["error_m"] for row in rows if row["error_m"] != ""]
            mean = f"{statistics.fmean(values):.2f} m" if values else "-"
            print(f"\n{name.capitalize()}: {len(values)}/{len(rows)} validas; erro medio {mean}")
    print(f"\nArtefatos:\n{out}")
    points = collection["points"]
    following = points[points.index(point) + 1] if points.index(point) + 1 < len(points) else None
    if following:
        print(f"\nProximo ponto:\n  python -m campaign_tools.validate point --campaign {collection['name']} "
              f"--point {following}")
    else:
        print(f"\nProximo passo:\n  python -m campaign_tools.validate summarize --campaign {collection['name']}")


# ---------------------------------------------------------------------------------------- summarize
def _num(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _stats(values, prefix=""):
    values = [v for v in values if v is not None]
    if not values:
        return {f"{prefix}n": 0, f"{prefix}mean": None, f"{prefix}median": None, f"{prefix}p95": None,
                f"{prefix}max": None, f"{prefix}min": None}
    ordered = sorted(values)
    return {f"{prefix}n": len(values), f"{prefix}mean": statistics.fmean(values),
            f"{prefix}median": statistics.median(values), f"{prefix}p95": percentile(ordered, 95),
            f"{prefix}max": ordered[-1], f"{prefix}min": ordered[0]}


def _read_json(path, problems):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        problems.append(f"{Path(path).name}: ausente ou invalido ({exc})")
        return None


def model_summary(rows):
    total = len(rows)
    valid = [row for row in rows if row["valid"] == "True"]
    errors = [_num(row["error_m"]) for row in valid]
    stats = error_summary([e for e in errors if e is not None], n_total=total)
    milliseconds = [(_num(row["prediction_s"]) or 0) * 1000 for row in rows if _num(row["prediction_s"]) is not None]
    return {
        "predictions": total, "valid": len(valid), "failures": total - len(valid),
        "coverage": len(valid) / total if total else None,
        "status_counts": dict(sorted(Counter(row["status"] for row in rows).items())),
        "mean_m": stats["mean_m"], "median_m": stats["median_m"], "rmse_m": stats["rmse_m"],
        "p90_m": stats["p90_m"], "p95_m": stats["p95_m"], "max_m": stats["max_m"],
        "mean_prediction_ms": statistics.fmean(milliseconds) if milliseconds else None,
        "p95_prediction_ms": percentile(sorted(milliseconds), 95) if milliseconds else None,
        "mean_observed_keys": _mean([_num(row["n_observed_keys"]) for row in rows]),
        "mean_ignored_keys": _mean([_num(row["n_ignored_keys"]) for row in rows]),
    }


def _mean(values):
    values = [v for v in values if v is not None]
    return statistics.fmean(values) if values else None


def dispersion_m(rows):
    """Mean distance (m) of the valid estimated positions to their centroid; None with fewer than 2."""
    positions = [(_num(r["estimated_lat"]), _num(r["estimated_lon"])) for r in rows if r["valid"] == "True"]
    positions = [p for p in positions if None not in p]
    if len(positions) < 2:
        return None
    lat, lon = statistics.fmean(p[0] for p in positions), statistics.fmean(p[1] for p in positions)
    return statistics.fmean(haversine_m(lat, lon, p[0], p[1]) for p in positions)


def point_summary(rows):
    valid = [row for row in rows if row["valid"] == "True"]
    errors = [e for e in (_num(row["error_m"]) for row in valid) if e is not None]
    return {
        "repetitions": len(rows), "valid": len(valid), "coverage": len(valid) / len(rows) if rows else None,
        "mean_error_m": statistics.fmean(errors) if errors else None, "max_error_m": max(errors) if errors else None,
        "dispersion_m": dispersion_m(rows),
        "status_counts": dict(sorted(Counter(row["status"] for row in rows).items())),
    }


def acquisition_summary(rows):
    result = {"snapshots": len(rows), "status_counts": dict(sorted(Counter(r["snapshot_status"] for r in rows).items()))}
    for key in ("total_s", "moni_s", "servinfo_s", "csurv_s", "gnss_s"):
        result[key] = _stats([_num(r[key]) for r in rows])
    for key in ("n_lte_observations", "n_nr_observations"):
        values = [_num(r[key]) for r in rows]
        result[key] = {"total": sum(v for v in values if v is not None), "mean": _mean(values)}
    result["anomalies"] = sum(_num(r["anomaly_count"]) or 0 for r in rows)
    result["transport_errors"] = sum(1 for r in rows if r["transport_error"])
    return result


def system_summary(rows, limitations):
    spec = {"cpu_total_pct": "cpu_total", "cpu_process_pct": "cpu_process", "rss_kb": "rss", "hwm_kb": "hwm",
            "memory_available_kb": "memory_available", "temperature_c": "temperature", "disk_free_bytes": "disk_free"}
    result = {"samples": len(rows)}
    for column, name in spec.items():
        stats = _stats([_num(r[column]) for r in rows])
        result[name] = stats
        if not stats["n"]:
            limitations.append(f"{column}: sem amostras em system_metrics.csv")
    return result


def run_summarize(args):
    collection, _ = wknn.load_context(args.campaign, args.config)
    results = Path(collection["results"])
    out = Path(args.output).expanduser() if args.output else results / VALIDATION_DIR
    if not out.is_dir():
        raise ValidationError(f"{out}: validacao ausente; execute 'campaign_tools.validate point' antes")
    problems, limitations = [], []
    platform_data = _read_json(out / "platform.json", problems)
    protocol = _read_json(out / "protocol.json", problems) or {}
    models = tuple(protocol.get("models", ())) or MODELS
    repetitions = protocol.get("repetitions")
    if not isinstance(repetitions, int) or repetitions < 1:
        problems.append("protocol.json: repeticoes ausentes ou invalidas")
        repetitions = 0
    try:
        tables = {name: read_rows(out / name, fields) for name, fields in CSV_FIELDS.items()}
    except ValidationError as exc:
        raise ValidationError(f"CSV incoerente: {exc}") from exc
    acquisitions, predictions = tables["acquisitions.csv"], tables["predictions.csv"]
    points = collection["points"]

    by_key = {}
    for row in acquisitions:
        by_key.setdefault((row["point"], row["repetition"]), []).append(row)
    for point in points:
        for repetition in range(1, repetitions + 1):
            rows = by_key.get((point, str(repetition)), [])
            if not rows:
                problems.append(f"{point}/T{repetition}: aquisicao ausente")
            elif len(rows) > 1:
                problems.append(f"{point}/T{repetition}: aquisicao duplicada ({len(rows)} linhas)")
    for (point, repetition) in sorted(by_key):
        if point not in points or not repetition.isdigit() or not 1 <= int(repetition) <= repetitions:
            problems.append(f"{point}/T{repetition}: aquisicao fora do plano")
    snapshot_ids = Counter(row["snapshot_id"] for row in acquisitions)
    problems += [f"snapshot_id duplicado: {sid}" for sid, n in snapshot_ids.items() if n > 1]
    for row in acquisitions:
        fingerprint = out / "fingerprints" / f"{row['point']}_T{row['repetition']}.json"
        data = _read_json(fingerprint, problems)
        if data is not None and data.get("snapshot_id") != row["snapshot_id"]:
            problems.append(f"{fingerprint.name}: snapshot_id diferente de acquisitions.csv")
    expected_ids = {row["snapshot_id"]: (row["point"], row["repetition"]) for row in acquisitions}
    per_snapshot = {}
    for row in predictions:
        per_snapshot.setdefault(row["snapshot_id"], []).append(row)
        if row["snapshot_id"] not in expected_ids:
            problems.append(f"predicao com snapshot_id desconhecido: {row['snapshot_id']}")
        elif expected_ids[row["snapshot_id"]] != (row["true_point"], row["repetition"]):
            problems.append(f"{row['snapshot_id']}: ponto/repeticao divergem de acquisitions.csv")
    for snapshot_id in expected_ids:
        names = Counter(row["model"] for row in per_snapshot.get(snapshot_id, []))
        if set(names) != set(models):
            problems.append(f"{snapshot_id}: modelos {sorted(names)} diferem de {sorted(models)}")
        problems += [f"{snapshot_id}: predicao duplicada do modelo {name}" for name, n in names.items() if n > 1]

    by_model = {name: model_summary([r for r in predictions if r["model"] == name]) for name in models}
    by_point = {point: {name: point_summary([r for r in predictions if r["true_point"] == point and r["model"] == name])
                        for name in models} for point in points}
    summary = {
        "campaign": collection["name"], "created_utc": utc_now(), "platform_id": (platform_data or {}).get("platform_id"),
        "points": points, "models": list(models), "repetitions": repetitions,
        "snapshots": len(acquisitions), "predictions": len(predictions),
        "expected_snapshots": len(points) * repetitions, "expected_predictions": len(points) * repetitions * len(models),
        "problems": problems, "ready_for_comparison": not problems, "by_model": by_model, "by_point": by_point,
        "acquisition": acquisition_summary(acquisitions), "system": system_summary(tables["system_metrics.csv"], limitations),
        "limitations": limitations,
    }
    model_fields = ("model", "predictions", "valid", "failures", "coverage", "status_counts", "mean_m", "median_m",
                    "rmse_m", "p90_m", "p95_m", "max_m", "mean_prediction_ms", "p95_prediction_ms",
                    "mean_observed_keys", "mean_ignored_keys")
    point_fields = ("point", "model", "repetitions", "valid", "coverage", "mean_error_m", "max_error_m",
                    "dispersion_m", "status_counts")
    rewrite_rows(out / "summary_by_model.csv", model_fields, [
        {"model": name, **{k: _cell(v) for k, v in item.items()}} for name, item in by_model.items()])
    rewrite_rows(out / "summary_by_point.csv", point_fields, [
        {"point": point, "model": name, **{k: _cell(v) for k, v in item.items()}}
        for point, items in by_point.items() for name, item in items.items()])
    write_json(out / "validation_summary.json", summary, replace=True)
    print(f"Validacao: {len(points)} pontos, {len(acquisitions)} snapshots, {len(predictions)} predicoes")
    print(f"Resumo salvo em {out}")
    for problem in problems:
        print(f"PROBLEMA: {problem}", file=sys.stderr)
    for limitation in limitations:
        print(f"LIMITACAO: {limitation}", file=sys.stderr)
    print(f"Pronto para comparacao com Buildroot: {'sim' if summary['ready_for_comparison'] else 'nao'}")
    return summary


def _cell(value):
    if isinstance(value, dict):
        return ";".join(f"{k}={v}" for k, v in value.items())
    return "" if value is None else value


# ------------------------------------------------------------------------------------------------ CLI
def _positive_int(text):
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("precisa ser inteiro positivo")
    return value


def _non_negative(text):
    value = float(text)
    if not math.isfinite(value) or value < 0:
        raise argparse.ArgumentTypeError("precisa ser numero nao negativo")
    return value


def _positive(text):
    value = _non_negative(text)
    if value <= 0:
        raise argparse.ArgumentTypeError("precisa ser numero positivo")
    return value


def _main(argv=None):
    parser = argparse.ArgumentParser(description="Validacao pratica dos modelos WKNN (desenvolvimento).")
    sub = parser.add_subparsers(dest="command", required=True)
    point = sub.add_parser("point", help="repete um snapshot compartilhado por modelo em um ponto")
    summary = sub.add_parser("summarize", help="consolida os artefatos de validation sem acessar hardware")
    for item in (point, summary):
        item.add_argument("--campaign", required=True, help="nome da coleta ou collection.conf")
        item.add_argument("--config", help="arquivo tcc-location.conf")
        item.add_argument("--output", help="diretorio de validation (padrao: <results>/avaliacoes/validation)")
    point.add_argument("--point", required=True, choices=prepare.VALID_POINTS)
    point.add_argument("--repetitions", type=_positive_int, default=3)
    point.add_argument("--models", default=",".join(MODELS), help="lista (padrao: spatial,temporal,complete)")
    point.add_argument("--stabilization-s", type=_non_negative, default=60)
    point.add_argument("--between-repetitions-s", type=_non_negative, default=10)
    point.add_argument("--metrics-interval-s", type=_positive, default=1)
    point.add_argument("--force", action="store_true", help="substitui somente os artefatos deste ponto")
    args = parser.parse_args(argv)
    try:
        if args.command == "point":
            run_point(args)
        else:
            return 0 if run_summarize(args)["ready_for_comparison"] else 1
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
