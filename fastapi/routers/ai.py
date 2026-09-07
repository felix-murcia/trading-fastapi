import logging
import httpx
import pickle
import os
import json
import time
import torch
import pandas as pd
import numpy as np
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from .deps import verify_token
from config import settings
from stable_baselines3 import PPO
from services.performance_metrics import record_cycle_metrics
from services.auto_retrain import record_trade_filled, force_retrain as _force_retrain, get_state as _get_retrain_state
from services.market_microstructure import add_microstructure_features
from db.connection import get_pool

router = APIRouter()
MODEL_PATH = "/app/ml/ppo_trading_bot.zip"
QWEN_URL = "http://100.90.16.33:8080/v1/chat/completions"

# ── Opción 1: Quality Score thresholds ───────────────────────────────────────
QUALITY_THRESHOLD = 4.0   # Setup con score < 4 = HOLD aunque PPO quiera operar


# ── Helper: consultar Qwen — Opciones 1, 2, 3 unificadas ────────────────────
async def _query_qwen_unified(
    symbol: str,
    decision: str,
    ml_prob: float,
    rsi: float,
    atr: float,
    atr_pct: float,
    macd_hist: float,
    bb_pos: float,
    range_pct: float,
    last_ret: float,
    hour: int,
    news: str,
    sl_proposed: float,
    tp_proposed: float,
    cycle_id: str,
    logger,
) -> dict:
    """
    Opciones 1+2+3 unificadas — una sola llamada a Qwen por candle.

    Retorna:
      - quality_score: 0-10
      - quality_reason: str
      - llm_bias: BULLISH/BEARISH/NEUTRAL
      - confidence_modifier: 0.5-1.5 (Opción 3)
      - sl_adjusted / tp_adjusted: norm values (Opción 2)
      - regime: TRENDING/RANGING/VOLATILE/BREAKOUT (Opción 4 anticipado)
    """
    # Clasificar sesión
    if 7 <= hour < 12:
        session = "London"
    elif 12 <= hour < 17:
        session = "NY"
    elif 17 <= hour < 23:
        session = "Asia"
    else:
        session = "Weekend/Closed"

    # Clasificar régimen
    if abs(bb_pos) > 0.8:
        regime = "TRENDING"
    elif atr_pct > 0.015:
        regime = "VOLATILE"
    elif abs(macd_hist) < 0.0002:
        regime = "RANGING"
    else:
        regime = "BREAKOUT"

    sl_pips = sl_proposed * 100.0
    tp_pips = tp_proposed * 200.0

    prompt = f"""Analyze this {symbol} H1 trading setup comprehensively.

Market Context:
- RSI(14): {rsi:.1f}
- ATR: {atr:.5f} ({atr_pct:.2%} of price)
- MACD histogram: {macd_hist:.6f}
- Bollinger position: {bb_pos:.2f}
- Range: {range_pct:.2%} of price
- Last return: {last_ret:.3%}
- Session: {session}
- Regime: {regime}

Live News:
{news}

ML Signal: {decision} with ML confidence {ml_prob:.3f}

Provide a comprehensive analysis responding EXACTLY in JSON (no extra text):
{{"quality": 7.5, "reason": "brief reason", "bias": "NEUTRAL", "confidence_modifier": 1.0,
  "sl_ok": true, "tp_ok": true, "sl_adjusted": {sl_proposed:.4f}, "tp_adjusted": {tp_proposed:.4f},
  "regime": "{regime}"}}

Fields:
- quality: rate setup 0-10
- reason: 1-2 sentence explanation
- bias: BULLISH/BEARISH/NEUTRAL
- confidence_modifier: continuous multiplier 0.5-1.5 (how much to boost/reduce ML confidence)
- sl_ok / tp_ok: whether proposed stops are reasonable
- sl_adjusted / tp_adjusted: corrected norm values if needed (SL 0.15-0.30, TP 0.125-0.30, TP:SL ≥1.5x)
- regime: market regime classification"""

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            res = await client.post(QWEN_URL, json={
                "messages": [
                    {"role": "system", "content": "You are a quantitative trading analyst. Always respond in valid JSON."},
                    {"role": "user", "content": prompt}
                ],
                "temperature": 0.2,
                "max_tokens": 300,
            })

        if res.status_code == 200:
            content = res.json().get("choices", [{}])[0].get("message", {}).get("content", "").strip()
            if "{" in content:
                json_str = content[content.index("{"):]
                # Usar raw_decode para extraer solo el JSON válido, ignorando texto posterior
                parsed, _ = json.JSONDecoder().raw_decode(json_str)
                q = float(parsed.get("quality", 5.0))
                reason = str(parsed.get("reason", ""))[:120]
                bias_raw = str(parsed.get("bias", "NEUTRAL")).upper()
                bias = "NEUTRAL"
                if "BULL" in bias_raw: bias = "BULLISH"
                elif "BEAR" in bias_raw: bias = "BEARISH"

                # Opción 3: confidence modifier [0.5, 1.5]
                conf_mod = float(parsed.get("confidence_modifier", 1.0))
                conf_mod = max(0.5, min(1.5, conf_mod))

                # Opción 2: SL/TP validation
                sl_adj = float(parsed.get("sl_adjusted", sl_proposed))
                tp_adj = float(parsed.get("tp_adjusted", tp_proposed))
                sl_adj = max(SL_MIN_NORM, min(SL_MAX_NORM, sl_adj))
                tp_adj = max(TP_MIN_NORM, min(TP_MAX_NORM, tp_adj))
                if tp_adj < sl_adj * 1.5:
                    tp_adj = sl_adj * 1.5

                # Effective probability (Opción 3)
                effective_prob = float(np.exp(np.log(max(ml_prob, 1e-6)) + np.log(conf_mod)))
                effective_prob = max(0.0, min(1.0, effective_prob))

                logger.info(
                    "[%s] QWEN-UNIFIED ║ q=%.1f bias=%s conf_mod=%.2f eff_prob=%.4f | "
                    "sl=%.3f→%.3f tp=%.3f→%.3f regime=%s | %s",
                    cycle_id, q, bias, conf_mod, effective_prob,
                    sl_proposed, sl_adj, tp_proposed, tp_adj,
                    parsed.get("regime", regime), reason[:60]
                )
                return {
                    "quality": q,
                    "reason": reason,
                    "bias": bias,
                    "confidence_modifier": conf_mod,
                    "effective_prob": effective_prob,
                    "sl_adjusted": sl_adj,
                    "tp_adjusted": tp_adj,
                    "regime": parsed.get("regime", regime),
                }
            else:
                logger.warning("[%s] QWEN-UNIFIED ║ respuesta sin JSON: %s", cycle_id, content[:80])
    except Exception as q_err:
        logger.warning("[%s] QWEN-UNIFIED ║ ERROR ║ %s → FALLBACK (q=5.0, bias=NEUTRAL, conf=1.0)",
                       cycle_id, q_err)

    # Fallback — Qwen no disponible
    logger.warning(
        "[%s] QWEN-UNIFIED ║ FALLBACK ║ q=5.0 bias=NEUTRAL conf_mod=1.0 eff_prob=%.4f "
        "sl=%.3f tp=%.3f regime=%s | Qwen unreachable → defaults aplicados",
        cycle_id, ml_prob, sl_proposed, tp_proposed, regime
    )
    effective_prob = float(np.exp(np.log(max(ml_prob, 1e-6))))
    return {
        "quality": 5.0,
        "reason": "Qwen unavailable, using defaults",
        "bias": "NEUTRAL",
        "confidence_modifier": 1.0,
        "effective_prob": effective_prob,
        "sl_adjusted": max(SL_MIN_NORM, min(SL_MAX_NORM, sl_proposed)),
        "tp_adjusted": max(TP_MIN_NORM, min(TP_MAX_NORM, tp_proposed)),
        "regime": regime,
    }


