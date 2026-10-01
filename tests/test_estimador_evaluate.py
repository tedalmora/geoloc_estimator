"""Avaliação do Estimador: métricas, compare (WKNN e Cell-ID nas mesmas tentativas) e cell-id."""
import csv
import json
import math
import sqlite3

import pytest

from radiomapa_sintetico import CAMPAIGN_PLMN, CELULAS, celulas_vistas, criar_radiomapa, inserir_medicao, para_latlon

import coletor.db as cdb
import estimador.evaluate as ev
import estimador.train as tr
from estimador.dataset import REF_REGISTERED, load_dataset
from software_version import software_version

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


def test_compare_avalia_os_dois_metodos_nas_mesmas_tentativas_de_teste(treinado, tmp_path):
    banco, modelo, bundle = treinado
    r = ev.compare(banco, modelo, cadastro(tmp_path / "cadastro.json"))
    ids = bundle["dataset"]["test_measurement_ids"]
    n = len(ids)
    assert (r["protocol"], r["group_column"], r["n_test_samples"], r["n_attempts"]) == ("spatial", "reference_point_id", n, n)
    por_metodo = {m: [p["measurement_id"] for p in r["predictions"] if p["method"] == m] for m in ("wknn", "cell_id")}
    assert por_metodo["wknn"] == por_metodo["cell_id"] == ids
    assert all(p["protocol"] == "spatial" and p["group_id"] in r["test_groups"] for p in r["predictions"])
    wknn, cid = r["methods"]["wknn"], r["methods"]["cell_id"]
    for resumo in (wknn, cid):
        assert (resumo["n_attempts"], resumo["n_eligible"], resumo["n_valid"], resumo["n_failed"]) == (n, n, n, 0)
        assert resumo["operational_coverage"] == resumo["specific_coverage"] == 1.0
    assert wknn["mean_m"] == pytest.approx(bundle["validation"]["test_metrics"]["mean_m"])
    assert cid["status_counts"] == {"valid": n}
    comum = r["common_subset"]
    assert comum["n"] == n and comum["methods"]["wknn"]["mean_m"] == pytest.approx(wknn["mean_m"])
    assert (r["software_version"], r["model_software_version"]) == (software_version(), bundle["software_version"])
    assert r["environments"] == {"outdoor": n}
    assert (r["reference"]["status"], r["reference"]["sources"]) == ("final", {REF_REGISTERED: n})
    assert list(r["by_point_type"]) == ["new_points"]  # no spatial, o ponto de teste nunca está no treino
    assert len(r["warnings"]) == 1 and "único grupo" in r["warnings"][0]
    assert {p["reference_source"] for p in r["predictions"]} == {REF_REGISTERED}
    t = r["timings_s"]
    assert (t["n_predictions"], t["n_fingerprints"]) == (n, len(bundle["fingerprints"]["x_m"]))
    assert t["load_bundle_s"] >= 0 and 0 <= t["wknn_predict_mean_s"] <= t["wknn_predict_p95_s"] <= t["wknn_predict_max_s"]


def test_cobertura_conta_tambem_as_tentativas_que_o_wknn_nao_aceita(tmp_path):
    banco = str(tmp_path / "radiomapa_v2.db")
    criar_radiomapa(banco)
    modelo = str(tmp_path / "modelo.json")
    bundle, _ = tr.train(banco, "LTE", "spatial", modelo, grid=tr.Grid(k=(1,)))
    (ponto,) = bundle["validation"]["split"]["test_groups"]
    conn = cdb.connect(banco)
    try:
        servidora = conn.execute("SELECT serving_global_cell_key FROM measurements WHERE reference_point_id = ?",
                                 (ponto,)).fetchone()[0]
        with conn:
            sid = cdb.create_session(conn, mode="continuous", scenario="estatico", environment="indoor",
                                     modem_id="sintetico", metadata={}, reference_point_id=ponto,
                                     campaign_plmn=CAMPAIGN_PLMN)
            # #CSURV sem resposta (a servidora foi lida) e snapshot inválido: não são amostras do WKNN
            inserir_medicao(conn, sid, 1, ponto, "2026-09-05T10:00:01.000Z", [], serving_key=servidora,
                            snapshot_status="partial", csurv_status="timeout")
            inserir_medicao(conn, sid, 2, ponto, "2026-09-05T10:00:02.000Z", [], snapshot_status="invalid",
                            csurv_status="timeout")
    finally:
        conn.close()
    r = ev.compare(banco, modelo, cadastro(tmp_path / "cadastro.json"))
    n = len(bundle["dataset"]["test_measurement_ids"])
    wknn, cid = r["methods"]["wknn"], r["methods"]["cell_id"]
    assert (r["n_test_samples"], r["n_attempts"]) == (n, n + 2)
    assert (wknn["n_attempts"], wknn["n_eligible"], wknn["n_valid"], wknn["n_failed"]) == (n + 2, n, n, 2)
    assert (cid["n_attempts"], cid["n_eligible"], cid["n_valid"], cid["n_failed"]) == (n + 2, n + 1, n + 1, 1)
    assert (wknn["status_counts"], cid["status_counts"]) == ({"unavailable": 2, "valid": n},
                                                             {"unavailable": 1, "valid": n + 1})
    assert wknn["operational_coverage"] == pytest.approx(n / (n + 2)) and wknn["specific_coverage"] == 1.0
    assert cid["operational_coverage"] == pytest.approx((n + 1) / (n + 2)) and cid["specific_coverage"] == 1.0
    assert (wknn["n_errors"], cid["n_errors"], r["common_subset"]["n"]) == (n, n + 1, n)
    assert r["environments"] == {"indoor": 2, "outdoor": n}
    extras = [p for p in r["predictions"] if p["environment"] == "indoor"]
    assert [(p["method"], p["eligible"], p["status"]) for p in extras] == [
        ("wknn", False, "unavailable"), ("cell_id", True, "valid"), ("wknn", False, "unavailable"),
        ("cell_id", False, "unavailable")]


