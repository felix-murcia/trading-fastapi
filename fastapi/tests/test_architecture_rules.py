"""Reglas de CLAUDE.md ejecutables (2026-10-22).

POR QUÉ EXISTE
--------------
Las reglas de `CLAUDE.md` eran prosa: nada impedía que un cambio volviera a
meter una segunda conversión de dinero (la orden 80), un `except Exception`
nuevo o una función de 1200 líneas. Este módulo las convierte en aserciones
mecánicas. No arregla la deuda histórica: la CONGELA en
`architecture_baseline.py` para que no pueda crecer.

Reglas verificadas
------------------
1. DINERO — una sola puerta: `expected_close_eur` (conversión pips→EUR) sólo
   la usa el contrato de dinero; `evaluate_trade` (lote+TP) sólo la llama el
   serve; ningún otro módulo importa las piezas sueltas.
2. CAPAS — `core/` (dominio) no importa infraestructura; `ml/` no importa
   `routers`/`db` y de `services` sólo la deuda declarada.
3. TAMAÑO — ninguna función nueva de 80+ líneas; las existentes no crecen.
4. ERRORES — ningún `except Exception` nuevo (AGENTS.md: "No silent
   failures").
5. HIGIENE — ningún import sin uso en el runtime.
"""
from __future__ import annotations

import ast
import pathlib

from tests.architecture_baseline import BIG_FUNCS, EXCEPT_EXCEPTION

FASTAPI = pathlib.Path(__file__).resolve().parents[1]
RUNTIME_DIRS = ("routers", "services", "core", "ml")
RUNTIME_FILES = ("main.py", "config.py")
INFRA_RAIZ = ("routers", "db", "httpx", "asyncpg", "psycopg", "fastapi")

# Deuda de capas DECLARADA (hoy no se puede pagar sin tocar el env de
# entrenamiento). Está en el punto de mira; ninguna entrada nueva se acepta.
#   · el env usa el sizing P2-2 (misma fórmula que /predict);
#   · el trainer promueve bundles a través del registry.
CAPAS_DEUDA = {
    ("ml/trading_env_v2.py", "services.position_sizing"),
    ("ml/train_ppo_v3.py", "services.model_registry"),
}

# `core/adapters.py` ES la frontera de infraestructura (implementa los puertos
# de `core/ports.py` sobre `services/*`): importarlos es su razón de existir.
# `core/container.py` sólo compone adapters. El resto de `core/` es dominio
# puro y no puede tocar infraestructura.
FRONTERA_INFRA = {"core/adapters.py", "core/container.py"}


def _runtime_files() -> list[pathlib.Path]:
    files = [FASTAPI / name for name in RUNTIME_FILES]
    for d in RUNTIME_DIRS:
        for p in sorted((FASTAPI / d).rglob("*.py")):
            rel = p.relative_to(FASTAPI).as_posix()
            if "__pycache__" in rel or rel.startswith("ml/archive/"):
                continue
            files.append(p)
    return files


def _rel(p: pathlib.Path) -> str:
    return p.relative_to(FASTAPI).as_posix()


