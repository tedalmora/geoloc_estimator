"""Lê o que `estimador/live.py` salvou (modos `once` e `continuous`) e compara os métodos
entre si, usando o erro real (contra GNSS do modem ou `--gnss-real`) já calculado na hora.

Diferente de `estimador/compare.py` (que compara contra o radiomapa histórico do Coletor,
com split treino/teste), este aqui compara o que você **mediu de verdade em campo**, ao
vivo -- não precisa do Coletor, só do que `live.py` já gravou.

Uso (rodar a partir da pasta `codigo/`):
    python -m estimador.relatorio --db estimador_estimativas.db --out resultados_live.csv
"""
import argparse
import sqlite3

import pandas as pd


def relatorio(db_path: str) -> pd.DataFrame:
    conn = sqlite3.connect(db_path)
    dados = pd.read_sql(
        "SELECT metodo, erro_m FROM position_estimates WHERE erro_m IS NOT NULL", conn
    )
    conn.close()
    if dados.empty:
        raise SystemExit(
            "Nenhuma estimativa com posição real conhecida encontrada -- rode "
            "'estimador.live' com GNSS disponível ou '--gnss-real lat,lon' antes de comparar."
        )
    resumo = dados.groupby("metodo")["erro_m"].agg(
        n="count", erro_medio_m="mean", erro_p90_m=lambda s: s.quantile(0.9), erro_max_m="max"
    ).reset_index()
    return resumo.sort_values("erro_medio_m")


def main() -> None:
    parser = argparse.ArgumentParser(description="Compara os métodos a partir do que estimador.live já salvou")
    parser.add_argument("--db", default="estimador_estimativas.db")
    parser.add_argument("--out", default=None, help="caminho do .csv de saída (opcional)")
    args = parser.parse_args()

    tabela = relatorio(args.db)
    print(tabela.to_string(index=False))
    if args.out:
        tabela.to_csv(args.out, index=False)
        print(f"\nTabela salva em {args.out}")


if __name__ == "__main__":
    main()
