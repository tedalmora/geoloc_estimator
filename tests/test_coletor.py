"""Coletor v2: banco, sessões, export/restauração e migração não destrutiva de bancos v1."""
import hashlib
import json
import os
import signal
import sqlite3

import pytest

from support import at_lines, driver_com

import coletor.daemon as cd
import coletor.db as cdb
from acquisition.snapshot import take_snapshot
from coletor import export_csv, migrate
from hal.models import AtResponse
from hal.parsing import parse_moni

CICLO = {
    "AT": ["AT", "OK"],
    "AT$GPSP?": "gpsp_ligado.txt",
    "AT#MONI": "moni_lte.txt",
    "AT#SERVINFO": "servinfo_lte.txt",
    "AT#CSURV": "csurv_vivo_campo.txt",
    "AT$GPSACP": "gpsacp_fix.txt",
}
SESSAO_PONTO = ["--scenario", "estatico", "--environment", "outdoor", "--reference-point", "P01"]
SESSAO_ROTA = ["--scenario", "pedestre", "--environment", "outdoor", "--route", "trilha_1"]

# Schema v1 (código anterior à reconfiguração), só para gerar bancos antigos de teste.
ESQUEMA_V1 = """
CREATE TABLE sessions (session_id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL, ended_at TEXT,
    scenario TEXT NOT NULL, environment TEXT NOT NULL, notes TEXT);
CREATE TABLE measurements (measurement_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(session_id), timestamp REAL NOT NULL, modem_id TEXT NOT NULL,
    technology TEXT NOT NULL, serving_cell_id TEXT, serving_pci INTEGER, serving_tac TEXT, rsrp_dbm REAL,
    rsrq_db REAL, sinr_db REAL, timing_advance INTEGER, gnss_lat REAL, gnss_lon REAL, gnss_alt_m REAL,
    gnss_fix_quality TEXT, lpp_raw_log TEXT);
CREATE TABLE neighbor_cells (id INTEGER PRIMARY KEY AUTOINCREMENT,
    measurement_id INTEGER NOT NULL REFERENCES measurements(measurement_id), pci INTEGER NOT NULL,
    rsrp_dbm REAL NOT NULL);
CREATE TABLE health_checks (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp REAL NOT NULL, modem_id TEXT NOT NULL,
    alive INTEGER NOT NULL, note TEXT);
"""


def sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def linhas(banco, sql, *params):
    conn = sqlite3.connect(banco)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def rodar(monkeypatch, roteiro, relogio, *argv):
    driver = driver_com(roteiro, relogio)
    monkeypatch.setattr(cd, "_open_modem", lambda args: driver)
    cd.main(list(argv))
    return driver


def banco_v1(caminho):
    conn = sqlite3.connect(caminho)
    conn.executescript(ESQUEMA_V1)
    conn.executemany("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?)", [
        (1, "2026-09-01T10:00:00", "2026-09-01T11:00:00", "pedestre", "outdoor", ""),
        (2, "2026-09-02T10:00:00", None, "estatico", "indoor", "nota"),
    ])
    conn.executemany("INSERT INTO measurements VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", [
        (1, 1, 1788000000.0, "fn990", "5G", "F560E67", 254, "87BB", -107, -12, None, None, -26.9, -49.07, 10.0, "3", None),
        (2, 1, 1788000060.0, "fn990", "5G", "F560E67", 254, "87BB", -108, -12, None, None, -26.91, -49.08, 9.0, "1", None),
        (3, 2, 1788100000.0, "fn990", "5G", None, None, None, None, None, None, None, None, None, None, None, None),
        (4, 9, 1788200000.0, "fn990", "4G", None, None, None, -99, -10, None, None, None, None, None, None, None),
    ])
    conn.executemany("INSERT INTO neighbor_cells VALUES (?, ?, ?, ?)", [
        (1, 1, 254, -107.0), (2, 1, 64, 0.0), (3, 2, 254, -108.0), (4, 999, 1, -100.0),
    ])
    conn.executemany("INSERT INTO health_checks VALUES (?, ?, ?, ?, ?)", [
        (1, 1788000000.0, "fn990", 1, ""), (2, 1788000060.0, "fn990", 0, "sem resposta"),
    ])
    conn.commit()
    conn.close()
    return str(caminho)


