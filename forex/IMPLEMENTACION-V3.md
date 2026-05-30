# GUÍA DE IMPLEMENTACIÓN v3.0 - SISTEMA DE TRADING FOREX

## RESUMEN

Esta guía contiene todo el código necesario para actualizar los nodos del sistema de trading a la versión 3.0. Cada sección tiene el código completo que debes copiar y pegar en el nodo correspondiente.

---

## ARCHIVOS MODIFICADOS

| Archivo | Nodos Actualizados | Versión |
|---------|-------------------|---------|
| `multi-agente-profesional-CORRECTED.json` | Analizar Pares, Memory Manager, Agente Estratega, Preparar Orden + nuevo Risk Manager | v2.0 → v3.0 |
| `jetson-CORRECTED.json` | Agente Técnico, Agente Fundamental, Agente Sentimiento | v2.0 → v3.0 |

---

## 1. NODO: Analizar Pares (v3.0)

**Archivo**: `multi-agente-profesional-CORRECTED.json`
**Nodo**: `Analizar Pares`

**Reemplazar TODO el código del nodo con el siguiente:**

```javascript
// ============================================
// ANALIZAR PARES - VERSIÓN v3.0 MEJORADA
// ============================================
// MEJORAS v3.0:
// 1. Indicadores avanzados: MACD, Bollinger Bands, ATR
// 2. Análisis de volumen con divergencia precio-volumen
// 3. Clasificación de estado del mercado
// 4. Correlaciones entre pares
// 5. News Scoring ponderado
// 6. Detección mejorada de patrones de velas
// ============================================

const allItems = $input.all();
console.log(`📊 Recibidos ${allItems.length} items para analizar`);

// ============================================
// ESTRUCTURAS DE DATOS
// ============================================
const pairsAnalysis = {};
let bestPair = null;
let bestScore = 0;

// Datos globales
let globalAccountData = {};
let globalNews = [];
let globalCalendar = [];
let globalPositions = [];
let globalPendingOrders = [];
let currencyStrength = null;
let globalRates = null;

// ============================================
// PRECIOS POR DEFECTO Y VALIDACIÓN
// ============================================
const DEFAULT_PRICES = {
    EURUSD: 1.1689,
    GBPUSD: 1.3570,
    USDJPY: 159.88,
    USDCHF: 0.7996
};

const VALID_RANGES = {
    EURUSD: { min: 1.05, max: 1.20 },
    GBPUSD: { min: 1.25, max: 1.40 },
    USDJPY: { min: 140.0, max: 170.0 },
    USDCHF: { min: 0.85, max: 1.00 }
};

// ============================================
// CORRELACIONES HISTÓRICAS ENTRE PARES
// ============================================
const PAIR_CORRELATIONS = {
    EURUSD: { GBPUSD: 0.85, USDCHF: -0.90, USDJPY: -0.30 },
    GBPUSD: { EURUSD: 0.85, USDCHF: -0.82, USDJPY: -0.25 },
    USDJPY: { EURUSD: -0.30, GBPUSD: -0.25, USDCHF: 0.35 },
    USDCHF: { EURUSD: -0.90, GBPUSD: -0.82, USDJPY: 0.35 }
};

// ============================================
// PESOS DE DIVISAS PARA NEWS SCORING
// ============================================
const CURRENCY_WEIGHTS = {
    USD: 1.0,
    EUR: 0.9,
    GBP: 0.85,
    JPY: 0.75,
    CHF: 0.65
};

// ============================================
// FUNCIÓN: Validar precio
// ============================================
function validatePrice(symbol, price, source) {
    if (price === null || price === undefined || isNaN(price)) {
        console.log(`   ⚠️ ${symbol}: Precio inválido (${price}) de ${source}, usando default`);
        return { valid: false, price: DEFAULT_PRICES[symbol] };
    }
    
    const range = VALID_RANGES[symbol];
    if (price < range.min || price > range.max) {
        console.log(`   ⚠️ ${symbol}: Precio ${price} FUERA DE RANGO [${range.min}-${range.max}] desde ${source}, usando default`);
        return { valid: false, price: DEFAULT_PRICES[symbol] };
    }
    
    console.log(`   ✅ ${symbol}: Precio ${price} válido desde ${source}`);
    return { valid: true, price: price };
}

// ============================================
// INDICADORES AVANZADOS v3.0
// ============================================

// MACD con histograma
function calculateMACD(candles, fast = 12, slow = 26, signal = 9) {
    if (!candles || candles.length < slow + signal) {
        return { macd: 0, signal: 0, histogram: 0, crossover: "none" };
    }
    
    const closes = candles.map(c => c.close || 0);
    
    function ema(data, period) {
        const k = 2 / (period + 1);
        let result = data.slice(0, period).reduce((s, v) => s + v, 0) / period;
        for (let i = period; i < data.length; i++) {
            result = data[i] * k + result * (1 - k);
        }
        return result;
    }
    
    // Calcular MACD line
    const fastEMA = ema(closes, fast);
    const slowEMA = ema(closes, slow);
    const macdLine = fastEMA - slowEMA;
    
    // Calcular señal (simplificado - EMA del MACD reciente)
    const macdHistory = [];
    for (let i = closes.length - signal; i < closes.length; i++) {
        const fEma = ema(closes.slice(0, i + 1), fast);
        const sEma = ema(closes.slice(0, i + 1), slow);
        macdHistory.push(fEma - sEma);
    }
    const signalLine = macdHistory.length > 0 ? 
        macdHistory.reduce((s, v) => s + v, 0) / macdHistory.length : macdLine;
    
    const histogram = macdLine - signalLine;
    
    // Detectar cruce
    let crossover = "none";
    if (macdHistory.length >= 2) {
        const prevHist = macdHistory[macdHistory.length - 2] - signalLine;
        const currHist = histogram;
        if (prevHist < 0 && currHist > 0) crossover = "bullish";
        else if (prevHist > 0 && currHist < 0) crossover = "bearish";
    }
    
    return {
        macd: macdLine,
        signal: signalLine,
        histogram: histogram,
        crossover: crossover
    };
}

// Bollinger Bands
function calculateBollingerBands(candles, period = 20, stdDev = 2) {
    if (!candles || candles.length < period) {
        return { upper: 0, middle: 0, lower: 0, percentB: 0.5, squeeze: false };
    }
    
    const closes = candles.slice(-period).map(c => c.close || 0);
    const middle = closes.reduce((s, v) => s + v, 0) / period;
    
    const variance = closes.reduce((s, v) => s + Math.pow(v - middle, 2), 0) / period;
    const std = Math.sqrt(variance);
    
    const upper = middle + stdDev * std;
    const lower = middle - stdDev * std;
    
    const currentPrice = closes[closes.length - 1];
    const percentB = (upper !== lower) ? (currentPrice - lower) / (upper - lower) : 0.5;
    
    // Detectar squeeze (bandas estrechas)
    const bandwidth = (upper - lower) / middle;
    const squeeze = bandwidth < 0.02; // Menos del 2% = squeeze
    
    return { upper, middle, lower, percentB, bandwidth, squeeze };
}

// ATR (Average True Range)
function calculateATR(candles, period = 14) {
    if (!candles || candles.length < period + 1) {
        return 0;
    }
    
    let trueRanges = [];
    for (let i = candles.length - period; i < candles.length; i++) {
        const prev = candles[i - 1];
        const curr = candles[i];
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
    
    return trueRanges.reduce((s, tr) => s + tr, 0) / trueRanges.length;
}

// Análisis de volumen avanzado
function analyzeVolume(candles, currentPrice, trend) {
    if (!candles || candles.length < 20) {
        return {
            volume_ratio: 1, volume_trend: "unknown",
            price_volume_divergence: false, exhaustion_detected: false,
            breakout_confirmed: false, volume_signal: "neutral",
            confidence_adjustment: 0
        };
    }
    
    const lastCandle = candles[candles.length - 1];
    const prevCandle = candles[candles.length - 2];
    
    const currentVolume = lastCandle?.tick_volume || 0;
    const avgVolume = candles.slice(-20).reduce((sum, c) => sum + (c.tick_volume || 0), 0) / 20;
    const volumeRatio = avgVolume > 0 ? currentVolume / avgVolume : 1;
    
    // Tendencia de volumen (últimas 5 velas)
    const recentVolumes = candles.slice(-5).map(c => c.tick_volume || 0);
    const volumeTrend = recentVolumes.length >= 3 ?
        (recentVolumes[recentVolumes.length - 1] > recentVolumes[0] ? "up" : "down") : "flat";
    
    // Divergencia precio-volumen
    const last10Prices = candles.slice(-10).map(c => c.close || 0);
    const last10Volumes = candles.slice(-10).map(c => c.tick_volume || 0);
    
    const priceTrend = last10Prices.length >= 3 ?
        (last10Prices[last10Prices.length - 1] > last10Prices[0] ? "up" : "down") : "flat";
    const volTrend10 = last10Volumes.length >= 3 ?
        (last10Volumes[last10Volumes.length - 1] > last10Volumes[0] ? "up" : "down") : "flat";
    
    const priceVolumeDivergence = (
        (priceTrend === "up" && volTrend10 === "down") ||
        (priceTrend === "down" && volTrend10 === "up")
    );
    
    // Detección de agotamiento
    const lastPriceChange = Math.abs((lastCandle.close || 0) - (prevCandle.close || 0));
    const avgPriceChange = candles.slice(-10).reduce((sum, c, i, arr) => {
        if (i === 0) return 0;
        return sum + Math.abs((arr[i].close || 0) - (arr[i-1].close || 0));
    }, 0) / 9;
    
    const exhaustionDetected = (volumeRatio > 2.0 && lastPriceChange < avgPriceChange * 0.3);
    
    // Confirmación de ruptura
    const support = Math.min(...candles.slice(-20).map(c => c.low || 0));
    const resistance = Math.max(...candles.slice(-20).map(c => c.high || 0));
    const breakoutConfirmed = (
        volumeRatio > 1.5 && (
            (lastCandle.close || 0) > resistance * 1.001 ||
            (lastCandle.close || 0) < support * 0.999
        )
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

// Detección avanzada de patrones de velas
function detectCandlePatterns(candles) {
    if (!candles || candles.length < 3) return { pattern: "none", strength: 0 };
    
    const last = candles[candles.length - 1];
    const prev = candles[candles.length - 2];
    const prev2 = candles[candles.length - 3];
    
    const lastBody = Math.abs((last.close || 0) - (last.open || 0));
    const lastRange = (last.high || 0) - (last.low || 0);
    const prevBody = Math.abs((prev.close || 0) - (prev.open || 0));
    const prev2Body = Math.abs((prev2.close || 0) - (prev2.open || 0));
    
    const lastIsBullish = (last.close || 0) > (last.open || 0);
    const prevIsBullish = (prev.close || 0) > (prev.open || 0);
    const prev2IsBullish = (prev2.close || 0) > (prev2.open || 0);
    
    let pattern = "none";
    let strength = 0;
    
    // Doji
    if (lastRange > 0 && lastBody / lastRange < 0.1) {
        pattern = "doji"; strength = 60;
    }
    // Hammer
    else if (lastRange > 0) {
        const lowerWick = Math.min(last.close || 0, last.open || 0) - (last.low || 0);
        const upperWick = (last.high || 0) - Math.max(last.close || 0, last.open || 0);
        if (lowerWick > lastBody * 2 && upperWick < lastBody) {
            pattern = lastIsBullish ? "hammer_bullish" : "hammer_bearish";
            strength = 70;
        }
        // Engulfing
        else if (lastBody > prevBody * 1.5 && lastIsBullish !== prevIsBullish) {
            pattern = lastIsBullish ? "bullish_engulfing" : "bearish_engulfing";
            strength = 75;
        }
        // Morning/Evening Star
        else if (candles.length >= 3 && prevBody < lastBody * 0.3 && prev2Body > prevBody * 2) {
            if (prev2IsBullish && !prevIsBullish && lastIsBullish) {
                pattern = "morning_star"; strength = 80;
            } else if (!prev2IsBullish && prevIsBullish && !lastIsBullish) {
                pattern = "evening_star"; strength = 80;
            }
        }
    }
    
    return { pattern, strength };
}

// Clasificación del estado del mercado
function classifyMarketState(candles, atr, bollinger) {
    if (!candles || candles.length < 20) {
        return { trend: "unknown", volatility: "unknown", session: "unknown", recommended_strategy: "range_trading" };
    }
    
    const now = new Date();
    const hourUTC = now.getUTCHours();
    
    // Clasificar sesión
    let session = "unknown";
    if (hourUTC >= 0 && hourUTC < 7) session = "asian";
    else if (hourUTC >= 7 && hourUTC < 12) session = "european";
    else if (hourUTC >= 12 && hourUTC < 17) session = "overlap";
    else if (hourUTC >= 17 && hourUTC < 22) session = "us";
    else session = "late";
    
    // Clasificar volatilidad
    const currentPrice = candles[candles.length - 1].close || 1;
    const atrPercent = (atr / currentPrice) * 100;
    
    let volatility = "normal";
    if (atrPercent > 0.15 || bollinger.squeeze === false && bollinger.bandwidth > 0.03) volatility = "high";
    else if (bollinger.squeeze || atrPercent < 0.05) volatility = "low";
    
    // Tendencia
    const closes = candles.map(c => c.close || 0);
    const sma20 = closes.slice(-20).reduce((s, v) => s + v, 0) / 20;
    const sma50 = closes.length >= 50 ? closes.slice(-50).reduce((s, v) => s + v, 0) / 50 : sma20;
    
    let trend = "consolidation";
    if (sma20 > sma50 * 1.002) trend = "bullish";
    else if (sma20 < sma50 * 0.998) trend = "bearish";
    
    // Estrategia recomendada
    let recommended_strategy = "range_trading";
    if (trend === "bullish" || trend === "bearish") {
        if (volatility === "high") recommended_strategy = "breakout";
        else recommended_strategy = "trend_following";
    } else if (volatility === "low") {
        recommended_strategy = "mean_reversion";
    }
    
    return { trend, volatility, session, recommended_strategy, atr_percent: atrPercent.toFixed(3) };
}

// News Scoring ponderado
function scoreNews(newsList, symbol) {
    if (!newsList || newsList.length === 0) return { score: 0, bias: "neutral", confidence: 0 };
    
    const baseCurrency = symbol.substring(0, 3);
    const quoteCurrency = symbol.substring(3, 6);
    
    // Pesos por tipo de noticia
    const NEWS_TYPE_WEIGHTS = {
        "NFP": 1.0, "CPI": 0.9, "rate_decision": 0.95,
        "GDP": 0.85, "employment": 0.8, "retail_sales": 0.75,
        "PMI": 0.7, "trade_balance": 0.65, "default": 0.6
    };
    
    let bullishScore = 0;
    let bearishScore = 0;
    let totalWeight = 0;
    
    for (const news of newsList) {
        const currency = news.currency || "";
        const newsType = news.type || "default";
        const sentiment = (news.sentiment || "neutral").toLowerCase();
        const impact = (news.impact || news.relevance || "medium").toLowerCase();
        
        // Solo contar noticias de las divisas relevantes
        if (currency !== baseCurrency && currency !== quoteCurrency) continue;
        
        // Peso compuesto
        const currencyWeight = CURRENCY_WEIGHTS[currency] || 0.5;
        const typeWeight = NEWS_TYPE_WEIGHTS[newsType] || NEWS_TYPE_WEIGHTS.default;
        const impactMultiplier = impact === "high" ? 1.5 : (impact === "medium" ? 1.0 : 0.5);
        
        const weight = currencyWeight * typeWeight * impactMultiplier;
        totalWeight += weight;
        
        if (sentiment === "bullish") bullishScore += weight;
        else if (sentiment === "bearish") bearishScore += weight;
    }
    
    if (totalWeight === 0) return { score: 0, bias: "neutral", confidence: 0 };
    
    const netScore = ((bullishScore - bearishScore) / totalWeight) * 100;
    const bias = netScore > 15 ? "bullish" : (netScore < -15 ? "bearish" : "neutral");
    const confidence = Math.min(100, Math.abs(netScore) * 2);
    
    return { score: Math.round(netScore), bias, confidence: Math.round(confidence) };
}

// Economic Calendar Scoring
function scoreCalendar(events, symbol) {
    if (!events || events.length === 0) return { risk_level: "low", act_wait: "act", score: 0 };
    
    const baseCurrency = symbol.substring(0, 3);
    const quoteCurrency = symbol.substring(3, 6);
    
    const IMPACT_WEIGHTS = { high: 3, medium: 2, low: 1 };
    let totalRisk = 0;
    let highImpactCount = 0;
    
    for (const event of events) {
        const currency = event.currency || "";
        if (currency !== baseCurrency && currency !== quoteCurrency) continue;
        
        const impact = (event.impact || "medium").toLowerCase();
        const weight = IMPACT_WEIGHTS[impact] || 2;
        
        // Penalizar por proximidad temporal (simplificado)
        const timeMultiplier = 1.5; // Asumir eventos próximos
        
        totalRisk += weight * timeMultiplier;
        if (impact === "high") highImpactCount++;
    }
    
    let risk_level = "low";
    let act_wait = "act";
    
    if (totalRisk > 6 || highImpactCount >= 2) {
        risk_level = "high";
        act_wait = "wait";
    } else if (totalRisk > 3 || highImpactCount >= 1) {
        risk_level = "medium";
        act_wait = "act";
    }
    
    return { risk_level, act_wait, score: totalRisk, high_impact_count: highImpactCount };
}

// Ajustar por correlaciones
function adjustByCorrelations(symbol, signals) {
    const correlations = PAIR_CORRELATIONS[symbol] || {};
    let correlationAdjustment = 0;
    
    for (const [otherSymbol, correlation] of Object.entries(correlations)) {
        if (signals[otherSymbol]) {
            const otherSignal = signals[otherSymbol].signal || "neutral";
            const otherConfidence = signals[otherSymbol].confidence || 0;
            
            if (correlation > 0.7 && otherSignal !== "neutral") {
                // Correlación positiva fuerte: confirmar si coincide
                if (otherSignal === signals[symbol]?.signal) {
                    correlationAdjustment += 10 * (otherConfidence / 100);
                } else {
                    correlationAdjustment -= 15; // Conflicto = reducir confianza
                }
            } else if (correlation < -0.7) {
                // Correlación negativa fuerte: confirmar si es opuesto
                const oppositeSignal = signals[symbol]?.signal === "buy" ? "sell" : 
                                      (signals[symbol]?.signal === "sell" ? "buy" : "neutral");
                if (otherSignal === oppositeSignal) {
                    correlationAdjustment += 10 * (otherConfidence / 100);
                }
            }
        }
    }
    
    return correlationAdjustment;
}

// ============================================
// ESTACIONALIDAD
// ============================================
function getSeasonalityFactors(symbol) {
    const now = new Date();
    const dayOfWeek = now.getDay(); // 0=Sun, 1=Mon, ...
    const hourUTC = now.getUTCHours();
    const month = now.getMonth() + 1;
    
    let confidenceAdjustment = 0;
    let notes = [];
    
    // Efecto día de la semana
    if (dayOfWeek === 1) { // Lunes
        confidenceAdjustment -= 5;
        notes.push("Monday effect: lower volatility");
    } else if (dayOfWeek === 5) { // Viernes
        confidenceAdjustment -= 5;
        notes.push("Friday effect: position squaring");
    }
    
    // Session overlap
    if (hourUTC >= 13 && hourUTC <= 16) {
        confidenceAdjustment += 10;
        notes.push("London-NY overlap: high liquidity");
    }
    
    // Asian session
    if (hourUTC >= 0 && hourUTC < 7) {
        confidenceAdjustment -= 10;
        notes.push("Asian session: lower volatility");
    }
    
    // Summer lull
    if (month >= 6 && month <= 8) {
        confidenceAdjustment -= 5;
        notes.push("Summer lull");
    }
    
    // NFP day (first Friday of month)
    if (dayOfWeek === 5 && now.getDate() <= 7) {
        confidenceAdjustment -= 20;
        notes.push("NFP day: HIGH RISK - consider avoiding");
    }
    
    return { confidenceAdjustment, notes: notes.join(", ") };
}

// ============================================
// FUNCIONES TÉCNICAS BÁSICAS
// ============================================
function calculateRSI(candles, period = 14) {
    if (!candles || candles.length < period + 1) return 50;
    try {
        let gains = 0, losses = 0;
        for (let i = candles.length - period; i < candles.length; i++) {
            if (i === 0) continue;
            const change = (candles[i].close || 0) - (candles[i-1].close || 0);
            if (change >= 0) gains += change;
            else losses -= change;
        }
        if (losses === 0) return 100;
        const rs = gains / losses;
        return Number((100 - (100 / (1 + rs))).toFixed(2));
    } catch (error) { return 50; }
}

function determineTrend(candles) {
    if (!candles || candles.length < 20) return "neutral";
    try {
        const closes20 = candles.slice(-20).map(c => c.close || 0);
        const sma20 = closes20.reduce((a, b) => a + b, 0) / 20;
        const closes50 = candles.slice(-50).map(c => c.close || 0);
        const sma50 = closes50.reduce((a, b) => a + b, 0) / 50;
        if (sma20 > sma50 * 1.002) return "bullish";
        if (sma20 < sma50 * 0.998) return "bearish";
        return "neutral";
    } catch (error) { return "neutral"; }
}

function findSupportResistance(candles, currentPrice) {
    if (!candles || candles.length === 0) {
        return { support: currentPrice * 0.995, resistance: currentPrice * 1.005 };
    }
    try {
        const recentCandles = candles.slice(-20);
        const highs = recentCandles.map(c => c.high || c.close || 0);
        const lows = recentCandles.map(c => c.low || c.close || 0);
        let support = Math.min(...lows);
        let resistance = Math.max(...highs);
        if (support === resistance || recentCandles.length === 1) {
            support = currentPrice * 0.995;
            resistance = currentPrice * 1.005;
        }
        return { support, resistance };
    } catch (error) {
        return { support: currentPrice * 0.995, resistance: currentPrice * 1.005 };
    }
}

// ============================================
// PROCESAR ITEMS ENTRANTES
// ============================================
for (const item of allItems) {
    const data = item.json;
    
    if (data.news && Array.isArray(data.news) && data.news.length > 0) {
        globalNews = data.news;
        console.log(`📰 Noticias: ${globalNews.length} items`);
    }
    
    if (data.calendar && Array.isArray(data.calendar) && data.calendar.length > 0) {
        globalCalendar = data.calendar;
        console.log(`📅 Calendario: ${globalCalendar.length} eventos`);
    }
    
    if (data.balance !== undefined && Object.keys(globalAccountData).length === 0) {
        globalAccountData = {
            balance: data.balance, equity: data.equity, profit: data.profit,
            free_margin: data.free_margin, leverage: data.leverage, currency: data.currency
        };
        console.log(`💰 Cuenta: balance ${data.balance}`);
    }
    
    if (Array.isArray(data) && data.length > 0 && data[0]?.symbol !== undefined) {
        globalPositions = data;
    }
    if (data.positions && Array.isArray(data.positions)) {
        globalPositions = data.positions;
    }
    
    if (Array.isArray(data) && data.length > 0 && data[0]?.state === "PLACED") {
        globalPendingOrders = data;
    }
    if (data.pendings && Array.isArray(data.pendings)) {
        globalPendingOrders = data.pendings;
    }
    if (data.id && data.state === "PLACED" && !Array.isArray(data)) {
        globalPendingOrders.push(data);
    }
    
    if (data.rates && data.strength && data.sentiment) {
        currencyStrength = {
            rates: data.rates, strength: data.strength,
            sentiment: data.sentiment, timestamp: data.timestamp
        };
        globalRates = data.rates;
        console.log(`💹 Fortaleza de divisas cargada`);
    }
    
    if (data.symbol && (data.bid !== undefined || data.ask !== undefined)) {
        const sym = data.symbol;
        const price = data.bid || data.ask;
        if (!globalRates) globalRates = {};
        const validation = validatePrice(sym, price, `bid/ask directo`);
        if (validation.valid) {
            globalRates[sym] = validation.price;
        }
    }
}

// ============================================
// ASEGURAR PRECIOS PARA TODOS LOS PARES
// ============================================
if (!globalRates) {
    console.log("⚠️ No se encontraron rates, usando valores por defecto");
    globalRates = { ...DEFAULT_PRICES };
} else {
    for (const [symbol, defaultPrice] of Object.entries(DEFAULT_PRICES)) {
        if (!globalRates[symbol]) {
            globalRates[symbol] = defaultPrice;
        } else {
            const validation = validatePrice(symbol, globalRates[symbol], 'rates');
            if (!validation.valid) globalRates[symbol] = validation.price;
        }
    }
}

console.log("\n💰 PRECIOS REALES CONFIRMADOS:");
for (const [symbol, price] of Object.entries(globalRates)) {
    if (DEFAULT_PRICES[symbol] !== undefined) console.log(`   ${symbol}: ${price}`);
}

// ============================================
// PROCESAR CADA PAR CON INDICADORES v3.0
// ============================================
const symbols = ["EURUSD", "GBPUSD", "USDJPY", "USDCHF"];
const tempSignals = {}; // Para correlaciones

for (const symbol of symbols) {
    console.log(`\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━`);
    console.log(`📊 Procesando par: ${symbol}`);
    
    let currentPrice = globalRates[symbol];
    console.log(`   ✅ Precio: ${currentPrice}`);
    
    // Buscar velas
    let candlesData = [];
    for (const item of allItems) {
        const data = item.json;
        if (data.symbol === symbol && data.candles && Array.isArray(data.candles)) {
            candlesData = data.candles;
            console.log(`   ✅ Velas: ${candlesData.length} candles`);
            break;
        }
    }
    
    // === INDICADORES AVANZADOS v3.0 ===
    const rsi = calculateRSI(candlesData);
    const trend = determineTrend(candlesData);
    const sr = findSupportResistance(candlesData, currentPrice);
    const macd = calculateMACD(candlesData);
    const bollinger = calculateBollingerBands(candlesData);
    const atr = calculateATR(candlesData);
    const volumeAnalysis = analyzeVolume(candlesData, currentPrice, trend);
    const candlePatterns = detectCandlePatterns(candlesData);
    const marketState = classifyMarketState(candlesData, atr, bollinger);
    
    // News scoring
    const relevantCurrencies = symbol === "EURUSD" ? ["EUR", "USD"] :
                               symbol === "GBPUSD" ? ["GBP", "USD"] :
                               symbol === "USDJPY" ? ["USD", "JPY"] : ["USD", "CHF"];
    
    const relevantNews = globalNews.filter(news => news.currency && relevantCurrencies.includes(news.currency));
    const relevantEvents = globalCalendar.filter(event => event.currency && relevantCurrencies.includes(event.currency));
    const newsScore = scoreNews(relevantNews, symbol);
    const calendarScore = scoreCalendar(relevantEvents, symbol);
    
    // Señal temporal para correlaciones
    tempSignals[symbol] = { signal: trend === "bullish" ? "buy" : (trend === "bearish" ? "sell" : "neutral"), confidence: 50 };
    
    // ============================================
    // CALCULAR SCORE COMPUESTO v3.0
    // ============================================
    let score = 50;
    
    // RSI
    if (rsi < 35) score += 20;
    else if (rsi < 45) score += 10;
    else if (rsi > 65) score += 20;
    else if (rsi > 55) score += 10;
    
    // Tendencia
    if (trend === "bullish") score += 15;
    else if (trend === "bearish") score += 15;
    
    // MACD
    if (macd.histogram > 0) score += 5;
    else if (macd.histogram < 0) score += 5;
    if (macd.crossover === "bullish") score += 10;
    else if (macd.crossover === "bearish") score += 10;
    
    // Bollinger
    if (bollinger.percentB < 0.2) score += 8;
    else if (bollinger.percentB > 0.8) score += 8;
    
    // Volumen
    score += volumeAnalysis.confidence_adjustment;
    
    // Patrones de velas
    if (candlePatterns.pattern !== "none") score += candlePatterns.strength * 0.1;
    
    // News
    score += newsScore.score * 0.15;
    
    // Calendar risk
    if (calendarScore.act_wait === "wait") score -= 20;
    
    // Ajustar por fortaleza de divisas si no hay velas
    let finalRsi = rsi;
    let finalTrend = trend;
    
    if (candlesData.length === 0 && currencyStrength) {
        const baseCurrency = symbol.substring(0, 3);
        const quoteCurrency = symbol.substring(3, 6);
        const baseStrength = currencyStrength.strength[baseCurrency] || "neutral";
        const quoteStrength = currencyStrength.strength[quoteCurrency] || "neutral";
        
        if (baseStrength === "bullish" && quoteStrength === "bearish") {
            finalTrend = "bullish"; finalRsi = 65;
        } else if (baseStrength === "bearish" && quoteStrength === "bullish") {
            finalTrend = "bearish"; finalRsi = 35;
        }
    }
    
    let finalScore = Math.max(0, Math.min(100, Math.round(score)));
    
    // Filtrar eventos de alto impacto
    const highImpactEvents = relevantEvents.filter(e => e.impact === "high");
    const symbolPositions = globalPositions.filter(p => p.symbol === symbol);
    const symbolPendingOrders = globalPendingOrders.filter(p => p.symbol === symbol);
    
    // Estacionalidad
    const seasonality = getSeasonalityFactors(symbol);
    finalScore = Math.max(0, Math.min(100, finalScore + seasonality.confidenceAdjustment));
    
    // Almacenar análisis v3.0
    pairsAnalysis[symbol] = {
        symbol: symbol,
        score: finalScore,
        technical: {
            current_price: currentPrice, rsi: finalRsi, trend: finalTrend,
            support: sr.support, resistance: sr.resistance,
            macd: macd, bollinger: bollinger, atr: atr,
            volume_analysis: volumeAnalysis, candle_patterns: candlePatterns
        },
        market_state: marketState,
        fundamental: {
            news_count: relevantNews.length,
            news_score: newsScore,
            calendar_score: calendarScore,
            high_impact_events: highImpactEvents.length,
            events: highImpactEvents.map(e => e.event).slice(0, 3),
            relevant_news: relevantNews.slice(0, 5),
            calendar_events: relevantEvents.slice(0, 5)
        },
        seasonality: seasonality,
        positions: {
            count: symbolPositions.length,
            exposure: symbolPositions.reduce((sum, p) => sum + Math.abs(p.volume || 0), 0),
            details: symbolPositions
        },
        pending_orders: {
            count: symbolPendingOrders.length,
            exposure: symbolPendingOrders.reduce((sum, p) => sum + Math.abs(p.volume || 0), 0),
            details: symbolPendingOrders
        }
    };
    
    console.log(`   ✅ Score: ${finalScore} | RSI: ${finalRsi} | Trend: ${finalTrend} | MACD: ${macd.crossover} | Vol: ${volumeAnalysis.volume_signal} | News: ${newsScore.bias}`);
    
    if (finalScore > bestScore) {
        bestScore = finalScore;
        bestPair = symbol;
        console.log(`   🏆 Nuevo mejor par: ${symbol} con score ${bestScore}`);
    }
}

// ============================================
// AJUSTAR POR CORRELACIONES
// ============================================
if (bestPair && pairsAnalysis[bestPair]) {
    const corrAdjustment = adjustByCorrelations(bestPair, tempSignals);
    pairsAnalysis[bestPair].score = Math.max(0, Math.min(100, pairsAnalysis[bestPair].score + corrAdjustment));
    bestScore = pairsAnalysis[bestPair].score;
    if (corrAdjustment !== 0) {
        console.log(`🔗 Ajuste por correlación para ${bestPair}: ${corrAdjustment > 0 ? '+' : ''}${corrAdjustment}`);
    }
}

// Fallback
if (!bestPair) {
    bestPair = "EURUSD";
    bestScore = 50;
    console.log("⚠️ No se encontraron pares válidos, usando EURUSD");
}

// Asegurar bestAnalysis
const bestAnalysis = pairsAnalysis[bestPair];
if (!bestAnalysis || !bestAnalysis.technical?.current_price) {
    pairsAnalysis[bestPair] = {
        symbol: bestPair, score: bestScore,
        technical: { current_price: globalRates[bestPair] || DEFAULT_PRICES[bestPair], rsi: 50, trend: "neutral", support: 0, resistance: 0, macd: {macd:0,signal:0,histogram:0,crossover:"none"}, bollinger: {upper:0,middle:0,lower:0,percentB:0.5,squeeze:false}, atr: 0, volume_analysis: {volume_signal:"neutral",confidence_adjustment:0}, candle_patterns: {pattern:"none",strength:0} },
        market_state: { trend:"unknown", volatility:"unknown", session:"unknown", recommended_strategy:"range_trading" },
        fundamental: { news_count: 0, news_score: {score:0,bias:"neutral",confidence:0}, calendar_score: {risk_level:"low",act_wait:"act",score:0}, high_impact_events: 0, events: [], relevant_news: [], calendar_events: [] },
        seasonality: { confidenceAdjustment: 0, notes: "" },
        positions: { count: 0, exposure: 0, details: [] },
        pending_orders: { count: 0, exposure: 0, details: [] }
    };
}

// ============================================
// CÁLCULO DE RIESGO
// ============================================
const accountBalance = globalAccountData.balance || 100000;
const totalPositionsExposure = globalPositions.reduce((sum, p) => sum + Math.abs(p.volume || 0), 0);
const totalPendingExposure = globalPendingOrders.reduce((sum, p) => sum + Math.abs(p.volume || 0), 0);

const riskAmount = accountBalance * 0.02;
const stopLossPips = 50;
const pipValue = 10;
let recommendedVolume = riskAmount / (stopLossPips * pipValue);
recommendedVolume = Math.min(recommendedVolume, 0.05);
recommendedVolume = Math.max(recommendedVolume, 0.01);
recommendedVolume = Math.round(recommendedVolume * 100) / 100;

const finalAnalysis = pairsAnalysis[bestPair];

console.log("\n========================================");
console.log("🏆 MEJOR PAR:", bestPair);
console.log(`   Score: ${bestScore}`);
console.log(`   Precio: ${finalAnalysis.technical.current_price}`);
console.log(`   RSI: ${finalAnalysis.technical.rsi}`);
console.log(`   Tendencia: ${finalAnalysis.technical.trend}`);
console.log(`   MACD: ${finalAnalysis.technical.macd.crossover}`);
console.log(`   Volumen: ${finalAnalysis.technical.volume_analysis.volume_signal}`);
console.log(`   Mercado: ${finalAnalysis.market_state.trend} / ${finalAnalysis.market_state.volatility}`);
console.log(`   Estrategia: ${finalAnalysis.market_state.recommended_strategy}`);
console.log("========================================");

return [{
    json: {
        best_pair: bestPair,
        best_score: bestScore,
        all_pairs: pairsAnalysis,
        currency_strength: currencyStrength || {
            rates: globalRates,
            strength: { EUR: "neutral", USD: "neutral", GBP: "neutral", JPY: "neutral", CHF: "neutral" },
            sentiment: { score: 0, overall: "neutral", market_mood: "calm" }
        },
        technical: finalAnalysis.technical,
        fundamental: finalAnalysis.fundamental,
        market_state: finalAnalysis.market_state,
        seasonality: finalAnalysis.seasonality,
        positions: { count: globalPositions.length, exposure: totalPositionsExposure, details: globalPositions },
        pending_orders: { count: globalPendingOrders.length, exposure: totalPendingExposure, details: globalPendingOrders },
        risk: { account_balance: accountBalance, risk_amount: riskAmount, recommended_volume: recommendedVolume, current_exposure: totalPositionsExposure, pending_exposure: totalPendingExposure },
        global_news: globalNews.slice(0, 5),
        global_calendar: globalCalendar.slice(0, 5),
        timestamp: new Date().toISOString()
    }
}];
```

