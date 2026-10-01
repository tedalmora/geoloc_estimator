"""Inferência atual (fase 6): live.py once com WKNN ou Cell-ID sobre 1 snapshot de modem simulado."""
import hashlib
import json
import math
import os
import subprocess
import sys

import pytest

from radiomapa_sintetico import CAMPAIGN_PLMN, criar_radiomapa
from support import driver_com

import estimador.live as live
import estimador.train as tr
from estimador.dataset import CsurvCell
from estimador.geometry import EnuFrame, haversine_m
from estimador.methods.fingerprint import WknnModel

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# (EARFCN, PCI, cellId decimal, RSRP): células do radiomapa sintético, vistas pela leitura simulada
CELULAS_LIDAS = [(3375, 254, 257298023, -95), (3375, 31, 257298024, -101), (1350, 64, 257298030, -104),
                 (2850, 64, 257298031, -109)]
EXTRA = (500, 7, 257298099, -90)  # radio_key fora do vocabulário do bundle


def csurv(*registros):
    linhas = ["AT#CSURV", "Network survey started ..."]
    for earfcn, pci, cell_id, rsrp in registros:
        linhas += [f"earfcn: {earfcn} rxLev: -70 mcc: 724 mnc: 06 cellId: {cell_id} tac: 34747 phyCellId: {pci}",
                   f"cellStatus: CELL_SUITABLE rsrp: {rsrp} rsrq: -12 bw: 15"]
    return linhas + ["OK"]


def leitura(csurv_linhas=None, **trocas):
    roteiro = {"AT#MONI": "moni_lte.txt", "AT#SERVINFO": "servinfo_lte.txt",
               "AT#CSURV": csurv(*CELULAS_LIDAS) if csurv_linhas is None else csurv_linhas,
               "AT$GPSACP": "gpsacp_fix.txt"}
    roteiro.update(trocas)
    return roteiro


def rodar(monkeypatch, relogio, roteiro, *argv):
    driver = driver_com(roteiro, relogio)
    driver.connect = lambda *args, **kwargs: None
    monkeypatch.setattr(live, "FN990Driver", lambda: driver)
    return live.main(list(argv)), driver


def resultado(monkeypatch, relogio, capsys, roteiro, *argv):
    codigo, driver = rodar(monkeypatch, relogio, roteiro, *argv, "--json")
    return codigo, json.loads(capsys.readouterr().out), driver


def cadastro(tmp_path, chave="LTE:72406:F560E67", lat=-26.9, lon=-49.07):
    caminho = tmp_path / "registry.json"
    caminho.write_text(json.dumps({"registry_version": 2, "cells": [{"global_cell_key": chave, "lat": lat, "lon": lon}]}),
                       encoding="utf-8")
    return str(caminho)


def derivar(bundle, tmp_path, nome, **mudancas):
    copia = json.loads(json.dumps(bundle))
    copia.update(mudancas)
    caminho = tmp_path / nome
    caminho.write_text(json.dumps(copia), encoding="utf-8")
    return str(caminho)


@pytest.fixture(scope="module")
def modelo(tmp_path_factory):
    pasta = tmp_path_factory.mktemp("live")
    banco = str(pasta / "radiomapa_v2.db")
    criar_radiomapa(banco)
    caminho = str(pasta / "wknn_lte_spatial.json")
    bundle, _ = tr.train(banco, "LTE", "spatial", caminho, grid=tr.Grid(k=(1, 3)))
    return caminho, bundle


# --------------------------------------------------------------------------- WKNN
def test_wknn_com_snapshot_completo(monkeypatch, sem_espera, capsys, modelo):
    caminho, bundle = modelo
    codigo, r, driver = resultado(monkeypatch, sem_espera, capsys, leitura(), "wknn", "--port", "P", "--model", caminho)
    usadas = {f"LTE:{e}:{p}" for e, p, _, _ in CELULAS_LIDAS} & set(bundle["cell_keys"])
    assert codigo == 0 and usadas
    assert (r["method"], r["valid"], r["status"], r["reason"], r["technology"], r["snapshot_status"]) == (
        "wknn", True, "valid", None, "LTE", "complete")
    assert all(isinstance(r[k], float) for k in ("x_m", "y_m", "lat", "lon"))
    assert (r["metadata"]["n_observed_keys"], r["metadata"]["n_ignored_keys"]) == (len(usadas), 4 - len(usadas))
    assert driver._ser.written == ["AT#MONI", "AT#SERVINFO", "AT#CSURV", "AT$GPSACP"]  # só leituras
    assert not driver._ser.is_open
    t = r["metadata"]["timings_s"]
    assert set(t) == {"load_s", "estimate_s", "end_to_end_s"} and t["end_to_end_s"] >= t["load_s"] + t["estimate_s"]


