import csv
import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call
import pytest
from acquisition.snapshot import Snapshot
from campaign_tools import (
    check, config, configure, export, manifest, monitor, position, prepare, reference, validate, visit, wknn,
)
from estimador.estimate import PositionEstimate
from estimador.geometry import haversine_m
from runtime import daemon as runtime_daemon
from coletor import db
from hal.models import (
    CellObservation, CsurvReading, GnssFix, GnssReading, MoniReading, PrepareResult, ServInfoReading,
    ServingCellResolution,
)


def _location_config(tmp_path):
    return {
        "system": "raspberrypi_os", "data_dir": str(tmp_path / "data"),
        "port": "/dev/ttyUSB2", "plmn": "72406", "environment": "indoor", "interval_s": "90",
    }


def test_location_config_round_trip_and_refuses_overwrite_without_force(tmp_path):
    path = tmp_path / config.CONFIG_NAME
    values = _location_config(tmp_path)
    config.save_config(path, values)
    assert config.load_config(path) == values
    original = path.read_bytes()
    with pytest.raises(FileExistsError):
        config.save_config(path, {**values, "environment": "outdoor"})
    assert path.read_bytes() == original
    config.save_config(path, {**values, "environment": "outdoor"}, force=True)
    assert config.load_config(path)["environment"] == "outdoor"
    contents = path.read_text(encoding="utf-8").lower()
    assert not any(token in contents for token in ("python", "imei", "imsi", "iccid", "password"))
    assert set(config.load_config(path)) == set(config.FIELDS)


@pytest.mark.parametrize("line", [
    "unknown=value", "imei=123", "imsi=123", "iccid=123", "python=/usr/bin/python3",
    "system=linux", "environment=inside", "port= ", "data_dir= ", "plmn=7240",
    "plmn=7240600", "plmn=72a06", "interval_s=0", "interval_s=-1", "interval_s=nan",
    "interval_s=inf", "interval_s=abc", "not-an-assignment",
])
def test_location_config_rejects_invalid_fields(tmp_path, line):
    path = tmp_path / config.CONFIG_NAME
    path.write_text(line + "\n", encoding="utf-8")
    with pytest.raises(ValueError):
        config.load_config(path)


def test_location_config_rejects_duplicate_keys(tmp_path):
    path = tmp_path / config.CONFIG_NAME
    path.write_text("port=/dev/ttyUSB2\n port = /dev/ttyUSB3\n", encoding="utf-8")
    with pytest.raises(ValueError, match="duplicada"):
        config.load_config(path)


def test_location_config_search_order_and_explicit_missing_error(tmp_path, monkeypatch):
    system_path = tmp_path / "etc" / config.CONFIG_NAME
    user_path = tmp_path / "user" / config.CONFIG_NAME
    explicit = tmp_path / "explicit.conf"
    monkeypatch.setattr(config, "SYSTEM_CONFIG", system_path)
    monkeypatch.setattr(config, "user_config_path", lambda: user_path)
    assert config.find_config() is None
    config.save_config(user_path, _location_config(tmp_path))
    assert config.find_config() == user_path
    config.save_config(system_path, _location_config(tmp_path))
    assert config.find_config() == system_path
    config.save_config(explicit, _location_config(tmp_path))
    assert config.find_config(explicit) == explicit
    with pytest.raises(FileNotFoundError):
        config.find_config(tmp_path / "missing.conf")


@pytest.mark.parametrize("release,model,expected", [
    ('ID=buildroot\nNAME="Buildroot"\n', "", "buildroot"),
    ('ID=raspbian\nNAME="Raspbian GNU/Linux"\n', "", "raspberrypi_os"),
    ('ID=debian\nNAME="Debian GNU/Linux"\n', "Raspberry Pi 3 Model B Rev 1.2\0", "raspberrypi_os"),
    ('ID=ubuntu\nNAME="Ubuntu"\n', "", None),
])
def test_configure_detects_system_and_default_data_dir(tmp_path, monkeypatch, release, model, expected):
    os_release = tmp_path / "os-release"
    device_model = tmp_path / "model"
    os_release.write_text(release, encoding="utf-8")
    device_model.write_text(model, encoding="utf-8")
    monkeypatch.setattr(configure, "OS_RELEASE", os_release)
    monkeypatch.setattr(configure, "DEVICE_MODEL", device_model)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    if expected is None:
        with pytest.raises(ValueError, match="nao reconhecido"):
            configure.detect_system()
    else:
        assert configure.detect_system() == expected
        expected_dir = Path("/data/tcc") if expected == "buildroot" else tmp_path / "tcc-dados"
        assert configure.default_data_dir(expected) == expected_dir


def _servinfo(plmn="72406", status="ok"):
    return ServInfoReading(
        status=status, observations=[CellObservation(rat="LTE", source="SERVINFO", plmn=plmn)],
    )


@pytest.mark.parametrize("plmn", ["72406", "001001"])
def test_configure_extracts_plmn_from_servinfo(plmn):
    assert configure.extract_plmn(_servinfo(plmn)) == plmn


@pytest.mark.parametrize("reading", [
    _servinfo(status="timeout"), _servinfo(plmn=None), _servinfo(plmn="invalid"),
    ServInfoReading(status="ok", observations=[
        CellObservation(rat="LTE", source="SERVINFO", plmn="72406"),
        CellObservation(rat="NR", source="SERVINFO", plmn="72410"),
    ]),
])
def test_configure_rejects_missing_invalid_or_ambiguous_plmn(reading):
    with pytest.raises(ValueError):
        configure.extract_plmn(reading)


def _mock_driver():
    driver = Mock()
    driver.is_alive.return_value = True
    driver.read_serving_info.return_value = _servinfo()
    driver.prepare_for_collection.return_value = PrepareResult(0, True, "OK")
    driver.read_gnss.return_value = GnssReading(status="no_fix")
    return driver


def test_configure_discovers_usb_and_acm_only(monkeypatch):
    def fake_glob(path, pattern):
        assert path == Path("/dev")
        return [path / ("ttyUSB2" if pattern == "ttyUSB*" else "ttyACM0")]

    monkeypatch.setattr(Path, "glob", fake_glob)
    assert configure.discover_ports() == [str(Path("/dev/ttyACM0")), str(Path("/dev/ttyUSB2"))]


def test_configure_selects_only_valid_port_and_closes_all_probes(monkeypatch):
    dead, no_service, good = _mock_driver(), _mock_driver(), _mock_driver()
    dead.is_alive.return_value = False
    no_service.read_serving_info.return_value = _servinfo(status="timeout")
    monkeypatch.setattr(configure, "FN990Driver", Mock(side_effect=[dead, no_service, good]))
    assert configure.select_port(
        ["/dev/ttyUSB0", "/dev/ttyUSB1", "/dev/ttyUSB2"],
        input_fn=lambda _: pytest.fail("nao deve perguntar com uma porta valida"),
    ) == "/dev/ttyUSB2"
    for driver in (dead, no_service, good):
        driver.close.assert_called_once_with()
        driver.get_status.assert_not_called()


def test_configure_prompts_for_multiple_valid_ports(monkeypatch, capsys):
    drivers = [_mock_driver(), _mock_driver()]
    monkeypatch.setattr(configure, "FN990Driver", Mock(side_effect=drivers))
    assert configure.select_port(["/dev/ttyUSB2", "/dev/ttyACM0"], input_fn=lambda _: "2") == "/dev/ttyACM0"
    assert "1: /dev/ttyUSB2" in capsys.readouterr().out
    for driver in drivers:
        driver.close.assert_called_once_with()


def test_configure_rejects_invalid_port_choice_and_no_valid_port(monkeypatch):
    driver = _mock_driver()
    monkeypatch.setattr(configure, "FN990Driver", lambda: driver)
    with pytest.raises(ValueError, match="escolha"):
        configure.select_port(["a", "b"], input_fn=lambda _: "3")
    driver.connect.side_effect = OSError("porta indisponivel")
    with pytest.raises(RuntimeError, match="nenhuma porta"):
        configure.select_port(["a"])
    assert driver.close.call_count == 3


def _configure_setup(tmp_path, monkeypatch):
    driver = _mock_driver()
    monkeypatch.setattr(configure, "detect_system", lambda: "raspberrypi_os")
    monkeypatch.setattr(configure, "discover_ports", lambda: ["/dev/ttyUSB2"])
    monkeypatch.setattr(configure, "FN990Driver", lambda: driver)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(config, "SYSTEM_CONFIG", tmp_path / "etc" / config.CONFIG_NAME)
    args = SimpleNamespace(config=None, environment="indoor", snapshots=4, force=False)
    return args, driver


@pytest.mark.parametrize("gnss", [
    GnssReading(status="no_fix"), GnssReading(status="off"),
    GnssReading(status="ok", fix=GnssFix(-26.9, -49.07, 12, 3, 0.8, 9, None)),
])
def test_configure_prepares_gnss_and_measures_four_snapshots(tmp_path, monkeypatch, capsys, gnss):
    args, driver = _configure_setup(tmp_path, monkeypatch)
    driver.read_gnss.return_value = gnss
    snapshots = Mock(side_effect=[
        SimpleNamespace(status="partial", transport_error=None, timings_s={"total": total})
        for total in (59.183, 10.162, 38.267, 10.927)
    ])
    monkeypatch.setattr(configure, "take_snapshot", snapshots)
    target = configure.run_configure(args)
    assert target == config.user_config_path()
    assert config.load_config(target) == {
        **_location_config(tmp_path), "data_dir": str(tmp_path / "tcc-dados"),
    }
    assert (tmp_path / "tcc-dados").is_dir()
    assert snapshots.call_args_list == [call(driver)] * 4
    driver.prepare_for_collection.assert_called_once_with()
    assert driver.close.call_count == 2
    driver.get_status.assert_not_called()
    assert sorted(path.name for path in tmp_path.rglob("*") if path.is_file()) == [config.CONFIG_NAME]
    captured = capsys.readouterr()
    assert f"GNSS: {'fix' if gnss.status == 'ok' else gnss.status}" in captured.out
    if gnss.status == "no_fix":
        assert "AVISO: GNSS no_fix" in captured.err


def test_configure_does_not_probe_or_overwrite_existing_file_without_force(tmp_path, monkeypatch):
    args, driver = _configure_setup(tmp_path, monkeypatch)
    args.config = str(tmp_path / config.CONFIG_NAME)
    config.save_config(args.config, _location_config(tmp_path))
    original = Path(args.config).read_bytes()
    with pytest.raises(FileExistsError, match="--force"):
        configure.run_configure(args)
    driver.connect.assert_not_called()
    assert Path(args.config).read_bytes() == original
    args.force = True
    args.snapshots = 1
    monkeypatch.setattr(configure, "take_snapshot", lambda _: SimpleNamespace(
        status="complete", transport_error=None, timings_s={"total": 90.1},
    ))
    configure.run_configure(args)
    assert config.load_config(args.config)["interval_s"] == "150"


@pytest.mark.parametrize("failure", ["invalid", "transport", "timing", "gnss", "prepare", "connect"])
def test_configure_failure_never_writes_and_closes_port(tmp_path, monkeypatch, failure):
    args, driver = _configure_setup(tmp_path, monkeypatch)
    args.config = str(tmp_path / config.CONFIG_NAME)
    monkeypatch.setattr(configure, "take_snapshot", lambda _: SimpleNamespace(
        status="invalid" if failure == "invalid" else "partial",
        transport_error="disconnected" if failure == "transport" else None,
        timings_s={"total": float("nan") if failure == "timing" else 59.183},
    ))
    if failure == "gnss":
        driver.read_gnss.return_value = GnssReading(status="error")
    elif failure == "prepare":
        driver.prepare_for_collection.return_value = PrepareResult(0, True, "ERROR")
    elif failure == "connect":
        driver.connect.side_effect = [None, OSError("porta caiu")]
    with pytest.raises((OSError, RuntimeError)):
        configure.run_configure(args)
    assert not Path(args.config).exists()
    assert driver.close.call_count == 2


def test_visit_loads_config_and_cli_overrides_each_config_value(tmp_path, monkeypatch):
    path = config.save_config(tmp_path / config.CONFIG_NAME, _location_config(tmp_path))
    monkeypatch.setattr(config, "SYSTEM_CONFIG", path)
    run = Mock()
    monkeypatch.setattr(visit, "run_visit", run)
    required = ["--point", "P01", "--session", "S1", "--db", "campaign.db", "--results", "results"]
    assert visit._main(required) == 0
    args = run.call_args.args[0]
    for key, value in _location_config(tmp_path).items():
        assert getattr(args, key) == (float(value) if key == "interval_s" else value)
    assert visit._main([
        "--config", str(path), *required, "--system", "buildroot", "--port", "/dev/ttyACM0",
        "--plmn", "001001", "--data-dir", str(tmp_path / "override"), "--interval-s", "120",
        "--environment", "outdoor",
    ]) == 0
    args = run.call_args.args[0]
    assert (args.system, args.port, args.plmn, args.data_dir, args.interval_s, args.environment) == (
        "buildroot", "/dev/ttyACM0", "001001", str(tmp_path / "override"), 120, "outdoor",
    )


