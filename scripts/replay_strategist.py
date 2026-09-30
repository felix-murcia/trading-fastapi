#!/usr/bin/env python3
"""Replay histórico de los criterios C1–C8 del Estratega (plan §6 — solo lectura).

Reconstruye el estado narrativo de cada ciclo `ai_predict_cycle` (formato nuevo
y antiguo), evalúa C1–C8 con el MISMO código que corre en shadow
(`services.strategist.evaluate_criteria`) y mide la expectancia de los
subconjuntos *permitido* vs *bloqueado* sobre los trades reales mapeados a su
ciclo productor, con split temporal (train = primera mitad, test = segunda).

NADA se activa: `CRITERIA[*].active` no se toca. La salida es un informe para
aprobación de Félix (plan §6: «ningún criterio activo sin informe de replay
aprobado»).

Uso (desde la raíz del repo, venv del host):

  source .venv312/bin/activate
  export DATABASE_URL="postgresql://trading:$(grep -h '^POSTGRES_PASSWORD=' .env | cut -d= -f2-)@localhost:5435/trading"
  export MT5_HTTP_URL="$(grep -h '^MT5_HTTP_URL=' .env | cut -d= -f2-)"
  export INTERNAL_TOKEN="$(grep -h '^INTERNAL_TOKEN=' .env | cut -d= -f2-)"
  python scripts/replay_strategist.py --out doc/replay-estratega.md

Decisiones metodológicas (todas también en el informe):
- **Dominio temporal**: `mt5_time` (filas nuevas), `created_at + 3 h` (filas
  antiguas, EEST), `trade_outcomes.*` y las velas MCP comparten la etiqueta de
  reloj BROKER («UTC falso»). La asunción +3 h se valida empíricamente con la
  tasa de mapeo trade→ciclo: si el dominio estuviera mal, casi nada mapearía.
- **C1 usa `ml_prob` como proxy de `|direction_head|`** (clip 0.1–0.99 del
  pipeline serve): la cabeza cruda no se persiste en auditoría. Documentado.
- **C7 no es replayable**: no existe spread histórico (el deque vivo empieza
  hoy) → `pass=None` «sin historia» en todos los ciclos.
- **Fail-safe idéntico a producción**: solo `pass is True` permite; `None`
  (dato ausente / no evaluable) contaría como bloqueo si estuviera activo.
- **Regla 3 €** (`core.rules.is_winning_close`) para win-rates; la
  expectancia es la media del pnl REAL del broker (ya neto de costes): la
  regla 3 € no puede alterar una media (identidad), por eso se reporta como
  win-rate y clasificación, no como pnl recortado.
- **Filas antiguas sin campos** (calidad/régimen/volumen): el criterio que
  los necesita se enmascara a `pass=None` con `detail` explícito — nunca
  «0.0 inventado».
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import random
import statistics
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "fastapi"))

import asyncpg  # noqa: E402
import httpx  # noqa: E402
import pandas as pd  # noqa: E402

from config import settings  # noqa: E402
from core.rules import is_winning_close  # noqa: E402
from ml.trading_env_v2 import engineer_market_features  # noqa: E402
from services.position_sizing import PIP_SIZE  # noqa: E402
from services.strategist import (  # noqa: E402
    CRITERIA,
    STRATEGIST_VERSION,
    compute_since_exit,
    evaluate_criteria,
)

SYMBOL = "EURUSD"
PIP = float(PIP_SIZE[SYMBOL])
# Ventana de mapeo trade→ciclo productor (ciclo de 5 min + latencia de orden).
MAP_EARLY_S = 300
MAP_LATE_S = 60
# Filas antiguas: created_at es UTC real; la etiqueta de broker es +3 h (EEST).
OLD_ROW_OFFSET_H = 3
MIN_BARS_TO_EVAL = 40          # ATR/indicadores con holgura sobre el mínimo 30
GAP_REPORT_S = 6 * 3600        # huecos de auditoría > 6 h (reales; la noche y el
                               # fin de semana sin ciclos es cobertura normal)

# Rejillas de θ para el barrido en train (§6: θ se elige en la primera mitad y
# se verifica en la segunda; los umbrales de serve NO se tocan jamás).
THETA_GRID: dict[str, list[float]] = {
    "C1_direction": [0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50],
    "C3_quality": [5.0, 5.5, 6.0, 6.5, 7.0, 7.5, 8.0, 8.5, 9.0],
    "C5_change_since_exit": [2.0, 4.0, 6.0, 8.0, 10.0, 12.0, 14.0, 16.0, 18.0, 20.0],
    "C6_post_loss_evidence": [6.0, 6.5, 7.0, 7.5, 8.0, 8.5, 9.0],
    "C8_late_join": [0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0],
}


# ─── Carga de datos ─────────────────────────────────────────────────────────

async def fetch_candles(client: httpx.AsyncClient) -> pd.DataFrame:
    """Velas H1 del MCP (etiqueta de broker, orden ascendente). Propaga."""
    res = await client.get(
        f"{settings.mt5_http_url}/api/v1/market/candles/latest",
        params={"symbol_name": SYMBOL, "timeframe": "H1", "count": 900},
        headers={"x-internal-token": settings.internal_token},
        timeout=30.0,
    )
    res.raise_for_status()
    data = res.json()
    rows = data if isinstance(data, list) else data.get("candles", [])
    if len(rows) < 200:
        raise RuntimeError(f"candles insuficientes: {len(rows)} < 200")
    return pd.DataFrame(
        {
            "time": [pd.Timestamp(c["time"]) for c in rows],
            "open": [float(c["open"]) for c in rows],
            "high": [float(c["high"]) for c in rows],
            "low": [float(c["low"]) for c in rows],
            "close": [float(c["close"]) for c in rows],
            "volume": [float(c.get("tick_volume", c.get("volume", 0))) for c in rows],
        }
    ).sort_values("time").reset_index(drop=True)


async def load_db() -> tuple[list[dict], list[dict], dict[int, dict]]:
    pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=2)
    try:
        audit_rows = await pool.fetch(
            "SELECT id, cycle_id, created_at, data FROM audit_log "
            "WHERE event='ai_predict_cycle' ORDER BY id"
        )
        trade_rows = await pool.fetch(
            "SELECT id, symbol, direction, entry_time, exit_time, pnl, exit_reason "
            "FROM trade_outcomes WHERE entry_time >= TIMESTAMP '2020-01-01' ORDER BY exit_time"
        )
        insight_rows = await pool.fetch(
            "SELECT trade_id, regime, quality_score, qwen_confidence, qwen_insight FROM trade_insights"
        )
    finally:
        await pool.close()
    trades = [
        {
            "id": r["id"],
            "symbol": r["symbol"],
            "direction": r["direction"],
            "entry_time": r["entry_time"],
            "exit_time": r["exit_time"],
            "pnl": float(r["pnl"]),
            "exit_reason": r["exit_reason"],
        }
        for r in trade_rows
    ]
    insights = {
        r["trade_id"]: {
            "regime": r["regime"],
            "quality_score": r["quality_score"],
            "qwen_confidence": r["qwen_confidence"],
            "qwen_insight": r["qwen_insight"],
        }
        for r in insight_rows
        if r["trade_id"] is not None
    }
    return [dict(r) for r in audit_rows], trades, insights


def _as_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_cycles(audit_rows: list[dict]) -> tuple[list[dict], dict]:
    """Normaliza los DOS formatos históricos de `ai_predict_cycle`."""
    cycles: list[dict] = []
    stats = {"new": 0, "old": 0, "unparseable": 0}
    for row in audit_rows:
        raw = row["data"]
        if isinstance(raw, str):
            raw = json.loads(raw)
        if not isinstance(raw, dict):
            stats["unparseable"] += 1
            continue
        inner = raw.get("data")
        if isinstance(inner, dict):
            fmt = "new"
            payload = inner
            mt5 = payload.get("mt5_time", raw.get("mt5_time"))
            if mt5 is None:
                stats["unparseable"] += 1
                continue
            t_label = pd.Timestamp(float(mt5), unit="s", tz="UTC")
        else:
            fmt = "old"
            payload = raw
            # created_at es UTC real → etiqueta de broker = +3 h (EEST).
            t_label = pd.Timestamp(row["created_at"]).tz_convert("UTC") + pd.Timedelta(hours=OLD_ROW_OFFSET_H)
        decision = payload.get("decision")
        if decision not in ("BUY", "SELL", "HOLD"):
            stats["unparseable"] += 1
            continue
        stats[fmt] += 1
        cycles.append(
            {
                "id": row["id"],
                "cycle_id": row["cycle_id"],
                "fmt": fmt,
                "t": t_label,
                "decision": decision,
                "symbol": payload.get("symbol") or SYMBOL,
                "ml_prob": _as_float(payload.get("ml_prob")),
                "quality": _as_float(payload.get("quality_score")),
                "regime": payload.get("regime"),
                "llm_bias": payload.get("llm_bias"),
                "qwen_degraded": payload.get("qwen_degraded"),
                "volume": _as_float(payload.get("volume")),
                "veto": payload.get("veto"),
                "veto_reason": payload.get("veto_reason"),
                "trade_id": None,   # se rellena en el mapeo
            }
        )
    cycles.sort(key=lambda c: c["t"])
    return cycles, stats


# ─── Estado narrativo as-of por ciclo ───────────────────────────────────────

def build_contexts(
    cycles: list[dict],
    trades: list[dict],
    insights: dict[int, dict],
    candles: pd.DataFrame,
) -> tuple[list[dict], dict]:
    """Por ciclo: último cierre previo, racha (semántica del guardián),
    bars/move (mismo `compute_since_exit` que en serve) y ctx para
    `evaluate_criteria`. Un solo barrido ordenado (dos punteros)."""
    stats = {"cycles": len(cycles), "skipped_small_window": 0}
    times = candles["time"]
    contexts: list[dict] = []
    trade_idx = 0
    streak = 0
    last_exit: dict | None = None

    for cyc in cycles:
        t = cyc["t"]
        while trade_idx < len(trades) and pd.Timestamp(trades[trade_idx]["exit_time"]) < t:
            tr = trades[trade_idx]
            streak = 0 if is_winning_close(tr["pnl"]) else streak + 1
            last_exit = tr
            trade_idx += 1

        df_raw = candles.loc[times <= t]
        is_entry = cyc["decision"] in ("BUY", "SELL")
        candidate = cyc["decision"] if is_entry else "HOLD"

        narrative: dict = {
            "last_exit": None,
            "thesis": None,
            "consecutive_losses": int(streak),
            "bars_since_close": None,
            "price_move_since_close_pips": None,
        }
        if last_exit is not None:
            narrative["last_exit"] = {
                "exit_time": last_exit["exit_time"].isoformat(),
                "exit_reason": last_exit["exit_reason"],
                "pnl": last_exit["pnl"],
                "direction": last_exit["direction"],
                "is_win": is_winning_close(last_exit["pnl"]),
            }
            li = insights.get(last_exit["id"])
            if li is not None and li.get("qwen_insight") is not None:
                narrative["thesis"] = {
                    "regime": li.get("regime"),
                    "quality_score": (
                        float(li["quality_score"]) if li.get("quality_score") is not None else None
                    ),
                    "confidence": (
                        float(li["qwen_confidence"]) if li.get("qwen_confidence") is not None else None
                    ),
                    "insight": str(li["qwen_insight"])[:280],
                }

        if len(df_raw) < MIN_BARS_TO_EVAL:
            stats["skipped_small_window"] += 1
            contexts.append(
                {
                    "cycle": cyc,
                    "candidate": candidate,
                    "narrative": narrative,
                    "ctx": None,
                    "missing": set(),
                    "skip_reason": f"ventana de velas < {MIN_BARS_TO_EVAL} velas",
                }
            )
            continue

        if last_exit is not None:
            bars, move = compute_since_exit(
                df_raw,
                pd.Timestamp(last_exit["exit_time"]),
                float(df_raw["close"].iloc[-1]),
                PIP,
            )
            narrative["bars_since_close"] = bars
            narrative["price_move_since_close_pips"] = move

        df_feat = engineer_market_features(df_raw)
        current_atr = float(df_feat["atr"].iloc[-1]) if "atr" in df_feat.columns else 0.0001

        ml = cyc["ml_prob"]
        quality = cyc["quality"]
        regime = cyc["regime"]
        degraded = cyc["qwen_degraded"]
        volume = cyc["volume"]

        # Enmascarados: dato ausente ≠ 0 inventado. Un criterio enmascarado
        # queda `pass=None` + `excluded=True` y se EXCLUYE del análisis de
        # expectancia (la ausencia de dato no es el juicio del criterio; en
        # serve live el dato siempre existe en el formato nuevo).
        # `qwen_degraded` se añadió el 2026-09-28 (5/557 = 0,9 % true): para
        # filas anteriores al campo se asume False y se documenta en el
        # informe — enmascarar C4 por eso dejaría la muestra sin poder.
        missing: set[str] = set()
        if ml is None:
            missing.add("C1_direction")
        if volume is None or not is_entry:
            missing.add("C2_executable_size")
        if quality is None:
            missing.add("C3_quality")
        if not regime:
            missing.add("C4_thesis_regime")
        if quality is None and int(streak) >= 1:
            missing.add("C6_post_loss_evidence")

        contexts.append(
            {
                "cycle": cyc,
                "candidate": candidate,
                "narrative": narrative,
                "missing": missing,
                "skip_reason": None,
                "ctx": {
                    "candidate": candidate,
                    "narrative": narrative,
                    "qwen": {
                        "quality": float(quality) if quality is not None else 0.0,
                        "regime": str(regime) if regime else "",
                        "bias": str(cyc["llm_bias"] or "NEUTRAL"),
                    },
                    "qwen_degraded": bool(degraded) if isinstance(degraded, bool) else False,
                    "direction_head": float(ml) if ml is not None else 0.0,
                    "lots": float(volume) if volume is not None else 0.0,
                    "current_atr": current_atr,
                    "pip_size": PIP,
                },
            }
        )
    return contexts, stats


def evaluate_context(
    context: dict,
    thresholds: dict[str, float] | None = None,
) -> dict[str, dict] | None:
    """`evaluate_criteria` (código de serve) + enmascarados. `thresholds`
    parchea CRITERIA temporalmente para el barrido offline (se restaura)."""
    if context["ctx"] is None:
        return None
    ctx = context["ctx"]
    saved: dict[str, object] = {}
    if thresholds:
        for cid, theta in thresholds.items():
            saved[cid] = CRITERIA[cid]["threshold"]
            CRITERIA[cid]["threshold"] = theta
    try:
        crit = evaluate_criteria(
            candidate=ctx["candidate"],
            narrative=ctx["narrative"],
            qwen=ctx["qwen"],
            qwen_degraded=ctx["qwen_degraded"],
            direction_head=ctx["direction_head"],
            lots=ctx["lots"],
            current_atr=ctx["current_atr"],
            pip_size=ctx["pip_size"],
            spread_pips=None,
            spread_error=None,
            spread_stats={"samples": 0, "threshold": None},
        )
    finally:
        for cid, val in saved.items():
            CRITERIA[cid]["threshold"] = val
    for cid in context["missing"]:
        if cid in crit:
            crit[cid]["pass"] = None
            crit[cid]["detail"] = "dato ausente en fila histórica / sin candidato: no evaluable"
    return crit


# ─── Mapeo trade → ciclo productor ──────────────────────────────────────────

def map_trades(cycles: list[dict], trades: list[dict]) -> tuple[list[dict], dict]:
    entries = [c for c in cycles if c["decision"] in ("BUY", "SELL")]
    entry_index = pd.Index([c["t"] for c in entries])
    mapped: list[dict] = []
    reasons = {"ok": 0, "no_cycle_in_window": 0, "direction_mismatch": 0}
    samples: list[str] = []
    for tr in trades:
        want = "BUY" if tr["direction"] == "LONG" else "SELL"
        entry_t = pd.Timestamp(tr["entry_time"])
        lo = int(entry_index.searchsorted(entry_t - pd.Timedelta(seconds=MAP_EARLY_S), side="left"))
        hi = int(entry_index.searchsorted(entry_t + pd.Timedelta(seconds=MAP_LATE_S), side="right"))
        window = entries[lo:hi]
        if not window:
            reasons["no_cycle_in_window"] += 1
            if len(samples) < 8:
                samples.append(f"trade {tr['id']} entry={tr['entry_time']}: sin ciclo BUY/SELL en ventana")
            continue
        same_dir = [c for c in window if c["decision"] == want]
        if not same_dir:
            reasons["direction_mismatch"] += 1
            if len(samples) < 8:
                samples.append(
                    f"trade {tr['id']} {tr['direction']}: en ventana solo "
                    f"{sorted({c['decision'] for c in window})}"
                )
            continue
        cyc = min(same_dir, key=lambda c: abs((entry_t - c["t"]).total_seconds()))
        cyc["trade_id"] = tr["id"]
        reasons["ok"] += 1
        mapped.append({"trade": tr, "cycle": cyc})
    return mapped, {"reasons": reasons, "samples": samples}


# ─── Análisis por criterio ──────────────────────────────────────────────────

def evaluate_on_mapped(
    mapped: list[dict],
    contexts_by_cycle_id: dict[int, dict],
    cid: str,
    thresholds: dict[str, float] | None = None,
) -> list[dict]:
    """[{trade_id, pnl, win, pass, excluded, value, entry_time}] alineado 1:1
    con `mapped`. `excluded=True` → dato ausente (fuera del análisis de
    expectancia; distinto de un bloqueo del criterio)."""
    out: list[dict] = []
    for m in mapped:
        context = contexts_by_cycle_id[m["cycle"]["id"]]
        crit = evaluate_context(context, thresholds)
        c = crit[cid] if crit is not None else {"pass": None, "value": None}
        out.append(
            {
                "trade_id": m["trade"]["id"],
                "pnl": m["trade"]["pnl"],
                "win": is_winning_close(m["trade"]["pnl"]),
                "pass": c["pass"],
                "excluded": cid in context["missing"] or crit is None,
                "value": c.get("value"),
                "entry_time": m["trade"]["entry_time"],
            }
        )
    return out


def subset_stats(rows: list[dict]) -> dict:
    if not rows:
        return {"n": 0, "exp": None, "win_rate": None}
    return {
        "n": len(rows),
        "exp": statistics.fmean(r["pnl"] for r in rows),
        "win_rate": statistics.fmean(1.0 if r["win"] else 0.0 for r in rows),
    }


def build_table(rows: list[dict]) -> dict:
    """Partición: excluidos (dato ausente) · permitidos (pass True) ·
    bloqueados (pass False o None estructural — el fail-safe de activate)."""
    evaluable = [r for r in rows if not r["excluded"]]
    allowed = [r for r in evaluable if r["pass"] is True]
    blocked = [r for r in evaluable if r["pass"] is not True]
    return {
        "allowed": subset_stats(allowed),
        "blocked": subset_stats(blocked),
        "allowed_rows": allowed,
        "blocked_rows": blocked,
        "n_excluded": len(rows) - len(evaluable),
    }


def separation(t: dict) -> float | None:
    if t["allowed"]["exp"] is None or t["blocked"]["exp"] is None:
        return None
    return t["allowed"]["exp"] - t["blocked"]["exp"]


def bootstrap_diff(a: list[float], b: list[float], n_boot: int = 2000, seed: int = 0):
    """IC 95 % de E[a] − E[b] (percentil)."""
    if not a or not b:
        return None, None
    rng = random.Random(seed)
    diffs = []
    for _ in range(n_boot):
        sa = [a[rng.randrange(len(a))] for _ in range(len(a))]
        sb = [b[rng.randrange(len(b))] for _ in range(len(b))]
        diffs.append(statistics.fmean(sa) - statistics.fmean(sb))
    diffs.sort()
    return diffs[int(0.025 * n_boot)], diffs[int(0.975 * n_boot)]


def sanity_hold_rate(
    hold_contexts: list[dict],
    cid: str,
    thresholds: dict[str, float] | None = None,
) -> tuple[int, int, float | None]:
    """(n_evaluables, n_bloqueados(False), tasa) sobre ciclos HOLD — solo
    `pass is False` cuenta como bloqueo afirmativo (None = no aplica)."""
    n_eval = 0
    n_block = 0
    for context in hold_contexts:
        crit = evaluate_context(context, thresholds)
        if crit is None:
            continue
        c = crit[cid]
        if c["pass"] is None:
            continue
        n_eval += 1
        if c["pass"] is False:
            n_block += 1
    return n_eval, n_block, (n_block / n_eval if n_eval else None)


def select_theta(
    mapped: list[dict],
    contexts_by_cycle_id: dict[int, dict],
    cid: str,
    train_ids: set[int],
) -> tuple[float | None, list[dict]]:
    """Barrido en train (plan §6): máximo E_permit − E_bloq sujeto a
    n_bloq ≥ 3, fracción bloqueada ≤ 50 % y win-rate bloqueado < 10 %
    (sanidades del propio plan aplicadas ya en train)."""
    results: list[dict] = []
    best_theta, best_sep = None, None
    for theta in THETA_GRID[cid]:
        rows = evaluate_on_mapped(mapped, contexts_by_cycle_id, cid, {cid: theta})
        train_rows = [
            r for r in rows if r["trade_id"] in train_ids and not r["excluded"]
        ]
        blocked = [r for r in train_rows if r["pass"] is not True]
        allowed = [r for r in train_rows if r["pass"] is True]
        if len(blocked) < 3 or len(blocked) / max(len(train_rows), 1) > 0.5 or not allowed:
            if not allowed and len(blocked) == len(train_rows):
                reason = f"bloquea el 100 % ({len(blocked)}/{len(train_rows)})"
            elif len(blocked) < 3:
                reason = f"n_bloq={len(blocked)} < 3"
            elif len(blocked) / max(len(train_rows), 1) > 0.5:
                reason = f"bloquea {len(blocked)}/{len(train_rows)} (> 50 %)"
            else:
                reason = "sin permitidos"
            results.append({"theta": theta, "reason": reason})
            continue
        wr = statistics.fmean(1.0 if r["win"] else 0.0 for r in blocked)
        if wr >= 0.10:
            results.append({"theta": theta, "reason": f"win-rate bloq {wr:.0%} ≥ 10%"})
            continue
        sep = statistics.fmean(r["pnl"] for r in allowed) - statistics.fmean(r["pnl"] for r in blocked)
        results.append({"theta": theta, "sep": sep, "n_block": len(blocked), "wr": wr})
        if best_sep is None or sep > best_sep:
            best_sep, best_theta = sep, theta
    return best_theta, results


# ─── Informe ────────────────────────────────────────────────────────────────

def fmt_eur(v: float | None) -> str:
    return "—" if v is None else f"{v:+.2f} €"


def fmt_pct(v: float | None) -> str:
    return "—" if v is None else f"{v:.0%}"


def write_report(
    path: Path,
    *,
    audit_stats: dict,
    build_stats: dict,
    map_stats: dict,
    cycles: list[dict],
    trades: list[dict],
    mapped: list[dict],
    analysis: dict,
    head: str,
) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    train_n = len(analysis["train_ids"])
    test_n = len(analysis["test_ids"])
    lines: list[str] = []
    w = lines.append

    w("# Replay histórico C1–C8 — informe de activación (plan §6)")
    w("")
    w(f"- Generado: **{now}** · HEAD `{head}` · config serve **sin tocar** "
      f"(`STRATEGIST_VERSION = {STRATEGIST_VERSION}`, todos los criterios OFF).")
    w("- Método: mismo `evaluate_criteria` que corre en shadow (código de serve, "
      "no una reimplementación). Solo lectura: SELECT + GET de velas.")
    w("")
    w("## 1. Muestra")
    w("")
    w("| Fuente | N | Nota |")
    w("|---|---:|---|")
    w(f"| Ciclos `ai_predict_cycle` | {audit_stats['cycles']} | "
      f"{audit_stats['new']} formato nuevo, {audit_stats['old']} antiguo, "
      f"{audit_stats['unparseable']} sin parsear |")
    w(f"| Trades válidos (entry ≥ 2020) | {len(trades)} | regla 3 € para win-rates; "
      "expectancia = pnl real medio (ya neto de costes) |")
    w(f"| Trades **mapeados** a su ciclo productor | {map_stats['reasons']['ok']} | "
      f"ventana [entry−{MAP_EARLY_S} s, entry+{MAP_LATE_S} s] + dirección coincidente |")
    w(f"| ↳ excluidos por era antigua (atribución débil) | {map_stats['weak_old_excluded']} | "
      "formato pre-2026-09-16 sin veto/quality/volumen; ver limitaciones |")
    w(f"| Trades **analizados** (era nueva) | {map_stats['reasons'].get('analyzed', len(mapped))} | "
      "muestra real del split train/test |")
    w(f"| Sin ciclo en ventana | {map_stats['reasons']['no_cycle_in_window']} | "
      "fuera de cobertura de auditoría o en hueco |")
    w(f"| Dirección no coincide | {map_stats['reasons']['direction_mismatch']} | "
      "candidato del ciclo ≠ dirección del trade |")
    evaluable = sum(1 for c in analysis["contexts"].values() if c["ctx"] is not None)
    w(f"| Ciclos evaluables (≥ {MIN_BARS_TO_EVAL} velas) | {evaluable} / {audit_stats['cycles']} | "
      f"ventanas descartadas: {build_stats['skipped_small_window']} |")
    w(f"| Split temporal | train {train_n} · test {test_n} | mitad cronológica de los mapeados |")
    w("")
    if map_stats["samples"]:
        first_t = cycles[0]["t"] if cycles else None
        pre_coverage = (
            sum(1 for tr in trades if first_t is not None and pd.Timestamp(tr["entry_time"]) < first_t)
        )
        w(f"Desglose de no mapeados: **{pre_coverage}** anteriores al primer ciclo de "
          "auditoría (incluye trades de 2024/2025 y el periodo sin cobertura) y "
          f"**{map_stats['reasons']['no_cycle_in_window'] - pre_coverage}** dentro de "
          "cobertura pero sin ciclo BUY/SELL en ventana (p. ej. rechazos por la EA "
          "o trades de fuentes distintas al chain /predict).")
        w("")
        w("Ejemplos de trades no mapeados:")
        for s in map_stats["samples"]:
            w(f"- {s}")
        w("")
    gaps = []
    prev = None
    for c in cycles:
        if prev is not None and (c["t"] - prev).total_seconds() > GAP_REPORT_S:
            gaps.append(f"{prev:%Y-%m-%d %H:%M} → {c['t']:%Y-%m-%d %H:%M} (etiqueta broker)")
        prev = c["t"]
    if gaps:
        w("Huecos de cobertura de auditoría > 6 h (trades dentro no mapeables; "
          "la noche/finde sin ciclos es cobertura normal):")
        for g in gaps:
            w(f"- {g}")
        w("")
    w("## 2. Metodología (decisiones y límites)")
    w("")
    w("- **Dominio temporal**: `mt5_time` (filas nuevas) / `created_at + 3 h` "
      "(antiguas, EEST) / `trade_outcomes` / velas MCP comparten la etiqueta de "
      "reloj broker. La tasa de mapeo trade→ciclo valida empíricamente la "
      "asunción: con el dominio erróneo casi nada mapearía.")
    w("- **C1**: proxy `ml_prob` = clip(|direction_head|, 0.1, 0.99) — la cabeza "
      "cruda no se persiste; por debajo de 0.1 la distinción se pierde "
      "(limitación documentada, no oculta).")
    w("- **C7**: sin historial de spread (el deque vivo empieza hoy) → `pass=None` "
      "en TODOS los ciclos: **no replayable**, se decide con datos vivos.")
    w("- **Fail-safe de activate**: en serve, un criterio ACTIVO solo permite "
      "`pass is True` — `None` bloquearía. Por eso en serve el dato siempre "
      "existe (formato nuevo); en replay, el dato ausente se EXCLUYE (ver "
      "«Excluido ≠ bloqueado») para no atribuir al criterio un bloqueo que no "
      "juzgó.")
    w("- **Expectancia**: media del pnl real del broker (spread/comisión/swap ya "
      "descontados). La regla 3 € no altera una media (identidad): se aplica a "
      "win-rates, no recortando pnl.")
    w("- **Excluido ≠ bloqueado**: un trade con dato ausente («n sin dato») sale "
      "del análisis — la ausencia de dato no es el juicio del criterio. Dentro "
      "del análisis, `pass=None` estructural (no aplica/no evaluable: p. ej. "
      "bias NEUTRAL en C4, dirección distinta en C5) SÍ cuenta como bloqueo: "
      "es el fail-safe real de la activación (solo `pass is True` permite).")
    w("- **Sanidad §6**: (a) E_bloq < E_permit en test; (b) win-rate de trades "
      "bloqueados < 10 %; (c) `pass=False` en ≤ 40 % de los ciclos en que el "
      "modelo ya hacía HOLD. `na` = subconjunto vacío → sin evidencia (la "
      "activación requiere evidencia, no solo ausencia de fallo).")
    w("")

    w("## 3. Resultados por criterio")
    w("")
    for cid, block in analysis["criteria"].items():
        w(f"### {cid} — θ config {CRITERIA[cid]['threshold']!r}")
        w("")
        if block.get("not_replayable"):
            w(f"_{block['not_replayable']}_")
            w("")
            w(f"**Veredicto: {block['verdict']}** — {block.get('verdict_reason', '')}")
            w("")
            continue
        for theta_label, tables in block["variants"]:
            w(f"**{theta_label}**")
            w("")
            w("| Conjunto | n permit | E permit | n bloque | E bloque | Separación | win-rate bloq | n sin dato |")
            w("|---|---:|---:|---:|---:|---:|---:|---:|")
            for split_name, t in tables:
                w(f"| {split_name} | {t['allowed']['n']} | {fmt_eur(t['allowed']['exp'])} | "
                  f"{t['blocked']['n']} | {fmt_eur(t['blocked']['exp'])} | "
                  f"{fmt_eur(separation(t))} | {fmt_pct(t['blocked']['win_rate'])} | "
                  f"{t['n_excluded']} |")
            w("")
        if block.get("bootstrap") is not None:
            lo, hi = block["bootstrap"]
            if lo is None:
                w("IC 95 % de la separación (test): **no calculable** (subconjunto vacío).")
            else:
                w(f"IC 95 % de la separación test (bootstrap 2000): "
                  f"**[{lo:+.2f} €, {hi:+.2f} €]**"
                  + ("" if lo > 0 else " — incluye 0 → la separación NO es significativa."))
            w("")
        for line in block.get("sanity_lines", []):
            w(f"- {line}")
        w("")
        w(f"**Veredicto: {block['verdict']}** — {block.get('verdict_reason', '')}")
        w("")

    w("## 4. Cuadro resumen (recomendación)")
    w("")
    w("| Criterio | θ config | θ replay | Recomendación | Motivo |")
    w("|---|---|---|---|---|")
    for cid, block in analysis["criteria"].items():
        theta_best = block.get("theta_best")
        w(f"| {cid} | {CRITERIA[cid]['threshold']!r} | "
          f"{theta_best if theta_best is not None else '—'} | "
          f"**{block['verdict']}** | {block.get('verdict_reason') or '—'} |")
    w("")
    w("## 5. Limitaciones")
    w("")
    for lim in analysis["limitations"]:
        w(f"- {lim}")
    w("")
    w("## 6. Siguiente paso")
    w("")
    w("1. Este informe → **aprobación de Félix** antes de tocar "
      "`CRITERIA[*].active` (plan §6: un criterio por PR, bump de "
      "`STRATEGIST_VERSION`, README con evidencia).")
    w("2. En paralelo, el shadow sigue recogiendo traza viva (§6: ≥ 7 días o "
      "≥ 50 candidatos deliberados) — ambas puertas deben pasar.")
    w("3. Si la recomendación es MANTENER OFF, el criterio queda OFF: no se "
      "ajustan umbrales para que la evidencia salga favorable.")
    w("")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def write_csv(path: Path, contexts: dict[int, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cids = list(CRITERIA.keys())
    with path.open("w", newline="", encoding="utf-8") as fh:
        wr = csv.writer(fh)
        wr.writerow(
            ["audit_id", "cycle_id", "t_label", "fmt", "decision", "veto", "veto_reason",
             "trade_id"]
            + [f"{cid}@config" for cid in cids]
        )
        for _, context in sorted(contexts.items()):
            cyc = context["cycle"]
            crit = evaluate_context(context, None)
            row = [
                cyc["id"],
                cyc["cycle_id"],
                cyc["t"].isoformat(),
                cyc["fmt"],
                cyc["decision"],
                cyc["veto"],
                cyc["veto_reason"],
                cyc["trade_id"],
            ]
            for cid in cids:
                if crit is None:
                    row.append("SKIP")
                else:
                    p = crit[cid]["pass"]
                    row.append("True" if p is True else ("False" if p is False else "None"))
            wr.writerow(row)


# ─── Main ──────────────────────────────────────────────────────────────────

async def main() -> int:
    parser = argparse.ArgumentParser(description="Replay C1–C8 (solo lectura)")
    parser.add_argument("--out", default=str(ROOT / "doc" / "replay-estratega.md"))
    parser.add_argument("--csv", default=str(ROOT / "doc" / "replay-cycles.csv"))
    args = parser.parse_args()

    async with httpx.AsyncClient() as client:
        candles = await fetch_candles(client)
    audit_rows, trades, insights = await load_db()
    cycles, audit_stats = parse_cycles(audit_rows)
    audit_stats["cycles"] = len(cycles)
    contexts, build_stats = await asyncio.to_thread(
        build_contexts, cycles, trades, insights, candles
    )
    contexts_by_cycle_id = {c["cycle"]["id"]: c for c in contexts}
    mapped_all, map_stats = map_trades(cycles, trades)
    # Los 6 mapeados de era antigua (formato pre-2026-09-16: sin veto/quality/
    # volumen, día 7 de sep anómalo y Δ≈3–5 min en lugar del patrón de
    # latencia de ~19 s de la era nueva) se EXCLUYEN del análisis: atribución
    # demasiado débil para sostener evidencia.
    map_stats["weak_old_excluded"] = sum(
        1 for m in mapped_all if m["cycle"]["fmt"] != "new"
    )
    mapped = [m for m in mapped_all if m["cycle"]["fmt"] == "new"]
    map_stats["reasons"]["analyzed"] = len(mapped)

    print(f"[replay] ciclos={len(cycles)} trades={len(trades)} "
          f"mapeados={map_stats['reasons']['ok']} (era nueva {len(mapped)}, "
          f"era antigua excluida {map_stats['weak_old_excluded']}) | "
          f"{map_stats['reasons']}")
    for s in map_stats["samples"]:
        print(f"    {s}")

    # Split temporal único (mitad cronológica de los mapeados) para todos los
    # criterios — mismo train/test en cada fila del informe.
    order = sorted(range(len(mapped)), key=lambda i: mapped[i]["trade"]["entry_time"])
    half = len(order) // 2
    train_ids = {mapped[i]["trade"]["id"] for i in order[:half]}
    test_ids = {mapped[i]["trade"]["id"] for i in order[half:]}

    hold_all = [c for c in contexts if c["cycle"]["decision"] == "HOLD"]
    hold_pure = [c for c in hold_all if c["cycle"]["veto"] is not True]

    analysis: dict = {
        "train_ids": train_ids,
        "test_ids": test_ids,
        "contexts": contexts_by_cycle_id,
        "criteria": {},
        "limitations": [],
    }

    for cid in CRITERIA:
        block: dict = {"variants": [], "sanity_lines": [], "theta_best": None}
        rows = evaluate_on_mapped(mapped, contexts_by_cycle_id, cid, None)
        train_rows = [r for r in rows if r["trade_id"] in train_ids]
        test_rows = [r for r in rows if r["trade_id"] in test_ids]

        # Contexto de θ: distribución del valor del criterio en los candidatos
        # analizados (para leer el barrido: si θ nunca alcanza el rango, el
        # criterio es vacío por construcción).
        if cid in ("C1_direction", "C3_quality", "C8_late_join"):
            vals = [
                r["value"] for r in rows
                if not r["excluded"] and r["value"] is not None
            ]
            if vals:
                vals_sorted = sorted(vals)
                med = vals_sorted[len(vals_sorted) // 2]
                block["sanity_lines"].append(
                    f"Valor del criterio en los {len(vals)} candidatos analizados: "
                    f"min={min(vals):.3f} · mediana={med:.3f} · max={max(vals):.3f} "
                    "(si θ ≤ min el criterio nunca bloquea un candidato)."
                )

        if cid == "C7_costs_vs_edge":
            block["not_replayable"] = (
                "Sin spread histórico no hay evidencia posible: `pass=None` en "
                "todos los ciclos. Se decide con las muestras vivas del deque "
                "(≥ 30 por par) — fuera de este replay por datos, no por método."
            )
            block["verdict"] = "MANTENER OFF (fuera de replay)"
            block["verdict_reason"] = "decidir con datos vivos (deque ≥ 30 muestras/par)"
            analysis["criteria"][cid] = block
            continue

        # θ* elegido en train (solo criterios con rejilla) → variante primaria.
        if cid in THETA_GRID:
            theta_best, grid_results = select_theta(mapped, contexts_by_cycle_id, cid, train_ids)
            block["theta_best"] = theta_best
            if theta_best is not None:
                rows_star = evaluate_on_mapped(mapped, contexts_by_cycle_id, cid, {cid: theta_best})
                tr_star = [r for r in rows_star if r["trade_id"] in train_ids]
                te_star = [r for r in rows_star if r["trade_id"] in test_ids]
                block["variants"].append((
                    f"**θ* replay = {theta_best}** (elegido en train; verificado en test)",
                    [("train", build_table(tr_star)), ("test", build_table(te_star))],
                ))
            else:
                detail = "; ".join(
                    f"{g['theta']}: {g.get('reason', 'ok')}" for g in grid_results
                )
                block["sanity_lines"].append(f"Barrido train sin θ elegible — {detail}")

        # Variante con θ de config (siempre presente, referencia oficial).
        block["variants"].append((
            f"θ config = {CRITERIA[cid]['threshold']!r}",
            [("train", build_table(train_rows)), ("test", build_table(test_rows))],
        ))

        # Mejor variante = la primaria (θ* si existe, si no config).
        best_tables = block["variants"][0][1]
        train_t, test_t = best_tables[0][1], best_tables[1][1]
        lo, hi = bootstrap_diff(
            [r["pnl"] for r in test_t["allowed_rows"]],
            [r["pnl"] for r in test_t["blocked_rows"]],
        )
        block["bootstrap"] = (lo, hi)

        # Sanidad (c): pass=False sobre ciclos HOLD (en la variante primaria).
        thresholds_best = {cid: block["theta_best"]} if block["theta_best"] is not None else None
        n_all, b_all, rate_all = sanity_hold_rate(hold_all, cid, thresholds_best)
        n_pure, b_pure, rate_pure = sanity_hold_rate(hold_pure, cid, thresholds_best)
        if n_all == 0:
            block["sanity_lines"].append(
                "Sanidad (c): no aplica — criterio dependiente del candidato; "
                "sobre un no-candidato el verdict es NO_CANDIDATE estructural "
                "(el criterio no llega a decidir)."
            )
        else:
            block["sanity_lines"].append(
                f"Sanidad (c): pass=False en {b_all}/{n_all} ({fmt_pct(rate_all)}) de "
                f"ciclos HOLD; {b_pure}/{n_pure} ({fmt_pct(rate_pure)}) de HOLD sin "
                f"veto (modelo por sí mismo)."
            )

        # Veredicto §6 sobre la variante primaria (test). Tres estados:
        # ok · fail · na (na = subconjunto vacío → sin evidencia, no falla).
        sep = separation(test_t)
        n_block = test_t["blocked"]["n"]
        n_allow = test_t["allowed"]["n"]
        wr_blocked = test_t["blocked"]["win_rate"]

        if n_block == 0:
            a_state, a_text = "na", (
                f"(a) E_bloq < E_permit en test: NO EVALUABLE "
                f"(0 bloqueados: sin subconjunto → sin evidencia)"
            )
        elif n_allow == 0:
            a_state, a_text = "fail", (
                f"(a) E_bloq < E_permit en test: NO (bloquea el 100 % de los "
                f"candidatos: n_bloq={n_block}, n_permit=0 → inhabilita la operativa)"
            )
        elif sep is not None and sep > 0:
            a_state, a_text = "ok", (
                f"(a) E_bloq < E_permit en test: SÍ (sep={fmt_eur(sep)}, "
                f"n_bloq={n_block})"
            )
        else:
            a_state, a_text = "fail", (
                f"(a) E_bloq < E_permit en test: NO (sep={fmt_eur(sep)}, "
                f"n_bloq={n_block})"
            )

        if n_block == 0:
            b_state, b_text = "ok", (
                "(b) 0 trades bloqueados en test: 0 ganancias bloqueadas "
                "(vacío — la activación exige (a) con subconjunto real)"
            )
        elif wr_blocked is not None and wr_blocked < 0.10:
            b_state, b_text = "ok", f"(b) win-rate bloqueado < 10 %: SÍ ({fmt_pct(wr_blocked)})"
        else:
            b_state, b_text = "fail", f"(b) win-rate bloqueado < 10 %: NO ({fmt_pct(wr_blocked)})"

        if rate_pure is None:
            c_state, c_text = "ok", (
                "(c) no aplica estructuralmente: el criterio solo decide sobre "
                "candidatos (NO_CANDIDATE en el resto) → no puede duplicar el "
                "rechazo del modelo"
            )
        elif rate_pure <= 0.40:
            c_state, c_text = "ok", f"(c) pass=False en ≤ 40 % de HOLD puro: SÍ ({fmt_pct(rate_pure)})"
        else:
            c_state, c_text = "fail", f"(c) pass=False en ≤ 40 % de HOLD puro: NO ({fmt_pct(rate_pure)})"

        checks = [(a_state, a_text), (b_state, b_text), (c_state, c_text)]
        block["sanity_lines"].extend(text for _, text in checks)

        theta_used = block["theta_best"] if block["theta_best"] is not None else CRITERIA[cid]["threshold"]
        train_blocked_n = train_t["blocked"]["n"]
        if (
            cid == "C2_executable_size"
            and n_block == 0
            and train_blocked_n == 0
            and a_state == "na"
        ):
            # Umbral de contrato broker (justificado_by ≠ replay): con 0
            # bloqueos evaluables en train Y test es una activación neutra.
            block["verdict"] = "CANDIDATO A ACTIVAR (contrato broker, neutro)"
            block["verdict_reason"] = (
                "0 trades bloqueados en train ni test: sin coste observable; "
                "umbral = settings.min_volume (no tuneable). Requiere aprobación."
            )
        elif all(state == "ok" for state, _ in checks):
            block["verdict"] = f"CANDIDATO A ACTIVAR (θ={theta_used})"
            block["verdict_reason"] = (
                "§6 (a)(b)(c) cumplidos en test — requiere aprobación de Félix "
                "y traza shadow ≥ 7 d / ≥ 50 candidatos"
            )
        else:
            parts = []
            for state, text in checks:
                name = text.split(":")[0].split(" ")[0]
                if state == "fail":
                    parts.append(f"falla {name}")
                elif state == "na":
                    parts.append(f"sin evidencia en {name}")
            block["verdict"] = "MANTENER OFF"
            block["verdict_reason"] = "; ".join(parts) + " (test)"
        analysis["criteria"][cid] = block

    analysis["limitations"] = [
        f"Muestra pequeña ({len(mapped)} trades analizados; test ≈ "
        f"{len(test_ids)}): los IC de la separación son anchos; un test "
        "«positivo» con n_bloq < 10 es débil por construcción — se reporta "
        "tal cual, sin adornos.",
        f"{map_stats['weak_old_excluded']} trades mapeados a la era antigua se "
        "excluyen (formato sin veto/quality/volumen, día 7 de sep anómalo, "
        "Δ≈3–5 min sin el patrón de latencia de ~19 s de la era nueva).",
        "C1 usa `ml_prob` (clip 0.1–0.99) como proxy de la cabeza cruda; la "
        "cabeza directa no se persiste y por debajo de 0.1 la distinción se pierde.",
        "`qwen_degraded` se registró por primera vez el 2026-09-28 (5/557 = "
        "0,9 % true): para filas anteriores al campo se asume False. Enmascarar "
        "C4 por eso dejaría la muestra sin poder; el sesgo residual es ≤ 1 %.",
        "Filas sin quality/regime/volumen → criterio excluido del análisis "
        "(«n sin dato» en las tablas): la ausencia de dato no es el juicio del "
        "criterio y no debe contaminar ni el subconjunto permitido ni el bloqueado.",
        "Ciclos en blackout de noticias o rechazados por la EA no se distinguen "
        "en auditoría: el bloqueo atribuido a un criterio puede solaparse con "
        "rechazos que ya ocurrían (sesgo hacia atribuir bloqueo en exceso).",
        "La racha de pérdidas se reconstruye desde `trade_outcomes` (misma "
        "semántica 3 € del guardián); si el guardián omitió algún cierre, la "
        "reconstrucción difiere de la memoria viva.",
        "C7 no tiene spread histórico (deque vivo desde hoy): queda fuera del "
        "replay por datos, no por método.",
        "Expectancia = pnl real medio: robusta a costes (ya netos) pero "
        "sensible a outliers con n pequeño; por eso el plan exige también "
        "win-rate bloqueado < 10 %.",
    ]

    head = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
        capture_output=True, text=True,
    ).stdout.strip()

    out_path = Path(args.out)
    write_report(
        out_path,
        audit_stats=audit_stats,
        build_stats=build_stats,
        map_stats=map_stats,
        cycles=cycles,
        trades=trades,
        mapped=mapped,
        analysis=analysis,
        head=head,
    )
    write_csv(Path(args.csv), contexts_by_cycle_id)

    print(f"[replay] informe → {out_path}")
    print(f"[replay] csv    → {args.csv}")
    print("[replay] veredictos:")
    for cid, block in analysis["criteria"].items():
        print(f"    {cid}: {block['verdict']} — {block.get('verdict_reason', '')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
