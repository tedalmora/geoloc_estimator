"""Restricted key=value configuration for location platform setup."""

import math
import re
from pathlib import Path
from typing import Dict, Mapping, Optional, Union

CONFIG_NAME = "tcc-location.conf"
SYSTEM_CONFIG = Path("/etc/tcc") / CONFIG_NAME
FIELDS = ("system", "data_dir", "port", "plmn", "environment", "interval_s")
SYSTEMS = ("raspberrypi_os", "buildroot")
ENVIRONMENTS = ("indoor", "outdoor")
PathLike = Union[str, Path]


def user_config_path() -> Path:
    return Path.home() / ".config" / "tcc" / CONFIG_NAME


def find_config(path: Optional[PathLike] = None) -> Optional[Path]:
    if path is not None:
        target = Path(path).expanduser()
        if not target.is_file():
            raise FileNotFoundError(f"configuracao ausente: {target}")
        return target
    for target in (SYSTEM_CONFIG, user_config_path()):
        if target.exists():
            if not target.is_file():
                raise ValueError(f"configuracao nao e arquivo: {target}")
            return target
    return None


def validate_config(values: Mapping[str, str]) -> None:
    unknown = set(values) - set(FIELDS)
    if unknown:
        raise ValueError(f"chaves desconhecidas: {', '.join(sorted(unknown))}")
    for key, value in values.items():
        if not isinstance(value, str) or not value.strip() or "\n" in value or "\r" in value:
            raise ValueError(f"{key}: valor vazio ou invalido")
    if "system" in values and values["system"] not in SYSTEMS:
        raise ValueError("system: esperado raspberrypi_os ou buildroot")
    if "environment" in values and values["environment"] not in ENVIRONMENTS:
        raise ValueError("environment: esperado indoor ou outdoor")
    if "plmn" in values and not re.fullmatch(r"[0-9]{5,6}", values["plmn"]):
        raise ValueError("plmn: esperados 5 ou 6 digitos")
    if "interval_s" in values:
        try:
            interval = float(values["interval_s"])
        except ValueError as exc:
            raise ValueError("interval_s: esperado numero positivo") from exc
        if not math.isfinite(interval) or interval <= 0:
            raise ValueError("interval_s: esperado numero positivo finito")


def load_config(path: PathLike) -> Dict[str, str]:
    target = Path(path).expanduser()
    values: Dict[str, str] = {}
    with target.open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            text = line.strip()
            if not text or text.startswith("#"):
                continue
            key, separator, value = text.partition("=")
            key, value = key.strip(), value.strip()
            if not separator:
                raise ValueError(f"{target}:{number}: esperado chave=valor")
            if key in values:
                raise ValueError(f"{target}:{number}: chave duplicada: {key}")
            values[key] = value
    validate_config(values)
    return values


def save_config(path: PathLike, values: Mapping[str, str], force: bool = False) -> Path:
    validate_config(values)
    missing = set(FIELDS) - set(values)
    if missing:
        raise ValueError(f"campos ausentes: {', '.join(sorted(missing))}")
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w" if force else "x", encoding="utf-8") as stream:
        stream.write("".join(f"{key}={values[key]}\n" for key in FIELDS))
    return target
