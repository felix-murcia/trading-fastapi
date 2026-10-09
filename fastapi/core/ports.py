"""Puertos (interfaces) del núcleo hexagonal — P3-1a.

Contratos entre APPLICATION e INFRASTRUCTURE: el dominio depende de estas
Protocol-clases, nunca de los servicios concretos. Los adapters reales
(`core.adapters`) implementan delegando con late binding, de modo que los
puntos de patch de la suite (`patch("services.mt5_client.get_candles")`,
`patch("services.qwen_client.query_qwen_unified")`, …) siguen operando.

Declarados: mercado, ejecución y LLM (P3-1a); almacén de modelos
(`ModelStorePort`, P3-1b) — entró cuando el loader dejó de ser único y
aceptó el símbolo del par, de modo que el protocolo refleja lo que el
código hace de verdad.
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
    async def close_position(self, ticket: int) -> dict: ...


@runtime_checkable
class ModelStorePort(Protocol):
    """Bundles de modelo por par (adapter: `routers.ai` vía `AiModelStoreAdapter`).

    `load` certifica y carga en memoria el bundle del par; `reload` hace el
    swap atómico tras una promoción (P0-5/P1-2) y devuelve la procedencia
    del bundle resultante.
    """

    def load(self, symbol: str) -> None: ...
    def reload(self, symbol: str) -> dict: ...


@runtime_checkable
class LLMPort(Protocol):
    """Veto/calidad del LLM (adapter: services.qwen_client).

    Firma idéntica a `query_qwen_unified` (paridad verificada por
    `inspect.signature` en la suite): el puerto refleja el contrato real,
    no una idealización. Sin campos de SL/TP (paquete 1-4: murieron con la
    Opción 1 — production_stops es la única fuente de stops).
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
        cycle_id: str,
        logger: logging.Logger,
        recent_insights: Optional[list[dict]] = None,
        adx: Optional[float] = None,
        sma200_dist: Optional[float] = None,
        sma200_slope: Optional[float] = None,
        regime: Optional[str] = None,
        support_level: Optional[float] = None,
        resistance_level: Optional[float] = None,
        reentry: Optional[dict] = None,
        calendar_events: Optional[list[dict]] = None,
    ) -> dict: ...
