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
import math
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
from runtime import daemon as runtime_daemon

from . import check, config, prepare

MODEL_PROTOCOLS = (PROTOCOL_SPATIAL, PROTOCOL_TEMPORAL, PROTOCOL_COMPLETE)
DEPLOY_DIR = "deployed"
DEBUG_DEFAULTS = {"TCC_DEBUG": "0", "TCC_METRICS": "", "TCC_SNAPSHOTS_DIR": "", "TCC_RUN_MANIFEST": ""}
# Equipamento em ponto ja cadastrado: k pequeno e L1 padronizado deram o menor erro (S1>S2, S2>S1 e leituras reais).
FIXED_HYPERPARAMETERS = ["--k", "2", "--metrics", "L1", "--scaling", "standardized", "--missing", "sentinel:-140"]


def load_context(campaign, config_path):
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
    collection, location = load_context(args.campaign, args.config)
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
               "--out", str(paths["model"]), "--report", str(paths["report"])] + FIXED_HYPERPARAMETERS
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


def _runtime_target(args, location):
    if args.runtime_config:
        return Path(args.runtime_config).expanduser()
    return config.find_runtime_config() or config.default_runtime_config(location.get("system"))


def plan_runtime_config(target, location, deployed_bundle):
    """New runtime.conf text and the (key, old, new) changes; nothing is written. Always manual mode."""
    missing = [key for key in ("system", "data_dir", "port", "interval_s") if not location.get(key)]
    if missing:
        raise ValueError(f"tcc-location.conf ausente ou sem {', '.join(missing)}; use --no-runtime-config")
    managed = {
        "TCC_METHOD": "wknn", "TCC_PORT": location["port"],
        "TCC_INTERVAL": str(math.ceil(float(location["interval_s"]))), "TCC_MODEL": str(deployed_bundle),
    }
    if not target.exists():
        runtime_dir = Path(location["data_dir"]) / "runtime"
        log = "/var/log/tcc/runtime.log" if location["system"] == "buildroot" else str(runtime_dir / "runtime.log")
        values = {
            "TCC_MODE": "manual", **managed, "TCC_REGISTRY": "",
            "TCC_OUTPUT": str(runtime_dir / "latest_position.json"),
            "TCC_HISTORY": str(runtime_dir / "history.jsonl"), "TCC_LOG": log, **DEBUG_DEFAULTS,
        }
        text = "# Gerado por campaign_tools.wknn deploy; TCC_MODE=manual: o runtime nao inicia sozinho.\n"
        text += "# TCC_DEBUG=0 e o modo normal; TCC_DEBUG=1 (instrumentacao de engenharia) so por escolha explicita.\n"
        text += "".join(f"{key}={value}\n" for key, value in values.items())
        return text, [(key, None, value) for key, value in managed.items()]
    try:
        current = runtime_daemon.load_config(str(target))
    except runtime_daemon.RuntimeConfigError as exc:
        raise ValueError(f"{target}: runtime.conf incompativel ({exc}); corrija-o ou use --no-runtime-config") from exc
    if current.mode != "manual":
        raise ValueError(f"{target}: TCC_MODE={current.mode}; o deploy so prepara runtime.conf em modo manual "
                         "(ajuste manualmente ou use --no-runtime-config)")
    old = {"TCC_METHOD": current.method, "TCC_PORT": current.port, "TCC_INTERVAL": current.interval,
           "TCC_MODEL": current.model}
    lines, present = [], set()
    for line in target.read_text(encoding="utf-8").splitlines():
        key = line.partition("=")[0].strip()
        if not line.lstrip().startswith("#"):
            present.add(key)
        lines.append(f"{key}={managed[key]}" if key in managed and not line.lstrip().startswith("#") else line)
    changes = [(key, old[key], value) for key, value in managed.items() if old[key] != value]
    for key, value in DEBUG_DEFAULTS.items():  # keys missing in an older file get the normal-mode default
        if key not in present:
            lines.append(f"{key}={value}")
            changes.append((key, None, value))
    return "\n".join(lines) + "\n", changes


def run_deploy(args):
    collection, location = load_context(args.campaign, args.config)
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
    target = target_dir / f"wknn_{technology.lower()}.json"
    metadata_path = target_dir / "deployment.json"
    runtime_target = runtime_text = changes = None
    if not args.no_runtime_config:
        runtime_target = _runtime_target(args, location)
        runtime_text, changes = plan_runtime_config(runtime_target, location, target)
    target_dir.mkdir(parents=True, exist_ok=True)
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
        if runtime_target is not None:
            runtime_target.parent.mkdir(parents=True, exist_ok=True)
            temporary.append(_prepare_atomic(runtime_target, runtime_text.encode("utf-8")))
        os.replace(temporary[0], target)
        os.replace(temporary[1], metadata_path)
        if runtime_target is not None:
            if runtime_target.exists() and changes:
                shutil.copy2(runtime_target, runtime_target.with_name(runtime_target.name + ".bak"))
            os.replace(temporary[2], runtime_target)
    finally:
        for tmp in temporary:
            tmp.unlink(missing_ok=True)
    print(f"Modelo implantado: {args.model} ({technology})")
    print(f"Origem: {source}\nDestino: {target}\nRegistro: {metadata_path}")
    if not metadata["evaluation_available"]:
        print("O modelo complete nao possui avaliacao independente.")
    if runtime_target is None:
        print("runtime.conf nao alterado (--no-runtime-config)")
        return target
    print(f"runtime.conf preparado: {runtime_target}\nModo: manual")
    for key, before, after in changes:
        print(f"  {key}: {'(novo)' if before is None else before or '(vazio)'} -> {after}")
    if not changes:
        print("  ja estava atualizado")
    option = f" --runtime-config {runtime_target}" if args.runtime_config else ""
    print(f"Inferencia pontual (nao inicia o daemon):\n  python -m campaign_tools.position{option}")
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
    runtime_group = deploy_parser.add_mutually_exclusive_group()
    runtime_group.add_argument("--runtime-config", help="runtime.conf a preparar (padrao: o existente ou o do sistema)")
    runtime_group.add_argument("--no-runtime-config", action="store_true",
                               help="implanta o bundle sem preparar runtime.conf")
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
