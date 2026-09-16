"""Funções utilitárias de parsing de respostas AT -- compartilhadas entre drivers de modem."""
import re
from typing import Optional


def strip_echo(raw: str) -> str:
    """Remove o eco do comando e o 'OK'/'ERROR' final, sobrando a linha de dado útil.

    Necessário porque, por padrão (ATE1), o modem repete o comando enviado na própria resposta.
    """
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    lines = [line for line in lines if line not in ("OK", "ERROR") and not line.upper().startswith("AT")]
    return lines[0] if lines else raw.strip()


def extract_float(raw: str, pattern: str) -> Optional[float]:
    match = re.search(pattern, raw)
    return float(match.group(1)) if match else None


def extract_int(raw: str, pattern: str) -> Optional[int]:
    match = re.search(pattern, raw)
    return int(match.group(1)) if match else None


def extract_str(raw: str, pattern: str) -> Optional[str]:
    match = re.search(pattern, raw)
    return match.group(1) if match else None


def nmea_to_decimal(value: str) -> Optional[float]:
    """Converte um campo de coordenada do $GPSACP (ex.: '4546.6334N', '00805.5885E') para grau
    decimal. Formato Telit: graus (2 dígitos p/ latitude, 3 p/ longitude) + minutos decimais,
    seguido da letra de hemisfério (N/S/E/W).
    """
    value = value.strip()
    if not value:
        return None
    hemisphere = value[-1].upper()
    number = value[:-1]
    dot = number.index(".")
    deg_len = dot - 2
    degrees = float(number[:deg_len])
    minutes = float(number[deg_len:])
    decimal = degrees + minutes / 60.0
    if hemisphere in ("S", "W"):
        decimal = -decimal
    return decimal
