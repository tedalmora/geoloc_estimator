"""Camada de acesso ao SQLite -- schema e funções de escrita usadas pelo daemon do Coletor.

O Coletor é o único dono deste banco (o HAL nunca grava nada aqui -- ver arquitetura_hal.md).
"""
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT NOT NULL,
    ended_at     TEXT,
    scenario     TEXT NOT NULL,
    environment  TEXT NOT NULL,
    notes        TEXT
);

CREATE TABLE IF NOT EXISTS measurements (
    measurement_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id       INTEGER NOT NULL REFERENCES sessions(session_id),
    timestamp        REAL NOT NULL,
    modem_id         TEXT NOT NULL,
    technology       TEXT NOT NULL,
    serving_cell_id  TEXT,
    serving_pci      INTEGER,
    serving_tac      TEXT,
    rsrp_dbm         REAL,
    rsrq_db          REAL,
    sinr_db          REAL,
    timing_advance   INTEGER,
    gnss_lat         REAL,
    gnss_lon         REAL,
    gnss_alt_m       REAL,
    gnss_fix_quality TEXT,
    lpp_raw_log      TEXT
);

CREATE TABLE IF NOT EXISTS neighbor_cells (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    measurement_id  INTEGER NOT NULL REFERENCES measurements(measurement_id),
    pci             INTEGER NOT NULL,
    rsrp_dbm        REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS health_checks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp  REAL NOT NULL,
    modem_id   TEXT NOT NULL,
    alive      INTEGER NOT NULL,  -- 0 ou 1 (SQLite não tem tipo booleano nativo)
    note       TEXT
);

CREATE INDEX IF NOT EXISTS idx_measurements_session ON measurements(session_id);
CREATE INDEX IF NOT EXISTS idx_neighbors_measurement ON neighbor_cells(measurement_id);
"""


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL;")  # permite o Estimador ler o banco enquanto o Coletor grava
    conn.executescript(SCHEMA)
    return conn


def create_session(conn: sqlite3.Connection, scenario: str, environment: str, notes: str = "") -> int:
    cur = conn.execute(
        "INSERT INTO sessions (started_at, scenario, environment, notes) VALUES (?, ?, ?, ?)",
        (time.strftime("%Y-%m-%dT%H:%M:%S"), scenario, environment, notes),
    )
    conn.commit()
    return cur.lastrowid


def close_session(conn: sqlite3.Connection, session_id: int) -> None:
    conn.execute(
        "UPDATE sessions SET ended_at = ? WHERE session_id = ?",
        (time.strftime("%Y-%m-%dT%H:%M:%S"), session_id),
    )
    conn.commit()


def insert_measurement(conn: sqlite3.Connection, item: dict) -> None:
    """`item` vem da fila do daemon -- ver coletor/daemon.py (poll_once) para o formato exato."""
    signal = item["signal"]
    cell = item["cell"]
    gnss = item["gnss"]
    cur = conn.execute(
        """INSERT INTO measurements (
            session_id, timestamp, modem_id, technology,
            serving_cell_id, serving_pci, serving_tac,
            rsrp_dbm, rsrq_db, sinr_db, timing_advance,
            gnss_lat, gnss_lon, gnss_alt_m, gnss_fix_quality, lpp_raw_log
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            item["session_id"], item["timestamp"], item["modem_id"], item["technology"],
            cell.cell_id, cell.pci, cell.tac,
            signal.rsrp_dbm, signal.rsrq_db, signal.sinr_db, signal.timing_advance,
            gnss.lat if gnss else None, gnss.lon if gnss else None,
            gnss.alt_m if gnss else None, gnss.fix_quality if gnss else None,
            item.get("lpp_raw_log"),
        ),
    )
    measurement_id = cur.lastrowid
    for neighbor in item.get("neighbors", []):
        conn.execute(
            "INSERT INTO neighbor_cells (measurement_id, pci, rsrp_dbm) VALUES (?, ?, ?)",
            (measurement_id, neighbor.pci, neighbor.rsrp_dbm),
        )
    conn.commit()


def insert_health_check(conn: sqlite3.Connection, modem_id: str, alive: bool, note: str = "") -> None:
    conn.execute(
        "INSERT INTO health_checks (timestamp, modem_id, alive, note) VALUES (?, ?, ?, ?)",
        (time.time(), modem_id, int(alive), note),
    )
    conn.commit()
