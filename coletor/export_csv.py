"""Exporta medições do SQLite para CSV -- backup versionável em Git.

Ver planejamento/arquitetura_coleta_dados.md, Seção 3, para o porquê desse backup existir.

Uso (rodar a partir da pasta `codigo/`):
    python -m coletor.export_csv --db radiomapa.db --session 3 --out sessao_3.csv
    python -m coletor.export_csv --db radiomapa.db --out tudo.csv   # sem --session = exporta tudo
"""
import argparse
import csv
import sqlite3
from typing import Optional


def export(db_path: str, out_path: str, session_id: Optional[int]) -> None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    if session_id is not None:
        rows = conn.execute("SELECT * FROM measurements WHERE session_id = ?", (session_id,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM measurements").fetchall()
    conn.close()

    if not rows:
        print("Nenhuma medição encontrada para exportar.")
        return
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(dict(row) for row in rows)
    print(f"{len(rows)} medições exportadas para {out_path}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="/data/radiomapa.db")
    parser.add_argument("--session", type=int, default=None)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    export(args.db, args.out, args.session)


if __name__ == "__main__":
    main()
