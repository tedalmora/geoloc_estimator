"""Export completo do banco v2 para CSV (backup versionável) e restauração num banco novo.

Uso (a partir de `codigo/`):
    python -m coletor.export_csv export --db radiomapa_v2.db --out backups/2026-09-30
    python -m coletor.export_csv restore --from backups/2026-09-30 --db restaurado.db

O export gera 1 CSV por tabela (reference_points, sessions, measurements, observed_cells,
health_checks) e um metadata.json com versão do schema, colunas, número de linhas e SHA-256 de
cada CSV. NULL vira \\N, para não se confundir com texto vazio. O banco de origem é aberto
somente leitura, e a pasta de saída precisa estar vazia ou não existir.

A restauração cria um banco v2 NOVO (recusa arquivo existente), confere SHA-256, colunas e
contagens e mantém os IDs originais. Se algo falhar, o arquivo parcial é removido.
"""
import argparse
import csv
import hashlib
import json
import os
import sqlite3

from acquisition.snapshot import utc_now_iso

from . import db

NULL = "\\N"
METADATA_FILE = "metadata.json"


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 16), b""):
            digest.update(block)
    return digest.hexdigest()


def export(db_path: str, out_dir: str) -> dict:
    if os.path.isdir(out_dir) and os.listdir(out_dir):
        raise FileExistsError(f"{out_dir}: a pasta de saída já tem arquivos; use uma pasta nova.")
    conn = db.open_read_only(db_path)
    try:
        version, _ = db.schema_info(conn)
        if version != db.SCHEMA_VERSION:
            raise db.SchemaError(f"{db_path}: export completo só para o schema v{db.SCHEMA_VERSION} (encontrado v{version}).")
        os.makedirs(out_dir, exist_ok=True)
        meta = {
            "schema_version": db.SCHEMA_VERSION, "exported_at": utc_now_iso(),
            "source_db": os.path.basename(db_path), "null_marker": NULL, "tables": {},
        }
        for table in db.TABLES:
            cur = conn.execute(f"SELECT * FROM {table} ORDER BY 1")
            columns = [d[0] for d in cur.description]
            path = os.path.join(out_dir, f"{table}.csv")
            rows = 0
            with open(path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(columns)
                for row in cur:
                    writer.writerow([NULL if value is None else value for value in row])
                    rows += 1
            meta["tables"][table] = {"file": f"{table}.csv", "columns": columns, "rows": rows, "sha256": sha256_file(path)}
    finally:
        conn.close()
    with open(os.path.join(out_dir, METADATA_FILE), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)
    return meta


def _converter(declared_type: str):
    declared_type = declared_type.upper()
    if "INT" in declared_type:
        return int
    if "REAL" in declared_type:
        return float
    return str


def restore(src_dir: str, db_path: str) -> dict:
    if os.path.exists(db_path):
        raise FileExistsError(f"{db_path}: já existe; a restauração só cria banco novo.")
    with open(os.path.join(src_dir, METADATA_FILE), encoding="utf-8") as f:
        meta = json.load(f)
    if meta.get("schema_version") != db.SCHEMA_VERSION:
        raise db.SchemaError(f"export do schema v{meta.get('schema_version')}; este código restaura v{db.SCHEMA_VERSION}.")
    for table in db.TABLES:
        info = meta["tables"][table]
        if sha256_file(os.path.join(src_dir, info["file"])) != info["sha256"]:
            raise ValueError(f"{info['file']}: SHA-256 não confere com o metadata.json (arquivo alterado).")

    conn = db.connect(db_path)
    try:
        with conn:
            for table in db.TABLES:
                info = meta["tables"][table]
                types = {r[1]: _converter(r[2]) for r in conn.execute(f"PRAGMA table_info({table})")}
                with open(os.path.join(src_dir, info["file"]), newline="", encoding="utf-8") as f:
                    reader = csv.reader(f)
                    header = next(reader)
                    if header != info["columns"] or set(header) != set(types):
                        raise ValueError(f"{info['file']}: colunas diferentes do schema v{db.SCHEMA_VERSION}.")
                    converters = [types[c] for c in header]
                    rows = [
                        [None if value == NULL else convert(value) for value, convert in zip(row, converters)]
                        for row in reader
                    ]
                if len(rows) != info["rows"]:
                    raise ValueError(f"{info['file']}: {len(rows)} linhas, metadata.json diz {info['rows']}.")
                conn.executemany(
                    f"INSERT INTO {table} ({', '.join(header)}) VALUES ({', '.join('?' * len(header))})", rows
                )
        counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in db.TABLES}
    except (sqlite3.Error, ValueError):
        conn.close()
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(db_path + suffix):
                os.remove(db_path + suffix)
        raise
    conn.close()
    return counts


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Export completo (CSV) e restauração do banco v2 do Coletor")
    sub = parser.add_subparsers(dest="command", required=True)
    p_exp = sub.add_parser("export")
    p_exp.add_argument("--db", required=True)
    p_exp.add_argument("--out", required=True, help="pasta nova (ou vazia) para os CSVs")
    p_res = sub.add_parser("restore")
    p_res.add_argument("--from", dest="src", required=True, help="pasta de um export")
    p_res.add_argument("--db", required=True, help="banco novo a criar")
    args = parser.parse_args(argv)
    try:
        if args.command == "export":
            meta = export(args.db, args.out)
            for table, info in meta["tables"].items():
                print(f"{table}: {info['rows']} linhas -> {info['file']}")
        else:
            for table, rows in restore(args.src, args.db).items():
                print(f"{table}: {rows} linhas restauradas")
    except (db.SchemaError, FileExistsError, FileNotFoundError, ValueError) as exc:
        raise SystemExit(f"erro: {exc}") from exc


if __name__ == "__main__":
    main()
