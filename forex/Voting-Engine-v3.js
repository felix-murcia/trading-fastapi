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