---

## 2. NODO: Memory Manager (v3.0)

**Archivo**: `multi-agente-profesional-CORRECTED.json`
**Nodo**: `Memory Manager`

**Reemplazar TODO el código del nodo con el siguiente:**

```javascript
// ============================================
// MEMORY MANAGER - Versión v3.0 MEJORADA
// ============================================
// MEJORAS v3.0:
// 1. Tracking de drawdown (global, diario, semanal)
// 2. Performance metrics por agente
// 3. Circuit breaker y cooldown management
// 4. Sharpe ratio y Calmar ratio
// 5. Auto-optimización de parámetros
// ============================================

const marketData = $input.all()[0].json;

// Inicializar estructura de memoria si no existe
if (!global.tradingMemory) {
    global.tradingMemory = {
        last_analysis: {},
        recent_decisions: [],
        trades: [],
        agent_weights: {
            technical: 0.35, fundamental: 0.25, sentiment: 0.25, statistical: 0.15
        },
        performance: {
            technical: { wins: 0, losses: 0, total: 0, winrate: 0, last_confidence: [] },
            fundamental: { wins: 0, losses: 0, total: 0, winrate: 0, last_confidence: [] },
            sentiment: { wins: 0, losses: 0, total: 0, winrate: 0, last_confidence: [] },
            statistical: { wins: 0, losses: 0, total: 0, winrate: 0, last_confidence: [] }
        },
        global_metrics: {
            total_trades: 0, winning_trades: 0, losing_trades: 0,
            total_pips: 0, avg_confidence: 0, sharpe_ratio: 0,
            calmar_ratio: 0, max_drawdown: 0, profit_factor: 0,
            avg_rr_ratio: 0, total_profit_usd: 0
        },
        drawdown: {
            peak_equity: 0, current_drawdown: 0, max_drawdown: 0,
            daily_pnl: 0, weekly_pnl: 0, monthly_pnl: 0,
            last_reset_daily: new Date().toDateString(),
            last_reset_weekly: new Date().toDateString(),
            last_reset_monthly: new Date().toDateString()
        },
        circuit_breaker: {
            is_halted: false, halt_timestamp: null, halt_reason: "",
            daily_dd_threshold: 0.05, weekly_dd_threshold: 0.10,
            monthly_dd_threshold: 0.15, consecutive_loss_limit: 5
        },
        preferences: {
            risk_tolerance: 0.02, max_positions: 3, max_volume: 0.05,
            min_confidence: 45, max_drawdown: 0.15
        },
        last_update: new Date().toISOString(),
        weight_history: []
    };
}

// ============================================
// ACTUALIZAR DRAWDOWN
// ============================================
function updateDrawdown(trades) {
    const dd = global.tradingMemory.drawdown;
    const now = new Date();
    
    // Reset diarios/semanales/mensuales
    if (dd.last_reset_daily !== now.toDateString()) {
        dd.daily_pnl = 0;
        dd.last_reset_daily = now.toDateString();
    }
    
    // Calcular P&L de trades recientes
    if (trades.length > 0) {
        const lastTrade = trades[trades.length - 1];
        if (lastTrade.order?.status === "executed") {
            const pips = lastTrade.pips || 0;
            const volume = lastTrade.volume || 0.01;
            const profitUSD = pips * volume * 10;
            
            dd.total_pnl = (dd.total_pnl || 0) + profitUSD;
            dd.daily_pnl = (dd.daily_pnl || 0) + profitUSD;
            dd.weekly_pnl = (dd.weekly_pnl || 0) + profitUSD;
            dd.monthly_pnl = (dd.monthly_pnl || 0) + profitUSD;
            
            // Actualizar peak
            const currentEquity = 100000 + dd.total_pnl;
            if (currentEquity > dd.peak_equity) dd.peak_equity = currentEquity;
            
            // Calcular drawdown actual
            if (dd.peak_equity > 0) {
                dd.current_drawdown = (dd.peak_equity - currentEquity) / dd.peak_equity;
                if (dd.current_drawdown > dd.max_drawdown) {
                    dd.max_drawdown = dd.current_drawdown;
                }
            }
        }
    }
    
    return dd;
}

// ============================================
// ACTUALIZAR MÉTRICAS GLOBALES
// ============================================
function updateGlobalMetrics(trades) {
    const metrics = global.tradingMemory.global_metrics;
    
    const executedTrades = trades.filter(t => t.order?.status === "executed" && t.result);
    if (executedTrades.length === 0) return;
    
    const wins = executedTrades.filter(t => t.result === "win").length;
    const losses = executedTrades.filter(t => t.result === "loss").length;
    const total = wins + losses;
    
    metrics.total_trades = total;
    metrics.winning_trades = wins;
    metrics.losing_trades = losses;
    
    // Winrate
    const winrate = total > 0 ? wins / total : 0;
    metrics.winrate = winrate;
    
    // Total pips
    metrics.total_pips = executedTrades.reduce((s, t) => s + (t.pips || 0), 0);
    
    // Profit factor (ganancia bruta / pérdida bruta)
    const grossProfit = executedTrades.filter(t => t.pips > 0).reduce((s, t) => s + Math.abs(t.pips || 0), 0);
    const grossLoss = executedTrades.filter(t => t.pips < 0).reduce((s, t) => s + Math.abs(t.pips || 0), 0);
    metrics.profit_factor = grossLoss > 0 ? grossProfit / grossLoss : grossProfit > 0 ? 999 : 0;
    
    // Avg R/R ratio
    const rrRatios = executedTrades.map(t => t.rr_ratio || 0).filter(r => r > 0);
    metrics.avg_rr_ratio = rrRatios.length > 0 ? 
        rrRatios.reduce((s, r) => s + r, 0) / rrRatios.length : 0;
    
    // Sharpe ratio (simplificado - rolling 20 trades)
    const recentTrades = executedTrades.slice(-20);
    if (recentTrades.length >= 5) {
        const returns = recentTrades.map(t => t.pips || 0);
        const avgReturn = returns.reduce((s, r) => s + r, 0) / returns.length;
        const stdDev = Math.sqrt(returns.reduce((s, r) => s + Math.pow(r - avgReturn, 2), 0) / returns.length);
        metrics.sharpe_ratio = stdDev > 0 ? (avgReturn / stdDev) : 0;
    }
    
    // Calmar ratio (retorno / max drawdown)
    const dd = global.tradingMemory.drawdown;
    metrics.calmar_ratio = dd.max_drawdown > 0 ? (metrics.total_pips / (dd.max_drawdown * 100000)) : 0;
    
    // Avg confidence
    const recentDecisions = global.tradingMemory.recent_decisions.slice(-10);
    if (recentDecisions.length > 0) {
        metrics.avg_confidence = recentDecisions.reduce((s, d) => s + (d.confianza || 0), 0) / recentDecisions.length;
    }
    
    // Total profit USD
    metrics.total_profit_usd = dd.total_pnl || 0;
}

// ============================================
// CIRCUIT BREAKER CHECK
// ============================================
function checkCircuitBreaker() {
    const cb = global.tradingMemory.circuit_breaker;
    const dd = global.tradingMemory.drawdown;
    const now = Date.now();
    
    // Verificar si ya está haltado
    if (cb.is_halted && cb.halt_timestamp) {
        const elapsed = now - cb.halt_timestamp;
        const cooldownMs = 4 * 60 * 60 * 1000; // 4 horas
        if (elapsed >= cooldownMs) {
            cb.is_halted = false;
            cb.halt_timestamp = null;
            cb.halt_reason = "";
            console.log("🔄 Circuit breaker: Cooldown terminado, trading reanudado");
        } else {
            const remainingMin = Math.ceil((cooldownMs - elapsed) / (60 * 1000));
            console.log(`🛑 Circuit breaker activo: ${remainingMin} minutos restantes`);
            return { halted: true, reason: cb.halt_reason, remaining_minutes: remainingMin };
        }
    }
    
    // Verificar drawdown diario
    const balance = global.tradingMemory.drawdown.peak_equity || 100000;
    const dailyDD = Math.abs(dd.daily_pnl || 0) / balance;
    const weeklyDD = Math.abs(dd.weekly_pnl || 0) / balance;
    const monthlyDD = Math.abs(dd.monthly_pnl || 0) / balance;
    
    // Pérdidas consecutivas
    const recentTrades = global.tradingMemory.trades.slice(-cb.consecutive_loss_limit);
    const consecutiveLosses = recentTrades.every(t => t.result === "loss") && recentTrades.length >= cb.consecutive_loss_limit ?
        recentTrades.length : 0;
    
    if (dailyDD >= cb.daily_dd_threshold) {
        cb.is_halted = true;
        cb.halt_timestamp = now;
        cb.halt_reason = `Daily DD ${(dailyDD * 100).toFixed(1)}% > ${(cb.daily_dd_threshold * 100).toFixed(1)}%`;
        console.log(`🛑 HALT: ${cb.halt_reason}`);
        return { halted: true, reason: cb.halt_reason };
    }
    
    if (weeklyDD >= cb.weekly_dd_threshold) {
        cb.is_halted = true;
        cb.halt_timestamp = now;
        cb.halt_reason = `Weekly DD ${(weeklyDD * 100).toFixed(1)}% > ${(cb.weekly_dd_threshold * 100).toFixed(1)}%`;
        console.log(`🛑 HALT: ${cb.halt_reason}`);
        return { halted: true, reason: cb.halt_reason };
    }
    
    if (monthlyDD >= cb.monthly_dd_threshold) {
        cb.is_halted = true;
        cb.halt_timestamp = now;
        cb.halt_reason = `Monthly DD ${(monthlyDD * 100).toFixed(1)}% > ${(cb.monthly_dd_threshold * 100).toFixed(1)}%`;
        console.log(`🛑 HALT: ${cb.halt_reason}`);
        return { halted: true, reason: cb.halt_reason };
    }
    
    if (consecutiveLosses >= cb.consecutive_loss_limit) {
        cb.is_halted = true;
        cb.halt_timestamp = now;
        cb.halt_reason = `${consecutiveLosses} pérdidas consecutivas`;
        console.log(`🛑 HALT: ${cb.halt_reason}`);
        return { halted: true, reason: cb.halt_reason };
    }
    
    return { halted: false };
}

// ============================================
// AUTO-OPTIMIZACIÓN DE PESOS
// ============================================
function optimizeAgentWeights() {
    const perf = global.tradingMemory.performance;
    const weights = global.tradingMemory.agent_weights;
    
    // Solo ajustar si hay suficientes datos
    for (const [agent, data] of Object.entries(perf)) {
        if (data.total < 10) continue;
        
        const winrate = data.winrate || 0;
        const baseWeight = { technical: 0.35, fundamental: 0.25, sentiment: 0.25, statistical: 0.15 }[agent];
        
        // Bonus por winrate > 55%
        const alpha = 0.8;
        const adjustedWeight = baseWeight * (1 + alpha * (winrate - 0.55));
        weights[agent] = Math.max(0.05, Math.min(0.50, adjustedWeight));
    }
    
    // Normalizar
    const totalWeight = Object.values(weights).reduce((s, w) => s + w, 0);
    for (const key of Object.keys(weights)) {
        weights[key] /= totalWeight;
    }
    
    global.tradingMemory.weight_history.push({
        timestamp: new Date().toISOString(),
        weights: { ...weights }
    });
}

// ============================================
// EJECUTAR UPDATES
// ============================================
const trades = global.tradingMemory.trades || [];

updateDrawdown(trades);
updateGlobalMetrics(trades);
const circuitStatus = checkCircuitBreaker();
optimizeAgentWeights();

// Ajustar umbral de confianza dinámicamente
const metrics = global.tradingMemory.global_metrics;
const winrate = metrics.total_trades > 0 ? (metrics.winning_trades / metrics.total_trades) * 100 : 50;

if (winrate > 60 && global.tradingMemory.preferences.min_confidence > 40) {
    global.tradingMemory.preferences.min_confidence = Math.max(40, global.tradingMemory.preferences.min_confidence - 2);
    console.log(`📈 Winrate alto (${winrate.toFixed(1)}%), umbral bajado a ${global.tradingMemory.preferences.min_confidence}%`);
} else if (winrate < 45 && winrate > 0 && global.tradingMemory.preferences.min_confidence < 55) {
    global.tradingMemory.preferences.min_confidence = Math.min(55, global.tradingMemory.preferences.min_confidence + 2);
    console.log(`📉 Winrate bajo (${winrate.toFixed(1)}%), umbral subido a ${global.tradingMemory.preferences.min_confidence}%`);
}

global.tradingMemory.last_update = new Date().toISOString();

const context = {
    timestamp: new Date().toISOString(),
    market_data: marketData,
    memory: global.tradingMemory,
    circuit_breaker: circuitStatus,
    recent_decisions: global.tradingMemory.recent_decisions.slice(-10),
    recent_trades: global.tradingMemory.trades.slice(-5),
    agent_performance: global.tradingMemory.performance,
    agent_weights: global.tradingMemory.agent_weights,
    global_metrics: global.tradingMemory.global_metrics,
    drawdown: global.tradingMemory.drawdown,
    preferences: global.tradingMemory.preferences
};

console.log("📚 Memory Manager v3.0:");
console.log(`   - Mejor par: ${marketData.best_pair} | Score: ${marketData.best_score}`);
console.log(`   - Trades: ${metrics.total_trades} | Winrate: ${winrate.toFixed(1)}% | PF: ${metrics.profit_factor.toFixed(2)}`);
console.log(`   - Sharpe: ${metrics.sharpe_ratio.toFixed(2)} | Max DD: ${(global.tradingMemory.drawdown.max_drawdown * 100).toFixed(1)}%`);
console.log(`   - Circuit Breaker: ${circuitStatus.halted ? 'HALTADO' : 'OK'}`);

return [{ json: context }];
```

