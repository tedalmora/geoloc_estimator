"""Dataset do Estimador a partir do radiomapa v2 do Coletor, e divisões por grupo.

- Lê só o schema v2, em modo somente leitura. Bancos v1 precisam ser migrados antes
  (`python -m coletor.migrate`); o Estimador não lê v1 diretamente.
- Linhas `legacy_ambiguous` (migradas do v1) nunca entram no dataset.
- As células de cada amostra vêm só do #CSURV. Valores do #MONI/#SERVINFO não entram nem
  substituem os do #CSURV. RSRP 0 (não coletado na varredura) vira ausente.
- Referência posicional, em ordem de prioridade:
  1. coordenada cadastrada do ponto de referência;
  2. mediana das leituras GNSS do ponto (mínimo `MIN_GNSS_FOR_MEDIAN`), com a dispersão;
  3. leitura GNSS isolada da própria medição, só com `allow_single_gnss` (dado auxiliar).
- Protocolos de divisão (não equivalentes; cada resultado informa o seu):
  `spatial` agrupa por reference_point_id e `temporal` por session_id. Não há divisão
  aleatória por linha.
"""
import hashlib
import json
import random
import statistics
from collections import Counter
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from coletor.db import SCHEMA_VERSION, open_read_only, schema_info

from .geometry import haversine_m

PROTOCOL_SPATIAL = "spatial"
PROTOCOL_TEMPORAL = "temporal"
PROTOCOLS = (PROTOCOL_SPATIAL, PROTOCOL_TEMPORAL)
GROUP_COLUMN = {PROTOCOL_SPATIAL: "reference_point_id", PROTOCOL_TEMPORAL: "session_id"}

REF_REGISTERED = "reference_point"
REF_GNSS_MEDIAN = "gnss_point_median"
REF_GNSS_SINGLE = "gnss_single"
MIN_GNSS_FOR_MEDIAN = 3
MIN_GROUPS = 3  # 1 grupo de teste + 2 para a seleção por validação
VALID_FIX_TYPES = (2, 3)


class DatasetError(RuntimeError):
    """Erro com código estável no início da mensagem, ex.: 'spatial_validation_unavailable: ...'."""

    def __init__(self, code: str, detail: str):
        self.code = code
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class CsurvCell:
    radio_key: str
    rat: str
    plmn: Optional[str]
    cell_status: Optional[str]
    rsrp_dbm: Optional[float]


@dataclass(frozen=True)
class ReferencePosition:
    lat: float
    lon: float
    source: str  # reference_point, gnss_point_median ou gnss_single
    coord_source: Optional[str] = None  # origem declarada da coordenada cadastrada (ex.: rtk)
    uncertainty_m: Optional[float] = None  # incerteza declarada no cadastro do ponto
    dispersion_m: Optional[float] = None  # mediana da distância das leituras GNSS até a mediana
    n_gnss: int = 0


@dataclass(frozen=True)
class Sample:
    measurement_id: int
    session_id: int
    reference_point_id: Optional[int]
    started_at: str
    session_started_at: str
    access_mode: str
    campaign_plmn: Optional[str]
    serving_global_cell_key: Optional[str]
    cells: Tuple[CsurvCell, ...]
    reference: ReferencePosition

    def group(self, protocol: str) -> Optional[int]:
        return self.reference_point_id if protocol == PROTOCOL_SPATIAL else self.session_id


@dataclass
class Dataset:
    source: str
    samples: List[Sample]
    excluded: Dict[str, int]
    allow_single_gnss: bool
    min_gnss_for_median: int

    def summary(self) -> dict:
        return {
            "source_db": self.source,
            "n_samples": len(self.samples),
            "excluded": dict(sorted(self.excluded.items())),
            "reference_sources": dict(sorted(Counter(s.reference.source for s in self.samples).items())),
            "access_modes": dict(sorted(Counter(s.access_mode for s in self.samples).items())),
            "sessions": len({s.session_id for s in self.samples}),
            "reference_points": len({s.reference_point_id for s in self.samples if s.reference_point_id is not None}),
            "reference_policy": self.reference_policy(),
        }

    def reference_policy(self) -> dict:
        return {"priority": [REF_REGISTERED, REF_GNSS_MEDIAN, REF_GNSS_SINGLE],
                "allow_single_gnss": self.allow_single_gnss, "min_gnss_for_median": self.min_gnss_for_median}


def samples_sha256(samples: Sequence[Sample]) -> str:
    """Identificação do conteúdo usado (medições, referências e células), independente da ordem."""
    payload = [
        [s.measurement_id, s.session_id, s.reference_point_id, round(s.reference.lat, 9), round(s.reference.lon, 9),
         s.reference.source, sorted(json.dumps([c.radio_key, c.plmn, c.cell_status, c.rsrp_dbm]) for c in s.cells)]
        for s in sorted(samples, key=lambda s: s.measurement_id)
    ]
    return hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode("utf-8")).hexdigest()


