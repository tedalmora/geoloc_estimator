"""Estruturas de dados devolvidas pelo HAL.

Identidade de célula (decisão da Etapa 2; ver planejamento/divergencias_codigo_tcc.md):

- radio_key = RAT:ARFCN:PCI -- identidade de rádio. É a chave das observações, do snapshot, do
  banco multicélula, do vocabulário e das features do fingerprint. O PCI sozinho não identifica
  a célula: o LTE tem só 504 PCIs (TS 36.300 §5.1.4), e o mesmo PCI se repete em portadoras
  diferentes (no exemplo do manual do FN990, p. 820, o PCI 64 aparece nos EARFCN 2850, 1350 e 3200).
- global_cell_key = RAT:PLMN:CELL_ID -- identidade lógica, equivalente ao ECGI (PLMN + E-UTRA Cell
  Identifier, TS 36.300 §8.2) e ao NCGI (PLMN + NCI, TS 38.300 §8.2). É a chave do cadastro de
  células e do método Cell-ID.

Cada chave só existe quando todos os seus componentes vieram no MESMO registro do modem. Não há
chave parcial nem troca de um campo por outro (por exemplo, PCI no lugar do Cell-ID). Assim a mesma
observação nunca muda de identidade conforme os campos disponíveis. A relação entre as duas chaves
fica nas observações que trazem ambas (registros completos do #CSURV).

A RAT é a tecnologia de rádio da célula (LTE ou NR). Ela não se confunde com o modo de acesso do UE
numa leitura (LTE, NR_NSA, NR_SA ou UNKNOWN): no EN-DC o UE fica ancorado numa célula LTE e usa
também uma célula NR, e cada célula guarda a própria RAT.
"""
import dataclasses
import re
from dataclasses import dataclass, field
from typing import Any, List, Optional

RAT_LTE = "LTE"
RAT_NR = "NR"

ACCESS_LTE = "LTE"
ACCESS_NR_NSA = "NR_NSA"
ACCESS_NR_SA = "NR_SA"
ACCESS_UNKNOWN = "UNKNOWN"

SOURCE_MONI = "MONI"
SOURCE_SERVINFO = "SERVINFO"
SOURCE_CSURV = "CSURV"

# Situação de cada leitura
READ_OK = "ok"
READ_EMPTY = "empty"  # comando aceito (OK), mas sem dado utilizável
READ_ERROR = "error"  # ERROR, +CME ERROR ou +CMS ERROR
READ_TIMEOUT = "timeout"  # nenhum código final dentro do prazo
READ_MALFORMED = "malformed"  # resposta fora do formato esperado
READ_UNSUPPORTED = "unsupported"  # formato reconhecido, mas de rede/recurso não tratado
READ_SKIPPED = "skipped"  # comando não executado (erro de porta ou modem sem resposta)
GNSS_NO_FIX = "no_fix"  # motor GNSS ligado, sem fix válido (campo fix 0 ou 1)
GNSS_OFF = "off"  # motor GNSS desligado (resposta "$GPSACP:" vazia)

_PLMN_RE = re.compile(r"\d{5,6}")
_HEX_RE = re.compile(r"[0-9A-Fa-f]+")
_FINAL_RE = re.compile(r"^(?:OK|ERROR|\+CME ERROR:.*|\+CMS ERROR:.*)$")
_ERROR_CODE_RE = re.compile(r"^\+CM[ES] ERROR:\s*(\d+)$")