# ── Opción 2: SL/TP Validator ─────────────────────────────────────────────────
SL_MIN_NORM = 0.15   # 15 pips minimum
SL_MAX_NORM = 0.30   # 30 pips maximum
TP_MIN_NORM = 0.125  # 25 pips minimum (SL*0.5 para ratio 2:1 mínimo)
TP_MAX_NORM = 0.30   # 60 pips maximum

async def _query_qwen_sltp_validator(
    symbol: str,
    decision: str,
    atr: float,
    atr_pct: float,
    rsi: float,
    bb_pos: float,
    macd_hist: float,
    range_pct: float,
    hour: int,
    sl_proposed: float,
    tp_proposed: float,
    cycle_id: str,
    logger,
) -> tuple[float, float, str]:
    """
    Opción 2 — SL/TP Validator:
    Qwen analiza si el SL y TP propuestos son razonables para el régimen
    de mercado actual (volatilidad, sesión, momentum).

    Retorna: (sl_norm_final, tp_norm_final, reason)
    """
    # Clasificar régimen según contexto
    if abs(bb_pos) > 0.8:
        regime = "TRENDING"
    elif atr_pct > 0.015:
        regime = "VOLATILE"
    elif abs(macd_hist) < 0.0002:
        regime = "RANGING"
    else:
        regime = "BREAKOUT"

    if 7 <= hour < 12:
        session = "London"
    elif 12 <= hour < 17:
        session = "NY"
    else:
        session = "Asia"

    sl_pips = sl_proposed * 100.0  # desproteger para display
    tp_pips = tp_proposed * 200.0

    prompt = f"""Validate the proposed stop-loss and take-profit for {symbol} {decision}.

Current Market Regime:
- ATR: {atr:.5f} ({atr_pct:.2%} of price → regime is {'HIGH VOLATILITY' if atr_pct > 0.015 else 'NORMAL'})
- RSI: {rsi:.1f}
- MACD histogram: {macd_hist:.6f} ({'bullish' if macd_hist > 0 else 'bearish'})
- Bollinger position: {bb_pos:.2f} ({regime})
- Range size: {range_pct:.2%} of price
- Session: {session}

Proposed Trade:
- Direction: {decision}
- Stop-Loss: {sl_pips:.1f} pips (norm={sl_proposed:.4f})
- Take-Profit: {tp_pips:.1f} pips (norm={tp_proposed:.4f})
- Current ATR: {atr:.5f} pips

Is the SL reasonable for this regime? Is the TP at least 1.5x the SL distance?
Reply EXACTLY JSON (no extra text):
{{"sl_ok": true/false, "tp_ok": true/false, "sl_adjusted": 0.20, "tp_adjusted": 0.28, "reason": "brief"}}
If both are OK, return the same values. If adjustment needed, propose sensible ones."""

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            res = await client.post(QWEN_URL, json={
                "messages": [
                    {"role": "system", "content": "You are a quantitative risk analyst. Always respond in valid JSON."},
                    {"role": "user", "content": prompt}
                ],
                "temperature": 0.15,
                "max_tokens": 150,
            })

        if res.status_code == 200:
            content = res.json().get("choices", [{}])[0].get("message", {}).get("content", "").strip()
            if "{" in content:
                json_str = content[content.index("{"):]
                # Usar raw_decode para extraer solo el JSON válido, ignorando texto posterior
                parsed, _ = json.JSONDecoder().raw_decode(json_str)
                sl_ok = bool(parsed.get("sl_ok", True))
                tp_ok = bool(parsed.get("tp_ok", True))
                sl_adj = float(parsed.get("sl_adjusted", sl_proposed))
                tp_adj = float(parsed.get("tp_adjusted", tp_proposed))

                # Aplicar boundries hard
                sl_adj = max(SL_MIN_NORM, min(SL_MAX_NORM, sl_adj))
                tp_adj = max(TP_MIN_NORM, min(TP_MAX_NORM, tp_adj))

                # Ratio 1.5:1 mínimo TP:SL
                min_tp_for_sl = sl_adj * 1.5
                if tp_adj < min_tp_for_sl:
                    logger.warning("[%s] SLTP-VALIDATOR ║ ratio TP:SL=%.2f < 1.5 → bumping TP %.3f→%.3f",
                                   cycle_id, tp_adj/sl_adj, tp_adj, min_tp_for_sl)
                    tp_adj = min_tp_for_sl

                if not sl_ok or not tp_ok:
                    logger.warning(
                        "[%s] SLTP-REJECTED ║ sl_ok=%s tp_ok=%s → adjusted sl=%.4f tp=%.4f | reason=%s",
                        cycle_id, sl_ok, tp_ok, sl_adj, tp_adj,
                        parsed.get("reason", "")[:80]
                    )
                return sl_adj, tp_adj, parsed.get("reason", "")[:100]
    except Exception as e:
        logger.warning("[%s] SLTP-VALIDATOR ║ error: %s", cycle_id, e)

    # Fallback: bounds hard
    sl_final = max(SL_MIN_NORM, min(SL_MAX_NORM, sl_proposed))
    tp_final = max(TP_MIN_NORM, min(TP_MAX_NORM, tp_proposed))
    min_tp = sl_final * 1.5
    if tp_final < min_tp:
        tp_final = min_tp
    return sl_final, tp_final, "Qwen unavailable, using hard bounds"


