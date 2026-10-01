"""Treino e seleção do WKNN com validação por grupos; salva o bundle JSON (model_version 2).

Fluxo, sem vazamento entre grupos:
1. dataset v2 (`estimador.dataset`) e um protocolo: `spatial` (grupo = reference_point_id) ou
   `temporal` (grupo = session_id). Os protocolos medem capacidades diferentes e não são
   equivalentes; o bundle e o relatório registram qual foi usado;
2. os grupos de teste são reservados antes de tudo; os demais formam os folds de seleção;
3. em cada fold, vocabulário, valor de ausência, padronização e plano ENU vêm só do treino do
   fold; a grade (k, L1/L2, RSRP bruto/padronizado, política de ausência) é medida na validação;
4. o candidato de menor erro médio na validação é ajustado em todos os grupos de seleção e
   avaliado uma única vez no teste, que não participa da seleção.

O bundle e o relatório registram a versão do software, grupos, pontos, sessões e amostras do
teste, da seleção e de cada fold, a origem da referência do teste (final só com coordenadas
cadastradas) e avisos de variância alta, referência exploratória ou compartilhada entre sessões.
O teste é separado por tipo de ponto: no protocolo temporal, pontos sem amostras no treino
(extrapolação espacial) não se misturam aos pontos conhecidos (generalização temporal).

Uso (a partir de `codigo/`):
    python -m estimador.train --db radiomapa_v2.db --technology LTE --protocol spatial \\
        --out modelos/wknn_lte_spatial.json [--campaign-plmn 72406] [--report relatorio_lte_spatial.json]
"""
import argparse
import json
import math
import os
import sqlite3
import statistics
from collections import Counter
from dataclasses import asdict, dataclass
from itertools import product
from typing import Dict, List, Optional, Sequence, Tuple

from acquisition.snapshot import utc_now_iso
from coletor.db import SCHEMA_VERSION
from software_version import software_version

from .dataset import (
    GROUP_COLUMN,
    POINT_KNOWN,
    POINT_NEW,
    POINT_NONE,
    PROTOCOLS,
    DatasetError,
    Sample,
    group_sizes,
    load_dataset,
    make_group_split,
    point_type_warnings,
    reference_summary,
    reference_warnings,
    resolve_campaign_plmn,
    samples_in_groups,
    samples_sha256,
    split_by_point_type,
    split_report,
    split_warnings,
)
from .evaluate import error_summary, percentile
from .features import (
    SCALINGS,
    TECHNOLOGIES,
    CellFilterPolicy,
    MissingPolicy,
    MissingValueError,
    apply_scaling,
    build_vocabulary,
    fit_scaling,
    observed_rsrp_range,
    vectorize,
)
from .geometry import COORDINATE_SYSTEM, EnuFrame, haversine_m
from .methods.fingerprint import (
    DISTANCE_METRICS,
    MODEL_TYPE,
    MODEL_VERSION,
    WEIGHTING,
    ZERO_DISTANCE_POLICY,
    BundleError,
    WknnModel,
    distance,
    save_bundle,
    wknn_from_sorted,
)

DEFAULT_K = (1, 3, 5, 7, 9)
DEFAULT_MISSING = ("sentinel:-140", "floor_offset:10")


@dataclass(frozen=True)
class Grid:
    k: Tuple[int, ...] = DEFAULT_K
    metrics: Tuple[str, ...] = DISTANCE_METRICS
    scalings: Tuple[str, ...] = SCALINGS
    missing: Tuple[str, ...] = DEFAULT_MISSING

    def __post_init__(self):
        if not self.k or any(isinstance(k, bool) or not isinstance(k, int) or k < 1 for k in self.k):
            raise ValueError(f"valores de k inválidos: {self.k}")
        if not self.metrics or not set(self.metrics) <= set(DISTANCE_METRICS):
            raise ValueError(f"distâncias inválidas: {self.metrics} (use {', '.join(DISTANCE_METRICS)})")
        if not self.scalings or not set(self.scalings) <= set(SCALINGS):
            raise ValueError(f"escalas inválidas: {self.scalings} (use {', '.join(SCALINGS)})")
        if not self.missing:
            raise ValueError("informe ao menos uma política de ausência")
        for label in self.missing:
            MissingPolicy.parse(label)


