"""LEGADO (banco v1): compara cell_id/ta por PCI num sorteio de 20% das medições do radiomapa.

cell_id/ta usam o cadastro antigo por PCI (methods/network_position.py). O fingerprint saiu
deste script na fase 5 (o Random Forest foi removido e o WKNN agora é treinado e avaliado por
`estimador.train`, com validação agrupada).

Uso (rodar a partir da pasta `codigo/`):
    python -m estimador.compare --db radiomapa.db --technology 5G --registry network_registry.json --out resultados_5g.csv
"""
import argparse
import sqlite3

import pandas as pd

from .evaluate import evaluate_predictions
from .features import open_legacy_radiomap
from .methods.network_position import estimate_cell_position, estimate_cell_position_ta, load_registry


def _load_measurements(conn: sqlite3.Connection, technology: str) -> pd.DataFrame:
    """1 única consulta, usada por TODOS os métodos -- garante que todos são avaliados
    exatamente nos mesmos pontos (nenhum método vê dados que os outros não veem)."""
    return pd.read_sql(
        "SELECT * FROM measurements WHERE technology = ? AND gnss_lat IS NOT NULL AND serving_pci IS NOT NULL "
        "ORDER BY measurement_id",
        conn, params=(technology,),
    )


def compare(db_path: str, technology: str, registry_path: str) -> pd.DataFrame:
    conn = open_legacy_radiomap(db_path)
    measurements = _load_measurements(conn, technology)
    conn.close()
    if len(measurements) < 10:
        raise SystemExit(f"Poucos pontos ({len(measurements)}) no radiomapa -- colete mais dados antes de comparar.")
    registry = load_registry(registry_path)

    # Sorteio simples de 20% (legado; não é validação agrupada).
    teste = measurements.sample(frac=0.2, random_state=42)
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

    tabela = pd.DataFrame(linhas)
    return tabela[["metodo", "n", "cobertura", "erro_medio_m", "erro_p90_m", "erro_max_m"]]


def main() -> None:
    parser = argparse.ArgumentParser(description="LEGADO: compara cell_id/ta por PCI no banco v1")
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
