"""One-shot WKNN position from the FN990 using a campaign model or the operational runtime.conf.

Resolves the port and the bundle, then calls estimador.live.run_estimate (the single inference
implementation). It never starts the runtime daemon, changes TCC_MODE or touches campaign data.
"""

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

from estimador import live
from estimador.features import TECHNOLOGIES
from runtime import daemon as runtime_daemon

from . import config, wknn

DEPLOYED = "deployed"
MODELS = wknn.MODEL_PROTOCOLS + (DEPLOYED,)
DEPLOYMENT_KEYS = ("selected_model", "source_bundle", "deployed_bundle", "source_bundle_sha256", "protocol",
                   "technology")


def verify_deployment(bundle, deployment):
    """deployment.json is complete and the deployed bundle matches its recorded SHA-256."""
    bundle, deployment = Path(bundle), Path(deployment)
    if not deployment.is_file():
        raise FileNotFoundError(f"{deployment}: deploy ausente; execute 'campaign_tools.wknn deploy' antes")
    try:
        record = json.loads(deployment.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"{deployment}: deployment.json ilegivel ({exc})") from exc
    missing = [key for key in DEPLOYMENT_KEYS if not isinstance(record, dict) or not record.get(key)]
    if missing:
        raise ValueError(f"{deployment}: campos ausentes: {', '.join(missing)}")
    if record["technology"] not in TECHNOLOGIES or record["selected_model"] not in wknn.MODEL_PROTOCOLS:
        raise ValueError(f"{deployment}: technology/selected_model nao suportados")
    if not bundle.is_file():
        raise FileNotFoundError(f"{bundle}: bundle implantado ausente; execute 'campaign_tools.wknn deploy' antes")
    digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
    if digest != record["source_bundle_sha256"]:
        raise ValueError(f"hash divergente: {bundle} tem SHA-256 {digest}, deployment.json registra "
                         f"{record['source_bundle_sha256']}; refaca o deploy")
    return record


def _campaign_bundle(args, collection):
    results = Path(collection["results"])
    model = args.model or DEPLOYED
    if model != DEPLOYED:
        path = wknn.artifact_paths(results, "LTE", model)["model"]
        if not path.is_file():
            raise FileNotFoundError(f"modelo {model} nao treinado: {path}; execute 'campaign_tools.wknn train'")
        return path, model
    deployment = results / "modelos" / wknn.DEPLOY_DIR / "deployment.json"
    if not deployment.is_file():
        raise FileNotFoundError(f"{deployment}: deploy ausente; execute 'campaign_tools.wknn deploy' antes")
    try:
        technology = json.loads(deployment.read_text(encoding="utf-8")).get("technology", "LTE")
    except (OSError, ValueError, AttributeError) as exc:
        raise ValueError(f"{deployment}: deployment.json ilegivel ({exc})") from exc
    path = deployment.with_name(f"wknn_{str(technology).lower()}.json")
    record = verify_deployment(path, deployment)
    return path, f"deployed ({record['selected_model']})"


def _runtime(path):
    target = config.find_runtime_config(path)
    if target is None:
        raise FileNotFoundError("runtime.conf ausente; execute 'campaign_tools.wknn deploy --campaign NOME' "
                                "ou informe --runtime-config")
    try:
        settings = runtime_daemon.load_config(str(target))
    except runtime_daemon.RuntimeConfigError as exc:
        raise ValueError(f"{target}: {exc}") from exc
    if settings.method != "wknn":
        raise ValueError(f"{target}: TCC_METHOD={settings.method}; position exige TCC_METHOD=wknn")
    return target, settings


def resolve(args):
    """(port, bundle, description). Precedence: overrides > campaign model > runtime.conf > tcc-location.conf."""
    runtime = None
    if args.campaign:
        collection, location = wknn.load_context(args.campaign, args.config)
        if args.bundle:
            bundle, description = Path(args.bundle), "bundle informado"
        else:
            bundle, description = _campaign_bundle(args, collection)
        if args.runtime_config:
            runtime = _runtime(args.runtime_config)
    else:
        if args.model:
            raise ValueError("--model exige --campaign; sem campanha o modelo vem do runtime.conf")
        location_path = config.find_config(args.config)
        location = {} if location_path is None else config.load_config(location_path)
        bundle = Path(args.bundle) if args.bundle else None
        description = "bundle informado"
        if not (args.port and args.bundle):
            runtime = _runtime(args.runtime_config)
        if bundle is None:
            if not runtime[1].model:
                raise ValueError(f"{runtime[0]}: TCC_MODEL vazio")
            bundle, description = Path(runtime[1].model), f"runtime.conf ({runtime[0]})"
            if bundle.is_file() and bundle.with_name("deployment.json").is_file():
                verify_deployment(bundle, bundle.with_name("deployment.json"))
    if not bundle.is_file():
        raise FileNotFoundError(f"bundle ausente: {bundle}")
    port = args.port or (runtime[1].port if runtime else "") or location.get("port")
    if not port:
        raise ValueError("porta ausente: informe --port, TCC_PORT em runtime.conf ou port em tcc-location.conf")
    return port, bundle, description


def write_result(path, text, force):
    target = Path(path).expanduser()
    if target.exists() and not force:
        raise FileExistsError(f"{target}: resultado ja existe; use --force para substituir")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as stream:
            stream.write(text + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)


def _main(argv=None):
    parser = argparse.ArgumentParser(description="Posicao pontual por WKNN a partir de 1 leitura do FN990.")
    parser.add_argument("--campaign", help="nome da coleta ou collection.conf (teste de modelos da campanha)")
    parser.add_argument("--config", help="arquivo tcc-location.conf")
    parser.add_argument("--runtime-config", help="arquivo runtime.conf (sem --campaign e o padrao)")
    parser.add_argument("--model", choices=MODELS, help="com --campaign; padrao: deployed")
    parser.add_argument("--json", action="store_true", help="resultado completo em JSON")
    parser.add_argument("--out", help="grava o JSON do resultado neste arquivo (nao sobrescreve)")
    parser.add_argument("--force", action="store_true", help="substitui apenas o arquivo de --out")
    parser.add_argument("--port", help="override tecnico da porta AT")
    parser.add_argument("--bundle", help="override tecnico do bundle WKNN")
    args = parser.parse_args(argv)
    try:
        port, bundle, description = resolve(args)
        if args.out and Path(args.out).expanduser().exists() and not args.force:
            raise FileExistsError(f"{args.out}: resultado ja existe; use --force para substituir")
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 2
    print(f"Modelo: {description}; bundle {bundle}; porta {port}", file=sys.stderr)
    try:
        estimate = live.run_estimate("wknn", port, str(bundle))
    except live.LiveInputError as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 2
    except live.LiveHardwareError as exc:
        print(f"ERRO: hardware indisponivel: {exc}", file=sys.stderr)
        return 1
    live.print_estimate(estimate, args.json)
    if args.out:
        try:
            write_result(args.out, live.estimate_json(estimate), args.force)
        except OSError as exc:
            print(f"ERRO: {exc}", file=sys.stderr)
            return 2
    return 0 if estimate.valid else 1


if __name__ == "__main__":
    raise SystemExit(_main())
