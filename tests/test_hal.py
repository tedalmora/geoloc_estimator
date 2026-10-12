"""HAL: chaves de identidade, transporte AT, parsers puros e sondas somente leitura."""
import dataclasses
import json
import math
import pathlib
import re
import sys

import pytest

from support import ScriptedSerial, at_lines, driver_com

from hal.models import (
    AtResponse,
    CellObservation,
    make_global_cell_key,
    make_radio_key,
)
from hal.parsing import (
    nmea_to_decimal,
    parse_csurv,
    parse_gpsacp,
    parse_moni,
    parse_servinfo,
    resolve_serving_cell,
)

ROOT = pathlib.Path(__file__).resolve().parents[1]


def resposta(comando, fixture):
    return AtResponse.from_raw(comando, at_lines(fixture))


def csurv(fixture):
    return parse_csurv(resposta("AT#CSURV", fixture))


# --------------------------------------------------------------------------- chaves
def test_radio_key_exige_rat_arfcn_e_pci():
    assert make_radio_key("LTE", 3375, 254) == "LTE:3375:254"
    assert make_radio_key("LTE", 3375, 0) == "LTE:3375:0"  # PCI 0 é válido
    assert make_radio_key("LTE", None, 254) is None
    assert make_radio_key("LTE", 3375, None) is None
    assert make_radio_key("5G", 3375, 254) is None


def test_global_cell_key_exige_plmn_e_cell_id_canonico():
    assert make_global_cell_key("LTE", "72406", "0f560e67") == "LTE:72406:F560E67"
    assert make_global_cell_key("LTE", None, "F560E67") is None
    assert make_global_cell_key("LTE", "7240", "F560E67") is None
    assert make_global_cell_key("NR", "45008", "zz") is None


def test_observacao_e_imutavel_e_expoe_identidade():
    obs = CellObservation(rat="LTE", source="CSURV", arfcn=3375, pci=254)
    with pytest.raises(dataclasses.FrozenInstanceError):
        obs.pci = 1
    assert obs.identity_status == "radio"
    assert obs.to_dict()["radio_key"] == "LTE:3375:254"


# --------------------------------------------------------------------------- AtResponse e transporte
def test_at_response_separa_eco_dados_e_codigo_final():
    r = resposta("AT#MONI", "moni_lte.txt")
    assert r.ok and r.lines == ["#MONI: VIVO RSRP:-107 RSRQ:-12 TAC:87BB Id:F560E67 EARFCN:3375 PWR:-76dbm DRX:640"]
    assert resposta("AT#MONI", "cme_error_4.txt").error_code == 4
    assert resposta("AT#MONI", "vazio.txt").timed_out


def test_send_at_termina_comando_com_cr(sem_espera):
    driver = driver_com({"AT": ["AT", "OK"]}, sem_espera)
    driver.send_at("AT")
    assert driver._ser.raw_written == [b"AT\r"]


def test_send_at_restaura_timeout_da_porta(sem_espera):
    driver = driver_com({"AT": ["AT", "OK"]}, sem_espera)
    driver._ser.timeout = 7.0
    driver.send_at("AT")
    assert driver._ser.timeout == 7.0


def test_send_at_descarta_e_conta_sobras_anteriores(sem_espera):
    driver = driver_com({"AT": ["AT", "OK"]}, sem_espera)
    driver._ser.buffer.extend(b"+CREG: 1\r\n")
    r = driver.send_at("AT")
    assert r.ok and r.lines == [] and r.pending_bytes_before == len(b"+CREG: 1\r\n")


def test_csurv_descarta_bytes_que_chegam_depois_do_ok(sem_espera):
    linhas = at_lines("csurv_vivo_campo.txt") + ["earfcn: 9999 rxLev: -90 phyCellId: 1"]
    driver = driver_com({"AT#CSURV": linhas, "AT#MONI": "moni_lte.txt"}, sem_espera)
    leitura = driver.scan_cells()
    assert leitura.response.drained_bytes_after > 0
    assert 9999 not in [o.arfcn for o in leitura.observations]
    assert driver.read_serving_cell().anomalies == []


