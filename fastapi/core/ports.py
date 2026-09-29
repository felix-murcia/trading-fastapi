"""Puertos (interfaces) del núcleo hexagonal — P3-1a.

Contratos entre APPLICATION e INFRASTRUCTURE: el dominio depende de estas
Protocol-clases, nunca de los servicios concretos. Los adapters reales
(`core.adapters`) implementan delegando con late binding, de modo que los
puntos de patch de la suite (`patch("services.mt5_client.get_candles")`,
`patch("services.qwen_client.query_qwen_unified")`, …) siguen operando.

Declarados en P3-1a: mercado, ejecución y LLM. `ModelStorePort` entra en
P3-1b junto con la carga de modelos por par (hoy el loader es único y sin
path — un puerto con un parámetro que el loader ignora sería un puerto que
miente; se declara cuando exista quien lo implemente honradamente).
"""
import logging
from typing import Optional, Protocol, runtime_checkable


@runtime_checkable
class MarketDataPort(Protocol):
    """Datos de mercado y estado de cuenta (adapter: services.mt5_client)."""

    async def get_account(self) -> dict: ...
    async def get_prices(self) -> dict: ...
    async def get_spread(self, symbol: str) -> float: ...
    async def get_candles(self, symbol: str, timeframe: str, count: int) -> list[dict]: ...
    async def get_positions(self) -> dict: ...
    async def get_candle_open(self, symbol: str, timeframe: str = "H1") -> float: ...


@runtime_checkable
class ExecutionPort(Protocol):
    """Ejecución de órdenes y cierres (adapter: services.mt5_client)."""

    async def place_order(self, symbol: str, order_type: str, volume: float,
                          price: float, sl: float, tp: float,
                          comment: str = "") -> dict: ...
    async def close_positions_by_symbol(self, symbol: str) -> dict: ...


@runtime_checkable
class LLMPort(Protocol):
    """Veto/calidad del LLM (adapter: services.qwen_client).

    Firma idéntica a `query_qwen_unified`: el puerto refleja el contrato
    real, no una idealización.
    """

    async def query_unified(
        self,
        symbol: str,
        decision: str,
        ml_prob: float,
        rsi: float,
        atr: float,
        atr_pct: float,
        macd_hist: float,
        bb_pos: float,
        range_pct: float,
        last_ret: float,
        hour: int,
        news: str,
        candle_context: str,
        sl_proposed: float,
        tp_proposed: float,
        cycle_id: str,
        logger: logging.Logger,
        recent_insights: Optional[list[dict]] = None,
    ) -> dict: ...
