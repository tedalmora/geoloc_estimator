"""Driver de modem para o Telit Cinterion FN990.

Os comandos usados aqui são reais -- confirmados a partir do AT+CLAC rodado no modem físico
(ver planejamento/comandosModem.txt e planejamento/guia_comandos_modem.md). O FORMATO exato
das respostas que dependem de rede (#MONI, #RFSTS, #CSURV, $GPSACP com fix) ainda não foi
validado com SIM + rede real -- os comentários abaixo marcam onde recalibrar assim que
houver dados de campo (cole a saída real e ajuste as regex, é o mesmo processo já usado
para confirmar os comandos existentes via AT+CLAC).
"""
import re
import time
from typing import List, Optional

import serial  # pyserial

from .modem_driver import ModemDriver
from .models import GnssFix, LppReport, ModemStatus, NeighborCell, ServingCell, SignalMetrics
from .parsing import extract_float, extract_int, extract_str, nmea_to_decimal, strip_echo


class FN990Driver(ModemDriver):
    def __init__(self) -> None:
        self._ser: Optional[serial.Serial] = None
        self._port: Optional[str] = None
        self._baudrate: int = 115200
        self._timeout: float = 5.0

    # ------------------------------------------------------------------ conexão
    def connect(self, port: str, baudrate: int = 115200, timeout: float = 5.0) -> None:
        self._port, self._baudrate, self._timeout = port, baudrate, timeout
        self._ser = serial.Serial(port, baudrate=baudrate, timeout=timeout)

    def close(self) -> None:
        if self._ser and self._ser.is_open:
            self._ser.close()

    # ------------------------------------------------------------------ AT cru
    def send_at(self, command: str, timeout: float = 5.0) -> str:
        """Envia 1 comando AT e devolve tudo que o modem respondeu, como texto puro.

        Lê linha a linha (em vez de 1 leitura só) porque vários comandos AT devolvem
        múltiplas linhas antes do 'OK' final (ex.: AT#CSURV lista várias células).
        """
        if self._ser is None:
            raise RuntimeError("Chame connect() antes de enviar comandos AT.")
        self._ser.reset_input_buffer()
        self._ser.write((command + "\r\n").encode("ascii"))

        deadline = time.time() + timeout
        lines: List[str] = []
        while time.time() < deadline:
            raw_line = self._ser.readline()  # respeita o timeout já configurado no Serial
            if not raw_line:
                continue
            line = raw_line.decode(errors="ignore").strip()
            if not line:
                continue
            lines.append(line)
            if line in ("OK", "ERROR") or line.startswith("+CME ERROR"):
                break
        return "\n".join(lines)

    def is_alive(self) -> bool:
        """Teste de vida mais barato possível -- usado pelo health check do Coletor."""
        try:
            return "OK" in self.send_at("AT", timeout=2.0)
        except Exception:
            return False

    # ------------------------------------------------------------------ status
    def get_status(self) -> ModemStatus:
        return ModemStatus(
            manufacturer=strip_echo(self.send_at("AT+CGMI")),
            model=strip_echo(self.send_at("AT+CGMM")),
            firmware=strip_echo(self.send_at("AT+CGMR")),
            imei=strip_echo(self.send_at("AT+CGSN")),
            sim_status=strip_echo(self.send_at("AT+CPIN?")),
            signal_quality_raw=strip_echo(self.send_at("AT+CSQ")),
            operator=strip_echo(self.send_at("AT+COPS?")),
            uptime_raw=strip_echo(self.send_at("AT#UPTIME")),
            temperature_raw=strip_echo(self.send_at("AT#TEMPSENS")),
            alive=self.is_alive(),
        )

    def reboot(self) -> None:
        """AT#REBOOT reinicia o módulo por completo -- a porta serial cai junto, por isso
        fechamos a conexão local em seguida (reconectar depois é responsabilidade de quem chamou)."""
        self.send_at("AT#REBOOT", timeout=2.0)
        self.close()

    # ------------------------------------------------------------------ sinal / célula
    def get_signal_metrics(self) -> SignalMetrics:
        raw = self.send_at("AT#RFSTS")
        rsrp = extract_float(raw, r"RSRP:\s*(-?\d+\.?\d*)")
        rsrq = extract_float(raw, r"RSRQ:\s*(-?\d+\.?\d*)")
        sinr = extract_float(raw, r"SINR:\s*(-?\d+\.?\d*)")
        ta = extract_int(raw, r"TA:\s*(\d+)")
        technology = "5G" if "NR" in raw.upper() else "4G"
        return SignalMetrics(technology, rsrp, rsrq, sinr, ta)

    def get_serving_cell(self) -> ServingCell:
        raw = self.send_at("AT#MONI")
        cell_id = extract_str(raw, r"Cell:\s*([0-9A-Fa-f]+)")
        pci = extract_int(raw, r"PSC/PCI:\s*(\d+)")
        tac = extract_str(raw, r"TAC:\s*([0-9A-Fa-f]+)")
        return ServingCell(cell_id, pci, tac)

    def get_neighbor_cells(self) -> List[NeighborCell]:
        raw = self.send_at("AT#CSURV", timeout=10.0)  # varredura completa -- pode demorar mais
        neighbors: List[NeighborCell] = []
        for pci_str, rsrp_str in re.findall(r"PCI:\s*(\d+).*?RSRP:\s*(-?\d+\.?\d*)", raw, re.S):
            neighbors.append(NeighborCell(pci=int(pci_str), rsrp_dbm=float(rsrp_str)))
        return neighbors

    # ------------------------------------------------------------------ GNSS
    def enable_gnss(self) -> None:
        self.send_at("AT$GPSP=1")

    def get_gnss_fix(self) -> Optional[GnssFix]:
        raw = self.send_at("AT$GPSACP")
        # Formato Telit: $GPSACP: <utc>,<lat>,<lon>,<hdop>,<alt>,<fix>,<cog>,<spkm>,<spkn>,<data>,<nsat>
        match = re.search(r"\$GPSACP:\s*([^\r\n]+)", raw)
        if not match:
            return None
        fields = [f.strip() for f in match.group(1).split(",")]
        if len(fields) < 6 or not fields[1] or fields[5] in ("0", ""):
            return None  # campo 'fix' = 0 (ou ausente) significa "sem posição ainda"
        lat = nmea_to_decimal(fields[1])
        lon = nmea_to_decimal(fields[2])
        alt = float(fields[4]) if fields[4] else None
        if lat is None or lon is None:
            return None
        return GnssFix(lat=lat, lon=lon, alt_m=alt, fix_quality=fields[5])

    # ------------------------------------------------------------------ LPP/DL-AoD (experimental)
    def enable_lpp_positioning(self) -> str:
        """'Melhor esforço': habilita a sessão LPP e devolve a resposta bruta para log --
        quem calcula a posição/ângulo é a rede, não este driver (ver arquitetura_hal.md)."""
        return self.send_at("AT$LCSLPP=1", timeout=10.0)

    def get_lpp_report(self) -> LppReport:
        """'Melhor esforço': consulta o estado da sessão LPP habilitada por enable_lpp_positioning().
        O comando exato de leitura do relatório e seu formato ainda não foram confirmados em
        campo -- por enquanto isso só devolve o texto bruto e tenta 2 padrões genéricos de
        ângulo/RSTD, que provavelmente precisarão ser ajustados assim que houver dados reais
        (mesmo processo já usado para os outros comandos, ver guia_comandos_modem.md)."""
        raw = self.send_at("AT$LCSLPP?", timeout=5.0)
        angle = extract_float(raw, r"(?:ANGLE|AOA|AOD)\s*[:=]?\s*(-?\d+\.?\d*)")
        rstd = extract_float(raw, r"RSTD\s*[:=]?\s*(-?\d+\.?\d*)")
        return LppReport(raw_text=raw, angle_deg=angle, rstd=rstd)