# --------------------------------------------------------------------------- banco
def test_banco_novo_e_v2_com_foreign_keys(tmp_path):
    conn = cdb.connect(str(tmp_path / "novo.db"))
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert cdb.schema_info(conn)[1] == set(cdb.TABLES)
    conn.close()


def test_banco_v1_e_recusado_sem_alteracao(tmp_path):
    v1 = banco_v1(tmp_path / "v1.db")
    antes = sha(v1)
    with pytest.raises(cdb.SchemaError, match="coletor.migrate"):
        cdb.connect(v1)
    assert sha(v1) == antes and not os.path.exists(v1 + "-wal")


def test_snapshot_gravado_com_celulas_e_identidades(sem_espera):
    conn = cdb.connect(":memory:")
    sid = cdb.create_session(conn, mode="once", scenario="estatico", environment="outdoor", modem_id="fn990",
                             metadata={}, route="r1")
    cdb.insert_snapshot(conn, sid, 1, take_snapshot(driver_com(CICLO, sem_espera)))
    assert conn.execute(
        "SELECT snapshot_status, access_mode, serving_radio_key, serving_global_cell_key, serving_radio_key_source, "
        "gnss_fix_type, gnss_hdop, gnss_nsat, gnss_utc FROM measurements"
    ).fetchone() == ("complete", "LTE", "LTE:3375:254", "LTE:72406:F560E67", "CSURV", 3, 0.8, 18, "2019-08-29T00:25:18.000Z")
    assert conn.execute(
        "SELECT source, radio_key, global_cell_key, identity_status, is_serving FROM observed_cells ORDER BY observed_cell_id"
    ).fetchall() == [
        ("MONI", None, None, "none", 1),
        ("SERVINFO", None, "LTE:72406:F560E67", "global", 1),
        ("CSURV", None, None, "none", 0),
        ("CSURV", "LTE:3375:254", "LTE:72406:F560E67", "complete", 1),
    ]
    raw = json.loads(conn.execute("SELECT raw_json FROM measurements").fetchone()[0])
    assert parse_moni(AtResponse(**raw["MONI"])).lte.cell_id == "F560E67"  # reprocessável
    conn.close()


def test_ponto_pendente_pode_ser_completado_mas_nao_substituido():
    conn = cdb.connect(":memory:")
    pid, criado, tem_coordenada = cdb.get_or_create_reference_point(conn, "P02")
    assert criado and not tem_coordenada
    assert cdb.add_reference_point(conn, "P02", -26.9, -49.0, coord_source="rtk") == pid
    assert cdb.get_or_create_reference_point(conn, "P02") == (pid, False, True)
    with pytest.raises(ValueError):
        cdb.add_reference_point(conn, "P02", 0.0, 0.0)
    conn.close()


# --------------------------------------------------------------------------- sessões
def test_once_grava_sessao_completa(monkeypatch, tmp_path, capsys, sem_espera):
    banco = str(tmp_path / "c.db")
    rodar(monkeypatch, CICLO, sem_espera, "once", "--port", "P", "--db", banco, *SESSAO_PONTO)
    saida = capsys.readouterr().out
    assert "o ponto 'P01' não tem coordenada cadastrada (criado agora)" in saida
    assert "--campaign-plmn não informado" in saida
    ((status, modo, ponto, rota, plmn, metadata),) = linhas(
        banco, "SELECT status, mode, reference_point_id, route, campaign_plmn, metadata_json FROM sessions")
    assert (status, modo, ponto, rota, plmn) == ("completed", "once", 1, None, None)
    assert json.loads(metadata)["prepare"]["gnss_power_before"] == 1 and "imei" not in metadata.lower()


def test_continuous_ate_o_limite_com_grade_e_saude(monkeypatch, tmp_path, sem_espera):
    banco = str(tmp_path / "c.db")
    rodar(monkeypatch, CICLO, sem_espera, "continuous", "--port", "P", "--db", banco, *SESSAO_ROTA,
          "--campaign-plmn", "72406", "--interval", "0", "--health-interval", "0", "--max-measurements", "3")
    assert linhas(banco, "SELECT seq FROM measurements ORDER BY seq") == [(1,), (2,), (3,)]
    assert linhas(banco, "SELECT status, campaign_plmn, route FROM sessions") == [("completed", "72406", "trilha_1")]
    assert [e for (e,) in linhas(banco, "SELECT event FROM health_checks ORDER BY health_check_id")] == [
        "start", "periodic", "periodic", "periodic", "end"]


