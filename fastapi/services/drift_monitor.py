"""
P3-2c: Monitor de drift — PSI de features + decay de métricas, con alertas.

Auditoría (MLOps): "Sin monitorización de drift/decay (grep=0) → deterioro
del modelo indetectable". Este módulo implementa las dos señales:

1. **PSI (Population Stability Index)** por feature de mercado: compara la
   distribución de las últimas N velas vivas contra el baseline congelado en
   la promoción (deciles de las features z-scoreadas con sus stats de entreno).
   Umbrales estándar de model monitoring: PSI < 0.1 sin cambio significativo,
   0.1–0.2 moderado (WARNING), ≥ 0.2 significativo (CRITICAL).

2. **Decay de métricas** sobre los últimos cierres reales (trade_outcomes):
   test z sobre la esperanza (z = media / (std/√n)); z ≤ -1.645 (p < 0.05
   unilateral, aproximación normal con n ≥ 20) con media < 0 → CRITICAL;
   media < 0 sin significancia → WARNING; n < 20 → insufficient (sin alerta:
   sin evidencia no se inventa degradación).

Alertas agregadas vía services.alerting (una por pase como máximo) + evento
`drift_report` en audit_log (evidencia consultable). Programado en lifespan
vía _drift_monitor_loop.
"""

import asyncio
import json
import logging
import math
import os
import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from config import settings
from core.pair import LEGACY_SYMBOL, default_model_path
from services.alerting import AlertLevel, send_alert
from services.feed_patcher import fill_feed_hole
from services.error_taxonomy import LOOP_ERRORS

logger = logging.getLogger(__name__)

PSI_WARN = 0.1                # literatura model monitoring: cambio moderado
PSI_CRIT = 0.2                # cambio significativo de distribución
DECAY_MIN_SAMPLES = 20        # mínimo de cierres para testar decaimiento
DECAY_Z_CRIT = -1.645         # z_{0.05} unilateral (p<0.05)
LIVE_CANDLES = 200            # velas vivas para la ventana de PSI
# Ventana del baseline de drift (2026-10-20): comparable con la ventana viva
# (200 velas) — el baseline sobre toda la historia producía un PSI estructural
# en las features de nivel de precio (sma20: PSI 12,4 sin significado).
BASELINE_WINDOW = 400
MIN_LIVE_VALUES = 50          # masa mínima de valores no-NaN por feature
PSI_EPS = 1e-6                # suavizado para bins vacíos
DRIFT_CHECK_DELAY_S = 300     # primera pasada tras el arranque


def baseline_path(symbol: str = LEGACY_SYMBOL) -> str:
    """Baseline de drift JUNTO al bundle del mundo (P3-3: uno por par).

    EURUSD (legado) → raíz de PPO_OUTPUT_DIR (la ruta de siempre); el resto
    → su namespace ml/<SYMBOL>/. La promoción de un par jamás pisa la
    referencia de drift de otro (mundos sin acoplamiento).
    """
    return os.path.join(
        os.path.dirname(default_model_path(symbol)),
        "feature_drift_baseline.json",
    )