@dataclass(frozen=True)
class Candidate:
    k: int
    metric: str
    scaling: str
    missing: str


@dataclass
class FittedFeatures:
    cell_keys: List[str]
    plmn_status: Dict[str, str]
    missing_value: float
    observed_range: Tuple[float, float]
    raw: List[List[float]]
    frame: EnuFrame
    xy: List[Tuple[float, float]]


def fit_features(samples: Sequence[Sample], policy: CellFilterPolicy, missing: MissingPolicy) -> FittedFeatures:
    """Tudo que depende dos dados vem só de `samples` (o treino do fold ou o conjunto de seleção)."""
    cell_keys, plmn_status = build_vocabulary(samples, policy)
    if not cell_keys:
        raise DatasetError("no_features", "nenhuma radio_key aceita pela política nas amostras de treino")
    observed = observed_rsrp_range(samples, policy, cell_keys)
    missing_value = missing.resolve(observed[0])
    raw = [vectorize(s.cells, policy, cell_keys, missing_value)[0] for s in samples]
    frame = EnuFrame(statistics.fmean(s.reference.lat for s in samples), statistics.fmean(s.reference.lon for s in samples))
    xy = [frame.to_enu(s.reference.lat, s.reference.lon)[:2] for s in samples]
    return FittedFeatures(cell_keys, plmn_status, missing_value, observed, raw, frame, xy)


def eligible_samples(samples: Sequence[Sample], protocol: str, policy: CellFilterPolicy) -> Tuple[List[Sample], Dict[str, int]]:
    """Amostras com grupo no protocolo e ao menos uma célula aceita (filtro por registro, sem vocabulário)."""
    eligible, excluded = [], Counter()
    for s in samples:
        if s.group(protocol) is None:
            excluded["sem_grupo_no_protocolo"] += 1
        elif not any(policy.accepts(c) for c in s.cells):
            excluded["sem_celula_aceita_pela_politica"] += 1
        else:
            eligible.append(s)
    return eligible, dict(excluded)


def _metrics_row(errors: List[float]) -> dict:
    summary = error_summary(errors)
    summary.pop("cdf_m")
    return summary


def select_candidate(samples: Sequence[Sample], split, policy: CellFilterPolicy, grid: Grid) -> Tuple[Candidate, List[dict], dict]:
    """Escolhe (k, distância, escala, ausência) só com treino/validação dos folds de seleção."""
    errors: Dict[Candidate, List[float]] = {}
    skipped: Dict[Candidate, str] = {}
    uncovered = 0
    for train_groups, val_groups in split.folds:
        train = samples_in_groups(samples, split.protocol, train_groups)
        val = samples_in_groups(samples, split.protocol, val_groups)
        for index, missing_label in enumerate(grid.missing):
            try:
                fitted = fit_features(train, policy, MissingPolicy.parse(missing_label))
            except MissingValueError as exc:
                for k, metric, scaling in product(grid.k, grid.metrics, grid.scalings):
                    skipped[Candidate(k, metric, scaling, missing_label)] = str(exc)
                continue
            queries = []
            for s in val:
                vector, n_observed = vectorize(s.cells, policy, fitted.cell_keys, fitted.missing_value)
                if n_observed:
                    queries.append((vector, fitted.frame.to_enu(s.reference.lat, s.reference.lon)[:2]))
                elif index == 0:
                    uncovered += 1
            xs, ys = [p[0] for p in fitted.xy], [p[1] for p in fitted.xy]
            for scaling in grid.scalings:
                params = fit_scaling(fitted.raw, scaling)
                train_vectors = [apply_scaling(v, params) for v in fitted.raw]
                scaled = [(apply_scaling(v, params), xy) for v, xy in queries]
                for metric in grid.metrics:
                    for query, (qx, qy) in scaled:
                        ordered = sorted((distance(query, f, metric), i) for i, f in enumerate(train_vectors))
                        for k in grid.k:
                            candidate = Candidate(k, metric, scaling, missing_label)
                            if k > len(train_vectors):
                                skipped[candidate] = f"k={k} maior que o treino do fold ({len(train_vectors)} amostras)"
                                continue
                            x, y, _, _ = wknn_from_sorted(ordered, xs, ys, k)
                            errors.setdefault(candidate, []).append(math.hypot(x - qx, y - qy))
    valid = {c: e for c, e in errors.items() if c not in skipped and e}
    if not valid:
        raise DatasetError("insufficient_samples", "nenhum candidato da grade é compatível com todos os folds "
                                                   "(k maior que o treino ou valor de ausência inválido)")
    ranking = sorted(valid, key=lambda c: (statistics.fmean(valid[c]), percentile(sorted(valid[c]), 90),
                                           c.k, c.metric, c.scaling, c.missing))
    rows = [{**asdict(c), **_metrics_row(valid[c])} for c in ranking]
    details = {"skipped_candidates": {f"k={c.k},{c.metric},{c.scaling},{c.missing}": why for c, why in skipped.items()},
               "uncovered_validation_samples": uncovered}
    return ranking[0], rows, details


