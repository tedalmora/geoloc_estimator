"""As 59 verificações da auditoria (Etapa 1) como testes automatizados.

Cada teste mantém o identificador do relatório da auditoria (B00, B01, H01...). Defeitos que já
existiam antes da reconfiguração ficam marcados com xfail estrito e a fase que deve corrigi-los;
quando a fase corrige o defeito, o marcador sai e o teste passa a ser obrigatório.
Os testes X01-X02 cobrem dois defeitos achados na auditoria fora das 59 verificações.
"""
import ast
import importlib
import json
import math
import pathlib
import sqlite3
import time

import pytest

from support import BlockingSerial, SilentSerial, at_lines, driver_com

from radiomapa_sintetico import LOCAL_KEY, criar_radiomapa

ROOT = pathlib.Path(__file__).resolve().parents[1]


def pendente(fase):
    return pytest.mark.xfail(reason=f"defeito pré-existente da auditoria; corrigir na fase {fase}", strict=True)


def resposta(comando, fixture):
    from hal.models import AtResponse

    return AtResponse.from_raw(comando, at_lines(fixture))


# --------------------------------------------------------------------------- B: base
def test_B00_sintaxe_de_todos_os_modulos():
    erros = []
    for path in ROOT.rglob("*.py"):
        if {"venv", ".venv", ".git"} & set(path.parts):
            continue
        try:
            ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            erros.append(f"{path}: {exc}")
    assert not erros


MODULOS_AUDITORIA = [
    "hal.models", "hal.parsing", "hal.modem_driver", "hal.fn990_driver", "hal.health", "hal.cli",
    "coletor.db", "coletor.daemon", "coletor.export_csv", "estimador.features", "estimador.evaluate",
    "estimador.db", "estimador.methods.network_position", "estimador.methods.aoa",
    "estimador.methods.fingerprint", "estimador.train", "estimador.compare", "estimador.infer",
    "estimador.live", "estimador.relatorio",
]


@pytest.mark.parametrize("modulo", MODULOS_AUDITORIA)
def test_B01_import(modulo):
    importlib.import_module(modulo)


# --------------------------------------------------------------------------- H: transporte AT
def test_H01_send_at_respeita_o_prazo_pedido():
    import hal.fn990_driver as fd

    driver = fd.FN990Driver()
    driver._ser = BlockingSerial(timeout=5.0)
    inicio = time.monotonic()
    resp = driver.send_at("AT", timeout=2.0)
    assert time.monotonic() - inicio < 2.5 and resp.timed_out


def test_H02_connect_falha_se_at_nunca_responde(monkeypatch, sem_espera):
    import hal.fn990_driver as fd

    monkeypatch.setattr(fd.serial, "Serial", lambda *a, **k: SilentSerial(clock=sem_espera, **k))
    with pytest.raises(ConnectionError):
        fd.FN990Driver().connect("PORTA_FALSA")


# --------------------------------------------------------------------------- P: parsing
def test_P01_moni_lte(sem_espera):
    m = driver_com({"AT#MONI": "moni_lte.txt"}, sem_espera).read_serving_cell()
    assert (m.status, m.access_mode, m.lte.rsrp_dbm, m.lte.rsrq_db) == ("ok", "LTE", -107.0, -12.0)


def test_P02_moni_endc_vira_nr_nsa(sem_espera):
    m = driver_com({"AT#MONI": "moni_endc_nr_conectado.txt"}, sem_espera).read_serving_cell()
    assert m.access_mode == "NR_NSA"


def test_P03_moni_nr_sa_identifica_servidora(sem_espera):
    m = driver_com({"AT#MONI": "moni_nr_sa.txt"}, sem_espera).read_serving_cell()
    assert (m.access_mode, m.nr.pci, m.nr.radio_key) == ("NR_SA", 123, "NR:629952:123")


def test_P04_cme_error_vira_unknown(sem_espera):
    m = driver_com({"AT#MONI": "cme_error_4.txt"}, sem_espera).read_serving_cell()
    assert (m.status, m.access_mode, m.response.error_code) == ("error", "UNKNOWN", 4)


def test_P05_resposta_vazia_vira_unknown(sem_espera):
    m = driver_com({"AT#MONI": "vazio.txt"}, sem_espera).read_serving_cell()
    assert (m.status, m.access_mode) == ("timeout", "UNKNOWN")


