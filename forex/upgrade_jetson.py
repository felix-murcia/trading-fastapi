#!/usr/bin/env python3
"""Script to upgrade jetson-CORRECTED.json with v3.0 improvements."""

import json
import re

# Read the JSON file
with open('/home/felix/Public/n8n/forex/jetson-CORRECTED.json', 'r', encoding='utf-8') as f:
    data = json.load(f)

# Find nodes by ID
nodes_by_id = {node['id']: node for node in data['nodes']}

# ============================================
# 1. AGENTE TÉCNICO v3.0 UPGRADE
# ============================================
tecnico_node = nodes_by_id.get('5fc5138e-33bb-453b-815b-b76b64249002')
if tecnico_node:
    tecnico_code = r'''// ============================================
// AGENTE TÉCNICO v3.0 - ANÁLISIS AVANZADO
// ============================================
// MEJORAS:
// 1. Volume Analyzer v3.0 completo
// 2. MACD (12,26,9) calculado
// 3. Bollinger Bands (20,2)
// 4. ATR dinámico con multiplicador de volatilidad
// 5. Patrones avanzados: morning/evening star, 3 soldiers/crows, shooting star, hanging man
// 6. Clasificación de estado del mercado (trending vs ranging)
// 7. Prompt con datos estructurados JSON Schema
// ============================================

const context = $input.all()[0].json;
const marketData = context.market_data || {};

// ============================================
// EXTRAER DATOS DEL SÍMBOLO CORRECTO
// ============================================
const symbol = marketData.best_pair || "EURUSD";
const isJPY = symbol.includes("JPY");
const decimals = isJPY ? 1000 : 100000;
const pipMultiplier = isJPY ? 100 : 10000;

const currentPrice = marketData.technical?.current_price || 
    marketData.all_prices?.[symbol] || 0;

// ============================================
// DATOS DE VELAS
// ============================================
const recentCandles = marketData.technical?.candles_recent || [];

// ============================================
// CALCULAR SMAs
// ============================================
let sma9 = 0, sma20 = 0, sma50 = 0, sma200 = 0;
if (recentCandles.length >= 9) {
    sma9 = recentCandles.slice(-9).reduce((s, c) => s + (c.close || 0), 0) / 9;
}
if (recentCandles.length >= 20) {
    sma20 = recentCandles.slice(-20).reduce((s, c) => s + (c.close || 0), 0) / 20;
}
if (recentCandles.length >= 50) {
    sma50 = recentCandles.slice(-50).reduce((s, c) => s + (c.close || 0), 0) / 50;
}
if (recentCandles.length >= 200) {
    sma200 = recentCandles.slice(-200).reduce((s, c) => s + (c.close || 0), 0) / 200;
}

// ============================================
// CALCULAR ATR DINÁMICO (14-period con multiplicador de volatilidad)
// ============================================
let atr = 0;
let atrMultiplier = 1.0;
if (recentCandles.length >= 14) {
    let trueRanges = [];
    for (let i = 1; i < Math.min(15, recentCandles.length); i++) {
        const prev = recentCandles[i - 1];
        const curr = recentCandles[i];
        const high = curr.high || 0;
        const low = curr.low || 0;
        const prevClose = prev.close || 0;
        const trueRange = Math.max(
            high - low,
            Math.abs(high - prevClose),
            Math.abs(low - prevClose)
        );
        trueRanges.push(trueRange);
    }
    atr = trueRanges.reduce((s, tr) => s + tr, 0) / trueRanges.length;
    
    // Calcular multiplicador basado en volatilidad reciente
    const recentATR = trueRanges.slice(-5).reduce((s, v) => s + v, 0) / Math.min(5, trueRanges.length);
    const olderATR = trueRanges.slice(-14, -5).reduce((s, v) => s + v, 0) / Math.min(9, trueRanges.length - 5);
    if (olderATR > 0) {
        atrMultiplier = recentATR / olderATR;
        atrMultiplier = Math.max(0.5, Math.min(2.0, atrMultiplier));
    }
} else {
    atr = currentPrice * (isJPY ? 0.003 : 0.0003);
}

// ============================================
// CALCULAR MACD (12, 26, 9)
// ============================================
function calculateEMA(values, period) {
    if (values.length < period) return values[values.length - 1] || 0;
    const k = 2 / (period + 1);
    let ema = values.slice(0, period).reduce((s, v) => s + v, 0) / period;
    for (let i = period; i < values.length; i++) {
        ema = values[i] * k + ema * (1 - k);
    }
    return ema;
}

let macd = { macdLine: 0, signalLine: 0, histogram: 0, trend: "neutral" };
if (recentCandles.length >= 35) {
    const closes = recentCandles.map(c => c.close || 0);
    const ema12 = calculateEMA(closes, 12);
    const ema26 = calculateEMA(closes, 26);
    macd.macdLine = ema12 - ema26;
    
    // Calcular signal line (EMA 9 del MACD)
    const macdValues = [];
    const k9 = 2 / 10;
    let ema9 = 0;
    for (let i = 25; i < closes.length; i++) {
        const e12 = calculateEMA(closes.slice(0, i + 1), 12);
        const e26 = calculateEMA(closes.slice(0, i + 1), 26);
        macdValues.push(e12 - e26);
    }
    if (macdValues.length >= 9) {
        ema9 = calculateEMA(macdValues, 9);
        macd.signalLine = ema9;
        macd.histogram = macd.macdLine - macd.signalLine;
    }
    
    // Determinar tendencia MACD
    if (macd.histogram > 0 && macd.macdLine > macd.signalLine) {
        macd.trend = "bullish";
    } else if (macd.histogram < 0 && macd.macdLine < macd.signalLine) {
        macd.trend = "bearish";
    }
}

// ============================================
// CALCULAR BOLLINGER BANDS (20, 2)
// ============================================
let bollinger = { upper: 0, middle: 0, lower: 0, percentB: 0.5, bandwidth: 0, squeeze: false };
if (recentCandles.length >= 20) {
    const last20 = recentCandles.slice(-20).map(c => c.close || 0);
    bollinger.middle = last20.reduce((s, v) => s + v, 0) / 20;
    
    const variance = last20.reduce((s, v) => s + Math.pow(v - bollinger.middle, 2), 0) / 20;
    const stdDev = Math.sqrt(variance);
    
    bollinger.upper = bollinger.middle + 2 * stdDev;
    bollinger.lower = bollinger.middle - 2 * stdDev;
    
    if (currentPrice > 0 && (bollinger.upper - bollinger.lower) > 0) {
        bollinger.percentB = (currentPrice - bollinger.lower) / (bollinger.upper - bollinger.lower);
        bollinger.bandwidth = (bollinger.upper - bollinger.lower) / bollinger.middle;
        bollinger.squeeze = bollinger.bandwidth < 0.02; // Bollinger squeeze
    }
}

// ============================================
// VOLUME ANALYZER v3.0
// ============================================
function analyzeVolume(candles) {
    if (!candles || candles.length < 20) {
        return {
            volume_ratio: 1, volume_trend: "unknown", price_volume_divergence: false,
            exhaustion_detected: false, breakout_confirmed: false,
            volume_signal: "neutral", confidence_adjustment: 0
        };
    }
    
    const lastCandle = candles[candles.length - 1];
    const prevCandle = candles[candles.length - 2];
    const currentVolume = lastCandle?.tick_volume || 0;
    const avgVolume = candles.slice(-20).reduce((sum, c) => sum + (c.tick_volume || 0), 0) / 20;
    const volumeRatio = avgVolume > 0 ? currentVolume / avgVolume : 1;
    
    // Tendencia de volumen (últimas 5 velas)
    const recentVolumes = candles.slice(-5).map(c => c.tick_volume || 0);
    let volumeTrend = "flat";
    if (recentVolumes.length >= 3) {
        const firstHalf = recentVolumes.slice(0, 2).reduce((s, v) => s + v, 0) / 2;
        const secondHalf = recentVolumes.slice(-2).reduce((s, v) => s + v, 0) / 2;
        if (secondHalf > firstHalf * 1.1) volumeTrend = "increasing";
        else if (secondHalf < firstHalf * 0.9) volumeTrend = "decreasing";
    }
    
    // Divergencia precio-volumen
    const priceUp = (lastCandle.close || 0) > (prevCandle.close || 0);
    const volUp = currentVolume > avgVolume;
    const priceVolumeDivergence = (priceUp && !volUp && volumeRatio < 0.7) || (!priceUp && volUp && volumeRatio > 1.3);
    
    // Detección de agotamiento
    const priceChange = Math.abs((lastCandle.close || 0) - (prevCandle.close || 0));
    const avgPriceChange = candles.slice(-10).reduce((sum, _, i, arr) => {
        if (i === 0) return 0;
        return sum + Math.abs((arr[i].close || 0) - (arr[i-1].close || 0));
    }, 0) / 9;
    const exhaustionDetected = volumeRatio > 2.0 && priceChange < avgPriceChange * 0.3;
    
    // Confirmación de ruptura
    const support = Math.min(...candles.slice(-20).map(c => c.low || 0));
    const resistance = Math.max(...candles.slice(-20).map(c => c.high || 0));
    const breakoutConfirmed = volumeRatio > 1.5 && (
        (lastCandle.close || 0) > resistance * 1.001 || (lastCandle.close || 0) < support * 0.999
    );
    
    // Señal compuesta
    let volumeSignal = "neutral";
    let confidenceAdjustment = 0;
    if (volumeRatio > 1.5 && !priceVolumeDivergence && !exhaustionDetected) {
        volumeSignal = "confirming"; confidenceAdjustment = 10;
    } else if (exhaustionDetected) {
        volumeSignal = "exhaustion"; confidenceAdjustment = -15;
    } else if (priceVolumeDivergence) {
        volumeSignal = "divergence"; confidenceAdjustment = -10;
    } else if (breakoutConfirmed) {
        volumeSignal = "breakout"; confidenceAdjustment = 15;
    } else if (volumeRatio < 0.5) {
        volumeSignal = "low_participation"; confidenceAdjustment = -5;
    }
    
    return {
        volume_ratio: Math.round(volumeRatio * 100) / 100,
        volume_trend: volumeTrend,
        price_volume_divergence: priceVolumeDivergence,
        exhaustion_detected: exhaustionDetected,
        breakout_confirmed: breakoutConfirmed,
        volume_signal: volumeSignal,
        confidence_adjustment: confidenceAdjustment
    };
}

const volumeAnalysis = analyzeVolume(recentCandles);

// ============================================
// DETECCIÓN AVANZADA DE PATRONES DE VELAS
// ============================================
let candlePattern = "none";
if (recentCandles.length >= 2) {
    const last = recentCandles[recentCandles.length - 1];
    const prev = recentCandles[recentCandles.length - 2];
    
    const lastBody = Math.abs((last.close || 0) - (last.open || 0));
    const lastRange = (last.high || 0) - (last.low || 0);
    const prevBody = Math.abs((prev.close || 0) - (prev.open || 0));
    const lastIsBullish = (last.close || 0) > (last.open || 0);
    const prevIsBullish = (prev.close || 0) > (prev.open || 0);
    
    if (lastRange > 0 && lastBody / lastRange < 0.1) {
        candlePattern = "doji";
    } else if (lastRange > 0) {
        const lowerWick = Math.min(last.close || 0, last.open || 0) - (last.low || 0);
        const upperWick = (last.high || 0) - Math.max(last.close || 0, last.open || 0);
        if (lowerWick > lastBody * 2 && upperWick < lastBody) {
            candlePattern = lastIsBullish ? "hammer_bullish" : "hammer_bearish";
        } else if (lastBody > prevBody * 1.5 && lastIsBullish !== prevIsBullish) {
            candlePattern = lastIsBullish ? "bullish_engulfing" : "bearish_engulfing";
        }
    }
}

// Patrones avanzados (requieren 3+ velas)
if (recentCandles.length >= 3 && candlePattern === "none") {
    const c1 = recentCandles[recentCandles.length - 1];
    const c2 = recentCandles[recentCandles.length - 2];
    const c3 = recentCandles[recentCandles.length - 3];
    
    const b1 = (c1.close || 0) > (c1.open || 0);
    const b2 = (c2.close || 0) > (c2.open || 0);
    const b3 = (c3.close || 0) > (c3.open || 0);
    
    // Three White Soldiers / Three Black Crows
    if (b1 && b2 && b3 && (c1.close || 0) > (c2.close || 0) && (c2.close || 0) > (c3.close || 0)) {
        candlePattern = "three_white_soldiers";
    } else if (!b1 && !b2 && !b3 && (c1.close || 0) < (c2.close || 0) && (c2.close || 0) < (c3.close || 0)) {
        candlePattern = "three_black_crows";
    }
    
    // Morning Star / Evening Star
    if (candlePattern === "none") {
        const body3 = Math.abs((c3.close || 0) - (c3.open || 0));
        const body2 = Math.abs((c2.close || 0) - (c2.open || 0));
        const body1 = Math.abs((c1.close || 0) - (c1.open || 0));
        if (body2 < body3 * 0.3 && body2 < body1 * 0.3) {
            if (!b3 && b1 && (c1.close || 0) > (c3.open || 0) - body3 * 0.5) {
                candlePattern = "morning_star";
            } else if (b3 && !b1 && (c1.close || 0) < (c3.open || 0) + body3 * 0.5) {
                candlePattern = "evening_star";
            }
        }
    }
    
    // Shooting Star / Hanging Man
    if (candlePattern === "none") {
        const range1 = (c1.high || 0) - (c1.low || 0);
        const lowerWick1 = Math.min(c1.close || 0, c1.open || 0) - (c1.low || 0);
        const upperWick1 = (c1.high || 0) - Math.max(c1.close || 0, c1.open || 0);
        const body1 = Math.abs((c1.close || 0) - (c1.open || 0));
        
        if (range1 > 0 && upperWick1 > body1 * 2 && lowerWick1 < body1 * 0.5) {
            candlePattern = "shooting_star";
        } else if (range1 > 0 && lowerWick1 > body1 * 2 && upperWick1 < body1 * 0.5) {
            candlePattern = "hanging_man";
        }
    }
}

// ============================================
// CLASIFICACIÓN DE ESTADO DEL MERCADO
// ============================================
function classifyMarketState(candles, currentPrice) {
    if (candles.length < 50) return { state: "unknown", adx_estimate: 20 };
    
    const closes = candles.map(c => c.close || 0);
    const sma20 = closes.slice(-20).reduce((s, v) => s + v, 0) / 20;
    const sma50 = closes.slice(-50).reduce((s, v) => s + v, 0) / 50;
    
    // Estimación de ADX basada en spread de SMAs
    const smaSpread = Math.abs(sma20 - sma50) / sma50;
    const adxEstimate = Math.min(100, smaSpread * 10000);
    
    // Rango de precio reciente vs ATR
    const recentHigh = Math.max(...candles.slice(-20).map(c => c.high || 0));
    const recentLow = Math.min(...candles.slice(-20).map(c => c.low || 0));
    const priceRange = recentHigh - recentLow;
    const atrRatio = atr > 0 ? priceRange / (atr * 20) : 1;
    
    let state = "ranging";
    if (adxEstimate > 25 && atrRatio > 0.8) {
        state = "trending";
    } else if (adxEstimate < 15) {
        state = "ranging";
    }
    
    return { state, adx_estimate: Math.round(adxEstimate), atr_ratio: Math.round(atrRatio * 100) / 100 };
}

const marketState = classifyMarketState(recentCandles, currentPrice);

// ============================================
// INDICADORES TÉCNICOS
// ============================================
const rsi = marketData.technical?.rsi || 50;
const rsiStatus = rsi < 30 ? "oversold" : (rsi > 70 ? "overbought" : "neutral");
const trend = marketData.technical?.trend || "neutral";
const support = marketData.technical?.support || (currentPrice * 0.995);
const resistance = marketData.technical?.resistance || (currentPrice * 1.005);
const score = marketData.best_score || 50;

let priceVsSma9 = "neutral";
let priceVsSma20 = "neutral";
let priceVsSma50 = "neutral";
if (currentPrice > 0) {
    if (sma9 > 0) priceVsSma9 = currentPrice > sma9 * 1.001 ? "above" : (currentPrice < sma9 * 0.999 ? "below" : "at");
    if (sma20 > 0) priceVsSma20 = currentPrice > sma20 * 1.001 ? "above" : (currentPrice < sma20 * 0.999 ? "below" : "at");
    if (sma50 > 0) priceVsSma50 = currentPrice > sma50 * 1.001 ? "above" : (currentPrice < sma50 * 0.999 ? "below" : "at");
}

// ============================================
// FORTALEZA DE DIVISAS
// ============================================
const currencyStrength = marketData.currency_strength || {};
const baseCurrency = symbol.substring(0, 3);
const quoteCurrency = symbol.substring(3, 6);
const baseStrength = currencyStrength.strength?.[baseCurrency] || "neutral";
const quoteStrength = currencyStrength.strength?.[quoteCurrency] || "neutral";

// ============================================
// EXPOSICIÓN ACTUAL
// ============================================
const positions = marketData.positions?.details || [];
const symbolPositions = positions.filter(p => p.symbol === symbol);
const totalExposure = symbolPositions.reduce((sum, p) => sum + Math.abs(p.volume || 0), 0);

// ============================================
// CONSTRUIR PROMPT v3.0 CON JSON SCHEMA
// ============================================
const last5Candles = recentCandles.slice(-5).map((c, i) => {
    const isBullish = (c.close || 0) > (c.open || 0);
    return `Candle${i+1}: O:${c.open || 0} H:${c.high || 0} L:${c.low || 0} C:${c.close || 0} V:${c.tick_volume || 0} ${isBullish ? "BULL" : "BEAR"}`;
}).join(" | ");

const prompt = `[INST] You are an expert forex trading analyst. Analyze ${symbol} with FULL v3.0 technical data.

═══════════════════════════════════════════
📊 TECHNICAL DATA FOR ${symbol} (v3.0)
═══════════════════════════════════════════
Current Price: ${currentPrice.toFixed(decimals === 1000 ? 3 : 5)}
RSI (14): ${rsi} (${rsiStatus.toUpperCase()})
Trend: ${trend.toUpperCase()} | Technical Score: ${score}/100

═══ MOVING AVERAGES ═══
SMA 9: ${sma9.toFixed(decimals === 1000 ? 3 : 5)} | Price: ${priceVsSma9.toUpperCase()}
SMA 20: ${sma20.toFixed(decimals === 1000 ? 3 : 5)} | Price: ${priceVsSma20.toUpperCase()}
SMA 50: ${sma50.toFixed(decimals === 1000 ? 3 : 5)} | Price: ${priceVsSma50.toUpperCase()}

═══ MACD (12,26,9) ═══
MACD Line: ${macd.macdLine.toFixed(decimals === 1000 ? 4 : 6)}
Signal Line: ${macd.signalLine.toFixed(decimals === 1000 ? 4 : 6)}
Histogram: ${macd.histogram.toFixed(decimals === 1000 ? 4 : 6)}
MACD Trend: ${macd.trend.toUpperCase()}

═══ BOLLINGER BANDS (20,2) ═══
Upper: ${bollinger.upper.toFixed(decimals === 1000 ? 3 : 5)}
Middle: ${bollinger.middle.toFixed(decimals === 1000 ? 3 : 5)}
Lower: ${bollinger.lower.toFixed(decimals === 1000 ? 3 : 5)}
%B: ${bollinger.percentB.toFixed(2)} | Bandwidth: ${(bollinger.bandwidth * 100).toFixed(2)}%
Squeeze: ${bollinger.squeeze ? "YES (breakout imminent)" : "NO"}

═══ SUPPORT/RESISTANCE ═══
Support: ${support.toFixed(decimals === 1000 ? 3 : 5)}
Resistance: ${resistance.toFixed(decimals === 1000 ? 3 : 5)}

═══ VOLATILITY ═══
ATR (14): ${atr.toFixed(decimals === 1000 ? 3 : 5)} (${(atr / currentPrice * 100).toFixed(3)}%)
ATR Multiplier: ${atrMultiplier.toFixed(2)}x (${atrMultiplier > 1.2 ? "HIGH VOL" : atrMultiplier < 0.8 ? "LOW VOL" : "NORMAL"})

═══ VOLUME ANALYSIS v3.0 ═══
Volume Ratio: ${volumeAnalysis.volume_ratio.toFixed(2)}x ${volumeAnalysis.volume_ratio > 1.5 ? "(HIGH)" : "(NORMAL)"}
Volume Trend: ${volumeAnalysis.volume_trend.toUpperCase()}
Volume Signal: ${volumeAnalysis.volume_signal.toUpperCase()}
Price/Volume Divergence: ${volumeAnalysis.price_volume_divergence ? "YES ⚠️" : "NO"}
Exhaustion: ${volumeAnalysis.exhaustion_detected ? "YES ⚠️" : "NO"}
Breakout Confirmed: ${volumeAnalysis.breakout_confirmed ? "YES ✓" : "NO"}
Confidence Adjustment: ${volumeAnalysis.confidence_adjustment > 0 ? "+" : ""}${volumeAnalysis.confidence_adjustment}

═══ CANDLESTICK PATTERNS ═══
Detected Pattern: ${candlePattern.toUpperCase()}
Last 5 Candles: ${last5Candles}

═══ MARKET STATE ═══
State: ${marketState.state.toUpperCase()}
ADX Estimate: ${marketState.adx_estimate}
ATR Ratio: ${marketState.atr_ratio}

═══ CURRENCY STRENGTH ═══
${baseCurrency}: ${baseStrength.toUpperCase()}
${quoteCurrency}: ${quoteStrength.toUpperCase()}
${baseStrength === "bullish" && quoteStrength === "bearish" ? "→ STRONG BULLISH setup for " + symbol : ""}${baseStrength === "bearish" && quoteStrength === "bullish" ? "→ STRONG BEARISH setup for " + symbol : ""}

═══ CURRENT EXPOSURE ═══
Open Positions in ${symbol}: ${symbolPositions.length}
Total Exposure: ${totalExposure.toFixed(2)} lots

═══════════════════════════════════════════
📋 REQUIRED JSON OUTPUT (JSON Schema):
═══════════════════════════════════════════
Return ONLY valid JSON matching this schema:
{
  "signal": "buy" | "sell" | "neutral",
  "confidence": 0-100,
  "analysis": "Brief technical reasoning (1 sentence)",
  "structure": "bullish" | "bearish" | "range",
  "momentum": "strong" | "moderate" | "weak",
  "entry_zone": [low, high],
  "stop_zone": [low, high]
}

⚠️ CRITICAL RULES:
- entry_zone MUST be within ±0.2% of ${currentPrice}
- Use EXACT price ${currentPrice} as reference
- Consider MACD ${macd.trend} trend, Bollinger %B ${bollinger.percentB.toFixed(2)}, volume ${volumeAnalysis.volume_signal}
- Market is ${marketState.state} (ADX: ${marketState.adx_estimate})
- Pattern: ${candlePattern}
- If exhaustion or divergence detected, reduce confidence [/INST]`;

console.log(`📈 Agente Técnico v3.0 - ${symbol}`);
console.log(`   Price: ${currentPrice} | RSI: ${rsi} | Trend: ${trend}`);
console.log(`   MACD: ${macd.trend} | BB: ${(bollinger.percentB * 100).toFixed(0)}% | State: ${marketState.state}`);
console.log(`   Pattern: ${candlePattern} | Volume: ${volumeAnalysis.volume_signal} (${volumeAnalysis.confidence_adjustment > 0 ? '+' : ''}${volumeAnalysis.confidence_adjustment})`);
console.log(`   Prompt length: ${prompt.length} chars`);

return [{
    json: {
        agent: "technical",
        context: context,
        technical_data: {
            symbol: symbol,
            price: currentPrice,
            rsi: rsi,
            trend: trend,
            support: support,
            resistance: resistance,
            score: score,
            sma9: sma9,
            sma20: sma20,
            sma50: sma50,
            atr: atr,
            atr_multiplier: atrMultiplier,
            macd: macd,
            bollinger: bollinger,
            volume_analysis: volumeAnalysis,
            candle_pattern: candlePattern,
            market_state: marketState,
            base_strength: baseStrength,
            quote_strength: quoteStrength,
            current_exposure: totalExposure
        },
        prompt: prompt,
        status: "ready"
    }
}];'''
    
    tecnico_node['parameters']['jsCode'] = tecnico_code
    print("✅ Agente Técnico upgraded to v3.0")