def open_radiomap(db_path: str):
    """Conexão somente leitura com um radiomapa v2; v1 e versões desconhecidas são recusados."""
    try:
        conn = open_read_only(db_path)
    except FileNotFoundError as exc:
        raise DatasetError("radiomap_not_found", str(exc)) from exc
    version, tables = schema_info(conn)
    if version != SCHEMA_VERSION:
        conn.close()
        if version == 0 and "measurements" in tables:
            raise DatasetError(
                "schema_v1",
                f"{db_path} está no formato v1, que o Estimador não lê. Gere uma cópia v2 com "
                f"'python -m coletor.migrate migrate --db {db_path} --out <novo.db>' (as linhas migradas "
                "ficam legacy_ambiguous e também não entram no treino).",
            )
        raise DatasetError("schema_unsupported", f"{db_path}: user_version={version}; o Estimador lê só o schema v{SCHEMA_VERSION}.")
    return conn


def load_dataset(db_path: str, allow_single_gnss: bool = False, min_gnss_for_median: int = MIN_GNSS_FOR_MEDIAN) -> Dataset:
    conn = open_radiomap(db_path)
    try:
        rows = conn.execute(
            "SELECT m.measurement_id, m.session_id, m.reference_point_id, m.started_at, s.started_at, m.access_mode, "
            "s.campaign_plmn, m.serving_global_cell_key, m.snapshot_status, m.csurv_status, m.legacy_ambiguous, "
            "m.gnss_status, m.gnss_lat, m.gnss_lon, m.gnss_fix_type "
            "FROM measurements m JOIN sessions s ON s.session_id = m.session_id ORDER BY m.measurement_id"
        ).fetchall()
        cells_by_measurement: Dict[int, List[CsurvCell]] = {}
        for mid, rat, radio_key, plmn, status, rsrp in conn.execute(
            "SELECT measurement_id, rat, radio_key, plmn, cell_status, rsrp_dbm FROM observed_cells "
            "WHERE source = 'CSURV' AND radio_key IS NOT NULL ORDER BY observed_cell_id"
        ):
            # RSRP 0 é "não coletado" no #CSURV (manual FN990, p. 819)
            cells_by_measurement.setdefault(mid, []).append(
                CsurvCell(radio_key, rat, plmn, status, None if rsrp in (None, 0) else float(rsrp))
            )
        points = conn.execute(
            "SELECT reference_point_id, lat, lon, coord_source, coord_uncertainty_m FROM reference_points"
        ).fetchall()
    finally:
        conn.close()

    fixes_by_point: Dict[int, List[Tuple[float, float]]] = {}
    for row in rows:
        _, _, pid, *_, legacy, gnss_status, lat, lon, fix_type = row
        if pid is not None and not legacy and gnss_status == "ok" and fix_type in VALID_FIX_TYPES and lat is not None:
            fixes_by_point.setdefault(pid, []).append((lat, lon))

    point_reference: Dict[int, ReferencePosition] = {}
    for pid, lat, lon, coord_source, uncertainty in points:
        if lat is not None and lon is not None:
            point_reference[pid] = ReferencePosition(lat, lon, REF_REGISTERED, coord_source, uncertainty)
            continue
        fixes = fixes_by_point.get(pid, [])
        if len(fixes) >= min_gnss_for_median:
            mlat = statistics.median(f[0] for f in fixes)
            mlon = statistics.median(f[1] for f in fixes)
            dispersion = statistics.median(haversine_m(mlat, mlon, f[0], f[1]) for f in fixes)
            point_reference[pid] = ReferencePosition(mlat, mlon, REF_GNSS_MEDIAN, dispersion_m=dispersion, n_gnss=len(fixes))

    excluded: Counter = Counter()
    samples: List[Sample] = []
    for (mid, sid, pid, started_at, session_started_at, access_mode, campaign_plmn, global_key, snapshot_status,
         csurv_status, legacy, gnss_status, lat, lon, fix_type) in rows:
        if legacy:
            excluded["legacy_ambiguous"] += 1
            continue
        if snapshot_status == "invalid":
            excluded["snapshot_invalid"] += 1
            continue
        if csurv_status != "ok":
            excluded["csurv_indisponivel"] += 1
            continue
        cells = tuple(cells_by_measurement.get(mid, ()))
        if not cells:
            excluded["sem_celulas_csurv"] += 1
            continue
        if pid is not None:
            reference = point_reference.get(pid)
            if reference is None:
                excluded["ponto_sem_referencia"] += 1
                continue
        elif allow_single_gnss and gnss_status == "ok" and fix_type in VALID_FIX_TYPES and lat is not None:
            reference = ReferencePosition(lat, lon, REF_GNSS_SINGLE, n_gnss=1)
        else:
            excluded["rota_sem_referencia_permitida"] += 1
            continue
        samples.append(Sample(mid, sid, pid, started_at, session_started_at, access_mode, campaign_plmn, global_key,
                              cells, reference))
    return Dataset(db_path, samples, dict(excluded), allow_single_gnss, min_gnss_for_median)


