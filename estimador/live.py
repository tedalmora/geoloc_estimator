"""Medição ao vivo: fala com o modem diretamente (não depende do Coletor estar rodando) para
estimar a posição AGORA, com 1 ou mais métodos ao mesmo tempo. Lê o modem pela aquisição
compartilhada (`acquisition.snapshot.take_snapshot`, a mesma usada pelo Coletor) e usa o
`HealthMonitor` (hal/health.py) para decidir reconexões. Adaptação mínima da Etapa 2 (fase 3):
os métodos do Estimador ainda são os legados (chave por PCI) até a fase 5. Ver
estimador/README.md, seção "Coleta vs. medição ao vivo", para a divisão de responsabilidades.

2 modos:
    once       -> tira N leituras (padrão 10), calcula a posição em cada uma por método
                  escolhido, reporta média + desvio-padrão (métrica de "Estabilidade" do
                  Capítulo 7 de visao_geral_projeto.md) e SALVA 1 linha por método no SQLite.
    continuous -> repete para sempre a cada --interval segundos (com health check + reconexão
                  automática, igual ao Coletor), logando cada estimativa, até Ctrl+C.

Em ambos os modos, se houver uma posição real conhecida na hora (GNSS do próprio modem, ou
`--gnss-real lat,lon` informado na mão), o erro (haversine) já é calculado e salvo junto --
é isso que `estimador/relatorio.py` lê depois para comparar os métodos entre si.

Uso (rodar a partir da pasta `codigo/`):
    python -m estimador.live once       --metodos cell_id,ta,fingerprint --fn990-port /dev/ttyUSB2 --technology 5G
    python -m estimador.live continuous --metodos cell_id,fingerprint --interval 5 --fn990-port /dev/ttyUSB2 --technology 5G

    # com posição real conhecida (ex.: parado num ponto já surveyado) -- calcula e salva o erro
    python -m estimador.live once --metodos cell_id,ta --gnss-real -26.9005,-49.0703 --fn990-port /dev/ttyUSB2

    # aoa precisa dos 2 ângulos na hora (ainda não são extraídos automaticamente do LPP em
    # cada ciclo -- ver hal/README.md), então eles ficam fixos durante toda a execução:
    python -m estimador.live once --metodos aoa \
        --aoa-anchor1 -26.900,-49.070 --aoa-bearing1 45 --aoa-anchor2 -26.901,-49.071 --aoa-bearing2 300 \
        --fn990-port /dev/ttyUSB2

Depois de medir em vários pontos/sessões: `python -m estimador.relatorio --db estimador_estimativas.db`.
"""
import argparse
import signal
import threading
import time
from typing import Dict, List, Optional, Tuple

from acquisition.snapshot import Snapshot, take_snapshot
from hal.fn990_driver import FN990Driver
from hal.health import HealthMonitor

from . import db as estimador_db
from .features import cells_from_observations
from .geometry import haversine_m
from .methods.aoa import estimate_position_from_bearings
from .methods.fingerprint import BundleError, WknnModel, load_bundle
from .methods.network_position import estimate_cell_position, estimate_cell_position_ta, load_registry

METODOS_DISPONIVEIS = ("cell_id", "ta", "fingerprint", "aoa")


def _parse_latlon(text: str) -> Tuple[float, float]:
    lat_str, lon_str = text.split(",")
    return float(lat_str), float(lon_str)


def _estimar_uma_leitura(
    metodo: str, leitura: Snapshot, registry: dict, fp_bundle: Optional[WknnModel], technology: str,
    aoa_bearings: Optional[tuple],
) -> Optional[dict]:
    """Aplica 1 método a 1 snapshot. Devolve {"lat", "lon", "raio_incerteza_m"}
    ou None se não foi possível estimar (célula fora do cadastro, modelo ausente, etc.).
    O PCI da servidora só existe quando o cruzamento com o #CSURV foi unívoco; o TA não é lido."""
    serving_pci = leitura.serving.pci

    if metodo == "cell_id":
        pos = estimate_cell_position(serving_pci, registry)
        return None if pos is None else {"lat": pos[0], "lon": pos[1], "raio_incerteza_m": None}

    if metodo == "ta":
        return estimate_cell_position_ta(serving_pci, None, technology, registry)

    if metodo == "fingerprint":
        if fp_bundle is None:
            return None
        estimativa = fp_bundle.predict(cells_from_observations(leitura.observations))
        return {"lat": estimativa.lat, "lon": estimativa.lon, "raio_incerteza_m": None} if estimativa.valid else None

    if metodo == "aoa":
        if aoa_bearings is None:
            return None
        anchor1, bearing1, anchor2, bearing2 = aoa_bearings
        lat, lon = estimate_position_from_bearings(anchor1, bearing1, anchor2, bearing2)
        return {"lat": lat, "lon": lon, "raio_incerteza_m": None}

    return None


