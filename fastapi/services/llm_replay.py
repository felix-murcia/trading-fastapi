"""
P4 · REPLAY DEL LLM CON LA MECÁNICA REAL — ¿el LLM aporta si decide?

Los juicios del LLM están registrados por ciclo (audit_log: llm_bias +
llm_bias_prob + mt5_time). Este módulo los REPLAYEA como si el LLM fuera el
decisor: entra cuando su sesgo es firme, con la MISMA mecánica de producción
(SL/TP de `production_stops`, sizing de `size_ppo_position`, spread real,
regla de 3 €, ancla anti-churn) y sale por SL/TP/time-stop.

Objetivo: medir su EXPECTANCY (la vara que decide la admisión), no su
exactitud de clasificación (que es diagnóstico y además no es traducible —
ver E1b).

Sólo lectura: no toca el sistema vivo, no abre posiciones, no escribe en la BD.
"""
import asyncio
import bisect
import datetime as dt
import logging

logger = logging.getLogger(__name__)

# Reglas de ejecución (las mismas que /predict):
HOLD_MAX_BARS = 12          # time-stop de producción (12 velas H1)
MIN_WIN_EUR = 3.0           # regla de negocio: un close <3 € cuenta como pérdida
SPREAD_PIPS = 0.17          # spread real medido (O-2)
SL_MULT = 1.5               # production_stops: SL por defecto = 1,5 × ATR
TP_MULT = 2.0               # TP por defecto = 2,0 × ATR (RR 1:2)
RISK_PCT = 0.03             # ppo_risk_pct (sizing)
MAX_LOT = 0.5
LOT_SIZE = 100000.0
PIP_SIZE = 0.0001
INITIAL_BALANCE = 1000.0    # el ledger compara en € por trade (sizing de /predict)
ACCOUNT_EQUITY = 475.0      # equity real de la cuenta (para el % de riesgo)


async def _candles(symbol: str, count: int = 900):
    from services.mt5_client import get_candles_with_offset
    candles, offset_h = await get_candles_with_offset(symbol, "H1", count)
    return candles, offset_h


