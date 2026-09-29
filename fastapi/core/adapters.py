"""Adapters de INFRASTRUCTURE — delegación con LATE BINDING (P3-1a).

Regla de oro de este módulo: **nunca** `from services.mt5_client import
get_candles` a nivel de módulo. Ese enlace temprano congelaría la función
original en el namespace del adapter y rompería
`patch("services.mt5_client.get_candles")`: la suite parchea los módulos
`services.*`, por eso cada método importa el módulo servicio y accede al
atributo en el instante de la LLAMADA — el patch se ve siempre.

Instancias sin estado: el container crea un juego de adapters por
`PairContext`, de modo que cada par es un mundo aislado a coste cero
(intercambiar un adapter por otro no puede afectar a otro par).
"""
import logging
from typing import Optional


class MT5MarketDataAdapter:
    """`MarketDataPort` sobre `services.mt5_client`."""

    async def get_account(self) -> dict:
        from services import mt5_client
        return await mt5_client.get_account()

    async def get_prices(self) -> dict:
        from services import mt5_client
        return await mt5_client.get_prices()

    async def get_spread(self, symbol: str) -> float:
        from services import mt5_client
        return await mt5_client.get_spread(symbol)

    async def get_candles(self, symbol: str, timeframe: str, count: int) -> list[dict]:
        from services import mt5_client
        return await mt5_client.get_candles(symbol, timeframe, count)

    async def get_positions(self) -> dict:
        from services import mt5_client
        return await mt5_client.get_positions()

    async def get_candle_open(self, symbol: str, timeframe: str = "H1") -> float:
        from services import mt5_client
        return await mt5_client.get_candle_open(symbol, timeframe)


class MT5ExecutionDataAdapter:
    """`ExecutionPort` sobre `services.mt5_client`.

    `place_order_with_fallback` fue eliminado en P3-1d (código muerto con
    cero llamantes, junto a `correlation_sizing.py`): un puerto no expone
    funciones que el sistema no usa.

    P2-4b: `close_positions_by_symbol` (borrar TODO el símbolo) dio paso a
    `close_position(ticket)` — el sistema solo cierra por ticket vía el
    ledger de propiedad; cerrar el símbolo entero tocaba posiciones ajenas.
    """

    async def place_order(self, symbol: str, order_type: str, volume: float,
                          price: float, sl: float, tp: float,
                          comment: str = "") -> dict:
        from services import mt5_client
        return await mt5_client.place_order(symbol, order_type, volume, price, sl, tp, comment)

    async def close_position(self, ticket: int) -> dict:
        from services import mt5_client
        return await mt5_client.close_position(ticket)


class AiModelStoreAdapter:
    """`ModelStorePort` sobre `routers.ai` (el loader de inferencia vive ahí).

    Late binding como el resto: `patch("routers.ai.reload_inference_artifacts")`
    —el del test de promoción de retrain— se ve siempre, porque el atributo
    se resuelve en el momento de la llamada.
    """

    def load(self, symbol: str) -> None:
        from routers import ai
        ai.load_inference_artifacts(symbol)

    def reload(self, symbol: str) -> dict:
        from routers import ai
        return ai.reload_inference_artifacts(symbol)


class QwenLLMAdapter:
    """`LLMPort` sobre `services.qwen_client`."""

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
    ) -> dict:
        from services import qwen_client
        return await qwen_client.query_qwen_unified(
            symbol=symbol,
            decision=decision,
            ml_prob=ml_prob,
            rsi=rsi,
            atr=atr,
            atr_pct=atr_pct,
            macd_hist=macd_hist,
            bb_pos=bb_pos,
            range_pct=range_pct,
            last_ret=last_ret,
            hour=hour,
            news=news,
            candle_context=candle_context,
            sl_proposed=sl_proposed,
            tp_proposed=tp_proposed,
            cycle_id=cycle_id,
            logger=logger,
            recent_insights=recent_insights,
        )
