"""Métricas de erro e avaliação do Estimador (Cell-ID e WKNN), só com a biblioteca padrão.

Métricas (`error_summary`): cobertura = estimativas válidas / leituras avaliadas; média,
mediana (CE50), RMSE, p90, p95, máximo e CDF (erros ordenados); percentis por interpolação
linear. O erro é a distância haversine até a posição de referência da amostra.

Subcomandos (a partir de `codigo/`):
    python -m estimador.evaluate compare --db radiomapa_v2.db --model modelos/wknn_lte_spatial.json \\
        [--registry estimador/network_registry.json] [--out resumo.csv] [--predictions-out estimativas.csv] [--json-out resumo.json]
    python -m estimador.evaluate cell-id --db radiomapa_v2.db --registry estimador/network_registry.json [--out celulas.csv]
    python -m estimador.evaluate live --db estimador_estimativas.db [--out resumo_live.csv]

compare: refaz o dataset com a política de referência gravada no bundle, usa exatamente as
medições de teste do bundle (`dataset.test_measurement_ids`) e confere `dataset.test_samples_sha256`;
se o banco mudou, recusa. WKNN e Cell-ID são avaliados nas mesmas amostras, e a saída registra o
protocolo (spatial ou temporal) e a coluna de grupo: resultados de protocolos diferentes não são
equivalentes.
cell-id: confronta o cadastro com todas as amostras rotuladas, com o erro por célula servidora.
live: resumo por método do que `estimador.live` gravou em position_estimates (no modo `once`,
cada linha já é a média de N leituras).
"""
import argparse
import csv
import json
import math
import os
import sqlite3
from collections import Counter
from typing import Dict, List, Optional, Sequence

from coletor.db import open_read_only

from .dataset import GROUP_COLUMN, MIN_GNSS_FOR_MEDIAN, DatasetError, Sample, load_dataset, samples_sha256
from .estimate import PositionEstimate
from .geometry import haversine_m
from .methods.cell_id import estimate_cell_id, load_registry
from .methods.fingerprint import BundleError, load_bundle

SUMMARY_FIELDS = ("n", "n_total", "coverage", "mean_m", "median_m", "rmse_m", "p90_m", "p95_m", "max_m")
PREDICTION_FIELDS = ("protocol", "group_id", "measurement_id", "session_id", "reference_point_id",
                     "serving_global_cell_key", "method", "status", "lat", "lon", "ref_lat", "ref_lon",
                     "reference_source", "error_m")


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


def _row(sample: Sample, estimate: PositionEstimate, protocol: Optional[str] = None) -> dict:
    ref = sample.reference
    return {
        "protocol": protocol, "group_id": sample.group(protocol) if protocol else None,
        "measurement_id": sample.measurement_id, "session_id": sample.session_id,
        "reference_point_id": sample.reference_point_id, "serving_global_cell_key": sample.serving_global_cell_key,
        "method": estimate.method, "status": estimate.status, "lat": estimate.lat, "lon": estimate.lon,
        "ref_lat": ref.lat, "ref_lon": ref.lon, "reference_source": ref.source,
        "error_m": haversine_m(estimate.lat, estimate.lon, ref.lat, ref.lon) if estimate.valid else None,
    }


def _summary(rows: List[dict], n_total: int) -> dict:
    errors = [r["error_m"] for r in rows if r["error_m"] is not None]
    return {**error_summary(errors, n_total), "status_counts": dict(sorted(Counter(r["status"] for r in rows).items()))}


def _registry(path: str):
    registry = load_registry(path)
    if not registry.valid:
        raise EvaluationError(f"invalid_registry: {registry.error}")
    return registry


def compare(db_path: str, model_path: str, registry_path: Optional[str] = None) -> dict:
    """WKNN (e Cell-ID, se houver cadastro) nas medições de teste registradas no bundle."""
    model = load_bundle(model_path)
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
    rows = []
    for sample in test:
        rows.append(_row(sample, model.predict(sample.cells), protocol))
        if registry is not None:
            rows.append(_row(sample, estimate_cell_id(registry, sample.serving_global_cell_key), protocol))
    methods = {name: _summary([r for r in rows if r["method"] == name], len(test))
               for name in dict.fromkeys(r["method"] for r in rows)}
    return {
        "protocol": protocol, "group_column": GROUP_COLUMN[protocol], "technology": bundle["technology"],
        "model": model_path, "registry": registry_path, "n_test_samples": len(test),
        "test_groups": sorted({s.group(protocol) for s in test}), "test_samples_sha256": info["test_samples_sha256"],
        "methods": methods, "predictions": rows,
    }


