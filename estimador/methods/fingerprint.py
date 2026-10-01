"""Fingerprinting multicélula com WKNN: runtime em Python puro e bundle JSON (model_version 2).

O runtime usa só a biblioteca padrão; o Raspberry Pi não precisa de NumPy nem de scikit-learn.
O bundle é JSON (formato aberto) e guarda os fingerprints em RSRP bruto (dBm, ausência =
`missing_value`); a padronização escolhida no treino é aplicada ao carregar. A estrutura
exigida está em `REQUIRED_FIELDS` e é conferida por `validate_bundle` antes de qualquer inferência.

Distância zero: se alguma amostra estiver a distância <= ZERO_DISTANCE_EPS do vetor consultado,
a posição é a média simples de TODAS essas amostras exatas, mesmo que sejam mais que k, sem peso
inverso. Senão, valem os k vizinhos com peso 1/d. Empate na k-ésima distância: entra a amostra
que vem antes no bundle (ordem crescente de measurement_id), regra determinística.
Sem nenhuma radio_key do bundle na leitura, o estado é `no_matching_radio_keys`.
Bundles antigos (.joblib/pickle, scikit-learn) são recusados sem abrir o conteúdo.
"""
import json
import math
import os
import re
from itertools import takewhile
from typing import List, Sequence, Tuple

from ..estimate import STATUS_TECHNOLOGY_MISMATCH, STATUS_VALID, PositionEstimate
from ..features import (SCALINGS, TECHNOLOGIES, CellFilterPolicy, apply_scaling, rsrp_by_key, validate_missing_value,
                        vectorize)
from ..geometry import COORDINATE_SYSTEM, EnuFrame

METHOD = "wknn"
MODEL_TYPE = "wknn"
MODEL_VERSION = 2
STATUS_NO_MATCHING_RADIO_KEYS = "no_matching_radio_keys"
DISTANCE_METRICS = ("L1", "L2")
WEIGHTING = "inverse_distance"
ZERO_DISTANCE_POLICY = "mean_of_exact_matches"
ZERO_DISTANCE_EPS = 1e-9
LEGACY_EXTENSIONS = (".joblib", ".pkl", ".pickle")
REQUIRED_FIELDS = (
    "model_type", "model_version", "created_at", "technology", "cell_keys", "cell_key_plmn_status",
    "missing_policy", "missing_value", "scaling_strategy", "scaling", "coordinate_system", "enu_origin",
    "n_neighbors", "distance_metric", "weighting", "zero_distance_policy", "cell_filter_policy",
    "reference_policy", "protocol", "training_groups", "dataset", "validation", "fingerprints",
)
_RADIO_KEY_RE = re.compile(r"(LTE|NR):\d+:\d+")


class BundleError(ValueError):
    """Bundle ausente, antigo, incompatível ou malformado."""


def distance(a: Sequence[float], b: Sequence[float], metric: str) -> float:
    if metric == "L1":
        return sum(abs(x - y) for x, y in zip(a, b))
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def wknn_from_sorted(ordered: Sequence[Tuple[float, int]], xs: Sequence[float], ys: Sequence[float],
                     k: int) -> Tuple[float, float, bool, List[int]]:
    """Posição WKNN a partir de TODOS os (distância, índice) em ordem crescente.

    Devolve (x, y, houve_distância_zero, índices usados): todas as amostras exatas, se houver; senão, os k primeiros.
    """
    exact = [i for _, i in takewhile(lambda pair: pair[0] <= ZERO_DISTANCE_EPS, ordered)]
    if exact:
        return sum(xs[i] for i in exact) / len(exact), sum(ys[i] for i in exact) / len(exact), True, exact
    nearest = ordered[:k]
    total = sum(1.0 / d for d, _ in nearest)
    return (sum(xs[i] / d for d, i in nearest) / total, sum(ys[i] / d for d, i in nearest) / total, False,
            [i for _, i in nearest])


