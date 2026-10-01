"""Estimador: leitura do radiomapa v2 e divisões por grupo (spatial e temporal)."""
import dataclasses
import sqlite3

import pytest

from radiomapa_sintetico import CAMPAIGN_PLMN, CELULAS, criar_radiomapa, info_json, inserir_medicao, observacao

import coletor.db as cdb
from estimador.dataset import (
    REF_GNSS_MEDIAN,
    REF_GNSS_SINGLE,
    REF_REGISTERED,
    DatasetError,
    load_dataset,
    make_group_split,
    resolve_campaign_plmn,
    samples_in_groups,
    samples_sha256,
)
from estimador.geometry import haversine_m


@pytest.fixture
def radiomapa(tmp_path):
    caminho = tmp_path / "radiomapa_v2.db"
    verdade = criar_radiomapa(caminho)
    return str(caminho), verdade


def banco_com_um_ponto(caminho):
    conn = cdb.connect(str(caminho))
    pid = cdb.add_reference_point(conn, "P1", -26.9, -49.07)
    sid = cdb.create_session(conn, mode="once", scenario="estatico", environment="outdoor", modem_id="sintetico",
                             metadata={}, reference_point_id=pid, campaign_plmn=CAMPAIGN_PLMN)
    return conn, sid, pid


def test_dataset_le_so_v2_sem_alterar_e_exclui_linhas_inadequadas(radiomapa):
    caminho, _ = radiomapa
    antes = info_json(caminho)
    ds = load_dataset(caminho)
    assert len(ds.samples) == 3 * 6 * 3
    assert ds.excluded == {"legacy_ambiguous": 1, "snapshot_invalid": 1, "csurv_indisponivel": 1,
                           "rota_sem_referencia_permitida": 3}
    assert info_json(caminho) == antes


def test_banco_v1_e_recusado_com_instrucao_de_migracao(tmp_path):
    v1 = tmp_path / "v1.db"
    conn = sqlite3.connect(v1)
    conn.execute("CREATE TABLE measurements (measurement_id INTEGER PRIMARY KEY)")
    conn.close()
    with pytest.raises(DatasetError, match="schema_v1: .*coletor.migrate"):
        load_dataset(str(v1))
    with pytest.raises(DatasetError, match="radiomap_not_found"):
        load_dataset(str(tmp_path / "nao_existe.db"))
    assert not (tmp_path / "nao_existe.db").exists()


def test_prioridade_da_referencia_posicional(radiomapa):
    caminho, verdade = radiomapa
    ds = load_dataset(caminho)
    por_fonte = {s.reference.source: s for s in ds.samples}
    cadastrada = por_fonte[REF_REGISTERED].reference
    assert (cadastrada.coord_source, cadastrada.uncertainty_m) == ("sintetico", 0.5)
    mediana = por_fonte[REF_GNSS_MEDIAN].reference
    assert mediana.n_gnss == 9 and 0 < mediana.dispersion_m < 6
    assert haversine_m(mediana.lat, mediana.lon, *verdade["P5"]) < 4
    assert REF_GNSS_SINGLE not in por_fonte
    com_rota = load_dataset(caminho, allow_single_gnss=True)
    assert sum(s.reference.source == REF_GNSS_SINGLE for s in com_rota.samples) == 3
    assert com_rota.summary()["reference_policy"]["allow_single_gnss"] is True


def test_mediana_gnss_exige_numero_minimo_de_leituras(tmp_path):
    caminho = tmp_path / "curto.db"
    criar_radiomapa(caminho, dias=1, por_sessao=2, extras=False)
    ds = load_dataset(str(caminho))
    assert ds.excluded == {"ponto_sem_referencia": 2}
    assert all(s.reference.source == REF_REGISTERED for s in ds.samples)


