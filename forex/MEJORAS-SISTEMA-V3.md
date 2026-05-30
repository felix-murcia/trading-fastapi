# MEJORAS DEL SISTEMA DE TRADING FOREX - v3.0

## RESUMEN EJECUTIVO

Este documento describe las mejoras **algorítmicas y de arquitectura** para elevar la calidad de las decisiones del sistema de trading. El sistema actual ya es funcional y sin bugs críticos. El objetivo ahora es **maximizar Sharpe Ratio y Win Rate** mediante análisis más profundo, gestión de riesgo adaptativa, y agentes de IA más sofisticados.

---

## PRIORIDAD DE IMPLEMENTACIÓN

| Prioridad | Área | Impacto | Complejidad | Estado |
|-----------|------|---------|-------------|--------|
| 1 | Gestión de Riesgo Adaptativa | ALTO | BAJA | ✅ Implementada abajo |
| 2 | Sistema de Votación Ponderada | ALTO | MEDIA | ✅ Implementada abajo |
| 3 | Análisis de Volumen Confirmado | MEDIO | BAJA | ✅ Implementada abajo |
| 4 | Indicadores Avanzados (MACD, Bollinger, ATR dinámico) | MEDIO | MEDIA | ✅ Implementada abajo |
| 5 | Detección de Patrones de Velas Mejorada | MEDIO | ALTA | ✅ Implementada abajo |
| 6 | Clasificación de Estado del Mercado | MEDIO | MEDIA | ✅ Implementada abajo |
| 7 | Prompt Estructurado con JSON Schema | MEDIO | BAJA | ✅ Implementada abajo |
| 8 | Correlaciones entre Pares | BAJO | MEDIA | ✅ Implementada abajo |
| 9 | News Scoring Ponderado | BAJO | MEDIA | ✅ Implementada abajo |
| 10 | Multi-Timeframe Analysis | ALTO | ALTA | 📋 Diseñada (requiere datos H4/D1) |

---

## ÁREA: Gestión de Riesgo Adaptativa v3.0

### ANÁLISIS ACTUAL
- Riesgo fijo del 2% por operación
- Volumen fijo entre 0.01-0.05 lots
- Sin ajuste dinámico basado en confianza del modelo
- Sin gestión de drawdown acumulativo

### ALGORITMO MEJORADO