def test_visit_old_cli_keeps_defaults_without_config(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SYSTEM_CONFIG", tmp_path / "absent")
    monkeypatch.setattr(config, "user_config_path", lambda: tmp_path / "also-absent")
    run = Mock()
    monkeypatch.setattr(visit, "run_visit", run)
    assert visit._main([
        "--system", "raspberrypi_os", "--port", "/dev/ttyUSB2", "--plmn", "72406",
        "--point", "P01", "--session", "S1", "--db", "campaign.db", "--results", "results",
    ]) == 0
    args = run.call_args.args[0]
    assert args.interval_s == 240
    assert args.environment == "outdoor"
    assert args.data_dir is None and args.canonical is None


@pytest.mark.parametrize("missing", ["--point", "--session", "--db", "--results"])
def test_visit_still_requires_visit_specific_arguments(tmp_path, monkeypatch, missing):
    path = config.save_config(tmp_path / config.CONFIG_NAME, _location_config(tmp_path))
    arguments = {
        "--point": "P01", "--session": "S1", "--db": "campaign.db", "--results": "results",
    }
    monkeypatch.setattr(visit, "run_visit", lambda _: pytest.fail("argumento obrigatorio ausente"))
    with pytest.raises(SystemExit) as exc:
        visit._main(["--config", str(path), *[
            token for key, value in arguments.items() if key != missing for token in (key, value)
        ]])
    assert exc.value.code == 2


def _prepare_env(tmp_path, monkeypatch):
    path = config.save_config(tmp_path / config.CONFIG_NAME, _location_config(tmp_path))
    monkeypatch.setattr(config, "SYSTEM_CONFIG", path)
    monkeypatch.setattr(config, "user_config_path", lambda: tmp_path / "absent")
    monkeypatch.setattr(config, "SYSTEM_RUNTIME_CONFIG", tmp_path / "etc" / "runtime.conf")
    monkeypatch.setattr(config, "user_runtime_config_path", lambda: tmp_path / "home" / "runtime.conf")
    return path


def _prepare_args(name="preliminar-interna", points="P01,P03,P05,P08"):
    return SimpleNamespace(
        name=name, points=points, operator="Thiago Dalmora", config=None,
        antenna_height_m=1.0, orientation="orientacao fixa conforme planta",
        weather="ambiente interno", phone_used="nao",
    )


@pytest.mark.parametrize("name,valid", [
    ("preliminar-interna", True), ("Coleta_2", True), ("", False), ("a b", False),
    ("../fora", False), ("x/y", False), ("acentuação", False),
])
def test_prepare_validates_name(name, valid):
    if valid:
        assert prepare.validate_name(name) == name
    else:
        with pytest.raises(ValueError):
            prepare.validate_name(name)


@pytest.mark.parametrize("text,expected", [
    ("P01,P03,P05,P08", ["P01", "P03", "P05", "P08"]), (" P02 , P01", ["P02", "P01"]),
    ("P01,P01", None), ("P09", None), ("P1", None), ("", None), ("P01,", None),
])
def test_prepare_validates_points(text, expected):
    if expected is not None:
        assert prepare.parse_points(text) == expected
    else:
        with pytest.raises(ValueError):
            prepare.parse_points(text)


@pytest.mark.parametrize("points", ["P01,P03,P05,P08", ",".join(f"P{i:02d}" for i in range(1, 9))])
def test_prepare_creates_collection_manifest_and_idle_metrics(tmp_path, monkeypatch, points):
    _prepare_env(tmp_path, monkeypatch)
    idle = Mock()
    results = prepare.run_prepare(_prepare_args(points=points), idle=idle)
    expected = (tmp_path / "data").resolve() / "campaigns" / "preliminar-interna"
    assert results == expected
    assert all((results / subdir).is_dir() for subdir in prepare.SUBDIRS)
    assert not (results / "referencias_canonicas.csv").exists()
    assert not (results / "campanha" / "radiomapa.db").exists()
    idle.assert_called_once_with(
        str(results / "campanha" / "radiomapa.db"), str(tmp_path / "data"),
        str(results / "metricas_repouso.csv"), 60, 10,
    )
    collection = prepare.load_collection(results / "collection.conf")
    assert collection["points"] == points.split(",")
    assert collection["results"] == str(results)
    assert collection["db"] == str(results / "campanha" / "radiomapa.db")
    assert collection["canonical"] == str(results / "referencias_canonicas.csv")
    raw = (results / "collection.conf").read_text(encoding="utf-8")
    assert [line.split("=")[0] for line in raw.splitlines()] == list(prepare.COLLECTION_FIELDS)
    assert not any(token in raw for token in ("port", "plmn", "environment", "interval", "python", "active"))
    manifest_text = (results / "manifesto_campanha.txt").read_text(encoding="utf-8")
    assert f"planned_points={len(points.split(','))}\n" in manifest_text
    assert "campaign_id=preliminar-interna\n" in manifest_text
    assert "at_port=/dev/ttyUSB2\n" in manifest_text and "interval_s=90.0\n" in manifest_text
    assert "operator=Thiago Dalmora\n" in manifest_text
    assert not any(token in manifest_text.lower() for token in ("imei", "imsi", "iccid"))


def test_prepare_refuses_existing_collection_and_keeps_it(tmp_path, monkeypatch):
    _prepare_env(tmp_path, monkeypatch)
    results = prepare.run_prepare(_prepare_args(), idle=Mock())
    before = (results / "collection.conf").read_bytes()
    with pytest.raises(FileExistsError):
        prepare.run_prepare(_prepare_args(points="P02"), idle=Mock())
    assert (results / "collection.conf").read_bytes() == before
    assert prepare._main([
        "--name", "preliminar-interna", "--points", "P02", "--operator", "x",
    ]) == 1


def test_prepare_failure_removes_only_new_incomplete_collection(tmp_path, monkeypatch):
    _prepare_env(tmp_path, monkeypatch)
    previous = prepare.run_prepare(_prepare_args(name="anterior"), idle=Mock())
    with pytest.raises(OSError, match="disco"):
        prepare.run_prepare(_prepare_args(name="nova"), idle=Mock(side_effect=OSError("disco indisponivel")))
    assert not (previous.parent / "nova").exists()
    assert (previous / "collection.conf").is_file()


def test_prepare_requires_location_config(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SYSTEM_CONFIG", tmp_path / "absent")
    monkeypatch.setattr(config, "user_config_path", lambda: tmp_path / "also-absent")
    with pytest.raises(FileNotFoundError, match="configure"):
        prepare.run_prepare(_prepare_args(), idle=Mock())


def test_manifest_planned_points_default_and_cli(tmp_path):
    common = ["--campaign-id", "c", "--system", "buildroot", "--operator", "o", "--db", "x.db",
              "--plmn", "72406", "--port", "/dev/ttyUSB2", "--interval-s", "90",
              "--antenna-height-m", "1", "--orientation", "n", "--root", str(tmp_path)]
    manifest._main(["--output", str(tmp_path / "default.txt"), *common])
    manifest._main(["--output", str(tmp_path / "four.txt"), *common, "--planned-points", "4"])
    assert "planned_points=8\n" in (tmp_path / "default.txt").read_text(encoding="utf-8")
    assert "planned_points=4\n" in (tmp_path / "four.txt").read_text(encoding="utf-8")
    with pytest.raises(ValueError):
        manifest.create_manifest(tmp_path / "bad.txt", "c", "buildroot", "o", "x.db", "72406",
                                 "/dev/ttyUSB2", 90, 1.0, "n", root=tmp_path, planned_points=0)


@pytest.mark.parametrize("by_path", [False, True])
def test_visit_resolves_campaign_by_name_or_collection_path(tmp_path, monkeypatch, by_path):
    _prepare_env(tmp_path, monkeypatch)
    results = prepare.run_prepare(_prepare_args(), idle=Mock())
    run = Mock()
    monkeypatch.setattr(visit, "run_visit", run)
    campaign = str(results / "collection.conf") if by_path else "preliminar-interna"
    assert visit._main(["--campaign", campaign, "--point", "P03", "--session", "S1"]) == 0
    args = run.call_args.args[0]
    assert (args.results, args.db, args.canonical) == (
        str(results), str(results / "campanha" / "radiomapa.db"), str(results / "referencias_canonicas.csv"),
    )
    assert (args.port, args.plmn, args.environment, args.interval_s, args.system) == (
        "/dev/ttyUSB2", "72406", "indoor", 90.0, "raspberrypi_os",
    )


def test_visit_rejects_point_outside_collection(tmp_path, monkeypatch, capsys):
    _prepare_env(tmp_path, monkeypatch)
    prepare.run_prepare(_prepare_args(), idle=Mock())
    monkeypatch.setattr(visit, "run_visit", lambda _: pytest.fail("ponto fora da coleta"))
    assert visit._main(["--campaign", "preliminar-interna", "--point", "P02", "--session", "S1"]) == 1
    assert "P02 nao pertence" in capsys.readouterr().err


def test_visit_cli_overrides_collection_and_location(tmp_path, monkeypatch):
    _prepare_env(tmp_path, monkeypatch)
    prepare.run_prepare(_prepare_args(), idle=Mock())
    run = Mock()
    monkeypatch.setattr(visit, "run_visit", run)
    assert visit._main([
        "--campaign", "preliminar-interna", "--point", "P01", "--session", "S2",
        "--db", "outro.db", "--results", "outros", "--canonical", "ref.csv",
        "--port", "/dev/ttyACM0", "--interval-s", "120", "--environment", "outdoor",
    ]) == 0
    args = run.call_args.args[0]
    assert (args.db, args.results, args.canonical, args.port, args.interval_s, args.environment) == (
        "outro.db", "outros", "ref.csv", "/dev/ttyACM0", 120.0, "outdoor",
    )


def test_visit_campaign_missing_collection_is_error(tmp_path, monkeypatch):
    _prepare_env(tmp_path, monkeypatch)
    monkeypatch.setattr(visit, "run_visit", lambda _: pytest.fail("coleta ausente"))
    assert visit._main(["--campaign", "inexistente", "--point", "P01", "--session", "S1"]) == 1


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


@pytest.mark.parametrize("environment", ["indoor", "outdoor"])
def test_visit_registers_primary_reference_only_after_confirmation(tmp_path, monkeypatch, environment):
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
        data_dir=None, interval_s=240, environment=environment, phone_lat="-26.90001", phone_lon="-49.07",
        phone_accuracy_m="5", phone_app="phone-app", phone_time="12:00Z",
    )
    session_id = visit.run_visit(args, input_fn=lambda _: "OK", sleep=sleeps.append)
    canonical = reference.read_canonical(tmp_path / "results" / "referencias_canonicas.csv")
    assert session_id == 88
    assert sleeps == [60]
    assert registered and len(collection) == 1
    assert "--max-measurements" in collection[0][-1]
    command = collection[0][-1]
    assert command[0] == sys.executable
    assert command[command.index("--environment") + 1] == environment
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


FOUR_POINTS = ("P01", "P03", "P05", "P08")


def _insert_measurement(conn, session_id, point_id, seq, status="complete"):
    conn.execute(
        "INSERT INTO measurements (session_id, reference_point_id, seq, started_at, snapshot_status, "
        "reasons_json, access_mode, moni_status, servinfo_status, csurv_status, gnss_status, "
        "timings_json, anomalies_json, raw_json) VALUES (?, ?, ?, '2026-11-18T12:00:00+00:00', ?, '[]', "
        "'LTE', 'ok', 'ok', 'ok', 'ok', '{}', '[]', '{}')",
        (session_id, point_id, seq, status),
    )


def _write_canonical(path, points):
    with Path(path).open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=reference.CANONICAL_FIELDS)
        writer.writeheader()
        for index, point in enumerate(points):
            writer.writerow({"point": point, "lat": -26.9 + index * 0.001, "lon": -49.07, "uncertainty_m": 0.5})


def _acquired_collection(tmp_path, monkeypatch, points=FOUR_POINTS):
    _prepare_env(tmp_path, monkeypatch)
    paths = prepare.collection_paths(tmp_path / "data", "coleta")
    results = paths["results"]
    for subdir in prepare.SUBDIRS:
        (results / subdir).mkdir(parents=True)
    values = {
        "name": "coleta", "points": ",".join(points), "results": str(results), "db": str(paths["db"]),
        "canonical": str(paths["canonical"]), "created_utc": "2026-11-18T00:00:00+00:00",
    }
    (results / prepare.COLLECTION_NAME).write_text(
        "".join(f"{key}={values[key]}\n" for key in prepare.COLLECTION_FIELDS), encoding="utf-8",
    )
    _write_canonical(paths["canonical"], points)
    conn = db.connect(str(paths["db"]))
    for index, point in enumerate(points):
        point_id = db.add_reference_point(conn, point, -26.9 + index * 0.001, -49.07, coord_source="gnss_media_s1")
        for session in ("S1", "S2"):
            session_id = db.create_session(
                conn, mode="continuous", scenario="estatico", environment="indoor", modem_id="fn990",
                metadata={}, reference_point_id=point_id, campaign_plmn="72406",
                notes=f"campaign_tools:raspberrypi_os:{point}:{session}",
            )
            with conn:
                for seq in range(1, 4):
                    _insert_measurement(conn, session_id, point_id, seq)
            db.finish_session(conn, session_id, "completed")
            (results / "referencias" / f"{point}_{session}_fixes.csv").write_text("x\n", encoding="utf-8")
            (results / "logs" / f"{point}_{session}.log").write_text("x\n", encoding="utf-8")
    conn.close()
    for name in ("manifesto_campanha.txt", "metricas_repouso.csv", "metricas_sistema.csv"):
        (results / name).write_text("x\n", encoding="utf-8")
    return results, paths["db"]


def _run_check(capsys):
    code = check._main(["--campaign", "coleta"])
    return code, capsys.readouterr().out


def _session_sql(db_path, sql):
    conn = sqlite3.connect(str(db_path))
    conn.executescript(sql)
    conn.close()


@pytest.mark.parametrize("points", [FOUR_POINTS, prepare.VALID_POINTS])
def test_check_complete_collection_is_ok(tmp_path, monkeypatch, capsys, points):
    _acquired_collection(tmp_path, monkeypatch, points)
    code, out = _run_check(capsys)
    assert code == check.EXIT_OK
    sessions, measurements = 2 * len(points), 6 * len(points)
    assert f"Sessões esperadas: {sessions}\nSessões encontradas: {sessions}" in out
    assert f"Medições esperadas: {measurements}\nMedições encontradas: {measurements}" in out
    assert f"Complete: {measurements}\nPartial: 0\nInvalid: 0" in out
    assert "Banco: íntegro" in out and out.rstrip().endswith("Resultado: OK")


