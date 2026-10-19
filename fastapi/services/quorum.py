"""
Quórum entre modelos en mercado LATERAL (T-31, orden Félix 2026-10-19:
«necesito test de quorum entre modelos en mercado lateral»).

Por qué
------
En régimen lateral el precio no tiene inercia: una entrada direccional
sostenida por UN solo modelo es una moneda al aire y el coste es llevarte
la contraria. La evidencia del propio sistema lo
dice: el PPO se entrenó en un contexto mayoritariamente tendencial (m5/m6
del gate T-24 medidos en rojo) y su edge no está demostrado en lateral.

Regla
-----
- Régimen NO lateral (TRENDING/VOLATILE/BREAKOUT): el quorum NO se aplica.
  El PPO opera con su lógica y el LLM conserva su veto — este veto sólo
  tiene autoridad si la ha demostrado con datos reales
  (services/qwen_authority.py).
- Régimen LATERAL (determinista, NO la opinión del LLM): una entrada
  direccional exige QUÓRUM = al menos 2 de las 3 fuentes independientes
  apuntando al mismo lado.

Fuentes de voto (independientes por construcción)
-------------------------------------------------
1. PPO        — dirección decodificada de la política (BUY/SELL/HOLD).
2. LLM        — sesgo del juez, SÓLO cuenta si APORTA EVIDENCIA
                (no abstención) y su `bias_prob` no es neutral.
3. ESTRUCTURA — determinista: precio vs SMA200 (distancia + pendiente).
                Sólo vota si la distancia y la pendiente CONFIrman el mismo
                lado; si se contradicen, no vota (no inventa).

Detección de lateral: determinista y conservadora — los tres criterios de
`_classify_regime` (bb_pos/atr_pct/macd_hist) más la confirmación de ADX
(sin tendencia). El régimen nunca lo decide el LLM: su campo `regime` es
una de las salidas copiadas por plantilla (T-31, RCA medida).
"""
from typing import Optional

# ADX por debajo del cual NO hay tendencia.
# ESCALA: la columna `adx` del df viene NORMALIZADA a [0,1]
# (`trading_env_v2.engineer_market_features`: DX medio / 100). El mismo
# criterio lo usa ya el reward del env (`is_ranging = adx < 0.25`,
# trading_env_v2.py:1101) — aquí se replica para que el detector de
# lateral y el reward del PPO hablen el mismo idioma.
# (Bug real detectado en vivo el 2026-10-19: con umbral 20.0 —escala
# clásica— TODO el mercado se declaraba lateral y el quorum bloqueaba
# el 100 % de las entradas.)
ADX_TREND_MIN = 0.25
# Umbrales de `_classify_regime` (RANGING) — espejo deliberado del cliente.
BB_POS_TREND_MIN = 0.8
ATR_PCT_VOLATILE_MIN = 0.015
MACD_HIST_FLAT_MAX = 0.0002
# ── T-31 (2026-10-19): el criterio de DESPLAZAMIENTO ──────────────────────
# El ADX de H1 es ciego al problema real: mide el RATIO de movimiento
# direccional sobre volatilidad, NO cuánto se ha movido el precio. Medido en
# vivo el 19-10: ADX H1 = 33,1 ("tendencia") con el precio en un rango de
# 54 pips y un desplazamiento de 24 h de 15,9 pips (1,16 ATR) ⇒ el detector
# decía "no lateral" y dejó entrar un SELL en rango (SL dentro del rango,
# TP fuera). El ADX de H4 (21,9) y el LLM ("Ranging EURUSD regime") sí veían
# el rango.
#
# Criterio nuevo (calibrado sobre 400 velas H1 con el movimiento FUTURO a
# +24 h como verdad): "lateral" = el precio NO ha ido a ninguna parte, medido
# por desplazamiento en ATR, con confirmación del marco superior.
# Separación medida (movimiento futuro medio):
#   desplazamiento ≤1,0 ATR → 1,36 ATR   | resto → 3,36 ATR  (+2,00)
#   desplazamiento ≤1,5 ATR → 1,59 ATR   | resto → 3,39 ATR  (+1,80)
#   sólo ADX H1 <0,25       → 2,37 ATR   | resto → 3,50 ATR  (+1,13)
DISP_MAX_ATR = 1.5
# ADX del marco superior (H4): por encima de esto hay tendencia y NO es lateral.
ADX_H4_TREND_MIN = 0.25
# Pendiente mínima del SMA200 (H1) para que la ESTRUCTURA vote: por debajo de
# esto la pendiente es ruido. Se mide en RATIO (ΔSMA200/close), el mismo
# criterio y la misma constante que el reward del env
# (REWARD_V2_S0_SLOPE = 0,0002 = 0,02 %).
SMA200_SLOPE_MIN = 0.0002
# Quórum: fuentes que deben coincidir (de 3 disponibles).
QUORUM_REQUIRED_VOTES = 2
# Umbral de `bias_prob` para que el voto del LLM cuente (además de la
# evidencia): por debajo, el juez es "indeciso" y no vota.
LLM_BIAS_PROB_MIN = 0.55
LLM_BIAS_PROB_MAX = 0.45