def build_bundle(selection: Sequence[Sample], policy: CellFilterPolicy, candidate: Candidate, protocol: str) -> dict:
    fitted = fit_features(selection, policy, MissingPolicy.parse(candidate.missing))
    scaling = fit_scaling(fitted.raw, candidate.scaling)
    if candidate.k > len(selection):
        raise DatasetError("insufficient_samples", f"k={candidate.k} maior que o conjunto de seleção ({len(selection)})")
    return {
        "model_type": MODEL_TYPE, "model_version": MODEL_VERSION, "created_at": utc_now_iso(),
        "technology": policy.technology, "cell_keys": fitted.cell_keys, "cell_key_plmn_status": fitted.plmn_status,
        "missing_policy": candidate.missing, "missing_value": fitted.missing_value,
        "missing_value_validation": {"observed_min_dbm": fitted.observed_range[0], "observed_max_dbm": fitted.observed_range[1],
                                     "rule": "missing_value < menor RSRP observado no treino e < 0 dBm"},
        "scaling_strategy": candidate.scaling, "scaling": scaling,
        "coordinate_system": COORDINATE_SYSTEM, "enu_origin": fitted.frame.to_dict(),
        "n_neighbors": candidate.k, "distance_metric": candidate.metric, "weighting": WEIGHTING,
        "zero_distance_policy": ZERO_DISTANCE_POLICY, "cell_filter_policy": policy.to_dict(),
        "protocol": protocol,
        "fingerprints": {
            "measurement_id": [s.measurement_id for s in selection],
            "group_id": [s.group(protocol) for s in selection],
            "x_m": [p[0] for p in fitted.xy], "y_m": [p[1] for p in fitted.xy], "rsrp_dbm": fitted.raw,
            "reference_source": [s.reference.source for s in selection],
            "reference_uncertainty_m": [s.reference.uncertainty_m for s in selection],
            "reference_dispersion_m": [s.reference.dispersion_m for s in selection],
        },
    }


def evaluate_model(model: WknnModel, samples: Sequence[Sample]) -> dict:
    errors, statuses = [], Counter()
    for s in samples:
        estimate = model.predict(s.cells)
        statuses[estimate.status] += 1
        if estimate.valid:
            errors.append(haversine_m(estimate.lat, estimate.lon, s.reference.lat, s.reference.lon))
    return {**error_summary(errors, n_total=len(samples)), "status_counts": dict(sorted(statuses.items()))}


