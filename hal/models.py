"""Estruturas de dados comuns que toda implementação de ModemDriver deve devolver.

Manter esses campos estáveis é o que permite ao Coletor e ao Estimador tratarem o
FN990, o EM7565 e o EM120R-GL da mesma forma, sem precisar saber qual modem está por trás.
"""
from dataclasses import dataclass
from typing import Optional


@dataclass
class SignalMetrics:
    technology: str  # '4G' ou '5G' -- detectado a partir do texto da resposta do modem
    rsrp_dbm: Optional[float]
    rsrq_db: Optional[float]
    sinr_db: Optional[float]
    timing_advance: Optional[int]


@dataclass
class ServingCell:
    cell_id: Optional[str]  # ID da célula, como o modem reporta (hexadecimal)
    pci: Optional[int]  # Physical Cell ID
    tac: Optional[str]  # Tracking Area Code


@dataclass
class NeighborCell:
    pci: int
    rsrp_dbm: float


@dataclass
class GnssFix:
    lat: float
    lon: float
    alt_m: Optional[float]
    fix_quality: Optional[str]


@dataclass
class ModemStatus:
    """Snapshot de identificação/saúde do modem -- usado pelo comando de status e pelo health check."""
    manufacturer: Optional[str]
    model: Optional[str]
    firmware: Optional[str]
    imei: Optional[str]
    sim_status: Optional[str]  # texto cru de AT+CPIN? (ex.: 'READY', '+CME ERROR: SIM not inserted')
    signal_quality_raw: Optional[str]  # texto cru de AT+CSQ -- útil mesmo sem parsing fino
    operator: Optional[str]  # texto cru de AT+COPS?
    uptime_raw: Optional[str]  # texto cru de AT#UPTIME
    temperature_raw: Optional[str]  # texto cru de AT#TEMPSENS
    alive: bool  # resultado do teste mais simples possível: "AT" -> contém "OK"


@dataclass
class LppReport:
    """Resultado de uma tentativa de leitura de posicionamento via LPP (DL-AoD/UL-AoA).

    O formato real da resposta ainda não foi validado em campo (precisa de SIM + core) -- por
    isso angle_deg/rstd ficam None até isso acontecer. raw_text sempre vem preenchido, para
    você colar aqui e ajustar o parsing quando tiver dados reais (mesmo processo já usado
    para os outros comandos, ver planejamento/guia_comandos_modem.md).
    """
    raw_text: str
    angle_deg: Optional[float]
    rstd: Optional[float]
