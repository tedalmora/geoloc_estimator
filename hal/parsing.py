"""Parsers puros das respostas AT do FN990.

Não abrem porta serial: recebem um AtResponse (do driver, de uma fixture ou de uma resposta crua
guardada no banco) e devolvem leituras tipadas. Assim são testáveis sem hardware e permitem
reprocessar coletas antigas quando um parser mudar.

Formatos conferidos no manual 80691ST11097A Rev.5 (2025-10-20): #MONI §4.2.31 (pp. 306-314),
#SERVINFO §4.2.16 (pp. 249-251), #CSURV §4.14.1 (pp. 814-820), $GPSACP §4.11.5.3 (pp. 783-786) e
$GPSP §4.11.2.3 (pp. 763-764). Onde o campo contradiz o manual, vale o campo, e a divergência
fica anotada junto do parser.
"""
import csv
import re
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .models import (
    ACCESS_LTE,
    ACCESS_NR_NSA,
    ACCESS_NR_SA,
    GNSS_NO_FIX,
    GNSS_OFF,
    RAT_LTE,
    RAT_NR,
    READ_EMPTY,
    READ_ERROR,
    READ_MALFORMED,
    READ_OK,
    READ_TIMEOUT,
    READ_UNSUPPORTED,
    SOURCE_CSURV,
    SOURCE_MONI,
    SOURCE_SERVINFO,
    AtResponse,
    CellObservation,
    CsurvReading,
    GnssFix,
    GnssReading,
    MoniReading,
    ServingCellResolution,
    ServInfoReading,
    canonical_hex,
)

_KEY_TOKEN = re.compile(r"^([A-Za-z_]+):(.*)$")
_INT_RE = re.compile(r"[+-]?\d+")
_FLOAT_RE = re.compile(r"[+-]?\d+(?:\.\d+)?")
_MCC_RE = re.compile(r"\d{3}")
_MNC_RE = re.compile(r"\d{2,3}")
_PLMN_RE = re.compile(r"\d{5,6}")
_COORD_RE = re.compile(r"(\d{2,3})(\d{2}\.\d+)([NSEW])")
_GPSP_RE = re.compile(r"\$GPSP:\s*(\d+)")
_CSURV_RECORD_START = re.compile(r"(?<![A-Za-z_])(?=(?:nr_arfcn|earfcn|uarfcn|arfcn):)")
_CSURV_HINT = re.compile(r"(?:earfcn|nr_arfcn|uarfcn|cellStatus|phyCellId)\s*:")

_LTE_FULL_KEYS = ("earfcn", "rxLev", "mcc", "mnc", "cellId", "tac", "phyCellId", "cellStatus", "rsrp", "rsrq")
_LTE_SHORT_KEYS = ("earfcn", "rxLev", "phyCellId", "cellStatus", "rsrp", "rsrq")
_NR_KEYS = ("nr_arfcn", "nr_band", "nr_scs", "nr_mcc", "nr_mnc", "cell_id", "tac", "pci", "cell_status", "rsrp", "rsrq")


# ---------------------------------------------------------------- valores
def parse_int(text) -> Optional[int]:
    if text is None:
        return None
    text = str(text).strip()
    return int(text) if _INT_RE.fullmatch(text) else None


def parse_float(text) -> Optional[float]:
    if text is None:
        return None
    text = str(text).strip()
    return float(text) if _FLOAT_RE.fullmatch(text) else None


def parse_dbm(text) -> Optional[float]:
    """'-76dbm' -> -76.0 (campo PWR do #MONI)."""
    if text is None:
        return None
    text = str(text).strip()
    if text.lower().endswith("dbm"):
        text = text[:-3]
    return parse_float(text)


def decimal_to_hex(text) -> Optional[str]:
    """Identificador decimal (cellId/tac do #CSURV) -> hexadecimal canônico (257298023 -> 'F560E67')."""
    value = parse_int(text)
    return canonical_hex(value) if value is not None and value >= 0 else None


def make_plmn(mcc, mnc) -> Optional[str]:
    """MCC (3 dígitos) + MNC (2 ou 3 dígitos, preservando zero à esquerda: '06')."""
    if mcc is None or mnc is None:
        return None
    mcc, mnc = str(mcc).strip(), str(mnc).strip()
    if not (_MCC_RE.fullmatch(mcc) and _MNC_RE.fullmatch(mnc)):
        return None
    return mcc + mnc


