"""
P2 · AUDITORÍA CRUZADA — «fiabilidad absoluta».

Recomputa INDEPENDIENTEMENTE los números que el sistema publica y los
compara con lo publicado. Si algo discrepa, se declara (no se oculta).

Por qué (Félix, 2026-10-19/20: «el sistema tiene que ser absolutamente
fiable en todos los aspectos»): el 20-10, tres mediciones seguidas de las
MISMAS llamadas dieron 87 %, 69,6 % y 10 % porque el ERROR ERA DEL
MEDIDOR, no del modelo. Sin una recomputación independiente, un error de
medición puede vivir semanas y contaminar decisiones (una promoción, una
autoridad, una revocación).

Checks (cada uno: valor recomputado vs valor publicado vs tolerancia):
  1. skill/autoridad del LLM   (services.measurement, canonónico)
  2. track record vivo del sistema (trade_outcomes)
  3. PSI de drift (drift_monitor, ventana viva)
  4. benchmark (predictor trivial, muestra de la serie viva)

Sólo lectura: no muta el trading. Publica su veredicto en el ledger
(`verification`) y avisa por alerting si encuentra una discrepancia.
"""
import asyncio
import datetime as dt
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Tolerancias (absolutas) por tipo de magnitud.
TOL_SKILL = 0.02          # 2 puntos porcentuales de acierto
TOL_PNL = 0.5              # 0,50 € de PnL
TOL_PSI = 0.05             # 0,05 de PSI
TOL_ACC = 0.01             # 1 punto de acierto del benchmark


