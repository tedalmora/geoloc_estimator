"""Inspeção e migração NÃO destrutiva de bancos v1 do Coletor para o schema v2.

Uso (a partir de `codigo/`):
    python -m coletor.migrate inspect --db radiomapa.db
    python -m coletor.migrate migrate --db radiomapa.db --out radiomapa_v2.db

O banco de origem é aberto somente leitura (URI mode=ro) e seu SHA-256 é conferido antes e
depois. A saída precisa ser um arquivo NOVO, diferente da origem; arquivo existente é recusado.

O v1 não guardava o suficiente para as identidades do v2, então nada é inventado:
- measurements: access_mode 'UNKNOWN' (o 'technology' do v1 era o rótulo configurado, não o
  medido), serving_radio_key/serving_global_cell_key vazios, PCI e Cell-ID copiados só como
  campos, legacy_ambiguous = 1 e campos sem equivalente em legacy_json. O started_at vem do
  timestamp epoch do v1, convertido para UTC;
- GNSS: só fix 2 (2D) ou 3 (3D) vira posição. O v1 aceitava fix 1 (inválido); essas coordenadas
  vão só para legacy_json;
- neighbor_cells -> observed_cells com fonte LEGACY_V1, RAT LTE (o parser v1 só lia registros
  'earfcn:' do #CSURV), sem ARFCN e por isso sem radio_key; identity_status 'legacy_ambiguous';
  RSRP 0 (valor não coletado do #CSURV) vira NULL;
- sessions: mode 'legacy_v1'. O horário do v1 era local e sem fuso; o texto original fica em
  started_at/ended_at e o fato é registrado em metadata_json;
- medições de sessão inexistente (o v1 não ligava foreign keys) ganham uma sessão substituta;
  células vizinhas sem medição não têm contexto e são só contadas no relatório;
- health_checks: sem sessão (o v1 não ligava a checagem à sessão), evento 'legacy_v1'.
"""
import argparse
import json
import os
from datetime import datetime, timezone

from acquisition.snapshot import utc_now_iso
from hal.models import canonical_hex

from . import db
from .export_csv import sha256_file

V1_TABLES = {"sessions", "measurements", "neighbor_cells", "health_checks"}


def _epoch_to_utc(value) -> str:
    return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _open_v1(db_path: str):
    conn = db.open_read_only(db_path)
    version, tables = db.schema_info(conn)
    if version != 0 or not V1_TABLES <= tables:
        conn.close()
        raise db.SchemaError(f"{db_path}: não é um banco v1 do Coletor (user_version={version}, tabelas={sorted(tables)}).")
    return conn


def inspect(db_path: str) -> dict:
    conn = _open_v1(db_path)
    try:
        q = lambda sql: conn.execute(sql).fetchall()  # noqa: E731
        report = {
            "arquivo": os.path.basename(db_path),
            "sha256": sha256_file(db_path),
            "contagens": {t: q(f"SELECT COUNT(*) FROM {t}")[0][0] for t in sorted(V1_TABLES)},
            "sessoes_sem_fim": q("SELECT COUNT(*) FROM sessions WHERE ended_at IS NULL")[0][0],
            "rotulos_technology_v1": dict(q("SELECT technology, COUNT(*) FROM measurements GROUP BY technology")),
            "gnss_fix_quality_v1": {str(k): v for k, v in q(
                "SELECT gnss_fix_quality, COUNT(*) FROM measurements WHERE gnss_lat IS NOT NULL GROUP BY gnss_fix_quality")},
            "medicoes_com_pci_sem_identidade_completa": q(
                "SELECT COUNT(*) FROM measurements WHERE serving_pci IS NOT NULL")[0][0],
            "vizinhas_rsrp_zero": q("SELECT COUNT(*) FROM neighbor_cells WHERE rsrp_dbm = 0")[0][0],
            "medicoes_orfas": q(
                "SELECT COUNT(*) FROM measurements WHERE session_id NOT IN (SELECT session_id FROM sessions)")[0][0],
            "vizinhas_orfas": q(
                "SELECT COUNT(*) FROM neighbor_cells WHERE measurement_id NOT IN (SELECT measurement_id FROM measurements)")[0][0],
        }
    finally:
        conn.close()
    report["na_migracao"] = (
        "todas as medições ficam legacy_ambiguous (sem radio_key/global_cell_key, access_mode UNKNOWN); "
        "coordenadas de fix diferente de 2/3 vão só para legacy_json; RSRP 0 vira NULL; "
        "vizinhas órfãs não são migradas"
    )
    return report


