"""Timing Advance (EXPERIMENTAL, não validado): faixa de distância até a antena da servidora.

Fora do caminho principal: hal/, acquisition/, coletor/ e estimador/ não importam este módulo.
O FN990 com o firmware em uso (M0R.000003) não expõe TA em comando AT documentado
(planejamento/procedimento_ta_sinr_fn990.md); sem TA real, o resultado correto é `unavailable`.

Regras (auditoria T01-T03):
- TA ausente (None) nunca vira zero: `unavailable`;
- não há constante embutida de metros por passo. Em LTE o comando de TA vem em múltiplos de
  16·Ts (3GPP TS 36.213 §4.2.3); em NR a granularidade depende da numerologia (TS 38.213 §4.2).
  Como não se sabe em que unidade o modem entregaria o valor, a conversão só existe se o
  chamador declarar uma `TaConversion` (tecnologia, unidade, metros por passo e a fonte que a
  confirma). Sem ela, o resultado é `unsupported`;
- o resultado é uma faixa de distância (range_min_m a range_max_m) em torno da antena
  cadastrada. O azimute do setor não é usado como direção do UE: lat/lon é a posição da antena
  (a mesma do Cell-ID), nunca um ponto projetado, e range_max_m é sempre maior que zero.
"""
import math
from dataclasses import asdict, dataclass
from typing import Optional

from estimador.estimate import (STATUS_INVALID_INPUT, STATUS_TECHNOLOGY_MISMATCH, STATUS_UNAVAILABLE, STATUS_VALID,
                                PositionEstimate)
from estimador.features import TECHNOLOGIES
from estimador.methods.cell_id import CellRegistry, estimate_cell_id

METHOD = "timing_advance"
STATUS_UNSUPPORTED = "unsupported"
HYPOTHESIS = ("UE em algum ponto do anel centrado na antena cadastrada, direção desconhecida; "
              "faixa = (TA ± 0,5) passo, supondo TA arredondado ao passo mais próximo")


@dataclass(frozen=True)
class TaConversion:
    """Conversão TA -> distância declarada pelo usuário, com a fonte que a confirma."""
    technology: str
    unit: str
    meters_per_step: float
    source: str

    def __post_init__(self):
        if self.technology not in TECHNOLOGIES:
            raise ValueError(f"tecnologia inválida: {self.technology!r}")
        step = self.meters_per_step
        if isinstance(step, bool) or not isinstance(step, (int, float)) or not math.isfinite(step) or step <= 0:
            raise ValueError(f"metros por passo inválido: {step!r}")
        if not self.unit.strip() or not self.source.strip():
            raise ValueError("informe a unidade do TA e a fonte que confirma a conversão")


def estimate_timing_advance(ta: Optional[int], technology: str, global_cell_key: Optional[str],
                            registry: CellRegistry, conversion: Optional[TaConversion] = None) -> PositionEstimate:
    if technology not in TECHNOLOGIES:
        return PositionEstimate(METHOD, STATUS_INVALID_INPUT, reason=f"tecnologia inválida: {technology!r}")
    if ta is None:
        return PositionEstimate(METHOD, STATUS_UNAVAILABLE, technology=technology,
                                reason="TA não informado (o FN990 em uso não expõe TA)")
    if isinstance(ta, bool) or not isinstance(ta, int) or ta < 0:
        return PositionEstimate(METHOD, STATUS_INVALID_INPUT, technology=technology, reason=f"TA inválido: {ta!r}")
    if conversion is None:
        return PositionEstimate(METHOD, STATUS_UNSUPPORTED, technology=technology,
                                reason=f"conversão de TA para distância não confirmada para {technology}")
    if conversion.technology != technology:
        return PositionEstimate(METHOD, STATUS_TECHNOLOGY_MISMATCH, technology=technology,
                                reason=f"a conversão declarada é de {conversion.technology}, não de {technology}")
    cell = estimate_cell_id(registry, global_cell_key)
    if not cell.valid:
        return PositionEstimate(METHOD, cell.status, reason=cell.reason, technology=technology)
    step = float(conversion.meters_per_step)
    entry = registry.entries[global_cell_key]
    return PositionEstimate(METHOD, STATUS_VALID, lat=cell.lat, lon=cell.lon, technology=technology, metadata={
        "experimental": True, "position": "antena cadastrada (centro do anel), não o UE", "ta": ta,
        "estimated_range_m": ta * step, "range_min_m": max(0.0, (ta - 0.5) * step), "range_max_m": (ta + 0.5) * step,
        "hypothesis": HYPOTHESIS, "conversion": asdict(conversion),
        "sector_azimuth_deg": entry.sector_azimuth_deg, "sector_width_deg": entry.sector_width_deg,
    })
