"""Inferência atual (modo once): 1 leitura do FN990 -> 1 estimativa por WKNN ou Cell-ID.

Único caminho oficial para estimar a posição com uma leitura atual do modem:
1. valida o bundle WKNN ou o cadastro de células, antes de abrir a porta;
2. conecta ao FN990;
3. tira 1 snapshot pela aquisição compartilhada (`acquisition.snapshot.take_snapshot`);
4. snapshot `invalid` não gera estimativa; `partial` só é usado se tiver o que o método exige
   (WKNN: #CSURV lido; Cell-ID: global_cell_key da servidora) e fica registrado em snapshot_status;
5. estima com WKNN (methods/fingerprint.py) ou Cell-ID (methods/cell_id.py);
6. mostra o resultado e os tempos (carga, leitura, estimativa e ponta a ponta), em texto ou JSON;
7. fecha a conexão.

Não grava banco, não repete leituras, não liga o GNSS nem altera configuração do modem e não usa
TA, LPP nem geometria angular. Código de saída: 0 com estimativa válida; 1 sem estimativa ou com
o FN990 indisponível; 2 para argumento, bundle ou cadastro inválido.

Uso (a partir de `codigo/`):
    python -m estimador.live wknn --port /dev/ttyUSB2 --model modelos/wknn_lte_spatial.json [--json]
    python -m estimador.live cell-id --port /dev/ttyUSB2 --registry estimador/network_registry.json [--json]
"""
import argparse
import json
import sys
import time
from typing import Optional

from acquisition.snapshot import Snapshot, take_snapshot
from hal.fn990_driver import FN990Driver

from .estimate import PositionEstimate, unavailable_reading
from .features import cells_from_observations
from .methods.cell_id import METHOD as CELL_ID, CellRegistry, estimate_cell_id, load_registry
from .methods.fingerprint import METHOD as WKNN, BundleError, WknnModel, load_bundle


def estimate_from_snapshot(snapshot: Snapshot, model: Optional[WknnModel] = None,
                           registry: Optional[CellRegistry] = None) -> PositionEstimate:
    """WKNN (com `model`) ou Cell-ID (com `registry`) sobre 1 snapshot; nunca devolve None."""
    estimate = unavailable_reading(WKNN if model else CELL_ID, snapshot.status, snapshot.csurv.status,
                                   model.technology if model else None,
                                   detail=", ".join(snapshot.reasons or ["sem observações"]))
    if estimate is None:
        estimate = (model.predict(cells_from_observations(snapshot.observations)) if model
                    else estimate_cell_id(registry, snapshot.serving.global_cell_key))
    estimate.snapshot_status = snapshot.status
    estimate.metadata["snapshot"] = {
        "started_at": snapshot.started_at, "reasons": snapshot.reasons, "access_mode": snapshot.access_mode,
        "serving_global_cell_key": snapshot.serving.global_cell_key, "serving_radio_key": snapshot.serving.radio_key,
        "timings_s": snapshot.timings_s,
    }
    return estimate


def _print_text(estimate: PositionEstimate) -> None:
    snap = estimate.metadata["snapshot"]
    reasons = f"; motivos: {', '.join(snap['reasons'])}" if snap["reasons"] else ""
    print(f"Leitura {snap['started_at']}: snapshot {estimate.snapshot_status}{reasons}; acesso {snap['access_mode']}; "
          f"servidora {snap['serving_global_cell_key'] or '-'}; {snap['timings_s'].get('total', 0.0):.1f} s")
    t = estimate.metadata["timings_s"]
    timings = (f"  tempos: carga {t['load_s']:.3f} s; estimativa {1000 * t['estimate_s']:.1f} ms; "
               f"ponta a ponta {t['end_to_end_s']:.1f} s")
    if not estimate.valid:
        print(f"{estimate.method}: {estimate.status} -- {estimate.reason}")
        print(timings)
        return
    print(f"{estimate.method}: valid {estimate.lat:.6f}, {estimate.lon:.6f}")
    if estimate.x_m is not None:
        print(f"  ENU do bundle: x = {estimate.x_m:.1f} m, y = {estimate.y_m:.1f} m; "
              f"{estimate.metadata['n_observed_keys']} radio_keys usadas, {estimate.metadata['n_ignored_keys']} ignoradas")
    else:
        print(f"  {estimate.metadata['position']}")
    print(timings)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Inferência atual com 1 leitura do FN990 (WKNN ou Cell-ID)")
    sub = parser.add_subparsers(dest="method", required=True)
    for name, title, option, help_text in (
        ("wknn", "fingerprinting WKNN", "--model", "bundle WKNN (.json) gerado por estimador.train"),
        ("cell-id", "posição cadastrada da célula servidora", "--registry", "cadastro de células (registry_version 2)"),
    ):
        p = sub.add_parser(name, help=title)
        p.add_argument("--port", required=True, help="porta AT do FN990, ex.: /dev/ttyUSB2")
        p.add_argument(option, required=True, help=help_text)
        p.add_argument("--baudrate", type=int, default=115200)
        p.add_argument("--json", action="store_true", help="resultado completo em JSON")
    args = parser.parse_args(argv)

    t0 = start = time.perf_counter()
    model = registry = None
    if args.method == "wknn":
        try:
            model = load_bundle(args.model)
        except (BundleError, OSError) as exc:
            parser.error(f"bundle inválido ou incompatível: {exc}")
    else:
        registry = load_registry(args.registry)
        if not registry.valid:
            parser.error(f"invalid_registry: {registry.error}")
    timings = {"load_s": time.perf_counter() - start}

    driver = FN990Driver()
    try:
        driver.connect(args.port, baudrate=args.baudrate)
    except (ConnectionError, OSError) as exc:
        raise SystemExit(f"erro: FN990 indisponível: {exc}") from exc
    try:
        snapshot = take_snapshot(driver)
    finally:
        driver.close()
    start = time.perf_counter()
    estimate = estimate_from_snapshot(snapshot, model, registry)
    timings["estimate_s"] = time.perf_counter() - start
    timings["end_to_end_s"] = time.perf_counter() - t0  # carga + conexão + leitura + estimativa
    estimate.metadata["timings_s"] = timings
    if args.json:
        print(json.dumps(estimate.to_dict(), indent=2, ensure_ascii=False))
    else:
        _print_text(estimate)
    return 0 if estimate.valid else 1


if __name__ == "__main__":
    sys.exit(main())
