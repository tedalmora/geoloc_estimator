"""Construção de vetores de features de tamanho fixo a partir do radiomapa (SQLite).

Problema resolvido aqui: cada medição enxerga um número diferente de células vizinhas
(a tabela `neighbor_cells` é 1:N com `measurements`), mas o scikit-learn exige um X de
tamanho fixo. A solução: um vocabulário fixo de PCIs conhecidos vira o conjunto de colunas;
cada leitura preenche a coluna do PCI que enxergou naquele instante, e usa uma sentinela
(FLOOR_RSRP_DBM) para os PCIs que não apareceram -- nunca o número do PCI em si dentro do
vetor (ver planejamento/knn_explicado.md, Seção 8, sobre não usar IDs como valor contínuo).

Ver planejamento/arquitetura_estimadores_posicao.md, Seção 3, para a justificativa completa.
"""
import sqlite3
from typing import Dict, List, Tuple

import pandas as pd

FLOOR_RSRP_DBM = -140.0  # abaixo do RSRP mínimo realista -- "célula não observada nesta leitura"


def build_pci_vocabulary(conn: sqlite3.Connection, technology: str) -> List[int]:
    """Lista ordenada de todos os PCIs já vistos (servidora + vizinhas) para uma tecnologia.
    Precisa ser salva junto do modelo treinado -- uma leitura nova só pode virar vetor usando
    exatamente esta mesma ordem de colunas."""
    query = """
        SELECT DISTINCT pci FROM (
            SELECT serving_pci AS pci FROM measurements WHERE technology = ?
            UNION
            SELECT nc.pci FROM neighbor_cells nc
            JOIN measurements m ON m.measurement_id = nc.measurement_id
            WHERE m.technology = ?
        ) WHERE pci IS NOT NULL
    """
    pcis = pd.read_sql(query, conn, params=(technology, technology))["pci"].tolist()
    return sorted(pcis)


def build_feature_vector(measurement_row: dict, neighbors: List[dict], vocabulary: List[int]) -> List[float]:
    """1 leitura (measurement + suas neighbor_cells) -> vetor de tamanho fixo len(vocabulary) + 1.

    IMPORTANTE: esta é a ÚNICA função que monta o vetor de features -- tanto o treino
    (build_training_table) quanto a inferência (estimador/infer.py) passam por aqui, para
    nunca correrem o risco de gerar vetores com tamanho/ordem diferentes entre si.
    O timing_advance vai sempre na última posição, depois das colunas de PCI.
    """
    rsrp_by_pci: Dict[int, float] = {n["pci"]: n["rsrp_dbm"] for n in neighbors}
    if measurement_row.get("serving_pci") is not None:
        rsrp_by_pci[measurement_row["serving_pci"]] = measurement_row["rsrp_dbm"]
    vector = [rsrp_by_pci.get(pci, FLOOR_RSRP_DBM) for pci in vocabulary]
    vector.append(measurement_row.get("timing_advance") or 0)
    return vector


def build_training_table(conn: sqlite3.Connection, technology: str) -> Tuple[pd.DataFrame, pd.DataFrame, List[int]]:
    """Lê o radiomapa inteiro (para 1 tecnologia) e devolve (X, y, vocabulario) prontos
    para train_test_split -- ver estimador/train.py."""
    vocabulary = build_pci_vocabulary(conn, technology)
    measurements = pd.read_sql(
        "SELECT * FROM measurements WHERE technology = ? AND gnss_lat IS NOT NULL",
        conn, params=(technology,),
    )
    rows = []
    for _, m in measurements.iterrows():
        neighbors = pd.read_sql(
            "SELECT pci, rsrp_dbm FROM neighbor_cells WHERE measurement_id = ?",
            conn, params=(int(m["measurement_id"]),),
        ).to_dict("records")
        rows.append(build_feature_vector(m.to_dict(), neighbors, vocabulary))
    columns = [f"pci_{p}" for p in vocabulary] + ["timing_advance"]
    X = pd.DataFrame(rows, columns=columns)
    y = measurements[["gnss_lat", "gnss_lon"]].reset_index(drop=True)
    return X, y, vocabulary
