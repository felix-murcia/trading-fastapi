"""
Capa Estratega — deliberación por ciclo (plan de optimización 2026-09-30 §4;
D-5: «shadow primero con replay»).

En CADA ciclo /predict que llega a la cadena de decisión se evalúa:

- **Estado narrativo** (§4.1): último cierre (regla 3 €), tesis del trade
  (trade_insights), racha de pérdidas, bars_since_close y movimiento desde el
  cierre en pips, más los inputs de límites de riesgo.
- **Criterios C1–C8** (§4.2): **OFF por defecto** — se activan uno a uno con
  evidencia de replay aprobada (§6, un criterio por PR) en la config
  versionada (`STRATEGIST_VERSION`). Hoy: **C2 activo** (aprobación de Félix
  2026-10-01), el resto OFF.
- **Vetos existentes**: los mismos inputs de la cadena de `routers/ai.py`,
  sin duplicar su precedencia — `vetoes.fired` lo decide `_veto_reason`
  (autoridad de la cadena).

El resultado se embebe en `audit_log.ai_predict_cycle.data.deliberation`:
**autonomía ≠ opacidad** — si el sistema decide ESPERAR, la traza dice por qué.

Contrato SHADOW (plan §6 — nada se activa sin informe de replay aprobado):
- `shadow: true` → el verdict **no se aplica** a la decisión final: la capa
  sigue log-only hasta que se cierre la ventana de sombra (§6: ≥ 7 días o
  ≥ 50 candidatos) y Félix ordene el corte. `active` decide qué criterios
  PUEDEN bloquear el verdict (entrar en `blocked_by` y virar ENTER→WAIT) —
  **activación de un criterio ≠ poder de decisión de la capa**.
- Un criterio activo solo pasa con `pass is True` (duda → ESPERAR, fail-safe).
- `justified_by` documenta la evidencia que permite activarlo; `None` =
  umbral placeholder aún injustificado por replay.

Fuentes — derivadas de estado existente, sin almacenamiento permanente nuevo:

  trade_outcomes ⋈ trade_insights (LEFT JOIN por trade_id) → último cierre + tesis
  risk_guardian (memoria; restaurado de risk_pair_state al arranque) → racha
  risk_account_state (memoria) → inputs de límites (el bloqueo real es de
      order_manager, post-predict — aquí solo se registran los datos)
  velas del ciclo (df) + exit_time → bars_since_close / movimiento en pips
  mt5_client.get_spread → muestras vivas por par (deque 288 ≈ 24 h) para C7

Dominio temporal: `exit_time` (trade_outcomes) y `time` (velas MCP) están
ambos en dominio BROKER — la comparación es válida mientras compartan dominio;
unificar el reloj de extremo a extremo es T-1 (Fase 3).

Errores (sin silencios):
- `deliberate()` **propaga**: la contención vive en la frontera
  (`routers/ai.py` → `verdict=ERROR` + WARNING), testeada en
  `test_strategist_shadow.py::TestRouterDeliberation`.
- El spread degrada SOLO el criterio C7 con `error` explícito: un endpoint
  no debe borrar la traza entera.
"""

import logging
import statistics
from collections import deque
from datetime import datetime
from typing import Optional

import httpx
import pandas as pd

from config import settings
from core.rules import is_winning_close
from db.connection import get_pool
from services.mt5_client import get_spread
from services.risk_guardian import get_account_state, registered_guardians

logger = logging.getLogger(__name__)

# Versión de la config de criterios — cada activación debe venir con bump aquí
# + entrada en README (plan §6: un criterio por PR, config versionada).
# 1.1.0 (2026-10-01): C2 activo con evidencia de replay aprobada por Félix;
# resto de criterios intactos (OFF).
STRATEGIST_VERSION = "1.1.0"

