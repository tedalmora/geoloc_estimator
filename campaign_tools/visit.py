"""Supervised one-point campaign visit orchestration using existing HAL and collector CLIs."""

import argparse
import csv
import json
import math
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from coletor import db as coletor_db
from estimador.geometry import validate_lat_lon

from . import monitor
from .reference import (
    FIX_COUNT, MAX_ATTEMPTS, add_canonical, compare_reference, distance_m, parse_fix,
    read_canonical, summarize_fixes,
)

SUMMARY_FIELDS = (
    "system", "point", "session", "attempts", "valid_fixes", "mean_lat", "mean_lon",
    "median_lat", "median_lon", "mean_median_distance_m", "mean_dispersion_m",
    "max_dispersion_m", "stddev_distance_m", "fix_interval_s", "interval_basis",
    "hdop_values", "mean_hdop", "nsat_values", "mean_nsat", "altitude_values_m",
    "phone_distance_m", "phone_accuracy_m", "phone_app", "phone_time",
    "canonical_distance_m", "review_threshold_m", "review_required",
)
FIX_FIELDS = (
    "attempt", "capture_utc", "valid", "reason", "lat", "lon", "alt_m", "fix_type",
    "hdop", "nsat", "gnss_utc", "distance_to_mean_m",
)


class InsufficientFixesError(RuntimeError):
    def __init__(self, valid_count, records):
        self.records = records
        super().__init__(
            f"obtidos {valid_count} de {FIX_COUNT} fixes válidos em {MAX_ATTEMPTS} tentativas; "
            "não iniciar coleta neste ponto"
        )


def collect_valid_fixes(port, command_runner=subprocess.run, sleep=time.sleep):
    fixes = []
    records = []
    for attempt in range(1, MAX_ATTEMPTS + 1):
        captured = datetime.now(timezone.utc).isoformat(timespec="seconds")
        command = [sys.executable, "-m", "hal.cli", "--port", port, "gnss"]
        result = command_runner(command, capture_output=True, text=True, check=False)
        reading = None
        reason = ""
        if result.returncode == 0:
            try:
                reading = json.loads(result.stdout)
                fix = parse_fix(reading, result.returncode)
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                reason = str(exc)
            else:
                fix.update({"attempt": attempt, "capture_utc": captured})
                fixes.append(fix)
        else:
            reason = f"hal_exit_code_{result.returncode}"
        record = {
            "attempt": attempt, "capture_utc": captured, "valid": bool(reading is not None and
                result.returncode == 0 and not reason), "reason": reason,
        }
        if record["valid"]:
            record.update(fix)
        else:
            record.update({field: "" for field in FIX_FIELDS if field not in record})
        records.append(record)
        if len(fixes) == FIX_COUNT:
            return fixes, records
        if attempt < MAX_ATTEMPTS:
            sleep(10)
    raise InsufficientFixesError(len(fixes), records)


def _read_point(db_path, point):
    path = Path(db_path)
    if not path.is_file() or path.stat().st_size == 0:
        return None
    conn = coletor_db.open_read_only(str(path))
    try:
        version, tables = coletor_db.schema_info(conn)
        if version != coletor_db.SCHEMA_VERSION or "reference_points" not in tables:
            raise ValueError(f"{path}: schema incompatível ao verificar {point}")
        row = conn.execute(
            "SELECT reference_point_id, lat, lon, coord_uncertainty_m "
            "FROM reference_points WHERE name=?", (point,)
        ).fetchone()
        return None if row is None else {
            "id": row[0], "lat": row[1], "lon": row[2], "uncertainty_m": row[3],
        }
    finally:
        conn.close()


def _max_session_id(db_path):
    path = Path(db_path)
    if not path.is_file() or path.stat().st_size == 0:
        return 0
    conn = coletor_db.open_read_only(str(path))
    try:
        version, tables = coletor_db.schema_info(conn)
        if version != coletor_db.SCHEMA_VERSION or "sessions" not in tables:
            raise ValueError(f"{path}: schema incompatível ao verificar sessões")
        return conn.execute("SELECT COALESCE(MAX(session_id), 0) FROM sessions").fetchone()[0]
    finally:
        conn.close()


