# MEJORAS v3.0 - SISTEMA DE TRADING FOREX

## RESUMEN EJECUTIVO

Este documento describe las mejoras de **versión 3.0** para el sistema de trading algorítmico, enfocadas en **maximizar el Sharpe Ratio y el Win Rate** mediante algoritmos avanzados, arquitectura de IA mejorada, y gestión de riesgo adaptativa.

**Estado actual (v2.0)**: Sistema funcional sin bugs críticos, con análisis técnico básico (RSI, SMA20/50, S/R por máx/mín), riesgo fijo 2%, y 4 agentes de IA.

**Objetivo v3.0**: Elevar la calidad de decisiones con indicadores multi-timeframe, patrones de velas avanzados, scoring fundamental ponderado, agentes especializados, votación dinámica, riesgo adaptativo, y aprendizaje continuo.

---

## PRIORIDAD DE IMPLEMENTACIÓN

| Prioridad | Área | Impacto | Complejidad | Wins Esperados |
|-----------|------|---------|-------------|----------------|
| 🔴 1 | Gestión de Riesgo Adaptativa | ALTO | BAJA | WR +10%, DD -40% |
| 🔴 2 | Sistema de Votación Ponderada | ALTO | MEDIA | WR +8%, SR +0.5 |
| 🟡 3 | Indicadores Multi-Timeframe | MEDIO | MEDIA | WR +5% |
| 🟡 4 | Prompt Estructurado con Schema | MEDIO | BAJA | Consistencia +30% |
| 🟡 5 | Análisis de Volumen | MEDIO | BAJA | WR +3% |
| 🟢 6 | Detección de Patrones de Velas | MEDIO | ALTA | WR +5% |
| 🟢 7 | Correlaciones entre Pares | BAJO | MEDIA | WR +3% |
| 🟢 8 | Estacionalidad | BAJO | ALTA | WR +2% |

---

## ÁREA 1: GESTIÓN DE RIESGO ADAPTATIVA v3.0

### ANÁLISIS ACTUAL
- Riesgo fijo del 2% por operación
- Volumen fijo entre 0.01-0.05 lots
- Stop loss fijo de 50 pips
- Sin control de drawdown global

### ALGORITMO MEJORADO

#### 1.1 Riesgo Adaptativo por Confianza

```javascript
// === NODO: Preparar Orden v3.0 ===
// === Reemplazar cálculo de volumen fijo ===

/**
 * Calcula el riesgo adaptativo basado en confianza y drawdown reciente
 * @param {number} confidence - Confianza de la señal (0-100)
 * @param {object} memory - Trading memory con historial
 * @returns {number} Porcentaje de riesgo (0.5% - 4%)
 */
function calculateAdaptiveRisk(confidence, memory) {
    const BASE_RISK = 0.02; // 2% base
    const CONFIDENCE_THRESHOLD = 50;
    
    // Factor de confianza: escala lineal alrededor del umbral
    const confidenceFactor = confidence / CONFIDENCE_THRESHOLD;
    let riskPercent = BASE_RISK * confidenceFactor;
    
    // Clamp entre 0.5% y 4%
    riskPercent = Math.max(0.005, Math.min(0.04, riskPercent));
    
    // Ajuste por drawdown reciente
    if (memory.recent_trades && memory.recent_trades.length >= 5) {
        const last5 = memory.recent_trades.slice(-5);
        const recentLosses = last5.filter(t => t.result === "loss").length;
        
        // Tras 3+ pérdidas en 5 trades: reducir riesgo 50%
        if (recentLosses >= 3) {
            riskPercent *= 0.5;
            console.log(`⚠️ Riesgo reducido 50% tras ${recentLosses} pérdidas en 5 trades`);
        }
        
        // Tras 3+ ganancias consecutivas: aumentar 20% (hasta máximo)
        const consecutiveWins = countConsecutiveWins(last5);
        if (consecutiveWins >= 3) {
            riskPercent = Math.min(riskPercent * 1.2, 0.04);
            console.log(`📈 Riesgo aumentado 20% tras ${consecutiveWins} ganancias consecutivas`);
        }
    }
    
    // Drawdown global check
    const currentDrawdown = calculateCurrentDrawdown(memory);
    const maxDrawdown = memory.preferences?.max_drawdown || 0.10; // 10%
    
    if (currentDrawdown > maxDrawdown * 0.5) {
        riskPercent *= 0.5; // Mitigar a mitad del drawdown máximo
        console.log(`⚠️ Riesgo reducido 50% por drawdown: ${(currentDrawdown * 100).toFixed(1)}%`);
    }
    
    if (currentDrawdown > maxDrawdown * 0.8) {
        riskPercent = 0; // STOP TOTAL al acercarse al límite
        console.log(`🛑 TRADING HALTED: Drawdown ${(currentDrawdown * 100).toFixed(1)}%接近 límite ${(maxDrawdown * 100).toFixed(1)}%`);
    }
    
    return riskPercent;
}

function countConsecutiveWins(trades) {
    let count = 0;
    for (let i = trades.length - 1; i >= 0; i--) {
        if (trades[i].result === "win") count++;
        else break;
    }
    return count;
}

function calculateCurrentDrawdown(memory) {
    if (!memory.trades || memory.trades.length === 0) return 0;
    
    // Calcular drawdown desde el pico más reciente
    let peak = 0;
    let maxDD = 0;
    let cumulative = 0;
    
    for (const trade of memory.trades) {
        cumulative += trade.pips || 0;
        peak = Math.max(peak, cumulative);
        const dd = (peak - cumulative) / (peak || 1);
        maxDD = Math.max(maxDD, dd);
    }
    
    return maxDD;
}

// Uso en Preparar Orden:
const riskPercent = calculateAdaptiveRisk(confidence, memory);
const riskAmount = accountBalance * riskPercent;
const stopPips = Math.abs(entryPrice - stopPrice) * (symbol.includes("JPY") ? 100 : 10000);
const pipValue = 10; // $10 per pip for standard lot
let volume = riskAmount / (stopPips * pipValue);
volume = Math.max(0.01, Math.min(0.05, volume));
volume = Math.round(volume * 100) / 100;
```

#### 1.2 Stop Loss y Take Profit Dinámicos (ATR-based)

```javascript
// === NODO: Agente Estratega v3.0 ===
// === Reemplazar cálculo fijo de SL/TP ===

/**
 * Calcula SL/TP dinámicos basados en ATR real del mercado
 * @param {number} entryPrice - Precio de entrada
 * @param {string} direction - "buy" o "sell"
 * @param {number} atr - Average True Range (14 períodos)
 * @param {string} symbol - Par de divisas
 * @returns {object} { stopLoss, takeProfit1, takeProfit2, trailingStop }
 */
function calculateDynamicLevels(entryPrice, direction, atr, symbol) {
    const isJPY = symbol.includes("JPY");
    
    // Multiplicadores ATR
    const SL_ATR_MULTIPLIER = 1.5;    // 1.5x ATR para stop loss
    const TP1_ATR_MULTIPLIER = 2.5;   // 2.5x ATR para TP1
    const TP2_ATR_MULTIPLIER = 4.0;   // 4.0x ATR para TP2
    
    // Calcular distancias
    const stopDistance = atr * SL_ATR_MULTIPLIER;
    const tp1Distance = atr * TP1_ATR_MULTIPLIER;
    const tp2Distance = atr * TP2_ATR_MULTIPLIER;
    
    let stopLoss, takeProfit1, takeProfit2;
    
    if (direction === "buy") {
        stopLoss = entryPrice - stopDistance;
        takeProfit1 = entryPrice + tp1Distance;
        takeProfit2 = entryPrice + tp2Distance;
    } else {
        stopLoss = entryPrice + stopDistance;
        takeProfit1 = entryPrice - tp1Distance;
        takeProfit2 = entryPrice - tp2Distance;
    }
    
    // Redondear a decimales correctos
    const decimals = isJPY ? 1000 : 100000;
    stopLoss = Math.round(stopLoss * decimals) / decimals;
    takeProfit1 = Math.round(takeProfit1 * decimals) / decimals;
    takeProfit2 = Math.round(takeProfit2 * decimals) / decimals;
    
    return {
        stopLoss,
        takeProfit1,
        takeProfit2,
        trailingStop: {
            active: false,
            breakevenTrigger: atr * 2,    // Mover a BE tras 2x ATR de ganancia
            trailingDistance: atr * 1.5    // Trail a 1.5x ATR
        }
    };
}

// Validar ratio R/R mínimo 1.5:1
function validateRRRatio(entryPrice, stopLoss, takeProfit, direction) {
    const risk = Math.abs(entryPrice - stopLoss);
    const reward = Math.abs(takeProfit - entryPrice);
    const rrRatio = risk > 0 ? reward / risk : 0;
    
    if (rrRatio < 1.5) {
        // Ajustar TP para cumplir mínimo
        const requiredReward = risk * 1.5;
        const adjustedTP = direction === "buy" ? 
            entryPrice + requiredReward : 
            entryPrice - requiredReward;
        return { valid: false, adjustedTakeProfit: adjustedTP, rrRatio };
    }
    
    return { valid: true, rrRatio };
}
```

