"""GNSS reference processing for field campaigns; this module never opens a modem or database."""

import argparse
import csv
import math
import statistics
from datetime import datetime
from pathlib import Path

from estimador.geometry import haversine_m, validate_lat_lon

FIX_COUNT = 5
MAX_ATTEMPTS = 8
POINTS = tuple(f"P{i:02d}" for i in range(1, 9))
CANONICAL_FIELDS = ("point", "lat", "lon", "uncertainty_m")


def parse_fix(reading, return_code=0):
    if return_code != 0 or not isinstance(reading, dict) or reading.get("status") != "ok":
        raise ValueError("HAL não retornou GNSS status=ok com código de saída zero")
    response = reading.get("response")
    fix = reading.get("fix")
    if not isinstance(response, dict) or response.get("final") != "OK":
        raise ValueError("resposta AT GNSS sem final=OK")
    if reading.get("anomalies") or not isinstance(fix, dict):
        raise ValueError("resposta GNSS contém anomalias ou não contém fix")
    fix_type = fix.get("fix_type")
    if isinstance(fix_type, bool) or not isinstance(fix_type, int) or fix_type not in (2, 3):
        raise ValueError("fix GNSS precisa ser 2D ou 3D")
    lat, lon = fix.get("lat"), fix.get("lon")
    for name, value in (("lat", lat), ("lon", lon)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{name} GNSS inválida")
    validate_lat_lon(lat, lon)
    for name in ("alt_m", "hdop"):
        value = fix.get(name)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, (int, float)) or
            not math.isfinite(value) or (name == "hdop" and value < 0)
        ):
            raise ValueError(f"{name} GNSS inválido")
    nsat = fix.get("nsat")
    if nsat is not None and (isinstance(nsat, bool) or not isinstance(nsat, int) or nsat < 0):
        raise ValueError("nsat GNSS inválido")
    utc = fix.get("utc")
    if utc is not None and not isinstance(utc, str):
        raise ValueError("UTC GNSS inválido")
    return {
        "lat": float(lat), "lon": float(lon), "alt_m": fix.get("alt_m"),
        "fix_type": fix_type, "hdop": fix.get("hdop"), "nsat": nsat, "gnss_utc": utc or "",
    }


def valid_fix(reading, return_code=0):
    try:
        parse_fix(reading, return_code)
    except (TypeError, ValueError):
        return False
    return True


def distance_m(a_lat, a_lon, b_lat, b_lon):
    validate_lat_lon(a_lat, a_lon)
    validate_lat_lon(b_lat, b_lon)
    return haversine_m(a_lat, a_lon, b_lat, b_lon)


def summarize_fixes(fixes):
    if len(fixes) != FIX_COUNT:
        raise ValueError(f"esperados exatamente {FIX_COUNT} fixes válidos")
    for fix in fixes:
        validate_lat_lon(fix["lat"], fix["lon"])
    mean_lat = statistics.fmean(fix["lat"] for fix in fixes)
    mean_lon = statistics.fmean(fix["lon"] for fix in fixes)
    median_lat = statistics.median(fix["lat"] for fix in fixes)
    median_lon = statistics.median(fix["lon"] for fix in fixes)
    distances = [distance_m(mean_lat, mean_lon, fix["lat"], fix["lon"]) for fix in fixes]
    hdop_values = [fix["hdop"] for fix in fixes if fix.get("hdop") is not None]
    nsat_values = [fix["nsat"] for fix in fixes if fix.get("nsat") is not None]
    gnss_times = [fix.get("gnss_utc") for fix in fixes]
    interval_basis = "gnss_utc" if all(gnss_times) else "capture_utc"
    time_values = gnss_times if interval_basis == "gnss_utc" else [fix.get("capture_utc") for fix in fixes]
    interval_s = None
    try:
        parsed_times = [datetime.fromisoformat(value.replace("Z", "+00:00")) for value in time_values]
        interval_s = (max(parsed_times) - min(parsed_times)).total_seconds()
    except (AttributeError, TypeError, ValueError):
        interval_basis = ""
    return {
        "mean_lat": mean_lat, "mean_lon": mean_lon,
        "median_lat": median_lat, "median_lon": median_lon,
        "mean_median_distance_m": distance_m(mean_lat, mean_lon, median_lat, median_lon),
        "mean_dispersion_m": statistics.fmean(distances),
        "max_dispersion_m": max(distances),
        "stddev_distance_m": statistics.stdev(distances),
        "hdop_values": hdop_values, "mean_hdop": statistics.fmean(hdop_values) if hdop_values else None,
        "nsat_values": nsat_values, "mean_nsat": statistics.fmean(nsat_values) if nsat_values else None,
        "altitude_values_m": [fix["alt_m"] for fix in fixes if fix.get("alt_m") is not None],
        "interval_basis": interval_basis, "fix_interval_s": interval_s, "fix_count": FIX_COUNT,
    }


