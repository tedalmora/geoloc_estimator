"""As 59 verificações da auditoria (Etapa 1) como testes automatizados.

Cada teste mantém o identificador do relatório da auditoria (B00, B01, H01...). Defeitos que já
existiam antes da reconfiguração ficam marcados com xfail estrito e a fase que deve corrigi-los;
quando a fase corrige o defeito, o marcador sai e o teste passa a ser obrigatório.
Os testes X01-X02 cobrem dois defeitos achados na auditoria fora das 59 verificações.
"""
import ast
import importlib
import math
import pathlib
import sqlite3
import sys
import time

import pytest

from support import BlockingSerial, ScriptedSerial, SilentSerial, at_lines

ROOT = pathlib.Path(__file__).resolve().parents[1]


def pendente(fase):
    return pytest.mark.xfail(reason=f"defeito pré-existente da auditoria; corrigir na fase {fase}", strict=True)


def driver_com(respostas):
    import hal.fn990_driver as fd

    driver = fd.FN990Driver()
    driver._ser = ScriptedSerial({cmd: at_lines(arq) for cmd, arq in respostas.items()})
    return driver


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
@pendente(2)
def test_H01_send_at_respeita_o_prazo_pedido():
    import hal.fn990_driver as fd

    driver = fd.FN990Driver()
    driver._ser = BlockingSerial(timeout=5.0)
    inicio = time.monotonic()
    driver.send_at("AT", timeout=2.0)
    assert time.monotonic() - inicio < 2.5


@pendente(2)
def test_H02_connect_falha_se_at_nunca_responde(monkeypatch, sem_espera):
    import hal.fn990_driver as fd

    monkeypatch.setattr(fd.serial, "Serial", SilentSerial)
    with pytest.raises(ConnectionError):
        fd.FN990Driver().connect("PORTA_FALSA")


# --------------------------------------------------------------------------- P: parsing
def test_P01_moni_lte(sem_espera):
    s = driver_com({"AT#MONI": "moni_lte.txt"}).get_signal_metrics()
    assert (s.technology, s.rsrp_dbm, s.rsrq_db) == ("4G", -107.0, -12.0)


@pendente(2)
def test_P02_moni_endc_vira_nr_nsa(sem_espera):
    s = driver_com({"AT#MONI": "moni_endc_nr_conectado.txt"}).get_signal_metrics()
    assert s.technology == "NR_NSA"


@pendente(2)
def test_P03_moni_nr_sa_identifica_servidora(sem_espera):
    c = driver_com({"AT#MONI": "moni_nr_sa.txt"}).get_serving_cell()
    assert c.pci == 123


@pendente(2)
def test_P04_cme_error_vira_unknown(sem_espera):
    s = driver_com({"AT#MONI": "cme_error_4.txt"}).get_signal_metrics()
    assert s.technology == "UNKNOWN"


@pendente(2)
def test_P05_resposta_vazia_vira_unknown(sem_espera):
    s = driver_com({"AT#MONI": "vazio.txt"}).get_signal_metrics()
    assert s.technology == "UNKNOWN"


def test_P06_pci_da_servidora_por_cruzamento(sem_espera):
    c = driver_com({"AT#MONI": "moni_lte.txt", "AT#CSURV": "csurv_vivo_campo.txt"}).get_serving_cell()
    assert (c.cell_id, c.pci) == ("F560E67", 254)


@pendente(2)
def test_P07_csurv_preserva_arfcn(sem_espera):
    obs = driver_com({"AT#CSURV": "csurv_manual_exemplo.txt"}).get_neighbor_cells()
    assert {o.arfcn for o in obs if o.pci == 64} == {2850, 1350, 3200}


@pendente(2)
def test_P08_csurv_aproveita_registro_nr(sem_espera):
    obs = driver_com({"AT#CSURV": "csurv_manual_exemplo.txt"}).get_neighbor_cells()
    assert 639 in [o.pci for o in obs]


def test_P09_gpsacp_com_fix(sem_espera):
    g = driver_com({"AT$GPSACP": "gpsacp_fix.txt"}).get_gnss_fix()
    assert math.isclose(g.lat, 37.52172767, abs_tol=1e-6) and math.isclose(g.lon, 126.92969693, abs_tol=1e-6)


@pendente(2)
def test_P10_gpsacp_preserva_hdop_e_satelites(sem_espera):
    g = driver_com({"AT$GPSACP": "gpsacp_fix.txt"}).get_gnss_fix()
    assert (g.hdop, g.nsat) == (0.8, 18)


