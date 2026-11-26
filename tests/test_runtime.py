"""Testes de configuração e persistência do daemon runtime sem hardware."""

import hashlib
import json
import logging
import os
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from radiomapa_sintetico import criar_radiomapa
from support import driver_com

import estimador.train as tr
import runtime.daemon as daemon
from campaign_tools import configure, monitor
from estimador.geometry import haversine_m
from hal.models import GnssFix, GnssReading, to_jsonable


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


# ------------------------------------------------------------------------------ TCC_DEBUG
CELULAS_LIDAS = [(3375, 254, 257298023, -95), (3375, 31, 257298024, -101), (1350, 64, 257298030, -104),
                 (2850, 64, 257298031, -109)]
CICLO_AT = ["AT#MONI", "AT#SERVINFO", "AT#CSURV", "AT$GPSACP"]


def csurv(*registros):
    linhas = ["AT#CSURV", "Network survey started ..."]
    for earfcn, pci, cell_id, rsrp in registros:
        linhas += [f"earfcn: {earfcn} rxLev: -70 mcc: 724 mnc: 06 cellId: {cell_id} tac: 34747 phyCellId: {pci}",
                   f"cellStatus: CELL_SUITABLE rsrp: {rsrp} rsrq: -12 bw: 15"]
    return linhas + ["OK"]


def leitura(**trocas):
    roteiro = {"AT#MONI": "moni_lte.txt", "AT#SERVINFO": "servinfo_lte.txt", "AT#CSURV": csurv(*CELULAS_LIDAS),
               "AT$GPSACP": "gpsacp_fix.txt"}
    roteiro.update(trocas)
    return roteiro


@pytest.fixture(autouse=True)
def _fecha_logging():
    yield
    raiz = logging.getLogger()
    for handler in list(raiz.handlers):
        raiz.removeHandler(handler)
        handler.close()


@pytest.fixture(scope="module")
def modelo(tmp_path_factory):
    pasta = tmp_path_factory.mktemp("runtime_modelo")
    banco = str(pasta / "radiomapa_v2.db")
    criar_radiomapa(banco)
    caminho = str(pasta / "wknn_lte_complete.json")
    tr.train(banco, "LTE", "spatial", caminho, grid=tr.Grid(k=(1, 3)))
    return caminho


class Modem:
    """FN990 roteirizado: cada FN990Driver() do daemon recebe um driver novo, todos ficam registrados."""

    def __init__(self, monkeypatch, relogio, roteiro, falha_conexao=False):
        self.drivers = []

        def fabrica():
            driver = driver_com(roteiro, relogio)

            def conectar(*args, **kwargs):
                if falha_conexao:
                    raise ConnectionError("porta indisponível")

            driver.connect = conectar
            self.drivers.append(driver)
            return driver

        monkeypatch.setattr(daemon, "FN990Driver", fabrica)

    @property
    def ciclos(self):
        return [d._ser.written for d in self.drivers if d._ser.written]


class FalsoSampler:
    instancias = []

    def __init__(self, *args, **kwargs):
        self.args, self.kwargs, self.estados, self.final, self.error = args, kwargs, [], None, None
        self.sessoes = []
        FalsoSampler.instancias.append(self)

    def set(self, state=None, session=None):
        if state is not None:
            self.estados.append(state)
        if session is not None:
            self.sessoes.append(session)

    def start(self):
        self.estados.append("start")
        return self

    def stop(self, final_state="completed"):
        self.final = final_state
        self.estados.append(final_state)


@pytest.fixture
def falso_sampler(monkeypatch):
    FalsoSampler.instancias = []
    monkeypatch.setattr(monitor, "ProcessSampler", FalsoSampler)
    return FalsoSampler


def colapsa(estados):
    saida = []
    for estado in estados:
        if estado != "start" and (not saida or saida[-1] != estado):
            saida.append(estado)
    return saida


def executa(tmp_path, monkeypatch, relogio, modelo, debug, roteiro=None, nome="exec", **extra):
    base = tmp_path / nome
    valores = {"TCC_METHOD": "wknn", "TCC_MODEL": modelo, "TCC_OUTPUT": str(base / "latest_position.json"),
               "TCC_HISTORY": str(base / "history.jsonl"), "TCC_LOG": str(base / "runtime.log"),
               "TCC_DEBUG": "1" if debug else "0"}
    valores.update(extra)
    modem = Modem(monkeypatch, relogio, roteiro or leitura())
    codigo = daemon.main(["--config", write_config(tmp_path, **valores), "--once"])
    return codigo, modem, base


