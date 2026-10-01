"""Estimador: features por radio_key, plano ENU, runtime WKNN, bundle JSON e treino por grupos."""
import dataclasses
import json
import math
import os
import pickle
import statistics

import pytest

from radiomapa_sintetico import CAMPAIGN_PLMN, CELULAS, LOCAL_KEY, criar_radiomapa, observacao

import estimador.train as tr
from estimador.dataset import CsurvCell, DatasetError, load_dataset, make_group_split, samples_in_groups
from estimador.features import (
    PLMN_CAMPAIGN,
    PLMN_UNCONFIRMED,
    CellFilterPolicy,
    MissingPolicy,
    MissingValueError,
    apply_scaling,
    build_vocabulary,
    cells_from_observations,
    fit_scaling,
    rsrp_by_key,
    vectorize,
)
from estimador.geometry import EnuFrame, haversine_m
from estimador.methods.fingerprint import BundleError, WknnModel, load_bundle, save_bundle, validate_bundle, wknn_from_sorted
from software_version import software_version

POLITICA = CellFilterPolicy("LTE", CAMPAIGN_PLMN)
SEMENTE_P6_NO_TESTE = 8  # com esta semente, o sorteio spatial põe P6 (único ponto que vê LOCAL_KEY) no teste


def cel(key, plmn="72406", status="CELL_SUITABLE", rsrp=-100.0, rat=None):
    return CsurvCell(key, rat or key.split(":")[0], plmn, status, rsrp)


class Amostra:
    def __init__(self, *cells):
        self.cells = cells


@pytest.fixture(scope="module")
def radiomapa(tmp_path_factory):
    caminho = tmp_path_factory.mktemp("rm") / "radiomapa_v2.db"
    criar_radiomapa(caminho)
    return str(caminho)


@pytest.fixture(scope="module")
def treino_spatial(radiomapa, tmp_path_factory):
    saida = str(tmp_path_factory.mktemp("modelo") / "wknn_lte_spatial.json")
    bundle, relatorio = tr.train(radiomapa, "LTE", "spatial", saida)
    return saida, bundle, relatorio


# --------------------------------------------------------------------------- ENU
def test_enu_ida_e_volta_e_deslocamentos_conhecidos():
    frame = EnuFrame(-26.9, -49.07)
    for lat, lon in [(-26.9, -49.07), (-26.905, -49.064), (-26.89, -49.08)]:
        e, n, u = frame.to_enu(lat, lon)
        lat2, lon2, _ = frame.to_geodetic(e, n, u)
        assert abs(lat2 - lat) < 1e-9 and abs(lon2 - lon) < 1e-9
    e, n, _ = frame.to_enu(-26.9 + 100 / 111_000, -49.07)
    assert abs(e) < 0.01 and abs(n - 100) < 1.0
    lat, lon, _ = frame.to_geodetic(300.0, -200.0)
    assert abs(haversine_m(-26.9, -49.07, lat, lon) - math.hypot(300, 200)) < 2.0
    assert EnuFrame.from_dict(frame.to_dict()) == frame
    with pytest.raises(ValueError):
        EnuFrame(95.0, 0.0)


# --------------------------------------------------------------------------- política e features
def test_politica_padrao_de_plmn_e_status():
    assert POLITICA.accepts(cel("LTE:1:1"))
    assert not POLITICA.accepts(cel("LTE:1:2", plmn="72405"))
    assert POLITICA.accepts(cel("LTE:1:3", plmn=None))
    assert not POLITICA.accepts(cel("LTE:1:4", status="CELL_FORBIDDEN"))
    assert not POLITICA.accepts(cel("NR:1:5"))
    assert not POLITICA.accepts(cel("LTE:1:6", rsrp=None))
    ampla = CellFilterPolicy("LTE", CAMPAIGN_PLMN, include_other_plmn=True, include_unknown_plmn=False, include_forbidden=True)
    assert ampla.accepts(cel("LTE:1:2", plmn="72405")) and ampla.accepts(cel("LTE:1:4", status="CELL_FORBIDDEN"))
    assert not ampla.accepts(cel("LTE:1:3", plmn=None))
    assert CellFilterPolicy.from_dict(ampla.to_dict()) == ampla