def nmea_to_decimal(value) -> Optional[float]:
    """Coordenada do $GPSACP ('3731.303660N', '12655.781816E') -> grau decimal; None se malformada.

    Formato: graus (2 dígitos na latitude, 3 na longitude) + minutos com 2 dígitos inteiros e
    parte decimal, seguidos do hemisfério. Minutos >= 60 ou graus fora da faixa são rejeitados.
    """
    if not value:
        return None
    match = _COORD_RE.fullmatch(str(value).strip().upper())
    if match is None:
        return None
    degrees, minutes, hemisphere = int(match.group(1)), float(match.group(2)), match.group(3)
    decimal = degrees + minutes / 60.0
    if minutes >= 60.0 or decimal > (90.0 if hemisphere in "NS" else 180.0):
        return None
    return -decimal if hemisphere in "SW" else decimal


def gnss_utc(hhmmss, ddmmyy) -> Optional[str]:
    """Hora e data do $GPSACP ('002518.000', '290819') -> '2019-08-29T00:25:18.000Z'."""
    time_match = re.fullmatch(r"(\d{2})(\d{2})(\d{2})(\.\d+)?", (hhmmss or "").strip())
    date_match = re.fullmatch(r"(\d{2})(\d{2})(\d{2})", (ddmmyy or "").strip())
    if not (time_match and date_match):
        return None
    day, month, year = (int(g) for g in date_match.groups())
    hour, minute, second = (int(time_match.group(i)) for i in (1, 2, 3))
    try:
        moment = datetime(2000 + year, month, day, hour, minute, second, tzinfo=timezone.utc)
    except ValueError:
        return None
    fraction = (time_match.group(4) or "")[:4]
    return moment.strftime("%Y-%m-%dT%H:%M:%S") + fraction + "Z"


# ---------------------------------------------------------------- utilitários internos
def _failure(resp: AtResponse) -> Optional[str]:
    if resp.timed_out:
        return READ_TIMEOUT
    if not resp.ok:
        return READ_ERROR
    return None


def _split_fields(tokens: Sequence[str]) -> Tuple[List[str], Dict[str, str]]:
    """Separa 'chave:valor' (com ou sem espaço depois dos dois-pontos) dos tokens soltos.

    Devolve (tokens soltos antes da primeira chave, campos). Uma chave seguida de outra chave
    fica com valor vazio (ex.: 'NR_TXPWR: NR_DLMOD:3'). Chave repetida: vale a primeira.
    """
    loose: List[str] = []
    fields: Dict[str, str] = {}
    i = 0
    while i < len(tokens):
        match = _KEY_TOKEN.match(tokens[i])
        if match is None:
            if not fields:
                loose.append(tokens[i])
            i += 1
            continue
        key, value = match.groups()
        if value == "" and i + 1 < len(tokens) and not _KEY_TOKEN.match(tokens[i + 1]):
            value = tokens[i + 1]
            i += 1
        fields.setdefault(key, value)
        i += 1
    return loose, fields


class _Fields:
    """Campos de 1 registro, com conversão que anota valores presentes porém inválidos."""

    def __init__(self, fields: Dict[str, str], anomalies: List[str], label: str):
        self.fields, self.anomalies, self.label = fields, anomalies, label

    def get(self, key: str, convert: Callable, zero_is_missing: bool = False):
        raw = self.fields.get(key)
        if raw is None or raw == "":
            return None
        value = convert(raw)
        if value is None:
            self.anomalies.append(f"{self.label}:valor_invalido:{key}={raw}")
            return None
        if zero_is_missing and value in (0, "0"):
            return None
        return value

    def plmn(self, mcc_key: str, mnc_key: str) -> Optional[str]:
        mcc, mnc = self.fields.get(mcc_key), self.fields.get(mnc_key)
        plmn = make_plmn(mcc, mnc)
        if plmn is None and mcc not in (None, "", "0") and mnc not in (None, "", "0"):
            self.anomalies.append(f"{self.label}:valor_invalido:plmn={mcc}/{mnc}")
        return plmn


