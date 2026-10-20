"""
ForexTradingEnv v3 — Arquitectura Óptima para RL (PPO)
=====================================================
- Recompensa diferencial telescópica: R_t = (Equity_t - Equity_{t-1}) / Balance_0.
- Ejecución realista OHLC: Detección de SL/TP intra-vela por High/Low con sesgo conservador.
- Espacio de acción continuo optimizado: Zona muerta reducida a [-0.05, 0.05] para preservar gradientes.
- Observación extendida: Distancias normalizadas a SL/TP por ATR y tiempo transcurrido en el trade.
- Manejo correcto de fin de episodio: Bootstrapping en 'truncated' sin liquidaciones forzadas artificiales.
- P2-1 / D3 (train/serve unificado): la acción es [direction, volume]; los stops
  salen de `production_stops()` — MISMA fórmula que `/api/v1/ai/predict` — y el
  trailing del EA está modelado (`_apply_trailing`). El MDP entrenado es el MDP
  ejecutado: lo que el modelo aprende es exactamente lo que el sistema ejecuta.
- P2-2: el sizing está delegado en `services.position_sizing.size_ppo_position`
  (MISMA función que `/predict`): la cabeza `volume` modula el riesgo entre 0 y
  `risk_pct` del balance; volumen 0 → sin entrada (paridad con el HOLD por
  sizing inviable de `/predict`).
- Fase 3 (plan 2026-09-30, ejecutado 2026-10-01):
  * Memoria: canal 8º `bars_since_close` (normalizado a max_holding_steps) —
    contrato obs `(20,24) → (20,25)`; `memory_channel=False` evalúa el
    contrato legado de un bundle pre-memoria (transición).
  * T-22 (2026-10-16, REQ-1): bloque de tendencia compartido PPO↔LLM —
    `TREND_MARKET_FEATURES` (dist_sma200, sma200_slope, adx) entra al obs
    sólo con el contrato trend (obs 27/28, ver `derive_obs_contract`);
    `trend_features=False` (default) conserva el contrato vivo byte-idéntico.
  * R-1 / regla 3 €: un close con pnl ∈ (0, 3 €) es PÉRDIDA también en la
    recompensa (su aporte se invierte; el balance de dinero no se toca).
  * Anti-churn (Félix, 2026-10-01; ancla declarada O-2, 2026-10-01): reabrir
    con ≤ CHURN_BARS velas desde el último cierre paga la fricción completa
    de ida+vuelta (volume × churn_friction_per_lot × 2) como penalización de
    reward. El ancla (7 €/lot, plan §5 «fricción solo comisión») va SEPARADA
    de la comisión del bróker: es un parámetro de política, y la comisión
    real del EURUSD es 0 (271/271 deals sin commission/fee, evidencia
    2026-10-01) — antes la comisión decidía el recargo y con la unidad v1
    (0.0001) quedaba inerte.
- O-1 / paridad `position_veto` (diagnóstico 2026-10-01): un flip de dirección
  con posición abierta NO cierra ni revierte — el env mantiene la posición,
  igual que producción (serve: `position_rejected` → HOLD; EA:
  `rejected_position_open` L407/L440/L475). Sólo SL/TP (incl. trailing) y la
  acción FLAT (→ CLOSE) terminan un trade.
"""

from datetime import datetime
from typing import Optional, Tuple
import gymnasium as gym
from gymnasium import spaces
import numpy as np
import pandas as pd

from core.rules import is_winning_close

# P2-2: el sizing del PPO vive en position_sizing (misma función que /predict)
from services.position_sizing import size_ppo_position


MARKET_FEATURES = (
    "spread", "real_volume",
    "returns", "range", "sma20", "dist_sma20",
    "macd", "macd_signal", "macd_hist", "tr", "atr",
    "rsi14", "bb_pos", "lag_return_1", "lag_return_2",
    "lag_return_3", "lag_return_5",
)
# Fase 3 (SL/TP estructural, backlog H3/P0): swing_high/low, last_swing_*,
# resistance/support_level, dist_* y adx se calculan en `engineer_market_features`
# como columnas del df y alimentan `production_stops()`/Qwen por AMBOS lados del
# contrato. T-22 (2026-10-16): el obs sigue con 17 features de mercado por
# defecto ((20,24)→(20,25) sólo con el canal de memoria); el bloque de
# tendencia (`TREND_MARKET_FEATURES`) entra al obs SÓLO cuando el contrato
# del bundle lo declara (obs 27/28 → `derive_obs_contract`), y añadir canales
# exige retrain pre-registrado de por medio (T-27).

# T-22 (2026-10-16, REQ-1): bloque de tendencia compartido PPO↔LLM. `adx` ya
# existía (normalizada [0,1]); `dist_sma200`/`sma200_slope` se calculan en
# `engineer_market_features` con el MISMO close/rolling(200)/shift(5) que
# `_sma200_context` del prompt (mismo hecho, mismo signo — el LLM lo renderiza
# en crudo, el PPO normalizado). NO entra en MARKET_FEATURES (17 base): el
# contrato se decide por el ANCHO del obs (`derive_obs_contract`), así el
# bundle vivo (20,25) sigue servible intacto hasta que T-27 promueva (20,28).
TREND_MARKET_FEATURES = ("dist_sma200", "sma200_slope", "adx")

# T-30 (2026-10-17, orden Félix «ejecución del candidato H4/D1»): bloque de
# horizonte LENTO — las 4 coordenadas del estado de la tendencia ~41d (H4) y
# ~250d (D1) que el LLM ya ve en su prompt (`_higher_tf_context_line`), ahora
# también en el obs del PPO (REQ-1 a nivel de representación: mismos datos,
# misma fórmula, mismo signo). Pre-registrado en
# audits/t30-pre-registration-hf-obs-2026-10-17.md §2 — UN candidato, sin
# ADX/RSI H4/D1 (8 canales descartados: superficie de tuning injustificada).
# NO entra en MARKET_FEATURES: el contrato lo decide el ancho del obs
# (derive_obs_contract 31/32), flag `higher_tf_features` default off.
# Las columnas se UNEN al df desde las velas H4/D1 (join asof estricto de
# vela CERRADA, ver `join_higher_tf_channels`) — no se calculan del H1.
HIGHER_TF_MARKET_FEATURES = (
    "dist_sma200_h4", "slope_sma200_h4", "dist_sma200_d1", "slope_sma200_d1",
)

# Ancho base del tensor: canales de mercado + 7 de estado. El contrato Fase 3
# añade +1 (bars_since_close) → obs (20, 25); 24 canales = bundle pre-memoria.
# T-22: +3 tendencia → (20,27)/(20,28); derive_obs_contract resuelve los 4.
FOREX_MARKET_FEATURE_COUNT = len(MARKET_FEATURES)

EXTRA_STATE_FEATURES = 7  # [entry_norm, pos, unrealized_pnl, balance_norm, dist_sl, dist_tp, holding_time]
# Fase 3: +1 canal opcional `bars_since_close` (obs 25) — ver memory_channel.

# T-22: ventana mínima de velas H1 para un obs trend HONESTO en serve —
# 204 filas de warm-up (SMA200 formado desde la fila 199; la pendiente a 5
# barras necesita fila 204) + las 20 del obs. Por debajo, /predict emite
# HOLD con error explícito: jamás se sirve un obs con tendencia de relleno
# (cero inventado). El entrenamiento (30000 H1) no lo alcanza nunca.
TREND_FEATURE_WINDOW_MIN = 204 + 20

# Filtro de volatilidad para evitar mercados de ruido (P2: mejora de selectividad)
MIN_ATR_FOR_ENTRY = 0.0004 

# Anti-churn: reabrir con <= CHURN_BARS velas H1 completas desde el último
# cierre paga la fricción completa de ida+vuelta (churn_friction_per_lot × 2
# por lot) — ancla de política declarada, no coste de bróker (ver O-2).
CHURN_BARS = 2

# P1 / H4 (backlog 2026-10-02; parámetro confirmado por Félix 2026-10-08):
# time-stop — una posición abierta >= TIME_STOP_BARS velas H1 se cierra al
# mark de esa vela (corta posiciones estancadas 12 h+, pierna SL tóxica).
# Serve convierte a la unidad que mide el EA: TIME_STOP_BARS × 12 pasos M5
# (PeriodSeconds(H1)/PeriodSeconds(M5)) — ver routers/ai.py `_TIME_STOP_M5_BARS`.
TIME_STOP_BARS = 12

