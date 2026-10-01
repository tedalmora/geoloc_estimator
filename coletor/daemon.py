"""Coletor: grava snapshots da aquisição compartilhada no banco v2 (coletor/db.py).

Síncrono: 1 processo, 1 modem, 1 conexão SQLite. Cada ciclo é take_snapshot + gravação. Toda
tentativa vira 1 linha em `measurements`, inclusive as inválidas (com os motivos). A sessão
sempre termina com status final (completed, interrupted ou error), mesmo com exceção.

Toda sessão de coleta exige um ponto de referência (`--reference-point`, medição parada num
ponto) ou uma rota (`--route`, medição em movimento). Ponto inexistente é criado sem
coordenada, com aviso; a coordenada cadastrada (point-add) é a referência de posição
prioritária. O GNSS do modem é guardado em toda medição como dado auxiliar.

Subcomandos (a partir de `codigo/`):
    python -m coletor.daemon status --port /dev/ttyUSB2
    python -m coletor.daemon once --port /dev/ttyUSB2 --db radiomapa_v2.db \\
        --scenario estatico --environment outdoor --reference-point P01 --campaign-plmn 72406
    python -m coletor.daemon continuous --port /dev/ttyUSB2 --db radiomapa_v2.db --interval 60 \\
        --scenario pedestre --environment outdoor --route trilha_1 [--max-measurements N] [--duration-s S]
    python -m coletor.daemon point-add --db radiomapa_v2.db P01 --lat -26.9 --lon -49.07 --coord-source levantamento
    python -m coletor.daemon point-list --db radiomapa_v2.db
"""
import argparse
import json
import math
import platform
import re
import signal
import sqlite3
import threading
import time
from typing import Optional

from acquisition.snapshot import Snapshot, take_snapshot
from hal.fn990_driver import FN990Driver
from hal.health import HealthMonitor
from hal.modem_driver import ModemDriver
from hal.models import to_jsonable

from . import db

DEFAULT_INTERVAL_S = 60.0
DEFAULT_HEALTH_INTERVAL_S = 600.0
DEVICE_MODEL_PATH = "/proc/device-tree/model"


def next_due(start: float, interval: float, now: float) -> float:
    """Próximo instante da grade monotônica start + k*interval estritamente depois de `now`.
    Ciclos que estouraram o intervalo pulam para o próximo horário da grade, sem acumular atraso."""
    if interval <= 0:
        return now
    return start + (math.floor((now - start) / interval) + 1) * interval


def snapshot_alive(snap: Snapshot) -> bool:
    """O modem respondeu nesse ciclo? Falso com erro de porta ou modem mudo."""
    return snap.transport_error is None and "modem_sem_resposta" not in snap.reasons


def _device_model() -> Optional[str]:
    """Modelo da placa lido em /proc/device-tree/model (verificação pendente do hardware)."""
    try:
        with open(DEVICE_MODEL_PATH, "rb") as f:
            return f.read().rstrip(b"\x00").decode("ascii", "replace").strip() or None
    except OSError:
        return None