def test_P06_pci_da_servidora_por_cruzamento():
    from hal.parsing import parse_csurv, parse_moni, parse_servinfo, resolve_serving_cell

    r = resolve_serving_cell(
        parse_moni(resposta("AT#MONI", "moni_lte.txt")),
        parse_servinfo(resposta("AT#SERVINFO", "servinfo_lte.txt")),
        parse_csurv(resposta("AT#CSURV", "csurv_vivo_campo.txt")).observations,
    )
    assert (r.cell_id, r.pci, r.radio_key, r.global_cell_key) == ("F560E67", 254, "LTE:3375:254", "LTE:72406:F560E67")


def test_P07_csurv_preserva_arfcn():
    from hal.parsing import parse_csurv

    obs = parse_csurv(resposta("AT#CSURV", "csurv_manual_exemplo.txt")).observations
    assert {o.arfcn for o in obs if o.pci == 64} == {2850, 1350, 3200}


def test_P08_csurv_aproveita_registro_nr():
    from hal.parsing import parse_csurv

    obs = parse_csurv(resposta("AT#CSURV", "csurv_manual_exemplo.txt")).observations
    assert "NR:636672:639" in [o.radio_key for o in obs]


def test_P09_gpsacp_com_fix(sem_espera):
    g = driver_com({"AT$GPSACP": "gpsacp_fix.txt"}, sem_espera).read_gnss().fix
    assert math.isclose(g.lat, 37.52172767, abs_tol=1e-6) and math.isclose(g.lon, 126.92969693, abs_tol=1e-6)


def test_P10_gpsacp_preserva_hdop_e_satelites(sem_espera):
    g = driver_com({"AT$GPSACP": "gpsacp_fix.txt"}, sem_espera).read_gnss().fix
    assert (g.hdop, g.nsat) == (0.8, 18)


def test_P11_gpsacp_sem_fix(sem_espera):
    g = driver_com({"AT$GPSACP": "gpsacp_sem_fix.txt"}, sem_espera).read_gnss()
    assert (g.status, g.fix) == ("no_fix", None)


def test_P12_gpsacp_fix_invalido_rejeitado(sem_espera):
    g = driver_com({"AT$GPSACP": "gpsacp_fix_invalido.txt"}, sem_espera).read_gnss()
    assert (g.status, g.fix) == ("no_fix", None)


def test_P13_gpsacp_motor_desligado(sem_espera):
    g = driver_com({"AT$GPSACP": "gpsacp_motor_desligado.txt"}, sem_espera).read_gnss()
    assert (g.status, g.fix) == ("off", None)


def test_P14_csurv_registros_colados():
    from hal.parsing import parse_csurv

    obs = parse_csurv(resposta("AT#CSURV", "csurv_registros_colados.txt")).observations
    assert [o.radio_key for o in obs] == [None, "LTE:3375:254"]


def test_P15_csurv_uma_vez_por_ciclo(sem_espera):
    from acquisition.snapshot import take_snapshot

    driver = driver_com(CICLO_LTE, sem_espera)
    take_snapshot(driver)
    assert driver._ser.written.count("AT#CSURV") == 1


def test_X01_gpsacp_malformado_nao_derruba_a_leitura(sem_espera):
    g = driver_com({"AT$GPSACP": "gpsacp_malformado.txt"}, sem_espera).read_gnss()
    assert (g.status, g.fix) == ("malformed", None)


def test_X02_csurv_rsrp_zero_e_ausente():
    from hal.parsing import parse_csurv

    obs = parse_csurv(resposta("AT#CSURV", "csurv_valor_nao_coletado.txt")).observations
    assert [o.rsrp_dbm for o in obs if o.pci == 254] == [None]


# --------------------------------------------------------------------------- F: features do fingerprint
def _politica_lte(plmn="72406"):
    from estimador.features import CellFilterPolicy

    return CellFilterPolicy("LTE", plmn)


def test_F01_ta_ausente_nao_vira_zero():
    from estimador.dataset import CsurvCell
    from estimador.features import vectorize

    celulas = [CsurvCell("LTE:3375:254", "LTE", "72406", "CELL_SUITABLE", -107.0)]
    vetor, _ = vectorize(celulas, _politica_lte(), ["LTE:3375:254", "LTE:3375:31"], -140.0)
    assert vetor == [-107.0, -140.0]  # só RSRP por radio_key: não há coluna de TA nem zero