def historico(base):
    return [json.loads(linha) for linha in (base / "history.jsonl").read_text(encoding="utf-8").splitlines()]


def ultimo_ciclo(base):
    return [entrada for entrada in historico(base) if "event" not in entrada][-1]


def test_tcc_debug_ausente_e_zero_e_aceita_somente_0_ou_1(tmp_path):
    assert daemon.load_config(write_config(tmp_path)).debug is False
    assert daemon.load_config(write_config(tmp_path, TCC_DEBUG="0")).debug is False
    assert daemon.load_config(write_config(tmp_path, TCC_DEBUG="1")).debug is True
    for invalido in ("2", "true", "", "yes", "01"):
        with pytest.raises(daemon.RuntimeConfigError, match="TCC_DEBUG deve ser 0 ou 1"):
            daemon.load_config(write_config(tmp_path, TCC_DEBUG=invalido))


def test_debug_deriva_caminhos_de_tcc_output_e_respeita_os_informados(tmp_path):
    padrao = daemon.load_config(write_config(tmp_path, TCC_DEBUG="1"))
    pasta = tmp_path / "runtime"
    assert (padrao.metrics, padrao.snapshots_dir, padrao.run_manifest) == (
        str(pasta / "runtime_metrics.csv"), str(pasta / "snapshots"), str(pasta / "runtime_manifest.json"))
    informado = daemon.load_config(write_config(tmp_path, TCC_DEBUG="1", TCC_METRICS="/x/m.csv",
                                                TCC_SNAPSHOTS_DIR="/x/s", TCC_RUN_MANIFEST="/x/r.json"))
    assert (informado.metrics, informado.snapshots_dir, informado.run_manifest) == ("/x/m.csv", "/x/s", "/x/r.json")
    normal = daemon.load_config(write_config(tmp_path, TCC_METRICS="", TCC_SNAPSHOTS_DIR="", TCC_RUN_MANIFEST=""))
    assert (normal.debug, normal.metrics, normal.snapshots_dir, normal.run_manifest) == (False, "", "", "")


def test_prepare_output_paths_cria_os_destinos_debug_so_quando_ativo(tmp_path):
    normal = daemon.load_config(write_config(tmp_path))
    daemon.prepare_output_paths(normal)
    assert not (tmp_path / "runtime" / "snapshots").exists()
    debug = daemon.load_config(write_config(tmp_path, TCC_DEBUG="1", TCC_METRICS=str(tmp_path / "eng" / "m.csv"),
                                            TCC_RUN_MANIFEST=str(tmp_path / "eng2" / "r.json")))
    daemon.prepare_output_paths(debug)
    assert (tmp_path / "runtime" / "snapshots").is_dir() and (tmp_path / "eng").is_dir()
    assert (tmp_path / "eng2").is_dir()
    repetido = daemon.load_config(write_config(tmp_path, TCC_DEBUG="1", TCC_METRICS=str(tmp_path / "runtime" /
                                                                                       "history.jsonl")))
    with pytest.raises(daemon.RuntimeConfigError, match="caminhos diferentes"):
        daemon.prepare_output_paths(repetido)


def test_modo_normal_so_cria_latest_history_e_log_e_nao_inicia_sampler(tmp_path, monkeypatch, sem_espera, modelo):
    monkeypatch.setattr(monitor, "ProcessSampler", lambda *a, **k: pytest.fail("normal não inicia sampler"))
    monkeypatch.setattr(daemon, "build_run_manifest", lambda *a, **k: pytest.fail("normal não cria manifesto"))
    codigo, modem, base = executa(tmp_path, monkeypatch, sem_espera, modelo, debug=False)
    assert codigo == 0
    assert sorted(p.name for p in base.iterdir()) == ["history.jsonl", "latest_position.json", "runtime.log"]
    entradas = historico(base)
    assert [e.get("event", "inference") for e in entradas] == ["runtime_started", "modem_ready", "inference"]
    assert set(entradas[-1]) == {"timestamp_utc", "method", "status", "latitude", "longitude", "technology",
                                 "reason", "software_version", "timings_s"}
    latest = json.loads((base / "latest_position.json").read_text(encoding="utf-8"))
    assert latest["status"] == "valid" and set(latest) == set(entradas[-1])
    log = (base / "runtime.log").read_text(encoding="utf-8")
    for evento in ("runtime_started", "cycle_started", "cycle_completed", "runtime_stopped"):
        assert evento in log
    assert "cycle_id" not in log and "snapshots" not in log