def test_vocabulario_marca_plmn_nao_confirmado_e_exclui_outra_operadora():
    amostras = [
        Amostra(cel("LTE:1:1"), cel("LTE:1:3", plmn=None), cel("LTE:1:7", plmn="72405")),
        Amostra(cel("LTE:1:7", plmn=None)),  # mesmo radio_key em registro curto: segue fora
    ]
    chaves, status = build_vocabulary(amostras, POLITICA)
    assert chaves == ["LTE:1:1", "LTE:1:3"]
    assert status == {"LTE:1:1": PLMN_CAMPAIGN, "LTE:1:3": PLMN_UNCONFIRMED}


def test_valores_repetidos_viram_media_e_ausencia_usa_o_valor_configurado():
    vetor, observadas = vectorize([cel("LTE:1:1", rsrp=-100), cel("LTE:1:1", rsrp=-90)], POLITICA, ["LTE:1:1", "LTE:1:2"], -150.0)
    assert (vetor, observadas) == ([-95.0, -150.0], 1)


def test_politica_de_ausencia_validada_contra_o_observado():
    assert MissingPolicy.parse("sentinel:-140").resolve(-120.0) == -140.0
    assert MissingPolicy.parse("floor_offset:10").resolve(-131.0) == -141.0
    with pytest.raises(MissingValueError, match="menor RSRP observado"):
        MissingPolicy.parse("sentinel:-140").resolve(-145.0)
    with pytest.raises(MissingValueError, match="0 dBm"):
        MissingPolicy.parse("sentinel:0").resolve(-120.0)
    for invalida in ("sentinela:-140", "floor_offset:0", "floor_offset:-5"):
        with pytest.raises(ValueError):
            MissingPolicy.parse(invalida)


def test_padronizacao_guarda_medias_e_desvios_e_trata_coluna_constante():
    escala = fit_scaling([[-100.0, -140.0], [-90.0, -140.0]], "standardized")
    assert escala == {"means": [-95.0, -140.0], "stds": [5.0, 1.0]}
    assert apply_scaling([-100.0, -140.0], escala) == [-1.0, 0.0]
    assert fit_scaling([[-100.0]], "raw") is None


def test_mesmo_vetor_no_treino_e_na_inferencia_e_moni_nao_entra():
    observacoes = [observacao(CELULAS[0], -101.0), observacao(CELULAS[1], 0.0), observacao(CELULAS[0], -60.0, source="MONI")]
    do_snapshot = cells_from_observations(observacoes)
    do_banco = (cel("LTE:3375:254", rsrp=-101.0), cel("LTE:3375:31", rsrp=None))
    chaves = ["LTE:3375:254", "LTE:3375:31"]
    assert vectorize(do_snapshot, POLITICA, chaves, -140.0) == vectorize(do_banco, POLITICA, chaves, -140.0) == ([-101.0, -140.0], 1)


# --------------------------------------------------------------------------- WKNN
def test_wknn_pesos_inversos_e_distancia_zero():
    xs, ys = [0.0, 10.0, 100.0], [0.0, 0.0, 0.0]
    x, y, zero, usados = wknn_from_sorted([(1.0, 0), (3.0, 1), (9.0, 2)], xs, ys, 2)
    assert (round(x, 6), y, zero, usados) == (2.5, 0.0, False, [0, 1])
    x, y, zero, usados = wknn_from_sorted([(0.0, 1), (0.0, 0), (2.0, 2)], xs, ys, 3)
    assert (x, zero, usados) == (5.0, True, [1, 0])  # média simples dos vizinhos exatos