---

## 3. NODO: Risk Manager (NUEVO v3.0)

**Archivo**: `multi-agente-profesional-CORRECTED.json`

**Añadir un NUEVO nodo de código ENTRE `Check Action` y `Preparar Orden`**. Llámalo `Risk Manager v3.0`.

```javascript
// ============================================
// RISK MANAGER v3.0 - GESTIÓN DE RIESGO ADAPTATIVA
// ============================================
// NUEVO NODO: Insertar entre "Check Action" y "Preparar Orden"
// Calcula riesgo adaptativo basado en confianza, drawdown y racha
// ============================================

const decision = $input.all()[0].json;
const memory = $node["Memory Manager"]?.json?.memory || global.tradingMemory || {};

// ============================================
// CONFIGURACIÓN DE RIESGO
// ============================================
const RISK_CONFIG = {
    base_risk_percent: 0.02,
    max_risk_percent: 0.04,
    min_risk_percent: 0.005,
    confidence_threshold: 50,
    drawdown_levels: {
        level1: { threshold: 0.05, reduction: 0.5, action: "reduce" },
        level2: { threshold: 0.10, reduction: 0.25, action: "reduce" },
        level3: { threshold: 0.15, reduction: 0, action: "halt" }
    },
    daily_loss_limit: 0.05,
    weekly_loss_limit: 0.10,
    cooldown_after_halt: 4 * 60 * 60 * 1000
};

// ============================================
// CALCULAR DRAWDOWN
// ============================================
function calculateDrawdown(trades) {
    if (!trades || trades.length === 0) return { current: 0, max: 0 };
    
    let peak = 0;
    let maxDrawdown = 0;
    let currentEquity = 100000;
    let currentDrawdown = 0;
    
    for (const trade of trades) {
        const pips = trade.pips || 0;
        const volume = trade.volume || 0.01;
        const profit = pips * volume * 10;
        currentEquity += profit;
        if (currentEquity > peak) peak = currentEquity;
        const dd = (peak - currentEquity) / peak;
        if (dd > maxDrawdown) maxDrawdown = dd;
        currentDrawdown = dd;
    }
    
    return { current: currentDrawdown, max: maxDrawdown };
}

function calculatePeriodDrawdown(trades, hours) {
    const cutoff = Date.now() - (hours * 60 * 60 * 1000);
    const recentTrades = trades.filter(t => new Date(t.timestamp).getTime() > cutoff);
    
    let peak = 0;
    let maxDD = 0;
    let runningTotal = 0;
    
    for (const trade of recentTrades) {
        const pips = trade.pips || 0;
        const volume = trade.volume || 0.01;
        runningTotal += pips * volume * 10;
        if (runningTotal > peak) peak = runningTotal;
        const dd = (peak - runningTotal) / (peak || 100000);
        if (dd > maxDD) maxDD = dd;
    }
    
    return maxDD;
}

// ============================================
// CÁLCULO DE RIESGO ADAPTATIVO
// ============================================
const trades = memory.trades || [];
const confidence = decision.confianza || 50;
const accountBalance = decision.market_data?.risk?.account_balance || 100000;

const overallDD = calculateDrawdown(trades);
const dailyDD = calculatePeriodDrawdown(trades, 24);
const weeklyDD = calculatePeriodDrawdown(trades, 168);

console.log(`📊 DD Actual: ${(overallDD.current * 100).toFixed(2)}% | Máx: ${(overallDD.max * 100).toFixed(2)}% | Diario: ${(dailyDD * 100).toFixed(2)}%`);

let riskMultiplier = 1.0;
let shouldHalt = false;
let haltReason = "";

// Verificar drawdown global
for (const [level, config] of Object.entries(RISK_CONFIG.drawdown_levels)) {
    if (overallDD.current >= config.threshold) {
        if (config.action === "halt") {
            shouldHalt = true;
            haltReason = `Drawdown global ${level}: ${(overallDD.current * 100).toFixed(1)}%`;
        } else {
            riskMultiplier *= config.reduction;
        }
    }
}

if (dailyDD >= RISK_CONFIG.daily_loss_limit) {
    shouldHalt = true;
    haltReason = `Drawdown diario: ${(dailyDD * 100).toFixed(1)}% > ${(RISK_CONFIG.daily_loss_limit * 100).toFixed(1)}%`;
}

if (weeklyDD >= RISK_CONFIG.weekly_loss_limit) {
    shouldHalt = true;
    haltReason = `Drawdown semanal: ${(weeklyDD * 100).toFixed(1)}% > ${(RISK_CONFIG.weekly_loss_limit * 100).toFixed(1)}%`;
}

// Verificar circuit breaker de memory
const cb = memory.circuit_breaker;
if (cb?.is_halted) {
    shouldHalt = true;
    haltReason = cb.halt_reason || "Circuit breaker activo";
}

// Ajustar por confianza
if (!shouldHalt) {
    const confidenceRatio = confidence / RISK_CONFIG.confidence_threshold;
    const confMultiplier = Math.max(0.5, Math.min(2.0, confidenceRatio));
    riskMultiplier *= confMultiplier;
    console.log(`📈 Confianza (${confidence}%): multiplicador ${(confMultiplier * 100).toFixed(0)}%`);
}

// Ajustar por racha
const recentTrades = trades.slice(-5);
const recentWins = recentTrades.filter(t => t.result === "win").length;
const recentLosses = recentTrades.filter(t => t.result === "loss").length;

if (recentLosses >= 3) {
    riskMultiplier *= 0.5;
    console.log(`⚠️ Racha ${recentLosses} pérdidas: riesgo -50%`);
} else if (recentWins >= 3) {
    riskMultiplier = Math.min(riskMultiplier * 1.2, RISK_CONFIG.max_risk_percent / RISK_CONFIG.base_risk_percent);
    console.log(`📈 Racha ${recentWins} ganancias: riesgo +20%`);
}

// Riesgo final
let finalRiskPercent = RISK_CONFIG.base_risk_percent * riskMultiplier;
finalRiskPercent = Math.max(RISK_CONFIG.min_risk_percent, Math.min(RISK_CONFIG.max_risk_percent, finalRiskPercent));

console.log(`🎯 Riesgo Final: ${(finalRiskPercent * 100).toFixed(2)}% (multiplier: ${riskMultiplier.toFixed(2)}x)`);

// ============================================
// DECISIÓN DE HALT
// ============================================
if (shouldHalt) {
    console.log(`🛑 HALT: ${haltReason}`);
    return [{
        json: {
            ...decision, should_execute: false, halt: true,
            halt_reason: haltReason, risk_percent: 0, volume: 0,
            drawdown: { current: overallDD.current, max: overallDD.max, daily: dailyDD, weekly: weeklyDD }
        }
    }];
}

// ============================================
// CALCULAR VOLUMEN ADAPTATIVO
// ============================================
const symbol = decision.symbol || decision.market_data?.best_pair || "EURUSD";
const entryPrice = decision.entrada?.precio || decision.market_data?.technical?.current_price || 0;
const stopPrice = decision.stop_loss?.precio || 0;

const riskInUSD = accountBalance * finalRiskPercent;
const stopPips = Math.abs(entryPrice - stopPrice);
const pipValue = symbol.includes("JPY") ? 100 : 10000;
const slInPips = stopPips * pipValue;

let calculatedVolume = slInPips > 0 ? riskInUSD / (slInPips * 10) : 0.01;
calculatedVolume = Math.max(0.01, Math.min(0.05, calculatedVolume));
calculatedVolume = Math.round(calculatedVolume * 100) / 100;

console.log(`💰 Riesgo: $${riskInUSD.toFixed(2)} | SL: ${slInPips.toFixed(1)} pips | Volumen: ${calculatedVolume}`);

return [{
    json: {
        ...decision, should_execute: true, halt: false,
        risk_percent: finalRiskPercent, risk_multiplier: riskMultiplier,
        volume: calculatedVolume, risk_in_usd: riskInUSD, stop_pips: slInPips,
        drawdown: { current: overallDD.current, max: overallDD.max, daily: dailyDD, weekly: weeklyDD },
        recent_performance: { wins: recentWins, losses: recentLosses }
    }
}];
```