def test_debug_cria_metricas_manifesto_e_um_snapshot_por_ciclo(tmp_path, monkeypatch, sem_espera, modelo):
    codigo, modem, base = executa(tmp_path, monkeypatch, sem_espera, modelo, debug=True)
    assert codigo == 0
    assert sorted(p.name for p in base.iterdir()) == [
        "history.jsonl", "latest_position.json", "runtime.log", "runtime_manifest.json", "runtime_metrics.csv",
        "snapshots"]
    manifesto = json.loads((base / "runtime_manifest.json").read_text(encoding="utf-8"))
    registro = ultimo_ciclo(base)
    assert registro["cycle_id"].startswith(manifesto["run_id"] + "-C")
    arquivos = list((base / "snapshots").iterdir())
    assert [a.name for a in arquivos] == [registro["cycle_id"] + ".json"]
    assert not [t for t in threading.enumerate() if t.name == "process-sampler"]
    linhas = (base / "runtime_metrics.csv").read_text(encoding="utf-8").splitlines()
    assert linhas[0].split(",") == list(monitor.FIELDS) and len(linhas) >= 2
    colunas = dict(zip(monitor.FIELDS, linhas[-1].split(",")))
    assert (colunas["mode"], colunas["point"], colunas["state"], colunas["session"]) == (
        "runtime", "", "stopped", registro["cycle_id"])
    assert colunas["database_bytes"] == "" and "runtime_stopped" in (base / "runtime.log").read_text(encoding="utf-8")


def test_debug_nao_altera_aquisicao_nem_resultado(tmp_path, monkeypatch, sem_espera, modelo):
    _, normal, base_normal = executa(tmp_path, monkeypatch, sem_espera, modelo, debug=False, nome="normal")
    _, debug, base_debug = executa(tmp_path, monkeypatch, sem_espera, modelo, debug=True, nome="debug")
    assert normal.ciclos == debug.ciclos == [CICLO_AT]  # um ciclo, só leituras, mesma ordem, sem AT extra
    leituras = {nome: json.loads((base / "latest_position.json").read_text(encoding="utf-8"))
                for nome, base in (("normal", base_normal), ("debug", base_debug))}
    for dado in leituras.values():
        dado.pop("timestamp_utc"), dado.pop("timings_s")
    assert leituras["normal"] == leituras["debug"] and leituras["normal"]["status"] == "valid"
    sem_extras = {k: v for k, v in ultimo_ciclo(base_debug).items() if k in ultimo_ciclo(base_normal)}
    assert set(sem_extras) == set(ultimo_ciclo(base_normal))


def test_infer_once_com_probe_so_observa(tmp_path, monkeypatch, sem_espera, modelo):
    config = daemon.load_config(write_config(tmp_path, TCC_METHOD="wknn", TCC_MODEL=modelo))
    model, _ = daemon.validate_runtime_config(config)
    resultados, comandos = [], []
    for probe in (None, daemon.CycleProbe()):
        modem = Modem(monkeypatch, sem_espera, leitura())
        resultado = daemon.infer_once(config, model, None) if probe is None else daemon.infer_once(
            config, model, None, probe)
        resultado.pop("timestamp_utc"), resultado.pop("timings_s")
        resultados.append(resultado)
        comandos.append(modem.ciclos)
    assert resultados[0] == resultados[1] and comandos[0] == comandos[1] == [CICLO_AT]
    assert probe.snapshot is not None and probe.estimate.valid and probe.gnss is probe.snapshot.gnss


