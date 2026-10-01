"""Avaliação do Estimador: métricas, compare (WKNN e Cell-ID no mesmo teste), cell-id e live (Fase 5)."""
import csv
import json
import math
import sqlite3

import pytest

from radiomapa_sintetico import CELULAS, criar_radiomapa, para_latlon

import estimador.evaluate as ev
import estimador.train as tr
from estimador import db as estimador_db
from estimador.dataset import load_dataset

GRADE = tr.Grid(k=(1, 3))
LOCAL = "LTE:72406:" + format(CELULAS[-1][4], "X")  # célula vista só em P6 (servidora lá)
NR = next(f"NR:{c[3]}:{c[4]:X}" for c in CELULAS if c[0] == "NR")


def cadastro(caminho, sem=()):
    cells = []
    for rat, arfcn, pci, plmn, cell_id, _status, leste, norte, _alcance in CELULAS:
        chave = None if cell_id is None else f"{rat}:{plmn}:{cell_id:X}"
        if chave and chave not in sem:
            lat, lon = para_latlon(leste, norte)
            cells.append({"global_cell_key": chave, "lat": lat, "lon": lon, "arfcn": arfcn, "pci": pci})
    caminho.write_text(json.dumps({"registry_version": 2, "cells": cells}), encoding="utf-8")
    return str(caminho)


@pytest.fixture(scope="module")
def treinado(tmp_path_factory):
    pasta = tmp_path_factory.mktemp("avaliacao")
    banco = str(pasta / "radiomapa_v2.db")
    criar_radiomapa(banco)
    modelo = str(pasta / "wknn_lte_spatial.json")
    bundle, _ = tr.train(banco, "LTE", "spatial", modelo, grid=GRADE)
    return banco, modelo, bundle


def test_metricas_de_erro():
    s = ev.error_summary([4.0, 1.0, 3.0, 2.0], n_total=5)
    assert (s["n"], s["n_total"], s["coverage"], s["mean_m"], s["median_m"], s["max_m"]) == (4, 5, 0.8, 2.5, 2.5, 4.0)
    assert s["rmse_m"] == pytest.approx(math.sqrt(7.5))
    assert (s["p90_m"], s["p95_m"]) == (pytest.approx(3.7), pytest.approx(3.85))
    assert s["cdf_m"] == [1.0, 2.0, 3.0, 4.0]
    vazio = ev.error_summary([], n_total=3)
    assert (vazio["coverage"], vazio["mean_m"], vazio["cdf_m"]) == (0.0, None, [])


def test_compare_avalia_os_dois_metodos_nas_mesmas_medicoes_de_teste(treinado, tmp_path):
    banco, modelo, bundle = treinado
    r = ev.compare(banco, modelo, cadastro(tmp_path / "cadastro.json"))
    ids = bundle["dataset"]["test_measurement_ids"]
    assert (r["protocol"], r["group_column"], r["n_test_samples"]) == ("spatial", "reference_point_id", len(ids))
    por_metodo = {m: [p["measurement_id"] for p in r["predictions"] if p["method"] == m] for m in ("wknn", "cell_id")}
    assert por_metodo["wknn"] == por_metodo["cell_id"] == ids
    assert all(p["protocol"] == "spatial" and p["group_id"] in r["test_groups"] for p in r["predictions"])
    for resumo in r["methods"].values():
        assert resumo["n_total"] == len(ids) and sum(resumo["status_counts"].values()) == len(ids)
    assert r["methods"]["wknn"]["mean_m"] == pytest.approx(bundle["validation"]["test_metrics"]["mean_m"])
    assert r["methods"]["cell_id"]["status_counts"] == {"valid": len(ids)}
    t = r["timings_s"]
    assert (t["n_predictions"], t["n_fingerprints"]) == (len(ids), len(bundle["fingerprints"]["x_m"]))
    assert t["load_bundle_s"] >= 0 and 0 <= t["wknn_predict_mean_s"] <= t["wknn_predict_p95_s"] <= t["wknn_predict_max_s"]


def test_compare_registra_o_protocolo_temporal(treinado, tmp_path):
    banco, _, _ = treinado
    modelo = str(tmp_path / "wknn_lte_temporal.json")
    bundle, _ = tr.train(banco, "LTE", "temporal", modelo, grid=GRADE)
    r = ev.compare(banco, modelo)
    assert (r["protocol"], r["group_column"], list(r["methods"])) == ("temporal", "session_id", ["wknn"])
    assert r["test_groups"] == sorted(bundle["validation"]["split"]["test_groups"])


