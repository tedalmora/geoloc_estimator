"""Cell-ID, o baseline principal: a posição estimada é a posição cadastrada da célula servidora.

A célula é identificada pela global_cell_key (RAT:PLMN:CELL_ID, equivalente ao ECGI/NCGI),
nunca pelo PCI isolado. Isto é Cell-ID, não E-CID: não usa TA, ângulo nem medidas de outras
células, e a posição devolvida é a da antena, não a do UE.

Cadastro (JSON, registry_version 2; exemplo em estimador/network_registry.example.json):
    {"registry_version": 2, "cells": [{"global_cell_key": "LTE:72406:F560E67", "lat": ..., "lon": ...}]}
Obrigatórios por célula: global_cell_key (canônica: hexadecimal maiúsculo sem zeros à esquerda),
lat e lon. Opcionais: sector_azimuth_deg [0, 360), sector_width_deg (0, 360], arfcn (>= 0),
pci (LTE 0-503, NR 0-1007) e description; azimute, ARFCN e PCI são só metadados de auditoria.
Campos desconhecidos e chaves repetidas são erro; chaves de topo iniciadas por "_" são comentários.

Estados: valid, unknown_cell, missing_global_cell_key e invalid_registry. Leitura que não serve
(snapshot inválido) vira `unavailable` antes de chegar aqui (estimate.unavailable_reading).
"""
import json
import math
import re
from dataclasses import dataclass, field
from typing import Dict, Optional

from hal.models import make_global_cell_key

from ..estimate import STATUS_VALID, PositionEstimate
from ..geometry import validate_lat_lon

METHOD = "cell_id"
REGISTRY_VERSION = 2
STATUS_UNKNOWN_CELL = "unknown_cell"
STATUS_MISSING_GLOBAL_CELL_KEY = "missing_global_cell_key"
STATUS_INVALID_REGISTRY = "invalid_registry"
_KEY_RE = re.compile(r"(LTE|NR):(\d{5,6}):([0-9A-F]+)")
_FIELDS = {"global_cell_key", "lat", "lon", "sector_azimuth_deg", "sector_width_deg", "arfcn", "pci", "description"}
_PCI_MAX = {"LTE": 503, "NR": 1007}


class RegistryError(ValueError):
    """Cadastro de células inválido (mensagem indica a entrada e o campo)."""


@dataclass(frozen=True)
class RegistryEntry:
    global_cell_key: str
    lat: float
    lon: float
    sector_azimuth_deg: Optional[float] = None
    sector_width_deg: Optional[float] = None
    arfcn: Optional[int] = None
    pci: Optional[int] = None
    description: Optional[str] = None


@dataclass
class CellRegistry:
    entries: Dict[str, RegistryEntry] = field(default_factory=dict)
    source: Optional[str] = None
    error: Optional[str] = None  # preenchido quando o arquivo não pôde ser usado

    @property
    def valid(self) -> bool:
        return self.error is None


def _number(item: dict, name: str, where: str, accept) -> Optional[float]:
    value = item.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not accept(value):
        raise RegistryError(f"{where}: {name} inválido: {value!r}")
    return float(value)


def _integer(item: dict, name: str, where: str, accept) -> Optional[int]:
    value = item.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not accept(value):
        raise RegistryError(f"{where}: {name} inválido: {value!r}")
    return value


