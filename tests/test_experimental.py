"""Componentes experimentais (fase 7): TA condicionado, geometria de bearings e sonda LPP só leitura.

Também confere o isolamento: hal/, acquisition/, coletor/ e estimador/ não importam experimental/.
"""
import json
import math
import os
import re
import subprocess
import sys

import pytest

from support import driver_com

import experimental.lpp_probe as lpp
from estimador.geometry import EnuFrame
from estimador.methods.cell_id import CellRegistry, RegistryEntry
from experimental.angular_geometry import intersect_bearings
from experimental.timing_advance import TaConversion, estimate_timing_advance
from experimental.timing_advance import main as ta_main

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACOTES_PRINCIPAIS = ("hal", "acquisition", "coletor", "estimador")
CHAVE = "LTE:72406:F560E67"
CADASTRO = CellRegistry({CHAVE: RegistryEntry(CHAVE, -26.9, -49.07, sector_azimuth_deg=120.0, sector_width_deg=65.0)})
CONVERSAO_TESTE = TaConversion("LTE", "passo de teste", 100.0, "valor fictício, só para teste")
A1 = (-26.9, -49.07)


def ponto(leste_m, norte_m):
    lat, lon, _ = EnuFrame(*A1).to_geodetic(leste_m, norte_m)
    return lat, lon


def arquivos_principais():
    for pacote in PACOTES_PRINCIPAIS:
        for pasta, _, nomes in os.walk(os.path.join(RAIZ, pacote)):
            for nome in nomes:
                if nome.endswith(".py"):
                    caminho = os.path.join(pasta, nome)
                    with open(caminho, encoding="utf-8") as f:
                        yield caminho, f.read()


# --------------------------------------------------------------------------- Timing Advance
def test_ta_ausente_e_unavailable_mesmo_com_conversao():
    r = estimate_timing_advance(None, "LTE", CHAVE, CADASTRO, CONVERSAO_TESTE)
    assert (r.status, r.lat, r.metadata) == ("unavailable", None, {})


def test_ta_zero_real_nao_e_ausencia():
    r = estimate_timing_advance(0, "LTE", CHAVE, CADASTRO, CONVERSAO_TESTE)
    assert r.valid and (r.metadata["ta"], r.metadata["estimated_range_m"]) == (0, 0.0)
    assert (r.metadata["range_min_m"], r.metadata["range_max_m"]) == (0.0, 50.0)


def test_ta_da_faixa_em_torno_da_antena_sem_usar_o_azimute_como_direcao():
    r = estimate_timing_advance(3, "LTE", CHAVE, CADASTRO, CONVERSAO_TESTE)
    assert (r.lat, r.lon, r.x_m, r.y_m) == (-26.9, -49.07, None, None)
    assert (r.metadata["range_min_m"], r.metadata["estimated_range_m"], r.metadata["range_max_m"]) == (250.0, 300.0, 350.0)
    assert r.metadata["experimental"] is True and "direção desconhecida" in r.metadata["hypothesis"]
    assert (r.metadata["sector_azimuth_deg"], r.metadata["sector_width_deg"]) == (120.0, 65.0)


@pytest.mark.parametrize("tecnologia", ["LTE", "NR"])
def test_ta_sem_conversao_confirmada_e_unsupported(tecnologia):
    r = estimate_timing_advance(3, tecnologia, CHAVE, CADASTRO)
    assert (r.status, r.lat) == ("unsupported", None) and "não confirmada" in r.reason


def test_ta_tecnologia_invalida_ou_diferente_da_conversao():
    assert estimate_timing_advance(3, "5G", CHAVE, CADASTRO, CONVERSAO_TESTE).status == "invalid_input"
    assert estimate_timing_advance(3, "NR", CHAVE, CADASTRO, CONVERSAO_TESTE).status == "technology_mismatch"