def _finite_number(value) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def validate_bundle(data) -> None:
    if not isinstance(data, dict):
        raise BundleError("bundle não é um objeto JSON")
    if data.get("model_type") != MODEL_TYPE:
        raise BundleError(f"model_type não suportado: {data.get('model_type')!r} (esperado {MODEL_TYPE!r})")
    if data.get("model_version") != MODEL_VERSION:
        raise BundleError(f"model_version não suportada: {data.get('model_version')!r} (esperada {MODEL_VERSION})")
    missing = [f for f in REQUIRED_FIELDS if f not in data]
    if missing:
        raise BundleError(f"metadados ausentes: {', '.join(missing)}")
    technology, keys = data["technology"], data["cell_keys"]
    if technology not in TECHNOLOGIES:
        raise BundleError(f"tecnologia inválida: {technology!r}")
    if (not isinstance(keys, list) or not keys or len(set(keys)) != len(keys)
            or not all(isinstance(k, str) and _RADIO_KEY_RE.fullmatch(k) and k.startswith(technology + ":") for k in keys)):
        raise BundleError(f"cell_keys inválidas: precisam ser radio_keys únicas de {technology}")
    try:
        validate_missing_value(data["missing_value"], None)
    except ValueError as exc:
        raise BundleError(str(exc)) from exc
    strategy, scaling = data["scaling_strategy"], data["scaling"]
    if strategy not in SCALINGS:
        raise BundleError(f"scaling_strategy inválida: {strategy!r}")
    if strategy == "raw" and scaling is not None:
        raise BundleError("scaling deve ser null quando scaling_strategy = raw")
    if strategy == "standardized" and not (
        isinstance(scaling, dict) and all(len(scaling.get(n, [])) == len(keys) for n in ("means", "stds"))
        and all(_finite_number(v) for v in scaling["means"]) and all(_finite_number(v) and v > 0 for v in scaling["stds"])
    ):
        raise BundleError("médias/desvios da padronização ausentes ou com tamanho diferente de cell_keys")
    if data["coordinate_system"] != COORDINATE_SYSTEM:
        raise BundleError(f"coordinate_system não suportado: {data['coordinate_system']!r}")
    try:
        EnuFrame.from_dict(data["enu_origin"])
        policy = CellFilterPolicy.from_dict(data["cell_filter_policy"])
    except (KeyError, TypeError, ValueError) as exc:
        raise BundleError(f"origem ENU ou política de células inválida: {exc}") from exc
    if policy.technology != technology:
        raise BundleError("a política de células é de outra tecnologia")
    if data["distance_metric"] not in DISTANCE_METRICS:
        raise BundleError(f"distance_metric inválida: {data['distance_metric']!r}")
    if data["weighting"] != WEIGHTING or data["zero_distance_policy"] != ZERO_DISTANCE_POLICY:
        raise BundleError("weighting ou zero_distance_policy não suportados")
    fp = data["fingerprints"]
    if not isinstance(fp, dict) or not all(isinstance(fp.get(n), list) for n in ("rsrp_dbm", "x_m", "y_m", "measurement_id")):
        raise BundleError("fingerprints ausentes ou malformados")
    n = len(fp["rsrp_dbm"])
    if n == 0 or any(len(fp[name]) != n for name in ("x_m", "y_m", "measurement_id")):
        raise BundleError("fingerprints com listas de tamanhos diferentes")
    if not all(isinstance(v, list) and len(v) == len(keys) and all(_finite_number(x) for x in v) for v in fp["rsrp_dbm"]):
        raise BundleError(f"cada fingerprint precisa ter {len(keys)} valores finitos (um por cell_key)")
    if not all(_finite_number(v) for v in fp["x_m"] + fp["y_m"]):
        raise BundleError("coordenadas x_m/y_m não finitas")
    k = data["n_neighbors"]
    if isinstance(k, bool) or not isinstance(k, int) or not 1 <= k <= n:
        raise BundleError(f"n_neighbors inválido: {k!r} (1 a {n})")