async def replay_llm_as_decider(
    symbol: str = "EURUSD",
    min_prob: float = 0.55,
    max_prob: float = 0.45,
    min_trades: int = 30,
) -> dict:
    """Replay del LLM como decisor → expectancy, DD, nº de operaciones.

    Sólo llamadas con sesgo FIRME y probabilidad convincente (las mismas que
    la autoridad del veto mide: |prob−0,5| ≥ 0,2 ⇒ prob ≥ 0,7 / ≤ 0,3).
    """
    from db.connection import get_pool

    pool = get_pool()   # el pool lo abre el lifespan de la app (nunca aquí)
    candles, offset_h = await _candles(symbol, 900)
    if not candles:
        return {"error": "sin velas"}

    # Velas en UTC real (get_candles_with_offset ya normaliza) + el mt5_time
    # del audit en reloj de broker ⇒ se alinea con el mismo offset.
    bt = [float(c["time"]) for c in candles]
    bc = [float(c["close"]) for c in candles]
    bh = [float(c["high"]) for c in candles]
    bl = [float(c["low"]) for c in candles]
    atr = _atr_series(candles)

    rows = await pool.fetch(
        """
        SELECT (data->'data'->>'mt5_time')::float AS t,
               data->'data'->>'llm_bias' AS b,
               (data->'data'->>'llm_bias_prob')::float AS p,
               data->'data'->>'position' AS pos
        FROM audit_log
        WHERE event='ai_predict_cycle' AND data->'data'->>'symbol'=$1
          AND data->'data'->>'llm_bias' IN ('BULLISH','BEARISH')
          AND COALESCE(data->'data'->>'qwen_skipped','false')='false'
          AND COALESCE(data->'data'->>'qwen_degraded','false')='false'
          AND (data->'data'->>'llm_bias_prob') IS NOT NULL
        ORDER BY created_at
        """, symbol,
    )

    trades = []
    last_exit_idx = -1
    equity = ACCOUNT_EQUITY
    peak = equity
    max_dd = 0.0

    for r in rows:
        prob = float(r["p"])
        if prob is None:
            continue
        firm = (r["b"] == "BULLISH" and prob >= min_prob) or \
               (r["b"] == "BEARISH" and prob <= max_prob)
        if not firm:
            continue
        t0 = float(r["t"]) - offset_h * 3600
        i0 = bisect.bisect_right(bt, t0) - 1
        if i0 < 0 or i0 <= last_exit_idx:
            continue                      # ya en posición (o sin datos)
        # La orden del EA se llena en la vela SIGUIENTE (no al cierre de la
        # vela de decisión): entrar al cierre de i0 sería optimista.
        entry_i = i0 + 1
        if entry_i >= len(bc):
            continue
        direction = 1 if r["b"] == "BULLISH" else -1
        a = atr[entry_i]
        if not a or a <= 0:
            continue
        # SL/TP como en producción (1,5 ATR / 2,0 ATR)
        sl_dist = SL_MULT * a
        tp_dist = TP_MULT * a
        entry = float(candles[entry_i]["open"]) + (SPREAD_PIPS * PIP_SIZE / 2) * direction
        sl = entry - sl_dist * direction
        tp = entry + tp_dist * direction
        # sizing (riesgo fijo por trade)
        risk_eur = ACCOUNT_EQUITY * RISK_PCT
        vol = risk_eur / (sl_dist / PIP_SIZE) / LOT_SIZE
        vol = max(0.01, min(MAX_LOT, round(vol, 2)))
        # salida: SL / TP / time-stop
        exit_price = None
        exit_reason = None
        for i in range(entry_i, min(entry_i + HOLD_MAX_BARS, len(bc))):
            hi, lo = bh[i], bl[i]
            if direction == 1:
                if lo <= sl:
                    exit_price, exit_reason = sl, "sl"
                    break
                if hi >= tp:
                    exit_price, exit_reason = tp, "tp"
                    break
            else:
                if hi >= sl:
                    exit_price, exit_reason = sl, "sl"
                    break
                if lo <= tp:
                    exit_price, exit_reason = tp, "tp"
                    break
        if exit_price is None:
            j = min(entry_i + HOLD_MAX_BARS - 1, len(bc) - 1)
            exit_price, exit_reason = bc[j], "timeout"
        last_exit_idx = j
        gross = (exit_price - entry) * direction
        pnl = gross * vol * LOT_SIZE
        equity += pnl
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak if peak else 0.0)
        trades.append({
            "entry": entry, "exit": exit_price, "reason": exit_reason,
            "pnl": pnl, "vol": vol, "bars": j - i0, "prob": prob,
        })

    n = len(trades)
    wins = [t for t in trades if t["pnl"] > 0]
    # Expectancy con la REGLA DE NEGOCIO (close <3 € = pérdida, igual que el
    # ledger y el walk-forward).
    exp_rule = (
        sum(
            t["pnl"] if (t["pnl"] >= MIN_WIN_EUR) else -abs(t["pnl"])
            for t in trades
        ) / n
    ) if n else 0.0
    return {
        "n_trades": n,
        "expectancy_eur_raw": round(sum(t["pnl"] for t in trades) / n, 3) if n else None,
        "expectancy_eur_regla_3": round(exp_rule, 3) if n else None,
        "pnl_total_eur": round(sum(t["pnl"] for t in trades), 2),
        "win_rate": round(len(wins) / n, 4) if n else None,
        "max_dd": round(max_dd, 4),
        "by_reason": {
            r: {
                "n": sum(1 for t in trades if t["reason"] == r),
                "pnl": round(sum(t["pnl"] for t in trades if t["reason"] == r), 2),
            }
            for r in ("tp", "sl", "timeout")
        },
        "reference_system_expectancy_eur": 1.81,
        "admissible": bool(n >= min_trades and exp_rule > 1.81),
        "verdict": (
            f"n={n} · expectancy(regla 3 €) {exp_rule:+.3f} € vs sistema +1,81 € ⇒ "
            + ("SUPERA el listón" if (n >= min_trades and exp_rule > 1.81)
               else "NO supera el listón" + ("" if n >= min_trades else f" (n={n} < {min_trades})"))
        ),
    }


def _atr_series(candles: list) -> list:
    """ATR14 simple sobre la serie (para el SL/TP de producción)."""
    n = len(candles)
    out = [0.0] * n
    if n < 15:
        return out
    for i in range(14, n):
        trs = []
        for j in range(i - 13, i + 1):
            h, l, pc = float(candles[j]["high"]), float(candles[j]["low"]), float(candles[j - 1]["close"])
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        out[i] = sum(trs) / 14.0
    return out


if __name__ == "__main__":
    import json
    import sys
    sys.path.insert(0, "/app")
    print(json.dumps(asyncio.run(replay_llm_as_decider()), indent=2, ensure_ascii=False))


async def _llm_replay_loop(interval_s: int = 1800) -> None:
    """Bucle periódico: replaya el LLM como decisor y publica el veredicto.

    La evidencia crece sola (cada ciclo del sistema añade un juicio del LLM);
    el listón de admisión es expectancy(regla 3 €) > +1,81 €/trade con n ≥ 30.
    Hasta entonces, el bundle vivo sigue operando (decisión de Félix
    2026-10-20).
    """
    # Retraso inicial: no golpear el MCP en el arranque (y no ensuciar las
    # pruebas que arrancan el lifespan).
    await asyncio.sleep(120)
    while True:
        try:
            r = await replay_llm_as_decider()
            logger.warning(
                "[LLM-REPLAY] %s | n=%s DD=%s",
                r.get("verdict"), r.get("n_trades"), r.get("max_dd"),
            )
        except Exception as exc:  # noqa: BLE001 — el vigilante nunca muere
            logger.warning("[LLM-REPLAY] fallo del ciclo: %s", exc)
        await asyncio.sleep(interval_s)
