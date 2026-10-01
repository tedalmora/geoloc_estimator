"""Geometria do Estimador: distância geodésica e plano local ENU (WGS-84).

ENU: plano tangente ao elipsoide WGS-84 numa origem (lat0, lon0, h0), com x = leste,
y = norte e z = cima, em metros. O WKNN trabalha em x/y; a volta para latitude e longitude
só acontece na saída. Só biblioteca padrão (math): roda no Raspberry Pi sem NumPy.
"""
import math
from dataclasses import dataclass
from typing import Tuple

EARTH_MEAN_RADIUS_M = 6371008.8  # raio médio (IUGG), usado no haversine
WGS84_A = 6378137.0
WGS84_F = 1 / 298.257223563
WGS84_E2 = WGS84_F * (2 - WGS84_F)
COORDINATE_SYSTEM = "ENU-WGS84"


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


def geodetic_to_ecef(lat: float, lon: float, h: float = 0.0) -> Tuple[float, float, float]:
    phi, lam = math.radians(lat), math.radians(lon)
    n = WGS84_A / math.sqrt(1 - WGS84_E2 * math.sin(phi) ** 2)
    return ((n + h) * math.cos(phi) * math.cos(lam), (n + h) * math.cos(phi) * math.sin(lam),
            (n * (1 - WGS84_E2) + h) * math.sin(phi))


def ecef_to_geodetic(x: float, y: float, z: float) -> Tuple[float, float, float]:
    p = math.hypot(x, y)
    lam = math.atan2(y, x)
    phi = math.atan2(z, p * (1 - WGS84_E2))
    for _ in range(10):  # iteração de ponto fixo; converge em poucas voltas perto da superfície
        n = WGS84_A / math.sqrt(1 - WGS84_E2 * math.sin(phi) ** 2)
        h = p * math.cos(phi) + z * math.sin(phi) - WGS84_A * math.sqrt(1 - WGS84_E2 * math.sin(phi) ** 2)
        new_phi = math.atan2(z, p * (1 - WGS84_E2 * n / (n + h)))
        if abs(new_phi - phi) < 1e-15:
            phi = new_phi
            break
        phi = new_phi
    h = p * math.cos(phi) + z * math.sin(phi) - WGS84_A * math.sqrt(1 - WGS84_E2 * math.sin(phi) ** 2)
    return math.degrees(phi), math.degrees(lam), h


@dataclass(frozen=True)
class EnuFrame:
    """Plano local ENU com origem registrada (vai para o bundle do WKNN)."""
    lat0: float
    lon0: float
    h0: float = 0.0

    def __post_init__(self):
        validate_lat_lon(self.lat0, self.lon0)

    def to_enu(self, lat: float, lon: float, h: float = 0.0) -> Tuple[float, float, float]:
        x0, y0, z0 = geodetic_to_ecef(self.lat0, self.lon0, self.h0)
        x, y, z = geodetic_to_ecef(lat, lon, h)
        dx, dy, dz = x - x0, y - y0, z - z0
        sp, cp = math.sin(math.radians(self.lat0)), math.cos(math.radians(self.lat0))
        sl, cl = math.sin(math.radians(self.lon0)), math.cos(math.radians(self.lon0))
        return (-sl * dx + cl * dy, -sp * cl * dx - sp * sl * dy + cp * dz, cp * cl * dx + cp * sl * dy + sp * dz)

    def to_geodetic(self, e: float, n: float, u: float = 0.0) -> Tuple[float, float, float]:
        x0, y0, z0 = geodetic_to_ecef(self.lat0, self.lon0, self.h0)
        sp, cp = math.sin(math.radians(self.lat0)), math.cos(math.radians(self.lat0))
        sl, cl = math.sin(math.radians(self.lon0)), math.cos(math.radians(self.lon0))
        dx = -sl * e - sp * cl * n + cp * cl * u
        dy = cl * e - sp * sl * n + cp * sl * u
        dz = cp * n + sp * u
        return ecef_to_geodetic(x0 + dx, y0 + dy, z0 + dz)

    def to_dict(self) -> dict:
        return {"lat": self.lat0, "lon": self.lon0, "h_m": self.h0}

    @classmethod
    def from_dict(cls, data: dict) -> "EnuFrame":
        return cls(data["lat"], data["lon"], data.get("h_m", 0.0))