def test_csurv_respeita_intervalo_minimo_entre_varreduras(sem_espera):
    driver = driver_com({"AT#CSURV": "csurv_vivo_campo.txt"}, sem_espera)
    driver.scan_cells()
    antes = sem_espera.monotonic()
    driver.scan_cells()
    assert sem_espera.monotonic() - antes >= 2.0


def test_csurv_sem_codigo_final_respeita_prazo_de_3_minutos(sem_espera):
    driver = driver_com({"AT#CSURV": ["AT#CSURV", "Network survey started ..."]}, sem_espera)
    leitura = driver.scan_cells()
    assert leitura.status == "timeout" and 180.0 <= leitura.response.elapsed_s < 181.0


def test_connect_descarta_sobras_e_confirma_com_at(monkeypatch, sem_espera):
    import hal.fn990_driver as fd

    porta = ScriptedSerial({"AT": ["AT", "OK"]}, pending=["phyCellId: 254 cellStatus: CELL_SUITABLE"], clock=sem_espera)
    monkeypatch.setattr(fd.serial, "Serial", lambda *a, **k: porta)
    fd.FN990Driver().connect("PORTA")
    assert porta.written == ["AT"]


def test_connect_levanta_connection_error_se_at_der_erro(monkeypatch, sem_espera):
    import hal.fn990_driver as fd

    porta = ScriptedSerial({"AT": ["AT", "ERROR"]}, clock=sem_espera)
    monkeypatch.setattr(fd.serial, "Serial", lambda *a, **k: porta)
    with pytest.raises(ConnectionError):
        fd.FN990Driver().connect("PORTA")
    assert porta.written == ["AT"] * 3 and not porta.is_open


def test_connect_porta_inexistente_vira_connection_error(monkeypatch):
    import hal.fn990_driver as fd

    def falha(*args, **kwargs):
        raise fd.serial.SerialException("could not open port")

    monkeypatch.setattr(fd.serial, "Serial", falha)
    with pytest.raises(ConnectionError):
        fd.FN990Driver().connect("NAO_EXISTE")


# --------------------------------------------------------------------------- preparação e sondas
def test_prepare_liga_gnss_so_se_estiver_desligado(sem_espera):
    driver = driver_com({"AT$GPSP?": "gpsp_desligado.txt", "AT$GPSP=1": ["AT$GPSP=1", "OK"]}, sem_espera)
    r = driver.prepare_for_collection()
    assert driver._ser.written == ["AT$GPSP?", "AT$GPSP=1"]
    assert (r.gnss_power_before, r.gnss_power_on_sent, r.gnss_power_on_final) == (0, True, "OK")


def test_prepare_nao_reinicia_gnss_ligado(sem_espera):
    driver = driver_com({"AT$GPSP?": "gpsp_ligado.txt"}, sem_espera)
    r = driver.prepare_for_collection()
    assert driver._ser.written == ["AT$GPSP?"] and (r.gnss_power_before, r.gnss_power_on_sent) == (1, False)


def test_prepare_nao_liga_gnss_se_a_consulta_falhar(sem_espera):
    driver = driver_com({}, sem_espera)
    r = driver.prepare_for_collection()
    assert driver._ser.written == ["AT$GPSP?"] and r.gnss_power_before is None


def test_codigo_ativo_nao_grava_lcslpp():
    ativos = [p for p in ROOT.rglob("*.py") if not {"venv", ".venv", "tests", "deprecated"} & set(p.relative_to(ROOT).parts)]
    assert [str(p) for p in ativos if re.search(r"LCSLPP\s*=", p.read_text(encoding="utf-8"))] == []