# ---------------------------------------------------------------- #MONI
def parse_moni(resp: AtResponse) -> MoniReading:
    """#MONI da célula servidora (formatos a/b do manual, pp. 308-309).

    Modo de acesso: LTE + campos NR_ com NR_STATE = 2 (connected) -> NR_NSA; LTE sem perna NR
    conectada -> LTE; só campos NR_ -> NR_SA. A perna NR do EN-DC só vira observação quando
    conectada. O formato de célula adjacente (sobra de um AT#MONI=1..6) é rejeitado: nele o
    campo Id é o PCI, não o Cell-ID. O driver não reconfigura o #MONI sozinho.
    """
    failure = _failure(resp)
    if failure:
        return MoniReading(status=failure, response=resp)
    anomalies: List[str] = []
    moni_lines = [line for line in resp.lines if line.startswith("#MONI:")]
    other_lines = [line for line in resp.lines if not line.startswith("#MONI:")]
    if other_lines:
        anomalies.append("contaminacao_csurv" if any(_CSURV_HINT.search(l) for l in other_lines) else "linhas_inesperadas")
    if not moni_lines:
        return MoniReading(status=READ_MALFORMED if other_lines else READ_EMPTY, anomalies=anomalies, response=resp)
    if len(moni_lines) > 1:
        anomalies.append("varias_linhas_moni")

    loose, raw_fields = _split_fields(moni_lines[0][len("#MONI:"):].split())
    f = _Fields(raw_fields, anomalies, "MONI")
    has_lte = "EARFCN" in raw_fields or "RSRP" in raw_fields
    has_nr = any(key.startswith("NR_") for key in raw_fields)
    if has_lte and "TAC" not in raw_fields:
        anomalies.append("formato_celula_adjacente")
        return MoniReading(status=READ_MALFORMED, anomalies=anomalies, response=resp)
    if not (has_lte or has_nr):
        anomalies.append("sem_campos_de_celula")
        return MoniReading(status=READ_MALFORMED, anomalies=anomalies, response=resp)

    plmn = make_plmn(raw_fields.get("Cc"), raw_fields.get("Nc"))  # só no formato sem nome de rede
    nr_state = f.get("NR_STATE", parse_int)
    lte = nr = None
    if has_lte:
        # Id e TAC em hexadecimal. O manual tipa <tac> como integer, mas em campo veio 'TAC:87BB',
        # que bate com 'tac: 34747' (decimal) do #CSURV na mesma célula.
        lte = CellObservation(
            rat=RAT_LTE, source=SOURCE_MONI, arfcn=f.get("EARFCN", parse_int), plmn=plmn,
            cell_id=f.get("Id", canonical_hex), tac=f.get("TAC", canonical_hex),
            rsrp_dbm=f.get("RSRP", parse_float), rsrq_db=f.get("RSRQ", parse_float),
            rx_level_dbm=f.get("PWR", parse_dbm), is_serving=True,
        )
    if has_nr:
        sinr_x10 = f.get("NR_SINR", parse_int)
        nr = CellObservation(
            rat=RAT_NR, source=SOURCE_MONI, arfcn=f.get("NR_CH", parse_int), pci=f.get("NR_PCI", parse_int),
            plmn=None if has_lte else plmn, rsrp_dbm=f.get("NR_RSRP", parse_float),
            rsrq_db=f.get("NR_RSRQ", parse_float),
            sinr_db=None if sinr_x10 is None else sinr_x10 / 10.0,  # NR_SINR vem em 10 x dB (p. 311)
            rx_level_dbm=f.get("NR_PWR", parse_dbm), band=f.get("NR_BAND", parse_int),
            bandwidth=f.get("NR_BW", parse_int), is_serving=True,
        )

    if lte is not None and nr is not None:
        access = ACCESS_NR_NSA if nr_state == 2 else ACCESS_LTE
    else:
        access = ACCESS_LTE if lte is not None else ACCESS_NR_SA
    observations = [lte] if lte is not None else []
    if nr is not None and access in (ACCESS_NR_NSA, ACCESS_NR_SA):
        observations.append(nr)
    return MoniReading(
        status=READ_OK, access_mode=access, observations=observations, network_name=" ".join(loose) or None,
        nr_state=nr_state, anomalies=anomalies, response=resp,
    )


# ---------------------------------------------------------------- #SERVINFO
_SERVINFO_LTE = ("EARFCN", "dBM", "NetNameAsc", "NetCode", "CID", "TAC", "DRX", "SD", "RSRP",
                 "NR_CH", "NR_ULCH", "NR_RSSI", "NR_RSRP", "NR_RSRQ")