def _resumir(nome: str, estimativas: List[Optional[dict]]) -> dict:
    """Média + desvio-padrão (em metros, via haversine) das N estimativas de 1 método --
    é isso que vira a métrica de Estabilidade no modo `once`."""
    validas = [e for e in estimativas if e is not None]
    if not validas:
        return {
            "metodo": nome, "n_validas": 0, "lat_media": None, "lon_media": None,
            "desvio_padrao_m": None, "raio_incerteza_m": None,
        }
    lat_media = sum(e["lat"] for e in validas) / len(validas)
    lon_media = sum(e["lon"] for e in validas) / len(validas)
    if len(validas) > 1:
        distancias = [haversine_m(lat_media, lon_media, e["lat"], e["lon"]) for e in validas]
        desvio_padrao_m = (sum(d ** 2 for d in distancias) / len(distancias)) ** 0.5
    else:
        desvio_padrao_m = 0.0
    raios = [e["raio_incerteza_m"] for e in validas if e.get("raio_incerteza_m") is not None]
    raio_medio = sum(raios) / len(raios) if raios else None
    return {
        "metodo": nome, "n_validas": len(validas), "lat_media": lat_media, "lon_media": lon_media,
        "desvio_padrao_m": desvio_padrao_m, "raio_incerteza_m": raio_medio,
    }


def _posicao_real(leituras_gnss: list, override: Optional[Tuple[float, float]]) -> Optional[Tuple[float, float]]:
    """Posição de referência para calcular erro -- prioriza --gnss-real (mais confiável, você
    sabe exatamente onde está parado) sobre o GNSS do próprio modem (pode não ter fix, ou ter
    seu próprio erro)."""
    if override is not None:
        return override
    validos = [g for g in leituras_gnss if g is not None]
    if not validos:
        return None
    return sum(g.lat for g in validos) / len(validos), sum(g.lon for g in validos) / len(validos)


def _rodar_once(
    driver, metodos, n_amostras, registry, fp_bundle, technology, aoa_bearings, gnss_real_override, conn
) -> None:
    resultados: Dict[str, List[Optional[dict]]] = {m: [] for m in metodos}
    leituras_gnss = []
    for _ in range(n_amostras):
        leitura = take_snapshot(driver)
        leituras_gnss.append(leitura.gnss.fix)
        for metodo in metodos:
            resultados[metodo].append(
                _estimar_uma_leitura(metodo, leitura, registry, fp_bundle, technology, aoa_bearings)
            )

    posicao_real = _posicao_real(leituras_gnss, gnss_real_override)

    cabecalho = f"{'metodo':<14}{'n_validas':>10}{'lat_media':>14}{'lon_media':>14}{'desvio_padrao_m':>18}"
    print(cabecalho + (f"{'erro_m':>10}" if posicao_real else ""))
    for metodo in metodos:
        r = _resumir(metodo, resultados[metodo])
        lat_s = f"{r['lat_media']:.6f}" if r["lat_media"] is not None else "-"
        lon_s = f"{r['lon_media']:.6f}" if r["lon_media"] is not None else "-"
        dp_s = f"{r['desvio_padrao_m']:.1f}" if r["desvio_padrao_m"] is not None else "-"
        linha = f"{r['metodo']:<14}{r['n_validas']:>10}{lat_s:>14}{lon_s:>14}{dp_s:>18}"

        erro_m = None
        if posicao_real and r["lat_media"] is not None:
            erro_m = haversine_m(posicao_real[0], posicao_real[1], r["lat_media"], r["lon_media"])
            linha += f"{erro_m:>10.1f}"
        print(linha)

        estimador_db.insert_estimate(
            conn, metodo, r["lat_media"], r["lon_media"], r["raio_incerteza_m"], r["desvio_padrao_m"],
            r["n_validas"], posicao_real[0] if posicao_real else None, posicao_real[1] if posicao_real else None,
            erro_m,
        )


