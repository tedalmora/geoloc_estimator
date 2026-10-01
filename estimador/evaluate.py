"""Métricas de erro e avaliação do Estimador (Cell-ID e WKNN), só com a biblioteca padrão.

Métricas (`error_summary`): média, mediana (CE50), RMSE, p90, p95, máximo e CDF (erros
ordenados); percentis por interpolação linear. O erro é a distância haversine até a posição de
referência da leitura.

Cobertura de cada método, sobre as tentativas pertinentes (todas as leituras não migradas dos
grupos de teste, inclusive as que falharam antes de chegar ao método):
- elegíveis: leituras com a entrada que o método exige (WKNN: snapshot não inválido e #CSURV lido
  com célula da tecnologia do bundle; Cell-ID: snapshot não inválido e global_cell_key da servidora);
- cobertura operacional = estimativas válidas / tentativas;
- cobertura específica = estimativas válidas / tentativas elegíveis;
- subconjunto comum = tentativas em que os dois métodos produziram estimativa com referência; só
  nele os erros dos dois métodos são diretamente comparáveis.
No recorte "tentativas", o erro de cada método usa as suas próprias estimativas válidas.

Subcomandos (a partir de `codigo/`):
    python -m estimador.evaluate compare --db radiomapa_v2.db --model modelos/wknn_lte_spatial.json \\
        [--registry estimador/network_registry.json] [--out resumo.csv] [--predictions-out estimativas.csv] [--json-out resumo.json]
    python -m estimador.evaluate cell-id --db radiomapa_v2.db --registry estimador/network_registry.json [--out celulas.csv]

compare: refaz o dataset com a política de referência gravada no bundle, confere as medições de
teste do bundle (`dataset.test_measurement_ids` e `dataset.test_samples_sha256`; se o banco
mudou, recusa) e avalia WKNN e Cell-ID em todas as tentativas dos grupos de teste. Registra o
protocolo (resultados de protocolos diferentes não são equivalentes), as versões do software
(avaliação e treino), os ambientes, a origem e a dispersão da referência e os avisos. Com os
pontos do treino gravados no bundle, separa pontos conhecidos (generalização temporal) de pontos
novos (extrapolação espacial). Também mede a carga do bundle e a latência do WKNN por leitura
(média, p95 e máximo), usadas na validação do runtime no Raspberry Pi.
cell-id: confronta o cadastro com todas as tentativas, com o erro por célula servidora.
"""
import argparse
import csv
import json
import math
import os
import sqlite3
import time
from collections import Counter
from typing import Dict, List, Optional, Sequence

from software_version import software_version

from .dataset import (
    GROUP_COLUMN,
    MIN_GNSS_FOR_MEDIAN,
    PROTOCOL_TEMPORAL,
    Attempt,
    DatasetError,
    load_dataset,
    point_type,
    point_type_warnings,
    reference_summary,
    reference_warnings,
    samples_sha256,
    split_warnings,
)
from .estimate import STATUS_VALID, PositionEstimate, unavailable_reading
from .geometry import haversine_m
from .methods.cell_id import METHOD as CELL_ID, CellRegistry, estimate_cell_id, load_registry
from .methods.fingerprint import METHOD as WKNN, BundleError, load_bundle

ERROR_FIELDS = ("mean_m", "median_m", "rmse_m", "p90_m", "p95_m", "max_m")
COUNT_FIELDS = ("n_attempts", "n_eligible", "n_valid", "n_failed", "operational_coverage", "specific_coverage",
                "n_errors")
SUBSET_ATTEMPTS, SUBSET_COMMON = "tentativas", "comum"
PREDICTION_FIELDS = ("protocol", "group_id", "measurement_id", "session_id", "reference_point_id", "point_type",
                     "environment", "serving_global_cell_key", "method", "eligible", "status", "reason", "lat", "lon",
                     "ref_lat", "ref_lon", "reference_source", "reference_uncertainty_m", "reference_dispersion_m",
                     "reference_shared_between_sessions", "error_m")


class EvaluationError(RuntimeError):
    """Avaliação impossível com os dados informados; a mensagem começa com um código."""


def percentile(sorted_values: Sequence[float], q: float) -> float:
    if not sorted_values:
        raise ValueError("percentil de lista vazia")
    pos = (len(sorted_values) - 1) * q / 100.0
    lo, hi = math.floor(pos), math.ceil(pos)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


