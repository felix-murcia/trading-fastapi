"""Fake LLMClient con respuestas predefinidas (canned responses).

Sin imports de proveedores de lenguaje ni de librerías de red. Todas las
respuestas se escriben a mano. Nunca toca la red.
"""

from typing import Optional


class RateLimitError(Exception):
    """El servidor de lenguaje devolvió cupo agotado."""


# ─── Respuestas predefinidas (canned responses) ──────────────────────────

RESPONSE_BUY = {
    "ok": True,
    "abstained": False,
    "quality": 8.0,
    "reason": "trend up 0.5% last hour, RSI 62",
    "bias": "BULLISH",
    "bias_prob": 0.75,
    "confidence_modifier": 1.2,
    "effective_prob": 0.80,
    "regime": "TRENDING",
    "reentry_ok": True,
}

RESPONSE_SELL = {
    "ok": True,
    "abstained": False,
    "quality": 7.0,
    "reason": "price below SMA200 by 40 pips, MACD negative",
    "bias": "BEARISH",
    "bias_prob": 0.25,
    "confidence_modifier": 0.8,
    "effective_prob": 0.20,
    "regime": "TRENDING",
    "reentry_ok": True,
}

RESPONSE_HOLD = {
    "ok": True,
    "abstained": False,
    "quality": 5.0,
    "reason": "mixed signals, no clear edge",
    "bias": "NEUTRAL",
    "bias_prob": 0.50,
    "confidence_modifier": 1.0,
    "effective_prob": 0.50,
    "regime": "RANGING",
    "reentry_ok": True,
}

# Respuesta vacía: el modelo respondió pero sin evidencia (razón vacía).
# Legacy: _abstain_unified → ok=True, abstained=True, quality=5.0.
RESPONSE_EMPTY = {
    "ok": True,
    "abstained": True,
    "quality": 5.0,
    "reason": "",
    "bias": "NEUTRAL",
    "bias_prob": 0.5,
    "confidence_modifier": 1.0,
    "effective_prob": 0.5,
    "regime": "UNKNOWN",
    "reentry_ok": None,
}

# Respuesta con campos faltantes (sin bias_prob, sin confidence_modifier).
# Legacy: parse con defaults (bias_prob=0.5, conf_mod=1.0).
RESPONSE_MISSING_FIELDS = {
    "ok": True,
    "abstained": False,
    "quality": 6.0,
    "reason": "price near support 1.0950",
    "bias": "BULLISH",
    "regime": "TRENDING",
    "reentry_ok": True,
}

# Respuesta malformada: el modelo devolvió JSON inválido.
# Legacy: degradado → ok=False, quality=0.0.
RESPONSE_MALFORMED = "not json at all"


class FakeLLMClient:
    """Fake que satisface LLMClient por typing estructural.

    `response`: dict devuelto por query (o None → degradado).
    `side_effect`: Excepción lanzada por query (tiempo agotado, cupo, etc.).
    """

    def __init__(
        self,
        response: Optional[dict] = None,
        side_effect: Optional[BaseException] = None,
    ):
        self._response = response
        self._side_effect = side_effect
        self.calls = []

    def veto(self, signal: dict) -> bool:
        return False

    def classify_regime(self, data: dict) -> str:
        return "NEUTRAL"

    def query(self, symbol: str, decision: str, ml_prob: float, context: dict) -> dict:
        self.calls.append(
            {"symbol": symbol, "decision": decision, "ml_prob": ml_prob, "context": context}
        )
        if self._side_effect is not None:
            raise self._side_effect
        if self._response is None:
            return {
                "ok": False,
                "abstained": False,
                "quality": 0.0,
                "reason": "modelo no disponible",
                "bias": "NEUTRAL",
                "bias_prob": 0.5,
                "confidence_modifier": 1.0,
                "effective_prob": ml_prob,
                "regime": "UNKNOWN",
            }
        return dict(self._response)