_SERVINFO_SA = ("NR_CH", "NR_ULCH", "NR_RSSI", "NR_RSRP", "NR_RSRQ")


def parse_servinfo(resp: AtResponse) -> ServInfoReading:
    """#SERVINFO: 9 campos (LTE), 14 (LTE com EN-DC) ou 5 (NR SA). Outros formatos (WCDMA,
    11 campos) ficam como 'unsupported'. Serve para obter o PLMN (NetCode) da servidora.

    CID e TAC em hexadecimal: o manual tipa como integer, mas em campo veio
    '#SERVINFO: 3375,-76,"VIVO","72406",F560E67,87BB,640,3,-107'. O EN-DC no #SERVINFO não diz
    se a perna NR está conectada; quem decide o modo de acesso é o NR_STATE do #MONI.
    """
    failure = _failure(resp)
    if failure:
        return ServInfoReading(status=failure, response=resp)
    lines = [line for line in resp.lines if line.startswith("#SERVINFO:")]
    if not lines:
        return ServInfoReading(status=READ_MALFORMED if resp.lines else READ_EMPTY, response=resp)
    anomalies: List[str] = []
    values = [v.strip() for v in next(csv.reader([lines[0][len("#SERVINFO:"):].strip()]))]

    if len(values) in (9, 14):
        f = _Fields(dict(zip(_SERVINFO_LTE, values)), anomalies, "SERVINFO")
        netcode = values[3]
        plmn = netcode if _PLMN_RE.fullmatch(netcode) else None
        if netcode and plmn is None:
            anomalies.append(f"SERVINFO:valor_invalido:NetCode={netcode}")
        observations = [CellObservation(
            rat=RAT_LTE, source=SOURCE_SERVINFO, arfcn=f.get("EARFCN", parse_int), plmn=plmn,
            cell_id=f.get("CID", canonical_hex), tac=f.get("TAC", canonical_hex),
            rsrp_dbm=f.get("RSRP", parse_float), rx_level_dbm=f.get("dBM", parse_float), is_serving=True,
        )]
        if len(values) == 14:
            observations.append(CellObservation(
                rat=RAT_NR, source=SOURCE_SERVINFO, arfcn=f.get("NR_CH", parse_int),
                rsrp_dbm=f.get("NR_RSRP", parse_float), rsrq_db=f.get("NR_RSRQ", parse_float),
                rx_level_dbm=f.get("NR_RSSI", parse_float), is_serving=True,
            ))
        return ServInfoReading(
            status=READ_OK, access_mode=ACCESS_LTE, observations=observations, network_name=values[2] or None,
            endc=len(values) == 14, anomalies=anomalies, response=resp,
        )
    if len(values) == 5:
        f = _Fields(dict(zip(_SERVINFO_SA, values)), anomalies, "SERVINFO")
        nr = CellObservation(
            rat=RAT_NR, source=SOURCE_SERVINFO, arfcn=f.get("NR_CH", parse_int),
            rsrp_dbm=f.get("NR_RSRP", parse_float), rsrq_db=f.get("NR_RSRQ", parse_float),
            rx_level_dbm=f.get("NR_RSSI", parse_float), is_serving=True,
        )
        return ServInfoReading(status=READ_OK, access_mode=ACCESS_NR_SA, observations=[nr], anomalies=anomalies, response=resp)
    return ServInfoReading(
        status=READ_UNSUPPORTED, anomalies=[f"SERVINFO:formato_desconhecido:{len(values)}_campos"], response=resp
    )