# --------------------------------------------------------------------------- #MONI
def test_moni_com_nome_de_rede_nao_tem_plmn():
    m = parse_moni(resposta("AT#MONI", "moni_lte.txt"))
    assert m.network_name == "VIVO" and m.lte.plmn is None and m.lte.identity_status == "none"
    assert (m.lte.cell_id, m.lte.tac, m.lte.arfcn, m.lte.rx_level_dbm) == ("F560E67", "87BB", 3375, -76.0)


def test_moni_cc_nc_da_plmn_e_chave_global():
    m = parse_moni(resposta("AT#MONI", "moni_lte_cc_nc.txt"))
    assert m.network_name is None and m.lte.global_cell_key == "LTE:72406:F560E67"


def test_moni_endc_conectado_tem_perna_nr_identificada():
    m = parse_moni(resposta("AT#MONI", "moni_endc_nr_conectado.txt"))
    assert m.nr.radio_key == "NR:629952:123" and m.nr.sinr_db == 15.0 and m.primary is m.lte


def test_moni_endc_ocioso_fica_lte_sem_observacao_nr():
    m = parse_moni(resposta("AT#MONI", "moni_endc_nr_ocioso.txt"))
    assert (m.access_mode, m.nr, m.nr_state) == ("LTE", None, 1)


def test_moni_formato_de_celula_adjacente_e_rejeitado():
    m = parse_moni(resposta("AT#MONI", "moni_formato_vizinha.txt"))
    assert m.status == "malformed" and "formato_celula_adjacente" in m.anomalies and m.observations == []


def test_moni_contaminado_por_csurv_e_rejeitado():
    m = parse_moni(resposta("AT#MONI", "moni_contaminado_csurv.txt"))
    assert m.status == "malformed" and "contaminacao_csurv" in m.anomalies


def test_moni_error_simples():
    assert parse_moni(resposta("AT#MONI", "error.txt")).status == "error"


# --------------------------------------------------------------------------- #SERVINFO
def test_servinfo_lte_traz_plmn_e_chave_global():
    s = parse_servinfo(resposta("AT#SERVINFO", "servinfo_lte.txt"))
    assert (s.status, s.access_mode, s.network_name, s.endc) == ("ok", "LTE", "VIVO", False)
    assert (s.lte.global_cell_key, s.lte.rsrp_dbm, s.lte.arfcn, s.lte.tac) == ("LTE:72406:F560E67", -107.0, 3375, "87BB")


def test_servinfo_endc_tem_canal_nr_sem_identidade():
    s = parse_servinfo(resposta("AT#SERVINFO", "servinfo_endc.txt"))
    assert s.endc and (s.nr.arfcn, s.nr.rsrq_db, s.nr.identity_status) == (629952, -11.0, "none")


def test_servinfo_nr_sa():
    s = parse_servinfo(resposta("AT#SERVINFO", "servinfo_nr_sa.txt"))
    assert (s.access_mode, s.nr.rsrp_dbm, s.lte) == ("NR_SA", -95.0, None)


def test_servinfo_formato_wcdma_nao_e_tratado():
    r = AtResponse.from_raw("AT#SERVINFO", ['#SERVINFO: 10700,-80,"X","72406",12,1A2B,7,3,-80,I,1', "OK"])
    assert parse_servinfo(r).status == "unsupported"


# --------------------------------------------------------------------------- #CSURV
def test_csurv_exemplo_do_manual_preserva_todas_as_celulas():
    c = csurv("csurv_manual_exemplo.txt")
    chaves = [o.radio_key for o in c.observations]
    assert c.status == "ok" and len(chaves) == 17 and len(set(chaves)) == 17 and None not in chaves
    assert {o.plmn for o in c.observations if o.record_format == "full"} == {"45005", "45006", "45008", "45030"}
    assert sum(o.cell_status == "CELL_FORBIDDEN" for o in c.observations) == 8
    curtas = [o for o in c.observations if o.record_format == "short"]
    assert len(curtas) == 3 and all(o.plmn is None and o.global_cell_key is None for o in curtas)
    assert c.anomalies == []