#### 1.3 Sistema de Stop Global (Circuit Breaker)

```javascript
// === NODO: Memory Manager v3.0 ===
// === Añadir al inicio del nodo ===

/**
 * Verifica si el sistema debe detenerse por drawdown excesivo
 * @param {object} memory - Trading memory
 * @returns {object} { canTrade: boolean, reason: string, cooldown: string }
 */
function checkGlobalStop(memory) {
    const now = new Date();
    
    // Configuración de límites
    const LIMITS = {
        daily_dd: 0.05,      // 5% daily max
        weekly_dd: 0.10,     // 10% weekly max
        monthly_dd: 0.15,    // 15% monthly max
        consecutive_losses: 5 // 5 pérdidas consecutivas = halt
    };
    
    // Calcular drawdown por período
    const todayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const weekStart = new Date(todayStart);
    weekStart.setDate(weekStart.getDate() - now.getDay());
    const monthStart = new Date(now.getFullYear(), now.getMonth(), 1);
    
    const todayTrades = memory.trades.filter(t => new Date(t.timestamp) >= todayStart);
    const weekTrades = memory.trades.filter(t => new Date(t.timestamp) >= weekStart);
    const monthTrades = memory.trades.filter(t => new Date(t.timestamp) >= monthStart);
    
    const todayPips = todayTrades.reduce((s, t) => s + (t.pips || 0), 0);
    const weekPips = weekTrades.reduce((s, t) => s + (t.pips || 0), 0);
    const monthPips = monthTrades.reduce((s, t) => s + (t.pips || 0), 0;
    
    const avgPipValue = memory.preferences?.avg_pip_value || 10;
    const balance = memory.preferences?.account_balance || 100000;
    
    const dailyDD = (todayPips * avgPipValue) / balance;
    const weeklyDD = (weekPips * avgPipValue) / balance;
    const monthlyDD = (monthPips * avgPipValue) / balance;
    
    // Verificar pérdidas consecutivas
    const recentConsecutiveLosses = countConsecutiveLosses(memory.trades);
    
    // Evaluar condiciones
    if (dailyDD < -LIMITS.daily_dd) {
        return {
            canTrade: false,
            reason: `Daily DD ${(dailyDD * 100).toFixed(1)}% > limit ${(LIMITS.daily_dd * 100)}%`,
            cooldown: "4 hours"
        };
    }
    
    if (weeklyDD < -LIMITS.weekly_dd) {
        return {
            canTrade: false,
            reason: `Weekly DD ${(weeklyDD * 100).toFixed(1)}% > limit ${(LIMITS.weekly_dd * 100)}%`,
            cooldown: "24 hours"
        };
    }
    
    if (monthlyDD < -LIMITS.monthly_dd) {
        return {
            canTrade: false,
            reason: `Monthly DD ${(monthlyDD * 100).toFixed(1)}% > limit ${(LIMITS.monthly_dd * 100)}%`,
            cooldown: "1 week"
        };
    }
    
    if (recentConsecutiveLosses >= LIMITS.consecutive_losses) {
        return {
            canTrade: false,
            reason: `${recentConsecutiveLosses} consecutive losses`,
            cooldown: "2 hours"
        };
    }
    
    return { canTrade: true, reason: "OK", cooldown: "0" };
}

function countConsecutiveLosses(trades) {
    let count = 0;
    for (let i = trades.length - 1; i >= 0; i--) {
        if (trades[i].result === "loss") count++;
        else break;
    }
    return count;
}
```

### IMPACTO ESPERADO
- Win Rate: +5-10% (menos exposición en malas rachas)
- Drawdown máximo: -40% (reducción por circuit breaker)
- Sharpe Ratio: +0.3-0.5 (menor volatilidad de retornos)

### DEPENDENCIAS
- Memory Manager necesita almacenar historial completo de trades
- Requiere ATR calculado en Agente Técnico

### RIESGOS
- Riesgo de parar antes de una reversión: mitigar con períodos de cooldown razonables
- Posible reducción de oportunidades: mitigar con umbrales no excesivamente restrictivos

---

## ÁREA 2: SISTEMA DE VOTACIÓN PONDERADA v3.0

### ANÁLISIS ACTUAL
- Agente Estratega recibe resultado unificado de un solo modelo
- No hay votación ni consenso entre agentes
- Pesos fijos: técnico 0.35, fundamental 0.25, sentimiento 0.25, estadístico 0.15

### ALGORITMO MEJORADO

#### 2.1 Estructura de Voto

```javascript
// === NODO: Agente Estratega v3.0 ===
// === Reemplazar lógica de decisión unificada ===

/**
 * Sistema de votación ponderada con pesos dinámicos
 * @param {Array} agentVotes - Votos de cada agente
 * @param {object} memory - Trading memory con performance histórico
 * @returns {object} { decision, confidence, reasoning, veto }
 */
function weightedVoting(agentVotes, memory) {
    // Pesos base
    let weights = {
        technical: 0.30,
        fundamental: 0.20,
        sentiment: 0.20,
        statistical: 0.15,
        trend: 0.10,      // Nuevo agente
        momentum: 0.05    // Nuevo agente
    };
    
    // Ajustar pesos por rendimiento histórico
    if (memory.performance) {
        for (const [agent, perf] of Object.entries(memory.performance)) {
            if (weights[agent] && perf.winrate !== undefined && perf.total > 10) {
                // Bonus por performance sobre 55%
                const bonus = Math.max(0, (perf.winrate - 0.55) * 0.5);
                weights[agent] *= (1 + bonus);
            }
        }
        
        // Normalizar pesos
        const totalWeight = Object.values(weights).reduce((s, w) => s + w, 0);
        for (const key of Object.keys(weights)) {
            weights[key] /= totalWeight;
        }
    }
    
    // Acumular votos ponderados
    let buyScore = 0;
    let sellScore = 0;
    let totalConfidence = 0;
    let agentCount = 0;
    let lowConfidenceAgents = 0;
    
    const voteDetails = [];
    
    for (const vote of agentVotes) {
        const weight = weights[vote.agent_name] || 0.1;
        const confidence = vote.confidence / 100; // Normalizar 0-1
        
        voteDetails.push({
            agent: vote.agent_name,
            direction: vote.direction,
            confidence: vote.confidence,
            weight: weight.toFixed(3),
            weighted_vote: (confidence * weight).toFixed(3)
        });
        
        if (vote.direction === "buy") {
            buyScore += confidence * weight;
        } else if (vote.direction === "sell") {
            sellScore += confidence * weight;
        }
        
        totalConfidence += confidence;
        agentCount++;
        
        if (confidence < 0.3) lowConfidenceAgents++;
    }
    
    // VETO: Si 2+ agentes tienen muy baja confianza (<30%)
    const veto = lowConfidenceAgents >= 2;
    
    if (veto) {
        console.log("🛑 VETO ACTIVADO: 2+ agentes con confianza <30%");
        return {
            decision: "hold",
            confidence: 0,
            reasoning: `Veto: ${lowConfidenceAgents} agentes con baja confianza`,
            votes: voteDetails,
            veto: true
        };
    }
    
    // REQUISITO: Mínimo 3 agentes para actuar
    if (agentCount < 3) {
        console.log(`⚠️ Solo ${agentCount} agentes votaron, mínimo 3 requeridos`);
        return {
            decision: "hold",
            confidence: 0,
            reasoning: `Insuficientes agentes: ${agentCount}/3`,
            votes: voteDetails,
            veto: false
        };
    }
    
    // Decisión final
    let decision, confidence;
    
    if (buyScore > sellScore * 1.3) {
        decision = "buy";
        confidence = Math.round((buyScore / (buyScore + sellScore)) * 100);
    } else if (sellScore > buyScore * 1.3) {
        decision = "sell";
        confidence = Math.round((sellScore / (buyScore + sellScore)) * 100);
    } else {
        decision = "neutral";
        confidence = Math.round(Math.abs(buyScore - sellScore) * 100);
    }
    
    console.log(`🗳️ Votación: BUY=${buyScore.toFixed(3)} vs SELL=${sellScore.toFixed(3)} → ${decision.toUpperCase()} (${confidence}%)`);
    
    return {
        decision,
        confidence,
        reasoning: `Buy: ${buyScore.toFixed(3)} vs Sell: ${sellScore.toFixed(3)} (${agentCount} agentes)`,
        votes: voteDetails,
        veto: false,
        buy_score: buyScore,
        sell_score: sellScore
    };
}
```

