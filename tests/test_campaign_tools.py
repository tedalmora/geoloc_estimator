import csv
import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from campaign_tools import export, manifest, monitor, reference, visit
from coletor import db


def _gnss_reading(lat, lon, *, status="ok", fix_type=3):
    return {
        "status": status, "response": {"final": "OK"}, "anomalies": [],
        "fix": {
            "lat": lat, "lon": lon, "alt_m": 12.0, "fix_type": fix_type,
            "hdop": 0.8, "nsat": 9, "utc": "2026-03-20T12:00:00Z",
        },
    }


def _fixes():
    return [
        {"lat": -26.9 + i * 0.000001, "lon": -49.07, "alt_m": 12.0,
         "fix_type": 3, "hdop": 0.8, "nsat": 9, "gnss_utc": f"2026-03-20T12:00:0{i}Z",
         "attempt": i + 1, "capture_utc": f"2026-03-20T12:00:0{i}+00:00"}
        for i in range(5)
    ]


def test_gnss_fix_validation_checks_status_fix_type_and_coordinates():
    assert reference.valid_fix(_gnss_reading(-26.9, -49.07), 0)
    assert reference.valid_fix(_gnss_reading(-26.9, -49.07, fix_type=2), 0)
    assert not reference.valid_fix(_gnss_reading(-26.9, -49.07, status="error"), 0)
    assert not reference.valid_fix(_gnss_reading(-26.9, -49.07, fix_type=1), 0)
    assert not reference.valid_fix(_gnss_reading(91, -49.07), 0)
    assert not reference.valid_fix(_gnss_reading(-26.9, -49.07), 1)


def test_reference_summary_requires_five_fixes_and_reports_dispersion():
    summary = reference.summarize_fixes(_fixes())
    assert summary["fix_count"] == 5
    assert summary["mean_lat"] == pytest.approx(-26.899998)
    assert summary["max_dispersion_m"] > 0
    assert summary["mean_hdop"] == pytest.approx(0.8)
    with pytest.raises(ValueError, match="exatamente 5"):
        reference.summarize_fixes(_fixes()[:4])


def test_canonical_references_append_without_overwriting(tmp_path):
    path = tmp_path / "referencias_canonicas.csv"
    summary = reference.summarize_fixes(_fixes())
    reference.add_canonical(path, "P01", summary)
    original = path.read_bytes()
    assert reference.read_canonical(path)["P01"]["lat"] == pytest.approx(summary["mean_lat"])
    with pytest.raises(FileExistsError, match="não será sobrescrita"):
        reference.add_canonical(path, "P01", summary)
    assert path.read_bytes() == original


def test_separation_checks_all_28_pairs_and_flags_close_points():
    references = {
        point: {"point": point, "lat": -26.9 + index * 0.001, "lon": -49.07,
                "uncertainty_m": 0.5}
        for index, point in enumerate(reference.POINTS)
    }
    rows = reference.separation_rows(references)
    assert len(rows) == 28
    assert rows[0]["point_a"] == "P01" and rows[0]["point_b"] == "P02"
    assert not rows[0]["below_minimum_distance"]
    references["P02"]["lat"] = references["P01"]["lat"] + 0.000001
    assert reference.separation_rows(references)[0]["below_minimum_distance"]


def test_monitor_cpu_and_process_metrics_handle_proc_samples(tmp_path):
    proc = tmp_path / "proc"
    process = proc / "123"
    process.mkdir(parents=True)
    (proc / "stat").write_text("cpu 100 0 20 80 0 0 0 0 0 0\n", encoding="ascii")
    (proc / "loadavg").write_text("0.10 0.20 0.30 1/2 123\n", encoding="ascii")
    (proc / "meminfo").write_text("MemAvailable: 4096 kB\n", encoding="ascii")
    (process / "stat").write_text("123 (sample process) R 0 0 0 0 0 0 0 0 0 0 50 10\n", encoding="ascii")
    (process / "status").write_text("VmRSS:\t100 kB\nVmHWM:\t200 kB\n", encoding="ascii")
    db_path = tmp_path / "campaign.db"
    db_path.write_bytes(b"db")
    Path(f"{db_path}-wal").write_bytes(b"wal")
    sample = monitor.sample_resources(db_path, tmp_path, 123, proc_root=proc, sys_root=tmp_path / "sys")
    assert sample["rss_kb"] == 100
    assert sample["hwm_kb"] == 200
    assert sample["memory_available_kb"] == 4096
    assert sample["load"] == ("0.10", "0.20", "0.30")
    assert sample["database_bytes"] == 5
    assert monitor.total_cpu_percent((100, 40), (200, 60)) == pytest.approx(80)
    assert monitor.process_cpu_percent(10, 20, 1, 100) == pytest.approx(10)


