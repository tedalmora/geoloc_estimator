"""Features do fingerprinting multicélula: RSRP por radio_key, só com células do #CSURV.

Política padrão de células (gravada no bundle e no relatório do treino):
- RAT diferente da tecnologia do modelo: fora;
- PLMN igual ao da campanha: dentro;
- PLMN conhecido e diferente: fora (`include_other_plmn` inclui);
- PLMN ausente (registro curto do #CSURV): dentro, marcado como PLMN não confirmado
  (`include_unknown_plmn=False` exclui). Nenhum PLMN é atribuído ao registro;
- CELL_FORBIDDEN: guardada no banco, mas fora das features (`include_forbidden` inclui);
- RSRP ausente (inclusive o 0 "não coletado" do #CSURV): a célula conta como não observada.
A mesma radio_key repetida numa leitura vira a média dos RSRP.

O vocabulário (ordem das colunas) é montado só com amostras de treino. Uma radio_key que
apareça no treino com PLMN conhecido de outra operadora fica fora do vocabulário, mesmo nos
registros curtos. Timing Advance não faz parte das features.

Abaixo da nova API ficam, temporariamente, as funções do Estimador legado (v1, por PCI), ainda
lidas por compare.py e infer.py até a consolidação da fase 5.
"""
import math
import re
import sqlite3
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .dataset import CsurvCell, csurv_cell

TECHNOLOGIES = ("LTE", "NR")
FORBIDDEN_STATUS = "CELL_FORBIDDEN"
PLMN_CAMPAIGN = "campaign"
PLMN_UNCONFIRMED = "unconfirmed"
PLMN_OTHER = "other"
PLMN_CONFLICT = "conflict"
SCALING_RAW = "raw"
SCALING_STANDARDIZED = "standardized"
SCALINGS = (SCALING_RAW, SCALING_STANDARDIZED)
DEFAULT_MISSING_POLICY = "sentinel:-140"
_PLMN_RE = re.compile(r"\d{5,6}")
_MISSING_RE = re.compile(r"(sentinel|floor_offset):(-?\d+(?:\.\d+)?)")


class MissingValueError(ValueError):
    """Valor de ausência incompatível com os RSRP observados no treino."""