def cell_id_report(db_path: str, registry_path: str, allow_single_gnss: bool = False) -> dict:
    """Cell-ID em todas as amostras rotuladas, com o erro agrupado por célula servidora."""
    registry = _registry(registry_path)
    dataset = load_dataset(db_path, allow_single_gnss=allow_single_gnss)
    rows = [_row(s, estimate_cell_id(registry, s.serving_global_cell_key)) for s in dataset.samples]
    by_cell: Dict[str, List[dict]] = {}
    for r in rows:
        by_cell.setdefault(r["serving_global_cell_key"] or "-", []).append(r)
    cells = []
    for key, items in by_cell.items():
        summary = _summary(items, len(items))
        cells.append({"global_cell_key": key, "n": len(items), "status": items[0]["status"],
                      "median_m": summary["median_m"], "p90_m": summary["p90_m"], "max_m": summary["max_m"]})
    cells.sort(key=lambda c: (c["median_m"] is None, -(c["median_m"] or 0.0), c["global_cell_key"]))
    seen = {r["serving_global_cell_key"] for r in rows}
    return {
        "registry": registry_path, "n_registered_cells": len(registry.entries),
        "registered_not_seen": sorted(set(registry.entries) - seen), "reference_policy": dataset.reference_policy(),
        "excluded_from_dataset": dataset.excluded, "summary": _summary(rows, len(rows)), "cells": cells,
    }


def live_report(db_path: str) -> dict:
    """Resumo por método das estimativas gravadas por `estimador.live` (somente leitura)."""
    try:
        conn = open_read_only(db_path)
    except FileNotFoundError as exc:
        raise EvaluationError(f"live_db_not_found: {exc}") from exc
    try:
        rows = conn.execute("SELECT metodo, lat, erro_m FROM position_estimates ORDER BY id").fetchall()
    except sqlite3.DatabaseError as exc:
        raise EvaluationError(f"live_db_invalid: {db_path} não tem position_estimates ({exc})") from exc
    finally:
        conn.close()
    if not rows:
        raise EvaluationError(f"no_estimates: {db_path} não tem estimativas; rode estimador.live antes")
    methods = {}
    for name in dict.fromkeys(r[0] for r in rows):
        items = [r for r in rows if r[0] == name]
        n_valid = sum(1 for r in items if r[1] is not None)
        summary = error_summary([r[2] for r in items if r[2] is not None])
        methods[name] = {**summary, "n_total": len(items), "n_valid": n_valid, "coverage": n_valid / len(items)}
    return {"db": db_path, "n_rows": len(rows), "methods": methods}


# ----------------------------------------------------------------------------- CLI
def _fmt(value, spec=".1f") -> str:
    return "-" if value is None else format(value, spec)


def _print_table(rows: List[dict], first: str) -> None:
    print(f"{first:<18}{'n/total':>10}{'cobert.':>9}{'média':>9}{'mediana':>9}{'RMSE':>9}{'p90':>9}{'p95':>9}{'máx':>9}  estados")
    for r in rows:
        states = ";".join(f"{k}={v}" for k, v in r.get("status_counts", {}).items())
        print(f"{r[first]:<18}{str(r['n']) + '/' + str(r['n_total']):>10}{_fmt(r['coverage'], '.0%'):>9}"
              f"{_fmt(r['mean_m']):>9}{_fmt(r['median_m']):>9}{_fmt(r['rmse_m']):>9}{_fmt(r['p90_m']):>9}"
              f"{_fmt(r['p95_m']):>9}{_fmt(r['max_m']):>9}  {states}")