def read_canonical(path):
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(
            f"referências canônicas ausentes: {source}; execute primeiro Raspberry Pi OS S1 e transfira o CSV"
        )
    references = {}
    with source.open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != list(CANONICAL_FIELDS):
            raise ValueError(f"{source}: cabeçalho inválido; esperado {','.join(CANONICAL_FIELDS)}")
        for row in reader:
            point = row["point"]
            if not point or point in references:
                raise ValueError(f"{source}: ponto ausente ou duplicado: {point!r}")
            try:
                lat, lon = float(row["lat"]), float(row["lon"])
                uncertainty = float(row["uncertainty_m"])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{source}: coordenada/ incerteza inválida para {point}") from exc
            validate_lat_lon(lat, lon)
            if not math.isfinite(uncertainty) or uncertainty < 0:
                raise ValueError(f"{source}: incerteza inválida para {point}")
            references[point] = {"point": point, "lat": lat, "lon": lon, "uncertainty_m": uncertainty}
    return references


def add_canonical(path, point, summary):
    references = read_canonical(path) if Path(path).exists() else {}
    if point in references:
        raise FileExistsError(f"{point}: referência canônica já existe; não será sobrescrita")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    new_file = not target.exists()
    with target.open("x" if new_file else "a", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=CANONICAL_FIELDS)
        if new_file:
            writer.writeheader()
        writer.writerow({
            "point": point, "lat": f"{summary['mean_lat']:.10f}",
            "lon": f"{summary['mean_lon']:.10f}",
            "uncertainty_m": f"{summary['max_dispersion_m']:.3f}",
        })


def compare_reference(summary, canonical, minimum_distance_m=20.0, spread_factor=3.0):
    distance = distance_m(summary["mean_lat"], summary["mean_lon"], canonical["lat"], canonical["lon"])
    threshold = max(minimum_distance_m, spread_factor * max(summary["max_dispersion_m"],
                                                             canonical["uncertainty_m"]))
    return {
        "distance_m": distance, "threshold_m": threshold,
        "review_required": distance > threshold,
    }


def separation_rows(references, minimum_distance_m=20.0, spread_factor=3.0):
    missing = sorted(set(POINTS) - set(references))
    if missing:
        raise ValueError(f"referências canônicas ausentes para: {', '.join(missing)}")
    rows = []
    for index, point_a in enumerate(POINTS):
        a = references[point_a]
        for point_b in POINTS[index + 1:]:
            b = references[point_b]
            distance = distance_m(a["lat"], a["lon"], b["lat"], b["lon"])
            spread = max(a["uncertainty_m"], b["uncertainty_m"])
            ratio = distance / spread if spread > 0 else None
            rows.append({
                "point_a": point_a, "point_b": point_b, "distance_m": distance,
                "max_uncertainty_m": spread, "distance_uncertainty_ratio": ratio,
                "below_minimum_distance": distance < minimum_distance_m,
                "below_uncertainty_margin": distance < spread_factor * spread,
            })
    return rows


def _main(argv=None):
    parser = argparse.ArgumentParser(description="Verifica a separação espacial das referências canônicas.")
    sub = parser.add_subparsers(dest="command", required=True)
    separation = sub.add_parser("separation", help="calcula distâncias entre P01 e P08")
    separation.add_argument("--canonical", required=True)
    separation.add_argument("--out", required=True)
    separation.add_argument("--minimum-distance-m", type=float, default=20.0)
    separation.add_argument("--spread-factor", type=float, default=3.0)
    args = parser.parse_args(argv)
    if (not math.isfinite(args.minimum_distance_m) or args.minimum_distance_m < 0 or
            not math.isfinite(args.spread_factor) or args.spread_factor <= 0):
        parser.error("limites precisam ser não negativos e o fator precisa ser positivo")
    rows = separation_rows(read_canonical(args.canonical), args.minimum_distance_m, args.spread_factor)
    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("x", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=tuple(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    for row in rows:
        if row["below_minimum_distance"] or row["below_uncertainty_margin"]:
            print(f"REVISAR {row['point_a']}-{row['point_b']}: {row['distance_m']:.2f} m")
    print(f"{len(rows)} pares analisados; decisão e justificativa são manuais: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