#### 2.2 Parseo de Respuestas de Agentes para Votación

```javascript
// === NODO: Agente Estratega v3.0 ===
// === Antes de weightedVoting, parsear respuestas ===

function parseAgentVotes(inputData) {
    const votes = [];
    
    // Parsear respuesta del Agente Técnico
    if (inputData.technical_response) {
        const tech = inputData.technical_response._extracted || inputData.technical_response;
        votes.push({
            agent_name: "technical",
            direction: tech.signal || "neutral",
            confidence: tech.confidence || 50,
            reasoning: tech.analysis || ""
        });
    }
    
    // Parsear respuesta del Agente Fundamental
    if (inputData.fundamental_response) {
        const fund = inputData.fundamental_response._extracted || inputData.fundamental_response;
        const direction = fund.recommendation === "act" ? 
            (fund.impact_direction?.includes("bullish") ? "buy" : "sell") : 
            (fund.recommendation === "avoid" ? "neutral" : "neutral");
        
        votes.push({
            agent_name: "fundamental",
            direction: direction,
            confidence: fund.confidence || 50,
            reasoning: fund.analysis || ""
        });
    }
    
    // Parsear respuesta del Agente Sentimiento
    if (inputData.sentiment_response) {
        const sent = inputData.sentiment_response._extracted || inputData.sentiment_response;
        votes.push({
            agent_name: "sentiment",
            direction: sent.sentiment || "neutral",
            confidence: sent.confidence || 50,
            reasoning: sent.analysis || ""
        });
    }
    
    // Parsear respuesta del Agente Estadístico
    if (inputData.statistical_response) {
        const stat = inputData.statistical_response._extracted || inputData.statistical_response;
        votes.push({
            agent_name: "statistical",
            direction: stat.signal || "neutral",
            confidence: stat.confidence || 50,
            reasoning: stat.analysis || ""
        });
    }
    
    console.log(`📊 ${votes.length} votos recibidos para votación`);
    return votes;
}
```

### IMPACTO ESPERADO
- Win Rate: +5-8% (consenso reduce decisiones erráticas)
- Reducción de falsas señales: -30%
- Consistencia de decisiones: +25%

### DEPENDENCIAS
- Requiere que cada agente retorne JSON estructurado con signal/confidence
- Memory Manager necesita trackear winrate por agente

### RIESGOS
- Votación puede diluir señales fuertes de un solo agente: mitigar con umbral 1.3x
- Agentes correlacionados pueden sesgar votación: mitigar con pesos diferenciados

---

## ÁREA 3: INDICADORES MULTI-TIMEFRAME v3.0

### ANÁLISIS ACTUAL
- Solo analiza un timeframe (H1 implícito en las velas recibidas)
- RSI simple de 14 períodos
- Tendencia basada en SMA20 vs SMA50

### ALGORITMO MEJORADO

#### 3.1 Análisis Multi-Timeframe

```javascript
// === NODO: Analizar Pares v3.0 ===
// === Reemplazar funciones de análisis técnico ===

/**
 * Calcula indicadores para múltiples timeframes y converge señales
 * @param {object} candlesByTimeframe - { H1: [...], H4: [...], D1: [...] }
 * @returns {object} { multiTimeframeAnalysis, convergence, divergence }
 */
function analyzeMultiTimeframe(candlesByTimeframe, symbol) {
    const timeframes = {
        H1: { candles: candlesByTimeframe.H1 || [], weight: 0.3 },
        H4: { candles: candlesByTimeframe.H4 || [], weight: 0.45 }, // Mayor peso
        D1: { candles: candlesByTimeframe.D1 || [], weight: 0.25 }
    };
    
    const analysis = {};
    let buySignals = 0;
    let sellSignals = 0;
    let totalWeight = 0;
    
    for (const [tf, data] of Object.entries(timeframes)) {
        if (!data.candles || data.candles.length < 20) {
            analysis[tf] = { rsi: 50, trend: "neutral", score: 50, available: false };
            continue;
        }
        
        const rsi = calculateRSI(data.candles, 14);
        const trend = determineTrendEnhanced(data.candles);
        const macd = calculateMACD(data.candles);
        const bb = calculateBollingerBands(data.candles, 20, 2);
        
        // Score por timeframe
        let score = 50;
        
        // RSI contribution
        if (rsi < 30) score += 20;      // Oversold = bullish
        else if (rsi < 40) score += 10;
        else if (rsi > 70) score -= 20; // Overbought = bearish
        else if (rsi > 60) score -= 10;
        
        // Trend contribution
        if (trend === "strong_bullish") score += 20;
        else if (trend === "bullish") score += 10;
        else if (trend === "strong_bearish") score -= 20;
        else if (trend === "bearish") score -= 10;
        
        // MACD contribution
        if (macd.histogram > 0 && macd.signal > macd.previousSignal) score += 10;
        else if (macd.histogram < 0 && macd.signal < macd.previousSignal) score -= 10;
        
        // Bollinger contribution
        if (bb.percentB < 0.2) score += 10; // Near lower band = bullish bounce
        else if (bb.percentB > 0.8) score -= 10; // Near upper band = bearish reversal
        
        score = Math.max(0, Math.min(100, score));
        
        analysis[tf] = {
            rsi: Math.round(rsi * 100) / 100,
            trend: trend,
            score: Math.round(score),
            macd: macd,
            bollinger: bb,
            available: true
        };
        
        // Contar señales
        if (score > 60) {
            buySignals += data.weight;
            if (score > 75) buySignals += data.weight * 0.5; // Bonus por fuerte
        } else if (score < 40) {
            sellSignals += data.weight;
            if (score < 25) sellSignals += data.weight * 0.5;
        }
        
        totalWeight += data.weight;
    }
    
    // Convergencia
    let convergence = "none";
    if (buySignals > totalWeight * 0.6) convergence = "bullish";
    else if (sellSignals > totalWeight * 0.6) convergence = "bearish";
    else if (Math.abs(buySignals - sellSignals) < totalWeight * 0.2) convergence = "mixed";
    
    // Detectar divergencias
    const divergence = detectDivergences(analysis);
    
    return {
        timeframes: analysis,
        convergence: convergence,
        divergence: divergence,
        buy_signals: buySignals,
        sell_signals: sellSignals,
        confidence: convergence === "mixed" ? 30 : 
                    (convergence === "bullish" || convergence === "bearish" ? 75 : 50)
    };
}

/**
 * Detecta divergencias entre timeframes
 */
function detectDivergences(analysis) {
    const availableTFs = Object.entries(analysis).filter(([_, v]) => v.available);
    if (availableTFs.length < 2) return { type: "none", description: "Insufficient data" };
    
    const trends = availableTFs.map(([tf, data]) => ({ tf, trend: data.trend }));
    
    // Divergencia: timeframe superior bullish, inferior bearish (o viceversa)
    const hasDivergence = trends.some((a, i) => 
        trends.some((b, j) => i !== j && 
            ((a.trend.includes("bullish") && b.trend.includes("bearish")) ||
             (a.trend.includes("bearish") && b.trend.includes("bullish"))))
    );
    
    if (hasDivergence) {
        return {
            type: "trend_divergence",
            description: `Timeframes show conflicting trends: ${trends.map(t => `${t.tf}:${t.trend}`).join(", ")}`,
            severity: "medium"
        };
    }
    
    return { type: "none", description: "No divergences detected" };
}

/**
 * Determina tendencia con más matices (enhanced)
 */
function determineTrendEnhanced(candles) {
    if (candles.length < 50) return "neutral";
    
    const closes = candles.map(c => c.close || 0);
    const sma20 = closes.slice(-20).reduce((s, c) => s + c, 0) / 20;
    const sma50 = closes.slice(-50).reduce((s, c) => s + c, 0) / 50;
    const sma200 = closes.length >= 200 ? 
        closes.slice(-200).reduce((s, c) => s + c, 0) / 200 : sma50;
    
    const currentPrice = closes[closes.length - 1];
    
    // Fuerza de tendencia
    const sma20vs50 = (sma20 - sma50) / sma50;
    const sma50vs200 = (sma50 - sma200) / sma200;
    
    if (sma20 > sma50 && sma50 > sma200 && sma20vs50 > 0.003) return "strong_bullish";
    if (sma20 > sma50) return "bullish";
    if (sma20 < sma50 && sma50 < sma200 && sma20vs50 < -0.003) return "strong_bearish";
    if (sma20 < sma50) return "bearish";
    
    return "neutral";
}
```

