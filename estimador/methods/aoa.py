"""Método: DL-AoD/UL-AoA -- estimativa por interseção de 2 retas de rolamento (bearing).

Diferente dos outros métodos (network_position.py, fingerprint.py), este NÃO usa aprendizado
de máquina nem o radiomapa: cada âncora (célula/ponto de RF com posição conhecida -- QCell,
gNodeB) mede um ângulo (bearing) até o UE, e 2 ângulos de 2 âncoras diferentes se cruzam em
exatamente 1 ponto -- geometria pura, sem precisar de treino.

IMPORTANTE (ver planejamento/arquitetura_hal.md e arquitetura_estimadores_posicao.md,
Seção 8): quem MEDE o ângulo é a REDE, via LPP/DL-AoD ou UL-AoA -- não este código. O HAL
só habilita a sessão e tenta ler o relatório bruto (`enable_lpp_positioning`/
`get_lpp_report`), e o formato real desse relatório ainda não foi validado em campo
(precisa de SIM + core). Este módulo assume que você já extraiu os 2 ângulos (em graus,
0-360, sentido horário a partir do Norte) de algum jeito -- manualmente do log bruto, por
enquanto -- e só faz a conta geométrica final. Por isso ele já funciona e pode ser testado
hoje com valores de exemplo, mesmo sem rede/SIM.
"""
import math
from typing import Tuple

EARTH_RADIUS_M = 6371000.0


def _to_local_plane(lat: float, lon: float, ref_lat: float, ref_lon: float) -> Tuple[float, float]:
    """Aproxima lat/lon como um plano local (x=leste, y=norte) em metros -- válido para
    distâncias pequenas (poucos km, como as do campus) e evita a matemática mais pesada de
    geodésia esférica completa, que não compensa nessa escala."""
    x = math.radians(lon - ref_lon) * EARTH_RADIUS_M * math.cos(math.radians(ref_lat))
    y = math.radians(lat - ref_lat) * EARTH_RADIUS_M
    return x, y


def _from_local_plane(x: float, y: float, ref_lat: float, ref_lon: float) -> Tuple[float, float]:
    lat = ref_lat + math.degrees(y / EARTH_RADIUS_M)
    lon = ref_lon + math.degrees(x / (EARTH_RADIUS_M * math.cos(math.radians(ref_lat))))
    return lat, lon


def estimate_position_from_bearings(
    anchor1: Tuple[float, float], bearing1_deg: float,
    anchor2: Tuple[float, float], bearing2_deg: float,
) -> Tuple[float, float]:
    """anchor1/anchor2: (lat, lon) das 2 âncoras com posição conhecida (QCell, gNodeB...).
    bearing_deg: ângulo medido pela rede, em graus, 0 = Norte, sentido horário.
    Devolve (lat, lon) do ponto onde as 2 retas de rolamento se cruzam.
    """
    ref_lat, ref_lon = anchor1
    x1, y1 = _to_local_plane(anchor1[0], anchor1[1], ref_lat, ref_lon)
    x2, y2 = _to_local_plane(anchor2[0], anchor2[1], ref_lat, ref_lon)

    # bearing -> vetor unitário de direção (bearing é medido a partir do Norte, sentido horário)
    d1x, d1y = math.sin(math.radians(bearing1_deg)), math.cos(math.radians(bearing1_deg))
    d2x, d2y = math.sin(math.radians(bearing2_deg)), math.cos(math.radians(bearing2_deg))

    # interseção de 2 retas paramétricas: p1 + t*d1 = p2 + s*d2
    denom = d1x * d2y - d1y * d2x
    if abs(denom) < 1e-9:
        raise ValueError("As 2 retas de rolamento são paralelas -- não há interseção única.")
    t = ((x2 - x1) * d2y - (y2 - y1) * d2x) / denom
    x, y = x1 + t * d1x, y1 + t * d1y

    return _from_local_plane(x, y, ref_lat, ref_lon)


def anchors_from_registry(pci1: int, pci2: int, registry: dict) -> tuple:
    """Busca as coordenadas de 2 células já cadastradas (methods/network_position.py) em vez
    de digitar lat/lon na mão -- uso opcional, complementar a estimate_position_from_bearings().
    Os 2 ângulos (bearings) continuam precisando vir de fora (do relatório LPP já extraído)."""
    torre1, torre2 = registry[pci1], registry[pci2]
    return (torre1.lat, torre1.lon), (torre2.lat, torre2.lon)