def build_feature_baseline(train_df: pd.DataFrame) -> dict:
    """Distribución de referencia de MARKET_FEATURES sobre el df de entreno.

    Por feature: media/desv + deciles (9 edges) + proporciones esperadas por
    bin (calculadas con los mismos datos, no supuestas uniformes). Features
    degeneradas (std=0, <100 muestras o bins no estrictamente crecientes) se
    excluyen: no hay distribución que comparar.

    VENTANA COMPARABLE (fix 2026-10-20): el baseline se calcula sobre la
    COLA reciente del df de entrenamiento (BASELINE_WINDOW velas), no sobre
    toda la historia. Motivo (medido el 20-10): la ventana viva de PSI son
    200 velas, y features de NIVEL DE PRECIO (p. ej. `sma20`, media 1,0987
    sobre 4 años) comparadas contra 4 años de historia (precio 1,05→1,25)
    concentran TODA la distribución viva en un solo bin ⇒ PSI ≈ 12 (crítico
    estructural) que no dice nada de las entradas del modelo: mide "el
    precio cambió de nivel", no "la forma de las features cambió". Con la
    ventana comparable, el PSI mide lo que debe medir.
    """
    from ml.trading_env_v2 import MARKET_FEATURES

    features: dict = {}
    excluded: list[str] = []
    # Cola comparable con la ventana viva (LIVE_CANDLES=200): con margen para
    # que los deciles no se estimen con muy pocos datos.
    if len(train_df) > BASELINE_WINDOW:
        train_df = train_df.tail(BASELINE_WINDOW)
    for col in MARKET_FEATURES:
        if col not in train_df.columns:
            excluded.append(f"{col}:ausente")
            continue
        s = pd.to_numeric(train_df[col], errors="coerce").dropna().to_numpy(dtype=float)
        if s.size < 100:
            excluded.append(f"{col}:n={s.size}")
            continue
        mean = float(s.mean())
        std = float(s.std(ddof=0))
        if std <= 0:
            excluded.append(f"{col}:std=0")
            continue
        z = (s - mean) / std
        edges = np.quantile(z, np.arange(0.1, 1.0, 0.1))
        if not np.all(np.diff(edges) > 0):
            excluded.append(f"{col}:bins-no-crecientes")
            continue
        idx = np.digitize(z, edges)
        expected = np.bincount(idx, minlength=10) / z.size
        features[col] = {
            "mean": mean,
            "std": std,
            "edges": [float(e) for e in edges],
            "expected": [float(p) for p in expected],
        }
    if excluded:
        logger.info("[DRIFT] baseline: features excluidas (%s)", ", ".join(excluded))
    return {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "rows": int(len(train_df)),
        "features": features,
    }


def save_baseline(train_df: pd.DataFrame, symbol: str = LEGACY_SYMBOL) -> dict:
    """Congela el baseline del MUNDO `symbol` junto a su bundle y devuelve
    {path, sha256, n_features}.

    Lanza si ninguna feature es utilizable: una promoción sin baseline
    dejaría al drift sin referencia (mejor no promover que promover a ciegas).
    """
    from services.model_registry import sha256_of

    baseline = build_feature_baseline(train_df)
    if not baseline["features"]:
        raise RuntimeError("save_baseline: ninguna feature con distribución utilizable")
    path = baseline_path(symbol)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(baseline, f, indent=2)
    os.replace(tmp, path)
    info = {"path": path, "sha256": sha256_of(path), "n_features": len(baseline["features"])}
    logger.warning("[DRIFT] baseline congelado: %d features → %s", info["n_features"], path)
    return info


def load_baseline(symbol: str = LEGACY_SYMBOL) -> dict | None:
    path = baseline_path(symbol)
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def compute_psi(live_values, stats: dict) -> float:
    """PSI estándar Σ(a−e)·ln(a/e) con los bins y esperanzas del baseline.

    Valores vivos → z con las stats del entreno → digitize sobre los deciles.
    Devuelve NaN si la masa viva es insuficiente (< MIN_LIVE_VALUES).
    """
    s = pd.to_numeric(pd.Series(np.asarray(live_values).ravel()), errors="coerce").dropna()
    if s.size < MIN_LIVE_VALUES:
        return float("nan")
    z = (s.to_numpy(dtype=float) - stats["mean"]) / stats["std"]
    n_bins = len(stats["expected"])
    idx = np.digitize(z, stats["edges"])
    actual = np.bincount(idx, minlength=n_bins)[:n_bins] / z.size
    a = np.clip(actual, PSI_EPS, None)
    e = np.clip(np.asarray(stats["expected"], dtype=float), PSI_EPS, None)
    return float(np.sum((a - e) * np.log(a / e)))


def classify_psi(psi: float) -> str:
    if math.isnan(psi):
        return "insufficient"
    if psi >= PSI_CRIT:
        return "critical"
    if psi >= PSI_WARN:
        return "warning"
    return "ok"


def _json_safe(obj):
    """NaN/±inf → null: PostgreSQL jsonb y JSON estricto rechazan NaN/Infinity."""
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    return obj