def detect_lateral(
    bb_pos: Optional[float],
    atr_pct: Optional[float],
    macd_hist: Optional[float],
    adx: Optional[float] = None,
    disp24_atr: Optional[float] = None,
    adx_h4: Optional[float] = None,
    slope_h1_ratio: Optional[float] = None,
) -> bool:
    """¿Mercado LATERAL (chop)? Determinista (nunca el LLM).

    Regla de tres partes:
      1. Si el marco SUPERIOR tiene tendencia (ADX H4 ≥ 0,25) → NO es
         lateral: el PPO (seguidor de tendencia) tiene con qué trabajar.
      2. Alguna señal de "el precio no tiene estructura" debe cumplirse:
         (a) desplazamiento ≤ DISP_MAX_ATR (1,5 ATR) en 24 h REALES — el
             criterio con poder predictivo medido (separación +1,8 ATR en el
             movimiento futuro);
         (b) la estructura H1 está PLANA (|pendiente SMA200| < 0,02 % del
             precio): el precio está pegado a su media sin dirección.
         El ADX de H1 NO decide: mide el ratio de movimiento direccional
         sobre volatilidad, no cuánto se ha movido el precio (medido en vivo
         el 19-10: ADX H1 = 35 "tendencia" con el precio en un rango de 54
         pips ⇒ fue el criterio que dejó pasar un SELL en rango).
      3. Ni extremo de banda, ni volatilidad alta, ni MACD en expansión.

    Ante datos insuficientes NO se declara lateral (fail-safe: no se aplica
    el quorum por falta de evidencia).
    """
    if bb_pos is None or atr_pct is None or macd_hist is None:
        return False
    if adx_h4 is not None and adx_h4 >= ADX_H4_TREND_MIN:
        return False  # el marco superior tiene tendencia
    hay_senal_de_chop = False
    if disp24_atr is not None and disp24_atr <= DISP_MAX_ATR:
        hay_senal_de_chop = True
    if slope_h1_ratio is not None and abs(slope_h1_ratio) < SMA200_SLOPE_MIN:
        hay_senal_de_chop = True
    if not hay_senal_de_chop:
        return False
    if abs(bb_pos) > BB_POS_TREND_MIN:
        return False
    if atr_pct > ATR_PCT_VOLATILE_MIN:
        return False
    if abs(macd_hist) >= MACD_HIST_FLAT_MAX:
        return False
    return True


def structure_vote(
    sma200_dist: Optional[float],
    sma200_slope: Optional[float],
    price: Optional[float] = None,
) -> Optional[str]:
    """Voto de ESTRUCTURA (determinista): precio vs SMA200.

    - distancia > 0 y pendiente (RATIO) > SMA200_SLOPE_MIN → "BUY"
    - distancia < 0 y pendiente (RATIO) < −SMA200_SLOPE_MIN → "SELL"
    - si se contradicen, la pendiente es ruido o faltan datos → None (no
      vota; no inventa)

    T-31: la pendiente se normaliza por el precio (ΔSMA200/close) para
    comparar con la misma constante que usa el reward del env
    (REWARD_V2_S0_SLOPE = 0,0002). Sin `price` se asume que la pendiente
    ya viene en ratio.
    """
    if sma200_dist is None or sma200_slope is None:
        return None
    slope_ratio = sma200_slope / price if (price and price > 0) else sma200_slope
    if sma200_dist > 0 and slope_ratio > SMA200_SLOPE_MIN:
        return "BUY"
    if sma200_dist < 0 and slope_ratio < -SMA200_SLOPE_MIN:
        return "SELL"
    return None