# T-24 (2026-10-17) — candidato «Sat-Align», DISEÑO PRE-REGISTRADO en
# audits/t24-reward-redesign-2026-10-17.md §4. Constantes CONGELADAS por
# reglas de anclaje a-priori (d0/λ/s0 con anclas anti-hacking): NO retunear
# — si el gate T-27 falla, se reporta y se para (protocolo anti-multiple-
# testing). Dos términos sobre v1 sin tocar la economía (Δe/B0×S, churn,
# regla 3 €, risk_penalty intactos):
#   (a) saturación: recargo creciente sólo para |dir| más allá del techo
#       d0 — higiene de la decisión saturada (decode_v3 es magnitud-libre →
#       el término es autoextinguible);
#   (b) alineación: recompensa por mantener la posición EN el sentido del
#       régimen de tendencia (slope ∧ adx) — supervisiona el atajo
#       momentum+intercepto medido en T-25. Usa los canales del bloque de
#       tendencia T-22 (mismo slope/adx que pregunta al LLM → REQ-1) y es
#       neutro en warm-up (slope relleno a 0).
REWARD_V2_D0_CEIL = 0.90         # onset del techo; sat(d)=max(0,|d|-d0)²/(1-d0)²
REWARD_V2_LAMBDA_SAT = 0.001     # × reward_scale → 0,10 u a pleno techo
REWARD_V2_S0_SLOPE = 0.0002      # pendiente mediana |slope| en régimen tendencial
REWARD_V2_LAMBDA_ALIGN = 0.000298  # × reward_scale → 0,0298 u/paso (ancla anti-
                                 # hacking: 0,25 × |pnl| típico 14,315 € por
                                 # trade de 12 barras / (1000 × 12))
REWARD_V2_ADX_TREND = 0.25       # mismo umbral ADX tendencial del bloque de régimen
REWARD_V2_VERSIONS = ("v1", "v2_align")  # contrato del parámetro reward_version

# T-31.9 / L3 (2026-10-20) — reward «v3_edge»: SOLO el término direccional.
#
# CAUSA RAÍZ medida (punto de inflexión del 20-10): la reward v1 codifica las
# REGLAS DE NEGOCIO, no el edge:
#   (a) la regla de 3 € INVIERTE el reward de cualquier close en (0, 3 €);
#   (b) el churn cobra 7 €/lot (×2) en cada reapertura.
# Con esos dos términos, el óptimo del agente es «operar poco y sólo en
# movimientos grandes» — que es EXACTAMENTE lo que hace el bundle promovido
# (16 operaciones en el holdout, DD 4,51 %) — y NO «acertar la dirección».
# Medido: con la reward v1 el PPO acierta el 50,7 % (n=146): moneda al aire.
# El edge de la ventana lo tiene la ESTRUCTURA (SMA200+pendiente: 66,7 %,
# CI 55,4 %) y el LLM (69,6 %, CI 55,2 %) — no el momentum (39,8 %, invertido).
#
# SEPARACIÓN DE OBJETIVOS (L3): la reward aprende SÓLO la dirección; las
# reglas de negocio se quedan donde son exactas y auditables — la CAPA DE
# EJECUCIÓN (core.rules.MIN_WIN_EUR, el veto de re-entrada, el ancla de
# churn, SL/TP, sizing, límites de pérdida, risk guardian). En el reward
# quedan: el término de equity (direccional), el risk_penalty (restricciones
# de riesgo del propio agente: filtro ATR + capital en riesgo) y el feedback
# de outcomes REALES (con el pnl CRUDO, sin la inversión de 3 €).
# Los costes reales (spread, comisión) siguen dentro del Δequity: no se
# esconde ningún coste, sólo se sacan las REGLAS DE NEGOCIO del gradiente.
REWARD_V3_VERSIONS = ("v1", "v2_align", "v3_edge")
# Horizonte de la métrica de dirección (mismo que el benchmark L2: +24 h).
REWARD_V3_DIRECTION_HORIZON_BARS = 24


def engineer_market_features(df: pd.DataFrame) -> pd.DataFrame:
    """Calcula indicadores técnicos normalizados y acotados para redes neuronales.

    Orden temporal (P0): el MCP entrega velas DESCENDENTES (más nueva primero)
    y `ml/train_ppo_v3.py` ordena ascendente su CSV inicial — sin un punto de
    normalización común, inferencia y retrain calculaban las features HACIA
    ATRÁS: `returns` con signo invertido, `lag_return_*` = vela futura,
    `iloc[-1]` ("actual") = la vela de hace días, walk-forward y split del env
    simulando en tiempo inverso, y el modelo (entrenado ascendente) servido con
    datos espejados (train/serve skew desde el despliegue: 0 retrains en
    training_experiments). Esta función es el choke point por el que pasan
    TODOS los caminos (inferencia `routers/ai.py`, retrain `auto_retrain`,
    entrenamiento y tests): con columna `time`, la serie se ordena
    cronológicamente ascendente — la convención de todo el sistema
    (iloc[-1]=actual, tail(n)=recientes, lag=vela anterior, split pasado→futuro).
    """
    result = df.copy()
    if "time" in result.columns:
        result = result.sort_values("time").reset_index(drop=True)
    close = result["close"].astype(float)
    high = result["high"].astype(float)
    low = result["low"].astype(float)

    result["spread"] = result.get("spread", 0.0)
    if "real_volume" not in result:
        result["real_volume"] = result.get("tick_volume", result.get("volume", 0.0))

    result["returns"] = close.pct_change().fillna(0.0)
    result["range"] = (high - low) / (close + 1e-10)
    result["sma20"] = close.rolling(20).mean()
    result["dist_sma20"] = (close - result["sma20"]) / (close + 1e-10)

    # T-22 · bloque de tendencia compartido PPO↔LLM (REQ-1) — mismos números
    # que `_sma200_context` del prompt (mismo close/rolling(200)/shift(5); el
    # signo es idéntico por construcción: LLM lo renderiza en crudo, el PPO
    # normalizado). Clip defensivo acotado (espejo de bb_pos) y warm-up
    # (SMA200 aún no formado, filas 0..203) rellena con el neutro 0.0 =
    # «precio EN la media, pendiente plana» (mismo patrón que adx→0.5): sin
    # el relleno, dropna() vaciaría enteras las rutas de ventana corta (serve
    # bundle vivo con 120 velas, fixtures de test). El obs del bundle trend se
    # alimenta con ≥ TREND_FEATURE_WINDOW_MIN velas (guard en serve), donde
    # sus 20 últimas filas son SMA200 REAL — nunca ve valores de relleno.
    sma200 = close.rolling(200).mean()
    result["dist_sma200"] = np.clip((close - sma200) / (close + 1e-10), -0.1, 0.1)
    result["sma200_slope"] = np.clip((sma200 - sma200.shift(5)) / (close + 1e-10), -0.1, 0.1)
    result["dist_sma200"] = result["dist_sma200"].fillna(0.0)
    result["sma200_slope"] = result["sma200_slope"].fillna(0.0)

    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    result["macd"] = macd / (close + 1e-10)
    result["macd_signal"] = signal / (close + 1e-10)
    result["macd_hist"] = (macd - signal) / (close + 1e-10)

    # True Range con gaps de apertura
    prev_close = close.shift(1)
    tr_components = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1)
    result["tr"] = tr_components.max(axis=1) / (close + 1e-10)
    result["atr"] = result["tr"].rolling(14).mean()

    # ADX (Average Directional Index) — fuerza de la tendencia
    # +DI = 100 * EMA((high - prev_high).clip(lower=0)) / ATR
    # -DI = 100 * EMA((prev_low - low).clip(lower=0)) / ATR
    # DX = 100 * |+DI - -DI| / (+DI + -DI)
    # ADX = EMA(DX, 14)
    prev_high = high.shift(1)
    prev_low = low.shift(1)
    plus_dm = (high - prev_high).clip(lower=0)
    minus_dm = (prev_low - low).clip(lower=0)
    plus_di = 100 * (plus_dm.rolling(14, min_periods=1).mean() / (result["atr"] + 1e-10))
    minus_di = 100 * (minus_dm.rolling(14, min_periods=1).mean() / (result["atr"] + 1e-10))
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di + 1e-10)
    result["adx"] = dx.rolling(14, min_periods=1).mean() / 100.0  # normalizado [0, 1]
    # Rellenar NaN iniciales con valor neutral (0.5) para no perder filas
    result["adx"] = result["adx"].fillna(0.5)

    # RSI normalizado en rango [0, 1]
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / (loss + 1e-10)
    result["rsi14"] = (100.0 - (100.0 / (1.0 + rs))) / 100.0

    # Bollinger Bands Position acotada para mitigar explosión de outliers
    std20 = close.rolling(20).std()
    bb_raw = (close - result["sma20"]) / (2.0 * std20 + 1e-10)
    result["bb_pos"] = np.clip(bb_raw, -3.0, 3.0)

    for lag in (1, 2, 3, 5):
        result[f"lag_return_{lag}"] = result["returns"].shift(lag).fillna(0.0)

    # ============================================================
    # Swing High/Low Detection (Fase 3: SL/TP Estructural)
    # ============================================================
    # Swing High: high mayor que los 2 vecinos a cada lado
    # Swing Low: low menor que los 2 vecinos a cada lado
    swing_window = 2
    result["swing_high"] = (
        (result["high"] > result["high"].shift(1)) &
        (result["high"] > result["high"].shift(2)) &
        (result["high"] > result["high"].shift(-1)) &
        (result["high"] > result["high"].shift(-2))
    ).astype(float)
    result["swing_low"] = (
        (result["low"] < result["low"].shift(1)) &
        (result["low"] < result["low"].shift(2)) &
        (result["low"] < result["low"].shift(-1)) &
        (result["low"] < result["low"].shift(-2))
    ).astype(float)

    # Propagar el último swing high/low conocido hacia adelante (forward fill)
    # para saber el swing relevante actual en cada vela
    result["last_swing_high"] = result["high"].where(result["swing_high"] == 1).ffill()
    result["last_swing_low"] = result["low"].where(result["swing_low"] == 1).ffill()

    # ============================================================
    # Liquidity Zones / Support/Resistance (Fase 3: TP Estructural)
    # ============================================================
    # Niveles donde el precio ha reaccionado múltiples veces
    # Usamos clusters de swing highs/lows como proxy de liquidez
    liquidity_window = 50
    def _resistance(x):
        if len(x) == 0:
            return np.nan
        max_val = np.nanmax(x)
        count = np.sum(x == max_val)
        return max_val if count >= 1 else np.nan
    
    def _support(x):
        if len(x) == 0:
            return np.nan
        min_val = np.nanmin(x)
        count = np.sum(x == min_val)
        return min_val if count >= 1 else np.nan
    
    result["resistance_level"] = result["high"].rolling(liquidity_window, min_periods=1).apply(_resistance, raw=True)
    result["support_level"] = result["low"].rolling(liquidity_window, min_periods=1).apply(_support, raw=True)
    
    # Rellenar cualquier NaN residual con forward/backward fill
    result["resistance_level"] = result["resistance_level"].ffill().bfill()
    result["support_level"] = result["support_level"].ffill().bfill()

    # Distancias normalizadas a niveles clave (para features)
    result["dist_resistance"] = (result["resistance_level"] - close) / (close + 1e-10)
    result["dist_support"] = (close - result["support_level"]) / (close + 1e-10)

    # Rellenar NaN iniciales - usar ffill/bfill para mantener filas.
    # fillna final: en un mercado plano NO se detecta ningún swing y una
    # columna totalmente NaN vaciaría el dropna — se rellena con el propio
    # high/low (distancia estructural < 10 pips → production_stops cae al
    # fallback ATR igual que sin datos estructurales).
    result["last_swing_high"] = result["last_swing_high"].bfill().ffill().fillna(high)
    result["last_swing_low"] = result["last_swing_low"].bfill().ffill().fillna(low)
    result["resistance_level"] = result["resistance_level"].bfill().ffill()
    result["support_level"] = result["support_level"].bfill().ffill()
    result["dist_resistance"] = result["dist_resistance"].fillna(0)
    result["dist_support"] = result["dist_support"].fillna(0)

    # Rellenar NaN iniciales (ADX ya hecho arriba)
    result["adx"] = result["adx"].fillna(0.5)

    # Descartar sólo las filas de warmup de los indicadores de ventana fija
    # (SMA20/std20 → 19 filas; ATR/RSI → 13, cubiertas por esas 19) y sólo
    # DESPUÉS de ordenar ascendente: se descartan las velas MÁS VIEJAS, nunca
    # las nuevas (P0: test_p0_time_order). dropna exige cero NaN en el resto:
    # por eso cada feature (ADX, swings, liquidez, bloque de tendencia T-22)
    # se rellena arriba.
    result = result.dropna().reset_index(drop=True)

    return result


