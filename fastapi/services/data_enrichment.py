"""
Enriquecimiento de datos para LLM agents.

Responsabilidades:
- Enriquecer news con currency real y contenido de URLs
- Enriquecer calendar con horarios, eventos y monedas relacionadas
- Validar que no haya campos vacíos antes de pasar a agents
- Cachear resultados para no repetir extracciones
"""

import re
from typing import Optional
from datetime import datetime

# Mapeo de monedas a pares de trading
CURRENCY_PAIRS = {
    "EUR": ["EURUSD", "EURGBP", "EURJPY", "EURCHF"],
    "USD": ["EURUSD", "GBPUSD", "USDJPY", "USDCHF"],
    "GBP": ["GBPUSD", "EURGBP", "GBPJPY"],
    "JPY": ["USDJPY", "EURJPY", "GBPJPY"],
    "CHF": ["USDCHF", "EURCHF"],
    "XAU": ["XAUUSD"],
}

FOREX_IMPACT_KEYWORDS = {
    "high": [
        "nfp", "payroll", "employment", "fed", "ecb", "boe", "gdp", "inflation",
        "cpi", "ppi", "interest rate", "rate decision", "fomc", "cpi", "pce",
        "unemployment", "jobless claims", "non-farm payroll", "earnings",
        "sales", "housing", "ism", "pmi", "central bank"
    ],
    "medium": [
        "manufacturing", "services", "confidence", "consumer", "retail",
        "factory orders", "durable goods", "wholesale", "inventory",
        "trade balance", "current account", "industrial production"
    ],
    "low": ["price", "data", "report", "update", "index"]
}

SENTIMENT_KEYWORDS = {
    "bullish": [
        "surge", "soars", "rallies", "jump", "gain", "rise", "strength",
        "outperform", "beat", "above forecast", "exceeds", "better than",
        "strong", "growth", "positive", "optimistic", "upbeat", "upgrade"
    ],
    "bearish": [
        "plunge", "crashes", "falls", "decline", "loss", "weakness",
        "underperform", "miss", "below forecast", "weaker than", "miss",
        "weak", "contraction", "negative", "pessimistic", "downgrade",
        "deteriorates", "concern"
    ]
}


def _extract_currency(text: str) -> str:
    """Extrae moneda de un texto usando regex y heurísticas avanzadas."""
    text_upper = text.upper()

    # 1. Búsqueda por código de moneda exacto (prioritario)
    for currency in CURRENCY_PAIRS.keys():
        if re.search(rf"\b{currency}\b", text_upper):
            return currency

    # 2. Búsqueda por pares forex completos (EURUSD, GBPUSD, etc)
    pair_patterns = [
        (r"\b(EURUSD|EUR/USD|EURO/DOLLAR)\b", "EUR"),
        (r"\b(GBPUSD|GBP/USD|STERLING/DOLLAR)\b", "GBP"),
        (r"\b(USDJPY|USD/JPY|DOLLAR/YEN)\b", "JPY"),
        (r"\b(USDCHF|USD/CHF|DOLLAR/SWISS)\b", "CHF"),
        (r"\b(XAUUSD|XAU/USD|GOLD/DOLLAR)\b", "XAU"),
    ]

    for pattern, currency in pair_patterns:
        if re.search(pattern, text_upper):
            return currency

    # 3. Búsqueda por país/institución (más específica)
    country_map = {
        r"\b(EURO|EUROZONE|EUROPEAN|ECB|EUROPEAN CENTRAL BANK)\b": "EUR",
        r"\b(POUND|STERLING|BANK OF ENGLAND|BOE|NATIONWIDE)\b": "GBP",
        r"^UK\s": "GBP",  # "UK may..." al inicio
        r"\bUK\s+(EMPLOYMENT|INFLATION|RETAIL|CONSTRUCTION|PMI|HPI|PAYROLL|CLAIMS|DATA)\b": "GBP",
        r"\b(JAPAN|JAPANESE|BOJ|BANK OF JAPAN|NIKKEI)\b": "JPY",
        r"\b(SWISS|SWISSIE|SNB|SWISS NATIONAL BANK|SWITZERLAND)\b": "CHF",
        r"\b(GOLD|PRECIOUS METAL|XAU)\b": "XAU",
        r"\b(AUSTRALIA|AUSTRALIAN|RBA|BANK OF AUSTRALIA|ANZ)\b": "AUD",
        r"\b(NEW ZEALAND|RBNZ)\b": "NZD",
        r"\b(CANADA|CANADIAN|BOC|BANK OF CANADA)\b": "CAD",
        r"\b(GERMANY|GERMAN|BUNDESBANK|IFOBUS)\b": "EUR",
        r"\b(FRANCE|FRENCH)\b": "EUR",
        r"\b(SPAIN|SPANISH|ITALY|ITALIAN|IRELAND|DUTCH|NETHERLANDS|EUROZONE)\b": "EUR",
        r"^US\s": "USD",  # "US may..." al inicio
        r"\bUS\s+(EMPLOYMENT|INFLATION|PAYROLL|CPI|PPI|RETAIL|CONSTRUCTION|ISM|PMI|GDP|JOBLESS|CLAIMS|LAYOFFS|DATA|ECONOMIC|NONFARM)\b": "USD",
        r"\b(FED|FEDERAL RESERVE|FOMC|CHALLENGER LAYOFFS|ADP PAYROLL)\b": "USD",
    }

    for pattern, currency in country_map.items():
        if re.search(pattern, text_upper):
            return currency

    return ""