def train(db_path: str, technology: str, protocol: str, out_path: str, campaign_plmn: Optional[str] = None,
          grid: Grid = Grid(), test_fraction: float = 0.2, n_folds: int = 5, seed: int = 42,
          include_other_plmn: bool = False, include_unknown_plmn: bool = True, include_forbidden: bool = False,
          allow_single_gnss: bool = False) -> Tuple[dict, dict]:
    """Treina, avalia no teste e salva o bundle. Devolve (bundle, relatório)."""
    if technology not in TECHNOLOGIES or protocol not in PROTOCOLS:
        raise ValueError("tecnologia ou protocolo inválido")
    if not out_path.lower().endswith(".json"):
        raise BundleError("o bundle WKNN é salvo em JSON: use a extensão .json")
    if os.path.exists(out_path):
        raise FileExistsError(f"{out_path}: já existe; escolha outro nome (bundles não são sobrescritos).")
    dataset = load_dataset(db_path, allow_single_gnss=allow_single_gnss)
    policy = CellFilterPolicy(technology, resolve_campaign_plmn(dataset.samples, campaign_plmn),
                              include_other_plmn, include_unknown_plmn, include_forbidden)
    samples, not_eligible = eligible_samples(dataset.samples, protocol, policy)
    split = make_group_split(samples, protocol, test_fraction, n_folds, seed)
    selection = samples_in_groups(samples, protocol, split.selection_groups)
    test = samples_in_groups(samples, protocol, split.test_groups)
    best, ranking, details = select_candidate(selection, split, policy, grid)

    bundle = build_bundle(selection, policy, best, protocol)
    bundle["software_version"] = software_version()
    bundle["reference_policy"] = dataset.reference_policy()
    known_points = sorted({s.reference_point_id for s in selection if s.reference_point_id is not None})
    bundle["training_groups"] = {"protocol": protocol, "group_column": GROUP_COLUMN[protocol],
                                 "groups": group_sizes(selection, protocol, split.selection_groups),
                                 "n_samples": len(selection), "reference_point_ids": known_points}
    bundle["dataset"] = {
        "source_db": os.path.basename(db_path), "schema_version": SCHEMA_VERSION,
        "samples_sha256": samples_sha256(samples), "n_eligible_samples": len(samples),
        "excluded_from_dataset": dataset.excluded, "excluded_by_protocol_or_policy": not_eligible,
        "selection_samples_sha256": samples_sha256(selection),
        "test_measurement_ids": sorted(s.measurement_id for s in test), "test_samples_sha256": samples_sha256(test),
    }
    bundle["validation"] = {
        "split": split.to_dict(), "subsets": split_report(samples, split), "test_fraction": test_fraction, "seed": seed,
        "grid": {"k": list(grid.k), "metrics": list(grid.metrics), "scalings": list(grid.scalings), "missing": list(grid.missing)},
        "selection_criterion": "menor erro médio (m) nas amostras de validação dos folds; desempate por p90",
        "chosen": asdict(best), "validation_metrics": ranking[0], "ranking_top": ranking[:10], **details,
        "test_reference": reference_summary(test),
        "warnings": (split_warnings(protocol, split.test_groups, [len(train) for train, _ in split.folds])
                     + reference_warnings(test, protocol) + point_type_warnings(test, set(known_points), protocol)),
    }
    model = WknnModel(bundle)
    by_type = dict(sorted(split_by_point_type(test, set(known_points)).items()))
    bundle["validation"]["test_metrics"] = {**evaluate_model(model, test),
                                           "composition": {kind: len(items) for kind, items in by_type.items()}}
    bundle["validation"]["test_metrics_by_point_type"] = {kind: evaluate_model(model, items)
                                                         for kind, items in by_type.items()}
    save_bundle(bundle, out_path)
    report = {key: value for key, value in bundle.items() if key != "fingerprints"}
    report["dataset_summary"] = dataset.summary()
    report["validation"] = {**bundle["validation"], "ranking": ranking}
    return bundle, report


def _fmt(value, unit=" m") -> str:
    return "-" if value is None else f"{value:.1f}{unit}"


def _metrics_text(t: dict) -> str:
    return (f"{t['n']}/{t['n_total']} válidas; média {_fmt(t['mean_m'])}, mediana {_fmt(t['median_m'])}, "
            f"RMSE {_fmt(t['rmse_m'])}, p90 {_fmt(t['p90_m'])}, p95 {_fmt(t['p95_m'])}, máx {_fmt(t['max_m'])}")


POINT_TYPE_LABELS = {POINT_KNOWN: "pontos conhecidos (generalização temporal)",
                     POINT_NEW: "pontos novos (extrapolação espacial)", POINT_NONE: "rotas, sem ponto"}


