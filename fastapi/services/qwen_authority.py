"""
T-31 Fase B (orden Félix 2026-10-19): «conf válido, estable y razonado» —
la autoridad del veto direccional del LLM se GANA con skill demostrada en
datos reales, no por lo que el modelo afirme de sí mismo.

Por qué existe
--------------
RCA (2026-10-19): el veto leía `qwen["confidence"]`, clave que el dict live
NUNCA devolvía → default 1.0 fantasma → el veto se disparaba SIEMPRE que el
sesgo oponía («conf=1.00» en el 100 % de los logs). Fase A (auditoría ex-post
de 1 140 ciclos) midió la skill REAL del juez:

    - pre-fenómeno (15-09→15-10): 49,0 % (moneda al aire)
    - monocultivo BULLISH (16-10→hoy): 22,0 % (ANTI-PREDICTIVO)
    - el veto acertó el 22 % de las veces en la ventana actual

Es decir: un juez que dice «estoy seguro al 85 %» y acierta el 22 % NO puede
tener autoridad para revertir señales. Pero el modelo tampoco se puede
desconfiar a ciegas — puede mejorar. Por eso la autoridad es CONDICIONAL y
se revisa sola:

    - el veto direccional solo revierte si el juez ha demostrado, en el
      histórico reciente y en vivo, exactitud direccional ≥ QWEN_AUTHORITY_MIN_ACC
      sobre ≥ QWEN_AUTHORITY_MIN_SAMPLES llamadas de alta convicción;
    - si no hay muestra suficiente o la skill no está, el veto queda INERTE
      (log explícito: por qué) y la señal pasa — los vetoes mecánicos
      (posición, calidad, ranging, sizing, riesgo, news, time-stop) siguen
      mandando intactos;
    - la autoridad se REVISA sola: si el juez mejora, recupera el veto sin
      intervención; si empeora, lo pierde sola.

Esto NO es «quitar el veto porque no funciona»: es condicionar su poder a
evidencia medida. El veto sigue existiendo, con umbrales congelados (0.7 /
0.3 sobre la probabilidad real `bias_prob`); lo que cambia es quién lo
habilita.

Criterio de dirección (congelado, T-24 §7 / T-30): coincidencia de
dirección PPO↔LLM. Aquí se mide la skill del LLM aislado (su sesgo frente al
mercado realizado a +24 h), que es la habilidad que el veto invoca.
"""
import asyncio
import bisect
import datetime as dt
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# Umbrales de autoridad (congelados en la decisión de Félix 2026-10-19).
# MIN_SAMPLES: sin muestra suficiente NO hay veredicto — el veto queda inerte
# por ignorancia (no por confianza en el juez).
QWEN_AUTHORITY_MIN_ACC = float(os.getenv("QWEN_AUTHORITY_MIN_ACC", "0.55"))
QWEN_AUTHORITY_MIN_SAMPLES = int(os.getenv("QWEN_AUTHORITY_MIN_SAMPLES", "30"))
# "Skill DEMOSTRADA" (fix 2026-10-20): no basta con no ser peor que el azar —
# el límite inferior del intervalo de Wilson (95 %) debe estar POR ENCIMA del
# 50 % (azar). Motivo (medido el 20-10): con 46 llamadas y 56,5 % de acierto
# la skill es estadísticamente INDISTINGUIBLE del azar (límite inferior
# ≈ 42 %) ⇒ un veto que puede bloquear la única fuente de señal con una
# moneda al aire es exactamente el fallo que T-31 vino a matar (el veto
# anterior acertaba el 15 %). Con este criterio la autoridad sólo se concede
# con evidencia que DISTINGA del azar, y se revoca sola cuando deja de
# distinguirlo.
QWEN_AUTHORITY_REQUIRE_SIGNIFICANCE = (
    os.getenv("QWEN_AUTHORITY_REQUIRE_SIGNIFICANCE", "true").lower() not in ("false", "0", "no")
)
QWEN_AUTHORITY_Z = float(os.getenv("QWEN_AUTHORITY_Z", "1.96"))
# Ventana de medición (días) y cadencia de refresco (s).
QWEN_AUTHORITY_WINDOW_DAYS = int(os.getenv("QWEN_AUTHORITY_WINDOW_DAYS", "14"))
QWEN_AUTHORITY_TTL_S = int(os.getenv("QWEN_AUTHORITY_TTL_S", "1800"))
# Horizonte de verificación: +24 h REALES (mismo horizonte del diseño T-31).
# En BARRAS sería ~35 h por los huecos de la serie H1 (fin de semana/pausa).
QWEN_AUTHORITY_HORIZON_H = 24
# Alta convicción = |bias_prob - 0.5| >= 0.2 (equivale a los umbrales 0.7/0.3).
QWEN_AUTHORITY_MIN_CONVICTION = 0.2

