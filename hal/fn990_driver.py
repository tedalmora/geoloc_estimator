"""Driver de modem para o Telit Cinterion FN990.

Os comandos usados aqui são reais -- confirmados a partir do AT+CLAC rodado no modem físico
(ver planejamento/comandosModem.txt e planejamento/guia_comandos_modem.md). O FORMATO das
respostas de rede (#MONI, #CSURV, #SERVINFO, +CESQ, $GPSACP com fix) já foi validado em campo
com SIM real (Vivo, LTE) -- ver docstrings de cada método para o que cada campo significa e o
que ainda não foi confirmado (SINR, Timing Advance).
"""
import re
import time
from typing import List, Optional

import serial  # pyserial

from .modem_driver import ModemDriver
from .models import GnssFix, LppReport, ModemStatus, NeighborCell, ServingCell, SignalMetrics
from .parsing import extract_float, extract_str, nmea_to_decimal, strip_echo


class FN990Driver(ModemDriver):
    def __init__(self) -> None:
        self._ser: Optional[serial.Serial] = None
        self._port: Optional[str] = None
        self._baudrate: int = 115200
        self._timeout: float = 5.0
        self._csurv_cache: Optional[List[str]] = None
        self._csurv_cache_time: float = 0.0

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
        """RSRP/RSRQ vêm do #MONI -- formato real confirmado em campo (Vivo, LTE), 2x, e
        cruzado com AT+CESQ (rsrq=(16/2)-19,5=-11,5dB, rsrp=34-140=-106dBm -- muito próximo
        do #MONI na mesma janela de tempo, confirma a leitura). SINR e Timing Advance **não**
        aparecem em nenhum dos comandos testados até agora (#MONI, #RFSTS, #SERVINFO, +CESQ)
        -- não é mais só "ainda não achamos", parece não estar exposto via AT simples neste
        firmware. Ficam None; só reconsiderar se aparecer um comando/campo novo confirmado."""
        raw = self.send_at("AT#MONI")
        rsrp = extract_float(raw, r"RSRP:\s*(-?\d+\.?\d*)")
        rsrq = extract_float(raw, r"RSRQ:\s*(-?\d+\.?\d*)")
        sinr = None  # não exposto via AT simples neste firmware -- ver docstring
        ta = None    # não exposto via AT simples neste firmware -- ver docstring
        technology = "5G" if "NR" in raw.upper() else "4G"
        return SignalMetrics(technology, rsrp, rsrq, sinr, ta)

    def get_serving_cell(self) -> ServingCell:
        """`Id:`/`TAC:` do #MONI dão cell_id/tac direto. PCI (Physical Cell ID) **não** aparece
        no #MONI nem no #SERVINFO (`#SERVINFO: 3375,-76,"VIVO","72406",F560E67,87BB,640,3,-107`
        -- conferido campo a campo, nenhum bate com o PCI real) -- só existe cruzando o
        #CSURV (que lista `phyCellId` por célula) com o cell_id da própria #MONI. Confirmado
        2x em campo: a mesma célula (cellId F560E67/tac 87BB/earfcn 3375) sempre aparece com
        phyCellId 254 no #CSURV. Isso custa 1 varredura completa (`_get_csurv_records`, com
        cache curto -- ver abaixo) só na primeira vez do ciclo de poll."""
        raw = self.send_at("AT#MONI")
        cell_id = extract_str(raw, r"Id:\s*([0-9A-Fa-f]+)")
        tac = extract_str(raw, r"TAC:\s*([0-9A-Fa-f]+)")
        pci = self._find_pci_via_csurv(cell_id) if cell_id else None
        return ServingCell(cell_id, pci, tac)

    def _get_csurv_records(self, max_age_s: float = 3.0) -> List[str]:
        """Cacheia o #CSURV por alguns segundos -- `get_serving_cell()` (via cross-match de
        PCI) e `get_neighbor_cells()` são chamados em sequência no mesmo ciclo de poll
        (`coletor/daemon.py: poll_once`) e não precisam rodar a varredura completa (alguns
        segundos) duas vezes seguidas. Separa por REGISTRO (não por linha) porque o terminal
        pode quebrar uma linha ao meio por causa da largura da tela -- os bytes reais do modem
        não garantem isso."""
        agora = time.time()
        if self._csurv_cache is not None and (agora - self._csurv_cache_time) < max_age_s:
            return self._csurv_cache
        raw = self.send_at("AT#CSURV", timeout=10.0)  # varredura completa -- pode demorar mais
        self._csurv_cache = re.split(r"(?=\b(?:earfcn|uarfcn|nr_arfcn):)", raw)
        self._csurv_cache_time = agora
        return self._csurv_cache

    def _find_pci_via_csurv(self, cell_id_hex: str) -> Optional[int]:
        """Acha o phyCellId (PCI) do registro do #CSURV cujo cellId (decimal) bate com o
        cell_id (hex) reportado pela #MONI -- ver docstring de get_serving_cell()."""
        try:
            cell_id_dec = int(cell_id_hex, 16)
        except ValueError:
            return None
        for record in self._get_csurv_records():
            if not record.lstrip().startswith("earfcn:"):
                continue
            match = re.search(r"cellId:\s*(\d+).*?phyCellId:\s*(\d+)", record, re.S)
            if match and int(match.group(1)) == cell_id_dec:
                return int(match.group(2))
        return None

    def get_neighbor_cells(self) -> List[NeighborCell]:
        """Formato real confirmado em campo -- 1 registro por célula, um destes 3 tipos:
        'nr_arfcn: 629952 nr_band: 78 nr_scs: 30 nr_mcc: 724 nr_mnc: 06' (5G NR -- sem RSRP
        nesse formato, só ARFCN/banda, então ainda não dá pra usar como feature de sinal);
        'earfcn: 3375 rxLev: -76 mcc: 724 mnc: 06 cellId: 257298023 tac: 34747 phyCellId: 254
        cellStatus: CELL_SUITABLE rsrp: -107 rsrq: -12 bw: 15' (LTE -- é o que usamos aqui,
        `phyCellId` = PCI); 'uarfcn: ... scr code: 2960 ... rscp: -78 ecio: -7.0' (3G/UMTS --
        usa "scrambling code" em vez de PCI e RSCP em vez de RSRP, TODO: incorporar também)."""
        neighbors: List[NeighborCell] = []
        for record in self._get_csurv_records():
            if not record.lstrip().startswith("earfcn:"):
                continue  # TODO: incorporar "uarfcn:" (3G) -- "nr_arfcn:" não traz RSRP
            match = re.search(r"phyCellId:\s*(\d+).*?rsrp:\s*(-?\d+\.?\d*)", record, re.S)
            if match:
                neighbors.append(NeighborCell(pci=int(match.group(1)), rsrp_dbm=float(match.group(2))))
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