---

## 4. NODO: Agente Estratega → VOTATION ENGINE v3.0

**Archivo**: `multi-agente-profesional-CORRECTED.json`
**Nodo**: `Agente Estratega` (o el nodo que procesa las respuestas de los agentes)

**Reemplazar TODO el código del nodo con el siguiente:**

```javascript
// ============================================
// VOTATION ENGINE v3.0 - SISTEMA DE VOTACIÓN PONDERADA
// ============================================
// Reemplaza "Agente Estratega" en multi-agente-profesional
// Recibe decisiones de agentes y vota ponderadamente
// ============================================

const inputData = $input.all()[0].json;

console.log("========================================");
console.log("🗳️ VOTATION ENGINE v3.0");
console.log("========================================");

const VOTATION_CONFIG = {
    min_consensus_agents: 2,
    veto_confidence_threshold: 25,
    min_weighted_score: 40,
    dynamic_weights: true
};

function normalizeSignal(signal) {
    if (!signal) return "neutral";
    const s = signal.toLowerCase().trim();
    if (["buy", "long", "comprar", "bullish"].includes(s)) return "buy";
    if (["sell", "short", "vender", "bearish"].includes(s)) return "sell";
    return "neutral";
}

function getAgentWeight(agentName) {
    const memory = global.tradingMemory || $node["Memory Manager"]?.json?.memory || {};
    const perf = memory.performance || {};
    const agentPerf = perf[agentName] || {};
    const baseWeights = { technical: 0.35, fundamental: 0.25, sentiment: 0.25, statistical: 0.15 };
    
    if (!agentPerf.wins && !agentPerf.losses) return baseWeights[agentName] || 0.25;
    
    const total = agentPerf.wins + agentPerf.losses;
    const winrate = total > 0 ? agentPerf.wins / total : 0.5;
    const alpha = 0.8;
    return Math.max(0.05, Math.min(0.50, baseWeights[agentName] * (1 + alpha * (winrate - 0.5))));
}

function extractAgentVotes(data) {
    const votes = [];
    
    // Formato Jetson (agents_responses)
    if (data.agents_responses) {
        const agents = data.agents_responses;
        
        if (agents.technical) {
            votes.push({
                agent_name: "technical",
                direction: normalizeSignal(agents.technical.signal || agents.technical.decision),
                confidence: parseInt(agents.technical.confidence || agents.technical.confianza || 50),
                weight: getAgentWeight("technical"),
                reasoning: agents.technical.analysis || ""
            });
        }
        if (agents.fundamental) {
            const fund = agents.fundamental;
            const dir = fund.recommendation === "act" ?
                (fund.market_bias?.includes("bullish") ? "buy" : fund.market_bias?.includes("bearish") ? "sell" : "neutral") : "neutral";
            votes.push({
                agent_name: "fundamental", direction: normalizeSignal(dir),
                confidence: parseInt(fund.confidence || 50), weight: getAgentWeight("fundamental"),
                reasoning: fund.analysis || ""
            });
        }
        if (agents.sentiment) {
            votes.push({
                agent_name: "sentiment", direction: normalizeSignal(agents.sentiment.sentiment || agents.sentiment.signal),
                confidence: parseInt(agents.sentiment.confidence || 50), weight: getAgentWeight("sentiment"),
                reasoning: agents.sentiment.analysis || ""
            });
        }
        if (agents.statistical) {
            votes.push({
                agent_name: "statistical", direction: normalizeSignal(agents.statistical.signal || agents.statistical.decision),
                confidence: parseInt(agents.statistical.confidence || agents.statistical.probability || 50),
                weight: getAgentWeight("statistical"), reasoning: agents.statistical.analysis || ""
            });
        }
    }
    
    // Formato OpenRouter (_extracted)
    if (data._extracted && votes.length === 0) {
        const ext = data._extracted;
        votes.push({
            agent_name: "unified_ai", direction: normalizeSignal(ext.decision),
            confidence: parseInt(ext.confidence || 50), weight: 0.70,
            reasoning: ext.analysis || ""
        });
        votes.push({
            agent_name: "statistical", direction: normalizeSignal(ext.decision),
            confidence: 50, weight: 0.30, reasoning: "Historical baseline"
        });
    }
    
    // Fallback: si hay un solo decision directo
    if (votes.length === 0 && data.decision) {
        votes.push({
            agent_name: "direct", direction: normalizeSignal(data.decision),
            confidence: parseInt(data.confianza || 50), weight: 1.0,
            reasoning: data.razonamiento || ""
        });
    }
    
    return votes;
}

function executeVotation(votes) {
    console.log("\n🗳️ PROCESO DE VOTACIÓN:");
    for (const v of votes) {
        console.log(`   ${v.agent_name}: ${v.direction.toUpperCase()} (${v.confidence}%, peso: ${v.weight.toFixed(2)})`);
    }
    
    // Verificar vetos
    const vetoes = votes.filter(v => v.confidence < VOTATION_CONFIG.veto_confidence_threshold);
    if (vetoes.length >= 2) {
        console.log(`🚫 VETO: ${vetoes.length} agentes con confianza < ${VOTATION_CONFIG.veto_confidence_threshold}%`);
        return { decision: "hold", confianza: 0, razonamiento: `Veto: ${vetoes.length} agentes baja confianza`, should_execute: false };
    }
    
    // Contar votos
    const buyVotes = votes.filter(v => v.direction === "buy");
    const sellVotes = votes.filter(v => v.direction === "sell");
    
    const buyScore = buyVotes.reduce((sum, v) => sum + (v.confidence * v.weight), 0);
    const sellScore = sellVotes.reduce((sum, v) => sum + (v.confidence * v.weight), 0);
    const totalWeight = votes.reduce((sum, v) => sum + v.weight, 0);
    
    console.log(`📊 BUY: ${buyScore.toFixed(2)} | SELL: ${sellScore.toFixed(2)} | Total peso: ${totalWeight.toFixed(2)}`);
    
    // Verificar consenso mínimo
    const actingAgents = buyVotes.length + sellVotes.length;
    if (actingAgents < VOTATION_CONFIG.min_consensus_agents && totalWeight > 0) {
        // Para agente unificado, usar directamente
        if (votes.length === 1 && votes[0].agent_name === "unified_ai") {
            // Unificado: pasar directamente
        } else {
            console.log(`⏸️ Consenso insuficiente: ${actingAgents} agentes`);
        }
    }
    
    // Decisión final
    let finalDecision = "hold";
    let finalConfidence = 0;
    
    if (buyScore > sellScore) {
        finalDecision = "buy";
        finalConfidence = totalWeight > 0 ? Math.round((buyScore / totalWeight) * 100) : 50;
    } else if (sellScore > buyScore) {
        finalDecision = "sell";
        finalConfidence = totalWeight > 0 ? Math.round((sellScore / totalWeight) * 100) : 50;
    }
    
    // Score mínimo
    if (finalConfidence < VOTATION_CONFIG.min_weighted_score) {
        console.log(`⏸️ Score ${finalConfidence}% < mínimo ${VOTATION_CONFIG.min_weighted_score}%`);
        return { decision: "hold", confianza: finalConfidence, razonamiento: `Score ${finalConfidence}% insuficiente`, should_execute: false };
    }
    
    // Ajustar confianza por consenso
    if (buyVotes.length + sellVotes.length >= 2) {
        finalConfidence = Math.min(100, finalConfidence + 5); // Bonus por consenso
    }
    
    const result = {
        decision: finalDecision,
        confianza: finalConfidence,
        razonamiento: `${finalDecision.toUpperCase()} con ${finalConfidence}% confianza. BUY: ${buyVotes.length}, SELL: ${sellVotes.length}`,
        votation_details: { buy_score: Math.round(buyScore), sell_score: Math.round(sellScore), acting_agents: actingAgents },
        votes: votes.map(v => ({ agent: v.agent_name, direction: v.direction, confidence: v.confidence, weight: v.weight })),
        should_execute: finalDecision !== "hold"
    };
    
    console.log(`✅ DECISIÓN: ${result.decision.toUpperCase()} (${result.confianza}%)`);
    return result;
}

const votes = extractAgentVotes(inputData);
if (votes.length === 0) {
    console.log("⚠️ No hay votos, usando hold");
    return [{ json: { decision: "hold", confianza: 0, razonamiento: "No agent votes", should_execute: false } }];
}

const result = executeVotation(votes);
return [{ json: result }];
```