```javascript
// ============================================
// RISK MANAGER v3.0 - GESTIÓN DE RIESGO ADAPTATIVA
// ============================================
// Colocar en un nuevo nodo de código antes de "Preparar Orden"
// Este nodo reemplaza el cálculo de volumen fijo por uno dinámico

const decision = $input.all()[0].json;
const memory = $node["Memory Manager"]?.json?.memory || global.tradingMemory || {};

// ============================================
// CONFIGURACIÓN DE RIESGO
// ============================================
const RISK_CONFIG = {
    base_risk_percent: 0.02,       // 2% riesgo base
    max_risk_percent: 0.04,        // 4% máximo absoluto
    min_risk_percent: 0.005,       // 0.5% mínimo
    confidence_threshold: 50,      // Umbral base para confianza
    drawdown_levels: {
        level1: { threshold: 0.05, reduction: 0.5, action: "reduce" },    // 5% DD → 50% riesgo
        level2: { threshold: 0.10, reduction: 0.25, action: "reduce" },   // 10% DD → 25% riesgo
        level3: { threshold: 0.15, reduction: 0, action: "halt" }         // 15% DD → PARAR
    },
    daily_loss_limit: 0.05,        // 5% pérdida diaria máxima
    weekly_loss_limit: 0.10,       // 10% pérdida semanal máxima
    cooldown_after_halt: 4 * 60 * 60 * 1000  // 4 horas de cooldown
};

// ============================================
// CALCULAR DRAWDOWN ACTUAL
// ============================================
function calculateDrawdown(trades) {
    if (!trades || trades.length === 0) return { current: 0, max: 0 };

    let peak = 0;
    let maxDrawdown = 0;
    let currentEquity = 100000; // Balance inicial
    let currentDrawdown = 0;

    for (const trade of trades) {
        const pips = trade.pips || 0;
        const volume = trade.volume || 0.01;
        const profit = pips * volume * 10; // Aproximación en USD

        currentEquity += profit;

        if (currentEquity > peak) {
            peak = currentEquity;
        }

        const dd = (peak - currentEquity) / peak;
        if (dd > maxDrawdown) {
            maxDrawdown = dd;
        }
        currentDrawdown = dd;
    }

    return { current: currentDrawdown, max: maxDrawdown };
}

// ============================================
// CALCULAR DRAWDOWN DIARIO Y SEMANAL
// ============================================
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
// VERIFICAR COOLDOWN TRAS HALT
// ============================================
function checkCooldown(memory) {
    if (!memory.halt_timestamp) return { isCooldown: false };

    const elapsed = Date.now() - memory.halt_timestamp;
    const cooldownMs = RISK_CONFIG.cooldown_after_halt;

    return {
        isCooldown: elapsed < cooldownMs,
        remainingMinutes: Math.ceil((cooldownMs - elapsed) / (60 * 1000)),
        canResume: elapsed >= cooldownMs
    };
}

// ============================================
// CÁLCULO DE RIESGO ADAPTATIVO
// ============================================
const trades = memory.trades || [];
const confidence = decision.confianza || 50;
const accountBalance = decision.market_data?.risk?.account_balance || 100000;

// 1. Calcular drawdowns
const overallDD = calculateDrawdown(trades);
const dailyDD = calculatePeriodDrawdown(trades, 24);
const weeklyDD = calculatePeriodDrawdown(trades, 168);

console.log(`📊 Drawdown Actual: ${(overallDD.current * 100).toFixed(2)}%`);
console.log(`📊 Drawdown Máximo: ${(overallDD.max * 100).toFixed(2)}%`);
console.log(`📊 Drawdown Diario: ${(dailyDD * 100).toFixed(2)}%`);
console.log(`📊 Drawdown Semanal: ${(weeklyDD * 100).toFixed(2)}%`);

// 2. Verificar límites de drawdown
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
            console.log(`⚠️ ${level}: Riesgo reducido a ${(riskMultiplier * 100).toFixed(0)}%`);
        }
    }
}

// Verificar drawdown diario
if (dailyDD >= RISK_CONFIG.daily_loss_limit) {
    shouldHalt = true;
    haltReason = `Drawdown diario alcanzado: ${(dailyDD * 100).toFixed(1)}% > ${(RISK_CONFIG.daily_loss_limit * 100).toFixed(1)}%`;
}

// Verificar drawdown semanal
if (weeklyDD >= RISK_CONFIG.weekly_loss_limit) {
    shouldHalt = true;
    haltReason = `Drawdown semanal alcanzado: ${(weeklyDD * 100).toFixed(1)}% > ${(RISK_CONFIG.weekly_loss_limit * 100).toFixed(1)}%`;
}

// 3. Ajustar por confianza
if (!shouldHalt) {
    const confidenceRatio = confidence / RISK_CONFIG.confidence_threshold;
    const confidenceMultiplier = Math.max(0.5, Math.min(2.0, confidenceRatio));
    riskMultiplier *= confidenceMultiplier;

    console.log(`📈 Ajuste por confianza (${confidence}%): ${(confidenceMultiplier * 100).toFixed(0)}%`);
}

// 4. Ajustar por racha
const recentTrades = trades.slice(-5);
const recentWins = recentTrades.filter(t => t.result === "win").length;
const recentLosses = recentTrades.filter(t => t.result === "loss").length;

if (recentLosses >= 3) {
    riskMultiplier *= 0.5;
    console.log(`⚠️ Racha de ${recentLosses} pérdidas: riesgo reducido 50%`);
} else if (recentWins >= 3) {
    riskMultiplier = Math.min(riskMultiplier * 1.2, RISK_CONFIG.max_risk_percent / RISK_CONFIG.base_risk_percent);
    console.log(`📈 Racha de ${recentWins} ganancias: riesgo aumentado 20%`);
}

// 5. Calcular riesgo final
let finalRiskPercent = RISK_CONFIG.base_risk_percent * riskMultiplier;
finalRiskPercent = Math.max(RISK_CONFIG.min_risk_percent, Math.min(RISK_CONFIG.max_risk_percent, finalRiskPercent));

console.log(`🎯 Riesgo Final: ${(finalRiskPercent * 100).toFixed(2)}% (multiplicador: ${riskMultiplier.toFixed(2)}x)`);

// ============================================
// DECISIÓN DE HALT
// ============================================
if (shouldHalt) {
    console.log(`🛑 HALT ACTIVADO: ${haltReason}`);

    return [{
        json: {
            should_execute: false,
            halt: true,
            halt_reason: haltReason,
            risk_percent: 0,
            volume: 0,
            drawdown: {
                current: overallDD.current,
                max: overallDD.max,
                daily: dailyDD,
                weekly: weeklyDD
            },
            cooldown: checkCooldown(memory)
        }
    }];
}

// ============================================
// CALCULAR VOLUMEN BASADO EN RIESGO
// ============================================
const symbol = decision.symbol || decision.market_data?.best_pair || "EURUSD";
const entryPrice = decision.entrada?.precio || decision.market_data?.technical?.current_price || 0;
const stopPrice = decision.stop_loss?.precio || 0;

const riskInUSD = accountBalance * finalRiskPercent;
const stopPips = Math.abs(entryPrice - stopPrice);
const pipValue = symbol.includes("JPY") ? 100 : 10000;
const slInPips = stopPips * pipValue;

let calculatedVolume = slInPips > 0 ? riskInUSD / (slInPips * 10) : 0.01;

// Limitar volumen
const MAX_VOLUME = 0.05;
const MIN_VOLUME = 0.01;
calculatedVolume = Math.max(MIN_VOLUME, Math.min(MAX_VOLUME, calculatedVolume));
calculatedVolume = Math.round(calculatedVolume * 100) / 100;

console.log(`💰 Riesgo en USD: $${riskInUSD.toFixed(2)}`);
console.log(`📏 Stop Loss: ${slInPips.toFixed(1)} pips`);
console.log(`📦 Volumen calculado: ${calculatedVolume} lots`);

// ============================================
// RESULTADO
// ============================================
return [{
    json: {
        ...decision,
        should_execute: true,
        halt: false,
        risk_percent: finalRiskPercent,
        risk_multiplier: riskMultiplier,
        volume: calculatedVolume,
        risk_in_usd: riskInUSD,
        stop_pips: slInPips,
        drawdown: {
            current: overallDD.current,
            max: overallDD.max,
            daily: dailyDD,
            weekly: weeklyDD
        },
        recent_performance: {
            wins: recentWins,
            losses: recentLosses,
            last_5: recentTrades.map(t => t.result || "unknown")
        }
    }
}];
```

