# GUÍA DE IMPLEMENTACIÓN v3.0 - SISTEMA DE TRADING FOREX

## RESUMEN

Este documento describe cómo implementar las mejoras v3.0 en los archivos de workflow existentes. Cada mejora incluye:
1. El código JavaScript completo para cada nodo
2. Instrucciones de dónde colocarlo
3. Dependencias entre nodos

---

## PASO 1: ACTUALIZAR "Analizar Pares" en multi-agente-profesional-CORRECTED.json

### Nuevas funciones a añadir:

Reemplaza la sección `// FUNCIONES DE ANÁLISIS TÉCNICO` con estas funciones mejoradas:

```javascript
// ============================================
// FUNCIONES DE ANÁLISIS TÉCNICO v3.0 MEJORADAS
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

// NUEVO v3.0: MACD con Histograma
function calculateMACD(candles, fast = 12, slow = 26, signal = 9) {
    if (!candles || candles.length < slow + signal) {
        return { macd: 0, signal: 0, histogram: 0, previousSignal: 0 };
    }
    
    const closes = candles.map(c => c.close || 0);
    
    function calculateEMA(data, period) {
        const multiplier = 2 / (period + 1);
        let ema = [data.slice(0, period).reduce((a, b) => a + b, 0) / period];
        
        for (let i = period; i < data.length; i++) {
            ema.push((data[i] - ema[ema.length - 1]) * multiplier + ema[ema.length - 1]);
        }
        return ema;
    }
    
    const emaFast = calculateEMA(closes, fast);
    const emaSlow = calculateEMA(closes, slow);
    
    const macdLine = [];
    const offset = slow - fast;
    for (let i = 0; i < emaFast.length; i++) {
        if (i + offset < emaSlow.length) {
            macdLine.push(emaFast[i] - emaSlow[i + offset]);
        }
    }
    
    const signalLine = calculateEMA(macdLine, signal);
    const histogram = [];
    for (let i = 0; i < Math.min(macdLine.length, signalLine.length); i++) {
        histogram.push(macdLine[macdLine.length - signalLine.length + i] - signalLine[i]);
    }
    
    return {
        macd: macdLine[macdLine.length - 1] || 0,
        signal: signalLine[signalLine.length - 1] || 0,
        histogram: histogram[histogram.length - 1] || 0,
        previousSignal: signalLine.length > 1 ? signalLine[signalLine.length - 2] : 0
    };
}

// NUEVO v3.0: Bandas de Bollinger
function calculateBollingerBands(candles, period = 20, multiplier = 2) {
    if (!candles || candles.length < period) {
        return { upper: 0, middle: 0, lower: 0, percentB: 0.5, bandwidth: 0, squeeze: false };
    }
    
    const closes = candles.slice(-period).map(c => c.close || 0);
    const currentPrice = closes[closes.length - 1];
    
    const middle = closes.reduce((a, b) => a + b, 0) / period;
    const variance = closes.reduce((sum, val) => sum + Math.pow(val - middle, 2), 0) / period;
    const stdDev = Math.sqrt(variance);
    
    const upper = middle + (multiplier * stdDev);
    const lower = middle - (multiplier * stdDev);
    
    const percentB = (upper - lower) !== 0 ? (currentPrice - lower) / (upper - lower) : 0.5;
    const bandwidth = middle !== 0 ? ((upper - lower) / middle) * 100 : 0;
    const squeeze = bandwidth < 4; // Squeeze cuando bandwidth < 4%
    
    return {
        upper, middle, lower,
        percentB: Math.round(percentB * 100) / 100,
        bandwidth: Math.round(bandwidth * 100) / 100,
        squeeze
    };
}

// NUEVO v3.0: ATR (Average True Range)
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
    
    return trueRanges.reduce((sum, tr) => sum + tr, 0) / trueRanges.length;
}

// NUEVO v3.0: Análisis de Volumen Avanzado
function analyzeVolume(candles, currentPrice, trend) {
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
    const volumeTrend = calculateTrendLine(recentVolumes);
    
    // Divergencia precio-volumen
    const last10Prices = candles.slice(-10).map(c => c.close || 0);
    const last10Volumes = candles.slice(-10).map(c => c.tick_volume || 0);
    const priceTrend = calculateTrendLine(last10Prices);
    const volTrend10 = calculateTrendLine(last10Volumes);
    const priceVolumeDivergence = (priceTrend === "up" && volTrend10 === "down") || 
                                   (priceTrend === "down" && volTrend10 === "up");
    
    // Detección de agotamiento
    const lastPriceChange = Math.abs((lastCandle.close || 0) - (prevCandle.close || 0));
    const avgPriceChange = candles.slice(-10).reduce((sum, c, i, arr) => {
        if (i === 0) return 0;
        return sum + Math.abs((arr[i].close || 0) - (arr[i-1].close || 0));
    }, 0) / 9;
    const exhaustionDetected = volumeRatio > 2.0 && lastPriceChange < avgPriceChange * 0.3;
    
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
        volume_ratio: Math.round(volumeRatio * 100) / 100, volume_trend: volumeTrend,
        price_volume_divergence: priceVolumeDivergence, exhaustion_detected: exhaustionDetected,
        breakout_confirmed: breakoutConfirmed, volume_signal: volumeSignal,
        confidence_adjustment: confidenceAdjustment
    };
}

function calculateTrendLine(values) {
    if (values.length < 3) return "flat";
    const n = values.length;
    const xMean = (n - 1) / 2;
    const yMean = values.reduce((s, v) => s + v, 0) / n;
    let numerator = 0, denominator = 0;
    for (let i = 0; i < n; i++) {
        numerator += (i - xMean) * (values[i] - yMean);
        denominator += (i - xMean) ** 2;
    }
    if (denominator === 0) return "flat";
    const slope = numerator / denominator;
    const normalizedSlope = slope / (yMean || 1);
    if (normalizedSlope > 0.02) return "up";
    if (normalizedSlope < -0.02) return "down";
    return "flat";
}

// NUEVO v3.0: Detección mejorada de patrones de velas
function detectCandlePatterns(candles) {
    if (!candles || candles.length < 3) return { pattern: "none", strength: 0 };
    
    const last = candles[candles.length - 1];
    const prev = candles[candles.length - 2];
    const prev2 = candles.length > 2 ? candles[candles.length - 3] : prev;
    
    const lastBody = Math.abs((last.close || 0) - (last.open || 0));
    const lastRange = (last.high || 0) - (last.low || 0);
    const prevBody = Math.abs((prev.close || 0) - (prev.open || 0));
    const lastIsBullish = (last.close || 0) > (last.open || 0);
    const prevIsBullish = (prev.close || 0) > (prev.open || 0);
    
    let pattern = "none";
    let strength = 0;
    
    // Doji
    if (lastRange > 0 && lastBody / lastRange < 0.1) { pattern = "doji"; strength = 60; }
    // Hammer
    else if (lastRange > 0) {
        const lowerWick = Math.min(last.close || 0, last.open || 0) - (last.low || 0);
        const upperWick = (last.high || 0) - Math.max(last.close || 0, last.open || 0);
        if (lowerWick > lastBody * 2 && upperWick < lastBody) {
            pattern = lastIsBullish ? "hammer_bullish" : "hammer_bearish"; strength = 70;
        }
        // Engulfing
        else if (lastBody > prevBody * 1.5 && lastIsBullish !== prevIsBullish) {
            pattern = lastIsBullish ? "bullish_engulfing" : "bearish_engulfing"; strength = 75;
        }
        // Morning/Evening Star
        else if (candles.length >= 3) {
            const prev2Body = Math.abs((prev2.close || 0) - (prev2.open || 0));
            const prevRange = (prev.high || 0) - (prev.low || 0);
            const prevBodyRatio = prevBody / prevRange;
            if (prevBodyRatio < 0.3 && lastBody > prevBody * 2) {
                if (lastIsBullish && !prevIsBullish) { pattern = "morning_star"; strength = 80; }
                else if (!lastIsBullish && prevIsBullish) { pattern = "evening_star"; strength = 80; }
            }
        }
    }
    
    return { pattern, strength };
}

// NUEVO v3.0: Clasificación de estado del mercado
function classifyMarketState(candles, atr, bollinger) {
    if (!candles || candles.length < 20) {
        return { trend: "unknown", volatility: "unknown", session_recommendation: "range_trading" };
    }
    
    const closes = candles.slice(-20).map(c => c.close || 0);
    const currentPrice = closes[closes.length - 1];
    const sma20 = closes.reduce((a, b) => a + b, 0) / 20;
    
    // Tendencia
    let trend = "consolidation";
    if (candles.length >= 50) {
        const sma50 = candles.slice(-50).map(c => c.close || 0).reduce((a, b) => a + b, 0) / 50;
        if (sma20 > sma50 * 1.002) trend = "bullish";
        else if (sma20 < sma50 * 0.998) trend = "bearish";
    }
    
    // Volatilidad
    const atrPercent = currentPrice > 0 ? (atr / currentPrice) * 100 : 0;
    let volatility = "normal";
    if (bollinger.bandwidth > 8 || atrPercent > 0.5) volatility = "high";
    else if (bollinger.bandwidth < 4 || atrPercent < 0.2) volatility = "low";
    
    // Recomendación de estrategia
    let recommendation = "range_trading";
    if (trend !== "consolidation" && volatility === "normal") recommendation = "trend_following";
    else if (volatility === "high" && bollinger.squeeze) recommendation = "breakout";
    else if (volatility === "low") recommendation = "mean_reversion";
    
    return { trend, volatility, strategy_recommendation: recommendation };
}

// NUEVO v3.0: News Scoring Ponderado
function scoreNewsByRelevance(newsList, symbol) {
    if (!newsList || newsList.length === 0) return { score: 0, bias: "neutral", weighted_score: 0 };
    
    const CURRENCY_WEIGHTS = { USD: 1.0, EUR: 0.9, GBP: 0.85, JPY: 0.8, CHF: 0.7 };
    const NEWS_TYPE_WEIGHTS = { NFP: 1.0, CPI: 0.9, rate_decision: 0.95, FOMC: 0.9, GDP: 0.8 };
    
    const baseCurrency = symbol.substring(0, 3);
    const quoteCurrency = symbol.substring(3, 6);
    
    let totalScore = 0;
    let totalWeight = 0;
    let bullishCount = 0;
    let bearishCount = 0;
    
    for (const news of newsList) {
        const currency = news.currency || "";
        const currencyWeight = CURRENCY_WEIGHTS[currency] || 0.5;
        
        const newsType = (news.type || news.event || "").toUpperCase();
        let typeWeight = 0.5;
        for (const [type, weight] of Object.entries(NEWS_TYPE_WEIGHTS)) {
            if (newsType.includes(type)) { typeWeight = weight; break; }
        }
        
        const sentiment = (news.sentiment || "").toLowerCase();
        let sentimentScore = 0;
        if (sentiment === "bullish") { sentimentScore = 1; bullishCount++; }
        else if (sentiment === "bearish") { sentimentScore = -1; bearishCount++; }
        
        const weight = currencyWeight * typeWeight;
        totalScore += sentimentScore * weight;
        totalWeight += weight;
    }
    
    const weightedScore = totalWeight > 0 ? (totalScore / totalWeight) * 100 : 0;
    const bias = weightedScore > 15 ? "bullish" : weightedScore < -15 ? "bearish" : "neutral";
    
    return {
        score: Math.round(weightedScore), bias, weighted_score: Math.round(weightedScore),
        bullish_count: bullishCount, bearish_count: bearishCount, total_news: newsList.length
    };
}

// NUEVO v3.0: Economic Calendar Scoring
function scoreEconomicCalendar(events, symbol) {
    if (!events || events.length === 0) return { risk: "low", score: 0, recommendation: "act" };
    
    const baseCurrency = symbol.substring(0, 3);
    const quoteCurrency = symbol.substring(3, 6);
    
    let totalScore = 0;
    let highImpactCount = 0;
    const now = new Date();
    
    for (const event of events) {
        const currency = event.currency || "";
        if (currency !== baseCurrency && currency !== quoteCurrency) continue;
        
        const impact = (event.impact || "medium").toLowerCase();
        let weight = impact === "high" ? 3 : impact === "medium" ? 2 : 1;
        
        if (impact === "high") highImpactCount++;
        
        // Proximidad temporal
        if (event.time || event.date) {
            try {
                const eventTime = new Date(event.time || event.date);
                const hoursDiff = Math.abs(eventTime - now) / (1000 * 60 * 60);
                if (hoursDiff < 2) weight *= 2;
                else if (hoursDiff < 6) weight *= 1.5;
            } catch (e) {}
        }
        
        totalScore += weight;
    }
    
    let risk = "low";
    let recommendation = "act";
    if (totalScore > 10 || highImpactCount > 2) { risk = "high"; recommendation = "wait"; }
    else if (totalScore > 5 || highImpactCount > 0) { risk = "medium"; }
    
    return { risk, score: Math.round(totalScore), recommendation, high_impact_count: highImpactCount };
}

// NUEVO v3.0: Correlaciones entre pares
const PAIR_CORRELATIONS = {
    "EURUSD_GBPUSD": 0.85, "EURUSD_USDCHF": -0.90, "GBPUSD_USDCHF": -0.80,
    "EURUSD_USDJPY": -0.30, "GBPUSD_USDJPY": -0.25, "USDJPY_USDCHF": 0.40
};

function adjustByCorrelation(symbol, allPairsAnalysis) {
    let confidenceAdjustment = 0;
    let conflicts = [];
    
    for (const [otherSymbol, otherAnalysis] of Object.entries(allPairsAnalysis)) {
        if (otherSymbol === symbol) continue;
        
        const key1 = `${symbol}_${otherSymbol}`;
        const key2 = `${otherSymbol}_${symbol}`;
        const correlation = PAIR_CORRELATIONS[key1] || PAIR_CORRELATIONS[key2] || 0;
        
        if (Math.abs(correlation) > 0.7) {
            const otherTrend = otherAnalysis.technical?.trend || "neutral";
            const currentTrend = allPairsAnalysis[symbol]?.technical?.trend || "neutral";
            
            // Pares correlacionados positivamente deberían tener misma tendencia
            if (correlation > 0) {
                if (otherTrend === currentTrend && currentTrend !== "neutral") {
                    confidenceAdjustment += 10; // Confirmación
                } else if (otherTrend !== currentTrend && currentTrend !== "neutral" && otherTrend !== "neutral") {
                    confidenceAdjustment -= 15; // Conflicto
                    conflicts.push(`${otherSymbol}: ${otherTrend}`);
                }
            }
            // Pares correlacionados negativamente deberían tener tendencia opuesta
            else {
                const oppositeTrend = otherTrend === "bullish" ? "bearish" : otherTrend === "bearish" ? "bullish" : "neutral";
                if (currentTrend === oppositeTrend && currentTrend !== "neutral") {
                    confidenceAdjustment += 10; // Confirmación
                } else if (currentTrend === otherTrend && currentTrend !== "neutral") {
                    confidenceAdjustment -= 15; // Conflicto
                    conflicts.push(`${otherSymbol}: ${otherTrend} (esperado: ${oppositeTrend})`);
                }
            }
        }
    }
    
    return { confidence_adjustment: confidenceAdjustment, conflicts };
}

// NUEVO v3.0: Estacionalidad y condiciones temporales
function getSeasonalityFactors() {
    const now = new Date();
    const dayOfWeek = now.getDay(); // 0=Domingo, 6=Sábado
    const hourUTC = now.getUTCHours();
    const month = now.getUTCMonth();
    
    let confidenceAdjustment = 0;
    let session = "unknown";
    let factors = [];
    
    // Sesiones de trading
    if (hourUTC >= 0 && hourUTC < 7) { session = "asian"; confidenceAdjustment -= 10; factors.push("Asian session (lower volatility)"); }
    else if (hourUTC >= 7 && hourUTC < 12) { session = "european"; confidenceAdjustment += 5; factors.push("European session"); }
    else if (hourUTC >= 12 && hourUTC < 17) { session = "overlap"; confidenceAdjustment += 10; factors.push("London-NY overlap (high liquidity)"); }
    else if (hourUTC >= 17 && hourUTC < 22) { session = "us"; confidenceAdjustment += 5; factors.push("US session"); }
    else { session = "late"; confidenceAdjustment -= 5; factors.push("Late session (lower volume)"); }
    
    // Efecto día de la semana
    if (dayOfWeek === 1) { confidenceAdjustment -= 5; factors.push("Monday effect"); }
    else if (dayOfWeek === 5) { confidenceAdjustment -= 10; factors.push("Friday effect (position closing)"); }
    else if (dayOfWeek === 0 || dayOfWeek === 6) { confidenceAdjustment -= 20; factors.push("Weekend (market closed or thin)"); }
    
    // Verificar si es día de NFP (primer viernes del mes)
    if (dayOfWeek === 5 && now.getDate() <= 7 && month !== now.getMonth()) {
        confidenceAdjustment -= 20; factors.push("⚠️ NFP Day - HIGH VOLATILITY");
    }
    
    // Efecto verano
    if (month >= 6 && month <= 8) { confidenceAdjustment -= 5; factors.push("Summer lull"); }
    
    return { confidence_adjustment: confidenceAdjustment, session, factors };
}
```

