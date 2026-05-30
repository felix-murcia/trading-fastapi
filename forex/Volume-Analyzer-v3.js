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
    if (normalizedSlope < -0.02) return "down";
    return "flat";
}

// Exportar para uso en n8n
module.exports = { analyzeVolume };
