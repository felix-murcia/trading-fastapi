"""Composition root del núcleo — P3-1a.

Único punto del sistema que instancia adapters y arma `PairContext`s. Los
routers/services no importan adapters directamente: dependen de los puertos
y reciben el registro ya construido (dependencia invertida).

Se construye de forma **explícita** (nunca en import de módulo): así los
tests pueden parchear `services.*` antes de que exista ningún adapter, y un
arranque con especificaciones inválidas falla de inmediato — sin mundos a
medias.
"""
from core.adapters import QwenLLMAdapter
from core.broker_factory import build_adapters
from core.pair import PairConfig, PairContext, PairRegistry


def build_registry(specs: list[dict], broker_type: str | None = None) -> PairRegistry:
    """Construye un mundo por cada especificación de par.

    Errores explícitos (sin silencios):
    - especificación inválida → TypeError/ValueError (ver PairConfig);
    - símbolo duplicado → ValueError (cada par se configura una vez);
    - ningún par habilitado → ValueError (opera sin mundos activos = error);
    - broker_type desconocido → ValueError (vía broker_factory).

    `broker_type` se lee de `settings.broker_type` si no se pasa explícitamente,
    de modo que los tests pueden inyectarlo sin recargar settings.
    """
    if not isinstance(specs, list) or not specs:
        raise ValueError(f"PAIRS: se espera una lista no vacía de pares, recibido: {specs!r}")

    # Resuelve el tipo de broker: parámetro explícito > settings > default.
    if broker_type is None:
        from config import settings
        broker_type = settings.broker_type

    market_cls, execution_cls = build_adapters(broker_type)

    contexts: dict[str, PairContext] = {}
    for spec in specs:
        config = PairConfig.from_spec(spec)
        if config.symbol in contexts:
            raise ValueError(
                f"PAIRS: símbolo duplicado {config.symbol!r} — cada par se "
                f"configura exactamente una vez."
            )
        # Juego de adapters propio por par: mundos sin acoplamiento.
        contexts[config.symbol] = PairContext(
            config=config,
            market=market_cls(),
            execution=execution_cls(),
            llm=QwenLLMAdapter(),
        )

    if not any(c.config.enabled for c in contexts.values()):
        raise ValueError(
            "PAIRS: ningún par con enabled=true — el sistema no puede operar "
            "sin al menos un par habilitado."
        )
    return PairRegistry(contexts)


def build_registry_from_settings() -> PairRegistry:
    """Registro a partir de `settings.pairs` (defecto: EURUSD habilitado)."""
    from config import settings
    return build_registry(settings.pairs)
