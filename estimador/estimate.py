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

    @property
    def valid(self) -> bool:
        return self.status == STATUS_VALID