def test_wknn_com_snapshot_parcial_suficiente(monkeypatch, sem_espera, capsys, modelo):
    roteiro = leitura(**{"AT$GPSACP": "gpsacp_sem_fix.txt"})
    codigo, r, _ = resultado(monkeypatch, sem_espera, capsys, roteiro, "wknn", "--port", "P", "--model", modelo[0])
    assert codigo == 0 and r["valid"] and r["snapshot_status"] == "partial"
    assert r["metadata"]["snapshot"]["reasons"] == ["gnss:no_fix"]


def test_wknn_sem_csurv_nao_estima(monkeypatch, sem_espera, capsys, modelo):
    roteiro = leitura(**{"AT#CSURV": "csurv_vazio.txt"})
    codigo, r, _ = resultado(monkeypatch, sem_espera, capsys, roteiro, "wknn", "--port", "P", "--model", modelo[0])
    assert codigo == 1 and (r["status"], r["lat"], r["snapshot_status"]) == ("unavailable", None, "partial")
    assert "#CSURV" in r["reason"]


@pytest.mark.parametrize("metodo", ["wknn", "cell-id"])
def test_snapshot_invalido_nao_gera_estimativa(monkeypatch, sem_espera, capsys, modelo, tmp_path, metodo):
    roteiro = leitura(**{"AT#MONI": "error.txt", "AT#SERVINFO": "error.txt", "AT#CSURV": "csurv_abortado.txt"})
    opcao = ["--model", modelo[0]] if metodo == "wknn" else ["--registry", cadastro(tmp_path)]
    codigo, r, _ = resultado(monkeypatch, sem_espera, capsys, roteiro, metodo, "--port", "P", *opcao)
    assert codigo == 1
    assert (r["status"], r["valid"], r["lat"], r["lon"], r["snapshot_status"]) == ("unavailable", False, None, None, "invalid")
    assert r["reason"].startswith("snapshot inválido")


def test_bundle_incompativel_e_recusado_antes_de_abrir_a_porta(monkeypatch, capsys, modelo, tmp_path):
    monkeypatch.setattr(live, "FN990Driver", lambda: pytest.fail("a porta não deveria ser aberta"))
    antigo = tmp_path / "fingerprint_5g.joblib"
    antigo.write_bytes(b"\x80\x04modelo antigo")
    for caminho, trecho in ((derivar(modelo[1], tmp_path, "v1.json", model_version=1), "model_version"),
                            (str(antigo), "bundle antigo")):
        with pytest.raises(SystemExit) as saida:
            live.main(["wknn", "--port", "P", "--model", caminho])
        assert saida.value.code == 2 and trecho in capsys.readouterr().err


def test_tecnologia_incompativel(monkeypatch, sem_espera, capsys, modelo, tmp_path):
    bundle = modelo[1]
    nr = derivar(bundle, tmp_path, "nr.json", technology="NR",
                 cell_keys=["NR:" + k.split(":", 1)[1] for k in bundle["cell_keys"]],
                 cell_filter_policy=dict(bundle["cell_filter_policy"], technology="NR"))
    codigo, r, _ = resultado(monkeypatch, sem_espera, capsys, leitura(), "wknn", "--port", "P", "--model", nr)
    assert codigo == 1 and (r["status"], r["technology"], r["lat"]) == ("technology_mismatch", "NR", None)


def test_nenhuma_radio_key_compativel(monkeypatch, sem_espera, capsys, modelo, tmp_path):
    bundle = modelo[1]
    outro = derivar(bundle, tmp_path, "outro.json", cell_keys=[f"LTE:9{i:03d}:1" for i in range(len(bundle["cell_keys"]))])
    codigo, r, _ = resultado(monkeypatch, sem_espera, capsys, leitura(), "wknn", "--port", "P", "--model", outro)
    assert codigo == 1 and (r["status"], r["lat"], r["x_m"]) == ("no_matching_radio_keys", None, None)
    assert "nenhuma radio_key do bundle" in r["reason"] and r["metadata"]["n_ignored_keys"] == len(CELULAS_LIDAS)


def test_radio_keys_extras_sao_ignoradas_sem_mudar_o_bundle(monkeypatch, sem_espera, capsys, modelo):
    caminho = modelo[0]
    with open(caminho, "rb") as f:
        antes = hashlib.sha256(f.read()).hexdigest()
    _, base, _ = resultado(monkeypatch, sem_espera, capsys, leitura(), "wknn", "--port", "P", "--model", caminho)
    _, extra, _ = resultado(monkeypatch, sem_espera, capsys, leitura(csurv(*CELULAS_LIDAS, EXTRA)),
                            "wknn", "--port", "P", "--model", caminho)
    assert (extra["lat"], extra["lon"]) == (base["lat"], base["lon"])
    assert extra["metadata"]["n_ignored_keys"] == base["metadata"]["n_ignored_keys"] + 1
    with open(caminho, "rb") as f:
        assert hashlib.sha256(f.read()).hexdigest() == antes


