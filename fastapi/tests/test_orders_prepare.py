"""
Tests del gate de órdenes /v1/orders/prepare (P0-3).

Cobertura:
- Contrato HTTP: token obligatorio, campos request/response.
- Lógica de aprobación: idempotencia por cycle_id, Risk Guardian,
  validadores geométricos, duplicados, registro pending + firma HMAC.
- Contrato EA↔backend (fuente MQL5): el EA llama al gate ANTES de
  trade.Buy/trade.Sell y lleva la versión bumps.
"""
import os
import re
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

import config
import services.order_manager as order_manager
from main import app

TOKEN = {"x-internal-token": "test-token", "x-cycle-id": "gate-test"}


def _order(**overrides):
    payload = {
        "cycle_id": "cycle-gate-1",
        "symbol": "EURUSD",
        "type": "BUY",
        "entry": 1.08500,
        "sl": 1.08000,
        "tp": 1.09500,
        "volume": 0.10,
    }
    payload.update(overrides)
    return payload


class _FakeAsyncClient:
    """Sustituye httpx en toda la app (lifespan + health loop): sin red real."""

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, **kwargs):
        response = MagicMock()
        response.status_code = 200
        response.json = lambda: {"equity": 5000.0, "balance": 5000.0}
        response.raise_for_status = lambda: None
        return response


def _prepare(payload, *, fetchrow=None, risk=(True, ""), fetchval=42, headers=None,
             positions=None, clock_ready=True):
    """POST /v1/orders/prepare con todo el entorno parcheado. Devuelve (response, pool).

    `positions`: contenido de get_positions (P3-1d, default sin posiciones).
    El mock de check_risk queda en `pool.risk_check` para inspeccionar args.
    """
    with ExitStack() as stack:
        stack.enter_context(patch("main.init_pool", AsyncMock()))
        stack.enter_context(patch("main.close_pool", AsyncMock()))
        stack.enter_context(patch("routers.ai.load_inference_artifacts"))
        stack.enter_context(patch("httpx.AsyncClient", _FakeAsyncClient))
        stack.enter_context(
            patch("services.timebase.broker_clock_ready", return_value=clock_ready)
        )

        pool = MagicMock()
        if isinstance(fetchrow, list):
            pool.fetchrow = AsyncMock(side_effect=fetchrow)
        else:
            pool.fetchrow = AsyncMock(return_value=fetchrow)
        pool.fetchval = AsyncMock(return_value=fetchval)
        pool.execute = AsyncMock()
        stack.enter_context(patch("services.order_manager.get_pool", return_value=pool))
        risk_check = stack.enter_context(
            patch("services.risk_guardian.check_risk", return_value=risk)
        )
        risk_flush = stack.enter_context(
            patch("services.risk_state_store.flush_risk_state", AsyncMock())
        )
        stack.enter_context(
            patch("services.mt5_client.get_account", AsyncMock(return_value={"equity": 5000.0}))
        )
        stack.enter_context(
            patch("services.mt5_client.get_positions",
                  AsyncMock(return_value=positions if positions is not None else {"open": []}))
        )

        with TestClient(app) as client:
            response = client.post(
                "/v1/orders/prepare",
                json=payload,
                headers=headers if headers is not None else TOKEN,
            )
    pool.risk_check = risk_check
    pool.risk_flush = risk_flush
    return response, pool


def _ea_source() -> str:
    candidates = (
        "/mql5/AI_Quant_Terminal.mq5",  # Docker: volumen de solo lectura
        os.path.join(os.path.dirname(__file__), "..", "..", "mql5", "AI_Quant_Terminal.mq5"),
    )
    for path in candidates:
        if os.path.exists(path):
            with open(path, "r") as f:
                return f.read()
    raise AssertionError(f"Fuente del EA no encontrada. Probado: {candidates}")