def _register_reference(db_path, point, reference, description):
    existing = _read_point(db_path, point)
    if existing is not None and (existing["lat"] is not None or existing["lon"] is not None):
        if (existing["lat"] is None or existing["lon"] is None or
                not math.isclose(existing["lat"], reference["lat"], abs_tol=1e-9) or
                not math.isclose(existing["lon"], reference["lon"], abs_tol=1e-9) or
                existing["uncertainty_m"] is None or
                not math.isclose(existing["uncertainty_m"], reference["uncertainty_m"], abs_tol=1e-6)):
            raise ValueError(
                f"{point}: coordenada/incerteza do banco diverge da referência canônica; não será substituída"
            )
        return existing["id"]
    command = [
        sys.executable, "-m", "coletor.daemon", "point-add", "--db", str(db_path),
        "--lat", str(reference["lat"]), "--lon", str(reference["lon"]),
        "--coord-source", "gnss_media_s1", "--uncertainty-m", str(reference["uncertainty_m"]),
        "--description", description, point,
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError(f"falha ao cadastrar referência {point}: {result.stderr.strip()}")
    registered = _read_point(db_path, point)
    if registered is None or registered["lat"] is None:
        raise RuntimeError(f"coletor não confirmou o cadastro da referência {point}")
    return registered["id"]


def _check_summary_target(path, system, point, session):
    target = Path(path)
    if not target.exists():
        return
    with target.open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != list(SUMMARY_FIELDS):
            raise ValueError(f"{target}: cabeçalho de resumo incompatível")
        if any(row["system"] == system and row["point"] == point and row["session"] == session
               for row in reader):
            raise FileExistsError(f"{target}: visita {system}/{point}/{session} já registrada")


def _write_visit_results(results_dir, system, point, session, attempts, fixes, summary, comparison, phone):
    references_dir = results_dir / "referencias"
    references_dir.mkdir(parents=True, exist_ok=True)
    fix_path = references_dir / f"{point}_{session}_fixes.csv"
    summary_path = results_dir / "resumo_referencia.csv"
    _check_summary_target(summary_path, system, point, session)
    with fix_path.open("x", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIX_FIELDS)
        writer.writeheader()
        mean = (summary["mean_lat"], summary["mean_lon"])
        for row in attempts:
            output = {key: row.get(key, "") for key in FIX_FIELDS}
            if row["valid"]:
                output["distance_to_mean_m"] = f"{distance_m(mean[0], mean[1], row['lat'], row['lon']):.3f}"
            writer.writerow(output)

    new_file = not summary_path.exists()
    row = {
        "system": system, "point": point, "session": session, "attempts": len(attempts),
        "valid_fixes": len(fixes), "mean_lat": f"{summary['mean_lat']:.10f}",
        "mean_lon": f"{summary['mean_lon']:.10f}", "median_lat": f"{summary['median_lat']:.10f}",
        "median_lon": f"{summary['median_lon']:.10f}",
        "mean_median_distance_m": f"{summary['mean_median_distance_m']:.3f}",
        "mean_dispersion_m": f"{summary['mean_dispersion_m']:.3f}",
        "max_dispersion_m": f"{summary['max_dispersion_m']:.3f}",
        "stddev_distance_m": f"{summary['stddev_distance_m']:.3f}",
        "fix_interval_s": "" if summary["fix_interval_s"] is None else f"{summary['fix_interval_s']:.3f}",
        "interval_basis": summary["interval_basis"],
        "hdop_values": ";".join(f"{value:g}" for value in summary["hdop_values"]),
        "mean_hdop": "" if summary["mean_hdop"] is None else f"{summary['mean_hdop']:.3f}",
        "nsat_values": ";".join(str(value) for value in summary["nsat_values"]),
        "mean_nsat": "" if summary["mean_nsat"] is None else f"{summary['mean_nsat']:.2f}",
        "altitude_values_m": ";".join(f"{value:g}" for value in summary["altitude_values_m"]),
        "phone_distance_m": "" if phone is None else f"{phone['distance_m']:.3f}",
        "phone_accuracy_m": "" if phone is None else phone["accuracy_m"],
        "phone_app": "" if phone is None else phone["app"],
        "phone_time": "" if phone is None else phone["time"],
        "canonical_distance_m": "" if comparison is None else f"{comparison['distance_m']:.3f}",
        "review_threshold_m": "" if comparison is None else f"{comparison['threshold_m']:.3f}",
        "review_required": "" if comparison is None else str(comparison["review_required"]).lower(),
    }
    with summary_path.open("x" if new_file else "a", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=SUMMARY_FIELDS)
        if new_file:
            writer.writeheader()
        writer.writerow(row)
    return fix_path, summary_path


def _write_failed_attempts(results_dir, point, session, attempts):
    target = results_dir / "referencias" / f"{point}_{session}_fixes.csv"
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("x", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIX_FIELDS)
        writer.writeheader()
        for row in attempts:
            writer.writerow({key: row.get(key, "") for key in FIX_FIELDS})


def _phone_input(args):
    lat_value = getattr(args, "phone_lat", None)
    lon_value = getattr(args, "phone_lon", None)
    if (lat_value in (None, "")) != (lon_value in (None, "")):
        raise ValueError("para a conferência telefônica, informe latitude e longitude juntas")
    if lat_value in (None, ""):
        if any(getattr(args, name, None) not in (None, "") for name in
               ("phone_accuracy_m", "phone_app", "phone_time")):
            raise ValueError("os metadados telefônicos exigem latitude e longitude")
        return None
    try:
        lat, lon = float(lat_value), float(lon_value)
        validate_lat_lon(lat, lon)
        accuracy_value = getattr(args, "phone_accuracy_m", None)
        accuracy = None if accuracy_value in (None, "") else float(accuracy_value)
        if accuracy is not None and (not math.isfinite(accuracy) or accuracy < 0):
            raise ValueError("precisão do telefone deve ser finita e não negativa")
    except (TypeError, ValueError) as exc:
        raise ValueError("coordenadas/precisão telefônicas inválidas") from exc
    return {
        "lat": lat, "lon": lon,
        "accuracy_m": "" if accuracy is None else f"{accuracy:.3f}",
        "app": getattr(args, "phone_app", "") or "",
        "time": getattr(args, "phone_time", "") or "",
    }


def _phone_check(phone_input, summary):
    if phone_input is None:
        return None
    return {
        "distance_m": distance_m(
            summary["mean_lat"], summary["mean_lon"], phone_input["lat"], phone_input["lon"]
        ),
        "accuracy_m": phone_input["accuracy_m"],
        "app": phone_input["app"], "time": phone_input["time"],
    }


def _verify_collection(db_path, previous_session_id, expected_point_id):
    conn = coletor_db.open_read_only(str(db_path))
    try:
        sessions = conn.execute(
            "SELECT session_id, reference_point_id, status FROM sessions "
            "WHERE session_id > ? ORDER BY session_id", (previous_session_id,)
        ).fetchall()
        if len(sessions) != 1:
            raise RuntimeError(f"esperada uma sessão nova; encontradas {len(sessions)}")
        session_id, point_id, status = sessions[0]
        count = conn.execute(
            "SELECT COUNT(*) FROM measurements WHERE session_id=?", (session_id,)
        ).fetchone()[0]
        if point_id != expected_point_id or status != "completed" or count != 3:
            raise RuntimeError(
                f"sessão {session_id} incompleta: status={status}, medições={count}, ponto_id={point_id}"
            )
        return session_id, count
    finally:
        conn.close()


def run_visit(args, input_fn=input, sleep=time.sleep):
    phone_input = _phone_input(args)
    results_dir = Path(args.results)
    results_dir.mkdir(parents=True, exist_ok=True)
    canonical_path = Path(args.canonical) if args.canonical else results_dir / "referencias_canonicas.csv"
    fix_path = results_dir / "referencias" / f"{args.point}_{args.session}_fixes.csv"
    log_path = results_dir / "logs" / f"{args.point}_{args.session}.log"
    if fix_path.exists() or log_path.exists():
        raise FileExistsError("já há evidência para esta visita; use outro diretório de resultados")
    _check_summary_target(results_dir / "resumo_referencia.csv", args.system, args.point, args.session)

    is_primary_reference = args.system == "raspberrypi_os" and args.session == "S1"
    if is_primary_reference:
        if canonical_path.exists() and args.point in read_canonical(canonical_path):
            raise FileExistsError(f"{args.point}: referência canônica já existe; não será sobrescrita")
        canonical = None
    else:
        canonical = read_canonical(canonical_path).get(args.point)
        if canonical is None:
            raise ValueError(f"{args.point}: ausente em {canonical_path}; execute Raspberry Pi OS S1 primeiro")

    if args.system == "raspberrypi_os" and args.session == "S1":
        existing = _read_point(args.db, args.point)
        if existing is not None and (existing["lat"] is not None or existing["lon"] is not None):
            raise ValueError(f"{args.point}: o banco já contém coordenada; não será substituída")

    print(f"Imobilize o equipamento em {args.point}; iniciando estabilização de 60 s.")
    sleep(60)
    try:
        fixes, attempts = collect_valid_fixes(args.port)
    except InsufficientFixesError as exc:
        _write_failed_attempts(results_dir, args.point, args.session, exc.records)
        raise
    summary = summarize_fixes(fixes)
    phone = _phone_check(phone_input, summary)
    comparison = None if canonical is None else compare_reference(summary, canonical)
    if comparison and comparison["review_required"]:
        print(
            f"ALERTA: {comparison['distance_m']:.2f} m da referência, "
            f"limite de revisão {comparison['threshold_m']:.2f} m; avaliação manual obrigatória."
        )
    saved_fixes, saved_summary = _write_visit_results(
        results_dir, args.system, args.point, args.session, attempts, fixes, summary, comparison, phone,
    )
    print(f"Referência registrada em {saved_fixes} e {saved_summary}.")
    confirmation = input_fn(
        f"Confirme equipamento imóvel e referência {args.point} (digite OK para cadastrar/coletar): "
    ).strip()
    if confirmation != "OK":
        raise RuntimeError("visita cancelada; referências permanecem salvas e nenhuma coleta foi iniciada")

    if is_primary_reference:
        add_canonical(canonical_path, args.point, summary)
        canonical = {
            "point": args.point, "lat": summary["mean_lat"], "lon": summary["mean_lon"],
            "uncertainty_m": summary["max_dispersion_m"],
        }
    point_id = _register_reference(
        args.db, args.point, canonical, "Raspberry Pi OS S1 GNSS",
    )
    previous_session_id = _max_session_id(args.db)
    command = [
        sys.executable, "-m", "coletor.daemon", "continuous", "--port", args.port,
        "--db", args.db, "--scenario", "estatico", "--environment", "outdoor",
        "--reference-point", args.point, "--campaign-plmn", args.plmn,
        "--notes", f"campaign_tools:{args.system}:{args.point}:{args.session}",
        "--interval", str(args.interval_s), "--max-measurements", "3",
    ]
    data_dir = args.data_dir or str(Path(args.db).parent)
    return_code = monitor.run_collection(
        args.db, data_dir, results_dir / "metricas_sistema.csv", log_path,
        args.point, args.session, command,
    )
    session_id, count = _verify_collection(args.db, previous_session_id, point_id)
    if return_code != 0:
        raise RuntimeError(f"coletor encerrou com código {return_code}; sessão {session_id} verificada parcialmente")
    print(f"Visita concluída: sessão {session_id}, {count} medições.")
    return session_id


def _main(argv=None):
    parser = argparse.ArgumentParser(description="Executa uma visita supervisionada de um ponto da campanha.")
    parser.add_argument("--system", choices=("raspberrypi_os", "buildroot"), required=True)
    parser.add_argument("--point", choices=tuple(f"P{i:02d}" for i in range(1, 9)), required=True)
    parser.add_argument("--session", choices=("S1", "S2"), required=True)
    parser.add_argument("--db", required=True)
    parser.add_argument("--port", required=True)
    parser.add_argument("--plmn", required=True)
    parser.add_argument("--results", required=True)
    parser.add_argument("--canonical")
    parser.add_argument("--data-dir")
    parser.add_argument("--interval-s", type=float, default=240)
    parser.add_argument("--phone-lat")
    parser.add_argument("--phone-lon")
    parser.add_argument("--phone-accuracy-m")
    parser.add_argument("--phone-app")
    parser.add_argument("--phone-time")
    args = parser.parse_args(argv)
    if not math.isfinite(args.interval_s) or args.interval_s <= 0:
        parser.error("--interval-s precisa ser positivo")
    try:
        run_visit(args)
    except (EOFError, OSError, RuntimeError, ValueError) as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
