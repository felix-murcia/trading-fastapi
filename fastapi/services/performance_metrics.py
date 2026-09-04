"""
Métricas de rendimiento en tiempo real.
Acumula estadísticas por sesión y las expone para monitoreo.
"""

import time
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class CycleMetrics:
    symbol: str = ""
    decision: str = "HOLD"
    ml_prob: float = 0.5
    llm_bias: str = "NEUTRAL"
    latency_ms: float = 0.0
    mt5_available: bool = True
    order_placed: bool = False
    order_deferred: bool = False
    order_ticket: Optional[int] = None
    error: Optional[str] = None
    ts: float = field(default_factory=time.time)


class PerformanceMetrics:
    """
    Acumula métricas por sesión activa.
    Se resetea en cada start/restart del servidor.
    """

    def __init__(self):
        self._cycles: list[CycleMetrics] = []
        self._symbol_stats: dict[str, dict] = defaultdict(lambda: {
            "total": 0, "buy": 0, "sell": 0, "hold": 0,
            "errors": 0, "deferred": 0, "latency_sum": 0.0
        })
        self._order_stats: dict[str, int] = defaultdict(int)
        # 0=FLAT, 1=LONG, 2=SHORT
        self._position_outcome: dict[int, dict] = {
            0: {"wins": 0, "losses": 0, "pending": 0},
            1: {"wins": 0, "losses": 0, "pending": 0},
            2: {"wins": 0, "losses": 0, "pending": 0},
        }
        self._start_ts = time.time()
        self._last_cycle_ts: float = 0

    def record_cycle(self, m: CycleMetrics):
        self._cycles.append(m)
        self._last_cycle_ts = m.ts
        sym = m.symbol or "UNKNOWN"
        s = self._symbol_stats[sym]
        s["total"] += 1
        if m.decision == "BUY":
            s["buy"] += 1
        elif m.decision == "SELL":
            s["sell"] += 1
        elif m.decision == "HOLD":
            s["hold"] += 1
        if m.error:
            s["errors"] += 1
        if m.order_deferred:
            s["deferred"] += 1
        s["latency_sum"] += m.latency_ms
        if m.order_placed:
            self._order_stats["placed"] += 1
        if m.error:
            self._order_stats["errors"] += 1

    def record_trade_result(self, action: int, won: bool):
        """Registra resultado de una operación (llamado desde order_manager)."""
        if action in self._position_outcome:
            if won:
                self._position_outcome[action]["wins"] += 1
            else:
                self._position_outcome[action]["losses"] += 1

    def summary(self) -> dict:
        uptime_s = time.time() - self._start_ts
        total = len(self._cycles)
        errors = sum(1 for c in self._cycles if c.error)
        deferred = sum(1 for c in self._cycles if c.order_deferred)
        placed = sum(1 for c in self._cycles if c.order_placed)
        avg_latency = sum(c.latency_ms for c in self._cycles) / total if total else 0
        hold_pct = sum(1 for c in self._cycles if c.decision == "HOLD") / total * 100 if total else 0

        action_wr = {}
        for action, outcomes in self._position_outcome.items():
            total_trades = outcomes["wins"] + outcomes["losses"]
            winrate = outcomes["wins"] / total_trades * 100 if total_trades else 0
            action_wr[action] = {
                "wins": outcomes["wins"],
                "losses": outcomes["losses"],
                "winrate_pct": round(winrate, 1),
                "pending": outcomes["pending"],
            }

        return {
            "uptime_seconds": round(uptime_s, 1),
            "total_cycles": total,
            "cycles_last_hour": sum(1 for c in self._cycles if (time.time() - c.ts) < 3600),
            "avg_latency_ms": round(avg_latency, 1),
            "hold_pct": round(hold_pct, 1),
            "orders_placed": placed,
            "orders_deferred": deferred,
            "errors": errors,
            "by_symbol": dict(self._symbol_stats),
            "action_winrates": action_wr,
        }

    def reset(self):
        self.__init__()


# Instancia global
_metrics = PerformanceMetrics()


def record_cycle_metrics(
    symbol: str, decision: str, ml_prob: float, llm_bias: str,
    latency_ms: float, mt5_available: bool,
    order_placed: bool = False, order_deferred: bool = False,
    order_ticket: Optional[int] = None, error: Optional[str] = None,
):
    m = CycleMetrics(
        symbol=symbol, decision=decision, ml_prob=ml_prob, llm_bias=llm_bias,
        latency_ms=latency_ms, mt5_available=mt5_available,
        order_placed=order_placed, order_deferred=order_deferred,
        order_ticket=order_ticket, error=error,
    )
    _metrics.record_cycle(m)


def get_metrics() -> dict:
    return _metrics.summary()


def reset_metrics():
    _metrics.reset()
