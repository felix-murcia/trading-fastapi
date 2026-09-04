"""
Market Microstructure Features — Volume Profile + Orderbook Imbalance + Correlations.

Genera features avanzadas para el modelo PPO:
1. Volume Profile — áreas de valor (POC, VAH, VAL)
2. Orderbook Imbalance — bid/ask pressure (si MT5 MCP provee datos)
3. Correlation Features — SPX, DXY, VIXbetaint
"""
import logging
import asyncio
from typing import Optional
from dataclasses import dataclass

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ─── Volume Profile ────────────────────────────────────────────────────────────

@dataclass
class VolumeProfileResult:
    """Resultado del análisis de Volume Profile."""
    poc: float           # Point of Control — precio con más volumen
    vah: float          # Value Area High — borde superior del 70%
    val: float          # Value Area Low — borde inferior del 70%
    vol_area_pct: float # % del volumen dentro del área de valor
    profile_strength: float  # 0-1 qué tan definido está el perfil


def compute_volume_profile(
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    volumes: np.ndarray,
    bins: int = 50,
    value_area_pct: float = 0.70,
) -> VolumeProfileResult:
    """
    Calcula Volume Profile clásico (TPO-style).
    Agrupa precio en bins y suma volumen en cada nivel.

    Args:
        highs, lows, closes, volumes: arrays de velas
        bins: número de niveles de precio
        value_area_pct: % del volumen a incluir en el área de valor (default 70%)
    """
    if len(closes) < 20:
        return VolumeProfileResult(poc=0, vah=0, val=0, vol_area_pct=0, profile_strength=0)

    try:
        # Crear bins de precio
        price_min = lows.min()
        price_max = highs.max()
        if price_max == price_min:
            return VolumeProfileResult(poc=float(closes[-1]), vah=price_max, val=price_min, vol_area_pct=1.0, profile_strength=0)

        bin_edges = np.linspace(price_min, price_max, bins + 1)
        bin_size = (price_max - price_min) / bins

        # Para cada vela, distribuir volumen proporcionalmente entre high y low
        bin_volumes = np.zeros(bins)
        for i in range(len(closes)):
            h, l, c, v = highs[i], lows[i], closes[i], volumes[i]
            if v <= 0:
                continue
            # Todos los precios entre low y high contribuyen
            low_bin = max(0, int((l - price_min) / bin_size))
            high_bin = min(bins - 1, int((h - price_min) / bin_size))
            for b in range(low_bin, high_bin + 1):
                bin_volumes[b] += v / (high_bin - low_bin + 1)

        total_volume = bin_volumes.sum()
        if total_volume == 0:
            return VolumeProfileResult(poc=float(closes[-1]), vah=price_max, val=price_min, vol_area_pct=0, profile_strength=0)

        # POC — bin con más volumen
        poc_bin = int(np.argmax(bin_volumes))
        poc = price_min + (poc_bin + 0.5) * bin_size

        # Value Area — buscar bins que acumulen value_area_pct del volumen total
        sorted_bins = np.argsort(bin_volumes)[::-1]  # Mayor a menor
        cumsum = 0
        va_bins = []
        for b in sorted_bins:
            cumsum += bin_volumes[b]
            va_bins.append(b)
            if cumsum >= total_volume * value_area_pct:
                break

        va_bins = sorted(va_bins)
        val = price_min + va_bins[0] * bin_size
        vah = price_min + (va_bins[-1] + 1) * bin_size

        # % del volumen dentro del área de valor
        vol_in_va = bin_volumes[va_bins[0]:va_bins[-1]+1].sum()
        vol_area_pct = vol_in_va / total_volume if total_volume > 0 else 0

        # Profile strength — qué tan concentrado está el volumen (0=uniforme, 1=muy concentrado)
        max_bin_vol = bin_volumes.max()
        avg_bin_vol = bin_volumes.mean()
        profile_strength = float(np.clip((max_bin_vol / (avg_bin_vol + 1e-10)) / 3, 0, 1))

        return VolumeProfileResult(
            poc=float(poc),
            vah=float(vah),
            val=float(val),
            vol_area_pct=float(vol_area_pct),
            profile_strength=float(profile_strength),
        )

    except Exception as exc:
        logger.warning("[VOL-PROFILE] Error calculando: %s", exc)
        return VolumeProfileResult(poc=float(closes[-1]), vah=highs.max(), val=lows.min(), vol_area_pct=0, profile_strength=0)


