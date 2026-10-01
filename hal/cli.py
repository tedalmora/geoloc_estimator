"""Utilitário de linha de comando para testar o HAL sozinho, sem o Coletor.

Cada subcomando executa só o comando AT correspondente e imprime a leitura em JSON (com status,
chaves das células e resposta crua). Uso (a partir da pasta `codigo/`):
    python -m hal.cli --port /dev/ttyUSB2 alive
    python -m hal.cli --port /dev/ttyUSB2 status
    python -m hal.cli --port /dev/ttyUSB2 prepare      # liga o GNSS só se estiver desligado
    python -m hal.cli --port /dev/ttyUSB2 moni         # célula servidora (AT#MONI)
    python -m hal.cli --port /dev/ttyUSB2 servinfo     # PLMN da servidora (AT#SERVINFO)
    python -m hal.cli --port /dev/ttyUSB2 csurv        # varredura (AT#CSURV, até 3 min)
    python -m hal.cli --port /dev/ttyUSB2 gnss         # AT$GPSACP
    python -m hal.cli --port /dev/ttyUSB2 lpp-config   # sonda experimental, só leitura (AT$LCSLPP?)
    python -m hal.cli --port /dev/ttyUSB2 reboot
"""
import argparse
import json

from .fn990_driver import FN990Driver
from .models import to_jsonable

COMMANDS = ("alive", "status", "prepare", "moni", "servinfo", "csurv", "gnss", "lpp-config", "reboot")


def _print(obj) -> None:
    print(json.dumps(to_jsonable(obj), indent=2, ensure_ascii=False, default=str))


def main() -> None:
    parser = argparse.ArgumentParser(description="CLI de teste manual do HAL (FN990)")
    parser.add_argument("--port", required=True, help="ex.: /dev/ttyUSB2")
    parser.add_argument("--baudrate", type=int, default=115200)
    parser.add_argument("comando", choices=COMMANDS)
    args = parser.parse_args()

    driver = FN990Driver()
    driver.connect(args.port, baudrate=args.baudrate)
    try:
        if args.comando == "alive":
            print("Vivo" if driver.is_alive() else "Sem resposta")
        elif args.comando == "status":
            _print(driver.get_status())
        elif args.comando == "prepare":
            _print(driver.prepare_for_collection())
        elif args.comando == "moni":
            _print(driver.read_serving_cell())
        elif args.comando == "servinfo":
            _print(driver.read_serving_info())
        elif args.comando == "csurv":
            _print(driver.scan_cells())
        elif args.comando == "gnss":
            _print(driver.read_gnss())
        elif args.comando == "lpp-config":
            _print(driver.query_lpp_config())
        elif args.comando == "reboot":
            driver.reboot()
            print("Comando de reboot enviado; a porta serial foi fechada localmente.")
    finally:
        if args.comando != "reboot":
            driver.close()


if __name__ == "__main__":
    main()
