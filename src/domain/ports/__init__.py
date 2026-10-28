"""Puertos de dominio (Protocol, no ABC). Solo firmas, sin infraestructura."""

from typing import Protocol


class TradeRepository(Protocol):
    def save(self, trade) -> None: ...
    def find_by_id(self, id: str): ...


class AuditLogger(Protocol):
    def log(self, event: str, details: dict) -> None: ...


class LLMClient(Protocol):
    def veto(self, signal: dict) -> bool: ...
    def classify_regime(self, data: dict) -> str: ...


class BrokerGateway(Protocol):
    def execute(self, order: dict) -> dict: ...
    def get_price(self, symbol: str) -> float: ...


class Clock(Protocol):
    def now(self) -> object: ...


class ConfigProvider(Protocol):
    def get(self, key: str, default=None): ...