class TestPrepareEndpoint:
    def test_missing_token_returns_422(self):
        response, _ = _prepare(_order(), headers={"x-cycle-id": "gate-test"})
        assert response.status_code == 422

    def test_wrong_token_returns_401(self):
        response, _ = _prepare(
            _order(), headers={"x-internal-token": "bad", "x-cycle-id": "gate-test"}
        )
        assert response.status_code == 401

    def test_approved_order_returns_signed_payload(self):
        response, pool = _prepare(_order())
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["approved"] is True
        payload = data["order_payload"]
        assert payload["cycle_id"] == "cycle-gate-1"
        assert payload["order_db_id"] == 42
        assert payload["type"] == "BUY"
        assert payload["volume"] == 0.10
        assert len(payload["_sig"]) == 64  # HMAC-SHA256 hex
        # Registrado como pending ANTES de enviar
        insert_sql = pool.fetchval.await_args.args[0]
        assert "INSERT INTO orders" in insert_sql and "'pending'" in insert_sql

    def test_unvalidated_broker_clock_blocks_order(self):
        response, _ = _prepare(_order(), clock_ready=False)

        assert response.status_code == 200
        data = response.json()
        assert data["approved"] is False
        assert data["rejection_reason"] == "broker_clock_unavailable"

    def test_cycle_idempotency_rejects_second_order(self):
        response, _ = _prepare(_order(), fetchrow={"id": 7})
        assert response.status_code == 200
        data = response.json()
        assert data["approved"] is False
        assert data["rejection_reason"] == "cycle_already_has_order"

    def test_risk_guardian_blocks(self):
        response, _ = _prepare(_order(), risk=(False, "max_open_positions"))
        data = response.json()
        assert data["approved"] is False
        assert data["rejection_reason"].startswith("risk_guardian_blocked")

    def test_invalid_geometry_rejected(self):
        # BUY con SL por ENCIMA de la entrada
        response, _ = _prepare(_order(sl=1.09000, tp=1.09500))
        data = response.json()
        assert data["approved"] is False
        assert data["rejection_reason"] == "invalid_geometry_buy"

    def test_duplicate_entry_rejected(self):
        # fetchrow #1 (idempotencia) → None; #2 (check_duplicate) → duplicado
        response, _ = _prepare(_order(), fetchrow=[None, {"id": 99}])
        data = response.json()
        assert data["approved"] is False
        assert data["rejection_reason"].startswith("duplicate_order:")

    def test_volume_out_of_range_rejected(self):
        response, _ = _prepare(_order(volume=0.001))
        data = response.json()
        assert data["approved"] is False
        assert data["rejection_reason"].startswith("volume_out_of_range")