else:
    print("❌ Agente Técnico node not found")

# ============================================
# 2. AGENTE FUNDAMENTAL v3.0 UPGRADE
# ============================================
fundamental_node = nodes_by_id.get('410d0e5e-c66d-435c-964e-cd96af95b646')
if fundamental_node:
    fundamental_code = r'''// ============================================
// AGENTE FUNDAMENTAL v3.0 - WEIGHTED NEWS SCORING
// ============================================
// MEJORAS:
// 1. Weighted News Scoring con decaimiento exponencial (half-life 24h)
// 2. Peso por impacto: high=3, medium=2, low=1
// 3. Consistencia de fuente (múltiples fuentes = boost)
// 4. Sentiment_score compuesto de -100 a +100
// 5. Análisis de eventos del calendario con forecast
// ============================================

const context = $input.all()[0].json;
const marketData = context.market_data || {};

// ============================================
// EXTRAER DATOS DEL SÍMBOLO CORRECTO
// ============================================
const symbol = marketData.best_pair || "EURUSD";
const baseCurrency = symbol.substring(0, 3);
const quoteCurrency = symbol.substring(3, 6);

const fundamentalData = marketData.fundamental || {};
const newsList = fundamentalData.relevant_news || [];
const calendarList = fundamentalData.calendar_events || [];

// ============================================
// WEIGHTED NEWS SCORING v3.0
// ============================================
function calculateWeightedNewsScore(newsList, now) {
    const HALF_LIFE_MS = 24 * 60 * 60 * 1000; // 24 horas
    const DECAY_CONSTANT = Math.log(2) / HALF_LIFE_MS;
    
    const IMPACT_WEIGHT = { high: 3, medium: 2, low: 1 };
    
    let totalScore = 0;
    let totalWeight = 0;
    let bullishCount = 0, bearishCount = 0, neutralCount = 0;
    let highImpactNews = [];
    let newsHeadlines = [];
    let sourceCounts = { bullish: 0, bearish: 0 };
    
    for (const news of newsList) {
        const sentiment = (news.sentiment || "neutral").toLowerCase();
        const impact = (news.impact || news.relevance || "medium").toLowerCase();
        
        // Determinar score base (-1 para bearish, 0 para neutral, +1 para bullish)
        let baseSentiment = 0;
        if (sentiment === "bullish") { baseSentiment = 1; bullishCount++; }
        else if (sentiment === "bearish") { baseSentiment = -1; bearishCount++; }
        else { neutralCount++; }
        
        // Peso por impacto
        const impactWeight = IMPACT_WEIGHT[impact] || 2;
        
        // Peso por recencia (decaimiento exponencial)
        let recencyWeight = 1.0;
        if (news.timestamp || news.published_at || news.date) {
            const newsDate = new Date(news.timestamp || news.published_at || news.date);
            const ageMs = now.getTime() - newsDate.getTime();
            if (ageMs > 0 && ageMs < HALF_LIFE_MS * 7) { // Máximo 7 días
                recencyWeight = Math.exp(-DECAY_CONSTANT * ageMs);
            } else if (ageMs