# Estado en memoria por símbolo (TTL) — el cálculo es el mismo para todos
# los pares salvo los datos, pero se cachea por par para no recomputar.
_STATE: dict[str, dict] = {}
_LOCK = asyncio.Lock()


def _to_epoch(bar: dict) -> float:
    t = bar.get("time")
    if isinstance(t, str):
        return dt.datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp()
    return float(t)


async def _measure(symbol: str) -> dict:
    """Mide la skill direccional del LLM en la ventana reciente (live)."""
    from db.connection import get_pool
    from services.mt5_client import get_candles

    pool = get_pool()
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=QWEN_AUTHORITY_WINDOW_DAYS)
    rows = await pool.fetch(
        """
        SELECT (data->'data'->>'mt5_time')::float AS mt5_time,
               data->'data'->>'llm_bias'          AS llm_bias,
               (data->'data'->>'llm_bias_prob')::float AS bias_prob
        FROM audit_log
        WHERE event='ai_predict_cycle'
          AND data->'data'->>'symbol' = $1
          AND data->'data'->>'llm_bias' IN ('BULLISH','BEARISH')
          AND data->'data'->>'llm_bias_prob' IS NOT NULL
          AND (data->'data'->>'mt5_time')::float >= $2
        ORDER BY created_at
        """,
        symbol,
        since.timestamp(),
    )

    result = {
        "symbol": symbol,
        "n": 0,
        "hits": 0,
        "acc": None,
        "granted": False,
        "reason": "sin datos",
        "measured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    if not rows:
        return result

    raw = await get_candles(symbol, "H1", 600)
    # Sólo velas H1 CERRADAS (fix 2026-10-20): la vela en formación lleva el
    # precio ACTUAL como "close" y.tickea. Como cierre terminal del horizonte
    # +24 h la usaba para ~12 ciclos (los de la hora en curso) ⇒ la skill
    # medida oscilaba entre 87 % y 54 % sin que cambiara el modelo (medido:
    # la autoridad concedía/concedía/concedía y luego la misma muestra daba
    # 54 %). Con velas cerradas, el terminal es un cierre REAL.
    now_epoch = dt.datetime.now(dt.timezone.utc).timestamp()
    closed = [c for c in raw if float(c["time"]) + 3600 <= now_epoch]
    pairs = sorted((_to_epoch(c), float(c["close"])) for c in closed)
    if not pairs:
        result["reason"] = "sin velas H1 cerradas"
        return result
    bar_ep = [p[0] for p in pairs]
    bar_close = [p[1] for p in pairs]

    def realized(t0: float) -> str | None:
        """Dirección realizada a +24 h REALES (no +24 barras).

        T-31 (2026-10-19): dos correcciones frente a la primera versión —
        (a) las velas llegan ya en UTC real (services/mt5_client normaliza
            el desfase broker→UTC de +3 h; antes la base quedaba 3 h
            desplazada);
        (b) el horizonte es por TIEMPO: la serie H1 tiene huecos (fin de
            semana, pausa diaria) ⇒ 24 barras ≈ 35 h, no 24 h.
        Se toma el último cierre en o antes de t0 y el último cierre en o
        antes de t0+24 h."""
        c0_i = bisect.bisect_right(bar_ep, t0) - 1
        c1_i = bisect.bisect_right(bar_ep, t0 + QWEN_AUTHORITY_HORIZON_H * 3600) - 1
        if c0_i < 0 or c1_i <= c0_i:
            return None
        d = bar_close[c1_i] - bar_close[c0_i]
        if d > 0.00005:
            return "UP"
        if d < -0.00005:
            return "DOWN"
        return None

    n = 0
    hits = 0
    buckets: dict = {}
    for r in rows:
        bias = r["llm_bias"]
        prob = r["bias_prob"]
        if prob is None or abs(prob - 0.5) < QWEN_AUTHORITY_MIN_CONVICTION:
            continue  # sólo llamadas de alta convicción: el veto nunca
            #             habría actuado sobre las demás
        rd = realized(float(r["mt5_time"]))
        if rd is None:
            continue
        n += 1
        hit = (bias == "BULLISH" and rd == "UP") or (bias == "BEARISH" and rd == "DOWN")
        hits += hit
        # Curva de calibración: acierto por bucket de probabilidad declarada.
        # Responde a la pregunta de T-31: ¿conf 0,75 acierta más que 0,60?
        b = buckets.setdefault(round(prob, 2), {"n": 0, "hits": 0})
        b["n"] += 1
        b["hits"] += hit

    acc = (hits / n) if n else None
    wilson_lo = _wilson_lower_bound(hits, n) if n else None
    result["n"] = n
    result["hits"] = hits
    result["acc"] = acc
    result["wilson_lower"] = wilson_lo
    result["calibration"] = {
        f"{p:.2f}": {"n": b["n"], "hits": b["hits"],
                     "acc": (b["hits"] / b["n"]) if b["n"] else None}
        for p, b in sorted(buckets.items())
    }
    if n < QWEN_AUTHORITY_MIN_SAMPLES:
        result["reason"] = f"muestra insuficiente ({n} < {QWEN_AUTHORITY_MIN_SAMPLES})"
    elif acc is not None and acc < QWEN_AUTHORITY_MIN_ACC:
        result["reason"] = (
            f"skill no demostrada ({acc*100:.1f}% < {QWEN_AUTHORITY_MIN_ACC*100:.0f}% sobre {n})"
        )
    elif (
        QWEN_AUTHORITY_REQUIRE_SIGNIFICANCE
        and wilson_lo is not None
        and wilson_lo <= 0.50
    ):
        result["reason"] = (
            f"skill indistinguible del azar (acc {acc*100:.1f}%, límite inferior "
            f"Wilson 95% {wilson_lo*100:.1f}% ≤ 50% sobre {n} llamadas)"
        )
    else:
        result["granted"] = True
        result["reason"] = (
            f"skill demostrada ({acc*100:.1f}% sobre {n} llamadas; "
            f"límite inferior {wilson_lo*100:.1f}%)"
        )
    return result


def _wilson_lower_bound(hits: int, n: int, z: float = QWEN_AUTHORITY_Z) -> Optional[float]:
    """Límite inferior del intervalo de Wilson al 95 % (proporción de aciertos).

    Es la forma robusta de exigir "mejor que el azar" con muestra pequeña:
    un 56 % sobre 46 llamadas NO es evidencia de nada (límite inferior ≈42 %).
    """
    if not n:
        return None
    p = hits / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5)
    return max(0.0, (centre - margin) / denom)


