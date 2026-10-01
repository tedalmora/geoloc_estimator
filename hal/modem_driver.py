"""Contrato comum (interface) que toda implementação de driver de modem deve seguir.

O HAL é só uma BIBLIOTECA: sem loop, sem sessão, sem banco de dados. Quem decide quando chamar
e o que fazer com o resultado é a aquisição compartilhada (Coletor e medição ao vivo). Cada
método de leitura executa só o próprio comando e devolve a leitura com status; o cruzamento
entre comandos (ex.: PCI da servidora) é feito por funções puras em hal/parsing.py.
"""
from abc import ABC, abstractmethod
from typing import Optional

from .models import (
    AtResponse,
    CsurvReading,
    GnssReading,
    ModemStatus,
    MoniReading,
    PrepareResult,
    ServInfoReading,
)


class ModemDriver(ABC):
    @abstractmethod
    def connect(self, port: str, baudrate: int = 115200, timeout: float = 5.0) -> None:
        """Abre a porta e confirma que o modem responde; levanta ConnectionError se não responder."""

    @abstractmethod
    def close(self) -> None:
        ...

    @abstractmethod
    def send_at(self, command: str, timeout: Optional[float] = None, drain_after: float = 0.0) -> AtResponse:
        """Envia 1 comando AT e devolve a resposta com código final, respeitando o prazo total."""

    @abstractmethod
    def is_alive(self) -> bool:
        """Teste mais barato possível de que o modem ainda responde (health check)."""

    @abstractmethod
    def get_status(self) -> ModemStatus:
        ...

    @abstractmethod
    def reboot(self) -> None:
        ...

    @abstractmethod
    def prepare_for_collection(self) -> PrepareResult:
        """Deixa o modem pronto para coletar sem gravar configuração persistente."""

    @abstractmethod
    def read_serving_cell(self) -> MoniReading:
        ...

    @abstractmethod
    def read_serving_info(self) -> ServInfoReading:
        ...

    @abstractmethod
    def scan_cells(self) -> CsurvReading:
        ...

    @abstractmethod
    def read_gnss(self) -> GnssReading:
        ...

    def reconnect(self) -> None:
        """Fecha e reabre com os parâmetros guardados por connect() (_port/_baudrate/_timeout)."""
        self.close()
        self.connect(self._port, baudrate=self._baudrate, timeout=self._timeout)  # type: ignore[attr-defined]
