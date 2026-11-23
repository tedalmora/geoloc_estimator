"""Simplified WKNN training and bundle selection for one already acquired collection.

train trains and evaluates the spatial and temporal models and trains the complete model (all eligible
samples, no independent test) by calling the existing estimador.train / estimador.evaluate CLIs.
deploy copies one validated bundle to a stable path inside the collection; it does not run inference,
touch runtime.conf or access the modem.
"""

import argparse
import contextlib
import hashlib
import io
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from coletor import db as coletor_db
from estimador import evaluate, train
from estimador.dataset import (
    MIN_SELECTION_GROUPS, PROTOCOL_COMPLETE, PROTOCOL_SPATIAL, PROTOCOL_TEMPORAL, DatasetError, load_dataset,
    resolve_campaign_plmn,
)
from estimador.features import TECHNOLOGIES, CellFilterPolicy
from estimador.methods.fingerprint import BundleError, load_bundle

from . import check, config, prepare

MODEL_PROTOCOLS = (PROTOCOL_SPATIAL, PROTOCOL_TEMPORAL, PROTOCOL_COMPLETE)
DEPLOY_DIR = "deployed"


def _load(campaign, config_path):
    path = config.find_config(config_path)
    location = {} if path is None else config.load_config(path)
    collection = prepare.load_collection(prepare.resolve_collection(campaign, location.get("data_dir")))
    return collection, location


def artifact_paths(results, technology, protocol):
    tag = technology.lower()
    models = Path(results) / "modelos"
    return {
        "model": models / f"wknn_{tag}_{protocol}.json",
        "report": models / f"treino_{tag}_{protocol}.json",
        "evaluation": Path(results) / "avaliacoes" / (protocol if tag == "lte" else f"{protocol}_{tag}"),
    }


def _derived_artifacts(results, technology):
    return [path for protocol in MODEL_PROTOCOLS for path in artifact_paths(results, technology, protocol).values()]


def _protect_artifacts(results, technology, force):
    existing = [path for path in _derived_artifacts(results, technology) if path.exists()]
    if not existing:
        return
    if not force:
        names = ", ".join(str(path) for path in existing)
        raise FileExistsError(f"artefatos WKNN ja existem ({names}); use --force para refazer o treino")
    for path in existing:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()


def _eligibility(dataset, technology, plmn):
    """Eligible samples and sessions under the same policy estimador.train applies."""
    policy = CellFilterPolicy(technology, resolve_campaign_plmn(dataset.samples, plmn), False, True, False)
    samples, _ = train.eligible_samples(dataset.samples, PROTOCOL_TEMPORAL, policy)
    return len(samples), len({sample.session_id for sample in samples})


def _sufficient(counts):
    return counts[0] > 0 and counts[1] >= MIN_SELECTION_GROUPS


def _run_cli(label, function, argv):
    with contextlib.redirect_stdout(io.StringIO()):
        try:
            function(argv)
        except SystemExit as exc:
            raise RuntimeError(f"etapa '{label}' falhou: {exc.code}") from None


def _point_names(db_path):
    conn = coletor_db.open_read_only(str(db_path))
    try:
        return dict(conn.execute("SELECT reference_point_id, name FROM reference_points").fetchall())
    finally:
        conn.close()


def _complete_summary(bundle, name, db_path):
    names = _point_names(db_path)
    ids = bundle["training_groups"]["reference_point_ids"]
    selection = bundle["validation"]["hyperparameter_selection"]
    return {
        "protocol": PROTOCOL_COMPLETE, "evaluation_available": False, "campaign": name,
        "technology": bundle["technology"], "n_attempts_considered": bundle["dataset"]["n_attempts"],
        "n_eligible_fingerprints": len(bundle["fingerprints"]["measurement_id"]),
        "points": [names.get(point_id, str(point_id)) for point_id in ids], "reference_point_ids": ids,
        "session_ids": sorted(set(bundle["fingerprints"]["group_id"])), "features": bundle["cell_keys"],
        "hyperparameters": selection["chosen"], "hyperparameter_source": bundle["hyperparameter_source"],
        "samples_sha256": bundle["dataset"]["samples_sha256"],
        "training_samples_sha256": bundle["dataset"]["training_samples_sha256"],
        "limitations": bundle["validation"]["limitations"],
    }