def _extract_impact(text: str) -> str:
    """Clasifica impacto según keywords."""
    text_lower = text.lower()

    for level, keywords in FOREX_IMPACT_KEYWORDS.items():
        if any(kw in text_lower for kw in keywords):
            return level

    return "low"


def _detect_sentiment(text: str) -> str:
    """Detecta sentiment (bullish/bearish/neutral) en el texto."""
    text_lower = text.lower()

    bullish_count = sum(1 for kw in SENTIMENT_KEYWORDS["bullish"] if kw in text_lower)
    bearish_count = sum(1 for kw in SENTIMENT_KEYWORDS["bearish"] if kw in text_lower)

    if bullish_count > bearish_count:
        return "bullish"
    elif bearish_count > bullish_count:
        return "bearish"
    else:
        return "neutral"


def _extract_datetime(text: str) -> dict:
    """Extrae fecha y hora de un evento."""
    # Patrones comunes: "2026-06-05 14:30", "June 5 2:30pm", "Today 14:30"
    patterns = [
        r"(\d{4}-\d{2}-\d{2})\s+(\d{1,2}:\d{2})",  # 2026-06-05 14:30
        r"([A-Za-z]+\s+\d{1,2})\s+(\d{1,2}:\d{2})",  # June 5 14:30
        r"(Today|Tomorrow)\s+(\d{1,2}:\d{2})",  # Today 14:30
    ]

    result = {"date": "", "time": "", "utc_offset": ""}

    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            result["date"] = match.group(1)
            result["time"] = match.group(2)
            break

    # Detectar zona horaria (si menciona GMT, EST, etc)
    tz_match = re.search(r"(GMT|UTC|EST|EDT|PST|JST)\s*([+-]\d{1,2})?", text.upper())
    if tz_match:
        result["utc_offset"] = tz_match.group(1)

    return result


class NewsItem:
    """Representa una noticia enriquecida."""
    def __init__(self, headline: str, source_url: str = "", raw_content: str = ""):
        self.headline = headline
        self.source_url = source_url
        combined_text = headline + " " + raw_content
        self.currency = _extract_currency(combined_text)
        self.impact = _extract_impact(combined_text)
        self.sentiment = _detect_sentiment(combined_text)
        self.summary = raw_content[:200] if raw_content else ""
        self.timestamp = datetime.utcnow().isoformat()

    def to_dict(self):
        return {
            "headline": self.headline,
            "currency": self.currency,
            "impact": self.impact,
            "summary": self.summary,
            "sentiment": self.sentiment,
            "source_url": self.source_url,
            "timestamp": self.timestamp,
        }


