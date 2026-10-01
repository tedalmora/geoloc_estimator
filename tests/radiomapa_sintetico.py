"""Radiomapas v2 SINTÉTICOS para os testes do Estimador. Não são dados nem resultados experimentais.

Modelo determinístico simples: células em posições fixas (metros a leste/norte de ORIGEM),
RSRP = -40 - 35·log10(d) + variação reprodutível de ±2 dB, pontos de referência numa grade de
60 m. Cada sessão fica parada num ponto (como no Coletor). Inclui, para os testes de política:
PCI repetido em portadoras diferentes, registro curto sem PLMN, outra operadora,
CELL_FORBIDDEN, célula NR e uma célula local visível só no ponto P6.
"""
import json
import math
import random

import coletor.db as cdb
from hal.models import CellObservation

ORIGEM = (-26.9000, -49.0700)
CAMPAIGN_PLMN = "72406"
LOCAL_KEY = "LTE:3375:400"

# (rat, arfcn, pci, plmn, cell_id decimal, status, leste_m, norte_m, alcance_m)
CELULAS = [
    ("LTE", 3375, 254, "72406", 257298023, "CELL_SUITABLE", 0, 300, None),
    ("LTE", 3375, 31, "72406", 257298024, "CELL_SUITABLE", 300, 0, None),
    ("LTE", 1350, 64, "72406", 257298030, "CELL_SUITABLE", -300, 0, None),
    ("LTE", 2850, 64, "72406", 257298031, "CELL_SUITABLE", 0, -300, None),
    ("LTE", 3375, 99, None, None, "CELL_SUITABLE", 200, 200, None),  # registro curto: PLMN não confirmado
    ("LTE", 9410, 21, "72405", 51998470, "CELL_SUITABLE", -200, -200, None),  # outra operadora
    ("LTE", 100, 87, "72406", 257298040, "CELL_FORBIDDEN", -200, 200, None),
    ("NR", 629952, 123, "72406", 810784393, "CELL_SUITABLE", 100, 100, None),
    ("LTE", 3375, 400, "72406", 257298050, "CELL_SUITABLE", 60, 60, 30),  # célula local, só em P6
]
# nome: (leste_m, norte_m, coordenada cadastrada?)
PONTOS = {
    "P1": (-60, -60, True), "P2": (0, -60, True), "P3": (60, -60, True),
    "P4": (-60, 60, True), "P5": (0, 60, False), "P6": (60, 60, True),
}


def para_latlon(leste_m, norte_m):
    lat0, lon0 = ORIGEM
    return lat0 + norte_m / 111320.0, lon0 + leste_m / (111320.0 * math.cos(math.radians(lat0)))


def observacao(celula, rsrp, source="CSURV"):
    rat, arfcn, pci, plmn, cell_id, status, *_ = celula
    return CellObservation(
        rat=rat, source=source, arfcn=arfcn, pci=pci, plmn=plmn,
        cell_id=None if cell_id is None else format(cell_id, "X"), rsrp_dbm=rsrp, cell_status=status,
        record_format="short" if cell_id is None else "full",
    )


def celulas_vistas(leste_m, norte_m, semente):
    rng = random.Random(semente)
    vistas = []
    for celula in CELULAS:
        *_, e, n, alcance = celula
        d = max(math.hypot(leste_m - e, norte_m - n), 10.0)
        if alcance is not None and d > alcance:
            continue
        vistas.append(observacao(celula, float(round(-40 - 35 * math.log10(d) + rng.uniform(-2, 2)))))
    return vistas


def inserir_medicao(conn, session_id, seq, point_id, started_at, observacoes, serving_key=None, gnss=None,
                    legacy=0, snapshot_status="complete", csurv_status="ok"):
    lat, lon = gnss if gnss else (None, None)
    cur = conn.execute(
        "INSERT INTO measurements (session_id, reference_point_id, seq, started_at, snapshot_status, reasons_json, "
        "access_mode, serving_global_cell_key, moni_status, servinfo_status, csurv_status, gnss_status, gnss_lat, "
        "gnss_lon, gnss_fix_type, gnss_hdop, gnss_nsat, timings_json, anomalies_json, raw_json, legacy_ambiguous) "
        "VALUES (?, ?, ?, ?, ?, '[]', 'LTE', ?, 'ok', 'ok', ?, ?, ?, ?, ?, ?, ?, '{}', '[]', '{}', ?)",
        (session_id, point_id, seq, started_at, snapshot_status, serving_key, csurv_status,
         "ok" if gnss else "no_fix", lat, lon, 3 if gnss else None, 0.9 if gnss else None, 9 if gnss else None, legacy),
    )
    for o in observacoes:
        conn.execute(
            "INSERT INTO observed_cells (measurement_id, source, rat, arfcn, pci, plmn, cell_id, rsrp_dbm, cell_status, "
            "record_format, radio_key, global_cell_key, identity_status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (cur.lastrowid, o.source, o.rat, o.arfcn, o.pci, o.plmn, o.cell_id, o.rsrp_dbm, o.cell_status,
             o.record_format, o.radio_key, o.global_cell_key, o.identity_status),
        )
    return cur.lastrowid


