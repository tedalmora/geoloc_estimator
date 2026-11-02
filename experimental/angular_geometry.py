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

`estimate_from_observations` e a CLI exigem a convenção angular explícita (hoje só `north-clockwise`)
e guardam o tipo original (DL-AoD, UL-AoA, external_bearing) apenas como metadado: nenhuma conversão
de orientação é feita aqui, o chamador entrega os bearings já convertidos. O ângulo de cruzamento e
`angle_quality` são indicadores geométricos, não o erro real de posicionamento.

Uso: python -m experimental.angular_geometry --help (só calcula; sem serial, AT, banco ou arquivos).
"""
import argparse
import json
import math
from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

from estimador.estimate import (STATUS_INVALID_INPUT, STATUS_UNAVAILABLE, STATUS_VALID, PositionEstimate)
from estimador.geometry import EnuFrame, validate_lat_lon

METHOD = "bearing_intersection"
STATUS_GEOMETRY_DEGENERATE = "geometry_degenerate"
STATUS_OUT_OF_RANGE = "out_of_range"
DEFAULT_MIN_ANGLE_DEG = 10.0
DEFAULT_MAX_RANGE_M = 5000.0
MIN_ANCHOR_SEPARATION_M = 1.0

CONVENTION_NORTH_CLOCKWISE = "north-clockwise"
SOURCE_TYPES = {"dl-aod": "DL-AoD", "ul-aoa": "UL-AoA", "external_bearing": "external_bearing"}
DEFAULT_SOURCE_TYPE = "external_bearing"
GEOMETRY_NOTE = ("Indicador geométrico (ângulo de cruzamento, distâncias às âncoras); não representa o erro real "
                 "de posicionamento. Os bearings vêm de fonte externa: o FN990 não fornece DL-AoD nem UL-AoA aqui.")

STATUS_SUCCESS = "success"
STATUS_CLI_DEGENERATE = "degenerate_geometry"
EXIT_CODES = {STATUS_SUCCESS: 0, STATUS_INVALID_INPUT: 2, STATUS_UNAVAILABLE: 3, STATUS_CLI_DEGENERATE: 4,
              STATUS_OUT_OF_RANGE: 5}


@dataclass(frozen=True)
class BearingObservation:
    """Âncora (lat, lon) com bearing já na convenção `north-clockwise`; source_type é só metadado."""
    lat: float
    lon: float
    bearing_deg: float
    source_type: str = DEFAULT_SOURCE_TYPE


def normalize_bearing(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"bearing inválido: {value!r}")
    return float(value) % 360.0


def _positive_finite(name: str, value, upper: Optional[float] = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0 \
            or (upper is not None and value > upper):
        raise ValueError(f"{name} inválido: {value!r}")
    return float(value)


def intersect_bearings(anchor1: Tuple[float, float], bearing1_deg: float, anchor2: Tuple[float, float],
                       bearing2_deg: float, min_angle_deg: float = DEFAULT_MIN_ANGLE_DEG,
                       max_range_m: float = DEFAULT_MAX_RANGE_M) -> PositionEstimate:
    """Interseção das semirretas que partem de anchor1/anchor2 (lat, lon) nos bearings dados."""
    try:
        for anchor in (anchor1, anchor2):
            validate_lat_lon(*anchor)
        b1, b2 = normalize_bearing(bearing1_deg), normalize_bearing(bearing2_deg)
        _positive_finite("ângulo mínimo de cruzamento (graus, 0-90)", min_angle_deg, 90.0)
        _positive_finite("alcance máximo (m)", max_range_m)
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
        return PositionEstimate(METHOD, STATUS_OUT_OF_RANGE,
                                reason=f"solução a {max(t, s):.0f} m de uma âncora, além do limite de {max_range_m:g} m")
    x, y = t * d1x, t * d1y
    lat, lon, _ = frame.to_geodetic(x, y)
    angle = math.degrees(math.acos(max(-1.0, min(1.0, d1x * d2x + d1y * d2y))))
    return PositionEstimate(METHOD, STATUS_VALID, lat=lat, lon=lon, x_m=x, y_m=y, metadata={
        "experimental": True, "bearings_deg": [b1, b2], "intersection_angle_deg": angle,
        "angle_quality": abs(cross), "range_from_anchor1_m": t, "range_from_anchor2_m": s,
        "convention": "bearing âncora -> UE, graus a partir do Norte geográfico, sentido horário",
        "local_frame": "ENU com origem na âncora 1", "quality_note": GEOMETRY_NOTE,
    })


def estimate_from_observations(obs1: Optional[BearingObservation], obs2: Optional[BearingObservation],
                               convention: Optional[str], min_angle_deg: float = DEFAULT_MIN_ANGLE_DEG,
                               max_range_m: float = DEFAULT_MAX_RANGE_M) -> PositionEstimate:
    """Interseção de dois bearings externos, com convenção explícita e tipo original preservado como metadado."""
    if obs1 is None or obs2 is None:
        return PositionEstimate(METHOD, STATUS_UNAVAILABLE, reason="são necessárias duas âncoras com bearing")
    if convention != CONVENTION_NORTH_CLOCKWISE:
        return PositionEstimate(METHOD, STATUS_INVALID_INPUT, reason=(
            f"convenção angular {convention!r} não suportada: informe {CONVENTION_NORTH_CLOCKWISE!r} "
            "(bearing já convertido pelo chamador)"))
    types = []
    for obs in (obs1, obs2):
        source = SOURCE_TYPES.get(obs.source_type.lower()) if isinstance(obs.source_type, str) else None
        if source is None:
            return PositionEstimate(METHOD, STATUS_INVALID_INPUT,
                                    reason=f"tipo de bearing inválido: {obs.source_type!r} (use {', '.join(SOURCE_TYPES.values())})")
        types.append(source)
    result = intersect_bearings((obs1.lat, obs1.lon), obs1.bearing_deg, (obs2.lat, obs2.lon), obs2.bearing_deg,
                                min_angle_deg, max_range_m)
    result.metadata.update({"angular_convention": convention, "bearing_source_types": types,
                            "orientation_conversion": "nenhuma: bearings recebidos já convertidos"})
    return result


def cli_result(estimate: PositionEstimate) -> dict:
    """Resultado da CLI: success/unavailable/invalid_input/degenerate_geometry/out_of_range (original em estimate_status)."""
    status = {STATUS_VALID: STATUS_SUCCESS, STATUS_UNAVAILABLE: STATUS_UNAVAILABLE,
              STATUS_GEOMETRY_DEGENERATE: STATUS_CLI_DEGENERATE,
              STATUS_OUT_OF_RANGE: STATUS_OUT_OF_RANGE}.get(estimate.status, STATUS_INVALID_INPUT)
    return {**estimate.to_dict(), "status": status, "estimate_status": estimate.status, "note": GEOMETRY_NOTE}


def _to_float(text: Optional[str]) -> Optional[float]:
    try:
        return float(text)
    except (TypeError, ValueError):
        return text  # texto inválido segue adiante e vira invalid_input na validação


def _cli_estimate(args: argparse.Namespace) -> PositionEstimate:
    fields = {"anchor-a-lat": args.anchor_a_lat, "anchor-a-lon": args.anchor_a_lon, "bearing-a": args.bearing_a,
              "anchor-b-lat": args.anchor_b_lat, "anchor-b-lon": args.anchor_b_lon, "bearing-b": args.bearing_b}
    missing = [name for name, value in fields.items() if value is None]
    if missing:
        return PositionEstimate(METHOD, STATUS_UNAVAILABLE, reason="argumentos ausentes: " + ", ".join("--" + m for m in missing))
    a = BearingObservation(_to_float(args.anchor_a_lat), _to_float(args.anchor_a_lon), _to_float(args.bearing_a), args.type_a)
    b = BearingObservation(_to_float(args.anchor_b_lat), _to_float(args.anchor_b_lon), _to_float(args.bearing_b), args.type_b)
    return estimate_from_observations(a, b, args.convention, _to_float(args.minimum_crossing_angle),
                                      _to_float(args.maximum_range_m))


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Posição a partir de dois bearings externos já convertidos (experimental). Só calcula; "
                    "não usa serial, AT, banco nem arquivos. Códigos de saída: 0 success, 2 invalid_input, "
                    "3 unavailable, 4 degenerate_geometry, 5 out_of_range.")
    for letter in ("a", "b"):
        parser.add_argument(f"--anchor-{letter}-lat", help=f"latitude da âncora {letter.upper()}")
        parser.add_argument(f"--anchor-{letter}-lon", help=f"longitude da âncora {letter.upper()}")
        parser.add_argument(f"--bearing-{letter}", help=f"bearing da âncora {letter.upper()} para o UE, em graus")
        parser.add_argument(f"--type-{letter}", default=DEFAULT_SOURCE_TYPE,
                            help="tipo original do bearing, só metadado: DL-AoD, UL-AoA ou external_bearing (padrão)")
    parser.add_argument("--convention",
                        help=f"convenção angular dos bearings; obrigatória, aceita {CONVENTION_NORTH_CLOCKWISE} "
                             "(graus, sentido horário a partir do Norte geográfico, âncora -> UE)")
    parser.add_argument("--minimum-crossing-angle", default=DEFAULT_MIN_ANGLE_DEG,
                        help=f"ângulo mínimo de cruzamento em graus (padrão {DEFAULT_MIN_ANGLE_DEG:g})")
    parser.add_argument("--maximum-range-m", default=DEFAULT_MAX_RANGE_M,
                        help=f"alcance máximo por âncora em metros (padrão {DEFAULT_MAX_RANGE_M:g})")
    result = cli_result(_cli_estimate(parser.parse_args(argv)))
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return EXIT_CODES[result["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
