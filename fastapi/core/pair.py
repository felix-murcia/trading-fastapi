"""Par de divisas como mundo independiente (P3-1).

- `PairConfig`: identidad del par + bundle propio. EURUSD conserva la ruta
  legada (es el modelo histórico); cada otro par vive en su directorio
  aislado `ml/<SYMBOL>/` — añadir un par nunca mueve archivos de otro.
- `PairContext`: un par + sus puertos. El estado de un par no se comparte
  con ningún otro (aislamiento total entre pares).
- `PairRegistry`: índice de contextos. Símbolo desconocido → `None` y
  deshabilitado → `config.enabled = False`: el caller decide HOLD — el
  núcleo nunca inventa un mundo ni cae en silencio.

Toda especificación inválida (sin `symbol`, sin `enabled` explícito, no
dict) es un `ValueError` en el arranque: configuración ambigua = error
temprano, nunca un fallback que active un par por accidente.
"""
import os
from dataclasses import dataclass

from core.ports import ExecutionPort, LLMPort, MarketDataPort

_LEGACY_MODEL_FILENAME = "ppo_trading_bot_v3.zip"


def default_model_path(symbol: str) -> str:
    """Bundle propio por par (decisión P3-1: modelo por par).

    `PPO_OUTPUT_DIR` (por defecto `/app/ml`) es la única fuente de la
    convención: `routers.ai` deriva sus rutas de esta función, así que
    núcleo y router nunca divergen.
    """
    model_dir = os.getenv("PPO_OUTPUT_DIR", "/app/ml")
    if symbol == "EURUSD":
        return os.path.join(model_dir, _LEGACY_MODEL_FILENAME)
    return os.path.join(model_dir, symbol, _LEGACY_MODEL_FILENAME)


@dataclass(frozen=True)
class PairConfig:
    symbol: str
    enabled: bool
    model_path: str

    @staticmethod
    def from_spec(spec: dict) -> "PairConfig":
        if not isinstance(spec, dict):
            raise TypeError(
                f"PAIRS: cada especificación debe ser un dict, recibido: {spec!r}"
            )
        symbol = spec.get("symbol")
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError(f"PAIRS: especificación sin 'symbol' válido: {spec!r}")
        # 'enabled' obligatorio: listar un par en PAIRS no lo activa por sí solo.
        if "enabled" not in spec:
            raise ValueError(
                f"PAIRS: especificación de {symbol!r} sin 'enabled' explícito "
                f"(true/false) — la activación de un par nunca es implícita."
            )
        enabled = spec["enabled"]
        if not isinstance(enabled, bool):
            raise ValueError(
                f"PAIRS: 'enabled' debe ser bool en {spec!r}, recibido: {enabled!r}"
            )
        model_path = spec.get("model_path")
        if model_path is None:
            model_path = default_model_path(symbol)
        elif not isinstance(model_path, str) or not model_path.strip():
            raise ValueError(f"PAIRS: 'model_path' inválido en {spec!r}: {model_path!r}")
        return PairConfig(symbol=symbol, enabled=enabled, model_path=model_path)


@dataclass(frozen=True)
class PairContext:
    """Un par aislado: su config y sus puertos, compartidos con nadie."""

    config: PairConfig
    market: MarketDataPort
    execution: ExecutionPort
    llm: LLMPort


class PairRegistry:
    """Índice de mundos por par. Inmutable tras la construcción."""

    def __init__(self, contexts: dict[str, PairContext]):
        if not contexts:
            raise ValueError(
                "PairRegistry: sin pares configurados — el sistema no puede "
                "operar sin al menos un mundo definido."
            )
        self._contexts = dict(contexts)

    def get(self, symbol: str) -> PairContext | None:
        """Contexto del par o `None` si el símbolo no está configurado."""
        return self._contexts.get(symbol)

    def enabled(self) -> list[PairContext]:
        """Solo los pares con `enabled=True` (los mundos activos)."""
        return [c for c in self._contexts.values() if c.config.enabled]

    def symbols(self) -> list[str]:
        """Todos los símbolos configurados (habilitados y deshabilitados)."""
        return list(self._contexts)

    def __len__(self) -> int:
        return len(self._contexts)

    def __contains__(self, symbol: str) -> bool:
        return symbol in self._contexts