def test_tentativa_invalida_tambem_vira_linha(monkeypatch, tmp_path, sem_espera):
    banco = str(tmp_path / "c.db")
    mudo = dict(CICLO, **{"AT#MONI": [], "AT#SERVINFO": []})
    rodar(monkeypatch, mudo, sem_espera, "once", "--port", "P", "--db", banco, *SESSAO_PONTO)
    ((status, motivos),) = linhas(banco, "SELECT snapshot_status, reasons_json FROM measurements")
    assert status == "invalid" and "modem_sem_resposta" in json.loads(motivos)
    assert [e for (e,) in linhas(banco, "SELECT event FROM health_checks ORDER BY health_check_id")] == ["start", "failure", "end"]


def test_reconexao_apos_tres_falhas_seguidas(monkeypatch, tmp_path, sem_espera):
    banco = str(tmp_path / "c.db")
    chamadas = {"moni": 0}

    def moni():
        chamadas["moni"] += 1
        if chamadas["moni"] <= 3:
            raise OSError("porta caiu")
        return at_lines("moni_lte.txt")

    driver = driver_com(dict(CICLO, **{"AT#MONI": moni}), sem_espera)
    reconexoes = []
    driver.reconnect = lambda: reconexoes.append(1)
    monkeypatch.setattr(cd, "_open_modem", lambda args: driver)
    cd.main(["continuous", "--port", "P", "--db", banco, *SESSAO_ROTA, "--interval", "0",
             "--health-interval", "1000", "--max-measurements", "5"])
    assert reconexoes == [1]
    assert [s for (s,) in linhas(banco, "SELECT snapshot_status FROM measurements ORDER BY seq")] == [
        "invalid", "invalid", "invalid", "complete", "complete"]
    assert [e for (e,) in linhas(banco, "SELECT event FROM health_checks ORDER BY health_check_id")] == [
        "start", "failure", "reconnect_ok", "recovered", "end"]


def test_sinal_interrompe_depois_do_ciclo_e_restaura_handlers(monkeypatch, tmp_path, sem_espera):
    banco = str(tmp_path / "c.db")
    capturado = {}

    class Parada(cd._StopFlag):
        def __init__(self):
            super().__init__()
            capturado["flag"] = self

    def gnss():
        capturado["flag"](signal.SIGTERM, None)
        return at_lines("gpsacp_fix.txt")

    monkeypatch.setattr(cd, "_StopFlag", Parada)
    antes = signal.getsignal(signal.SIGINT)
    rodar(monkeypatch, dict(CICLO, **{"AT$GPSACP": gnss}), sem_espera, "continuous", "--port", "P", "--db", banco,
          *SESSAO_ROTA, "--interval", "0")
    assert linhas(banco, "SELECT status, end_reason FROM sessions") == [("interrupted", "sinal SIGTERM")]
    assert linhas(banco, "SELECT COUNT(*) FROM measurements") == [(1,)]
    assert signal.getsignal(signal.SIGINT) is antes


def test_ctrl_c_no_once_encerra_sessao_como_interrompida(monkeypatch, tmp_path, sem_espera):
    banco = str(tmp_path / "c.db")

    def ctrl_c():
        raise KeyboardInterrupt

    rodar(monkeypatch, dict(CICLO, **{"AT#MONI": ctrl_c}), sem_espera, "once", "--port", "P", "--db", banco, *SESSAO_PONTO)
    assert linhas(banco, "SELECT status, end_reason FROM sessions") == [("interrupted", "KeyboardInterrupt")]


def test_grade_monotonica_pula_ciclos_atrasados():
    assert cd.next_due(100.0, 60.0, 100.0) == 160.0
    assert cd.next_due(100.0, 60.0, 159.9) == 160.0
    assert cd.next_due(100.0, 60.0, 230.0) == 280.0
    assert cd.next_due(100.0, 0.0, 123.0) == 123.0


def test_ponto_ou_rota_e_obrigatorio_e_exclusivo():
    base = ["once", "--port", "P", "--db", "x.db", "--scenario", "estatico", "--environment", "outdoor"]
    with pytest.raises(SystemExit):
        cd.build_parser().parse_args(base)
    with pytest.raises(SystemExit):
        cd.build_parser().parse_args(base + ["--reference-point", "P01", "--route", "r1"])