def test_manifest_records_environment_and_refuses_overwrite(tmp_path):
    path = tmp_path / "manifest.txt"
    manifest.create_manifest(
        path, "campaign-test", "raspberrypi_os", "operator", tmp_path / "campaign.db",
        "72406", "/dev/ttyUSB2", 240, 1.0, "north", root=tmp_path,
    )
    contents = path.read_text(encoding="utf-8")
    assert "system=raspberrypi_os" in contents
    assert "measurements_per_session=3" in contents
    assert "database_file=campaign.db" in contents
    assert not any(token in contents.lower() for token in ("imei", "imsi", "iccid"))
    with pytest.raises(FileExistsError):
        manifest.create_manifest(
            path, "campaign-test", "raspberrypi_os", "operator", "campaign.db",
            "72406", "/dev/ttyUSB2", 240, 1.0, "north", root=tmp_path,
        )


def test_fix_collection_stops_at_five_valid_results_within_eight_attempts():
    responses = [
        subprocess.CompletedProcess([], 0, json.dumps(_gnss_reading(-26.9, -49.07, status="error")), ""),
        *[
            subprocess.CompletedProcess([], 0, json.dumps(_gnss_reading(-26.9 + i * 0.000001, -49.07)), "")
            for i in range(5)
        ],
    ]
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return responses.pop(0)

    fixes, records = visit.collect_valid_fixes("/dev/ttyUSB2", command_runner=fake_run, sleep=lambda _: None)
    assert len(fixes) == 5
    assert len(records) == 6
    assert sum(row["valid"] for row in records) == 5
    assert len(calls) == 6
    with pytest.raises(visit.InsufficientFixesError) as error:
        visit.collect_valid_fixes(
            "/dev/ttyUSB2",
            command_runner=lambda *args, **kwargs: subprocess.CompletedProcess(
                [], 0, json.dumps(_gnss_reading(-26.9, -49.07, status="error")), ""
            ),
            sleep=lambda _: None,
        )
    assert len(error.value.records) == 8


def _campaign_database(path):
    conn = db.connect(str(path))
    point_id = db.add_reference_point(conn, "P01", -26.9, -49.07, coord_source="gnss")
    session_id = db.create_session(
        conn, mode="continuous", scenario="estatico", environment="outdoor", modem_id="fn990",
        metadata={"software_version": "test-version", "interval_s": 240},
        reference_point_id=point_id, campaign_plmn="72406",
    )
    measurement_ids = []
    for seq in range(1, 4):
        started = f"2026-03-20T12:00:{(seq - 1) * 4:02d}+00:00"
        cursor = conn.execute(
            "INSERT INTO measurements (session_id, reference_point_id, seq, started_at, snapshot_status, "
            "reasons_json, access_mode, serving_global_cell_key, serving_rsrp_dbm, moni_status, "
            "servinfo_status, csurv_status, gnss_status, gnss_fix_type, gnss_hdop, gnss_nsat, "
            "timings_json, anomalies_json, raw_json) VALUES (?, ?, ?, ?, 'complete', '[]', 'LTE', "
            "'LTE|72406|1', -90, 'ok', 'ok', 'ok', 'ok', 3, 0.8, 9, '{}', '[]', '{}')",
            (session_id, point_id, seq, started),
        )
        measurement_id = cursor.lastrowid
        measurement_ids.append(measurement_id)
        conn.execute(
            "INSERT INTO observed_cells (measurement_id, source, rat, arfcn, pci, plmn, cell_id, tac, "
            "rsrp_dbm, is_serving, radio_key, global_cell_key, identity_status) "
            "VALUES (?, 'CSURV', 'LTE', 100, 1, '72406', '1', '1', -90, 1, 'LTE|100|1', "
            "'LTE|72406|1', 'complete')",
            (measurement_id,),
        )
    db.insert_health_check(conn, session_id, "fn990", "start", True)
    db.finish_session(conn, session_id, "completed")
    conn.close()
    return measurement_ids