def resolve_campaign_plmn(samples: Sequence[Sample], requested: Optional[str]) -> str:
    """PLMN da campanha: o informado ou o único registrado nas sessões. Nunca é inferido dos dados de rádio."""
    if requested:
        return requested
    plmns = sorted({s.campaign_plmn for s in samples if s.campaign_plmn})
    if len(plmns) == 1:
        return plmns[0]
    if not plmns:
        raise DatasetError("campaign_plmn_unknown", "as sessões não registram campaign_plmn; informe --campaign-plmn.")
    raise DatasetError("campaign_plmn_conflict", f"sessões com PLMNs de campanha diferentes {plmns}; informe --campaign-plmn.")


# ---------------------------------------------------------------- divisões por grupo
@dataclass(frozen=True)
class GroupSplit:
    protocol: str
    test_groups: Tuple[int, ...]
    selection_groups: Tuple[int, ...]
    folds: Tuple[Tuple[Tuple[int, ...], Tuple[int, ...]], ...]  # (grupos de treino, grupos de validação)
    ungrouped_samples: int  # amostras sem grupo neste protocolo (ex.: rotas no protocolo spatial)

    def to_dict(self) -> dict:
        return {
            "protocol": self.protocol, "group_column": GROUP_COLUMN[self.protocol],
            "test_groups": list(self.test_groups), "selection_groups": list(self.selection_groups),
            "folds": [{"train_groups": list(t), "validation_groups": list(v)} for t, v in self.folds],
            "ungrouped_samples": self.ungrouped_samples,
        }


def make_group_split(
    samples: Sequence[Sample], protocol: str, test_fraction: float = 0.2, n_folds: int = 5, seed: int = 42,
    min_groups: int = MIN_GROUPS,
) -> GroupSplit:
    """Reserva grupos inteiros para o teste final e divide os demais em folds de seleção.

    spatial: grupos de teste sorteados com `seed`. temporal: as sessões mais recentes ficam no
    teste. Os folds distribuem os grupos de seleção equilibrando o número de amostras.
    """
    if protocol not in PROTOCOLS:
        raise ValueError(f"protocolo desconhecido: {protocol!r}")
    if not 0 < test_fraction < 1 or n_folds < 2:
        raise ValueError("test_fraction deve estar em (0, 1) e n_folds >= 2")
    sizes = Counter(s.group(protocol) for s in samples if s.group(protocol) is not None)
    ungrouped = sum(1 for s in samples if s.group(protocol) is None)
    groups = sorted(sizes)
    if len(groups) < min_groups:
        raise DatasetError(
            f"{protocol}_validation_unavailable",
            f"insufficient_groups: {len(groups)} grupo(s) com amostras rotuladas ({GROUP_COLUMN[protocol]}); "
            f"mínimo {min_groups} (1 para teste e 2 para seleção). Não há divisão aleatória por linha como alternativa.",
        )
    n_test = min(max(1, round(test_fraction * len(groups))), len(groups) - 2)
    if protocol == PROTOCOL_SPATIAL:
        shuffled = list(groups)
        random.Random(seed).shuffle(shuffled)
        test = sorted(shuffled[:n_test])
    else:
        start = {s.session_id: s.session_started_at for s in samples}
        test = sorted(sorted(groups, key=lambda g: (start[g], g))[-n_test:])
    selection = [g for g in groups if g not in test]
    k = min(n_folds, len(selection))
    members: List[List[int]] = [[] for _ in range(k)]
    loads = [0] * k
    for g in sorted(selection, key=lambda g: (-sizes[g], g)):
        i = min(range(k), key=lambda j: (loads[j], j))
        members[i].append(g)
        loads[i] += sizes[g]
    folds = tuple((tuple(g for g in selection if g not in m), tuple(sorted(m))) for m in members)
    return GroupSplit(protocol, tuple(test), tuple(selection), folds, ungrouped)


def samples_in_groups(samples: Sequence[Sample], protocol: str, groups: Sequence[int]) -> List[Sample]:
    wanted = set(groups)
    return [s for s in samples if s.group(protocol) in wanted]


def group_sizes(samples: Sequence[Sample], protocol: str, groups: Sequence[int]) -> Dict[str, int]:
    counts = Counter(s.group(protocol) for s in samples_in_groups(samples, protocol, groups))
    return {str(g): counts[g] for g in sorted(counts)}