def test_um_take_snapshot_por_ciclo_e_o_mesmo_snapshot_na_posicao_e_no_arquivo_bruto(
        tmp_path, monkeypatch, sem_espera, modelo, falso_sampler):
    snapshots, estimativas = [], []
    real_snapshot, real_estimate = daemon.take_snapshot, daemon.estimate_from_snapshot
    monkeypatch.setattr(daemon, "take_snapshot", lambda driver: snapshots.append(real_snapshot(driver)) or snapshots[-1])
    monkeypatch.setattr(daemon, "estimate_from_snapshot",
                        lambda snapshot, *a, **k: estimativas.append(snapshot) or real_estimate(snapshot, *a, **k))
    codigo, modem, base = executa(tmp_path, monkeypatch, sem_espera, modelo, debug=True)
    assert codigo == 0 and len(snapshots) == 1 and estimativas == snapshots and modem.ciclos == [CICLO_AT]
    registro = ultimo_ciclo(base)
    envelope = json.loads((base / "snapshots" / f"{registro['cycle_id']}.json").read_text(encoding="utf-8"))
    assert envelope["snapshot"] == json.loads(json.dumps(to_jsonable(snapshots[0]), default=str))
    latest = json.loads((base / "latest_position.json").read_text(encoding="utf-8"))
    assert envelope["result"] == latest and "snapshot" not in latest
    assert (latest["latitude"], latest["longitude"]) == (registro["lat"], registro["lon"])
    assert set(envelope) == {"cycle_id", "scheduled_utc", "started_utc", "finished_utc", "model_sha256", "result",
                             "snapshot"}
    tempos = envelope["snapshot"]["timings_s"]
    assert (registro["moni_s"], registro["servinfo_s"], registro["csurv_s"], registro["gnss_s"],
            registro["snapshot_total_s"]) == (tempos["MONI"], tempos["SERVINFO"], tempos["CSURV"], tempos["GNSS"],
                                              tempos["total"])


def test_snapshot_bruto_e_gravado_atomicamente_e_nunca_sobrescrito(tmp_path, falso_sampler):
    config = daemon.load_config(write_config(tmp_path, TCC_DEBUG="1"))
    daemon.prepare_output_paths(config)
    sessao = daemon.DebugSession(config, FalsoSampler(), "run", "hash")
    probe = daemon.CycleProbe()
    probe.snapshot = SimpleNamespace(timings_s={}, observations=[], reasons=[], anomalies=[])
    ciclo = {"cycle_id": "run-C000001", "scheduled_utc": "a", "started_utc": "b", "finished_utc": "c"}
    with patch.object(daemon.os, "replace", wraps=os.replace) as replace:
        alvo = sessao.write_snapshot(ciclo, probe, result("valid", 1.0, 2.0))
    assert replace.call_count == 1 and Path(replace.call_args.args[0]).parent == alvo.parent
    assert Path(replace.call_args.args[1]) == alvo and list(alvo.parent.glob("*.tmp")) == []
    original = alvo.read_bytes()
    with pytest.raises(FileExistsError, match="nunca é sobrescrito"):
        sessao.write_snapshot(ciclo, probe, result("valid", 9.0, 9.0))
    assert alvo.read_bytes() == original
    assert sessao.write_snapshot(ciclo, daemon.CycleProbe(), result("valid", 1.0, 2.0)) is None


def test_sha256_do_modelo_no_historico_no_envelope_e_no_manifesto(tmp_path, monkeypatch, sem_espera, modelo):
    esperado = hashlib.sha256(Path(modelo).read_bytes()).hexdigest()
    codigo, _, base = executa(tmp_path, monkeypatch, sem_espera, modelo, debug=True)
    registro = ultimo_ciclo(base)
    manifesto = json.loads((base / "runtime_manifest.json").read_text(encoding="utf-8"))
    envelope = json.loads((base / "snapshots" / f"{registro['cycle_id']}.json").read_text(encoding="utf-8"))
    assert registro["model_sha256"] == manifesto["model_sha256"] == envelope["model_sha256"] == esperado