def _imports(tree: ast.AST) -> list[tuple[str, int]]:
    """(módulo, línea) de cada import absoluto del fichero."""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [(a.name, node.lineno) for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            out.append((node.module, node.lineno))
    return out


def _imported_names(tree: ast.AST) -> dict[str, set[str]]:
    """nombre importado → ficheros que lo importan (sólo runtime)."""
    out: dict[str, set[str]] = {}
    for p in _runtime_files():
        tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                for alias in node.names:
                    out.setdefault(alias.name, set()).add(_rel(p))
    return out


def test_dinero_una_sola_puerta():
    """Una conversión y una decisión de lote/TP para TODA la producción.

    La orden 80 salió de aquí: lote, SL, TP y recompensa se calculaban en
    cuatro sitios con cuatro conversiones. Sólo el contrato puede decidir.
    """
    importadores = _imported_names(_runtime_files)
    # Los DOS consumidores legítimos de la conversión pips→EUR son el contrato
    # (que decide) y el guardián de riesgo total (que la LEE para medir la
    # exposición: antes dividía a mano y para USDCHF erraba −17,8 %).
    assert importadores.get("expected_close_eur") == {
        "services/trade_contract.py",
        "services/order_manager.py",
    }, (
        "la conversión pips→EUR sólo puede usarla el contrato (decide) y el "
        "guardián de riesgo (lee); nadie más la reimplementa"
    )
    assert importadores.get("evaluate_trade") == {"routers/ai.py"}, (
        "lote+TP sólo los decide el contrato, en el serve"
    )
    assert not importadores.get("min_lot_for_protection"), (
        "el suelo de protección vive en el contrato (I2), no en una función aparte"
    )
    assert not importadores.get("pip_value_per_lot"), (
        "el pip en divisa de cotización no es dinero: usa pip_value_eur_per_lot"
    )


def test_una_sola_definicion_de_conversion():
    """Nadie reimplementa la conversión en otro módulo (segundo motor)."""
    definidas: dict[str, set[str]] = {}
    for p in _runtime_files():
        tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                definidas.setdefault(node.name, set()).add(_rel(p))
    for nombre, esperado in (
        ("expected_close_eur", {"services/position_sizing.py"}),
        ("pip_value_eur_per_lot", {"services/trade_contract.py"}),
        ("evaluate_trade", {"services/trade_contract.py"}),
    ):
        assert definidas.get(nombre) == esperado, (
            f"{nombre} debe existir una sola vez en {esperado}: "
            f"{definidas.get(nombre)}"
        )


def test_capas_dominio_no_importa_infraestructura():
    """Hexagonal: `core/` es dominio/puertos; `ml/` no conoce la web ni la BD."""
    violaciones = []
    for p in _runtime_files():
        rel = _rel(p)
        if rel in FRONTERA_INFRA:
            continue
        tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
        for modulo, linea in _imports(tree):
            raiz = modulo.split(".")[0]
            if rel.startswith("core/") and raiz in INFRA_RAIZ + ("services",):
                violaciones.append((rel, linea, modulo))
            if rel.startswith("ml/") and raiz in INFRA_RAIZ:
                violaciones.append((rel, linea, modulo))
            if rel.startswith("ml/") and raiz == "services":
                if (rel, modulo) not in CAPAS_DEUDA:
                    violaciones.append((rel, linea, modulo))
    assert not violaciones, f"violaciones de capas: {violaciones}"


def test_complejidad_no_crece():
    """Ninguna función nueva de 80+ líneas y ninguna existente se alarga."""
    observadas: dict[str, int] = {}
    for p in _runtime_files():
        rel = _rel(p)
        tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                lineas = node.end_lineno - node.lineno + 1
                if lineas >= 80:
                    observadas[f"{rel}::{node.name}"] = lineas
    nuevas = {k: v for k, v in observadas.items() if k not in BIG_FUNCS}
    assert not nuevas, f"funciones NUEVAS de 80+ líneas (divídelas): {nuevas}"
    crecidas = {
        k: (BIG_FUNCS[k], v) for k, v in observadas.items() if v > BIG_FUNCS[k]
    }
    assert not crecidas, f"funciones que han CRECIDO: {crecidas}"


def test_except_exception_no_crece():
    """AGENTS.md: 'No silent failures' — la deuda de capturas genéricas no crece."""
    observadas: dict[str, int] = {}
    for p in _runtime_files():
        rel = _rel(p)
        cuenta = p.read_text(encoding="utf-8", errors="replace").count("except Exception")
        if cuenta:
            observadas[rel] = cuenta
    nuevos = {k: v for k, v in observadas.items() if k not in EXCEPT_EXCEPTION}
    assert not nuevos, f"ficheros NUEVOS con `except Exception`: {nuevos}"
    crecidos = {
        k: (EXCEPT_EXCEPTION[k], v) for k, v in observadas.items() if v > EXCEPT_EXCEPTION[k]
    }
    assert not crecidos, f"`except Exception` que ha CRECIDO: {crecidos}"


def test_sin_imports_muertos():
    """Ningún import sin uso en el runtime (código muerto = ruido que engaña)."""
    muertos = []
    for p in _runtime_files():
        tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
        usados = {
            node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            for alias in node.names:
                nombre = alias.asname or alias.name
                if nombre == "annotations":       # __future__: no es un nombre
                    continue
                if nombre not in usados:
                    muertos.append((_rel(p), node.lineno, nombre))
    assert not muertos, f"imports sin uso: {muertos}"