@pytest.mark.parametrize("ta", [-1, 2.5, True, "3"])
def test_ta_invalido(ta):
    assert estimate_timing_advance(ta, "LTE", CHAVE, CADASTRO, CONVERSAO_TESTE).status == "invalid_input"


def test_ta_com_celula_desconhecida_ou_sem_chave():
    assert estimate_timing_advance(3, "LTE", "LTE:72406:ABC", CADASTRO, CONVERSAO_TESTE).status == "unknown_cell"
    assert estimate_timing_advance(3, "LTE", None, CADASTRO, CONVERSAO_TESTE).status == "missing_global_cell_key"


@pytest.mark.parametrize("campos", [("LTE", "u", 0.0, "f"), ("LTE", "u", float("nan"), "f"), ("LTE", "", 10.0, "f"),
                                    ("LTE", "u", 10.0, " "), ("5G", "u", 10.0, "f"), ("LTE", "u", True, "f")])
def test_conversao_precisa_ser_declarada_por_completo(campos):
    with pytest.raises(ValueError):
        TaConversion(*campos)


def test_ta_com_posicao_direta_e_incerteza_configuravel():
    r = estimate_timing_advance(3, "LTE", None, None, CONVERSAO_TESTE, cell_position=(-26.9, -49.07), uncertainty_steps=1.0)
    assert (r.status, r.lat, r.lon, r.x_m) == ("valid", -26.9, -49.07, None)
    assert (r.metadata["range_min_m"], r.metadata["range_max_m"]) == (200.0, 400.0)
    assert r.metadata["position_source"] == "cell_position" and "não determina posição 2D" in r.metadata["limitation"]
    invalidos = [dict(cell_position=(91.0, 0.0)), dict(cell_position=(float("nan"), 0.0)), dict(cell_position=("0", "0")),
                 dict(cell_position=(0.0, 0.0), uncertainty_steps=0), dict(cell_position=(0.0, 0.0), uncertainty_steps=float("inf")),
                 dict(cell_position=(0.0, 0.0), uncertainty_steps=-1.0), dict(cell_position=(0.0, 0.0), uncertainty_steps=True),
                 dict(), dict(cell_position=(0.0, 0.0))]
    for extra in invalidos:
        registro = CADASTRO if extra == dict(cell_position=(0.0, 0.0)) else None
        assert estimate_timing_advance(3, "LTE", CHAVE, registro, CONVERSAO_TESTE, **extra).status == "invalid_input"


