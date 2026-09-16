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
from typing import List, Tuple

from hal.fn990_driver import FN990Driver
from hal.health import HealthMonitor
from hal.modem_driver import ModemDriver

from . import db

DEFAULT_INTERVAL_S = 60.0


def poll_once(driver: ModemDriver, modem_id: str, technology: str, session_id: int, collect_lpp: bool = False) -> dict:
    """1 leitura completa de 1 modem -- reaproveitada pelo modo on-demand e pelo loop contínuo.

    `collect_lpp`: também tenta ler o relatório LPP (AoA/DL-AoD, ver hal/fn990_driver.py) a cada
    leitura -- é opcional e experimental (--enable-lpp), então uma falha aqui não deve derrubar
    a leitura normal (sinal/célula/GNSS), só deixa lpp_raw_log vazio.
    """
    lpp_raw_log = None
    if collect_lpp:
        try:
            lpp_raw_log = driver.get_lpp_report().raw_text
        except Exception as exc:
            print(f"[{modem_id}] leitura de LPP falhou (ok, é experimental): {exc}")
    return {
        "session_id": session_id,
        "timestamp": time.time(),
        "modem_id": modem_id,
        "technology": technology,
        "signal": driver.get_signal_metrics(),
        "cell": driver.get_serving_cell(),
        "neighbors": driver.get_neighbor_cells(),
        "gnss": driver.get_gnss_fix(),
        "lpp_raw_log": lpp_raw_log,
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
        "--enable-lpp", dest="enable_lpp", action="store_true",
        help="também tenta habilitar e ler dados de LPP (AoA/DL-AoD) a cada leitura -- experimental",
    )
    args = parser.parse_args()

    if args.mode != "status" and (not args.scenario or not args.environment):
        parser.error("--scenario e --environment são obrigatórios nos modos 'ondemand' e 'continuous'.")

    modems = _connect_modems(args)
    if args.enable_lpp:
        for modem_id, _technology, driver in modems:
            try:
                driver.enable_lpp_positioning()
            except Exception as exc:
                print(f"[{modem_id}] não foi possível habilitar LPP (ok, é experimental): {exc}")
    try:
        if args.mode == "status":
            run_status(modems)
            return

        conn = db.connect(args.db)
        session_id = db.create_session(conn, args.scenario, args.environment)
        conn.close()

        if args.mode == "continuous":
            print(f"Sessão {session_id} iniciada em modo contínuo (a cada {args.interval:.0f}s). Ctrl+C para parar.")
            run_continuous(modems, args.db, session_id, args.interval, collect_lpp=args.enable_lpp)
        else:
            print(f"Sessão {session_id}: leitura sob demanda.")
            run_ondemand(modems, args.db, session_id, collect_lpp=args.enable_lpp)

        conn = db.connect(args.db)
        db.close_session(conn, session_id)
        conn.close()
        print(f"Sessão {session_id} encerrada.")
    finally:
        for _, _, driver in modems:
            driver.close()


if __name__ == "__main__":
    main()
