"""Cell-ID por global_cell_key com cadastro validado (Fase 5, commit 4)."""
import json
import os
import sys

import pytest

from estimador.estimate import STATUS_UNAVAILABLE, STATUS_VALID
from estimador.methods.cell_id import (
    STATUS_INVALID_REGISTRY,
    STATUS_MISSING_GLOBAL_CELL_KEY,
    STATUS_UNKNOWN_CELL,
    estimate_cell_id,
    load_registry,
)

CHAVE = "LTE:72406:F560E67"
EXEMPLO = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "estimador",
                       "network_registry.example.json")


def gravar(tmp_path, dados) -> str:
    caminho = tmp_path / "registry.json"
    caminho.write_text(dados if isinstance(dados, str) else json.dumps(dados), encoding="utf-8")
    return str(caminho)


def cadastro(tmp_path, cells):
    return load_registry(gravar(tmp_path, {"registry_version": 2, "cells": cells}))


def celula(**campos):
    return {"global_cell_key": CHAVE, "lat": -26.9, "lon": -49.07, **campos}


def test_celula_cadastrada_devolve_a_posicao_da_antena(tmp_path):
    reg = cadastro(tmp_path, [celula(sector_azimuth_deg=120, sector_width_deg=65, arfcn=3375, pci=254,
                                     description="setor A")])
    est = estimate_cell_id(reg, CHAVE)
    assert reg.valid and est.valid
    assert (est.method, est.status, est.lat, est.lon, est.technology) == ("cell_id", STATUS_VALID, -26.9, -49.07, "LTE")
    assert "não do UE" in est.metadata["position"] and est.metadata["sector_azimuth_deg"] == 120.0


def test_celula_fora_do_cadastro(tmp_path):
    est = estimate_cell_id(cadastro(tmp_path, [celula()]), "LTE:72406:F560E68")
    assert (est.status, est.lat, est.technology, est.valid) == (STATUS_UNKNOWN_CELL, None, "LTE", False)


@pytest.mark.parametrize("chave", [None, ""])
def test_leitura_sem_chave_global(tmp_path, chave):
    est = estimate_cell_id(cadastro(tmp_path, [celula()]), chave)
    assert (est.status, est.lat) == (STATUS_MISSING_GLOBAL_CELL_KEY, None)


def test_leitura_indisponivel(tmp_path):
    est = estimate_cell_id(cadastro(tmp_path, [celula()]), CHAVE, reading_available=False)
    assert (est.status, est.lat) == (STATUS_UNAVAILABLE, None)


def test_pci_nao_identifica_a_celula(tmp_path):
    reg = cadastro(tmp_path, [celula(arfcn=3375, pci=254)])
    assert estimate_cell_id(reg, "LTE:72406:ABC").status == STATUS_UNKNOWN_CELL  # mesmo PCI/ARFCN, outra célula
    assert estimate_cell_id(reg, "LTE:3375:254").status == STATUS_UNKNOWN_CELL  # radio_key não é chave do cadastro


def test_pci_nr_aceita_ate_1007(tmp_path):
    assert cadastro(tmp_path, [celula(global_cell_key="NR:72406:3053A189", pci=1007)]).valid


def test_exemplo_do_repositorio_e_valido():
    reg = load_registry(EXEMPLO)
    assert reg.valid, reg.error
    assert set(reg.entries) == {"LTE:00101:1A2B3C", "NR:00101:3053A189"}


INVALIDOS = [
    ({"_leia_isto": "x", "254": {"lat": -26.9, "lon": -49.07}}, "formato antigo"),
    ({"registry_version": 1, "cells": []}, "registry_version"),
    ({"registry_version": 2, "cells": [], "extra": 1}, "campos de topo"),
    ({"registry_version": 2, "cells": {}}, "'cells'"),
    ([celula(global_cell_key="254")], "global_cell_key inválida"),
    ([celula(global_cell_key="LTE:72406:f560e67")], "global_cell_key inválida"),
    ([celula(global_cell_key="LTE:72406:0F560E67")], "global_cell_key inválida"),
    ([celula(global_cell_key="GSM:72406:F560E67")], "global_cell_key inválida"),
    ([celula(), celula()], "repetida"),
    ([{"global_cell_key": CHAVE, "lon": -49.07}], "'lat'"),
    ([celula(lat=-91)], "lat fora da faixa"),
    ([celula(lat="-26.9")], "lat inválida"),
    ([celula(lon=True)], "lon inválida"),
    ([celula(azimute_graus=120)], "campos desconhecidos"),
    ([celula(sector_azimuth_deg=360)], "sector_azimuth_deg"),
    ([celula(sector_width_deg=0)], "sector_width_deg"),
    ([celula(pci=600)], "pci"),
    ([celula(arfcn=-1)], "arfcn"),
    ([celula(arfcn=3375.0)], "arfcn"),
    ([celula(description=5)], "description"),
]


@pytest.mark.parametrize("dados,trecho", INVALIDOS)
def test_cadastro_invalido_vira_invalid_registry(tmp_path, dados, trecho):
    if isinstance(dados, list):
        dados = {"registry_version": 2, "cells": dados}
    reg = load_registry(gravar(tmp_path, dados))
    assert not reg.valid and trecho in reg.error
    est = estimate_cell_id(reg, CHAVE)
    assert (est.status, est.lat, est.reason) == (STATUS_INVALID_REGISTRY, None, reg.error)


def test_arquivo_ausente_ou_json_quebrado(tmp_path):
    assert "não encontrado" in load_registry(str(tmp_path / "nao_existe.json")).error
    assert "JSON inválido" in load_registry(gravar(tmp_path, "{quebrado")).error


def test_live_recusa_cadastro_invalido_antes_de_abrir_o_modem(tmp_path, monkeypatch, capsys):
    import estimador.live as live

    def nao_conectar():
        raise AssertionError("o modem não deveria ser aberto")

    monkeypatch.setattr(live, "FN990Driver", nao_conectar)
    caminho = gravar(tmp_path, {"254": {"lat": -26.9, "lon": -49.07}})
    monkeypatch.setattr(sys, "argv", ["live", "once", "--metodos", "cell_id", "--fn990-port", "X", "--registry", caminho])
    with pytest.raises(SystemExit):
        live.main()
    assert "invalid_registry" in capsys.readouterr().err
