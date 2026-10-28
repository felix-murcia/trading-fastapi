"""Reglas de parseo puro (sin infraestructura).

Migrado de fastapi/services/auto_retrain.py:
- _validate_symbol (líneas 57-73): símbolo debe estar en PAIRS listados.
- _parse_bool (líneas 163-171): parseo de bool desde jsonb.
- _parse_timestamp (líneas 294-298): parseo de timestamp Unix.
"""


class UnknownSymbol(ValueError):
    """El símbolo no está listado en PAIRS (sin pipeline de retrain)."""


def validate_symbol(symbol: str, listed_symbols: set[str]) -> str:
    """`symbol` debe estar listado en PAIRS para tener pipeline de retrain.

    Listado basta (habilitado o NO). Símbolo desconocido -> ValueError explícito.
    """
    if symbol not in listed_symbols:
        raise UnknownSymbol(
            f"símbolo {symbol!r} no listado en PAIRS — sin pipeline de retrain"
        )
    return symbol


def parse_bool(value, default: bool = False) -> bool:
    """jsonb puede devolver bool, número o string según el escritor."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return bool(value)


def parse_timestamp(ts: float | str) -> float:
    """Acepta Unix timestamp (float o string numérico) o ISO string, devuelve Unix timestamp (float)."""
    if isinstance(ts, str):
        return float(ts)
    return float(ts)