CAMPOS_DEBUG = (
    "cycle_id scheduled_utc started_utc finished_utc period_s schedule_delay_s cycle_total_s snapshot_started_at "
    "snapshot_status snapshot_reasons access_mode moni_s servinfo_s csurv_s gnss_s snapshot_total_s n_observations "
    "n_lte_observations n_nr_observations n_lte_radio_keys n_nr_radio_keys serving_radio_key serving_global_cell_key "
    "transport_error anomaly_count method model_sha256 prediction_s valid status reason technology lat lon x_m y_m "
    "n_observed_keys n_ignored_keys n_neighbors zero_distance gnss_status gnss_lat gnss_lon gnss_alt_m gnss_fix_type "
    "gnss_hdop gnss_nsat gnss_utc gnss_elapsed_s wknn_gnss_distance_m").split()


def test_historico_debug_tem_os_campos_minimos_e_nao_despeja_dados_brutos(tmp_path, monkeypatch, sem_espera, modelo):
    _, _, base = executa(tmp_path, monkeypatch, sem_espera, modelo, debug=True)
    registro = ultimo_ciclo(base)
    assert set(CAMPOS_DEBUG) <= set(registro)
    assert registro["period_s"] is None and registro["schedule_delay_s"] == pytest.approx(0, abs=1)
    assert (registro["valid"], registro["status"], registro["technology"]) == (True, "valid", "LTE")
    assert registro["snapshot_status"] == "complete" and registro["access_mode"] == "LTE"
    assert registro["n_lte_observations"] >= 4 and registro["n_nr_observations"] == 0
    assert registro["n_lte_radio_keys"] >= 1 and registro["serving_radio_key"] and registro["anomaly_count"] == 0
    assert registro["n_observed_keys"] >= 1 and registro["prediction_s"] == registro["timings_s"]["estimate_s"]
    assert registro["x_m"] is not None and registro["n_neighbors"] in (1, 3)
    assert not {"observations", "moni", "servinfo", "csurv", "gnss", "neighbor_measurement_ids",
                "fingerprints", "weights"} & set(registro)
    assert not {"imei", "imsi", "iccid"} & set(json.dumps(registro).lower().replace('"', " ").split())


def test_gnss_com_fix_valido_gera_distancia_auxiliar_sem_substituir_o_wknn(tmp_path, monkeypatch, sem_espera, modelo):
    chamadas = []
    real = daemon.haversine_m
    monkeypatch.setattr(daemon, "haversine_m", lambda *a: chamadas.append(a) or real(*a))
    _, _, base = executa(tmp_path, monkeypatch, sem_espera, modelo, debug=True)
    registro = ultimo_ciclo(base)
    assert (registro["gnss_status"], registro["gnss_fix_type"], registro["gnss_hdop"], registro["gnss_nsat"]) == (
        "ok", 3, 0.8, 18)
    assert registro["gnss_lat"] == pytest.approx(37.5217, abs=1e-3) and registro["gnss_alt_m"] == 73.1
    assert isinstance(registro["gnss_utc"], str) and registro["gnss_elapsed_s"] >= 0
    assert chamadas == [(registro["latitude"], registro["longitude"], registro["gnss_lat"], registro["gnss_lon"])]
    assert registro["wknn_gnss_distance_m"] == pytest.approx(
        haversine_m(registro["latitude"], registro["longitude"], registro["gnss_lat"], registro["gnss_lon"]))
    assert "error_m" not in registro
    latest = json.loads((base / "latest_position.json").read_text(encoding="utf-8"))
    assert (latest["latitude"], latest["longitude"]) != (registro["gnss_lat"], registro["gnss_lon"])
    assert registro["lat"] == latest["latitude"] and registro["method"] == "wknn"


@pytest.mark.parametrize("fixture,status", [("gpsacp_sem_fix.txt", "no_fix"), ("gpsacp_motor_desligado.txt", "off")])
def test_gnss_sem_fix_ou_desligado_nao_invalida_o_wknn_nem_gera_distancia(
        tmp_path, monkeypatch, sem_espera, modelo, fixture, status):
    monkeypatch.setattr(daemon, "haversine_m", lambda *a: pytest.fail("sem fix não calcula distância"))
    _, _, base = executa(tmp_path, monkeypatch, sem_espera, modelo, debug=True,
                         roteiro=leitura(**{"AT$GPSACP": fixture}))
    registro = ultimo_ciclo(base)
    assert registro["gnss_status"] == status and registro["snapshot_status"] == "partial"
    assert all(registro[campo] is None for campo in ("gnss_lat", "gnss_lon", "gnss_alt_m", "gnss_fix_type",
                                                     "gnss_hdop", "gnss_nsat", "gnss_utc", "wknn_gnss_distance_m"))
    assert registro["status"] == "valid" and registro["lat"] is not None  # o GNSS não invalida o WKNN