def test_so_o_csurv_entra_e_moni_nao_substitui_valores(tmp_path):
    caminho = tmp_path / "moni.db"
    conn, sid, pid = banco_com_um_ponto(caminho)
    celula = CELULAS[0]
    with conn:
        inserir_medicao(conn, sid, 1, pid, "2026-09-01T10:00:01.000Z",
                        [observacao(celula, -100.0), observacao(celula, -60.0, source="MONI")])
    conn.close()
    (amostra,) = load_dataset(str(caminho)).samples
    assert [(c.radio_key, c.rsrp_dbm) for c in amostra.cells] == [("LTE:3375:254", -100.0)]


def test_rsrp_zero_do_csurv_vira_ausente(tmp_path):
    caminho = tmp_path / "zero.db"
    conn, sid, pid = banco_com_um_ponto(caminho)
    with conn:
        inserir_medicao(conn, sid, 1, pid, "2026-09-01T10:00:01.000Z", [observacao(CELULAS[1], 0.0)])
    conn.close()
    (amostra,) = load_dataset(str(caminho)).samples
    assert amostra.cells[0].rsrp_dbm is None


def test_hash_do_dataset_e_estavel_e_sensivel_ao_conteudo(radiomapa):
    caminho, _ = radiomapa
    a, b = load_dataset(caminho), load_dataset(caminho)
    assert samples_sha256(a.samples) == samples_sha256(list(reversed(b.samples)))
    assert samples_sha256(a.samples) != samples_sha256(a.samples[:-1])


def test_plmn_da_campanha_vem_das_sessoes_ou_do_argumento(radiomapa):
    caminho, _ = radiomapa
    amostras = load_dataset(caminho).samples
    assert resolve_campaign_plmn(amostras, None) == CAMPAIGN_PLMN
    assert resolve_campaign_plmn(amostras, "72405") == "72405"
    sem_plmn = [dataclasses.replace(s, campaign_plmn=None) for s in amostras]
    with pytest.raises(DatasetError, match="campaign_plmn_unknown"):
        resolve_campaign_plmn(sem_plmn, None)
    conflito = [dataclasses.replace(amostras[0], campaign_plmn="72405")] + amostras[1:]
    with pytest.raises(DatasetError, match="campaign_plmn_conflict"):
        resolve_campaign_plmn(conflito, None)


def test_divisao_spatial_mantem_cada_ponto_de_um_lado_so(radiomapa):
    caminho, _ = radiomapa
    amostras = load_dataset(caminho).samples
    split = make_group_split(amostras, "spatial")
    assert split.test_groups and not set(split.test_groups) & set(split.selection_groups)
    assert len(split.folds) >= 2 and split.ungrouped_samples == 0
    for treino, validacao in split.folds:
        assert not set(treino) & set(validacao) and set(treino) | set(validacao) == set(split.selection_groups)
    teste = samples_in_groups(amostras, "spatial", split.test_groups)
    assert {s.reference_point_id for s in teste} == set(split.test_groups)
    assert make_group_split(amostras, "spatial") == split  # determinístico


def test_divisao_temporal_reserva_as_sessoes_mais_recentes(radiomapa):
    caminho, _ = radiomapa
    amostras = load_dataset(caminho).samples
    split = make_group_split(amostras, "temporal")
    inicio = {s.session_id: s.session_started_at for s in amostras}
    assert min(inicio[g] for g in split.test_groups) > max(inicio[g] for g in split.selection_groups)
    assert not set(split.test_groups) & set(split.selection_groups)


def test_grupos_insuficientes_encerram_sem_divisao_por_linha(tmp_path):
    caminho = tmp_path / "pequeno.db"
    criar_radiomapa(caminho, dias=1, pontos={"P1": (-60, -60, True), "P2": (60, 60, True)}, extras=False)
    amostras = load_dataset(str(caminho)).samples
    with pytest.raises(DatasetError, match="spatial_validation_unavailable: insufficient_groups"):
        make_group_split(amostras, "spatial")
    with pytest.raises(DatasetError, match="temporal_validation_unavailable: insufficient_groups"):
        make_group_split(amostras, "temporal")


def test_rotas_ficam_fora_do_protocolo_spatial(radiomapa):
    caminho, _ = radiomapa
    split = make_group_split(load_dataset(caminho, allow_single_gnss=True).samples, "spatial")
    assert split.ungrouped_samples == 3
