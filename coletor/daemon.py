"""Daemon do Coletor -- usa o HAL para ler os modems e grava tudo no SQLite.

Ver planejamento/arquitetura_coleta_dados.md para a explicação completa do desenho.

Modos de operação (responde a "é daemon ou só sob demanda?"):
    --mode status      -> imprime o status de cada modem configurado e termina (não cria sessão)
    --mode ondemand     -> (padrão) faz 1 leitura de cada modem, grava, e termina
    --mode continuous   -> vira o daemon de verdade: fica rodando, lendo a cada --interval
                            segundos (padrão 60s, "a cada 1 minuto"), até Ctrl+C/SIGTERM

Uso (rodar a partir da pasta `codigo/`):
    python -m coletor.daemon --mode status --fn990-port /dev/ttyUSB2
    python -m coletor.daemon --scenario pedestre --environment outdoor --fn990-port /dev/ttyUSB2 --db radiomapa.db
    python -m coletor.daemon --mode continuous --interval 60 --scenario pedestre --environment outdoor \
        --fn990-port /dev/ttyUSB2 --db radiomapa.db
"""
import argparse
import dataclasses
import json
import queue
import signal
import threading
import time
from types import SimpleNamespace
from typing import List, Tuple

from acquisition.snapshot import take_snapshot
from hal.fn990_driver import FN990Driver
from hal.health import HealthMonitor
from hal.modem_driver import ModemDriver
from hal.models import RAT_LTE, SOURCE_CSURV

from . import db

DEFAULT_INTERVAL_S = 60.0


def poll_once(driver: ModemDriver, modem_id: str, technology: str, session_id: int, collect_lpp: bool = False) -> dict:
    """1 snapshot da aquisição compartilhada convertido para o formato do banco v1 (adaptação
    provisória; o Coletor é reescrito sobre o snapshot na fase 4).

    `collect_lpp`: também guarda o texto cru da sonda experimental somente leitura AT$LCSLPP?.
    """
    snap = take_snapshot(driver, include_lpp_probe=collect_lpp)
    fix = snap.gnss.fix
    return {
        "session_id": session_id,
        "timestamp": time.time(),
        "modem_id": modem_id,
        "technology": technology,
        "signal": SimpleNamespace(
            rsrp_dbm=snap.serving_rsrp_dbm, rsrq_db=snap.serving_rsrq_db, sinr_db=snap.serving_sinr_db,
            timing_advance=None,
        ),
        "cell": SimpleNamespace(cell_id=snap.serving.cell_id, pci=snap.serving.pci, tac=snap.serving.tac),
        "neighbors": [
            SimpleNamespace(pci=o.pci, rsrp_dbm=o.rsrp_dbm)
            for o in snap.observations
            if o.source == SOURCE_CSURV and o.rat == RAT_LTE and o.pci is not None and o.rsrp_dbm is not None
        ],
        "gnss": None if fix is None else SimpleNamespace(
            lat=fix.lat, lon=fix.lon, alt_m=fix.alt_m, fix_quality=str(fix.fix_type)
        ),
        "lpp_raw_log": snap.lpp_probe.raw_text if snap.lpp_probe is not None else None,
    }


def poll_modem_loop(
    driver: ModemDriver,
    modem_id: str,
    technology: str,
    session_id: int,
    out_queue: "queue.Queue",
    health: HealthMonitor,
    interval_s: float,
    stop_event: threading.Event,
    collect_lpp: bool = False,
) -> None:
    """1 thread por modem: health check + leitura, repetindo a cada `interval_s` até `stop_event`."""
    while not stop_event.is_set():
        alive = driver.is_alive()
        should_reconnect = health.record(modem_id, alive)
        out_queue.put(("health", modem_id, alive))

        if should_reconnect:
            print(f"[{modem_id}] {health.max_failures_before_reconnect}+ falhas seguidas -- tentando reconectar...")
            try:
                driver.reconnect()
                health.reset(modem_id)
                alive = True
            except Exception as exc:
                print(f"[{modem_id}] falha ao reconectar: {exc}")

        if alive:
            try:
                out_queue.put(("measurement", poll_once(driver, modem_id, technology, session_id, collect_lpp)))
            except Exception as exc:
                # 1 leitura falha não deve derrubar a sessão inteira -- só loga e segue
                print(f"[{modem_id}] erro de leitura: {exc}")
        stop_event.wait(interval_s)


def writer_loop(db_path: str, in_queue: "queue.Queue", stop_event: threading.Event) -> None:
    """Única thread que escreve no SQLite -- evita concorrência de escrita entre threads de leitura."""
    conn = db.connect(db_path)
    while not (stop_event.is_set() and in_queue.empty()):
        try:
            kind, *payload = in_queue.get(timeout=1.0)
        except queue.Empty:
            continue
        if kind == "measurement":
            db.insert_measurement(conn, payload[0])
        elif kind == "health":
            modem_id, alive = payload
            db.insert_health_check(conn, modem_id, alive)
    conn.close()