def test_csurv_registro_nr_do_manual():
    (nr,) = [o for o in csurv("csurv_manual_exemplo.txt").observations if o.rat == "NR"]
    assert (nr.arfcn, nr.pci, nr.plmn, nr.band, nr.scs_khz, nr.rsrp_dbm, nr.rsrq_db) == (636672, 639, "45008", 78, 30, -94.0, -11.0)
    assert (nr.cell_id, nr.tac) == (f"{810784393:X}", f"{10485767:X}")
    assert nr.global_cell_key == f"NR:45008:{810784393:X}"


def test_csurv_de_campo_converte_ids_decimais_para_hexadecimal():
    c = csurv("csurv_vivo_campo.txt")
    lte = next(o for o in c.observations if o.rat == "LTE")
    assert (lte.cell_id, lte.tac, lte.plmn, lte.bandwidth, lte.identity_status) == ("F560E67", "87BB", "72406", 15, "complete")
    nr = next(o for o in c.observations if o.rat == "NR")
    assert nr.radio_key is None and nr.plmn == "72406"
    assert any("registro_incompleto" in a for a in c.anomalies)

def test_csurv_recupera_tokens_fragmentados_em_campo():
    leitura = csurv("csurv_fragmentacao_tokens_real.txt")

    assert leitura.status == "ok"
    assert len(leitura.observations) == 4

    completa = next(
        observation
        for observation in leitura.observations
        if observation.arfcn == 525
    )

    assert completa.pci == 122
    assert completa.rsrq_db == -8.0
    assert completa.radio_key == "LTE:525:122"
    assert completa.global_cell_key == "LTE:72499:F55FB0C"

    pci_254 = next(
        observation
        for observation in leitura.observations
        if observation.arfcn == 3375
        and observation.pci == 254
    )

    assert pci_254.rsrp_dbm == -111.0
    assert pci_254.rsrq_db == -12.0
    assert pci_254.radio_key == "LTE:3375:254"

    pci_253 = next(
        observation
        for observation in leitura.observations
        if observation.arfcn == 3375
        and observation.pci == 253
    )

    assert pci_253.rsrp_dbm == -126.0
    assert pci_253.rsrq_db == -20.0
    assert pci_253.radio_key == "LTE:3375:253"

    pci_252 = next(
        observation
        for observation in leitura.observations
        if observation.arfcn == 1725
        and observation.pci == 252
    )

    assert pci_252.rsrq_db == -20.0
    assert pci_252.radio_key == "LTE:1725:252"

    assert not [
        anomaly
        for anomaly in leitura.anomalies
        if "registro_incompleto:rsrq" in anomaly
        or "registro_incompleto:phyCellId" in anomaly
    ]

def test_csurv_registros_colados_nao_inventam_plmn():
    c = csurv("csurv_registros_colados.txt")
    assert [o.radio_key for o in c.observations] == [None, "LTE:3375:254"]
    assert c.observations[0].plmn is None  # 'nr_mnc: 0' truncado = não coletado


def test_csurv_zero_e_valor_nao_coletado():
    o = next(o for o in csurv("csurv_valor_nao_coletado.txt").observations if o.pci == 254)
    assert (o.rsrp_dbm, o.rsrq_db, o.rx_level_dbm, o.band, o.radio_key) == (None, None, None, 28, "LTE:3375:254")


def test_csurv_pci_zero_e_arfcn_zero_sao_validos():
    r = AtResponse.from_raw("AT#CSURV", ["earfcn: 0 rxLev: -80 phyCellId: 0 cellStatus: CELL_SUITABLE rsrp: -100 rsrq: -10", "OK"])
    assert parse_csurv(r).observations[0].radio_key == "LTE:0:0"


def test_csurv_vazio_e_abortado():
    assert csurv("csurv_vazio.txt").status == "empty"
    abortado = csurv("csurv_abortado.txt")
    assert abortado.status == "error" and abortado.response.error_code == 660