---

## 5. NODO: Preparar Orden (v3.0 - ATR-based SL/TP)

**Archivo**: `multi-agente-profesional-CORRECTED.json`
**Nodo**: `Preparar Orden`

**Reemplazar TODO el código del nodo con el siguiente:**

```javascript
// ============================================
// PREPARAR ORDEN v3.0 - ATR-BASED SL/TP
// ============================================
// MEJORAS v3.0:
// 1. SL/TP dinámicos basados en ATR real
// 2. Validación de ratio R/R mínimo 1.5:1
// 3. Volume calculado por Risk Manager (si existe)
// 4. Trailing stop configurado
// ============================================

const decision = $input.all()[0].json;

console.log("========================================");
console.log("📋 PREPARAR ORDEN v3.0");
console.log("========================================");

// Validar HOLD/ESPERAR
if (decision.decision === "esperar" || decision.decision === "hold" || decision.decision === "wait") {
    console.log("⏸️ HOLD - No se ejecuta orden");
    return [];
}

// Validar HALT del Risk Manager
if (decision.halt === true) {
    console.log(`🛑 HALT: ${decision.halt_reason}`);
    return [];
}

// Validar should_execute
if (decision.should_execute === false) {
    console.log("⏸️ should_execute: false");
    return [];
}

let orderType = "BUY";
if (decision.decision === "vender" || decision.decision === "sell") {
    orderType = "SELL";
}

// ============================================
// EXTRAER DATOS DEL MERCADO
// ============================================
let marketData = null;
try {
    if ($node["Memory Manager"] && $node["Memory Manager"].json) {
        marketData = $node["Memory Manager"].json.market_data;
    }
} catch(e) {}

if (!marketData && decision.market_data) marketData = decision.market_data;

const symbol = marketData?.best_pair || decision.symbol || "EURUSD";
const isJPY = symbol.includes("JPY");
const decimals = isJPY ? 1000 : 100000;

// ============================================
// PRECIO DE ENTRADA
// ============================================
let entryPrice = 0;

if (decision.entrada?.precio) entryPrice = decision.entrada.precio;
else if (marketData?.technical?.current_price) entryPrice = marketData.technical.current_price;
else if (marketData?.all_pairs?.[symbol]?.technical?.current_price) {
    entryPrice = marketData.all_pairs[symbol].technical.current_price;
}

// Validar precio
const VALID_RANGES = {
    EURUSD: { min: 1.05, max: 1.20 }, GBPUSD: { min: 1.25, max: 1.40 },
    USDJPY: { min: 140.0, max: 170.0 }, USDCHF: { min: 0.85, max: 1.00 }
};
const DEFAULT_PRICES = { EURUSD: 1.1689, GBPUSD: 1.3570, USDJPY: 159.88, USDCHF: 0.7996 };

const range = VALID_RANGES[symbol];
if (!entryPrice || entryPrice < range.min || entryPrice > range.max) {
    console.log(`⚠️ Precio inválido para ${symbol}: ${entryPrice}, usando default`);
    entryPrice = DEFAULT_PRICES[symbol];
}

console.log(`💵 ${symbol} entrada: ${entryPrice}`);

// ============================================
// ATR-BASED SL/TP v3.0
// ============================================
// Intentar obtener ATR del análisis técnico
let atr = marketData?.technical?.atr || 0;

// Si no hay ATR disponible, usar valores estimados
if (atr === 0) {
    atr = entryPrice * (isJPY ? 0.003 : 0.0003); // ~30 pips para JPY, ~3 pips para otros
    console.log(`⚠️ ATR no disponible, estimando: ${atr.toFixed(decimals === 1000 ? 4 : 6)}`);
}

// Multiplicadores ATR
const SL_ATR_MULTIPLIER = 1.5;    // 1.5x ATR para stop loss
const TP1_ATR_MULTIPLIER = 2.5;   // 2.5x ATR para TP1 (R/R = 1.67:1)
const TP2_ATR_MULTIPLIER = 4.0;   // 4.0x ATR para TP2 (R/R = 2.67:1)

const slDistance = atr * SL_ATR_MULTIPLIER;
const tp1Distance = atr * TP1_ATR_MULTIPLIER;
const tp2Distance = atr * TP2_ATR_MULTIPLIER;

let stopPrice, tp1Price, tp2Price;

if (orderType === "BUY") {
    stopPrice = entryPrice - slDistance;
    tp1Price = entryPrice + tp1Distance;
    tp2Price = entryPrice + tp2Distance;
} else {
    stopPrice = entryPrice + slDistance;
    tp1Price = entryPrice - tp1Distance;
    tp2Price = entryPrice - tp2Distance;
}

// Redondear
stopPrice = Math.round(stopPrice * decimals) / decimals;
tp1Price = Math.round(tp1Price * decimals) / decimals;
tp2Price = Math.round(tp2Price * decimals) / decimals;

// Validar R/R mínimo 1.5:1
const risk = Math.abs(entryPrice - stopPrice);
const reward1 = Math.abs(tp1Price - entryPrice);
const rrRatio = risk > 0 ? reward1 / risk : 0;

console.log(`📏 SL: ${stopPrice} | TP1: ${tp1Price} | TP2: ${tp2Price}`);
console.log(`📊 ATR: ${atr.toFixed(decimals === 1000 ? 4 : 6)} | R/R: ${rrRatio.toFixed(2)}:1`);

if (rrRatio < 1.5) {
    console.log(`⚠️ R/R ${rrRatio.toFixed(2)} < 1.5, ajustando TP1`);
    tp1Price = orderType === "BUY" ?
        entryPrice + (risk * 1.5) :
        entryPrice - (risk * 1.5);
    tp1Price = Math.round(tp1Price * decimals) / decimals;
}

// ============================================
// VOLUMEN (usar Risk Manager si disponible)
// ============================================
let volumeValue;

// Si Risk Manager calculó el volumen, usarlo
if (decision.volume && decision.volume > 0) {
    volumeValue = decision.volume;
    console.log(`📦 Volumen del Risk Manager: ${volumeValue}`);
} else {
    // Fallback: cálculo estándar
    const accountBalance = marketData?.risk?.account_balance || 100000;
    const riskPercent = 0.02;
    const riskInUSD = accountBalance * riskPercent;
    const slPips = Math.abs(entryPrice - stopPrice) * (isJPY ? 100 : 10000);
    volumeValue = slPips > 0 ? riskInUSD / (slPips * 10) : 0.01;
    volumeValue = Math.max(0.01, Math.min(0.05, volumeValue));
    volumeValue = Math.round(volumeValue * 100) / 100;
    console.log(`📦 Volumen calculado (fallback): ${volumeValue}`);
}

// ============================================
// RESULTADO FINAL
// ============================================
const finalRR = Math.abs(entryPrice - stopPrice) > 0 ?
    Math.abs(tp1Price - entryPrice) / Math.abs(entryPrice - stopPrice) : 0;

const tolerancia = isJPY ? 0.05 : 0.0005;
const slPips = Math.round(Math.abs(entryPrice - stopPrice) * (isJPY ? 100 : 10000));
const tp1Pips = Math.round(Math.abs(tp1Price - entryPrice) * (isJPY ? 100 : 10000));
const tp2Pips = Math.round(Math.abs(tp2Price - entryPrice) * (isJPY ? 100 : 10000));

console.log("\n========================================");
console.log("📋 ORDEN PREPARADA:");
console.log(`   ${orderType} ${symbol}`);
console.log(`   Entrada: ${entryPrice}`);
console.log(`   SL: ${stopPrice} (${slPips} pips)`);
console.log(`   TP1: ${tp1Price} (${tp1Pips} pips)`);
console.log(`   TP2: ${tp2Price} (${tp2Pips} pips)`);
console.log(`   Volumen: ${volumeValue}`);
console.log(`   R/R: ${finalRR.toFixed(2)}:1`);
console.log("========================================\n");

return [{
    json: {
        decision: orderType.toLowerCase(),
        confianza: decision.confianza || 50,
        razonamiento: decision.razonamiento || `${orderType} ${symbol} basado en análisis v3.0`,
        entrada: { precio: entryPrice, tolerancia: tolerancia },
        stop_loss: { precio: stopPrice, pips: slPips, atr_based: true },
        take_profit: { objetivo1: tp1Price, objetivo2: tp2Price, pips1: tp1Pips, pips2: tp2Pips },
        volumen: volumeValue,
        atr: atr,
        rr_ratio: finalRR,
        symbol: symbol,
        risk_percent: decision.risk_percent || 0.02,
        risk_in_usd: decision.risk_in_usd || 0,
        market_data: marketData,
        order_type: orderType,
        trailing_stop: {
            active: true,
            breakeven_trigger_pips: Math.round((atr * 2) * (isJPY ? 100 : 10000)),
            trailing_distance_pips: Math.round((atr * 1.5) * (isJPY ? 100 : 10000))
        }
    }
}];
```