# ---------------------------------------------------------------- #CSURV
def parse_csurv(resp: AtResponse) -> CsurvReading:
    """#CSURV: 1 observação por registro LTE ou NR (registros 2G/3G são só contados).

    O texto é unido e dividido pelo início de cada registro, não por linha: o manual quebra
    registros longos em 80 colunas (p. 819), e em campo registros vieram colados sem separador
    ('...nr_mnc: 0earfcn: 3375 ...'). O valor '0' significa "não coletado na varredura" (p. 819)
    para medidas, PLMN, Cell-ID e TAC. PCI 0 e ARFCN 0 continuam válidos (são valores possíveis).
    Nenhuma célula é descartada aqui (outras operadoras e CELL_FORBIDDEN incluídas): a escolha do
    que entra nas features é do Estimador.
    """
    failure = _failure(resp)
    if failure:
        return CsurvReading(status=failure, response=resp)
    anomalies: List[str] = []
    observations: List[CellObservation] = []
    ignored = 0
    for chunk in _CSURV_RECORD_START.split(" ".join(resp.lines)):
        _, fields = _split_fields(chunk.split())
        if "nr_arfcn" in fields:
            observations.append(_csurv_nr(fields, anomalies))
        elif "earfcn" in fields:
            observations.append(_csurv_lte(fields, anomalies))
        elif "uarfcn" in fields or "arfcn" in fields:
            ignored += 1
    keys = [o.radio_key for o in observations if o.radio_key]
    anomalies.extend(f"CSURV:radio_key_repetida:{k}" for k in sorted({k for k in keys if keys.count(k) > 1}))
    if ignored:
        anomalies.append(f"CSURV:registros_2g_3g_ignorados:{ignored}")
    return CsurvReading(
        status=READ_OK if observations else READ_EMPTY, observations=observations, ignored_records=ignored,
        anomalies=anomalies, response=resp,
    )


def _missing(fields: Dict[str, str], expected: Sequence[str], label: str, anomalies: List[str]) -> None:
    missing = [key for key in expected if key not in fields]
    if missing:
        anomalies.append(f"{label}:registro_incompleto:{','.join(missing)}")


def _csurv_lte(fields: Dict[str, str], anomalies: List[str]) -> CellObservation:
    # Registro completo ("For serving cell" no manual, p. 816): célula cuja informação de sistema
    # foi decodificada na varredura; não é necessariamente a servidora do UE.
    full = "cellId" in fields or "mcc" in fields
    label = f"CSURV:LTE:{fields.get('earfcn')}"
    _missing(fields, _LTE_FULL_KEYS if full else _LTE_SHORT_KEYS, label, anomalies)
    f = _Fields(fields, anomalies, label)
    return CellObservation(
        rat=RAT_LTE, source=SOURCE_CSURV, arfcn=f.get("earfcn", parse_int), pci=f.get("phyCellId", parse_int),
        plmn=f.plmn("mcc", "mnc"), cell_id=f.get("cellId", decimal_to_hex, zero_is_missing=True),
        tac=f.get("tac", decimal_to_hex, zero_is_missing=True), rsrp_dbm=f.get("rsrp", parse_float, True),
        rsrq_db=f.get("rsrq", parse_float, True), rx_level_dbm=f.get("rxLev", parse_float, True),
        cell_status=fields.get("cellStatus") or None, band=f.get("band", parse_int, True),
        bandwidth=f.get("bw", parse_int, True), record_format="full" if full else "short",
    )


def _csurv_nr(fields: Dict[str, str], anomalies: List[str]) -> CellObservation:
    label = f"CSURV:NR:{fields.get('nr_arfcn')}"
    _missing(fields, _NR_KEYS, label, anomalies)
    f = _Fields(fields, anomalies, label)
    return CellObservation(
        rat=RAT_NR, source=SOURCE_CSURV, arfcn=f.get("nr_arfcn", parse_int), pci=f.get("pci", parse_int),
        plmn=f.plmn("nr_mcc", "nr_mnc"), cell_id=f.get("cell_id", decimal_to_hex, zero_is_missing=True),
        tac=f.get("tac", decimal_to_hex, zero_is_missing=True), rsrp_dbm=f.get("rsrp", parse_float, True),
        rsrq_db=f.get("rsrq", parse_float, True), cell_status=fields.get("cell_status") or None,
        band=f.get("nr_band", parse_int, True), bandwidth=f.get("bw", parse_int, True),
        scs_khz=f.get("nr_scs", parse_int, True), record_format="full",
    )


