"""Read-only final check of one prepared collection; never touches the modem, GNSS or database contents."""

import argparse
import sqlite3
import sys
from collections import Counter
from pathlib import Path

from coletor import db as coletor_db

from . import config, prepare
from .reference import read_canonical

SESSIONS = ("S1", "S2")
MEASUREMENTS_PER_SESSION = 3
SNAPSHOT_STATUSES = ("complete", "partial", "invalid")
NOTES_PREFIX = "campaign_tools"
EXIT_OK, EXIT_ERROR, EXIT_REVIEW = 0, 1, 2


def load_target(campaign, config_path=None):
    path = config.find_config(config_path)
    values = {} if path is None else config.load_config(path)
    return prepare.load_collection(prepare.resolve_collection(campaign, values.get("data_dir")))


def _pending_wal(db_path):
    wal = Path(f"{db_path}-wal")
    return wal.is_file() and wal.stat().st_size > 0


def _open_read_only(db_path):
    # immutable=1 avoids creating -wal/-shm files next to a database that is not being written.
    return sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro&immutable=1", uri=True)


def _visit_label(notes):
    parts = (notes or "").split(":")
    if len(parts) == 4 and parts[0] == NOTES_PREFIX:
        return parts[2], parts[3]
    return None


def _read_database(db_path, errors):
    path = Path(db_path)
    if not path.is_file():
        errors.append(f"banco ausente: {path}")
        return None
    if path.stat().st_size == 0:
        errors.append(f"banco vazio: {path}")
        return None
    if _pending_wal(path):
        # Reading pending WAL frames would require SQLite to create a -shm file; the coletor
        # checkpoints and removes the WAL on a normal close, so this means it is still running.
        errors.append(f"banco com WAL pendente: {path}-wal; encerre o coletor e repita o check")
        return None
    conn = None
    try:
        conn = _open_read_only(path)
        version, tables = coletor_db.schema_info(conn)
        if version != coletor_db.SCHEMA_VERSION:
            errors.append(f"schema versao {version}; esperada {coletor_db.SCHEMA_VERSION}")
            return None
        missing = sorted(set(coletor_db.TABLES) - tables)
        if missing:
            errors.append(f"tabelas ausentes: {', '.join(missing)}")
            return None
        integrity = [row[0] for row in conn.execute("PRAGMA integrity_check")]
        if integrity != ["ok"]:
            errors.append(f"integrity_check: {'; '.join(integrity[:5])}")
            return None
        foreign = conn.execute("PRAGMA foreign_key_check").fetchall()
        if foreign:
            errors.append(f"foreign_key_check: {len(foreign)} violacoes (tabela {foreign[0][0]})")
        names = {row[0] for row in conn.execute("SELECT name FROM reference_points")}
        sessions = conn.execute(
            "SELECT s.session_id, s.status, s.notes, rp.name FROM sessions s "
            "LEFT JOIN reference_points rp ON rp.reference_point_id = s.reference_point_id "
            "ORDER BY s.session_id"
        ).fetchall()
        snapshots = {}
        for session_id, status, count in conn.execute(
            "SELECT session_id, snapshot_status, COUNT(*) FROM measurements GROUP BY session_id, snapshot_status"
        ):
            snapshots.setdefault(session_id, Counter())[status] += count
        return {"points": names, "sessions": sessions, "snapshots": snapshots}
    except sqlite3.Error as exc:
        errors.append(f"banco ilegivel: {exc}")
        return None
    finally:
        if conn is not None:
            conn.close()