---

## 6. NODO: Agente Técnico (jetson) - v3.0

**Archivo**: `jetson-CORRECTED.json`
**Nodo**: `Agente Técnico`

**Reemplazar TODO el código del nodo con el siguiente:**

```javascript
// ============================================
// AGENTE TÉCNICO v3.0 - INDICADORES AVANZADOS
// ============================================
// MEJORAS v3.0:
// 1. MACD con histograma y cruces
// 2. Bollinger Bands con %B y squeeze detection
// 3. ATR real para volatilidad
// 4. Análisis de volumen avanzado
// 5. Patrones de velas mejorados (engulfing, morning/evening star)
// 6. Clasificación de estado del mercado
// ============================================

const context = $input.all()[0].json;
const marketData = context.market_data || {};

const symbol = marketData.best_pair || "EURUSD";
const isJPY = symbol.includes("JPY");
const decimals = isJPY ? 1000 : 100000;
const pipMultiplier = isJPY ? 100 : 10000;

const currentPrice = marketData.technical?.current_price || marketData.all_prices?.[symbol] || 0;
const recentCandles = marketData.technical?.candles_recent || [];

// ============================================
// INDICADORES BÁSICOS
// ============================================
function calculateSMA(candles, period) {
    if (candles.length < period) return 0;
    return candles.slice(-period).reduce((s, c) => s + (c.close || 0), 0) / period;
}

function calculateEMA(candles, period) {
    if (candles.length < period) return 0;
    const k = 2 / (period + 1);
    const closes = candles.slice(0, period);
    let ema = closes.reduce((s, c) => s + (c.close || 0), 0) / period;
    for (let i = period; i < candles.length; i++) {
        ema = candles[i].close * k + ema * (1 - k);
    }
    return ema;
}

// SMAs
let sma9 = calculateSMA(recentCandles, 9);
let sma20 = calculateSMA(recentCandles, 20);
let sma50 = calculateSMA(recentCandles, 50);
let sma200 = calculateSMA(recentCandles, 200);

// ATR
function calculateATR(candles, period = 14) {
    if (candles.length < period + 1) return currentPrice * (isJPY ? 0.003 : 0.0003);
    let trueRanges = [];
    for (let i = candles.length - period; i < candles.length; i++) {
        const prev = candles[i - 1];
        const curr = candles[i];
        trueRanges.push(Math.max(
            (curr.high || 0) - (curr.low || 0),
            Math.abs((curr.high || 0) - (prev.close || 0)),
            Math.abs((curr.low || 0) - (prev.close || 0))
        ));
    }
    return trueRanges.reduce((s, tr) => s + tr, 0) / trueRanges.length;
}

const atr = calculateATR(recentCandles);

// MACD
function calculateMACD(candles, fast = 12, slow = 26, signal = 9) {
    if (candles.length < slow) return { macd: 0, signal: 0, histogram: 0, crossover: "none" };
    
    const closes = candles.map(c => c.close || 0);
    const fastEMA = calculateEMAAt(closes, fast, closes.length - 1);
    const slowEMA = calculateEMAAt(closes, slow, closes.length - 1);
    const macdLine = fastEMA - slowEMA;
    
    // Señal simplificada
    const prevMacd = calculateEMAAt(closes.slice(0, -1), fast, closes.length - 2) - 
                     calculateEMAAt(closes.slice(0, -1), slow, closes.length - 2);
    const signalLine = macdLine * 0.8 + prevMacd * 0.2; // Aproximación
    const histogram = macdLine - signalLine;
    
    let crossover = "none";
    const prevHist = prevMacd - signalLine;
    if (prevHist < 0 && histogram > 0) crossover = "bullish";
    else if (prevHist > 0 && histogram < 0) crossover = "bearish";
    
    return { macd: macdLine, signal: signalLine, histogram, crossover };
}

function calculateEMAAt(closes, period, index) {
    if (index < period - 1) return closes[index] || 0;
    const k = 2 / (period + 1);
    let ema = closes.slice(0, period).reduce((s, v) => s + v, 0) / period;
    for (let i = period; i <= index; i++) {
        ema = closes[i] * k + ema * (1 - k);
    }
    return ema;
}

const macd = calculateMACD(recentCandles);

// Bollinger Bands
function calculateBollinger(candles, period = 20, stdDev = 2) {
    if (candles.length < period) return { upper: 0, middle: 0, lower: 0, percentB: 0.5, squeeze: false };
    const closes = candles.slice(-period).map(c => c.close || 0);
    const middle = closes.reduce((s, v) => s + v, 0) / period;
    const variance = closes.reduce((s, v) => s + Math.pow(v - middle, 2), 0) / period;
    const std = Math.sqrt(variance);
    const upper = middle + stdDev * std;
    const lower = middle - stdDev * std;
    const percentB = (upper !== lower) ? (currentPrice - lower) / (upper - lower) : 0.5;
    const bandwidth = (upper - lower) / middle;
    return { upper, middle, lower, percentB, bandwidth, squeeze: bandwidth < 0.02 };
}

const bollinger = calculateBollinger(recentCandles);

// ============================================
// PATRONES DE VELAS MEJORADOS
// ============================================
function detectPatterns(candles) {
    if (candles.length < 3) return { pattern: "none", strength: 0 };
    
    const last = candles[candles.length - 1];
    const prev = candles[candles.length - 2];
    const prev2 = candles[candles.length - 3];
    
    const lastBody = Math.abs((last.close || 0) - (last.open || 0));
    const lastRange = (last.high || 0) - (last.low || 0);
    const prevBody = Math.abs((prev.close || 0) - (prev.open || 0));
    const prev2Body = Math.abs((prev2.close || 0) - (prev2.open || 0));
    const lastBullish = (last.close || 0) > (last.open || 0);
    const prevBullish = (prev.close || 0) > (prev.open || 0);
    const prev2Bullish = (prev2.close || 0) > (prev2.open || 0);
    
    let pattern = "none";
    let strength = 0;
    
    if (lastRange > 0 && lastBody / lastRange < 0.1) {
        pattern = "doji"; strength = 60;
    } else if (lastRange > 0) {
        const lowerWick = Math.min(last.close || 0, last.open || 0) - (last.low || 0);
        const upperWick = (last.high || 0) - Math.max(last.close || 0, last.open || 0);
        if (lowerWick > lastBody * 2 && upperWick < lastBody) {
            pattern = lastBullish ? "hammer_bullish" : "hammer_bearish"; strength = 70;
        } else if (lastBody > prevBody * 1.5 && lastBullish !== prevBullish) {
            pattern = lastBullish ? "bullish_engulfing" : "bearish_engulfing"; strength = 75;
        } else if (prevBody < lastBody * 0.3 && prev2Body > prevBody * 2 && candles.length >= 3) {
            if (prev2Bullish && !prevBullish && lastBullish) {
                pattern = "morning_star"; strength = 80;
            } else if (!prev2Bullish && prevBullish && !lastBullish) {
                pattern = "evening_star"; strength = 80;
            }
        }
    }
    
    return { pattern, strength };
}

const patterns = detectPatterns(recentCandles);

// ============================================
// VOLUMEN AVANZADO
// ============================================
let currentVolume = 0;
let avgVolume = 0;
let volumeRatio = 1;

if (recentCandles.length >= 5) {
    currentVolume = recentCandles[recentCandles.length - 1]?.tick_volume || 0;
    avgVolume = recentCandles.slice(-20).reduce((s, c) => s + (c.tick_volume || 0), 0) / Math.min(20, recentCandles.length);
    volumeRatio = avgVolume > 0 ? currentVolume / avgVolume : 1;
}

let volumeSignal = "neutral";
let volumeAdjustment = 0;
if (volumeRatio > 1.5) { volumeSignal = "high"; volumeAdjustment = 5; }
else if (volumeRatio < 0.5) { volumeSignal = "low"; volumeAdjustment = -5; }

// ============================================
// PRECIO vs SMAs
// ============================================
let priceVsSma9 = "neutral", priceVsSma20 = "neutral", priceVsSma50 = "neutral";
if (currentPrice > 0) {
    if (sma9 > 0) priceVsSma9 = currentPrice > sma9 * 1.001 ? "above" : (currentPrice < sma9 * 0.999 ? "below" : "at");
    if (sma20 > 0) priceVsSma20 = currentPrice > sma20 * 1.001 ? "above" : (currentPrice < sma20 * 0.999 ? "below" : "at");
    if (sma50 > 0) priceVsSma50 = currentPrice > sma50 * 1.001 ? "above" : (currentPrice < sma50 * 0.999 ? "below" : "at");
}

// ============================================
// CONTEXTO ADICIONAL
// ============================================
const currencyStrength = marketData.currency_strength || {};
const baseCurrency = symbol.substring(0, 3);
const quoteCurrency = symbol.substring(3, 6);
const baseStrength = currencyStrength.strength?.[baseCurrency] || "neutral";
const quoteStrength = currencyStrength.strength?.[quoteCurrency] || "neutral";

const positions = marketData.positions?.details || [];
const symbolPositions = positions.filter(p => p.symbol === symbol);
const totalExposure = symbolPositions.reduce((sum, p) => sum + Math.abs(p.volume || 0), 0);

const rsi = marketData.technical?.rsi || 50;
const rsiStatus = rsi < 30 ? "oversold" : (rsi > 70 ? "overbought" : "neutral");
const trend = marketData.technical?.trend || "neutral";
const support = marketData.technical?.support || (currentPrice * 0.995);
const resistance = marketData.technical?.resistance || (currentPrice * 1.005);
const score = marketData.best_score || 50;

// ============================================
// PROMPT ESTRUCTURADO v3.0
// ============================================
const last5Candles = recentCandles.slice(-5).map((c, i) => {
    const isBullish = (c.close || 0) > (c.open || 0);
    return `${i+1}. O:${c.open || 0} H:${c.high || 0} L:${c.low || 0} C:${c.close || 0} V:${c.tick_volume || 0} ${isBullish ? "BULL" : "BEAR"}`;
}).join("\n");

const prompt = `[INST] You are an expert forex trading analyst. Analyze ${symbol} with complete technical data.