def parse_registry(data) -> Dict[str, RegistryEntry]:
    if not isinstance(data, dict):
        raise RegistryError("o cadastro precisa ser um objeto JSON")
    if "cells" not in data and any(str(k).isdigit() for k in data):
        raise RegistryError("cadastro no formato antigo (chaves por PCI); use registry_version 2 com global_cell_key")
    if data.get("registry_version") != REGISTRY_VERSION:
        raise RegistryError(f"registry_version {data.get('registry_version')!r} não suportada (esperada {REGISTRY_VERSION})")
    extra = sorted(k for k in data if k not in ("registry_version", "cells") and not str(k).startswith("_"))
    if extra:
        raise RegistryError(f"campos de topo desconhecidos: {extra}")
    if not isinstance(data.get("cells"), list):
        raise RegistryError("'cells' precisa ser uma lista")
    entries: Dict[str, RegistryEntry] = {}
    for index, item in enumerate(data["cells"]):
        where = f"cells[{index}]"
        if not isinstance(item, dict):
            raise RegistryError(f"{where}: cada célula precisa ser um objeto")
        unknown = sorted(set(item) - _FIELDS)
        if unknown:
            raise RegistryError(f"{where}: campos desconhecidos {unknown}")
        key = item.get("global_cell_key")
        match = _KEY_RE.fullmatch(key) if isinstance(key, str) else None
        if match is None or make_global_cell_key(*match.groups()) != key:
            raise RegistryError(f"{where}: global_cell_key inválida {key!r}; use RAT:PLMN:CELL_ID com Cell-ID em "
                                "hexadecimal maiúsculo sem zeros à esquerda (ex.: LTE:72406:F560E67)")
        where = f"{where} ({key})"
        if key in entries:
            raise RegistryError(f"{where}: global_cell_key repetida")
        for name in ("lat", "lon"):
            if name not in item:
                raise RegistryError(f"{where}: campo obrigatório {name!r} ausente")
        try:
            validate_lat_lon(item["lat"], item["lon"])
        except ValueError as exc:
            raise RegistryError(f"{where}: {exc}") from exc
        rat = match.group(1)
        description = item.get("description")
        if description is not None and not isinstance(description, str):
            raise RegistryError(f"{where}: description precisa ser texto")
        entries[key] = RegistryEntry(
            key, float(item["lat"]), float(item["lon"]),
            _number(item, "sector_azimuth_deg", where, lambda v: 0 <= v < 360),
            _number(item, "sector_width_deg", where, lambda v: 0 < v <= 360),
            _integer(item, "arfcn", where, lambda v: v >= 0),
            _integer(item, "pci", where, lambda v: 0 <= v <= _PCI_MAX[rat]),
            description,
        )
    return entries


def load_registry(path: str) -> CellRegistry:
    """Carrega e valida o cadastro. Problemas no arquivo viram `error` (estado invalid_registry)."""
    try:
        with open(path, encoding="utf-8") as f:
            return CellRegistry(parse_registry(json.load(f)), path)
    except FileNotFoundError:
        return CellRegistry(source=path, error=f"{path}: cadastro não encontrado")
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return CellRegistry(source=path, error=f"{path}: JSON inválido ({exc})")
    except RegistryError as exc:
        return CellRegistry(source=path, error=f"{path}: {exc}")


def estimate_cell_id(registry: CellRegistry, global_cell_key: Optional[str]) -> PositionEstimate:
    if not registry.valid:
        return PositionEstimate(METHOD, STATUS_INVALID_REGISTRY, reason=registry.error)
    if not global_cell_key:
        return PositionEstimate(METHOD, STATUS_MISSING_GLOBAL_CELL_KEY,
                                reason="a servidora da leitura não tem global_cell_key (PLMN ou Cell-ID ausente)")
    technology = global_cell_key.split(":", 1)[0]
    entry = registry.entries.get(global_cell_key)
    if entry is None:
        return PositionEstimate(METHOD, STATUS_UNKNOWN_CELL, reason=f"{global_cell_key} não está no cadastro",
                                technology=technology, metadata={"global_cell_key": global_cell_key})
    return PositionEstimate(
        METHOD, STATUS_VALID, lat=entry.lat, lon=entry.lon, technology=technology,
        metadata={"global_cell_key": global_cell_key, "position": "posição cadastrada da célula (não do UE)",
                  "sector_azimuth_deg": entry.sector_azimuth_deg, "description": entry.description},
    )
