"""RealLLMClient: adaptador real del puerto LLMClient.

Recibe una función query_fn síncrona (symbol, decision, ml_prob, context) -> dict
que se inyecta desde la composición raíz (main.py). En tests se pasa un fake;
en producción se pasa una función que envuelva query_qwen_unified (async) en
un contexto síncrono (run_in_executor).

Implementa LLMClient por typing estructural:
  - veto(signal: dict) -> bool
  - classify_regime(data: dict) -> str
  - query(symbol, decision, ml_prob, context) -> dict
"""

from typing import Callable, Optional

from src.domain.ports import LLMClient


class RealLLMClient(LLMClient):
    def __init__(self, query_fn: Callable[[str, str, float, dict], dict]):
        self._query_fn = query_fn

    def query(self, symbol: str, decision: str, ml_prob: float,
              context: dict) -> dict:
        return self._query_fn(symbol, decision, ml_prob, context)

    def veto(self, signal: dict) -> bool:
        # RealLLMClient no implementa veto de autoridad; eso vive en
        # services/qwen_authority.py. Se deja como no-op (False = no veto).
        return False

    def classify_regime(self, data: dict) -> str:
        # RealLLMClient no implementa clasificación de régimen por separado;
        # el régimen viene dentro de la respuesta de query(). No-op.
        return "UNKNOWN"