═══════════════════════════════════════
📊 TECHNICAL ANALYSIS - ${symbol} v3.0
═══════════════════════════════════════
Price: ${currentPrice.toFixed(decimals === 1000 ? 3 : 5)}
RSI(14): ${rsi} (${rsiStatus.toUpperCase()})
Trend: ${trend.toUpperCase()} | Score: ${score}/100

═══ MOVING AVERAGES ═══
SMA9: ${sma9.toFixed(decimals === 1000 ? 3 : 5)} | ${priceVsSma9.toUpperCase()}
SMA20: ${sma20.toFixed(decimals === 1000 ? 3 : 5)} | ${priceVsSma20.toUpperCase()}
SMA50: ${sma50.toFixed(decimals === 1000 ? 3 : 5)} | ${priceVsSma50.toUpperCase()}

═══ MACD ═══
MACD: ${macd.macd.toFixed(decimals === 1000 ? 4 : 6)}
Signal: ${macd.signal.toFixed(decimals === 1000 ? 4 : 6)}
Histogram: ${macd.histogram.toFixed(decimals === 1000 ? 4 : 6)}
Crossover: ${macd.crossover.toUpperCase()}

═══ BOLLINGER BANDS ═══
Upper: ${bollinger.upper.toFixed(decimals === 1000 ? 3 : 5)}
Middle: ${bollinger.middle.toFixed(decimals === 1000 ? 3 : 5)}
Lower: ${bollinger.lower.toFixed(decimals === 1000 ? 3 : 5)}
%B: ${bollinger.percentB.toFixed(2)} | Squeeze: ${bollinger.squeeze ? "YES" : "NO"}

═══ VOLATILITY ═══
ATR(14): ${atr.toFixed(decimals === 1000 ? 4 : 6)} (${(atr / currentPrice * 100).toFixed(3)}%)

═══ VOLUME ═══
Current: ${currentVolume} | Avg(20): ${avgVolume.toFixed(0)} | Ratio: ${volumeRatio.toFixed(2)}x (${volumeSignal.toUpperCase()})

═══ CANDLESTICK PATTERNS ═══
Pattern: ${patterns.pattern.toUpperCase()} (strength: ${patterns.strength})
Last 5 candles:
${last5Candles}

═══ SUPPORT/RESISTANCE ═══
Support: ${support.toFixed(decimals === 1000 ? 3 : 5)}
Resistance: ${resistance.toFixed(decimals === 1000 ? 3 : 5)}

═══ CURRENCY STRENGTH ═══
${baseCurrency}: ${baseStrength.toUpperCase()} | ${quoteCurrency}: ${quoteStrength.toUpperCase()}

═══ POSITIONS ═══
Open in ${symbol}: ${symbolPositions.length} | Exposure: ${totalExposure.toFixed(2)} lots

═══════════════════════════════════════
📋 REQUIRED JSON OUTPUT:
═══════════════════════════════════════
Return ONLY valid JSON:
{
  "signal": "buy" or "sell" or "neutral",
  "confidence": 0-100,
  "analysis": "Brief technical reasoning (1 sentence)",
  "structure": "bullish" or "bearish" or "range",
  "momentum": "strong" or "moderate" or "weak",
  "entry_zone": [low, high],
  "stop_zone": [low, high]
}

⚠️ Use price ${currentPrice} as reference for ALL calculations
⚠️ entry_zone must be within ±0.2% of current price
⚠️ stop_zone must be within 0.3-0.5% of current price [/INST]`;

console.log(`📈 Agente Técnico v3.0 - ${symbol}`);
console.log(`   Price: ${currentPrice} | RSI: ${rsi} | Trend: ${trend}`);
console.log(`   MACD: ${macd.crossover} | BB: %B=${bollinger.percentB.toFixed(2)} | ATR: ${(atr / currentPrice * 100).toFixed(3)}%`);
console.log(`   Pattern: ${patterns.pattern} | Volume: ${volumeRatio.toFixed(2)}x (${volumeSignal})`);

return [{
    json: {
        agent: "technical", context: context,
        technical_data: {
            symbol, price: currentPrice, rsi, trend, support, resistance, score,
            sma9, sma20, sma50, atr, macd, bollinger,
            volume_ratio: volumeRatio, volume_signal: volumeSignal,
            candle_pattern: patterns.pattern, pattern_strength: patterns.strength,
            base_strength: baseStrength, quote_strength: quoteStrength,
            current_exposure: totalExposure
        },
        prompt, status: "ready"
    }
}];
```

---

## 7. NODO: Agente Fundamental (jetson) - v3.0

**Archivo**: `jetson-CORRECTED.json`
**Nodo**: `Agente Fundamental`

**Reemplazar TODO el código del nodo con el siguiente:**

```javascript
// ============================================
// AGENTE FUNDAMENTAL v3.0 - NEWS SCORING PONDERADO
// ============================================
// MEJORAS v3.0:
// 1. News scoring ponderado por tipo, impacto y divisa
// 2. Economic calendar scoring con proximidad temporal
// 3. Evaluación de riesgo compuesto
// 4. Prompt estructurado con JSON schema
// ============================================

const context = $input.all()[0].json;
const marketData = context.market_data || {};

const symbol = marketData.best_pair || "EURUSD";
const baseCurrency = symbol.substring(0, 3);
const quoteCurrency = symbol.substring(3, 6);

const fundamentalData = marketData.fundamental || {};
const newsList = fundamentalData.relevant_news || [];
const calendarList = fundamentalData.calendar_events || [];

// ============================================
// CONFIGURACIÓN DE PESOS
// ============================================
const CURRENCY_WEIGHTS = { USD: 1.0, EUR: 0.9, GBP: 0.85, JPY: 0.75, CHF: 0.65 };
const NEWS_TYPE_WEIGHTS = {
    "NFP": 1.0, "CPI": 0.9, "rate_decision": 0.95,
    "GDP": 0.85, "employment": 0.8, "retail_sales": 0.75,
    "PMI": 0.7, "trade_balance": 0.65, "default": 0.6
};
const IMPACT_MULTIPLIERS = { high: 1.5, medium: 1.0, low: 0.5 };

// ============================================
// NEWS SCORING PONDERADO
// ============================================
function scoreNews(newsList) {
    let bullishScore = 0, bearishScore = 0, totalWeight = 0;
    let newsDetails = [];
    
    for (const news of newsList) {
        const currency = news.currency || "";
        const newsType = news.type || "default";
        const sentiment = (news.sentiment || "neutral").toLowerCase();
        const impact = (news.impact || news.relevance || "medium").toLowerCase();
        
        if (currency !== baseCurrency && currency !== quoteCurrency) continue;
        
        const currencyWeight = CURRENCY_WEIGHTS[currency] || 0.5;
        const typeWeight = NEWS_TYPE_WEIGHTS[newsType] || NEWS_TYPE_WEIGHTS.default;
        const impactMult = IMPACT_MULTIPLIERS[impact] || 1.0;
        const weight = currencyWeight * typeWeight * impactMult;
        totalWeight += weight;
        
        if (sentiment === "bullish") bullishScore += weight;
        else if (sentiment === "bearish") bearishScore += weight;
        
        newsDetails.push({
            headline: news.headline || news.title || "Unknown",
            currency, sentiment, weight: weight.toFixed(2)
        });
    }
    
    if (totalWeight === 0) return { score: 0, bias: "neutral", confidence: 0, details: [] };
    
    const netScore = ((bullishScore - bearishScore) / totalWeight) * 100;
    const bias = netScore > 15 ? "bullish" : (netScore < -15 ? "bearish" : "neutral");
    const confidence = Math.min(100, Math.abs(netScore) * 2);
    
    return {
        score: Math.round(netScore), bias, confidence: Math.round(confidence),
        bullish: Math.round(bullishScore), bearish: Math.round(bearishScore),
        details: newsDetails.slice(0, 5)
    };
}

// ============================================
// CALENDAR SCORING
// ============================================
function scoreCalendar(calendarList) {
    const IMPACT_WEIGHTS = { high: 3, medium: 2, low: 1 };
    let totalRisk = 0, highImpactCount = 0;
    let eventDetails = [];
    
    for (const event of calendarList) {
        const currency = event.currency || "";
        if (currency !== baseCurrency && currency !== quoteCurrency) continue;
        
        const impact = (event.impact || "medium").toLowerCase();
        const weight = IMPACT_WEIGHTS[impact] || 2;
        totalRisk += weight * 1.5; // Proximidad temporal
        if (impact === "high") highImpactCount++;
        
        eventDetails.push({
            event: event.event || event.name || "Unknown",
            currency, impact, forecast: event.forecast || "N/A"
        });
    }
    
    let risk_level = "low", recommendation = "act";
    if (totalRisk > 6 || highImpactCount >= 2) { risk_level = "high"; recommendation = "wait"; }
    else if (totalRisk > 3 || highImpactCount >= 1) { risk_level = "medium"; recommendation = "act"; }
    
    return { risk_level, recommendation, score: totalRisk, high_impact_count: highImpactCount, details: eventDetails.slice(0, 5) };
}

const newsScore = scoreNews(newsList);
const calendarScore = scoreCalendar(calendarList);

// Evaluación compuesta
let fundamentalRisk = "low";
if (newsScore.confidence > 60 || calendarScore.risk_level === "high") fundamentalRisk = "high";
else if (newsScore.confidence > 30 || calendarScore.risk_level === "medium") fundamentalRisk = "medium";

let marketBias = newsScore.bias;
if (calendarScore.recommendation === "wait") marketBias = "volatile";

// ============================================
// PROMPT ESTRUCTURADO v3.0
// ============================================
const newsSummary = newsScore.details.length > 0 ?
    newsScore.details.map(n => `[${n.sentiment.toUpperCase()}] ${n.headline} (${n.currency}, weight: ${n.weight})`).join("\n") :
    "No news available";

const eventsSummary = calendarScore.details.length > 0 ?
    calendarScore.details.map(e => `[${e.impact.toUpperCase()}] ${e.currency}: ${e.event} (Forecast: ${e.forecast})`).join("\n") :
    "No events scheduled";

const prompt = `[INST] You are an expert forex fundamental analyst. Evaluate ${symbol} based on news and economic events.