def run_continuous(
    modems: List[Tuple[str, str, ModemDriver]], db_path: str, session_id: int, interval_s: float,
    collect_lpp: bool = False,
) -> None:
    stop_event = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop_event.set())
    signal.signal(signal.SIGTERM, lambda *_: stop_event.set())

    out_queue: "queue.Queue" = queue.Queue()
    health = HealthMonitor()

    writer = threading.Thread(target=writer_loop, args=(db_path, out_queue, stop_event))
    writer.start()

    threads = []
    for modem_id, technology, driver in modems:
        t = threading.Thread(
            target=poll_modem_loop,
            args=(driver, modem_id, technology, session_id, out_queue, health, interval_s, stop_event, collect_lpp),
        )
        t.start()
        threads.append(t)

    for t in threads:
        t.join()
    writer.join()


def run_ondemand(
    modems: List[Tuple[str, str, ModemDriver]], db_path: str, session_id: int, collect_lpp: bool = False
) -> None:
    conn = db.connect(db_path)
    for modem_id, technology, driver in modems:
        alive = driver.is_alive()
        db.insert_health_check(conn, modem_id, alive)
        if not alive:
            print(f"[{modem_id}] não respondeu -- pulei a leitura.")
            continue
        item = poll_once(driver, modem_id, technology, session_id, collect_lpp)
        db.insert_measurement(conn, item)
        print(f"[{modem_id}] leitura gravada.")
    conn.close()


def run_status(modems: List[Tuple[str, str, ModemDriver]]) -> None:
    for modem_id, _technology, driver in modems:
        print(f"--- {modem_id} ---")
        print(json.dumps(dataclasses.asdict(driver.get_status()), indent=2, ensure_ascii=False))


def _connect_modems(args: argparse.Namespace) -> List[Tuple[str, str, ModemDriver]]:
    """Conecta só os modems cuja porta foi informada -- os outros (EM7565/EM120R-GL) ainda
    não têm driver implementado, então simplesmente não entram na lista (o resto do Coletor
    não precisa mudar quando esses drivers existirem -- essa é a vantagem da interface comum)."""
    modems: List[Tuple[str, str, ModemDriver]] = []
    if args.fn990_port:
        driver = FN990Driver()
        driver.connect(args.fn990_port, baudrate=args.baudrate)
        modems.append(("fn990", "5G", driver))
    if not modems:
        raise SystemExit("Nenhum modem configurado -- use pelo menos --fn990-port.")
    return modems


def main() -> None:
    parser = argparse.ArgumentParser(description="Daemon/coletor de dados de rede celular")
    parser.add_argument("--mode", choices=["status", "ondemand", "continuous"], default="ondemand")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_S)
    parser.add_argument("--scenario", choices=["estatico", "pedestre", "veicular"], default=None)
    parser.add_argument("--environment", choices=["indoor", "outdoor"], default=None)
    parser.add_argument("--db", default="/data/radiomapa.db")
    parser.add_argument("--fn990-port", dest="fn990_port", default=None, help="ex.: /dev/ttyUSB2")
    parser.add_argument("--baudrate", type=int, default=115200)
    parser.add_argument(
        "--lpp-probe", dest="lpp_probe", action="store_true",
        help="também guarda a resposta crua de AT$LCSLPP? (sonda experimental, só leitura)",
    )
    args = parser.parse_args()

    if args.mode != "status" and (not args.scenario or not args.environment):
        parser.error("--scenario e --environment são obrigatórios nos modos 'ondemand' e 'continuous'.")

    modems = _connect_modems(args)
    try:
        if args.mode == "status":
            run_status(modems)
            return

        conn = db.connect(args.db)
        session_id = db.create_session(conn, args.scenario, args.environment)
        conn.close()

        if args.mode == "continuous":
            print(f"Sessão {session_id} iniciada em modo contínuo (a cada {args.interval:.0f}s). Ctrl+C para parar.")
            run_continuous(modems, args.db, session_id, args.interval, collect_lpp=args.lpp_probe)
        else:
            print(f"Sessão {session_id}: leitura sob demanda.")
            run_ondemand(modems, args.db, session_id, collect_lpp=args.lpp_probe)

        conn = db.connect(args.db)
        db.close_session(conn, session_id)
        conn.close()
        print(f"Sessão {session_id} encerrada.")
    finally:
        for _, _, driver in modems:
            driver.close()


if __name__ == "__main__":
    main()