def test_distancia_zero_usa_todas_as_amostras_exatas_mesmo_alem_de_k():
    xs, ys = [0.0, 10.0, 100.0, 40.0], [0.0, 0.0, 0.0, 30.0]
    x, y, zero, usados = wknn_from_sorted([(0.0, 0), (0.0, 1), (0.0, 3), (2.0, 2)], xs, ys, 2)
    assert (x, y, zero, usados) == (pytest.approx(50 / 3), 10.0, True, [0, 1, 3])  # 3 exatas com k = 2, sem peso


def test_empate_na_k_esima_distancia_fica_com_a_amostra_anterior_do_bundle(treino_spatial):
    _, bundle, _ = treino_spatial
    copia = json.loads(json.dumps(bundle))
    fp = copia["fingerprints"]
    fp["rsrp_dbm"][1] = list(fp["rsrp_dbm"][0])  # duas amostras iguais em posições diferentes
    copia["n_neighbors"] = 1
    consulta = list(fp["rsrp_dbm"][0])
    consulta[0] += 0.001  # distância não nula e igual às duas
    x, y, zero, vizinhos = WknnModel(copia).predict_xy(consulta)
    assert not zero and vizinhos == [fp["measurement_id"][0]]
    assert (x, y) == (pytest.approx(fp["x_m"][0]), pytest.approx(fp["y_m"][0]))


# --------------------------------------------------------------------------- treino e bundle
def test_treino_spatial_gera_bundle_completo_e_valido(treino_spatial, radiomapa):
    saida, bundle, relatorio = treino_spatial
    for campo in ("model_type", "model_version", "technology", "cell_keys", "missing_value", "scaling_strategy",
                  "coordinate_system", "enu_origin", "n_neighbors", "distance_metric", "zero_distance_policy",
                  "cell_filter_policy", "training_groups", "dataset", "fingerprints"):
        assert campo in bundle
    assert (bundle["model_type"], bundle["model_version"], bundle["protocol"]) == ("wknn", 2, "spatial")
    assert bundle["training_groups"]["group_column"] == "reference_point_id"
    assert bundle["cell_filter_policy"] == relatorio["cell_filter_policy"] == POLITICA.to_dict()
    assert bundle["missing_value"] < bundle["missing_value_validation"]["observed_min_dbm"]
    assert "LTE:9410:21" not in bundle["cell_keys"] and "LTE:100:87" not in bundle["cell_keys"]  # outra operadora, proibida
    assert bundle["cell_key_plmn_status"]["LTE:3375:99"] == PLMN_UNCONFIRMED
    assert "fingerprints" not in relatorio and relatorio["validation"]["ranking"]
    with open(saida, encoding="utf-8") as f:
        assert json.load(f)["dataset"]["test_samples_sha256"] == bundle["dataset"]["test_samples_sha256"]
    teste = bundle["validation"]["test_metrics"]
    assert teste["n_total"] == len(bundle["dataset"]["test_measurement_ids"]) and teste["coverage"] == 1.0
    assert math.isfinite(teste["mean_m"]) and teste["mean_m"] < 150  # sanidade: dentro da área sintética
    assert bundle["software_version"] == software_version()
    assert bundle["training_groups"]["reference_point_ids"] == [1, 2, 3, 5, 6]  # P4 no teste
    v = bundle["validation"]
    assert v["subsets"]["test"] == {"groups": 1, "points": 1, "sessions": 3, "samples": 9}
    assert v["subsets"]["selection"] == {"groups": 5, "points": 5, "sessions": 15, "samples": 45}
    assert [f["train"]["groups"] for f in v["subsets"]["folds"]] == [4] * 5
    assert teste["composition"] == {"new_points": 9} and v["test_reference"]["status"] == "final"
    assert len(v["warnings"]) == 1 and "único grupo" in v["warnings"][0]


