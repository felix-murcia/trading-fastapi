"""Excepciones de dominio. Heredan de DomainError."""

from decimal import Decimal


class DomainError(Exception):
    """Excepción base del dominio puro."""
    pass


class InvalidTradeError(DomainError):
    def __init__(self, reason: str):
        super().__init__(f"Invalid trade: {reason}")


class InvalidPnLError(DomainError):
    def __init__(self, reason: str):
        super().__init__(f"Invalid PnL: {reason}")


class ThresholdViolationError(DomainError):
    def __init__(self, threshold_name: str, expected: Decimal, actual: Decimal):
        super().__init__(
            f"Threshold violation: {threshold_name} expected >= {expected}, got {actual}"
        )


class UnknownSymbol(DomainError):
    """Símbolo no listado en PAIRS (sin pipeline de retrain)."""
    pass