def criar_radiomapa(caminho, dias=3, pontos=None, por_sessao=3, extras=True, campaign_plmn=CAMPAIGN_PLMN):
    """Cria o banco v2 sintético. Devolve {nome do ponto: (lat, lon)} com a posição verdadeira."""
    pontos = PONTOS if pontos is None else pontos
    conn = cdb.connect(str(caminho))
    verdade = {}
    ids = {}
    for nome, (e, n, cadastrado) in pontos.items():
        verdade[nome] = para_latlon(e, n)
        if cadastrado:
            ids[nome] = cdb.add_reference_point(conn, nome, *verdade[nome], coord_source="sintetico", coord_uncertainty_m=0.5)
        else:
            ids[nome] = cdb.get_or_create_reference_point(conn, nome)[0]
    with conn:
        for dia in range(1, dias + 1):
            for ordem, (nome, (e, n, _)) in enumerate(pontos.items()):
                sid = cdb.create_session(conn, mode="continuous", scenario="estatico", environment="outdoor",
                                         modem_id="sintetico", metadata={"sintetico": True}, reference_point_id=ids[nome],
                                         campaign_plmn=campaign_plmn)
                conn.execute("UPDATE sessions SET started_at = ? WHERE session_id = ?",
                             (f"2026-09-{dia:02d}T{10 + ordem:02d}:00:00.000Z", sid))
                for seq in range(1, por_sessao + 1):
                    obs = celulas_vistas(e, n, f"{dia}-{nome}-{seq}")
                    lte_campanha = [o for o in obs if o.rat == "LTE" and o.plmn == CAMPAIGN_PLMN and o.cell_status == "CELL_SUITABLE"]
                    servidora = max(lte_campanha, key=lambda o: o.rsrp_dbm)
                    gnss = para_latlon(e + random.Random(f"g{dia}{nome}{seq}").uniform(-4, 4),
                                       n + random.Random(f"h{dia}{nome}{seq}").uniform(-4, 4))
                    inserir_medicao(conn, sid, seq, ids[nome], f"2026-09-{dia:02d}T{10 + ordem:02d}:00:{seq:02d}.000Z",
                                    obs, servidora.global_cell_key, gnss)
        if extras:
            _extras(conn, ids)
    conn.close()
    return verdade


def _extras(conn, ids):
    """Linhas que o dataset precisa excluir ou tratar à parte."""
    primeiro = next(iter(ids))
    sid = cdb.create_session(conn, mode="continuous", scenario="pedestre", environment="outdoor", modem_id="sintetico",
                             metadata={"sintetico": True}, route="trilha", campaign_plmn=CAMPAIGN_PLMN)
    conn.execute("UPDATE sessions SET started_at = '2026-08-30T09:00:00.000Z' WHERE session_id = ?", (sid,))
    for seq in range(1, 4):  # rota: só GNSS isolado como referência
        inserir_medicao(conn, sid, seq, None, f"2026-08-30T09:00:{seq:02d}.000Z", celulas_vistas(30 * seq, 0, f"r{seq}"),
                        gnss=para_latlon(30 * seq, 0))
    sid = cdb.create_session(conn, mode="continuous", scenario="estatico", environment="outdoor", modem_id="sintetico",
                             metadata={"sintetico": True}, reference_point_id=ids[primeiro], campaign_plmn=CAMPAIGN_PLMN)
    conn.execute("UPDATE sessions SET started_at = '2026-08-29T09:00:00.000Z' WHERE session_id = ?", (sid,))
    celulas = celulas_vistas(0, 0, "x")
    inserir_medicao(conn, sid, 1, ids[primeiro], "2026-08-29T09:00:01.000Z", celulas, legacy=1)
    inserir_medicao(conn, sid, 2, ids[primeiro], "2026-08-29T09:00:02.000Z", celulas, snapshot_status="invalid")
    inserir_medicao(conn, sid, 3, ids[primeiro], "2026-08-29T09:00:03.000Z", [], csurv_status="empty",
                    snapshot_status="partial")


def info_json(caminho):
    """Conteúdo do banco em JSON (para comparar antes/depois sem depender do arquivo)."""
    conn = cdb.open_read_only(str(caminho))
    try:
        return json.dumps([conn.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall() for t in cdb.TABLES])
    finally:
        conn.close()