def _write_json(path, data):
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(data, stream, indent=2, ensure_ascii=False)


def _metrics_line(summary):
    def fmt(value):
        return "-" if value is None else f"{value:.1f}"
    return (f"{summary['n_valid']}/{summary['n_attempts']} validas (cobertura operacional "
            f"{summary['operational_coverage']:.0%}); media {fmt(summary['mean_m'])} m, mediana "
            f"{fmt(summary['median_m'])} m, RMSE {fmt(summary['rmse_m'])} m, p90 {fmt(summary['p90_m'])} m, "
            f"p95 {fmt(summary['p95_m'])} m")


def run_train(args):
    collection, location = _load(args.campaign, args.config)
    name, results, db_path = collection["name"], Path(collection["results"]), collection["db"]
    technology = args.technology
    report = check.check_collection(collection)
    blockers = report["db_errors"] + report["acquisition_errors"] + report["invalid_snapshots"]
    if blockers:
        raise RuntimeError("coleta nao validada para treino: " + "; ".join(blockers[:5]))
    plmn = location.get("plmn")
    try:
        dataset = load_dataset(db_path)
        counts = {tech: _eligibility(dataset, tech, plmn) for tech in TECHNOLOGIES}
    except DatasetError as exc:
        raise RuntimeError(f"dataset: {exc}") from exc
    if not _sufficient(counts[technology]):
        raise RuntimeError(
            f"{technology} nao treinado: dados elegiveis insuficientes "
            f"({counts[technology][0]} amostras, {counts[technology][1]} sessoes); nenhum bundle criado"
        )
    _protect_artifacts(results, technology, args.force)
    (results / "modelos").mkdir(parents=True, exist_ok=True)

    snapshots = report["snapshots"]
    print(f"Coleta: {name}\nTecnologia: {technology}\nTentativas: {report['found_measurements']}")
    print(f"Complete: {snapshots['complete']}\nPartial: {snapshots['partial']}\nInvalid: {snapshots['invalid']}")
    print(f"Elegiveis: {counts[technology][0]}")
    for message in report["reference_errors"] + report["evidence_errors"]:
        print(f"AVISO: {message}", file=sys.stderr)

    summaries = {}
    for protocol in MODEL_PROTOCOLS:
        paths = artifact_paths(results, technology, protocol)
        cli = ["--db", db_path, "--technology", technology, "--protocol", protocol,
               "--out", str(paths["model"]), "--report", str(paths["report"])]
        if plmn:
            cli += ["--campaign-plmn", plmn]
        _run_cli(f"treino {technology} {protocol}", train.main, cli)
        paths["evaluation"].mkdir(parents=True)
        if protocol == PROTOCOL_COMPLETE:
            with paths["model"].open(encoding="utf-8") as stream:
                bundle = json.load(stream)
            _write_json(paths["evaluation"] / "resumo.json", _complete_summary(bundle, name, db_path))
            continue
        _run_cli(f"avaliacao {technology} {protocol}", evaluate.main, [
            "compare", "--db", db_path, "--model", str(paths["model"]),
            "--out", str(paths["evaluation"] / "resumo.csv"),
            "--predictions-out", str(paths["evaluation"] / "predicoes.csv"),
            "--json-out", str(paths["evaluation"] / "avaliacao.json"),
        ])
        with (paths["evaluation"] / "avaliacao.json").open(encoding="utf-8") as stream:
            summaries[protocol] = json.load(stream)

    for protocol in (PROTOCOL_SPATIAL, PROTOCOL_TEMPORAL):
        result = summaries[protocol]
        print(f"\n{protocol.capitalize()}:\n- treinado e avaliado (grupo = {result['group_column']}, "
              f"grupos de teste {result['test_groups']})")
        print(f"- modelo: {artifact_paths(results, technology, protocol)['model']}")
        print(f"- {_metrics_line(result['methods']['wknn'])}")
    complete = artifact_paths(results, technology, PROTOCOL_COMPLETE)
    print("\nComplete:\n- treinado com todos os fingerprints elegiveis; nao possui avaliacao independente")
    print(f"- origem dos hiperparametros: {train.HYPERPARAMETER_SOURCE_CV}")
    print(f"- modelo: {complete['model']}")
    for other in (tech for tech in TECHNOLOGIES if tech != technology):
        if _sufficient(counts[other]):
            print(f"\n{other}: {counts[other][0]} amostras elegiveis; treine com --technology {other}")
        else:
            print(f"\n{other}:\n- nao treinado: dados elegiveis insuficientes")
    print(f"\nProximo comando:\n  python -m campaign_tools.wknn deploy --campaign {args.campaign}")
    return results