def evaluate_decay(pnls: list[float]) -> dict:
    """Test z sobre la esperanza de los últimos n cierres (ver docstring módulo)."""
    n = len(pnls)
    if n < DECAY_MIN_SAMPLES:
        return {"status": "insufficient", "n": n,
                "detail": f"n={n} < {DECAY_MIN_SAMPLES}: sin evidencia suficiente"}
    arr = np.asarray(pnls, dtype=float)
    mean = float(arr.mean())
    std = float(arr.std(ddof=1))
    base = {"n": n, "mean_pnl": mean, "winrate": float((arr > 0).mean())}
    if std == 0:
        # Sin varianza: todas ganan o todas pierden — decisión directa.
        z = float("-inf") if mean < 0 else float("inf")
        status = "critical" if mean < 0 else "ok"
    else:
        z = mean / (std / math.sqrt(n))
        if mean < 0 and z <= DECAY_Z_CRIT:
            status = "critical"
        elif mean < 0:
            status = "warning"
        else:
            status = "ok"
    return {"status": status, "z": z, **base}


async def _recent_pnl(limit: int = 50) -> list[float]:
    """Últimos cierres reales (trade_outcomes) — fuente persistente de PnL."""
    from db.connection import get_pool

    pool = get_pool()
    rows = await pool.fetch(
        "SELECT pnl FROM trade_outcomes ORDER BY exit_time DESC LIMIT $1", limit
    )
    return [float(r["pnl"]) for r in rows]


