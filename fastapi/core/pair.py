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

# Único mundo que existía antes de P3-1: conserva las rutas legadas en raíz
# (bundle y backups en la raíz de ml/). Los demás viven en su namespace.
LEGACY_SYMBOL = "EURUSD"


def default_model_path(symbol: str) -> str:
    """Bundle propio por par (decisión P3-1: modelo por par).

    `PPO_OUTPUT_DIR` (por defecto `/app/ml`) es la única fuente de la
    convención: `routers.ai` deriva sus rutas de esta función, así que
    núcleo y router nunca divergen.
    """
    model_dir = os.getenv("PPO_OUTPUT_DIR", "/app/ml")
    if symbol == LEGACY_SYMBOL:
        return os.path.join(model_dir, _LEGACY_MODEL_FILENAME)
    return os.path.join(model_dir, symbol, _LEGACY_MODEL_FILENAME)


@dataclass(frozen=True)
class PairConfig:
    symbol: str
    enabled: bool
    model_path: str
    # P3-1d: fracción de equity (0-100] que el par puede mandar — techo de
    # size por par. 100 (default) = el techo global de siempre.
    equity_allocation_pct: float = 100.0
    # P3-3: límite de pérdida diaria PROPIO del par (canario GBPUSD=2%,
    # EURUSD sigue en el default 5% del guardian). Ausente = 5.0.
    daily_loss_pct: float = 5.0

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
        # P3-1d: asignación de equity — ausente = 100 (todo el techo global);
        # presente = número en (0, 100]. Nada de defaults por heurística.
        allocation = spec.get("equity_allocation_pct", 100.0)
        if isinstance(allocation, bool) or not isinstance(allocation, (int, float)):
            raise ValueError(
                f"PAIRS: 'equity_allocation_pct' debe ser numérico en {spec!r}: "
                f"{allocation!r}"
            )
        allocation = float(allocation)
        if not (0.0 < allocation <= 100.0):
            raise ValueError(
                f"PAIRS: 'equity_allocation_pct' de {symbol!r} fuera de (0, 100]: "
                f"{allocation!r}"
            )
        # P3-3: límite de pérdida diaria del par — ausente = 5.0 (default del
        # guardian); presente = número en (0, 100]. La validación del spec es
        # una sola (aquí): risk_guardian reutiliza esta clase al crear su guardián.
        daily_loss = spec.get("daily_loss_pct", 5.0)
        if isinstance(daily_loss, bool) or not isinstance(daily_loss, (int, float)):
            raise ValueError(
                f"PAIRS: 'daily_loss_pct' debe ser numérico en {spec!r}: "
                f"{daily_loss!r}"
            )
        daily_loss = float(daily_loss)
        if not (0.0 < daily_loss <= 100.0):
            raise ValueError(
                f"PAIRS: 'daily_loss_pct' de {symbol!r} fuera de (0, 100]: "
                f"{daily_loss!r}"
            )
        return PairConfig(
            symbol=symbol,
            enabled=enabled,
            model_path=model_path,
            equity_allocation_pct=allocation,
            daily_loss_pct=daily_loss,
        )


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
