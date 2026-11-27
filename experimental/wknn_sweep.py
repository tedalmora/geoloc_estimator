"""Varredura offline de hiperparametros do WKNN com os dados da campanha (somente leitura).

Uso: python -m experimental.wknn_sweep <diretorio de resultados da coleta>
Compara cada configuracao em tres testes de ponto conhecido: treino S1 -> teste S2, treino S2 -> teste S1 e
leituras reais de avaliacoes/validation (modelo treinado com todas as amostras).
"""
import csv, glob, json, math, statistics, sys
from itertools import product

from estimador.dataset import csurv_cell, load_dataset
from estimador.features import CellFilterPolicy, MissingPolicy, apply_scaling, fit_scaling, vectorize
from estimador.methods.fingerprint import distance, wknn_from_sorted
from estimador.train import eligible_samples, fit_features

POLICY = CellFilterPolicy("LTE", "72406")


def errors(train, queries, k, metric, scaling, missing):
    fitted = fit_features(train, POLICY, MissingPolicy.parse(missing))
    params = fit_scaling(fitted.raw, scaling)
    vectors = [apply_scaling(v, params) for v in fitted.raw]
    xs, ys = [p[0] for p in fitted.xy], [p[1] for p in fitted.xy]
    out = []
    for cells, (lat, lon) in queries:
        vec, n_observed = vectorize(cells, POLICY, fitted.cell_keys, fitted.missing_value)
        if not n_observed:
            continue
        q = apply_scaling(vec, params)
        ordered = sorted((distance(q, f, metric), i) for i, f in enumerate(vectors))
        x, y, _, _ = wknn_from_sorted(ordered, xs, ys, min(k, len(vectors)))
        qx, qy = fitted.frame.to_enu(lat, lon)[:2]
        out.append(math.hypot(x - qx, y - qy))
    return out


def validation_queries(root):
    truth = {(r["true_point"], r["repetition"]): (float(r["true_lat"]), float(r["true_lon"]))
             for r in csv.DictReader(open(root + "/avaliacoes/validation/predictions.csv"))}
    queries = []
    for path in sorted(glob.glob(root + "/avaliacoes/validation/fingerprints/*.json")):
        data = json.load(open(path))
        cells = tuple(csurv_cell(o["radio_key"], o["rat"], o.get("plmn"), o.get("cell_status"), o.get("rsrp_dbm"))
                      for o in data["observations"] if o.get("source") == "CSURV" and o.get("radio_key"))
        queries.append((cells, truth[(data["point"], str(data["repetition"]))]))
    return queries


def sweep(samples, validation, grid):
    s1 = [s for s in samples if s.session_label == "S1"]
    s2 = [s for s in samples if s.session_label == "S2"]
    as_query = lambda items: [(s.cells, (s.reference.lat, s.reference.lon)) for s in items]
    rows = []
    for k, metric, scaling, missing in grid:
        a = errors(s1, as_query(s2), k, metric, scaling, missing)
        b = errors(s2, as_query(s1), k, metric, scaling, missing)
        c = errors(samples, validation, k, metric, scaling, missing)
        means = [statistics.fmean(x) if x else float("nan") for x in (a, b, c)]
        rows.append((statistics.fmean(means), means, (k, metric, scaling, missing), len(a), len(b), len(c)))
    return sorted(rows, key=lambda r: r[0])


def main(root):
    dataset = load_dataset(root + "/campanha/radiomapa.db")
    samples, _ = eligible_samples(dataset.samples, "temporal", POLICY)
    grid = list(product((1, 2, 3, 4, 5, 7, 9), ("L1", "L2"), ("raw", "standardized"),
                        ("sentinel:-140", "floor_offset:10")))
    print("media  S1>S2  S2>S1  valid.  k metrica escala ausencia (n)")
    for mean, (a, b, c), cfg, na, nb, nc in sweep(samples, validation_queries(root), grid)[:20]:
        print("%5.2f %6.2f %6.2f %6.2f  %d %s %s %s (%d,%d,%d)" % (mean, a, b, c, *cfg, na, nb, nc))


if __name__ == "__main__":
    main(sys.argv[1])