# ---------------------------------------------------------------- GNSS
def parse_gpsacp(resp: AtResponse) -> GnssReading:
    """$GPSACP: <UTC>,<lat>,<lon>,<hdop>,<alt>,<fix>,<cog>,<spkm>,<spkn>,<date>,<nsat>.

    fix 0 ou 1 = fix inválido (sem posição, mesmo que venham coordenadas); 2 = 2D; 3 = 3D.
    Resposta vazia ('$GPSACP:') = motor GNSS desligado. Coordenada malformada não levanta
    exceção: vira status 'malformed'.
    """
    failure = _failure(resp)
    if failure:
        return GnssReading(status=failure, response=resp)
    lines = [line for line in resp.lines if line.startswith("$GPSACP:")]
    if not lines:
        return GnssReading(status=READ_MALFORMED if resp.lines else READ_EMPTY, response=resp)
    body = lines[0][len("$GPSACP:"):].strip()
    if not body:
        return GnssReading(status=GNSS_OFF, response=resp)
    v = [x.strip() for x in body.split(",")]
    if len(v) < 11:
        return GnssReading(status=READ_MALFORMED, anomalies=[f"GPSACP:campos_insuficientes:{len(v)}"], response=resp)
    fix_type = parse_int(v[5])
    if fix_type in (0, 1):
        return GnssReading(status=GNSS_NO_FIX, response=resp)
    if fix_type not in (2, 3):
        return GnssReading(status=READ_MALFORMED, anomalies=[f"GPSACP:fix_desconhecido:{v[5]}"], response=resp)
    lat = nmea_to_decimal(v[1]) if v[1][-1:].upper() in ("N", "S") else None
    lon = nmea_to_decimal(v[2]) if v[2][-1:].upper() in ("E", "W") else None
    if lat is None or lon is None:
        return GnssReading(status=READ_MALFORMED, anomalies=[f"GPSACP:coordenada_invalida:{v[1]},{v[2]}"], response=resp)
    fix = GnssFix(
        lat=lat, lon=lon, alt_m=parse_float(v[4]), fix_type=fix_type, hdop=parse_float(v[3]),
        nsat=parse_int(v[10]), utc=gnss_utc(v[0], v[9]), course_deg=parse_float(v[6]), speed_kmh=parse_float(v[7]),
    )
    return GnssReading(status=READ_OK, fix=fix, response=resp)


def parse_gpsp(resp: AtResponse) -> Optional[int]:
    """AT$GPSP? -> 0 (sessão GNSS parada) ou 1 (ativa); None se a consulta falhou."""
    if not resp.ok:
        return None
    for line in resp.lines:
        match = _GPSP_RE.fullmatch(line)
        if match:
            return int(match.group(1))
    return None


# ---------------------------------------------------------------- célula servidora
_IDENTITY_FIELDS = ("rat", "arfcn", "pci", "plmn", "cell_id", "tac")


def _serving_candidate(matches, anomalies: List[str]):
    """Registro do #CSURV que identifica a servidora, entre os candidatos (índice, observação).

    Cópias do mesmo registro (identidade igual, ou só com campos ausentes em alguma) contam como
    um: fica a mais completa e a repetição vira anomalia. Valores diferentes num campo de
    identidade (ARFCN, PCI, PLMN, Cell-ID ou TAC) tornam a escolha ambígua: devolve None.
    """
    if len(matches) > 1:
        conflicts = [f for f in _IDENTITY_FIELDS if len({getattr(o, f) for _, o in matches} - {None}) > 1]
        if conflicts:
            anomalies.append(f"varios_registros_csurv:{len(matches)}:{','.join(conflicts)}")
            return None
        anomalies.append(f"registro_servidora_repetido_csurv:{len(matches)}")
    if not matches:
        return None
    return max(matches, key=lambda m: sum(getattr(m[1], f) is not None for f in _IDENTITY_FIELDS))