# ── Helper: estimar equity desde MT5 ──────────────────────────────────────────
async def _get_equity_estimate() -> float:
    """Equity aproximado desde MT5 para logging."""
    from services.alerting import send_alert, AlertLevel
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{settings.mt5_http_url}/api/v1/account/info", timeout=2.0)
            if r.status_code == 200:
                return r.json().get("equity", 0.0)
    except Exception as e:
        send_alert(
            AlertLevel.ERROR,
            "MT5",
            f"No se pudo obtener equity desde MT5: {e}",
            exc=e,
            context={"mt5_url": settings.mt5_http_url}
        )
    return 0.0

class PredictRequest(BaseModel):
    symbol: str
    timeframe: str = "H1"
    position: int = 0 # 0=Flat, 1=Long, 2=Short

class PredictResponse(BaseModel):
    ml_prob: float
    llm_bias: str
    decision: str
    # Parámetros de gestión de riesgo autónomo (solo presentes con modelo v3)
    volume: float | None = None
    sl_pips: float | None = None
    tp_pips: float | None = None
    model_version: str | None = None
    # Opción 1: Qwen Quality Score
    quality_score: float | None = None  # 0-10 escala de calidad del setup
    quality_reason: str | None = None   # Razón breve del score    # Opción 3: Confidence Modulation
    confidence_modifier: float | None = None  # 0.5-1.5 multiplicador
    effective_prob: float | None = None       # ml_prob * modifier
    # Opción 4: Regime
    regime: str | None = None  # TRENDING/RANGING/VOLATILE/BREAKOUT

@router.on_event("startup")
async def ensure_model_available():
    """Intenta restaurar el modelo desde GCS si no existe localmente."""
    if not os.path.exists(MODEL_PATH):
        logger = logging.getLogger(__name__)
        logger.warning("[AI-STARTUP] Modelo no encontrado. Intentando restaurar desde GCS...")
        try:
            from services.model_backup import restore_latest_model
            result = await restore_latest_model()
            logger.warning("[AI-STARTUP] Restauración: %s", result)
        except Exception as exc:
            logger.error("[AI-STARTUP] No se pudo restaurar modelo: %s", exc)