class WknnModel:
    """Modelo WKNN carregado de um bundle validado."""

    def __init__(self, bundle: dict):
        validate_bundle(bundle)
        self.bundle = bundle
        self.technology = bundle["technology"]
        self.cell_keys: List[str] = bundle["cell_keys"]
        self._key_set = set(self.cell_keys)
        self.policy = CellFilterPolicy.from_dict(bundle["cell_filter_policy"])
        self.missing_value = float(bundle["missing_value"])
        self.scaling = bundle["scaling"]
        self.k = bundle["n_neighbors"]
        self.metric = bundle["distance_metric"]
        self.frame = EnuFrame.from_dict(bundle["enu_origin"])
        fp = bundle["fingerprints"]
        self._vectors = [apply_scaling(v, self.scaling) for v in fp["rsrp_dbm"]]
        self._xs, self._ys = fp["x_m"], fp["y_m"]
        self._ids = fp["measurement_id"]

    def predict_xy(self, raw_vector: Sequence[float]) -> Tuple[float, float, bool, List[int]]:
        query = apply_scaling(raw_vector, self.scaling)
        ordered = sorted((distance(query, f, self.metric), i) for i, f in enumerate(self._vectors))
        x, y, zero, used = wknn_from_sorted(ordered, self._xs, self._ys, self.k)
        return x, y, zero, [self._ids[i] for i in used]

    def predict(self, cells) -> PositionEstimate:
        """Estima a posição a partir das células do #CSURV de uma leitura (mesmo formato do dataset).
        Células fora do vocabulário são ignoradas e só contadas; o bundle não muda."""
        if not any(c.rat == self.technology for c in cells):
            return PositionEstimate(METHOD, STATUS_TECHNOLOGY_MISMATCH, technology=self.technology,
                                    reason=f"a leitura não tem células {self.technology} do #CSURV")
        vector, n_observed = vectorize(cells, self.policy, self.cell_keys, self.missing_value)
        n_ignored = len(set(rsrp_by_key(cells, self.policy)) - self._key_set)
        if n_observed == 0:
            return PositionEstimate(METHOD, STATUS_NO_MATCHING_RADIO_KEYS, technology=self.technology,
                                    reason="nenhuma radio_key do bundle foi observada na leitura",
                                    metadata={"n_observed_keys": 0, "n_ignored_keys": n_ignored})
        x, y, zero, neighbors = self.predict_xy(vector)
        lat, lon, _ = self.frame.to_geodetic(x, y)
        return PositionEstimate(
            METHOD, STATUS_VALID, lat=lat, lon=lon, x_m=x, y_m=y, technology=self.technology,
            metadata={"n_observed_keys": n_observed, "n_ignored_keys": n_ignored, "n_neighbors": self.k,
                      "distance_metric": self.metric, "zero_distance": zero, "neighbor_measurement_ids": neighbors},
        )


def load_bundle(path: str) -> WknnModel:
    if os.path.splitext(path)[1].lower() in LEGACY_EXTENSIONS:
        raise BundleError(
            f"{path}: bundle antigo (scikit-learn/joblib) não é mais aceito e não foi aberto. O modelo atual é "
            "WKNN em JSON (model_version 2); treine de novo com 'python -m estimador.train'."
        )
    with open(path, "rb") as f:
        head = f.read(1)
    if head == b"\x80":  # assinatura de pickle: nunca desserializar
        raise BundleError(f"{path}: conteúdo pickle (bundle antigo) recusado; treine de novo com 'python -m estimador.train'.")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BundleError(f"{path}: não é um bundle JSON válido ({exc})") from exc
    return WknnModel(data)


def save_bundle(bundle: dict, path: str) -> None:
    if not path.lower().endswith(".json"):
        raise BundleError("o bundle WKNN é salvo em JSON: use a extensão .json")
    if os.path.exists(path):
        raise FileExistsError(f"{path}: já existe; escolha outro nome (bundles não são sobrescritos).")
    validate_bundle(bundle)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(bundle, f, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    os.replace(tmp, path)