def error_summary(errors: Sequence[float], n_total: Optional[int] = None) -> dict:
    n = len(errors)
    total = n if n_total is None else n_total
    summary = {"n": n, "n_total": total, "coverage": (n / total) if total else None}
    if n == 0:
        return {**summary, "mean_m": None, "median_m": None, "rmse_m": None, "p90_m": None, "p95_m": None,
                "max_m": None, "cdf_m": []}
    ordered = sorted(errors)
    return {
        **summary, "mean_m": sum(ordered) / n, "median_m": percentile(ordered, 50),
        "rmse_m": math.sqrt(sum(e * e for e in ordered) / n), "p90_m": percentile(ordered, 90),
        "p95_m": percentile(ordered, 95), "max_m": ordered[-1], "cdf_m": ordered,
    }


def error_stats(errors: Sequence[float]) -> dict:
    """Só os erros, sem cobertura: n_errors, média, mediana, RMSE, p90, p95, máximo e CDF."""
    s = error_summary(errors)
    return {"n_errors": s["n"], **{k: s[k] for k in ERROR_FIELDS}, "cdf_m": s["cdf_m"]}


def wknn_eligible(attempt: Attempt, technology: str) -> bool:
    """Entrada do WKNN: snapshot não inválido e #CSURV lido com ao menos uma célula da tecnologia do bundle."""
    return (attempt.snapshot_status != "invalid" and attempt.csurv_status == "ok"
            and any(c.rat == technology for c in attempt.cells))


def cell_id_eligible(attempt: Attempt) -> bool:
    """Entrada do Cell-ID: snapshot não inválido e global_cell_key da servidora (não depende do #CSURV)."""
    return attempt.snapshot_status != "invalid" and attempt.serving_global_cell_key is not None


def _cell_id(registry: CellRegistry, attempt: Attempt) -> PositionEstimate:
    return (unavailable_reading(CELL_ID, attempt.snapshot_status)
            or estimate_cell_id(registry, attempt.serving_global_cell_key))


def _row(attempt: Attempt, estimate: PositionEstimate, eligible: bool, protocol: Optional[str] = None,
         known_points=None) -> dict:
    ref = attempt.reference
    row = {
        "protocol": protocol, "group_id": attempt.group(protocol) if protocol else None,
        "measurement_id": attempt.measurement_id, "session_id": attempt.session_id,
        "reference_point_id": attempt.reference_point_id,
        "point_type": None if known_points is None else point_type(attempt, known_points),
        "environment": attempt.environment, "serving_global_cell_key": attempt.serving_global_cell_key,
        "method": estimate.method, "eligible": eligible, "status": estimate.status, "reason": estimate.reason,
        "lat": estimate.lat, "lon": estimate.lon, "ref_lat": None, "ref_lon": None, "reference_source": None,
        "reference_uncertainty_m": None, "reference_dispersion_m": None, "reference_shared_between_sessions": None,
        "error_m": None,
    }
    if ref is not None:
        row.update(ref_lat=ref.lat, ref_lon=ref.lon, reference_source=ref.source, reference_uncertainty_m=ref.uncertainty_m,
                   reference_dispersion_m=ref.dispersion_m, reference_shared_between_sessions=ref.shared_between_sessions)
        if estimate.valid:
            row["error_m"] = haversine_m(estimate.lat, estimate.lon, ref.lat, ref.lon)
    return row


def _summary(rows: Sequence[dict]) -> dict:
    """Tentativas, elegíveis, válidas, falhas, coberturas operacional e específica, estados e erros."""
    n, eligible = len(rows), sum(1 for r in rows if r["eligible"])
    valid = sum(1 for r in rows if r["status"] == STATUS_VALID)
    return {
        "n_attempts": n, "n_eligible": eligible, "n_valid": valid, "n_failed": n - valid,
        "operational_coverage": valid / n if n else None, "specific_coverage": valid / eligible if eligible else None,
        "status_counts": dict(sorted(Counter(r["status"] for r in rows).items())),
        **error_stats([r["error_m"] for r in rows if r["error_m"] is not None]),
    }


def _by_method(rows: Sequence[dict]) -> Dict[str, dict]:
    return {name: _summary([r for r in rows if r["method"] == name]) for name in dict.fromkeys(r["method"] for r in rows)}


def _common(rows: Sequence[dict]) -> Optional[dict]:
    """Erros de cada método só nas tentativas em que todos os métodos produziram estimativa com referência."""
    errors: Dict[str, Dict[int, float]] = {}
    for r in rows:
        errors.setdefault(r["method"], {})
        if r["error_m"] is not None:
            errors[r["method"]][r["measurement_id"]] = r["error_m"]
    if len(errors) < 2:
        return None
    ids = sorted(set.intersection(*(set(e) for e in errors.values())))
    return {"n": len(ids), "measurement_ids": ids,
            "methods": {name: error_stats([e[i] for i in ids]) for name, e in errors.items()}}