# C7: muestras vivas de spread por par — μ+kσ con k=2 (plan §4.2).
SPREAD_WINDOW = 288        # ≈ 24 h de ciclos de 5 min
MIN_SPREAD_SAMPLES = 30    # mínimo para que μ+σ sea señal y no ruido
SPREAD_K = 2.0

# ─── Criterios C1–C8 (plan §4.2) — OFF POR DEFECTO (C2 activo, ver. 1.1.0) ─────
# `active: false` = trazado sin poder sobre el verdict. `justified_by` =
# evidencia de replay que justifica el umbral; `None` = placeholder pendiente.
CRITERIA: dict[str, dict] = {
    "C1_direction": {
        "active": False,
        "threshold": 0.25,
        "justified_by": None,
        "metric": "abs(direction_head)",
        "notes": (
            "Hoy el único filtro de señal es la banda muerta ±0.05 de "
            "decode_v3_direction; θ pendiente de replay (plan §6)."
        ),
    },
    "C2_executable_size": {
        "active": True,
        "threshold": "min_volume",
        "justified_by": (
            "replay doc/replay-estratega.md (informe del 2026-09-30, aprobado "
            "por Félix 2026-10-01): 0 bloqueos en train (23) ni test (23) de 46 "
            "trades analizados; umbral = settings.min_volume, contrato broker "
            "(no tuneable) — sin coste observable"
        ),
        "metric": "lots",
        "notes": (
            "Equiv. vol_norm ≥ ≈0.11 según plan §4.2; se mide contra el lote "
            "mínimo REAL del broker."
        ),
    },
    "C3_quality": {
        "active": False,
        "threshold": 7.0,
        "justified_by": None,
        "metric": "qwen quality_score",
        "notes": (
            "El veto existente usa 5.0 (QUALITY_THRESHOLD); 7.0 es el "
            "candidato del plan, aún sin evidencia de replay."
        ),
    },
    "C4_thesis_regime": {
        "active": False,
        "threshold": "coherente",
        "justified_by": None,
        "metric": "regime/bias vs dirección de la entrada",
        "notes": (
            "RANGING→no coherente; bias opuesto→no; NEUTRAL o Qwen degradado→"
            "no evaluable (duda, no se afirma)."
        ),
    },
    "C5_change_since_exit": {
        "active": False,
        "threshold": 10.0,
        "justified_by": None,
        "metric": "price_move_since_close_pips",
        "notes": (
            "Solo re-entrada MISMA dirección tras un cierre dentro de la "
            "ventana de velas; replay → X pips (plan §4.2)."
        ),
    },
    "C6_post_loss_evidence": {
        "active": False,
        "threshold": 8.0,
        "justified_by": None,
        "metric": "quality exigido viniendo de racha ≥ 1",
        "notes": (
            "θ_high (C3 alto). El blackout próximo lo cubre el gate previo del "
            "ciclo (velas anterior/actual/siguiente): solo llega aquí quien lo pasó."
        ),
    },
    "C7_costs_vs_edge": {
        "active": False,
        "threshold": "mu+2sigma",
        "justified_by": "plan §4.2: k=2 sobre μ+σ del spread del símbolo",
        "metric": "spread_pips",
        "notes": (
            "Precisa ≥30 muestras vivas del par (deque 288); sin historia → "
            "no evaluable."
        ),
    },
    "C8_late_join": {
        "active": False,
        "threshold": 1.0,
        "justified_by": None,
        "metric": "move_desde_cierre en dirección de la entrada / ATR",
        "notes": "«Movimiento ya consumido»; replay → θ en múltiplos de ATR (plan §4.2).",
    },
}

_LAST_CLOSE_SQL = """
SELECT t.id, t.exit_time, t.exit_reason, t.pnl, t.direction,
       i.regime, i.quality_score, i.qwen_confidence, i.qwen_insight
FROM trade_outcomes t
LEFT JOIN trade_insights i ON i.trade_id = t.id
WHERE t.symbol = $1
ORDER BY t.exit_time DESC, i.id DESC NULLS LAST
LIMIT 1
"""