def test_conversao_enu_para_lat_lon(modelo):
    bundle = modelo[1]
    fp, status = bundle["fingerprints"], bundle["cell_key_plmn_status"]
    vetor = fp["rsrp_dbm"][0]
    celulas = [CsurvCell(k, "LTE", CAMPAIGN_PLMN if status[k] == "campaign" else None, "CELL_SUITABLE", v)
               for k, v in zip(bundle["cell_keys"], vetor) if v != bundle["missing_value"]]
    est = WknnModel(bundle).predict(celulas)
    iguais = [j for j, v in enumerate(fp["rsrp_dbm"]) if v == vetor]  # todas as amostras exatas, mesmo se forem mais que k
    x = sum(fp["x_m"][j] for j in iguais) / len(iguais)
    y = sum(fp["y_m"][j] for j in iguais) / len(iguais)
    assert est.metadata["zero_distance"] and (est.x_m, est.y_m) == pytest.approx((x, y))
    origem = EnuFrame.from_dict(bundle["enu_origin"])
    lat, lon, _ = origem.to_geodetic(x, y)
    assert (est.lat, est.lon) == pytest.approx((lat, lon), abs=1e-9)
    assert haversine_m(origem.lat0, origem.lon0, est.lat, est.lon) == pytest.approx(math.hypot(x, y), abs=0.5)


# --------------------------------------------------------------------------- Cell-ID
def test_cell_id_com_cadastro_valido(monkeypatch, sem_espera, capsys, tmp_path):
    codigo, driver = rodar(monkeypatch, sem_espera, leitura(), "cell-id", "--port", "P", "--registry", cadastro(tmp_path))
    texto = capsys.readouterr().out
    assert codigo == 0 and "cell_id: valid -26.900000, -49.070000" in texto and "não do UE" in texto
    assert "snapshot complete" in texto and "ponta a ponta" in texto and not driver._ser.is_open


def test_cell_id_celula_desconhecida(monkeypatch, sem_espera, capsys, tmp_path):
    registro = cadastro(tmp_path, chave="LTE:72406:ABC")
    codigo, r, _ = resultado(monkeypatch, sem_espera, capsys, leitura(), "cell-id", "--port", "P", "--registry", registro)
    assert codigo == 1 and (r["method"], r["status"], r["lat"]) == ("cell_id", "unknown_cell", None)


def test_cell_id_sem_global_cell_key(monkeypatch, sem_espera, capsys, tmp_path):
    roteiro = leitura(**{"AT#SERVINFO": "error.txt", "AT#CSURV": "csurv_vazio.txt"})
    codigo, r, _ = resultado(monkeypatch, sem_espera, capsys, roteiro, "cell-id", "--port", "P", "--registry",
                             cadastro(tmp_path))
    assert codigo == 1 and (r["status"], r["snapshot_status"], r["lat"]) == ("missing_global_cell_key", "partial", None)


# --------------------------------------------------------------------------- CLI e isolamento
@pytest.mark.parametrize("mensagem", ["P: não foi possível abrir a porta (porta inexistente)",
                                      "P: o modem não respondeu OK a 'AT' em 3 tentativas (nenhuma resposta)"])
def test_fn990_indisponivel_tem_mensagem_clara(monkeypatch, tmp_path, mensagem):
    class SemModem:
        def connect(self, *args, **kwargs):
            raise ConnectionError(mensagem)

    monkeypatch.setattr(live, "FN990Driver", SemModem)
    with pytest.raises(SystemExit) as saida:
        live.main(["cell-id", "--port", "P", "--registry", cadastro(tmp_path)])
    assert saida.value.code == f"erro: FN990 indisponível: {mensagem}"


@pytest.mark.parametrize("metodo", ["ta", "aoa", "fingerprint", "once", "continuous"])
def test_so_wknn_e_cell_id_no_caminho_principal(metodo):
    with pytest.raises(SystemExit) as saida:
        live.main([metodo, "--port", "P"])
    assert saida.value.code == 2


def test_live_nao_importa_coletor_ta_lpp_nem_geometria_angular():
    codigo = ("import sys, estimador.live; print(sorted(m for m in sys.modules if m in ('coletor.daemon', 'hal.health', "
              "'estimador.methods.network_position', 'estimador.methods.aoa') "
              "or m.startswith('experimental')))")
    saida = subprocess.run([sys.executable, "-c", codigo], capture_output=True, text=True, cwd=RAIZ, check=True)
    assert saida.stdout.strip() == "[]"
