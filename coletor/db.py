"""Banco do Coletor (SQLite), schema v2.

O Coletor é o único que grava aqui. Cada tentativa de leitura vira 1 linha em `measurements`,
inclusive as inválidas (com os motivos). Cada célula observada vira 1 linha em
`observed_cells`, com a fonte (MONI, SERVINFO, CSURV) e as duas identidades (radio_key e
global_cell_key). As respostas cruas de cada comando ficam em `measurements.raw_json`, para
que a coleta possa ser reprocessada se um parser mudar.

Convenções: horários em UTC, ISO 8601 com 'Z'; PRAGMA foreign_keys = ON em toda conexão;
PRAGMA user_version = 2. Bancos v1 (sem user_version) são recusados. Use
`python -m coletor.migrate`, que gera um banco v2 NOVO e nunca altera o original.
"""
import dataclasses
import json
import os
import pathlib
import sqlite3
from typing import List, Optional, Tuple

from acquisition.snapshot import Snapshot, utc_now_iso

SCHEMA_VERSION = 2
SCENARIOS = ("estatico", "pedestre", "veicular")
ENVIRONMENTS = ("indoor", "outdoor")
FINAL_SESSION_STATUSES = ("completed", "interrupted", "error")
SOURCE_LEGACY_V1 = "LEGACY_V1"
PENDING_COORD_SOURCE = "pendente"
# Ordem de dependência (chaves estrangeiras): usada também pelo export/restore e pela migração.
TABLES = ("reference_points", "sessions", "measurements", "observed_cells", "health_checks")

