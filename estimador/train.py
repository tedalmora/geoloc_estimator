"""Treino offline (batch) do Fingerprint -- o ÚNICO método deste projeto que precisa de
aprendizado de máquina. Os outros 4 (cell_id, ta, ecid_rede, aoa) são cálculo direto sobre o
cadastro de posições conhecidas (methods/network_position.py) e não têm o que "treinar" --
ver estimador/README.md.

Uso (rodar a partir da pasta `codigo/`):
    python -m estimador.train --db radiomapa.db --technology 5G --out modelos/fingerprint_5g.joblib
"""
import argparse
import os
import sqlite3

import joblib
from sklearn.model_selection import train_test_split

from .evaluate import evaluate_predictions
from .features import build_training_table
from .methods.fingerprint import train_fingerprint


def train(db_path: str, technology: str, out_path: str) -> None:
    conn = sqlite3.connect(db_path)
    X, y, vocabulary = build_training_table(conn, technology)
    conn.close()
    if len(X) < 10:
        raise SystemExit(f"Poucos pontos ({len(X)}) para o Fingerprint -- colete mais dados com o Coletor.")

    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)
    bundle = train_fingerprint(X_train, y_train)
    x_test_norm = bundle["scaler"].transform(X_test)
    print("Fingerprint WKNN:", evaluate_predictions(y_test.values, bundle["wknn"].predict(x_test_norm)))
    print("Fingerprint RF:", evaluate_predictions(y_test.values, bundle["rf"].predict(X_test)))

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    # scaler/vocabulário viajam junto com o modelo -- uma leitura nova só pode virar vetor
    # usando a MESMA ordem de colunas usada aqui no treino.
    joblib.dump({"technology": technology, "vocabulary": vocabulary, **bundle}, out_path)
    print(f"Modelo salvo em {out_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Treino offline do Fingerprint")
    parser.add_argument("--db", default="/data/radiomapa.db")
    parser.add_argument("--technology", required=True, choices=["4G", "5G"])
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    train(args.db, args.technology, args.out)


if __name__ == "__main__":
    main()


