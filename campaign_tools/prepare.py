"""Prepare the directory, manifest and idle metrics of one named data collection."""

import argparse
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import config, manifest, monitor

COLLECTION_NAME = "collection.conf"
COLLECTION_FIELDS = ("name", "points", "results", "db", "canonical", "created_utc")
SUBDIRS = ("campanha", "logs", "referencias", "export", "modelos", "avaliacoes")
VALID_POINTS = tuple(f"P{i:02d}" for i in range(1, 9))
NAME_PATTERN = re.compile(r"[A-Za-z0-9_-]+")


def validate_name(name):
    if not isinstance(name, str) or NAME_PATTERN.fullmatch(name) is None:
        raise ValueError("nome: use somente letras, numeros, hifen e underscore")
    return name


def parse_points(text):
    points = [item.strip() for item in str(text).split(",")]
    if not points or points == [""]:
        raise ValueError("pontos: informe ao menos um ponto")
    invalid = [point for point in points if point not in VALID_POINTS]
    if invalid:
        raise ValueError(f"pontos invalidos: {', '.join(invalid) or 'vazio'}; use P01 a P08")
    if len(set(points)) != len(points):
        raise ValueError("pontos duplicados")
    return points


def collection_paths(data_dir, name):
    results = Path(data_dir).expanduser().resolve() / "campaigns" / validate_name(name)
    return {
        "results": results,
        "db": results / "campanha" / "radiomapa.db",
        "canonical": results / "referencias_canonicas.csv",
        "metrics_idle": results / "metricas_repouso.csv",
    }


def load_collection(path):
    target = Path(path).expanduser()
    values = {}
    with target.open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            text = line.strip()
            if not text or text.startswith("#"):
                continue
            key, separator, value = text.partition("=")
            key, value = key.strip(), value.strip()
            if not separator:
                raise ValueError(f"{target}:{number}: esperado chave=valor")
            if key not in COLLECTION_FIELDS:
                raise ValueError(f"{target}:{number}: chave desconhecida: {key}")
            if key in values:
                raise ValueError(f"{target}:{number}: chave duplicada: {key}")
            if not value:
                raise ValueError(f"{target}:{number}: valor vazio: {key}")
            values[key] = value
    missing = set(COLLECTION_FIELDS) - set(values)
    if missing:
        raise ValueError(f"{target}: campos ausentes: {', '.join(sorted(missing))}")
    validate_name(values["name"])
    values["points"] = parse_points(values["points"])
    return values


def resolve_collection(campaign, data_dir):
    """Accept a collection name (under data_dir/campaigns) or a collection.conf path."""
    if NAME_PATTERN.fullmatch(campaign):
        if not data_dir:
            raise ValueError("--campaign por nome exige data_dir na configuracao ou --data-dir")
        return collection_paths(data_dir, campaign)["results"] / COLLECTION_NAME
    return Path(campaign).expanduser()


def run_prepare(args, idle=monitor.run_idle):
    name = validate_name(args.name)
    points = parse_points(args.points)
    config_path = config.find_config(args.config)
    if config_path is None:
        raise FileNotFoundError("tcc-location.conf ausente; execute campaign_tools.configure primeiro")
    location = config.load_config(config_path)
    missing = set(config.FIELDS) - set(location)
    if missing:
        raise ValueError(f"{config_path}: campos ausentes: {', '.join(sorted(missing))}")
    paths = collection_paths(location["data_dir"], name)
    results = paths["results"]
    results.parent.mkdir(parents=True, exist_ok=True)
    try:
        results.mkdir()
    except FileExistsError:
        raise FileExistsError(f"{results}: coleta ja existe; escolha outro nome") from None
    try:
        for subdir in SUBDIRS:
            (results / subdir).mkdir()
        manifest.create_manifest(
            results / "manifesto_campanha.txt", name, location["system"], args.operator,
            paths["db"], location["plmn"], location["port"], float(location["interval_s"]),
            args.antenna_height_m, args.orientation, args.weather,
            phone_used=args.phone_used, planned_points=len(points),
        )
        print("Registrando metricas de repouso por 60 s...")
        idle(str(paths["db"]), location["data_dir"], str(paths["metrics_idle"]), 60, 10)
        created = datetime.now(timezone.utc).isoformat(timespec="seconds")
        values = {
            "name": name, "points": ",".join(points), "results": str(results),
            "db": str(paths["db"]), "canonical": str(paths["canonical"]), "created_utc": created,
        }
        with (results / COLLECTION_NAME).open("x", encoding="utf-8", newline="\n") as stream:
            stream.write("".join(f"{key}={values[key]}\n" for key in COLLECTION_FIELDS))
    except BaseException:
        # The directory was created by this call, so removing it never touches a previous collection.
        shutil.rmtree(results, ignore_errors=True)
        raise
    print(f"Coleta preparada: {results}")
    print(f"Pontos: {', '.join(points)}; banco: {paths['db']}")
    return results


def _main(argv=None):
    parser = argparse.ArgumentParser(description="Prepara os diretorios e o manifesto de uma coleta nomeada.")
    parser.add_argument("--name", required=True, help="letras, numeros, hifen e underscore")
    parser.add_argument("--points", required=True, help="lista separada por virgulas, por exemplo P01,P03")
    parser.add_argument("--operator", required=True)
    parser.add_argument("--config", help="arquivo tcc-location.conf")
    parser.add_argument("--antenna-height-m", type=float, default=1.0)
    parser.add_argument("--orientation", default="orientacao fixa conforme planta")
    parser.add_argument("--weather", default="ambiente interno")
    parser.add_argument("--phone-used", choices=("sim", "nao"), default="nao")
    args = parser.parse_args(argv)
    try:
        run_prepare(args)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERRO: preparacao nao concluida: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