def canonical_hex(value) -> Optional[str]:
    """Hexadecimal maiúsculo sem zeros à esquerda ('0f560e67' -> 'F560E67'); None se inválido."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return format(value, "X") if value >= 0 else None
    text = str(value).strip()
    if not _HEX_RE.fullmatch(text):
        return None
    return format(int(text, 16), "X")


def make_radio_key(rat: Optional[str], arfcn: Optional[int], pci: Optional[int]) -> Optional[str]:
    if rat not in (RAT_LTE, RAT_NR) or arfcn is None or pci is None:
        return None
    return f"{rat}:{int(arfcn)}:{int(pci)}"


def make_global_cell_key(rat: Optional[str], plmn: Optional[str], cell_id: Optional[str]) -> Optional[str]:
    cell = canonical_hex(cell_id)
    if rat not in (RAT_LTE, RAT_NR) or not plmn or not _PLMN_RE.fullmatch(plmn) or cell is None:
        return None
    return f"{rat}:{plmn}:{cell}"


@dataclass(frozen=True)
class CellObservation:
    """1 célula vista por 1 comando, só com o que aquele registro trouxe (fontes não se misturam).

    É imutável: as chaves derivam dos próprios campos e não mudam depois de criada.
    """
    rat: str
    source: str
    arfcn: Optional[int] = None
    pci: Optional[int] = None
    plmn: Optional[str] = None  # MCC+MNC em dígitos, ex.: '72406'
    cell_id: Optional[str] = None  # hexadecimal maiúsculo, sem zeros à esquerda
    tac: Optional[str] = None  # hexadecimal maiúsculo (o #CSURV informa em decimal)
    rsrp_dbm: Optional[float] = None
    rsrq_db: Optional[float] = None
    sinr_db: Optional[float] = None
    rx_level_dbm: Optional[float] = None  # PWR do #MONI, <dBM> do #SERVINFO, rxLev do #CSURV
    cell_status: Optional[str] = None  # só #CSURV: CELL_SUITABLE, CELL_FORBIDDEN, ...
    band: Optional[int] = None
    bandwidth: Optional[int] = None  # valor como o modem informa (bw do #CSURV, NR_BW do #MONI)
    scs_khz: Optional[int] = None  # só NR (nr_scs do #CSURV)
    record_format: Optional[str] = None  # #CSURV: 'full' (com PLMN/Cell-ID) ou 'short'
    is_serving: bool = False

    @property
    def radio_key(self) -> Optional[str]:
        return make_radio_key(self.rat, self.arfcn, self.pci)

    @property
    def global_cell_key(self) -> Optional[str]:
        return make_global_cell_key(self.rat, self.plmn, self.cell_id)

    @property
    def identity_status(self) -> str:
        """'complete' (as duas chaves), 'radio', 'global' ou 'none'."""
        radio, glob = self.radio_key is not None, self.global_cell_key is not None
        if radio and glob:
            return "complete"
        return "radio" if radio else ("global" if glob else "none")

    def to_dict(self) -> dict:
        data = dataclasses.asdict(self)
        data.update(
            radio_key=self.radio_key, global_cell_key=self.global_cell_key, identity_status=self.identity_status
        )
        return data


@dataclass
class AtResponse:
    """Resposta de 1 comando AT: linhas recebidas, código final e tempos."""
    command: str
    raw_lines: List[str]  # tudo que chegou até o código final, sem linhas vazias (inclui eco)
    final: Optional[str]  # 'OK', 'ERROR', '+CME ERROR: <n>', '+CMS ERROR: <n>' ou None (prazo esgotado)
    elapsed_s: float = 0.0
    pending_bytes_before: int = 0  # bytes que já estavam na porta antes do envio (descartados)
    drained_bytes_after: int = 0  # bytes que chegaram depois do código final (descartados)

    @staticmethod
    def is_final(line: str) -> bool:
        return bool(_FINAL_RE.match(line.strip()))

    @classmethod
    def from_raw(cls, command: str, raw_lines: List[str], elapsed_s: float = 0.0) -> "AtResponse":
        """Remonta a resposta a partir das linhas cruas (fixtures, reprocessamento do banco)."""
        lines = [line.strip() for line in raw_lines if line.strip()]
        for i, line in enumerate(lines):
            if cls.is_final(line):
                return cls(command, lines[: i + 1], line, elapsed_s)
        return cls(command, lines, None, elapsed_s)

    @property
    def ok(self) -> bool:
        return self.final == "OK"

    @property
    def timed_out(self) -> bool:
        return self.final is None

    @property
    def error_code(self) -> Optional[int]:
        match = _ERROR_CODE_RE.match(self.final or "")
        return int(match.group(1)) if match else None

    @property
    def lines(self) -> List[str]:
        """Linhas de dado: sem o eco do comando e sem o código final."""
        lines = list(self.raw_lines)
        if self.final is not None and lines and lines[-1] == self.final:
            lines.pop()
        if lines and lines[0].upper() == self.command.strip().upper():
            lines.pop(0)
        return lines

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    @property
    def raw_text(self) -> str:
        return "\n".join(self.raw_lines)


@dataclass
class MoniReading:
    """Resultado do #MONI: célula servidora LTE e/ou NR, com o modo de acesso deduzido."""
    status: str
    access_mode: str = ACCESS_UNKNOWN
    observations: List[CellObservation] = field(default_factory=list)
    network_name: Optional[str] = None
    nr_state: Optional[int] = None  # 0 init, 1 idle, 2 connected (manual p. 312)
    anomalies: List[str] = field(default_factory=list)
    response: Optional[AtResponse] = None

    @property
    def lte(self) -> Optional[CellObservation]:
        return next((o for o in self.observations if o.rat == RAT_LTE), None)

    @property
    def nr(self) -> Optional[CellObservation]:
        return next((o for o in self.observations if o.rat == RAT_NR), None)

    @property
    def primary(self) -> Optional[CellObservation]:
        """Célula que define o modo de acesso: a âncora LTE (LTE/NR_NSA) ou a célula NR (NR_SA)."""
        return self.nr if self.access_mode == ACCESS_NR_SA else self.lte


