"""Sonda LPP (EXPERIMENTAL) somente leitura: consulta AT$LCSLPP? e guarda a resposta bruta.

Não é método de posição e fica fora do caminho principal (hal/, acquisition/, coletor/ e
estimador/ não importam este módulo). Envia só a consulta `AT$LCSLPP?`, nunca a forma de escrita
do mesmo comando, que o manual do FN990 descreve como configuração salva automaticamente
(AT Commands Reference Guide 80691ST11097A r5, §4.11.1.14, p. 740). Em campo (Vivo, set/2026) a
escrita foi recusada com +CME ERROR: 4; a resposta da consulta ainda não foi registrada.

O registro guarda o comando, a resposta bruta, o código final, o status (ok, error, timeout,
unexpected ou unavailable), o erro, o horário UTC e a latência. Nada é interpretado: o manual não
documenta comando AT que devolva ângulo (DL-AoD/UL-AoA) ou RSTD.

Uso (a partir de `codigo/`):
    python -m experimental.lpp_probe --port /dev/ttyUSB2 [--out sonda_lpp.json]
"""
import argparse
import json
import os

from acquisition.snapshot import utc_now_iso
from hal.fn990_driver import FN990Driver

COMMAND = "AT$LCSLPP?"


def probe(driver) -> dict:
    """Envia só a consulta e devolve o registro da tentativa."""
    record = {"command": COMMAND, "timestamp": utc_now_iso(), "status": None, "final": None,
              "raw_response": None, "elapsed_s": None, "error": None}
    try:
        resp = driver.send_at(COMMAND)
    except (OSError, RuntimeError) as exc:
        return {**record, "status": "unavailable", "error": f"{type(exc).__name__}: {exc}"}
    record.update(final=resp.final, raw_response=resp.raw_lines, elapsed_s=resp.elapsed_s)
    if resp.timed_out:
        return {**record, "status": "timeout", "error": "sem código final dentro do prazo"}
    if not resp.ok:
        return {**record, "status": "error", "error": resp.final}
    if any(line.startswith("$LCSLPP:") for line in resp.lines):
        return {**record, "status": "ok"}
    return {**record, "status": "unexpected", "error": "OK sem linha $LCSLPP:"}


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Sonda LPP experimental, somente leitura (AT$LCSLPP?)")
    parser.add_argument("--port", required=True, help="porta AT do FN990, ex.: /dev/ttyUSB2")
    parser.add_argument("--baudrate", type=int, default=115200)
    parser.add_argument("--out", help="grava o registro em JSON (arquivo novo)")
    args = parser.parse_args(argv)
    if args.out and os.path.exists(args.out):
        parser.error(f"{args.out}: já existe; escolha outro nome.")
    driver = FN990Driver()
    try:
        driver.connect(args.port, baudrate=args.baudrate)
    except (ConnectionError, OSError) as exc:
        record = {"command": COMMAND, "timestamp": utc_now_iso(), "status": "unavailable", "final": None,
                  "raw_response": None, "elapsed_s": None, "error": f"FN990 indisponível: {exc}"}
    else:
        try:
            record = probe(driver)
        finally:
            driver.close()
    text = json.dumps(record, indent=2, ensure_ascii=False)
    print(text)
    if args.out:
        try:
            with open(args.out, "x", encoding="utf-8") as f:
                f.write(text + "\n")
        except OSError as exc:
            raise SystemExit(f"erro: não foi possível gravar {args.out}: {exc}") from exc


if __name__ == "__main__":
    main()