def _summary_rows(methods: dict, **extra) -> List[dict]:
    rows = []
    for name, s in methods.items():
        row = {"method": name, **extra, **{k: s.get(k) for k in SUMMARY_FIELDS}}
        if "status_counts" in s:
            row["status_counts"] = s["status_counts"]
        if "n_valid" in s:
            row["n_valid"] = s["n_valid"]
        rows.append(row)
    return rows


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
    parser = argparse.ArgumentParser(description="Avaliação do Estimador: Cell-ID e WKNN no mesmo teste, cadastro e medições ao vivo")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("compare", help="WKNN e Cell-ID nas medições de teste registradas no bundle")
    p.add_argument("--db", required=True, help="radiomapa v2 usado no treino (só leitura)")
    p.add_argument("--model", required=True, help="bundle WKNN (.json) gerado por estimador.train")
    p.add_argument("--registry", help="cadastro de células; sem ele, só o WKNN é avaliado")
    p.add_argument("--out", help="CSV com o resumo por método")
    p.add_argument("--predictions-out", dest="predictions_out", help="CSV com 1 linha por amostra e método")
    p.add_argument("--json-out", dest="json_out", help="JSON completo (resumo, CDF e estimativas)")
    p = sub.add_parser("cell-id", help="confronta o cadastro com todas as amostras rotuladas")
    p.add_argument("--db", required=True)
    p.add_argument("--registry", required=True)
    p.add_argument("--allow-single-gnss", dest="allow_single_gnss", action="store_true",
                   help="inclui rotas com leitura GNSS isolada como referência")
    p.add_argument("--out", help="CSV com o erro por célula servidora")
    p.add_argument("--json-out", dest="json_out")
    p = sub.add_parser("live", help="resumo por método do que estimador.live gravou")
    p.add_argument("--db", default="estimador_estimativas.db")
    p.add_argument("--out", help="CSV com o resumo por método")
    args = parser.parse_args(argv)
    try:
        for path in (getattr(args, n, None) for n in ("out", "predictions_out", "json_out")):
            if path and os.path.exists(path):
                raise FileExistsError(f"{path}: já existe; escolha outro nome.")
        if args.command == "compare":
            result = compare(args.db, args.model, args.registry)
            print(f"Protocolo: {result['protocol']} (grupo = {result['group_column']}); tecnologia "
                  f"{result['technology']}; teste: {result['n_test_samples']} medições em {len(result['test_groups'])} "
                  f"grupos {result['test_groups']}")
            summary = _summary_rows(result["methods"], protocol=result["protocol"], group_column=result["group_column"])
            _print_table(summary, "method")
            if args.out:
                _write_csv(args.out, summary, ("method", "protocol", "group_column") + SUMMARY_FIELDS + ("status_counts",))
            if args.predictions_out:
                _write_csv(args.predictions_out, result["predictions"], PREDICTION_FIELDS)
            if args.json_out:
                _write_json(args.json_out, result)
        elif args.command == "cell-id":
            result = cell_id_report(args.db, args.registry, args.allow_single_gnss)
            _print_table(_summary_rows({"cell_id": result["summary"]}), "method")
            print(f"\n{'global_cell_key':<22}{'n':>5}{'mediana':>10}{'p90':>10}{'máx':>10}  estado")
            for c in result["cells"]:
                print(f"{c['global_cell_key']:<22}{c['n']:>5}{_fmt(c['median_m']):>10}{_fmt(c['p90_m']):>10}"
                      f"{_fmt(c['max_m']):>10}  {c['status']}")
            if result["registered_not_seen"]:
                print(f"\nCadastradas e nunca servidoras nas amostras: {', '.join(result['registered_not_seen'])}")
            print("\nErro mediano muito acima do raio esperado da célula sugere coordenada cadastrada errada.")
            if args.out:
                _write_csv(args.out, result["cells"], ("global_cell_key", "n", "status", "median_m", "p90_m", "max_m"))
            if args.json_out:
                _write_json(args.json_out, result)
        else:
            result = live_report(args.db)
            summary = _summary_rows(result["methods"])
            _print_table(summary, "method")
            print("(n = estimativas com posição real conhecida; cobertura = estimativas válidas / linhas gravadas)")
            if args.out:
                _write_csv(args.out, summary, ("method", "n_valid") + SUMMARY_FIELDS)
    except (EvaluationError, DatasetError, BundleError, FileExistsError, FileNotFoundError) as exc:
        raise SystemExit(f"erro: {exc}") from exc


if __name__ == "__main__":
    main()
