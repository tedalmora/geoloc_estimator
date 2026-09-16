"""Utilitário de linha de comando para testar o HAL sozinho, sem precisar do Coletor.

Uso (rodar a partir da pasta `codigo/`):
    python -m hal.cli --port /dev/ttyUSB2 alive
    python -m hal.cli --port /dev/ttyUSB2 status
    python -m hal.cli --port /dev/ttyUSB2 signal
    python -m hal.cli --port /dev/ttyUSB2 cell
    python -m hal.cli --port /dev/ttyUSB2 neighbors
    python -m hal.cli --port /dev/ttyUSB2 gnss
    python -m hal.cli --port /dev/ttyUSB2 reboot
"""
import argparse
import dataclasses
import json

from .fn990_driver import FN990Driver


def _print(obj) -> None:
    if dataclasses.is_dataclass(obj):
        obj = dataclasses.asdict(obj)
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def main() -> None:
    parser = argparse.ArgumentParser(description="CLI de teste manual do HAL (FN990)")
    parser.add_argument("--port", required=True, help="ex.: /dev/ttyUSB2")
    parser.add_argument("--baudrate", type=int, default=115200)
    parser.add_argument(
        "comando",
        choices=["alive", "status", "signal", "cell", "neighbors", "gnss", "reboot", "lpp"],
    )
    args = parser.parse_args()

    driver = FN990Driver()
    driver.connect(args.port, baudrate=args.baudrate)
    try:
        if args.comando == "alive":
            print("Vivo" if driver.is_alive() else "Sem resposta")
        elif args.comando == "status":
            _print(driver.get_status())
        elif args.comando == "signal":
            _print(driver.get_signal_metrics())
        elif args.comando == "cell":
            _print(driver.get_serving_cell())
        elif args.comando == "neighbors":
            _print([dataclasses.asdict(n) for n in driver.get_neighbor_cells()])
        elif args.comando == "gnss":
            fix = driver.get_gnss_fix()
            _print(fix if fix else "Sem fix GNSS ainda")
        elif args.comando == "reboot":
            driver.reboot()
            print("Comando de reboot enviado -- a porta serial foi fechada localmente.")
        elif args.comando == "lpp":
            print("Habilitação:", driver.enable_lpp_positioning())
            _print(driver.get_lpp_report())
    finally:
        if args.comando != "reboot":
            driver.close()


if __name__ == "__main__":
    main()