def _registry(path: str):
    registry = load_registry(path)
    if not registry.valid:
        raise EvaluationError(f"invalid_registry: {registry.error}")
    return registry


def compare(db_path: str, model_path: str, registry_path: Optional[str] = None) -> dict:
    """WKNN (e Cell-ID, se houver cadastro) em todas as tentativas dos grupos de teste do bundle. Também
    mede o tempo de carga do bundle e a latência do WKNN por leitura (validação no hardware)."""
    start = time.perf_counter()
    model = load_bundle(model_path)
    load_s = time.perf_counter() - start
    bundle = model.bundle
    policy, info = bundle["reference_policy"], bundle["dataset"]
    if (not isinstance(policy, dict) or not isinstance(policy.get("allow_single_gnss"), bool)
            or not isinstance(info, dict) or not isinstance(info.get("test_measurement_ids"), list)
            or not isinstance(info.get("test_samples_sha256"), str)):
        raise EvaluationError("bundle_incompatible: reference_policy ou dataset sem os campos do teste")
    registry = _registry(registry_path) if registry_path else None
    dataset = load_dataset(db_path, allow_single_gnss=policy["allow_single_gnss"],
                           min_gnss_for_median=policy.get("min_gnss_for_median", MIN_GNSS_FOR_MEDIAN))
    by_id = {s.measurement_id: s for s in dataset.samples}
    missing = [i for i in info["test_measurement_ids"] if i not in by_id]
    if missing:
        raise EvaluationError(f"test_samples_missing: {len(missing)} medições de teste do bundle não estão no banco "
                              f"ou deixaram de ser elegíveis (ex.: {missing[:5]}); use o radiomapa do treino")
    test = [by_id[i] for i in info["test_measurement_ids"]]
    if samples_sha256(test) != info["test_samples_sha256"]:
        raise EvaluationError("test_samples_changed: as medições de teste ou suas referências mudaram desde o treino "
                              "(hash diferente; novas sessões no mesmo ponto alteram a mediana GNSS)")
    protocol = bundle["protocol"]
    groups = sorted({s.group(protocol) for s in test})
    attempts = [a for a in dataset.attempts if a.group(protocol) in set(groups)]
    known = bundle["training_groups"].get("reference_point_ids") if isinstance(bundle["training_groups"], dict) else None
    known = set(known) if isinstance(known, list) else None  # bundles anteriores não informam os pontos do treino
    rows, predict_s = [], []
    for attempt in attempts:
        estimate = unavailable_reading(WKNN, attempt.snapshot_status, attempt.csurv_status, model.technology)
        if estimate is None:
            start = time.perf_counter()
            estimate = model.predict(attempt.cells)
            predict_s.append(time.perf_counter() - start)
        rows.append(_row(attempt, estimate, wknn_eligible(attempt, model.technology), protocol, known))
        if registry is not None:
            rows.append(_row(attempt, _cell_id(registry, attempt), cell_id_eligible(attempt), protocol, known))
    by_type = None
    if known is not None:
        by_type = {kind: _by_method([r for r in rows if r["point_type"] == kind])
                   for kind in sorted({r["point_type"] for r in rows})}
    predict_s.sort()
    timings = {"load_bundle_s": load_s, "n_fingerprints": len(bundle["fingerprints"]["x_m"]),
               "n_cell_keys": len(bundle["cell_keys"]), "n_predictions": len(predict_s)}
    if predict_s:
        timings.update(wknn_predict_mean_s=sum(predict_s) / len(predict_s), wknn_predict_p95_s=percentile(predict_s, 95),
                       wknn_predict_max_s=predict_s[-1])
    warnings = split_warnings(protocol, groups) + reference_warnings(attempts, protocol)
    if known is not None:
        warnings += point_type_warnings(attempts, known, protocol)
    return {
        "software_version": software_version(), "model_software_version": bundle.get("software_version"),
        "protocol": protocol, "group_column": GROUP_COLUMN[protocol], "technology": bundle["technology"],
        "model": model_path, "registry": registry_path, "n_test_samples": len(test), "n_attempts": len(attempts),
        "test_groups": groups, "test_samples_sha256": info["test_samples_sha256"],
        "environments": dict(sorted(Counter(a.environment or "-" for a in attempts).items())),
        "reference": reference_summary(attempts), "methods": _by_method(rows), "common_subset": _common(rows),
        "by_point_type": by_type, "warnings": warnings, "timings_s": timings, "predictions": rows,
    }