# Muestras vivas de spread por par (en memoria, ventana deslizante — el plan
# prohíbe almacenamiento permanente nuevo; al reiniciar la ventana empieza de
# 0 y C7 queda «no evaluable» hasta de acumular historial).
_SPREAD_SAMPLES: dict[str, deque] = {}


def _record_spread(symbol: str, spread_pips: float) -> None:
    dq = _SPREAD_SAMPLES.get(symbol)
    if dq is None:
        dq = deque(maxlen=SPREAD_WINDOW)
        _SPREAD_SAMPLES[symbol] = dq
    dq.append(spread_pips)


def _spread_stats(symbol: str) -> dict:
    """μ+2σ del spread del par; `threshold=None` con <30 muestras."""
    dq = _SPREAD_SAMPLES.get(symbol)
    n = len(dq) if dq else 0
    if n < MIN_SPREAD_SAMPLES:
        return {"samples": n, "threshold": None}
    mu = statistics.fmean(dq)
    sigma = statistics.stdev(dq)
    return {
        "samples": n,
        "mu": round(mu, 3),
        "sigma": round(sigma, 3),
        "threshold": round(mu + SPREAD_K * sigma, 3),
    }


async def _sample_spread(symbol: str, pip_size: float) -> tuple[Optional[float], Optional[str]]:
    """Muestra viva de spread para C7 — contención SOLO a nivel de criterio.

    Un fallo del endpoint de precio degrada C7 con `error` explícito en la
    traza; el resto de la deliberación (narrativo, C1–C6, C8) se emite igual.
    """
    try:
        spread_price = await get_spread(symbol)
    except (httpx.HTTPError, ValueError, TypeError, AttributeError) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    spread_pips = float(spread_price) / pip_size
    _record_spread(symbol, spread_pips)
    return round(spread_pips, 3), None


def compute_since_exit(
    df: pd.DataFrame,
    exit_time: datetime,
    current_price: float,
    pip_size: float,
) -> tuple[Optional[int], Optional[float]]:
    """(bars_since_close, price_move_since_close_pips) — puro.

    `exit_time` y `df['time']` comparten dominio (broker, tz-aware UTC): se
    busca el último candel cuyo OPEN ≤ exit_time; los `bars` son las velas
    posteriores (0 = salida dentro de la vela formándose). La aproximación de
    precio es el close de esa vela (trade_outcomes no almacena precio de
    salida — documentado, no inventado).

    (None, None) = la salida es anterior a la ventana disponible → no se
    evalúa (honesto: sin datos, sin valor).
    """
    times = pd.to_datetime(df["time"], utc=True)
    idx = int(times.searchsorted(exit_time, side="right")) - 1
    if idx < 0:
        return None, None
    bars = (len(times) - 1) - idx
    price_then = float(df["close"].iloc[idx])
    move_pips = (float(current_price) - price_then) / pip_size
    return bars, round(move_pips, 2)