def test_vocabulario_e_fingerprints_vem_so_dos_grupos_de_selecao(treino_spatial, radiomapa):
    _, bundle, _ = treino_spatial
    amostras = load_dataset(radiomapa).samples
    selecao = samples_in_groups(amostras, "spatial", bundle["validation"]["split"]["selection_groups"])
    vistas = {c.radio_key for s in selecao for c in s.cells}
    assert set(bundle["cell_keys"]) <= vistas
    teste = set(bundle["dataset"]["test_measurement_ids"])
    assert not teste & set(bundle["fingerprints"]["measurement_id"])


def test_radio_key_vista_so_no_ponto_de_teste_fica_fora_do_vocabulario(radiomapa, tmp_path):
    bundle, _ = tr.train(radiomapa, "LTE", "spatial", str(tmp_path / "p6.json"), grid=tr.Grid(k=(1, 3)),
                         seed=SEMENTE_P6_NO_TESTE)
    assert bundle["validation"]["split"]["test_groups"] == [6]
    teste = [s for s in load_dataset(radiomapa).samples if s.measurement_id in bundle["dataset"]["test_measurement_ids"]]
    assert any(c.radio_key == LOCAL_KEY for s in teste for c in s.cells)
    assert LOCAL_KEY not in bundle["cell_keys"]
    estimativa = WknnModel(bundle).predict(next(s for s in teste if any(c.radio_key == LOCAL_KEY for c in s.cells)).cells)
    assert estimativa.valid and estimativa.metadata["n_ignored_keys"] >= 1  # ignorada, sem mudar o bundle


def test_cada_fold_ajusta_vocabulario_ausencia_e_escala_so_com_o_proprio_treino(radiomapa, monkeypatch):
    amostras, _ = tr.eligible_samples(load_dataset(radiomapa).samples, "spatial", POLITICA)
    split = make_group_split(amostras, "spatial")
    selecao = samples_in_groups(amostras, "spatial", split.selection_groups)
    ajustes, escalas = [], []
    fit_features, fit_scaling = tr.fit_features, tr.fit_scaling

    def espiao_features(samples, policy, missing):
        ajustado = fit_features(samples, policy, missing)
        ajustes.append(([s.measurement_id for s in samples], ajustado))
        return ajustado

    def espiao_escala(vetores, estrategia):
        params = fit_scaling(vetores, estrategia)
        escalas.append((len(vetores), params))
        return params

    monkeypatch.setattr(tr, "fit_features", espiao_features)
    monkeypatch.setattr(tr, "fit_scaling", espiao_escala)
    tr.select_candidate(selecao, split, POLITICA, tr.Grid(k=(1,), metrics=("L1",), scalings=("standardized",),
                                                       missing=("floor_offset:10",)))
    assert len(ajustes) == len(escalas) == len(split.folds) == 5
    com_chave_so_na_validacao = 0
    for (ids, ajustado), (linhas, params), (grupos_treino, grupos_validacao) in zip(ajustes, escalas, split.folds):
        treino = samples_in_groups(selecao, "spatial", grupos_treino)
        validacao = samples_in_groups(selecao, "spatial", grupos_validacao)
        assert ids == [s.measurement_id for s in treino]
        aceitas = [c for s in treino for c in s.cells if POLITICA.accepts(c)]
        assert set(ajustado.cell_keys) <= {c.radio_key for c in aceitas}
        rsrp_treino = [v for s in treino for k, v in rsrp_by_key(s.cells, POLITICA).items() if k in ajustado.cell_keys]
        assert ajustado.missing_value == min(rsrp_treino) - 10
        assert linhas == len(treino) == len(ajustado.raw)
        medias = [statistics.fmean(v[j] for v in ajustado.raw) for j in range(len(ajustado.cell_keys))]
        assert params["means"] == pytest.approx(medias)
        chaves_validacao = {c.radio_key for s in validacao for c in s.cells if POLITICA.accepts(c)}
        com_chave_so_na_validacao += bool(chaves_validacao - set(ajustado.cell_keys))
    assert com_chave_so_na_validacao >= 1  # o fold que valida em P6 não conhece LOCAL_KEY


