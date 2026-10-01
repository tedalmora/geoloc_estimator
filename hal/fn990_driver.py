"""Driver de modem para o Telit Cinterion FN990.

Comandos conferidos no AT+CLAC do modem (planejamento/comandosModem.txt) e no manual
80691ST11097A Rev.5 (2025-10-20). Os formatos de resposta e as decisões de parsing estão em
hal/parsing.py; aqui ficam só o transporte AT e a ordem dos comandos.

Regras de segurança do driver:
- nenhum comando com configuração salva automaticamente. Em particular, não há escrita de
  AT$LCSLPP ("Setting saved: Auto", manual p. 740); o HAL não trata LPP (a consulta experimental,
  só leitura, fica em experimental/lpp_probe.py);
- prepare_for_collection() ALTERA O ESTADO do GNSS: envia AT$GPSP=1 só se AT$GPSP? disser que a
  sessão GNSS está parada, porque o comando também apaga a memória do GNSS (manual p. 763, que
  classifica a configuração como "Setting saved: Other", fora do perfil comum). Executam o
  prepare: `hal.cli prepare`, `acquisition.snapshot` sem --no-prepare e `coletor.daemon
  once/continuous` (no início da sessão e depois de cada reconexão);
- o #MONI não é reconfigurado: se ficou em modo de célula adjacente, a leitura vem 'malformed'.
"""
import time
from typing import List, Optional

import serial  # pyserial

from .modem_driver import ModemDriver
from .models import (
    AtResponse,
    CsurvReading,
    GnssReading,
    ModemStatus,
    MoniReading,
    PrepareResult,
    ServInfoReading,
)
from .parsing import parse_csurv, parse_gpsacp, parse_gpsp, parse_moni, parse_servinfo

READ_SLICE_S = 0.2  # leitura em fatias curtas: o prazo total vale mesmo com a porta muda
POLL_S = 0.05
HANDSHAKE_ATTEMPTS = 3
HANDSHAKE_SETTLE_S = 1.0
DEFAULT_TIMEOUT_S = 5.0
CSURV_TIMEOUT_S = 180.0  # manual p. 818: "executed within max. 3 minute"
CSURV_DRAIN_S = 1.0  # campo: o #CSURV continua emitindo bytes logo depois do OK
CSURV_MIN_INTERVAL_S = 2.0  # campo: um #CSURV logo depois de outro devolve varredura vazia
GPSP_ON_TIMEOUT_S = 10.0


