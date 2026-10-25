"""Fábrica de adaptadores de broker — B2.1 (Desacoplamiento de broker).

`build_adapters(broker_type)` devuelve `(MarketDataPort, ExecutionPort)`
según el tipo de broker configurado.

- `mt5_demo` y `mt5_real`: devuelven los adaptadores MT5 existentes
  (`core.adapters.MT5MarketDataAdapter` / `MT5ExecutionDataAdapter`).
  La URL de conexión (demo vs real) se resuelve en `services.mt5_client`
  vía `settings.broker_http_url` / `settings.mt5_http_url`; la fábrica
  no la conoce.
- Cualquier otro valor: `ValueError` (falla rápido, sin silencios).

Regla: **zero import de `services.mt5_client` a nivel de módulo**.
Los adaptadores usan late binding (import dentro de cada método), de modo
que `patch("services.mt5_client.get_candles")` siga funcionando.
"""

from core.adapters import (
    MT5ExecutionDataAdapter,
    MT5MarketDataAdapter,
    MockBrokerAdapter,
)

# Tipos de broker soportados. `mt5_demo` y `mt5_real` son de producción;
# `mock` es solo para tests (no toca red ni `services.mt5_client`).
_BROKER_ADAPTERS: dict[str, tuple[type, type]] = {
    "mt5_demo": (MT5MarketDataAdapter, MT5ExecutionDataAdapter),
    "mt5_real": (MT5MarketDataAdapter, MT5ExecutionDataAdapter),
    "mock": (MockBrokerAdapter, MockBrokerAdapter),
}


def build_adapters(broker_type: str) -> tuple[type, type]:
    """Devuelve `(MarketDataPort, ExecutionPort)` **clases** para el `broker_type` dado.

    El caller (container) instancia una vez por par, de modo que cada par
    recibe su propio juego de adapter instances (aislamiento de mundos).

    Raises:
        ValueError: si `broker_type` no es `mt5_demo` ni `mt5_real`.
    """
    try:
        market_cls, execution_cls = _BROKER_ADAPTERS[broker_type]
    except KeyError:
        raise ValueError(
            f"broker_type desconocido: {broker_type!r}. "
            f"Valores soportados: {sorted(_BROKER_ADAPTERS)}."
        ) from None
    return market_cls, execution_cls