async def fetch_narrative(
    symbol: str,
    *,
    df: pd.DataFrame,
    current_price: float,
    pip_size: float,
    equity: float,
) -> dict:
    """Estado narrativo (§4.1) — deriva de tablas/estado existentes. Propaga."""
    pool = get_pool()
    row = await pool.fetchrow(_LAST_CLOSE_SQL, symbol)

    last_exit = None
    thesis = None
    bars: Optional[int] = None
    move_pips: Optional[float] = None
    if row is not None:
        pnl = float(row["pnl"])
        last_exit = {
            "exit_time": row["exit_time"].isoformat(),
            "exit_reason": row["exit_reason"],
            "pnl": pnl,
            "direction": row["direction"],
            "is_win": is_winning_close(pnl),   # regla 3 € (core.rules)
        }
        bars, move_pips = compute_since_exit(df, row["exit_time"], current_price, pip_size)
        if row["qwen_insight"] is not None:
            thesis = {
                "regime": row["regime"],
                "quality_score": float(row["quality_score"]) if row["quality_score"] is not None else None,
                "confidence": float(row["qwen_confidence"]) if row["qwen_confidence"] is not None else None,
                "insight": str(row["qwen_insight"])[:280],
            }

    # Racha: el guardián en memoria es la fuente viva (restaurada de
    # risk_pair_state al arranque y flush-eada en CADA ciclo previo — línea
    # flush_risk_state(symbol=...) de ai.py). Sin guardián → None honesto,
    # nunca un 0 inventado.
    guardian = registered_guardians().get(symbol)
    consecutive_losses = int(guardian.state.consecutive_losses) if guardian is not None else None

    # Inputs de límites (crudos, sin fórmulas duplicadas): el bloqueo real lo
    # evalúa order_manager vía risk_guardian.check_risk (post-predict).
    account = get_account_state()
    limits = {
        "equity": float(equity),
        "daily_start_equity": float(account.daily_start_equity),
        "peak_equity": float(account.peak_equity),
        "daily_loss_limit_pct": float(guardian.state.daily_loss_limit_pct) if guardian is not None else None,
        "max_drawdown_block_pct": settings.max_drawdown_block_pct,
    }

    return {
        "last_exit": last_exit,
        "thesis": thesis,
        "consecutive_losses": consecutive_losses,
        "bars_since_close": bars,
        "price_move_since_close_pips": move_pips,
        "limits": limits,
    }


def _c(
    cid: str,
    passed: Optional[bool],
    value,
    detail: Optional[str] = None,
    *,
    threshold_override=None,
) -> dict:
    """Resultado de UN criterio: {active, pass, value, threshold, justified_by}.

    `pass is None` = no evaluable / no aplica (diferente de «pasa»: la traza
    jamás confunde «sin datos» con «evidencia a favor»).
    """
    cfg = CRITERIA[cid]
    if threshold_override is not None:
        threshold = threshold_override
    elif cfg["threshold"] == "min_volume":
        threshold = float(settings.min_volume)
    else:
        threshold = cfg["threshold"]
    out = {
        "active": bool(cfg["active"]),
        "pass": passed,
        "value": value,
        "threshold": threshold,
        "justified_by": cfg["justified_by"],
    }
    if detail is not None:
        out["detail"] = detail
    return out


