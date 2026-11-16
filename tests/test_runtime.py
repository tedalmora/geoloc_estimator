"""Testes de configuração e persistência do daemon runtime sem hardware."""

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

import runtime.daemon as daemon
from hal.models import GnssFix, GnssReading


ROOT = Path(__file__).resolve().parents[1]


def write_config(tmp_path, **overrides):
    values = {
        "TCC_MODE": "runtime",
        "TCC_METHOD": "gnss",
        "TCC_PORT": "/dev/ttyTEST",
        "TCC_INTERVAL": "240",
        "TCC_MODEL": "",
        "TCC_REGISTRY": "",
        "TCC_OUTPUT": str(tmp_path / "runtime" / "latest_position.json"),
        "TCC_HISTORY": str(tmp_path / "runtime" / "history.jsonl"),
        "TCC_LOG": str(tmp_path / "logs" / "runtime.log"),
    }
    values.update(overrides)
    path = tmp_path / "runtime.conf"
    path.write_text(
        "".join(f"{key}={value}\n" for key, value in values.items()),
        encoding="utf-8",
    )
    return str(path)


def result(status, latitude=None, longitude=None, reason=None):
    return {
        "timestamp_utc": "2026-01-01T00:00:00.000Z",
        "method": "gnss",
        "status": status,
        "latitude": latitude,
        "longitude": longitude,
        "technology": "GNSS" if status == "valid" else None,
        "reason": reason,
        "software_version": "test",
        "timings_s": {"total_s": 1.0},
    }


def test_load_config_parses_runtime_conf(tmp_path):
    path = write_config(tmp_path, TCC_MODE="manual", TCC_PORT="")
    config = daemon.load_config(path)
    assert (config.mode, config.method, config.port, config.interval) == ("manual", "gnss", "", "240")


def test_manual_mode_does_not_create_driver_or_start_loop(tmp_path, monkeypatch, capsys):
    path = write_config(tmp_path, TCC_MODE="manual", TCC_PORT="")
    monkeypatch.setattr(daemon, "FN990Driver", lambda: pytest.fail("manual não acessa o modem"))
    assert daemon.main(["--config", path]) == 0
    assert "TCC_MODE=manual" in capsys.readouterr().out


def test_rejects_unknown_method_before_loop(tmp_path):
    config = daemon.load_config(write_config(tmp_path, TCC_METHOD="unknown"))
    with pytest.raises(daemon.RuntimeConfigError, match="TCC_METHOD"):
        daemon.validate_runtime_config(config)


def test_wknn_requires_existing_valid_model(tmp_path):
    config = daemon.load_config(write_config(tmp_path, TCC_METHOD="wknn", TCC_MODEL=""))
    with pytest.raises(daemon.RuntimeConfigError, match="TCC_MODEL"):
        daemon.validate_runtime_config(config)


def test_cell_id_requires_valid_registry(tmp_path):
    config = daemon.load_config(write_config(tmp_path, TCC_METHOD="cell-id", TCC_REGISTRY=""))
    with pytest.raises(daemon.RuntimeConfigError, match="TCC_REGISTRY"):
        daemon.validate_runtime_config(config)


def test_gnss_needs_no_model_and_no_fix_is_unavailable(tmp_path, monkeypatch):
    config = daemon.load_config(write_config(tmp_path))
    model, registry = daemon.validate_runtime_config(config)

    class Driver:
        def connect(self, _port):
            pass

        def read_gnss(self):
            return GnssReading(status="no_fix")

        def close(self):
            pass

    monkeypatch.setattr(daemon, "FN990Driver", Driver)
    output = daemon.infer_once(config, model, registry)
    assert output["status"] == "unavailable"
    assert output["latitude"] is None and output["longitude"] is None
    assert "no_fix" in output["reason"]