def test_inferencia_pelo_bundle_salvo(treino_spatial, radiomapa):
    saida, bundle, _ = treino_spatial
    modelo = load_bundle(saida)
    amostra = next(s for s in load_dataset(radiomapa).samples if s.measurement_id in bundle["dataset"]["test_measurement_ids"])
    estimativa = modelo.predict(amostra.cells)
    assert estimativa.valid and math.isfinite(estimativa.lat) and math.isfinite(estimativa.lon)
    assert (estimativa.lat, estimativa.lon) == pytest.approx(modelo.frame.to_geodetic(estimativa.x_m, estimativa.y_m)[:2])
    assert modelo.predict([cel("NR:629952:123", rat="NR")]).status == "technology_mismatch"
    assert modelo.predict([cel("LTE:9999:1")]).status == "no_matching_radio_keys"


def test_selecao_nao_usa_o_teste(radiomapa, tmp_path, monkeypatch):
    original = tr.load_dataset
    _, rel_a = tr.train(radiomapa, "LTE", "spatial", str(tmp_path / "a.json"))
    teste = set(rel_a["dataset"]["test_measurement_ids"])

    def dataset_com_teste_adulterado(*args, **kwargs):
        ds = original(*args, **kwargs)
        ds.samples = [dataclasses.replace(s, cells=tuple(dataclasses.replace(c, rsrp_dbm=-60.0) for c in s.cells))
                      if s.measurement_id in teste else s for s in ds.samples]
        return ds

    monkeypatch.setattr(tr, "load_dataset", dataset_com_teste_adulterado)
    _, rel_b = tr.train(radiomapa, "LTE", "spatial", str(tmp_path / "b.json"))
    assert rel_b["validation"]["ranking"] == rel_a["validation"]["ranking"]
    assert rel_b["validation"]["chosen"] == rel_a["validation"]["chosen"]
    assert rel_b["validation"]["test_metrics"] != rel_a["validation"]["test_metrics"]


def test_protocolo_temporal_e_registrado_a_parte(radiomapa, tmp_path):
    bundle, _ = tr.train(radiomapa, "LTE", "temporal", str(tmp_path / "t.json"))
    assert bundle["protocol"] == "temporal" and bundle["training_groups"]["group_column"] == "session_id"
    assert bundle["validation"]["split"]["test_groups"] == [15, 16, 17, 18]  # 4 sessões mais recentes (dia 3)
    v = bundle["validation"]
    assert v["test_metrics"]["composition"] == {"known_points": 12}  # P3 a P6 também estão no treino
    assert (v["test_reference"]["status"], v["test_reference"]["gnss_median_shared_points"]) == ("exploratoria", [5])
    assert any("independência temporal completa" in w for w in v["warnings"])


def test_grupos_insuficientes_e_k_incompativel_encerram_com_mensagem(tmp_path, radiomapa):
    pequeno = tmp_path / "pequeno.db"
    criar_radiomapa(pequeno, dias=1, pontos={"P1": (-60, -60, True), "P2": (60, 60, True)}, extras=False)
    with pytest.raises(SystemExit, match="spatial_validation_unavailable: insufficient_groups"):
        tr.main(["--db", str(pequeno), "--technology", "LTE", "--protocol", "spatial", "--out", str(tmp_path / "x.json")])
    with pytest.raises(DatasetError, match="insufficient_samples"):
        tr.train(radiomapa, "LTE", "spatial", str(tmp_path / "y.json"), grid=tr.Grid(k=(500,)))
    assert not (tmp_path / "x.json").exists() and not (tmp_path / "y.json").exists()


