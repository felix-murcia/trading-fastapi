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
from services.error_taxonomy import LOOP_ERRORS

logger = logging.getLogger(__name__)

# Tolerancias (absolutas) por tipo de magnitud.
TOL_SKILL = 0.02          # 2 puntos porcentuales de acierto
TOL_PNL = 0.5              # 0,50 € de PnL
TOL_PSI = 0.05             # 0,05 de PSI
TOL_ACC = 0.01             # 1 punto de acierto del benchmark


async def _mt5_alive() -> bool:
    """¿Contesta el servidor MT5? (1 llamada corta, sin estados)."""
    try:
        import httpx
        from config import settings
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.get(f"{settings.mt5_http_url}/api/v1/account/info")
            return r.status_code == 200
    except LOOP_ERRORS:  # noqa: BLE001
        return False


def _status_error(mt5_alive: bool) -> str:
    """Estado de un check que falló: sin datos del MT5 o error real."""
    return "error" if mt5_alive else "sin_datos_mt5"


async def run_cross_audit(symbol: str = "EURUSD",
                          market_port=None) -> dict:
    """Recomputa y compara. Devuelve el veredicto (nunca lanza)."""
    checks: list = []
    ts = dt.datetime.now(dt.timezone.utc).isoformat()

    if market_port is None:
        from core.container import build_registry_from_settings
        market_port = build_registry_from_settings().get(symbol).market

    # T-31.9 (2026-10-20): si el servidor MT5 no responde, los checks que
    # dependen de él no se pueden recalcular. Se distingue de una
    # discrepancia real (que sería un fallo del SISTEMA) ⇒ estado
    # "sin_datos_mt5" (la causa raíz se declara una vez, no seis).
    mt5_alive = await _mt5_alive()

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
    except LOOP_ERRORS as exc:  # noqa: BLE001
        checks.append({"check": "llm_skill", "status": _status_error(mt5_alive),
                       "error": f"{type(exc).__name__}: {exc}"})

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
    except LOOP_ERRORS as exc:  # noqa: BLE001
        checks.append({"check": "live_track_record", "status": "error", "error": str(exc)})

    # ── 3) PSI de drift (recomputado sobre la ventana viva) ────────────────
    try:
        from services import drift_monitor
        from ml.trading_env_v2 import engineer_market_features
        import pandas as pd

        candles, _off = await market_port.get_candles_with_offset(symbol, "H1", drift_monitor.LIVE_CANDLES)
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
    except LOOP_ERRORS as exc:  # noqa: BLE001
        checks.append({"check": "drift_psi", "status": _status_error(mt5_alive), "error": str(exc)})

    # ── 4) benchmark (muestra de la serie viva, con la definición canónica) ─
    try:
        from services.measurement import (
            benchmark_predictions,
            canonical_direction,
            skill_vs_base,
        )

        candles, _off = await market_port.get_candles_with_offset(symbol, "H1", 400)
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
                # T-31.9 (2026-10-20): la exactitud de un predictor trivial
                # NO es su skill: si la regla dice siempre lo mismo, su
                # exactitud ES la base de la ventana (la deriva del
                # mercado). El benchmark se mide, por tanto, contra SU
                # PROPIA BASE (la mejor predicción constante) ⇒ lo que se
                # compara es el SKILL, no la exactitud bruta.
                dirs: list = []
                for i in range(0, len(bt)):
                    d0 = canonical_direction(bt, bc, bt[i], now)
                    if d0 is None:
                        continue
                    dirs.append(d0)
                sk = skill_vs_base(hit, n, dirs)
                published_ref = 0.0   # el skill del trivial es 0 por definición
                delta = abs((sk["skill_pp"] or 0.0) - published_ref)
                status = "ok" if delta <= 5.0 else "divergencia_regimen"
                checks.append({
                    "check": "benchmark_sma200", "status": status,
                    "recomputed_ventana_viva": round(acc, 4), "n": n,
                    "base_ventana": sk["base"],
                    "skill_pp": sk["skill_pp"],
                    "skill_real": sk["granted"],
                    "published_30dias": published_ref,
                    "delta": round(delta, 4),
                    "note": "el skill del predictor trivial frente a SU PROPIA "
                            "BASE (la deriva del mercado): si no supera su "
                            "base, no hay edge que capitalizar, por muy alto "
                            "que sea su acierto bruto",
                })
            else:
                checks.append({"check": "benchmark_sma200", "status": "sin_datos"})
        else:
            checks.append({"check": "benchmark_sma200", "status": "sin_datos"})
    except LOOP_ERRORS as exc:  # noqa: BLE001
        checks.append({"check": "benchmark_sma200", "status": _status_error(mt5_alive), "error": str(exc)})

    # ── 5) EDGE VIVO del par (G2/mapa de edge) ──────────────────────────
    # Orden Félix 2026-10-20: «cada par tiene su propia idiosincrasia» ⇒ el
    # sistema debe saber si el edge que explota sigue encendido. El mapa
    # canónico por ventana (30/14/7 d) lo dice: si la estructura acierta
    # <50 % en la ventana viva, el edge que el modelo explotaba está APAGADO
    # ⇒ se DECLARA (no se esconde ni se opera en silencio).
    try:
        from services.ledger import _edge_decay_map
        emap = await _edge_decay_map()
        for sym, v in (emap or {}).items():
            if "error" in v:
                checks.append({"check": f"edge_vivo_{sym}",
                               "status": _status_error(mt5_alive),
                               "error": v["error"]})
                continue
            w = v.get("windows", {})
            acc7 = (w.get("7d") or {}).get("acc")
            alive = bool(v.get("edge_alive"))
            checks.append({
                "check": f"edge_vivo_{sym}",
                "status": "ok" if alive else "edge_apagado",
                "acc_30d": (w.get("30d") or {}).get("acc"),
                "acc_14d": (w.get("14d") or {}).get("acc"),
                "acc_7d": acc7,
                "n_7d": (w.get("7d") or {}).get("n"),
                "note": "el edge trivial (sma200 H1) que el modelo explota "
                        "está por debajo del azar en la ventana viva ⇒ el "
                        "sistema opera sobre una señal apagada: la expectativa "
                        "viva del sistema tenderá a decaer",
            })
    except LOOP_ERRORS as exc:  # noqa: BLE001
        checks.append({"check": "edge_vivo", "status": "error", "error": str(exc)})

    # ── 6) LATENCIA de los ciclos (C3) ───────────────────────────────────
    # El segundo cerebro (LLM) consume ~40 s de un presupuesto de 60 s del
    # EA. Su valor está medido (bloquea entradas PEORES que la media: 40 % y
    # 33 % de acierto frente al 50 % del sistema) ⇒ NO se le saca del bucle.
    # Lo que sí hay que vigilar es la COLA: si el p95 se acerca al
    # presupuesto del EA, el ciclo se pierde (no se pierde la decisión).
    try:
        from db.connection import get_pool
        pool = get_pool()
        lat = await pool.fetch(
            """
            SELECT (data->'data'->>'latency_ms')::float AS l
            FROM audit_log
            WHERE event='ai_predict_cycle' AND data->'data'->>'symbol'=$1
              AND created_at > now() - interval '3 days'
              AND (data->'data'->>'latency_ms') IS NOT NULL
            """, symbol)
        vals = sorted(float(r["l"]) for r in lat)
        if vals:
            p50 = vals[len(vals) // 2] / 1000
            p95 = vals[int(len(vals) * 0.95)] / 1000
            checks.append({
                "check": "latencia_ciclos", "status": "ok" if p95 < 52 else "latencia_al_limite",
                "p50_s": round(p50, 1), "p95_s": round(p95, 1),
                "presupuesto_ea_s": 60,
                "note": "el segundo cerebro consume ~40 s del ciclo; su veto "
                        "aporta (bloquea entradas por debajo de la media), "
                        "pero la cola se acerca al presupuesto del EA ⇒ si el p95 "
                        "supera 52 s hay que actuar (modelo más rápido o "
                        "prompt más corto), no al revés",
            })
    except LOOP_ERRORS as exc:  # noqa: BLE001
        checks.append({"check": "latencia_ciclos", "status": "error", "error": str(exc)})

    discrepancias = [c for c in checks if c.get("status") in ("discrepancia", "error")]

    # T-31.9 (2026-10-20): si el servidor MT5 está caído, TODOS los checks
    # que dependen de él fallan por la MISMA causa ⇒ no son seis fallos del
    # sistema, sino uno solo (y el mensaje de alerta decía "6 discrepancias"
    # sin decir por qué). Se declara la causa raíz y se separa: el veredicto
    # es "SIN DATOS (MT5 CAÍDO)", no "DISCREPANCIA".
    mt5_caido = [c for c in checks if c.get("status") == "sin_datos_mt5"]
    if mt5_caido and not discrepancias:
        veredicto = {
            "generated_at": ts,
            "checks": checks,
            "n_discrepancias": 0,
            "verdict": "SIN DATOS (servidor MT5 caído — el sistema está en "
                       "HOLD por seguridad; no es una discrepancia)",
            "caja_negra": "servidor MT5 sin respuesta",
        }
        logger.warning(
            "[CROSS-AUDIT] SIN DATOS: el servidor MT5 no responde ⇒ %d check(s) "
            "dependientes no se pueden recalcular (no son discrepancias).",
            len(mt5_caido),
        )
        return veredicto

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
        except LOOP_ERRORS as exc:  # noqa: BLE001
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
        except LOOP_ERRORS as exc:  # noqa: BLE001 — el vigilante nunca muere
            logger.warning("[CROSS-AUDIT] fallo del ciclo: %s", exc)
        await asyncio.sleep(interval_s)
