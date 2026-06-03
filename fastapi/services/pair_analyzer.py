"""
Análisis técnico determinista de los 4 pares.

Sin LLM. Matemática pura: RSI, SMA, ATR, S/R, fortaleza de divisa.
Fetcha candles directamente desde MT5 vía socket (n8n no pasa candles).
Devuelve best_pair y los datos técnicos completos de ese par.
"""

from models.analysis import PairsAnalysisRequest, PairsAnalysisResponse, TechnicalData, PairScore, Candle
from services import mt5_client

SYMBOLS = ["EURUSD", "GBPUSD", "USDJPY", "USDCHF", "XAUUSD"]
TIMEFRAME    = "M5"
CANDLE_COUNT = 220   # suficiente para SMA-200 + margen

PAIR_CURRENCIES = {
    "EURUSD": ("EUR", "USD"),
    "GBPUSD": ("GBP", "USD"),
    "USDJPY": ("USD", "JPY"),
    "USDCHF": ("USD", "CHF"),
    "XAUUSD": ("XAU", "USD"),
}


def _closes(candles: list[Candle]) -> list[float]:
    return [c.close for c in candles]


def _sma(values: list[float], period: int) -> float:
    if len(values) < period:
        return values[-1] if values else 0.0
    return sum(values[-period:]) / period


def _rsi(closes: list[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, period + 1):
        diff = closes[-period + i] - closes[-period + i - 1]
        (gains if diff > 0 else losses).append(abs(diff))
    avg_gain = sum(gains) / period if gains else 0.0
    avg_loss = sum(losses) / period if losses else 0.0
    if avg_loss == 0:
        return 100.0
    return 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))


def _atr(candles: list[Candle], period: int = 14) -> float:
    if len(candles) < 2:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        prev = candles[i - 1].close
        tr = max(candles[i].high - candles[i].low,
                 abs(candles[i].high - prev),
                 abs(candles[i].low  - prev))
        trs.append(tr)
    recent = trs[-period:]
    return sum(recent) / len(recent)


def _sr(candles: list[Candle], lookback: int = 20) -> tuple[float, float]:
    recent = candles[-lookback:] if len(candles) >= lookback else candles
    return min(c.low for c in recent), max(c.high for c in recent)


def _trend(price: float, sma20: float, sma200: float) -> str:
    if price > sma20 > sma200:
        return "bullish"
    if price < sma20 < sma200:
        return "bearish"
    return "range"


def _currency_strength(candles_map: dict[str, list[Candle]]) -> dict[str, float]:
    pair_change: dict[str, float] = {}
    for sym, candles in candles_map.items():
        closes = _closes(candles)
        pair_change[sym] = (closes[-1] - closes[0]) / closes[0] * 100 if len(closes) >= 2 else 0.0

    strength: dict[str, float] = {"EUR": 0.0, "GBP": 0.0, "USD": 0.0, "JPY": 0.0, "CHF": 0.0, "XAU": 0.0}
    count:    dict[str, int]   = {k: 0 for k in strength}
    for sym, chg in pair_change.items():
        base, quote = PAIR_CURRENCIES.get(sym, ("?", "?"))
        if base in strength:
            strength[base] += chg;  count[base] += 1
        if quote in strength:
            strength[quote] -= chg; count[quote] += 1
    for cur in strength:
        if count[cur]:
            strength[cur] /= count[cur]
    return strength


def _score(sym: str, closes: list[float], rsi: float,
           sma20: float, sma200: float, strength: dict[str, float]) -> float:
    price = closes[-1]
    momentum      = abs(rsi - 50) / 50.0
    trend_clarity = abs(price - sma200) / (sma200 or 1)
    base, quote   = PAIR_CURRENCIES.get(sym, ("?", "?"))
    str_diff      = abs(strength.get(base, 0) - strength.get(quote, 0))
    return round(momentum * 0.4 + trend_clarity * 10 * 0.3 + str_diff * 0.3, 4)


async def analyze(req: PairsAnalysisRequest) -> PairsAnalysisResponse:
    # Fetchar candles de MT5 para cada par en paralelo (secuencial es suficiente dado el volumen)
    candles_map: dict[str, list[Candle]] = {}
    for sym in SYMBOLS:
        raw = await mt5_client.get_candles(sym, TIMEFRAME, CANDLE_COUNT)
        candles_map[sym] = [Candle(**c) for c in raw]

    strength = _currency_strength(candles_map)
    scores: list[PairScore] = []

    for sym in SYMBOLS:
        candles = candles_map.get(sym, [])
        if not candles:
            continue
        closes = _closes(candles)
        rsi    = _rsi(closes)
        sma20  = _sma(closes, 20)
        sma200 = _sma(closes, 200)
        base, quote = PAIR_CURRENCIES[sym]
        scores.append(PairScore(
            symbol=sym, score=_score(sym, closes, rsi, sma20, sma200, strength),
            strength_base=round(strength.get(base, 0), 4),
            strength_quote=round(strength.get(quote, 0), 4),
        ))

    if not scores:
        raise ValueError("Sin candles disponibles para ningún par")

    best    = max(scores, key=lambda s: s.score)
    sym     = best.symbol
    candles = candles_map[sym]
    closes  = _closes(candles)
    rsi     = _rsi(closes)
    sma20   = _sma(closes, 20)
    atr     = _atr(candles)
    support, resistance = _sr(candles)
    price   = req.prices.get(sym, closes[-1])    # precio en tiempo real > último cierre

    technical = TechnicalData(
        current_price=round(price,  5),
        rsi=          round(rsi,    2),
        sma9=         round(_sma(closes,  9), 5),
        sma20=        round(sma20,  5),
        sma50=        round(_sma(closes, 50), 5),
        sma200=       round(_sma(closes, 200), 5),
        atr=          round(atr,    5),
        atr_avg20=    round(_atr(candles, 20), 5),
        support=      round(support,    5),
        resistance=   round(resistance, 5),
        trend=        _trend(price, sma20, _sma(closes, 200)),
        candles_recent=candles[-5:],
    )

    return PairsAnalysisResponse(
        best_pair=sym,
        price=technical.current_price,
        scores=sorted(scores, key=lambda s: s.score, reverse=True),
        technical=technical,
    )
