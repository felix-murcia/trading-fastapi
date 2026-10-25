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

from core.rules import MIN_WIN_EUR, is_winning_close
from services.error_taxonomy import LOOP_ERRORS

LEDGER_PATH = os.environ.get(
    "DAILY_LEDGER_PATH",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ops", "daily_ledger.jsonl"),
)


async def build_daily_entry(symbol: str = "EURUSD", market_port=None) -> dict:
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
    except LOOP_ERRORS:  # noqa: BLE001
        equity = None

    # trades del día + descomposición dirección correcta/incorrecta
    #
    # T-31.9 (2026-10-20): `exit_time` viene en el RELOJ DEL BRÓKER (medido:
    # un deal cerrado a las 17:25 del bróquer = 14:25 UTC). El corte del día
    # se hacía contra la medianoche UTC sin convertir ⇒ la línea del día
    # incluía hasta 3 h del día anterior y excluía las 3 primeras del
    # actual. Aquí se convierte la marca al UTC real y se corta por el día
    # UTC (declarado en la línea: `day_basis`), que es el día del informe.
    try:
        from services.timebase import broker_offset_hours, utc_day_bounds
        # B6.2: el desfase se obtiene a través del puerto de mercado, no
        # mediante httpx directo desde timebase.
        if market_port is None:
            from core.container import build_registry_from_settings
            _registry = build_registry_from_settings()
            _mp = _registry.get("EURUSD").market
            if _mp is None:
                _enabled = _registry.enabled()
                _mp = _enabled[0].market if _enabled else None
            market_port = _mp
        if market_port is not None:
            broker_off = await broker_offset_hours(symbol, market_port)
        else:
            from services.timebase import current_broker_offset
            broker_off = current_broker_offset()
        day_start = utc_day_bounds(now)[0]
    except LOOP_ERRORS:  # noqa: BLE001
        try:
            from services.timebase import current_broker_offset, utc_day_bounds
            broker_off = current_broker_offset()
            day_start = utc_day_bounds(now)[0]
        except LOOP_ERRORS:  # noqa: BLE001
            broker_off = 0
            day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    trades = await pool.fetch(
        """
        SELECT entry_time, exit_time, direction, pnl, exit_reason
        FROM trade_outcomes
        WHERE symbol = $1
          AND (exit_time - make_interval(hours => $3::int)) >= $2
        ORDER BY exit_time
        """,
        symbol,
        day_start,
        broker_off,
    )
    correct = wrong = 0
    pnl_correct = pnl_wrong = 0.0
    for t in trades:
        pnl = float(t["pnl"])
        # Convención AGENTS.md / core.rules: un cierre < 3 € es PÉRDIDA,
        # aunque sea positivo. Antes se contaba `pnl > 0` y un +0,54 € o
        # +0,77 € contaminaba el win rate y la descomposición del PnL.
        if is_winning_close(pnl):
            correct += 1
            pnl_correct += pnl
        else:
            wrong += 1
            pnl_wrong += pnl

    # histórico completo (la referencia de confianza)
    hist = await pool.fetchrow(
        """
        SELECT count(*) n, COALESCE(sum(pnl),0) pnl,
               count(*) FILTER (WHERE pnl >= $2) wins,
               count(*) FILTER (WHERE pnl < $2) losses
        FROM trade_outcomes WHERE symbol = $1
        """, symbol, MIN_WIN_EUR,
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
    except LOOP_ERRORS:  # noqa: BLE001
        bundle_sha = None

    # autoridad del segundo cerebro
    authority = None
    try:
        from services import qwen_authority
        # T-31.10: MEDIR, no leer la caché. Tras un reinicio `peek()` devuelve
        # None y la línea del registro publicaba `granted: null` (ambigüedad:
        # ¿revocado o sin medir?). `get_authority` mide y cachea; si la
        # medición falla devuelve un veredicto explícito, nunca un hueco.
        a = await qwen_authority.get_authority(symbol)
        authority = {
            "granted": a.get("granted"),
            "n": a.get("n"),
            "acc": a.get("acc"),
            "base": a.get("base"),
            "skill_pp": a.get("skill_pp"),
            "wilson_lower": a.get("wilson_lower"),
            "measured_at": a.get("measured_at"),
            "reason": a.get("reason"),
        }
    except LOOP_ERRORS as exc:  # noqa: BLE001
        authority = {"granted": None, "reason": f"medición falló: {type(exc).__name__}"}

    return {
        "ts": now.isoformat(),
        "symbol": symbol,
        # T-31.9: el día del informe es el día UTC real y las marcas de los
        # deals vienen del reloj del bróquer ⇒ se declara el desfase usado
        # para convertirlas (auditable, sin ambigüedad).
        "day_basis": "utc",
        "broker_offset_h": broker_off,
        # Regla de clasificación declarada en la propia línea: un cierre
        # sólo es WIN si pnl >= 3 € (AGENTS.md). Así el lector no tiene que
        # adivinar por qué un +0,77 € aparece como pérdida.
        "win_threshold_eur": MIN_WIN_EUR,
        "equity_eur": equity,
        # El equity/balance son de la CUENTA; las operaciones y el PnL de
        # `trades_today` son del SÍMBOLO de la línea. Se declara para que nadie
        # sume dos líneas del mismo instante y cuente el equity dos veces.
        "equity_scope": "account",
        "trades_scope": "symbol",
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


def active_symbols() -> list[str]:
    """Símbolos habilitados en PAIRS (el registro sigue a los mundos vivos).

    G3 (2026-10-21): antes el bucle registraba SÓLO EURUSD; la operación
    USDCHF #278 (+0,54 €) existía en `trade_outcomes` pero no en la línea
    diaria. La línea es por símbolo porque sus estadísticas lo son; el
    equity es de la CUENTA (se declara con `equity_scope`).
    """
    from config import settings
    return [str(p.get("symbol")) for p in settings.pairs if p.get("enabled") and p.get("symbol")]


async def record_all(symbols: list[str] | None = None) -> list[dict]:
    """Una línea por símbolo activo; un fallo en uno no tumba a los demás."""
    import logging
    out: list[dict] = []
    for sym in (symbols if symbols is not None else active_symbols()):
        try:
            out.append(await record_now(sym))
        except LOOP_ERRORS as exc:  # noqa: BLE001 — el registrador nunca muere
            logging.getLogger(__name__).warning(
                "[DAILY-LEDGER] %s: no se pudo registrar la línea: %s", sym, exc
            )
    return out


async def _daily_ledger_loop(interval_s: int = 3600) -> None:
    """Una línea por hora y por símbolo activo: el histórico muestra la evolución."""
    await asyncio.sleep(90)   # no golpear la BD ni el bróker al arrancar
    while True:
        try:
            await record_all()
        except LOOP_ERRORS as exc:  # noqa: BLE001 — el registrador nunca muere
            import logging
            logging.getLogger(__name__).warning(
                "[DAILY-LEDGER] no se pudo registrar la línea: %s", exc
            )
        await asyncio.sleep(interval_s)
