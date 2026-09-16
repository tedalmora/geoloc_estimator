"""Contrato comum (interface) que toda implementação de driver de modem deve seguir.

Importante: esta classe é só uma BIBLIOTECA -- sem loop, sem estado de "sessão", sem banco
de dados. Quem decide quando chamar e o que fazer com o resultado é quem usa o HAL (o
Coletor). Ver planejamento/arquitetura_hal.md para a justificativa completa dessa separação.
"""
from abc import ABC, abstractmethod
from typing import List, Optional

from .models import GnssFix, LppReport, ModemStatus, NeighborCell, ServingCell, SignalMetrics


class ModemDriver(ABC):
    @abstractmethod
    def connect(self, port: str, baudrate: int = 115200, timeout: float = 5.0) -> None:
        ...

    @abstractmethod
    def close(self) -> None:
        ...

    @abstractmethod
    def send_at(self, command: str, timeout: float = 5.0) -> str:
        """Envia um comando AT cru e devolve a resposta bruta (texto), sem parsing."""
        ...

    @abstractmethod
    def is_alive(self) -> bool:
        """Teste mais barato possível de que o modem ainda responde -- usado no health check."""
        ...

    @abstractmethod
    def get_status(self) -> ModemStatus:
        ...

    @abstractmethod
    def get_signal_metrics(self) -> SignalMetrics:
        ...

    @abstractmethod
    def get_serving_cell(self) -> ServingCell:
        ...

    @abstractmethod
    def get_neighbor_cells(self) -> List[NeighborCell]:
        ...

    @abstractmethod
    def get_gnss_fix(self) -> Optional[GnssFix]:
        ...

    @abstractmethod
    def reboot(self) -> None:
        ...

    def reconnect(self) -> None:
        """Implementação padrão: fecha e reabre com os parâmetros salvos por connect().
        Requer que a subclasse guarde _port/_baudrate/_timeout dentro do próprio connect()
        (o FN990Driver já faz isso)."""
        self.close()
        self.connect(self._port, baudrate=self._baudrate, timeout=self._timeout)  # type: ignore[attr-defined]

    def enable_lpp_positioning(self) -> str:
        """Extensão experimental (DL-AoD/LPP) -- ver arquitetura_hal.md, Seção 3.
        A implementação real depende de validar em campo com rede/core disponíveis."""
        raise NotImplementedError("Suporte a LPP é experimental -- implementar/validar em campo")

    def get_lpp_report(self) -> LppReport:
        """Lê o relatório mais recente de uma sessão LPP habilitada por enable_lpp_positioning().
        Serve de base para o método AoA/DL-AoD do Estimador (estimador/methods/aoa.py) --
        extensão experimental, mesma ressalva de enable_lpp_positioning()."""
        raise NotImplementedError("Suporte a LPP é experimental -- implementar/validar em campo")