SCHEMA = """
CREATE TABLE reference_points (
    reference_point_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT NOT NULL UNIQUE,
    lat                 REAL,
    lon                 REAL,
    alt_m               REAL,
    coord_source        TEXT NOT NULL,  -- origem da coordenada cadastrada; 'pendente' = ainda sem coordenada
    coord_uncertainty_m REAL,
    description         TEXT,
    created_at          TEXT NOT NULL,
    CHECK ((lat IS NULL AND lon IS NULL) OR (lat BETWEEN -90 AND 90 AND lon BETWEEN -180 AND 180))
);

CREATE TABLE sessions (
    session_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at          TEXT NOT NULL,
    ended_at            TEXT,
    status              TEXT NOT NULL CHECK (status IN ('running', 'completed', 'interrupted', 'error')),
    end_reason          TEXT,
    mode                TEXT NOT NULL CHECK (mode IN ('once', 'continuous', 'legacy_v1')),
    scenario            TEXT NOT NULL,
    environment         TEXT NOT NULL,
    reference_point_id  INTEGER REFERENCES reference_points(reference_point_id),
    route               TEXT,
    campaign_plmn       TEXT,
    modem_id            TEXT NOT NULL,
    notes               TEXT,
    metadata_json       TEXT NOT NULL
);

CREATE TABLE measurements (
    measurement_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id                INTEGER NOT NULL REFERENCES sessions(session_id),
    reference_point_id        INTEGER REFERENCES reference_points(reference_point_id),
    seq                       INTEGER NOT NULL,
    started_at                TEXT NOT NULL,
    snapshot_status           TEXT NOT NULL CHECK (snapshot_status IN ('complete', 'partial', 'invalid')),
    reasons_json              TEXT NOT NULL,
    access_mode               TEXT NOT NULL CHECK (access_mode IN ('LTE', 'NR_NSA', 'NR_SA', 'UNKNOWN')),
    serving_method            TEXT,
    serving_rat               TEXT,
    serving_arfcn             INTEGER,
    serving_pci               INTEGER,
    serving_plmn              TEXT,
    serving_cell_id           TEXT,
    serving_tac               TEXT,
    serving_radio_key         TEXT,
    serving_radio_key_source  TEXT,
    serving_global_cell_key   TEXT,
    serving_global_key_source TEXT,
    nr_leg_radio_key          TEXT,
    serving_rsrp_dbm          REAL,
    serving_rsrq_db           REAL,
    serving_sinr_db           REAL,
    moni_status               TEXT NOT NULL,
    servinfo_status           TEXT NOT NULL,
    csurv_status              TEXT NOT NULL,
    gnss_status               TEXT NOT NULL,
    lpp_probe_status          TEXT,
    gnss_lat                  REAL,
    gnss_lon                  REAL,
    gnss_alt_m                REAL,
    gnss_fix_type             INTEGER,
    gnss_hdop                 REAL,
    gnss_nsat                 INTEGER,
    gnss_utc                  TEXT,
    timings_json              TEXT NOT NULL,
    anomalies_json            TEXT NOT NULL,
    transport_error           TEXT,
    raw_json                  TEXT NOT NULL,
    legacy_ambiguous          INTEGER NOT NULL DEFAULT 0 CHECK (legacy_ambiguous IN (0, 1)),
    legacy_json               TEXT,
    UNIQUE (session_id, seq)
);

CREATE TABLE observed_cells (
    observed_cell_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    measurement_id    INTEGER NOT NULL REFERENCES measurements(measurement_id),
    source            TEXT NOT NULL CHECK (source IN ('MONI', 'SERVINFO', 'CSURV', 'LEGACY_V1')),
    rat               TEXT CHECK (rat IN ('LTE', 'NR')),
    arfcn             INTEGER,
    pci               INTEGER,
    plmn              TEXT,
    cell_id           TEXT,
    tac               TEXT,
    rsrp_dbm          REAL,
    rsrq_db           REAL,
    sinr_db           REAL,
    rx_level_dbm      REAL,
    cell_status       TEXT,
    band              INTEGER,
    bandwidth         INTEGER,
    scs_khz           INTEGER,
    record_format     TEXT,
    is_serving        INTEGER NOT NULL DEFAULT 0 CHECK (is_serving IN (0, 1)),
    radio_key         TEXT,
    global_cell_key   TEXT,
    identity_status   TEXT NOT NULL
        CHECK (identity_status IN ('complete', 'radio', 'global', 'none', 'legacy_ambiguous'))
);

CREATE TABLE health_checks (
    health_check_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id       INTEGER REFERENCES sessions(session_id),
    checked_at       TEXT NOT NULL,
    modem_id         TEXT NOT NULL,
    event            TEXT NOT NULL CHECK (event IN ('start', 'periodic', 'failure', 'recovered',
                                                    'reconnect_ok', 'reconnect_failed', 'end', 'legacy_v1')),
    alive            INTEGER NOT NULL CHECK (alive IN (0, 1)),
    detail           TEXT
);

CREATE INDEX idx_measurements_session ON measurements(session_id);
CREATE INDEX idx_observed_cells_measurement ON observed_cells(measurement_id);
CREATE INDEX idx_observed_cells_radio_key ON observed_cells(radio_key);
CREATE INDEX idx_health_checks_session ON health_checks(session_id);
PRAGMA user_version = 2;
"""


class SchemaError(RuntimeError):
    """Banco em formato que este código não grava (v1 ou versão desconhecida)."""


def schema_info(conn: sqlite3.Connection) -> Tuple[int, set]:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    return version, tables - {"sqlite_sequence"}


def open_read_only(db_path: str) -> sqlite3.Connection:
    """Conexão somente leitura (URI mode=ro): o arquivo não é criado nem alterado."""
    if not os.path.isfile(db_path):
        raise FileNotFoundError(f"{db_path}: arquivo não encontrado")
    return sqlite3.connect(pathlib.Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)


def connect(db_path: str) -> sqlite3.Connection:
    """Abre um banco v2 (ou cria, se o arquivo não existir ou estiver vazio). Bancos v1 ou de
    versão desconhecida são recusados depois de uma inspeção somente leitura, sem alteração."""
    if db_path != ":memory:" and os.path.isfile(db_path) and os.path.getsize(db_path) > 0:
        probe = open_read_only(db_path)
        try:
            version, tables = schema_info(probe)
        finally:
            probe.close()
        if version != SCHEMA_VERSION and (version != 0 or tables):
            if version == 0 and "measurements" in tables:
                raise SchemaError(
                    f"{db_path}: banco do Coletor no formato v1. Ele não é alterado. Para gerar uma cópia v2, use "
                    f"'python -m coletor.migrate inspect --db {db_path}' e depois "
                    f"'python -m coletor.migrate migrate --db {db_path} --out <novo_arquivo.db>'."
                )
            raise SchemaError(
                f"{db_path}: versão de schema {version} desconhecida (este código grava a versão {SCHEMA_VERSION})."
            )
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    version, tables = schema_info(conn)
    if version == 0 and not tables:
        conn.executescript(SCHEMA)
    conn.execute("PRAGMA journal_mode = WAL")  # o Estimador pode ler enquanto o Coletor grava
    return conn