def _rodar_continuous(
    driver, metodos, interval, registry, fp_bundle, technology, aoa_bearings, gnss_real_override, conn
) -> None:
    stop_event = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop_event.set())
    signal.signal(signal.SIGTERM, lambda *_: stop_event.set())

    health = HealthMonitor()
    print(f"Medindo continuamente a cada {interval:.0f}s -- Ctrl+C para parar.")

    while not stop_event.is_set():
        alive = driver.is_alive()
        if health.record("fn990", alive):
            print("fn990: falhas seguidas -- tentando reconectar...")
            try:
                driver.reconnect()
                health.reset("fn990")
                alive = True
            except Exception as exc:
                print(f"fn990: falha ao reconectar: {exc}")

        if alive:
            leitura = take_snapshot(driver)
            posicao_real = _posicao_real([leitura.gnss.fix], gnss_real_override)
            for metodo in metodos:
                estimativa = _estimar_uma_leitura(metodo, leitura, registry, fp_bundle, technology, aoa_bearings)
                if estimativa is None:
                    print(f"[{time.strftime('%H:%M:%S')}] {metodo}: sem estimativa")
                    estimador_db.insert_estimate(conn, metodo, None, None)
                    continue

                erro_m = None
                extra = ""
                if posicao_real:
                    erro_m = haversine_m(posicao_real[0], posicao_real[1], estimativa["lat"], estimativa["lon"])
                    extra = f" (erro: {erro_m:.1f} m)"
                print(f"[{time.strftime('%H:%M:%S')}] {metodo}: {estimativa['lat']:.6f}, {estimativa['lon']:.6f}{extra}")
                estimador_db.insert_estimate(
                    conn, metodo, estimativa["lat"], estimativa["lon"], estimativa["raio_incerteza_m"],
                    n_amostras=1,
                    lat_real=posicao_real[0] if posicao_real else None,
                    lon_real=posicao_real[1] if posicao_real else None,
                    erro_m=erro_m,
                )
        stop_event.wait(interval)


def main() -> None:
    parser = argparse.ArgumentParser(description="Medição de posição ao vivo (sem depender do Coletor)")
    parser.add_argument("modo", choices=["once", "continuous"])
    parser.add_argument("--metodos", required=True, help="separados por vírgula: " + ",".join(METODOS_DISPONIVEIS))
    parser.add_argument("--fn990-port", dest="fn990_port", required=True)
    parser.add_argument("--baudrate", type=int, default=115200)
    parser.add_argument("--technology", default="5G", choices=["4G", "5G"],
                        help="rótulo usado só pelo método ta legado (TA é revisto na fase 7)")
    parser.add_argument("--registry", default="estimador/network_registry.json")
    parser.add_argument("--fingerprint-modelo", dest="fingerprint_modelo", default=None,
                        help="bundle WKNN em JSON gerado por 'python -m estimador.train'")
    parser.add_argument("--amostras", type=int, default=10, help="só usado no modo 'once'")
    parser.add_argument("--interval", type=float, default=5.0, help="só usado no modo 'continuous'")
    parser.add_argument("--db", default="estimador_estimativas.db", help="onde salvar as estimativas (once e continuous)")
    parser.add_argument(
        "--gnss-real", dest="gnss_real", default=None,
        help="lat,lon conhecidos na hora da medição -- sobrepõe o GNSS do próprio modem para calcular o erro",
    )
    parser.add_argument("--aoa-anchor1", dest="aoa_anchor1")
    parser.add_argument("--aoa-bearing1", dest="aoa_bearing1", type=float)
    parser.add_argument("--aoa-anchor2", dest="aoa_anchor2")
    parser.add_argument("--aoa-bearing2", dest="aoa_bearing2", type=float)
    args = parser.parse_args()

    metodos = args.metodos.split(",")
    for metodo in metodos:
        if metodo not in METODOS_DISPONIVEIS:
            parser.error(f"método desconhecido: {metodo!r} (opções: {', '.join(METODOS_DISPONIVEIS)})")

    registry = load_registry(args.registry) if ("cell_id" in metodos or "ta" in metodos) else {}
    try:
        fp_bundle = load_bundle(args.fingerprint_modelo) if ("fingerprint" in metodos and args.fingerprint_modelo) else None
    except BundleError as exc:
        parser.error(str(exc))

    aoa_bearings = None
    if "aoa" in metodos and args.aoa_anchor1 and args.aoa_anchor2:
        aoa_bearings = (
            _parse_latlon(args.aoa_anchor1), args.aoa_bearing1,
            _parse_latlon(args.aoa_anchor2), args.aoa_bearing2,
        )

    gnss_real_override = _parse_latlon(args.gnss_real) if args.gnss_real else None

    driver = FN990Driver()
    driver.connect(args.fn990_port, baudrate=args.baudrate)
    driver.prepare_for_collection()
    conn = estimador_db.connect(args.db)
    try:
        if args.modo == "once":
            _rodar_once(
                driver, metodos, args.amostras, registry, fp_bundle, args.technology, aoa_bearings,
                gnss_real_override, conn,
            )
        else:
            _rodar_continuous(
                driver, metodos, args.interval, registry, fp_bundle, args.technology, aoa_bearings,
                gnss_real_override, conn,
            )
    finally:
        conn.close()
        driver.close()


if __name__ == "__main__":
    main()