def test_wknn_invalido_nao_gera_distancia_mesmo_com_gnss_valido(tmp_path, monkeypatch, sem_espera, modelo):
    monkeypatch.setattr(daemon, "haversine_m", lambda *a: pytest.fail("WKNN inválido não calcula distância"))
    _, _, base = executa(tmp_path, monkeypatch, sem_espera, modelo, debug=True,
                         roteiro=leitura(**{"AT#CSURV": "csurv_vazio.txt"}))
    registro = ultimo_ciclo(base)
    assert registro["valid"] is False and registro["status"] == "unavailable" and registro["lat"] is None
    assert registro["gnss_status"] == "ok" and registro["gnss_lat"] is not None
    assert registro["wknn_gnss_distance_m"] is None and registro["x_m"] is None


def test_estados_do_sampler_acompanham_o_ciclo_e_terminam_em_stopped(
        tmp_path, monkeypatch, sem_espera, modelo, falso_sampler):
    codigo, _, base = executa(tmp_path, monkeypatch, sem_espera, modelo, debug=True)
    sampler = falso_sampler.instancias[0]
    assert codigo == 0 and colapsa(sampler.estados) == [
        "starting", "waiting", "acquiring", "predicting", "writing", "stopped"]
    assert sampler.final == "stopped" and sampler.args[0] == ""  # sem banco operacional
    assert sampler.args[1] == str(base) and sampler.args[2] == str(base / "runtime_metrics.csv")
    assert sampler.args[3:5] == ("runtime", "") and sampler.sessoes and sampler.sessoes[0].endswith("-C000001")


def test_sampler_termina_mesmo_com_erro_inesperado_e_o_erro_fica_no_log(
        tmp_path, monkeypatch, sem_espera, modelo, falso_sampler):
    monkeypatch.setattr(daemon, "infer_once", lambda *a, **k: (_ for _ in ()).throw(ValueError("boom")))
    codigo, _, base = executa(tmp_path, monkeypatch, sem_espera, modelo, debug=True)
    log = (base / "runtime.log").read_text(encoding="utf-8")
    assert codigo == 1 and "unexpected_error" in log and "boom" in log and "runtime_stopped" in log
    assert falso_sampler.instancias[0].final == "failed"


def test_erro_de_porta_e_registrado_no_log_no_historico_e_no_estado_failed(tmp_path, monkeypatch, sem_espera, caplog):
    config = daemon.load_config(write_config(tmp_path, TCC_DEBUG="1", TCC_METHOD="wknn"))
    daemon.prepare_output_paths(config)
    Modem(monkeypatch, sem_espera, leitura(), falha_conexao=True)
    monkeypatch.setattr(daemon, "wait_for_modem", lambda *a: True)
    sampler = FalsoSampler()
    sessao = daemon.DebugSession(config, sampler, "run", None)
    with caplog.at_level(logging.INFO, logger="runtime.daemon"):
        daemon.run_loop(config, None, None, threading.Event(), once=True, session=sessao)
    registro = [e for e in historico(tmp_path / "runtime") if "event" not in e][0]
    assert registro["status"] == "unavailable" and registro["reason"].startswith("FN990 indisponível")
    assert registro["cycle_id"] == "run-C000001" and registro["snapshot_status"] is None
    assert registro["gnss_status"] is None and registro["wknn_gnss_distance_m"] is None
    assert colapsa(sampler.estados) == ["waiting", "acquiring", "failed", "writing"]
    assert "hardware_error" in caplog.text and "cycle_unavailable" in caplog.text
    assert list((tmp_path / "runtime" / "snapshots").glob("*.json")) == []  # sem snapshot, sem arquivo bruto


class RelogioFalso:
    def __init__(self):
        self.agora = 1000.0

    def monotonic(self):
        return self.agora

    perf_counter = monotonic


