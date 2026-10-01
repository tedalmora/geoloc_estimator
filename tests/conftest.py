import importlib
import time

import pytest

# Módulos cujo `time` é trocado pelo relógio virtual no fixture `sem_espera`.
MODULOS_COM_RELOGIO = ("hal.fn990_driver", "acquisition.snapshot", "coletor.daemon")


class RelogioVirtual:
    """Relógio de teste: sleep() avança o relógio na hora, sem esperar de verdade."""

    def __init__(self):
        self.avanco = 0.0

    def monotonic(self):
        return time.monotonic() + self.avanco

    def time(self):
        return time.time() + self.avanco

    def sleep(self, segundos):
        self.avanco += max(0.0, float(segundos))

    def __getattr__(self, nome):
        return getattr(time, nome)


@pytest.fixture
def sem_espera(monkeypatch):
    """Troca o relógio dos módulos de aquisição por um relógio virtual (pausas instantâneas,
    prazos ainda respeitados). Use com ScriptedSerial(clock=...) para leituras sem dado."""
    relogio = RelogioVirtual()
    for nome in MODULOS_COM_RELOGIO:
        monkeypatch.setattr(importlib.import_module(nome), "time", relogio)
    return relogio