def resolve_serving_cell(
    moni: Optional[MoniReading], servinfo: Optional[ServInfoReading], csurv: Sequence[CellObservation]
) -> ServingCellResolution:
    """Identidade da célula servidora por cruzamento entre #MONI, #SERVINFO e #CSURV (função pura).

    LTE (e âncora do NR_NSA): #MONI e #SERVINFO dão Cell-ID, EARFCN e TAC, mas não o PCI. O PCI
    vem do registro LTE do #CSURV com o mesmo Cell-ID (e mesmo PLMN/EARFCN, quando conhecidos).
    Em campo, a célula F560E67 (EARFCN 3375, TAC 87BB) apareceu 2 vezes no #CSURV como cellId
    257298023, phyCellId 254: cópias do mesmo registro contam como um (anomalia
    registro_servidora_repetido_csurv). Registros com PCI, PLMN ou TAC diferentes tornam a
    servidora ambígua, e a radio_key fica vazia: o PCI não é adivinhado. A global_cell_key vem do
    registro do #CSURV escolhido ou, sem ele, do #SERVINFO (que traz o PLMN) ou do #MONI com Cc/Nc.
    NR_SA: o #MONI já traz NR_CH e NR_PCI (radio_key); o Cell-ID (NCI) só vem do #CSURV.
    """
    anomalies: List[str] = []
    moni_ok = moni is not None and moni.status == READ_OK
    servinfo_ok = servinfo is not None and servinfo.status == READ_OK
    if moni_ok:
        access = moni.access_mode
    elif servinfo_ok:
        access = servinfo.access_mode
    else:
        return ServingCellResolution(method="none")

    if access == ACCESS_NR_SA:
        nr = moni.nr if moni_ok else None
        if nr is None:
            fallback = servinfo.nr if servinfo_ok else None
            return ServingCellResolution(
                method="no_match", rat=RAT_NR, arfcn=fallback.arfcn if fallback else None,
                anomalies=["nr_sa_sem_pci"],
            )
        result = ServingCellResolution(
            method="moni_nr", rat=RAT_NR, arfcn=nr.arfcn, pci=nr.pci, plmn=nr.plmn,
            radio_key=nr.radio_key, radio_key_source=SOURCE_MONI if nr.radio_key else None,
        )
        matches = [(i, o) for i, o in enumerate(csurv)
                   if o.rat == RAT_NR and nr.radio_key and o.radio_key == nr.radio_key]
        chosen = _serving_candidate(matches, anomalies)
        if chosen is not None:
            i, obs = chosen
            result.csurv_index, result.cell_id, result.tac = i, obs.cell_id, obs.tac
            result.plmn = obs.plmn or result.plmn
            if obs.global_cell_key:
                result.global_cell_key, result.global_key_source = obs.global_cell_key, SOURCE_CSURV
        result.anomalies = anomalies
        return result

    moni_lte = moni.lte if moni_ok else None
    servinfo_lte = servinfo.lte if servinfo_ok else None
    if moni_lte and servinfo_lte and moni_lte.cell_id and servinfo_lte.cell_id and moni_lte.cell_id != servinfo_lte.cell_id:
        anomalies.append("servidora_mudou_entre_moni_e_servinfo")
        servinfo_lte = None  # o #SERVINFO descreve outra célula: não completa a do #MONI
    reference = moni_lte or servinfo_lte
    if reference is None:
        return ServingCellResolution(method="none", anomalies=anomalies)

    def first(attr):
        for obs in (moni_lte, servinfo_lte):
            if obs is not None and getattr(obs, attr) is not None:
                return getattr(obs, attr)
        return None

    cell_id, arfcn, tac, plmn = first("cell_id"), first("arfcn"), first("tac"), first("plmn")
    result = ServingCellResolution(method="no_match", rat=RAT_LTE, arfcn=arfcn, plmn=plmn, cell_id=cell_id, tac=tac)
    if access == ACCESS_NR_NSA and moni_ok and moni.nr is not None:
        result.nr_leg_radio_key = moni.nr.radio_key
    if cell_id is None:
        anomalies.append("servidora_sem_cell_id")
    else:
        matches = [
            (i, o) for i, o in enumerate(csurv)
            if o.rat == RAT_LTE and o.cell_id == cell_id
            and (plmn is None or o.plmn is None or o.plmn == plmn)
            and (arfcn is None or o.arfcn == arfcn)
        ]
        chosen = _serving_candidate(matches, anomalies)
        if chosen is not None:
            i, obs = chosen
            result.method, result.csurv_index, result.pci = "csurv", i, obs.pci
            result.radio_key = obs.radio_key
            result.radio_key_source = SOURCE_CSURV if obs.radio_key else None
            result.plmn = obs.plmn or result.plmn
            if obs.global_cell_key:
                result.global_cell_key, result.global_key_source = obs.global_cell_key, SOURCE_CSURV
            if tac and obs.tac and tac != obs.tac:
                anomalies.append("tac_divergente_csurv")
        elif matches:
            result.method = "ambiguous"
    if result.global_cell_key is None:
        for obs, source in ((servinfo_lte, SOURCE_SERVINFO), (moni_lte, SOURCE_MONI)):
            if obs is not None and obs.global_cell_key:
                result.global_cell_key, result.global_key_source = obs.global_cell_key, source
                break
    result.anomalies = anomalies
    return result
