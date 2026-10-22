"""
G3 · U-1 · USDCHF REGISTRADO COMO CANARIO APAGADO (orden Félix 2026-10-20).

Registro de tareas: `doc/backlog-g3-usdchf-2026-10-20.md`.

Contrato de U-1 (sin lógica de trading nueva):
  · USDCHF queda LISTADO como canario con `enabled=false` y sus límites
    (DD diario 2 %, 20 % del equity) — listar NO habilita (invariante G2-4);
  · una petición de USDCHF ⇒ HOLD con la razón `pair_disabled` y SIN tocar
    MT5 ni noticias (fail-closed por worlds);
  · el lookback de retrain de USDCHF = 30 000 H1 (hay historia suficiente:
    verificado en el MCP el 20-10: 2021-12-23 → hoy, 7 s de fetch);
  · NO existe bundle de USDCHF ⇒ aunque alguien lo habilite, no puede
    operar (el canario no nace con modelo).
"""
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from main import app
from tests.test_p31b_pair_gate import TOKEN, _ExplodingClient  # noqa: F401


class TestUsdChfCanaryRegistration:
    def test_usdchf_is_listed_as_disabled_canary_with_its_limits(self):
        from config import settings

        specs = {s["symbol"]: s for s in settings.pairs}
        assert "USDCHF" in specs, "USDCHF debe quedar LISTADO (apagado)"
        assert specs["USDCHF"].get("enabled") is False, (
            "listar USDCHF no lo habilita (invariante G2-4)"
        )
        assert specs["USDCHF"].get("daily_loss_pct") == 2.0
        assert specs["USDCHF"].get("equity_allocation_pct") == 20

    def test_usdchf_declares_the_rule_decider(self):
        """El canario declara SU fuente de decisión (la regla medida): el
        default del código lo deja APAGADO (como GBPUSD) y el despliegue lo
        enciende con .env PAIRS."""
        from config import settings

        specs = {s["symbol"]: s for s in settings.pairs}
        assert specs["USDCHF"].get("decision_source") == "momentum24", (
            "USDCHF debe declarar el decisor por regla (momentum24)"
        )

    def test_eurusd_stays_enabled(self):
        """A1/A3: registrar USDCHF no toca el mundo vivo."""
        from config import settings

        specs = {s["symbol"]: s for s in settings.pairs}
        assert specs["EURUSD"].get("enabled") is True
        assert specs.get("GBPUSD", {}).get("enabled") is False, (
            "GBPUSD sigue apagado (G2-4 bloqueado por diseño)"
        )

    def test_usdchf_request_holds_without_touching_mt5_or_news(self):
        """El canario apagado ⇒ HOLD con `pair_disabled`, sin red."""
        from config import settings
        from routers import ai

        original = settings.pairs
        settings.pairs = [
            {"symbol": "EURUSD", "enabled": True},
            {"symbol": "GBPUSD", "enabled": False},
            {"symbol": "USDCHF", "enabled": False, "daily_loss_pct": 2.0,
             "equity_allocation_pct": 20},
        ]
        ai._REGISTRY = None
        try:
            news = AsyncMock(return_value=(False, None))
            with patch("main.init_pool", AsyncMock()), \
                 patch("main.close_pool", AsyncMock()), \
                 patch("routers.ai.load_inference_artifacts", MagicMock()), \
                 patch("routers.ai.is_news_blackout", news):
                with TestClient(app) as client:
                    # El parche va DESPUÉS del arranque: el gate se mide en
                    # la petición, no en el bootstrap (que lee equity).
                    with patch("httpx.AsyncClient", _ExplodingClient):
                        r = client.post(
                            "/api/v1/ai/predict",
                            json={"symbol": "USDCHF", "timeframe": "H1", "position": 0},
                            headers={**TOKEN, "x-cycle-id": "g3-u1-usdchf"},
                        )
        finally:
            settings.pairs = original
            ai._REGISTRY = None

        assert r.status_code == 200, r.text
        payload = r.json()
        assert payload["decision"] == "HOLD"
        assert payload["quality_reason"] == "pair_disabled"
        news.assert_not_awaited()

    def test_usdchf_retrain_lookback_is_30000(self):
        """Hay historia suficiente en el MCP para el retrain del canario."""
        from services.auto_retrain import RetrainConfig

        assert RetrainConfig.lookback_for("USDCHF") == 30000

    def test_usdchf_has_no_bundle_so_it_cannot_operate(self):
        """El canario no nace con modelo: sin bundle, no hay operación."""
        from core.pair import default_model_path

        path = default_model_path("USDCHF")
        assert path.endswith(os.path.join("USDCHF", "ppo_trading_bot_v3.zip"))
        assert not os.path.exists(path), (
            "USDCHF no debe tener bundle hasta que uno pase el gate"
        )