def llm_vote(
    llm_bias: Optional[str],
    llm_bias_prob: Optional[float],
    abstained: bool = False,
) -> Optional[str]:
    """Voto del LLM: sólo si APORTA EVIDENCIA y no es indeciso.

    - abstención (sin evidencia / parrota) → None (no vota)
    - bias_prob dentro de la zona neutra → None (no vota)
    - BULLISH con prob ≥ 0.55 → "BUY"; BEARISH con prob ≤ 0.45 → "SELL"
    """
    if abstained:
        return None
    if llm_bias not in ("BULLISH", "BEARISH"):
        return None
    if llm_bias_prob is None:
        return None
    if llm_bias == "BULLISH" and llm_bias_prob >= LLM_BIAS_PROB_MIN:
        return "BUY"
    if llm_bias == "BEARISH" and llm_bias_prob <= LLM_BIAS_PROB_MAX:
        return "SELL"
    return None


def collect_votes(
    ppo_direction: Optional[str],
    llm_bias: Optional[str] = None,
    llm_bias_prob: Optional[float] = None,
    llm_abstained: bool = False,
    sma200_dist: Optional[float] = None,
    sma200_slope: Optional[float] = None,
) -> dict:
    """Recoge los votos de las tres fuentes independientes."""
    votes = {
        "ppo": ppo_direction if ppo_direction in ("BUY", "SELL") else None,
        "llm": llm_vote(llm_bias, llm_bias_prob, llm_abstained),
        "structure": structure_vote(sma200_dist, sma200_slope),
    }
    tally = {"BUY": 0, "SELL": 0}
    for v in votes.values():
        if v in tally:
            tally[v] += 1
    return {
        "votes": votes,
        "tally": tally,
        "n_votes": tally["BUY"] + tally["SELL"],
        "required": QUORUM_REQUIRED_VOTES,
    }


def evaluate(collected: dict, direction: Optional[str] = None) -> dict:
    """Veredicto del quórum: ¿hay consenso suficiente para entrar?

    `direction` = dirección que el PPO propone. El quórum CONFIRMA esa
    dirección; nunca autoriza la contraria (el PPO es la señal, el quórum
    es el filtro). Sin `direction` (uso diagnóstico) se reporta el lado
    mayoritario, pero la entrada real siempre pasa por la dirección del
    PPO.
    """
    tally = collected["tally"]
    required = collected["required"]
    buy, sell = tally["BUY"], tally["SELL"]
    if direction in ("BUY", "SELL"):
        aligned = tally[direction]
        granted = aligned >= required
        reason = (
            f"quórum {direction} {aligned}/{collected['n_votes']} votos "
            f"(se exigen {required} coincidentes)"
            if granted
            else (
                f"sin quórum {direction}: sólo {aligned} de {collected['n_votes']} "
                f"fuentes coinciden (se exigen {required})"
            )
        )
        return {
            "granted": granted,
            "direction": direction if granted else None,
            "votes": collected["votes"],
            "tally": tally,
            "n_votes": collected["n_votes"],
            "required": required,
            "reason": reason,
        }
    if buy >= required and buy > sell:
        granted, direction = True, "BUY"
        reason = f"quórum BUY {buy}/{collected['n_votes']} (≥{required})"
    elif sell >= required and sell > buy:
        granted, direction = True, "SELL"
        reason = f"quórum SELL {sell}/{collected['n_votes']} (≥{required})"
    else:
        granted, direction = False, None
        reason = (
            f"sin quórum: BUY={buy} SELL={sell} de {collected['n_votes']} votos "
            f"(se exigen {required} coincidentes)"
        )
    return {
        "granted": granted,
        "direction": direction,
        "votes": collected["votes"],
        "tally": tally,
        "n_votes": collected["n_votes"],
        "required": required,
        "reason": reason,
    }