def test_point_add_e_point_list(tmp_path, capsys):
    banco = str(tmp_path / "p.db")
    cd.main(["point-add", "--db", banco, "P01", "--lat", "-26.9", "--lon", "-49.07", "--coord-source", "rtk",
             "--uncertainty-m", "0.02"])
    cd.main(["point-list", "--db", banco])
    assert "P01 | -26.9 | -49.07 |  | rtk | 0.02" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="já tem coordenada"):
        cd.main(["point-add", "--db", banco, "P01", "--lat", "0", "--lon", "0"])


def test_coletor_recusa_banco_v1_sem_abrir_o_modem(monkeypatch, tmp_path):
    v1 = banco_v1(tmp_path / "v1.db")
    antes = sha(v1)
    monkeypatch.setattr(cd, "_open_modem", lambda args: pytest.fail("não deveria abrir o modem"))
    with pytest.raises(SystemExit, match="coletor.migrate"):
        cd.main(["once", "--port", "P", "--db", v1, *SESSAO_PONTO])
    assert sha(v1) == antes


# --------------------------------------------------------------------------- export e restauração
@pytest.fixture
def banco_com_dados(monkeypatch, tmp_path, sem_espera):
    banco = str(tmp_path / "dados.db")
    rodar(monkeypatch, CICLO, sem_espera, "once", "--port", "P", "--db", banco, *SESSAO_PONTO, "--notes", "linha 1\nlinha 2")
    rodar(monkeypatch, dict(CICLO, **{"AT$GPSACP": "gpsacp_sem_fix.txt"}), sem_espera, "continuous", "--port", "P",
          "--db", banco, *SESSAO_ROTA, "--interval", "0", "--max-measurements", "2", "--lpp-probe")
    return banco


def test_export_e_restore_reconstroem_o_banco(banco_com_dados, tmp_path):
    antes = sha(banco_com_dados)
    pasta = tmp_path / "export"
    meta = export_csv.export(banco_com_dados, str(pasta))
    assert sha(banco_com_dados) == antes
    assert set(os.listdir(pasta)) == {f"{t}.csv" for t in cdb.TABLES} | {"metadata.json"}
    assert meta["tables"]["measurements"]["rows"] == 3
    novo = str(tmp_path / "restaurado.db")
    export_csv.restore(str(pasta), novo)
    for tabela in cdb.TABLES:
        assert linhas(novo, f"SELECT * FROM {tabela} ORDER BY 1") == linhas(banco_com_dados, f"SELECT * FROM {tabela} ORDER BY 1")
    assert linhas(novo, "PRAGMA user_version") == [(2,)]


def test_restore_recusa_destino_existente_e_csv_alterado(banco_com_dados, tmp_path):
    pasta = tmp_path / "export"
    export_csv.export(banco_com_dados, str(pasta))
    with pytest.raises(FileExistsError):
        export_csv.restore(str(pasta), banco_com_dados)
    with open(pasta / "sessions.csv", "a", encoding="utf-8") as f:
        f.write("adulterado\n")
    with pytest.raises(ValueError, match="SHA-256"):
        export_csv.restore(str(pasta), str(tmp_path / "novo.db"))
    assert not (tmp_path / "novo.db").exists()