def test_cli_ta_informa_unavailable_unsupported_success_e_invalid_input(capsys):
    base = ["--technology", "LTE", "--cell-lat", "-26.9", "--cell-lon", "-49.07"]
    conversao = ["--unit", "passo de teste", "--meters-per-step", "100", "--conversion-source", "fictícia"]

    def roda(*args):
        codigo = ta_main(base + list(args))
        return codigo, json.loads(capsys.readouterr().out)

    assert roda()[1]["status"] == "unavailable"
    codigo, saida = roda("--ta", "3")
    assert (codigo, saida["status"], saida["valid"]) == (0, "unsupported", False)
    codigo, saida = roda("--ta", "0", *conversao)
    assert (codigo, saida["status"], saida["metadata"]["range_max_m"], saida["lat"]) == (0, "success", 50.0, -26.9)
    assert "posição 2D" in saida["note"]
    for args in (["--ta", "-1"], ["--ta", "x"], ["--ta", "3", "--unit", "u", "--conversion-source", "f"],
                 ["--ta", "3", "--unit", "u", "--meters-per-step", "nan", "--conversion-source", "f"],
                 ["--ta", "3", *conversao, "--uncertainty-steps", "0"]):
        codigo, saida = roda(*args)
        assert (codigo, saida["status"]) == (2, "invalid_input")
    assert ta_main(["--technology", "LTE", "--ta", "3", *conversao, "--cell-lat", "95", "--cell-lon", "0"]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "invalid_input"


def test_cli_ta_com_cadastro_via_modulo_sem_gravar_nada(tmp_path):
    cadastro = tmp_path / "cadastro.json"
    cadastro.write_text(json.dumps({"registry_version": 2, "cells": [{"global_cell_key": CHAVE, "lat": -26.9, "lon": -49.07}]}),
                        encoding="utf-8")
    antes = sorted(p.name for p in tmp_path.iterdir())
    cmd = [sys.executable, "-m", "experimental.timing_advance", "--technology", "LTE", "--ta", "2", "--unit", "u",
           "--meters-per-step", "100", "--conversion-source", "f", "--registry", str(cadastro), "--global-cell-key", CHAVE]
    saida = subprocess.run(cmd, capture_output=True, text=True, cwd=RAIZ, check=True)
    resultado = json.loads(saida.stdout)
    assert (resultado["status"], resultado["metadata"]["position_source"]) == ("success", "registry")
    assert sorted(p.name for p in tmp_path.iterdir()) == antes


# --------------------------------------------------------------------------- geometria de bearings
def test_cruzamento_ortogonal_conhecido():
    r = intersect_bearings(A1, 90.0, ponto(100, 100), 180.0)
    assert r.valid and (r.x_m, r.y_m) == pytest.approx((100.0, 0.0), abs=1e-6)
    assert (r.metadata["intersection_angle_deg"], r.metadata["angle_quality"]) == pytest.approx((90.0, 1.0))
    assert (r.metadata["range_from_anchor1_m"], r.metadata["range_from_anchor2_m"]) == pytest.approx((100.0, 100.0))


def test_semirretas_paralelas_ou_quase_paralelas():
    for bearing2 in (0.0, 180.0, 355.0):
        assert intersect_bearings(A1, 0.0, ponto(100, 0), bearing2).status == "geometry_degenerate"
    r = intersect_bearings(A1, 0.0, ponto(100, 0), 355.0, min_angle_deg=2.0)
    assert r.valid and r.metadata["intersection_angle_deg"] == pytest.approx(5.0)
    assert r.metadata["angle_quality"] == pytest.approx(math.sin(math.radians(5.0)))


def test_intersecao_atras_de_uma_ancora():
    r = intersect_bearings(A1, 270.0, ponto(100, 100), 180.0)
    assert (r.status, r.lat) == ("geometry_degenerate", None) and "âncora 1" in r.reason


def test_ancoras_iguais():
    assert intersect_bearings(A1, 0.0, A1, 90.0).status == "geometry_degenerate"


@pytest.mark.parametrize("bearing", [float("nan"), float("inf"), None, "90", True])
def test_bearing_invalido(bearing):
    assert intersect_bearings(A1, bearing, ponto(100, 100), 180.0).status == "invalid_input"


@pytest.mark.parametrize("ancora", [(91.0, 0.0), (0.0, 181.0), (float("nan"), 0.0), ("0", "0"), (0.0,)])
def test_coordenadas_invalidas(ancora):
    assert intersect_bearings(ancora, 90.0, A1, 180.0).status == "invalid_input"


def test_bearing_e_normalizado():
    a = intersect_bearings(A1, 90.0, ponto(100, 100), 180.0)
    b = intersect_bearings(A1, 450.0, ponto(100, 100), -180.0)
    assert (b.lat, b.lon) == pytest.approx((a.lat, a.lon)) and b.metadata["bearings_deg"] == [90.0, 180.0]


def test_solucao_longe_demais_e_rejeitada():
    r = intersect_bearings(A1, 0.0, ponto(100, 0), 359.0, min_angle_deg=0.5)
    assert r.status == "geometry_degenerate" and "limite" in r.reason


def test_ida_e_volta_do_plano_local():
    r = intersect_bearings(A1, 30.0, ponto(400, 0), 330.0)
    x, y, _ = EnuFrame(*A1).to_enu(r.lat, r.lon)
    assert r.valid and (x, y) == pytest.approx((r.x_m, r.y_m), abs=1e-6)
    assert (r.x_m, r.y_m) == pytest.approx((200.0, 200.0 / math.tan(math.radians(30.0))), abs=1e-6)
    assert math.degrees(math.atan2(r.x_m, r.y_m)) == pytest.approx(30.0)


# --------------------------------------------------------------------------- sonda LPP
def test_sonda_lpp_so_consulta_e_guarda_a_resposta_bruta(sem_espera):
    driver = driver_com({"AT$LCSLPP?": "lcslpp_consulta.txt"}, sem_espera)
    r = lpp.probe(driver)
    assert driver._ser.written == ["AT$LCSLPP?"]
    assert (r["command"], r["status"], r["final"], r["error"]) == ("AT$LCSLPP?", "ok", "OK", None)
    assert "$LCSLPP: 3" in r["raw_response"] and r["timestamp"].endswith("Z") and r["elapsed_s"] >= 0


@pytest.mark.parametrize("resposta, status", [("cme_error_4.txt", "error"), ([], "timeout"),
                                             (["AT$LCSLPP?", "OK"], "unexpected")])
def test_sonda_lpp_registra_erro_e_indisponibilidade(sem_espera, resposta, status):
    r = lpp.probe(driver_com({"AT$LCSLPP?": resposta}, sem_espera))
    assert r["status"] == status and r["error"]


def test_sonda_lpp_com_porta_caida(sem_espera):
    def porta_caiu():
        raise OSError("porta desconectada")

    r = lpp.probe(driver_com({"AT$LCSLPP?": porta_caiu}, sem_espera))
    assert r["status"] == "unavailable" and "OSError" in r["error"] and r["raw_response"] is None


def test_cli_da_sonda_grava_json_novo(monkeypatch, sem_espera, tmp_path, capsys):
    driver = driver_com({"AT$LCSLPP?": "lcslpp_consulta.txt"}, sem_espera)
    driver.connect = lambda *args, **kwargs: None
    monkeypatch.setattr(lpp, "FN990Driver", lambda: driver)
    saida = tmp_path / "sonda_lpp.json"
    lpp.main(["--port", "P", "--out", str(saida)])
    assert json.loads(saida.read_text(encoding="utf-8"))["status"] == "ok" and not driver._ser.is_open
    with pytest.raises(SystemExit):
        lpp.main(["--port", "P", "--out", str(saida)])


def test_cli_da_sonda_com_fn990_indisponivel(monkeypatch, capsys):
    class SemModem:
        def connect(self, *args, **kwargs):
            raise ConnectionError("P: não foi possível abrir a porta")

    monkeypatch.setattr(lpp, "FN990Driver", SemModem)
    lpp.main(["--port", "P"])
    r = json.loads(capsys.readouterr().out)
    assert r["status"] == "unavailable" and "não foi possível abrir" in r["error"]


# --------------------------------------------------------------------------- isolamento
def test_caminho_principal_nao_importa_nem_envia_comandos_experimentais():
    importa = re.compile(r"^\s*(?:from|import)\s+experimental\b", re.M)
    envia_lpp = re.compile(r"[\"']AT\$LCSLPP")
    ofensores = [caminho for caminho, texto in arquivos_principais() if importa.search(texto) or envia_lpp.search(texto)]
    assert ofensores == []
    codigo = ("import sys, hal.cli, acquisition.snapshot, coletor.daemon, coletor.export_csv, coletor.migrate, "
              "estimador.train, estimador.evaluate, estimador.live; "
              "print(sorted(m for m in sys.modules if m.startswith('experimental')))")
    saida = subprocess.run([sys.executable, "-c", codigo], capture_output=True, text=True, cwd=RAIZ, check=True)
    assert saida.stdout.strip() == "[]"


def test_experimental_tem_so_os_tres_componentes():
    modulos = sorted(n for n in os.listdir(os.path.join(RAIZ, "experimental")) if n.endswith(".py"))
    assert modulos == ["angular_geometry.py", "lpp_probe.py", "timing_advance.py"]