class TestUsdExposureGuard:
    """2026-10-21: la orden nueva no puede contradecir la pata USD de una
    posición abierta (EURUSD BUY corto USD vs USDCHF BUY largo USD)."""

    _USDCHF_BUY = {
        "symbol": "USDCHF",
        "type": "buy",
        "volume": 0.05,
        "price": 0.82536,
        "sl": 0.82372,
        "ticket": 58986641010,
    }

    def test_opposite_usd_blocks(self, monkeypatch):
        monkeypatch.setattr(order_manager.settings, "usd_exposure_guard", True)
        response, pool = _prepare(
            _order(type="BUY"),
            positions={"open": [self._USDCHF_BUY]},
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["approved"] is False
        assert data["rejection_reason"].startswith("USD_EXPOSURE_CONFLICT")
        assert "USDCHF" in data["rejection_reason"]
        # El guard corre antes del Risk Guardian: no se consulta ni se inserta.
        pool.risk_check.assert_not_called()
        pool.fetchval.assert_not_called()
        audit = [c.args for c in pool.execute.await_args_list if len(c.args) >= 4]
        assert any(
            args[2] == "order_rejected" and "usd_exposure_conflict" in args[3]
            for args in audit
        )

    def test_same_usd_allows(self, monkeypatch):
        monkeypatch.setattr(order_manager.settings, "usd_exposure_guard", True)
        response, pool = _prepare(
            _order(type="BUY"),
            positions={"open": [{
                "symbol": "GBPUSD", "type": "buy", "volume": 0.05,
                "price": 1.27, "sl": 1.265, "ticket": 11,
            }]},
        )
        assert response.status_code == 200, response.text
        assert response.json()["approved"] is True
        pool.risk_check.assert_called_once()

    def test_sell_vs_usdchf_buy_allows(self, monkeypatch):
        # EURUSD SELL = largo USD; USDCHF BUY = largo USD: alineados.
        monkeypatch.setattr(order_manager.settings, "usd_exposure_guard", True)
        response, pool = _prepare(
            _order(type="SELL", entry=1.08500, sl=1.09000, tp=1.07500),
            positions={"open": [self._USDCHF_BUY]},
        )
        assert response.status_code == 200, response.text
        assert response.json()["approved"] is True
        pool.risk_check.assert_called_once()

    def test_guard_off_preserves_behavior(self, monkeypatch):
        monkeypatch.setattr(order_manager.settings, "usd_exposure_guard", False)
        response, pool = _prepare(
            _order(type="BUY"),
            positions={"open": [self._USDCHF_BUY]},
        )
        assert response.status_code == 200, response.text
        assert response.json()["approved"] is True
        pool.risk_check.assert_called_once()



class TestEAGateContract:
    """La fuente MQL5 debe demostrar que TODA orden pasa por el gate."""

    def test_ea_calls_gate_before_placement(self):
        src = _ea_source()
        assert "/v1/orders/prepare" in src, "El EA no llama al gate del servidor"
        assert "RequestOrderPermission" in src, "Helper de gate ausente"

        gate_buy = src.index('RequestOrderPermission(cycle_id, "BUY"')
        buy = src.index("trade.Buy(")
        assert gate_buy < buy, "trade.Buy() se ejecuta ANTES del gate (P0-3 incumplido)"

        gate_sell = src.index('RequestOrderPermission(cycle_id, "SELL"')
        sell = src.index("trade.Sell(")
        assert gate_sell < sell, "trade.Sell() se ejecuta ANTES del gate (P0-3 incumplido)"

    def test_ea_gate_is_fail_closed(self):
        src = _ea_source()
        # Si el gate no responde (res != 200) debe retornar false sin colocar
        assert "gate_unreachable" in src
        assert 'return false;' in src

    def test_ea_version_bumped_to_14(self):
        src = _ea_source()
        assert re.search(r'#property\s+version\s+"14\.', src), "Falta bump de #property version"
        assert re.search(r'#define\s+EA_VERSION\s+"14\.', src), "Falta bump de EA_VERSION"
        assert "long &orderDbId" in src


# ─── P3-1d: gate de mundo, cupo por par y presupuesto de equity ─────────────

class TestWorldGatePrepare:
    def test_unknown_symbol_rejected(self):
        """Orden de un par no configurado → rechazo explícito (sin tocar riesgo)."""
        response, pool = _prepare(_order(symbol="BTCUSD"))
        assert response.json()["rejection_reason"] == "pair_not_configured"
        pool.risk_check.assert_not_called()

    def test_disabled_pair_rejected(self, monkeypatch):
        # Un par deshabilitado solo existe junto a uno habilitado
        # (el registry prohíbe cero mundos activos, P3-1a).
        monkeypatch.setattr(config.settings, "pairs", [
            {"symbol": "EURUSD", "enabled": True},
            {"symbol": "GBPUSD", "enabled": False},
        ])
        response, _ = _prepare(_order(symbol="GBPUSD"))
        assert response.json()["rejection_reason"] == "pair_disabled"


class TestOpenCountPerSymbol:
    def test_counts_only_request_symbol(self):
        """El cupo de posiciones es POR PAR: solo cuentan las de este mundo."""
        response, pool = _prepare(
            _order(),
            positions={"open": [
                {"symbol": "EURUSD"}, {"symbol": "GBPUSD"}, {"symbol": "EURUSD"},
            ]},
        )
        assert response.json()["approved"] is True
        args = pool.risk_check.call_args
        assert args.args[1] == 2                      # las 2 de EURUSD, no las 3
        assert args.kwargs["symbol"] == "EURUSD"
        # P2-4: la exposición AGREGADA de la cuenta viaja en el mismo check
        assert args.kwargs["total_open_positions"] == 3
        # P2-4: el estado de riesgo se persiste tras cada check (solo si dirty)
        pool.risk_flush.assert_awaited_once_with(symbol="EURUSD")


class TestPairAllocationCeiling:
    def test_volume_exceeding_allocation_rejected(self, monkeypatch):
        # techo = max_volume(0.50) × 50% = 0.25
        monkeypatch.setattr(config.settings, "pairs", [
            {"symbol": "EURUSD", "enabled": True, "equity_allocation_pct": 50}
        ])
        response, _ = _prepare(_order(volume=0.30))
        reason = response.json()["rejection_reason"]
        assert reason.startswith("volume_exceeds_pair_allocation")
        assert "0.25" in reason

    def test_volume_within_allocation_approved(self, monkeypatch):
        monkeypatch.setattr(config.settings, "pairs", [
            {"symbol": "EURUSD", "enabled": True, "equity_allocation_pct": 50}
        ])
        response, _ = _prepare(_order(volume=0.20))
        assert response.json()["approved"] is True

    def test_full_allocation_keeps_global_ceiling(self):
        # 100% (default) → techo idéntico al global de siempre: sin cambio
        response, _ = _prepare(_order(volume=0.50))
        assert response.json()["approved"] is True


class TestG25RiskWiring:
    """G2-5 (orden Félix 2026-10-17): exposición D2/D3 cableada en prepare.

    - D2 on: el riesgo-1R agregado (EUR, convenio B1 vía EURUSD) viaja
      explícito en check_risk; sin precio EURUSD → RISK_RATE_UNAVAILABLE.
    - D3: same_dir_open cuenta posiciones de la MISMA dirección (type del
      MCP en lowercase; el request, uppercase).

    Nota de aislamiento: el parche va ANclado al objeto `settings` que
    order_manager importó a nivel de módulo — test_config.py hace
    importlib.reload(config), y sólo parchear config.settings dejaría el
    objeto viejo intacto (fallo detectado en suite completa).
    """

    def test_same_dir_and_risk_reach_guardian(self, monkeypatch):
        monkeypatch.setattr(order_manager.settings, "max_total_risk_pct", 0.09)
        positions = {"open": [
            {"symbol": "EURUSD", "type": "buy",  "volume": 0.10,
             "price": 1.3300, "sl": 1.3250},
            {"symbol": "GBPUSD", "type": "sell", "volume": 0.10,
             "price": 1.3400, "sl": 1.3450},
        ]}
        with patch("services.mt5_client.get_candles",
                   AsyncMock(return_value=[{"close": 1.33}])):
            response, pool = _prepare(_order(type="BUY"), positions=positions)
        assert response.json()["approved"] is True
        args = pool.risk_check.call_args
        # D3: sólo la EURUSD buy comparte dirección con la petición
        assert args.kwargs["same_dir_open"] == 1
        # D2: 50 pips × 10 USD/pip/lote × 0.10 lot / 1.33 por posición
        assert abs(args.kwargs["total_risk_eur"] - 2 * 50.0 / 1.33) < 1e-6

    def test_rate_unavailable_rejects_order(self, monkeypatch):
        monkeypatch.setattr(order_manager.settings, "max_total_risk_pct", 0.09)
        positions = {"open": [
            {"symbol": "EURUSD", "type": "buy", "volume": 0.10,
             "price": 1.3300, "sl": 1.3250},
        ]}
        with patch("services.mt5_client.get_candles",
                   AsyncMock(side_effect=RuntimeError("feed caído"))):
            response, pool = _prepare(_order(), positions=positions)
        assert response.json()["approved"] is False
        assert "RISK_RATE_UNAVAILABLE" in response.json()["rejection_reason"]
        pool.risk_check.assert_not_called()  # no se mide ⇒ no se decide

    def test_d2_off_skips_rate_fetch(self, monkeypatch):
        # default None → sin llamada a get_candles (cero cambio de comportamiento)
        monkeypatch.setattr(order_manager.settings, "max_total_risk_pct", None)
        positions = {"open": [
            {"symbol": "EURUSD", "type": "buy", "volume": 0.10,
             "price": 1.3300, "sl": 1.3250},
        ]}
        with patch("services.mt5_client.get_candles",
                   AsyncMock(side_effect=AssertionError("no debe llamarse"))):
            response, pool = _prepare(_order(), positions=positions)
        assert response.json()["approved"] is True
        assert pool.risk_check.call_args.kwargs["total_risk_eur"] == 0.0
