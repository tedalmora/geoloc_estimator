"""Cell-ID + Timing Advance (LEGADO, revisto na fase 7). O Cell-ID principal está em methods/cell_id.py.

A distância do TA é projetada a partir da posição cadastrada da célula servidora (na direção do
azimute do setor, se conhecido; senão o TA só define um raio de incerteza). O modem atual não
expõe TA (o `live.py` passa sempre None), então hoje o resultado coincide com o Cell-ID.
`live.py` monta o dicionário {global_cell_key: CellPosition} a partir do cadastro validado.
A constante por RAT e o modelo geométrico são pendências da fase 7 (auditoria T01-T03).
"""
import math
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

# metros por unidade de Timing Advance -- valor padrão da literatura (78,12 m/passo em LTE);
# em 5G NR varia com a numerologia (subcarrier spacing) -- PRECISA ser confirmado em campo
# (comparar a distância estimada contra pontos de distância real conhecida) antes de confiar
# no número absoluto, mesmo processo já usado para os outros comandos do HAL.
METROS_POR_TA = {"4G": 78.12, "5G": 78.12}


@dataclass
class CellPosition:
    lat: float
    lon: float
    azimute_graus: Optional[float] = None  # None = célula omnidirecional ou azimute ainda desconhecido


def _projetar_ponto(lat: float, lon: float, azimute_graus: float, distancia_m: float) -> Tuple[float, float]:
    """Desloca (lat, lon) por `distancia_m` na direção `azimute_graus` (0=Norte, sentido horário).
    Aproximação de plano local -- válida para as distâncias curtas deste projeto (poucos km),
    mesma técnica já usada em methods/aoa.py."""
    R = 6371000.0
    dx = distancia_m * math.sin(math.radians(azimute_graus))
    dy = distancia_m * math.cos(math.radians(azimute_graus))
    nova_lat = lat + math.degrees(dy / R)
    nova_lon = lon + math.degrees(dx / (R * math.cos(math.radians(lat))))
    return nova_lat, nova_lon


def estimate_cell_position_ta(
    serving_key,
    timing_advance: Optional[int],
    technology: str,
    registry: Dict[object, CellPosition],
) -> Optional[dict]:
    """Cell-ID + Timing Advance -- SEM aprendizado de máquina. `serving_key` é a chave da
    célula servidora no `registry` (global_cell_key no live.py).

    Usa a posição cadastrada da célula + a distância derivada do TA. Se o azimute do setor for
    conhecido, desloca o ponto nessa direção. Se não for conhecido (célula omnidirecional ou
    azimute ainda não cadastrado), devolve a MESMA posição do Cell-ID -- nesse caso o TA não
    move o ponto, só informa um raio de incerteza.
    """
    if serving_key is None or serving_key not in registry:
        return None
    torre = registry[serving_key]
    distancia_m = (timing_advance or 0) * METROS_POR_TA.get(technology, 78.12)
    if torre.azimute_graus is not None:
        lat, lon = _projetar_ponto(torre.lat, torre.lon, torre.azimute_graus, distancia_m)
        return {"lat": lat, "lon": lon, "raio_incerteza_m": 0.0}
    return {"lat": torre.lat, "lon": torre.lon, "raio_incerteza_m": distancia_m}