def _prepare_atomic(target, data):
    tmp = target.with_name(target.name + ".tmp")
    with tmp.open("wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    return tmp


def run_deploy(args):
    collection, _ = _load(args.campaign, args.config)
    results = Path(collection["results"])
    technology = args.technology
    source = artifact_paths(results, technology, args.model)["model"]
    if not source.is_file():
        raise FileNotFoundError(f"{source}: bundle ausente; execute 'campaign_tools.wknn train' antes")
    bundle = load_bundle(str(source)).bundle
    if bundle["technology"] != technology or bundle["protocol"] != args.model:
        raise ValueError(f"{source}: bundle e {bundle['technology']}/{bundle['protocol']}, esperado "
                         f"{technology}/{args.model}")
    data = source.read_bytes()
    info = bundle["dataset"]
    target_dir = results / "modelos" / DEPLOY_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"wknn_{technology.lower()}.json"
    metadata_path = target_dir / "deployment.json"
    metadata = {
        "campaign": collection["name"], "technology": technology, "selected_model": args.model,
        "source_bundle": str(source), "source_bundle_sha256": hashlib.sha256(data).hexdigest(),
        "deployed_bundle": str(target), "deployed_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "protocol": bundle["protocol"], "evaluation_available": bundle.get("evaluation_available", True),
        "training_data_hash": info.get("training_samples_sha256") or info.get("selection_samples_sha256"),
    }
    temporary = []
    try:
        temporary.append(_prepare_atomic(target, data))
        temporary.append(_prepare_atomic(metadata_path, json.dumps(metadata, indent=2).encode("utf-8")))
        os.replace(temporary[0], target)
        os.replace(temporary[1], metadata_path)
    finally:
        for tmp in temporary:
            tmp.unlink(missing_ok=True)
    print(f"Modelo implantado: {args.model} ({technology})")
    print(f"Origem: {source}\nDestino: {target}\nRegistro: {metadata_path}")
    if not metadata["evaluation_available"]:
        print("O modelo complete nao possui avaliacao independente. runtime.conf nao foi alterado.")
    return target


def _main(argv=None):
    parser = argparse.ArgumentParser(description="Treino e selecao de modelos WKNN de uma coleta ja adquirida.")
    sub = parser.add_subparsers(dest="command", required=True)
    train_parser = sub.add_parser("train", help="treina spatial, temporal e complete e avalia spatial/temporal")
    deploy_parser = sub.add_parser("deploy", help="copia um bundle validado para modelos/deployed")
    for item in (train_parser, deploy_parser):
        item.add_argument("--campaign", required=True, help="nome da coleta ou caminho do collection.conf")
        item.add_argument("--config", help="arquivo tcc-location.conf")
        item.add_argument("--technology", choices=TECHNOLOGIES, default="LTE")
    train_parser.add_argument("--force", action="store_true",
                              help="substitui modelos, relatorios e avaliacoes WKNN ja gerados")
    deploy_parser.add_argument("--model", choices=MODEL_PROTOCOLS, default=PROTOCOL_COMPLETE,
                               help="modelo a implantar (padrao: complete)")
    args = parser.parse_args(argv)
    try:
        (run_train if args.command == "train" else run_deploy)(args)
    except (OSError, RuntimeError, ValueError, BundleError) as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        if args.command == "train":
            print("Treino nao concluido; nenhum modelo foi selecionado.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