def check_collection(collection):
    points = collection["points"]
    results = Path(collection["results"])
    db_errors, acquisition_errors, reference_errors, evidence_errors = [], [], [], []
    warnings, invalid_snapshots = [], []
    totals = Counter()
    found_sessions = 0
    data = _read_database(collection["db"], db_errors)
    if data is not None:
        missing_points = [point for point in points if point not in data["points"]]
        if missing_points:
            acquisition_errors.append(f"pontos ausentes no banco: {', '.join(missing_points)}")
        found_sessions = len(data["sessions"])
        visits = {}
        for session_id, status, notes, name in data["sessions"]:
            counts = data["snapshots"].get(session_id, Counter())
            totals.update(counts)
            label = _visit_label(notes)
            if label is None or label[0] != name or label[0] not in points or label[1] not in SESSIONS:
                acquisition_errors.append(f"sessao {session_id} inesperada ({name}, {notes!r})")
                continue
            visits.setdefault(label, []).append((session_id, status, counts))
        for point in points:
            for session in SESSIONS:
                entries = visits.get((point, session), [])
                if not entries:
                    acquisition_errors.append(f"{point}/{session}: sessao ausente")
                    continue
                if len(entries) > 1:
                    acquisition_errors.append(f"{point}/{session}: {len(entries)} sessoes; esperada 1")
                for session_id, status, counts in entries:
                    if status != "completed":
                        acquisition_errors.append(f"{point}/{session}: sessao {session_id} com status {status}")
                    measured = sum(counts.values())
                    if measured != MEASUREMENTS_PER_SESSION:
                        acquisition_errors.append(
                            f"{point}/{session}: {measured} medicoes; esperadas {MEASUREMENTS_PER_SESSION}"
                        )
                    if counts["invalid"]:
                        invalid_snapshots.append(f"{point}/{session}: {counts['invalid']} snapshots invalid")
                    if counts["partial"]:
                        warnings.append(f"{point}/{session}: {counts['partial']} partial")

    try:
        canonical = read_canonical(collection["canonical"])
    except (OSError, ValueError) as exc:
        reference_errors.append(f"referencias canonicas: {exc}")
    else:
        missing = [point for point in points if point not in canonical]
        extra = sorted(set(canonical) - set(points))
        if missing:
            reference_errors.append(f"referencias canonicas sem: {', '.join(missing)}")
        if extra:
            reference_errors.append(f"referencias canonicas com pontos fora da coleta: {', '.join(extra)}")
    for point in points:
        for session in SESSIONS:
            fixes = results / "referencias" / f"{point}_{session}_fixes.csv"
            if not fixes.is_file():
                reference_errors.append(f"fixes ausentes: {fixes}")
            log = results / "logs" / f"{point}_{session}.log"
            if not log.is_file():
                evidence_errors.append(f"log ausente: {log}")
    for name in ("manifesto_campanha.txt", "metricas_repouso.csv", "metricas_sistema.csv"):
        if not (results / name).is_file():
            evidence_errors.append(f"evidencia ausente: {results / name}")

    return {
        "name": collection["name"], "points": points,
        "expected_sessions": len(points) * len(SESSIONS), "found_sessions": found_sessions,
        "expected_measurements": len(points) * len(SESSIONS) * MEASUREMENTS_PER_SESSION,
        "found_measurements": sum(totals.values()), "snapshots": totals,
        "db_errors": db_errors, "acquisition_errors": acquisition_errors,
        "reference_errors": reference_errors, "evidence_errors": evidence_errors,
        "invalid_snapshots": invalid_snapshots, "warnings": warnings,
    }


def _errors(report):
    return (report["db_errors"] + report["acquisition_errors"] + report["reference_errors"] +
            report["evidence_errors"] + report["invalid_snapshots"])


def verdict(report):
    errors = _errors(report)
    if errors:
        return EXIT_ERROR, f"ERRO ({len(errors)} problemas)"
    if report["snapshots"]["partial"]:
        return EXIT_REVIEW, f"REVISAR ({report['snapshots']['partial']} snapshots partial)"
    return EXIT_OK, "OK"


def print_report(report, stream=None):
    code, result = verdict(report)
    lines = [
        f"Coleta: {report['name']}",
        f"Pontos planejados: {len(report['points'])}",
        f"Sessões esperadas: {report['expected_sessions']}",
        f"Sessões encontradas: {report['found_sessions']}",
        f"Medições esperadas: {report['expected_measurements']}",
        f"Medições encontradas: {report['found_measurements']}",
        *(f"{status.capitalize()}: {report['snapshots'][status]}" for status in SNAPSHOT_STATUSES),
        f"Banco: {'com problemas' if report['db_errors'] else 'íntegro'}",
        f"Aquisição: {'incompleta' if report['acquisition_errors'] else 'completa'}",
        f"Referências: {'incompletas' if report['reference_errors'] else 'completas'}",
        f"Evidências: {'incompletas' if report['evidence_errors'] else 'completas'}",
    ]
    for message in _errors(report):
        lines.append(f"ERRO: {message}")
    for message in report["warnings"]:
        lines.append(f"AVISO: {message}; revisao manual")
    lines.append(f"Resultado: {result}")
    print("\n".join(lines), file=stream or sys.stdout)
    return code


def _main(argv=None):
    parser = argparse.ArgumentParser(
        description="Verifica, somente leitura, se a aquisicao de uma coleta preparada esta completa.",
        epilog="Saida: 0 = OK; 2 = REVISAR (snapshots partial); 1 = ERRO (inclui snapshots invalid).",
    )
    parser.add_argument("--campaign", required=True, help="nome da coleta ou caminho do collection.conf")
    parser.add_argument("--config", help="arquivo tcc-location.conf")
    args = parser.parse_args(argv)
    try:
        collection = load_target(args.campaign, args.config)
    except (OSError, ValueError) as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        print("Resultado: ERRO")
        return EXIT_ERROR
    return print_report(check_collection(collection))


if __name__ == "__main__":
    raise SystemExit(_main())