def _session_metadata(args: argparse.Namespace, driver: ModemDriver, prepare) -> dict:
    status = driver.get_status()
    return {
        "schema_version": db.SCHEMA_VERSION,
        "port": args.port,
        "baudrate": args.baudrate,
        "interval_s": getattr(args, "interval", None),
        "lpp_probe": args.lpp_probe,
        "device_model": _device_model(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        # IMEI omitido de propósito: o banco e o export podem ser compartilhados.
        "modem": {
            "manufacturer": status.manufacturer, "model": status.model, "firmware": status.firmware,
            "sim_status": status.sim_status, "operator": status.operator,
        },
        "prepare": {
            "gnss_power_before": prepare.gnss_power_before,
            "gnss_power_on_sent": prepare.gnss_power_on_sent,
            "gnss_power_on_final": prepare.gnss_power_on_final,
        },
    }


def _open_modem(args: argparse.Namespace) -> ModemDriver:
    driver = FN990Driver()
    driver.connect(args.port, baudrate=args.baudrate)
    return driver


class _StopFlag:
    """Sinal de parada do modo contínuo (SIGINT/SIGTERM): termina depois do ciclo em andamento."""

    def __init__(self) -> None:
        self.event = threading.Event()
        self.reason: Optional[str] = None

    def __call__(self, signum, frame) -> None:
        self.reason = signal.Signals(signum).name
        self.event.set()
        print(f"{self.reason} recebido: encerrando depois do ciclo em andamento.")


def _install_handlers(stop: _StopFlag) -> dict:
    previous = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            previous[sig] = signal.signal(sig, stop)
        except (ValueError, OSError):  # fora da thread principal ou sinal indisponível na plataforma
            pass
    return previous


def _collect(conn, driver: ModemDriver, session_id: int, seq: int, point_id: Optional[int], lpp_probe: bool) -> Snapshot:
    snap = take_snapshot(driver, include_lpp_probe=lpp_probe)
    db.insert_snapshot(conn, session_id, seq, snap, point_id)
    motivos = f" motivos={','.join(snap.reasons)}" if snap.reasons else ""
    print(f"[{snap.started_at}] #{seq} {snap.status} {snap.access_mode} servidora={snap.serving.radio_key or '-'} "
          f"células={len(snap.observations)}{motivos}")
    return snap


def _loop(args, driver: ModemDriver, conn, session_id: int, point_id: Optional[int], stop: _StopFlag) -> bool:
    """Modo contínuo. Devolve True ao atingir --max-measurements/--duration-s e False se parado por sinal."""
    health = HealthMonitor()
    start = time.monotonic()
    last_health = start
    alive_before = True
    seq = 0
    while not stop.event.is_set():
        seq += 1
        snap = _collect(conn, driver, session_id, seq, point_id, args.lpp_probe)
        alive = snapshot_alive(snap)
        now = time.monotonic()
        if alive != alive_before:
            detail = snap.transport_error or ",".join(snap.reasons) or None
            db.insert_health_check(conn, session_id, args.modem_id, "recovered" if alive else "failure", alive, detail)
            last_health = now
        elif now - last_health >= args.health_interval:
            db.insert_health_check(conn, session_id, args.modem_id, "periodic", alive)
            last_health = now
        alive_before = alive
        if health.record(args.modem_id, alive):
            try:
                driver.reconnect()
                driver.prepare_for_collection()
                health.reset(args.modem_id)
                db.insert_health_check(conn, session_id, args.modem_id, "reconnect_ok", True)
            except (ConnectionError, OSError) as exc:
                db.insert_health_check(conn, session_id, args.modem_id, "reconnect_failed", False, str(exc))
        if args.max_measurements and seq >= args.max_measurements:
            return True
        if args.duration_s and time.monotonic() - start >= args.duration_s:
            return True
        delay = next_due(start, args.interval, time.monotonic()) - time.monotonic()
        if delay > 0:
            stop.event.wait(delay)
    return False


def _close_session(conn, session_id: int, modem_id: str, status: str, reason: Optional[str]) -> None:
    try:
        db.insert_health_check(conn, session_id, modem_id, "end", status != "error", reason)
    except sqlite3.Error as exc:
        print(f"AVISO: não foi possível registrar o fim da sessão no health_checks: {exc}")
    db.finish_session(conn, session_id, status, reason)
    print(f"Sessão {session_id} encerrada: {status}" + (f" ({reason})" if reason else ""))


def run_session(args: argparse.Namespace, driver: ModemDriver, conn, stop: _StopFlag) -> int:
    """Executa a sessão (once ou continuous) e devolve o session_id."""
    point_id = None
    if args.reference_point:
        point_id, created, has_coord = db.get_or_create_reference_point(conn, args.reference_point)
        if created or not has_coord:
            print(f"AVISO: o ponto {args.reference_point!r} não tem coordenada cadastrada"
                  + (" (criado agora)" if created else "")
                  + "; cadastre com 'python -m coletor.daemon point-add'.")
    if not args.campaign_plmn:
        print("AVISO: --campaign-plmn não informado; o Estimador precisará decidir a operadora da campanha.")
    prepare = driver.prepare_for_collection()
    metadata = _session_metadata(args, driver, prepare)
    session_id = db.create_session(
        conn, mode=args.command, scenario=args.scenario, environment=args.environment, modem_id=args.modem_id,
        metadata=metadata, reference_point_id=point_id, route=args.route, campaign_plmn=args.campaign_plmn,
        notes=args.notes,
    )
    db.insert_health_check(conn, session_id, args.modem_id, "start", True)
    print(f"Sessão {session_id} iniciada ({args.command}).")
    status, reason = "error", None
    try:
        if args.command == "once":
            snap = _collect(conn, driver, session_id, 1, point_id, args.lpp_probe)
            if not snapshot_alive(snap):
                db.insert_health_check(conn, session_id, args.modem_id, "failure", False,
                                       snap.transport_error or ",".join(snap.reasons))
            status = "completed"
        elif _loop(args, driver, conn, session_id, point_id, stop):
            status = "completed"
        else:
            status, reason = "interrupted", f"sinal {stop.reason}"
    except KeyboardInterrupt:
        status, reason = "interrupted", "KeyboardInterrupt"
    except BaseException as exc:
        status, reason = "error", f"{type(exc).__name__}: {exc}"
        raise
    finally:
        _close_session(conn, session_id, args.modem_id, status, reason)
    return session_id


# ---------------------------------------------------------------- linha de comando
def _plmn(text: str) -> str:
    if not re.fullmatch(r"\d{5,6}", text):
        raise argparse.ArgumentTypeError("PLMN = MCC + MNC em dígitos, ex.: 72406")
    return text


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Coletor de dados de rede celular (banco v2)")
    sub = parser.add_subparsers(dest="command", required=True)

    def modem_args(p):
        p.add_argument("--port", required=True, help="porta AT, ex.: /dev/ttyUSB2")
        p.add_argument("--baudrate", type=int, default=115200)
        p.add_argument("--modem-id", dest="modem_id", default="fn990")

    modem_args(sub.add_parser("status", help="imprime o status do modem (não grava nada)"))
    for name, help_text in (("once", "1 snapshot gravado numa sessão nova"),
                            ("continuous", "snapshots periódicos até o limite ou SIGINT/SIGTERM")):
        p = sub.add_parser(name, help=help_text)
        modem_args(p)
        p.add_argument("--db", required=True, help="banco v2 (criado se não existir; v1 é recusado)")
        p.add_argument("--scenario", required=True, choices=db.SCENARIOS)
        p.add_argument("--environment", required=True, choices=db.ENVIRONMENTS)
        where = p.add_mutually_exclusive_group(required=True)
        where.add_argument("--reference-point", dest="reference_point", help="nome do ponto (medição parada)")
        where.add_argument("--route", help="nome da rota (medição em movimento)")
        p.add_argument("--campaign-plmn", dest="campaign_plmn", type=_plmn, help="operadora da campanha, ex.: 72406")
        p.add_argument("--notes")
        p.add_argument("--lpp-probe", dest="lpp_probe", action="store_true",
                       help="inclui a sonda experimental somente leitura AT$LCSLPP?")
        if name == "continuous":
            p.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_S, help="segundos entre inícios de ciclo")
            p.add_argument("--health-interval", dest="health_interval", type=float, default=DEFAULT_HEALTH_INTERVAL_S)
            p.add_argument("--max-measurements", dest="max_measurements", type=int)
            p.add_argument("--duration-s", dest="duration_s", type=float)

    p_add = sub.add_parser("point-add", help="cadastra a coordenada de um ponto de referência")
    p_add.add_argument("--db", required=True)
    p_add.add_argument("name")
    p_add.add_argument("--lat", type=float, required=True)
    p_add.add_argument("--lon", type=float, required=True)
    p_add.add_argument("--alt", type=float)
    p_add.add_argument("--coord-source", dest="coord_source", default="levantamento",
                       help="origem da coordenada (ex.: levantamento, rtk, planta)")
    p_add.add_argument("--uncertainty-m", dest="uncertainty_m", type=float)
    p_add.add_argument("--description")

    p_list = sub.add_parser("point-list", help="lista os pontos de referência")
    p_list.add_argument("--db", required=True)
    return parser


def main(argv=None) -> None:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "status":
            driver = _open_modem(args)
            try:
                print(json.dumps(to_jsonable(driver.get_status()), indent=2, ensure_ascii=False))
            finally:
                driver.close()
            return

        conn = db.connect(args.db)
        try:
            if args.command == "point-add":
                point_id = db.add_reference_point(
                    conn, args.name, args.lat, args.lon, args.alt, args.coord_source, args.uncertainty_m,
                    args.description,
                )
                print(f"Ponto {args.name!r} cadastrado (id {point_id}).")
            elif args.command == "point-list":
                for row in db.list_reference_points(conn):
                    print(" | ".join("" if v is None else str(v) for v in row))
            else:
                driver = _open_modem(args)
                stop = _StopFlag()
                previous = _install_handlers(stop) if args.command == "continuous" else {}
                try:
                    run_session(args, driver, conn, stop)
                finally:
                    for sig, handler in previous.items():
                        signal.signal(sig, handler)
                    driver.close()
        finally:
            conn.close()
    except (db.SchemaError, ConnectionError, ValueError) as exc:
        raise SystemExit(f"erro: {exc}") from exc


if __name__ == "__main__":
    main()
