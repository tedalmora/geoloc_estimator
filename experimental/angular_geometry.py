"""Geometria de bearings (EXPERIMENTAL): interseção de duas semirretas a partir de âncoras conhecidas.

Não mede ângulo: recebe dois bearings externos e só calcula a solução geométrica. Nenhum ângulo
real foi obtido do FN990 nem da rede; DL-AoD e UL-AoA não estão integrados, e hal/, acquisition/,
coletor/ e estimador/ não importam este módulo.

Convenção de entrada: bearing = direção da âncora para o UE, em graus a partir do Norte
geográfico, no sentido horário (normalizado para [0, 360)). DL-AoD e UL-AoA podem usar outras
convenções (referência no eixo do painel de antenas, ângulo local, sentido UE -> âncora). Um
ângulo medido no UE em direção à âncora precisa de +180°; um UL-AoA só pode entrar como bearing
âncora -> UE depois de confirmar a convenção do equipamento.

Validações (auditoria G01-G04): lat/lon válidos; bearings finitos; âncoras distintas; semirretas,
nunca retas (t >= 0 e s >= 0, sem interseção atrás de uma âncora); ângulo de cruzamento de pelo
menos `min_angle_deg` (paralelas e quase paralelas são rejeitadas, porque o erro cresce com
1/sen do ângulo); solução a até `max_range_m` de cada âncora. O plano local é o ENU de
estimador/geometry.py com origem na âncora 1.
"""
import math
from typing import Tuple

from estimador.estimate import STATUS_INVALID_INPUT, STATUS_VALID, PositionEstimate
from estimador.geometry import EnuFrame, validate_lat_lon

METHOD = "bearing_intersection"
STATUS_GEOMETRY_DEGENERATE = "geometry_degenerate"
DEFAULT_MIN_ANGLE_DEG = 10.0
DEFAULT_MAX_RANGE_M = 5000.0
MIN_ANCHOR_SEPARATION_M = 1.0


def normalize_bearing(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"bearing inválido: {value!r}")
    return float(value) % 360.0


def intersect_bearings(anchor1: Tuple[float, float], bearing1_deg: float, anchor2: Tuple[float, float],
                       bearing2_deg: float, min_angle_deg: float = DEFAULT_MIN_ANGLE_DEG,
                       max_range_m: float = DEFAULT_MAX_RANGE_M) -> PositionEstimate:
    """Interseção das semirretas que partem de anchor1/anchor2 (lat, lon) nos bearings dados."""
    try:
        for anchor in (anchor1, anchor2):
            validate_lat_lon(*anchor)
        b1, b2 = normalize_bearing(bearing1_deg), normalize_bearing(bearing2_deg)
    except (TypeError, ValueError) as exc:
        return PositionEstimate(METHOD, STATUS_INVALID_INPUT, reason=str(exc))
    frame = EnuFrame(anchor1[0], anchor1[1])
    x2, y2, _ = frame.to_enu(anchor2[0], anchor2[1])
    if math.hypot(x2, y2) < MIN_ANCHOR_SEPARATION_M:
        return PositionEstimate(METHOD, STATUS_GEOMETRY_DEGENERATE, reason="âncoras iguais (a menos de 1 m)")
    d1x, d1y = math.sin(math.radians(b1)), math.cos(math.radians(b1))
    d2x, d2y = math.sin(math.radians(b2)), math.cos(math.radians(b2))
    cross = d1x * d2y - d1y * d2x  # seno do ângulo entre as direções
    if abs(cross) < math.sin(math.radians(min_angle_deg)):
        return PositionEstimate(METHOD, STATUS_GEOMETRY_DEGENERATE,
                                reason=f"semirretas paralelas ou quase paralelas (cruzamento abaixo de {min_angle_deg:g}°)")
    t = (x2 * d2y - y2 * d2x) / cross  # p1 + t*d1 = p2 + s*d2, com p1 na origem
    s = (x2 * d1y - y2 * d1x) / cross
    if t < 0 or s < 0:
        return PositionEstimate(METHOD, STATUS_GEOMETRY_DEGENERATE,
                                reason=f"interseção atrás da âncora {1 if t < 0 else 2} (as semirretas não se cruzam)")
    if max(t, s) > max_range_m:
        return PositionEstimate(METHOD, STATUS_GEOMETRY_DEGENERATE,
                                reason=f"solução a {max(t, s):.0f} m de uma âncora, além do limite de {max_range_m:g} m")
    x, y = t * d1x, t * d1y
    lat, lon, _ = frame.to_geodetic(x, y)
    angle = math.degrees(math.acos(max(-1.0, min(1.0, d1x * d2x + d1y * d2y))))
    return PositionEstimate(METHOD, STATUS_VALID, lat=lat, lon=lon, x_m=x, y_m=y, metadata={
        "experimental": True, "bearings_deg": [b1, b2], "intersection_angle_deg": angle,
        "angle_quality": abs(cross), "range_from_anchor1_m": t, "range_from_anchor2_m": s,
        "convention": "bearing âncora -> UE, graus a partir do Norte geográfico, sentido horário",
        "local_frame": "ENU com origem na âncora 1",
    })