def _print_summary(bundle: dict, report: dict, out_path: str) -> None:
    v = bundle["validation"]
    split = v["split"]
    print(f"Protocolo: {bundle['protocol']} (grupo = {split['group_column']}); tecnologia {bundle['technology']}; "
          f"PLMN da campanha {bundle['cell_filter_policy']['campaign_plmn']}; software {bundle['software_version']}")
    print(f"Amostras elegíveis: {bundle['dataset']['n_eligible_samples']}; excluídas: "
          f"{ {**bundle['dataset']['excluded_from_dataset'], **bundle['dataset']['excluded_by_protocol_or_policy']} }")
    for name, label in (("test", "Teste"), ("selection", "Seleção")):
        s = v["subsets"][name]
        print(f"{label}: {s['groups']} grupos, {s['points']} pontos, {s['sessions']} sessões, {s['samples']} amostras")
    for i, fold in enumerate(v["subsets"]["folds"], 1):
        t, val = fold["train"], fold["validation"]
        print(f"  fold {i}: treino {t['groups']} grupos/{t['samples']} amostras; validação {val['groups']} grupos/"
              f"{val['samples']} amostras")
    print("Seleção por validação (top 5):")
    for row in report["validation"]["ranking"][:5]:
        print(f"  k={row['k']:<2} {row['metric']} {row['scaling']:<12} {row['missing']:<16} "
              f"média {_fmt(row['mean_m'])}  p90 {_fmt(row['p90_m'])}  n={row['n']}")
    print(f"Escolhido: {v['chosen']}; {len(bundle['cell_keys'])} colunas; ausência = {bundle['missing_value']:g} dBm")
    print(f"Referência do teste: {v['test_reference']['status']} {v['test_reference']['sources']}")
    print(f"Teste ({bundle['protocol']}, fora da seleção), todas as amostras {v['test_metrics']['composition']}: "
          f"{_metrics_text(v['test_metrics'])}")
    for kind, metrics in v["test_metrics_by_point_type"].items():
        print(f"  {POINT_TYPE_LABELS[kind]}: {_metrics_text(metrics)}")
    for warning in v["warnings"]:
        print(f"AVISO: {warning}")
    print(f"Bundle: {out_path}")


def _csv(text: str, cast=str) -> Tuple:
    return tuple(cast(item.strip()) for item in text.split(",") if item.strip())


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Treino e seleção do WKNN com validação por grupos (banco v2)")
    parser.add_argument("--db", required=True, help="radiomapa v2 do Coletor (só leitura)")
    parser.add_argument("--technology", required=True, choices=TECHNOLOGIES, help="RAT das células usadas como features")
    parser.add_argument("--protocol", required=True, choices=PROTOCOLS,
                        help="spatial: grupo = ponto de referência; temporal: grupo = sessão")
    parser.add_argument("--out", required=True, help="bundle novo (.json)")
    parser.add_argument("--campaign-plmn", dest="campaign_plmn")
    parser.add_argument("--k", default=",".join(map(str, DEFAULT_K)))
    parser.add_argument("--metrics", default=",".join(DISTANCE_METRICS))
    parser.add_argument("--scaling", default=",".join(SCALINGS))
    parser.add_argument("--missing", default=",".join(DEFAULT_MISSING), help="ex.: sentinel:-140,floor_offset:10")
    parser.add_argument("--test-fraction", dest="test_fraction", type=float, default=0.2)
    parser.add_argument("--folds", type=int, default=5)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--include-other-plmn", dest="include_other_plmn", action="store_true")
    parser.add_argument("--exclude-unknown-plmn", dest="include_unknown_plmn", action="store_false")
    parser.add_argument("--include-forbidden", dest="include_forbidden", action="store_true")
    parser.add_argument("--allow-single-gnss", dest="allow_single_gnss", action="store_true",
                        help="aceita leitura GNSS isolada como referência (rotas); fica registrado no bundle")
    parser.add_argument("--report", help="relatório JSON (sem os fingerprints)")
    args = parser.parse_args(argv)
    try:
        if args.report and os.path.exists(args.report):
            raise FileExistsError(f"{args.report}: já existe; escolha outro nome.")
        grid = Grid(_csv(args.k, int), _csv(args.metrics), _csv(args.scaling), _csv(args.missing))
        bundle, report = train(args.db, args.technology, args.protocol, args.out, args.campaign_plmn, grid,
                               args.test_fraction, args.folds, args.seed, args.include_other_plmn,
                               args.include_unknown_plmn, args.include_forbidden, args.allow_single_gnss)
    except (DatasetError, BundleError, MissingValueError, OSError, ValueError, sqlite3.Error) as exc:
        raise SystemExit(f"erro: {exc}") from exc
    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
    _print_summary(bundle, report, args.out)


if __name__ == "__main__":
    main()