class CalendarEvent:
    """Representa un evento de calendario enriquecido."""
    def __init__(self, event_name: str, source_url: str = "", raw_content: str = ""):
        self.event_name = event_name
        self.source_url = source_url
        combined_text = event_name + " " + raw_content
        self.currency = _extract_currency(combined_text)
        self.impact = _extract_impact(combined_text)

        datetime_data = _extract_datetime(combined_text)
        self.date = datetime_data["date"]
        self.time = datetime_data["time"]
        self.utc_offset = datetime_data["utc_offset"]

        # Extraer forecast, actual, previous del contenido (si disponibles)
        self.forecast = self._extract_number_field(raw_content, "forecast")
        self.actual = self._extract_number_field(raw_content, "actual")
        self.previous = self._extract_number_field(raw_content, "previous")
        self.timestamp = datetime.utcnow().isoformat()

    def _extract_number_field(self, text: str, field_name: str) -> str:
        """Extrae valor numérico para forecast/actual/previous."""
        pattern = rf"{field_name}[:\s]+([+-]?[\d.]+%?)"
        match = re.search(pattern, text, re.IGNORECASE)
        return match.group(1) if match else ""

    def to_dict(self):
        return {
            "event": self.event_name,
            "currency": self.currency,
            "impact": self.impact,
            "date": self.date,
            "time": self.time,
            "utc_offset": self.utc_offset,
            "forecast": self.forecast,
            "actual": self.actual,
            "previous": self.previous,
            "source_url": self.source_url,
            "timestamp": self.timestamp,
        }


def _is_valid_iso8601(timestamp: str) -> bool:
    """Valida que el timestamp sea ISO 8601 válido."""
    if not timestamp or not isinstance(timestamp, str):
        return False
    try:
        datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
        return True
    except:
        return False


def enrich_news(raw_news: list[dict]) -> list[dict]:
    """
    Preserva noticias que Parse News extrajo (ya tienen currency, impact, timestamp).

    Solo filtra noticias SIN currency (geopolíticas irrelevantes para forex).
    """
    enriched = []

    for item in raw_news:
        headline = item.get("headline", "")
        if not headline:
            continue

        # SKIP si no tiene currency (noticia geopolítica sin impacto forex)
        if not item.get("currency"):
            continue

        # Preservar EXACTAMENTE lo que Parse News extrajo
        # Validar timestamp: si no es ISO 8601 válido, usar fallback
        timestamp = item.get("timestamp", "")
        if not timestamp or not _is_valid_iso8601(timestamp):
            timestamp = datetime.utcnow().isoformat()

        enriched.append({
            "headline": headline,
            "currency": item.get("currency", ""),
            "impact": item.get("impact", ""),
            "summary": item.get("summary", ""),
            "sentiment": item.get("sentiment", ""),
            "source_url": item.get("source_url", ""),
            "timestamp": timestamp,
        })

    return enriched


def enrich_calendar(raw_calendar: list[dict]) -> list[dict]:
    """
    Procesa eventos de calendario (Parse Calendar ya hizo la extracción).

    Solo preserva y valida lo que Parse Calendar extrajo.
    No re-extrae porque Parse Calendar es más preciso con la tabla HTML.
    """
    enriched = []

    for item in raw_calendar:
        event = item.get("event", "")
        if not event:
            continue

        # Preservar EXACTAMENTE lo que Parse Calendar extrajo
        enriched.append({
            "event": event,
            "currency": item.get("currency", ""),
            "impact": item.get("impact", ""),
            "date": item.get("date", ""),
            "time": item.get("time", ""),
            "utc_offset": item.get("utc_offset", ""),
            "forecast": item.get("forecast", ""),
            "actual": item.get("actual", ""),
            "previous": item.get("previous", ""),
            "source_url": item.get("source_url", ""),
            "timestamp": datetime.utcnow().isoformat(),
        })

    return enriched


def validate_before_agents(market_data: dict) -> tuple[bool, list[str]]:
    """
    Valida que los datos estén completos antes de pasar a LLM agents.

    Retorna: (is_valid, list_of_errors)
    """
    errors = []

    # Validar news
    news = market_data.get("news", [])
    for i, item in enumerate(news):
        if not item.get("headline"):
            errors.append(f"news[{i}]: headline vacío")
        if not item.get("currency"):
            errors.append(f"news[{i}]: currency vacío")
        if not item.get("impact"):
            errors.append(f"news[{i}]: impact vacío")

    # Validar calendar
    calendar = market_data.get("calendar", [])
    for i, item in enumerate(calendar):
        if not item.get("event"):
            errors.append(f"calendar[{i}]: event vacío")
        if not item.get("currency"):
            errors.append(f"calendar[{i}]: currency vacío")
        if not item.get("impact"):
            errors.append(f"calendar[{i}]: impact vacío")

    # Validar prices
    prices = market_data.get("prices", {})
    if not prices:
        errors.append("prices vacío")

    return (len(errors) == 0, errors)