def migrate(src_path: str, out_path: str) -> dict:
    if os.path.abspath(src_path) == os.path.abspath(out_path):
        raise ValueError("a saída precisa ser um arquivo diferente da origem.")
    if os.path.exists(out_path):
        raise FileExistsError(f"{out_path}: já existe; a migração só cria arquivo novo.")
    sha_before = sha256_file(src_path)
    src = _open_v1(src_path)
    try:
        report = _copy(src, out_path, src_path, sha_before)
    finally:
        src.close()
    if sha256_file(src_path) != sha_before:  # garantia de não destruição
        raise RuntimeError(f"{src_path}: o arquivo de origem mudou durante a migração.")
    report["sha256_origem"] = sha_before
    return report


def _copy(src, out_path: str, src_path: str, sha_before: str) -> dict:
    dst = db.connect(out_path)
    migrated_at = utc_now_iso()
    provenance = {"migrado_de": os.path.basename(src_path), "sha256_origem": sha_before, "migrado_em": migrated_at,
                  "horario_v1": "local, sem fuso (texto original preservado)"}
    report = {"sessoes": 0, "sessoes_substitutas": 0, "medicoes": 0, "celulas": 0, "celulas_rsrp_zero_para_null": 0,
              "vizinhas_orfas_ignoradas": 0, "fix_invalido_v1_movido_para_legacy": 0, "health_checks": 0}
    try:
        with dst:
            modem_by_session = {}
            for sid, modem in src.execute("SELECT session_id, modem_id FROM measurements"):
                modem_by_session.setdefault(sid, set()).add(modem)
            sessions = {row[0]: row for row in src.execute(
                "SELECT session_id, started_at, ended_at, scenario, environment, notes FROM sessions")}
            for sid in sorted(set(sessions) | set(modem_by_session)):
                if sid in sessions:
                    _, started, ended, scenario, environment, notes = sessions[sid]
                    status = "completed" if ended else "interrupted"
                    reason = None if ended else "v1 sem ended_at (sessão não encerrada)"
                else:
                    first = src.execute("SELECT MIN(timestamp) FROM measurements WHERE session_id = ?", (sid,)).fetchone()[0]
                    started, ended, scenario, environment, notes = _epoch_to_utc(first), None, "desconhecido", "desconhecido", None
                    status, reason = "interrupted", "sessão ausente no v1 (medições órfãs)"
                    report["sessoes_substitutas"] += 1
                dst.execute(
                    "INSERT INTO sessions (session_id, started_at, ended_at, status, end_reason, mode, scenario, "
                    "environment, modem_id, notes, metadata_json) VALUES (?, ?, ?, ?, ?, 'legacy_v1', ?, ?, ?, ?, ?)",
                    (sid, started, ended, status, reason, scenario, environment,
                     ",".join(sorted(modem_by_session.get(sid, {"desconhecido"}))), notes,
                     json.dumps(provenance, ensure_ascii=False, sort_keys=True)),
                )
                report["sessoes"] += 1

            seq_by_session = {}
            columns = [d[1] for d in src.execute("PRAGMA table_info(measurements)")]
            for row in src.execute("SELECT * FROM measurements ORDER BY session_id, timestamp, measurement_id"):
                m = dict(zip(columns, row))
                seq = seq_by_session[m["session_id"]] = seq_by_session.get(m["session_id"], 0) + 1
                fix_ok = str(m.get("gnss_fix_quality")) in ("2", "3") and m.get("gnss_lat") is not None
                if m.get("gnss_lat") is not None and not fix_ok:
                    report["fix_invalido_v1_movido_para_legacy"] += 1
                legacy = {k: m.get(k) for k in ("technology", "modem_id", "timing_advance", "gnss_fix_quality",
                                                "lpp_raw_log", "timestamp")}
                if not fix_ok:
                    legacy.update(gnss_lat=m.get("gnss_lat"), gnss_lon=m.get("gnss_lon"), gnss_alt_m=m.get("gnss_alt_m"))
                dst.execute(
                    "INSERT INTO measurements (measurement_id, session_id, seq, started_at, snapshot_status, "
                    "reasons_json, access_mode, serving_method, serving_pci, serving_cell_id, serving_tac, "
                    "serving_rsrp_dbm, serving_rsrq_db, serving_sinr_db, moni_status, servinfo_status, csurv_status, "
                    "gnss_status, gnss_lat, gnss_lon, gnss_alt_m, gnss_fix_type, timings_json, anomalies_json, "
                    "raw_json, legacy_ambiguous, legacy_json) VALUES (?, ?, ?, ?, 'partial', ?, 'UNKNOWN', 'legacy_v1', "
                    "?, ?, ?, ?, ?, ?, 'legacy_v1', 'legacy_v1', 'legacy_v1', ?, ?, ?, ?, ?, '{}', '[]', '{}', 1, ?)",
                    (m["measurement_id"], m["session_id"], seq, _epoch_to_utc(m["timestamp"]),
                     json.dumps(["legacy_v1"]), m.get("serving_pci"), canonical_hex(m.get("serving_cell_id")),
                     canonical_hex(m.get("serving_tac")), m.get("rsrp_dbm"), m.get("rsrq_db"), m.get("sinr_db"),
                     "ok" if fix_ok else ("legacy_fix_invalido" if m.get("gnss_lat") is not None else "legacy_sem_fix"),
                     m["gnss_lat"] if fix_ok else None, m["gnss_lon"] if fix_ok else None,
                     m.get("gnss_alt_m") if fix_ok else None, int(m["gnss_fix_quality"]) if fix_ok else None,
                     json.dumps(legacy, ensure_ascii=False, sort_keys=True)),
                )
                report["medicoes"] += 1

            for _, measurement_id, pci, rsrp in src.execute(
                "SELECT id, measurement_id, pci, rsrp_dbm FROM neighbor_cells "
                "WHERE measurement_id IN (SELECT measurement_id FROM measurements) ORDER BY id"
            ):
                if rsrp == 0:
                    report["celulas_rsrp_zero_para_null"] += 1
                dst.execute(
                    "INSERT INTO observed_cells (measurement_id, source, rat, pci, rsrp_dbm, identity_status) "
                    "VALUES (?, ?, 'LTE', ?, ?, 'legacy_ambiguous')",
                    (measurement_id, db.SOURCE_LEGACY_V1, pci, None if rsrp == 0 else rsrp),
                )
                report["celulas"] += 1
            report["vizinhas_orfas_ignoradas"] = src.execute(
                "SELECT COUNT(*) FROM neighbor_cells WHERE measurement_id NOT IN (SELECT measurement_id FROM measurements)"
            ).fetchone()[0]

            for timestamp, modem_id, alive, note in src.execute(
                "SELECT timestamp, modem_id, alive, note FROM health_checks ORDER BY id"
            ):
                dst.execute(
                    "INSERT INTO health_checks (session_id, checked_at, modem_id, event, alive, detail) "
                    "VALUES (NULL, ?, ?, 'legacy_v1', ?, ?)",
                    (_epoch_to_utc(timestamp), modem_id, 1 if alive else 0, note or None),
                )
                report["health_checks"] += 1
    except Exception:
        dst.close()
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(out_path + suffix):
                os.remove(out_path + suffix)
        raise
    dst.close()
    return report


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Inspeção e migração não destrutiva do banco v1 do Coletor")
    sub = parser.add_subparsers(dest="command", required=True)
    p_ins = sub.add_parser("inspect")
    p_ins.add_argument("--db", required=True)
    p_mig = sub.add_parser("migrate")
    p_mig.add_argument("--db", required=True, help="banco v1 (só lido)")
    p_mig.add_argument("--out", required=True, help="banco v2 novo")
    args = parser.parse_args(argv)
    try:
        result = inspect(args.db) if args.command == "inspect" else migrate(args.db, args.out)
    except (db.SchemaError, FileExistsError, FileNotFoundError, ValueError) as exc:
        raise SystemExit(f"erro: {exc}") from exc
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
