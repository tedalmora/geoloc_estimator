"""Método: Fingerprinting completo.

Usa o RSRP de TODAS as células vistas numa leitura (servidora + vizinhas), comparado com o
radiomapa inteiro via KNN/WKNN e Random Forest. É o método mais rico dos três "baseados no
aparelho" (ver cell_id.py e timing_advance.py para os outros dois) -- ver
planejamento/arquitetura_estimadores_posicao.md, Seções 3-4, para o desenho completo do
vetor de features (o vocabulário fixo de PCIs que resolve o problema de células vizinhas
variáveis).
"""
from typing import List

import pandas as pd
from sklearn.ensemble import RandomForestRegressor
from sklearn.neighbors import KNeighborsRegressor
from sklearn.preprocessing import StandardScaler

from ..features import build_feature_vector


def train_fingerprint(X_train: pd.DataFrame, y_train: pd.DataFrame) -> dict:
    # KNN/WKNN depende de distância numérica -- precisa normalizar (ver knn_explicado.md, Seção 8).
    # Random Forest não precisa (compara limiares por feature, não distância entre elas).
    scaler = StandardScaler()
    X_train_norm = scaler.fit_transform(X_train)
    wknn = KNeighborsRegressor(n_neighbors=5, weights="distance").fit(X_train_norm, y_train)
    rf = RandomForestRegressor(n_estimators=200, random_state=42).fit(X_train, y_train)
    return {"scaler": scaler, "wknn": wknn, "rf": rf}


def predict_fingerprint(
    bundle: dict, measurement_row: dict, neighbors: List[dict], vocabulary: List[int], modelo: str = "wknn"
):
    x = build_feature_vector(measurement_row, neighbors, vocabulary)
    if modelo == "rf":
        lat, lon = bundle["rf"].predict([x])[0]  # Random Forest usa o vetor cru, sem normalizar
    else:
        x_norm = bundle["scaler"].transform([x])
        lat, lon = bundle["wknn"].predict(x_norm)[0]
    return float(lat), float(lon)
