"""Resultado comum dos métodos de posição: estado explícito em vez de None genérico."""
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

STATUS_VALID = "valid"
STATUS_UNAVAILABLE = "unavailable"
STATUS_TECHNOLOGY_MISMATCH = "technology_mismatch"
STATUS_INVALID_INPUT = "invalid_input"


@dataclass
class PositionEstimate:
    method: str
    status: str
    reason: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    x_m: Optional[float] = None  # ENU do bundle, quando o método usa plano local
    y_m: Optional[float] = None
    technology: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    snapshot_status: Optional[str] = None  # complete/partial/invalid, quando vem de uma leitura do modem

    @property
    def valid(self) -> bool:
        return self.status == STATUS_VALID

    def to_dict(self) -> dict:
        return {"method": self.method, "valid": self.valid, "status": self.status, "reason": self.reason,
                "x_m": self.x_m, "y_m": self.y_m, "lat": self.lat, "lon": self.lon, "technology": self.technology,
                "snapshot_status": self.snapshot_status, "metadata": self.metadata}


def unavailable_reading(method: str, snapshot_status: str, csurv_status: Optional[str] = None,
                        technology: Optional[str] = None, detail: str = "") -> Optional[PositionEstimate]:
    """`unavailable` quando a leitura não serve ao método; senão None. Mesma regra no live.py e no evaluate.

    Snapshot inválido não gera estimativa; o WKNN ("wknn") também exige o #CSURV lido.
    """
    if snapshot_status == "invalid":
        return PositionEstimate(method, STATUS_UNAVAILABLE, technology=technology,
                                reason="snapshot inválido" + (f": {detail}" if detail else ""))
    if method == "wknn" and csurv_status != "ok":
        return PositionEstimate(method, STATUS_UNAVAILABLE, technology=technology,
                                reason=f"#CSURV sem dado nesta leitura (csurv:{csurv_status}); o WKNN usa só células do #CSURV")
    return None
