"""Snapshot: 1 ciclo de leitura do modem, com status, motivos e latência por comando.

É a única implementação de "ler o modem uma vez": o Coletor grava o snapshot no banco e a
medição ao vivo (estimador/live.py) estima a posição a partir dele.

Ordem dos comandos: #MONI e #SERVINFO em seguida, para reduzir a chance de a servidora mudar
entre os dois; depois #CSURV (até 3 min); por fim $GPSACP. Exatamente 1 #CSURV por snapshot.
Nenhum comando grava configuração no modem. O LPP não faz parte do snapshot (sonda experimental
separada em experimental/lpp_probe.py).

Status do snapshot:
- complete: todas as leituras 'ok' e a servidora com radio_key e global_cell_key;
- partial: há dado de rádio, mas algo faltou (motivos em `reasons`);
- invalid: nenhuma observação de célula (modem mudo, erro de porta, todas as leituras falharam).

Uso (a partir de `codigo/`):
    python -m acquisition.snapshot --port /dev/ttyUSB2 [--no-prepare]
"""
import argparse
import dataclasses
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from hal.modem_driver import ModemDriver
from hal.models import (
    ACCESS_UNKNOWN,
    READ_ERROR,
    READ_OK,
    READ_SKIPPED,
    READ_TIMEOUT,
    CellObservation,
    CsurvReading,
    GnssReading,
    MoniReading,
    ServingCellResolution,
    ServInfoReading,
    to_jsonable,
)
from hal.parsing import resolve_serving_cell

SNAPSHOT_COMPLETE = "complete"
SNAPSHOT_PARTIAL = "partial"
SNAPSHOT_INVALID = "invalid"


@dataclass
class Snapshot:
    status: str
    reasons: List[str]
    started_at: str  # UTC, ISO 8601 com 'Z'
    access_mode: str
    serving: ServingCellResolution
    serving_rsrp_dbm: Optional[float]  # da célula que define o acesso (âncora LTE ou NR SA), via #MONI
    serving_rsrq_db: Optional[float]
    serving_sinr_db: Optional[float]
    observations: List[CellObservation]  # #MONI + #SERVINFO + #CSURV, cada uma com a própria fonte
    moni: MoniReading
    servinfo: ServInfoReading
    csurv: CsurvReading
    gnss: GnssReading
    timings_s: Dict[str, float] = field(default_factory=dict)  # latência por comando e 'total'
    anomalies: List[str] = field(default_factory=list)
    transport_error: Optional[str] = None


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def take_snapshot(driver: ModemDriver) -> Snapshot:
    """Executa 1 ciclo de leitura. Não levanta exceção por falha do modem ou da porta: a falha
    vira status/motivo no snapshot (erro de porta interrompe os comandos seguintes)."""
    started_at = utc_now_iso()
    start = time.monotonic()
    steps = [
        ("MONI", driver.read_serving_cell, MoniReading),
        ("SERVINFO", driver.read_serving_info, ServInfoReading),
        ("CSURV", driver.scan_cells, CsurvReading),
        ("GNSS", driver.read_gnss, GnssReading),
    ]

    readings = {}
    transport_error: Optional[str] = None
    modem_silent = False
    for name, read, reading_type in steps:
        if transport_error is not None or modem_silent:
            readings[name] = reading_type(status=READ_SKIPPED)
            continue
        try:
            readings[name] = read()
        except (OSError, RuntimeError) as exc:  # porta caiu ou não está aberta
            transport_error = f"{name}: {type(exc).__name__}: {exc}"
            readings[name] = reading_type(status=READ_ERROR)
        if name == "SERVINFO" and readings["MONI"].status == READ_TIMEOUT and readings[name].status == READ_TIMEOUT:
            modem_silent = True  # evita esperar os 3 min do #CSURV num modem que não responde

    moni, servinfo, csurv, gnss = readings["MONI"], readings["SERVINFO"], readings["CSURV"], readings["GNSS"]
    serving = resolve_serving_cell(moni, servinfo, csurv.observations)
    observations = list(moni.observations) + list(servinfo.observations) + [
        dataclasses.replace(obs, is_serving=True) if i == serving.csurv_index else obs
        for i, obs in enumerate(csurv.observations)
    ]
    if moni.status == READ_OK:
        access_mode = moni.access_mode
    elif servinfo.status == READ_OK:
        access_mode = servinfo.access_mode
    else:
        access_mode = ACCESS_UNKNOWN

    timings = {name: r.response.elapsed_s for name, r in readings.items() if r.response is not None}
    timings["total"] = time.monotonic() - start
    anomalies = moni.anomalies + servinfo.anomalies + csurv.anomalies + gnss.anomalies + serving.anomalies

    reasons = [f"{name.lower()}:{readings[name].status}" for name in ("MONI", "SERVINFO", "CSURV", "GNSS")
               if readings[name].status != READ_OK]
    if transport_error is not None:
        reasons.append("erro_de_porta")
    if modem_silent:
        reasons.append("modem_sem_resposta")
    if serving.radio_key is None:
        reasons.append(f"servidora_sem_radio_key:{serving.method}")
    if serving.global_cell_key is None:
        reasons.append("servidora_sem_global_cell_key")
    if not observations:
        status = SNAPSHOT_INVALID
    else:
        status = SNAPSHOT_PARTIAL if reasons else SNAPSHOT_COMPLETE

    primary = moni.primary
    return Snapshot(
        status=status, reasons=reasons, started_at=started_at, access_mode=access_mode, serving=serving,
        serving_rsrp_dbm=primary.rsrp_dbm if primary else None,
        serving_rsrq_db=primary.rsrq_db if primary else None,
        serving_sinr_db=primary.sinr_db if primary else None,
        observations=observations, moni=moni, servinfo=servinfo, csurv=csurv, gnss=gnss,
        timings_s=timings, anomalies=anomalies, transport_error=transport_error,
    )


def main() -> None:
    from hal.fn990_driver import FN990Driver

    parser = argparse.ArgumentParser(description="Tira 1 snapshot do modem e imprime em JSON")
    parser.add_argument("--port", required=True, help="ex.: /dev/ttyUSB2")
    parser.add_argument("--baudrate", type=int, default=115200)
    parser.add_argument("--no-prepare", dest="prepare", action="store_false",
                        help="não consulta/liga o GNSS antes do snapshot")
    args = parser.parse_args()

    driver = FN990Driver()
    driver.connect(args.port, baudrate=args.baudrate)
    try:
        saida = {}
        if args.prepare:
            saida["prepare"] = to_jsonable(driver.prepare_for_collection())
        saida["snapshot"] = to_jsonable(take_snapshot(driver))
        print(json.dumps(saida, indent=2, ensure_ascii=False, default=str))
    finally:
        driver.close()


if __name__ == "__main__":
    main()