### IMPACTO ESPERADO
- Reducción de drawdown máximo: 15% → 8%
- Mejor ratio riesgo/recompensa: 1.5:1 → 2.0:1
- Win Rate: sin cambio directo, pero menos pérdidas grandes

### DEPENDENCIAS
- Requiere que Memory Manager registre trades con timestamp y resultado
- Requiere campo `pips` en cada trade registrado

---

## ÁREA: Sistema de Votación Ponderada v3.0

### ANÁLISIS ACTUAL
- Agente Estratega recibe decisiones pero no implementa votación real
- Pesos de agentes fijos (Técnico: 0.35, Fundamental: 0.25, etc.)
- Sin veto por baja confianza individual
- Sin requisito de consenso mínimo

### ALGORITMO MEJORADO

```javascript
// ============================================
// VOTATION ENGINE v3.0 - SISTEMA DE VOTACIÓN PONDERADA
// ============================================
// Reemplazar el nodo "Agente Estratega" actual por este sistema de votación
// Este nodo recibe las decisiones de todos los agentes y vota ponderadamente

const inputData = $input.all()[0].json;

console.log("========================================");
console.log("🗳️ VOTATION ENGINE v3.0 - INICIANDO");
console.log("========================================");

// ============================================
// CONFIGURACIÓN DE VOTACIÓN
// ============================================
const VOTATION_CONFIG = {
    min_consensus_agents: 3,     // Mínimo 3 agentes deben coincidir
    veto_confidence_threshold: 25, // Veto si algún agente tiene confianza < 25
    min_weighted_score: 40,       // Score ponderado mínimo para actuar
    dynamic_weights: true         // Activar pesos dinámicos por rendimiento
};

// ============================================
// EXTRAER VOTOS DE CADA AGENTE
// ============================================
function extractAgentVotes(inputData) {
    const votes = [];

    // Extraer del campo agents_responses si existe (formato jetson)
    if (inputData.agents_responses) {
        const agents = inputData.agents_responses;

        // Agente Técnico
        if (agents.technical) {
            const tech = agents.technical;
            votes.push({
                agent_name: "technical",
                direction: normalizeSignal(tech.signal || tech.decision),
                confidence: parseInt(tech.confidence || tech.confianza || 50),
                weight: getAgentWeight("technical"),
                reasoning: tech.analysis || tech.reasoning || "No analysis provided"
            });
        }

        // Agente Fundamental
        if (agents.fundamental) {
            const fund = agents.fundamental;
            votes.push({
                agent_name: "fundamental",
                direction: normalizeSignal(fund.recommendation === "act" ? 
                    (fund.market_bias?.includes("bullish") ? "buy" : 
                     fund.market_bias?.includes("bearish") ? "sell" : "neutral") : "neutral"),
                confidence: parseInt(fund.confidence || 50),
                weight: getAgentWeight("fundamental"),
                reasoning: fund.analysis || "No analysis provided"
            });
        }

        // Agente Sentimiento
        if (agents.sentiment) {
            const sent = agents.sentiment;
            votes.push({
                agent_name: "sentiment",
                direction: normalizeSignal(sent.sentiment || sent.signal),
                confidence: parseInt(sent.confidence || 50),
                weight: getAgentWeight("sentiment"),
                reasoning: sent.analysis || "No analysis provided"
            });
        }

        // Agente Estadístico
        if (agents.statistical) {
            const stat = agents.statistical;
            votes.push({
                agent_name: "statistical",
                direction: normalizeSignal(stat.signal || stat.decision),
                confidence: parseInt(stat.confidence || stat.probability || 50),
                weight: getAgentWeight("statistical"),
                reasoning: stat.analysis || "No analysis provided"
            });
        }
    }

    // Extraer de _extracted si viene de OpenRouter unificado (formato openrouter-una)
    if (inputData._extracted && votes.length === 0) {
        const extracted = inputData._extracted;
        votes.push({
            agent_name: "unified_ai",
            direction: normalizeSignal(extracted.decision),
            confidence: parseInt(extracted.confidence || 50),
            weight: 0.70, // Peso consolidado para agente unificado
            reasoning: extracted.analysis || "Unified AI analysis"
        });

        // Agente estadístico como segundo voto (datos históricos)
        votes.push({
            agent_name: "statistical",
            direction: normalizeSignal(extracted.decision),
            confidence: calculateStatisticalConfidence(inputData),
            weight: 0.30,
            reasoning: "Based on historical performance"
        });
    }

    return votes;
}

function normalizeSignal(signal) {
    if (!signal) return "neutral";
    const s = signal.toLowerCase().trim();
    if (["buy", "long", "comprar", "bullish"].includes(s)) return "buy";
    if (["sell", "short", "vender", "bearish"].includes(s)) return "sell";
    return "neutral";
}

// ============================================
// PESOS DINÁMICOS POR RENDIMIENTO
// ============================================
function getAgentWeight(agentName) {
    const memory = global.tradingMemory || $node["Memory Manager"]?.json?.memory || {};
    const performance = memory.performance || {};
    const agentPerf = performance[agentName] || {};

    const baseWeights = {
        technical: 0.35,
        fundamental: 0.25,
        sentiment: 0.25,
        statistical: 0.15
    };

    // Si no hay rendimiento registrado, usar peso base
    if (!agentPerf.wins && !agentPerf.losses) {
        return baseWeights[agentName] || 0.25;
    }

    // Calcular peso dinámico basado en winrate
    const total = agentPerf.wins + agentPerf.losses;
    const winrate = total > 0 ? agentPerf.wins / total : 0.5;

    // Fórmula: peso_base * (1 + alpha * (winrate - 0.5))
    const alpha = 0.8; // Factor de ajuste
    const dynamicWeight = baseWeights[agentName] * (1 + alpha * (winrate - 0.5));

    return Math.max(0.05, Math.min(0.50, dynamicWeight));
}

function calculateStatisticalConfidence(inputData) {
    const memory = global.tradingMemory || {};
    const metrics = memory.global_metrics || {};
    const totalTrades = metrics.total_trades || 0;
    const winrate = metrics.winning_trades / (totalTrades || 1);

    // Confianza estadística: más trades + mejor winrate = más confianza
    const tradeConfidence = Math.min(100, totalTrades * 2); // 50 trades = 100%
    const winrateConfidence = winrate * 100;

    return Math.round((tradeConfidence * 0.3 + winrateConfidence * 0.7));
}

// ============================================
// SISTEMA DE VOTACIÓN
// ============================================
function executeVotation(votes) {
    console.log("\n🗳️ === PROCESO DE VOTACIÓN ===");

    // Mostrar todos los votos
    for (const vote of votes) {
        console.log(`   ${vote.agent_name}: ${vote.direction.toUpperCase()} (confianza: ${vote.confidence}%, peso: ${vote.weight.toFixed(2)})`);
    }

    // ============================================
    // 1. VERIFICAR VETOS
    // ============================================
    const vetoes = votes.filter(v => v.confidence < VOTATION_CONFIG.veto_confidence_threshold);
    if (vetoes.length > 0) {
        console.log(`\n🚫 VETO DETECTADO: ${vetoes.map(v => v.agent_name).join(", ")} con confianza < ${VOTATION_CONFIG.veto_confidence_threshold}%`);

        // Si 2+ agentes vetan, forzar neutral
        if (vetoes.length >= 2) {
            return {
                decision: "hold",
                confidence: 0,
                reasoning: `Veto: ${vetoes.length} agentes con muy baja confianza`,
                votes: votes,
                vetoes: vetoes.map(v => v.agent_name),
                should_execute: false
            };
        }
    }

    // ============================================
    // 2. CONTAR VOTOS POR DIRECCIÓN
    // ============================================
    const buyVotes = votes.filter(v => v.direction === "buy");
    const sellVotes = votes.filter(v => v.direction === "sell");
    const neutralVotes = votes.filter(v => v.direction === "neutral");

    // Calcular scores ponderados
    const buyScore = buyVotes.reduce((sum, v) => sum + (v.confidence * v.weight), 0);
    const sellScore = sellVotes.reduce((sum, v) => sum + (v.confidence * v.weight), 0);
    const neutralScore = neutralVotes.reduce((sum, v) => sum + (v.confidence * v.weight), 0);

    const totalWeight = votes.reduce((sum, v) => sum + v.weight, 0);

    console.log(`\n📊 SCORES PONDERADOS:`);
    console.log(`   BUY: ${buyScore.toFixed(2)} (${(buyScore / (totalWeight || 1) * 100).toFixed(0)}%)`);
    console.log(`   SELL: ${sellScore.toFixed(2)} (${(sellScore / (totalWeight || 1) * 100).toFixed(0)}%)`);
    console.log(`   NEUTRAL: ${neutralScore.toFixed(2)} (${(neutralScore / (totalWeight || 1) * 100).toFixed(0)}%)`);

    // ============================================
    // 3. VERIFICAR CONSENSO
    // ============================================
    const actingAgents = buyVotes.length + sellVotes.length;
    if (actingAgents < VOTATION_CONFIG.min_consensus_agents) {
        console.log(`\n⏸️ CONSENSO INSUFICIENTE: ${actingAgents} agentes actuando (mínimo ${VOTATION_CONFIG.min_consensus_agents})`);
        return {
            decision: "hold",
            confidence: 0,
            reasoning: `Solo ${actingAgents} agentes con señal clara (mínimo ${VOTATION_CONFIG.min_consensus_agents})`,
            votes: votes,
            should_execute: false
        };
    }

    // ============================================
    // 4. DETERMINAR DECISIÓN FINAL
    // ============================================
    let finalDecision = "hold";
    let finalConfidence = 0;
    let winningVotes = [];

    if (buyScore > sellScore && buyScore > neutralScore) {
        finalDecision = "buy";
        finalConfidence = Math.round((buyScore / (totalWeight || 1)) * 100);
        winningVotes = buyVotes;
    } else if (sellScore > buyScore && sellScore > neutralScore) {
        finalDecision = "sell";
        finalConfidence = Math.round((sellScore / (totalWeight || 1)) * 100);
        winningVotes = sellVotes;
    }

    // Verificar score ponderado mínimo
    if (finalConfidence < VOTATION_CONFIG.min_weighted_score) {
        console.log(`\n⏸️ SCORE INSUFICIENTE: ${finalConfidence}% < ${VOTATION_CONFIG.min_weighted_score}%`);
        return {
            decision: "hold",
            confidence: finalConfidence,
            reasoning: `Score ponderado ${finalConfidence}% insuficiente`,
            votes: votes,
            should_execute: false
        };
    }

    // ============================================
    // 5. CALCULAR CONFIANZA COMPUESTA
    // ============================================
    // Promedio ponderado de confianzas de agentes ganadores
    const avgConfidence = winningVotes.length > 0 ?
        Math.round(winningVotes.reduce((sum, v) => sum + v.confidence, 0) / winningVotes.length) :
        finalConfidence;

    const finalResult = {
        decision: finalDecision,
        confianza: avgConfidence,
        razonamiento: `${finalDecision.toUpperCase()} con ${avgConfidence}% confianza ponderada. ` +
            `Votos: ${buyVotes.length} BUY, ${sellVotes.length} SELL, ${neutralVotes.length} NEUTRAL`,
        votation_details: {
            buy_score: Math.round(buyScore),
            sell_score: Math.round(sellScore),
            neutral_score: Math.round(neutralScore),
            acting_agents: actingAgents,
            winning_agents: winningVotes.map(v => v.agent_name),
            vetoed_agents: vetoes.map(v => v.agent_name)
        },
        votes: votes.map(v => ({
            agent: v.agent_name,
            direction: v.direction,
            confidence: v.confidence,
            weight: v.weight
        })),
        should_execute: true
    };

    console.log(`\n✅ DECISIÓN FINAL: ${finalResult.decision.toUpperCase()} (${avgConfidence}%)`);
    console.log("========================================\n");

    return finalResult;
}

// ============================================
// EJECUTAR VOTACIÓN
// ============================================
const votes = extractAgentVotes(inputData);

if (votes.length === 0) {
    console.log("⚠️ No se encontraron votos de agentes, usando valores por defecto");
    return [{
        json: {
            decision: "hold",
            confianza: 0,
            razonamiento: "No agent votes available",
            should_execute: false
        }
    }];
}

const result = executeVotation(votes);

return [{ json: result }];
```

