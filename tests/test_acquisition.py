"""Aquisição compartilhada: snapshot com status, motivos, latências e 1 #CSURV por ciclo."""
import json
import sys

from support import at_lines, driver_com

from acquisition.snapshot import take_snapshot
from hal.models import CellObservation, to_jsonable

CICLO = {
    "AT#MONI": "moni_lte.txt",
    "AT#SERVINFO": "servinfo_lte.txt",
    "AT#CSURV": "csurv_vivo_campo.txt",
    "AT$GPSACP": "gpsacp_fix.txt",
}
# Comandos que um ciclo pode enviar; nenhum grava configuração no modem.
COMANDOS_PERMITIDOS = {"AT#MONI", "AT#SERVINFO", "AT#CSURV", "AT$GPSACP", "AT$LCSLPP?"}


def ciclo(**trocas):
    roteiro = dict(CICLO)
    roteiro.update(trocas)
    return roteiro


def test_snapshot_completo_lte(sem_espera):
    driver = driver_com(CICLO, sem_espera)
    s = take_snapshot(driver)
    assert (s.status, s.reasons, s.access_mode) == ("complete", [], "LTE")
    assert (s.serving.radio_key, s.serving.global_cell_key) == ("LTE:3375:254", "LTE:72406:F560E67")
    assert (s.serving_rsrp_dbm, s.serving_rsrq_db) == (-107.0, -12.0)
    assert [o.source for o in s.observations] == ["MONI", "SERVINFO", "CSURV", "CSURV"]
    servidoras_csurv = [o for o in s.observations if o.source == "CSURV" and o.is_serving]
    assert [o.radio_key for o in servidoras_csurv] == ["LTE:3375:254"]
    assert set(s.timings_s) == {"MONI", "SERVINFO", "CSURV", "GNSS", "total"}
    assert s.gnss.fix.hdop == 0.8 and s.started_at.endswith("Z")


def test_snapshot_roda_comandos_na_ordem_e_so_le(sem_espera):
    driver = driver_com(CICLO, sem_espera)
    take_snapshot(driver)
    assert driver._ser.written == ["AT#MONI", "AT#SERVINFO", "AT#CSURV", "AT$GPSACP"]
    assert set(driver._ser.written) <= COMANDOS_PERMITIDOS


def test_snapshot_sem_fix_gnss_e_parcial(sem_espera):
    s = take_snapshot(driver_com(ciclo(**{"AT$GPSACP": "gpsacp_sem_fix.txt"}), sem_espera))
    assert (s.status, s.reasons) == ("partial", ["gnss:no_fix"])


def test_snapshot_com_csurv_vazio_fica_sem_radio_key(sem_espera):
    s = take_snapshot(driver_com(ciclo(**{"AT#CSURV": "csurv_vazio.txt"}), sem_espera))
    assert s.status == "partial"
    assert s.reasons == ["csurv:empty", "servidora_sem_radio_key:no_match"]
    assert s.serving.global_cell_key == "LTE:72406:F560E67"  # vem do #SERVINFO


def test_snapshot_sem_nenhuma_celula_e_invalido(sem_espera):
    roteiro = ciclo(**{"AT#MONI": "error.txt", "AT#SERVINFO": "error.txt", "AT#CSURV": "csurv_abortado.txt"})
    s = take_snapshot(driver_com(roteiro, sem_espera))
    assert s.status == "invalid" and s.observations == []
    assert s.reasons[:3] == ["moni:error", "servinfo:error", "csurv:error"]


def test_modem_mudo_nao_espera_o_csurv(sem_espera):
    driver = driver_com({}, sem_espera)
    driver._ser.responses = {"AT#MONI": [], "AT#SERVINFO": []}
    s = take_snapshot(driver)
    assert driver._ser.written == ["AT#MONI", "AT#SERVINFO"]
    assert s.status == "invalid" and "modem_sem_resposta" in s.reasons
    assert (s.csurv.status, s.gnss.status) == ("skipped", "skipped")
    assert s.timings_s["total"] < 15.0


def test_erro_de_porta_nao_levanta_excecao(sem_espera):
    def porta_caiu():
        raise OSError("porta desconectada")

    driver = driver_com(ciclo(**{"AT#SERVINFO": porta_caiu}), sem_espera)
    s = take_snapshot(driver)
    assert s.transport_error.startswith("SERVINFO: OSError")
    assert driver._ser.written == ["AT#MONI", "AT#SERVINFO"]
    assert s.status == "partial" and "erro_de_porta" in s.reasons and s.csurv.status == "skipped"


def test_sonda_lpp_opcional_e_somente_leitura(sem_espera):
    driver = driver_com(ciclo(**{"AT$LCSLPP?": "lcslpp_consulta.txt"}), sem_espera)
    s = take_snapshot(driver, include_lpp_probe=True)
    assert driver._ser.written[-1] == "AT$LCSLPP?" and set(driver._ser.written) <= COMANDOS_PERMITIDOS
    assert s.lpp_probe.status == "ok" and s.status == "complete"


def test_snapshot_nsa_guarda_perna_nr(sem_espera):
    s = take_snapshot(driver_com(ciclo(**{"AT#MONI": "moni_endc_nr_conectado.txt"}), sem_espera))
    assert s.access_mode == "NR_NSA" and s.serving.nr_leg_radio_key == "NR:629952:123"
    assert "NR:629952:123" in [o.radio_key for o in s.observations if o.source == "MONI"]


def test_snapshot_serializa_em_json(sem_espera):
    s = take_snapshot(driver_com(CICLO, sem_espera))
    dados = json.loads(json.dumps(to_jsonable(s)))
    assert dados["serving"]["radio_key"] == "LTE:3375:254"
    assert dados["observations"][3]["global_cell_key"] == "LTE:72406:F560E67"
    assert dados["moni"]["response"]["raw_lines"] == at_lines("moni_lte.txt")


def test_cli_do_snapshot(monkeypatch, capsys, sem_espera):
    import acquisition.snapshot as snap
    import hal.fn990_driver as fd

    driver = driver_com(ciclo(**{"AT$GPSP?": "gpsp_ligado.txt"}), sem_espera)
    driver.connect = lambda *args, **kwargs: None
    monkeypatch.setattr(fd, "FN990Driver", lambda: driver)
    monkeypatch.setattr(sys, "argv", ["acquisition.snapshot", "--port", "PORTA"])
    snap.main()
    saida = json.loads(capsys.readouterr().out)
    assert saida["prepare"]["gnss_power_on_sent"] is False and saida["snapshot"]["status"] == "complete"


def test_observacao_da_servidora_no_csurv_mantem_as_chaves():
    obs = CellObservation(rat="LTE", source="CSURV", arfcn=3375, pci=254, plmn="72406", cell_id="F560E67")
    import dataclasses

    marcada = dataclasses.replace(obs, is_serving=True)
    assert (marcada.radio_key, marcada.global_cell_key) == (obs.radio_key, obs.global_cell_key)