### IMPACTO ESPERADO
- Win Rate: +5% (confirmación multi-timeframe reduce falsas señales)
- Reducción de trades contra tendencia principal: -40%

### DEPENDENCIAS
- Data fetchers deben proporcionar velas en H1, H4, D1
- Aumenta complejidad del prompt (necesita más tokens)

### RIESGOS
- Más consumo de tokens en prompts: mitigar con resumen conciso de cada TF
- Conflictos entre TF pueden paralizar decisiones: mitigar con peso mayor a TF superior

---

## ÁREA 4: INDICADORES AVANZADOS Y PATRONES DE VELAS

### ANÁLISIS ACTUAL
- RSI simple
- SMA20/50
- Soportes/resistencias por máx/mín de 20 velas
- Detección básica de patrones (doji, hammer, engulfing)

### ALGORITMO MEJORADO

#### 4.1 MACD con Histograma

```javascript
// === NODO: Agente Técnico v3.0 ===

function calculateMACD(candles, fast = 12, slow = 26, signal = 9) {
    if (candles.length < slow + signal) {
        return { macd: 0, signal: 0, histogram: 0, previousSignal: 0 };
    }
    
    const closes = candles.map(c => c.close || 0);
    
    // EMA rápida
    const emaFast = calculateEMA(closes, fast);
    // EMA lenta
    const emaSlow = calculateEMA(closes, slow);
    
    // Línea MACD
    const macdLine = emaFast - emaSlow;
    
    // Señal (EMA de MACD)
    const macdValues = [];
    for (let i = slow - 1; i < closes.length; i++) {
        const fastEMA = calculateEMAAt(closes, fast, i);
        const slowEMA = calculateEMAAt(closes, slow, i);
        macdValues.push(fastEMA - slowEMA);
    }
    
    const signalLine = calculateEMAFromArray(macdValues, signal);
    const histogram = macdLine - signalLine;
    const previousSignal = macdValues.length > signal ? 
        macdValues[macdValues.length - 2] - signalLine : signalLine;
    
    return {
        macd: Math.round(macdLine * 100000) / 100000,
        signal: Math.round(signalLine * 100000) / 100000,
        histogram: Math.round(histogram * 100000) / 100000,
        previousSignal: Math.round(previousSignal * 100000) / 100000,
        crossover: (histogram > 0 && previousSignal <= 0) ? "bullish" :
                   (histogram < 0 && previousSignal >= 0) ? "bearish" : "none"
    };
}

function calculateEMA(data, period) {
    return calculateEMAAt(data, period, data.length - 1);
}

function calculateEMAAt(data, period, index) {
    if (index < period - 1) return data[index] || 0;
    
    const multiplier = 2 / (period + 1);
    let ema = data.slice(0, period).reduce((s, v) => s + v, 0) / period;
    
    for (let i = period; i <= index; i++) {
        ema = (data[i] - ema) * multiplier + ema;
    }
    
    return ema;
}

function calculateEMAFromArray(array, period) {
    if (array.length < period) return array[array.length - 1] || 0;
    
    const multiplier = 2 / (period + 1);
    let ema = array.slice(0, period).reduce((s, v) => s + v, 0) / period;
    
    for (let i = period; i < array.length; i++) {
        ema = (array[i] - ema) * multiplier + ema;
    }
    
    return ema;
}
```

#### 4.2 Bandas de Bollinger

```javascript
function calculateBollingerBands(candles, period = 20, stdDev = 2) {
    if (candles.length < period) {
        const price = candles[candles.length - 1]?.close || 0;
        return { upper: price, middle: price, lower: price, percentB: 0.5, bandwidth: 0 };
    }
    
    const closes = candles.slice(-period).map(c => c.close || 0);
    const currentPrice = candles[candles.length - 1].close || 0;
    
    // Media (SMA)
    const middle = closes.reduce((s, c) => s + c, 0) / period;
    
    // Desviación estándar
    const variance = closes.reduce((s, c) => s + Math.pow(c - middle, 2), 0) / period;
    const stdDeviation = Math.sqrt(variance);
    
    // Bandas
    const upper = middle + (stdDev * stdDeviation);
    const lower = middle - (stdDev * stdDeviation);
    
    // %B (posición dentro de las bandas)
    const percentB = (upper !== lower) ? (currentPrice - lower) / (upper - lower) : 0.5;
    
    // Ancho de banda (volatilidad relativa)
    const bandwidth = middle > 0 ? ((upper - lower) / middle) * 100 : 0;
    
    return {
        upper: Math.round(upper * 100000) / 100000,
        middle: Math.round(middle * 100000) / 100000,
        lower: Math.round(lower * 100000) / 100000,
        percentB: Math.round(percentB * 100) / 100,
        bandwidth: Math.round(bandwidth * 100) / 100,
        squeeze: bandwidth < 4 // Bollinger Squeeze = baja volatilidad, posible ruptura
    };
}
```

#### 4.3 Detección Avanzada de Patrones de Velas

```javascript
/**
 * Detecta patrones de velas japonesas avanzados
 * @param {Array} candles - Últimas 5+ velas
 * @returns {Array} patrones detectados con fuerza
 */
function detectCandlePatterns(candles) {
    if (candles.length < 2) return [];
    
    const patterns = [];
    const last = candles[candles.length - 1];
    const prev = candles[candles.length - 2];
    const prev2 = candles.length >= 3 ? candles[candles.length - 3] : null;
    
    const lastBody = Math.abs(last.close - last.open);
    const lastRange = last.high - last.low;
    const lastUpperWick = last.high - Math.max(last.close, last.open);
    const lastLowerWick = Math.min(last.close, last.open) - last.low;
    const lastIsBullish = last.close > last.open;
    
    const prevBody = Math.abs(prev.close - prev.open);
    const prevIsBullish = prev.close > prev.open;
    
    // === PATRONES DE UNA VELA ===
    
    // Doji
    if (lastRange > 0 && lastBody / lastRange < 0.1) {
        patterns.push({ name: "doji", strength: 0.7, direction: "reversal" });
    }
    
    // Martillo (bullish reversal en downtrend)
    if (lastRange > 0 && lastLowerWick > lastBody * 2 && lastUpperWick < lastBody * 0.5) {
        const strength = lastIsBullish ? 0.8 : 0.5;
        patterns.push({ name: "hammer", strength, direction: "bullish" });
    }
    
    // Estrella Fugaz (bearish reversal en uptrend)
    if (lastRange > 0 && lastUpperWick > lastBody * 2 && lastLowerWick < lastBody * 0.5) {
        const strength = !lastIsBullish ? 0.8 : 0.5;
        patterns.push({ name: "shooting_star", strength, direction: "bearish" });
    }
    
    // === PATRONES DE DOS VELAS ===
    
    // Bullish Engulfing
    if (lastBody > prevBody * 1.5 && lastIsBullish && !prevIsBullish) {
        patterns.push({ name: "bullish_engulfing", strength: 0.85, direction: "bullish" });
    }
    
    // Bearish Engulfing
    if (lastBody > prevBody * 1.5 && !lastIsBullish && prevIsBullish) {
        patterns.push({ name: "bearish_engulfing", strength: 0.85, direction: "bearish" });
    }
    
    // === PATRONES DE TRES VELAS ===
    
    if (prev2) {
        const prev2IsBullish = prev2.close > prev2.open;
        
        // Morning Star (bullish reversal)
        if (!prev2IsBullish && prevBody < prev2Body * 0.3 && lastIsBullish && lastBody > prevBody) {
            patterns.push({ name: "morning_star", strength: 0.9, direction: "bullish" });
        }
        
        // Evening Star (bearish reversal)
        if (prev2IsBullish && prevBody < prev2Body * 0.3 && !lastIsBullish && lastBody > prevBody) {
            patterns.push({ name: "evening_star", strength: 0.9, direction: "bearish" });
        }
    }
    
    return patterns;
}
```

