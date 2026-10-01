"""Toda fixture AT precisa declarar origem, se foi confirmada em campo e o uso permitido."""
import os

import pytest

from support import FIXTURES_DIR, REQUIRED_HEADER_KEYS, read_fixture

FIXTURES = sorted(f for f in os.listdir(FIXTURES_DIR) if f.endswith(".txt"))


def test_existem_fixtures():
    assert FIXTURES


@pytest.mark.parametrize("nome", FIXTURES)
def test_fixture_tem_cabecalho_de_origem(nome):
    meta, _ = read_fixture(nome)
    assert [k for k in REQUIRED_HEADER_KEYS if not meta.get(k)] == []
    assert "não é resultado experimental" in meta["uso"]