def evaluate_criteria(
    *,
    candidate: str,
    narrative: dict,
    qwen: dict,
    qwen_degraded: bool,
    direction_head: float,
    lots: float,
    current_atr: float,
    pip_size: float,
    spread_pips: Optional[float],
    spread_error: Optional[str],
    spread_stats: dict,
) -> dict:
    """Criterios C1–C8 sobre el estado del ciclo — puro. Nunca lanza por
    ausencia de datos: la duda se registra como `pass: None` + `detail`."""
    quality = float(qwen.get("quality", 0.0))
    regime = str(qwen.get("regime", ""))
    bias = str(qwen.get("bias", "NEUTRAL"))
    last_exit = narrative.get("last_exit")
    move_pips = narrative.get("price_move_since_close_pips")
    consecutive = narrative.get("consecutive_losses")
    is_entry = candidate in ("BUY", "SELL")

    # C1 — señal no marginal (chattering)
    c1_val = abs(float(direction_head))
    c1 = _c("C1_direction", c1_val >= float(CRITERIA["C1_direction"]["threshold"]), round(c1_val, 4))

    # C2 — tamaño ejecutable (contrato broker: lote mínimo)
    c2 = _c(
        "C2_executable_size",
        float(lots) >= float(settings.min_volume),
        round(float(lots), 3),
    )

    # C3 — calidad Qwen
    thr3 = float(CRITERIA["C3_quality"]["threshold"])
    c3 = _c("C3_quality", quality >= thr3, round(quality, 2))

    # C4 — coherencia tesis/régimen
    if qwen_degraded:
        c4 = _c("C4_thesis_regime", None, f"{regime}/{bias}", "qwen degradado: sin tesis fiable")
    elif not is_entry:
        c4 = _c("C4_thesis_regime", None, f"{regime}/{bias}", "sin candidato de entrada")
    elif regime == "RANGING":
        c4 = _c("C4_thesis_regime", False, f"{regime}/{bias}", "entrada en régimen lateral")
    elif (candidate == "BUY" and bias == "BEARISH") or (candidate == "SELL" and bias == "BULLISH"):
        c4 = _c("C4_thesis_regime", False, f"{regime}/{bias}", "bias opuesto a la dirección")
    elif bias == "NEUTRAL":
        c4 = _c("C4_thesis_regime", None, f"{regime}/{bias}", "bias NEUTRAL: sin tesis de dirección")
    else:
        c4 = _c("C4_thesis_regime", True, f"{regime}/{bias}", "regime y bias coherentes con la entrada")

    # C5 — cambio desde la salida (solo re-entrada MISMA dirección)
    thr5 = float(CRITERIA["C5_change_since_exit"]["threshold"])
    if not is_entry:
        c5 = _c("C5_change_since_exit", None, move_pips, "sin candidato de entrada")
    elif last_exit is None:
        c5 = _c("C5_change_since_exit", None, move_pips, "sin salida previa")
    else:
        same_dir = (
            (candidate == "BUY" and last_exit["direction"] == "LONG")
            or (candidate == "SELL" and last_exit["direction"] == "SHORT")
        )
        if not same_dir:
            c5 = _c("C5_change_since_exit", None, move_pips, "dirección distinta a la última salida")
        elif move_pips is None:
            c5 = _c("C5_change_since_exit", None, None, "salida fuera de la ventana de velas: no evaluable")
        else:
            c5 = _c("C5_change_since_exit", move_pips >= thr5, round(float(move_pips), 2))

    # C6 — evidencia post-pérdida (racha ≥ 1 exige calidad alta)
    thr6 = float(CRITERIA["C6_post_loss_evidence"]["threshold"])
    if consecutive is None:
        c6 = _c("C6_post_loss_evidence", None, None, "racha no disponible (sin guardián del par)")
    elif int(consecutive) == 0:
        c6 = _c("C6_post_loss_evidence", True, 0, "sin racha de pérdidas")
    else:
        c6 = _c(
            "C6_post_loss_evidence",
            quality >= thr6,
            int(consecutive),
            f"racha={int(consecutive)}: exige quality ≥ {thr6} (gate de blackout ya superado)",
        )

    # C7 — costes < edge (spread vs μ+2σ vivo del par)
    if spread_error is not None:
        c7 = _c("C7_costs_vs_edge", None, None, "spread no disponible")
        c7["error"] = spread_error
    elif spread_stats.get("threshold") is None:
        c7 = _c(
            "C7_costs_vs_edge",
            None,
            spread_pips,
            f"samples={spread_stats.get('samples', 0)} < {MIN_SPREAD_SAMPLES}: sin historia",
        )
    else:
        thr7 = float(spread_stats["threshold"])
        c7 = _c(
            "C7_costs_vs_edge",
            float(spread_pips) <= thr7,
            spread_pips,
            f"n={spread_stats['samples']} μ={spread_stats['mu']} σ={spread_stats['sigma']}",
            threshold_override=thr7,
        )

    # C8 — no incorporarse tarde (movimiento desde el cierre / ATR)
    thr8 = float(CRITERIA["C8_late_join"]["threshold"])
    if not is_entry:
        c8 = _c("C8_late_join", None, None, "sin candidato de entrada")
    elif last_exit is None:
        c8 = _c("C8_late_join", None, None, "sin salida previa")
    elif move_pips is None:
        c8 = _c("C8_late_join", None, None, "salida fuera de la ventana de velas: no evaluable")
    else:
        atr_pips = float(current_atr) / pip_size
        if atr_pips <= 0:
            c8 = _c("C8_late_join", None, None, "ATR no disponible")
        else:
            signed = float(move_pips) if candidate == "BUY" else -float(move_pips)
            norm = signed / atr_pips
            c8 = _c(
                "C8_late_join",
                norm <= thr8,
                round(norm, 3),
                "normalizado a ATR en la dirección de la entrada",
            )

    return {
        "C1_direction": c1,
        "C2_executable_size": c2,
        "C3_quality": c3,
        "C4_thesis_regime": c4,
        "C5_change_since_exit": c5,
        "C6_post_loss_evidence": c6,
        "C7_costs_vs_edge": c7,
        "C8_late_join": c8,
    }