class ParaDepois(threading.Event):
    """Evento que, em vez de dormir, avança o relógio falso e para depois de `esperas` esperas."""

    def __init__(self, relogio, esperas, jitter):
        super().__init__()
        self.relogio, self.restantes, self.jitter, self.pedidos = relogio, esperas, jitter, []

    def wait(self, timeout=None):
        self.pedidos.append(timeout)
        self.relogio.agora += timeout + self.jitter
        self.restantes -= 1
        if self.restantes <= 0:
            self.set()
        return self.is_set()


def test_atraso_de_agendamento_e_ciclo_maior_que_o_periodo_sem_paralelismo(tmp_path, monkeypatch):
    config = daemon.load_config(write_config(tmp_path, TCC_DEBUG="1", TCC_INTERVAL="2"))
    daemon.prepare_output_paths(config)
    relogio = RelogioFalso()
    monkeypatch.setattr(daemon, "time", relogio)
    monkeypatch.setattr(daemon, "wait_for_modem", lambda *a: True)
    ativo = []

    def ciclo_lento(*args):
        assert not ativo, "ciclos não podem rodar em paralelo"
        ativo.append(1)
        relogio.agora += 5.0  # o ciclo dura mais que TCC_INTERVAL=2
        ativo.pop()
        return result("valid", 1.0, 2.0)

    monkeypatch.setattr(daemon, "infer_once", ciclo_lento)
    parar = ParaDepois(relogio, esperas=3, jitter=0.25)
    sessao = daemon.DebugSession(config, FalsoSampler(), "run", None)
    assert daemon.run_loop(config, None, None, parar, session=sessao) == 0
    ciclos = [e for e in historico(tmp_path / "runtime") if "event" not in e]
    assert len(ciclos) == 3 and parar.pedidos == [2, 2, 2]  # mesma regra: espera TCC_INTERVAL depois de cada ciclo
    assert [c["cycle_total_s"] for c in ciclos] == [5.0, 5.0, 5.0]
    assert [c["period_s"] for c in ciclos] == [None, 7.25, 7.25]  # ciclo + intervalo + atraso, nada em paralelo
    assert [c["schedule_delay_s"] for c in ciclos] == [0.0, 0.25, 0.25]
    assert ciclos[1]["scheduled_utc"] > ciclos[0]["started_utc"] and ciclos[0]["started_utc"] <= ciclos[0]["finished_utc"]


def plataforma_simulada(tmp_path, monkeypatch, os_id, modelo_cpu):
    proc = tmp_path / "proc"
    proc.mkdir(exist_ok=True)
    (proc / "cpuinfo").write_text(f"processor\t: 0\nprocessor\t: 1\nModel\t\t: {modelo_cpu}\nHardware\t: BCM2835\n")
    (proc / "meminfo").write_text("MemTotal:        948304 kB\nMemAvailable:    500000 kB\n")
    release = tmp_path / "os-release"
    release.write_text(f'ID={os_id}\nNAME="{os_id.title()}"\nVERSION_ID=12\n')
    monkeypatch.setattr(configure, "OS_RELEASE", release)
    monkeypatch.setattr(configure, "DEVICE_MODEL", tmp_path / "sem-modelo")
    return proc


@pytest.mark.parametrize("os_id,esperado", [("raspbian", "raspberrypi_os"), ("buildroot", "buildroot")])
def test_manifesto_da_execucao_no_raspberry_pi_os_e_no_buildroot(tmp_path, monkeypatch, os_id, esperado):
    proc = plataforma_simulada(tmp_path, monkeypatch, os_id, "Raspberry Pi 3 Model B Rev 1.2")
    config = daemon.load_config(write_config(tmp_path, TCC_DEBUG="1", TCC_METHOD="wknn", TCC_MODEL="/m/w.json"))
    manifesto = daemon.build_run_manifest(config, "run-1", "2026-11-26T12:00:00.000Z", "abc", proc_root=str(proc))
    assert (manifesto["platform_id"], manifesto["os_release"]["ID"], manifesto["run_id"]) == (esperado, os_id, "run-1")
    assert manifesto["cpu"]["cores"] == 2 and manifesto["memory_total_kb"] == 948304
    assert (manifesto["tcc_debug"], manifesto["interval_s"], manifesto["method"], manifesto["port"]) == (
        1, 240, "wknn", "/dev/ttyTEST")
    assert (manifesto["model"], manifesto["model_sha256"]) == ("/m/w.json", "abc")
    assert set(manifesto["paths"]) == {"output", "history", "log", "metrics", "snapshots", "manifest"}
    assert manifesto["kernel"] and manifesto["architecture"] and manifesto["python"] and manifesto["hostname"]
    assert not {"imei", "imsi", "iccid", "password", "environ"} & set(json.dumps(manifesto).lower().replace(
        '"', " ").split())