def get_market_features(df: pd.DataFrame, include_trend: bool = False,
                        include_higher_tf: bool = False) -> list[str]:
    """Valida la presencia ordenada del vector de features de mercado.

    T-22 (REQ-1): `include_trend=True` añade `TREND_MARKET_FEATURES` al FINAL
    del vector — misma función, mismo orden en train (`ForexTradingEnvV2`) y
    serve (`/predict`): paridad por construcción. El default False conserva
    byte-idéntico el vector de los 17 base (bundle vivo (20,25)).

    T-30: `include_higher_tf=True` añade `HIGHER_TF_MARKET_FEATURES` después
    del bloque de tendencia — y EXIGE sus columnas en el df (ValueError si
    faltan): los canales H4/D1 llegan por join desde las velas superiores
    (`join_higher_tf_channels`), su ausencia no puede rellenarse con ceros
    (un obs de horizonte lento inventado sería exactamente el atajo que este
    candidato existe para destruir). Sólo válido con `include_trend=True`
    (el contrato 31/32 implica tendencia — validate en el caller).
    """
    features = list(MARKET_FEATURES)
    if include_trend:
        features += list(TREND_MARKET_FEATURES)
    if include_higher_tf:
        missing = [f for f in HIGHER_TF_MARKET_FEATURES if f not in df.columns]
        if missing:
            raise ValueError(
                f"include_higher_tf exige las columnas H4/D1 en el df (faltan: "
                f"{missing}) — únelas con join_higher_tf_channels antes"
            )
        features += list(HIGHER_TF_MARKET_FEATURES)
    for feature in features:
        if feature not in df.columns:
            df[feature] = 0.0
    return features


def derive_obs_contract(obs_width: Optional[int]) -> Optional[Tuple[bool, bool, bool]]:
    """Deriva (trend_features, higher_tf_features, memory_channel) del ancho.

    T-22: única fuente de verdad del contrato por ancho — la usan ambos lados
    (`routers/ai.load_inference_artifacts` en serve y
    `services.auto_retrain._evaluate_model` en retrain), de modo que un bundle
    se construye igual donde se mire. Anchuras válidas:

    - 24 = 17 market + 7 state            → pre-memoria legado (Fase 3)
    - 25 = 24 + memoria                   → CONTRATO VIVO (20,25)
    - 27 = 20 market + 7 state            → trend pre-memoria (T-22)
    - 28 = 27 + memoria                   → objetivo T-27 (20,28)
    - 31 = 24 market + 7 state            → T-30 pre-memoria (trend + HF)
    - 32 = 31 + memoria                   → CANDIDATO T-30 (20,32)

    Devuelve None si el ancho no corresponde a ningún contrato construible:
    el llamador decide el RuntimeError contextual (sin degradación silenciosa).
    """
    base_width = FOREX_MARKET_FEATURE_COUNT + EXTRA_STATE_FEATURES
    trend_width = base_width + len(TREND_MARKET_FEATURES)
    hf_width = trend_width + len(HIGHER_TF_MARKET_FEATURES)
    if obs_width == base_width:
        return (False, False, False)
    if obs_width == base_width + 1:
        return (False, False, True)
    if obs_width == trend_width:
        return (True, False, False)
    if obs_width == trend_width + 1:
        return (True, False, True)
    if obs_width == hf_width:
        return (True, True, False)
    if obs_width == hf_width + 1:
        return (True, True, True)
    return None