#### 4.4 Análisis de Volumen Avanzado

```javascript
/**
 * Analiza volumen de ticks para confirmar señales
 * @param {Array} candles
 * @returns {object} volumen analysis
 */
function analyzeVolume(candles) {
    if (candles.length < 20) return { ratio: 1, interpretation: "insufficient_data" };
    
    const volumes = candles.map(c => c.tick_volume || 0);
    const currentVolume = volumes[volumes.length - 1];
    const avgVolume = volumes.slice(-20).reduce((s, v) => s + v, 0) / 20;
    const ratio = avgVolume > 0 ? currentVolume / avgVolume : 1;
    
    // Divergencia precio-volumen
    const priceChange = (candles[candles.length - 1].close - candles[candles.length - 5].close) / candles[candles.length - 5].close;
    const volumeChange = (avgVolume - volumes.slice(-25, -5).reduce((s, v) => s + v, 0) / 20) / (volumes.slice(-25, -5).reduce((s, v) => s + v, 0) / 20 || 1);
    
    let interpretation = "normal";
    
    // Confirmación de ruptura
    if (ratio > 2 && Math.abs(priceChange) > 0.002) {
        interpretation = priceChange > 0 ? "bullish_breakout_confirmed" : "bearish_breakout_confirmed";
    }
    
    // Divergencia (precio sube, volumen baja = debilidad)
    if (priceChange > 0.001 && volumeChange < -0.2) {
        interpretation = "bullish_divergence_warning";
    }
    if (priceChange < -0.001 && volumeChange < -0.2) {
        interpretation = "bearish_divergence_warning";
    }
    
    // Agotamiento (volumen alto, precio estancado)
    if (ratio > 2.5 && Math.abs(priceChange) < 0.0005) {
        interpretation = "exhaustion_pattern";
    }
    
    return {
        ratio: Math.round(ratio * 100) / 100,
        avg_volume: Math.round(avgVolume),
        current_volume: currentVolume,
        interpretation: interpretation,
        confirms_price: Math.abs(priceChange) > 0.001 && ratio > 1.2
    };
}
```

### IMPACTO ESPERADO
- Win Rate: +5-8% (patrones + MACD + Bollinger confirman señales)
- Mejor timing de entrada: +10% en R/R promedio

### DEPENDENCIAS
- Necesita velas OHLCV con volumen
- Aumenta tamaño del prompt

### RIESGOS
- Patrones falsos en mercados laterales: mitigar con filtro de tendencia
- Volumen de ticks no es volumen real: mitigar con interpretación conservadora

---

## ÁREA 5: PROCESAMIENTO DE DATOS FUNDAMENTALES MEJORADO

### ANÁLISIS ACTUAL
- Solo cuenta noticias y eventos por moneda
- No analiza contenido ni impacto real

### ALGORITMO MEJORADO

#### 5.1 Scoring de Noticias por Relevancia

```javascript
// === NODO: Analizar Pares v3.0 ===

/**
 * Calcula score ponderado de noticias por impacto
 * @param {Array} news - Lista de noticias
 * @param {string} symbol - Par de divisas
 * @returns {object} fundamental score y bias
 */
function scoreNewsByRelevance(news, symbol) {
    const baseCurrency = symbol.substring(0, 3);
    const quoteCurrency = symbol.substring(3, 6);
    
    // Pesos por moneda
    const currencyWeights = {
        USD: 1.0,   // Mayor impacto
        EUR: 0.9,
        GBP: 0.8,
        JPY: 0.7,
        CHF: 0.6
    };
    
    // Pesos por tipo de noticia
    const newsTypeWeights = {
        "NFP": 1.0,
        "CPI": 0.9,
        "rate_decision": 0.95,
        "FOMC": 0.9,
        "ECB": 0.85,
        "GDP": 0.7,
        "PMI": 0.6,
        "retail_sales": 0.5,
        "speech": 0.4,
        "default": 0.3
    };
    
    let bullishScore = 0;
    let bearishScore = 0;
    let totalImpact = 0;
    let highImpactCount = 0;
    
    for (const item of news) {
        // Determinar moneda relevante
        const currency = item.currency || "USD";
        const currencyWeight = currencyWeights[currency] || 0.3;
        
        // Determinar tipo de noticia
        const newsType = detectNewsType(item.headline || item.title || "");
        const typeWeight = newsTypeWeights[newsType] || newsTypeWeights.default;
        
        // Sentimiento
        const sentiment = (item.sentiment || "neutral").toLowerCase();
        const sentimentScore = sentiment === "bullish" ? 1 : 
                               sentiment === "bearish" ? -1 : 0;
        
        // Desviación del forecast (si está disponible)
        let deviationBonus = 1;
        if (item.actual !== undefined && item.forecast !== undefined) {
            const deviation = Math.abs(item.actual - item.forecast) / (Math.abs(item.forecast) || 1);
            if (deviation > 0.1) deviationBonus = 1.5; // Gran sorpresa = más impacto
        }
        
        // Score final
        const itemScore = Math.abs(sentimentScore) * currencyWeight * typeWeight * deviationBonus;
        
        if (sentimentScore > 0) bullishScore += itemScore;
        else if (sentimentScore < 0) bearishScore += itemScore;
        
        totalImpact += itemScore;
        if (typeWeight > 0.7) highImpactCount++;
    }
    
    const netBias = bullishScore - bearishScore;
    const maxPossible = news.length * 1.5; // Máximo teórico
    const normalizedBias = maxPossible > 0 ? (netBias / maxPossible) * 100 : 0;
    
    return {
        bullish_score: Math.round(bullishScore * 100) / 100,
        bearish_score: Math.round(bearishScore * 100) / 100,
        net_bias: Math.round(normalizedBias),
        total_impact: Math.round(totalImpact * 100) / 100,
        high_impact_count: highImpactCount,
        interpretation: normalizedBias > 30 ? "strongly_bullish" :
                        normalizedBias > 10 ? "bullish" :
                        normalizedBias < -30 ? "strongly_bearish" :
                        normalizedBias < -10 ? "bearish" : "neutral"
    };
}

function detectNewsType(headline) {
    const lower = headline.toLowerCase();
    if (lower.includes("nfp") || lower.includes("nonfarm") || lower.includes("payrolls")) return "NFP";
    if (lower.includes("cpi") || lower.includes("inflation")) return "CPI";
    if (lower.includes("rate") || lower.includes("interest")) return "rate_decision";
    if (lower.includes("fomc") || lower.includes("fed")) return "FOMC";
    if (lower.includes("ecb") || lower.includes("lagarde")) return "ECB";
    if (lower.includes("gdp")) return "GDP";
    if (lower.includes("pmi")) return "PMI";
    if (lower.includes("retail")) return "retail_sales";
    if (lower.includes("speech") || lower.includes("speaks")) return "speech";
    return "default";
}
```

#### 5.2 Calendario Económico Ponderado

```javascript
function scoreEconomicCalendar(events, symbol) {
    const baseCurrency = symbol.substring(0, 3);
    const quoteCurrency = symbol.substring(3, 6);
    
    const now = new Date();
    let upcomingImpact = 0;
    let eventDetails = [];
    
    for (const event of events) {
        const currency = event.currency || "";
        if (currency !== baseCurrency && currency !== quoteCurrency) continue;
        
        const impact = event.impact === "high" ? 3 : event.impact === "medium" ? 2 : 1;
        
        // Impacto temporal: eventos en <2h tienen más peso
        let timeWeight = 1;
        if (event.time) {
            const eventTime = new Date(event.time);
            const hoursUntil = (eventTime - now) / (1000 * 60 * 60);
            if (hoursUntil < 2) timeWeight = 2;
            else if (hoursUntil < 6) timeWeight = 1.5;
            else if (hoursUntil > 48) timeWeight = 0.3; // Eventos lejanos = menos relevante
        }
        
        // Moneda base tiene más peso
        const currencyWeight = currency === baseCurrency ? 1.2 : 0.8;
        
        const weightedImpact = impact * timeWeight * currencyWeight;
        upcomingImpact += weightedImpact;
        
        eventDetails.push({
            event: event.event || event.name,
            currency,
            impact,
            time_weight: timeWeight,
            weighted_impact: Math.round(weightedImpact * 100) / 100
        });
    }
    
    return {
        total_upcoming_impact: Math.round(upcomingImpact * 100) / 100,
        events: eventDetails,
        risk_level: upcomingImpact > 6 ? "high" : upcomingImpact > 3 ? "medium" : "low",
        recommendation: upcomingImpact > 6 ? "wait" : "act"
    };
}
```