═══════════════════════════════════════
📰 FUNDAMENTAL ANALYSIS - ${symbol} v3.0
═══════════════════════════════════════
Base Currency: ${baseCurrency} | Quote: ${quoteCurrency}

═══ NEWS SCORING (Weighted) ═══
Net Score: ${newsScore.score} (range: -100 to +100)
Bias: ${newsScore.bias.toUpperCase()} | Confidence: ${newsScore.confidence}%
Bullish: ${newsScore.bullish} | Bearish: ${newsScore.bearish}

═══ NEWS HEADLINES ═══
${newsSummary}

═══ ECONOMIC CALENDAR ═══
Risk Level: ${calendarScore.risk_level.toUpperCase()}
Recommendation: ${calendarScore.recommendation.toUpperCase()}
High Impact Events: ${calendarScore.high_impact_count}

═══ UPCOMING EVENTS ═══
${eventsSummary}

═══ RISK ASSESSMENT ═══
Fundamental Risk: ${fundamentalRisk.toUpperCase()}
Market Bias: ${marketBias.toUpperCase()}
${fundamentalRisk === "high" ? "⚠️ HIGH VOLATILITY - Consider waiting" : ""}
${calendarScore.recommendation === "wait" ? "⚠️ Important events pending - wait for clarity" : ""}

═══════════════════════════════════════
📋 REQUIRED JSON OUTPUT:
═══════════════════════════════════════
Return ONLY valid JSON:
{
  "recommendation": "act" or "wait" or "avoid",
  "confidence": 0-100,
  "analysis": "Brief fundamental reasoning (1 sentence)",
  "impact_direction": "bullish" or "bearish" or "neutral" or "volatile",
  "sentiment_score": -100 to 100,
  "news_risk": "low" or "medium" or "high"
}

⚠️ "act" = favorable for trading
⚠️ "wait" = uncertain, wait for clarity
⚠️ "avoid" = high risk, do NOT trade [/INST]`;

console.log(`📰 Agente Fundamental v3.0 - ${symbol}`);
console.log(`   News Score: ${newsScore.score} (${newsScore.bias}) | Risk: ${calendarScore.risk_level}`);
console.log(`   Events: ${calendarScore.high_impact_count} high impact | Recommendation: ${calendarScore.recommendation}`);

return [{
    json: {
        agent: "fundamental", context: context,
        fundamental_data: {
            symbol, total_news: newsList.length,
            news_score: newsScore, calendar_score: calendarScore,
            fundamental_risk: fundamentalRisk, market_bias: marketBias
        },
        prompt, status: "ready"
    }
}];
```

---

## 8. NODO: Agente Sentimiento (jetson) - v3.0

**Archivo**: `jetson-CORRECTED.json`
**Nodo**: `Agente Sentimiento`

**Reemplazar TODO el código del nodo con el siguiente:**

```javascript
// ============================================
// AGENTE SENTIMIENTO v3.0 - ANÁLISIS MEJORADO
// ============================================
// MEJORAS v3.0:
// 1. Análisis de posicionamiento long/short detallado
// 2. Señal contraria por posicionamiento extremo
// 3. Contexto risk-on/risk-off
// 4. Prompt estructurado con JSON schema
// ============================================

const context = $input.all()[0].json;
const marketData = context.market_data || {};

const symbol = marketData.best_pair || "EURUSD";

// Posiciones abiertas
const positions = marketData.positions?.details || [];
const symbolPositions = positions.filter(p => p.symbol === symbol);

// Órdenes pendientes
const pendingOrders = marketData.pending_orders?.details || [];
const symbolPendings = pendingOrders.filter(p => p.symbol === symbol);

// ============================================
// VOLÚMENES POR DIRECCIÓN
// ============================================
let longVolume = 0, shortVolume = 0, longCount = 0, shortCount = 0;

for (const p of symbolPositions) {
    const v = Math.abs(p.volume || 0);
    const type = (p.type || "").toLowerCase();
    if (type === "buy" || type === "long") { longVolume += v; longCount++; }
    else if (type === "sell" || type === "short") { shortVolume += v; shortCount++; }
}

let pendingLongVolume = 0, pendingShortVolume = 0, pendingLongCount = 0, pendingShortCount = 0;

for (const p of symbolPendings) {
    const v = Math.abs(p.volume || 0);
    const type = (p.type || "").toLowerCase();
    if (type.includes("buy") || type === "long") { pendingLongVolume += v; pendingLongCount++; }
    else if (type.includes("sell") || type === "short") { pendingShortVolume += v; pendingShortCount++; }
}

// ============================================
// ANÁLISIS COMPUESTO
// ============================================
const totalLong = longVolume + pendingLongVolume;
const totalShort = shortVolume + pendingShortVolume;
const totalVolume = totalLong + totalShort;

let longShortRatio = 0;
let sentimentScore = 0;

if (totalVolume > 0) {
    longShortRatio = totalLong / (totalShort || 0.001);
    sentimentScore = ((totalLong - totalShort) / totalVolume) * 100;
}

let sentimentLevel = "neutral";
let contrarianSignal = "none";

if (sentimentScore > 70) { sentimentLevel = "extreme_bullish"; contrarianSignal = "sell"; }
else if (sentimentScore > 30) { sentimentLevel = "bullish"; }
else if (sentimentScore < -70) { sentimentLevel = "extreme_bearish"; contrarianSignal = "buy"; }
else if (sentimentScore < -30) { sentimentLevel = "bearish"; }

let positioningAnalysis = "balanced";
if (longCount > shortCount * 2 && longCount > 0) positioningAnalysis = "crowded_long";
else if (shortCount > longCount * 2 && shortCount > 0) positioningAnalysis = "crowded_short";
else if (totalVolume === 0) positioningAnalysis = "no_positions";

// Contexto
const currencyStrength = marketData.currency_strength || {};
const sentiment = currencyStrength.sentiment || {};
const marketMood = sentiment.market_mood || sentiment.overall || "neutral";
const baseCurrency = symbol.substring(0, 3);
const quoteCurrency = symbol.substring(3, 6);
const baseStrength = currencyStrength.strength?.[baseCurrency] || "neutral";
const quoteStrength = currencyStrength.strength?.[quoteCurrency] || "neutral";

// ============================================
// PROMPT ESTRUCTURADO v3.0
// ============================================
const prompt = `[INST] You are an expert forex sentiment analyst. Analyze market positioning for ${symbol}.

═══════════════════════════════════════
💭 SENTIMENT ANALYSIS - ${symbol} v3.0
═══════════════════════════════════════

═══ OPEN POSITIONS ═══
Long: ${longCount} positions (${longVolume.toFixed(2)} lots)
Short: ${shortCount} positions (${shortVolume.toFixed(2)} lots)

═══ PENDING ORDERS ═══
Pending Buy: ${pendingLongCount} (${pendingLongVolume.toFixed(2)} lots)
Pending Sell: ${pendingShortCount} (${pendingShortVolume.toFixed(2)} lots)

═══ COMBINED METRICS ═══
Total Long: ${totalLong.toFixed(2)} lots | Total Short: ${totalShort.toFixed(2)} lots
Long/Short Ratio: ${longShortRatio.toFixed(2)}:1
Sentiment Score: ${sentimentScore.toFixed(0)} (range: -100 to +100)

═══ SENTIMENT LEVEL ═══
Current: ${sentimentLevel.toUpperCase()}
Contrarian Signal: ${contrarianSignal.toUpperCase()}
Positioning: ${positioningAnalysis.toUpperCase()}

═══ MARKET CONTEXT ═══
Market Mood: ${marketMood.toUpperCase()}
${baseCurrency}: ${baseStrength.toUpperCase()} | ${quoteCurrency}: ${quoteStrength.toUpperCase()}
${sentimentLevel.includes("extreme") ? "⚠️ EXTREME positioning - contrarian opportunity" : ""}

═══ GUIDE ═══
Score > +70: Extreme bullish → Contrarian SELL
Score > +30: Moderate bullish → Bias long
Score -30 to +30: Neutral → No bias
Score < -30: Moderate bearish → Bias short
Score < -70: Extreme bearish → Contrarian BUY

═══════════════════════════════════════
📋 REQUIRED JSON OUTPUT:
═══════════════════════════════════════
Return ONLY valid JSON:
{
  "sentiment": "bullish" or "bearish" or "neutral",
  "confidence": 0-100,
  "analysis": "Brief sentiment reasoning (1 sentence)",
  "contrary_signal": "buy" or "sell" or "none",
  "sentiment_score": -100 to 100
} [/INST]`;

console.log(`💭 Agente Sentimiento v3.0 - ${symbol}`);
console.log(`   Long: ${totalLong.toFixed(2)} | Short: ${totalShort.toFixed(2)} | Ratio: ${longShortRatio.toFixed(2)}:1`);
console.log(`   Score: ${sentimentScore} | Level: ${sentimentLevel} | Contrarian: ${contrarianSignal}`);

return [{
    json: {
        agent: "sentiment", context: context,
        sentiment_data: {
            symbol, long_volume: totalLong, short_volume: totalShort,
            long_short_ratio: longShortRatio, sentiment_score: sentimentScore,
            sentiment_level: sentimentLevel, contrarian_signal: contrarianSignal,
            positioning: positioningAnalysis, market_mood: marketMood,
            base_strength: baseStrength, quote_strength: quoteStrength
        },
        prompt, status: "ready"
    }
}];
```

---

## RESUMEN DE CAMBIOS

| Mejora | Archivo | Nodo | Estado |
|--------|---------|------|--------|
| Indicadores avanzados (MACD, BB, ATR) | multi-agente | Analizar Pares | ✅ |
| Análisis de volumen avanzado | multi-agente | Analizar Pares | ✅ |
| Detección de patrones de velas | multi-agente | Analizar Pares | ✅ |
| Clasificación estado del mercado | multi-agente | Analizar Pares | ✅ |
| Correlaciones entre pares | multi-agente | Analizar Pares | ✅ |
| News Scoring ponderado | multi-agente | Analizar Pares | ✅ |
| Calendar Scoring | multi-agente | Analizar Pares | ✅ |
| Estacionalidad | multi-agente | Analizar Pares | ✅ |
| Circuit Breaker / Drawdown | multi-agente | Memory Manager | ✅ |
| Risk Manager adaptativo | multi-agente | NUEVO nodo | ✅ |
| Votación ponderada | multi-agente | Agente Estratega | ✅ |
| ATR-based SL/TP | multi-agente | Preparar Orden | ✅ |
| MACD, BB, ATR, patrones | jetson | Agente Técnico | ✅ |
| News Scoring ponderado | jetson | Agente Fundamental | ✅ |
| Análisis sentimiento mejorado | jetson | Agente Sentimiento | ✅ |
| Prompts estructurados con schema | Todos | Todos los agentes | ✅ |

---

## INSTRUCCIONES DE IMPLEMENTACIÓN

1. **Backup**: Antes de modificarar nada, haz backup de todos los archivos `.json`

2. **multi-agente-profesional-CORRECTED.json**:
   - Abre el archivo en n8n UI
   - Para cada nodo listado arriba, abre el nodo → Code → Reemplaza TODO el código
   - Para el nodo **Risk Manager v3.0**, crea un nuevo nodo de código y conéctalo después de `Check Action` (rama TRUE) y antes de `Preparar Orden`

3. **jetson-CORRECTED.json**:
   - Abre el archivo en n8n UI
   - Para cada agente (Técnico, Fundamental, Sentimiento), reemplaza el código

4. **Testing**:
   - Ejecuta manualmente una vez
   - Verifica los logs de cada nodo
   - Confirma que los indicadores avanzados se calculan correctamente
   - Verifica que el Risk Manager funciona (sin halt si no hay drawdown)
   - Confirma que la votación produce decisiones coherentes

5. **Activar Schedule**: Solo después de verificar que todo funciona correctamente.