def cell_id_report(db_path: str, registry_path: str, allow_single_gnss: bool = False) -> dict:
    """Cell-ID em todas as tentativas (não só nas amostras do WKNN), com o erro agrupado por célula servidora."""
    registry = _registry(registry_path)
    dataset = load_dataset(db_path, allow_single_gnss=allow_single_gnss)
    rows = [_row(a, _cell_id(registry, a), cell_id_eligible(a)) for a in dataset.attempts]
    by_cell: Dict[str, List[dict]] = {}
    for r in rows:
        by_cell.setdefault(r["serving_global_cell_key"] or "-", []).append(r)
    cells = []
    for key, items in by_cell.items():
        s = _summary(items)
        cells.append({"global_cell_key": key, "n": len(items), "n_valid": s["n_valid"], "n_errors": s["n_errors"],
                      "median_m": s["median_m"], "p90_m": s["p90_m"], "max_m": s["max_m"],
                      "status_counts": s["status_counts"]})
    cells.sort(key=lambda c: (c["median_m"] is None, -(c["median_m"] or 0.0), c["global_cell_key"]))
    seen = {r["serving_global_cell_key"] for r in rows}
    return {
        "software_version": software_version(), "registry": registry_path, "n_registered_cells": len(registry.entries),
        "registered_not_seen": sorted(set(registry.entries) - seen), "reference_policy": dataset.reference_policy(),
        "n_attempts": len(rows), "excluded_from_dataset": dataset.excluded,
        "environments": dict(sorted(Counter(a.environment or "-" for a in dataset.attempts).items())),
        "reference": reference_summary(dataset.attempts), "warnings": reference_warnings(dataset.attempts),
        "summary": _summary(rows), "cells": cells,
    }


# ----------------------------------------------------------------------------- CLI
def _fmt(value, spec=".1f") -> str:
    return "-" if value is None else format(value, spec)


def _table_rows(result: dict) -> List[dict]:
    """Uma linha por método e recorte: todas as tentativas, subconjunto comum e, no temporal ou com
    mais de um tipo de ponto, pontos conhecidos e pontos novos em linhas separadas."""
    rows = [{"method": m, "subset": SUBSET_ATTEMPTS, **s} for m, s in result["methods"].items()]
    if result["common_subset"] is not None:
        rows += [{"method": m, "subset": SUBSET_COMMON, **s} for m, s in result["common_subset"]["methods"].items()]
    by_type = result["by_point_type"]
    if by_type and (result["protocol"] == PROTOCOL_TEMPORAL or len(by_type) > 1):
        rows += [{"method": m, "subset": kind, **s} for kind, methods in by_type.items() for m, s in methods.items()]
    return rows


def _print_table(rows: List[dict]) -> None:
    print(f"{'método':<9}{'recorte':<14}{'tent.':>6}{'eleg.':>6}{'válid.':>7}{'c.oper.':>8}{'c.espec.':>9}"
          f"{'n erro':>7}{'média':>8}{'mediana':>9}{'RMSE':>8}{'p90':>8}{'p95':>8}{'máx':>8}  estados")
    for r in rows:
        states = ";".join(f"{k}={v}" for k, v in (r.get("status_counts") or {}).items())
        print(f"{r['method']:<9}{r['subset']:<14}{_fmt(r.get('n_attempts'), 'd'):>6}{_fmt(r.get('n_eligible'), 'd'):>6}"
              f"{_fmt(r.get('n_valid'), 'd'):>7}{_fmt(r.get('operational_coverage'), '.0%'):>8}"
              f"{_fmt(r.get('specific_coverage'), '.0%'):>9}{_fmt(r.get('n_errors'), 'd'):>7}{_fmt(r.get('mean_m')):>8}"
              f"{_fmt(r.get('median_m')):>9}{_fmt(r.get('rmse_m')):>8}{_fmt(r.get('p90_m')):>8}"
              f"{_fmt(r.get('p95_m')):>8}{_fmt(r.get('max_m')):>8}  {states}")


def _print_warnings(warnings: Sequence[str]) -> None:
    for warning in warnings:
        print(f"AVISO: {warning}")


def _write_csv(path: str, rows: List[dict], fields: Sequence[str]) -> None:
    with open(path, "x", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(fields), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: (";".join(f"{a}={b}" for a, b in v.items()) if isinstance(v, dict) else v)
                             for k, v in row.items()})


