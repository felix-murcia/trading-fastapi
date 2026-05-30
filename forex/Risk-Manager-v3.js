// ============================================
// RISK MANAGER v3.0 - GESTIÓN DE RIESGO ADAPTATIVA COMPLETA
// ============================================
// Nodo de código ejecutado antes de "Preparar Orden"
// Reemplaza cálculo de volumen fijo por sistema adaptativo multi-dimensional

const decision = $input.all()[0].json;
const memory = $node["Memory Manager"]?.json?.memory || global.tradingMemory || {
    trades: [],
    performance: {},
    global_metrics: { total_trades: 0, winning_trades: 0 }
};

// ============================================
// CONFIGURACIÓN DE RIESGO - PARÁMETROS AJUSTABLES
// ============================================
const RISK_CONFIG = {
    // Riesgo base y límites
    base_risk_percent: 0.02,
    max_risk_percent: 0.04,
    min_risk_percent: 0.005,
    confidence_threshold: 50,
    
    // Niveles de drawdown con acciones específicas
    drawdown_levels: {
        level1: { threshold: 0.05, reduction: 0.5, action: "reduce", label: "Precaución" },
        level2: { threshold: 0.10, reduction: 0.25, action: "reduce", label: "Alerta" },
        level3: { threshold: 0.15, reduction: 0, action: "halt", label: "Emergencia" }
    },
    
    // Límites de pérdida período
    daily_loss_limit: 0.05,
    weekly_loss_limit: 0.10,
    monthly_loss_limit: 0.20,
    
    // Cooldown post-halt
    cooldown_after_halt: 4 * 60 * 60 * 1000,
    
    // Ajustes por racha
    streak_threshold: 3,
    win_streak_bonus: 1.2,
    loss_streak_penalty: 0.5,
    
    // Factores de ajuste
    max_volume: 0.05,
    min_volume: 0.01,
    pip_value_jpy: 100,
    pip_value_standard: 10000
};

// ============================================
// FUNCIONES DE CÁLCULO DE DRAWDOWN
// ============================================

function calculateDrawdown(trades) {
    if (!trades || trades.length === 0) {
        return { current: 0, max: 0, peak_equity: 100000, current_equity: 100000 };
    }

    let peakEquity = 100000;
    let maxDrawdown = 0;
    let currentEquity = 100000;

    for (const trade of trades) {
        const pips = trade.pips || 0;
        const volume = trade.volume || 0.01;
        const profit = pips * volume * 10;
        currentEquity += profit;

        if (currentEquity > peakEquity) {
            peakEquity = currentEquity;
        }

        const dd = (peakEquity - currentEquity) / peakEquity;
        if (dd > maxDrawdown) {
            maxDrawdown = dd;
        }
    }

    const currentDD = (peakEquity - currentEquity) / peakEquity;
    
    return {
        current: currentDD,
        max: maxDrawdown,
        peak_equity: peakEquity,
        current_equity: currentEquity
    };
}

function calculatePeriodDrawdown(trades, hours) {
    const cutoff = Date.now() - (hours * 60 * 60 * 1000);
    const recentTrades = trades.filter(t => new Date(t.timestamp).getTime() > cutoff);

    if (recentTrades.length === 0) return 0;

    let peak = 0;
    let maxDD = 0;
    let runningTotal = 0;

    for (const trade of recentTrades) {
        const pips = trade.pips || 0;
        const volume = trade.volume || 0.01;
        runningTotal += pips * volume * 10;

        if (runningTotal > peak) peak = runningTotal;
        
        const dd = peak > 0 ? (peak - runningTotal) / peak : 0;
        if (dd > maxDD) maxDD = dd;
    }

    return maxDD;
}

function checkCooldown(memory) {
    if (!memory.halt_timestamp) {
        return { isCooldown: false, remainingMinutes: 0, canResume: true };
    }

    const elapsed = Date.now() - memory.halt_timestamp;
    const cooldownMs = RISK_CONFIG.cooldown_after_halt;

    return {
        isCooldown: elapsed < cooldownMs,
        remainingMinutes: Math.ceil((cooldownMs - elapsed) / (60 * 1000)),
        canResume: elapsed >= cooldownMs,
        haltTimestamp: memory.halt_timestamp
    };
}

// ============================================
// CÁLCULO DE RIESGO ADAPTATIVO
// ============================================