def test_restore_com_contagem_errada_nao_deixa_arquivo(banco_com_dados, tmp_path):
    pasta = tmp_path / "export"
    export_csv.export(banco_com_dados, str(pasta))
    meta = json.loads((pasta / "metadata.json").read_text(encoding="utf-8"))
    meta["tables"]["measurements"]["rows"] = 99
    (pasta / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    with pytest.raises(ValueError, match="linhas"):
        export_csv.restore(str(pasta), str(tmp_path / "novo.db"))
    assert not (tmp_path / "novo.db").exists()


def test_export_recusa_pasta_com_arquivos_e_banco_v1(banco_com_dados, tmp_path):
    pasta = tmp_path / "ocupada"
    pasta.mkdir()
    (pasta / "algo.txt").write_text("x", encoding="utf-8")
    with pytest.raises(FileExistsError):
        export_csv.export(banco_com_dados, str(pasta))
    with pytest.raises(cdb.SchemaError):
        export_csv.export(banco_v1(tmp_path / "v1.db"), str(tmp_path / "export_v1"))


# --------------------------------------------------------------------------- migração v1 -> v2
def test_inspect_relata_sem_alterar(tmp_path):
    v1 = banco_v1(tmp_path / "v1.db")
    antes = sha(v1)
    r = migrate.inspect(v1)
    assert r["contagens"] == {"health_checks": 2, "measurements": 4, "neighbor_cells": 4, "sessions": 2}
    assert (r["medicoes_orfas"], r["vizinhas_orfas"], r["vizinhas_rsrp_zero"], r["sessoes_sem_fim"]) == (1, 1, 1, 1)
    assert sha(v1) == antes


def test_migracao_nao_destrutiva_marca_identidades_ambiguas(tmp_path):
    v1 = banco_v1(tmp_path / "v1.db")
    antes = sha(v1)
    saida = str(tmp_path / "v2.db")
    r = migrate.migrate(v1, saida)
    assert sha(v1) == antes and r["sha256_origem"] == antes
    assert (r["sessoes"], r["sessoes_substitutas"], r["medicoes"], r["celulas"]) == (3, 1, 4, 3)
    assert (r["celulas_rsrp_zero_para_null"], r["vizinhas_orfas_ignoradas"], r["fix_invalido_v1_movido_para_legacy"]) == (1, 1, 1)

    conn = cdb.connect(saida)  # abre como v2 válido
    assert set(conn.execute(
        "SELECT access_mode, legacy_ambiguous, serving_radio_key, serving_global_cell_key FROM measurements"
    ).fetchall()) == {("UNKNOWN", 1, None, None)}
    assert conn.execute(
        "SELECT gnss_status, gnss_fix_type, gnss_lat FROM measurements WHERE measurement_id = 1").fetchone() == ("ok", 3, -26.9)
    status, lat, legacy = conn.execute(
        "SELECT gnss_status, gnss_lat, legacy_json FROM measurements WHERE measurement_id = 2").fetchone()
    assert (status, lat, json.loads(legacy)["gnss_lat"]) == ("legacy_fix_invalido", None, -26.91)
    assert set(conn.execute("SELECT source, identity_status, radio_key FROM observed_cells").fetchall()) == {
        ("LEGACY_V1", "legacy_ambiguous", None)}
    assert conn.execute("SELECT rsrp_dbm FROM observed_cells WHERE pci = 64").fetchone() == (None,)
    assert conn.execute("SELECT session_id, mode, status FROM sessions ORDER BY session_id").fetchall() == [
        (1, "legacy_v1", "completed"), (2, "legacy_v1", "interrupted"), (9, "legacy_v1", "interrupted")]
    assert conn.execute("SELECT DISTINCT session_id, event FROM health_checks").fetchall() == [(None, "legacy_v1")]
    conn.close()


def test_migracao_recusa_saida_igual_ou_existente(tmp_path):
    v1 = banco_v1(tmp_path / "v1.db")
    with pytest.raises(ValueError):
        migrate.migrate(v1, v1)
    existente = tmp_path / "existe.db"
    existente.write_bytes(b"")
    with pytest.raises(FileExistsError):
        migrate.migrate(v1, str(existente))


def test_migracao_recusa_banco_que_nao_e_v1(tmp_path):
    v2 = str(tmp_path / "v2.db")
    cdb.connect(v2).close()
    with pytest.raises(cdb.SchemaError):
        migrate.migrate(v2, str(tmp_path / "saida.db"))


def test_cli_inspect_imprime_json(tmp_path, capsys):
    migrate.main(["inspect", "--db", banco_v1(tmp_path / "v1.db")])
    assert json.loads(capsys.readouterr().out)["contagens"]["sessions"] == 2


# --------------------------------------------------------------------------- Estimador legado
def test_estimador_legado_recusa_banco_v2(tmp_path):
    from estimador.features import open_legacy_radiomap

    banco = str(tmp_path / "v2.db")
    cdb.connect(banco).close()
    with pytest.raises(SystemExit, match="fase 5"):
        open_legacy_radiomap(banco)


def test_estimador_legado_le_v1_somente_leitura_e_nao_cria_arquivo(tmp_path):
    from estimador.features import open_legacy_radiomap

    v1 = banco_v1(tmp_path / "v1.db")
    antes = sha(v1)
    conn = open_legacy_radiomap(v1)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM sessions")
    conn.close()
    assert sha(v1) == antes
    with pytest.raises(SystemExit):
        open_legacy_radiomap(str(tmp_path / "nao_existe.db"))
    assert not (tmp_path / "nao_existe.db").exists()