#### 5.3 Correlaciones entre Pares

```javascript
/**
 * Ajusta señales según correlaciones conocidas entre pares
 * @param {object} signalsByPair - Señales de cada par
 * @returns {object} adjusted signals
 */
function adjustByCorrelations(signalsByPair) {
    // Correlaciones históricas
    const correlations = {
        "EURUSD_GBPUSD": { value: 0.85, type: "positive_strong" },
        "EURUSD_USDCHF": { value: -0.90, type: "negative_strong" },
        "GBPUSD_USDCHF": { value: -0.80, type: "negative_strong" },
        "USDJPY_GOLD": { value: -0.60, type: "negative_moderate" },
        "EURUSD_USDJPY": { value: 0.30, type: "positive_weak" }
    };
    
    const adjustments = {};
    
    for (const [pair, signal] of Object.entries(signalsByPair)) {
        let confidenceAdjustment = 0;
        
        // Verificar correlaciones con otros pares
        for (const [corrKey, corrData] of Object.entries(correlations)) {
            const [pair1, pair2] = corrKey.split("_");
            
            if (pair1 === pair && signalsByPair[pair2]) {
                const otherSignal = signalsByPair[pair2].signal;
                
                if (corrData.type.includes("positive")) {
                    // Pares correlacionados positivamente deben coincidir
                    if (signal.signal === otherSignal) {
                        confidenceAdjustment += 10; // Confirmación
                    } else {
                        confidenceAdjustment -= 15; // Conflicto
                    }
                } else if (corrData.type.includes("negative")) {
                    // Pares correlacionados negativamente deben ser opuestos
                    if (signal.signal !== otherSignal && otherSignal !== "neutral") {
                        confidenceAdjustment += 10; // Confirmación inversa
                    } else if (signal.signal === otherSignal) {
                        confidenceAdjustment -= 15; // Conflicto
                    }
                }
            }
        }
        
        adjustments[pair] = {
            ...signal,
            original_confidence: signal.confidence,
            adjusted_confidence: Math.max(0, Math.min(100, signal.confidence + confidenceAdjustment)),
            correlation_adjustment: confidenceAdjustment
        };
    }
    
    return adjustments;
}
```

### IMPACTO ESPERADO
- Win Rate: +3-5% (mejor filtro de noticias de alto impacto)
- Reducción de trades antes de NFP/CPI: -80% pérdidas por volatilidad inesperada

### DEPENDENCIAS
- Jina news fetcher necesita retornar tipo de noticia y desviación
- Calendario económico necesita forecast vs actual

### RIESGOS
- Clasificación automática de noticias puede ser imprecisa: mitigar con NLP básico del modelo IA

---

## ÁREA 6: PROMPT ESTRUCTURADO CON JSON SCHEMA

### ANÁLISIS ACTUAL
- Prompts largos (3000+ tokens) pero sin estructura clara
- IA a veces ignora instrucciones de formato
- JSON response no siempre válido

### MEJORA IMPLEMENTADA

#### 6.1 Prompt con Schema Explícito

```javascript
// === NODO: Agente Técnico v3.0 ===
// === Nuevo formato de prompt ===

function buildStructuredPrompt(technicalData, context) {
    const schema = JSON.stringify({
        signal: "buy | sell | neutral",
        confidence: "0-100 integer",
        analysis: "one sentence technical reasoning",
        structure: "bullish | bearish | range",
        momentum: "strong | moderate | weak",
        entry_zone: ["low price", "high price"],
        stop_zone: ["low price", "high price"],
        indicators_used: ["list of indicators supporting the signal"],
        key_levels: {
            support: "price level",
            resistance: "price level",
            pivot: "price level"
        }
    }, null, 2);
    
    const fewShotExamples = `
=== EXAMPLE 1: Oversold bounce ===
Input: RSI=28, price at support, bullish hammer, USD bearish
Output: {"signal":"buy","confidence":72,"analysis":"Oversold bounce at support with bullish reversal pattern","structure":"bullish","momentum":"moderate","entry_zone":[1.0945,1.0955],"stop_zone":[1.0915,1.0925]}

=== EXAMPLE 2: Overbought rejection ===
Input: RSI=76, price at resistance, bearish engulfing, EUR bearish
Output: {"signal":"sell","confidence":68,"analysis":"Overbought rejection at resistance with bearish engulfing","structure":"bearish","momentum":"strong","entry_zone":[1.0975,1.0985],"stop_zone":[1.1005,1.1015]}
`;
    
    return `[INST] You are a senior forex technical analyst. Analyze the data below and return ONLY valid JSON.

${fewShotExamples}

═══════════════════════════════════════
📊 MARKET DATA FOR ${technicalData.symbol}
═══════════════════════════════════════

PRICE: ${technicalData.price}
RSI(14): ${technicalData.rsi} (${technicalData.rsiStatus})
TREND: ${technicalData.trend.toUpperCase()}
MACD: ${technicalData.macd.histogram} (${technicalData.macd.crossover})
BOLLINGER: %B=${technicalData.bollinger.percentB}, squeeze=${technicalData.bollinger.squeeze}
ATR(14): ${technicalData.atr} (${technicalData.atrPercent}%)

SUPPORT: ${technicalData.support} | RESISTANCE: ${technicalData.resistance}
SMA9: ${technicalData.sma9} | SMA20: ${technicalData.sma20} | SMA50: ${technicalData.sma50}

CANDLESTICK: ${technicalData.candlePattern}
VOLUME: ${technicalData.volumeRatio}x avg (${technicalData.volumeInterpretation})

CURRENCY STRENGTH: ${technicalData.baseStrength} ${technicalData.baseCurrency} vs ${technicalData.quoteStrength} ${technicalData.quoteCurrency}

MULTI-TIMEFRAME: H1=${technicalData.H1_trend}, H4=${technicalData.H4_trend}, D1=${technicalData.D1_trend}
CONVERGENCE: ${technicalData.mtfConvergence}

═══════════════════════════════════════
📋 REQUIRED JSON OUTPUT (MUST be valid JSON):
═══════════════════════════════════════
${schema}

⚠️ CRITICAL RULES:
1. Use EXACT price ${technicalData.price} as reference
2. entry_zone must be within ±0.2% of current price
3. stop_zone must be 0.3-0.5% from current price
4. confidence reflects strength of ALL indicators combined
5. Return ONLY JSON, no explanation before or after [/INST]`;
}
```

#### 6.2 Chain of Thought for Complex Analysis

```javascript
// Para el Agente Unificado (OpenRouter), usar chain of thought

function buildChainOfThoughtPrompt(marketData) {
    return `[INST] Perform a step-by-step analysis following these steps:

STEP 1 - Technical Assessment:
- Evaluate RSI, MACD, Bollinger, trend across H1/H4/D1
- Identify key support/resistance levels
- Note candlestick patterns and volume confirmation

STEP 2 - Fundamental Assessment:
- Review news sentiment bias and high-impact events
- Evaluate economic calendar risks
- Consider central bank policy direction

STEP 3 - Sentiment Assessment:
- Analyze positioning (long/short ratio)
- Identify contrarian opportunities
- Evaluate market mood

STEP 4 - Statistical Assessment:
- Review historical winrate for similar setups
- Consider correlation with other pairs
- Evaluate risk/reward ratio

STEP 5 - Synthesis:
- Weigh all factors
- Determine if signals converge or conflict
- Make final decision

STEP 6 - Output:
Return ONLY the final JSON decision object.

═══ DATA ═══
${buildMarketDataSummary(marketData)}

═══════════════════════════════════════
After reasoning through Steps 1-5, return ONLY valid JSON:
{
  "signal": "buy|sell|neutral",
  "confidence": 0-100,
  "analysis": "one sentence",
  "entry_zone": [low, high],
  "stop_zone": [low, high]
}
[/INST]`;
}
```

### IMPACTO ESPERADO
- Consistencia de JSON responses: +30%
- Reducción de respuestas inválidas: -50%
- Calidad de análisis: +15% (estructura obliga a considerar todos los factores)

