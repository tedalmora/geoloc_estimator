"""Métricas de avaliação -- erro haversine, CDF e percentil 90.

Reaproveita a definição de métricas de planejamento/visao_geral_projeto.md, Seção 7, para
não ter a métrica redefinida (e potencialmente divergente) em mais de um lugar.
"""
import numpy as np


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distância em metros entre 2 pontos GPS (fórmula haversine)."""
    R = 6371000.0  # raio médio da Terra, em metros
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dphi = np.radians(lat2 - lat1)
    dlambda = np.radians(lon2 - lon1)
    a = np.sin(dphi / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dlambda / 2) ** 2
    return 2 * R * np.arcsin(np.sqrt(a))


def evaluate_predictions(y_true, y_pred) -> dict:
    """y_true/y_pred: arrays Nx2 no formato [lat, lon] -- é o que KNeighborsRegressor e
    RandomForestRegressor devolvem quando treinados com y = [gnss_lat, gnss_lon].
    Tolera lista vazia (ex.: cell_id sem nenhuma célula reconhecida no conjunto avaliado)."""
    y_true = np.asarray(y_true)
    y_pred = np.asarray(y_pred)
    if len(y_true) == 0:
        return {"erro_medio_m": None, "erro_p90_m": None, "erro_max_m": None, "cdf_ordenada_m": [], "n": 0}
    erros = np.array([
        haversine_m(y_true[i, 0], y_true[i, 1], y_pred[i, 0], y_pred[i, 1])
        for i in range(len(y_true))
    ])
    return {
        "erro_medio_m": float(erros.mean()),
        "erro_p90_m": float(np.percentile(erros, 90)),  # percentil 90, padrão citado na literatura 3GPP
        "erro_max_m": float(erros.max()),
        "cdf_ordenada_m": np.sort(erros).tolist(),  # plotar com matplotlib para a figura de CDF do Capítulo 5
        "n": len(erros),
    }