@router.post("/predict", response_model=PredictResponse)
async def predict_direction(req: PredictRequest, _: None = Depends(verify_token)):
    t_start = time.time()
    logger = logging.getLogger(__name__)
    cycle_id = f"{req.symbol}_{int(time.time() * 1000)}"
    
    # ── LOG 1: Ciclo iniciado ──────────────────────────────────────────────
    equity_val = await _get_equity_estimate()
    logger.info("[%s] ══ CICLO INICIADO ║ symbol=%s timeframe=%s position=%s equity=%.2f",
                cycle_id, req.symbol, req.timeframe, req.position, equity_val)
    # 1. Feature Engineering (Exact match to train_ppo.py)
    async with httpx.AsyncClient() as client:
        res = await client.get(f"{settings.mt5_http_url}/api/v1/market/candles/latest?symbol_name={req.symbol}&timeframe={req.timeframe}&count=100")
        data = res.json()
        candles = data if isinstance(data, list) else data.get("candles", [])
        
    df = pd.DataFrame(candles)
    if df.empty:
        return {"ml_prob": 0.5, "llm_bias": "ERROR", "decision": "HOLD"}
    
    df['returns'] = df['close'].pct_change()
    df['range'] = df['high'] - df['low']
    df['sma20'] = df['close'].rolling(20).mean()
    df['dist_sma20'] = (df['close'] - df['sma20']) / df['sma20']
    
    exp1 = df['close'].ewm(span=12, adjust=False).mean()
    exp2 = df['close'].ewm(span=26, adjust=False).mean()
    df['macd'] = exp1 - exp2
    df['macd_signal'] = df['macd'].ewm(span=9, adjust=False).mean()
    df['macd_hist'] = df['macd'] - df['macd_signal']
    
    df['tr'] = df['high'] - df['low']
    df['atr'] = df['tr'].rolling(14).mean()
    
    # ─── RSI (14) ─────────────────────────────────────────────────────────────
    delta = df['close'].diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, 1e-10)
    df['rsi14'] = 100 - (100 / (1 + rs))
    
    # ─── Bollinger Bands Position ───────────────────────────────────────────────
    bb_sma = df['close'].rolling(20).mean()
    bb_std = df['close'].rolling(20).std()
    df['bb_pos'] = (df['close'] - bb_sma) / (2 * bb_std.replace(0, 1e-10))
    
    # ─── Lagged Returns ─────────────────────────────────────────────────────────
    df['lag_return_1'] = df['returns'].shift(1)
    df['lag_return_2'] = df['returns'].shift(2)
    df['lag_return_3'] = df['returns'].shift(3)
    df['lag_return_5'] = df['returns'].shift(5)
    
    # ─── Hour of day ────────────────────────────────────────────────────────────
    df['hour'] = pd.to_datetime(df['time']).dt.hour
    # ─── Market Microstructure Features (Volume Profile + Orderbook) ───────────────
    # Solo si está habilitado Y el modelo fue reentrenado con estas features
    # (requiere cambiar el observation space del entorno)
    if settings.microstructure_features_enabled:
        try:
            df = await add_microstructure_features(df, req.symbol)
        except Exception as ms_exc:
            logger.warning("[AI-PREDICT] Microstructure features fallaron: %s", ms_exc)
    
    df = df.dropna()
    
    # Exact 10 features matching ppo_trading_bot.zip (10 features + 1 position = 11 cols)
    MODEL_FEATURES = [
        'returns', 'range', 'dist_sma20', 'rsi14',
        'macd', 'macd_signal', 'macd_hist',
        'bb_pos', 'lag_return_1', 'lag_return_2',
    ]
    # Solo usar las que existan en el DataFrame (robusto a cambios de velas)
    available = [f for f in MODEL_FEATURES if f in df.columns]
    if len(available) != len(MODEL_FEATURES):
        missing = set(MODEL_FEATURES) - set(available)
        logger.warning("[AI-PREDICT] Features faltantes: %s — el modelo puede comportarse inesperadamente", missing)
    df_clean = df[available].dropna()
    
    # 2. ML Prediction (Stable-Baselines3 PPO)
    # Soporta DOS versiones del modelo:
    #  - v2 (ppo_trading_bot.zip):  action_space=Discrete(3) → [FLAT, LONG, SHORT]
    #  - v3 (ppo_trading_bot_v3.zip): action_space=Box(4,) → [direction, volume, sl_pips, tp_pips]
    ml_prob = 0.5
    decision = "HOLD"
    raw_action = None

    # Defaults para indicadores (se sobreescriben dentro del branch v3 exitoso)
    rsi_val = 50.0
    atr_val = 0.001
    atr_pct = 0.001
    macd_hist_val = 0.0
    bb_pos_val = 0.0
    range_pct = 0.002
    last_ret_val = 0.0
    hour_val = 12

    # Preferir modelo v3 si existe (acción continua → autónomo en volume/SL/TP)
    ppo_paths = [
        ("/app/ml/ppo_trading_bot_v3.zip", "v3"),
        ("/app/ml/ppo_trading_bot.zip", "v2"),
    ]

    for ppo_path, model_version in ppo_paths:
        if not os.path.exists(ppo_path):
            continue

        # ── Preparar df_clean según versión del modelo ─────────────────────────
        if model_version == "v3":
            # v3: usar todas las features de mercado (excluir OHLCV/tiempo/target)
            # El environment v2 toma todas las features del df que no sean OHLCV
            _exclude = {'time', 'open', 'high', 'low', 'close', 'tick_volume',
                        'target', 'volume'}
            v3_market_features = [c for c in df.columns if c not in _exclude]
            v3_available = [f for f in v3_market_features if f in df.columns]
            df_clean = df[v3_available].dropna()
        else:
            df_clean = df[available].dropna()

        if len(df_clean) < 10:
            continue

        try:
            model = PPO.load(ppo_path)

            # ── Construir observation matching exactamente el training ──────────
            if model_version == "v3":
                # v3 obs: (10, N_market + 4) — el shape se autodetecta del modelo
                # Auto-detect: leer observación esperada del environment cargado
                try:
                    expected_market_features = model.observation_space.shape[1] - 4
                except Exception:
                    expected_market_features = 12  # fallback legacy

                last_market = df_clean.iloc[-10:].values.astype(np.float32)
                # Pad si hay menos features que las esperadas
                if last_market.shape[1] < expected_market_features:
                    pad = np.zeros((10, expected_market_features - last_market.shape[1]), dtype=np.float32)
                    last_market = np.hstack([last_market, pad])
                # Truncar si hay más features que las esperadas
                elif last_market.shape[1] > expected_market_features:
                    last_market = last_market[:, :expected_market_features]

                last_price = float(df['close'].iloc[-1])
                entry_norm = np.full((10, 1), (last_price - 1.0) / 0.1, dtype=np.float32)
                pos_matrix = np.full((10, 1), float(req.position), dtype=np.float32)
                unrealized_norm = np.zeros((10, 1), dtype=np.float32)
                balance_norm = np.full((10, 1), 1.0, dtype=np.float32)
                obs = np.hstack([last_market, entry_norm, pos_matrix, unrealized_norm, balance_norm])
            else:
                last_10 = df_clean.iloc[-10:].values
                pos_matrix = np.full((10, 1), req.position)
                obs = np.hstack((last_10, pos_matrix)).astype(np.float32)

            # Predicción
            action, _ = model.predict(obs, deterministic=True)

            # ── Modelo v3: acción continua (direction, volume, sl_pips, tp_pips) ──
            if model_version == "v3":
                direction, volume, sl_pips_norm, tp_pips_norm = action.squeeze()

                # Decode dirección
                if direction < -0.33:
                    target_pos = -1  # SHORT
                elif direction > 0.33:
                    target_pos = 1  # LONG
                else:
                    target_pos = 0  # FLAT

                if target_pos == 1:
                    decision = "HOLD" if req.position == 1 else "BUY"
                elif target_pos == -1:
                    decision = "HOLD" if req.position == 2 else "SELL"
                else:
                    decision = "HOLD"

                # ml_prob = confidence de la dirección predicha
                # v3 tiene acción continua → usar CDF del Normal para dar
                # una "probabilidad" intuitiva: cuánta masa de probabilidad
                # queda en el lado correcto de la acción tomada (1 = seguro, 0 = improbable)
                obs_t = torch.from_numpy(obs).float()
                preprocessed = obs_t.flatten().unsqueeze(0)
                with torch.no_grad():
                    dist = model.policy.get_distribution(preprocessed)
                mean = dist.distribution.mean.numpy().squeeze()
                std = dist.distribution.stddev.numpy().squeeze()
                std = np.maximum(std, 0.01)  # evitar std=0
                action_arr = action.squeeze()
                # Para la dirección (índice 0): CDF en la acción significa
                # "qué tan lejos está la acción del centro, en dirección correcta"
                # Si mean<0 y action<0 → acción está en el lado correcto del centro
                from torch.distributions import Normal
                n = Normal(torch.tensor(mean), torch.tensor(std))
                action_tensor = torch.tensor(action_arr, dtype=torch.float32)
                log_prob = n.log_prob(action_tensor).sum().item()
                ml_prob = float(np.exp(log_prob))
                # Normalizar: log_prob de un Normal típico está en [-5, 0]
                # Mapear a [0, 1] con 0 = más negativo, 1 = 0
                ml_prob = float(np.exp(log_prob / 4))  # escala para que sea más legible

                # ── LOG 2: Raw model output ─────────────────────────────────
                logger.info("[%s] MODEL-RAW ║ direction=%.4f volume=%.4f sl_norm=%.4f tp_norm=%.4f | target_pos=%d ml_prob=%.4f",
                            cycle_id, float(direction), float(volume),
                            float(sl_pips_norm), float(tp_pips_norm),
                            target_pos, ml_prob)

                # Guardar valores para quality score (antes de guards)
                rsi_val = float(df['rsi14'].iloc[-1]) if 'rsi14' in df.columns else 50.0
                atr_val = float(df['atr'].iloc[-1]) if 'atr' in df.columns else 0.001
                atr_pct = atr_val / float(df['close'].iloc[-1]) if float(df['close'].iloc[-1]) > 0 else 0.001
                macd_hist_val = float(df['macd_hist'].iloc[-1]) if 'macd_hist' in df.columns else 0.0
                bb_pos_val = float(df['bb_pos'].iloc[-1]) if 'bb_pos' in df.columns else 0.0
                range_pct = float(df['range'].iloc[-1]) / float(df['close'].iloc[-1]) if float(df['close'].iloc[-1]) > 0 else 0.002
                last_ret_val = float(df['returns'].iloc[-1]) if 'returns' in df.columns else 0.0
                hour_val = int(pd.to_datetime(df['time'].iloc[-1]).hour) if 'time' in df.columns else 12

                # ── Guards: evitar valores extremos del modelo ──────────────
                # El modelo puede dar 0.0 (min) o 1.0 (max) por no haber aprendido bien
                # Aplicamos un floor/ceiling razonable para evitar órdenes imposibles
                volume_val = float(volume)
                sl_val = float(sl_pips_norm)
                tp_val = float(tp_pips_norm)

                # ── LOG 3: Guards aplicados ──────────────────────────────────
                guards_log = {
                    "volume_raw": float(volume),
                    "sl_raw": float(sl_pips_norm),
                    "tp_raw": float(tp_pips_norm),
                    "guards_applied": []
                }

                # Volume: capar siempre al 30% (equity bajo no soporta más)
                if volume_val > 0.30:
                    guards_log["guards_applied"].append(f"VOLUME_MAX: {volume_val:.4f}→0.30")
                    logger.warning("[%s] GUARD-VOLUME-MAX ║ %.4f → 0.30 (equity bajo)", cycle_id, volume_val)
                    volume_val = 0.30
                if volume_val < 0.10:
                    guards_log["guards_applied"].append(f"VOLUME_MIN: {volume_val:.4f}→0.10")
                    logger.warning("[%s] GUARD-VOLUME-MIN ║ %.4f → 0.10", cycle_id, volume_val)
                    volume_val = 0.10
                # SL/TP: capar siempre — sin condición > 0.95
                # (sin ella el modelo puede generar 0.8-0.9 que equivale a 160-180 pips)
                if sl_val > 0.30:
                    guards_log["guards_applied"].append(f"SL_MAX: {sl_val:.4f}→0.30")
                    logger.warning("[%s] GUARD-SL-MAX ║ %.4f → 0.30 (30 pips)", cycle_id, sl_val)
                    sl_val = 0.30
                if tp_val > 0.30:
                    guards_log["guards_applied"].append(f"TP_MAX: {tp_val:.4f}→0.30")
                    logger.warning("[%s] GUARD-TP-MAX ║ %.4f → 0.30 (60 pips)", cycle_id, tp_val)
                    tp_val = 0.30
                if sl_val < 0.15:
                    guards_log["guards_applied"].append(f"SL_MIN: {sl_val:.4f}→0.15")
                    logger.warning("[%s] GUARD-SL-MIN ║ %.4f → 0.15", cycle_id, sl_val)
                    sl_val = 0.15
                if tp_val < 0.125:
                    guards_log["guards_applied"].append(f"TP_MIN: {tp_val:.4f}→0.125")
                    logger.warning("[%s] GUARD-TP-MIN ║ %.4f → 0.125", cycle_id, tp_val)
                    tp_val = 0.125

                if not guards_log["guards_applied"]:
                    logger.info("[%s] GUARD-CLEAN ║ sin intervención", cycle_id)

                raw_action = {
                    "version": "v3",
                    "direction": float(direction),
                    "volume": float(volume_val),
                    "sl_pips_norm": float(sl_val),
                    "tp_pips_norm": float(tp_val),
                    "target_position": target_pos,
                }
                break

            # ── Modelo v2: acción discreta (0=FLAT, 1=LONG, 2=SHORT) ─────────
            else:
                obs_t = torch.from_numpy(obs).float()
                preprocessed = obs_t.flatten().unsqueeze(0)
                with torch.no_grad():
                    dist = model.policy.get_distribution(preprocessed)
                action_probs = dist.distribution.probs.numpy().squeeze()

                act_val = int(action.item()) if isinstance(action, np.ndarray) else int(action)
                ml_prob = float(action_probs[act_val])

                if act_val == 1:
                    decision = "HOLD" if req.position == 1 else "BUY"
                elif act_val == 2:
                    decision = "HOLD" if req.position == 2 else "SELL"
                else:
                    decision = "HOLD"

                raw_action = {"version": "v2", "action_discrete": int(act_val)}
                break

        except Exception as exc:
            logger.warning("[AI-PREDICT] Error con modelo %s: %s", ppo_path, exc)
            continue


    # 3. LLM Unified — Opciones 1+2+3+4 en una sola llamada (no solo BUY/SELL)
    try:
        from services.news_scraper import get_macro_news
        live_news = await get_macro_news(req.symbol)
    except Exception:
        live_news = "Macro data unavailable."

    qwen_result = await _query_qwen_unified(
        symbol=req.symbol,
        decision=decision,
        ml_prob=ml_prob,
        rsi=rsi_val,
        atr=atr_val,
        atr_pct=atr_pct,
        macd_hist=macd_hist_val,
        bb_pos=bb_pos_val,
        range_pct=range_pct,
        last_ret=last_ret_val,
        hour=hour_val,
        news=live_news,
        sl_proposed=raw_action["sl_pips_norm"] if raw_action and raw_action.get("version") == "v3" else 0.20,
        tp_proposed=raw_action["tp_pips_norm"] if raw_action and raw_action.get("version") == "v3" else 0.20,
        cycle_id=cycle_id,
        logger=logger,
    )

    quality_score = qwen_result["quality"]
    quality_reason = qwen_result["reason"]
    llm_bias = qwen_result["bias"]
    conf_modifier = qwen_result["confidence_modifier"]
    effective_prob = qwen_result["effective_prob"]
    regime = qwen_result["regime"]

    # Usar effective_prob (modulado) para la decisión
    ml_prob_decision = effective_prob

    # ── LOG 4: Quality-threshold override ────────────────────────────────
    decision_raw = decision
    if quality_score < QUALITY_THRESHOLD:
        logger.warning(
            "[%s] QUALITY-VETO ║ score=%.1f < %.1f → %s→HOLD | reason=%s",
            cycle_id, quality_score, QUALITY_THRESHOLD, decision, quality_reason[:80]
        )
        decision = "HOLD"
    elif decision == "BUY" and llm_bias == "BEARISH":
        logger.warning("[%s] VETO ║ BUY→HOLD (LLM=BEARISH)", cycle_id)
        decision = "HOLD"
    elif decision == "SELL" and llm_bias == "BULLISH":
        logger.warning("[%s] VETO ║ SELL→HOLD (LLM=BULLISH)", cycle_id)
        decision = "HOLD"

    # ── Opción 2: SL/TP Validator — apply adjusted values ──────────────
    if raw_action and raw_action.get("version") == "v3":
        raw_action["sl_pips_norm"] = qwen_result["sl_adjusted"]
        raw_action["tp_pips_norm"] = qwen_result["tp_adjusted"]

    # ── LOG 5: Decisión final ─────────────────────────────────────────────
    if raw_action and raw_action.get("version") == "v3":
        vol_out = round(raw_action["volume"] * 0.5, 4)
        sl_out = round(max(1.0, raw_action["sl_pips_norm"] * 100.0), 1)
        tp_out = round(max(1.0, raw_action["tp_pips_norm"] * 200.0), 1)
        logger.info(
            "[%s] DECISION-FINAL ║ decision=%s ml_prob=%.4f eff_prob=%.4f conf_mod=%.2f | "
            "llm=%s regime=%s quality=%.1f | volume=%.4f sl=%.1f tp=%.1f equity=%.2f",
            cycle_id, decision, ml_prob, effective_prob, conf_modifier, llm_bias, regime,
            quality_score, vol_out, sl_out, tp_out,
            await _get_equity_estimate()
        )
    else:
        logger.info("[%s] DECISION-FINAL ║ decision=%s ml_prob=%.4f llm=%s",
                    cycle_id, decision, ml_prob, llm_bias)
    
    # 5. Telemetry & Auditing
    try:
        pool = get_pool()
        await pool.execute(
            "INSERT INTO audit_log(cycle_id, event, data) VALUES($1,$2,$3)",
            f"ai_predict_{req.symbol}_{int(time.time())}",
            "ai_predict_cycle",
            json.dumps({
                "symbol": req.symbol,
                "ml_prob": round(ml_prob, 4),
                "llm_bias": llm_bias,
                "decision": decision,
                **({"raw_action": raw_action} if raw_action else {})
            })
        )
    except Exception as resp_err:
        from services.alerting import send_alert, AlertLevel
        send_alert(
            AlertLevel.ERROR,
            "AI-PREDICT",
            f"Error armando respuesta de predict: {resp_err}",
            exc=resp_err,
            context={"symbol": req.symbol, "decision": decision}
        )

    # 6. Métricas de rendimiento
    t_end = time.time()
    from services.performance_metrics import record_cycle_metrics as _rec
    _rec(symbol=req.symbol, decision=decision, ml_prob=effective_prob, llm_bias=llm_bias,
         latency_ms=(t_end - t_start) * 1000,
         mt5_available=True, order_placed=False)

    resp = PredictResponse(
        ml_prob=round(effective_prob, 4),
        llm_bias=llm_bias,
        decision=decision,
        model_version=raw_action.get("version") if raw_action else None,
        # v3 normalizado → valores reales
        volume=round(raw_action["volume"] * 0.5, 4) if raw_action and raw_action.get("version") == "v3" else None,
        sl_pips=round(max(1.0, raw_action["sl_pips_norm"] * 100.0), 1) if raw_action and raw_action.get("version") == "v3" else None,
        tp_pips=round(max(1.0, raw_action["tp_pips_norm"] * 200.0), 1) if raw_action and raw_action.get("version") == "v3" else None,
        # Opción 1: Quality Score
        quality_score=round(quality_score, 2),
        quality_reason=quality_reason[:200] if quality_reason else None,
        # Opción 3: Confidence Modulation
        confidence_modifier=round(conf_modifier, 3),
        effective_prob=round(effective_prob, 4),
        # Opción 4: Regime
        regime=regime,
    )
    return resp