def test_F02_mesmo_pci_em_canais_diferentes_gera_colunas_distintas():
    from types import SimpleNamespace

    from estimador.features import build_vocabulary, cells_from_observations, vectorize
    from hal.parsing import parse_csurv

    celulas = cells_from_observations(parse_csurv(resposta("AT#CSURV", "csurv_manual_exemplo.txt")).observations)
    politica = _politica_lte("45005")
    chaves, _ = build_vocabulary([SimpleNamespace(cells=celulas)], politica)
    vetor, _ = vectorize(celulas, politica, chaves, -150.0)
    assert {k: v for k, v in zip(chaves, vetor) if k.endswith(":64")} == {
        "LTE:1350:64": -97.0, "LTE:2850:64": -99.0, "LTE:3200:64": -102.0}


def test_F03_rsrp_ausente_nao_injeta_none():
    from estimador.dataset import CsurvCell
    from estimador.features import vectorize

    celulas = [CsurvCell("LTE:3375:254", "LTE", "72406", "CELL_SUITABLE", None)]
    assert vectorize(celulas, _politica_lte(), ["LTE:3375:254"], -140.0) == ([-140.0], 0)


# --------------------------------------------------------------------------- T: Timing Advance (legado)
@pendente(7)
def test_T01_ta_ausente_produz_unavailable():
    from estimador.methods.network_position import CellPosition, estimate_cell_position_ta

    r = estimate_cell_position_ta(254, None, "5G", {254: CellPosition(-26.9, -49.07, 120.0)})
    assert r is None or r.get("status") == "unavailable"


@pendente(7)
def test_T02_incerteza_nunca_zero():
    from estimador.methods.network_position import CellPosition, estimate_cell_position_ta

    r = estimate_cell_position_ta(254, 3, "4G", {254: CellPosition(-26.9, -49.07, 120.0)})
    assert r is None or r.get("raio_incerteza_m") != 0.0


@pendente(7)
def test_T03_sem_constante_fixa_de_ta_para_nr():
    from estimador.methods import network_position

    assert "5G" not in getattr(network_position, "METROS_POR_TA", {})


# --------------------------------------------------------------------------- G: geometria angular (legado)
@pendente(7)
def test_G01_intersecao_atras_da_ancora_rejeitada():
    from estimador.methods.aoa import estimate_position_from_bearings

    with pytest.raises(ValueError):
        estimate_position_from_bearings((0.0, 0.0), 225.0, (0.0, 0.001), 315.0)


@pendente(7)
def test_G02_ancoras_iguais_rejeitadas():
    from estimador.methods.aoa import estimate_position_from_bearings

    with pytest.raises(ValueError):
        estimate_position_from_bearings((0.0, 0.0), 0.0, (0.0, 0.0), 90.0)


@pendente(7)
def test_G03_bearing_nan_rejeitado():
    from estimador.methods.aoa import estimate_position_from_bearings

    with pytest.raises(ValueError):
        estimate_position_from_bearings((0.0, 0.0), float("nan"), (0.0, 0.001), 90.0)


def test_G04_cruzamento_ortogonal_conhecido():
    from estimador.methods.aoa import estimate_position_from_bearings

    lat, lon = estimate_position_from_bearings((0.0, 0.0), 90.0, (0.0009, 0.0009), 180.0)
    assert abs(lat) < 1e-6 and abs(lon - 0.0009) < 1e-6


# --------------------------------------------------------------------------- C: Coletor (schema v2)
CICLO_LTE = {
    "AT": ["AT", "OK"],
    "AT$GPSP?": "gpsp_ligado.txt",
    "AT#MONI": "moni_lte.txt",
    "AT#SERVINFO": "servinfo_lte.txt",
    "AT#CSURV": "csurv_vivo_campo.txt",
    "AT$GPSACP": "gpsacp_sem_fix.txt",
}


def coletar_once(monkeypatch, banco, roteiro, relogio):
    import coletor.daemon as cd

    driver = driver_com(roteiro, relogio)
    monkeypatch.setattr(cd, "_open_modem", lambda args: driver)
    cd.main(["once", "--port", "PORTA", "--db", str(banco), "--scenario", "estatico", "--environment", "outdoor",
             "--reference-point", "P01", "--campaign-plmn", "72406"])


def consulta(banco, sql):
    conn = sqlite3.connect(banco)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


@pytest.fixture
def banco_coletor():
    import coletor.db as cdb

    conn = cdb.connect(":memory:")
    yield conn
    conn.close()


