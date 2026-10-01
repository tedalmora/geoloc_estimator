"""Apoio aos testes: carregamento de fixtures AT rotuladas e portas seriais falsas.

As respostas em tests/fixtures/at/ são sintéticas ou fragmentos de campo identificados no
cabeçalho de cada arquivo. Elas existem só para testes automatizados e não são resultados
experimentais.
"""
import os
import time

FIXTURES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "at")
REQUIRED_HEADER_KEYS = ("origem", "confirmado_em_campo", "uso")


def read_fixture(name):
    """Devolve (metadados, linhas da resposta). O cabeçalho termina na linha '---'."""
    with open(os.path.join(FIXTURES_DIR, name), encoding="utf-8") as f:
        content = f.read()
    header, sep, body = content.partition("\n---\n")
    if not sep:
        raise ValueError(f"fixture sem cabeçalho de origem: {name}")
    meta = {}
    for line in header.splitlines():
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip()
    return meta, body.splitlines()


def at_lines(name):
    return read_fixture(name)[1]


class ScriptedSerial:
    """pyserial falso: cada comando escrito enfileira as linhas roteirizadas, terminadas em CRLF.

    `responses` mapeia o comando (sem CR/LF) para uma lista de linhas ou para uma função sem
    argumentos que devolve a lista (útil para respostas que mudam a cada chamada). Comando
    desconhecido recebe eco + ERROR, como o modem faz com comando não suportado.
    `pending` simula bytes que já estavam no buffer antes do primeiro comando (URCs, sobras).
    """

    def __init__(self, responses=None, pending=None, timeout=5.0):
        self.responses = {k.upper(): v for k, v in (responses or {}).items()}
        self.buffer = bytearray()
        if pending:
            self.buffer.extend("".join(line + "\r\n" for line in pending).encode())
        self.timeout = timeout
        self.is_open = True
        self.written = []

    @property
    def in_waiting(self):
        return len(self.buffer)

    def reset_input_buffer(self):
        self.buffer.clear()

    def write(self, data):
        command = data.decode("ascii", "ignore").strip()
        self.written.append(command)
        lines = self.responses.get(command.upper())
        if lines is None:
            lines = [command, "ERROR"]
        elif callable(lines):
            lines = lines()
        self.buffer.extend("".join(line + "\r\n" for line in lines).encode())
        return len(data)

    def readline(self):
        idx = self.buffer.find(b"\n")
        end = len(self.buffer) if idx < 0 else idx + 1
        data = bytes(self.buffer[:end])
        del self.buffer[:end]
        return data

    def read(self, size=1):
        data = bytes(self.buffer[:size])
        del self.buffer[:size]
        return data

    def flush(self):
        pass

    def close(self):
        self.is_open = False


class BlockingSerial:
    """Emula o pyserial quando nada chega: read/readline bloqueiam pelo timeout configurado."""

    def __init__(self, *args, timeout=5.0, **kwargs):
        self.timeout = timeout
        self.is_open = True
        self.written = []

    in_waiting = 0

    def reset_input_buffer(self):
        pass

    def write(self, data):
        self.written.append(data.decode("ascii", "ignore").strip())
        return len(data)

    def _block(self):
        time.sleep(self.timeout or 0)
        return b""

    def readline(self):
        return self._block()

    def read(self, size=1):
        return self._block()

    def flush(self):
        pass

    def close(self):
        self.is_open = False


class SilentSerial(BlockingSerial):
    """Porta aberta que nunca responde e não bloqueia (para testar desistência por prazo)."""

    def _block(self):
        return b""
