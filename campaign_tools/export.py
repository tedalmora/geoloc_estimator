"""Read-only, privacy-conscious CSV export for campaign databases."""

import argparse
import csv
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from coletor.db import SCHEMA_VERSION, TABLES, open_read_only, schema_info
from estimador.dataset import load_dataset
from estimador.evaluate import cell_id_eligible, wknn_eligible

SESSION_FIELDS = (
    "session_id", "started_at", "ended_at", "status", "end_reason", "mode", "scenario",
    "environment", "reference_point_id", "campaign_plmn", "modem_id", "software_version",
    "interval_s",
)
MEASUREMENT_FIELDS = (
    "measurement_id", "session_id", "reference_point_id", "seq", "started_at",
    "snapshot_status", "reasons_json", "access_mode", "serving_method", "serving_rat",
    "serving_arfcn", "serving_pci", "serving_plmn", "serving_cell_id", "serving_tac",
    "serving_radio_key", "serving_global_cell_key", "serving_rsrp_dbm", "serving_rsrq_db",
    "serving_sinr_db", "moni_status", "servinfo_status", "csurv_status", "gnss_status",
    "gnss_fix_type", "gnss_hdop", "gnss_nsat", "gnss_utc", "lpp_probe_status",
    "timings_json", "anomalies_json", "transport_error",
)
CELL_FIELDS = (
    "measurement_id", "source", "rat", "arfcn", "pci", "plmn", "cell_id", "tac",
    "rsrp_dbm", "rsrq_db", "sinr_db", "rx_level_dbm", "cell_status", "band",
    "bandwidth", "scs_khz", "record_format", "is_serving", "radio_key",
    "global_cell_key", "identity_status",
)
HEALTH_FIELDS = ("health_check_id", "session_id", "checked_at", "modem_id", "event", "alive", "detail")
PERIODICITY_FIELDS = ("session_id", "previous_measurement_id", "measurement_id",
                      "previous_started_at", "started_at", "interval_s", "configured_interval_s")
AVAILABILITY_FIELDS = ("metric", "count", "attempts", "percentage")


def _validate_database(conn):
    version, tables = schema_info(conn)
    if version != SCHEMA_VERSION or not set(TABLES).issubset(tables):
        raise ValueError(f"esperado schema v{SCHEMA_VERSION} e tabelas completas; encontrado v{version}")


def _metadata(value, session_id):
    try:
        result = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"metadata_json inválido na sessão {session_id}") from exc
    if not isinstance(result, dict):
        raise ValueError(f"metadata_json da sessão {session_id} não é um objeto")
    return result


def _interval_seconds(previous, current):
    if not previous or not current:
        return None
    try:
        before = datetime.fromisoformat(previous.replace("Z", "+00:00"))
        after = datetime.fromisoformat(current.replace("Z", "+00:00"))
        return (after - before).total_seconds()
    except (AttributeError, TypeError, ValueError):
        return None


def _write_csv(directory, name, fields, rows):
    path = directory / name
    with path.open("x", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="raise")
        writer.writeheader()
        writer.writerows(rows)