def join_higher_tf_channels(
    df: pd.DataFrame, h4_candles: list, d1_candles: list,
) -> pd.DataFrame:
    """T-30: une los 4 canales del horizonte lento (H4/D1) al df H1.

    Pre-registrado (audits/t30-pre-registration §3): para cada barra H1 en t,
    la feature usa SÓLO información con timestamp ≤ t — la barra H4/D1 se
    considera usable cuando ya está CERRADA (`bar.time + timeframe ≤ t`,
    búsqueda con `searchsorted(side='right')`: la vela D1 de las 00:00 de hoy
    no entra hasta las 00:00 de mañana; cero lookahead, incluido el bloque
    asof de 24 h documentado en R3).

    Fórmulas = MISMAS que el bloque de tendencia T-22 y que `_sma200_context`
    del prompt del LLM (paridad REQ-1): rolling(200) sobre la serie superior,
    dist = (close_hf − sma_hf)/(close_hf + 1e-10) y slope = (sma_hf −
    sma_hf.shift(5))/(close_hf + 1e-10), clip ±0,1 — el denominador es el
    close de la barra SUPERIOR (la misma referencia en crudo que el LLM
    renderiza: `obs × (close_hf + ε)` reconstruye sus {dist:+.4f}/{slope:+.4f}).

    Devuelve el df con las 4 columnas; NaN donde la barra cerrada aún no
    existe o el SMA200 no está formado — EL CALLER decide (train: RuntimeError
    explícito; serve: HOLD fail-closed). Relleno jamás: un canal de horizonte
    lento en cero es «sin tendencia» = mentira de estado.
    """
    if "time" not in df.columns:
        raise ValueError("join_higher_tf_channels exige df H1 con columna `time`")

    def _epoch(v) -> int:
        """Cualquier formato de `time` del sistema → epoch segundos UTC.

        El MCP devuelve ISO-strings (\"2026-10-16T23:00:00Z\"), mt5_client
        convierte a epoch numérico y los CSV del parche de agujero traen
        datetime — por elemento, sin adivinar unidades (los numéricos son
        segundos, convención _to_ts; jamás milisegundos en este sistema).
        """
        if isinstance(v, str):
            return int(pd.to_datetime(v, utc=True).timestamp())
        if isinstance(v, bool) or v is None:
            raise ValueError(f"time no convertible: {v!r}")
        if isinstance(v, (int, float)):
            return int(v)
        ts = getattr(v, "timestamp", None)  # datetime / pd.Timestamp
        if ts is not None:
            return int(ts())
        raise ValueError(f"time no convertible: {v!r}")

    out = df.copy()
    h1_times = np.array([_epoch(v) for v in out["time"]], dtype=np.int64)
    for tf_key, candles in (("h4", h4_candles), ("d1", d1_candles)):
        if not candles:
            raise ValueError(f"join_higher_tf_channels: sin velas {tf_key.upper()}")
        times = np.array([_epoch(c["time"]) for c in candles], dtype=np.int64)
        closes = np.array([float(c["close"]) for c in candles], dtype=np.float64)
        order = np.argsort(times, kind="stable")
        times, closes = times[order], closes[order]
        sma = pd.Series(closes).rolling(200).mean().to_numpy()
        slope_raw = sma - np.concatenate((np.full(5, np.nan), sma[:-5]))
        # Cierre efectivo de cada barra superior: usable en t si ≤ t.
        tf_seconds = 4 * 3600 if tf_key == "h4" else 24 * 3600
        closed_at = times + tf_seconds
        idx = np.searchsorted(closed_at, h1_times, side="right") - 1
        valid = idx >= 0
        sel_close = np.where(valid, closes[np.clip(idx, 0, None)], np.nan)
        sel_sma = np.where(valid, sma[np.clip(idx, 0, None)], np.nan)
        sel_slope = np.where(valid, slope_raw[np.clip(idx, 0, None)], np.nan)
        denom = sel_close + 1e-10
        out[f"dist_sma200_{tf_key}"] = np.clip(
            (sel_close - sel_sma) / denom, -0.1, 0.1
        )
        out[f"slope_sma200_{tf_key}"] = np.clip(sel_slope / denom, -0.1, 0.1)
    return out


def build_v3_observation(
    market_values: np.ndarray,
    expected_market_features: int,
    position: int,
    entry_price: float,
    current_price: float,
    sl_price: float,
    tp_price: float,
    current_atr: float,
    steps_in_trade: int,
    max_holding_steps: int,
    balance: float,
    initial_balance: float,
    window_size: int = 20,
    bars_since_close_norm: Optional[float] = None,
) -> np.ndarray:
    """Genera el tensor de estado (window_size, total_features) invariante al par de divisas.

    Fase 3: `bars_since_close_norm` (0 = cerró en esta vela, 1 = ≥
    max_holding_steps o sin cierre) añade el 8º canal de estado → obs
    (20, 25). `None` = contrato legado (20, 24) de un bundle pre-memoria.
    """
    last_market = np.asarray(market_values[-window_size:], dtype=np.float32)
    feats_present = last_market.shape[1]

    if feats_present < expected_market_features:
        pad = np.zeros((window_size, expected_market_features - feats_present), dtype=np.float32)
        last_market = np.hstack([last_market, pad])
    elif feats_present > expected_market_features:
        last_market = last_market[:, :expected_market_features]

    atr_safe = max(current_atr, 1e-5)
    
    if position != 0 and entry_price > 0:
        entry_norm_val = (current_price - entry_price) / entry_price
        unrealized_pct = (current_price - entry_price) / entry_price if position == 1 else (entry_price - current_price) / entry_price
        dist_sl = (current_price - sl_price) / (atr_safe * current_price) if position == 1 else (sl_price - current_price) / (atr_safe * current_price)
        dist_tp = (tp_price - current_price) / (atr_safe * current_price) if position == 1 else (current_price - tp_price) / (atr_safe * current_price)
        holding_norm = min(1.0, steps_in_trade / max(1, max_holding_steps))
    else:
        entry_norm_val = 0.0
        unrealized_pct = 0.0
        dist_sl = 0.0
        dist_tp = 0.0
        holding_norm = 0.0

    state_rows = [
        entry_norm_val,
        float(position),
        unrealized_pct * 10.0,
        balance / initial_balance,
        np.clip(dist_sl, -5.0, 5.0),
        np.clip(dist_tp, -5.0, 5.0),
        holding_norm,
    ]
    if bars_since_close_norm is not None:
        state_rows.append(float(np.clip(bars_since_close_norm, 0.0, 1.0)))

    state_extensions = np.tile(
        np.array(state_rows, dtype=np.float32),
        (window_size, 1)
    )

    return np.hstack([last_market, state_extensions])


def decode_v3_direction(direction: float, current_position: int) -> Tuple[int, str]:
    """
    Decodifica la acción con zona muerta reducida (5%) para evitar mesetas de gradiente nulo.
    1: LONG, -1: SHORT, 0: FLAT/CLOSE.
    """
    if direction < -0.05:
        target_pos = -1
    elif direction > 0.05:
        target_pos = 1
    else:
        target_pos = 0

    if target_pos == 1:
        decision = "HOLD" if current_position == 1 else "BUY"
    elif target_pos == -1:
        decision = "HOLD" if current_position == -1 else "SELL"
    else:
        decision = "CLOSE" if current_position != 0 else "FLAT"

    return target_pos, decision


def production_stops(
    atr_norm: float,
    pip_size: float,
    # Fase 3: Parámetros opcionales para SL/TP estructural
    swing_high: Optional[float] = None,
    swing_low: Optional[float] = None,
    resistance_level: Optional[float] = None,
    support_level: Optional[float] = None,
    current_price: Optional[float] = None,
    entry_price: Optional[float] = None,
    position: int = 0,
) -> Tuple[float, float]:
    """Stops de producción (P2-1 + Fase 3): única fuente de verdad del cálculo SL/TP.

    La usan AMBOS lados del contrato train/serve:
    - el env al abrir posiciones (entrenamiento y evaluación walk-forward), y
    - `/api/v1/ai/predict` al proponer `sl_pips`/`tp_pips` de la decisión.

    Regla Fase 3 (SL/TP Estructural):
    - SL: Swing Low (LONG) / Swing High (SHORT) ± buffer ATR
    - TP: Resistance (LONG) / Support (SHORT) ± buffer ATR
    - Fallback a ATR si no hay datos estructurales: pips ATR = atr_norm / pip_size; 
      SL = clamp(pips × 1.5, 10, 50); TP = 2 × SL.

    Buffer ATR: 0.5 × ATR para dar espacio al ruido.
    """
    # Fallback ATR-based calculation
    atr_pips = atr_norm / pip_size
    atr_pips_clamped = max(1.0, min(100.0, atr_pips))  # clamp razonable
    buffer_pips = max(5.0, atr_pips * 0.5)  # Buffer = 0.5 ATR, mínimo 5 pips
    
    sl_pips = 0.0
    tp_pips = 0.0
    
    if position == 1:  # LONG
        # SL: Swing Low - buffer
        if swing_low is not None and swing_low > 0:
            structural_sl = (current_price - swing_low) / pip_size - buffer_pips
            sl_pips = max(10.0, min(50.0, structural_sl)) if structural_sl >= 10.0 else max(10.0, min(50.0, atr_pips * 1.5))
        else:
            sl_pips = max(10.0, min(50.0, atr_pips * 1.5))  # Fallback ATR
        
        # TP: Resistance + buffer — mínimo 2:1 R:R
        if resistance_level is not None and resistance_level > current_price:
            structural_tp = (resistance_level - current_price) / pip_size + buffer_pips
            # Usar TP estructural solo si respeta mínimo 2:1 R:R
            tp_pips = max(sl_pips * 2.0, structural_tp) if structural_tp >= sl_pips * 2.0 else sl_pips * 2.0
        else:
            tp_pips = sl_pips * 2.0  # Fallback 2:1 R:R
            
    elif position == -1:  # SHORT
        # SL: Swing High + buffer
        if swing_high is not None and swing_high > 0:
            structural_sl = (swing_high - current_price) / pip_size - buffer_pips
            sl_pips = max(10.0, min(50.0, structural_sl)) if structural_sl >= 10.0 else max(10.0, min(50.0, atr_pips * 1.5))
        else:
            sl_pips = max(10.0, min(50.0, atr_pips * 1.5))
        
        # TP: Support - buffer — mínimo 2:1 R:R
        if support_level is not None and support_level < current_price:
            structural_tp = (current_price - support_level) / pip_size + buffer_pips
            tp_pips = max(sl_pips * 2.0, structural_tp) if structural_tp >= sl_pips * 2.0 else sl_pips * 2.0
        else:
            tp_pips = sl_pips * 2.0
    else:
        # Flat - fallback ATR
        sl_pips = max(10.0, min(50.0, atr_pips * 1.5))
        tp_pips = sl_pips * 2.0
    
    # Clamp final
    sl_pips = max(10.0, min(100.0, sl_pips))
    tp_pips = max(15.0, min(200.0, tp_pips))
    
    return sl_pips, tp_pips


