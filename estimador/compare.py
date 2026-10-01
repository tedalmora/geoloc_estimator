"""Compara os métodos de geolocalização no MESMO conjunto de pontos do radiomapa -- para gerar
as tabelas de resultado do Capítulo 5 do TCC de forma justa (nenhum método "ganha" só por ter
sido avaliado em pontos mais fáceis).

cell_id/ta usam o cadastro de posições conhecidas (methods/network_position.py) -- não têm
"treino", só são aplicados nos mesmos pontos de teste do fingerprint (que é o único que
precisa de um split treino/teste de verdade).

aoa não entra aqui (não usa o radiomapa) -- ver estimador/README.md para como avaliá-lo à
parte, com os mesmos números de erro (haversine/p90), usando pontos de referência manuais.

Uso (rodar a partir da pasta `codigo/`):
    python -m estimador.compare --db radiomapa.db --technology 5G --registry network_registry.json --out resultados_5g.csv
"""
import argparse
import sqlite3
from typing import Dict, List

import pandas as pd
from sklearn.model_selection import train_test_split

from .evaluate import evaluate_predictions
from .features import build_feature_vector, open_legacy_radiomap
from .methods.fingerprint import train_fingerprint
from .methods.network_position import estimate_cell_position, estimate_cell_position_ta, load_registry


def _load_measurements(conn: sqlite3.Connection, technology: str) -> pd.DataFrame:
    """1 única consulta, usada por TODOS os métodos -- garante que todos são avaliados
    exatamente nos mesmos pontos (nenhum método vê dados que os outros não veem)."""
    return pd.read_sql(
        "SELECT * FROM measurements WHERE technology = ? AND gnss_lat IS NOT NULL AND serving_pci IS NOT NULL "
        "ORDER BY measurement_id",
        conn, params=(technology,),
    )


def _load_neighbors(conn: sqlite3.Connection, measurement_ids: List[int]) -> Dict[int, List[dict]]:
    neighbors: Dict[int, List[dict]] = {}
    for mid in measurement_ids:
        rows = conn.execute(
            "SELECT pci, rsrp_dbm FROM neighbor_cells WHERE measurement_id = ?", (int(mid),)
        ).fetchall()
        neighbors[int(mid)] = [{"pci": pci, "rsrp_dbm": rsrp} for pci, rsrp in rows]
    return neighbors


def _vocabulary_from_training(treino: pd.DataFrame, neighbors_by_measurement: Dict[int, List[dict]]) -> List[int]:
    """Vocabulário do fingerprint construído SÓ com dados de treino -- incluir células vistas
    apenas no teste seria vazamento de informação."""
    pcis = set(treino["serving_pci"].dropna().astype(int).tolist())
    for mid in treino["measurement_id"]:
        for n in neighbors_by_measurement.get(int(mid), []):
            pcis.add(int(n["pci"]))
    return sorted(pcis)


def compare(db_path: str, technology: str, registry_path: str) -> pd.DataFrame:
    conn = open_legacy_radiomap(db_path)
    measurements = _load_measurements(conn, technology)
    if len(measurements) < 10:
        conn.close()
        raise SystemExit(f"Poucos pontos ({len(measurements)}) no radiomapa -- colete mais dados antes de comparar.")
    neighbors_by_measurement = _load_neighbors(conn, measurements["measurement_id"].tolist())
    conn.close()
    registry = load_registry(registry_path)

    # 1 único split -- todo método é avaliado exatamente nos mesmos pontos de teste.
    treino, teste = train_test_split(measurements, test_size=0.2, random_state=42)
    y_test = teste[["gnss_lat", "gnss_lon"]].values

    linhas = []

    # -- Cell-ID/E-CID: lookup direto no cadastro, sem ML (ver network_position.py)
    preds = [estimate_cell_position(pci, registry) for pci in teste["serving_pci"]]
    validos = [(p, y_test[i]) for i, p in enumerate(preds) if p is not None]
    if validos:
        preds_v, y_v = zip(*validos)
        m = evaluate_predictions(list(y_v), list(preds_v))
    else:
        m = evaluate_predictions([], [])
    m["cobertura"] = f"{len(validos)}/{len(teste)}"
    linhas.append({"metodo": "cell_id", **m})

    # -- Cell-ID + Timing Advance: mesmo cadastro + distância do TA, sem ML
    preds_ta = [
        estimate_cell_position_ta(r["serving_pci"], r["timing_advance"], technology, registry)
        for _, r in teste.iterrows()
    ]
    validos = [((p["lat"], p["lon"]), y_test[i]) for i, p in enumerate(preds_ta) if p is not None]
    if validos:
        preds_v, y_v = zip(*validos)
        m = evaluate_predictions(list(y_v), list(preds_v))
    else:
        m = evaluate_predictions([], [])
    m["cobertura"] = f"{len(validos)}/{len(teste)}"
    linhas.append({"metodo": "ta", **m})

    # -- Fingerprint: KNN/WKNN e Random Forest -- único método com treino de verdade
    fp_vocab = _vocabulary_from_training(treino, neighbors_by_measurement)
    X_train_fp = [
        build_feature_vector(r.to_dict(), neighbors_by_measurement[int(r["measurement_id"])], fp_vocab)
        for _, r in treino.iterrows()
    ]
    X_test_fp = [
        build_feature_vector(r.to_dict(), neighbors_by_measurement[int(r["measurement_id"])], fp_vocab)
        for _, r in teste.iterrows()
    ]
    y_train = treino[["gnss_lat", "gnss_lon"]]
    fp_bundle = train_fingerprint(pd.DataFrame(X_train_fp), y_train)
    preds_wknn = fp_bundle["wknn"].predict(fp_bundle["scaler"].transform(X_test_fp))
    preds_rf = fp_bundle["rf"].predict(pd.DataFrame(X_test_fp))

    m = evaluate_predictions(y_test, preds_wknn)
    m["cobertura"] = f"{len(teste)}/{len(teste)}"
    linhas.append({"metodo": "fingerprint_wknn", **m})

    m = evaluate_predictions(y_test, preds_rf)
    m["cobertura"] = f"{len(teste)}/{len(teste)}"
    linhas.append({"metodo": "fingerprint_rf", **m})

    tabela = pd.DataFrame(linhas)
    return tabela[["metodo", "n", "cobertura", "erro_medio_m", "erro_p90_m", "erro_max_m"]]


def main() -> None:
    parser = argparse.ArgumentParser(description="Compara cell_id/ta/fingerprint no mesmo conjunto de teste")
    parser.add_argument("--db", default="/data/radiomapa.db")
    parser.add_argument("--technology", required=True, choices=["4G", "5G"])
    parser.add_argument("--registry", default="estimador/network_registry.json")
    parser.add_argument("--out", default=None, help="caminho do .csv de saída (opcional)")
    args = parser.parse_args()

    tabela = compare(args.db, args.technology, args.registry)
    print(tabela.to_string(index=False))
    if args.out:
        tabela.to_csv(args.out, index=False)
        print(f"\nTabela salva em {args.out}")


if __name__ == "__main__":
    main()
