import csv
import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from campaign_tools import check, config, configure, export, manifest, monitor, prepare, reference, visit
from coletor import db
from hal.models import CellObservation, GnssFix, GnssReading, PrepareResult, ServInfoReading


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