async def get_authority(symbol: str) -> dict:
    """Autoridad del veto para `symbol` (cacheada con TTL). Nunca lanza."""
    now = dt.datetime.now(dt.timezone.utc).timestamp()
    async with _LOCK:
        entry = _STATE.get(symbol)
        if entry and now - entry["ts"] < QWEN_AUTHORITY_TTL_S:
            return entry["data"]
    try:
        data = await _measure(symbol)
    except Exception as exc:  # noqa: BLE001 — la autoridad NUNCA tumba el ciclo
        logger.warning(
            "[QWEN-AUTH] medición falló para %s (%s: %s) → veto inerte (fail-safe explícito)",
            symbol, type(exc).__name__, exc,
        )
        data = {
            "symbol": symbol,
            "n": 0,
            "hits": 0,
            "acc": None,
            "granted": False,
            "reason": f"medición falló: {type(exc).__name__}",
            "measured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
    async with _LOCK:
        _STATE[symbol] = {"ts": now, "data": data}
    logger.info(
        "[QWEN-AUTH] %s → autoridad=%s (%s)",
        symbol, "CONCEDIDA" if data["granted"] else "REVOCADA", data["reason"],
    )
    if data.get("wilson_lower") is not None:
        logger.info(
            "[QWEN-AUTH] %s skill: %d/%d = %.1f%% (límite inferior Wilson 95%%: %.1f%%)",
            symbol, data["hits"], data["n"], (data["acc"] or 0) * 100,
            data["wilson_lower"] * 100,
        )
    cal = data.get("calibration") or {}
    if cal:
        parts = []
        for p, v in cal.items():
            acc_pct = (v["acc"] * 100) if v.get("acc") is not None else 0.0
            parts.append(f"{p}→{v['hits']}/{v['n']}={acc_pct:.0f}%")
        logger.info(
            "[QWEN-AUTH] %s calibración por bucket de bias_prob: %s",
            symbol, " · ".join(parts),
        )
    return data


def peek(symbol: str) -> dict | None:
    """Última medición cacheada (sin refrescar) — solo para diagnóstico."""
    entry = _STATE.get(symbol)
    return entry["data"] if entry else None


async def refresh_loop(symbol: str, interval_s: int = 900) -> None:
    """Refresco periódico de la autoridad en segundo plano — el TTL también la refresca."""
    while True:
        try:
            await get_authority(symbol)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[QWEN-AUTH] refresh falló para %s: %s", symbol, exc)
        await asyncio.sleep(interval_s)