def _write_json(path: str, data: dict) -> None:
    with open(path, "x", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Avaliação do Estimador: Cell-ID e WKNN no mesmo teste e cadastro de células")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("compare", help="WKNN e Cell-ID em todas as tentativas dos grupos de teste do bundle")
    p.add_argument("--db", required=True, help="radiomapa v2 usado no treino (só leitura)")
    p.add_argument("--model", required=True, help="bundle WKNN (.json) gerado por estimador.train")
    p.add_argument("--registry", help="cadastro de células; sem ele, só o WKNN é avaliado")
    p.add_argument("--out", help="CSV com o resumo por método e recorte")
    p.add_argument("--predictions-out", dest="predictions_out", help="CSV com 1 linha por tentativa e método")
    p.add_argument("--json-out", dest="json_out", help="JSON completo (resumo, CDF e estimativas)")
    p = sub.add_parser("cell-id", help="confronta o cadastro com todas as tentativas")
    p.add_argument("--db", required=True)
    p.add_argument("--registry", required=True)
    p.add_argument("--allow-single-gnss", dest="allow_single_gnss", action="store_true",
                   help="usa a leitura GNSS isolada como referência das rotas")
    p.add_argument("--out", help="CSV com o erro por célula servidora")
    p.add_argument("--json-out", dest="json_out")
    args = parser.parse_args(argv)
    try:
        for path in (getattr(args, n, None) for n in ("out", "predictions_out", "json_out")):
            if path and os.path.exists(path):
                raise FileExistsError(f"{path}: já existe; escolha outro nome.")
        if args.command == "compare":
            result = compare(args.db, args.model, args.registry)
            ref = result["reference"]
            print(f"Protocolo: {result['protocol']} (grupo = {result['group_column']}); tecnologia {result['technology']}; "
                  f"software {result['software_version']} (treino: {result['model_software_version'] or '-'})")
            print(f"Teste: {len(result['test_groups'])} grupos {result['test_groups']}; {result['n_attempts']} tentativas, "
                  f"{result['n_test_samples']} amostras do treino; ambientes {result['environments']}")
            print(f"Referência: {ref['status']} {ref['sources']}; dispersão GNSS máx. {_fmt(ref['max_dispersion_m'])} m")
            rows = _table_rows(result)
            _print_table(rows)
            print("(c.oper. = válidas/tentativas; c.espec. = válidas/elegíveis; comum = tentativas em que os dois "
                  "métodos estimaram, único recorte em que os erros são comparáveis)")
            t = result["timings_s"]
            if t["n_predictions"]:
                print(f"Tempos: bundle carregado em {t['load_bundle_s']:.3f} s ({t['n_fingerprints']} fingerprints, "
                      f"{t['n_cell_keys']} radio_keys); WKNN por leitura: média {1000 * t['wknn_predict_mean_s']:.2f} ms, "
                      f"p95 {1000 * t['wknn_predict_p95_s']:.2f} ms, máx {1000 * t['wknn_predict_max_s']:.2f} ms")
            _print_warnings(result["warnings"])
            if args.out:
                context = {k: result[k] for k in ("protocol", "group_column", "software_version")}
                _write_csv(args.out, [{**r, **context} for r in rows],
                           ("method", "subset", "protocol", "group_column", "software_version") + COUNT_FIELDS
                           + ERROR_FIELDS + ("status_counts",))
            if args.predictions_out:
                _write_csv(args.predictions_out, result["predictions"], PREDICTION_FIELDS)
            if args.json_out:
                _write_json(args.json_out, result)
        else:
            result = cell_id_report(args.db, args.registry, args.allow_single_gnss)
            _print_table([{"method": CELL_ID, "subset": SUBSET_ATTEMPTS, **result["summary"]}])
            print(f"\n{'global_cell_key':<22}{'n':>5}{'válid.':>7}{'mediana':>10}{'p90':>10}{'máx':>10}  estados")
            for c in result["cells"]:
                states = ";".join(f"{k}={v}" for k, v in c["status_counts"].items())
                print(f"{c['global_cell_key']:<22}{c['n']:>5}{c['n_valid']:>7}{_fmt(c['median_m']):>10}"
                      f"{_fmt(c['p90_m']):>10}{_fmt(c['max_m']):>10}  {states}")
            if result["registered_not_seen"]:
                print(f"\nCadastradas e nunca servidoras nas tentativas: {', '.join(result['registered_not_seen'])}")
            print("\nErro mediano muito acima do raio esperado da célula sugere coordenada cadastrada errada.")
            _print_warnings(result["warnings"])
            if args.out:
                _write_csv(args.out, result["cells"], ("global_cell_key", "n", "n_valid", "n_errors", "median_m", "p90_m",
                                                       "max_m", "status_counts"))
            if args.json_out:
                _write_json(args.json_out, result)
    except (EvaluationError, DatasetError, BundleError, OSError, sqlite3.Error) as exc:
        raise SystemExit(f"erro: {exc}") from exc


if __name__ == "__main__":
    main()