async def run_drift_check(symbol: str = LEGACY_SYMBOL) -> dict:
    """Un pase completo: PSI de features vivas + decay de métricas + alertas.

    Devuelve SIEMPRE el reporte (endpoint de observabilidad): los fallos
    parciales (red MT5, BD) se alertan aparte y quedan reflejados en el
    reporte en lugar de romperlo.

    G2-6/B2 (orden Félix 2026-10-20): el pase estaba HARDCODEADO a EURUSD
    (baseline sin símbolo + fetch symbol="EURUSD") ⇒ **el drift de GBPUSD
    nunca se reportaba** ⇒ mundo a ciegas. Ahora el pase es POR MUNDO.
    """
    report: dict = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "symbol": symbol,
        "baseline": None,
        "features": {},
        "max_psi": None,
        "max_psi_feature": None,
        "level": "ok",
        "decay": None,
    }

    baseline = load_baseline(symbol)
    if baseline is None:
        report.update(status="no_baseline", level="warning")
        send_alert(
            AlertLevel.WARNING, "drift_monitor",
            "Sin baseline de features: el próximo retrain promovido lo congelará",
        )
    else:
        report["status"] = "ok"
        report["baseline"] = {
            "created_at": baseline.get("created_at"),
            "n_features": len(baseline.get("features", {})),
        }
        try:
            from services.auto_retrain import _build_training_dataframe, _fetch_historical_candles
            candles = await _fetch_historical_candles(symbol=symbol, count=LIVE_CANDLES)
            # Parche de agujero con CSV (orden Félix 2026-10-13, recom. 1):
            # sin él, si el feed vuelve a cortar la historia la ventana de PSI
            # se llena de saltos fantasma y el monitor queda CIEGO (PSI≈11.2
            # por overflow de sma20, medido en vivo 2026-10-12). El fetch del
            # retrain NO se toca (freeze de datos de entrenamiento).
            candles = fill_feed_hole(candles, symbol, "H1")
            live_df = _build_training_dataframe(candles)
            for col, stats in baseline.get("features", {}).items():
                if col not in live_df.columns:
                    report["features"][col] = {"psi": float("nan"), "level": "insufficient"}
                    continue
                psi = compute_psi(live_df[col].to_numpy(), stats)
                report["features"][col] = {"psi": psi, "level": classify_psi(psi)}
            finite = {
                c: f["psi"] for c, f in report["features"].items()
                if not math.isnan(f["psi"])
            }
            if finite:
                worst = max(finite, key=finite.get)
                report["max_psi_feature"] = worst
                report["max_psi"] = finite[worst]
            levels = {f["level"] for f in report["features"].values()}
            if "critical" in levels:
                report["level"] = "critical"
            elif "warning" in levels:
                report["level"] = "warning"
        except LOOP_ERRORS as exc:
            report["features_error"] = f"{type(exc).__name__}: {exc}"
            report["level"] = "warning"
            send_alert(
                AlertLevel.ERROR, "drift_monitor",
                f"PSI de features no evaluado: {type(exc).__name__}: {exc}", exc=exc,
            )

    try:
        report["decay"] = evaluate_decay(await _recent_pnl())
    except LOOP_ERRORS as exc:
        report["decay"] = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
        send_alert(
            AlertLevel.ERROR, "drift_monitor",
            f"Decay de métricas no evaluado: {type(exc).__name__}: {exc}", exc=exc,
        )
        if report["level"] == "ok":
            report["level"] = "warning"

    decay_status = (report.get("decay") or {}).get("status")
    if decay_status == "critical":
        report["level"] = "critical"
    elif decay_status == "warning" and report["level"] == "ok":
        report["level"] = "warning"

    # Alerta agregada SOLO si hay ofensores de PSI o decay con problemática:
    # los fallos estructurales (no_baseline, red, BD) ya alertaron arriba con
    # su mensaje específico — así cada pase emite como máximo una alerta por
    # tipo de problema, sin duplicados redundantes.
    offenders = {
        c: round(f["psi"], 3) for c, f in report["features"].items()
        if f["level"] in ("warning", "critical")
    }
    has_issue = bool(offenders) or decay_status in ("warning", "critical")
    if report["level"] in ("warning", "critical") and has_issue:
        ctx = {
            "max_psi": report.get("max_psi"),
            "max_psi_feature": report.get("max_psi_feature"),
            "offenders": offenders,
            "decay": report.get("decay"),
        }
        send_alert(
            AlertLevel.ERROR if report["level"] == "critical" else AlertLevel.WARNING,
            "drift_monitor",
            f"Drift {report['level']}: max_psi={report.get('max_psi')} "
            f"({report.get('max_psi_feature')}) decay={decay_status}",
            context=ctx,
        )

    # Evidencia consultable. Si el insert falla se alerta, pero el reporte se
    # devuelve igual (un endpoint de observabilidad no debe 500 por auditoría).
    report = _json_safe(report)   # NaN/inf → null ANTES de jsonb y de la respuesta
    try:
        from db.connection import get_pool

        await get_pool().execute(
            "INSERT INTO audit_log(cycle_id, event, data) VALUES($1, $2, $3)",
            f"drift_{int(time.time())}",
            "drift_report",
            json.dumps(report, default=float, ensure_ascii=False),
        )
    except LOOP_ERRORS as exc:
        send_alert(
            AlertLevel.ERROR, "drift_monitor",
            f"Insert de drift_report en audit_log falló: {type(exc).__name__}: {exc}",
            exc=exc,
        )

    logger.warning(
        "[DRIFT] level=%s max_psi=%s (%s) decay=%s",
        report.get("level"), report.get("max_psi"),
        report.get("max_psi_feature"), decay_status,
    )
    return report


async def _drift_monitor_loop() -> None:
    """Drift programado: primera pasada pasados DRIFT_CHECK_DELAY_S, luego cada N horas."""
    interval = max(1, settings.drift_check_interval_hours) * 3600
    await asyncio.sleep(DRIFT_CHECK_DELAY_S)
    while True:
        try:
            # G2-6/B2: el drift se mide POR MUNDO habilitado (antes sólo
            # EURUSD ⇒ el drift de GBPUSD nunca se reportaba).
            from core.container import build_registry_from_settings
            symbols = [ctx.config.symbol for ctx in build_registry_from_settings().enabled()]
            for sym in symbols:
                report = await run_drift_check(sym)
                logger.warning(
                    "[DRIFT] [%s] programado → level=%s (próximo en %dh)",
                    sym, report.get("level"), settings.drift_check_interval_hours,
                )
        except asyncio.CancelledError:
            raise
        except LOOP_ERRORS as exc:
            send_alert(
                AlertLevel.ERROR, "drift_monitor",
                f"Drift check programado falló: {type(exc).__name__}: {exc}", exc=exc,
            )
        await asyncio.sleep(interval)