def _verdict(
    candidate: str,
    veto_reason: Optional[str],
    criteria: dict,
) -> tuple[str, list[str]]:
    """ENTER solo si el candidato existe, ningún veto disparó y todos los
    criterios ACTIVOS evalúan `pass is True` (la duda → ESPERAR)."""
    blocked: list[str] = []
    if veto_reason is not None:
        blocked.append(f"veto:{veto_reason}")
    if candidate not in ("BUY", "SELL"):
        return "NO_CANDIDATE", blocked
    for cid in CRITERIA:
        c = criteria[cid]
        if c["active"] and c["pass"] is not True:
            blocked.append(cid)
    if blocked:
        return "WAIT", blocked
    return "ENTER", []


async def deliberate(
    *,
    symbol: str,
    candidate: str,
    veto_reason: Optional[str],
    qwen: dict,
    qwen_degraded: bool,
    direction_head: float,
    lots: float,
    sizing_below_min: bool,
    env_position: int,
    df: pd.DataFrame,
    current_price: float,
    current_atr: float,
    pip_size: float,
    equity: float,
    quality_threshold: float,
) -> dict:
    """Deliberación de UN ciclo: narrativo + C1–C8 + vetos + verdict.

    SHADOW: el verdict se registra, no se aplica. Propaga errores de fuentes
    (la contención es de la frontera en routers/ai.py).
    """
    narrative = await fetch_narrative(
        symbol, df=df, current_price=current_price, pip_size=pip_size, equity=equity
    )
    spread_pips, spread_error = await _sample_spread(symbol, pip_size)
    stats = _spread_stats(symbol)

    criteria = evaluate_criteria(
        candidate=candidate,
        narrative=narrative,
        qwen=qwen,
        qwen_degraded=qwen_degraded,
        direction_head=direction_head,
        lots=lots,
        current_atr=current_atr,
        pip_size=pip_size,
        spread_pips=spread_pips,
        spread_error=spread_error,
        spread_stats=stats,
    )
    verdict, blocked_by = _verdict(candidate, veto_reason, criteria)

    regime = str(qwen.get("regime", ""))
    bias = str(qwen.get("bias", "NEUTRAL"))
    quality = float(qwen.get("quality", 0.0))
    return {
        "verdict": verdict,
        "shadow": True,
        "config_version": STRATEGIST_VERSION,
        "blocked_by": blocked_by,
        # Vetos actuales integrados en la MISMA traza: `fired` es lo que la
        # cadena real (_veto_reason) decidió; los booleanos son sus inputs.
        # `news_blackout` solo existe como False: los ciclos en blackout salen
        # antes (gate de velas anterior/actual/siguiente) y no llegan aquí.
        "vetoes": {
            "fired": veto_reason,
            "position_open": env_position != 0,
            "news_blackout": False,
            "ranging": regime == "RANGING",
            "quality_below": quality < float(quality_threshold),
            "bias_opposed": (
                (candidate == "BUY" and bias == "BEARISH")
                or (candidate == "SELL" and bias == "BULLISH")
            ),
            "sizing_below_min": bool(sizing_below_min),
        },
        "narrative": narrative,
        "criteria": criteria,
    }
