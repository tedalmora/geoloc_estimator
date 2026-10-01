"""Métricas de erro de posição: média, mediana (CE50), RMSE, p90 (CE90), p95, máximo e CDF.

Só biblioteca padrão. Percentis por interpolação linear entre as amostras ordenadas. A
cobertura é a fração de leituras com estimativa válida (n / n_total).
"""
import math
from typing import Optional, Sequence

from .geometry import haversine_m


def percentile(sorted_values: Sequence[float], q: float) -> float:
    if not sorted_values:
        raise ValueError("percentil de lista vazia")
    pos = (len(sorted_values) - 1) * q / 100.0
    lo, hi = math.floor(pos), math.ceil(pos)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


def error_summary(errors: Sequence[float], n_total: Optional[int] = None) -> dict:
    n = len(errors)
    total = n if n_total is None else n_total
    summary = {"n": n, "n_total": total, "coverage": (n / total) if total else None}
    if n == 0:
        return {**summary, "mean_m": None, "median_m": None, "rmse_m": None, "p90_m": None, "p95_m": None,
                "max_m": None, "cdf_m": []}
    ordered = sorted(errors)
    return {
        **summary, "mean_m": sum(ordered) / n, "median_m": percentile(ordered, 50),
        "rmse_m": math.sqrt(sum(e * e for e in ordered) / n), "p90_m": percentile(ordered, 90),
        "p95_m": percentile(ordered, 95), "max_m": ordered[-1], "cdf_m": ordered,
    }


# ---------------------------------------------------------------- legado (compare.py até a consolidação)
def evaluate_predictions(y_true, y_pred) -> dict:
    """y_true/y_pred: sequências de [lat, lon] (formato do compare.py legado)."""
    s = error_summary([haversine_m(t[0], t[1], p[0], p[1]) for t, p in zip(y_true, y_pred)])
    return {"erro_medio_m": s["mean_m"], "erro_p90_m": s["p90_m"], "erro_max_m": s["max_m"],
            "cdf_ordenada_m": s["cdf_m"], "n": s["n"]}
