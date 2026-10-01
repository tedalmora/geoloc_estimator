"""LEGADO (banco v1): inferência sob demanda de cell_id/ta por PCI e do AoA geométrico.

cell_id/ta: cálculo direto (sem ML) usando o cadastro antigo por PCI
(methods/network_position.py) -- lêem a leitura mais recente do banco do Coletor só para
saber qual foi a última célula servidora/TA vistos.

O subcomando fingerprint saiu na fase 5 (Random Forest removido; o WKNN usa o bundle JSON
em `estimador/live.py`).

aoa: geometria pura a partir de 2 ângulos já extraídos de um relatório LPP -- as âncoras
podem ser digitadas na mão (--anchor1/--anchor2) ou buscadas no cadastro (--registry +
--pci1/--pci2).

Para medir a posição AGORA use `estimador/live.py`, que fala com o HAL diretamente.

Uso (rodar a partir da pasta `codigo/`):
    python -m estimador.infer cell_id --db radiomapa.db --technology 5G --registry network_registry.json
    python -m estimador.infer ta      --db radiomapa.db --technology 5G --registry network_registry.json
    python -m estimador.infer aoa --anchor1 -26.900,-49.070 --bearing1 45 --anchor2 -26.901,-49.071 --bearing2 300
    python -m estimador.infer aoa --registry network_registry.json --pci1 100 --bearing1 45 --pci2 200 --bearing2 300
"""
import argparse
import sqlite3
import time
from typing import Optional, Tuple

from .features import open_legacy_radiomap
from .methods.aoa import anchors_from_registry, estimate_position_from_bearings
from .methods.network_position import estimate_cell_position, estimate_cell_position_ta, load_registry


def _latest_measurement(db_path: str, technology: str) -> Tuple[dict, list]:
    conn = open_legacy_radiomap(db_path)
    conn.row_factory = sqlite3.Row
    m = conn.execute(
        "SELECT * FROM measurements WHERE technology = ? ORDER BY timestamp DESC LIMIT 1", (technology,)
    ).fetchone()
    if m is None:
        conn.close()
        raise RuntimeError("Nenhuma medição encontrada para essa tecnologia no banco.")
    neighbors = conn.execute(
        "SELECT pci, rsrp_dbm FROM neighbor_cells WHERE measurement_id = ?", (m["measurement_id"],)
    ).fetchall()
    conn.close()
    return dict(m), [dict(n) for n in neighbors]


def infer_cell_id(db_path: str, technology: str, registry_path: str) -> Optional[Tuple[float, float]]:
    m, _ = _latest_measurement(db_path, technology)
    registry = load_registry(registry_path)
    return estimate_cell_position(m["serving_pci"], registry)


def infer_ta(db_path: str, technology: str, registry_path: str) -> Optional[dict]:
    m, _ = _latest_measurement(db_path, technology)
    registry = load_registry(registry_path)
    return estimate_cell_position_ta(m["serving_pci"], m["timing_advance"], technology, registry)


def _parse_latlon(text: str) -> Tuple[float, float]:
    lat_str, lon_str = text.split(",")
    return float(lat_str), float(lon_str)


def main() -> None:
    parser = argparse.ArgumentParser(description="LEGADO: inferência sob demanda de cell_id/ta por PCI e AoA")
    subparsers = parser.add_subparsers(dest="metodo", required=True)

    for nome in ("cell_id", "ta"):
        sp = subparsers.add_parser(nome)
        sp.add_argument("--db", default="/data/radiomapa.db")
        sp.add_argument("--technology", required=True, choices=["4G", "5G"])
        sp.add_argument("--registry", default="estimador/network_registry.json")
        sp.add_argument("--watch", action="store_true", help="repete a inferência a cada --interval segundos")
        sp.add_argument("--interval", type=float, default=5.0)

    sp_aoa = subparsers.add_parser("aoa")
    sp_aoa.add_argument("--registry", default=None, help="se informado, use --pci1/--pci2 em vez de --anchor1/--anchor2")
    sp_aoa.add_argument("--anchor1", help="lat,lon da 1a âncora (ex.: -26.900,-49.070)")
    sp_aoa.add_argument("--pci1", type=int, help="alternativa a --anchor1: busca a posição no --registry")
    sp_aoa.add_argument("--bearing1", type=float, required=True, help="ângulo em graus, 0=Norte, sentido horário")
    sp_aoa.add_argument("--anchor2")
    sp_aoa.add_argument("--pci2", type=int)
    sp_aoa.add_argument("--bearing2", type=float, required=True)

    args = parser.parse_args()

    if args.metodo == "aoa":
        if args.registry:
            registry = load_registry(args.registry)
            anchor1, anchor2 = anchors_from_registry(args.pci1, args.pci2, registry)
        else:
            anchor1, anchor2 = _parse_latlon(args.anchor1), _parse_latlon(args.anchor2)
        lat, lon = estimate_position_from_bearings(anchor1, args.bearing1, anchor2, args.bearing2)
        print(f"Posição estimada (AoA): {lat:.6f}, {lon:.6f}")
        return

    while True:
        try:
            if args.metodo == "cell_id":
                resultado = infer_cell_id(args.db, args.technology, args.registry)
                lat_lon = resultado
                raio = None
            elif args.metodo == "ta":
                resultado = infer_ta(args.db, args.technology, args.registry)
                lat_lon = None if resultado is None else (resultado["lat"], resultado["lon"])
                raio = None if resultado is None else resultado["raio_incerteza_m"]

            if lat_lon is None:
                print("Sem estimativa possível (célula fora do cadastro).")
            else:
                lat, lon = lat_lon
                extra = f" (raio de incerteza: {raio:.0f} m)" if raio else ""
                print(f"[{time.strftime('%H:%M:%S')}] Posição estimada ({args.metodo}): {lat:.6f}, {lon:.6f}{extra}")
        except RuntimeError as exc:
            print(exc)
        if not args.watch:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()