const trades = memory.trades || [];
const confidence = decision.confianza || decision.confidence || 50;
const accountBalance = decision.market_data?.risk?.account_balance || 
                        memory.account_balance || 100000;
const symbol = decision.symbol || decision.market_data?.best_pair || "EURUSD";

// 1. Calcular métricas de drawdown
const overallDD = calculateDrawdown(trades);
const dailyDD = calculatePeriodDrawdown(trades, 24);
const weeklyDD = calculatePeriodDrawdown(trades, 168);
const monthlyDD = calculatePeriodDrawdown(trades, 720);

console.log("╔══════════════════════════════════════════════════════════╗");
console.log("║           ANÁLISIS DE DRAWDOWN - RISK MANAGER v3.0        ║");
console.log("╠══════════════════════════════════════════════════════════╣");
console.log(`║ Drawdown Actual:    ${(overallDD.current * 100).toFixed(2)}%                      ║`);
console.log(`║ Drawdown Máximo:   ${(overallDD.max * 100).toFixed(2)}%                      ║`);
console.log(`║ Drawdown Diario:   ${(dailyDD * 100).toFixed(2)}% (límite: 5%)              ║`);
console.log(`║ Drawdown Semanal:  ${(weeklyDD * 100).toFixed(2)}% (límite: 10%)             ║`);
console.log(`║ Drawdown Mensual:  ${(monthlyDD * 100).toFixed(2)}% (límite: 20%)             ║`);
console.log("╚══════════════════════════════════════════════════════════╝");

// 2. Inicializar multiplicador de riesgo
let riskMultiplier = 1.0;
let shouldHalt = false;
let haltReason = "";
let haltActions = [];

// 3. Verificar límites de drawdown por nivel
for (const [level, config] of Object.entries(RISK_CONFIG.drawdown_levels)) {
    if (overallDD.current >= config.threshold) {
        if (config.action === "halt") {
            shouldHalt = true;
            haltReason = `${config.label}: Drawdown global ${level} alcanzado (${(overallDD.current * 100).toFixed(1)}% ≥ ${(config.threshold * 100).toFixed(0)}%)`;
            haltActions.push(level);
        } else {
            riskMultiplier *= config.reduction;
            console.log(`⚠️ ${config.label}: Riesgo reducido a ${(riskMultiplier * 100).toFixed(0)}% del nominal`);
            haltActions.push(level);
        }
    }
}

// 4. Verificar límites de pérdida por período
if (dailyDD >= RISK_CONFIG.daily_loss_limit) {
    shouldHalt = true;
    haltReason = `Límite diario alcanzado: ${(dailyDD * 100).toFixed(1)}% > ${(RISK_CONFIG.daily_loss_limit * 100).toFixed(0)}%`;
}
if (weeklyDD >= RISK_CONFIG.weekly_loss_limit) {
    shouldHalt = true;
    haltReason = `Límite semanal alcanzado: ${(weeklyDD * 100).toFixed(1)}% > ${(RISK_CONFIG.weekly_loss_limit * 100).toFixed(0)}%`;
}

// 5. Verificar cooldown
const cooldownStatus = checkCooldown(memory);
if (cooldownStatus.isCooldown) {
    shouldHalt = true;
    haltReason = `Cooldown activo: ${cooldownStatus.remainingMinutes} minutos restantes`;
}

// 6. Ajustar por confianza del modelo
if (!shouldHalt) {
    const confidenceRatio = confidence / RISK_CONFIG.confidence_threshold;
    const confidenceMultiplier = Math.max(0.5, Math.min(2.0, confidenceRatio));
    riskMultiplier *= confidenceMultiplier;
    console.log(`📈 Ajuste por confianza (${confidence}%): multiplicador ${confidenceMultiplier.toFixed(2)}x`);
}

// 7. Ajustar por rachas de trades recientes
const recentTrades = trades.slice(-5);
const recentWins = recentTrades.filter(t => (t.result || t.pips) > 0).length;
const recentLosses = recentTrades.filter(t => (t.result || t.pips) < 0).length;