def add_volume_profile_features(
    df: pd.DataFrame,
    lookback: int = 20,
) -> pd.DataFrame:
    """
    Añade features de Volume Profile al DataFrame de velas.
    Para cada vela, calcula el VP de las `lookback` velas anteriores.

    Añade columnas:
    - vp_poc_dist: distancia del precio actual al POC (normalizado)
    - vp_vah_dist: distancia a Value Area High
    - vp_val_dist: distancia a Value Area Low
    - vp_in_va: 1 si el precio está dentro del área de valor
    - vp_profile_strength
    """
    df = df.copy()

    # Inicializar columnas
    for col in ["vp_poc_dist", "vp_vah_dist", "vp_val_dist", "vp_in_va", "vp_profile_strength"]:
        df[col] = 0.0

    for i in range(lookback, len(df)):
        window_df = df.iloc[i-lookback:i]
        vp = compute_volume_profile(
            highs=window_df["high"].values,
            lows=window_df["low"].values,
            closes=window_df["close"].values,
            volumes=window_df["tick_volume"].values if "tick_volume" in window_df else np.ones(len(window_df)),
        )

        current_close = df.iloc[i]["close"]

        # Distancia normalizada al POC (en % del precio)
        df.iloc[i, df.columns.get_loc("vp_poc_dist")] = (current_close - vp.poc) / (current_close + 1e-10)
        df.iloc[i, df.columns.get_loc("vp_vah_dist")] = (current_close - vp.vah) / (current_close + 1e-10)
        df.iloc[i, df.columns.get_loc("vp_val_dist")] = (current_close - vp.val) / (current_close + 1e-10)
        df.iloc[i, df.columns.get_loc("vp_in_va")] = 1.0 if vp.val <= current_close <= vp.vah else 0.0
        df.iloc[i, df.columns.get_loc("vp_profile_strength")] = vp.profile_strength

    return df


# ─── Orderbook Imbalance ──────────────────────────────────────────────────────

@dataclass
class OrderbookImbalanceResult:
    """Resultado del análisis de Orderbook Imbalance."""
    bid_ask_ratio: float   # Ratio bid_vol / ask_vol (1 = equilibrado)
    pressure: float        # -1=max sell pressure, +1=max buy pressure
    depth_imbalance: float # imbalance en los primeros N niveles


async def fetch_orderbook(symbol: str, levels: int = 10) -> Optional[dict]:
    """
    Intenta obtener orderbook desde MT5 MCP.
    Devuelve dict con 'bids' y 'asks' o None si no está disponible.
    """
    import httpx
    from config import settings

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            # MT5 MCP puede no tener este endpoint — es un intento
            res = await client.get(
                f"{settings.mt5_http_url}/api/v1/market/orderbook",
                params={"symbol": symbol, "depth": levels},
                timeout=3.0,
            )
            if res.status_code == 200:
                return res.json()
    except Exception:
        pass
    return None


def compute_ob_imbalance(
    bids: list[tuple[float, float]],
    asks: list[tuple[float, float]],
    levels: int = 5,
) -> OrderbookImbalanceResult:
    """
    Calcula orderbook imbalance desde bids y asks.
    bids/asks: lista de (precio, volumen)

    Returns OrderbookImbalanceResult con:
    - bid_ask_ratio: ratio de volumen total bid/ask
    - pressure: -1 a +1 (sell pressure a buy pressure)
    - depth_imbalance: imbalance normalizado en primeros N niveles
    """
    if not bids or not asks:
        return OrderbookImbalanceResult(bid_ask_ratio=1.0, pressure=0.0, depth_imbalance=0.0)

    try:
        bid_vols = [v for _, v in bids[:levels]]
        ask_vols = [v for _, v in asks[:levels]]

        total_bid = sum(bid_vols)
        total_ask = sum(ask_vols)

        # Bid/Ask Ratio
        bid_ask_ratio = total_bid / (total_ask + 1e-10)

        # Pressure: normalizado entre -1 y +1
        total_vol = total_bid + total_ask
        pressure = (total_bid - total_ask) / (total_vol + 1e-10)

        # Depth imbalance (WOBP — Weighted Obstruction Baseline Pressure)
        # Compara volumen acumulado en cada nivel
        imbalance = 0.0
        for i in range(min(levels, len(bid_vols), len(ask_vols))):
            b = bid_vols[i]
            a = ask_vols[i]
            imbalance += (b - a) / (b + a + 1e-10)
        depth_imbalance = imbalance / levels if levels > 0 else 0.0

        return OrderbookImbalanceResult(
            bid_ask_ratio=float(bid_ask_ratio),
            pressure=float(pressure),
            depth_imbalance=float(depth_imbalance),
        )

    except Exception as exc:
        logger.warning("[OB-IMBALANCE] Error: %s", exc)
        return OrderbookImbalanceResult(bid_ask_ratio=1.0, pressure=0.0, depth_imbalance=0.0)