def test_C01_foreign_keys_ativas(banco_coletor):
    assert banco_coletor.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_C02_tecnologia_gravada_e_a_medida(monkeypatch, tmp_path, sem_espera):
    banco = tmp_path / "c02.db"
    coletar_once(monkeypatch, banco, CICLO_LTE, sem_espera)
    coletar_once(monkeypatch, banco, dict(CICLO_LTE, **{"AT#MONI": "moni_endc_nr_conectado.txt"}), sem_espera)
    assert consulta(banco, "SELECT access_mode FROM measurements ORDER BY measurement_id") == [("LTE",), ("NR_NSA",)]


def test_C03_foreign_key_rejeita_celula_orfa(banco_coletor):
    with pytest.raises(sqlite3.IntegrityError):
        banco_coletor.execute(
            "INSERT INTO observed_cells (measurement_id, source, identity_status) VALUES (9999, 'CSURV', 'none')"
        )


def test_C04_health_checks_vinculados_a_sessao(monkeypatch, tmp_path, sem_espera):
    banco = tmp_path / "c04.db"
    coletar_once(monkeypatch, banco, CICLO_LTE, sem_espera)
    assert consulta(banco, "SELECT session_id, event FROM health_checks ORDER BY health_check_id") == [(1, "start"), (1, "end")]


def test_C05_sessao_com_status_e_metadados(monkeypatch, tmp_path, sem_espera):
    banco = tmp_path / "c05.db"
    coletar_once(monkeypatch, banco, CICLO_LTE, sem_espera)
    ((status, metadata),) = consulta(banco, "SELECT status, metadata_json FROM sessions")
    assert status == "completed" and json.loads(metadata)["schema_version"] == 2


def test_C06_medicao_com_ponto_de_referencia(monkeypatch, tmp_path, sem_espera):
    banco = tmp_path / "c06.db"
    coletar_once(monkeypatch, banco, CICLO_LTE, sem_espera)
    assert consulta(banco, "SELECT m.reference_point_id = p.reference_point_id FROM measurements m, reference_points p "
                           "WHERE p.name = 'P01'") == [(1,)]


def test_C07_horario_da_sessao_em_utc(monkeypatch, tmp_path, sem_espera):
    banco = tmp_path / "c07.db"
    coletar_once(monkeypatch, banco, CICLO_LTE, sem_espera)
    ((inicio, fim),) = consulta(banco, "SELECT started_at, ended_at FROM sessions")
    assert inicio.endswith("Z") and fim.endswith("Z")


def test_C08_sessao_encerrada_mesmo_com_excecao(monkeypatch, tmp_path, sem_espera):
    import coletor.daemon as cd

    def falha(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(cd.db, "insert_snapshot", falha)
    banco = tmp_path / "c08.db"
    with pytest.raises(sqlite3.OperationalError):
        coletar_once(monkeypatch, banco, CICLO_LTE, sem_espera)
    ((status, fim, motivo),) = consulta(banco, "SELECT status, ended_at, end_reason FROM sessions")
    assert status == "error" and fim is not None and "OperationalError" in motivo


# --------------------------------------------------------------------------- D: dataset e vocabulário (schema v2)
def test_D01_mesmo_ponto_nao_aparece_em_treino_e_teste(tmp_path):
    from estimador.dataset import load_dataset, make_group_split, samples_in_groups

    banco = tmp_path / "d01.db"
    criar_radiomapa(banco)
    amostras = load_dataset(str(banco)).samples
    split = make_group_split(amostras, "spatial")
    teste = {s.reference_point_id for s in samples_in_groups(amostras, "spatial", split.test_groups)}
    selecao = {s.reference_point_id for s in samples_in_groups(amostras, "spatial", split.selection_groups)}
    assert teste and selecao and not teste & selecao


def test_D02_vocabulario_construido_so_com_treino(tmp_path):
    from estimador.dataset import load_dataset
    from estimador.features import build_vocabulary

    banco = tmp_path / "d02.db"
    criar_radiomapa(banco)
    amostras = load_dataset(str(banco)).samples
    treino = [s for s in amostras if s.reference_point_id != 6]  # P6 é o único ponto que vê a célula local
    assert LOCAL_KEY in build_vocabulary(amostras, _politica_lte())[0]
    assert LOCAL_KEY not in build_vocabulary(treino, _politica_lte())[0]


def test_D03_ta_fora_da_feature_padrao(tmp_path):
    import re

    from estimador.train import train

    banco = tmp_path / "d03.db"
    criar_radiomapa(banco)
    bundle, _ = train(str(banco), "LTE", "spatial", str(tmp_path / "wknn.json"))
    assert all(re.fullmatch(r"LTE:\d+:\d+", k) for k in bundle["cell_keys"])
    assert "timing" not in json.dumps(bundle).lower()