# --------------------------------------------------------------------------- GNSS
def test_gpsacp_preserva_tipo_de_fix_altitude_e_utc():
    g = parse_gpsacp(resposta("AT$GPSACP", "gpsacp_fix.txt")).fix
    assert (g.fix_type, g.alt_m, g.utc) == (3, 73.1, "2019-08-29T00:25:18.000Z")


@pytest.mark.parametrize("valor,esperado", [
    ("4546.6334N", 45 + 46.6334 / 60),
    ("00805.5885W", -(8 + 5.5885 / 60)),
    ("3731.303660S", -(37 + 31.30366 / 60)),
])
def test_nmea_valido(valor, esperado):
    assert math.isclose(nmea_to_decimal(valor), esperado)


@pytest.mark.parametrize("valor", ["", "3731N", "3760.0N", "9100.0N", "18100.0E", "abc", "3731.30X"])
def test_nmea_invalido_vira_none(valor):
    assert nmea_to_decimal(valor) is None


def test_gpsacp_com_hemisferio_trocado_e_malformado():
    r = AtResponse.from_raw("AT$GPSACP", ["$GPSACP: 002518.000,3731.303660E,12655.781816E,0.8,73.1,3,0.0,0.0,0.0,290819,18", "OK"])
    assert parse_gpsacp(r).status == "malformed"


# --------------------------------------------------------------------------- célula servidora
def test_servidora_cruzada_usa_as_duas_chaves_do_mesmo_registro():
    r = resolve_serving_cell(
        parse_moni(resposta("AT#MONI", "moni_lte.txt")),
        parse_servinfo(resposta("AT#SERVINFO", "servinfo_lte.txt")),
        csurv("csurv_vivo_campo.txt").observations,
    )
    assert (r.method, r.radio_key, r.radio_key_source, r.global_cell_key, r.global_key_source, r.csurv_index) == (
        "csurv", "LTE:3375:254", "CSURV", "LTE:72406:F560E67", "CSURV", 1
    )


def test_servidora_sem_registro_no_csurv_nao_adivinha_pci():
    r = resolve_serving_cell(
        parse_moni(resposta("AT#MONI", "moni_lte.txt")),
        parse_servinfo(resposta("AT#SERVINFO", "servinfo_lte.txt")),
        csurv("csurv_manual_exemplo.txt").observations,
    )
    assert (r.method, r.radio_key, r.pci) == ("no_match", None, None)
    assert (r.global_cell_key, r.global_key_source) == ("LTE:72406:F560E67", "SERVINFO")


def _lte_de_campo():
    return next(o for o in csurv("csurv_vivo_campo.txt").observations if o.rat == "LTE")


def _servidora(*candidatos):
    return resolve_serving_cell(parse_moni(resposta("AT#MONI", "moni_lte.txt")),
                                parse_servinfo(resposta("AT#SERVINFO", "servinfo_lte.txt")), list(candidatos))


def test_servidora_com_um_unico_candidato():
    r = _servidora(_lte_de_campo())
    assert (r.method, r.radio_key, r.global_cell_key, r.csurv_index) == ("csurv", "LTE:3375:254", "LTE:72406:F560E67", 0)
    assert not [a for a in r.anomalies if "registro" in a]


def test_copias_identicas_da_servidora_nao_tornam_a_escolha_ambigua():
    lte = _lte_de_campo()
    for copia in (lte, dataclasses.replace(lte, rsrp_dbm=-110.0)):  # mesma identidade; só a medida pode mudar
        r = _servidora(lte, copia)
        assert (r.method, r.radio_key, r.global_cell_key, r.csurv_index) == (
            "csurv", "LTE:3375:254", "LTE:72406:F560E67", 0)
        assert "registro_servidora_repetido_csurv:2" in r.anomalies