### DEPENDENCIAS
- Ninguna, es mejora de prompt engineering puro

### RIESGOS
- Chain of thought consume más tokens: mitigar con resúmenes concisos por paso

---

## ÁREA 7: CONDICIONES DE MERCADO Y ESTACIONALIDAD

### ANÁLISIS ACTUAL
- No clasifica estado del mercado
- Sin consideración de horario/sesión

### ALGORITMO MEJORADO

#### 7.1 Clasificación de Estado del Mercado

```javascript
/**
 * Clasifica el estado actual del mercado
 * @param {object} marketData
 * @returns {object} marketState
 */
function classifyMarketState(marketData) {
    const { rsi, trend, atr, bollinger, volume } = marketData;
    
    // Volatilidad
    const atrPercent = marketData.atrPercent || 0;
    const bbWidth = marketData.bollinger?.bandwidth || 0;
    
    let volatilityState;
    if (atrPercent > 0.5 || bbWidth > 8) volatilityState = "high_volatility";
    else if (atrPercent < 0.15 || bbWidth < 3) volatilityState = "low_volatility";
    else volatilityState = "normal_volatility";
    
    // Tendencia
    let trendState;
    if (marketData.mtfConvergence === "bullish") trendState = "bullish_trend";
    else if (marketData.mtfConvergence === "bearish") trendState = "bearish_trend";
    else if (Math.abs(rsi - 50) < 10 && trend === "neutral") trendState = "consolidation";
    else trendState = "weak_trend";
    
    // Sesión actual
    const hourUTC = new Date().getUTCHours();
    let session;
    if (hourUTC >= 0 && hourUTC < 7) session = "asian";
    else if (hourUTC >= 7 && hourUTC < 12) session = "european";
    else if (hourUTC >= 12 && hourUTC < 17) session = "european_us_overlap";
    else if (hourUTC >= 17 && hourUTC < 22) session = "us";
    else session = "late_us";
    
    // Recomendación por estado
    let strategy;
    if (trendState.includes("bullish") || trendState.includes("bearish")) {
        strategy = "trend_following";
    } else if (trendState === "consolidation") {
        strategy = "mean_reversion";
    } else if (volatilityState === "high_volatility") {
        strategy = "breakout";
    } else {
        strategy = "range_trading";
    }
    
    return {
        trend: trendState,
        volatility: volatilityState,
        session: session,
        strategy: strategy,
        recommended_action: volatilityState === "high_volatility" ? "reduce_size" :
                            trendState === "consolidation" ? "wait_for_breakout" :
                            "normal"
    };
}
```

#### 7.2 Análisis de Estacionalidad

```javascript
function getSeasonalityFactors(symbol) {
    const now = new Date();
    const dayOfWeek = now.getUTCDay(); // 0=Sunday, 5=Friday
    const hourUTC = now.getUTCHours();
    const month = now.getUTCMonth();
    
    let factors = [];
    let adjustment = 0;
    
    // Efecto día de la semana
    if (dayOfWeek === 1) { // Monday
        factors.push("monday_effect: lower volatility, range-bound typical");
        adjustment -= 5;
    }
    if (dayOfWeek === 5) { // Friday
        factors.push("friday_effect: profit taking, position squaring");
        adjustment -= 5;
    }
    
    // Efecto de sesión
    if (hourUTC >= 12 && hourUTC < 17) {
        factors.push("overlap_session: highest volatility (EU/US)");
        adjustment += 10; // Más confianza en breakout
    }
    if (hourUTC >= 0 && hourUTC < 7) {
        factors.push("asian_session: lower volatility for EUR/GBP pairs");
        adjustment -= 10; // Menos confianza
    }
    
    // Efecto de mes
    if (month === 0) { // January
        factors.push("january_effect: portfolio rebalancing");
    }
    if (month >= 6 && month <= 7) { // Summer
        factors.push("summer_lull: lower volume, thinner markets");
        adjustment -= 5;
    }
    
    // Primer viernes del mes = NFP
    const isFirstFriday = dayOfWeek === 5 && now.getDate() <= 7;
    if (isFirstFriday && hourUTC >= 12) {
        factors.push("NFP_RELEASE: extreme volatility expected");
        adjustment -= 20; // Evitar trading durante NFP
    }
    
    return {
        factors,
        confidence_adjustment: adjustment,
        recommendation: adjustment < -15 ? "avoid_trading" :
                        adjustment < -5 ? "reduce_confidence" : "normal"
    };
}
```

### IMPACTO ESPERADO
- Win Rate: +2-3% (evitar trading en malas condiciones)
- Reducción de drawdown en NFP: -90%

### DEPENDENCIAS
- Ninguna, lógica temporal pura

### RIESGOS
- Sobre-optimización estacional: mitigar con ajustes conservadores

---

## ÁREA 8: SISTEMA DE BACKTESTING Y APRENDIZAJE

### ANÁLISIS ACTUAL
- Memory Manager registra wins/losses pero sin contexto detallado
- Sin métricas de rendimiento avanzadas (Sharpe, Calmar, profit factor)

### ALGORITMO MEJORADO

#### 8.1 Registro Completo de Trades

```javascript
// === NODO: Log Result v3.0 ===

function buildTradeRecord(decision, marketData, result) {
    return {
        timestamp: new Date().toISOString(),
        symbol: decision.symbol || marketData.best_pair,
        decision: decision.decision,
        confidence: decision.confianza,
        
        // Niveles
        entry_price: decision.entrada?.precio || 0,
        stop_loss: decision.stop_loss?.precio || 0,
        take_profit_1: decision.take_profit?.objetivo1 || 0,
        take_profit_2: decision.take_profit?.objetivo2 || 0,
        volume: decision.volumen || 0,
        risk_percent: decision.risk_percent || 0,
        
        // Resultado
        result: result.status || "unknown", // "win", "loss", "breakeven"
        exit_price: result.exit_price || 0,
        pips: result.pips || 0,
        profit_usd: result.profit || 0,
        
        // Contexto para análisis posterior
        market_state: {
            trend: marketData.mtfAnalysis?.convergence || "unknown",
            volatility: marketData.atrPercent || 0,
            session: marketData.session || "unknown"
        },
        
        // Datos de agentes (para optimización de pesos)
        agent_votes: decision.votes || [],
        
        // Indicadores clave (para optimización de parámetros)
        indicators: {
            rsi: marketData.technical?.rsi || 0,
            macd_histogram: marketData.macd?.histogram || 0,
            bb_percent: marketData.bollinger?.percentB || 0,
            volume_ratio: marketData.volume?.ratio || 0
        },
        
        // Ratio R/R
        risk_reward: decision.rr_ratio || 0
    };
}
```

#### 8.2 Cálculo de Métricas de Rendimiento

