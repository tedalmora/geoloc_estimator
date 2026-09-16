"""Monitor de saúde de uma conexão com modem: acompanha falhas consecutivas e decide quando
vale a pena tentar reconectar. Vive aqui (não em coletor/) porque "o modem ainda responde?"
é uma pergunta sobre a CONEXÃO (assunto do HAL), não sobre coleta de dados -- por isso é
reaproveitado tanto pelo Coletor (campanha de campo) quanto pelo estimador/live.py (posição
ao vivo): os dois rodam como daemon, cada um com sua própria conexão, mas com a mesma lógica
de "quantas falhas seguidas até tentar reconectar".
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