def test_check_is_read_only_and_creates_no_files(tmp_path, monkeypatch, capsys):
    results, db_path = _acquired_collection(tmp_path, monkeypatch)
    canonical = results / "referencias_canonicas.csv"
    before = {path: path.read_bytes() for path in results.rglob("*") if path.is_file()}
    assert _run_check(capsys)[0] == check.EXIT_OK
    after = {path: path.read_bytes() for path in results.rglob("*") if path.is_file()}
    assert after == before
    assert db_path in after and canonical in after


def test_check_pending_wal_is_error_without_creating_files(tmp_path, monkeypatch, capsys):
    results, db_path = _acquired_collection(tmp_path, monkeypatch)
    Path(f"{db_path}-wal").write_bytes(b"pending")
    before = {path: path.read_bytes() for path in results.rglob("*") if path.is_file()}
    code, out = _run_check(capsys)
    after = {path: path.read_bytes() for path in results.rglob("*") if path.is_file()}
    assert code == check.EXIT_ERROR and "WAL pendente" in out and "Resultado: ERRO" in out
    assert after == before and not Path(f"{db_path}-shm").exists()


@pytest.mark.parametrize("damage,message", [
    (lambda db_path: db_path.unlink(), "banco ausente"),
    (lambda db_path: db_path.write_bytes(b""), "banco vazio"),
    (lambda db_path: _session_sql(db_path, "PRAGMA user_version = 1"), "schema versao 1"),
    (lambda db_path: _session_sql(db_path, "DROP TABLE health_checks"), "tabelas ausentes: health_checks"),
    (lambda db_path: _session_sql(
        db_path, "INSERT INTO observed_cells (measurement_id, source, identity_status) VALUES (999, 'CSURV', 'none')",
    ), "foreign_key_check"),
])
def test_check_database_failures_are_errors(tmp_path, monkeypatch, capsys, damage, message):
    _, db_path = _acquired_collection(tmp_path, monkeypatch)
    damage(db_path)
    code, out = _run_check(capsys)
    assert code == check.EXIT_ERROR
    assert message in out and "Banco: com problemas" in out and "Resultado: ERRO" in out


def test_check_integrity_check_failure_is_error(tmp_path, monkeypatch, capsys):
    _acquired_collection(tmp_path, monkeypatch)
    real_open = check._open_read_only

    class Damaged:
        def __init__(self, path):
            self.conn = real_open(path)

        def execute(self, sql, *params):
            if sql == "PRAGMA integrity_check":
                return iter([("row 3 missing from index",)])
            return self.conn.execute(sql, *params)

        def close(self):
            self.conn.close()

    monkeypatch.setattr(check, "_open_read_only", Damaged)
    code, out = _run_check(capsys)
    assert code == check.EXIT_ERROR and "integrity_check: row 3 missing from index" in out


S1_P03 = "campaign_tools:raspberrypi_os:P03:S1"
P03_S1_SESSION = f"(SELECT session_id FROM sessions WHERE notes = '{S1_P03}')"


@pytest.mark.parametrize("sql,message", [
    ("DELETE FROM measurements WHERE reference_point_id = 2; DELETE FROM sessions WHERE reference_point_id = 2; "
     "DELETE FROM reference_points WHERE name = 'P03'", "pontos ausentes no banco: P03"),
    (f"DELETE FROM measurements WHERE session_id = {P03_S1_SESSION}; "
     f"DELETE FROM sessions WHERE notes = '{S1_P03}'", "P03/S1: sessao ausente"),
    ("DELETE FROM measurements WHERE session_id = (SELECT session_id FROM sessions WHERE notes = "
     "'campaign_tools:raspberrypi_os:P03:S2'); DELETE FROM sessions WHERE notes = "
     "'campaign_tools:raspberrypi_os:P03:S2'", "P03/S2: sessao ausente"),
    (f"UPDATE sessions SET status = 'interrupted' WHERE notes = '{S1_P03}'", "com status interrupted"),
    (f"DELETE FROM measurements WHERE seq = 3 AND session_id = {P03_S1_SESSION}", "P03/S1: 2 medicoes"),
    (f"INSERT INTO measurements (session_id, reference_point_id, seq, started_at, snapshot_status, reasons_json, "
     f"access_mode, moni_status, servinfo_status, csurv_status, gnss_status, timings_json, anomalies_json, "
     f"raw_json) SELECT {P03_S1_SESSION}, 2, 4, 'x', 'complete', '[]', 'LTE', 'ok', 'ok', 'ok', 'ok', '{{}}', "
     f"'[]', '{{}}'", "P03/S1: 4 medicoes"),
])
def test_check_incomplete_acquisition_is_error(tmp_path, monkeypatch, capsys, sql, message):
    _, db_path = _acquired_collection(tmp_path, monkeypatch)
    _session_sql(db_path, sql)
    code, out = _run_check(capsys)
    assert code == check.EXIT_ERROR
    assert message in out and "Aquisição: incompleta" in out and "Banco: íntegro" in out


def test_check_partial_snapshot_requires_review_and_keeps_database(tmp_path, monkeypatch, capsys):
    _, db_path = _acquired_collection(tmp_path, monkeypatch)
    _session_sql(db_path, "UPDATE measurements SET snapshot_status = 'partial' WHERE measurement_id IN (1, 2)")
    before = db_path.read_bytes()
    code, out = _run_check(capsys)
    assert code == check.EXIT_REVIEW
    assert "Partial: 2" in out and "Invalid: 0" in out and "Complete: 22" in out and "Banco: íntegro" in out
    assert "AVISO: P01/S1: 2 partial" in out
    assert out.rstrip().endswith("Resultado: REVISAR (2 snapshots partial)")
    assert db_path.read_bytes() == before


def test_check_any_invalid_snapshot_is_error_and_keeps_database(tmp_path, monkeypatch, capsys):
    _, db_path = _acquired_collection(tmp_path, monkeypatch)
    _session_sql(db_path, "UPDATE measurements SET snapshot_status = 'partial' WHERE measurement_id = 1")
    _session_sql(db_path, "UPDATE measurements SET snapshot_status = 'invalid' WHERE measurement_id = 5")
    before = db_path.read_bytes()
    code, out = _run_check(capsys)
    assert code == check.EXIT_ERROR
    assert "Invalid: 1" in out and "Partial: 1" in out and "Banco: íntegro" in out
    assert "ERRO: P01/S2: 1 snapshots invalid" in out
    assert out.rstrip().endswith("Resultado: ERRO (1 problemas)")
    assert db_path.read_bytes() == before


@pytest.mark.parametrize("damage,message,line", [
    (lambda r: (r / "referencias_canonicas.csv").unlink(), "referencias canonicas", "Referências: incompletas"),
    (lambda r: _write_canonical(r / "referencias_canonicas.csv", ("P01", "P03", "P08")),
     "referencias canonicas sem: P05", "Referências: incompletas"),
    (lambda r: (r / "referencias" / "P05_S2_fixes.csv").unlink(), "fixes ausentes", "Referências: incompletas"),
    (lambda r: (r / "logs" / "P08_S1.log").unlink(), "log ausente", "Evidências: incompletas"),
    (lambda r: (r / "manifesto_campanha.txt").unlink(), "manifesto_campanha.txt", "Evidências: incompletas"),
    (lambda r: (r / "metricas_repouso.csv").unlink(), "metricas_repouso.csv", "Evidências: incompletas"),
    (lambda r: (r / "metricas_sistema.csv").unlink(), "metricas_sistema.csv", "Evidências: incompletas"),
])
def test_check_missing_references_or_evidence_is_error(tmp_path, monkeypatch, capsys, damage, message, line):
    results, _ = _acquired_collection(tmp_path, monkeypatch)
    damage(results)
    code, out = _run_check(capsys)
    assert code == check.EXIT_ERROR and message in out and line in out


def _references(points):
    return {
        point: {"point": point, "lat": -26.9 + index * 0.001, "lon": -49.07, "uncertainty_m": 0.5}
        for index, point in enumerate(points)
    }


def test_separation_uses_explicit_points_only():
    assert len(reference.separation_rows(_references(FOUR_POINTS), FOUR_POINTS)) == 6
    assert len(reference.separation_rows(_references(reference.POINTS), reference.POINTS)) == 28
    with pytest.raises(ValueError, match="P02"):
        reference.separation_rows(_references(FOUR_POINTS))
    with pytest.raises(ValueError, match="ausentes para: P05"):
        reference.separation_rows(_references(("P01", "P03", "P08")), FOUR_POINTS)


def test_separation_campaign_writes_default_output_without_touching_canonical(tmp_path, monkeypatch, capsys):
    results, _ = _acquired_collection(tmp_path, monkeypatch)
    canonical = results / "referencias_canonicas.csv"
    original = canonical.read_bytes()
    assert reference._main(["separation", "--campaign", "coleta"]) == 0
    assert canonical.read_bytes() == original
    with (results / "separacao_pontos.csv").open(newline="", encoding="utf-8") as stream:
        pairs = [(row["point_a"], row["point_b"]) for row in csv.DictReader(stream)]
    assert len(pairs) == 6 and ("P01", "P03") in pairs
    assert "6 pares analisados" in capsys.readouterr().out
    assert reference._main(["separation", "--campaign", "coleta"]) == 1
    _write_canonical(canonical, ("P01", "P03", "P08"))
    (results / "separacao_pontos.csv").unlink()
    assert reference._main(["separation", "--campaign", "coleta"]) == 1
    assert "ausentes para: P05" in capsys.readouterr().err
    assert not (results / "separacao_pontos.csv").exists()


def test_separation_old_cli_still_requires_canonical_and_out():
    with pytest.raises(SystemExit) as exc:
        reference._main(["separation", "--canonical", "refs.csv"])
    assert exc.value.code == 2


REAL_P01 = {"lat": -26.8790459566667, "lon": -49.11330076, "uncertainty_m": 0.047814995826053}
REAL_P01_CSV = {"lat": "-26.8790459567", "lon": "-49.1133007600", "uncertainty_m": "0.048"}