def test_P11_gpsacp_sem_fix(sem_espera):
    assert driver_com({"AT$GPSACP": "gpsacp_sem_fix.txt"}).get_gnss_fix() is None


@pendente(2)
def test_P12_gpsacp_fix_invalido_rejeitado(sem_espera):
    assert driver_com({"AT$GPSACP": "gpsacp_fix_invalido.txt"}).get_gnss_fix() is None


def test_P13_gpsacp_motor_desligado(sem_espera):
    assert driver_com({"AT$GPSACP": "gpsacp_motor_desligado.txt"}).get_gnss_fix() is None


def test_P14_csurv_registros_colados(sem_espera):
    obs = driver_com({"AT#CSURV": "csurv_registros_colados.txt"}).get_neighbor_cells()
    assert [o.pci for o in obs] == [254]


def test_P15_csurv_uma_vez_por_ciclo(sem_espera):
    driver = driver_com({"AT#MONI": "moni_lte.txt", "AT#CSURV": "csurv_vivo_campo.txt"})
    driver.get_serving_cell()
    driver.get_neighbor_cells()
    assert driver._ser.written.count("AT#CSURV") == 1


@pendente(2)
def test_X01_gpsacp_malformado_nao_derruba_a_leitura(sem_espera):
    assert driver_com({"AT$GPSACP": "gpsacp_malformado.txt"}).get_gnss_fix() is None


@pendente(2)
def test_X02_csurv_rsrp_zero_e_ausente(sem_espera):
    obs = driver_com({"AT#CSURV": "csurv_valor_nao_coletado.txt"}).get_neighbor_cells()
    assert [o.rsrp_dbm for o in obs if o.pci == 254] == [None]


# --------------------------------------------------------------------------- F: features (Estimador legado)
@pendente(5)
def test_F01_ta_ausente_nao_vira_zero():
    from estimador.features import build_feature_vector

    v = build_feature_vector({"serving_pci": 254, "rsrp_dbm": -107, "timing_advance": None}, [], [254])
    assert 0 not in v[1:]


@pendente(5)
def test_F02_mesmo_pci_em_canais_diferentes_gera_colunas_distintas():
    from estimador.features import build_feature_vector

    v = build_feature_vector({"serving_pci": 254, "rsrp_dbm": -107, "timing_advance": None},
                             [{"pci": 64, "rsrp_dbm": -99}, {"pci": 64, "rsrp_dbm": -97}], [64, 254])
    assert -99 in v and -97 in v


@pendente(5)
def test_F03_rsrp_ausente_nao_injeta_none():
    from estimador.features import build_feature_vector

    v = build_feature_vector({"serving_pci": 254, "rsrp_dbm": None, "timing_advance": None}, [], [254])
    assert None not in v


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


# --------------------------------------------------------------------------- C: Coletor (schema v1)
class _DriverV1:
    """Driver falso com a interface antiga (get_signal_metrics/get_serving_cell/...)."""

    def __init__(self, technology="4G", falha=None):
        self.technology = technology
        self.falha = falha

    def is_alive(self):
        return True

    def get_signal_metrics(self):
        from hal.models import SignalMetrics

        if self.falha:
            raise self.falha
        return SignalMetrics(self.technology, -107.0, -12.0, None, None)

    def get_serving_cell(self):
        from hal.models import ServingCell

        return ServingCell("F560E67", 254, "87BB")

    def get_neighbor_cells(self):
        from hal.models import NeighborCell

        return [NeighborCell(64, -99.0)]

    def get_gnss_fix(self):
        return None

    def get_lpp_report(self):
        raise NotImplementedError

    def close(self):
        pass


@pytest.fixture
def banco_coletor():
    import coletor.db as cdb

    conn = cdb.connect(":memory:")
    yield conn
    conn.close()


@pendente(4)
def test_C01_foreign_keys_ativas(banco_coletor):
    assert banco_coletor.execute("PRAGMA foreign_keys").fetchone()[0] == 1


@pendente(4)
def test_C02_tecnologia_gravada_e_a_medida(banco_coletor):
    import coletor.daemon as cd
    import coletor.db as cdb

    sid = cdb.create_session(banco_coletor, "pedestre", "outdoor")
    cdb.insert_measurement(banco_coletor, cd.poll_once(_DriverV1("4G"), "fn990", "5G", sid))
    assert banco_coletor.execute("SELECT technology FROM measurements").fetchone()[0] != "5G"


@pendente(4)
def test_C03_foreign_key_rejeita_celula_orfa(banco_coletor):
    with pytest.raises(sqlite3.IntegrityError):
        banco_coletor.execute("INSERT INTO neighbor_cells (measurement_id, pci, rsrp_dbm) VALUES (9999, 1, -100)")