@router.post("/retrain/force")
async def force_retrain_endpoint(_: None = Depends(verify_token)):
    """
    Fuerza un retrain inmediato del modelo PPO.
    Útil para when market regime cambia (e.g., central bank intervention).
    """
    state = _get_retrain_state()
    result = await _force_retrain()
    return {"status": result["status"], "filled_count": state.filled_count, "in_progress": state.retrain_in_progress}


@router.get("/retrain/status")
async def retrain_status(_: None = Depends(verify_token)):
    """Estado actual del auto-retrain."""
    state = _get_retrain_state()
    return {
        "filled_count": state.filled_count,
        "retrain_in_progress": state.retrain_in_progress,
        "last_retrain_time": state.last_retrain_time,
        "outcomes_count": len(state.outcomes),
    }


# ─── Pipeline Health ──────────────────────────────────────────────────────────
# Health check integral del sistema — verifica que todo el pipeline funcione.


class HealthCheckResponse(BaseModel):
    status: str  # "healthy" | "degraded" | "critical"
    components: dict  # {name: {"ok": bool, "error": str|null}}


@router.get("/pipeline/health", response_model=HealthCheckResponse)
async def pipeline_health() -> HealthCheckResponse:
    """
    Verifica la salud de todo el sistema de trading.
    Cada componente que falle se reporta explícitamente.
    """
    from services.alerting import send_alert, AlertLevel

    components = {}
    issues = []

    # 1. Database
    try:
        pool = get_pool()
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        trade_count = await pool.fetchval("SELECT COUNT(*) FROM trade_outcomes")
        components["database"] = {"ok": True, "trade_count": trade_count, "error": None}
    except Exception as e:
        components["database"] = {"ok": False, "error": str(e)}
        issues.append(f"DB: {e}")

    # 2. MT5 connectivity
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{settings.mt5_http_url}/api/v1/account/info", timeout=5.0)
            mt5_ok = r.status_code == 200
        components["mt5"] = {"ok": mt5_ok, "error": None if mt5_ok else f"HTTP {r.status_code}"}
        if not mt5_ok:
            issues.append(f"MT5: HTTP {r.status_code}")
    except Exception as e:
        components["mt5"] = {"ok": False, "error": str(e)}
        issues.append(f"MT5: {e}")

    # 3. PPO model file exists
    MODEL_PATH = "/app/ml/ppo_trading_bot_v3.zip"
    try:
        model_exists = os.path.exists(MODEL_PATH)
        components["ppo_model"] = {"ok": model_exists, "error": None if model_exists else "Model file not found"}
        if not model_exists:
            issues.append("PPO model missing")
    except Exception as e:
        components["ppo_model"] = {"ok": False, "error": str(e)}

    # 4. Retrain state
    state = _get_retrain_state()
    components["retrain"] = {
        "ok": True,
        "filled_count": state.filled_count,
        "in_progress": state.retrain_in_progress,
        "last_retrain": state.last_retrain_time,
        "error": None,
    }
    if state.filled_count == 0:
        issues.append("No trades recorded yet (EA may not be sending webhooks)")

    # Determine overall status
    critical_failures = sum(1 for c in components.values() if not c["ok"])
    if critical_failures > 0:
        status = "critical" if any(c == "database" or c == "ppo_model" for c in components) else "degraded"
    else:
        status = "healthy"

    if status != "healthy":
        send_alert(
            AlertLevel.ERROR,
            "PIPELINE-HEALTH",
            f"Pipeline status: {status}. Issues: {'; '.join(issues)}",
            context={"components": components, "status": status},
        )

    return HealthCheckResponse(status=status, components=components)


