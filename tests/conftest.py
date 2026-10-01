import time
import types

import pytest


@pytest.fixture
def sem_espera(monkeypatch):
    """Remove as pausas do driver (sleep) sem alterar o relógio usado nos prazos."""
    import hal.fn990_driver as fd

    fake = types.SimpleNamespace(time=time.time, monotonic=time.monotonic, sleep=lambda s: None)
    monkeypatch.setattr(fd, "time", fake)
    return fake