@pytest.mark.parametrize(
    ("latitude", "longitude", "status"),
    [(46.0, -72.0, "valid"), (91.0, 0.0, "unavailable")],
)
def test_gnss_fix_reports_validity_and_reason(tmp_path, monkeypatch, latitude, longitude, status):
    config = daemon.load_config(write_config(tmp_path))

    class Driver:
        def connect(self, _port):
            pass

        def read_gnss(self):
            fix = GnssFix(
                lat=latitude, lon=longitude, alt_m=None, fix_type=3,
                hdop=None, nsat=None, utc=None,
            )
            return GnssReading(status="ok", fix=fix)

        def close(self):
            pass

    monkeypatch.setattr(daemon, "FN990Driver", Driver)
    output = daemon.infer_once(config, None, None)
    assert output["status"] == status
    if status == "unavailable":
        assert output["latitude"] is None and output["longitude"] is None
        assert output["reason"] == "posição estimada com coordenadas inválidas"
    else:
        assert output["latitude"] == latitude and output["longitude"] == longitude
        assert output["reason"] is None


def test_latest_is_replaced_atomically(tmp_path):
    path = tmp_path / "runtime" / "latest_position.json"
    with patch.object(daemon.os, "replace", wraps=os.replace) as replace:
        daemon.write_latest(str(path), result("valid", -10.0, -20.0))
    assert replace.call_count == 1
    source, destination = replace.call_args.args
    assert Path(source).parent == path.parent
    assert Path(destination) == path
    assert json.loads(path.read_text(encoding="utf-8"))["latitude"] == -10.0
    assert list(path.parent.glob("*.tmp")) == []


def test_history_appends_success_and_unavailable_attempts(tmp_path):
    path = tmp_path / "runtime" / "history.jsonl"
    path.parent.mkdir()
    daemon.append_history(str(path), result("valid", 1.0, 2.0))
    daemon.append_history(str(path), result("unavailable", reason="sem fix"))
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [line["status"] for line in lines] == ["valid", "unavailable"]
    assert all(not {"imei", "imsi", "iccid"} & line.keys() for line in lines)


def test_unavailable_latest_does_not_keep_previous_coordinates(tmp_path):
    path = tmp_path / "latest_position.json"
    daemon.write_latest(str(path), result("valid", 1.0, 2.0))
    daemon.write_latest(str(path), result("unavailable", reason="sem fix"))
    latest = json.loads(path.read_text(encoding="utf-8"))
    assert latest["status"] == "unavailable"
    assert latest["latitude"] is None and latest["longitude"] is None


def test_runtime_start_replaces_old_position_before_waiting_for_modem(tmp_path):
    config = daemon.load_config(write_config(tmp_path))
    daemon.prepare_output_paths(config)
    old = result("valid", -10.0, -20.0)
    daemon.write_latest(config.output, old)
    stop_event = threading.Event()
    history = Path(config.history)

    with patch.object(daemon, "wait_for_modem", return_value=False) as wait:
        assert daemon.run_loop(config, None, None, stop_event) == 0

    wait.assert_called_once()
    latest = json.loads(Path(config.output).read_text(encoding="utf-8"))
    assert latest["status"] == "unavailable"
    assert latest["latitude"] is None and latest["longitude"] is None
    assert latest["method"] == config.method
    assert "aguardando FN990" in latest["reason"]
    assert latest["timestamp_utc"] != old["timestamp_utc"]
    history = [json.loads(line) for line in history.read_text(encoding="utf-8").splitlines()]
    assert len(history) == 1
    assert history[0]["event"] == "runtime_started"


def test_runtime_history_records_modem_state_and_inference_once(tmp_path, monkeypatch):
    config = daemon.load_config(write_config(tmp_path))
    daemon.prepare_output_paths(config)
    monkeypatch.setattr(daemon, "wait_for_modem", lambda *_args: True)
    monkeypatch.setattr(daemon, "infer_once", lambda *_args: result("valid", 1.0, 2.0))

    assert daemon.run_loop(config, None, None, threading.Event(), once=True) == 0

    history = [json.loads(line) for line in Path(config.history).read_text(encoding="utf-8").splitlines()]
    assert [entry.get("event", "inference") for entry in history] == [
        "runtime_started", "modem_ready", "inference",
    ]


def test_help_and_cooperative_shutdown(tmp_path):
    help_result = subprocess.run(
        [sys.executable, "-m", "runtime.daemon", "--help"],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    assert help_result.returncode == 0
    assert "--config" in help_result.stdout and "--once" in help_result.stdout

    config = daemon.load_config(write_config(tmp_path))
    stop_event = threading.Event()
    stop_event.set()
    with patch.object(daemon, "wait_for_modem", side_effect=AssertionError("não deve aguardar modem")):
        assert daemon.run_loop(config, None, None, stop_event) == 0