# ─── Correlation Features ─────────────────────────────────────────────────────

@dataclass
class CorrelationFeatures:
    """Features de correlación con índices macro."""
    spx_correlation: float   # Correlación con S&P 500 (rolling)
    dxy_correlation: float   # Correlación con DXY (USD Index)
    vix_correlation: float  # Correlación con VIX (fear index)


# Nota: Estas correlaciones requieren datos de SPX, DXY, VIX.
# En producción se obtendrían de un feed de datos (yfinance, Polygon.io, etc.)
# Por ahora devolvemos 0 (neutral) con logging de que se necesita feed externo.

async def fetch_correlation_data(
    symbol: str,
    timeframe: str = "H1",
    count: int = 100,
) -> dict[str, np.ndarray]:
    """
    Obtiene datos de índices macro para correlación.
    Por implementar: conectar a yfinance, Polygon.io, o similar.

    Returns dict con 'spx', 'dxy', 'vix' arrays de precios.
    """
    # Placeholder — en producción usar:
    # import yfinance as yf
    # spx = yf.download("^GSPC", period="2d", interval=timeframe.lower())
    logger.debug("[CORR] fetch_correlation_data called — external feed not connected yet")
    return {}


def compute_correlation_features(
    symbol_prices: np.ndarray,
    spx_prices: Optional[np.ndarray] = None,
    dxy_prices: Optional[np.ndarray] = None,
    vix_prices: Optional[np.ndarray] = None,
    lookback: int = 50,
) -> CorrelationFeatures:
    """
    Calcula correlaciones rolling entre el símbolo e índices macro.
    Solo calcula si los datos de índices están disponibles.
    """
    returns = np.diff(np.log(symbol_prices + 1e-10))

    def rolling_corr(a, b, window):
        if len(a) < window or len(b) < window:
            return 0.0
        # Últimos `window` datos
        a_w = a[-window:]
        b_w = b[-window:]
        if np.std(a_w) < 1e-10 or np.std(b_w) < 1e-10:
            return 0.0
        return float(np.corrcoef(a_w, b_w)[0, 1])

    spx_corr = rolling_corr(returns, spx_prices, lookback) if spx_prices is not None else 0.0
    dxy_corr = rolling_corr(returns, dxy_prices, lookback) if dxy_prices is not None else 0.0
    vix_corr = rolling_corr(returns, vix_prices, lookback) if vix_prices is not None else 0.0

    return CorrelationFeatures(
        spx_correlation=spx_corr,
        dxy_correlation=dxy_corr,
        vix_correlation=vix_corr,
    )


# ─── Feature Pipeline ─────────────────────────────────────────────────────────

async def add_microstructure_features(
    df: pd.DataFrame,
    symbol: str,
) -> pd.DataFrame:
    """
    Pipeline completo: añade Volume Profile + Orderbook + Correlations.
    Llama a los tres módulos anteriores.
    """
    df = df.copy()

    # 1. Volume Profile (disponible siempre — solo necesita velas)
    df = add_volume_profile_features(df, lookback=20)

    # 2. Orderbook Imbalance (requiere MT5 MCP endpoint — optional)
    ob = await fetch_orderbook(symbol)
    if ob:
        bids = ob.get("bids", [])
        asks = ob.get("asks", [])
        ob_result = compute_ob_imbalance(bids, asks)
        # Añadir como constantes para todo el df (no cambia por vela)
        df["ob_pressure"] = ob_result.pressure
        df["ob_bid_ask_ratio"] = ob_result.bid_ask_ratio
        df["ob_depth_imbalance"] = ob_result.depth_imbalance
        logger.debug("[OB] pressure=%.2f ratio=%.2f", ob_result.pressure, ob_result.bid_ask_ratio)
    else:
        df["ob_pressure"] = 0.0
        df["ob_bid_ask_ratio"] = 1.0
        df["ob_depth_imbalance"] = 0.0

    # 3. Correlation features (placeholder — necesita feed externo)
    corr = await fetch_correlation_data(symbol)
    if corr:
        spx = corr.get("spx")
        dxy = corr.get("dxy")
        vix = corr.get("vix")
        corr_features = compute_correlation_features(
            symbol_prices=df["close"].values,
            spx_prices=spx,
            dxy_prices=dxy,
            vix_prices=vix,
        )
        df["corr_spx"] = corr_features.spx_correlation
        df["corr_dxy"] = corr_features.dxy_correlation
        df["corr_vix"] = corr_features.vix_correlation
    else:
        df["corr_spx"] = 0.0
        df["corr_dxy"] = 0.0
        df["corr_vix"] = 0.0

    return df