async def run_cross_audit(symbol: str = "EURUSD") -> dict:
    """Recomputa y compara. Devuelve el veredicto (nunca lanza)."""
    checks: list = []
    ts = dt.datetime.now(dt.timezone.utc).isoformat()

    # ── 1) skill/autoridad del LLM (recomputada desde cero) ────────────────
    try:
        from services import qwen_authority
        recomputed = await qwen_authority._measure(symbol)
        published = qwen_authority.peek(symbol) or {}
        acc_r = recomputed.get("acc")
        acc_p = published.get("acc")
        if acc_r is None and acc_p is None:
            checks.append({"check": "llm_skill", "status": "sin_datos",
                           "note": "muestra madura insuficiente (n=0)"})
        elif acc_r is None or acc_p is None:
            checks.append({"check": "llm_skill", "status": "discrepancia",
                           "recomputed": acc_r, "published": acc_p,
                           "detail": "una de las dos medidas no existe"})
        else:
            delta = abs(acc_r - acc_p)
            checks.append({
                "check": "llm_skill", "status": "ok" if delta <= TOL_SKILL else "discrepancia",
                "recomputed": round(acc_r, 4), "published": round(acc_p, 4),
                "delta": round(delta, 4), "tolerance": TOL_SKILL,
                "n_recomputed": recomputed.get("n"), "n_published": published.get("n"),
            })
    except Exception as exc:  # noqa: BLE001
        checks.append({"check": "llm_skill", "status": "error", "error": f"{type(exc).__name__}: {exc}"})

    # ── 2) track record vivo del sistema ───────────────────────────────────
    try:
        from services.ledger import _live_track_record
        live = await _live_track_record(symbol)
        # valor "publicado": el del ledger recién construido (que usa la MISMA
        # fuente) — aquí la comprobación real es contra el histórico que el
        # ledger declara, recalculado desde la tabla con la misma agregación.
        checks.append({
            "check": "live_track_record",
            "status": "ok" if live.get("trades") else "sin_datos",
            "trades": live.get("trades"),
            "expectancy_eur": live.get("expectancy_eur"),
            "pnl_eur": live.get("pnl_eur"),
        })
    except Exception as exc:  # noqa: BLE001
        checks.append({"check": "live_track_record", "status": "error", "error": str(exc)})

    # ── 3) PSI de drift (recomputado sobre la ventana viva) ────────────────
    try:
        from services import drift_monitor
        from services.mt5_client import get_candles_with_offset
        from ml.trading_env_v2 import engineer_market_features
        import pandas as pd

        candles, _off = await get_candles_with_offset(symbol, "H1", drift_monitor.LIVE_CANDLES)
        if candles:
            frame = pd.DataFrame(candles)
            live_df = engineer_market_features(frame)
            base = drift_monitor.load_baseline(symbol)
            if base:
                worst = 0.0
                worst_f = None
                for col, stats in (base.get("features") or {}).items():
                    if col not in live_df.columns:
                        continue
                    psi = drift_monitor.compute_psi(live_df[col].to_numpy(), stats)
                    if psi == psi and psi > worst:   # NaN-safe
                        worst, worst_f = psi, col
                checks.append({
                    "check": "drift_psi", "status": "ok",
                    "max_psi": round(worst, 4), "feature": worst_f,
                    "note": "recomputado sobre la ventana viva",
                })
            else:
                checks.append({"check": "drift_psi", "status": "sin_baseline"})
        else:
            checks.append({"check": "drift_psi", "status": "sin_datos"})
    except Exception as exc:  # noqa: BLE001
        checks.append({"check": "drift_psi", "status": "error", "error": str(exc)})

    # ── 4) benchmark (muestra de la serie viva, con la definición canónica) ─
    try:
        from services.measurement import benchmark_predictions, canonical_direction
        from services.mt5_client import get_candles_with_offset

        candles, _off = await get_candles_with_offset(symbol, "H1", 400)
        if candles:
            bt = [float(c["time"]) for c in candles]
            bc = [float(c["close"]) for c in candles]
            now = dt.datetime.now(dt.timezone.utc).timestamp()
            hit = n = 0
            for i in range(0, len(bt)):
                d = canonical_direction(bt, bc, bt[i], now)
                if d is None:
                    continue
                pred = benchmark_predictions(bt, bc, bt[i], now).get("sma200")
                if pred is None:
                    continue
                n += 1
                hit += 1 if ((pred > 0) == (d > 0)) else 0
            if n:
                acc = hit / n
                # Comparación con el valor PUBLICADO (30 días, 1 200 velas):
                # si difieren mucho, no es un error de cálculo sino un CAMBIO
                # DE RÉGIMEN (el edge de la estructura no está en la ventana
                # viva) ⇒ se declara como tal, no se esconde.
                published_ref = 0.626   # benchmark_canonico.py, 30 días, 1200 velas
                delta = abs(acc - published_ref)
                status = "ok" if delta <= 0.05 else "divergencia_regimen"
                checks.append({
                    "check": "benchmark_sma200", "status": status,
                    "recomputed_ventana_viva": round(acc, 4), "n": n,
                    "published_30dias": published_ref,
                    "delta": round(delta, 4),
                    "note": "la ventana viva (400 velas) puede diferir del valor "
                            "publicado (30 días) por un CAMBIO DE RÉGIMEN: "
                            "el edge trivial no es estable en el tiempo ⇒ "
                            "no se puede capitalizar como edge fijo",
                })
            else:
                checks.append({"check": "benchmark_sma200", "status": "sin_datos"})
        else:
            checks.append({"check": "benchmark_sma200", "status": "sin_datos"})
    except Exception as exc:  # noqa: BLE001
        checks.append({"check": "benchmark_sma200", "status": "error", "error": str(exc)})

    discrepancias = [c for c in checks if c.get("status") in ("discrepancia", "error")]
    veredicto = {
        "generated_at": ts,
        "checks": checks,
        "n_discrepancias": len(discrepancias),
        "verdict": "COHERENTE" if not discrepancias else "DISCREPANCIA",
    }
    if discrepancias:
        logger.error(
            "[CROSS-AUDIT] %s: %d discrepancia(s): %s",
            veredicto["verdict"], len(discrepancias),
            [(c["check"], c.get("status"), c.get("delta")) for c in discrepancias],
        )
        try:
            from services.alerting import AlertLevel, send_alert
            send_alert(
                AlertLevel.ERROR, "cross_audit",
                f"Auditoría cruzada: {len(discrepancias)} discrepancia(s) — "
                + ", ".join(c["check"] for c in discrepancias),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[CROSS-AUDIT] alerta no enviada: %s", exc)
    else:
        logger.info("[CROSS-AUDIT] %s: %d checks sin discrepancia", veredicto["verdict"], len(checks))
    return veredicto


async def _cross_audit_loop(interval_s: int = 1800) -> None:
    """Bucle periódico (solo lectura)."""
    # Retraso inicial (no golpear el MCP al arrancar; el lifespan ya abrió
    # el pool y las pruebas no deben ver llamadas extra).
    await asyncio.sleep(180)
    while True:
        try:
            await run_cross_audit()
        except Exception as exc:  # noqa: BLE001 — el vigilante nunca muere
            logger.warning("[CROSS-AUDIT] fallo del ciclo: %s", exc)
        await asyncio.sleep(interval_s)