---

## PASO 2: ACTUALIZAR PROCESAMIENTO DE PARES

Reemplaza el bucle `// PROCESAR CADA PAR DE DIVISAS` con esta versión v3.0:

```javascript
// ============================================
// PROCESAR CADA PAR DE DIVISAS v3.0
// ============================================
const symbols = ["EURUSD", "GBPUSD", "USDJPY", "USDCHF"];

for (const symbol of symbols) {
    console.log(`\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━`);
    console.log(`📊 Procesando par: ${symbol}`);
    
    let currentPrice = globalRates[symbol];
    console.log(`   ✅ Precio desde globalRates: ${currentPrice}`);
    
    // Buscar velas para este símbolo
    let candlesData = [];
    for (const item of allItems) {
        const data = item.json;
        if (data.symbol === symbol && data.candles && Array.isArray(data.candles)) {
            candlesData = data.candles;
            console.log(`   ✅ Velas encontradas: ${candlesData.length} candles`);
            break;
        }
    }
    
    if (candlesData.length === 0) {
        console.log(`   ⚠️ No se encontraron velas para ${symbol}`);
    }
    
    // Calcular indicadores técnicos v3.0
    const rsi = calculateRSI(candlesData);
    const trend = determineTrend(candlesData);
    const sr = findSupportResistance(candlesData, currentPrice);
    const macd = calculateMACD(candlesData);
    const bollinger = calculateBollingerBands(candlesData);
    const atr = calculateATR(candlesData);
    const volumeAnalysis = analyzeVolume(candlesData, currentPrice, trend);
    const candlePatterns = detectCandlePatterns(candlesData);
    const marketState = classifyMarketState(candlesData, atr, bollinger);
    
    // Filtrar noticias y eventos relevantes
    let relevantCurrencies = [];
    if (symbol === "EURUSD") relevantCurrencies = ["EUR", "USD"];
    else if (symbol === "GBPUSD") relevantCurrencies = ["GBP", "USD"];
    else if (symbol === "USDJPY") relevantCurrencies = ["USD", "JPY"];
    else if (symbol === "USDCHF") relevantCurrencies = ["USD", "CHF"];
    
    const relevantNews = globalNews.filter(news => 
        news.currency && relevantCurrencies.includes(news.currency)
    );
    const relevantEvents = globalCalendar.filter(event => 
        event.currency && relevantCurrencies.includes(event.currency)
    );
    
    // NUEVO v3.0: News scoring ponderado
    const newsScore = scoreNewsByRelevance(relevantNews, symbol);
    const calendarScore = scoreEconomicCalendar(relevantEvents, symbol);
    
    // Ajustar según fortaleza de divisas si no hay velas
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
        console.log(`   📊 Ajustado por fortaleza: ${finalTrend}, RSI: ${finalRsi}`);
    }
    
    // Calcular score v3.0 (más factores)
    let score = 50;
    if (finalRsi < 35) score += 20; if (finalRsi > 65) score += 20;
    if (finalTrend === "bullish") score += 15; if (finalTrend === "bearish") score += 15;
    
    // MACD contribution
    if (macd.histogram > 0 && macd.signal > macd.previousSignal) score += 10;
    else if (macd.histogram < 0 && macd.signal < macd.previousSignal) score -= 10;
    
    // Bollinger contribution
    if (bollinger.percentB < 0.2) score += 8; else if (bollinger.percentB > 0.8) score -= 8;
    
    // Volume contribution
    score += volumeAnalysis.confidence_adjustment;
    
    // Candle pattern contribution
    if (candlePatterns.pattern !== "none") score += candlePatterns.strength * 0.15;
    
    // News score contribution
    score += newsScore.weighted_score * 0.1;
    
    // Calendar risk adjustment
    if (calendarScore.recommendation === "wait") score = Math.min(score, 40);
    
    let finalScore = Math.max(0, Math.min(100, score));
    
    // Filtrar posiciones y pendientes del símbolo
    const symbolPositions = globalPositions.filter(p => p.symbol === symbol);
    const symbolPendingOrders = globalPendingOrders.filter(p => p.symbol === symbol);
    
    // Almacenar análisis v3.0
    pairsAnalysis[symbol] = {
        symbol: symbol, score: finalScore,
        technical: {
            current_price: currentPrice, rsi: finalRsi, trend: finalTrend,
            support: sr.support, resistance: sr.resistance,
            macd: macd, bollinger: bollinger, atr: atr,
            volume_analysis: volume_analysis, candle_patterns: candlePatterns,
            market_state: marketState
        },
        fundamental: {
            news_count: relevantNews.length, high_impact_events: relevantEvents.filter(e => e.impact === "high").length,
            news_score: newsScore, calendar_score: calendarScore,
            relevant_news: relevantNews.slice(0, 5), calendar_events: relevantEvents.slice(0, 5)
        },
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
    
    console.log(`   ✅ Score: ${finalScore} | RSI: ${finalRsi} | Trend: ${finalTrend} | Price: ${currentPrice}`);
    console.log(`   📊 MACD: ${macd.histogram.toFixed(5)} | BB: ${bollinger.percentB} | ATR: ${atr.toFixed(5)}`);
    console.log(`   📰 News: ${newsScore.bias} (${newsScore.score}) | Calendar: ${calendarScore.risk}`);
    console.log(`   🕯️ Pattern: ${candlePatterns.pattern} | Volume: ${volumeAnalysis.volume_signal}`);
    
    if (finalScore > bestScore) {
        bestScore = finalScore; bestPair = symbol;
        console.log(`   🏆 Nuevo mejor par: ${symbol} con score ${bestScore}`);
    }
}

// ============================================
// APLICAR CORRELACIONES DESPUÉS DE ANALIZAR TODOS LOS PARES
// ============================================
for (const symbol of symbols) {
    if (pairsAnalysis[symbol]) {
        const correlationAdj = adjustByCorrelation(symbol, pairsAnalysis);
        pairsAnalysis[symbol].correlation_adjustment = correlationAdj;
        
        // Ajustar score final del par
        pairsAnalysis[symbol].score = Math.max(0, Math.min(100, 
            pairsAnalysis[symbol].score + correlationAdj.confidence_adjustment
        ));
        
        if (correlationAdj.conflicts.length > 0) {
            console.log(`   ⚠️ ${symbol} correlación conflictiva con: ${correlationAdj.conflicts.join(", ")}`);
        }
    }
}

// Aplicar estacionalidad
const seasonality = getSeasonalityFactors();
console.log(`\n📅 Estacionalidad: ${seasonality.session} | Ajuste: ${seasonality.confidence_adjustment}%`);
console.log(`   Factores: ${seasonality.factors.join(", ")}`);

// Re-calcular mejor par considerando correlaciones
let adjustedBestPair = null;
let adjustedBestScore = 0;

for (const [sym, analysis] of Object.entries(pairsAnalysis)) {
    const adjustedScore = analysis.score + seasonality.confidence_adjustment;
    if (adjustedScore > adjustedBestScore) {
        adjustedBestScore = adjustedScore; adjustedBestPair = sym;
    }
}

if (adjustedBestPair) {
    bestPair = adjustedBestPair;
    bestScore = Math.max(0, Math.min(100, adjustedBestScore));
}

// Fallback: si no se encontró ningún par válido
if (!bestPair) { bestPair = "EURUSD"; bestScore = 50; }

// Asegurar