@dataclass(frozen=True)
class CellFilterPolicy:
    technology: str
    campaign_plmn: str
    include_other_plmn: bool = False
    include_unknown_plmn: bool = True
    include_forbidden: bool = False

    def __post_init__(self):
        if self.technology not in TECHNOLOGIES:
            raise ValueError(f"tecnologia inválida: {self.technology!r} (use LTE ou NR)")
        if not isinstance(self.campaign_plmn, str) or not _PLMN_RE.fullmatch(self.campaign_plmn):
            raise ValueError(f"PLMN da campanha inválido: {self.campaign_plmn!r}")

    def accepts(self, cell: CsurvCell) -> bool:
        """Filtro por registro, idêntico no treino e na inferência."""
        if cell.rat != self.technology or cell.rsrp_dbm is None:
            return False
        if cell.cell_status == FORBIDDEN_STATUS and not self.include_forbidden:
            return False
        if cell.plmn is None:
            return self.include_unknown_plmn
        return cell.plmn == self.campaign_plmn or self.include_other_plmn

    def to_dict(self) -> dict:
        return {
            "technology": self.technology, "campaign_plmn": self.campaign_plmn,
            "include_other_plmn": self.include_other_plmn, "include_unknown_plmn": self.include_unknown_plmn,
            "include_forbidden": self.include_forbidden, "source": "CSURV",
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CellFilterPolicy":
        return cls(data["technology"], data["campaign_plmn"], bool(data["include_other_plmn"]),
                   bool(data["include_unknown_plmn"]), bool(data["include_forbidden"]))


def cells_from_observations(observations: Iterable) -> Tuple[CsurvCell, ...]:
    """Células do #CSURV de um snapshot no mesmo formato do dataset (MONI/SERVINFO ficam fora)."""
    return tuple(csurv_cell(o.radio_key, o.rat, o.plmn, o.cell_status, o.rsrp_dbm)
                 for o in observations if o.source == "CSURV" and o.radio_key is not None)


def build_vocabulary(samples: Sequence, policy: CellFilterPolicy) -> Tuple[List[str], Dict[str, str]]:
    """Colunas (radio_keys em ordem estável) e status de PLMN de cada uma, só com as amostras de treino."""
    seen_plmns: Dict[str, set] = {}
    accepted = set()
    for sample in samples:
        for cell in sample.cells:
            if cell.rat != policy.technology:
                continue
            plmns = seen_plmns.setdefault(cell.radio_key, set())
            if cell.plmn:
                plmns.add(cell.plmn)
            if policy.accepts(cell):
                accepted.add(cell.radio_key)
    keys, status = [], {}
    for key in sorted(accepted):
        plmns = seen_plmns[key]
        if not plmns:
            key_status = PLMN_UNCONFIRMED
        elif plmns == {policy.campaign_plmn}:
            key_status = PLMN_CAMPAIGN
        else:
            key_status = PLMN_CONFLICT if policy.campaign_plmn in plmns else PLMN_OTHER
        if key_status in (PLMN_OTHER, PLMN_CONFLICT) and not policy.include_other_plmn:
            continue
        keys.append(key)
        status[key] = key_status
    return keys, status


def rsrp_by_key(cells: Iterable[CsurvCell], policy: CellFilterPolicy) -> Dict[str, float]:
    sums: Dict[str, List[float]] = {}
    for cell in cells:
        if policy.accepts(cell):
            acc = sums.setdefault(cell.radio_key, [0.0, 0])
            acc[0] += cell.rsrp_dbm
            acc[1] += 1
    return {key: total / count for key, (total, count) in sums.items()}


def vectorize(cells: Iterable[CsurvCell], policy: CellFilterPolicy, cell_keys: Sequence[str],
              missing_value: float) -> Tuple[List[float], int]:
    """Vetor RSRP na ordem de `cell_keys` (ausente = missing_value) e quantas colunas foram observadas."""
    observed = rsrp_by_key(cells, policy)
    return [observed.get(key, missing_value) for key in cell_keys], sum(1 for key in cell_keys if key in observed)


def observed_rsrp_range(samples: Sequence, policy: CellFilterPolicy, cell_keys: Sequence[str]) -> Tuple[float, float]:
    wanted = set(cell_keys)
    values = [v for s in samples for k, v in rsrp_by_key(s.cells, policy).items() if k in wanted]
    if not values:
        raise MissingValueError("nenhum RSRP observado nas colunas do treino")
    return min(values), max(values)


def validate_missing_value(value: float, observed_min: Optional[float]) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value >= 0:
        raise MissingValueError(f"valor de ausência {value!r} inválido: precisa ser negativo e finito (não é 0 dBm)")
    if observed_min is not None and value >= observed_min:
        raise MissingValueError(
            f"valor de ausência {value:g} dBm não é menor que o menor RSRP observado no treino ({observed_min:g} dBm)"
        )


@dataclass(frozen=True)
class MissingPolicy:
    """`sentinel:<dBm>` (constante) ou `floor_offset:<dB>` (menor RSRP do treino menos <dB>)."""
    kind: str
    value: float

    @classmethod
    def parse(cls, text: str) -> "MissingPolicy":
        match = _MISSING_RE.fullmatch(text.strip())
        if match is None:
            raise ValueError(f"política de ausência inválida: {text!r} (use sentinel:-140 ou floor_offset:10)")
        kind, value = match.group(1), float(match.group(2))
        if kind == "floor_offset" and value <= 0:
            raise ValueError("floor_offset precisa ser positivo (dB abaixo do menor RSRP do treino)")
        return cls(kind, value)

    @property
    def label(self) -> str:
        return f"{self.kind}:{self.value:g}"

    def resolve(self, observed_min: float) -> float:
        value = self.value if self.kind == "sentinel" else observed_min - self.value
        validate_missing_value(value, observed_min)
        return value


def fit_scaling(vectors: Sequence[Sequence[float]], strategy: str) -> Optional[dict]:
    if strategy == SCALING_RAW:
        return None
    if strategy != SCALING_STANDARDIZED:
        raise ValueError(f"escala desconhecida: {strategy!r}")
    n = len(vectors)
    means = [sum(col) / n for col in zip(*vectors)]
    stds = []
    for col, mean in zip(zip(*vectors), means):
        std = math.sqrt(sum((v - mean) ** 2 for v in col) / n)
        stds.append(std if std > 1e-12 else 1.0)  # coluna constante: não amplia ruído numérico
    return {"means": means, "stds": stds}


def apply_scaling(vector: Sequence[float], scaling: Optional[dict]) -> List[float]:
    if scaling is None:
        return list(vector)
    return [(v - m) / s for v, m, s in zip(vector, scaling["means"], scaling["stds"])]


# ---------------------------------------------------------------- legado (v1, por PCI)
FLOOR_RSRP_DBM = -140.0


def open_legacy_radiomap(db_path: str) -> sqlite3.Connection:
    """Abre o radiomapa do Coletor para o Estimador legado: somente leitura e só no schema v1.

    O Coletor grava o schema v2 desde a fase 4 da reconfiguração; o Estimador passa a ler o v2
    na fase 5. Até lá, um banco v2 é recusado com mensagem clara, sem nenhuma alteração.
    """
    from coletor.db import open_read_only, schema_info

    try:
        conn = open_read_only(db_path)
    except FileNotFoundError as exc:
        raise SystemExit(f"erro: {exc}") from exc
    version, _ = schema_info(conn)
    if version != 0:
        conn.close()
        raise SystemExit(
            f"erro: {db_path} está no schema v{version} do Coletor. O Estimador legado (train/compare/infer) "
            "só lê o schema v1; a leitura do schema v2 entra na fase 5 da reconfiguração."
        )
    return conn


def build_feature_vector(measurement_row: dict, neighbors: List[dict], vocabulary: List[int]) -> List[float]:
    """1 leitura (measurement + suas neighbor_cells) -> vetor de tamanho fixo len(vocabulary) + 1.

    IMPORTANTE: esta é a ÚNICA função que monta o vetor de features -- tanto o treino
    (build_training_table) quanto a inferência (estimador/infer.py) passam por aqui, para
    nunca correrem o risco de gerar vetores com tamanho/ordem diferentes entre si.
    O timing_advance vai sempre na última posição, depois das colunas de PCI.
    """
    rsrp_by_pci: Dict[int, float] = {n["pci"]: n["rsrp_dbm"] for n in neighbors}
    if measurement_row.get("serving_pci") is not None:
        rsrp_by_pci[measurement_row["serving_pci"]] = measurement_row["rsrp_dbm"]
    vector = [rsrp_by_pci.get(pci, FLOOR_RSRP_DBM) for pci in vocabulary]
    vector.append(measurement_row.get("timing_advance") or 0)
    return vector