def test_cli_de_treino_gera_bundle_e_relatorio(radiomapa, tmp_path, capsys):
    saida, relatorio = tmp_path / "cli.json", tmp_path / "cli_relatorio.json"
    tr.main(["--db", radiomapa, "--technology", "LTE", "--protocol", "spatial", "--out", str(saida),
             "--report", str(relatorio), "--k", "1,3", "--missing", "sentinel:-145"])
    texto = capsys.readouterr().out
    assert "Protocolo: spatial (grupo = reference_point_id)" in texto and "fora da seleção" in texto
    assert "Referência do teste: final" in texto and "AVISO: o teste tem um único grupo" in texto
    assert load_bundle(str(saida)).bundle["missing_policy"] == "sentinel:-145"
    assert json.loads(relatorio.read_text(encoding="utf-8"))["validation"]["grid"]["k"] == [1, 3]
    with pytest.raises(SystemExit, match="já existe"):
        tr.main(["--db", radiomapa, "--technology", "LTE", "--protocol", "spatial", "--out", str(saida)])


# --------------------------------------------------------------------------- validação do bundle
@pytest.mark.parametrize("alteracao, mensagem", [
    (lambda b: b.update(model_version=1), "model_version"),
    (lambda b: b.update(model_type="knn"), "model_type"),
    (lambda b: b.pop("enu_origin"), "metadados ausentes"),
    (lambda b: b["fingerprints"]["rsrp_dbm"][0].pop(), "um por cell_key"),
    (lambda b: b.update(n_neighbors=0), "n_neighbors"),
    (lambda b: b.update(n_neighbors=10_000), "n_neighbors"),
    (lambda b: b.update(technology="NR"), "cell_keys"),
    (lambda b: b.update(missing_value=0), "0 dBm"),
    (lambda b: b.update(scaling_strategy="standardized", scaling=None), "padronização"),
])
def test_bundle_incompativel_e_recusado(treino_spatial, alteracao, mensagem):
    _, bundle, _ = treino_spatial
    copia = json.loads(json.dumps(bundle))
    alteracao(copia)
    with pytest.raises(BundleError, match=mensagem):
        validate_bundle(copia)
    with pytest.raises(BundleError):
        WknnModel(copia)


def test_bundle_joblib_antigo_e_recusado_sem_abrir(tmp_path):
    antigo = tmp_path / "fingerprint_5g.joblib"
    antigo.write_bytes(pickle.dumps({"modelo": "antigo"}))
    conteudo = antigo.read_bytes()
    with pytest.raises(BundleError, match="retreine|treine de novo"):
        load_bundle(str(antigo))
    disfarcado = tmp_path / "disfarcado.json"
    disfarcado.write_bytes(conteudo)
    with pytest.raises(BundleError, match="pickle"):
        load_bundle(str(disfarcado))
    assert antigo.read_bytes() == conteudo


def test_save_bundle_nao_sobrescreve_e_exige_json(treino_spatial, tmp_path):
    saida, bundle, _ = treino_spatial
    tamanho = os.path.getsize(saida)
    with pytest.raises(FileExistsError):
        save_bundle(bundle, saida)
    with pytest.raises(BundleError, match=".json"):
        save_bundle(bundle, str(tmp_path / "modelo.joblib"))
    assert os.path.getsize(saida) == tamanho


def test_codigo_ativo_sem_bibliotecas_numericas_nem_api_antiga_do_fingerprint():
    import re

    raiz = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    proibido = re.compile(r"^\s*(from|import)\s+(sklearn|joblib|pandas|numpy)\b", re.MULTILINE)
    for pacote in ("acquisition", "coletor", "estimador", "hal"):
        for pasta, _, arquivos in os.walk(os.path.join(raiz, pacote)):
            for nome in arquivos:
                if nome.endswith(".py"):
                    with open(os.path.join(pasta, nome), encoding="utf-8") as f:
                        assert not proibido.search(f.read()), os.path.join(pasta, nome)
    for nome in ("requirements.txt", "requirements-dev.txt"):
        with open(os.path.join(raiz, nome), encoding="utf-8") as f:
            assert not re.search(r"scikit-learn|joblib|pandas|numpy", f.read()), nome
    import estimador.methods.fingerprint as fp

    assert not hasattr(fp, "train_fingerprint") and not hasattr(fp, "predict_fingerprint")