def test_compare_temporal_separa_pontos_conhecidos_de_pontos_novos(tmp_path):
    banco = str(tmp_path / "radiomapa_v2.db")
    criar_radiomapa(banco)
    conn = cdb.connect(banco)
    try:
        with conn:
            novo = cdb.add_reference_point(conn, "P7", *para_latlon(60, 0), coord_source="sintetico",
                                           coord_uncertainty_m=0.5)
            sid = cdb.create_session(conn, mode="continuous", scenario="estatico", environment="outdoor",
                                     modem_id="sintetico", metadata={}, reference_point_id=novo,
                                     campaign_plmn=CAMPAIGN_PLMN)
            conn.execute("UPDATE sessions SET started_at = '2026-09-04T10:00:00.000Z' WHERE session_id = ?", (sid,))
            for seq in range(1, 4):
                inserir_medicao(conn, sid, seq, novo, f"2026-09-04T10:00:{seq:02d}.000Z", celulas_vistas(60, 0, f"p7-{seq}"))
    finally:
        conn.close()
    modelo = str(tmp_path / "temporal.json")
    bundle, _ = tr.train(banco, "LTE", "temporal", modelo, grid=GRADE)
    v = bundle["validation"]
    assert v["test_metrics"]["composition"] == {"known_points": 9, "new_points": 3}
    assert {k: m["n_total"] for k, m in v["test_metrics_by_point_type"].items()} == {"known_points": 9, "new_points": 3}
    r = ev.compare(banco, modelo)
    assert {k: m["wknn"]["n_attempts"] for k, m in r["by_point_type"].items()} == {"known_points": 9, "new_points": 3}
    assert r["methods"]["wknn"]["n_attempts"] == 12 and r["common_subset"] is None
    assert r["reference"]["status"] == "exploratoria" and r["reference"]["gnss_median_shared_points"] == [5]
    assert any(f"pontos [{novo}]" in w and "extrapolação espacial" in w for w in r["warnings"])
    assert any("pontos [5]" in w and "independência temporal completa" in w for w in r["warnings"])
    assert all(p["reference_shared_between_sessions"] == (p["reference_point_id"] == 5) for p in r["predictions"])


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


def test_cell_id_report_usa_todas_as_tentativas(treinado, tmp_path):
    banco, _, _ = treinado
    r = ev.cell_id_report(banco, cadastro(tmp_path / "parcial.json", sem={LOCAL}), allow_single_gnss=True)
    tentativas = load_dataset(banco, allow_single_gnss=True).attempts
    desconhecidas = sum(1 for a in tentativas if a.serving_global_cell_key == LOCAL)
    assert desconhecidas > 0 and r["n_attempts"] == len(tentativas) == 59
    # sem chave: 3 da rota e o #CSURV vazio; indisponível: o snapshot inválido
    assert r["summary"]["status_counts"] == {"missing_global_cell_key": 4, "unavailable": 1,
                                             "unknown_cell": desconhecidas, "valid": 59 - 5 - desconhecidas}
    assert (r["summary"]["n_eligible"], r["summary"]["n_valid"]) == (54, 54 - desconhecidas)
    celulas = {c["global_cell_key"]: c for c in r["cells"]}
    assert (celulas[LOCAL]["status_counts"], celulas[LOCAL]["median_m"]) == ({"unknown_cell": desconhecidas}, None)
    assert celulas["-"]["status_counts"] == {"missing_global_cell_key": 4, "unavailable": 1}
    assert NR in r["registered_not_seen"]
    assert r["reference"]["status"] == "exploratoria" and "cadastre coordenadas" in r["warnings"][0]


def test_cli_compare_grava_resumo_e_estimativas(treinado, tmp_path, capsys):
    banco, modelo, bundle = treinado
    resumo, estimativas = tmp_path / "resumo.csv", tmp_path / "estimativas.csv"
    ev.main(["compare", "--db", banco, "--model", modelo, "--registry", cadastro(tmp_path / "c.json"),
             "--out", str(resumo), "--predictions-out", str(estimativas)])
    texto = capsys.readouterr().out
    assert "Protocolo: spatial (grupo = reference_point_id)" in texto and "Referência: final" in texto
    assert "AVISO: o teste tem um único grupo" in texto
    with open(resumo, encoding="utf-8", newline="") as f:
        linhas = list(csv.DictReader(f))
    assert [(l["method"], l["subset"]) for l in linhas] == [("wknn", "tentativas"), ("cell_id", "tentativas"),
                                                            ("wknn", "comum"), ("cell_id", "comum")]
    assert {l["protocol"] for l in linhas} == {"spatial"} and {l["software_version"] for l in linhas} == {software_version()}
    assert linhas[0]["operational_coverage"] == "1.0" and linhas[2]["operational_coverage"] == ""
    with open(estimativas, encoding="utf-8", newline="") as f:
        linhas = list(csv.DictReader(f))
    assert len(linhas) == 2 * len(bundle["dataset"]["test_measurement_ids"])
    assert {(l["eligible"], l["environment"], l["reference_source"]) for l in linhas} == {("True", "outdoor", REF_REGISTERED)}
    with pytest.raises(SystemExit, match="já existe"):
        ev.main(["compare", "--db", banco, "--model", modelo, "--out", str(resumo)])


def test_cli_sem_subcomando_live(treinado):
    with pytest.raises(SystemExit) as saida:
        ev.main(["live", "--db", treinado[0]])
    assert saida.value.code == 2


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