# ---------------------------------------------------------------- pontos de referência
def add_reference_point(
    conn: sqlite3.Connection, name: str, lat: float, lon: float, alt_m: Optional[float] = None,
    coord_source: str = "levantamento", coord_uncertainty_m: Optional[float] = None,
    description: Optional[str] = None,
) -> int:
    """Cadastra a coordenada de um ponto. Completa um ponto 'pendente' (criado pela coleta) e
    recusa trocar uma coordenada já cadastrada."""
    if coord_source == PENDING_COORD_SOURCE:
        raise ValueError("coord_source 'pendente' é reservado a pontos sem coordenada.")
    row = conn.execute("SELECT reference_point_id, lat FROM reference_points WHERE name = ?", (name,)).fetchone()
    with conn:
        if row is None:
            cur = conn.execute(
                "INSERT INTO reference_points (name, lat, lon, alt_m, coord_source, coord_uncertainty_m, "
                "description, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (name, lat, lon, alt_m, coord_source, coord_uncertainty_m, description, utc_now_iso()),
            )
            return cur.lastrowid
        if row[1] is not None:
            raise ValueError(f"o ponto {name!r} já tem coordenada cadastrada; ela não é substituída.")
        conn.execute(
            "UPDATE reference_points SET lat = ?, lon = ?, alt_m = ?, coord_source = ?, coord_uncertainty_m = ?, "
            "description = COALESCE(?, description) WHERE reference_point_id = ?",
            (lat, lon, alt_m, coord_source, coord_uncertainty_m, description, row[0]),
        )
        return row[0]


def get_or_create_reference_point(conn: sqlite3.Connection, name: str) -> Tuple[int, bool, bool]:
    """Devolve (id, criado_agora, tem_coordenada). Ponto novo nasce 'pendente', sem coordenada."""
    row = conn.execute("SELECT reference_point_id, lat FROM reference_points WHERE name = ?", (name,)).fetchone()
    if row is not None:
        return row[0], False, row[1] is not None
    with conn:
        cur = conn.execute(
            "INSERT INTO reference_points (name, coord_source, created_at) VALUES (?, ?, ?)",
            (name, PENDING_COORD_SOURCE, utc_now_iso()),
        )
    return cur.lastrowid, True, False


def list_reference_points(conn: sqlite3.Connection) -> List[tuple]:
    return conn.execute(
        "SELECT reference_point_id, name, lat, lon, alt_m, coord_source, coord_uncertainty_m, description "
        "FROM reference_points ORDER BY reference_point_id"
    ).fetchall()


# ---------------------------------------------------------------- sessões
def create_session(
    conn: sqlite3.Connection, *, mode: str, scenario: str, environment: str, modem_id: str,
    metadata: dict, reference_point_id: Optional[int] = None, route: Optional[str] = None,
    campaign_plmn: Optional[str] = None, notes: Optional[str] = None,
) -> int:
    if scenario not in SCENARIOS or environment not in ENVIRONMENTS:
        raise ValueError(f"cenário/ambiente inválido: {scenario!r}/{environment!r}")
    if (reference_point_id is None) == (route is None):
        raise ValueError("informe exatamente um entre ponto de referência e rota.")
    with conn:
        cur = conn.execute(
            "INSERT INTO sessions (started_at, status, mode, scenario, environment, reference_point_id, route, "
            "campaign_plmn, modem_id, notes, metadata_json) VALUES (?, 'running', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (utc_now_iso(), mode, scenario, environment, reference_point_id, route, campaign_plmn, modem_id, notes,
             json.dumps(metadata, ensure_ascii=False, sort_keys=True)),
        )
    return cur.lastrowid


def finish_session(conn: sqlite3.Connection, session_id: int, status: str, end_reason: Optional[str] = None) -> None:
    if status not in FINAL_SESSION_STATUSES:
        raise ValueError(f"status final inválido: {status!r}")
    with conn:
        conn.execute(
            "UPDATE sessions SET status = ?, ended_at = ?, end_reason = ? WHERE session_id = ?",
            (status, utc_now_iso(), end_reason, session_id),
        )


