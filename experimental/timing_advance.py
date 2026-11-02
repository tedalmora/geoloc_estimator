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

Uma única faixa radial não determina posição 2D; ela só restringe o UE a um anel em torno da antena.
A posição da célula vem do cadastro Cell-ID (somente leitura) ou é passada em `cell_position`.

CLI (só calcula e imprime JSON; não abre a serial, não envia AT, não grava em banco nem em arquivo):
    python -m experimental.timing_advance --technology LTE --ta 3 --unit <unidade> \
        --meters-per-step <m> --conversion-source <fonte> --cell-lat <lat> --cell-lon <lon>
"""
import argparse
import json
import math
import re
from dataclasses import asdict, dataclass
from typing import Optional, Sequence, Tuple

from estimador.estimate import (STATUS_INVALID_INPUT, STATUS_TECHNOLOGY_MISMATCH, STATUS_UNAVAILABLE, STATUS_VALID,
                                PositionEstimate)
from estimador.features import TECHNOLOGIES
from estimador.geometry import validate_lat_lon
from estimador.methods.cell_id import CellRegistry, estimate_cell_id, load_registry

METHOD = "timing_advance"
STATUS_UNSUPPORTED = "unsupported"
STATUS_SUCCESS = "success"  # nome do estado `valid` na saída da CLI
DEFAULT_UNCERTAINTY_STEPS = 0.5
HYPOTHESIS = ("UE em algum ponto do anel centrado na antena, direção desconhecida; "
              "faixa = (TA ± incerteza) passos (padrão 0,5, supondo TA arredondado ao passo mais próximo)")
LIMITATION = ("uma única faixa radial não determina posição 2D: lat/lon é a antena (centro do anel), não o UE; "
              "localizar o UE exige outra medida independente")


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
                            registry: Optional[CellRegistry], conversion: Optional[TaConversion] = None, *,
                            cell_position: Optional[Tuple[float, float]] = None,
                            uncertainty_steps: float = DEFAULT_UNCERTAINTY_STEPS) -> PositionEstimate:
    """Faixa radial a partir de um TA externo. A antena vem de `cell_position` (lat, lon) ou do `registry`."""
    if technology not in TECHNOLOGIES:
        return PositionEstimate(METHOD, STATUS_INVALID_INPUT, reason=f"tecnologia inválida: {technology!r}")
    if ta is None:
        return PositionEstimate(METHOD, STATUS_UNAVAILABLE, technology=technology,
                                reason="TA não informado (o FN990 em uso não expõe TA)")
    if isinstance(ta, bool) or not isinstance(ta, int) or ta < 0:
        return PositionEstimate(METHOD, STATUS_INVALID_INPUT, technology=technology, reason=f"TA inválido: {ta!r}")
    u = uncertainty_steps
    if isinstance(u, bool) or not isinstance(u, (int, float)) or not math.isfinite(u) or u <= 0:
        return PositionEstimate(METHOD, STATUS_INVALID_INPUT, technology=technology,
                                reason=f"incerteza inválida (passos, finita e > 0): {u!r}")
    if cell_position is not None and registry is not None:
        return PositionEstimate(METHOD, STATUS_INVALID_INPUT, technology=technology,
                                reason="informe a posição da célula ou o cadastro, não os dois")
    if cell_position is None and registry is None:
        return PositionEstimate(METHOD, STATUS_INVALID_INPUT, technology=technology,
                                reason="informe a posição da célula ou o cadastro Cell-ID")
    if cell_position is not None:
        try:
            validate_lat_lon(*cell_position)
        except (TypeError, ValueError) as exc:
            return PositionEstimate(METHOD, STATUS_INVALID_INPUT, technology=technology,
                                    reason=f"posição da célula inválida: {exc}")
    if conversion is None:
        return PositionEstimate(METHOD, STATUS_UNSUPPORTED, technology=technology,
                                reason=f"conversão de TA para distância não confirmada para {technology}")
    if conversion.technology != technology:
        return PositionEstimate(METHOD, STATUS_TECHNOLOGY_MISMATCH, technology=technology,
                                reason=f"a conversão declarada é de {conversion.technology}, não de {technology}")
    sector = {}
    if cell_position is not None:
        lat, lon = float(cell_position[0]), float(cell_position[1])
        source = "cell_position"
    else:
        cell = estimate_cell_id(registry, global_cell_key)
        if not cell.valid:
            return PositionEstimate(METHOD, cell.status, reason=cell.reason, technology=technology)
        entry = registry.entries[global_cell_key]
        lat, lon, source = cell.lat, cell.lon, "registry"
        sector = {"sector_azimuth_deg": entry.sector_azimuth_deg, "sector_width_deg": entry.sector_width_deg}
    step = float(conversion.meters_per_step)
    return PositionEstimate(METHOD, STATUS_VALID, lat=lat, lon=lon, technology=technology, metadata={
        "experimental": True, "position": "antena da célula (centro do anel), não o UE", "position_source": source,
        "ta": ta, "estimated_range_m": ta * step, "range_min_m": max(0.0, (ta - u) * step),
        "range_max_m": (ta + u) * step, "uncertainty_steps": float(u), "hypothesis": HYPOTHESIS,
        "limitation": LIMITATION, "conversion": asdict(conversion), **sector,
    })


# --------------------------------------------------------------------------- CLI
def _parse_int(text: Optional[str], name: str) -> Optional[int]:
    if text is None:
        return None
    if re.fullmatch(r"[+-]?\d+", text.strip()) is None:
        raise ValueError(f"{name} precisa ser inteiro: {text!r}")
    return int(text)


def _parse_float(text: Optional[str], name: str) -> Optional[float]:
    if text is None:
        return None
    try:
        return float(text)
    except ValueError:
        raise ValueError(f"{name} precisa ser numérico: {text!r}") from None


def _cli_estimate(args) -> PositionEstimate:
    technology = args.technology
    try:
        ta = _parse_int(args.ta, "--ta")
        step = _parse_float(args.meters_per_step, "--meters-per-step")
        uncertainty = _parse_float(args.uncertainty_steps, "--uncertainty-steps")
        lat, lon = _parse_float(args.cell_lat, "--cell-lat"), _parse_float(args.cell_lon, "--cell-lon")
        if (lat is None) != (lon is None):
            raise ValueError("--cell-lat e --cell-lon devem ser informados juntos")
        if args.registry and lat is not None:
            raise ValueError("use --cell-lat/--cell-lon ou --registry, não os dois")
        conversion = None
        if any(v is not None for v in (args.unit, args.meters_per_step, args.conversion_source)):
            if step is None:
                raise ValueError("a conversão exige --meters-per-step explícito")
            conversion = TaConversion(args.conversion_technology or technology, args.unit or "", step,
                                      args.conversion_source or "")
    except ValueError as exc:
        return PositionEstimate(METHOD, STATUS_INVALID_INPUT, technology=technology, reason=str(exc))
    registry = load_registry(args.registry) if args.registry else None
    position = (lat, lon) if lat is not None else None
    kwargs = {} if uncertainty is None else {"uncertainty_steps": uncertainty}
    return estimate_timing_advance(ta, technology, args.global_cell_key, registry, conversion,
                                   cell_position=position, **kwargs)


def cli_result(estimate: PositionEstimate) -> dict:
    """Resultado da CLI: status em success/unavailable/unsupported/invalid_input (o original fica em estimate_status)."""
    status = {STATUS_VALID: STATUS_SUCCESS, STATUS_UNAVAILABLE: STATUS_UNAVAILABLE,
              STATUS_UNSUPPORTED: STATUS_UNSUPPORTED}.get(estimate.status, STATUS_INVALID_INPUT)
    return {**estimate.to_dict(), "status": status, "estimate_status": estimate.status, "note": LIMITATION}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Faixa radial a partir de um TA externo (experimental). Só calcula; não usa serial, AT, banco nem arquivos.")
    parser.add_argument("--technology", required=True, help="LTE ou NR")
    parser.add_argument("--ta", help="valor de TA recebido de fonte externa (0 é válido; omitir = indisponível)")
    parser.add_argument("--unit", help="unidade do TA (ex.: o passo do procedimento usado)")
    parser.add_argument("--meters-per-step", help="fator de conversão explícito, metros por passo de TA")
    parser.add_argument("--conversion-source", help="fonte que confirma o fator (norma, manual, medição)")
    parser.add_argument("--conversion-technology", help="tecnologia da conversão (padrão: --technology)")
    parser.add_argument("--uncertainty-steps", help=f"incerteza em passos de TA (padrão {DEFAULT_UNCERTAINTY_STEPS})")
    parser.add_argument("--cell-lat", help="latitude da antena")
    parser.add_argument("--cell-lon", help="longitude da antena")
    parser.add_argument("--registry", help="cadastro Cell-ID (JSON), lido sem alteração")
    parser.add_argument("--global-cell-key", help="célula no cadastro, ex.: LTE:72406:F560E67")
    result = cli_result(_cli_estimate(parser.parse_args(argv)))
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 2 if result["status"] == STATUS_INVALID_INPUT else 0


if __name__ == "__main__":
    raise SystemExit(main())