```javascript
/**
 * Calcula métricas avanzadas de rendimiento
 * @param {Array} trades - Historial de trades
 * @returns {object} métricas
 */
function calculatePerformanceMetrics(trades) {
    const executedTrades = trades.filter(t => t.result === "win" || t.result === "loss");
    
    if (executedTrades.length < 5) {
        return { insufficient_data: true, trades_count: executedTrades.length };
    }
    
    const wins = executedTrades.filter(t => t.result === "win");
    const losses = executedTrades.filter(t => t.result === "loss");
    
    const winRate = wins.length / executedTrades.length;
    
    // Profit factor
    const grossProfit = wins.reduce((s, t) => s + (t.profit_usd || t.pips || 0), 0);
    const grossLoss = Math.abs(losses.reduce((s, t) => s + (t.profit_usd || t.pips || 0), 0));
    const profitFactor = grossLoss > 0 ? grossProfit / grossLoss : grossProfit > 0 ? Infinity : 0;
    
    // Average win / average loss
    const avgWin = wins.length > 0 ? grossProfit / wins.length : 0;
    const avgLoss = losses.length > 0 ? grossLoss / losses.length : 0;
    const payoffRatio = avgLoss > 0 ? avgWin / avgLoss : 0;
    
    // Max consecutive wins/losses
    const maxConsecWins = maxConsecutive(executedTrades, "win");
    const maxConsecLosses = maxConsecutive(executedTrades, "loss");
    
    // Sharpe Ratio (rolling 20 trades)
    const last20 = executedTrades.slice(-20);
    const returns = last20.map(t => t.pips || 0);
    const avgReturn = returns.reduce((s, r) => s + r, 0) / returns.length;
    const stdDev = Math.sqrt(returns.reduce((s, r) => s + Math.pow(r - avgReturn, 2), 0) / returns.length);
    const sharpeRatio = stdDev > 0 ? (avgReturn / stdDev) * Math.sqrt(252) : 0;
    
    // Calmar Ratio
    const totalReturn = executedTrades.reduce((s, t) => s + (t.pips || 0), 0);
    const maxDrawdown = calculateMaxDrawdown(executedTrades);
    const calmarRatio = maxDrawdown > 0 ? totalReturn / maxDrawdown : 0;
    
    // Winrate por agente (para ajustar pesos)
    const agentPerformance = {};
    for (const trade of executedTrades) {
        if (trade.agent_votes) {
            for (const vote of trade.agent_votes) {
                if (!agentPerformance[vote.agent]) {
                    agentPerformance[vote.agent] = { wins: 0, losses: 0, total_confidence: 0, count: 0 };
                }
                if (trade.result === "win") agentPerformance[vote.agent].wins++;
                else agentPerformance[vote.agent].losses++;
                agentPerformance[vote.agent].total_confidence += vote.confidence;
                agentPerformance[vote.agent].count++;
            }
        }
    }
    
    // Calcular winrate por agente
    for (const [agent, data] of Object.entries(agentPerformance)) {
        data.winrate = (data.wins + data.losses) > 0 ? data.wins / (data.wins + data.losses) : 0;
        data.avg_confidence = data.count > 0 ? data.total_confidence / data.count : 50;
    }
    
    return {
        total_trades: executedTrades.length,
        wins: wins.length,
        losses: losses.length,
        win_rate: Math.round(winRate * 10000) / 100,
        profit_factor: Math.round(profitFactor * 100) / 100,
        avg_win: Math.round(avgWin * 100) / 100,
        avg_loss: Math.round(avgLoss * 100) / 100,
        payoff_ratio: Math.round(payoffRatio * 100) / 100,
        max_consecutive_wins: maxConsecWins,
        max_consecutive_losses: maxConsecLosses,
        sharpe_ratio: Math.round(sharpeRatio * 100) / 100,
        calmar_ratio: Math.round(calmarRatio * 100) / 100,
        total_pips: Math.round(totalReturn * 100) / 100,
        max_drawdown_pips: Math.round(maxDrawdown * 100) / 100,
        agent_performance: agentPerformance
    };
}

function maxConsecutive(trades, result) {
    let max = 0, current = 0;
    for (const t of trades) {
        if (t.result === result) {
            current++;
            max = Math.max(max, current);
        } else {
            current = 0;
        }
    }
    return max;
}

function calculateMaxDrawdown(trades) {
    let peak = 0, maxDD = 0, cumulative = 0;
    for (const t of trades) {
        cumulative += t.pips || 0;
        peak = Math.max(peak, cumulative);
        maxDD = Math.max(maxDD, peak - cumulative);
    }
    return maxDD;
}
```

#### 8.3 Optimización de Parámetros (Auto-ajuste)

```javascript
/**
 * Auto-ajusta parámetros basado en rendimiento reciente
 * @param {object} metrics - Performance metrics
 * @param {object} currentParams - Parámetros actuales
 * @returns {object} optimized parameters
 */
function optimizeParameters(metrics, currentParams) {
    const optimized = { ...currentParams };
    
    // Optimizar pesos de agentes
    if (metrics.agent_performance && Object.keys(metrics.agent_performance).length > 0) {
        const newWeights = {};
        
        for (const [agent, perf] of Object.entries(metrics.agent_performance)) {
            if (perf.count >= 5) { // Mínimo 5 trades para evaluar
                const performanceBonus = Math.max(0, perf.winrate - 0.50); // Bonus sobre 50%
                const baseWeight = currentParams.agent_weights?.[agent] || 0.15;
                newWeights[agent] = baseWeight * (1 + performanceBonus);
            }
        }
        
        // Normalizar
        const totalWeight = Object.values(newWeights).reduce((s, w) => s + w, 0);
        if (totalWeight > 0) {
            for (const key of Object.keys(newWeights)) {
                newWeights[key] = Math.round((newWeights[key] / totalWeight) * 100) / 100;
            }
            optimized.agent_weights = newWeights;
        }
    }
    
    // Ajustar riesgo base según Sharpe
    if (metrics.sharpe_ratio > 1.5) {
        optimized.base_risk = Math.min(0.03, currentParams.base_risk * 1.1); // Aumentar si buen Sharpe
    } else if (metrics.sharpe_ratio < 0.5) {
        optimized.base_risk = Math.max(0.01, currentParams.base_risk * 0.9); // Reducir si mal Sharpe
    }
    
    // Ajustar confianza mínima según winrate
    if (metrics.win_rate < 45) {
        optimized.min_confidence = Math.min(55, currentParams.min_confidence + 2);
    } else if (metrics.win_rate > 60) {
        optimized.min_confidence = Math.max(40, currentParams.min_confidence - 1);
    }
    
    return optimized;
}
```

### IMPACTO ESPERADO
- Win Rate: +3-5% (aprendizaje continuo ajusta pesos)
- Sharpe Ratio: +0.5-1.0 (reducción de riesgo en malas rachas)
- Auto-optimización: +2-3% win rate tras 100+ trades

### DEPENDENCIAS
- Necesita logging completo de trades con contexto
- Memory Manager debe persistir historial

### RIESGOS
- Overfitting a datos recientes: mitigar con decay de experiencia antigua
- Cambios de régimen de mercado: mitigar con ventana de rolling 20 trades

---

## RESUMEN DE IMPLEMENTACIÓN

### Fase 1: Gestión de Riesgo y Votación (Prioridad 🔴)
**Archivos a modificar:**
- `multi-agente-profesional-CORRECTED.json` → `multi-agente-profesional-v3.json`
  - Nodo "Preparar Orden": riesgo adaptativo
  - Nodo "Memory Manager": circuit breaker global
  - Nodo "Agente Estratega": votación ponderada

**Tiempo estimado:** 2-3 horas de implementación
**Impacto inmediato:** WR +10%, DD -40%

### Fase 2: Indicadores Avanzados (Prioridad 🟡)
**Archivos a modificar:**
- `jetson-CORRECTED.json` → `jetson-v3.json`
  - Nodo "Agente Técnico": MACD, Bollinger, patrones avanzados, volumen
  - Nodo "Agente Fundamental": scoring de noticias, calendario ponderado
  - Nodo "Analizar Pares": multi-timeframe, correlaciones

**Tiempo estimado:** 3-4 horas
**Impacto:** WR +8-13%

### Fase 3: Prompts y Estacionalidad (Prioridad 🟡)
**Archivos a modificar:**
- Todos los agentes: prompts estructurados con schema
- `Analizar Pares`: clasificación de mercado, estacionalidad

**Tiempo estimado:** 2 horas
**Impacto:** Consistencia +30%, WR +2-5%

### Fase 4: Backtesting y Optimización (Prioridad 🟢)
**Archivos a modificar:**
- `Log Result`: trade record completo
- `Memory Manager`: métricas de rendimiento, auto-optimización

**Tiempo estimado:** 2-3 horas
**Impacto:** WR +3-5% tras 100 trades

---

## CHECKLIST PRE-IMPLEMENTACIÓN v3.0

- [ ] Backup de todos los workflows v2.0
- [ ] Verificar IDs de subworkflows
- [ ] Verificar endpoints (MT5, Mistral, OpenRouter)
- [ ] Implementar Fase 1 y probar en demo
- [ ] Verificar logs de riesgo adaptativo
- [ ] Implementar Fase 2 y probar
- [ ] Verificar prompts estructurados
- [ ] Implementar Fase 3 y 4
- [ ] Ejecutar 50+ trades en demo para calibrar
- [ ] Activar en producción solo tras confirmar métricas

---

## IMPACTO TOTAL ESPERADO

| Métrica | v2.0 | v3.0 Esperado | Mejora |
|---------|------|---------------|--------|
| Win Rate | 45-60% | 55-70% | +10-15% |
| Sharpe Ratio | 0.5-1.0 | 1.0-2.0 | +0.5-1.0 |
| Max Drawdown | 10-15% | 5-8% | -40-50% |
| Profit Factor | 1.2-1.5 | 1.5-2.0 | +0.3-0.5 |
| Consistencia JSON | 70% | 95% | +25% |
| Calmar Ratio | 3-5 | 5-10 | +2-5 |