class ForexTradingEnvV2(gym.Env):
    """
    Entorno Forex con simulación estocástica por episodios, PnL estándar y recompensa incremental.
    """
    metadata = {'render_modes': ['human']}

    def __init__(
        self,
        df: pd.DataFrame,
        window_size: int = 20,
        initial_balance: float = 10000.0,
        lot_size: float = 100000.0,
        max_lot: float = 0.5,
        risk_pct: float = 0.03,       # P2-2: espejo de settings.ppo_risk_pct (tope de riesgo/trade)
        pip_size: float = 0.0001,
        spread_pips: float = 1.5,
        commission_per_lot: float = 7.0,   # comisión de bróker €/lot/lado (EURUSD real = 0, evidencia O-2 2026-10-01)
        churn_friction_per_lot: float = 7.0,  # ancla de política anti-churn (decisión Félix "fricción completa", 7 €/lot)
        max_episode_steps: int = 1000,
        max_holding_steps: int = 120,
        reward_scale: float = 100.0,
        reward_version: str = "v1",   # T-24: "v1" (byte-idéntico, default) | "v2_align"
        random_reset: bool = True,
        real_outcomes: Optional[list] = None,
        real_outcome_weight: float = 0.1,
        trailing_enabled: bool = True,
        breakeven_enabled: bool = True,
        trailing_percent: float = 0.25,
        trailing_min_pips: float = 15.0,
        trailing_atr_mult: float = 2.0,
        min_stop_dist_pips: float = 1.0,
        memory_channel: bool = True,
        trend_features: bool = False,
        higher_tf_features: bool = False,   # T-30: canales H4/D1 (obs 31/32)
    ):
        super(ForexTradingEnvV2, self).__init__()
        self.df = df.reset_index(drop=True)
        self.window_size = window_size
        self.initial_balance = initial_balance
        self.lot_size = lot_size
        self.max_lot = max_lot
        self.risk_pct = risk_pct
        self.pip_size = pip_size
        self.spread = spread_pips * pip_size
        self.commission_per_lot = commission_per_lot
        self.churn_friction_per_lot = churn_friction_per_lot
        self.max_episode_steps = max_episode_steps
        self.max_holding_steps = max_holding_steps
        self.reward_scale = reward_scale
        # T-24: versión de la recompensa. Contrato declarado (v1/v2_align);
        # cualquier otro valor es un error EXPLÍCITO — jamás caer a v1 en
        # silencio (convención del repo: sin fallbacks).
        if reward_version not in REWARD_V3_VERSIONS:
            raise ValueError(
                f"reward_version={reward_version!r} desconocida; "
                f"esperadas {REWARD_V3_VERSIONS} (T-24 + T-31.9/L3)."
            )
        self.reward_version = reward_version
        self.random_reset = random_reset

        # Trailing espejo de los inputs del EA (mql5 §V): EnableTrailing,
        # TrailingPercent, TrailingMinPips, TrailingATRMult y stops-level
        # (SYMBOL_TRADE_STOPS_LEVEL ≈ 1 pip). P2-1: el env modela lo que
        # el EA ejecuta.
        self.trailing_enabled = trailing_enabled
        # P1: espejo del input independiente `EnableBreakeven` del EA — el EA
        # puede activar el breakeven sin el trailing, y el env debe modelar
        # esa combinación (paridad D3).
        self.breakeven_enabled = breakeven_enabled
        self.trailing_percent = trailing_percent
        self.trailing_min_pips = trailing_min_pips
        self.trailing_atr_mult = trailing_atr_mult
        self.min_stop_dist_pips = min_stop_dist_pips

        self.real_outcomes = real_outcomes or []
        self.real_outcome_weight = real_outcome_weight
        self._real_outcomes_by_step = self._index_real_outcomes(self.real_outcomes)
        self._used_outcomes = set()

        # Fase 3: contrato de observación. True (canonical) = 8 canales de
        # estado → obs (20, 25). False = legado pre-memoria (20, 24), sólo
        # para evaluar bundles viejos (auto_retrain._evaluate_model decide
        # según observation_space del modelo que se evalúa).
        self.memory_channel = bool(memory_channel)
        # T-22: bloque de tendencia en el obs (20 market → obs 27/28). False
        # (default) = contrato vivo de 17 base, byte-idéntico. Quien decide es
        # derive_obs_contract(obs_width) — ver auto_retrain/routers.ai.
        self.trend_features = bool(trend_features)
        # T-30: bloque de horizonte lento H4/D1 (obs 31/32) — REQUIERE el
        # bloque de tendencia (el contrato 31/32 = 20+3+4 market); sin él,
        # ValueError explícito (cero degradación silenciosa a otro ancho).
        self.higher_tf_features = bool(higher_tf_features)
        if self.higher_tf_features and not self.trend_features:
            raise ValueError(
                "higher_tf_features=True exige trend_features=True "
                "(contrato T-30: 24 market = 20 T-22 + 4 H4/D1)"
            )
        # Memoria desde el último cierre (0 = cerró en esta vela; el arranque
        # usa max_holding_steps → normalizado a 1.0 = "sin cierre reciente").
        self.bars_since_close = int(max_holding_steps)
        self.ever_closed = False
        self.trade_pnls: list = []

        self.features = get_market_features(
            self.df, include_trend=self.trend_features,
            include_higher_tf=self.higher_tf_features,
        )
        # P2-1 / D3: el modelo decide [direction, volume]; SL/TP los pone
        # production_stops() igual que en /predict (cabezas muertas fuera).
        self.action_space = spaces.Box(
            low=np.array([-1.0, 0.0], dtype=np.float32),
            high=np.array([1.0, 1.0], dtype=np.float32),
            dtype=np.float32
        )
        self.observation_space = spaces.Box(
            low=-np.inf,
            high=np.inf,
            shape=(
                window_size,
                len(self.features) + EXTRA_STATE_FEATURES + (1 if self.memory_channel else 0),
            ),
            dtype=np.float32
        )

        # Variables de estado
        self.current_step = 0
        self.start_step = 0
        self.position = 0
        self.entry_price = 0.0
        self.volume = 0.0
        self.sl_price = 0.0
        self.tp_price = 0.0
        self.sl_pips = 0.0
        self.tp_pips = 0.0
        self.steps_in_trade = 0

        self.balance = self.initial_balance
        self.equity = self.initial_balance
        self.prev_equity = self.initial_balance
        self.max_equity = self.initial_balance

        self.trade_count = 0
        self.opening_count = 0   # aperturas REALES (integridad métrica gate, fix 2026-10-01)
        self.wins = 0
        self.losses = 0

    def _index_real_outcomes(self, outcomes: list) -> dict:
        if not outcomes or 'time' not in self.df.columns:
            return {}
        try:
            time_col_ts = pd.to_datetime(self.df['time'], utc=True).apply(lambda x: x.timestamp())
        except Exception:
            return {}

        indexed = {}
        for outcome in outcomes:
            entry_t = self._coerce_timestamp(outcome.entry_time)
            if entry_t is None:
                continue
            matches = self.df.index[time_col_ts >= entry_t].tolist()
            if matches and matches[0] not in indexed:
                indexed[int(matches[0])] = outcome
        return indexed

    @staticmethod
    def _coerce_timestamp(value) -> Optional[float]:
        if isinstance(value, (int, float)): return float(value)
        if isinstance(value, datetime): return value.timestamp()
        if isinstance(value, str):
            try: return float(value)
            except ValueError:
                try: return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
                except Exception: return None
        return None

    def _get_observation(self) -> np.ndarray:
        current_candle = self.df.iloc[self.current_step]
        current_price = float(current_candle['close'])
        current_atr = float(current_candle.get('atr', 0.001))

        market_slice = self.df[self.features].iloc[
            self.current_step - self.window_size + 1 : self.current_step + 1
        ].values

        bars_norm = None
        if self.memory_channel:
            bars_norm = min(
                1.0, self.bars_since_close / max(1, self.max_holding_steps)
            )

        return build_v3_observation(
            market_values=market_slice,
            expected_market_features=len(self.features),
            position=self.position,
            entry_price=self.entry_price,
            current_price=current_price,
            sl_price=self.sl_price,
            tp_price=self.tp_price,
            current_atr=current_atr,
            steps_in_trade=self.steps_in_trade,
            max_holding_steps=self.max_holding_steps,
            balance=self.balance,
            initial_balance=self.initial_balance,
            window_size=self.window_size,
            bars_since_close_norm=bars_norm,
        )

    def reset(self, seed=None, options=None):
        super().reset(seed=seed)

        # T-31.9 / L3 — métrica de DIRECCIÓN (criterio de aceptación):
        # ¿acertó el agente la dirección del mercado? Se registra POR TRADE
        # (1 = el mercado fue a su favor, 0 = en contra) usando el movimiento
        # del precio a +24 h desde la apertura — el MISMO horizonte y la
        # MISMA definición que el benchmark L2 (así la comparación es
        # directa: candidato vs SMA200-trend vs LLM).
        # Es una MEDIDA POST-HOC: no influye en la decisión del agente (el
        # df completo está disponible en el env, pero el agente sólo ve la
        # ventana de observación).
        self.direction_hits: list = []
        self._pending_direction: Optional[dict] = None

        max_start = len(self.df) - self.max_episode_steps - 2
        if self.random_reset and max_start > self.window_size:
            self.current_step = int(self.np_random.integers(self.window_size, max_start))
        else:
            self.current_step = self.window_size

        self.start_step = self.current_step
        self.position = 0
        self.entry_price = 0.0
        self.volume = 0.0
        self.sl_price = 0.0
        self.tp_price = 0.0
        self.sl_pips = 0.0
        self.tp_pips = 0.0
        self.steps_in_trade = 0
        self.bars_since_close = int(self.max_holding_steps)
        self.ever_closed = False
        self.trade_pnls = []

        self.balance = self.initial_balance
        self.equity = self.initial_balance
        self.prev_equity = self.initial_balance
        self.max_equity = self.initial_balance

        self.trade_count = 0
        self.opening_count = 0
        self.wins = 0
        self.losses = 0
        self._used_outcomes.clear()

        return self._get_observation(), {}

    def step(self, action):
        action = np.asarray(action, dtype=np.float32).reshape(-1)
        if action.size not in (2, 4):
            raise ValueError(
                f"Acción con {action.size} elementos; se esperan 2 ([direction, volume], "
                "contrato P2-1) o 4 (bundle legado pre-P2-1, evaluable en el gate "
                "walk-forward). Tamaños distintos no son un contrato conocido."
            )
        # P2-1 / D3: solo direction+volume; las cabezas sl/tp (legado 4-dim) se
        # ignoran — el MDP no las consume, igual que /predict.
        dir_val, vol_norm = float(action[0]), float(action[1])
        target_pos, _ = decode_v3_direction(dir_val, self.position)

        candle = self.df.iloc[self.current_step]
        high_price = float(candle['high'])
        low_price = float(candle['low'])
        close_price = float(candle['close'])

        close_reason = None
        exit_price = 0.0
        closed_this_step = False
        last_realized_pnl = 0.0
        churn_penalty = 0.0

        # P1 / H4: la edad de la posición avanza ANTES del bloqueo de salidas
        # para que el time-stop mida las velas completas al cierre de ESTA
        # vela (apertura en T → cierre en T+TIME_STOP_BARS = 12 velas exactas).
        # Estados finales idénticos al incremento antiguo (sección 6): todo
        # cierre o apertura resetea steps_in_trade a 0 por delante.
        if self.position != 0:
            self.steps_in_trade += 1

        # -------------------------------------------------------------
        # 1. Chequeo de salidas intra-vela (High/Low)
        # -------------------------------------------------------------
        if self.position == 1:
            hit_sl = low_price <= self.sl_price
            hit_tp = high_price >= self.tp_price
            if hit_sl:
                close_reason = 'sl'
                exit_price = self.sl_price
            elif hit_tp:
                close_reason = 'tp'
                exit_price = self.tp_price

        elif self.position == -1:
            hit_sl = high_price >= self.sl_price
            hit_tp = low_price <= self.tp_price
            if hit_sl:
                close_reason = 'sl'
                exit_price = self.sl_price
            elif hit_tp:
                close_reason = 'tp'
                exit_price = self.tp_price

        # -------------------------------------------------------------
        # 1b. P1 / H4: TIME-STOP — posición >= TIME_STOP_BARS velas H1 →
        #     cierre al mark de esta vela. Prioridad: SL/TP (precio,
        #     intra-vela) gana sobre el cierre por tiempo; el FLAT del
        #     agente (sección 2) queda DESPUÉS — con la misma edad decide
        #     la salida por tiempo (cierra con cualquier acción).
        # -------------------------------------------------------------
        if close_reason is None and self.position != 0 and self.steps_in_trade >= TIME_STOP_BARS:
            close_reason = 'time_stop'
            exit_price = close_price - (self.spread / 2.0) if self.position == 1 else close_price + (self.spread / 2.0)

        # -------------------------------------------------------------
        # 2. Cierre discrecional del agente (FLAT → CLOSE)
        # -------------------------------------------------------------
        # O-1 / paridad position_veto (diagnóstico 2026-10-01): un flip
        # (target == -position) NO cierra ni revierte. En serve el flip con
        # posición abierta es `position_rejected` → HOLD (routers/ai.py) y el
        # EA lo rechaza en tres sitios (rejected_position_open): la posición
        # vive hasta SL/TP/CLOSE. Antes el env cerraba y revertía aquí mismo —
        # 56 de 89 cierres de sep-2026 eran flips imposibles en producción.
        if close_reason is None and self.position != 0:
            if target_pos == 0:
                close_reason = 'agent'
                exit_price = close_price - (self.spread / 2.0) if self.position == 1 else close_price + (self.spread / 2.0)

        # -------------------------------------------------------------
        # 3. Liquidación si la posición finaliza
        # -------------------------------------------------------------
        if close_reason is not None:
            gross_diff = (exit_price - self.entry_price) if self.position == 1 else (self.entry_price - exit_price)
            realized_pnl = (self.volume * self.lot_size * gross_diff) - (self.volume * self.commission_per_lot)

            # T-31.9 / L3: acierto DIRECCIONAL del trade (1 = el mercado fue a
            # su favor a +24 h desde la apertura; 0 = en contra). Es la
            # métrica comparable con el benchmark L2. Post-hoc: no influye en
            # la decisión.
            if self._pending_direction is not None:
                self.direction_hits.append(
                    1 if self._pending_direction == self.position else 0
                )
            self._pending_direction = None

            self.balance += realized_pnl
            self.equity = self.balance
            self.trade_count += 1
            self.trade_pnls.append(float(realized_pnl))
            # Fase 3: memoria desde el cierre (0 = esta vela) + flag para el
            # recargo anti-churn (sin cierre previo no hay churn que penalizar).
            self.bars_since_close = 0
            self.ever_closed = True
            closed_this_step = True
            last_realized_pnl = float(realized_pnl)
            # Convención AGENTS.md: cierre < MIN_WIN_EUR (3€) = pérdida
            # también en simulación — el win rate del entrenamiento mide
            # la misma regla que el mercado real.
            if is_winning_close(realized_pnl):
                self.wins += 1
            else:
                self.losses += 1

            self.position = 0
            self.entry_price = 0.0
            self.volume = 0.0
            self.sl_price = 0.0
            self.tp_price = 0.0
            self.steps_in_trade = 0

        # Fase 3: si no hubo cierre en ESTE paso, avanza la memoria — al
        # final de la vela el obs muestra las velas completas desde el
        # cierre (paridad con serve: 0 = cerró en esta vela). Un cierre en
        # esta misma vela lo reinicia a 0 (por delante de este incremento).
        if not closed_this_step:
            self.bars_since_close += 1

        # -------------------------------------------------------------
        # 4. Apertura de nueva posición
        # -------------------------------------------------------------
        risk_penalty = 0.0
        if self.position == 0 and target_pos != 0:
            # P2-1: mismos stops que /predict (production_stops) — el modelo ya
            # no elige SL/TP (D3): el MDP entrenado usa los stops ejecutados.
            sl_pips_c, tp_pips_c = production_stops(
                float(candle["atr"]), self.pip_size,
                swing_high=float(candle.get("last_swing_high", 0)) or None,
                swing_low=float(candle.get("last_swing_low", 0)) or None,
                resistance_level=float(candle.get("resistance_level", 0)) or None,
                support_level=float(candle.get("support_level", 0)) or None,
                current_price=close_price,
                position=target_pos,
            )
            # P2-2: sizing delegado en position_sizing.size_ppo_position (misma
            # función que /predict): la cabeza `volume` modula el riesgo
            # (0…risk_pct del balance) y el pip_value es pip_size × lot_size
            # (la convención de dinero de este env).
            new_volume = size_ppo_position(
                volume_norm=vol_norm,
                sl_pips=sl_pips_c,
                equity=self.balance,
                pip_value=self.pip_size * self.lot_size,
                risk_pct=self.risk_pct,
                max_lot=self.max_lot,
            )
            if new_volume > 0.0:
                # Anti-churn (Félix 2026-10-01): reabrir habiendo pasado ≤
                # CHURN_BARS velas desde el último cierre paga la fricción
                # completa de ida+vuelta (churn_friction_per_lot × 2 por lot)
                # — el re-entry inmediato sólo se hace si el edge lo justifica.
                # O-2 (2026-10-01): el recargo usa el ancla DECLARADA aparte de
                # la comisión del bróker (real = 0 → antes el recargo era ≈0
                # y la decisión "fricción completa" no cobraba nada).
                # Sin cierre previo en el episodio (ever_closed) no aplica.
                if self.ever_closed and self.bars_since_close <= CHURN_BARS:
                    churn_penalty = (
                        2.0 * self.churn_friction_per_lot * new_volume
                        / self.initial_balance
                    ) * self.reward_scale

                self.position = target_pos
                self.volume = new_volume
                self.sl_pips, self.tp_pips = sl_pips_c, tp_pips_c
                self.steps_in_trade = 0
                # Único sitio del env donde 0 → posición con volumen > 0: la
                # métrica `openings` del gate cuenta APERTURAS REALES aquí,
                # no la intención de la política (fix 2026-10-01, O-4).
                self.opening_count += 1
                
                # Penalización por volatilidad insuficiente (Félix, 2026-10-06: mejora de selectividad)
                if float(candle["atr"]) < MIN_ATR_FOR_ENTRY:
                    risk_penalty += 0.05

                # Aplicar spread al precio de entrada
                if self.position == 1:
                    self.entry_price = close_price + (self.spread / 2.0)
                    self.sl_price = self.entry_price - (self.sl_pips * self.pip_size)
                    self.tp_price = self.entry_price + (self.tp_pips * self.pip_size)
                else:
                    self.entry_price = close_price - (self.spread / 2.0)
                    self.sl_price = self.entry_price + (self.sl_pips * self.pip_size)
                    self.tp_price = self.entry_price - (self.tp_pips * self.pip_size)

                # Coste de transacción inmediato en equidad
                opening_commission = self.volume * self.commission_per_lot
                self.balance -= opening_commission
                self.equity = self.balance

                # Penalización suave por capital en riesgo inicial (fracción del balance arriesgada)
                capital_at_risk = self.volume * self.lot_size * (self.sl_pips * self.pip_size)
                risk_penalty = (capital_at_risk / self.balance) * 0.02

                # T-31.9 / L3: registra la dirección del mercado a +24 h desde
                # la apertura (para la métrica de dirección). El acierto se
                # evalúa al cerrar. Si el futuro no está disponible (últimas
                # barras del segmento) no se registra (sin dato, sin valor).
                self._pending_direction = self._future_direction_sign()

        elif self.position != 0:
            # (P1: la edad `steps_in_trade` ya avanzó al inicio del paso)
            # Mark-to-market con precio Bid/Ask de cierre
            mark_price = close_price - (self.spread / 2.0) if self.position == 1 else close_price + (self.spread / 2.0)
            unrealized = (self.volume * self.lot_size * (mark_price - self.entry_price)) if self.position == 1 else (self.volume * self.lot_size * (self.entry_price - mark_price))
            self.equity = self.balance + unrealized

        # -------------------------------------------------------------
        # 4b. Trailing stop espejo del EA (P2-1) — se calcula con el close de
        #     esta vela y rige desde la SIGUIENTE (sin lookahead: el EA lo hace
        #     con ticks dentro de la vela; se aproxima sin ver el futuro).
        #     P1: el breakeven se evalúa DESPUÉS (paridad con el orden de
        #     llamada del EA: ManageTrailingStop → ManageBreakeven).
        # -------------------------------------------------------------
        if self.position != 0:
            self._apply_trailing(close_price, float(candle["atr"]))
            self._apply_breakeven(close_price)

        # -------------------------------------------------------------
        # 5. Cálculo del Reward Diferencial (Moody & Saffell)
        # -------------------------------------------------------------
        delta_equity = self.equity - self.prev_equity
        reward = (delta_equity / self.initial_balance) * self.reward_scale - risk_penalty

        # --- Detección de régimen (ADX) ---
        # ADX < 0.25 → mercado lateral/rango (sin tendencia clara)
        # ADX > 0.25 → tendencia direccional
        # Si ADX es NaN (datos insuficientes) → asumir NO ranging (conservador)
        current_adx = float(candle.get("adx", 0.0))
        if np.isnan(current_adx):
            current_adx = 0.5  # neutral: no penalizar por falta de datos
        is_ranging = current_adx < 0.25  # umbral estándar ADX < 25 → rango

        # R-1 / regla 3 € (plan D-2): un close con pnl ∈ (0, 3 €) NO es una
        # ganancia — la recompensa lo trata como PÉRDIDA (el aporte se invierte:
        # delta ya lo sumó positivo, restar 2× lo convierte en −pnl). El
        # balance de dinero real queda intacto; sólo cambia la señal de
        # aprendizaje. Así un candidato que sobrevive de closes <3 € no puede
        # acumular reward positivo.
        #
        # T-31.9 / L3 (v3_edge): la regla 3 € es una REGLA DE NEGOCIO (se
        # aplica en la capa de ejecución: core.rules.MIN_WIN_EUR), no un
        # objetivo de aprendizaje ⇒ en v3_edge NO se invierte (el gradiente
        # queda sólo con la dirección). v1/v2_align: byte-idénticos.
        if (
            self.reward_version != "v3_edge"
            and closed_this_step
            and last_realized_pnl > 0.0
            and not is_winning_close(last_realized_pnl)
        ):
            reward -= 2.0 * (last_realized_pnl / self.initial_balance) * self.reward_scale

        # Anti-churn (Félix 2026-10-01): recargo cobrado en la apertura.
        # T-31.9 / L3 (v3_edge): el ancla anti-churn es una REGLA DE NEGOCIO
        # (se aplica en la ejecución: veto de re-entrada + CHURN_BARS) ⇒ fuera
        # del gradiente. El coste REAL (spread + comisión) sigue dentro del
        # Δequity: no se esconde ningún coste, sólo la regla.
        if self.reward_version != "v3_edge":
            reward -= churn_penalty

        # Penalización por estancamiento si supera tiempo máximo sugerido
        if self.steps_in_trade > self.max_holding_steps:
            reward -= 0.001

        # -------------------------------------------------------------
        # T-24 «Sat-Align» (sólo reward_version="v2_align"; default v1 no
        # entra aquí — byte-idéntico). Dos términos PUNTUALES sobre la suma
        # v1 completa, sin tocar la economía:
        #   (a) higiene de saturación — recargo cuadrático para |dir| > d0;
        #   (b) alineación de posición con la tendencia del régimen —
        #       A_t = sign(pos)·clip(slope/s0,±1)·min(1, adx/0.25); sólo
        #       con posición mantenida (flat → sin término) y neutro en
        #       warm-up (slope relleno 0 / NaN → 0, paridad con el fallback
        #       conservador de current_adx). `candle` es la vela del paso
        #       (L: entrada al step) y `current_adx` el de la detección de
        #       régimen de arriba → mismos inputs que ve el LLM (REQ-1).
        # -------------------------------------------------------------
        if self.reward_version == "v2_align":
            # (a) sat(d): 0 hasta d0; en |dir|=1 → 1 (recargo completo 0,10 u)
            _excess = max(0.0, abs(dir_val) - REWARD_V2_D0_CEIL)
            _sat = (_excess * _excess) / ((1.0 - REWARD_V2_D0_CEIL) ** 2)
            reward -= REWARD_V2_LAMBDA_SAT * self.reward_scale * _sat

            # (b) alineación — sólo con posición viva al final del paso
            if self.position != 0:
                _slope = float(candle.get("sma200_slope", 0.0))
                if np.isnan(_slope):
                    _slope = 0.0
                _a_t = float(np.sign(self.position)) * float(
                    np.clip(_slope / REWARD_V2_S0_SLOPE, -1.0, 1.0)
                ) * min(1.0, current_adx / REWARD_V2_ADX_TREND)
                reward += REWARD_V2_LAMBDA_ALIGN * self.reward_scale * _a_t

        # Reward shaping auxiliar por trades reales
        reward += self._apply_real_outcome_feedback(action)

        self.prev_equity = self.equity
        self.max_equity = max(self.max_equity, self.equity)
        self.current_step += 1

        # -------------------------------------------------------------
        # 6. Terminación vs Truncado
        # -------------------------------------------------------------
        # Terminated: Condición de fallo por quiebra o drawdown terminal (50% de equidad)
        terminated = self.equity <= (self.initial_balance * 0.5)

        # Truncated: Fin de horizonte temporal. NO se aplican penalizaciones artificiales
        steps_taken = self.current_step - self.start_step
        truncated = (self.current_step >= len(self.df) - 1) or (steps_taken >= self.max_episode_steps)

        info = {
            'position': self.position,
            'volume': self.volume,
            'sl_pips': self.sl_pips,
            'tp_pips': self.tp_pips,
            'balance': self.balance,
            'equity': self.equity,
            'trades': self.trade_count,
            'openings': self.opening_count,
            'win_rate': (self.wins / self.trade_count) if self.trade_count > 0 else 0.0,
            'drawdown': (self.max_equity - self.equity) / self.max_equity,
            # Fase 3 / R-1: pnl por trade para que el gate walk-forward calcule
            # la expectancy con la regla 3 € (sin lista no se inventa un valor).
            'trade_pnls': list(self.trade_pnls),
            # T-31.9 / L3: acierto DIRECCIONAL por trade (1/0) — comparable
            # con el benchmark L2 (mismo horizonte +24 h). Vacío = sin trades
            # o sin futuro disponible en el segmento.
            'direction_hits': list(self.direction_hits),
        }

        return self._get_observation(), float(reward), terminated, truncated, info

    def _apply_trailing(self, close_price: float, atr_norm: float) -> None:
        """Trailing stop espejo de `ManageTrailingStop` del EA (mql5 §V).

        Regla BUY (SELL simétrica):
        - riskDist = entry − SL actual; si ≤ 0 (SL ya en beneficio) → sin
          cambios, igual que el `return` del EA: tras el primer movimiento el
          trailing queda congelado.
        - Activación: precio ≥ entry + riskDist (el precio recupera el riesgo).
        - Destino: entry + max(riskDist × TrailingPercent, minTrail) con
          minTrail = max(TrailingMinPips, ATR real/pip × TrailingATRMult).
        - Solo aprieta (newSL > SL actual) y solo si newSL ≤ precio −
          stops-level (SYMBOL_TRADE_STOPS_LEVEL ≈ 1 pip en el EA).

        `atr_norm × close_price` aproxima el ATR en precio que lee el EA
        (iATR sobre precios reales; el ATR del env es adimensional). El precio
        empleado es el de mark (bid/ask con spread), conservador frente al
        ASK/BID del EA. `sl_pips` se recalcula SOLO si el SL se movió (evita
        arrastrar ruido de coma flotante en el caso habitual).
        """
        # P1: los dos mecanismos intraposición del EA son inputs INDEPENDIENTES
        # (EnableTrailing / EnableBreakeven) — gates como en el OnTick del EA.
        if not self.trailing_enabled or self.position == 0 or self.sl_price == 0.0:
            return

        mark_price = (
            close_price - (self.spread / 2.0) if self.position == 1
            else close_price + (self.spread / 2.0)
        )
        atr_real = atr_norm * close_price
        min_trail_pips = self.trailing_min_pips
        if self.trailing_atr_mult > 0 and atr_real > 0:
            min_trail_pips = max(
                min_trail_pips, (atr_real / self.pip_size) * self.trailing_atr_mult
            )
        min_trail_price = min_trail_pips * self.pip_size
        min_stop_dist = self.min_stop_dist_pips * self.pip_size

        moved = False
        if self.position == 1:
            risk_dist = self.entry_price - self.sl_price
            if risk_dist <= 0:
                return
            new_sl = self.entry_price + max(
                risk_dist * self.trailing_percent, min_trail_price
            )
            if (
                mark_price >= self.entry_price + risk_dist
                and new_sl > self.sl_price
                and new_sl <= mark_price - min_stop_dist
            ):
                self.sl_price = new_sl
                moved = True
        else:
            risk_dist = self.sl_price - self.entry_price
            if risk_dist <= 0:
                return
            new_sl = self.entry_price - max(
                risk_dist * self.trailing_percent, min_trail_price
            )
            if (
                mark_price <= self.entry_price - risk_dist
                and new_sl < self.sl_price
                and new_sl >= mark_price + min_stop_dist
            ):
                self.sl_price = new_sl
                moved = True

        if moved:
            self.sl_pips = abs(self.entry_price - self.sl_price) / self.pip_size

    def _apply_breakeven(self, close_price: float) -> None:
        """Breakeven a 1R (P1, parámetro confirmado por Félix 2026-10-08).

        Espejo de `ManageBreakeven` del EA — se ejecuta DESPUÉS del trailing
        (mismo orden de llamada) y con la misma estructura de guards:
        - Sólo actúa si el SL sigue por DEBAJO de la entrada: si el trailing
          ya lo movió por encima (su activación es también +1R), riskDist ≤ 0
          y ambos retornan igual que el `return` del EA.
        - Riesgo R = |entry − SL actual| (con el SL intacto = riesgo inicial).
        - Disparo: mark (bid/ask con spread,≈ el precio que el EA lee con
          BID/ASK) alcanza +1R → el SL pasa a la entrada.
        - Guard de stops-level: el nuevo SL sólo aplica a ≥ min_stop_dist del
          mark (mismo `newSL ≤/≥ precio ∓ minStopDist` del EA).
        `sl_pips` se recalcula SÓLO si el SL se movió (convención de
        `_apply_trailing`).
        """
        if (
            not self.breakeven_enabled
            or self.position == 0
            or self.sl_price == 0.0
            or self.entry_price == 0.0
        ):
            return

        mark_price = (
            close_price - (self.spread / 2.0) if self.position == 1
            else close_price + (self.spread / 2.0)
        )
        min_stop_dist = self.min_stop_dist_pips * self.pip_size

        moved = False
        if self.position == 1:
            risk = self.entry_price - self.sl_price
            if risk <= 0:
                return
            if (
                mark_price - self.entry_price >= risk
                and self.entry_price <= mark_price - min_stop_dist
            ):
                self.sl_price = self.entry_price
                moved = True
        else:
            risk = self.sl_price - self.entry_price
            if risk <= 0:
                return
            if (
                self.entry_price - mark_price >= risk
                and self.entry_price >= mark_price + min_stop_dist
            ):
                self.sl_price = self.entry_price
                moved = True

        if moved:
            self.sl_pips = abs(self.entry_price - self.sl_price) / self.pip_size

    def _future_direction_sign(self) -> Optional[int]:
        """Dirección del mercado a +24 h (1 = arriba, -1 = abajo) desde el
        paso actual. None si el futuro no está disponible (fin del segmento).

        Es una medida POST-HOC de la skill direccional (T-31.9 / L3): el
        agente no la ve (sólo su ventana de observación) — no hay fuga.
        """
        try:
            j = self.current_step + REWARD_V3_DIRECTION_HORIZON_BARS
            if j >= len(self.df):
                return None
            now = float(self.df["close"].iloc[self.current_step])
            fut = float(self.df["close"].iloc[j])
            if fut > now:
                return 1
            if fut < now:
                return -1
            return None
        except (IndexError, KeyError, TypeError, ValueError):
            return None

    def _apply_real_outcome_feedback(self, action: np.ndarray) -> float:
        outcome = self._real_outcomes_by_step.get(self.current_step)
        if outcome is None or self.current_step in self._used_outcomes:
            return 0.0

        self._used_outcomes.add(self.current_step)
        target_pos, _ = decode_v3_direction(float(action[0]), self.position)
        real_pos = 1 if outcome.direction == "LONG" else (-1 if outcome.direction == "SHORT" else 0)

        pnl_norm = float(np.clip(outcome.pnl / 100.0, -1.0, 1.0))
        # R-1 / regla 3 €: la evidencia real se clasifica igual que AGENTS.md —
        # un cierre <3 € es pérdida, así que coincidir con él es MALO (antes
        # un win sub-3 € recompensaba la coincidencia: pnl crudo).
        #
        # T-31.9 / L3 (v3_edge): la evidencia real es DIRECCIONAL (el pnl
        # crudo de ese trade real, sin la inversión de la regla 3 €): lo que
        # se enseña al agente es «el mercado fue a mi favor», no «el trade
        # pasó el filtro de negocio».
        if self.reward_version == "v3_edge":
            evidence = pnl_norm
        else:
            evidence = pnl_norm if is_winning_close(outcome.pnl) else -abs(pnl_norm)

        if target_pos == real_pos and target_pos != 0:
            return evidence * self.real_outcome_weight
        elif target_pos != 0 and real_pos != 0 and target_pos != real_pos:
            return -abs(evidence) * self.real_outcome_weight
        return 0.0