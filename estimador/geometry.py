"""Geometria do Estimador: distância geodésica e plano local ENU (WGS-84).

Só biblioteca padrão (math): roda no Raspberry Pi sem NumPy.
"""
import math

EARTH_MEAN_RADIUS_M = 6371008.8  # raio médio (IUGG), usado no haversine


def validate_lat_lon(lat, lon) -> None:
    for name, value, limit in (("lat", lat, 90.0), ("lon", lon, 180.0)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{name} inválida: {value!r}")
        if abs(value) > limit:
            raise ValueError(f"{name} fora da faixa ±{limit:g}: {value!r}")


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distância em metros entre dois pontos (esfera de raio médio)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_MEAN_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))