def insert_health_check(
    conn: sqlite3.Connection, session_id: Optional[int], modem_id: str, event: str, alive: bool,
    detail: Optional[str] = None,
) -> None:
    with conn:
        conn.execute(
            "INSERT INTO health_checks (session_id, checked_at, modem_id, event, alive, detail) VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, utc_now_iso(), modem_id, event, int(alive), detail),
        )


# ---------------------------------------------------------------- medições
_OBSERVATION_COLUMNS = (
    "source", "rat", "arfcn", "pci", "plmn", "cell_id", "tac", "rsrp_dbm", "rsrq_db", "sinr_db", "rx_level_dbm",
    "cell_status", "band", "bandwidth", "scs_khz", "record_format", "is_serving",
)


def _raw_response(reading) -> Optional[dict]:
    if reading is None or reading.response is None:
        return None
    return dataclasses.asdict(reading.response)


def insert_snapshot(
    conn: sqlite3.Connection, session_id: int, seq: int, snap: Snapshot, reference_point_id: Optional[int] = None
) -> int:
    """Grava 1 snapshot (medição + células observadas) numa única transação."""
    fix = snap.gnss.fix
    serving = snap.serving
    row = {
        "session_id": session_id, "reference_point_id": reference_point_id, "seq": seq,
        "started_at": snap.started_at, "snapshot_status": snap.status,
        "reasons_json": json.dumps(snap.reasons, ensure_ascii=False), "access_mode": snap.access_mode,
        "serving_method": serving.method, "serving_rat": serving.rat, "serving_arfcn": serving.arfcn,
        "serving_pci": serving.pci, "serving_plmn": serving.plmn, "serving_cell_id": serving.cell_id,
        "serving_tac": serving.tac, "serving_radio_key": serving.radio_key,
        "serving_radio_key_source": serving.radio_key_source, "serving_global_cell_key": serving.global_cell_key,
        "serving_global_key_source": serving.global_key_source, "nr_leg_radio_key": serving.nr_leg_radio_key,
        "serving_rsrp_dbm": snap.serving_rsrp_dbm, "serving_rsrq_db": snap.serving_rsrq_db,
        "serving_sinr_db": snap.serving_sinr_db, "moni_status": snap.moni.status,
        "servinfo_status": snap.servinfo.status, "csurv_status": snap.csurv.status,
        "gnss_status": snap.gnss.status,
        "gnss_lat": fix.lat if fix else None, "gnss_lon": fix.lon if fix else None,
        "gnss_alt_m": fix.alt_m if fix else None, "gnss_fix_type": fix.fix_type if fix else None,
        "gnss_hdop": fix.hdop if fix else None, "gnss_nsat": fix.nsat if fix else None,
        "gnss_utc": fix.utc if fix else None, "timings_json": json.dumps(snap.timings_s, sort_keys=True),
        "anomalies_json": json.dumps(snap.anomalies, ensure_ascii=False), "transport_error": snap.transport_error,
        "raw_json": json.dumps({
            "MONI": _raw_response(snap.moni), "SERVINFO": _raw_response(snap.servinfo),
            "CSURV": _raw_response(snap.csurv), "GNSS": _raw_response(snap.gnss),
        }, ensure_ascii=False),
    }
    with conn:
        cur = conn.execute(
            f"INSERT INTO measurements ({', '.join(row)}) VALUES ({', '.join(':' + k for k in row)})", row
        )
        measurement_id = cur.lastrowid
        conn.executemany(
            f"INSERT INTO observed_cells (measurement_id, {', '.join(_OBSERVATION_COLUMNS)}, radio_key, "
            f"global_cell_key, identity_status) VALUES ({', '.join('?' * (len(_OBSERVATION_COLUMNS) + 4))})",
            [
                (measurement_id, *(int(getattr(o, c)) if c == "is_serving" else getattr(o, c) for c in _OBSERVATION_COLUMNS),
                 o.radio_key, o.global_cell_key, o.identity_status)
                for o in snap.observations
            ],
        )
    return measurement_id