if (recentLosses >= RISK_CONFIG.streak_threshold) {
    riskMultiplier *= RISK_CONFIG.loss_streak_penalty;
    console.log(`⚠️ Racha de ${recentLosses} pérdidas: riesgo reducido ${((1 - RISK_CONFIG.loss_streak_penalty) * 100).toFixed(0)}%`);
} else if (recentWins >= RISK_CONFIG.streak_threshold) {
    riskMultiplier = Math.min(
        riskMultiplier * RISK_CONFIG.win_streak_bonus,
        RISK_CONFIG.max_risk_percent / RISK_CONFIG.base_risk_percent
    );
    console.log(`📈 Racha de ${recentWins} ganancias: riesgo aumentado ${((RISK_CONFIG.win_streak_bonus - 1) * 100).toFixed(0)}%`);
}

// 8. Calcular riesgo final con límites
let finalRiskPercent = RISK_CONFIG.base_risk_percent * riskMultiplier;
finalRiskPercent = Math.max(
    RISK_CONFIG.min_risk_percent,
    Math.min(RISK_CONFIG.max_risk_percent, finalRiskPercent)
);

console.log(`\n🎯 RIESGO FINAL: ${(finalRiskPercent * 100).toFixed(3)}% (multiplicador: ${riskMultiplier.toFixed(3)}x)`);

// ============================================
// DECISIÓN DE HALT
// ============================================
if (shouldHalt) {
    console.log(`\n🛑 SISTEMA DETENIDO: ${haltReason}`);
    console.log(`   Acciones activadas: ${haltActions.join(", ")}`);
    
    if (!cooldownStatus.isCooldown && haltActions.includes("level3")) {
        global.tradingMemory = {
            ...global.tradingMemory,
            halt_timestamp: Date.now(),
            halt_reason: haltReason
        };
    }

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
                weekly: weeklyDD,
                monthly: monthlyDD
            },
            cooldown: cooldownStatus,
            actions_taken: haltActions,
            risk_multiplier: riskMultiplier
        }
    }];
}

// ============================================
// CÁLCULO DE VOLUMEN BASADO EN RIESGO
// ============================================
const entryPrice = decision.entrada?.precio || 
                   decision.market_data?.technical?.current_price || 
                   decision.entry_price || 0;
const stopPrice = decision.stop_loss?.precio || 
                  decision.sl_price || 
                  decision.stop_loss || 0;

const riskInUSD = accountBalance * finalRiskPercent;
const stopPips = Math.abs(entryPrice - stopPrice);
const pipValue = symbol.includes("JPY") ? RISK_CONFIG.pip_value_jpy : RISK_CONFIG.pip_value_standard;
const slInPips = stopPips * pipValue;

let calculatedVolume = slInPips > 0 ? riskInUSD / (slInPips * 10) : 0.01;

// Aplicar límites de volumen
calculatedVolume = Math.max(RISK_CONFIG.min_volume, Math.min(RISK_CONFIG.max_volume, calculatedVolume));
calculatedVolume = Math.round(calculatedVolume * 100) / 100;

console.log(`\n💰 CÁLCULO DE VOLUMEN:`);
console.log(`   Balance: $${accountBalance.toLocaleString()}`);
console.log(`   Riesgo: ${(finalRiskPercent * 100).toFixed(3)}% = $${riskInUSD.toFixed(2)}`);
console.log(`   Stop Loss: ${slInPips.toFixed(1)} pips`);
console.log(`   Volumen: ${calculatedVolume} lots`);

// ============================================
// RESULTADO FINAL
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
            weekly: weeklyDD,
            monthly: monthlyDD,
            peak_equity: overallDD.peak_equity,
            current_equity: overallDD.current_equity
        },
        recent_performance: {
            wins: recentWins,
            losses: recentLosses,
            streak_type: recentWins >= RISK_CONFIG.streak_threshold ? "winning" : 
                        recentLosses >= RISK_CONFIG.streak_threshold ? "losing" : "neutral",
            last_5_results: recentTrades.map(t => (t.result || (t.pips > 0 ? "win" : "loss")))
        },
        risk_breakdown: {
            base: RISK_CONFIG.base_risk_percent,
            confidence_adjust: confidence / RISK_CONFIG.confidence_threshold,
            streak_adjust: recentLosses >= RISK_CONFIG.streak_threshold ? RISK_CONFIG.loss_streak_penalty :
                           recentWins >= RISK_CONFIG.streak_threshold ? RISK_CONFIG.win_streak_bonus : 1.0,
            drawdown_reduction: riskMultiplier
        }
    }
}];