def test_manifesto_sem_git_temperatura_ou_proc_nao_falha(tmp_path, monkeypatch):
    monkeypatch.setattr(configure, "OS_RELEASE", tmp_path / "sem-os-release")
    monkeypatch.setattr(configure, "DEVICE_MODEL", tmp_path / "sem-modelo")
    monkeypatch.setattr(monitor, "git_commit", lambda root: None)
    config = daemon.load_config(write_config(tmp_path, TCC_DEBUG="1", TCC_METHOD="gnss"))
    manifesto = daemon.build_run_manifest(config, "run-2", "t", None, proc_root=str(tmp_path / "sem-proc"))
    assert (manifesto["git_commit"], manifesto["platform_id"], manifesto["os_release"], manifesto["cpu"],
            manifesto["memory_total_kb"], manifesto["model"], manifesto["model_sha256"]) == (
        None, None, None, None, None, None, None)


def test_manifesto_anterior_e_preservado_quando_a_execucao_e_reiniciada(tmp_path):
    caminho = tmp_path / "eng" / "runtime_manifest.json"
    daemon._write_manifest(str(caminho), {"run_id": "primeira"})
    daemon._write_manifest(str(caminho), {"run_id": "segunda"})
    assert json.loads(caminho.read_text(encoding="utf-8"))["run_id"] == "segunda"
    assert json.loads((caminho.parent / "runtime_manifest.primeira.json").read_text(encoding="utf-8"))["run_id"] == (
        "primeira")


def test_encerramento_limpo_por_sigterm_e_keyboardinterrupt(tmp_path, monkeypatch, falso_sampler):
    caminho = write_config(tmp_path, TCC_DEBUG="1")
    capturados = {}
    monkeypatch.setattr(daemon.signal, "signal", lambda numero, tratador: capturados.setdefault(numero, tratador))

    def sigterm(config, model, registry, stop_event, once=False, session=None):
        capturados[daemon.signal.SIGTERM](daemon.signal.SIGTERM, None)  # o handler instalado pelo main
        assert stop_event.is_set()
        return 0

    monkeypatch.setattr(daemon, "run_loop", sigterm)
    assert daemon.main(["--config", caminho]) == 0 and falso_sampler.instancias[-1].final == "stopped"

    def interrompido(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(daemon, "run_loop", interrompido)
    assert daemon.main(["--config", caminho]) == 0 and falso_sampler.instancias[-1].final == "stopped"
    assert "runtime_stopped" in (tmp_path / "logs" / "runtime.log").read_text(encoding="utf-8")


def test_exemplo_de_runtime_conf_e_o_servico_buildroot_usam_o_mesmo_daemon():
    exemplo = ROOT / "buildroot-external-tcc" / "board" / "tcc-rpi3" / "rootfs-overlay" / "etc" / "tcc" / "runtime.conf.example"
    config = daemon.load_config(str(exemplo))
    assert (config.mode, config.debug, config.metrics, config.snapshots_dir, config.run_manifest) == (
        "manual", False, "", "", "")
    for chave in ("TCC_DEBUG=0", "TCC_METRICS=", "TCC_SNAPSHOTS_DIR=", "TCC_RUN_MANIFEST="):
        assert chave in exemplo.read_text(encoding="utf-8").splitlines()
    servico = (exemplo.parents[1] / "init.d" / "S98tcc-runtime").read_text(encoding="utf-8")
    assert servico.count("-m runtime.daemon") == 2 and "TCC_DEBUG" not in servico and "campaign_tools" not in servico


def test_position_validate_e_live_nao_conhecem_o_modo_debug():
    from campaign_tools import position, validate
    from estimador import live

    for modulo in (position, validate, live):
        assert "TCC_DEBUG" not in Path(modulo.__file__).read_text(encoding="utf-8")