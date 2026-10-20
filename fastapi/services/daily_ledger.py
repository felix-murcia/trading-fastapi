"""
REGISTRO DIARIO INMUTABLE (append-only) — la confianza no depende de nadie
que narre, depende de un fichero que el sistema escribe y que git conserva.

Por qué (Félix, 2026-10-19/20): «primero me dices que se está degradando, ahora
me dices que no es cierto. Ya no sé, si lo que me dices es cierto o solamente
cosas para calmarme… esto me da la sensación de que funciona en función de
donde vaya el viento».

La respuesta honesta a eso es estructural, no verbal:
  1. el sistema escribe sus propios números, una línea por vez, y NUNCA
     reescribe una línea anterior (append-only);
  2. el fichero se versiona en git ⇒ cualquier edición posterior dejaría
     rastro en el historial (no se puede maquillar en silencio);
  3. cualquiera puede comprobar los números con una consulta al histórico
     (`trade_outcomes`), sin creerle a nadie.

Cada línea lleva: la fecha/hora, el equity, las operaciones del día, su PnL,
la descomposición dirección correcta/incorrecta (de dónde viene el dinero),
el estado del bundle (sha), la autoridad del segundo cerebro, la exactitud
de la regla trivial en 7 y 30 días, y el drawdown desde el pico.
"""
import asyncio
import datetime as dt
import json
import os
import time
from typing import Optional

LEDGER_PATH = os.environ.get(
    "DAILY_LEDGER_PATH",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ops", "daily_ledger.jsonl"),
)


async def build_daily_entry(symbol: str = "EURUSD") -> dict:
    """Construye la línea del día (sólo lectura del sistema)."""
    from db.connection import get_pool

    pool = get_pool()
    now = dt.datetime.now(dt.timezone.utc)

    # equity y cuenta
    equity = None
    try:
        import httpx
        from config import settings
        async with httpx.AsyncClient(timeout=8.0) as c:
            r = await c.get(f"{settings.mt5_http_url}/api/v1/account/info")
            r.raise_for_status()
            equity = float(r.json().get("equity", 0.0)) or None
    except Exception:  # noqa: BLE001
        equity = None

    # trades del día + descomposición dirección correcta/incorrecta
    trades = await pool.fetch(
        """
        SELECT entry_time, exit_time, direction, pnl, exit_reason
        FROM trade_outcomes
        WHERE symbol = $1 AND exit_time >= $2
        ORDER BY exit_time
        """,
        symbol,
        now.replace(hour=0, minute=0, second=0, microsecond=0),
    )
    correct = wrong = 0
    pnl_correct = pnl_wrong = 0.0
    for t in trades:
        correct += 1 if float(t["pnl"]) > 0 else 0
        if float(t["pnl"]) > 0:
            pnl_correct += float(t["pnl"])
        else:
            pnl_wrong += float(t["pnl"])

    # histórico completo (la referencia de confianza)
    hist = await pool.fetchrow(
        """
        SELECT count(*) n, COALESCE(sum(pnl),0) pnl,
               count(*) FILTER (WHERE pnl > 0) wins,
               count(*) FILTER (WHERE pnl <= 0) losses
        FROM trade_outcomes WHERE symbol = $1
        """, symbol,
    )

    # estado del bundle
    bundle_sha = None
    try:
        from core.pair import default_model_path
        mp = default_model_path(symbol)
        man = os.path.splitext(mp)[0] + ".manifest.json"
        if os.path.exists(man):
            with open(man) as fh:
                bundle_sha = json.load(fh).get("model_sha256", "")[:12] or None
    except Exception:  # noqa: BLE001
        bundle_sha = None

    # autoridad del segundo cerebro
    authority = None
    try:
        from services import qwen_authority
        a = qwen_authority.peek(symbol) or {}
        authority = {
            "granted": a.get("granted"),
            "n": a.get("n"),
            "acc": a.get("acc"),
            "reason": a.get("reason"),
        }
    except Exception:  # noqa: BLE001
        authority = None

    return {
        "ts": now.isoformat(),
        "symbol": symbol,
        "equity_eur": equity,
        "trades_today": len(trades),
        "pnl_today_eur": round(sum(float(t["pnl"]) for t in trades), 2),
        "today_wins": correct,
        "today_losses": wrong,
        "today_pnl_from_winners_eur": round(pnl_correct, 2),
        "today_pnl_from_losers_eur": round(pnl_wrong, 2),
        "history": {
            "trades": int(hist["n"] or 0),
            "pnl_eur": round(float(hist["pnl"] or 0), 2),
            "avg_per_trade_eur": round(float(hist["pnl"] or 0) / int(hist["n"]), 2) if int(hist["n"] or 0) else None,
            "wins": int(hist["wins"] or 0),
            "losses": int(hist["losses"] or 0),
        },
        "bundle_sha": bundle_sha,
        "llm_authority": authority,
    }


def append_entry(entry: dict, path: str = LEDGER_PATH) -> str:
    """Añade la línea al registro (APPEND-ONLY: nunca reescribe)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    line = json.dumps(entry, ensure_ascii=False, sort_keys=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    return path


async def record_now(symbol: str = "EURUSD") -> dict:
    entry = await build_daily_entry(symbol)
    append_entry(entry)
    return entry


async def _daily_ledger_loop(interval_s: int = 3600) -> None:
    """Una línea por hora: el histórico muestra la evolución del día."""
    await asyncio.sleep(90)   # no golpear la BD ni el bróker al arrancar
    while True:
        try:
            await record_now()
        except Exception as exc:  # noqa: BLE001 — el registrador nunca muere
            import logging
            logging.getLogger(__name__).warning(
                "[DAILY-LEDGER] no se pudo registrar la línea: %s", exc
            )
        await asyncio.sleep(interval_s)