# ─── Trade Completion Webhook ────────────────────────────────────────────────
# Llamado por el MT5 EA cuando una orden se cierra (filled).


class TradeFilledRequest(BaseModel):
    symbol: str
    entry_time: float | str  # Unix timestamp (float) or ISO string
    exit_time: float | str   # Unix timestamp (float) or ISO string
    pnl: float
    pnl_pct: float
    direction: str   # "LONG" or "SHORT"
    sl_hit: bool
    tp_hit: bool
    exit_reason: str  # "sl", "tp", "manual", "news"


@router.post("/trade/filled")
async def trade_filled_webhook(req: TradeFilledRequest, _: None = Depends(verify_token)):
    """
    Webhook que el MT5 EA llama cuando una orden se cierra.
    Registra el trade y dispara auto-retrain si corresponde.
    """
    # Normalize direction to uppercase to match DB constraint (LONG/SHORT)
    direction = req.direction.upper() if req.direction else req.direction
    await record_trade_filled(
        symbol=req.symbol,
        entry_time=req.entry_time,
        exit_time=req.exit_time,
        pnl=req.pnl,
        pnl_pct=req.pnl_pct,
        direction=direction,
        sl_hit=req.sl_hit,
        tp_hit=req.tp_hit,
        exit_reason=req.exit_reason,
    )
    return {"status": "recorded"}