@dataclass
class ServInfoReading:
    """Resultado do #SERVINFO: traz o PLMN (NetCode) que o #MONI não informa quando há nome de rede."""
    status: str
    access_mode: str = ACCESS_UNKNOWN
    observations: List[CellObservation] = field(default_factory=list)
    network_name: Optional[str] = None
    endc: bool = False
    anomalies: List[str] = field(default_factory=list)
    response: Optional[AtResponse] = None

    @property
    def lte(self) -> Optional[CellObservation]:
        return next((o for o in self.observations if o.rat == RAT_LTE), None)

    @property
    def nr(self) -> Optional[CellObservation]:
        return next((o for o in self.observations if o.rat == RAT_NR), None)


@dataclass
class CsurvReading:
    """Resultado do #CSURV: todas as células LTE/NR decodificadas na varredura."""
    status: str
    observations: List[CellObservation] = field(default_factory=list)
    ignored_records: int = 0  # registros 2G/3G (não tratados)
    anomalies: List[str] = field(default_factory=list)
    response: Optional[AtResponse] = None


@dataclass
class GnssFix:
    lat: float
    lon: float
    alt_m: Optional[float]
    fix_type: int  # 2 = 2D, 3 = 3D (manual §4.11.5.3)
    hdop: Optional[float]
    nsat: Optional[int]
    utc: Optional[str]  # ISO 8601, ex.: '2019-08-29T00:25:18.000Z'
    course_deg: Optional[float] = None
    speed_kmh: Optional[float] = None


@dataclass
class GnssReading:
    status: str  # ok, no_fix, off, error, timeout, malformed, empty
    fix: Optional[GnssFix] = None
    anomalies: List[str] = field(default_factory=list)
    response: Optional[AtResponse] = None


@dataclass
class LppConfigProbe:
    """Sonda experimental, somente leitura, de AT$LCSLPP? (configuração do LPP para A-GNSS,
    manual §4.11.1.14). Guarda só o texto cru e a situação da consulta. O manual não documenta
    comando AT que devolva ângulo (DL-AoD/UL-AoA) nem RSTD, então nada disso é extraído aqui."""
    status: str
    raw_text: str = ""
    response: Optional[AtResponse] = None


@dataclass
class PrepareResult:
    """O que prepare_for_collection() encontrou e fez."""
    gnss_power_before: Optional[int]  # resposta de AT$GPSP? (0 desligado, 1 ligado) ou None
    gnss_power_on_sent: bool
    gnss_power_on_final: Optional[str]
    responses: List[AtResponse] = field(default_factory=list)


@dataclass
class ServingCellResolution:
    """Identidade da célula servidora de uma leitura, montada por cruzamento entre comandos.

    As chaves vêm prontas de UMA observação (nunca de campos misturados); *_source diz de qual.
    """
    method: str  # csurv, ambiguous, no_match, moni_nr, none
    rat: Optional[str] = None
    arfcn: Optional[int] = None
    pci: Optional[int] = None
    plmn: Optional[str] = None
    cell_id: Optional[str] = None
    tac: Optional[str] = None
    radio_key: Optional[str] = None
    radio_key_source: Optional[str] = None
    global_cell_key: Optional[str] = None
    global_key_source: Optional[str] = None
    csurv_index: Optional[int] = None  # posição da observação do #CSURV que casou
    nr_leg_radio_key: Optional[str] = None  # perna NR do EN-DC (do #MONI), quando conectada
    anomalies: List[str] = field(default_factory=list)


@dataclass
class ModemStatus:
    """Identificação e saúde do modem (texto cru de cada comando; usado no status e no health check)."""
    manufacturer: Optional[str]
    model: Optional[str]
    firmware: Optional[str]
    imei: Optional[str]
    sim_status: Optional[str]
    signal_quality_raw: Optional[str]
    operator: Optional[str]
    uptime_raw: Optional[str]
    temperature_raw: Optional[str]
    alive: bool


def to_jsonable(obj: Any) -> Any:
    """Converte leituras do HAL em estruturas serializáveis em JSON (inclui as chaves das células)."""
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        data = {f.name: to_jsonable(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
        if isinstance(obj, AtResponse):
            data["lines"] = obj.lines
        return data
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(item) for item in obj]
    if isinstance(obj, dict):
        return {key: to_jsonable(value) for key, value in obj.items()}
    return obj