def _s2_visit(tmp_path, monkeypatch, canonical=None, stored=REAL_P01):
    results = tmp_path / "results"
    results.mkdir()
    values = REAL_P01_CSV if canonical is None else canonical
    with (results / "referencias_canonicas.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=reference.CANONICAL_FIELDS)
        writer.writeheader()
        writer.writerow({"point": "P01", **values})
    db_path = tmp_path / "db.sqlite"
    conn = db.connect(str(db_path))
    point_id = db.add_reference_point(
        conn, "P01", stored["lat"], stored["lon"], coord_source="gnss_media_s1",
        coord_uncertainty_m=stored["uncertainty_m"],
    )
    conn.close()
    fixes = [{**fix, "lat": float(values["lat"]) + i * 1e-8, "lon": float(values["lon"])}
             for i, fix in enumerate(_fixes())]
    attempts = [{**fix, "valid": True, "reason": ""} for fix in fixes]
    monkeypatch.setattr(visit, "collect_valid_fixes", lambda port: (fixes, attempts))
    monkeypatch.setattr(visit.monitor, "run_collection", lambda *args: 0)
    used = []
    monkeypatch.setattr(visit, "_verify_collection", lambda _db, _prev, pid: used.append(pid) or (88, 3))
    args = SimpleNamespace(
        system="raspberrypi_os", point="P01", session="S2", db=str(db_path), port="/dev/ttyUSB2",
        plmn="72406", results=str(results), canonical=None, data_dir=None, interval_s=90,
        environment="indoor",
    )
    return args, results, db_path, point_id, used


def _stored_points(db_path):
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute("SELECT * FROM reference_points").fetchall()
    finally:
        conn.close()


def test_s2_reuses_point_accepting_csv_rounding_without_changing_database(tmp_path, monkeypatch):
    args, results, db_path, point_id, used = _s2_visit(tmp_path, monkeypatch)
    canonical = reference.read_canonical(results / "referencias_canonicas.csv")["P01"]
    assert canonical["uncertainty_m"] == 0.048 and canonical["lat"] == pytest.approx(REAL_P01["lat"], abs=1e-10)
    before = _stored_points(db_path)
    assert visit.run_visit(args, input_fn=lambda _: "OK", sleep=lambda _: None) == 88
    assert used == [point_id]
    assert _stored_points(db_path) == before
    assert (results / "referencias" / "P01_S2_fixes.csv").exists()


@pytest.mark.parametrize("field,value", [
    ("lat", REAL_P01["lat"] + 1e-7), ("lon", REAL_P01["lon"] - 1e-7),
    ("uncertainty_m", 0.049), ("uncertainty_m", 0.047),
])
def test_register_reference_rejects_real_divergence_but_accepts_csv_rounding(tmp_path, monkeypatch, field, value):
    args, results, db_path, point_id, _ = _s2_visit(tmp_path, monkeypatch)
    canonical = reference.read_canonical(results / "referencias_canonicas.csv")["P01"]
    assert visit._register_reference(db_path, "P01", canonical, "x") == point_id
    with pytest.raises(ValueError, match="diverge"):
        visit._register_reference(db_path, "P01", {**canonical, field: value}, "x")


def test_s2_with_divergent_database_fails_before_writing_artifacts(tmp_path, monkeypatch):
    args, results, db_path, _, _ = _s2_visit(
        tmp_path, monkeypatch, stored={**REAL_P01, "uncertainty_m": 0.5},
    )
    monkeypatch.setattr(visit, "collect_valid_fixes", lambda port: pytest.fail("não deve coletar fixes"))
    with pytest.raises(ValueError, match="diverge"):
        visit.run_visit(args, input_fn=lambda _: "OK", sleep=lambda _: None)
    assert not (results / "referencias").exists() and not (results / "resumo_referencia.csv").exists()


def test_s2_outside_tolerance_alerts_and_needs_manual_confirmation(tmp_path, monkeypatch, capsys):
    far = {**REAL_P01_CSV, "lat": "-26.8780459567"}
    args, results, db_path, _, used = _s2_visit(tmp_path, monkeypatch, canonical=far,
                                                stored={**REAL_P01, "lat": -26.8780459567})
    monkeypatch.setattr(visit, "collect_valid_fixes", lambda port: (
        [{**f, "lat": -26.8790459567 + i * 1e-8} for i, f in enumerate(_fixes())],
        [{**f, "lat": -26.8790459567 + i * 1e-8, "valid": True, "reason": ""} for i, f in enumerate(_fixes())],
    ))
    before = _stored_points(db_path)
    with pytest.raises(RuntimeError, match="visita cancelada"):
        visit.run_visit(args, input_fn=lambda _: "NAO", sleep=lambda _: None)
    assert "ALERTA" in capsys.readouterr().out and used == []
    assert _stored_points(db_path) == before


def _orphan_summary(results, *visits):
    rows = [{"system": "raspberrypi_os", "point": "P01", "session": session, "attempts": "5"} for session in visits]
    with (results / "resumo_referencia.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=visit.SUMMARY_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    (results / "referencias").mkdir(exist_ok=True)
    for session in visits:
        (results / "referencias" / f"P01_{session}_fixes.csv").write_text(f"old {session}\n", encoding="utf-8")


def test_s2_repeats_orphan_attempt_replacing_only_its_artifacts(tmp_path, monkeypatch):
    args, results, db_path, _, _ = _s2_visit(tmp_path, monkeypatch)
    _orphan_summary(results, "S1", "S2")
    assert visit.run_visit(args, input_fn=lambda _: "OK", sleep=lambda _: None) == 88
    with (results / "resumo_referencia.csv").open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    assert [(row["point"], row["session"]) for row in rows] == [("P01", "S1"), ("P01", "S2")]
    assert rows[0]["attempts"] == "5" and rows[1]["attempts"] == "5" and rows[1]["valid_fixes"] == "5"
    assert (results / "referencias" / "P01_S1_fixes.csv").read_text(encoding="utf-8") == "old S1\n"
    assert (results / "referencias" / "P01_S2_fixes.csv").read_text(encoding="utf-8").startswith("attempt,")


@pytest.mark.parametrize("existing", ["session", "log"])
def test_s2_cannot_repeat_existing_visit(tmp_path, monkeypatch, existing):
    args, results, db_path, point_id, _ = _s2_visit(tmp_path, monkeypatch)
    _orphan_summary(results, "S2")
    if existing == "session":
        conn = db.connect(str(db_path))
        db.create_session(
            conn, mode="continuous", scenario="estatico", environment="indoor", modem_id="fn990",
            metadata={}, reference_point_id=point_id, notes="campaign_tools:raspberrypi_os:P01:S2",
        )
        conn.close()
    else:
        (results / "logs").mkdir()
        (results / "logs" / "P01_S2.log").write_text("x\n", encoding="utf-8")
    monkeypatch.setattr(visit, "collect_valid_fixes", lambda port: pytest.fail("não deve coletar fixes"))
    with pytest.raises(FileExistsError):
        visit.run_visit(args, input_fn=lambda _: "OK", sleep=lambda _: None)
    assert (results / "referencias" / "P01_S2_fixes.csv").read_text(encoding="utf-8") == "old S2\n"


def _trainable_collection(tmp_path, monkeypatch, points=FOUR_POINTS):
    results, db_path = _acquired_collection(tmp_path, monkeypatch, points)
    conn = sqlite3.connect(str(db_path))
    for measurement_id, seq, point_id in conn.execute(
        "SELECT measurement_id, seq, reference_point_id FROM measurements"
    ).fetchall():
        for index in range(3):
            arfcn, pci, cell_id = 3000 + index, 10 + index, f"{index + 1:X}"
            conn.execute(
                "INSERT INTO observed_cells (measurement_id, source, rat, arfcn, pci, plmn, cell_id, rsrp_dbm, "
                "cell_status, record_format, radio_key, global_cell_key, identity_status) VALUES "
                "(?, 'CSURV', 'LTE', ?, ?, '72406', ?, ?, 'CELL_SUITABLE', 'full', ?, ?, 'complete')",
                (measurement_id, arfcn, pci, cell_id, -60.0 - 8 * index - 6 * abs(point_id - 1 - index) - seq,
                 f"LTE:{arfcn}:{pci}", f"LTE|72406|{cell_id}"),
            )
    conn.commit()
    conn.close()
    return results, db_path


def _wknn(capsys, *argv):
    code = wknn._main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _tree_bytes(root):
    return {path: path.read_bytes() for path in Path(root).rglob("*") if path.is_file()}


@pytest.mark.parametrize("by_path", [False, True])
def test_wknn_train_resolves_collection_and_writes_only_inside_it(tmp_path, monkeypatch, capsys, by_path):
    results, db_path = _trainable_collection(tmp_path, monkeypatch)
    campaign = str(results / prepare.COLLECTION_NAME) if by_path else "coleta"
    raw = {path: path.read_bytes() for path in (db_path, results / "collection.conf", results / "referencias_canonicas.csv")}
    before = set(_tree_bytes(tmp_path))
    code, out, err = _wknn(capsys, "train", "--campaign", campaign)
    assert code == 0, err
    created = set(_tree_bytes(tmp_path)) - before
    assert all(results in path.parents for path in created)
    models = results / "modelos"
    assert {path.name for path in models.iterdir()} == {
        f"{kind}_lte_{protocol}.json" for kind in ("wknn", "treino") for protocol in wknn.MODEL_PROTOCOLS}
    for protocol in ("spatial", "temporal"):
        assert {p.name for p in (results / "avaliacoes" / protocol).iterdir()} == {
            "resumo.csv", "predicoes.csv", "avaliacao.json"}
    assert [p.name for p in (results / "avaliacoes" / "complete").iterdir()] == ["resumo.json"]
    assert all(path.read_bytes() == content for path, content in raw.items())
    assert "Tentativas: 24\nComplete: 24\nPartial: 0\nInvalid: 0\nElegiveis: 24" in out
    assert "Complete:\n- treinado com todos os fingerprints elegiveis; nao possui avaliacao independente" in out
    assert "NR:\n- nao treinado: dados elegiveis insuficientes" in out
    assert "python -m campaign_tools.wknn deploy --campaign" in out


def test_wknn_complete_summary_has_only_training_facts(tmp_path, monkeypatch, capsys):
    results, _ = _trainable_collection(tmp_path, monkeypatch)
    assert _wknn(capsys, "train", "--campaign", "coleta")[0] == 0
    summary = json.loads((results / "avaliacoes" / "complete" / "resumo.json").read_text(encoding="utf-8"))
    bundle = json.loads((results / "modelos" / "wknn_lte_complete.json").read_text(encoding="utf-8"))
    assert (summary["protocol"], summary["evaluation_available"]) == ("complete", False)
    assert summary["n_eligible_fingerprints"] == 24 == len(bundle["fingerprints"]["measurement_id"])
    assert summary["n_attempts_considered"] == 24 and summary["points"] == list(FOUR_POINTS)
    assert len(summary["session_ids"]) == 8 and summary["hyperparameter_source"] == "grouped_cv_all_eligible"
    assert not {"mean_m", "rmse_m", "p90_m", "p95_m", "coverage"} & set(summary)
    assert "dataset" in bundle and "test_samples_sha256" not in bundle["dataset"]
    assert (bundle["protocol"], bundle["evaluation_available"]) == ("complete", False)
    temporal = json.loads((results / "avaliacoes" / "temporal" / "avaliacao.json").read_text(encoding="utf-8"))
    split = json.loads((results / "modelos" / "wknn_lte_temporal.json").read_text(encoding="utf-8"))["validation"]["split"]
    assert temporal["test_groups"] == split["test_groups"] == [2, 4, 6, 8]  # S2 de cada ponto
    assert split["selection_groups"] == [1, 3, 5, 7] and temporal["n_attempts"] == 12
    assert (results / "avaliacoes" / "spatial" / "avaliacao.json").read_text(encoding="utf-8")


def test_wknn_train_calls_training_and_evaluation_in_order(tmp_path, monkeypatch, capsys):
    results, db_path = _trainable_collection(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(wknn.train, "main", lambda argv: calls.append(("train", argv)) or Path(
        argv[argv.index("--out") + 1]).write_text("{}", encoding="utf-8"))
    monkeypatch.setattr(wknn.evaluate, "main", lambda argv: calls.append(("evaluate", argv)) or Path(
        argv[argv.index("--json-out") + 1]).write_text(json.dumps(
            {"group_column": "g", "test_groups": [1], "methods": {"wknn": {
                "n_valid": 1, "n_attempts": 1, "operational_coverage": 1.0, "mean_m": 1.0, "median_m": 1.0,
                "rmse_m": 1.0, "p90_m": 1.0, "p95_m": 1.0}}}), encoding="utf-8"))
    monkeypatch.setattr(wknn, "_complete_summary", lambda *args: {"protocol": "complete"})
    assert _wknn(capsys, "train", "--campaign", "coleta")[0] == 0
    assert [(kind, argv[argv.index("--protocol") + 1] if kind == "train" else argv[argv.index("--model") + 1])
            for kind, argv in calls] == [
        ("train", "spatial"), ("evaluate", str(results / "modelos" / "wknn_lte_spatial.json")),
        ("train", "temporal"), ("evaluate", str(results / "modelos" / "wknn_lte_temporal.json")),
        ("train", "complete")]
    train_argv = calls[0][1]
    assert train_argv[train_argv.index("--db") + 1] == str(db_path)
    assert train_argv[train_argv.index("--campaign-plmn") + 1] == "72406"
    assert train_argv[train_argv.index("--report") + 1] == str(results / "modelos" / "treino_lte_spatial.json")
    evaluate_argv = calls[1][1]
    assert evaluate_argv[evaluate_argv.index("--json-out") + 1] == str(results / "avaliacoes" / "spatial" / "avaliacao.json")


def test_wknn_partial_follows_existing_eligibility_and_invalid_blocks(tmp_path, monkeypatch, capsys):
    results, db_path = _trainable_collection(tmp_path, monkeypatch)
    _session_sql(db_path, "UPDATE measurements SET snapshot_status='partial' WHERE measurement_id IN (1, 2);"
                          "UPDATE measurements SET csurv_status='empty' WHERE measurement_id = 2;"
                          "DELETE FROM observed_cells WHERE measurement_id = 2")
    code, out, err = _wknn(capsys, "train", "--campaign", "coleta")
    assert code == 0, err
    assert "Complete: 22\nPartial: 2\nInvalid: 0\nElegiveis: 23" in out  # só o partial com #CSURV e células entra
    shutil.rmtree(results / "modelos")
    shutil.rmtree(results / "avaliacoes")
    (results / "modelos").mkdir()
    (results / "avaliacoes").mkdir()
    _session_sql(db_path, "UPDATE measurements SET snapshot_status='invalid' WHERE measurement_id = 3")
    code, out, err = _wknn(capsys, "train", "--campaign", "coleta")
    assert code == 1 and "invalid" in err and "Treino nao concluido" in err
    assert not any((results / "modelos").iterdir()) and not any((results / "avaliacoes").iterdir())


def test_wknn_nr_without_eligible_data_fails_and_lte_is_not_affected(tmp_path, monkeypatch, capsys):
    results, _ = _trainable_collection(tmp_path, monkeypatch)
    code, out, err = _wknn(capsys, "train", "--campaign", "coleta", "--technology", "NR")
    assert code == 1 and "NR nao treinado: dados elegiveis insuficientes" in err
    assert not any((results / "modelos").iterdir()) and not any((results / "avaliacoes").iterdir())
    assert _wknn(capsys, "train", "--campaign", "coleta")[0] == 0


def test_wknn_refuses_overwrite_and_force_replaces_only_derived_artifacts(tmp_path, monkeypatch, capsys):
    results, db_path = _trainable_collection(tmp_path, monkeypatch)
    assert _wknn(capsys, "train", "--campaign", "coleta")[0] == 0
    (results / "export" / "sessoes.csv").write_text("keep\n", encoding="utf-8")
    (results / "avaliacoes" / "notas.txt").write_text("keep\n", encoding="utf-8")
    (results / "avaliacoes" / "spatial" / "extra.txt").write_text("derived\n", encoding="utf-8")
    (results / "modelos" / "deployed").mkdir()
    (results / "modelos" / "deployed" / "wknn_lte.json").write_text("keep\n", encoding="utf-8")
    before = _tree_bytes(results)
    code, out, err = _wknn(capsys, "train", "--campaign", "coleta")
    assert code == 1 and "--force" in err and _tree_bytes(results) == before
    code, out, err = _wknn(capsys, "train", "--campaign", "coleta", "--force")
    assert code == 0, err
    after = _tree_bytes(results)
    derived_dirs = {results / "avaliacoes" / protocol for protocol in wknn.MODEL_PROTOCOLS}

    def derived(path):
        return path.parent in derived_dirs or (
            path.parent == results / "modelos" and path.name.startswith(("wknn_lte_", "treino_lte_")))

    assert any(not derived(path) for path in before)
    for path, content in before.items():
        if not derived(path):
            assert after[path] == content, path
    assert not (results / "avaliacoes" / "spatial" / "extra.txt").exists()


def test_wknn_intermediate_failure_never_reports_success(tmp_path, monkeypatch, capsys):
    results, _ = _trainable_collection(tmp_path, monkeypatch)
    real = wknn.evaluate.main

    def failing(argv):
        if "wknn_lte_temporal.json" in " ".join(argv):
            raise SystemExit("erro: avaliacao indisponivel")
        return real(argv)

    monkeypatch.setattr(wknn.evaluate, "main", failing)
    code, out, err = _wknn(capsys, "train", "--campaign", "coleta")
    assert code == 1 and "etapa 'avaliacao LTE temporal' falhou" in err and "Treino nao concluido" in err
    assert "Proximo comando" not in out and "Complete:\n-" not in out
    assert not (results / "modelos" / "wknn_lte_complete.json").exists()
    assert not (results / "modelos" / "deployed").exists()


@pytest.fixture
def trained(tmp_path, monkeypatch, capsys):
    results, _ = _trainable_collection(tmp_path, monkeypatch)
    assert _wknn(capsys, "train", "--campaign", "coleta")[0] == 0
    return results


@pytest.mark.parametrize("model", [None, "spatial", "temporal", "complete"])
def test_wknn_deploy_selects_model_and_registers_it(trained, tmp_path, capsys, model):
    runtime_conf = tmp_path / "runtime.conf"
    runtime_conf.write_text("TCC_MODE=manual\n", encoding="utf-8")
    args = ["deploy", "--campaign", "coleta"] + ([] if model is None else ["--model", model])
    code, out, err = _wknn(capsys, *args)
    assert code == 0, err
    selected = model or "complete"
    source = trained / "modelos" / f"wknn_lte_{selected}.json"
    deployed = trained / "modelos" / "deployed" / "wknn_lte.json"
    record = json.loads((trained / "modelos" / "deployed" / "deployment.json").read_text(encoding="utf-8"))
    assert deployed.read_bytes() == source.read_bytes()
    bundle = json.loads(source.read_text(encoding="utf-8"))
    assert record == {
        "campaign": "coleta", "technology": "LTE", "selected_model": selected, "source_bundle": str(source),
        "source_bundle_sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "deployed_bundle": str(deployed),
        "deployed_utc": record["deployed_utc"], "protocol": selected,
        "evaluation_available": selected != "complete",
        "training_data_hash": bundle["dataset"].get("training_samples_sha256") or bundle["dataset"]["selection_samples_sha256"],
    }
    assert record["training_data_hash"] and datetime_is_utc(record["deployed_utc"])
    assert runtime_conf.read_text(encoding="utf-8") == "TCC_MODE=manual\n"
    for protocol in wknn.MODEL_PROTOCOLS:
        assert (trained / "modelos" / f"wknn_lte_{protocol}.json").is_file()
    assert not list((trained / "modelos" / "deployed").glob("*.tmp"))


def datetime_is_utc(text):
    from datetime import datetime
    return datetime.fromisoformat(text).utcoffset().total_seconds() == 0


def test_wknn_deploy_copy_is_atomic_and_keeps_previous_bundle_on_failure(trained, monkeypatch, capsys):
    assert _wknn(capsys, "deploy", "--campaign", "coleta")[0] == 0
    deployed = trained / "modelos" / "deployed" / "wknn_lte.json"
    previous = deployed.read_bytes()
    replaced = []
    real_replace = wknn.os.replace

    def spy(source, target):
        replaced.append((Path(source), Path(target)))
        raise OSError("falha simulada")

    monkeypatch.setattr(wknn.os, "replace", spy)
    code, out, err = _wknn(capsys, "deploy", "--campaign", "coleta", "--model", "temporal")
    assert code == 1 and "falha simulada" in err
    assert deployed.read_bytes() == previous and not list(deployed.parent.glob("*.tmp"))
    assert replaced[0][0].parent == replaced[0][1].parent and replaced[0][0].suffix == ".tmp"
    monkeypatch.setattr(wknn.os, "replace", real_replace)
    assert _wknn(capsys, "deploy", "--campaign", "coleta", "--model", "temporal")[0] == 0
    assert deployed.read_bytes() == (trained / "modelos" / "wknn_lte_temporal.json").read_bytes()


def test_wknn_deploy_does_not_touch_hardware_and_needs_a_trained_bundle(tmp_path, monkeypatch, capsys):
    results, _ = _trainable_collection(tmp_path, monkeypatch)
    from hal.fn990_driver import FN990Driver
    monkeypatch.setattr(FN990Driver, "connect", lambda *a, **k: pytest.fail("deploy nao acessa o modem"))
    code, out, err = _wknn(capsys, "deploy", "--campaign", "coleta")
    assert code == 1 and "execute 'campaign_tools.wknn train' antes" in err
    assert not (results / "modelos" / "deployed").exists()


RUNTIME_KEYS = ("TCC_MODE", "TCC_METHOD", "TCC_PORT", "TCC_INTERVAL", "TCC_MODEL", "TCC_REGISTRY", "TCC_OUTPUT",
                "TCC_HISTORY", "TCC_LOG", "TCC_DEBUG", "TCC_METRICS", "TCC_SNAPSHOTS_DIR", "TCC_RUN_MANIFEST")


def _runtime_values(path):
    return dict(line.split("=", 1) for line in Path(path).read_text(encoding="utf-8").splitlines()
                if line and not line.startswith("#"))


def _deploy(capsys, *extra, model=None):
    args = ["deploy", "--campaign", "coleta", *extra] + ([] if model is None else ["--model", model])
    return _wknn(capsys, *args)


def _fake_live(monkeypatch, status="valid"):
    calls = []

    def run_estimate(method, port, source, baudrate=115200):
        calls.append({"method": method, "port": port, "source": source})
        return PositionEstimate("wknn", status, lat=-26.9, lon=-49.07, x_m=1.0, y_m=2.0, technology="LTE",
                                reason=None if status == "valid" else "sem estimativa",
                                snapshot_status="complete",
                                metadata={"snapshot": {"started_at": "t", "reasons": [], "access_mode": "LTE",
                                                       "serving_global_cell_key": None, "timings_s": {}},
                                          "timings_s": {"load_s": 0.0, "estimate_s": 0.0, "end_to_end_s": 0.0},
                                          "n_observed_keys": 1, "n_ignored_keys": 0})

    monkeypatch.setattr(position.live, "run_estimate", run_estimate)
    monkeypatch.setattr(position.live.FN990Driver, "connect",
                        lambda *a, **k: pytest.fail("os testes nao acessam o modem"))
    return calls


@pytest.mark.parametrize("model", ["spatial", "temporal", "complete"])
def test_position_with_campaign_resolves_each_trained_model(trained, monkeypatch, capsys, model):
    calls = _fake_live(monkeypatch)
    code, out, err = _wknn_position(capsys, "--campaign", "coleta", "--model", model, "--json")
    assert code == 0, err
    assert calls == [{"method": "wknn", "port": "/dev/ttyUSB2",
                      "source": str(trained / "modelos" / f"wknn_lte_{model}.json")}]
    assert json.loads(out)["method"] == "wknn"


def _wknn_position(capsys, *argv):
    code = position._main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.mark.parametrize("extra", [[], ["--model", "deployed"]])
def test_position_uses_deployed_by_default_and_verifies_deployment(trained, monkeypatch, capsys, extra):
    calls = _fake_live(monkeypatch)
    code, out, err = _wknn_position(capsys, "--campaign", "coleta", *extra, "--json")
    assert code == 2 and "deploy ausente" in err and not calls
    assert _deploy(capsys, model="temporal")[0] == 0
    code, out, err = _wknn_position(capsys, "--campaign", "coleta", *extra, "--json")
    assert code == 0, err
    assert calls[0]["source"] == str(trained / "modelos" / "deployed" / "wknn_lte.json")
    assert "deployed (temporal)" in err


def test_position_refuses_deployed_bundle_with_divergent_hash(trained, monkeypatch, capsys):
    calls = _fake_live(monkeypatch)
    assert _deploy(capsys)[0] == 0
    deployed = trained / "modelos" / "deployed" / "wknn_lte.json"
    deployed.write_bytes(deployed.read_bytes() + b" ")
    code, out, err = _wknn_position(capsys, "--campaign", "coleta", "--json")
    assert code == 2 and "hash divergente" in err and not calls


def test_position_without_campaign_uses_runtime_conf_in_manual_mode(trained, monkeypatch, capsys):
    calls = _fake_live(monkeypatch)
    assert _deploy(capsys)[0] == 0
    runtime = config.user_runtime_config_path()
    values = _runtime_values(runtime)
    assert values["TCC_MODE"] == "manual"
    code, out, err = _wknn_position(capsys, "--json")
    assert code == 0, err
    assert calls == [{"method": "wknn", "port": values["TCC_PORT"], "source": values["TCC_MODEL"]}]
    assert _runtime_values(runtime) == values  # modo e arquivo intactos


def test_position_requires_wknn_runtime_and_clear_errors_without_it(trained, monkeypatch, capsys):
    calls = _fake_live(monkeypatch)
    code, out, err = _wknn_position(capsys, "--json")
    assert code == 2 and "runtime.conf ausente" in err
    assert _deploy(capsys)[0] == 0
    runtime = config.user_runtime_config_path()
    runtime.write_text(runtime.read_text(encoding="utf-8").replace("TCC_METHOD=wknn", "TCC_METHOD=cell-id"),
                       encoding="utf-8")
    code, out, err = _wknn_position(capsys, "--json")
    assert code == 2 and "TCC_METHOD=cell-id" in err and not calls
    code, out, err = _wknn_position(capsys, "--model", "complete", "--json")
    assert code == 2 and "--model exige --campaign" in err


def test_position_technical_overrides_take_precedence(trained, monkeypatch, capsys):
    calls = _fake_live(monkeypatch)
    assert _deploy(capsys)[0] == 0
    other = trained / "modelos" / "wknn_lte_spatial.json"
    code, out, err = _wknn_position(capsys, "--port", "/dev/ttyACM0", "--bundle", str(other), "--json")
    assert code == 0, err
    code, out, err = _wknn_position(capsys, "--campaign", "coleta", "--model", "complete", "--port", "/dev/ttyUSB9",
                                    "--bundle", str(other), "--json")
    assert code == 0, err
    assert [(c["port"], c["source"]) for c in calls] == [("/dev/ttyACM0", str(other)), ("/dev/ttyUSB9", str(other))]


def test_position_out_writes_json_atomically_and_refuses_existing(trained, monkeypatch, capsys, tmp_path):
    _fake_live(monkeypatch)
    out_path = tmp_path / "live" / "complete" / "P01_T1.json"
    before = _tree_bytes(trained)
    argv = ("--campaign", "coleta", "--model", "complete", "--json", "--out", str(out_path))
    code, out, err = _wknn_position(capsys, *argv)
    assert code == 0 and json.loads(out_path.read_text(encoding="utf-8")) == json.loads(out)
    assert not list(out_path.parent.glob("*.tmp")) and _tree_bytes(trained) == before
    first = out_path.read_bytes()
    out_path.write_text("antigo\n", encoding="utf-8")
    code, out, err = _wknn_position(capsys, *argv)
    assert code == 2 and "ja existe" in err and out_path.read_text(encoding="utf-8") == "antigo\n"
    code, out, err = _wknn_position(capsys, *argv, "--force")
    assert code == 0 and out_path.read_bytes() == first and _tree_bytes(trained) == before


def test_position_reports_unavailable_hardware_and_no_estimate(trained, monkeypatch, capsys):
    def no_modem(*args, **kwargs):
        raise position.live.LiveHardwareError("sem porta")

    monkeypatch.setattr(position.live, "run_estimate", no_modem)
    code, out, err = _wknn_position(capsys, "--campaign", "coleta", "--model", "complete")
    assert code == 1 and "hardware indisponivel: sem porta" in err
    _fake_live(monkeypatch, status="unavailable")
    assert _wknn_position(capsys, "--campaign", "coleta", "--model", "complete", "--json")[0] == 1


@pytest.mark.parametrize("model", [None, "spatial", "temporal"])
def test_deploy_prepares_manual_runtime_conf_pointing_to_deployed_bundle(trained, monkeypatch, capsys, model):
    monkeypatch.setattr(runtime_daemon, "main", lambda *a, **k: pytest.fail("deploy nao inicia o daemon"))
    monkeypatch.setattr(runtime_daemon, "run_loop", lambda *a, **k: pytest.fail("deploy nao inicia o daemon"))
    before = {path: data for path, data in _tree_bytes(trained).items() if "deployed" not in path.parts}
    code, out, err = _deploy(capsys, model=model)
    assert code == 0, err
    runtime = config.user_runtime_config_path()
    values = _runtime_values(runtime)
    deployed = trained / "modelos" / "deployed" / "wknn_lte.json"
    selected = model or "complete"
    assert set(values) == set(RUNTIME_KEYS)
    assert (values["TCC_MODE"], values["TCC_METHOD"], values["TCC_PORT"], values["TCC_INTERVAL"],
            values["TCC_MODEL"], values["TCC_REGISTRY"]) == ("manual", "wknn", "/dev/ttyUSB2", "90", str(deployed), "")
    assert values["TCC_OUTPUT"].startswith(str(config.find_config().parent / "data" / "runtime"))
    assert runtime_daemon.load_config(str(runtime)).mode == "manual"
    assert (values["TCC_DEBUG"], values["TCC_METRICS"], values["TCC_SNAPSHOTS_DIR"], values["TCC_RUN_MANIFEST"]) == (
        "0", "", "", "")  # o deploy normal nunca ativa o debug
    assert runtime_daemon.load_config(str(runtime)).debug is False
    assert deployed.read_bytes() == (trained / "modelos" / f"wknn_lte_{selected}.json").read_bytes()
    assert "runtime.conf preparado" in out and "Modo: manual" in out and "python -m campaign_tools.position" in out
    assert {path: data for path, data in _tree_bytes(trained).items() if "deployed" not in path.parts} == before


def test_runtime_conf_resolution_order_explicit_user_system_then_system_default(tmp_path, monkeypatch):
    _prepare_env(tmp_path, monkeypatch)
    system, user, explicit = config.SYSTEM_RUNTIME_CONFIG, config.user_runtime_config_path(), tmp_path / "x.conf"
    assert config.find_runtime_config() is None
    system.parent.mkdir(parents=True)
    system.write_text("s\n", encoding="utf-8")
    assert config.find_runtime_config() == system  # Buildroot: sem arquivo do usuario, /etc/tcc e encontrado
    user.parent.mkdir(parents=True)
    user.write_text("u\n", encoding="utf-8")
    assert config.find_runtime_config() == user  # o arquivo do usuario vence o global antigo
    explicit.write_text("e\n", encoding="utf-8")
    assert config.find_runtime_config(explicit) == explicit
    with pytest.raises(FileNotFoundError):
        config.find_runtime_config(tmp_path / "ausente.conf")
    assert config.default_runtime_config("raspberrypi_os") == user  # sem sudo
    assert config.default_runtime_config("buildroot") == system


@pytest.mark.parametrize("system_name,expected", [("raspberrypi_os", "user"), ("buildroot", "system")])
def test_deploy_creates_runtime_conf_where_the_system_requires(trained, capsys, tmp_path, system_name, expected):
    location = config.load_config(config.SYSTEM_CONFIG)
    config.save_config(config.SYSTEM_CONFIG, {**location, "system": system_name}, force=True)
    user, system = config.user_runtime_config_path(), config.SYSTEM_RUNTIME_CONFIG
    code, out, err = _deploy(capsys)
    assert code == 0, err
    created, other = (user, system) if expected == "user" else (system, user)
    assert created.is_file() and not other.exists()


def test_deploy_prefers_the_user_runtime_conf_over_an_old_system_one(trained, capsys):
    system, user = config.SYSTEM_RUNTIME_CONFIG, config.user_runtime_config_path()
    assert _deploy(capsys)[0] == 0 and user.is_file()  # sem nenhum arquivo, cria o do usuario
    system.parent.mkdir(parents=True)
    system.write_text("TCC_MODE=runtime\n", encoding="utf-8")  # global antigo: nao deve ser tocado nem exigir sudo
    code, out, err = _deploy(capsys, model="spatial")
    assert code == 0, err
    assert str(user) in out and system.read_text(encoding="utf-8") == "TCC_MODE=runtime\n"
    assert _runtime_values(user)["TCC_MODE"] == "manual"
    user.unlink()
    code, out, err = _deploy(capsys)  # sem arquivo do usuario, o /etc/tcc existente e o encontrado
    assert code == 1 and "incompativel" in err and str(system) in err and not user.exists()


def test_deploy_no_runtime_config_keeps_previous_behavior(trained, capsys, tmp_path):
    code, out, err = _deploy(capsys, "--no-runtime-config")
    assert code == 0, err
    assert not config.user_runtime_config_path().exists() and "runtime.conf nao alterado" in out
    explicit = tmp_path / "custom" / "runtime.conf"
    code, out, err = _deploy(capsys, "--runtime-config", str(explicit))
    assert code == 0 and explicit.is_file() and not config.user_runtime_config_path().exists()
    assert f"--runtime-config {explicit}" in out
    with pytest.raises(SystemExit):
        _deploy(capsys, "--runtime-config", str(explicit), "--no-runtime-config")


def test_deploy_updates_only_managed_fields_of_an_existing_runtime_conf(trained, capsys):
    runtime = config.user_runtime_config_path()
    runtime.parent.mkdir(parents=True, exist_ok=True)
    runtime.write_text(
        "# minha configuracao\nTCC_MODE=manual\nTCC_METHOD=cell-id\nTCC_PORT=/dev/ttyUSB0\nTCC_INTERVAL=240\n"
        "TCC_MODEL=/antigo.json\nTCC_REGISTRY=/data/registro.json\nTCC_OUTPUT=/x/latest.json\n"
        "TCC_HISTORY=/x/history.jsonl\nTCC_LOG=/x/runtime.log\n", encoding="utf-8")
    original = runtime.read_bytes()
    code, out, err = _deploy(capsys)
    assert code == 0, err
    values = _runtime_values(runtime)
    assert (values["TCC_MODE"], values["TCC_METHOD"], values["TCC_PORT"], values["TCC_INTERVAL"]) == (
        "manual", "wknn", "/dev/ttyUSB2", "90")
    assert (values["TCC_REGISTRY"], values["TCC_OUTPUT"], values["TCC_LOG"]) == (
        "/data/registro.json", "/x/latest.json", "/x/runtime.log")
    assert runtime.read_text(encoding="utf-8").startswith("# minha configuracao\n")
    assert runtime.with_name("runtime.conf.bak").read_bytes() == original
    assert "TCC_METHOD: cell-id -> wknn" in out
    assert values["TCC_DEBUG"] == "0" and "TCC_DEBUG: (novo) -> 0" in out  # chave ausente recebe o padrao normal


def test_deploy_preserves_an_existing_debug_choice_in_runtime_conf(trained, capsys):
    runtime = config.user_runtime_config_path()
    runtime.parent.mkdir(parents=True, exist_ok=True)
    runtime.write_text(
        "TCC_MODE=manual\nTCC_METHOD=wknn\nTCC_PORT=/dev/ttyUSB0\nTCC_INTERVAL=240\nTCC_MODEL=/antigo.json\n"
        "TCC_REGISTRY=\nTCC_OUTPUT=/x/latest.json\nTCC_HISTORY=/x/history.jsonl\nTCC_LOG=/x/runtime.log\n"
        "TCC_DEBUG=1\nTCC_METRICS=/x/m.csv\n", encoding="utf-8")
    assert _deploy(capsys)[0] == 0
    values = _runtime_values(runtime)
    assert (values["TCC_DEBUG"], values["TCC_METRICS"]) == ("1", "/x/m.csv")
    assert (values["TCC_SNAPSHOTS_DIR"], values["TCC_RUN_MANIFEST"]) == ("", "")
    assert values["TCC_MODE"] == "manual" and runtime_daemon.load_config(str(runtime)).debug is True


@pytest.mark.parametrize("content,message", [
    ("TCC_MODE=runtime\n" + "".join(f"{k}=x\n" for k in RUNTIME_KEYS[1:9]), "TCC_MODE=runtime"),
    ("TCC_MODE=manual\nOUTRA=1\n", "incompativel"),
])
def test_deploy_does_not_overwrite_incompatible_runtime_conf(trained, capsys, content, message):
    runtime = config.user_runtime_config_path()
    runtime.parent.mkdir(parents=True, exist_ok=True)
    runtime.write_text(content, encoding="utf-8")
    code, out, err = _deploy(capsys)
    assert code == 1 and message in err and runtime.read_text(encoding="utf-8") == content
    assert not (trained / "modelos" / "deployed").exists()


# ------------------------------------------------------------------------------------ validate
CELLS = ((3000, 10, "1", -60.0), (3001, 11, "2", -68.0), (3002, 12, "3", -76.0))


def _cell(rat="LTE", arfcn=3000, pci=10, cell_id="1", rsrp=-60.0):
    return CellObservation(rat=rat, source="CSURV", arfcn=arfcn, pci=pci, plmn="72406", cell_id=cell_id,
                           rsrp_dbm=rsrp, cell_status="CELL_SUITABLE", record_format="full")


def _snapshot(status="complete", cells=None, started="2026-11-26T12:00:00.000Z", transport_error=None):
    observations = [_cell(arfcn=a, pci=p, cell_id=c, rsrp=r) for a, p, c, r in CELLS] if cells is None else cells
    return Snapshot(
        status=status, reasons=[] if status == "complete" else ["gnss:no_fix"], started_at=started, access_mode="LTE",
        serving=ServingCellResolution(method="csurv", rat="LTE", radio_key="LTE:3000:10",
                                      global_cell_key="LTE:72406:1"),
        serving_rsrp_dbm=-60.0, serving_rsrq_db=-10.0, serving_sinr_db=None, observations=observations,
        moni=MoniReading(status="ok"), servinfo=ServInfoReading(status="ok"),
        csurv=CsurvReading(status="error" if status == "invalid" else "ok", observations=observations),
        gnss=GnssReading(status="ok"),
        timings_s={"MONI": 0.1, "SERVINFO": 0.2, "CSURV": 2.0, "GNSS": 0.3, "total": 2.6},
        anomalies=["a"], transport_error=transport_error,
    )


class _Rig:
    """Scripted modem: records connect/snapshot/close and hands out the given snapshots in order."""

    def __init__(self, monkeypatch, snapshots, fail_connect=(), snapshot_error=None):
        self.events, self.taken, self.snapshots = [], [], list(snapshots)
        self.fail_connect, self.connects, self.snapshot_error = set(fail_connect), 0, snapshot_error
        rig = self

        class Driver:
            def connect(self, port, *args, **kwargs):
                rig.connects += 1
                rig.events.append(("connect", port))
                if rig.connects in rig.fail_connect:
                    raise ConnectionError("sem porta")

            def close(self):
                rig.events.append(("close",))

        def take(driver):
            rig.events.append(("snapshot",))
            if rig.snapshot_error:
                raise rig.snapshot_error
            rig.taken.append(rig.snapshots.pop(0))
            return rig.taken[-1]

        monkeypatch.setattr(validate, "FN990Driver", Driver)
        monkeypatch.setattr(validate, "take_snapshot", take)
        self.estimates = []
        real = validate.estimate_from_snapshot

        def spy(snapshot, model=None, registry=None):
            self.estimates.append((snapshot, model.bundle["protocol"]))
            return real(snapshot, model=model)

        monkeypatch.setattr(validate, "estimate_from_snapshot", spy)


def _vargs(**over):
    values = dict(campaign="coleta", config=None, output=None, point="P01", repetitions=3,
                  models="spatial,temporal,complete", stabilization_s=0, between_repetitions_s=0,
                  metrics_interval_s=0.01, force=False)
    return SimpleNamespace(**{**values, **over})


def _run_point(args, answer="OK"):
    return validate.run_point(args, input_fn=lambda _: answer, sleep=lambda seconds: None)


def _validate_point(monkeypatch, point="P01", snapshots=None, **over):
    rig = _Rig(monkeypatch, snapshots if snapshots is not None else [_snapshot(started=f"t{i}") for i in range(12)])
    _run_point(_vargs(point=point, **over))
    return rig


def _rows(path):
    with Path(path).open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def _vdir(results):
    return results / "avaliacoes" / "validation"


def _without_validation(root):
    return {path: data for path, data in _tree_bytes(root).items() if "validation" not in path.parts}


def test_validate_shares_one_snapshot_between_the_three_models(trained, monkeypatch):
    rig = _validate_point(monkeypatch)
    out = _vdir(trained)
    assert len(rig.taken) == 3 and len(rig.estimates) == 9  # 1 snapshot (1 CSURV) por repeticao, 3 modelos cada
    for index, snapshot in enumerate(rig.taken):
        used = [(model, s) for s, model in rig.estimates[index * 3:index * 3 + 3]]
        assert {id(s) for _, s in used} == {id(snapshot)} and {m for m, _ in used} == {"spatial", "temporal", "complete"}
    acquisitions, predictions = _rows(out / "acquisitions.csv"), _rows(out / "predictions.csv")
    assert len(acquisitions) == 3 and len(predictions) == 9
    assert len(list((out / "fingerprints").glob("P01_T*.json"))) == 3
    for row in acquisitions:
        trio = [p for p in predictions if p["snapshot_id"] == row["snapshot_id"]]
        assert sorted(p["model"] for p in trio) == ["complete", "spatial", "temporal"]
        assert row["snapshot_id"].startswith(f"raspberrypi_os:coleta:P01:T{row['repetition']}:")
    assert len({p["snapshot_id"] for p in predictions}) == 3


def test_validate_opens_and_closes_the_port_per_repetition_and_loads_bundles_once(trained, monkeypatch):
    loads = []
    real = validate.load_bundle
    monkeypatch.setattr(validate, "load_bundle", lambda path: loads.append(path) or real(path))
    rig = _validate_point(monkeypatch)
    assert rig.events == [("connect", "/dev/ttyUSB2"), ("snapshot",), ("close",)] * 3
    assert len(loads) == 3  # um por modelo, nao um por predicao


def test_validate_closes_the_port_when_the_snapshot_fails(trained, monkeypatch):
    rig = _Rig(monkeypatch, [], snapshot_error=OSError("porta caiu"))
    _run_point(_vargs(repetitions=2))
    assert rig.events == [("connect", "/dev/ttyUSB2"), ("snapshot",), ("close",)] * 2
    failures = _rows(_vdir(trained) / "failures.csv")
    assert [(f["stage"], f["status"]) for f in failures] == [("snapshot", "error")] * 2
    assert not (_vdir(trained) / "predictions.csv").exists()


def test_validate_uses_the_canonical_reference_and_the_existing_geodesic_distance(trained, monkeypatch):
    _validate_point(monkeypatch)
    truth = reference.read_canonical(trained / "referencias_canonicas.csv")["P01"]
    predictions = _rows(_vdir(trained) / "predictions.csv")
    valid = [p for p in predictions if p["valid"] == "True"]
    assert valid
    for row in predictions:
        assert (float(row["true_lat"]), float(row["true_lon"])) == (truth["lat"], truth["lon"])
        assert row["true_point"] == "P01" and row["model_load_s"] and row["prediction_s"]
    for row in valid:
        expected = haversine_m(truth["lat"], truth["lon"], float(row["estimated_lat"]), float(row["estimated_lon"]))
        assert float(row["error_m"]) == pytest.approx(expected) and float(row["error_m"]) > 0
        assert row["estimated_x_m"] and row["estimated_y_m"] and row["n_observed_keys"] != ""


@pytest.mark.parametrize("status,expected", [("complete", "valid"), ("partial", "valid")])
def test_validate_applies_complete_and_partial_snapshots_normally(trained, monkeypatch, status, expected):
    _validate_point(monkeypatch, snapshots=[_snapshot(status=status, started=f"t{i}") for i in range(3)])
    assert {p["status"] for p in _rows(_vdir(trained) / "predictions.csv")} == {expected}
    assert {a["snapshot_status"] for a in _rows(_vdir(trained) / "acquisitions.csv")} == {status}


def test_validate_invalid_snapshot_is_saved_and_does_not_abort_the_next_repetitions(trained, monkeypatch):
    invalid = _snapshot(status="invalid", cells=[], transport_error="MONI: OSError: caiu")
    _validate_point(monkeypatch, snapshots=[_snapshot(started="t0"), invalid, _snapshot(started="t2")])
    out = _vdir(trained)
    assert len(_rows(out / "acquisitions.csv")) == 3 and len(list((out / "fingerprints").glob("*.json"))) == 3
    second = [p for p in _rows(out / "predictions.csv") if p["repetition"] == "2"]
    assert {p["status"] for p in second} == {"unavailable"} and {p["valid"] for p in second} == {"False"}
    assert all(p["error_m"] == "" for p in second)
    failures = _rows(out / "failures.csv")
    assert [f["status"] for f in failures if f["stage"] == "snapshot"] == ["invalid"]
    assert sorted(f["stage"] for f in failures if f["repetition"] == "2" and f["stage"] != "snapshot") == [
        "complete", "spatial", "temporal"]
    stored = json.loads((out / "fingerprints" / "P01_T2.json").read_text(encoding="utf-8"))
    assert stored["status"] == "invalid" and stored["transport_error"] == "MONI: OSError: caiu"


@pytest.mark.parametrize("cells,status", [
    ([_cell(arfcn=9999, pci=99, cell_id="9", rsrp=-70.0)], "no_matching_radio_keys"),
    ([_cell(rat="NR", arfcn=629952, pci=1, cell_id="7")], "technology_mismatch"),
])
def test_validate_records_unmatched_and_wrong_technology_estimates(trained, monkeypatch, cells, status):
    _validate_point(monkeypatch, snapshots=[_snapshot(cells=cells, started=f"t{i}") for i in range(3)])
    assert {p["status"] for p in _rows(_vdir(trained) / "predictions.csv")} == {status}
    assert {f["status"] for f in _rows(_vdir(trained) / "failures.csv") if f["model"]} == {status}


def test_validate_connection_failure_skips_only_that_repetition(trained, monkeypatch):
    rig = _Rig(monkeypatch, [_snapshot(started="t0"), _snapshot(started="t2")], fail_connect={2})
    _run_point(_vargs())
    assert [a["repetition"] for a in _rows(_vdir(trained) / "acquisitions.csv")] == ["1", "3"]
    assert len(rig.taken) == 2 and rig.connects == 3
    assert [(f["repetition"], f["stage"]) for f in _rows(_vdir(trained) / "failures.csv")] == [("2", "connect")]


def _failing_predictions_write(monkeypatch, fail_on_call):
    real, state = validate.append_rows, {"calls": 0, "active": True}

    def append(path, fields, rows):
        if state["active"] and Path(path).name == "predictions.csv":
            state["calls"] += 1
            if state["calls"] == fail_on_call:
                raise OSError("disco cheio")
        return real(path, fields, rows)

    monkeypatch.setattr(validate, "append_rows", append)
    return state


def test_validate_write_failure_preserves_what_was_written_and_can_be_resumed_with_force(trained, monkeypatch):
    state = _failing_predictions_write(monkeypatch, 2)
    rig = _Rig(monkeypatch, [_snapshot(started=f"t{i}") for i in range(8)])
    with pytest.raises(validate.ValidationError, match="erro de escrita em T2"):
        _run_point(_vargs())
    out = _vdir(trained)
    assert [a["repetition"] for a in _rows(out / "acquisitions.csv")] == ["1", "2"]  # T1 completo e utilizavel
    assert len(_rows(out / "predictions.csv")) == 3 and (out / "fingerprints" / "P01_T1.json").is_file()
    assert [f["stage"] for f in _rows(out / "failures.csv")] == ["write"]
    assert not [t for t in threading.enumerate() if t.name == "process-sampler"]
    with pytest.raises(validate.ValidationError, match="ja ha artefatos.*--force"):
        _run_point(_vargs())
    state["active"] = False
    _Rig(monkeypatch, [_snapshot(started=f"u{i}") for i in range(3)])
    _run_point(_vargs(force=True))
    assert len(_rows(out / "acquisitions.csv")) == 3 and len(_rows(out / "predictions.csv")) == 9
    assert len(rig.taken) == 2


def test_validate_cancellation_creates_no_artifacts_and_touches_no_hardware(trained, monkeypatch):
    rig = _Rig(monkeypatch, [])
    with pytest.raises(validate.ValidationError, match="cancelada"):
        _run_point(_vargs(), answer="nao")
    assert not _vdir(trained).exists() and rig.events == []


def test_validate_rejects_a_point_outside_the_campaign_and_unknown_models(trained, monkeypatch, capsys):
    rig = _Rig(monkeypatch, [])
    with pytest.raises(validate.ValidationError, match="P02 fora da campanha"):
        _run_point(_vargs(point="P02"))
    with pytest.raises(ValueError, match="--models invalido"):
        _run_point(_vargs(models="spatial,deployed"))
    assert validate._main(["point", "--campaign", "coleta", "--point", "P02"]) == 1
    assert "fora da campanha" in capsys.readouterr().err and rig.events == [] and not _vdir(trained).exists()


def test_validate_missing_model_fails_before_the_modem_is_opened(trained, monkeypatch):
    (trained / "modelos" / "wknn_lte_temporal.json").unlink()
    rig = _Rig(monkeypatch, [])
    with pytest.raises(validate.ValidationError, match="modelo temporal nao treinado"):
        _run_point(_vargs())
    assert rig.events == [] and not _vdir(trained).exists()


def test_validate_protects_existing_points_and_allows_the_next_point(trained, monkeypatch):
    _validate_point(monkeypatch)
    out = _vdir(trained)
    before = _tree_bytes(out)
    _Rig(monkeypatch, [])
    with pytest.raises(validate.ValidationError, match="ja ha artefatos"):
        _run_point(_vargs())
    assert _tree_bytes(out) == before
    _validate_point(monkeypatch, point="P03")
    after = _tree_bytes(out)
    for path, data in before.items():
        if path.name in ("platform.json", "protocol.json") or "P01" in path.name:
            assert after[path] == data, path
    assert {r["point"] for r in _rows(out / "acquisitions.csv")} == {"P01", "P03"}


def test_validate_force_replaces_only_the_artifacts_of_that_point(trained, monkeypatch):
    _validate_point(monkeypatch)
    _validate_point(monkeypatch, point="P03")
    out = _vdir(trained)
    before = _tree_bytes(out)
    others = _without_validation(trained)
    p03_metrics = [r for r in _rows(out / "system_metrics.csv") if r["point"] == "P03"]
    _validate_point(monkeypatch, force=True, snapshots=[_snapshot(started=f"new{i}") for i in range(3)])
    after = _tree_bytes(out)
    assert _without_validation(trained) == others  # modelos, referencias, banco e logs intactos
    for path, data in before.items():
        if "P03" in path.name or path.name in ("platform.json", "protocol.json"):
            assert after[path] == data, path
    assert all("new" in r["started_at"] for r in _rows(out / "acquisitions.csv") if r["point"] == "P01")
    assert [r for r in _rows(out / "system_metrics.csv") if r["point"] == "P03"] == p03_metrics
    assert len([r for r in _rows(out / "acquisitions.csv") if r["point"] == "P01"]) == 3
    assert len([r for r in _rows(out / "predictions.csv") if r["true_point"] == "P03"]) == 9


def test_validate_records_protocol_with_rotating_prediction_order(trained, monkeypatch):
    _validate_point(monkeypatch)
    protocol = json.loads((_vdir(trained) / "protocol.json").read_text(encoding="utf-8"))
    assert protocol["prediction_order"] == {"T1": ["complete", "temporal", "spatial"],
                                            "T2": ["temporal", "spatial", "complete"],
                                            "T3": ["spatial", "complete", "temporal"]}
    assert (protocol["one_snapshot_per_repetition"], protocol["one_csurv_per_repetition"],
            protocol["snapshot_shared_between_models"], protocol["true_reference_source"]) == (
        True, True, True, "canonical_S1")
    assert protocol["models"] == ["spatial", "temporal", "complete"] and protocol["repetitions"] == 3
    assert protocol["points"] == list(FOUR_POINTS) and protocol["technology"] == "LTE"
    assert [p["model"] for p in _rows(_vdir(trained) / "predictions.csv")][:3] == ["complete", "temporal", "spatial"]
    with pytest.raises(validate.ValidationError, match="protocol.json incompativel.*repetitions"):
        _Rig(monkeypatch, [])
        _run_point(_vargs(point="P03", repetitions=2))
    assert not [r for r in _rows(_vdir(trained) / "acquisitions.csv") if r["point"] == "P03"]


@pytest.mark.parametrize("deploy,expected", [(None, None), ("complete", True), ("spatial", False)])
def test_validate_platform_json_records_bundles_and_deployment(trained, monkeypatch, capsys, deploy, expected):
    if deploy:
        assert _deploy(capsys, model=deploy)[0] == 0
    _validate_point(monkeypatch)
    platform_data = json.loads((_vdir(trained) / "platform.json").read_text(encoding="utf-8"))
    assert platform_data["platform_id"] == "raspberrypi_os" and platform_data["campaign"] == "coleta"
    assert platform_data["deployed_matches_complete"] is expected
    for name in ("spatial", "temporal", "complete"):
        path = trained / "modelos" / f"wknn_lte_{name}.json"
        assert platform_data["bundles"][name] == {
            "path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "dataset_hashes": platform_data["bundles"][name]["dataset_hashes"]}
    assert platform_data["tcc_location_conf"]["values"]["port"] == "/dev/ttyUSB2"
    assert platform_data["collection_conf"]["sha256"] and platform_data["python"] and platform_data["kernel"]
    assert not {"imei", "imsi", "iccid"} & set(json.dumps(platform_data).lower().replace('"', " ").split())


def test_validate_platform_json_for_buildroot_and_incompatible_platform_is_not_overwritten(trained, monkeypatch):
    location = config.load_config(config.SYSTEM_CONFIG)
    config.save_config(config.SYSTEM_CONFIG, {**location, "system": "buildroot"}, force=True)
    _validate_point(monkeypatch)
    out = _vdir(trained)
    assert json.loads((out / "platform.json").read_text(encoding="utf-8"))["platform_id"] == "buildroot"
    assert _rows(out / "acquisitions.csv")[0]["platform"] == "buildroot"
    config.save_config(config.SYSTEM_CONFIG, {**location, "system": "raspberrypi_os"}, force=True)
    original = (out / "platform.json").read_bytes()
    _Rig(monkeypatch, [])
    with pytest.raises(validate.ValidationError, match="platform.json incompativel.*platform_id"):
        _run_point(_vargs(point="P03"))
    assert (out / "platform.json").read_bytes() == original


@pytest.mark.parametrize("os_id,model", [("raspbian", "Raspberry Pi 3 Model B Rev 1.2"), ("buildroot", "BCM2835")])
def test_platform_info_reads_simulated_proc_and_os_release(tmp_path, os_id, model):
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "cpuinfo").write_text(f"processor\t: 0\nprocessor\t: 1\nModel\t\t: {model}\nHardware\t: BCM2835\n")
    (proc / "meminfo").write_text("MemTotal:        948304 kB\nMemAvailable:    500000 kB\n")
    release = tmp_path / "os-release"
    release.write_text(f'ID={os_id}\nNAME="{os_id.title()}"\nVERSION_ID=12\n')
    info = validate.platform_info(os_id, "coleta", {}, tmp_path / "c.conf", {"port": "/dev/ttyUSB2"},
                                  tmp_path / "t.conf", "LTE", {}, None, proc_root=proc, os_release=release) \
        if (tmp_path / "c.conf").write_text("a=1\n") and (tmp_path / "t.conf").write_text("b=2\n") else None
    assert info["os_release"]["ID"] == os_id and info["cpu"]["cores"] == 2 and info["cpu"]["Model"] == model
    assert info["memory_total_kb"] == 948304 and info["platform_id"] == os_id
    assert info["deployed_matches_complete"] is None and info["architecture"]


def test_platform_info_survives_missing_git_lscpu_vcgencmd_and_proc(tmp_path, monkeypatch):
    def no_tools(*args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(manifest.subprocess, "run", no_tools)
    (tmp_path / "c.conf").write_text("a=1\n")
    (tmp_path / "t.conf").write_text("b=2\n")
    info = validate.platform_info("buildroot", "coleta", {}, tmp_path / "c.conf", {}, tmp_path / "t.conf", "LTE", {},
                                  None, proc_root=tmp_path / "sem-proc", os_release=tmp_path / "sem-os-release")
    assert (info["git_commit"], info["cpu"], info["memory_total_kb"], info["os_release"]) == (None, None, None, None)


def _simulated_system(tmp_path):
    proc, sys_root = tmp_path / "proc", tmp_path / "sys"
    (proc / "123").mkdir(parents=True)
    (proc / "stat").write_text("cpu 100 0 20 80 0 0 0 0 0 0\n", encoding="ascii")
    (proc / "loadavg").write_text("0.10 0.20 0.30 1/2 123\n", encoding="ascii")
    (proc / "meminfo").write_text("MemAvailable: 4096 kB\n", encoding="ascii")
    (proc / "123" / "stat").write_text("123 (python) R 0 0 0 0 0 0 0 0 0 0 50 10\n", encoding="ascii")
    (proc / "123" / "status").write_text("VmRSS:\t100 kB\nVmHWM:\t200 kB\n", encoding="ascii")
    thermal = sys_root / "class" / "thermal" / "thermal_zone0"
    thermal.mkdir(parents=True)
    (thermal / "temp").write_text("47000\n", encoding="ascii")
    return proc, sys_root


def test_process_sampler_uses_simulated_proc_and_sys_and_always_stops(tmp_path):
    proc, sys_root = _simulated_system(tmp_path)
    database = tmp_path / "campaign.db"
    database.write_bytes(b"db")
    out = tmp_path / "m" / "system_metrics.csv"
    sampler = monitor.ProcessSampler(database, tmp_path, out, "validation", "P01", interval_s=0.01, pid=123,
                                     proc_root=proc, sys_root=sys_root)
    sampler.set(state="acquiring", session="T2")
    with pytest.raises(RuntimeError, match="boom"):
        with sampler:
            raise RuntimeError("boom")
    rows = _rows(out)
    assert list(rows[0]) == list(monitor.FIELDS) and rows[0]["state"] == "acquiring" and rows[-1]["state"] == "failed"
    first = rows[0]
    assert (first["mode"], first["point"], first["session"]) == ("validation", "P01", "T2")
    assert (first["rss_kb"], first["hwm_kb"], first["memory_available_kb"]) == ("100", "200", "4096")
    assert (first["load_1m"], first["temperature_c"], first["database_bytes"]) == ("0.10", "47.0", "2")
    assert first["disk_free_bytes"]
    assert not sampler._thread.is_alive() and not [t for t in threading.enumerate() if t.name == "process-sampler"]
    sampler.stop()  # idempotent
    assert len(_rows(out)) == len(rows)


def test_monitor_system_helpers_read_simulated_files_and_tolerate_missing_ones(tmp_path):
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "cpuinfo").write_text("processor\t: 0\nprocessor\t: 1\nModel\t\t: Raspberry Pi 3\nHardware\t: BCM2835\n")
    (proc / "meminfo").write_text("MemTotal:        948304 kB\nMemAvailable:    500000 kB\n")
    release = tmp_path / "os-release"
    release.write_text('ID=buildroot\nNAME="Buildroot"\nVERSION_ID=2025.02\nHOME_URL=x\n')
    assert monitor.cpu_info(proc) == {"cores": 2, "Model": "Raspberry Pi 3", "Hardware": "BCM2835"}
    assert monitor.memory_total_kb(proc) == 948304
    assert monitor.os_release(release) == {"ID": "buildroot", "NAME": "Buildroot", "VERSION_ID": "2025.02"}
    missing = tmp_path / "ausente"
    assert (monitor.cpu_info(missing), monitor.memory_total_kb(missing), monitor.os_release(missing)) == (
        None, None, None)


def test_monitor_git_commit_reads_git_files_without_running_git(tmp_path):
    assert monitor.git_commit(tmp_path) is None  # Buildroot: /opt/tcc sem .git
    git = tmp_path / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n")
    assert monitor.git_commit(tmp_path) is None  # ref sem arquivo nem packed-refs
    (git / "packed-refs").write_text("# pack-refs\n" + "a" * 40 + " refs/heads/main\n")
    assert monitor.git_commit(tmp_path) == "a" * 40
    (git / "refs" / "heads" / "main").write_text("b" * 40 + "\n")
    assert monitor.git_commit(tmp_path) == "b" * 40
    (git / "HEAD").write_text("c" * 40 + "\n")  # HEAD solto
    assert monitor.git_commit(tmp_path) == "c" * 40


def test_monitor_empty_db_path_leaves_database_bytes_empty_and_missing_sensors_do_not_fail(tmp_path):
    sample = monitor.sample_resources("", tmp_path, proc_root=tmp_path / "sem-proc", sys_root=tmp_path / "sem-sys")
    assert sample["database_bytes"] == "" and sample["temperature_c"] == "" and sample["cpu"] is None
    out = tmp_path / "m.csv"
    sampler = monitor.ProcessSampler("", tmp_path, out, "runtime", "", interval_s=0.01, proc_root=tmp_path / "sem-proc",
                                     sys_root=tmp_path / "sem-sys")
    sampler.set(state="waiting", session="run-C000001")
    with sampler:
        pass
    rows = _rows(out)
    assert rows[0]["state"] == "waiting" and rows[-1]["state"] == "completed"
    assert {r["database_bytes"] for r in rows} == {""} and {r["temperature_c"] for r in rows} == {""}
    assert {r["mode"] for r in rows} == {"runtime"} and rows[0]["session"] == "run-C000001"

def test_validate_metrics_follow_stabilizing_acquiring_predicting_completed(trained, monkeypatch):
    _validate_point(monkeypatch, repetitions=1)
    rows = _rows(_vdir(trained) / "system_metrics.csv")
    assert {r["mode"] for r in rows} == {"validation"} and {r["point"] for r in rows} == {"P01"}
    states = [r["state"] for r in rows]
    assert states[0] in ("stabilizing", "acquiring", "predicting") and states[-1] == "completed"
    assert {"completed"} <= set(states) <= {"stabilizing", "acquiring", "predicting", "completed"}
    assert {r["session"] for r in rows} == {"T1"}


def test_validate_sampler_stops_when_an_unexpected_error_aborts_the_point(trained, monkeypatch):
    _Rig(monkeypatch, [])
    monkeypatch.setattr(validate, "take_snapshot", lambda driver: (_ for _ in ()).throw(ValueError("inesperado")))
    with pytest.raises(ValueError, match="inesperado"):
        _run_point(_vargs())
    assert not [t for t in threading.enumerate() if t.name == "process-sampler"]
    assert _rows(_vdir(trained) / "system_metrics.csv")[-1]["state"] == "failed"


def test_validate_fingerprint_allows_rebuilding_the_cells_without_the_modem(trained, monkeypatch):
    from estimador.features import cells_from_observations
    from estimador.methods.fingerprint import load_bundle

    rig = _validate_point(monkeypatch, snapshots=[_snapshot(started=f"t{i}") for i in range(3)], repetitions=1)
    stored = json.loads((_vdir(trained) / "fingerprints" / "P01_T1.json").read_text(encoding="utf-8"))
    assert {"snapshot_id", "point", "repetition", "started_at", "status", "reasons", "access_mode", "serving",
            "serving_rsrp_dbm", "serving_rsrq_db", "serving_sinr_db", "observations", "moni", "servinfo", "csurv",
            "gnss", "timings_s", "anomalies", "transport_error"} <= set(stored)
    cells = cells_from_observations([SimpleNamespace(**o) for o in stored["observations"]])
    model = load_bundle(str(trained / "modelos" / "wknn_lte_complete.json"))
    original = validate.estimate_from_snapshot(rig.taken[0], model=model)
    assert model.predict(cells).lat == pytest.approx(original.lat)


@pytest.fixture
def validated(trained, monkeypatch):
    for point in FOUR_POINTS:
        _validate_point(monkeypatch, point=point)
    return _vdir(trained)


def _summarize(capsys, *extra):
    code = validate._main(["summarize", "--campaign", "coleta", *extra])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_validate_summarize_consolidates_without_touching_hardware(validated, trained, monkeypatch, capsys):
    monkeypatch.setattr(validate, "FN990Driver", lambda: pytest.fail("summarize nao acessa hardware"))
    others = _without_validation(trained)
    code, out, err = _summarize(capsys)
    assert code == 0, err
    assert "Validacao: 4 pontos, 12 snapshots, 36 predicoes" in out and "Pronto para comparacao com Buildroot: sim" in out
    summary = json.loads((validated / "validation_summary.json").read_text(encoding="utf-8"))
    assert (summary["snapshots"], summary["predictions"], summary["problems"], summary["ready_for_comparison"]) == (
        12, 36, [], True)
    assert set(summary["by_model"]) == {"spatial", "temporal", "complete"}
    assert all(item["predictions"] == 12 and item["coverage"] == 1.0 for item in summary["by_model"].values())
    assert summary["acquisition"]["status_counts"] == {"complete": 12} and summary["acquisition"]["total_s"]["max"] == 2.6
    assert summary["acquisition"]["n_lte_observations"]["total"] == 36
    assert summary["system"]["samples"] > 0 and "cpu_total" in summary["system"]
    by_model = {r["model"]: r for r in _rows(validated / "summary_by_model.csv")}
    assert set(by_model) == {"spatial", "temporal", "complete"} and by_model["complete"]["predictions"] == "12"
    by_point = _rows(validated / "summary_by_point.csv")
    assert len(by_point) == 12 and {r["repetitions"] for r in by_point} == {"3"}
    assert _without_validation(trained) == others


def test_validate_summarize_detects_a_missing_point(trained, monkeypatch, capsys):
    for point in ("P01", "P03", "P05"):
        _validate_point(monkeypatch, point=point)
    code, out, err = _summarize(capsys)
    assert code == 1 and "P08/T1: aquisicao ausente" in err and "Pronto para comparacao com Buildroot: nao" in out


def test_validate_summarize_detects_a_missing_repetition(trained, monkeypatch, capsys):
    _Rig(monkeypatch, [_snapshot(started=f"t{i}") for i in range(2)], fail_connect={2})
    _run_point(_vargs())
    for point in ("P03", "P05", "P08"):
        _validate_point(monkeypatch, point=point)
    code, out, err = _summarize(capsys)
    assert code == 1 and "P01/T2: aquisicao ausente" in err and "P03/T" not in err


def test_validate_summarize_detects_duplicates_and_broken_files(validated, capsys):
    rows = _rows(validated / "acquisitions.csv")
    validate.append_rows(validated / "acquisitions.csv", validate.ACQUISITION_FIELDS, [rows[0]])
    predictions = _rows(validated / "predictions.csv")
    validate.append_rows(validated / "predictions.csv", validate.PREDICTION_FIELDS, [predictions[0]])
    (validated / "fingerprints" / "P03_T2.json").write_text("{nao e json", encoding="utf-8")
    code, out, err = _summarize(capsys)
    assert code == 1
    assert "aquisicao duplicada" in err and "snapshot_id duplicado" in err and "predicao duplicada do modelo" in err
    assert "P03_T2.json: ausente ou invalido" in err


def test_validate_summarize_detects_a_snapshot_not_shared_by_all_models(validated, capsys):
    rows = [r for r in _rows(validated / "predictions.csv") if not (r["true_point"] == "P01" and r["repetition"] == "1"
                                                                     and r["model"] == "spatial")]
    validate.rewrite_rows(validated / "predictions.csv", validate.PREDICTION_FIELDS, rows)
    code, out, err = _summarize(capsys)
    assert code == 1 and "modelos ['complete', 'temporal'] diferem" in err


def test_validate_summarize_handles_eight_points_and_three_repetitions(tmp_path, monkeypatch, capsys):
    results, _ = _trainable_collection(tmp_path, monkeypatch, prepare.VALID_POINTS)
    assert _wknn(capsys, "train", "--campaign", "coleta")[0] == 0
    for point in prepare.VALID_POINTS:
        _validate_point(monkeypatch, point=point)
    code, out, err = _summarize(capsys)
    assert code == 0, err
    assert "Validacao: 8 pontos, 24 snapshots, 72 predicoes" in out
    summary = json.loads((_vdir(results) / "validation_summary.json").read_text(encoding="utf-8"))
    assert (summary["expected_snapshots"], summary["expected_predictions"]) == (24, 72)


def _model_rows(errors, invalid=1):
    rows = [{"valid": "True", "error_m": str(e), "status": "valid", "prediction_s": "0.002",
             "n_observed_keys": "3", "n_ignored_keys": "1"} for e in errors]
    rows += [{"valid": "False", "error_m": "", "status": "unavailable", "prediction_s": "0.004",
              "n_observed_keys": "", "n_ignored_keys": ""} for _ in range(invalid)]
    return rows


def test_validate_summary_statistics():
    summary = validate.model_summary(_model_rows([10.0, 20.0, 30.0, 40.0]))
    assert (summary["predictions"], summary["valid"], summary["failures"], summary["coverage"]) == (5, 4, 1, 0.8)
    assert summary["status_counts"] == {"unavailable": 1, "valid": 4}
    assert (summary["mean_m"], summary["median_m"], summary["max_m"]) == (25.0, 25.0, 40.0)
    assert summary["rmse_m"] == pytest.approx(750 ** 0.5)
    assert (summary["p90_m"], summary["p95_m"]) == (pytest.approx(37.0), pytest.approx(38.5))
    assert summary["mean_prediction_ms"] == pytest.approx((4 * 2 + 4) / 5)
    assert summary["p95_prediction_ms"] == pytest.approx(3.6)
    assert (summary["mean_observed_keys"], summary["mean_ignored_keys"]) == (3.0, 1.0)
    empty = validate.model_summary(_model_rows([], invalid=2))
    assert (empty["coverage"], empty["mean_m"], empty["rmse_m"], empty["p95_m"]) == (0.0, None, None, None)
    assert validate.point_summary([])["coverage"] is None


def test_validate_does_not_change_position_training_or_bundles(trained, monkeypatch, capsys):
    bundles = {path: data for path, data in _tree_bytes(trained / "modelos").items()}
    others = _without_validation(trained)
    _validate_point(monkeypatch)
    assert _without_validation(trained) == others
    assert {path: data for path, data in _tree_bytes(trained / "modelos").items()} == bundles
    _fake_live(monkeypatch)
    code, out, err = _wknn_position(capsys, "--campaign", "coleta", "--model", "complete", "--json")
    assert code == 0, err and json.loads(out)["method"] == "wknn"
    assert _without_validation(trained) == others