class FN990Driver(ModemDriver):
    def __init__(self) -> None:
        self._ser: Optional[serial.Serial] = None
        self._port: Optional[str] = None
        self._baudrate: int = 115200
        self._timeout: float = DEFAULT_TIMEOUT_S
        self._last_csurv_end: Optional[float] = None

    # ------------------------------------------------------------------ conexão
    def connect(self, port: str, baudrate: int = 115200, timeout: float = DEFAULT_TIMEOUT_S) -> None:
        """Abre a porta e só retorna quando 'AT' recebe OK; senão levanta ConnectionError.

        Antes de cada tentativa descarta o que chegar por HANDSHAKE_SETTLE_S: em campo, bytes
        atrasados de um #CSURV de uma conexão anterior corromperam o primeiro comando.
        """
        self._port, self._baudrate, self._timeout = port, baudrate, timeout
        try:
            self._ser = serial.Serial(port, baudrate=baudrate, timeout=READ_SLICE_S)
        except serial.SerialException as exc:
            raise ConnectionError(f"{port}: não foi possível abrir a porta ({exc})") from exc
        last: Optional[AtResponse] = None
        for _ in range(HANDSHAKE_ATTEMPTS):
            self._discard_pending(HANDSHAKE_SETTLE_S)
            last = self.send_at("AT", timeout=2.0)
            if last.ok:
                return
        self.close()
        final = last.final if last is not None and last.final else "nenhuma resposta"
        raise ConnectionError(f"{port}: o modem não respondeu OK a 'AT' em {HANDSHAKE_ATTEMPTS} tentativas ({final})")

    def close(self) -> None:
        if self._ser is not None and self._ser.is_open:
            self._ser.close()

    # ------------------------------------------------------------------ transporte AT
    def send_at(self, command: str, timeout: Optional[float] = None, drain_after: float = 0.0) -> AtResponse:
        """Envia 1 comando (terminado em <CR>, manual §3) e lê até o código final ou o prazo.

        O prazo é total: a porta é lida em fatias de READ_SLICE_S, então uma porta muda não
        segura a chamada além de timeout + READ_SLICE_S. Bytes pendentes antes do envio são
        descartados e contados; `drain_after` > 0 descarta o que chegar depois do código final
        (usado no #CSURV). O timeout original da porta é restaurado no fim.
        """
        ser = self._require_port()
        timeout = self._timeout if timeout is None else timeout
        pending = ser.in_waiting
        ser.reset_input_buffer()
        original_timeout = ser.timeout
        ser.timeout = READ_SLICE_S
        lines: List[str] = []
        final: Optional[str] = None
        partial = b""
        start = time.monotonic()
        deadline = start + timeout
        try:
            ser.write((command + "\r").encode("ascii"))
            while final is None and time.monotonic() < deadline:
                chunk = ser.readline()
                if not chunk:
                    continue
                partial += chunk
                if not partial.endswith(b"\n"):
                    continue
                line = partial.decode("utf-8", errors="replace").strip()
                partial = b""
                if line:
                    lines.append(line)
                    if AtResponse.is_final(line):
                        final = line
            if partial.strip():
                lines.append(partial.decode("utf-8", errors="replace").strip())
            elapsed = time.monotonic() - start
            drained = self._discard_pending(drain_after) if drain_after > 0 else 0
        finally:
            ser.timeout = original_timeout
        return AtResponse(command, lines, final, elapsed, pending, drained)

    def _discard_pending(self, duration: float) -> int:
        """Lê e descarta tudo que chegar durante `duration` segundos; devolve quantos bytes."""
        ser = self._require_port()
        end = time.monotonic() + duration
        total = 0
        while time.monotonic() < end:
            waiting = ser.in_waiting
            if waiting:
                total += len(ser.read(waiting))
            else:
                time.sleep(POLL_S)
        return total

    def _require_port(self):
        if self._ser is None:
            raise RuntimeError("Chame connect() antes de enviar comandos AT.")
        return self._ser

    def is_alive(self) -> bool:
        try:
            return self.send_at("AT", timeout=2.0).ok
        except (serial.SerialException, OSError, RuntimeError):
            return False

    # ------------------------------------------------------------------ status
    def get_status(self) -> ModemStatus:
        def text(command: str) -> Optional[str]:
            resp = self.send_at(command)
            return resp.text if resp.ok else (resp.final or "sem resposta")

        return ModemStatus(
            manufacturer=text("AT+CGMI"),
            model=text("AT+CGMM"),
            firmware=text("AT+CGMR"),
            imei=text("AT+CGSN"),
            sim_status=text("AT+CPIN?"),
            signal_quality_raw=text("AT+CSQ"),
            operator=text("AT+COPS?"),
            uptime_raw=text("AT#UPTIME"),
            temperature_raw=text("AT#TEMPSENS"),
            alive=self.is_alive(),
        )

    def reboot(self) -> None:
        """AT#REBOOT derruba a porta serial junto; reconectar fica com quem chamou."""
        self.send_at("AT#REBOOT", timeout=2.0)
        self.close()

    def prepare_for_collection(self) -> PrepareResult:
        """Liga o GNSS só se AT$GPSP? disser que está desligado (AT$GPSP=1 apaga a memória do GNSS)."""
        query = self.send_at("AT$GPSP?")
        before = parse_gpsp(query)
        responses = [query]
        power_on: Optional[AtResponse] = None
        if before == 0:
            power_on = self.send_at("AT$GPSP=1", timeout=GPSP_ON_TIMEOUT_S)
            responses.append(power_on)
        return PrepareResult(
            gnss_power_before=before,
            gnss_power_on_sent=power_on is not None,
            gnss_power_on_final=power_on.final if power_on is not None else None,
            responses=responses,
        )

    # ------------------------------------------------------------------ leituras
    def read_serving_cell(self) -> MoniReading:
        return parse_moni(self.send_at("AT#MONI"))

    def read_serving_info(self) -> ServInfoReading:
        return parse_servinfo(self.send_at("AT#SERVINFO"))

    def scan_cells(self) -> CsurvReading:
        """#CSURV com prazo de 3 min, descarte do que chega depois do OK e intervalo mínimo
        entre varreduras (em campo, varreduras seguidas vieram vazias)."""
        if self._last_csurv_end is not None:
            wait = CSURV_MIN_INTERVAL_S - (time.monotonic() - self._last_csurv_end)
            if wait > 0:
                time.sleep(wait)
        try:
            resp = self.send_at("AT#CSURV", timeout=CSURV_TIMEOUT_S, drain_after=CSURV_DRAIN_S)
        finally:
            self._last_csurv_end = time.monotonic()
        return parse_csurv(resp)

    def read_gnss(self) -> GnssReading:
        return parse_gpsacp(self.send_at("AT$GPSACP"))