def test_export_is_read_only_and_omits_raw_gnss_coordinates(tmp_path):
    db_path = tmp_path / "campaign.db"
    _campaign_database(db_path)
    before = hashlib.sha256(db_path.read_bytes()).hexdigest()
    out = export.export_campaign(db_path, tmp_path / "export")
    after = hashlib.sha256(db_path.read_bytes()).hexdigest()
    assert before == after
    expected = {
        "sessoes.csv", "medicoes_campanha.csv", "celulas_observadas.csv",
        "eventos_saude.csv", "periodicidade.csv", "disponibilidade.csv",
    }
    assert {path.name for path in out.iterdir()} == expected
    contents = "\n".join(path.read_text(encoding="utf-8") for path in out.iterdir())
    assert "raw_json" not in contents
    assert "gnss_lat" not in contents and "gnss_lon" not in contents
    assert "-26.9" not in contents and "-49.07" not in contents
    with (out / "disponibilidade.csv").open(newline="", encoding="utf-8") as stream:
        rows = {row["metric"]: row for row in csv.DictReader(stream)}
    assert rows["wknn_lte_eligible"]["count"] == "3"
    assert rows["cell_id_eligible"]["count"] == "3"
    assert rows["csurv_lte_cell_observations"]["count"] == "3"
    assert rows["csurv_lte_cell_attempts"]["percentage"] == "100.00"


def test_visit_registers_primary_reference_only_after_confirmation(tmp_path, monkeypatch):
    fixes = _fixes()
    attempts = [
        {**fix, "valid": True, "reason": ""} for fix in fixes
    ]
    monkeypatch.setattr(visit, "collect_valid_fixes", lambda port: (fixes, attempts))
    registered = []
    monkeypatch.setattr(visit, "_register_reference", lambda *args: registered.append(args) or 7)
    collection = []
    monkeypatch.setattr(visit.monitor, "run_collection", lambda *args: collection.append(args) or 0)
    monkeypatch.setattr(visit, "_verify_collection", lambda *args: (88, 3))
    sleeps = []
    args = SimpleNamespace(
        system="raspberrypi_os", point="P01", session="S1", db=str(tmp_path / "db.sqlite"),
        port="/dev/ttyUSB2", plmn="72406", results=str(tmp_path / "results"), canonical=None,
        data_dir=None, interval_s=240, phone_lat="-26.90001", phone_lon="-49.07",
        phone_accuracy_m="5", phone_app="phone-app", phone_time="12:00Z",
    )
    session_id = visit.run_visit(args, input_fn=lambda _: "OK", sleep=sleeps.append)
    canonical = reference.read_canonical(tmp_path / "results" / "referencias_canonicas.csv")
    assert session_id == 88
    assert sleeps == [60]
    assert registered and len(collection) == 1
    assert "--max-measurements" in collection[0][-1]
    assert canonical["P01"]["lat"] == pytest.approx(reference.summarize_fixes(fixes)["mean_lat"])
    with (tmp_path / "results" / "resumo_referencia.csv").open(newline="", encoding="utf-8") as stream:
        row = next(csv.DictReader(stream))
    assert float(row["phone_distance_m"]) > 0
    assert row["phone_accuracy_m"] == "5.000"
    assert "phone_lat" not in row and "phone_lon" not in row


def test_visit_cancellation_preserves_reference_without_starting_collection(tmp_path, monkeypatch):
    fixes = _fixes()
    attempts = [
        {**fix, "valid": True, "reason": ""} for fix in fixes
    ]
    monkeypatch.setattr(visit, "collect_valid_fixes", lambda port: (fixes, attempts))
    monkeypatch.setattr(
        visit.monitor, "run_collection",
        lambda *args: pytest.fail("coleta não deve iniciar sem confirmação manual"),
    )
    args = SimpleNamespace(
        system="raspberrypi_os", point="P01", session="S1", db=str(tmp_path / "db.sqlite"),
        port="/dev/ttyUSB2", plmn="72406", results=str(tmp_path / "results"), canonical=None,
        data_dir=None, interval_s=240,
    )
    with pytest.raises(RuntimeError, match="visita cancelada"):
        visit.run_visit(args, input_fn=lambda _: "NAO", sleep=lambda _: None)
    assert not (tmp_path / "results" / "referencias_canonicas.csv").exists()
    assert (tmp_path / "results" / "referencias" / "P01_S1_fixes.csv").exists()