@pendente(4)
def test_C04_health_checks_vinculados_a_sessao(banco_coletor):
    cols = [r[1] for r in banco_coletor.execute("PRAGMA table_info(health_checks)")]
    assert "session_id" in cols


@pendente(4)
def test_C05_sessao_com_status_e_metadados(banco_coletor):
    cols = [r[1] for r in banco_coletor.execute("PRAGMA table_info(sessions)")]
    assert "status" in cols and "metadata_json" in cols


@pendente(4)
def test_C06_medicao_com_ponto_de_referencia(banco_coletor):
    cols = [r[1] for r in banco_coletor.execute("PRAGMA table_info(measurements)")]
    assert "reference_point_id" in cols


@pendente(4)
def test_C07_horario_da_sessao_em_utc(banco_coletor):
    import coletor.db as cdb

    cdb.create_session(banco_coletor, "pedestre", "outdoor")
    inicio = banco_coletor.execute("SELECT started_at FROM sessions").fetchone()[0]
    assert inicio.endswith("Z")


@pendente(4)
def test_C08_sessao_encerrada_mesmo_com_excecao(monkeypatch):
    import coletor.daemon as cd
    import coletor.db as cdb

    compartilhado = sqlite3.connect(":memory:")
    compartilhado.executescript(cdb.SCHEMA)

    class SemFechar:
        def __init__(self, conn):
            self._conn = conn

        def __getattr__(self, nome):
            return getattr(self._conn, nome)

        def close(self):
            pass

    monkeypatch.setattr(cd.db, "connect", lambda caminho: SemFechar(compartilhado))
    monkeypatch.setattr(cd, "_connect_modems", lambda args: [("fn990", "5G", _DriverV1(falha=TimeoutError("timeout simulado")))])
    monkeypatch.setattr(sys, "argv", ["coletor.daemon", "--scenario", "pedestre", "--environment", "outdoor",
                                      "--fn990-port", "PORTA_FALSA", "--db", ":memory:"])
    with pytest.raises(TimeoutError):
        cd.main()
    assert compartilhado.execute("SELECT ended_at FROM sessions").fetchone()[0] is not None


# --------------------------------------------------------------------------- D: dataset (Estimador legado)
ESQUEMA_V1_MINIMO = """
CREATE TABLE measurements (measurement_id INTEGER PRIMARY KEY, technology TEXT, serving_pci INTEGER,
    rsrp_dbm REAL, timing_advance INTEGER, gnss_lat REAL, gnss_lon REAL);
CREATE TABLE neighbor_cells (id INTEGER PRIMARY KEY, measurement_id INTEGER, pci INTEGER, rsrp_dbm REAL);
"""


@pytest.fixture
def tabela_legado():
    from estimador.features import build_training_table

    conn = sqlite3.connect(":memory:")
    conn.executescript(ESQUEMA_V1_MINIMO)
    pontos = [(-26.900, -49.070), (-26.901, -49.071), (-26.902, -49.072)]
    mid = 0
    for k, (lat, lon) in enumerate(pontos):
        for j in range(5):
            mid += 1
            conn.execute("INSERT INTO measurements VALUES (?, '5G', 254, ?, NULL, ?, ?)", (mid, -100.0 - k, lat, lon))
            conn.execute("INSERT INTO neighbor_cells (measurement_id, pci, rsrp_dbm) VALUES (?, 64, ?)", (mid, -90.0 - j))
            if (k, j) == (2, 4):
                conn.execute("INSERT INTO neighbor_cells (measurement_id, pci, rsrp_dbm) VALUES (?, 999, -120)", (mid,))
    X, y, vocab = build_training_table(conn, "5G")
    conn.close()
    return X, y, vocab


@pendente(5)
def test_D01_mesmo_ponto_nao_aparece_em_treino_e_teste(tabela_legado):
    from sklearn.model_selection import train_test_split

    X, y, _ = tabela_legado
    _, _, y_tr, y_te = train_test_split(X, y, test_size=0.2, random_state=42)
    assert not set(map(tuple, y_tr.values.round(6))) & set(map(tuple, y_te.values.round(6)))


@pendente(5)
def test_D02_vocabulario_construido_so_com_treino(tabela_legado):
    _, _, vocab = tabela_legado
    assert 999 not in vocab


@pendente(5)
def test_D03_ta_fora_da_feature_padrao(tabela_legado):
    X, _, _ = tabela_legado
    assert "timing_advance" not in X.columns
