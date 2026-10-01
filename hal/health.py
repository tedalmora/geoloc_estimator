"""Monitor de saúde da conexão com o modem: conta falhas seguidas e indica quando tentar
reconectar. Usado pelo Coletor no modo contínuo; o live.py faz uma única leitura e não o usa.
"""
from dataclasses import dataclass, field
from typing import Dict


@dataclass
class HealthMonitor:
    max_failures_before_reconnect: int = 3
    _failures: Dict[str, int] = field(default_factory=dict)

    def record(self, modem_id: str, alive: bool) -> bool:
        """Registra 1 resultado de health check. Devolve True quando já é a Nª (ou mais)
        falha seguida daquele modem -- sinal de que vale tentar reconectar."""
        if alive:
            self._failures[modem_id] = 0
            return False
        self._failures[modem_id] = self._failures.get(modem_id, 0) + 1
        return self._failures[modem_id] >= self.max_failures_before_reconnect

    def reset(self, modem_id: str) -> None:
        self._failures[modem_id] = 0