def test_compare_recusa_banco_alterado_depois_do_treino(tmp_path):
    banco = str(tmp_path / "radiomapa_v2.db")
    criar_radiomapa(banco)
    modelo = str(tmp_path / "modelo.json")
    bundle, _ = tr.train(banco, "LTE", "spatial", modelo, grid=tr.Grid(k=(1,)))
    alvo = bundle["dataset"]["test_measurement_ids"][0]
    conn = sqlite3.connect(banco)
    try:
        conn.execute("UPDATE observed_cells SET rsrp_dbm = rsrp_dbm - 1 WHERE measurement_id = ?", (alvo,))
        conn.commit()
        with pytest.raises(ev.EvaluationError, match="test_samples_changed"):
            ev.compare(banco, modelo)
        conn.execute("UPDATE measurements SET legacy_ambiguous = 1 WHERE measurement_id = ?", (alvo,))
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(ev.EvaluationError, match="test_samples_missing"):
        ev.compare(banco, modelo)


def test_cell_id_report_conta_os_estados_por_celula(treinado, tmp_path):
    banco, _, _ = treinado
    r = ev.cell_id_report(banco, cadastro(tmp_path / "parcial.json", sem={LOCAL}), allow_single_gnss=True)
    amostras = load_dataset(banco, allow_single_gnss=True).samples
    desconhecidas = sum(1 for s in amostras if s.serving_global_cell_key == LOCAL)
    assert desconhecidas > 0
    assert r["summary"]["status_counts"] == {"missing_global_cell_key": 3, "unknown_cell": desconhecidas,
                                             "valid": len(amostras) - 3 - desconhecidas}
    celulas = {c["global_cell_key"]: c for c in r["cells"]}
    assert (celulas[LOCAL]["status"], celulas[LOCAL]["median_m"]) == ("unknown_cell", None)
    assert celulas["-"]["status"] == "missing_global_cell_key"
    assert NR in r["registered_not_seen"]


def test_live_report_resume_o_que_o_live_gravou(tmp_path):
    caminho = str(tmp_path / "estimador_estimativas.db")
    conn = estimador_db.connect(caminho)
    for erro in (10.0, 20.0, 30.0):
        estimador_db.insert_estimate(conn, "cell_id", -26.9, -49.07, n_amostras=1, lat_real=-26.9, lon_real=-49.07,
                                     erro_m=erro)
    estimador_db.insert_estimate(conn, "fingerprint", None, None)
    estimador_db.insert_estimate(conn, "fingerprint", -26.9, -49.07, n_amostras=1)
    conn.close()
    r = ev.live_report(caminho)
    cid, fp = r["methods"]["cell_id"], r["methods"]["fingerprint"]
    assert (cid["n_total"], cid["n_valid"], cid["n"], cid["mean_m"], cid["max_m"]) == (3, 3, 3, 20.0, 30.0)
    assert (fp["n_total"], fp["n_valid"], fp["coverage"], fp["n"], fp["mean_m"]) == (2, 1, 0.5, 0, None)
    with pytest.raises(ev.EvaluationError, match="live_db_not_found"):
        ev.live_report(str(tmp_path / "nao_existe.db"))
    assert not (tmp_path / "nao_existe.db").exists()


def test_cli_compare_grava_resumo_e_estimativas(treinado, tmp_path, capsys):
    banco, modelo, bundle = treinado
    resumo, estimativas = tmp_path / "resumo.csv", tmp_path / "estimativas.csv"
    ev.main(["compare", "--db", banco, "--model", modelo, "--registry", cadastro(tmp_path / "c.json"),
             "--out", str(resumo), "--predictions-out", str(estimativas)])
    assert "Protocolo: spatial (grupo = reference_point_id)" in capsys.readouterr().out
    with open(resumo, encoding="utf-8", newline="") as f:
        linhas = list(csv.DictReader(f))
    assert [l["method"] for l in linhas] == ["wknn", "cell_id"] and {l["protocol"] for l in linhas} == {"spatial"}
    with open(estimativas, encoding="utf-8", newline="") as f:
        assert len(list(csv.DictReader(f))) == 2 * len(bundle["dataset"]["test_measurement_ids"])
    with pytest.raises(SystemExit, match="já existe"):
        ev.main(["compare", "--db", banco, "--model", modelo, "--out", str(resumo)])


def test_cli_recusa_cadastro_invalido_e_bundle_antigo(treinado, tmp_path):
    banco, _, _ = treinado
    ruim = tmp_path / "ruim.json"
    ruim.write_text(json.dumps({"254": {"lat": -26.9, "lon": -49.07}}), encoding="utf-8")
    with pytest.raises(SystemExit, match="invalid_registry"):
        ev.main(["cell-id", "--db", banco, "--registry", str(ruim)])
    antigo = tmp_path / "fingerprint_5g.joblib"
    antigo.write_bytes(b"\x80\x04modelo antigo")
    with pytest.raises(SystemExit, match="não é mais aceito"):
        ev.main(["compare", "--db", banco, "--model", str(antigo)])