def test_copia_com_campo_ausente_nao_conflita_e_vale_a_mais_completa():
    lte = _lte_de_campo()
    r = _servidora(dataclasses.replace(lte, tac=None), lte)
    assert (r.method, r.radio_key, r.csurv_index) == ("csurv", "LTE:3375:254", 1)


def test_mesmo_cell_id_com_radio_keys_diferentes_e_ambiguo():
    lte = _lte_de_campo()
    r = _servidora(lte, dataclasses.replace(lte, pci=31))
    assert (r.method, r.radio_key, r.pci, r.csurv_index) == ("ambiguous", None, None, None)
    assert "varios_registros_csurv:2:pci" in r.anomalies
    assert (r.global_cell_key, r.global_key_source) == ("LTE:72406:F560E67", "SERVINFO")


def test_candidatos_com_informacao_conflitante_sao_ambiguos():
    lte = _lte_de_campo()
    r = _servidora(lte, dataclasses.replace(lte, tac="ABCD"))
    assert (r.method, r.radio_key) == ("ambiguous", None) and "varios_registros_csurv:2:tac" in r.anomalies


def test_nr_sa_com_registro_repetido_usa_a_chave_global_do_csurv():
    nr = CellObservation(rat="NR", source="CSURV", arfcn=629952, pci=123, plmn="72406", cell_id="3053A189", tac="1")
    r = resolve_serving_cell(parse_moni(resposta("AT#MONI", "moni_nr_sa.txt")), None, [nr, nr])
    assert (r.radio_key, r.global_cell_key, r.csurv_index) == ("NR:629952:123", "NR:72406:3053A189", 0)
    assert "registro_servidora_repetido_csurv:2" in r.anomalies


def test_servidora_que_mudou_entre_comandos_ignora_servinfo():
    outro = AtResponse.from_raw("AT#SERVINFO", ['#SERVINFO: 3375,-76,"VIVO","72406",ABC123,87BB,640,3,-107', "OK"])
    r = resolve_serving_cell(
        parse_moni(resposta("AT#MONI", "moni_lte.txt")), parse_servinfo(outro), csurv("csurv_vivo_campo.txt").observations
    )
    assert "servidora_mudou_entre_moni_e_servinfo" in r.anomalies and r.radio_key == "LTE:3375:254"


def test_nr_sa_usa_radio_key_do_moni():
    r = resolve_serving_cell(parse_moni(resposta("AT#MONI", "moni_nr_sa.txt")), None, [])
    assert (r.method, r.rat, r.radio_key, r.radio_key_source, r.global_cell_key) == ("moni_nr", "NR", "NR:629952:123", "MONI", None)


def test_nsa_registra_perna_nr():
    r = resolve_serving_cell(
        parse_moni(resposta("AT#MONI", "moni_endc_nr_conectado.txt")), None, csurv("csurv_vivo_campo.txt").observations
    )
    assert (r.radio_key, r.nr_leg_radio_key) == ("LTE:3375:254", "NR:629952:123")


def test_sem_leituras_validas_nao_ha_servidora():
    r = resolve_serving_cell(parse_moni(resposta("AT#MONI", "error.txt")), None, [])
    assert (r.method, r.radio_key, r.global_cell_key) == ("none", None, None)


# --------------------------------------------------------------------------- CLI
def test_cli_imprime_leitura_em_json_com_chaves(monkeypatch, capsys, sem_espera):
    import hal.cli as cli

    driver = driver_com({"AT#CSURV": "csurv_vivo_campo.txt"}, sem_espera)
    driver.connect = lambda *args, **kwargs: None
    monkeypatch.setattr(cli, "FN990Driver", lambda: driver)
    monkeypatch.setattr(sys, "argv", ["hal.cli", "--port", "PORTA", "csurv"])
    cli.main()
    saida = json.loads(capsys.readouterr().out)
    assert saida["status"] == "ok" and saida["observations"][1]["radio_key"] == "LTE:3375:254"
