"""Métodos: Cell-ID/E-CID (lado do UE ou da rede) e Cell-ID + Timing Advance.

Unificados neste único arquivo porque são, no fundo, o MESMO cálculo: procurar a posição já
CADASTRADA da célula servidora (`estimate_cell_position`), e opcionalmente refinar com o
Timing Advance (`estimate_cell_position_ta`). Nenhuma função aqui usa aprendizado de máquina
-- o único método do projeto que usa ML é o Fingerprint (methods/fingerprint.py).

Isso só é possível porque o cadastro (`network_registry.json`) traz a posição real de cada
célula (você vai instalar/posicionar as antenas) -- sem isso, TA sozinho só dá um RAIO em
torno da torre, não um PONTO (por isso, sem controle da rede, isso teria que ser aprendido
empiricamente do radiomapa -- ver planejamento/arquitetura_estimadores_posicao.md).

**Cell-ID/E-CID (lado do UE) e E-CID de rede usam a MESMA função `estimate_cell_position()`**
-- a única diferença entre os dois é de onde vem o `serving_pci`: do HAL (AT, lado do UE, via
`get_serving_cell()`) ou do core (lado da rede -- integração ainda não implementada; é só
trocar a fonte do `serving_pci` quando isso existir). Ver estimador/README.md.
"""
import json
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


def load_registry(path: str) -> Dict[int, CellPosition]:
    """Lê o cadastro de posições conhecidas (JSON) -- 1 entrada por PCI/cell id.
    Ver network_registry.example.json para o formato e instruções de preenchimento.
    """
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    registry: Dict[int, CellPosition] = {}
    for pci, valores in raw.items():
        if pci.startswith("_"):
            continue  # chaves começando com "_" são comentários/instruções, não células
        registry[int(pci)] = CellPosition(
            lat=valores["lat"], lon=valores["lon"], azimute_graus=valores.get("azimute_graus")
        )
    return registry


def estimate_cell_position(
    serving_pci: Optional[int], registry: Dict[int, CellPosition]
) -> Optional[Tuple[float, float]]:
    """Método: Cell-ID/E-CID. Usado tanto para o lado do UE (serving_pci vindo do HAL) quanto
    para o E-CID de rede (serving_pci vindo do core) -- mesma função, fonte de dado diferente."""
    if serving_pci is None or serving_pci not in registry:
        return None  # célula fora do cadastro -- sem estimativa possível
    torre = registry[serving_pci]
    return torre.lat, torre.lon


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
    serving_pci: Optional[int],
    timing_advance: Optional[int],
    technology: str,
    registry: Dict[int, CellPosition],
) -> Optional[dict]:
    """Método: Cell-ID + Timing Advance -- SEM aprendizado de máquina.

    Usa a posição cadastrada da célula + a distância derivada do TA. Se o azimute do setor for
    conhecido, desloca o ponto nessa direção (estimativa mais precisa que o Cell-ID puro). Se
    não for conhecido (célula omnidirecional, ex. o QCell indoor, ou azimute ainda não
    cadastrado), devolve a MESMA posição do Cell-ID puro -- nesse caso o TA não move o ponto,
    só informa um raio de incerteza (o TA melhora o que você sabe sobre o erro, não a posição).

    Devolve um dict (não só uma tupla) porque o raio de incerteza é informação relevante por
    si só -- ver estimador/README.md.
    """
    if serving_pci is None or serving_pci not in registry:
        return None
    torre = registry[serving_pci]
    distancia_m = (timing_advance or 0) * METROS_POR_TA.get(technology, 78.12)
    if torre.azimute_graus is not None:
        lat, lon = _projetar_ponto(torre.lat, torre.lon, torre.azimute_graus, distancia_m)
        return {"lat": lat, "lon": lon, "raio_incerteza_m": 0.0}
    return {"lat": torre.lat, "lon": torre.lon, "raio_incerteza_m": distancia_m}
