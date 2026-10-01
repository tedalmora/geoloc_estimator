"""Banco SQLite das estimativas gravadas pelo estimador/live.py anterior (position_estimates).

O live.py atual (modo once) não grava neste banco. O módulo fica só para descrever o formato
dos arquivos já existentes, que `python -m estimador.evaluate live` lê em modo somente leitura.
Não tem nada a ver com o radiomapa do Coletor.

`lat_real`/`lon_real`/`erro_m` só vêm preenchidos quando havia posição real conhecida na hora
da medição (GNSS do modem ou `--gnss-real` do live.py antigo).
"""
import sqlite3
import time
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS position_estimates (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp         REAL NOT NULL,
    metodo            TEXT NOT NULL,
    lat               REAL,
    lon               REAL,
    raio_incerteza_m  REAL,
    desvio_padrao_m   REAL,  -- só preenchido no modo `once` (N amostras por método)
    n_amostras        INTEGER,
    lat_real          REAL,  -- posição real conhecida na hora, se houver
    lon_real          REAL,
    erro_m            REAL   -- haversine(estimativa, real) -- só se lat_real/lon_real existirem
);
"""


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.executescript(SCHEMA)
    return conn


def insert_estimate(
    conn: sqlite3.Connection,
    metodo: str,
    lat: Optional[float],
    lon: Optional[float],
    raio_incerteza_m: Optional[float] = None,
    desvio_padrao_m: Optional[float] = None,
    n_amostras: Optional[int] = None,
    lat_real: Optional[float] = None,
    lon_real: Optional[float] = None,
    erro_m: Optional[float] = None,
) -> None:
    conn.execute(
        """INSERT INTO position_estimates (
            timestamp, metodo, lat, lon, raio_incerteza_m, desvio_padrao_m, n_amostras,
            lat_real, lon_real, erro_m
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (time.time(), metodo, lat, lon, raio_incerteza_m, desvio_padrao_m, n_amostras, lat_real, lon_real, erro_m),
    )
    conn.commit()

