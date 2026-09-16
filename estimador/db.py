"""Banco SQLite PRÓPRIO do Estimador -- guarda as ESTIMATIVAS de posição de estimador/live.py
(modos `once` E `continuous`). Não tem nada a ver com o radiomapa do Coletor
(measurements/neighbor_cells): são arquivos/tabelas diferentes de propósito, para não
misturar "dado histórico de treino" com "resultado de posição ao vivo".

`lat_real`/`lon_real`/`erro_m` só vêm preenchidos quando havia um jeito de saber a posição
real na hora da medição (GNSS do próprio modem, ou `--gnss-real` informado na mão) -- é isso
que permite comparar os métodos depois, com `estimador/relatorio.py`.
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