def export_campaign(db_path, output_dir):
    attempts_dataset = load_dataset(str(db_path))
    with open_read_only(str(db_path)) as conn:
        _validate_database(conn)
        sessions = []
        for row in conn.execute(
            "SELECT session_id, started_at, ended_at, status, end_reason, mode, scenario, "
            "environment, reference_point_id, campaign_plmn, modem_id, metadata_json "
            "FROM sessions WHERE mode != 'legacy_v1' ORDER BY session_id"
        ):
            metadata = _metadata(row[11], row[0])
            sessions.append({
                "session_id": row[0], "started_at": row[1], "ended_at": row[2],
                "status": row[3], "end_reason": row[4], "mode": row[5], "scenario": row[6],
                "environment": row[7], "reference_point_id": row[8], "campaign_plmn": row[9],
                "modem_id": row[10], "software_version": metadata.get("software_version", ""),
                "interval_s": metadata.get("interval_s", ""),
            })
        measurement_sql = (
            "SELECT m.measurement_id, m.session_id, m.reference_point_id, m.seq, m.started_at, "
            "m.snapshot_status, m.reasons_json, m.access_mode, m.serving_method, m.serving_rat, "
            "m.serving_arfcn, m.serving_pci, m.serving_plmn, m.serving_cell_id, m.serving_tac, "
            "m.serving_radio_key, m.serving_global_cell_key, m.serving_rsrp_dbm, m.serving_rsrq_db, "
            "m.serving_sinr_db, m.moni_status, m.servinfo_status, m.csurv_status, m.gnss_status, "
            "m.gnss_fix_type, m.gnss_hdop, m.gnss_nsat, m.gnss_utc, m.lpp_probe_status, "
            "m.timings_json, m.anomalies_json, m.transport_error FROM measurements m "
            "JOIN sessions s ON s.session_id=m.session_id "
            "WHERE m.legacy_ambiguous=0 AND s.mode != 'legacy_v1' ORDER BY m.session_id, m.seq"
        )
        measurement_rows = [
            dict(zip(MEASUREMENT_FIELDS, row)) for row in conn.execute(measurement_sql)
        ]
        cells_sql = (
            "SELECT c.measurement_id, c.source, c.rat, c.arfcn, c.pci, c.plmn, c.cell_id, c.tac, "
            "c.rsrp_dbm, c.rsrq_db, c.sinr_db, c.rx_level_dbm, c.cell_status, c.band, c.bandwidth, "
            "c.scs_khz, c.record_format, c.is_serving, c.radio_key, c.global_cell_key, c.identity_status "
            "FROM observed_cells c JOIN measurements m ON m.measurement_id=c.measurement_id "
            "JOIN sessions s ON s.session_id=m.session_id "
            "WHERE m.legacy_ambiguous=0 AND s.mode != 'legacy_v1' ORDER BY c.observed_cell_id"
        )
        cell_rows = [dict(zip(CELL_FIELDS, row)) for row in conn.execute(cells_sql)]
        health_rows = [
            dict(zip(HEALTH_FIELDS, row)) for row in conn.execute(
                "SELECT h.health_check_id, h.session_id, h.checked_at, h.modem_id, h.event, h.alive, h.detail "
                "FROM health_checks h LEFT JOIN sessions s ON s.session_id=h.session_id "
                "WHERE h.event != 'legacy_v1' AND (s.mode IS NULL OR s.mode != 'legacy_v1') "
                "ORDER BY h.health_check_id"
            )
        ]
        periodicity_rows = []
        times_by_session = defaultdict(list)
        for row in measurement_rows:
            times_by_session[row["session_id"]].append(row)
        intervals = {row["session_id"]: row["interval_s"] for row in sessions}
        for session_id, rows in times_by_session.items():
            for previous, current in zip(rows, rows[1:]):
                periodicity_rows.append({
                    "session_id": session_id,
                    "previous_measurement_id": previous["measurement_id"],
                    "measurement_id": current["measurement_id"],
                    "previous_started_at": previous["started_at"],
                    "started_at": current["started_at"],
                    "interval_s": _interval_seconds(previous["started_at"], current["started_at"]),
                    "configured_interval_s": intervals.get(session_id, ""),
                })

    attempts = attempts_dataset.attempts
    total = len(attempts)
    counts = Counter()
    counts["snapshot_complete"] = sum(a.snapshot_status == "complete" for a in attempts)
    counts["snapshot_partial"] = sum(a.snapshot_status == "partial" for a in attempts)
    counts["snapshot_invalid"] = sum(a.snapshot_status == "invalid" for a in attempts)
    counts["csurv_ok"] = sum(a.csurv_status == "ok" for a in attempts)
    counts["serving_global_cell_key_present"] = sum(a.serving_global_cell_key is not None for a in attempts)
    counts["wknn_lte_eligible"] = sum(wknn_eligible(a, "LTE") for a in attempts)
    counts["wknn_nr_eligible"] = sum(wknn_eligible(a, "NR") for a in attempts)
    counts["cell_id_eligible"] = sum(cell_id_eligible(a) for a in attempts)
    for metric in ("moni_ok", "servinfo_ok", "gnss_ok"):
        counts[metric] = sum(row[f"{metric.split('_')[0]}_status"] == "ok" for row in measurement_rows)
    for rat in ("lte", "nr"):
        for suffix in ("cell_observations", "rsrp_observations", "cell_attempts", "rsrp_attempts"):
            counts[f"csurv_{rat}_{suffix}"] = 0
    cells_by_measurement = defaultdict(list)
    for cell in cell_rows:
        if cell["source"] == "CSURV" and cell["rat"] in ("LTE", "NR"):
            rat = cell["rat"].lower()
            counts[f"csurv_{rat}_cell_observations"] += 1
            if cell["rsrp_dbm"] is not None and cell["rsrp_dbm"] != 0:
                counts[f"csurv_{rat}_rsrp_observations"] += 1
            cells_by_measurement[cell["measurement_id"]].append(cell)
    for attempt in attempts:
        rows = cells_by_measurement.get(attempt.measurement_id, [])
        for rat in ("LTE", "NR"):
            matching = [cell for cell in rows if cell["rat"] == rat]
            counts[f"csurv_{rat.lower()}_cell_attempts"] += bool(matching)
            counts[f"csurv_{rat.lower()}_rsrp_attempts"] += any(
                cell["rsrp_dbm"] is not None and cell["rsrp_dbm"] != 0 for cell in matching
            )
    availability_rows = [
        {"metric": name, "count": count,
         "attempts": "" if name.endswith("_observations") else total,
         "percentage": "" if total == 0 or name.endswith("_observations")
         else f"{100 * count / total:.2f}"}
        for name, count in sorted(counts.items())
    ]

    target = Path(output_dir)
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise FileExistsError(f"{target}: destino existente não vazio; exportação não sobrescreve evidências")
    target.mkdir(parents=True, exist_ok=True)
    _write_csv(target, "sessoes.csv", SESSION_FIELDS, sessions)
    _write_csv(target, "medicoes_campanha.csv", MEASUREMENT_FIELDS, measurement_rows)
    _write_csv(target, "celulas_observadas.csv", CELL_FIELDS, cell_rows)
    _write_csv(target, "eventos_saude.csv", HEALTH_FIELDS, health_rows)
    _write_csv(target, "periodicidade.csv", PERIODICITY_FIELDS, periodicity_rows)
    _write_csv(target, "disponibilidade.csv", AVAILABILITY_FIELDS, availability_rows)
    return target


def _main(argv=None):
    parser = argparse.ArgumentParser(description="Exporta CSVs resumidos sem coordenadas GNSS brutas ou raw_json.")
    parser.add_argument("--db", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    target = export_campaign(args.db, args.out)
    print(f"Exportação somente leitura concluída: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