### IMPACTO ESPERADO
- Win Rate: 45-60% → 55-65% (mejor filtro de señales débiles)
- Reducción de operaciones perdedoras: ~20%
- Mejor consistencia en decisiones

### DEPENDENCIAS
- Requiere que cada agente retorne `signal`, `confidence`, y `analysis`
- Requiere Memory Manager con registro de rendimiento por agente

---

## ÁREA: Análisis de Volumen Confirmado v3.0

### ANÁLISIS ACTUAL
- Solo calcula ratio de volumen (actual vs promedio)
- No interpreta volumen para confirmar señales
- No detecta divergencias precio-volumen
- No identifica agotamiento

### ALGORITMO MEJORADO

```javascript
// ============================================
// VOLUME ANALYZER v3.0 - ANÁLISIS DE VOLUMEN AVANZADO
// ============================================
// Añadir como función dentro de "Agente Técnico" o como nodo separado
// Este algoritmo interpreta el volumen para confirmar/rechazar señales

function analyzeVolume(candles, currentPrice, trend) {
    if (!candles || candles.length < 20) {
        return {
            volume_ratio: 1,
            volume_trend: "unknown",
            price_volume_divergence: false,
            exhaustion_detected: false,
            breakout_confirmed: false,
            volume_signal: "neutral",
            confidence_adjustment: 0
        };
    }

    const lastCandle = candles[candles.length - 1];
    const prevCandle = candles[candles.length - 2];

    const currentVolume = lastCandle?.tick_volume || 0;
    const prevVolume = prevCandle?.tick_volume || 0;

    // Calcular volumen promedio de 20 velas
    const avgVolume = candles.slice(-20).reduce((sum, c) => sum + (c.tick_volume || 0), 0) / 20;
    const volumeRatio = avgVolume > 0 ? currentVolume / avgVolume : 1;

    // ============================================
    // 1. TENDENCIA DE VOLUMEN (últimas 5 velas)
    // ============================================
    const recentVolumes = candles.slice(-5).map(c => c.tick_volume || 0);
    const volumeTrend = calculateTrend(recentVolumes);

    // ============================================
    // 2. DIVERGENCIA PRECIO-VOLUMEN
    // ============================================
    const last10Prices = candles.slice(-10).map(c => c.close || 0);
    const last10Volumes = candles.slice(-10).map(c => c.tick_volume || 0);

    const priceTrend = calculateTrend(last10Prices);
    const volTrend10 = calculateTrend(last10Volumes);

    // Divergencia: precio sube pero volumen baja (señal de debilidad)
    const priceVolumeDivergence = (
        (priceTrend === "up" && volTrend10 === "down") ||
        (priceTrend === "down" && volTrend10 === "up")
    );

    // ============================================
    // 3. DETECCIÓN DE AGOTAMIENTO
    // ============================================
    // Volumen muy alto pero precio estancado = agotamiento
    const lastPriceChange = Math.abs(
        (lastCandle.close || 0) - (prevCandle.close || 0)
    );
    const avgPriceChange = candles.slice(-10).reduce((sum, _, i, arr) => {
        if (i === 0) return 0;
        return sum + Math.abs((arr[i].close || 0) - (arr[i-1].close || 0));
    }, 0) / 9;

    const exhaustionDetected = (
        volumeRatio > 2.0 && // Volumen muy alto
        lastPriceChange < avgPriceChange * 0.3 // Pero precio casi no se mueve
    );

    // ============================================
    // 4. CONFIRMACIÓN DE RUPTURA
    // ============================================
    // Detectar si hay una ruptura de soporte/resistencia confirmada por volumen
    const support = Math.min(...candles.slice(-20).map(c => c.low || 0));
    const resistance = Math.max(...candles.slice(-20).map(c => c.high || 0));

    const breakoutConfirmed = (
        volumeRatio > 1.5 && (
            (lastCandle.close || 0) > resistance * 1.001 || // Ruptura alcista
            (lastCandle.close || 0) < support * 0.999       // Ruptura bajista
        )
    );

    // ============================================
    // 5. SEÑAL DE VOLUMEN COMPUESTA
    // ============================================
    let volumeSignal = "neutral";
    let confidenceAdjustment = 0;

    if (volumeRatio > 1.5 && !priceVolumeDivergence && !exhaustionDetected) {
        volumeSignal = "confirming";
        confidenceAdjustment = 10; // Aumentar confianza
    } else if (exhaustionDetected) {
        volumeSignal = "exhaustion";
        confidenceAdjustment = -15; // Reducir confianza significativamente
    } else if (priceVolumeDivergence) {
        volumeSignal = "divergence";
        confidenceAdjustment = -10; // Reducir confianza
    } else if (breakoutConfirmed) {
        volumeSignal = "breakout";
        confidenceAdjustment = 15; // Aumentar confianza fuertemente
    } else if (volumeRatio < 0.5) {
        volumeSignal = "low_participation";
        confidenceAdjustment = -5; // Ligeramente negativo
    }

    return {
        volume_ratio: Math.round(volumeRatio * 100) / 100,
        volume_trend: volumeTrend,
        price_volume_divergence: priceVolumeDivergence,
        exhaustion_detected: exhaustionDetected,
        breakout_confirmed: breakoutConfirmed,
        volume_signal: volumeSignal,
        confidence_adjustment: confidenceAdjustment,
        avg_volume_20: Math.round(avgVolume),
        current_volume: currentVolume
    };
}

function calculateTrend(values) {
    if (values.length < 3) return "flat";

    // Simple linear regression slope
    const n = values.length;
    const xMean = (n - 1) / 2;
    const yMean = values.reduce((s, v) => s + v, 0) / n;

    let numerator = 0;
    let denominator = 0;

    for (let i = 0; i < n; i++) {
        numerator += (i - xMean) * (values[i] - yMean);
        denominator += (i - xMean) ** 2;
    }

    if (denominator === 0) return "flat";

    const slope = numerator / denominator;
    const normalizedSlope = slope / (yMean || 1);

    if (normalizedSlope > 0.02) return "up";
    if (normalizedSlope